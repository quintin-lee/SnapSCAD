"""3D 几何生成器：把 LLM 输出的 JSON 规格转译为可编译的 OpenSCAD 源码。

设计原则：
- 每个 part 生成一个独立 OpenSCAD 模块（function），便于阅读、调试与局部修改。
- 顶层按 assembly.union_parts 做 union，再对 holes 做 difference 挖除，
  输出单一可打印实体。
- 全程使用标准 OpenSCAD 2.x CSG 语法，兼容 openscad -o 渲染。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import AgentConfig
from .spec import normalize_size as _spec_norm_size, normalize_hole_size as _spec_norm_hole, validate_spec
from .doctor import check_printability
from .log import get_logger as _log_module

_log = _log_module()


def _fmt(v: float) -> str:
    """格式化数值：整数不带小数点。"""
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _cylinder_code(size: list[float], center: list[float], holes: list[dict], note: str) -> str:
    """圆柱/圆锥生成器。
    size = [直径, 高度] 或 [底面直径d1, 顶面直径d2, 高度h] (圆锥/圆台)。
    """
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
    cx, cy, cz = (float(center[i]) for i in range(3))

    # 安全约束：倒圆角半径不能超过半宽或半深
    fillet = min(fillet, min(x, y) / 2.0 - 0.01) if fillet > 0.0 else 0.0

    if fillet > 1e-4:
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


PRIMITIVE_BUILDERS = {
    "cube": _cube_code,
    "cuboid": _cube_code,        # LLM 常写 cuboid，等价 cube
    "box": _cube_code,
    "cylinder": _cylinder_code,
    "sphere": _sphere_code,
    "capsule": _capsule_code,
    "polyhedron": _polyhedron_code,
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


def _hole_code(hole: dict[str, Any]) -> list[str]:
    """生成单个挖除实体的代码行（缩进对齐 difference() 内部，支持阶梯沉头孔与阵列）。"""
    prim = hole.get("primitive", "cube")
    center = hole.get("center", [0, 0, 0])
    if prim == "counterbore" or hole.get("standard"):
        lines = _counterbore_code(hole, center)
    else:
        size = _normalize_hole_size(prim, hole)
        body = _part_body(prim, size, center, "hole")
        lines = [f"    // hole: {prim} {size} @ {center}"]
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
    if holes:
        body.append(f"{indent}  difference() {{")
        body += [f"{indent}    {l}" if l else l for l in _part_body(prim, size, center, note)]
        for hole in holes:
            body += [f"{indent}{l}" if l else l for l in _hole_code(hole)]
        body.append(f"{indent}  }}")
    else:
        body += [f"{indent}  {l}" if l else l for l in _part_body(prim, size, center, note)]

    cx, cy, cz = (float(center[i]) for i in range(3))
    rx, ry, rz = (float(rotation[i]) for i in range(3))
    has_rot = any(abs(v) > 1e-6 for v in (rx, ry, rz))

    if has_rot:
        # 绕部件中心旋转：translate 到中心 → rotate → 内部几何以中心为原点生成
        inner = [f"{indent}  translate([{_fmt(cx)}, {_fmt(cy)}, {_fmt(cz)}])",
                 f"{indent}    rotate([{_fmt(rx)}, {_fmt(ry)}, {_fmt(rz)}])"]
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
    return lines


def _part_body(prim: str, size: list[float], center: list[float], note: str) -> list[str]:
    """按绝对坐标（center）生成几何体。"""
    builder = PRIMITIVE_BUILDERS.get(prim, _cube_code)
    return builder(size, center, [], note).split("\n")


def _part_body_rel(prim: str, size: list[float], note: str) -> list[str]:
    """按相对坐标（中心为原点）生成几何体，用于 rotate 包裹。"""
    builder = PRIMITIVE_BUILDERS.get(prim, _cube_code)
    return builder(size, [0, 0, 0], [], note).split("\n")


def _hole_code_rel(hole: dict[str, Any], part_center: list[float]) -> list[str]:
    """相对坐标版本的孔洞（孔洞中心减去部件中心，配合外层 translate+rotate）。"""
    prim = hole.get("primitive", "cube")
    size = _normalize_hole_size(prim, hole)
    hc = hole.get("center", [0, 0, 0])
    rel_center = [float(hc[i]) - float(part_center[i]) for i in range(3)]
    # 直接以 rel_center 为原点生成几何（不经过 builder 的默认 center），
    # 复用 builder 但把其 translate 目标改成 rel_center。
    builder = PRIMITIVE_BUILDERS.get(prim, _cube_code)
    body = builder(size, rel_center, [], "").split("\n")
    out: list[str] = [f"        // hole: {prim} rel{rel_center}"]
    for line in body:
        s = line.strip()
        if s.startswith("translate("):
            # 重新定位到相对坐标（builder 已按 rel_center 生成，直接用）
            out.append(f"      {s}")
        elif s.startswith("//"):
            continue
        else:
            out.append(f"        {s}")
    return out


def _normalize_hole_size(prim: str, hole: dict[str, Any]) -> list[float]:
    """hole 的尺寸规范化（同 part，但默认半径 5 / 高度 10）。"""
    return _normalize_size(prim, hole)


def _estimate_part_min_z(part: dict[str, Any]) -> float:
    """计算单个部件在世界坐标下的 Z 轴最低点（用于自动贴合 Z=0 底板）。"""
    prim = part.get("primitive", "cube")
    center = part.get("center", [0, 0, 0])
    cz = float(center[2]) if len(center) >= 3 else 0.0
    size = _normalize_size(prim, part)

    rot = part.get("rotation") or [0, 0, 0]
    rx = float(rot[0]) if len(rot) >= 1 else 0.0
    ry = float(rot[1]) if len(rot) >= 2 else 0.0
    has_tilt = abs(rx) > 1e-3 or abs(ry) > 1e-3

    if prim in ("cube", "cuboid", "box"):
        z_len = float(size[2]) if len(size) >= 3 else 10.0
        if has_tilt:
            return cz - z_len / 2.0
        return cz
    elif prim == "cylinder":
        h = float(size[-1]) if size else 10.0
        if has_tilt:
            return cz - h / 2.0
        return cz
    elif prim == "sphere" or prim == "capsule":
        r = float(size[0]) if size else 5.0
        return cz - (0 if prim == "capsule" else r)
    elif prim in ("linear_extrude", "rotate_extrude", "text"):
        # linear_extrude / rotate_extrude / text 均向上拉伸，最低点为中心 Z
        return cz
    elif prim == "polyhedron":
        pts = part.get("points") or part.get("vertices")
        if isinstance(pts, list) and pts:
            try:
                min_pt_z = min(float(p[2]) for p in pts if len(p) >= 3)
                max_abs = max((abs(float(c)) for p in pts for c in p), default=0.0)
                if max_abs <= 2.0:
                    return cz + min_pt_z
                return min_pt_z
            except (ValueError, TypeError):
                pass
        return cz
    return cz


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
    if params:
        out.append(f"// params: {json.dumps(params, ensure_ascii=False)}")
    out.append(f"// ============================================================")
    out.append("")

    # 自定义参数声明（支持 OpenSCAD Customizer 参数控件）
    if params:
        out.append("// ---------------- 参数 (OpenSCAD Customizer) ----------------")
        out.append("/* [Parameters] */")
        for k, v in params.items():
            safe_k = "".join(c if c.isalnum() or c == "_" else "_" for c in str(k))
            if not safe_k or safe_k[0].isdigit():
                safe_k = f"p_{safe_k}"
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(f"{safe_k} = {_fmt(float(v))};")
            elif isinstance(v, bool):
                out.append(f"{safe_k} = {'true' if v else 'false'};")
            elif isinstance(v, str):
                out.append(f'{safe_k} = "{v}";')
            elif isinstance(v, (list, tuple)):
                vals = ", ".join(_fmt(float(x)) if isinstance(x, (int, float)) else str(x) for x in v)
                out.append(f"{safe_k} = [{vals}];")
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

    def _render_part_call(pid: str, idx: int, total_count: int, indent: str = "    ") -> str:
        pid_safe = _sanitize_module_name(pid, fallback="part")
        p_obj = part_map.get(pid_safe)
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
            return f"{indent}translate(explode * [{_fmt(dx)}, {_fmt(dy)}, {_fmt(dz)}])\n{indent}  {pid_safe}();"
        return f"{indent}{pid_safe}();"

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
    doc = check_printability(spec)
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