"""统一日志：控制台 + 可选文件落盘，线程安全（logging 自带锁）。

设计：
- 库内各模块用 get_logger() 取同一个 logger（_log），不再散落 print。
- setup_logging 在 CLI / GUI 入口调用一次，配置控制台与文件 handler。
- 文件 handler 输出到 out/agent.log（可配置）。

避免重复 handler：所有模块共享同一个 logger 实例（_log）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

_LOG_FORMAT = "%(asctime)s  %(levelname)-7s  %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_ROOT_NAME = "openscad_agent"

# 所有模块共享同一个 logger 实例，避免各模块 get_logger 时重复加 handler 造成输出翻倍
_log: Optional[logging.Logger] = None
_configured = False


def get_logger(name: str = _ROOT_NAME) -> logging.Logger:
    """获取共享 logger。首次调用时若未 setup，加一个 console handler 兜底。"""
    global _log, _configured
    if _log is None:
        _log = logging.getLogger(_ROOT_NAME)
        _log.propagate = False
        if not _configured and not _log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
            _log.addHandler(handler)
            _log.setLevel(logging.INFO)
    return _log


def setup_logging(log_file: Optional[str | Path] = "out/agent.log",
                  level: int = logging.INFO) -> logging.Logger:
    """配置 logger：控制台 + 文件。重复调用安全（幂等）。"""
    global _log, _configured
    if _configured:
        return get_logger()
    logger = get_logger()
    logger.setLevel(level)
    # 清除兜底 handler，重建为统一配置
    for h in list(logger.handlers):
        logger.removeHandler(h)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    logger.addHandler(console)

    if log_file:
        p = Path(log_file)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(p, encoding="utf-8")
            fh.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
            logger.addHandler(fh)
            logger.info("日志文件: %s", p)
        except OSError as e:
            logger.warning("无法打开日志文件 %s: %s", p, e)

    _configured = True
    return logger
