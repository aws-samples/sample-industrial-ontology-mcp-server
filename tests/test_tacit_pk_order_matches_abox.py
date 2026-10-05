"""tacit ``source_composite_pk`` 순서가 A-Box IRI 와 어긋나 평행 인스턴스를 만들었다.

2026-08-25 실측. ``rule_blast_furnace_to_equipment`` 등 4개 규칙이
``source_composite_pk: ["Timestamp", "Equipment_ID"]`` 였는데 A-Box 생성기는 같은
테이블의 IRI 를 ``Product_ID`` + ``Timestamp`` 순서로 만든다. 결과:

    A-Box   ProcessBlastFurnace_P004_2025-09-30_23_50_00      (rdf:type 있음)
    tacit   ProcessBlastFurnace_2025-09-01_00_00_00_EQ001     (rdf:type 없음)

같은 CSV 행에 IRI 두 벌 → ``ProcessBlastFurnace_*`` 주어가 **8,640 = CSV 4,320 의
정확히 2.00배**.

## 왜 게이트가 통과했나

- ``클래스별 인스턴스 수`` 는 ``rdf:type`` 이 있는 것만 센다 → 4,320 (정상) 로 통과.
  tacit 쪽 IRI 는 타입 선언이 없어 분모에도 분자에도 안 들어갔다.
- ``step_22e`` 는 ``hasBlastFurnaceEquipment`` 와 ``blastFurnaceUsesEquipment`` 를
  같은 ``(domain, range)`` 중복으로 보고 병합 후보로 올렸다. 그런데 두 OP 의 주어
  교집합이 **0** 이라(서로 다른 IRI 규약) 병합하면 데이터가 뒤섞인다 —
  ``prune_mode`` 를 켰다면 17,480 트리플이 잘못 이동했을 것이다.

## 교정 후

주어 8,640 → **4,320 (1.00배)**, 두 OP 의 주어 교집합 0 → **4,320 (완전 일치)**.
즉 IRI 규약을 맞추자 "중복 OP" 가 비로소 **진짜 중복** 이 되어 병합이 안전해졌다.

교정하지 않은 2건은 이미 A-Box 와 일치했다 (``Meter_ID``+``Timestamp``,
``Item_Code``+``Warehouse_Code``) — 일률 변경이 아니라 A-Box 실측 대조로 판정했다.

## 2026-08-30 정정 — ``Meter_ID`` 는 A-Box 와 **일치하지 않았다**

위 문단의 "이미 일치했다" 는 ``rule_electrical_consumption_to_equipment`` 에 대해
**틀렸다**. 실측하지 않고 기대값으로 고정했고, 이 테스트가 그 틀린 값을 지켰다.

배포 A-Box 실측 (``ElectricalConsumption_*`` 주어 형태별)::

    EQ 형태  734  ← 이 중 720개가 ``a steel:ElectricalConsumption`` (타입 있음)
    MT 형태  720  ← 타입 **없음** (tacit 이 만든 유령)

``_detect_pk_column(rows, "ElectricalConsumption")`` 도 ``[equipment_id, timestamp]``
를 반환한다. 즉 A-Box 는 ``Equipment_ID`` 를 골랐고 tacit 만 ``Meter_ID`` 였다.

**왜 uniqueness 선검사(A2)로 잡히지 않았나**: ``Meter_ID``(5종) 와
``Equipment_ID``(14종) 둘 다 ``+Timestamp`` 조합에서 720행 전체 unique 다. 두
조합이 모두 유효하므로 "어느 쪽이 A-Box 의 선택인가" 는 uniqueness 로 판정할 수
없다 — ``_detect_pk_column`` 을 직접 물어봐야 한다. 그것이
``tacit_rules._warn_pk_diverges_from_abox`` 가 하는 일이다.

피해: 유령 720개 + ``hasElectricalConsumptionEquipment`` 1,440건 중 절반이 유령
주어 → CQ05 가 정확히 2배로 답한다.

교훈: **기대값을 손으로 적으면 그 값이 틀렸을 때 테스트가 결함을 지킨다.** 아래
``_EXPECTED_PK_ORDER`` 는 이제 A-Box 감지기와 대조해 검증한다.
"""
from __future__ import annotations

import json
import re

import pytest

from domain.rules_paths import rules_path

#: A-Box IRI 가 ``<PK1>_<PK2>`` 순서로 만들어지는 것이 확인된 규칙 → 기대 순서.
#: 값은 A-Box 산출물 실측으로 정한다 (추측 금지).
#:
#: ``rule_electrical_consumption_to_equipment`` 는 2026-08-30 에
#: ``[Meter_ID, Timestamp]`` → ``[Equipment_ID, Timestamp]`` 로 정정됐다. 이전 값은
#: 실측 없이 적힌 것이었고 A-Box 는 ``Equipment_ID`` 를 골랐다 (모듈 docstring 참조).
_EXPECTED_PK_ORDER = {
    "rule_blast_furnace_to_equipment": ["Product_ID", "Timestamp"],
    "rule_blast_furnace_to_product": ["Product_ID", "Timestamp"],
    "rule_continuous_casting_to_equipment": ["Product_ID", "Timestamp"],
    "rule_continuous_casting_to_product": ["Product_ID", "Timestamp"],
    "rule_electrical_consumption_to_equipment": ["Equipment_ID", "Timestamp"],
    "rule_inventory_to_supplier_via_item_map": ["Item_Code", "Warehouse_Code"],
}


def _mappings() -> list[dict]:
    with open(rules_path("tacit_rules.json"), encoding="utf-8") as fh:
        return json.load(fh).get("mappings") or []


# ──────────────────────────────────────────────────────────────────
# 1. 설정 축 — PK 순서가 A-Box 규약과 같다 (THE REGRESSION)
# ──────────────────────────────────────────────────────────────────

def test_composite_pk_order_matches_abox_convention():
    """``source_composite_pk`` 가 A-Box IRI 순서와 일치한다.

    어긋나면 같은 CSV 행에 IRI 두 벌이 생기고, ``rdf:type`` 이 없는 쪽은 인스턴스
    카운트 게이트에 **보이지 않는다**.
    """
    by_name = {m.get("name"): m for m in _mappings()}
    wrong = []
    for name, expected in _EXPECTED_PK_ORDER.items():
        m = by_name.get(name)
        if m is None:
            continue  # 규칙이 제거됐으면 이 테스트의 범위 밖
        actual = m.get("source_composite_pk")
        if actual != expected:
            wrong.append(f"{name}: {actual} != {expected}")
    assert not wrong, (
        "tacit PK 순서가 A-Box IRI 규약과 어긋난다 (평행 인스턴스 발생):\n  "
        + "\n  ".join(wrong)
    )


def test_no_rule_uses_timestamp_first_for_product_keyed_tables():
    """Product_ID 를 가진 공정 테이블에 ``Timestamp`` 선행 PK 를 쓰지 않는다.

    이 방향을 따로 두는 이유: 위 테스트는 정확한 리스트를 요구하지만, 새 규칙이
    추가될 때 같은 함정(Timestamp 선행)에 빠지는 것을 이름 목록 없이도 잡아야 한다.
    """
    offenders = []
    for m in _mappings():
        pk = m.get("source_composite_pk") or []
        csv = (m.get("source_csv") or "").lower()
        if not pk or len(pk) < 2:
            continue
        # Product_ID 를 PK 구성에 쓰는 공정 테이블에서 Timestamp 가 첫 자리면 의심
        if pk[0] == "Timestamp" and ("process_" in csv):
            offenders.append(f"{m.get('name')}: {pk} ({m.get('source_csv')})")
    assert not offenders, (
        "공정 테이블에 Timestamp 선행 PK — A-Box 는 보통 업무키(Product_ID) 를 앞에 "
        "둔다. A-Box 산출물의 IRI 를 실측해 순서를 맞추라:\n  " + "\n  ".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────
# 2. 산출물 축 — 평행 인스턴스가 없다
# ──────────────────────────────────────────────────────────────────

_ABOX = "data/generated/abox/a_box.ttl"
_INSTANCE_NS = "http://example.com/steel-ontology/instances#"

#: (클래스, CSV 행수). 행수는 instance_completeness 실측값.
_PARALLEL_RISK_CLASSES = [
    ("ProcessBlastFurnace", 4320),
    ("ProcessContinuousCasting", 4320),
]


def _abox_graph():
    import os
    if not os.path.exists(_ABOX):
        pytest.skip("A-Box 산출물 없음")
    from rdflib import Graph
    g = Graph()
    g.parse(_ABOX, format="turtle")
    return g


@pytest.mark.parametrize(("cls", "csv_rows"), _PARALLEL_RISK_CLASSES)
def test_no_parallel_instance_iris(cls, csv_rows):
    """한 CSV 행이 IRI 한 개만 갖는다 — 주어 수가 행수를 넘지 않는다.

    이 단정이 깨지면 tacit 과 A-Box 가 서로 다른 IRI 를 만들고 있다. 배수가 정확히
    2.00 이면 두 규약이 공존하는 전형적 신호다.
    """
    from rdflib import URIRef
    g = _abox_graph()
    prefix = _INSTANCE_NS + cls + "_"
    subjects = {
        str(s) for s, _, _ in g
        if isinstance(s, URIRef) and str(s).startswith(prefix)
    }
    ratio = len(subjects) / csv_rows
    assert ratio <= 1.01, (
        f"{cls}: 주어 {len(subjects)} / CSV {csv_rows} = {ratio:.2f}배 — "
        "IRI 규약이 두 벌이다 (tacit source_composite_pk 순서 확인)"
    )


@pytest.mark.parametrize(("cls", "csv_rows"), _PARALLEL_RISK_CLASSES)
def test_every_subject_is_typed(cls, csv_rows):
    """모든 주어가 ``rdf:type`` 을 갖는다.

    타입 없는 주어는 인스턴스 카운트 게이트에 **보이지 않으면서** OP 트리플의
    주어로는 존재한다 — 그것이 이 결함이 오래 숨어 있던 이유다.
    """
    from rdflib import RDF, URIRef

    from domain.namespaces import DOMAIN_NS
    g = _abox_graph()
    prefix = _INSTANCE_NS + cls + "_"
    subjects = {
        str(s) for s, _, _ in g
        if isinstance(s, URIRef) and str(s).startswith(prefix)
    }
    typed = {str(s) for s in g.subjects(RDF.type, URIRef(str(DOMAIN_NS) + cls))}
    untyped = subjects - typed
    assert not untyped, (
        f"{cls}: rdf:type 없는 주어 {len(untyped)}개 — 인스턴스 카운트 게이트가 "
        f"보지 못한다. 샘플: {sorted(untyped)[:3]}"
    )


def test_duplicate_op_pairs_share_subjects():
    """같은 ``(domain,range)`` 를 잇는 두 OP 는 **같은 주어 집합** 을 가져야 한다.

    ``step_22e`` 는 이런 쌍을 병합 후보로 올린다. 주어가 다르면(서로 다른 IRI 규약)
    병합이 데이터를 뒤섞는다 — 실측 2026-08-25: 교집합 0 인 상태로 17,480 트리플이
    이동 예정이었다. 교정 후 교집합 4,320 (완전 일치).
    """
    from rdflib import URIRef

    from domain.namespaces import DOMAIN_NS
    ns = str(DOMAIN_NS)
    g = _abox_graph()
    pairs = [
        ("hasBlastFurnaceEquipment", "blastFurnaceUsesEquipment"),
        ("hasBlastFurnaceProduct", "blastFurnaceHasProduct"),
        ("hasContinuousCastingEquipment", "continuousCastingUsesEquipment"),
    ]
    mismatched = []
    for a, b in pairs:
        sa = {s for s, _ in g.subject_objects(URIRef(ns + a))}
        sb = {s for s, _ in g.subject_objects(URIRef(ns + b))}
        if not sa or not sb:
            continue  # 한쪽이 없으면 중복이 아니다
        if not (sa & sb):
            mismatched.append(f"{a} ∩ {b} = 0 (각 {len(sa)},{len(sb)})")
    assert not mismatched, (
        "중복 OP 쌍의 주어가 겹치지 않는다 — IRI 규약이 두 벌이고, 이 상태로 "
        "TBOX_DUP_OP_PRUNE 를 켜면 데이터가 뒤섞인다:\n  " + "\n  ".join(mismatched)
    )


# ──────────────────────────────────────────────────────────────────
# 3. 과잉 교정 방지 — 원래 맞던 것을 건드리지 않았다
# ──────────────────────────────────────────────────────────────────

def test_already_correct_rules_untouched():
    """A-Box 와 이미 일치했던 규칙의 PK 는 그대로다.

    "전부 Product_ID 앞으로" 같은 일률 변경을 막는다 — InventoryStatus 는
    ``Item_Code`` 가 앞이다.
    """
    by_name = {m.get("name"): m for m in _mappings()}
    for name, expected in (
        ("rule_inventory_to_supplier_via_item_map", ["Item_Code", "Warehouse_Code"]),
    ):
        m = by_name.get(name)
        if m is None:
            continue
        assert m.get("source_composite_pk") == expected, (
            f"{name} 의 PK 가 일률 변경됐다: {m.get('source_composite_pk')}"
        )


def test_declared_pk_matches_abox_detector_for_every_rule():
    """모든 규칙의 선언 PK 가 **A-Box 감지기의 선택**과 같은가.

    이것이 정본 검사다. 위 ``_EXPECTED_PK_ORDER`` 는 손으로 적은 값이라 틀릴 수
    있고, 실제로 ``rule_electrical_consumption_to_equipment`` 에서 틀렸다
    (``Meter_ID`` 로 고정돼 유령 인스턴스 720개를 지켰다). 기대값 대신
    ``_detect_pk_column`` 에 직접 물어본다 — A-Box 가 실제로 쓰는 판정기다.
    """
    import csv
    import os

    from tools.abox_generation import _detect_pk_column

    drifted = []
    for mapping in _mappings():
        src = mapping.get("source_csv")
        cls = mapping.get("source_class")
        declared = (
            mapping.get("source_composite_pk") or mapping.get("source_pk_column")
        )
        if not (src and cls and declared):
            continue
        path = os.path.join("data/source/rawdata", src)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            continue
        detected = _detect_pk_column(rows, cls)
        if not detected:
            continue

        def _norm(value):
            if isinstance(value, str):
                return [value.lower()]
            return [str(c).lower() for c in value]

        if _norm(declared) != _norm(detected):
            drifted.append(
                f"{mapping.get('name')}: 선언 {declared} vs A-Box 감지 {detected}"
            )

    assert not drifted, (
        "선언 PK 가 A-Box 감지 PK 와 다르다 — 같은 행에 IRI 두 벌이 생겨 "
        "rdf:type 없는 유령 인스턴스가 만들어진다:\n  " + "\n  ".join(drifted)
    )


def test_pk_columns_exist_in_source_csv():
    """PK 컬럼이 실제 CSV 헤더에 있다.

    없는 컬럼을 적으면 전략 함수가 조용히 빈 결과를 내거나 잘못된 IRI 를 만든다.
    """
    import csv
    import os
    missing = []
    for m in _mappings():
        pk = m.get("source_composite_pk") or []
        src = m.get("source_csv")
        if not (pk and src):
            continue
        path = os.path.join("data/source/rawdata", src)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), [])
        for col in pk:
            if col not in header:
                missing.append(f"{m.get('name')}: {col} not in {src}")
    assert not missing, "PK 컬럼이 CSV 에 없다:\n  " + "\n  ".join(missing)


def test_pk_column_order_follows_csv_business_key_not_alphabet():
    """PK 첫 컬럼이 ``Timestamp`` 가 아니어야 한다 (시계열 테이블 일반 규칙).

    A-Box 는 업무 식별자를 앞에, 시각을 뒤에 둔다. 이 방향을 고정해 두면 새 규칙이
    추가될 때 같은 결함이 재발하는 것을 설정 축에서 잡는다. 단, 시각만으로
    식별되는 테이블은 예외이므로 PK 가 2개 이상일 때만 본다.
    """
    offenders = [
        f"{m.get('name')}: {m.get('source_composite_pk')}"
        for m in _mappings()
        if (m.get("source_composite_pk") or []) and
        len(m["source_composite_pk"]) >= 2 and
        m["source_composite_pk"][0] == "Timestamp"
    ]
    assert not offenders, (
        "PK 첫 자리가 Timestamp — A-Box IRI 와 어긋날 가능성이 높다. "
        "A-Box 산출물의 실제 IRI 를 확인하라:\n  " + "\n  ".join(offenders)
    )


def test_regenerated_tacit_uses_abox_iri_shape():
    """재생성된 tacit TTL 의 주어가 A-Box 규약(``P###_<timestamp>``)을 따른다.

    설정만 고치고 재생성을 잊으면 산출물은 그대로다 — 이 리포에서 반복된 실패
    모드라 산출물 축으로 직접 확인한다.
    """
    import os

    from rdflib import Graph, URIRef

    from domain.namespaces import DOMAIN_NS
    path = "data/source/tacit/tacit_process_equipment.ttl"
    if not os.path.exists(path):
        pytest.skip("tacit_process_equipment.ttl 없음")
    g = Graph()
    g.parse(path, format="turtle")
    so = list(g.subject_objects(URIRef(str(DOMAIN_NS) + "hasBlastFurnaceEquipment")))
    if not so:
        pytest.skip("hasBlastFurnaceEquipment 트리플 없음")
    local = str(so[0][0])[len(_INSTANCE_NS):]
    assert re.match(r"^ProcessBlastFurnace_P\d+_", local), (
        f"tacit 주어가 A-Box 규약을 따르지 않는다: {local} — "
        "tacit_rules.json 수정 후 generate_tacit_from_rules 를 다시 실행했는지 확인"
    )
