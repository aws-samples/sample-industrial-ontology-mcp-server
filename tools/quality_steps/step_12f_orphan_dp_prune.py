"""Step 12f — 소속·출처가 불명이거나 잉여인 DatatypeProperty 제거.

S2 Architect 가 만든 DP 중 일부는 ``rdfs:domain`` 도 ``dcterms:source`` 도 없다.
2026-07-25 실측 6개 (operationStartDatetime, operationEndDatetime,
facilityOperationCode, scarfingStartDatetime, scarfingEndDatetime, scarfingWeight).

이런 DP 는:

  - **어느 클래스의 속성인지 알 수 없다** — OWL 의미론상 ``owl:Thing`` 과 동등해
    Path B (class-specific DP) 정책 위반이고, ``validate_kg`` 가
    ``class_specific_violations_orphan`` 으로 보고한다
  - **어느 CSV 컬럼에서 왔는지 알 수 없다** — A-Box 가 값을 채울 근거가 없어
    실제로 사용 0건이다
  - 그런데 ``minCardinality`` 제약에서 참조되면 **실데이터로 절대 만족할 수 없는
    필수 조건** 이 된다 (MaterialSpecA·MaterialB 이
    ``operationStartDatetime`` 을 필수로 참조 → 카디널리티 위반)

제거 대상은 세 유형이고, 모두 **A-Box 미사용** 조건을 반드시 통과해야 한다:

  **유형 1 — 소속·출처 불명 (orphan)**
    1. ``rdfs:domain`` 이 없다 (``owl:Thing`` 뿐인 경우 포함)
    2. ``dcterms:source`` 표기가 없다
    3. A-Box 에서 사용되지 않는다

  **유형 2 — union domain 잉여 (redundant union)**
    ``rdfs:domain`` 이 ``owl:unionOf`` 로 여러 클래스를 묶고, 그 각 클래스에
    **같은 컬럼을 가리키는 class-specific DP 가 이미 있으며**, 자신은 A-Box
    미사용인 DP. 실측: ``materialAQaGrade`` (domain = MaterialA ∪ Order, source =
    GRADE_COL_1) 는 ``materialAGRADECOL1`` 13,339건 / ``orderGradeCol1`` 9,788건이
    같은 컬럼을 이미 담고 있어 자신은 0건이다. union domain 은 Path B 위반
    (클래스별 속성이 아님) 이면서 컬럼↔DP 1:1 대응도 깨뜨린다.

  **유형 3 — 출처 없는 미사용 DP (sourceless unused)**
    ``rdfs:domain`` 이 **CSV 에 매핑된 클래스** 이고, ``dcterms:source`` 가 없고,
    그 클래스의 CSV 컬럼이 **전부 다른 DP 로 이미 선언됐고**, A-Box 사용도 0건인
    DP. 네 조건을 모두 요구하는 이유:

      - CSV 매핑이 없는 클래스는 컬럼 목록을 알 수 없어 "담당 컬럼이 없다" 를
        증명할 수 없다. 암묵지(S5) 클래스나 추론 전용 클래스는 CSV 가 없는 것이
        정상이므로, 출처가 없다는 이유로 지우면 도메인 지식이 사라진다.
      - 미선언 컬럼이 남아 있으면 이 DP 가 그 컬럼을 담당할 의도였을 수 있다 →
        제거 대신 ``dcterms:source`` 보강 대상으로 보고한다.

    실측 4개 (2026-07-25) — 모두 같은 값을 담는 class-specific DP 가 이미
    존재했다:

        MaterialSpecA.materialNumber      ↔ materialASpecMtlNo (MATERIAL_NO)
        MaterialSpecA.materialADirectedWeight  ↔ materialASpecWeightCol4 (WEIGHT_COL_4)
        MaterialSpecA.idCol3       ↔ materialASpecIdCol3
        MaterialA.materialAWeight                       ↔ materialAUncondWeight (WEIGHT_COL_1)

    이 4개는 ``minCardinality 1`` 로 선언돼 있어 해당 클래스 전 인스턴스가
    구조적으로 위반 상태였다 (validate_kg 가 ProcessStepA/Order 만 검사해 드러나지
    않았을 뿐). 유형 1 은 domain 까지 없는 경우이고, 유형 3 은 domain 은 있으나
    출처가 없어 **적재 경로가 없는** 경우다.

A-Box 미사용 확인이 공통 안전장치다. 값이 실제로 적재된 DP 는 지우면 데이터가
끊기므로 경고만 남기고 보존한다. A-Box 파일이 없으면(최초 생성 전) 사용 여부를
확인할 수 없으므로 아무것도 지우지 않는다.

환경변수:
  - ``TBOX_PRUNE_ORPHAN_DPS``: ``true`` (default) | ``false``
  - ``TBOX_PRUNE_SOURCELESS_DPS``: ``true`` (default) | ``false`` — 유형 3 만
    개별로 끌 수 있다. 출처 표기를 아직 도입하지 않은 T-Box 에서는 유형 3 이
    광범위하게 걸릴 수 있어 별도 스위치를 둔다.
"""
from __future__ import annotations

import logging
import os

from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")

#: A-Box 사용 여부 스캔 캐시 — ``{(파일서명, 후보집합): used}``.
#: A-Box 는 500MB+ 라 한 번 훑는 데 수 초 걸린다.
_ABOX_USED_CACHE: dict[tuple, set[str]] = {}


def _abox_used_predicates(candidates: set[str]) -> set[str] | None:
    """A-Box 파일을 스캔해 실제 사용된 predicate local name 집합 반환.

    후보만 정규식으로 찾으므로 456MB+ 파일도 한 번의 순차 읽기로 끝난다.
    A-Box 파일이 하나도 없으면 ``None`` — 호출자는 이때 제거를 보류한다.
    """
    from config import ABOX_PATH

    paths = [ABOX_PATH]
    master = os.path.join(os.path.dirname(ABOX_PATH), "master_data.ttl")
    if os.path.exists(master):
        paths.append(master)
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return None
    if not candidates:
        return set()

    # A-Box 는 500MB+ 라 스캔이 수 초 걸린다. 같은 (파일 서명, 후보 집합) 조회는
    # 캐시로 되돌린다 — 한 T-Box 개선 실행에서 재호출되거나 테스트가 같은
    # 프로세스에서 반복 호출할 때 비용이 곱해지는 것을 막는다.
    signature: tuple = tuple(
        (p, os.stat(p).st_mtime, os.stat(p).st_size) for p in existing
    )
    cache_key = (signature, frozenset(candidates))
    if cache_key in _ABOX_USED_CACHE:
        return _ABOX_USED_CACHE[cache_key]

    # Both serialisations must be recognised, and the prefix must come from the
    # deployment config. A hardcoded prefix made this scan silently return 0
    # matches on any other domain, and `prunable = candidates - used` then
    # deleted DPs that were in active use (measured: a `med:` deployment lost
    # every candidate). Full-IRI input (N-Triples, a GraphDB export) defeats a
    # prefix-only pattern the same way even when the prefix does match.
    #
    # The pattern is built by the shared helper so this cannot drift out of sync
    # with the other text-scan site (tools/ontology_quality._load_abox_op_usage,
    # which carried the identical hardcoded-prefix bug until 2026-08-08).
    from domain.graph_utils import domain_predicate_pattern

    pattern = domain_predicate_pattern(candidates)
    if pattern is None:
        # No namespace configured — we cannot tell used from unused, and
        # guessing here deletes live data. Withhold pruning.
        logger.warning(
            "step_12f: 도메인 네임스페이스 설정이 비어 A-Box 사용 판정 불가 — 제거 보류",
        )
        return None
    used: set[str] = set()
    for path in existing:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    used.update(pattern.findall(line))
        except OSError as exc:
            logger.debug("A-Box 스캔 skip (%s): %s", path, exc)
            return None       # 확인 불가 → 보수적으로 제거 보류
        if used == candidates:
            break             # 전부 사용 중이면 더 읽을 필요 없다
    _ABOX_USED_CACHE[cache_key] = used
    return used


def _uncovered_columns_by_class(g: Graph, steel_str: str) -> dict[str, set[str]]:
    """{class: 아직 어떤 DP 도 출처로 주장하지 않은 CSV 컬럼}.

    유형 3 판정의 안전장치. 클래스의 CSV 컬럼이 전부 다른 DP 로 선언돼 있으면
    출처 없는 DP 는 담당 컬럼이 없는 잉여다. 반대로 미선언 컬럼이 남아 있으면
    그 DP 가 해당 컬럼을 담당할 의도였을 수 있어 제거 대신 보강 대상으로 본다.

    CSV·매핑을 읽을 수 없으면 빈 dict — 호출자는 그때 클래스가 목록에 없다는
    이유로 유형 3 제거를 진행한다 (기존 동작 유지).
    """
    import csv

    from config import SOURCE_RAWDATA_DIR
    from tools.abox_generation import _load_table_class_mapping

    try:
        table_class = _load_table_class_mapping()
    except Exception as exc:  # noqa: BLE001 — 측정 실패가 파이프라인을 막지 않는다
        logger.debug("매핑 로드 skip (12f): %s", exc)
        return {}

    declared: dict[str, set[str]] = {}
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        columns = {
            str(src).strip().upper()
            for src in g.objects(dp, _DCTERMS_SOURCE) if str(src).strip()
        }
        if not columns:
            continue
        for domain in g.objects(dp, RDFS.domain):
            if isinstance(domain, URIRef) and str(domain).startswith(steel_str):
                declared.setdefault(str(domain)[len(steel_str):], set()).update(
                    columns,
                )

    uncovered: dict[str, set[str]] = {}
    for table, cls_name in table_class.items():
        path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle), []) or []
        except OSError as exc:
            logger.debug("CSV 헤더 읽기 skip (%s): %s", table, exc)
            continue
        columns = {h.strip().upper() for h in header if h and h.strip()}
        uncovered[cls_name] = columns - declared.get(cls_name, set())
    return uncovered


def _remove_bnode_closure(g: Graph, node, *, depth: int = 0) -> None:
    """참조자가 사라진 BNode 와 그 자손 BNode 트리플을 회수한다.

    ``owl:unionOf`` 는 rdf:List (BNode 사슬) 로 표현되므로 헤드만 지우면 꼬리가
    남는다. 재귀 깊이를 제한해 순환 구조에서도 멈춘다.
    """
    if isinstance(node, URIRef) or depth > 32:
        return
    children = [o for o in g.objects(node, None) if not isinstance(o, URIRef)]
    for triple in list(g.triples((node, None, None))):
        g.remove(triple)
    for child in children:
        if next(g.subjects(None, child), None) is None:
            _remove_bnode_closure(g, child, depth=depth + 1)


def _union_domain_classes(g: Graph, dp: URIRef, steel_str: str) -> set[str]:
    """``rdfs:domain`` 이 ``owl:unionOf`` 인 경우의 멤버 클래스 local name.

    단일 named domain 이거나 union 이 아니면 빈 집합 — 유형 2 후보가 아니다.
    """
    from rdflib.collection import Collection

    members: set[str] = set()
    for domain in g.objects(dp, RDFS.domain):
        if isinstance(domain, URIRef):
            continue                      # named domain — union 아님
        union_list = g.value(domain, OWL.unionOf)
        if union_list is None:
            continue
        try:
            for member in Collection(g, union_list):
                if isinstance(member, URIRef) and str(member).startswith(steel_str):
                    members.add(str(member)[len(steel_str):])
        except Exception as exc:          # noqa: BLE001 — 파이프라인 차단 금지
            logger.debug("unionOf 수집 실패 (%s): %s", dp, exc)
    return members if len(members) >= 2 else set()


def _find_redundant_union_dps(g: Graph, steel_str: str) -> dict[str, dict]:
    """유형 2 후보 — union domain 이고 각 멤버에 같은 컬럼 DP 가 이미 있는 DP.

    Returns:
        ``{dp_local: {"union_members": [...], "covered_by": {cls: [dp,...]}}}``
    """
    # (class, column) → class-specific DP 색인. union DP 자신은 제외해야 하므로
    # named domain 만 센다.
    by_class_column: dict[tuple[str, str], list[str]] = {}
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        columns = {
            str(src).strip().upper()
            for src in g.objects(dp, _DCTERMS_SOURCE) if str(src).strip()
        }
        if not columns:
            continue
        for domain in g.objects(dp, RDFS.domain):
            if not (isinstance(domain, URIRef) and str(domain).startswith(steel_str)):
                continue
            cls_local = str(domain)[len(steel_str):]
            for column in columns:
                by_class_column.setdefault((cls_local, column), []).append(
                    str(dp)[len(steel_str):],
                )

    redundant: dict[str, dict] = {}
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        members = _union_domain_classes(g, dp, steel_str)
        if not members:
            continue
        columns = {
            str(src).strip().upper()
            for src in g.objects(dp, _DCTERMS_SOURCE) if str(src).strip()
        }
        if not columns:
            continue          # 출처가 없으면 대체 여부를 판단할 근거가 없다
        dp_local = str(dp)[len(steel_str):]
        covered_by: dict[str, list[str]] = {}
        for cls_local in members:
            substitutes = [
                other
                for column in columns
                for other in by_class_column.get((cls_local, column), [])
                if other != dp_local
            ]
            if not substitutes:
                covered_by = {}
                break         # 대체 DP 가 없는 멤버가 있으면 지우면 손실
            covered_by[cls_local] = sorted(set(substitutes))
        if covered_by:
            redundant[dp_local] = {
                "union_members": sorted(members),
                "columns": sorted(columns),
                "covered_by": covered_by,
            }
    return redundant


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """소속·출처 불명 또는 union 잉여이고 A-Box 미사용인 DP 를 제거."""
    before = len(g)
    if (os.getenv("TBOX_PRUNE_ORPHAN_DPS") or "true").lower() == "false":
        return StepResult(
            name="step_12f_orphan_dp_prune",
            stats={"skipped": "disabled by TBOX_PRUNE_ORPHAN_DPS"},
            triples_delta=0,
            step_number="12f",
            step_label="orphan_dp_prune",
        )

    steel_str = ctx.domain_ns

    # 유형 1 — domain 없음(또는 owl:Thing 뿐) + 출처 표기 없음
    candidates: set[str] = set()
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        domains = {
            o for o in g.objects(dp, RDFS.domain)
            if isinstance(o, URIRef) and o != OWL.Thing
        }
        if domains:
            continue
        if _union_domain_classes(g, dp, steel_str):
            continue          # union domain 은 유형 2 가 판단한다
        if any(str(s).strip() for s in g.objects(dp, _DCTERMS_SOURCE)):
            continue
        candidates.add(str(dp)[len(steel_str):])

    # 유형 2 — union domain 이지만 멤버별 class-specific DP 가 이미 커버
    redundant_union = _find_redundant_union_dps(g, steel_str)
    candidates |= set(redundant_union)

    # 유형 3 — domain 은 있으나 출처 표기가 없어 적재 경로가 없는 DP
    sourceless: set[str] = set()
    sourceless_skipped_uncovered: dict[str, list[str]] = {}
    if (os.getenv("TBOX_PRUNE_SOURCELESS_DPS") or "true").lower() != "false":
        uncovered = _uncovered_columns_by_class(g, steel_str)
        for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
            if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
                continue
            dp_local = str(dp)[len(steel_str):]
            if dp_local in candidates:
                continue
            named_domains = [
                str(o)[len(steel_str):] for o in g.objects(dp, RDFS.domain)
                if isinstance(o, URIRef) and o != OWL.Thing
                and str(o).startswith(steel_str)
            ]
            if not named_domains:
                continue          # 유형 1·2 가 판단
            if any(str(s).strip() for s in g.objects(dp, _DCTERMS_SOURCE)):
                continue
            # **CSV 에 매핑된 클래스만 판단한다.** 매핑이 없는 클래스는 컬럼
            # 목록을 알 수 없어 "담당 컬럼이 없다" 를 증명할 방법이 없다. 암묵지
            # (S5) 로 추가된 클래스나 추론용 클래스는 CSV 가 없는 것이 정상이며,
            # 이들의 DP 를 출처 없다는 이유로 지우면 도메인 지식이 사라진다.
            if not any(cls in uncovered for cls in named_domains):
                continue
            # 이 클래스의 CSV 컬럼이 전부 다른 DP 로 선언돼 있으면, 출처 없는 이
            # DP 는 어떤 컬럼도 담당하지 않는 잉여다 (실측: 13 테이블 2,570 컬럼
            # 중 미선언 2개뿐 — 둘 다 다른 클래스의 PK). 반대로 미선언 컬럼이
            # 남아 있으면 이 DP 가 그 컬럼을 담당할 의도였을 수 있으므로
            # 지우지 않고 출처 보강 대상으로 보고한다.
            remaining = sorted({
                col for cls in named_domains
                for col in uncovered.get(cls, set())
            })
            if remaining:
                sourceless_skipped_uncovered[dp_local] = remaining[:8]
                continue
            sourceless.add(dp_local)
        candidates |= sourceless

    # 어느 경로로 빠져나가도 호출자가 같은 키를 읽을 수 있게 기본 통계를 준다.
    base_stats = {
        "orphan_dps_pruned": 0,
        "pruned_names": [],
        "pruned_orphan_names": [],
        "pruned_sourceless_names": [],
        "pruned_redundant_union_names": [],
        "sourceless_kept_uncovered_columns": sourceless_skipped_uncovered,
        "removed_restrictions": [],
        "kept_in_use_count": 0,
        "kept_in_use": [],
        "candidates": len(candidates),
    }

    if not candidates:
        return StepResult(
            name="step_12f_orphan_dp_prune",
            stats=base_stats,
            triples_delta=0,
            step_number="12f",
            step_label="orphan_dp_prune",
        )

    # 조건 3 — A-Box 미사용
    used = _abox_used_predicates(candidates)
    if used is None:
        logger.info(
            "Step 12f: A-Box 부재로 사용 여부 확인 불가 — 제거 보류 (후보 %d개)",
            len(candidates),
        )
        return StepResult(
            name="step_12f_orphan_dp_prune",
            stats={
                **base_stats,
                "skipped": "A-Box 없음 — 사용 여부 확인 불가",
                "candidate_names": sorted(candidates),
            },
            triples_delta=0,
            step_number="12f",
            step_label="orphan_dp_prune",
        )

    prunable = sorted(candidates - used)
    kept_in_use = sorted(candidates & used)

    removed_restrictions: list[str] = []
    for dp_local in prunable:
        dp_uri = URIRef(steel_str + dp_local)
        # 이 DP 를 onProperty 로 갖는 restriction 을 함께 제거 — 남기면 참조
        # 대상이 사라진 필수 제약이 되어 더 나쁘다.
        for restriction in list(g.subjects(OWL.onProperty, dp_uri)):
            for triple in list(g.triples((restriction, None, None))):
                g.remove(triple)
            for triple in list(g.triples((None, None, restriction))):
                g.remove(triple)
            if isinstance(restriction, URIRef):
                removed_restrictions.append(str(restriction)[len(steel_str):])
        # union domain 을 표현한 BNode 는 DP 트리플을 지우면 참조자가 사라진다.
        # 남기면 목적지 없는 unionOf 리스트가 T-Box 에 떠돌므로 함께 회수한다.
        union_nodes = [
            o for o in g.objects(dp_uri, RDFS.domain) if not isinstance(o, URIRef)
        ]
        for triple in list(g.triples((dp_uri, None, None))):
            g.remove(triple)
        for triple in list(g.triples((None, None, dp_uri))):
            g.remove(triple)
        for node in union_nodes:
            if next(g.subjects(None, node), None) is None:
                _remove_bnode_closure(g, node)

    prunable_union = [d for d in prunable if d in redundant_union]
    prunable_sourceless = [d for d in prunable if d in sourceless]
    prunable_orphan = [
        d for d in prunable
        if d not in redundant_union and d not in sourceless
    ]

    if prunable_orphan:
        logger.warning(
            "Step 12f: domain·출처 없고 A-Box 미사용인 DP %d개 제거 "
            "(참조 restriction %d개 동반) — %s",
            len(prunable_orphan), len(removed_restrictions), prunable_orphan,
        )
    if prunable_sourceless:
        logger.warning(
            "Step 12f: 출처 표기가 없어 적재 경로가 없고 A-Box 미사용인 DP "
            "%d개 제거 (해당 클래스 CSV 컬럼은 이미 전부 다른 DP 가 담당) — %s",
            len(prunable_sourceless), prunable_sourceless,
        )
    if sourceless_skipped_uncovered:
        logger.warning(
            "Step 12f: 출처 없는 DP %d개는 클래스에 미선언 컬럼이 남아 있어 "
            "제거 보류 — dcterms:source 보강 필요: %s",
            len(sourceless_skipped_uncovered),
            dict(list(sourceless_skipped_uncovered.items())[:5]),
        )
    if prunable_union:
        logger.warning(
            "Step 12f: union domain 잉여 DP %d개 제거 — 각 멤버 클래스에 같은 "
            "컬럼의 class-specific DP 가 이미 있고 자신은 A-Box 미사용: %s",
            len(prunable_union),
            {d: redundant_union[d]["covered_by"] for d in prunable_union},
        )
    if kept_in_use:
        logger.warning(
            "Step 12f: 제거 조건에 걸렸으나 A-Box 에서 사용 중이라 보존한 DP "
            "%d개 — domain/출처 보강 필요: %s",
            len(kept_in_use), kept_in_use,
        )

    return StepResult(
        name="step_12f_orphan_dp_prune",
        stats={
            "orphan_dps_pruned": len(prunable),
            "pruned_names": prunable,
            "pruned_orphan_names": prunable_orphan,
            "pruned_sourceless_names": prunable_sourceless,
            "sourceless_kept_uncovered_columns": sourceless_skipped_uncovered,
            "pruned_redundant_union_names": prunable_union,
            "redundant_union_detail": {
                d: redundant_union[d] for d in prunable_union
            },
            "removed_restrictions": removed_restrictions,
            "kept_in_use_count": len(kept_in_use),
            "kept_in_use": kept_in_use,
            "candidates": len(candidates),
        },
        triples_delta=len(g) - before,
        step_number="12f",
        step_label="orphan_dp_prune",
    )
