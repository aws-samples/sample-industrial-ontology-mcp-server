"""Heartbeat context manager 테스트."""
from __future__ import annotations

import logging
import time


def test_heartbeat_logs_completion(caplog):
    from tools.heartbeat import Heartbeat
    caplog.set_level(logging.INFO, logger="tools.heartbeat")
    with Heartbeat(stage="TEST", interval=60.0):
        pass
    msgs = [r.message for r in caplog.records]
    # 짧은 블록이면 "진행 중" 메시지는 없고 "완료"만 기록.
    assert any("완료" in m and "TEST" in m for m in msgs)


def test_heartbeat_emits_periodic_logs(caplog):
    from tools.heartbeat import Heartbeat
    caplog.set_level(logging.INFO, logger="tools.heartbeat")
    with Heartbeat(stage="PERIOD", interval=0.05, min_interval=0.0):
        time.sleep(0.18)  # ~3 intervals
    heartbeat_msgs = [r.message for r in caplog.records if "진행 중" in r.message]
    assert len(heartbeat_msgs) >= 1


def test_heartbeat_stops_after_exception(caplog):
    from tools.heartbeat import Heartbeat
    caplog.set_level(logging.INFO, logger="tools.heartbeat")
    try:
        with Heartbeat(stage="FAIL", interval=0.05, min_interval=0.0):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    completion_msgs = [r.message for r in caplog.records if "FAIL" in r.message and "완료" in r.message]
    assert completion_msgs
