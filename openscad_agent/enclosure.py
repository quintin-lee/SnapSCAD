"""SnapSCAD 电子外壳 PCB 导柱/散热开槽/面板开孔智能生成引擎 (Electronics Enclosure & PCB Engine)。

支持:
  - 工业常用开发板与控制器模板库 (Raspberry Pi 4B, Arduino Uno, ESP32 DevKit, Jetson Orin Nano 等)
  - 标准 I/O 面板开孔规范库 (USB-C, Micro-USB, USB-A, HDMI, Micro-HDMI, RJ45, DC Barrel, SD Slot)
  - 带径向加强筋的 PCB 定位支撑柱 (Standoff Bosses, 支持热熔嵌件孔与自攻底孔)
  - 高透气率散热开槽/蜂窝网孔/圆孔阵列 (Ventilation Grille Cutters)
  - 一键生成上下合盖双件式可 3D 打印电子外壳 (含止口唇边、角部螺丝柱、I/O 侧墙开窗与散热风道)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


#: 标准电子接口面板开孔尺寸库: (宽 width_mm, 高 height_mm, 圆角 radius_mm) - 已含 +0.6mm 打印装配余量
IO_PORT_PROFILES: dict[str, dict[str, float]] = {
    "USB_C": {"width": 9.6, "height": 3.8, "radius": 1.5},
    "MICRO_USB": {"width": 8.4, "height": 3.4, "radius": 0.8},
    "USB_B": {"width": 12.6, "height": 11.4, "radius": 0.6},
    "USB_A": {"width": 14.8, "height": 7.5, "radius": 0.6},
    "USB_A_DUAL": {"width": 15.2, "height": 16.0, "radius": 0.8},
    "HDMI": {"width": 15.6, "height": 6.2, "radius": 0.8},
    "MICRO_HDMI": {"width": 7.2, "height": 3.6, "radius": 0.6},
    "RJ45": {"width": 16.6, "height": 14.0, "radius": 0.6},
    "DC_BARREL": {"width": 9.4, "height": 11.4, "radius": 1.0},
    "AUDIO_35": {"width": 6.8, "height": 6.8, "radius": 3.4},
    "SD_SLOT": {"width": 15.6, "height": 3.0, "radius": 0.5},
}


#: 常用主流开发板 / PCB 模板尺寸与精准安装孔坐标系 (原点为 PCB 左下角 [0, 0])
PCB_TEMPLATES: dict[str, dict[str, Any]] = {
    "RASPBERRY_PI_4B": {
        "name": "Raspberry Pi 4B / 5 (85x56mm)",
        "width": 85.0,
        "height": 56.0,
        "pcb_thickness": 1.6,
        "corner_radius": 3.0,
        "screw_size": "M2.5",
        "standoff_h": 5.0,
        "component_clearance_z": 18.0,
        "mounting_holes": [
            {"x": 3.5, "y": 3.5, "d": 2.75},
            {"x": 61.5, "y": 3.5, "d": 2.75},
            {"x": 3.5, "y": 52.5, "d": 2.75},
            {"x": 61.5, "y": 52.5, "d": 2.75},
        ],
        "io_ports": [
            {"edge": "bottom", "offset": 11.2, "type": "USB_C", "z_offset": 0.0},
            {"edge": "bottom", "offset": 26.0, "type": "MICRO_HDMI", "z_offset": 0.0},
            {"edge": "bottom", "offset": 39.5, "type": "MICRO_HDMI", "z_offset": 0.0},
            {"edge": "bottom", "offset": 54.0, "type": "AUDIO_35", "z_offset": 0.0},
            {"edge": "right", "offset": 10.2, "type": "USB_A_DUAL", "z_offset": 0.0},
            {"edge": "right", "offset": 29.0, "type": "USB_A_DUAL", "z_offset": 0.0},
            {"edge": "right", "offset": 45.7, "type": "RJ45", "z_offset": 0.0},
        ],
    },
    "ARDUINO_UNO": {
        "name": "Arduino Uno R3 / R4 (68.6x53.3mm)",
        "width": 68.6,
        "height": 53.3,
        "pcb_thickness": 1.6,
        "corner_radius": 1.5,
        "screw_size": "M3",
        "standoff_h": 5.0,
        "component_clearance_z": 16.0,
        "mounting_holes": [
            {"x": 14.0, "y": 2.5, "d": 3.2},
            {"x": 15.2, "y": 50.8, "d": 3.2},
            {"x": 66.0, "y": 7.6, "d": 3.2},
            {"x": 66.0, "y": 35.6, "d": 3.2},
        ],
        "io_ports": [
            {"edge": "left", "offset": 38.0, "type": "USB_B", "z_offset": 0.0},
            {"edge": "left", "offset": 7.5, "type": "DC_BARREL", "z_offset": 0.0},
        ],
    },
    "ESP32_DEVKIT": {
        "name": "ESP32 DevKit / S3 通用主板 (55x28mm)",
        "width": 55.0,
        "height": 28.0,
        "pcb_thickness": 1.6,
        "corner_radius": 1.5,
        "screw_size": "M2",
        "standoff_h": 4.0,
        "component_clearance_z": 12.0,
        "mounting_holes": [
            {"x": 2.5, "y": 2.5, "d": 2.2},
            {"x": 52.5, "y": 2.5, "d": 2.2},
            {"x": 2.5, "y": 25.5, "d": 2.2},
            {"x": 52.5, "y": 25.5, "d": 2.2},
        ],
        "io_ports": [
            {"edge": "left", "offset": 14.0, "type": "USB_C", "z_offset": 0.0},
        ],
    },
    "JETSON_ORIN_NANO": {
        "name": "NVIDIA Jetson Orin Nano 载板 (100x79mm)",
        "width": 100.0,
        "height": 79.0,
        "pcb_thickness": 1.6,
        "corner_radius": 3.0,
        "screw_size": "M3",
        "standoff_h": 6.0,
        "component_clearance_z": 28.0,
        "mounting_holes": [
            {"x": 4.0, "y": 4.0, "d": 3.2},
            {"x": 96.0, "y": 4.0, "d": 3.2},
            {"x": 4.0, "y": 75.0, "d": 3.2},
            {"x": 96.0, "y": 75.0, "d": 3.2},
        ],
        "io_ports": [
            {"edge": "right", "offset": 15.0, "type": "DC_BARREL", "z_offset": 0.0},
            {"edge": "right", "offset": 35.0, "type": "USB_A_DUAL", "z_offset": 0.0},
            {"edge": "right", "offset": 55.0, "type": "RJ45", "z_offset": 0.0},
        ],
    },
}


@dataclass
class EnclosureConfig:
    """电子外壳全参数配置。"""
    template: str = "RASPBERRY_PI_4B"
    pcb_width: Optional[float] = None
    pcb_height: Optional[float] = None
    pcb_thickness: float = 1.6
    wall_thickness: float = 2.4
    floor_thickness: float = 2.4
    clearance_xy: float = 3.0
    standoff_height: Optional[float] = None
    inner_height: Optional[float] = None
    corner_radius: float = 4.0
    screw_size: str = "M3"
    use_heat_set: bool = True
    vent_pattern: str = "linear_slots"   # "linear_slots", "honeycomb", "circular_grid", "none"
    include_lid: bool = True
    split_layout: bool = True            # 底盒与顶盖并排摆放，方便单板打印
    color_bottom: str = "#334155"
    color_lid: str = "#475569"


# ==============================================================================
# 1. 独立特征构件生成器 (Standoffs & Ventilation Grilles)
# ==============================================================================

def generate_pcb_standoff_scad(
    height: float = 6.0,
    outer_d: float = 6.5,
    hole_d: float = 2.5,
    gusset_ribs: bool = True,
    base_flare_r: float = 1.5,
) -> str:
    """生成带径向加强筋的 PCB 支撑定位柱 (Standoff Boss) OpenSCAD 模块。"""
    rib_w = max(outer_d * 0.22, 1.2)
    rib_h = max(height * 0.65, 2.0)
    lines = [
        f"// PCB 定位支撑柱 (H={height:.1f}mm, 外径D={outer_d:.1f}mm, 内孔d={hole_d:.2f}mm)",
        f"module pcb_standoff(h = {height:.2f}, od = {outer_d:.2f}, hd = {hole_d:.2f}) {{",
        f"  difference() {{",
        f"    union() {{",
        f"      cylinder(d = od, h = h, $fn = 32, center = false);",
        f"      // 底部应力扩散倒圆锥底座",
        f"      cylinder(d1 = od + {base_flare_r * 2:.2f}, d2 = od, h = {min(height * 0.4, 2.5):.2f}, $fn = 32, center = false);",
    ]
    if gusset_ribs and height >= 3.5:
        lines.append(f"      // 四向十字径向加强筋 (防止打印层间剪切断裂)")
        lines.append(f"      for (a = [0, 90, 180, 270]) {{")
        lines.append(f"        rotate([0, 0, a])")
        lines.append(f"          translate([od * 0.35, -{rib_w / 2:.2f}, 0])")
        lines.append(f"            cube([od * 0.45, {rib_w:.2f}, {rib_h:.2f}], center = false);")
        lines.append(f"      }}")
    lines.extend([
        f"    }}",
        f"    // 内置螺丝底孔或热熔嵌件预留孔",
        f"    translate([0, 0, {max(height - max(height * 0.85, 4.0), 0.8):.2f}])",
        f"      cylinder(d = hd, h = {height + 1.0:.2f}, $fn = 24, center = false);",
        f"  }}",
        f"}}",
    ])
    return "\n".join(lines)


def generate_vent_grille_scad(
    zone_width: float = 50.0,
    zone_height: float = 36.0,
    cut_depth: float = 4.0,
    pattern: str = "linear_slots",
    slot_width: float = 2.6,
    pitch: float = 5.5,
) -> tuple[str, float]:
    """生成对流散热栅格负形切削体 OpenSCAD 模块，并估算有效开孔透气面积 (mm²)。

    :return: (OpenSCAD 模块字符串, 有效开孔总面积 mm²)
    """
    pat = str(pattern).lower().strip()
    open_area = 0.0

    lines = [
        f"// 散热栅格切削模块 (模式: {pat}, 区域: {zone_width:.1f}x{zone_height:.1f}mm)",
        f"module enclosure_vent_cutter() {{",
    ]

    if pat == "honeycomb":
        cell_d = max(slot_width * 2.2, 5.5)
        dx = max(cell_d * 0.95 + 1.8, 6.5)
        dy = dx * 0.866
        cols = max(int(zone_width // dx), 1)
        rows = max(int(zone_height // dy), 1)
        r_cell = cell_d / 2.0
        single_area = 2.598 * (r_cell ** 2)
        open_area = cols * rows * single_area
        start_x = -((cols - 1) * dx) / 2.0
        start_y = -((rows - 1) * dy) / 2.0
        lines.append(f"  for (r = [0 : {rows - 1}], c = [0 : {cols - 1}]) {{")
        lines.append(f"    translate([{start_x:.2f} + c * {dx:.2f} + (r % 2) * {dx / 2.0:.2f}, {start_y:.2f} + r * {dy:.2f}, -0.5])")
        lines.append(f"      cylinder(d = {cell_d:.2f}, h = {cut_depth + 1.0:.2f}, $fn = 6, center = false);")
        lines.append(f"  }}")

    elif pat == "circular_grid":
        hole_d = max(slot_width * 1.4, 3.5)
        step = max(pitch, hole_d + 2.0)
        cols = max(int(zone_width // step), 1)
        rows = max(int(zone_height // step), 1)
        open_area = cols * rows * math.pi * (hole_d / 2.0) ** 2
        start_x = -((cols - 1) * step) / 2.0
        start_y = -((rows - 1) * step) / 2.0
        lines.append(f"  for (r = [0 : {rows - 1}], c = [0 : {cols - 1}]) {{")
        lines.append(f"    translate([{start_x:.2f} + c * {step:.2f}, {start_y:.2f} + r * {step:.2f}, -0.5])")
        lines.append(f"      cylinder(d = {hole_d:.2f}, h = {cut_depth + 1.0:.2f}, $fn = 20, center = false);")
        lines.append(f"  }}")

    else:  # linear_slots
        step = max(pitch, slot_width + 2.2)
        count = max(int(zone_width // step), 1)
        slot_len = max(zone_height - slot_width, 6.0)
        r_tip = slot_width / 2.0
        open_area = count * (slot_width * slot_len + math.pi * (r_tip ** 2))
        start_x = -((count - 1) * step) / 2.0
        lines.append(f"  for (i = [0 : {count - 1}]) {{")
        lines.append(f"    translate([{start_x:.2f} + i * {step:.2f}, 0, -0.5])")
        lines.append(f"      hull() {{")
        lines.append(f"        translate([0, -{slot_len / 2.0:.2f}, 0]) cylinder(d = {slot_width:.2f}, h = {cut_depth + 1.0:.2f}, $fn = 20);")
        lines.append(f"        translate([0, {slot_len / 2.0:.2f}, 0]) cylinder(d = {slot_width:.2f}, h = {cut_depth + 1.0:.2f}, $fn = 20);")
        lines.append(f"      }}")
        lines.append(f"  }}")

    lines.append("}")
    return "\n".join(lines), open_area


# ==============================================================================
# 2. 完整电子外壳集成装配生成器 & 报告分析
# ==============================================================================

def _resolve_enclosure_geometry(cfg: EnclosureConfig) -> tuple[dict[str, Any], dict[str, Any]]:
    """解析 PCB 模板并推导外壳整体几何与散热数据。"""
    tmpl_key = str(cfg.template).upper().strip()
    tmpl = PCB_TEMPLATES.get(tmpl_key)

    if tmpl is not None:
        pcb_w = cfg.pcb_width if cfg.pcb_width is not None else float(tmpl["width"])
        pcb_h = cfg.pcb_height if cfg.pcb_height is not None else float(tmpl["height"])
        pcb_t = float(tmpl["pcb_thickness"])
        standoff_h = cfg.standoff_height if cfg.standoff_height is not None else float(tmpl["standoff_h"])
        comp_z = float(tmpl["component_clearance_z"])
        screw_sz = tmpl.get("screw_size", cfg.screw_size)
        holes = list(tmpl["mounting_holes"])
        io_ports = list(tmpl.get("io_ports") or [])
        board_name = tmpl["name"]
    else:
        pcb_w = cfg.pcb_width if cfg.pcb_width is not None else 70.0
        pcb_h = cfg.pcb_height if cfg.pcb_height is not None else 50.0
        pcb_t = cfg.pcb_thickness
        standoff_h = cfg.standoff_height if cfg.standoff_height is not None else 5.0
        comp_z = 16.0
        screw_sz = cfg.screw_size
        holes = [
            {"x": 4.0, "y": 4.0, "d": 3.2},
            {"x": pcb_w - 4.0, "y": 4.0, "d": 3.2},
            {"x": 4.0, "y": pcb_h - 4.0, "d": 3.2},
            {"x": pcb_w - 4.0, "y": pcb_h - 4.0, "d": 3.2},
        ]
        io_ports = [{"edge": "left", "offset": pcb_h / 2.0, "type": "USB_C", "z_offset": 0.0}]
        board_name = f"自定义通用 PCB ({pcb_w:.0f}x{pcb_h:.0f}mm)"

    wall = cfg.wall_thickness
    floor = cfg.floor_thickness
    clr = cfg.clearance_xy
    # 留出四角合盖螺丝耳空间 (若边缘间隙不足则外扩)
    eff_clr = max(clr, 5.5) if cfg.include_lid else clr

    inner_w = pcb_w + eff_clr * 2.0
    inner_h = pcb_h + eff_clr * 2.0
    outer_w = inner_w + wall * 2.0
    outer_h = inner_h + wall * 2.0
    cavity_z = cfg.inner_height if cfg.inner_height is not None else (standoff_h + pcb_t + comp_z)
    outer_z = floor + cavity_z

    meta = {
        "board_name": board_name,
        "pcb_size": [pcb_w, pcb_h, pcb_t],
        "standoff_h": standoff_h,
        "inner_size": [round(inner_w, 2), round(inner_h, 2), round(cavity_z, 2)],
        "outer_size": [round(outer_w, 2), round(outer_h, 2), round(outer_z, 2)],
        "wall_thickness": wall,
        "floor_thickness": floor,
        "eff_clearance_xy": eff_clr,
        "screw_size": screw_sz,
        "mounting_holes": holes,
        "io_ports": io_ports,
    }
    return meta, {"wall": wall, "floor": floor, "outer_w": outer_w, "outer_h": outer_h, "outer_z": outer_z, "inner_w": inner_w, "inner_h": inner_h}


def generate_enclosure_scad(config: EnclosureConfig | dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """生成完整双件式电子外壳（底壳 + PCB 支撑柱 + I/O 面板开孔 + 散热顶盖）OpenSCAD 代码与工程摘要。"""
    if isinstance(config, dict):
        cfg = EnclosureConfig(
            template=str(config.get("template", "RASPBERRY_PI_4B")),
            pcb_width=float(config["pcb_width"]) if config.get("pcb_width") is not None else None,
            pcb_height=float(config["pcb_height"]) if config.get("pcb_height") is not None else None,
            pcb_thickness=float(config.get("pcb_thickness", 1.6)),
            wall_thickness=float(config.get("wall_thickness", 2.4)),
            floor_thickness=float(config.get("floor_thickness", 2.4)),
            clearance_xy=float(config.get("clearance_xy", 3.0)),
            standoff_height=float(config["standoff_height"]) if config.get("standoff_height") is not None else None,
            inner_height=float(config["inner_height"]) if config.get("inner_height") is not None else None,
            corner_radius=float(config.get("corner_radius", 4.0)),
            screw_size=str(config.get("screw_size", "M3")),
            use_heat_set=bool(config.get("use_heat_set", True)),
            vent_pattern=str(config.get("vent_pattern", "linear_slots")),
            include_lid=bool(config.get("include_lid", True)),
            split_layout=bool(config.get("split_layout", True)),
        )
    else:
        cfg = config

    meta, dims = _resolve_enclosure_geometry(cfg)
    from .fasteners import FASTENER_CATALOG

    pcb_w, pcb_h, pcb_t = meta["pcb_size"]
    standoff_h = meta["standoff_h"]
    wall = dims["wall"]
    floor = dims["floor"]
    outer_w = dims["outer_w"]
    outer_h = dims["outer_h"]
    outer_z = dims["outer_z"]
    inner_w = dims["inner_w"]
    inner_h = dims["inner_h"]
    eff_clr = meta["eff_clearance_xy"]
    cr = max(min(cfg.corner_radius, outer_w * 0.2, outer_h * 0.2), 1.5)
    inner_cr = max(cr - wall, 0.6)

    # 紧固件规格解析 (PCB 柱孔 & 合盖角柱孔)
    pcb_f_cat = FASTENER_CATALOG.get(str(meta["screw_size"]).upper(), FASTENER_CATALOG["M2.5"])
    pcb_hole_d = pcb_f_cat["heat_set"]["hole_top_d"] if cfg.use_heat_set else pcb_f_cat["tap_hole"]
    pcb_boss_od = max(pcb_hole_d + 3.0, 6.2)

    lid_f_cat = FASTENER_CATALOG.get(str(cfg.screw_size).upper(), FASTENER_CATALOG["M3"])
    lid_hole_d = lid_f_cat["heat_set"]["hole_top_d"] if cfg.use_heat_set else lid_f_cat["tap_hole"]
    lid_clear_d = lid_f_cat["clearance_hole"]["normal"]

    standoff_mod = generate_pcb_standoff_scad(
        height=standoff_h,
        outer_d=pcb_boss_od,
        hole_d=pcb_hole_d,
        gusset_ribs=True,
    )

    vent_zone_w = max(pcb_w * 0.72, 20.0)
    vent_zone_h = max(pcb_h * 0.65, 18.0)
    vent_mod, vent_area = generate_vent_grille_scad(
        zone_width=vent_zone_w,
        zone_height=vent_zone_h,
        cut_depth=floor + 1.0,
        pattern=cfg.vent_pattern if cfg.vent_pattern != "none" else "linear_slots",
    )
    if cfg.vent_pattern == "none":
        vent_area = 0.0

    pcb_z_bottom = floor + standoff_h

    lines = [
        f"// ========================================================",
        f"// 智能电子产品外壳: {meta['board_name']}",
        f"// 外形尺寸: {outer_w:.1f} x {outer_h:.1f} x {outer_z:.1f} mm (壁厚={wall:.1f}mm)",
        f"// 内含: {len(meta['mounting_holes'])} 个 PCB 加强支撑柱, {len(meta['io_ports'])} 处 I/O 接口开孔",
        f"// ========================================================",
        f"$fn = 36;",
        f"",
        f"module rounded_box(w, h, z, r) {{",
        f"  hull() {{",
        f"    for (dx = [r, w - r], dy = [r, h - r])",
        f"      translate([dx, dy, 0]) cylinder(r = r, h = z, center = false);",
        f"  }}",
        f"}}",
        f"",
        standoff_mod,
        f"",
    ]
    if cfg.vent_pattern != "none":
        lines.append(vent_mod)
        lines.append("")

    # 底壳模块 (Bottom Enclosure Tray)
    lines.extend([
        f"module enclosure_bottom() {{",
        f"  color(\"{cfg.color_bottom}\") {{",
        f"    union() {{",
        f"      difference() {{",
        f"        // 1. 外壳轮廓主体",
        f"        rounded_box({outer_w:.2f}, {outer_h:.2f}, {outer_z:.2f}, {cr:.2f});",
        f"        // 2. 内部挖空腔体",
        f"        translate([{wall:.2f}, {wall:.2f}, {floor:.2f}])",
        f"          rounded_box({inner_w:.2f}, {inner_h:.2f}, {outer_z:.2f}, {inner_cr:.2f});",
    ])

    # I/O 面板侧墙开窗扣除
    for port in meta["io_ports"]:
        p_type = str(port.get("type", "USB_C")).upper()
        prof = IO_PORT_PROFILES.get(p_type, IO_PORT_PROFILES["USB_C"])
        pw, ph, pr = prof["width"], prof["height"], prof["radius"]
        edge = str(port.get("edge", "left")).lower()
        offset = float(port.get("offset", 15.0))
        z_center = pcb_z_bottom + pcb_t + ph / 2.0 + float(port.get("z_offset", 0.0))

        lines.append(f"        // I/O 面板开窗: {p_type} ({edge} 侧, {pw}x{ph}mm)")
        if edge == "bottom":
            cx = wall + eff_clr + offset
            lines.append(f"        translate([{cx - pw / 2.0:.2f}, -0.5, {z_center - ph / 2.0:.2f}])")
            lines.append(f"          cube([{pw:.2f}, {wall + 1.5:.2f}, {ph:.2f}], center = false);")
        elif edge == "top":
            cx = wall + eff_clr + offset
            lines.append(f"        translate([{cx - pw / 2.0:.2f}, {outer_h - wall - 0.5:.2f}, {z_center - ph / 2.0:.2f}])")
            lines.append(f"          cube([{pw:.2f}, {wall + 1.5:.2f}, {ph:.2f}], center = false);")
        elif edge == "left":
            cy = wall + eff_clr + offset
            lines.append(f"        translate([-0.5, {cy - pw / 2.0:.2f}, {z_center - ph / 2.0:.2f}])")
            lines.append(f"          cube([{wall + 1.5:.2f}, {pw:.2f}, {ph:.2f}], center = false);")
        elif edge == "right":
            cy = wall + eff_clr + offset
            lines.append(f"        translate([{outer_w - wall - 0.5:.2f}, {cy - pw / 2.0:.2f}, {z_center - ph / 2.0:.2f}])")
            lines.append(f"          cube([{wall + 1.5:.2f}, {pw:.2f}, {ph:.2f}], center = false);")

    lines.append(f"      }}")  # end difference outer shell

    # PCB 支撑立柱阵列 (放置于对应安装孔坐标)
    lines.append(f"      // 3. PCB 定位支撑柱阵列")
    for idx, mh in enumerate(meta["mounting_holes"]):
        mx = wall + eff_clr + float(mh["x"])
        my = wall + eff_clr + float(mh["y"])
        lines.append(f"      translate([{mx:.2f}, {my:.2f}, {floor:.2f}]) pcb_standoff();")

    # 顶盖合盖角柱 (四角内角嵌件柱)
    if cfg.include_lid:
        boss_r = 3.8
        corner_pts = [
            (wall + boss_r, wall + boss_r),
            (outer_w - wall - boss_r, wall + boss_r),
            (wall + boss_r, outer_h - wall - boss_r),
            (outer_w - wall - boss_r, outer_h - wall - boss_r),
        ]
        lines.append(f"      // 4. 顶盖固定角柱 (预留 {cfg.screw_size} 嵌件/底孔)")
        for cx, cy in corner_pts:
            lines.append(f"      translate([{cx:.2f}, {cy:.2f}, {floor:.2f}]) difference() {{")
            lines.append(f"        cylinder(r = {boss_r:.2f}, h = {outer_z - floor:.2f}, $fn = 24);")
            lines.append(f"        translate([0, 0, {max(outer_z - floor - 7.0, 1.0):.2f}])")
            lines.append(f"          cylinder(d = {lid_hole_d:.2f}, h = 8.0, $fn = 20);")
            lines.append(f"      }}")

    lines.extend([
        f"    }}",
        f"  }}",
        f"}}",
        f"",
    ])

    # 顶盖模块 (Top Enclosure Lid with Lip & Ventilation Grille)
    if cfg.include_lid:
        lip_h = 2.0
        lip_clr = 0.25
        boss_r = 3.8
        corner_pts = [
            (wall + boss_r, wall + boss_r),
            (outer_w - wall - boss_r, wall + boss_r),
            (wall + boss_r, outer_h - wall - boss_r),
            (outer_w - wall - boss_r, outer_h - wall - boss_r),
        ]
        lines.extend([
            f"module enclosure_lid() {{",
            f"  color(\"{cfg.color_lid}\") {{",
            f"    difference() {{",
            f"      union() {{",
            f"        // 顶盖盖板",
            f"        rounded_box({outer_w:.2f}, {outer_h:.2f}, {floor:.2f}, {cr:.2f});",
            f"        // 内嵌止口定位唇边 (扣合防滑移)",
            f"        translate([{wall + lip_clr:.2f}, {wall + lip_clr:.2f}, {floor:.2f}])",
            f"          difference() {{",
            f"            rounded_box({inner_w - lip_clr * 2:.2f}, {inner_h - lip_clr * 2:.2f}, {lip_h:.2f}, {max(inner_cr - lip_clr, 0.5):.2f});",
            f"            translate([1.4, 1.4, -0.5])",
            f"              rounded_box({max(inner_w - lip_clr * 2 - 2.8, 4.0):.2f}, {max(inner_h - lip_clr * 2 - 2.8, 4.0):.2f}, {lip_h + 1.0:.2f}, {max(inner_cr - 1.4, 0.4):.2f});",
            f"          }}",
            f"      }}",
        ])
        if cfg.vent_pattern != "none":
            lines.append(f"      // 顶盖散热通风格栅")
            lines.append(f"      translate([{outer_w / 2.0:.2f}, {outer_h / 2.0:.2f}, 0]) enclosure_vent_cutter();")

        lines.append(f"      // 四角紧固沉头通孔 ({cfg.screw_size})")
        for cx, cy in corner_pts:
            lines.append(f"      translate([{cx:.2f}, {cy:.2f}, -0.5]) cylinder(d = {lid_clear_d:.2f}, h = {floor + 1.5:.2f}, $fn = 24);")

        lines.extend([
            f"    }}",
            f"  }}",
            f"}}",
            f"",
        ])

    # 主装配布局
    lines.append("enclosure_bottom();")
    if cfg.include_lid:
        if cfg.split_layout:
            lines.append(f"translate([{outer_w + 12.0:.2f}, 0, 0]) enclosure_lid();")
        else:
            lines.append(f"translate([0, {outer_h:.2f}, {outer_z + floor:.2f}]) rotate([180, 0, 0]) enclosure_lid();")

    # 汇总报告字典
    report = {
        "board_name": meta["board_name"],
        "template": cfg.template,
        "outer_dimensions_mm": meta["outer_size"],
        "inner_cavity_mm": meta["inner_size"],
        "standoff_count": len(meta["mounting_holes"]),
        "standoff_height_mm": standoff_h,
        "io_cutouts_count": len(meta["io_ports"]),
        "vent_pattern": cfg.vent_pattern,
        "vent_open_area_mm2": round(vent_area, 1),
        "vent_open_ratio_pct": round((vent_area / max(outer_w * outer_h, 1.0)) * 100.0, 1),
        "recommended_hardware": f"{len(meta['mounting_holes'])}x {meta['screw_size']} PCB螺丝" + (f" + 4x {cfg.screw_size} 合盖螺丝" if cfg.include_lid else ""),
    }
    return "\n".join(lines), report
