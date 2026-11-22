#!/usr/bin/env python3
"""OpenSCAD Agent CLI：从图片或文字描述生成 OpenSCAD 3D 模型。

用法示例：
  python main.py --image photo.jpg --hint "这是一个齿轮箱外壳"
  python main.py --image photo.jpg --format obj
  python main.py --describe "一个 M3 螺栓, 8mm 长, 圆柱头"
  python main.py --image photo.jpg --no-render      # 只生成 .scad 不渲染
  python main.py --image photo.jpg --preview-only    # 跳过 3D 导出, 仅 2D 预览
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openscad_agent import AgentConfig, OpenscadAgent, SPEC_SCHEMA
from openscad_agent.core import PROVIDER_PRESETS
from openscad_agent.gallery import generate_gallery
from openscad_agent.log import setup_logging
from openscad_agent.packager import package_project


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="SnapSCAD: Visual Reverse Engineering & AI CAD Agent (照片/草图/文字 -> 参数化 OpenSCAD 3D 模型)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--image", "-i", nargs="+", help="输入图片路径 (支持传入多张多角度照片, 如 -i f.jpg s.jpg)")
    src.add_argument("--describe", "-d", help="文字描述, 无图片场景")
    src.add_argument("--batch-dir", help="批量生产目录: 自动对目录下的所有图片依次执行建模与汇总")
    p.add_argument("--batch-workers", type=int, default=1, help="批量并发逆向流水线并行工作线程数 (默认 1 单线程, 可设 2~8)")
    p.add_argument("--slice", action="store_true", help="建模完成后自动调用切片器直出 3D 打印物理 G-code (*.gcode)")
    p.add_argument("--material", default="PLA", choices=["PLA", "PETG", "ABS", "TPU", "PC"],
                   help="3D 打印推荐耗材与切片温控模板 (默认 PLA)")
    p.add_argument("--layer-height", type=float, default=0.2, help="切片打印层高 (mm, 默认 0.2)")
    p.add_argument("--infill", type=int, default=15, help="切片内部填充率 (0~100%, 默认 15)")
    p.add_argument("--push-printer", help="建模/切片完成后一键推送到局域网 3D 打印机 (如 http://192.168.1.100:7125 或拓竹 IP)")

    p.add_argument("--gallery", action="store_true", help="根据输出目录中的全部模型生成 3D 展厅画廊网页 (gallery.html)")

    p.add_argument("--provider", choices=list(PROVIDER_PRESETS.keys()),
                   help="模型提供商预设 (agnes/openai/deepseek/gemini/ollama/siliconflow/dashscope/groq/openrouter)")
    p.add_argument("--max-tokens", type=int, default=4096, help="LLM 最大输出 token 限制 (默认 4096)")
    p.add_argument("--hint", default="", help="对图片的补充说明, 帮助 LLM 理解物体")
    p.add_argument("--output", "-o", default="out", help="输出目录 (默认 ./out)")
    p.add_argument("--format", "-f", default="stl",
                   choices=["stl", "obj", "3mf", "csg", "bmp", "png", "amf"],
                   help="3D 导出格式 (默认 stl)")
    p.add_argument("--precision", default="High", choices=["Fast", "Slow", "High"],
                   help="OpenSCAD 渲染精度 (默认 High)")
    p.add_argument("--model", default="agnes-3.0-flash", help="Agnes 模型名")
    p.add_argument("--base-url", default="https://apihub.agnes-ai.com",
                   help="OpenAI 兼容 API 端点 (可切换其他视觉 LLM)")
    p.add_argument("--no-review", action="store_true", help="跳过渲染图审查闭环 (默认开启)")
    p.add_argument("--no-multi-view", action="store_true", help="审查时不做多视角渲染 (默认开启)")
    p.add_argument("--no-3mf", action="store_true", help="跳过同步导出 3MF 格式 (默认同时导出 STL+3MF)")
    p.add_argument("--export-2d", action="store_true", help="同步导出 2D 激光切割投影 (DXF+SVG)")
    p.add_argument("--zip", action="store_true", help="建模完成后一键打包全套工程资产为 ZIP 压缩包 (*_project.zip)")
    p.add_argument("--no-render", action="store_true", help="跳过 3D/预览渲染, 仅生成 .scad")
    p.add_argument("--preview-only", action="store_true", help="仅渲染 2D 预览, 跳过 3D 导出")
    p.add_argument("--max-image-side", type=int, default=2048, help="图片预处理最大边长")
    p.add_argument("--extra-system", default="", help="追加到系统提示词的额外指令")
    p.add_argument("--show-prompt", action="store_true", help="打印识别用系统提示词后退出")
    p.add_argument("--test", action="store_true", help="执行全套自动化工程质量自检与回归测试套件")
    p.add_argument("--serve", nargs="?", const="8080", help="启动局域网 3D 资产共享 Web 服务 (可指定端口如 --serve 8080)")
    p.add_argument("--wizard", action="store_true", help="启动交互式终端步进建模向导")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.show_prompt:
        print(SPEC_SCHEMA)
        return 0

    if args.test:
        from test_agent import run_all_tests
        return 0 if run_all_tests() else 1

    if args.serve:
        from openscad_agent.server import serve_assets, is_port_in_use
        port = int(args.serve) if str(args.serve).isdigit() else 8080
        # 端口占用预检: 若指定端口已被占用, 提示用户
        if is_port_in_use(port):
            print(f"⚠️  端口 {port} 已被占用, 将自动寻找空闲端口")
        serve_assets(args.output, port=port, blocking=True)
        return 0

    if args.wizard:
        return run_cli_wizard()

    setup_logging(log_file="out/agent.log")

    if args.gallery and not args.image and not args.describe and not args.batch_dir:
        gpath = generate_gallery(args.output)
        print(f"✅ 3D 模型展厅画廊已生成: {gpath}")
        return 0

    if not args.image and not args.describe and not args.batch_dir:
        if sys.stdin.isatty():
            return run_cli_wizard()
        build_parser().print_help()
        return 1

    if args.provider and args.provider in PROVIDER_PRESETS:
        preset = PROVIDER_PRESETS[args.provider]
        if args.base_url == "https://apihub.agnes-ai.com":
            args.base_url = preset["base_url"]
        if args.model == "agnes-3.0-flash":
            args.model = preset["model"]

    config = AgentConfig(
        output_dir=args.output,
        export_format="png" if args.preview_only else args.format,
        render_precision=args.precision,
        model=args.model,
        base_url=args.base_url,
        max_tokens=args.max_tokens,
        image_max_side=args.max_image_side,
        extra_system=args.extra_system,
        export_3mf=not args.no_3mf,
        export_2d=args.export_2d,
        auto_slice=args.slice,
        material=args.material,
        slice_layer_height=args.layer_height,
        slice_infill=args.infill,
    )

    agent = OpenscadAgent(config)
    agent.enable_multi_view = not args.no_multi_view

    # 批量生产流水线
    if args.batch_dir:
        batch_p = Path(args.batch_dir).resolve()
        if not batch_p.exists() or not batch_p.is_dir():
            print(f"❌ 批量目录不存在或不是目录: {batch_p}")
            return 1
        img_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        img_files = sorted([f for f in batch_p.iterdir() if f.is_file() and f.suffix.lower() in img_exts])
        if not img_files:
            print(f"⚠️ 批量目录中未找到支持的图片文件: {batch_p}")
            return 1

        workers = max(1, min(args.batch_workers, 16))
        print(f"🏭 启动工业化批量生产流水线: 共发现 {len(img_files)} 张待处理图片 (并发工作线程={workers})")
        print(f"   输入目录: {batch_p}")
        print(f"   输出目录: {config.output_dir}")
        print("=" * 60)

        success_count = 0
        fail_count = 0

        def process_one(idx_file: tuple[int, Path]) -> tuple[bool, str]:
            idx, img_p = idx_file
            print(f"\n▶ [{idx}/{len(img_files)}] 正在处理: {img_p.name}")
            try:
                res = agent.build_from_image(str(img_p), hint=args.hint, review=not args.no_review)
                if res.success:
                    print(f"   ✅ 成功: {res.spec.get('name') if res.spec else img_p.stem} -> {res.model_file}")
                    return True, ""
                else:
                    err_msg = "; ".join(res.errors)
                    print(f"   ❌ 失败: {err_msg}")
                    return False, err_msg
            except Exception as e:
                print(f"   ❌ 异常: {e}")
                return False, str(e)

        if workers > 1 and len(img_files) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(process_one, enumerate(img_files, 1)))
            for ok, _ in results:
                if ok:
                    success_count += 1
                else:
                    fail_count += 1
        else:
            for idx_f in enumerate(img_files, 1):
                ok, _ = process_one(idx_f)
                if ok:
                    success_count += 1
                else:
                    fail_count += 1

        gpath = generate_gallery(args.output)
        print("\n" + "=" * 60)
        print(f"🏁 批量流水线处理完成: 成功 {success_count} 个 / 失败 {fail_count} 个 (共 {len(img_files)} 个)")
        print(f"🏛️ 汇总 3D 展厅画廊已生成: {gpath}")
        print("=" * 60)
        return 0 if success_count > 0 else 1

    if args.image:
        images: list[str] = []
        for item in args.image:
            for piece in str(item).split(","):
                piece = piece.strip()
                if piece:
                    images.append(piece)
        target_imgs = images if len(images) > 1 else images[0]
        result = agent.build_from_image(target_imgs, hint=args.hint, review=not args.no_review)
    else:
        result = agent.build_from_description(args.describe)

    # 汇报
    print("\n" + "=" * 60)
    if result.success:
        print(f"✅ 成功: 模型名 = {result.spec.get('name') if result.spec else '?'}")
        if result.scad_path:
            print(f"   OpenSCAD 源码 : {result.scad_path}")
        if result.spec_path:
            print(f"   规格 JSON     : {result.spec_path}")
        if result.model_file:
            print(f"   3D 模型 (STL) : {result.model_file}")
            f3mf = Path(result.model_file).with_suffix(".3mf")
            if f3mf.exists():
                print(f"   3D 模型 (3MF) : {f3mf}")
        if getattr(result, "gcode_path", None):
            print(f"   物理切片 G-code: {result.gcode_path}")
            s_res = getattr(result, "slicing_result", None)
            if s_res:
                print(f"   切片工艺指标  : 耗时 {s_res.estimated_time_formatted} | 耗材 {s_res.filament_weight_grams}g ({s_res.filament_length_meters}m) | 层数 {s_res.layer_count}")
        if result.preview_png:
            print(f"   预览图        : {result.preview_png}")
        if result.viewer_html:
            print(f"   3D 网页查看器 : {result.viewer_html}")
        if result.print_guide:
            print(f"   切片工艺指导  : {result.print_guide}")
        if args.zip and result.scad_path:
            try:
                zpath = package_project(result.scad_path)
                print(f"   工程打包 ZIP  : {zpath}")
            except Exception as e:
                print(f"   打包 ZIP 异常 : {e}")
        if args.push_printer and (result.gcode_path or result.model_file):
            target_f = result.gcode_path or result.model_file
            print(f"   📡 正在推送至打印机: {args.push_printer} ...")
            try:
                from openscad_agent.core import upload_to_3d_printer
                ok_push, push_msg = upload_to_3d_printer(target_f, server_url=args.push_printer)
                print(f"   {'✅' if ok_push else '❌'} 打印机推送: {push_msg}")
            except Exception as e:
                print(f"   ❌ 打印机推送异常: {e}")
        try:
            gpath = generate_gallery(args.output)
            print(f"   3D 展厅画廊   : {gpath}")
        except Exception:
            pass
        doc = getattr(result, "doctor_report", None)
        if doc:
            print(f"   适印性自检    : {doc.summary()} | 尺寸: {doc.bounding_box[0]}x{doc.bounding_box[1]}x{doc.bounding_box[2]}mm")
            for w in doc.warnings[:2]:
                print(f"     ⚠️  {w}")
        if result.fix_rounds:
            print(f"   自动修正轮数  : {result.fix_rounds}")
        if result.review_rounds:
            print(f"   审查闭环轮数  : {result.review_rounds}")
            if getattr(result, "score_trajectory", None):
                print(f"   得分演进走势  : {' -> '.join(str(s) for s in result.score_trajectory)}")
            rv = result.review or {}
            if rv:
                print(f"   审查结论      : match={rv.get('match')} score={rv.get('score')} | {rv.get('verdict')}")
                for iss in rv.get('issues', [])[:5]:
                    print(f"     - {iss}")
    else:
        print("❌ 失败:")
        for e in result.errors:
            print(f"   - {e}")
        if result.scad_path:
            print(f"   (已生成未通过校验的源码: {result.scad_path})")
    print("=" * 60)
    return 0 if result.success else 1


def run_cli_wizard() -> int:
    """启动交互式终端建模向导。"""
    print("\n" + "=" * 60)
    print("🧙 欢迎使用 SnapSCAD 终端智能建模向导!")
    print("=" * 60)

    print("\n[1/4] 请选择建模输入方式:")
    print("  1. 输入单张或多张本地照片路径 (支持多角度)")
    print("  2. 输入纯自然语言描述")
    print("  3. 批量图片目录处理")
    choice = input("请输入选项编号 [1/2/3, 默认 2]: ").strip() or "2"

    images = []
    desc = ""
    batch_dir = ""
    hint = ""

    if choice == "1":
        path_str = input("请输入图片文件路径 (多图用空格或逗号分隔): ").strip()
        for p in path_str.replace(",", " ").split():
            if p:
                images.append(p)
        hint = input("可选补充说明 (例如: 铝合金外壳，底部带沉孔): ").strip()
    elif choice == "3":
        batch_dir = input("请输入包含待处理图片的目录路径: ").strip()
    else:
        desc = input("请输入你想创建的 3D 模型自然语言描述:\n> ").strip()
        if not desc:
            desc = "一个标准的 M4 螺栓，长度 20mm，圆柱头"
            print(f"（使用默认描述: {desc}）")

    print("\n[2/4] 请选择模型/服务商配置:")
    print("  1. Agnes AI 云端 (agnes-3.0-flash, 默认)")
    print("  2. 本地离线 Ollama (无需 Key, http://localhost:11434/v1)")
    print("  3. OpenAI (gpt-4o)")
    print("  4. DeepSeek (deepseek-chat)")
    prov_choice = input("请输入选项编号 [1/2/3/4, 默认 1]: ").strip() or "1"

    if prov_choice == "2":
        base_url = "http://localhost:11434/v1"
        # 与 PROVIDER_PRESETS 一致用 llava（多模态）：向导含图片建模，纯文本模型必然失败
        model = "llava:latest"
    elif prov_choice == "3":
        base_url = "https://api.openai.com/v1"
        model = "gpt-4o"
    elif prov_choice == "4":
        base_url = "https://api.deepseek.com"
        model = "deepseek-chat"
    else:
        base_url = "https://apihub.agnes-ai.com"
        model = "agnes-3.0-flash"

    print("\n[3/4] 请选择输出精度与质量:")
    print("  1. High (高质量完整 CSG 求值, $fn=64, 默认)")
    print("  2. Slow (标准质量, $fn=32)")
    print("  3. Fast (快速粗糙预览, $fn=24)")
    prec_choice = input("请输入选项编号 [1/2/3, 默认 1]: ").strip() or "1"
    precision_map = {"1": "High", "2": "Slow", "3": "Fast"}
    precision = precision_map.get(prec_choice, "High")

    print("\n[4/4] 附加选项:")
    review = (input("是否启用多视角视觉审查闭环? [Y/n, 默认 Y]: ").strip().lower() != "n")
    pack_zip = (input("是否一键打包全套工程 ZIP 归档? [y/N, 默认 N]: ").strip().lower() == "y")
    export_2d = (input("是否同步导出 2D 激光切割投影 (DXF+SVG)? [y/N, 默认 N]: ").strip().lower() == "y")

    argv = ["--precision", precision, "--model", model, "--base-url", base_url]
    if not review:
        argv.append("--no-review")
    if pack_zip:
        argv.append("--zip")
    if export_2d:
        argv += ["--export-2d"]
    if hint:
        argv += ["--hint", hint]

    if batch_dir:
        argv += ["--batch-dir", batch_dir]
    elif images:
        argv += ["--image"] + images
    else:
        argv += ["--describe", desc]

    print("\n" + "=" * 60)
    print("🚀 参数就绪，正在启动建模流水线…")
    print("=" * 60)
    return main(argv)


if __name__ == "__main__":
    sys.exit(main())
