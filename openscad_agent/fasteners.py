"""SnapSCAD 螺栓标准件智能紧固库与垫圈/沉头孔自动配准引擎 (Standard Fasteners & Counterbore Engine)。

支持:
  - 工业标准紧固件数据库: M2 ~ M10
    * ISO 4762 / DIN 912 (内六角圆柱头螺钉 Socket Head Cap Screw)
    * ISO 10642 / DIN 7991 (内六角沉头螺钉 Countersunk Flat Head Screw, 90° 锥角)
    * ISO 7380 (内六角半圆头螺钉 Button Head Screw)
    * ISO 4032 / DIN 934 (标准公制六角螺母 Hex Nut)
    * ISO 7089 / DIN 125 (公制标准平垫圈 Flat Washer)
    * 热熔铜螺母 (3D 打印标准预埋螺母 Heat-Set Threaded Inserts, Voron / Ruthex 标准)
  - 独立标准件 OpenSCAD 实体几何代码生成 (generate_bolt_scad, generate_nut_scad, etc.)
  - 差集负形切削体代码生成 (generate_counterbore_cutter, generate_countersink_cutter, generate_hex_pocket_cutter, etc.)
  - 智能孔径自动配准与紧固件选型 (match_fastener_for_hole)
  - 紧固件物料清单自动提取与汇总 (generate_fasteners_bom)
  - 自动沉头孔升级补丁 (apply_auto_counterbores)
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class FastenerStandard(str, Enum):
    ISO_4762 = "ISO_4762"      # 内六角圆柱头螺钉 (DIN 912)
    ISO_10642 = "ISO_10642"    # 内六角沉头螺钉 (DIN 7991, 90°)
    ISO_7380 = "ISO_7380"      # 内六角半圆头螺钉 (Button Head)
    ISO_4032 = "ISO_4032"      # 标准六角螺母 (DIN 934)
    ISO_7089 = "ISO_7089"      # 标准平垫圈 (DIN 125)
    HEAT_SET = "HEAT_SET"      # 热熔铜螺母 (Heat-Set Threaded Insert)


#: 标准公制紧固件尺寸数据库 (M2 ~ M10)
FASTENER_CATALOG: dict[str, dict[str, Any]] = {
    "M2": {
        "nominal_d": 2.0,
        "pitch": 0.4,
        "clearance_hole": {"close": 2.2, "normal": 2.4, "loose": 2.6},
        "tap_hole": 1.6,
        "iso4762": {
            "head_d": 3.8, "head_k": 2.0, "hex_s": 1.5, "hex_t": 1.0,
            "counterbore_d": 4.4, "counterbore_depth": 2.4,
        },
        "iso10642": {
            "head_d": 4.0, "head_k": 1.2, "hex_s": 1.25, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 3.5, "head_k": 1.3, "hex_s": 1.25,
        },
        "iso4032": {
            "hex_s": 4.0, "thickness_m": 1.6, "pocket_s": 4.25, "pocket_depth": 2.0,
        },
        "iso7089": {
            "inner_d": 2.2, "outer_d": 5.0, "thickness_h": 0.3,
        },
        "heat_set": {
            "outer_d": 3.2, "length": 3.6, "hole_top_d": 3.0, "hole_bot_d": 2.8, "hole_min_depth": 4.4,
        },
    },
    "M2.5": {
        "nominal_d": 2.5,
        "pitch": 0.45,
        "clearance_hole": {"close": 2.7, "normal": 2.9, "loose": 3.1},
        "tap_hole": 2.05,
        "iso4762": {
            "head_d": 4.5, "head_k": 2.5, "hex_s": 2.0, "hex_t": 1.1,
            "counterbore_d": 5.2, "counterbore_depth": 2.9,
        },
        "iso10642": {
            "head_d": 5.0, "head_k": 1.5, "hex_s": 1.5, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 4.7, "head_k": 1.5, "hex_s": 1.5,
        },
        "iso4032": {
            "hex_s": 5.0, "thickness_m": 2.0, "pocket_s": 5.3, "pocket_depth": 2.4,
        },
        "iso7089": {
            "inner_d": 2.7, "outer_d": 6.0, "thickness_h": 0.5,
        },
        "heat_set": {
            "outer_d": 3.6, "length": 5.0, "hole_top_d": 3.4, "hole_bot_d": 3.2, "hole_min_depth": 5.8,
        },
    },
    "M3": {
        "nominal_d": 3.0,
        "pitch": 0.5,
        "clearance_hole": {"close": 3.2, "normal": 3.4, "loose": 3.6},
        "tap_hole": 2.5,
        "iso4762": {
            "head_d": 5.5, "head_k": 3.0, "hex_s": 2.5, "hex_t": 1.3,
            "counterbore_d": 6.2, "counterbore_depth": 3.4,
        },
        "iso10642": {
            "head_d": 6.0, "head_k": 1.7, "hex_s": 2.0, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 5.7, "head_k": 1.65, "hex_s": 2.0,
        },
        "iso4032": {
            "hex_s": 5.5, "thickness_m": 2.4, "pocket_s": 5.8, "pocket_depth": 2.8,
        },
        "iso7089": {
            "inner_d": 3.2, "outer_d": 7.0, "thickness_h": 0.5,
        },
        "heat_set": {
            "outer_d": 4.6, "length": 5.7, "hole_top_d": 4.0, "hole_bot_d": 3.8, "hole_min_depth": 6.5,
        },
    },
    "M4": {
        "nominal_d": 4.0,
        "pitch": 0.7,
        "clearance_hole": {"close": 4.3, "normal": 4.5, "loose": 4.8},
        "tap_hole": 3.3,
        "iso4762": {
            "head_d": 7.0, "head_k": 4.0, "hex_s": 3.0, "hex_t": 2.0,
            "counterbore_d": 8.0, "counterbore_depth": 4.4,
        },
        "iso10642": {
            "head_d": 8.0, "head_k": 2.3, "hex_s": 2.5, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 7.6, "head_k": 2.2, "hex_s": 2.5,
        },
        "iso4032": {
            "hex_s": 7.0, "thickness_m": 3.2, "pocket_s": 7.35, "pocket_depth": 3.6,
        },
        "iso7089": {
            "inner_d": 4.3, "outer_d": 9.0, "thickness_h": 0.8,
        },
        "heat_set": {
            "outer_d": 6.3, "length": 8.1, "hole_top_d": 5.6, "hole_bot_d": 5.4, "hole_min_depth": 8.9,
        },
    },
    "M5": {
        "nominal_d": 5.0,
        "pitch": 0.8,
        "clearance_hole": {"close": 5.3, "normal": 5.5, "loose": 5.8},
        "tap_hole": 4.2,
        "iso4762": {
            "head_d": 8.5, "head_k": 5.0, "hex_s": 4.0, "hex_t": 2.5,
            "counterbore_d": 10.0, "counterbore_depth": 5.5,
        },
        "iso10642": {
            "head_d": 10.0, "head_k": 2.8, "hex_s": 3.0, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 9.5, "head_k": 2.75, "hex_s": 3.0,
        },
        "iso4032": {
            "hex_s": 8.0, "thickness_m": 4.7, "pocket_s": 8.4, "pocket_depth": 5.2,
        },
        "iso7089": {
            "inner_d": 5.3, "outer_d": 10.0, "thickness_h": 1.0,
        },
        "heat_set": {
            "outer_d": 7.1, "length": 9.5, "hole_top_d": 6.4, "hole_bot_d": 6.2, "hole_min_depth": 10.3,
        },
    },
    "M6": {
        "nominal_d": 6.0,
        "pitch": 1.0,
        "clearance_hole": {"close": 6.4, "normal": 6.6, "loose": 7.0},
        "tap_hole": 5.0,
        "iso4762": {
            "head_d": 10.0, "head_k": 6.0, "hex_s": 5.0, "hex_t": 3.0,
            "counterbore_d": 11.5, "counterbore_depth": 6.6,
        },
        "iso10642": {
            "head_d": 12.0, "head_k": 3.3, "hex_s": 4.0, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 10.5, "head_k": 3.3, "hex_s": 4.0,
        },
        "iso4032": {
            "hex_s": 10.0, "thickness_m": 5.2, "pocket_s": 10.45, "pocket_depth": 5.7,
        },
        "iso7089": {
            "inner_d": 6.4, "outer_d": 12.0, "thickness_h": 1.6,
        },
        "heat_set": {
            "outer_d": 8.7, "length": 12.7, "hole_top_d": 7.9, "hole_bot_d": 7.7, "hole_min_depth": 13.5,
        },
    },
    "M8": {
        "nominal_d": 8.0,
        "pitch": 1.25,
        "clearance_hole": {"close": 8.4, "normal": 9.0, "loose": 9.5},
        "tap_hole": 6.8,
        "iso4762": {
            "head_d": 13.0, "head_k": 8.0, "hex_s": 6.0, "hex_t": 4.0,
            "counterbore_d": 15.0, "counterbore_depth": 8.8,
        },
        "iso10642": {
            "head_d": 16.0, "head_k": 4.4, "hex_s": 5.0, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 14.0, "head_k": 4.4, "hex_s": 5.0,
        },
        "iso4032": {
            "hex_s": 13.0, "thickness_m": 6.8, "pocket_s": 13.5, "pocket_depth": 7.4,
        },
        "iso7089": {
            "inner_d": 8.4, "outer_d": 16.0, "thickness_h": 1.6,
        },
        "heat_set": {
            "outer_d": 10.2, "length": 12.7, "hole_top_d": 9.4, "hole_bot_d": 9.2, "hole_min_depth": 13.5,
        },
    },
    "M10": {
        "nominal_d": 10.0,
        "pitch": 1.5,
        "clearance_hole": {"close": 10.5, "normal": 11.0, "loose": 12.0},
        "tap_hole": 8.5,
        "iso4762": {
            "head_d": 16.0, "head_k": 10.0, "hex_s": 8.0, "hex_t": 5.0,
            "counterbore_d": 18.0, "counterbore_depth": 10.8,
        },
        "iso10642": {
            "head_d": 20.0, "head_k": 5.5, "hex_s": 6.0, "angle": 90.0,
        },
        "iso7380": {
            "head_d": 17.5, "head_k": 5.3, "hex_s": 6.0,
        },
        "iso4032": {
            "hex_s": 16.0, "thickness_m": 8.4, "pocket_s": 16.6, "pocket_depth": 9.0,
        },
        "iso7089": {
            "inner_d": 10.5, "outer_d": 20.0, "thickness_h": 2.0,
        },
        "heat_set": {
            "outer_d": 12.5, "length": 15.0, "hole_top_d": 11.6, "hole_bot_d": 11.3, "hole_min_depth": 16.0,
        },
    },
}


def _norm_size(size: str) -> str:
    """归一化螺纹公称尺寸 (如 'm3' -> 'M3', '3' -> 'M3')。"""
    s = str(size).strip().upper()
    if not s.startswith("M") and s.replace(".", "").isdigit():
        s = f"M{s}"
    return s


def _norm_standard(std: str) -> FastenerStandard:
    """归一化紧固件标准标识符。"""
    s = str(std).strip().upper().replace("-", "_").replace(" ", "_")
    if "4762" in s or "912" in s or "SOCKET" in s or "CAP" in s:
        return FastenerStandard.ISO_4762
    if "10642" in s or "7991" in s or "COUNTERSUNK" in s or "FLAT" in s:
        return FastenerStandard.ISO_10642
    if "7380" in s or "BUTTON" in s or "DOME" in s:
        return FastenerStandard.ISO_7380
    if "4032" in s or "934" in s or "NUT" in s:
        return FastenerStandard.ISO_4032
    if "7089" in s or "125" in s or "WASHER" in s:
        return FastenerStandard.ISO_7089
    if "HEAT" in s or "INSERT" in s or "RUTHEX" in s:
        return FastenerStandard.HEAT_SET
    return FastenerStandard.ISO_4762


# ==============================================================================
# 1. 独立紧固件 OpenSCAD 几何生成器
# ==============================================================================

def generate_bolt_scad(
    standard: str | FastenerStandard = FastenerStandard.ISO_4762,
    size: str = "M3",
    length: float = 12.0,
    thread: bool = True,
    color: str = "#8f9ca6",
) -> str:
    """生成标准公制螺栓的物理实体 OpenSCAD 代码。

    :param standard: 螺栓标准 (ISO_4762 / ISO_10642 / ISO_7380)
    :param size: 公称规格 (如 'M3', 'M4')
    :param length: 螺杆有效长度 (mm)
    :param thread: 是否呈现螺纹螺距刻痕
    :param color: 预览着色 (默认不锈钢浅灰色)
    :return: 完整的 OpenSCAD 代码字符串
    """
    sz = _norm_size(size)
    std = _norm_standard(standard)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    nom_d = cat["nominal_d"]
    pitch = cat["pitch"]

    lines = [
        f"// ========================================================",
        f"// 标准工业紧固件: {std.value} {sz} x {length:.1f}mm",
        f"// 自动由 SnapSCAD Fasteners Library 生成",
        f"// ========================================================",
        f"$fn = 48;",
        f"",
        f"color(\"{color}\") {{",
        f"  translate([0, 0, 0]) {{",
    ]

    # 生成螺杆 (Shank)
    tip_chamfer = min(nom_d * 0.15, 0.6)
    lines.append(f"    // 螺杆主体 (d={nom_d}mm, L={length}mm)")
    lines.append(f"    cylinder(d = {nom_d:.2f}, h = {length:.2f}, center = false);")
    lines.append(f"    // 螺钉末端倒角 (防卡扣导向)")
    lines.append(f"    translate([0, 0, 0])")
    lines.append(f"      cylinder(d1 = {nom_d - tip_chamfer * 2:.2f}, d2 = {nom_d:.2f}, h = {tip_chamfer:.2f}, center = false);")

    # 螺纹纹理呈现 (简化物理牙纹或螺旋圈)
    if thread and length > 2.0:
        lines.append(f"    // 螺距牙型指示 (P={pitch}mm)")
        lines.append(f"    for (z = [{tip_chamfer:.2f} : {pitch:.2f} : {length - tip_chamfer:.2f}]) {{")
        lines.append(f"      translate([0, 0, z])")
        lines.append(f"        cylinder(d = {nom_d + 0.10:.2f}, h = {pitch * 0.45:.2f}, center = false, $fn = 24);")
        lines.append(f"    }}")

    # 生成螺栓头部 (Head)
    lines.append(f"    // 螺栓头部特征")
    if std == FastenerStandard.ISO_4762:
        info = cat["iso4762"]
        hd = info["head_d"]
        hk = info["head_k"]
        hex_s = info["hex_s"]
        hex_t = info["hex_t"]
        hex_r = hex_s / math.cos(math.radians(30)) / 2.0
        lines.append(f"    translate([0, 0, {length:.2f}]) {{")
        lines.append(f"      difference() {{")
        lines.append(f"        // ISO 4762 圆柱头 (D={hd}mm, H={hk}mm)")
        lines.append(f"        cylinder(d = {hd:.2f}, h = {hk:.2f}, center = false);")
        lines.append(f"        // 内六角扳手凹槽 (s={hex_s}mm, t={hex_t}mm)")
        lines.append(f"        translate([0, 0, {hk - hex_t:.2f}])")
        lines.append(f"          cylinder(r = {hex_r:.2f}, h = {hex_t + 0.1:.2f}, $fn = 6, center = false);")
        lines.append(f"      }}")
        lines.append(f"    }}")

    elif std == FastenerStandard.ISO_10642:
        info = cat["iso10642"]
        hd = info["head_d"]
        hk = info["head_k"]
        hex_s = info["hex_s"]
        hex_r = hex_s / math.cos(math.radians(30)) / 2.0
        lines.append(f"    translate([0, 0, {length - hk:.2f}]) {{")
        lines.append(f"      difference() {{")
        lines.append(f"        // ISO 10642 90° 沉头锥体 (D={hd}mm, H={hk}mm)")
        lines.append(f"        cylinder(d1 = {nom_d:.2f}, d2 = {hd:.2f}, h = {hk:.2f}, center = false);")
        lines.append(f"        // 内六角凹槽 (s={hex_s}mm)")
        lines.append(f"        translate([0, 0, {hk * 0.4:.2f}])")
        lines.append(f"          cylinder(r = {hex_r:.2f}, h = {hk * 0.65:.2f}, $fn = 6, center = false);")
        lines.append(f"      }}")
        lines.append(f"    }}")

    elif std == FastenerStandard.ISO_7380:
        info = cat["iso7380"]
        hd = info["head_d"]
        hk = info["head_k"]
        hex_s = info["hex_s"]
        hex_r = hex_s / math.cos(math.radians(30)) / 2.0
        lines.append(f"    translate([0, 0, {length:.2f}]) {{")
        lines.append(f"      difference() {{")
        lines.append(f"        // ISO 7380 盘头/半球头 (D={hd}mm, H={hk}mm)")
        lines.append(f"        resize([{hd:.2f}, {hd:.2f}, {hk * 2:.2f}])")
        lines.append(f"          sphere(d = {hd:.2f});")
        lines.append(f"        // 裁剪下半球与内六角")
        lines.append(f"        translate([0, 0, -{hd:.2f}]) cube([{hd * 2:.2f}, {hd * 2:.2f}, {hd * 2:.2f}], center = true);")
        lines.append(f"        translate([0, 0, {hk * 0.3:.2f}])")
        lines.append(f"          cylinder(r = {hex_r:.2f}, h = {hk:.2f}, $fn = 6, center = false);")
        lines.append(f"      }}")
        lines.append(f"    }}")

    lines.append(f"  }}")
    lines.append(f"}}")
    return "\n".join(lines)


def generate_nut_scad(
    size: str = "M3",
    color: str = "#a0abb5",
) -> str:
    """生成 ISO 4032 标准公制六角螺母物理实体 OpenSCAD 代码。"""
    sz = _norm_size(size)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    nom_d = cat["nominal_d"]
    info = cat["iso4032"]
    s = info["hex_s"]
    m = info["thickness_m"]
    r_hex = s / math.cos(math.radians(30)) / 2.0

    lines = [
        f"// ========================================================",
        f"// 标准六角螺母: ISO 4032 {sz} (对边s={s}mm, 厚m={m}mm)",
        f"// ========================================================",
        f"$fn = 48;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    // 六角棱柱体",
        f"    cylinder(r = {r_hex:.2f}, h = {m:.2f}, $fn = 6, center = false);",
        f"    // 内螺纹通孔 (通径d={nom_d}mm)",
        f"    translate([0, 0, -0.2])",
        f"      cylinder(d = {nom_d:.2f}, h = {m + 0.4:.2f}, center = false);",
        f"    // 顶端倒角锥 (双面倒角)",
        f"    translate([0, 0, -0.1])",
        f"      cylinder(d1 = {nom_d + 0.8:.2f}, d2 = {nom_d:.2f}, h = 0.5, center = false);",
        f"    translate([0, 0, {m - 0.4:.2f}])",
        f"      cylinder(d1 = {nom_d:.2f}, d2 = {nom_d + 0.8:.2f}, h = 0.5, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_washer_scad(
    size: str = "M3",
    color: str = "#c2cbd4",
) -> str:
    """生成 ISO 7089 公制标准平垫圈实体 OpenSCAD 代码。"""
    sz = _norm_size(size)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    info = cat["iso7089"]
    d1 = info["inner_d"]
    d2 = info["outer_d"]
    h = info["thickness_h"]

    lines = [
        f"// ========================================================",
        f"// 标准平垫圈: ISO 7089 {sz} (内孔d={d1}mm, 外径D={d2}mm, 厚h={h}mm)",
        f"// ========================================================",
        f"$fn = 48;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    cylinder(d = {d2:.2f}, h = {h:.2f}, center = false);",
        f"    translate([0, 0, -0.1])",
        f"      cylinder(d = {d1:.2f}, h = {h + 0.2:.2f}, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_heat_set_insert_scad(
    size: str = "M3",
    color: str = "#e5b839",
) -> str:
    """生成 3D 打印标准黄铜热熔螺母 (Heat-Set Insert) 实体 OpenSCAD 代码。"""
    sz = _norm_size(size)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    nom_d = cat["nominal_d"]
    info = cat["heat_set"]
    out_d = info["outer_d"]
    length = info["length"]

    lines = [
        f"// ========================================================",
        f"// 热熔铜螺母: Heat-Set Insert {sz} (外径D={out_d}mm, 长度L={length}mm)",
        f"// ========================================================",
        f"$fn = 48;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    union() {{",
        f"      // 主体圆柱",
        f"      cylinder(d = {out_d:.2f}, h = {length:.2f}, center = false);",
        f"      // 滚花压痕凸肋 (用于塑料热熔锚固)",
        f"      for (z = [1.0 : 1.4 : {length - 1.2:.2f}]) {{",
        f"        translate([0, 0, z])",
        f"          cylinder(d = {out_d + 0.25:.2f}, h = 0.6, center = false, $fn = 24);",
        f"      }}",
        f"    }}",
        f"    // 内螺纹孔",
        f"    translate([0, 0, -0.2])",
        f"      cylinder(d = {nom_d:.2f}, h = {length + 0.4:.2f}, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


# ==============================================================================
# 2. 负形切削体代码生成器 (差集扣除 Counterbore / Pocket Cutters)
# ==============================================================================

def generate_counterbore_cutter(
    size: str = "M3",
    plate_thickness: float = 10.0,
    include_washer: bool = False,
    extra_clearance: float = 0.2,
) -> str:
    """生成工业级圆柱阶梯沉头孔 (Counterbore) 差集切削体 OpenSCAD 模块。

    :param size: 紧固件公称尺寸 (如 'M3')
    :param plate_thickness: 安装板总厚度 (mm)
    :param include_washer: 是否预留垫圈沉头深度与直径
    :param extra_clearance: 沉头间隙余量 (mm)
    :return: OpenSCAD 模块代码
    """
    sz = _norm_size(size)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    d_screw = cat["clearance_hole"]["normal"]
    cb_info = cat["iso4762"]
    d_head = cb_info["counterbore_d"] + extra_clearance
    h_head = cb_info["counterbore_depth"]

    if include_washer:
        w_info = cat["iso7089"]
        d_head = max(d_head, w_info["outer_d"] + 0.5)
        h_head += w_info["thickness_h"]

    lines = [
        f"// ISO 4762 {sz} 阶梯沉头孔切削体 (板厚: {plate_thickness:.1f}mm, 沉头深: {h_head:.2f}mm)",
        f"module counterbore_cutter_{sz.lower()}() {{",
        f"  union() {{",
        f"    // 螺栓通孔 (d={d_screw:.2f}mm)",
        f"    translate([0, 0, -0.5])",
        f"      cylinder(d = {d_screw:.2f}, h = {plate_thickness + 1.0:.2f}, $fn = 32, center = false);",
        f"    // 头部沉槽 (D={d_head:.2f}mm, 深={h_head:.2f}mm)",
        f"    translate([0, 0, {max(plate_thickness - h_head, 0.0):.2f}])",
        f"      cylinder(d = {d_head:.2f}, h = {h_head + 1.0:.2f}, $fn = 32, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_countersink_cutter(
    size: str = "M3",
    plate_thickness: float = 10.0,
    angle: float = 90.0,
) -> str:
    """生成 ISO 10642 90° 沉头孔 (Countersink) 差集切削体 OpenSCAD 模块。"""
    sz = _norm_size(size)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    d_screw = cat["clearance_hole"]["normal"]
    cs_info = cat["iso10642"]
    d_head = cs_info["head_d"] + 0.4
    h_head = cs_info["head_k"] + 0.2

    lines = [
        f"// ISO 10642 {sz} 90° 沉头锥切削体 (板厚: {plate_thickness:.1f}mm, 沉头外径: {d_head:.2f}mm)",
        f"module countersink_cutter_{sz.lower()}() {{",
        f"  union() {{",
        f"    // 螺钉通孔",
        f"    translate([0, 0, -0.5])",
        f"      cylinder(d = {d_screw:.2f}, h = {plate_thickness + 1.0:.2f}, $fn = 32, center = false);",
        f"    // 90° 锥形沉头窝",
        f"    translate([0, 0, {max(plate_thickness - h_head, 0.0):.2f}])",
        f"      cylinder(d1 = {d_screw:.2f}, d2 = {d_head:.2f}, h = {h_head + 0.1:.2f}, $fn = 32, center = false);",
        f"    // 顶部溢出延长防止共面",
        f"    translate([0, 0, {plate_thickness:.2f}])",
        f"      cylinder(d = {d_head:.2f}, h = 1.0, $fn = 32, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_hex_pocket_cutter(
    size: str = "M3",
    pocket_depth: float = 3.0,
    side_entry: bool = False,
    entry_length: float = 15.0,
) -> str:
    """生成 3D 打印防转嵌件/六角螺母沉槽 (Hex Nut Trap) 切削体。

    :param size: 公称螺母规格 (如 'M3')
    :param pocket_depth: 螺母槽深度 (mm)
    :param side_entry: 是否生成侧向滑入插槽 (用于无垂直操作空间时的侧插螺母)
    :param entry_length: 侧插槽引出长度 (mm)
    """
    sz = _norm_size(size)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    nom_d = cat["nominal_d"]
    nut_info = cat["iso4032"]
    s_pocket = nut_info["pocket_s"]
    r_hex = s_pocket / math.cos(math.radians(30)) / 2.0
    h_pocket = max(pocket_depth, nut_info["pocket_depth"])

    lines = [
        f"// ISO 4032 {sz} 六角螺母防转沉槽切削体 (对边: {s_pocket:.2f}mm, 深: {h_pocket:.2f}mm)",
        f"module nut_pocket_cutter_{sz.lower()}() {{",
        f"  union() {{",
        f"    // 螺栓穿入通孔",
        f"    translate([0, 0, -5.0])",
        f"      cylinder(d = {cat['clearance_hole']['normal']:.2f}, h = 20.0, $fn = 32, center = false);",
        f"    // 六角槽体",
        f"    translate([0, 0, 0])",
        f"      cylinder(r = {r_hex:.2f}, h = {h_pocket:.2f}, $fn = 6, center = false);",
    ]
    if side_entry:
        lines.append(f"    // 侧边滑入导轨插槽 (宽={s_pocket:.2f}mm, 长={entry_length:.1f}mm)")
        lines.append(f"    translate([0, -{s_pocket/2:.2f}, 0])")
        lines.append(f"      cube([{entry_length:.2f}, {s_pocket:.2f}, {h_pocket:.2f}], center = false);")
    lines.append(f"  }}")
    lines.append(f"}}")
    return "\n".join(lines)


def generate_heat_set_hole_cutter(
    size: str = "M3",
    plate_thickness: float = 10.0,
) -> str:
    """生成 3D 打印黄铜热熔螺母安装孔切削体 (自带 8° 拔模倒锥与底部熔胶沉淀槽)。"""
    sz = _norm_size(size)
    cat = FASTENER_CATALOG.get(sz, FASTENER_CATALOG["M3"])
    info = cat["heat_set"]
    top_d = info["hole_top_d"]
    bot_d = info["hole_bot_d"]
    min_depth = info["hole_min_depth"]
    depth = min(plate_thickness, min_depth)

    lines = [
        f"// 热熔螺母预留安装孔切削体 {sz} (上口径={top_d}mm, 下口径={bot_d}mm, 深度={depth:.2f}mm)",
        f"module heat_set_hole_cutter_{sz.lower()}() {{",
        f"  union() {{",
        f"    // 顶部 45° 导向倒角 (便于放置热熔咀)",
        f"    translate([0, 0, {depth - 0.6:.2f}])",
        f"      cylinder(d1 = {top_d:.2f}, d2 = {top_d + 1.0:.2f}, h = 0.7, $fn = 32, center = false);",
        f"    // 8° 锥度配合主体孔",
        f"    cylinder(d1 = {bot_d:.2f}, d2 = {top_d:.2f}, h = {depth:.2f}, $fn = 32, center = false);",
        f"    // 底部熔胶缓冲溢流坑 (d={bot_d - 0.5:.2f}mm)",
        f"    translate([0, 0, -0.8])",
        f"      cylinder(d = {max(bot_d - 0.5, 1.5):.2f}, h = 0.9, $fn = 24, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


# ==============================================================================
# 3. 智能孔径自动配准与物料清单 (BOM) 生成器
# ==============================================================================

def match_fastener_for_hole(hole_d: float) -> tuple[str, dict[str, Any]]:
    """根据实际孔径自动配准最匹配的标准公制紧固件规格。

    :param hole_d: 测得或建模孔径 (mm)
    :return: (公称规格例如 'M3', 对应尺寸数据库条目)
    """
    d = float(hole_d)
    if d < 2.3:
        sz = "M2"
    elif d < 2.8:
        sz = "M2.5"
    elif d < 3.8:
        sz = "M3"
    elif d < 4.8:
        sz = "M4"
    elif d < 5.8:
        sz = "M5"
    elif d < 7.2:
        sz = "M6"
    elif d < 9.5:
        sz = "M8"
    else:
        sz = "M10"
    return sz, FASTENER_CATALOG[sz]


@dataclass
class FastenerBOMItem:
    standard: str
    size: str
    nominal_length: float
    quantity: int
    role: str
    recess_type: str = "counterbore"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "standard": self.standard,
            "size": self.size,
            "nominal_length": self.nominal_length,
            "quantity": self.quantity,
            "role": self.role,
            "recess_type": self.recess_type,
            "notes": self.notes,
        }


def generate_fasteners_bom(spec: dict[str, Any]) -> dict[str, Any]:
    """遍历 `spec.json` 自动分析装配所需的螺栓、螺母、垫圈与热熔嵌件物料清单 (BOM)。

    :param spec: SnapSCAD 规范字典
    :return: 包含 items, total_count, summary_markdown 的字典
    """
    parts = spec.get("parts") or []
    items_map: dict[tuple[str, str, float, str], FastenerBOMItem] = {}

    for part in parts:
        p_name = part.get("name", "未命名部件")
        holes = part.get("holes") or []
        for h in holes:
            prim = str(h.get("primitive", "")).lower()
            std_hint = str(h.get("standard", h.get("bolt", ""))).upper().strip()
            sz = h.get("size") or []
            h_d = float(sz[0]) if len(sz) >= 1 else 3.0
            h_depth = float(sz[1]) if len(sz) >= 2 else (part.get("size", [10, 10, 10])[2] if len(part.get("size", [])) >= 3 else 10.0)

            # 匹配紧固件
            if std_hint in FASTENER_CATALOG:
                matched_sz = std_hint
            else:
                matched_sz, _ = match_fastener_for_hole(h_d)

            # 推导沉头类型与标准
            if prim == "counterbore" or "4762" in std_hint or std_hint in FASTENER_CATALOG:
                std_name = "ISO 4762 (内六角圆柱头)"
                recess = "圆柱沉头孔"
            elif "10642" in std_hint or "countersink" in prim:
                std_name = "ISO 10642 (90° 沉头螺钉)"
                recess = "沉头锥孔"
            elif "heat_set" in prim or "insert" in std_hint.lower():
                std_name = "热熔铜螺母"
                recess = "热熔安装孔"
            elif "nut" in prim:
                std_name = "ISO 4032 (六角螺母)"
                recess = "六角防转槽"
            else:
                std_name = "ISO 4762 (标准螺栓)"
                recess = "通孔装配"

            # 推荐螺栓公称长度 (取最近的 2mm 步进标准长度，如 8, 10, 12, 16, 20...)
            approx_len = math.ceil((h_depth + 2.0) / 2.0) * 2.0
            approx_len = max(approx_len, 6.0)

            key = (std_name, matched_sz, approx_len, recess)
            if key in items_map:
                items_map[key].quantity += 1
            else:
                items_map[key] = FastenerBOMItem(
                    standard=std_name,
                    size=matched_sz,
                    nominal_length=approx_len,
                    quantity=1,
                    role=f"用于 {p_name} 固定",
                    recess_type=recess,
                    notes=f"配合板厚 {h_depth:.1f}mm",
                )

    bom_list = list(items_map.values())
    total_qty = sum(item.quantity for item in bom_list)

    # 生成 Markdown 表格
    md_lines = [
        "### 🔩 标准紧固件装配物料清单 (Fasteners BOM)",
        "",
        "| 序号 | 紧固件标准 | 规格型号 | 推荐公称长度 | 数量 | 配合沉槽类型 | 装配用途说明 |",
        "| :--- | :--- | :--- | :--- | :---: | :--- | :--- |",
    ]
    for idx, item in enumerate(bom_list, 1):
        md_lines.append(
            f"| {idx} | {item.standard} | **{item.size}** | {item.nominal_length:.0f} mm | **{item.quantity}** | {item.recess_type} | {item.role} ({item.notes}) |"
        )
    if not bom_list:
        md_lines.append("| - | 未检测到紧固件特征 | - | - | 0 | - | 模型可能为单体结构或免螺丝卡扣装配 |")
    md_lines.append(f"\n> **紧固件合计需求**: {total_qty} 件。")

    return {
        "total_count": total_qty,
        "items": [it.to_dict() for it in bom_list],
        "summary_markdown": "\n".join(md_lines),
    }


def apply_auto_counterbores(
    spec: dict[str, Any],
    default_standard: str = "ISO_4762",
    target_part_indices: Optional[list[int]] = None,
) -> dict[str, Any]:
    """为规范中的常规圆柱安装孔自动升级注入工业级阶梯沉头孔特征。

    :param spec: 原始 spec 字典
    :param default_standard: 沉头标准 (ISO_4762 / ISO_10642)
    :param target_part_indices: 目标部件索引列表 (若为 None 则全部应用)
    :return: 经过沉头孔升级增强的新 spec 字典
    """
    new_spec = copy.deepcopy(spec)
    parts = new_spec.get("parts") or []

    for p_idx, part in enumerate(parts):
        if target_part_indices is not None and p_idx not in target_part_indices:
            continue
        holes = part.get("holes") or []
        updated_holes = []
        for h in holes:
            h_copy = dict(h)
            prim = str(h_copy.get("primitive", "cylinder")).lower()
            sz = h_copy.get("size") or []
            if prim in ("cylinder", "hole") and len(sz) >= 1:
                dia = float(sz[0])
                # 对 2.0mm ~ 10.0mm 直径的安装通孔自动升级为 counterbore
                if 2.0 <= dia <= 11.0:
                    matched_sz, cat_info = match_fastener_for_hole(dia)
                    h_copy["primitive"] = "counterbore"
                    h_copy["standard"] = matched_sz
                    # 设定沉头深度
                    if default_standard.upper() == "ISO_10642":
                        h_copy["head_type"] = "countersunk"
                    else:
                        h_copy["head_type"] = "socket_head"
            updated_holes.append(h_copy)
        part["holes"] = updated_holes

    return new_spec
