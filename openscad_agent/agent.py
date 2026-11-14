"""Agent 编排器：串联 图片→识别→规格→OpenSCAD 源码→渲染/校验→审查闭环 全流程。

流程：
  1. 预处理输入图片（缩放/转 PNG）
  2. 调 Agnes 视觉 API 得到模型规格 JSON（或纯文本描述）
  3. 规格转译为 OpenSCAD 源码
  4. OpenSCAD 语法校验 + 编译失败自动修正（最多 max_fix_rounds 轮）
  5. 渲染导出 3D 模型（stl/obj/3mf）+ 2D 预览图
  6. 渲染图审查闭环：预览图 vs 原图 → LLM 比对，不符合则自动修正
     规格并重渲染（最多 max_review_rounds 轮，含回归保护与得分回退）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .core import AgentConfig, OpenscadRenderer, preprocess_image
from .geometry import spec_to_scad, write_spec_and_scad
from .vision import AgnesVision, SpecParseError


def _build_fix_messages(spec: dict, compile_err: str, hint: str, image_name: str) -> tuple[str, str]:
    sys = (
        "你是 OpenSCAD 专家。用户给出了一份模型规格 JSON，但由它生成的 OpenSCAD "
        f"源码编译失败。请修正规格 JSON，使其能顺利编译。只输出修正后的完整 JSON 对象。\n"
        f"原始规格:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\n"
        f"编译错误:\n{compile_err}\n"
    )
    user = (
        f"请输出修正后的完整规格 JSON（图片: {image_name}"
        + (f"，用户说明: {hint}" if hint else "")
        + "）"
    )
    return sys, user


@dataclass
class AgentResult:
    success: bool
    spec: Optional[dict[str, Any]] = None
    spec_path: Optional[str] = None
    scad_path: Optional[str] = None
    model_file: Optional[str] = None
    preview_png: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    fix_rounds: int = 0
    review_rounds: int = 0
    #: 最后一次审查结论（含 match/score/issues/verdict）
    review: Optional[dict[str, Any]] = None


class OpenscadAgent:
    """图片 → OpenSCAD 3D 模型 全流程 Agent。"""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.vision = AgnesVision(config)
        self.renderer = OpenscadRenderer(config)
        self.max_fix_rounds = 3      # 编译错误自动修正上限
        self.max_review_rounds = 3   # 渲染图审查自动修正上限
        self.review_views = OpenscadRenderer.VIEWS  # 审查时渲染的视角
        self.enable_multi_view = True              # 是否开启多视角审查
        self.last_review: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------
    def build_from_image(self, image: str | Path, hint: str = "", review: bool = True) -> AgentResult:
        """主入口：图片识别建模。review=True 时开启渲染图审查闭环。"""
        image = Path(image).expanduser()
        if not image.exists():
            return AgentResult(success=False, errors=[f"图片不存在: {image}"])

        # 1. 预处理
        try:
            prep_dir = Path(self.config.output_dir) / "_prep"
            prep_dir.mkdir(parents=True, exist_ok=True)
            prep_path = prep_dir / f"{image.stem}_prep.png"
            preprocess_image(image, prep_path, max_side=self.config.image_max_side)
        except Exception as e:  # noqa: BLE001
            return AgentResult(success=False, errors=[f"图片预处理失败: {e}"])

        # 2. 识别
        try:
            spec = self.vision.recognize(prep_path, user_hint=hint)
        except (SpecParseError, RuntimeError) as e:
            return AgentResult(success=False, errors=[f"识别/解析失败: {e}"])

        return self._finalize(
            spec, tag=image.stem, render=True, hint=hint, image_name=image.name,
            review=review, original_image=prep_path,
        )

    def build_from_description(self, description: str) -> AgentResult:
        """纯文字描述建模（无图片场景）。无原图可比对，不启用审查闭环。"""
        try:
            spec = self.vision.describe_from_text(description)
        except (SpecParseError, RuntimeError) as e:
            return AgentResult(success=False, errors=[f"识别/解析失败: {e}"])
        return self._finalize(spec, tag="text_model", render=True, hint="", image_name="", review=False, original_image=None)

    # ------------------------------------------------------------------
    def _finalize(
        self,
        spec: dict[str, Any],
        tag: str,
        render: bool,
        hint: str,
        image_name: str,
        review: bool = False,
        original_image: Optional[Path] = None,
    ) -> AgentResult:
        """规格 → 源码 → 校验 → 渲染 → (审查闭环)。

        审查闭环：若 review=True 且有 original_image，则渲染出预览图后
        回传给 LLM 与原图比对，不符合预期则自动修正规格并重新渲染，
        直到符合或达 max_review_rounds。
        """
        errors: list[str] = []
        rounds = 0

        # 3. 生成源码 + 校验
        spec_path_str, scad_path_str, safe = write_spec_and_scad(spec, self.config, tag=tag)
        scad_path = Path(scad_path_str)
        ok, msg = self.renderer.validate(scad_path)
        if not ok:
            # 4. 带错误信息重试（最多 3 轮）
            for i in range(self.max_fix_rounds):
                rounds += 1
                print(f"[agent] 编译失败 (round {i + 1}/{self.max_fix_rounds})，请求 LLM 修正…")
                fixed_spec = self._ask_fix(spec, msg, hint, image_name)
                if fixed_spec is None:
                    errors.append(f"修正请求失败 (round {i + 1}): LLM 限流或不可用")
                    break
                if fixed_spec is spec:
                    errors.append(f"LLM 无法修正 (round {i + 1})，保留当前 spec")
                    break
                spec = fixed_spec
                spec_path_str, scad_path_str, safe = write_spec_and_scad(spec, self.config, tag=tag)
                scad_path = Path(scad_path_str)
                ok, msg = self.renderer.validate(scad_path)
                if ok:
                    break
            if not ok:
                ok2, msg2 = self.renderer.validate(scad_path)
                if ok2:
                    print(f"[agent] spec 本身可编译，跳过修复直接渲染")
                    ok = True
                    msg = msg2
                else:
                    errors.append(f"OpenSCAD 编译校验始终失败:\n{msg}")
                    return AgentResult(
                        success=False, spec=spec, spec_path=spec_path_str,
                        scad_path=str(scad_path), errors=errors, fix_rounds=rounds,
                    )

        # 5. 渲染导出
        model_file: Optional[str] = None
        preview_png: Optional[str] = None
        if render:
            out_dir = Path(self.config.output_dir)
            model_file = str(out_dir / f"{safe}.{self.config.export_format}")
            preview_png = str(out_dir / f"{safe}_preview.png")
            try:
                self.renderer.export_model(scad_path, Path(model_file))
                self.renderer.render_preview(scad_path, Path(preview_png))
            except RuntimeError as e:
                errors.append(f"渲染失败: {e}")
                return AgentResult(
                    success=False, spec=spec, spec_path=spec_path_str,
                    scad_path=str(scad_path), model_file=model_file,
                    preview_png=preview_png, errors=errors, fix_rounds=rounds,
                )

        # 6. 审查闭环：渲染图 vs 原图，不符合则自动修正重渲染
        review_rounds = 0
        if review and original_image is not None and preview_png and Path(preview_png).exists():
            self.last_review = None
            review_rounds = self._review_loop(spec, scad_path, safe, original_image,
                                               preview_png, hint, model_file)
            # spec 可能已在闭环中被修改，重新落盘并渲染最终结果
            spec_path_str, scad_path_str, safe = write_spec_and_scad(spec, self.config, tag=tag)
            scad_path = Path(scad_path_str)
            out_dir = Path(self.config.output_dir)
            model_file = str(out_dir / f"{safe}.{self.config.export_format}")
            preview_png = str(out_dir / f"{safe}_preview.png")
            try:
                self.renderer.export_model(scad_path, Path(model_file))
                self.renderer.render_preview(scad_path, Path(preview_png))
            except RuntimeError as e:
                errors.append(f"审查后重新渲染失败: {e}")

        print(f"[agent] 完成: {safe} (fix_rounds={rounds}, review_rounds={review_rounds}, errors={len(errors)})")
        return AgentResult(
            success=True, spec=spec, spec_path=spec_path_str,
            scad_path=str(scad_path), model_file=model_file,
            preview_png=preview_png, errors=errors, fix_rounds=rounds,
            review_rounds=review_rounds, review=self.last_review,
        )

    # ------------------------------------------------------------------
    def _review_loop(
        self,
        spec: dict[str, Any],
        scad_path: Path,
        safe: str,
        original_image: Path,
        preview_png: str,
        hint: str,
        model_file: str,
    ) -> int:
        """审查闭环：预览图 vs 原图比对，不符合则修正规格并重渲染。

        返回审查轮数。spec 就地修改（通过 self._apply_review_fix 写回调用方传入的 dict）。
        """
        out_dir = Path(self.config.output_dir)
        max_review = getattr(self, "max_review_rounds", 3)
        # 多视角渲染：除主预览外额外渲 top/front/iso，审查时多角度回传 LLM
        views = getattr(self, "review_views", OpenscadRenderer.VIEWS)
        best_spec = spec          # 记录迄今最好的一版（部件数最多且不编译失败）
        best_score = -1.0

        def _render_views(scad: Path):
            """渲染主预览 + 多视角，导出 3D 模型，返回 (主预览png, [额外视角pngs])。"""
            safe2 = scad.stem.replace("_scad", "")  # 去后缀安全名
            self.renderer.export_model(scad, Path(out_dir / f"{safe2}.{self.config.export_format}"))
            main_png = out_dir / f"{safe2}_preview.png"
            self.renderer.render_preview(scad, main_png)
            extra = self.renderer.render_multi_view(
                scad, main_png.with_name(f"{safe2}_v"), views=views, size=(500, 400)
            )
            return main_png, [p for p in extra if p != main_png]

        # 首轮渲染（若 _finalize 已渲染过 preview_png，直接复用其多视角）
        main_png = Path(preview_png)
        extra_views: list = []
        if self.enable_multi_view:
            extra_views = self.renderer.render_multi_view(
                scad_path, main_png.with_name(main_png.stem + "_v"), views=views, size=(500, 400)
            )
            extra_views = [p for p in extra_views if p != main_png]

        for i in range(1, max_review + 1):
            print(f"[review] 审查渲染图 (round {i}/{max_review}, {len(extra_views) + 1} 视角) …")
            verdict = self.vision.review_preview(
                original_image, main_png, spec, user_hint=hint, extra_views=extra_views or None
            )
            if verdict.get("skipped"):
                print(f"[review] {verdict.get('verdict')}")
                break
            cur_score = float(verdict.get("score", 0.0) or 0.0)
            print(f"[review] match={verdict.get('match')} score={cur_score} | {verdict.get('verdict')}")
            if verdict.get("match"):
                # 符合预期，成功收敛
                best_score = cur_score
                self.last_review = verdict
                return i

            # 记录迄今最好的一版（用于回退）
            if cur_score > best_score:
                best_score = cur_score
                best_spec = dict(spec)

            # 不符合 → 应用 LLM 建议的 spec_patch 并重新生成+渲染
            patch = (verdict.get("suggested_fix") or {}).get("spec_patch")
            if not patch:
                print(f"[review] LLM 判为不符合但未给出 spec_patch，停止: {verdict.get('issues')}")
                self.last_review = verdict
                break
            try:
                new_spec = self._merge_spec_patch(spec, patch)
            except Exception as e:  # noqa: BLE001
                print(f"[review] 应用 spec_patch 失败: {e}，停止")
                self.last_review = verdict
                break

            # 回归保护：若 LLM 修正规格使部件数减少（简化而非补全）→ 拒绝该修正
            old_cnt = len(spec.get("parts", []))
            new_cnt = len(new_spec.get("parts", []))
            if new_cnt < old_cnt:
                print(f"[review] LLM 修正减少部件数 ({old_cnt}->{new_cnt})，视为回归，拒绝并停止")
                spec.clear()
                spec.update(best_spec)
                self.last_review = verdict
                break

            # 写回 spec（就地更新调用方的 dict）
            spec.clear()
            spec.update(new_spec)
            spec_path_str, scad_path_str, safe2 = write_spec_and_scad(spec, self.config, tag=safe)
            new_scad = Path(scad_path_str)
            ok, cerr = self.renderer.validate(new_scad)
            if not ok:
                print(f"[review] 修正后 spec 编译失败: {cerr[:300]}，停止")
                self.last_review = verdict
                break
            # 重渲染主预览 + 多视角
            try:
                new_main, new_extra = _render_views(new_scad)
                main_png = new_main
                extra_views = new_extra if self.enable_multi_view else []
            except RuntimeError as e:
                print(f"[review] 重渲染失败: {e}，停止")
                self.last_review = verdict
                break
            print(f"[review] 已修正规格并重渲染 (round {i})")

        # 若循环结束仍 match=false（未收敛），回退到迄今得分最高的 spec，
        # 避免把模型越改越糟（LLM 可能简化而非补全）。
        if best_spec is not spec and best_score > -1.0:
            print(f"[review] 未收敛，回退到得分最高的 spec (score={best_score})")
            spec.clear()
            spec.update(best_spec)

        self.last_review = getattr(self, "last_review", None)
        return min(max_review, 99)

    @staticmethod
    def _merge_spec_patch(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        """把 LLM 给出的 spec_patch 合并到 base spec。

        约定：patch 里出现的顶层字段（如 parts/assembly）整体替换 base 对应字段；
        未出现的字段保持 base 原值。这是最稳健的合并策略——LLM 通常返回完整修正后的 parts。
        """
        merged = dict(base)
        for k, v in patch.items():
            merged[k] = v
        # 保底：确保必需字段存在
        if "parts" not in merged or not isinstance(merged.get("parts"), list):
            merged["parts"] = base.get("parts", [])
        if not merged.get("name"):
            merged["name"] = base.get("name", "model")
        return merged

    # ------------------------------------------------------------------
    def _ask_fix(
        self, spec: dict[str, Any], compile_err: str, hint: str, image_name: str
    ) -> Optional[dict[str, Any]]:
        """把 OpenSCAD 编译错误回传给 LLM，请求修正后的规格。

        若 LLM 因限流/不可用无法修正，返回原 spec（避免把已知正确的 spec
        覆盖为 LLM 重新解析的无效数据）。
        """
        sys, user = _build_fix_messages(spec, compile_err, hint, image_name)
        try:
            vision = AgnesVision(self.config)
            result = vision._chat([
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ])
            return result
        except Exception as e:  # noqa: BLE001
            print(f"[agent] 修正请求异常: {e}，保留原 spec")
            return spec
