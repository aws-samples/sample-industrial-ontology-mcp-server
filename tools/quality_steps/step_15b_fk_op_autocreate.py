"""Step 15b — FK-OP 자동 생성 (Multi-Agent/Jury 미생성 OP 보강).

본문 ontology_quality.py 의 Step 15b 블록을 그대로 모듈로 옮김. 3 sub-helper:

- 15b-1: self-loop FK OP 제거 (domain == range, 이름 ``has<Range>``)
- 15c: incompatible inverseOf cleanup — best-score 1건만 유지
- 15b-2: FK-OP 자동 생성 — fk_patterns.json + CSV 헤더 스캔

본문 위치를 보존하기 위해 inline 호출 (POST_STEPS 가 아닌 본문 위치).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re

import rdflib
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.rules_paths import rules_path
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: ``dcterms:source`` — OP 의 근거(CSV FK 컬럼)를 각인하는 술어.
_DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")


def _record_fk_source(g: Graph, prop_uri: URIRef, fk_header: str) -> None:
    """OP 에 FK 컬럼 근거를 기록한다. 이미 있으면 덮어쓰지 않는다.

    값은 ``clean_source_column`` 으로 정규화한다 (사본 금지) — A-Box 는 이 값을
    CSV 헤더와 직접 비교하므로 ``"Event_ID"^^xsd:string`` 같은 직렬화 표기가
    들어가면 **어떤 헤더와도 매칭되지 않고** 조용히 폴백으로 떨어진다.
    """
    if not fk_header:
        return
    if next(g.objects(prop_uri, _DCTERMS_SOURCE), None) is not None:
        return          # 기존 근거(SME/수동/이전 스텝) 보존
    from domain.graph_utils import clean_source_column

    cleaned = clean_source_column(fk_header)
    if cleaned:
        g.add((prop_uri, _DCTERMS_SOURCE, Literal(cleaned)))


def _remove_self_loop_fk_ops(g: Graph, steel_str: str) -> dict:
    """15b-1 — self-loop FK OP (domain == range, ``has<Range>``) 제거.

    이전 R6 run 이나 Multi-Agent 가 잘못 만든 무의미한 OP
    (hasSupplierMaster domain=SupplierMaster range=SupplierMaster) 가 있으면
    FK-OP Gap 은 충족되지만 실제 A-Box 매칭이 일어날 수 없어 CW master 고립
    100% 를 유발. inverse OP 도 함께 제거.
    """
    from tools.ontology_quality import DOMAIN_NS_OBJ

    self_loop_ops_removed = 0
    try:
        for op in list(g.subjects(RDF.type, OWL.ObjectProperty)):
            if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
                continue
            op_name = str(op).split("#")[-1]
            if not op_name.startswith("has"):
                continue
            doms = list(g.objects(op, RDFS.domain))
            rngs = list(g.objects(op, RDFS.range))
            if len(doms) != 1 or len(rngs) != 1:
                continue
            dom = doms[0]
            rng = rngs[0]
            if not (isinstance(dom, URIRef) and isinstance(rng, URIRef)):
                continue
            if dom != rng:
                continue
            class_name = str(rng).split("#")[-1]
            if op_name != f"has{class_name}":
                continue
            # 짝이 되는 inverse 도 함께 제거할지 **먼저 판정** 한다. 아래에서 op 의
            # 트리플을 지우면 ``owl:inverseOf`` 링크도 함께 사라져 판정 근거를
            # 잃는다.
            #
            # **이름만 보고 지우면 안 된다**: ``is{Class}Of`` 는 흔한 명명 규칙이라
            # domain≠range 인 정상 프로퍼티가 같은 이름을 가질 수 있고, 그것을
            # 지우면 살아있는 관계가 라벨·range 까지 통째로 사라진다 (2026-08-08
            # 실측: ``isEquipmentOf`` domain=Equipment range=Plant 가 삭제됐다).
            #
            # 조건: 이름이 일치하고, **그 자신도 domain==range 인 self-loop** 이거나
            # 이 self-loop OP 의 inverseOf 로 명시 선언돼 있을 때만.
            inv_name = f"is{class_name}Of"
            inv_uri = DOMAIN_NS_OBJ[inv_name]
            remove_inverse = False
            if (inv_uri, RDF.type, OWL.ObjectProperty) in g:
                inv_doms = list(g.objects(inv_uri, RDFS.domain))
                inv_rngs = list(g.objects(inv_uri, RDFS.range))
                is_self_loop = (
                    len(inv_doms) == 1 and len(inv_rngs) == 1
                    and inv_doms[0] == inv_rngs[0]
                )
                declared_inverse = (
                    (op, OWL.inverseOf, inv_uri) in g
                    or (inv_uri, OWL.inverseOf, op) in g
                )
                remove_inverse = is_self_loop or declared_inverse
                if not remove_inverse:
                    logger.info(
                        "step_15b: %s 는 self-loop 이 아니고 %s 의 inverse 선언도 "
                        "없어 보존 (이름만 일치)", inv_name, op_name,
                    )

            for _, p, o in list(g.triples((op, None, None))):
                g.remove((op, p, o))
            if remove_inverse:
                for _, p, o in list(g.triples((inv_uri, None, None))):
                    g.remove((inv_uri, p, o))
                for s, p, _ in list(g.triples((None, None, inv_uri))):
                    g.remove((s, p, inv_uri))
            for s, p, _ in list(g.triples((None, None, op))):
                g.remove((s, p, op))
            self_loop_ops_removed += 1
    except Exception as e:
        logger.warning("self-loop FK OP 제거 실패: %s", e)
    return {"self_loop_ops_removed": self_loop_ops_removed}


def _clean_incompatible_inverse_of(g: Graph, steel_str: str) -> dict:
    """15c — incompatible inverseOf 선언 제거.

    Multi-Agent 가 ``producesProduct owl:inverseOf producedByEquipment,
    producedBy, isProducedByBlastFurnace`` 처럼 한 OP 에 서로 다른 domain 을
    가진 여러 inverse 를 선언하면, OWL RL prp-inv1 규칙으로 EquipmentMaster
    인스턴스에 ProductMaster 타입이 전파되어 AllDisjoint / 카디널리티 /
    domain/range 위반이 연쇄 발생.

    정책: inverseOf 쌍이 있으면 (op.domain, op.range) == (inv.range, inv.domain)
    완전 대응 여부를 검사, 어긋나면 해당 inverseOf 트리플만 제거.
    """
    incompatible_inverseOf_removed = 0
    try:
        def _supers(cls_uri):
            result = {cls_uri}
            stack = [cls_uri]
            while stack:
                cur = stack.pop()
                for par in g.objects(cur, RDFS.subClassOf):
                    if isinstance(par, URIRef) and par not in result:
                        result.add(par)
                        stack.append(par)
            return result

        def _domain_range_compat(a_uri, b_uri):
            if a_uri == b_uri:
                return True
            # owl:Thing 은 universal class — 모든 named class 와 호환.
            # Step 10 의 broaden 결과 (named domain → owl:Thing) 가 정상
            # inverseOf 쌍을 잘못 incompat 으로 판정하는 것을 방지.
            if a_uri == OWL.Thing or b_uri == OWL.Thing:
                return True
            a_sup = _supers(a_uri)
            b_sup = _supers(b_uri)
            return a_uri in b_sup or b_uri in a_sup

        def _expand_domain(cls_or_bnode):
            if isinstance(cls_or_bnode, URIRef):
                return {cls_or_bnode}
            members: set[URIRef] = set()
            for u_list in g.objects(cls_or_bnode, OWL.unionOf):
                try:
                    for m in rdflib.collection.Collection(g, u_list):
                        if isinstance(m, URIRef):
                            members.add(m)
                except Exception:  # noqa: BLE001 — 깨진 unionOf domain은 해당 목록만 건너뛴다
                    pass
            return members

        def _set_compat(a_set, b_set):
            """inverseOf 대칭 요건 — a_set 의 모든 원소가 b_set 의 어떤
            원소와 sub/super 체인에 있어야 compat.
            """
            if not a_set or not b_set:
                return False
            return all(
                any(_domain_range_compat(a, b) for b in b_set) for a in a_set
            )

        def _exact_match_score(op_dom_set, op_rng_set, inv_dom_set, inv_rng_set):
            """inverseOf 쌍의 적합도 0/1/2/3 계산.

            - 3: op.dom/rng 가 inv.rng/dom 과 동일 집합 (perfect exact)
            - 2: op.dom/rng 가 inv.rng/dom 의 부분집합 또는 포함 (strict sub/super)
            - 1: subclass 체인을 통해 연결 (loose compat)
            - 0: 연결 없음
            """
            if op_dom_set == inv_rng_set and op_rng_set == inv_dom_set:
                return 3
            dom_subset = (op_dom_set.issubset(inv_rng_set)
                          or inv_rng_set.issubset(op_dom_set))
            rng_subset = (op_rng_set.issubset(inv_dom_set)
                          or inv_dom_set.issubset(op_rng_set))
            if dom_subset and rng_subset:
                return 2
            if (_set_compat(op_dom_set, inv_rng_set)
                    and _set_compat(op_rng_set, inv_dom_set)):
                return 1
            return 0

        for op in list(g.subjects(RDF.type, OWL.ObjectProperty)):
            if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
                continue
            op_dom_raw = list(g.objects(op, RDFS.domain))
            op_rng_raw = list(g.objects(op, RDFS.range))
            if not (len(op_dom_raw) == 1 and len(op_rng_raw) == 1):
                continue
            op_dom_set = _expand_domain(op_dom_raw[0])
            op_rng_set = _expand_domain(op_rng_raw[0])
            if not op_dom_set or not op_rng_set:
                continue

            inv_candidates = list(g.objects(op, OWL.inverseOf))
            if len(inv_candidates) < 1:
                continue

            scored: list[tuple[int, URIRef]] = []
            for inv in inv_candidates:
                if not (isinstance(inv, URIRef) and str(inv).startswith(steel_str)):
                    scored.append((99, inv))
                    continue
                inv_dom_raw = list(g.objects(inv, RDFS.domain))
                inv_rng_raw = list(g.objects(inv, RDFS.range))
                if not (len(inv_dom_raw) == 1 and len(inv_rng_raw) == 1):
                    scored.append((99, inv))
                    continue
                inv_dom_set = _expand_domain(inv_dom_raw[0])
                inv_rng_set = _expand_domain(inv_rng_raw[0])
                if not inv_dom_set or not inv_rng_set:
                    scored.append((99, inv))
                    continue
                score = _exact_match_score(
                    op_dom_set, op_rng_set, inv_dom_set, inv_rng_set)
                scored.append((score, inv))

            testable = [(s, i) for s, i in scored if s != 99]
            testable.sort(key=lambda x: -x[0])
            to_remove: list[URIRef] = []
            if testable:
                best_score = testable[0][0]
                kept_one = False
                for sc, inv_uri in testable:
                    if sc == 0:
                        to_remove.append(inv_uri)
                        continue
                    if best_score >= 2 and sc < best_score:
                        to_remove.append(inv_uri)
                        continue
                    if kept_one:
                        to_remove.append(inv_uri)
                    else:
                        kept_one = True
            for inv_uri in to_remove:
                g.remove((op, OWL.inverseOf, inv_uri))
                if (inv_uri, OWL.inverseOf, op) in g:
                    g.remove((inv_uri, OWL.inverseOf, op))
                incompatible_inverseOf_removed += 1
                logger.debug(
                    "incompatible inverseOf 제거: %s ↔ %s",
                    str(op).split("#")[-1], str(inv_uri).split("#")[-1],
                )
    except Exception as e:
        logger.warning("inverseOf 호환성 정리 실패: %s", e)
    return {"incompatible_inverseOf_removed": incompatible_inverseOf_removed}


def _autocreate_fk_ops(g: Graph, steel_str: str) -> dict:
    """15b-2 — fk_patterns.json + CSV 헤더 스캔으로 누락 FK-OP 자동 생성."""
    import config  # 테스트 monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", ...) 반영
    from tools.ontology_quality import (
        DOMAIN_NS_OBJ,
        _local_name_to_human,
    )
    SOURCE_RAWDATA_DIR = config.SOURCE_RAWDATA_DIR

    fk_ops_autocreated = 0
    fk_inverse_skipped_duplicate = 0
    fk_ops_kept_abox_used = 0
    try:
        fk_patterns_path = rules_path("fk_patterns.json")
        if os.path.exists(fk_patterns_path):
            with open(fk_patterns_path, encoding="utf-8") as _fkf:
                fk_data = json.load(_fkf)
            fk_patterns: dict[str, str] = fk_data.get("patterns", {})
            suffix_rules = fk_data.get("suffix_rules", [])

            existing_op_ranges: set[tuple[str, str]] = set()
            existing_op_names: set[str] = set()
            for op in g.subjects(RDF.type, OWL.ObjectProperty):
                if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
                    continue
                existing_op_names.add(str(op).split("#")[-1].lower())
                doms = [str(d).split("#")[-1] for d in g.objects(op, RDFS.domain)
                        if isinstance(d, URIRef) and str(d).startswith(steel_str)]
                rngs = [str(r).split("#")[-1] for r in g.objects(op, RDFS.range)
                        if isinstance(r, URIRef) and str(r).startswith(steel_str)]
                for d in doms:
                    for r in rngs:
                        existing_op_ranges.add((d, r))

            csv_tables_lower: set[str] = set()
            # ``(source_class, target_class, fk_header)`` — **헤더 원문을 함께 담는다.**
            # 예전에는 ``(source_class, target)`` 만 담아 판정 근거인 CSV 컬럼명을
            # 버렸다. 그래서 OP 의 ``dcterms:source`` 보유율이 21%(29/137)에 머물렀고
            # (DP 는 역기록 스텝 덕에 99.6%), ``step_22f`` 근거 게이트는 대신
            # ``owl:onProperty`` 를 신호로 쓰다가 **OP 선언이 자기 근거가 되는 순환**
            # 에 빠졌다 (step_13b 가 모든 단일 domain/range OP 에 Restriction 을
            # 자동 부착하므로 — 실측: Restriction 111개 중 107개가 tautological).
            fk_entries: list[tuple[str, str, str]] = []
            if SOURCE_RAWDATA_DIR and os.path.isdir(SOURCE_RAWDATA_DIR):
                for fname in os.listdir(SOURCE_RAWDATA_DIR):
                    if not fname.lower().endswith(".csv"):
                        continue
                    base = fname.rsplit(".", 1)[0]
                    source_class = base.replace("_", "")
                    csv_tables_lower.add(source_class.lower())
                    csv_path = os.path.join(SOURCE_RAWDATA_DIR, fname)
                    try:
                        with open(csv_path, encoding="utf-8-sig") as _cf:
                            headers = next(csv.reader(_cf), [])
                        for header in headers:
                            normalized = re.sub(r"[_\s]", "", header).lower()
                            target: str | None = None
                            if normalized in fk_patterns:
                                target = fk_patterns[normalized]
                            else:
                                for rule in suffix_rules:
                                    sfx = rule["suffix"]
                                    if (normalized.endswith(sfx)
                                            and len(normalized) > len(sfx)):
                                        bs = normalized[:-len(sfx)]
                                        target = rule["template"].format(
                                            base=bs.capitalize())
                                        break
                            if target:
                                fk_entries.append((source_class, target, header))
                    except Exception as e:
                        logger.debug("FK OP autocreate CSV 스캔 실패 (%s): %s",
                                     fname, e)

            # A-Box 사용량 1회 조회 (500MB+ 순차 읽기). 후보는 이 경로가 만들 수
            # 있는 이름 전체 — has{Target} 과 is{Target}Of.
            from domain.graph_utils import abox_used_local_names
            _cands = {f"has{t}" for _, t, _h in fk_entries} | {
                f"is{t}Of" for _, t, _h in fk_entries
            }
            abox_used = abox_used_local_names(_cands)
            if abox_used is None:
                logger.warning(
                    "step_15b: A-Box 사용 판정 불가 — 억제-취소 신호 없이 진행한다",
                )

            for source_class, target_class, fk_header in fk_entries:
                # Self-reference skip — Item_Master.Item_Code 같이 자기 PK 가 FK
                # 패턴에도 매칭되는 케이스. self-loop OP (hasItemMaster @
                # ItemMaster → ItemMaster) 는 무의미하며 R6 step 15b 가 잘못
                # 생성하던 원인.
                if source_class.lower() == target_class.lower():
                    continue
                if target_class.lower() not in csv_tables_lower:
                    continue
                op_local = f"has{target_class}"
                inv_local_probe = f"is{target_class}Of"
                # **A-Box 가 두 다리 중 어느 쪽으로든 트리플을 만들었으면 쌍 점유를
                # 이유로 억제하지 않는다.** 억제의 목적은 값 0건인 빈 관계를 막는
                # 것인데, 값이 있는 쪽을 억제하면 그 관계가 T-Box 미선언 술어로 남아
                # 더 나쁘다.
                if (source_class, target_class) in existing_op_ranges:
                    _live = (
                        {n for n in (op_local, inv_local_probe) if n in abox_used}
                        if abox_used else set()
                    )
                    if not _live:
                        continue
                    logger.info(
                        "FK OP 억제 취소: %s (%s→%s) — 쌍은 점유됐지만 A-Box 가 이 "
                        "이름으로 트리플을 만들었다",
                        sorted(_live), source_class, target_class,
                    )
                    fk_ops_kept_abox_used += 1
                if op_local.lower() in existing_op_names:
                    continue
                src_uri = DOMAIN_NS_OBJ[source_class]
                tgt_uri = DOMAIN_NS_OBJ[target_class]
                if (src_uri, RDF.type, OWL.Class) not in g:
                    continue
                if (tgt_uri, RDF.type, OWL.Class) not in g:
                    continue

                op_uri = DOMAIN_NS_OBJ[op_local]
                inv_local = f"is{target_class}Of"
                inv_uri = DOMAIN_NS_OBJ[inv_local]

                # 역방향 중복 여부를 **정방향을 쓰기 전에** 판정한다. 이름만 보면
                # (target, source) 를 이미 잇는 다른 이름의 OP 를 놓쳐 같은 쌍에
                # 동의어가 생긴다 — 실측 (2026-08-11 배포 T-Box): 이 경로가 만든
                # 역방향 5개가 5개 모두 중복이었다.
                #
                # 그러나 **역방향만 건너뛰면 안 된다**: step_02 가 정방향에
                # ``owl:inverseOf is{Target}Of`` 를 붙이므로 역방향을 선언하지
                # 않으면 그 참조가 미선언 이름을 가리키는 dangling 이 된다 (실측:
                # 이 스텝의 leg 단위 가드가 2차 실행에서 dangling 5건을 만들었고
                # 어떤 validator 도 잡지 못했다). 중복이면 **쌍 전체를 건너뛴다.**
                inverse_pair_taken = (target_class, source_class) in existing_op_ranges
                inverse_name_taken = inv_local.lower() in existing_op_names
                # 억제 취소는 **두 다리 중 어느 쪽이든** A-Box 값이 있으면 성립한다.
                # 역방향 이름만 보면 정방향에 값이 있는 경우를 놓친다 — 실측
                # (2026-08-11): hasMonitoringPointMaster 576 / hasWarehouseMaster 85 /
                # hasItemMaster 85 가 `is{Target}Of` 기준 판정 때문에 억제돼
                # T-Box 미선언 술어로 남았다.
                live_legs = (
                    {n for n in (op_local, inv_local) if n in abox_used}
                    if abox_used else set()
                )
                if inverse_pair_taken and live_legs:
                    logger.info(
                        "FK OP 쌍 억제 취소: %s (%s↔%s) — A-Box 가 이 이름으로 "
                        "트리플을 만들었다. 값 있는 쪽을 억제하면 미선언 술어가 되어 "
                        "중복보다 나쁘다",
                        sorted(live_legs), source_class, target_class,
                    )
                    fk_ops_kept_abox_used += 1
                    inverse_pair_taken = False
                if inverse_pair_taken and not inverse_name_taken:
                    fk_inverse_skipped_duplicate += 1
                    logger.info(
                        "FK OP 쌍 생성 건너뜀: %s / %s (%s↔%s) — 같은 쌍을 잇는 OP "
                        "가 이미 있다. 정방향만 만들면 step_02 가 붙이는 "
                        "owl:inverseOf 가 미선언 이름을 가리킨다",
                        op_local, inv_local, source_class, target_class,
                    )
                    continue

                g.add((op_uri, RDF.type, OWL.ObjectProperty))
                g.add((op_uri, RDFS.domain, src_uri))
                g.add((op_uri, RDFS.range, tgt_uri))
                g.add((op_uri, RDFS.label,
                       Literal(_local_name_to_human(op_local), lang="en")))
                g.add((op_uri, RDFS.comment, Literal(
                    f"{source_class} 인스턴스에서 {target_class} 로의 FK 연결",
                    lang="ko")))
                # 판정 근거인 CSV 컬럼을 각인한다 (정·역 양쪽). ``dcterms:source``
                # 는 T-Box 밖의 사실을 가리키므로 순환하지 않는 근거다 — OP 선언이
                # 자기 근거가 되는 ``owl:onProperty`` 신호와 다르다.
                # 기존 값이 있으면 덮어쓰지 않는다 (SME/수동 기록 보존).
                _record_fk_source(g, op_uri, fk_header)
                if not inverse_name_taken:
                    g.add((inv_uri, RDF.type, OWL.ObjectProperty))
                    g.add((inv_uri, RDFS.domain, tgt_uri))
                    g.add((inv_uri, RDFS.range, src_uri))
                    g.add((inv_uri, OWL.inverseOf, op_uri))
                    g.add((inv_uri, RDFS.label,
                           Literal(_local_name_to_human(inv_local), lang="en")))
                    _record_fk_source(g, inv_uri, fk_header)
                    existing_op_names.add(inv_local.lower())
                    existing_op_ranges.add((target_class, source_class))
                existing_op_names.add(op_local.lower())
                existing_op_ranges.add((source_class, target_class))
                fk_ops_autocreated += 1
    except Exception as e:
        logger.warning("FK-OP 자동 생성 실패: %s", e)
    return {
        "fk_ops_autocreated": fk_ops_autocreated,
        "fk_inverse_skipped_duplicate": fk_inverse_skipped_duplicate,
        "fk_ops_kept_abox_used": fk_ops_kept_abox_used,
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    stats: dict = {}
    error: str | None = None
    try:
        steel_str = ctx.domain_ns
        stats.update(_remove_self_loop_fk_ops(g, steel_str))
        stats.update(_clean_incompatible_inverse_of(g, steel_str))
        stats.update(_autocreate_fk_ops(g, steel_str))
    except Exception as e:
        logger.warning("FK-OP autocreate 실패: %s", e)
        error = str(e)
    return StepResult(
        name="step_15b_fk_op_autocreate",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number=15.5,
        step_label="fk_op_autocreate",
    )
