"""T-Box 온톨로지 시각화 도구

T-Box TTL을 파싱하여 인터랙티브 HTML을 생성한다.
vis.js 네트워크 그래프 + 클래스 계층 트리 + 상세 패널.

HTML 레이아웃 규칙 (변경 금지):
- 왼쪽 사이드바: IOF 상위 클래스별 계층 트리 (collapsible details)
- 가운데: vis.js 네트워크 그래프 (클래스=노드, ObjectProperty=엣지, IOF 그룹별 색상)
- 오른쪽 사이드바: 클릭 시 DatatypeProperty/ObjectProperty 상세 패널
- 상단 헤더: 통계 배지 (Classes, ObjectProp, DatatypeProp, Triples)
- 상단 도구: 전체 보기, 물리 엔진 토글, 공정 흐름 하이라이트
기능 추가는 OK, 레이아웃 구조 변경은 금지.
"""

import html
import json
import logging
import os
import webbrowser

from rdflib import OWL, RDF, RDFS, BNode, Graph

from config import (
    GENERATED_TBOX_DIR,
    TBOX_PATH,
    resolve_generated_path,
)
from domain.namespaces import DOMAIN_CONFIG, DOMAIN_NS, IOF_IRI_ROOT, NS_PREFIX
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.common import (
    ESCAPE_HTML_JS,
    VIS_NETWORK_SCRIPT_TAG,
    error_response,
    json_for_script,
    path_to_file_uri,
    resolve_child_path,
)

logger = logging.getLogger(__name__)

# IOF parent → color mapping
_COLOR_MAP = {
    "ManufacturingProcess": "#4A90D9",
    "MaterialArtifact": "#5DADE2",
    "MaterialState": "#85C1E9",
    "MaterialProduct": "#2ECC71",
    "MaterialResource": "#9B59B6",
    "MeasurementInformationContentEntity": "#6BC06B",
    "MeasurementProcess": "#1ABC9C",
    "Event": "#E8A838",
    "PlanSpecification": "#F4D03F",
    "PlannedProcess": "#EB984E",
    "Identifier": "#F1948A",
    "PhysicalLocationIdentifier": "#D2B4DE",
    "MaterialLocationChangeProcess": "#AED6F1",
    "Supplier": "#BB8FCE",
    "Agreement": "#E74C8B",
}


def _short_uri(uri: str) -> str:
    uri = str(uri)
    if uri.startswith(DOMAIN_NS):
        return f"{NS_PREFIX}:{uri[len(DOMAIN_NS):]}"
    if uri.startswith(IOF_IRI_ROOT):
        return f"iof:{_local_name(uri)}"
    if uri.startswith("http://www.w3.org/2001/XMLSchema#"):
        return f"xsd:{_local_name(uri)}"
    if uri.startswith("http://www.w3.org/2002/07/owl#"):
        return f"owl:{_local_name(uri)}"
    return _local_name(uri)


def _get_label(g, uri, lang="ko"):
    for label in g.objects(uri, RDFS.label):
        if hasattr(label, "language") and label.language == lang:
            return str(label)
    for label in g.objects(uri, RDFS.label):
        if hasattr(label, "language") and label.language == "en":
            return str(label)
    return _local_name(uri)


def _get_comment(g, uri, lang="ko"):
    for comment in g.objects(uri, RDFS.comment):
        if hasattr(comment, "language") and comment.language == lang:
            return str(comment)
    return ""


def _extract_tbox_data(g: Graph) -> dict:
    """T-Box 그래프에서 시각화 데이터를 추출한다."""
    steel_str = DOMAIN_NS

    # Classes
    classes = []
    class_map = {}
    for cls in g.subjects(RDF.type, OWL.Class):
        if isinstance(cls, BNode) or not str(cls).startswith(steel_str):
            continue
        local = _local_name(cls)
        parents = [
            str(p)
            for p in g.objects(cls, RDFS.subClassOf)
            if not isinstance(p, BNode)
        ]
        parent_local = _local_name(parents[0]) if parents else ""
        parent_short = _short_uri(parents[0]) if parents else ""

        c = {
            "id": local,
            "uri": str(cls),
            "label_ko": _get_label(g, cls, "ko"),
            "label_en": _get_label(g, cls, "en"),
            "comment_ko": _get_comment(g, cls, "ko"),
            "parent_local": parent_local,
            "parent_short": parent_short,
            "datatype_properties": [],
            "outgoing": [],
            "incoming": [],
        }
        classes.append(c)
        class_map[local] = c

    # ObjectProperties
    obj_props = []
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        if not str(prop).startswith(steel_str):
            continue
        domains = [str(d) for d in g.objects(prop, RDFS.domain) if not isinstance(d, BNode)]
        ranges = [str(r) for r in g.objects(prop, RDFS.range) if not isinstance(r, BNode)]
        inverses = [_local_name(i) for i in g.objects(prop, OWL.inverseOf)]

        domain_local = _local_name(domains[0]) if domains else ""
        range_local = _local_name(ranges[0]) if ranges else ""
        prop_local = _local_name(prop)

        op = {
            "id": prop_local,
            "label_ko": _get_label(g, prop, "ko"),
            "domain": domain_local,
            "range": range_local,
            "inverse": inverses[0] if inverses else "",
        }
        obj_props.append(op)

        if domain_local in class_map:
            class_map[domain_local]["outgoing"].append({
                "property": prop_local,
                "target": range_local,
                "label_ko": _get_label(g, prop, "ko"),
            })
        if range_local in class_map:
            class_map[range_local]["incoming"].append({
                "property": prop_local,
                "source": domain_local,
                "label_ko": _get_label(g, prop, "ko"),
            })

    # DatatypeProperties
    dp_count = 0
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(prop).startswith(steel_str):
            continue
        dp_count += 1
        prop_local = _local_name(prop)
        domains = [str(d) for d in g.objects(prop, RDFS.domain) if not isinstance(d, BNode)]
        ranges = [str(r) for r in g.objects(prop, RDFS.range)]

        range_short = _short_uri(ranges[0]) if ranges else "xsd:string"

        for domain_uri in domains:
            domain_local = _local_name(domain_uri)
            if domain_local in class_map:
                class_map[domain_local]["datatype_properties"].append({
                    "name": prop_local,
                    "label_ko": _get_label(g, prop, "ko"),
                    "range": range_short,
                })

    # Hierarchy (group by parent)
    hierarchy = {}
    for c in classes:
        parent = c["parent_local"] or "기타"
        if parent not in hierarchy:
            hierarchy[parent] = []
        hierarchy[parent].append(c["id"])

    # Sub-class hierarchy within steel: namespace
    for parent_name, children in list(hierarchy.items()):
        if parent_name in class_map:
            hierarchy[parent_name] = sorted(children)

    return {
        "stats": {
            "class_count": len(classes),
            "object_property_count": len(obj_props),
            "datatype_property_count": dp_count,
            "triple_count": len(g),
        },
        "classes": sorted(classes, key=lambda c: c["id"]),
        "object_properties": sorted(obj_props, key=lambda p: p["id"]),
        "hierarchy": hierarchy,
    }


def _build_html(data: dict) -> str:
    """시각화 데이터를 인터랙티브 HTML로 렌더링한다.

    label·comment·IRI 지역명은 T-Box 에서 그대로 온 값이다. 서버 쪽 HTML 은
    ``html.escape``, script 블록의 JSON 은 ``json_for_script``, 브라우저에서 조립하는
    innerHTML 은 ``escapeHtml`` 을 거친다. 클릭 대상 클래스 ID 는 인라인 핸들러 문자열에
    넣지 않고 ``data-*`` 속성으로 넘긴다.
    """
    data_json = json_for_script(data)
    domain_name = html.escape(str(DOMAIN_CONFIG["domain"]["name"]))
    stats = data["stats"]

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>T-Box 온톨로지 시각화 — {domain_name}</title>
{VIS_NETWORK_SCRIPT_TAG}
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
    background: #f5f6fa; color: #2d3436;
}}
.header {{
    background: linear-gradient(135deg, #2c3e50 0%, #3498db 100%);
    color: white; padding: 16px 24px;
    display: flex; align-items: center; justify-content: space-between;
}}
.header h1 {{ font-size: 20px; font-weight: 600; }}
.header .subtitle {{ font-size: 13px; opacity: 0.8; margin-top: 2px; }}
.badges {{ display: flex; gap: 10px; }}
.badge {{
    background: rgba(255,255,255,0.2); border-radius: 20px;
    padding: 6px 14px; font-size: 13px; font-weight: 500;
}}
.badge .num {{ font-weight: 700; margin-right: 4px; }}
.main {{ display: flex; height: calc(100vh - 60px); }}
.sidebar-left {{
    width: 240px; background: white; border-right: 1px solid #dfe6e9;
    overflow-y: auto; padding: 12px; flex-shrink: 0;
}}
.sidebar-left h2 {{ font-size: 14px; color: #636e72; margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid #dfe6e9; }}
.center {{ flex: 1; position: relative; }}
#network {{ width: 100%; height: 100%; }}
.sidebar-right {{
    width: 340px; background: white; border-left: 1px solid #dfe6e9;
    overflow-y: auto; padding: 16px; flex-shrink: 0;
}}
.sidebar-right h2 {{ font-size: 14px; color: #636e72; margin-bottom: 10px; }}
details {{ margin-bottom: 4px; }}
summary {{
    cursor: pointer; font-size: 13px; font-weight: 600; color: #2c3e50;
    padding: 4px 0; user-select: none;
}}
summary:hover {{ color: #3498db; }}
.tree-item {{
    padding: 3px 0 3px 16px; font-size: 12px; cursor: pointer;
    border-radius: 4px; margin: 1px 0;
}}
.tree-item:hover {{ background: #ebf5fb; color: #2980b9; }}
.tree-item.active {{ background: #3498db; color: white; }}
.detail-empty {{ color: #b2bec3; font-size: 13px; text-align: center; margin-top: 60px; }}
.detail-title {{ font-size: 18px; font-weight: 700; margin-bottom: 4px; color: #2c3e50; }}
.detail-subtitle {{ font-size: 13px; color: #636e72; margin-bottom: 6px; }}
.detail-comment {{ font-size: 13px; color: #636e72; margin-bottom: 14px; line-height: 1.5; padding: 8px; background: #f8f9fa; border-radius: 6px; }}
.detail-section {{ margin-bottom: 14px; }}
.detail-section h3 {{ font-size: 13px; font-weight: 600; color: #636e72; margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px solid #f0f0f0; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{ text-align: left; padding: 5px 8px; background: #f8f9fa; color: #636e72; font-weight: 600; }}
td {{ padding: 5px 8px; border-bottom: 1px solid #f0f0f0; }}
td.clickable {{ color: #3498db; cursor: pointer; }}
td.clickable:hover {{ text-decoration: underline; }}
.range-tag {{
    font-size: 11px; background: #f0f0f0; padding: 1px 6px;
    border-radius: 3px; color: #636e72;
}}
.toolbar {{
    position: absolute; top: 10px; left: 10px; z-index: 10;
    display: flex; gap: 6px;
}}
.toolbar button {{
    background: white; border: 1px solid #dfe6e9; border-radius: 6px;
    padding: 6px 12px; font-size: 12px; cursor: pointer;
    font-family: inherit;
}}
.toolbar button:hover {{ background: #ebf5fb; }}
.toolbar button.active {{ background: #3498db; color: white; border-color: #3498db; }}
.legend {{
    position: absolute; bottom: 10px; left: 10px; z-index: 10;
    background: white; border: 1px solid #dfe6e9; border-radius: 8px;
    padding: 10px; font-size: 11px; max-width: 200px;
}}
.legend-item {{ display: flex; align-items: center; gap: 6px; margin: 3px 0; }}
.legend-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
</style>
</head>
<body>
<div class="header">
    <div>
        <h1>{domain_name} — T-Box</h1>
        <div class="subtitle">클래스 관계 네트워크 + 계층 구조 + 프로퍼티 상세</div>
    </div>
    <div class="badges">
        <div class="badge"><span class="num">{stats['class_count']}</span>Classes</div>
        <div class="badge"><span class="num">{stats['object_property_count']}</span>ObjectProp</div>
        <div class="badge"><span class="num">{stats['datatype_property_count']}</span>DatatypeProp</div>
        <div class="badge"><span class="num">{stats['triple_count']}</span>Triples</div>
    </div>
</div>
<div class="main">
    <div class="sidebar-left">
        <h2>클래스 계층</h2>
        <div id="hierarchy"></div>
    </div>
    <div class="center">
        <div class="toolbar">
            <button onclick="resetView()">전체 보기</button>
            <button id="btnPhysics" onclick="togglePhysics()">물리 엔진 끄기</button>
            <button onclick="showProcessFlow()">공정 흐름</button>
        </div>
        <div id="network"></div>
        <div class="legend" id="legend"></div>
    </div>
    <div class="sidebar-right">
        <h2>클래스 상세</h2>
        <div id="detail"><div class="detail-empty">클래스를 클릭하면<br>상세 정보가 표시됩니다</div></div>
    </div>
</div>
<script>
const DATA = {data_json};

const colorMap = {json_for_script(_COLOR_MAP)};

{ESCAPE_HTML_JS}

function getColor(parentLocal) {{
    return colorMap[parentLocal] || '#BDC3C7';
}}

// Build vis.js nodes
const nodes = new vis.DataSet(
    DATA.classes.map(c => ({{
        id: c.id,
        label: c.label_ko + '\\n' + c.id,
        color: {{
            background: getColor(c.parent_local),
            border: getColor(c.parent_local),
            highlight: {{ background: '#e74c3c', border: '#c0392b' }},
        }},
        font: {{ face: "'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif", size: 12, color: '#fff', multi: true }},
        shape: 'box',
        borderWidth: 0,
        borderWidthSelected: 3,
        margin: 8,
        title: c.comment_ko || c.label_en,
    }}))
);

// Build vis.js edges
const processProps = new Set(['followedBy', 'precededBy']);
const edges = new vis.DataSet(
    DATA.object_properties
        .filter(p => p.domain && p.range)
        .map((p, i) => ({{
            id: 'e' + i,
            from: p.domain,
            to: p.range,
            label: p.id,
            arrows: 'to',
            color: {{
                color: processProps.has(p.id) ? '#e74c3c' : 'rgba(150,150,150,0.5)',
                highlight: '#e74c3c',
            }},
            font: {{ size: 9, color: '#636e72', strokeWidth: 2, strokeColor: '#fff' }},
            smooth: {{ type: 'curvedCW', roundness: 0.2 }},
            width: processProps.has(p.id) ? 2.5 : 1,
        }}))
);

// Network
const container = document.getElementById('network');
const network = new vis.Network(container, {{ nodes, edges }}, {{
    physics: {{
        enabled: true,
        barnesHut: {{
            gravitationalConstant: -4000,
            centralGravity: 0.3,
            springLength: 180,
            springConstant: 0.02,
            damping: 0.3,
        }},
        stabilization: {{ iterations: 400 }},
    }},
    interaction: {{
        hover: true,
        tooltipDelay: 200,
        navigationButtons: false,
        keyboard: true,
    }},
    edges: {{
        font: {{ align: 'middle' }},
    }},
}});

let physicsEnabled = true;
function togglePhysics() {{
    physicsEnabled = !physicsEnabled;
    network.setOptions({{ physics: {{ enabled: physicsEnabled }} }});
    document.getElementById('btnPhysics').textContent = physicsEnabled ? '물리 엔진 끄기' : '물리 엔진 켜기';
    document.getElementById('btnPhysics').classList.toggle('active', !physicsEnabled);
}}

function resetView() {{
    network.fit({{ animation: true }});
}}

function showProcessFlow() {{
    const processNodes = DATA.classes
        .filter(c => c.parent_local === 'ManufacturingProcess' || c.id === 'ManufacturingProcessStep')
        .map(c => c.id);
    if (processNodes.length > 0) {{
        network.selectNodes(processNodes);
        network.fit({{ nodes: processNodes, animation: true }});
    }}
}}

// Hierarchy tree
function buildHierarchy() {{
    const el = document.getElementById('hierarchy');
    const groups = {{}};
    DATA.classes.forEach(c => {{
        const parent = c.parent_local || '기타';
        if (!groups[parent]) groups[parent] = [];
        groups[parent].push(c);
    }});
    const sortedParents = Object.keys(groups).sort();
    let html = '';
    sortedParents.forEach(parent => {{
        const children = groups[parent].sort((a, b) => a.label_ko.localeCompare(b.label_ko));
        const count = children.length;
        html += `<details${{count <= 5 ? ' open' : ''}}>`;
        html += `<summary>${{escapeHtml(parent)}} (${{count}})</summary>`;
        children.forEach(c => {{
            html += `<div class="tree-item" data-id="${{escapeHtml(c.id)}}">${{escapeHtml(c.label_ko)}} <span style="color:#b2bec3">${{escapeHtml(c.id)}}</span></div>`;
        }});
        html += '</details>';
    }});
    el.innerHTML = html;
}}

// 클래스 ID 는 data-* 속성에서 읽는다 (인라인 핸들러 문자열에 값을 넣지 않는다)
document.getElementById('hierarchy').addEventListener('click', e => {{
    const item = e.target.closest('.tree-item');
    if (item) selectClass(item.dataset.id);
}});
document.getElementById('detail').addEventListener('click', e => {{
    const link = e.target.closest('[data-class-id]');
    if (link) selectClass(link.dataset.classId);
}});

// Detail panel
function selectClass(classId) {{
    const c = DATA.classes.find(x => x.id === classId);
    if (!c) return;
    network.selectNodes([classId]);
    network.focus(classId, {{ scale: 1.2, animation: true }});

    // Update tree active (ID 를 CSS 선택자에 넣지 않고 dataset 으로 비교한다)
    document.querySelectorAll('.tree-item').forEach(el => {{
        const active = el.dataset.id === classId;
        el.classList.toggle('active', active);
        if (active) el.scrollIntoView({{ block: 'nearest' }});
    }});

    let html = `<div class="detail-title">${{escapeHtml(c.label_ko)}}</div>`;
    html += `<div class="detail-subtitle">${{escapeHtml(c.id)}} &mdash; ${{escapeHtml(c.parent_short)}}</div>`;
    if (c.comment_ko) html += `<div class="detail-comment">${{escapeHtml(c.comment_ko)}}</div>`;

    // DatatypeProperties
    if (c.datatype_properties.length > 0) {{
        html += `<div class="detail-section"><h3>DatatypeProperty (${{c.datatype_properties.length}})</h3><table>`;
        html += '<tr><th>프로퍼티</th><th>타입</th></tr>';
        c.datatype_properties.sort((a,b) => a.name.localeCompare(b.name)).forEach(dp => {{
            html += `<tr><td title="${{escapeHtml(dp.label_ko)}}">${{escapeHtml(dp.name)}}</td><td><span class="range-tag">${{escapeHtml(dp.range)}}</span></td></tr>`;
        }});
        html += '</table></div>';
    }}

    // Outgoing ObjectProperties
    if (c.outgoing.length > 0) {{
        html += `<div class="detail-section"><h3>Outgoing (${{c.outgoing.length}})</h3><table>`;
        html += '<tr><th>프로퍼티</th><th>대상 클래스</th></tr>';
        c.outgoing.sort((a,b) => a.property.localeCompare(b.property)).forEach(op => {{
            html += `<tr><td>${{escapeHtml(op.property)}}</td><td class="clickable" data-class-id="${{escapeHtml(op.target)}}">${{escapeHtml(op.target)}}</td></tr>`;
        }});
        html += '</table></div>';
    }}

    // Incoming ObjectProperties
    if (c.incoming.length > 0) {{
        html += `<div class="detail-section"><h3>Incoming (${{c.incoming.length}})</h3><table>`;
        html += '<tr><th>소스 클래스</th><th>프로퍼티</th></tr>';
        c.incoming.sort((a,b) => a.source.localeCompare(b.source)).forEach(op => {{
            html += `<tr><td class="clickable" data-class-id="${{escapeHtml(op.source)}}">${{escapeHtml(op.source)}}</td><td>${{escapeHtml(op.property)}}</td></tr>`;
        }});
        html += '</table></div>';
    }}

    document.getElementById('detail').innerHTML = html;
}}

network.on('click', params => {{
    if (params.nodes.length > 0) selectClass(params.nodes[0]);
}});

// Legend
function buildLegend() {{
    const usedParents = new Set(DATA.classes.map(c => c.parent_local).filter(Boolean));
    let html = '<strong>IOF 상위 클래스</strong>';
    Object.entries(colorMap)
        .filter(([k]) => usedParents.has(k))
        .sort(([a],[b]) => a.localeCompare(b))
        .forEach(([name, color]) => {{
            const count = DATA.classes.filter(c => c.parent_local === name).length;
            html += `<div class="legend-item"><div class="legend-dot" style="background:${{escapeHtml(color)}}"></div>${{escapeHtml(name)}} (${{count}})</div>`;
        }});
    document.getElementById('legend').innerHTML = html;
}}

buildHierarchy();
buildLegend();
</script>
</body>
</html>"""


def visualize_tbox(tbox_path: str = "", open_report: bool = False) -> str:
    """T-Box 온톨로지를 인터랙티브 HTML로 시각화한다.

    클래스 네트워크 그래프, 계층 구조 트리, 프로퍼티 상세 패널을 제공.
    클래스를 클릭하면 DatatypeProperty, ObjectProperty 관계를 확인할 수 있다.

    Args:
        tbox_path: data/generated/tbox 아래 TTL 파일명. 비어있으면 기본 파일 사용.
        open_report: True면 생성한 HTML을 브라우저에서 연다. 기본값은 False.
    """
    try:
        path = (
            resolve_child_path(
                GENERATED_TBOX_DIR,
                tbox_path,
                allowed_suffixes=(".ttl",),
            )
            if tbox_path
            else TBOX_PATH
        )
    except Exception as e:
        return error_response(e, logger=logger)

    if not os.path.exists(path):
        return error_response(
            f"T-Box 파일이 없습니다: {path}",
            hint="generate_tbox로 T-Box를 먼저 생성하세요.",
            logger=logger,
        )

    g = _new_graph()
    g.parse(path, format="turtle")

    data = _extract_tbox_data(g)
    html = _build_html(data)

    out_path = resolve_generated_path("reports/tbox_visualization.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    if open_report:
        webbrowser.open(path_to_file_uri(out_path))

    return json.dumps({
        "success": True,
        "path": str(out_path),
        "stats": data["stats"],
    }, ensure_ascii=False, indent=2)
