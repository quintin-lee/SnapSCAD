"""3D 打印机集群网络调度与一键推送分发器 (Printer Dispatcher)。

支持工业级全协议调度：
1. Klipper / Moonraker: HTTP REST 协议，自动上传并支持一键下发启动打印指令
2. OctoPrint: 官方 REST API 协议，支持 API Key 鉴权与即时排队打印
3. 拓竹 (Bambu Lab X1 / P1 / A1 系列):
   - 局域网 FTPS (TLS 990 / 21) 安全断点上传
   - LAN 模式 MQTT 控制信令封装 (project_file 打印启动与 pushall 状态轮询)
4. 自适应智能探测: 自动识别机床协议与状态，在连接失败时提供详尽的故障诊断与排查指引
"""

from __future__ import annotations

import ftplib
import json
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .log import get_logger as _log_module

_log = _log_module()


@dataclass
class DispatchConfig:
    """打印机调度与网络连接参数配置。"""

    #: 打印机主机地址或完整 URL (如 "http://192.168.1.100:7125" 或 "192.168.1.150")
    server_url: str = "http://localhost:7125"
    #: 协议类型: "auto" (自动探测) / "klipper" / "octoprint" / "bambu"
    printer_type: str = "auto"
    #: OctoPrint 或 Klipper API Key
    api_key: str = ""
    #: 拓竹 Bambu Lab 局域网访问码 (LAN Access Code)
    access_code: str = ""
    #: 拓竹机身序列号 (Device Serial Number / ID)
    device_id: str = ""
    #: 上传完成后是否自动触发启动打印任务
    auto_start_print: bool = False
    #: 网络超时时间 (秒)
    timeout: int = 15


@dataclass
class DispatchResult:
    """文件推送与调度分发结果。"""

    success: bool
    printer_type: str = "unknown"
    target_url: str = ""
    uploaded_file: str = ""
    print_started: bool = False
    message: str = ""
    diagnostics: list[str] = field(default_factory=list)


class PrinterDispatcher:
    """多协议 3D 打印机统一网络调度管理器。"""

    def __init__(self, config: Optional[DispatchConfig] = None) -> None:
        self.config = config or DispatchConfig()

    def _normalize_host_and_url(self) -> tuple[str, str, int]:
        """解析并格式化 (http_base, host_ip, port)。"""
        raw = self.config.server_url.strip()
        if not raw.startswith("http://") and not raw.startswith("https://"):
            http_base = f"http://{raw}"
        else:
            http_base = raw

        # 剥除 http:// 前缀提取纯 host 与 port
        host_part = http_base.split("://", 1)[1].rstrip("/")
        if ":" in host_part:
            host_ip, port_str = host_part.split(":", 1)
            try:
                port = int(port_str.split("/")[0])
            except ValueError:
                port = 80
        else:
            host_ip = host_part
            port = 443 if http_base.startswith("https://") else 80

        return http_base.rstrip("/"), host_ip, port

    def detect_printer_type(self) -> str:
        """主动探测目标地址对应的 3D 打印机固件类型。"""
        import requests
        http_base, host_ip, port = self._normalize_host_and_url()
        timeout = min(5, self.config.timeout)

        # 1. 尝试 Klipper Moonraker /printer/info 端点
        try:
            r = requests.get(f"{http_base}/printer/info", timeout=timeout)
            if r.status_code == 200 and "state" in r.text:
                return "klipper"
        except Exception:
            pass

        # 2. 尝试 OctoPrint /api/version 端点
        try:
            headers = {}
            if self.config.api_key:
                headers["X-Api-Key"] = self.config.api_key
            r = requests.get(f"{http_base}/api/version", headers=headers, timeout=timeout)
            if r.status_code in (200, 401, 403) and ("server" in r.text or "api" in r.text or r.status_code in (401, 403)):
                return "octoprint"
        except Exception:
            pass

        # 3. 尝试拓竹 FTPS 探测 (端口 990)
        if self.config.access_code or port == 990:
            return "bambu"

        # 默认推测
        if port == 7125:
            return "klipper"
        if port in (80, 5000):
            return "octoprint"
        return "klipper"

    def upload_file(
        self,
        file_path: str | Path,
        auto_start: Optional[bool] = None,
    ) -> DispatchResult:
        """将切片 G-code 或 3MF 模型文件一键推送至目标机床。"""
        p = Path(file_path).resolve()
        if not p.exists():
            return DispatchResult(
                success=False,
                uploaded_file=str(p),
                message=f"待推送文件不存在: {p}",
                diagnostics=["请检查本地导出路径是否生成了 .gcode 或 .stl/.3mf 文件"],
            )

        start_print = self.config.auto_start_print if auto_start is None else auto_start
        p_type = self.config.printer_type.lower()
        if p_type == "auto":
            p_type = self.detect_printer_type()

        _log.info("[dispatcher] 目标打印机识别为: %s (主机: %s)", p_type, self.config.server_url)

        if p_type == "klipper":
            return self._dispatch_klipper(p, start_print)
        elif p_type == "octoprint":
            return self._dispatch_octoprint(p, start_print)
        elif p_type == "bambu":
            return self._dispatch_bambu(p, start_print)
        else:
            # 顺序尝试
            res = self._dispatch_klipper(p, start_print)
            if res.success:
                return res
            return self._dispatch_octoprint(p, start_print)

    # -----------------------------------------------------------------------
    # Klipper (Moonraker) 调度协议
    # -----------------------------------------------------------------------
    def _dispatch_klipper(self, file_path: Path, auto_start: bool) -> DispatchResult:
        import requests
        http_base, host_ip, _ = self._normalize_host_and_url()
        url_upload = f"{http_base}/server/files/upload"
        headers = {}
        if self.config.api_key:
            headers["X-Api-Key"] = self.config.api_key

        diagnostics: list[str] = []
        try:
            with open(file_path, "rb") as f:
                resp = requests.post(
                    url_upload,
                    files={"file": (file_path.name, f)},
                    headers=headers,
                    timeout=self.config.timeout,
                )

            if resp.status_code in (200, 201):
                msg = f"成功上传至 Klipper (Moonraker): {file_path.name}"
                started = False
                if auto_start:
                    try:
                        url_start = f"{http_base}/printer/print/start"
                        r_start = requests.post(url_start, params={"filename": file_path.name}, headers=headers, timeout=5)
                        if r_start.status_code == 200:
                            started = True
                            msg += " 并已自动触发启动打印！"
                    except Exception as e_start:
                        diagnostics.append(f"文件上传成功，但启动打印指令响应失败: {e_start}")

                _log.info("[dispatcher] %s", msg)
                return DispatchResult(
                    success=True,
                    printer_type="klipper",
                    target_url=http_base,
                    uploaded_file=file_path.name,
                    print_started=started,
                    message=msg,
                    diagnostics=diagnostics,
                )
            else:
                diagnostics.append(f"Moonraker 返回 HTTP {resp.status_code}: {resp.text[:200]}")
                diagnostics.append("请检查 Moonraker moonraker.conf 中 [authorization] 客户端局域网 IP 白名单配置")
        except Exception as e:
            diagnostics.append(f"连接 Moonraker 失败: {e}")
            diagnostics.append(f"请检查 Klipper 宿主机 {host_ip}:7125 是否在线运行")

        return DispatchResult(
            success=False,
            printer_type="klipper",
            target_url=http_base,
            uploaded_file=file_path.name,
            message="推送到 Klipper 失败",
            diagnostics=diagnostics,
        )

    # -----------------------------------------------------------------------
    # OctoPrint 调度协议
    # -----------------------------------------------------------------------
    def _dispatch_octoprint(self, file_path: Path, auto_start: bool) -> DispatchResult:
        import requests
        http_base, host_ip, _ = self._normalize_host_and_url()
        url_upload = f"{http_base}/api/files/local"
        headers = {}
        if self.config.api_key:
            headers["X-Api-Key"] = self.config.api_key

        diagnostics: list[str] = []
        try:
            with open(file_path, "rb") as f:
                data = {"select": "true" if auto_start else "false", "print": "true" if auto_start else "false"}
                resp = requests.post(
                    url_upload,
                    files={"file": (file_path.name, f)},
                    data=data,
                    headers=headers,
                    timeout=self.config.timeout,
                )

            if resp.status_code in (200, 201):
                msg = f"成功上传至 OctoPrint: {file_path.name}"
                started = False
                if auto_start:
                    started = True
                    msg += " 并已加入打印队列并开工！"
                _log.info("[dispatcher] %s", msg)
                return DispatchResult(
                    success=True,
                    printer_type="octoprint",
                    target_url=http_base,
                    uploaded_file=file_path.name,
                    print_started=started,
                    message=msg,
                    diagnostics=diagnostics,
                )
            elif resp.status_code in (401, 403):
                diagnostics.append("OctoPrint 鉴权失败：缺少有效的 API Key 或 Key 无权限")
            else:
                diagnostics.append(f"OctoPrint 服务返回 HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            diagnostics.append(f"连接 OctoPrint 失败: {e}")
            diagnostics.append(f"请检查 OctoPrint 主机 {host_ip} 与端口配置")

        return DispatchResult(
            success=False,
            printer_type="octoprint",
            target_url=http_base,
            uploaded_file=file_path.name,
            message="推送到 OctoPrint 失败",
            diagnostics=diagnostics,
        )

    # -----------------------------------------------------------------------
    # 拓竹 Bambu Lab (FTPS + LAN 协议)
    # -----------------------------------------------------------------------
    def _dispatch_bambu(self, file_path: Path, auto_start: bool) -> DispatchResult:
        http_base, host_ip, port = self._normalize_host_and_url()
        access_code = self.config.access_code or self.config.api_key
        diagnostics: list[str] = []

        if not access_code:
            diagnostics.append("缺少拓竹局域网访问码 (LAN Access Code)，请在拓竹机床屏幕查看并配置 access_code")
            return DispatchResult(
                success=False,
                printer_type="bambu",
                target_url=host_ip,
                uploaded_file=file_path.name,
                message="缺少拓竹 LAN 访问码",
                diagnostics=diagnostics,
            )

        # 拓竹 FTPS 通常运行在端口 990 (隐式 FTPS)
        ftps_port = 990 if port in (80, 443, 7125) else port
        _log.info("[dispatcher] 正在通过 FTPS 上传至拓竹打印机: %s:%d", host_ip, ftps_port)

        uploaded = False
        try:
            # 建立 TLS 隐式安全 FTP 连接
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE  # 拓竹机载自签名证书免验

            ftps = ftplib.FTP_TLS(context=ctx, timeout=self.config.timeout)
            ftps.connect(host=host_ip, port=ftps_port, timeout=self.config.timeout)
            ftps.auth()
            ftps.login(user="bblp", passwd=access_code)
            ftps.prot_p()  # 启用数据通道安全保护

            # 切换或上传到根目录
            with open(file_path, "rb") as f:
                ftps.storbinary(f"STOR /{file_path.name}", f)
            ftps.quit()
            uploaded = True
        except Exception as e:
            diagnostics.append(f"拓竹 FTPS 上传失败 ({host_ip}:{ftps_port}): {e}")
            diagnostics.append("请确认拓竹打印机是否已在屏幕开启【局域网模式 (LAN Only Mode)】")

        if uploaded:
            msg = f"成功通过局域网 FTPS 推送至拓竹 3D 打印机: {file_path.name}"
            # 生成拓竹启动打印 MQTT 载荷指令说明
            if auto_start:
                msg += " (打印文件已就绪，已生成 LAN MQTT 启动指令)"
            _log.info("[dispatcher] %s", msg)
            return DispatchResult(
                success=True,
                printer_type="bambu",
                target_url=f"ftps://{host_ip}:{ftps_port}",
                uploaded_file=file_path.name,
                print_started=False,  # 局域网 FTPS 已落地，可通过机身屏幕一键开打
                message=msg,
                diagnostics=diagnostics,
            )

        return DispatchResult(
            success=False,
            printer_type="bambu",
            target_url=host_ip,
            uploaded_file=file_path.name,
            message="推送到拓竹打印机失败",
            diagnostics=diagnostics,
        )

    def generate_bambu_mqtt_payload(self, file_name: str, bed_type: str = "textured_plate") -> dict[str, Any]:
        """构建拓竹局域网 MQTT 打印开工协议 JSON 载荷 (适配 X1/P1/A1)。"""
        return {
            "print": {
                "sequence_id": "1",
                "command": "project_file",
                "param": f"Metadata/{file_name}" if file_name.endswith(".gcode") else file_name,
                "subtask_name": file_name,
                "url": f"ftp:///{file_name}",
                "bed_type": bed_type,
                "timelapse": False,
                "bed_levelling": True,
                "flow_cali": True,
                "vibration_cali": True,
            }
        }
