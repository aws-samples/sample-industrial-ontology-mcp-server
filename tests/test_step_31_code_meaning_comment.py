"""Step 31 (code-meaning comment annotation) 검증.

step_31_code_meaning_comment.apply 가 rules/domain/code_meanings.json 의 코드값 의미를
code-valued DatatypeProperty 의 한국어 rdfs:comment 에 append 하는지, 그리고
멱등성 / override-safe (중복 comment 미생성) / 컬럼코드 복원 두 경로를 검증.
"""
from __future__ import annotations

from unittest.mock import patch

from rdflib import OWL, RDF, RDFS, Literal, URIRef

from domain.tbox_utils import _new_graph
from tools.quality_steps import step_31_code_meaning_comment as s31
from tools.quality_steps._base import StepContext

STEEL_STR = "http://example.com/steel-ontology#"


def _u(name: str) -> URIRef:
    return URIRef(STEEL_STR + name)


def _graph():
    """Class MaterialA + MaterialADetail with two code-valued DPs (label-token & name paths)."""
    g = _new_graph()
    g.add((_u("MaterialA"), RDF.type, OWL.Class))
    g.add((_u("MaterialADetail"), RDF.type, OWL.Class))
    # DP #1 — column code embedded in the comment token.
    g.add((_u("codeCol1"), RDF.type, OWL.DatatypeProperty))
    g.add((_u("codeCol1"), RDFS.domain, _u("MaterialA")))
    g.add((_u("codeCol1"), RDFS.comment,
           Literal("표면 결함 코드(CODE_COL_1)", lang="ko")))
    # DP #2 — no token in comment; must recover code from the DP name tail.
    g.add((_u("materialADetailCodeCol1"), RDF.type, OWL.DatatypeProperty))
    g.add((_u("materialADetailCodeCol1"), RDFS.domain, _u("MaterialADetail")))
    g.add((_u("materialADetailCodeCol1"), RDFS.comment,
           Literal("2차 공정 표면처리 발생 코드.", lang="ko")))
    return g


_FAKE_CODES = {
    "columns": {
        "CODE_COL_1": {"10": "typeA", "20": "typeA2", "30": "typeB"}
    }
}


def _ko_comments(g, dp):
    return [str(o) for o in g.objects(_u(dp), RDFS.comment)
            if isinstance(o, Literal) and o.language == "ko"]


def test_appends_code_meanings_via_label_token():
    g = _graph()
    with patch.object(s31, "_load_code_columns", return_value=_FAKE_CODES["columns"]):
        res = s31.apply(g, StepContext(domain_ns=STEEL_STR))
    assert res.stats["code_comment_annotated"] == 2
    kos = _ko_comments(g, "codeCol1")
    assert len(kos) == 1, "ko comment must stay single (override-safe)"
    assert "코드값: 10=typeA, 20=typeA2, 30=typeB" in kos[0]
    assert kos[0].startswith("표면 결함 코드(CODE_COL_1)")


def test_recovers_column_from_dp_name_tail():
    g = _graph()
    with patch.object(s31, "_load_code_columns", return_value=_FAKE_CODES["columns"]):
        s31.apply(g, StepContext(domain_ns=STEEL_STR))
    kos = _ko_comments(g, "materialADetailCodeCol1")
    assert len(kos) == 1
    assert "코드값: 10=typeA" in kos[0]


def test_idempotent_second_run_annotates_zero():
    g = _graph()
    with patch.object(s31, "_load_code_columns", return_value=_FAKE_CODES["columns"]):
        s31.apply(g, StepContext(domain_ns=STEEL_STR))
        res2 = s31.apply(g, StepContext(domain_ns=STEEL_STR))
    assert res2.stats["code_comment_annotated"] == 0
    # still exactly one ko comment after a second pass.
    assert len(_ko_comments(g, "codeCol1")) == 1


def test_noop_when_code_meanings_absent():
    g = _graph()
    with patch.object(s31, "_load_code_columns", return_value={}):
        res = s31.apply(g, StepContext(domain_ns=STEEL_STR))
    assert res.stats["code_comment_annotated"] == 0
    assert res.stats.get("skipped") == "no_code_meanings"
    assert _ko_comments(g, "codeCol1") == ["표면 결함 코드(CODE_COL_1)"]


def test_camel_tail_to_column():
    classes = {"materialadetail", "materiala", "materialb"}
    assert s31._camel_tail_to_column("materialADetailCodeCol1", classes) == "CODE_COL_1"
    assert s31._camel_tail_to_column("materialADetailTypeCol2", classes) == "TYPE_COL_2"


def test_format_clause_deterministic_order():
    clause = s31._format_clause({"30": "typeB", "10": "typeA", "20": "typeA2"})
    assert clause == "코드값: 10=typeA, 20=typeA2, 30=typeB"
