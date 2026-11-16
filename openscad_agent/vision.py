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
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .core import AgentConfig, SPEC_SCHEMA
from .log import get_logger as _log_module
_log = _log_module()

FEW_SHOT_EXAMPLES = {
    "mechanical": """\
{
  "name": "flange_mount",
  "description": "带均布沉孔与中心凸台的圆形法兰座",
  "units": "mm",
  "params": {"flange_dia": 60, "boss_dia": 30, "hole_dia": 10},
  "parts": [
    {
      "id": "base_flange",
      "primitive": "cylinder",
      "size": [60, 6],
      "center": [0, 0, 0],
      "color": "dimgray",
      "holes": [
        {"primitive": "cylinder", "size": [10, 6], "center": [0, 0, 0]},
        {"primitive": "cylinder", "size": [4, 6], "radial_array": {"count": 4, "radius": 22}}
      ]
    },
    {
      "id": "center_boss",
      "primitive": "cylinder",
      "size": [30, 20],
      "center": [0, 0, 6],
      "color": "silver",
      "holes": [{"primitive": "cylinder", "size": [10, 20], "center": [0, 0, 6]}]
    }
  ],
  "assembly": {"union_parts": ["base_flange", "center_boss"]}
}""",
    "revolve": """\
{
  "name": "minimal_vase",
  "description": "极简流线型回转体花瓶",
  "units": "mm",
  "parts": [
    {
      "id": "vase_body",
      "primitive": "rotate_extrude",
      "angle": 360,
      "polygon": [[12, 0], [20, 0], [25, 20], [15, 35], [20, 50], [16, 50], [12, 25]],
      "center": [0, 0, 0],
      "color": "mediumpurple"
    }
  ],
  "assembly": {"union_parts": ["vase_body"]}
}""",
    "architecture": """\
{
  "name": "hexagonal_pavilion",
  "description": "东方古典六角亭",
  "units": "mm",
  "parts": [
    {
      "id": "base_platform",
      "primitive": "cylinder",
      "r1": 35,
      "r2": 35,
      "height": 5,
      "center": [0, 0, 0],
      "color": "dimgray"
    },
    {
      "id": "pillar_ring",
      "primitive": "cylinder",
      "radius": 2,
      "height": 30,
      "center": [0, 0, 5],
      "color": "darkred",
      "radial_array": {"count": 6, "radius": 26}
    },
    {
      "id": "spire_roof",
      "primitive": "cylinder",
      "r1": 38,
      "r2": 0,
      "height": 22,
      "center": [0, 0, 35],
      "color": "gold"
    }
  ],
  "assembly": {"union_parts": ["base_platform", "pillar_ring", "spire_roof"]}
}""",
    "electronics": """\
{
  "name": "enclosure_case",
  "description": "带接口开口的电子设备下壳",
  "units": "mm",
  "parts": [
    {
      "id": "bottom_case",
      "primitive": "cube",
      "size": [80, 50, 20],
      "center": [0, 0, 0],
      "color": "steelblue",
      "holes": [
        {"primitive": "cube", "size": [76, 46, 18], "center": [0, 0, 2]},
        {"primitive": "cube", "size": [12, 10, 6], "center": [40, 0, 6]}
      ]
    }
  ],
  "assembly": {"union_parts": ["bottom_case"]}
}""",
    "organic": """\
{
  "name": "robotic_gripper",
  "description": "带双侧对称夹爪的连杆支架",
  "units": "mm",
  "parts": [
    {
      "id": "central_link",
      "primitive": "capsule",
      "radius": 4,
      "height": 25,
      "center": [0, 0, 0],
      "color": "darkorange"
    },
    {
      "id": "symmetric_claw",
      "primitive": "cube",
      "size": [18, 5, 4],
      "center": [12, 0, 15],
      "symmetric": true,
      "color": "silver"
    }
  ],
  "assembly": {"union_parts": ["central_link", "symmetric_claw"]}
}"""
}


def select_few_shot_domain(text: str) -> str:
    """根据输入文本自动匹配最贴合的 Few-Shot 逆向领域范例。"""
    t = text.lower()
    if any(k in t for k in ("花瓶", "瓶", "杯", "罐", "滑轮", "圆环", "碗", "vase", "bottle", "cup", "pulley", "revolve", "lathe")):
        return "revolve"
    if any(k in t for k in ("建筑", "亭", "楼", "塔", "城", "屋", "房", "castle", "tower", "building", "house", "pavilion", "city")):
        return "architecture"
    if any(k in t for k in ("外壳", "盒", "壳", "底座", "固定座", "支架", "case", "enclosure", "housing", "stand", "bracket")):
        return "electronics"
    if any(k in t for k in ("翅膀", "手臂", "对称", "生物", "连杆", "wing", "arm", "symmetry", "capsule", "gripper")):
        return "organic"
    return "mechanical"


def build_vision_prompt(context_text: str = "") -> str:
    """动态拼装包含领域特定 Few-Shot 范例的系统提示词。"""
    domain = select_few_shot_domain(context_text)
    example = FEW_SHOT_EXAMPLES.get(domain, FEW_SHOT_EXAMPLES["mechanical"])
    return f"""\
你是一位资深 3D CAD 工程师 + 逆向工程专家。用户会给你产品/零件/物体的参考照片或需求，
请仔细分析其几何结构，并输出一个可直接驱动 OpenSCAD 建模的 JSON 规格。

要求：
1. 严格输出且只输出一个 JSON 对象（不要 markdown 代码块、不要解释文字），
   结构必须完全符合以下 schema：
{SPEC_SCHEMA}

2. 几何判断原则：
   - 优先用最简单的 CSG 原语与高级特征表达：方块/圆柱/圆锥/球/胶囊/多面体/2D拉伸/2D回转 + 阵列(array)/镜像(symmetric)/union/difference。
   - 尺寸以毫米(mm)为单位，给出合理工程估计（例如手机壳约 150x75x8mm）。
   - 孔、凹槽、切角等"减去"的特征放入对应 part 的 holes。
   - 装配关系写入 assembly 字段。
3. 对于无法用简单原语精确表达的曲面特征，用 polyhedron/linear_extrude 近似，并在 part.note 中说明近似方式。
4. 若图片模糊或无法判断，请给出最合理的假设并全部记录在 description 与 print_tips 中。
5. 参考逆向分解范例（针对当前领域 [{domain}]，示范接地对齐、阵列与语义着色）：
{example}
"""

VISION_PROMPT = build_vision_prompt()


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
        """无图片场景：仅凭文字描述建模（自适应注入领域 Few-Shot 范例）。"""
        system = build_vision_prompt(description) + self.config.extra_system
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"请根据以下文字描述生成 OpenSCAD 模型规格 JSON：\n{description}",
            },
        ]
        return self._chat(messages)

    def recognize(
        self,
        image_path: Path | list[Path] | str | list[str],
        user_hint: str = "",
        edge_paths: Optional[list[Path]] = None,
    ) -> dict[str, Any]:
        """图片识别：支持单张或多张多角度图片联合建模，并可选并联输入物理边缘轮廓图。

        若提供多张不同视角照片或边缘轮廓图，多模态模型联合比对各视角与轮廓边界，
        消除单视角深度与光影反光干扰。
        """
        paths = [Path(p) for p in (image_path if isinstance(image_path, (list, tuple)) else [image_path])]
        context_text = f"{user_hint} {' '.join(p.stem for p in paths)}"
        system = build_vision_prompt(context_text) + self.config.extra_system

        multimodal: list[dict[str, Any]] = [
            {"type": "text", "text": "请仔细分析以下提供的图片（多角度参考），综合交叉比对输出 OpenSCAD 模型规格 JSON。"},
        ]
        for idx, p in enumerate(paths):
            data_url = self._image_data_url(p)
            label = f"[参考视角 {idx + 1}: {p.name}]" if len(paths) > 1 else "参考照片"
            multimodal.append({"type": "text", "text": label})
            multimodal.append({"type": "image_url", "image_url": {"url": data_url}})

        if edge_paths:
            multimodal.append({"type": "text", "text": "以下是提取的物理几何边缘轮廓图（无光影干扰，指示边界线条与孔位）:"})
            for idx, ep in enumerate(edge_paths):
                if ep.exists():
                    data_url_edge = self._image_data_url(ep)
                    multimodal.append({"type": "text", "text": f"[边缘轮廓 {idx + 1}: {ep.name}]"})
                    multimodal.append({"type": "image_url", "image_url": {"url": data_url_edge}})

        if user_hint:
            multimodal.insert(0, {"type": "text", "text": f"用户补充说明: {user_hint}"})

        try:
            return self._chat([
                {"role": "system", "content": system},
                {"role": "user", "content": multimodal},
            ])
        except RuntimeError as e:
            msg = str(e).lower()
            if "http 400" in msg or "http 422" in msg or "http 401" in msg:
                names = ", ".join(p.name for p in paths)
                _log.warning("[agnes] 多模态图片消息被拒绝(%s)，降级为纯文本请求", e)
                fallback = (
                    f"参考图片: {names}。"
                    + (f" 用户补充说明: {user_hint}" if user_hint else "")
                    + " 请基于图片文件名与补充说明给出最合理的 OpenSCAD 模型规格 JSON。"
                )
                return self._chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": fallback},
                ])
            raise

    # ------------------------------------------------------------------
    def _post_chat(self, messages: list[dict[str, Any]]) -> str:
        """公共 POST chat/completions，含 429/5xx 指数退避重试，返回原始文本。

        被 _chat（需解析 spec）与 _chat_raw（通用文本）共享，消除重复重试逻辑。
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
            _log.warning("[agnes] HTTP %s (尝试 %d/%d): %s", code, attempt, max_attempts, detail)
            _log.info("[agnes] %ss 后重试…", wait)
            time.sleep(wait)

        raise RuntimeError("Agnes API 重试耗尽")

    def _chat_raw(self, messages: list[dict[str, Any]]) -> str:
        """通用对话：返回 LLM 回复的原始文本，不做 JSON 规格解析。

        用于预览图审查、自由问答等不需要提取 spec 的场景。
        复用 _post_chat 的重试逻辑。
        """
        return self._post_chat(messages)

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
        conversation_history: Optional[list[dict[str, Any]]] = None,
        round_index: int = 1,
    ) -> dict[str, Any]:
        """审查渲染图：支持多轮会话上下文记忆（Conversational Review Chain）。

        在多轮审查中，多模态 LLM 拥有前序轮次的评判建议与生成效果上下文，
        具备连续性认知，避免互相矛盾或盲目简化的退化现象。
        """
        prev_url = self._image_data_url(preview_image)
        preview_imgs = [prev_url]
        view_labels = ["主视角"]
        if extra_views:
            for i, v in enumerate(extra_views):
                try:
                    preview_imgs.append(self._image_data_url(v))
                    view_labels.append(f"视角{i + 1}")
                except Exception:  # noqa: BLE001
                    continue

        if round_index == 1 or not conversation_history:
            # 首轮：提供原图 + 主视角及多视角渲染图 + 当前规格
            orig_url = self._image_data_url(original_image)
            content: list[dict[str, Any]] = [
                {"type": "text", "text": f"当前初版规格:\n{json.dumps(spec, ensure_ascii=False)[:3000]}"},
                {"type": "image_url", "image_url": {"url": orig_url}},
                {"type": "text", "text": "以下是当前 OpenSCAD 渲染生成的初版预览图（多角度）:"},
            ]
            for label, url in zip(view_labels, preview_imgs):
                content.append({"type": "text", "text": f"[{label}]"})
                content.append({"type": "image_url", "image_url": {"url": url}})
            if user_hint:
                content.insert(0, {"type": "text", "text": f"用户补充说明: {user_hint}"})
        else:
            # 后续轮次：带入上一轮的修正上下文，附带最新渲染图与最新规格
            content = [
                {"type": "text", "text": f"已根据上一轮（第 {round_index - 1} 轮）你的修改建议重新生成规格并完成 3D 渲染。\n这是修改后的第 {round_index} 轮最新渲染图与最新规格:\n{json.dumps(spec, ensure_ascii=False)[:3000]}"},
                {"type": "text", "text": "请继续严格对比原图，评估上一轮修正是否解决了问题，并给出本轮判定与进一步修正建议:"},
            ]
            for label, url in zip(view_labels, preview_imgs):
                content.append({"type": "text", "text": f"[{label}]"})
                content.append({"type": "image_url", "image_url": {"url": url}})

        try:
            if conversation_history is not None:
                conversation_history.append({"role": "user", "content": content})
                # 修剪策略 (OpenAI 多模态消息 user/assistant 严格交替约束):
                # 保留首轮 user+assistant (索引 0,1), 其余保留最新的 N 条且为偶数,
                # 从索引 2 的 user 边界对齐, 确保拼接后不会出现连续 user 或连续 assistant。
                if len(conversation_history) > 6:
                    recent = len(conversation_history) - 2
                    recent = recent - (recent % 2)  # 对齐到偶数边界
                    recent = min(recent, 4)          # 最多保留 4 条
                    conversation_history[:] = conversation_history[:2] + conversation_history[2 + (4 - recent):]
                messages = [{"role": "system", "content": self.REVIEW_PROMPT}] + list(conversation_history)
            else:
                messages = [
                    {"role": "system", "content": self.REVIEW_PROMPT},
                    {"role": "user", "content": content},
                ]
            raw = self._post_chat(messages)
            if conversation_history is not None:
                conversation_history.append({"role": "assistant", "content": raw})
            return self._extract_review(raw)
        except Exception as e:  # noqa: BLE001
            # 审查环节失败（限流/网络/解析）→ 保守放行，避免阻断建模
            _log.warning("[review] 审查请求失败，保守放行: %s", e)
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
        """调用 Agnes chat completions 并解析出 JSON 规格。

        重试逻辑统一在 _post_chat（429/5xx 指数退避）；此处仅负责把
        原始文本解析为 spec dict。
        """
        content = self._post_chat(messages)
        return self._extract_spec(content)

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        wait = min(5 * (2 ** (attempt - 1)), 30)
        _log.warning("[agnes] %s (尝试 %d)，%ss 后重试…", reason, attempt, wait)
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
