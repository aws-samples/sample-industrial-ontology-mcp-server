"""Jury fix 결과의 ``noop`` / ``failed`` 분류가 원인과 일치한다.

``failed`` 버킷에는 성질이 다른 두 가지가 섞일 수 있다:

1. **계약 위반** — LLM 이 필드 이름/필수값을 몰랐다 (``members list(>=2) 누락``).
   → 프롬프트를 고쳐야 한다.
2. **no-op** — 지시는 올바르지만 이미 그 상태다 (``모든 children 이 이미 parent
   의 하위``). → 고칠 것이 없다. 정상 동작이다.

둘을 한 버킷에 담으면 **"프롬프트를 고쳐서 실패가 줄었는가" 를 측정할 수 없다.**
실측 (2026-08-22): 핸들러가 반환하는 no-op 성격 사유 22개 중 **14개가 마커에
걸리지 않아** ``failed`` 로 집계됐다. 최종 Jury 로그의 실패 3건 중
``hierarchy 에서 추가할 엔티티 없음`` 은 중복 지시(=정상)였는데 실패로 보고됐다.

## 이 파일의 핵심 — 드리프트 가드

문자열 매칭은 핸들러가 문구를 바꾸면 **조용히 어긋난다**
(``_apply_property_characteristic`` 의 주석이 이미 그 위험을 경고한다).
그래서 :func:`test_no_new_noop_reason_escapes_markers` 가 **소스에서 사유 문자열을
전수 추출해** 새 no-op 사유가 마커 없이 추가되면 실패한다. 핸들러 문구를 바꾸면
이 테스트가 먼저 깨진다.

## 양방향 주장

- no-op 이 ``failed`` 로 새지 않는다 (통계 오염 방지).
- **계약 위반이 ``noop`` 으로 숨지 않는다** — 이게 더 위험하다. 숨으면 LLM 의
  스키마 오류가 통계에서 사라져 프롬프트 결함이 영구히 안 보인다.
"""
from __future__ import annotations

import re

import pytest

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.jury_fixes import _NOOP_MARKERS, _is_noop_reason, apply_jury_fixes

_SRC_PATH = "tools/jury_fixes.py"

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)
_TTL = _HDR + f"""
{NS_PREFIX}:Parent a owl:Class .
{NS_PREFIX}:Child a owl:Class ; rdfs:subClassOf {NS_PREFIX}:Parent .
{NS_PREFIX}:someProp a owl:ObjectProperty ;
    rdfs:domain {NS_PREFIX}:Child ; rdfs:range {NS_PREFIX}:Parent .
"""


def _handler_failure_reasons() -> list[str]:
    """소스에서 핸들러의 ``return False, "..."`` 사유를 전수 추출."""
    with open(_SRC_PATH, encoding="utf-8") as fh:
        src = fh.read()
    return sorted(set(re.findall(r'return False, (?:f)?"([^"]{4,120})"', src)))


#: no-op 을 뜻하는 사유의 판별 어휘 — "대상이 없어서 할 일이 없었다".
#: 이 어휘가 들어간 사유는 ``_NOOP_MARKERS`` 에 등록돼 있어야 한다.
_NOOP_VOCAB = ("이미", "없음", "없다", "존재하지 않")

#: 계약 위반 사유의 판별 어휘 — 필수 필드/값 문제. no-op 으로 분류되면 안 된다.
_CONTRACT_VOCAB = ("누락", "필요", "필수", "미지원", "해석할 수 없")


def test_no_new_noop_reason_escapes_markers():
    """새 no-op 사유가 마커 없이 추가되면 실패한다 (드리프트 가드).

    이 테스트가 깨지면 둘 중 하나다:
    - 핸들러 문구를 바꿨다 → ``_NOOP_MARKERS`` 를 함께 갱신하라.
    - 새 no-op 사유를 추가했다 → 마커에 등록하라.
    """
    escaped = [
        r for r in _handler_failure_reasons()
        if any(v in r for v in _NOOP_VOCAB)
        and not any(v in r for v in _CONTRACT_VOCAB)
        and not _is_noop_reason(r)
    ]
    assert not escaped, (
        "no-op 성격 사유가 failed 로 집계된다 — _NOOP_MARKERS 에 등록하라:\n  "
        + "\n  ".join(escaped)
    )


def test_contract_violations_are_never_noop():
    """필수 필드 누락은 절대 no-op 이 아니다 (더 위험한 방향).

    계약 위반이 ``noop`` 으로 숨으면 LLM 스키마 오류가 통계에서 사라져
    프롬프트 결함이 영구히 안 보인다.
    """
    misclassified = [
        r for r in _handler_failure_reasons()
        if any(v in r for v in _CONTRACT_VOCAB) and _is_noop_reason(r)
    ]
    # "mappings 에서 추가할 매핑 없음 (이미 존재 또는 필수 필드 누락)" 은 두 어휘가
    # 함께 있는 혼합 사유다 — 핸들러가 두 경우를 구분하지 않으므로 예외로 둔다.
    misclassified = [r for r in misclassified if "추가할 매핑 없음" not in r]
    assert not misclassified, (
        "계약 위반이 noop 으로 숨는다 — 마커를 좁혀라:\n  " + "\n  ".join(misclassified)
    )


@pytest.mark.parametrize(
    "reason",
    [
        "members list(>=2) 누락",
        "parent/children 누락",
        "property/domain/range 중 누락",
        "hierarchy 리스트 누락 — description 기반 자동 생성 불가",
        "target 누락",
        "class 누락",
        "object 값을 해석할 수 없음",
    ],
)
def test_known_contract_violations_stay_failed(reason):
    """실측된 계약 위반 사유가 failed 로 남는지 개별 고정."""
    assert not _is_noop_reason(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "hierarchy 에서 추가할 엔티티 없음",       # 실측: failed 로 오분류됐던 것
        "모든 children 이 이미 parent 의 하위",
        "제거할 Restriction 없음",
        "매칭되는 annotation 없음",
        "해당 property 에 대한 트리플 없음",
        "이미 동등 관계가 모두 선언됨",
        "모든 axiom 이 이미 존재",
        "교체할 트리플 없음",
    ],
)
def test_known_noops_are_classified_as_noop(reason):
    """실측된 no-op 사유가 noop 으로 분류되는지 개별 고정."""
    assert _is_noop_reason(reason)


# ──────────────────────────────────────────────────────────────────
# 산출물로 확인 — apply_jury_fixes 를 실제로 돌린다
# ──────────────────────────────────────────────────────────────────

def test_duplicate_hierarchy_instruction_is_noop_not_failed():
    """이미 있는 계층을 다시 지시하면 noop 이다 (실측 오분류 사례)."""
    result = apply_jury_fixes(_TTL, [{
        "action": "add_class_hierarchy",
        "hierarchy": [{"parent": f"{NS_PREFIX}:Parent",
                       "children": [f"{NS_PREFIX}:Child"]}],
    }])
    assert not result["failed"], f"중복 지시가 실패로 집계됐다: {result['failed']}"
    assert len(result["noop"]) == 1


def test_missing_members_is_failed_not_noop():
    """필수 필드를 빠뜨린 지시는 failed 로 남아야 한다 (프롬프트 신호)."""
    result = apply_jury_fixes(_TTL, [{"action": "add_disjoint_classes"}])
    assert len(result["failed"]) == 1, (
        f"계약 위반이 noop 으로 숨었다: noop={result['noop']}"
    )
    assert "members" in str(result["failed"][0]["reason"])


def test_removing_absent_property_is_noop():
    """없는 프로퍼티 제거는 할 일이 없었던 것이다."""
    result = apply_jury_fixes(_TTL, [{
        "action": "remove_object_property", "property": f"{NS_PREFIX}:notThere",
    }])
    assert not result["failed"]
    assert len(result["noop"]) == 1


def test_markers_are_nonempty_and_unique():
    """마커 목록 위생 — 중복/빈 문자열이 있으면 의도가 흐려진다."""
    assert all(m.strip() for m in _NOOP_MARKERS)
    assert len(set(_NOOP_MARKERS)) == len(_NOOP_MARKERS)
