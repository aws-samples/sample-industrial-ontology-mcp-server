"""Step 15d — OP 의 FK 근거를 ``dcterms:source`` 로 **역기록**.

## 왜 필요한가

근거 표기 보유율이 두 축에서 극단적으로 갈렸다 (실측 2026-08-18 배포 T-Box):

| 축 | ``dcterms:source`` 보유 | 장치 |
|----|------------------------:|------|
| DP | 239/240 (99.6%)         | ``step_12d3`` 역기록 + ``step_12e`` 게이트 |
| OP | 29/137 (21.2%)          | 없음 |

차이는 **역기록**이다. DP 는 커버리지 판정 결과를 되써서 99.6% 를 만드는데 OP 에는
대응 스텝이 없었다. ``step_15b`` 가 OP 를 만들 때 컬럼을 기록하도록 고쳐도
**앞으로 만들 OP** 에만 적용된다 — 실측: 현행 T-Box 에서 ``fk_ops_autocreated=0``
(모든 FK OP 가 이미 존재), 근거율 21.2% 불변.

## 추측이 아니다 — 이미 내린 판정의 전사(轉寫)다

``csv_fk_pair_columns()`` 가 "이 ``(domain, range)`` 관계는 이 CSV 컬럼에서 왔다"
를 이미 판정한다 (``csv_fk_class_pairs`` 와 같은 도출인데 컬럼명을 버리지 않는
버전). ``step_15`` 는 그 판정으로 "FK 근거 없음" 을 로그에 남기며 OP 생성을
거부한다. 즉 시스템은 답을 알고 **기록만 안 하고** 있었다.

## 왜 근거 표기가 중요한가 — 순환하지 않는 근거

``step_22f_op_grounding_gate`` 는 근거 신호로 ``owl:onProperty`` 대상 여부를 보는데,
그 Restriction 은 ``step_13b`` 가 **모든 단일 domain/range OP 에 자동으로** 붙인
것이라 OP 선언이 자기 근거가 되는 순환이다 (실측: Restriction 111개 중 107개가
tautological → phantom 76개가 무근거 6개로만 보고). ``dcterms:source`` 는 T-Box
밖의 사실(CSV 컬럼)을 가리키므로 순환하지 않는다.

## 지표를 매수하지 않는다

FK 근거가 **없는** ``(domain, range)`` 는 비워 둔다. 아무 컬럼이나 적으면 근거율만
오르고 근거는 없다 — 그러면 게이트가 무의미해진다. 이 스텝이 채우는 것은 CSV FK 가
실제로 잇는 쌍뿐이고, 채우지 못한 나머지가 곧 "근거 없는 OP" 목록이다.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")


def _pick_column_for_op(op_local: str, columns: list[str]) -> str | None:
    """한 쌍을 잇는 FK 컬럼이 여러 개일 때 **OP 이름과 맞는 것**을 고른다.

    ## 왜 필요한가 (2026-08-30 실측)

    ``csv_fk_pair_columns`` 는 같은 ``(domain, range)`` 를 여러 컬럼이 잇으면
    사전순 첫 컬럼만 남긴다. ``Transportation.csv`` 는 ``Origin_Warehouse`` 와
    ``Destination_Warehouse`` 두 컬럼이 모두 ``Transportation → WarehouseMaster``
    를 잇고, 사전순 첫 값이 ``Destination_Warehouse`` 이므로 **Origin 방향 OP
    3개가 Destination 컬럼을 출처로 갖게 됐다**.

    잘못된 provenance 는 올바른 것과 게이트 상 구분되지 않는다 — ``step_12e`` 는
    존재 여부만 세고, 그 컬럼이 CSV 에 실재하므로 resolvable 로도 집계된다.

    판정은 **컬럼명의 판별 토큰이 OP 이름에 있는가** 로 한다. 컬럼명에서 대상
    클래스를 가리키는 부분(``Warehouse``)은 두 컬럼이 공유하므로 판별에 쓸 수
    없고, 차이를 만드는 토큰(``Origin`` / ``Destination``)만 본다.

    후보가 2개 이상 맞거나 하나도 맞지 않으면 ``None`` — **틀린 출처는 없는
    출처보다 나쁘다** (A-Box 가 그 값으로 컬럼을 찾는다).
    """
    if not columns:
        return None
    if len(columns) == 1:
        return columns[0]

    op_lower = op_local.lower()
    # 컬럼들이 공유하는 토큰은 판별력이 없다 — 각 컬럼의 고유 토큰만 남긴다.
    token_sets = {
        col: {t for t in col.lower().replace("-", "_").split("_") if t}
        for col in columns
    }
    shared: set[str] = set.intersection(*token_sets.values()) if token_sets else set()
    matches = [
        col for col, toks in token_sets.items()
        if any(t in op_lower for t in (toks - shared))
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _backfill_op_sources(g: Graph, steel_str: str) -> dict:
    from domain.graph_utils import (
        clean_source_column,
        csv_fk_pair_all_columns,
        csv_fk_pair_columns,
    )

    pair_cols = csv_fk_pair_columns()
    # 한 쌍을 여러 컬럼이 잇는 경우를 구분하기 위한 전체 목록 (없으면 단일 맵만).
    pair_all_cols = csv_fk_pair_all_columns() or {}
    if pair_cols is None:
        logger.info(
            "step_15d: CSV FK 판정 불가 — OP 출처 역기록을 건너뛴다 "
            "(0건과 판정불가를 혼동하면 잘못된 출처를 적는다)",
        )
        return {
            "op_source_backfilled": 0,
            "op_source_already_present": 0,
            "op_source_no_fk_evidence": 0,
            "op_source_skipped_ambiguous": 0,
        }

    def _norm(x: str) -> str:
        return x.replace("_", "").lower()

    backfilled = 0
    already = 0
    no_evidence = 0
    ambiguous = 0
    samples: list[str] = []
    ambiguous_samples: list[str] = []

    for op in sorted(g.subjects(RDF.type, OWL.ObjectProperty), key=str):
        if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
            continue
        existing = next(g.objects(op, _DCTERMS_SOURCE), None)
        # ``none:`` 접두사는 "근거 없음" 을 명시한 값이다 (step_12e 규약). 실제 FK
        # 근거를 찾았으면 그것으로 **교체**한다 — 그러지 않으면 근거가 있는데도
        # 영구히 "없음" 으로 남고, 근거율 집계에서도 빠진다.
        if existing is not None and not str(existing).startswith("none:"):
            already += 1
            continue
        doms = [d for d in g.objects(op, RDFS.domain)
                if isinstance(d, URIRef) and str(d).startswith(steel_str)]
        rngs = [r for r in g.objects(op, RDFS.range)
                if isinstance(r, URIRef) and str(r).startswith(steel_str)]
        # domain/range 가 여러 개면 어느 쌍의 근거인지 확정할 수 없다. 틀린
        # 출처는 없는 출처보다 나쁘다 (A-Box 가 그 값으로 컬럼을 찾는다).
        if len(doms) != 1 or len(rngs) != 1:
            ambiguous += 1
            continue
        key = (_norm(str(doms[0])[len(steel_str):]),
               _norm(str(rngs[0])[len(steel_str):]))
        op_local = str(op)[len(steel_str):]
        candidates = pair_all_cols.get(key) or []
        if len(candidates) > 1:
            # 한 쌍을 여러 컬럼이 잇는다 — OP 이름으로 판별한다. 못 고르면
            # 비워 둔다 (사전순 첫 컬럼을 쓰면 Origin OP 에 Destination 컬럼이
            # 붙는다 — 2026-08-30 실측 결함).
            col = _pick_column_for_op(op_local, candidates)
            if not col:
                ambiguous += 1
                if len(ambiguous_samples) < 5:
                    ambiguous_samples.append(f"{op_local}←{candidates}")
                continue
        else:
            col = pair_cols.get(key)
        if not col:
            no_evidence += 1
            continue
        cleaned = clean_source_column(col)
        if not cleaned:
            no_evidence += 1
            continue
        if existing is not None:
            # ``none:`` 센티넬 교체 — 남겨두면 한 OP 가 "근거 있음/없음" 을 동시에
            # 주장해 A-Box 출처 인덱스가 어느 값을 쓸지 비결정적이 된다.
            g.remove((op, _DCTERMS_SOURCE, existing))
        g.add((op, _DCTERMS_SOURCE, Literal(cleaned)))
        backfilled += 1
        if len(samples) < 5:
            samples.append(f"{str(op)[len(steel_str):]}←{cleaned}")

    if backfilled:
        logger.info(
            "Step 15d: OP 출처 역기록 %d건 (이미 보유 %d / FK 근거 없음 %d / "
            "domain·range 모호 %d). 예: %s",
            backfilled, already, no_evidence, ambiguous, samples,
        )
    if no_evidence:
        logger.info(
            "Step 15d: FK 근거가 없어 출처를 비워 둔 OP %d개 — 이것이 곧 "
            "'근거 없는 OP' 목록이다 (아무 컬럼이나 적으면 근거율만 오르고 "
            "게이트가 무의미해진다)",
            no_evidence,
        )
    corrections = _correct_wrong_op_sources(g, steel_str, pair_all_cols)
    return {
        "op_source_backfilled": backfilled,
        "op_source_already_present": already,
        "op_source_no_fk_evidence": no_evidence,
        "op_source_skipped_ambiguous": ambiguous,
        "op_source_ambiguous_samples": ambiguous_samples,
        **corrections,
    }


def _correct_wrong_op_sources(
    g: Graph, steel_str: str, pair_all_cols: dict,
) -> dict:
    """**이미 기록된** 출처가 OP 이름과 어긋나면 교정하거나 제거한다.

    생성 지점(위 ``_backfill_op_sources``)을 고쳐도 **이미 파일에 박힌** 값은
    남는다 — 이 리포에 기록된 "가드는 신규 쓰기만 막는다" 다. 그리고 그 값은
    ``already`` 로 집계돼 재기록 대상에서 제외되므로 영구히 남는다.

    실측 (2026-08-30 배포 T-Box): Origin 방향 OP 3개가
    ``dcterms:source "Destination_Warehouse"`` 를 갖고 있었다.

    판정은 생성 지점과 **같은 함수**(``_pick_column_for_op``)를 쓴다 (사본 금지).
    현재 값이 후보 목록에 있는데 이름 판정 결과와 다르면 교정하고, 판정이
    불가능하면 값을 **제거**한다 — 틀린 출처를 남기면 A-Box 가 그 컬럼을 찾아
    출발/도착이 뒤바뀔 수 있고, 게이트는 그것을 정상으로 본다.
    """
    corrected = 0
    dropped = 0
    samples: list[str] = []

    def _norm(x: str) -> str:
        return x.replace("_", "").lower()

    for op in sorted(g.subjects(RDF.type, OWL.ObjectProperty), key=str):
        if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
            continue
        existing = next(g.objects(op, _DCTERMS_SOURCE), None)
        if existing is None or str(existing).startswith("none:"):
            continue
        doms = [d for d in g.objects(op, RDFS.domain)
                if isinstance(d, URIRef) and str(d).startswith(steel_str)]
        rngs = [r for r in g.objects(op, RDFS.range)
                if isinstance(r, URIRef) and str(r).startswith(steel_str)]
        if len(doms) != 1 or len(rngs) != 1:
            continue
        key = (_norm(str(doms[0])[len(steel_str):]),
               _norm(str(rngs[0])[len(steel_str):]))
        candidates = pair_all_cols.get(key) or []
        # 후보가 1개 이하면 경쟁이 없으므로 현재 값을 의심할 근거가 없다.
        if len(candidates) < 2:
            continue
        if str(existing) not in candidates:
            # 이 쌍의 FK 컬럼이 아닌 값 — 다른 판정 경로에서 온 것일 수 있어
            # 건드리지 않는다 (과잉 교정 방지).
            continue
        op_local = str(op)[len(steel_str):]
        chosen = _pick_column_for_op(op_local, candidates)
        if chosen == str(existing):
            continue
        g.remove((op, _DCTERMS_SOURCE, existing))
        if chosen:
            from rdflib import Literal as _Literal
            g.add((op, _DCTERMS_SOURCE, _Literal(chosen)))
            corrected += 1
            if len(samples) < 5:
                samples.append(f"{op_local}: {existing} → {chosen}")
        else:
            dropped += 1
            if len(samples) < 5:
                samples.append(f"{op_local}: {existing} → (제거, 판정 불가)")

    if corrected or dropped:
        logger.warning(
            "Step 15d: 잘못된 OP 출처 %d건 교정 / %d건 제거 — 한 (domain, range) "
            "쌍을 여러 FK 컬럼이 잇는데 사전순 첫 컬럼이 모든 OP 에 붙어 있었다. "
            "잘못된 provenance 는 올바른 것과 게이트 상 구분되지 않고, A-Box 가 "
            "그 컬럼을 찾으므로 방향이 뒤바뀔 수 있다. 예: %s",
            corrected, dropped, samples[:5],
        )
    return {
        "op_source_corrected": corrected,
        "op_source_dropped_wrong": dropped,
        "op_source_correction_samples": samples,
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    stats = _backfill_op_sources(g, ctx.domain_ns)
    return StepResult(
        name="step_15d_op_source_backfill",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="15d",
        step_label="op_source_backfill",
    )
