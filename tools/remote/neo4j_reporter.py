"""Neo4j LPG 변환 보고서 HTML 렌더링.

tools/remote/neo4j.py의 2,379 LOC God Module에서 HTML 생성 책임(236 LOC)을 분리한다.
순수 함수 — verification/stats/nodes/relationships dict만 받아 HTML 문자열을 리턴.
Neo4j 연결이나 RDF 파싱을 수행하지 않으므로 단위 테스트가 쉽다.

CQ id·클래스명·관계 타입·소스 경로·도메인 이름은 사용자 입력이나 LLM 출력에서 온 값이다.
문자열로 싣는 값은 모두 ``_esc`` 를 거친다. 숫자 형식 지정(``:,``)을 거치는 값은 문자열이
올 수 없으므로 그대로 둔다.
"""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime

from domain.namespaces import DOMAIN_CONFIG


def _esc(value: object) -> str:
    """임의 값을 HTML 텍스트·속성 값으로 escape 한다."""
    return html.escape(str(value))


def build_cq_section_html(verification: dict) -> str:
    """CQ 커버리지 HTML 섹션."""
    cq = verification.get("cq_coverage", {})
    if cq.get("status") == "skip":
        return ""
    total = cq.get("total", 0)
    passed = cq.get("passed", 0)
    rate = cq.get("pass_rate", 0)
    color = "#27ae60" if rate >= 90 else ("#f39c12" if rate >= 70 else "#e74c3c")
    rows = ""
    for r in cq.get("results", []):
        st = "ok" if r["passed"] else "fail"
        cls_icon = "O" if r["classes_exist"] else "X"
        conn_icon = "O" if r["connected"] else "X"
        rows += (
            f'<tr><td>{_esc(r["id"])}</td>'
            f'<td><span class="chip {st}">{"PASS" if r["passed"] else "FAIL"}</span></td>'
            f'<td>{cls_icon}</td><td>{conn_icon}</td></tr>'
        )
    return f"""
<div class="section">
    <h2>CQ 커버리지 (LPG 응답 가능성)</h2>
    <div style="text-align:center;margin-bottom:12px;">
        <span style="font-size:32px;font-weight:800;color:{color};">{_esc(passed)}/{_esc(total)}</span>
        <span style="font-size:14px;color:#636e72;"> ({_esc(rate)}%)</span>
    </div>
    <table><tr><th>CQ</th><th>상태</th><th>클래스 존재</th><th>연결성</th></tr>{rows}</table>
</div>"""


def build_lpg_report_html(
    source_file: str, rdf_triples: int, nodes: dict, relationships: list,
    verification: dict, stats: dict,
) -> str:
    """LPG 변환 보고서 HTML 렌더링.

    Args:
        source_file: 원본 TTL 경로(표시용).
        rdf_triples: 원본 RDF 트리플 수.
        nodes: {node_id: node_dict}
        relationships: [(src_id, tgt_id, rel_type), ...]
        verification: verify_lpg 결과 dict (verified/coverage/class_distribution 등).
        stats: 변환 통계 dict (excluded_axiom 등).

    Returns:
        단일 HTML 문자열.
    """
    domain_name = _esc(DOMAIN_CONFIG["domain"]["name"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    verified = verification["verified"]
    v_class = "pass" if verified else "fail"
    v_text = "PASS" if verified else "FAIL"
    cov = verification["coverage"]

    cov_pct = cov["percent"]
    cov_color = "green" if cov_pct >= 99 else ("yellow" if cov_pct >= 95 else "red")

    cd = verification["class_distribution"]
    cd_chip = f'<span class="chip ok">{_esc(cd["matched"])}/{_esc(cd["total"])}</span>'
    cd_rows = "".join(
        f'<tr><td>{_esc(m["class"])}</td><td>{_esc(m["rdf"])}</td><td>{_esc(m["lpg"])}</td></tr>'
        for m in cd.get("mismatches", [])
    )

    dr = verification["domain_relationships"]
    dr_chip = f'<span class="chip ok">{_esc(dr["matched"])}/{_esc(dr["total"])}</span>'
    dr_rows = "".join(
        f'<tr><td>{_esc(m["rel"])}</td><td>{_esc(m["rdf"])}</td><td>{_esc(m["lpg"])}</td></tr>'
        for m in dr.get("mismatches", [])
    )

    iof = verification.get("iof_relationships", {})
    iof_rows = "".join(f"<tr><td>{_esc(r)}</td><td>{c:,}</td></tr>" for r, c in iof.items())

    rel_dist: Counter = Counter()
    for _, _, rt in relationships:
        rel_dist[rt] += 1
    rel_rows = "".join(
        f"<tr><td>{_esc(r)}</td><td>{c:,}</td></tr>"
        for r, c in rel_dist.most_common(20)
    )

    sc = verification["spot_check"]
    sc_chip_class = "ok" if sc["passed"] == sc["total"] else "fail"

    qs = verification.get("quality_score", {})
    qs_total = qs.get("total", 0)
    qs_total_color = "#27ae60" if qs_total >= 95 else ("#f39c12" if qs_total >= 80 else "#e74c3c")

    def _dim_html(key: str, label: str, icon: str) -> str:
        dim = qs.get(key, {})
        s = dim.get("score", 0) or 0
        w = dim.get("weight", 0)
        details = dim.get("details", {})
        color = "#27ae60" if s >= 95 else ("#f39c12" if s >= 80 else "#e74c3c")
        if key == "query_equivalence" and s is None:
            return (
                f'<div class="stat-card"><div class="num" style="color:#b2bec3;">N/A</div>'
                f'<div class="label">{icon} {label} ({int(w*100)}%)</div>'
                f'<div style="font-size:11px;color:#b2bec3;">Neo4j 연결 필요</div></div>'
            )
        detail_items = "".join(
            f'<div style="font-size:11px;color:#636e72;">{_esc(k)}: {_esc(v)}</div>'
            for k, v in details.items()
            if not isinstance(v, list | dict) and k != "status"
        )
        return (
            f'<div class="stat-card"><div class="num" style="color:{color};">{_esc(s)}</div>'
            f'<div class="label">{icon} {label} ({int(w*100)}%)</div>{detail_items}</div>'
        )

    quality_dims_html = "".join([
        _dim_html("completeness", "Completeness", ""),
        _dim_html("faithfulness", "Faithfulness", ""),
        _dim_html("query_equivalence", "Query Equiv.", ""),
        _dim_html("consistency", "Consistency", ""),
        _dim_html("conciseness", "Conciseness", ""),
    ])

    cd_table_html = (
        "" if not cd.get("mismatches")
        else '<h3 style="font-size:13px;margin:8px 0;">클래스 불일치</h3>'
             '<table><tr><th>클래스</th><th>RDF</th><th>LPG</th></tr>'
             + cd_rows + '</table>'
    )
    dr_table_html = (
        "" if not dr.get("mismatches")
        else '<h3 style="font-size:13px;margin:8px 0;">관계 불일치</h3>'
             '<table><tr><th>관계</th><th>RDF</th><th>LPG</th></tr>'
             + dr_rows + '</table>'
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RDF → LPG 변환 보고서 — {domain_name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
    background: #f5f6fa; color: #2d3436; line-height: 1.6;
}}
.container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
.header {{
    background: linear-gradient(135deg, #2c3e50 0%, #8e44ad 100%);
    color: white; padding: 24px 32px; border-radius: 12px; margin-bottom: 24px;
}}
.header h1 {{ font-size: 22px; font-weight: 700; }}
.header .meta {{ font-size: 13px; opacity: 0.8; margin-top: 4px; }}
.section {{
    background: white; border-radius: 10px; padding: 20px 24px;
    margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}}
.section h2 {{
    font-size: 16px; font-weight: 700; color: #2c3e50;
    margin-bottom: 14px; padding-bottom: 8px; border-bottom: 2px solid #f0f0f0;
}}
.stats-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px; margin-bottom: 16px;
}}
.stat-card {{
    background: #f8f9fa; border-radius: 8px; padding: 14px 16px; text-align: center;
}}
.stat-card .num {{ font-size: 28px; font-weight: 800; color: #2c3e50; }}
.stat-card .label {{ font-size: 12px; color: #636e72; margin-top: 2px; }}
.stat-card.blue .num {{ color: #3498db; }}
.stat-card.green .num {{ color: #27ae60; }}
.stat-card.orange .num {{ color: #e67e22; }}
.stat-card.purple .num {{ color: #8e44ad; }}
.stat-card.teal .num {{ color: #16a085; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }}
th {{ text-align: left; padding: 8px 10px; background: #f8f9fa; color: #636e72; font-weight: 600; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #f0f0f0; }}
.chip {{
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 600; margin: 2px 3px;
}}
.chip.ok {{ background: #d5f5e3; color: #27ae60; }}
.chip.fail {{ background: #fadbd8; color: #e74c3c; }}
.verdict {{
    display: inline-block; padding: 4px 16px; border-radius: 20px;
    font-weight: 700; font-size: 14px; letter-spacing: 1px;
}}
.verdict.pass {{ background: #d5f5e3; color: #27ae60; }}
.verdict.fail {{ background: #fadbd8; color: #e74c3c; }}
.coverage-bar {{
    background: #ecf0f1; border-radius: 6px; height: 24px; position: relative;
    overflow: hidden; margin: 6px 0;
}}
.coverage-fill {{
    height: 100%; border-radius: 6px; display: flex; align-items: center;
    justify-content: center; font-size: 12px; font-weight: 700; color: white;
}}
.coverage-fill.green {{ background: #27ae60; }}
.coverage-fill.yellow {{ background: #f39c12; }}
.coverage-fill.red {{ background: #e74c3c; }}
.two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 768px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
.footer {{ text-align: center; color: #b2bec3; font-size: 12px; padding: 16px; }}
</style>
</head>
<body>
<div class="container">

<div class="header">
    <h1>RDF → LPG 변환 보고서</h1>
    <div class="meta">{domain_name} | {now} | <span class="verdict {v_class}">{v_text}</span></div>
</div>

<div class="section">
    <h2>변환 요약</h2>
    <div class="stats-grid">
        <div class="stat-card blue"><div class="num">{rdf_triples:,}</div><div class="label">RDF 트리플</div></div>
        <div class="stat-card purple"><div class="num">{len(nodes):,}</div><div class="label">LPG 노드</div></div>
        <div class="stat-card teal"><div class="num">{len(relationships):,}</div><div class="label">LPG 관계</div></div>
        <div class="stat-card orange"><div class="num">{stats['excluded_axiom']:,}</div><div class="label">제외 (OWL Axiom)</div></div>
        <div class="stat-card green"><div class="num">{verification['iof_total']:,}</div><div class="label">IOF 추론 관계</div></div>
        <div class="stat-card"><div class="num">{_esc(verification['class_hierarchy'])}</div><div class="label">클래스 계층</div></div>
    </div>
    <p style="font-size:13px;color:#636e72;">소스: <code>{_esc(source_file)}</code></p>
</div>

<div class="section">
    <h2>트리플 커버리지</h2>
    <div class="coverage-bar">
        <div class="coverage-fill {cov_color}" style="width:{_esc(cov_pct)}%">{_esc(cov_pct)}%</div>
    </div>
    <p style="font-size:12px;color:#636e72;margin-top:4px;">
        LPG 반영: {cov['lpg_triples']:,} / 인스턴스 데이터: {cov['rdf_instance_triples']:,}
        | 제외: {cov['excluded']:,}개 (OWL axiom + BNode + 메타 rdf:type)
    </p>
</div>

<div class="section">
    <h2>LPG 변환 품질 점수</h2>
    <div style="text-align:center;margin-bottom:16px;">
        <span style="font-size:48px;font-weight:800;color:{qs_total_color};">{_esc(qs_total)}</span>
        <span style="font-size:18px;color:#636e72;"> / 100</span>
    </div>
    <div class="stats-grid" style="grid-template-columns: repeat(5, 1fr);">
        {quality_dims_html}
    </div>
    <p style="font-size:11px;color:#b2bec3;margin-top:8px;">
        Zaveri(2016), Angles(2020), Kontokostas(2014), Färber(2018), Hartig(2014)
    </p>
</div>

<div class="section">
    <h2>검증 결과</h2>
    <div class="stats-grid">
        <div class="stat-card">
            <div class="label">클래스 분포</div>{cd_chip}
        </div>
        <div class="stat-card">
            <div class="label">도메인 관계</div>{dr_chip}
        </div>
        <div class="stat-card">
            <div class="label">프로퍼티 스팟체크</div>
            <span class="chip {sc_chip_class}">{_esc(sc['passed'])}/{_esc(sc['total'])}</span>
        </div>
        <div class="stat-card">
            <div class="label">어노테이션</div>
            <span class="chip ok">ko:{_esc(verification['labels']['ko'])}</span>
            <span class="chip ok">en:{_esc(verification['labels']['en'])}</span>
        </div>
    </div>
    {cd_table_html}
    {dr_table_html}
</div>

<div class="two-col">
    <div class="section">
        <h2>IOF 추론 관계 ({verification['iof_total']:,}개)</h2>
        <table><tr><th>관계 타입</th><th>수</th></tr>{iof_rows}</table>
    </div>
    <div class="section">
        <h2>관계 타입 분포 (Top 20)</h2>
        <table><tr><th>관계 타입</th><th>수</th></tr>{rel_rows}</table>
    </div>
</div>

{build_cq_section_html(verification)}

<div class="footer">ontology-agent LPG converter | {now}</div>

</div>
</body>
</html>"""


__all__ = ["build_cq_section_html", "build_lpg_report_html"]
