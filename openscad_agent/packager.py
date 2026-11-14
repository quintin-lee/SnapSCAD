"""全套 CAD 工程资产归档打包系统 (All-in-One Project ZIP Packager)。

将指定模型关联的全部工程资产一键归档为一个可离线共享的 .zip 压缩包：
- 3D 打印模型: .stl (binstl 紧凑二进制), .3mf (现代切片格式)
- 2D 激光与 CNC 图纸: .dxf, .svg (若已生成)
- CAD 源码与参数: .scad, _spec.json
- 便携交互查看器: _viewer.html (离线 Three.js 交互网页)
- 3D 切片与制造指南: _print_guide.md (材料、耗时、推荐参数)
- 视觉参考封面: _preview.png 及多视角图
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Optional

from .log import get_logger as _log_module

_log = _log_module()


def package_project(scad_or_model_path: str | Path, zip_output: Optional[str | Path] = None) -> Path:
    """把指定模型关联的全部生成资产归档打包为单个 .zip 压缩包。"""
    src_path = Path(scad_or_model_path).resolve()
    out_dir = src_path.parent
    stem = src_path.stem.replace("_spec", "").replace("_viewer", "").replace("_preview", "").replace("_print_guide", "")

    if zip_output:
        zip_target = Path(zip_output).resolve()
    else:
        zip_target = out_dir / f"{stem}_project.zip"
    zip_target.parent.mkdir(parents=True, exist_ok=True)

    # 匹配该模型的前缀文件
    candidates = sorted(out_dir.glob(f"{stem}*"))
    valid_files: list[Path] = []
    valid_exts = {".scad", ".json", ".stl", ".3mf", ".dxf", ".svg", ".png", ".md", ".html"}

    for f in candidates:
        if f.is_file() and f != zip_target and f.suffix.lower() in valid_exts:
            # 排除非当前工程的同前缀文件 (如 stem_v_top.png 是本工程, 但 stem2 不是)
            if f.name.startswith(f"{stem}.") or f.name.startswith(f"{stem}_"):
                valid_files.append(f)

    if not valid_files:
        raise FileNotFoundError(f"在目录 {out_dir} 中未找到属于工程 '{stem}' 的可打包文件")

    with zipfile.ZipFile(zip_target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in valid_files:
            zf.write(f, arcname=f.name)

    _log.info("[packager] 已打包工程全量资产 (%d 个文件): %s (大小: %d 字节)", len(valid_files), zip_target, zip_target.stat().st_size)
    return zip_target


__all__ = ["package_project"]
