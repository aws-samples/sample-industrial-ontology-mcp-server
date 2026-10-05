"""Step 21b — 어디에도 연결되지 않은 **죽은 클래스 스텁** 제거.

## 대상과 근거

실측 (2026-08-11 배포 T-Box): ``GHGScope`` / ``ghgScopeEnum`` 2개가
``check_quality_rules`` 의 isolated_class·naming_class·asymmetric_annotation 을
동시에 유발하고 ``detect_tbox_antipatterns`` lazy_class 로도 잡힌다. 출처는 S2
초안(LLM)이며 **소스코드·설정 어디에도 이 이름이 없다**.

유일한 참조는 서로를 향한 **깨진 자기참조** 다:
``GHGScope owl:equivalentClass "_:ghgScopeEnum"^^xsd:string`` /
``ghgScopeEnum owl:oneOf "(steel:Scope1 …)"^^xsd:string`` — 둘 다 IRI·컬렉션이
아니라 **문자열 리터럴** 이라 추론기·SPARQL 에 아무 의미가 없다. A-Box·추론·
딕셔너리·tacit·CQ·SHACL 어디에도 참조가 없다 (전수 grep 0건).

## lazy class 를 지우면 안 되는 이유 — 22개는 의도된 추상이다

``lazy_class`` 24개 중 22개(``MasterData`` / ``TransactionRecord`` /
``EquipmentManagement`` / ``QualityMeasurement`` …)는 S3 가 만든 **추상 그룹**
으로, DP·OP 를 갖지 않는 것이 설계 의도다. 지우면 계층이 붕괴한다.

그래서 판정은 **6개 신호의 OR** 이고, 하나라도 켜지면 보존한다:

  S1 구조 공리의 object 로 등장 (subClassOf / domain / range / restriction /
     disjoint / oneOf / equivalentClass 의 대상)
  S2 자신이 subClassOf·equivalentClass 의 **subject** 이고 그 대상이 **IRI** 다
     (계층에 자리가 있음). 문자열 리터럴을 가리키는 경우는 자리로 세지 않는다 —
     그것이 ``GHGScope`` 를 영구히 살려두던 원인이다.
  S3 설정(``design_patterns.json`` 등)이 선언한 이름
  S4 tacit TTL 이 참조
  S5 개체(named individual)를 보유하고, 그 개체가 **자기 타입 선언·주석 밖에서
     쓰인다**. 아무도 참조하지 않는 개체는 스텁의 일부이므로 함께 정리한다.
  S6 **A-Box 가 인스턴스를 만들었다.** T-Box 만 보고 "참조 0건" 이라 지우면 그
     인스턴스들이 타입 없는 고아가 된다. ``step_12f`` 와 같은 fail-closed 계약:
     판정 불가(네임스페이스 미설정 / 읽기 실패)면 **아무것도 지우지 않는다.**

각 신호를 하나씩 빼면 무엇이 잘못 지워지는지 실측했다 — 그래서 여섯 개가 모두
필요하다:

  자식(subClassOf) 유무만 보면        → ``MaintenanceManagement`` 삭제 (자식 0)
  rdfs:comment 유무를 넣으면          → ``EquipmentAsset`` 삭제
  설정 선언만 신뢰하면                → ``TransactionRecord`` 삭제 (설정 밖)
  인스턴스 0건을 넣으면               → **65개 중 64개 삭제** (T-Box 는 스키마다)
  A-Box 를 안 보면                    → 구조 신호 없는 클래스의 인스턴스가 고아화

이름을 하드코딩하지 않으므로 도메인-중립이다. 신규 도메인(설정 파일이 빈 상태)에서도
S1·S2 신호만으로 22개가 전부 보존된다.

S6 은 현재 T-Box 에서 아무것도 바꾸지 않는다 (5신호 전무 + A-Box 등장 클래스 0건).
잠재 방어선이다 — S3 는 S7 보다 앞서 돌므로 A-Box 가 아직 없는 것이 정상이고,
그때는 없는 파일을 근거로 전면 보류하지 않고 T-Box 신호만으로 판정한다 (그렇게
하지 않으면 스텝이 영구 no-op 이 된다).
"""
from __future__ import annotations

import logging
import os

import rdflib.collection
from rdflib import RDF, RDFS, Graph, Namespace, URIRef
from rdflib.namespace import OWL

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_SKOS_NS = Namespace("http://www.w3.org/2004/02/skos/core#")

#: 클래스가 "구조 공리의 대상" 으로 등장했다고 보는 predicate.
_STRUCTURAL_PREDS = (
    RDFS.subClassOf, RDFS.domain, RDFS.range,
    OWL.someValuesFrom, OWL.allValuesFrom, OWL.onClass, OWL.onProperty,
    OWL.equivalentClass, OWL.disjointWith, OWL.complementOf,
)


def _local(node) -> str:
    return str(node).split("#")[-1].split("/")[-1]


def _declared_names() -> set[str]:
    """설정 파일이 선언한 클래스 이름 (도메인 그룹·서브그룹·중간 추상)."""
    names: set[str] = set()
    try:
        from tools.ontology_quality import (
            _load_hierarchy_config,
            _load_sub_group_config,
        )
        for cfg in _load_hierarchy_config().values():
            if cfg.get("class"):
                names.add(cfg["class"])
            for table in cfg.get("children_tables") or ():
                names.add(table.replace("_", ""))
        for subgroups in _load_sub_group_config().values():
            for sg in subgroups:
                names.add(sg["class"])
                names.update(sg.get("children") or ())
    except Exception as exc:  # noqa: BLE001 — 설정 없으면 구조 신호만 쓴다
        logger.debug("설정 로드 실패 (구조 신호만 사용): %s", exc)
    return names


def _tacit_referenced_locals() -> set[str]:
    """tacit TTL 이 언급하는 local name 집합.

    ``step_11_tacit_class`` 는 클래스+라벨만 만들고 구조 공리를 붙이지 않으므로
    이 신호가 그 산출물의 **유일한 방어선** 이다.
    """
    names: set[str] = set()
    try:
        import glob

        from config import SOURCE_DIR
        for path in glob.glob(os.path.join(SOURCE_DIR, "tacit", "*.ttl")):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for token in text.replace("<", " ").replace(">", " ").split():
                if ":" in token:
                    names.add(token.rsplit(":", 1)[-1].strip(" ;,.]["))
                elif "#" in token:
                    names.add(token.rsplit("#", 1)[-1].strip(" ;,.]["))
    except Exception as exc:  # noqa: BLE001
        logger.debug("tacit 스캔 실패 (신호 생략): %s", exc)
    return names


#: 개체에 붙어도 "쓰이고 있다" 로 보지 않는 순수 주석 predicate.
_ANNOTATION_ONLY = frozenset({RDF.type, RDFS.label, RDFS.comment, _SKOS_NS.prefLabel})


def _abox_referenced_locals(candidates: set[str]) -> set[str] | None:
    """A-Box 가 실제로 인스턴스를 만든 클래스 local name 집합.

    T-Box 만 보고 "구조 참조 0건" 이라고 판정하면, A-Box 에 인스턴스가 살아 있는
    클래스를 지울 수 있다. 삭제되면 그 인스턴스들은 타입이 사라진 고아가 된다.

    ``step_12f._abox_used_predicates`` 와 같은 계약을 따른다 (후보만 정규식으로
    찾아 500MB+ 파일을 한 번 순차 읽기, prefix 는 설정에서 — 하드코딩하면 타
    도메인에서 조용히 0건이 되고 호출부가 그 0 을 "미사용" 으로 읽는다).

    Returns:
        사용 중인 local name 집합. ``None`` — **판정 불가**(A-Box 파일 없음 /
        네임스페이스 미설정 / 읽기 실패). 호출부는 이때 **삭제를 보류** 해야 한다.
    """
    from domain.graph_utils import domain_predicate_pattern

    if not candidates:
        return set()
    try:
        from config import ABOX_PATH
    except Exception as exc:  # noqa: BLE001
        logger.debug("step_21b: ABOX_PATH 조회 실패: %s", exc)
        return None

    paths = [ABOX_PATH]
    master = os.path.join(os.path.dirname(ABOX_PATH), "master_data.ttl")
    if os.path.exists(master):
        paths.append(master)
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        # A-Box 를 아직 만들지 않은 정상 상태 (S3 는 S7 보다 앞선다). 이때는
        # 구조·설정·tacit 신호만으로 판정하는 것이 맞다 — 없는 파일을 근거로
        # 전면 보류하면 스텝이 영구 no-op 이 된다.
        return set()

    pattern = domain_predicate_pattern(candidates)
    if pattern is None:
        logger.warning(
            "step_21b: 도메인 네임스페이스 설정이 비어 A-Box 사용 판정 불가 — "
            "죽은 스텁 제거를 보류한다",
        )
        return None

    used: set[str] = set()
    for path in existing:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    used.update(pattern.findall(line))
        except OSError as exc:
            logger.warning("step_21b: A-Box 스캔 실패 (%s) — 제거 보류: %s", path, exc)
            return None
        if used == candidates:
            break
    return used


def _individual_is_used(g: Graph, ind: URIRef) -> bool:
    """개체가 **자기 타입 선언과 주석 밖에서** 쓰이고 있는가.

    ``Scope1 a GHGScope ; rdfs:label "Scope 1"`` 만 있으면 그 개체는 스텁의 일부다 —
    누구도 값으로 쓰지 않으므로 클래스와 함께 정리해야 한다 (실측: A-Box·추론·
    딕셔너리·tacit 어디에도 ``Scope1/2/3`` 참조 0건).

    반대로 다른 트리플이 그 개체를 **object 로 쓰거나** (``X hasScope Scope1``),
    개체 자신이 주석 아닌 프로퍼티를 가지면 (``Scope1 ghgFactor 2.5``) 살아있다.
    """
    for subj, pred in g.subject_predicates(ind):
        if subj != ind or pred != RDF.type:
            return True                      # 남이 참조 / 자기 타입 선언이 아닌 유입
    return any(pred not in _ANNOTATION_ONLY for pred, _ in g.predicate_objects(ind))


def apply(g: Graph, ctx: StepContext) -> StepResult:
    if (os.getenv("TBOX_DEAD_STUB_PRUNE") or "true").strip().lower() in (
        "false", "0", "off", "no",
    ):
        return StepResult(
            name="step_21b_dead_stub_prune",
            stats={"dead_stub_prune_disabled": True},
            step_number="21b", step_label="dead_stub_prune",
        )

    before = len(g)
    steel_str = ctx.domain_ns
    declared = _declared_names()
    tacit = _tacit_referenced_locals()

    # S1 — 구조 공리의 object 로 등장한 클래스 (자기 자신 제외).
    referenced: set[URIRef] = set()
    for pred in _STRUCTURAL_PREDS:
        for subj, _, obj in g.triples((None, pred, None)):
            if isinstance(obj, URIRef) and obj != subj:
                referenced.add(obj)
    # disjoint 그룹 멤버 / oneOf 컬렉션도 구조 참조로 본다.
    for node in g.subjects(RDF.type, OWL.AllDisjointClasses):
        for members in g.objects(node, OWL.members):
            try:
                referenced.update(
                    m for m in rdflib.collection.Collection(g, members)
                    if isinstance(m, URIRef)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("AllDisjointClasses 전개 실패: %s", exc)
    for _, _, obj in g.triples((None, OWL.oneOf, None)):
        if isinstance(obj, URIRef):
            try:
                referenced.update(
                    m for m in rdflib.collection.Collection(g, obj)
                    if isinstance(m, URIRef)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("oneOf 전개 실패: %s", exc)

    # pass 1 — T-Box 신호(S1~S5)만으로 후보를 모은다. A-Box 스캔은 500MB+ 파일
    # 순차 읽기라 후보가 확정된 뒤 **한 번만** 한다.
    candidates: list[tuple[URIRef, str, list[URIRef]]] = []
    for cls in list(g.subjects(RDF.type, OWL.Class)):
        if not (isinstance(cls, URIRef) and str(cls).startswith(steel_str)):
            continue
        name = _local(cls)

        if cls in referenced:
            continue                                        # S1
        # S2 — 단, **URIRef** 만 자리로 인정한다. LLM 이
        # `owl:equivalentClass "_:ghgScopeEnum"^^xsd:string` 처럼 IRI 가 아니라
        # 문자열 리터럴을 쓰면 추론기·SPARQL 에 아무 의미가 없고, 그것을 자리로
        # 세면 죽은 스텁이 영구히 살아남는다 (실측).
        has_place = any(
            isinstance(o, URIRef) and str(o).startswith(("http://", "https://", "urn:"))
            for pred in (RDFS.subClassOf, OWL.equivalentClass)
            for o in g.objects(cls, pred)
        )
        if has_place:
            continue                                        # S2
        if name in declared:
            continue                                        # S3
        if name in tacit:
            continue                                        # S4
        # S5 — 개체 보유. 단 그 개체가 **다른 트리플에서 쓰여야** 산 것으로 본다.
        # `Scope1 a GHGScope` 만 있고 아무도 Scope1 을 참조하지 않으면 그 개체는
        # 스텁의 일부이므로 클래스와 함께 정리 대상이다 (실측: A-Box·추론·딕셔너리
        # 어디에도 Scope1/2/3 참조 0건).
        individuals = [s for s in g.subjects(RDF.type, cls) if isinstance(s, URIRef)]
        if any(_individual_is_used(g, ind) for ind in individuals):
            continue                                        # S5

        candidates.append((cls, name, individuals))

    # pass 2 — S6: A-Box 가 인스턴스를 만든 클래스는 보존한다. **fail-closed**:
    # 판정 불가(None)면 아무것도 지우지 않는다. T-Box 만 보고 "참조 0건" 이라고
    # 지우면 A-Box 인스턴스가 타입 없는 고아로 남는다.
    abox_used = _abox_referenced_locals({name for _, name, _ in candidates})
    if abox_used is None:
        logger.warning(
            "Step 21b: A-Box 사용 판정 불가 — 죽은 스텁 후보 %d개 전부 보류 (%s)",
            len(candidates), sorted(name for _, name, _ in candidates)[:5],
        )
        return StepResult(
            name="step_21b_dead_stub_prune",
            stats={
                "dead_stubs_pruned": 0,
                "dead_stub_names": [],
                "dead_stub_withheld_abox_unknown": len(candidates),
            },
            triples_delta=0,
            step_number="21b",
            step_label="dead_stub_prune",
        )

    pruned: list[str] = []
    kept_by_abox: list[str] = []
    for cls, name, individuals in candidates:
        if name in abox_used:
            kept_by_abox.append(name)                       # S6
            continue
        # 어떤 신호도 없다 — 죽은 스텁 (개체까지 함께 정리).
        for ind in individuals:
            for p, o in list(g.predicate_objects(ind)):
                g.remove((ind, p, o))
            for s, p in list(g.subject_predicates(ind)):
                g.remove((s, p, ind))
        for p, o in list(g.predicate_objects(cls)):
            g.remove((cls, p, o))
        for s, p in list(g.subject_predicates(cls)):
            g.remove((s, p, cls))
        pruned.append(name)

    if pruned:
        logger.info(
            "Step 21b: 죽은 클래스 스텁 %d개 제거 — 구조 공리·설정·tacit·개체·"
            "A-Box 어디에도 참조가 없다: %s", len(pruned), sorted(pruned),
        )
    if kept_by_abox:
        logger.info(
            "Step 21b: A-Box 가 인스턴스를 가진 클래스 %d개 보존 (T-Box 구조 신호는 "
            "없었다 — 지웠다면 인스턴스가 타입 없는 고아가 됐다): %s",
            len(kept_by_abox), sorted(kept_by_abox),
        )

    return StepResult(
        name="step_21b_dead_stub_prune",
        stats={
            "dead_stubs_pruned": len(pruned),
            "dead_stub_names": sorted(pruned),
            "dead_stub_kept_by_abox": sorted(kept_by_abox),
        },
        triples_delta=len(g) - before,
        step_number="21b",
        step_label="dead_stub_prune",
    )
