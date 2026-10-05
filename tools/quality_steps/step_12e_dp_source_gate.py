"""Step 12e — DP 출처 컬럼 (``dcterms:source``) 게이트.

T-Box 생성 프롬프트 (``prompts/tbox-prompt-modules/04-property-rules.md``
원칙 4-1) 는 모든 DatatypeProperty 에 유래한 CSV 컬럼 코드를
``dcterms:source "<COLUMN>"`` 으로 기록하도록 요구한다. 이 표기가 A-Box
생성기의 **1순위 매핑 근거** 이므로 (``_col_to_prop`` step 0), 누락되면 해당
컬럼은 이름 추측 경로로 떨어지고 매칭 실패 시 조용히 폐기된다.

이 단계는 T-Box 를 수정하지 않고 (read-only) 다음을 측정한다:

  - ``coverage``: 출처 표기가 있는 DP 비율
  - ``resolvable``: 표기된 컬럼이 실제 CSV 헤더에 존재하는 DP 수 (오타 감지)
  - ``unknown_columns``: CSV 에 없는 컬럼을 가리키는 표기 (LLM hallucination)
  - ``duplicate_sources``: 한 (class, column) 을 두 DP 가 주장 — A-Box 가
    양쪽 모두 신뢰하지 않고 폴백하므로 실질 누락과 같다
  - ``missing_by_class``: 클래스별 미표기 DP 수 (프롬프트 회귀 진단용)

환경변수:
  - ``TBOX_DP_SOURCE_GATE``: ``warn`` (default) | ``fail``
  - ``TBOX_DP_SOURCE_THRESHOLD``: 0.0~1.0 (default 0.95)

``fail`` 모드에서 임계치 미달 시 ``RuntimeError`` 를 호출자에게 전파한다.
CSV 나 매핑 파일이 없는 환경 (도메인-중립 배포) 에서는 컬럼 존재 검증을
건너뛰고 coverage 만 측정한다.
"""
from __future__ import annotations

import collections
import csv
import logging
import os

from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")

_DEFAULT_THRESHOLD = 0.95


def _load_csv_columns_by_class() -> dict[str, set[str]]:
    """Return {ClassLocalName: {UPPER(column), ...}} from rawdata + mapping.

    Empty dict when the mapping file or rawdata directory is unavailable — the
    caller then reports coverage only and skips existence checks.
    """
    from config import SOURCE_RAWDATA_DIR
    from domain.table_mapping import load_table_class_mapping

    table_class = load_table_class_mapping()
    if not table_class:
        return {}
    result: dict[str, set[str]] = collections.defaultdict(set)
    for table, class_name in table_class.items():
        csv_path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(csv_path):
            continue
        try:
            with open(csv_path, encoding="utf-8-sig", newline="") as handle:
                header = next(csv.reader(handle), []) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("CSV 헤더 읽기 skip (%s): %s", table, exc)
            continue
        result[class_name].update(col.strip().upper() for col in header if col.strip())
    return dict(result)


def _collect_dp_sources(
    g: Graph, steel_str: str,
) -> tuple[list[str], dict[str, list[str]], dict[str, set[str]]]:
    """Return (all DP names, {dp: [source columns]}, {dp: {domain classes}})."""
    dp_names: list[str] = []
    sources: dict[str, list[str]] = {}
    domains: dict[str, set[str]] = {}
    for subject in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(subject).startswith(steel_str):
            continue
        name = str(subject).split("#")[-1].split("/")[-1]
        if not name:
            continue
        dp_names.append(name)
        columns = [
            str(obj).strip().upper()
            for obj in g.objects(subject, _DCTERMS_NS.source)
            if str(obj).strip()
        ]
        if columns:
            sources[name] = sorted(set(columns))
        domain_names = {
            str(obj).split("#")[-1].split("/")[-1]
            for obj in g.objects(subject, RDFS.domain)
            if isinstance(obj, URIRef)
        }
        domains[name] = {d for d in domain_names if d}
    return dp_names, sources, domains


def _measure(g: Graph, steel_str: str) -> dict:
    """Compute the source-annotation statistics for the current T-Box."""
    dp_names, sources, domains = _collect_dp_sources(g, steel_str)
    total = len(dp_names)
    annotated = len(sources)

    csv_by_class = _load_csv_columns_by_class()
    resolvable = 0
    unknown: list[dict] = []
    claim_owners: dict[tuple[str, str], set[str]] = collections.defaultdict(set)

    for dp_name, columns in sources.items():
        dp_domains = domains.get(dp_name) or {""}
        dp_resolved = False
        for column in columns:
            for domain_name in dp_domains:
                claim_owners[(domain_name, column)].add(dp_name)
            if not csv_by_class:
                continue
            known = any(
                column in csv_by_class.get(domain_name, ())
                for domain_name in dp_domains
            )
            if known:
                dp_resolved = True
            elif not any(
                column in cols for cols in csv_by_class.values()
            ):
                unknown.append({"dp": dp_name, "column": column,
                                "domains": sorted(dp_domains)})
        if dp_resolved:
            resolvable += 1

    duplicates = {
        f"{domain_name}/{column}": sorted(owners)
        for (domain_name, column), owners in claim_owners.items()
        if len(owners) > 1
    }

    # 반대 방향: 한 DP 가 **서로 다른 컬럼 여럿**을 주장하는 경우. A-Box 는 각 컬럼을
    # 순회하며 같은 DP 에 값을 넣으므로 두 컬럼의 값이 한 속성에 합쳐진다.
    #
    # 실측 (2026-07-27): ``orderStdNoBOld`` 가 ``STD_NO_B_OLD`` 와
    # ``STD_NO_B_NEW`` 을 함께 주장했다. 두 컬럼은 7,272행에서 모두 채워지지만
    # **값이 같은 행이 0건** 인 서로 다른 항목이다 (전자 ``GRADE002`` / 후자
    # ``GRADE001``). 결과: 값 트리플 14,544 = 인스턴스 약 1.2만 초과 → 이 속성으로
    # 필터하면 두 체계의 표준번호가 섞여 나오고, 집계는 중복 계수된다.
    # 다른 ``*_SPEC_NO_COL`` 컬럼들은 전용 DP 를 갖고 있어 이 DP 만 예외였다.
    #
    # ``_1``/``_2`` 같은 번호 계열은 각자 별개 DP 를 갖는 것이 원칙(원칙 4-1)이므로,
    # 컬럼이 2개 이상이면 이름이 비슷해도 병합 오류로 본다.
    multi_column = {
        dp_name: columns
        for dp_name, columns in sources.items()
        if len(columns) > 1
    }
    missing_by_class: collections.Counter = collections.Counter()
    for dp_name in dp_names:
        if dp_name in sources:
            continue
        for domain_name in domains.get(dp_name) or {"(no domain)"}:
            missing_by_class[domain_name] += 1

    return {
        "dp_total": total,
        "dp_with_source": annotated,
        "coverage": round(annotated / total, 4) if total else 0.0,
        "dp_source_resolvable": resolvable,
        "unknown_column_count": len(unknown),
        "unknown_columns_sample": unknown[:20],
        "duplicate_source_count": len(duplicates),
        "duplicate_sources_sample": dict(list(duplicates.items())[:20]),
        "multi_column_dp_count": len(multi_column),
        "multi_column_dps": dict(list(multi_column.items())[:20]),
        "missing_by_class": dict(missing_by_class.most_common(20)),
        "csv_reference_available": bool(csv_by_class),
    }


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """Measure ``dcterms:source`` coverage; warn or fail per policy."""
    before = len(g)
    mode = (os.getenv("TBOX_DP_SOURCE_GATE") or "warn").strip().lower()
    try:
        threshold = float(
            os.getenv("TBOX_DP_SOURCE_THRESHOLD") or _DEFAULT_THRESHOLD,
        )
    except ValueError:
        threshold = _DEFAULT_THRESHOLD

    stats: dict = {}
    error: str | None = None
    try:
        stats = _measure(g, ctx.domain_ns)
    except Exception as exc:  # noqa: BLE001 — measurement must not break S3
        logger.warning("Step 12e DP source 측정 실패 (무시): %s", exc)
        error = str(exc)
        stats = {"error": str(exc)}

    if not error:
        stats["gate_mode"] = mode
        stats["gate_threshold"] = threshold
        coverage = stats.get("coverage", 0.0)
        shortfall = stats["dp_total"] - stats["dp_with_source"]
        # DP 가 하나도 없으면 커버리지는 정의되지 않는다 (0/0). 이를 0% 로 보고
        # 게이트를 적용하면 "커버리지 0.0% — 미표기 DP 0개" 라는 자기모순 메시지가
        # 나오고, fail 모드에서는 **빈 T-Box 로 파이프라인이 중단**된다
        # (2026-08-08 규명: vacuous truth 를 위반으로 오판). 측정 대상이 없으면
        # 통과시키고 그 사실을 stats 에 남긴다.
        if stats["dp_total"] == 0:
            stats["gate_passed"] = True
            stats["gate_skipped"] = "도메인 네임스페이스에 DatatypeProperty 0개 — 측정 대상 없음"
            logger.info(
                "Step 12e DP source gate SKIP — 측정 대상 DP 0개 "
                "(T-Box 가 비었거나 네임스페이스 불일치)",
            )
            return StepResult(
                name="step_12e_dp_source_gate",
                stats=stats,
                triples_delta=len(g) - before,
                step_number="12e",
                step_label="dp_source_annotation_gate",
            )
        passed = coverage >= threshold
        stats["gate_passed"] = passed
        if not passed:
            message = (
                f"DP 출처 표기 (dcterms:source) 커버리지 {coverage:.1%} < "
                f"임계치 {threshold:.1%} — 미표기 DP {shortfall}개. "
                "이 DP 들은 A-Box 가 컬럼명 추측으로 매칭을 시도하며, 실패 시 "
                "해당 CSV 컬럼이 조용히 폐기된다. "
                "prompts/tbox-prompt-modules/04-property-rules.md 원칙 4-1 참조."
            )
            if mode == "fail":
                logger.error("Step 12e DP source gate FAIL: %s", message)
                raise RuntimeError(message)
            logger.warning("Step 12e DP source gate WARN: %s", message)
            error = message
        else:
            logger.info(
                "Step 12e DP source gate PASS — coverage %.1f%% (%d/%d)",
                coverage * 100, stats["dp_with_source"], stats["dp_total"],
            )
        if stats.get("duplicate_source_count"):
            logger.warning(
                "Step 12e: 같은 (class, column) 을 2개 이상 DP 가 주장 %d건 — "
                "A-Box 는 이 컬럼들을 폴백 경로로 처리한다",
                stats["duplicate_source_count"],
            )
        if stats.get("multi_column_dp_count"):
            # 이쪽은 폴백으로 방어되지 않는다 — A-Box 가 두 컬럼 값을 같은 속성에
            # **합쳐서** 적재하므로 값이 섞이고 집계가 중복 계수된다.
            logger.warning(
                "Step 12e: 한 DP 가 서로 다른 컬럼 여럿을 주장 %d건 — 두 컬럼 값이 "
                "한 속성에 합쳐져 질의 결과가 섞인다 (예: %s). 컬럼마다 별개 DP 로 "
                "분리할 것 (원칙 4-1).",
                stats["multi_column_dp_count"],
                list(stats.get("multi_column_dps", {}).items())[:2],
            )
        if stats.get("unknown_column_count"):
            logger.warning(
                "Step 12e: CSV 에 없는 컬럼을 가리키는 출처 표기 %d건 "
                "(LLM 오타/환각 가능)",
                stats["unknown_column_count"],
            )

    return StepResult(
        name="step_12e_dp_source_gate",
        stats=stats,
        triples_delta=len(g) - before,
        error=error,
        step_number="12e",
        step_label="dp_source_annotation_gate",
    )
