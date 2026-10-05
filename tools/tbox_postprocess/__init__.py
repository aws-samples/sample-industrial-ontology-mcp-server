"""T-Box 후처리 단계별 registry — #7.

이 서브패키지는 `tools/ontology_quality.py`의 17단계 파이프라인을
**단계별 메타데이터 registry**로 노출한다.

현재는 이행 단계: 실제 변환 로직은 여전히 `ontology_quality.improve_tbox`에
일체로 존재한다. 하지만 이 registry가 존재함으로써
- 단계 번호/이름/목적을 한 곳에서 조회
- 각 단계가 수행하는 변경을 change_log로 역추적
- 향후 단계별 함수 추출 시 선언적 refactor 경로 제공

사용:
    from tools.tbox_postprocess import STEP_REGISTRY, describe_step
    print(describe_step(13))
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PostprocessStep:
    """T-Box 후처리 단계 메타데이터."""

    number: int
    name: str
    purpose: str
    affects: tuple[str, ...]  # 변경 대상 (classes/ops/dps/annotations/...)
    citation: str | None = None


STEP_REGISTRY: dict[int, PostprocessStep] = {
    0: PostprocessStep(
        0, "antipattern_autofix",
        "Lonely Disjoint 카운트, Asymmetric inverseOf 보완, "
        "Redundant subClassOf 제거.",
        ("ops", "classes"),
    ),
    1: PostprocessStep(
        1, "alldisjoint_completion",
        "disjoint_groups.json 기준으로 AllDisjointClasses 완전화.",
        ("classes",),
    ),
    2: PostprocessStep(
        2, "inverseof_bidirectional",
        "OP간 inverseOf 양방향 선언 보장.",
        ("ops",),
    ),
    3: PostprocessStep(
        3, "op_domain_infill",
        "ObjectProperty domain 누락 자동 보완.",
        ("ops",),
    ),
    4: PostprocessStep(
        4, "comment_dedup",
        "동일 언어 중복 rdfs:comment 통합 (가장 긴 것 유지).",
        ("annotations",),
    ),
    5: PostprocessStep(
        5, "comment_cleanup",
        "rdfs:comment 내 코드 패턴/BNode 참조 정리.",
        ("annotations",),
    ),
    6: PostprocessStep(
        6, "mps_equivalentclass",
        "ManufacturingProcessStep unionOf → equivalentClass 변환.",
        ("classes",),
    ),
    7: PostprocessStep(
        7, "ontology_metadata",
        "owl:imports, versionIRI, dcterms 메타데이터 보강.",
        ("metadata",),
    ),
    8: PostprocessStep(
        8, "xsd_date_to_datetime",
        "xsd:date → xsd:dateTime 변환 (HermiT OWL 2 호환).",
        ("dps",),
        citation="W3C OWL 2 DL — XSD Datatype Map",
    ),
    9: PostprocessStep(
        9, "property_domain_range_repair",
        "공유 프로퍼티 domain/range 누락 보완, phantom class 교정.",
        ("ops", "dps"),
    ),
    10: PostprocessStep(
        10, "fk_domain_union",
        "공유 FK ObjectProperty domain 확장 (CSV FK 컬럼 스캔).",
        ("ops",),
    ),
    11: PostprocessStep(
        11, "tacit_class_autoimport",
        "암묵지 TTL에서 사용된 미등록 클래스를 T-Box에 추가.",
        ("classes",),
    ),
    12: PostprocessStep(
        12, "intermediate_abstract_class",
        "도메인별 중간 추상 클래스 생성 (DIT/NOC 개선).",
        ("classes",),
    ),
    13: PostprocessStep(
        13, "functional_pk_restriction",
        "PK FunctionalProperty + OP someValuesFrom 제약 추가 (Axiom Richness 개선).",
        ("ops", "dps", "classes"),
    ),
    14: PostprocessStep(
        14, "secondary_subgroups",
        "2차 계층 서브그룹 생성 (NOC/DIT 추가 최적화).",
        ("classes",),
    ),
    15: PostprocessStep(
        15, "cross_domain_ops",
        "design_patterns.json 기준 크로스 도메인 ObjectProperty 추가 (RR 향상).",
        ("ops",),
    ),
    16: PostprocessStep(
        16, "scope_emission_hasvalue",
        "scopeType 기반 hasValue Restriction 추가 (Scope 1/2/3 emission 자동 분류).",
        ("classes",),
    ),
    17: PostprocessStep(
        17, "completeness_annotation",
        "CWA/OWA 브릿지 — CSV 유래(closed), 암묵지(open), 추론(inferred) 태깅.",
        ("annotations",),
        citation="Zaveri et al. (2016); FOOPS! 2021 A1 체크",
    ),
    18: PostprocessStep(
        18, "bnode_skolemization",
        "OWL Restriction BNode 중 의미적으로 중요한 노드를 Skolem IRI로 교체.",
        ("classes",),
        citation="W3C RDF 1.1 — Skolemization",
    ),
}


def describe_step(number: int) -> dict[str, Any]:
    """단계 번호 → 메타데이터 dict."""
    step = STEP_REGISTRY.get(number)
    if not step:
        raise KeyError(f"Unknown postprocess step: {number}")
    return {
        "number": step.number,
        "name": step.name,
        "purpose": step.purpose,
        "affects": list(step.affects),
        "citation": step.citation,
    }


def list_steps() -> list[dict[str, Any]]:
    """모든 후처리 단계 메타데이터를 순서대로."""
    return [describe_step(i) for i in sorted(STEP_REGISTRY.keys())]


def count_steps() -> int:
    """전체 단계 수."""
    return len(STEP_REGISTRY)
