"""CSV ⇄ KG Round-trip Fidelity — Q4.

원본 CSV를 KG로 변환한 뒤 역으로 CSV로 복원했을 때 원본과 얼마나 같은지
측정한다. 정보 이론적 보존 지표 + 추론으로 늘어난 것과 손실된 것을 분리.

3-stage loss manifest는 "얼마나 떨어졌는가"를 단계별로 기록했지만
실제로 원본과 재구성본을 cell 단위 비교한 적은 없음. 여기서 그걸 한다.

방법:
1. rules/domain/table_class_mapping.json으로 CSV 테이블 ↔ 도메인 클래스 매핑
2. 각 클래스마다 A-Box에서 SPARQL로 인스턴스+DP 값 조회 → row dict
3. 원본 CSV row와 cell-by-cell diff (row key는 PK 컬럼 추정)
4. metric:
   - row_recall = KG→CSV에 있는 원본 row / 원본 총 row
   - cell_recall = 일치 cell / 원본 cell 총
   - extra_cells = KG→CSV에만 있는 cell (추론 이득 혹은 노이즈 후보)

주의: 전수 비교는 메모리 크다 → 테이블별 sample N=1000 기본.
"""
from __future__ import annotations

import csv
import json
import logging
import os

from config import GENERATED_REPORTS_DIR, SOURCE_RAWDATA_DIR
from domain.namespaces import NS_PREFIX, prepend_prefixes
from domain.rules_paths import RULES_ROOT, rules_path
from domain.sparql_templates import reject_sparql_egress
from tools.common import error_response, success_response
from tools.query_test import _is_local_name

logger = logging.getLogger(__name__)

_RULES_DIR = RULES_ROOT
_MAPPING_PATH = rules_path("table_class_mapping.json", base=_RULES_DIR)


def _load_table_class_mapping() -> dict[str, str]:
    """CSV 테이블 → 클래스 local name. 파싱은 ``domain.table_mapping`` 이 담당한다.

    직접 파싱하던 시절엔 중첩 dict 형태 (``{"class": ...}``) 를 무시해 그 테이블이
    조용히 사라졌다 — roundtrip 은 그 테이블을 "KG 에 없는 행" 으로 계산해
    row_recall 을 실제보다 낮게 보고한다 (2026-08-08).
    """
    from domain.table_mapping import load_table_class_mapping
    return load_table_class_mapping(_MAPPING_PATH)


def _guess_pk_column(headers: list[str]) -> str | None:
    """헤더에서 PK 후보 추정 — '*Id', '*_id', '*ID', 'id'."""
    candidates = []
    for h in headers:
        hl = h.lower()
        if hl == "id":
            return h
        if hl.endswith("id") or hl.endswith("_id"):
            candidates.append(h)
    return candidates[0] if candidates else (headers[0] if headers else None)


def _read_csv(path: str, limit: int | None = None) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        rows = []
        for i, row in enumerate(reader):
            if limit is not None and i >= limit:
                break
            rows.append(row)
    return headers, rows


def _reconstruct_class_rows(graph, class_name: str, limit: int = 1000) -> list[dict]:
    """A-Box에서 해당 클래스 인스턴스 + 모든 DP 값을 dict로 복원.

    클래스는 설정된 도메인 prefix (``NS_PREFIX``) 로 질의한다. ``class_name`` 은
    로컬 이름 형식일 때만 보간하고, PREFIX 를 붙인 최종 문자열을 egress 가드에 넣은
    뒤 실행한다.

    Raises:
        ValueError: ``class_name`` 이 로컬 이름 형식이 아니거나 최종 질의가 가드에
            거부될 때. 질의는 실행하지 않는다.
    """
    if not _is_local_name(class_name):
        raise ValueError(
            f"SPARQL 로컬 이름 형식이 아닌 클래스 이름: {str(class_name)[:80]!r}"
        )
    query = prepend_prefixes(
        "SELECT ?s ?p ?o WHERE { "
        f"  ?s a {NS_PREFIX}:{class_name} . "
        "  ?s ?p ?o . "
        "  FILTER(isLiteral(?o)) "
        "}"
    )
    reject_sparql_egress(query)
    rows_by_s: dict[str, dict[str, str]] = {}
    try:
        for s, p, o in graph.query(query):
            key = str(s)
            prop = str(p).split("#")[-1].split("/")[-1]
            rows_by_s.setdefault(key, {})[prop] = str(o)
            if len(rows_by_s) >= limit * 2:
                break
    except Exception as e:
        logger.warning("SPARQL 복원 실패 (%s): %s", class_name, e)
        return []
    return list(rows_by_s.values())[:limit]


def _cell_diff(orig: list[dict], recon: list[dict], pk: str) -> dict:
    """원본 vs 복원 cell-level 비교."""
    orig_by_pk = {r.get(pk): r for r in orig if r.get(pk)}
    # 복원 rows는 property name을 key로 씀 → PK 컬럼 이름을 다양하게 시도
    recon_by_pk: dict[str, dict] = {}
    pk_lower = pk.lower().replace("_", "")
    for r in recon:
        # DP property name에서 PK 매칭 후보 찾기
        val = None
        for k, v in r.items():
            if k.lower().replace("_", "") == pk_lower:
                val = v
                break
        if val is not None:
            recon_by_pk[val] = r

    matched_pks = set(orig_by_pk.keys()) & set(recon_by_pk.keys())
    missing_pks = set(orig_by_pk.keys()) - set(recon_by_pk.keys())
    extra_pks = set(recon_by_pk.keys()) - set(orig_by_pk.keys())

    cell_matches = 0
    cell_total_orig = 0
    cell_extras = 0
    for pk_val in matched_pks:
        o = orig_by_pk[pk_val]
        r = recon_by_pk[pk_val]
        for col, val in o.items():
            if val is None or val == "":
                continue
            cell_total_orig += 1
            # recon에서 같은 컬럼명(공백/언더스코어 무시)으로 찾기
            col_norm = col.lower().replace("_", "")
            matched = False
            for rk, rv in r.items():
                if rk.lower().replace("_", "") == col_norm and str(rv) == str(val):
                    matched = True
                    break
            if matched:
                cell_matches += 1
        # recon에 있는데 orig에 없는 cell (추론 이득 or 노이즈)
        for rk, _rv in r.items():
            if rk.lower().replace("_", "") == pk_lower:
                continue
            col_norm = rk.lower().replace("_", "")
            if not any(ok.lower().replace("_", "") == col_norm for ok in o):
                cell_extras += 1

    return {
        "orig_rows": len(orig_by_pk),
        "recon_rows": len(recon_by_pk),
        "matched_rows": len(matched_pks),
        "missing_rows": len(missing_pks),
        "extra_rows": len(extra_pks),
        "row_recall_pct": round(
            len(matched_pks) / max(len(orig_by_pk), 1) * 100, 2,
        ),
        "cell_total_orig": cell_total_orig,
        "cell_matches": cell_matches,
        "cell_recall_pct": round(
            cell_matches / max(cell_total_orig, 1) * 100, 2,
        ),
        "cell_extras": cell_extras,
    }


def roundtrip_class(
    graph, csv_path: str, class_name: str, limit: int = 1000,
) -> dict:
    if not _is_local_name(class_name):
        return {"class": class_name,
                "skipped": "class name is not a SPARQL local name"}
    headers, orig_rows = _read_csv(csv_path, limit=limit)
    if not orig_rows:
        return {"class": class_name, "skipped": "empty CSV"}
    pk = _guess_pk_column(headers)
    if not pk:
        return {"class": class_name, "skipped": "no PK candidate"}

    recon = _reconstruct_class_rows(graph, class_name, limit=limit)
    if not recon:
        return {"class": class_name, "skipped": "no reconstructed rows"}

    diff = _cell_diff(orig_rows, recon, pk)
    return {"class": class_name, "csv_table": os.path.basename(csv_path),
            "pk_column": pk, "limit": limit, **diff}


def run_roundtrip_fidelity(
    limit_per_class: int = 500,
    max_classes: int = 10,
) -> dict:
    """전 매핑 테이블을 순회하며 round-trip 측정."""
    mapping = _load_table_class_mapping()
    if not mapping:
        return {"error": "table_class_mapping.json 없음"}

    from domain.tbox_utils import load_graph
    g, _ = load_graph(use_inferred=False)

    per_class: list[dict] = []
    for table, cls in list(mapping.items())[:max_classes]:
        csv_path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(csv_path):
            per_class.append({"class": cls, "csv_table": table,
                              "skipped": "csv missing"})
            continue
        per_class.append(roundtrip_class(g, csv_path, cls, limit=limit_per_class))

    # 전체 집계
    valid = [r for r in per_class if "row_recall_pct" in r]
    if not valid:
        return {"per_class": per_class, "summary": {"no_valid_classes": True}}
    avg_row_recall = round(sum(r["row_recall_pct"] for r in valid) / len(valid), 2)
    avg_cell_recall = round(sum(r["cell_recall_pct"] for r in valid) / len(valid), 2)
    total_extras = sum(r["cell_extras"] for r in valid)
    return {
        "summary": {
            "classes_measured": len(valid),
            "classes_skipped": len(per_class) - len(valid),
            "avg_row_recall_pct": avg_row_recall,
            "avg_cell_recall_pct": avg_cell_recall,
            "total_extra_cells": total_extras,
        },
        "per_class": per_class,
    }


def verify_roundtrip_fidelity(
    limit_per_class: int = 500,
    max_classes: int = 10,
) -> str:
    """CSV → KG → CSV 복원 fidelity 측정 (Q4).

    row_recall: 원본 row 중 KG에서 재구성된 비율
    cell_recall: 원본 cell 중 값까지 일치 복원된 비율
    cell_extras: KG에만 있고 원본에 없던 cell (추론 이득 혹은 노이즈)

    Args:
        limit_per_class: 클래스당 비교할 최대 row 수.
        max_classes: 측정할 클래스 수 상한.
    """
    try:
        report = run_roundtrip_fidelity(limit_per_class=limit_per_class,
                                        max_classes=max_classes)
        try:
            os.makedirs(GENERATED_REPORTS_DIR, exist_ok=True)
            with open(os.path.join(GENERATED_REPORTS_DIR, "roundtrip_fidelity.json"),
                      "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("roundtrip_fidelity 저장 실패: %s", e)
        return success_response(report)
    except Exception as e:
        return error_response(
            f"roundtrip fidelity 실패: {e}",
            hint="load_graph + SPARQL이 작동하는지 확인.",
            logger=logger,
        )
