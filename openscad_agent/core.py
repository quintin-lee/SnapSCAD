"""OpenSCAD Agent 核心模块：配置、OpenSCAD 渲染、图片预处理。

架构说明：
- 图片预处理不依赖 Pillow，直接调用系统 ImageMagick (magick/convert)
  缩放至 Agnes API 可接受的尺寸（<=2048px）并输出 PNG，避免引入额外依赖。
- OpenSCAD 渲染通过 subprocess 调用系统 openscad 可执行文件，
  支持导出 STL/OBJ/3MF 等 OpenSCAD 官方支持的格式。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def run_cmd(cmd: list[str], *, cwd: Optional[str] = None) -> tuple[int, str, str]:
    """运行命令，返回 (exit_code, stdout, stderr)。"""
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, timeout=600
    )
    return proc.returncode, proc.stdout, proc.stderr


def find_executable(name: str) -> str:
    """定位可执行文件（优先 PATH，其次常见安装目录），找不到则抛错。"""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
        f"C:\\Program Files\\OpenSCAD\\bin\\{name}.exe",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(
        f"未找到可执行文件 '{name}'。请先安装：sudo pacman -S openscad 或 https://openscad.org"
    )


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class AgentConfig:
    """Agent 运行配置。

    API key 从环境变量 AGNES_API_KEY / AGNES_API_TOKEN / APIHUB_AGNES_API_KEY
    依次读取，绝不写死在代码或文件中。
    """

    #: Agnes API Base URL
    base_url: str = "https://apihub.agnes-ai.com"
    #: 视觉识别使用的文本/多模态模型（OpenAI 兼容 chat completions）
    model: str = "agnes-3.0-flash"
    #: 采样温度，识别任务保持低温保证 JSON 结构稳定
    temperature: float = 0.2
    #: 生成的 OpenSCAD 工程输出目录
    output_dir: str = "out"
    #: 渲染使用的 OpenSCAD 可执行文件
    openscad_bin: str = "openscad"
    #: 导出 3D 模型的格式（OpenSCAD 支持 stl/obj/3mf/csg/bmp/png/amf）
    export_format: str = "stl"
    #: 渲染精度（Fast/Slow/High）
    render_precision: str = "High"
    #: 图片预处理最大边长（像素）
    image_max_side: int = 2048
    #: Agnes API 调用超时（秒）
    api_timeout: int = 120
    #: 额外透传给 LLM 的系统提示补充
    extra_system: str = ""

    def __post_init__(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        # 校验 OpenSCAD 可执行文件存在（仅定位，不阻塞后续非渲染步骤）
        try:
            self.openscad_bin = find_executable(self.openscad_bin)
        except FileNotFoundError:
            self.openscad_bin = "openscad"  # 延迟到渲染时再报错

    @property
    def api_key(self) -> Optional[str]:
        for var in ("AGNES_API_KEY", "AGNES_API_TOKEN", "APIHUB_AGNES_API_KEY"):
            key = os.environ.get(var)
            if key:
                return key
        return None

    def require_api_key(self) -> str:
        key = self.api_key
        if not key:
            raise RuntimeError(
                "缺少 Agnes API key。请设置环境变量 AGNES_API_KEY（或 AGNES_API_TOKEN / APIHUB_AGNES_API_KEY）"
            )
        return key


# ---------------------------------------------------------------------------
# OpenSCAD 渲染器
# ---------------------------------------------------------------------------

class OpenscadRenderer:
    """把 .scad 源文件渲染为 3D 模型或 2D 预览图。

    使用 openscad 官方 CLI：
      openscad -o out.stl model.scad        # 导出 3D 模型
      openscad --render -o preview.png model.scad  # 渲染 2D 预览
    """

    FORMATS = {"stl", "obj", "3mf", "csg", "bmp", "png", "amf"}

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.openscad = config.openscad_bin

    def _base_cmd(self) -> list[str]:
        return [self.openscad]

    def export_model(self, scad_path: Path, out_path: Path) -> Path:
        """导出 3D 模型（stl/obj/3mf）。"""
        fmt = out_path.suffix.lstrip(".").lower() or self.config.export_format
        if fmt not in self.FORMATS:
            raise ValueError(f"不支持的导出格式: {fmt}，可选: {sorted(self.FORMATS)}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = self._base_cmd() + [
            "-o", str(out_path),
            f"--export-format={fmt}",
            str(scad_path),
        ]
        code, out, err = run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"OpenSCAD 导出失败 ({scad_path.name} -> {out_path.name}):\n{err or out}")
        print(f"[render] 已导出 3D 模型: {out_path}")
        return out_path

    def render_preview(self, scad_path: Path, out_png: Path, size: tuple[int, int] = (800, 600)) -> Path:
        """渲染 2D 预览图（用于直观展示结果）。

        OpenSCAD 2021.01 CLI：--imgsize=WxH 指定 png 尺寸，--autocenter/--viewall
        让相机自适应物体。不强制 --render（老版本默认已求值），兼容新旧。
        """
        out_png.parent.mkdir(parents=True, exist_ok=True)
        cmd = self._base_cmd() + [
            "--autocenter", "--viewall",
            f"--imgsize={size[0]},{size[1]}",
            "-o", str(out_png), str(scad_path),
        ]
        code, out, err = run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"OpenSCAD 渲染预览失败 ({scad_path.name}):\n{err or out}")
        print(f"[render] 已生成预览图: {out_png}")
        return out_png

    def export_model_or_validate(self, scad_path: Path, out_stl: Optional[Path]) -> tuple[bool, str, Optional[Path]]:
        """导出模型与校验合二为一，避免对同一 .scad 重复 CSG 求值。

        若 out_stl 为 None，仅校验（导出到临时文件后删除）；
        若 out_stl 为路径，直接导出到该路径（兼作校验）。
        返回 (成功?, 消息, 实际导出的 STL 路径或 None)。
        """
        if out_stl is None:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as tf:
                tmp = tf.name
            target = Path(tmp)
            cleanup = True
        else:
            target = out_stl
            target.parent.mkdir(parents=True, exist_ok=True)
            cleanup = False
        cmd = self._base_cmd() + ["-o", str(target), "--export-format=stl", str(scad_path)]
        code, out, err = run_cmd(cmd)
        if cleanup:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
        ok = code == 0
        msg = (err or out).strip()
        if ok:
            return True, "ok", (None if cleanup else target)
        return False, msg[-2000:], None

    def validate(self, scad_path: Path) -> tuple[bool, str]:
        """语法校验：导出到临时 STL（仅校验，不保留）。"""
        ok, msg, _ = self.export_model_or_validate(scad_path, None)
        if ok:
            return True, "语法与编译检查通过"
        return False, msg[-2000:]


# ---------------------------------------------------------------------------
# 图片预处理
# ---------------------------------------------------------------------------

def preprocess_image(src: Path, dst: Path, max_side: int = 2048) -> Path:
    """用 ImageMagick 缩放图片到 max_side 以内并输出 PNG。

    不引入 Pillow 依赖；Manjaro 下 ImageMagick 通常已安装或可
    `sudo pacman -S imagemagick` 安装。
    """
    src = Path(src).expanduser()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        raise FileNotFoundError(
            "未找到 ImageMagick (magick/convert)。请安装: sudo pacman -S imagemagick"
        )

    # 读取尺寸，判断是否需要缩放
    probe = f'identify -format "%w %h" "{src}"'
    code, out, err = run_cmd(["bash", "-lc", probe])
    if code != 0:
        raise RuntimeError(f"无法读取图片尺寸 {src}: {err.strip()[:300]}")
    w, h = (int(x) for x in out.split())
    if max(w, h) <= max_side:
        # 尺寸已合规，仅转 PNG 统一格式
        cmd = [magick, str(src), str(dst)]
    else:
        cmd = [magick, str(src), "-resize", f"{max_side}x{max_side}>", "-strip", str(dst)]
    code, out, err = run_cmd(cmd)
    if code != 0:
        raise RuntimeError(f"图片预处理失败 {src} -> {dst}: {err.strip()[:300]}")
    print(f"[preprocess] 图片已预处理: {dst.name} (max_side={max_side})")
    return dst


# ---------------------------------------------------------------------------
# 模型规格（LLM 输出的结构化中间表示）
# ---------------------------------------------------------------------------

#: LLM 输出的模型规格 JSON Schema 说明（嵌入系统提示词）
SPEC_SCHEMA = """\
{
  "name": "string, 模型名称（英文, 用作文件名, 如 'gearbox_housing'）",
  "description": "string, 对模型的一句话中文描述",
  "units": "mm",
  "params": { "key": number, ... },          // 自定义参数（可选）
  "parts": [
    {
      "id": "string, 部件唯一 id（如 'body'）",
      "primitive": "cube|cylinder|sphere|polyhedron",
      // 尺寸字段（二选一，优先 size）:
      //   cube:     size: [x, y, z]
      //   cylinder: size: [直径, 高度]  或  radius: N, height: N
      //   sphere:   size: [半径]       或  radius: N
      "size": [x, y, z],
      "radius": N,          // 圆柱/球专用（size 缺省时使用）
      "height": N,          // 圆柱专用（size 缺省时使用）
      "center": [x, y, z], // 部件原点坐标（mm）
      "rotation": [rx, ry, rz], // 绕三轴角度（度，可选）
      "holes": [           // 需减去的实体（孔、凹槽），字段同上
        { "primitive": "cylinder|cube", "size": [x,y,z], "center": [x,y,z] }
      ],
      "note": "string, 部件说明（可选）"
    }
  ],
  "assembly": {
    "description": "string, 装配关系说明",
    "union_parts": ["id", ...],              // 需要 union 的部件 id
    "difference_from": "string|id, 基准部件 id（可选, 表示 parts 中其余为挖除项）"
  },
  "print_tips": ["string", ...]               // 3D 打印建议（可选）
}
"""


def spec_to_json(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2)
