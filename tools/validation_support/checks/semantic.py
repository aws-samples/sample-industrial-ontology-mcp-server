"""Semantic check 그룹.

T-Box 선언과 A-Box 데이터 간의 의미적 일치를 검증:
- domain_range_conformance: ObjectProperty의 domain/range가 실제 인스턴스 타입과 일치
- property_coverage: T-Box 정의 프로퍼티가 A-Box에서 실제 쓰이는 비율
- tbox_fitness: T-Box 클래스가 CSV/tacit 데이터 소스와 매핑되는 비율

공유 자원: SharedCheckContext.instance_types, superclass_map, steel_classes, ns.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from config import SOURCE_RAWDATA_DIR, SOURCE_TACIT_DIR
from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.validation_support.common import (
    SharedCheckContext,
    local,
    validate_prop_name,
)
from tools.validation_support.thresholds import get_tier_thresholds

logger = logging.getLogger(__name__)

# Tacit TTL 클래스 mtime 기반 캐시 (모듈 레벨 전역)
_tacit_classes_cache: tuple[float, set[str]] | None = None


def load_tacit_classes_cached(
    source_tacit_dir: str | None = None,
) -> set[str]:
    """tacit TTL 디렉터리에서 steel 클래스 집합을 추출 (mtime 캐시)."""
    global _tacit_classes_cache
    tacit_dir = source_tacit_dir if source_tacit_dir is not None else SOURCE_TACIT_DIR
    if not os.path.isdir(tacit_dir):
        return set()
    try:
        mtime = max(
            (os.path.getmtime(os.path.join(tacit_dir, f))
             for f in os.listdir(tacit_dir) if f.lower().endswith(".ttl")),
            default=0.0,
        )
    except OSError:
        mtime = 0.0
    if _tacit_classes_cache is not None and _tacit_classes_cache[0] == mtime:
        return _tacit_classes_cache[1]

    classes: set[str] = set()
    for fname in os.listdir(tacit_dir):
        if not fname.lower().endswith(".ttl"):
            continue
        try:
            tg = _new_graph()
            tg.parse(os.path.join(tacit_dir, fname), format="turtle")
            for _, _, t in tg.triples((None, RDF.type, None)):
                if isinstance(t, URIRef) and str(t).startswith(DOMAIN_NS):
                    classes.add(_local_name(str(t)))
        except Exception as e:
            logger.debug("tacit TTL 파싱 실패 (%s): %s", fname, e)
    _tacit_classes_cache = (mtime, classes)
    return classes


def invalidate_tacit_cache() -> None:
    global _tacit_classes_cache
    _tacit_classes_cache = None


def check_domain_range_conformance(
    g: Graph, tbox: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """7. domain/range 타입 정합성 — ObjectProperty 사용이 T-Box 정의와 일치하는지 전수 검증."""
    if shared is None:
        shared = SharedCheckContext(g, tbox)
    ns = shared.ns
    superclass_map = shared.superclass_map
    instance_types = shared.instance_types

    def _is_compatible(actual_type: str, expected_type: str) -> bool:
        if actual_type == expected_type:
            return True
        return expected_type in superclass_map.get(actual_type, set())

    def _axis(prop: URIRef, pred: URIRef) -> str | None:
        """한 축(domain 또는 range)의 기대 클래스. ``None`` = 검사 불가.

        ``owl:Thing`` 은 **universal class** 로 제약이 없다 — 미선언(빈 값)과
        구분해야 한다. 둘을 같게 취급하면 검사에서 빼는 결과가 같아 보이지만,
        universal 은 "통과" 이고 미선언은 "판정 불가" 다. 이 리포는 그 혼동으로
        같은 버그가 세 곳에 퍼진 이력이 있어 ``structural.py:39-48`` 이
        ``None`` 센티넬로 구분한다 — 같은 의미론을 여기서도 쓴다.

        Returns:
            도메인 클래스 local name — 그 축을 검사할 수 있을 때.
            ``None`` — universal(``owl:Thing``) 또는 미선언. 그 축만 건너뛴다.
        """
        vals = set(tbox.objects(prop, pred))
        if OWL.Thing in vals:
            # 아래 도메인-NS 필터도 owl:Thing 을 걸러 같은 None 을 내지만, 이
            # 분기를 명시해 **의도** 를 남긴다: universal 은 "제약 없음(통과)" 이고
            # 우연히 필터에 걸린 외래 IRI 가 아니다. 필터 조건이 바뀌어도
            # universal 의미론이 함께 흔들리지 않는다.
            return None                       # universal → 제약 없음
        named = [local(str(v)) for v in vals
                 if isinstance(v, URIRef) and str(v).startswith(ns)]
        return named[0] if named else None    # 미선언 → 판정 불가

    # **축을 독립적으로 판정한다.** 예전엔 `if domains and ranges` 로 둘 다
    # 있어야 등록했는데, 그러면 domain 이 owl:Thing 인 OP 는 **range 검사까지
    # 함께 잃는다**. 실측 (2026-08-11 배포 T-Box): owl:Thing domain OP 62개가
    # 전부 range 는 구체 클래스였고, 그 62개에 실린 A-Box 트리플 12,846건이
    # 무검사 상태였다 (검사 커버리지 167/229 = 72.9%). mutation 으로 확인:
    # domain 위반·range 위반을 주입해도 이 검사가 놓쳤다.
    op_schema: dict[str, dict[str, str | None]] = {}
    op_uris: dict[str, URIRef] = {}
    for prop in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if not isinstance(prop, URIRef) or not str(prop).startswith(ns):
            continue
        expected_domain = _axis(prop, RDFS.domain)
        expected_range = _axis(prop, RDFS.range)
        if expected_domain is None and expected_range is None:
            continue                          # 두 축 모두 검사 불가 → 등록 의미 없음
        name = local(str(prop))
        op_schema[name] = {"domain": expected_domain, "range": expected_range}
        op_uris[name] = prop

    violations: list[dict] = []
    checked_triples = 0
    all_props = list(op_schema.keys())

    for prop_name in all_props:
        prop_uri = op_uris[prop_name]
        expected = op_schema[prop_name]
        validate_prop_name(prop_name)

        checked_subjects: set[str] = set()
        checked_objects: set[str] = set()

        for s, _, o in g.triples((None, prop_uri, None)):
            checked_triples += 1
            s_uri = str(s)
            o_uri = str(o)

            if expected["domain"] is not None and s_uri not in checked_subjects:
                checked_subjects.add(s_uri)
                s_types = instance_types.get(s_uri, set())
                if (
                    s_types
                    and len(violations) < 10
                    and not any(_is_compatible(t, expected["domain"]) for t in s_types)
                ):
                    violations.append({
                        "property": prop_name,
                        "issue": "domain 불일치",
                        "expected": expected["domain"],
                        "actual": sorted(s_types),
                        "subject": local(s_uri),
                    })

            if (expected["range"] is not None and isinstance(o, URIRef)
                    and o_uri not in checked_objects):
                checked_objects.add(o_uri)
                o_types = instance_types.get(o_uri, set())
                if (
                    o_types
                    and len(violations) < 10
                    and not any(_is_compatible(t, expected["range"]) for t in o_types)
                ):
                    violations.append({
                        "property": prop_name,
                        "issue": "range 불일치",
                        "expected": expected["range"],
                        "actual": sorted(o_types),
                        "object": local(o_uri),
                    })

    # ── DatatypeProperty 축 ────────────────────────────────────────────
    #
    # ## 왜 추가했나 (2026-09-04, S9.5 전량 노출 실측)
    #
    # 이 검사는 ``subjects(RDF.type, OWL.ObjectProperty)`` 만 순회했다. 그래서 DP 의
    # domain/range 변조가 **구조적으로 보이지 않았다**. KG mutation 감사에서 미검출
    # 8건 중 3건이 정확히 이 갭이었고, 셋 다 같은 DP 를 표적으로 했다::
    #
    #   M1/swap_domain   DP domain 을 무관한 클래스로 교체        → 무발화
    #   M1/delete_domain DP domain 선언 제거                      → 무발화
    #   M6/swap_range    DP range 를 datatype 대신 **클래스** 로   → 무발화
    #
    # SHACL(S4)이 DP domain/range 필수를 보지만 S9.5 는 validate_kg 만 돌린다 —
    # 즉 KG 축에는 이 판정이 전무했다.
    #
    # 세 축을 독립으로 본다. baseline 은 셋 다 0 이다 (실측 2026-09-04: DP 264개
    # 전부 도메인 클래스 domain 보유 / range 가 클래스인 것 0 / 리터럴 445,044건
    # 검사 위반 0). baseline 이 FAIL 이면 그 축은 catch 를 등록할 수 없으므로,
    # 0 인 것을 확인하고 넣었다.
    #
    # ``delete_domain`` 처럼 **선언을 제거하는** 변조는 위반 검사로는 잡히지 않는다
    # (제약이 사라지면 위반할 대상도 없다). 그래서 "A-Box 가 쓰는데 domain 이 없다"
    # 를 별 축으로 둔다 — 이 리포의 "미선언 ≠ universal" 을 그대로 적용한 것이다.
    dp_violations: list[dict] = []
    dp_checked_triples = 0
    dp_domain_checkable = 0
    declared_dps = 0
    dp_used_without_domain: list[str] = []
    dp_non_datatype_range: list[dict] = []

    for prop in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not isinstance(prop, URIRef) or not str(prop).startswith(ns):
            continue
        declared_dps += 1
        prop_name = local(str(prop))
        expected_domain = _axis(prop, RDFS.domain)

        # range 는 datatype 이어야 한다. 도메인 클래스를 가리키면 DP 가 개체를
        # 값으로 받는다는 뜻이 되어 OWL 2 DL 위반이고 A-Box 로더가 깨진다.
        for rng in tbox.objects(prop, RDFS.range):
            if isinstance(rng, URIRef) and str(rng).startswith(ns) and \
                    len(dp_non_datatype_range) < 10:
                dp_non_datatype_range.append({
                    "property": prop_name,
                    "issue": "DP range 가 datatype 이 아니다",
                    "actual": local(str(rng)),
                })

        if expected_domain is None:
            # ``_axis`` 는 universal(owl:Thing) 과 미선언을 **둘 다** None 으로 낸다.
            # 여기서 그 둘을 반드시 갈라야 한다: universal 은 "제약 없음(통과)" 이고
            # 미선언은 "선언 불완전" 이다. 같게 취급하면 owl:Thing domain DP 를
            # 결함으로 오보고한다 — 이 리포가 세 번 물린 버그 부류다
            # (``owl:Thing domain 사각지대``: universal ≠ 미선언).
            has_any_domain = next(tbox.objects(prop, RDFS.domain), None) is not None
            if has_any_domain:
                continue                      # universal → 제약 없음, 정상
            used = next(g.triples((None, prop, None)), None) is not None
            if used and len(dp_used_without_domain) < 10:
                dp_used_without_domain.append(prop_name)
            continue
        dp_domain_checkable += 1

        checked_subjects: set[str] = set()
        for s, _, o in g.triples((None, prop, None)):
            if not isinstance(o, Literal):
                continue                      # DP 값이 IRI 인 축은 undeclared_dp 소관
            dp_checked_triples += 1
            s_uri = str(s)
            if s_uri in checked_subjects:
                continue
            checked_subjects.add(s_uri)
            s_types = instance_types.get(s_uri, set())
            if (
                s_types
                and len(dp_violations) < 10
                and not any(_is_compatible(t, expected_domain) for t in s_types)
            ):
                dp_violations.append({
                    "property": prop_name,
                    "issue": "DP domain 불일치",
                    "expected": expected_domain,
                    "actual": sorted(s_types),
                    "subject": local(s_uri),
                })

    # 축별 커버리지를 함께 보고한다. 한쪽 축만 검사 가능한 OP 가 몇 개인지
    # 드러내지 않으면 "checked_properties 229" 가 두 축 전수 검사처럼 읽힌다.
    total_domain_ops = len([1 for meta in op_schema.values() if meta["domain"] is not None])
    total_range_ops = len([1 for meta in op_schema.values() if meta["range"] is not None])
    declared_ops = len([
        p for p in tbox.subjects(RDF.type, OWL.ObjectProperty)
        if isinstance(p, URIRef) and str(p).startswith(ns)
    ])
    dp_axis_failed = bool(
        dp_violations or dp_used_without_domain or dp_non_datatype_range
    )
    result: dict[str, object] = {
        "name": "domain/range 타입 정합성",
        "passed": len(violations) == 0 and not dp_axis_failed,
        "checked_properties": len(all_props),
        "checked_triples": checked_triples,
        "declared_object_properties": declared_ops,
        "domain_checkable_ops": total_domain_ops,
        "range_checkable_ops": total_range_ops,
        "universal_or_undeclared_skipped": declared_ops - len(all_props),
        "violations": violations[:10],
        # DP 축은 별 필드로 낸다 — 기존 ``violations`` 소비자의 형태를 바꾸지 않고
        # "OP 만 봤다" 와 "DP 도 봤다" 를 구분할 수 있게 한다.
        "declared_datatype_properties": declared_dps,
        "dp_domain_checkable": dp_domain_checkable,
        "dp_checked_triples": dp_checked_triples,
        "dp_violations": dp_violations[:10],
        "dp_used_without_domain": dp_used_without_domain[:10],
        "dp_non_datatype_range": dp_non_datatype_range[:10],
    }
    if dp_axis_failed:
        logger.warning(
            "domain/range 정합성 DP 축 위반 — domain 불일치 %d / domain 없이 사용 %d / "
            "range 가 datatype 아님 %d. DP 축을 추가하기 전에는 "
            "OP 만 순회해 DP 변조가 구조적으로 안 보였다)",
            len(dp_violations), len(dp_used_without_domain),
            len(dp_non_datatype_range),
        )
    return result


def check_property_coverage(
    g: Graph, tbox: Graph, *, class_tiers: dict[str, str] | None = None,
) -> dict[str, object]:
    """8. 프로퍼티 사용 커버리지 — T-Box 정의 프로퍼티가 A-Box에서 실제로 쓰이는지."""
    prop_defs: dict[str, dict] = {}
    for prop_type, label in [(OWL.ObjectProperty, "OP"), (OWL.DatatypeProperty, "DP")]:
        for prop in tbox.subjects(RDF.type, prop_type):
            if not isinstance(prop, URIRef) or not str(prop).startswith(DOMAIN_NS):
                continue
            name = local(str(prop))
            domains = [local(str(d)) for d in tbox.objects(prop, RDFS.domain)
                       if isinstance(d, URIRef) and str(d).startswith(DOMAIN_NS)]
            prop_defs[name] = {
                "type": label,
                "domain": domains[0] if domains else "owl:Thing",
                "uri": str(prop),
            }

    if not prop_defs:
        return {
            "name": "프로퍼티 사용 커버리지",
            "passed": True,
            "message": "T-Box에 프로퍼티 정의 없음",
            "defined": 0,
            "used": 0,
        }

    usage: dict[str, int] = {}
    for prop_name, defn in prop_defs.items():
        prop_uri = URIRef(defn["uri"])
        usage[prop_name] = sum(1 for _ in g.triples((None, prop_uri, None)))

    unused = sorted([n for n, c in usage.items() if c == 0])
    low_usage = sorted([n for n, c in usage.items() if 0 < c <= 5])
    total_defined = len(prop_defs)
    total_used = sum(1 for c in usage.values() if c > 0)
    coverage_pct = round(total_used / total_defined * 100, 1) if total_defined > 0 else 0

    min_coverage = 50
    if class_tiers is not None:
        tier_thresholds = get_tier_thresholds()
        active_tiers = set(class_tiers.values())
        tier_coverages = [
            tier_thresholds[t]["property_coverage"]
            for t in active_tiers if t in tier_thresholds
        ]
        if tier_coverages:
            min_coverage = min(tier_coverages)

    return {
        "name": "프로퍼티 사용 커버리지",
        "passed": coverage_pct >= min_coverage,
        "defined_properties": total_defined,
        "used_properties": total_used,
        "coverage_pct": coverage_pct,
        "unused_count": len(unused),
        "unused_top20": unused[:20],
        "low_usage_count": len(low_usage),
        "low_usage_top10": low_usage[:10],
    }


def check_tbox_fitness(
    tbox: Graph, *, class_tiers: dict[str, str] | None = None,
    shared: SharedCheckContext | None = None,
    source_rawdata_dir: str | None = None,
    source_tacit_dir: str | None = None,
) -> dict[str, object]:
    """10. T-Box Fitness — 클래스와 데이터 소스(CSV/tacit) 매핑 비율.

    매핑 판정 순서:
      0. ``rules/domain/table_class_mapping.json`` 의 명시적 테이블→클래스 매핑
      1. CSV 파일명 CamelCase 정규화 일치 (``Air_Emission_Monitoring`` ↔
         ``AirEmissionMonitoring``)
      2. 파일명 부분 문자열 포함
      3. tacit 클래스

    step 0 이 필요한 이유 (2026-07-25 실측): 1~2 는 **클래스명과 파일명의 글자
    유사도** 로 판정한다. 테이블명이 시스템 코드
    (``SOURCE_TABLE_002``) 이고 클래스가 도메인 용어(``ProcessStepA``)
    면 글자가 전혀 겹치지 않아 fitness 가 2.9% 로 오보고됐다 (실제로는 13개
    테이블 전부가 A-Box 에 100% 적재된 상태). ``SRC_TBL_13`` 파일명에 "materialA" 이
    우연히 포함돼 ``MaterialA`` 하나만 걸린 것이 유일한 매칭이었다.
    """
    if shared is not None:
        tbox_classes = set(shared.steel_classes)
    else:
        tbox_classes = {
            _local_name(str(cls))
            for cls in tbox.subjects(RDF.type, OWL.Class)
            if isinstance(cls, URIRef) and str(cls).startswith(DOMAIN_NS)
        }

    if not tbox_classes:
        return {
            "name": "T-Box Fitness",
            "passed": True,
            "message": "T-Box에 클래스 없음",
            "fitness": 0,
            "over_engineered": [],
            "under_covered": [],
        }

    csv_dir = source_rawdata_dir if source_rawdata_dir is not None else SOURCE_RAWDATA_DIR
    csv_names: set[str] = set()
    # CSV 파일명을 CamelCase 로 정규화된 형태로도 보관 → T-Box 클래스 매핑 정확도 향상
    csv_camel_names: set[str] = set()
    if os.path.isdir(csv_dir):
        for fname in os.listdir(csv_dir):
            if fname.lower().endswith(".csv"):
                base = fname.rsplit(".", 1)[0]
                csv_names.add(base)
                # Air_Emission_Monitoring → AirEmissionMonitoring
                parts = base.replace("-", "_").split("_")
                csv_camel_names.add("".join(p.capitalize() for p in parts if p))

    tacit_classes: set[str] = load_tacit_classes_cached(source_tacit_dir)

    # step 0 — 명시적 테이블→클래스 매핑. 이름 유사도로는 절대 찾을 수 없는
    # 시스템 코드 테이블명을 도메인 클래스에 잇는 authoritative 소스.
    mapped_classes: set[str] = set()
    mapped_tables: set[str] = set()
    try:
        from tools.abox_generation import _load_table_class_mapping
        table_class_map = _load_table_class_mapping()
        mapped_classes = {v for v in table_class_map.values() if v}
        mapped_tables = set(table_class_map)
    except Exception as exc:  # noqa: BLE001 — 측정이 파이프라인을 막지 않는다
        logger.debug("table_class_mapping 로드 skip (fitness): %s", exc)
    mapped_lower = {c.lower() for c in mapped_classes}

    classes_with_source: set[str] = set()
    csv_camel_lower = {c.lower() for c in csv_camel_names}
    for cls_name in tbox_classes:
        cls_lower = cls_name.lower()
        # 0. 명시적 매핑
        if cls_lower in mapped_lower:
            classes_with_source.add(cls_name)
            continue
        # 1. CamelCase 정규화 비교 (AirEmissionMonitoring ↔ Air_Emission_Monitoring)
        if cls_lower in csv_camel_lower:
            classes_with_source.add(cls_name)
            continue
        # 2. 기존 부분 문자열 비교도 유지 (교집합 넓힘)
        if any(cls_lower in cn.lower() or cn.lower() in cls_lower for cn in csv_names) or cls_name in tacit_classes:
            classes_with_source.add(cls_name)

    # 추상 상위 클래스는 CSV 테이블이 없는 것이 **정상 설계** 이므로 분모에서
    # 제외한다. 판정 로직은 '클래스별 인스턴스 수' 체크와 공유 —
    # ``find_abstract_parent_classes`` docstring 참조.
    from tools.validation_support.common import find_abstract_parent_classes
    abstract_parents = find_abstract_parent_classes(
        tbox, candidates=tbox_classes - classes_with_source,
    )

    scored_classes = tbox_classes - abstract_parents
    fitness = round(
        len(classes_with_source) / max(len(scored_classes), 1) * 100, 1,
    )
    over_engineered = sorted(scored_classes - classes_with_source)
    # under_covered = 대응 클래스가 없는 CSV 테이블. 매핑에 등록된 테이블은
    # 이름이 안 닮았을 뿐 클래스가 있으므로 제외한다 (미제외 시 실측 13개
    # 테이블 전부가 "미커버" 로 오보고된다).
    under_covered = sorted(csv_names - {c.lower() for c in tbox_classes}
                           - {c.lower() for c in classes_with_source}
                           - mapped_tables)

    threshold = 50
    if class_tiers is not None:
        tier_thresholds_cfg = get_tier_thresholds()
        active_tiers = set(class_tiers.values())
        tier_t = [
            tier_thresholds_cfg[t]["property_coverage"]
            for t in active_tiers if t in tier_thresholds_cfg
        ]
        if tier_t:
            threshold = min(tier_t)

    return {
        "name": "T-Box Fitness",
        "passed": fitness >= threshold,
        "fitness_pct": fitness,
        "tbox_classes": len(tbox_classes),
        "scored_classes": len(scored_classes),
        "abstract_parents_excluded": sorted(abstract_parents),
        "classes_with_source": len(classes_with_source),
        "csv_tables": len(csv_names),
        "tacit_classes": len(tacit_classes),
        "over_engineered": over_engineered[:20],
        "under_covered": under_covered[:20],
    }


#: T-Box 스키마 참조가 가리켜도 정상인 네임스페이스.
#: XSD 는 데이터타입, OWL/RDFS/RDF 는 메타 어휘 — 도메인이 선언할 대상이 아니다.
_SCHEMA_REF_ALLOWED_NS: tuple[str, ...] = (
    str(XSD),
    str(OWL),
    str(RDFS),
    str(RDF),
)

#: 스키마 참조 무결성을 검사하는 술어. 각각 "이 IRI 가 정의돼 있어야 한다" 를 전제한다.
_SCHEMA_REF_PREDICATES: tuple[tuple[URIRef, str], ...] = (
    (RDFS.domain, "domain"),
    (RDFS.range, "range"),
    (RDFS.subClassOf, "subClassOf"),
    (RDFS.subPropertyOf, "subPropertyOf"),
)


@dataclass(frozen=True)
class UndeclaredSchemaRef:
    """선언되지 않은 IRI 를 가리키는 스키마 참조 1건 (원본 트리플 포함)."""

    subject: URIRef
    predicate: URIRef
    target: URIRef
    axis: str

    def as_dict(self) -> dict[str, str]:
        """보고용 JSON-safe 형태. rdflib term 은 넣지 않는다."""
        return {
            "subject": local(str(self.subject)),
            "axis": self.axis,
            "undeclared_target": local(str(self.target)),
            "target_iri": str(self.target),
        }


def scan_undeclared_schema_references(
    tbox: Graph, ns: str | None = None,
) -> tuple[int, list[UndeclaredSchemaRef]]:
    """(검사한 참조 수, 위반 **전량**) — 상한 없이 돌려준다.

    ``check_schema_reference_integrity`` (게이트) 와
    ``step_24c_undeclared_schema_ref_prune`` (수정 스텝) 이 **이 함수 하나**를 쓴다.
    판정 사본을 만들면 게이트가 잡은 것을 스텝이 못 고치거나, 스텝이 정당한 축을
    지우고 게이트는 침묵하는 형태가 된다 — 이 리포에서 반복된 실패다.

    보고 상한(20건)은 게이트 쪽 관심사이므로 여기서 자르지 않는다: 스텝은 21번째
    위반도 고쳐야 한다.

    정상으로 보는 대상(오탐 방지)은 ``check_schema_reference_integrity`` docstring
    참조 — XSD/OWL/RDFS/RDF, 외래 온톨로지, 명명된 Restriction, blank node.
    """
    from domain.graph_utils import is_anonymous_class_expression
    from domain.namespaces import FOREIGN_PREFIXES

    if ns is None:
        ns = str(DOMAIN_NS)

    # 이 그래프가 "정의한" IRI 전부. 어느 메타클래스로든 선언되면 정의로 본다 —
    # 이 검사의 목적은 타입 정확성이 아니라 **참조 대상의 존재** 확인이다.
    defined: set[URIRef] = set()
    for meta in (OWL.Class, RDFS.Class, OWL.ObjectProperty, OWL.DatatypeProperty,
                 OWL.AnnotationProperty, RDFS.Datatype, OWL.Restriction,
                 OWL.NamedIndividual, RDFS.Resource):
        defined |= {s for s in tbox.subjects(RDF.type, meta) if isinstance(s, URIRef)}

    foreign_ns = tuple(str(v) for v in FOREIGN_PREFIXES.values())
    allowed_ns = _SCHEMA_REF_ALLOWED_NS + foreign_ns

    checked = 0
    out: list[UndeclaredSchemaRef] = []
    for pred, axis in _SCHEMA_REF_PREDICATES:
        for subj, obj in tbox.subject_objects(pred):
            if not (isinstance(subj, URIRef) and isinstance(obj, URIRef)):
                continue                      # blank node = 익명 표현식
            if not str(subj).startswith(ns):
                continue                      # 외래 주어는 이 T-Box 의 책임이 아니다
            checked += 1
            if obj in defined:
                continue
            obj_str = str(obj)
            if obj_str.startswith(allowed_ns):
                continue
            if is_anonymous_class_expression(tbox, obj, ns):
                continue                      # 명명된 Restriction = 공리 노드
            out.append(UndeclaredSchemaRef(subj, pred, obj, axis))
    return checked, out


def check_schema_reference_integrity(
    tbox: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """23. T-Box 스키마 참조 무결성 — 선언되지 않은 IRI 를 가리키는 축 탐지.

    ``rdfs:domain`` / ``rdfs:range`` / ``rdfs:subClassOf`` / ``rdfs:subPropertyOf``
    가 **어디에도 선언되지 않은** 도메인 IRI 를 가리키면 보고한다.

    ## 왜 필요한가 — 어떤 게이트도 이것을 보지 않았다

    실측 (2026-08-24, 배포 T-Box): ``MaintenanceHistory ⊑ MaintenanceActivity`` 인데
    ``MaintenanceActivity`` 는 **어디에도 선언되지 않았다** (``owl:Class`` 아님,
    A-Box 인스턴스 0건). 그런데:

      - ``check_quality_rules`` → critical 0 / high 0 (언급 0건)
      - ``validate_owl_consistency`` (HermiT) → 통과. RDFS 의미론상 미선언 IRI 를
        참조하는 것 자체는 **논리적 오류가 아니다** (암묵적으로 클래스로 취급된다).
      - ``댕글링 참조 탐지`` → A-Box 인스턴스 대상만 훑는다. **스키마 축은 범위 밖.**

    즉 "유령 부모" 가 계층에 조용히 섞인다. 결과로 DIT/NOC 지표가 실재하지 않는
    노드를 세고, 그 부모를 기대한 질의는 영구 0건이 된다 — 에러 없이.

    ``rename_to_unknown`` mutation (``rdfs:range`` 를 존재하지 않는 IRI 로 교체) 이
    **어떤 체크에도 잡히지 않은** 것이 이 갭의 증거다.

    ## 무엇을 정상으로 보는가 (오탐 방지)

      - **XSD/OWL/RDFS/RDF** — 데이터타입·메타 어휘. 도메인이 선언할 것이 아니다.
      - **외래 온톨로지** (``FOREIGN_PREFIXES``) — IOF/BFO 등은 ``owl:imports`` 대상이고
        이 그래프에 선언이 없는 것이 정상이다.
      - **명명된 ``owl:Restriction``** — skolemize 산물로 공리이지 분류 노드가 아니다.
        이 리포는 그 혼동으로 클래스 수가 2.4배 부풀고 baseline 이 FAIL 로 고정된
        이력이 있다 (``graph_utils.is_anonymous_class_expression`` 이 정본 판정).
      - **blank node** — 익명 표현식은 선언 대상이 아니다.

    Returns:
        표준 check dict. ``passed`` 는 위반 0건일 때만 True.
    """
    from domain.graph_utils import is_anonymous_class_expression

    ns = shared.ns if shared is not None else str(DOMAIN_NS)

    # 판정은 ``scan_undeclared_schema_references`` 정본 하나만 쓴다 (사본 금지).
    # ``step_24c`` 도 같은 함수를 쓰므로 게이트와 수정 스텝이 어긋날 수 없다.
    checked, refs = scan_undeclared_schema_references(tbox, ns)

    by_predicate: dict[str, int] = {}
    for ref in refs:
        by_predicate[ref.axis] = by_predicate.get(ref.axis, 0) + 1
    #: 보고는 20건까지만 — 전량이 필요한 소비자는 정본 함수를 직접 부른다.
    violations: list[dict] = [ref.as_dict() for ref in refs[:20]]

    # ── 공리 노드가 분류 계층의 **주어** 가 된 경우 ────────────────────
    # 명명된 ``owl:Restriction`` / ``Union_*`` 은 공리이지 분류 노드가 아니다.
    # 그것이 ``rdfs:subClassOf`` 의 주어로 오면 공리가 계층에 참여한다 — 구조적
    # 오류이고 HermiT 는 논리적 모순이 아니므로 통과한다.
    #
    # 실측 (2026-08-24): ``flip_parent`` mutation 이 ``AirEmissionMonitoring`` 의
    # subClassOf 3건을 역전시켜 Restriction 2개를 부모→자식으로 바꿨는데, 23개 체크
    # 중 어느 것도 잡지 못했다. 순환도 생기지 않아 ``subclass_cycle`` 도 무반응이다.
    # 배포 T-Box 의 baseline 은 **0건** 이므로 오탐 없이 민감하다.
    axiom_as_subject: list[dict] = []
    for subj, obj in tbox.subject_objects(RDFS.subClassOf):
        if not (isinstance(subj, URIRef) and str(subj).startswith(ns)):
            continue
        if not is_anonymous_class_expression(tbox, subj, ns):
            continue
        if len(axiom_as_subject) < 20:
            axiom_as_subject.append({
                "axiom_node": local(str(subj)),
                "declared_parent": local(str(obj)) if isinstance(obj, URIRef) else "<bnode>",
            })

    total = sum(by_predicate.values())
    result: dict[str, object] = {
        "name": "스키마 참조 무결성",
        "passed": total == 0 and not axiom_as_subject,
        "checked_references": checked,
        "undeclared_references": total,
        "by_axis": by_predicate,
        "violations": violations,
    }
    if axiom_as_subject:
        result["axiom_nodes_in_hierarchy"] = axiom_as_subject
        result["axiom_as_subject_count"] = len(axiom_as_subject)
    if axiom_as_subject and not total:
        first = axiom_as_subject[0]
        result["message"] = (
            f"공리 노드가 분류 계층의 주어가 됐다 {len(axiom_as_subject)}건 "
            f"(예: {first['axiom_node']} ⊑ {first['declared_parent']}). "
            "명명된 Restriction/Union 은 공리이지 분류 노드가 아니다 — 계층에 "
            "참여하면 DIT/NOC 가 공리를 클래스로 세고, HermiT 는 논리적 모순이 "
            "아니므로 통과한다."
        )
        logger.warning("check_schema_reference_integrity: %s", result["message"])
    if total:
        result["message"] = (
            f"선언되지 않은 IRI 를 가리키는 스키마 참조 {total}건 "
            f"({', '.join(f'{k} {v}' for k, v in sorted(by_predicate.items()))}). "
            "유령 노드가 계층에 섞이면 DIT/NOC 지표가 실재하지 않는 클래스를 세고, "
            "그 대상을 기대한 질의가 에러 없이 0건을 반환한다. "
            f"예: {violations[0]['subject']} --{violations[0]['axis']}--> "
            f"{violations[0]['undeclared_target']}"
        )
        logger.warning("check_schema_reference_integrity: %s", result["message"])
    return result


def check_existential_participation(
    g: Graph, tbox: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """25. 필수참여 공리 (C ⊑ ∃p.D) 가 A-Box 에서 실제로 충족되는가.

    T-Box 가 ``someValuesFrom`` Restriction 으로 "이 클래스의 모든 개체는 p 로
    무언가를 가리킨다" 고 선언했는데, 개체가 그 관계를 **하나도** 갖지 않는 경우를
    보고한다.

    ## 왜 필요한가 — 기존 24 check 전체가 이것을 보지 않았다

    실측 (2026-08-28, 배포 산출물): 공리 109개 중 **64 (class, OP) 쌍**이
    **53,781 개체**에서 위반이다. 최다는 전량 미보유다::

        RealTimeData              realTimeDataMonitorsSteelmaking     36,000 / 36,000
        EquipmentStatus           equipmentStatusRelatedFailureCause   6,000 /  6,000
        ProcessSteelmakingFurnace steelmakingMonitoredByRealTimeData    4,320 /  4,320

    그런데 어느 게이트도 발화하지 않았다:

      - **HermiT / validate_owl_consistency** → 통과. OWL 은 개방세계(OWA)라
        "관계가 보이지 않는다" 가 "관계가 없다" 를 뜻하지 않는다. 원리적으로
        모순이 아니므로 추론기는 **영구히 침묵**한다.
      - **SHACL (validate_tbox_shacl)** → conforms=true. 동적 shape 이 DP/OP 의
        domain·range·label 만 보고 ``sh:minCount`` 를 만들지 않는다.
      - **domain_range_conformance** → 통과. 존재하는 트리플의 타입만 검사하고
        "있어야 하는데 없는" 트리플은 범위 밖이다.
      - **property_coverage** → OP 가 전역에서 0건인지만 본다. 클래스별 필수참여는
        보지 않는다 (tacit 이 일부를 채우면 전역 카운트는 0 이 아니다).

    ## 왜 조용한 것이 위험한가

    추론이 그 개체들을 **자기가 위반하는 클래스로 타이핑한다**. 실측:
    ``RealTimeData`` 36,000개가 ``realTimeDataMonitorsSteelmaking`` 을 0건 갖는데
    ``RealTimeData_realTimeDataMonitorsSteelmaking_someValuesFrom`` 타입은 36,002건
    붙어 있다. 그 클래스로 필터하는 질의는 "요구 관계를 가진 개체" 를 기대하는데
    실제로는 **0개 가진 개체 전부**를 받는다 — 결과가 조용히 틀리고, 다운스트림
    추론이 그 타입을 근거로 더 파생한다.

    ## 이 체크는 위반을 없애지 않는다

    보이게 만드는 것이 목적이다. 조치는 두 갈래이고 **도메인 판단**이 필요하다:
    OP 배선을 복구할 것인가(FK 근거가 있으면), 공리를 완화할 것인가(근거가 없으면).
    공리를 데이터에 맞춰 지우는 것은 지표 매수다 — 그래서 자동 수정하지 않는다.

    Returns:
        표준 check dict. ``violations`` 는 위반 개체 수 내림차순 최대 20건.
    """
    from domain.graph_utils import is_anonymous_class_expression

    ns = shared.ns if shared is not None else str(DOMAIN_NS)

    # ── 1) T-Box 에서 C ⊑ ∃p.D 수집 ────────────────────────────────
    # subClassOf 와 equivalentClass 두 축 모두 본다 — 정의 클래스는 후자를 쓰고
    # (step_16 이 그렇게 붙인다), 한 축만 보면 그쪽 공리를 통째로 놓친다.
    requirements: list[tuple[URIRef, URIRef, str]] = []
    for axis in (RDFS.subClassOf, OWL.equivalentClass):
        for cls, node in tbox.subject_objects(axis):
            if not isinstance(cls, URIRef) or not str(cls).startswith(ns):
                continue
            if (node, RDF.type, OWL.Restriction) not in tbox:
                continue
            prop = tbox.value(node, OWL.onProperty)
            target = tbox.value(node, OWL.someValuesFrom)
            if prop is None or target is None:
                continue
            if not (isinstance(prop, URIRef) and str(prop).startswith(ns)):
                continue                      # 외래 프로퍼티는 이 T-Box 책임 아님
            # 공리 노드가 주어면 분류 클래스가 아니다 (schema_ref 소관).
            if is_anonymous_class_expression(tbox, cls, ns):
                continue
            requirements.append((cls, prop, local(str(target))))

    result: dict[str, object] = {
        "name": "필수참여 공리 충족",
        "passed": True,
        "axioms_checked": len(requirements),
        "violated_pairs": 0,
        "violating_instances": 0,
        "violations": [],
    }
    if not requirements:
        # 검사 대상이 0건이면 **미판정** 이다 — "위반이 없다" 가 아니다. 예전에는
        # 메시지로만 "vacuous pass" 라고 적고 ``passed: True`` 를 냈는데, 그러면
        # 점수 분자에 들어가고 mutation 감사에서도 PASS→PASS 로 읽혀 **축이 통째로
        # 사라진 것을 아무도 못 본다**.
        #
        # 실측 (2026-09-05, S4.5/S9.5 양쪽이 놓친 유일한 순수 갭):
        #   M7/some_to_all 이 someValuesFrom 40건을 전부 allValuesFrom 으로 바꾼다
        #   → axioms_checked 40 → 0, passed: true. 필수참여 보장이 사라졌는데
        #     25 check 중 어느 것도 변화를 보고하지 않았다.
        #
        # ``applicable: false`` 는 이 리포의 규약이다 (미판정을 FAIL 로도, PASS 로도
        # 접지 않는다). ``validate_kg`` 는 이것을 ``score_measured`` 분모에서 빼고,
        # mutation 감사는 ``N/A`` 로 매핑해 ``blinded_checks`` 로 낸다.
        result["applicable"] = False
        result["reason"] = (
            "someValuesFrom 필수참여 공리가 T-Box 에 0건 — 판정할 대상이 없다. "
            "이 축이 보장하는 것이 아무것도 없으므로 PASS 로 읽지 말 것 "
            "(공리가 있었다가 사라진 것이라면 그 자체가 회귀다)."
        )
        # "vacuous" 표현은 유지한다 — 기존 테스트가 그 용어로 이 상황을 주장하고
        # 있고, ``applicable: false`` 는 그 주장을 **기계가 읽을 수 있게** 강화한
        # 것이지 대체한 것이 아니다.
        result["message"] = (
            "someValuesFrom 필수참여 공리가 없다 — 검사 대상 0건 "
            "(vacuous pass → applicable: false, 미판정)."
        )
        logger.warning("check_existential_participation: %s", result["reason"])
        return result

    # ── 2) A-Box 에서 충족 여부 측정 ───────────────────────────────
    # 추론 그래프에는 상위 클래스 타입이 전파돼 있으므로 rdf:type 직접 조회로
    # 충분하다. 병합 그래프에서도 같은 질문이 성립한다 (선언 타입만 봄).
    rows: list[dict[str, object]] = []
    total_violating = 0
    for cls, prop, target_local in requirements:
        subjects = set(g.subjects(RDF.type, cls))
        if not subjects:
            # 인스턴스 0 클래스는 이 체크 범위 밖 (``class_instance_count`` 소관).
            # 판정상으로는 아래 ``if not missing`` 이 같은 결과를 내므로 이 가드는
            # 조기 종료(성능)다 — mutation 으로 지워도 결과가 같음을 확인했다.
            # 대량 그래프에서 40 클래스 × 전수 순회를 줄이기 위해 남긴다.
            continue
        missing = [s for s in subjects if next(g.objects(s, prop), None) is None]
        if not missing:
            continue
        total_violating += len(missing)
        rows.append({
            "class": local(str(cls)),
            "property": local(str(prop)),
            "expected_type": target_local,
            "missing": len(missing),
            "total": len(subjects),
            "sample": local(str(missing[0])),
        })

    rows.sort(key=lambda r: -int(r["missing"]))
    result["violated_pairs"] = len(rows)
    result["violating_instances"] = total_violating
    result["violations"] = rows[:20]
    result["passed"] = not rows

    if rows:
        worst = rows[0]
        result["message"] = (
            f"필수참여 공리 위반 {len(rows)}쌍 / 개체 {total_violating:,}건 "
            f"(공리 {len(requirements)}개 중). 최다: {worst['class']} 개체 "
            f"{worst['missing']:,}/{worst['total']:,} 이 {worst['property']} 를 "
            f"하나도 갖지 않는다. OWL 개방세계라 HermiT 는 원리적으로 침묵하고 "
            "SHACL 동적 shape 은 sh:minCount 를 만들지 않는다 — 추론은 그 개체들을 "
            "자기가 위반하는 someValuesFrom 클래스로 타이핑하므로 그 클래스로 "
            "필터하는 질의가 조용히 틀린 결과를 낸다. 조치는 OP 배선 복구(FK 근거 "
            "있음) 또는 공리 완화(근거 없음) — 도메인 판단이 필요해 자동 수정하지 않는다."
        )
        logger.warning("check_existential_participation: %s", result["message"])
    return result
