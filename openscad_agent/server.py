"""SnapSCAD 局域网 3D 资产共享 Web 服务器。

提供零依赖的轻量 HTTP 资产共享服务：
- 自动解析本机局域网 IP (如 192.168.x.x)
- 支持手机/平板/团队多端设备在同一 Wi-Fi 下无线 3D 检视与模型下载
- 支持 CLI 阻塞运行 (snapscad --serve) 与 GUI 后台线程非阻塞运行
"""

from __future__ import annotations

import functools
import os
import socket
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

from .gallery import generate_gallery
from .log import get_logger as _log_module

_log = _log_module()

_server_thread: Optional[threading.Thread] = None
_active_httpd: Optional[HTTPServer] = None


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


def serve_assets(
    directory: str | Path,
    port: int = 8080,
    open_browser: bool = True,
    blocking: bool = True,
) -> tuple[str, str, int]:
    """启动 3D 资产共享 Web 服务，托管指定的输出目录。

    返回 (local_url, lan_url, actual_port)。
    """
    global _server_thread, _active_httpd
    out_dir = Path(directory).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 若未生成 gallery.html，自动预先构建
    gallery_p = out_dir / "gallery.html"
    if not gallery_p.exists():
        generate_gallery(out_dir)

    free_port = find_free_port(port)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(out_dir))

    class QuietServer(HTTPServer):
        def log_message(self, format, *args):  # noqa: A002
            pass

    httpd = QuietServer(("0.0.0.0", free_port), handler)
    _active_httpd = httpd

    lan_ip = get_local_lan_ip()
    local_url = f"http://localhost:{free_port}/gallery.html"
    lan_url = f"http://{lan_ip}:{free_port}/gallery.html"

    print("\n" + "=" * 60)
    print("📡 SnapSCAD 局域网 3D 资产服务已启动!")
    print(f"   💻 本机电脑访问: {local_url}")
    print(f"   📱 手机/平板/多端: {lan_url}")
    print(f"   📁 托管本地目录: {out_dir}")
    print("=" * 60 + "\n")

    _log.info("[server] 局域网 3D 资产共享已就绪: %s | %s", local_url, lan_url)

    if open_browser:
        try:
            webbrowser.open(local_url)
        except Exception:
            pass

    if blocking:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n🛑 资产服务已停止。")
            httpd.server_close()
    else:
        def _worker():
            httpd.serve_forever()
        _server_thread = threading.Thread(target=_worker, daemon=True)
        _server_thread.start()

    return local_url, lan_url, free_port


__all__ = ["serve_assets", "get_local_lan_ip"]
