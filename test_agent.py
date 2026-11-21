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
import os
import sys
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
    assert "btn-clip" in vt and "btn-ruler" in vt and "btn-theme" in vt and "btn-full" in vt

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
    """测试 8: 本地离线端点与 API Key 网关"""
    cfg_loc = AgentConfig(base_url="http://localhost:11434/v1")
    assert cfg_loc.is_local_endpoint is True
    assert cfg_loc.api_key == "ollama-local"
    assert cfg_loc.require_api_key() == "ollama-local"


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
        ("多格式渲染矩阵 (binstl/3mf/dxf/svg/png)", test_multi_formats_export),
        ("3D 打印体检系统与切片指导书生成", test_doctor_and_guide),
        ("独立 HTML 3D 查看器与全景展厅画廊", test_viewer_and_gallery),
        ("All-in-One 全套工程资产 ZIP 打包", test_packager),
        ("本地离线端点识别与免 Key 认证网关", test_offline_gateway),
        ("机械爆炸图与四视图工业蓝图拼版合成", test_blueprint_and_explode),
        ("局域网 Web 资产共享服务与多端 IP 解析", test_lan_server_and_capture),
        ("3D 文字浮雕生成编译与审查修剪角色交替", test_text_relief_and_trim),
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
