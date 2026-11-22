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
                        "has_gcode": (out_dir / f"{stem}.gcode").exists(),
                        "has_viewer": (out_dir / f"{stem}_viewer.html").exists(),
                    })
                except Exception:
                    pass
            self.send_json(models)
            return

        # 3. 获取单模型详情
        if clean_path.startswith("/api/model/"):
            stem = clean_path.replace("/api/model/", "").strip()
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

        # 4. 路由别名跳转
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
