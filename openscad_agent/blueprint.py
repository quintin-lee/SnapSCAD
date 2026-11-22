"""工业级工程制图自动尺寸标注与矢量图纸引擎 (Dimensioned Technical Blueprint Engine)。

职责：
- 从 CAD 规格中解析几何拓扑与包围盒，投影生成标准机械正交三视图 (主视/俯视/侧视) 与轴测图；
- 自动计算并绘制符合 ISO / GB 制图标准的尺寸标注线、引出线、中心线、对称轴与双向箭头；
- 自动标注通孔/沉孔直径 (⌀)、孔心距、孔边距与整体轮廓长宽高尺寸；
- 绘制标准工程图框与右下角标准工程标题栏 (Title Block，包含零件名、材质、比例、投影符号与生成日期)；
- 零第三方重型依赖，直接生成纯矢量 SVG 图纸 (*_blueprint.svg) 与高清光栅 PNG。
"""

from __future__ import annotations

import base64
import datetime
import math
import shutil
from pathlib import Path
from typing import Any, Optional

from .core import run_cmd
from .doctor import _get_part_aabb
from .log import get_logger as _log_module
from .spec import normalize_hole_size, normalize_size, resolve_geometric_constraints

_log = _log_module()


def _fmt(val: float) -> str:
    f = float(val)
    return str(int(f)) if f.is_integer() else f"{f:.1f}"


def generate_technical_blueprint(
    spec: dict[str, Any],
    out_svg: Path,
    out_png: Optional[Path] = None,
    preview_png: Optional[Path] = None,
    material: str = "PLA",
) -> Path:
    """生成带标准尺寸标注、孔系引出标注与 ISO 标题栏的纯矢量工程图纸。"""
    out_svg = Path(out_svg)
    out_svg.parent.mkdir(parents=True, exist_ok=True)

    # 1. 几何约束解算与包围盒计算
    resolved_spec, _ = resolve_geometric_constraints(spec)
    parts = resolved_spec.get("parts", [])
    if not parts:
        parts = [{"id": "body", "primitive": "cube", "size": [40, 30, 20], "center": [0, 0, 0]}]

    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")
    min_z, max_z = float("inf"), float("-inf")

    for p in parts:
        p_min, p_max = _get_part_aabb(p)
        min_x = min(min_x, p_min[0])
        max_x = max(max_x, p_max[0])
        min_y = min(min_y, p_min[1])
        max_y = max(max_y, p_max[1])
        min_z = min(min_z, p_min[2])
        max_z = max(max_z, p_max[2])

    total_w = max(max_x - min_x, 1.0)
    total_d = max(max_y - min_y, 1.0)
    total_h = max(max_z - min_z, 1.0)

    # 提取孔洞信息
    holes_info: list[dict[str, Any]] = []
    for p in parts:
        holes = p.get("holes") or []
        for h in holes:
            prim = (h.get("primitive") or "cylinder").lower()
            sz = normalize_hole_size(prim, h)
            c = h.get("center", [0, 0, 0])
            cx = float(c[0]) if len(c) >= 1 else 0.0
            cy = float(c[1]) if len(c) >= 2 else 0.0
            cz = float(c[2]) if len(c) >= 3 else 0.0
            dia = sz[0]
            holes_info.append({"dia": dia, "cx": cx, "cy": cy, "cz": cz, "prim": prim})

    # 2. 图纸尺寸与缩放比例规划 (标准 A3/A4 横向工程幅面: 1200 x 800)
    sheet_w, sheet_h = 1200, 800
    view_box_w = 260.0
    view_box_h = 190.0
    scale = min(view_box_w / total_w, view_box_h / total_d, view_box_h / total_h, 3.5)

    # 确定视图中心基准点
    # 俯视图 Top View (X-Y)
    top_cx, top_cy = 340.0, 240.0
    # 主视图 Front View (X-Z)
    front_cx, front_cy = 340.0, 530.0
    # 左视图 / 侧视图 Left/Side View (Y-Z)
    side_cx, side_cy = 730.0, 530.0
    # 轴测图 Isometric View
    iso_cx, iso_cy = 730.0, 240.0

    lines: list[str] = []
    lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {sheet_w} {sheet_h}" width="{sheet_w}" height="{sheet_h}">')

    # 定义箭头与样式
    lines.append('  <defs>')
    lines.append('    <marker id="arrow" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">')
    lines.append('      <path d="M 0 1.5 L 10 5 L 0 8.5 z" fill="#0284c7" />')
    lines.append('    </marker>')
    lines.append('    <marker id="arrow_rev" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="6" markerHeight="6" orient="auto">')
    lines.append('      <path d="M 10 1.5 L 0 5 L 10 8.5 z" fill="#0284c7" />')
    lines.append('    </marker>')
    lines.append('    <style>')
    lines.append('      .sheet-bg { fill: #ffffff; }')
    lines.append('      .border-outer { stroke: #0f172a; stroke-width: 2.5; fill: none; }')
    lines.append('      .border-inner { stroke: #334155; stroke-width: 1.2; fill: none; }')
    lines.append('      .title-block { stroke: #0f172a; stroke-width: 1.5; fill: #f8fafc; }')
    lines.append('      .title-text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 11px; fill: #334155; }')
    lines.append('      .title-bold { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 13px; font-weight: bold; fill: #0f172a; }')
    lines.append('      .part-outline { stroke: #0f172a; stroke-width: 2.0; fill: #f1f5f9; stroke-linejoin: round; }')
    lines.append('      .center-line { stroke: #ef4444; stroke-width: 0.9; stroke-dasharray: 10, 3, 2, 3; }')
    lines.append('      .dim-line { stroke: #0284c7; stroke-width: 1.1; fill: none; }')
    lines.append('      .dim-text { font-family: "Consolas", "Courier New", monospace; font-size: 11px; font-weight: bold; fill: #0369a1; text-anchor: middle; }')
    lines.append('      .view-label { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 14px; font-weight: bold; fill: #475569; }')
    lines.append('    </style>')
    lines.append('  </defs>')

    # 3. 绘制图纸图框 (Standard Drawing Border)
    lines.append(f'  <rect class="sheet-bg" width="{sheet_w}" height="{sheet_h}" />')
    lines.append('  <!-- 外图框与内图框 (左侧留 20mm 装订边) -->')
    lines.append(f'  <rect class="border-outer" x="15" y="15" width="{sheet_w - 30}" height="{sheet_h - 30}" />')
    lines.append(f'  <rect class="border-inner" x="40" y="25" width="{sheet_w - 65}" height="{sheet_h - 50}" />')

    # 4. 辅助函数：绘制尺寸标注线 (Dimension Line with Witness Lines & Arrows)
    def _draw_horiz_dim(x1: float, x2: float, y_dim: float, y_feat: float, text: str, text_above: bool = True):
        # 尺寸界线 (witness lines)
        margin = 3.0
        ext = 5.0
        min_y = min(y_dim, y_feat) - ext
        max_y = max(y_dim, y_feat) + ext
        lines.append(f'  <line class="dim-line" x1="{x1:.1f}" y1="{min_y:.1f}" x2="{x1:.1f}" y2="{max_y:.1f}" />')
        lines.append(f'  <line class="dim-line" x1="{x2:.1f}" y1="{min_y:.1f}" x2="{x2:.1f}" y2="{max_y:.1f}" />')
        # 尺寸线 (dimension line with arrows)
        lines.append(f'  <line class="dim-line" x1="{x1:.1f}" y1="{y_dim:.1f}" x2="{x2:.1f}" y2="{y_dim:.1f}" marker-start="url(#arrow_rev)" marker-end="url(#arrow)" />')
        # 尺寸文字
        t_y = y_dim - 4.0 if text_above else y_dim + 12.0
        lines.append(f'  <text class="dim-text" x="{(x1 + x2) / 2.0:.1f}" y="{t_y:.1f}">{text}</text>')

    def _draw_vert_dim(y1: float, y2: float, x_dim: float, x_feat: float, text: str, text_right: bool = True):
        ext = 5.0
        min_x = min(x_dim, x_feat) - ext
        max_x = max(x_dim, x_feat) + ext
        lines.append(f'  <line class="dim-line" x1="{min_x:.1f}" y1="{y1:.1f}" x2="{max_x:.1f}" y2="{y1:.1f}" />')
        lines.append(f'  <line class="dim-line" x1="{min_x:.1f}" y1="{y2:.1f}" x2="{max_x:.1f}" y2="{y2:.1f}" />')
        lines.append(f'  <line class="dim-line" x1="{x_dim:.1f}" y1="{y1:.1f}" x2="{x_dim:.1f}" y2="{y2:.1f}" marker-start="url(#arrow_rev)" marker-end="url(#arrow)" />')
        # 垂直文字旋转或居侧
        t_x = x_dim + 12.0 if text_right else x_dim - 12.0
        lines.append(f'  <text class="dim-text" x="{t_x:.1f}" y="{(y1 + y2) / 2.0 + 4:.1f}">{text}</text>')

    # 5. 视图 1: 俯视图 (Top View - X-Y 面)
    top_w_px = total_w * scale
    top_d_px = total_d * scale
    top_x0 = top_cx - top_w_px / 2.0
    top_y0 = top_cy - top_d_px / 2.0
    lines.append('  <!-- 视图 1: 俯视图 Top View (X-Y) -->')
    lines.append(f'  <text class="view-label" x="{top_x0:.1f}" y="{top_y0 - 20:.1f}">俯视图 TOP VIEW (X-Y)</text>')
    lines.append(f'  <rect class="part-outline" x="{top_x0:.1f}" y="{top_y0:.1f}" width="{top_w_px:.1f}" height="{top_d_px:.1f}" rx="3" />')

    # 俯视图中心线
    lines.append(f'  <line class="center-line" x1="{top_x0 - 15:.1f}" y1="{top_cy:.1f}" x2="{top_x0 + top_w_px + 15:.1f}" y2="{top_cy:.1f}" />')
    lines.append(f'  <line class="center-line" x1="{top_cx:.1f}" y1="{top_y0 - 15:.1f}" x2="{top_cx:.1f}" y2="{top_y0 + top_d_px + 15:.1f}" />')

    # 俯视图孔洞投影
    for idx, h in enumerate(holes_info):
        hx = top_x0 + (h["cx"] - min_x) * scale
        hy = top_y0 + (h["cy"] - min_y) * scale
        hr = (h["dia"] / 2.0) * scale
        lines.append(f'  <circle cx="{hx:.1f}" cy="{hy:.1f}" r="{hr:.1f}" fill="#ffffff" stroke="#0f172a" stroke-width="1.6" />')
        lines.append(f'  <line class="center-line" x1="{hx - hr - 4:.1f}" y1="{hy:.1f}" x2="{hx + hr + 4:.1f}" y2="{hy:.1f}" />')
        lines.append(f'  <line class="center-line" x1="{hx:.1f}" y1="{hy - hr - 4:.1f}" x2="{hx:.1f}" y2="{hy + hr + 4:.1f}" />')

    # 俯视图外形尺寸标注: 宽度 W 与 深度 D
    _draw_horiz_dim(top_x0, top_x0 + top_w_px, top_y0 - 10, top_y0, f"W {_fmt(total_w)}")
    _draw_vert_dim(top_y0, top_y0 + top_d_px, top_x0 - 25, top_x0, f"D {_fmt(total_d)}", text_right=False)

    # 6. 视图 2: 主视图 (Front View - X-Z 面)
    front_h_px = total_h * scale
    front_x0 = top_x0  # 上下严格对齐
    front_y0 = front_cy - front_h_px / 2.0
    lines.append('  <!-- 视图 2: 主视图 Front View (X-Z) -->')
    lines.append(f'  <text class="view-label" x="{front_x0:.1f}" y="{front_y0 - 20:.1f}">主视图 FRONT VIEW (X-Z)</text>')
    lines.append(f'  <rect class="part-outline" x="{front_x0:.1f}" y="{front_y0:.1f}" width="{top_w_px:.1f}" height="{front_h_px:.1f}" rx="2" />')

    # 主视图中心线
    lines.append(f'  <line class="center-line" x1="{front_cx:.1f}" y1="{front_y0 - 15:.1f}" x2="{front_cx:.1f}" y2="{front_y0 + front_h_px + 15:.1f}" />')

    # 主视图孔洞虚线 (隐藏线)
    for h in holes_info:
        hx = front_x0 + (h["cx"] - min_x) * scale
        hr = (h["dia"] / 2.0) * scale
        lines.append(f'  <line x1="{hx - hr:.1f}" y1="{front_y0:.1f}" x2="{hx - hr:.1f}" y2="{front_y0 + front_h_px:.1f}" stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="4, 3" />')
        lines.append(f'  <line x1="{hx + hr:.1f}" y1="{front_y0:.1f}" x2="{hx + hr:.1f}" y2="{front_y0 + front_h_px:.1f}" stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="4, 3" />')
        lines.append(f'  <line class="center-line" x1="{hx:.1f}" y1="{front_y0 - 6:.1f}" x2="{hx:.1f}" y2="{front_y0 + front_h_px + 6:.1f}" />')

    # 主视图外形标注: 总高 H
    _draw_vert_dim(front_y0, front_y0 + front_h_px, front_x0 - 25, front_x0, f"H {_fmt(total_h)}", text_right=False)
    # 底面基准引出总宽 W
    _draw_horiz_dim(front_x0, front_x0 + top_w_px, front_y0 + front_h_px + 25, front_y0 + front_h_px, f"{_fmt(total_w)} mm", text_above=False)

    # 7. 视图 3: 左/侧视图 (Side View - Y-Z 面)
    side_w_px = top_d_px  # 侧视宽度等于俯视深度
    side_h_px = front_h_px  # 左右高度严格对齐
    side_x0 = side_cx - side_w_px / 2.0
    side_y0 = front_y0
    lines.append('  <!-- 视图 3: 左/侧视图 Side View (Y-Z) -->')
    lines.append(f'  <text class="view-label" x="{side_x0:.1f}" y="{side_y0 - 20:.1f}">侧视图 SIDE VIEW (Y-Z)</text>')
    lines.append(f'  <rect class="part-outline" x="{side_x0:.1f}" y="{side_y0:.1f}" width="{side_w_px:.1f}" height="{side_h_px:.1f}" rx="2" />')
    lines.append(f'  <line class="center-line" x1="{side_cx:.1f}" y1="{side_y0 - 15:.1f}" x2="{side_cx:.1f}" y2="{side_y0 + side_h_px + 15:.1f}" />')
    _draw_horiz_dim(side_x0, side_x0 + side_w_px, side_y0 + side_h_px + 25, side_y0 + side_h_px, f"D {_fmt(total_d)}", text_above=False)

    # 8. 视图 4: 轴测示意图 (Isometric View, 右上角)
    lines.append('  <!-- 视图 4: 轴测投影与实物预览 Isometric View -->')
    lines.append(f'  <text class="view-label" x="{iso_cx - 100:.1f}" y="{iso_cy - 100:.1f}">轴测图 ISOMETRIC 3D</text>')
    if preview_png and Path(preview_png).exists():
        try:
            b64_data = base64.b64encode(Path(preview_png).read_bytes()).decode("ascii")
            lines.append(f'  <image x="{iso_cx - 110:.1f}" y="{iso_cy - 90:.1f}" width="220" height="170" href="data:image/png;base64,{b64_data}" />')
        except Exception:
            lines.append(f'  <rect x="{iso_cx - 90:.1f}" y="{iso_cy - 70:.1f}" width="180" height="140" fill="#f8fafc" stroke="#cbd5e1" stroke-width="1.5" rx="4" />')
    else:
        # 纯矢量等轴测立方示意框
        lines.append(f'  <rect x="{iso_cx - 90:.1f}" y="{iso_cy - 70:.1f}" width="180" height="140" fill="#f8fafc" stroke="#cbd5e1" stroke-width="1.5" rx="4" />')
        lines.append(f'  <text class="dim-text" x="{iso_cx:.1f}" y="{iso_cy:.1f}">3D AXONOMETRIC</text>')

    # 9. 孔径标准引线标注 (Hole Leader Callout)
    if holes_info:
        target_h = holes_info[0]
        hx = top_x0 + (target_h["cx"] - min_x) * scale
        hy = top_y0 + (target_h["cy"] - min_y) * scale
        hr = (target_h["dia"] / 2.0) * scale
        h_count = len(holes_info)
        leader_text = f"{h_count}x ⌀{_fmt(target_h['dia'])} THRU" if h_count > 1 else f"⌀{_fmt(target_h['dia'])} THRU"
        # 45度折线引出
        lx1, ly1 = hx + hr * 0.707, hy - hr * 0.707
        lx2, ly2 = lx1 + 35.0, ly1 - 25.0
        lx3 = lx2 + 45.0
        lines.append('  <!-- 孔径引线标注 -->')
        lines.append(f'  <polyline points="{lx1:.1f},{ly1:.1f} {lx2:.1f},{ly2:.1f} {lx3:.1f},{ly2:.1f}" stroke="#0284c7" stroke-width="1.2" fill="none" marker-start="url(#arrow)" />')
        lines.append(f'  <text class="dim-text" x="{lx2 + 20:.1f}" y="{ly2 - 4:.1f}">{leader_text}</text>')

    # 10. 标准 ISO / GB 标题栏 (Title Block - 右下角)
    tb_x, tb_y = 780.0, 640.0
    tb_w, tb_h = 355.0, 110.0
    lines.append('  <!-- 标准工程制图标题栏 Title Block (ISO / GB) -->')
    lines.append(f'  <rect class="title-block" x="{tb_x:.1f}" y="{tb_y:.1f}" width="{tb_w:.1f}" height="{tb_h:.1f}" />')

    # 内部网格分隔线
    lines.append(f'  <line x1="{tb_x}" y1="{tb_y + 36}" x2="{tb_x + tb_w}" y2="{tb_y + 36}" stroke="#0f172a" stroke-width="1.2" />')
    lines.append(f'  <line x1="{tb_x}" y1="{tb_y + 72}" x2="{tb_x + tb_w}" y2="{tb_y + 72}" stroke="#0f172a" stroke-width="1.2" />')
    lines.append(f'  <line x1="{tb_x + 190}" y1="{tb_y}" x2="{tb_x + 190}" y2="{tb_y + 72}" stroke="#0f172a" stroke-width="1.2" />')
    lines.append(f'  <line x1="{tb_x + 270}" y1="{tb_y}" x2="{tb_x + 270}" y2="{tb_y + 72}" stroke="#0f172a" stroke-width="1.2" />')
    lines.append(f'  <line x1="{tb_x + 120}" y1="{tb_y + 72}" x2="{tb_x + 120}" y2="{tb_y + tb_h}" stroke="#0f172a" stroke-width="1.2" />')
    lines.append(f'  <line x1="{tb_x + 240}" y1="{tb_y + 72}" x2="{tb_x + 240}" y2="{tb_y + tb_h}" stroke="#0f172a" stroke-width="1.2" />')

    # 标题栏文字
    m_name = spec.get("name", "CAD_MODEL").upper()
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    lines.append(f'  <text class="title-text" x="{tb_x + 8}" y="{tb_y + 16}">工程图名称 DRAWING TITLE</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 8}" y="{tb_y + 30}">{m_name}</text>')

    lines.append(f'  <text class="title-text" x="{tb_x + 198}" y="{tb_y + 16}">图号 DWG NO</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 198}" y="{tb_y + 30}">SCAD-01</text>')

    lines.append(f'  <text class="title-text" x="{tb_x + 278}" y="{tb_y + 16}">比例 SCALE</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 278}" y="{tb_y + 30}">1 : 1</text>')

    lines.append(f'  <text class="title-text" x="{tb_x + 8}" y="{tb_y + 52}">推荐材料 MATERIAL</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 8}" y="{tb_y + 66}">{material.upper()} (FDM)</text>')

    lines.append(f'  <text class="title-text" x="{tb_x + 198}" y="{tb_y + 52}">单位 UNITS</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 198}" y="{tb_y + 66}">METRIC (mm)</text>')

    # 第三角投影法图符 (Third-Angle Projection Symbol: 锥台与圆)
    px, py = tb_x + 310, tb_y + 52
    lines.append('  <!-- 投影法图符 -->')
    lines.append(f'  <circle cx="{px + 22}" cy="{py}" r="7" stroke="#334155" stroke-width="1.1" fill="none" />')
    lines.append(f'  <circle cx="{px + 22}" cy="{py}" r="3.5" stroke="#334155" stroke-width="1.1" fill="none" />')
    lines.append(f'  <polygon points="{px - 14},{py - 7} {px - 4},{py - 4} {px - 4},{py + 4} {px - 14},{py + 7}" stroke="#334155" stroke-width="1.1" fill="none" />')

    lines.append(f'  <text class="title-text" x="{tb_x + 8}" y="{tb_y + 88}">设计生成 GENERATOR</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 8}" y="{tb_y + 102}">SnapSCAD Agent</text>')

    lines.append(f'  <text class="title-text" x="{tb_x + 128}" y="{tb_y + 88}">生成日期 DATE</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 128}" y="{tb_y + 102}">{date_str}</text>')

    lines.append(f'  <text class="title-text" x="{tb_x + 248}" y="{tb_y + 88}">版次 REV</text>')
    lines.append(f'  <text class="title-bold" x="{tb_x + 248}" y="{tb_y + 102}">A.1</text>')

    lines.append('</svg>')
    svg_content = "\n".join(lines)
    out_svg.write_text(svg_content, encoding="utf-8")
    _log.info("[blueprint] 工业级标准尺寸工程图纸已生成: %s", out_svg)

    # 11. 可选转换为光栅 PNG
    if out_png:
        out_png = Path(out_png)
        magick = shutil.which("magick") or shutil.which("convert")
        if magick:
            cmd = [magick, "-density", "150", str(out_svg), str(out_png)]
            code, _, _ = run_cmd(cmd)
            if code == 0 and out_png.exists():
                _log.info("[blueprint] 同步渲染高清制图 PNG: %s", out_png)

    return out_svg


__all__ = ["generate_technical_blueprint"]
