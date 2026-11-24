"""SnapSCAD 工业管路与流体弯管/三通/法兰连接器生成引擎 (Piping, Flanges & Fluid Manifolds Generator)。

支持:
  - 工业标准公称管径 (DN6 ~ DN25) 与气动 PU 软管规格表
  - 光滑圆弧弯管 (Elbow: 45° / 90° / 180° 旋转扫描成形 rotate_extrude)
  - 连通正交三通 (Tee Junction) 与流体 Y 型分流管 (Y-Splitter)
  - 标准管道法兰盘 (Flange with Bolt Circle PCD & O-Ring 密封圈凹槽)
  - 软管防滑脱宝塔形快插接头 (Multi-stage Hose Barb)
  - 多通道流体分配歧管块 (Multi-port Fluid Distribution Manifold Block)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class PipeStandard(str, Enum):
    DN6 = "DN6"      # 外径 10.2mm, 内径 7.0mm, 壁厚 1.6mm
    DN8 = "DN8"      # 外径 13.5mm, 内径 9.5mm, 壁厚 2.0mm
    DN10 = "DN10"    # 外径 17.2mm, 内径 13.2mm, 壁厚 2.0mm
    DN15 = "DN15"    # 外径 21.3mm, 内径 17.3mm, 壁厚 2.0mm
    DN20 = "DN20"    # 外径 26.9mm, 内径 22.3mm, 壁厚 2.3mm
    DN25 = "DN25"    # 外径 33.7mm, 内径 28.5mm, 壁厚 2.6mm
    PU_4x2_5 = "PU_4x2.5"   # 气动管 4mm 外径, 2.5mm 内径
    PU_6x4 = "PU_6x4"       # 气动管 6mm 外径, 4.0mm 内径
    PU_8x5 = "PU_8x5"       # 气动管 8mm 外径, 5.0mm 内径
    PU_10x6_5 = "PU_10x6.5" # 气动管 10mm 外径, 6.5mm 内径
    PU_12x8 = "PU_12x8"     # 气动管 12mm 外径, 8.0mm 内径


#: 工业管道公称规格表
PIPE_CATALOG: dict[str, dict[str, Any]] = {
    "DN6": {
        "od": 10.2, "id": 7.0, "wall": 1.6, "thread": "G1/8",
        "flange_od": 28.0, "pcd": 20.0, "bolt_holes": 4, "bolt_size": "M3",
        "o_ring_id": 9.0, "o_ring_cs": 1.5,
    },
    "DN8": {
        "od": 13.5, "id": 9.5, "wall": 2.0, "thread": "G1/4",
        "flange_od": 34.0, "pcd": 24.0, "bolt_holes": 4, "bolt_size": "M3",
        "o_ring_id": 12.0, "o_ring_cs": 1.8,
    },
    "DN10": {
        "od": 17.2, "id": 13.2, "wall": 2.0, "thread": "G3/8",
        "flange_od": 42.0, "pcd": 30.0, "bolt_holes": 4, "bolt_size": "M4",
        "o_ring_id": 15.5, "o_ring_cs": 2.0,
    },
    "DN15": {
        "od": 21.3, "id": 17.3, "wall": 2.0, "thread": "G1/2",
        "flange_od": 50.0, "pcd": 38.0, "bolt_holes": 4, "bolt_size": "M4",
        "o_ring_id": 19.5, "o_ring_cs": 2.4,
    },
    "DN20": {
        "od": 26.9, "id": 22.3, "wall": 2.3, "thread": "G3/4",
        "flange_od": 60.0, "pcd": 46.0, "bolt_holes": 4, "bolt_size": "M5",
        "o_ring_id": 24.5, "o_ring_cs": 2.5,
    },
    "DN25": {
        "od": 33.7, "id": 28.5, "wall": 2.6, "thread": "G1",
        "flange_od": 72.0, "pcd": 56.0, "bolt_holes": 4, "bolt_size": "M5",
        "o_ring_id": 31.0, "o_ring_cs": 3.0,
    },
    "PU_4x2.5": {"od": 4.0, "id": 2.5, "wall": 0.75, "barb_tip": 3.2, "barb_max": 4.6},
    "PU_6x4": {"od": 6.0, "id": 4.0, "wall": 1.0, "barb_tip": 4.8, "barb_max": 6.8},
    "PU_8x5": {"od": 8.0, "id": 5.0, "wall": 1.5, "barb_tip": 6.2, "barb_max": 9.0},
    "PU_10x6.5": {"od": 10.0, "id": 6.5, "wall": 1.75, "barb_tip": 8.0, "barb_max": 11.2},
    "PU_12x8": {"od": 12.0, "id": 8.0, "wall": 2.0, "barb_tip": 9.8, "barb_max": 13.4},
}


def _resolve_pipe_dims(
    dn: Optional[str] = None,
    od: Optional[float] = None,
    id: Optional[float] = None,
    wall: Optional[float] = None,
) -> tuple[float, float, float]:
    """统一解析管道外径、内径与壁厚参数。"""
    if dn and dn in PIPE_CATALOG:
        info = PIPE_CATALOG[dn]
        return float(info["od"]), float(info["id"]), float(info["wall"])
    
    if od is not None and id is not None:
        effective_wall = (od - id) / 2.0
        return float(od), float(id), max(effective_wall, 0.5)
    
    if od is not None and wall is not None:
        effective_id = max(od - wall * 2.0, 1.0)
        return float(od), float(effective_id), float(wall)
    
    # 默认 DN15 尺寸
    d15 = PIPE_CATALOG["DN15"]
    return float(d15["od"]), float(d15["id"]), float(d15["wall"])


# ==============================================================================
# 1. 基础管路原语生成器 (Straight, Elbow, Tee, Y-Splitter)
# ==============================================================================

def generate_pipe_segment(
    od: float = 21.3,
    id: float = 17.3,
    length: float = 60.0,
    dn: Optional[str] = None,
    color: str = "#607274",
) -> str:
    """生成标准圆柱中空流体直管段 OpenSCAD 代码。

    :param od: 管道外径 (mm)
    :param id: 管道内径 / 流道孔径 (mm)
    :param length: 管段有效长度 (mm)
    :param dn: 公称规格 (如 'DN15')
    :param color: 渲染预览色彩
    :return: 完整的 OpenSCAD 几何代码
    """
    effective_od, effective_id, wall = _resolve_pipe_dims(dn, od, id)
    lines = [
        f"// ========================================================",
        f"// 工业直通管道: OD={effective_od:.2f}mm, ID={effective_id:.2f}mm, 壁厚={wall:.2f}mm, L={length:.2f}mm",
        f"// 由 SnapSCAD Piping Engine 自动生成",
        f"// ========================================================",
        f"$fn = 64;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    // 管道外壁",
        f"    cylinder(d = {effective_od:.2f}, h = {length:.2f}, center = false);",
        f"    // 贯穿流体内腔 (微延展防共面)",
        f"    translate([0, 0, -0.5])",
        f"      cylinder(d = {effective_id:.2f}, h = {length + 1.0:.2f}, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_pipe_elbow(
    bend_angle: float = 90.0,
    bend_radius: float = 30.0,
    od: float = 21.3,
    id: float = 17.3,
    dn: Optional[str] = None,
    tangent_length: float = 10.0,
    color: str = "#435585",
) -> str:
    """基于二维旋转扫描 (rotate_extrude) 生成数学光滑圆弧弯管 (Elbow)。

    :param bend_angle: 弯曲角度 (度数, 如 45, 90, 180)
    :param bend_radius: 弯曲中心曲率半径 R_bend (mm)
    :param od: 管道外径
    :param id: 管道流道内径
    :param tangent_length: 弯管两端切线直管过渡段长度 (mm)
    :return: 完整的 OpenSCAD 几何代码
    """
    effective_od, effective_id, wall = _resolve_pipe_dims(dn, od, id)
    ang = float(bend_angle)
    r = float(bend_radius)
    t_len = float(tangent_length)

    lines = [
        f"// ========================================================",
        f"// 工业流体光滑圆弧弯管 ({ang:.1f}° Elbow): R={r:.1f}mm, OD={effective_od:.2f}mm, ID={effective_id:.2f}mm",
        f"// ========================================================",
        f"$fn = 64;",
        f"color(\"{color}\") {{",
        f"  union() {{",
        f"    // 核心圆弧扫描段",
        f"    rotate_extrude(angle = {ang:.2f}, convexity = 10) {{",
        f"      translate([{r:.2f}, 0, 0]) {{",
        f"        difference() {{",
        f"          circle(d = {effective_od:.2f});",
        f"          circle(d = {effective_id:.2f});",
        f"        }}",
        f"      }}",
        f"    }}",
    ]

    # 添加起点与终点切线平直接口管段
    if t_len > 0.1:
        lines.append(f"    // 起始端平直连接管段")
        lines.append(f"    translate([{r:.2f}, 0, -{t_len:.2f}]) {{")
        lines.append(f"      difference() {{")
        lines.append(f"        cylinder(d = {effective_od:.2f}, h = {t_len + 0.1:.2f}, center = false);")
        lines.append(f"        translate([0, 0, -0.5])")
        lines.append(f"          cylinder(d = {effective_id:.2f}, h = {t_len + 1.0:.2f}, center = false);")
        lines.append(f"      }}")
        lines.append(f"    }}")

        # 终点旋转并外接直管段
        lines.append(f"    // 终端平直连接管段")
        lines.append(f"    rotate([0, 0, {ang:.2f}]) {{")
        lines.append(f"      translate([{r:.2f}, 0, 0]) {{")
        lines.append(f"        rotate([-90, 0, 0]) {{")
        lines.append(f"          difference() {{")
        lines.append(f"            cylinder(d = {effective_od:.2f}, h = {t_len:.2f}, center = false);")
        lines.append(f"            translate([0, 0, -0.5])")
        lines.append(f"              cylinder(d = {effective_id:.2f}, h = {t_len + 1.0:.2f}, center = false);")
        lines.append(f"          }}")
        lines.append(f"        }}")
        lines.append(f"      }}")
        lines.append(f"    }}")

    lines.append(f"  }}")
    lines.append(f"}}")
    return "\n".join(lines)


def generate_tee_junction(
    main_od: float = 21.3,
    main_id: float = 17.3,
    main_len: float = 70.0,
    branch_od: Optional[float] = None,
    branch_id: Optional[float] = None,
    branch_len: float = 35.0,
    angle: float = 90.0,
    color: str = "#365486",
) -> str:
    """生成内外流道完全通透的正交或斜接三通管 (Tee Junction)。

    :param main_od: 主干管外径 (mm)
    :param main_id: 主干管内径 (mm)
    :param main_len: 主干管长度 (mm)
    :param branch_od: 支管外径 (默认等于主干管外径，即等径三通)
    :param branch_id: 支管内径 (默认等于主干管内径)
    :param branch_len: 支管伸出长度 (mm)
    :param angle: 支管与主管夹角 (度数, 默认 90°)
    """
    b_od = branch_od if branch_od is not None else main_od
    b_id = branch_id if branch_id is not None else main_id
    b_len = float(branch_len)
    m_len = float(main_len)

    lines = [
        f"// ========================================================",
        f"// 流体连通三通管路 (Tee Junction): 主管OD={main_od:.2f}mm, 支管OD={b_od:.2f}mm",
        f"// ========================================================",
        f"$fn = 64;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    // 外部实体求并",
        f"    union() {{",
        f"      // 主干管道外形",
        f"      translate([0, 0, -{m_len/2:.2f}])",
        f"        cylinder(d = {main_od:.2f}, h = {m_len:.2f}, center = false);",
        f"      // 侧向支管外形",
        f"      rotate([0, {angle:.2f}, 0])",
        f"        cylinder(d = {b_od:.2f}, h = {b_len:.2f}, center = false);",
        f"    }}",
        f"    // 内部连续流腔全贯通布尔扣除",
        f"    union() {{",
        f"      // 主干管道流腔",
        f"      translate([0, 0, -{m_len/2 + 0.5:.2f}])",
        f"        cylinder(d = {main_id:.2f}, h = {m_len + 1.0:.2f}, center = false);",
        f"      // 侧向支管流腔 (直通至主管中心)",
        f"      rotate([0, {angle:.2f}, 0])",
        f"        translate([0, 0, -0.5])",
        f"          cylinder(d = {b_id:.2f}, h = {b_len + 1.0:.2f}, center = false);",
        f"    }}",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


def generate_y_splitter(
    main_od: float = 21.3,
    main_id: float = 17.3,
    branch_od: Optional[float] = None,
    branch_id: Optional[float] = None,
    split_angle: float = 60.0,
    main_len: float = 25.0,
    branch_len: float = 40.0,
    color: str = "#0f766e",
) -> str:
    """生成流体低流阻对称 Y 型双路分流管 (Y-Splitter Manifold)。"""
    b_od = branch_od if branch_od is not None else main_od * 0.75
    b_id = branch_id if branch_id is not None else main_id * 0.75
    half_ang = split_angle / 2.0

    lines = [
        f"// ========================================================",
        f"// 对称 Y 型流体分流歧管 (Y-Splitter, 分流角 {split_angle:.1f}°)",
        f"// ========================================================",
        f"$fn = 64;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    union() {{",
        f"      // 入口主管",
        f"      translate([0, 0, -{main_len:.2f}])",
        f"        cylinder(d = {main_od:.2f}, h = {main_len:.2f}, center = false);",
        f"      // 左分流支管",
        f"      rotate([0, -{half_ang:.2f}, 0])",
        f"        cylinder(d = {b_od:.2f}, h = {branch_len:.2f}, center = false);",
        f"      // 右分流支管",
        f"      rotate([0, {half_ang:.2f}, 0])",
        f"        cylinder(d = {b_od:.2f}, h = {branch_len:.2f}, center = false);",
        f"      // 中心过渡平滑球体",
        f"      sphere(d = {main_od * 1.05:.2f});",
        f"    }}",
        f"    union() {{",
        f"      // 入口主腔",
        f"      translate([0, 0, -{main_len + 0.5:.2f}])",
        f"        cylinder(d = {main_id:.2f}, h = {main_len + 1.0:.2f}, center = false);",
        f"      // 左支路内腔",
        f"      rotate([0, -{half_ang:.2f}, 0])",
        f"        translate([0, 0, -0.5])",
        f"          cylinder(d = {b_id:.2f}, h = {branch_len + 1.0:.2f}, center = false);",
        f"      // 右支路内腔",
        f"      rotate([0, {half_ang:.2f}, 0])",
        f"        translate([0, 0, -0.5])",
        f"          cylinder(d = {b_id:.2f}, h = {branch_len + 1.0:.2f}, center = false);",
        f"      // 内部腔体贯通过渡球",
        f"      sphere(d = {main_id:.2f});",
        f"    }}",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


# ==============================================================================
# 2. 管道法兰盘与软管宝塔接头生成器 (Flange & Hose Barb)
# ==============================================================================

def generate_pipe_flange(
    pipe_od: float = 21.3,
    pipe_id: float = 17.3,
    flange_od: Optional[float] = None,
    thickness: float = 8.0,
    pcd: Optional[float] = None,
    bolt_size: str = "M4",
    bolt_count: int = 4,
    o_ring_groove: bool = True,
    o_ring_id: Optional[float] = None,
    o_ring_cs: float = 2.4,
    pipe_stub_length: float = 15.0,
    color: str = "#5c6b73",
) -> str:
    """生成带分度圆螺栓安装孔与 O-ring 密封圈凹槽的标准法兰盘 (Flange)。

    :param pipe_od: 对接管路外径 (mm)
    :param pipe_id: 对接管路通径 (mm)
    :param flange_od: 法兰外径 (若 None 则自动按 2.2x 管外径设定)
    :param thickness: 法兰厚度 (mm)
    :param pcd: 螺栓分度圆直径 (Pitch Circle Diameter)
    :param bolt_size: 安装紧固件规格 (如 'M3', 'M4')
    :param bolt_count: 螺栓孔均匀环列数量 (通常为 4 或 6)
    :param o_ring_groove: 是否在法兰结合面切削 O 型密封圈环槽
    :param o_ring_cs: 密封圈线径截面直径 (mm, 槽深~75%, 槽宽~130% 提供预紧压缩)
    :param pipe_stub_length: 背部对焊或插入式短管长度 (mm)
    """
    from .fasteners import FASTENER_CATALOG
    f_od = flange_od if flange_od is not None else max(pipe_od * 2.2, pipe_od + 24.0)
    p_pcd = pcd if pcd is not None else (pipe_od + f_od) / 2.0
    th = float(thickness)
    b_sz = str(bolt_size).upper()
    bolt_info = FASTENER_CATALOG.get(b_sz, FASTENER_CATALOG["M4"])
    d_bolt = bolt_info["clearance_hole"]["normal"]

    # O-Ring 槽尺寸计算 (根据 ISO 3601-1 标准工程经验压缩率)
    oring_id = o_ring_id if o_ring_id is not None else pipe_od + 2.0
    groove_depth = o_ring_cs * 0.75
    groove_width = o_ring_cs * 1.30
    groove_mid_d = oring_id + o_ring_cs

    lines = [
        f"// ========================================================",
        f"// 标准管道密封法兰盘: 外径={f_od:.1f}mm, 厚={th:.1f}mm, PCD={p_pcd:.1f}mm ({bolt_count}x{b_sz})",
        f"// ========================================================",
        f"$fn = 64;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    union() {{",
        f"      // 主法兰圆盘",
        f"      cylinder(d = {f_od:.2f}, h = {th:.2f}, center = false);",
        f"      // 背部管路对接短套管",
        f"      translate([0, 0, {th:.2f}])",
        f"        cylinder(d = {pipe_od + 2.0:.2f}, h = {pipe_stub_length:.2f}, center = false);",
        f"    }}",
        f"    // 中心介质流道通孔",
        f"    translate([0, 0, -0.5])",
        f"      cylinder(d = {pipe_id:.2f}, h = {th + pipe_stub_length + 1.0:.2f}, center = false);",
        f"    // 螺栓安装通孔环形阵列 (PCD={p_pcd:.2f}mm, 数量={bolt_count})",
        f"    for (i = [0 : {bolt_count - 1}]) {{",
        f"      rotate([0, 0, i * {360.0 / bolt_count:.2f}])",
        f"        translate([{p_pcd / 2.0:.2f}, 0, -0.5])",
        f"          cylinder(d = {d_bolt:.2f}, h = {th + 1.0:.2f}, center = false, $fn = 32);",
        f"    }}",
    ]

    if o_ring_groove:
        lines.append(f"    // O 型密封圈端面密封环槽 (中径={groove_mid_d:.2f}mm, 槽深={groove_depth:.2f}mm, 槽宽={groove_width:.2f}mm)")
        lines.append(f"    translate([0, 0, -0.1]) {{")
        lines.append(f"      difference() {{")
        lines.append(f"        cylinder(d = {groove_mid_d + groove_width:.2f}, h = {groove_depth + 0.1:.2f}, center = false);")
        lines.append(f"        translate([0, 0, -0.1])")
        lines.append(f"          cylinder(d = {groove_mid_d - groove_width:.2f}, h = {groove_depth + 0.3:.2f}, center = false);")
        lines.append(f"      }}")
        lines.append(f"    }}")

    lines.append(f"  }}")
    lines.append(f"}}")
    return "\n".join(lines)


def generate_hose_barb(
    hose_id: float = 6.0,
    barb_count: int = 3,
    wall_thickness: float = 1.2,
    barb_pitch: float = 5.0,
    hex_base_size: float = 12.0,
    hex_base_len: float = 6.0,
    color: str = "#d97706",
) -> str:
    """生成防滑脱多齿宝塔形软管接头 (Hose Barb Fitting)。

    :param hose_id: 软管公称内径 (mm)
    :param barb_count: 宝塔倒刺齿数 (通常 2~4)
    :param wall_thickness: 接头壁厚 (mm)
    :param barb_pitch: 单个宝塔齿的节距长度 (mm)
    :param hex_base_size: 扳手六角法兰对边宽度 (mm)
    :param hex_base_len: 扳手夹持段长度 (mm)
    """
    inner_d = max(hose_id - wall_thickness * 2.0, 1.5)
    tip_d = hose_id * 0.95        # 倒刺前端导入外径 (略小于软管内径以便插入)
    max_d = hose_id * 1.22        # 倒刺最大外径 (胀紧密封软管壁)
    r_hex = hex_base_size / math.cos(math.radians(30)) / 2.0
    barb_total_len = barb_count * barb_pitch

    lines = [
        f"// ========================================================",
        f"// 防脱宝塔气管/水管接头 (Hose Barb): 适配软管ID={hose_id:.1f}mm, 齿数={barb_count}",
        f"// ========================================================",
        f"$fn = 64;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    union() {{",
        f"      // 六角紧固底座 (对边s={hex_base_size:.1f}mm)",
        f"      cylinder(r = {r_hex:.2f}, h = {hex_base_len:.2f}, $fn = 6, center = false);",
        f"      // 多阶宝塔倒锥体",
        f"      translate([0, 0, {hex_base_len:.2f}]) {{",
        f"        for (i = [0 : {barb_count - 1}]) {{",
        f"          translate([0, 0, i * {barb_pitch:.2f}])",
        f"            cylinder(d1 = {max_d:.2f}, d2 = {tip_d:.2f}, h = {barb_pitch:.2f}, center = false);",
        f"        }}",
        f"      }}",
        f"    }}",
        f"    // 贯通流体腔孔",
        f"    translate([0, 0, -0.5])",
        f"      cylinder(d = {inner_d:.2f}, h = {hex_base_len + barb_total_len + 1.0:.2f}, center = false);",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


# ==============================================================================
# 3. 多通道流体分配歧管块生成器 (Manifold Block)
# ==============================================================================

def generate_manifold_block(
    port_count: int = 4,
    main_port_d: float = 17.2,
    branch_port_d: float = 9.5,
    branch_spacing: float = 24.0,
    mounting_holes: bool = True,
    block_depth: float = 30.0,
    block_height: float = 30.0,
    color: str = "#475569",
) -> str:
    """生成工业级多通道流体分配歧管块 (Multi-port Distribution Manifold)。

    :param port_count: 侧向出流口数量 (N 路输出)
    :param main_port_d: 主供液/供气内腔通道孔径 (mm)
    :param branch_port_d: 各分支出流口孔径 (mm)
    :param branch_spacing: 各支路孔间中心距 (mm)
    :param mounting_holes: 是否在两端预留装配安装通孔
    :param block_depth: 歧管块宽度/深度 (mm)
    :param block_height: 歧管块厚度/高度 (mm)
    """
    total_branch_span = (port_count - 1) * branch_spacing
    margin_end = 25.0 if mounting_holes else 15.0
    block_length = total_branch_span + margin_end * 2.0
    half_l = block_length / 2.0
    half_w = block_depth / 2.0
    half_h = block_height / 2.0

    lines = [
        f"// ========================================================",
        f"// 工业级多路流体集液/分气歧管块 (Manifold Block: {port_count} 路输出)",
        f"// 整体尺寸: [{block_length:.1f}, {block_depth:.1f}, {block_height:.1f}] mm",
        f"// ========================================================",
        f"$fn = 48;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    // 实体金属/尼龙基块",
        f"    cube([{block_length:.2f}, {block_depth:.2f}, {block_height:.2f}], center = true);",
        f"    // 主介质通道 (轴向贯通孔 d={main_port_d:.2f}mm)",
        f"    rotate([0, 90, 0])",
        f"      cylinder(d = {main_port_d:.2f}, h = {block_length + 2.0:.2f}, center = true);",
        f"    // 分流出水/气孔阵列 (d={branch_port_d:.2f}mm)",
        f"    for (i = [0 : {port_count - 1}]) {{",
        f"      translate([-{total_branch_span/2:.2f} + i * {branch_spacing:.2f}, 0, 0])",
        f"        cylinder(d = {branch_port_d:.2f}, h = {block_height + 2.0:.2f}, center = true);",
        f"    }}",
    ]

    if mounting_holes:
        m_x = half_l - 8.0
        lines.append(f"    // 两端安装固定孔 (配 M4 螺钉)")
        lines.append(f"    translate([{m_x:.2f}, 0, 0])")
        lines.append(f"      rotate([90, 0, 0])")
        lines.append(f"        cylinder(d = 4.50, h = {block_depth + 2.0:.2f}, center = true);")
        lines.append(f"    translate([- {m_x:.2f}, 0, 0])")
        lines.append(f"      rotate([90, 0, 0])")
        lines.append(f"        cylinder(d = 4.50, h = {block_depth + 2.0:.2f}, center = true);")

    lines.append(f"  }}")
    lines.append(f"}}")
    return "\n".join(lines)
