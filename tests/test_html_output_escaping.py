"""생성 HTML 보고서가 신뢰하지 않는 텍스트를 마크업으로 해석하지 않는지 고정한다.

보고서에 실리는 문자열(T-Box label·comment·IRI, CSV 헤더·셀·파일명, CQ id, 도메인
이름, 사이드카 JSON 값)은 사용자 입력이나 LLM 출력에서 온다. 판정은 구조 비교다. 모양이
같은 무해한 입력과 공격 입력으로 각각 렌더링하고 ``html.parser`` 로 읽은 (태그, 속성
이름) 순서가 같아야 한다. 값이 escape 되면 공격 문자열은 텍스트나 속성 값으로만 남아
구조가 그대로이고, escape 가 하나라도 빠지면 ``<script>`` 나 ``<img onerror>`` 가 새
요소로 나타나 구조가 달라진다.

브라우저 쪽 ``innerHTML`` 대입은 Python 에서 실행할 수 없다. 그래서 페이지의 인라인
스크립트를 Node ``vm`` 안에서 최소 DOM 스텁으로 실행하고, 대입된 문자열을 같은 방식으로
비교한다. Node 가 없으면 그 테스트만 skip 한다.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from domain.namespaces import DOMAIN_CONFIG, DOMAIN_NS

SCRIPT_PAYLOAD = "</script><script>globalThis.__probe=1</script>"
IMG_PAYLOAD = "<img src=x onerror=alert(1)>"
ATTR_PAYLOAD = "\" onmouseover=\"alert(1)\" data-x='"

#: 서버 쪽 렌더링용. script 탈출과 요소·속성 주입을 한 번에 싣는다.
HOSTILE_TEXT = SCRIPT_PAYLOAD + IMG_PAYLOAD + ATTR_PAYLOAD
#: 브라우저 쪽 렌더링용. script 탈출 없이 innerHTML 주입만 본다.
CLIENT_HOSTILE_TEXT = IMG_PAYLOAD + ATTR_PAYLOAD
#: IRI 지역명·CSV 파일명처럼 식별자로 쓰이는 값. 인라인 핸들러 문자열을 닫는 따옴표를 싣는다.
#: ``/`` 와 ``#`` 는 지역명·파일명 경계라서 넣지 않는다.
HOSTILE_IDENT = "Id');globalThis.__probe=1;('" + IMG_PAYLOAD
HOSTILE_COLOR = '#000" onmouseover="alert(1)'

BENIGN_TEXT = "plain text"
BENIGN_IDENT = "Furnace"
BENIGN_COLOR = "#123456"

NODE = shutil.which("node")


# ── HTML 구조 비교 ─────────────────────────────────────────


class _Outline(HTMLParser):
    """시작 태그와 속성 이름 순서, 텍스트 노드를 모은다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, tuple[str, ...]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, tuple(sorted(name for name, _ in attrs))))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)


def _outline(markup: str) -> _Outline:
    parser = _Outline()
    parser.feed(markup)
    parser.close()
    return parser


def _assert_same_structure(benign: str, hostile: str) -> _Outline:
    """공격 입력이 요소나 속성을 하나도 추가하지 않았는지 확인한다."""
    ok = _outline(benign)
    bad = _outline(hostile)
    ok_scripts = sum(1 for tag, _ in ok.tags if tag == "script")
    bad_scripts = sum(1 for tag, _ in bad.tags if tag == "script")
    assert bad_scripts == ok_scripts, "script 요소가 늘었다"
    assert not any(tag == "img" for tag, _ in bad.tags), "주입한 img 요소가 생겼다"
    assert bad.tags == ok.tags
    return bad


class _InlineScripts(HTMLParser):
    """``src`` 없는 script 요소의 본문을 원문 그대로 모은다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[str] = []
        self._inside = False

    def handle_starttag(self, tag, attrs):
        if tag == "script" and not dict(attrs).get("src"):
            self._inside = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self._inside = False

    def handle_data(self, data):
        if self._inside:
            self.scripts[-1] += data


def _inline_scripts(page: str) -> list[str]:
    parser = _InlineScripts()
    parser.feed(page)
    parser.close()
    return parser.scripts


# ── 브라우저 쪽 innerHTML 실행 (Node vm) ────────────────────

_NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const [scriptPath, callsJson] = process.argv.slice(2);
const writes = [];
function element(id) {
  const el = {
    id, dataset: {}, style: {}, textContent: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, scrollIntoView() {},
    querySelector() { return element(id + ' > *'); },
    querySelectorAll() { return []; },
    closest() { return null; },
  };
  let markup = '';
  Object.defineProperty(el, 'innerHTML', {
    get() { return markup; },
    set(value) { markup = String(value); writes.push({ id, html: markup }); },
  });
  return el;
}
const registry = {};
const document = {
  getElementById(id) { return registry[id] || (registry[id] = element(id)); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
class DataSet { constructor(items) { this.items = items; } }
class Network {
  on() {} selectNodes() {} focus() {} fit() {} setOptions() {}
  getConnectedEdges() { return []; } selectEdges() {}
}
const sandbox = { document, vis: { DataSet, Network } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), sandbox);
for (const [name, ...args] of JSON.parse(callsJson)) sandbox[name](...args);
process.stdout.write(JSON.stringify({
  writes,
  probe: sandbox.__probe === undefined ? null : sandbox.__probe,
}));
"""


def _run_page_script(work_dir: Path, page: str, calls: list[list[str]]) -> dict:
    """페이지 인라인 스크립트를 실행하고 innerHTML 대입 기록을 돌려준다."""
    scripts = _inline_scripts(page)
    assert len(scripts) == 1, "인라인 스크립트는 하나여야 한다"
    work_dir.mkdir(parents=True, exist_ok=True)
    script_path = work_dir / "page.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    harness_path = work_dir / "harness.js"
    harness_path.write_text(_NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(harness_path), str(script_path), json.dumps(calls)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _assert_client_writes_inert(benign_run: dict, hostile_run: dict) -> None:
    """모든 innerHTML 대입이 구조를 보존하고 인라인 이벤트 핸들러를 쓰지 않는다."""
    assert hostile_run["writes"], "innerHTML 대입이 하나도 없다 (호출 시나리오 확인)"
    assert [w["id"] for w in hostile_run["writes"]] == [w["id"] for w in benign_run["writes"]]
    for ok, bad in zip(benign_run["writes"], hostile_run["writes"], strict=True):
        outline = _assert_same_structure(ok["html"], bad["html"])
        handlers = [(tag, name) for tag, names in outline.tags for name in names if name.startswith("on")]
        assert handlers == [], f"{bad['id']} 에 인라인 이벤트 핸들러가 있다: {handlers}"
    assert hostile_run["probe"] is None


requires_node = pytest.mark.skipif(NODE is None, reason="node 실행 파일이 없다")


# ── 공용 헬퍼 ─────────────────────────────────────────────


def test_json_for_script_round_trips_and_hides_markup():
    from tools.common import json_for_script

    value = {"a": SCRIPT_PAYLOAD, "b": "x\u2028y\u2029z & <!-- -->", "k": "한글"}
    encoded = json_for_script(value)

    assert json.loads(encoded) == value
    for ch in ("<", ">", "&", "\u2028", "\u2029"):
        assert ch not in encoded
    assert "한글" in encoded
    page = f"<script>var v = {encoded};</script>"
    assert [tag for tag, _ in _outline(page).tags] == ["script"]


# ── tools/visualization.py ─────────────────────────────────


def _vis_graph(text: str, ident: str) -> Graph:
    # 메모리 store 는 공격 문자가 섞인 IRI 를 거부하지 않는다.
    g = Graph()
    cls = URIRef(DOMAIN_NS + "Cls" + ident)
    other = URIRef(DOMAIN_NS + "Other" + ident)
    parent = URIRef("http://example.org/upper#Parent" + ident)
    g.add((cls, RDF.type, OWL.Class))
    g.add((other, RDF.type, OWL.Class))
    g.add((cls, RDFS.subClassOf, parent))
    for subject in (cls, other):
        g.add((subject, RDFS.label, Literal(text, lang="ko")))
        g.add((subject, RDFS.label, Literal(text, lang="en")))
        g.add((subject, RDFS.comment, Literal(text, lang="ko")))
    link = URIRef(DOMAIN_NS + "link" + ident)
    g.add((link, RDF.type, OWL.ObjectProperty))
    g.add((link, RDFS.domain, cls))
    g.add((link, RDFS.range, other))
    g.add((link, RDFS.label, Literal(text, lang="ko")))
    value = URIRef(DOMAIN_NS + "value" + ident)
    g.add((value, RDF.type, OWL.DatatypeProperty))
    g.add((value, RDFS.domain, cls))
    g.add((value, RDFS.range, XSD.string))
    g.add((value, RDFS.label, Literal(text, lang="ko")))
    return g


def _render_vis(monkeypatch, text: str, ident: str) -> str:
    from tools.visualization import _build_html, _extract_tbox_data

    monkeypatch.setitem(DOMAIN_CONFIG["domain"], "name", text)
    return _build_html(_extract_tbox_data(_vis_graph(text, ident)))


def test_tbox_visualization_page_keeps_untrusted_text_out_of_markup(monkeypatch):
    benign = _render_vis(monkeypatch, BENIGN_TEXT, BENIGN_IDENT)
    hostile = _render_vis(monkeypatch, HOSTILE_TEXT, HOSTILE_IDENT)

    outline = _assert_same_structure(benign, hostile)
    # 도메인 이름은 title·h1 에 텍스트로 남는다.
    assert HOSTILE_TEXT in "".join(outline.text)


@requires_node
def test_tbox_visualization_inner_html_is_inert(monkeypatch, tmp_path):
    runs = {}
    for case, text, ident in (
        ("benign", BENIGN_TEXT, BENIGN_IDENT),
        ("hostile", CLIENT_HOSTILE_TEXT, HOSTILE_IDENT),
    ):
        page = _render_vis(monkeypatch, text, ident)
        calls = [["selectClass", "Cls" + ident], ["selectClass", "Other" + ident]]
        runs[case] = _run_page_script(tmp_path / case, page, calls)

    _assert_client_writes_inert(runs["benign"], runs["hostile"])
    detail = [w["html"] for w in runs["hostile"]["writes"] if w["id"] == "detail"]
    assert detail and CLIENT_HOSTILE_TEXT in "".join(_outline(detail[0]).text)


# ── tools/local_artifacts.py::generate_csv_erd ─────────────


def _norm_column(col: str) -> str:
    return col.lower().replace("_", "").replace(" ", "")


def _render_erd(monkeypatch, work_dir: Path, text: str, ident: str, color: str) -> tuple[str, str, str]:
    import domain.table_mapping as table_mapping
    import tools.local_artifacts as local_artifacts

    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True)
    parent, child = f"Parent{ident}", f"Child{ident}"
    key, name, ref, note = f"Key{ident}", f"Name{ident}", f"Ref{ident}", f"Note{ident}"
    for table, header, rows in (
        (parent, [key, name], [["K1", text], ["K2", text]]),
        (child, [ref, note], [["K1", text], ["K2", text]]),
    ):
        with open(raw_dir / f"{table}.csv", "w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)

    parent_cls, child_cls = f"ParentClass{ident}", f"ChildClass{ident}"
    fk_file = work_dir / "fk_patterns.json"
    fk_file.write_text(json.dumps({"patterns": {_norm_column(ref): parent_cls}}), encoding="utf-8")
    stage = f"Stage{text}"

    monkeypatch.setattr(local_artifacts, "SOURCE_RAWDATA_DIR", str(raw_dir))
    monkeypatch.setattr(local_artifacts, "resolve_generated_path", lambda rel: work_dir / "out" / rel)
    monkeypatch.setattr(local_artifacts, "rules_path", lambda _name: str(fk_file))
    monkeypatch.setattr(local_artifacts, "_STEEL_STAGE", {parent_cls: stage, child_cls: stage})
    monkeypatch.setattr(local_artifacts, "_STAGE_COLORS", {stage: color})
    monkeypatch.setattr(
        table_mapping, "load_table_class_mapping",
        lambda *_a, **_k: {parent: parent_cls, child: child_cls},
    )
    monkeypatch.setattr(table_mapping, "load_table_pk_columns", lambda *_a, **_k: {parent: [key]})

    result = json.loads(local_artifacts.generate_csv_erd(open_report=False))
    assert result.get("success") is True, result
    assert result["fk_relations"] == 1
    page = (work_dir / "out" / "reports" / "csv_erd.html").read_text(encoding="utf-8")
    return page, parent, child


def test_csv_erd_page_keeps_untrusted_text_out_of_markup(monkeypatch, tmp_path):
    benign, _, _ = _render_erd(monkeypatch, tmp_path / "benign", BENIGN_TEXT, BENIGN_IDENT, BENIGN_COLOR)
    hostile, _, _ = _render_erd(monkeypatch, tmp_path / "hostile", HOSTILE_TEXT, HOSTILE_IDENT, HOSTILE_COLOR)

    outline = _assert_same_structure(benign, hostile)
    # 범례의 공정 단계명은 서버 쪽에서 텍스트로 렌더링된다.
    assert f"Stage{HOSTILE_TEXT}" in "".join(outline.text)


@requires_node
def test_csv_erd_inner_html_is_inert(monkeypatch, tmp_path):
    runs = {}
    for case, text, ident, color in (
        ("benign", BENIGN_TEXT, BENIGN_IDENT, BENIGN_COLOR),
        ("hostile", CLIENT_HOSTILE_TEXT, HOSTILE_IDENT, HOSTILE_COLOR),
    ):
        page, parent, child = _render_erd(monkeypatch, tmp_path / case, text, ident, color)
        calls = [["showDetail", child], ["showDetail", parent], ["filterCols", parent, ""]]
        runs[case] = _run_page_script(tmp_path / case / "js", page, calls)

    _assert_client_writes_inert(runs["benign"], runs["hostile"])
    detail = "".join(
        "".join(_outline(w["html"]).text) for w in runs["hostile"]["writes"] if w["id"] == "detail"
    )
    assert CLIENT_HOSTILE_TEXT in detail


# ── tools/report.py ───────────────────────────────────────


def _cq_feedback(text: str) -> dict:
    return {"iterations": [{"suggestions": [
        {"type": "missing_connection", "class_a": text, "class_b": text, "cq_id": text, "age_iterations": 3},
        {"type": text, "class": text, "cq_id": text, "age_iterations": 4},
    ]}]}


def _render_cq_rotation(monkeypatch, text: str) -> str:
    import tools.cq_feedback as cq_feedback
    from tools.report import _render_cq_feedback_rotation

    monkeypatch.setattr(cq_feedback, "_load_feedback", lambda: _cq_feedback(text))
    return _render_cq_feedback_rotation()


def test_cq_feedback_rotation_escapes_cq_fields(monkeypatch):
    benign = _render_cq_rotation(monkeypatch, BENIGN_TEXT)
    hostile = _render_cq_rotation(monkeypatch, HOSTILE_TEXT)

    outline = _assert_same_structure(benign, hostile)
    assert HOSTILE_TEXT in outline.text


def _meta_audit(text: str) -> dict:
    return {
        "history_insufficient": False,
        "blind_spots": [{"category": text, "mutants": text, "caught_by_any_check": text,
                         "recommendation": text}],
        "dead_checks": [{"check": text, "recommendation": text}],
        "pairwise_correlation": [{"pair": [text, text], "cooccurrence": text, "sample_size": text,
                                  "source": text, "interpretation": text}],
        "sensitivity_matrix": {text: {text: 0.5}},
    }


def test_meta_audit_section_escapes_artifact_fields(tmp_path):
    from tools.report import _render_meta_audit_section

    rendered = {}
    for case, text in (("benign", BENIGN_TEXT), ("hostile", HOSTILE_TEXT)):
        path = tmp_path / f"{case}.json"
        path.write_text(json.dumps(_meta_audit(text)), encoding="utf-8")
        rendered[case] = _render_meta_audit_section(str(path))

    outline = _assert_same_structure(rendered["benign"], rendered["hostile"])
    assert HOSTILE_TEXT in "".join(outline.text)


def _debate_summary(text: str) -> dict:
    return {
        "consensus_reached": False,
        "veto_lock_triggered": False,
        "infra_abort": {"phase": text, "round": text},
        "rounds": [{
            "round": text, "validator_issues": text, "validator_critical_high": text,
            "sme_issues": text, "sme_critical_high": text, "validator_approved": True,
            "sme_approved": False, "cq_coverage_pct": text, "triples": text,
            "jury": {"requested": text, "applied": text, "noop": text, "failed": text},
            "veto_persistent_validator": text, "veto_persistent_sme": text,
            "no_progress": True, "no_progress_streak": text,
        }],
        "recurring": [{"max_severity": text, "rounds_seen": text, "rounds": [text],
                       "category": text, "target": text, "agents": [text], "summary": text}],
        "duration_seconds": 120,
        "statistics": {"object_properties": text, "classes": text},
        "architect_initial": {"object_properties": text, "classes": text},
        "veto_persistent_targets": [text],
        "total_rounds": text,
        "timestamp": text,
        "runs_recorded": text,
        "recurring_distinct": text,
        "persistent_issue_count": text,
    }


def test_debate_section_escapes_debate_log_fields(monkeypatch):
    import tools.debate_log_store as debate_log_store
    from tools.report import _render_debate_section

    rendered = {}
    for case, text in (("benign", BENIGN_TEXT), ("hostile", HOSTILE_TEXT)):
        monkeypatch.setattr(debate_log_store, "summarize_latest_run", lambda text=text: _debate_summary(text))
        rendered[case] = _render_debate_section()

    outline = _assert_same_structure(rendered["benign"], rendered["hostile"])
    assert HOSTILE_TEXT in "".join(outline.text)


def _report_data(text: str) -> dict:
    info = {"exists": True, "path": text, "size_display": text, "modified": text}
    return {
        "tbox_vis_path": "",
        "tbox_stats": {"classes": 1, "object_properties": 1, "datatype_properties": 1, "triples": 10,
                       "hierarchy_groups": 1, "hierarchy": {text: [text, text]}},
        "artifacts": {"tbox": info, "abox": info, "inferred": {"exists": False, "path": text},
                      "semantic_dict": info},
        "tacit_stats": {"count": 1, "files": [{"name": text, "triples": 3, "size": text}],
                        "total_triples": 3},
        "dict_stats": {"classes": 1},
        "dict_validation": {
            "summary": {"total": 1},
            "coverage": {"class_match_rate": 95, "op_match_rate": 50, "tbox_classes": 1,
                         "dict_classes": 1, "tbox_object_properties": 1, "dict_object_properties": 1},
            "sections": {text: True},
            "issues": [{"severity": text, "rule": text, "message": text}],
            "passed": False,
        },
        "generated_at": text,
        "source": {"csv_count": 2},
        "abox_stats": None,
        "inferred_stats": None,
        "golden_queries": {
            "status": "executed",
            "summary": {"total": text, "passed": text, "failed": text, "pass_rate": 50.0},
            "regression": {"regressions": [{"id": text, "from": "PASS", "to": "FAIL"}],
                           "recoveries": [{"id": text}], "has_previous": True},
            "timestamp": text,
            "results_count": text,
        },
        "loss_budget": {
            "stages": {text: {"total_loss_items": text, "preservation": {"meaningful_triples_ratio": text}}},
            "cumulative_preservation_rate": 0.9,
        },
        "meta_audit_html": "",
    }


def test_pipeline_report_page_escapes_collected_fields(monkeypatch, tmp_path):
    import tools.cq_feedback as cq_feedback
    import tools.debate_log_store as debate_log_store
    import tools.report as report

    # 사이드카 읽기는 비워 둔다. 각 섹션은 위 테스트가 따로 본다.
    monkeypatch.setattr(debate_log_store, "summarize_latest_run", lambda: None)
    monkeypatch.setattr(cq_feedback, "_load_feedback", lambda: {"iterations": []})
    monkeypatch.setattr(report, "GENERATED_DIR", str(tmp_path))

    rendered = {}
    for case, text in (("benign", BENIGN_TEXT), ("hostile", HOSTILE_TEXT)):
        monkeypatch.setitem(DOMAIN_CONFIG["domain"], "name", text)
        rendered[case] = report._build_html(_report_data(text))

    outline = _assert_same_structure(rendered["benign"], rendered["hostile"])
    assert HOSTILE_TEXT in "".join(outline.text)


# ── tools/remote/neo4j_reporter.py ────────────────────────


def _lpg_inputs(text: str) -> dict:
    verification = {
        "verified": True,
        "coverage": {"percent": 99.5, "lpg_triples": 10, "rdf_instance_triples": 10, "excluded": 0},
        "class_distribution": {"matched": 1, "total": 2,
                               "mismatches": [{"class": text, "rdf": text, "lpg": text}]},
        "domain_relationships": {"matched": 1, "total": 2,
                                 "mismatches": [{"rel": text, "rdf": text, "lpg": text}]},
        "iof_relationships": {text: 5},
        "spot_check": {"passed": 1, "total": 1},
        "quality_score": {"total": 90, "completeness": {"score": 90, "weight": 0.2,
                                                        "details": {text: text}}},
        "iof_total": 5,
        "class_hierarchy": text,
        "labels": {"ko": text, "en": text},
        "cq_coverage": {"status": "ok", "total": 1, "passed": 1, "pass_rate": 100, "results": [
            {"id": text, "passed": True, "classes_exist": True, "connected": False},
        ]},
    }
    return {
        "source_file": text, "rdf_triples": 10, "nodes": {"n1": {}},
        "relationships": [("a", "b", text)], "verification": verification,
        "stats": {"excluded_axiom": 0},
    }


def test_lpg_report_escapes_cq_ids_paths_and_names(monkeypatch):
    from tools.remote.neo4j_reporter import build_cq_section_html, build_lpg_report_html

    pages, sections = {}, {}
    for case, text in (("benign", BENIGN_TEXT), ("hostile", HOSTILE_TEXT)):
        monkeypatch.setitem(DOMAIN_CONFIG["domain"], "name", text)
        inputs = _lpg_inputs(text)
        pages[case] = build_lpg_report_html(**inputs)
        sections[case] = build_cq_section_html(inputs["verification"])

    outline = _assert_same_structure(pages["benign"], pages["hostile"])
    assert HOSTILE_TEXT in "".join(outline.text)
    section = _assert_same_structure(sections["benign"], sections["hostile"])
    assert HOSTILE_TEXT in section.text
