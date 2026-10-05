"""A2 — _abox_pk_iri auto_timestamp parameter unit tests.

Verifies that _abox_pk_iri auto-detects a timestamp column when caller passes
``pk_column`` (single) without ``composite_pk``, so tacit TTL IRIs match A-Box
IRIs on time-series tables (Process_Blast_Furnace etc).
"""
from __future__ import annotations

from tools.tacit_rules import _abox_pk_iri


def test_single_pk_without_timestamp():
    """Non-timeseries single PK — no ts append."""
    row = {"Equipment_ID": "EQ001", "Name": "pump"}
    iri = _abox_pk_iri(
        "EquipmentMaster", row,
        pk_column="Equipment_ID", composite_pk=None,
    )
    assert iri == "EquipmentMaster_EQ001"


def test_single_pk_with_timestamp_auto_appends():
    """single PK + Timestamp col → auto-append (auto_timestamp=True default)."""
    row = {"Product_ID": "P001", "Timestamp": "2025-09-01 00:00:00"}
    iri = _abox_pk_iri(
        "ProcessBlastFurnace", row,
        pk_column="Product_ID", composite_pk=None,
    )
    assert iri == "ProcessBlastFurnace_P001_2025-09-01_00_00_00"


def test_single_pk_with_timestamp_opt_out():
    """auto_timestamp=False → no ts append even if Timestamp exists."""
    row = {"Product_ID": "P001", "Timestamp": "2025-09-01 00:00:00"}
    iri = _abox_pk_iri(
        "ProcessBlastFurnace", row,
        pk_column="Product_ID", composite_pk=None,
        auto_timestamp=False,
    )
    assert iri == "ProcessBlastFurnace_P001"


def test_composite_pk_with_timestamp_no_duplicate():
    """composite_pk 에 Timestamp 포함 → ts 중복 없음 (2026-05-10 fix).

    이전에는 composite 경로가 parts join 후 다시 ts 접미 → 이중 반복 URI 를
    생성. 이는 abox_generation 의 기존 버그를 미러링한 것이었는데, abox
    쪽을 고친 이상 tacit 도 일치시켜야 inverse triple 이 댕글링 안 됨.
    """
    row = {"Product_ID": "P001", "Timestamp": "2025-09-01 00:00:00"}
    iri = _abox_pk_iri(
        "ProcessBlastFurnace", row,
        pk_column=None,
        composite_pk=["Product_ID", "Timestamp"],
    )
    assert iri == "ProcessBlastFurnace_P001_2025-09-01_00_00_00"
    # 이중 반복 방지 회귀 가드
    assert iri.count("2025-09-01_00_00_00") == 1


def test_composite_pk_without_timestamp_appends_ts():
    """composite_pk 에 Timestamp 없음 → 외부 Timestamp 있으면 tie-breaker 접미."""
    row = {
        "Item_Code": "ITM01",
        "Warehouse_Code": "WH1",
        "Timestamp": "2025-09-01 10:00:00",
    }
    iri = _abox_pk_iri(
        "InventoryStatus", row,
        pk_column=None,
        composite_pk=["Item_Code", "Warehouse_Code"],  # no timestamp in composite
    )
    assert iri == "InventoryStatus_ITM01_WH1_2025-09-01_10_00_00"


def test_steel_surface_quality_regression(tmp_path, monkeypatch):
    """Regression: Surface_Quality.csv has unique Sample_ID + Test_DateTime.

    A-Box detects Sample_ID as unique single PK → emits ``SurfaceQuality_Q0001``
    (no ts). Tacit rules MUST match — so ``_abox_pk_iri`` at the strategy-call
    level must pass ``auto_timestamp=False`` when the pk_column is verified
    unique. The strategy functions do this via the CSV uniqueness precheck.

    This test goes through ``generate_tacit_from_rules`` end-to-end to verify
    the integration, not ``_abox_pk_iri`` directly.
    """
    import csv
    import json
    rawdata = tmp_path / "rawdata"
    tacit = tmp_path / "tacit"
    rawdata.mkdir()
    tacit.mkdir()
    import config
    import tools.tacit_rules as tr
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(tr, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tr, "SOURCE_TACIT_DIR", str(tacit))

    # Unique Sample_ID + Test_DateTime — like real Surface_Quality.csv
    with open(rawdata / "Surface_Quality.csv", "w", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Sample_ID", "Product_ID", "Test_DateTime"])
        w.writerow(["Q0001", "P001", "2025-09-30 19:04:59"])
        w.writerow(["Q0002", "P001", "2025-09-01 06:49:57"])

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps({"mappings": [{
        "name": "sq",
        "strategy": "simple_join",
        "source_csv": "Surface_Quality.csv",
        "source_class": "SurfaceQuality",
        "source_pk_column": "Sample_ID",   # unique — no ts expected
        "source_fk_column": "Product_ID",
        "target_class": "ProductMaster",
        "op": "surfaceQualityOfProduct",
        "output_file": "sq.ttl",
    }]}), encoding="utf-8")

    result = json.loads(tr.generate_tacit_from_rules(str(rules_path)))
    assert result["success"] is True
    ttl = (tacit / "sq.ttl").read_text(encoding="utf-8")
    # Must match A-Box IRI (no ts because Sample_ID is unique)
    assert "SurfaceQuality_Q0001 " in ttl
    assert "SurfaceQuality_Q0001_2025" not in ttl   # no ts append


def test_single_pk_with_various_timestamp_cols():
    """Various ts column naming conventions (MeasurementDateTime etc)."""
    for ts_col in (
        "timestamp", "measurementdatetime", "occurrencedatetime",
        "testdatetime", "maintenancedate",
    ):
        row = {"Item_ID": "I001", ts_col: "2025-01-01"}
        iri = _abox_pk_iri(
            "ItemMaster", row,
            pk_column="Item_ID", composite_pk=None,
        )
        assert iri is not None, f"failed for ts column {ts_col}"
        assert "2025-01-01" in iri, f"ts not appended for {ts_col}: {iri}"
