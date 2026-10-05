"""OWL DL 추론 기반 검증 도구 — HermiT + Pellet (owlready2)

T-Box 파이프라인:
  - HermiT: 일관성 검사 (consistency), 분류 (classification), unsatisfiable class 탐지
  - Pellet: 추론 근거 설명 (justification), 프로퍼티 값 추론

A-Box 파이프라인:
  - HermiT: 기본 일관성 검증
  - Pellet: 명시적 opt-in 실현 (realization), 인스턴스 타입 정합성, 프로퍼티 값 추론

Pellet 경로는 ``PELLET_REALISATION_ENABLED=true``일 때만 실행한다.
Java 실행 파일은 ``JAVA_EXE`` 환경변수 또는 config.py에서 설정한다.

``owlready2`` 는 ``JAVA_EXE`` / ``JAVA_MEMORY`` 를 **모듈 전역**으로 읽으므로 그 설정이
owlready2 심볼 import 보다 앞에 와야 한다. 그래서 이 파일은 E402 (module level import
not at top) 를 구조적으로 피할 수 없다 — 아래 ruff 억제는 그 이유이며, 선재 부채
(SIM103/SIM105/SIM115) 도 함께 남아 있다. 새 코드에 적용하지 말 것.
"""
# ruff: noqa: E402, SIM103, SIM105, SIM115

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, ONTOLOGY_URI
from domain.uri_conventions import local_name as _local_name
from tools.common import error_response, resolve_child_path

logger = logging.getLogger(__name__)


# ── HermiT 결과 캐시 (동일 TTL 내 validate + classify 중복 JVM 호출 회피) ─────
# key = (tool_name, sha256(ttl)[:16]) → JSON 문자열
_HERMIT_RESULT_CACHE: dict[tuple[str, str], str] = {}
_HERMIT_CACHE_MAX_ENTRIES = 8


def _hermit_cache_key(tool: str, ttl: str) -> tuple[str, str]:
    import hashlib as _hl
    return (tool, _hl.sha256(ttl.encode("utf-8")).hexdigest()[:16])


def _hermit_cache_put(key: tuple[str, str], value: str) -> None:
    # 가장 오래된 엔트리부터 제거 (dict 삽입 순서 LRU 근사).
    while len(_HERMIT_RESULT_CACHE) >= _HERMIT_CACHE_MAX_ENTRIES and key not in _HERMIT_RESULT_CACHE:
        _HERMIT_RESULT_CACHE.pop(next(iter(_HERMIT_RESULT_CACHE)))
    _HERMIT_RESULT_CACHE[key] = value


def clear_hermit_cache() -> None:
    """테스트 helper + 명시적 초기화 지점."""
    _HERMIT_RESULT_CACHE.clear()


def _safe_unlink(path: str) -> None:
    """임시 파일을 안전하게 삭제한다. Windows에서 Java 프로세스가 파일을 잡고 있으면 무시."""
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except PermissionError:
        logger.warning("임시 파일 삭제 실패 (잠금): %s", path)

# ── Java 설정 ──────────────────────────────────────

# owlready2 import 전에 JAVA_EXE를 설정해야 함
_JAVA_EXE = os.getenv("JAVA_EXE", "")
if _JAVA_EXE:
    import owlready2
    owlready2.JAVA_EXE = _JAVA_EXE

# JVM heap — owlready2 기본 2000MB 는 이 프로젝트 규모에서 부족하다 (실측: 1.37M
# 트리플 추론 그래프에서 Pellet 이 OutOfMemoryError). reasoning 모듈의 모듈 전역이라
# import 후 대입해야 하고, 명령줄은 호출 시점에 조립되므로 여기서 한 번 설정하면 된다.
# 파싱은 config._safe_int 를 쓴다 — 사본을 두면 두 곳의 방어 수준이 어긋난다.
from config import GENERATED_TBOX_DIR  # noqa: E402
from config import JAVA_MEMORY_MB as _JAVA_MEMORY_MB

if _JAVA_MEMORY_MB > 0:
    import owlready2.reasoning as _owlready_reasoning
    _owlready_reasoning.JAVA_MEMORY = _JAVA_MEMORY_MB


import owlready2
from owlready2 import (
    Nothing,
    OwlReadyInconsistentOntologyError,
    default_world,
    get_ontology,
    sync_reasoner_pellet,
)
from owlready2 import (
    sync_reasoner as sync_reasoner_hermit,
)


def _register_local_imports_dir() -> None:
    """Register local directory for owl:imports resolution.

    owlready2 looks up onto_path entries by IRI's last path segment.
    Examples (IOF):
      https://spec.industrialontologies.org/ontology/core/Core/      → Core(.rdf|.owl)
      https://spec.industrialontologies.org/ontology/maintenance/    → Maintenance(.rdf|.owl)
      https://spec.industrialontologies.org/ontology/supplychain/    → SupplyChain(.rdf|.owl)

    Override via env IOF_LOCAL_DIR (or domain-neutral OWL_IMPORT_LOCAL_DIR);
    falls back to <project>/data/external, then to the shipped reference dir.

    The reference fallback matters: `data/external` does not exist in a fresh
    checkout, so `onto_path` stayed empty and every `owl:imports` went to the
    network. The shipped `data/source/reference` holds the very files the
    imports name (measured 2026-08-18: Core.rdf resolves the `…/core/Core/`
    ontology IRI offline, 3,466 triples), so registering it makes HermiT work
    without network access.
    """
    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.getenv("OWL_IMPORT_LOCAL_DIR"),
        os.getenv("IOF_LOCAL_DIR"),
        os.path.join(_project_root, "data", "external"),
        os.path.join(_project_root, "data", "source", "reference"),
    ]
    for d in candidates:
        if not d:
            continue
        if os.path.isdir(d) and d not in owlready2.onto_path:
            owlready2.onto_path.append(d)
            logger.info("owl:imports 로컬 캐시 등록: %s", d)
            return


_register_local_imports_dir()

# ── 헬퍼 ──────────────────────────────────────────


def _anonymize_named_restrictions(g) -> int:
    """명명 IRI ``owl:Restriction`` 을 익명 bnode 로 바꾼다 → 바꾼 개수.

    ## 왜 변환 경계에서만 하는가 (2026-08-30)

    owlready2 는 IRI 노드를 ``ThingClass`` 로 만들 뿐 ``owl:Restriction`` 타입을
    클래스 표현식으로 **재구성하지 않는다**. 그래서 ``:D owl:equivalentClass :R``
    은 HermiT 에게 "이름 있는 두 클래스가 동등" 으로만 보이고 ``onProperty`` /
    ``hasValue`` 제약은 어디에도 반영되지 않는다 —
    :func:`check_reasoner_blindness` 가 실증하는 눈멂이다.

    **T-Box 파일은 건드리지 않는다.** 명명 형태는 클래스 열거 오염(skolemize 된
    Restriction 이 ``owl:Class`` 로 세어져 지표를 부풀리는 문제)을 막으려고
    ``step_19`` 가 의도적으로 만든 것이고, 배제 헬퍼가 23곳에 깔려 있다. 원본을
    바꾸면 그 전부를 동시에 손대야 한다. 그래서 **추론기에 넘기는 사본에서만**
    익명화한다 — 검증기와 실행기가 같은 의미를 보게 하는 최소 개입이다.

    ``owl:Class`` 로도 타입된 노드는 건드리지 않는다: 그것은 다른 공리가 이름으로
    참조하는 실체일 수 있고, 익명화하면 그 참조가 끊긴다.
    """
    from rdflib import OWL, RDF, BNode, URIRef

    named = [
        s for s in g.subjects(RDF.type, OWL.Restriction)
        if isinstance(s, URIRef) and (s, RDF.type, OWL.Class) not in g
    ]
    for old in named:
        new = BNode()
        for p, o in list(g.predicate_objects(old)):
            g.remove((old, p, o))
            g.add((new, p, o))
        for s, p in list(g.subject_predicates(old)):
            g.remove((s, p, old))
            g.add((s, p, new))
    return len(named)


def _ttl_to_owlready(ttl_content: str, onto_iri: str = ONTOLOGY_URI):
    """TTL 문자열을 owlready2 Ontology로 로드한다.

    owlready2는 RDF/XML을 선호하므로 rdflib로 중간 변환 후 로드.

    명명 Restriction 은 로드 전에 익명화한다 (:func:`_anonymize_named_restrictions`)
    — 그렇지 않으면 추론기가 제약을 보지 못하고 ``consistent: true`` 를 반환한다.
    ``OWL_KEEP_NAMED_RESTRICTIONS=true`` 로 이전 동작을 되돌릴 수 있다 (A/B 비교용).
    """
    from domain.tbox_utils import _new_graph

    # rdflib로 TTL 파싱 → RDF/XML 변환
    g = _new_graph()
    g.parse(data=ttl_content, format="turtle")
    if os.getenv(
        "OWL_KEEP_NAMED_RESTRICTIONS", "false",
    ).strip().lower() not in ("true", "1", "yes"):
        anonymized = _anonymize_named_restrictions(g)
        if anonymized:
            logger.info(
                "추론기 입력에서 명명 Restriction %d개를 익명화했다 — owlready2 는 "
                "IRI Restriction 을 클래스 표현식으로 재구성하지 않으므로 "
                "익명화하지 않으면 제약이 추론에 반영되지 않는다 (T-Box 파일은 "
                "그대로 유지된다)", anonymized,
            )
    rdfxml = g.serialize(format="xml")

    # 임시 파일로 저장 후 owlready2 로드
    tmp = tempfile.NamedTemporaryFile(suffix=".owl", delete=False, mode="w", encoding="utf-8")
    tmp.write(rdfxml)
    tmp.close()

    try:
        onto = get_ontology(Path(tmp.name).resolve().as_uri()).load()
        return onto, tmp.name
    except Exception:
        _safe_unlink(tmp.name)
        raise


#: 검증기 눈멂 probe 결과 캐시 — 같은 프로세스에서 반복 호출을 막는다
#: (probe 1회에 HermiT 2회 실행 = 수 초).
_BLINDNESS_PROBE_CACHE: dict[str, dict] = {}

#: probe 픽스처 — **의도적으로 모순인** 최소 온톨로지 2벌.
#:
#: 두 TTL 은 의미가 완전히 같다: ``:D`` 는 "p 값이 X 인 것" 과 동등하고, ``:C`` 와
#: ``:D`` 는 disjoint 이며, ``:i`` 는 ``:C`` 이면서 ``p = X`` 다 → ``:i`` 가 C 와 D 에
#: 동시에 속하므로 **모순**이다. 차이는 Restriction 을 어떻게 적는가 하나다:
#:
#: * ``_PROBE_EXAMPLE_NAMED``  — ``:R a owl:Restriction`` (명명 IRI)
#: * ``_PROBE_EXAMPLE_ANON``   — ``[ a owl:Restriction ... ]`` (익명 bnode)
#:
#: 정상 추론기라면 **둘 다** INCONSISTENT 다. 명명 쪽만 CONSISTENT 로 나오면
#: 그 스택이 명명 Restriction 을 클래스 표현식으로 재구성하지 못한다는 뜻이고,
#: 배포 T-Box 의 Restriction 74개가 전부 명명형이므로 그 온톨로지에 대한
#: ``consistent: true`` 는 **아무 증거가 아니다**.
_PROBE_EXAMPLE_PREAMBLE = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix : <http://ex.org/probe#> .
<http://ex.org/probe> a owl:Ontology .
:C a owl:Class .
:D a owl:Class .
:C owl:disjointWith :D .
:p a owl:DatatypeProperty , owl:FunctionalProperty ;
    rdfs:domain :C ; rdfs:range xsd:string .
:i a :C ; :p "X" .
"""

_PROBE_EXAMPLE_NAMED = _PROBE_EXAMPLE_PREAMBLE + """
:R a owl:Restriction ; owl:onProperty :p ; owl:hasValue "X" .
:D owl:equivalentClass :R .
"""

_PROBE_EXAMPLE_ANON = _PROBE_EXAMPLE_PREAMBLE + """
:D owl:equivalentClass [ a owl:Restriction ; owl:onProperty :p ; owl:hasValue "X" ] .
"""


def _probe_detects_contradiction(ttl: str) -> bool | None:
    """probe TTL 을 HermiT 에 걸어 모순을 **탐지하는가**.

    Returns: True (모순 탐지) / False (탐지 못함) / None (실행 자체 실패).
    """
    tmp_path = None
    try:
        _cleanup_world()
        onto, tmp_path = _ttl_to_owlready(ttl, "http://ex.org/probe")
        try:
            with onto:
                sync_reasoner_hermit(infer_property_values=False)
        except OwlReadyInconsistentOntologyError:
            return True
        # 모순을 못 잡았어도 unsatisfiable 로 드러날 수 있다 — 둘 다 "탐지" 다.
        return bool(_collect_unsatisfiable(onto))
    except Exception as exc:  # noqa: BLE001
        logger.debug("blindness probe 실행 실패: %s", exc)
        return None
    finally:
        _safe_unlink(tmp_path)
        _cleanup_world()


def check_reasoner_blindness() -> dict:
    """추론기가 **명명 Restriction 을 보는가** 를 실측한다.

    ## 왜 필요한가 (2026-08-30 실측)

    배포 T-Box 의 ``owl:Restriction`` 74개는 **전부 명명 IRI** 이고 익명 bnode 는
    0개다 (클래스 열거 오염을 막으려고 step_19 가 skolemize 한 결과다). 그런데
    owlready2 는 IRI 노드를 ``ThingClass`` 로 만들 뿐 ``owl:Restriction`` 타입을
    클래스 표현식으로 **재구성하지 않는다**. 그래서 HermiT 에게는

        :D owl:equivalentClass :R      →  "이름 있는 두 클래스가 동등"

    으로만 보이고 ``onProperty`` / ``hasValue`` 제약은 어디에도 반영되지 않는다.

    피해: 정의 클래스 20개·존재 공리 54개·파티션 10건에 대한 S4·S9.1 의
    ``consistent: true`` 가 **적극적 오보고**다. 예외도 경고도 없이 정상 응답을
    주므로 "검증했다" 로 읽힌다. 같은 T-Box 를 프로그램으로 재익명화하면
    INCONSISTENT 가 나온다. 반면 S8 이 실제로 쓰는 ``reasonable`` (OWL 2 RL) 은
    명명형을 정상 처리하므로 **검증기와 실행기가 다른 온톨로지를 본다**.

    이 리포에는 같은 실패가 두 번 기록돼 있다 ("검증기 실패 = 검증 없음",
    "onto_path 는 파일명으로 대입한다"). 그 둘은 로드 실패 흔적이 있었지만 이번은
    정상 응답을 주므로 더 은밀하다. 그래서 **측정 장치를 먼저** 만든다.

    Returns:
        ``{"blind": bool|None, "named_detected": ..., "anon_detected": ...,
        "explanation": str}``. ``blind=True`` 면 이 스택의 ``consistent`` 결과를
        명명 Restriction 축의 증거로 쓸 수 없다.
    """
    cache_key = "named_restriction_v1"
    if cache_key in _BLINDNESS_PROBE_CACHE:
        return _BLINDNESS_PROBE_CACHE[cache_key]

    anon = _probe_detects_contradiction(_PROBE_EXAMPLE_ANON)
    named = _probe_detects_contradiction(_PROBE_EXAMPLE_NAMED)

    if anon is None or named is None:
        blind: bool | None = None
        explanation = (
            "probe 실행 실패 — 추론기 가용성을 확인할 수 없다 "
            "(Java 25+ / JAVA_EXE 확인). blind 판정 불가."
        )
    elif anon and not named:
        blind = True
        explanation = (
            "추론기가 **명명(named) Restriction 을 보지 못한다**. 같은 모순을 익명 "
            "bnode 로 적으면 탐지하지만 명명 IRI 로 적으면 CONSISTENT 를 반환한다. "
            "배포 T-Box 의 Restriction 은 전부 명명형이므로, 이 스택의 consistent/"
            "unsatisfiable 결과는 정의 클래스·존재 공리·파티션 축의 증거가 아니다. "
            "S4/S9.1 의 PASS 를 그 축의 검증으로 읽지 말 것."
        )
    elif anon and named:
        blind = False
        explanation = (
            "추론기가 명명·익명 Restriction 을 모두 본다 — consistent 결과를 "
            "그 축의 증거로 쓸 수 있다."
        )
    else:
        # 익명조차 못 잡으면 픽스처나 추론기 설정 문제다. 어느 쪽이든 이 스택의
        # 결과를 신뢰할 수 없으므로 blind 로 본다 (fail closed).
        blind = True
        explanation = (
            "probe 가 익명 Restriction 모순조차 탐지하지 못했다 — 추론기 설정이나 "
            "픽스처가 깨졌다. consistent 결과를 신뢰하지 말 것 (fail closed)."
        )

    result = {
        "blind": blind,
        "named_restriction_detected": named,
        "anon_restriction_detected": anon,
        "explanation": explanation,
    }
    _BLINDNESS_PROBE_CACHE[cache_key] = result
    if blind:
        logger.warning("[검증기 눈멂] %s", explanation)
    return result


def _count_named_restrictions(ttl: str) -> set:
    """TTL 에서 **명명 IRI** 로 선언된 ``owl:Restriction`` 주어 집합.

    익명 bnode Restriction 은 세지 않는다 — 그것은 추론기가 정상 처리한다.
    이 수가 0 이면 눈멂이 이 온톨로지의 결과에 영향을 주지 않는다.
    """
    from rdflib import OWL, RDF, URIRef

    from domain.tbox_utils import _new_graph

    graph = _new_graph()
    graph.parse(data=ttl, format="turtle")
    return {
        s for s in graph.subjects(RDF.type, OWL.Restriction)
        if isinstance(s, URIRef)
    }


def _cleanup_world():
    """owlready2 default_world를 초기화하여 이전 추론 결과를 제거한다."""
    # 모든 온톨로지 제거
    for onto in list(default_world.ontologies.values()):
        try:
            onto.destroy()
        except Exception as e:
            logger.debug("온톨로지 destroy 실패: %s", e)
    # default_world 그래프 재생성은 owlready2가 자동 처리


def _collect_unsatisfiable(onto) -> list:
    """unsatisfiable class 목록을 수집한다."""
    results = []
    try:
        for cls in default_world.inconsistent_classes():
            if cls is Nothing:
                continue
            results.append({
                "class": str(cls.iri) if hasattr(cls, "iri") else str(cls),
                "name": cls.name if hasattr(cls, "name") else str(cls),
            })
    except Exception as e:
        logger.debug("unsatisfiable class 수집 실패: %s", e)
    return results


def _collect_inferred_hierarchy(onto) -> list:
    """추론 후 inferred subClassOf 관계를 수집한다."""
    results = []
    steel_ns = DOMAIN_NS
    try:
        for cls in onto.classes():
            if not str(cls.iri).startswith(steel_ns):
                continue
            for parent in cls.is_a:
                if hasattr(parent, "iri") and str(parent.iri).startswith(steel_ns):
                    results.append({
                        "child": cls.name,
                        "parent": parent.name,
                    })
    except Exception as e:
        logger.debug("추론된 계층 수집 실패: %s", e)
    return results


# ── MCP 도구: HermiT ────────────────────────────────


def _load_ttl_content(ttl_content: str, ttl_path: str) -> str:
    """ttl_content 또는 ttl_path에서 TTL을 로드한다."""
    from tools.common import load_ttl_content
    return load_ttl_content(ttl_content, ttl_path)


def _consistency_scope(ttl_content: str, ttl_path: str) -> dict:
    """이 검증이 **무엇을 로드했는가**.

    호출자가 명시 입력을 주지 않으면 기본 경로는 ``TBOX_PATH`` 다
    (``tools/common.py`` 의 ``load_ttl_content``). 즉 A-Box 는 로드되지 않는다.
    이 사실이 응답에 없으면 ``consistent: true`` 가 "KG 가 일관적" 으로 읽힌다.
    """
    from config import TBOX_PATH

    if ttl_content:
        return {"scope": "explicit_content", "data_graph": "<inline ttl_content>",
                "abox_loaded": None}
    if ttl_path:
        return {"scope": "explicit_path", "data_graph": ttl_path,
                "abox_loaded": None}
    return {"scope": "tbox_only", "data_graph": TBOX_PATH, "abox_loaded": False}


def _cardinality_blindness(ttl: str) -> dict:
    """카디널리티 공리를 이 추론기가 검출할 수 있는가.

    **답은 거의 항상 "아니다"** 이고 이유가 둘이다 (2026-08-30 실측):

    1. **A-Box 를 로드하지 않는다.** 기본 경로가 ``TBOX_PATH`` 다.
    2. **로드해도 못 잡는다.** OWL 은 UNA(고유명 가정)를 쓰지 않으므로, 개체가
       서로 다르다는 것이 **증명되지 않으면** ``≤1`` 위반이 성립하지 않는다.
       최소 재현 (14 트리플): ``owl:differentFrom`` 없음 → ``consistent: true`` /
       한 줄 추가 → ``false``. 그리고 산출물 실측 — ``a_box.ttl`` ·
       ``all_inferred.ttl`` · tacit 8파일 전부 ``differentFrom``/``AllDifferent``
       **0건**이다.

    3. **넣는 것 자체가 불가능하다.** T-Box + ``a_box.ttl`` 전체(715,864 트리플)는
       HermiT 이 **timeout 3000s 에서 미반환**(java RSS 5.8GB). 클래스별 개체 1개만
       추린 10,143 트리플조차 30분 미반환. T-Box 단독은 47.6s 다.

    그래서 A-Box 로드 옵션을 추가하지 않았다 — 비용은 무한대이고 검출력은 0이다.
    카디널리티는 CWA 도구(``validate_owl_cardinality``)가 담당한다.
    """
    axioms = (
        ttl.count("owl:maxCardinality") + ttl.count("owl:minCardinality")
        + ttl.count("owl:cardinality")
        + ttl.count("owl:maxQualifiedCardinality")
        + ttl.count("owl:minQualifiedCardinality")
    )
    has_una = "owl:differentFrom" in ttl or "owl:AllDifferent" in ttl
    return {
        "cardinality_axioms": axioms,
        # 공리가 없으면 눈멂 자체가 무의미하다 (0회 발동을 0회로 보고한다).
        "detectable": False if axioms else None,
        "una_present": has_una,
        "reason": (
            "A-Box 미로드 + owl:differentFrom/AllDifferent 부재(No-UNA) — 이 축은 "
            "validate_owl_cardinality 가 CWA 로 검증한다. A-Box 를 넣어도 검출되지 "
            "않으며(실측), 전체 A-Box 는 HermiT 이 3000s 내 반환하지 못한다."
        ) if axioms else "카디널리티 공리 없음 — 이 축은 해당 없음",
    }


def validate_owl_consistency(ttl_content: str = "", ttl_path: str = "") -> str:
    """HermiT 추론기로 OWL DL 일관성을 검증한다.

    예상 소요시간: 15~25초 (T-Box 단독 실측 47.6s)

    T-Box 생성/수정 후 논리적 일관성을 확인하는 핵심 도구.

    검증 항목:
    - 온톨로지 전체 일관성 (consistency)
    - Unsatisfiable class 탐지 (논리적으로 인스턴스를 가질 수 없는 클래스)
    - 추론된 클래스 계층 구조 (inferred subClassOf)
    - 클래스/프로퍼티 통계

    ## 검증 범위: 기본은 **T-Box 만** 이다 (2026-08-30 명시)

    명시 입력이 없으면 기본 경로가 ``TBOX_PATH`` 이고 A-Box 는 로드되지 않는다
    (``tools/owl_reasoner.py`` 전체에 ``ABOX_PATH`` 참조 0건). 그래서 이 도구의
    ``consistent: true`` 는 **스키마 일관성**의 증거이지 "KG 가 일관적" 이 아니다.
    응답의 ``scope`` 에 그 사실을 각인한다.

    **카디널리티 위반은 이 도구로 잡히지 않는다** — ``cardinality_blindness`` 참조.
    A-Box 를 로드하는 옵션은 의도적으로 만들지 않았다: 전체 A-Box(715,864 트리플)는
    HermiT 이 3000s 내 반환하지 못하고(RSS 5.8GB), 반환한다 해도 No-UNA 때문에
    검출력이 0이다. 그 축은 ``validate_owl_cardinality`` (CWA) 가 담당한다.

    Args:
        ttl_content: 검증할 Turtle 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
    """
    tmp_path = None

    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl_content(ttl_content, ttl_path)
        # 캐시 체크: 동일 TTL 이면 JVM/HermiT 재호출 스킵.
        _cache_key = _hermit_cache_key("validate_owl_consistency", ttl)
        if _cache_key in _HERMIT_RESULT_CACHE:
            logger.info("HermiT 결과 캐시 hit (validate_owl_consistency)")
            return _HERMIT_RESULT_CACHE[_cache_key]

        _cleanup_world()
        start = time.monotonic()
        onto, tmp_path = _ttl_to_owlready(ttl)

        # 클래스/프로퍼티 통계 (추론 전)
        steel_ns = DOMAIN_NS
        classes_before = [c for c in onto.classes() if str(c.iri).startswith(steel_ns)]
        obj_props = [p for p in onto.object_properties() if str(p.iri).startswith(steel_ns)]
        data_props = [p for p in onto.data_properties() if str(p.iri).startswith(steel_ns)]

        # HermiT 추론
        # HermiT 자체는 정상 동작해도, 결과를 owlready2 Python 객체로 병합하는
        # post-processing (_apply_reasoning_results) 이 다음과 같은 상류 버그를 일으킨다:
        #   - TypeError: metaclass conflict (FusionClass 생성 실패 — ThingClass vs
        #     ObjectPropertyClass 메타클래스 비호환)
        #   - AttributeError: 'X' object has no attribute 'equivalent_to' (HermiT
        #     infer 결과 IRI 에 대응하는 Python 객체 누락)
        # 이 두 에러 모두 owlready2 의 내부 상태 동기화 문제이며, HermiT 가 반환한
        # consistency/unsatisfiable/classification 결과 자체는 신뢰 가능하다.
        # 따라서 이 에러들을 degraded 모드로 캐치하고, infer_property_values=False
        # 로 재시도. 재시도도 실패하면 HermiT 가 "consistency 위반은 아닌 것으로
        # 판단된" 상태를 보고한다 (엄밀한 증거는 아니지만 unsatisfiable 수집 시도는
        # 계속 수행).
        consistent = True
        error_msg = None
        property_value_inference_degraded = False
        reasoner_post_merge_failed = False

        def _is_post_merge_bug(exc: Exception) -> bool:
            """owlready2 post-processing 의 알려진 상류 버그 감지.

            진단 기준: 예외 traceback 이 owlready2/reasoning.py:_apply_reasoning_results
            경로를 거쳤는지 확인. 이 함수는 HermiT 결과를 Python 객체에 병합하는
            단계이며, 여기서 TypeError/AttributeError 가 나면 HermiT 자체는 성공한
            상태다.
            """
            import traceback as _tb
            tb_str = "".join(_tb.format_tb(exc.__traceback__))
            return "_apply_reasoning_results" in tb_str or "reinit" in tb_str

        try:
            sync_reasoner_hermit(infer_property_values=True)
        except OwlReadyInconsistentOntologyError as e:
            consistent = False
            error_msg = str(e)
        except (TypeError, AttributeError) as e:
            if not _is_post_merge_bug(e):
                raise  # 진짜 설정 오류면 상위 error_response 로.
            logger.warning(
                "HermiT post-merge 실패 (%s): %s — infer_property_values=False 로 재시도",
                type(e).__name__, e,
            )
            # world 초기화 + TTL 재로드 후 재시도.
            _cleanup_world()
            onto, _tmp_retry = _ttl_to_owlready(ttl)
            _safe_unlink(tmp_path)
            tmp_path = _tmp_retry
            property_value_inference_degraded = True
            try:
                sync_reasoner_hermit(infer_property_values=False)
            except OwlReadyInconsistentOntologyError as e2:
                consistent = False
                error_msg = str(e2)
            except (TypeError, AttributeError) as e2:
                if not _is_post_merge_bug(e2):
                    raise
                logger.warning(
                    "HermiT post-merge 재시도도 실패 (%s): %s — "
                    "classification/unsatisfiable 수집은 시도하되 reasoner 병합은 스킵",
                    type(e2).__name__, e2,
                )
                reasoner_post_merge_failed = True

        duration = round(time.monotonic() - start, 2)

        # Unsatisfiable class 수집
        unsatisfiable = _collect_unsatisfiable(onto)

        # 추론된 계층 구조
        inferred_hierarchy = _collect_inferred_hierarchy(onto)

        result = {
            "success": True,
            "consistent": consistent,
            "reasoner": "HermiT",
            "duration_seconds": duration,
            "unsatisfiable_classes": unsatisfiable,
            "unsatisfiable_count": len(unsatisfiable),
            "inferred_hierarchy": inferred_hierarchy,
            "property_value_inference_degraded": property_value_inference_degraded,
            "reasoner_post_merge_failed": reasoner_post_merge_failed,
            "statistics": {
                "classes": len(classes_before),
                "object_properties": len(obj_props),
                "data_properties": len(data_props),
            },
            # 무엇을 검증했는가 — ``consistent: true`` 를 "KG 가 일관적" 으로 읽지
            # 않게 한다 (``scope`` docstring 항목 참조).
            "scope": _consistency_scope(ttl_content, ttl_path),
            # 카디널리티 축은 이 도구가 원리상 볼 수 없다 (No-UNA). 진단 필드로만
            # 두고 게이트 판정에는 넣지 않는다 — 게이트를 baseline FAIL 로 고정하면
            # 아무도 보지 않게 된다 (이 리포의 "익명 표현식이 게이트를 죽였다").
            "cardinality_blindness": _cardinality_blindness(ttl),
        }

        # 검증기 눈멂 자기진단 — ``consistent: true`` 가 무엇의 증거인지 명시한다.
        # 명명 Restriction 을 못 보는 스택에서 PASS 는 정의 클래스·존재 공리·파티션
        # 축에 대해 아무 증거가 아니다 (check_reasoner_blindness docstring 참조).
        # 이 T-Box 가 실제로 명명 Restriction 을 쓰는지도 함께 세어, 눈멂이 이
        # 온톨로지에 **실제로 영향을 주는지** 판정한다.
        try:
            blindness = check_reasoner_blindness()
            named_restrictions = len({
                s for s in _count_named_restrictions(ttl)
            }) if blindness.get("blind") else 0
            result["validator_blindness"] = {
                **blindness,
                "named_restrictions_in_this_tbox": named_restrictions,
                "affects_this_result": bool(
                    blindness.get("blind") and named_restrictions
                ),
            }
            if blindness.get("blind") and named_restrictions:
                result["hint_validator_blind"] = (
                    f"이 T-Box 는 명명 Restriction {named_restrictions}개를 쓰지만 "
                    f"추론기가 그것을 클래스 표현식으로 보지 못한다 — "
                    f"consistent={consistent} 는 그 공리들에 대한 증거가 아니다. "
                    f"정의 클래스/파티션 축을 검증하려면 익명화 후 재실행하라."
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("blindness 자기진단 실패 (무시): %s", exc)

        if property_value_inference_degraded or reasoner_post_merge_failed:
            parts = []
            if property_value_inference_degraded:
                parts.append(
                    "infer_property_values=True 에서 owlready2 post-merge 실패 — "
                    "infer_property_values=False 로 재시도"
                )
            if reasoner_post_merge_failed:
                parts.append(
                    "재시도도 owlready2 post-merge 단계에서 실패. HermiT 자체는 "
                    "실행됐으나 결과를 Python 객체로 병합하지 못했으므로 unsatisfiable/"
                    "inferred_hierarchy 가 부분적일 수 있음. classify_tbox (별도 호출) 또는 "
                    "PELLET_REALISATION_ENABLED=true 설정 후 Pellet "
                    "(validate_owl_realisation) 으로 교차 확인 권장"
                )
            result["hint_degraded"] = " / ".join(parts)

        if not consistent:
            result["error"] = error_msg
            result["hint"] = ("온톨로지가 논리적으로 일관되지 않습니다. "
                              "모순된 axiom 조합을 확인하세요. "
                              "Pellet justification(validate_owl_justification)으로 원인을 분석할 수 있습니다.")

        if unsatisfiable:
            result["hint"] = (f"{len(unsatisfiable)}개 unsatisfiable class가 발견되었습니다. "
                              "이 클래스들은 논리적으로 인스턴스를 가질 수 없습니다. "
                              "domain/range 조합, disjoint 선언, restriction을 확인하세요.")

        json_result = json.dumps(result, ensure_ascii=False, indent=2)
        _hermit_cache_put(_cache_key, json_result)
        return json_result

    except Exception as e:
        return error_response(e, hint="Java 25+ 필요. JAVA_EXE 환경변수를 확인하세요.", logger=logger)
    finally:
        _safe_unlink(tmp_path)


def classify_tbox(ttl_content: str = "", ttl_path: str = "") -> str:
    """HermiT로 T-Box를 분류(classification)하여 추론된 클래스 계층을 반환한다.

    생성된 T-Box의 클래스 계층이 의도대로 추론되는지 확인하는 도구.
    명시적 subClassOf와 추론된 subClassOf를 비교할 수 있다.

    Args:
        ttl_content: 분류할 Turtle 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
    """
    tmp_path = None

    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl_content(ttl_content, ttl_path)
        _cache_key = _hermit_cache_key("classify_tbox", ttl)
        if _cache_key in _HERMIT_RESULT_CACHE:
            logger.info("HermiT 결과 캐시 hit (classify_tbox)")
            return _HERMIT_RESULT_CACHE[_cache_key]

        _cleanup_world()
        start = time.monotonic()
        onto, tmp_path = _ttl_to_owlready(ttl)

        steel_ns = DOMAIN_NS

        # 추론 전 명시적 계층
        explicit_hierarchy = []
        for cls in onto.classes():
            if not str(cls.iri).startswith(steel_ns):
                continue
            for parent in cls.is_a:
                if hasattr(parent, "iri") and str(parent.iri).startswith(steel_ns):
                    explicit_hierarchy.append({
                        "child": cls.name,
                        "parent": parent.name,
                        "type": "explicit",
                    })

        # HermiT classification
        try:
            sync_reasoner_hermit(infer_property_values=False)
        except OwlReadyInconsistentOntologyError:
            return error_response("온톨로지 비일관 — classification 불가", hint="validate_owl_consistency로 먼저 일관성을 확인하세요.", logger=logger)

        duration = round(time.monotonic() - start, 2)

        # 추론 후 전체 계층
        full_hierarchy = []
        for cls in onto.classes():
            if not str(cls.iri).startswith(steel_ns):
                continue
            for parent in cls.is_a:
                if hasattr(parent, "iri") and str(parent.iri).startswith(steel_ns):
                    full_hierarchy.append({
                        "child": cls.name,
                        "parent": parent.name,
                    })

        # 새로 추론된 관계 = full - explicit
        explicit_set = {(h["child"], h["parent"]) for h in explicit_hierarchy}
        inferred_new = [
            {"child": h["child"], "parent": h["parent"], "type": "inferred"}
            for h in full_hierarchy
            if (h["child"], h["parent"]) not in explicit_set
        ]

        unsatisfiable = _collect_unsatisfiable(onto)

        json_result = json.dumps({
            "success": True,
            "reasoner": "HermiT",
            "duration_seconds": duration,
            "explicit_hierarchy": explicit_hierarchy,
            "inferred_hierarchy": inferred_new,
            "explicit_count": len(explicit_hierarchy),
            "inferred_count": len(inferred_new),
            "unsatisfiable_classes": unsatisfiable,
        }, ensure_ascii=False, indent=2)
        _hermit_cache_put(_cache_key, json_result)
        return json_result

    except Exception as e:
        return error_response(e, logger=logger)
    finally:
        _safe_unlink(tmp_path)


# ── MCP 도구: Pellet ────────────────────────────────


def validate_owl_realisation(ttl_content: str = "", ttl_path: str = "") -> str:
    """명시적 opt-in 시 Pellet 추론기로 A-Box 실현(realisation)을 수행한다.

    각 인스턴스의 가장 구체적인 타입(most specific type)을 추론하고,
    프로퍼티 값 추론(infer_property_values)을 수행한다.

    기본값은 skip이다. ``PELLET_REALISATION_ENABLED=true``를 설정한 경우에만
    Pellet을 실행한다.

    Args:
        ttl_content: T-Box + A-Box가 포함된 Turtle 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
    """
    if os.getenv("PELLET_REALISATION_ENABLED", "false").lower() != "true":
        return json.dumps(
            {
                "success": True,
                "skipped": True,
                "reasoner": "Pellet",
                "reason": (
                    "PELLET_REALISATION_ENABLED=false (default). "
                    "활성화: export PELLET_REALISATION_ENABLED=true"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    _cleanup_world()
    tmp_path = None

    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl_content(ttl_content, ttl_path)
        start = time.monotonic()
        onto, tmp_path = _ttl_to_owlready(ttl)

        inst_ns = DOMAIN_INST_NS

        # 추론 전 인스턴스 타입 수집
        instances_before = {}
        for ind in onto.individuals():
            ind_iri = str(ind.iri) if hasattr(ind, "iri") else str(ind)
            if not ind_iri.startswith(inst_ns):
                continue
            types = [str(t.iri) if hasattr(t, "iri") else str(t)
                     for t in ind.is_a if hasattr(t, "iri")]
            instances_before[ind_iri] = types

        # Pellet realisation
        consistent = True
        error_msg = None
        try:
            sync_reasoner_pellet(
                infer_property_values=True,
                infer_data_property_values=True,
            )
        except OwlReadyInconsistentOntologyError as e:
            consistent = False
            error_msg = str(e)
        except Exception as e:
            return error_response(f"Pellet 실행 실패: {e}", hint="Java 25+ 필요. JAVA_EXE 환경변수를 확인하세요.", logger=logger)

        duration = round(time.monotonic() - start, 2)

        # 추론 후 인스턴스 타입 수집
        instances_after = {}
        type_changes = []
        for ind in onto.individuals():
            ind_iri = str(ind.iri) if hasattr(ind, "iri") else str(ind)
            if not ind_iri.startswith(inst_ns):
                continue
            types = [str(t.iri) if hasattr(t, "iri") else str(t)
                     for t in ind.is_a if hasattr(t, "iri")]
            instances_after[ind_iri] = types

            # 타입 변경 감지
            before_types = set(instances_before.get(ind_iri, []))
            after_types = set(types)
            new_types = after_types - before_types
            if new_types:
                local = _local_name(ind_iri)
                type_changes.append({
                    "instance": local,
                    "added_types": [_local_name(t) for t in new_types],
                })

        unsatisfiable = _collect_unsatisfiable(onto)

        result = {
            "success": True,
            "skipped": False,
            "consistent": consistent,
            "reasoner": "Pellet",
            "duration_seconds": duration,
            "instances_total": len(instances_after),
            "type_changes": type_changes[:50],  # 상위 50개만
            "type_changes_count": len(type_changes),
            "unsatisfiable_classes": unsatisfiable,
        }

        if not consistent:
            result["error"] = error_msg

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)
    finally:
        _safe_unlink(tmp_path)


# ── 내부 API: 생성 루프 통합용 ────────────────────────


def hermit_consistency_check(ttl_content: str) -> dict:
    """generate_tbox 교정 루프에서 호출하는 내부 헬퍼.

    MCP 도구가 아닌 Python 함수. 청크별 일관성 검사에 사용.

    Returns:
        {
            "consistent": bool,
            "unsatisfiable": [{"class": ..., "name": ...}, ...],
            "error": str or None,
        }
    """
    _cleanup_world()
    tmp_path = None

    try:
        onto, tmp_path = _ttl_to_owlready(ttl_content)

        consistent = True
        error_msg = None
        try:
            sync_reasoner_hermit(infer_property_values=False)
        except OwlReadyInconsistentOntologyError as e:
            consistent = False
            error_msg = str(e)

        unsatisfiable = _collect_unsatisfiable(onto)

        return {
            "consistent": consistent,
            "unsatisfiable": unsatisfiable,
            "error": error_msg,
        }

    except Exception as e:
        return {
            "consistent": None,
            "unsatisfiable": [],
            "error": str(e),
        }
    finally:
        _safe_unlink(tmp_path)


def pellet_realisation_check(ttl_content: str) -> dict:
    """generate_abox 검증에서 호출하는 내부 헬퍼.

    Returns:
        {
            "consistent": bool,
            "type_changes_count": int,
            "unsatisfiable": [...],
            "error": str or None,
        }
    """
    _cleanup_world()
    tmp_path = None

    try:
        onto, tmp_path = _ttl_to_owlready(ttl_content)

        inst_ns = DOMAIN_INST_NS

        # 추론 전 타입
        before = {}
        for ind in onto.individuals():
            iri = str(ind.iri) if hasattr(ind, "iri") else ""
            if iri.startswith(inst_ns):
                before[iri] = set(str(t.iri) for t in ind.is_a if hasattr(t, "iri"))

        consistent = True
        error_msg = None
        try:
            sync_reasoner_pellet(
                infer_property_values=True,
                infer_data_property_values=True,
            )
        except OwlReadyInconsistentOntologyError as e:
            consistent = False
            error_msg = str(e)

        # 추론 후 타입 변경 수
        changes = 0
        for ind in onto.individuals():
            iri = str(ind.iri) if hasattr(ind, "iri") else ""
            if iri.startswith(inst_ns):
                after = set(str(t.iri) for t in ind.is_a if hasattr(t, "iri"))
                if after - before.get(iri, set()):
                    changes += 1

        return {
            "consistent": consistent,
            "type_changes_count": changes,
            "unsatisfiable": _collect_unsatisfiable(onto),
            "error": error_msg,
        }

    except Exception as e:
        return {
            "consistent": None,
            "type_changes_count": 0,
            "unsatisfiable": [],
            "error": str(e),
        }
    finally:
        _safe_unlink(tmp_path)


# ── MinA Justification (Horridge 2010 / Reiter 1987 black-box HST) ───────


def _ttl_to_owlready_from_string(ttl_str: str, label: str = "minA"):
    """TTL 문자열을 owlready2 Ontology 로 로드. 에러 시 None 반환."""
    try:
        onto, tmp = _ttl_to_owlready(ttl_str)
        return onto, tmp
    except Exception as exc:
        logger.debug("MinA load failed (%s): %s", label, exc)
        return None, None


def _is_inconsistent(ttl_str: str) -> bool:
    """TTL 이 inconsistent / unsat class 가 있는지 HermiT 로 검사.

    중간 단계에서 TypeError 등이 나면 보수적으로 True(모순) 간주 — 이런 부분
    공리 집합은 MinA 후보에서 제외된다.
    """
    _cleanup_world()
    onto, tmp = _ttl_to_owlready_from_string(ttl_str, "check")
    if onto is None:
        return True
    try:
        try:
            sync_reasoner_hermit(infer_property_values=False)
        except OwlReadyInconsistentOntologyError:
            return True
        # unsatisfiable 이 하나라도 있으면 모순으로 간주
        if _collect_unsatisfiable(onto):
            return True
        return False
    except Exception as exc:
        logger.debug("HermiT check error: %s", exc)
        return True
    finally:
        _safe_unlink(tmp)


def _serialize_axiom_subset(axioms: list[tuple], prefix_ttl: str) -> str:
    """axiom subset + prefix declarations 를 TTL 로 직렬화."""
    from rdflib import Graph as _G
    g = _G()
    g.parse(data=prefix_ttl, format="turtle")
    # prefix_ttl 에 포함된 triple 은 제거 (prefix 만 추출 목적)
    triples_to_remove = list(g)
    for t in triples_to_remove:
        g.remove(t)
    # axioms 주입
    for s, p, o in axioms:
        g.add((s, p, o))
    return g.serialize(format="turtle")


def _find_one_mina(ttl: str, candidate_axioms: list[tuple],
                   prefix_ttl: str, max_iter: int = 200) -> list[tuple]:
    """Single MinA 를 공리 순차 제거로 추출한다.

    불변: candidate_axioms 만 포함한 TTL 도 inconsistent (=정당화의 상위집합).
    알고리즘: 각 axiom 을 순차로 제거해 보고, 제거해도 여전히 inconsistent 면
    정말 불필요하므로 버린다. O(|axioms|) HermiT 호출.

    Args:
        ttl: 원본 TTL (fallback).
        candidate_axioms: 줄여나갈 공리 리스트.
        prefix_ttl: prefix/ontology 선언만 포함한 최소 TTL.
        max_iter: HermiT 호출 상한 (큰 온톨로지 보호).
    """
    working = list(candidate_axioms)
    iters = 0
    i = 0
    while i < len(working):
        if iters >= max_iter:
            logger.warning("MinA 추출: max_iter(%d) 도달, 조기 종료", max_iter)
            break
        trial = working[:i] + working[i + 1:]
        trial_ttl = _serialize_axiom_subset(trial, prefix_ttl)
        iters += 1
        if _is_inconsistent(trial_ttl):
            # axiom i 없어도 여전히 모순 → 불필요
            working.pop(i)
        else:
            # axiom i 는 필수 → 유지하고 다음
            i += 1
    return working


def _extract_prefix_ttl(ttl: str) -> str:
    """TTL 에서 @prefix 선언과 owl:Ontology 선언만 추출."""
    lines = []
    for line in ttl.splitlines():
        stripped = line.strip()
        if stripped.startswith("@prefix") or stripped.startswith("@base") or not stripped:
            lines.append(line)
    return "\n".join(lines) + "\n"


def validate_owl_justification(
    ttl_content: str = "", ttl_path: str = "",
    max_axioms: int = 150, max_iter: int = 200,
) -> str:
    """OWL 온톨로지의 비일관성/unsat 클래스 원인 공리(MinA)를 추출한다.

    정당화(justification)는 함의를 유지하는 최소 공리 부분집합이며, 정의는
    Horridge, Parsia, Sattler (ISWC 2010, LNCS 6496, pp. 354-369,
    DOI:10.1007/978-3-642-17746-0_23) 를 따른다. 추출은 HermiT 를 판정
    oracle 로 쓰는 black-box 방식이고, Reiter 1987 HST 가 열거하는 MinA 전체
    대신 하나만 구한다.

    동작:
    1. T-Box 가 inconsistent 인지 HermiT 로 재확인.
    2. T-Box 공리(rdfs:subClassOf, owl:equivalentClass, owl:disjointWith,
       rdfs:domain/range, AllDisjointClasses) 를 후보로 수집.
    3. 각 공리를 하나씩 제거해 보며 여전히 모순이면 제거, 아니면 필수로 유지
       → 최종 남은 집합이 single MinA.

    한계:
    - single MinA 만 반환 (all MinAs 는 HST 확장 필요, 비용 큼).
    - 후보 공리 수 > max_axioms 면 샘플링.
    - 블랭크노드 Restriction 의 complex axiom 은 단위로 묶어 처리.

    Args:
        ttl_content: 대상 T-Box Turtle 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
        max_axioms: 후보 공리 상한 (기본 150). 초과 시 샘플링.
        max_iter: HermiT 호출 상한 (기본 200, 각 호출 5~20초).

    Returns:
        {
          "success": bool,
          "consistent": bool,
          "mina_axioms": [{"s": ..., "p": ..., "o": ...}],  # 원인 공리 리스트
          "mina_size": int,
          "hermit_calls": int,
          "unsatisfiable_classes": [...],
          "hint": str,
        }
    """
    from rdflib import OWL, RDFS

    from tools.common import load_ttl_content, success_response

    start = time.monotonic()
    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = load_ttl_content(ttl_content, ttl_path)

        # 1. 사전 체크 — 모순이 아니면 MinA 없음
        if not _is_inconsistent(ttl):
            return success_response({
                "consistent": True,
                "message": "온톨로지가 일관됨 — MinA 추출 불필요",
                "mina_axioms": [],
                "mina_size": 0,
            })

        # 2. 후보 공리 수집
        from domain.tbox_utils import _new_graph
        g = _new_graph()
        g.parse(data=ttl, format="turtle")

        candidate_predicates = {
            RDFS.subClassOf, RDFS.domain, RDFS.range,
            OWL.equivalentClass, OWL.disjointWith,
            OWL.inverseOf, OWL.onProperty, OWL.someValuesFrom,
            OWL.allValuesFrom, OWL.hasValue,
            OWL.minCardinality, OWL.maxCardinality, OWL.cardinality,
            OWL.unionOf, OWL.intersectionOf, OWL.complementOf,
        }
        candidates: list[tuple] = []
        for s, p, o in g:
            if p in candidate_predicates:
                candidates.append((s, p, o))
        # 모든 axiom (타입 선언 등) 은 유지하고 논리적 axiom 만 후보로.
        # prefix_ttl + 비후보 axiom = baseline, 여기에 candidates 를 더한 것이 inconsistent.
        base_axioms = [(s, p, o) for s, p, o in g if (s, p, o) not in set(candidates)]

        if len(candidates) > max_axioms:
            logger.info("MinA 후보 %d > %d, 샘플링 없이 전체 시도 (시간 많이 걸릴 수 있음)",
                        len(candidates), max_axioms)

        # prefix + baseline 은 항상 포함됨
        prefix_ttl = _extract_prefix_ttl(ttl)
        baseline_ttl = _serialize_axiom_subset(base_axioms + candidates, prefix_ttl)
        # baseline 이 inconsistent 한지 재확인 (후보 축소 후)
        if not _is_inconsistent(baseline_ttl):
            return success_response({
                "consistent": False,
                "message": "baseline + candidates 조합이 재확인 시 consistent 로 판정 — HermiT nondeterminism 가능",
                "mina_axioms": [],
                "mina_size": 0,
                "hint": "HermiT 재실행 시 결과 불일치. validate_owl_consistency 재실행 후 시도.",
            })

        # 3. MinA 추출 — candidates 를 좁혀나감
        # baseline (비후보) 는 항상 유지. candidates 에서만 reduce.
        def _check_with_base(cand_subset: list[tuple]) -> bool:
            combined = base_axioms + cand_subset
            ttl_str = _serialize_axiom_subset(combined, prefix_ttl)
            return _is_inconsistent(ttl_str)

        working = list(candidates)
        hermit_calls = 2  # 사전 + baseline 체크
        i = 0
        while i < len(working) and hermit_calls < max_iter:
            trial = working[:i] + working[i + 1:]
            hermit_calls += 1
            if _check_with_base(trial):
                # 없어도 모순 → 불필요
                working.pop(i)
            else:
                i += 1

        duration = round(time.monotonic() - start, 2)

        # 최종 unsat class 목록
        _cleanup_world()
        onto, tmp_path = _ttl_to_owlready(ttl)
        try:
            try:
                sync_reasoner_hermit(infer_property_values=False)
            except OwlReadyInconsistentOntologyError:
                pass
            unsat = _collect_unsatisfiable(onto)
        finally:
            _safe_unlink(tmp_path)

        mina_serialized = [
            {"s": str(s), "p": str(p), "o": str(o)}
            for s, p, o in working
        ]

        return success_response({
            "consistent": False,
            "mina_axioms": mina_serialized,
            "mina_size": len(working),
            "unsatisfiable_classes": unsat,
            "hermit_calls": hermit_calls,
            "duration_seconds": duration,
            "total_candidates": len(candidates),
            "citation": (
                "Horridge, Parsia, Sattler (2010). "
                "Justification Oriented Proofs in OWL. ISWC 2010, "
                "LNCS 6496, pp. 354-369. "
                "DOI:10.1007/978-3-642-17746-0_23"
            ),
            "hint": (
                f"{len(working)}개 공리 조합이 비일관성의 원인입니다. "
                "이 공리들을 제거하거나 수정하면 T-Box 가 일관됩니다. "
                "all-MinAs 추출은 HST 확장 필요 (현재 single MinA)."
            ),
        })

    except Exception as e:
        return error_response(e, logger=logger)


# ── OWL 2 Profile Detection (ELK 준비) ──────


def _detect_owl2_profile(tbox_ttl: str) -> dict:
    """T-Box 가 어떤 OWL 2 profile (EL/QL/RL/DL) 에 맞는지 감지한다.

    - OWL 2 EL: subClassOf/equivalentClass 하위 SOMEValuesFrom/Intersection/hasValue
      만 허용. owl:unionOf / complementOf / inverseOf / FunctionalProperty 등은
      **EL 밖**.
    - OWL 2 QL: 더 제한적. rdf:type 추론 중심, 클래스 정의에 intersection 만.
    - OWL 2 RL: subClass/subProperty/disjoint 위주, 추론 rule-based.
    - OWL 2 DL: 상위 전체.

    현재 추론 파이프라인은 Rust `reasonable` (OWL 2 RL) 로 구현되어 있어
    대부분 T-Box 가 RL 로 처리된다. EL 전용 ELK 는 profile 이 EL 에 fit 할 때만
    이득이 있으므로, 이 함수는 **진단용** 이며 자동 전환하지 않는다.
    """
    from rdflib import OWL, RDF

    from domain.tbox_utils import _new_graph

    g = _new_graph()
    g.parse(data=tbox_ttl, format="turtle")

    # EL 밖의 특징 연산자/공리 감지
    el_violations: list[str] = []
    if any(g.triples((None, OWL.unionOf, None))):
        el_violations.append("owl:unionOf 사용 — EL 밖")
    if any(g.triples((None, OWL.complementOf, None))):
        el_violations.append("owl:complementOf 사용 — EL 밖")
    if any(g.triples((None, OWL.inverseOf, None))):
        el_violations.append("owl:inverseOf 사용 — EL 밖")
    if any(g.triples((None, OWL.allValuesFrom, None))):
        el_violations.append("owl:allValuesFrom 사용 — EL 밖")
    if any(g.subjects(RDF.type, OWL.InverseFunctionalProperty)):
        el_violations.append("InverseFunctionalProperty 사용 — EL 밖")
    if any(g.subjects(RDF.type, OWL.SymmetricProperty)):
        el_violations.append("SymmetricProperty 사용 — EL 밖")
    if any(g.subjects(RDF.type, OWL.AsymmetricProperty)):
        el_violations.append("AsymmetricProperty 사용 — EL 밖")
    # maxCardinality (>0) 도 EL 밖
    for _s, _p, o in g.triples((None, OWL.maxCardinality, None)):
        try:
            if int(o) > 0:
                el_violations.append("owl:maxCardinality > 0 사용 — EL 밖")
                break
        except (TypeError, ValueError):
            pass

    is_el = not el_violations

    return {
        "profile_best_fit": "EL" if is_el else "DL",
        "el_eligible": is_el,
        "el_violations": el_violations[:10],
        "el_violation_count": len(el_violations),
        "recommendation": (
            "현재 T-Box 는 OWL 2 EL 프로파일에 fit 합니다. "
            "ELK reasoner 도입 시 S8 추론 속도 대폭 향상 가능."
            if is_el
            else (
                "T-Box 가 OWL 2 DL 표현력을 사용하고 있어 EL 전용 ELK 는 부적합합니다. "
                "현재 Rust `reasonable` (OWL 2 RL) 유지 권장. "
                "EL 로 축소하려면 unionOf/inverseOf/SymmetricProperty 등 제거 필요."
            )
        ),
    }


def check_owl2_profile(ttl_content: str = "", ttl_path: str = "") -> str:
    """T-Box 가 OWL 2 프로파일(EL/QL/RL/DL) 중 어디에 fit 하는지 감지한다.

    주 용도: ELK 같은 프로파일-특화 고속 reasoner 의 도입 가능성을 사전에
    판단. ELK (Kazakov, Krötzsch, Simančík 2014, JAR 53) 는 OWL EL 전용
    reasoner 이고 EL 분류는 다항시간이라, 대형 온톨로지에서 범용 OWL DL
    reasoner 보다 유리하다.

    **자동 reasoner 전환은 하지 않음** — profile 적합성만 리포트. 사용자가
    필요 시 수동으로 ELK 를 붙일 수 있도록 판단 근거 제공.

    Args:
        ttl_content: 평가할 T-Box Turtle 문자열.
        ttl_path: data/generated/tbox 아래 TTL 파일명.

    Returns:
        profile_best_fit, el_eligible, 위반 목록, 권고.
    """
    from tools.common import load_ttl_content, success_response

    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = load_ttl_content(ttl_content, ttl_path)
        result = _detect_owl2_profile(ttl)
        result["citation"] = (
            "Kazakov, Krötzsch, Simančík (2014). The Incredible ELK. "
            "J. Automated Reasoning 53. DOI:10.1007/s10817-013-9296-3"
        )
        return success_response(result)
    except Exception as e:
        return error_response(e, logger=logger)
