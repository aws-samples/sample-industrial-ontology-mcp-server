"""파이프라인 최종 보고서 생성 도구

FULL_PIPELINE 완료 후 전체 산출물을 분석하여
HTML 보고서를 생성한다.
"""

import glob
import html
import json
import logging
import os
import webbrowser
from datetime import datetime
from pathlib import Path

from rdflib import OWL, RDF, RDFS, BNode, URIRef

from config import (
    ABOX_PATH,
    GENERATED_DIR,
    GENERATED_REPORTS_DIR,
    INFERRED_PATH,
    SEMANTIC_DICT_PATH,
    SOURCE_RAWDATA_DIR,
    SOURCE_TACIT_DIR,
    TBOX_PATH,
    resolve_generated_path,
)
from domain.namespaces import DOMAIN_CONFIG, DOMAIN_NS
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name

logger = logging.getLogger(__name__)

#: ``_build_html`` 은 로컬 변수 ``html`` 에 문서를 누적하므로 그 함수 안에서
#: ``html.escape`` 는 **AttributeError** 다 (str 에 escape 가 없다). 실측
#: 2026-08-22: golden_history.json 에 regression 이 있는 상태로
#: ``generate_pipeline_report`` 를 돌리면 보고서 생성이 통째로 실패했다
#: (``'str' object has no attribute 'escape'``) — 회귀가 있을 때만 발화하는
#: 경로라 지금까지 아무도 밟지 않았다. 모듈 레벨 별칭은 shadowing 과 무관하다.
_escape = html.escape


def _esc(value: object) -> str:
    """보고서에 싣는 임의 값을 HTML 텍스트·속성 값으로 escape 한다.

    사이드카 JSON·T-Box·CSV 에서 온 값은 문자열이든 숫자든 ``str`` 로 바꾼 뒤 escape
    한다. 숫자 형식 지정(``:,`` / ``:.1f``)을 거치는 값은 문자열이 올 수 없으므로 제외한다.
    """
    return _escape(str(value))


def _file_info(path):
    """파일 존재 여부, 크기, 수정 시간을 반환."""
    if not os.path.exists(path):
        return {"exists": False, "path": path}
    stat = os.stat(path)
    return {
        "exists": True,
        "path": path,
        "size_bytes": stat.st_size,
        "size_display": _human_size(stat.st_size),
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _human_size(size):
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _collect_tbox_stats(path):
    """T-Box 통계를 수집한다."""
    if not os.path.exists(path):
        return None
    g = _new_graph()
    g.parse(path, format="turtle")

    classes = [c for c in g.subjects(RDF.type, OWL.Class)
               if isinstance(c, URIRef) and str(c).startswith(DOMAIN_NS)]
    obj_props = [p for p in g.subjects(RDF.type, OWL.ObjectProperty)
                 if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS)]
    dt_props = [p for p in g.subjects(RDF.type, OWL.DatatypeProperty)
                if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS)]

    # Class hierarchy groups
    hierarchy = {}
    for cls in classes:
        parents = [str(p) for p in g.objects(cls, RDFS.subClassOf)
                   if not isinstance(p, BNode)]
        parent = _local_name(parents[0]) if parents else "기타"
        hierarchy.setdefault(parent, []).append(_local_name(cls))

    return {
        "triples": len(g),
        "classes": len(classes),
        "object_properties": len(obj_props),
        "datatype_properties": len(dt_props),
        "hierarchy_groups": len(hierarchy),
        "hierarchy": {k: sorted(v) for k, v in sorted(hierarchy.items())},
    }


def _collect_abox_stats(path):
    """A-Box 통계를 수집한다 (전체 파싱 없이 라인/트리플 수만)."""
    if not os.path.exists(path):
        return None
    line_count = 0
    with open(path, encoding="utf-8") as f:
        for _ in f:
            line_count += 1
    return {"lines": line_count}


def _collect_inferred_stats(path):
    """추론 결과 통계를 수집한다."""
    if not os.path.exists(path):
        return None
    line_count = 0
    with open(path, encoding="utf-8") as f:
        for _ in f:
            line_count += 1
    return {"lines": line_count}


def _collect_tacit_stats(tacit_dir):
    """암묵지 파일 통계를 수집한다."""
    if not os.path.isdir(tacit_dir):
        return {"count": 0, "files": []}
    ttl_files = sorted(glob.glob(os.path.join(tacit_dir, "*.ttl")))
    files = []
    total_triples = 0
    for path in ttl_files:
        g = _new_graph()
        g.parse(path, format="turtle")
        count = len(g)
        total_triples += count
        files.append({
            "name": os.path.basename(path),
            "triples": count,
            "size": _human_size(os.path.getsize(path)),
        })
    return {"count": len(files), "files": files, "total_triples": total_triples}


def _collect_dict_validation(dict_path, tbox_path, abox_path):
    """시맨틱 딕셔너리 검증 결과를 수집한다."""
    if not os.path.exists(dict_path):
        return None
    from tools.semantic_dict_validation import _validate
    with open(dict_path, encoding="utf-8") as f:
        dict_data = json.load(f)
    return _validate(dict_data, tbox_path, abox_path)


def _collect_dict_stats(dict_path):
    """시맨틱 딕셔너리 내부 통계를 수집한다."""
    if not os.path.exists(dict_path):
        return None
    with open(dict_path, encoding="utf-8") as f:
        d = json.load(f)
    return {
        "classes": len(d.get("classes", {})),
        "object_properties": len(d.get("object_properties", {})),
        "functional_properties": len(d.get("functional_properties", {})),
        "anti_patterns": len(d.get("sparql_guide", {}).get("anti_patterns", {})),
        "common_patterns": len(d.get("sparql_guide", {}).get("common_patterns", {})),
        "question_templates": len(d.get("question_templates", {})),
    }


def _generate_tbox_visualization(_out_dir: str) -> str:
    """T-Box 시각화 HTML을 생성하고 경로를 반환한다."""
    if not os.path.exists(TBOX_PATH):
        return ""
    from tools.visualization import _build_html as _build_vis_html
    from tools.visualization import _extract_tbox_data
    g = _new_graph()
    g.parse(TBOX_PATH, format="turtle")
    data = _extract_tbox_data(g)
    html = _build_vis_html(data)
    vis_path = resolve_generated_path("reports/tbox_visualization.html")
    vis_path.parent.mkdir(parents=True, exist_ok=True)
    with open(vis_path, "w", encoding="utf-8") as f:
        f.write(html)
    return str(vis_path)


def _collect_golden_queries_result() -> dict:
    """최신 golden_history.json entry 를 읽어 S13 보고서 섹션 데이터 생성 (X1).

    Returns:
        dict: {status, message?, timestamp?, summary?, regression?, results_count?}
            status: "not_configured" | "executed" | "error"
    """
    path = os.path.join(GENERATED_REPORTS_DIR, "golden_history.json")
    if not os.path.exists(path):
        return {"status": "not_configured", "message": "Golden queries 미설정"}
    try:
        with open(path, encoding="utf-8") as f:
            history = json.load(f)
        if not history:
            return {"status": "not_configured", "message": "history 비어 있음"}
        latest = history[-1]
        return {
            "status": "executed",
            "timestamp": latest.get("timestamp"),
            "summary": latest.get("summary"),
            "regression": latest.get("regression", {}),
            "results_count": len(latest.get("results", [])),
        }
    except (OSError, json.JSONDecodeError) as e:
        return {"status": "error", "message": str(e)}


def _collect_all_data():
    """모든 산출물에서 보고서 데이터를 수집한다."""
    data = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "artifacts": {
            "tbox": _file_info(TBOX_PATH),
            "abox": _file_info(ABOX_PATH),
            "inferred": _file_info(INFERRED_PATH),
            "semantic_dict": _file_info(SEMANTIC_DICT_PATH),
        },
        "source": {
            "csv_count": len(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))),
        },
        "tbox_stats": _collect_tbox_stats(TBOX_PATH),
        "abox_stats": _collect_abox_stats(ABOX_PATH),
        "inferred_stats": _collect_inferred_stats(INFERRED_PATH),
        "tacit_stats": _collect_tacit_stats(SOURCE_TACIT_DIR),
        "dict_stats": _collect_dict_stats(SEMANTIC_DICT_PATH),
        "dict_validation": _collect_dict_validation(SEMANTIC_DICT_PATH, TBOX_PATH, ABOX_PATH),
    }
    # Loss budget for Information Preservation section
    loss_budget = None
    try:
        from tools.kg_validation import _load_loss_budget
        loss_budget = _load_loss_budget()
    except Exception as exc:
        logger.warning(
            "loss budget 로드 실패; 보고서의 Information Preservation 값을 비운다: %s",
            exc,
        )
    data["loss_budget"] = loss_budget
    # Golden queries result (X1)
    data["golden_queries"] = _collect_golden_queries_result()
    return data


def _render_meta_audit_section(audit_path: str) -> str:
    """Render meta-audit artifact as HTML section. Empty string if missing."""
    if not os.path.exists(audit_path):
        return ""
    try:
        with open(audit_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""

    parts = ['<section class="meta-audit"><h2>Meta-Audit</h2>']

    if data.get("history_insufficient"):
        parts.append('<p class="warn">History below minimum — correlation/dead-check '
                     'sections omitted.</p>')

    bs = data.get("blind_spots", [])
    if bs:
        parts.append("<h3>Blind Spots</h3><ul>")
        for s in bs:
            parts.append(f"<li><strong>{_esc(s['category'])}</strong>: {_esc(s['mutants'])} mutants, "
                         f"{_esc(s['caught_by_any_check'])} caught — {_esc(s['recommendation'])}</li>")
        parts.append("</ul>")

    dead = data.get("dead_checks", [])
    if dead:
        parts.append("<h3>Dead-Check Candidates</h3><ul>")
        for d in dead:
            parts.append(f"<li>{_esc(d['check'])} — {_esc(d['recommendation'])}</li>")
        parts.append("</ul>")

    corr = data.get("pairwise_correlation", [])
    if corr:
        parts.append("<h3>Redundant-Check Candidates (cooccurrence ≥ 0.9)</h3><ul>")
        for p in corr:
            src = p.get("source", "mixed")
            parts.append(f"<li>{_esc(p['pair'][0])} ↔ {_esc(p['pair'][1])}: "
                         f"{_esc(p['cooccurrence'])} (n={_esc(p['sample_size'])}, source={_esc(src)}) — "
                         f"{_esc(p['interpretation'])}</li>")
        parts.append("</ul>")

    matrix = data.get("sensitivity_matrix", {})
    if matrix:
        cats = sorted({cat for row in matrix.values() for cat in row})
        parts.append("<h3>Sensitivity Matrix</h3><table><thead><tr><th>Check</th>")
        for c in cats:
            parts.append(f"<th>{_esc(c)}</th>")
        parts.append("</tr></thead><tbody>")
        for check in sorted(matrix):
            parts.append(f"<tr><td>{_esc(check)}</td>")
            for c in cats:
                v = matrix[check].get(c, 0.0)
                parts.append(f"<td>{v:.2f}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")

    parts.append("</section>")
    return "".join(parts)


def _render_meta_audit_playbook() -> str:
    """X3: S13 보고서에 meta_audit action_playbook 섹션 렌더.

    ``data/generated/meta_audit/latest.json`` 에서 ``action_playbook`` 배열을
    읽어 우선순위별 (high 빨강 / medium 주황 / low 회색) 테이블 생성.

    - 파일 없음 → 빈 문자열
    - playbook 키 없음 또는 비어있음 → "검증 체계 블라인드 스팟 없음" info 박스
    - playbook 있음 → 우선순위 테이블 (TOP 10)
    """
    audit_latest = os.path.join(GENERATED_DIR, "meta_audit", "latest.json")
    if not os.path.exists(audit_latest):
        return ""
    try:
        with open(audit_latest, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return ""

    playbook = data.get("action_playbook", [])

    if not playbook:
        return (
            '<div class="section">'
            '<h2>🛡️ 검증 체계 Meta-Audit Playbook</h2>'
            '<div style="background:#d5f5e3; border-radius:8px; padding:12px; '
            'color:#27ae60; font-size:13px;">'
            '<strong>검증 체계 블라인드 스팟 없음</strong> — 최근 mutation audit 에서 '
            'validator 가 모든 mutant 를 감지했습니다.'
            '</div>'
            '</div>'
        )

    # Priority 색상 매핑 (inline CSS 로 기존 .severity 스타일 재사용)
    priority_badge = {
        "high": '<span class="severity critical">HIGH</span>',
        "medium": '<span class="severity high">MEDIUM</span>',
        "low": '<span class="severity info" style="background:#95a5a6">LOW</span>',
    }

    rows: list[str] = []
    # Show top 10 entries (already sorted by priority in compute_action_playbook)
    for entry in playbook[:10]:
        priority = entry.get("priority", "low")
        badge = priority_badge.get(priority,
                                    '<span class="severity info">?</span>')
        etype = html.escape(str(entry.get("type", "?")))
        issue = html.escape(str(entry.get("issue", "")))
        action = entry.get("suggested_action", {})
        kind = html.escape(str(action.get("kind", "?")))

        # Type-specific target: category / check / pair
        if entry.get("type") == "blind_spot":
            target = html.escape(str(entry.get("category", "?")))
        elif entry.get("type") == "dead_check":
            target = html.escape(str(entry.get("check", "?")))
        elif entry.get("type") == "correlation_gap":
            pair = entry.get("pair", ["?", "?"])
            target = f"{html.escape(str(pair[0]))} ↔ {html.escape(str(pair[1]))}"
        else:
            target = "?"

        rows.append(
            f'<tr><td>{badge}</td><td>{etype}</td><td>{target}</td>'
            f'<td>{issue}</td><td><code>{kind}</code></td></tr>'
        )

    overflow_note = ""
    if len(playbook) > 10:
        overflow_note = (
            f'<p style="font-size:11px; color:#b2bec3; margin-top:8px;">'
            f'+ {len(playbook) - 10} more entries in '
            f'<code>data/generated/meta_audit/latest.json</code></p>'
        )

    return (
        '<div class="section">'
        '<h2>🛡️ 검증 체계 Meta-Audit Playbook</h2>'
        '<p style="font-size:12px; color:#636e72; margin-bottom:8px;">'
        'Mutation audit 으로 발견된 validator blind-spot / dead-check / '
        'redundant pair 에 대한 구체 액션 제안. '
        '<code>suggested_action.kind</code> 별 판단 기준은 JSON 파일 참조.</p>'
        '<table><thead><tr>'
        '<th>우선순위</th><th>유형</th><th>대상</th><th>이슈</th><th>조치</th>'
        '</tr></thead><tbody>'
        + "".join(rows) +
        '</tbody></table>'
        + overflow_note +
        '</div>'
    )


#: S2 섹션이 없을 때 보여줄 안내. 섹션을 통째로 생략하지 않는 이유는, 보고서가
#: 조용히 짧아지면 운영자가 "토론이 잘 됐다" 로 오독하기 때문이다 — 기록이 없다는
#: 사실 자체가 판단 정보다 (첫 실행 / 구버전 산출물 / 기록 실패 구분 불가).
_debate_absent_html = (
    '<div class="section">'
    '<h2>🗣️ S2 다중 에이전트 토론 궤적</h2>'
    '<div style="background:#ecf0f1; border-radius:8px; padding:16px; '
    'color:#636e72; font-size:13px;">'
    '<strong>S2 토론 기록 없음</strong> — <code>data/generated/tbox/debate_log.json</code> '
    '이 없습니다. T-Box 를 <code>generate_tbox_collaborative</code> 로 생성하면 '
    '라운드별 궤적이 기록됩니다 (수동 T-Box / 단일 에이전트 생성 시에는 정상).'
    '</div></div>'
)


def _debate_verdict(summary: dict) -> tuple[str, str]:
    """토론 결과를 (배지 class, 라벨) 로. ``_build_debate_status_literal`` 과 같은 우선순위.

    우선순위를 맞추는 이유: T-Box 에 각인된 ``debate_status`` 와 보고서 배지가
    엇갈리면 어느 쪽이 정본인지 알 수 없다. 인프라 중단이 "미합의" 로 보이면
    **검토본이 미검토 초안으로 조용히 교체된 사고** 를 놓친다.
    """
    if summary.get("infra_abort"):
        return "fail", "인프라 중단 (라운드 미완주)"
    if summary.get("consensus_reached"):
        return "pass", "합의 도달"
    if summary.get("veto_lock_triggered"):
        return "fail", "미합의 — veto lock"
    return "fail", "미합의 — 라운드 소진"


def _render_debate_round_rows(rounds: list[dict]) -> str:
    """라운드별 궤적 표의 <tr> 들. 숫자가 없으면 '—' (0 으로 위장하지 않는다).

    값은 debate_log.json 에서 그대로 오므로 숫자 칸도 escape 해서 싣는다.
    """
    def num(value) -> str:
        return "—" if value is None else _esc(value)

    rows: list[str] = []
    for r in rounds:
        jury = r.get("jury")
        if jury:
            jury_cell = (
                f"{num(jury.get('requested'))} 요청 / "
                f"{num(jury.get('applied'))} 적용 / "
                f"{num(jury.get('noop'))} noop"
            )
            if jury.get("failed"):
                jury_cell += (
                    f' / <span style="color:#e74c3c;font-weight:600">'
                    f"{num(jury['failed'])} 실패</span>"
                )
        else:
            jury_cell = '<span class="na">미개입</span>'

        veto_v = r.get("veto_persistent_validator")
        veto_s = r.get("veto_persistent_sme")
        if r.get("veto_lock_released"):
            veto_cell = '<span style="color:#27ae60">해제</span>'
        elif veto_v is None and veto_s is None:
            veto_cell = "—"
        else:
            veto_cell = f"V {num(veto_v)} / S {num(veto_s)}"

        flags: list[str] = []
        if r.get("no_progress"):
            streak = r.get("no_progress_streak")
            flags.append(f'<span class="severity warning">정체'
                         f'{f" ×{_esc(streak)}" if streak else ""}</span>')
        if r.get("validator_parse_error") or r.get("sme_parse_error"):
            # 리뷰 유실 라운드를 "0 이슈 = 깨끗함" 으로 오독하면 안 된다.
            flags.append('<span class="severity critical">리뷰 파싱 실패</span>')
        if r.get("jury_final_production_ready") is False:
            flags.append('<span class="severity high">Jury: 미완성</span>')

        cq = r.get("cq_coverage_pct")
        cq_cell = "—" if cq is None else f"{_esc(cq)}%"
        approved = ("V " + ("✓" if r.get("validator_approved") else "✗")
                    + " / S " + ("✓" if r.get("sme_approved") else "✗"))

        rows.append(
            f"<tr><td><strong>R{num(r.get('round'))}</strong></td>"
            f"<td>{num(r.get('validator_issues'))} "
            f"<span style=\"color:#e67e22\">({num(r.get('validator_critical_high'))} c/h)</span></td>"
            f"<td>{num(r.get('sme_issues'))} "
            f"<span style=\"color:#e67e22\">({num(r.get('sme_critical_high'))} c/h)</span></td>"
            f"<td>{approved}</td>"
            f"<td>{cq_cell}</td>"
            f"<td>{num(r.get('triples'))}</td>"
            f"<td>{jury_cell}</td>"
            f"<td>{veto_cell}</td>"
            f"<td>{' '.join(flags) if flags else '—'}</td></tr>"
        )
    return "".join(rows)


def _render_debate_recurring_rows(recurring: list[dict]) -> str:
    """2라운드 이상 잔존한 이슈 표의 <tr> 들. LLM 텍스트는 반드시 escape."""
    sev_class = {"critical": "critical", "high": "high",
                 "medium": "warning", "low": "info"}
    rows: list[str] = []
    for item in recurring[:15]:
        sev = str(item.get("max_severity", "") or "low").lower()
        badge = (f'<span class="severity {sev_class.get(sev, "info")}">'
                 f'{html.escape(sev or "?")}</span>')
        seen = item.get("rounds_seen", 0)
        rnds = ", ".join(f"R{r}" for r in item.get("rounds", []))
        rows.append(
            f"<tr><td>{badge}</td>"
            f"<td>{html.escape(str(item.get('category', '')))}</td>"
            f"<td>{html.escape(str(item.get('target', ''))[:70])}</td>"
            f"<td>{html.escape('/'.join(str(a) for a in item.get('agents', [])))}</td>"
            f"<td><strong>{_esc(seen)}회</strong> ({html.escape(rnds)})</td>"
            f"<td style=\"font-size:11px;color:#636e72\">"
            f"{html.escape(str(item.get('summary', ''))[:110])}</td></tr>"
        )
    return "".join(rows)


def _render_debate_section() -> str:
    """S13 보고서에 S2 토론 궤적 섹션 렌더.

    사용자의 원 질문은 "토론 과정에서 문제가 잘 수정되고 있는지 모르겠다" 였다.
    그 질문에 답하는 것만 고른다:

    1. **라운드별 궤적 표** — critical/high 이슈 수, 승인 여부, CQ 커버리지,
       T-Box triples, Jury 개입 결과(적용/noop/실패), veto 잔존, 정체 플래그.
       이슈 수가 줄지 않는데 CQ 커버리지만 오르는 패턴이 한눈에 보인다.
    2. **반복 이슈 표** — 같은 target+category 가 몇 라운드 잔존했는가.
       veto 판정과 같은 축이므로 배지와 모순되지 않는다.
    3. **판정 배지** — T-Box 에 각인된 ``debate_status`` 와 같은 우선순위.

    데이터는 :func:`tools.debate_log_store.summarize_latest_run` 하나만 읽는다
    (라운드 dict 판독 로직 사본을 만들지 않는다). 기록이 없으면
    :data:`_debate_absent_html` — 섹션을 조용히 생략하지 않는다.
    """
    try:
        from tools.debate_log_store import summarize_latest_run
        summary = summarize_latest_run()
    except Exception as exc:  # noqa: BLE001 — 보고서가 기록 때문에 죽지 않는다
        logger.warning("S2 토론 기록 판독 실패, 섹션 생략: %s", exc)
        return _debate_absent_html
    if not summary:
        return _debate_absent_html

    verdict_class, verdict_label = _debate_verdict(summary)
    rounds = summary.get("rounds") or []
    recurring = summary.get("recurring") or []

    duration = summary.get("duration_seconds")
    duration_cell = "—" if duration is None else f"{duration / 60:.0f}분"
    cq_values = [r.get("cq_coverage_pct") for r in rounds
                 if r.get("cq_coverage_pct") is not None]
    cq_first = f"{_esc(cq_values[0])}%" if cq_values else "—"
    cq_last = f"{_esc(cq_values[-1])}%" if cq_values else "—"

    stats = summary.get("statistics") or {}
    init = summary.get("architect_initial") or {}

    infra_note = ""
    if summary.get("infra_abort"):
        abort = summary["infra_abort"]
        infra_note = (
            '<div style="background:#fadbd8;padding:10px;border-radius:6px;'
            'color:#e74c3c;font-size:12px;margin-bottom:10px">'
            f'<strong>⚠ 인프라 장애로 중단</strong> — phase='
            f'{html.escape(str(abort.get("phase", "?")))}, round='
            f'{html.escape(str(abort.get("round", "?")))}. '
            'T-Box 는 저장됐지만 예정 라운드를 다 돌지 못했습니다 — 정상 산출물로 '
            '취급하지 말고 S2 재실행을 검토하세요.</div>'
        )

    veto_note = ""
    targets = summary.get("veto_persistent_targets") or []
    if targets:
        veto_note = (
            '<div style="background:#fdebd0;padding:8px;border-radius:6px;'
            'color:#b9770e;font-size:11px;margin-top:8px">'
            '<strong>veto 잔존 대상:</strong> '
            + ", ".join(html.escape(str(t)[:80]) for t in targets[:5])
            + '</div>'
        )

    if recurring:
        recurring_html = (
            '<table><thead><tr><th>심각도</th><th>카테고리</th><th>대상</th>'
            '<th>제기자</th><th>잔존</th><th>증상</th></tr></thead><tbody>'
            + _render_debate_recurring_rows(recurring)
            + '</tbody></table>'
            + (f'<p style="font-size:11px;color:#b2bec3;margin-top:6px">'
               f'+ {len(recurring) - 15}건 더 — '
               f'<code>data/generated/tbox/debate_log.json</code></p>'
               if len(recurring) > 15 else "")
        )
    else:
        recurring_html = ('<p class="ok-text">2라운드 이상 잔존한 이슈 없음 — '
                          '지적이 라운드마다 해소됐습니다.</p>')

    return f"""
<div class="section">
    <h2>🗣️ S2 다중 에이전트 토론 궤적
        <span class="verdict {verdict_class}">{html.escape(verdict_label)}</span></h2>
    {infra_note}
    <div class="stats-grid">
        <div class="stat-card blue"><div class="num">{_esc(summary.get("total_rounds", 0))}</div>
            <div class="label">토론 라운드</div></div>
        <div class="stat-card"><div class="num">{duration_cell}</div>
            <div class="label">소요 시간</div></div>
        <div class="stat-card green"><div class="num">{cq_first} &rarr; {cq_last}</div>
            <div class="label">CQ 커버리지 (첫 &rarr; 끝)</div></div>
        <div class="stat-card orange"><div class="num">{len(recurring)}</div>
            <div class="label">2R+ 잔존 이슈</div></div>
        <div class="stat-card purple"><div class="num">{_esc(init.get("object_properties", 0))}
            &rarr; {_esc(stats.get("object_properties", 0))}</div>
            <div class="label">OP (초안 &rarr; 최종)</div></div>
        <div class="stat-card teal"><div class="num">{_esc(init.get("classes", 0))}
            &rarr; {_esc(stats.get("classes", 0))}</div>
            <div class="label">Class (초안 &rarr; 최종)</div></div>
    </div>

    <h3 style="font-size:13px;color:#636e72;margin:14px 0 6px">라운드별 궤적</h3>
    <table><thead><tr>
        <th>라운드</th><th>Validator 이슈</th><th>SME 이슈</th><th>승인</th>
        <th>CQ 커버리지</th><th>T-Box triples</th><th>Jury 개입</th>
        <th>veto 잔존</th><th>플래그</th>
    </tr></thead><tbody>{_render_debate_round_rows(rounds)}</tbody></table>
    {veto_note}

    <h3 style="font-size:13px;color:#636e72;margin:14px 0 6px">
        반복 지적 이슈 (target+category 축, 2라운드 이상)</h3>
    {recurring_html}

    <p style="font-size:11px;color:#b2bec3;margin-top:10px">
        기록: {html.escape(str(summary.get("timestamp", "N/A")))} |
        보관된 실행 {_esc(summary.get("runs_recorded", 0))}회 |
        distinct 이슈 {_esc(summary.get("recurring_distinct", 0))}건 |
        기록 당시 지문 축 반복 {_esc(summary.get("persistent_issue_count", 0))}건
        (S2 실행 시점에 계산된 값 — 지문 함수가 그 뒤 개선되면 위 표와 다를 수 있다.
        위 표는 현재 코드로 재계산한 값이다) |
        <code>data/generated/tbox/debate_log.json</code>
    </p>
</div>
"""


def _render_cq_feedback_rotation() -> str:
    """S13 보고서에 "반복 실패 패턴" 경고 섹션 렌더 (T3, docs/reference/task-glossary.md).

    cq_feedback.json 의 최신 iteration 에서 ``age_iterations >= 3`` 인
    suggestion 이 있으면 표시. LLM 이 여러 번 재생성해도 해결 못 한
    구조 결함이므로 사용자 수동 개입 필요.

    파일이 없거나 조건 불충족이면 빈 문자열 → S13 보고서 변화 없음.

    CQ id·클래스명·유형은 사용자 입력이나 LLM 출력에서 온 값이므로 escape 해서 싣는다.
    """
    try:
        from tools.cq_feedback import _load_feedback
        data = _load_feedback()
    except Exception:  # noqa: BLE001
        return ""

    iterations = data.get("iterations", [])
    if not iterations:
        return ""
    latest = iterations[-1]
    stubborn = [
        s for s in latest.get("suggestions", [])
        if s.get("age_iterations", 1) >= 3
    ]
    if not stubborn:
        return ""

    parts = [
        '<div class="section" style="border-left:4px solid #f59e0b;padding-left:12px">',
        '<h2>⚠️ 반복 실패 패턴 (CQ Feedback)</h2>',
        '<p>아래 구조 결함은 <strong>3회 이상 연속 iteration</strong>에서 감지됐습니다. '
        'LLM 자동 재생성으로 해결되지 않았으므로 <strong>수동 T-Box 편집</strong> 또는 '
        'CQ 재작성을 고려하세요.</p>',
        '<table><thead><tr><th>유형</th><th>대상</th><th>CQ ID</th>'
        '<th>연속 실패</th></tr></thead><tbody>',
    ]
    for s in stubborn:
        stype = s.get("type", "?")
        if stype == "missing_connection":
            target = f"{s.get('class_a', '?')} ↔ {s.get('class_b', '?')}"
        else:
            target = s.get("class", "?")
        parts.append(
            f"<tr><td>{_esc(stype)}</td><td>{_esc(target)}</td>"
            f"<td>{_esc(s.get('cq_id', '?'))}</td>"
            f"<td>{_esc(s.get('age_iterations', 1))}회</td></tr>"
        )
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _build_html(data: dict) -> str:
    """보고서 데이터를 HTML로 렌더링한다.

    클래스명·파일명·경로·검증 메시지·도메인 이름은 ``_esc`` 를 거쳐 싣는다. 이 함수는
    로컬 변수 ``html`` 에 문서를 누적하므로 ``html.escape`` 대신 모듈 별칭을 쓴다.
    """
    domain_name = _esc(DOMAIN_CONFIG["domain"]["name"])
    tbox_vis_path = data.get("tbox_vis_path", "")
    ts = data["tbox_stats"] or {}
    artifacts = data["artifacts"]
    tacit = data["tacit_stats"]
    dict_stats = data["dict_stats"] or {}
    dv = data["dict_validation"] or {}
    dv_summary = dv.get("summary", {})
    dv_coverage = dv.get("coverage", {})
    dv_sections = dv.get("sections", {})
    dv_issues = dv.get("issues", [])

    # Hierarchy HTML
    hierarchy = ts.get("hierarchy", {})
    hierarchy_html = ""
    for parent, children in sorted(hierarchy.items()):
        items = "".join(f"<li>{_esc(c)}</li>" for c in children)
        hierarchy_html += f'<details><summary>{_esc(parent)} ({len(children)})</summary><ul>{items}</ul></details>'

    # Artifacts table
    def artifact_row(name: str, info: dict) -> str:
        if not info.get("exists"):
            return f'<tr><td>{_esc(name)}</td><td colspan="3" class="na">미생성</td></tr>'
        return (
            f'<tr><td>{_esc(name)}</td><td><code>{_esc(info["path"])}</code></td>'
            f'<td>{_esc(info["size_display"])}</td><td>{_esc(info["modified"])}</td></tr>'
        )

    artifacts_html = "".join([
        artifact_row("T-Box", artifacts["tbox"]),
        artifact_row("A-Box", artifacts["abox"]),
        artifact_row("추론 결과", artifacts["inferred"]),
        artifact_row("시맨틱 딕셔너리", artifacts["semantic_dict"]),
    ])

    # Tacit files table
    tacit_html = ""
    if tacit["count"] > 0:
        rows = "".join(
            f'<tr><td>{_esc(f["name"])}</td><td>{_esc(f["triples"])}</td><td>{_esc(f["size"])}</td></tr>'
            for f in tacit["files"]
        )
        tacit_html = f"""<table>
            <tr><th>파일명</th><th>트리플 수</th><th>크기</th></tr>
            {rows}
            <tr class="total"><td>합계</td><td>{_esc(tacit["total_triples"])}</td><td></td></tr>
        </table>"""
    else:
        tacit_html = '<p class="na">암묵지 파일 없음</p>'

    # Dict validation sections
    sections_html = "".join(
        f'<span class="chip {"ok" if v else "fail"}">{_esc(k)}</span>'
        for k, v in dv_sections.items()
    )

    # Dict validation issues
    issues_html = ""
    if dv_issues:
        rows = "".join(
            f'<tr><td><span class="severity {_esc(i["severity"])}">{_esc(i["severity"])}</span></td>'
            f'<td>{_esc(i["rule"])}</td><td>{_esc(i["message"])}</td></tr>'
            for i in dv_issues
        )
        issues_html = f'<table><tr><th>등급</th><th>규칙</th><th>내용</th></tr>{rows}</table>'
    else:
        issues_html = '<p class="ok-text">이슈 없음</p>'

    passed_class = "pass" if dv.get("passed") else "fail"
    passed_text = "PASS" if dv.get("passed") else "FAIL"

    # S2 토론 궤적 — T-Box 가 **어떻게** 만들어졌는지를 산출물 목록 바로 뒤에
    # 둔다. 이 섹션은 debate_log.json 이 없으면 "기록 없음" 안내를 반환하므로
    # 항상 문자열이다 (섹션이 조용히 사라지지 않는다).
    debate_html = _render_debate_section()

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>파이프라인 최종 보고서 — {domain_name}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
    background: #f5f6fa; color: #2d3436; line-height: 1.6;
}}
.container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
.header {{
    background: linear-gradient(135deg, #2c3e50 0%, #27ae60 100%);
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
tr.total td {{ font-weight: 700; border-top: 2px solid #dfe6e9; }}
code {{ font-size: 11px; background: #f0f0f0; padding: 2px 6px; border-radius: 3px; word-break: break-all; }}
.na {{ color: #b2bec3; font-style: italic; }}
.ok-text {{ color: #27ae60; font-weight: 600; }}
.verdict {{
    display: inline-block; padding: 4px 16px; border-radius: 20px;
    font-weight: 700; font-size: 14px; letter-spacing: 1px;
}}
.verdict.pass {{ background: #d5f5e3; color: #27ae60; }}
.verdict.fail {{ background: #fadbd8; color: #e74c3c; }}
.chip {{
    display: inline-block; padding: 3px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 600; margin: 2px 3px;
}}
.chip.ok {{ background: #d5f5e3; color: #27ae60; }}
.chip.fail {{ background: #fadbd8; color: #e74c3c; }}
.severity {{
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600;
}}
.severity.critical {{ background: #e74c3c; color: white; }}
.severity.high {{ background: #e67e22; color: white; }}
.severity.warning {{ background: #f1c40f; color: #2c3e50; }}
.severity.info {{ background: #3498db; color: white; }}
details {{ margin: 4px 0; }}
summary {{ cursor: pointer; font-size: 13px; font-weight: 600; color: #2c3e50; padding: 3px 0; }}
details ul {{ padding-left: 20px; font-size: 12px; color: #636e72; }}
details li {{ margin: 2px 0; }}
.two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
@media (max-width: 768px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
.coverage-bar {{
    background: #ecf0f1; border-radius: 6px; height: 24px; position: relative; overflow: hidden; margin: 6px 0;
}}
.coverage-fill {{
    height: 100%; border-radius: 6px; display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; color: white;
}}
.coverage-fill.green {{ background: #27ae60; }}
.coverage-fill.yellow {{ background: #f39c12; }}
.coverage-fill.red {{ background: #e74c3c; }}
.vis-link {{
    font-size: 13px; font-weight: 500; color: #3498db; text-decoration: none;
    margin-left: 12px; padding: 4px 12px; border: 1px solid #3498db;
    border-radius: 6px; vertical-align: middle;
}}
.vis-link:hover {{ background: #3498db; color: white; }}
.footer {{ text-align: center; color: #b2bec3; font-size: 12px; padding: 16px; }}
</style>
</head>
<body>
<div class="container">

<div class="header">
    <h1>{domain_name} — 파이프라인 최종 보고서</h1>
    <div class="meta">생성 시각: {_esc(data["generated_at"])} &nbsp;|&nbsp; CSV 테이블: {_esc(data["source"]["csv_count"])}개</div>
</div>

<!-- 1. KG 핵심 통계 -->
<div class="section">
    <h2>KG 핵심 통계</h2>
    <div class="stats-grid">
        <div class="stat-card blue">
            <div class="num">{ts.get("classes", 0)}</div>
            <div class="label">Classes</div>
        </div>
        <div class="stat-card green">
            <div class="num">{ts.get("object_properties", 0)}</div>
            <div class="label">ObjectProperties</div>
        </div>
        <div class="stat-card orange">
            <div class="num">{ts.get("datatype_properties", 0)}</div>
            <div class="label">DatatypeProperties</div>
        </div>
        <div class="stat-card purple">
            <div class="num">{ts.get("triples", 0):,}</div>
            <div class="label">T-Box Triples</div>
        </div>
        <div class="stat-card teal">
            <div class="num">{data["abox_stats"]["lines"] if data["abox_stats"] else 0:,}</div>
            <div class="label">A-Box Lines</div>
        </div>
        <div class="stat-card">
            <div class="num">{data["inferred_stats"]["lines"] if data["inferred_stats"] else 0:,}</div>
            <div class="label">Inferred Lines</div>
        </div>
    </div>
</div>

<!-- 2. 산출물 파일 -->
<div class="section">
    <h2>산출물 파일</h2>
    <table>
        <tr><th>산출물</th><th>경로</th><th>크기</th><th>수정일</th></tr>
        {artifacts_html}
    </table>
</div>

<!-- 2.5 S2 토론 궤적 (debate_log.json) -->
{debate_html}

<!-- 3. 클래스 계층 + 암묵지 -->
<div class="two-col">
    <div class="section">
        <h2>클래스 계층 ({ts.get("hierarchy_groups", 0)}개 그룹)
            {'<a href="' + _esc(Path(tbox_vis_path).resolve().as_uri()) + '" target="_blank" class="vis-link">인터랙티브 시각화 열기 &rarr;</a>' if tbox_vis_path and os.path.exists(tbox_vis_path) else ''}
        </h2>
        {hierarchy_html}
    </div>
    <div class="section">
        <h2>암묵지 (Tacit Knowledge)</h2>
        <div class="stats-grid">
            <div class="stat-card"><div class="num">{_esc(tacit["count"])}</div><div class="label">파일 수</div></div>
            <div class="stat-card"><div class="num">{_esc(tacit["total_triples"])}</div><div class="label">트리플 수</div></div>
        </div>
        {tacit_html}
    </div>
</div>

<!-- 4. 시맨틱 딕셔너리 통계 -->
<div class="section">
    <h2>시맨틱 딕셔너리</h2>
    <div class="stats-grid">
        <div class="stat-card"><div class="num">{dict_stats.get("classes", 0)}</div><div class="label">클래스 항목</div></div>
        <div class="stat-card"><div class="num">{dict_stats.get("object_properties", 0)}</div><div class="label">ObjectProperty 항목</div></div>
        <div class="stat-card"><div class="num">{dict_stats.get("anti_patterns", 0)}</div><div class="label">Anti-patterns</div></div>
        <div class="stat-card"><div class="num">{dict_stats.get("common_patterns", 0)}</div><div class="label">Common Patterns</div></div>
        <div class="stat-card"><div class="num">{dict_stats.get("question_templates", 0)}</div><div class="label">질문 템플릿</div></div>
        <div class="stat-card"><div class="num">{dict_stats.get("functional_properties", 0)}</div><div class="label">FunctionalProperty</div></div>
    </div>
</div>

<!-- 5. 시맨틱 딕셔너리 검증 -->
<div class="section">
    <h2>시맨틱 딕셔너리 검증 <span class="verdict {passed_class}">{passed_text}</span></h2>

    <h3 style="font-size:13px; color:#636e72; margin:12px 0 6px;">커버리지</h3>
    <div class="two-col">
        <div>
            <div style="font-size:12px; color:#636e72;">클래스 매칭률</div>
            <div class="coverage-bar">
                <div class="coverage-fill {'green' if dv_coverage.get('class_match_rate', 0) >= 90 else 'yellow' if dv_coverage.get('class_match_rate', 0) >= 70 else 'red'}"
                     style="width:{dv_coverage.get('class_match_rate', 0)}%">{dv_coverage.get('class_match_rate', 0)}%</div>
            </div>
            <div style="font-size:11px; color:#b2bec3;">T-Box {dv_coverage.get('tbox_classes', 0)}개 / 딕셔너리 {dv_coverage.get('dict_classes', 0)}개</div>
        </div>
        <div>
            <div style="font-size:12px; color:#636e72;">ObjectProperty 매칭률</div>
            <div class="coverage-bar">
                <div class="coverage-fill {'green' if dv_coverage.get('op_match_rate', 0) >= 90 else 'yellow' if dv_coverage.get('op_match_rate', 0) >= 70 else 'red'}"
                     style="width:{dv_coverage.get('op_match_rate', 0)}%">{dv_coverage.get('op_match_rate', 0)}%</div>
            </div>
            <div style="font-size:11px; color:#b2bec3;">T-Box {dv_coverage.get('tbox_object_properties', 0)}개 / 딕셔너리 {dv_coverage.get('dict_object_properties', 0)}개</div>
        </div>
    </div>

    <h3 style="font-size:13px; color:#636e72; margin:14px 0 6px;">필수 섹션</h3>
    <div>{sections_html}</div>

    <h3 style="font-size:13px; color:#636e72; margin:14px 0 6px;">이슈 ({_esc(dv_summary.get('total', 0))}건)</h3>
    {issues_html}
</div>

"""

    # Golden Queries section (X1)
    gq = data.get("golden_queries", {})
    gq_status = gq.get("status", "not_configured")
    gq_html = ""
    if gq_status == "not_configured":
        gq_html = """
<div class="section">
    <h2>Golden Query 회귀</h2>
    <div style="background:#ecf0f1; border-radius:8px; padding:16px; color:#636e72; font-size:13px;">
        <strong>Golden queries 미설정</strong> — SME가 검증한 SPARQL을 회귀 게이트로 사용할 수 있습니다.<br>
        <code>add_golden_queries(user_provided='...')</code> 도구로 추가하세요.
    </div>
</div>
"""
    elif gq_status == "executed":
        summary = gq.get("summary", {})
        regression = gq.get("regression", {})
        total = summary.get("total", 0)
        passed = summary.get("passed", 0)
        failed = summary.get("failed", 0)
        pass_rate = summary.get("pass_rate", 0.0)
        regressions = regression.get("regressions", [])
        recoveries = regression.get("recoveries", [])
        has_prev = regression.get("has_previous", False)

        reg_alert = ""
        if regressions:
            # HTML injection 방어: user-provided ID 와 status 를 escape
            reg_list = ", ".join(
                f"{_escape(str(r['id']))} ({_escape(str(r['from']))}→{_escape(str(r['to']))})"
                for r in regressions[:3]
            )
            reg_alert = f'<div style="background:#fadbd8; padding:10px; border-radius:6px; color:#e74c3c; font-size:12px; margin-top:8px;"><strong>⚠ 회귀 감지 {len(regressions)}건:</strong> {reg_list}</div>'
        rec_info = ""
        if recoveries:
            rec_list = ", ".join(_escape(str(r['id'])) for r in recoveries[:3])
            rec_info = f'<div style="background:#d5f5e3; padding:8px; border-radius:6px; color:#27ae60; font-size:11px; margin-top:6px;">✓ 복구 {len(recoveries)}건: {rec_list}</div>'

        gq_html = f"""
<div class="section">
    <h2>Golden Query 회귀 <span class="verdict {'pass' if pass_rate == 100 else 'fail'}">{pass_rate:.0f}% PASS</span></h2>
    <div class="stats-grid">
        <div class="stat-card"><div class="num">{_esc(total)}</div><div class="label">총 케이스</div></div>
        <div class="stat-card green"><div class="num">{_esc(passed)}</div><div class="label">PASS</div></div>
        <div class="stat-card orange"><div class="num">{_esc(failed)}</div><div class="label">FAIL</div></div>
        <div class="stat-card"><div class="num">{pass_rate:.1f}%</div><div class="label">통과율</div></div>
    </div>
    {reg_alert}
    {rec_info}
    <div style="font-size:11px; color:#b2bec3; margin-top:10px;">
        최근 실행: {_esc(gq.get("timestamp", "N/A"))} | 케이스 수: {_esc(gq.get("results_count", 0))}
        {'| 이전 실행과 비교 가능' if has_prev else '| 이전 실행 기록 없음'}
    </div>
</div>
"""
    elif gq_status == "error":
        # HTML injection 방어: user-provided error message escape
        error_msg = _escape(str(gq.get("message", "알 수 없는 오류")))
        gq_html = f"""
<div class="section">
    <h2>Golden Query 회귀</h2>
    <div style="background:#fadbd8; border-radius:8px; padding:16px; color:#e74c3c; font-size:13px;">
        <strong>오류 발생:</strong> {error_msg}
    </div>
</div>
"""

    html += gq_html

    # Information Preservation section
    loss_budget = data.get("loss_budget")
    if loss_budget and loss_budget.get("stages"):
        html += '<div class="section"><h2>\U0001f4ca 정보 보존률</h2>'
        html += '<table><tr><th>단계</th><th>손실 항목</th><th>보존률</th></tr>'
        for stage_name, stage_data in loss_budget["stages"].items():
            stage_label = {
                "abox_generation": "A-Box 생성",
                "inference": "OWL RL 추론",
                "lpg_conversion": "LPG 변환",
            }.get(stage_name, stage_name)
            loss_count = stage_data.get("total_loss_items", stage_data.get("noise_pruned", 0))
            pres = stage_data.get("preservation", stage_data.get("fidelity", {}))
            if isinstance(pres, dict):
                pres_ratio = pres.get("meaningful_triples_ratio", pres.get("overall", "N/A"))
            else:
                pres_ratio = pres
            if isinstance(pres_ratio, int | float):
                pres_str = f"{pres_ratio * 100:.1f}%"
            else:
                pres_str = str(pres_ratio)
            html += f'<tr><td>{_esc(stage_label)}</td><td>{_esc(loss_count)}건</td><td>{_esc(pres_str)}</td></tr>'
        cum = loss_budget.get("cumulative_preservation_rate")
        if cum is not None:
            html += f'<tr style="font-weight:bold"><td>누적 보존률</td><td>\u2014</td><td>{cum * 100:.1f}%</td></tr>'
        html += '</table></div>'

    meta_audit_html = data.get("meta_audit_html", "")

    # T3: CQ feedback rotation warning — age_iterations >= 3 인 suggestion 있으면
    # "반복 실패 패턴" 섹션을 노출해 수동 개입 신호 제공.
    cq_feedback_html = _render_cq_feedback_rotation()
    if cq_feedback_html:
        html += cq_feedback_html

    # X3: Meta-audit action_playbook — validator blind-spot / dead-check /
    # redundant pair 에 대한 구체 액션 제안 섹션. 파일 없으면 빈 문자열.
    playbook_html = _render_meta_audit_playbook()
    if playbook_html:
        html += playbook_html

    html += f"""
<!-- 다음 단계 가이드 -->
<div class="section">
    <h2>다음 단계</h2>
    <ol>
        <li><strong>SPARQL 탐색</strong>: Claude Code에서 "핵심 엔터티별 지표 조회해줘" 입력</li>
        <li><strong>T-Box 메트릭스</strong>: "T-Box 품질 점수 확인해줘" &rarr; measure_tbox_metrics</li>
        <li><strong>Neo4j 배포</strong>: "Neo4j 에 올려줘" &rarr; NEO4J_DEPLOY 워크플로우</li>
        <li><strong>패턴 탐지</strong>: "이상 패턴 탐지해줘" &rarr; detect_failure_patterns</li>
        <li><strong>인과 관계 분석</strong>: "값 A가 변하면 어디에 영향줘?" &rarr; analyze_causal_rules</li>
        <li><strong>T-Box 수정</strong>: "T-Box에 새 클래스 추가해줘" &rarr; TBOX_MODIFICATION 워크플로우</li>
    </ol>
</div>

{meta_audit_html}

<div class="footer">
    {domain_name} Pipeline Report &mdash; Generated by ontology-agent
</div>

</div>
</body>
</html>"""

    return html


def generate_pipeline_report(open_report: bool = False) -> str:
    """FULL_PIPELINE 최종 보고서를 HTML로 생성한다.

    KG 통계, 산출물 파일 목록, 추론 통계, 암묵지 통계,
    시맨틱 딕셔너리 검증 결과를 포함하는 종합 보고서.

    Args:
        open_report: True면 생성한 HTML을 브라우저에서 연다. 기본값은 False.
    """
    try:
        data = _collect_all_data()

        reports_dir = resolve_generated_path("reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        vis_path = _generate_tbox_visualization(GENERATED_REPORTS_DIR)
        data["tbox_vis_path"] = vis_path

        audit_latest = os.path.join(GENERATED_DIR, "meta_audit", "latest.json")
        data["meta_audit_html"] = _render_meta_audit_section(audit_latest)

        html = _build_html(data)

        out_path = resolve_generated_path("reports/pipeline_report.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)

        if open_report:
            webbrowser.open(out_path.resolve().as_uri())

        return json.dumps({
            "success": True,
            "path": str(out_path),
            "summary": {
                "classes": data["tbox_stats"]["classes"] if data["tbox_stats"] else 0,
                "object_properties": data["tbox_stats"]["object_properties"] if data["tbox_stats"] else 0,
                "datatype_properties": data["tbox_stats"]["datatype_properties"] if data["tbox_stats"] else 0,
                "tacit_files": data["tacit_stats"]["count"],
                "tacit_triples": data["tacit_stats"]["total_triples"],
                "dict_validation_passed": data["dict_validation"].get("passed") if data["dict_validation"] else None,
            },
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
