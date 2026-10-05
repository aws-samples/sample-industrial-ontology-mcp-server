"""validation_core.py — Syntax, SHACL, Quality Rules.

T-Box 구문 검증, SHACL 검증, 18개 품질 규칙 체크, OWL Cardinality 검증.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from pathlib import Path

import rdflib.collection
from pyshacl import validate as pyshacl_validate
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

from config import ABOX_PATH, GENERATED_TBOX_DIR, SEMANTIC_DICT_PATH, TBOX_PATH
from domain.namespaces import DOMAIN_NS, DOMAIN_NS_OBJ, NS_PREFIX, ONTOLOGY_URI
from domain.rules_paths import RULES_ROOT, rules_path
from domain.tbox_utils import _new_graph
from tools.common import error_response, load_ttl_content, resolve_child_path

# Naming convention patterns
_PASCAL_RE = re.compile(r'^[A-Z][a-zA-Z0-9]*$')
_CAMEL_RE = re.compile(r'^[a-z][a-zA-Z0-9]*$')

logger = logging.getLogger(__name__)

SH = Namespace("http://www.w3.org/ns/shacl#")
SHAPES_NS = Namespace(f"{ONTOLOGY_URI}/shapes#")

# rules/domain/disjoint_groups.json — _rule_disjoint_presence 가 baseline 으로 읽음
DISJOINT_GROUPS_PATH = rules_path("disjoint_groups.json")

RULES_DIR = Path(RULES_ROOT)


# ── SHACL engine selection (pyshacl | pyrudof) ─────────────────────────────
# pyshacl 은 순수 Python 으로 정확성은 높지만 대규모에서 느리다 (36K 인스턴스 기준
# 100s+). pyrudof 는 Rust 구현으로 동일 SHACL Core 를 ~20-30× 빠르게 실행한다.
# 기본값은 환경변수 SHACL_ENGINE 으로 선택, 호출 단위 override 가능.
#
# 반환 포맷은 기존 pyshacl 과 동일한 3-tuple 로 정규화:
#   (conforms: bool, results_graph: rdflib.Graph, results_text: str)
# → 기존 _parse_shacl_violations(results_graph) 가 그대로 재사용 가능.


def _run_shacl(
    data_graph: Graph | None = None,
    data_ttl: str | None = None,
    shacl_graph: Graph | None = None,
    shapes_ttl: str | None = None,
    *,
    engine: str | None = None,
    abort_on_first: bool = False,
    inference: str = "none",
) -> tuple[bool, Graph, str]:
    """SHACL 검증을 pyshacl 또는 pyrudof 로 실행.

    Args:
        data_graph: rdflib Graph. ``data_ttl`` 과 중 하나 필수.
        data_ttl: Turtle 문자열.
        shacl_graph: shapes rdflib Graph.
        shapes_ttl: shapes Turtle 문자열.
        engine: ``"pyshacl"`` | ``"pyrudof"`` | ``None`` (= env SHACL_ENGINE, default pyshacl).
        abort_on_first: pyshacl 전용, pyrudof 는 기본 full.
        inference: pyshacl 전용, pyrudof 는 추론 미적용.

    Returns:
        (conforms, results_graph, results_text)
    """
    if engine is None:
        # Rust 기반 pyrudof 가 pyshacl 대비 ~20-30× 빠르고 parity 테스트로
        # conformance 동등성 검증됨 (tests/test_shacl_engine_parity.py).
        # pyshacl 로 폴백하려면 SHACL_ENGINE=pyshacl 환경변수.
        engine = os.getenv("SHACL_ENGINE", "pyrudof").lower()

    if engine == "pyrudof":
        return _run_shacl_pyrudof(
            data_graph=data_graph, data_ttl=data_ttl,
            shacl_graph=shacl_graph, shapes_ttl=shapes_ttl,
        )

    # default pyshacl
    if data_graph is None:
        data_graph = _new_graph()
        data_graph.parse(data=data_ttl or "", format="turtle")
    return pyshacl_validate(
        data_graph=data_graph,
        shacl_graph=shacl_graph,
        inference=inference,
        abort_on_first=abort_on_first,
    )


def _run_shacl_pyrudof(
    data_graph: Graph | None = None,
    data_ttl: str | None = None,
    shacl_graph: Graph | None = None,
    shapes_ttl: str | None = None,
) -> tuple[bool, Graph, str]:
    """pyrudof SHACL validator 를 pyshacl 과 동일한 3-tuple 로 반환.

    data / shapes 중 rdflib Graph 가 주어지면 내부에서 Turtle 직렬화 한 번 수행.
    이미 Turtle 문자열이 있다면 그것을 그대로 사용해 직렬화 오버헤드 회피.
    """
    try:
        # ``from pyrudof import ...`` 자체가 가용성 검사다 — 미설치면 ImportError.
        from pyrudof import (
            RDFFormat,
            ReaderMode,
            ResultShaclValidationFormat,
            Rudof,
            RudofConfig,
            ShaclFormat,
            ShaclValidationMode,
        )
    except ImportError as e:
        raise RuntimeError(
            "pyrudof 미설치 — pip install pyrudof 또는 engine='pyshacl' 사용"
        ) from e

    if data_ttl is None:
        if data_graph is None:
            raise ValueError("data_graph 또는 data_ttl 필수")
        data_ttl = data_graph.serialize(format="turtle")

    if shapes_ttl is None:
        if shacl_graph is None or len(shacl_graph) == 0:
            # pyrudof는 shapes 없으면 에러 — 빈 report 반환
            empty_g = _new_graph()
            return True, empty_g, ""
        shapes_ttl = shacl_graph.serialize(format="turtle")

    r = Rudof(RudofConfig())
    r.read_data(data_ttl, RDFFormat.Turtle, None, ReaderMode.Lax)
    r.read_shacl(shapes_ttl, ShaclFormat.Turtle, None, ReaderMode.Lax)
    r.validate_shacl(ShaclValidationMode.Native)
    report_ttl = r.serialize_shacl_validation_results(
        ResultShaclValidationFormat.Turtle,
    )

    # Parse report back to rdflib Graph — 기존 _parse_shacl_violations 재사용
    rg = _new_graph()
    with contextlib.suppress(Exception):
        rg.parse(data=report_ttl, format="turtle")
    conforms = (rg.value(predicate=SH.conforms) == Literal(True)) or \
               any(
                   o == Literal(True)
                   for s, p, o in rg.triples((None, SH.conforms, None))
               )

    try:
        text_compact = r.serialize_shacl_validation_results(
            ResultShaclValidationFormat.Compact,
        )
    except Exception:
        text_compact = ""
    return conforms, rg, text_compact

# Violation rules that can be fixed deterministically (no LLM needed)
DETERMINISTIC_RULES = {
    "missing_label",
    "missing_comment",
    "missing_domain",
    "missing_range",
    "domain_mismatch",
    "range_mismatch",
}

# Violation rules that require LLM reasoning to fix
LLM_REQUIRED_RULES = {
    "subclass_issue",
    "circular_property",
    "structural_error",
}


def _load_ttl(ttl_content: str, ttl_path: str) -> str:
    """ttl_content 또는 ttl_path에서 TTL 문자열을 로드한다."""
    return load_ttl_content(ttl_content, ttl_path, TBOX_PATH)


def validate_ttl_syntax(ttl_content: str = "", ttl_path: str = "") -> str:
    """TTL(Turtle) 구문을 검증한다. rdflib로 파싱하여 문법 오류를 확인.

    Args:
        ttl_content: 검증할 Turtle 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
    """
    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl(ttl_content, ttl_path)
        g = _new_graph()
        g.parse(data=ttl, format="turtle")
        subjects = set(g.subjects())
        predicates = set(g.predicates())
        objects = set(g.objects())
        return json.dumps({
            "success": True,
            "triples": len(g),
            "subjects": len(subjects),
            "predicates": len(predicates),
            "objects": len(objects),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False, indent=2)


def validate_shacl(ttl_content: str = "", ttl_path: str = "", shapes_ttl: str = "",
                    engine: str = "") -> str:
    """SHACL 검증을 실행한다.

    Args:
        ttl_content: 검증 대상 TTL 데이터. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
        shapes_ttl: SHACL shapes TTL. 비어있으면 기본 OWL 클래스 구조 검증만 수행.
        engine: "pyshacl" (기본) | "pyrudof" (Rust, 대규모 ~20-30× 가속).
                빈 값이면 환경변수 SHACL_ENGINE, 없으면 pyshacl.
    """
    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl(ttl_content, ttl_path)
        data_graph = _new_graph()
        data_graph.parse(data=ttl, format="turtle")

        if shapes_ttl:
            shapes_graph = _new_graph()
            shapes_graph.parse(data=shapes_ttl, format="turtle")
        else:
            shapes_graph = None

        conforms, results_graph, results_text = _run_shacl(
            data_graph=data_graph,
            shacl_graph=shapes_graph,
            engine=engine or None,
            abort_on_first=False,
        )
        # violations_count: pyshacl 텍스트 포맷은 "Constraint Violation" 출현으로
        # 카운트했다. pyrudof 는 표 형태 텍스트라 같은 토큰이 없으므로 폴백으로
        # results_graph 의 sh:result 노드 수를 사용한다.
        _text_count = results_text.count("Constraint Violation") if results_text else 0
        _graph_count = 0
        with contextlib.suppress(Exception):
            _graph_count = sum(
                1 for _ in results_graph.subjects(RDF.type, SH.ValidationResult)
            )
        if not conforms and _text_count == 0:
            violations_count = _graph_count or 1
        else:
            violations_count = max(_text_count, _graph_count)
        return json.dumps({
            "success": True,
            "conforms": conforms,
            "results_text": results_text,
            "violations_count": violations_count,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def _has_cycle(g: Graph, start: URIRef, steel_classes: set, visited: set | None = None) -> bool:
    """subClassOf 순환을 재귀 탐지한다."""
    if visited is None:
        visited = set()
    if start in visited:
        return True
    visited.add(start)
    for parent in g.objects(start, RDFS.subClassOf):
        if isinstance(parent, URIRef) and parent in steel_classes and _has_cycle(g, parent, steel_classes, visited.copy()):
            return True
    return False


def _rule_circular_property(g: Graph, steel_ns: str, transitive_props: set) -> list[dict]:
    """1. 순환 프로퍼티 검사 (TransitiveProperty + inverseOf)."""
    issues: list[dict] = []
    for prop in transitive_props:
        inverses = list(g.objects(prop, OWL.inverseOf))
        if inverses:
            issues.append({
                "rule": "circular_property",
                "severity": "critical",
                "message": f"{prop} 은 TransitiveProperty이면서 inverseOf가 있음 → 무한 추론 루프 위험",
            })
    return issues


def _rule_disjoint_style(g: Graph) -> list[dict]:
    """2. AllDisjointClasses 사용 확인."""
    issues: list[dict] = []
    disjoint_classes = list(g.subjects(RDF.type, OWL.AllDisjointClasses))
    pairwise_disjoint = list(g.subject_objects(OWL.disjointWith))
    if pairwise_disjoint and not disjoint_classes:
        issues.append({
            "rule": "disjoint_style",
            "severity": "warning",
            "message": "pairwise disjointWith 사용 중. AllDisjointClasses로 변환 권장.",
        })
    return issues


def _rule_missing_labels(g: Graph, steel_ns: str, classes: set) -> list[dict]:
    """3. 레이블 완전성 (클래스에 @en, @ko 레이블 확인)."""
    issues: list[dict] = []
    for cls in classes:
        if not str(cls).startswith(steel_ns):
            continue
        labels = list(g.objects(cls, RDFS.label))
        langs = {
            lab.language for lab in labels
            if hasattr(lab, "language") and lab.language
        }
        if "en" not in langs:
            issues.append({
                "rule": "missing_label",
                "severity": "high",
                "message": f"{cls} 에 @en 레이블 없음",
            })
        if "ko" not in langs:
            issues.append({
                "rule": "missing_label",
                "severity": "high",
                "message": f"{cls} 에 @ko 레이블 없음",
            })
    return issues


def _rule_missing_domain_range(g: Graph, steel_ns: str, obj_props: set) -> list[dict]:
    """4. domain/range 완전성 (ObjectProperty)."""
    issues: list[dict] = []
    for prop in obj_props:
        if not str(prop).startswith(steel_ns):
            continue
        domains = list(g.objects(prop, RDFS.domain))
        ranges = list(g.objects(prop, RDFS.range))
        if not domains:
            issues.append({
                "rule": "missing_domain",
                "severity": "high",
                "message": f"ObjectProperty {prop} 에 domain 없음",
            })
        if not ranges:
            issues.append({
                "rule": "missing_range",
                "severity": "high",
                "message": f"ObjectProperty {prop} 에 range 없음",
            })
    return issues


def _rule_isolated_class(g: Graph, steel_ns: str, steel_classes: set) -> list[dict]:
    """5. 고립 클래스 (OOPS! P04) — 다른 엔티티와 연결 없음."""
    issues: list[dict] = []
    for cls in steel_classes:
        has_parent = any(True for p in g.objects(cls, RDFS.subClassOf)
                         if isinstance(p, URIRef) and str(p).startswith(steel_ns))
        has_child = any(True for _ in g.subjects(RDFS.subClassOf, cls))
        has_domain = any(True for _ in g.subjects(RDFS.domain, cls))
        has_range = any(True for _ in g.subjects(RDFS.range, cls))
        if not (has_parent or has_child or has_domain or has_range):
            issues.append({
                "rule": "isolated_class",
                "severity": "warning",
                "message": f"{cls} 은 다른 클래스/프로퍼티와 연결 없음 (고립 클래스)",
            })
    return issues


def _rule_subclass_cycle(g: Graph, steel_classes: set) -> list[dict]:
    """6. subClassOf 순환 (OOPS! P06) — A⊂B⊂A."""
    issues: list[dict] = []
    for cls in steel_classes:
        for parent in g.objects(cls, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent in steel_classes and _has_cycle(g, parent, steel_classes, {cls}):
                    issues.append({
                        "rule": "subclass_cycle",
                        "severity": "critical",
                        "message": f"subClassOf 순환 감지: {cls} 관련",
                    })
                    break
    return issues


def _rule_multiple_domain_range(g: Graph, steel_ns: str, obj_props: set) -> list[dict]:
    """7. 다중 domain/range (OOPS! P19) — 프로퍼티에 2개 이상 domain 또는 range."""
    issues: list[dict] = []
    for prop in obj_props:
        if not str(prop).startswith(steel_ns):
            continue
        domains = [d for d in g.objects(prop, RDFS.domain) if isinstance(d, URIRef)]
        ranges = [r for r in g.objects(prop, RDFS.range) if isinstance(r, URIRef)]
        if len(domains) > 1:
            issues.append({
                "rule": "multiple_domains",
                "severity": "high",
                "message": f"ObjectProperty {prop} 에 domain이 {len(domains)}개 — OWL에서 intersection으로 해석됨",
            })
        if len(ranges) > 1:
            issues.append({
                "rule": "multiple_ranges",
                "severity": "high",
                "message": f"ObjectProperty {prop} 에 range가 {len(ranges)}개 — OWL에서 intersection으로 해석됨",
            })
    return issues


def _rule_self_subclass(g: Graph, steel_classes: set) -> list[dict]:
    """8. 자기 참조 subClassOf (OOPS! P24) — A subClassOf A."""
    issues: list[dict] = []
    for cls in steel_classes:
        if (cls, RDFS.subClassOf, cls) in g:
            issues.append({
                "rule": "self_subclass",
                "severity": "critical",
                "message": f"{cls} 이 자기 자신의 subClass로 선언됨",
            })
    return issues


def _rule_self_inverse(g: Graph, steel_ns: str, obj_props: set) -> list[dict]:
    """9. 자기 참조 inverseOf (OOPS! P25) — P inverseOf P."""
    issues: list[dict] = []
    for prop in obj_props:
        if not str(prop).startswith(steel_ns):
            continue
        if (prop, OWL.inverseOf, prop) in g:
            issues.append({
                "rule": "self_inverse",
                "severity": "high",
                "message": f"ObjectProperty {prop} 이 자기 자신의 inverseOf로 선언됨",
            })
    return issues


def _rule_transitive_self_loop(g: Graph, steel_ns: str, transitive_props: set) -> list[dict]:
    """10. TransitiveProperty 오용 (OOPS! P29) — 트리플 폭발 위험 프로퍼티."""
    issues: list[dict] = []
    for prop in transitive_props:
        if not str(prop).startswith(steel_ns):
            continue
        # 이미 #1에서 inverseOf 조합은 체크, 여기서는 domain=range인 경우 경고
        domains = [str(d) for d in g.objects(prop, RDFS.domain)]
        ranges = [str(r) for r in g.objects(prop, RDFS.range)]
        if domains and ranges and domains[0] == ranges[0]:
            issues.append({
                "rule": "transitive_self_loop_risk",
                "severity": "warning",
                "message": f"TransitiveProperty {prop} 의 domain=range={domains[0]} — 데이터에 따라 추론 폭발 가능",
            })
    return issues


def _rule_naming_class(steel_classes: set) -> list[dict]:
    """11. 클래스 네이밍 — PascalCase."""
    issues: list[dict] = []
    for cls in steel_classes:
        local = str(cls).split('#')[-1].split('/')[-1]
        if local and not _PASCAL_RE.match(local):
            issues.append({
                "rule": "naming_class",
                "severity": "warning",
                "message": f"Class {local} violates PascalCase convention",
            })
    return issues


def _rule_naming_property(steel_ns: str, obj_props: set, data_props: set) -> list[dict]:
    """12. 프로퍼티 네이밍 — camelCase.

    검사 분기:
    - local name 에 ':' 가 포함되면 URI 포매팅 오류 (prefix 가 URI 본체에 중첩)
      → severity=high, rule=invalid_uri_format
    - 그 외 non-camelCase → warning, rule=naming_property
    """
    issues: list[dict] = []
    all_properties = {p for p in (obj_props | data_props) if str(p).startswith(steel_ns)}
    for prop in all_properties:
        local = str(prop).split('#')[-1].split('/')[-1]
        if not local:
            continue
        if ":" in local:
            issues.append({
                "rule": "invalid_uri_format",
                "severity": "high",
                "message": (f"Property URI '{local}' contains prefix colon "
                            f"— steel namespace already applied, double prefix detected"),
            })
            continue
        if not _CAMEL_RE.match(local):
            issues.append({
                "rule": "naming_property",
                "severity": "warning",
                "message": f"Property {local} violates camelCase convention",
            })
    return issues


def _rule_missing_sibling_disjoint(g: Graph, steel_ns: str, steel_classes: set) -> list[dict]:
    """13. 형제 클래스 disjoint 선언 누락 (advisory)."""
    issues: list[dict] = []
    declared_disjoint: set[frozenset[str]] = set()
    for bnode in g.subjects(RDF.type, OWL.AllDisjointClasses):
        members_node = list(g.objects(bnode, OWL.members))
        if members_node:
            try:
                members = list(rdflib.collection.Collection(g, members_node[0]))
                for i, a in enumerate(members):
                    for b in members[i + 1:]:
                        declared_disjoint.add(frozenset([str(a), str(b)]))
            except Exception:  # noqa: BLE001 — 깨진 disjoint 목록은 해당 그룹만 건너뛴다
                pass

    for parent in steel_classes:
        children = [c for c in g.subjects(RDFS.subClassOf, parent)
                    if str(c).startswith(steel_ns)]
        if len(children) < 2:
            continue
        for i, a in enumerate(children):
            for b in children[i + 1:]:
                pair = frozenset([str(a), str(b)])
                if pair not in declared_disjoint:
                    a_local = str(a).split('#')[-1].split('/')[-1]
                    b_local = str(b).split('#')[-1].split('/')[-1]
                    issues.append({
                        "rule": "missing_sibling_disjoint",
                        "severity": "info",
                        "message": f"{a_local} and {b_local} are siblings but not declared disjoint",
                    })
    return issues


def _rule_duplicate_label(g: Graph, steel_classes: set, obj_props: set, data_props: set) -> list[dict]:
    """14. 중복 레이블 (ROBOT P2) — 다른 엔티티가 같은 rdfs:label 공유."""
    issues: list[dict] = []
    label_map: dict[str, list[str]] = {}  # label_text → [entity_uris]
    for entity in (steel_classes | obj_props | data_props):
        for label in g.objects(entity, RDFS.label):
            if hasattr(label, 'language'):
                key = f"{str(label)}@{label.language}"
                label_map.setdefault(key, []).append(str(entity))
    for label_key, entities in label_map.items():
        if len(entities) > 1:
            entity_names = [e.split('#')[-1].split('/')[-1] for e in entities]
            issues.append({
                "rule": "duplicate_label",
                "severity": "warning",
                "message": f"Duplicate label '{label_key}' shared by: {', '.join(entity_names[:5])}",
            })
    return issues


def _rule_self_referencing_restriction(g: Graph, steel_classes: set) -> list[dict]:
    """15. 자기참조 Restriction (ROBOT P26)."""
    issues: list[dict] = []
    for cls in steel_classes:
        for parent in g.objects(cls, RDFS.subClassOf):
            if not isinstance(parent, BNode):
                continue
            if (parent, RDF.type, OWL.Restriction) not in g:
                continue
            on_prop = g.value(parent, OWL.onProperty)
            if on_prop:
                prop_domain = g.value(on_prop, RDFS.domain)
                if prop_domain == cls:
                    cls_name = str(cls).split('#')[-1]
                    prop_name = str(on_prop).split('#')[-1]
                    issues.append({
                        "rule": "self_referencing_restriction",
                        "severity": "warning",
                        "message": f"{cls_name} has Restriction on {prop_name} whose domain is {cls_name} itself (trivially satisfied)",
                    })
    return issues


def _rule_asymmetric_annotation(g: Graph, steel_classes: set) -> list[dict]:
    """16. 비대칭 어노테이션 (ROBOT) — label 있지만 comment 없음 (또는 반대)."""
    issues: list[dict] = []
    for entity in steel_classes:
        has_label = any(True for _ in g.objects(entity, RDFS.label))
        has_comment = any(True for _ in g.objects(entity, RDFS.comment))
        if has_label and not has_comment:
            name = str(entity).split('#')[-1]
            issues.append({
                "rule": "asymmetric_annotation",
                "severity": "info",
                "message": f"{name} has rdfs:label but no rdfs:comment",
            })
    return issues


def _rule_deprecated_reference(g: Graph, steel_ns: str) -> list[dict]:
    """17. Deprecated 엔티티 참조 (ROBOT P3).

    **익명 클래스 표현식은 제외한다.** skolemize 된 ``Union_*`` / ``owl:Restriction``
    본체에 과거 실행이 붙여 둔 ``owl:deprecated`` 는 결함이 아니다 — 그 표현식을
    ``owl:equivalentClass`` 로 참조하는 것은 정상 구조이므로 "deprecated 인데
    참조됨" 이 영구히 참이 된다.

    실측 (2026-08-19): ``Union_ManufacturingProcessStep_18a9fee9`` 한 개가 high 1
    을 고정시켜 ``check_quality_rules`` 가 **baseline FAIL** 이 됐고, ``_caught_by``
    는 PASS→FAIL 강등만 세므로 이 체크로는 **어떤 mutant 도 검출되지 않았다**
    (S4.5 검출률 14.3%). 생성 지점 가드(step_21)는 커밋 420b250 에서 고쳤지만
    **이미 파일에 박힌 플래그는 아무도 걷어내지 않는다** — step_21 은 이제 익명
    노드를 아예 보지 않으므로 자기가 과거에 붙인 것을 제거하지 못한다.

    프로퍼티(OP/DP)의 deprecated 는 계속 검사한다 — ``is_named_domain_class`` 를
    필터로 쓰면 그 정당한 지적까지 사라지므로 표현식 축만 보는 헬퍼를 쓴다.
    """
    from domain.graph_utils import is_anonymous_class_expression

    issues: list[dict] = []
    deprecated = set()
    for s in g.subjects(OWL.deprecated, Literal(True)):
        if str(s).startswith(steel_ns) and not is_anonymous_class_expression(
            g, s, steel_ns,
        ):
            deprecated.add(s)
    for dep_entity in deprecated:
        # Check if any non-deprecated entity references this
        referrers = set()
        for s, p, _o in g.triples((None, None, dep_entity)):
            if p not in (OWL.deprecated, RDF.type) and str(s).startswith(steel_ns):
                referrers.add(str(s).split('#')[-1])
        if referrers:
            dep_name = str(dep_entity).split('#')[-1]
            issues.append({
                "rule": "deprecated_reference",
                "severity": "high",
                "message": f"Deprecated entity {dep_name} is still referenced by: {', '.join(sorted(referrers)[:5])}",
            })
    return issues


def _rule_equivalent_class_cycle(g: Graph, steel_classes: set) -> list[dict]:
    """19. equivalentClass 순환 탐지 — A≡B≡A."""
    issues: list[dict] = []
    # Build equivalentClass adjacency
    eq_adj: dict[URIRef, set[URIRef]] = {}
    for cls in steel_classes:
        eq_adj[cls] = set()
        for eq in g.objects(cls, OWL.equivalentClass):
            if isinstance(eq, URIRef) and eq in steel_classes:
                eq_adj[cls].add(eq)

    # DFS cycle detection
    visited_global: set[URIRef] = set()
    for start in steel_classes:
        if start in visited_global:
            continue
        stack = [(start, {start})]
        while stack:
            node, path = stack.pop()
            visited_global.add(node)
            for neighbor in eq_adj.get(node, set()):
                if neighbor in path and neighbor != node:
                    # Skip trivial A≡A (legitimate)
                    cycle_names = [str(n).split('#')[-1].split('/')[-1] for n in path]
                    issues.append({
                        "rule": "equivalent_class_cycle",
                        "severity": "critical",
                        "message": f"equivalentClass 순환 감지: {' ≡ '.join(cycle_names)} ≡ {str(neighbor).split('#')[-1]}",
                    })
                    break
                if neighbor not in visited_global:
                    stack.append((neighbor, path | {neighbor}))
    return issues


def _rule_sub_property_cycle(g: Graph, steel_ns: str, obj_props: set, data_props: set) -> list[dict]:
    """20. subPropertyOf 순환 탐지 — P1 ⊂ P2 ⊂ P1."""
    issues: list[dict] = []
    all_props = {p for p in (obj_props | data_props) if str(p).startswith(steel_ns)}

    def _has_prop_cycle(start: URIRef, visited: set | None = None) -> bool:
        if visited is None:
            visited = set()
        if start in visited:
            return True
        visited.add(start)
        for parent in g.objects(start, RDFS.subPropertyOf):
            if (isinstance(parent, URIRef) and parent in all_props
                    and _has_prop_cycle(parent, visited.copy())):
                return True
        return False

    for prop in all_props:
        for parent in g.objects(prop, RDFS.subPropertyOf):
            if (isinstance(parent, URIRef) and parent in all_props
                    and _has_prop_cycle(parent, {prop})):
                prop_name = str(prop).split('#')[-1].split('/')[-1]
                issues.append({
                    "rule": "sub_property_cycle",
                    "severity": "critical",
                    "message": f"subPropertyOf 순환 감지: {prop_name} 관련",
                })
                break

    # Also check domain/range consistency in subPropertyOf chains
    for prop in all_props:
        for parent in g.objects(prop, RDFS.subPropertyOf):
            if not isinstance(parent, URIRef) or parent not in all_props:
                continue
            prop_domains = set(str(d) for d in g.objects(prop, RDFS.domain))
            parent_domains = set(str(d) for d in g.objects(parent, RDFS.domain))
            prop_ranges = set(str(r) for r in g.objects(prop, RDFS.range))
            parent_ranges = set(str(r) for r in g.objects(parent, RDFS.range))
            # In OWL, sub-property domain should be same or narrower
            if prop_domains and parent_domains and not prop_domains & parent_domains:
                p_name = str(prop).split('#')[-1]
                pp_name = str(parent).split('#')[-1]
                issues.append({
                    "rule": "sub_property_domain_mismatch",
                    "severity": "warning",
                    "message": f"{p_name} subPropertyOf {pp_name} 이지만 domain이 불일치",
                })
            if prop_ranges and parent_ranges and not prop_ranges & parent_ranges:
                p_name = str(prop).split('#')[-1]
                pp_name = str(parent).split('#')[-1]
                issues.append({
                    "rule": "sub_property_range_mismatch",
                    "severity": "warning",
                    "message": f"{p_name} subPropertyOf {pp_name} 이지만 range가 불일치",
                })
    return issues


def _rule_disjoint_presence(g: Graph) -> list[dict]:
    """21. AllDisjointClasses 존재 보장.

    R10 meta-audit (2026-04-27) 가 발견한 blind-spot: T-Box 에서 AllDisjointClasses
    블록을 지워도 어떤 validator 도 잡지 못함. rules/domain/disjoint_groups.json 을
    baseline 으로 쓰면 "선언되어야 할 그룹 중 실제 T-Box 에 몇 개 있는지" 판정 가능.

    rules/domain/disjoint_groups.json 이 없는 도메인(선언되지 않음)에서는 skip (빈 리스트).

    WARN 기준: 선언된 그룹 대비 T-Box 의 AllDisjointClasses 수가 절반 미만.
    """
    issues: list[dict] = []

    try:
        with open(DISJOINT_GROUPS_PATH, encoding="utf-8") as f:
            rules_data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return issues

    declared_groups = rules_data.get("groups", [])
    # T-Box 에 실제로 존재하는 steel: 클래스만 카운트 대상.
    # minimal fixture (2 클래스) 는 disjoint_groups.json 13 그룹과 무관하므로
    # 실제로 매칭되는 그룹만 expected 로 셈.
    tbox_class_locals: set[str] = set()
    for cls in g.subjects(RDF.type, OWL.Class):
        if isinstance(cls, URIRef):
            s = str(cls)
            if s.startswith(DOMAIN_NS):
                tbox_class_locals.add(s.split("#")[-1].split("/")[-1])

    expected_count = 0
    for grp in declared_groups:
        if not isinstance(grp, dict):
            continue
        grp_classes = grp.get("classes", [])
        if not isinstance(grp_classes, list) or len(grp_classes) < 2:
            continue
        # 이 그룹의 ≥2 개 클래스가 T-Box 에 실제로 있어야 disjoint 선언 의미 있음
        present = [c for c in grp_classes if c in tbox_class_locals]
        if len(present) >= 2:
            expected_count += 1

    # ≥3 그룹이 expected 여야 actionable signal. 1~2 는 fixture / 부분 T-Box 의
    # 자연스런 상태라 noise. R10 meta-audit 의 핵심 타깃은 "많은 disjoint 가 일괄
    # 누락됐을 때 (mutation delete_disjoint 등) 반드시 잡는 것".
    if expected_count < 3:
        return issues

    tbox_disjoint_count = sum(1 for _ in g.subjects(RDF.type, OWL.AllDisjointClasses))

    if tbox_disjoint_count < expected_count:
        # 완전 누락 (0 개) 은 critical — delete_disjoint 등 대량 삭제 확실히 catch.
        # 부분 누락 (1+ 있지만 expected 미만) 도 critical — single delete_disjoint
        # mutation 이나 일부 그룹 누락도 잡아야 R10 M4 blind-spot 해소.
        # T-Box fitness 문제는 warning 이 아니라 structural gap 이므로 critical.
        missing = expected_count - tbox_disjoint_count
        severity = "critical"
        issues.append({
            "rule": "disjoint_presence",
            "severity": severity,
            "message": (
                f"rules/domain/disjoint_groups.json 에 {expected_count} 그룹이 선언됐지만 "
                f"T-Box 에는 AllDisjointClasses 가 {tbox_disjoint_count} 개만 있음 "
                f"(누락 {missing}). improve_tbox_quality 로 완전화 필요."
            ),
        })
    return issues


def _check_quality_rules_internal(g: Graph) -> dict:
    """check_quality_rules의 내부 로직. 이미 파싱된 Graph를 받아 검사한다.

    Returns:
        {"issues": [...], "issues_count": int, "critical": int, ...}
    """
    steel_ns = DOMAIN_NS

    # Shared entity sets — computed once, passed to individual rules.
    #
    # **익명 클래스 표현식의 skolem 이름을 제외한다.** skolemize 는
    # ``equivalentClass [unionOf ...]`` / ``owl:Restriction`` bnode 에 IRI 를 붙이는데,
    # 이름이 생겨도 명명된 도메인 클래스가 아니다. 예전에는 그것들이 이 세트에
    # 섞여 라벨·고립·네이밍 규칙의 대상이 됐다.
    #
    # 2026-08-19 S4.5 mutation 감사 실측: ``Union_ManufacturingProcessStep_18a9fee9``
    # **한 개** 노드가 high 3건(missing_label @en/@ko + deprecated_reference)을
    # 만들어 이 게이트를 baseline FAIL 로 고정시켰다. 그 결과 mutant 와 델타가
    # 생기지 않아 **어떤 변형도 구분하지 못하는 죽은 게이트** 가 됐다 —
    # 적용 7개 중 1개만 검출(14.3%)이고, 생존한 M5 3건(label/comment 삭제)은
    # 정확히 이 게이트의 담당 영역이었다.
    #
    # 판정은 ``domain.graph_utils`` 공용 헬퍼만 쓴다. 실측 23개 열거 지점 중 21개가
    # 배제 없이 돌고 있어, 여기만 고치면 다른 스텝이 다음 실행에 같은 오염을 다시
    # 만든다 (사본이 퍼진 로직 — 이 리포에서 반복된 유형).
    from domain.graph_utils import named_domain_classes

    steel_classes = named_domain_classes(g, str(steel_ns))
    # 외래 클래스는 그대로 포함한다 (라벨 규칙이 도메인 prefix 로 다시 걸러낸다).
    classes = steel_classes | {
        c for c in g.subjects(RDF.type, OWL.Class)
        if not str(c).startswith(str(steel_ns))
    }
    obj_props = set(g.subjects(RDF.type, OWL.ObjectProperty))
    data_props = set(g.subjects(RDF.type, OWL.DatatypeProperty))
    transitive_props = set(g.subjects(RDF.type, OWL.TransitiveProperty))

    # Run all 19 rules and aggregate issues
    issues: list[dict] = []
    issues.extend(_rule_circular_property(g, steel_ns, transitive_props))
    issues.extend(_rule_disjoint_style(g))
    issues.extend(_rule_missing_labels(g, steel_ns, classes))
    issues.extend(_rule_missing_domain_range(g, steel_ns, obj_props))
    issues.extend(_rule_isolated_class(g, steel_ns, steel_classes))
    issues.extend(_rule_subclass_cycle(g, steel_classes))
    issues.extend(_rule_multiple_domain_range(g, steel_ns, obj_props))
    issues.extend(_rule_self_subclass(g, steel_classes))
    issues.extend(_rule_self_inverse(g, steel_ns, obj_props))
    issues.extend(_rule_transitive_self_loop(g, steel_ns, transitive_props))
    issues.extend(_rule_naming_class(steel_classes))
    issues.extend(_rule_naming_property(steel_ns, obj_props, data_props))
    issues.extend(_rule_missing_sibling_disjoint(g, steel_ns, steel_classes))
    issues.extend(_rule_duplicate_label(g, steel_classes, obj_props, data_props))
    issues.extend(_rule_self_referencing_restriction(g, steel_classes))
    issues.extend(_rule_asymmetric_annotation(g, steel_classes))
    issues.extend(_rule_deprecated_reference(g, steel_ns))
    issues.extend(_rule_equivalent_class_cycle(g, steel_classes))
    issues.extend(_rule_sub_property_cycle(g, steel_ns, obj_props, data_props))
    issues.extend(_rule_disjoint_presence(g))

    # 통계
    stats = {
        "classes": len(steel_classes),
        "object_properties": len([p for p in obj_props if str(p).startswith(steel_ns)]),
        "data_properties": len([p for p in data_props if str(p).startswith(steel_ns)]),
        "total_triples": len(g),
    }

    return {
        "issues": issues,
        "issues_count": len(issues),
        "critical": len([i for i in issues if i["severity"] == "critical"]),
        "high": len([i for i in issues if i["severity"] == "high"]),
        "warning": len([i for i in issues if i["severity"] == "warning"]),
        "info": len([i for i in issues if i["severity"] == "info"]),
        "statistics": stats,
    }


def check_quality_rules(ttl_content: str = "", ttl_path: str = "") -> str:
    """T-Box 품질 규칙을 체크한다. (순환 프로퍼티, AllDisjointClasses, 레이블 완전성, 디자인 패턴 등 19개 규칙)

    Args:
        ttl_content: 검증할 T-Box TTL 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
    """
    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl(ttl_content, ttl_path)
        g = _new_graph()
        g.parse(data=ttl, format="turtle")
    except Exception as e:
        return error_response(f"TTL 파싱 실패: {e}", logger=logger)
    result = _check_quality_rules_internal(g)
    result["success"] = True
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# SHACL helpers
# ---------------------------------------------------------------------------

def _build_shapes_from_semantic_dict() -> Graph:
    """시맨틱 딕셔너리에서 동적 SHACL NodeShape을 생성한다.

    data/generated/semantic_dictionary.json을 읽어 각 클래스에 대해:
    - sh:targetClass로 클래스 지정
    - 필수 DatatypeProperty에 sh:datatype + sh:minCount 0
    - ObjectProperty에 sh:class로 range 지정
    """
    shapes = _new_graph()
    shapes.bind("sh", SH)
    shapes.bind("owl", OWL)
    shapes.bind("rdfs", RDFS)
    shapes.bind(NS_PREFIX, DOMAIN_NS_OBJ)
    shapes.bind("shapes", SHAPES_NS)

    if not os.path.exists(SEMANTIC_DICT_PATH):
        return shapes

    try:
        with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
            sem_dict = json.load(f)
    except (json.JSONDecodeError, OSError):
        return shapes

    # 딕셔너리 구조 A: {"classes": [{name, data_properties, object_properties}, ...]}
    # 딕셔너리 구조 B: {"classes": {"ClassName": {data_properties, object_properties}}}
    # 최상위 flat dict 도 지원.
    raw_classes = sem_dict.get("classes", [])
    classes: list[dict] = []
    if isinstance(raw_classes, list):
        classes = [c for c in raw_classes if isinstance(c, dict)]
    elif isinstance(raw_classes, dict):
        for key, val in raw_classes.items():
            if isinstance(val, dict):
                entry = dict(val)
                entry.setdefault("name", key)
                classes.append(entry)
    if not classes and isinstance(sem_dict, dict):
        for key, val in sem_dict.items():
            if isinstance(val, dict) and ("data_properties" in val or "object_properties" in val):
                entry = dict(val)
                entry.setdefault("name", key)
                classes.append(entry)

    XSD = Namespace("http://www.w3.org/2001/XMLSchema#")
    XSD_MAP = {
        "string": XSD.string, "xsd:string": XSD.string,
        "integer": XSD.integer, "xsd:integer": XSD.integer,
        "int": XSD.integer, "xsd:int": XSD.integer,
        "float": XSD.float, "xsd:float": XSD.float,
        "double": XSD.double, "xsd:double": XSD.double,
        "decimal": XSD.decimal, "xsd:decimal": XSD.decimal,
        "boolean": XSD.boolean, "xsd:boolean": XSD.boolean,
        "date": XSD.date, "xsd:date": XSD.date,
        "dateTime": XSD.dateTime, "xsd:dateTime": XSD.dateTime,
    }

    for cls_entry in classes:
        cls_name = cls_entry.get("name", "")
        if not cls_name:
            continue

        cls_uri = DOMAIN_NS_OBJ[cls_name]
        shape_uri = SHAPES_NS[f"SemDict_{cls_name}Shape"]
        shapes.add((shape_uri, RDF.type, SH.NodeShape))
        shapes.add((shape_uri, SH.targetClass, cls_uri))
        shapes.add((shape_uri, RDFS.label,
                     Literal(f"Auto-SHACL shape for {cls_name} (from semantic dictionary)", lang="en")))

        # DatatypeProperty shapes
        for dp in cls_entry.get("data_properties", []):
            dp_name = dp.get("name", "") if isinstance(dp, dict) else str(dp)
            if not dp_name:
                continue
            dp_uri = DOMAIN_NS_OBJ[dp_name]
            constraint = BNode()
            shapes.add((shape_uri, SH.property, constraint))
            shapes.add((constraint, SH.path, dp_uri))
            shapes.add((constraint, SH.minCount, Literal(0)))

            # datatype 매핑
            dp_range = dp.get("range", "") if isinstance(dp, dict) else ""
            if dp_range:
                xsd_type = XSD_MAP.get(dp_range.split("#")[-1].split("/")[-1], None)
                if xsd_type:
                    shapes.add((constraint, SH.datatype, xsd_type))

        # ObjectProperty shapes
        for op in cls_entry.get("object_properties", []):
            op_name = op.get("name", "") if isinstance(op, dict) else str(op)
            if not op_name:
                continue
            op_uri = DOMAIN_NS_OBJ[op_name]
            constraint = BNode()
            shapes.add((shape_uri, SH.property, constraint))
            shapes.add((constraint, SH.path, op_uri))

            op_range = op.get("range", "") if isinstance(op, dict) else ""
            if op_range:
                range_local = op_range.split("#")[-1].split("/")[-1]
                if range_local:
                    shapes.add((constraint, SH["class"], DOMAIN_NS_OBJ[range_local]))

    return shapes


def _build_dynamic_shapes(object_properties: list[dict[str, str]]) -> Graph:
    """object_properties.json 항목들로부터 동적 SHACL shapes를 생성한다.

    각 ObjectProperty에 대해 rdfs:domain과 rdfs:range 값이 올바른지
    검증하는 NodeShape을 생성한다.

    Args:
        object_properties: object_properties.json의 "properties" 리스트.
            각 항목은 {"name", "domain", "range", ...} 딕셔너리.

    Returns:
        동적 shapes가 포함된 rdflib Graph.
    """
    g = _new_graph()
    g.bind("sh", SH)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)
    g.bind(NS_PREFIX, DOMAIN_NS_OBJ)
    g.bind("shapes", SHAPES_NS)

    for prop_def in object_properties:
        prop_name = prop_def.get("name")
        expected_domain = prop_def.get("domain")
        expected_range = prop_def.get("range")
        if not prop_name or not expected_domain or not expected_range:
            continue

        prop_uri = URIRef(DOMAIN_NS + prop_name)
        domain_uri = URIRef(DOMAIN_NS + expected_domain)
        range_uri = URIRef(DOMAIN_NS + expected_range)

        shape_uri = SHAPES_NS[f"DynShape_{prop_name}"]

        # NodeShape targeting the specific property URI
        g.add((shape_uri, RDF.type, SH.NodeShape))
        g.add((shape_uri, SH.targetNode, prop_uri))
        g.add((shape_uri, RDFS.label,
               Literal(f"Dynamic shape for steel:{prop_name}", lang="en")))

        # domain constraint
        domain_constraint = BNode()
        g.add((shape_uri, SH.property, domain_constraint))
        g.add((domain_constraint, SH.path, RDFS.domain))
        g.add((domain_constraint, SH.hasValue, domain_uri))
        g.add((domain_constraint, SH.minCount, Literal(1)))
        g.add((domain_constraint, SH.message,
               Literal(f"steel:{prop_name} must have rdfs:domain steel:{expected_domain}",
                       lang="en")))

        # range constraint
        range_constraint = BNode()
        g.add((shape_uri, SH.property, range_constraint))
        g.add((range_constraint, SH.path, RDFS.range))
        g.add((range_constraint, SH.hasValue, range_uri))
        g.add((range_constraint, SH.minCount, Literal(1)))
        g.add((range_constraint, SH.message,
               Literal(f"steel:{prop_name} must have rdfs:range steel:{expected_range}",
                       lang="en")))

    return g


def _parse_shacl_violations(results_graph: Graph) -> list[dict[str, str | None]]:
    """SHACL 결과 그래프에서 위반 세부 정보를 추출한다.

    Args:
        results_graph: pyshacl이 반환한 결과 rdflib Graph.

    Returns:
        각 위반을 나타내는 딕셔너리 리스트. 각 항목은:
        - focus_node: 위반 대상 노드 URI
        - path: 위반 프로퍼티 경로
        - message: SHACL 메시지
        - severity: sh:resultSeverity 값
        - source_shape: 위반을 발생시킨 shape URI
        - value: 실제 값 (있는 경우)
        - classification: "deterministic_fixable" 또는 "llm_required"
    """
    from rdflib import BNode

    violations = []

    for result in results_graph.subjects(RDF.type, SH.ValidationResult):
        focus_term = next(results_graph.objects(result, SH.focusNode), None)
        # 익명(blank node) 클래스 표현식 — owl:unionOf / owl:Restriction 등 —
        # 은 rdfs:label/comment 대상이 아니므로 위반에서 제외(2026-06-23).
        # named IRI 노드의 누락만 실제 결함으로 보고한다.
        if isinstance(focus_term, BNode):
            continue
        focus_node = str(focus_term) if focus_term is not None else ""
        path = str(next(results_graph.objects(result, SH.resultPath), ""))
        message = str(next(results_graph.objects(result, SH.resultMessage), ""))
        severity = str(next(results_graph.objects(result, SH.resultSeverity), ""))
        source_shape = str(next(results_graph.objects(result, SH.sourceShape), ""))
        value = str(next(results_graph.objects(result, SH.value), ""))

        # Classify the violation based on message content
        classification = _classify_violation(message, path)

        violations.append({
            "focus_node": focus_node,
            "path": path,
            "message": message,
            "severity": severity.split("#")[-1] if "#" in severity else severity,
            "source_shape": source_shape,
            "value": value if value else None,
            "classification": classification,
        })

    return violations


def _classify_violation(message: str, path: str) -> str:
    """위반 메시지와 경로를 분석하여 수정 가능 분류를 결정한다.

    Returns:
        "deterministic_fixable" 또는 "llm_required"
    """
    msg_lower = message.lower()

    # domain/range mismatch or missing
    if "domain" in msg_lower and ("must have" in msg_lower or "mismatch" in msg_lower):
        return "deterministic_fixable"
    if "range" in msg_lower and ("must have" in msg_lower or "mismatch" in msg_lower):
        return "deterministic_fixable"

    # Missing labels or comments
    if "label" in msg_lower and "must have" in msg_lower:
        return "deterministic_fixable"
    if "comment" in msg_lower and "must have" in msg_lower:
        return "deterministic_fixable"

    # Check path for known deterministic patterns
    path_lower = path.lower()
    if "label" in path_lower or "comment" in path_lower:
        return "deterministic_fixable"
    if "domain" in path_lower or "range" in path_lower:
        return "deterministic_fixable"

    return "llm_required"


def validate_tbox_shacl(ttl_content: str = "", ttl_path: str = "",
                         engine: str = "") -> str:
    """T-Box TTL을 정적 SHACL shapes로 검증한다.

    정적 shapes (rules/policy/tbox_shapes.ttl):
      - owl:Class에 rdfs:label, rdfs:comment 필수
      - owl:DatatypeProperty에 domain, range, label 필수
      - owl:ObjectProperty에 domain, range 필수

    위반 결과를 deterministic_fixable vs llm_required로 분류하여 반환.

    Args:
        ttl_content: 검증할 T-Box Turtle 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
        engine: "pyshacl" (기본) | "pyrudof" (Rust). 빈 값이면 SHACL_ENGINE env.
    """
    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl(ttl_content, ttl_path)
        # Parse the T-Box data
        data_graph = _new_graph()
        data_graph.parse(data=ttl, format="turtle")
    except Exception as e:
        return error_response(f"TTL 파싱 실패: {e}", logger=logger)

    # Load static shapes from rules/policy/tbox_shapes.ttl
    combined_shapes = _new_graph()
    static_shapes_path = Path(rules_path("tbox_shapes.ttl", base=str(RULES_DIR)))
    if static_shapes_path.exists():
        combined_shapes.parse(str(static_shapes_path), format="turtle")
        static_loaded = True
    else:
        static_loaded = False

    # Load dynamic shapes from semantic dictionary (Auto-SHACL)
    sem_dict_shapes = _build_shapes_from_semantic_dict()
    dynamic_shapes_count = sum(
        1 for _ in sem_dict_shapes.subjects(RDF.type, SH.NodeShape)
    )
    if dynamic_shapes_count > 0:
        for triple in sem_dict_shapes:
            combined_shapes.add(triple)

    try:
        conforms, results_graph, results_text = _run_shacl(
            data_graph=data_graph,
            shacl_graph=combined_shapes if len(combined_shapes) > 0 else None,
            engine=engine or None,
            abort_on_first=False,
        )
    except Exception as e:
        return error_response(f"SHACL 검증 실패: {e}", logger=logger)

    # Parse structured violations from the results graph
    violations = _parse_shacl_violations(results_graph)

    # Classify and count
    deterministic_fixable = [v for v in violations if v["classification"] == "deterministic_fixable"]
    llm_required = [v for v in violations if v["classification"] == "llm_required"]

    return json.dumps({
        "success": True,
        "conforms": conforms,
        "violations": violations,
        "counts": {
            "total": len(violations),
            "deterministic_fixable": len(deterministic_fixable),
            "llm_required": len(llm_required),
        },
        "shapes_info": {
            "static_shapes_loaded": static_loaded,
            "static_shapes_path": str(static_shapes_path),
            "dynamic_shapes_count": dynamic_shapes_count,
        },
    }, ensure_ascii=False, indent=2)


def validate_owl_cardinality(merge_tacit_and_inverse: bool = True) -> str:
    """T-Box의 OWL Restriction(cardinality 제약)을 A-Box에 대해 검증한다.

    T-Box에 정의된 owl:minCardinality, owl:maxCardinality, owl:exactCardinality,
    owl:someValuesFrom, owl:hasValue 등을 파싱하여 동적 SHACL shape을 생성,
    A-Box 인스턴스에 대해 Closed World Assumption으로 검증한다.

    OWL은 Open World Assumption이므로 HermiT/Pellet은 이 위반을 잡지 못함.
    이 도구는 CWA 관점에서 "데이터에 실제로 충족되는지"를 확인한다.

    ## 데이터 그래프가 A-Box 파일만이면 역방향 술어를 못 본다 (2026-08-30 규명)

    예전에는 데이터 그래프를 ``TBOX_PATH`` + ``ABOX_PATH`` **파일 두 개**로만
    만들었다. 그러면 두 종류의 트리플이 빠진다:

      · **tacit** (``data/source/tacit/*.ttl``) — 공정 흐름·설비 매핑 등
      · **역방향** — ``load_graph`` 가 ``ensure_inverse_triples()`` 로 만드는 것

    A-Box 생성기는 FK 컬럼이 있는 쪽에서만 술어를 쓰므로, ``owl:inverseOf`` 짝의
    다른 쪽은 **A-Box 파일에 0건**이고 역방향 materialize 후에만 존재한다. 그
    술어에 걸린 카디널리티 제약은 이 도구가 원리적으로 볼 수 없었다.

    실측 — ``EquipmentMaster ⊑ ≤1 hasEquipmentStatus`` (CSV 는 설비 1대당 상태
    120건이므로 전수 위반) 에 대해 **같은 shape** 으로::

        tbox+abox 파일만 : conforms=True   위반 0     ← 예전 동작
        load_graph 병합   : conforms=False  위반 50    ← 실제 결함

    그 술어는 ``a_box.ttl`` 에 0건, 역방향 ``equipmentStatusRefersToEquipment`` 로만
    6,000건 존재한다. docstring 이 "HermiT 이 OWA 라 못 잡으니 이 도구가 CWA 로
    잡는다" 고 약속했는데 그 약속이 바로 이 경우에 깨졌다 (게다가 HermiT 쪽도
    A-Box 를 로드하지 않아 두 게이트가 독립적으로 눈멀어 있었다).

    ## 두 축은 서로를 **양방향으로** 정정한다 (전수 실측)

    병합 축이 단순히 "더 많이 잡는" 것이 아니다. 88 restriction 전수 비교::

        파일 축   : conforms=False  위반 391   (51s)
        병합 축   : conforms=False  위반 245   (69s)

    갈리는 지점이 각각 정당하다:

      · 파일 축에만 있던 50건 (``equipmentUsedInSteelmaking`` 49 + ``followedBy`` 1)
        → **tacit 이 실제로 채운다**. 파일 축은 tacit 을 안 읽어 "값 없음" 으로 봤다.
        이 50건은 **오탐**이었다.
      · 병합 축에만 있는 50건 (``isElectricalEquipmentOf`` 36 +
        ``equipmentUsedInBlastFurnace`` 14) → 역방향 materialize 후에만 관측된다.
        파일 축은 원리적으로 볼 수 없었다.

    즉 예전 축은 오탐 50건을 인쇄하면서 실제 위반 50건을 놓치고 있었다. 기본값을
    병합 축으로 둔 이유가 이것이다 — 다만 두 축 다 의미가 있으므로 스위치를 남긴다
    ("A-Box 파일 자체에 무엇이 있나" 를 보려면 ``False``).

    Args:
        merge_tacit_and_inverse: ``True`` (기본) 면 ``load_graph`` 로 tacit + 역방향을
            병합한 그래프를 검증한다 — 이것이 정확한 축이다. 대신 비싸다 (실측:
            826,109 트리플 69s vs 파일 2개 715,864 트리플 51s). ``False`` 는 예전
            동작 (A-Box 파일만).
            응답의 ``data_graph`` 에 어느 축으로 쟀는지 각인된다 — 이 리포에는
            "통계는 출처 그래프를 밝혀야 한다" 는 기록이 있다.
    """
    import os

    from config import TBOX_PATH

    try:
        if not os.path.exists(TBOX_PATH):
            return error_response("T-Box 파일이 없습니다.", hint="generate_tbox로 먼저 생성하세요.", logger=logger)
        if not os.path.exists(ABOX_PATH):
            return error_response("A-Box 파일이 없습니다.", hint="generate_abox로 먼저 생성하세요.", logger=logger)

        # T-Box 로드
        tbox = _new_graph()
        tbox.parse(TBOX_PATH, format="turtle")

        # 데이터 그래프 — 역방향 술어를 보려면 load_graph 가 필요하다 (docstring).
        data_graph_kind = "tbox+abox_files"
        tacit_files = 0
        if merge_tacit_and_inverse:
            try:
                from domain.tbox_utils import load_graph

                merged, tacit_files = load_graph(use_inferred=False)
                # load_graph 는 캐시 참조를 반환한다 — pyshacl 이 그래프를 건드릴 수
                # 있으므로 복사한다 (그 함수의 WARNING 참조).
                data = _new_graph()
                for triple in merged:
                    data.add(triple)
                data_graph_kind = "load_graph(tacit+inverse)"
            except Exception as exc:  # noqa: BLE001 — 병합 실패 시 파일 축으로 폴백
                logger.warning(
                    "load_graph 병합 실패, A-Box 파일 축으로 폴백 (역방향 술어는 "
                    "보이지 않는다): %s", exc,
                )
                data = _new_graph()
                data.parse(TBOX_PATH, format="turtle")
                data.parse(ABOX_PATH, format="turtle")
        else:
            data = _new_graph()
            data.parse(TBOX_PATH, format="turtle")
            data.parse(ABOX_PATH, format="turtle")

        steel_ns = DOMAIN_NS

        # OWL Restriction 파싱
        restrictions = []
        for restr_node in tbox.subjects(RDF.type, OWL.Restriction):
            on_prop = tbox.value(restr_node, OWL.onProperty)
            if not on_prop or not str(on_prop).startswith(steel_ns):
                continue

            # 이 restriction이 어떤 클래스의 subClassOf인지 찾기
            target_classes = []
            for cls in tbox.subjects(RDFS.subClassOf, restr_node):
                if isinstance(cls, URIRef) and str(cls).startswith(steel_ns):
                    target_classes.append(cls)

            if not target_classes:
                continue

            r = {"property": on_prop, "classes": target_classes}

            # Cardinality 종류 파싱
            min_card = tbox.value(restr_node, OWL.minCardinality) or tbox.value(restr_node, OWL.minQualifiedCardinality)
            max_card = tbox.value(restr_node, OWL.maxCardinality) or tbox.value(restr_node, OWL.maxQualifiedCardinality)
            exact_card = tbox.value(restr_node, OWL.cardinality) or tbox.value(restr_node, OWL.qualifiedCardinality)
            some_values = tbox.value(restr_node, OWL.someValuesFrom)
            has_value = tbox.value(restr_node, OWL.hasValue)

            if min_card is not None:
                r["min_cardinality"] = int(min_card)
            if max_card is not None:
                r["max_cardinality"] = int(max_card)
            if exact_card is not None:
                r["exact_cardinality"] = int(exact_card)
            if some_values is not None:
                r["some_values_from"] = some_values
            if has_value is not None:
                r["has_value"] = has_value

            restrictions.append(r)

        if not restrictions:
            return json.dumps({
                "success": True,
                "restrictions_found": 0,
                "message": "T-Box에 OWL Restriction(cardinality 제약)이 없습니다. 검증 대상 없음.",
                "violations": [],
                # 조기 반환에도 출처를 남긴다 — 키 부재를 "정상" 으로 오독하는 함정.
                "data_graph": data_graph_kind,
                "data_graph_triples": len(data),
                "tacit_files_merged": tacit_files,
            }, ensure_ascii=False, indent=2)

        # 동적 SHACL shape 생성
        shapes = _new_graph()
        shapes.bind("sh", SH)
        shapes.bind(NS_PREFIX, DOMAIN_NS_OBJ)

        shape_count = 0
        for r in restrictions:
            prop_uri = r["property"]
            prop_name = str(prop_uri).split("#")[-1]

            for cls in r["classes"]:
                cls_name = str(cls).split("#")[-1]
                shape_uri = SHAPES_NS[f"Card_{cls_name}_{prop_name}"]
                shapes.add((shape_uri, RDF.type, SH.NodeShape))
                shapes.add((shape_uri, SH.targetClass, cls))

                constraint = BNode()
                shapes.add((shape_uri, SH.property, constraint))
                shapes.add((constraint, SH.path, prop_uri))
                shapes.add((constraint, SH.severity, SH.Warning))

                if "min_cardinality" in r:
                    shapes.add((constraint, SH.minCount, Literal(r["min_cardinality"])))
                if "max_cardinality" in r:
                    shapes.add((constraint, SH.maxCount, Literal(r["max_cardinality"])))
                if "exact_cardinality" in r:
                    shapes.add((constraint, SH.minCount, Literal(r["exact_cardinality"])))
                    shapes.add((constraint, SH.maxCount, Literal(r["exact_cardinality"])))
                if "some_values_from" in r and isinstance(r["some_values_from"], URIRef):
                    shapes.add((constraint, SH.minCount, Literal(1)))
                    shapes.add((constraint, SH["class"], r["some_values_from"]))

                shapes.add((constraint, SH.message,
                            Literal(f"{cls_name}.{prop_name} cardinality 위반", lang="ko")))
                shape_count += 1

        # SHACL 검증 실행 (pyshacl | pyrudof via SHACL_ENGINE)
        conforms, results_graph, results_text = _run_shacl(
            data_graph=data,
            shacl_graph=shapes,
            abort_on_first=False,
        )

        violations = _parse_shacl_violations(results_graph)

        return json.dumps({
            "success": True,
            "restrictions_found": len(restrictions),
            "shapes_generated": shape_count,
            "conforms": conforms,
            "violations_count": len(violations),
            "violations": violations[:50],
            # 어느 그래프를 쟀는가 — 이 값이 없으면 "위반 0" 이 "결함 없음" 인지
            # "역방향 술어가 계측 밖" 인지 구분되지 않는다 (docstring 의 실측 참조).
            "data_graph": data_graph_kind,
            "data_graph_triples": len(data),
            "tacit_files_merged": tacit_files,
            "restriction_summary": [
                {
                    "property": str(r["property"]).split("#")[-1],
                    "classes": [str(c).split("#")[-1] for c in r["classes"]],
                    **{k: v for k, v in r.items() if k not in ("property", "classes")
                       and not isinstance(v, URIRef | BNode)},
                }
                for r in restrictions
            ],
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


# ---------------------------------------------------------------------------
# CWA/OWA Bridge — completenessStatus 기반 카디널리티 위반 심각도 분기
# ---------------------------------------------------------------------------



def _check_cardinality_with_completeness(
    cls_uri: str,
    completeness: str,
    restriction_type: str,
    expected: int,
    actual: int,
) -> dict | None:
    """completenessStatus에 따라 카디널리티 위반을 평가한다.

    OWL은 Open World Assumption이므로 minCardinality 미충족이 곧 오류가 아니다.
    그러나 CSV 기반 데이터(closed)는 Closed World이므로 미충족 = 실제 오류.

    Args:
        cls_uri: 검증 대상 클래스 URI 문자열.
        completeness: "closed" | "open" | "inferred".
        restriction_type: "minCardinality" | "maxCardinality" | "exactCardinality".
        expected: 제약에 명시된 기대값.
        actual: 인스턴스에서 관찰된 실제값.

    Returns:
        위반 딕셔너리 (severity 포함) 또는 None (위반 없음/스킵).
    """
    if completeness == "inferred":
        return None  # 추론 전용 클래스는 검증 스킵

    # restriction_type 은 세 값 중 하나뿐이라 or-체인은 원래 if/elif 와 동등하다.
    # (ruff SIM114 가 if/elif 를 한 줄로 접어 150자를 만들었으므로 명시적으로 쓴다.)
    violated = (
        (restriction_type == "minCardinality" and actual < expected)
        or (restriction_type == "maxCardinality" and actual > expected)
        or (restriction_type == "exactCardinality" and actual != expected)
    )

    if not violated:
        return None

    severity = "error" if completeness == "closed" else "info"
    return {
        "class": cls_uri,
        "completeness": completeness,
        "restriction": restriction_type,
        "expected": expected,
        "actual": actual,
        "severity": severity,
    }
