"""Entailment regression testing — R13 axiom-behaviour verification.

Mutation testing answers "does the checker notice bad T-Box?"; this module
answers the orthogonal "does the schema entail what we want?".

A golden set (rules/domain/entailment_golden.json) declares two lists:
    - positive: ASK queries that MUST return true on the inferred graph.
                They encode the entailments the domain experts expect the
                reasoner to materialise (subClassOf chains, inverseOf,
                propertyChain, disjointUnion coverage, ...).
    - negative: ASK queries that MUST return false. They encode assertions
                the schema must forbid (disjoint violations, cross-scope
                overlaps, ...).

A schema change that breaks either kind is a regression and should fail
the pipeline gate.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from rdflib import Graph

from config import GENERATED_DIR, INFERRED_PATH
from domain.rules_paths import RULES_ROOT, rules_path
from domain.sparql_templates import reject_sparql_egress
from domain.tbox_utils import _new_graph
from tools.common import error_response, resolve_path_within, success_response

logger = logging.getLogger(__name__)

_RULES_DIR = RULES_ROOT
_GOLDEN_PATH = rules_path("entailment_golden.json", base=_RULES_DIR)


def load_golden_set(path: str | None = None) -> dict[str, list[dict]]:
    """Load the golden set from disk. Missing file yields an empty skeleton.

    Returns:
        ``{"positive": [...], "negative": [...]}``. Each item is a dict with
        at least ``id``, ``description``, ``query``.
    """
    p = path or _GOLDEN_PATH
    if not os.path.exists(p):
        return {"positive": [], "negative": []}
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    return {
        "positive": list(raw.get("positive") or []),
        "negative": list(raw.get("negative") or []),
    }


def _eval_ask(g: Graph, query: str) -> tuple[bool, str | None]:
    """Run a SPARQL ASK on g. Return (answer, error_or_None).

    golden 질의 원문을 실행 직전 그대로 egress 가드에 넣는다. 거부된 질의는
    실행하지 않고 오류로 돌려준다.
    """
    try:
        reject_sparql_egress(query)
        result = g.query(query)
        return bool(result.askAnswer), None
    except Exception as e:
        return False, str(e)


def _run_golden_set(
    g: Graph, golden: dict[str, list[dict]],
) -> dict[str, Any]:
    """Execute the golden set against g and return a structured report.

    positive queries PASS when ASK → true.
    negative queries PASS when ASK → false.
    Malformed queries are recorded as failures with ``error`` set.

    The ``pass_rate`` of an empty set is defined as 1.0 so that downstream
    pipeline gates treat "no gold set yet" as non-blocking.
    """
    positive = golden.get("positive") or []
    negative = golden.get("negative") or []
    failures: list[dict[str, Any]] = []
    pos_pass = 0
    neg_pass = 0

    for item in positive:
        ans, err = _eval_ask(g, item.get("query", ""))
        if err is not None:
            failures.append({
                "id": item.get("id"), "kind": "positive",
                "description": item.get("description", ""),
                "expected": True, "actual": None, "error": err,
            })
        elif ans is True:
            pos_pass += 1
        else:
            failures.append({
                "id": item.get("id"), "kind": "positive",
                "description": item.get("description", ""),
                "expected": True, "actual": False,
            })

    for item in negative:
        ans, err = _eval_ask(g, item.get("query", ""))
        if err is not None:
            failures.append({
                "id": item.get("id"), "kind": "negative",
                "description": item.get("description", ""),
                "expected": False, "actual": None, "error": err,
            })
        elif ans is False:
            neg_pass += 1
        else:
            failures.append({
                "id": item.get("id"), "kind": "negative",
                "description": item.get("description", ""),
                "expected": False, "actual": True,
            })

    total = len(positive) + len(negative)
    total_pass = pos_pass + neg_pass
    pass_rate = 1.0 if total == 0 else total_pass / total
    return {
        "total": total,
        "pass_rate": round(pass_rate, 4),
        "positive_total": len(positive),
        "positive_pass": pos_pass,
        "negative_total": len(negative),
        "negative_pass": neg_pass,
        "failures": failures,
    }


def _resolve_inferred_path(inferred_path: str) -> str:
    """공개 ``inferred_path`` 를 data/generated 아래 ``.ttl`` 파일로 제한한다.

    빈 값은 기본 ``INFERRED_PATH`` 를 쓴다.

    Raises:
        ValueError: 허용 디렉터리 밖이거나 확장자가 ``.ttl`` 이 아닐 때.
    """
    if not inferred_path:
        return INFERRED_PATH
    try:
        return resolve_path_within(
            GENERATED_DIR, inferred_path, allowed_suffixes=(".ttl",),
        )
    except ValueError as exc:
        raise ValueError(f"inferred_path: {exc}") from exc


def _resolve_golden_path(golden_path: str) -> str | None:
    """공개 ``golden_path`` 를 rules/ 아래 ``.json`` 파일로 제한한다.

    빈 값은 ``None`` 을 돌려주고 ``load_golden_set`` 이 기본 경로를 쓴다.

    Raises:
        ValueError: 허용 디렉터리 밖이거나 확장자가 ``.json`` 이 아닐 때.
    """
    if not golden_path:
        return None
    try:
        return resolve_path_within(
            _RULES_DIR, golden_path, allowed_suffixes=(".json",),
        )
    except ValueError as exc:
        raise ValueError(f"golden_path: {exc}") from exc


def run_entailment_regression(
    inferred_path: str = "", golden_path: str = "",
) -> str:
    """Verify that the inferred graph entails the expected positives and
    refutes the expected negatives.

    Typically called right after ``run_owl_rl_inference`` in FULL_PIPELINE
    (between S9 KG_VALIDATE and S10 SEMANTIC_DICT) to catch axiom-level
    regressions that mutation testing cannot see.

    Args:
        inferred_path: path to the inferred TTL. Empty → default INFERRED_PATH.
            symlink 해석 후에도 data/generated 아래의 ``.ttl`` 파일이어야 한다.
        golden_path: path to entailment_golden.json. Empty → project default.
            symlink 해석 후에도 rules/ 아래의 ``.json`` 파일이어야 한다.

    Returns:
        JSON string with total / pass_rate / per-kind counts / failure list.
        ``pass_rate`` is 1.0 for an empty golden set so the step does not block
        early pipelines that haven't authored the golden set yet.
    """
    try:
        path = _resolve_inferred_path(inferred_path)
        golden_file = _resolve_golden_path(golden_path)
    except ValueError as e:
        return error_response(e, logger=logger)
    try:
        if not os.path.exists(path):
            return error_response(
                f"추론 결과 파일이 없습니다: {path}",
                hint="run_owl_rl_inference 를 먼저 실행하세요.",
                logger=logger,
            )
        g = _new_graph()
        g.parse(path, format="turtle")
        golden = load_golden_set(golden_file)
        report = _run_golden_set(g, golden)
        return success_response(report)
    except Exception as e:
        return error_response(e, logger=logger)
