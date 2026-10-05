"""Step 11d — 출처 컬럼 집합이 동일한 중복 클래스 병합.

같은 CSV 를 파일명만 달리한 사본으로 두면 (실측: ``Facility_Master_A.csv`` /
``Facility_Master_B.csv`` — md5 동일) Multi-Agent Architect 가 같은
실체에 클래스를 두 벌 만든다. 2026-07-25 실측:

    FacilityEquipmentMaster   DP 9개  ← dcterms:source 집합 {FACILITY_CD, ...}
    ProductionProcessMaster   DP 9개  ← 동일 집합
    + 둘을 잇는 hasFacilityEquipmentMaster / isFacilityEquipmentMasterOf
      ("생산공정 마스터가 설비 마스터를 참조") — 같은 데이터끼리의 참조라
      실데이터로 채울 수 없고 사용 0건으로 남는다

근본 원인(사본 CSV)은 ``tools/common.dedupe_identical_csvs`` 로 S2/S7 양쪽에서
막았다. 이 단계는 **이미 오염된 T-Box** 를 복구하고, 다른 경로로 같은 중복이
생겼을 때의 안전망이다.

판별 기준은 이름 유사도가 아니라 **``dcterms:source`` 컬럼 집합** 이다. 출처
표기는 DP 가 어느 CSV 컬럼에서 왔는지를 단언하므로 같은 테이블을 모델링한
클래스를 결정적으로 찾을 수 있다. 표기가 없는 클래스는 판별 근거가 없어
건드리지 않는다. 두 pass 로 동작한다:

  - **pass 1 — 완전 일치**: 출처 집합이 같은 클래스들을 병합
    (FacilityEquipmentMaster ≡ ProductionProcessMaster)
  - **pass 2 — 스텁 흡수**: 출처 집합이 진부분집합인 스텁 클래스를 구체
    클래스에 흡수 (FacilityOperation {FACILITY_CD} ⊂ FacilityEquipmentMaster
    {FACILITY_CD, OPER_KIND_FLAG, SITE_CODE, ...}). 서로 다른 테이블이 컬럼 하나를
    공유하는 정상 케이스와 구분하기 위해 스텁 쪽 ≤ 2컬럼 / 구체 쪽 ≥ 4컬럼
    조건을 걸고, subClassOf 계층으로 이어진 쌍(정상 설계)은 제외한다.

정본 선택 — 실제 데이터와 관계를 가진 쪽을 살린다:
  1. 인스턴스를 가진 클래스
  2. OP 연결이 많은 클래스 (KG 허브)
  3. ``table_class_mapping.json`` 등록 클래스
  4. 이름 사전순 (결정적)

매핑 등록보다 인스턴스·관계를 앞세우는 이유: 매핑은 "이 테이블은 이 클래스"
지정이지만, 중복 상황에서는 관계 수만 건을 보유한 쪽을 버리면 KG 가 끊긴다
(실측: FacilityEquipmentMaster 는 매핑 등록 + 인스턴스 0 / FacilityOperation 은
미등록 + facilityProcessedMaterial 63,825건).

병합은 삭제가 아니라 URI 치환이다. 삭제만 하면 해당 클래스를 domain/range 로
쓰는 OP 가 ``owl:Thing`` 으로 퇴화한다. 병합 후 자기 자신을 domain·range 로
갖게 된 OP (원래 두 클래스를 잇던 관계) 는 무의미하므로 제거한다.

환경변수:
  - ``TBOX_MERGE_DUPLICATE_SOURCE_CLASSES``: ``true`` (default) | ``false``
"""
from __future__ import annotations

import collections
import logging
import os

from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")
_MIN_SOURCE_COLUMNS = 2  # 1컬럼 일치는 우연일 수 있어 병합 근거로 약하다

# 부분집합 병합(pass 2) 안전 조건 — 아래를 모두 만족할 때만 흡수한다.
#   출처 집합이 {A} ⊂ {A, B, C...} 인 두 클래스는 같은 테이블의 일부만 모델링한
#   것일 수 있으나, 서로 다른 테이블이 한 컬럼을 공유하는 경우와 구분해야 한다.
#   실측 케이스: FacilityOperation {FACILITY_CD} ⊂ FacilityEquipmentMaster
#   {FACILITY_CD, OPER_KIND_FLAG, SITE_CODE, 공정 명칭, ...} — 둘 다 같은 설비 마스터.
_SUBSET_MAX_COLUMNS = 2      # 흡수되는 쪽의 출처 컬럼 수 상한 (스텁 수준만)
_SUBSET_MIN_SUPERSET = 4     # 흡수하는 쪽은 충분히 구체적이어야 함


def _mapped_class_names() -> set[str]:
    """``table_class_mapping.json`` 이 지정한 정식 클래스 local name 집합."""
    from domain.table_mapping import mapped_class_names
    return mapped_class_names()


def _class_to_table() -> dict[str, str]:
    """``{클래스 local name: 원본 테이블명}`` — 매핑의 역방향.

    두 클래스가 **서로 다른 테이블** 로 등록돼 있으면 같은 실체가 아니다. 이것이
    출처 컬럼 집합보다 강한 증거다 (매핑은 SME/DDL 이 단언한 것이고, 컬럼 집합은
    LLM 의 ``dcterms:source`` 표기에 의존한다).
    """
    from domain.table_mapping import load_table_class_mapping
    return {cls: table for table, cls in load_table_class_mapping().items()}


def _is_distinct_source_table(
    class_to_table: dict[str, str], a: str, b: str,
) -> bool:
    """두 클래스가 매핑상 **다른 테이블** 로 등록됐는지.

    pass 2 의 부분집합 판정은 "선언된" 출처 컬럼 집합만 본다. 그런데 모든 DP 가
    ``dcterms:source`` 를 갖는 것은 아니어서 (실측: 출하 T-Box 는 295개 중 0개),
    표기가 부분적이면 컬럼이 많은 테이블도 **1~2컬럼짜리 인공 스텁** 으로 보인다.
    그 상태에서 우연히 공유 컬럼 하나 (``ITEM_CODE``/``PLANT_CODE`` 처럼 ERP 에
    보편적인 것) 가 있으면 서로 무관한 테이블이 부분집합으로 판정된다.

    2026-08-08 실측: 이 리포의 40개 CSV 를 전수 헤더로 보면 해당 쌍이 0개지만,
    표기 커버리지 40% 를 가정하면 12쌍이 조건을 만족하고 그중
    ``Inventory_Status ⊂ Item_Master`` / ``Item_Supplier_Map ⊂ Item_Master`` 처럼
    실제로 별개인 테이블이 병합 대상이 된다. 매핑에 등록된 테이블이 다르면
    무조건 제외한다.
    """
    table_a, table_b = class_to_table.get(a), class_to_table.get(b)
    return bool(table_a and table_b and table_a != table_b)


def _source_columns_by_class(g: Graph, steel_str: str) -> dict[str, frozenset[str]]:
    """{class_local: frozenset(출처 컬럼)} — 표기 없는 클래스는 제외."""
    acc: dict[str, set[str]] = collections.defaultdict(set)
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        columns = {
            str(src).strip().upper()
            for src in g.objects(dp, _DCTERMS_SOURCE)
            if str(src).strip()
        }
        if not columns:
            continue
        for domain in g.objects(dp, RDFS.domain):
            if isinstance(domain, URIRef) and str(domain).startswith(steel_str):
                acc[str(domain)[len(steel_str):]].update(columns)
    return {cls: frozenset(cols) for cls, cols in acc.items()}


def _instance_counts(g: Graph, steel_str: str) -> collections.Counter:
    """클래스별 인스턴스 수 (T-Box 그래프에는 보통 0 — A-Box 병합 시 유효)."""
    counts: collections.Counter = collections.Counter()
    for _s, obj in g.subject_objects(RDF.type):
        if isinstance(obj, URIRef) and str(obj).startswith(steel_str):
            counts[str(obj)[len(steel_str):]] += 1
    return counts


def _op_degree(g: Graph, steel_str: str) -> collections.Counter:
    """클래스별 OP 연결 수 (domain 또는 range 로 등장한 횟수).

    관계를 많이 가진 클래스가 KG 의 실질 허브다. 중복 병합 시 이쪽을 정본으로
    삼아야 기존 연결이 살아남는다 (실측: FacilityOperation 이 63,825건의
    facilityProcessedMaterial/performedAtFacility 를 보유).
    """
    degree: collections.Counter = collections.Counter()
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        for pred in (RDFS.domain, RDFS.range):
            for obj in g.objects(op, pred):
                if isinstance(obj, URIRef) and str(obj).startswith(steel_str):
                    degree[str(obj)[len(steel_str):]] += 1
    return degree


def _pick_canonical(
    candidates: list[str],
    mapped: set[str],
    counts: collections.Counter,
    op_degree: collections.Counter | None = None,
) -> str:
    """정본 클래스 선택.

    우선순위: 인스턴스 보유 > OP 연결 보유 > 매핑 등록 > 사전순.

    인스턴스와 OP 연결을 매핑보다 앞세우는 이유: 매핑은 "이 테이블은 이 클래스"
    라는 지정이지만, 중복이 생긴 상황에서는 **실제 데이터와 관계가 붙어 있는
    쪽을 살려야** 손실이 없다. 매핑이 가리키는 클래스가 인스턴스 0 이고 다른
    쪽이 관계 수만 건을 갖고 있으면 후자가 정본이어야 한다 (실측:
    FacilityEquipmentMaster 는 매핑 등록 + 인스턴스 0 / FacilityOperation 은
    미등록 + 관계 63,825건).
    """
    degree = op_degree or collections.Counter()
    with_instances = sorted(
        (c for c in candidates if counts.get(c, 0) > 0),
        key=lambda c: (-counts[c], c),
    )
    if with_instances:
        return with_instances[0]
    with_relations = sorted(
        (c for c in candidates if degree.get(c, 0) > 0),
        key=lambda c: (-degree[c], c),
    )
    if with_relations:
        return with_relations[0]
    in_mapping = sorted(c for c in candidates if c in mapped)
    if in_mapping:
        return in_mapping[0]
    return sorted(candidates)[0]


def _rename_entity(g: Graph, old: URIRef, new: URIRef) -> int:
    """``old`` 를 언급하는 모든 트리플을 ``new`` 로 재작성."""
    rewritten = 0
    for s, p, o in list(g.triples((old, None, None))):
        g.remove((s, p, o))
        g.add((new, p, o))
        rewritten += 1
    for s, p, o in list(g.triples((None, None, old))):
        g.remove((s, p, o))
        g.add((s, p, new))
        rewritten += 1
    for s, p, o in list(g.triples((None, old, None))):
        g.remove((s, p, o))
        g.add((s, new, o))
        rewritten += 1
    return rewritten


def _drop_self_referential_ops(g: Graph, merged_class: URIRef) -> list[str]:
    """domain 과 range 가 모두 ``merged_class`` 인 OP 를 제거.

    병합 전 두 클래스를 잇던 관계는 병합 후 자기 자신을 가리키게 되어 의미가
    없다. 단, 명시적으로 재귀 관계로 설계된 OP (예: followedBy) 는
    ``owl:TransitiveProperty`` / ``owl:SymmetricProperty`` 로 선언되므로 보존한다.
    """
    dropped: list[str] = []
    for op in list(g.subjects(RDF.type, OWL.ObjectProperty)):
        domains = set(g.objects(op, RDFS.domain))
        ranges = set(g.objects(op, RDFS.range))
        if domains != {merged_class} or ranges != {merged_class}:
            continue
        if (op, RDF.type, OWL.TransitiveProperty) in g:
            continue
        if (op, RDF.type, OWL.SymmetricProperty) in g:
            continue
        for triple in list(g.triples((op, None, None))):
            g.remove(triple)
        for triple in list(g.triples((None, None, op))):
            g.remove(triple)
        dropped.append(str(op).split("#")[-1])
    return dropped


def _is_related_in_hierarchy(
    g: Graph, steel_str: str, a: str, b: str, *, max_depth: int = 6,
) -> bool:
    """``a`` 와 ``b`` 가 subClassOf 계층으로 이어져 있는지 (양방향).

    부모-자식 관계는 의도된 설계다 (예: MaterialB ⊂ MaterialA — 자식이 부모의 출처
    컬럼 일부만 재선언할 수 있다). 이런 쌍을 병합하면 계층이 붕괴하므로
    부분집합 병합에서 제외한다.
    """
    def _ancestors(start: str) -> set[str]:
        seen: set[str] = set()
        frontier = [start]
        for _ in range(max_depth):
            nxt: list[str] = []
            for node in frontier:
                for parent in g.objects(URIRef(steel_str + node), RDFS.subClassOf):
                    if not (isinstance(parent, URIRef)
                            and str(parent).startswith(steel_str)):
                        continue
                    local = str(parent)[len(steel_str):]
                    if local not in seen:
                        seen.add(local)
                        nxt.append(local)
            if not nxt:
                break
            frontier = nxt
        return seen

    return b in _ancestors(a) or a in _ancestors(b)


def _merge_dps_by_source(
    g: Graph, steel_str: str, class_uri: URIRef, class_local: str,
) -> tuple[int, dict[str, str]]:
    """한 클래스 안에서 같은 출처 컬럼을 가리키는 DP 들을 하나로 병합.

    클래스 병합 후 두 벌의 DP 가 공존하는 상황을 정리한다. 정본은 정본 클래스의
    camelCase 접두를 가진 DP (``facilityEquipmentMaster*``); 없으면 이름 사전순.

    Returns:
        ``(재작성 트리플 수, {제거된 DP: 정본 DP})``
    """
    by_column: dict[str, list[str]] = collections.defaultdict(list)
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        if class_uri not in set(g.objects(dp, RDFS.domain)):
            continue
        for src in g.objects(dp, _DCTERMS_SOURCE):
            column = str(src).strip().upper()
            if column:
                by_column[column].append(str(dp)[len(steel_str):])

    class_prefix = (class_local[:1].lower() + class_local[1:]).lower()
    rewritten = 0
    dp_map: dict[str, str] = {}
    for _column, dps in by_column.items():
        if len(dps) < 2:
            continue
        preferred = sorted(d for d in dps if d.lower().startswith(class_prefix))
        canonical_dp = preferred[0] if preferred else sorted(dps)[0]
        for duplicate in dps:
            if duplicate == canonical_dp:
                continue
            rewritten += _rename_entity(
                g, URIRef(steel_str + duplicate), URIRef(steel_str + canonical_dp),
            )
            dp_map[duplicate] = canonical_dp
    return rewritten, dp_map


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """출처 컬럼 집합이 동일한 클래스들을 정본 하나로 병합."""
    before = len(g)
    if (os.getenv("TBOX_MERGE_DUPLICATE_SOURCE_CLASSES") or "true").lower() == "false":
        return StepResult(
            name="step_11d_duplicate_source_class_merge",
            stats={"skipped": "disabled by TBOX_MERGE_DUPLICATE_SOURCE_CLASSES"},
            triples_delta=0,
            step_number="11d",
            step_label="duplicate_source_class_merge",
        )

    steel_str = ctx.domain_ns
    mapped = _mapped_class_names()
    counts = _instance_counts(g, steel_str)
    degree = _op_degree(g, steel_str)

    merge_map: dict[str, str] = {}
    triples_rewritten = 0
    dropped_ops: list[str] = []
    dp_merge_map: dict[str, str] = {}

    def _merge_group(candidates: list[str], canonical: str | None = None) -> None:
        """후보 클래스들을 정본 하나로 합치고 부산물까지 정리.

        ``canonical`` 을 명시하면 그 클래스를 정본으로 쓴다 (스텁 흡수처럼
        방향이 이미 정해진 경우). 생략하면 ``_pick_canonical`` 이 고른다.
        """
        nonlocal triples_rewritten
        if canonical is None:
            canonical = _pick_canonical(candidates, mapped, counts, degree)
        canonical_uri = URIRef(steel_str + canonical)
        for duplicate in candidates:
            if duplicate == canonical:
                continue
            triples_rewritten += _rename_entity(
                g, URIRef(steel_str + duplicate), canonical_uri,
            )
            merge_map[duplicate] = canonical
        dropped_ops.extend(_drop_self_referential_ops(g, canonical_uri))
        # 클래스만 합치면 DP 가 두 벌 남는다 (facilityEquipmentMasterFacOpCode 와
        # productionProcessMasterFacOpCode 가 같은 컬럼을 가리킨 채 공존).
        # step_23 은 라벨 기준이라 이를 잡지 못하므로 여기서 출처 기준으로 통합.
        dp_rewritten, dp_map = _merge_dps_by_source(
            g, steel_str, canonical_uri, canonical,
        )
        triples_rewritten += dp_rewritten
        dp_merge_map.update(dp_map)

    # ── pass 1: 출처 컬럼 집합이 **완전히 동일** 한 클래스들 ────────────
    #
    # 여기서도 매핑이 다른 테이블로 단언한 클래스는 제외한다. 출처 표기가 부분적일
    # 때 (실측: 출하 T-Box 는 DP 295개 중 표기 0개) 서로 다른 테이블의 선언 집합이
    # 우연히 같아질 수 있다 — 그러면 별개 테이블이 하나로 합쳐진다.
    class_to_table_p1 = _class_to_table()
    by_source = _source_columns_by_class(g, steel_str)
    groups: dict[frozenset[str], list[str]] = collections.defaultdict(list)
    for cls, columns in by_source.items():
        if len(columns) >= _MIN_SOURCE_COLUMNS:
            groups[columns].append(cls)
    skipped_distinct_p1: list[str] = []
    for _columns, candidates in groups.items():
        if len(candidates) < 2:
            continue
        # 같은 테이블(또는 미등록)끼리만 묶는다. 등록된 테이블이 여러 개면 각
        # 테이블의 클래스는 서로 병합 대상이 아니다.
        by_table: dict[str, list[str]] = collections.defaultdict(list)
        for cls in candidates:
            by_table[class_to_table_p1.get(cls, "")].append(cls)
        if len(by_table) > 1:
            skipped_distinct_p1.append(
                "|".join(f"{t or '(unmapped)'}:{','.join(sorted(cs))}"
                         for t, cs in sorted(by_table.items())),
            )
        for _table, group in by_table.items():
            if len(group) >= 2:
                _merge_group(group)

    # ── pass 2: 스텁 클래스를 구체 클래스에 흡수 (부분집합) ─────────────
    # 같은 테이블을 한쪽은 스텁(컬럼 1~2개)으로, 다른 쪽은 전 컬럼으로 모델링한
    # 경우 (실측: FacilityOperation {FACILITY_CD} vs FacilityEquipmentMaster
    # {FACILITY_CD, OPER_KIND_FLAG, SITE_CODE, ...}). 서로 다른 테이블이 컬럼 하나를
    # 공유하는 정상 케이스와 구분하기 위해 조건을 좁힌다:
    #   - 흡수되는 쪽 출처 컬럼 ≤ _SUBSET_MAX_COLUMNS (스텁 수준)
    #   - 흡수하는 쪽 출처 컬럼 ≥ _SUBSET_MIN_SUPERSET (충분히 구체적)
    #   - 진부분집합 (같으면 pass 1 이 이미 처리)
    #   - 흡수되는 쪽이 흡수하는 쪽의 조상/자손이 아님 (계층 관계는 정상 설계)
    by_source = _source_columns_by_class(g, steel_str)   # pass 1 결과 반영
    class_to_table = _class_to_table()
    subset_pairs: list[tuple[str, str]] = []
    skipped_distinct_tables: list[str] = []
    for stub, stub_cols in by_source.items():
        if not 0 < len(stub_cols) <= _SUBSET_MAX_COLUMNS:
            continue
        for concrete, concrete_cols in by_source.items():
            if concrete == stub or len(concrete_cols) < _SUBSET_MIN_SUPERSET:
                continue
            if not stub_cols < concrete_cols:
                continue
            if _is_related_in_hierarchy(g, steel_str, stub, concrete):
                continue
            if _is_distinct_source_table(class_to_table, stub, concrete):
                # 매핑이 서로 다른 테이블로 단언 — 컬럼 부분집합은 표기 누락에
                # 따른 우연이다 (docstring 참조). 병합하면 실 테이블이 소멸한다.
                skipped_distinct_tables.append(f"{stub} ⊄ {concrete}")
                continue
            subset_pairs.append((stub, concrete))
            break
    # 스텁 흡수의 정본은 **관계·인스턴스를 가진 쪽** 으로 고정한다. 컬럼 수가
    # 많다는 이유로 구체 클래스를 정본으로 삼으면, 스텁이 KG 허브였던 경우
    # 수만 건의 OP 를 잃는다 (실측: FacilityOperation 이 스텁이지만
    # facilityProcessedMaterial 63,825건 보유 — 이쪽을 살려야 한다). DP 는 어느
    # 쪽이 정본이어도 _merge_dps_by_source 가 출처 기준으로 통합하므로 손실이
    # 없다.
    resolved_subsets: list[str] = []
    for stub, concrete in subset_pairs:
        if stub in merge_map or concrete in merge_map:
            continue  # 이미 다른 그룹에서 처리됨
        canonical = _pick_canonical([stub, concrete], mapped, counts, degree)
        if counts.get(stub, 0) == 0 and counts.get(concrete, 0) == 0 \
                and degree.get(stub, 0) == degree.get(concrete, 0):
            # 인스턴스도 없고 OP 연결 수도 동률 — T-Box 단독 실행에서 흔하다.
            # 이때는 이름 사전순(=_pick_canonical 폴백) 대신 **구체 클래스**를
            # 정본으로 삼는다: DP 가 많은 쪽이 테이블을 온전히 표현한다.
            canonical = concrete
        _merge_group([stub, concrete], canonical=canonical)
        absorbed = stub if canonical == concrete else concrete
        resolved_subsets.append(f"{absorbed} → {canonical}")

    # 병합 부산물 정리 — 자기 자신을 상위로 갖는 subClassOf.
    self_loops = 0
    for subject, obj in list(g.subject_objects(RDFS.subClassOf)):
        if subject == obj:
            g.remove((subject, RDFS.subClassOf, obj))
            self_loops += 1

    # 병합 부산물 정리 2 — 상충하는 단일값 주석. 두 클래스가 서로 다른 OntoClean
    # 메타(identity "+I" vs "-I") 를 갖고 있었다면 병합 후 한 클래스가 두 값을
    # 동시에 갖게 되어 OntoClean 검증이 오작동한다 (실측: FacilityOperation
    # identity ['+I','-I'], ProcessResultD dependence ['+D','-D']).
    from tools.validation_support.common import resolve_single_valued_annotations
    annotation_conflicts = resolve_single_valued_annotations(
        g, {URIRef(steel_str + c) for c in merge_map.values()},
    )

    if merge_map:
        logger.warning(
            "Step 11d: 출처 기준 중복 클래스 %d개 병합 (스텁 흡수 %d건 포함, "
            "DP %d개 통합, 자기참조 OP %d개 제거, 트리플 %d개 재작성, "
            "self-loop %d개) — %s",
            len(merge_map), len(subset_pairs), len(dp_merge_map),
            len(dropped_ops), triples_rewritten, self_loops, merge_map,
        )

    return StepResult(
        name="step_11d_duplicate_source_class_merge",
        stats={
            "duplicate_classes_merged": len(merge_map),
            "merge_map": merge_map,
            "stub_absorptions": resolved_subsets,
            # 매핑이 다른 테이블로 단언해 병합을 보류한 쌍 — 0 이 아니면 출처 표기
            # 커버리지가 낮아 컬럼 집합이 신뢰할 수 없다는 신호다 (감사 추적용).
            "skipped_distinct_tables": skipped_distinct_tables[:20],
            "skipped_distinct_tables_pass1": skipped_distinct_p1[:20],
            "duplicate_dps_merged": len(dp_merge_map),
            "dp_merge_sample": dict(list(dp_merge_map.items())[:10]),
            "self_referential_ops_dropped": dropped_ops,
            "triples_rewritten": triples_rewritten,
            "subclass_self_loops_removed": self_loops,
            "annotation_conflicts_resolved": annotation_conflicts,
        },
        triples_delta=len(g) - before,
        step_number="11d",
        step_label="duplicate_source_class_merge",
    )
