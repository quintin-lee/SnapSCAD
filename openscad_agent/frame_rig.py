"""SnapSCAD — 铝型材机架与标准直线导轨/丝杠运动平台生成引擎 (frame_rig.py)

功能:
  • EXTRUSION_PROFILES: 20系/30系/40系欧标铝型材截面规格库
  • LINEAR_COMPONENTS: 光轴+直线轴承、MGN12 线轨、T8 梯形丝杠、NEMA17 步进电机参数库
  • generate_extrusion_frame_scad(): 参数化矩形铝型材机架 (立柱+横梁+角码支架)
  • generate_linear_axis_scad(): 单轴直线运动组件装配 (光轴/线轨/丝杠三种方案)
  • create_motion_platform_preset(): 运动平台预设 (XY 工作台/CoreXY 龙门/Z 轴升降台)
  • FrameRigReport: 切料清单、总用量、BOM 物料表、估算质量
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 铝型材截面规格库  (slot 宽度, 中心孔径, 壁厚, 密度 kg/m)
# ---------------------------------------------------------------------------
EXTRUSION_PROFILES: dict[str, dict[str, Any]] = {
    "V2020": {
        "name": "欧标 V-slot 2020 铝型材",
        "width_mm": 20,
        "height_mm": 20,
        "slot_width_mm": 6.0,
        "bore_mm": 5.0,
        "wall_mm": 1.5,
        "linear_density_kg_m": 0.48,
        "color": "[0.75, 0.75, 0.80]",
    },
    "V2040": {
        "name": "欧标 V-slot 2040 铝型材",
        "width_mm": 20,
        "height_mm": 40,
        "slot_width_mm": 6.0,
        "bore_mm": 5.0,
        "wall_mm": 1.8,
        "linear_density_kg_m": 0.90,
        "color": "[0.75, 0.75, 0.80]",
    },
    "V3030": {
        "name": "欧标 V-slot 3030 铝型材",
        "width_mm": 30,
        "height_mm": 30,
        "slot_width_mm": 8.0,
        "bore_mm": 7.0,
        "wall_mm": 2.0,
        "linear_density_kg_m": 0.90,
        "color": "[0.72, 0.72, 0.78]",
    },
    "V4040": {
        "name": "欧标 V-slot 4040 铝型材",
        "width_mm": 40,
        "height_mm": 40,
        "slot_width_mm": 8.0,
        "bore_mm": 8.5,
        "wall_mm": 2.5,
        "linear_density_kg_m": 1.72,
        "color": "[0.72, 0.72, 0.78]",
    },
    "T2020": {
        "name": "T-slot 2020 铝型材",
        "width_mm": 20,
        "height_mm": 20,
        "slot_width_mm": 6.0,
        "bore_mm": 4.3,
        "wall_mm": 1.5,
        "linear_density_kg_m": 0.45,
        "color": "[0.78, 0.78, 0.82]",
    },
    "T3030": {
        "name": "T-slot 3030 铝型材",
        "width_mm": 30,
        "height_mm": 30,
        "slot_width_mm": 8.0,
        "bore_mm": 6.8,
        "wall_mm": 2.0,
        "linear_density_kg_m": 0.88,
        "color": "[0.78, 0.78, 0.82]",
    },
}

# ---------------------------------------------------------------------------
# 直线运动部件规格库
# ---------------------------------------------------------------------------
LINEAR_COMPONENTS: dict[str, dict[str, Any]] = {
    "ROD_8MM": {
        "name": "Ø8 光轴 + SK8 轴座 + SC8UU 直线轴承",
        "type": "smooth_rod",
        "rod_diameter_mm": 8.0,
        "bearing_type": "SC8UU",
        "support_type": "SK8",
        "carriage_len_mm": 25.0,
        "carriage_flange_mm": 34.0,
        "support_width_mm": 32.0,
        "support_height_mm": 23.0,
        "mass_per_m_kg": 0.395,
    },
    "ROD_12MM": {
        "name": "Ø12 光轴 + SK12 轴座 + SC12UU 直线轴承",
        "type": "smooth_rod",
        "rod_diameter_mm": 12.0,
        "bearing_type": "SC12UU",
        "support_type": "SK12",
        "carriage_len_mm": 36.0,
        "carriage_flange_mm": 42.0,
        "support_width_mm": 42.0,
        "support_height_mm": 32.0,
        "mass_per_m_kg": 0.888,
    },
    "MGN12H": {
        "name": "MGN12 微型线轨 + MGN12H 滑块",
        "type": "linear_rail",
        "rail_width_mm": 12.0,
        "rail_height_mm": 8.0,
        "carriage_len_mm": 45.0,
        "carriage_width_mm": 27.0,
        "carriage_height_mm": 10.0,
        "bolt_spacing_mm": 10.0,
        "rail_hole_pitch_mm": 15.0,
        "mass_per_m_kg": 0.42,
    },
    "T8_LEADSCREW": {
        "name": "T8 梯形铅螺杆 (2mm 螺距, 8mm 导程) + KFL08 轴承座",
        "type": "leadscrew",
        "screw_diameter_mm": 8.0,
        "pitch_mm": 2.0,
        "lead_mm": 8.0,
        "thread_starts": 4,
        "nut_flange_mm": 22.0,
        "nut_height_mm": 5.0,
        "bearing_type": "KFL08",
        "mass_per_m_kg": 0.395,
    },
    "T8_NUT": {
        "name": "T8 防间隙铜螺母 (消背隙双弹簧)",
        "type": "leadscrew_nut",
        "thread_diameter_mm": 8.0,
        "pitch_mm": 2.0,
        "lead_mm": 8.0,
        "flange_diameter_mm": 22.0,
        "flange_height_mm": 5.0,
        "body_diameter_mm": 10.0,
        "body_height_mm": 10.0,
    },
    "KFL08_BEARING": {
        "name": "KFL08 方形法兰轴承座 (8mm 轴孔)",
        "type": "bearing_block",
        "bore_mm": 8.0,
        "flange_width_mm": 42.0,
        "flange_height_mm": 20.0,
        "bolt_spacing_mm": 32.0,
    },
    "NEMA17": {
        "name": "NEMA17 步进电机 (42×42mm 机面, 4× M3 安装孔, 5mm 轴径)",
        "type": "stepper_motor",
        "face_mm": 42.0,
        "face_hole_circle_mm": 31.0,
        "shaft_diameter_mm": 5.0,
        "shaft_length_mm": 22.0,
        "mount_bolt": "M3",
        "mount_holes": 4,
        "body_length_mm": 48.0,
        "mass_kg": 0.28,
    },
}

# ---------------------------------------------------------------------------
# 报告数据类
# ---------------------------------------------------------------------------

@dataclass
class FrameRigReport:
    """铝型材机架/直线平台生成报告。"""
    cut_list: dict[str, list[float]] = field(default_factory=dict)
    total_extrusion_mm: float = 0.0
    component_bom: dict[str, int] = field(default_factory=dict)
    estimated_mass_kg: float = 0.0
    profile_type: str = "V2020"
    preset_name: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_type": self.profile_type,
            "preset_name": self.preset_name,
            "cut_list": self.cut_list,
            "total_extrusion_mm": round(self.total_extrusion_mm, 1),
            "component_bom": self.component_bom,
            "estimated_mass_kg": round(self.estimated_mass_kg, 3),
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# 辅助: 铝型材截面简化模型 (近似空心正方形横截面)
# ---------------------------------------------------------------------------

def _extrusion_cross_section_scad(profile: dict[str, Any], *, fn: int = 4) -> str:
    """返回铝型材截面 (外廓 - 中心孔) 的近似 2D 轮廓 OpenSCAD 片段。"""
    w = profile["width_mm"]
    h = profile["height_mm"]
    bore = profile["bore_mm"]
    slot = profile["slot_width_mm"]
    # 外廓
    lines = [
        "difference() {",
        f"  square([{w}, {h}], center=true);",
        # 中心孔
        f"  circle(d={bore}, $fn=16);",
        # 四面 T/V 槽 (简化为矩形缺口)
        f"  translate([0, {h/2 - slot*0.4}]) square([{slot}, {slot*0.8}], center=true);",
        f"  translate([0, {-h/2 + slot*0.4}]) square([{slot}, {slot*0.8}], center=true);",
        f"  translate([{w/2 - slot*0.4}, 0]) square([{slot*0.8}, {slot}], center=true);",
        f"  translate([{-w/2 + slot*0.4}, 0]) square([{slot*0.8}, {slot}], center=true);",
        "}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 生成矩形铝型材机架 OpenSCAD 代码
# ---------------------------------------------------------------------------

def generate_extrusion_frame_scad(config: dict[str, Any]) -> tuple[str, dict[str, list[float]]]:
    """生成参数化矩形铝型材机架 (立柱 + 水平横梁 + 角码支架) OpenSCAD 源码。

    Args:
        config: 机架配置字典，支持字段:
            profile_type (str): 型材型号，默认 "V2020"
            width_mm (float): 机架宽度 X，默认 300
            depth_mm (float): 机架深度 Y，默认 300
            height_mm (float): 机架高度 Z，默认 300
            corner_bracket (bool): 是否生成角码，默认 True

    Returns:
        tuple: (scad_code: str, cut_list: dict)
            cut_list 格式: {"V2020": [300.0, 300.0, ...]}
    """
    ptype = config.get("profile_type", "V2020")
    if ptype not in EXTRUSION_PROFILES:
        ptype = "V2020"
    prof = EXTRUSION_PROFILES[ptype]

    W = float(config.get("width_mm", 300.0))
    D = float(config.get("depth_mm", 300.0))
    H = float(config.get("height_mm", 300.0))
    want_bracket = bool(config.get("corner_bracket", True))

    pw = prof["width_mm"]
    ph = prof["height_mm"]
    col = prof["color"]

    # 切料清单: 4 根立柱 + 4 根 X 横梁 + 4 根 Y 横梁 (内净尺寸)
    vert_len = H - 2 * pw           # 立柱净高
    horiz_x = W - 2 * pw            # X 横梁净长
    horiz_y = D - 2 * pw            # Y 横梁净长

    cut_list: dict[str, list[float]] = {
        ptype: (
            [round(vert_len, 1)] * 4 +      # 垂直立柱
            [round(horiz_x, 1)] * 4 +       # 水平 X 方向横梁 (上下各 2)
            [round(horiz_y, 1)] * 4         # 水平 Y 方向横梁 (上下各 2)
        )
    }

    lines = [
        "// SnapSCAD 自动生成 — 铝型材机架",
        f"// 型材型号: {ptype}  规格: {pw}×{ph}mm",
        f"// 机架尺寸: W={W}mm × D={D}mm × H={H}mm",
        f"$fn = 32;",
        "",
        "module extrusion_beam(length) {",
        f"  color({col})",
        f"  linear_extrude(height=length)",
        f"  {_extrusion_cross_section_scad(prof)}",
        "}",
        "",
    ]

    # 角码 L 形支架模块 (简化版: 20mm × 20mm × 3mm 厚)
    if want_bracket:
        bk = pw
        lines += [
            "module corner_bracket() {",
            f"  color([0.4, 0.4, 0.4])",
            "  difference() {",
            f"    cube([{bk}, {bk}, {bk}], center=false);",
            f"    translate([{bk*0.4}, {bk*0.4}, -0.1])",
            f"      cube([{bk*0.3}, {bk*0.3}, {bk+0.2}]);",
            "  }",
            "}",
            "",
        ]

    # 四根垂直立柱 (角部)
    lines += [
        "// === 垂直立柱 ===",
        f"positions = [[0,0], [{W-pw},0], [0,{D-ph}], [{W-pw},{D-ph}]];",
        "for(p = positions) {",
        f"  translate([p[0], p[1], {pw}])",
        f"    rotate([0, 0, 0]) extrusion_beam({round(vert_len, 1)});",
        "}",
        "",
        "// === 底部水平横梁 ===",
        "// X 方向 (前后):",
        f"translate([{pw}, 0, 0]) rotate([270,0,0]) extrusion_beam({round(horiz_x, 1)});",
        f"translate([{pw}, {D}, 0]) rotate([270,0,0]) extrusion_beam({round(horiz_x, 1)});",
        "// Y 方向 (左右):",
        f"translate([0, {ph}, 0]) rotate([270,0,90]) extrusion_beam({round(horiz_y, 1)});",
        f"translate([{W}, {ph}, 0]) rotate([270,0,90]) extrusion_beam({round(horiz_y, 1)});",
        "",
        "// === 顶部水平横梁 ===",
        f"translate([{pw}, 0, {H-pw}]) rotate([270,0,0]) extrusion_beam({round(horiz_x, 1)});",
        f"translate([{pw}, {D}, {H-pw}]) rotate([270,0,0]) extrusion_beam({round(horiz_x, 1)});",
        f"translate([0, {ph}, {H-pw}]) rotate([270,0,90]) extrusion_beam({round(horiz_y, 1)});",
        f"translate([{W}, {ph}, {H-pw}]) rotate([270,0,90]) extrusion_beam({round(horiz_y, 1)});",
        "",
    ]

    if want_bracket:
        # 底部四个角码
        lines += [
            "// === 角码支架 (底部) ===",
            f"translate([0, 0, 0]) corner_bracket();",
            f"translate([{W-pw}, 0, 0]) corner_bracket();",
            f"translate([0, {D-ph}, 0]) corner_bracket();",
            f"translate([{W-pw}, {D-ph}, 0]) corner_bracket();",
            "",
        ]

    return "\n".join(lines), cut_list


# ---------------------------------------------------------------------------
# 生成单轴直线运动组件 OpenSCAD 代码
# ---------------------------------------------------------------------------

def generate_linear_axis_scad(
    axis: str,
    length_mm: float,
    component_type: str = "MGN12H",
) -> str:
    """生成单轴直线运动组件装配 OpenSCAD 源码。

    Args:
        axis: 轴方向，"x" / "y" / "z"
        length_mm: 导轨/光轴总长 (mm)
        component_type: 部件型号，来自 LINEAR_COMPONENTS

    Returns:
        OpenSCAD 源码字符串
    """
    if component_type not in LINEAR_COMPONENTS:
        component_type = "MGN12H"
    comp = LINEAR_COMPONENTS[component_type]
    ctype = comp["type"]

    # 轴旋转映射
    rot_map = {"x": [0, 90, 0], "y": [90, 0, 0], "z": [0, 0, 0]}
    rot = rot_map.get(axis.lower(), [0, 0, 0])
    rot_str = f"[{rot[0]}, {rot[1]}, {rot[2]}]"

    lines = [
        f"// SnapSCAD 自动生成 — 单轴直线运动组件",
        f"// 型号: {component_type}  方向: {axis.upper()}  长度: {length_mm}mm",
        f"$fn = 32;",
        "",
        f"rotate({rot_str}) {{",
    ]

    if ctype == "smooth_rod":
        rod_d = comp["rod_diameter_mm"]
        sup_w = comp["support_width_mm"]
        sup_h = comp["support_height_mm"]
        carr_l = comp["carriage_len_mm"]
        carr_fl = comp["carriage_flange_mm"]
        lines += [
            f"  // 光轴",
            f"  color([0.85, 0.85, 0.90])",
            f"  cylinder(d={rod_d}, h={length_mm});",
            "",
            f"  // SK 轴座 (两端)",
            f"  color([0.5, 0.5, 0.5])",
            f"  translate([0, 0, 10])",
            f"    cube([{sup_w}, {sup_h}, 20], center=true);",
            f"  color([0.5, 0.5, 0.5])",
            f"  translate([0, 0, {length_mm - 10}])",
            f"    cube([{sup_w}, {sup_h}, 20], center=true);",
            "",
            f"  // SC8UU/SC12UU 直线轴承滑台 (居中)",
            f"  color([0.70, 0.70, 0.75])",
            f"  translate([0, 0, {length_mm/2 - carr_l/2}])",
            f"    cube([{carr_fl}, {carr_fl}, {carr_l}], center=false);",
        ]

    elif ctype == "linear_rail":
        rw = comp["rail_width_mm"]
        rh = comp["rail_height_mm"]
        cw = comp["carriage_width_mm"]
        cl = comp["carriage_len_mm"]
        ch = comp["carriage_height_mm"]
        pitch = comp["rail_hole_pitch_mm"]
        n_holes = max(1, int(length_mm // pitch) - 1)
        lines += [
            f"  // MGN12 线轨导轨",
            f"  color([0.80, 0.80, 0.85])",
            f"  cube([{rw}, {rh}, {length_mm}], center=false);",
            "",
            f"  // 安装孔 ({n_holes} × Ø3.5mm)",
            f"  color([0.60, 0.60, 0.65])",
            f"  for(i = [1:{n_holes}])",
            f"    translate([{rw/2}, {rh+0.1}, i*{pitch}])",
            f"      rotate([90, 0, 0]) cylinder(d=3.5, h={rh+0.2});",
            "",
            f"  // MGN12H 滑块 (居中)",
            f"  color([0.65, 0.65, 0.70])",
            f"  translate([{(rw-cw)/2}, {rh}, {length_mm/2 - cl/2}])",
            f"    cube([{cw}, {ch}, {cl}], center=false);",
        ]

    elif ctype == "leadscrew":
        screw_d = comp["screw_diameter_mm"]
        nut_fl = comp["nut_flange_mm"]
        nut_h = comp["nut_height_mm"]
        lines += [
            f"  // T8 梯形丝杠",
            f"  color([0.90, 0.85, 0.70])",
            f"  cylinder(d={screw_d}, h={length_mm});",
            "",
            f"  // KFL08 轴承座 (两端)",
            f"  color([0.55, 0.55, 0.60])",
            f"  translate([0, 0, 5])",
            f"    cube([{comp.get('nut_flange_mm', 42)}, {comp.get('nut_flange_mm', 42)}, 20], center=true);",
            f"  translate([0, 0, {length_mm - 25}])",
            f"    cube([{comp.get('nut_flange_mm', 42)}, {comp.get('nut_flange_mm', 42)}, 20], center=true);",
            "",
            f"  // T8 防间隙铜螺母 (居中)",
            f"  color([0.85, 0.70, 0.30])",
            f"  translate([0, 0, {length_mm/2 - nut_h/2}])",
            f"    cylinder(d={nut_fl}, h={nut_h});",
            "",
            f"  // NEMA17 步进电机 (底端)",
            f"  color([0.20, 0.20, 0.22])",
            f"  translate([-21, -21, -{LINEAR_COMPONENTS['NEMA17']['body_length_mm']}])",
            f"    cube([42, 42, {LINEAR_COMPONENTS['NEMA17']['body_length_mm']}]);",
        ]
    else:
        lines += [f"  cylinder(d=10, h={length_mm}, $fn=32);"]

    lines += ["}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 运动平台预设
# ---------------------------------------------------------------------------

def create_motion_platform_preset(
    preset_name: str,
    width_mm: float = 300.0,
    depth_mm: float = 300.0,
    height_mm: float = 300.0,
    profile_type: str = "V2020",
    axis_component: str = "MGN12H",
) -> tuple[str, FrameRigReport]:
    """生成完整运动平台预设 OpenSCAD 源码与 BOM 报告。

    Args:
        preset_name: 预设名称，可选 "xy_table" / "corexy_frame" / "z_axis"
        width_mm: 平台宽度 X (mm)
        depth_mm: 平台深度 Y (mm)
        height_mm: 平台/立柱高度 Z (mm)
        profile_type: 铝型材型号
        axis_component: 轴向运动部件型号

    Returns:
        tuple: (scad_code: str, FrameRigReport)
    """
    if profile_type not in EXTRUSION_PROFILES:
        profile_type = "V2020"
    prof = EXTRUSION_PROFILES[profile_type]
    linear_density = prof["linear_density_kg_m"]

    # 机架底座
    frame_config = {
        "profile_type": profile_type,
        "width_mm": width_mm,
        "depth_mm": depth_mm,
        "height_mm": height_mm,
        "corner_bracket": True,
    }
    frame_scad, cut_list = generate_extrusion_frame_scad(frame_config)

    # 计算总铝型材用量
    total_mm = sum(sum(v) for v in cut_list.values())
    extrusion_mass_kg = (total_mm / 1000.0) * linear_density

    # BOM 物料表基础
    bom: dict[str, int] = {
        "M5×10 T形槽螺母+螺栓": 16,
        "90° L形铝角码": 8,
    }

    report_warnings: list[str] = []

    # 轴代码
    axis_parts: list[str] = []

    if preset_name == "xy_table":
        # XY 两轴工作台: X 轴 + Y 轴各一套
        x_scad = generate_linear_axis_scad("x", width_mm - 60, axis_component)
        y_scad = generate_linear_axis_scad("y", depth_mm - 60, axis_component)
        axis_parts = [
            "// === X 轴直线运动组件 ===",
            f"translate([30, {depth_mm/2}, {height_mm + 10}])",
            "{ " + x_scad.replace("\n", "\n  ") + " }",
            "",
            "// === Y 轴直线运动组件 ===",
            f"translate([{width_mm/2}, 30, {height_mm + 10}])",
            "{ " + y_scad.replace("\n", "\n  ") + " }",
        ]
        bom.update({
            axis_component + " × X轴": 1,
            axis_component + " × Y轴": 1,
            "NEMA17 步进电机": 2,
        })
        if axis_component == "T8_LEADSCREW":
            bom["T8 防间隙铜螺母"] = 2
            bom["T8 联轴器 Ø5-Ø8"] = 2

    elif preset_name == "corexy_frame":
        # CoreXY 龙门框架: 顶部两根 X/Y MGN12 轨道
        x_scad = generate_linear_axis_scad("x", width_mm - 40, "MGN12H")
        y_scad = generate_linear_axis_scad("y", depth_mm - 40, "MGN12H")
        axis_parts = [
            "// === CoreXY 顶部 X 轨道 ===",
            f"translate([20, {depth_mm * 0.25}, {height_mm - 20}])",
            "{ " + x_scad.replace("\n", "\n  ") + " }",
            "",
            "// === CoreXY 顶部 Y 轨道 ===",
            f"translate([{width_mm * 0.25}, 20, {height_mm - 20}])",
            "{ " + y_scad.replace("\n", "\n  ") + " }",
        ]
        bom.update({
            "MGN12H 线轨 × X": 1,
            "MGN12H 线轨 × Y": 1,
            "NEMA17 步进电机": 2,
            "GT2 同步带 (m)": int(math.ceil((width_mm + depth_mm) * 2 / 1000.0)),
            "GT2 20T 同步轮": 2,
        })
        if width_mm > 400 or depth_mm > 400:
            report_warnings.append(f"CoreXY 单侧跨度超过 400mm，建议使用 V2040 双槽加强横梁。")

    elif preset_name == "z_axis":
        # 单 Z 轴丝杠升降台
        z_scad = generate_linear_axis_scad("z", height_mm - 40, "T8_LEADSCREW")
        axis_parts = [
            "// === Z 轴丝杠升降台 ===",
            f"translate([{width_mm/2}, {depth_mm/2}, 20])",
            "{ " + z_scad.replace("\n", "\n  ") + " }",
        ]
        bom.update({
            "T8 梯形丝杠 × Z轴": 1,
            "T8 防间隙铜螺母": 1,
            "KFL08 轴承座": 2,
            "NEMA17 步进电机": 1,
            "T8 联轴器 Ø5-Ø8": 1,
        })

    else:
        report_warnings.append(f"未知预设 '{preset_name}'，已生成纯机架结构。")

    # 拼合完整 SCAD
    full_scad_lines = [
        f"// SnapSCAD — 运动平台预设: {preset_name}",
        f"// 型材: {profile_type}  规格: {width_mm}×{depth_mm}×{height_mm}mm",
        "$fn = 32;",
        "",
        "// === 铝型材机架底座 ===",
        frame_scad,
        "",
    ] + axis_parts

    full_scad = "\n".join(full_scad_lines)

    report = FrameRigReport(
        cut_list=cut_list,
        total_extrusion_mm=round(total_mm, 1),
        component_bom=bom,
        estimated_mass_kg=round(extrusion_mass_kg, 3),
        profile_type=profile_type,
        preset_name=preset_name,
        warnings=report_warnings,
    )
    return full_scad, report
