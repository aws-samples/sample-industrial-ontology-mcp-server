"""Step 11c — 테이블명 클래스를 정식 도메인 클래스로 병합.

CQ 의 ``domains`` 는 원본 CSV 테이블명을 담는데, Multi-Agent 협업의 일부 경로가
밑줄만 제거해 클래스명으로 사용하면 ``SOURCE_TABLE_007`` 이
``TBSOURCETABLE007202601010000`` 클래스로 T-Box 에 실체화된다. 이는
``rules/domain/table_class_mapping.json`` 이 이미 ``ProcessResultD`` 로 지정한 것과
**같은 실체를 가리키는 중복 클래스** 이므로:

  - 도메인 계층에 기계적 이름이 노출돼 SME 검토·시각화·NL→SPARQL 를 오염시킨다
  - A-Box 는 매핑에 따라 정식 클래스로만 인스턴스를 만들므로 중복 클래스는
    인스턴스 0건으로 남고, 이를 domain/range 로 쓰는 OP 도 함께 죽는다

이 단계는 **삭제가 아니라 병합** 한다: 테이블명 클래스에 걸린 모든 트리플의
주어/목적어를 정식 클래스로 치환한 뒤 중복 선언을 제거한다. 삭제만 하면 해당
클래스를 참조하는 OP (실측 35개) 가 domain/range 를 잃고 ``owl:Thing`` 으로
퇴화한다.

병합에 이어 **이름에 테이블명을 품은 파생 엔티티** 도 개명한다. Step 19
(skolemization) 가 restriction URI 를 ``{클래스}_{프로퍼티}_{타입}`` 으로
조립하므로, 오염된 클래스명이 스콜렘 이름에 굳으면
(``TBSOURCETABLE007202601010000_isOrderedBy_someValuesFrom``) 클래스 병합만으로는
남는다. 이 스캔은 병합 발생 여부와 무관하게 실행되므로 이전 실행에서 병합만
이뤄진 T-Box 도 복구된다.

근본 원인은 ``tools/multi_agent_tbox.py`` 의 도메인 해석을
``_resolve_domain_to_class`` 로 통일해 수정했다 (2026-07-25). 이 단계는 이미
오염된 T-Box 를 복구하고, 다른 경로로 같은 오염이 재발할 때의 안전망이다.

식별 기준: ``table_class_mapping.json`` 의 테이블명에서 밑줄을 제거한 문자열과
정확히 일치하는 클래스. 휴리스틱(정규식)이 아니라 매핑 대조이므로
``TBM``/``TBD`` 로 시작하는 정상 도메인 클래스를 오검출하지 않는다.
"""
from __future__ import annotations

import logging
import re

from rdflib import RDF, Graph, URIRef
from rdflib.namespace import OWL

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def _load_table_name_aliases() -> dict[str, str]:
    """Return {mangled_table_name: CanonicalClassName} from the mapping file.

    Mangled form = table name with underscores stripped, matching what the
    buggy code paths produced. Empty dict when the mapping is unavailable
    (domain-neutral no-op).
    """
    from domain.table_mapping import load_table_class_mapping

    aliases: dict[str, str] = {}
    for table, canonical in load_table_class_mapping().items():
        mangled = table.replace("_", "")
        if not mangled or mangled == canonical:
            continue
        aliases[mangled] = canonical
        # 날짜 접미(추출 타임스탬프)를 떼어낸 축약형도 등록. LLM 은 긴 테이블명을
        # 줄이려 ``SOURCE_TABLE_007`` 을
        # ``TBSOURCETABLE007`` 으로 축약해 쓰기도 한다 (실측 4건). 접미가 없으면
        # 축약형이 원형과 같아지므로 추가하지 않는다.
        trimmed = re.sub(r"\d{8,}$", "", mangled)
        if trimmed and trimmed != mangled and trimmed != canonical:
            aliases.setdefault(trimmed, canonical)
    # 긴 alias 를 먼저 치환해야 짧은 축약형이 긴 이름의 일부를 잘라먹지 않는다
    # (TBSOURCETABLE007 이 TBSOURCETABLE007202601010000 보다 먼저 매칭되면
    # 뒤에 날짜만 남는다).
    return dict(sorted(aliases.items(), key=lambda kv: -len(kv[0])))


def _rename_entity(g: Graph, old: URIRef, new: URIRef) -> int:
    """Rewrite every triple mentioning ``old`` to use ``new``. Returns count."""
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


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """Merge table-name classes into their canonical domain classes."""
    before = len(g)
    steel_str = ctx.domain_ns
    aliases = _load_table_name_aliases()

    merged: dict[str, str] = {}
    triples_rewritten = 0
    skipped_missing_target: list[str] = []

    for mangled, canonical in aliases.items():
        old_uri = URIRef(steel_str + mangled)
        if (old_uri, RDF.type, OWL.Class) not in g:
            continue
        new_uri = URIRef(steel_str + canonical)
        if (new_uri, RDF.type, OWL.Class) not in g:
            # 정식 클래스가 없으면 병합 대상이 없다 — 이름만 바꾸면 오히려
            # 계층에서 고립되므로 건드리지 않고 보고만 한다.
            skipped_missing_target.append(mangled)
            continue
        triples_rewritten += _rename_entity(g, old_uri, new_uri)
        merged[mangled] = canonical

    # 병합된 이름을 **local name 안에 품고 있는** 파생 엔티티도 함께 개명한다.
    # Step 19 (skolemization) 는 restriction URI 를
    # ``{클래스}_{프로퍼티}_{타입}`` 으로 조립하므로, 오염된 클래스명이 이미
    # 스콜렘 이름에 굳어 있으면 (``TBSOURCETABLE007..._isOrderedBy_someValuesFrom``)
    # 클래스 병합만으로는 남는다. OP/DP 이름에 테이블명이 섞인 경우도 동일.
    # 11c 가 19 보다 먼저 실행되므로 신규 생성에는 이 경로가 필요 없지만,
    # 이미 오염된 T-Box 를 복구할 때와 다른 순서로 호출될 때의 안전망이다.
    #
    # 클래스 병합이 이번 실행에서 일어나지 않았어도 (이전 실행이 이미 병합했거나
    # 다른 경로로 정리된 경우) 이름에 남은 테이블명은 청소해야 하므로, 병합
    # 여부와 무관하게 alias 전체를 대상으로 스캔한다.
    derived_renamed: dict[str, str] = {}
    canonical_names = set(aliases.values())
    for entity in (
        {s for s in g.subjects() if isinstance(s, URIRef)}
        | {o for o in g.objects() if isinstance(o, URIRef)}
        | {p for p in g.predicates() if isinstance(p, URIRef)}
    ):
        text = str(entity)
        if not text.startswith(steel_str):
            continue
        local = text[len(steel_str):]
        if local in aliases or local in canonical_names:
            continue  # 클래스 자체는 위 병합 루프가 처리
        new_local = local
        for mangled, canonical in aliases.items():
            if mangled in new_local:
                new_local = new_local.replace(mangled, canonical)
        if new_local == local:
            continue
        triples_rewritten += _rename_entity(
            g, entity, URIRef(steel_str + new_local),
        )
        derived_renamed[local] = new_local

    # 병합 후 자기 참조 subClassOf 정리 (A→B 병합 시 B rdfs:subClassOf B 발생).
    from rdflib.namespace import RDFS
    self_loops = 0
    for s, o in list(g.subject_objects(RDFS.subClassOf)):
        if s == o:
            g.remove((s, RDFS.subClassOf, o))
            self_loops += 1

    # 병합으로 상충하게 된 단일값 주석 정리 (OntoClean identity "+I" vs "-I" 등).
    # 두 클래스가 서로 다른 메타를 갖고 있었다면 병합 후 한 클래스가 두 값을
    # 동시에 갖게 되어 검증이 오작동한다 (실측: ProcessResultD dependence
    # ['+D','-D']).
    from tools.validation_support.common import resolve_single_valued_annotations
    annotation_conflicts = resolve_single_valued_annotations(
        g, {URIRef(steel_str + c) for c in merged.values()},
    )

    if merged or derived_renamed:
        logger.warning(
            "Step 11c: 테이블명 클래스 %d개 병합 / 파생 엔티티 %d개 개명 "
            "(트리플 %d개 재작성, self-loop %d개 제거) — %s",
            len(merged), len(derived_renamed), triples_rewritten, self_loops,
            list(merged.items())[:5] or list(derived_renamed.items())[:3],
        )
    if skipped_missing_target:
        logger.warning(
            "Step 11c: 정식 클래스가 없어 병합 보류 %d개 — %s",
            len(skipped_missing_target), skipped_missing_target[:5],
        )

    return StepResult(
        name="step_11c_table_name_class_merge",
        stats={
            "table_name_classes_merged": len(merged),
            "merge_map": merged,
            "derived_entities_renamed": len(derived_renamed),
            "derived_rename_sample": dict(list(derived_renamed.items())[:10]),
            "triples_rewritten": triples_rewritten,
            "subclass_self_loops_removed": self_loops,
            "annotation_conflicts_resolved": annotation_conflicts,
            "skipped_missing_target": skipped_missing_target,
        },
        triples_delta=len(g) - before,
        step_number="11c",
        step_label="table_name_class_merge",
    )
