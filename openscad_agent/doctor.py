"""3D 打印适印性诊断系统 (Printability Doctor)。

在模型导出前自动分析规格几何特征：
- 极细特征 / 薄壁诊断（<0.4mm 无法切片警告，<0.8mm 脆弱壁厚提醒）
- 微小孔径预警（<1.5mm 熔融热缩闭孔提醒）
- 细长高比结构预警（高径比 > 10 打印晃动走丝预警）
- 打印机热床超限评估（常见 256x256x256mm 热床适配）
- 输出结构化评估报告与优化建议，写入 OpenSCAD 源码注释并在终端/GUI提示。
"""

from __future__ import annotations

import math
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from .spec import normalize_size, normalize_hole_size


@dataclass
class PrintabilityReport:
    """3D 打印适印性诊断报告。"""
    printable: bool = True
    score: int = 100
    warnings: list[str] = field(default_factory=list)
    tips: list[str] = field(default_factory=list)
    bounding_box: tuple[float, float, float] = (0.0, 0.0, 0.0)
    volume_cm3: float = 0.0
    weight_g: float = 0.0
    print_time_hours: float = 0.0
    cost_cny: float = 0.0
    cost_usd: float = 0.0
    material: str = "PLA"
    shrinkage_pct: float = 0.3

    def summary(self) -> str:
        status = "✅ 优良" if self.score >= 90 else ("⚠️ 需注意" if self.score >= 70 else "❌ 风险较高")
        return f"适印性评分: {self.score}/100 ({status})"


MATERIAL_PROFILES: dict[str, dict[str, Any]] = {
    "PLA": {
        "name": "PLA (聚乳酸)",
        "density": 1.24,             # g/cm³
        "shrinkage_pct": 0.3,         # % 收缩率
        "nozzle_temp": "205-220°C (推荐 210°C)",
        "bed_temp": "55-65°C (推荐 60°C)",
        "fan_speed": "100%",
        "cost_per_kg_cny": 80.0,
        "cost_per_kg_usd": 20.0,
        "enclosure_required": False,
        "features": "收缩率极低、尺寸稳定性高、极易打印、适合原型与结构件",
    },
    "PETG": {
        "name": "PETG (改性聚对苯二甲酸乙二醇酯)",
        "density": 1.27,
        "shrinkage_pct": 0.5,
        "nozzle_temp": "230-250°C (推荐 240°C)",
        "bed_temp": "75-85°C (推荐 80°C)",
        "fan_speed": "30-50%",
        "cost_per_kg_cny": 90.0,
        "cost_per_kg_usd": 22.0,
        "enclosure_required": False,
        "features": "高韧性、耐候耐冲击、耐温达 75°C，易轻微拉丝",
    },
    "ABS": {
        "name": "ABS (丙烯腈-丁二烯-苯乙烯共聚物)",
        "density": 1.04,
        "shrinkage_pct": 1.5,
        "nozzle_temp": "245-265°C (推荐 250°C)",
        "bed_temp": "95-110°C (推荐 100°C)",
        "fan_speed": "0-20%",
        "cost_per_kg_cny": 95.0,
        "cost_per_kg_usd": 24.0,
        "enclosure_required": True,
        "features": "耐冲击、耐温达 100°C、易打磨，收缩率较高必须封箱保温防翘边",
    },
    "TPU": {
        "name": "TPU (热塑性聚氨酯弹性体 95A)",
        "density": 1.21,
        "shrinkage_pct": 0.8,
        "nozzle_temp": "215-235°C (推荐 225°C)",
        "bed_temp": "35-50°C (推荐 45°C)",
        "fan_speed": "80-100%",
        "cost_per_kg_cny": 120.0,
        "cost_per_kg_usd": 30.0,
        "enclosure_required": False,
        "features": "高弹性、抗撕裂耐磨，建议近端挤出机、打印速度降至 25-35mm/s",
    },
    "PC": {
        "name": "PC (聚碳酸酯)",
        "density": 1.20,
        "shrinkage_pct": 1.6,
        "nozzle_temp": "260-290°C (推荐 275°C)",
        "bed_temp": "105-120°C (推荐 110°C)",
        "fan_speed": "0%",
        "cost_per_kg_cny": 180.0,
        "cost_per_kg_usd": 45.0,
        "enclosure_required": True,
        "features": "超高强度刚性、耐温达 115°C，高热应力需高温封箱保温与特种附着胶",
    },
}


def _get_rotation_matrix(rx_deg: float, ry_deg: float, rz_deg: float) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """计算 OpenSCAD Euler 旋转矩阵 (Rz * Ry * Rx)。

    OpenSCAD rotate([rx, ry, rz]) 依次按 X 轴、Y 轴、Z 轴顺序旋转。
    """
    a = math.radians(rx_deg)
    b = math.radians(ry_deg)
    c = math.radians(rz_deg)
    ca, sa = math.cos(a), math.sin(a)
    cb, sb = math.cos(b), math.sin(b)
    cc, sc = math.cos(c), math.sin(c)
    return (
        (cc * cb, cc * sb * sa - sc * ca, cc * sb * ca + sc * sa),
        (sc * cb, sc * sb * sa + cc * ca, sc * sb * ca - cc * sa),
        (-sb, cb * sa, cb * ca),
    )


def _apply_rotation(r: tuple[tuple[float, float, float], ...], v: tuple[float, float, float]) -> tuple[float, float, float]:
    """计算 3x3 旋转矩阵作用于 3D 向量。"""
    return (
        r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
        r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
        r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2],
    )


def _get_part_aabb(p: dict[str, Any]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """计算单个部件在世界坐标下的 Axis-Aligned Bounding Box (AABB)。

    采用精确 3D Euler 旋转矩阵 (Rz * Ry * Rx)，对旋转倾斜物体计算闭式解析或顶点映射包围盒。
    返回 ((min_x, min_y, min_z), (max_x, max_y, max_z))。
    """
    prim = str(p.get("primitive", "cube")).lower()
    size = normalize_size(prim, p)
    center = p.get("center", [0, 0, 0])
    cx = float(center[0]) if len(center) >= 1 else 0.0
    cy = float(center[1]) if len(center) >= 2 else 0.0
    cz = float(center[2]) if len(center) >= 3 else 0.0

    rot = p.get("rotation") or [0, 0, 0]
    rx = float(rot[0]) if len(rot) >= 1 else 0.0
    ry = float(rot[1]) if len(rot) >= 2 else 0.0
    rz = float(rot[2]) if len(rot) >= 3 else 0.0
    has_rot = any(abs(v) > 1e-4 for v in (rx, ry, rz))

    if prim in ("cube", "cuboid", "box"):
        w, d, h = size[0], size[1], size[2]
        if not has_rot:
            return ((cx - w / 2, cy - d / 2, cz), (cx + w / 2, cy + d / 2, cz + h))
        R = _get_rotation_matrix(rx, ry, rz)
        # 8 个局部顶点: x in [-w/2, w/2], y in [-d/2, d/2], z in [0, h]
        corners = [
            (sx * w / 2.0, sy * d / 2.0, sz * h)
            for sx in (-1.0, 1.0)
            for sy in (-1.0, 1.0)
            for sz in (0.0, 1.0)
        ]
        pts = [
            (cx + rv[0], cy + rv[1], cz + rv[2])
            for rv in (_apply_rotation(R, c) for c in corners)
        ]
        return (
            (min(p[0] for p in pts), min(p[1] for p in pts), min(p[2] for p in pts)),
            (max(p[0] for p in pts), max(p[1] for p in pts), max(p[2] for p in pts)),
        )

    elif prim == "cylinder":
        if len(size) >= 3:
            d1, d2, h = float(size[0]), float(size[1]), float(size[2])
        else:
            d1 = d2 = float(size[0])
            h = float(size[1]) if len(size) >= 2 else 10.0
        r1, r2 = d1 / 2.0, d2 / 2.0

        if not has_rot:
            max_r = max(r1, r2)
            return ((cx - max_r, cy - max_r, cz), (cx + max_r, cy + max_r, cz + h))

        R = _get_rotation_matrix(rx, ry, rz)
        top_center = (h * R[0][2], h * R[1][2], h * R[2][2])
        mins: list[float] = []
        maxs: list[float] = []
        for k in range(3):
            proj_scale = math.sqrt(R[k][0] ** 2 + R[k][1] ** 2)
            b_min, b_max = -r1 * proj_scale, r1 * proj_scale
            t_min, t_max = top_center[k] - r2 * proj_scale, top_center[k] + r2 * proj_scale
            c_k = cx if k == 0 else (cy if k == 1 else cz)
            mins.append(c_k + min(b_min, t_min))
            maxs.append(c_k + max(b_max, t_max))
        return ((mins[0], mins[1], mins[2]), (maxs[0], maxs[1], maxs[2]))

    elif prim == "capsule":
        r = float(size[0]) if size else 5.0
        h = float(size[1]) if len(size) >= 2 else 20.0
        span = max(h - 2 * r, 0.0)

        if not has_rot:
            return ((cx - r, cy - r, cz), (cx + r, cy + r, cz + h))

        R = _get_rotation_matrix(rx, ry, rz)
        v1 = _apply_rotation(R, (0.0, 0.0, r))
        v2 = _apply_rotation(R, (0.0, 0.0, r + span))
        mins = [
            (cx if k == 0 else (cy if k == 1 else cz)) + min(v1[k], v2[k]) - r
            for k in range(3)
        ]
        maxs = [
            (cx if k == 0 else (cy if k == 1 else cz)) + max(v1[k], v2[k]) + r
            for k in range(3)
        ]
        return ((mins[0], mins[1], mins[2]), (maxs[0], maxs[1], maxs[2]))

    elif prim == "sphere":
        r = float(size[0]) if size else 5.0
        return ((cx - r, cy - r, cz - r), (cx + r, cy + r, cz + r))

    elif prim in ("linear_extrude", "rotate_extrude", "text"):
        h = float(p.get("height", 10.0))
        poly = p.get("polygon")
        if poly and len(poly) >= 3:
            pts2d = [(float(pt[0]), float(pt[1])) for pt in poly]
        else:
            pts2d = [(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)]

        if not has_rot:
            min_x_p = min(pt[0] for pt in pts2d)
            max_x_p = max(pt[0] for pt in pts2d)
            min_y_p = min(pt[1] for pt in pts2d)
            max_y_p = max(pt[1] for pt in pts2d)
            return ((cx + min_x_p, cy + min_y_p, cz), (cx + max_x_p, cy + max_y_p, cz + h))

        R = _get_rotation_matrix(rx, ry, rz)
        pts3d: list[tuple[float, float, float]] = []
        for pt in pts2d:
            pts3d.append((pt[0], pt[1], 0.0))
            pts3d.append((pt[0], pt[1], h))
        world_pts = [
            (cx + rv[0], cy + rv[1], cz + rv[2])
            for rv in (_apply_rotation(R, v) for v in pts3d)
        ]
        return (
            (min(pt[0] for pt in world_pts), min(pt[1] for pt in world_pts), min(pt[2] for pt in world_pts)),
            (max(pt[0] for pt in world_pts), max(pt[1] for pt in world_pts), max(pt[2] for pt in world_pts)),
        )

    elif prim == "polyhedron":
        points = p.get("points") or p.get("vertices") or p.get("polygon") or []
        valid: list[tuple[float, float, float]] = []
        for pt in points:
            if isinstance(pt, (list, tuple)) and len(pt) >= 3:
                try:
                    valid.append((float(pt[0]), float(pt[1]), float(pt[2])))
                except (TypeError, ValueError):
                    pass
        if len(valid) >= 4:
            max_abs = max((abs(c) for v in valid for c in v), default=0.0)
            is_local = max_abs <= 2.0
            if has_rot:
                R = _get_rotation_matrix(rx, ry, rz)
                world_pts = []
                for v in valid:
                    loc_v = v if is_local else (v[0] - cx, v[1] - cy, v[2] - cz)
                    rv = _apply_rotation(R, loc_v)
                    world_pts.append((cx + rv[0], cy + rv[1], cz + rv[2]))
                return (
                    (min(pt[0] for pt in world_pts), min(pt[1] for pt in world_pts), min(pt[2] for pt in world_pts)),
                    (max(pt[0] for pt in world_pts), max(pt[1] for pt in world_pts), max(pt[2] for pt in world_pts)),
                )
            else:
                if is_local:
                    valid = [(cx + v[0], cy + v[1], cz + v[2]) for v in valid]
                return (
                    (min(v[0] for v in valid), min(v[1] for v in valid), min(v[2] for v in valid)),
                    (max(v[0] for v in valid), max(v[1] for v in valid), max(v[2] for v in valid)),
                )
        return ((cx - 10, cy - 10, cz), (cx + 10, cy + 10, cz + 10))

    else:
        return ((cx - 10, cy - 10, cz), (cx + 10, cy + 10, cz + 10))


get_part_aabb = _get_part_aabb


def check_assembly_interference(parts: list[dict[str, Any]]) -> list[tuple[str, str, str, float]]:
    """检测部件对之间的空间几何干涉与接触贴合关系。

    返回列表项: (part_A_id, part_B_id, 关系类型 'collision' | 'contact', 重叠体积或接触面积)
    """
    results: list[tuple[str, str, str, float]] = []
    if len(parts) < 2:
        return results

    aabbs = [(_get_part_aabb(p), str(p.get("id", f"part_{i}"))) for i, p in enumerate(parts)]
    for i in range(len(aabbs)):
        (min_a, max_a), id_a = aabbs[i]
        for j in range(i + 1, len(aabbs)):
            (min_b, max_b), id_b = aabbs[j]
            dx = min(max_a[0], max_b[0]) - max(min_a[0], min_b[0])
            dy = min(max_a[1], max_b[1]) - max(min_a[1], min_b[1])
            dz = min(max_a[2], max_b[2]) - max(min_a[2], min_b[2])

            if dx > 0.05 and dy > 0.05 and dz > 0.05:
                v_overlap = round(dx * dy * dz, 1)
                if v_overlap > 30.0:
                    results.append((id_a, id_b, "collision", v_overlap))
            elif dx > 0.05 and dy > 0.05 and abs(dz) < 0.2:
                contact_area = round(dx * dy, 1)
                results.append((id_a, id_b, "contact", contact_area))
    return results


def _estimate_primitive_volume_mm3(prim: str, size: list[float], part: dict[str, Any] | None = None) -> float:
    """粗略估算基础几何体的体积 (mm³)。

    polyhedron: 若 part 提供 points 顶点坐标, 用包围盒体积近似 (比 1000mm³ 硬编码更准确)。
    """
    prim = str(prim).lower()
    if prim in ("cube", "cuboid", "box"):
        return size[0] * size[1] * size[2] if len(size) >= 3 else 1000.0
    elif prim == "cylinder":
        d = size[0]
        h = size[1] if len(size) >= 2 else 10.0
        return math.pi * ((d / 2.0) ** 2) * h
    elif prim == "sphere":
        r = size[0] if size else 5.0
        return (4.0 / 3.0) * math.pi * (r ** 3)
    elif prim == "capsule":
        r = size[0] if size else 5.0
        h = size[1] if len(size) >= 2 else 20.0
        cyl_h = max(h - 2 * r, 0.0)
        return (math.pi * (r ** 2) * cyl_h) + ((4.0 / 3.0) * math.pi * (r ** 3))
    elif prim == "polyhedron" and part:
        # 从顶点坐标计算包围盒体积近似 (比硬编码 1000mm³ 更准确)
        points = part.get("points") or part.get("polygon") or []
        valid: list[tuple[float, float, float]] = []
        for pt in points:
            if isinstance(pt, (list, tuple)) and len(pt) >= 3:
                try:
                    valid.append((float(pt[0]), float(pt[1]), float(pt[2])))
                except (TypeError, ValueError):
                    pass
        if len(valid) >= 4:
            min_x = min(v[0] for v in valid)
            max_x = max(v[0] for v in valid)
            min_y = min(v[1] for v in valid)
            max_y = max(v[1] for v in valid)
            min_z = min(v[2] for v in valid)
            max_z = max(v[2] for v in valid)
            return max(1.0, (max_x - min_x) * (max_y - min_y) * (max_z - min_z))
        return 1000.0
    return 1000.0


def check_printability(spec: dict[str, Any], material: str = "PLA") -> PrintabilityReport:
    """对模型规格执行全方位 3D 打印工程自检，支持多耗材材质物性推导。"""
    report = PrintabilityReport()
    parts = spec.get("parts") or []
    if not parts:
        report.printable = True
        return report

    mat_key = str(material or spec.get("material") or "PLA").upper().strip()
    if mat_key not in MATERIAL_PROFILES:
        mat_key = "PLA"
    prof = MATERIAL_PROFILES[mat_key]
    density = float(prof["density"])
    shrinkage = float(prof["shrinkage_pct"])
    report.material = mat_key
    report.shrinkage_pct = shrinkage

    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")
    min_z, max_z = float("inf"), float("-inf")

    has_critical_thin = False

    for p in parts:
        pid = str(p.get("id", "part"))
        prim = p.get("primitive", "cube")
        size = normalize_size(prim, p)
        center = p.get("center", [0, 0, 0])
        cx = float(center[0]) if len(center) >= 1 else 0.0
        cy = float(center[1]) if len(center) >= 2 else 0.0
        cz = float(center[2]) if len(center) >= 3 else 0.0

        # 特征尺寸与壁厚检查
        if prim in ("cube", "cuboid", "box"):
            w, d, h = size[0], size[1], size[2]
            min_dim = min(w, d, h)
            if min_dim < 0.4:
                report.warnings.append(f"部件 '{pid}' 存在低于 0.4mm 的超薄特征 ({min_dim:.2f}mm)，常规喷嘴切片将被忽略")
                report.score -= 20
                has_critical_thin = True
            elif min_dim < 0.8:
                report.warnings.append(f"部件 '{pid}' 特征壁厚偏薄 ({min_dim:.2f}mm)，单圈走丝强度较脆弱")
                report.score -= 8

        elif prim in ("cylinder", "capsule"):
            d = size[0]
            h = size[2] if len(size) >= 3 else (size[1] if len(size) >= 2 else 10.0)
            if d < 0.8:
                report.warnings.append(f"部件 '{pid}' 直径过小 ({d:.2f}mm)，打印易折断")
                report.score -= 15
            elif d > 0 and h / d > 10.0:
                report.warnings.append(f"部件 '{pid}' 高径比过大 ({h/d:.1f})，细长立柱在高速移动时易晃动拉丝")
                report.tips.append(f"针对部件 '{pid}' 建议切片时开启外侧支撑或大底裙边 (Brim)")
                report.score -= 5

        elif prim == "sphere":
            r = size[0]
            if r < 1.0:
                report.warnings.append(f"球体 '{pid}' 半径极小 ({r:.2f}mm)")
                report.score -= 10

        # 统一由精确 3D Euler AABB 更新全模型外轮廓包围盒
        part_min, part_max = _get_part_aabb(p)
        min_x = min(min_x, part_min[0])
        max_x = max(max_x, part_max[0])
        min_y = min(min_y, part_min[1])
        max_y = max(max_y, part_max[1])
        min_z = min(min_z, part_min[2])
        max_z = max(max_z, part_max[2])

        # 孔洞检查
        holes = p.get("holes") or []
        for h_item in holes:
            h_prim = h_item.get("primitive", "cube")
            h_size = normalize_hole_size(h_prim, h_item)
            if h_prim == "cylinder":
                hole_d = h_size[0]
                if hole_d < 1.5:
                    report.tips.append(f"部件 '{pid}' 孔洞直径仅 {hole_d:.1f}mm，耗材热缩易偏紧，建议预留 0.2mm 孔径补偿")
                    report.score -= 3

    # 装配几何干涉与配合自检
    for id_a, id_b, rel_type, val in check_assembly_interference(parts)[:3]:
        if rel_type == "collision":
            report.warnings.append(f"部件 '{id_a}' 与 '{id_b}' 存在空间实体干涉重叠 (重叠 {val:.1f} mm³)，请注意装配间隙")
        elif rel_type == "contact":
            report.tips.append(f"部件 '{id_a}' 与 '{id_b}' 贴合接触良好 (接触面 {val:.1f} mm²)")

    # 包围盒评估
    dim_x = max(max_x - min_x, 0.0) if min_x != float("inf") else 0.0
    dim_y = max(max_y - min_y, 0.0) if min_y != float("inf") else 0.0
    dim_z = max(max_z - min_z, 0.0) if min_z != float("inf") else 0.0
    report.bounding_box = (round(dim_x, 1), round(dim_y, 1), round(dim_z, 1))

    max_bed_size = 256.0
    if max(dim_x, dim_y, dim_z) > max_bed_size:
        report.tips.append(f"模型外轮廓 ({max(dim_x, dim_y, dim_z):.1f}mm) 超出常见 256mm 热床，需对角线排布或切块打印")
        report.score -= 5

    # 材料特性指导提示
    if prof.get("enclosure_required"):
        report.tips.append(f"采用 {prof['name']} 耗材: 收缩率达 {shrinkage}%，建议开启封闭式保温箱 (Enclosure) 避免翘边开裂")
    if shrinkage >= 1.0:
        report.tips.append(f"针对 {mat_key} 高收缩特性，紧密装配孔建议增加 0.2~0.4mm 补偿，或切片时整体缩放 {100.0 + shrinkage:.1f}%")
    if mat_key == "TPU":
        report.tips.append("TPU 软胶耗材打印注意：请关闭回抽或限制在 1mm 以内，走丝速度建议调至 25~35mm/s")

    # 体积、耗材克重与耗时估算 (结合具体材料密度与流速计算)
    total_vol_mm3 = 0.0
    for p in parts:
        prim = p.get("primitive", "cube")
        size = normalize_size(prim, p)
        total_vol_mm3 += _estimate_primitive_volume_mm3(prim, size, part=p)
        for h in p.get("holes") or []:
            h_prim = h.get("primitive", "cube")
            h_size = normalize_hole_size(h_prim, h)
            total_vol_mm3 = max(total_vol_mm3 - _estimate_primitive_volume_mm3(h_prim, h_size), 0.0)

    vol_cm3 = round(total_vol_mm3 / 1000.0, 2)
    # 按 15% 内部填充 + 3 圈实心壁厚，折合实体填充率约 38%
    est_weight = round(vol_cm3 * density * 0.38, 1)
    speed_factor = 0.5 if mat_key == "TPU" else (0.8 if mat_key in ("ABS", "PC") else 1.0)
    est_hours = round(max((vol_cm3 * 0.38) / (12.0 * speed_factor), 0.2), 1)
    cost_cny = round((est_weight / 1000.0) * float(prof["cost_per_kg_cny"]), 2)
    cost_usd = round((est_weight / 1000.0) * float(prof["cost_per_kg_usd"]), 2)

    report.volume_cm3 = vol_cm3
    report.weight_g = est_weight
    report.print_time_hours = est_hours
    report.cost_cny = cost_cny
    report.cost_usd = cost_usd

    report.score = max(report.score, 10)
    report.printable = not has_critical_thin and report.score >= 50
    return report


def generate_print_guide(spec: dict[str, Any], report: PrintabilityReport, out_path: Path) -> Path:
    """自动生成工业级 Markdown 3D 打印切片与工艺指导书。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    name = spec.get("name", "model")
    desc = spec.get("description", "OpenSCAD AI 逆向 3D 模型")
    bbox = report.bounding_box
    mat_key = report.material if report.material in MATERIAL_PROFILES else "PLA"
    prof = MATERIAL_PROFILES[mat_key]

    # 针对性工艺推导
    has_slender = any("细长" in w for w in report.warnings)
    brim_rec = "强烈建议开启 5mm 外侧裙边 (Outer Brim)" if has_slender else "可选开启裙边 (Brim: 3mm 保持底座稳定)"
    layer_rec = "0.16mm (高精度) 或 0.20mm (标准)" if max(bbox) < 80 else "0.20mm (标准)"
    enclosure_rec = "必须开启封闭式保温箱 (Enclosure 60°C+)" if prof["enclosure_required"] else "敞开式打印机即可 (无需封箱)"

    warnings_md = "\n".join(f"- ⚠️ {w}" for w in report.warnings) if report.warnings else "- ✅ 全模型未检测到明显阻断性薄壁风险"
    tips_md = "\n".join(f"- 💡 {t}" for t in report.tips) if report.tips else f"- 💡 推荐使用标准热床温度 ({prof['bed_temp']}) 确保首层良好附着"
    user_tips = "\n".join(f"- 💡 {t}" for t in spec.get("print_tips") or [])

    content = f"""# 3D 打印切片与工艺指导书: {name}

> 由 OpenSCAD AI Agent 自动分析生成 · 适印性健康体检: **{report.summary()}**

## 📊 物理特性与耗材预估
| 指标 | 预估数值 | 评估参考 |
|---|---|---|
| **推荐材质** | `{prof['name']}` | 密度: {prof['density']} g/cm³ · 热收缩率: {prof['shrinkage_pct']}% |
| **包围盒跨度** | `{bbox[0]} × {bbox[1]} × {bbox[2]} mm` | 适配常见 256×256mm 打印机 |
| **预估实体体积** | `{report.volume_cm3:.1f} cm³` | 净体积几何积分 |
| **预估耗材克重** | `约 {report.weight_g:.1f} g` | 以 {mat_key} 材料 / 15% 填充率测算 |
| **预估打印耗时** | `约 {report.print_time_hours:.1f} 小时` | 针对 {mat_key} 工艺流速调校 |
| **预估耗材成本** | `约 ¥{report.cost_cny:.2f} 元 (${report.cost_usd:.2f})` | {mat_key} 耗材市场均价核算 |

---

## ⚙️ 推荐切片工艺参数 (Slicer Configuration - {mat_key})

| 切片参数项 | 推荐设定值 | 针对性工程建议 |
|---|---|---|
| **首选耗材类型** | **{prof['name']}** | {prof['features']} |
| **层高 (Layer Height)** | `{layer_rec}` | 首层建议 0.24mm，后续层 0.20mm |
| **外墙圈数 (Wall Loops)**| `3 圈 (约 1.2mm 壁厚)` | 提高部件外壳刚度 |
| **填充率与图案** | `15% ~ 20% (Gyroid 陀螺仪图案)` | 陀螺仪图案在各向受力均匀且不易与喷嘴刮擦 |
| **喷嘴温度** | `{prof['nozzle_temp']}` | 保证熔融层间高附着力 |
| **热床温度** | `{prof['bed_temp']}` | 防止首层翘边，打完自然脱落 |
| **冷却风扇** | `{prof['fan_speed']}` | 兼顾细部硬化与层间熔融融合度 |
| **封箱保温** | `{enclosure_rec}` | 控制环境温降与防冷缩 |
| **支撑策略 (Supports)** | 自动有机树状支撑 (Tree Support) | 悬空角阈值设为 45° |
| **裙边与附着 (Brim)** | `{brim_rec}` | 提高细小接触面的附着力 |

---

## 🩺 适印性体检诊断与预警
{warnings_md}

## 💡 切片优化与工艺要点
{tips_md}
{user_tips}
"""
    out_path.write_text(content, encoding="utf-8")
    return out_path


__all__ = ["PrintabilityReport", "check_printability", "generate_print_guide", "check_assembly_interference", "get_part_aabb", "MATERIAL_PROFILES"]
