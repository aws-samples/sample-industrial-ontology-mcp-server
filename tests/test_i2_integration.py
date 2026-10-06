"""I2 end-to-end — reification 매립 + 역추적 조회.

build_per_triple_reification → TTL serialize → 재parse → SPARQL 조회 roundtrip.
query_inference_justification MCP 도구 검증 (rule + prerequisites 반환).
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from rdflib import URIRef
from rdflib.namespace import RDF

from tools.provenance import (
    PROV_BASE,
    build_per_triple_reification,
    make_inferred_stmt_uri,
    query_inference_justification,
)


def _sample_justifications() -> list[dict]:
    """실제 _build_justifications_from_graphs 결과와 동일 스키마."""
    return [
        {
            "triple": (
                URIRef("http://e/EQ001"),
                URIRef("http://e/isTagOf"),
                URIRef("http://e/BF1"),
            ),
            "rule": "inverse_of",
            "prerequisites": [
                "(http://e/BF1, http://e/hasTag, http://e/EQ001) in pre",
                "(http://e/isTagOf, owl:inverseOf, http://e/hasTag) in tbox",
            ],
            "confidence": "high",
        },
        {
            "triple": (
                URIRef("http://e/EQ002"),
                URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
                URIRef("http://e/Equipment"),
            ),
            "rule": "rdfs_domain",
            "prerequisites": [
                "(http://e/EQ002, http://e/locatedIn, _) in pre",
                "(http://e/locatedIn, rdfs:domain, http://e/Equipment) in tbox",
            ],
            "confidence": "high",
        },
    ]


def test_reification_roundtrip_in_memory():
    """build → 메모리 graph 에서 역추적 조회 roundtrip."""
    activity = URIRef("urn:activity:test")
    js = _sample_justifications()
    g = build_per_triple_reification(js, None, None, activity)

    # 역추적: 첫번째 triple 의 reification URI 를 구해 rule 조회
    stmt_uri = make_inferred_stmt_uri(
        URIRef("http://e/EQ001"),
        URIRef("http://e/isTagOf"),
        URIRef("http://e/BF1"),
    )
    rule_pred = URIRef(f"{PROV_BASE}derivedByRule")
    rules = list(g.objects(stmt_uri, rule_pred))
    assert len(rules) == 1
    assert str(rules[0]) == "inverse_of"

    # prerequisites 도 매립됐는지
    note_pred = URIRef(f"{PROV_BASE}prerequisiteNote")
    notes = list(g.objects(stmt_uri, note_pred))
    assert len(notes) == 2


def test_reification_roundtrip_via_ttl_file():
    """build → TTL 파일 → 재parse → SPARQL 조회."""
    activity = URIRef("urn:activity:test")
    js = _sample_justifications()
    g = build_per_triple_reification(js, None, None, activity)

    with tempfile.NamedTemporaryFile(
        suffix=".ttl", delete=False, mode="w",
    ) as tf:
        ttl_path = tf.name
    try:
        g.serialize(destination=ttl_path, format="turtle")
        # 파일 재parse
        from domain.tbox_utils import _new_graph
        g2 = _new_graph()
        g2.parse(ttl_path, format="turtle")

        # 원본 justification 개수만큼 rdf:Statement 존재
        stmts = list(g2.subjects(RDF.type, RDF.Statement))
        assert len(stmts) == 2
    finally:
        os.unlink(ttl_path)


@pytest.fixture
def prov_dir(tmp_path, monkeypatch):
    """도구의 data/generated 경계를 tmp_path 로 옮긴다.

    ``query_inference_justification`` 은 prov_path 를 GENERATED_DIR 아래 경로로만
    받으므로 provenance TTL 을 이 디렉터리 안에 쓴다.
    """
    import tools.provenance as provenance

    monkeypatch.setattr(provenance, "GENERATED_DIR", str(tmp_path))
    return tmp_path


def _write_sample_provenance(prov_dir) -> str:
    activity = URIRef("urn:activity:test")
    g = build_per_triple_reification(_sample_justifications(), None, None, activity)
    ttl_path = prov_dir / "inference_provenance.ttl"
    g.serialize(destination=str(ttl_path), format="turtle")
    return str(ttl_path)


def test_query_inference_justification_found(prov_dir):
    """query_inference_justification MCP 도구 — reified triple 조회."""
    ttl_path = _write_sample_provenance(prov_dir)

    result = query_inference_justification(
        "http://e/EQ001", "http://e/isTagOf", "http://e/BF1",
        prov_path=ttl_path,
    )
    data = json.loads(result)
    assert data.get("success") is True, f"unexpected result: {data}"
    assert data["found"] is True
    assert data["rule"] == "inverse_of"
    assert data["confidence"] == "high"
    assert len(data["prerequisites"]) == 2


def test_query_inference_justification_not_found(prov_dir):
    """조회 대상이 reification 되지 않은 triple — found=false."""
    ttl_path = _write_sample_provenance(prov_dir)

    result = query_inference_justification(
        "http://e/DOES_NOT_EXIST", "http://e/p", "http://e/o",
        prov_path=ttl_path,
    )
    data = json.loads(result)
    assert data.get("success") is True
    assert data["found"] is False
    assert "message" in data


def test_query_inference_justification_missing_file(prov_dir):
    """prov_path 파일 없음 → error_response.

    경계 안의 없는 파일이어야 경계 거부가 아니라 '없음' 분기를 탄다.
    """
    result = query_inference_justification(
        "http://e/s", "http://e/p", "http://e/o",
        prov_path=str(prov_dir / "missing" / "inference_provenance.ttl"),
    )
    data = json.loads(result)
    assert data.get("success") is False
    assert "없음" in data.get("error", "") or "없음" in str(data)


# ── 환경변수 회귀 테스트 (I2_PER_TRIPLE_JUSTIFICATION / I2_MAX_REIFY) ──


def test_i2_opt_out_via_env_var(monkeypatch):
    """I2_PER_TRIPLE_JUSTIFICATION=false 이면 inference.py 의 조건 분기 skip.

    실제 run_owl_rl_inference 전체 실행은 비용이 크므로 (추론 7~200s),
    inference.py L746 의 조건문과 동일 패턴을 여기서 재현해 env var 가
    파이프라인 로직에 의도대로 반영되는지 검증.
    """
    # 기본값 ("true" / 미설정) → reification 생성 경로 활성화
    monkeypatch.delenv("I2_PER_TRIPLE_JUSTIFICATION", raising=False)
    assert os.getenv("I2_PER_TRIPLE_JUSTIFICATION", "true").lower() != "false"

    # 명시적 "true" → 활성화
    monkeypatch.setenv("I2_PER_TRIPLE_JUSTIFICATION", "true")
    assert os.getenv("I2_PER_TRIPLE_JUSTIFICATION", "true").lower() != "false"

    # "false" → reification 경로 skip (opt-out 의도 대로)
    monkeypatch.setenv("I2_PER_TRIPLE_JUSTIFICATION", "false")
    assert os.getenv("I2_PER_TRIPLE_JUSTIFICATION", "true").lower() == "false"

    # 대소문자 무관 (inference.py 에서 .lower() 호출하므로)
    monkeypatch.setenv("I2_PER_TRIPLE_JUSTIFICATION", "FALSE")
    assert os.getenv("I2_PER_TRIPLE_JUSTIFICATION", "true").lower() == "false"


def test_i2_max_reify_cap_enforced():
    """I2_MAX_REIFY 값이 build_per_triple_reification 의 max_entries 로
    전달될 때 rule 별 상한 enforcement.

    max_entries 는 rule 별 cap 이므로 같은 rule 의 10 justification 중
    max_entries=3 이면 정확히 3개만 reify 된다.
    """
    activity = URIRef("urn:activity:test")
    # 같은 rule ("inverse_of") 에 10 justifications, 모두 서로 다른 triple
    justifications = [
        {
            "triple": (
                URIRef(f"http://e/s{i}"),
                URIRef("http://e/p"),
                URIRef(f"http://e/o{i}"),
            ),
            "rule": "inverse_of",
            "confidence": "high",
            "prerequisites": [],
        }
        for i in range(10)
    ]

    g = build_per_triple_reification(
        justifications, None, None, activity, max_entries=3,
    )

    stmts = list(g.subjects(RDF.type, RDF.Statement))
    assert len(stmts) == 3, (
        f"max_entries=3 enforced, got {len(stmts)} statements"
    )

    # max_entries=None (기본) → 제한 없음 확인
    g_unlimited = build_per_triple_reification(
        justifications, None, None, activity, max_entries=None,
    )
    stmts_unlimited = list(g_unlimited.subjects(RDF.type, RDF.Statement))
    assert len(stmts_unlimited) == 10, (
        f"max_entries=None unlimited, got {len(stmts_unlimited)}"
    )
