"""规格规范化与校验层：把 LLM 输出的 JSON 规格整理为可安全驱动的 OpenSCAD 输入。

职责：
- validate_spec(spec): 字段级校验 + 缺省补全 + 类型修正，返回 (规范后的 spec, 问题列表)。
  不抛异常——脏数据尽量降级为可编译形态，问题记录到 issues 供日志/审查参考。
- normalize_size(prim, part): 收敛 LLM 多种尺寸写法的规范化（原 geometry 内联逻辑迁移至此）。

设计原则：
- 宽松输入、严格输出：LLM 常给 cuboid/box、size 标量、radius+height 等变体，
  统一收敛为标准字段（primitive/center/rotation/size/holes）。
- 永不丢数据：无法识别的字段原样保留，仅补全缺省与修正类型，不擅自删字段。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from .log import get_logger as _log_module

_log = _log_module()

#: 合法原语名（含 LLM 常见别名）
PRIMITIVES = {
    "cube", "cuboid", "box", "cylinder", "sphere", "capsule", "polyhedron", "hull",
    "linear_extrude", "rotate_extrude", "counterbore", "text",
    "thread", "metric_thread", "gear", "involute_gear",
    "timing_pulley", "gt2_pulley", "snap_joint", "magnet_pocket",
}
#: 原语别名 → 标准名
_ALIAS = {
    "cuboid": "cube", "box": "cube",
    "extrude": "linear_extrude",
    "lathe": "rotate_extrude", "revolve": "rotate_extrude",
    "metric_thread": "thread", "threads": "thread", "screw": "thread", "bolt": "thread", "nut": "thread",
    "involute_gear": "gear", "spur_gear": "gear", "cog": "gear",
    "gt2_pulley": "timing_pulley", "gt2": "timing_pulley", "pulley": "timing_pulley",
    "snap_fit": "snap_joint", "cantilever_snap": "snap_joint", "latch": "snap_joint",
    "magnet_hole": "magnet_pocket", "magnet": "magnet_pocket",
}

#: ISO / DIN 标准公制螺纹粗牙标准螺距映射 (mm)
METRIC_PITCH: dict[float, float] = {
    1.6: 0.35,
    2.0: 0.4,
    2.5: 0.45,
    3.0: 0.5,
    4.0: 0.7,
    5.0: 0.8,
    6.0: 1.0,
    8.0: 1.25,
    10.0: 1.5,
    12.0: 1.75,
    14.0: 2.0,
    16.0: 2.0,
    20.0: 2.5,
}

#: ISO / DIN 标准公制紧固件阶梯沉头孔尺寸映射: {标称: (通孔直径d_screw, 沉头直径d_head, 沉头深h_head)}
METRIC_FASTENERS: dict[str, tuple[float, float, float]] = {
    "M2": (2.4, 4.4, 2.2),
    "M2.5": (2.9, 5.0, 2.7),
    "M3": (3.4, 6.2, 3.2),
    "M4": (4.5, 8.0, 4.2),
    "M5": (5.5, 10.0, 5.2),
    "M6": (6.6, 11.5, 6.2),
    "M8": (9.0, 15.0, 8.2),
}

#: 常见实物基准参照物数据库 (供视觉比例标定自动推算 mm 缩放比率)
METRIC_CALIBRATION_REFS: dict[str, dict[str, Any]] = {
    "coin_1cny": {"name": "1元硬币 (人民币)", "shape": "circle", "d_mm": 25.0},
    "coin_5jiao": {"name": "5角硬币 (人民币)", "shape": "circle", "d_mm": 20.5},
    "coin_1jiao": {"name": "1角硬币 (人民币)", "shape": "circle", "d_mm": 19.0},
    "coin_us_quarter": {"name": "US Quarter 25美分", "shape": "circle", "d_mm": 24.26},
    "coin_us_penny": {"name": "US Penny 1美分", "shape": "circle", "d_mm": 19.05},
    "credit_card": {"name": "标准银行卡/身份证 (ID-1)", "shape": "rect", "width_mm": 85.60, "height_mm": 53.98},
    "a4_paper": {"name": "A4 打印纸", "shape": "rect", "width_mm": 210.0, "height_mm": 297.0},
    "ruler_10mm": {"name": "格尺/标尺 10mm", "shape": "linear", "length_mm": 10.0},
}


def _as_vec3(v: Any, default=(0.0, 0.0, 0.0)) -> list[float]:
    """把任意值规范为 3 分量 float 向量。"""
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        try:
            return [float(v[0]), float(v[1]), float(v[2])]
        except (TypeError, ValueError):
            pass
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # 标量 → 立方体各向
        return [float(v), float(v), float(v)]
    return list(default)


def _sanitize_num(v: Any, default: float) -> float:
    """单个数值净化：NaN/inf/非数 → default。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            f = float(v)
            if math.isfinite(f):
                return f
        except (TypeError, ValueError):
            pass
    return default


def _sanitize_color(c: Any) -> Optional[str | list[float]]:
    """规范化部件颜色：支持英文颜色名、十六进制 #RRGGBB、RGB/RGBA 数组。"""
    if not c:
        return None
    if isinstance(c, str):
        s = c.strip().strip("'\"")
        if s and all(ch.isalnum() or ch in "#_-" for ch in s) and len(s) <= 32:
            return s
        return None
    if isinstance(c, (list, tuple)) and len(c) >= 3:
        try:
            nums = [float(x) for x in c[:4]]
            if any(x > 1.0 for x in nums):
                nums = [min(max(x / 255.0, 0.0), 1.0) for x in nums]
            else:
                nums = [min(max(x, 0.0), 1.0) for x in nums]
            return nums
        except (TypeError, ValueError):
            return None
    return None


def normalize_size(prim: str, part: dict[str, Any]) -> list[float]:
    """把 LLM 输出的多种尺寸字段统一为生成器期望的 size 列表。

    迁移自 geometry._normalize_size，作为规范化层的唯一权威实现。
    - cube/cuboid: size=[x,y,z] 或 size=N(立方体) 或 width/depth/height
    - cylinder:  size=[直径,高度] 或 radius/height/diameter/height
    - sphere:    size=[半径] 或 radius
    - polyhedron: 默认 10x10x10（真顶点由 polyhedron builder 处理）
    """
    p = (prim or "cube").lower()
    raw = part.get("size")
    # 标量 size
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        v = _sanitize_num(raw, 10.0)
        if p in ("cube", "cuboid", "box"):
            return [v, v, v]
        if p == "sphere":
            return [v]
        if p == "cylinder":
            return [v * 2, v]
    if isinstance(raw, list) and raw:
        if p in ("cube", "cuboid", "box"):
            clean = [_sanitize_num(x, 10.0) for x in raw[:3] or [10.0, 10.0, 10.0]] or [10.0] * 3
            fillet = _sanitize_num(part.get("fillet", 0.0), 0.0)
            chamfer = _sanitize_num(part.get("chamfer", 0.0), 0.0)
            if len(raw) >= 4 and float(raw[3]) > 0.0:
                fillet = float(raw[3])
            if len(raw) >= 5 and float(raw[4]) > 0.0:
                chamfer = float(raw[4])
            if fillet > 0.0:
                clean = clean[:3] + [fillet, 0.0]
            elif chamfer > 0.0:
                clean = clean[:3] + [0.0, chamfer]
            return clean
        if p == "cylinder":
            chamfer = _sanitize_num(part.get("chamfer", 0.0), 0.0)
            if len(raw) >= 4 and float(raw[3]) > 0.0:
                chamfer = float(raw[3])
            if chamfer > 0.0:
                d = _sanitize_num(raw[0], 10.0)
                h = _sanitize_num(raw[2] if len(raw) >= 3 else raw[1], 10.0)
                return [d, d, h, chamfer]
            if len(raw) >= 3:
                return [_sanitize_num(raw[0], 10.0), _sanitize_num(raw[1], 10.0), _sanitize_num(raw[2], 10.0)]
            return [_sanitize_num(raw[0], 10.0), _sanitize_num(raw[1], 10.0)]
        clean = [_sanitize_num(x, 10.0) for x in raw[:3] or [10.0, 10.0, 10.0]] or [10.0] * 3
        return clean
    # 从其他字段推断
    if p in ("cube", "cuboid", "box"):
        w = _sanitize_num(part.get("width", part.get("x", 10)), 10.0)
        d = _sanitize_num(part.get("depth", part.get("y", 10)), 10.0)
        h = _sanitize_num(part.get("height", part.get("z", 10)), 10.0)
        fillet = _sanitize_num(part.get("fillet", 0.0), 0.0)
        chamfer = _sanitize_num(part.get("chamfer", 0.0), 0.0)
        if fillet > 0.0:
            return [w, d, h, fillet, 0.0]
        elif chamfer > 0.0:
            return [w, d, h, 0.0, chamfer]
        return [w, d, h]
    if p == "cylinder":
        # 显式三元组 [d1, d2, h] 支持圆台/圆锥
        if isinstance(raw, list) and len(raw) >= 3:
            return [_sanitize_num(raw[0], 10.0), _sanitize_num(raw[1], 10.0), _sanitize_num(raw[2], 10.0)]
        # 检查是否有圆台/圆锥参数: r1, r2, d1, d2 等
        has_taper = any(k in part for k in ("r1", "r2", "d1", "d2", "radius1", "radius2", "diameter1", "diameter2"))
        if has_taper:
            r1_raw = part.get("r1", part.get("radius1"))
            r2_raw = part.get("r2", part.get("radius2"))
            r1 = _sanitize_num(r1_raw, 5.0) if r1_raw is not None else None
            r2 = _sanitize_num(r2_raw, 0.0) if r2_raw is not None else None
            d1 = _sanitize_num(part.get("d1", part.get("diameter1")), r1 * 2 if r1 is not None else 10.0)
            d2 = _sanitize_num(part.get("d2", part.get("diameter2")), r2 * 2 if r2 is not None else 0.0)
            h = _sanitize_num(part.get("height", 10), 10.0)
            return [d1, d2, h]
        r = _sanitize_num(part.get("radius", 5), 5.0)
        d = _sanitize_num(part.get("diameter", r * 2), r * 2)
        h = _sanitize_num(part.get("height", 10), 10.0)
        chamfer = _sanitize_num(part.get("chamfer", 0.0), 0.0)
        if chamfer > 0.0:
            return [d, d, h, chamfer]
        return [d, h]
    if p == "sphere":
        return [_sanitize_num(part.get("radius", 5), 5.0)]
    if p == "capsule":
        r = _sanitize_num(part.get("radius", 5), 5.0)
        h = _sanitize_num(part.get("height", part.get("length", 20)), 20.0)
        return [r, h]
    if p in ("thread", "metric_thread"):
        if isinstance(raw, list) and len(raw) >= 3:
            d = _sanitize_num(raw[0], 8.0)
            pitch = _sanitize_num(raw[1], METRIC_PITCH.get(d, 1.25))
            length = _sanitize_num(raw[2], 15.0)
            tol = _sanitize_num(raw[3] if len(raw) >= 4 else part.get("tolerance", 0.0), 0.0)
            internal = 1.0 if (len(raw) >= 5 and raw[4]) or part.get("internal") else 0.0
            return [d, pitch, length, tol, internal]
        d = _sanitize_num(part.get("diameter", part.get("d", 8.0)), 8.0)
        pitch = _sanitize_num(part.get("pitch", part.get("p", METRIC_PITCH.get(d, 1.25))), 1.25)
        length = _sanitize_num(part.get("length", part.get("height", part.get("h", 15.0))), 15.0)
        tol = _sanitize_num(part.get("tolerance", -0.1 if not part.get("internal") else 0.2), -0.1 if not part.get("internal") else 0.2)
        internal = 1.0 if part.get("internal") else 0.0
        return [d, pitch, length, tol, internal]
    if p in ("gear", "involute_gear"):
        if isinstance(raw, list) and len(raw) >= 2:
            teeth = _sanitize_num(raw[0], 20.0)
            mod = _sanitize_num(raw[1], 1.5)
            h = _sanitize_num(raw[2] if len(raw) >= 3 else 6.0, 6.0)
            bore = _sanitize_num(raw[3] if len(raw) >= 4 else 5.0, 5.0)
            pa = _sanitize_num(raw[4] if len(raw) >= 5 else 20.0, 20.0)
            return [teeth, mod, h, bore, pa]
        teeth = _sanitize_num(part.get("teeth", part.get("z", 20.0)), 20.0)
        mod = _sanitize_num(part.get("module", part.get("m", 1.5)), 1.5)
        h = _sanitize_num(part.get("height", part.get("thickness", part.get("h", 6.0))), 6.0)
        bore = _sanitize_num(part.get("bore", part.get("shaft", 5.0)), 5.0)
        pa = _sanitize_num(part.get("pressure_angle", 20.0), 20.0)
        return [teeth, mod, h, bore, pa]
    if p in ("timing_pulley", "gt2_pulley"):
        if isinstance(raw, list) and len(raw) >= 2:
            teeth = _sanitize_num(raw[0], 20.0)
            w = _sanitize_num(raw[1], 6.0)
            bore = _sanitize_num(raw[2] if len(raw) >= 3 else 5.0, 5.0)
            flange = 1.0 if (len(raw) < 4 or raw[3]) else 0.0
            pitch = _sanitize_num(raw[4] if len(raw) >= 5 else 2.0, 2.0)
            return [teeth, w, bore, flange, pitch]
        teeth = _sanitize_num(part.get("teeth", part.get("z", 20.0)), 20.0)
        w = _sanitize_num(part.get("belt_width", part.get("width", part.get("height", 6.0))), 6.0)
        bore = _sanitize_num(part.get("bore", part.get("shaft", 5.0)), 5.0)
        flange = 1.0 if part.get("flange", True) else 0.0
        pitch = _sanitize_num(part.get("pitch", 2.0), 2.0)
        return [teeth, w, bore, flange, pitch]
    if p in ("snap_joint", "snap_fit"):
        if isinstance(raw, list) and len(raw) >= 3:
            l = _sanitize_num(raw[0], 12.0)
            w = _sanitize_num(raw[1], 6.0)
            t = _sanitize_num(raw[2], 1.5)
            hook = _sanitize_num(raw[3] if len(raw) >= 4 else 1.0, 1.0)
            return [l, w, t, hook]
        l = _sanitize_num(part.get("length", part.get("height", 12.0)), 12.0)
        w = _sanitize_num(part.get("width", 6.0), 6.0)
        t = _sanitize_num(part.get("thickness", 1.5), 1.5)
        hook = _sanitize_num(part.get("hook_depth", part.get("hook", 1.0)), 1.0)
        return [l, w, t, hook]
    if p in ("magnet_pocket", "magnet_hole"):
        if isinstance(raw, list) and len(raw) >= 2:
            d = _sanitize_num(raw[0], 6.0)
            depth = _sanitize_num(raw[1], 3.0)
            tol = _sanitize_num(raw[2] if len(raw) >= 3 else 0.2, 0.2)
            eject = _sanitize_num(raw[3] if len(raw) >= 4 else 1.5, 1.5)
            return [d, depth, tol, eject]
        d = _sanitize_num(part.get("diameter", part.get("d", 6.0)), 6.0)
        depth = _sanitize_num(part.get("depth", part.get("height", part.get("h", 3.0))), 3.0)
        tol = _sanitize_num(part.get("clearance", part.get("tolerance", 0.2)), 0.2)
        eject = _sanitize_num(part.get("ejection_hole", part.get("eject", 1.5)), 1.5)
        return [d, depth, tol, eject]
    return [10.0, 10.0, 10.0]


def normalize_hole_size(prim: str, hole: dict[str, Any]) -> list[float]:
    """hole 的尺寸规范化（与 part 相同，默认半径 5 / 高度 10）。"""
    return normalize_size(prim, hole)


def _sanitize_array(cfg: Any, array_type: str) -> Optional[dict[str, Any]]:
    """规范化阵列配置（radial: count+radius, linear: count+step）。"""
    if not isinstance(cfg, dict):
        return None
    count = int(_sanitize_num(cfg.get("count", 0), 0))
    if count <= 1:
        return None
    if array_type == "radial":
        radius = _sanitize_num(cfg.get("radius", cfg.get("r", 20.0)), 20.0)
        return {"count": count, "radius": radius}
    elif array_type == "linear":
        step = _as_vec3(cfg.get("step"), (10.0, 0.0, 0.0))
        return {"count": count, "step": step}
    return None


def _normalize_hole(hole: dict[str, Any]) -> dict[str, Any]:
    """规范化单个 hole（补 primitive/center/size，净化数值与阵列）。"""
    if not isinstance(hole, dict):
        return {"primitive": "cube"}
    prim = _ALIAS.get(str(hole.get("primitive", "cube")).lower(),
                      str(hole.get("primitive", "cube")).lower())
    # 工业标准阶梯沉头孔识别 (如 standard: "M3")
    std = str(hole.get("standard", hole.get("counterbore", hole.get("bolt", "")))).strip().upper()
    if std in METRIC_FASTENERS or prim == "counterbore":
        prim = "counterbore"
        if std not in METRIC_FASTENERS:
            std = "M3"

    if prim not in PRIMITIVES:
        prim = "cube"
    out = dict(hole)  # 保留 LLM 原字段
    out["primitive"] = prim
    if prim == "counterbore":
        out["standard"] = std
    out["center"] = _as_vec3(hole.get("center"), (0.0, 0.0, 0.0))
    out["size"] = normalize_hole_size(prim, hole)
    if "radial_array" in hole:
        arr = _sanitize_array(hole["radial_array"], "radial")
        if arr:
            out["radial_array"] = arr
    if "linear_array" in hole:
        arr = _sanitize_array(hole["linear_array"], "linear")
        if arr:
            out["linear_array"] = arr
    return out


def normalize_part(part: dict[str, Any], default_id: str = "part") -> dict[str, Any]:
    """规范化单个 part：primitive/center/rotation/size/holes，类型安全。"""
    if not isinstance(part, dict):
        part = {}
    out = dict(part)  # 保留原字段
    prim = _ALIAS.get(str(part.get("primitive", "cube")).lower(),
                      str(part.get("primitive", "cube")).lower())
    if prim not in PRIMITIVES:
        prim = "cube"
    out["primitive"] = prim
    out["id"] = part.get("id", default_id)
    out["center"] = _as_vec3(part.get("center"), (0.0, 0.0, 0.0))
    rot = _as_vec3(part.get("rotation"), (0.0, 0.0, 0.0))
    out["rotation"] = rot
    color = _sanitize_color(part.get("color"))
    if color is not None:
        out["color"] = color
    fillet = _sanitize_num(part.get("fillet", 0.0), 0.0)
    if fillet > 0.0:
        out["fillet"] = fillet
    chamfer = _sanitize_num(part.get("chamfer", 0.0), 0.0)
    if chamfer > 0.0:
        out["chamfer"] = chamfer
    if part.get("symmetric") or part.get("symmetry"):
        out["symmetric"] = True
    if "mirror" in part and part.get("mirror"):
        out["mirror"] = _as_vec3(part.get("mirror"), (1.0, 0.0, 0.0))
    if "radial_array" in part:
        arr = _sanitize_array(part["radial_array"], "radial")
        if arr:
            out["radial_array"] = arr
    if "linear_array" in part:
        arr = _sanitize_array(part["linear_array"], "linear")
        if arr:
            out["linear_array"] = arr

    # 拉伸与回转体的 2D polygon 点列规范化
    if prim in ("linear_extrude", "rotate_extrude"):
        poly = part.get("polygon") or part.get("points")
        if isinstance(poly, list) and poly:
            clean_poly = []
            for pt in poly:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    clean_poly.append([_sanitize_num(pt[0], 0.0), _sanitize_num(pt[1], 0.0)])
            out["polygon"] = clean_poly if len(clean_poly) >= 3 else None
        else:
            out["polygon"] = None
        out["twist"] = _sanitize_num(part.get("twist", 0.0), 0.0)
        out["scale"] = _sanitize_num(part.get("scale", 1.0), 1.0)
        out["angle"] = _sanitize_num(part.get("angle", 360.0), 360.0)

    # 3D 文字规范化
    if prim == "text":
        out["text"] = str(part.get("text", part.get("content", "CAD")))
        out["font_size"] = _sanitize_num(part.get("font_size", 10.0), 10.0)
        out["height"] = _sanitize_num(part.get("height", 3.0), 3.0)
        if part.get("font"):
            out["font"] = str(part.get("font"))

    out["size"] = normalize_size(prim, part)
    holes = part.get("holes") or []
    out["holes"] = [_normalize_hole(h) for h in holes]
    # polyhedron 顶点校验：points/faces 必须为 list of 3-vec / list of index-list
    if prim == "polyhedron":
        pts = part.get("points") or part.get("vertices")
        faces = part.get("faces")
        if isinstance(pts, list) and pts:
            clean_pts = []
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 3:
                    clean_pts.append([_sanitize_num(p[0], 0.0),
                                      _sanitize_num(p[1], 0.0),
                                      _sanitize_num(p[2], 0.0)])
            out["points"] = clean_pts or None
        else:
            out["points"] = None  # 非法/缺失 → 降级 box
        if isinstance(faces, list) and faces:
            out["faces"] = [[int(i) for i in f] if isinstance(f, (list, tuple)) else [int(f)]
                            for f in faces]
        else:
            out["faces"] = []
    return out


_UNIT_FACTORS = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0,
    "m": 1000.0, "meter": 1000.0, "meters": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4,
}


def _scale_geom(val: Any, factor: float) -> Any:
    """递归按比例缩放几何尺寸与坐标。"""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return round(float(val) * factor, 4)
    if isinstance(val, list):
        return [_scale_geom(x, factor) for x in val]
    return val


def validate_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """校验并规范化整个规格，返回 (规范后 spec, 问题列表)。

    - 补全缺省：name/description/units/parts/assembly
    - 规范化每个 part（primitive/center/rotation/size/holes）
    - union_parts 若缺省则用全部 part id
    - 问题仅记录，不抛异常
    """
    issues: list[str] = []
    if not isinstance(spec, dict):
        spec = {}
        issues.append("spec 不是 dict，已重置为空")
    out = dict(spec)
    out.setdefault("name", "model")
    out.setdefault("description", "")
    out.setdefault("units", "mm")

    parts = out.get("parts")
    if not isinstance(parts, list):
        if parts:
            issues.append("parts 不是 list，已丢弃")
        out["parts"] = []
    else:
        out["parts"] = [normalize_part(p, f"part_{i}") for i, p in enumerate(parts)]

    # 工业尺度自动归一化：支持 cm / inch / m 自动换算为 OpenSCAD 标尺 mm
    raw_units = str(out.get("units", "mm")).strip().lower()
    factor = _UNIT_FACTORS.get(raw_units, 1.0)
    if abs(factor - 1.0) > 1e-4:
        issues.append(f"单位 '{raw_units}' 自动换算为标准毫米 (缩放系数 ×{factor})")
        out["units"] = "mm"
        for p in out["parts"]:
            p["size"] = _scale_geom(p.get("size"), factor)
            p["center"] = _scale_geom(p.get("center"), factor)
            if "height" in p:
                p["height"] = _scale_geom(p["height"], factor)
            if "radius" in p:
                p["radius"] = _scale_geom(p["radius"], factor)
            if "fillet" in p:
                p["fillet"] = _scale_geom(p["fillet"], factor)
            if "chamfer" in p:
                p["chamfer"] = _scale_geom(p["chamfer"], factor)
            if "r1" in p:
                p["r1"] = _scale_geom(p["r1"], factor)
            if "r2" in p:
                p["r2"] = _scale_geom(p["r2"], factor)
            if p.get("polygon"):
                p["polygon"] = _scale_geom(p["polygon"], factor)
            if p.get("points"):
                p["points"] = _scale_geom(p["points"], factor)
            if p.get("linear_array"):
                p["linear_array"]["step"] = _scale_geom(p["linear_array"]["step"], factor)
            if p.get("radial_array"):
                p["radial_array"]["radius"] = _scale_geom(p["radial_array"]["radius"], factor)
            # 3D 浮雕文字: 字号与浮雕厚度同样必须随单位缩放, 否则字体与实体比例失真
            if str(p.get("primitive", "")).lower() == "text" or p.get("text") or p.get("font_size"):
                if "font_size" in p:
                    p["font_size"] = _scale_geom(p["font_size"], factor)
                # p["height"] 已由上方通用逻辑缩放 (覆盖 text/linear_extrude/capsule 的拉伸高度)
            for h in p.get("holes", []):
                h["size"] = _scale_geom(h.get("size"), factor)
                h["center"] = _scale_geom(h.get("center"), factor)
                if h.get("linear_array"):
                    h["linear_array"]["step"] = _scale_geom(h["linear_array"]["step"], factor)
                if h.get("radial_array"):
                    h["radial_array"]["radius"] = _scale_geom(h["radial_array"]["radius"], factor)

    # 视觉基准参照物标定 (Metric Visual Calibration)
    calib = out.get("calibration")
    if isinstance(calib, dict):
        calib_factor = float(_sanitize_num(calib.get("scale_factor", 1.0), 1.0))
        ref_id = str(calib.get("reference", calib.get("object", ""))).lower().strip()
        ref_info = METRIC_CALIBRATION_REFS.get(ref_id)
        if ref_info:
            target_mm = ref_info.get("d_mm") or ref_info.get("width_mm") or ref_info.get("length_mm")
            estimated_mm = float(_sanitize_num(calib.get("estimated_mm", calib.get("measured_mm", 0.0)), 0.0))
            if estimated_mm > 1e-4 and target_mm:
                calib_factor = float(target_mm) / estimated_mm
                calib["scale_factor"] = round(calib_factor, 4)
        if abs(calib_factor - 1.0) > 1e-4:
            issues.append(f"视觉基准参照物标定自动缩放 (校准系数 ×{calib_factor:.4f})")
            for p in out["parts"]:
                p["size"] = _scale_geom(p.get("size"), calib_factor)
                p["center"] = _scale_geom(p.get("center"), calib_factor)
                if "height" in p:
                    p["height"] = _scale_geom(p["height"], calib_factor)
                if "radius" in p:
                    p["radius"] = _scale_geom(p["radius"], calib_factor)
                if "fillet" in p:
                    p["fillet"] = _scale_geom(p["fillet"], calib_factor)
                if "chamfer" in p:
                    p["chamfer"] = _scale_geom(p["chamfer"], calib_factor)
                if "r1" in p:
                    p["r1"] = _scale_geom(p["r1"], calib_factor)
                if "r2" in p:
                    p["r2"] = _scale_geom(p["r2"], calib_factor)
                if p.get("polygon"):
                    p["polygon"] = _scale_geom(p["polygon"], calib_factor)
                if p.get("points"):
                    p["points"] = _scale_geom(p["points"], calib_factor)
                if p.get("linear_array"):
                    p["linear_array"]["step"] = _scale_geom(p["linear_array"]["step"], calib_factor)
                if p.get("radial_array"):
                    p["radial_array"]["radius"] = _scale_geom(p["radial_array"]["radius"], calib_factor)
                if str(p.get("primitive", "")).lower() == "text" or p.get("text") or p.get("font_size"):
                    if "font_size" in p:
                        p["font_size"] = _scale_geom(p["font_size"], calib_factor)
                for h in p.get("holes", []):
                    h["size"] = _scale_geom(h.get("size"), calib_factor)
                    h["center"] = _scale_geom(h.get("center"), calib_factor)
                    if h.get("linear_array"):
                        h["linear_array"]["step"] = _scale_geom(h["linear_array"]["step"], calib_factor)
                    if h.get("radial_array"):
                        h["radial_array"]["radius"] = _scale_geom(h["radial_array"]["radius"], calib_factor)

    # 规范化 id：去重
    seen: set[str] = set()
    for p in out["parts"]:
        pid = str(p.get("id", "part"))
        if pid in seen:
            new_id = f"{pid}_{len(seen)}"
            p["id"] = new_id
            issues.append(f"重复 part id '{pid}' → '{new_id}'")
        seen.add(p["id"])

    # assembly
    assembly = out.get("assembly")
    if not isinstance(assembly, dict):
        assembly = {}
    all_ids = [str(p.get("id")) for p in out["parts"]]

    # intersection_parts 校验
    intersect = assembly.get("intersection_parts")
    if isinstance(intersect, list) and intersect:
        valid_i = [u for u in intersect if str(u) in set(all_ids)]
        dropped_i = [u for u in intersect if str(u) not in set(all_ids)]
        if dropped_i:
            issues.append(f"assembly.intersection_parts 含未知 id {dropped_i}，已忽略")
        assembly["intersection_parts"] = valid_i
    else:
        intersect = None

    # difference_parts 校验 (需从装配主体中挖除的部件)
    diff = assembly.get("difference_parts")
    if isinstance(diff, list) and diff:
        valid_d = [u for u in diff if str(u) in set(all_ids)]
        dropped_d = [u for u in diff if str(u) not in set(all_ids)]
        if dropped_d:
            issues.append(f"assembly.difference_parts 含未知 id {dropped_d}，已忽略")
        assembly["difference_parts"] = valid_d
    else:
        diff = None

    # difference_from 基准部件校验
    diff_from = assembly.get("difference_from")
    if diff_from:
        diff_from_str = str(diff_from)
        if diff_from_str not in set(all_ids):
            issues.append(f"assembly.difference_from 指向未知 id '{diff_from_str}'，已忽略")
            assembly.pop("difference_from", None)
        else:
            assembly["difference_from"] = diff_from_str

    union = assembly.get("union_parts")
    if not isinstance(union, list) or not union:
        # 若未指定 union_parts，默认排除单独作为挖除项的 difference_parts
        diff_set = set(assembly.get("difference_parts") or [])
        if intersect and not [pid for pid in all_ids if pid not in intersect and pid not in diff_set]:
            assembly["union_parts"] = []
        else:
            assembly["union_parts"] = [pid for pid in all_ids if pid not in diff_set and pid not in (intersect or [])]
            if not assembly["union_parts"] and not intersect:
                assembly["union_parts"] = all_ids
    else:
        # 只保留存在于 parts 的 id
        valid = [u for u in union if str(u) in set(all_ids)]
        dropped = [u for u in union if str(u) not in set(all_ids)]
        if dropped:
            issues.append(f"assembly.union_parts 含未知 id {dropped}，已忽略")
        assembly["union_parts"] = valid

    out["assembly"] = assembly

    return out, issues


__all__ = ["validate_spec", "normalize_part", "normalize_size", "normalize_hole_size",
           "PRIMITIVES"]
