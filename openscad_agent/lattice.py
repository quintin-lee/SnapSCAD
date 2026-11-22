"""轻量化减重拓扑与点阵晶格/蜂窝功能性微结构生成器 (Lattice & Cellular Structure Engine)。

职责：
- 解决无人机航模、机器人轻量化结构件、手柄握把缓震与减重痛点；
- 在 CAD 几何与源码层直接生成参数化内部减重晶格与表面蜂窝多孔结构；
- 支持多种工业级微结构单元：
  1. 六边形蜂窝 (Honeycomb / Hexagonal): 航空级最高刚度/重量比；
  2. 正交加强筋格栅 (Orthogrid): 经典薄壁蒙皮加固；
  3. 菱形骨架点阵 (Diamond / Octet): 三维等向承载与能量吸收；
  4. 旋转极小曲面 (Gyroid Infill Approximation): 适用于软胶 TPU 弹性缓震；
- 具备减重率与材料节省精确核算能力 (Weight Reduction % & Material Saved)；
- 与 OpenSCAD 实体布尔树 (intersection / difference) 深度融合。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .log import get_logger as _log_module

_log = _log_module()


@dataclass
class LatticeReport:
    """拓扑轻量化与晶格结构评估报告。"""
    pattern: str
    cell_size_mm: float
    wall_thickness_mm: float
    solid_volume_mm3: float
    lattice_volume_mm3: float
    weight_reduction_pct: float
    material_saved_grams: float
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "cell_size_mm": round(self.cell_size_mm, 2),
            "wall_thickness_mm": round(self.wall_thickness_mm, 2),
            "solid_volume_mm3": round(self.solid_volume_mm3, 2),
            "lattice_volume_mm3": round(self.lattice_volume_mm3, 2),
            "weight_reduction_pct": round(self.weight_reduction_pct, 1),
            "material_saved_grams": round(self.material_saved_grams, 2),
            "recommendations": self.recommendations,
        }


def generate_honeycomb_scad(
    width: float,
    length: float,
    height: float,
    cell_size: float = 8.0,
    wall_thickness: float = 1.2,
    module_name: str = "lattice_honeycomb",
) -> str:
    """生成参数化六边形蜂窝晶格减重模块的 OpenSCAD 代码。

    六边形正对边距为 cell_size，壁厚为 wall_thickness。
    """
    w = max(5.0, float(width))
    l = max(5.0, float(length))
    h = max(1.0, float(height))
    c = max(2.0, float(cell_size))
    t = max(0.4, float(wall_thickness))

    # 正六边形对边距 c -> 外接圆半径 R = c / sqrt(3)
    r_outer = c / math.sqrt(3.0)
    r_inner = max(0.5, (c - t) / math.sqrt(3.0))

    dx = c
    dy = 1.5 * r_outer
    nx = int(math.ceil(w / dx)) + 2
    ny = int(math.ceil(l / dy)) + 2

    lines = [
        f"// --- 航空级六边形蜂窝轻量化减重模块 ({module_name}) ---",
        f"module {module_name}() {{",
        f"    // 限制包围盒",
        f"    intersection() {{",
        f"        cube([{w:.2f}, {l:.2f}, {h:.2f}]);",
        f"        difference() {{",
        f"            cube([{w:.2f}, {l:.2f}, {h:.2f}]);",
        f"            for (row = [0 : {ny}]) {{",
        f"                y_pos = row * {dy:.3f};",
        f"                x_shift = (row % 2 == 1) ? ({dx:.3f} / 2) : 0;",
        f"                for (col = [-1 : {nx}]) {{",
        f"                    x_pos = col * {dx:.3f} + x_shift;",
        f"                    translate([x_pos, y_pos, -1])",
        f"                        cylinder(r={r_inner:.3f}, h={h + 2:.2f}, $fn=6);",
        f"                }}",
        f"            }}",
        f"        }}",
        f"    }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_orthogrid_scad(
    width: float,
    length: float,
    height: float,
    cell_size: float = 10.0,
    rib_thickness: float = 1.5,
    module_name: str = "lattice_orthogrid",
) -> str:
    """生成正交十字加强筋减重格栅模块的 OpenSCAD 代码。"""
    w = max(5.0, float(width))
    l = max(5.0, float(length))
    h = max(1.0, float(height))
    c = max(2.0, float(cell_size))
    t = max(0.4, float(rib_thickness))

    nx = int(math.ceil(w / c)) + 1
    ny = int(math.ceil(l / c)) + 1

    lines = [
        f"// --- 正交十字加强筋轻量化格栅模块 ({module_name}) ---",
        f"module {module_name}() {{",
        f"    intersection() {{",
        f"        cube([{w:.2f}, {l:.2f}, {h:.2f}]);",
        f"        difference() {{",
        f"            cube([{w:.2f}, {l:.2f}, {h:.2f}]);",
        f"            for (ix = [0 : {nx}]) {{",
        f"                for (iy = [0 : {ny}]) {{",
        f"                    translate([ix * {c:.2f} + {t:.2f}, iy * {c:.2f} + {t:.2f}, -1])",
        f"                        cube([{c - t:.2f}, {c - t:.2f}, {h + 2:.2f}]);",
        f"                }}",
        f"            }}",
        f"        }}",
        f"    }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_diamond_lattice_scad(
    width: float,
    length: float,
    height: float,
    cell_size: float = 10.0,
    strut_radius: float = 0.8,
    module_name: str = "lattice_diamond",
) -> str:
    """生成三维空间空间网格八面体/菱形微桁架点阵模块 (Diamond / Octet Lattice)。"""
    w = max(5.0, float(width))
    l = max(5.0, float(length))
    h = max(1.0, float(height))
    c = max(3.0, float(cell_size))
    sr = max(0.3, float(strut_radius))

    nx = int(math.ceil(w / c))
    ny = int(math.ceil(l / c))
    nz = int(math.ceil(h / c))

    lines = [
        f"// --- 三维菱形八面体空间点阵减重模块 ({module_name}) ---",
        f"module {module_name}() {{",
        f"    intersection() {{",
        f"        cube([{w:.2f}, {l:.2f}, {h:.2f}]);",
        f"        union() {{",
        f"            for (ix = [0 : {nx}]) {{",
        f"                for (iy = [0 : {ny}]) {{",
        f"                    for (iz = [0 : {nz}]) {{",
        f"                        translate([ix * {c:.2f}, iy * {c:.2f}, iz * {c:.2f}]) {{",
        f"                            // 空间斜向连接支柱",
        f"                            rotate([45, 0, 45]) cylinder(r={sr:.2f}, h={c * 1.414:.2f}, center=true, $fn=8);",
        f"                            rotate([-45, 0, 45]) cylinder(r={sr:.2f}, h={c * 1.414:.2f}, center=true, $fn=8);",
        f"                            rotate([0, 45, -45]) cylinder(r={sr:.2f}, h={c * 1.414:.2f}, center=true, $fn=8);",
        f"                        }}",
        f"                    }}",
        f"                }}",
        f"            }}",
        f"        }}",
        f"    }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_gyroid_infill_scad(
    width: float,
    length: float,
    height: float,
    cell_size: float = 12.0,
    wall_thickness: float = 1.0,
    module_name: str = "lattice_gyroid",
) -> str:
    """生成螺旋极小曲面 (Gyroid Approximation) 弹性缓震减重模块。"""
    w = max(5.0, float(width))
    l = max(5.0, float(length))
    h = max(1.0, float(height))
    c = max(4.0, float(cell_size))
    t = max(0.4, float(wall_thickness))

    nx = int(math.ceil(w / c))
    ny = int(math.ceil(l / c))

    lines = [
        f"// --- 螺旋极小曲面 (Gyroid Approximation) 弹性缓震晶格模块 ({module_name}) ---",
        f"module {module_name}() {{",
        f"    intersection() {{",
        f"        cube([{w:.2f}, {l:.2f}, {h:.2f}]);",
        f"        union() {{",
        f"            for (ix = [0 : {nx}]) {{",
        f"                for (iy = [0 : {ny}]) {{",
        f"                    translate([ix * {c:.2f} + {c/2:.2f}, iy * {c:.2f} + {c/2:.2f}, {h/2:.2f}]) {{",
        f"                        rotate([0, 0, (ix + iy) % 2 * 90]) {{",
        f"                            difference() {{",
        f"                                cylinder(r={c/2:.2f}, h={h:.2f}, center=true, $fn=16);",
        f"                                cylinder(r={c/2 - t:.2f}, h={h + 2:.2f}, center=true, $fn=16);",
        f"                            }}",
        f"                        }}",
        f"                    }}",
        f"                }}",
        f"            }}",
        f"        }}",
        f"    }}",
        f"}}",
    ]
    return "\n".join(lines)


def estimate_lattice_weight_savings(
    width: float,
    length: float,
    height: float,
    pattern: str = "honeycomb",
    cell_size: float = 8.0,
    wall_thickness: float = 1.2,
    density_g_cm3: float = 1.24,  # 默认 PLA 密度 1.24 g/cm3
) -> LatticeReport:
    """理论测算拓扑减重晶格的体积节约率与克重减少。"""
    solid_vol = width * length * height  # mm^3

    pattern_lower = pattern.lower()
    if "honeycomb" in pattern_lower or "hex" in pattern_lower:
        # 正六边形面积比例推导: A_solid / A_total = 1 - ((cell_size - wall) / cell_size)^2
        inner = max(0.1, cell_size - wall_thickness)
        void_ratio = min(0.92, (inner / cell_size) ** 2)
        solid_ratio = max(0.08, 1.0 - void_ratio)
        lattice_vol = solid_vol * solid_ratio
        recs = [
            "航空蜂窝结构：在 X-Y 面具备最佳抗压抗弯强度，建议载荷垂直于蜂窝轴向施加。",
            "推荐打印层高: 0.20mm，顶层/底层厚度建议保留 1.2mm (6 层) 以形成坚固闭合蒙皮。",
        ]
    elif "ortho" in pattern_lower or "grid" in pattern_lower:
        # 正交格栅面积比例: 2*(t/c) - (t/c)^2
        t_c = min(0.9, wall_thickness / cell_size)
        solid_ratio = min(0.95, max(0.08, 2.0 * t_c - (t_c ** 2)))
        lattice_vol = solid_vol * solid_ratio
        recs = [
            "正交格栅结构：具备双向对称抗扭曲性能，适合箱体底板与盖板减重。",
            "若使用 CNC 铣削加工，刀具半径需小于格栅内角半径。",
        ]
    elif "diamond" in pattern_lower or "octet" in pattern_lower:
        # 三维微桁架: 体积分数通常为 12% ~ 25%
        lattice_vol = solid_vol * 0.18
        recs = [
            "三维菱形八面体：全向等刚度晶格，打印时自支撑性能极佳 (斜角 45°)。",
            "推荐在轻量化机械臂、航模连接件等高动态过载部件上使用。",
        ]
    elif "gyroid" in pattern_lower:
        # 极小曲面结构: 体积分数约 15% ~ 22%
        lattice_vol = solid_vol * 0.20
        recs = [
            "极小曲面 Gyroid：内部无死角流体通道，适用于柔性材料 (TPU) 缓震衬垫或散热热交换器。",
            "各向同性弹性吸能，抗疲劳冲击性能超越传统单向点阵。",
        ]
    else:
        lattice_vol = solid_vol * 0.30
        recs = ["通用多孔晶格减重结构。"]

    saved_vol_mm3 = max(0.0, solid_vol - lattice_vol)
    weight_reduction = (saved_vol_mm3 / solid_vol) * 100.0 if solid_vol > 0 else 0.0

    # mm^3 -> cm^3 (* 1e-3) -> grams (* density)
    saved_grams = (saved_vol_mm3 * 1e-3) * density_g_cm3

    return LatticeReport(
        pattern=pattern,
        cell_size_mm=cell_size,
        wall_thickness_mm=wall_thickness,
        solid_volume_mm3=solid_vol,
        lattice_volume_mm3=lattice_vol,
        weight_reduction_pct=weight_reduction,
        material_saved_grams=saved_grams,
        recommendations=recs,
    )


def apply_lattice_lightweighting(
    spec: dict[str, Any],
    target_part_id: Optional[str] = None,
    pattern: str = "honeycomb",
    cell_size: float = 8.0,
    wall_thickness: float = 1.2,
    skin_thickness: float = 1.6,
) -> tuple[dict[str, Any], list[str]]:
    """在现有模型规格中为目标部件应用拓扑轻量化减重晶格。

    生成保留外部实体蒙皮 (skin_thickness) 的内部点阵减重结构。
    """
    import copy
    new_spec = copy.deepcopy(spec)
    actions: list[str] = []

    parts = new_spec.get("parts") or []
    if not parts:
        return new_spec, ["模型无有效零件，跳过减重处理"]

    target_part = None
    if target_part_id:
        target_part = next((p for p in parts if p.get("id") == target_part_id), None)
    if not target_part:
        # 默认选取体积最大的主体零件 (通常为 base / body)
        def _get_vol(p: dict[str, Any]) -> float:
            sz = p.get("size") or [10, 10, 10]
            if isinstance(sz, (list, tuple)):
                if len(sz) >= 3:
                    return float(sz[0]) * float(sz[1]) * float(sz[2])
                elif len(sz) == 2:
                    return math.pi * ((float(sz[0])/2)**2) * float(sz[1])
            return 100.0
        target_part = max(parts, key=_get_vol)

    pid = target_part.get("id", "body")
    target_part["lattice"] = {
        "pattern": pattern,
        "cell_size": cell_size,
        "wall_thickness": wall_thickness,
        "skin_thickness": skin_thickness,
    }
    actions.append(f"为部件 '{pid}' 注入 {pattern} 轻量化晶格 (单元大小={cell_size}mm, 壁厚={wall_thickness}mm, 蒙皮={skin_thickness}mm)")

    _log.info("[lattice] 拓扑轻量化晶格配置已注入: %s -> %s", pid, pattern)
    return new_spec, actions
