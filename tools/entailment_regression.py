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

from config import INFERRED_PATH
from domain.rules_paths import RULES_ROOT, rules_path
from domain.tbox_utils import _new_graph
from tools.common import error_response, success_response

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
    """Run a SPARQL ASK on g. Return (answer, error_or_None)."""
    try:
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
        golden_path: path to entailment_golden.json. Empty → project default.

    Returns:
        JSON string with total / pass_rate / per-kind counts / failure list.
        ``pass_rate`` is 1.0 for an empty golden set so the step does not block
        early pipelines that haven't authored the golden set yet.
    """
    try:
        path = inferred_path or INFERRED_PATH
        if not os.path.exists(path):
            return error_response(
                f"추론 결과 파일이 없습니다: {path}",
                hint="run_owl_rl_inference 를 먼저 실행하세요.",
                logger=logger,
            )
        g = _new_graph()
        g.parse(path, format="turtle")
        golden = load_golden_set(golden_path or None)
        report = _run_golden_set(g, golden)
        return success_response(report)
    except Exception as e:
        return error_response(e, logger=logger)
