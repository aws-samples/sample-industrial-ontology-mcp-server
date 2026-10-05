#!/usr/bin/env python3
"""원천 CSV → 온톨로지 클래스 매핑 검토 보고서.

``tools/local_artifacts.generate_table_class_map_report`` 가 이 모듈을 로드해
``collect()`` / ``render()`` / ``_OUT`` 을 사용한다. 그 MCP 도구는 2026-08-08 까지
**항상 실패** 했다 — 이 파일이 git 이력 전체에 존재한 적이 없었기 때문이다
(도구는 등록돼 있고 docstring 도 상세했지만 호출하면 FileNotFoundError).

도구가 약속한 검토 항목을 그대로 구현한다:
  - 테이블별 컬럼/행 수, PK, 생성된 인스턴스/속성/관계 수
  - **행 수 ≠ 인스턴스 수** 인 테이블 (적재 누락 검출)
  - 동명 컬럼 (같은 이름이 여러 테이블에 있어 값 의미가 다를 수 있음)
  - 원천 테이블 없이 만들어진 클래스 (코드 마스터 승격 / 추상 상위)

데이터 출처는 모두 기존 산출물이다 (새 계산 없음):
  ``rules/domain/table_class_mapping.json`` · ``data/source/rawdata/*.csv`` ·
  ``data/generated/abox/abox_stats.json`` · ``data/generated/tbox/t_box.ttl``

CLI 로도 쓸 수 있다::

    python scripts/build_table_class_map_report.py
"""
from __future__ import annotations

import collections
import csv
import html
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import (  # noqa: E402 — sys.path 조정 후 import
    GENERATED_ABOX_DIR,
    GENERATED_REPORTS_DIR,
    SOURCE_RAWDATA_DIR,
    TBOX_PATH,
)

#: 도구가 이 이름으로 산출 경로를 읽는다.
_OUT = os.path.join(GENERATED_REPORTS_DIR, "table_class_map.html")


def _load_abox_stats() -> dict:
    """``abox_stats.json`` 의 ``per_class``. 없으면 빈 dict (A-Box 미생성)."""
    path = os.path.join(GENERATED_ABOX_DIR, "abox_stats.json")
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("per_class") or {}
    except Exception:                                    # noqa: BLE001
        return {}


def _csv_shape(table: str) -> tuple[list[str], int]:
    """``(헤더, 데이터 행 수)``. 파일이 없으면 ``([], 0)``.

    ``utf-8-sig``: BOM 이 첫 컬럼명에 섞이면 매핑 검토가 통째로 어긋난다.
    """
    path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
    if not os.path.exists(path):
        return [], 0
    try:
        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, []) or []
            return [c.strip() for c in header if c.strip()], sum(1 for _ in reader)
    except Exception:                                    # noqa: BLE001
        return [], 0


def _tbox_classes() -> set[str]:
    """T-Box 에 선언된 도메인 클래스 local name 집합."""
    try:
        from rdflib import RDF, Graph
        from rdflib.namespace import OWL

        from domain.namespaces import DOMAIN_NS
        graph = Graph()
        graph.parse(TBOX_PATH, format="turtle")
        return {
            str(c)[len(DOMAIN_NS):]
            for c in graph.subjects(RDF.type, OWL.Class)
            if str(c).startswith(DOMAIN_NS)
        }
    except Exception:                                    # noqa: BLE001
        return set()


def _abstract_parents(classes: set[str]) -> set[str]:
    """자식이 있고 인스턴스가 없는 클래스 (추상 상위) — 원천 없음이 정상."""
    try:
        from rdflib import RDFS, Graph

        from domain.namespaces import DOMAIN_NS
        graph = Graph()
        graph.parse(TBOX_PATH, format="turtle")
        parents = {
            str(o)[len(DOMAIN_NS):]
            for _s, o in graph.subject_objects(RDFS.subClassOf)
            if str(o).startswith(DOMAIN_NS)
        }
        return parents & classes
    except Exception:                                    # noqa: BLE001
        return set()


def collect() -> dict:
    """보고서 데이터 수집. 도구가 기대하는 키를 모두 채운다."""
    from domain.table_mapping import load_table_class_mapping, load_table_pk_columns

    table_class = load_table_class_mapping()
    pk_columns = load_table_pk_columns()
    per_class = _load_abox_stats()

    rows: list[dict] = []
    column_owners: dict[str, list[str]] = collections.defaultdict(list)
    total_columns = total_rows = total_instances = total_links = 0

    for table in sorted(table_class):
        cls = table_class[table]
        header, row_count = _csv_shape(table)
        stats = per_class.get(cls, {})
        instances = int(stats.get("instance_count") or 0)
        links = int(stats.get("op_triples") or 0)
        for column in header:
            column_owners[column].append(table)
        total_columns += len(header)
        total_rows += row_count
        total_instances += instances
        total_links += links
        rows.append({
            "base": table,
            "table": table,
            "cls": cls,
            "columns": len(header),
            "rows": row_count,
            "pk": ", ".join(pk_columns.get(table, [])) or "(heuristic)",
            "instances": instances,
            "properties": int(stats.get("dp_triples") or 0),
            "links": links,
        })

    declared = _tbox_classes()
    mapped_classes = set(table_class.values())
    without_source = sorted(declared - mapped_classes)
    abstract = sorted(set(without_source) & _abstract_parents(declared))
    derived = [c for c in without_source if c not in abstract]

    return {
        "rows": rows,
        "total_columns": total_columns,
        "total_rows": total_rows,
        "total_instances": total_instances,
        "total_links": total_links,
        # 같은 컬럼명이 2개 이상 테이블에 등장 — 값 의미가 다를 수 있어 집계 오답 위험.
        "shared_columns": {
            col: sorted(tables)
            for col, tables in sorted(column_owners.items())
            if len(tables) > 1
        },
        "derived": derived,
        "abstract": abstract,
    }


def _esc(value: object) -> str:
    return html.escape(str(value))


def render(data: dict) -> str:
    """검토용 단일 HTML. 외부 의존 없음 (오프라인 열람 가능)."""
    mismatched = {r["base"] for r in data["rows"] if r["rows"] != r["instances"]}
    body = [
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>",
        "<title>테이블 → 클래스 매핑 검토</title><style>",
        "body{font-family:'Apple SD Gothic Neo','Malgun Gothic',sans-serif;",
        "margin:24px;color:#222}h1{font-size:20px}h2{font-size:16px;margin-top:28px}",
        "table{border-collapse:collapse;width:100%;font-size:13px}",
        "th,td{border:1px solid #ddd;padding:6px 8px;text-align:left}",
        "th{background:#f4f6f8}tr.warn{background:#fff4f4}",
        "code{background:#f4f6f8;padding:1px 4px;border-radius:3px}",
        ".sum{display:flex;gap:24px;margin:12px 0;font-size:13px}",
        ".sum div{background:#f4f6f8;padding:8px 12px;border-radius:4px}",
        "</style></head><body>",
        "<h1>원천 테이블 → 온톨로지 클래스 매핑 검토</h1>",
        "<div class='sum'>",
        f"<div>테이블 <b>{len(data['rows'])}</b></div>",
        f"<div>컬럼 <b>{data['total_columns']:,}</b></div>",
        f"<div>원천 행 <b>{data['total_rows']:,}</b></div>",
        f"<div>인스턴스 <b>{data['total_instances']:,}</b></div>",
        f"<div>관계 트리플 <b>{data['total_links']:,}</b></div>",
        "</div>",
    ]

    if mismatched:
        body.append(
            f"<p><b>행 수 ≠ 인스턴스 수: {len(mismatched)}개 테이블</b> — "
            "적재 누락 또는 composite PK 로 인한 병합 가능성 (아래 붉은 행).</p>",
        )
    body.append("<h2>테이블별 매핑</h2><table><tr>"
                "<th>테이블</th><th>클래스</th><th>컬럼</th><th>원천 행</th>"
                "<th>PK</th><th>인스턴스</th><th>관계</th></tr>")
    for row in data["rows"]:
        cls_attr = " class='warn'" if row["base"] in mismatched else ""
        body.append(
            f"<tr{cls_attr}><td><code>{_esc(row['table'])}</code></td>"
            f"<td>{_esc(row['cls'])}</td><td>{row['columns']}</td>"
            f"<td>{row['rows']:,}</td><td>{_esc(row['pk'])}</td>"
            f"<td>{row['instances']:,}</td><td>{row['links']:,}</td></tr>",
        )
    body.append("</table>")

    shared = data["shared_columns"]
    body.append(f"<h2>동명 컬럼 ({len(shared)}개)</h2>")
    if shared:
        body.append("<p>같은 이름이 여러 테이블에 있다 — 값의 의미가 다르면 집계가 "
                    "섞인다.</p><table><tr><th>컬럼</th><th>테이블</th></tr>")
        for col, tables in list(shared.items())[:200]:
            body.append(f"<tr><td><code>{_esc(col)}</code></td>"
                        f"<td>{_esc(', '.join(tables))}</td></tr>")
        body.append("</table>")
    else:
        body.append("<p>없음.</p>")

    body.append("<h2>원천 테이블이 없는 클래스</h2><table><tr>"
                "<th>구분</th><th>클래스</th></tr>")
    body.append(f"<tr><td>추상 상위 (정상)</td><td>{_esc(', '.join(data['abstract']) or '없음')}</td></tr>")
    body.append(f"<tr><td>코드 마스터 승격 등</td><td>{_esc(', '.join(data['derived']) or '없음')}</td></tr>")
    body.append("</table></body></html>")
    return "\n".join(body)


if __name__ == "__main__":
    payload = collect()
    os.makedirs(os.path.dirname(_OUT), exist_ok=True)
    with open(_OUT, "w", encoding="utf-8") as handle:
        handle.write(render(payload))
    print(f"{_OUT} ({len(payload['rows'])} tables)")
