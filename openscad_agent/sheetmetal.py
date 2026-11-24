"""SnapSCAD 钣金折弯与展开下料图 DXF/SVG 引擎 (Sheet Metal Bending & Flat Pattern Unfolding Engine)。

支持:
  - 工业钣金材料库与中性层 K 因子 (Aluminum 5052, Steel SPCC, Stainless 304, Brass H62)
  - 精确折弯补偿量 (Bend Allowance, BA) 与折弯扣除量 (Bend Deduction, BD) 计算
  - 任意折弯角度 (-165° ~ +165°) 三维折弯实体 OpenSCAD 代码生成 (含圆弧折弯过渡区与止裂槽)
  - 2D 无损平面展开计算 (多边翻边、安装孔坐标映射、拐角止裂释放槽)
  - 纯 Python 零依赖工业标准 DXF 激光切割下料图导出 (分层: CUT 切割外轮廓 / BEND 折弯线)
  - 纯矢量工程展开图 SVG 导出 (含折弯方向、角度、内圆角标注与标题栏)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


#: 工业标准钣金材料物性及默认中性层 K 因子库
SHEET_METAL_MATERIALS: dict[str, dict[str, Any]] = {
    "ALUMINUM_5052": {
        "name": "5052-H32 铝合金板",
        "k_factor": 0.42,
        "min_bend_radius_ratio": 1.0,  # R_min = 1.0 * T
        "density_g_cm3": 2.68,
        "yield_mpa": 193.0,
        "color": "#cbd5e1",
    },
    "STEEL_SPCC": {
        "name": "SPCC 冷轧碳钢板",
        "k_factor": 0.44,
        "min_bend_radius_ratio": 1.0,
        "density_g_cm3": 7.85,
        "yield_mpa": 240.0,
        "color": "#64748b",
    },
    "STAINLESS_304": {
        "name": "SUS304 不锈钢板",
        "k_factor": 0.46,
        "min_bend_radius_ratio": 1.2,
        "density_g_cm3": 7.93,
        "yield_mpa": 290.0,
        "color": "#94a3b8",
    },
    "BRASS_H62": {
        "name": "H62 黄铜板",
        "k_factor": 0.40,
        "min_bend_radius_ratio": 0.8,
        "density_g_cm3": 8.43,
        "yield_mpa": 220.0,
        "color": "#d97706",
    },
}


def compute_outside_setback(angle_deg: float, inner_radius: float, thickness: float) -> float:
    """计算折弯外退让距离 OSSB (Outside Setback)。"""
    ang_rad = math.radians(min(abs(float(angle_deg)), 175.0))
    return math.tan(ang_rad / 2.0) * (float(inner_radius) + float(thickness))


def compute_bend_allowance(
    angle_deg: float,
    inner_radius: float,
    thickness: float,
    k_factor: float = 0.44,
) -> float:
    """计算折弯弧长中性层展开补偿量 BA (Bend Allowance)。

    BA = (pi / 180) * |angle| * (R + K * T)
    """
    ang_rad = math.radians(abs(float(angle_deg)))
    return ang_rad * (float(inner_radius) + float(k_factor) * float(thickness))


def compute_bend_deduction(
    angle_deg: float,
    inner_radius: float,
    thickness: float,
    k_factor: float = 0.44,
) -> float:
    """计算基于外模线尺寸的折弯扣除量 BD (Bend Deduction = 2 * OSSB - BA)。"""
    ossb = compute_outside_setback(angle_deg, inner_radius, thickness)
    ba = compute_bend_allowance(angle_deg, inner_radius, thickness, k_factor)
    return 2.0 * ossb - ba


@dataclass
class SheetHole:
    """钣金平面或折弯翼上的安装孔/腰型孔定义 (相对于所在板面局部左下角)。"""
    x: float
    y: float
    diameter: float
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x, "y": self.y, "diameter": self.diameter, "label": self.label}


@dataclass
class FlangeSpec:
    """折弯翼缘 (Flange) 定义。"""
    edge: str               # "right", "left", "top", "bottom"
    length: float           # 外模线总折弯高度/长度 (mm)
    angle_deg: float = 90.0 # 正值向 +Z 向上折弯，负值向 -Z 向下折弯
    inner_radius: Optional[float] = None
    holes: list[SheetHole] = field(default_factory=list)
    relief_width: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge": self.edge,
            "length": self.length,
            "angle_deg": self.angle_deg,
            "inner_radius": self.inner_radius,
            "holes": [h.to_dict() for h in self.holes],
            "relief_width": self.relief_width,
        }


@dataclass
class SheetMetalPart:
    """参数化钣金结构件规范。"""
    name: str = "sheet_metal_part"
    base_width: float = 80.0    # 基板 X 方向尺寸 (mm)
    base_height: float = 60.0   # 基板 Y 方向尺寸 (mm)
    thickness: float = 1.5      # 板厚 T (mm)
    material: str = "ALUMINUM_5052"
    k_factor: Optional[float] = None
    default_radius: Optional[float] = None
    base_holes: list[SheetHole] = field(default_factory=list)
    flanges: list[FlangeSpec] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SheetMetalPart":
        mat = str(data.get("material", "ALUMINUM_5052")).upper()
        base_holes = [
            SheetHole(
                x=float(h.get("x", 0.0)),
                y=float(h.get("y", 0.0)),
                diameter=float(h.get("diameter", 3.2)),
                label=str(h.get("label", "")),
            )
            for h in (data.get("base_holes") or [])
        ]
        flanges = []
        for f in (data.get("flanges") or []):
            f_holes = [
                SheetHole(
                    x=float(h.get("x", 0.0)),
                    y=float(h.get("y", 0.0)),
                    diameter=float(h.get("diameter", 3.2)),
                    label=str(h.get("label", "")),
                )
                for h in (f.get("holes") or [])
            ]
            flanges.append(FlangeSpec(
                edge=str(f.get("edge", "right")).lower(),
                length=float(f.get("length", 25.0)),
                angle_deg=float(f.get("angle_deg", 90.0)),
                inner_radius=float(f["inner_radius"]) if f.get("inner_radius") is not None else None,
                holes=f_holes,
                relief_width=float(f["relief_width"]) if f.get("relief_width") is not None else None,
            ))
        return cls(
            name=str(data.get("name", "sheet_metal_part")),
            base_width=float(data.get("base_width", 80.0)),
            base_height=float(data.get("base_height", 60.0)),
            thickness=float(data.get("thickness", 1.5)),
            material=mat,
            k_factor=float(data["k_factor"]) if data.get("k_factor") is not None else None,
            default_radius=float(data["default_radius"]) if data.get("default_radius") is not None else None,
            base_holes=base_holes,
            flanges=flanges,
        )


@dataclass
class BendLine2D:
    x1: float
    y1: float
    x2: float
    y2: float
    edge: str
    angle_deg: float
    inner_radius: float
    bend_allowance: float
    bend_deduction: float

    @property
    def direction(self) -> str:
        return "UP" if self.angle_deg >= 0 else "DOWN"

    def to_dict(self) -> dict[str, Any]:
        return {
            "x1": round(self.x1, 3),
            "y1": round(self.y1, 3),
            "x2": round(self.x2, 3),
            "y2": round(self.y2, 3),
            "edge": self.edge,
            "direction": self.direction,
            "angle_deg": self.angle_deg,
            "inner_radius": self.inner_radius,
            "bend_allowance": round(self.bend_allowance, 3),
            "bend_deduction": round(self.bend_deduction, 3),
        }


@dataclass
class FlatPatternResult:
    """钣金 2D 无损展开下料结果。"""
    part_name: str
    material_name: str
    thickness: float
    k_factor: float
    flat_width: float
    flat_height: float
    blank_area_mm2: float
    estimated_mass_g: float
    outline_polygons: list[list[tuple[float, float]]]   # DXF/SVG 闭合切割多边形轮廓
    flat_holes: list[SheetHole]                         # 映射至展开平面的全部孔位 (左下角偏移至 [0,0] 起)
    bend_lines: list[BendLine2D]
    dxf_content: str = ""
    svg_content: str = ""
    scad_3d: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "part_name": self.part_name,
            "material_name": self.material_name,
            "thickness": self.thickness,
            "k_factor": self.k_factor,
            "flat_width": round(self.flat_width, 3),
            "flat_height": round(self.flat_height, 3),
            "blank_area_mm2": round(self.blank_area_mm2, 2),
            "estimated_mass_g": round(self.estimated_mass_g, 2),
            "holes_count": len(self.flat_holes),
            "bends_count": len(self.bend_lines),
            "bend_lines": [b.to_dict() for b in self.bend_lines],
            "flat_holes": [h.to_dict() for h in self.flat_holes],
        }


# ==============================================================================
# 1. 钣金三维建模 & 2D 展开核心求解器
# ==============================================================================

def _get_mat_params(part: SheetMetalPart) -> tuple[dict[str, Any], float, float]:
    mat = SHEET_METAL_MATERIALS.get(part.material, SHEET_METAL_MATERIALS["ALUMINUM_5052"])
    k = part.k_factor if part.k_factor is not None else float(mat["k_factor"])
    r_def = part.default_radius if part.default_radius is not None else max(part.thickness * float(mat["min_bend_radius_ratio"]), 0.5)
    return mat, k, r_def


def unfold_sheet_metal(spec_or_part: SheetMetalPart | dict[str, Any]) -> FlatPatternResult:
    """将三维折弯钣金件精确展开为二维下料平面，计算折弯线、外轮廓与孔位映射。"""
    part = spec_or_part if isinstance(spec_or_part, SheetMetalPart) else SheetMetalPart.from_dict(spec_or_part)
    mat_info, k, r_def = _get_mat_params(part)
    T = part.thickness
    W = part.base_width
    H = part.base_height

    flange_map: dict[str, FlangeSpec] = {f.edge: f for f in part.flanges}

    # 计算四边外延展开量与折弯线坐标 (局部基板坐标系: [0, W] x [0, H])
    ossb_map: dict[str, float] = {}
    ba_map: dict[str, float] = {}
    bd_map: dict[str, float] = {}
    ext_map: dict[str, float] = {"left": 0.0, "right": 0.0, "top": 0.0, "bottom": 0.0}

    for edge, fl in flange_map.items():
        r = fl.inner_radius if fl.inner_radius is not None else r_def
        ossb = compute_outside_setback(fl.angle_deg, r, T)
        ba = compute_bend_allowance(fl.angle_deg, r, T, k)
        bd = 2.0 * ossb - ba
        ossb_map[edge] = ossb
        ba_map[edge] = ba
        bd_map[edge] = bd
        # 展开相对于外模线边界的外伸净增值 = fl.length - BD
        ext_map[edge] = max(fl.length - bd, 0.5)

    # 总展开外包络偏移 (把展开后最左下角对齐至 [0, 0])
    shift_x = ext_map["left"]
    shift_y = ext_map["bottom"]
    flat_w = ext_map["left"] + W + ext_map["right"]
    flat_h = ext_map["bottom"] + H + ext_map["top"]

    # 基板有效平展角点 (扣除相邻折弯的 OSSB 后加上止裂间隙)
    bx0 = ossb_map.get("left", 0.0)
    bx1 = W - ossb_map.get("right", 0.0)
    by0 = ossb_map.get("bottom", 0.0)
    by1 = H - ossb_map.get("top", 0.0)

    # 构造带拐角让位槽的 2D 展开多边形轮廓 (顺时针或逆时针闭合点集，在平移前坐标系生成再整体偏移)
    # 先收集矩形并集/连续外围环路径
    pts: list[tuple[float, float]] = []
    # 如果有相邻两边同时存在翻边，为翼缘宽度留出止裂让位 (relief = max(T, 1.5))
    relief = max(T * 1.2, 1.5)

    def _edge_span_y(edge: str) -> tuple[float, float]:
        y_lo = (by0 + relief) if "bottom" in flange_map else 0.0
        y_hi = (by1 - relief) if "top" in flange_map else H
        return y_lo, y_hi

    def _edge_span_x(edge: str) -> tuple[float, float]:
        x_lo = (bx0 + relief) if "left" in flange_map else 0.0
        x_hi = (bx1 - relief) if "right" in flange_map else W
        return x_lo, x_hi

    # 从底边左端开始逆时针追踪展开外轮廓
    # 1. Bottom edge / flange
    if "bottom" in flange_map:
        fx0, fx1 = _edge_span_x("bottom")
        y_bot = -ext_map["bottom"]
        pts.extend([(0.0, 0.0) if "left" not in flange_map else (fx0, 0.0), (fx0, y_bot), (fx1, y_bot), (fx1, 0.0)])
        if "right" not in flange_map:
            pts.append((W, 0.0))
    else:
        pts.append((0.0, 0.0))
        pts.append((W, 0.0))

    # 2. Right edge / flange
    if "right" in flange_map:
        fy0, fy1 = _edge_span_y("right")
        x_right = W + ext_map["right"]
        pts.extend([(W, fy0), (x_right, fy0), (x_right, fy1), (W, fy1)])
        if "top" not in flange_map:
            pts.append((W, H))
    else:
        pts.append((W, H))

    # 3. Top edge / flange
    if "top" in flange_map:
        fx0, fx1 = _edge_span_x("top")
        y_top = H + ext_map["top"]
        pts.extend([(fx1, H), (fx1, y_top), (fx0, y_top), (fx0, H)])
        if "left" not in flange_map:
            pts.append((0.0, H))
    else:
        pts.append((0.0, H))

    # 4. Left edge / flange
    if "left" in flange_map:
        fy0, fy1 = _edge_span_y("left")
        x_left = -ext_map["left"]
        pts.extend([(0.0, fy1), (x_left, fy1), (x_left, fy0), (0.0, fy0)])

    # 清理连续重复点并整体偏移至正象限 [0, 0] 起
    cleaned_pts: list[tuple[float, float]] = []
    for p in pts:
        sp = (round(p[0] + shift_x, 4), round(p[1] + shift_y, 4))
        if not cleaned_pts or (abs(cleaned_pts[-1][0] - sp[0]) > 1e-4 or abs(cleaned_pts[-1][1] - sp[1]) > 1e-4):
            cleaned_pts.append(sp)
    if len(cleaned_pts) > 2 and abs(cleaned_pts[0][0] - cleaned_pts[-1][0]) < 1e-4 and abs(cleaned_pts[0][1] - cleaned_pts[-1][1]) < 1e-4:
        cleaned_pts.pop()

    # 计算精准展开毛坯面积 (Shoelace 公式减去孔面积)
    area = 0.0
    n_p = len(cleaned_pts)
    for i in range(n_p):
        x1, y1 = cleaned_pts[i]
        x2, y2 = cleaned_pts[(i + 1) % n_p]
        area += x1 * y2 - x2 * y1
    area = abs(area) / 2.0

    # 收集折弯中心线
    bend_lines: list[BendLine2D] = []
    # 收集映射到 2D 展开空间的所有孔
    flat_holes: list[SheetHole] = []
    for bh in part.base_holes:
        flat_holes.append(SheetHole(x=bh.x + shift_x, y=bh.y + shift_y, diameter=bh.diameter, label=bh.label))
        area -= math.pi * (bh.diameter / 2.0) ** 2

    for edge, fl in flange_map.items():
        r = fl.inner_radius if fl.inner_radius is not None else r_def
        ossb = ossb_map[edge]
        ba = ba_map[edge]
        bd = bd_map[edge]

        if edge == "right":
            bend_cx = (W - ossb) + ba / 2.0 + shift_x
            fy0, fy1 = _edge_span_y("right")
            bend_lines.append(BendLine2D(bend_cx, fy0 + shift_y, bend_cx, fy1 + shift_y, edge, fl.angle_deg, r, ba, bd))
            for fh in fl.holes:
                # 翼缘局部 x 沿折弯高度方向 (从折弯底部向外), 局部 y 沿基板 Y
                hx = (W - ossb) + ba + max(fh.x - ossb, 0.0) + shift_x
                hy = fh.y + shift_y
                flat_holes.append(SheetHole(x=hx, y=hy, diameter=fh.diameter, label=fh.label))
                area -= math.pi * (fh.diameter / 2.0) ** 2

        elif edge == "left":
            bend_cx = ossb - ba / 2.0 + shift_x
            fy0, fy1 = _edge_span_y("left")
            bend_lines.append(BendLine2D(bend_cx, fy0 + shift_y, bend_cx, fy1 + shift_y, edge, fl.angle_deg, r, ba, bd))
            for fh in fl.holes:
                hx = ossb - ba - max(fh.x - ossb, 0.0) + shift_x
                hy = fh.y + shift_y
                flat_holes.append(SheetHole(x=hx, y=hy, diameter=fh.diameter, label=fh.label))
                area -= math.pi * (fh.diameter / 2.0) ** 2

        elif edge == "top":
            bend_cy = (H - ossb) + ba / 2.0 + shift_y
            fx0, fx1 = _edge_span_x("top")
            bend_lines.append(BendLine2D(fx0 + shift_x, bend_cy, fx1 + shift_x, bend_cy, edge, fl.angle_deg, r, ba, bd))
            for fh in fl.holes:
                hx = fh.x + shift_x
                hy = (H - ossb) + ba + max(fh.y - ossb, 0.0) + shift_y
                flat_holes.append(SheetHole(x=hx, y=hy, diameter=fh.diameter, label=fh.label))
                area -= math.pi * (fh.diameter / 2.0) ** 2

        elif edge == "bottom":
            bend_cy = ossb - ba / 2.0 + shift_y
            fx0, fx1 = _edge_span_x("bottom")
            bend_lines.append(BendLine2D(fx0 + shift_x, bend_cy, fx1 + shift_x, bend_cy, edge, fl.angle_deg, r, ba, bd))
            for fh in fl.holes:
                hx = fh.x + shift_x
                hy = ossb - ba - max(fh.y - ossb, 0.0) + shift_y
                flat_holes.append(SheetHole(x=hx, y=hy, diameter=fh.diameter, label=fh.label))
                area -= math.pi * (fh.diameter / 2.0) ** 2

    area = max(area, 1.0)
    mass_g = (area * T / 1000.0) * float(mat_info["density_g_cm3"])

    res = FlatPatternResult(
        part_name=part.name,
        material_name=mat_info["name"],
        thickness=T,
        k_factor=k,
        flat_width=flat_w,
        flat_height=flat_h,
        blank_area_mm2=area,
        estimated_mass_g=mass_g,
        outline_polygons=[cleaned_pts],
        flat_holes=flat_holes,
        bend_lines=bend_lines,
    )
    res.dxf_content = generate_sheet_metal_flat_dxf(res)
    res.svg_content = generate_sheet_metal_flat_svg(res)
    res.scad_3d = generate_sheet_metal_3d_scad(part)
    return res


def generate_sheet_metal_3d_scad(spec_or_part: SheetMetalPart | dict[str, Any]) -> str:
    """生成带圆弧折弯区、止裂让位与安装孔的三维钣金折叠 OpenSCAD 实体代码。"""
    part = spec_or_part if isinstance(spec_or_part, SheetMetalPart) else SheetMetalPart.from_dict(spec_or_part)
    mat_info, k, r_def = _get_mat_params(part)
    T = part.thickness
    W = part.base_width
    H = part.base_height
    color = mat_info["color"]

    flange_map: dict[str, FlangeSpec] = {f.edge: f for f in part.flanges}
    ossb_map: dict[str, float] = {}
    for edge, fl in flange_map.items():
        r = fl.inner_radius if fl.inner_radius is not None else r_def
        ossb_map[edge] = compute_outside_setback(fl.angle_deg, r, T)

    bx0 = ossb_map.get("left", 0.0)
    bx1 = W - ossb_map.get("right", 0.0)
    by0 = ossb_map.get("bottom", 0.0)
    by1 = H - ossb_map.get("top", 0.0)
    relief = max(T * 1.2, 1.5)

    lines = [
        f"// ========================================================",
        f"// 工业级三维钣金折弯件: {part.name}",
        f"// 材料: {mat_info['name']} | 板厚 T={T:.2f}mm | K-Factor={k:.2f}",
        f"// 基板尺寸: {W:.1f} x {H:.1f} mm | 折弯翼缘数: {len(part.flanges)}",
        f"// ========================================================",
        f"$fn = 48;",
        f"color(\"{color}\") {{",
        f"  difference() {{",
        f"    union() {{",
        f"      // 1. 基板主平面 ({bx1 - bx0:.2f} x {by1 - by0:.2f} x {T:.2f} mm)",
        f"      translate([{bx0:.2f}, {by0:.2f}, 0])",
        f"        cube([{max(bx1 - bx0, 1.0):.2f}, {max(by1 - by0, 1.0):.2f}, {T:.2f}], center = false);",
    ]

    # 若一边没有翻边但相邻边有，需要把基板角补全到外模线边界
    if "left" not in flange_map and ("top" in flange_map or "bottom" in flange_map):
        pass  # bx0 is already 0.0
    if "right" not in flange_map and ("top" in flange_map or "bottom" in flange_map):
        pass  # bx1 is already W

    def _span_y() -> tuple[float, float]:
        y_lo = (by0 + relief) if "bottom" in flange_map else 0.0
        y_hi = (by1 - relief) if "top" in flange_map else H
        return y_lo, max(y_hi - y_lo, 2.0)

    def _span_x() -> tuple[float, float]:
        x_lo = (bx0 + relief) if "left" in flange_map else 0.0
        x_hi = (bx1 - relief) if "right" in flange_map else W
        return x_lo, max(x_hi - x_lo, 2.0)

    # 模块辅助: 沿局部 +X 方向末端向 +Z 折弯 alpha 度 (起始于原点 [0, 0, 0] 板厚 [0, T], Y 宽 width)
    def _emit_flange_block(edge: str, fl: FlangeSpec) -> list[str]:
        r = fl.inner_radius if fl.inner_radius is not None else r_def
        ossb = ossb_map[edge]
        ang = abs(fl.angle_deg)
        is_down = fl.angle_deg < 0
        leg_len = max(fl.length - ossb, 1.0)
        b_lines = []

        if edge in ("right", "left"):
            y0, w_span = _span_y()
        else:
            x0, w_span = _span_x()

        # 生成局部 +X 向的折弯弧 + 直臂几何
        def _local_bend_arm(span_w: float) -> list[str]:
            loc = [
                f"        // 圆弧折弯过渡区 (R={r:.2f}mm, 角度={fl.angle_deg:.1f}°)",
                f"        translate([0, 0, {T + r:.2f}])",
                f"          rotate([90, 0, 0])",
                f"            translate([0, 0, -{span_w:.2f}])",
                f"              rotate([0, 0, -90])",
                f"                rotate_extrude(angle = {ang:.2f}, convexity = 6)",
                f"                  translate([{r:.2f}, 0, 0])",
                f"                    square([{T:.2f}, {span_w:.2f}], center = false);",
                f"        // 折弯直臂段 (长={leg_len:.2f}mm)",
                f"        translate([0, 0, {T + r:.2f}])",
                f"          rotate([0, -{ang:.2f}, 0])",
                f"            translate([0, 0, -{T + r:.2f}])",
                f"              cube([{leg_len:.2f}, {span_w:.2f}, {T:.2f}], center = false);",
            ]
            return loc

        if edge == "right":
            y0, w_span = _span_y()
            b_lines.append(f"      // 右侧折弯翼缘 (Right Flange)")
            b_lines.append(f"      translate([{W - ossb:.2f}, {y0:.2f}, 0]) {{")
            if is_down:
                b_lines.append(f"        translate([0, 0, {T:.2f}]) mirror([0, 0, 1]) {{")
            b_lines.extend(_local_bend_arm(w_span))
            if is_down:
                b_lines.append(f"        }}")
            b_lines.append(f"      }}")
        elif edge == "left":
            y0, w_span = _span_y()
            b_lines.append(f"      // 左侧折弯翼缘 (Left Flange)")
            b_lines.append(f"      translate([{ossb:.2f}, {y0 + w_span:.2f}, 0]) rotate([0, 0, 180]) {{")
            if is_down:
                b_lines.append(f"        translate([0, 0, {T:.2f}]) mirror([0, 0, 1]) {{")
            b_lines.extend(_local_bend_arm(w_span))
            if is_down:
                b_lines.append(f"        }}")
            b_lines.append(f"      }}")
        elif edge == "top":
            x0, w_span = _span_x()
            b_lines.append(f"      // 顶边折弯翼缘 (Top Flange)")
            b_lines.append(f"      translate([{x0 + w_span:.2f}, {H - ossb:.2f}, 0]) rotate([0, 0, 90]) {{")
            if is_down:
                b_lines.append(f"        translate([0, 0, {T:.2f}]) mirror([0, 0, 1]) {{")
            b_lines.extend(_local_bend_arm(w_span))
            if is_down:
                b_lines.append(f"        }}")
            b_lines.append(f"      }}")
        elif edge == "bottom":
            x0, w_span = _span_x()
            b_lines.append(f"      // 底边折弯翼缘 (Bottom Flange)")
            b_lines.append(f"      translate([{x0:.2f}, {ossb:.2f}, 0]) rotate([0, 0, -90]) {{")
            if is_down:
                b_lines.append(f"        translate([0, 0, {T:.2f}]) mirror([0, 0, 1]) {{")
            b_lines.extend(_local_bend_arm(w_span))
            if is_down:
                b_lines.append(f"        }}")
            b_lines.append(f"      }}")
        return b_lines

    for edge, fl in flange_map.items():
        lines.extend(_emit_flange_block(edge, fl))

    lines.append(f"    }}")

    # 减去基板及翼缘上的安装孔
    lines.append(f"    // 安装孔与工艺孔系扣除")
    if part.base_holes:
        for bh in part.base_holes:
            lines.append(f"    translate([{bh.x:.2f}, {bh.y:.2f}, -0.5])")
            lines.append(f"      cylinder(d = {bh.diameter:.2f}, h = {T + 1.0:.2f}, $fn = 32, center = false);")
    for edge, fl in flange_map.items():
        if not fl.holes:
            continue
        r = fl.inner_radius if fl.inner_radius is not None else r_def
        ossb = ossb_map[edge]
        ang = abs(fl.angle_deg)
        sign = -1.0 if fl.angle_deg >= 0 else 1.0
        for fh in fl.holes:
            if edge == "right":
                lines.append(f"    translate([{W - ossb:.2f}, {fh.y:.2f}, {T/2:.2f}]) rotate([0, {sign * ang:.2f}, 0]) translate([{max(fh.x - ossb, 0):.2f}, 0, -5.0]) cylinder(d = {fh.diameter:.2f}, h = 10.0, $fn = 32);")
            elif edge == "left":
                lines.append(f"    translate([{ossb:.2f}, {fh.y:.2f}, {T/2:.2f}]) rotate([0, {-sign * ang:.2f}, 0]) translate([-{max(fh.x - ossb, 0):.2f}, 0, -5.0]) cylinder(d = {fh.diameter:.2f}, h = 10.0, $fn = 32);")

    lines.append(f"  }}")
    lines.append(f"}}")
    return "\n".join(lines)


# ==============================================================================
# 2. 工业标准 DXF 下料图 & 矢量 SVG 图纸生成
# ==============================================================================

def generate_sheet_metal_flat_dxf(flat: FlatPatternResult) -> str:
    """纯 Python 原生生成工业标准 R12 ASCII DXF 下料工程图 (分 CUT 与 BEND 图层)。"""
    dxf: list[str] = [
        "0", "SECTION",
        "2", "HEADER",
        "9", "$INSUNITS", "70", "4",  # 4 = Millimeters
        "0", "ENDSEC",
        "0", "SECTION",
        "2", "TABLES",
        "0", "TABLE", "2", "LAYER", "70", "2",
        "0", "LAYER", "2", "CUT", "70", "0", "62", "7", "6", "CONTINUOUS",
        "0", "LAYER", "2", "BEND", "70", "0", "62", "1", "6", "DASHED",
        "0", "ENDTAB",
        "0", "ENDSEC",
        "0", "SECTION",
        "2", "ENTITIES",
    ]

    # 1. CUT 图层: 外切割轮廓线段
    for poly in flat.outline_polygons:
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            dxf.extend([
                "0", "LINE",
                "8", "CUT",
                "10", f"{x1:.4f}", "20", f"{y1:.4f}", "30", "0.0",
                "11", f"{x2:.4f}", "21", f"{y2:.4f}", "31", "0.0",
            ])

    # 2. CUT 图层: 圆孔
    for h in flat.flat_holes:
        dxf.extend([
            "0", "CIRCLE",
            "8", "CUT",
            "10", f"{h.x:.4f}", "20", f"{h.y:.4f}", "30", "0.0",
            "40", f"{h.diameter / 2.0:.4f}",
        ])

    # 3. BEND 图层: 折弯线与文本注释
    for b in flat.bend_lines:
        dxf.extend([
            "0", "LINE",
            "8", "BEND",
            "10", f"{b.x1:.4f}", "20", f"{b.y1:.4f}", "30", "0.0",
            "11", f"{b.x2:.4f}", "21", f"{b.y2:.4f}", "31", "0.0",
        ])
        mx = (b.x1 + b.x2) / 2.0
        my = (b.y1 + b.y2) / 2.0
        note = f"{b.direction} {abs(b.angle_deg):.0f}deg R{b.inner_radius:.1f}"
        dxf.extend([
            "0", "TEXT",
            "8", "BEND",
            "10", f"{mx:.4f}", "20", f"{my:.4f}", "30", "0.0",
            "40", "2.5",
            "1", note,
        ])

    dxf.extend(["0", "ENDSEC", "0", "EOF"])
    return "\n".join(dxf)


def generate_sheet_metal_flat_svg(flat: FlatPatternResult) -> str:
    """生成带折弯方向/角度注释、标准图框与尺寸标记的钣金展开矢量 SVG 图纸。"""
    pad = 25.0
    title_h = 32.0
    vb_w = flat.flat_width + pad * 2.0
    vb_h = flat.flat_height + pad * 2.0 + title_h

    # SVG Y 轴向下，翻转数学坐标系 Y' = pad + flat_height - y
    def _sx(x: float) -> float:
        return pad + x

    def _sy(y: float) -> float:
        return pad + flat.flat_height - y

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w:.1f} {vb_h:.1f}" width="{max(vb_w * 3, 420):.0f}" height="{max(vb_h * 3, 320):.0f}" style="background:#f8fafc;font-family:sans-serif;">',
        f'  <!-- Outer Cutting Contour -->',
    ]

    for poly in flat.outline_polygons:
        pts_str = " ".join(f"{_sx(x):.2f},{_sy(y):.2f}" for x, y in poly)
        svg.append(f'  <polygon points="{pts_str}" fill="#e2e8f0" fill-opacity="0.65" stroke="#0f172a" stroke-width="0.9"/>')

    # Holes
    for h in flat.flat_holes:
        cx, cy = _sx(h.x), _sy(h.y)
        r = h.diameter / 2.0
        svg.append(f'  <circle cx="{cx:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="#ffffff" stroke="#0f172a" stroke-width="0.7"/>')
        svg.append(f'  <line x1="{cx - r - 1.5:.2f}" y1="{cy:.2f}" x2="{cx + r + 1.5:.2f}" y2="{cy:.2f}" stroke="#64748b" stroke-width="0.3" stroke-dasharray="1.5,1"/>')
        svg.append(f'  <line x1="{cx:.2f}" y1="{cy - r - 1.5:.2f}" x2="{cx:.2f}" y2="{cy + r + 1.5:.2f}" stroke="#64748b" stroke-width="0.3" stroke-dasharray="1.5,1"/>')

    # Bend Lines
    for b in flat.bend_lines:
        color = "#dc2626" if b.direction == "UP" else "#2563eb"
        x1, y1 = _sx(b.x1), _sy(b.y1)
        x2, y2 = _sx(b.x2), _sy(b.y2)
        svg.append(f'  <line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" stroke="{color}" stroke-width="0.8" stroke-dasharray="3,2"/>')
        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        lbl = f'{b.direction} {abs(b.angle_deg):.0f}° R{b.inner_radius:.1f}'
        svg.append(f'  <text x="{mx:.2f}" y="{my - 1.5:.2f}" fill="{color}" font-size="3.5" text-anchor="middle" font-weight="bold">{lbl}</text>')

    # Overall Dimensions Callouts
    svg.append(f'  <text x="{vb_w / 2.0:.1f}" y="{pad - 8:.1f}" fill="#334155" font-size="4.5" text-anchor="middle">展开毛坯总宽 (Blank W): {flat.flat_width:.2f} mm</text>')
    svg.append(f'  <text x="{pad - 8:.1f}" y="{pad + flat.flat_height / 2.0:.1f}" fill="#334155" font-size="4.5" text-anchor="middle" transform="rotate(-90 {pad - 8:.1f},{pad + flat.flat_height / 2.0:.1f})">展开高 (H): {flat.flat_height:.2f} mm</text>')

    # Engineering Title Block at bottom
    ty = pad + flat.flat_height + 10.0
    svg.append(f'  <rect x="4" y="{ty:.1f}" width="{vb_w - 8:.1f}" height="20" fill="#ffffff" stroke="#475569" stroke-width="0.6"/>')
    svg.append(f'  <text x="8" y="{ty + 7:.1f}" fill="#0f172a" font-size="4.0" font-weight="bold">PART: {flat.part_name} | MAT: {flat.material_name} | T={flat.thickness:.2f}mm (K={flat.k_factor:.2f})</text>')
    svg.append(f'  <text x="8" y="{ty + 15:.1f}" fill="#475569" font-size="3.6">BLANK: {flat.flat_width:.2f} x {flat.flat_height:.2f} mm | AREA: {flat.blank_area_mm2:.1f} mm² | MASS: {flat.estimated_mass_g:.1f} g | BENDS: {len(flat.bend_lines)}</text>')
    svg.append('</svg>')
    return "\n".join(svg)


# ==============================================================================
# 3. 常用工业钣金标准构件预设生成器
# ==============================================================================

def create_sheet_metal_preset(
    preset_name: str = "u_channel",
    width: float = 80.0,
    height: float = 60.0,
    flange_len: float = 25.0,
    thickness: float = 1.5,
    material: str = "ALUMINUM_5052",
) -> SheetMetalPart:
    """快速构建常用工业钣金模型预设 (L型支架 / U型槽 / 四周翻边机盒托盘)。"""
    p_name = str(preset_name).lower().strip()
    holes = [
        SheetHole(12.0, 12.0, 4.5, "M4"),
        SheetHole(width - 12.0, 12.0, 4.5, "M4"),
        SheetHole(12.0, height - 12.0, 4.5, "M4"),
        SheetHole(width - 12.0, height - 12.0, 4.5, "M4"),
    ]
    if "l_bracket" in p_name or "l" == p_name:
        return SheetMetalPart(
            name="L型安装支架",
            base_width=width,
            base_height=height,
            thickness=thickness,
            material=material,
            base_holes=holes[:2],
            flanges=[
                FlangeSpec(edge="right", length=flange_len, angle_deg=90.0, holes=[SheetHole(flange_len * 0.6, height / 2.0, 4.5, "M4")]),
            ],
        )
    elif "tray" in p_name or "box" in p_name or "enclosure" in p_name:
        return SheetMetalPart(
            name="四周翻边机箱托盘",
            base_width=width,
            base_height=height,
            thickness=thickness,
            material=material,
            base_holes=holes,
            flanges=[
                FlangeSpec(edge="left", length=flange_len, angle_deg=90.0),
                FlangeSpec(edge="right", length=flange_len, angle_deg=90.0),
                FlangeSpec(edge="top", length=flange_len, angle_deg=90.0),
                FlangeSpec(edge="bottom", length=flange_len, angle_deg=90.0),
            ],
        )
    else:  # u_channel
        return SheetMetalPart(
            name="U型折弯槽架",
            base_width=width,
            base_height=height,
            thickness=thickness,
            material=material,
            base_holes=holes,
            flanges=[
                FlangeSpec(edge="left", length=flange_len, angle_deg=90.0, holes=[SheetHole(flange_len * 0.6, height / 2.0, 4.5, "M4")]),
                FlangeSpec(edge="right", length=flange_len, angle_deg=90.0, holes=[SheetHole(flange_len * 0.6, height / 2.0, 4.5, "M4")]),
            ],
        )
