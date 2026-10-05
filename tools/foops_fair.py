"""FOOPS!/FAIR 온톨로지 점수 (Garijo et al. 2021, Wilkinson et al. 2016).

작업코드 표기(R1 등)는 docs/reference/task-glossary.md 참조.

근거:
- Garijo, D., Amdouni, I., et al. (2021). "FOOPS!: An Ontology Pitfall Scanner
  for the FAIR Principles." ISWC 2021 Posters.
- Wilkinson, M.D. et al. (2016). "The FAIR Guiding Principles for scientific
  data management and stewardship." Scientific Data 3:160018.
  DOI: 10.1038/sdata.2016.18.

온톨로지(T-Box) 파일을 입력으로 받아 4개 원칙 (Findable, Accessible,
Interoperable, Reusable)을 각 0~100점으로 평가한다.

구현 범위:
- 오프라인 정적 분석만 (HTTP resolve 체크는 선택 옵션 `deep=True`)
- 온톨로지 IRI / 메타데이터 / 라이선스 / imports / version 등 TTL 파싱으로 확인
- 각 점수에 대해 체크 항목별 pass/fail + 수정 제안 반환
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS

from config import GENERATED_REPORTS_DIR, GENERATED_TBOX_DIR, TBOX_PATH
from domain.tbox_utils import _new_graph
from tools.common import (
    error_response,
    is_deployed_input,
    resolve_child_path,
    success_response,
    write_deployed_sidecar,
)

logger = logging.getLogger(__name__)


DC_ELEMENTS_CREATOR = URIRef("http://purl.org/dc/elements/1.1/creator")
DC_TITLE = URIRef("http://purl.org/dc/elements/1.1/title")
DC_DESCRIPTION = URIRef("http://purl.org/dc/elements/1.1/description")
FOAF_HOMEPAGE = URIRef("http://xmlns.com/foaf/0.1/homepage")
CC_LICENSE = URIRef("http://creativecommons.org/ns#license")


# ── 개별 체크 결과 타입 ───────────────────────────

def _ok(name: str, detail: str = "") -> dict:
    return {"check": name, "pass": True, "detail": detail}


def _fail(name: str, detail: str, fix: str = "") -> dict:
    return {"check": name, "pass": False, "detail": detail, "fix": fix}


# ── Findable (F1~F4) ──────────────────────────────


def _check_findable(g: Graph, onto_iri: URIRef | None) -> dict:
    """F: ontology IRI 존재 + persistent identifier + 메타데이터 (label/title)."""
    checks: list[dict] = []

    # F1: (meta)data are assigned a globally unique and persistent identifier
    if onto_iri is None:
        checks.append(_fail("F1_ontology_iri", "owl:Ontology 선언이 없음",
                            "'<iri> a owl:Ontology .' 트리플 추가"))
    else:
        iri_str = str(onto_iri)
        # persistent identifier 패턴 (w3id, purl, doi 등) 가점
        persistent = any(p in iri_str for p in (
            "w3id.org", "purl.org", "doi.org", "identifiers.org",
        ))
        if persistent:
            checks.append(_ok("F1_ontology_iri", f"persistent IRI: {iri_str}"))
        else:
            checks.append(_fail(
                "F1_persistent_iri",
                f"IRI '{iri_str}'가 persistent identifier 서비스(w3id.org/purl.org/doi.org)를 사용하지 않음",
                "w3id.org 또는 purl.org에 IRI 등록 권장",
            ))

    # F2: data are described with rich metadata
    has_title = bool(onto_iri) and (
        any(g.objects(onto_iri, RDFS.label)) or
        any(g.objects(onto_iri, DCTERMS.title)) or
        any(g.objects(onto_iri, DC_TITLE))
    )
    if has_title:
        checks.append(_ok("F2_title", "title/label 선언됨"))
    else:
        checks.append(_fail("F2_title", "ontology에 rdfs:label 또는 dcterms:title 없음",
                            "`<iri> rdfs:label \"...\"@en .` 추가"))

    has_description = bool(onto_iri) and (
        any(g.objects(onto_iri, RDFS.comment)) or
        any(g.objects(onto_iri, DCTERMS.description)) or
        any(g.objects(onto_iri, DC_DESCRIPTION))
    )
    if has_description:
        checks.append(_ok("F2_description", "description 선언됨"))
    else:
        checks.append(_fail("F2_description",
                            "ontology에 dcterms:description/rdfs:comment 없음",
                            "온톨로지의 목적/범위 설명 추가"))

    # F3: metadata clearly and explicitly include the identifier
    # (이건 F1과 겹침 — ontology IRI 자체가 identifier)

    # F4: searchable — 레이블/키워드가 있는지 확인
    has_keyword = bool(onto_iri) and any(
        g.objects(onto_iri, DCTERMS.subject)
    )
    if has_keyword:
        checks.append(_ok("F4_keywords", "dcterms:subject 키워드 있음"))
    else:
        checks.append(_fail("F4_keywords",
                            "검색 가능성을 높일 dcterms:subject 키워드 없음",
                            "dcterms:subject \"keyword\" 추가 (권장, 점수 영향 소)"))

    return _aggregate("F", checks)


# ── Accessible (A1~A2) ────────────────────────────


def _check_accessible(g: Graph, onto_iri: URIRef | None, tbox_path: str) -> dict:
    """A: 표준 프로토콜(HTTP)로 접근 가능 + 메타데이터 접근성."""
    checks: list[dict] = []

    # A1: (meta)data retrievable by identifier using standardized protocol
    if onto_iri:
        scheme = urlparse(str(onto_iri)).scheme
        if scheme in ("http", "https"):
            checks.append(_ok("A1_protocol",
                              f"HTTP(S) 스킴 사용: {scheme}"))
        else:
            checks.append(_fail("A1_protocol",
                                f"비표준 스킴: {scheme or '(없음)'}",
                                "http:// 또는 https:// URL 사용"))

    # A1.1: protocol is open, free, and universally implementable — HTTP이면 OK
    # A1.2: authentication if needed — 온톨로지는 보통 public

    # A2: metadata are accessible, even when the data are no longer available
    # → 로컬 파일이 존재하는지만 확인
    if os.path.exists(tbox_path):
        size = os.path.getsize(tbox_path)
        checks.append(_ok("A2_file_present",
                          f"T-Box 파일 존재: {size:,} bytes"))
    else:
        checks.append(_fail("A2_file_present",
                            f"파일 없음: {tbox_path}",
                            "T-Box 파일을 먼저 생성하세요 (generate_tbox)"))

    return _aggregate("A", checks)


# ── Interoperable (I1~I3) ─────────────────────────


def _check_interoperable(g: Graph, onto_iri: URIRef | None) -> dict:
    """I: 표준 vocabulary + imports + formal knowledge representation."""
    checks: list[dict] = []

    # I1: formal, accessible, shared, broadly applicable language
    # → OWL/RDFS 사용 확인
    has_owl_class = any(g.subjects(RDF.type, OWL.Class))
    has_rdfs_subclass = any(g.triples((None, RDFS.subClassOf, None)))
    if has_owl_class or has_rdfs_subclass:
        checks.append(_ok("I1_formal_language",
                          "OWL/RDFS 표준 사용"))
    else:
        checks.append(_fail("I1_formal_language",
                            "owl:Class 또는 rdfs:subClassOf 사용 없음",
                            "표준 OWL/RDFS 구조로 전환"))

    # I2: vocabularies that follow FAIR principles
    # → owl:imports로 외부 표준 vocabulary 재사용
    imports_count = 0
    standard_vocab_count = 0
    if onto_iri:
        imports = list(g.objects(onto_iri, OWL.imports))
        imports_count = len(imports)
        standard_prefixes = (
            "http://purl.obolibrary.org/obo/",
            "http://xmlns.com/foaf/",
            "http://purl.org/dc/",
            "http://www.w3.org/ns/prov",
            "https://spec.industrialontologies.org/",
        )
        for imp in imports:
            if any(str(imp).startswith(p) for p in standard_prefixes):
                standard_vocab_count += 1

    if imports_count > 0:
        checks.append(_ok("I2_imports",
                          f"owl:imports {imports_count}개 (표준 vocabulary {standard_vocab_count}개)"))
    else:
        checks.append(_fail("I2_imports",
                            "owl:imports로 외부 표준 vocabulary 재사용 없음",
                            "IOF/FOAF/PROV-O 등 import 권장"))

    # I3: qualified references to other (meta)data
    # → rdfs:seeAlso / owl:sameAs / skos:closeMatch 등
    has_cross_ref = (
        any(g.triples((None, RDFS.seeAlso, None))) or
        any(g.triples((None, OWL.sameAs, None))) or
        any(g.triples((None, URIRef("http://www.w3.org/2004/02/skos/core#closeMatch"), None)))
    )
    if has_cross_ref:
        checks.append(_ok("I3_cross_references",
                          "rdfs:seeAlso / owl:sameAs 등 cross-reference 있음"))
    else:
        checks.append(_fail("I3_cross_references",
                            "외부 자원에 대한 rdfs:seeAlso / owl:sameAs 없음",
                            "IOF/표준 온톨로지에 대한 mapping 추가"))

    return _aggregate("I", checks)


# ── Reusable (R1) ─────────────────────────────────


def _check_reusable(g: Graph, onto_iri: URIRef | None) -> dict:
    """R: 라이선스, 버전, provenance, 재사용 가능한 메타데이터."""
    checks: list[dict] = []

    # R1.1: (meta)data are released with a clear and accessible data usage license
    license_preds = [
        DCTERMS.license, CC_LICENSE, URIRef("http://purl.org/dc/elements/1.1/license"),
    ]
    has_license = False
    if onto_iri:
        for pred in license_preds:
            if any(g.objects(onto_iri, pred)):
                has_license = True
                break
    if has_license:
        checks.append(_ok("R1_1_license", "dcterms:license 또는 cc:license 선언"))
    else:
        checks.append(_fail(
            "R1_1_license",
            "라이선스 선언 없음 — 재사용자가 법적 사용 가능성 판단 불가",
            "dcterms:license <URL> (예: CC-BY-4.0) 추가",
        ))

    # R1.2: provenance
    prov_preds = [
        DCTERMS.creator, URIRef("http://www.w3.org/ns/prov#wasAttributedTo"),
        DC_ELEMENTS_CREATOR,
    ]
    has_creator = False
    if onto_iri:
        for pred in prov_preds:
            if any(g.objects(onto_iri, pred)):
                has_creator = True
                break
    if has_creator:
        checks.append(_ok("R1_2_provenance", "creator/wasAttributedTo 있음"))
    else:
        checks.append(_fail(
            "R1_2_provenance",
            "온톨로지 저자/기관 정보 없음",
            "dcterms:creator \"...\" 또는 prov:wasAttributedTo 추가",
        ))

    # R1.2: version
    has_version = False
    if onto_iri and (any(g.objects(onto_iri, OWL.versionInfo)) or \
           any(g.objects(onto_iri, OWL.versionIRI)) or \
           any(g.objects(onto_iri, DCTERMS.hasVersion))):
        has_version = True
    if has_version:
        checks.append(_ok("R1_2_version", "owl:versionInfo / owl:versionIRI 선언"))
    else:
        checks.append(_fail(
            "R1_2_version",
            "버전 정보 없음 — 재사용자가 변경 이력 추적 불가",
            "owl:versionInfo \"1.0.0\" 추가",
        ))

    # R1.3: community standards (IOF/BFO 등)
    # I2와 일부 겹침이나 R1.3은 specifically "community-driven standards"
    community_standards = 0
    if onto_iri:
        imports = list(g.objects(onto_iri, OWL.imports))
        community_prefixes = (
            "http://purl.obolibrary.org/obo/",
            "https://spec.industrialontologies.org/",
            "http://www.w3.org/ns/prov",
        )
        for imp in imports:
            if any(str(imp).startswith(p) for p in community_prefixes):
                community_standards += 1
    if community_standards > 0:
        checks.append(_ok(
            "R1_3_community_standards",
            f"IOF/OBO/PROV 등 community standard {community_standards}개 import",
        ))
    else:
        checks.append(_fail(
            "R1_3_community_standards",
            "community-driven 표준(IOF, OBO, PROV 등) import 없음",
            "해당 도메인 community standard ontology import 권장",
        ))

    return _aggregate("R", checks)


# ── 집계 유틸 ─────────────────────────────────────


def _aggregate(axis: str, checks: list[dict]) -> dict:
    passed = sum(1 for c in checks if c["pass"])
    total = len(checks)
    score = round(passed / max(total, 1) * 100, 1)
    return {
        "axis": axis,
        "score": score,
        "passed": passed,
        "total": total,
        "checks": checks,
    }


# ── 오케스트레이션 ────────────────────────────────


def _find_ontology_iri(g: Graph) -> URIRef | None:
    for onto in g.subjects(RDF.type, OWL.Ontology):
        if isinstance(onto, URIRef):
            return onto
    return None


def evaluate_fair(tbox_path: str | None = None, deep: bool = False) -> dict:
    """T-Box에 대해 FAIR 4축 점수를 계산.

    Args:
        tbox_path: T-Box TTL 경로. None이면 config.TBOX_PATH.
        deep: 외부 HTTP resolve 체크용 확장 지점. 현재 구현은 로컬 검사만 수행한다.

    Returns:
        {axis_scores, overall_score, grade, findings}
    """
    path = tbox_path or TBOX_PATH
    if not os.path.exists(path):
        return {"error": f"T-Box not found: {path}"}

    g = _new_graph()
    try:
        g.parse(path, format="turtle")
    except Exception as e:
        return {"error": f"TTL parse failed: {type(e).__name__}: {e}"}

    onto_iri = _find_ontology_iri(g)

    findable = _check_findable(g, onto_iri)
    accessible = _check_accessible(g, onto_iri, path)
    interoperable = _check_interoperable(g, onto_iri)
    reusable = _check_reusable(g, onto_iri)

    overall = round(
        (findable["score"] + accessible["score"]
         + interoperable["score"] + reusable["score"]) / 4, 1,
    )
    if overall >= 85:
        grade = "A"
    elif overall >= 70:
        grade = "B"
    elif overall >= 55:
        grade = "C"
    else:
        grade = "D"

    # 전체 실패 항목 모음
    all_findings: list[dict] = []
    for axis_result in (findable, accessible, interoperable, reusable):
        for check in axis_result["checks"]:
            if not check["pass"]:
                all_findings.append({
                    "axis": axis_result["axis"],
                    **check,
                })

    return {
        "tbox_path": path,
        "ontology_iri": str(onto_iri) if onto_iri else None,
        "axis_scores": {
            "Findable": findable,
            "Accessible": accessible,
            "Interoperable": interoperable,
            "Reusable": reusable,
        },
        "overall_score": overall,
        "grade": grade,
        "findings_count": len(all_findings),
        "findings": all_findings,
        "citation": (
            "Garijo et al. (2021) FOOPS! (ISWC 2021 Posters) + "
            "Wilkinson et al. (2016) FAIR (Scientific Data 3:160018)"
        ),
    }


def evaluate_fair_score(tbox_path: str = "", deep: bool = False) -> str:
    """T-Box에 대한 FAIR 4축(Findable/Accessible/Interoperable/Reusable) 점수.

    근거: Garijo et al. 2021 (FOOPS!), Wilkinson et al. 2016 (FAIR Principles).

    Args:
        tbox_path: data/generated/tbox 아래 T-Box TTL 파일명. 비면 config.TBOX_PATH.
        deep: True면 HTTP resolve 등 외부 검증을 포함한다.
    """
    try:
        if tbox_path:
            tbox_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                tbox_path,
                allowed_suffixes=(".ttl",),
            )
        report = evaluate_fair(tbox_path or None, deep=deep)
        if "error" in report:
            return error_response(report["error"], logger=logger)
        # 배포 fair_score.json 의 tbox_path 가 `/tmp/pytest-…/tbox.ttl` 이었다
        # (실측 2026-09-02) — 3 트리플 테스트 온톨로지를 서술하고 있었다.
        write = write_deployed_sidecar(
            os.path.join(GENERATED_REPORTS_DIR, "fair_score.json"),
            report,
            sources={"tbox": tbox_path or TBOX_PATH},
            inputs_are_deployed=is_deployed_input(tbox_path, TBOX_PATH),
            logger=logger,
        )
        return success_response({**report, "report_written": write})
    except Exception as e:
        return error_response(f"FAIR 평가 실패: {e}", logger=logger)


# ── Matentzoglu et al. 2018 MIRO 체크리스트 ────


def _miro_check_ontology_iri(g) -> dict:
    """A.1: Ontology IRI 선언 존재."""
    from rdflib import OWL, RDF
    onts = list(g.subjects(RDF.type, OWL.Ontology))
    return {
        "check": "A.1_ontology_iri",
        "pass": bool(onts),
        "detail": f"owl:Ontology 선언 {len(onts)}개",
    }


def _miro_check_creator(g) -> dict:
    """A.4: Creator / Contributor 명시."""
    from rdflib import DCTERMS
    creators = list(g.objects(predicate=DCTERMS.creator))
    contribs = list(g.objects(predicate=DCTERMS.contributor))
    total = len(creators) + len(contribs)
    return {
        "check": "A.4_creator_contributor",
        "pass": total > 0,
        "detail": (
            f"creator {len(creators)} + contributor {len(contribs)}"
            if total else "dcterms:creator / dcterms:contributor 미선언"
        ),
    }


def _miro_check_license(g) -> dict:
    """A.6: License URI 명시."""
    from rdflib import DCTERMS, URIRef
    lics = list(g.objects(predicate=DCTERMS.license))
    uri_lics = [license_value for license_value in lics if isinstance(license_value, URIRef)]
    return {
        "check": "A.6_license",
        "pass": bool(uri_lics),
        "detail": (
            f"URI license {len(uri_lics)}개" if uri_lics
            else "dcterms:license URI 미선언"
        ),
    }


def _miro_check_version(g) -> dict:
    """A.5: Version info / versionIRI."""
    from rdflib import OWL
    vinfo = list(g.objects(predicate=OWL.versionInfo))
    viri = list(g.objects(predicate=OWL.versionIRI))
    total = len(vinfo) + len(viri)
    return {
        "check": "A.5_version",
        "pass": total > 0,
        "detail": (
            f"versionInfo {len(vinfo)} + versionIRI {len(viri)}"
            if total else "버전 정보 미선언"
        ),
    }


def _miro_check_description(g) -> dict:
    """B.2: Description — ontology scope 설명."""
    from rdflib import DCTERMS, OWL, RDF, RDFS
    onts = list(g.subjects(RDF.type, OWL.Ontology))
    if not onts:
        return {"check": "B.2_description", "pass": False,
                "detail": "owl:Ontology 선언 없음"}
    o = onts[0]
    has_desc = (
        any(g.triples((o, RDFS.comment, None)))
        or any(g.triples((o, DCTERMS.description, None)))
    )
    return {
        "check": "B.2_description",
        "pass": has_desc,
        "detail": "rdfs:comment / dcterms:description 존재" if has_desc
                  else "ontology description 미선언",
    }


def _miro_check_scope(g) -> dict:
    """B.3: Ontology scope 명확성 — class_count + comment presence 로 대리."""
    from rdflib import OWL, RDF
    classes = [c for c in g.subjects(RDF.type, OWL.Class)
               if str(c).startswith("http")]
    # scope 가 명확하려면 최소 10 class 이상 있어야
    return {
        "check": "B.3_scope",
        "pass": len(classes) >= 10,
        "detail": f"{len(classes)}개 클래스",
    }


def _miro_check_knowledge_representation(g) -> dict:
    """C.1: KR language — OWL/RDFS 사용 명시."""
    from rdflib import OWL, RDF
    has_owl = any(True for _ in g.triples((None, RDF.type, OWL.Class)))
    has_op = any(True for _ in g.triples(
        (None, RDF.type, OWL.ObjectProperty)))
    return {
        "check": "C.1_knowledge_representation",
        "pass": has_owl and has_op,
        "detail": "OWL Class + ObjectProperty 선언 존재" if has_owl and has_op
                  else "OWL 표현력 부족",
    }


def _miro_check_language(g) -> dict:
    """D.1: Natural language — label 언어 태그 다양성."""
    from rdflib import RDFS, Literal
    langs: set = set()
    for _, _, o in g.triples((None, RDFS.label, None)):
        if isinstance(o, Literal) and o.language:
            langs.add(o.language)
    return {
        "check": "D.1_language",
        "pass": len(langs) >= 2,
        "detail": (
            f"label 언어 {len(langs)}개: {sorted(langs)}"
            if langs else "언어 태그된 label 없음"
        ),
    }


def _miro_check_community_standards(g) -> dict:
    """D.2: External reference — owl:imports + cross-reference."""
    from rdflib import OWL, RDFS
    imports = list(g.objects(predicate=OWL.imports))
    see_also = list(g.objects(predicate=RDFS.seeAlso))
    total = len(imports) + len(see_also)
    return {
        "check": "D.2_community_standards",
        "pass": total > 0,
        "detail": (
            f"imports {len(imports)} + seeAlso {len(see_also)}"
            if total else "외부 community standard 참조 없음"
        ),
    }


_MIRO_CHECKS = [
    ("A.1", _miro_check_ontology_iri),
    ("A.4", _miro_check_creator),
    ("A.5", _miro_check_version),
    ("A.6", _miro_check_license),
    ("B.2", _miro_check_description),
    ("B.3", _miro_check_scope),
    ("C.1", _miro_check_knowledge_representation),
    ("D.1", _miro_check_language),
    ("D.2", _miro_check_community_standards),
]


def evaluate_miro(tbox_path: str = "") -> str:
    """MIRO (Minimum Information for Reporting an Ontology) 체크리스트 평가.

    근거: Matentzoglu, Malone, Mungall, Stevens (2018). "MIRO: Guidelines
    for Minimum Information for the Reporting of an Ontology."
    J. Biomedical Semantics 9:6. DOI:10.1186/s13326-017-0172-7

    FOOPS!/FAIR 이 "찾을 수 있는가(Findability)" 에 집중한다면, MIRO 는
    **공개/재사용 시 필요한 최소 메타데이터** 에 집중. 두 평가는 상보적.

    현재 구현은 peer-reviewed MIRO 체크리스트 원전 중 머신 판독 가능한 항목만
    (9 check). 전체 24 check 중 프로세스/동료검토/통계 관련은 사람 평가 필요.

    Args:
        tbox_path: data/generated/tbox 아래 T-Box TTL 파일명. 비면 config.TBOX_PATH.
    """
    try:
        if tbox_path:
            tbox_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                tbox_path,
                allowed_suffixes=(".ttl",),
            )
        else:
            tbox_path = TBOX_PATH
        if not os.path.exists(tbox_path):
            return error_response(
                f"T-Box 파일 없음: {tbox_path}", logger=logger,
            )

        from domain.tbox_utils import _new_graph, fast_parse_turtle
        g = _new_graph()
        fast_parse_turtle(g, tbox_path)

        results = []
        passed = 0
        for miro_id, fn in _MIRO_CHECKS:
            r = fn(g)
            r["miro_id"] = miro_id
            results.append(r)
            if r["pass"]:
                passed += 1

        total = len(_MIRO_CHECKS)
        score = round(passed / total * 100, 1)
        findings = [r for r in results if not r["pass"]]

        return success_response({
            "tbox_path": tbox_path,
            "overall_score": score,
            "grade": "A" if score >= 90 else "B" if score >= 75
                     else "C" if score >= 60 else "D",
            "passed": passed,
            "total": total,
            "checks": results,
            "findings_count": len(findings),
            "findings": findings,
            "citation": (
                "Matentzoglu et al. (2018). MIRO: Guidelines for Minimum "
                "Information for the Reporting of an Ontology. "
                "J. Biomedical Semantics 9:6. "
                "DOI:10.1186/s13326-017-0172-7"
            ),
            "notes": (
                "본 도구는 MIRO 체크리스트 중 머신 판독 가능한 9개 항목만 "
                "평가. 전체 24개 체크리스트(peer-review, adoption 통계 등)는 "
                "별도 사람 평가 필요."
            ),
        })

    except Exception as e:
        return error_response(f"MIRO 평가 실패: {e}", logger=logger)
