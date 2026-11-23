"""真实物理碰撞检测与多构件装配干涉校验引擎 (AABB & OBB Collision & Interference Doctor)。

职责：
- 解决 3D 多构件 CAD 装配体与逆向工程中，肉眼难以察觉的内部体积实体硬重叠 (Hard Clash) 与负配合间隙；
- 多级几何碰撞过滤：
  1. 粗检测层 (Broad-Phase): 3D 轴对齐包围盒 (AABB) 空间快速剪枝与极值投影；
  2. 旋转定向包围盒 (OBB): 支持任意 3D Euler 旋转矩阵 ($R_z R_y R_x$) 顶点变换；
  3. 特征级检测层 (Narrow-Phase): 穿透孔洞轴套特征识别、孔径轴径配合公差核算 (Fit Clash) 与打印临界粘连间隙预警 (Clearance Warning)；
- 计算最小分离位移向量 (Minimum Translation Vector, MTV)，给出参数化自愈与装配修正指引；
- 与 Web 3D 视口和微服务 REST API 深度集成，直观高亮干涉部件对。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .log import get_logger as _log_module

_log = _log_module()


@dataclass
class AABB3D:
    """三维轴对齐包围盒。"""
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    @property
    def volume(self) -> float:
        return max(0.0, self.max_x - self.min_x) * max(0.0, self.max_y - self.min_y) * max(0.0, self.max_z - self.min_z)

    @property
    def center(self) -> list[float]:
        return [
            (self.min_x + self.max_x) / 2.0,
            (self.min_y + self.max_y) / 2.0,
            (self.min_z + self.max_z) / 2.0,
        ]

    def overlaps(self, other: AABB3D) -> bool:
        return (
            self.min_x <= other.max_x and self.max_x >= other.min_x and
            self.min_y <= other.max_y and self.max_y >= other.min_y and
            self.min_z <= other.max_z and self.max_z >= other.min_z
        )

    def intersection_volume(self, other: AABB3D) -> float:
        dx = max(0.0, min(self.max_x, other.max_x) - max(self.min_x, other.min_x))
        dy = max(0.0, min(self.max_y, other.max_y) - max(self.min_y, other.min_y))
        dz = max(0.0, min(self.max_z, other.max_z) - max(self.min_z, other.min_z))
        return dx * dy * dz

    def distance_to(self, other: AABB3D) -> float:
        """两包围盒之间的欧几里得空间最短物理间隙距离。"""
        dx = max(0.0, max(self.min_x - other.max_x, other.min_x - self.max_x))
        dy = max(0.0, max(self.min_y - other.max_y, other.min_y - self.max_y))
        dz = max(0.0, max(self.min_z - other.max_z, other.min_z - self.max_z))
        return math.sqrt(dx * dx + dy * dy + dz * dz)


@dataclass
class ClashRecord:
    """单个干涉冲突记录。"""
    part_a: str
    part_b: str
    clash_type: str  # "HARD_CLASH" (硬干涉), "FIT_INTERFERENCE" (孔轴配合过盈), "CLEARANCE_WARNING" (间隙过小预警)
    severity: str    # "CRITICAL", "WARNING", "INFO"
    overlap_volume_mm3: float
    clearance_mm: float
    mtv: list[float]  # 最小分离位移建议 [dx, dy, dz]
    description: str
    resolution_advice: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_a": self.part_a,
            "part_b": self.part_b,
            "clash_type": self.clash_type,
            "severity": self.severity,
            "overlap_volume_mm3": round(self.overlap_volume_mm3, 2),
            "clearance_mm": round(self.clearance_mm, 3),
            "mtv": [round(v, 2) for v in self.mtv],
            "description": self.description,
            "resolution_advice": self.resolution_advice,
        }


@dataclass
class InterferenceReport:
    """多构件装配干涉校验整体报告。"""
    model_name: str
    total_parts: int
    total_pairs_checked: int
    hard_clashes_count: int
    fit_interferences_count: int
    clearance_warnings_count: int
    clashes: list[ClashRecord]
    is_assembly_valid: bool
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "total_parts": self.total_parts,
            "total_pairs_checked": self.total_pairs_checked,
            "hard_clashes_count": self.hard_clashes_count,
            "fit_interferences_count": self.fit_interferences_count,
            "clearance_warnings_count": self.clearance_warnings_count,
            "is_assembly_valid": self.is_assembly_valid,
            "summary": self.summary,
            "clashes": [c.to_dict() for c in self.clashes],
        }


def _rotate_vertex(v: list[float], rz: float, ry: float, rx: float, origin: list[float]) -> list[float]:
    """绕指定原点应用 Euler 角旋转 (单位: 度)。"""
    dx = v[0] - origin[0]
    dy = v[1] - origin[1]
    dz = v[2] - origin[2]

    # X 轴旋转
    rad_x = math.radians(rx)
    cx, sx = math.cos(rad_x), math.sin(rad_x)
    y1 = dy * cx - dz * sx
    z1 = dy * sx + dz * cx
    x1 = dx

    # Y 轴旋转
    rad_y = math.radians(ry)
    cy, sy = math.cos(rad_y), math.sin(rad_y)
    x2 = x1 * cy + z1 * sy
    z2 = -x1 * sy + z1 * cy
    y2 = y1

    # Z 轴旋转
    rad_z = math.radians(rz)
    cz, sz = math.cos(rad_z), math.sin(rad_z)
    x3 = x2 * cz - y2 * sz
    y3 = x2 * sz + y2 * cz
    z3 = z2

    return [x3 + origin[0], y3 + origin[1], z3 + origin[2]]


def compute_part_aabb(part: dict[str, Any]) -> AABB3D:
    """计算零件在三维空间中的精确包围盒 (支持尺寸推导与旋转变换)。"""
    prim = str(part.get("primitive", "cube")).lower()
    sz = part.get("size") or [10.0, 10.0, 10.0]
    center = part.get("center") or [0.0, 0.0, 0.0]
    rotation = part.get("rotation") or [0.0, 0.0, 0.0]

    cx = float(center[0]) if len(center) >= 1 else 0.0
    cy = float(center[1]) if len(center) >= 2 else 0.0
    cz = float(center[2]) if len(center) >= 3 else 0.0

    if prim in ("cube", "cuboid", "box"):
        w = float(sz[0]) if len(sz) >= 1 else 10.0
        l = float(sz[1]) if len(sz) >= 2 else 10.0
        h = float(sz[2]) if len(sz) >= 3 else 10.0
    elif prim in ("cylinder", "cone", "gear", "timing_pulley", "thread"):
        dia = float(sz[0]) if len(sz) >= 1 else 10.0
        h = float(sz[1]) if len(sz) >= 2 else 10.0
        w, l = dia, dia
    elif prim == "sphere":
        dia = float(sz[0]) if len(sz) >= 1 else 10.0
        w, l, h = dia, dia, dia
    else:
        w = float(sz[0]) if len(sz) >= 1 else 10.0
        l = float(sz[1]) if len(sz) >= 2 else 10.0
        h = float(sz[2]) if len(sz) >= 3 else 10.0

    # 基础 8 个角点坐标 (在 OpenSCAD 坐标系中，translate(center) 将左下角放置在 center)
    base_vertices = [
        [cx, cy, cz],
        [cx + w, cy, cz],
        [cx + w, cy + l, cz],
        [cx, cy + l, cz],
        [cx, cy, cz + h],
        [cx + w, cy, cz + h],
        [cx + w, cy + l, cz + h],
        [cx, cy + l, cz + h],
    ]

    rx = float(rotation[0]) if len(rotation) >= 1 else 0.0
    ry = float(rotation[1]) if len(rotation) >= 2 else 0.0
    rz = float(rotation[2]) if len(rotation) >= 3 else 0.0

    if abs(rx) > 1e-3 or abs(ry) > 1e-3 or abs(rz) > 1e-3:
        rot_center = [cx, cy, cz]
        transformed = [_rotate_vertex(v, rz, ry, rx, rot_center) for v in base_vertices]
    else:
        transformed = base_vertices

    xs = [v[0] for v in transformed]
    ys = [v[1] for v in transformed]
    zs = [v[2] for v in transformed]

    return AABB3D(
        min_x=min(xs), min_y=min(ys), min_z=min(zs),
        max_x=max(xs), max_y=max(ys), max_z=max(zs),
    )


def check_assembly_interference(
    spec: dict[str, Any],
    clearance_threshold_mm: float = 0.30,
) -> InterferenceReport:
    """对装配体所有零件进行全量空间物理碰撞与装配干涉校验。

    - 辨识实体硬干涉 (HARD_CLASH);
    - 辨识孔轴配合过盈阻滞 (FIT_INTERFERENCE);
    - 预警低于 3D 打印热熔粘连阈值的危险间隙 (CLEARANCE_WARNING)。
    """
    model_name = spec.get("name", "assembly")
    parts = spec.get("parts") or []
    if len(parts) <= 1:
        return InterferenceReport(
            model_name=model_name,
            total_parts=len(parts),
            total_pairs_checked=0,
            hard_clashes_count=0,
            fit_interferences_count=0,
            clearance_warnings_count=0,
            clashes=[],
            is_assembly_valid=True,
            summary="装配体仅含 1 个或 0 个零件，无多构件干涉风险。",
        )

    # 预先计算各零件的包围盒与元数据
    part_boxes: list[tuple[dict[str, Any], AABB3D]] = []
    for p in parts:
        part_boxes.append((p, compute_part_aabb(p)))

    clashes: list[ClashRecord] = []
    pair_count = 0

    for i in range(len(part_boxes)):
        for j in range(i + 1, len(part_boxes)):
            pair_count += 1
            part_a, box_a = part_boxes[i]
            part_b, box_b = part_boxes[j]
            pid_a = str(part_a.get("id", f"part_{i+1}"))
            pid_b = str(part_b.get("id", f"part_{j+1}"))

            # 1. 检查是否重叠
            if box_a.overlaps(box_b):
                vol_inter = box_a.intersection_volume(box_b)
                if vol_inter > 1.0:  # 容忍微量浮点共面接触
                    # 检查是否为合法的孔轴嵌套 (Shaft in Hole)
                    is_shaft_hole, fit_detail = _check_shaft_in_hole(part_a, part_b)
                    if not is_shaft_hole:
                        # 反向检查 B 在 A 的孔内
                        is_shaft_hole, fit_detail = _check_shaft_in_hole(part_b, part_a)

                    if is_shaft_hole:
                        if fit_detail["has_interference"]:
                            # 孔轴尺寸过盈干涉
                            clashes.append(ClashRecord(
                                part_a=pid_a,
                                part_b=pid_b,
                                clash_type="FIT_INTERFERENCE",
                                severity="CRITICAL",
                                overlap_volume_mm3=vol_inter,
                                clearance_mm=fit_detail["delta_mm"],
                                mtv=[0.0, 0.0, 0.0],
                                description=f"配合过盈干涉: 轴件直径 ({fit_detail['shaft_d']:.2f}mm) 超出孔径 ({fit_detail['hole_d']:.2f}mm) 达 {abs(fit_detail['delta_mm']):.2f}mm，实物无法装配插入！",
                                resolution_advice=f"建议将孔径扩大至至少 {fit_detail['shaft_d'] + 0.20:.2f}mm (提供 +0.20mm 过渡装配间隙)。",
                            ))
                    else:
                        # 纯实体硬碰撞 (Hard Clash)
                        mtv = _calculate_mtv(box_a, box_b)
                        clashes.append(ClashRecord(
                            part_a=pid_a,
                            part_b=pid_b,
                            clash_type="HARD_CLASH",
                            severity="CRITICAL",
                            overlap_volume_mm3=vol_inter,
                            clearance_mm=0.0,
                            mtv=mtv,
                            description=f"实体空间重叠碰撞: 部件 [{pid_a}] 与 [{pid_b}] 存在重叠体积 {vol_inter:.1f} mm³！",
                            resolution_advice=f"建议沿分离向量移动 [{pid_b}] 至少 [{mtv[0]:.1f}, {mtv[1]:.1f}, {mtv[2]:.1f}] mm 或通过布尔差集切除干涉区域。",
                        ))
            else:
                # 2. 未重叠，检查间隙是否低于 3D 打印粘连警戒线
                gap = box_a.distance_to(box_b)
                if 0.0 < gap < clearance_threshold_mm:
                    clashes.append(ClashRecord(
                        part_a=pid_a,
                        part_b=pid_b,
                        clash_type="CLEARANCE_WARNING",
                        severity="WARNING",
                        overlap_volume_mm3=0.0,
                        clearance_mm=gap,
                        mtv=[0.0, 0.0, 0.0],
                        description=f"临界配合间隙过小: [{pid_a}] 与 [{pid_b}] 物理间隙仅 {gap:.2f}mm (< {clearance_threshold_mm:.2f}mm)，3D 打印受热膨胀极易融连粘死！",
                        resolution_advice=f"建议将两部件间距增加至至少 {clearance_threshold_mm:.2f}mm 以上以保证自由运动/可拆卸。",
                    ))

    hard_count = sum(1 for c in clashes if c.clash_type == "HARD_CLASH")
    fit_count = sum(1 for c in clashes if c.clash_type == "FIT_INTERFERENCE")
    warn_count = sum(1 for c in clashes if c.clash_type == "CLEARANCE_WARNING")
    is_valid = (hard_count == 0 and fit_count == 0)

    if is_valid and warn_count == 0:
        summary = f"✅ 装配体验证完美！已校验全部 {pair_count} 对零件组合，无空间碰撞、无配合过盈且间隙充裕。"
    elif is_valid:
        summary = f"⚡ 装配体无实体硬碰撞，但检出 {warn_count} 处临界粘连间隙预警，建议打印前适当放宽公差。"
    else:
        summary = f"⚠️ 装配体存在严重干涉冲突: 发现 {hard_count} 处实体硬碰撞、{fit_count} 处孔轴配合过盈！需修正几何位置。"

    _log.info("[interference] 干涉检查完成: 零件数=%d, 硬碰撞=%d, 过盈=%d, 警告=%d",
              len(parts), hard_count, fit_count, warn_count)

    return InterferenceReport(
        model_name=model_name,
        total_parts=len(parts),
        total_pairs_checked=pair_count,
        hard_clashes_count=hard_count,
        fit_interferences_count=fit_count,
        clearance_warnings_count=warn_count,
        clashes=clashes,
        is_assembly_valid=is_valid,
        summary=summary,
    )


def _check_shaft_in_hole(part_host: dict[str, Any], part_guest: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """检测 part_guest (轴/螺栓) 是否嵌套在 part_host 的预留孔洞中，并核算过盈公差。"""
    holes = part_host.get("holes") or []
    if not holes:
        return False, {}

    prim_g = str(part_guest.get("primitive", "")).lower()
    if prim_g not in ("cylinder", "cone", "thread", "pin", "screw", "bolt"):
        return False, {}

    sz_g = part_guest.get("size") or [10.0, 10.0]
    shaft_d = float(sz_g[0]) if len(sz_g) >= 1 else 10.0

    center_g = part_guest.get("center") or [0.0, 0.0, 0.0]
    gx, gy = float(center_g[0]), float(center_g[1])

    for h in holes:
        h_sz = h.get("size") or [10.0, 10.0]
        hole_d = float(h_sz[0]) if len(h_sz) >= 1 else 10.0
        h_center = h.get("center") or part_host.get("center") or [0.0, 0.0, 0.0]
        hx, hy = float(h_center[0]), float(h_center[1])

        # 若 XY 投影同轴度在 3.0mm 容差内，判定为同轴装配对
        dist_xy = math.sqrt((gx - hx) ** 2 + (gy - hy) ** 2)
        if dist_xy <= 3.0:
            delta = hole_d - shaft_d
            has_interference = (delta < -0.05)  # 孔径小于轴径超过 0.05mm
            return True, {
                "has_interference": has_interference,
                "hole_d": hole_d,
                "shaft_d": shaft_d,
                "delta_mm": delta,
            }

    return False, {}


def _calculate_mtv(box_a: AABB3D, box_b: AABB3D) -> list[float]:
    """计算将 box_b 移出 box_a 的最小分离平移向量 (MTV)。"""
    overlap_x = min(box_a.max_x - box_b.min_x, box_b.max_x - box_a.min_x)
    overlap_y = min(box_a.max_y - box_b.min_y, box_b.max_y - box_a.min_y)
    overlap_z = min(box_a.max_z - box_b.min_z, box_b.max_z - box_a.min_z)

    # 沿最小重叠维度的正负方向推出
    ca = box_a.center
    cb = box_b.center

    if overlap_x <= overlap_y and overlap_x <= overlap_z:
        sign = 1.0 if cb[0] >= ca[0] else -1.0
        return [sign * (overlap_x + 0.5), 0.0, 0.0]
    elif overlap_y <= overlap_x and overlap_y <= overlap_z:
        sign = 1.0 if cb[1] >= ca[1] else -1.0
        return [0.0, sign * (overlap_y + 0.5), 0.0]
    else:
        sign = 1.0 if cb[2] >= ca[2] else -1.0
        return [0.0, 0.0, sign * (overlap_z + 0.5)]
