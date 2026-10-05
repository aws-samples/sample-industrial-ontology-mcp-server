"""이슈 지문이 LLM 재표현을 흡수하되 **서로 다른 결함은 병합하지 않는다**.

2026-08-22 실측 (`debate_log.json` 90 이슈 레코드, 4라운드):

예전 지문축(``텍스트 200자 + severity + target 원문``)은 **2라운드 이상 잔존
0건** 을 보고했다. 그런데 라운드별 이슈 수는 15→17→14→17 로 반복하고 있었다.
원인은 LLM 이 같은 결함을 매번 다르게 쓰기 때문이다:

    R2: "AirEmissionMonitoring owl:Restriction 중복 선언"
    R5: "AirEmissionMonitoring rdfs:subClassOf 내 owl:Restriction 중복 (diff로 확인됨)"

게다가 ``target`` 90개 중 **54개가 식별자가 아니라 서술 문장** 이었다.
그 결과 ``_compute_issue_priority`` 의 age 가중치(라운드당 +15, 최대 +45)가
한 번도 발화하지 않았다.

## 이 파일의 핵심 — 과잉 병합 가드

지문을 느슨하게 하면 ``age_rounds`` 숫자는 오르지만 **서로 다른 결함이 하나로
합쳐진다**. 그러면 persistence 신호가 거짓이 된다 (지표 매수). 실측 비교:

===================================  ======  =====  ==========
정규화 변형                            그룹수   2R+    증상 혼재
===================================  ======  =====  ==========
현행 이전 (텍스트 200 + sev + raw)       90      0       0
category + 엔티티 (텍스트 버림)          71     15     **8**
category + 엔티티 + severity            75     12     **8**
**채택: category + 엔티티 + 텍스트 60**   82      8     **0**
===================================  ======  =====  ==========

"증상 혼재" = 서로 다른 결함이 한 그룹으로 병합된 수. 텍스트 축을 버리면
``logic:restriction`` 하나에 AirEmissionMonitoring / EquipmentMaster /
PurchaseOrder 의 **서로 다른 3개 결함** 이 합쳐진다.

아래 :func:`test_no_over_merge_on_real_debate_log` 가 배포 산출물로 그 수치를
고정한다 — 정규화를 더 느슨하게 바꾸면 이 테스트가 먼저 깨진다.
"""
from __future__ import annotations

import collections
import json
import os
import re

import pytest

from tools.multi_agent_tbox import (
    _compute_issue_persistence,
    _compute_issue_priority,
    _issue_atoms,
    _issue_fingerprint,
    _issue_identity,
    _issue_key,
)

_LOG = "data/generated/tbox/debate_log.json"


def _real_issues() -> list[dict]:
    """배포 debate_log.json 의 이슈 레코드 (라운드 번호 부착)."""
    if not os.path.exists(_LOG):
        return []
    with open(_LOG, encoding="utf-8") as fh:
        data = json.load(fh)
    runs = data.get("runs") or []
    if not runs:
        return []
    out: list[dict] = []
    for entry in runs[-1].get("rounds") or []:
        for agent in ("validator", "sme"):
            for issue in (entry.get(agent) or {}).get("issues") or []:
                if isinstance(issue, dict):
                    out.append({**issue, "_round": entry.get("round")})
    return out


# ──────────────────────────────────────────────────────────────────
# 1. 재표현 흡수 (POSITIVE)
# ──────────────────────────────────────────────────────────────────

def test_reworded_same_defect_merges():
    """실측 재표현 쌍 — 같은 결함이면 지문이 같아야 한다.

    실제 실측 패턴은 **도입부가 같고 뒤가 늘어나는** 형태다 (LLM 이 근거를
    덧붙인다). 도입부까지 완전히 다르게 쓰면 코드가 같은 결함인지 알 방법이
    없다 — 그 경우는 :func:`test_key_and_fingerprint_agree` 가 보는 target
    엔티티 축으로만 묶인다.
    """
    i1 = {"category": "logic", "severity": "critical",
          "target": "AirEmissionMonitoring owl:Restriction 중복 선언",
          "symptom": "AirEmissionMonitoring의 rdfs:subClassOf에 동일한 "
                     "owl:Restriction 이 두 번 나온다"}
    i2 = {"category": "logic", "severity": "high",   # 심각도도 바뀌었다
          "target": "AirEmissionMonitoring rdfs:subClassOf 내 중복 (diff로 확인)",
          "symptom": "AirEmissionMonitoring의 rdfs:subClassOf에 동일한 "
                     "owl:Restriction 이 두 번 나온다 — diff 에서도 확인됨, "
                     "step_20 dedup 이 놓친 것으로 보인다"}
    assert _issue_fingerprint(i1) == _issue_fingerprint(i2)


def test_case_and_whitespace_variation_merges():
    i1 = {"category": "logic", "target": "EquipmentMaster", "symptom": "domain 누락"}
    i2 = {"category": "Logic", "target": "equipmentmaster", "symptom": "Domain  누락"}
    assert _issue_fingerprint(i1) == _issue_fingerprint(i2)


def test_entity_order_variation_merges():
    """``A ↔ B`` 와 ``B ↔ A`` 는 같은 쌍이다 (원자를 정렬하므로)."""
    i1 = {"category": "inference", "target": "steel:TagMaster ↔ steel:EquipmentMaster",
          "symptom": "방향 불일치"}
    i2 = {"category": "inference", "target": "steel:EquipmentMaster ↔ steel:TagMaster",
          "symptom": "방향 불일치"}
    assert _issue_fingerprint(i1) == _issue_fingerprint(i2)


def test_severity_change_does_not_split():
    """심각도는 동일성 축이 아니다 — 중요도는 priority 가 따로 반영한다."""
    base = {"category": "logic", "target": "A", "symptom": "같은 문제"}
    assert (_issue_fingerprint({**base, "severity": "high"})
            == _issue_fingerprint({**base, "severity": "medium"}))


# ──────────────────────────────────────────────────────────────────
# 2. 과잉 병합 금지 (NEGATIVE — 이쪽이 더 위험하다)
# ──────────────────────────────────────────────────────────────────

def test_different_classes_do_not_merge():
    """같은 category·같은 증상 유형이라도 **대상 클래스가 다르면** 다른 결함이다."""
    a = {"category": "logic", "target": "AirEmissionMonitoring owl:Restriction 중복",
         "symptom": "AirEmissionMonitoring의 subClassOf에 중복 Restriction"}
    b = {"category": "logic", "target": "PurchaseOrder owl:Restriction 중복",
         "symptom": "PurchaseOrder의 subClassOf에 중복 Restriction"}
    assert _issue_fingerprint(a) != _issue_fingerprint(b)


def test_different_symptoms_on_same_class_do_not_merge():
    """같은 클래스의 **서로 다른 문제** 는 병합되면 안 된다.

    실측: AirEmissionMonitoring 하나에 Restriction 중복 / domain owl:Thing /
    데이터 갭 주석 미비 등 7건이 달려 있었다. 텍스트 축을 버리면 전부 하나가 된다.
    """
    base = {"category": "logic", "target": "AirEmissionMonitoring"}
    a = {**base, "symptom": "owl:Restriction 이 중복 선언됐다"}
    b = {**base, "symptom": "rdfs:domain 이 owl:Thing 으로 잘못 선언됐다"}
    c = {**base, "symptom": "데이터 갭 주석이 Stack_ID 값 공간을 설명하지 못한다"}
    fps = {_issue_fingerprint(x) for x in (a, b, c)}
    assert len(fps) == 3, "서로 다른 결함 3개가 병합됐다 — persistence 신호가 거짓이 된다"


def test_different_category_does_not_merge():
    base = {"target": "EquipmentMaster", "symptom": "같은 문장"}
    assert (_issue_fingerprint({**base, "category": "logic"})
            != _issue_fingerprint({**base, "category": "metric"}))


@pytest.mark.skipif(not os.path.exists(_LOG), reason="배포 debate_log.json 없음")
def test_no_over_merge_on_real_debate_log():
    """배포 산출물로 과잉 병합 0건을 고정한다 (핵심 회귀 가드).

    정규화를 더 느슨하게 바꾸면 이 테스트가 먼저 깨진다.
    """
    issues = _real_issues()
    if not issues:
        pytest.skip("이슈 레코드 없음")
    groups: dict[str, list[dict]] = collections.defaultdict(list)
    for issue in issues:
        groups[_issue_fingerprint(issue)].append(issue)
    mixed = {
        key: {re.sub(r"\s+", " ", str(i.get("symptom") or ""))[:58] for i in items}
        for key, items in groups.items()
    }
    offenders = {k: v for k, v in mixed.items() if len(v) > 1}
    assert not offenders, (
        f"서로 다른 증상이 병합된 그룹 {len(offenders)}개: "
        f"{list(offenders.items())[:2]}"
    )


@pytest.mark.skipif(not os.path.exists(_LOG), reason="배포 debate_log.json 없음")
def test_real_debate_log_detects_repeats():
    """재표현 흡수가 실제로 반복을 잡는가 (예전엔 0건이었다)."""
    issues = _real_issues()
    if not issues:
        pytest.skip("이슈 레코드 없음")
    groups: dict[str, set] = collections.defaultdict(set)
    for issue in issues:
        groups[_issue_fingerprint(issue)].add(issue.get("_round"))
    repeats = [k for k, rounds in groups.items() if len(rounds) >= 2]
    assert repeats, (
        "반복 이슈를 하나도 못 잡았다 — 재표현 흡수가 동작하지 않는다 "
        "(이슈 수가 라운드마다 반복되는데 지문이 전부 달라지는 상태)"
    )


# ──────────────────────────────────────────────────────────────────
# 3. 두 축 통일 + 안전성
# ──────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not os.path.exists(_LOG), reason="배포 debate_log.json 없음")
def test_key_and_fingerprint_agree():
    """``_issue_key`` 와 ``_issue_fingerprint`` 가 같은 그룹핑을 낸다.

    예전엔 축이 갈려 같은 실행에서 ``persistent_issue_count=0`` 과
    ``veto_lock`` 잔존 2건이 모순됐다.
    """
    issues = _real_issues()
    if not issues:
        pytest.skip("이슈 레코드 없음")
    assert (len({_issue_fingerprint(i) for i in issues})
            == len({_issue_key(i) for i in issues}))


def test_key_is_human_readable_and_printable():
    """veto 로그·보고서·T-Box 각인에 그대로 찍히므로 제어문자가 없어야 한다.

    원문을 키에 넣으면 개행·NUL 이 섞여 RDF 직렬화를 깨뜨린다.
    """
    issue = {"category": "logic", "target": "RR 미달 (0.254 < 0.30)",
             "symptom": "크로스 도메인 OP 누락\n두 번째 줄\x00"}
    key = _issue_key(issue)
    assert key.isprintable(), repr(key)
    assert "\n" not in key and "\x00" not in key
    assert key.startswith("logic:")


def test_korean_particle_does_not_stick_to_identifier():
    """``\\w`` 를 쓰면 한글 조사가 이름에 붙는다 — ASCII 전용 패턴이어야 한다."""
    atoms = _issue_atoms("steel:equipmentStatusMaintenanceFlag의 range 오류")
    assert atoms == ("equipmentstatusmaintenanceflag",), atoms


@pytest.mark.parametrize("bad", [12345, ["a"], {"k": 1}, None, 3.14])
def test_non_str_inputs_do_not_crash(bad):
    """LLM 이 비-str 을 내도 죽지 않는다 (섹션 소실 방지)."""
    fp = _issue_fingerprint({"symptom": bad, "target": bad, "category": bad})
    assert isinstance(fp, str) and len(fp) == 16


def test_identity_returns_three_axes():
    category, atoms, digest = _issue_identity(
        {"category": "Logic", "target": "steel:Foo", "symptom": "x"},
    )
    assert category == "logic"
    assert atoms == ("foo",)
    assert len(digest) == 12 and digest.isalnum()


# ──────────────────────────────────────────────────────────────────
# 4. 하류 효과 — age_rounds 와 priority 가 실제로 움직인다
# ──────────────────────────────────────────────────────────────────

def test_age_rounds_increases_for_reworded_issue():
    """재표현된 같은 결함이 라운드를 넘어 누적된다."""
    from tools.multi_agent_tbox import _build_round_log

    # 실측 symptom 길이는 중앙값 200자이고 **99% 가 60자 이상** 이다 (2026-08-22,
    # 90건). 즉 LLM 의 재표현은 지문 창(60자) **밖** 에서 일어난다. 픽스처도 그
    # 형태여야 한다 — 60자 미만으로 만들면 창 안에서 갈려 코드가 아니라 픽스처를
    # 시험하게 된다 (처음 이 테스트가 그렇게 실패했다).
    _HEAD = (
        "EquipmentMaster 의 rdfs:domain 이 누락돼 어떤 인스턴스든 제약 없이 "
        "통과한다. CSV FK 근거가 있으므로 선언 가능하다"
    )

    def _issue(suffix: str) -> dict:
        # 도입부는 유지되고 뒤에 근거가 덧붙는다. target 서술과 severity 는
        # 라운드마다 흔들린다 — 둘 다 동일성을 깨지 않아야 한다.
        return {"category": "logic",
                "severity": "high" if len(suffix) % 2 else "critical",
                "target": f"steel:EquipmentMaster domain 문제{suffix}",
                "symptom": _HEAD + suffix}

    prior = [
        _build_round_log({"issues": [_issue(f" (라운드 {r} 표현)")],
                          "approved": False, "summary": ""},
                         {"issues": [], "approved": False, "summary": ""}, r, True)
        for r in range(3)
    ]
    enriched = _compute_issue_persistence([_issue(" — 또 다르게")], prior)
    assert enriched[0]["age_rounds"] >= 2, (
        f"age_rounds={enriched[0]['age_rounds']} — 재표현 흡수가 동작하지 않는다"
    )


def test_priority_rises_with_age():
    base = {"category": "logic", "severity": "high", "target": "A",
            "symptom": "문제"}
    fresh = _compute_issue_priority({**base, "age_rounds": 1}, 66.7)
    aged = _compute_issue_priority({**base, "age_rounds": 3}, 66.7)
    assert aged > fresh
