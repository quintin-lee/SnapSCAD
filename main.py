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
from openscad_agent.gallery import generate_gallery


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OpenSCAD Agent: 图片/文字 -> OpenSCAD 3D 模型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = p.add_mutually_exclusive_group(required=False)
    src.add_argument("--image", "-i", nargs="+", help="输入图片路径 (支持传入多张多角度照片, 如 -i f.jpg s.jpg)")
    src.add_argument("--describe", "-d", help="文字描述, 无图片场景")
    src.add_argument("--batch-dir", help="批量生产目录: 自动对目录下的所有图片依次执行建模与汇总")

    p.add_argument("--gallery", action="store_true", help="根据输出目录中的全部模型生成 3D 展厅画廊网页 (gallery.html)")

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
    p.add_argument("--no-render", action="store_true", help="跳过 3D/预览渲染, 仅生成 .scad")
    p.add_argument("--preview-only", action="store_true", help="仅渲染 2D 预览, 跳过 3D 导出")
    p.add_argument("--max-image-side", type=int, default=2048, help="图片预处理最大边长")
    p.add_argument("--extra-system", default="", help="追加到系统提示词的额外指令")
    p.add_argument("--show-prompt", action="store_true", help="打印识别用系统提示词后退出")
    return p
from openscad_agent.log import setup_logging


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.show_prompt:
        print(SPEC_SCHEMA)
        return 0

    setup_logging(log_file="out/agent.log")

    if args.gallery and not args.image and not args.describe and not args.batch_dir:
        gpath = generate_gallery(args.output)
        print(f"✅ 3D 模型展厅画廊已生成: {gpath}")
        return 0

    if not args.image and not args.describe and not args.batch_dir:
        build_parser().print_help()
        return 1

    config = AgentConfig(
        output_dir=args.output,
        export_format="png" if args.preview_only else args.format,
        render_precision=args.precision,
        model=args.model,
        base_url=args.base_url,
        image_max_side=args.max_image_side,
        extra_system=args.extra_system,
        export_3mf=not args.no_3mf,
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

        print(f"🏭 启动工业化批量生产流水线: 共发现 {len(img_files)} 张待处理图片")
        print(f"   输入目录: {batch_p}")
        print(f"   输出目录: {config.output_dir}")
        print("=" * 60)

        success_count = 0
        fail_count = 0
        for idx, img_path in enumerate(img_files, 1):
            print(f"\n▶ [{idx}/{len(img_files)}] 正在处理: {img_path.name}")
            try:
                res = agent.build_from_image(str(img_path), hint=args.hint, review=not args.no_review)
                if res.success:
                    success_count += 1
                    print(f"   ✅ 成功: {res.spec.get('name') if res.spec else img_path.stem} -> {res.model_file}")
                else:
                    fail_count += 1
                    print(f"   ❌ 失败: {'; '.join(res.errors)}")
            except Exception as e:
                fail_count += 1
                print(f"   ❌ 异常: {e}")

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
        if result.preview_png:
            print(f"   预览图        : {result.preview_png}")
        if result.viewer_html:
            print(f"   3D 网页查看器 : {result.viewer_html}")
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


if __name__ == "__main__":
    sys.exit(main())
