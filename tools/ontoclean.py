"""OntoClean 메타-속성 기반 T-Box 검증 — B (근거: Guarino & Welty 2002 [20]).

핵심 제약:
- C1 (Rigidity): -R 클래스는 +R 서브클래스를 가질 수 없다.
- C2 (Unity): 서로 다른 unity criterion을 가진 클래스 간 subClassOf 금지.
- C3 (Identity): identity criterion이 있는 클래스의 서브클래스는 동일 기준 상속.
- C4 (Dependence): 의존 클래스는 피의존 클래스와의 관계가 T-Box에 명시돼야 함.

메타 속성은 T-Box annotation으로 선언한다고 가정:
    steel-oc:rigidity "+R" | "-R" | "~R"
    steel-oc:unity "+U" | "-U"
    steel-oc:identity "+I" | "-I"
    steel-oc:dependence "+D" | "-D"

선언되지 않은 클래스는 "unlabeled"로 분류. 검증은 선언된 쌍에 대해서만.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from config import GENERATED_REPORTS_DIR, TBOX_PATH
from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.common import error_response, is_deployed_input, success_response, write_deployed_sidecar

logger = logging.getLogger(__name__)

ONTOCLEAN_NS = Namespace(str(DOMAIN_NS).rstrip("#") + "-ontoclean#")
OC_RIGIDITY = URIRef(str(ONTOCLEAN_NS) + "rigidity")
OC_UNITY = URIRef(str(ONTOCLEAN_NS) + "unity")
OC_IDENTITY = URIRef(str(ONTOCLEAN_NS) + "identity")
OC_DEPENDENCE = URIRef(str(ONTOCLEAN_NS) + "dependence")
# T2: auto-labeled flag — steel-oc:autoLabel true 일 때 휴리스틱으로 붙은 라벨.
OC_AUTO_LABEL = URIRef(str(ONTOCLEAN_NS) + "autoLabel")

RIGIDITY_VALUES = {"+R", "-R", "~R"}
BINARY_VALUES = {"+U", "-U", "+I", "-I", "+D", "-D"}


def _local(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def _load_meta_annotations(tbox: Graph) -> dict[str, dict]:
    """T-Box에서 OntoClean annotation 수집.

    반환: {class_local_name: {"rigidity": "+R", "unity": "+U", ..., "_auto": bool}}
    ``_auto`` 필드는 `steel-oc:autoLabel true` 플래그 유무 (T2 휴리스틱 라벨링).
    """
    meta: dict[str, dict] = {}
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        key = _local(str(cls))
        entry = {}
        for pred, name in [
            (OC_RIGIDITY, "rigidity"),
            (OC_UNITY, "unity"),
            (OC_IDENTITY, "identity"),
            (OC_DEPENDENCE, "dependence"),
        ]:
            vals = list(tbox.objects(cls, pred))
            if vals:
                entry[name] = str(vals[0])
        if entry:
            auto_vals = list(tbox.objects(cls, OC_AUTO_LABEL))
            entry["_auto"] = any(str(v).lower() == "true" for v in auto_vals)
            meta[key] = entry
    return meta


def _collect_subclass_pairs(tbox: Graph) -> list[tuple[str, str]]:
    """(subclass_local, parent_local) 리스트."""
    pairs = []
    for s, _, o in tbox.triples((None, RDFS.subClassOf, None)):
        if isinstance(s, URIRef) and isinstance(o, URIRef):
            pairs.append((_local(str(s)), _local(str(o))))
    return pairs


def check_rigidity_constraint(
    meta: dict[str, dict], pairs: list[tuple[str, str]],
) -> list[dict]:
    """C1: -R 클래스가 +R 서브클래스를 가지면 위반."""
    violations = []
    for sub, parent in pairs:
        sub_r = meta.get(sub, {}).get("rigidity")
        par_r = meta.get(parent, {}).get("rigidity")
        if sub_r == "+R" and par_r == "-R":
            violations.append({
                "rule": "C1_rigidity",
                "severity": "critical",
                "subclass": sub,
                "subclass_rigidity": sub_r,
                "parent": parent,
                "parent_rigidity": par_r,
                "message": (
                    f"+R 클래스 {sub}가 -R 클래스 {parent}의 서브클래스. "
                    "rigid 인스턴스가 non-rigid 상위에 속하면 존재론적 모순."
                ),
            })
    return violations


def check_unity_constraint(
    meta: dict[str, dict], pairs: list[tuple[str, str]],
) -> list[dict]:
    """C2: +U 클래스가 -U 클래스의 서브클래스가 되면 위반 (혹은 그 역도 일반적으로 의심)."""
    violations = []
    for sub, parent in pairs:
        sub_u = meta.get(sub, {}).get("unity")
        par_u = meta.get(parent, {}).get("unity")
        if sub_u == "+U" and par_u == "-U":
            violations.append({
                "rule": "C2_unity",
                "severity": "high",
                "subclass": sub,
                "parent": parent,
                "message": (
                    f"+U 클래스 {sub}가 -U 클래스 {parent}의 서브클래스. "
                    "unity 기준이 상속되지 않음."
                ),
            })
    return violations


def check_identity_constraint(
    meta: dict[str, dict], pairs: list[tuple[str, str]],
) -> list[dict]:
    """C3: -I 클래스가 +I 서브클래스를 가지면 identity criterion이 비상속 → 위반."""
    violations = []
    for sub, parent in pairs:
        sub_i = meta.get(sub, {}).get("identity")
        par_i = meta.get(parent, {}).get("identity")
        if sub_i == "+I" and par_i == "-I":
            violations.append({
                "rule": "C3_identity",
                "severity": "high",
                "subclass": sub,
                "parent": parent,
                "message": (
                    f"+I 클래스 {sub}의 identity criterion이 -I 상위 {parent}에서 "
                    "정의되지 않음. OntoClean 원칙 위반."
                ),
            })
    return violations


def analyze_ontoclean(tbox_path: str | None = None) -> dict:
    path = tbox_path or TBOX_PATH
    if not os.path.exists(path):
        return {"error": f"T-Box not found: {path}"}
    tbox = _new_graph()
    tbox.parse(path, format="turtle")

    meta = _load_meta_annotations(tbox)
    pairs = _collect_subclass_pairs(tbox)

    all_classes = {
        _local(str(c)) for c in tbox.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(DOMAIN_NS)
    }
    labeled = set(meta.keys())
    unlabeled = all_classes - labeled
    # T2: manual vs auto (heuristic) 분리 — coverage 가 automatic bloat 되는 것 구분.
    auto_labeled = {k for k, m in meta.items() if m.get("_auto")}
    manual_labeled = labeled - auto_labeled

    violations = (
        check_rigidity_constraint(meta, pairs)
        + check_unity_constraint(meta, pairs)
        + check_identity_constraint(meta, pairs)
    )

    by_severity = defaultdict(int)
    for v in violations:
        by_severity[v["severity"]] += 1

    total_for_pct = max(len(all_classes), 1)
    labeling_coverage = round(
        len(labeled) / total_for_pct * 100, 1,
    ) if all_classes else 0.0
    manual_coverage = round(
        len(manual_labeled) / total_for_pct * 100, 1,
    ) if all_classes else 0.0
    auto_coverage = round(
        len(auto_labeled) / total_for_pct * 100, 1,
    ) if all_classes else 0.0

    return {
        "classes_total": len(all_classes),
        "classes_labeled": len(labeled),
        "classes_manual_labeled": len(manual_labeled),
        "classes_auto_labeled": len(auto_labeled),
        "classes_unlabeled": len(unlabeled),
        "labeling_coverage_pct": labeling_coverage,
        "manual_coverage_pct": manual_coverage,
        "auto_coverage_pct": auto_coverage,
        "subclass_pairs_checked": len(pairs),
        "violations_total": len(violations),
        "by_severity": dict(by_severity),
        "violations": violations[:50],
        "unlabeled_sample": sorted(unlabeled)[:20],
        "annotations_sample": dict(list(meta.items())[:10]),
    }


def validate_ontoclean(tbox_path: str = "") -> str:
    """OntoClean 메타-속성 위반 검증 (B, [20]).

    T-Box에 steel-ontoclean: annotation으로 rigidity/unity/identity/dependence를
    선언한 클래스에 한해 3대 제약(C1/C2/C3)을 검증.

    선언이 없으면 "unlabeled"로 분류하고 coverage 보고.

    Args:
        tbox_path: T-Box TTL 경로. 비어있으면 config의 TBOX_PATH 사용.
    """
    try:
        report = analyze_ontoclean(tbox_path or None)
        # analyze_ontoclean 은 실패를 `{"error": ...}` 로 돌려주는데 그것을
        # success_response 로 감싸면 호출자가 성공으로 읽는다 (foops_fair 는
        # 같은 자리에서 이미 error_response 로 내린다). 같은 결함 부류다.
        if "error" in report:
            return error_response(report["error"], logger=logger)
        # 배포 보고서가 `{"error": "T-Box not found: /nonexistent/t.ttl"}` 였다
        # (실측 2026-09-02). 실패한 분석이 성공한 보고서와 같은 형태로 남아
        # 다음 소비자가 그것을 사실로 읽었다. 임의 경로 입력도 마찬가지로
        # 배포 사이드카를 덮어쓰지 않는다.
        write = write_deployed_sidecar(
            os.path.join(GENERATED_REPORTS_DIR, "ontoclean_report.json"),
            report,
            sources={"tbox": tbox_path or TBOX_PATH},
            inputs_are_deployed=is_deployed_input(tbox_path, TBOX_PATH),
            logger=logger,
        )
        return success_response({**report, "report_written": write})
    except Exception as e:
        return error_response(f"ontoclean analyze 실패: {e}", logger=logger)
