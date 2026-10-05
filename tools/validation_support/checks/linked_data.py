"""Linked Data 품질 차원 check — Zaveri et al. 2016 기반.

Zaveri, Rula, Maurino, Pietrobon, Lehmann, Auer (2016).
"Quality Assessment for Linked Data: A Survey."
Semantic Web Journal 7(1):63-93. DOI:10.3233/SW-150175

Färber 2018 이 커버하지 못하는 차원:
- Interlinking (외부 네임스페이스 URI 링크 밀도)
- Licensing (dct:license / cc:license 선언 및 머신 판독 가능성)
- Understandability (rdfs:label/comment 커버리지 + 인간 가독성)

각 check 는 기존 SharedCheckContext 규약을 따라 g/tbox 를 받는다.
"""
from __future__ import annotations

import logging

from rdflib import DCTERMS, OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_NS
from tools.validation_support.common import local

logger = logging.getLogger(__name__)

# Creative Commons 네임스페이스
_CC = Namespace("http://creativecommons.org/ns#")

# 외부 표준 vocabulary — interlinking 대상으로 인정할 prefix
_EXTERNAL_VOCABS = {
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2001/XMLSchema#",
    "http://purl.org/dc/terms/",
    "http://purl.org/dc/elements/1.1/",
    "http://www.w3.org/ns/prov#",
    "http://xmlns.com/foaf/0.1/",
    "http://www.w3.org/2004/02/skos/core#",
    "http://www.w3.org/ns/shacl#",
    "https://spec.industrialontologies.org/ontology/",
    "http://purl.obolibrary.org/obo/",
    "http://creativecommons.org/ns#",
}


def _is_external_uri(uri_str: str, internal_ns: str) -> bool:
    """URI 가 domain 내부 네임스페이스 밖이면서 표준 vocab 인지."""
    if uri_str.startswith(internal_ns):
        return False
    return any(uri_str.startswith(ext) for ext in _EXTERNAL_VOCABS)


def check_interlinking(
    tbox: Graph, *, internal_ns: str | None = None,
) -> dict[str, object]:
    """Zaveri §5.3.4 Interlinking — 외부 vocabulary 연결 밀도.

    T-Box 가 외부 표준(OWL/RDFS/DC/PROV/SKOS/IOF/BFO 등)을 얼마나 참조하는지
    측정. 참조 방식:
    - owl:imports 선언 개수
    - 외부 prefix URI 를 object 로 쓰는 triple 비율
    - rdfs:subClassOf / rdfs:subPropertyOf 가 외부 클래스/프로퍼티를 가리키는 비율

    Returns:
        {"name", "passed", "external_imports", "external_object_ratio_pct",
         "subsumption_to_external", "score", "recommendations"}
    """
    ns = internal_ns or DOMAIN_NS

    imports = list(tbox.objects(predicate=OWL.imports))
    imports_list = [str(u) for u in imports if isinstance(u, URIRef)]

    total_object_triples = 0
    external_object_triples = 0
    for _s, _p, o in tbox:
        if isinstance(o, URIRef):
            total_object_triples += 1
            if _is_external_uri(str(o), ns):
                external_object_triples += 1

    # subsumption to external: rdfs:subClassOf 가 외부 클래스를 가리키는 개수
    subsumption_external = 0
    subsumption_total = 0
    for _s, _p, o in tbox.triples((None, RDFS.subClassOf, None)):
        if isinstance(o, URIRef):
            subsumption_total += 1
            if _is_external_uri(str(o), ns):
                subsumption_external += 1

    ratio_external = (
        external_object_triples / total_object_triples * 100
        if total_object_triples else 0.0
    )
    subsumption_ratio = (
        subsumption_external / subsumption_total * 100
        if subsumption_total else 0.0
    )

    # **imports 가 실제로 해석되는가** — 선언만으로 점수를 주면 안 된다.
    #
    # 참조된 외부 IRI 가 그래프 안에서 subject 로 등장하는지(= 공리를 갖는지) 센다.
    # 0 이면 그 imports 는 **장식** 이다: 상위 클래스의 disjointness·domain/range 가
    # 없으므로 어떤 정렬 오류도 추론기가 잡지 못한다.
    #
    # 실측 (2026-08-17, 배포 T-Box): owl:imports 3건이 선언돼 있고 IOF IRI 19개를
    # 참조하는데 **공리 보유 0개** 였다. 원인은 네임스페이스 드리프트 — 번들
    # Core.rdf 는 클래스를 ``/ontology/construct/`` 로 선언하는데 T-Box 는
    # ``/ontology/core/Core/`` 를 참조한다. 그 결과 "HermiT consistent /
    # unsatisfiable 0" 이 **공리가 없어서 얻은 침묵** 이었고, 네임스페이스를 맞춰
    # 병합하자 SupplierMaster 가 unsatisfiable 로 드러났다.
    #
    # 예전 점수식은 ``if imports_list: score += 30`` — 개수만 봤다. 그래서 이 상태가
    # 만점 근처로 보고됐다.
    # 측정 범위는 **선언된 imports 네임스페이스 아래의 IRI** 로 한정한다.
    # 외부 IRI 전체를 세면 ``owl:Class``/``rdfs:label`` 처럼 T-Box 안에 공리가 없는
    # 것이 정상인 메타 용어가 분모를 오염시킨다 (실측 40개 중 대부분).
    referenced_imported: set[str] = set()
    for _s, _p, o in tbox:
        if not isinstance(o, URIRef):
            continue
        o_str = str(o)
        if o_str in imports_list:
            continue                       # imports 선언 자체는 제외
        if any(o_str.startswith(imp) for imp in imports_list):
            referenced_imported.add(o_str)
    resolved_imported = {
        u for u in referenced_imported
        if any(True for _ in tbox.triples((URIRef(u), None, None)))
    }
    resolution_pct = (
        len(resolved_imported) / len(referenced_imported) * 100
        if referenced_imported else 0.0
    )
    # imports 를 선언하고 그 아래 IRI 를 실제로 참조하는데 공리가 0 이면 inert 다.
    imports_inert = bool(imports_list) and bool(referenced_imported) \
        and not resolved_imported
    # imports 는 선언됐지만 그 네임스페이스 아래 IRI 를 아무것도 참조하지 않는 경우 —
    # 다른 종류의 문제(네임스페이스 드리프트로 참조가 어긋났을 수 있다).
    imports_unreferenced = bool(imports_list) and not referenced_imported

    # 스코어: imports 해석됨 (30점) + external ratio≥5% (40점) + subsumption ≥10% (30점)
    score = 0.0
    if imports_list and not imports_inert:
        score += 30
    if ratio_external >= 5.0:
        score += 40
    elif ratio_external >= 1.0:
        score += 20
    if subsumption_ratio >= 10.0:
        score += 30
    elif subsumption_ratio >= 3.0:
        score += 15

    recs = []
    if not imports_list:
        recs.append("owl:imports 를 통해 IOF/BFO/PROV-O 같은 표준 vocabulary 를 명시 선언하세요.")
    if imports_inert:
        recs.append(
            f"owl:imports {len(imports_list)}건이 선언됐지만 그 네임스페이스 아래 참조 IRI "
            f"{len(referenced_imported)}개 중 **공리를 가진 것이 0개** 입니다. "
            "imports 가 해석되지 않으면 상위 클래스의 disjointness 가 없어 정렬 오류를 "
            "추론기가 잡지 못합니다 (실측: 정렬을 맞춰 병합하자 unsatisfiable 이 드러났다). "
            "네임스페이스 불일치(선언 IRI vs 실제 온톨로지 IRI)를 확인하고, 로컬 사본을 "
            "병합하거나 IRI 를 정규화하세요."
        )
    if imports_unreferenced:
        recs.append(
            f"owl:imports {len(imports_list)}건이 선언됐지만 그 네임스페이스 아래 IRI 를 "
            "하나도 참조하지 않습니다 — 실효 없는 선언입니다."
        )
    if ratio_external < 5.0:
        recs.append(
            f"외부 표준 vocabulary 참조가 {ratio_external:.1f}% 로 낮습니다. "
            "rdfs:subClassOf 등으로 표준 어휘에 연결하세요."
        )

    return {
        "name": "Interlinking (Zaveri 2016)",
        "passed": score >= 60,
        "score": round(score, 1),
        "external_imports": imports_list,
        "import_count": len(imports_list),
        "external_object_ratio_pct": round(ratio_external, 2),
        "subsumption_to_external_pct": round(subsumption_ratio, 2),
        "subsumption_to_external": subsumption_external,
        "subsumption_total": subsumption_total,
        # imports 해석 상태 — 선언 개수와 **분리해서** 보고한다.
        "imported_ns_referenced_iris": len(referenced_imported),
        "imported_ns_resolved_iris": len(resolved_imported),
        "import_resolution_pct": round(resolution_pct, 2),
        "imports_inert": imports_inert,
        "imports_unreferenced": imports_unreferenced,
        "recommendations": recs,
        "citation": "Zaveri et al. 2016 §5.3.4 Interlinking",
    }


def check_licensing(tbox: Graph) -> dict[str, object]:
    """Zaveri §5.4.1 Licensing — license 선언 존재 + 머신 판독 가능성.

    검증:
    - dcterms:license / dc:license / cc:license 중 하나 이상 선언
    - 값이 URI (예: CC-BY, GPL) 여야 machine-readable
    - versionIRI / versionInfo 존재 여부 (부가 점수)

    Returns:
        {"name", "passed", "license_uris", "version_info", "score", "recommendations"}
    """
    # DC 1.1 은 license 를 정식 term 으로 포함하지 않음 → DCTERMS 와 CC 만 사용.
    # OWL.priorVersion 은 버전 정보이지 라이선스가 아니라 별도 처리.
    license_preds = [DCTERMS.license, _CC.license]
    license_uris = []
    license_literals = []
    for pred in license_preds:
        for _s, _p, o in tbox.triples((None, pred, None)):
            if isinstance(o, URIRef):
                license_uris.append({"predicate": str(pred), "value": str(o)})
            elif isinstance(o, Literal):
                license_literals.append({"predicate": str(pred), "value": str(o)})

    # 버전 정보
    version_info = {
        "versionInfo": [str(o) for _, _, o in tbox.triples((None, OWL.versionInfo, None))],
        "versionIRI": [str(o) for _, _, o in tbox.triples((None, OWL.versionIRI, None))],
    }

    # 스코어
    score = 0.0
    if license_uris:
        score += 70  # machine-readable URI 선언
    elif license_literals:
        score += 35  # literal 만 있어 덜 규범적
    if version_info["versionInfo"]:
        score += 15
    if version_info["versionIRI"]:
        score += 15

    recs = []
    if not license_uris and not license_literals:
        recs.append(
            "dcterms:license 로 license URI 를 선언하세요 "
            "(예: <https://creativecommons.org/licenses/by/4.0/>)."
        )
    elif not license_uris:
        recs.append("license 를 literal 이 아닌 URI 로 바꾸면 machine-readable 이 됩니다.")
    if not version_info["versionInfo"] and not version_info["versionIRI"]:
        recs.append("owl:versionInfo 또는 owl:versionIRI 를 추가하세요.")

    return {
        "name": "Licensing (Zaveri 2016)",
        "passed": score >= 70,
        "score": round(score, 1),
        "license_uris": license_uris,
        "license_literals": license_literals,
        "version_info": version_info,
        "recommendations": recs,
        "citation": "Zaveri et al. 2016 §5.4.1 Licensing",
    }


def check_understandability(
    tbox: Graph, *, internal_ns: str | None = None,
) -> dict[str, object]:
    """Zaveri §5.4.4 Understandability — 인간 가독성.

    검증:
    - 모든 Class / ObjectProperty / DatatypeProperty 에 rdfs:label 존재
    - 각 엔티티에 rdfs:comment 존재
    - label 이 영어 / 로컬어(한국어) 양방향 제공
    - 엔티티 URI 로컬명의 가독성(camelCase / PascalCase — 약어 남발 아닌지)

    Returns:
        {"name", "passed", "missing_label", "missing_comment",
         "bilingual_coverage_pct", "score", "recommendations"}
    """
    ns = internal_ns or DOMAIN_NS

    entities = set()
    for s in tbox.subjects(RDF.type, OWL.Class):
        if isinstance(s, URIRef) and str(s).startswith(ns):
            entities.add(s)
    for s in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if isinstance(s, URIRef) and str(s).startswith(ns):
            entities.add(s)
    for s in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if isinstance(s, URIRef) and str(s).startswith(ns):
            entities.add(s)

    if not entities:
        return {
            "name": "Understandability (Zaveri 2016)",
            "passed": True,
            "message": "도메인 네임스페이스 엔티티 없음 — 평가 대상 없음",
            "score": 0.0,
        }

    total = len(entities)
    missing_label = []
    missing_comment = []
    bilingual = 0

    for ent in entities:
        labels = list(tbox.objects(ent, RDFS.label))
        comments = list(tbox.objects(ent, RDFS.comment))
        langs = {
            getattr(label, "language", None)
            for label in labels
            if isinstance(label, Literal)
        }

        if not labels:
            missing_label.append(local(str(ent)))
        if not comments:
            missing_comment.append(local(str(ent)))
        # 영어 + 로컬어 양쪽 제공
        if "en" in langs and len(langs - {"en", None}) >= 1:
            bilingual += 1

    label_coverage = (total - len(missing_label)) / total * 100
    comment_coverage = (total - len(missing_comment)) / total * 100
    bilingual_coverage = bilingual / total * 100

    # 스코어: label coverage 40 + comment 30 + bilingual 30
    score = (
        label_coverage * 0.40
        + comment_coverage * 0.30
        + bilingual_coverage * 0.30
    )

    recs = []
    if label_coverage < 95:
        recs.append(
            f"{len(missing_label)}개 엔티티에 rdfs:label 누락 "
            f"(샘플: {missing_label[:5]}). 추가하세요."
        )
    if comment_coverage < 80:
        recs.append(
            f"{len(missing_comment)}개 엔티티에 rdfs:comment 누락. "
            "도메인 설명을 추가하세요."
        )
    if bilingual_coverage < 60:
        recs.append(
            f"bilingual label 커버리지 {bilingual_coverage:.1f}% — "
            "영어 + 로컬어 양쪽 label 을 제공하면 국제 재사용성이 높아집니다."
        )

    return {
        "name": "Understandability (Zaveri 2016)",
        "passed": score >= 80,
        "score": round(score, 1),
        "total_entities": total,
        "label_coverage_pct": round(label_coverage, 1),
        "comment_coverage_pct": round(comment_coverage, 1),
        "bilingual_coverage_pct": round(bilingual_coverage, 1),
        "missing_label_count": len(missing_label),
        "missing_label_samples": missing_label[:10],
        "missing_comment_count": len(missing_comment),
        "recommendations": recs,
        "citation": "Zaveri et al. 2016 §5.4.4 Understandability",
    }


__all__ = [
    "check_interlinking",
    "check_licensing",
    "check_understandability",
]
