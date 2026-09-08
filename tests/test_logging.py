"""日志配置冒烟测试。"""

import structlog

from pm_arb.infra.logging import get_logger, setup_logging


def test_setup_logging_console():
    setup_logging(level="DEBUG", json_logs=False)
    log = get_logger("test")
    log.info("hello", key="value")


def test_setup_logging_json():
    setup_logging(level="INFO", json_logs=True)
    log = get_logger("test")
    log.warning("json-line", event_id=1)
    assert structlog.is_configured()
