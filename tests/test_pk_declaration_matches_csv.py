"""``table_pk_columns`` 선언이 실제 CSV 헤더와 일치하는지 검사.

**왜 필요한가**: CSV 헤더를 개명하면서 ``rules/domain/table_class_mapping.json`` 의
``table_pk_columns`` 를 함께 갱신하지 않으면 **PK 선언이 존재하지 않는 컬럼을
가리킨다.** A-Box 생성기는 경고 로그만 남기고 휴리스틱 PK 탐지로 폴백한다. 휴리스틱이
다른 컬럼이나 조합을 고르면 인스턴스 IRI 가 바뀌고 서로 다른 행이 한 인스턴스로
합쳐질 수 있지만, 생성은 성공으로 끝난다.

    예 (공개 합성 데이터 기준): ``Equipment_Master.csv`` 의 ``Equipment_ID`` 를
    ``EquipmentId`` 로 개명하고 선언은 ``Equipment_ID`` 로 남기면 선언이 헤더와
    어긋난다.

이 테스트는 선언과 헤더의 불일치를 A-Box 생성 전에 잡는다. 공개 매핑은
``table_pk_columns`` 를 선언하지 않으므로 이 파일의 테스트는 PK 를 선언한 배포에서만
동작하고, 그 밖에서는 skip 한다.
"""
from __future__ import annotations

import csv
import json
import os
import sys

import pytest

from domain.rules_paths import rules_path

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAPPING = rules_path("table_class_mapping.json")


def _load_mapping() -> dict:
    if not os.path.exists(_MAPPING):
        pytest.skip("table_class_mapping.json 없음 (도메인 미설정)")
    with open(_MAPPING, encoding="utf-8") as handle:
        return json.load(handle)


def _csv_header(table: str) -> list[str] | None:
    """rawdata 에서 테이블 CSV 헤더를 읽는다. 파일이 없으면 None."""
    from config import SOURCE_RAWDATA_DIR

    path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig", newline="", errors="replace") as handle:
        return [c.strip().upper() for c in (next(csv.reader(handle), []) or [])]


def test_declared_pk_columns_exist_in_csv():
    """선언된 모든 PK 컬럼이 해당 CSV 에 실제로 존재해야 한다."""
    mapping = _load_mapping()
    pk_columns = mapping.get("table_pk_columns") or {}
    if not pk_columns:
        pytest.skip("table_pk_columns 선언 없음")

    missing: list[str] = []
    checked = 0
    for table, cols in pk_columns.items():
        header = _csv_header(table)
        if header is None:
            continue                      # CSV 미배치 — 이 테스트의 관심사 아님
        checked += 1
        for col in cols:
            if col.strip().upper() not in header:
                missing.append(f"{table}.{col}")

    if checked == 0:
        pytest.skip("검사 가능한 CSV 없음")
    assert not missing, (
        "PK 선언이 CSV 에 없는 컬럼을 가리킨다. 개명 후 동기화 누락으로 보인다. "
        f"A-Box 생성기가 휴리스틱 PK 로 폴백해 IRI 가 바뀔 수 있다: {missing}"
    )


#: PK 선언이 CSV 에서 유일하지 않지만 **의도적으로 유지** 하는 테이블. 값은 사유.
#: 권위 PK 는 timestamp 접미 없이 IRI 를 만들므로, 값이 겹치는 행은 한 인스턴스로
#: 합쳐진다. 그 결과를 받아들이기로 한 테이블만 등록한다.
#:
#: 키는 ``table_pk_columns`` 의 테이블명과 정확히 일치해야 면제가 걸린다. 공개 매핑은
#: PK 를 선언하지 않으므로 비어 있다. 자기 배포에서 면제가 필요하면 그 배포 매핑의
#: 테이블명과 사유를 등록한다.
_KNOWN_NON_UNIQUE_PK: dict[str, str] = {}


def test_declared_pk_columns_are_unique_in_csv():
    """선언된 PK 는 CSV 안에서 실제로 유일해야 한다 (단일 컬럼 선언 기준).

    유일하지 않으면 A-Box 가 값이 겹치는 여러 행을 한 인스턴스로 합친다. 복합키
    선언은 조합 유일성을 본다.

    ``_KNOWN_NON_UNIQUE_PK`` 에 사유와 함께 등록된 테이블은 제외한다 — 새로운
    불일치만 잡는 것이 목적이다.
    """
    from config import SOURCE_RAWDATA_DIR

    mapping = _load_mapping()
    pk_columns = mapping.get("table_pk_columns") or {}
    if not pk_columns:
        pytest.skip("table_pk_columns 선언 없음")

    violations: list[str] = []
    checked = 0
    for table, cols in pk_columns.items():
        if table in _KNOWN_NON_UNIQUE_PK:
            continue
        path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="", errors="replace") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            upper = [c.strip().upper() for c in fields]
            targets = [c.strip().upper() for c in cols]
            if any(t not in upper for t in targets):
                continue                  # 위 테스트가 이미 잡는다
            idx = {t: fields[upper.index(t)] for t in targets}
            seen: set[tuple] = set()
            rows = 0
            for row in reader:
                rows += 1
                key = tuple((row.get(o) or "").strip() for o in idx.values())
                seen.add(key)
        checked += 1
        if rows and len(seen) < rows:
            violations.append(
                f"{table} {cols}: 고유 {len(seen):,} < 행 {rows:,}"
            )

    if checked == 0:
        pytest.skip("검사 가능한 CSV 없음")
    assert not violations, (
        "PK 선언이 CSV 에서 유일하지 않다. 값이 겹치는 행이 한 인스턴스로 "
        f"합쳐진다: {violations}"
    )


def test_rename_map_targets_are_current_headers():
    """``_column_rename_map.json`` 의 새 이름이 현재 CSV 헤더에 존재해야 한다.

    개명 감사 기록과 실제 파일이 어긋나면, 무엇이 적용됐는지 추적할 수 없다.
    """
    from config import SOURCE_RAWDATA_DIR

    audit_path = os.path.join(SOURCE_RAWDATA_DIR, "_column_rename_map.json")
    if not os.path.exists(audit_path):
        pytest.skip("_column_rename_map.json 없음 (개명 미수행 도메인)")
    with open(audit_path, encoding="utf-8") as handle:
        audit = json.load(handle)

    problems: list[str] = []
    checked = 0
    for fname, renames in audit.items():
        path = os.path.join(SOURCE_RAWDATA_DIR, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="", errors="replace") as handle:
            header = {c.strip().upper() for c in (next(csv.reader(handle), []) or [])}
        checked += 1
        for old, new in renames.items():
            if new.strip().upper() not in header:
                problems.append(f"{fname}: 새 이름 {new} 없음")
            if old.strip().upper() in header:
                problems.append(f"{fname}: 옛 이름 {old} 잔존 (개명 미적용)")

    if checked == 0:
        pytest.skip("검사 가능한 CSV 없음")
    assert not problems, f"개명 감사 기록이 실제 CSV 와 불일치: {problems}"
