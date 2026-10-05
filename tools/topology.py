"""Graph Topology Clinic — Q3.

KG를 네트워크(노드=인스턴스/클래스, 엣지=ObjectProperty)로 보고
네트워크 과학 지표로 구조 건강성을 진단한다.

기존 22개 validate_kg check는 전부 "로컬 논리 제약"이라 그래프 전체가
여러 섬으로 쪼개졌거나, 한 허브가 병목인 상황을 전혀 보지 못한다.

제공 지표:
- connected_components: 연결 성분 개수 + 크기 분포
- top_degree_nodes: 차수(연결 수) 상위 N
- approximate_diameter: random source BFS 기반 근사 지름
- degree_distribution: power-law vs uniform 판별용 분위수

주의: betweenness/Louvain은 60만 트리플에서 O(V·E) 이상 → 기본은 off,
`heavy=True`로 opt-in.
"""
from __future__ import annotations

import json
import logging
import os
import random
from typing import Any

from config import GENERATED_REPORTS_DIR
from tools.common import error_response, success_response

logger = logging.getLogger(__name__)


def _build_instance_graph():
    """A-Box + T-Box를 로드해 **인스턴스** 그래프(유향)로 networkx 변환.

    노드: steel-inst 네임스페이스에 속한 typed URI (T-Box 메타 제외)
    엣지: steel 도메인 ObjectProperty만 (rdf:type, rdfs:label 등은 제외)
    """
    import networkx as nx
    from rdflib import OWL, RDF, URIRef

    from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
    from domain.tbox_utils import load_graph

    g, _ = load_graph(use_inferred=False)
    G = nx.DiGraph()

    op_uris: set = set()
    for p in g.subjects(RDF.type, OWL.ObjectProperty):
        if isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS):
            op_uris.add(p)

    steel_inst_str = str(DOMAIN_INST_NS)

    def _is_instance(u) -> bool:
        return isinstance(u, URIRef) and str(u).startswith(steel_inst_str)

    typed_instances: set = set()
    for s, _, _ in g.triples((None, RDF.type, None)):
        if _is_instance(s):
            typed_instances.add(s)

    added_edges = 0
    for s, p, o in g:
        if p not in op_uris:
            continue
        if not (_is_instance(s) and _is_instance(o)):
            continue
        G.add_edge(str(s), str(o), key=str(p))
        added_edges += 1

    for s in typed_instances:
        G.add_node(str(s))

    return G, {"nodes": G.number_of_nodes(), "edges": added_edges}


def _component_stats(G) -> dict:
    import networkx as nx
    UG = G.to_undirected()
    comps = sorted((len(c) for c in nx.connected_components(UG)), reverse=True)
    total = sum(comps)
    largest_pct = round(comps[0] / total * 100, 1) if comps else 0.0
    return {
        "component_count": len(comps),
        "largest_component_size": comps[0] if comps else 0,
        "largest_component_pct": largest_pct,
        "top_5_sizes": comps[:5],
    }


def _degree_stats(G, top_n: int = 20) -> dict:
    """degree 상위 N + 분포 분위수."""
    degrees = [(n, d) for n, d in G.degree()]
    if not degrees:
        return {"top_nodes": [], "percentiles": {}}
    degrees.sort(key=lambda x: -x[1])
    top = [{"uri": n.split("#")[-1] if "#" in n else n.split("/")[-1],
            "degree": d} for n, d in degrees[:top_n]]
    vals = sorted(d for _, d in degrees)
    def pct(p: float) -> int:
        k = max(0, min(len(vals) - 1, int(len(vals) * p / 100)))
        return vals[k]
    return {
        "top_nodes": top,
        "percentiles": {
            "p50": pct(50), "p90": pct(90), "p99": pct(99), "max": vals[-1],
        },
        "mean": round(sum(vals) / len(vals), 2),
    }


def _approximate_diameter(G, n_sources: int = 50, seed: int = 42) -> dict:
    """랜덤 소스 n개에서 BFS → 최대 최단거리의 max로 지름 근사."""
    import networkx as nx
    UG = G.to_undirected()
    nodes = list(UG.nodes())
    if not nodes:
        return {"sampled_sources": 0, "approx_diameter": 0}
    rng = random.Random(seed)
    sources = rng.sample(nodes, min(n_sources, len(nodes)))
    max_depth = 0
    eccentricities: list[int] = []
    for src in sources:
        lengths = nx.single_source_shortest_path_length(UG, src)
        if not lengths:
            continue
        ecc = max(lengths.values())
        eccentricities.append(ecc)
        if ecc > max_depth:
            max_depth = ecc
    return {
        "sampled_sources": len(sources),
        "approx_diameter": max_depth,
        "median_eccentricity": (
            sorted(eccentricities)[len(eccentricities) // 2]
            if eccentricities else 0
        ),
    }


def _betweenness_top(G, k: int = 500, top_n: int = 20) -> dict:
    """샘플 기반 근사 betweenness — 전체 V·E는 너무 무거움."""
    import networkx as nx
    UG = G.to_undirected()
    if UG.number_of_nodes() == 0:
        return {"top_nodes": []}
    k = min(k, UG.number_of_nodes())
    try:
        bc = nx.betweenness_centrality(UG, k=k, seed=42, normalized=True)
    except Exception as e:
        return {"error": f"betweenness failed: {type(e).__name__}"}
    ranked = sorted(bc.items(), key=lambda kv: -kv[1])[:top_n]
    return {
        "sampled_k": k,
        "top_nodes": [
            {
                "uri": n.split("#")[-1] if "#" in n else n.split("/")[-1],
                "betweenness": round(v, 4),
            }
            for n, v in ranked
        ],
    }


def analyze_topology(heavy: bool = False) -> dict:
    """토폴로지 전체 분석. heavy=True면 betweenness 포함."""
    G, size = _build_instance_graph()
    report: dict[str, Any] = {"graph_size": size}
    if size["nodes"] == 0:
        report["warning"] = "empty graph"
        return report

    report["components"] = _component_stats(G)
    report["degree"] = _degree_stats(G)
    report["diameter"] = _approximate_diameter(G)

    if heavy:
        report["betweenness"] = _betweenness_top(G)

    # 건강성 신호 종합
    signals = []
    if report["components"]["component_count"] > 1:
        signals.append({
            "severity": "warn",
            "code": "disconnected",
            "message": (
                f"그래프가 {report['components']['component_count']}개 성분으로 "
                f"분리됨 (최대 {report['components']['largest_component_pct']}%)"
            ),
        })
    if report["degree"]["percentiles"]["max"] > report["degree"]["mean"] * 50:
        signals.append({
            "severity": "info",
            "code": "hub_imbalance",
            "message": (
                f"최대 차수 {report['degree']['percentiles']['max']}가 "
                f"평균 {report['degree']['mean']}의 50배 이상 — hub 집중"
            ),
        })
    if report["diameter"]["approx_diameter"] > 15:
        signals.append({
            "severity": "warn",
            "code": "large_diameter",
            "message": f"근사 지름 {report['diameter']['approx_diameter']} > 15",
        })
    report["signals"] = signals
    return report


def analyze_graph_topology(heavy: bool = False) -> str:
    """KG 구조 건강성 — 연결 성분/차수 분포/근사 지름 분석 (Q3).

    네트워크 과학 지표로 22개 로컬 검증이 놓치는 "KG 전체 형태"를 진단.

    Args:
        heavy: True면 betweenness centrality도 포함 (샘플 k=500).
               그래프가 크면 수십 초~수 분 소요.

    예상 소요시간: 5~30초 (heavy=False), 30초~3분 (heavy=True).
    Bedrock 호출 0회.
    """
    try:
        report = analyze_topology(heavy=heavy)
        try:
            os.makedirs(GENERATED_REPORTS_DIR, exist_ok=True)
            with open(os.path.join(GENERATED_REPORTS_DIR, "topology_report.json"),
                      "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("topology_report 저장 실패: %s", e)
        return success_response(report)
    except Exception as e:
        return error_response(
            f"토폴로지 분석 실패: {e}",
            hint="load_graph가 성공하는지 먼저 확인 (T-Box/A-Box 경로).",
            logger=logger,
        )
