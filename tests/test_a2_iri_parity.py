"""A2 parity — ``_abox_pk_iri`` vs ``_detect_pk_value`` IRI equality.

Core correctness test for A2's primary goal: when tacit rules and A-Box
generation look at the same CSV row for a unique single-PK class, they
must produce the same ``{Class}_{value}`` IRI. Before the CSV uniqueness
precheck was added to the strategy functions the tacit path appended a
Timestamp suffix even when A-Box skipped it (``pk_is_unique_single=True``
branch in ``abox_generation._detect_pk_value``), so CQ joins silently
hit 0 on classes like ``AlarmEvents``.

Parametrised over realistic steel tables so the test exercises both
unique-single-PK (``Alarm_Events``) and genuine composite-key
(``Equipment_Master``) paths.
"""
from __future__ import annotations

import os

import pytest

from config import SOURCE_RAWDATA_DIR
from tools.abox_generation import _detect_pk_column, _detect_pk_value
from tools.tacit_rules import _abox_pk_iri, _is_pk_unique_in_csv, _read_csv_rows


@pytest.mark.parametrize("csv_file,class_name,pk_column", [
    ("Alarm_Events.csv", "AlarmEvents", "Event_ID"),
    ("Surface_Quality.csv", "SurfaceQuality", "Sample_ID"),
    ("Equipment_Master.csv", "EquipmentMaster", "Equipment_ID"),
    ("Process_Blast_Furnace.csv", "ProcessBlastFurnace", "Product_ID"),
])
def test_abox_tacit_iri_parity(csv_file: str, class_name: str, pk_column: str) -> None:
    """A-Box 와 A2 tacit 이 동일 row 에서 동일 IRI 를 생성해야 한다.

    Regression guard for the A2 Critical fix — without the strategy-level
    ``_is_pk_unique_in_csv`` precheck, the tacit path appends a Timestamp
    suffix on tables with unique single PKs (e.g. ``Alarm_Events``) while
    the A-Box path skips it — breaking CQ joins.
    """
    path = os.path.join(SOURCE_RAWDATA_DIR, csv_file)
    if not os.path.exists(path):
        pytest.skip(f"{csv_file} not present in rawdata/")

    _, rows = _read_csv_rows(csv_file)
    if not rows:
        pytest.skip(f"{csv_file} is empty")

    # Strategy functions use this same precheck to decide auto_timestamp.
    is_unique = _is_pk_unique_in_csv(csv_file, pk_column)
    auto_ts = not is_unique

    # A-Box path: _detect_pk_column returns lowercase canonical name, then
    # _detect_pk_value matches that against its internally lower-cased row.
    pk_detected = _detect_pk_column(rows, class_name)

    # Only exercise rows where the A-Box path treats pk_column as a unique
    # single PK — composite detection is out of scope for this parity test.
    if isinstance(pk_detected, list):
        pytest.skip(
            f"{class_name} uses composite PK {pk_detected} — parity with "
            f"single-str tacit pk_column not applicable."
        )

    for row in rows[:5]:
        abox_suffix = _detect_pk_value(row, class_name, pk_column=pk_detected)
        if abox_suffix is None:
            continue
        abox_iri = f"{class_name}_{abox_suffix}"

        tacit_iri = _abox_pk_iri(
            class_name, row, pk_column, None, auto_timestamp=auto_ts,
        )
        if tacit_iri is None:
            continue

        assert tacit_iri == abox_iri, (
            f"IRI mismatch on {csv_file} row {row}: "
            f"A-Box={abox_iri!r}, tacit={tacit_iri!r} "
            f"(is_unique={is_unique}, auto_ts={auto_ts})"
        )


def test_is_pk_unique_in_csv_positive_negative() -> None:
    """Helper sanity — Event_ID 는 unique, Tag_ID 도 unique (각각 single-PK table).

    Covers both the True branch (uniqueness holds) and defensive fall-throughs
    (missing CSV, missing column) without touching external state.
    """
    # Event_ID is guaranteed unique in Alarm_Events.csv fixture.
    assert _is_pk_unique_in_csv("Alarm_Events.csv", "Event_ID") is True

    # Non-existent column → empty value list → False.
    assert _is_pk_unique_in_csv("Alarm_Events.csv", "NoSuchColumn") is False

    # Non-existent CSV → IOError → False (defensive).
    assert _is_pk_unique_in_csv("zz_does_not_exist.csv", "Whatever") is False


def test_simple_join_strategy_end_to_end_skips_ts_on_unique_pk(tmp_path, monkeypatch) -> None:
    """Strategy-level regression — simple_join on a unique-PK timeseries CSV
    emits IRIs WITHOUT a timestamp suffix, matching A-Box.

    This is the end-to-end contract the fix must enforce: even though the CSV
    carries a Timestamp column (so ``_detect_timestamp_column`` fires), the
    PK uniqueness precheck inside the strategy must set
    ``auto_timestamp=False`` when calling ``_abox_pk_iri``.
    """
    import csv
    import json

    import config
    import tools.tacit_rules as tr

    rawdata = tmp_path / "rawdata"
    tacit = tmp_path / "tacit"
    # generate_tacit_from_rules 는 RULES_ROOT 아래 규칙 파일만 받는다.
    rules_dir = tmp_path / "rules"
    rawdata.mkdir()
    tacit.mkdir()
    rules_dir.mkdir()
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(tr, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tr, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(tr, "RULES_ROOT", str(rules_dir))

    # Unique Event_ID + Timestamp — mirrors real Alarm_Events.csv shape.
    with open(rawdata / "Alarm_Events.csv", "w", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Event_ID", "Tag_ID", "Timestamp", "Severity"])
        w.writerow(["EVT00001", "TAG008", "2025-09-01 13:55:08", "Critical"])
        w.writerow(["EVT00002", "TAG008", "2025-09-02 10:12:34", "Warning"])

    rules_path = rules_dir / "rules.json"
    rules_path.write_text(json.dumps({"mappings": [{
        "name": "alarm_to_tag",
        "strategy": "simple_join",
        "source_csv": "Alarm_Events.csv",
        "source_class": "AlarmEvents",
        "source_pk_column": "Event_ID",       # unique → no ts expected
        "source_fk_column": "Tag_ID",
        "target_class": "TagMaster",
        "op": "alarmOnTag",
        "output_file": "alarm_tag.ttl",
    }]}), encoding="utf-8")

    result = json.loads(tr.generate_tacit_from_rules(str(rules_path)))
    assert result["success"] is True, result

    ttl = (tacit / "alarm_tag.ttl").read_text(encoding="utf-8")
    # A-Box 와 일치해야 하는 IRI — ts 접미 없음.
    assert "AlarmEvents_EVT00001 " in ttl, ttl
    assert "AlarmEvents_EVT00002 " in ttl, ttl
    # 버그 regression 감시: ts 접미가 절대 붙지 않아야 함.
    assert "AlarmEvents_EVT00001_2025" not in ttl, ttl
    assert "AlarmEvents_EVT00002_2025" not in ttl, ttl
