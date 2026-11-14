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
from typing import Any

from .log import get_logger as _log_module

_log = _log_module()

#: 合法原语名（含 LLM 常见别名）
PRIMITIVES = {"cube", "cuboid", "box", "cylinder", "sphere", "polyhedron"}
#: 原语别名 → 标准名
_ALIAS = {"cuboid": "cube", "box": "cube"}


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
        return [_sanitize_num(x, 10.0) for x in raw[:3] or [10.0, 10.0, 10.0]] or [10.0] * 3
    # 从其他字段推断
    if p in ("cube", "cuboid", "box"):
        w = _sanitize_num(part.get("width", part.get("x", 10)), 10.0)
        d = _sanitize_num(part.get("depth", part.get("y", 10)), 10.0)
        h = _sanitize_num(part.get("height", part.get("z", 10)), 10.0)
        return [w, d, h]
    if p == "cylinder":
        r = _sanitize_num(part.get("radius", 5), 5.0)
        d = _sanitize_num(part.get("diameter", r * 2), r * 2)
        h = _sanitize_num(part.get("height", 10), 10.0)
        return [d, h]
    if p == "sphere":
        return [_sanitize_num(part.get("radius", 5), 5.0)]
    return [10.0, 10.0, 10.0]


def normalize_hole_size(prim: str, hole: dict[str, Any]) -> list[float]:
    """hole 的尺寸规范化（与 part 相同，默认半径 5 / 高度 10）。"""
    return normalize_size(prim, hole)


def _normalize_hole(hole: dict[str, Any]) -> dict[str, Any]:
    """规范化单个 hole（补 primitive/center/size，净化数值）。"""
    if not isinstance(hole, dict):
        return {"primitive": "cube"}
    prim = _ALIAS.get(str(hole.get("primitive", "cube")).lower(),
                      str(hole.get("primitive", "cube")).lower())
    if prim not in PRIMITIVES:
        prim = "cube"
    out = dict(hole)  # 保留 LLM 原字段
    out["primitive"] = prim
    out["center"] = _as_vec3(hole.get("center"), (0.0, 0.0, 0.0))
    out["size"] = normalize_hole_size(prim, hole)
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
        if issues:
            pass
        if not isinstance(parts, list) and parts:
            issues.append("parts 不是 list，已丢弃")
        out["parts"] = [normalize_part(p, f"part_{i}") for i, p in enumerate(parts or [])]
    else:
        out["parts"] = [normalize_part(p, f"part_{i}") for i, p in enumerate(parts)]

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
    union = assembly.get("union_parts")
    if not isinstance(union, list) or not union:
        assembly["union_parts"] = all_ids
    else:
        # 只保留存在于 parts 的 id
        valid = [u for u in union if str(u) in set(all_ids)]
        dropped = [u for u in union if str(u) not in set(all_ids)]
        if dropped:
            issues.append(f"assembly.union_parts 含未知 id {dropped}，已忽略")
        assembly["union_parts"] = valid or all_ids
    out["assembly"] = assembly

    return out, issues


__all__ = ["validate_spec", "normalize_part", "normalize_size", "normalize_hole_size",
           "PRIMITIVES"]
