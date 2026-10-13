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
_CONSOLE_FORMATTER_MARKER = "_ontology_agent_console_formatter"


def _configure_logging() -> None:
    """루트 로거에 stderr + (선택적) 파일 핸들러를 설치한다.

    - **stdio transport 안전성**: stdout 은 MCP JSON-RPC 에 독점되므로 절대 건드리지
      않고, stderr 기반 로깅 + 선택적 FileHandler 만 사용.
    - LOG_FORMAT=json이면 기존 StructuredLogFormatter를 stderr와 서버 파일에
      적용한다. 그 외에는 기존 텍스트 포맷을 유지한다.
    - FastMCP 생성자의 basicConfig는 핸들러가 있으면 생략된다. 파일 핸들러보다
      먼저 콘솔 출력을 보장하고, 반복 설정 시 각 핸들러를 재사용한다.
    - 파일 로깅: 환경변수 ONTOLOGY_SERVER_LOG_FILE 로 경로 지정. 기본값은
      /tmp/ontology-agent-server.log (빈 문자열 → 비활성).
    """
    root = logging.getLogger()
    json_mode = os.getenv("LOG_FORMAT", "").lower() == "json"
    if json_mode:
        from tools.common import StructuredLogFormatter

        formatter = StructuredLogFormatter()
    else:
        formatter = logging.Formatter(_LOG_FMT)

    # 기본 레벨이 WARNING 이면 INFO 로 끌어올림 (basicConfig 를 다시 부를 필요 없이).
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)

    # FastMCP가 먼저 생성된 경우 RichHandler가 있을 수 있다. JSON에 Rich 장식을
    # 섞지 않도록 콘솔만 표준 핸들러로 바꾸며 외부 FileHandler는 그대로 둔다.
    rich_types = ()
    with contextlib.suppress(ImportError):
        from rich.logging import RichHandler

        rich_types = (RichHandler,)
    console_handlers = []
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            continue
        if isinstance(h, rich_types):
            if json_mode:
                plain = logging.StreamHandler(sys.stderr)
                plain.setLevel(h.level)
                plain.filters[:] = h.filters
                root.removeHandler(h)
                root.addHandler(plain)
                h = plain
            console_handlers.append(h)
        elif isinstance(h, logging.StreamHandler):
            console_handlers.append(h)
    if not console_handlers:
        handler = logging.StreamHandler(sys.stderr)
        root.addHandler(handler)
        console_handlers.append(handler)
    for h in console_handlers:
        if json_mode:
            # MCP stdout은 프로토콜 전용으로 보존한다.
            h.setStream(sys.stderr)
        if json_mode or h.formatter is None or getattr(h, _CONSOLE_FORMATTER_MARKER, False):
            h.setFormatter(formatter)
            # 우리가 설정한 포맷은 다음 명시적 설정에서도 파일과 함께 갱신한다.
            setattr(h, _CONSOLE_FORMATTER_MARKER, True)

    # 파일 핸들러: 중복 설치 방지 위해 marker attribute 로 식별.
    file_handlers = [
        h for h in root.handlers if getattr(h, _FILE_HANDLER_MARKER, False)
    ]
    if file_handlers:
        for h in file_handlers:
            h.setFormatter(formatter)
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
        fh.setFormatter(formatter)
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
