"""Agnes 视觉识别客户端：图片 → 结构化模型规格 JSON。

调用 Agnes 的 OpenAI 兼容 chat completions 接口
(POST /v1/chat/completions, model=agnes-2.0-flash)，
以 system prompt 约束其输出符合 SPEC_SCHEMA 的 JSON。
若配置了本地图片文件，则按 base64 data URL 作为 image_url 传入
（OpenAI 多模态消息格式，兼容接口通常支持；若 400 则降级为纯文本描述）。
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Optional

import requests

from .core import AgentConfig, SPEC_SCHEMA

VISION_PROMPT = f"""\
你是一位资深 3D CAD 工程师 + 逆向工程专家。用户会给你一张产品/零件/物体的照片，
请仔细分析其几何结构，并输出一个可直接驱动 OpenSCAD 建模的 JSON 规格。

要求：
1. 严格输出且只输出一个 JSON 对象（不要 markdown 代码块、不要解释文字），
   结构必须完全符合以下 schema：
{SPEC_SCHEMA}

2. 几何判断原则：
   - 优先用最简单的 CSG 原语表达：方块/圆柱/球/多面体 + union/difference/translate/rotate。
   - 尺寸以毫米(mm)为单位，给出合理工程估计（例如手机壳约 150x75x8mm）。
   - 孔、凹槽、切角等"减去"的特征放入对应 part 的 holes。
   - 装配关系写入 assembly 字段。
3. 对于无法用简单原语精确表达的曲面特征，用 polyhedron 近似或退化为
   圆柱/球近似，并在 part.note 中说明近似方式。
4. 若图片模糊或无法判断，请给出最合理的假设并全部记录在 description 与 print_tips 中。
"""


class SpecParseError(RuntimeError):
    pass


class AgnesVision:
    """封装 Agnes chat completions 调用，产出模型规格 dict。"""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.require_api_key()}",
            "Content-Type": "application/json",
        }

    def _image_data_url(self, image_path: Path) -> str:
        # 使用预处理后的小图（_prep 目录）避免超大 base64 体
        p = Path(image_path)
        suffix = p.suffix.lower().lstrip(".") or "png"
        mime = "image/png" if suffix == "png" else f"image/{suffix}"
        b64 = base64.b64encode(p.read_bytes()).decode()
        return f"data:{mime};base64,{b64}"

    def describe_from_text(self, description: str) -> dict[str, Any]:
        """无图片场景：仅凭文字描述建模。"""
        messages = [
            {"role": "system", "content": VISION_PROMPT + self.config.extra_system},
            {
                "role": "user",
                "content": f"请根据以下文字描述生成 OpenSCAD 模型规格 JSON：\n{description}",
            },
        ]
        return self._chat(messages)

    def recognize(self, image_path: Path, user_hint: str = "") -> dict[str, Any]:
        """图片识别：先尝试带图多模态消息；若接口拒绝则降级为纯文本请求
        （附带对图片文件名的描述），保证流程不中断。

        注意：429/5xx 已由 _chat 自动重试；只有 400/401/422 类
        "消息格式不被接受"才降级为纯文本。
        """
        system = VISION_PROMPT + self.config.extra_system
        data_url = self._image_data_url(image_path)
        multimodal: list[dict[str, Any]] = [
            {"type": "text", "text": "请分析这张图片并输出 OpenSCAD 模型规格 JSON。"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        if user_hint:
            multimodal.insert(0, {"type": "text", "text": f"用户补充说明: {user_hint}"})

        try:
            return self._chat([
                {"role": "system", "content": system},
                {"role": "user", "content": multimodal},
            ])
        except RuntimeError as e:
            msg = str(e).lower()
            # 仅当明确是图片消息格式不被接受时才降级；
            # 限流/服务端暂不可用等已由重试处理，不应降级为纯文本
            # (否则丢失图片信息，且纯文本同样会限流)。
            if "http 400" in msg or "http 422" in msg or "http 401" in msg:
                print(f"[agnes] 多模态图片消息被拒绝({e})，降级为纯文本请求")
                fallback = (
                    f"图片文件: {image_path.name}。"
                    + (f"用户补充说明: {user_hint}" if user_hint else "")
                    + " 请基于图片文件名与补充说明给出最合理的 OpenSCAD 模型规格 JSON。"
                )
                return self._chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": fallback},
                ])
            raise

    # ------------------------------------------------------------------
    def _chat_raw(self, messages: list[dict[str, Any]]) -> str:
        """通用对话：返回 LLM 回复的原始文本，不做 JSON 规格解析。

        用于预览图审查、自由问答等不需要提取 spec 的场景。
        同样带 429/5xx 指数退避重试。
        """
        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.post(
                    url, headers=self._headers(), json=payload,
                    timeout=self.config.api_timeout,
                )
            except requests.RequestException as e:
                if attempt == max_attempts:
                    raise RuntimeError(f"Agnes API 网络异常: {e}") from e
                self._backoff(attempt, "network")
                continue

            code = resp.status_code
            if code == 200:
                data = resp.json()
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError) as e:
                    raise RuntimeError(
                        f"Agnes 返回结构异常: {json.dumps(data, ensure_ascii=False)[:500]}"
                    ) from e

            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:300]

            retryable = code in (429, 500, 502, 503, 504)
            if not retryable or attempt == max_attempts:
                raise RuntimeError(f"Agnes API HTTP {code}: {detail or resp.text[:300]}")

            base_wait = 6 if code == 429 else 10
            wait = min(base_wait * (2 ** (attempt - 1)), 60)
            print(f"[agnes] HTTP {code} (尝试 {attempt}/{max_attempts}): {detail}")
            print(f"[agnes] {wait}s 后重试…")
            import time
            time.sleep(wait)

        raise RuntimeError("Agnes API 重试耗尽")

    #: 预览图审查系统提示：把渲染出的 2D 预览图 + 原图回传 LLM，判断是否吻合预期
    REVIEW_PROMPT = """\
你是一位 3D 模型质检工程师。用户给你两张图：
  ① 原始参考图（用户提供的照片）
  ② OpenSCAD 渲染出的 3D 模型预览图
请判断渲染图是否忠实还原了原图的核心几何结构（形状、比例、部件布局、相对位置）。

只输出一个 JSON 对象（不要其他文字），结构如下：
{
  "match": true/false,            // 渲染图是否足够接近原图预期
  "score": 0.0~1.0,              // 相似度评分
  "issues": ["string", ...],     // 不符合预期之处（match=true 时可为空）
  "suggested_fix": {             // match=false 时给出修正规格建议（JSON 片段）
    "reason": "string",           // 为什么当前规格不对
    "spec_patch": {              // 要修改/新增/删除的 part 级字段（同 SPEC_SCHEMA 结构）
      ...                        // 例如 {"parts": [...修正后的完整 parts...]} 或具体字段
    }
  },
  "verdict": "string"            // 一句话中文结论
}
要求：
- 重点看几何结构而非颜色/贴图（CSG 模型不含纹理）。
- 若只是细节偏差但整体轮廓正确，可判 match=true。
- 若形状/比例/部件明显错位或缺失，判 match=false。
- 修正原则（重要）：spec_patch.parts 必须保留原规格中所有已有部件，
  只针对 issues 描述的问题做增量修改（新增缺失部件、调整尺寸/位置），
  严禁删除已有的正确部件或简化为更少的部件。
- spec_patch.parts 必须是完整的 parts 数组（非增量），保持 schema 不变。
- 若 issues 无法通过现有 CSG 原语解决，可新增 cube/cylinder/sphere 近似。
"""

    def review_preview(
        self,
        original_image: Path,
        preview_image: Path,
        spec: dict[str, Any],
        user_hint: str = "",
        extra_views: Optional[list[Path]] = None,
    ) -> dict[str, Any]:
        """审查渲染图：回传原图 + 预览图（可多视角）+ 当前规格，让 LLM 判断是否吻合。

        extra_views: 额外的多视角预览图路径（可选），用于多角度比对。
        返回 dict，至少含 {match: bool, score: float, issues: list, verdict: str}。
        若 LLM 限流/不可用或解析失败，返回 {"match": True, "skipped": True, ...}
        以便流程不中断（保守放行，避免死循环）。
        """
        orig_url = self._image_data_url(original_image)
        prev_url = self._image_data_url(preview_image)
        # 预览图主图
        preview_imgs = [prev_url]
        view_labels = ["主视角"]
        if extra_views:
            for i, v in enumerate(extra_views):
                try:
                    preview_imgs.append(self._image_data_url(v))
                    view_labels.append(f"视角{i + 1}")
                except Exception:  # noqa: BLE001
                    continue

        content: list[dict[str, Any]] = [
            {"type": "text",
             "text": f"当前规格:\n{json.dumps(spec, ensure_ascii=False)[:3000]}"},
            {"type": "image_url", "image_url": {"url": orig_url}},
            {"type": "text", "text": "以下是渲染预览图（多角度）:"},
        ]
        for label, url in zip(view_labels, preview_imgs):
            content.append({"type": "text", "text": f"[{label}]"})
            content.append({"type": "image_url", "image_url": {"url": url}})
        if user_hint:
            content.insert(0, {"type": "text", "text": f"用户补充说明: {user_hint}"})

        try:
            raw = self._chat_raw([
                {"role": "system", "content": self.REVIEW_PROMPT},
                {"role": "user", "content": content},
            ])
            return self._extract_review(raw)
        except Exception as e:  # noqa: BLE001
            # 审查环节失败（限流/网络/解析）→ 保守放行，避免阻断建模
            print(f"[review] 审查请求失败，保守放行: {e}")
            return {"match": True, "score": -1.0, "issues": [],
                    "verdict": f"审查跳过: {e}", "skipped": True}

    @staticmethod
    def _extract_review(content: str) -> dict[str, Any]:
        """从 LLM 回复中提取审查 JSON。失败时保守放行。"""
        text = content.strip()
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                return {"match": True, "score": -1.0, "issues": [],
                        "verdict": "无法解析审查结果，保守放行", "skipped": True}
            text = text[start : end + 1]
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"match": True, "score": -1.0, "issues": [],
                    "verdict": "审查 JSON 解析失败，保守放行", "skipped": True}
        # 规范化
        return {
            "match": bool(data.get("match", True)),
            "score": float(data.get("score", 0.0) or 0.0),
            "issues": data.get("issues") or [],
            "suggested_fix": data.get("suggested_fix") or {},
            "verdict": data.get("verdict", ""),
            "skipped": False,
        }

    # ------------------------------------------------------------------
    def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """调用 Agnes chat completions，含 429/5xx 自动重试。

        Agnes 免费额度容易触发 429 限流，且多模态消息可能返回 500
        (get_channel_failed)。此处对 429/500/502/503 做指数退避重试，
        500 且带 get_channel 标记时额外多等一会。
        """
        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }

        max_attempts = 8
        for attempt in range(1, max_attempts + 1):
            try:
                resp = requests.post(
                    url, headers=self._headers(), json=payload,
                    timeout=self.config.api_timeout,
                )
            except requests.RequestException as e:
                if attempt == max_attempts:
                    raise RuntimeError(f"Agnes API 网络异常: {e}") from e
                self._backoff(attempt, "network")
                continue

            code = resp.status_code
            if code == 200:
                data = resp.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError) as e:
                    raise RuntimeError(
                        f"Agnes 返回结构异常: {json.dumps(data, ensure_ascii=False)[:500]}"
                    ) from e
                return self._extract_spec(content)

            # 解析错误体
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                detail = resp.text[:300]

            retryable = code in (429, 500, 502, 503, 504)
            if not retryable or attempt == max_attempts:
                raise RuntimeError(f"Agnes API HTTP {code}: {detail or resp.text[:300]}")

            # 429 限流 / 500 模型暂不可用 → 指数退避（上限 60s）
            base_wait = 6 if code == 429 else 10
            wait = min(base_wait * (2 ** (attempt - 1)), 60)
            print(f"[agnes] HTTP {code} (尝试 {attempt}/{max_attempts}): {detail}")
            print(f"[agnes] {wait}s 后重试…")
            import time
            time.sleep(wait)

        raise RuntimeError("Agnes API 重试耗尽")

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        import time
        wait = min(5 * (2 ** (attempt - 1)), 30)
        print(f"[agnes] {reason} (尝试 {attempt})，{wait}s 后重试…")
        time.sleep(wait)

    @staticmethod
    def _extract_spec(content: str) -> dict[str, Any]:
        """从 LLM 回复中提取 JSON 对象，容忍 markdown 代码块与前后杂文。"""
        text = content.strip()
        m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1:
                raise SpecParseError(f"LLM 未返回 JSON 对象: {content[:300]!r}")
            text = text[start : end + 1]
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as e:
            raise SpecParseError(f"JSON 解析失败: {e}\n原文: {text[:500]}") from e
        if not isinstance(spec, dict) or "parts" not in spec:
            raise SpecParseError(f"JSON 不符合规格结构(缺少 parts): {text[:300]}")
        return spec
