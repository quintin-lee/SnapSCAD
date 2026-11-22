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


def test_scad_library_exporter():
    """测试 24: OpenSCAD 模块生态包导出规范 (命名空间隔离与可复用库函数)"""
    from openscad_agent.packager import export_scad_library
    import subprocess

    with tempfile.TemporaryDirectory() as td:
        spec = {
            "name": "bearing_flange",
            "description": "高精度法兰轴承座",
            "parts": [
                {"id": "flange_base", "primitive": "cube", "size": [50.0, 50.0, 8.0], "center": [0, 0, 0]},
                {"id": "shaft_sleeve", "primitive": "cylinder", "size": [22.0, 20.0], "center": [0, 0, 8.0]},
            ],
        }
        lib_path = export_scad_library(spec, out_dir=td, namespace="snapscad")
        assert lib_path.exists()

        target_dir = lib_path.parent
        ex_path = target_dir / "example.scad"
        md_path = target_dir / "README.md"
        zip_path = target_dir / "bearing_flange_library_bundle.zip"

        assert ex_path.exists()
        assert md_path.exists()
        assert zip_path.exists()

        # 验证生成的库代码包含隔离命名空间
        lib_text = lib_path.read_text(encoding="utf-8")
        assert "module snapscad_bearing_flange_flange_base" in lib_text
        assert "module snapscad_bearing_flange_assembly" in lib_text

        # 验证 example.scad 使用 OpenSCAD 真实编译无报错
        out_stl = Path(td) / "compiled_lib.stl"
        res = subprocess.run(["openscad", "-o", str(out_stl), str(ex_path)], capture_output=True, text=True, timeout=15)
        assert res.returncode == 0
        assert out_stl.exists() and out_stl.stat().st_size > 0


def test_cloud_microservice_api_and_workspace():
    """测试 25: 云原生微服务 REST API 与协同工作台 (Health/Compile/Slice/FastAPI)"""
    from openscad_agent.server import serve_assets, stop_server, create_app
    from openscad_agent.workspace import generate_workspace_html
    import requests

    # 1. 验证云原生 FastAPI 应用工厂
    fastapi_app = create_app()
    assert fastapi_app is not None

    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        # 生成测试文件
        scad_f = out_dir / "cloud_cube.scad"
        scad_f.write_text("cube([15, 15, 10]);\n", encoding="utf-8")
        spec_f = out_dir / "cloud_cube_spec.json"
        spec_f.write_text(json.dumps({
            "name": "cloud_cube",
            "description": "云原生测试方块",
            "parts": [{"id": "cube", "primitive": "cube", "size": [15, 15, 10], "center": [0,0,0]}],
        }), encoding="utf-8")

        # 2. 验证工作台页面生成
        ws_html = generate_workspace_html(out_dir)
        assert ws_html.exists()
        assert "SnapSCAD 交互协同建模工作台" in ws_html.read_text(encoding="utf-8")

        # 3. 启动轻量微服务 (非阻塞模式)
        _, _, port = serve_assets(out_dir, port=8993, open_browser=False, blocking=False)
        time.sleep(0.5)
        base = f"http://127.0.0.1:{port}"

        try:
            # 4. 验证 GET /api/health
            r_h = requests.get(f"{base}/api/health", timeout=3)
            assert r_h.status_code == 200
            assert r_h.json()["status"] == "ok"

            # 5. 验证 GET /api/models
            r_m = requests.get(f"{base}/api/models", timeout=3)
            assert r_m.status_code == 200
            assert len(r_m.json()) >= 1

            # 6. 验证 POST /api/compile (实时重编译)
            r_c = requests.post(f"{base}/api/compile", json={
                "stem": "cloud_cube",
                "scad_code": "cube([18, 18, 12]);\n",
            }, timeout=15)
            assert r_c.status_code == 200
            assert r_c.json()["success"] is True
            assert (out_dir / "cloud_cube.stl").exists()

            # 7. 验证 POST /api/slice (在线切片)
            r_s = requests.post(f"{base}/api/slice", json={
                "stem": "cloud_cube",
                "material": "PLA",
                "layer_height": 0.2,
            }, timeout=15)
            assert r_s.status_code == 200
            assert r_s.json()["success"] is True
            assert (out_dir / "cloud_cube.gcode").exists()

            # 8. 验证 POST /api/export_library (在线导出生态库)
            r_e = requests.post(f"{base}/api/export_library", json={
                "stem": "cloud_cube",
                "namespace": "cloud",
            }, timeout=10)
            assert r_e.status_code == 200
            assert r_e.json()["success"] is True

            # 9. 验证 GET /api/model/{stem}/gcode (原生 G-code 流)
            r_g = requests.get(f"{base}/api/model/cloud_cube/gcode", timeout=5)
            assert r_g.status_code == 200
            assert "G1" in r_g.text or "G0" in r_g.text or "LAYER" in r_g.text

            # 10. 验证 GET /api/printer/status (遥测代理)
            r_tel = requests.get(f"{base}/api/printer/status?server_url=http://127.0.0.1:59999&printer_type=klipper", timeout=5)
            assert r_tel.status_code == 200
            t_data = r_tel.json()
            assert "online" in t_data
            assert t_data["printer_type"] == "klipper"
        finally:
            stop_server()


def test_printer_telemetry_and_status():
    """测试 26: 打印机实时物理遥测解析 (Klipper/OctoPrint/拓竹) 与离线降级"""
    from openscad_agent.dispatcher import DispatchConfig, PrinterDispatcher, PrinterStatus
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import threading

    # 1. 模拟一个 Moonraker (Klipper) 服务器
    class MockKlipperHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if "printer/objects/query" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                data = {
                    "result": {
                        "status": {
                            "heater_bed": {"temperature": 60.1, "target": 60.0},
                            "extruder": {"temperature": 210.4, "target": 210.0},
                            "print_stats": {
                                "state": "printing",
                                "filename": "bracket.gcode",
                                "print_duration": 1200,
                            },
                            "display_status": {"progress": 0.45},
                        }
                    }
                }
                self.wfile.write(json.dumps(data).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), MockKlipperHandler)
    mock_port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        cfg = DispatchConfig(server_url=f"http://127.0.0.1:{mock_port}", printer_type="klipper", timeout=3)
        disp = PrinterDispatcher(cfg)
        status = disp.get_printer_status()

        assert status.online is True
        assert status.state == "printing"
        assert status.printer_type == "klipper"
        assert status.hotend_temp == 210.4
        assert status.bed_temp == 60.1
        assert status.progress == 45.0
        assert status.current_file == "bracket.gcode"
        assert status.print_duration == 1200
        assert status.time_remaining > 0
    finally:
        server.shutdown()

    # 2. 验证离线主机探测 (优雅降级不抛异常)
    cfg_off = DispatchConfig(server_url="http://127.0.0.1:59998", printer_type="octoprint", timeout=1)
    disp_off = PrinterDispatcher(cfg_off)
    st_off = disp_off.get_printer_status()
    assert st_off.online is False
    assert st_off.state == "offline"
    assert len(st_off.diagnostics) > 0

    # 3. 验证拓竹默认状态
    cfg_bambu = DispatchConfig(server_url="192.168.1.188", printer_type="bambu")
    disp_bambu = PrinterDispatcher(cfg_bambu)
    st_bambu = disp_bambu.get_printer_status()
    assert st_bambu.online is True
    assert st_bambu.printer_type == "bambu"


def test_gcode_stream_and_workspace_visualizer():
    """测试 27: 3D 走丝可视化解析引擎与在线工作台动态回放"""
    from openscad_agent.workspace import generate_workspace_html

    with tempfile.TemporaryDirectory() as td:
        ws_p = generate_workspace_html(td)
        assert ws_p.exists()
        txt = ws_p.read_text(encoding="utf-8")

        # 验证 G-code 3D 走丝控制节点
        assert "btn-mode-gcode" in txt
        assert "gcode-hud" in txt
        assert "gcode-layer-slider" in txt
        assert "toggleGcodePlay" in txt
        assert "parseAndRenderGcode" in txt

        # 验证机床实时遥测仪表板节点
        assert "card-telemetry" in txt
        assert "tel-hotend" in txt
        assert "tel-bed" in txt
        assert "tel-prog-bar" in txt
        assert "pollPrinterStatus" in txt


def test_spec_patching_and_hot_reload():
    """测试 28: 参数化规格增量修剪引擎 (patch_spec & apply_patch_to_model)"""
    from openscad_agent.geometry import patch_spec, apply_patch_to_model

    base_spec = {
        "name": "base_bracket",
        "description": "初始支架规格",
        "parts": [
            {
                "id": "base_plate",
                "primitive": "cube",
                "size": [50.0, 40.0, 10.0],
                "center": [0.0, 0.0, 0.0],
                "holes": [{"primitive": "cylinder", "size": [4.0, 12.0], "center": [10.0, 10.0, 0.0]}],
            }
        ],
    }

    # 1. 结构化字典补丁测试
    dict_patch = {
        "modify_parts": [{"id": "base_plate", "size": [60.0, 45.0, 12.0]}],
        "add_parts": [{"id": "pin", "primitive": "cylinder", "size": [5.0, 15.0], "center": [0, 0, 12.0]}],
        "params": {"wall_thickness": 3.0},
    }
    spec_v1 = patch_spec(base_spec, dict_patch)
    assert len(spec_v1["parts"]) == 2
    assert spec_v1["parts"][0]["size"] == [60.0, 45.0, 12.0]
    assert spec_v1["params"]["wall_thickness"] == 3.0

    # 2. 自然语言高度增量测试 ("高度增加 10mm")
    spec_h = patch_spec(base_spec, "高度增加 10mm")
    assert spec_h["parts"][0]["size"][2] == 20.0

    # 3. 自然语言孔径扩大测试 ("孔径扩大 1mm")
    spec_hole = patch_spec(base_spec, "孔径扩大 1mm")
    assert spec_hole["parts"][0]["holes"][0]["size"][0] == 5.0

    # 4. 自然语言倒角测试 ("倒角 2mm")
    spec_c = patch_spec(base_spec, "倒角 2mm")
    assert spec_c["parts"][0]["chamfer"] == 2.0

    # 5. 端到端 apply_patch_to_model 热重载编译测试
    with tempfile.TemporaryDirectory() as td:
        sp_init = Path(td) / "bracket_spec.json"
        sp_init.write_text(json.dumps(base_spec), encoding="utf-8")

        sp, sc, updated = apply_patch_to_model(sp_init, "高度增加 5mm", out_dir=td, recompile=True)
        assert sp.exists() and sc.exists()
        assert updated["parts"][0]["size"][2] == 15.0
        assert (Path(td) / "bracket.stl").exists()
        assert (Path(td) / "bracket.stl").stat().st_size > 0


def test_patch_api_and_workspace_raycast():
    """测试 29: 微服务增量修剪 REST API 与工作台 3D 尺寸测距交互"""
    from openscad_agent.server import serve_assets, stop_server
    from openscad_agent.workspace import generate_workspace_html
    import requests

    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        # 准备模型规格
        spec_f = out_dir / "patch_cube_spec.json"
        spec_f.write_text(json.dumps({
            "name": "patch_cube",
            "description": "修剪测试方块",
            "parts": [{"id": "box", "primitive": "cube", "size": [20, 20, 10], "center": [0,0,0]}],
        }), encoding="utf-8")
        scad_f = out_dir / "patch_cube.scad"
        scad_f.write_text("cube([20, 20, 10]);\n", encoding="utf-8")

        # 1. 验证工作台包含三维测距与增量修剪节点
        ws_p = generate_workspace_html(out_dir)
        txt = ws_p.read_text(encoding="utf-8")
        assert "tab-patch" in txt
        assert "patch-instruction" in txt
        assert "triggerApplyPatch" in txt
        assert "btn-measure" in txt
        assert "measure-hud" in txt
        assert "toggleMeasureMode" in txt
        assert "raycaster" in txt.lower() or "raycaster" in txt

        # 2. 验证微服务 POST /api/patch 端点
        _, _, port = serve_assets(out_dir, port=8991, open_browser=False, blocking=False)
        time.sleep(0.5)
        base = f"http://127.0.0.1:{port}"

        try:
            r = requests.post(f"{base}/api/patch", json={
                "stem": "patch_cube",
                "instruction": "高度增加 15mm",
            }, timeout=15)
            assert r.status_code == 200
            res_data = r.json()
            assert res_data["success"] is True
            assert (out_dir / "patch_cube.stl").exists()
            up_spec = json.loads(spec_f.read_text(encoding="utf-8"))
            assert up_spec["parts"][0]["size"][2] == 25.0
        finally:
            stop_server()


def test_step_and_freecad_interop():
    """测试 30: 工业级 STEP AP214 实体模型与 FreeCAD 参数化建模脚本导出"""
    from openscad_agent.interop import export_step_from_stl, export_freecad_script
    from openscad_agent.core import AgentConfig, OpenscadRenderer
    import ast

    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        scad_f = out_dir / "step_demo.scad"
        scad_f.write_text("cube([20, 15, 10]);\n", encoding="utf-8")
        stl_f = out_dir / "step_demo.stl"
        step_f = out_dir / "step_demo.step"
        fc_f = out_dir / "step_demo_freecad.py"

        cfg = AgentConfig(output_dir=td)
        renderer = OpenscadRenderer(cfg)
        renderer.export_model(scad_f, stl_f, force=True)
        assert stl_f.exists()

        # 1. 验证生成标准 ISO 10303-21 STEP 实体文件
        res_step = export_step_from_stl(stl_f, out_step=step_f, name="step_demo")
        assert res_step.exists() and res_step.stat().st_size > 0
        step_txt = res_step.read_text(encoding="utf-8")
        assert "ISO-10303-21;" in step_txt
        assert "MANIFOLD_SOLID_BREP" in step_txt
        assert "ADVANCED_FACE" in step_txt
        assert "CLOSED_SHELL" in step_txt
        assert "END-ISO-10303-21;" in step_txt

        # 2. 验证生成 FreeCAD Python 自动化脚本
        spec = {
            "name": "step_demo",
            "parts": [
                {"id": "base", "primitive": "cube", "size": [20, 15, 10], "center": [0, 0, 0],
                 "holes": [{"primitive": "cylinder", "size": [4, 15], "center": [5, 5, 0]}]}
            ]
        }
        res_fc = export_freecad_script(spec, out_script=fc_f)
        assert res_fc.exists()
        fc_txt = res_fc.read_text(encoding="utf-8")
        ast.parse(fc_txt)  # 严格验证 Python 语法合规
        assert "Part.makeBox" in fc_txt
        assert "Part.makeCylinder" in fc_txt
        assert ".cut(" in fc_txt
        assert "Part.export" in fc_txt


def test_structural_stress_doctor_and_step_api():
    """测试 31: 机械结构力学应力集中深度分析与微服务 STEP 导出 API"""
    from openscad_agent.doctor import analyze_structural_stress, check_printability
    from openscad_agent.server import serve_assets, stop_server
    import requests

    # 1. 验证紧固孔近壁剪切撕裂预警
    danger_spec = {
        "name": "danger_flange",
        "parts": [
            {
                "id": "thin_wall_part",
                "primitive": "cube",
                "size": [20.0, 20.0, 8.0],
                "center": [0.0, 0.0, 0.0],
                "holes": [{"primitive": "cylinder", "size": [6.0, 10.0], "center": [7.5, 0.0, 0.0]}],
            }
        ]
    }
    diag = analyze_structural_stress(danger_spec)
    assert diag["safety_score"] < 100
    assert any("剪切撕裂" in w or "穿透" in w or "孔洞边缘" in w for w in diag["structural_warnings"])

    # 2. 验证微服务 POST /api/export_step 端点
    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        scad_f = out_dir / "api_step_box.scad"
        scad_f.write_text("cube([12, 12, 8]);\n", encoding="utf-8")
        stl_f = out_dir / "api_step_box.stl"
        from openscad_agent.core import AgentConfig, OpenscadRenderer
        cfg = AgentConfig(output_dir=td)
        renderer = OpenscadRenderer(cfg)
        renderer.export_model(scad_f, stl_f, force=True)

        _, _, port = serve_assets(out_dir, port=8989, open_browser=False, blocking=False)
        time.sleep(0.5)
        base = f"http://127.0.0.1:{port}"

        try:
            r = requests.post(f"{base}/api/export_step", json={"stem": "api_step_box"}, timeout=15)
            assert r.status_code == 200
            data = r.json()
            assert data["success"] is True
            assert (out_dir / "api_step_box.step").exists()
            assert (out_dir / "api_step_box.step").stat().st_size > 0
        finally:
            stop_server()


def test_epsilon_anti_coplanar_and_param_hoisting():
    """测试 32: 几何转译防共面微延展保护 (Epsilon Offset) 与参数提升引擎 (Customizer Hoisting)"""
    from openscad_agent.geometry import spec_to_scad
    from openscad_agent.core import AgentConfig, OpenscadRenderer

    # 1. 验证没有显式 params 时的自动参数提升 (Hoisting)
    spec_auto = {
        "name": "auto_hoist_box",
        "description": "无参数硬编码零件，验证参数自动提升",
        "parts": [
            {
                "id": "main_body",
                "primitive": "cube",
                "size": [60.0, 40.0, 15.0],
                "center": [0.0, 0.0, 0.0],
                "holes": [
                    {"primitive": "cylinder", "size": [8.0, 15.0], "center": [15.0, 10.0, 0.0]}
                ]
            }
        ],
        "assembly": {"union_parts": ["main_body"]}
    }

    scad_code = spec_to_scad(spec_auto, fn=32)
    # 验证防共面微延展常数与说明
    assert "epsilon = 0.05;" in scad_code
    # 验证孔洞带防共面微延展 (cz - 0.05, h + 2 * 0.05 = 15.1)
    assert "防共面" in scad_code
    assert "15.1" in scad_code
    # 验证关键尺寸被提升为 Customizer 交互滑块参数
    assert "/* [Parameters] */" in scad_code
    assert "main_body_x = 60;" in scad_code or "main_body_x = 60.0;" in scad_code
    assert "main_body_y = 40;" in scad_code or "main_body_y = 40.0;" in scad_code
    assert "main_body_z = 15;" in scad_code or "main_body_z = 15.0;" in scad_code
    assert "main_body 长度 (X)" in scad_code

    # 2. 真实 OpenSCAD CGAL 编译验证
    with tempfile.TemporaryDirectory() as td:
        scad_f = Path(td) / "hoist_demo.scad"
        scad_f.write_text(scad_code, encoding="utf-8")
        cfg = AgentConfig(output_dir=td)
        renderer = OpenscadRenderer(cfg)
        ok, msg = renderer.validate(scad_f)
        assert ok, f"带参数提升与防共面微延展的 OpenSCAD 编译失败: {msg}"


def test_local_healing_and_incremental_patch():
    """测试 33: 本地零延迟规则自愈引擎 (heal_spec_locally) 与局部部件定位 (extract_failing_part_id)"""
    from openscad_agent.doctor import heal_spec_locally, extract_failing_part_id
    from openscad_agent.geometry import patch_spec

    # 1. 验证本地规则库对极端非法规格的 0ms 自愈
    bad_spec = {
        "name": "broken_model",
        "parts": [
            {
                "id": "1_bad#id",  # 非法标识符
                "primitive": "cube",
                "size": [-10.0, 0.0, 15.0],  # 负数与零尺寸
            },
            {
                "id": "bad_poly",
                "primitive": "polyhedron",
                "vertices": [[0,0,0], [10,0,0], [5,10,0], [5,5,10]],
                "faces": [[0, 1, 2], [0, 1, 9999]]  # 越界顶点索引 9999
            },
            {
                "id": "bad_extrude",
                "primitive": "linear_extrude",
                "polygon": [[0,0]]  # 仅 1 个点，不足 3 点
            }
        ],
        "assembly": {
            "union_parts": ["1_bad#id", "bad_poly", "ghost_nonexistent_part"]  # 含悬空不存在部件
        }
    }

    healed_spec, was_fixed, actions = heal_spec_locally(bad_spec, compile_err="ERROR: Parser error")
    assert was_fixed is True
    assert len(actions) >= 4

    p0 = healed_spec["parts"][0]
    assert p0["id"] == "p_1_bad_id"
    assert p0["size"][0] > 0 and p0["size"][1] > 0

    p1 = healed_spec["parts"][1]
    assert len(p1["faces"]) == 1  # 越界面已被剔除

    p2 = healed_spec["parts"][2]
    assert len(p2["polygon"]) >= 3  # 点集已自动补齐

    # 悬空部件已从装配中剔除
    assert "ghost_nonexistent_part" not in healed_spec["assembly"]["union_parts"]

    # 2. 验证编译器报错部件定位
    err_text = "WARNING: CGAL error in module arm_bracket, line 45"
    assert extract_failing_part_id(err_text) == "arm_bracket"

    scad_code = (
        "module base_plate() { cube([10,10,2]); }\n"
        "module pillar_mount() {\n"
        "  translate([0,0,0]) cylinder(d=0, h=0);\n"
        "}\n"
    )
    assert extract_failing_part_id("ERROR: line 3: cylinder dimension error", scad_code) == "pillar_mount"

    # 3. 验证局部 patch 修剪修复
    patch_data = {"modify_parts": [{"id": "p_1_bad_id", "size": [20.0, 30.0, 40.0]}]}
    patched = patch_spec(healed_spec, patch_data)
    assert patched["parts"][0]["size"] == [20.0, 30.0, 40.0]
    assert len(patched["parts"]) == 3  # 其他部件丝毫不受影响


def test_geometric_constraint_solver_and_semantic_anchors():
    """测试 34: 确定性语义锚点与相对几何约束求解器 (resolve_geometric_constraints)"""
    from openscad_agent.spec import resolve_geometric_constraints, validate_spec
    from openscad_agent.geometry import spec_to_scad
    from openscad_agent.core import AgentConfig, OpenscadRenderer

    spec = {
        "name": "assembly_anchors",
        "description": "基于语义相对锚点与孔系阵列求解的模型",
        "parts": [
            {
                "id": "base_plate",
                "primitive": "cube",
                "size": [100.0, 80.0, 10.0],
                "center": [0.0, 0.0, 0.0],
                "holes": [
                    # 四角阵列沉孔: 边距 8mm
                    {"primitive": "cylinder", "size": [4.0, 10.0], "pattern": "4_corners", "corner_inset": 8.0}
                ]
            },
            {
                "id": "center_boss",
                "primitive": "cylinder",
                "size": [30.0, 25.0],
                "attach_to": "base_plate",
                "face": "top",
                "align": "center",
                "holes": [
                    {"primitive": "cylinder", "size": [10.0, 25.0], "align": "center"}
                ]
            },
            {
                "id": "side_flange",
                "primitive": "cube",
                "size": [15.0, 20.0, 10.0],
                "attach_to": "base_plate",
                "face": "right",
                "align": "center"
            }
        ],
        "assembly": {"union_parts": ["base_plate", "center_boss", "side_flange"]}
    }

    # 1. 约束求解器推导
    solved, actions = resolve_geometric_constraints(spec)
    assert len(actions) >= 3

    # 验证 base_plate 四角阵列通孔
    base_p = solved["parts"][0]
    assert len(base_p["holes"]) == 4
    h_centers = [h["center"] for h in base_p["holes"]]
    assert [8.0, 8.0, 0.0] in h_centers
    assert [92.0, 8.0, 0.0] in h_centers
    assert [8.0, 72.0, 0.0] in h_centers
    assert [92.0, 72.0, 0.0] in h_centers

    # 验证 center_boss 自动求解到 base_plate 顶部 (z=10) 且 XY 居中 (x=50, y=40)
    boss_p = solved["parts"][1]
    assert boss_p["center"] == [50.0, 40.0, 10.0]
    # 验证内部中心孔自动继承 boss 中心
    assert boss_p["holes"][0]["center"] == [50.0, 40.0, 10.0]

    # 验证 side_flange 自动贴合在 base_plate 右侧面 (x=100.0)
    flange_p = solved["parts"][2]
    assert flange_p["center"][0] == 100.0
    assert flange_p["center"][2] == 0.0

    # 2. 真实 OpenSCAD 几何生成与编译校验
    scad_code = spec_to_scad(spec, fn=32)
    with tempfile.TemporaryDirectory() as td:
        scad_f = Path(td) / "anchor_test.scad"
        scad_f.write_text(scad_code, encoding="utf-8")
        cfg = AgentConfig(output_dir=td)
        renderer = OpenscadRenderer(cfg)
        ok, msg = renderer.validate(scad_f)
        assert ok, f"语义锚点求解生成的 OpenSCAD 编译失败: {msg}"


def test_structured_output_and_robust_json_parsing():
    """测试 35: 服务端结构化约束解码与容错 JSON 解析器 (robust_json_loads)"""
    from openscad_agent.vision import robust_json_loads, AgnesVision

    # 1. 验证被截断缺失闭合括号的 JSON 自动补全
    truncated_raw = '{"name": "cut_cube", "parts": [{"id": "box", "primitive": "cube", "size": [10, 20, 30]'
    data1 = robust_json_loads(truncated_raw)
    assert data1["name"] == "cut_cube"
    assert data1["parts"][0]["id"] == "box"

    # 2. 验证多余尾逗号 (trailing commas) 清洗
    trailing_raw = """
    {
      "name": "comma_test",
      "parts": [
        {"id": "p1", "primitive": "cylinder", "size": [10, 20],},
      ],
    }
    """
    data2 = robust_json_loads(trailing_raw)
    assert data2["name"] == "comma_test"
    assert len(data2["parts"]) == 1

    # 3. 验证带前后自由文本和 Markdown 代码块的内容提取
    mixed_raw = """
    好的，这是根据您的描述为您生成的 3D 模型规格：
    ```json
    {
      "name": "flange",
      "parts": [{"id": "flange_base", "primitive": "cylinder", "size": [50, 5]}]
    }
    ```
    请在 OpenSCAD 中查看效果！
    """
    spec = AgnesVision._extract_spec(mixed_raw)
    assert spec["name"] == "flange"
    assert spec["parts"][0]["primitive"] == "cylinder"


def test_engineering_priors_and_image_roi_crop():
    """测试 36: 工业常识规范先验库 (apply_engineering_priors) 与视觉目标 ROI 自动裁剪"""
    from openscad_agent.spec import apply_engineering_priors
    from openscad_agent.core import auto_crop_roi, run_cmd
    import shutil

    # 1. 验证外壳类工程常识先验注入与薄壁加固
    spec_enclosure = {
        "name": "rpi_case",
        "description": "树莓派机箱外壳保护套",
        "parts": [
            {
                "id": "bottom_shell",
                "primitive": "cube",
                "size": [85.0, 56.0, 0.4],  # 0.4mm 极端薄壁易断裂
                "center": [0.0, 0.0, 0.0]
            }
        ]
    }
    prior_spec, actions = apply_engineering_priors(spec_enclosure, user_hint="树莓派外壳")
    assert len(actions) >= 2
    # 验证壁厚参数与公差已注入
    assert prior_spec["params"]["wall_thickness"] == 2.0
    assert prior_spec["params"]["fit_clearance"] == 0.25
    # 验证零件薄壁已被加固至安全阈值 1.6mm
    assert prior_spec["parts"][0]["size"][2] >= 1.6

    # 2. 验证支架类工程常识先验
    spec_bracket = {
        "name": "motor_bracket",
        "description": "步进电机安装固定支架",
        "parts": [{"id": "b1", "primitive": "cube", "size": [40, 40, 1.0]}]
    }
    b_spec, b_actions = apply_engineering_priors(spec_bracket)
    assert b_spec["params"]["wall_thickness"] == 3.0
    assert b_spec["parts"][0]["size"][2] >= 2.5

    # 3. 验证 ImageMagick ROI 自动裁剪
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        with tempfile.TemporaryDirectory() as td:
            src_png = Path(td) / "test_full.png"
            dst_png = Path(td) / "test_cropped.png"
            # 生成带大面积白边的色块图片 (200x200 带有中间 50x50 红色块)
            create_cmd = [magick, "-size", "200x200", "xc:white", "-fill", "red", "-draw", "rectangle 75,75 125,125", str(src_png)]
            run_cmd(create_cmd)
            assert src_png.exists()

            cropped = auto_crop_roi(src_png, dst_png)
            assert cropped.exists()
            assert cropped.stat().st_size > 0


def test_dimensioned_technical_blueprint_and_api():
    """测试 37: 工业级工程制图自动尺寸标注与矢量图纸引擎 (blueprint) 及微服务端点"""
    from openscad_agent.blueprint import generate_technical_blueprint
    from openscad_agent.server import serve_assets, stop_server
    import requests

    with tempfile.TemporaryDirectory() as td:
        out_dir = Path(td)
        spec = {
            "name": "mounting_flange",
            "description": "法兰安装底座",
            "parts": [
                {
                    "id": "base_plate",
                    "primitive": "cube",
                    "size": [60.0, 40.0, 12.0],
                    "center": [0.0, 0.0, 0.0],
                    "holes": [
                        {"primitive": "cylinder", "size": [6.0, 20.0], "center": [10.0, 10.0, 0.0]},
                        {"primitive": "cylinder", "size": [6.0, 20.0], "center": [50.0, 30.0, 0.0]},
                    ]
                }
            ]
        }
        svg_file = out_dir / "flange_blueprint.svg"

        # 1. 验证纯矢量工程图纸生成与规范合规
        res_svg = generate_technical_blueprint(spec, out_svg=svg_file, material="PETG")
        assert res_svg.exists()
        assert res_svg.stat().st_size > 0

        svg_txt = res_svg.read_text(encoding="utf-8")
        # 验证 SVG 根节点与标准工程幅面
        assert '<svg xmlns="http://www.w3.org/2000/svg"' in svg_txt
        assert 'viewBox="0 0 1200 800"' in svg_txt
        # 验证图框与内边框
        assert 'class="border-outer"' in svg_txt
        assert 'class="border-inner"' in svg_txt
        # 验证标准正交三视图与轴测图
        assert '俯视图 TOP VIEW (X-Y)' in svg_txt
        assert '主视图 FRONT VIEW (X-Z)' in svg_txt
        assert '侧视图 SIDE VIEW (Y-Z)' in svg_txt
        assert '轴测图 ISOMETRIC 3D' in svg_txt
        # 验证双向尺寸箭头与引出线定义
        assert '<marker id="arrow"' in svg_txt
        assert '<marker id="arrow_rev"' in svg_txt
        assert 'class="dim-line"' in svg_txt
        assert 'class="dim-text"' in svg_txt
        # 验证尺寸标注数值正确提取
        assert 'W 60' in svg_txt
        assert 'D 40' in svg_txt
        assert 'H 12' in svg_txt
        # 验证通孔直径引线标注与第三角投影法图符
        assert '⌀6 THRU' in svg_txt
        assert 'PETG (FDM)' in svg_txt
        assert 'MOUNTING_FLANGE' in svg_txt
        assert 'METRIC (mm)' in svg_txt

        # 2. 准备 spec 文件并验证微服务 GET /api/model/{stem}/blueprint 与 POST /api/export_blueprint
        spec_file = out_dir / "flange_spec.json"
        spec_file.write_text(json.dumps(spec), encoding="utf-8")

        _, _, port = serve_assets(out_dir, port=8994, open_browser=False, blocking=False)
        time.sleep(0.5)
        base = f"http://127.0.0.1:{port}"

        try:
            # 测试 GET 图纸端点
            r_get = requests.get(f"{base}/api/model/flange/blueprint", timeout=10)
            assert r_get.status_code == 200
            assert "image/svg+xml" in r_get.headers.get("Content-Type", "")
            assert "<svg" in r_get.text
            assert "MOUNTING_FLANGE" in r_get.text

            # 测试 POST 导出图纸端点
            r_post = requests.post(f"{base}/api/export_blueprint", json={
                "stem": "flange",
                "material": "ABS",
            }, timeout=10)
            assert r_post.status_code == 200
            p_data = r_post.json()
            assert p_data["success"] is True
            assert p_data["svg"] == "flange_blueprint.svg"
        finally:
            stop_server()


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
        ("OpenSCAD 模块生态包导出规范 (命名空间与外部库调用)", test_scad_library_exporter),
        ("云原生微服务 REST API 与协同工作台 (Health/Compile/Slice/FastAPI)", test_cloud_microservice_api_and_workspace),
        ("打印机实时物理遥测解析 (Klipper/OctoPrint/拓竹) 与离线降级", test_printer_telemetry_and_status),
        ("3D 走丝可视化解析引擎与在线工作台动态回放", test_gcode_stream_and_workspace_visualizer),
        ("参数化规格增量修剪引擎 (patch_spec & apply_patch_to_model)", test_spec_patching_and_hot_reload),
        ("微服务增量修剪 REST API 与工作台 3D 尺寸测距交互", test_patch_api_and_workspace_raycast),
        ("工业级 STEP AP214 实体模型与 FreeCAD 参数化建模脚本导出", test_step_and_freecad_interop),
        ("机械结构力学应力集中深度分析与微服务 STEP 导出 API", test_structural_stress_doctor_and_step_api),
        ("几何转译防共面微延展保护与 Customizer 参数提升引擎", test_epsilon_anti_coplanar_and_param_hoisting),
        ("本地零延迟自愈规则库与故障部件定位 (heal_spec_locally)", test_local_healing_and_incremental_patch),
        ("确定性语义锚点与相对几何约束求解器 (resolve_geometric_constraints)", test_geometric_constraint_solver_and_semantic_anchors),
        ("服务端结构化语法约束解码与容错 JSON 解析 (robust_json_loads)", test_structured_output_and_robust_json_parsing),
        ("工业常识先验规范库与目标 ROI 自动裁剪 (apply_engineering_priors)", test_engineering_priors_and_image_roi_crop),
        ("工业级工程制图自动尺寸标注与矢量图纸引擎 (blueprint)", test_dimensioned_technical_blueprint_and_api),
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
