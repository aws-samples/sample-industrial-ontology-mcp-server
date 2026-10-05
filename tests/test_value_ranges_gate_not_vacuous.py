"""값 범위 게이트가 Path B 이후 영구 0건 검사였다.

2026-08-24 실측 (배포 A-Box 746,373 트리플): ``check_value_ranges`` 가
``checked_properties: 0`` / ``passed: True`` 를 반환했다. 선언된 범위 규칙 7개가
**하나도 DP 에 닿지 않았다.**

## 원인 — 규칙 키를 리터럴 DP 이름으로 조회했다

Path B 정책이 모든 DP 를 class-prefixed 로 바꿨다::

    규칙 키          temperatureC
    실제 DP          gasEnergyTemperatureC / steamEnergyTemperatureC

``SELECT … WHERE {{ ?s ns:temperatureC ?v }}`` 는 0행이고, 코드는 ``total == 0`` 이면
``continue`` 한다. 즉 **규칙이 전부 조용히 건너뛰어졌다.** 9999도·효율 500% 를 넣어도
PASS 였다 (아래 테스트가 그 상태를 고정한다).

## 왜 0건을 PASS 로 두면 안 되는가

"위반이 없다" 와 "검사를 못 했다" 가 같은 결과로 보이면 게이트가 꺼진 것을 아무도
모른다. 이 리포에서 반복된 실패 모드다 — 익명 표현식이 baseline 을 FAIL 로 고정한
사례, ``fk_op_coverage`` 가 domain 무시로 100% 오보고한 사례, ``추론 sanity`` 가
자기비교로 영구 FAIL 이던 사례. 그래서 vacuous 판정 시 ``passed=False`` 로 보고한다.

## 왜 substring 매칭을 안 쓰는가

``carbonContent`` 규칙 ``[0, 1]`` 은 비율을 전제하는데 실제 DP 는
``steelmakingCarbonContentPercent`` (퍼센트) 다. substring 으로 끌어오면 단위가
다른 DP 를 검사해 오탐이 된다. 단위는 이름으로 판별할 수 없으므로 그런 키는
``applies_to`` 로 명시하게 하고, 추론은 **접미 매칭까지만** 허용한다.
"""
from __future__ import annotations

import json

import pytest
from rdflib import XSD, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.rules_paths import rules_path
from domain.tbox_utils import _new_graph
from tools.validation_support.checks.statistical import (
    _resolve_range_targets,
    check_value_ranges,
)

_ABOX = "data/generated/abox/a_box.ttl"


def _dp(name: str) -> URIRef:
    return URIRef(f"{DOMAIN_NS}{name}")


def _inst(name: str) -> URIRef:
    return URIRef(f"{DOMAIN_INST_NS}{name}")


def _rules(tmp_path, ranges: dict):
    d = tmp_path / "rules"
    d.mkdir(exist_ok=True)
    (d / "value_ranges.json").write_text(
        json.dumps({"ranges": ranges}, ensure_ascii=False), encoding="utf-8",
    )
    return str(d)


# ──────────────────────────────────────────────────────────────────
# 1. class-prefixed DP 해상 (THE REGRESSION)
# ──────────────────────────────────────────────────────────────────

def test_class_prefixed_dp_is_matched_by_suffix(tmp_path):
    """``temperatureC`` 규칙이 ``gasEnergyTemperatureC`` 를 검사한다."""
    g = _new_graph()
    g.add((_inst("A"), _dp("gasEnergyTemperatureC"), Literal("9999", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "temperatureC": {"min": -50, "max": 2500, "description": "온도"},
    }))
    assert r["checked_properties"] == 1, r
    assert r["passed"] is False, "class-prefixed DP 의 위반을 놓쳤다"
    assert r["violations"][0]["property"] == "gasEnergyTemperatureC"


def test_suffix_match_is_case_insensitive(tmp_path):
    """``pressureMpa`` 규칙이 ``steamEnergyPressureMPa`` 에 걸린다 (MPa 대소문자)."""
    g = _new_graph()
    g.add((_inst("A"), _dp("steamEnergyPressureMPa"), Literal("999", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "pressureMpa": {"min": 0, "max": 100, "description": "압력"},
    }))
    assert r["passed"] is False, "대소문자 차이로 해상에 실패했다"


def test_multiple_class_prefixed_dps_all_checked(tmp_path):
    """한 규칙이 여러 클래스의 DP 를 동시에 담당한다."""
    g = _new_graph()
    for name in ("gasEnergyTemperatureC", "steamEnergyTemperatureC"):
        g.add((_inst("A"), _dp(name), Literal("50", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "temperatureC": {"min": -50, "max": 2500, "description": "온도"},
    }))
    assert r["checked_properties"] == 2, r


def test_exact_match_still_works(tmp_path):
    """Path B 이전 도메인 (generic DP) 하위 호환."""
    g = _new_graph()
    g.add((_inst("A"), _dp("pressure"), Literal("999", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "pressure": {"min": 0, "max": 100, "description": "bar"},
    }))
    assert r["passed"] is False


# ──────────────────────────────────────────────────────────────────
# 2. 0건 검사는 PASS 가 아니다
# ──────────────────────────────────────────────────────────────────

def test_no_rule_matches_any_dp_is_reported_as_failure(tmp_path):
    """규칙이 전부 미매칭이면 ``passed=False`` — 게이트가 꺼진 것을 드러낸다."""
    g = _new_graph()
    g.add((_inst("A"), _dp("somethingElse"), Literal("5", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "temperatureC": {"min": 0, "max": 100, "description": "온도"},
    }))
    assert r["checked_properties"] == 0
    assert r["passed"] is False, "0건 검사를 PASS 로 보고했다 — 꺼진 게이트가 숨는다"
    assert "꺼져 있다" in str(r.get("message", "")), r
    assert r["unmatched_rules"] == ["temperatureC"]


def test_missing_rules_file_is_still_a_legitimate_skip(tmp_path):
    """설정 파일 자체가 없는 오픈소스 클론은 skip 이 정상 (과잉 차단 방지)."""
    r = check_value_ranges(_new_graph(), rules_dir=str(tmp_path))
    assert r["passed"] is True
    assert "건너뜀" in r["message"]


def test_empty_rules_object_does_not_fail(tmp_path):
    """규칙을 아직 안 채운 신규 도메인은 FAIL 이 아니다."""
    r = check_value_ranges(_new_graph(), rules_dir=_rules(tmp_path, {}))
    assert r["passed"] is True, "빈 규칙은 '검사 못 함' 이 아니라 '검사할 것이 없음'"


# ──────────────────────────────────────────────────────────────────
# 3. applies_to 명시 — 단위 모호성 해소
# ──────────────────────────────────────────────────────────────────

def test_applies_to_overrides_inference(tmp_path):
    """명시 선언이 접미 추론을 이긴다 (SME 판단이 정본)."""
    g = _new_graph()
    g.add((_inst("A"), _dp("steelmakingCarbonContentPercent"),
           Literal("150", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "carbonContent": {
            "min": 0, "max": 100, "description": "탄소 함량 (%)",
            "applies_to": ["steelmakingCarbonContentPercent"],
        },
    }))
    assert r["checked_properties"] == 1, r
    assert r["passed"] is False
    assert r["violations"][0]["rule_key"] == "carbonContent"


def test_applies_to_accepts_bare_string(tmp_path):
    g = _new_graph()
    g.add((_inst("A"), _dp("energyEfficiencyPercent"), Literal("500", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "efficiencyValue": {
            "min": 0, "max": 100, "description": "효율",
            "applies_to": "energyEfficiencyPercent",
        },
    }))
    assert r["passed"] is False


def test_substring_is_not_used_for_inference():
    """추론은 접미까지만 — substring 은 단위 다른 DP 를 끌어와 오탐이 된다."""
    g = _new_graph()
    g.add((_inst("A"), _dp("steelmakingCarbonContentPercent"),
           Literal("0.05", datatype=XSD.decimal)))
    targets, how, stale = _resolve_range_targets(g, "carbonContent", None)
    assert targets == [], (
        f"substring 매칭으로 {targets} 를 끌어왔다 — 퍼센트 DP 에 비율 범위가 적용된다"
    )
    assert how == "none"
    assert stale == [], "applies_to 미지정이면 낡은 이름도 없다"


def test_resolution_is_reported_for_audit(tmp_path):
    """어떤 DP 로 해상됐는지 응답에 남는다 (추론 결과를 감사 가능하게)."""
    g = _new_graph()
    g.add((_inst("A"), _dp("gasEnergyTemperatureC"), Literal("50", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "temperatureC": {"min": -50, "max": 2500, "description": "온도"},
    }))
    assert r["resolved_via_suffix"] == {"temperatureC": ["gasEnergyTemperatureC"]}


# ──────────────────────────────────────────────────────────────────
# 3b. applies_to 이름 드리프트 — rules/ 는 추적, T-Box 는 gitignore
# ──────────────────────────────────────────────────────────────────
#
# 2026-09-05 실측: S2 세대 교체로 7규칙 중 2개가 0건이 됐다.
#   efficiencyValue  energyEfficiencyPercent → energyEfficiencyEfficiencyPercent  (실제 개명)
#   co2Amount        blastFurnaceCo2Percent  → blastFurnaceCO2Percent             (대소문자만)
# 앞의 것은 사람이 갱신해야 하고, 뒤의 것은 자동 해상돼야 한다.


def test_applies_to_matches_case_insensitively(tmp_path):
    """대소문자만 다른 개명은 같은 DP 다 — 자동 해상돼야 한다.

    S2 는 재생성마다 casing 을 다르게 낸다 (``Co2`` ↔ ``CO2``). 이 축을
    case-sensitive 로 두면 규칙이 조용히 죽는다.
    """
    g = _new_graph()
    g.add((_inst("A"), _dp("blastFurnaceCO2Percent"),
           Literal("20", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "co2Amount": {"min": 0, "max": 100, "description": "CO2",
                      "applies_to": ["blastFurnaceCo2Percent"]},
    }))
    assert r["rules_measured"] == 1, r
    assert "unmatched_rules" not in r, r
    assert "applies_to_stale" not in r, r


def test_stale_applies_to_is_reported_with_candidates(tmp_path):
    """실제 개명은 자동 해상하지 않고 **근접 후보와 함께** 보고한다.

    후보를 안 주면 "미매칭" 만 보고 사람이 T-Box 를 다시 뒤져야 한다.
    """
    g = _new_graph()
    g.add((_inst("A"), _dp("energyEfficiencyEfficiencyPercent"),
           Literal("80", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "efficiencyValue": {"min": 0, "max": 100, "description": "효율",
                            "applies_to": ["energyEfficiencyPercent"]},
    }))
    stale = r["applies_to_stale"]["efficiencyValue"]
    assert stale["missing"] == ["energyEfficiencyPercent"]
    assert "energyEfficiencyEfficiencyPercent" in \
        stale["candidates"]["energyEfficiencyPercent"], stale


def test_stale_applies_to_does_not_fall_back_to_suffix(tmp_path):
    """PRESERVATION: 폴백하면 ``applies_to`` 의 존재 이유가 무너진다.

    ``applies_to`` 는 단위 모호성을 막기 위해 있다 — 낡았을 때 접미 매칭으로
    폴백하면 비율 범위가 퍼센트 DP 에 걸리는 오탐이 되살아난다.
    """
    g = _new_graph()
    g.add((_inst("A"), _dp("steelmakingCarbonContentPercent"),
           Literal("0.05", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "carbonContent": {"min": 0, "max": 1, "description": "탄소 비율",
                          "applies_to": ["chemicalAnalysisCarbonRatio"]},
    }))
    assert r["violations"] == [], "폴백으로 단위 다른 DP 를 검사했다"
    assert r["unmatched_rules"] == ["carbonContent"]
    assert r["rules_measured"] == 0


def test_stale_name_is_not_reported_as_resolved(tmp_path):
    """낡은 이름이 ``resolved_via_suffix`` 에 실리면 해상된 것으로 오독된다.

    예전 구현은 ``present or list(declared)`` 로 낡은 이름을 그대로 돌려줘, 실측
    응답에 ``resolved_via_suffix: {"co2Amount": ["blastFurnaceCo2Percent"]}`` 가
    찍혔다 — 존재하지 않는 DP 였다.
    """
    g = _new_graph()
    g.add((_inst("A"), _dp("gasEnergyTemperatureC"),
           Literal("50", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "temperatureC": {"min": -50, "max": 2500, "description": "온도"},
        "co2Amount": {"min": 0, "max": 100, "description": "CO2",
                      "applies_to": ["blastFurnaceCo2Percent"]},
    }))
    assert "co2Amount" not in r.get("resolved_via_suffix", {}), r
    assert "co2Amount" in r["applies_to_stale"], r


def test_partially_dead_gate_is_visible_at_top_level(tmp_path):
    """규칙 7개 중 2개가 죽어도 ``checked_properties`` 는 그것을 감춘다.

    ``checked_properties`` 는 DP 개수이므로 살아 있는 규칙 하나가 DP 2개를 재면 2 가
    된다 — "규칙 절반이 꺼졌다" 를 최상위 수치로는 알 수 없었다. ``rules_measured``
    가 그 축이다. (부분 정지는 ``passed`` 를 내리지 않는다 — 미측정을 FAIL 로 보고하면
    상시 빨간불이 되어 아무도 보지 않는다.)
    """
    g = _new_graph()
    g.add((_inst("A"), _dp("gasEnergyTemperatureC"), Literal("50", datatype=XSD.decimal)))
    g.add((_inst("B"), _dp("steamEnergyTemperatureC"), Literal("60", datatype=XSD.decimal)))
    r = check_value_ranges(g, rules_dir=_rules(tmp_path, {
        "temperatureC": {"min": -50, "max": 2500, "description": "온도"},
        "co2Amount": {"min": 0, "max": 100, "description": "CO2",
                      "applies_to": ["blastFurnaceCo2Percent"]},
    }))
    assert r["checked_properties"] == 2      # DP 개수
    assert r["rules_measured"] == 1          # 규칙 개수 — 이쪽이 절반 정지를 드러낸다
    assert r["total_rules"] == 2
    assert r["passed"] is True


# ──────────────────────────────────────────────────────────────────
# 4. 배포 산출물 회귀 — 게이트가 실제로 무언가를 검사한다
# ──────────────────────────────────────────────────────────────────

def _deployed_abox() -> Graph:
    import os
    if not os.path.exists(_ABOX):
        pytest.skip("배포 A-Box 없음")
    g = _new_graph()
    g.parse(_ABOX, format="turtle")
    return g


def test_deployed_gate_checks_more_than_zero_properties():
    """THE REGRESSION: 배포 설정 + 배포 A-Box 로 0건이 아니어야 한다."""
    r = check_value_ranges(_deployed_abox())
    assert r["checked_properties"] > 0, (
        f"배포 환경에서 값 범위 검증이 0건이다 (게이트 꺼짐): {r}"
    )


def test_deployed_gate_has_no_false_alarm():
    """정상 데이터에 오탐이 없다 — 단위 불일치 매칭을 걸렀는지 확인."""
    r = check_value_ranges(_deployed_abox())
    assert r["violations"] == [], f"배포 데이터에 오탐: {r['violations']}"
    assert r["passed"] is True


def test_deployed_gate_detects_injected_violation():
    """실측 주입 검증 — 9999도와 ``applies_to`` 전용 DP 의 범위 초과를 잡는다.

    수정 전 코드는 이 둘을 놓치고 ``passed: True`` 를 반환했다 (실측).

    2026-09-05: 주입 대상 DP 이름을 **규칙 파일에서 읽는다.** 예전에는
    ``energyEfficiencyPercent`` 를 박아 뒀는데 S2 세대 교체로 그 DP 가
    ``energyEfficiencyEfficiencyPercent`` 로 개명돼 "존재하지 않는 DP 에 주입하고
    안 잡혔다고 실패" 하는 상태가 됐다. 이름을 규칙 파일에서 읽으면 이 테스트가
    **규칙 파일과 배포 산출물의 동기까지** 함께 주장한다.
    """
    with open(rules_path("value_ranges.json"), encoding="utf-8") as fh:
        ranges = json.load(fh)["ranges"]
    declared_only = {
        key: rule["applies_to"][0]
        for key, rule in ranges.items()
        if rule.get("applies_to") and rule.get("max") is not None
    }
    assert declared_only, "applies_to 를 쓰는 규칙이 없다 — 이 축을 검증할 수 없다"

    g = _deployed_abox()
    g.add((_inst("ProbeTemp"), _dp("gasEnergyTemperatureC"),
           Literal("9999", datatype=XSD.decimal)))
    injected = {}
    for key, dp_name in declared_only.items():
        over = float(ranges[key]["max"]) + 1000
        g.add((_inst(f"Probe_{key}"), _dp(dp_name),
               Literal(str(over), datatype=XSD.decimal)))
        injected[dp_name] = key

    r = check_value_ranges(g)
    assert r["passed"] is False, "주입한 명백한 위반을 놓쳤다"
    caught = {v["property"] for v in r["violations"]}
    assert "gasEnergyTemperatureC" in caught
    missed = sorted(set(injected) - caught)
    assert not missed, (
        f"applies_to 로만 도달하는 DP 의 위반을 놓쳤다: {missed} — "
        f"그 이름이 배포 T-Box 에 없으면 규칙 파일이 낡은 것이다 "
        f"(applies_to_stale: {r.get('applies_to_stale')})"
    )


def test_deployed_config_has_no_unmatched_rules():
    """배포 설정의 모든 규칙이 실제 DP 에 닿는다.

    미매칭 규칙은 "선언했으니 검사되고 있다" 는 착각을 만든다. 새 규칙을 추가할
    때 ``applies_to`` 또는 접미 규칙 중 하나로 반드시 해상되어야 한다.
    """
    r = check_value_ranges(_deployed_abox())
    assert not r.get("unmatched_rules"), (
        f"어떤 DP 에도 닿지 않는 규칙: {r.get('unmatched_rules')} — "
        "applies_to 로 실제 DP 이름을 명시하라"
    )


def test_deployed_rules_declare_units_explicitly():
    """단위가 갈리는 4개 키는 ``applies_to`` 로 명시돼 있다."""
    with open(rules_path("value_ranges.json"), encoding="utf-8") as fh:
        ranges = json.load(fh)["ranges"]
    for key in ("carbonContent", "efficiencyValue", "co2Amount", "wasteQuantity"):
        assert ranges.get(key, {}).get("applies_to"), (
            f"{key} 는 이름으로 단위를 판별할 수 없어 applies_to 가 필요하다"
        )
