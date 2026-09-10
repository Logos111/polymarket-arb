"""结构化日志配置（structlog）。

- 开发环境：彩色控制台输出；
- 生产环境（``PM_LOG_JSON=true``）：JSON 行，便于采集到 ELK/Loki。
"""

from __future__ import annotations

import logging

import structlog


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    """配置根日志与 structlog。进程启动时调用一次即可。"""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # 让第三方库（httpx / websockets / web3）的日志走标准 logging
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
    )
    # 噪声较大的第三方 logger 降级
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("web3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None, **bound):
    """获取一个绑定了初始上下文的 structlog logger。

    无绑定参数时返回懒代理，使其在首次真正打日志时才解析配置，
    从而尊重导入之后才调用的 ``setup_logging(level=...)``。
    """
    lg = structlog.get_logger(name)
    return lg.bind(**bound) if bound else lg
