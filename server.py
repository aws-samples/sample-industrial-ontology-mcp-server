import atexit
import contextlib
import faulthandler
import logging
import os
import signal
import sys
import tempfile

_ROOT_LOGGER = logging.getLogger()
_IMPORT_LOGGING_STATE = {
    "level": _ROOT_LOGGER.level,
    "disabled": _ROOT_LOGGER.disabled,
    "filters": list(_ROOT_LOGGER.filters),
    "handlers": [
        (handler, handler.level, handler.formatter, list(handler.filters))
        for handler in _ROOT_LOGGER.handlers
    ],
}

from app import build_server  # noqa: E402

logger = logging.getLogger(__name__)


def _restore_import_logging_state() -> None:
    """하위 라이브러리 import가 바꾼 root logger 상태를 원래대로 복원한다."""
    root = logging.getLogger()
    root.setLevel(_IMPORT_LOGGING_STATE["level"])
    root.disabled = _IMPORT_LOGGING_STATE["disabled"]
    root.filters[:] = _IMPORT_LOGGING_STATE["filters"]
    original_handlers = []
    for handler, level, formatter, filters in _IMPORT_LOGGING_STATE["handlers"]:
        handler.setLevel(level)
        handler.setFormatter(formatter)
        handler.filters[:] = filters
        original_handlers.append(handler)
    root.handlers[:] = original_handlers


def _install_death_diagnostics() -> None:
    """서버 종료 사유를 증거로 남긴다 (진단 계측, 동작 변경 없음).

    추론 완료 후 서버가 조용히 사라지는 현상의 정체(stdin EOF 정상종료 vs
    시그널 사망 vs OOM-kill)를 다음 재현에서 확정하기 위함. stdout 은 절대
    건드리지 않고 stderr/로그로만 출력한다 (JSON-RPC 불가침).

    - faulthandler: SIGSEGV/SIGABRT/SIGFPE 등 치명 시그널 시 네이티브 스택을
      stderr 에 덤프 → C/Rust 확장 크래시면 흔적이 남는다.
    - SIGTERM/SIGINT 핸들러: 외부 종료 시그널 수신을 로그로 기록.
    - atexit: 인터프리터가 정상 언와인드로 종료(=stdin EOF 경로)하면 호출됨.
      이게 찍히고 시그널 로그가 없으면 'EOF → exit 0' 가설 확정.
    """
    with contextlib.suppress(Exception):
        faulthandler.enable(file=sys.stderr, all_threads=True)

    def _on_signal(signum, _frame):
        logger.error("서버가 시그널 %s(%s) 수신 — 종료 경로 기록",
                     signum, signal.Signals(signum).name)
        # 기본 동작 복원 후 재전달 (정상 종료 흐름 유지)
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(_sig, _on_signal)

    @atexit.register
    def _on_exit():
        logger.info("서버 인터프리터 정상 종료(atexit) — stdin EOF/정상 언와인드 경로")


_LOG_FMT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_FILE_HANDLER_MARKER = "_ontology_agent_file_handler"


def _configure_logging() -> None:
    """루트 로거에 stderr + (선택적) 파일 핸들러를 설치한다.

    - **stdio transport 안전성**: stdout 은 MCP JSON-RPC 에 독점되므로 절대 건드리지
      않고, stderr 기반 로깅 + 선택적 FileHandler 만 사용.
    - **멱등성 (동작 보장)**: FastMCP 가 import 시점에 이미 StreamHandler 를
      설치한다. 따라서 "핸들러 있으면 return" 이 아니라 **파일 핸들러 부재 시에만
      추가**하는 방식이 맞다. 과거엔 early return 때문에 파일 로깅이 조용히
      비활성화됐었음.
    - 파일 로깅: 환경변수 ONTOLOGY_SERVER_LOG_FILE 로 경로 지정. 기본값은
      /tmp/ontology-agent-server.log (빈 문자열 → 비활성).
    """
    root = logging.getLogger()
    # 기본 레벨이 WARNING 이면 INFO 로 끌어올림 (basicConfig 를 다시 부를 필요 없이).
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # 기존 StreamHandler 에 포맷터가 없으면 우리 포맷을 씌운다.
    for h in root.handlers:
        if (
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and h.formatter is None
        ):
            h.setFormatter(logging.Formatter(_LOG_FMT))

    # 파일 핸들러: 중복 설치 방지 위해 marker attribute 로 식별.
    if any(getattr(h, _FILE_HANDLER_MARKER, False) for h in root.handlers):
        return
    configured_log_path = os.getenv("ONTOLOGY_SERVER_LOG_FILE")
    if configured_log_path is not None and not configured_log_path.strip():
        return
    log_path = configured_log_path or os.path.join(
        tempfile.gettempdir(),
        "ontology-agent-server.log",
    )
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)

        # Auto-flushing FileHandler — by default FileHandler buffers writes
        # via the underlying Python file object, so a `tail -f` reader can
        # appear "frozen" for minutes during a tight CPU-bound loop. We
        # override emit() to flush immediately after every record. The cost
        # (one extra fsync per log line) is negligible compared to the
        # observability win on multi-hour A-Box generation runs.
        class _AutoFlushFileHandler(logging.FileHandler):
            def emit(self, record: logging.LogRecord) -> None:
                super().emit(record)
                with contextlib.suppress(Exception):
                    self.flush()

        fh = _AutoFlushFileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(_LOG_FMT))
        setattr(fh, _FILE_HANDLER_MARKER, True)
        root.addHandler(fh)
        logging.getLogger(__name__).info(
            "서버 로그 파일 기록 활성화 (auto-flush): %s", log_path,
        )
    except Exception as e:
        # stderr 로는 계속 나가므로 파일 실패는 치명 아님.
        logging.getLogger(__name__).warning(
            "파일 로깅 설정 실패 (%s 로만 출력): %s", "stderr", e,
        )


if __name__ == "__main__":
    # 프로세스 전역 진단 설정은 실제 서버 실행에서만 설치한다. 모듈 import는
    # 도구 목록 검사와 테스트에도 쓰이므로 로거, 시그널, atexit을 바꾸면 안 된다.
    _configure_logging()
    _install_death_diagnostics()

mcp = build_server()

if __name__ != "__main__":
    _restore_import_logging_state()


if __name__ == "__main__":
    mcp.run()
