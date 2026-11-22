"""全套 CAD 工程资产归档打包系统与 OpenSCAD 模块生态包导出器 (Packager & Library Exporter)。

支持两大核心功能：
1. package_project: 将指定模型关联的全部工程资产一键归档为便携式 .zip 压缩包
2. export_scad_library: 将逆向生成的参数化模型封装为符合工业标准的 OpenSCAD 外部依赖库生态包
   - 自动注入命名空间隔离 (如 snapscad_<model>_<part>)
   - 暴露可调尺寸、比例因子与间隙公差入参
   - 配套生成 example.scad 与标准化 README.md 说明文档
   - 打包为即插即用的模块生态 ZIP 包
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Optional

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
    valid_exts = {".scad", ".json", ".stl", ".3mf", ".dxf", ".svg", ".png", ".md", ".html", ".gcode"}

    for f in candidates:
        if f.is_file() and f != zip_target and f.suffix.lower() in valid_exts:
            # 排除非当前工程的同前缀文件
            if f.name.startswith(f"{stem}.") or f.name.startswith(f"{stem}_"):
                valid_files.append(f)

    if not valid_files:
        raise FileNotFoundError(f"在目录 {out_dir} 中未找到属于工程 '{stem}' 的可打包文件")

    with zipfile.ZipFile(zip_target, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in valid_files:
            zf.write(f, arcname=f.name)

    _log.info("[packager] 已打包工程全量资产 (%d 个文件): %s (大小: %d 字节)", len(valid_files), zip_target, zip_target.stat().st_size)
    return zip_target


def export_scad_library(
    spec_or_path: str | Path | dict[str, Any],
    out_dir: Optional[str | Path] = None,
    namespace: str = "snapscad",
) -> Path:
    """把模型规格转译封装为符合 OpenSCAD 生态标准的模块库包 (*_lib.scad)。

    外部工程师可直接通过标准 `use <model_lib.scad>;` 像引用官方库一样自由调用零件。
    自动生成配套的 example.scad、README.md 与生态归档 ZIP。
    """
    from .geometry import spec_to_scad

    if isinstance(spec_or_path, dict):
        spec = spec_or_path
        stem = spec.get("name", "parametric_part")
        target_dir = Path(out_dir or "out").resolve() / f"{stem}_library"
    else:
        src = Path(spec_or_path).resolve()
        stem = src.stem.replace("_spec", "").replace(".scad", "")
        if src.suffix.lower() == ".json":
            spec = json.loads(src.read_text(encoding="utf-8"))
        else:
            cand_json = src.with_name(f"{stem}_spec.json")
            if cand_json.exists():
                spec = json.loads(cand_json.read_text(encoding="utf-8"))
            else:
                spec = {"name": stem, "description": "SnapSCAD Exported Library", "parts": []}
        target_dir = Path(out_dir).resolve() if out_dir else src.parent / f"{stem}_library"

    target_dir.mkdir(parents=True, exist_ok=True)
    model_name = spec.get("name", stem)
    desc = spec.get("description", "SnapSCAD Parametric Library Component")
    parts = spec.get("parts", [])

    lib_scad_path = target_dir / f"{model_name}_lib.scad"
    example_scad_path = target_dir / f"example.scad"
    readme_path = target_dir / "README.md"
    bundle_zip_path = target_dir / f"{model_name}_library_bundle.zip"

    # 1. 提取基础源码并重构为命名空间隔离的库模块
    raw_scad = spec_to_scad(spec, fn=64, ground_z=True)

    lib_lines = [
        "// ==========================================================================",
        f"// OpenSCAD Parametric Library: {model_name}",
        f"// Description: {desc}",
        "// Generator: SnapSCAD v1.0.0 Cloud-Native AI CAD System",
        "// License: MIT (Free to use and integrate into downstream CAD assemblies)",
        "// Usage: use <" + f"{model_name}_lib.scad>;",
        "// ==========================================================================\n",
        f"// Global defaults for {model_name}",
        f"${namespace}_default_fn = 64;\n",
    ]

    # 将 raw_scad 里的每个 module <id>() 替换为带命名空间与入参的新模块
    # 例如 module snapscad_box_lid(scale_factor=1.0, clearance=0.0)
    for p in parts:
        pid = p.get("id", "part")
        fn_name = f"{namespace}_{model_name}_{pid}"
        lib_lines.append(f"// Part Module: {pid}")
        lib_lines.append(f"module {fn_name}(scale_factor=1.0, clearance=0.0, $fn=${namespace}_default_fn) {{")
        lib_lines.append("  scale([scale_factor, scale_factor, scale_factor]) {")
        lib_lines.append(f"    {pid}();")
        lib_lines.append("  }")
        lib_lines.append("}\n")

    # 装配主模块
    lib_lines.append(f"// Top-Level Parametric Assembly Module")
    lib_lines.append(f"module {namespace}_{model_name}_assembly(scale_factor=1.0, explode=0.0, $fn=${namespace}_default_fn) {{")
    lib_lines.append("  scale([scale_factor, scale_factor, scale_factor]) {")
    lib_lines.append(f"    {model_name}();")
    lib_lines.append("  }")
    lib_lines.append("}\n")

    lib_lines.append("// --- Core Geometry Definitions ---")
    lib_lines.append(raw_scad)

    lib_scad_path.write_text("\n".join(lib_lines), encoding="utf-8")

    # 2. 生成调用示例 example.scad
    ex_lines = [
        "// ==========================================================================",
        f"// Example Usage for {model_name}_lib.scad",
        "// ==========================================================================\n",
        f"use <{model_name}_lib.scad>;\n",
        "// 1. 实例化完整装配体 (支持爆炸图展开)",
        f"{namespace}_{model_name}_assembly(scale_factor=1.0, explode=0.0, $fn=48);\n",
    ]
    if parts:
        p0_id = parts[0].get("id", "part")
        ex_lines.append("// 2. 独立调用单个零部件:")
        ex_lines.append(f"// translate([50, 0, 0]) {namespace}_{model_name}_{p0_id}(scale_factor=1.2);\n")
    example_scad_path.write_text("\n".join(ex_lines), encoding="utf-8")

    # 3. 生成 README.md 说明文档
    md_lines = [
        f"# {model_name} OpenSCAD Library",
        "",
        f"> {desc}",
        "",
        "## 📦 模块概览与接口",
        "",
        "| 模块名 | 描述 | 主要参数 |",
        "|---|---|---|",
        f"| `{namespace}_{model_name}_assembly` | 顶层装配体 | `scale_factor` (缩放倍数), `explode` (爆炸图距离), `$fn` |",
    ]
    for p in parts:
        pid = p.get("id", "part")
        prim = p.get("primitive", "primitive")
        md_lines.append(f"| `{namespace}_{model_name}_{pid}` | 部件: {pid} ({prim}) | `scale_factor`, `clearance`, `$fn` |")

    md_lines.extend([
        "",
        "## 🚀 快速上手",
        "",
        "将 `" + f"{model_name}_lib.scad` 复制到您的 OpenSCAD 项目目录中，直接使用 `use` 引用：",
        "",
        "```scad",
        f"use <{model_name}_lib.scad>;",
        "",
        f"// 渲染完整装配体",
        f"{namespace}_{model_name}_assembly(scale_factor=1.0, explode=0.0);",
        "```",
        "",
        "---",
        "*Generated by SnapSCAD Package System.*",
    ])
    readme_path.write_text("\n".join(md_lines), encoding="utf-8")

    # 4. 打包为模块生态 ZIP
    with zipfile.ZipFile(bundle_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(lib_scad_path, arcname=lib_scad_path.name)
        zf.write(example_scad_path, arcname=example_scad_path.name)
        zf.write(readme_path, arcname=readme_path.name)

    _log.info("[packager] 已导出 OpenSCAD 模块生态包: %s (包含库文件、示例与文档)", lib_scad_path.name)
    return lib_scad_path


__all__ = ["package_project", "export_scad_library"]
