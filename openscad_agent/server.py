"""SnapSCAD 局域网 3D 资产共享 Web 服务器。

提供零依赖的轻量 HTTP 资产共享服务：
- 自动解析本机局域网 IP (如 192.168.x.x)
- 支持手机/平板/团队多端设备在同一 Wi-Fi 下无线 3D 检视与模型下载
- 支持 CLI 阻塞运行 (snapscad --serve) 与 GUI 后台线程非阻塞运行
- 重复调用 serve_assets 时自动关闭旧 HTTPServer 并 join 旧线程, 防止端口泄漏与僵尸线程堆积
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
    """检测指定端口是否已被占用 (bind 尝试, 不发送实际请求)。

    使用 SO_EXCLUSIVEADDRUSE (Windows) / 不加 SO_REUSEADDR (POSIX) 保证 bind 成功即
    表示端口真正空闲; 被其他进程监听时 bind 会报 EADDRINUSE。返回 True 表示被占用。
    用于 CLI --serve 与 GUI 局域网分享前的端口冲突预检。
    """
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


def serve_assets(
    directory: str | Path,
    port: int = 8080,
    open_browser: bool = True,
    blocking: bool = True,
) -> tuple[str, str, int]:
    """启动 3D 资产共享 Web 服务，托管指定的输出目录。

    若已存在后台服务, 先自动关闭旧服务并释放端口, 防止线程堆积。
    返回 (local_url, lan_url, actual_port)。
    """
    global _server_thread, _active_httpd

    # 防止重复调用导致端口泄漏与僵尸线程: 先清理旧服务
    if not blocking:
        stop_server()

    out_dir = Path(directory).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # 若未生成 gallery.html，自动预先构建
    gallery_p = out_dir / "gallery.html"
    if not gallery_p.exists():
        generate_gallery(out_dir)

    # 端口冲突预检: 若指定端口已被占用, 提示并自动寻找空闲端口
    if is_port_in_use(port):
        free_port = find_free_port(port + 1)
        if free_port != port:
            print(f"⚠️  端口 {port} 已被占用, 自动切换到 {free_port}")
            _log.warning("[server] 端口 %d 被占用, 自动切换到 %d", port, free_port)
    else:
        free_port = port
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(out_dir))

    class QuietServer(HTTPServer):
        def log_message(self, format, *args):  # noqa: A002
            pass

    httpd = QuietServer(("0.0.0.0", free_port), handler)
    with _lock:
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
            stop_server()
    else:
        def _worker() -> None:
            httpd.serve_forever()
        t = threading.Thread(target=_worker, daemon=True)
        with _lock:
            _server_thread = t
        t.start()

    return local_url, lan_url, free_port


__all__ = ["serve_assets", "stop_server", "get_local_lan_ip", "find_free_port", "is_port_in_use"]
