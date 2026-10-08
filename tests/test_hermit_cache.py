"""HermiT 결과 공유 캐시 테스트."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

try:
    import owlready2  # noqa: F401
    _OK = True
except Exception:
    _OK = False

pytestmark = pytest.mark.skipif(not _OK, reason="owlready2 미설치")


def _stub_pipeline():
    """HermiT 경로 전체를 가벼운 MagicMock으로 대체."""
    from owlready2 import OwlReadyInconsistentOntologyError  # noqa: F401
    onto = MagicMock()
    onto.classes.return_value = []
    onto.object_properties.return_value = []
    onto.data_properties.return_value = []
    return onto


@pytest.fixture
def isolated_hermit_caches(monkeypatch):
    """테스트가 결과·자기진단 캐시를 물려받거나 다른 테스트에 남기지 않게 한다."""
    from tools import owl_reasoner as r

    monkeypatch.setattr(r, "_HERMIT_RESULT_CACHE", {})
    monkeypatch.setattr(r, "_BLINDNESS_PROBE_CACHE", {})
    return r


@pytest.mark.parametrize("warm_probe_cache", [False, True], ids=["cold_probe", "warm_probe"])
def test_validate_owl_consistency_cache_hits_second_call(
    isolated_hermit_caches, warm_probe_cache,
):
    r = isolated_hermit_caches
    stub_onto = _stub_pipeline()
    with patch.object(r, "_cleanup_world"), \
         patch.object(r, "_ttl_to_owlready", return_value=(stub_onto, "/tmp/x.owl")), \
         patch.object(r, "sync_reasoner_hermit") as sync, \
         patch.object(r, "_collect_unsatisfiable", return_value=[]), \
         patch.object(r, "_collect_inferred_hierarchy", return_value=[]), \
         patch.object(r, "_safe_unlink"), \
         patch.object(r, "_load_ttl_content", return_value="same_ttl"):
        if warm_probe_cache:
            r.check_reasoner_blindness()
            sync.reset_mock()
        out1 = json.loads(r.validate_owl_consistency("same_ttl"))
        calls_after_first = sync.call_count
        out2 = json.loads(r.validate_owl_consistency("same_ttl"))
    assert out1["success"] is True
    assert out1["consistent"] is True
    assert out2["consistent"] is True
    assert out2 == out1
    # 첫 호출의 자기진단 횟수와 무관하게 캐시 히트에서는 추가 추론이 없어야 한다.
    assert calls_after_first > 0
    assert sync.call_count == calls_after_first


def test_classify_tbox_cache_hits_second_call(isolated_hermit_caches):
    r = isolated_hermit_caches
    stub_onto = _stub_pipeline()
    with patch.object(r, "_cleanup_world"), \
         patch.object(r, "_ttl_to_owlready", return_value=(stub_onto, "/tmp/x.owl")), \
         patch.object(r, "sync_reasoner_hermit") as sync, \
         patch.object(r, "_collect_unsatisfiable", return_value=[]), \
         patch.object(r, "_safe_unlink"), \
         patch.object(r, "_load_ttl_content", return_value="same_ttl_c"):
        out1 = json.loads(r.classify_tbox("same_ttl_c"))
        calls_after_first = sync.call_count
        out2 = json.loads(r.classify_tbox("same_ttl_c"))
    assert out1["success"] is True
    assert out2 == out1
    assert calls_after_first > 0
    assert sync.call_count == calls_after_first


@pytest.mark.parametrize("warm_probe_cache", [False, True], ids=["cold_probe", "warm_probe"])
def test_different_ttl_does_not_share_cache(isolated_hermit_caches, warm_probe_cache):
    r = isolated_hermit_caches
    stub_onto = _stub_pipeline()
    # _load_ttl_content는 호출마다 인자를 그대로 돌려주도록.
    with patch.object(r, "_cleanup_world"), \
         patch.object(r, "_ttl_to_owlready", return_value=(stub_onto, "/tmp/x.owl")), \
         patch.object(r, "sync_reasoner_hermit") as sync, \
         patch.object(r, "_collect_unsatisfiable", return_value=[]), \
         patch.object(r, "_collect_inferred_hierarchy", return_value=[]), \
         patch.object(r, "_safe_unlink"), \
         patch.object(r, "_load_ttl_content", side_effect=lambda c, p: c):
        if warm_probe_cache:
            r.check_reasoner_blindness()
            sync.reset_mock()
        out1 = json.loads(r.validate_owl_consistency("ttl_a"))
        calls_after_first = sync.call_count
        out2 = json.loads(r.validate_owl_consistency("ttl_b"))
        calls_after_second = sync.call_count
        out3 = json.loads(r.validate_owl_consistency("ttl_a"))
    assert out1["consistent"] is True
    assert out2["consistent"] is True
    # 자기진단 캐시는 첫 호출에서 채워지고, 새 TTL에는 본 추론이 다시 필요하다.
    assert calls_after_first > 0
    assert calls_after_second == calls_after_first + 1
    # 다른 TTL을 처리한 뒤에도 이전 TTL의 결과를 재사용한다.
    assert sync.call_count == calls_after_second
    assert out3 == out1


# ── 검증 범위 각인 (2026-08-30) ───────────────────────────────────────────
#
# validate_owl_consistency 는 기본적으로 **T-Box 만** 읽는다 (owl_reasoner.py 에
# ABOX_PATH 참조 0건, 기본 경로가 TBOX_PATH). 그런데 응답에 그 사실이 없어
# ``consistent: true`` 가 "KG 가 일관적" 으로 읽혔다.
#
# A-Box 를 로드하는 옵션은 만들지 않았다 — 실측상 불가능하고 무의미하다:
#   · T-Box + a_box.ttl(715,864 트리플): HermiT timeout 3000s 미반환, RSS 5.8GB
#   · 클래스별 개체 1개(10,143 트리플)조차 30분 미반환 (T-Box 단독은 47.6s)
#   · 반환해도 검출력 0 — OWL 은 No-UNA 이고 산출물에 differentFrom 0건이다
#     (최소 재현 14 트리플: differentFrom 없음 → true / 한 줄 추가 → false)


@pytest.mark.requires_java
def test_consistency_response_records_its_scope():
    """THE REGRESSION: 무엇을 로드했는지 응답에 남는다."""
    import json

    from tools.owl_reasoner import validate_owl_consistency

    data = json.loads(validate_owl_consistency())
    assert "scope" in data, "검증 범위가 각인되지 않았다"
    assert data["scope"]["scope"] == "tbox_only"
    assert data["scope"]["abox_loaded"] is False
    assert data["scope"]["data_graph"].endswith(".ttl")


def test_explicit_input_is_marked_differently():
    """명시 입력은 tbox_only 가 아니다 — 범위를 정직하게 구분한다."""
    from tools.owl_reasoner import _consistency_scope

    assert _consistency_scope("@prefix x: <u#> .", "")["scope"] == "explicit_content"
    assert _consistency_scope("", "/tmp/x.ttl")["scope"] == "explicit_path"
    assert _consistency_scope("", "")["scope"] == "tbox_only"


@pytest.mark.requires_java
def test_cardinality_blindness_is_reported():
    """카디널리티 축을 이 도구가 못 본다는 사실을 각인한다."""
    import json

    from tools.owl_reasoner import validate_owl_consistency

    data = json.loads(validate_owl_consistency())
    cb = data["cardinality_blindness"]
    if cb["cardinality_axioms"] == 0:
        # 공리가 없으면 눈멂이 무의미 — None 으로 구분한다 (0회를 0회로).
        assert cb["detectable"] is None
    else:
        assert cb["detectable"] is False, (
            "카디널리티 공리가 있는데 검출 가능으로 보고했다"
        )
        assert "validate_owl_cardinality" in cb["reason"], (
            "위임 대상을 안내하지 않았다"
        )


def test_cardinality_blindness_notes_una_presence():
    """UNA(differentFrom)가 있으면 그 사실을 기록한다 — 판정 근거가 바뀐다."""
    from tools.owl_reasoner import _cardinality_blindness

    without = _cardinality_blindness('x:C rdfs:subClassOf [ owl:maxCardinality 1 ] .')
    assert without["cardinality_axioms"] == 1
    assert without["una_present"] is False

    with_una = _cardinality_blindness(
        'x:C rdfs:subClassOf [ owl:maxCardinality 1 ] . x:a owl:differentFrom x:b .')
    assert with_una["una_present"] is True


@pytest.mark.requires_java
def test_scope_survives_the_result_cache():
    """캐시 hit 경로에도 같은 키가 있어야 한다.

    이 리포의 함정: 조기 반환/캐시 경로에 필드가 없으면 소비자가 키 부재를 "정상"
    으로 오독한다.
    """
    import json

    from tools.owl_reasoner import validate_owl_consistency

    first = json.loads(validate_owl_consistency())
    second = json.loads(validate_owl_consistency())   # 캐시 hit
    for key in ("scope", "cardinality_blindness"):
        assert key in first and key in second, f"{key} 가 캐시 경로에서 사라졌다"
    assert first["scope"] == second["scope"]
