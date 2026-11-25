"""SnapSCAD 机械零件 CNC 铣削特征工步与减材可制造性体检引擎 (CNC Machining DFM & Toolpath Advisor)。

支持:
  - 工业常见金属与工程塑料 CNC 切削参数数据库 (6061铝合金、45#钢、SUS304不锈钢、C360黄铜、POM赛钢)
  - 3 轴减材铣削可制造性深度体检 (DFM):
    * 内部型腔/槽口 90° 不可加工尖角检测 (SHARP_INTERNAL_CORNER) 与最小刀具圆角校验
    * 深窄槽/深孔长径比 L/D 颤振与让刀风险评估 (DEEP_POCKET_CHATTER)
    * 装夹翻面/侧向孔/倒扣可及性分析 (MULTI_SETUP_REQUIRED)
    * 金属/塑胶薄壁铣削振刀预警 (THIN_WALL_DEFLECTION)
    * 非标钻孔径与螺旋插补识别
  - CAM 加工工步规划与主轴转速 (RPM) / 进给速率 (Feed Rate mm/min) / 预计工时推算
  - CNC 专用“狗骨头”避让内直角型腔模块生成器 (generate_dogbone_pocket_scad)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


#: CNC 切削材料工艺参数数据库
CNC_MATERIALS: dict[str, dict[str, Any]] = {
    "ALUMINUM_6061": {
        "name": "6061-T6 铝合金",
        "surface_speed_m_min": 180.0,
        "chip_load_mm": 0.050,
        "flutes": 3,
        "density_g_cm3": 2.70,
        "min_wall_mm": 1.2,
        "mrr_cm3_min_base": 18.0,
        "notes": "推荐使用 3 刃抛光硬质合金立铣刀，适度喷雾冷却防止积屑瘤",
    },
    "STEEL_45": {
        "name": "45# 中碳钢 (AISI 1045)",
        "surface_speed_m_min": 75.0,
        "chip_load_mm": 0.035,
        "flutes": 4,
        "density_g_cm3": 7.85,
        "min_wall_mm": 1.5,
        "mrr_cm3_min_base": 7.5,
        "notes": "推荐使用 TiAlN 涂层 4 刃硬质合金铣刀，采用顺铣策略与充分切削液冷却",
    },
    "STAINLESS_304": {
        "name": "SUS304 奥氏体不锈钢",
        "surface_speed_m_min": 45.0,
        "chip_load_mm": 0.025,
        "flutes": 4,
        "density_g_cm3": 7.93,
        "min_wall_mm": 1.8,
        "mrr_cm3_min_base": 4.2,
        "notes": "易加工硬化，严禁刀具原地摩擦打滑，保持恒定吃刀量与高压冷却",
    },
    "BRASS_C360": {
        "name": "C360 易切削黄铜",
        "surface_speed_m_min": 210.0,
        "chip_load_mm": 0.060,
        "flutes": 3,
        "density_g_cm3": 8.50,
        "min_wall_mm": 1.0,
        "mrr_cm3_min_base": 22.0,
        "notes": "极佳切削性，可使用小前角立铣刀防扎刀",
    },
    "POM_DELRIN": {
        "name": "POM 赛钢 / 工程塑料",
        "surface_speed_m_min": 240.0,
        "chip_load_mm": 0.080,
        "flutes": 2,
        "density_g_cm3": 1.41,
        "min_wall_mm": 2.0,
        "mrr_cm3_min_base": 28.0,
        "notes": "建议使用单刃/双刃锋利 O 型槽铣刀，吹气排屑防止融粘",
    },
}


#: 标准立铣刀规格与有效切削刃长库
STANDARD_ENDMILLS: list[dict[str, float]] = [
    {"diameter": 2.0, "max_depth": 8.0, "shank": 4.0},
    {"diameter": 3.0, "max_depth": 12.0, "shank": 4.0},
    {"diameter": 4.0, "max_depth": 16.0, "shank": 4.0},
    {"diameter": 6.0, "max_depth": 24.0, "shank": 6.0},
    {"diameter": 8.0, "max_depth": 30.0, "shank": 8.0},
    {"diameter": 10.0, "max_depth": 35.0, "shank": 10.0},
    {"diameter": 12.0, "max_depth": 40.0, "shank": 12.0},
]

STANDARD_DRILLS_MM = {1.5, 2.0, 2.5, 3.0, 3.2, 3.3, 4.0, 4.2, 4.5, 5.0, 5.5, 6.0, 6.8, 8.0, 8.5, 10.0, 12.0}


@dataclass
class CncIssue:
    part_id: str
    issue_type: str       # SHARP_INTERNAL_CORNER, DEEP_POCKET_CHATTER, MULTI_SETUP_REQUIRED, THIN_WALL_DEFLECTION, NON_STANDARD_DRILL
    severity: str         # CRITICAL, WARNING, INFO
    description: str
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_id": self.part_id,
            "issue_type": self.issue_type,
            "severity": self.severity,
            "description": self.description,
            "recommendation": self.recommendation,
        }


@dataclass
class CamOperationStep:
    step_no: int
    operation_name: str
    tool_spec: str
    spindle_rpm: int
    feed_rate_mm_min: int
    step_down_mm: float
    est_time_min: float
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_no": self.step_no,
            "operation_name": self.operation_name,
            "tool_spec": self.tool_spec,
            "spindle_rpm": self.spindle_rpm,
            "feed_rate_mm_min": self.feed_rate_mm_min,
            "step_down_mm": round(self.step_down_mm, 2),
            "est_time_min": round(self.est_time_min, 2),
            "notes": self.notes,
        }


@dataclass
class CncDfmReport:
    material: str
    material_name: str
    min_tool_d_mm: float
    machinability_score: int
    is_3axis_feasible: bool
    setup_count: int
    stock_dimensions_mm: list[float]
    part_volume_cm3: float
    stock_volume_cm3: float
    est_total_cycle_min: float
    issues: list[CncIssue] = field(default_factory=list)
    operations: list[CamOperationStep] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "material": self.material,
            "material_name": self.material_name,
            "min_tool_d_mm": self.min_tool_d_mm,
            "machinability_score": self.machinability_score,
            "is_3axis_feasible": self.is_3axis_feasible,
            "setup_count": self.setup_count,
            "stock_dimensions_mm": self.stock_dimensions_mm,
            "part_volume_cm3": round(self.part_volume_cm3, 2),
            "stock_volume_cm3": round(self.stock_volume_cm3, 2),
            "est_total_cycle_min": round(self.est_total_cycle_min, 2),
            "issues_count": len(self.issues),
            "issues": [i.to_dict() for i in self.issues],
            "operations": [o.to_dict() for o in self.operations],
            "summary": self.summary,
        }


def compute_cutting_params(tool_d: float, material_key: str = "ALUMINUM_6061") -> tuple[int, int]:
    """根据标准线速度 Vc 与每齿进给量 fz 推算安全 CNC 主轴转速 (RPM) 与进给率 (mm/min)。"""
    mat = CNC_MATERIALS.get(material_key.upper(), CNC_MATERIALS["ALUMINUM_6061"])
    vc = float(mat["surface_speed_m_min"])
    fz = float(mat["chip_load_mm"])
    z = int(mat["flutes"])
    d = max(float(tool_d), 0.5)

    rpm = int(round((1000.0 * vc) / (math.pi * d)))
    rpm = min(max(rpm, 1200), 24000)  # 限幅适配主流立加/精雕机主轴范围
    # 小直径刀具按比例保守降载
    if d <= 3.0:
        fz *= 0.65
    feed = int(round(rpm * z * fz))
    feed = min(max(feed, 80), 4500)
    return rpm, feed


def generate_dogbone_pocket_scad(
    width: float = 30.0,
    height: float = 20.0,
    depth: float = 8.0,
    tool_d: float = 4.0,
    style: str = "dogbone",
) -> str:
    """生成 CNC 铣削专用的‘狗骨头 (Dogbone)’内直角避让槽负形切削模块。

    在矩形型腔四角沿 45° 对角线精确扩展铣刀半径 R_tool 避让圆，确保方形配合件可无干涉插装到位。
    """
    r_tool = tool_d / 2.0
    # 中心向角点移动 45°: 偏移距离 dx = dy = w/2 - r_tool / sqrt(2)
    # 这样避让圆周正好切过矩形直角顶点 (w/2, h/2)!
    inset = r_tool / math.sqrt(2.0)
    cx = max(width / 2.0 - inset, 0.1)
    cy = max(height / 2.0 - inset, 0.1)

    lines = [
        f"// ========================================================",
        f"// CNC 铣削防干涉内直角避让槽 ({style.upper()}): {width:.1f}x{height:.1f}x{depth:.1f}mm, 适配刀具 Ø{tool_d:.1f}mm",
        f"// ========================================================",
        f"module cnc_dogbone_pocket(w = {width:.2f}, h = {height:.2f}, d = {depth:.2f}, tool_d = {tool_d:.2f}) {{",
        f"  union() {{",
        f"    // 主矩形型腔",
        f"    translate([0, 0, d / 2.0])",
        f"      cube([w, h, d + 0.2], center = true);",
        f"    // 四角 45° 刀具让位圆 (消除旋转铣刀半径干涉)",
        f"    for (sx = [-1, 1], sy = [-1, 1]) {{",
        f"      translate([sx * {cx:.3f}, sy * {cy:.3f}, -0.1])",
        f"        cylinder(d = tool_d, h = d + 0.2, $fn = 24, center = false);",
        f"    }}",
        f"  }}",
        f"}}",
    ]
    return "\n".join(lines)


def check_cnc_machinability(
    spec: dict[str, Any],
    material: str = "ALUMINUM_6061",
    min_tool_d: float = 3.0,
) -> CncDfmReport:
    """对模型 `spec.json` 执行三轴 CNC 铣削可制造性深度体检 (DFM) 与工步规划。"""
    mat_key = str(material).upper()
    mat_info = CNC_MATERIALS.get(mat_key, CNC_MATERIALS["ALUMINUM_6061"])
    parts = spec.get("parts") or []

    issues: list[CncIssue] = []
    score = 100
    setup_axes: set[str] = {"TOP_Z"}

    # 计算整体毛坯外包络与体积估算
    min_x, min_y, min_z = 1e9, 1e9, 1e9
    max_x, max_y, max_z = -1e9, -1e9, -1e9
    part_vol_mm3 = 0.0
    hole_count = 0
    pocket_count = 0

    for idx, p in enumerate(parts):
        pid = str(p.get("id") or p.get("name") or f"part_{idx}")
        prim = str(p.get("primitive", "cube")).lower()
        sz = p.get("size") or [20.0, 20.0, 10.0]
        if isinstance(sz, (int, float)):
            sz = [float(sz), float(sz), float(sz)]
        w = float(sz[0]) if len(sz) >= 1 else 20.0
        d = float(sz[1]) if len(sz) >= 2 else w
        h = float(sz[2]) if len(sz) >= 3 else w

        tr = p.get("transform") or {}
        pos = p.get("center") or tr.get("translate") or [0.0, 0.0, 0.0]
        rot = tr.get("rotate") or [0.0, 0.0, 0.0]
        rx = abs(float(rot[0])) % 180.0
        ry = abs(float(rot[1])) % 180.0

        px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
        min_x = min(min_x, px - w / 2.0)
        max_x = max(max_x, px + w / 2.0)
        min_y = min(min_y, py - d / 2.0)
        max_y = max(max_y, py + d / 2.0)
        min_z = min(min_z, pz)
        max_z = max(max_z, pz + h)

        if prim == "cylinder":
            part_vol_mm3 += math.pi * ((w / 2.0) ** 2) * d
        else:
            part_vol_mm3 += w * d * h

        # 1. 薄壁振刀检查
        min_dim = min(w, d, h)
        if min_dim < mat_info["min_wall_mm"]:
            score -= 10
            issues.append(CncIssue(
                part_id=pid,
                issue_type="THIN_WALL_DEFLECTION",
                severity="WARNING",
                description=f"构件最小壁厚/厚度为 {min_dim:.2f}mm，低于 {mat_info['name']} 安全铣削刚度下限 ({mat_info['min_wall_mm']}mm)。",
                recommendation="加厚壁板或改用高压空气冷却+小切宽 (AE<=0.1D) 高速精铣以减少让刀变形。",
            ))

        # 2. 多面装夹方位检测 (旋转体偏离 Z 轴)
        if (15.0 < rx < 165.0) or (15.0 < ry < 165.0):
            setup_axes.add(f"INDEX_{int(rx)}_{int(ry)}")
            issues.append(CncIssue(
                part_id=pid,
                issue_type="MULTI_SETUP_REQUIRED",
                severity="INFO",
                description=f"构件轴线存在空间倾角 (rx={rx:.0f}°, ry={ry:.0f}°)，标准三轴单次装夹无法全部覆盖。",
                recommendation="需规划二次翻转装夹定位工装，或采用 4/5 轴联动机床一次装夹完成。",
            ))

        # 3. 检验布尔挖孔与型腔特征
        holes = p.get("holes") or []
        for h_item in holes:
            h_prim = str(h_item.get("primitive", "cylinder")).lower()
            h_sz = h_item.get("size") or [4.0, 10.0]
            if isinstance(h_sz, (int, float)):
                h_sz = [float(h_sz)]
            h_tr = h_item.get("transform") or {}
            h_rot = h_tr.get("rotate") or [0.0, 0.0, 0.0]
            hrx = abs(float(h_rot[0])) % 180.0
            hry = abs(float(h_rot[1])) % 180.0
            if (20.0 < hrx < 160.0) or (20.0 < hry < 160.0):
                setup_axes.add("SIDE_OPERATION")
                issues.append(CncIssue(
                    part_id=pid,
                    issue_type="MULTI_SETUP_REQUIRED",
                    severity="WARNING",
                    description=f"检测到侧向/斜向内孔特征 (rx={hrx:.0f}°, ry={hry:.0f}°)，三轴主轴无法从顶面直接铣削。",
                    recommendation="增加侧向翻面工步 (Setup 2)，或确认是否必须侧向开孔。",
                ))

            # 矩形内腔尖角检查 (Cube pocket inside solid)
            if h_prim in ("cube", "box", "slot"):
                pocket_count += 1
                hw = float(h_sz[0]) if len(h_sz) >= 1 else 10.0
                hd = float(h_sz[1]) if len(h_sz) >= 2 else hw
                hh = float(h_sz[2]) if len(h_sz) >= 3 else h
                r_fillet = float(h_item.get("corner_radius", 0.0))

                if r_fillet < min_tool_d / 2.0:
                    score -= 15
                    issues.append(CncIssue(
                        part_id=pid,
                        issue_type="SHARP_INTERNAL_CORNER",
                        severity="CRITICAL",
                        description=f"矩形凹槽 ({hw:.1f}x{hd:.1f}mm) 存在 90° 未倒圆内直角，使用 Ø{min_tool_d:.1f}mm 旋转立铣刀无法清角。",
                        recommendation=f"在四个角注入 R≥{min_tool_d / 2.0:.1f}mm 工艺圆角，或调用 generate_dogbone_pocket_scad 注入狗骨避让槽。",
                    ))

                # 深窄槽长径比检测
                min_span = min(hw, hd)
                if min_span > 0.1 and (hh / min_span) > 4.0:
                    sev = "CRITICAL" if (hh / min_span) > 6.0 else "WARNING"
                    score -= (15 if sev == "CRITICAL" else 8)
                    issues.append(CncIssue(
                        part_id=pid,
                        issue_type="DEEP_POCKET_CHATTER",
                        severity=sev,
                        description=f"窄槽宽仅 {min_span:.1f}mm 但深度达 {hh:.1f}mm (深宽比 L/D={hh / min_span:.1f}x > 4.0)。",
                        recommendation="减小槽深或扩宽凹槽以允许使用更粗刚度立铣刀，或改用慢走丝线切割加工。",
                    ))

            else:  # 圆柱孔 / 沉孔
                hole_count += 1
                dia = float(h_sz[0]) if len(h_sz) >= 1 else 4.0
                depth = float(h_sz[1]) if len(h_sz) >= 2 else h
                if dia < min_tool_d:
                    score -= 10
                    issues.append(CncIssue(
                        part_id=pid,
                        issue_type="DEEP_POCKET_CHATTER",
                        severity="WARNING",
                        description=f"孔径 Ø{dia:.2f}mm 小于当前选定最小铣刀直径 Ø{min_tool_d:.1f}mm。",
                        recommendation=f"需换装对应 Ø{dia:.2f}mm 钻头直接钻削，或调小 min_tool_d 设定。",
                    ))
                if dia > 0.1 and (depth / dia) > 5.0:
                    score -= 8
                    issues.append(CncIssue(
                        part_id=pid,
                        issue_type="DEEP_POCKET_CHATTER",
                        severity="WARNING",
                        description=f"深孔 Ø{dia:.2f}mm 深 {depth:.1f}mm (深径比 {depth / dia:.1f}:1 > 5:1)。",
                        recommendation="采用 G83 深孔啄钻循环 (Peck Drilling) 以利断屑排屑。",
                    ))
                if round(dia, 1) not in STANDARD_DRILLS_MM and dia <= 12.0:
                    issues.append(CncIssue(
                        part_id=pid,
                        issue_type="NON_STANDARD_DRILL",
                        severity="INFO",
                        description=f"孔径 Ø{dia:.2f}mm 属于非标钻头直径，无法单次钻出，需立铣刀螺旋插补扩孔。",
                        recommendation="若仅为普通紧固通孔，建议归整至标准钻孔尺寸 (如 3.2, 4.5, 5.5, 6.0mm)。",
                    ))

    if min_x > max_x:
        stock_w, stock_d, stock_h = 50.0, 50.0, 20.0
    else:
        stock_w = (max_x - min_x) + 4.0
        stock_d = (max_y - min_y) + 4.0
        stock_h = (max_z - min_z) + 2.0

    stock_vol_cm3 = (stock_w * stock_d * stock_h) / 1000.0
    part_vol_cm3 = min(part_vol_mm3 / 1000.0, stock_vol_cm3 * 0.95)
    removal_cm3 = max(stock_vol_cm3 - part_vol_cm3, 1.0)

    # 规划标准三轴 CNC 工步路径表
    ops: list[CamOperationStep] = []
    rough_d = min(10.0, max(stock_w * 0.15, 6.0))
    rpm_r, feed_r = compute_cutting_params(rough_d, mat_key)
    rough_time = max(removal_cm3 / float(mat_info["mrr_cm3_min_base"]), 0.8)
    ops.append(CamOperationStep(
        step_no=1,
        operation_name="基准面铣平与型腔粗开粗 (Facing & Adaptive Roughing)",
        tool_spec=f"Ø{rough_d:.1f}mm 平底粗皮立铣刀 ({mat_info['flutes']}刃)",
        spindle_rpm=rpm_r,
        feed_rate_mm_min=feed_r,
        step_down_mm=min(rough_d * 0.35, 3.0),
        est_time_min=rough_time,
        notes=f"预计切除材料 {removal_cm3:.1f} cm³，留精加工余量 0.2mm",
    ))

    fin_d = max(min_tool_d, 3.0)
    rpm_f, feed_f = compute_cutting_params(fin_d, mat_key)
    fin_time = max(0.6 + pocket_count * 0.8 + (stock_w + stock_d) / 120.0, 0.5)
    ops.append(CamOperationStep(
        step_no=2,
        operation_name="外轮廓与内型腔高速精铣 (Contour & Pocket Finish)",
        tool_spec=f"Ø{fin_d:.1f}mm 精铣平底立铣刀",
        spindle_rpm=rpm_f,
        feed_rate_mm_min=int(feed_f * 0.85),
        step_down_mm=min(fin_d * 0.5, 2.0),
        est_time_min=fin_time,
        notes="保障尺寸公差 ±0.03mm 与表面粗糙度 Ra1.6",
    ))

    if hole_count > 0:
        rpm_d, feed_d = compute_cutting_params(4.0, mat_key)
        drill_time = max(hole_count * 0.25, 0.3)
        ops.append(CamOperationStep(
            step_no=3,
            operation_name=f"孔系中心定位与啄钻/铰孔 ({hole_count} 孔)",
            tool_spec="90° 定点钻 + 标准麻花钻组",
            spindle_rpm=int(rpm_d * 0.4),
            feed_rate_mm_min=int(feed_d * 0.35),
            step_down_mm=2.0,
            est_time_min=drill_time,
            notes="先点孔防偏斜，再执行 G83 啄钻",
        ))

    rpm_c, feed_c = compute_cutting_params(6.0, mat_key)
    ops.append(CamOperationStep(
        step_no=len(ops) + 1,
        operation_name="全周边锐边倒角与去毛刺 (Chamfer & Deburr)",
        tool_spec="Ø6.0mm 90° 硬质合金倒角刀",
        spindle_rpm=rpm_c,
        feed_rate_mm_min=feed_c,
        step_down_mm=0.3,
        est_time_min=max(0.4 + len(parts) * 0.15, 0.4),
        notes="清除 0.3mm x 45° 工艺倒角防划伤",
    ))

    total_cycle = sum(o.est_time_min for o in ops) + (len(setup_axes) - 1) * 4.5
    score = max(min(score, 100), 10)
    feasible_3axis = not any(i.issue_type == "SHARP_INTERNAL_CORNER" and i.severity == "CRITICAL" for i in issues)

    summary = (
        f"CNC 可制造性评分: {score}/100 ({mat_info['name']}, 最小刀具 Ø{min_tool_d:.1f}mm) | "
        f"预计装夹 {len(setup_axes)} 次 | 单件总加工节拍约 {total_cycle:.1f} 分钟 | "
        f"发现 {len(issues)} 项工艺建议。"
    )

    return CncDfmReport(
        material=mat_key,
        material_name=mat_info["name"],
        min_tool_d_mm=min_tool_d,
        machinability_score=score,
        is_3axis_feasible=feasible_3axis,
        setup_count=len(setup_axes),
        stock_dimensions_mm=[round(stock_w, 1), round(stock_d, 1), round(stock_h, 1)],
        part_volume_cm3=part_vol_cm3,
        stock_volume_cm3=stock_vol_cm3,
        est_total_cycle_min=total_cycle,
        issues=issues,
        operations=ops,
        summary=summary,
    )
