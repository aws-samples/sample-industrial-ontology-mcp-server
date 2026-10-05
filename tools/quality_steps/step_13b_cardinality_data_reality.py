"""Step 13b — 데이터 충진율에 맞지 않는 minCardinality 제약 완화.

S2 Architect(LLM) 는 CSV **헤더만** 받고 값의 충진율은 보지 못한다. 그래서
실제로는 비어 있는 행이 많은 컬럼에도 ``owl:minCardinality 1`` (필수) 을 선언한다.
2026-07-25 실측:

    steel:ProcessStepA_processStepAALblwGsTp_minCardinality  owl:minCardinality 1
      → 출처 컬럼 TYPE_COL_3 는 794행 중 522행만 채워짐 (65.7%)
      → ProcessStepA 인스턴스 272개가 카디널리티 위반 (validate_kg FAIL)

OWL 의 열린세계 가정(OWA) 에서는 값이 없어도 "아직 모름" 으로 해석되어 추론이
실패하진 않는다. 그러나:

  - ``validate_kg`` 의 카디널리티 체크가 FAIL 하고, 실제 데이터로는 절대 통과할 수
    없다 (원본 CSV 가 그런 상태이므로)
  - SHACL 로 검증하면 진짜 위반이 된다 (SHACL 은 닫힌세계)
  - "필수" 라는 선언이 데이터 현실과 어긋나 SME 가 오해한다

이 단계는 CSV 를 실측해 충진율이 임계치 미달인 DP 의 minCardinality 제약을
제거한다. **PK 는 항상 보존** 한다 (PK 가 비면 인스턴스가 만들어지지 않으므로
100% 가 보장되고, 필수 선언이 의미 있다).

식별 경로: restriction 의 ``owl:onProperty`` → DP → ``dcterms:source`` → CSV 컬럼.
출처 표기가 없으면 DP 이름에서 class prefix 를 벗겨 컬럼을 추정하고, 그래도 못
찾으면 건드리지 않는다 (판단 근거 없이 제약을 지우지 않는다).

**임계치를 넘겨도 100% 가 아니면 위반이 남는다.** minCardinality 는 인스턴스
**하나만** 비어도 위반이므로, 충진율 임계치(기본 0.95) 는 "완화 여부" 판단에는
쓸 수 있어도 "안전" 을 뜻하지 않는다. 실측 2026-07-25: ``processStepABlwMethTp`` 는
794행 중 793행(99.9%) 이 채워져 임계치를 통과했지만 빈 1행이 그대로
``validate_kg`` 카디널리티 위반으로 남았다. 그래서 임계치와 별개로 **충진율이
1.0 미만이면 항상 완화** 한다 (``_STRICT_FULL_FILL``). 임계치는 로그에서 심각도를
구분하는 데만 쓴다.

**제약은 자식 클래스에게 상속되므로 자식의 CSV 도 봐야 한다.** ``rdfs:subClassOf``
로 이어진 자식은 부모의 필수 제약을 그대로 물려받는데, 자식이 **다른 CSV 테이블**
을 모델링하면 그 컬럼이 자식 테이블에 없어 자식 인스턴스 전체가 위반한다.
실측 2026-07-25: ``Order.idCol1`` (ID_COL_1, Order 테이블 100% 채움) 이
``PlanRecordB`` 로 상속됐으나 ``PlanRecordB`` 의 원본 테이블 SRC_TBL_04 에는
``ID_COL_1`` 컬럼이 없어 약 800 인스턴스가 전부 위반했다 (이들은 ``planRecordB*``
DP 로 자기 식별자를 담는다). 그래서 소유 클래스뿐 아니라 **모든 자손 클래스** 의
CSV 를 함께 검사하고, 자손 중 하나라도 해당 컬럼을 갖지 않으면 완화한다. PK 라도
예외가 아니다 — 부모의 PK 가 자식의 PK 는 아니기 때문이다.

**자기 클래스 테이블에 없는 출처 컬럼도 완화 대상이다.** LLM 이 다른 테이블의
컬럼명을 빌려 필수 제약을 걸면 (실측: ``Order.keyCol1`` → ``KEY_COL_1`` 은
``Order`` 의 원본 테이블에 없고 ``OrderToleranceSpec`` 테이블에만
있다) A-Box 가 값을 채울 경로가 아예 없어 **모든 인스턴스가 위반** 한다. 예전에는
이를 "출처 미상(unresolved)" 으로 분류해 그냥 두었으나, 출처 표기가 명시적으로
있는데 그 컬럼이 자기 테이블에 없다는 것은 판단 근거가 없는 게 아니라 **근거가
있는 오류** 다. 이름 유추로 얻은 추정 컬럼은 신뢰도가 낮으므로 종전대로 보류한다.

환경변수:
  - ``TBOX_CARDINALITY_DATA_CHECK``: ``relax`` (default) | ``warn`` | ``off``
      relax = 미달 제약 제거 / warn = 보고만 / off = 스텝 skip
  - ``TBOX_CARDINALITY_MIN_FILL``: 심각도 구분용 충진율 임계치 (default 0.95).
      이 값과 무관하게 충진율 < 1.0 이면 완화한다.
"""
from __future__ import annotations

import collections
import csv
import logging
import os

from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")
_DEFAULT_MIN_FILL = 0.95

#: ``_column_fill_ratios`` 결과 캐시 — ``(rawdata 서명, ratios, pk_by_class)``.
#: 충진율 측정은 CSV 전 행을 훑으므로 (실측 3.8초) 반복 호출을 막는다.
_FILL_RATIO_CACHE: tuple | None = None

#: minCardinality 는 인스턴스 하나만 비어도 위반이므로, 충진율이 1.0 미만이면
#: 임계치와 무관하게 완화한다. 임계치는 로그 심각도 구분용으로만 남긴다.
_STRICT_FULL_FILL = 1.0


def _column_fill_ratios() -> tuple[dict[str, dict[str, float]], dict[str, set[str]]]:
    """CSV 를 스캔해 ({class: {UPPER(col): 충진율}}, {class: PK 컬럼}) 반환.

    충진율 = 값이 비어 있지 않은 행 / 전체 행. 빈 dict 는 CSV·매핑 부재를 뜻하며
    호출자는 그때 아무 제약도 건드리지 않는다.

    결과는 rawdata 서명 기준으로 캐시된다 (``_FILL_RATIO_CACHE``).
    """
    global _FILL_RATIO_CACHE

    from config import SOURCE_RAWDATA_DIR
    from tools.abox_generation import _load_table_class_mapping, _load_table_pk_columns
    from tools.ontology_quality import _rawdata_signature

    # 캐시 키는 이 함수가 **실제로 읽는** 디렉토리 기준이어야 한다. 여기서는
    # config 의 값을 쓰므로 서명도 그 값으로 명시한다 (인자를 생략하면
    # tools.ontology_quality 의 모듈 전역이 쓰여 키가 어긋난다).
    signature = _rawdata_signature(SOURCE_RAWDATA_DIR)
    if _FILL_RATIO_CACHE is not None and _FILL_RATIO_CACHE[0] == signature:
        return _FILL_RATIO_CACHE[1], _FILL_RATIO_CACHE[2]

    try:
        table_class = _load_table_class_mapping()
        table_pk = _load_table_pk_columns()
    except Exception as exc:  # noqa: BLE001 — measurement must not block
        logger.debug("매핑 로드 skip (13b): %s", exc)
        return {}, {}

    ratios: dict[str, dict[str, float]] = {}
    pk_by_class: dict[str, set[str]] = {}
    for table, cls_name in table_class.items():
        path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(path):
            continue
        pk_by_class[cls_name] = {
            c.upper() for c in (table_pk.get(table) or [])
        }
        filled: collections.Counter = collections.Counter()
        total = 0
        header: list[str] = []
        try:
            # 한 번의 순차 읽기로 헤더와 충진 카운트를 모두 얻는다. DictReader 는
            # fieldnames 로 헤더를 노출하므로 파일을 다시 열 필요가 없다.
            with open(path, encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                header = list(reader.fieldnames or [])
                for row in reader:
                    total += 1
                    for col, value in row.items():
                        if col and value and value.strip():
                            filled[col.upper()] += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("CSV 스캔 skip (%s): %s", table, exc)
            continue
        if total:
            ratios[cls_name] = {
                col: cnt / total for col, cnt in filled.items()
            }
            # 헤더에 있으나 값이 하나도 없는 컬럼도 0.0 으로 기록
            for col in header:
                if col:
                    ratios[cls_name].setdefault(col.upper(), 0.0)
    _FILL_RATIO_CACHE = (signature, ratios, pk_by_class)
    return ratios, pk_by_class


def _descendant_classes(
    g: Graph, cls_local: str, steel_str: str, *, max_depth: int = 8,
) -> set[str]:
    """``cls_local`` 의 모든 자손 클래스 local name (자신 제외).

    필수 제약은 ``rdfs:subClassOf`` 로 상속되므로, 부모에 걸린 minCardinality 가
    자식 인스턴스에도 강제된다. 자식이 다른 CSV 테이블을 모델링하면 해당 컬럼이
    없어 전부 위반이 된다.
    """
    seen: set[str] = set()
    frontier = [cls_local]
    for _ in range(max_depth):
        nxt: list[str] = []
        for node in frontier:
            for child in g.subjects(RDFS.subClassOf, URIRef(steel_str + node)):
                if not (isinstance(child, URIRef)
                        and str(child).startswith(steel_str)):
                    continue
                local = str(child)[len(steel_str):]
                if local != cls_local and local not in seen:
                    seen.add(local)
                    nxt.append(local)
        if not nxt:
            break
        frontier = nxt
    return seen


def _source_column_of(g: Graph, dp: URIRef, steel_str: str, cls_local: str) -> str | None:
    """DP 의 출처 CSV 컬럼 추정 — dcterms:source 우선, 없으면 이름에서 유추.

    이름 유추는 class prefix 를 벗긴 나머지를 대문자 스네이크로 되돌리는
    수준이며, 정확도가 낮으므로 호출자는 결과가 실제 CSV 컬럼 목록에 있을 때만
    사용해야 한다.
    """
    for src in g.objects(dp, _DCTERMS_SOURCE):
        text = str(src).strip()
        if text:
            return text.upper()
    local = str(dp)[len(steel_str):]
    prefix = cls_local[:1].lower() + cls_local[1:]
    if local.startswith(prefix):
        local = local[len(prefix):]
    if not local:
        return None
    # camelCase → UPPER_SNAKE (근사)
    out: list[str] = []
    for ch in local:
        if ch.isupper() and out and out[-1] != "_":
            out.append("_")
        out.append(ch.upper())
    return "".join(out)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """CSV 충진율이 임계치 미달인 DP 의 minCardinality 제약을 완화."""
    before = len(g)
    mode = (os.getenv("TBOX_CARDINALITY_DATA_CHECK") or "relax").strip().lower()
    if mode == "off":
        return StepResult(
            name="step_13b_cardinality_data_reality",
            stats={"skipped": "disabled by TBOX_CARDINALITY_DATA_CHECK"},
            triples_delta=0,
            step_number="13b",
            step_label="cardinality_data_reality",
        )
    try:
        min_fill = float(os.getenv("TBOX_CARDINALITY_MIN_FILL") or _DEFAULT_MIN_FILL)
    except ValueError:
        min_fill = _DEFAULT_MIN_FILL

    steel_str = ctx.domain_ns
    ratios, pk_by_class = _column_fill_ratios()
    if not ratios:
        return StepResult(
            name="step_13b_cardinality_data_reality",
            stats={"skipped": "CSV/매핑 없음 — 충진율 측정 불가"},
            triples_delta=0,
            step_number="13b",
            step_label="cardinality_data_reality",
        )

    relaxed: list[dict] = []
    kept_pk: list[str] = []
    unresolved: list[str] = []

    for restriction in list(g.subjects(RDF.type, OWL.Restriction)):
        if not isinstance(restriction, URIRef):
            continue
        if (restriction, OWL.minCardinality, None) not in g:
            continue
        dp = g.value(restriction, OWL.onProperty)
        if not isinstance(dp, URIRef) or (dp, RDF.type, OWL.DatatypeProperty) not in g:
            continue
        # 이 제약을 subClassOf 로 참조하는 클래스 (보통 1개)
        owners = [
            str(cls)[len(steel_str):]
            for cls in g.subjects(RDFS.subClassOf, restriction)
            if isinstance(cls, URIRef) and str(cls).startswith(steel_str)
        ]
        dp_local = str(dp)[len(steel_str):]
        target_cls = next((c for c in owners if c in ratios), None)
        if target_cls is None:
            unresolved.append(dp_local)
            continue

        declared_source = next(
            (str(s).strip().upper() for s in g.objects(dp, _DCTERMS_SOURCE)
             if str(s).strip()),
            None,
        )
        column = _source_column_of(g, dp, steel_str, target_cls)
        pk_columns = pk_by_class.get(target_cls, set())

        # 상속 검사 — 자손 클래스가 다른 CSV 를 모델링하면 컬럼이 없거나 덜 채워져
        # 자손 인스턴스가 위반한다 (실측: Order.idCol1 → PlanRecordB 794건은
        # SRC_TBL_04 에 ID_COL_1 가 아예 없음). PK 여부보다 먼저 본다: 부모의 PK 가
        # 자식의 PK 는 아니다.
        if column is not None:
            starved: dict[str, float | None] = {}
            for child in _descendant_classes(g, target_cls, steel_str):
                if child not in ratios:
                    continue          # 자식이 CSV 에 매핑되지 않음 — 판단 근거 없음
                child_fill = ratios[child].get(column)
                if child_fill is None or child_fill < _STRICT_FULL_FILL:
                    starved[child] = (
                        None if child_fill is None else round(child_fill, 4)
                    )
            if starved:
                relaxed.append({
                    "class": target_cls,
                    "property": dp_local,
                    "column": column,
                    "fill_ratio": None,
                    "reason": "column_absent_in_subclass_table",
                    "starved_subclasses": sorted(starved),
                    "subclass_fill_ratios": starved,
                })
                if mode == "relax":
                    for triple in list(g.triples((restriction, None, None))):
                        g.remove(triple)
                    for triple in list(g.triples((None, None, restriction))):
                        g.remove(triple)
                continue

        if column is not None and column in pk_columns:
            kept_pk.append(dp_local)
            continue

        if column is None or column not in ratios[target_cls]:
            # 이름 유추가 약해 컬럼을 못 찾았을 때, PK 컬럼의 접미와 겹치면 PK 로
            # 보고 보존한다 (DP 이름에서 컬럼 코드를 복원). 확실치 않은 제약을 지우는 것보다
            # 남기는 편이 안전하다.
            if column and any(
                pk.endswith(column) or column.endswith(pk.replace("_", ""))
                for pk in pk_columns
            ):
                kept_pk.append(dp_local)
                continue
            if declared_source is None:
                # 출처 표기가 없으면 이름 유추뿐이라 근거가 약하다 — 보류.
                unresolved.append(dp_local)
                continue
            # 출처가 **명시** 됐는데 자기 클래스 테이블에 그 컬럼이 없다 → A-Box 가
            # 값을 채울 경로가 아예 없어 모든 인스턴스가 위반한다 (실측:
            # Order.keyCol1 ← KEY_COL_1 은 다른 테이블 컬럼). 근거 있는 오류이므로
            # 완화한다.
            record = {
                "class": target_cls,
                "property": dp_local,
                "column": declared_source,
                "fill_ratio": None,
                "reason": "source_column_absent_in_class_table",
            }
        else:
            fill = ratios[target_cls][column]
            if fill >= _STRICT_FULL_FILL:
                continue
            record = {
                "class": target_cls,
                "property": dp_local,
                "column": column,
                "fill_ratio": round(fill, 4),
                "reason": (
                    "below_threshold" if fill < min_fill else "not_fully_filled"
                ),
            }

        relaxed.append(record)
        if mode == "relax":
            for triple in list(g.triples((restriction, None, None))):
                g.remove(triple)
            for triple in list(g.triples((None, None, restriction))):
                g.remove(triple)

    if relaxed:
        verb = "제거" if mode == "relax" else "감지(warn)"
        by_reason = collections.Counter(r["reason"] for r in relaxed)
        logger.warning(
            "Step 13b: 실데이터로 만족 불가한 minCardinality %d개 %s "
            "(사유별 %s / PK 보존 %d개, 출처 미상 %d개) — %s",
            len(relaxed), verb, dict(by_reason), len(kept_pk), len(unresolved),
            [
                f"{r['class']}.{r['property']}("
                + (f"{r['fill_ratio']:.1%}" if r["fill_ratio"] is not None
                   else f"컬럼 {r['column']} 없음")
                + ")"
                for r in relaxed[:5]
            ],
        )

    return StepResult(
        name="step_13b_cardinality_data_reality",
        stats={
            "mode": mode,
            "min_fill_threshold": min_fill,
            "relaxed_count": len(relaxed),
            "relaxed": relaxed[:20],
            "relaxed_by_reason": dict(
                collections.Counter(r["reason"] for r in relaxed),
            ),
            "kept_pk_count": len(kept_pk),
            "kept_pk": sorted(kept_pk),
            "unresolved_count": len(unresolved),
            "unresolved_sample": unresolved[:10],
        },
        triples_delta=len(g) - before,
        step_number="13b",
        step_label="cardinality_data_reality",
    )
