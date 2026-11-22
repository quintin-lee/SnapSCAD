"""机械机构运动学铰链约束与动态仿真引擎 (Kinematic Joints & Motion Simulation Engine)。

职责：
- 从模型规格 (spec.json) 中解析装配体运动学拓扑与自由度 (DOF) 铰链约束；
- 支持常用机械运动副：
  1. 旋转铰链 (Revolute Joint / Hinge): 绕空间锚点与旋转轴在给定角度区间往复摆动/连续回转；
  2. 移动副滑块 (Prismatic Joint / Slider): 沿直线导轨/滑动槽方向的平移往复运动；
  3. 齿轮副耦合 (Gear Coupling): 主动齿轮与从动齿轮的定传动比反向齿合联动；
- 自动生成 OpenSCAD 原生动画控制块 ($t / anim_t / anim_smooth)，在 OpenSCAD 菜单开启 View -> Animate 即可原生 60 FPS 动态仿真；
- 提供 Python / Three.js 运动学姿态求值器 (Forward Kinematics)，驱动在线 Web 3D 工作台实时多轴联动。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .log import get_logger as _log_module

_log = _log_module()


@dataclass
class KinematicJoint:
    """机械运动副 (Kinematic Joint) 数据结构。"""
    id: str
    type: str  # "revolute", "prismatic", "gear_coupling"
    parent: str
    child: str
    pivot: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    axis: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0])
    limits: list[float] = field(default_factory=lambda: [-45.0, 45.0])
    speed: float = 1.0
    ratio: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "parent": self.parent,
            "child": self.child,
            "pivot": self.pivot,
            "axis": self.axis,
            "limits": self.limits,
            "speed": self.speed,
            "ratio": self.ratio,
        }


def _norm_vec(vec: Any, default: list[float]) -> list[float]:
    if isinstance(vec, (list, tuple)) and len(vec) >= 3:
        try:
            return [float(vec[0]), float(vec[1]), float(vec[2])]
        except (ValueError, TypeError):
            pass
    return list(default)


def _norm_limits(lim: Any, default: list[float]) -> list[float]:
    if isinstance(lim, (list, tuple)) and len(lim) >= 2:
        try:
            return [float(lim[0]), float(lim[1])]
        except (ValueError, TypeError):
            pass
    return list(default)


def parse_joints(spec: dict[str, Any]) -> list[KinematicJoint]:
    """从 spec 字典中解析显式或隐式的机构运动副约束列表。"""
    raw_joints = spec.get("joints")
    if raw_joints is None:
        raw_joints = spec.get("assembly", {}).get("joints")

    joints: list[KinematicJoint] = []
    if isinstance(raw_joints, list) and raw_joints:
        for idx, rj in enumerate(raw_joints):
            if not isinstance(rj, dict):
                continue
            jid = str(rj.get("id") or f"joint_{idx+1}")
            jtype = str(rj.get("type", "revolute")).strip().lower()
            if jtype not in ("revolute", "prismatic", "gear_coupling", "hinge", "slider"):
                jtype = "revolute"
            if jtype == "hinge":
                jtype = "revolute"
            elif jtype == "slider":
                jtype = "prismatic"

            parent = str(rj.get("parent", "base"))
            child = str(rj.get("child", "part"))
            pivot = _norm_vec(rj.get("pivot"), [0.0, 0.0, 0.0])
            axis = _norm_vec(rj.get("axis"), [0.0, 0.0, 1.0])
            # 归一化轴向量
            mag = math.sqrt(axis[0]**2 + axis[1]**2 + axis[2]**2)
            if mag > 1e-6:
                axis = [axis[0] / mag, axis[1] / mag, axis[2] / mag]
            else:
                axis = [0.0, 0.0, 1.0]

            def_lim = [-45.0, 45.0] if jtype == "revolute" else [0.0, 30.0]
            limits = _norm_limits(rj.get("limits"), def_lim)
            speed = float(rj.get("speed", 1.0))
            ratio = float(rj.get("ratio", 1.0))

            joints.append(KinematicJoint(
                id=jid,
                type=jtype,
                parent=parent,
                child=child,
                pivot=pivot,
                axis=axis,
                limits=limits,
                speed=speed,
                ratio=ratio,
            ))
        return joints

    # 自动推理机制：若未显式指定 joints 但模型具备明显多构件联动特征
    parts = spec.get("parts") or []
    if len(parts) >= 2:
        part_ids = [str(p.get("id", "")).lower() for p in parts]
        # 寻找动件候选词
        moving_keywords = ["arm", "lever", "door", "lid", "flap", "blade", "gear", "wheel", "rotor", "piston", "slider"]
        candidate_child: Optional[str] = None
        cand_type = "revolute"
        for pid in part_ids:
            for kw in moving_keywords:
                if kw in pid:
                    candidate_child = pid
                    if kw in ("piston", "slider"):
                        cand_type = "prismatic"
                    break
            if candidate_child:
                break

        if candidate_child:
            # 确定父部件 (非 candidate_child 的第一个部件)
            parent_id = next((p for p in part_ids if p != candidate_child), "base")
            # 寻找动件几何中心作为 pivot
            child_part_obj = next((p for p in parts if str(p.get("id", "")).lower() == candidate_child), None)
            c_pos = [0.0, 0.0, 0.0]
            if child_part_obj and "center" in child_part_obj:
                c_pos = _norm_vec(child_part_obj.get("center"), [0.0, 0.0, 0.0])

            inferred_joint = KinematicJoint(
                id=f"joint_{candidate_child}",
                type=cand_type,
                parent=parent_id,
                child=candidate_child,
                pivot=c_pos,
                axis=[0.0, 0.0, 1.0] if cand_type == "revolute" else [1.0, 0.0, 0.0],
                limits=[-60.0, 60.0] if cand_type == "revolute" else [0.0, 25.0],
                speed=1.0,
            )
            joints.append(inferred_joint)
            _log.info("[kinematics] 自动识别机械机构运动学铰链: %s (%s -> %s)", inferred_joint.id, parent_id, candidate_child)

    return joints


def compute_joint_pose(joint: KinematicJoint, t: float) -> dict[str, Any]:
    """计算归一化时间 step t (0.0~1.0) 下的瞬时几何变换。"""
    t = float(t) % 1.0
    # 正弦平滑往复因子 (0 ~ 1 ~ 0)
    smooth_factor = (1.0 + math.sin(2.0 * math.pi * t * joint.speed - math.pi / 2.0)) / 2.0
    min_val, max_val = joint.limits[0], joint.limits[1]
    curr_val = min_val + smooth_factor * (max_val - min_val)

    if joint.type == "revolute":
        return {
            "type": "revolute",
            "angle_deg": curr_val,
            "pivot": joint.pivot,
            "axis": joint.axis,
            "factor": smooth_factor,
        }
    elif joint.type == "prismatic":
        dx = curr_val * joint.axis[0]
        dy = curr_val * joint.axis[1]
        dz = curr_val * joint.axis[2]
        return {
            "type": "prismatic",
            "displacement": curr_val,
            "offset": [dx, dy, dz],
            "axis": joint.axis,
            "factor": smooth_factor,
        }
    elif joint.type == "gear_coupling":
        gear_angle = (t * 360.0 * joint.speed * joint.ratio) % 360.0
        return {
            "type": "gear_coupling",
            "angle_deg": gear_angle,
            "pivot": joint.pivot,
            "axis": joint.axis,
            "ratio": joint.ratio,
        }
    return {"type": "static"}


def generate_kinematic_scad_blocks(joints: list[KinematicJoint]) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """生成 OpenSCAD Customizer / 动画参数控制块与针对各动部件的包覆变换代码。

    返回:
    - header_lines: 注入在模型顶部的动画控制参数
    - wrapper_map: {child_part_id: (prefix_code, suffix_code)}
    """
    if not joints:
        return [], {}

    headers: list[str] = [
        "/* [Kinematic Animation] */",
        "// 机构动态仿真时间步 (勾选 OpenSCAD 菜单 View -> Animate 即可启动连续物理动态仿真)",
        "anim_t = $t; // [0:0.005:1]",
        "// 往复平滑正弦插值因子 (0 ~ 1 连续运动，消除加速度突变冲击)",
        "anim_smooth = (1 + sin(anim_t * 360 - 90)) / 2;",
        "",
    ]

    wrapper_map: dict[str, tuple[str, str]] = {}
    for j in joints:
        cid = j.child
        px, py, pz = f"{j.pivot[0]:.2f}", f"{j.pivot[1]:.2f}", f"{j.pivot[2]:.2f}"
        ax, ay, az = f"{j.axis[0]:.2f}", f"{j.axis[1]:.2f}", f"{j.axis[2]:.2f}"
        l_min, l_max = f"{j.limits[0]:.2f}", f"{j.limits[1]:.2f}"

        if j.type == "revolute":
            prefix = (
                f"translate([{px}, {py}, {pz}])\n"
                f"  rotate(a = {l_min} + anim_smooth * ({l_max} - ({l_min})), v = [{ax}, {ay}, {az}])\n"
                f"    translate([-({px}), -({py}), -({pz})])\n    "
            )
            suffix = ""
            wrapper_map[cid] = (prefix, suffix)
        elif j.type == "prismatic":
            prefix = (
                f"translate(({l_min} + anim_smooth * ({l_max} - ({l_min}))) * [{ax}, {ay}, {az}])\n    "
            )
            suffix = ""
            wrapper_map[cid] = (prefix, suffix)
        elif j.type == "gear_coupling":
            ratio_str = f"{j.ratio:.2f}"
            prefix = (
                f"translate([{px}, {py}, {pz}])\n"
                f"  rotate(a = anim_t * 360 * ({ratio_str}), v = [{ax}, {ay}, {az}])\n"
                f"    translate([-({px}), -({py}), -({pz})])\n    "
            )
            suffix = ""
            wrapper_map[cid] = (prefix, suffix)

    return headers, wrapper_map


__all__ = [
    "KinematicJoint",
    "parse_joints",
    "compute_joint_pose",
    "generate_kinematic_scad_blocks",
]
