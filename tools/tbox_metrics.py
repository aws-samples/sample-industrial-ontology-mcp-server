"""tbox_metrics.py — Metrics, Regression, Baseline, Antipatterns.

T-Box 정량적 메트릭 측정, 베이스라인 비교, 회귀 감지, 안티패턴 검출.
"""
from __future__ import annotations

import json
import logging
import os

import rdflib.collection
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from config import GENERATED_TBOX_DIR, TBOX_BASELINE_PATH, TBOX_PATH
from domain.namespaces import DOMAIN_NS
from domain.rules_paths import rules_path
from domain.tbox_utils import _new_graph
from tools.common import error_response, resolve_child_path
from tools.validation_core import _load_ttl

logger = logging.getLogger(__name__)


def smooth_score(
    value: float,
    ideal_min: float,
    ideal_max: float,
    max_points: float,
    falloff: float = 2.0,
) -> float:
    """범위 내면 ``max_points``, 범위 밖이면 거리에 비례해 감소.

    ``measure_tbox_metrics`` 의 종합 점수 계산 핵심. 예전엔 그 함수 **안의
    클로저** 라 import 이 불가능했고, ``tests/test_metric_calibration.py`` 가
    본문을 복제해 테스트했다 ("테스트용으로 복제" 주석까지 달려 있었다). 사본을
    테스트하면 **실제 구현 변경을 절대 감지하지 못한다** — 커버리지 연극이다
    (2026-08-08 규명). 모듈 레벨로 빼서 실제 함수를 테스트하게 만들었다.

    Args:
        value: 측정값.
        ideal_min: 이상 범위 하한.
        ideal_max: 이상 범위 상한.
        max_points: 최대 배점.
        falloff: 감소 속도 (높을수록 빠르게 감소).

    Returns:
        0 이상 ``max_points`` 이하의 점수 (소수 1자리).
    """
    if ideal_min <= value <= ideal_max:
        return max_points
    if value < ideal_min:
        distance = (ideal_min - value) / max(ideal_min, 0.001)
    else:
        distance = (value - ideal_max) / max(ideal_max, 0.001)
    return round(max(max_points * max(1 - distance * falloff, 0), 0), 1)


def analyze_tbox() -> str:
    """S3에서 현재 T-Box를 읽어 클래스, ObjectProperty, DatatypeProperty를 분석한다."""
    from tools.local_artifacts import read_tbox

    ttl = read_tbox()
    g = _new_graph()
    g.parse(data=ttl, format="turtle")

    steel_ns = DOMAIN_NS

    classes = []
    for cls in g.subjects(RDF.type, OWL.Class):
        if not str(cls).startswith(steel_ns):
            continue
        labels = {
            label.language: str(label)
            for label in g.objects(cls, RDFS.label)
            if hasattr(label, "language")
        }
        parents = [str(p) for p in g.objects(cls, RDFS.subClassOf)]
        classes.append({"uri": str(cls), "labels": labels, "parents": parents})

    obj_props = []
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        if not str(prop).startswith(steel_ns):
            continue
        domains = [str(d) for d in g.objects(prop, RDFS.domain)]
        ranges = [str(r) for r in g.objects(prop, RDFS.range)]
        inverses = [str(i) for i in g.objects(prop, OWL.inverseOf)]
        obj_props.append({
            "uri": str(prop),
            "domain": domains,
            "range": ranges,
            "inverse": inverses,
        })

    data_props = []
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(prop).startswith(steel_ns):
            continue
        domains = [str(d) for d in g.objects(prop, RDFS.domain)]
        ranges = [str(r) for r in g.objects(prop, RDFS.range)]
        data_props.append({"uri": str(prop), "domain": domains, "range": ranges})

    return json.dumps({
        "success": True,
        "classes_count": len(classes),
        "classes": classes,
        "object_properties_count": len(obj_props),
        "object_properties": obj_props,
        "data_properties_count": len(data_props),
        "data_properties": data_props,
    }, ensure_ascii=False, indent=2)


def measure_tbox_metrics() -> str:
    """T-Box의 정량적 품질 메트릭스를 측정하여 종합 점수를 반환한다.

    메트릭:
    - DIT (Depth of Inheritance Tree): 가장 깊은 subClassOf 체인. 정상 2~5.
    - NOC (Number of Children): 클래스당 직접 자식 수. 정상 2~10.
    - RR (Relationship Richness): ObjectProperty / 전체 프로퍼티 비율. 정상 0.3~0.6.
    - AR (Attribute Richness): 클래스당 평균 DatatypeProperty 수. 정상 3~15.
    - Annotation Completeness: label+comment 모두 있는 엔티티 비율.
    - 종합 점수 0~100.
    """
    from collections import defaultdict

    from tools.local_artifacts import read_tbox

    ttl = read_tbox()
    g = _new_graph()
    g.parse(data=ttl, format="turtle")

    steel_ns = DOMAIN_NS

    # 클래스 수집
    cls_set = set()
    for cls in g.subjects(RDF.type, OWL.Class):
        if isinstance(cls, URIRef) and str(cls).startswith(steel_ns):
            cls_set.add(cls)

    # 부모-자식 관계
    children_map: dict[str, list[str]] = defaultdict(list)  # parent → children
    parent_map: dict[str, list[str]] = defaultdict(list)    # child → parents
    for cls in cls_set:
        for parent in g.objects(cls, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent in cls_set:
                children_map[str(parent)].append(str(cls))
                parent_map[str(cls)].append(str(parent))

    # DIT: 최대 깊이 (루트에서 리프까지)
    roots = [c for c in cls_set if str(c) not in parent_map]

    def _depth(node_uri: str, visited: set | None = None) -> int:
        if visited is None:
            visited = set()
        if node_uri in visited:
            return 0  # 순환 방지
        visited.add(node_uri)
        ch = children_map.get(node_uri, [])
        if not ch:
            return 0
        return 1 + max(_depth(c, visited.copy()) for c in ch)

    dit = max((_depth(str(r)) for r in roots), default=0) if roots else 0

    # NOC: 클래스당 직접 자식 수 통계
    noc_values = [len(children_map.get(str(c), [])) for c in cls_set]
    noc_avg = round(sum(noc_values) / max(len(noc_values), 1), 1)
    noc_max = max(noc_values, default=0)

    # 프로퍼티 수집
    obj_props = {p for p in g.subjects(RDF.type, OWL.ObjectProperty)
                 if isinstance(p, URIRef) and str(p).startswith(steel_ns)}
    data_props = {p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
                  if isinstance(p, URIRef) and str(p).startswith(steel_ns)}
    total_props = len(obj_props) + len(data_props)

    # RR: 관계 풍부도
    rr = round(len(obj_props) / max(total_props, 1), 2)

    # AR: 클래스당 평균 DatatypeProperty 수 (추상/중간 클래스 제외)
    # 추상 클래스(DP 0개이면서 자식만 가진 클래스)는 AR에서 제외하여 공정한 측정
    dp_per_class: dict[str, int] = defaultdict(int)
    for dp in data_props:
        for domain in g.objects(dp, RDFS.domain):
            if isinstance(domain, URIRef) and domain in cls_set:
                dp_per_class[str(domain)] += 1
    # DP가 1개 이상인 클래스(데이터 클래스)만 AR 계산에 포함
    data_classes = [c for c in cls_set if dp_per_class.get(str(c), 0) > 0]
    ar_values = [dp_per_class.get(str(c), 0) for c in data_classes] if data_classes else [0]
    ar = round(sum(ar_values) / max(len(ar_values), 1), 1)

    # 어노테이션 완전성
    all_entities = cls_set | obj_props | data_props
    annotated = 0
    for entity in all_entities:
        has_label = any(True for _ in g.objects(entity, RDFS.label))
        has_comment = any(True for _ in g.objects(entity, RDFS.comment))
        if has_label and has_comment:
            annotated += 1
    annotation_pct = round(annotated / max(len(all_entities), 1) * 100, 1)

    # Axiom 풍부도: 클래스당 평균 axiom 수 (subClassOf + restriction + disjoint + equivalentClass)
    axiom_count = 0
    for cls in cls_set:
        axiom_count += sum(1 for _ in g.objects(cls, RDFS.subClassOf))
        axiom_count += sum(1 for _ in g.objects(cls, OWL.equivalentClass))
        axiom_count += sum(1 for _ in g.objects(cls, OWL.disjointWith))
    axiom_richness = round(axiom_count / max(len(cls_set), 1), 1)

    # IR: Inheritance Richness - ratio of classes with subclasses
    total_classes = len(cls_set)
    classes_with_children = sum(1 for c in children_map if children_map[c])
    ir = round(classes_with_children / max(total_classes, 1), 3)

    # CC: Class Connectivity - ratio of classes connected via OP
    connected: set[str] = set()
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        for d in g.objects(prop, RDFS.domain):
            connected.add(str(d))
        for r in g.objects(prop, RDFS.range):
            connected.add(str(r))
    cc = round(len([c for c in connected if DOMAIN_NS in c]) / max(total_classes, 1), 3)

    # TAN: Tangledness — percentage of classes with 2+ parents (multiple inheritance)
    multi_parent_count = sum(1 for c in cls_set if len(parent_map.get(str(c), [])) >= 2)
    tan = round(multi_parent_count / max(total_classes, 1) * 100, 1)

    # COU: Cross-domain Coupling — OPs connecting different domain groups / total OPs
    # Domain groups are determined by top-level classes (classes with no steel parents)
    def _get_root_ancestor(cls_uri: str, visited: set | None = None) -> str:
        """Return the top-level steel ancestor of a class, or itself if root."""
        if visited is None:
            visited = set()
        if cls_uri in visited:
            return cls_uri
        visited.add(cls_uri)
        parents = parent_map.get(cls_uri, [])
        if not parents:
            return cls_uri
        return _get_root_ancestor(parents[0], visited)

    cross_domain_ops = 0
    steel_op_count = len(obj_props)
    for prop in obj_props:
        prop_domains = [str(d) for d in g.objects(prop, RDFS.domain)
                        if isinstance(d, URIRef) and str(d).startswith(steel_ns)]
        prop_ranges = [str(r) for r in g.objects(prop, RDFS.range)
                       if isinstance(r, URIRef) and str(r).startswith(steel_ns)]
        if prop_domains and prop_ranges:
            domain_root = _get_root_ancestor(prop_domains[0])
            range_root = _get_root_ancestor(prop_ranges[0])
            if domain_root != range_root:
                cross_domain_ops += 1
    cou = round(cross_domain_ops / max(steel_op_count, 1), 2)

    # 메트릭 목표값 로드 (design_patterns.json)
    _dp_path = rules_path("design_patterns.json")
    _mt: dict = {}
    try:
        with open(_dp_path, encoding="utf-8") as f:
            _mt = json.load(f).get("metric_targets", {})
    except Exception as exc:
        logger.warning(
            "metric_targets 로드 실패; 기본 T-Box 메트릭 범위를 사용한다: %s",
            exc,
        )
    dit_range = (_mt.get("dit", {}).get("min", 2), _mt.get("dit", {}).get("max", 5))
    rr_range = (_mt.get("rr", {}).get("min", 0.3), _mt.get("rr", {}).get("max", 0.6))
    ar_range = (_mt.get("ar", {}).get("min", 3), _mt.get("ar", {}).get("max", 15))
    axiom_target = _mt.get("axiom", {}).get("min", 2)

    # #15: Score formula 파라미터 로드 (rules/policy/quality_thresholds.json)
    _qt_path = rules_path("quality_thresholds.json")
    _sf: dict = {}
    try:
        with open(_qt_path, encoding="utf-8") as f:
            _sf = json.load(f).get("score_formula", {})
    except Exception as exc:
        logger.warning(
            "score_formula 로드 실패; 기본 T-Box 점수 가중치를 사용한다: %s",
            exc,
        )
    _W_DIT = _sf.get("dit_weight", 20)
    _W_NOC = _sf.get("noc_weight", 15)
    _W_RR = _sf.get("rr_weight", 15)
    _W_AR = _sf.get("ar_weight", 15)
    _W_ANN = _sf.get("annotation_weight", 20)
    _W_AX = _sf.get("axiom_weight", 15)
    _FALLOFF = _sf.get("falloff", 2.0)
    _CUTOFF = _sf.get("grade_cutoffs", {"A": 85, "B": 70, "C": 55})

    # 종합 점수 (0~100) — smooth gradient scoring.
    # 설정에서 읽은 falloff 를 기본값으로 바인딩한 얇은 래퍼. 실제 계산은 모듈
    # 레벨 ``smooth_score`` 다 (테스트가 import 할 수 있게 밖으로 뺐다).
    def _smooth_score(value, ideal_min, ideal_max, max_points, falloff=_FALLOFF):
        return smooth_score(value, ideal_min, ideal_max, max_points, falloff)

    scores = {
        "dit": _smooth_score(dit, dit_range[0], dit_range[1], _W_DIT),
        "noc_avg": _smooth_score(noc_avg, 2, 10, _W_NOC),
        "rr": _smooth_score(rr, rr_range[0], rr_range[1], _W_RR),
        "ar": _smooth_score(ar, ar_range[0], ar_range[1], _W_AR),
        "annotation": round(annotation_pct / 100 * _W_ANN, 1),  # already continuous
        "axiom": round(min(axiom_richness / max(axiom_target, 1), 1.0) * _W_AX, 1),
    }
    total_score = round(sum(scores.values()), 1)

    return json.dumps({
        "success": True,
        "metrics": {
            "dit": {"value": dit, "range": f"{dit_range[0]}~{dit_range[1]}", "status": "GOOD" if dit_range[0] <= dit <= dit_range[1] else "WARNING"},
            "noc_avg": {"value": noc_avg, "max": noc_max, "range": "2~10", "status": "GOOD" if 2 <= noc_avg <= 10 else "WARNING"},
            "rr": {"value": rr, "op": len(obj_props), "dp": len(data_props), "range": f"{rr_range[0]}~{rr_range[1]}", "status": "GOOD" if rr_range[0] - 0.05 <= rr <= rr_range[1] + 0.05 else "WARNING"},
            "ar": {"value": ar, "range": f"{ar_range[0]}~{ar_range[1]}", "status": "GOOD" if ar_range[0] <= ar <= ar_range[1] else "WARNING"},
            "annotation_completeness": {"value": annotation_pct, "annotated": annotated, "total": len(all_entities), "status": "GOOD" if annotation_pct >= 90 else "WARNING"},
            "axiom_richness": {"value": axiom_richness, "total_axioms": axiom_count, "status": "GOOD" if axiom_richness >= axiom_target else "WARNING"},
            "ir": {"value": ir, "description": "Inheritance Richness — ratio of classes with subclasses"},
            "cc": {"value": cc, "description": "Class Connectivity — ratio of classes connected via OP"},
            "tan": {"value": tan, "multi_parent_classes": multi_parent_count, "range": "<20%", "status": "GOOD" if tan < 20 else "WARNING"},
            "cou": {"value": cou, "cross_domain_ops": cross_domain_ops, "total_ops": steel_op_count, "description": "Cross-domain Coupling — OPs connecting different domain groups / total OPs"},
        },
        "summary": {
            "classes": total_classes,
            "object_properties": len(obj_props),
            "data_properties": len(data_props),
            "total_score": total_score,
            "max_score": 100,
            "grade": (
                "A" if total_score >= _CUTOFF.get("A", 85)
                else "B" if total_score >= _CUTOFF.get("B", 70)
                else "C" if total_score >= _CUTOFF.get("C", 55)
                else "D"
            ),
        },
        "score_breakdown": scores,
        "quality_gates": {
            "dit": {"passed": dit_range[0] <= dit <= dit_range[1], "target": f"{dit_range[0]}~{dit_range[1]}", "actual": dit},
            "rr": {"passed": rr_range[0] - 0.05 <= rr <= rr_range[1] + 0.05, "target": f"{rr_range[0]}~{rr_range[1]}", "actual": rr},
            "ar": {"passed": ar_range[0] <= ar <= ar_range[1], "target": f"{ar_range[0]}~{ar_range[1]}", "actual": ar},
            "axiom": {"passed": axiom_richness >= axiom_target, "target": f">={axiom_target}", "actual": axiom_richness},
            "all_passed": (
                dit_range[0] <= dit <= dit_range[1]
                and rr_range[0] - 0.05 <= rr <= rr_range[1] + 0.05
                and ar_range[0] <= ar <= ar_range[1]
                and axiom_richness >= axiom_target
            ),
        },
        "regression": _detect_regression(g, cls_set, obj_props, data_props, dit, rr, ar, axiom_richness),
    }, ensure_ascii=False, indent=2)


def _detect_regression(
    current_g: Graph,
    current_cls: set,
    current_ops: set,
    current_dps: set,
    current_dit: int,
    current_rr: float,
    current_ar: float,
    current_axiom: float,
) -> dict | None:
    """베이스라인 대비 메트릭 회귀를 감지한다.

    TBOX_BASELINE_PATH가 존재하면 베이스라인의 DIT/RR/AR/Axiom을 계산하고
    현재 값과 비교하여 감소한 메트릭을 regression으로 플래그한다.

    Returns:
        회귀 정보 dict 또는 베이스라인 없으면 None.
    """
    from collections import defaultdict

    if not os.path.exists(TBOX_BASELINE_PATH):
        return None

    try:
        bg = _new_graph()
        bg.parse(TBOX_BASELINE_PATH, format="turtle")
    except Exception:
        return None

    steel_ns = DOMAIN_NS

    # Baseline classes
    b_cls = {c for c in bg.subjects(RDF.type, OWL.Class)
             if isinstance(c, URIRef) and str(c).startswith(steel_ns)}

    # Baseline parent/children
    b_children: dict[str, list[str]] = defaultdict(list)
    b_parents: dict[str, list[str]] = defaultdict(list)
    for cls in b_cls:
        for parent in bg.objects(cls, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent in b_cls:
                b_children[str(parent)].append(str(cls))
                b_parents[str(cls)].append(str(parent))

    b_roots = [c for c in b_cls if str(c) not in b_parents]

    def _b_depth(node_uri: str, visited: set | None = None) -> int:
        if visited is None:
            visited = set()
        if node_uri in visited:
            return 0
        visited.add(node_uri)
        ch = b_children.get(node_uri, [])
        if not ch:
            return 0
        return 1 + max(_b_depth(c, visited.copy()) for c in ch)

    b_dit = max((_b_depth(str(r)) for r in b_roots), default=0) if b_roots else 0

    # Baseline properties
    b_ops = {p for p in bg.subjects(RDF.type, OWL.ObjectProperty)
             if isinstance(p, URIRef) and str(p).startswith(steel_ns)}
    b_dps = {p for p in bg.subjects(RDF.type, OWL.DatatypeProperty)
             if isinstance(p, URIRef) and str(p).startswith(steel_ns)}
    b_total_props = len(b_ops) + len(b_dps)

    b_rr = round(len(b_ops) / max(b_total_props, 1), 2)

    # Baseline AR (same logic: exclude abstract classes)
    b_dp_per_class: dict[str, int] = defaultdict(int)
    for dp in b_dps:
        for domain in bg.objects(dp, RDFS.domain):
            if isinstance(domain, URIRef) and domain in b_cls:
                b_dp_per_class[str(domain)] += 1
    b_data_classes = [c for c in b_cls if b_dp_per_class.get(str(c), 0) > 0]
    b_ar_values = [b_dp_per_class.get(str(c), 0) for c in b_data_classes] if b_data_classes else [0]
    b_ar = round(sum(b_ar_values) / max(len(b_ar_values), 1), 1)

    # Baseline axiom richness
    b_axiom_count = 0
    for cls in b_cls:
        b_axiom_count += sum(1 for _ in bg.objects(cls, RDFS.subClassOf))
        b_axiom_count += sum(1 for _ in bg.objects(cls, OWL.equivalentClass))
        b_axiom_count += sum(1 for _ in bg.objects(cls, OWL.disjointWith))
    b_axiom = round(b_axiom_count / max(len(b_cls), 1), 1)

    # Compare and flag regressions (metric DECREASED)
    comparisons = {
        "dit": {"baseline": b_dit, "current": current_dit},
        "rr": {"baseline": b_rr, "current": current_rr},
        "ar": {"baseline": b_ar, "current": current_ar},
        "axiom_richness": {"baseline": b_axiom, "current": current_axiom},
    }

    regressions = []
    for metric_name, vals in comparisons.items():
        if vals["current"] < vals["baseline"]:
            regressions.append({
                "metric": metric_name,
                "baseline": vals["baseline"],
                "current": vals["current"],
                "delta": round(vals["current"] - vals["baseline"], 2),
            })

    return {
        "baseline_exists": True,
        "baseline_path": TBOX_BASELINE_PATH,
        "comparisons": comparisons,
        "regressions": regressions,
        "has_regression": len(regressions) > 0,
    }


#: 세대 간 비교에서 세는 공리 종류. 도메인-중립 어휘만 쓴다 (OWL/RDFS).
#:
#: 이름 축(클래스/OP/DP 이름 + domain/range)만 비교하면 **공리 제거가 보이지 않는다**.
#: 실측 2026-09-05: weakening mutant 4종을 배포 T-Box 에 적용했을 때 이름 축 변화가
#: 전부 **0** 이었다::
#:
#:     delete_inverse     (-1 트리플)  이름축 변화 없음
#:     toggle_functional  (-1)         없음
#:     delete_disjoint    (-4)         없음
#:     remove_parent      (-3)         없음
#:
#: 그 넷은 S4.5/S9.5 감사에서도 미검출로 남아 있고 사유가 ``weakening_only`` 다 —
#: 제약이 사라지면 **위반** 검사로는 볼 수 없다. 세대 간 인벤토리 비교가 그 축을
#: 보는 유일한 방법이다.
_AXIOM_KINDS: tuple[tuple[str, URIRef, str], ...] = (
    ("subClassOf", RDFS.subClassOf, "predicate"),
    ("subPropertyOf", RDFS.subPropertyOf, "predicate"),
    ("inverseOf", OWL.inverseOf, "predicate"),
    ("equivalentClass", OWL.equivalentClass, "predicate"),
    ("disjointWith", OWL.disjointWith, "predicate"),
    ("someValuesFrom", OWL.someValuesFrom, "predicate"),
    ("allValuesFrom", OWL.allValuesFrom, "predicate"),
    ("hasValue", OWL.hasValue, "predicate"),
    ("maxCardinality", OWL.maxCardinality, "predicate"),
    ("minCardinality", OWL.minCardinality, "predicate"),
    ("cardinality", OWL.cardinality, "predicate"),
    ("hasKey", OWL.hasKey, "predicate"),
    ("propertyChainAxiom", OWL.propertyChainAxiom, "predicate"),
    ("disjointUnionOf", OWL.disjointUnionOf, "predicate"),
    ("AllDisjointClasses", OWL.AllDisjointClasses, "type"),
    ("FunctionalProperty", OWL.FunctionalProperty, "type"),
    ("InverseFunctionalProperty", OWL.InverseFunctionalProperty, "type"),
    ("TransitiveProperty", OWL.TransitiveProperty, "type"),
    ("SymmetricProperty", OWL.SymmetricProperty, "type"),
    ("Restriction", OWL.Restriction, "type"),
)


def axiom_inventory(g: Graph) -> dict[str, int]:
    """공리 종류별 개수. 세대 간 비교의 기준이 된다.

    ``predicate`` 종류는 그 술어를 쓰는 트리플 수, ``type`` 종류는
    ``rdf:type <kind>`` 인 주어 수를 센다. 주어/목적어의 네임스페이스로 걸러내지
    않는다 — 외래 온톨로지에서 상속된 공리도 이 T-Box 가 잃으면 회귀이기 때문이다.
    """
    out: dict[str, int] = {}
    for label, term, mode in _AXIOM_KINDS:
        if mode == "predicate":
            out[label] = sum(1 for _ in g.triples((None, term, None)))
        else:
            out[label] = sum(1 for _ in g.subjects(RDF.type, term))
    return out


def compare_tbox_baseline() -> str:
    """현재 T-Box를 베이스라인과 비교하여 변경 사항을 보고한다.

    클래스/ObjectProperty/DatatypeProperty의 추가/삭제/변경 수를 측정하여
    T-Box 재현성과 안정성을 확인한다.

    예상 소요시간: ~3초
    """
    try:
        if not os.path.exists(TBOX_BASELINE_PATH):
            return error_response(
                "베이스라인이 없습니다.",
                hint="첫 번째 파이프라인 실행 후 자동 생성됩니다.",
            )
        if not os.path.exists(TBOX_PATH):
            return error_response("현재 T-Box가 없습니다.")

        baseline_g = _new_graph()
        baseline_g.parse(TBOX_BASELINE_PATH, format="turtle")
        current_g = _new_graph()
        current_g.parse(TBOX_PATH, format="turtle")

        steel_ns = DOMAIN_NS

        def _extract_classes(g: Graph) -> set[str]:
            return {
                str(c).split("#")[-1]
                for c in g.subjects(RDF.type, OWL.Class)
                if str(c).startswith(steel_ns)
            }

        def _extract_props(g: Graph, prop_type: URIRef) -> dict[str, dict[str, str | None]]:
            props: dict[str, dict[str, str | None]] = {}
            for p in g.subjects(RDF.type, prop_type):
                if not str(p).startswith(steel_ns):
                    continue
                name = str(p).split("#")[-1]
                domains = [
                    str(d).split("#")[-1]
                    for d in g.objects(p, RDFS.domain)
                    if str(d).startswith(steel_ns)
                ]
                ranges = [str(r).split("#")[-1] for r in g.objects(p, RDFS.range)]
                props[name] = {
                    "domain": domains[0] if domains else None,
                    "range": str(ranges[0]).split("#")[-1] if ranges else None,
                }
            return props

        b_classes = _extract_classes(baseline_g)
        c_classes = _extract_classes(current_g)

        b_ops = _extract_props(baseline_g, OWL.ObjectProperty)
        c_ops = _extract_props(current_g, OWL.ObjectProperty)

        b_dps = _extract_props(baseline_g, OWL.DatatypeProperty)
        c_dps = _extract_props(current_g, OWL.DatatypeProperty)

        # Diff
        added_classes = sorted(c_classes - b_classes)
        removed_classes = sorted(b_classes - c_classes)

        added_ops = sorted(set(c_ops) - set(b_ops))
        removed_ops = sorted(set(b_ops) - set(c_ops))
        changed_ops = []
        for name in sorted(set(b_ops) & set(c_ops)):
            if b_ops[name] != c_ops[name]:
                changed_ops.append({"name": name, "baseline": b_ops[name], "current": c_ops[name]})

        added_dps = sorted(set(c_dps) - set(b_dps))
        removed_dps = sorted(set(b_dps) - set(c_dps))

        # 공리 인벤토리 — 이름 축이 못 보는 **제거** 를 잡는 축 (``_AXIOM_KINDS``).
        b_axioms = axiom_inventory(baseline_g)
        c_axioms = axiom_inventory(current_g)
        axiom_delta = {
            kind: c_axioms[kind] - b_axioms[kind]
            for kind in b_axioms
            if c_axioms[kind] != b_axioms[kind]
        }
        # 감소만 따로 낸다. 증가는 표현력이 늘어난 것이라 다른 성격이다.
        weakened = {k: v for k, v in axiom_delta.items() if v < 0}
        if weakened:
            logger.warning(
                "베이스라인 대비 공리가 줄었다 %s — 이름 축(클래스/OP/DP)에는 보이지 "
                "않는 손실이다. 제약이 사라지면 위반 검사로는 볼 수 없으므로 "
                "(mutation 감사의 weakening_only) 이 축이 유일한 탐지 경로다",
                weakened,
            )

        # Stability score
        total_baseline = len(b_classes) + len(b_ops) + len(b_dps)
        unchanged = (
            total_baseline
            - len(removed_classes)
            - len(removed_ops)
            - len(changed_ops)
            - len(removed_dps)
        )
        stability = round(unchanged / max(total_baseline, 1) * 100, 1)

        return json.dumps(
            {
                "success": True,
                "baseline_path": TBOX_BASELINE_PATH,
                "current_path": TBOX_PATH,
                "stability_score": stability,
                "summary": {
                    "classes": {
                        "baseline": len(b_classes),
                        "current": len(c_classes),
                        "added": len(added_classes),
                        "removed": len(removed_classes),
                    },
                    "object_properties": {
                        "baseline": len(b_ops),
                        "current": len(c_ops),
                        "added": len(added_ops),
                        "removed": len(removed_ops),
                        "changed": len(changed_ops),
                    },
                    "data_properties": {
                        "baseline": len(b_dps),
                        "current": len(c_dps),
                        "added": len(added_dps),
                        "removed": len(removed_dps),
                    },
                },
                # 이름 축과 **독립된** 축이다. 실측 2026-09-05: weakening mutant 4종의
                # 이름 축 변화가 전부 0 이었다 (``_AXIOM_KINDS`` docstring).
                "axiom_inventory": {
                    "baseline": b_axioms,
                    "current": c_axioms,
                    "delta": axiom_delta,
                    "weakened": weakened,
                    "weakened_count": len(weakened),
                },
                "details": {
                    "added_classes": added_classes[:20],
                    "removed_classes": removed_classes[:20],
                    "added_ops": added_ops[:20],
                    "removed_ops": removed_ops[:20],
                    "changed_ops": changed_ops[:20],
                    "added_dps": added_dps[:20],
                    "removed_dps": removed_dps[:20],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return error_response(e, logger=logger)


def detect_tbox_antipatterns(ttl_content: str = "", ttl_path: str = "") -> str:
    """T-Box에서 안티패턴을 검출한다.

    검출 대상:
    1. Lazy Class: DatatypeProperty와 ObjectProperty가 모두 0개인 클래스 (domain/range 어디에도 등장하지 않음)
    2. Lonely Disjoint: AllDisjointClasses에 멤버가 1개뿐인 경우
    3. Asymmetric inverseOf: P1 inverseOf P2이지만 P2에 inverseOf P1이 없는 경우
    4. Redundant subClassOf: A⊂B⊂C이면서 명시적 A⊂C가 존재 (전이적 중복)

    Args:
        ttl_content: 검증할 T-Box TTL 문자열. 비어있으면 ttl_path 또는 기본 T-Box 파일 사용.
        ttl_path: data/generated/tbox 아래 TTL 파일명. ttl_content가 비어있을 때 사용.
    """
    g = _new_graph()
    try:
        if ttl_path:
            ttl_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                ttl_path,
                allowed_suffixes=(".ttl",),
            )
        ttl = _load_ttl(ttl_content, ttl_path)
        g.parse(data=ttl, format="turtle")
    except Exception as e:
        return error_response(f"TTL 파싱 실패: {e}", logger=logger)

    steel_ns = DOMAIN_NS
    antipatterns: dict[str, list[dict]] = {
        "lazy_class": [],
        "lonely_disjoint": [],
        "asymmetric_inverse": [],
        "redundant_subclass": [],
    }

    # --- 1. Lazy Class ---
    # Classes with zero DP AND zero OP (neither as domain nor range)
    steel_classes = {c for c in g.subjects(RDF.type, OWL.Class)
                     if isinstance(c, URIRef) and str(c).startswith(steel_ns)}
    obj_props = {p for p in g.subjects(RDF.type, OWL.ObjectProperty)
                 if isinstance(p, URIRef) and str(p).startswith(steel_ns)}
    data_props = {p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
                  if isinstance(p, URIRef) and str(p).startswith(steel_ns)}
    all_props = obj_props | data_props

    # Collect classes appearing in any domain or range
    classes_in_domain_or_range: set[URIRef] = set()
    for prop in all_props:
        for d in g.objects(prop, RDFS.domain):
            if isinstance(d, URIRef) and d in steel_classes:
                classes_in_domain_or_range.add(d)
        for r in g.objects(prop, RDFS.range):
            if isinstance(r, URIRef) and r in steel_classes:
                classes_in_domain_or_range.add(r)

    for cls in steel_classes:
        if cls not in classes_in_domain_or_range:
            local = str(cls).split("#")[-1].split("/")[-1]
            antipatterns["lazy_class"].append({
                "class": local,
                "uri": str(cls),
                "reason": "No DatatypeProperty or ObjectProperty references this class as domain or range",
            })

    # --- 2. Lonely Disjoint ---
    for bnode in g.subjects(RDF.type, OWL.AllDisjointClasses):
        members_node = list(g.objects(bnode, OWL.members))
        if not members_node:
            continue
        try:
            members = list(rdflib.collection.Collection(g, members_node[0]))
        except Exception:  # noqa: BLE001 — 깨진 disjoint 목록은 해당 안티패턴 판정에서 제외한다
            continue
        if len(members) <= 1:
            member_names = [str(m).split("#")[-1].split("/")[-1] for m in members]
            antipatterns["lonely_disjoint"].append({
                "members": member_names,
                "count": len(members),
                "reason": "AllDisjointClasses group with <= 1 member is meaningless",
            })

    # --- 3. Asymmetric inverseOf ---
    for prop in obj_props:
        for inv in g.objects(prop, OWL.inverseOf):
            if not isinstance(inv, URIRef):
                continue
            # Check if inv declares inverseOf back to prop
            reverse_inverses = set(g.objects(inv, OWL.inverseOf))
            if prop not in reverse_inverses:
                p_local = str(prop).split("#")[-1].split("/")[-1]
                inv_local = str(inv).split("#")[-1].split("/")[-1]
                antipatterns["asymmetric_inverse"].append({
                    "property": p_local,
                    "inverse": inv_local,
                    "reason": f"{p_local} inverseOf {inv_local} but {inv_local} does NOT declare inverseOf {p_local}",
                })

    # --- 4. Redundant subClassOf (transitive redundancy) ---
    # Build parent map: child -> set of direct parents
    direct_parents: dict[URIRef, set[URIRef]] = {}
    for cls in steel_classes:
        parents = set()
        for p in g.objects(cls, RDFS.subClassOf):
            if isinstance(p, URIRef) and p in steel_classes:
                parents.add(p)
        direct_parents[cls] = parents

    def _ancestors(cls: URIRef, exclude_direct: URIRef, visited: set | None = None) -> set[URIRef]:
        """Compute all ancestors of cls reachable WITHOUT going through exclude_direct first."""
        if visited is None:
            visited = set()
        result: set[URIRef] = set()
        for p in direct_parents.get(cls, set()):
            if p == exclude_direct or p in visited:
                continue
            visited.add(p)
            result.add(p)
            result |= _ancestors(p, exclude_direct, visited)
        return result

    for cls in steel_classes:
        parents = direct_parents.get(cls, set())
        if len(parents) < 2:
            continue
        # For each pair of direct parents (A, B): if B is reachable from A
        # via subClassOf chain, then cls -> B is redundant
        parent_list = sorted(parents, key=str)
        for i, pa in enumerate(parent_list):
            for pb in parent_list[i + 1:]:
                # Check if pb is ancestor of pa (meaning cls->pb is redundant via pa)
                pa_ancestors = _ancestors(pa, cls)
                if pb in pa_ancestors:
                    cls_local = str(cls).split("#")[-1].split("/")[-1]
                    pa_local = str(pa).split("#")[-1].split("/")[-1]
                    pb_local = str(pb).split("#")[-1].split("/")[-1]
                    antipatterns["redundant_subclass"].append({
                        "class": cls_local,
                        "direct_parent": pb_local,
                        "via": pa_local,
                        "reason": f"{cls_local} ⊂ {pa_local} ⊂ {pb_local} with explicit {cls_local} ⊂ {pb_local}",
                    })
                    continue
                # Check the reverse: pa reachable from pb
                pb_ancestors = _ancestors(pb, cls)
                if pa in pb_ancestors:
                    cls_local = str(cls).split("#")[-1].split("/")[-1]
                    pa_local = str(pa).split("#")[-1].split("/")[-1]
                    pb_local = str(pb).split("#")[-1].split("/")[-1]
                    antipatterns["redundant_subclass"].append({
                        "class": cls_local,
                        "direct_parent": pa_local,
                        "via": pb_local,
                        "reason": f"{cls_local} ⊂ {pb_local} ⊂ {pa_local} with explicit {cls_local} ⊂ {pa_local}",
                    })

    total = sum(len(v) for v in antipatterns.values())
    return json.dumps({
        "success": True,
        "antipatterns": antipatterns,
        "counts": {k: len(v) for k, v in antipatterns.items()},
        "total": total,
        "clean": total == 0,
    }, ensure_ascii=False, indent=2)
