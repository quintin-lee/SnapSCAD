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

    def summary(self) -> str:
        status = "✅ 优良" if self.score >= 90 else ("⚠️ 需注意" if self.score >= 70 else "❌ 风险较高")
        return f"适印性评分: {self.score}/100 ({status})"


def _estimate_primitive_volume_mm3(prim: str, size: list[float]) -> float:
    """粗略估算基础几何体的体积 (mm³)。"""
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
    return 1000.0


def check_printability(spec: dict[str, Any]) -> PrintabilityReport:
    """对模型规格执行全方位 3D 打印工程自检。"""
    report = PrintabilityReport()
    parts = spec.get("parts") or []
    if not parts:
        report.printable = True
        return report

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
            # 更新包围盒
            min_x = min(min_x, cx - w / 2)
            max_x = max(max_x, cx + w / 2)
            min_y = min(min_y, cy - d / 2)
            max_y = max(max_y, cy + d / 2)
            min_z = min(min_z, cz)
            max_z = max(max_z, cz + h)

        elif prim in ("cylinder", "capsule"):
            d = size[0]
            h = size[1] if len(size) >= 2 else 10.0
            r = d / 2.0
            if d < 0.8:
                report.warnings.append(f"部件 '{pid}' 直径过小 ({d:.2f}mm)，打印易折断")
                report.score -= 15
            elif d > 0 and h / d > 10.0:
                report.warnings.append(f"部件 '{pid}' 高径比过大 ({h/d:.1f})，细长立柱在高速移动时易晃动拉丝")
                report.tips.append(f"针对部件 '{pid}' 建议切片时开启外侧支撑或大底裙边 (Brim)")
                report.score -= 5
            # 更新包围盒
            min_x = min(min_x, cx - r)
            max_x = max(max_x, cx + r)
            min_y = min(min_y, cy - r)
            max_y = max(max_y, cy + r)
            min_z = min(min_z, cz)
            max_z = max(max_z, cz + h)

        elif prim == "sphere":
            r = size[0]
            if r < 1.0:
                report.warnings.append(f"球体 '{pid}' 半径极小 ({r:.2f}mm)")
                report.score -= 10
            min_x = min(min_x, cx - r)
            max_x = max(max_x, cx + r)
            min_y = min(min_y, cy - r)
            max_y = max(max_y, cy + r)
            min_z = min(min_z, cz - r)
            max_z = max(max_z, cz + r)

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

    # 包围盒评估
    dim_x = max(max_x - min_x, 0.0) if min_x != float("inf") else 0.0
    dim_y = max(max_y - min_y, 0.0) if min_y != float("inf") else 0.0
    dim_z = max(max_z - min_z, 0.0) if min_z != float("inf") else 0.0
    report.bounding_box = (round(dim_x, 1), round(dim_y, 1), round(dim_z, 1))

    max_bed_size = 256.0
    if max(dim_x, dim_y, dim_z) > max_bed_size:
        report.tips.append(f"模型外轮廓 ({max(dim_x, dim_y, dim_z):.1f}mm) 超出常见 256mm 热床，需对角线排布或切块打印")
        report.score -= 5

    # 体积、耗材克重与耗时估算
    total_vol_mm3 = 0.0
    for p in parts:
        prim = p.get("primitive", "cube")
        size = normalize_size(prim, p)
        total_vol_mm3 += _estimate_primitive_volume_mm3(prim, size)
        for h in p.get("holes") or []:
            h_prim = h.get("primitive", "cube")
            h_size = normalize_hole_size(h_prim, h)
            total_vol_mm3 = max(total_vol_mm3 - _estimate_primitive_volume_mm3(h_prim, h_size), 0.0)

    vol_cm3 = round(total_vol_mm3 / 1000.0, 2)
    # 按 15% 内部填充 + 3 圈实心壁厚，折合实体填充率约 35%，PLA 密度 1.24g/cm³
    est_weight = round(vol_cm3 * 1.24 * 0.38, 1)
    # 典型走丝速度 60mm/s 挤出速率约 12 cm³/小时
    est_hours = round(max((vol_cm3 * 0.38) / 12.0, 0.2), 1)
    # 耗材成本核算：以工业标准 PLA 均价 ¥80/kg 与 $20/kg 测算
    cost_cny = round((est_weight / 1000.0) * 80.0, 2)
    cost_usd = round((est_weight / 1000.0) * 20.0, 2)

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

    # 针对性工艺推导
    has_slender = any("细长" in w for w in report.warnings)
    brim_rec = "强烈建议开启 5mm 外侧裙边 (Outer Brim)" if has_slender else "可选开启裙边 (Brim: 3mm 保持底座稳定)"
    layer_rec = "0.16mm (高精度) 或 0.20mm (标准)" if max(bbox) < 80 else "0.20mm (标准)"

    warnings_md = "\n".join(f"- ⚠️ {w}" for w in report.warnings) if report.warnings else "- ✅ 全模型未检测到明显阻断性薄壁风险"
    tips_md = "\n".join(f"- 💡 {t}" for t in report.tips) if report.tips else "- 💡 推荐使用标准热床温度 (PEI: 60°C) 确保首层良好附着"
    user_tips = "\n".join(f"- 💡 {t}" for t in spec.get("print_tips") or [])

    content = f"""# 3D 打印切片与工艺指导书: {name}

> 由 OpenSCAD AI Agent 自动分析生成 · 适印性健康体检: **{report.summary()}**

## 📊 物理特性与耗材预估
| 指标 | 预估数值 | 评估参考 |
|---|---|---|
| **包围盒跨度** | `{bbox[0]} × {bbox[1]} × {bbox[2]} mm` | 适配常见 256×256mm 打印机 |
| **预估实体体积** | `{report.volume_cm3:.1f} cm³` | 净体积几何积分 |
| **预估耗材克重** | `约 {report.weight_g:.1f} g` | 以标准 PLA 耗材 / 15% 填充率测算 |
| **预估打印耗时** | `约 {report.print_time_hours:.1f} 小时` | 典型 60mm/s 走丝速度 |
| **预估耗材成本** | `约 ¥{report.cost_cny:.2f} 元 (${report.cost_usd:.2f})` | 工业级 PLA/PETG 市场均价折算 |

---

## ⚙️ 推荐切片工艺参数 (Slicer Configuration)

| 切片参数项 | 推荐设定值 | 针对性工程建议 |
|---|---|---|
| **首选耗材类型** | **PLA / PETG** | 收缩率低、尺寸稳定性优良 |
| **层高 (Layer Height)** | `{layer_rec}` | 首层建议 0.24mm，后续层 0.20mm |
| **外墙圈数 (Wall Loops)**| `3 圈 (约 1.2mm 壁厚)` | 提高部件外壳刚度 |
| **填充率与图案** | `15% ~ 20% (Gyroid 陀螺仪图案)` | 陀螺仪图案在各向受力均匀且不易与喷嘴刮擦 |
| **喷嘴温度** | `210°C (PLA) / 235°C (PETG)` | 保证熔融层间高附着力 |
| **热床温度** | `60°C (PEI 弹簧钢板)` | 防止首层翘边，打完自然脱落 |
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


__all__ = ["PrintabilityReport", "check_printability", "generate_print_guide"]
