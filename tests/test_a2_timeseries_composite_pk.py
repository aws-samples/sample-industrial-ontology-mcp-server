"""A2 timeseries composite PK IRI automation — integration tests.

Covers the public behaviour users observe when A2 kicks in:
  1. ``_build_timeseries_prompt_section`` surfaces flagged tables.
  2. ``_warn_composite_pk_missing`` emits an INFO log when the rule omits
     a detected timestamp column.
  3. ``_warn_composite_pk_missing`` stays silent when composite_pk already
     lists the timestamp column.
  4. ``_detect_timestamp_column`` recognises a variety of conventions.
  5. ``_abox_pk_iri`` keeps existing explicit composite_pk IRIs stable
     (backward compat).
"""
from __future__ import annotations

import logging
from unittest.mock import patch


def test_suggest_tacit_rules_prompt_includes_timeseries_section():
    """시계열 테이블이 있는 CSV 요약을 넣으면 경고 섹션 생성."""
    from tools.tacit_rules import _build_timeseries_prompt_section

    csv_summary = {
        "Process_Blast_Furnace.csv": ["Product_ID", "Timestamp", "Temperature"],
        "Equipment_Master.csv": ["Equipment_ID", "Name"],   # 시계열 아님
    }
    section = _build_timeseries_prompt_section(csv_summary)
    assert "Process_Blast_Furnace" in section
    assert "Timestamp" in section
    # 비시계열 테이블은 섹션에 등장하지 않아야 함
    assert "Equipment_Master" not in section
    assert "composite PK 필수" in section


def test_suggest_tacit_rules_prompt_empty_when_no_timeseries():
    """시계열 테이블이 하나도 없으면 빈 문자열 반환 — 프롬프트 방해 없음."""
    from tools.tacit_rules import _build_timeseries_prompt_section

    section = _build_timeseries_prompt_section({
        "Master.csv": ["ID", "Name"],
        "Catalog.csv": ["SKU", "Description"],
    })
    assert section == ""


def test_warn_composite_pk_missing_logs_info(caplog):
    """generate_tacit_from_rules 실행 시 시계열 컬럼 누락 경고 (INFO 로그)."""
    from tools.tacit_rules import _warn_composite_pk_missing

    with patch("tools.tacit_rules._read_csv_rows") as mock_read:
        mock_read.return_value = (
            ["Product_ID", "Timestamp", "Value"],
            [],
        )
        rule = {
            "name": "test_rule",
            "source_csv": "Process_Blast_Furnace.csv",
            "source_pk_column": "Product_ID",
            # source_composite_pk 누락
        }
        # propagate=True 이므로 root logger 로 전파 — caplog 가 캐치
        with caplog.at_level(logging.INFO, logger="tools.tacit_rules"):
            _warn_composite_pk_missing(rule)
    assert any(
        "시계열 컬럼" in rec.getMessage() for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]


def test_warn_no_log_when_composite_includes_timestamp(caplog):
    """composite_pk 에 Timestamp 명시돼 있으면 경고 없음."""
    from tools.tacit_rules import _warn_composite_pk_missing

    with patch("tools.tacit_rules._read_csv_rows") as mock_read:
        mock_read.return_value = (
            ["Product_ID", "Timestamp", "Value"],
            [],
        )
        rule = {
            "name": "test_rule",
            "source_csv": "Process_Blast_Furnace.csv",
            "source_composite_pk": ["Product_ID", "Timestamp"],
        }
        with caplog.at_level(logging.INFO, logger="tools.tacit_rules"):
            _warn_composite_pk_missing(rule)
    assert not any(
        "시계열 컬럼" in rec.getMessage() for rec in caplog.records
    )


def test_warn_no_log_when_source_csv_missing(caplog):
    """source_csv 읽기 실패 시 조용히 반환 (strategy 단에서 명시 에러 처리)."""
    from tools.tacit_rules import _warn_composite_pk_missing

    with (
        patch("tools.tacit_rules._read_csv_rows", side_effect=FileNotFoundError),
        caplog.at_level(logging.INFO, logger="tools.tacit_rules"),
    ):
        _warn_composite_pk_missing({
            "name": "broken",
            "source_csv": "nope.csv",
        })
    assert not any("시계열 컬럼" in rec.getMessage() for rec in caplog.records)


def test_detect_timestamp_column_variations():
    """다양한 네이밍 패턴의 timestamp 컬럼 감지."""
    from tools.tacit_rules import _detect_timestamp_column

    assert _detect_timestamp_column(["Timestamp", "Value"]) == "Timestamp"
    assert _detect_timestamp_column(["MeasurementDateTime"]) == "MeasurementDateTime"
    # Order_Date — date suffix fallback
    assert _detect_timestamp_column(["Order_Date"]) is not None
    # Test_DateTime — datetime suffix fallback
    assert _detect_timestamp_column(["Test_DateTime"]) is not None
    # 비시계열
    assert _detect_timestamp_column(["Equipment_ID", "Name"]) is None
    # 빈 리스트
    assert _detect_timestamp_column([]) is None


def test_abox_iri_backward_compat_with_explicit_composite():
    """시계열 composite PK + single PK auto-timestamp 두 경로가 동일 IRI 를 생성.

    2026-05-10 fix (커밋 3eba1c2 + 6decfb9) 이후 _abox_pk_iri 는 composite PK
    에 timestamp 컬럼이 포함되면 suffix 중복을 방지 (`composite_has_ts` flag).
    결과적으로 "composite [Product_ID, Timestamp]" 경로와 "single PK +
    auto_timestamp=True" 경로는 동일 IRI 를 생성 — abox/tacit IRI 정렬 유지.

    과거 버그 시절엔 composite 경로가 ts 를 두 번 append 해 두 IRI 가 달랐다.
    이 테스트는 **수정 이후 상태** 를 고정 — IRI 가 같아야 CQ 조인이 두 경로
    어느 쪽 rule 이든 A-Box 인스턴스와 정렬된다.
    """
    from tools.tacit_rules import _abox_pk_iri

    row = {"Product_ID": "P001", "Timestamp": "2025-09-01 00:00:00"}

    # 명시 composite — Timestamp 가 composite 에 포함되어 있으므로 ts 재-append
    # 안 함 (composite_has_ts=True fast path).
    iri_explicit = _abox_pk_iri("PBF", row, None, ["Product_ID", "Timestamp"])
    assert iri_explicit == "PBF_P001_2025-09-01_00_00_00"

    # 자동 (single pk + auto_timestamp=True default) — ts 한 번만 append.
    iri_auto = _abox_pk_iri("PBF", row, "Product_ID", None)
    assert iri_auto == "PBF_P001_2025-09-01_00_00_00"

    # 두 경로가 **동일 IRI** 를 생성 — tacit 규칙을 composite 로 쓰든 single PK
    # + auto_timestamp 으로 쓰든 A-Box 인스턴스와 같은 위치에 triple 이 떨어진다.
    assert iri_explicit == iri_auto
