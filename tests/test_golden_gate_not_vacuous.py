"""Golden query 게이트가 **항상 통과하는 장식**이 되지 않는다.

2026-08-22 실측으로 두 결함을 확인했다.

## 1. 스키마 예시가 죽은 게이트였다

``_GOLDEN_SCHEMA_EXAMPLE`` 은 SME 가 복사해 쓰는 템플릿인데:

- ``steel:occurrenceDateTime`` 이 T-Box 에 **존재하지 않는다** (grep 0건 —
  실제 이름은 ``failureCauseOccurrenceDateTime``).
- **GROUP BY 없는 순수 집계는 매칭 0건에도 1행을 반환한다** (``[('0',)]``).
  그래서 ``min_rows: 1`` 이 항상 통과했다 — T-Box 를 통째로 비워도 PASS 하는
  게이트였다.
- 날짜 창(2024-01)이 샘플 데이터 구간(2025-09)과 어긋나, DP 이름만 고치면
  **항상 0행 = 항상 FAIL** 로 반대편 실패가 된다.
- ``hasFailureCause`` 는 T-Box 에 선언돼 있지만 A-Box 는 역방향
  ``isFailureCauseOf`` 만 채운다 (132 트리플 vs 0). 빈 동의어로 질의하면 0행이
  "정답처럼" 반환된다 — 골든 쿼리의 가장 흔한 함정이다.

## 2. 공허한 assertion 이 입력 시점에 통과했다

``_evaluate_assertions`` 는 ``min_rows = a.get("min_rows", 0)`` 이므로 키가
없거나 오타(``min_row``)면 **0행을 정답으로 등록** 한다. ``_validate_case`` 는
그것을 에러 0건으로 통과시켰다.

**단, ``min_rows>=1`` 을 무조건 요구하면 안 된다** — ``max_rows: 0``
("위반이 0건이어야 한다") 는 정당한 장르이고 이미 지원된다. 이 파일은 그
보존 방향도 함께 고정한다.
"""
from __future__ import annotations

import json

import pytest

from tools.golden_queries import (
    _GOLDEN_SCHEMA_EXAMPLE,
    _validate_case,
    _validate_row_bound,
)

_EXAMPLE = _GOLDEN_SCHEMA_EXAMPLE[0]


def _case(**over) -> dict:
    c = json.loads(json.dumps(_EXAMPLE))
    c.update(over)
    return c


# ──────────────────────────────────────────────────────────────────
# 1. 스키마 예시가 실제로 동작하는 형태인가
# ──────────────────────────────────────────────────────────────────

def test_example_passes_its_own_validation():
    """SME 가 복사할 템플릿이 자기 검증을 통과해야 한다."""
    assert _validate_case(_EXAMPLE, 0) == []


def test_example_uses_group_by():
    """GROUP BY 가 없으면 0건 매칭에도 1행이 나와 min_rows 가 무력해진다."""
    sparql = _EXAMPLE["golden_sparql"]
    assert "GROUP BY" in sparql, (
        "GROUP BY 없는 순수 집계는 항상 1행을 반환해 게이트가 죽는다"
    )


def test_example_has_no_hardcoded_year_literal():
    """날짜 리터럴을 템플릿에 박으면 다른 데이터 구간에서 항상 FAIL 이다."""
    sparql = _EXAMPLE["golden_sparql"]
    for year in ("2023", "2024", "2025", "2026"):
        assert year not in sparql, f"연도 리터럴 {year} 이 박혀 있다"


def test_example_min_rows_is_meaningful():
    """0 이면 "아무것도 검사 안 함" 이다."""
    assert _EXAMPLE["assertions"]["min_rows"] >= 1


def test_example_property_names_exist_in_tbox():
    """예시가 참조하는 프로퍼티가 배포 T-Box 에 **실재** 하는가.

    존재하지 않는 이름을 쓰면 영구히 0행이다 — 예전 예시의
    ``steel:occurrenceDateTime`` 이 정확히 그랬다 (grep 0건).
    """
    import os
    import re

    path = "data/generated/tbox/t_box.ttl"
    if not os.path.exists(path):
        pytest.skip("배포 T-Box 없음")
    with open(path, encoding="utf-8") as fh:
        tbox = fh.read()
    names = set(re.findall(r"steel:([a-zA-Z][A-Za-z0-9_]*)",
                           _EXAMPLE["golden_sparql"]))
    missing = [n for n in sorted(names) if f"steel:{n}" not in tbox]
    assert not missing, (
        f"T-Box 에 없는 이름을 참조한다 (영구 0행): {missing} — S2 재생성으로 개명된 "
        "것이면 배포 T-Box 에서 실제 이름을 재고 tools/golden_queries.py 의 _EXAMPLE 을 "
        "갱신하라 (2026-09-05: isFailureCauseOf → failureCauseRefersToEquipment)"
    )


def test_example_uses_the_direction_the_abox_actually_fills():
    """선언만으로는 부족하다 — **채워지는 방향** 이어야 한다.

    ``_EXAMPLE`` 자신의 주석이 지적하는 함정이다: 역방향 동의어가 T-Box 에 선언돼
    있어도 A-Box 가 한쪽만 채우면 다른 쪽으로 질의한 0행이 "정답처럼" 반환돼 게이트가
    조용히 죽는다. 이름 존재 검사는 그것을 못 잡는다.

    ``load_graph()`` 는 ``ensure_inverse_triples()`` 로 역방향을 보완하므로, 여기서
    0건이면 어느 방향으로도 데이터가 없다는 뜻이다.
    """
    import os
    import re

    from config import ABOX_PATH

    if not os.path.exists(ABOX_PATH):
        pytest.skip("A-Box 없음")
    from rdflib import URIRef

    from domain.namespaces import DOMAIN_NS
    from domain.tbox_utils import load_graph

    g, _ = load_graph()
    names = sorted(set(re.findall(r"steel:([a-z][A-Za-z0-9_]*)",
                                  _EXAMPLE["golden_sparql"])))
    empty = [n for n in names
             if next(g.triples((None, URIRef(str(DOMAIN_NS) + n), None)), None) is None]
    assert not empty, (
        f"예시가 A-Box 가 채우지 않는 프로퍼티를 쓴다 (0행이 정답처럼 반환된다): "
        f"{empty} — 선언된 역방향 동의어 대신 실제로 채워지는 방향을 쓰라"
    )


# ──────────────────────────────────────────────────────────────────
# 2. 공허한 assertion 거부 (NEGATIVE 방향)
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("label", "assertions"),
    [
        ("min_rows 누락", {"required_vars": ["x"]}),
        ("min_rows 0", {"min_rows": 0}),
        ("min_rows 음수", {"min_rows": -1}),
        ("min_rows bool", {"min_rows": True}),
        ("min_rows 문자열", {"min_rows": "10"}),
        ("빈 dict", {}),
    ],
)
def test_vacuous_row_bound_is_rejected(label, assertions):
    """0행을 정답으로 등록하는 형태는 입력 시점에 막는다."""
    errors = _validate_row_bound(assertions, 0)
    assert errors, f"{label} 가 통과했다 — 게이트가 장식이 된다"
    assert "min_rows" in errors[0]


def test_typo_key_is_reported():
    """``min_row`` 오타는 평가기가 조용히 무시하므로 입력 시점에 알린다."""
    errors = _validate_row_bound({"min_row": 10}, 0)
    assert len(errors) >= 2, errors            # 경계 없음 + 미지 키
    assert any("알 수 없는 키" in e for e in errors)


def test_add_rejects_vacuous_case(tmp_path):
    """도구 경계에서도 거부되는가 (배선 확인)."""
    from unittest.mock import patch

    from tools import golden_queries as gq

    bad = _case(assertions={"required_vars": []})
    with patch.object(gq, "_GOLDEN_PATH", str(tmp_path / "g.json")):
        out = json.loads(gq.add_golden_queries(user_provided=json.dumps([bad])))
    assert out.get("success") is False
    assert "min_rows" in json.dumps(out, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────
# 3. 정당한 형태 보존 (POSITIVE 방향 — 과잉 차단 방지)
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("label", "assertions"),
    [
        ("min_rows 10", {"min_rows": 10}),
        ("max_rows 0 (위반 0건 장르)", {"max_rows": 0}),
        ("min+max 동시", {"min_rows": 1, "max_rows": 100}),
        ("전체 키 사용", {"min_rows": 1, "max_rows": 5, "required_vars": ["a"],
                        "sparql_must_contain": ["X"],
                        "sparql_must_not_contain": ["DELETE"]}),
    ],
)
def test_legitimate_assertions_are_preserved(label, assertions):
    """``max_rows: 0`` 은 "위반이 0건이어야 한다" 는 정당한 게이트다."""
    assert _validate_row_bound(assertions, 0) == [], label


def test_max_rows_zero_case_is_accepted_end_to_end(tmp_path):
    """위반 탐지 장르가 도구 경계를 통과하는가."""
    from unittest.mock import patch

    from tools import golden_queries as gq

    case = _case(id="no_violations", assertions={"max_rows": 0})
    with patch.object(gq, "_GOLDEN_PATH", str(tmp_path / "g.json")):
        out = json.loads(gq.add_golden_queries(user_provided=json.dumps([case])))
    assert out.get("success") is True, out


def test_non_dict_assertions_defers_to_caller():
    """타입 오류는 ``_validate_case`` 가 이미 보고하므로 중복 보고하지 않는다."""
    assert _validate_row_bound("not-a-dict", 0) == []
    assert _validate_row_bound(None, 0) == []
