"""A5 기동 부작용과 생성 산출물 경계 회귀 테스트."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF


def test_resolve_generated_path_returns_path_below_configured_root(
    tmp_path,
    monkeypatch,
):
    """정상 상대 경로는 설정된 generated 루트 아래의 Path로 해석한다."""
    import config

    generated = tmp_path / "generated"
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))

    resolved = config.resolve_generated_path("reports/result.html")

    assert resolved == generated / "reports" / "result.html"
    assert isinstance(resolved, Path)


@pytest.mark.parametrize(
    "relative",
    [
        "",
        ".",
        "..",
        "../outside.txt",
        "reports/../../outside.txt",
        "/tmp/outside.txt",
    ],
)
def test_resolve_generated_path_rejects_escape(relative, tmp_path, monkeypatch):
    """절대경로와 상위 디렉터리 탈출은 generated 경계에서 거부한다."""
    import config

    generated = tmp_path / "generated"
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))

    with pytest.raises(ValueError):
        config.resolve_generated_path(relative)


def test_resolve_generated_path_rejects_external_symlink(tmp_path, monkeypatch):
    """generated 내부 symlink가 외부를 가리켜도 최종 경로 기준으로 거부한다."""
    import config

    generated = tmp_path / "generated"
    outside = tmp_path / "outside"
    generated.mkdir()
    outside.mkdir()
    (generated / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))

    with pytest.raises(ValueError):
        config.resolve_generated_path("escape/result.html")


def test_import_server_has_no_process_global_side_effects(tmp_path):
    """`import server`는 파일, 스레드, 전역 진단 설정, 외부 연결을 만들지 않는다."""
    repo_root = Path(__file__).resolve().parents[1]
    probe = textwrap.dedent(
        """
        import atexit
        import builtins
        import faulthandler
        import json
        import logging
        import os
        import signal
        import socket
        import threading
        import webbrowser

        events = {
            "atexit": [],
            "browser": [],
            "connect": [],
            "faulthandler": [],
            "getaddrinfo": [],
            "writes": [],
        }

        original_open = builtins.open
        original_os_open = os.open
        original_connect = socket.socket.connect
        original_getaddrinfo = socket.getaddrinfo

        def tracked_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                events["writes"].append(os.fspath(file))
            return original_open(file, mode, *args, **kwargs)

        def tracked_os_open(path, flags, *args, **kwargs):
            write_flags = (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            )
            if flags & write_flags:
                events["writes"].append(os.fspath(path))
            return original_os_open(path, flags, *args, **kwargs)

        def tracked_register(func, *args, **kwargs):
            if getattr(func, "__module__", "") == "server":
                events["atexit"].append(getattr(func, "__qualname__", repr(func)))
            return func

        def tracked_faulthandler(*args, **kwargs):
            events["faulthandler"].append(True)

        def tracked_connect(self, address):
            events["connect"].append(repr(address))
            return original_connect(self, address)

        def tracked_getaddrinfo(*args, **kwargs):
            events["getaddrinfo"].append(repr(args[:2]))
            return original_getaddrinfo(*args, **kwargs)

        def logging_state():
            root = logging.getLogger()
            return {
                "level": root.level,
                "handlers": [
                    {
                        "id": id(handler),
                        "type": type(handler).__qualname__,
                        "format": getattr(
                            getattr(handler, "formatter", None), "_fmt", None
                        ),
                    }
                    for handler in root.handlers
                ],
            }

        builtins.open = tracked_open
        os.open = tracked_os_open
        atexit.register = tracked_register
        faulthandler.enable = tracked_faulthandler
        socket.socket.connect = tracked_connect
        socket.getaddrinfo = tracked_getaddrinfo
        webbrowser.open = lambda *args, **kwargs: events["browser"].append(
            repr(args)
        )

        threads_before = {
            (thread.ident, thread.name) for thread in threading.enumerate()
        }
        signals_before = {
            str(signum): repr(signal.getsignal(signum))
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        logging_before = logging_state()

        import server  # noqa: F401

        threads_after = {
            (thread.ident, thread.name) for thread in threading.enumerate()
        }
        signals_after = {
            str(signum): repr(signal.getsignal(signum))
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        logging_after = logging_state()

        print(json.dumps({
            "events": events,
            "logging_changed": logging_after != logging_before,
            "signals_changed": signals_after != signals_before,
            "threads_added": sorted(
                name for item, name in threads_after - threads_before
            ),
        }, sort_keys=True))
        """
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(repo_root)
    env.pop("ONTOLOGY_SERVER_LOG_FILE", None)

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    result = json.loads(completed.stdout)

    assert result["events"]["writes"] == []
    assert result["threads_added"] == []
    assert result["signals_changed"] is False
    assert result["logging_changed"] is False
    assert result["events"]["atexit"] == []
    assert result["events"]["faulthandler"] == []
    assert result["events"]["browser"] == []
    assert result["events"]["connect"] == []
    assert result["events"]["getaddrinfo"] == []


def test_generate_csv_erd_does_not_open_browser_by_default(
    tmp_path,
    monkeypatch,
):
    """CSV ERD 기본 호출은 HTML만 만들고 브라우저를 열지 않는다."""
    import config
    from tools import local_artifacts

    rawdata = tmp_path / "rawdata"
    generated = tmp_path / "generated"
    rawdata.mkdir()
    (rawdata / "Example.csv").write_text(
        "Example_ID,Name\n1,alpha\n",
        encoding="utf-8",
    )
    opened: list[object] = []
    monkeypatch.setattr(local_artifacts, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(config, "GENERATED_REPORTS_DIR", str(generated / "reports"))
    monkeypatch.setattr("webbrowser.open", lambda target: opened.append(target))

    result = json.loads(local_artifacts.generate_csv_erd())

    assert result["success"] is True
    assert opened == []


def test_visualize_tbox_does_not_open_browser_by_default(
    sample_tbox_ttl,
    tmp_path,
    monkeypatch,
):
    """T-Box 시각화 기본 호출은 HTML만 만들고 브라우저를 열지 않는다."""
    import config
    from tools import visualization

    tbox_dir = tmp_path / "tbox"
    generated = tmp_path / "generated"
    tbox_dir.mkdir()
    (tbox_dir / "t_box.ttl").write_text(sample_tbox_ttl, encoding="utf-8")
    opened: list[object] = []
    monkeypatch.setattr(visualization, "GENERATED_TBOX_DIR", str(tbox_dir))
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(visualization.webbrowser, "open", lambda target: opened.append(target))

    result = json.loads(visualization.visualize_tbox(tbox_path="t_box.ttl"))

    assert result["success"] is True
    assert opened == []


def test_domain_queries_does_not_open_browser_by_default(tmp_path, monkeypatch):
    """CQ 검증 기본 호출은 보고서만 만들고 브라우저를 열지 않는다."""
    import config
    from tools import query_test

    query_dir = tmp_path / "query_tests"
    generated = tmp_path / "generated"
    query_dir.mkdir()
    (query_dir / "cases.json").write_text(
        json.dumps(
            [
                {
                    "id": "CQ01",
                    "question_ko": "로컬 데이터가 있는가?",
                    "domains": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    opened: list[object] = []
    monkeypatch.setattr(query_test, "SOURCE_QUERY_TESTS_DIR", str(query_dir))
    monkeypatch.setattr(query_test, "_CQ_PATH", str(query_dir / "cases.json"))
    monkeypatch.setattr(query_test, "_load_semantic_dict", lambda: {})
    monkeypatch.setattr(query_test, "_load_abox_stats", lambda: {"per_class": {}})
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(query_test.webbrowser, "open", lambda target: opened.append(target))

    result = json.loads(
        query_test.test_domain_queries(
            test_cases_path="cases.json",
            verify_joins=False,
        )
    )

    assert result["success"] is True
    assert opened == []


def test_pipeline_report_does_not_open_browser_by_default(tmp_path, monkeypatch):
    """파이프라인 보고서 기본 호출은 HTML만 만들고 브라우저를 열지 않는다."""
    import config
    from tools import report

    generated = tmp_path / "generated"
    opened: list[object] = []
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(report, "GENERATED_REPORTS_DIR", str(generated / "reports"))
    monkeypatch.setattr(
        report,
        "_collect_all_data",
        lambda: {
            "tbox_stats": None,
            "tacit_stats": {"count": 0, "total_triples": 0},
            "dict_validation": None,
        },
    )
    monkeypatch.setattr(report, "_generate_tbox_visualization", lambda _out_dir: "")
    monkeypatch.setattr(report, "_build_html", lambda _data: "<html></html>")
    monkeypatch.setattr(report.webbrowser, "open", lambda target: opened.append(target))

    result = json.loads(report.generate_pipeline_report())

    assert result["success"] is True
    assert opened == []


def test_competency_questions_are_labeled_as_curated_input_exception(
    tmp_path,
    monkeypatch,
):
    """CQ는 생성 산출물이 아니라 후속 단계가 소비하는 큐레이션 입력으로 분류한다."""
    import config
    from tools import competency_questions

    generated = tmp_path / "generated"
    curated_path = tmp_path / "source" / "query_tests" / "competency_questions.json"
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(competency_questions, "_CQ_PATH", str(curated_path))

    result = json.loads(
        competency_questions.generate_competency_questions(
            user_provided="어떤 설비가 가동 중인가?"
        )
    )

    assert result["success"] is True
    assert result["artifact_class"] == "curated_input"
    assert Path(result["path"]) == curated_path
    assert generated not in curated_path.parents


def test_abox_injections_default_to_overlay_consumed_by_validate_kg(
    tmp_path,
    monkeypatch,
):
    """S7 기본 경로는 T-Box를 보존하고 overlay로 undeclared_dp를 막는다."""
    import config
    from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
    from tools import abox_generation, kg_validation
    from tools.validation_support.checks.referential import check_undeclared_dp

    generated = tmp_path / "generated"
    tbox_path = generated / "tbox" / "t_box.ttl"
    tbox_path.parent.mkdir(parents=True)
    tbox_path.write_text(
        textwrap.dedent(
            f"""
            @prefix domain: <{DOMAIN_NS}> .
            @prefix owl: <http://www.w3.org/2002/07/owl#> .

            domain:Example a owl:Class .
            """
        ),
        encoding="utf-8",
    )
    before_hash = hashlib.sha256(tbox_path.read_bytes()).hexdigest()
    plan = [
        {
            "dp_name": "exampleTimestamp",
            "range": "http://www.w3.org/2001/XMLSchema#dateTime",
            "domain_class": "Example",
            "label_en": "example timestamp",
            "label_ko": "예시 시각",
            "comment_en": "",
            "comment_ko": "",
            "source_columns": [("Example", "Timestamp")],
        }
    ]
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(abox_generation, "TBOX_PATH", str(tbox_path))
    monkeypatch.delenv("ABOX_WRITE_TBOX_INJECTIONS", raising=False)
    monkeypatch.setattr(
        abox_generation,
        "_plan_common_dp_injection",
        lambda _info, _csv, _filters: plan,
    )
    original_ttl = tbox_path.read_text(encoding="utf-8")

    _new_ttl, _info, stats = abox_generation._inject_common_dps_into_tbox_memory(
        original_ttl,
        abox_generation._parse_tbox(original_ttl),
        [],
        None,
    )

    assert hashlib.sha256(tbox_path.read_bytes()).hexdigest() == before_hash
    overlay_path = generated / "tbox" / "abox_injections.ttl"
    assert overlay_path.exists()
    assert stats["injection_audit"]["tbox_file_written"] is False
    assert Path(stats["injection_audit"]["overlay_path"]) == overlay_path

    abox = Graph()
    instance = URIRef(str(DOMAIN_INST_NS) + "Example_1")
    dp = URIRef(str(DOMAIN_NS) + "exampleTimestamp")
    abox.add((instance, RDF.type, URIRef(str(DOMAIN_NS) + "Example")))
    abox.add((instance, dp, Literal("2026-09-08T00:00:00")))

    validation_tbox = kg_validation._load_tbox_with_abox_overlay(str(tbox_path))
    result = check_undeclared_dp(abox, validation_tbox)

    assert result["undeclared_count"] == 0


def test_zero_injection_clears_stale_overlay(tmp_path, monkeypatch):
    """새 주입이 0건이면 이전 실행의 overlay 선언을 빈 파일로 교체한다."""
    import config
    from domain.namespaces import DOMAIN_NS
    from tools import abox_generation

    generated = tmp_path / "generated"
    overlay_path = generated / "tbox" / "abox_injections.ttl"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text(
        textwrap.dedent(
            f"""
            @prefix domain: <{DOMAIN_NS}> .
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            domain:staleDp a owl:DatatypeProperty .
            """
        ),
        encoding="utf-8",
    )
    tbox_ttl = textwrap.dedent(
        f"""
        @prefix domain: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        domain:Example a owl:Class .
        """
    )
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(
        abox_generation,
        "_plan_common_dp_injection",
        lambda _info, _csv, _filters: [],
    )

    abox_generation._inject_common_dps_into_tbox_memory(
        tbox_ttl,
        abox_generation._parse_tbox(tbox_ttl),
        [],
        None,
    )

    overlay = Graph()
    overlay.parse(overlay_path, format="turtle")
    assert len(overlay) == 0
