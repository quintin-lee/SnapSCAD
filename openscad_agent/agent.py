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
from .log import get_logger as _log_module
_log = _log_module()


def _analyze_compile_error(compile_err: str) -> str:
    """分析 OpenSCAD 编译器报错日志，生成针对性修正建议。"""
    err_low = compile_err.lower()
    hints: list[str] = []
    if "polyhedron" in err_low:
        hints.append("💡 多面体 (polyhedron) 报错：请仔细检查 faces 数组顶点索引是否从 0 开始且闭合，或将该复杂部件直接简化为 cube/cylinder/capsule 降级实现。")
    if "difference" in err_low or "hole" in err_low:
        hints.append("💡 挖除差集 (difference/holes) 报错：请确保 holes 内部的 primitive 和尺寸合法，且与父部件空间相交。")
    if "syntax error" in err_low or "parser error" in err_low:
        hints.append("💡 语法解析报错：请严格检查各部件尺寸与坐标数值是否为纯数字，确保属性名符合 SPEC_SCHEMA 规范。")
    if not hints:
        hints.append("💡 编译未通过：请简化可能有拓扑冲突的复杂部件，优先采用标准 CSG 基础原语保证可编译性。")
    return "\n".join(hints)


def _build_fix_messages(spec: dict, compile_err: str, hint: str, image_name: str) -> tuple[str, str]:
    diag_hint = _analyze_compile_error(compile_err)
    sys = (
        "你是 OpenSCAD 专家。用户给出了一份模型规格 JSON，但由它生成的 OpenSCAD "
        f"源码编译失败。请修正规格 JSON，使其能顺利编译。只输出修正后的完整 JSON 对象。\n"
        f"原始规格:\n{json.dumps(spec, ensure_ascii=False, indent=2)}\n\n"
        f"编译器报错信息:\n{compile_err}\n\n"
        f"诊断与修复建议:\n{diag_hint}\n"
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
    #: 审查各轮得分演进走势 (如 [0.3, 0.7, 0.95])
    score_trajectory: list[float] = field(default_factory=list)
    #: 3D 打印切片自检健康报告 (Printability Doctor)
    doctor_report: Optional[Any] = None
    #: 自动生成的 3D 打印切片工艺指导书路径 (*_print_guide.md)
    print_guide: Optional[str] = None
    #: 生成的独立 HTML 3D 网页交互查看器路径
    viewer_html: Optional[str] = None
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
        self.review_views = ("top", "front", "side", "iso")  # 审查视角；perspective 与主预览重复，剔除
        self.enable_multi_view = True              # 是否开启多视角审查
        self.export_3mf: Optional[bool] = None     # GUI 显式 3MF 导出开关 (None=使用 config 默认)
        self.export_2d: Optional[bool] = None      # GUI 显式 2D 投影开关 (None=使用 config 默认)
        self.last_review: Optional[dict[str, Any]] = None
        self.score_trajectory: list[float] = []   # 审查闭环各轮得分走势
        # 将 GUI 显式开关注入渲染器 (None 时渲染器自动回退 config 值)
        self.renderer.set_agent_overrides(export_3mf=self.export_3mf, export_2d=self.export_2d)

    def _sync_renderer_overrides(self) -> None:
        """将当前 agent 层 export_3mf/export_2d 开关同步到渲染器覆盖层。

        GUI 在 on_build 中一次性写入 agent.export_3mf/export_2d 后,
        调用此方法使渲染器感知最新开关状态, 避免竞态。
        """
        self.renderer.set_agent_overrides(export_3mf=self.export_3mf, export_2d=self.export_2d)

    # ------------------------------------------------------------------
    def build_from_image(
        self,
        image: str | Path | list[str | Path],
        hint: str = "",
        review: bool = True,
    ) -> AgentResult:
        """主入口：图片识别建模。支持单张图片或多张多角度图片联合建模。

        若传入多张图片列表，Agnes 视觉模型将交叉比对各视角，消除深度歧义。
        """
        raw_images = image if isinstance(image, (list, tuple)) else [image]
        images = [Path(p).expanduser() for p in raw_images]
        missing = [p for p in images if not p.exists()]
        if missing:
            return AgentResult(success=False, errors=[f"图片不存在: {', '.join(str(m) for m in missing)}"])

        # 1. 预处理所有输入图片 (支持并联生成 Canny 几何边缘先验图)
        prep_dir = Path(self.config.output_dir) / "_prep"
        prep_dir.mkdir(parents=True, exist_ok=True)
        prep_paths: list[Path] = []
        edge_paths: list[Path] = []
        try:
            from .core import extract_edge_map
            for idx, img in enumerate(images):
                target = prep_dir / f"{img.stem}_prep_{idx}.png"
                preprocess_image(img, target, max_side=self.config.image_max_side)
                prep_paths.append(target)
                if getattr(self.config, "enable_edge_map", True):
                    edge_target = prep_dir / f"{img.stem}_edge_{idx}.png"
                    extract_edge_map(target, edge_target)
                    edge_paths.append(edge_target)
        except Exception as e:  # noqa: BLE001
            for p in prep_paths + edge_paths:
                _rm_quiet(p)
            _rm_empty_dir(prep_dir)
            return AgentResult(success=False, errors=[f"图片预处理失败: {e}"])

        # 2. 识别（多图联合输入 + Canny 几何轮廓并联）
        try:
            spec = self.vision.recognize(prep_paths, user_hint=hint, edge_paths=edge_paths or None)
        except (SpecParseError, RuntimeError) as e:
            for p in prep_paths + edge_paths:
                _rm_quiet(p)
            _rm_empty_dir(prep_dir)
            return AgentResult(success=False, errors=[f"识别/解析失败: {e}"])

        primary_prep = prep_paths[0]
        tag = images[0].stem
        image_label = ", ".join(i.name for i in images)
        result = self._finalize(
            spec, tag=tag, render=True, hint=hint, image_name=image_label,
            review=review, original_image=primary_prep,
        )

        # 识别与建模完成后删除临时预处理图 (避免 out/_prep 反复累积)
        for p in prep_paths + edge_paths:
            _rm_quiet(p)
        _rm_empty_dir(prep_dir)
        return result

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
                _log.warning("[agent] 编译失败 (round %d/%d)，阶梯温控请求 LLM 修正…", i + 1, self.max_fix_rounds)
                fixed_spec = self._ask_fix(spec, msg, hint, image_name, round_index=i + 1)
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
                # 循环退出时 ok/msg 已是最后一次 validate 的结果，无需重复编译
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
                # 同步 GUI 层显式导出开关到渲染器覆盖层, 使 export_model 内部
                # 的 3MF/2D 同步导出路径感知 GUI 开关状态
                self._sync_renderer_overrides()
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
        spec_before_review = json.dumps(spec, sort_keys=True)  # 快照, 判断 spec 是否被审查修改
        scad_rewritten = False  # 记录审查闭环内是否已重新落盘+渲染 (避免 _finalize 重复渲染)
        if review and original_image is not None and preview_png and Path(preview_png).exists():
            self.last_review = None
            review_rounds, scad_rewritten = self._review_loop(
                spec, scad_path, safe, original_image, preview_png, hint, model_file
            )
            # 仅当审查闭环实际修改了 spec 且尚未在循环内重新落盘渲染时, 才重新写盘+渲染
            spec_changed = json.dumps(spec, sort_keys=True) != spec_before_review
            if spec_changed and not scad_rewritten:
                spec_path_str, scad_path_str, safe = write_spec_and_scad(spec, self.config, tag=tag)
                scad_path = Path(scad_path_str)
                out_dir = Path(self.config.output_dir)
                model_file = str(out_dir / f"{safe}.{self.config.export_format}")
                preview_png = str(out_dir / f"{safe}_preview.png")
                try:
                    self._sync_renderer_overrides()
                    self.renderer.export_model(scad_path, Path(model_file))
                    self.renderer.render_preview(scad_path, Path(preview_png))
                except RuntimeError as e:
                    errors.append(f"审查后重新渲染失败: {e}")
            elif spec_changed and scad_rewritten:
                _log.info("[agent] 审查闭环已回退至最优 spec 并在循环内完成重新落盘渲染, 跳过重复渲染")

        viewer_html: Optional[str] = None
        if model_file and Path(model_file).suffix.lower() == ".stl":
            v_cand = Path(model_file).with_name(f"{Path(model_file).stem}_viewer.html")
            if v_cand.exists():
                viewer_html = str(v_cand)

        from .doctor import check_printability, generate_print_guide
        doc_report = check_printability(spec, material=getattr(self.config, "material", "PLA"))
        _log.info(
            "[doctor] %s | 尺寸: %s mm | 估算体积: %.1f cm³ (约%.1fg, 成本:约¥%.2f/耗时:%.1f小时)",
            doc_report.summary(), doc_report.bounding_box, doc_report.volume_cm3, doc_report.weight_g, doc_report.cost_cny, doc_report.print_time_hours
        )
        for w in doc_report.warnings[:2]:
            _log.warning("[doctor] ⚠️ %s", w)

        print_guide_file = None
        if model_file:
            try:
                guide_path = Path(model_file).with_name(f"{Path(model_file).stem}_print_guide.md")
                generate_print_guide(spec, doc_report, guide_path)
                print_guide_file = str(guide_path)
                _log.info("[doctor] 已生成切片工艺指导书: %s", guide_path.name)
            except Exception as e:  # noqa: BLE001
                _log.warning("[doctor] 生成切片工艺指导书失败: %s", e)

        _log.info("[agent] 完成: %s (fix_rounds=%d, review_rounds=%d, errors=%d)", safe, rounds, review_rounds, len(errors))
        return AgentResult(
            success=True, spec=spec, spec_path=spec_path_str,
            scad_path=str(scad_path), model_file=model_file,
            preview_png=preview_png, viewer_html=viewer_html,
            print_guide=print_guide_file,
            errors=errors, fix_rounds=rounds,
            review_rounds=review_rounds, review=self.last_review,
            score_trajectory=getattr(self, "score_trajectory", []),
            doctor_report=doc_report,
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
    ) -> tuple[int, bool]:
        """审查闭环：预览图 vs 原图比对，不符合则修正规格并重渲染。

        返回 (审查轮数, scad 是否已在循环内重新落盘渲染)。
        spec 就地修改（通过 self._apply_review_fix 写回调用方传入的 dict）。
        """
        out_dir = Path(self.config.output_dir)
        max_review = getattr(self, "max_review_rounds", 3)
        # 多视角渲染：除主预览外额外渲 top/front/iso，审查时多角度回传 LLM
        views = getattr(self, "review_views", ("top", "front", "side", "iso"))
        best_spec = spec          # 记录迄今最好的一版（部件数最多且不编译失败）
        best_score = -1.0

        def _render_views(scad: Path):
            """渲染主预览 + 多视角，导出 3D 模型，返回 (主预览png, [额外视角pngs])。

            审查闭环内部重渲染时, 若 GUI 开启了 export_3mf/export_2d 开关,
            会同步重新生成 3MF 切片文件与 2D 激光投影 (DXF+SVG), 避免交付模型与旧投影不一致。
            """
            safe2 = scad.stem.replace("_scad", "")  # 去后缀安全名
            self._sync_renderer_overrides()
            model_path = Path(out_dir / f"{safe2}.{self.config.export_format}")
            self.renderer.export_model(scad, model_path)
            main_png = out_dir / f"{safe2}_preview.png"
            self.renderer.render_preview(scad, main_png)
            extra = self.renderer.render_multi_view(
                scad, main_png.with_name(f"{safe2}_v"), views=views, size=(500, 400)
            )
            # 3MF 切片同步重导出 (export_model 内部已根据 GUI 开关同步生成 .3mf, 无需二次 CGAL 求值)
            if getattr(self, "export_3mf", False) and model_path.with_suffix(".3mf").exists():
                _log.info("[review] 已同步重导出 3MF 切片文件: %s", safe2)
            # 2D 激光投影同步重渲染 (若 GUI 开关开启)
            if getattr(self, "export_2d", False):
                try:
                    self.renderer.export_2d_projection(scad, out_dir / f"{safe2}.dxf", cut=False)
                    self.renderer.export_2d_projection(scad, out_dir / f"{safe2}.svg", cut=False)
                    _log.info("[review] 已同步重导出 2D 激光投影: %s", safe2)
                except Exception as e:
                    _log.warning("[review] 2D 投影同步重导出失败: %s", e)
            return main_png, [p for p in extra if p != main_png]

        # 首轮渲染（若 _finalize 已渲染过 preview_png，直接复用其多视角）
        main_png = Path(preview_png)
        extra_views: list = []
        if self.enable_multi_view:
            extra_views = self.renderer.render_multi_view(
                scad_path, main_png.with_name(main_png.stem + "_v"), views=views, size=(500, 400)
            )
            extra_views = [p for p in extra_views if p != main_png]

        # 连续会话上下文记忆 (Conversational Review Chain)
        conversation_history: list[dict[str, Any]] = []
        score_trajectory: list[float] = []

        for i in range(1, max_review + 1):
            _log.info("[review] 审查渲染图 (round %d/%d, %d 视角, 会话记忆模式) …", i, max_review, len(extra_views) + 1)
            verdict = self.vision.review_preview(
                original_image, main_png, spec, user_hint=hint,
                extra_views=extra_views or None,
                conversation_history=conversation_history,
                round_index=i,
            )
            if verdict.get("skipped"):
                _log.info("[review] %s", verdict.get('verdict'))
                break
            cur_score = float(verdict.get("score", 0.0) or 0.0)
            score_trajectory.append(cur_score)
            self.score_trajectory = score_trajectory
            traj_str = " -> ".join(str(s) for s in score_trajectory)
            _log.info("[review] match=%s score=%s (演进走势: %s) | %s", verdict.get('match'), cur_score, traj_str, verdict.get('verdict'))
            if verdict.get("match"):
                # 符合预期，成功收敛
                best_score = cur_score
                self.last_review = verdict
                return i, False

            # 记录迄今最好的一版（用于回退）
            if cur_score > best_score:
                best_score = cur_score
                best_spec = dict(spec)

            # 不符合 → 应用 LLM 建议的 spec_patch 并重新生成+渲染
            patch = (verdict.get("suggested_fix") or {}).get("spec_patch")
            if not patch:
                _log.warning("[review] LLM 判为不符合但未给出 spec_patch，停止: %s", verdict.get('issues'))
                self.last_review = verdict
                return i, False
            try:
                new_spec = self._merge_spec_patch(spec, patch)
            except Exception as e:  # noqa: BLE001
                _log.warning("[review] 应用 spec_patch 失败: %s，停止", e)
                self.last_review = verdict
                return i, False

            # 回归保护：若 LLM 修正规格使部件数减少（简化而非补全）→ 拒绝该修正，回退最优 spec
            old_cnt = len(spec.get("parts", []))
            new_cnt = len(new_spec.get("parts", []))
            if new_cnt < old_cnt:
                _log.warning("[review] LLM 修正减少部件数 (%d->%d)，视为回归，拒绝并回退最优 spec", old_cnt, new_cnt)
                spec.clear()
                spec.update(best_spec)
                self.last_review = verdict
                return i, self._rewind_best_spec(best_spec, safe, out_dir)

            # 写回 spec（就地更新调用方的 dict）
            spec.clear()
            spec.update(new_spec)
            spec_path_str, scad_path_str, safe2 = write_spec_and_scad(spec, self.config, tag=safe)
            new_scad = Path(scad_path_str)
            ok, cerr = self.renderer.validate(new_scad)
            if not ok:
                _log.warning("[review] 修正后 spec 编译失败: %s，停止", cerr[:300])
                self.last_review = verdict
                return i, False
            # 重渲染主预览 + 多视角
            try:
                new_main, new_extra = _render_views(new_scad)
                main_png = new_main
                extra_views = new_extra if self.enable_multi_view else []
            except RuntimeError as e:
                _log.warning("[review] 重渲染失败: %s，停止", e)
                self.last_review = verdict
                return i, False
            _log.info("[review] 已修正规格并重渲染 (round %d)", i)

        # 若循环结束仍 match=false（未收敛），回退到迄今得分最高的 spec，
        # 避免把模型越改越糟（LLM 可能简化而非补全）。
        rewound = False
        if best_spec is not spec and best_score > -1.0:
            _log.info("[review] 未收敛，回退到得分最高的 spec (score=%s)", best_score)
            spec.clear()
            spec.update(best_spec)
            rewound = self._rewind_best_spec(best_spec, safe, out_dir)

        self.last_review = getattr(self, "last_review", None)
        return min(max_review, 99), rewound

    def _rewind_best_spec(
        self, best_spec: dict[str, Any], safe: str, out_dir: Path
    ) -> bool:
        """将最优 spec 重新落盘并重新渲染，使交付的 3D 模型与回退后的规格一致。

        若 GUI 开启了 export_3mf/export_2d 开关, 回退后同步重新生成 3MF 与 2D 投影,
        避免交付包中 3D 模型与旧 2D 切片文件版本不一致。
        返回是否已成功重新渲染 (True 表示调用方无需再重复渲染)。
        """
        try:
            spec_path_str, scad_path_str, safe2 = write_spec_and_scad(best_spec, self.config, tag=safe)
            new_scad = Path(scad_path_str)
            ok, cerr = self.renderer.validate(new_scad)
            if not ok:
                _log.warning("[review] 回退 spec 重新编译失败: %s", cerr[:200])
                return False
            model_file = out_dir / f"{safe2}.{self.config.export_format}"
            preview_png = out_dir / f"{safe2}_preview.png"
            self._sync_renderer_overrides()  # 确保渲染器感知最新 GUI 开关状态
            self.renderer.export_model(new_scad, model_file)
            self.renderer.render_preview(new_scad, preview_png)
            # 3MF 切片同步重导出 (export_model 内部已根据 GUI 开关同步生成 .3mf, 无需二次 CGAL 求值)
            if getattr(self, "export_3mf", False) and model_file.with_suffix(".3mf").exists():
                _log.info("[review] 回退后已同步重导出 3MF 切片文件: %s", safe2)
            # 2D 激光投影同步重渲染 (若 GUI 开关开启)
            if getattr(self, "export_2d", False):
                try:
                    self.renderer.export_2d_projection(new_scad, out_dir / f"{safe2}.dxf", cut=False)
                    self.renderer.export_2d_projection(new_scad, out_dir / f"{safe2}.svg", cut=False)
                except Exception as e:
                    _log.warning("[review] 回退后 2D 投影重导出失败: %s", e)
            _log.info("[review] 已将最优 spec 重新落盘并渲染: %s", safe2)
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("[review] 回退重新渲染异常: %s", e)
            return False

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
        self, spec: dict[str, Any], compile_err: str, hint: str, image_name: str, round_index: int = 1
    ) -> Optional[dict[str, Any]]:
        """把 OpenSCAD 编译错误回传给 LLM，请求修正后的规格（带阶梯温控与错误定向引导）。

        若 LLM 因限流/不可用无法修正，返回原 spec（避免把已知正确的 spec
        覆盖为 LLM 重新解析的无效数据）。
        """
        from dataclasses import replace
        step_temp = min(self.config.temperature + (round_index - 1) * 0.2, 0.7)
        sys, user = _build_fix_messages(spec, compile_err, hint, image_name)
        try:
            fix_config = replace(self.config, temperature=step_temp)
            vision = AgnesVision(fix_config)
            result = vision._chat([
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ])
            return result
        except Exception as e:  # noqa: BLE001
            _log.warning("[agent] 修正请求异常: %s，保留原 spec", e)
            return spec


def _rm_quiet(path: Path) -> None:
    """静默删除文件（不存在/失败均忽略）。用于清理 _prep 临时预处理图。"""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except OSError:
        pass


def _rm_empty_dir(path: Path) -> None:
    """若目录已空则删除（失败忽略）。用于清理 out/_prep 空目录。"""
    try:
        p = Path(path)
        if p.exists() and not any(p.iterdir()):
            p.rmdir()
    except OSError:
        pass
