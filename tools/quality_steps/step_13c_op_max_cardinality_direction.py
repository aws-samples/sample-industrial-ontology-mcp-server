"""Step 13c — ObjectProperty ``owl:maxCardinality`` 를 CSV FK **방향**으로 검증.

## 왜 필요한가 — max 축에는 데이터 대조가 전무했다

``step_13b`` 는 ``owl:minCardinality`` 만 본다 (그 스텝 240행 필터). 그리고
onProperty 가 ``owl:DatatypeProperty`` 일 것도 요구한다. 그래서 **OP 에 걸린
maxCardinality** 는 어떤 스텝도 데이터와 대조하지 않았다.

2026-08-30 실측: 이번 S2 가 ``owl:maxCardinality 1`` 을 **32개** 만들었다 (기준선은
**0개** — 이 공리군 자체가 신규다). 그중 **1개가 데이터와 정면 모순**이었다::

    steel:EquipmentMaster ⊑ ≤1 steel:hasEquipmentStatus

``Equipment_Status.csv`` 는 6,000행 = 설비 50대 × 시점 120 이다. 설비 1대는 상태
기록을 120개 갖는다. 나머지 31개는 CSV 실측상 정합이다 (관측 최대 1).

## 왜 게이트 3개가 이것을 놓쳤는가

===================================== ==========================================
게이트                                  왜 못 봤는가
===================================== ==========================================
``validate_owl_consistency`` (S4/S9.1)  **A-Box 를 로드하지 않는다.**
                                        ``tools/owl_reasoner.py`` 에 ``ABOX_PATH``
                                        참조 0건, 기본 경로가 ``TBOX_PATH``. 그
                                        ``consistent: true`` 는 T-Box 만의 결과다.
``validate_owl_cardinality`` (CWA)      데이터 그래프를 ``TBOX_PATH`` +
                                        ``ABOX_PATH`` 로만 만든다. 문제의 술어는
                                        ``a_box.ttl`` 에 **0건**이고 역방향
                                        6,000건으로만 존재한다.
``check_shacl_owl_cardinality_sync``    restriction 이 ``BNode`` 일 것을 요구한다.
                                        step_19 스콜렘화 후 101개 전부 URIRef 라
                                        32개를 하나도 추출하지 못한다.
===================================== ==========================================

유일하게 발화한 것은 ``validate_kg`` 기본 모드다 — ``load_graph`` 가
``ensure_inverse_triples`` 를 호출해 역방향 6,000 트리플을 만들기 때문이다.
검증으로 확인: A-Box 개체 3개(설비 1 + 값이 다른 상태 2)만 넣으면 HermiT 이
``consistent: false`` 를 내고, **이 제약 엣지 1개만** 제거하면 ``true`` 로 돌아온다.

## 판정 신호 — 컬럼 존재 여부가 아니라 컬럼의 **역할**

처음에 "출처 컬럼이 소유 클래스의 테이블에 있으면 행당 값 1개니까 정합" 으로
판정했더니 **32/32 를 OK 로 오판**했다. ``Equipment_ID`` 는
``Equipment_Master``(소유 테이블)에도 있기 때문이다 — 다만 거기서는 **PK** 다.

옳은 신호는 하나다::

    출처 컬럼이 소유 클래스 테이블에서 unique(= PK) 인가?
      예  → FK 는 range 쪽 테이블에 있다. domain 한 개체가 가리키는 개수는
             그 테이블에서 컬럼 값의 **최대 반복 횟수** 다.
      아니오 → 반복되는 FK 컬럼이다. 행마다 값 1개이므로 max 1 은 정합.

이 판정기는 SPARQL 로 추론 그래프를 직접 잰 결과와 일치한다 (위반 1건,
``hasEquipmentStatus`` observed 120). 즉 CSV 만으로 추론 후 위반을 예측한다.

## 왜 지우는가 (그리고 무엇은 지우지 않는가)

이 리포는 **공리 값을 데이터에 맞추는 자동 교정을 지표 매수로 기각**한 전례가 있다
(``Recycle`` → ``Recycling`` 류). 그 판단은 여기서도 유효하다 — 그래서 이 스텝은
``maxCardinality`` 값을 1 → 120 으로 **고쳐 쓰지 않는다**. 데이터 최대치는 표본에
따라 변하므로 그것을 공리로 박으면 다음 CSV 에서 다시 깨진다.

대신 ``step_12i`` 의 정책을 따른다: **모든** 소유 개체가 위반하는(= 위반율 1.0)
공리만 제거한다. 근거가 확정적이고 (컬럼 역할로 결정됨) 보존할 의미가 없기
때문이다. 부분 위반은 보고만 한다 — 데이터 정제로 해결될 수 있고, 공리가 의도된
제약일 수 있다.

역방향 공리는 **건드리지 않는다**. ``equipmentStatusRefersToEquipment ≤1`` 은
옳다 (상태 1건 → 설비 1대). 방향이 뒤집힌 쪽만 지운다 — inverse 쌍 양쪽에 1 이
걸린 것이 32개 중 이 1쌍뿐이었고, 둘 다 지우면 참인 제약을 잃는다.

## 2차 근거 — tacit 이 채우는 OP 는 CSV 로 판정할 수 없었다

CSV FK 컬럼만 보던 판정기는 **암묵지가 채우는 관계** 를 구조적으로 못 본다.
``dcterms:source`` 컬럼이 없으므로 첫 관문(``no_source_column``)에서 판정 불가로
빠진다. 그런데 이 KG 에서 A-Box OP 트리플의 절반 이상이 tacit 유래다.

2026-09-03 실측 (S2 재실행 후)::

    steel:AirEmissionMonitoring ⊑ ≤1 steel:hasStackEquipment
      step_13c 판정 : undecided (no_source_column)  ← CSV FK 컬럼이 없다
      실제          : validate_kg 위반 1,560건 (개체마다 7개)

발생 경로가 두 겹이다. S2 Round 5 가 "``Stack_ID`` 를 ``Equipment_ID`` 에 조인한다는
**가정**" 으로 이 OP 를 신설하고 ``maxCardinality 1`` 을 걸었다 (T-Box 주석에 그
가정과 "SME 가 실제 값 대조 필요" 가 적혀 있다). 실측하면 ``Stack_ID`` 는
``Stack_1``~``Stack_5`` 5개뿐이고 ``EQ001``~``EQ050`` 과 교집합이 **0** 이다 —
그 FK 는 존재하지 않는다. 한편 ``rules/domain/tacit_rules.json`` 은 같은 OP 를
``Location`` 버킷 co-location 으로 채우며 **다대다임을 명시**한다. 즉 공리(1:1)와
그것을 채우는 유일한 출처(다대다)가 정면으로 모순이고, R5 SME 가 정확히 이것을
반대했으나 반영되지 않았다.

그래서 CSV 로 판정 불가일 때 **tacit TTL 의 실측 팬아웃** 을 2차 근거로 쓴다.
tacit 은 S3 시점에 이미 존재하는 결정적 소스 입력이라 A-Box 를 기다릴 필요가 없다.

분모는 **소유 클래스의 전체 개체 수**(소유 테이블 행 수) 다. tacit 주체 수를 분모로
쓰면 tacit 이 10개만 이어도 비율이 1.0 이 되어, 나머지 1,550개가 만족하는 공리를
지운다. 여기서는 1,560/1,560 이라 ``_FULL_VIOLATION`` 정책을 그대로 만족한다.

값을 1 → 7 로 **고쳐 쓰지 않는 것은 CSV 축과 동일** 하다 (표본에 따라 변하는 최대치를
공리로 박으면 다음 데이터에서 다시 깨진다). 임계를 올리는 것도 지표 매수다.

환경변수:
  - ``TBOX_OP_MAX_CARD_CHECK``: ``relax`` (default) | ``warn`` | ``off``
      relax = 전수 위반 공리 제거 / warn = 보고만 / off = skip
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

#: 소유 개체 **전부** 가 위반할 때만 제거한다 (``step_12i`` 의 fail_ratio==1.0 정책).
#: 부분 위반은 데이터 정제로 해결될 수 있고 공리가 의도된 제약일 수 있다.
_FULL_VIOLATION = 1.0


def _table_columns() -> dict[str, list[str]]:
    """{table: [header...]} — CSV 헤더만 읽는다 (전 행 스캔 회피)."""
    from config import SOURCE_RAWDATA_DIR

    out: dict[str, list[str]] = {}
    if not os.path.isdir(SOURCE_RAWDATA_DIR):
        return out
    for name in os.listdir(SOURCE_RAWDATA_DIR):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(SOURCE_RAWDATA_DIR, name)
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle), [])
        except Exception as exc:  # noqa: BLE001 — 측정이 파이프라인을 막지 않는다
            logger.debug("헤더 읽기 skip (%s): %s", name, exc)
            continue
        out[name[:-4]] = [c.upper() for c in header if c]
    return out


def _max_repeat_of_column(table: str, column: str) -> tuple[int, int]:
    """``table.column`` 값의 (최대 반복 횟수, distinct 값 수).

    반복 횟수가 곧 "그 값을 가리키는 행이 몇 개인가" = domain 쪽 다중도다.
    """
    from config import SOURCE_RAWDATA_DIR

    path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
    counter: collections.Counter = collections.Counter()
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            field = next(
                (f for f in (reader.fieldnames or []) if f.upper() == column),
                None,
            )
            if field is None:
                return 0, 0
            for row in reader:
                value = (row.get(field) or "").strip()
                if value:
                    counter[value] += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug("컬럼 스캔 skip (%s.%s): %s", table, column, exc)
        return 0, 0
    if not counter:
        return 0, 0
    return max(counter.values()), len(counter)


def _row_count(table: str) -> int:
    """CSV 데이터 행 수 — 소유 클래스의 개체 수 근사 (A-Box 는 행당 1개체)."""
    from config import SOURCE_RAWDATA_DIR

    path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            return max(0, sum(1 for _ in csv.reader(handle)) - 1)  # 헤더 제외
    except Exception as exc:  # noqa: BLE001 — 측정이 파이프라인을 막지 않는다
        logger.debug("행 수 스캔 skip (%s): %s", table, exc)
        return 0


def _tacit_fanout(tacit: Graph, prop: URIRef) -> tuple[int, int, int]:
    """``prop`` 의 (최대 팬아웃, 주체 수, 팬아웃>limit 판정용 주체별 카운터 최대).

    주체는 OP 의 domain 클래스 개체이므로 별도 타입 필터를 두지 않는다 — tacit 은
    ``rdf:type`` 을 쓰지 않고, IRI 문자열로 클래스를 유추하는 것은 이 리포가
    금지한 방식이다 (도메인 이식 시 조용히 0건이 된다).

    Returns:
        ``(최대 팬아웃, 주체 수, 0)`` — 세 번째는 호출자가 limit 을 알고 세도록 남긴다.
    """
    counter: collections.Counter = collections.Counter()
    for subj in tacit.subjects(prop, None):
        counter[subj] += 1
    if not counter:
        return 0, 0, 0
    return max(counter.values()), len(counter), 0


def _count_subjects_over(tacit: Graph, prop: URIRef, limit: int) -> int:
    """``prop`` 에서 ``limit`` 을 초과하는 주체 수 — 위반 개체 수."""
    counter: collections.Counter = collections.Counter()
    for subj in tacit.subjects(prop, None):
        counter[subj] += 1
    return sum(1 for c in counter.values() if c > limit)


def _class_to_table() -> dict[str, str]:
    """{class local name: table} — ``step_13b`` 와 같은 매핑 소스를 쓴다."""
    from tools.abox_generation import _load_table_class_mapping

    try:
        table_class = _load_table_class_mapping()
    except Exception as exc:  # noqa: BLE001
        logger.debug("클래스-테이블 매핑 로드 skip (13c): %s", exc)
        return {}
    return {cls: table for table, cls in table_class.items()}


def _source_column(g: Graph, prop: URIRef) -> str | None:
    """OP 의 ``dcterms:source`` 컬럼. 이름 유추는 하지 않는다.

    ``step_13b`` 의 ``_source_column_of`` 는 표기가 없으면 이름에서 컬럼을
    유추하는데, OP 이름은 관계 서술어(``hasEquipmentStatus``)라 컬럼명과 무관하다.
    근거 없이 추측하면 정당한 공리를 지울 수 있으므로 표기가 없으면 판정을 포기한다.
    """
    for src in g.objects(prop, _DCTERMS_SOURCE):
        text = str(src).strip()
        if text:
            return text.upper()
    return None


def _local(node, steel_str: str) -> str:
    return str(node)[len(steel_str):]


def _in_scope(g: Graph, restriction, steel_str: str) -> bool:
    """이 restriction 이 **도메인 OP** 의 maxCardinality 제약인가.

    ``_evaluate`` 와 총계 집계가 **같은 판정**을 써야 한다. 처음에는 총계를 따로
    셌는데 도메인 필터를 빠뜨려 외래 OP(IOF 등)를 검사 대상으로 보고했다 —
    "검사 대상 N개 중 위반 0" 이 실제로는 손대지도 않는 공리를 포함한 수치가 된다.
    """
    if g.value(restriction, OWL.maxCardinality) is None:
        return False
    prop = g.value(restriction, OWL.onProperty)
    if not isinstance(prop, URIRef) or not str(prop).startswith(steel_str):
        return False
    # DP 축은 step_13b 의 소관이다 (그 스텝이 충진율로 판정한다).
    return (prop, RDF.type, OWL.ObjectProperty) in g


def _count_in_scope(g: Graph, steel_str: str) -> int:
    return sum(
        1 for r in g.subjects(RDF.type, OWL.Restriction)
        if _in_scope(g, r, steel_str)
    )


def _route_undecided(
    record: dict, prop: URIRef, limit: int, owner_table: str | None,
    violating: list[dict], partial: list[dict], undecided: list[dict],
) -> None:
    """CSV 로 판정 불가인 레코드를 **tacit 실측 팬아웃** 으로 재판정한다.

    tacit 이 그 술어를 채우지 않으면 판정 불가 그대로 둔다 — "측정할 수 없다" 를
    "정합" 으로 읽지 않는다 (그 반대도 아니다: 근거 없이 지우지 않는다).
    """
    from domain.tbox_utils import load_tacit_graph

    tacit = load_tacit_graph()
    fanout, subjects, _ = _tacit_fanout(tacit, prop)
    if subjects == 0:
        undecided.append(record)               # tacit 도 이 술어를 모른다
        return

    record.update({
        "evidence": "tacit", "tacit_subjects": subjects, "observed_max": fanout,
    })
    if fanout <= limit:
        # tacit 실측이 공리를 만족한다 — 정합이므로 보존하고 보고하지 않는다.
        return

    violators = _count_subjects_over(tacit, prop, limit)
    # 분모는 소유 클래스 **전체 개체 수** 다. tacit 주체 수를 쓰면 일부만 이어도
    # 비율이 1.0 이 되어 나머지가 만족하는 공리를 지운다 (docstring 참조).
    owner_rows = _row_count(owner_table) if owner_table else 0
    denominator = owner_rows or subjects
    record.update({
        "tacit_violators": violators, "owner_rows": owner_rows,
        "violation_ratio": round(violators / denominator, 4) if denominator else None,
        "denominator": "owner_rows" if owner_rows else "tacit_subjects",
    })
    if record["violation_ratio"] is not None and \
            record["violation_ratio"] >= _FULL_VIOLATION:
        record["reason"] = "all_owners_violate_tacit"
        violating.append(record)
    else:
        record["reason"] = "partial_violation_tacit"
        partial.append(record)


def _evaluate(
    g: Graph, steel_str: str, class_table: dict[str, str],
    headers: dict[str, list[str]],
) -> tuple[list[dict], list[dict], list[dict]]:
    """(전수 위반, 부분 위반, 판정 불가) 세 갈래로 분류한다."""
    violating: list[dict] = []
    partial: list[dict] = []
    undecided: list[dict] = []

    for restriction in list(g.subjects(RDF.type, OWL.Restriction)):
        # 총계 집계와 **같은 판정기** 를 쓴다 (``_in_scope`` docstring 참조).
        if not _in_scope(g, restriction, steel_str):
            continue
        prop = g.value(restriction, OWL.onProperty)
        try:
            limit = int(g.value(restriction, OWL.maxCardinality))
        except (TypeError, ValueError):
            continue

        owners = [
            _local(cls, steel_str)
            for cls in g.subjects(RDFS.subClassOf, restriction)
            if isinstance(cls, URIRef) and str(cls).startswith(steel_str)
        ]
        prop_local = _local(prop, steel_str)
        rng = g.value(prop, RDFS.range)
        range_local = (
            _local(rng, steel_str)
            if isinstance(rng, URIRef) and str(rng).startswith(steel_str) else None
        )
        column = _source_column(g, prop)

        for owner in owners:
            record = {
                "class": owner, "property": prop_local, "column": column,
                "limit": limit, "range": range_local,
                "restriction": _local(restriction, steel_str),
            }
            owner_table = class_table.get(owner)
            range_table = class_table.get(range_local) if range_local else None
            if not column or not owner_table:
                record["reason"] = (
                    "no_source_column" if not column else "no_owner_table"
                )
                _route_undecided(
                    record, prop, limit, owner_table,
                    violating, partial, undecided,
                )
                continue
            if column not in headers.get(owner_table, []):
                record["reason"] = "column_absent_in_owner_table"
                _route_undecided(
                    record, prop, limit, owner_table,
                    violating, partial, undecided,
                )
                continue

            # ── 판정 신호: 소유 테이블에서 이 컬럼이 unique(=PK) 인가 ──────
            owner_repeat, owner_distinct = _max_repeat_of_column(
                owner_table, column,
            )
            if owner_repeat == 0:
                record["reason"] = "column_empty_in_owner_table"
                _route_undecided(
                    record, prop, limit, owner_table,
                    violating, partial, undecided,
                )
                continue
            if owner_repeat > 1:
                # 반복되는 FK 컬럼 → 행마다 값 1개 → max 1 은 정합.
                continue
            # unique = PK. 그러면 FK 는 range 쪽 테이블에 있고, domain 다중도는
            # 그 테이블에서 이 컬럼 값의 최대 반복 횟수다.
            if not range_table or column not in headers.get(range_table, []):
                record["reason"] = "fk_side_table_unknown"
                _route_undecided(
                    record, prop, limit, owner_table,
                    violating, partial, undecided,
                )
                continue
            fanout, distinct = _max_repeat_of_column(range_table, column)
            record.update({
                "owner_table": owner_table, "fk_table": range_table,
                "observed_max": fanout, "fk_distinct_values": distinct,
                "owner_rows": owner_distinct,
            })
            if fanout <= limit:
                continue
            # 소유 개체 전부가 위반하는가 — FK 테이블이 참조하는 distinct 값 수가
            # 소유 테이블 행 수와 같으면 모든 개체가 위반한다.
            record["violation_ratio"] = (
                round(distinct / owner_distinct, 4) if owner_distinct else None
            )
            if record["violation_ratio"] is not None and \
                    record["violation_ratio"] >= _FULL_VIOLATION:
                record["reason"] = "all_owners_violate"
                violating.append(record)
            else:
                record["reason"] = "partial_violation"
                partial.append(record)

    return violating, partial, undecided


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """OP 의 maxCardinality 를 CSV FK 방향으로 검증하고 전수 위반을 제거."""
    before = len(g)
    mode = (os.getenv("TBOX_OP_MAX_CARD_CHECK") or "relax").strip().lower()
    if mode == "off":
        return StepResult(
            name="step_13c_op_max_cardinality_direction",
            stats={"skipped": "disabled by TBOX_OP_MAX_CARD_CHECK",
                   "op_max_card_checked": 0},
            triples_delta=0,
            step_number="13c", step_label="op_max_cardinality_direction",
        )

    steel_str = ctx.domain_ns
    total = _count_in_scope(g, steel_str)
    class_table = _class_to_table()
    headers = _table_columns()
    if not class_table or not headers:
        # 측정 불가를 "위반 0" 과 구분해 남긴다 — 이 리포의 "0회 발동 게이트는
        # 0회로 보고하라" 원칙 (키 부재/무발화를 정상으로 오독하는 함정).
        return StepResult(
            name="step_13c_op_max_cardinality_direction",
            stats={"skipped": "CSV/매핑 없음 — FK 방향 측정 불가",
                   "op_max_card_checked": total},
            triples_delta=0,
            step_number="13c", step_label="op_max_cardinality_direction",
        )

    violating, partial, undecided = _evaluate(g, steel_str, class_table, headers)

    removed = 0
    if mode == "relax":
        for record in violating:
            restriction = URIRef(steel_str + record["restriction"])
            for triple in list(g.triples((restriction, None, None))):
                g.remove(triple)
            for triple in list(g.triples((None, None, restriction))):
                g.remove(triple)
            removed += 1

    if violating:
        verb = "제거" if mode == "relax" else "감지(warn)"
        # 근거 축마다 있는 키가 다르다 — tacit 레코드엔 ``fk_table`` 이 없다.
        # 예전 포맷은 그 키를 직접 색인해서 tacit 축이 붙는 순간 KeyError 로
        # 스텝을 죽였다 (2026-09-03, 배선 중 발견).
        def _why(record: dict) -> str:
            if record.get("evidence") == "tacit":
                return (
                    f"tacit 주체 {record.get('tacit_subjects')}개 중 "
                    f"{record.get('tacit_violators')}개 위반"
                )
            return f"FK={record.get('fk_table')}.{record.get('column')}"

        logger.warning(
            "Step 13c: 데이터와 모순인 maxCardinality %d개 %s — %s. "
            "CSV 축은 출처 컬럼이 소유 테이블의 PK 라 FK 가 range 쪽에 있는 경우이고, "
            "tacit 축은 암묵지가 그 술어를 다대다로 채우는 경우다. 값을 데이터 "
            "최대치로 고쳐 쓰지 않는 이유는 표본에 따라 변하기 때문이다 (지표 매수 방지)",
            len(violating), verb,
            [
                f"{r['class']}.{r['property']}(≤{r['limit']} vs 실측 "
                f"{r['observed_max']}, {_why(r)})"
                for r in violating[:5]
            ],
        )
    if partial:
        logger.info(
            "Step 13c: 부분 위반 %d개는 보고만 한다 (데이터 정제로 해결될 수 있고 "
            "공리가 의도된 제약일 수 있다) — %s",
            len(partial),
            [f"{r['class']}.{r['property']}({r['violation_ratio']})"
             for r in partial[:5]],
        )

    return StepResult(
        name="step_13c_op_max_cardinality_direction",
        stats={
            "mode": mode,
            "op_max_card_checked": total,
            "op_max_card_violating": len(violating),
            "op_max_card_removed": removed,
            "op_max_card_violating_detail": violating[:20],
            "op_max_card_partial": len(partial),
            "op_max_card_partial_detail": partial[:10],
            "op_max_card_undecided": len(undecided),
            "op_max_card_undecided_by_reason": dict(
                collections.Counter(r["reason"] for r in undecided),
            ),
        },
        triples_delta=len(g) - before,
        step_number="13c",
        step_label="op_max_cardinality_direction",
    )
