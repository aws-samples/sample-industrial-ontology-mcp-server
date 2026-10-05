"""rules/swrl/*.swrl 이 참조하는 이름이 실제 어휘에 존재하는가.

2026-08-28 실측. 규칙 3개가 참조한 이름 중 4개가 T-Box·tbox_extensions.ttl 양쪽에
없었고, ``set_as_rule`` 이 전부 거부해 **rules_loaded=0** 이었다::

    hasAlarmEvent          → 대응 관계가 아예 없다 (설비-알람은 Tag 경유 2홉)
    carbonContentPercent   → chemicalAnalysisCarbonPercent   (Path B 전환)
    isChemicalAnalysisOf   → hasChemicalAnalysisProduct
    steelGrade             → productMasterSteelGrade         (Path B 전환)

Path B (class-specific DP) 전환이 리터럴 이름을 낡게 만들었는데 아무 게이트도
그것을 보지 않았다. ``rules/`` 는 git 추적, ``data/generated/`` 는 gitignore 라
구조적으로 어긋난다.

## 왜 이 테스트가 필요한가

``run_swrl_inference`` 는 ``rules_loaded=0`` 일 때도 ``success=true`` 를 준다
(``if loaded == 0: return`` 가드). 즉 **어휘가 썩어도 조용히 통과**한다 — S8.5 가
opt-in 이라 아무도 눈치채지 못했다. 이 테스트가 그 침묵을 깬다.

## 이 테스트의 방향

"규칙이 로드된다" 만 주장하면 규칙 파일을 비워도 통과한다. 세 축을 함께 고정한다:

* 존재 — 참조 이름이 T-Box 또는 extensions 에 선언돼 있다
* 비공허 — 규칙이 실제로 존재하고 술어를 참조한다 (빈 파일로 통과 못 함)
* 근거 — 관계 이름이 도메인/레인지상 규칙의 변수 연결과 맞다
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS
from tools.swrl_inference import (
    _load_swrl_rules,
    _normalize_dl_for_owlready,
    _referenced_names,
)

SWRL_DIR = "rules/swrl"
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")
EXTENSIONS = pathlib.Path(SWRL_DIR) / "tbox_extensions.ttl"

_DECLARED_TYPES = (
    OWL.Class,
    OWL.ObjectProperty,
    OWL.DatatypeProperty,
    OWL.AnnotationProperty,
    RDF.Property,
)


@pytest.fixture(scope="module")
def vocabulary() -> set[str]:
    """Local names declared by the T-Box plus the SWRL extension file."""
    if not TBOX.exists():
        pytest.skip("T-Box 산출물 없음")
    graph = Graph()
    graph.parse(str(TBOX), format="turtle")
    if EXTENSIONS.exists():
        graph.parse(str(EXTENSIONS), format="turtle")

    names = set()
    for kind in _DECLARED_TYPES:
        for subject in graph.subjects(RDF.type, kind):
            text = str(subject)
            if text.startswith(str(DOMAIN_NS)):
                names.add(text[len(str(DOMAIN_NS)):])
    return names


@pytest.fixture(scope="module")
def referenced() -> tuple[str, ...]:
    rules = _load_swrl_rules(SWRL_DIR)
    normalized = [_normalize_dl_for_owlready(r["dl"], {"steel": str(DOMAIN_NS)}) for r in rules]
    return _referenced_names(normalized)


@pytest.fixture(scope="module")
def tbox_graph() -> Graph:
    if not TBOX.exists():
        pytest.skip("T-Box 산출물 없음")
    graph = Graph()
    graph.parse(str(TBOX), format="turtle")
    if EXTENSIONS.exists():
        graph.parse(str(EXTENSIONS), format="turtle")
    return graph


# ── 존재: 참조 이름이 선언돼 있다 ───────────────────────────────────────


def test_every_referenced_name_is_declared(referenced, vocabulary):
    """THE REGRESSION: 규칙이 쓰는 이름이 전부 선언돼 있다.

    미선언 이름 하나가 그 규칙 전체를 로드 실패시키고, 도구는 그래도
    ``success=true`` 를 반환한다.
    """
    missing = [name for name in referenced if name not in vocabulary]

    assert not missing, (
        f"미선언 이름 {len(missing)}건: {missing}\n"
        "규칙이 로드되지 않으므로 SWRL 파생이 0건이 된다. "
        "T-Box 의 현재 이름으로 갱신하거나 tbox_extensions.ttl 에 선언하라."
    )


# ── 비공허: 실제로 검사할 것이 있다 ─────────────────────────────────────


def test_rules_exist(referenced):
    """규칙 파일이 비면 위 테스트가 공허하게 통과한다."""
    assert len(_load_swrl_rules(SWRL_DIR)) >= 1, "SWRL 규칙이 없다"
    assert len(referenced) >= 3, f"참조 이름이 {len(referenced)}개뿐이다"


def test_extensions_declare_derived_classes(vocabulary):
    """파생 클래스는 T-Box 가 모르므로 extensions 가 선언해야 한다.

    ``PotentialFailure`` / ``QualityViolation`` 은 SWRL 이 만들어내는 개념이라
    S2 가 생성하지 않는다. 이것이 tbox_extensions.ttl 의 존재 이유다.
    """
    for name in ("PotentialFailure", "QualityViolation"):
        assert name in vocabulary, f"{name} 선언이 없다 — 규칙 head 가 해석되지 않는다"


# ── 근거: 관계 방향이 규칙의 변수 연결과 맞다 ───────────────────────────


def _domains(graph: Graph, name: str) -> set[str]:
    return {
        str(o)[len(str(DOMAIN_NS)):]
        for o in graph.objects(URIRef(str(DOMAIN_NS) + name), RDFS.domain)
        if str(o).startswith(str(DOMAIN_NS))
    }


def _ranges(graph: Graph, name: str) -> set[str]:
    return {
        str(o)[len(str(DOMAIN_NS)):]
        for o in graph.objects(URIRef(str(DOMAIN_NS) + name), RDFS.range)
        if str(o).startswith(str(DOMAIN_NS))
    }


def test_alarm_path_goes_through_tag(tbox_graph, vocabulary):
    """설비-알람은 2홉이다 — 직접 관계를 쓰면 로드 실패한다.

    실측: AlarmEvents 는 TagMaster 로만 연결된다. 이 2홉 변수 연결이 OWL RL 로
    표현되지 않는 것이 SWRL 을 쓰는 이유다::

        EquipmentMaster ←tagEquipment─ TagMaster ─isAlarmTagOf→ AlarmEvents
    """
    assert "tagEquipment" in vocabulary and "isAlarmTagOf" in vocabulary

    assert _ranges(tbox_graph, "tagEquipment") == {"EquipmentMaster"}
    assert _domains(tbox_graph, "tagEquipment") == {"TagMaster"}
    assert _domains(tbox_graph, "isAlarmTagOf") == {"TagMaster"}
    assert _ranges(tbox_graph, "isAlarmTagOf") == {"AlarmEvents"}

    # 직접 관계는 없어야 한다 — 있으면 규칙을 1홉으로 단순화할 수 있다는 신호다.
    assert "hasAlarmEvent" not in vocabulary, (
        "직접 관계가 생겼다 — failure_prediction.swrl 의 2홉 우회를 재검토하라"
    )


def test_quality_rule_names_match_path_b(tbox_graph, vocabulary):
    """품질 규칙의 DP 가 class-specific (Path B) 이름인가.

    generic 이름(``carbonContentPercent``)은 Path B 전환으로 사라졌다. 규칙이
    그것을 참조하면 조용히 0건이 된다.
    """
    assert "chemicalAnalysisCarbonPercent" in vocabulary
    assert _domains(tbox_graph, "chemicalAnalysisCarbonPercent") == {"ChemicalAnalysis"}
    assert _domains(tbox_graph, "productMasterSteelGrade") == {"ProductMaster"}

    # ChemicalAnalysis → ProductMaster 방향이어야 변수 ?p 가 제품에 묶인다.
    assert _domains(tbox_graph, "hasChemicalAnalysisProduct") == {"ChemicalAnalysis"}
    assert _ranges(tbox_graph, "hasChemicalAnalysisProduct") == {"ProductMaster"}


def test_generic_predecessor_names_are_gone(vocabulary):
    """Path B 이전 generic 이름이 되살아나지 않았는가 (역방향 감시).

    되살아나면 어느 쪽이 정본인지 흐려지고, 규칙이 조용히 빈 결과를 낸다.
    """
    for stale in ("carbonContentPercent", "isChemicalAnalysisOf", "steelGrade"):
        assert stale not in vocabulary, f"{stale} 가 다시 선언됐다 — 이름 정본을 확인하라"
