"""Step 13 — PK FunctionalProperty + OP someValuesFrom 제약 (Axiom Richness).

본문 ontology_quality.py 의 Step 13 블록을 그대로 모듈로 옮김. 4 sub-helper:

- 13a: PK DatatypeProperty → owl:FunctionalProperty 마킹 + owl:Thing domain
  복구 (CSV PK 인덱스 기반 단일 클래스 매칭)
- 13a-1: domain-incompatible OWL Restriction 제거 (legacy T-Box 정리)
- 13a-2: owl:InverseFunctionalProperty 제거 (reasonable prp-ifp 오작동 회피)
- 13b: 단일 domain/range OP 에 owl:someValuesFrom restriction 추가
  (AllDisjoint / FK NULL 비율 / owl:Thing 가드)
"""
from __future__ import annotations

import logging
import os

import rdflib
from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


_PK_PATTERNS = ("Id", "ID", "Code")


def _mark_pk_functional_props(g: Graph, steel_str: str) -> dict:
    """13a — PK DP 를 owl:FunctionalProperty 로 마킹 + owl:Thing domain 복구."""
    from tools.ontology_quality import _build_csv_pk_map, _dp_matches_domain_pk

    functional_props_added = 0
    functional_props_removed = 0
    dp_thing_domain_restored = 0
    csv_pk_map = _build_csv_pk_map()

    pk_to_classes: dict[str, set[str]] = {}
    for cls_lower, cols in csv_pk_map.items():
        if len(cols) != 1:
            continue
        for c in cols:
            pk_to_classes.setdefault(c, set()).add(cls_lower)
    class_by_name: dict[str, URIRef] = {}
    for cls in g.subjects(RDF.type, OWL.Class):
        if isinstance(cls, URIRef) and str(cls).startswith(steel_str):
            class_by_name[str(cls).split("#")[-1].lower()] = cls

    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(dp).startswith(steel_str):
            continue
        dp_name = str(dp).split("#")[-1]
        if not any(dp_name.endswith(pat) for pat in _PK_PATTERNS):
            continue
        all_domains = list(g.objects(dp, RDFS.domain))
        steel_domains = [
            d for d in all_domains
            if isinstance(d, URIRef) and str(d).startswith(steel_str)
        ]
        if not steel_domains and OWL.Thing in all_domains:
            dp_name_norm = dp_name.replace("_", "").lower()
            candidates = pk_to_classes.get(dp_name_norm, set())
            resolved = {class_by_name[c] for c in candidates if c in class_by_name}
            if len(resolved) == 1:
                chosen = next(iter(resolved))
                g.remove((dp, RDFS.domain, OWL.Thing))
                g.add((dp, RDFS.domain, chosen))
                steel_domains = [chosen]
                dp_thing_domain_restored += 1

        qualifies = (
            len(steel_domains) == 1
            and _dp_matches_domain_pk(dp_name, steel_domains[0], csv_pk_map)
        )
        already_functional = (dp, RDF.type, OWL.FunctionalProperty) in g
        if qualifies and not already_functional:
            g.add((dp, RDF.type, OWL.FunctionalProperty))
            functional_props_added += 1
        elif already_functional and not qualifies:
            g.remove((dp, RDF.type, OWL.FunctionalProperty))
            functional_props_removed += 1
    return {
        "functional_props_added": functional_props_added,
        "functional_props_removed": functional_props_removed,
        "dp_thing_domain_restored": dp_thing_domain_restored,
    }


def _strip_incompatible_restrictions(g: Graph, steel_str: str) -> dict:
    """13a-1 — domain-incompatible OWL Restriction subClassOf 링크 제거."""
    from tools.ontology_quality import (
        _compute_steel_parents_map,
        _is_ancestor_via,
    )

    # subClassOf 전이 폐쇄 — 다중 홉 상속을 인정하기 위해 한 번만 만든다.
    parents = _compute_steel_parents_map(g, steel_str)
    removed = 0
    for cls in list(g.subjects(RDF.type, OWL.Class)):
        if not (isinstance(cls, URIRef) and str(cls).startswith(steel_str)):
            continue
        for parent in list(g.objects(cls, RDFS.subClassOf)):
            if not isinstance(parent, BNode | URIRef):
                continue
            if (parent, RDF.type, OWL.Restriction) not in g:
                continue
            on_prop = next(g.objects(parent, OWL.onProperty), None)
            if not isinstance(on_prop, URIRef):
                continue
            op_domains = [
                d for d in g.objects(on_prop, RDFS.domain)
                if isinstance(d, URIRef) and str(d).startswith(steel_str)
            ]
            if not op_domains:
                continue
            # **전이적** 상속을 인정해야 한다. 예전엔 한 홉만 봤다
            # (`(cls, subClassOf, d) in g`). OWL 의미론상 두 단계 아래 클래스도
            # 프로퍼티를 상속하는데, 그것을 "호환 불가" 로 판정해 정상 restriction 의
            # subClassOf 링크를 지웠다 (2026-08-08 실측: Pump ⊂ RotatingEquipment ⊂
            # Equipment 에서 domain=Equipment 인 OP 의 restriction 이 삭제됨).
            #
            # step_09e / step_12 가 중간 추상 클래스를 만들면 1홉이 2홉으로 바뀌므로,
            # 파이프라인이 스스로 이 상황을 만들어낸다.
            compatible = any(
                d == cls
                or _is_ancestor_via(parents, cls, d)
                or _is_ancestor_via(parents, d, cls)
                for d in op_domains
            )
            if compatible:
                continue
            g.remove((cls, RDFS.subClassOf, parent))
            removed += 1
    return {"incompatible_restrictions_removed": removed}


def _strip_inverse_functional_property(g: Graph) -> dict:
    """13a-2 — owl:InverseFunctionalProperty 제거 (reasonable prp-ifp 오작동 회피)."""
    removed = 0
    for prop in list(g.subjects(RDF.type, OWL.InverseFunctionalProperty)):
        g.remove((prop, RDF.type, OWL.InverseFunctionalProperty))
        removed += 1
    return {"ifp_props_removed": removed}


def _collect_disjoint_groups(g: Graph) -> list[set[str]]:
    """AllDisjointClasses 그룹 멤버 set list 수집."""
    groups: list[set[str]] = []
    for bnode in g.subjects(RDF.type, OWL.AllDisjointClasses):
        members: set[str] = set()
        for member_list in g.objects(bnode, OWL.members):
            try:
                for m in rdflib.collection.Collection(g, member_list):
                    members.add(str(m))
            except Exception as e:
                logger.debug("disjoint_groups collection 파싱 실패: %s", e)
        if members:
            groups.append(members)
    return groups


#: 값이 비었다고 볼 문자열 (대소문자 무시).
_NULLISH = frozenset({"", "n/a", "null", "none", "-", "nan"})

#: 시각 컬럼 판정 토큰 — 이 컬럼은 식별자 후보에서 제외한다 (동시성 ≠ 참조).
_TIME_TOKENS = ("timestamp", "datetime", "date", "time")


def _is_time_column(col: str) -> bool:
    """컬럼명이 시각을 담는가 (부분 문자열 매칭).

    ``Departure_Time`` / ``Order_Date`` / ``Measurement_DateTime`` 등을 포괄한다.
    ``Update_Date`` 같은 감사 컬럼도 함께 걸러지는데, 그것 역시 식별자가 아니므로
    의도한 동작이다.
    """
    norm = col.strip().lower().replace("_", "")
    return any(tok in norm for tok in _TIME_TOKENS)

#: FK 컬럼이 이 비율 미만으로 채워져 있으면 필수참여(∃) 공리를 만들지 않는다.
#: 1.0 = 전수 요구. someValuesFrom 은 "모든 개체가 이 관계를 갖는다" 는 주장이므로
#: 한 행이라도 비면 거짓이다. 부동소수 비교 여유만 둔다.
_REQUIRED_FILL_RATIO = 0.999


def _csv_rows_for_class(class_local: str) -> tuple[list[str], list[list[str]]] | None:
    """클래스의 원본 CSV 를 (헤더, 행) 으로 준다. 매핑이 없으면 None.

    이전 구현은 파일명과 클래스명의 부분 문자열 포함으로 테이블을 찾았다 —
    ``table_class_mapping.json`` 이 정본이므로 그것을 쓴다.
    """
    from config import SOURCE_RAWDATA_DIR

    try:
        from tools.abox_generation import _load_table_class_mapping

        table_class = _load_table_class_mapping()
    except Exception as exc:                  # pragma: no cover — 매핑 부재 방어
        logger.debug("table_class_mapping 로드 실패: %s", exc)
        return None

    tables = [t for t, c in table_class.items() if c == class_local]
    if not tables:
        # 매핑 누락 폴백 — 파일명에서 구분자를 지운 것과 클래스명이 같으면 같은 것으로
        # 본다. 실측 2026-08-28: 40 CSV 중 ``Soil_Monitoring`` 하나가 매핑에 없어
        # ``SoilMonitoring`` 판정이 "CSV 없음" 으로 빠져나갔고, 그러면 근거 검사가
        # 무조건 통과해 게이트가 조용해진다.
        target = class_local.lower()
        try:
            for fn in os.listdir(SOURCE_RAWDATA_DIR):
                if not fn.lower().endswith(".csv"):
                    continue
                stem = fn.rsplit(".", 1)[0]
                if stem.replace("_", "").lower() == target:
                    tables = [stem]
                    break
        except OSError:
            pass
    for table in tables:
        path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(path):
            continue
        try:
            import csv as _csv

            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = _csv.reader(fh)
                header = next(reader, None)
                if not header:
                    continue
                return [h.strip() for h in header], [row for row in reader if row]
        except Exception as exc:              # pragma: no cover
            logger.debug("CSV 읽기 실패 %s: %s", path, exc)
    return None


def _domain_pk_coverage_in_range(
    dom_header: list[str], dom_rows: list[list[str]],
    rng_header: list[str], rng_rows: list[list[str]],
    dom_local: str, rng_local: str,
) -> tuple[bool, float, str]:
    """역축 판정 — domain 의 PK 가 range 테이블에서 **몇 %** 나타나는가.

    range 쪽 PK 를 특정할 수 없을 때(composite PK 테이블) 쓰는 두 번째 축이다.
    ``someValuesFrom`` 은 "domain 의 **모든** 개체가 이 관계를 갖는다" 는 주장이므로,
    domain PK 가 range 테이블에서 14% 만 나타나면 86% 가 거짓이다.

    2026-08-31 실측 — 이 축이 정확히 갈라낸다::

        Equipment_Master.Equipment_ID (unique PK 50/50) 가 나타나는 비율
          Process_Steelmaking_Furnace   7/50 =  14%   → 공리 거짓
          Process_Rolling               7/50 =  14%   → 공리 거짓
          Process_Continuous_Casting    7/50 =  14%   → 공리 거짓
          Process_Blast_Furnace         8/50 =  16%   → 공리 거짓
          Electrical_Consumption       14/50 =  28%   → 공리 거짓
          Equipment_Status             50/50 = 100%   → 공리 **참** (보존해야 한다)

    domain PK 를 못 찾으면 판정하지 않는다 (기존 동작 유지) — 근거 없이 막으면
    정당한 공리를 잃는다. 이 리포의 원칙: 게이트는 "확실히 근거 없는 것만" 막는다.
    """
    total = len(dom_rows)
    # domain 의 PK 후보 — 전 행이 채워지고 값이 전부 distinct 한 컬럼 (PK 의 정의적
    # 성질). 시각 컬럼은 제외한다 (동시성은 참조가 아니다 — 위 target_values 와 동일).
    best: tuple[float, str, str] | None = None
    for idx, col in enumerate(dom_header):
        if _is_time_column(col):
            continue
        vals = [r[idx].strip() for r in dom_rows if idx < len(r) and r[idx].strip()]
        if len(vals) != total or len(set(vals)) != total:
            continue                       # PK 가 아니다
        pk_values = set(vals)
        # range 테이블에서 같은 값을 담는 컬럼을 찾는다 (값 교집합 — 이름 무관).
        for r_idx, r_col in enumerate(rng_header):
            if _is_time_column(r_col):
                continue
            seen = {
                row[r_idx].strip() for row in rng_rows
                if r_idx < len(row) and row[r_idx].strip()
            }
            hit = len(pk_values & seen)
            if not hit:
                continue
            coverage = hit / total
            if best is None or coverage > best[0]:
                best = (coverage, col, r_col)

    if best is None:
        # domain PK 를 못 찾거나 range 어느 컬럼도 그 값을 담지 않는다 — 판정 근거가
        # 없으므로 기존 동작(허용)을 유지한다. 25번째 check 가 사후에 잡는다.
        return True, 1.0, "no_pk_candidate_in_range"

    coverage, dom_col, rng_col = best
    return (
        coverage >= _REQUIRED_FILL_RATIO,
        coverage,
        f"reverse_axis: {dom_local}.{dom_col} appears in "
        f"{rng_local}.{rng_col} for {coverage:.1%} of rows",
    )


def _class_family(g: Graph, cls: URIRef) -> set[URIRef]:
    """``cls`` 와 그 모든 자손 클래스 (subClassOf 폐쇄).

    추상 부모에 걸린 공리는 자손의 개체에도 적용되므로, 커버리지를 재려면 자손까지
    포함해야 한다. 이름 문자열이 아니라 그래프 간선으로 따라간다 (도메인 이식 시
    이름 규칙에 의존하면 조용히 0건이 된다).
    """
    out = {cls}
    frontier = [cls]
    while frontier:
        current = frontier.pop()
        for sub in g.subjects(RDFS.subClassOf, current):
            if isinstance(sub, URIRef) and sub not in out:
                out.add(sub)
                frontier.append(sub)
    return out


def _tacit_coverage_for_op(
    g: Graph, op: URIRef, domain_cls: URIRef,
) -> tuple[int, int]:
    """tacit 에서 ``domain_cls`` (자손 포함) 개체 중 ``op`` 를 가진 비율.

    Returns:
        ``(op 를 가진 개체 수, 타입된 개체 총수)``. tacit 이 그 클래스의 개체를
        하나도 타입하지 않으면 ``(0, 0)`` — 판정 불가이며 호출자가 이전 동작을
        유지한다 ("측정할 수 없다" 를 "위반" 으로 읽지 않는다).
    """
    from domain.tbox_utils import load_tacit_graph

    tacit = load_tacit_graph()
    family = _class_family(g, domain_cls)
    individuals: set = set()
    for cls in family:
        individuals |= set(tacit.subjects(RDF.type, cls))
    if not individuals:
        return 0, 0
    covered = sum(
        1 for ind in individuals
        if next(tacit.objects(ind, op), None) is not None
    )
    return covered, len(individuals)


def _fk_evidence_for_op(
    g: Graph, op: URIRef, domain_cls: URIRef, range_cls: URIRef, steel_str: str,
) -> tuple[bool, float, str]:
    """이 OP 를 채울 CSV 근거가 있는가, 있으면 얼마나 채워져 있는가.

    Returns:
        ``(근거 있음, 채움률, 사유)``. 근거가 없으면 ``(False, 0.0, 이유)``.

    ## 왜 이름 추측을 버렸나 (2026-08-28)

    이전 구현은 FK 컬럼명을 문자열 조작으로 만들었다::

        fk_col = op_local.lower().replace("has","").replace("_","") + "id"
        realTimeDataMonitorsSteelmaking → "realtimedatamonitorssteelmakingid"

    그런 컬럼은 존재하지 않으므로 헤더 검사에서 빠져나가 ``return False`` (= skip
    안 함) 가 됐다. 실측: 배포 T-Box 의 위반 OP 5개 전부 ``skip?=False`` 였고,
    ``hasFailureCause`` 는 ``failurecauseid`` 로 그럴듯하게 추측했지만 실제 컬럼은
    ``Equipment_ID`` 다. 즉 **가드가 한 번도 발화하지 못했다** — 그 결과 필수참여
    공리 109개 중 44쌍이 A-Box 에서 위반이고 개체 53,781건이 영향받았다.

    지금은 두 축을 실측한다:

    1. **근거** — domain CSV 의 어떤 컬럼이 range 클래스의 PK 값 집합과 겹치는가.
       컬럼명이 아니라 **값 교집합**으로 판정하므로 이름 규칙에 의존하지 않는다.
    2. **커버리지** — 그 컬럼이 전수 채워져 있는가. ``someValuesFrom`` 은 "모든
       개체가 이 관계를 갖는다" 는 주장이라 한 행이라도 비면 거짓이다.
    """
    from domain.uri_conventions import local_name as _local_name

    dom_local = _local_name(str(domain_cls))
    rng_local = _local_name(str(range_cls))

    dom_csv = _csv_rows_for_class(dom_local)
    if dom_csv is None:
        # CSV 테이블이 없는 클래스 (추상 부모 / tacit 파생 등). 예전에는 여기서
        # **무조건 (근거 있음, 커버리지 1.0)** 을 돌려줬다 — 근거를 확인할 수 없다는
        # 사실이 "전수 채움" 으로 기록됐다. 2026-09-03 실측으로 그 대가가 드러났다:
        #
        #   ManufacturingProcessStep ⊑ ∃followedBy.ManufacturingProcessStep
        #     → 추론 restriction 위반 34,562건
        #
        # 공정 체인의 **마지막 단계(압연)에는 후행이 없다** — 정의상 거짓인 공리다.
        # tacit 이 4개 공정 노드 중 3개에만 followedBy 를 걸기 때문이다 (실측 3/4).
        #
        # 그래서 CSV 가 없으면 포기하지 않고 **tacit 실측 커버리지**를 2차 축으로
        # 본다 (step_13c 와 같은 원리). 개체가 0이면 판정 근거가 없으므로 이전
        # 동작을 유지한다 — 여기서 막으면 정당한 공리를 대량으로 잃는다는 원래
        # 우려는 유효하다 (실측: 기존 40개 중 이 경로는 2개뿐이고 둘 다 개체 0).
        covered, total = _tacit_coverage_for_op(g, op, domain_cls)
        if total == 0:
            return True, 1.0, "no_csv_for_domain_no_individuals"
        ratio = covered / total
        return True, ratio, (
            f"tacit coverage {covered}/{total} for {dom_local}.{_local_name(str(op))}"
        )
    rng_csv = _csv_rows_for_class(rng_local)
    if rng_csv is None:
        return True, 1.0, "no_csv_for_range"

    dom_header, dom_rows = dom_csv
    rng_header, rng_rows = rng_csv
    if not dom_rows or not rng_rows:
        return True, 1.0, "empty_csv"

    # range 클래스의 식별자 값 집합 — 어느 컬럼이 PK 인지 모르므로 "값이 전부
    # distinct 한 컬럼" 을 후보로 본다 (PK 의 정의적 성질).
    #
    # 시각 컬럼은 제외한다. composite PK 테이블은 단일 distinct 컬럼이 Timestamp
    # 뿐인 경우가 있고, 그러면 **두 테이블이 같은 시각을 기록했다는 사실**이 관계
    # 근거로 오인된다 — 실측 2026-08-28: Real_Time_Data.Timestamp 가
    # Process_Steelmaking_Furnace.Timestamp 와 겹쳐 realTimeDataMonitorsSteelmaking
    # 이 "근거 있음/커버리지 100%" 로 판정됐다. 시각 일치는 동시성이지 참조가 아니다.
    target_values: set[str] = set()
    for idx, col in enumerate(rng_header):
        if _is_time_column(col):
            continue
        vals = [r[idx].strip() for r in rng_rows if idx < len(r) and r[idx].strip()]
        if len(vals) == len(rng_rows) and len(set(vals)) == len(vals):
            target_values |= set(vals)
    if not target_values:
        # composite PK 테이블은 단일 distinct 컬럼이 없다 (실측: Process_* 4개는
        # Product_ID 30/4320, Equipment_ID 7/4320). 그 테이블이 **range 인** 관계는
        # 위 방식으로 참조 대상을 특정할 수 없다.
        #
        # 예전에는 여기서 판정을 포기하고 통과시켰다 ("근거 부재를 단정하려면 반대
        # 방향 컬럼을 봐야 하고 그쪽이 오탐 위험이 크다"). 그런데 **반대 방향은
        # 판정 가능하다** — 2026-08-31 실측:
        #
        #   Equipment_Master.Equipment_ID 는 unique PK (50/50)
        #   Process_Steelmaking_Furnace 가 그것을 FK 로 갖는데 distinct 7 → 커버리지 14%
        #   Process_Rolling 14% / Process_Continuous_Casting 14% / Blast_Furnace 16%
        #   Electrical_Consumption 28%
        #   ↔ 대조: Equipment_Status 는 **100%** (그쪽 필수참여는 참이다)
        #
        # ``someValuesFrom`` 은 "domain 의 **모든** 개체가 이 관계를 갖는다" 는
        # 주장이므로, domain PK 가 range 테이블에서 14% 만 나타나면 86% 가 거짓이다.
        # 판정을 포기하면 그 공리가 생성되고, 추론이 위반 개체를 그 클래스로 타이핑해
        # 질의가 조용히 틀린 결과를 낸다 (실측: 위반 5쌍 / 개체 207건).
        return _domain_pk_coverage_in_range(
            dom_header, dom_rows, rng_header, rng_rows, dom_local, rng_local,
        )

    # domain CSV 에서 그 값들을 가리키는 컬럼을 찾는다 (값 교집합 기준).
    best_ratio, best_col = 0.0, ""
    total = len(dom_rows)
    for idx, col in enumerate(dom_header):
        if _is_time_column(col):
            continue                          # 시각 컬럼은 참조가 아니다 (위와 같은 이유)
        filled = [r[idx].strip() for r in dom_rows
                  if idx < len(r) and r[idx].strip().lower() not in _NULLISH]
        if not filled:
            continue
        hits = sum(1 for v in filled if v in target_values)
        if hits == 0:
            continue
        # 이 컬럼이 range PK 를 가리킨다고 볼 수 있는가 — 채운 값의 대부분이 맞아야.
        if hits / len(filled) < 0.9:
            continue
        ratio = len(filled) / total
        if ratio > best_ratio:
            best_ratio, best_col = ratio, col

    if not best_col:
        return False, 0.0, f"no_column_in_{dom_local}_references_{rng_local}"
    return True, best_ratio, f"column={best_col}"


def _check_fk_null_ratio(
    op_local: str, dom_local: str, threshold: float = 0.20,
    *, g: Graph | None = None, op: URIRef | None = None,
    domain_cls: URIRef | None = None, range_cls: URIRef | None = None,
    steel_str: str = "",
) -> bool:
    """``someValuesFrom`` 을 **skip 해야 하는가** (True = 만들지 않는다).

    그래프 컨텍스트(``g``/``op``/``domain_cls``/``range_cls``)가 주어지면 값 교집합
    기반 실측으로 판정한다 — ``_fk_evidence_for_op`` 참조. 주어지지 않으면 판정
    근거가 없으므로 기존 동작(허용)을 유지한다.

    ``threshold`` 는 하위 호환용으로 남긴다 (NULL 비율 한계) — 내부적으로는
    ``_REQUIRED_FILL_RATIO`` 로 전수 커버리지를 요구한다.
    """
    if g is None or op is None or domain_cls is None or range_cls is None:
        return False

    has_basis, fill_ratio, reason = _fk_evidence_for_op(
        g, op, domain_cls, range_cls, steel_str,
    )
    # 두 사유를 한 판정으로 합친다. 예전엔 ``if not has_basis`` 와
    # ``if fill_ratio < threshold`` 를 따로 뒀는데, 근거가 없으면 fill_ratio 가 0.0
    # 이므로 **두 분기가 같은 일을 했다** — 어느 한쪽을 지워도 결과가 같아 mutation
    # 3종이 생존했다 (2026-08-28). 사유는 로그로만 구분한다.
    if has_basis and fill_ratio >= _REQUIRED_FILL_RATIO:
        return False
    logger.debug(
        "someValuesFrom 스킵 (%s, 커버리지 %.1f%%): %s.%s — %s",
        "CSV 근거 없음" if not has_basis else "FK 부분 채움",
        fill_ratio * 100, dom_local, op_local, reason,
    )
    return True


def _already_restricted(g: Graph, domain_cls: URIRef, op: URIRef) -> bool:
    """이 클래스에 ``op`` 를 대상으로 한 restriction 부모가 **이미** 있는가.

    ## 왜 BNode 요구를 버렸나 (2026-09-04 실측)

    예전 가드는 ``isinstance(parent, BNode)`` 를 요구했다. 그런데 이 스텝보다
    **뒤에** 도는 ``step_19_bnode_skolemize`` 가 restriction BNode 를 전부 명명
    URIRef 로 바꾼다. 그래서 이미 S3 를 한 번 거친 T-Box 에 S3 를 다시 돌리면
    가드가 한 건도 못 알아보고 **같은 공리를 BNode 로 중복 추가**했다:

        배포 T-Box someValuesFrom 40개 (전부 URIRef)
        step_13 재실행 → 36개 추가 (35개가 기존과 동일한 (op, owner) 쌍)

    이 리포는 같은 형태를 이미 겪었다 — ``check_shacl_owl_cardinality_sync`` 가
    restriction 이 BNode 일 것을 요구해 스콜렘화 후 101개 전부 URIRef 라 32개를
    하나도 추출하지 못했다. 노드 종류로 공리를 식별하면 스콜렘화 전후가 달라진다.

    ## 왜 ``someValuesFrom`` 까지 요구하는가 (2026-09-05 정정)

    처음 고칠 때는 판정 조건을 예전과 같이 ``onProperty`` 일치만 두고 BNode 요구만
    빼면 "엄격히 더 억제적이니 안전하다" 고 적었다. **틀렸다.** 이 스텝이 만드는
    것은 ``someValuesFrom`` 인데, 같은 프로퍼티에 걸린 **maxCardinality** restriction
    까지 억제 근거로 세면 종류가 다른 공리를 이유로 생성을 건너뛴다. 스콜렘화된
    T-Box 에서는 그 maxCardinality 가 명명 URIRef 라 예전 가드에는 보이지 않았고,
    BNode 요구를 빼자 갑자기 보이기 시작했다.

    실측 (배포 T-Box, 기존 someValuesFrom 제거 후 재생성)::

        onProperty 일치만            added 13   ← 정당한 존재 공리 22건 손실
        someValuesFrom 까지 요구      added 35   ← 사유별 집계와 일치

    ``tests/test_existential_axiom_guard.py`` 의 고정 수치(≥20)가 이것을 잡았다.
    중복 억제는 **같은 종류의 공리** 로만 판정한다.
    """
    return any(
        (parent, OWL.onProperty, op) in g
        and (parent, OWL.someValuesFrom, None) in g
        for parent in g.objects(domain_cls, RDFS.subClassOf)
    )


def _add_some_values_from_restrictions(g: Graph, steel_str: str) -> dict:
    """13b — 단일 domain/range OP 에 owl:someValuesFrom restriction 추가."""
    from domain.uri_conventions import local_name as _local_name

    disjoint_groups = _collect_disjoint_groups(g)

    def _are_disjoint(cls_a: URIRef, cls_b: URIRef) -> bool:
        a_str, b_str = str(cls_a), str(cls_b)
        return any(a_str in group and b_str in group for group in disjoint_groups)

    added = 0
    skipped = 0
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        if not str(op).startswith(steel_str):
            continue
        op_domains = [
            d for d in g.objects(op, RDFS.domain)
            if isinstance(d, URIRef) and str(d).startswith(steel_str)
        ]
        op_ranges = [
            r for r in g.objects(op, RDFS.range)
            if isinstance(r, URIRef) and str(r).startswith(steel_str)
        ]
        if len(op_domains) != 1 or len(op_ranges) != 1:
            continue
        domain_cls, range_cls = op_domains[0], op_ranges[0]

        if _are_disjoint(domain_cls, range_cls):
            logger.debug(
                "someValuesFrom 스킵 (disjoint 충돌): %s → %s",
                _local_name(str(domain_cls)), _local_name(str(range_cls)),
            )
            continue
        if str(domain_cls) == str(OWL.Thing) or _local_name(str(domain_cls)) == "Thing":
            continue

        op_local = _local_name(str(op))
        dom_local = _local_name(str(domain_cls))
        if _check_fk_null_ratio(
            op_local, dom_local,
            g=g, op=op, domain_cls=domain_cls, range_cls=range_cls,
            steel_str=steel_str,
        ):
            skipped += 1
            continue

        if _already_restricted(g, domain_cls, op):
            continue

        restriction = BNode()
        g.add((restriction, RDF.type, OWL.Restriction))
        g.add((restriction, OWL.onProperty, op))
        g.add((restriction, OWL.someValuesFrom, range_cls))
        g.add((domain_cls, RDFS.subClassOf, restriction))
        added += 1
    return {"restrictions_added": added, "restrictions_skipped_no_basis": skipped}


def _scan_single_valued(props: set) -> tuple[set[str], list[str], str | None]:
    """A-Box 를 **한 번 순차 읽어** DP 별 "주어당 값이 하나인가" 를 판정한다.

    Returns:
        ``(단일값 DP local name 집합, 다중값 DP 목록, 오류 사유 또는 None)``.
        A-Box 에 값이 아예 없는 DP 는 단일값 집합에 넣지 않는다 — 판정 근거가 없다.

    rdflib 파싱을 쓰지 않는 이유는 호출부 주석 참조 (533MB A-Box 실측 사고).
    생성기 출력은 주어 블록이 ``steel-inst:X a steel:C ;`` 로 시작하고 술어가
    ``steel:prop`` 접두형으로 이어지는 Turtle 이므로, 주어 경계를 추적하며 술어
    등장 횟수를 세면 된다.
    """
    import re

    from config import ABOX_PATH

    locals_wanted = {str(p).split("#")[-1] for p in props}
    if not locals_wanted:
        return set(), [], None
    try:
        # 술어는 접두형(``steel:name``) 또는 완전 IRI 로 나타날 수 있다.
        pattern = re.compile(
            r"(?:^|[;\s])(?:[A-Za-z][\w-]*:)?(" + "|".join(
                re.escape(n) for n in sorted(locals_wanted)
            ) + r")\s",
        )
        # 주어 경계: 줄이 공백으로 시작하지 않고 ``.`` 로 끝난 직후 새 주어가 온다.
        # 정확한 Turtle 파싱이 아니라 **주어별 반복 여부** 만 필요하므로, 마지막
        # 트리플 종결(``.``) 을 기준으로 블록을 나눈다.
        multi: set[str] = set()
        seen: set[str] = set()
        with open(ABOX_PATH, encoding="utf-8", errors="replace") as fh:
            block_counts: dict[str, int] = {}
            for line in fh:
                for name in pattern.findall(line):
                    seen.add(name)
                    block_counts[name] = block_counts.get(name, 0) + 1
                    if block_counts[name] > 1:
                        multi.add(name)
                if line.rstrip().endswith("."):
                    block_counts = {}       # 주어 블록 종료
    except OSError as exc:
        return set(), [], f"abox-unreadable: {exc}"
    single = {n for n in seen if n not in multi}
    return single, sorted(multi), None


def _mark_classifying_dps_functional(g: Graph, steel_str: str) -> dict:
    """정의 클래스가 분류 기준으로 쓰는 DP 를 ``owl:FunctionalProperty`` 로 마킹.

    ## 왜 필요한가 (2026-08-27 실측)

    값 기반 서브클래스는 ``owl:equivalentClass [ owl:onProperty :dp ;
    owl:hasValue "V" ]`` 형태로 정의되고, 추론기가 그 값으로 개체를 분류한다. 그런데
    한 개체가 **같은 DP 에 값을 두 개** 가지면 서로 disjoint 인 두 서브클래스에 동시
    배정돼 **unsatisfiable** 이 된다.

    배포 T-Box 실측: 분류 DP **11개 중 FunctionalProperty 가 0개** 였다. A-Box 에서
    다중값은 현재 0/10 이므로 위반이 실현되지는 않았지만, 아무 장치도 그것을 지키지
    않았다 — CSV 가 바뀌거나 tacit 이 값을 추가하면 조용히 unsat 이 된다. 이 리포는
    직교 축 혼재로 6개 클래스가 unsat 이 된 이력이 있다.

    ``FunctionalProperty`` 로 선언하면 ``validate_kg`` 의
    ``check_functional_violations`` 가 다중값을 잡는다 — **잠재 위험이 게이트로 전환**
    된다. 그 게이트는 선언이 없으면 ``"T-Box에 FunctionalProperty 선언 없음"`` 으로
    통과해버린다.

    ## DatatypeProperty 이므로 안전하다

    ``InverseFunctionalProperty`` 는 이 파이프라인에서 금지돼 있다 (`reasonable` 의
    prp-ifp 오작동으로 sameAs 폭발). ``FunctionalProperty`` 는 **DP 에 붙는 한 다르다** —
    최소 예제로 확인했다: 같은 값을 가진 개체 3개에 대해 파생 트리플이 17 → 18 로 늘고
    ``owl:sameAs`` 는 **0건**이다. 값이 아니라 개체를 동일시하는 것은 IFP 쪽이다.

    ## 판정 근거는 실측 단일값이다

    T-Box 에 정의 공리가 있다는 것만으로 선언하지 않는다. **A-Box 에서 이미 개체당
    값이 하나** 임을 확인한 DP 만 마킹한다 — 다중값이 있는 DP 를 Functional 로 만들면
    기존 데이터가 즉시 위반이 되고, 그것은 게이트를 켜는 것이 아니라 데이터를 깨는
    것이다. A-Box 를 못 읽으면 **마킹하지 않는다** (판정 불가는 선언 근거가 아니다).
    """
    from rdflib import Literal as _Literal

    # 정의 클래스가 쓰는 DP 수집 — equivalentClass 축이 분류를 만드는 유일한 형태다.
    classifying: set[URIRef] = set()
    for _, target in g.subject_objects(OWL.equivalentClass):
        if isinstance(target, _Literal):
            continue          # Turtle 소스가 문자열로 저장된 것 — 추론에 무의미
        for prop in g.objects(target, OWL.onProperty):
            if isinstance(prop, URIRef) and str(prop).startswith(steel_str):
                classifying.add(prop)

    if not classifying:
        return {"classifying_dp_functional_marked": 0,
                "classifying_dp_candidates": 0}

    # A-Box 실측으로 단일값을 확인한다.
    #
    # **rdflib 파싱을 쓰지 않는다.** 이 리포는 533MB A-Box 를 파싱하다 step 22 가
    # 사실상 정지한 실측 이력이 있다(수 분 + 수 GB RSS). 필요한 것은 "한 주어가 이 DP 를
    # 두 번 이상 갖는가" 뿐이므로 한 번의 순차 읽기로 판정한다
    # (``_load_abox_op_usage`` 와 같은 방식).
    single, multi_valued, scan_error = _scan_single_valued(classifying)
    if scan_error:
        logger.info(
            "Step 13c: A-Box 를 읽지 못해 분류 DP Functional 마킹을 건너뛴다 — "
            "다중값 여부를 확인하지 않고 선언하면 기존 데이터가 즉시 위반이 된다 (%s)",
            scan_error,
        )
        return {"classifying_dp_functional_marked": 0,
                "classifying_dp_candidates": len(classifying),
                "classifying_dp_skip_reason": scan_error}

    marked = 0
    for prop in sorted(classifying, key=str):
        local = str(prop).split("#")[-1]
        if (prop, RDF.type, OWL.FunctionalProperty) in g:
            continue
        if local not in single:
            continue          # 다중값이거나 A-Box 에 값이 없다 → 선언 근거 없음
        g.add((prop, RDF.type, OWL.FunctionalProperty))
        marked += 1

    if multi_valued:
        logger.warning(
            "Step 13c: 분류 DP %d개가 A-Box 에서 다중값이라 Functional 로 선언하지 "
            "않았다 — 이 DP 로 정의된 서브클래스는 disjoint 위반 위험이 있다: %s",
            len(multi_valued), multi_valued[:5],
        )
    if marked:
        logger.info(
            "Step 13c: 분류 DP %d개를 owl:FunctionalProperty 로 마킹 "
            "(후보 %d개) — validate_kg 의 functional 체크가 다중값을 잡는다",
            marked, len(classifying),
        )
    return {
        "classifying_dp_functional_marked": marked,
        "classifying_dp_candidates": len(classifying),
        # 다중값 DP 는 게이트로 잡을 수 없는 상태다 — 목록을 남겨야 사람이 판단한다.
        "classifying_dp_multivalued": multi_valued[:10],
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    stats: dict = {}
    error: str | None = None
    try:
        steel_str = ctx.domain_ns
        stats.update(_mark_pk_functional_props(g, steel_str))
        stats.update(_mark_classifying_dps_functional(g, steel_str))
        stats.update(_strip_incompatible_restrictions(g, steel_str))
        stats.update(_strip_inverse_functional_property(g))
        stats.update(_add_some_values_from_restrictions(g, steel_str))
    except Exception as e:
        logger.warning("Cardinality/Restriction 추가 실패: %s", e)
        error = str(e)
    return StepResult(
        name="step_13_pk_functional_someValuesFrom",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=13,
        step_label="PK_functional_someValuesFrom",
    )
