"""P2b — resolve_entity_references MCP 진단 도구.

기존 A-Box 의 FK 매칭 품질을 **사후적으로** 분석. tools/fk_matching.py 의 4단
사다리가 generate_abox 실행 중 누적한 통계는 그 세션에서만 유효. 이 도구는
a_box.ttl + master_data.ttl 을 읽어 현재 상태의 매칭 품질을 재계산.

dry_run only (MVP) — A-Box 파일 변경 없음. 관찰 + 제안만.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from rdflib import URIRef

from config import ABOX_PATH, GENERATED_ABOX_DIR, MASTER_DATA_PATH
from domain.namespaces import DOMAIN_INST_NS_OBJ
from domain.rules_paths import rules_path
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.common import error_response, resolve_child_path
from tools.fk_matching import build_master_value_index

logger = logging.getLogger(__name__)


_TOP_UNRESOLVED_SAMPLE = 10


def _load_canonical_masters_declaration() -> dict[str, dict]:
    """rules/contracts/fk_patterns.json::canonical_masters 선언 로드 (P2a).

    Returns: {class_name: {"master_csv": str, "pk_column": str}} or {} if
    section missing / file unreadable.
    """
    path = rules_path("fk_patterns.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        decl = data.get("canonical_masters") or {}
        if isinstance(decl, dict):
            return decl
    except Exception as e:
        logger.debug("canonical_masters 로드 실패 (무시): %s", e)
    return {}


def _compare_declared_vs_discovered(
    declared: dict[str, dict], discovered_index: dict[str, dict[str, str]],
) -> dict:
    """P2a: 선언된 canonical_masters 와 dynamic discovery 결과 대조.

    Returns:
        {
          "declared_classes": [...],           # 선언된 master 클래스 목록
          "discovered_classes": [...],         # master_data.ttl 에서 발견된 클래스
          "missing_in_discovery": [...],       # 선언됐으나 master_data.ttl 에 0 인스턴스
          "missing_in_declaration": [...],     # discovered 되었으나 선언 없음
          "consistent": bool,                  # 양쪽 일치 여부
        }
    """
    declared_set = set(declared.keys())
    discovered_set = {
        cls for cls, entries in discovered_index.items() if entries
    }
    missing_in_discovery = sorted(declared_set - discovered_set)
    missing_in_declaration = sorted(discovered_set - declared_set)
    return {
        "declared_classes": sorted(declared_set),
        "discovered_classes": sorted(discovered_set),
        "missing_in_discovery": missing_in_discovery,
        "missing_in_declaration": missing_in_declaration,
        "consistent": not missing_in_discovery and not missing_in_declaration,
    }


def _extract_satellite_fk_links(
    abox_graph, master_uris: set[str], steel_inst_ns: str,
) -> tuple[int, dict[str, list[dict]]]:
    """A-Box 에서 satellite → master ObjectProperty triple 을 분류.

    Returns:
        (satellite_fk_links, per_stage_samples) where per_stage_samples is
        {"resolved": [...], "unresolved": [...]} — 상위 _TOP_UNRESOLVED_SAMPLE 샘플.
    """
    from rdflib import RDF

    satellite_fk_links = 0
    unresolved_samples: list[dict] = []
    resolved_samples: list[dict] = []

    # 모든 OP triple 순회 (literal object 인 DP 는 제외)
    for s, p, o in abox_graph:
        if not isinstance(o, URIRef):
            continue
        # 도메인 인스턴스 URI 만 (steel 네임스페이스)
        if not str(o).startswith(steel_inst_ns):
            continue
        if not str(s).startswith(steel_inst_ns):
            continue
        # RDF.type 은 class assertion — FK 아님
        if p == RDF.type:
            continue
        satellite_fk_links += 1
        target_uri_str = str(o)
        if target_uri_str in master_uris:
            if len(resolved_samples) < _TOP_UNRESOLVED_SAMPLE:
                resolved_samples.append({
                    "source": str(s),
                    "property": str(p),
                    "target": target_uri_str,
                })
        else:
            if len(unresolved_samples) < _TOP_UNRESOLVED_SAMPLE:
                unresolved_samples.append({
                    "source": str(s),
                    "property": str(p),
                    "target": target_uri_str,
                })

    return satellite_fk_links, {
        "resolved": resolved_samples,
        "unresolved": unresolved_samples,
    }


def _recommend_fixes(
    unresolved_samples: list[dict], master_value_index: dict[str, dict[str, str]],
) -> list[str]:
    """unresolved samples 기반 fix 제안."""
    recs: list[str] = []
    if not unresolved_samples:
        return recs

    # 클래스별 unresolved 집계
    from collections import Counter
    per_class: Counter = Counter()
    for s in unresolved_samples:
        target_local = _local_name(s["target"])
        if target_local and "_" in target_local:
            cls = target_local.split("_", 1)[0]
            per_class[cls] += 1

    for cls, cnt in per_class.most_common(3):
        master_vals = master_value_index.get(cls, {})
        if not master_vals:
            recs.append(
                f"master CSV 에서 {cls} 인스턴스가 0개 — rules/contracts/fk_patterns.json 의 "
                f"'canonical_masters' 섹션에서 {cls} 의 master_csv 선언 확인 필요."
            )
        else:
            sample_val = next(iter(master_vals)) if master_vals else "EQ001"
            recs.append(
                f"{cls} FK 중 {cnt}건 unresolved — L3/L4 fuzzy matching 활성화 고려 "
                f"(rules/domain/domain_config.json::fk_fuzzy_match). master 샘플: {sample_val}"
            )
    return recs


def resolve_entity_references(
    abox_path: str | None = None,
    master_path: str | None = None,
) -> dict[str, Any]:
    """A-Box 의 master-satellite FK 매칭 품질 리포트 (read-only, dry-run 전용).

    tools/fk_matching.py 의 4단 사다리가 실제로 얼마나 잘 동작했는지 사후적으로
    재계산. generate_abox 실행 후 언제든 호출 가능.

    Args:
        abox_path: data/generated/abox 아래 A-Box TTL 파일명. 비어 있으면 config.ABOX_PATH.
        master_path: data/generated/abox 아래 master TTL 파일명. 비어 있으면
            config.MASTER_DATA_PATH.

    Returns:
        {
          "total_instances": int,
          "canonical_sources_detected": int,       # master_data.ttl 인스턴스 수
          "satellite_fk_links": int,               # satellite → 도메인 target OP triple 수
          "fk_match_stages": {
            "resolved": int,   # master 에 존재하는 target 으로 연결된 triple
            "unresolved": int, # master 에 없는 target — dangling 또는 satellite 간 link
          },
          "unresolved_samples": [...],             # 상위 10건
          "resolved_samples": [...],               # 상위 10건 (검증용)
          "recommendations": [...],                # fix 힌트
        }
    """
    try:
        _abox = (
            resolve_child_path(
                GENERATED_ABOX_DIR,
                abox_path,
                allowed_suffixes=(".ttl",),
            )
            if abox_path
            else ABOX_PATH
        )
        _master = (
            resolve_child_path(
                GENERATED_ABOX_DIR,
                master_path,
                allowed_suffixes=(".ttl",),
            )
            if master_path
            else MASTER_DATA_PATH
        )
    except Exception as e:
        return error_response(e, logger=logger)

    if not os.path.exists(_abox):
        return error_response(f"A-Box not found: {_abox}")
    if not os.path.exists(_master):
        logger.warning("master_data.ttl not found, proceeding with empty master set")
        master_graph = _new_graph()
    else:
        master_graph = _new_graph()
        master_graph.parse(_master, format="turtle")

    abox_graph = _new_graph()
    abox_graph.parse(_abox, format="turtle")

    steel_inst_ns = str(DOMAIN_INST_NS_OBJ)

    # master URI set + class 별 normalized index
    master_uris: set[str] = set()
    for s in master_graph.subjects():
        if isinstance(s, URIRef) and str(s).startswith(steel_inst_ns):
            master_uris.add(str(s))

    master_value_index = build_master_value_index(master_uris)

    # A-Box 전체 인스턴스 수 (rdf:type 기반)
    from rdflib import RDF
    total_instances = len({
        s for s in abox_graph.subjects(RDF.type, None)
        if isinstance(s, URIRef) and str(s).startswith(steel_inst_ns)
    })

    # satellite FK link 분류
    fk_link_count, samples = _extract_satellite_fk_links(
        abox_graph, master_uris, steel_inst_ns,
    )

    len(samples["resolved"])
    # 정확한 resolved/unresolved 카운트 — 샘플이 아닌 전체
    resolved_total = 0
    unresolved_total = 0
    for s, p, o in abox_graph:
        if not isinstance(o, URIRef):
            continue
        if not str(o).startswith(steel_inst_ns):
            continue
        if not str(s).startswith(steel_inst_ns):
            continue
        if p == RDF.type:
            continue
        if str(o) in master_uris:
            resolved_total += 1
        else:
            unresolved_total += 1

    recommendations = _recommend_fixes(samples["unresolved"], master_value_index)

    # P2a: declared vs discovered 대조
    declared_masters = _load_canonical_masters_declaration()
    declaration_check = _compare_declared_vs_discovered(
        declared_masters, master_value_index,
    )
    # declared 됐으나 discovery 실패 → recommendations 에 추가
    for cls in declaration_check["missing_in_discovery"]:
        entry = declared_masters.get(cls, {})
        master_csv = entry.get("master_csv", "?")
        recommendations.append(
            f"canonical_masters 에 {cls} 선언됨 (master_csv={master_csv}) 이나 "
            f"master_data.ttl 에 인스턴스 0개 — CSV 파일 존재 / PK 컬럼 설정 확인 필요."
        )

    return {
        "total_instances": total_instances,
        "canonical_sources_detected": len(master_uris),
        "satellite_fk_links": fk_link_count,
        "fk_match_stages": {
            "resolved": resolved_total,
            "unresolved": unresolved_total,
        },
        "unresolved_samples": samples["unresolved"],
        "resolved_samples": samples["resolved"],
        "recommendations": recommendations,
        "declaration_check": declaration_check,
        "abox_path": _abox,
        "master_path": _master,
    }
