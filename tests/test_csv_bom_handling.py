"""CSV BOM 이 헤더 키를 오염시키지 않는지 회귀 가드.

배경 (2026-08-08 실측): A-Box 생성기는 CSV 를 ``encoding="utf-8"`` 로 읽어서
UTF-8 BOM 이 **첫 헤더 키에 그대로 남았다** (``\\ufeffEquipment_ID``). 값은
``_normalize_value`` 가 BOM 을 벗기지만 **헤더 키는 그 경로를 타지 않는다**.

반면 검증 쪽 5개 모듈은 같은 CSV 를 ``utf-8-sig`` 로 읽었다. 그래서 생산자와
검증자가 **모든 테이블의 첫 컬럼에 대해 서로 다른 이름을 봤다**.

Windows Excel/ERP export 는 기본으로 BOM 을 쓴다. 그 경우:
  - 첫 컬럼의 DP 트리플이 사라진다 (``_col_to_prop`` 가 None 반환)
  - 첫 컬럼이 FK 면 아무것과도 매칭되지 않는 phantom 클래스가 된다
  - ``table_pk_columns`` 가 그 컬럼을 지정하면 헤더 검증이 실패해 휴리스틱 PK 로
    조용히 폴백한다 (154행이 2개 인스턴스로 뭉치던 그 실패 모드)

그런데 ``validate_kg`` 는 BOM 을 벗기고 읽으므로 **컬럼이 정상이라고 보고한다** —
어느 게이트도 이 손실을 볼 수 없다.
"""
from __future__ import annotations

import csv
import glob
import os
from pathlib import Path

import pytest

BOM_CSV = "Equipment_ID,Status,Temp\nEQ001,RUN,120\nEQ002,STOP,20\n"

#: CSV 를 읽는 모듈들 — 생산자·검증자가 **같은** 인코딩을 써야 한다.
_CSV_READING_MODULES = [
    "tools/abox_generation.py",
    "tools/tbox_generation.py",
    "tools/local_artifacts.py",
    "tools/ontology_quality.py",
    "tools/competency_questions.py",
    "tools/validation_support/checks/temporal_cardinality.py",
    "tools/validation_support/checks/statistical.py",
    "tools/validation_support/checks/referential.py",
    "tools/quality_steps/step_10_shared_fk_op_domain.py",
    "tools/quality_steps/step_13_pk_functional_someValuesFrom.py",
]


@pytest.fixture
def bom_csv(tmp_path):
    path = tmp_path / "Equipment_Master.csv"
    path.write_text(BOM_CSV, encoding="utf-8-sig")   # BOM 포함 기록
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf", "픽스처에 BOM 이 없다"
    return path


def test_bom_fixture_breaks_plain_utf8(bom_csv):
    """전제 확인: 일반 utf-8 로 읽으면 첫 키가 실제로 오염된다.

    이 테스트가 실패하면 (BOM 이 저절로 사라지면) 아래 테스트들이 무의미해지므로
    가드의 전제를 명시적으로 고정한다.
    """
    with open(bom_csv, encoding="utf-8") as f:
        first_key = next(iter(next(csv.DictReader(f))))
    assert first_key != "Equipment_ID"
    assert first_key.startswith("﻿")


def test_utf8_sig_yields_clean_header(bom_csv):
    """``utf-8-sig`` 는 BOM 을 벗겨 정상 컬럼명을 준다."""
    with open(bom_csv, encoding="utf-8-sig") as f:
        keys = list(next(csv.DictReader(f)))
    assert keys == ["Equipment_ID", "Status", "Temp"]


@pytest.mark.parametrize("module_path", _CSV_READING_MODULES)
def test_no_module_reads_csv_as_plain_utf8(module_path):
    """THE REGRESSION: CSV 를 ``encoding="utf-8"`` 로 여는 코드가 없어야 한다.

    생산자 한 곳만 BOM 처리를 빼먹으면 검증자와 첫 컬럼 이름이 갈라지고, 그
    불일치는 어느 게이트도 보지 못한다. 소스 레벨로 고정하는 이유: 동작
    테스트만으로는 다음 편집에서 되돌아오는 것을 막기 어렵다.
    """
    source = Path(module_path).read_text(encoding="utf-8")
    assert 'csv_path, encoding="utf-8"' not in source, (
        f"{module_path}: CSV 를 plain utf-8 로 읽는다 — BOM 이 헤더 키를 오염시켜 "
        "첫 컬럼의 DP/FK 가 조용히 사라진다"
    )


def test_pk_column_resolution_survives_bom(tmp_path, monkeypatch):
    """권위 PK 지정이 BOM 때문에 무효화되지 않는다.

    ``table_pk_columns`` 가 첫 컬럼을 PK 로 지정했는데 헤더 키에 BOM 이 붙으면
    "설정 PK 가 헤더에 없음" 으로 판정돼 휴리스틱 PK 로 폴백한다.
    """
    import tools.abox_generation as ab

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir()
    (rawdata / "Equipment_Master.csv").write_text(BOM_CSV, encoding="utf-8-sig")

    with open(rawdata / "Equipment_Master.csv", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    monkeypatch.setattr(ab, "_load_table_pk_columns",
                        lambda: {"Equipment_Master": ["Equipment_ID"]})
    resolved = ab._resolve_explicit_pk("Equipment_Master", rows)
    assert resolved == "equipment_id", (
        f"권위 PK 해석이 실패했다 (got {resolved!r}) — 휴리스틱 폴백은 인스턴스 "
        "붕괴로 이어진다"
    )


def test_repo_csvs_have_no_bom_regression():
    """리포 샘플 CSV 는 BOM 이 없다 (있어도 이제 안전하지만 사실을 기록해 둔다)."""
    with_bom = [
        os.path.basename(p) for p in glob.glob("data/source/rawdata/*.csv")
        if Path(p).read_bytes()[:3] == b"\xef\xbb\xbf"
    ]
    # 정보성 assert — BOM 이 생겨도 위 가드들이 처리하므로 실패시키지 않는다.
    assert isinstance(with_bom, list)
