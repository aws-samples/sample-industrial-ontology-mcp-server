"""OWL ↔ SHACL cardinality sync 검증 — #8.

T-Box의 owl:*Cardinality Restriction과 SHACL shapes 파일의 sh:minCount/maxCount
제약이 **동일 의미**를 갖고 있는지 확인한다.

Motivation:
- OWL cardinality는 Open World — "모델 안에 선언된 개수"만 본다.
- SHACL cardinality는 Closed World — "A-Box 인스턴스에 실제 채워진 값"을 본다.
- 둘이 다르게 선언돼 있으면 배포 후 "HermiT는 통과하지만 SHACL에서는 실패"
  (또는 그 반대)의 혼란이 발생.

본 도구는 다음을 탐지:
1. OWL에 있지만 SHACL에 없는 제약 (CWA 검증 누락)
2. SHACL에 있지만 OWL에 없는 제약 (스키마 선언 누락)
3. 두쪽 모두 있지만 값이 다른 경우 (inconsistent)

결과는 report 형식이며, 수정은 사용자 책임.

## 두 결함이 이 도구를 무의미하게 만들고 있었다 (2026-08-30 규명)

**(1) BNode 만 받아 공리를 하나도 추출하지 못했다.** ``_extract_owl_cardinalities``
가 ``if not isinstance(restr, BNode): continue`` 로 익명 restriction 만 봤는데,
S3 의 ``step_19_bnode_skolemize`` 가 restriction 을 **명명 IRI 로 바꾼다**. 실측:

    owl:Restriction 100개 — BNode 0 / URIRef 100  →  추출 0개

그래서 ``owl_only`` 가 항상 ``[]`` 이었고 그것이 "누락 없음" 처럼 읽혔다. 이 리포의
"익명 표현식이 게이트를 죽였다" 와 같은 축이다 (그때는 반대로 skolemize 된 노드를
클래스로 **세어서** 게이트가 죽었다).

**(2) 비교 대상 축이 애초에 어긋나 있었다.** 기본 shapes 파일
``rules/policy/tbox_shapes.ttl`` 은 **T-Box 메타 shape** 이다 — "모든
``owl:DatatypeProperty`` 는 ``rdfs:label`` 을 가져야 한다" 류. 반면 OWL
cardinality restriction 은 **A-Box 개체** 에 대한 제약이다
(``EquipmentMaster ⊑ ≤1 hasEquipmentStatus``). 실측 ``shacl_only`` 6건은 전부
``targetClass owl:DatatypeProperty`` / ``path rdfs:label`` 같은 메타 shape 이라
OWL 쪽에 대응이 있을 수 없다.

(1)만 고치면 31개 공리가 전부 ``owl_only`` 로 쏟아진다 — 그것은 결함 발견이 아니라
**오발화**다. 이 리포에는 "게이트가 열리면 핵심 게이트 23개를 제거 후보로 인쇄했다"
는 전례가 있다.

그래서 두 축을 **분리해 보고**한다: 도메인 클래스를 target 으로 하는 shape 만
OWL 과 대조하고 (``comparable_*``), 메타 shape 은 ``meta_shapes_skipped`` 로 세어
비교에서 제외한다. 대조 가능한 shape 이 0개면 그 사실을 명시한다 — 이 리포의
"0회 발동 게이트는 0회로 정직하게 보고하라" 원칙.
"""
from __future__ import annotations

import logging
import os

from rdflib import BNode, Graph, Namespace, URIRef

from config import GENERATED_TBOX_DIR, TBOX_PATH
from domain.namespaces import DOMAIN_NS
from domain.rules_paths import RULES_ROOT, rules_path
from domain.tbox_utils import _new_graph
from tools.common import (
    error_response,
    resolve_child_path,
    resolve_path_within,
    success_response,
)

logger = logging.getLogger(__name__)

SH = Namespace("http://www.w3.org/ns/shacl#")
OWL_NS = Namespace("http://www.w3.org/2002/07/owl#")
RDF_NS = Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#")
RDFS_NS = Namespace("http://www.w3.org/2000/01/rdf-schema#")

_DEFAULT_SHAPES = rules_path("tbox_shapes.ttl")


def _extract_owl_cardinalities(tbox: Graph) -> dict[tuple[str, str], dict]:
    """T-Box에서 owl:Restriction → (target_class, on_property) → constraints 추출.

    Returns:
        {(class_uri, property_uri): {min: int?, max: int?, exact: int?,
                                     someValuesFrom: class_uri?,
                                     hasValue: literal/uri?}}
    """
    out: dict[tuple[str, str], dict] = {}

    # subject 클래스가 subClassOf <Restriction> 패턴.
    #
    # ⚠️ **BNode 로 한정하지 말 것.** S3 의 ``step_19_bnode_skolemize`` 가 restriction
    # 을 명명 IRI 로 바꾸므로 배포 T-Box 에는 BNode restriction 이 **0개**다 (실측:
    # 100개 전부 URIRef). 예전 구현은 ``isinstance(restr, BNode)`` 로 걸러 공리를
    # 하나도 추출하지 못했고, 빈 ``owl_only`` 가 "누락 없음" 으로 읽혔다.
    for cls in tbox.subjects(RDFS_NS.subClassOf, None):
        if not isinstance(cls, URIRef) or not str(cls).startswith(str(DOMAIN_NS)):
            continue
        for restr in tbox.objects(cls, RDFS_NS.subClassOf):
            if not isinstance(restr, BNode | URIRef):
                continue
            # Restriction인지 확인 — 종류(BNode/IRI)가 아니라 **타입**으로 판정한다.
            types = list(tbox.objects(restr, RDF_NS.type))
            if OWL_NS.Restriction not in types:
                continue
            on_props = list(tbox.objects(restr, OWL_NS.onProperty))
            if not on_props or not isinstance(on_props[0], URIRef):
                continue
            key = (str(cls), str(on_props[0]))
            entry = out.setdefault(key, {})
            for mincard in tbox.objects(restr, OWL_NS.minCardinality):
                entry["min"] = int(str(mincard))
            for maxcard in tbox.objects(restr, OWL_NS.maxCardinality):
                entry["max"] = int(str(maxcard))
            for exact in tbox.objects(restr, OWL_NS.cardinality):
                entry["exact"] = int(str(exact))
            for sv in tbox.objects(restr, OWL_NS.someValuesFrom):
                entry["someValuesFrom"] = str(sv)
            for hv in tbox.objects(restr, OWL_NS.hasValue):
                entry["hasValue"] = str(hv)
    return out


def _extract_shacl_cardinalities(shapes: Graph) -> dict[tuple[str, str], dict]:
    """SHACL shapes에서 NodeShape → (target_class, path) → constraints 추출."""
    out: dict[tuple[str, str], dict] = {}
    for shape in shapes.subjects(RDF_NS.type, SH.NodeShape):
        targets = list(shapes.objects(shape, SH.targetClass))
        if not targets:
            continue
        for target in targets:
            if not isinstance(target, URIRef):
                continue
            for prop_shape in shapes.objects(shape, SH.property):
                paths = list(shapes.objects(prop_shape, SH.path))
                if not paths or not isinstance(paths[0], URIRef):
                    continue
                key = (str(target), str(paths[0]))
                entry = out.setdefault(key, {})
                for mc in shapes.objects(prop_shape, SH.minCount):
                    entry["min"] = int(str(mc))
                for xc in shapes.objects(prop_shape, SH.maxCount):
                    entry["max"] = int(str(xc))
                for hv in shapes.objects(prop_shape, SH.hasValue):
                    entry["hasValue"] = str(hv)
    return out


def check_cardinality_sync(
    tbox_path: str = "",
    shapes_path: str = "",
) -> dict:
    """OWL vs SHACL cardinality 일치 여부 검증.

    Returns:
        {
            owl_only: [{class, property, constraints}],  # SHACL에 없는 제약
            shacl_only: [{class, property, constraints}],  # OWL에 없는 제약
            inconsistent: [{class, property, owl, shacl, diff}],
            consistent_count: int,
            total_checked: int,
        }
    """
    tpath = tbox_path or TBOX_PATH
    spath = shapes_path or _DEFAULT_SHAPES
    tbox = _new_graph()
    tbox.parse(tpath, format="turtle")
    shapes = _new_graph()
    shapes.parse(spath, format="turtle")

    owl_map = _extract_owl_cardinalities(tbox)
    shacl_map_all = _extract_shacl_cardinalities(shapes)

    # 축 분리 — 메타 shape 은 비교 대상이 아니다 (모듈 docstring 의 결함 (2)).
    #
    # OWL cardinality restriction 은 **A-Box 개체** 에 대한 제약이고, 기본 shapes
    # 파일은 **T-Box 메타** shape ("모든 DP 는 rdfs:label 을 가져야 한다") 이다.
    # 도메인 클래스를 target 으로 하지 않는 shape 을 OWL 과 대조하면 대응이 있을 수
    # 없으므로 전부 ``shacl_only`` 로 인쇄된다 — 결함이 아니라 오발화다.
    shacl_map = {
        key: value for key, value in shacl_map_all.items()
        if key[0].startswith(str(DOMAIN_NS))
    }
    meta_shapes_skipped = len(shacl_map_all) - len(shacl_map)

    owl_only = []
    shacl_only = []
    inconsistent = []
    consistent = 0

    # 대조 가능한 shape 이 하나도 없으면 **비교를 하지 않는다.**
    #
    # 이 경우 모든 OWL 공리가 "SHACL 에 없다" 로 참이지만, 그것은 결함 발견이 아니라
    # 비교 대상 부재다. 58개를 ``owl_only`` 로 인쇄하면 "SHACL shape 58개를 추가하라"
    # 는 잘못된 권고가 되고 (기본 shapes 는 애초에 다른 축이다), 이 리포에는 게이트가
    # 열리자 핵심 게이트 23개를 제거 후보로 인쇄한 전례가 있다.
    #
    # ``comparison_possible: False`` + ``owl_restrictions_extracted`` 로 상태를
    # 드러내되, 오발화 목록은 만들지 않는다.
    if not shacl_map:
        all_keys: set[tuple[str, str]] = set()
    else:
        all_keys = set(owl_map.keys()) | set(shacl_map.keys())
    for key in sorted(all_keys):
        owl_c = owl_map.get(key, {})
        shacl_c = shacl_map.get(key, {})
        cls, prop = key
        # exact → min+max 동등성 해석
        def _normalize(c: dict) -> dict:
            n = dict(c)
            if "exact" in n:
                n.setdefault("min", n["exact"])
                n.setdefault("max", n["exact"])
                del n["exact"]
            return n

        o = _normalize(owl_c)
        s = _normalize(shacl_c)

        if o and not s:
            owl_only.append({"class": cls, "property": prop,
                             "constraints": o})
        elif s and not o:
            shacl_only.append({"class": cls, "property": prop,
                               "constraints": s})
        elif o and s:
            diff_keys = []
            for k in ("min", "max", "hasValue"):
                if o.get(k) != s.get(k) and (
                    o.get(k) is not None or s.get(k) is not None
                ):
                    diff_keys.append(k)
            if diff_keys:
                inconsistent.append({
                    "class": cls, "property": prop,
                    "owl": o, "shacl": s, "diff_keys": diff_keys,
                })
            else:
                consistent += 1

    return {
        "owl_only": owl_only,
        "shacl_only": shacl_only,
        "inconsistent": inconsistent,
        "consistent_count": consistent,
        "total_checked": len(all_keys),
        # 추출 규모를 각인한다 — ``owl_only: []`` 만 보면 "누락 없음" 과 "공리를 하나도
        # 추출하지 못했다" 가 구분되지 않는다 (예전 BNode 필터가 정확히 그 상태였다).
        "owl_restrictions_extracted": len(owl_map),
        "shacl_shapes_comparable": len(shacl_map),
        # 도메인 클래스를 target 으로 하지 않아 비교에서 제외한 메타 shape 수.
        "meta_shapes_skipped": meta_shapes_skipped,
        # 대조 가능한 shape 이 없으면 이 감사는 아무것도 검증하지 못한다. 그 사실을
        # 명시한다 — 0회 발동을 0회로 보고하고, 침묵을 PASS 로 읽히지 않게 한다.
        "comparison_possible": bool(shacl_map),
        "note": (
            "SHACL shape 중 도메인 클래스를 target 으로 하는 것이 없어 OWL "
            "cardinality 와 대조할 수 없다. 기본 shapes(rules/policy/tbox_shapes.ttl) "
            "는 T-Box 메타 shape 이므로 A-Box 카디널리티 축과 대응하지 않는다 — "
            "A-Box 제약을 CWA 로 보려면 validate_owl_cardinality 를 쓰라."
        ) if not shacl_map else "",
    }


def check_shacl_owl_cardinality_sync(
    tbox_path: str = "",
    shapes_path: str = "",
) -> str:
    """로컬 OWL/SHACL 파일을 읽어 cardinality 일치 여부를 감사한다 (#8).

    OWL(OWA) vs SHACL(CWA) 규칙은 의미가 달라 두쪽이 따로 선언되면
    배포 후 일관성 문제가 생긴다. 본 도구는:
    1. OWL에만 있는 cardinality → SHACL shape 추가 권장
    2. SHACL에만 있는 → T-Box Restriction 추가 권장
    3. 둘 다 있는데 값이 다른 → inconsistent (수정 필요)

    Args:
        tbox_path: data/generated/tbox 아래 T-Box TTL 파일명. 비어 있으면 기본 T-Box.
        shapes_path: rules/ 아래 SHACL shapes TTL 경로 (절대경로 또는 작업 디렉터리
            기준 상대경로). symlink 해석 후에도 rules/ 안이어야 한다. 비어 있으면
            rules/policy/tbox_shapes.ttl.

    파일을 변경하거나 network operation을 수행하지 않는다. 두 경로는 위 디렉터리
    밖이나 URL 을 가리키면 읽기 전에 거부한다.
    """
    try:
        if tbox_path:
            tbox_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                tbox_path,
                allowed_suffixes=(".ttl",),
            )
        if shapes_path:
            shapes_path = resolve_path_within(
                RULES_ROOT,
                shapes_path,
                allowed_suffixes=(".ttl",),
            )
        # rdflib 는 없는 경로 문자열을 URL 로 해석하려 하므로 파싱 전에 확인한다.
        for label, path in (
            ("T-Box", tbox_path or TBOX_PATH),
            ("SHACL shapes", shapes_path or _DEFAULT_SHAPES),
        ):
            if not os.path.isfile(path):
                return error_response(f"{label} 파일이 없습니다: {path}", logger=logger)
        report = check_cardinality_sync(tbox_path, shapes_path)
        # ``comparison_possible=False`` 를 ``ok`` 로 보고하면 안 된다.
        #
        # 오발화를 막으려고 "대조 가능한 shape 이 0개면 비교를 건너뛴다" 로 고쳤는데,
        # severity 는 세 리스트가 비었는지만 봐서 "58개 공리를 추출했지만 대조 대상이
        # 없어 아무것도 검증하지 못했다" 가 ``ok`` 로 나왔다. ``ok`` 는 "대조했고
        # 일치했다" 로 읽히고, 그 오독이 정확히 예전 BNode 필터가 만든 상태다.
        #
        # 이 리포의 원칙: "검증기 실패 = 검증 없음" / "0회 발동 게이트는 0회로 정직하게
        # 보고하라". 임계 완화가 아니라 **측정 불가를 측정 불가로 표기**하는 것이므로
        # 지표 매수가 아니다. 기존 어휘와 충돌하지 않게 ``info`` 를 쓴다 (상위 소비자가
        # critical/warning/ok 3값을 가정할 수 있다).
        if not report.get("comparison_possible", True):
            report["severity"] = "info"
        else:
            report["severity"] = (
                "critical" if report["inconsistent"]
                else ("warning" if (report["owl_only"] or report["shacl_only"])
                      else "ok")
            )
        report["citation"] = (
            "Knublauch & Kontokostas (2017). Shapes Constraint Language (SHACL). "
            "W3C Recommendation. § 4.6 Cardinality Constraints."
        )
        return success_response(report)
    except Exception as e:
        return error_response(e, logger=logger)
