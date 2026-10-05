"""Step 7b — ``owl:imports`` 가 **해석 가능한** 온톨로지 IRI 만 가리키게 정리.

## 왜 필요한가

``owl:imports`` 는 **온톨로지 IRI** (문서의 이름) 를 가리켜야 한다. 용어
네임스페이스 (``…/ontology/construct/``) 를 넣으면 그 IRI 는 어떤 파일의
``owl:Ontology`` 선언도 아니어서 owlready2 가 네트워크로 나가고, IOF 서버 응답이
HTML 리다이렉트(307)라 파싱이 실패한다. 그러면 **HermiT 이 온톨로지를 아예 열지
못하고 전체 검증을 포기한다**:

    validate_owl_consistency → {"success": false,
      "error": "Cannot download 'https://…/ontology/construct/'!"}

실측 (2026-08-18): 이 한 줄 때문에 S9.1 게이트가 통째로 사라졌다. 해당 imports 만
제거하면 즉시 ``consistent=true`` 로 돌아오고 **unsat 4건** (``AlarmEvents`` /
``TagMaster`` / ``EquipmentMaster`` / ``EquipmentStatus``) 이 드러났다 — 검증이
없었던 동안 숨어 있던 실제 결함이다.

이 프로젝트에서 반복 확인한 실패 양식이다: **검증기가 실패하면 결함이 아니라
"검증 없음" 이 되고, 그 침묵이 통과로 읽힌다.**

## 왜 게이트가 아니라 정리인가

생성 지점(``step_07``)을 고쳐도 **이미 배포된 T-Box 파일** 에 박힌 값은 사라지지
않는다 (같은 세션에 ``step_02b`` 에서 겪은 것과 동일). 두 벌이 필요하다.

## 판정 기준 — 도메인-중립

특정 표준 이름을 박지 않는다. ``rdflib`` 이 파싱할 수 있는 형태인지, 그리고
**동봉 reference 파일의 ``owl:Ontology`` 집합**에 그 IRI 가 있는지로 본다. 로컬
사본이 없는 imports 는 (사설 IRI 일 수 있으므로) 건드리지 않는다 — 로컬에 사본이
있는 표준을 잘못된 IRI 로 가리키는 경우만 교정한다.
"""
from __future__ import annotations

import logging
import os

from rdflib import OWL, RDF, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def _shipped_ontology_iris() -> set[str]:
    """동봉 reference 파일들이 선언한 ``owl:Ontology`` IRI 집합.

    파일이 없으면 빈 집합 — 판정 불가이므로 아무것도 제거하지 않는다 (0건과
    판정 불가를 혼동하면 정당한 imports 를 지운다).
    """
    import config

    base = getattr(config, "SOURCE_REFERENCE_DIR", None) or os.path.join(
        "data", "source", "reference",
    )
    if not os.path.isdir(base):
        return set()
    out: set[str] = set()
    for fname in sorted(os.listdir(base)):
        if not fname.lower().endswith((".rdf", ".owl", ".ttl")):
            continue
        try:
            ref = Graph()
            ref.parse(os.path.join(base, fname))
        except Exception as exc:  # noqa: BLE001 — 일부 실패는 무시
            logger.debug("step_07b: reference 파싱 실패 (%s): %s", fname, exc)
            continue
        out |= {str(s) for s in ref.subjects(RDF.type, OWL.Ontology)}
    return out


def _fix_imports(g: Graph) -> dict:
    from domain.namespaces import IOF_ONTOLOGY_IRIS

    shipped = _shipped_ontology_iris()
    if not shipped:
        logger.info(
            "step_07b: reference 파일 없음 — owl:imports 판정을 건너뛴다",
        )
        return {
            "imports_unresolvable_removed": 0,
            "imports_canonical_added": 0,
            "imports_removed_samples": [],
        }

    # 용어 네임스페이스는 imports 대상이 아니다. 동봉 파일이 선언한 온톨로지
    # IRI 집합에 없고, 그 파일들이 실제로 쓰는 **용어 네임스페이스** 인 IRI 만
    # 제거 대상으로 본다 (사설/미지의 imports 는 보존).
    term_namespaces = _term_namespaces_of(shipped)

    removed = 0
    samples: list[str] = []
    for onto, _, target in list(g.triples((None, OWL.imports, None))):
        if not isinstance(target, URIRef):
            continue
        t = str(target)
        if t in shipped:
            continue
        if t not in term_namespaces:
            continue
        g.remove((onto, OWL.imports, target))
        removed += 1
        if len(samples) < 10:
            samples.append(t)

    # 제거로 표준 imports 가 통째로 사라지지 않도록, 동봉 파일에 대응하는
    # 정본 온톨로지 IRI 를 보강한다 (이미 있으면 no-op).
    added = 0
    subjects = list(g.subjects(RDF.type, OWL.Ontology))
    for onto in subjects:
        for iri in IOF_ONTOLOGY_IRIS:
            if iri not in shipped:
                continue
            if (onto, OWL.imports, URIRef(iri)) not in g:
                g.add((onto, OWL.imports, URIRef(iri)))
                added += 1

    if removed:
        logger.warning(
            "Step 7b: 해석 불가한 owl:imports %d건 제거 (%s). 용어 네임스페이스를 "
            "imports 로 쓰면 추론기가 네트워크 다운로드로 빠지고, 실패 시 "
            "**HermiT 이 전체 검증을 포기한다** — 결함이 아니라 검증 자체가 "
            "사라진다.", removed, samples[:3],
        )
    if added:
        logger.info(
            "Step 7b: 동봉 reference 에 대응하는 정본 온톨로지 IRI %d건 보강 "
            "(오프라인 onto_path 로 해석된다)", added,
        )
    return {
        "imports_unresolvable_removed": removed,
        "imports_canonical_added": added,
        "imports_removed_samples": samples,
    }


def _term_namespaces_of(shipped: set[str]) -> set[str]:
    """동봉 파일들이 **용어 IRI** 에 쓰는 네임스페이스 집합.

    ``owl:Ontology`` IRI 와 구분하기 위해 실제 subject 들의 공통 접두를 본다.
    """
    import config

    base = getattr(config, "SOURCE_REFERENCE_DIR", None) or os.path.join(
        "data", "source", "reference",
    )
    out: set[str] = set()
    if not os.path.isdir(base):
        return out
    for fname in sorted(os.listdir(base)):
        if not fname.lower().endswith((".rdf", ".owl", ".ttl")):
            continue
        try:
            ref = Graph()
            ref.parse(os.path.join(base, fname))
        except Exception:  # noqa: BLE001 — 파싱할 수 없는 선택적 참조 온톨로지는 건너뛴다
            continue
        for s in ref.subjects():
            if not isinstance(s, URIRef):
                continue
            iri = str(s)
            if iri in shipped:
                continue
            cut = max(iri.rfind("#"), iri.rfind("/"))
            if cut > 0:
                out.add(iri[: cut + 1])
    return out


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    stats = _fix_imports(g)
    return StepResult(
        name="step_07b_imports_resolvable",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="7b",
        step_label="imports_resolvable",
    )
