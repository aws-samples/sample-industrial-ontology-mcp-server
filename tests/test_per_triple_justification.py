"""I2 per-triple reification unit tests.

tools/provenance.py 의 신규 헬퍼 + build_per_triple_reification 단위 검증.
justification prerequisites 는 실제 _build_justifications_from_graphs 결과와
동일한 "설명 문자열" 포맷을 가정한다 (예: "(s, p, o) in pre").
"""
from __future__ import annotations

from rdflib import URIRef
from rdflib.namespace import RDF

from tools.provenance import (
    PROV_BASE,
    _stable_triple_hash,
    build_per_triple_reification,
    make_inferred_stmt_uri,
    make_source_stmt_uri,
    make_tbox_axiom_uri,
)

# ── 관련 단계: hash / URI helpers ────────────────────────────────


def test_stable_hash_consistency():
    """동일 triple 은 항상 동일 해시."""
    h1 = _stable_triple_hash("s", "p", "o")
    h2 = _stable_triple_hash("s", "p", "o")
    assert h1 == h2
    assert len(h1) == 12  # 12자 prefix


def test_stable_hash_uniqueness():
    """다른 triple 은 다른 해시 (순서 바뀌어도 다름)."""
    h_spo = _stable_triple_hash("s", "p", "o")
    h_ops = _stable_triple_hash("o", "p", "s")
    assert h_spo != h_ops

    h1 = _stable_triple_hash("http://e/a", "http://e/b", "http://e/c")
    h2 = _stable_triple_hash("http://e/a", "http://e/b", "http://e/d")
    assert h1 != h2


def test_stable_hash_special_chars():
    """하이픈, 공백, URI 등 특수문자가 있어도 안전."""
    h1 = _stable_triple_hash(
        "http://e/EQ-001", "http://e/has Tag", "http://e/BF 1"
    )
    h2 = _stable_triple_hash(
        "http://e/EQ-001", "http://e/has Tag", "http://e/BF 1"
    )
    assert h1 == h2


def test_make_inferred_stmt_uri_format():
    uri = make_inferred_stmt_uri("http://e/s", "http://e/p", "http://e/o")
    assert str(uri).startswith("urn:inferred-stmt:")
    # 12자 해시 prefix
    assert len(str(uri)) == len("urn:inferred-stmt:") + 12


def test_make_source_stmt_uri_format():
    uri = make_source_stmt_uri("s", "p", "o")
    assert str(uri).startswith("urn:source-stmt:")


def test_make_tbox_axiom_uri_format():
    uri = make_tbox_axiom_uri("s", "p", "o")
    assert str(uri).startswith("urn:tbox-axiom:")


def test_same_triple_stable_uri():
    """같은 triple 은 같은 URI — 이후 dedupe 가능."""
    u1 = make_inferred_stmt_uri("s", "p", "o")
    u2 = make_inferred_stmt_uri("s", "p", "o")
    assert u1 == u2


# ── 관련 단계: build_per_triple_reification ──────────────────────


def _make_justification(
    s: str = "http://e/s",
    p: str = "http://e/p",
    o: str = "http://e/o",
    rule: str = "inverse_of",
    prereqs: list[str] | None = None,
    conf: str = "high",
) -> dict:
    return {
        "triple": (URIRef(s), URIRef(p), URIRef(o)),
        "rule": rule,
        "prerequisites": prereqs or [],
        "confidence": conf,
    }


def test_build_reification_basic():
    """justification 한 개 → reification 6 triple (type/s/p/o/rule/confidence)."""
    activity = URIRef("urn:activity:test")
    js = [_make_justification()]
    g = build_per_triple_reification(js, None, None, activity)

    # rdf:Statement 한 개
    stmts = list(g.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == 1

    stmt = stmts[0]
    # rdf:subject/predicate/object 가 제대로 바인딩
    subjs = list(g.objects(stmt, RDF.subject))
    preds = list(g.objects(stmt, RDF.predicate))
    objs = list(g.objects(stmt, RDF.object))
    assert subjs == [URIRef("http://e/s")]
    assert preds == [URIRef("http://e/p")]
    assert objs == [URIRef("http://e/o")]

    # derivedByRule + confidence 리터럴 (store 가 xsd:string auto-datatype 부여 가능)
    rule_pred = URIRef(f"{PROV_BASE}derivedByRule")
    rules = list(g.objects(stmt, rule_pred))
    assert len(rules) == 1
    assert str(rules[0]) == "inverse_of"


def test_build_reification_skip_rules_default():
    """기본 skip: already_exists / unknown 은 reify 안 함."""
    activity = URIRef("urn:activity:test")
    js = [
        _make_justification(rule="already_exists"),
        _make_justification(s="http://e/s2", rule="unknown"),
    ]
    g = build_per_triple_reification(js, None, None, activity)
    # 모두 skip → 빈 그래프
    stmts = list(g.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == 0


def test_build_reification_custom_skip():
    """skip_rules 파라미터 커스텀."""
    activity = URIRef("urn:activity:test")
    js = [_make_justification(rule="inverse_of")]
    g = build_per_triple_reification(
        js, None, None, activity, skip_rules=frozenset({"inverse_of"}),
    )
    assert len(list(g.subjects(RDF.type, RDF.Statement))) == 0


def test_build_reification_max_entries():
    """max_entries=3 → rule 별 3개만 reify."""
    activity = URIRef("urn:activity:test")
    js = [
        _make_justification(s=f"http://e/s{i}", rule="inverse_of")
        for i in range(10)
    ]
    g = build_per_triple_reification(
        js, None, None, activity, max_entries=3,
    )
    stmts = list(g.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == 3


def test_build_reification_prerequisite_strings():
    """prerequisites 설명 문자열은 prerequisiteNote 리터럴로 보존."""
    activity = URIRef("urn:activity:test")
    prereqs = [
        "(http://e/o, http://e/q, http://e/s) in pre",
        "(http://e/p, owl:inverseOf, http://e/q) in tbox",
    ]
    js = [_make_justification(prereqs=prereqs)]
    g = build_per_triple_reification(js, None, None, activity)

    stmts = list(g.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == 1

    note_pred = URIRef(f"{PROV_BASE}prerequisiteNote")
    notes = list(g.objects(stmts[0], note_pred))
    assert len(notes) == 2
    note_values = {str(n) for n in notes}
    assert prereqs[0] in note_values
    assert prereqs[1] in note_values


def test_build_reification_same_triple_dedupe():
    """동일 (s,p,o) 은 같은 reification URI 로 dedupe (중복 add 는 graph set 의미로 no-op)."""
    activity = URIRef("urn:activity:test")
    js = [_make_justification(), _make_justification()]
    g = build_per_triple_reification(js, None, None, activity)

    # 서로 다른 statement 는 만들어지지 않고, 동일 URI 로 1개만 존재
    stmts = list(g.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == 1


def test_build_reification_activity_link():
    """각 reification 은 prov:wasGeneratedBy activity 링크를 가진다."""
    from domain.namespaces import PROV

    activity = URIRef("urn:activity:test_run")
    js = [_make_justification()]
    g = build_per_triple_reification(js, None, None, activity)

    stmts = list(g.subjects(RDF.type, RDF.Statement))
    gen_by = URIRef(f"{PROV}wasGeneratedBy")
    activities = list(g.objects(stmts[0], gen_by))
    assert activities == [activity]


def test_build_reification_empty_list():
    """빈 justification list → 빈 graph."""
    activity = URIRef("urn:activity:test")
    g = build_per_triple_reification([], None, None, activity)
    assert len(g) == 0
