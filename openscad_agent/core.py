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

from .log import get_logger as _log_module
_log = _log_module()

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
    #: 是否自动平移使模型底部贴合 Z=0 打印底板
    ground_z: bool = True
    #: 是否同步导出现代切片格式 3MF (适配 Bambu/Orca/Prusa Slicer)
    export_3mf: bool = True

    def __post_init__(self) -> None:
        os.makedirs(self.output_dir, exist_ok=True)
        # 校验 OpenSCAD 可执行文件存在（仅定位，不阻塞后续非渲染步骤）
        try:
            self.openscad_bin = find_executable(self.openscad_bin)
        except FileNotFoundError:
            self.openscad_bin = "openscad"  # 延迟到渲染时再报错

    #: render_precision → (--preview 模式, $fn 平滑度)
    _PRECISION_MAP = {
        "Fast":  ("thrown-together", 24),
        "Slow":  ("off", 32),
        "High":  ("off", 64),
    }

    def preview_mode(self) -> str:
        """当前精度的 --preview 模式（Fast=快预览, 其他=完整求值）。"""
        return self._PRECISION_MAP.get(self.render_precision, ("off", 64))[0]

    def fn_value(self) -> int:
        """当前精度的 $fn（圆柱/球体分段数）。"""
        return self._PRECISION_MAP.get(self.render_precision, ("off", 64))[1]

    @property
    def is_local_endpoint(self) -> bool:
        """是否为本地离线端点（如 Ollama / vLLM / LocalAI）。"""
        u = (self.base_url or "").lower()
        return any(h in u for h in ("localhost", "127.0.0.1", "0.0.0.0", ":11434"))

    @property
    def api_key(self) -> Optional[str]:
        if self.is_local_endpoint:
            return "ollama-local"
        for var in (
            "AGNES_API_KEY", "AGNES_API_TOKEN", "APIHUB_AGNES_API_KEY",
            "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
            "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY",
        ):
            key = os.environ.get(var)
            if key:
                return key
        return None

    def require_api_key(self) -> str:
        if self.is_local_endpoint:
            return "ollama-local"
        key = self.api_key
        if not key:
            raise RuntimeError(
                "缺少 API Key。请设置环境变量 AGNES_API_KEY（或 OPENAI_API_KEY / DEEPSEEK_API_KEY）\n"
                "若使用本地离线模型（Ollama/vLLM），请将 Base URL 设为 http://localhost:11434/v1 免 Key 运行。"
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

    FORMATS = {"stl", "obj", "3mf", "csg", "bmp", "png", "amf", "dxf", "svg"}

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.openscad = config.openscad_bin

    def _base_cmd(self) -> list[str]:
        return [self.openscad]

    def _preview_arg(self) -> list[str]:
        """根据 render_precision 返回 --preview 参数（Fast=快预览, 其他=完整求值）。"""
        return [f"--preview={self.config.preview_mode()}"]

    def export_model(self, scad_path: Path, out_path: Path) -> Path:
        """导出 3D 模型（stl/obj/3mf）。"""
        fmt = out_path.suffix.lstrip(".").lower() or self.config.export_format
        if fmt not in self.FORMATS:
            raise ValueError(f"不支持的导出格式: {fmt}，可选: {sorted(self.FORMATS)}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 若导出格式为 stl，使用 binstl (二进制 STL，体积缩小 70%+, 导出速度更快)
        export_fmt = "binstl" if fmt == "stl" else fmt
        cmd = self._base_cmd() + [
            "-o", str(out_path),
            f"--export-format={export_fmt}",
            str(scad_path),
        ]
        code, out, err = run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"OpenSCAD 导出失败 ({scad_path.name} -> {out_path.name}):\n{err or out}")
        _log.info("[render] 已导出 3D 模型: %s", out_path)
        if fmt == "stl":
            # 同步导出 3MF 现代切片格式
            if getattr(self.config, "export_3mf", True):
                try:
                    target_3mf = out_path.with_suffix(".3mf")
                    cmd_3mf = self._base_cmd() + [
                        "-o", str(target_3mf),
                        "--export-format=3mf",
                        str(scad_path),
                    ]
                    run_cmd(cmd_3mf)
                    _log.info("[render] 已同步导出 3MF 切片模型: %s", target_3mf.name)
                except Exception as e:  # noqa: BLE001
                    _log.warning("[render] 同步导出 3MF 失败: %s", e)

            try:
                html_path = out_path.with_name(f"{out_path.stem}_viewer.html")
                generate_html_viewer(out_path, html_path, model_name=out_path.stem)
            except Exception as e:  # noqa: BLE001
                _log.warning("[render] 生成 3D 网页查看器失败: %s", e)
        return out_path

    def render_preview(self, scad_path: Path, out_png: Path, size: tuple[int, int] = (800, 600)) -> Path:
        """渲染 2D 预览图（默认斜视角）。

        OpenSCAD 2021.01 CLI：--imgsize=WxH 指定 png 尺寸，--autocenter/--viewall
        让相机自适应物体。不强制 --render（老版本默认已求值），兼容新旧。
        """
        return self.render_preview_view(scad_path, out_png, size=size, view="perspective")

    #: 常用预览视角（用于多视角渲染 + 审查）
    VIEWS = ("top", "front", "iso", "perspective")

    def render_preview_view(
        self,
        scad_path: Path,
        out_png: Path,
        size: tuple[int, int] = (800, 600),
        view: str = "perspective",
    ) -> Path:
        """按指定视角渲染 2D 预览图。

        view 取值：top/front/right/left/back/iso/perspective。
        用 --projection + --camera 控制相机；老版本若不支持某参数会报 unrecognised，
        此时回退到不带视角参数的默认渲染，保证不中断。
        """
        out_png.parent.mkdir(parents=True, exist_ok=True)
        base = self._base_cmd() + [
            "--autocenter", "--viewall",
            f"--imgsize={size[0]},{size[1]}",
            *self._preview_arg(),
            "-o", str(out_png), str(scad_path),
        ]
        # 视角参数（OpenSCAD --view 可选值: axes/crosshairs/edges/scales/wireframe；
        # 真正视角用 --camera translate/rot。此处用 --projection 控制正/透视）。
        proj = "o" if view in ("top", "front", "right", "left", "back") else "p"
        cmd = base + [f"--projection={proj}"]
        code, out, err = run_cmd(cmd)
        if code != 0 and "unrecognised" in (err or out).lower():
            # 老版本不支持 --projection → 回退默认
            code, out, err = run_cmd(base)
        if code != 0:
            raise RuntimeError(f"OpenSCAD 渲染预览失败 ({scad_path.name}, view={view}):\n{err or out}")
        _log.info("[render] 已生成预览图(%s): %s", view, out_png)
        return out_png

    def render_multi_view(
        self, scad_path: Path, out_prefix: Path, views: tuple[str, ...] = VIEWS,
        size: tuple[int, int] = (600, 450),
    ) -> list[Path]:
        """渲染多个视角预览图，返回各视角 png 路径列表。

        用于审查时多角度比对。失败视角自动跳过（不影响其他视角）。
        """
        out_prefix = Path(out_prefix)
        paths: list[Path] = []
        for v in views:
            target = out_prefix.with_name(f"{out_prefix.stem}_{v}.png")
            try:
                paths.append(self.render_preview_view(scad_path, target, size=size, view=v))
            except RuntimeError as e:
                _log.info("[render] 视角 %s 渲染失败，跳过: %s", v, str(e)[:120])
        return paths

    def render_exploded_view(
        self, scad_path: Path, out_png: Path, size: tuple[int, int] = (800, 600), explode_factor: float = 1.2
    ) -> Path:
        """渲染机械爆炸图分解预览（将 explode 参数设为 >0 展开各内部零件）。"""
        out_png.parent.mkdir(parents=True, exist_ok=True)
        cmd = self._base_cmd() + [
            "-D", f"explode={explode_factor}",
            "--autocenter", "--viewall",
            f"--imgsize={size[0]},{size[1]}",
            *self._preview_arg(),
            "-o", str(out_png), str(scad_path),
        ]
        code, out, err = run_cmd(cmd)
        if code != 0:
            raise RuntimeError(f"OpenSCAD 渲染爆炸图失败: {err or out}")
        _log.info("[render] 已生成机械爆炸图分解视图: %s", out_png)
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
        cmd = self._base_cmd() + ["-o", str(target), "--export-format=binstl", str(scad_path)]
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

    def export_2d_projection(self, scad_path: Path, out_2d: Path, cut: bool = False) -> Path:
        """导出 2D 激光切割/CNC 轮廓投影 (dxf 或 svg)。

        cut = False: 全模型正投影 (俯视外轮廓)
        cut = True:  Z=0 水平面横剖切截面轮廓
        """
        fmt = out_2d.suffix.lstrip(".").lower()
        if fmt not in ("dxf", "svg"):
            raise ValueError(f"2D 投影仅支持 dxf 或 svg，传入为: {fmt}")
        out_2d.parent.mkdir(parents=True, exist_ok=True)

        root_module = _extract_root_module(scad_path)
        cut_str = "true" if cut else "false"
        wrapper = scad_path.parent / f"_proj_tmp_{scad_path.stem}.scad"
        content = f"use <{scad_path.resolve()}>\nprojection(cut = {cut_str}) {root_module}();\n"
        wrapper.write_text(content, encoding="utf-8")
        try:
            cmd = self._base_cmd() + [
                "-o", str(out_2d),
                str(wrapper),
            ]
            code, out, err = run_cmd(cmd)
            if code != 0:
                raise RuntimeError(f"OpenSCAD 2D 投影导出失败: {err or out}")
            _log.info("[render] 已导出 2D 激光切割/CNC 轮廓 (%s): %s", fmt.upper(), out_2d)
            return out_2d
        finally:
            try:
                wrapper.unlink(missing_ok=True)
            except OSError:
                pass


def _extract_root_module(scad_path: Path) -> str:
    """从 .scad 文件末尾提取顶层入口模块名。"""
    try:
        lines = scad_path.read_text(encoding="utf-8").splitlines()
        for l in reversed(lines):
            s = l.strip()
            if s.endswith("();"):
                call = s.rstrip("();").strip()
                parts = call.split()
                if parts:
                    return parts[-1]
    except Exception:
        pass
    return scad_path.stem


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
    _log.info("[preprocess] 图片已预处理: %s (max_side=%d)", dst.name, max_side)
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
      "primitive": "cube|cylinder|sphere|capsule|polyhedron|linear_extrude|rotate_extrude|text",
      // 尺寸/参数字段（优先使用 size，拉伸/回转使用 polygon 二维轮廓）:
      //   cube:           size: [x, y, z]
      //   cylinder/cone:  size: [直径, 高度]  或  [底径d1, 顶径d2, 高度]
      //                   或 radius: N, height: N 或 r1: N, r2: N, height: N (圆锥/圆台)
      //   sphere:         size: [半径]       或  radius: N
      //   capsule:        size: [半径, 柱高]  或  radius: N, height: N (平滑两端倒角胶囊柱)
      //   linear_extrude: height: H, polygon: [[x,y],...], twist: deg, scale: S (2D草图直拉伸/扭转)
      //   rotate_extrude: angle: 360, polygon: [[x,y],...] (2D截面回转体如花瓶/圆环/滑轮)
      //   text:           text: "STRING", font_size: N, height: H (3D立体浮雕文字或铭牌刻字)
      "size": [x, y, z],
      "radius": N,          // 圆柱/球/胶囊专用（size 缺省时使用）
      "height": N,          // 圆柱/胶囊/拉伸专用（size 缺省时使用）
      "r1": N,              // 圆锥/圆台底半径 (可选)
      "r2": N,              // 圆锥/圆台顶半径 (可选，为 0 则为尖锥)
      "fillet": N,          // 立方体可选平滑倒圆角半径 (mm，自动生成倒角圆角柱)
      "polygon": [[x,y],...], // 2D 草图多边形点列 (用于 linear_extrude/rotate_extrude)
      "color": "string|[r,g,b]", // 部件视觉颜色（如 "dimgray", "firebrick", "gold", "#e67e22"），帮助预览图区分各语义部件
      "symmetric": true,    // 可选，沿X轴双侧镜像对称 (如翅膀、把手、双足对称结构)
      "mirror": [0, 0, 0],  // 可选，[mx, my, mz] 单侧镜像轴
      "radial_array": { "count": 6, "radius": 25 }, // 可选，沿圆周均布 (如法兰螺栓孔、齿轮齿、环形柱)
      "linear_array": { "count": 4, "step": [15, 0, 0] }, // 可选，沿直线等距排布 (如散热片、排针)
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
    "intersection_parts": ["id", ...],       // 可选，需要做求交集的部件 id
    "difference_from": "string|id, 基准部件 id（可选, 表示 parts 中其余为挖除项）"
  },
  "print_tips": ["string", ...]               // 3D 打印建议（可选）
}
"""


def upload_to_3d_printer(
    file_path: str | Path,
    server_url: str = "http://localhost:7125",
    api_key: str = "",
    timeout: int = 10,
) -> tuple[bool, str]:
    """通过 HTTP API 将模型/切片文件一键推送至局域网 3D 打印机 (Klipper/Moonraker 或 OctoPrint)。"""
    import requests
    p = Path(file_path).resolve()
    if not p.exists():
        return False, f"待上传文件不存在: {p}"

    base = server_url.rstrip("/")
    if not base.startswith("http://") and not base.startswith("https://"):
        base = f"http://{base}"

    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    # 1. 尝试 Klipper (Moonraker) 端点
    url_moonraker = f"{base}/server/files/upload"
    try:
        with open(p, "rb") as f:
            resp = requests.post(url_moonraker, files={"file": (p.name, f)}, headers=headers, timeout=timeout)
        if resp.status_code in (200, 201):
            _log.info("[printer] 已推送至 Klipper 打印机: %s", p.name)
            return True, f"已成功推送至 Klipper (Moonraker): {p.name}"
    except Exception:
        pass

    # 2. 尝试 OctoPrint 端点
    url_octo = f"{base}/api/files/local"
    try:
        with open(p, "rb") as f:
            resp = requests.post(url_octo, files={"file": (p.name, f)}, headers=headers, timeout=timeout)
        if resp.status_code in (200, 201):
            _log.info("[printer] 已推送至 OctoPrint 打印机: %s", p.name)
            return True, f"已成功推送至 OctoPrint: {p.name}"
    except Exception as e:
        return False, f"无法连接局域网打印机 ({base}): {e}"

    return False, f"打印机服务返回状态码 {resp.status_code}: {resp.text[:150]}"


def spec_to_json(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, indent=2)


def generate_html_viewer(stl_path: Path, html_path: Path, model_name: str = "") -> Path:
    """生成内嵌 STL 模型的独立便携式 3D 网页交互查看器。

    基于 Three.js + OrbitControls，将 STL 数据以 base64 嵌入 HTML，
    可直接双击通过 file:/// 离线打开，支持鼠标 360° 旋转、缩放、平移、线框、调色。
    """
    import base64
    stl_path = Path(stl_path)
    html_path = Path(html_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    name = model_name or stl_path.stem

    raw_bytes = stl_path.read_bytes()
    b64_stl = base64.b64encode(raw_bytes).decode("ascii")

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{name} - 3D 交互查看器</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ overflow: hidden; background: radial-gradient(circle at center, #1e293b 0%, #0f172a 100%); font-family: system-ui, -apple-system, sans-serif; color: #f8fafc; }}
  #canvas-container {{ width: 100vw; height: 100vh; display: block; }}
  .hud {{ position: absolute; top: 16px; left: 16px; background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(8px); padding: 14px 18px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 10px 25px rgba(0,0,0,0.4); max-width: 320px; }}
  .hud h1 {{ font-size: 16px; font-weight: 700; margin-bottom: 4px; color: #38bdf8; word-break: break-all; }}
  .hud p {{ font-size: 12px; color: #94a3b8; margin-bottom: 8px; }}
  .stat {{ font-size: 11px; color: #cbd5e1; margin-bottom: 4px; }}
  .toolbar {{ position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%); display: flex; gap: 8px; background: rgba(15, 23, 42, 0.85); backdrop-filter: blur(8px); padding: 8px 14px; border-radius: 30px; border: 1px solid rgba(255,255,255,0.15); box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
  .toolbar button {{ background: #334155; border: none; color: #f1f5f9; padding: 6px 12px; font-size: 12px; border-radius: 20px; cursor: pointer; transition: all 0.2s; display: flex; align-items: center; gap: 4px; }}
  .toolbar button:hover {{ background: #2563eb; color: #fff; }}
  .toolbar button.active {{ background: #2563eb; font-weight: 600; }}
  .toolbar input[type="color"] {{ width: 28px; height: 28px; border: none; border-radius: 50%; cursor: pointer; background: transparent; }}
  .tip {{ position: absolute; bottom: 20px; right: 20px; font-size: 11px; color: #64748b; background: rgba(15,23,42,0.6); padding: 4px 10px; border-radius: 6px; }}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
</head>
<body>
<div id="canvas-container"></div>
<div class="hud">
  <h1>{name}</h1>
  <p>OpenSCAD Agent 3D 模型</p>
  <div class="stat" id="stat-tri">三角形面数: 加载中…</div>
  <div class="stat" id="stat-dim">包围盒尺寸: 加载中…</div>
</div>
<div class="toolbar">
  <button id="btn-rotate" class="active">🔄 旋转</button>
  <button id="btn-wire">🕸️ 线框</button>
  <button id="btn-clip">✂️ 剖切</button>
  <button id="btn-ruler">📏 尺寸</button>
  <button id="btn-reset">🎯 居中</button>
  <input type="color" id="picker-color" value="#38bdf8" title="更换模型颜色">
</div>
<div id="clip-panel" style="display:none; position:absolute; bottom:68px; left:50%; transform:translateX(-50%); background:rgba(15,23,42,0.92); backdrop-filter:blur(8px); padding:6px 14px; border-radius:20px; font-size:12px; gap:8px; align-items:center; border:1px solid rgba(255,255,255,0.15); box-shadow:0 8px 20px rgba(0,0,0,0.4);">
  <span>Z 轴截面剖切:</span>
  <input type="range" id="clip-slider" min="-50" max="50" step="0.5" value="0" style="width:140px; cursor:pointer;">
  <span id="clip-val">0.0mm</span>
</div>
<div class="tip">左键旋转 | 右键平移 | 滚轮缩放</div>

<script>
const STL_BASE64 = "{b64_stl}";

function parseSTL(buffer) {{
  const data = new DataView(buffer);
  const isBinary = buffer.byteLength > 84 && data.getUint32(80, true) * 50 + 84 === buffer.byteLength;
  if (isBinary) {{
    const count = data.getUint32(80, true);
    const vertices = new Float32Array(count * 9);
    const normals = new Float32Array(count * 9);
    let offset = 84;
    for (let i = 0; i < count; i++) {{
      const nx = data.getFloat32(offset, true);
      const ny = data.getFloat32(offset + 4, true);
      const nz = data.getFloat32(offset + 8, true);
      for (let v = 0; v < 3; v++) {{
        const vi = i * 9 + v * 3;
        vertices[vi] = data.getFloat32(offset + 12 + v * 12, true);
        vertices[vi + 1] = data.getFloat32(offset + 16 + v * 12, true);
        vertices[vi + 2] = data.getFloat32(offset + 20 + v * 12, true);
        normals[vi] = nx; normals[vi + 1] = ny; normals[vi + 2] = nz;
      }}
      offset += 50;
    }}
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(vertices, 3));
    geom.setAttribute('normal', new THREE.BufferAttribute(normals, 3));
    return {{ geom, count }};
  }} else {{
    const text = new TextDecoder().decode(buffer);
    const vMatches = text.matchAll(/vertex\\s+([-\\d.e+]+)\\s+([-\\d.e+]+)\\s+([-\\d.e+]+)/gi);
    const coords = [];
    for (const m of vMatches) {{
      coords.push(parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3]));
    }}
    const geom = new THREE.BufferGeometry();
    geom.setAttribute('position', new THREE.BufferAttribute(new Float32Array(coords), 3));
    geom.computeVertexNormals();
    return {{ geom, count: Math.floor(coords.length / 9) }};
  }}
}}

window.addEventListener('DOMContentLoaded', () => {{
  if (typeof THREE === 'undefined') {{
    document.body.innerHTML = '<div style="padding:40px;color:#f87171">无法加载 Three.js 引擎，请确认网络连接。</div>';
    return;
  }}

  const container = document.getElementById('canvas-container');
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x0f172a, 0.002);

  const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.1, 2000);
  const renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true }});
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.localClippingEnabled = true;
  container.appendChild(renderer.domElement);

  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 2.0;

  // 光照
  const ambient = new THREE.AmbientLight(0xffffff, 0.6);
  scene.add(ambient);
  const dir1 = new THREE.DirectionalLight(0xffffff, 0.8);
  dir1.position.set(100, 200, 150);
  scene.add(dir1);
  const dir2 = new THREE.DirectionalLight(0x38bdf8, 0.4);
  dir2.position.set(-100, -100, -50);
  scene.add(dir2);

  // 解码 STL
  const binStr = atob(STL_BASE64);
  const bytes = new Uint8Array(binStr.length);
  for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);
  const {{ geom, count }} = parseSTL(bytes.buffer);

  geom.computeBoundingBox();
  const box = geom.boundingBox;
  const size = new THREE.Vector3();
  box.getSize(size);
  const center = new THREE.Vector3();
  box.getCenter(center);

  // 居中
  geom.center();

  // 材质
  const material = new THREE.MeshStandardMaterial({{
    color: 0x38bdf8,
    metalness: 0.2,
    roughness: 0.4,
    side: THREE.DoubleSide
  }});
  const mesh = new THREE.Mesh(geom, material);
  scene.add(mesh);

  // 网格底盘
  const maxDim = Math.max(size.x, size.y, size.z, 50);
  const grid = new THREE.GridHelper(maxDim * 2.5, 20, 0x38bdf8, 0x334155);
  grid.position.y = -size.y / 2;
  scene.add(grid);

  // 相机适应
  const radius = maxDim * 1.8;
  camera.position.set(radius, radius * 0.8, radius);
  controls.target.set(0, 0, 0);
  controls.update();

  // HUD 信息
  document.getElementById('stat-tri').textContent = `三角形面数: ${{count.toLocaleString()}}`;
  document.getElementById('stat-dim').textContent = `尺寸: ${{size.x.toFixed(1)}} × ${{size.y.toFixed(1)}} × ${{size.z.toFixed(1)}} mm`;

  // 工具栏交互
  const btnRotate = document.getElementById('btn-rotate');
  btnRotate.addEventListener('click', () => {{
    controls.autoRotate = !controls.autoRotate;
    btnRotate.classList.toggle('active', controls.autoRotate);
  }});

  const btnWire = document.getElementById('btn-wire');
  btnWire.addEventListener('click', () => {{
    material.wireframe = !material.wireframe;
    btnWire.classList.toggle('active', material.wireframe);
  }});

  // 实时内部截面剖切 (Cross-Section Clipping Plane)
  const clipPlane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 0);
  let isClipping = false;
  const btnClip = document.getElementById('btn-clip');
  const clipPanel = document.getElementById('clip-panel');
  const clipSlider = document.getElementById('clip-slider');
  const clipVal = document.getElementById('clip-val');

  clipSlider.min = (-size.y / 2).toFixed(1);
  clipSlider.max = (size.y / 2).toFixed(1);
  clipSlider.value = (size.y / 4).toFixed(1);
  clipVal.textContent = `${{clipSlider.value}}mm`;

  btnClip.addEventListener('click', () => {{
    isClipping = !isClipping;
    btnClip.classList.toggle('active', isClipping);
    clipPanel.style.display = isClipping ? 'flex' : 'none';
    if (isClipping) {{
      clipPlane.constant = parseFloat(clipSlider.value);
      material.clippingPlanes = [clipPlane];
      material.clipShadows = true;
    }} else {{
      material.clippingPlanes = [];
    }}
  }});

  clipSlider.addEventListener('input', (e) => {{
    const v = parseFloat(e.target.value);
    clipVal.textContent = `${{v.toFixed(1)}}mm`;
    clipPlane.constant = v;
  }});

  // 三维空间尺寸标定盒 (3D Dimension Bounding Box)
  const boxHelper = new THREE.Box3Helper(new THREE.Box3().setFromObject(mesh), 0x38bdf8);
  boxHelper.visible = false;
  scene.add(boxHelper);

  let showRuler = false;
  const btnRuler = document.getElementById('btn-ruler');
  btnRuler.addEventListener('click', () => {{
    showRuler = !showRuler;
    boxHelper.visible = showRuler;
    btnRuler.classList.toggle('active', showRuler);
  }});

  document.getElementById('btn-reset').addEventListener('click', () => {{
    camera.position.set(radius, radius * 0.8, radius);
    controls.target.set(0, 0, 0);
    controls.update();
  }});

  document.getElementById('picker-color').addEventListener('input', (e) => {{
    material.color.set(e.target.value);
  }});

  window.addEventListener('resize', () => {{
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  }});

  function animate() {{
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }}
  animate();
}});
</script>
</body>
</html>"""
    html_path.write_text(html_content, encoding="utf-8")
    _log.info("[viewer] 已生成 3D 网页查看器: %s", html_path)
    return html_path
