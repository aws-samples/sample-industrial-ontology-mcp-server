"""Tests for composite PK + timestamp dedup (이슈 1 FK 댕글링).

2026-05-10 FULL_PIPELINE 회귀 분석:
  Process_Continuous_Casting 같이 composite PK = [Product_ID, Timestamp]
  인 테이블에서 `_detect_pk_value` 가 composite join 결과 (`P001_2025-09-...`)
  에 `_detect_timestamp` 를 한 번 더 추가 → `P001_2025-..._2025-...` 이중
  반복. 이 잘못된 URI 로 생성된 4320 ProcessContinuousCasting 인스턴스의
  inverse OP triple 이 A-Box 인스턴스 URI 와 매칭 안 돼 validate_kg 의
  "댕글링 참조" 에서 17,280 건 (8.1%) 실패.

수정: composite PK 구성 컬럼 중 하나라도 timestamp 계열이면 timestamp
suffix 를 추가하지 않음 (이미 PK 에 포함).
"""
from __future__ import annotations


def test_composite_pk_without_timestamp_appends_ts():
    """composite PK 에 timestamp 없음 → 기존 tie-breaker 로 ts 접미."""
    from tools.abox_generation import _detect_pk_value

    row = {
        "Item_Code": "ITM01",
        "Warehouse_Code": "WH1",
        "Timestamp": "2025-09-01 10:00:00",
    }
    pk = _detect_pk_value(
        row, "InventoryStatus",
        pk_column=["item_code", "warehouse_code"],  # no timestamp in composite
    )
    # 기존 동작 유지: composite 외부의 timestamp 가 있으면 접미
    assert pk == "ITM01_WH1_2025-09-01_10_00_00"


def test_composite_pk_with_timestamp_does_not_duplicate():
    """composite PK 에 Timestamp 포함 → ts 중복 없음."""
    from tools.abox_generation import _detect_pk_value

    row = {
        "Product_ID": "P001",
        "Equipment_ID": "EQ003",
        "Timestamp": "2025-09-01 00:00:00",
    }
    pk = _detect_pk_value(
        row, "ProcessContinuousCasting",
        pk_column=["product_id", "timestamp"],  # composite 이미 ts 포함
    )
    # Timestamp 가 한 번만
    assert pk == "P001_2025-09-01_00_00_00"
    # 이중 반복 방지 회귀 가드
    assert pk.count("2025-09-01_00_00_00") == 1


def test_composite_pk_with_timestamp_uppercase():
    """대소문자 상관없이 composite PK 의 timestamp 컬럼 감지."""
    from tools.abox_generation import _detect_pk_value

    row = {"Product_ID": "P007", "Timestamp": "2025-10-15 12:30:00"}
    pk = _detect_pk_value(
        row, "ProcessBlastFurnace",
        pk_column=["Product_ID", "Timestamp"],  # 원래 CSV 컬럼명 그대로
    )
    assert pk == "P007_2025-10-15_12_30_00"
    assert pk.count("2025-10-15") == 1


def test_composite_pk_with_measurement_datetime():
    """Timestamp 외 다른 시계열 컬럼명 (MeasurementDateTime 등) 도 감지."""
    from tools.abox_generation import _detect_pk_value

    row = {
        "Sample_ID": "S042",
        "MeasurementDateTime": "2025-09-20 14:22:33",
    }
    pk = _detect_pk_value(
        row, "ChemicalAnalysis",
        pk_column=["sample_id", "measurementdatetime"],
    )
    assert pk == "S042_2025-09-20_14_22_33"
    assert pk.count("2025-09-20") == 1


def test_single_pk_unchanged():
    """단일 str PK 는 기존 동작 그대로 (timestamp 생략)."""
    from tools.abox_generation import _detect_pk_value

    row = {"Equipment_ID": "EQ001", "Timestamp": "2025-09-01 00:00:00"}
    pk = _detect_pk_value(row, "EquipmentMaster", pk_column="equipment_id")
    assert pk == "EQ001"


def test_composite_pk_empty_component_falls_back():
    """composite 컬럼 중 빈 값 → composite 실패 → fallback 경로 (기존 동작).

    row_lower 가 빈 값 제거 후 전달되므로 composite 중간에 None 감지되고
    parts=[] 로 초기화 → 그 후 suffix 매칭 fallback. 이 동작은 기존이며
    본 수정에서 건드리지 않음. 테스트는 회귀 방어용.
    """
    from tools.abox_generation import _detect_pk_value

    row = {"Product_ID": "P001", "Timestamp": ""}
    pk = _detect_pk_value(
        row, "ProcessBlastFurnace",
        pk_column=["product_id", "timestamp"],
    )
    # composite 실패 → fallback 이 Product_ID 매칭 (또는 다른 *id/*code 컬럼)
    # 정확한 fallback 결과는 suffix 로직에 따라 다르지만, 이중 timestamp 반복은
    # 없어야 한다.
    if pk is not None:
        assert "2025" not in pk  # timestamp 가 없으므로 접미도 없어야
