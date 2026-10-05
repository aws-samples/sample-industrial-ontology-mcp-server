"""장기 실행 도구를 위한 heartbeat 로거.

10분 이상 걸리는 S2/S8/S9 등에서 사용자가 "죽었나?"를 판단할 수 있도록
주기적 로그를 출력한다. threading.Timer 기반 — 메인 스레드 블로킹 없음.

사용:
    with Heartbeat(stage="S8", interval=60.0):
        run_long_thing()
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class Heartbeat:
    """일정 간격으로 "[stage] 진행 중, 경과 N초" 메시지를 남기는 context manager."""

    def __init__(self, stage: str, interval: float = 60.0,
                 tool_logger: logging.Logger | None = None,
                 min_interval: float = 5.0) -> None:
        self.stage = stage
        # 프로덕션 기본 최소값 5초. 테스트에서는 min_interval을 낮출 수 있다.
        self.interval = max(min_interval, float(interval))
        self.logger = tool_logger or logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_time: float = 0.0

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            elapsed = int(time.monotonic() - self._start_time)
            self.logger.info("[%s] 진행 중 — 경과 %d초", self.stage, elapsed)

    def __enter__(self) -> Heartbeat:
        self._start_time = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"heartbeat-{self.stage}", daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1.0)
        elapsed = round(time.monotonic() - self._start_time, 1)
        level = self.logger.info if exc_type is None else self.logger.warning
        level("[%s] 완료 — 총 %.1f초%s", self.stage, elapsed,
              "" if exc_type is None else f" (예외: {exc_type.__name__})")


__all__ = ("Heartbeat",)
