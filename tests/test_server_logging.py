"""MCP stdout을 보존하면서 실제 서버 핸들러가 선택한 포맷을 출력하는지 검증한다."""

from __future__ import annotations

import atexit
import faulthandler
import json
import logging
import runpy
import signal
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def server_logging(monkeypatch):
    import server

    root = logging.getLogger()
    original_handlers = tuple(root.handlers)
    monkeypatch.setenv("ONTOLOGY_SERVER_LOG_FILE", "")
    monkeypatch.delenv("LOG_FORMAT", raising=False)

    def isolate():
        # pytest는 fixture setup 뒤에도 캡처 핸들러를 추가하므로 call 단계에서 격리한다.
        monkeypatch.setattr(root, "handlers", [])
        monkeypatch.setattr(root, "filters", [])
        monkeypatch.setattr(root, "level", logging.WARNING)
        monkeypatch.setattr(root, "disabled", False)
        return server, root

    yield isolate
    for handler in root.handlers:
        if handler not in original_handlers:
            handler.close()


@pytest.mark.parametrize("log_format", [None, "", "text", "unknown", "json", "JSON"])
@pytest.mark.parametrize("file_enabled", [False, True])
def test_server_emits_selected_format_without_duplicate_handlers(
    server_logging, monkeypatch, tmp_path, capsys, log_format, file_enabled,
):
    server, root = server_logging()
    log_path = tmp_path / "server.log"
    if log_format is not None:
        monkeypatch.setenv("LOG_FORMAT", log_format)
    if file_enabled:
        monkeypatch.setenv("ONTOLOGY_SERVER_LOG_FILE", str(log_path))

    server._configure_logging()
    handlers = tuple(root.handlers)
    # 실제 FastMCP 생성자가 뒤에서 logging.basicConfig를 호출해도 유지돼야 한다.
    FastMCP("logging-constructor-test")
    server._configure_logging()
    server._configure_logging()
    assert tuple(root.handlers) == handlers
    capsys.readouterr()

    message = "서버 준비 완료"
    logging.getLogger("ontology.logging.test").info(message, extra={"stage": "S4"})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count(message) == 1

    is_json = (log_format or "").lower() == "json"
    if is_json:
        payload = json.loads(captured.err)
        assert payload["message"] == message
        assert payload["stage"] == "S4"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "ontology.logging.test"
        assert payload["ts"].endswith("+00:00")
    else:
        assert "[ontology.logging.test] INFO: " + message in captured.err

    if file_enabled:
        file_text = log_path.read_text(encoding="utf-8")
        assert file_text.count(message) == 1
        if is_json:
            records = [json.loads(line) for line in file_text.splitlines()]
            assert records[-1] == payload
        else:
            assert "[ontology.logging.test] INFO: " + message in file_text
    else:
        assert not log_path.exists()
        assert not any(isinstance(h, logging.FileHandler) for h in root.handlers)


def test_json_logging_handles_existing_fastmcp_console(server_logging, monkeypatch, capsys):
    server, root = server_logging()
    FastMCP("logging-existing-console-test")
    assert len(root.handlers) == 1
    root.handlers[0].setLevel(logging.WARNING)
    root.handlers[0].addFilter(logging.Filter("ontology.logging.existing"))
    monkeypatch.setenv("LOG_FORMAT", "json")

    server._configure_logging()
    handlers = tuple(root.handlers)
    server._configure_logging()
    assert tuple(root.handlers) == handlers
    logger = logging.getLogger("ontology.logging.existing")
    logger.info("filtered by existing level")
    logger.error("기존 콘솔 검증")
    logging.getLogger("ontology.logging.other").error("filtered by existing filter")
    captured = capsys.readouterr()

    assert captured.out == ""
    assert json.loads(captured.err)["message"] == "기존 콘솔 검증"


def test_json_console_does_not_write_to_mcp_stdout(server_logging, monkeypatch, capsys):
    server, root = server_logging()
    root.addHandler(logging.StreamHandler(sys.stdout))
    monkeypatch.setenv("LOG_FORMAT", "json")

    server._configure_logging()
    logging.getLogger("ontology.logging.stdout").warning("stderr 전용")
    captured = capsys.readouterr()

    assert captured.out == ""
    assert json.loads(captured.err)["message"] == "stderr 전용"


def test_text_logging_preserves_existing_formatter(server_logging, capsys):
    server, root = server_logging()
    handler = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter("existing: %(message)s")
    handler.setFormatter(formatter)
    root.addHandler(handler)

    server._configure_logging()
    logging.getLogger("ontology.logging.text").info("ready")
    captured = capsys.readouterr()

    assert handler.formatter is formatter
    assert captured.out == ""
    assert captured.err == "existing: ready\n"


def test_json_logging_leaves_external_file_handler_unchanged(
    server_logging, monkeypatch, tmp_path, capsys,
):
    server, root = server_logging()
    external_path = tmp_path / "external.log"
    handler = logging.FileHandler(external_path, encoding="utf-8")
    formatter = logging.Formatter("external: %(message)s")
    handler.setFormatter(formatter)
    root.addHandler(handler)
    monkeypatch.setenv("LOG_FORMAT", "json")

    server._configure_logging()
    logging.getLogger("ontology.logging.external").info("ready")
    captured = capsys.readouterr()

    assert handler in root.handlers
    assert handler.formatter is formatter
    assert external_path.read_text() == "external: ready\n"
    assert captured.out == ""
    assert json.loads(captured.err)["message"] == "ready"


@pytest.mark.parametrize("text_format", [None, "text"])
def test_format_reconfiguration_updates_console_and_file_together(
    server_logging, monkeypatch, tmp_path, capsys, text_format,
):
    server, root = server_logging()
    log_path = tmp_path / "mode-switch.log"
    monkeypatch.setenv("ONTOLOGY_SERVER_LOG_FILE", str(log_path))
    monkeypatch.setenv("LOG_FORMAT", "json")
    server._configure_logging()
    handlers = tuple(root.handlers)
    logger = logging.getLogger("ontology.logging.switch")
    logger.info("first mode")
    capsys.readouterr()

    if text_format is None:
        monkeypatch.delenv("LOG_FORMAT")
    else:
        monkeypatch.setenv("LOG_FORMAT", text_format)
    server._configure_logging()
    logger.info("second mode")
    captured = capsys.readouterr()

    assert tuple(root.handlers) == handlers
    assert captured.out == ""
    assert "[ontology.logging.switch] INFO: second mode" in captured.err
    assert log_path.read_text().splitlines()[-1] == captured.err.strip()

    monkeypatch.setenv("LOG_FORMAT", "json")
    server._configure_logging()
    logger.info("third mode")
    captured = capsys.readouterr()

    assert tuple(root.handlers) == handlers
    assert captured.out == ""
    assert json.loads(captured.err)["message"] == "third mode"
    assert json.loads(log_path.read_text().splitlines()[-1]) == json.loads(captured.err)


def test_json_exception_is_one_record_with_existing_extra_fields(
    server_logging, monkeypatch, capsys,
):
    server, _ = server_logging()
    monkeypatch.setenv("LOG_FORMAT", "json")
    server._configure_logging()
    try:
        raise ValueError("fixture failure\nsecond line")
    except ValueError:
        logging.getLogger("ontology.logging.exception").exception(
            "검증 실패: %s", "first\nsecond",
            extra={"stage": "S4", "duration_sec": 0.25, "tool": "fixture", "kg_triples": 0},
        )
    captured = capsys.readouterr()

    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    payload = json.loads(captured.err)
    assert payload["message"] == "검증 실패: first\nsecond"
    assert "ValueError: fixture failure\nsecond line" in payload["exception"]
    assert payload["stage"] == "S4"
    assert payload["duration_sec"] == 0.25
    assert payload["tool"] == "fixture"
    assert payload["kg_triples"] == 0


def test_file_setup_failure_keeps_json_stderr(
    server_logging, monkeypatch, tmp_path, capsys,
):
    server, _ = server_logging()
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("ONTOLOGY_SERVER_LOG_FILE", str(parent_file / "server.log"))
    monkeypatch.setenv("LOG_FORMAT", "json")

    server._configure_logging()
    logging.getLogger("ontology.logging.fallback").info("still available")
    captured = capsys.readouterr()

    assert captured.out == ""
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert len(records) == 2
    assert records[0]["level"] == "WARNING"
    assert "파일 로깅 설정 실패" in records[0]["message"]
    assert records[1]["message"] == "still available"


def test_entrypoint_keeps_json_logging_after_build_server(
    server_logging, monkeypatch, tmp_path, capsys,
):
    """실제 transport와 종료 hook을 대체하고 entry point의 로그 출력을 검증한다."""
    import app

    server, _ = server_logging()
    log_path = tmp_path / "entrypoint.log"
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("ONTOLOGY_SERVER_LOG_FILE", str(log_path))
    monkeypatch.setattr(atexit, "register", lambda func: func)
    monkeypatch.setattr(faulthandler, "enable", lambda **kwargs: None)
    monkeypatch.setattr(signal, "signal", lambda *args: None)

    def build_test_server():
        instance = FastMCP("entrypoint-logging-test")
        instance.run = lambda: logging.getLogger("ontology.logging.entrypoint").info(
            "transport stub reached",
        )
        return instance

    monkeypatch.setattr(app, "build_server", build_test_server)
    runpy.run_path(str(Path(server.__file__)), run_name="__main__")
    captured = capsys.readouterr()

    assert captured.out == ""
    stderr_records = [json.loads(line) for line in captured.err.splitlines()]
    file_records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert stderr_records == file_records
    assert stderr_records[-1]["message"] == "transport stub reached"
