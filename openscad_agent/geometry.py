"""3D 几何生成器：把 LLM 输出的 JSON 规格转译为可编译的 OpenSCAD 源码。

设计原则：
- 每个 part 生成一个独立 OpenSCAD 模块（function），便于阅读、调试与局部修改。
- 顶层按 assembly.union_parts 做 union，再对 holes 做 difference 挖除，
  输出单一可打印实体。
- 全程使用标准 OpenSCAD 2.x CSG 语法，兼容 openscad -o 渲染。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .core import AgentConfig
from .spec import normalize_size as _spec_norm_size, normalize_hole_size as _spec_norm_hole, validate_spec, resolve_geometric_constraints
from .doctor import check_printability, _get_part_aabb
from .log import get_logger as _log_module

_log = _log_module()


def _fmt(v: float) -> str:
    """格式化数值：整数不带小数点。"""
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _cylinder_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """圆柱/圆锥/倒角立柱生成器。
    size = [直径, 高度] 或 [底面直径d1, 顶面直径d2, 高度h] (圆锥/圆台)
    或 [直径, 直径, 高度, chamfer] (带顶部45度机械导入倒角立柱)。
    """
    if len(size) >= 4 and float(size[3]) > 1e-4:
        d = float(size[0])
        h = float(size[2])
        c = min(float(size[3]), h * 0.45, d / 2.0 - 0.1)
        cx, cy, cz = (float(center[i]) for i in range(3))
        r = d / 2.0
        lines = [f"  // {note} (顶部倒角 chamfer c={_fmt(c)}mm)" if note else f"  // chamfered cylinder (c={_fmt(c)}mm)"]
        lines.append(f"  translate([{_fmt(cx - r)}, {_fmt(cy - r)}, {_fmt(cz)}]) {{")
        lines.append(f"    cylinder(h = {_fmt(h - c)}, d = {_fmt(d)}, center = false);")
        lines.append(f"    translate([0, 0, {_fmt(h - c)}])")
        lines.append(f"      cylinder(h = {_fmt(c)}, d1 = {_fmt(d)}, d2 = {_fmt(d - 2 * c)}, center = false);")
        lines.append("  }")
        return "\n".join(lines)

    if len(size) >= 3:
        d1, d2, h = float(size[0]), float(size[1]), float(size[2])
    else:
        d1 = d2 = float(size[0])
        h = float(size[1])
    cx, cy, cz = (float(center[i]) for i in range(3))
    max_r = max(d1, d2) / 2.0
    lines = [f"  // {note}" if note else ("  // cone/taper" if abs(d1 - d2) > 1e-6 else "  // cylinder")]
    lines.append(f"  translate([{_fmt(cx - max_r)}, {_fmt(cy - max_r)}, {_fmt(cz)}])")
    if abs(d1 - d2) < 1e-6:
        lines.append(f"    cylinder(h = {_fmt(h)}, d = {_fmt(d1)}, center = false);")
    else:
        lines.append(f"    cylinder(h = {_fmt(h)}, d1 = {_fmt(d1)}, d2 = {_fmt(d2)}, center = false);")
    return "\n".join(lines)


def _cube_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    x, y, z = (float(size[i]) for i in range(3))
    fillet = float(size[3]) if len(size) >= 4 else 0.0
    chamfer = float(size[4]) if len(size) >= 5 else 0.0
    cx, cy, cz = (float(center[i]) for i in range(3))

    # 安全约束：倒角/倒圆角半径不能超过半宽或半深
    fillet = min(fillet, min(x, y) / 2.0 - 0.01) if fillet > 0.0 else 0.0
    chamfer = min(chamfer, min(x, y) / 2.0 - 0.01) if chamfer > 0.0 else 0.0

    if chamfer > 1e-4:
        lines = [f"  // {note} (平滑倒角 chamfer c={_fmt(chamfer)}mm)" if note else f"  // chamfered cube (c={_fmt(chamfer)}mm)"]
        lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])")
        lines.append(f"    linear_extrude(height = {_fmt(z)}) {{")
        lines.append(f"      offset(delta = {_fmt(chamfer)}, chamfer = true) {{")
        lines.append(f"        square([{_fmt(x - 2 * chamfer)}, {_fmt(y - 2 * chamfer)}], center = true);")
        lines.append("      }")
        lines.append("    }")
        return "\n".join(lines)
    elif fillet > 1e-4:
        lines = [f"  // {note} (圆角倒角 r={_fmt(fillet)}mm)" if note else f"  // rounded cube (r={_fmt(fillet)}mm)"]
        lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])")
        lines.append(f"    linear_extrude(height = {_fmt(z)}) {{")
        lines.append(f"      offset(r = {_fmt(fillet)}) {{")
        lines.append(f"        square([{_fmt(x - 2 * fillet)}, {_fmt(y - 2 * fillet)}], center = true);")
        lines.append("      }")
        lines.append("    }")
        return "\n".join(lines)
    else:
        lines = [f"  // {note}" if note else "  // cube"]
        lines.append(f"  translate([{_fmt(cx - x / 2)}, {_fmt(cy - y / 2)}, {_fmt(cz)}])")
        lines.append(f"    cube([{_fmt(x)}, {_fmt(y)}, {_fmt(z)}], center = false);")
        return "\n".join(lines)


def _capsule_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """胶囊体生成器：通过两端球体的凸包 hull() 构造平滑倒角胶囊柱。"""
    r = float(size[0]) if size else 5.0
    h = float(size[1]) if len(size) >= 2 else 20.0
    cx, cy, cz = (float(center[i]) for i in range(3))
    span = max(h - 2 * r, 0.0)
    lines = [f"  // {note}" if note else "  // capsule"]
    lines.append("  hull() {")
    lines.append(f"    translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz + r)}])")
    lines.append(f"      sphere(r = {_fmt(r)});")
    lines.append(f"    translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz + r + span)}])")
    lines.append(f"      sphere(r = {_fmt(r)});")
    lines.append("  }")
    return "\n".join(lines)


def _sphere_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    r = float(size[0])
    cx, cy, cz = (float(center[i]) for i in range(3))
    lines = [f"  // {note}" if note else "  // sphere"]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])")
    lines.append(f"    sphere(r = {_fmt(r)});")
    return "\n".join(lines)


def _polyhedron_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """polyhedron 无顶点数据时的降级处理（退化为 box）。

    有 points/faces 时由 _polyhedron_vertices 专门处理，不走此 builder。
    """
    x, y, z = (float(size[i]) for i in range(3))
    cx, cy, cz = (float(center[i]) for i in range(3))
    lines = [f"  // {note} (polyhedron 降级为 box)" if note else "  // polyhedron -> box"]
    lines.append(f"  translate([{_fmt(cx - x / 2)}, {_fmt(cy - y / 2)}, {_fmt(cz)}])")
    lines.append(f"    cube([{_fmt(x)}, {_fmt(y)}, {_fmt(z)}]);")
    return "\n".join(lines)


def _has_polyhedron_vertices(part: dict[str, Any]) -> bool:
    """判断 polyhedron 是否携带可用顶点数据。"""
    points = part.get("points") or part.get("vertices")
    faces = part.get("faces")
    return isinstance(points, list) and len(points) > 0 and isinstance(faces, list) and len(faces) > 0


def _fmt_point(p) -> str:
    try:
        return f"[{_fmt(p[0])}, {_fmt(p[1])}, {_fmt(p[2])}]"
    except (IndexError, TypeError, ValueError):
        return "[0, 0, 0]"


def _polyhedron_vertices(part: dict[str, Any], center: list[float], note: str) -> list[str]:
    """用 LLM 提供的 points/faces 生成真正的 polyhedron() 调用。

    - points: 顶点坐标数组（绝对坐标，已包含位置；若为局部坐标则加上 center 平移）
    - faces:  面（顶点下标数组）
    顶点坐标若都在 [0,1] 量级（疑似归一化/局部），自动加上 center 平移；
    否则视为绝对坐标直接使用。
    """
    points = part.get("points") or part.get("vertices")
    faces = part.get("faces")
    cx, cy, cz = (float(center[i]) for i in range(3))

    # 判断是否需要平移：若所有顶点分量绝对值都较小（<=2），视为局部坐标
    coords = []
    try:
        for p in points:
            coords.append([float(p[0]), float(p[1]), float(p[2])])
    except (IndexError, TypeError, ValueError):
        # 顶点数据非法 → 降级
        return _polyhedron_code([10, 10, 10], center, [], note).split("\n")

    max_abs = max((abs(c) for p in coords for c in p), default=0.0)
    # 所有顶点分量都较小（<=2）视为局部/归一化坐标 → 加上 center 平移
    translate = max_abs <= 2.0

    lines = [f"  // {note} (polyhedron {len(coords)}v/{len(faces)}f)" if note else "  // polyhedron"]
    # 顶点列表
    pt_lines: list[str] = []
    for p in coords:
        if translate:
            pt_lines.append(f"    [{_fmt(p[0] + cx)}, {_fmt(p[1] + cy)}, {_fmt(p[2] + cz)}]")
        else:
            pt_lines.append(f"    [{_fmt(p[0])}, {_fmt(p[1])}, {_fmt(p[2])}]")
    # 面列表
    face_lines: list[str] = []
    for f in faces:
        if isinstance(f, (list, tuple)):
            idx = ", ".join(_fmt(int(i)) for i in f)
            face_lines.append(f"    [{idx}]")
        else:
            face_lines.append(f"    [{_fmt(int(f))}]")

    lines.append("  polyhedron(points = [")
    lines.append(",\n".join(pt_lines))
    lines.append("  ], faces = [")
    lines.append(",\n".join(face_lines))
    lines.append("  ], convexity = 3);")
    return lines


def _build_thread_mesh(
    d: float = 8.0,
    pitch: float = 1.25,
    length: float = 15.0,
    pts_per_ring: int = 24,
    rings_per_pitch: int = 8,
    tolerance: float = 0.0,
) -> tuple[list[list[float]], list[list[int]]]:
    """生成 100% 封闭无自交的 ISO 公制内外螺纹单 polyhedron 拓扑网格。"""
    r_crest = max((d / 2.0) + tolerance, 0.4)
    depth = 0.541266 * pitch
    r_root = max(r_crest - depth, 0.2)

    total_rings = int(math.ceil((length / pitch) * rings_per_pitch)) + 1
    dz = length / (total_rings - 1)
    d_theta = 2.0 * math.pi / pts_per_ring

    points: list[list[float]] = []
    for j in range(total_rings):
        z = j * dz
        taper_len = pitch * 0.8
        taper = 1.0
        if z < taper_len:
            taper = z / taper_len
        elif z > length - taper_len:
            taper = (length - z) / taper_len
        taper = max(min(taper, 1.0), 0.0)

        for i in range(pts_per_ring):
            theta = i * d_theta
            phi = ((z / pitch) - (theta / (2.0 * math.pi))) % 1.0
            if phi < 0.375:
                r_val = r_root + (phi / 0.375) * (r_crest - r_root)
            elif phi < 0.625:
                r_val = r_crest
            else:
                r_val = r_crest - ((phi - 0.625) / 0.375) * (r_crest - r_root)

            r_eff = r_root + (r_val - r_root) * taper
            x = r_eff * math.cos(theta)
            y = r_eff * math.sin(theta)
            points.append([round(x, 4), round(y, 4), round(z, 4)])

    faces: list[list[int]] = []
    P = pts_per_ring
    N = total_rings

    for j in range(N - 1):
        for i in range(P):
            i_next = (i + 1) % P
            v0 = j * P + i
            v1 = (j + 1) * P + i
            v2 = (j + 1) * P + i_next
            v3 = j * P + i_next
            faces.append([v0, v3, v2])
            faces.append([v0, v2, v1])

    c_bot = N * P
    points.append([0.0, 0.0, 0.0])
    for i in range(P):
        i_next = (i + 1) % P
        faces.append([c_bot, i_next, i])

    c_top = N * P + 1
    points.append([0.0, 0.0, round(length, 4)])
    top_base = (N - 1) * P
    for i in range(P):
        i_next = (i + 1) % P
        faces.append([c_top, top_base + i, top_base + i_next])

    return points, faces


def _thread_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """ISO 公制公称螺纹生成器（支持标准外螺栓螺柱与内螺纹螺母套筒）。"""
    d = float(size[0]) if size else 8.0
    pitch = float(size[1]) if len(size) >= 2 else 1.25
    length = float(size[2]) if len(size) >= 3 else 15.0
    tol = float(size[3]) if len(size) >= 4 else 0.0
    internal = bool(size[4] > 0.5) if len(size) >= 5 else False
    cx, cy, cz = (float(center[i]) for i in range(3))

    pts, fcs = _build_thread_mesh(d, pitch, length, tolerance=tol)
    pts_str = json.dumps(pts)
    fcs_str = json.dumps(fcs)

    lines: list[str] = [
        f"  // {note}" if note else f"  // ISO metric thread: M{_fmt(d)}x{_fmt(pitch)} L={_fmt(length)} ({'internal' if internal else 'external'})"
    ]
    if internal:
        nut_d = max(d * 1.8, d + 6.0)
        lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}]) {{")
        lines.append("    difference() {")
        lines.append(f"      cylinder(d = {_fmt(nut_d)}, h = {_fmt(length)}, $fn = 6, center = false);")
        lines.append(f"      polyhedron(points = {pts_str}, faces = {fcs_str}, convexity = 10);")
        lines.append("    }")
        lines.append("  }")
    else:
        lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])")
        lines.append(f"    polyhedron(points = {pts_str}, faces = {fcs_str}, convexity = 10);")
    return "\n".join(lines)


def _build_involute_gear_polygon(
    teeth: int = 20,
    module: float = 1.5,
    pressure_angle: float = 20.0,
) -> list[list[float]]:
    """生成工业渐开线圆柱正齿轮 2D 轮廓多边形顶点。"""
    m = float(module)
    z = max(int(teeth), 4)
    alpha = math.radians(pressure_angle)

    r_pitch = (m * z) / 2.0
    r_base = r_pitch * math.cos(alpha)
    r_outer = r_pitch + 1.0 * m
    r_root = max(r_pitch - 1.25 * m, 0.5 * m)

    inv_alpha = math.tan(alpha) - alpha
    tooth_angle = 2.0 * math.pi / z
    half_tooth = tooth_angle / 4.0

    pts: list[list[float]] = []
    for tooth in range(z):
        base_angle = tooth * tooth_angle
        # 左齿廓渐开线
        for step in range(5):
            frac = step / 4.0
            r = r_root + frac * (r_outer - r_root)
            if r <= r_base:
                theta = 0.0
            else:
                u = math.sqrt((r / r_base) ** 2 - 1.0)
                theta = u - math.atan(u)
            ang = base_angle - (half_tooth + inv_alpha - theta)
            pts.append([round(r * math.cos(ang), 4), round(r * math.sin(ang), 4)])

        # 右齿廓渐开线
        for step in reversed(range(5)):
            frac = step / 4.0
            r = r_root + frac * (r_outer - r_root)
            if r <= r_base:
                theta = 0.0
            else:
                u = math.sqrt((r / r_base) ** 2 - 1.0)
                theta = u - math.atan(u)
            ang = base_angle + (half_tooth + inv_alpha - theta)
            pts.append([round(r * math.cos(ang), 4), round(r * math.sin(ang), 4)])

    return pts


def _gear_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """工业渐开线齿轮生成器（支持中心传动轴孔）。"""
    teeth = int(size[0]) if size else 20
    mod = float(size[1]) if len(size) >= 2 else 1.5
    h = float(size[2]) if len(size) >= 3 else 6.0
    bore = float(size[3]) if len(size) >= 4 else 5.0
    pa = float(size[4]) if len(size) >= 5 else 20.0
    cx, cy, cz = (float(center[i]) for i in range(3))

    pts = _build_involute_gear_polygon(teeth, mod, pa)
    pts_str = json.dumps(pts)

    lines: list[str] = [
        f"  // {note}" if note else f"  // involute gear: z={teeth} m={_fmt(mod)} h={_fmt(h)} bore={_fmt(bore)}"
    ]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}]) {{")
    if bore > 1e-4:
        lines.append("    difference() {")
        lines.append(f"      linear_extrude(height = {_fmt(h)}) {{")
        lines.append(f"        polygon(points = {pts_str});")
        lines.append("      }")
        lines.append(f"      translate([0, 0, -1])")
        lines.append(f"        cylinder(d = {_fmt(bore)}, h = {_fmt(h + 2.0)}, $fn = 32, center = false);")
        lines.append("    }")
    else:
        lines.append(f"    linear_extrude(height = {_fmt(h)}) {{")
        lines.append(f"      polygon(points = {pts_str});")
        lines.append("    }")
    lines.append("  }")
    return "\n".join(lines)


def _timing_pulley_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """GT2 标准同步轮生成器（支持齿槽、挡边、轴孔）。"""
    teeth = int(size[0]) if size else 20
    w = float(size[1]) if len(size) >= 2 else 6.0
    bore = float(size[2]) if len(size) >= 3 else 5.0
    flange = bool(size[3] > 0.5) if len(size) >= 4 else True
    pitch = float(size[4]) if len(size) >= 5 else 2.0
    cx, cy, cz = (float(center[i]) for i in range(3))

    d_pitch = (teeth * pitch) / math.pi
    d_outer = d_pitch - 0.508
    r_outer = d_outer / 2.0
    h_flange = 1.0 if flange else 0.0
    d_flange = d_outer + 3.0 if flange else d_outer
    total_h = w + 2 * h_flange
    r_groove = 0.555

    lines = [f"  // {note}" if note else f"  // GT2 timing pulley: {teeth}T w={_fmt(w)}mm bore={_fmt(bore)}mm"]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}]) {{")
    lines.append("    difference() {")
    lines.append("      union() {")
    lines.append(f"        cylinder(d = {_fmt(d_outer)}, h = {_fmt(total_h)}, $fn = 64, center = false);")
    if flange:
        lines.append(f"        cylinder(d = {_fmt(d_flange)}, h = {_fmt(h_flange)}, $fn = 64, center = false);")
        lines.append(f"        translate([0, 0, {_fmt(total_h - h_flange)}])")
        lines.append(f"          cylinder(d = {_fmt(d_flange)}, h = {_fmt(h_flange)}, $fn = 64, center = false);")
    lines.append("      }")
    # 弧形齿槽
    angle_step = 360.0 / teeth
    lines.append(f"      for (i = [0 : {teeth - 1}]) {{")
    lines.append(f"        rotate([0, 0, i * {_fmt(angle_step)}])")
    lines.append(f"          translate([{_fmt(r_outer)}, 0, {_fmt(h_flange - 0.1)}])")
    lines.append(f"            cylinder(r = {_fmt(r_groove)}, h = {_fmt(w + 0.2)}, $fn = 16, center = false);")
    lines.append("      }")
    if bore > 1e-4:
        lines.append("      translate([0, 0, -1])")
        lines.append(f"        cylinder(d = {_fmt(bore)}, h = {_fmt(total_h + 2.0)}, $fn = 32, center = false);")
    lines.append("    }")
    lines.append("  }")
    return "\n".join(lines)


def _snap_joint_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """悬臂梁卡扣生成器（免胶可拆卸卡扣与勾角）。"""
    l = float(size[0]) if size else 12.0
    w = float(size[1]) if len(size) >= 2 else 6.0
    t = float(size[2]) if len(size) >= 3 else 1.5
    h_hook = float(size[3]) if len(size) >= 4 else 1.0
    cx, cy, cz = (float(center[i]) for i in range(3))

    pts = [
        [0.0, 0.0],
        [round(t, 4), 0.0],
        [round(t, 4), round(l, 4)],
        [round(t + h_hook, 4), round(l, 4)],
        [round(t, 4), round(l + h_hook, 4)],
        [0.0, round(l + h_hook, 4)],
    ]
    pts_str = json.dumps(pts)

    lines = [f"  // {note}" if note else f"  // cantilever snap joint: L={_fmt(l)} W={_fmt(w)} T={_fmt(t)} Hook={_fmt(h_hook)}"]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy - w / 2.0)}, {_fmt(cz)}]) {{")
    lines.append("    rotate([90, 0, 90])")
    lines.append(f"      linear_extrude(height = {_fmt(w)}) {{")
    lines.append(f"        polygon(points = {pts_str});")
    lines.append("      }")
    lines.append("  }")
    return "\n".join(lines)


def _magnet_pocket_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """永磁体沉孔底座生成器（作为实体独立部件时的安装座）。"""
    d = float(size[0]) if size else 6.0
    depth = float(size[1]) if len(size) >= 2 else 3.0
    tol = float(size[2]) if len(size) >= 3 else 0.2
    eject = float(size[3]) if len(size) >= 4 else 1.5
    cx, cy, cz = (float(center[i]) for i in range(3))
    eff_d = d + tol
    outer_d = eff_d + 4.0
    h_block = depth + 2.0

    lines = [f"  // {note}" if note else f"  // magnet pocket base: d={_fmt(d)}mm depth={_fmt(depth)}mm"]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}]) {{")
    lines.append("    difference() {")
    lines.append(f"      cylinder(d = {_fmt(outer_d)}, h = {_fmt(h_block)}, $fn = 32, center = false);")
    lines.append(f"      translate([0, 0, {_fmt(h_block - depth)}]) {{")
    lines.append(f"        cylinder(d = {_fmt(eff_d)}, h = {_fmt(depth + 0.1)}, $fn = 32, center = false);")
    lines.append(f"        translate([0, 0, {_fmt(max(depth - 0.4, 0))}])")
    lines.append(f"          cylinder(d1 = {_fmt(eff_d)}, d2 = {_fmt(eff_d + 0.8)}, h = 0.5, $fn = 32, center = false);")
    lines.append("      }")
    if eject > 1e-4:
        lines.append("      translate([0, 0, -1])")
        lines.append(f"        cylinder(d = {_fmt(eject)}, h = {_fmt(h_block + 2.0)}, $fn = 16, center = false);")
    lines.append("    }")
    lines.append("  }")
    return "\n".join(lines)


PRIMITIVE_BUILDERS = {
    "cube": _cube_code,
    "cuboid": _cube_code,        # LLM 常写 cuboid，等价 cube
    "box": _cube_code,
    "cylinder": _cylinder_code,
    "sphere": _sphere_code,
    "capsule": _capsule_code,
    "polyhedron": _polyhedron_code,
    "thread": _thread_code,
    "metric_thread": _thread_code,
    "gear": _gear_code,
    "involute_gear": _gear_code,
    "timing_pulley": _timing_pulley_code,
    "gt2_pulley": _timing_pulley_code,
    "snap_joint": _snap_joint_code,
    "magnet_pocket": _magnet_pocket_code,
}


def _normalize_size(prim: str, part: dict[str, Any]) -> list[float]:
    """委托 spec.normalize_size（唯一权威实现）。"""
    return _spec_norm_size(prim, part)


def _normalize_hole_size(prim: str, hole: dict[str, Any]) -> list[float]:
    """委托 spec.normalize_hole_size。"""
    return _spec_norm_hole(prim, hole)


def _linear_extrude_code(part: dict[str, Any], center: list[float], note: str) -> list[str]:
    """2D 多边形草图直拉伸生成器（支持扭转 twist 与锥度 scale）。"""
    h = float(part.get("height", 10.0))
    twist = float(part.get("twist", 0.0))
    scale = float(part.get("scale", 1.0))
    poly = part.get("polygon")
    cx, cy, cz = (float(center[i]) for i in range(3))

    params = [f"height = {_fmt(h)}"]
    if abs(twist) > 1e-4:
        params.append(f"twist = {_fmt(twist)}")
    if abs(scale - 1.0) > 1e-4:
        params.append(f"scale = {_fmt(scale)}")

    lines = [f"  // {note}" if note else "  // linear_extrude"]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])")
    lines.append(f"    linear_extrude({', '.join(params)}) {{")
    if poly and len(poly) >= 3:
        pts = ", ".join(f"[{_fmt(p[0])}, {_fmt(p[1])}]" for p in poly)
        lines.append(f"      polygon(points = [{pts}]);")
    else:
        lines.append("      square([10, 10], center = true);")
    lines.append("    }")
    return lines


def _rotate_extrude_code(part: dict[str, Any], center: list[float], note: str) -> list[str]:
    """2D 截面回转体生成器（生成花瓶、圆环、带轮等回转几何）。"""
    angle = float(part.get("angle", 360.0))
    poly = part.get("polygon")
    cx, cy, cz = (float(center[i]) for i in range(3))

    lines = [f"  // {note}" if note else "  // rotate_extrude"]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])")
    if abs(angle - 360.0) < 1e-4:
        lines.append("    rotate_extrude() {")
    else:
        lines.append(f"    rotate_extrude(angle = {_fmt(angle)}) {{")
    if poly and len(poly) >= 3:
        pts = ", ".join(f"[{_fmt(max(p[0], 0.0))}, {_fmt(p[1])}]" for p in poly)
        lines.append(f"      polygon(points = [{pts}]);")
    else:
        lines.append("      translate([10, 0]) circle(r = 5);")
    lines.append("    }")
    return lines


def _text_code(part: dict[str, Any], center: list[float], note: str) -> list[str]:
    """3D 浮雕与铭牌文字生成器。"""
    raw_text = str(part.get("text", part.get("content", "CAD"))).replace('"', '\\"')
    font_size = float(part.get("font_size", 10.0))
    h = float(part.get("height", 3.0))
    cx, cy, cz = (float(center[i]) for i in range(3))
    font_str = f', font = "{part.get("font")}"' if part.get("font") else ""

    lines = [f"  // {note}" if note else f"  // 3D text: \"{raw_text}\""]
    lines.append(f"  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])")
    # OpenSCAD 2021.01: text() 的 height 参数在老版本中未注册为有效形参 (会报
    # "variable height not specified as parameter" 并降级为 2D 对象)，
    # 因此 3D 浮雕必须用 linear_extrude(height) 包裹 2D text() 路径轮廓。
    lines.append(f"    linear_extrude(height = {_fmt(h)}) {{")
    lines.append(f'      text("{raw_text}", size = {_fmt(font_size)}, halign = "center", valign = "center"{font_str});')
    lines.append("    }")
    return lines


def _apply_array_wrapper(lines: list[str], item: dict[str, Any], indent: str = "") -> list[str]:
    """对几何行列表应用环形均布 (radial_array) 或线性等距阵列 (linear_array)。"""
    radial = item.get("radial_array")
    linear = item.get("linear_array")
    if radial:
        count = max(int(radial.get("count", 4)), 1)
        radius = float(radial.get("radius", 20.0))
        angle_step = 360.0 / count
        out = [f"{indent}  // 环形均布阵列 (PCD 数量: {count}, 半径: {_fmt(radius)}mm)"]
        out.append(f"{indent}  for (i = [0 : {count - 1}]) {{")
        out.append(f"{indent}    rotate([0, 0, i * {_fmt(angle_step)}])")
        out.append(f"{indent}      translate([{_fmt(radius)}, 0, 0])")
        for l in lines:
            out.append(f"  {l}" if l.strip() else l)
        out.append(f"{indent}  }}")
        return out
    elif linear:
        count = max(int(linear.get("count", 3)), 1)
        step = linear.get("step", [10, 0, 0])
        sx, sy, sz = float(step[0]), float(step[1]), float(step[2])
        out = [f"{indent}  // 线性等距阵列 (数量: {count}, 步长: [{_fmt(sx)}, {_fmt(sy)}, {_fmt(sz)}])"]
        out.append(f"{indent}  for (i = [0 : {count - 1}]) {{")
        out.append(f"{indent}    translate([i * {_fmt(sx)}, i * {_fmt(sy)}, i * {_fmt(sz)}])")
        for l in lines:
            out.append(f"  {l}" if l.strip() else l)
        out.append(f"{indent}  }}")
        return out
    return lines


def _counterbore_code(hole: dict[str, Any], center: list[float]) -> list[str]:
    """生成工业标准公制阶梯沉头螺栓孔 (M2~M8) 代码。"""
    from .spec import METRIC_FASTENERS
    std = str(hole.get("standard", "M3")).upper()
    d_screw, d_head, h_head = METRIC_FASTENERS.get(std, (3.4, 6.2, 3.2))
    size = hole.get("size") or []
    h = float(hole.get("height", size[1] if len(size) >= 2 else 10.0))
    cx, cy, cz = (float(center[i]) for i in range(3))

    lines = [f"      // ISO/DIN {std} 阶梯沉头螺栓孔 (通孔d={d_screw}mm, 沉孔d={d_head}mm 深={h_head}mm)"]
    lines.append(f"      translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz - 0.5)}]) {{")
    lines.append(f"        cylinder(h = {_fmt(h + 1.0)}, d = {_fmt(d_screw)}, center = false);")
    lines.append(f"        translate([0, 0, {_fmt(max(h + 0.5 - h_head, 0))}])")
    lines.append(f"          cylinder(h = {_fmt(h_head + 1.0)}, d = {_fmt(d_head)}, center = false);")
    lines.append("      }")
    return lines


def _magnet_pocket_hole_code(hole: dict[str, Any], center: list[float]) -> list[str]:
    """生成永磁体压配沉孔与背面退磁顶针孔代码。"""
    size = hole.get("size") or [6.0, 3.0, 0.2, 1.5]
    d = float(size[0]) if len(size) >= 1 else 6.0
    depth = float(size[1]) if len(size) >= 2 else 3.0
    tol = float(size[2]) if len(size) >= 3 else 0.2
    eject = float(size[3]) if len(size) >= 4 else 1.5
    cx, cy, cz = (float(center[i]) for i in range(3))
    eff_d = d + tol
    lines = [f"      // 永磁体装配沉孔: d={_fmt(d)}mm(实孔{_fmt(eff_d)}mm) 深={_fmt(depth)}mm, 顶针孔d={_fmt(eject)}mm"]
    lines.append(f"      translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}]) {{")
    lines.append(f"        cylinder(d = {_fmt(eff_d)}, h = {_fmt(depth + 0.1)}, $fn = 32, center = false);")
    lines.append(f"        translate([0, 0, {_fmt(max(depth - 0.4, 0))}])")
    lines.append(f"          cylinder(d1 = {_fmt(eff_d)}, d2 = {_fmt(eff_d + 0.8)}, h = 0.5, $fn = 32, center = false);")
    if eject > 1e-4:
        lines.append(f"        translate([0, 0, -10])")
        lines.append(f"          cylinder(d = {_fmt(eject)}, h = {_fmt(depth + 10.2)}, $fn = 16, center = false);")
    lines.append("      }")
    return lines


def _hole_body_with_epsilon(prim: str, size: list[float], center: list[float], epsilon: float = 0.05) -> list[str]:
    """按绝对坐标生成带防共面微延展保护的挖除实体，彻底杜绝 CGAL 差集零厚度面与非流形坏边。"""
    cx, cy, cz = (float(center[i]) if i < len(center) else 0.0 for i in range(3))
    p = (prim or "cube").lower()
    if p in ("cylinder", "cone"):
        d = float(size[0]) if len(size) >= 1 else 10.0
        h = float(size[1]) if len(size) >= 2 else 10.0
        r = d / 2.0
        return [
            f"translate([{_fmt(cx - r)}, {_fmt(cy - r)}, {_fmt(cz - epsilon)}])",
            f"  cylinder(h = {_fmt(h + 2 * epsilon)}, d = {_fmt(d)}, center = false);",
        ]
    elif p in ("cube", "cuboid", "box"):
        x = float(size[0]) if len(size) >= 1 else 10.0
        y = float(size[1]) if len(size) >= 2 else 10.0
        z = float(size[2]) if len(size) >= 3 else 10.0
        return [
            f"translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz - epsilon)}])",
            f"  cube([{_fmt(x)}, {_fmt(y)}, {_fmt(z + 2 * epsilon)}], center = false);",
        ]
    elif p == "sphere":
        d = float(size[0]) if len(size) >= 1 else 10.0
        return [
            f"translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])",
            f"  sphere(d = {_fmt(d + 2 * epsilon)});",
        ]
    else:
        return _part_body(prim, size, center, "hole")


def _format_customizer_param(name: str, val: Any, label: str = "") -> list[str]:
    """将参数转换为带 Customizer 注释与合理滑块边界的 OpenSCAD 变量声明。"""
    safe_k = "".join(c if c.isalnum() or c == "_" else "_" for c in str(name))
    if not safe_k or safe_k[0].isdigit():
        safe_k = f"p_{safe_k}"
    lines: list[str] = []
    if label:
        lines.append(f"// {label}")
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        f = float(val)
        step = 0.1 if f < 2 else (0.5 if f <= 10 else 1.0)
        min_v = max(step, round(f * 0.2, 1))
        max_v = max(min_v + step, round(f * 3.0, 1))
        lines.append(f"{safe_k} = {_fmt(f)}; // [{_fmt(min_v)}:{_fmt(step)}:{_fmt(max_v)}]")
    elif isinstance(val, bool):
        lines.append(f"{safe_k} = {'true' if val else 'false'};")
    elif isinstance(val, str):
        lines.append(f'{safe_k} = "{val}";')
    elif isinstance(val, (list, tuple)):
        vals = ", ".join(_fmt(float(x)) if isinstance(x, (int, float)) else str(x) for x in val)
        lines.append(f"{safe_k} = [{vals}];")
    return lines


def _hole_code(hole: dict[str, Any], epsilon: float = 0.05) -> list[str]:
    """生成单个挖除实体的代码行（缩进对齐 difference() 内部，支持阶梯沉头孔、强磁沉孔、防共面微延展与阵列）。"""
    prim = hole.get("primitive", "cube")
    center = hole.get("center", [0, 0, 0])
    if prim == "counterbore" or hole.get("standard"):
        lines = _counterbore_code(hole, center)
    elif prim in ("magnet_pocket", "magnet_hole"):
        lines = _magnet_pocket_hole_code(hole, center)
    else:
        size = _normalize_hole_size(prim, hole)
        body = _hole_body_with_epsilon(prim, size, center, epsilon=epsilon)
        lines = [f"    // hole: {prim} {size} @ {center} (防共面 eps={_fmt(epsilon)}mm)"]
        lines += [f"      {l}" if l else l for l in body]
    return _apply_array_wrapper(lines, hole, indent="    ")


def _color_wrapper(color: Any) -> tuple[str, str]:
    """返回 (color_open, color_close) 供 OpenSCAD 代码包裹。若无 color 则返回 ('', '')。"""
    if not color:
        return "", ""
    if isinstance(color, str):
        c = color.strip().strip("'\"")
        if c:
            return f"  color(\"{c}\") {{", "  }"
    elif isinstance(color, (list, tuple)) and len(color) >= 3:
        vals = ", ".join(_fmt(v) for v in color[:4])
        return f"  color([{vals}]) {{", "  }"
    return "", ""


def _sanitize_module_name(raw: Any, fallback: str = "part") -> str:
    """把 LLM 输出的部件 id 消毒为合法的 OpenSCAD 模块标识符。"""
    s = str(raw).strip()
    if not s:
        return fallback
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in s)
    if not safe or safe[0].isdigit():
        safe = f"p_{safe}"
    return safe


def _part_module(name: str, part: dict[str, Any]) -> list[str]:
    """生成一个 part 对应的 OpenSCAD 模块定义。

    支持 color 视觉颜色包裹，rotation 绕部件自身中心旋转。
    模块名经过 _sanitize_module_name 消毒，避免 LLM 输出含空格/中文导致编译失败。
    """
    part_name = _sanitize_module_name(part.get("id", name), fallback=name)
    prim = part.get("primitive", "cube")
    center = part.get("center", [0, 0, 0])
    rotation = part.get("rotation") or [0, 0, 0]
    holes = part.get("holes") or []
    note = part.get("note", "")
    color = part.get("color")

    # 规范化尺寸：兼容 size=[..]、size=标量、radius/height、cuboid/box 等多种 LLM 输出
    size = _normalize_size(prim, part)

    # 拓扑轻量化与功能晶格 (Lattice & Infill Structure)
    lat_info = part.get("lattice")
    lattice_header_lines: list[str] = []
    mod_lat: Optional[str] = None
    if lat_info and isinstance(lat_info, dict):
        from .lattice import (
            generate_honeycomb_scad,
            generate_orthogrid_scad,
            generate_diamond_lattice_scad,
            generate_gyroid_infill_scad,
        )
        lat_pat = str(lat_info.get("pattern", "honeycomb")).lower()
        lat_cell = float(lat_info.get("cell_size", 8.0))
        lat_wall = float(lat_info.get("wall_thickness", 1.2))
        lx = float(size[0]) if len(size) >= 1 else 20.0
        ly = float(size[1]) if len(size) >= 2 else 20.0
        lz = float(size[2]) if len(size) >= 3 else 10.0
        mod_lat = f"{part_name}_lattice"
        if "honey" in lat_pat or "hex" in lat_pat:
            l_code = generate_honeycomb_scad(lx, ly, lz, cell_size=lat_cell, wall_thickness=lat_wall, module_name=mod_lat)
        elif "ortho" in lat_pat or "grid" in lat_pat:
            l_code = generate_orthogrid_scad(lx, ly, lz, cell_size=lat_cell, rib_thickness=lat_wall, module_name=mod_lat)
        elif "diamond" in lat_pat or "octet" in lat_pat:
            l_code = generate_diamond_lattice_scad(lx, ly, lz, cell_size=lat_cell, strut_radius=lat_wall/2.0, module_name=mod_lat)
        elif "gyroid" in lat_pat:
            l_code = generate_gyroid_infill_scad(lx, ly, lz, cell_size=lat_cell, wall_thickness=lat_wall, module_name=mod_lat)
        else:
            l_code = generate_honeycomb_scad(lx, ly, lz, cell_size=lat_cell, wall_thickness=lat_wall, module_name=mod_lat)
        lattice_header_lines = l_code.split("\n") + [""]

    lines: list[str] = [f"module {part_name}() {{"]

    c_open, c_close = _color_wrapper(color)
    indent = "  " if c_open else ""
    if c_open:
        lines.append(c_open)

    # polyhedron 带顶点数据 → 生成真正的 polyhedron()；否则走通用 builder
    if prim == "polyhedron" and _has_polyhedron_vertices(part):
        poly_body = _polyhedron_vertices(part, center, note)
        rx, ry, rz = (float(rotation[i]) for i in range(3))
        if any(abs(v) > 1e-6 for v in (rx, ry, rz)):
            lines.append(f"{indent}  translate([{_fmt(float(center[0]))}, {_fmt(float(center[1]))}, {_fmt(float(center[2]))}])")
            lines.append(f"{indent}    rotate([{_fmt(rx)}, {_fmt(ry)}, {_fmt(rz)}])")
            for l in poly_body:
                lines.append(f"{indent}      {l}" if l.strip() else l)
        else:
            for l in poly_body:
                lines.append(f"{indent}{l}" if l.strip() else l)
        if c_close:
            lines.append(c_close)
        lines.append("}")
        return lines

    # linear_extrude 直拉伸, rotate_extrude 回转体 与 text 浮雕文字
    if prim in ("linear_extrude", "rotate_extrude", "text"):
        if prim == "linear_extrude":
            ext_body = _linear_extrude_code(part, center, note)
        elif prim == "rotate_extrude":
            ext_body = _rotate_extrude_code(part, center, note)
        else:
            ext_body = _text_code(part, center, note)
        ext_body = _apply_array_wrapper(ext_body, part, indent=indent)
        rx, ry, rz = (float(rotation[i]) for i in range(3))
        if any(abs(v) > 1e-6 for v in (rx, ry, rz)):
            lines.append(f"{indent}  translate([{_fmt(float(center[0]))}, {_fmt(float(center[1]))}, {_fmt(float(center[2]))}])")
            lines.append(f"{indent}    rotate([{_fmt(rx)}, {_fmt(ry)}, {_fmt(rz)}])")
            for l in ext_body:
                lines.append(f"{indent}      {l}" if l.strip() else l)
        else:
            for l in ext_body:
                lines.append(f"{indent}{l}" if l.strip() else l)
        if c_close:
            lines.append(c_close)
        lines.append("}")
        return lines

    # 部件体（主体 + 孔洞）
    body: list[str] = []
    if mod_lat:
        core_body = [f"translate([{_fmt(float(center[0]))}, {_fmt(float(center[1]))}, {_fmt(float(center[2]))}])", f"  {mod_lat}();"]
    else:
        core_body = _part_body(prim, size, center, note)

    if holes:
        body.append(f"{indent}  difference() {{")
        body += [f"{indent}    {l}" if l else l for l in core_body]
        for hole in holes:
            body += [f"{indent}{l}" if l else l for l in _hole_code(hole)]
        body.append(f"{indent}  }}")
    else:
        body += [f"{indent}  {l}" if l else l for l in core_body]

    cx, cy, cz = (float(center[i]) for i in range(3))
    rx, ry, rz = (float(rotation[i]) for i in range(3))
    has_rot = any(abs(v) > 1e-6 for v in (rx, ry, rz))

    if has_rot:
        # 绕部件中心旋转：translate 到中心 → rotate → 内部几何以中心为原点生成
        inner = [f"{indent}  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])",
                 f"{indent}    rotate([{_fmt(rx)}, {_fmt(ry)}, {_fmt(rz)}])"]
        if mod_lat:
            inner += [f"{indent}      {mod_lat}();"]
        else:
            inner += [f"{indent}      {l}" if l else l for l in _part_body_rel(prim, size, note)]
        for hole in holes:
            inner += [f"{indent}{l}" if l else l for l in _hole_code_rel(hole, center)]
    else:
        inner = body

    # 阵列复制 (Array: radial / linear)
    inner = _apply_array_wrapper(inner, part, indent=indent)

    # 镜像与对称 (Symmetry & Mirror)
    if part.get("symmetric"):
        lines.append(f"{indent}  union() {{")
        lines += [f"{indent}    {l}" if l else l for l in inner]
        lines.append(f"{indent}    mirror([1, 0, 0])")
        lines += [f"{indent}      {l}" if l else l for l in inner]
        lines.append(f"{indent}  }}")
    elif part.get("mirror"):
        m = part["mirror"]
        lines.append(f"{indent}  mirror([{_fmt(m[0])}, {_fmt(m[1])}, {_fmt(m[2])}]) {{")
        lines += [f"{indent}    {l}" if l else l for l in inner]
        lines.append(f"{indent}  }}")
    else:
        lines += inner

    if c_close:
        lines.append(c_close)
    lines.append("}")
    return lattice_header_lines + lines


def _part_body(prim: str, size: list[float], center: list[float], note: str) -> list[str]:
    """按绝对坐标（center）生成几何体。"""
    builder = PRIMITIVE_BUILDERS.get(prim, _cube_code)
    return builder(size, center, [], note).split("\n")


def _part_body_rel(prim: str, size: list[float], note: str) -> list[str]:
    """按相对坐标（中心为原点）生成几何体，用于 rotate 包裹。"""
    builder = PRIMITIVE_BUILDERS.get(prim, _cube_code)
    return builder(size, [0, 0, 0], [], note).split("\n")


def _hole_code_rel(hole: dict[str, Any], part_center: list[float], epsilon: float = 0.05) -> list[str]:
    """相对坐标版本的孔洞（孔洞中心减去部件中心，配合外层 translate+rotate，附带防共面微延展）。"""
    prim = hole.get("primitive", "cube")
    size = _normalize_hole_size(prim, hole)
    hc = hole.get("center", [0, 0, 0])
    rel_center = [float(hc[i]) - float(part_center[i]) for i in range(3)]
    body = _hole_body_with_epsilon(prim, size, rel_center, epsilon=epsilon)
    out: list[str] = [f"        // hole: {prim} rel{rel_center} (防共面 eps={_fmt(epsilon)}mm)"]
    for line in body:
        s = line.strip()
        if s.startswith("//"):
            continue
        out.append(f"        {s}")
    return out


def _normalize_hole_size(prim: str, hole: dict[str, Any]) -> list[float]:
    """hole 的尺寸规范化（同 part，但默认半径 5 / 高度 10）。"""
    return _normalize_size(prim, hole)


def _estimate_part_min_z(part: dict[str, Any]) -> float:
    """计算单个部件在世界坐标下的 Z 轴最低点（用于自动贴合 Z=0 底板）。

    委托 doctor._get_part_aabb 计算完整 3D Euler 旋转包围盒，确保倾斜或多轴旋转零件贴合底板精确无漂移。
    """
    aabb = _get_part_aabb(part)
    return aabb[0][2]


def spec_to_scad(
    spec: dict[str, Any],
    config: AgentConfig | None = None,
    fn: int = 0,
    ground_z: bool = True,
) -> str:
    """规格 dict → 完整 OpenSCAD 源码字符串。

    fn: 圆柱/球体分段精度 ($fn)；0 表示不注入（使用 OpenSCAD 默认）。
    非 0 时在文件头注入 $fn=..., 控制曲面体平滑度。
    """
    spec, _ = resolve_geometric_constraints(spec)
    model_name = spec.get("name", "model")
    safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in model_name)
    description = spec.get("description", "")
    params = spec.get("params") or {}
    parts = spec.get("parts", [])
    assembly = spec.get("assembly", {})
    tips = spec.get("print_tips") or []

    out: list[str] = []
    out.append(f"// ============================================================")
    out.append(f"// {model_name} - {description}")
    out.append(f"// 由 OpenSCAD Agent 自动生成: 图片识别 -> 3D 模型")
    out.append(f"// 单位: mm | 生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if fn:
        out.append(f"$fn = {fn};  // 圆柱/球体分段精度")
    out.append("epsilon = 0.05;  // 防共面自交微增量，杜绝 CGAL 差集零厚度与非流形坏边")
    if params:
        out.append(f"// params: {json.dumps(params, ensure_ascii=False)}")
    out.append(f"// ============================================================")
    out.append("")

    # 自定义参数声明（支持 OpenSCAD Customizer 参数控件与滑块联动）
    out.append("// ---------------- 参数 (OpenSCAD Customizer) ----------------")
    out.append("/* [Parameters] */")
    if params:
        for k, v in params.items():
            out += _format_customizer_param(k, v)
    else:
        # 自动提取主要部件尺寸为参数变量 (Parameter Hoisting)
        for p in parts[:4]:
            pid = _sanitize_module_name(p.get("id", p.get("primitive", "part")), fallback="part")
            prim = p.get("primitive", "cube")
            sz = _normalize_size(prim, p)
            if prim in ("cube", "cuboid", "box") and len(sz) >= 3:
                out += _format_customizer_param(f"{pid}_x", sz[0], f"{pid} 长度 (X)")
                out += _format_customizer_param(f"{pid}_y", sz[1], f"{pid} 宽度 (Y)")
                out += _format_customizer_param(f"{pid}_z", sz[2], f"{pid} 高度 (Z)")
            elif prim in ("cylinder", "cone") and len(sz) >= 2:
                out += _format_customizer_param(f"{pid}_dia", sz[0], f"{pid} 直径")
                out += _format_customizer_param(f"{pid}_height", sz[1], f"{pid} 高度")
            elif prim == "sphere" and len(sz) >= 1:
                out += _format_customizer_param(f"{pid}_dia", sz[0], f"{pid} 直径")
    out.append("")

    # 各部件模块
    out.append("// ---------------- 部件 ----------------")
    for part in parts:
        out += _part_module(part.get("id", part.get("primitive", "part")), part)
        out.append("")

    # 装配
    out.append("// ---------------- 装配 ----------------")
    union_ids: list[str] = assembly.get("union_parts") or []
    intersection_ids: list[str] = assembly.get("intersection_parts") or []
    difference_ids: list[str] = assembly.get("difference_parts") or []
    difference_from = assembly.get("difference_from")
    if not union_ids and not intersection_ids:
        union_ids = [p.get("id", p.get("primitive")) for p in parts]

    out.append(f"// assembly: {assembly.get('description', 'CSG assembly of parts')}")

    # 计算各部件几何中心作为装配体展开基准 (Exploded View Vector)
    part_map = {_sanitize_module_name(p.get("id", ""), fallback="part"): p for p in parts}
    centers = [p.get("center", [0, 0, 0]) for p in parts]
    if centers:
        avg_cx = sum(float(c[0]) if len(c) >= 1 else 0.0 for c in centers) / len(centers)
        avg_cy = sum(float(c[1]) if len(c) >= 2 else 0.0 for c in centers) / len(centers)
        avg_cz = sum(float(c[2]) if len(c) >= 3 else 0.0 for c in centers) / len(centers)
    else:
        avg_cx, avg_cy, avg_cz = 0.0, 0.0, 0.0

    out.append("/* [Assembly Explode] */")
    out.append("// 爆炸图分解间距比例 (0 为闭合装配状态，大于0向外展开内部零件)")
    out.append("explode = 0.0; // [0:0.1:3]")
    out.append("")

    # 机构运动学铰链约束与动画参数 (Kinematic Joints & Animation)
    from .kinematics import parse_joints, generate_kinematic_scad_blocks
    kin_joints = parse_joints(spec)
    kin_headers, kin_wrappers = generate_kinematic_scad_blocks(kin_joints)
    if kin_headers:
        out += kin_headers

    def _render_part_call(pid: str, idx: int, total_count: int, indent: str = "    ") -> str:
        pid_safe = _sanitize_module_name(pid, fallback="part")
        p_obj = part_map.get(pid_safe)
        kin_wrap = kin_wrappers.get(pid_safe) or kin_wrappers.get(pid)
        core_call = f"{pid_safe}();"
        if kin_wrap:
            pre, suf = kin_wrap
            core_call = f"{pre}{core_call}{suf}"

        if p_obj and total_count > 1:
            p_center = p_obj.get("center", [0, 0, 0])
            pcx = float(p_center[0]) if len(p_center) >= 1 else 0.0
            pcy = float(p_center[1]) if len(p_center) >= 2 else 0.0
            pcz = float(p_center[2]) if len(p_center) >= 3 else 0.0
            dx = pcx - avg_cx
            dy = pcy - avg_cy
            dz = pcz - avg_cz
            if abs(dx) < 1e-3 and abs(dy) < 1e-3 and abs(dz) < 1e-3:
                dz = (idx - (total_count - 1) / 2.0) * 20.0
            return f"{indent}translate(explode * [{_fmt(dx)}, {_fmt(dy)}, {_fmt(dz)}])\n{indent}  {core_call}"
        return f"{indent}{core_call}"

    # 顶层模型模块
    out.append(f"module {safe_name}() {{")

    # 是否存在减除部分 (difference_parts 或 difference_from)
    has_subtraction = bool(difference_ids or difference_from)
    top_indent = "  " if not has_subtraction else "    "

    if has_subtraction:
        out.append("  difference() {")

    # 构建主体正向几何 (positive geometry)
    if difference_from:
        base_safe = _sanitize_module_name(difference_from, fallback="part")
        out.append(f"{top_indent}// 基准部件 (Base Part)")
        out.append(f"{top_indent}{base_safe}();")
    else:
        both_ops = bool(union_ids and intersection_ids)
        if both_ops or len(union_ids) > 1:
            out.append(f"{top_indent}union() {{")
            for idx, pid in enumerate(union_ids):
                out.append(_render_part_call(pid, idx, len(union_ids), indent=top_indent + "  "))
            if intersection_ids:
                out.append(f"{top_indent}  intersection() {{")
                for idx, pid in enumerate(intersection_ids):
                    out.append(_render_part_call(pid, idx, len(intersection_ids), indent=top_indent + "    "))
                out.append(f"{top_indent}  }}")
            out.append(f"{top_indent}}}")
        elif intersection_ids and not union_ids:
            out.append(f"{top_indent}intersection() {{")
            for idx, pid in enumerate(intersection_ids):
                out.append(_render_part_call(pid, idx, len(intersection_ids), indent=top_indent + "  "))
            out.append(f"{top_indent}}}")
        elif union_ids:
            for idx, pid in enumerate(union_ids):
                out.append(_render_part_call(pid, idx, len(union_ids), indent=top_indent))
        else:
            out.append(f"{top_indent}// empty assembly")

    # 构建减除负向几何 (negative geometry)
    if has_subtraction:
        if difference_from and not difference_ids:
            subtract_ids = [p for p in union_ids if str(p) != str(difference_from)]
        else:
            subtract_ids = difference_ids

        out.append("    // 减除部件 (Subtracted Parts)")
        if len(subtract_ids) > 1:
            out.append("    union() {")
            for idx, pid in enumerate(subtract_ids):
                out.append(_render_part_call(pid, idx, len(subtract_ids), indent="      "))
            out.append("    }")
        elif subtract_ids:
            out.append(_render_part_call(subtract_ids[0], 0, 1, indent="    "))
        out.append("  }")

    out.append("}")
    out.append("")

    final_module = safe_name

    # 自动接地贴合计算 (Z=0)
    all_min_z = min((_estimate_part_min_z(p) for p in parts), default=0.0) if ground_z else 0.0

    out.append("// ---------------- 顶层入口 ----------------")
    if ground_z and abs(all_min_z) > 1e-4:
        shift_z = -all_min_z
        out.append(f"// 自动接地贴合: 整体平移使底部平贴 Z=0 打印底板 (最低点偏移 {_fmt(all_min_z)}mm)")
        out.append(f"translate([0, 0, {_fmt(shift_z)}])")
        out.append(f"  {final_module}();")
    else:
        out.append(f"{final_module}();")
    out.append("")

    # 3D 打印适印性诊断 (Printability Doctor)
    mat = getattr(config, "material", "PLA") if config else "PLA"
    doc = check_printability(spec, material=mat)
    out.append("// ---------------- 3D 打印切片自检报告 (Printability Doctor) ----------------")
    out.append(f"// {doc.summary()}")
    out.append(f"// 包围盒尺寸: {doc.bounding_box[0]} × {doc.bounding_box[1]} × {doc.bounding_box[2]} mm")
    if doc.warnings:
        out.append("// 适印性风险预警:")
        for w in doc.warnings:
            out.append(f"//   ⚠️ {w}")
    all_tips = (doc.tips or []) + (tips or [])
    if all_tips:
        out.append("// 切片与工艺建议:")
        for t in all_tips:
            out.append(f"//   💡 {t}")
    out.append("")

    return "\n".join(out)


def write_spec_and_scad(
    spec: dict[str, Any],
    config: AgentConfig,
    tag: str = "model",
) -> tuple[str, str, str]:
    """写 spec.json + model.scad 到 config.output_dir，返回 (spec_path, scad_path, model_name)。

    写盘前先经 spec.validate_spec 规范化/校验，保证生成的 .scad 可编译。
    """
    out_dir = Path(config.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # 规范化 + 校验（补全缺省/修正类型/去重 id/过滤未知 union）
    spec, issues = validate_spec(spec)
    for iss in issues:
        _log.warning("[spec] %s", iss)
    model_name = spec.get("name", tag)
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in model_name)

    spec_path = out_dir / f"{safe}_spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    ground_z = getattr(config, "ground_z", True) if config else True
    scad_src = spec_to_scad(
        spec, config,
        fn=config.fn_value() if config else 0,
        ground_z=ground_z,
    )
    scad_path = out_dir / f"{safe}.scad"
    scad_path.write_text(scad_src, encoding="utf-8")
    _log.info("[gen] 规格已写入: %s", spec_path)
    _log.info("[gen] OpenSCAD 源码已写入: %s", scad_path)
    return str(spec_path), str(scad_path), safe


# ---------------------------------------------------------------------------
# 参数化增量修剪与热重载引擎 (Spec Patch & Hot-Reload)
# ---------------------------------------------------------------------------

def patch_spec(base_spec: dict[str, Any], patch: dict[str, Any] | str) -> dict[str, Any]:
    """对基础 CAD 模型规格 JSON 应用结构化增量补丁或自然语言修剪指令。

    支持：
    1. 结构化字典补丁 (Dict Patch):
       - modify_parts: [{"id": "base", "size": [10, 20, 30], "holes": ...}]
       - add_parts: [新零件对象]
       - remove_parts: ["part_id_to_remove"]
       - scale: float 或 [sx, sy, sz]
       - params: {"wall_thickness": 3.0}
    2. 自然语言/启发式文本指令 (Text Rule Patch):
       - "高度+10" / "高度增加 15mm" / "height + 10" / "拉伸高度到 35"
       - "孔径 5mm" / "沉孔扩大 1mm" / "孔径改为 4"
       - "倒角 2mm" / "chamfer 2"
       - "壁厚 3mm" / "wall_thickness 3"
       - "缩放 1.5" / "scale 1.2"
    """
    import copy
    import re

    spec = copy.deepcopy(base_spec)
    parts = spec.setdefault("parts", [])

    if isinstance(patch, str):
        patch_str = patch.strip()

        # 1. 等比缩放: "缩放 1.5" 或 "scale 1.2"
        m_scale = re.search(r"(?:缩放|scale|等比缩放)\s*[:=]?\s*([0-9.]+)", patch_str, re.IGNORECASE)
        if m_scale:
            s_val = float(m_scale.group(1))
            for p in parts:
                if "size" in p and isinstance(p["size"], list):
                    p["size"] = [round(float(v) * s_val, 3) for v in p["size"]]
                if "center" in p and isinstance(p["center"], list):
                    p["center"] = [round(float(v) * s_val, 3) for v in p["center"]]

        # 2. 高度调整: "高度+10", "高度增加 15", "height + 10", "拉伸高度到 35", "高度改为 40"
        m_h_add = re.search(r"(?:高度|高|height|z轴?)\s*(?:\+|增加|拉长)\s*([0-9.]+)", patch_str, re.IGNORECASE)
        m_h_set = re.search(r"(?:高度|高|height|z轴?)\s*(?:改为|设为|变为|拉伸到|到|=)\s*([0-9.]+)", patch_str, re.IGNORECASE)
        if m_h_add:
            delta = float(m_h_add.group(1))
            for p in parts:
                sz = p.get("size", [])
                if len(sz) >= 3 and p.get("primitive") in ("cube", "chamfer_cube", "rounded_cube"):
                    sz[2] = round(float(sz[2]) + delta, 3)
                elif len(sz) >= 2 and p.get("primitive") in ("cylinder", "chamfer_cylinder", "thread"):
                    sz[1] = round(float(sz[1]) + delta, 3)
        elif m_h_set:
            new_h = float(m_h_set.group(1))
            for p in parts:
                sz = p.get("size", [])
                if len(sz) >= 3 and p.get("primitive") in ("cube", "chamfer_cube", "rounded_cube"):
                    sz[2] = new_h
                    break
                elif len(sz) >= 2 and p.get("primitive") in ("cylinder", "chamfer_cylinder", "thread"):
                    sz[1] = new_h
                    break

        # 3. 孔径调整: "孔径 5mm", "孔径改为 4.5", "沉孔扩大 1mm", "沉孔直径 6"
        m_hole_add = re.search(r"(?:孔径|沉孔|孔)\s*(?:\+|扩大|增加)\s*([0-9.]+)", patch_str, re.IGNORECASE)
        m_hole_dia = re.search(r"(?:孔径|沉孔|孔直径|hole)\s*(?:改为|设为|变为|=|:)?\s*([0-9.]+)", patch_str, re.IGNORECASE)
        if m_hole_add:
            d_delta = float(m_hole_add.group(1))
            for p in parts:
                for h in p.get("holes", []):
                    if "size" in h and len(h["size"]) >= 1:
                        h["size"][0] = round(float(h["size"][0]) + d_delta, 3)
        elif m_hole_dia:
            new_d = float(m_hole_dia.group(1))
            for p in parts:
                for h in p.get("holes", []):
                    if "size" in h and len(h["size"]) >= 1:
                        h["size"][0] = new_d

        # 4. 倒角注入: "倒角 2mm", "chamfer 2"
        m_chamfer = re.search(r"(?:倒角|chamfer)\s*[:=]?\s*([0-9.]+)", patch_str, re.IGNORECASE)
        if m_chamfer:
            c_val = float(m_chamfer.group(1))
            for p in parts:
                p["chamfer"] = c_val

        # 5. 壁厚调整: "壁厚 3mm", "wall 2.5"
        m_wall = re.search(r"(?:壁厚|wall_thickness|wall)\s*[:=]?\s*([0-9.]+)", patch_str, re.IGNORECASE)
        if m_wall:
            w_val = float(m_wall.group(1))
            spec.setdefault("params", {})["wall_thickness"] = w_val

    elif isinstance(patch, dict):
        if "params" in patch:
            spec.setdefault("params", {}).update(patch["params"])
        if "name" in patch:
            spec["name"] = patch["name"]
        if "description" in patch:
            spec["description"] = patch["description"]
        if "units" in patch:
            spec["units"] = patch["units"]

        if "modify_parts" in patch:
            part_map = {p.get("id"): p for p in parts if p.get("id")}
            for mod in patch["modify_parts"]:
                pid = mod.get("id")
                if pid and pid in part_map:
                    target = part_map[pid]
                    for k, v in mod.items():
                        if k != "id":
                            target[k] = copy.deepcopy(v)

        if "add_parts" in patch:
            for new_p in patch["add_parts"]:
                parts.append(copy.deepcopy(new_p))

        if "remove_parts" in patch:
            rem_ids = set(patch["remove_parts"])
            spec["parts"] = [p for p in parts if p.get("id") not in rem_ids]

        if "scale" in patch:
            s = patch["scale"]
            if isinstance(s, (int, float)):
                sx = sy = sz = float(s)
            elif isinstance(s, (list, tuple)) and len(s) >= 3:
                sx, sy, sz = float(s[0]), float(s[1]), float(s[2])
            else:
                sx = sy = sz = 1.0

            for p in spec.get("parts", []):
                if "size" in p and isinstance(p["size"], list):
                    if len(p["size"]) >= 3:
                        p["size"] = [round(p["size"][0] * sx, 3), round(p["size"][1] * sy, 3), round(p["size"][2] * sz, 3)]
                    elif len(p["size"]) == 2:
                        p["size"] = [round(p["size"][0] * max(sx, sy), 3), round(p["size"][1] * sz, 3)]
                if "center" in p and isinstance(p["center"], list) and len(p["center"]) >= 3:
                    p["center"] = [round(p["center"][0] * sx, 3), round(p["center"][1] * sy, 3), round(p["center"][2] * sz, 3)]

    # 规范化校验
    validated_spec, _ = validate_spec(spec)
    return validated_spec


def apply_patch_to_model(
    stem_or_path: str | Path,
    patch: dict[str, Any] | str,
    out_dir: Optional[str | Path] = None,
    recompile: bool = True,
) -> tuple[Path, Path, dict[str, Any]]:
    """读取指定模型规格，应用修剪补丁并重新生成 OpenSCAD 源码与 STL。

    返回 (spec_path, scad_path, updated_spec)。
    """
    target = Path(stem_or_path)
    if target.suffix == ".json" and target.exists():
        spec_path = target
        working_dir = target.parent if out_dir is None else Path(out_dir)
        stem = target.stem.replace("_spec", "")
    else:
        working_dir = Path(out_dir).resolve() if out_dir else Path.cwd()
        stem = str(stem_or_path).replace("_spec.json", "").replace(".scad", "").replace(".stl", "")
        spec_path = working_dir / f"{stem}_spec.json"

    if not spec_path.exists():
        raise FileNotFoundError(f"找不到模型规格文件: {spec_path}")

    base_spec = json.loads(spec_path.read_text(encoding="utf-8"))
    updated = patch_spec(base_spec, patch)

    # 回写 spec.json
    spec_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")

    # 转译并回写 scad
    from .core import AgentConfig, OpenscadRenderer
    cfg = AgentConfig(output_dir=str(working_dir))
    scad_code = spec_to_scad(updated, cfg, fn=64, ground_z=True)
    scad_path = working_dir / f"{stem}.scad"
    scad_path.write_text(scad_code, encoding="utf-8")

    if recompile:
        stl_path = working_dir / f"{stem}.stl"
        renderer = OpenscadRenderer(cfg)
        ok, msg = renderer.validate(scad_path)
        if ok:
            renderer.export_model(scad_path, stl_path, force=True)

    _log.info("[geometry] 增量修剪已应用至模型: %s", stem)
    return spec_path, scad_path, updated