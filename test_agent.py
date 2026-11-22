"""OpenSCAD AI Agent 全自动工程质量自检与回归测试套件 (Test Runner)。

覆盖 8 大核心子系统：
1. 规格规范化与工业单位换算 (spec.py)
2. 几何转译与 OpenSCAD 真实编译 (geometry.py)
3. 多格式导出矩阵 (binstl, 3mf, dxf, svg, png) (core.py)
4. 3D 打印适印性自检与切片指导书 (doctor.py)
5. 独立 HTML 3D 网页交互查看器 (core.py)
6. 资产展厅画廊自动归集 (gallery.py)
7. 工程全量资产打包 (packager.py)
8. 本地离线算力网关与 GUI 组件冒烟 (gui.py, core.py)

运行方式:
  python test_agent.py
  python main.py --test
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# 确保加载当前工程
root_dir = Path(__file__).resolve().parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from openscad_agent import (
    AgentConfig, OpenscadRenderer, write_spec_and_scad,
    validate_spec, normalize_part, check_printability,
    generate_print_guide, generate_html_viewer,
    generate_gallery, package_project,
)


def run_test(name: str, fn):
    """测试用例执行包装器。"""
    t0 = time.perf_counter()
    try:
        fn()
        dt = (time.perf_counter() - t0) * 1000
        print(f"  ✅ [PASS] {name} ({dt:.1f}ms)")
        return True
    except Exception as e:
        dt = (time.perf_counter() - t0) * 1000
        print(f"  ❌ [FAIL] {name} ({dt:.1f}ms): {e}")
        return False


def test_ast_syntax():
    """测试 1: 全量 Python 代码语法解析"""
    for py_file in root_dir.glob("**/*.py"):
        if ".venv" in py_file.parts:
            continue
        ast.parse(py_file.read_text(encoding="utf-8"))


def test_spec_and_units():
    """测试 2: 规格规范化、单位换算与紧固件解析"""
    # cm 换算
    s_cm = {"name": "cm_test", "units": "cm", "parts": [{"id": "b", "primitive": "cube", "size": [5, 2, 1]}]}
    norm_cm, _ = validate_spec(s_cm)
    assert norm_cm["units"] == "mm"
    assert norm_cm["parts"][0]["size"] == [50.0, 20.0, 10.0]

    # inch 换算
    s_in = {"name": "in_test", "units": "inch", "parts": [{"id": "r", "primitive": "cylinder", "radius": 1, "height": 2}]}
    norm_in, _ = validate_spec(s_in)
    assert norm_in["parts"][0]["size"] == [50.8, 50.8]

    # M3 沉头孔
    part_cb = normalize_part({
        "primitive": "cube", "holes": [{"primitive": "counterbore", "standard": "M3", "height": 8}]
    })
    assert part_cb["holes"][0]["standard"] == "M3"


def test_geometry_and_compilation():
    """测试 3: 几何转译与真实 OpenSCAD 编译"""
    spec = {
        "name": "test_geom_all",
        "params": {"wall": 2.0},
        "parts": [
            # 倒角 cube
            {"id": "c1", "primitive": "cube", "size": [40, 30, 10], "fillet": 3.0, "center": [0,0,0], "color": "royalblue"},
            # 锥台 cylinder
            {"id": "t1", "primitive": "cylinder", "r1": 10, "r2": 2, "height": 20, "center": [0,0,10], "color": "gold"},
            # 胶囊体 capsule
            {"id": "cap", "primitive": "capsule", "radius": 3, "height": 18, "center": [25,0,0], "color": "orange"},
            # 3D 浮雕文字 text
            {"id": "txt", "primitive": "text", "text": "OK", "font_size": 8, "height": 2, "center": [0,0,30], "color": "white"}
        ],
        "assembly": {"union_parts": ["c1", "t1", "cap", "txt"]}
    }
    cfg = AgentConfig(output_dir="out_test", render_precision="High", ground_z=True)
    sp, sc, safe = write_spec_and_scad(spec, cfg)
    r = OpenscadRenderer(cfg)
    ok, msg = r.validate(Path(sc))
    assert ok, f"编译失败: {msg}"


def test_multi_formats_export():
    """测试 4: 多格式渲染矩阵 (binstl, 3mf, dxf, svg, preview)"""
    cfg = AgentConfig(output_dir="out_test", export_3mf=True)
    r = OpenscadRenderer(cfg)
    scad = Path("out_test/test_geom_all.scad")
    stl = Path("out_test/test_geom_all.stl")
    dxf = Path("out_test/test_geom_all.dxf")
    svg = Path("out_test/test_geom_all.svg")
    png = Path("out_test/test_geom_all_preview.png")

    r.export_model(scad, stl)
    r.export_2d_projection(scad, dxf)
    r.export_2d_projection(scad, svg)
    r.render_preview(scad, png)

    assert stl.exists() and not stl.read_bytes().startswith(b"solid"), "必须为二进制 STL"
    assert Path("out_test/test_geom_all.3mf").exists(), "3MF 文件应自动同步导出"
    assert dxf.exists() and dxf.stat().st_size > 0
    assert svg.exists() and svg.stat().st_size > 0
    assert png.exists() and png.stat().st_size > 0


def test_doctor_and_guide():
    """测试 5: 3D 打印体检系统与切片指导书"""
    spec = {
        "name": "test_doctor",
        "parts": [
            {"id": "p1", "primitive": "cube", "size": [60, 40, 15], "center": [0,0,0]},
            {"id": "pin", "primitive": "cylinder", "radius": 0.8, "height": 25, "center": [0,0,15]}
        ]
    }
    rep = check_printability(spec)
    assert rep.printable is True
    assert rep.volume_cm3 > 0.0
    assert rep.weight_g > 0.0
    guide_p = generate_print_guide(spec, rep, Path("out_test/test_doctor_print_guide.md"))
    assert guide_p.exists() and guide_p.stat().st_size > 500


def test_viewer_and_gallery():
    """测试 6: 便携式 HTML 3D 查看器与展厅画廊"""
    stl = Path("out_test/test_geom_all.stl")
    viewer_p = Path("out_test/test_geom_all_viewer.html")
    generate_html_viewer(stl, viewer_p, "TestGeomAll")
    assert viewer_p.exists()
    vt = viewer_p.read_text(encoding="utf-8")
    assert "btn-clip" in vt and "btn-ruler" in vt and "btn-axes" in vt and "btn-theme" in vt and "btn-full" in vt

    gallery_p = generate_gallery("out_test")
    assert gallery_p.exists()
    gt = gallery_p.read_text(encoding="utf-8")
    assert "展厅画廊" in gt and "stat-card" in gt


def test_packager():
    """测试 7: 全套资产工程打包器"""
    scad = Path("out_test/test_geom_all.scad")
    zip_p = package_project(scad)
    assert zip_p.exists() and zip_p.stat().st_size > 1000


def test_offline_gateway():
    """测试 8: 本地离线端点、多提供商预设与 API Key 网关"""
    cfg_loc = AgentConfig(base_url="http://localhost:11434/v1")
    assert cfg_loc.is_local_endpoint is True
    assert cfg_loc.api_key == "ollama-local"
    assert cfg_loc.require_api_key() == "ollama-local"
    assert cfg_loc.max_tokens == 4096

    # 扩展提供商预设与端点规范化测试
    from openscad_agent.core import PROVIDER_PRESETS
    from openscad_agent.vision import AgnesVision
    assert "gemini" in PROVIDER_PRESETS
    assert "groq" in PROVIDER_PRESETS
    assert "deepseek" in PROVIDER_PRESETS
    assert "siliconflow" in PROVIDER_PRESETS

    # 端点 URL 规范化测试 (自动消除 /v1 重复)
    v_gemini = AgnesVision(AgentConfig(base_url="https://generativelanguage.googleapis.com/v1beta/openai"))
    assert v_gemini._endpoint_url() == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    v_v1 = AgnesVision(AgentConfig(base_url="http://localhost:11434/v1"))
    assert v_v1._endpoint_url() == "http://localhost:11434/v1/chat/completions"

    v_full = AgnesVision(AgentConfig(base_url="https://custom.endpoint.com/v1/chat/completions"))
    assert v_full._endpoint_url() == "https://custom.endpoint.com/v1/chat/completions"

    # 扩展环境变量 API Key 读取 (如 GEMINI_API_KEY)
    old_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "test-gemini-key-123"
    try:
        cfg_gem = AgentConfig(base_url="https://generativelanguage.googleapis.com/v1beta/openai")
        assert cfg_gem.api_key is not None
    finally:
        if old_key is not None:
            os.environ["GEMINI_API_KEY"] = old_key
        else:
            os.environ.pop("GEMINI_API_KEY", None)


def test_blueprint_and_explode():
    """测试 9: 机械爆炸图与四视图工业蓝图拼版合成"""
    cfg = AgentConfig(output_dir="out_test")
    r = OpenscadRenderer(cfg)
    scad = Path("out_test/test_geom_all.scad")
    bp = Path("out_test/test_agent_bp.png")
    exp = Path("out_test/test_agent_exp.png")
    r.render_blueprint(scad, bp)
    r.render_exploded_view(scad, exp, explode_factor=1.2)
    assert bp.exists() and bp.stat().st_size > 0
    assert exp.exists() and exp.stat().st_size > 0


def test_lan_server_and_capture():
    """测试 10: 局域网 Web 资产共享服务与多端 IP 解析"""
    from openscad_agent.server import serve_assets, stop_server, get_local_lan_ip
    loc, lan, port = serve_assets("out_test", port=8996, open_browser=False, blocking=False)
    assert "8996" in loc
    assert get_local_lan_ip() is not None
    stop_server()  # 清理, 防止后续测试端口占用


def test_text_relief_and_trim():
    """测试 11: 3D 文字浮雕生成编译 + 审查会话修剪角色交替"""
    # 3D 文字浮雕: linear_extrude 包裹 2D text() 路径 (OpenSCAD 2021.01 兼容)
    spec = {"name": "text_fix", "parts": [{"id": "t", "primitive": "text", "text": "SNAPSCAD", "font_size": 10, "height": 2}]}
    sp, sc, safe = write_spec_and_scad(spec, AgentConfig(output_dir="out_test"))
    ok, msg = OpenscadRenderer(AgentConfig(output_dir="out_test")).validate(Path(sc))
    assert ok, f"3D 文字浮雕编译失败: {msg[:300]}"

    # 审查会话修剪: 模拟 8 轮审查后 conversation_history 角色严格交替
    hist: list[dict] = []
    for i in range(7):
        hist.append({"role": "user", "content": f"u{i}"})
        hist.append({"role": "assistant", "content": f"a{i}"})
    hist.append({"role": "user", "content": "u7"})
    if len(hist) > 6:
        recent = len(hist) - 2
        recent = recent - (recent % 2)
        recent = min(recent, 4)
        hist[:] = hist[:2] + hist[2 + (4 - recent):]
    for i in range(1, len(hist)):
        assert hist[i]["role"] != hist[i-1]["role"], f"审查修剪角色交替违规 @idx {i}: {[(h['role']) for h in hist]}"


def test_csg_assembly():
    """测试 12: CSG 树层级与布尔运算装配 (union, intersection, difference)"""
    spec = {
        "name": "test_csg_model",
        "parts": [
            {"id": "base_box", "primitive": "cube", "size": [40, 40, 20], "center": [0, 0, 0]},
            {"id": "cut_cyl", "primitive": "cylinder", "size": [15, 30], "center": [0, 0, 0]},
            {"id": "cut_sphere", "primitive": "sphere", "size": [12], "center": [0, 0, 10]},
            {"id": "lens_a", "primitive": "sphere", "size": [20], "center": [-5, 0, 0]},
            {"id": "lens_b", "primitive": "sphere", "size": [20], "center": [5, 0, 0]},
        ],
        "assembly": {
            "description": "Base box with intersection lens and subtracted holes",
            "union_parts": ["base_box"],
            "intersection_parts": ["lens_a", "lens_b"],
            "difference_parts": ["cut_cyl", "cut_sphere"],
        }
    }
    norm_spec, issues = validate_spec(spec)
    assert "lens_a" in norm_spec["assembly"]["intersection_parts"]
    assert "cut_cyl" in norm_spec["assembly"]["difference_parts"]
    assert "base_box" in norm_spec["assembly"]["union_parts"]

    cfg = AgentConfig(output_dir="out_test")
    sp, sc, safe = write_spec_and_scad(norm_spec, cfg)
    sc_text = Path(sc).read_text(encoding="utf-8")
    assert "intersection()" in sc_text
    assert "difference()" in sc_text
    assert "union()" in sc_text

    r = OpenscadRenderer(cfg)
    ok, msg = r.validate(Path(sc))
    assert ok, f"CSG 装配编译失败: {msg}"


def test_chamfer_features():
    """测试 13: 45度棱角平滑倒角 (chamfer) 规范化与 OpenSCAD 真实编译"""
    spec = {
        "name": "test_chamfer_model",
        "units": "cm",
        "parts": [
            {"id": "chamfer_box", "primitive": "cube", "size": [4, 4, 2], "chamfer": 0.3, "center": [0, 0, 0]},
            {"id": "chamfer_pin", "primitive": "cylinder", "radius": 0.8, "height": 3, "chamfer": 0.2, "center": [0, 0, 2]},
        ],
        "assembly": {"union_parts": ["chamfer_box", "chamfer_pin"]}
    }
    norm_spec, _ = validate_spec(spec)
    assert norm_spec["units"] == "mm"
    # 0.3cm -> 3mm chamfer
    assert norm_spec["parts"][0]["chamfer"] == 3.0
    assert norm_spec["parts"][1]["chamfer"] == 2.0

    cfg = AgentConfig(output_dir="out_test")
    sp, sc, safe = write_spec_and_scad(norm_spec, cfg)
    sc_text = Path(sc).read_text(encoding="utf-8")
    assert "chamfer = true" in sc_text
    assert "chamfered cylinder" in sc_text or "顶部倒角 chamfer" in sc_text

    r = OpenscadRenderer(cfg)
    ok, msg = r.validate(Path(sc))
    assert ok, f"倒角模型编译失败: {msg}"


def test_exact_rotation_aabb_grounding():
    """测试 14: 精确 3D Euler 旋转 AABB 计算与 Z=0 物理接地贴合"""
    from openscad_agent.doctor import get_part_aabb
    from openscad_agent.geometry import spec_to_scad

    # 1. 倾斜 90 度的立柱 (沿 Y 轴旋转 90 度): d=10, h=30, 原点 [0,0,0]
    cyl = {
        "id": "tilted_cyl",
        "primitive": "cylinder",
        "size": [10, 30],
        "rotation": [0, 90, 0],
        "center": [0, 0, 0]
    }
    aabb_cyl = get_part_aabb(cyl)
    assert abs(aabb_cyl[0][2] - (-5.0)) < 1e-3, f"最低点 Z 期望 -5.0, 实际 {aabb_cyl[0][2]}"
    assert abs(aabb_cyl[1][2] - 5.0) < 1e-3, f"最高点 Z 期望 5.0, 实际 {aabb_cyl[1][2]}"

    # 2. 绕 X 轴旋转 90 度的立方体: w=20, d=10, h=40, 原点 [0,0,0]
    box = {
        "id": "tilted_box",
        "primitive": "cube",
        "size": [20, 10, 40],
        "rotation": [90, 0, 0],
        "center": [0, 0, 0]
    }
    aabb_box = get_part_aabb(box)
    assert abs(aabb_box[0][2] - (-5.0)) < 1e-3, f"最低点 Z 期望 -5.0, 实际 {aabb_box[0][2]}"
    assert abs(aabb_box[1][2] - 5.0) < 1e-3, f"最高点 Z 期望 5.0, 实际 {aabb_box[1][2]}"

    # 3. 验证 spec_to_scad 自动接地贴合计算注入
    spec = {
        "name": "grounding_test",
        "parts": [cyl],
        "assembly": {"union_parts": ["tilted_cyl"]}
    }
    sc_text = spec_to_scad(spec, ground_z=True)
    assert "translate([0, 0, 5])" in sc_text


def test_material_profiles():
    """测试 15: 多耗材材质物性推导 (PLA/PETG/ABS/TPU/PC) 与针对性切片工艺指导"""
    from openscad_agent.doctor import check_printability, generate_print_guide, MATERIAL_PROFILES

    spec = {
        "name": "mat_test_model",
        "parts": [{"id": "body", "primitive": "cube", "size": [40, 40, 20], "center": [0, 0, 0]}],
        "assembly": {"union_parts": ["body"]}
    }

    # PLA 标准检查
    rep_pla = check_printability(spec, material="PLA")
    assert rep_pla.material == "PLA"
    assert rep_pla.shrinkage_pct == 0.3
    assert rep_pla.weight_g > 0

    # ABS 高温高收缩检查
    rep_abs = check_printability(spec, material="ABS")
    assert rep_abs.material == "ABS"
    assert rep_abs.shrinkage_pct == 1.5
    # ABS 密度 1.04 比 PLA 1.24 小，相同体积克重更轻
    assert rep_abs.weight_g < rep_pla.weight_g
    assert any("保温箱" in tip for tip in rep_abs.tips)

    # TPU 弹性软胶慢速打印检查
    rep_tpu = check_printability(spec, material="TPU")
    assert rep_tpu.material == "TPU"
    assert any("回抽" in tip for tip in rep_tpu.tips)
    # TPU 速度降低 50%，预估耗时更长
    assert rep_tpu.print_time_hours > rep_pla.print_time_hours

    # 导出指导书并验证包含材质特定参数
    guide_path = Path("out_test/mat_test_guide.md")
    generate_print_guide(spec, rep_abs, guide_path)
    guide_content = guide_path.read_text(encoding="utf-8")
    assert "ABS" in guide_content
    assert "保温箱" in guide_content
    assert "250°C" in guide_content


def test_gui_embedded_viewport():
    """测试 16: GUI 嵌入式 3D 交互视口与双模渲染切换"""
    gui_text = Path("gui.py").read_text(encoding="utf-8")
    tree = ast.parse(gui_text)
    assert tree is not None
    assert "on_switch_to_3d_viewport" in gui_text
    assert "preview_stack" in gui_text
    assert "HAS_WEBENGINE" in gui_text
    assert "btn_view_3d" in gui_text
    assert "_load_3d_viewer" in gui_text


def test_caching_and_parallel_rendering():
    """测试 17: 增量渲染缓存检测与多视角并发渲染加速"""
    from openscad_agent.core import AgentConfig, OpenscadRenderer

    cfg = AgentConfig()
    renderer = OpenscadRenderer(cfg)

    # 1. 验证 OpenSCAD 特性探测 (返回集合且不抛异常)
    feats = renderer._detect_features(renderer.openscad)
    assert isinstance(feats, set)

    # 2. 验证多视角并发渲染
    scad_code = "cube([20, 20, 20], center=true);"
    scad_path = Path("out_test/cache_test.scad")
    scad_path.parent.mkdir(parents=True, exist_ok=True)
    scad_path.write_text(scad_code, encoding="utf-8")

    views = ["top", "front", "side", "iso"]
    out_mv = Path("out_test/cache_test_mv")
    png_paths = renderer.render_multi_view(scad_path, out_mv, views=views, size=(200, 150))
    assert len(png_paths) == 4
    for p in png_paths:
        assert p.exists() and p.stat().st_size > 0

    # 3. 验证增量缓存命中 (mtime 比较)
    out_stl = Path("out_test/cache_test.stl")
    renderer.export_model(scad_path, out_stl, force=True)
    assert out_stl.exists()
    st_mtime_1 = out_stl.stat().st_mtime

    # 再次调用，无需修改 scad，应当命中缓存直接返回
    out_stl_cached = renderer.export_model(scad_path, out_stl, force=False)
    assert out_stl_cached == out_stl
    assert out_stl.stat().st_mtime == st_mtime_1

    # 验证 preview_view 增量缓存
    out_png = Path("out_test/cache_preview.png")
    renderer.render_preview_view(scad_path, out_png, view="top", force=True)
    png_mtime_1 = out_png.stat().st_mtime
    renderer.render_preview_view(scad_path, out_png, view="top", force=False)
    assert out_png.stat().st_mtime == png_mtime_1


def test_threads_and_gears():
    """测试 18: 工业级机械原语与复杂特征库 (公制螺纹与渐开线齿轮)"""
    from openscad_agent.spec import validate_spec
    from openscad_agent.geometry import spec_to_scad
    from openscad_agent.core import AgentConfig, OpenscadRenderer

    spec = {
        "name": "mech_primitives",
        "parts": [
            {
                "id": "bolt_m8",
                "primitive": "thread",
                "diameter": 8.0,
                "pitch": 1.25,
                "length": 12.0,
                "tolerance": -0.1,
                "internal": False,
                "center": [-15, 0, 0],
            },
            {
                "id": "nut_m8",
                "primitive": "thread",
                "diameter": 8.0,
                "pitch": 1.25,
                "length": 8.0,
                "tolerance": 0.2,
                "internal": True,
                "center": [0, 0, 0],
            },
            {
                "id": "pinion_gear",
                "primitive": "gear",
                "teeth": 18,
                "module": 1.25,
                "height": 6.0,
                "bore": 4.0,
                "pressure_angle": 20.0,
                "center": [25, 0, 0],
            },
        ],
        "assembly": {
            "union_parts": ["bolt_m8", "nut_m8", "pinion_gear"],
        },
    }

    norm, issues = validate_spec(spec)
    assert len(norm["parts"]) == 3
    scad_code = spec_to_scad(norm)
    assert "polyhedron" in scad_code
    assert "linear_extrude" in scad_code
    assert "polygon" in scad_code

    scad_path = Path("out_test/test_mech_primitives.scad")
    scad_path.write_text(scad_code, encoding="utf-8")

    cfg = AgentConfig()
    renderer = OpenscadRenderer(cfg)
    ok_val, msg_val = renderer.validate(scad_path)
    assert ok_val, f"OpenSCAD 编译失败: {msg_val}"

    out_stl = Path("out_test/test_mech_primitives.stl")
    res = renderer.export_model(scad_path, out_stl, force=True)
    assert res.exists() and res.stat().st_size > 0


def test_pulleys_snaps_magnets():
    """测试 19: GT2 同步轮与免胶装配结构 (悬臂梁卡扣与强磁沉孔)"""
    from openscad_agent.spec import validate_spec
    from openscad_agent.geometry import spec_to_scad
    from openscad_agent.core import AgentConfig, OpenscadRenderer

    spec = {
        "name": "assembly_primitives",
        "parts": [
            {
                "id": "gt2_20t",
                "primitive": "timing_pulley",
                "teeth": 20,
                "belt_width": 6.0,
                "bore": 5.0,
                "flange": True,
                "center": [-25, 0, 0],
            },
            {
                "id": "snap_latch",
                "primitive": "snap_joint",
                "length": 15.0,
                "width": 8.0,
                "thickness": 1.6,
                "hook_depth": 1.2,
                "center": [0, 0, 0],
            },
            {
                "id": "mounting_block",
                "primitive": "cube",
                "size": [20, 20, 10],
                "center": [25, 0, 0],
                "holes": [
                    {
                        "primitive": "magnet_pocket",
                        "diameter": 6.0,
                        "depth": 3.0,
                        "clearance": 0.2,
                        "ejection_hole": 1.5,
                        "center": [25, 0, 5],
                    }
                ],
            },
        ],
        "assembly": {
            "union_parts": ["gt2_20t", "snap_latch", "mounting_block"],
        },
    }

    norm, issues = validate_spec(spec)
    assert len(norm["parts"]) == 3
    scad_code = spec_to_scad(norm)
    assert "timing pulley" in scad_code or "cylinder" in scad_code
    assert "snap joint" in scad_code or "polygon" in scad_code
    assert "永磁体装配沉孔" in scad_code or "magnet" in scad_code

    scad_path = Path("out_test/test_assembly_primitives.scad")
    scad_path.write_text(scad_code, encoding="utf-8")

    cfg = AgentConfig()
    renderer = OpenscadRenderer(cfg)
    ok_val, msg_val = renderer.validate(scad_path)
    assert ok_val, f"OpenSCAD 编译失败: {msg_val}"

    out_stl = Path("out_test/test_assembly_primitives.stl")
    res = renderer.export_model(scad_path, out_stl, force=True)
    assert res.exists() and res.stat().st_size > 0


def test_metric_calibration_and_multiview():
    """测试 20: 物理基准参照物标尺校准与多视角空间推导"""
    from openscad_agent.spec import validate_spec, METRIC_CALIBRATION_REFS
    from openscad_agent.vision import build_vision_prompt

    # 1. 验证基准物数据库覆盖常见硬币、银行卡、A4纸
    assert "coin_1cny" in METRIC_CALIBRATION_REFS
    assert "credit_card" in METRIC_CALIBRATION_REFS
    assert METRIC_CALIBRATION_REFS["coin_1cny"]["d_mm"] == 25.0
    assert METRIC_CALIBRATION_REFS["credit_card"]["width_mm"] == 85.60

    # 2. 验证基于 1元硬币 (25mm) 的真实尺寸自动校准
    # 假设图像初步估算硬币基准为 50mm -> 缩放系数应为 25.0 / 50.0 = 0.5
    raw_spec = {
        "name": "calibrated_mount",
        "calibration": {
            "reference": "coin_1cny",
            "estimated_mm": 50.0,
        },
        "parts": [
            {
                "id": "mount_base",
                "primitive": "cube",
                "size": [100.0, 60.0, 20.0],
                "center": [0.0, 0.0, 0.0],
                "holes": [
                    {"primitive": "cylinder", "size": [10.0, 20.0], "center": [0.0, 0.0, 0.0]}
                ],
            }
        ],
    }
    norm, issues = validate_spec(raw_spec)
    assert any("校准系数" in s for s in issues)
    # 尺寸缩小 50%
    assert norm["parts"][0]["size"] == [50.0, 30.0, 10.0]
    assert norm["parts"][0]["holes"][0]["size"] == [5.0, 10.0]

    # 3. 验证提示词注入多视角空间推导与草图标注支持
    prompt = build_vision_prompt("机械支架草图 Φ20 R5 1元硬币参照")
    assert "多视角空间对齐" in prompt
    assert "coin_1cny" in prompt
    assert "Φ" in prompt


def test_slicer_engine():
    """测试 21: 自动化无头切片引擎、拓扑解析、G-code 生成与增量缓存"""
    from openscad_agent.slicer import (
        probe_slicers,
        slice_model,
        SlicingConfig,
        parse_gcode_metadata,
        parse_stl_geometry,
        MATERIAL_PRESETS,
    )
    from openscad_agent.core import AgentConfig, OpenscadRenderer

    # 1. 验证切片工具链探测
    probe = probe_slicers()
    assert "installed" in probe
    assert "primary_engine" in probe

    # 2. 导出测试二进制 STL
    with tempfile.TemporaryDirectory() as td:
        scad_path = Path(td) / "slice_test.scad"
        stl_path = Path(td) / "slice_test.stl"
        gcode_path = Path(td) / "slice_test.gcode"
        scad_path.write_text("cube([30, 20, 10]);\n", encoding="utf-8")

        cfg = AgentConfig(output_dir=td)
        renderer = OpenscadRenderer(cfg)
        renderer.export_model(scad_path, stl_path, force=True)
        assert stl_path.exists() and stl_path.stat().st_size > 0

        # 3. 验证 STL 拓扑与流形体积解析
        geom = parse_stl_geometry(stl_path)
        assert geom["facets"] == 12
        assert geom["size"] == (30.0, 20.0, 10.0)
        assert abs(geom["volume_mm3"] - 6000.0) < 50.0  # 30x20x10 = 6000 mm³

        # 4. 执行切片
        s_cfg = SlicingConfig(material="PETG", layer_height=0.2, infill_density=20)
        res = slice_model(stl_path, out_gcode=gcode_path, config=s_cfg)
        assert res.success is True
        assert res.gcode_path == str(gcode_path)
        assert gcode_path.exists()
        assert res.layer_count == 50  # 10mm / 0.2mm
        assert res.filament_weight_grams > 0.5
        assert res.estimated_time_minutes > 0.0

        # 5. 校验 G-code 文本合规性 (起始/层高/挤出/温控/结束码)
        g_text = gcode_path.read_text(encoding="utf-8")
        assert "G28 ; home all axes" in g_text
        assert "M140 S80" in g_text or "M190 S80" in g_text  # PETG bed temp 80
        assert "M104 S240" in g_text or "M109 S240" in g_text  # PETG nozzle temp 240
        assert ";LAYER:1" in g_text
        assert ";LAYER:50" in g_text
        assert "M84 ; disable stepper motors" in g_text

        # 6. 校验元数据反向解析
        meta = parse_gcode_metadata(gcode_path)
        assert meta["layer_count"] == 50
        assert meta["filament_weight_g"] > 0.5
        assert meta["estimated_time_minutes"] > 0.0

        # 7. 增量缓存测试
        cached_res = slice_model(stl_path, out_gcode=gcode_path, config=s_cfg, force=False)
        assert cached_res.slicer_engine == "cached"


def test_printer_dispatcher():
    """测试 22: 多协议 3D 打印机集群调度器 (Klipper/OctoPrint/拓竹 FTPS+MQTT)"""
    from openscad_agent.dispatcher import PrinterDispatcher, DispatchConfig, DispatchResult
    from openscad_agent.core import upload_to_3d_printer

    # 1. 验证主机与端口解析
    cfg = DispatchConfig(server_url="192.168.1.120:7125", printer_type="klipper")
    disp = PrinterDispatcher(cfg)
    http_base, host_ip, port = disp._normalize_host_and_url()
    assert http_base == "http://192.168.1.120:7125"
    assert host_ip == "192.168.1.120"
    assert port == 7125

    # 2. 验证拓竹 Bambu Lab MQTT 打印启始指令 JSON 载荷生成
    payload = disp.generate_bambu_mqtt_payload("gearbox.gcode", bed_type="textured_plate")
    assert "print" in payload
    assert payload["print"]["command"] == "project_file"
    assert payload["print"]["subtask_name"] == "gearbox.gcode"
    assert payload["print"]["bed_levelling"] is True

    # 3. 验证文件不存在时的优雅拦截与排错指引
    res = disp.upload_file("non_existent_file.gcode")
    assert res.success is False
    assert any("待推送文件不存在" in res.message for _ in [1])
    assert len(res.diagnostics) > 0

    # 4. 验证网络不通时的诊断信息返回 (短超时测试)
    with tempfile.NamedTemporaryFile(suffix=".gcode", delete=False) as tf:
        tf.write(b"; test gcode\nG28\n")
        tmp_gcode = tf.name
    try:
        cfg_offline = DispatchConfig(server_url="http://127.0.0.1:59999", timeout=1, printer_type="klipper")
        disp_offline = PrinterDispatcher(cfg_offline)
        res_offline = disp_offline.upload_file(tmp_gcode)
        assert res_offline.success is False
        assert len(res_offline.diagnostics) > 0

        # 5. 验证 core.upload_to_3d_printer 向后兼容接口
        ok, msg = upload_to_3d_printer(tmp_gcode, server_url="http://127.0.0.1:59999", timeout=1)
        assert ok is False
        assert "失败" in msg or "127.0.0.1" in msg
    finally:
        os.unlink(tmp_gcode)


def test_batch_pipeline_and_gallery_gcode():
    """测试 23: 工业级批量并发逆向生产流水线与展厅 G-code 元数据集成"""
    from openscad_agent.gallery import generate_gallery
    from openscad_agent.slicer import slice_model, SlicingConfig
    from openscad_agent.core import AgentConfig, OpenscadRenderer

    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        # 1. 模拟生成一个已切片的模型产物
        scad_f = out_dir / "turbine_blade.scad"
        scad_f.write_text("cylinder(d=20, h=15, $fn=32);\n", encoding="utf-8")
        stl_f = out_dir / "turbine_blade.stl"
        gcode_f = out_dir / "turbine_blade.gcode"
        spec_f = out_dir / "turbine_blade_spec.json"
        spec_f.write_text(json.dumps({
            "name": "turbine_blade",
            "description": "工业燃气轮机高压涡轮叶片",
            "parts": [{"id": "blade", "primitive": "cylinder", "size": [20, 15], "center": [0,0,0]}],
        }), encoding="utf-8")

        cfg = AgentConfig(output_dir=td)
        renderer = OpenscadRenderer(cfg)
        renderer.export_model(scad_f, stl_f, force=True)
        # 生成切片 G-code
        s_res = slice_model(stl_f, out_gcode=gcode_f, config=SlicingConfig(material="ABS", layer_height=0.2))
        assert gcode_f.exists()

        # 2. 渲染画廊
        g_html = generate_gallery(out_dir, "gallery.html")
        assert g_html.exists()
        html_txt = g_html.read_text(encoding="utf-8")
        # 检查画廊是否提取了 G-code 下载按钮与切片指标
        assert "⚡ G-code" in html_txt
        assert "turbine_blade.gcode" in html_txt
        assert "turbine_blade" in html_txt


def run_all_tests() -> bool:
    """运行全套自动化测试并输出报告。"""
    print("\n" + "=" * 60)
    print("🧪 OpenSCAD AI Agent 全自动化工程质量基准自检套件")
    print("=" * 60)

    suites = [
        ("全量 Python 代码 AST 语法解析", test_ast_syntax),
        ("规格规范化、多单位换算与紧固件解析", test_spec_and_units),
        ("几何转译与真实 OpenSCAD CGAL 编译", test_geometry_and_compilation),
        ("CSG 树层级与布尔装配 (交集/差集/并集)", test_csg_assembly),
        ("45度棱边倒角与立柱导入角 (chamfer)", test_chamfer_features),
        ("精确 3D Euler 旋转 AABB 与 Z=0 物理接地", test_exact_rotation_aabb_grounding),
        ("多格式渲染矩阵 (binstl/3mf/dxf/svg/png)", test_multi_formats_export),
        ("3D 打印体检系统与切片指导书生成", test_doctor_and_guide),
        ("多材质物性推导与切片工艺指导 (PLA/ABS/TPU)", test_material_profiles),
        ("独立 HTML 3D 查看器与全景展厅画廊", test_viewer_and_gallery),
        ("All-in-One 全套工程资产 ZIP 打包", test_packager),
        ("本地离线端点识别与免 Key 认证网关", test_offline_gateway),
        ("机械爆炸图与四视图工业蓝图拼版合成", test_blueprint_and_explode),
        ("局域网 Web 资产共享服务与多端 IP 解析", test_lan_server_and_capture),
        ("3D 文字浮雕生成编译与审查修剪角色交替", test_text_relief_and_trim),
        ("GUI 嵌入式 3D 交互视口与双模渲染切换", test_gui_embedded_viewport),
        ("增量渲染缓存检测与多视角并发渲染加速", test_caching_and_parallel_rendering),
        ("工业级机械原语与复杂特征库 (公制螺纹与渐开线齿轮)", test_threads_and_gears),
        ("GT2 同步轮与免胶装配结构 (悬臂梁卡扣与强磁沉孔)", test_pulleys_snaps_magnets),
        ("物理基准参照物标尺校准与多视角空间推导", test_metric_calibration_and_multiview),
        ("自动化切片引擎、拓扑解析、G-code 生成与增量缓存", test_slicer_engine),
        ("多协议 3D 打印机集群调度器 (Klipper/OctoPrint/拓竹)", test_printer_dispatcher),
        ("工业级批量并发逆向生产流水线与展厅 G-code 元数据集成", test_batch_pipeline_and_gallery_gcode),
    ]

    all_ok = True
    t_start = time.perf_counter()
    for name, fn in suites:
        ok = run_test(name, fn)
        if not ok:
            all_ok = False

    total_time = time.perf_counter() - t_start
    print("=" * 60)
    if all_ok:
        print(f"🎉 全部 {len(suites)} 项核心测试用例 100% 通过！(总耗时: {total_time:.2f}s)")
    else:
        print(f"⚠️ 测试未全部通过，请检查上方失败项。(总耗时: {total_time:.2f}s)")
    print("=" * 60 + "\n")
    return all_ok


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
