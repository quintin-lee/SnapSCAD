"""SnapSCAD 局域网 3D 资产共享 Web 服务器与云原生微服务 (Web Server & Microservice)。

提供两套无缝融合的微服务架构：
1. 零重型依赖的原生 REST API + 静态资产共享服务器 (SnapSCADRequestHandler)
   - `/api/health`: 服务健康检查与依赖状态
   - `/api/models`: 历史模型资产清单与制造指标
   - `/api/model/{stem}`: 单个模型规格与代码详情
   - `/api/compile`: 动态在线重编译求值与实时 3D 渲染 (Live Compile)
   - `/api/slice`: 在线调用切片引擎实时直出 G-code
   - `/api/export_library`: 一键将模型转译为标准 OpenSCAD 模块生态库包
   - `/api/dispatch`: 一键向局域网 3D 打印机 (Klipper/OctoPrint/拓竹) 投递任务
   - `/workspace.html`: 浏览器端协同 3D WebGL 交互建模工作台
2. 云原生 FastAPI 微服务应用工厂 (`create_app`):
   - 支持通过 uvicorn / gunicorn / Docker / Kubernetes 容器化部署企业级 CAD 微服务集群
"""

from __future__ import annotations

import functools
import json
import os
import socket
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional

from .gallery import generate_gallery
from .log import get_logger as _log_module
from .workspace import generate_workspace_html

_log = _log_module()

_server_thread: Optional[threading.Thread] = None
_active_httpd: Optional[HTTPServer] = None
_lock = threading.Lock()  # 保护全局 httpd/thread 的并发赋值


def get_local_lan_ip() -> str:
    """获取本机局域网 IPv4 地址。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def find_free_port(start_port: int = 8080, max_tries: int = 50) -> int:
    """寻找从 start_port 开始的空闲端口。"""
    for p in range(start_port, start_port + max_tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start_port


def is_port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """检测指定端口是否已被占用。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
            s.close()
            return False
    except OSError:
        return True


def stop_server() -> None:
    """停止并清理当前后台 HTTPServer 与守护线程 (线程安全)。"""
    global _server_thread, _active_httpd
    with _lock:
        if _active_httpd is not None:
            try:
                _active_httpd.shutdown()
                _active_httpd.server_close()
            except Exception:
                pass
            _active_httpd = None
        if _server_thread is not None and _server_thread.is_alive():
            _server_thread.join(timeout=3)
        _server_thread = None


# ---------------------------------------------------------------------------
# SnapSCAD 零依赖微服务 REST API 处理器
# ---------------------------------------------------------------------------

class SnapSCADRequestHandler(SimpleHTTPRequestHandler):
    """兼具静态资产托管与 REST API 的轻量微服务请求处理器。"""

    def send_json(self, data: Any, status: int = 200) -> None:
        """发送 JSON 响应。"""
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self) -> None:
        """处理 CORS 预检请求。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        clean_path = self.path.split("?")[0]
        out_dir = Path(self.directory).resolve()

        # 1. 微服务健康探测
        if clean_path == "/api/health":
            self.send_json({
                "status": "ok",
                "service": "SnapSCAD Cloud-Native Microservice",
                "version": "1.0.0",
                "hosted_dir": str(out_dir),
            })
            return

        # 2. 获取模型列表
        if clean_path == "/api/models":
            spec_files = sorted(out_dir.glob("*_spec.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            models = []
            for sf in spec_files:
                stem = sf.stem.replace("_spec", "")
                try:
                    spec = json.loads(sf.read_text(encoding="utf-8"))
                    models.append({
                        "name": spec.get("name", stem),
                        "stem": stem,
                        "desc": spec.get("description", ""),
                        "has_stl": (out_dir / f"{stem}.stl").exists(),
                        "has_3mf": (out_dir / f"{stem}.3mf").exists(),
                        "has_multi_3mf": (out_dir / f"{stem}_multimaterial.3mf").exists(),
                        "has_sop": (out_dir / f"{stem}_sop.html").exists(),
                        "has_gcode": (out_dir / f"{stem}.gcode").exists(),
                        "has_blueprint": (out_dir / f"{stem}_blueprint.svg").exists(),
                        "has_viewer": (out_dir / f"{stem}_viewer.html").exists(),
                    })
                except Exception:
                    pass
            self.send_json(models)
            return

        # 2.1 紧固件数据库目录
        if clean_path == "/api/fasteners/catalog":
            try:
                from .fasteners import FASTENER_CATALOG, FastenerStandard
                self.send_json({
                    "success": True,
                    "catalog": FASTENER_CATALOG,
                    "standards": [e.value for e in FastenerStandard],
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 2.2 工业管路规格库目录
        if clean_path == "/api/piping/catalog":
            try:
                from .piping import PIPE_CATALOG, PipeStandard
                self.send_json({
                    "success": True,
                    "catalog": PIPE_CATALOG,
                    "standards": [e.value for e in PipeStandard],
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 2.3 钣金材料物性与 K 因子目录
        if clean_path == "/api/sheetmetal/materials":
            try:
                from .sheetmetal import SHEET_METAL_MATERIALS
                self.send_json({
                    "success": True,
                    "materials": SHEET_METAL_MATERIALS,
                    "presets": ["l_bracket", "u_channel", "enclosure_tray"],
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 3. 获取单模型详情 或 G-code 原始内容 或 矢量工程图纸
        if clean_path.startswith("/api/model/"):
            rest = clean_path.replace("/api/model/", "").strip()
            if rest.endswith("/gcode"):
                stem = rest.replace("/gcode", "").strip()
                cand_gcode = out_dir / f"{stem}.gcode"
                if cand_gcode.exists():
                    data = cand_gcode.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                else:
                    self.send_json({"error": f"模型 '{stem}' 尚未切片生成 G-code"}, status=404)
                    return

            if rest.endswith("/blueprint"):
                stem = rest.replace("/blueprint", "").strip()
                cand_svg = out_dir / f"{stem}_blueprint.svg"
                if not cand_svg.exists():
                    cand_spec = out_dir / f"{stem}_spec.json"
                    if cand_spec.exists():
                        try:
                            from .blueprint import generate_technical_blueprint
                            spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                            preview_png = out_dir / f"{stem}.png"
                            generate_technical_blueprint(spec, cand_svg, preview_png=preview_png)
                        except Exception as e:
                            self.send_json({"error": f"生成工程图纸失败: {e}"}, status=500)
                            return
                if cand_svg.exists():
                    data = cand_svg.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                else:
                    self.send_json({"error": f"模型 '{stem}' 未找到工程图纸"}, status=404)
                    return

            if rest.endswith("/assembly_sop"):
                stem = rest.replace("/assembly_sop", "").strip()
                cand_spec = out_dir / f"{stem}_spec.json"
                if cand_spec.exists():
                    try:
                        from .assembly_sop import derive_assembly_sequence
                        spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                        plan = derive_assembly_sequence(spec)
                        self.send_json(plan.to_dict())
                        return
                    except Exception as e:
                        self.send_json({"error": f"推导装配工艺时序失败: {e}"}, status=500)
                        return
                else:
                    self.send_json({"error": f"未找到模型规格: {stem}"}, status=404)
                    return

            if rest.endswith("/interference"):
                stem = rest.replace("/interference", "").strip()
                cand_spec = out_dir / f"{stem}_spec.json"
                if cand_spec.exists():
                    try:
                        from .interference import check_assembly_interference
                        spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                        report = check_assembly_interference(spec)
                        self.send_json(report.to_dict())
                        return
                    except Exception as e:
                        self.send_json({"error": f"执行干涉检查失败: {e}"}, status=500)
                        return
                else:
                    self.send_json({"error": f"未找到模型规格: {stem}"}, status=404)
                    return

            if rest.endswith("/fasteners_bom"):
                stem = rest.replace("/fasteners_bom", "").strip()
                cand_spec = out_dir / f"{stem}_spec.json"
                if cand_spec.exists():
                    try:
                        from .fasteners import generate_fasteners_bom
                        spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                        bom = generate_fasteners_bom(spec)
                        self.send_json({"success": True, "stem": stem, "bom": bom})
                        return
                    except Exception as e:
                        self.send_json({"error": f"生成紧固件 BOM 失败: {e}"}, status=500)
                        return
                else:
                    self.send_json({"error": f"未找到模型规格: {stem}"}, status=404)
                    return

            stem = rest
            cand_spec = out_dir / f"{stem}_spec.json"
            cand_scad = out_dir / f"{stem}.scad"
            if cand_spec.exists():
                try:
                    spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                    scad_txt = cand_scad.read_text(encoding="utf-8") if cand_scad.exists() else ""
                    self.send_json({"stem": stem, "spec": spec, "scad": scad_txt})
                    return
                except Exception as e:
                    self.send_json({"error": str(e)}, status=500)
                    return
            self.send_json({"error": f"模型 '{stem}' 不存在"}, status=404)
            return

        # 4. 获取 3D 打印机实时物理遥测指标
        if clean_path == "/api/printer/status":
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            server_url = qs.get("server_url", ["http://localhost:7125"])[0]
            printer_type = qs.get("printer_type", ["auto"])[0]
            api_key = qs.get("api_key", [""])[0]
            access_code = qs.get("access_code", [""])[0] or api_key

            from .dispatcher import DispatchConfig, PrinterDispatcher
            cfg = DispatchConfig(
                server_url=server_url,
                printer_type=printer_type,
                api_key=api_key,
                access_code=access_code,
            )
            dispatcher = PrinterDispatcher(cfg)
            status = dispatcher.get_printer_status()
            self.send_json({
                "online": status.online,
                "state": status.state,
                "printer_type": status.printer_type,
                "hotend_temp": status.hotend_temp,
                "hotend_target": status.hotend_target,
                "bed_temp": status.bed_temp,
                "bed_target": status.bed_target,
                "progress": status.progress,
                "current_file": status.current_file,
                "print_duration": status.print_duration,
                "time_remaining": status.time_remaining,
                "message": status.message,
                "diagnostics": status.diagnostics,
            })
            return

        # 5. 路由别名跳转
        if clean_path in ("/", "/gallery"):
            self.path = "/gallery.html"
        elif clean_path in ("/workspace", "/work"):
            self.path = "/workspace.html"

        super().do_GET()

    def do_POST(self) -> None:
        clean_path = self.path.split("?")[0]
        out_dir = Path(self.directory).resolve()

        # 读取请求体
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception as e:
            self.send_json({"error": f"请求体 JSON 解析失败: {e}"}, status=400)
            return

        # 1. 动态在线重编译 (Live Compile)
        if clean_path == "/api/compile":
            scad_code = body.get("scad_code", "")
            stem = body.get("stem", "temp_model")
            if not scad_code:
                self.send_json({"error": "缺少 scad_code 源码"}, status=400)
                return

            scad_file = out_dir / f"{stem}.scad"
            stl_file = out_dir / f"{stem}.stl"
            scad_file.write_text(scad_code, encoding="utf-8")

            from .core import AgentConfig, OpenscadRenderer
            cfg = AgentConfig(output_dir=str(out_dir))
            renderer = OpenscadRenderer(cfg)
            ok, msg = renderer.validate(scad_file)
            if ok:
                try:
                    renderer.export_model(scad_file, stl_file, force=True)
                    self.send_json({"success": True, "message": "编译并导出 STL 成功", "stl": stl_file.name})
                except Exception as e:
                    self.send_json({"success": False, "errors": [str(e)]})
            else:
                self.send_json({"success": False, "errors": [msg]})
            return

        # 2. 在线切片直出 (Slice)
        if clean_path == "/api/slice":
            stem = body.get("stem", "")
            stl_file = out_dir / f"{stem}.stl"
            if not stl_file.exists():
                self.send_json({"error": f"待切片 STL 模型不存在: {stem}.stl"}, status=404)
                return

            material = body.get("material", "PLA")
            layer_height = float(body.get("layer_height", 0.2))
            infill_density = int(body.get("infill_density", 15))

            from .slicer import slice_model, SlicingConfig
            s_cfg = SlicingConfig(material=material, layer_height=layer_height, infill_density=infill_density)
            gcode_file = out_dir / f"{stem}.gcode"
            res = slice_model(stl_file, out_gcode=gcode_file, config=s_cfg, force=True)

            if res.success:
                self.send_json({
                    "success": True,
                    "gcode": gcode_file.name,
                    "metrics": {
                        "time_formatted": res.estimated_time_formatted,
                        "time_minutes": res.estimated_time_minutes,
                        "filament_weight_g": res.filament_weight_grams,
                        "filament_length_m": res.filament_length_meters,
                        "layer_count": res.layer_count,
                    }
                })
            else:
                self.send_json({"success": False, "error": res.error_msg})
            return

        # 3. OpenSCAD 模块生态库导出 (Export Library)
        if clean_path == "/api/export_library":
            stem = body.get("stem", "")
            cand_spec = out_dir / f"{stem}_spec.json"
            from .packager import export_scad_library
            try:
                target_arg = cand_spec if cand_spec.exists() else stem
                lib_path = export_scad_library(target_arg, out_dir=out_dir, namespace=body.get("namespace", "snapscad"))
                self.send_json({
                    "success": True,
                    "lib_file": lib_path.name,
                    "dir": str(lib_path.parent.name),
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)})
            return

        # 4. 打印机网络推送调度 (Dispatch)
        if clean_path == "/api/dispatch":
            stem = body.get("stem", "")
            target_file = out_dir / f"{stem}.gcode"
            if not target_file.exists():
                target_file = out_dir / f"{stem}.stl"
            if not target_file.exists():
                self.send_json({"success": False, "message": f"未找到可推送的文件 ({stem}.gcode 或 {stem}.stl)"})
                return

            server_url = body.get("server_url", "http://localhost:7125")
            printer_type = body.get("printer_type", "auto")
            access_code = body.get("access_code", "")

            from .dispatcher import DispatchConfig, PrinterDispatcher
            cfg = DispatchConfig(
                server_url=server_url,
                printer_type=printer_type,
                access_code=access_code,
                api_key=access_code,
            )
            dispatcher = PrinterDispatcher(cfg)
            res = dispatcher.upload_file(target_file)
            self.send_json({
                "success": res.success,
                "message": res.message,
                "diagnostics": res.diagnostics,
            })
            return

        # 5. 交互式规格参数化增量修剪与热重载 (Patch Spec)
        if clean_path == "/api/patch":
            stem = body.get("stem", "")
            instruction = body.get("instruction", "")
            if not stem:
                self.send_json({"error": "缺少模型 stem 名称"}, status=400)
                return
            if not instruction:
                self.send_json({"error": "缺少修剪指令 instruction"}, status=400)
                return

            from .geometry import apply_patch_to_model
            try:
                spec_p, scad_p, updated_spec = apply_patch_to_model(stem, instruction, out_dir=out_dir, recompile=True)
                self.send_json({
                    "success": True,
                    "stem": stem,
                    "message": "参数化增量修剪成功并已重新编译 STL",
                    "spec": updated_spec,
                    "stl": f"{stem}.stl",
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 6. 工业级 STEP 实体与 FreeCAD 参数化建模脚本导出 (Export STEP)
        if clean_path == "/api/export_step":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少模型 stem 名称"}, status=400)
                return

            stl_file = out_dir / f"{stem}.stl"
            if not stl_file.exists():
                self.send_json({"error": f"未找到可转换的 STL 实体: {stem}.stl，请先完成渲染"}, status=404)
                return

            from .interop import export_step_from_stl, export_freecad_script
            try:
                step_file = out_dir / f"{stem}.step"
                export_step_from_stl(stl_file, out_step=step_file, name=stem)

                cand_spec = out_dir / f"{stem}_spec.json"
                freecad_file = out_dir / f"{stem}_freecad.py"
                if cand_spec.exists():
                    spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                    export_freecad_script(spec, out_script=freecad_file)

                self.send_json({
                    "success": True,
                    "stem": stem,
                    "step": step_file.name,
                    "freecad": freecad_file.name if freecad_file.exists() else "",
                    "message": "工业级标准 STEP 实体模型已生成",
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 7. 工业级带标注正交工程图纸导出 (Export Blueprint)
        if clean_path == "/api/export_blueprint":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少模型 stem 名称"}, status=400)
                return

            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                self.send_json({"error": f"未找到模型规格: {stem}_spec.json"}, status=404)
                return

            from .blueprint import generate_technical_blueprint
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                out_svg = out_dir / f"{stem}_blueprint.svg"
                out_png = out_dir / f"{stem}_blueprint.png"
                preview_png = out_dir / f"{stem}.png"
                material = body.get("material", "PLA")
                generate_technical_blueprint(spec, out_svg, out_png=out_png, preview_png=preview_png, material=material)
                self.send_json({
                    "success": True,
                    "stem": stem,
                    "svg": out_svg.name,
                    "png": out_png.name if out_png.exists() else "",
                    "message": "工业级标准工程尺寸标注图纸已生成",
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 8. 热力学有限元翘曲形变分析 (Thermal Warping Analysis)
        if clean_path == "/api/fea/thermal_warping":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少模型 stem 名称"}, status=400)
                return
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                self.send_json({"error": f"未找到模型规格: {stem}_spec.json"}, status=404)
                return
            from .fea import estimate_thermal_warping
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                material = body.get("material", "PLA")
                chamber = bool(body.get("chamber_heated", False))
                report = estimate_thermal_warping(spec, material=material, chamber_heated=chamber)
                self.send_json({
                    "success": True,
                    "stem": stem,
                    "report": report.to_dict(),
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 9. 3D 装配公差与孔径热缩反向补偿重载 (Tolerance & Shrinkage Compensation)
        if clean_path == "/api/tolerance/compensate":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少模型 stem 名称"}, status=400)
                return
            material = body.get("material", "PLA")
            fit_type = body.get("fit_type", "normal")
            recompile = bool(body.get("recompile", True))
            from .fea import compensate_and_recompile_model
            try:
                spec_p, scad_p, comp_spec, actions = compensate_and_recompile_model(
                    stem, out_dir=out_dir, material=material, fit_type=fit_type, recompile=recompile
                )
                self.send_json({
                    "success": True,
                    "stem": stem,
                    "actions": actions,
                    "spec": comp_spec,
                    "stl": f"{stem}.stl",
                    "message": f"装配公差与热缩补偿成功并已重新渲染 ({len(actions)} 项修正)",
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 10. 原生多色多耗材 3MF 生产切片工程导出 (Export Multi-Material 3MF)
        if clean_path == "/api/export_multimaterial_3mf":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少模型 stem 名称"}, status=400)
                return
            cand_spec = out_dir / f"{stem}_spec.json"
            cand_scad = out_dir / f"{stem}.scad"
            if not cand_spec.exists() or not cand_scad.exists():
                self.send_json({"error": f"未找到模型文件: {stem}_spec.json 或 {stem}.scad"}, status=404)
                return
            from .multimaterial import export_multimaterial_3mf
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                out_3mf = out_dir / f"{stem}_multimaterial.3mf"
                extruder_map = body.get("extruder_map")
                color_map = body.get("color_map")
                p_3mf = export_multimaterial_3mf(cand_scad, spec, out_3mf, extruder_map=extruder_map, color_map=color_map)
                parts_len = len(spec.get("parts") or [])
                self.send_json({
                    "success": True,
                    "stem": stem,
                    "file_3mf": out_3mf.name,
                    "parts_count": parts_len,
                    "message": "原生多色多耗材 3MF 工程文件已生成，适配拓竹 AMS / Prusa MMU3！",
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        if clean_path == "/api/lattice/estimate":
            stem = body.get("stem", "")
            cand_spec = out_dir / f"{stem}_spec.json" if stem else None
            w = float(body.get("width", 50.0))
            l = float(body.get("length", 40.0))
            h = float(body.get("height", 10.0))
            if cand_spec and cand_spec.exists():
                try:
                    s_data = json.loads(cand_spec.read_text(encoding="utf-8"))
                    p0 = (s_data.get("parts") or [{}])[0]
                    sz = p0.get("size") or [50, 40, 10]
                    if len(sz) >= 3:
                        w, l, h = float(sz[0]), float(sz[1]), float(sz[2])
                except Exception:
                    pass
            from .lattice import estimate_lattice_weight_savings
            pat = body.get("pattern", "honeycomb")
            c_sz = float(body.get("cell_size", 8.0))
            w_th = float(body.get("wall_thickness", 1.2))
            rep = estimate_lattice_weight_savings(w, l, h, pattern=pat, cell_size=c_sz, wall_thickness=w_th)
            self.send_json({"success": True, "report": rep.to_dict()})
            return

        if clean_path == "/api/lattice/apply":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少 stem 名称"}, status=400)
                return
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                self.send_json({"error": f"未找到规格文件: {stem}_spec.json"}, status=404)
                return
            from .lattice import apply_lattice_lightweighting, estimate_lattice_weight_savings
            from .geometry import spec_to_scad
            from .core import OpenscadRenderer, AgentConfig
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                pat = body.get("pattern", "honeycomb")
                c_sz = float(body.get("cell_size", 8.0))
                w_th = float(body.get("wall_thickness", 1.2))
                new_spec, actions = apply_lattice_lightweighting(
                    spec, pattern=pat, cell_size=c_sz, wall_thickness=w_th
                )
                cand_spec.write_text(json.dumps(new_spec, indent=2, ensure_ascii=False), encoding="utf-8")
                scad_code = spec_to_scad(new_spec, fn=48, ground_z=True)
                cand_scad = out_dir / f"{stem}.scad"
                cand_scad.write_text(scad_code, encoding="utf-8")
                if body.get("recompile", True):
                    cfg = AgentConfig(output_dir=str(out_dir))
                    renderer = OpenscadRenderer(cfg)
                    renderer.export_model(cand_scad, cand_scad.with_suffix(".stl"), force=True)
                p0 = (new_spec.get("parts") or [{}])[0]
                sz = p0.get("size") or [50, 40, 10]
                w, l, h = (float(sz[0]), float(sz[1]), float(sz[2])) if len(sz) >= 3 else (50.0, 40.0, 10.0)
                rep = estimate_lattice_weight_savings(w, l, h, pattern=pat, cell_size=c_sz, wall_thickness=w_th)
                self.send_json({"success": True, "actions": actions, "report": rep.to_dict()})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        if clean_path == "/api/assembly_sop/generate":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少 stem 名称"}, status=400)
                return
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                self.send_json({"error": f"未找到模型规格: {stem}_spec.json"}, status=404)
                return
            from .assembly_sop import derive_assembly_sequence, generate_sop_guide_html
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                plan = derive_assembly_sequence(spec)
                out_sop = out_dir / f"{stem}_sop.html"
                generate_sop_guide_html(plan, out_sop)
                self.send_json({
                    "success": True,
                    "stem": stem,
                    "sop_html": out_sop.name,
                    "total_steps": plan.total_steps,
                    "plan": plan.to_dict(),
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        if clean_path == "/api/interference/check":
            stem = body.get("stem", "")
            if not stem:
                self.send_json({"error": "缺少 stem 名称"}, status=400)
                return
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                self.send_json({"error": f"未找到模型规格: {stem}_spec.json"}, status=404)
                return
            from .interference import check_assembly_interference
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                th = float(body.get("clearance_threshold", 0.30))
                report = check_assembly_interference(spec, clearance_threshold_mm=th)
                self.send_json({
                    "success": True,
                    "stem": stem,
                    "report": report.to_dict(),
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 紧固件实体代码在线生成
        if clean_path == "/api/fasteners/generate":
            part_type = str(body.get("part_type", "bolt")).lower()
            std = body.get("standard", "ISO_4762")
            size = body.get("size", "M3")
            length = float(body.get("length", 12.0))
            thread = bool(body.get("thread", True))
            try:
                from .fasteners import (
                    generate_bolt_scad,
                    generate_nut_scad,
                    generate_washer_scad,
                    generate_heat_set_insert_scad,
                )
                if "nut" in part_type:
                    scad = generate_nut_scad(size=size)
                elif "washer" in part_type:
                    scad = generate_washer_scad(size=size)
                elif "heat" in part_type or "insert" in part_type:
                    scad = generate_heat_set_insert_scad(size=size)
                else:
                    scad = generate_bolt_scad(standard=std, size=size, length=length, thread=thread)
                self.send_json({"success": True, "scad": scad, "part_type": part_type, "size": size})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 沉槽负形切削体在线生成
        if clean_path == "/api/fasteners/cutter":
            cutter_type = str(body.get("cutter_type", "counterbore")).lower()
            size = body.get("size", "M3")
            th = float(body.get("plate_thickness", 10.0))
            try:
                from .fasteners import (
                    generate_counterbore_cutter,
                    generate_countersink_cutter,
                    generate_hex_pocket_cutter,
                    generate_heat_set_hole_cutter,
                )
                if "countersink" in cutter_type or "10642" in cutter_type:
                    scad = generate_countersink_cutter(size=size, plate_thickness=th)
                elif "pocket" in cutter_type or "nut" in cutter_type:
                    side = bool(body.get("side_entry", False))
                    scad = generate_hex_pocket_cutter(size=size, pocket_depth=float(body.get("pocket_depth", 3.0)), side_entry=side)
                elif "heat" in cutter_type or "insert" in cutter_type:
                    scad = generate_heat_set_hole_cutter(size=size, plate_thickness=th)
                else:
                    inc_w = bool(body.get("include_washer", False))
                    scad = generate_counterbore_cutter(size=size, plate_thickness=th, include_washer=inc_w)
                self.send_json({"success": True, "scad": scad, "cutter_type": cutter_type, "size": size})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 规格孔特征自动升级沉头孔
        if clean_path == "/api/fasteners/auto_counterbore":
            stem = body.get("stem", "")
            spec = body.get("spec")
            if not spec and stem:
                cand_spec = out_dir / f"{stem}_spec.json"
                if cand_spec.exists():
                    try:
                        spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            if not spec:
                self.send_json({"error": "缺少 spec 或有效 stem"}, status=400)
                return
            try:
                from .fasteners import apply_auto_counterbores, generate_fasteners_bom
                from .geometry import spec_to_scad
                std = body.get("standard", "ISO_4762")
                new_spec = apply_auto_counterbores(spec, default_standard=std)
                new_scad = spec_to_scad(new_spec)
                bom = generate_fasteners_bom(new_spec)
                if stem and body.get("save", False):
                    (out_dir / f"{stem}_spec.json").write_text(json.dumps(new_spec, ensure_ascii=False, indent=2), encoding="utf-8")
                    (out_dir / f"{stem}.scad").write_text(new_scad, encoding="utf-8")
                self.send_json({
                    "success": True,
                    "spec": new_spec,
                    "scad": new_scad,
                    "bom": bom,
                })
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 工业管路与连接件代码在线生成
        if clean_path == "/api/piping/generate":
            pipe_type = str(body.get("pipe_type", "straight")).lower()
            dn = body.get("dn")
            od = float(body.get("od", 21.3))
            id_ = float(body.get("id", 17.3))
            try:
                from .piping import (
                    generate_pipe_segment,
                    generate_pipe_elbow,
                    generate_tee_junction,
                    generate_y_splitter,
                    generate_pipe_flange,
                    generate_hose_barb,
                )
                if "elbow" in pipe_type or "bend" in pipe_type:
                    ang = float(body.get("bend_angle", 90.0))
                    rad = float(body.get("bend_radius", 30.0))
                    t_len = float(body.get("tangent_length", 10.0))
                    scad = generate_pipe_elbow(bend_angle=ang, bend_radius=rad, od=od, id=id_, dn=dn, tangent_length=t_len)
                elif "tee" in pipe_type:
                    m_len = float(body.get("main_len", 70.0))
                    b_od = float(body["branch_od"]) if "branch_od" in body else None
                    b_id = float(body["branch_id"]) if "branch_id" in body else None
                    b_len = float(body.get("branch_len", 35.0))
                    ang = float(body.get("angle", 90.0))
                    scad = generate_tee_junction(main_od=od, main_id=id_, main_len=m_len, branch_od=b_od, branch_id=b_id, branch_len=b_len, angle=ang)
                elif "y" in pipe_type or "splitter" in pipe_type:
                    b_od = float(body["branch_od"]) if "branch_od" in body else None
                    b_id = float(body["branch_id"]) if "branch_id" in body else None
                    s_ang = float(body.get("split_angle", 60.0))
                    m_len = float(body.get("main_len", 25.0))
                    b_len = float(body.get("branch_len", 40.0))
                    scad = generate_y_splitter(main_od=od, main_id=id_, branch_od=b_od, branch_id=b_id, split_angle=s_ang, main_len=m_len, branch_len=b_len)
                elif "flange" in pipe_type:
                    f_od = float(body["flange_od"]) if "flange_od" in body else None
                    th = float(body.get("thickness", 8.0))
                    pcd = float(body["pcd"]) if "pcd" in body else None
                    b_sz = str(body.get("bolt_size", "M4"))
                    b_cnt = int(body.get("bolt_count", 4))
                    o_ring = bool(body.get("o_ring_groove", True))
                    scad = generate_pipe_flange(pipe_od=od, pipe_id=id_, flange_od=f_od, thickness=th, pcd=pcd, bolt_size=b_sz, bolt_count=b_cnt, o_ring_groove=o_ring)
                elif "barb" in pipe_type or "hose" in pipe_type:
                    h_id = float(body.get("hose_id", 6.0))
                    cnt = int(body.get("barb_count", 3))
                    scad = generate_hose_barb(hose_id=h_id, barb_count=cnt)
                else:
                    length = float(body.get("length", 60.0))
                    scad = generate_pipe_segment(od=od, id=id_, length=length, dn=dn)
                self.send_json({"success": True, "scad": scad, "pipe_type": pipe_type})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 多通道流体分配歧管块在线生成
        if clean_path == "/api/piping/manifold":
            try:
                from .piping import generate_manifold_block
                p_cnt = int(body.get("port_count", 4))
                m_d = float(body.get("main_port_d", 17.2))
                b_d = float(body.get("branch_port_d", 9.5))
                spacing = float(body.get("branch_spacing", 24.0))
                m_holes = bool(body.get("mounting_holes", True))
                scad = generate_manifold_block(port_count=p_cnt, main_port_d=m_d, branch_port_d=b_d, branch_spacing=spacing, mounting_holes=m_holes)
                self.send_json({"success": True, "scad": scad, "port_count": p_cnt})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        # 钣金三维建模与二维无损展开下料 (DXF/SVG)
        if clean_path == "/api/sheetmetal/unfold":
            try:
                from .sheetmetal import create_sheet_metal_preset, unfold_sheet_metal
                if "preset" in body and "flanges" not in body:
                    part = create_sheet_metal_preset(
                        preset_name=str(body.get("preset", "u_channel")),
                        width=float(body.get("width", body.get("base_width", 80.0))),
                        height=float(body.get("height", body.get("base_height", 60.0))),
                        flange_len=float(body.get("flange_len", 25.0)),
                        thickness=float(body.get("thickness", 1.5)),
                        material=str(body.get("material", "ALUMINUM_5052")),
                    )
                else:
                    part = body
                flat = unfold_sheet_metal(part)
                resp: dict[str, Any] = {
                    "success": True,
                    "flat": flat.to_dict(),
                    "scad_3d": flat.scad_3d,
                    "dxf": flat.dxf_content,
                    "svg": flat.svg_content,
                }
                stem = body.get("stem", "")
                if stem and body.get("save", False):
                    dxf_f = out_dir / f"{stem}_flat.dxf"
                    svg_f = out_dir / f"{stem}_flat.svg"
                    scad_f = out_dir / f"{stem}.scad"
                    dxf_f.write_text(flat.dxf_content, encoding="utf-8")
                    svg_f.write_text(flat.svg_content, encoding="utf-8")
                    scad_f.write_text(flat.scad_3d, encoding="utf-8")
                    resp["dxf_file"] = dxf_f.name
                    resp["svg_file"] = svg_f.name
                self.send_json(resp)
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=500)
            return

        self.send_json({"error": "未知的 API 端点"}, status=404)


# ---------------------------------------------------------------------------
# 云原生 FastAPI 微服务应用工厂 (若环境支持)
# ---------------------------------------------------------------------------

def create_app(directory: Optional[str | Path] = None) -> Any:
    """创建云原生 FastAPI 微服务实例，适配生产级 ASGI/Docker 集群部署。"""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.staticfiles import StaticFiles

        out_dir = Path(directory or "out").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        app = FastAPI(
            title="SnapSCAD Cloud-Native CAD Microservice",
            version="1.0.0",
            description="高可用、无状态 AI 参数化 CAD 逆向与 3D 打印云端微服务",
        )

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.get("/api/health")
        def health() -> dict[str, Any]:
            return {"status": "ok", "service": "SnapSCAD FastAPI Microservice", "version": "1.0.0"}

        @app.get("/api/models")
        def list_models() -> list[dict[str, Any]]:
            spec_files = sorted(out_dir.glob("*_spec.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            models = []
            for sf in spec_files:
                stem = sf.stem.replace("_spec", "")
                try:
                    spec = json.loads(sf.read_text(encoding="utf-8"))
                    models.append({
                        "name": spec.get("name", stem),
                        "stem": stem,
                        "desc": spec.get("description", ""),
                    })
                except Exception:
                    pass
            return models

        @app.get("/api/model/{stem}/gcode")
        def get_model_gcode(stem: str) -> Any:
            from fastapi.responses import PlainTextResponse, Response
            cand_gcode = out_dir / f"{stem}.gcode"
            if cand_gcode.exists():
                return PlainTextResponse(cand_gcode.read_text(encoding="utf-8"))
            return Response(content=f"模型 '{stem}' 尚未生成 G-code", status_code=404)

        @app.get("/api/printer/status")
        def get_printer_status(
            server_url: str = "http://localhost:7125",
            printer_type: str = "auto",
            api_key: str = "",
            access_code: str = "",
        ) -> dict[str, Any]:
            from .dispatcher import DispatchConfig, PrinterDispatcher
            cfg = DispatchConfig(
                server_url=server_url,
                printer_type=printer_type,
                api_key=api_key or access_code,
                access_code=access_code or api_key,
            )
            dispatcher = PrinterDispatcher(cfg)
            status = dispatcher.get_printer_status()
            return {
                "online": status.online,
                "state": status.state,
                "printer_type": status.printer_type,
                "hotend_temp": status.hotend_temp,
                "hotend_target": status.hotend_target,
                "bed_temp": status.bed_temp,
                "bed_target": status.bed_target,
                "progress": status.progress,
                "current_file": status.current_file,
                "print_duration": status.print_duration,
                "time_remaining": status.time_remaining,
                "message": status.message,
                "diagnostics": status.diagnostics,
            }

        @app.post("/api/patch")
        def patch_model_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            instruction = body.get("instruction", "")
            if not stem or not instruction:
                raise HTTPException(status_code=400, detail="缺少 stem 或 instruction")
            from .geometry import apply_patch_to_model
            try:
                _, _, updated = apply_patch_to_model(stem, instruction, out_dir=out_dir, recompile=True)
                return {"success": True, "stem": stem, "spec": updated, "stl": f"{stem}.stl"}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/export_step")
        def export_step_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            stl_file = out_dir / f"{stem}.stl"
            if not stl_file.exists():
                raise HTTPException(status_code=404, detail=f"未找到 STL 文件: {stem}.stl")
            from .interop import export_step_from_stl, export_freecad_script
            try:
                step_file = out_dir / f"{stem}.step"
                export_step_from_stl(stl_file, out_step=step_file, name=stem)
                cand_spec = out_dir / f"{stem}_spec.json"
                fc_file = out_dir / f"{stem}_freecad.py"
                if cand_spec.exists():
                    spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                    export_freecad_script(spec, out_script=fc_file)
                return {"success": True, "stem": stem, "step": step_file.name, "freecad": fc_file.name}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/model/{stem}/blueprint")
        def get_model_blueprint(stem: str) -> Any:
            from fastapi.responses import Response
            cand_svg = out_dir / f"{stem}_blueprint.svg"
            if not cand_svg.exists():
                cand_spec = out_dir / f"{stem}_spec.json"
                if cand_spec.exists():
                    from .blueprint import generate_technical_blueprint
                    spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                    generate_technical_blueprint(spec, cand_svg, preview_png=out_dir / f"{stem}.png")
            if cand_svg.exists():
                return Response(content=cand_svg.read_bytes(), media_type="image/svg+xml")
            raise HTTPException(status_code=404, detail=f"未找到工程图纸: {stem}")

        @app.post("/api/export_blueprint")
        def export_blueprint_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格文件: {stem}_spec.json")
            from .blueprint import generate_technical_blueprint
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                out_svg = out_dir / f"{stem}_blueprint.svg"
                out_png = out_dir / f"{stem}_blueprint.png"
                generate_technical_blueprint(spec, out_svg, out_png=out_png, preview_png=out_dir / f"{stem}.png")
                return {"success": True, "stem": stem, "svg": out_svg.name, "png": out_png.name if out_png.exists() else ""}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/fea/thermal_warping")
        def fea_thermal_warping_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格: {stem}_spec.json")
            from .fea import estimate_thermal_warping
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                material = body.get("material", "PLA")
                chamber = bool(body.get("chamber_heated", False))
                report = estimate_thermal_warping(spec, material=material, chamber_heated=chamber)
                return {"success": True, "stem": stem, "report": report.to_dict()}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/tolerance/compensate")
        def tolerance_compensate_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            material = body.get("material", "PLA")
            fit_type = body.get("fit_type", "normal")
            recompile = bool(body.get("recompile", True))
            from .fea import compensate_and_recompile_model
            try:
                _, _, comp_spec, actions = compensate_and_recompile_model(
                    stem, out_dir=out_dir, material=material, fit_type=fit_type, recompile=recompile
                )
                return {"success": True, "stem": stem, "actions": actions, "spec": comp_spec}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/export_multimaterial_3mf")
        def export_multimaterial_3mf_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            cand_spec = out_dir / f"{stem}_spec.json"
            cand_scad = out_dir / f"{stem}.scad"
            if not cand_spec.exists() or not cand_scad.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格或源码: {stem}")
            from .multimaterial import export_multimaterial_3mf
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                out_3mf = out_dir / f"{stem}_multimaterial.3mf"
                export_multimaterial_3mf(
                    cand_scad, spec, out_3mf,
                    extruder_map=body.get("extruder_map"),
                    color_map=body.get("color_map")
                )
                return {
                    "success": True,
                    "stem": stem,
                    "file_3mf": out_3mf.name,
                    "parts_count": len(spec.get("parts") or []),
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/lattice/estimate")
        def lattice_estimate_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            from .lattice import estimate_lattice_weight_savings
            w = float(body.get("width", 50.0))
            l = float(body.get("length", 40.0))
            h = float(body.get("height", 10.0))
            pat = body.get("pattern", "honeycomb")
            c_sz = float(body.get("cell_size", 8.0))
            w_th = float(body.get("wall_thickness", 1.2))
            rep = estimate_lattice_weight_savings(w, l, h, pattern=pat, cell_size=c_sz, wall_thickness=w_th)
            return {"success": True, "report": rep.to_dict()}

        @app.post("/api/lattice/apply")
        def lattice_apply_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格: {stem}")
            from .lattice import apply_lattice_lightweighting, estimate_lattice_weight_savings
            from .geometry import spec_to_scad
            from .core import OpenscadRenderer, AgentConfig
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                pat = body.get("pattern", "honeycomb")
                c_sz = float(body.get("cell_size", 8.0))
                w_th = float(body.get("wall_thickness", 1.2))
                new_spec, actions = apply_lattice_lightweighting(
                    spec, pattern=pat, cell_size=c_sz, wall_thickness=w_th
                )
                cand_spec.write_text(json.dumps(new_spec, indent=2, ensure_ascii=False), encoding="utf-8")
                scad_code = spec_to_scad(new_spec, fn=48, ground_z=True)
                cand_scad = out_dir / f"{stem}.scad"
                cand_scad.write_text(scad_code, encoding="utf-8")
                if body.get("recompile", True):
                    cfg = AgentConfig(output_dir=str(out_dir))
                    renderer = OpenscadRenderer(cfg)
                    renderer.export_model(cand_scad, cand_scad.with_suffix(".stl"), force=True)
                p0 = (new_spec.get("parts") or [{}])[0]
                sz = p0.get("size") or [50, 40, 10]
                w, l, h = (float(sz[0]), float(sz[1]), float(sz[2])) if len(sz) >= 3 else (50.0, 40.0, 10.0)
                rep = estimate_lattice_weight_savings(w, l, h, pattern=pat, cell_size=c_sz, wall_thickness=w_th)
                return {"success": True, "actions": actions, "report": rep.to_dict()}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/model/{stem}/assembly_sop")
        def get_model_assembly_sop(stem: str) -> dict[str, Any]:
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格: {stem}")
            from .assembly_sop import derive_assembly_sequence
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                plan = derive_assembly_sequence(spec)
                return plan.to_dict()
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/assembly_sop/generate")
        def assembly_sop_generate_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格: {stem}")
            from .assembly_sop import derive_assembly_sequence, generate_sop_guide_html
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                plan = derive_assembly_sequence(spec)
                out_sop = out_dir / f"{stem}_sop.html"
                generate_sop_guide_html(plan, out_sop)
                return {
                    "success": True,
                    "stem": stem,
                    "sop_html": out_sop.name,
                    "total_steps": plan.total_steps,
                    "plan": plan.to_dict(),
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/model/{stem}/interference")
        def get_model_interference(stem: str) -> dict[str, Any]:
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格: {stem}")
            from .interference import check_assembly_interference
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                report = check_assembly_interference(spec)
                return report.to_dict()
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/interference/check")
        def interference_check_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            if not stem:
                raise HTTPException(status_code=400, detail="缺少 stem")
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格: {stem}")
            from .interference import check_assembly_interference
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                th = float(body.get("clearance_threshold", 0.30))
                report = check_assembly_interference(spec, clearance_threshold_mm=th)
                return {"success": True, "stem": stem, "report": report.to_dict()}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/fasteners/catalog")
        def get_fasteners_catalog() -> dict[str, Any]:
            from .fasteners import FASTENER_CATALOG, FastenerStandard
            return {
                "success": True,
                "catalog": FASTENER_CATALOG,
                "standards": [e.value for e in FastenerStandard],
            }

        @app.get("/api/model/{stem}/fasteners_bom")
        def get_model_fasteners_bom(stem: str) -> dict[str, Any]:
            cand_spec = out_dir / f"{stem}_spec.json"
            if not cand_spec.exists():
                raise HTTPException(status_code=404, detail=f"未找到规格: {stem}")
            from .fasteners import generate_fasteners_bom
            try:
                spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                bom = generate_fasteners_bom(spec)
                return {"success": True, "stem": stem, "bom": bom}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/fasteners/generate")
        def generate_fastener_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            part_type = str(body.get("part_type", "bolt")).lower()
            std = body.get("standard", "ISO_4762")
            size = body.get("size", "M3")
            length = float(body.get("length", 12.0))
            thread = bool(body.get("thread", True))
            try:
                from .fasteners import (
                    generate_bolt_scad,
                    generate_nut_scad,
                    generate_washer_scad,
                    generate_heat_set_insert_scad,
                )
                if "nut" in part_type:
                    scad = generate_nut_scad(size=size)
                elif "washer" in part_type:
                    scad = generate_washer_scad(size=size)
                elif "heat" in part_type or "insert" in part_type:
                    scad = generate_heat_set_insert_scad(size=size)
                else:
                    scad = generate_bolt_scad(standard=std, size=size, length=length, thread=thread)
                return {"success": True, "scad": scad, "part_type": part_type, "size": size}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/fasteners/cutter")
        def generate_cutter_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            cutter_type = str(body.get("cutter_type", "counterbore")).lower()
            size = body.get("size", "M3")
            th = float(body.get("plate_thickness", 10.0))
            try:
                from .fasteners import (
                    generate_counterbore_cutter,
                    generate_countersink_cutter,
                    generate_hex_pocket_cutter,
                    generate_heat_set_hole_cutter,
                )
                if "countersink" in cutter_type or "10642" in cutter_type:
                    scad = generate_countersink_cutter(size=size, plate_thickness=th)
                elif "pocket" in cutter_type or "nut" in cutter_type:
                    side = bool(body.get("side_entry", False))
                    scad = generate_hex_pocket_cutter(size=size, pocket_depth=float(body.get("pocket_depth", 3.0)), side_entry=side)
                elif "heat" in cutter_type or "insert" in cutter_type:
                    scad = generate_heat_set_hole_cutter(size=size, plate_thickness=th)
                else:
                    inc_w = bool(body.get("include_washer", False))
                    scad = generate_counterbore_cutter(size=size, plate_thickness=th, include_washer=inc_w)
                return {"success": True, "scad": scad, "cutter_type": cutter_type, "size": size}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/fasteners/auto_counterbore")
        def auto_counterbore_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            stem = body.get("stem", "")
            spec = body.get("spec")
            if not spec and stem:
                cand_spec = out_dir / f"{stem}_spec.json"
                if cand_spec.exists():
                    try:
                        spec = json.loads(cand_spec.read_text(encoding="utf-8"))
                    except Exception:
                        pass
            if not spec:
                raise HTTPException(status_code=400, detail="缺少 spec 或有效 stem")
            try:
                from .fasteners import apply_auto_counterbores, generate_fasteners_bom
                from .geometry import spec_to_scad
                std = body.get("standard", "ISO_4762")
                new_spec = apply_auto_counterbores(spec, default_standard=std)
                new_scad = spec_to_scad(new_spec)
                bom = generate_fasteners_bom(new_spec)
                if stem and body.get("save", False):
                    (out_dir / f"{stem}_spec.json").write_text(json.dumps(new_spec, ensure_ascii=False, indent=2), encoding="utf-8")
                    (out_dir / f"{stem}.scad").write_text(new_scad, encoding="utf-8")
                return {
                    "success": True,
                    "spec": new_spec,
                    "scad": new_scad,
                    "bom": bom,
                }
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/piping/catalog")
        def get_piping_catalog() -> dict[str, Any]:
            from .piping import PIPE_CATALOG, PipeStandard
            return {
                "success": True,
                "catalog": PIPE_CATALOG,
                "standards": [e.value for e in PipeStandard],
            }

        @app.post("/api/piping/generate")
        def generate_piping_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            pipe_type = str(body.get("pipe_type", "straight")).lower()
            dn = body.get("dn")
            od = float(body.get("od", 21.3))
            id_ = float(body.get("id", 17.3))
            try:
                from .piping import (
                    generate_pipe_segment,
                    generate_pipe_elbow,
                    generate_tee_junction,
                    generate_y_splitter,
                    generate_pipe_flange,
                    generate_hose_barb,
                )
                if "elbow" in pipe_type or "bend" in pipe_type:
                    ang = float(body.get("bend_angle", 90.0))
                    rad = float(body.get("bend_radius", 30.0))
                    t_len = float(body.get("tangent_length", 10.0))
                    scad = generate_pipe_elbow(bend_angle=ang, bend_radius=rad, od=od, id=id_, dn=dn, tangent_length=t_len)
                elif "tee" in pipe_type:
                    m_len = float(body.get("main_len", 70.0))
                    b_od = float(body["branch_od"]) if "branch_od" in body else None
                    b_id = float(body["branch_id"]) if "branch_id" in body else None
                    b_len = float(body.get("branch_len", 35.0))
                    ang = float(body.get("angle", 90.0))
                    scad = generate_tee_junction(main_od=od, main_id=id_, main_len=m_len, branch_od=b_od, branch_id=b_id, branch_len=b_len, angle=ang)
                elif "y" in pipe_type or "splitter" in pipe_type:
                    b_od = float(body["branch_od"]) if "branch_od" in body else None
                    b_id = float(body["branch_id"]) if "branch_id" in body else None
                    s_ang = float(body.get("split_angle", 60.0))
                    m_len = float(body.get("main_len", 25.0))
                    b_len = float(body.get("branch_len", 40.0))
                    scad = generate_y_splitter(main_od=od, main_id=id_, branch_od=b_od, branch_id=b_id, split_angle=s_ang, main_len=m_len, branch_len=b_len)
                elif "flange" in pipe_type:
                    f_od = float(body["flange_od"]) if "flange_od" in body else None
                    th = float(body.get("thickness", 8.0))
                    pcd = float(body["pcd"]) if "pcd" in body else None
                    b_sz = str(body.get("bolt_size", "M4"))
                    b_cnt = int(body.get("bolt_count", 4))
                    o_ring = bool(body.get("o_ring_groove", True))
                    scad = generate_pipe_flange(pipe_od=od, pipe_id=id_, flange_od=f_od, thickness=th, pcd=pcd, bolt_size=b_sz, bolt_count=b_cnt, o_ring_groove=o_ring)
                elif "barb" in pipe_type or "hose" in pipe_type:
                    h_id = float(body.get("hose_id", 6.0))
                    cnt = int(body.get("barb_count", 3))
                    scad = generate_hose_barb(hose_id=h_id, barb_count=cnt)
                else:
                    length = float(body.get("length", 60.0))
                    scad = generate_pipe_segment(od=od, id=id_, length=length, dn=dn)
                return {"success": True, "scad": scad, "pipe_type": pipe_type}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.post("/api/piping/manifold")
        def generate_manifold_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            try:
                from .piping import generate_manifold_block
                p_cnt = int(body.get("port_count", 4))
                m_d = float(body.get("main_port_d", 17.2))
                b_d = float(body.get("branch_port_d", 9.5))
                spacing = float(body.get("branch_spacing", 24.0))
                m_holes = bool(body.get("mounting_holes", True))
                scad = generate_manifold_block(port_count=p_cnt, main_port_d=m_d, branch_port_d=b_d, branch_spacing=spacing, mounting_holes=m_holes)
                return {"success": True, "scad": scad, "port_count": p_cnt}
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        @app.get("/api/sheetmetal/materials")
        def get_sheetmetal_materials() -> dict[str, Any]:
            from .sheetmetal import SHEET_METAL_MATERIALS
            return {
                "success": True,
                "materials": SHEET_METAL_MATERIALS,
                "presets": ["l_bracket", "u_channel", "enclosure_tray"],
            }

        @app.post("/api/sheetmetal/unfold")
        def unfold_sheetmetal_endpoint(body: dict[str, Any]) -> dict[str, Any]:
            try:
                from .sheetmetal import create_sheet_metal_preset, unfold_sheet_metal
                if "preset" in body and "flanges" not in body:
                    part = create_sheet_metal_preset(
                        preset_name=str(body.get("preset", "u_channel")),
                        width=float(body.get("width", body.get("base_width", 80.0))),
                        height=float(body.get("height", body.get("base_height", 60.0))),
                        flange_len=float(body.get("flange_len", 25.0)),
                        thickness=float(body.get("thickness", 1.5)),
                        material=str(body.get("material", "ALUMINUM_5052")),
                    )
                else:
                    part = body
                flat = unfold_sheet_metal(part)
                resp: dict[str, Any] = {
                    "success": True,
                    "flat": flat.to_dict(),
                    "scad_3d": flat.scad_3d,
                    "dxf": flat.dxf_content,
                    "svg": flat.svg_content,
                }
                stem = body.get("stem", "")
                if stem and body.get("save", False):
                    dxf_f = out_dir / f"{stem}_flat.dxf"
                    svg_f = out_dir / f"{stem}_flat.svg"
                    scad_f = out_dir / f"{stem}.scad"
                    dxf_f.write_text(flat.dxf_content, encoding="utf-8")
                    svg_f.write_text(flat.svg_content, encoding="utf-8")
                    scad_f.write_text(flat.scad_3d, encoding="utf-8")
                    resp["dxf_file"] = dxf_f.name
                    resp["svg_file"] = svg_f.name
                return resp
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        # 挂载静态文件目录供前端视口调用
        app.mount("/", StaticFiles(directory=str(out_dir), html=True), name="static")
        return app
    except ImportError:
        _log.info("[server] FastAPI 未安装或未启用，微服务使用内置高性能 SnapSCADRequestHandler 运行")
        return None


# ---------------------------------------------------------------------------
# 主启动入口
# ---------------------------------------------------------------------------

def serve_assets(
    directory: str | Path,
    port: int = 8080,
    open_browser: bool = True,
    blocking: bool = True,
) -> tuple[str, str, int]:
    """启动 SnapSCAD 微服务与 3D 资产共享 Web 工作台。"""
    global _server_thread, _active_httpd

    if not blocking:
        stop_server()

    out_dir = Path(directory).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 预构建展厅画廊与协同工作台页面
    generate_gallery(out_dir)
    generate_workspace_html(out_dir)

    handler = functools.partial(SnapSCADRequestHandler, directory=str(out_dir))

    class QuietServer(HTTPServer):
        def log_message(self, format, *args):  # noqa: A002
            pass

    actual_port = port
    try:
        if is_port_in_use(port):
            actual_port = find_free_port(port + 1)
            print(f"⚠️  端口 {port} 已被占用, 自动切换到 {actual_port}")
            _log.warning("[server] 端口 %d 被占用, 自动切换到 %d", port, actual_port)
        httpd = QuietServer(("0.0.0.0", actual_port), handler)
    except OSError as e:
        _log.warning("[server] 绑定端口 %d 失败 (%s), 重新寻找空闲端口", actual_port, e)
        actual_port = find_free_port(port + 1)
        httpd = QuietServer(("0.0.0.0", actual_port), handler)

    with _lock:
        _active_httpd = httpd

    lan_ip = get_local_lan_ip()
    local_url = f"http://localhost:{actual_port}/workspace.html"
    lan_url = f"http://{lan_ip}:{actual_port}/workspace.html"

    print("\n" + "=" * 60)
    print("📡 SnapSCAD 云原生微服务与在线协同工作台已启动!")
    print(f"   💻 在线协同工作台: {local_url}")
    print(f"   📱 手机/局域网多端: {lan_url}")
    print(f"   🏛️ 3D 模型展厅画廊: http://localhost:{actual_port}/gallery.html")
    print(f"   ⚡ REST API 端点:  http://localhost:{actual_port}/api/health")
    print(f"   📁 托管本地输出目录: {out_dir}")
    print("=" * 60 + "\n")

    _log.info("[server] 微服务与在线工作台已就绪: %s | %s", local_url, lan_url)

    if open_browser:
        try:
            webbrowser.open(local_url)
        except Exception:
            pass

    if blocking:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 微服务已停止。")
            stop_server()
    else:
        def _worker() -> None:
            httpd.serve_forever()
        t = threading.Thread(target=_worker, daemon=True)
        with _lock:
            _server_thread = t
        t.start()

    return local_url, lan_url, actual_port


__all__ = [
    "serve_assets",
    "stop_server",
    "get_local_lan_ip",
    "find_free_port",
    "is_port_in_use",
    "SnapSCADRequestHandler",
    "create_app",
]
