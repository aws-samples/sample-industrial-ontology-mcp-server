"""Step 12i — 선언된 DP ``rdfs:range`` 가 **실제 CSV 값**과 맞는가.

기존 range 스텝들은 range 의 **형식**만 봤다:

* ``step_09c_dp_range_xsd`` — range 가 XSD 타입인가, 정확히 하나인가
* ``step_12d_csv_dp_inject`` — 컬럼에 대응하는 DP 가 선언돼 있는가

어느 쪽도 "그 XSD 타입으로 그 컬럼의 값이 실제로 변환되는가" 를 묻지 않는다.
그래서 range 가 값과 정면으로 충돌해도 전 파이프라인이 조용히 통과한다.

## 2026-08-30 실측 — 98행이 소리 없이 사라졌다

``itemSupplierMapPriority`` 의 range 가 ``xsd:integer`` 로 선언됐는데
``Item_Supplier_Map.csv`` 의 ``Priority`` 실값은 ``Primary`` / ``Secondary``
문자열이다 (50 + 48 = 98행, 전량)::

    T-Box   itemSupplierMapPriority  rdfs:range  xsd:integer
    CSV     Priority                 = "Primary" | "Secondary"
    A-Box   → _format_value 가 변환 실패 → None → **트리플 스킵**

``rules/contracts/common_dp.json`` 의 ``hasPriority`` 템플릿은 ``xsd:string`` 을
지시하므로 이 ``integer`` 는 S2 LLM 이 이름만 보고 발명한 값이다
("priority" → 숫자 등급이라는 추측).

**왜 어떤 게이트도 못 잡았나** — 세 곳이 동시에 침묵했다:

1. ``_format_value`` 는 변환 실패 시 ``None`` 을 반환하고 **트리플만 건너뛴다**.
   예외도 경고도 없다 (타입 불일치 적재를 막으려는 의도된 설계다).
2. ``column_coverage`` 는 컬럼 → DP **이름 해석**만 센다. 해석은 성공했으므로
   ``mapped: 5 / rate: 100.0`` 을 보고한다. 리터럴이 실제로 써졌는지는 안 본다.
3. ``property_completeness_detail`` 은 이 DP 를 ``pct: 0.0`` 으로 정확히 기록하지만
   **FAIL 축이 아니라 정보 필드**다. ``fidelity_score`` 는 여전히
   ``column_coverage: 1.0`` 을 보고한다.

``abox_loss_manifest.json`` 의 ``type_coercion_failures: 98`` 이 유일한 흔적인데,
그 숫자가 "특정 컬럼의 전량 손실" 인지 "여러 컬럼의 산발적 이상치" 인지 구분되지
않아 아무도 읽지 않았다.

## 이 스텝의 방향 — 전량 손실만 교정하고, 부분 손실은 보고만 한다

**전량 실패 (fail_ratio == 1.0)** 는 값 이상치가 아니라 **타입 오선언**이다. 그
range 로는 단 한 행도 적재되지 않으므로 선언을 유지할 근거가 없다. 이 경우만
``xsd:string`` 으로 넓힌다 — string 은 모든 CSV 값을 적재하므로 데이터 손실이 0이
되고, 원래 타입은 ``skos:note`` 로 남겨 판단 근거를 잃지 않는다.

**부분 실패**는 건드리지 않는다. 소수 이상치는 CSV 오타일 수도 있고(그러면 range 가
옳다) 새 값 어휘의 등장일 수도 있다(그러면 range 가 틀렸다). 어느 쪽인지는 도메인
판단이므로 측정·보고까지만 한다. 조용히 넓히면 진짜 데이터 오류가 문자열로 적재돼
영구히 숨는다.

교정 방향을 **넓히는 쪽으로만** 제한하는 이유: 좁히는 교정(string → integer)은
기존 A-Box 트리플을 무효화하지만, 넓히는 교정은 이미 적재되던 값을 계속 적재한다.
이 리포에 기록된 "수정이 다음 결함을 만든다" 를 피하려면 단조 방향이어야 한다.

순서 계약: 이 스텝은 ``step_09c`` (다중 range 정리) **뒤** 여야 한다. 앞에 두면
09c 가 ``xsd:string`` 을 "폴백" 으로 보고 버리고 원래 타입을 되살린다 (실측으로
확인: integer + string → integer 를 남긴다). 같은 이유로 이 교정은 range 를
**교체**하며 추가하지 않는다.

환경변수:
  - ``TBOX_DP_RANGE_REALITY_GATE``: ``warn`` (default) | ``fail``
  - ``TBOX_DP_RANGE_REALITY_MAX_FAIL_RATIO``: 0.0~1.0 (default 0.5) — 이 비율을
    넘는 값이 변환 실패하면 위반으로 본다. 소수 이상치(오타·결측 표기)는 정상
    데이터에도 있으므로 전량 실패에 가까운 것만 잡는다.
  - ``TBOX_DP_RANGE_REALITY_AUTOFIX``: ``true`` (default) | ``false`` — 전량 손실
    range 를 ``xsd:string`` 으로 넓힐지. ``false`` 면 read-only 측정만.

CSV 나 매핑 파일이 없는 배포(도메인-중립)에서는 no-op 이다.
"""
from __future__ import annotations

import collections
import contextlib
import csv
import logging
import os

from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")

#: 이 비율을 넘게 변환 실패하면 range 가 값과 맞지 않는 것으로 본다.
_DEFAULT_MAX_FAIL_RATIO = 0.5

#: 값이 비었다고 보는 표기 — ``_format_value`` 의 센티널 처리와 같은 취지.
#: 여기서 세면 "결측이 많은 컬럼" 이 타입 불일치로 오분류된다.
_NULL_TOKENS = frozenset({
    "", "-", "n/a", "na", "null", "none", "nan", "nil", "?", "--",
})


def _load_columns_with_values() -> dict[str, dict[str, list[str]]]:
    """Return ``{ClassLocalName: {UPPER(column): [non-empty values...]}}``.

    ``step_12e`` 의 ``_load_csv_columns_by_class`` 와 같은 매핑 경로를 쓰지만
    **헤더가 아니라 값**까지 읽는다 — 이 스텝의 판정 근거가 값이기 때문이다.
    """
    from config import SOURCE_RAWDATA_DIR
    from domain.table_mapping import load_table_class_mapping

    table_class = load_table_class_mapping()
    if not table_class:
        return {}
    result: dict[str, dict[str, list[str]]] = {}
    for table, class_name in table_class.items():
        csv_path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except Exception as exc:  # noqa: BLE001
            logger.debug("CSV 값 읽기 skip (%s): %s", table, exc)
            continue
        if not rows:
            continue
        by_column: dict[str, list[str]] = collections.defaultdict(list)
        for row in rows:
            for raw_col, raw_val in row.items():
                if raw_col is None:
                    continue
                value = str(raw_val).strip() if raw_val is not None else ""
                if value.lower() in _NULL_TOKENS:
                    continue
                by_column[raw_col.strip().upper()].append(value)
        # 같은 클래스를 여러 테이블이 가리키면 값을 합친다 (매핑상 가능).
        target = result.setdefault(class_name, {})
        for column, values in by_column.items():
            target.setdefault(column, []).extend(values)
    return result


def _convertible(value: str, xsd_range: str) -> bool:
    """``value`` 가 ``xsd_range`` 로 변환되는가 — A-Box 와 **같은 판정기**를 쓴다.

    사본을 만들면 두 경로가 갈린다 (이 리포의 반복 실패 패턴). A-Box 가 실제로
    쓰는 ``_format_value`` 를 그대로 호출해, 이 스텝이 "통과" 라고 한 값은
    A-Box 에서도 반드시 적재된다.
    """
    from tools.abox_generation import _format_value

    literal = _format_value(value, "probe", tbox_range=xsd_range)
    return isinstance(literal, Literal)


def _measure(g: Graph, domain_ns: str) -> dict:
    """(class, column) 별 변환 실패율을 재고 위반 목록을 만든다."""
    values_by_class = _load_columns_with_values()
    if not values_by_class:
        return {"skipped": "csv_or_mapping_unavailable", "violations": []}

    violations: list[dict] = []
    checked = 0
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(dp).startswith(domain_ns):
            continue
        ranges = [
            str(obj) for obj in g.objects(dp, RDFS.range)
            if isinstance(obj, URIRef)
        ]
        # range 가 없거나 여럿이면 이 스텝의 관심사가 아니다 — step_09c 담당.
        if len(ranges) != 1:
            continue
        xsd_range = ranges[0]
        columns = [
            str(obj).strip().upper()
            for obj in g.objects(dp, _DCTERMS_NS.source)
            if str(obj).strip()
        ]
        if not columns:
            continue
        domains = {
            str(obj)[len(domain_ns):]
            for obj in g.objects(dp, RDFS.domain)
            if isinstance(obj, URIRef) and str(obj).startswith(domain_ns)
        }
        dp_name = str(dp)[len(domain_ns):]
        for class_name in domains:
            per_column = values_by_class.get(class_name) or {}
            for column in columns:
                values = per_column.get(column)
                if not values:
                    continue
                checked += 1
                failed = sum(1 for v in values if not _convertible(v, xsd_range))
                if not failed:
                    continue
                ratio = failed / len(values)
                violations.append({
                    "dp": dp_name,
                    "class": class_name,
                    "column": column,
                    "declared_range": xsd_range.split("#")[-1],
                    "values_total": len(values),
                    "values_failed": failed,
                    "fail_ratio": round(ratio, 4),
                    "sample_failed": [
                        v for v in values if not _convertible(v, xsd_range)
                    ][:3],
                })

    max_ratio = _DEFAULT_MAX_FAIL_RATIO
    with contextlib.suppress(ValueError):
        max_ratio = float(
            os.getenv("TBOX_DP_RANGE_REALITY_MAX_FAIL_RATIO", str(max_ratio))
        )
    blocking = [v for v in violations if v["fail_ratio"] > max_ratio]
    return {
        "dp_column_pairs_checked": checked,
        "violations": sorted(violations, key=lambda v: -v["fail_ratio"]),
        "violation_count": len(violations),
        "blocking": blocking,
        "blocking_count": len(blocking),
        "max_fail_ratio": max_ratio,
        # 전량 손실은 오타가 아니라 타입 오선언이다 — 별도 카운터로 눈에 띄게.
        "total_loss_count": sum(1 for v in violations if v["fail_ratio"] >= 1.0),
    }


def _widen_total_loss_ranges(
    g: Graph, domain_ns: str, violations: list[dict],
) -> list[dict]:
    """전량 손실 range 를 ``xsd:string`` 으로 넓힌다. 교정 내역을 반환.

    ``xsd:string`` 은 모든 CSV 값을 적재하므로 이 교정 후 해당 컬럼의 손실은 0이 된다.
    원래 선언은 ``skos:note`` 로 남겨 "누가 왜 바꿨는지" 를 산출물에서 읽을 수 있게
    한다 — 지우면 다음 사람이 같은 추측을 반복한다.
    """
    from rdflib import XSD
    from rdflib import Literal as _Literal
    from rdflib.namespace import SKOS

    applied: list[dict] = []
    for violation in violations:
        if violation["fail_ratio"] < 1.0:
            continue
        prop = URIRef(domain_ns + violation["dp"])
        current = list(g.objects(prop, RDFS.range))
        if len(current) != 1 or current[0] == XSD.string:
            # 여러 range 가 남아 있으면 step_09c 의 관심사다 — 건드리지 않는다.
            continue
        g.remove((prop, RDFS.range, current[0]))
        g.add((prop, RDFS.range, XSD.string))
        g.add((prop, SKOS.note, _Literal(
            f"rdfs:range 를 {violation['declared_range']} → string 으로 교정 "
            f"(step_12i): CSV {violation['class']}.{violation['column']} 의 값 "
            f"{violation['values_failed']}/{violation['values_total']} 이 "
            f"{violation['declared_range']} 로 변환되지 않아 A-Box 가 전량 폐기했다. "
            f"실값 예: {violation['sample_failed']}", lang="ko",
        )))
        applied.append({
            "dp": violation["dp"],
            "from": violation["declared_range"],
            "to": "string",
            "rows_recovered": violation["values_failed"],
        })
        logger.info(
            "[step_12i] %s range %s → string 교정 — %d행 복구",
            violation["dp"], violation["declared_range"],
            violation["values_failed"],
        )
    return applied


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """선언 range 대비 CSV 값 변환 가능성을 측정하고 전량 손실만 교정한다."""
    stats: dict = {}
    before = len(g)
    try:
        measured = _measure(g, ctx.domain_ns)
    except Exception as exc:  # noqa: BLE001
        # 진단 스텝이 파이프라인을 막지 않는다.
        logger.warning("step_12i: 측정 실패 (건너뜀): %s", exc)
        return StepResult(
            name="step_12i_dp_range_csv_reality",
            stats={"dp_range_reality": {"error": str(exc)[:200]}},
            step_number="12i",
            step_label="dp_range_csv_reality",
        )

    autofix = os.getenv(
        "TBOX_DP_RANGE_REALITY_AUTOFIX", "true",
    ).strip().lower() in ("true", "1", "yes")
    if autofix and measured.get("violations"):
        measured["autofixed"] = _widen_total_loss_ranges(
            g, ctx.domain_ns, measured["violations"],
        )
        measured["autofixed_count"] = len(measured["autofixed"])
        # 교정된 것은 더 이상 차단 대상이 아니다 — 남은 것만 게이트에 넘긴다.
        fixed_dps = {a["dp"] for a in measured["autofixed"]}
        measured["blocking"] = [
            v for v in measured.get("blocking", []) if v["dp"] not in fixed_dps
        ]
        measured["blocking_count"] = len(measured["blocking"])

    stats["dp_range_reality"] = measured
    blocking = measured.get("blocking") or []
    if blocking:
        for violation in blocking[:5]:
            logger.warning(
                "[step_12i] %s (%s.%s): range=%s 인데 값 %d/%d 가 변환 불가 — "
                "A-Box 가 그 트리플을 조용히 버린다. 예: %s",
                violation["dp"], violation["class"], violation["column"],
                violation["declared_range"], violation["values_failed"],
                violation["values_total"], violation["sample_failed"],
            )
        mode = os.getenv("TBOX_DP_RANGE_REALITY_GATE", "warn").strip().lower()
        if mode == "fail":
            names = ", ".join(
                f"{v['dp']}({v['declared_range']}←{v['sample_failed'][:1]})"
                for v in blocking[:5]
            )
            raise RuntimeError(
                f"DP range 가 CSV 실값과 불일치: {len(blocking)}건 — {names}. "
                "range 를 값에 맞추거나 CSV 를 교정하라 "
                "(TBOX_DP_RANGE_REALITY_GATE=warn 으로 우회 가능)."
            )

    return StepResult(
        name="step_12i_dp_range_csv_reality",
        stats=stats,
        triples_delta=len(g) - before,
        step_number="12i",
        step_label="dp_range_csv_reality",
    )
