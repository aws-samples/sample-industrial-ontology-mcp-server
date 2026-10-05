"""Golden query 입력 가이드 로직 테스트."""
from __future__ import annotations

import json
import os
from unittest.mock import patch


def _path_in(tmp: str) -> str:
    return os.path.join(tmp, "golden_queries.json")


# ── check_golden_queries_exist ─────────────────────────

def test_check_exist_returns_false_when_file_missing(tmp_path):
    from tools import golden_queries as gq
    with patch.object(gq, "_GOLDEN_PATH", _path_in(str(tmp_path))):
        out = json.loads(gq.check_golden_queries_exist())
    assert out["success"] is True
    assert out["exists"] is False
    assert out["count"] == 0
    # 없을 때만 input_guide 포함
    assert "golden_queries.json" in out["input_guide"]


def test_check_exist_returns_true_after_write(tmp_path):
    from tools import golden_queries as gq
    p = _path_in(str(tmp_path))
    with open(p, "w", encoding="utf-8") as f:
        json.dump([{
            "id": "t1", "question": "Q?", "difficulty": "easy",
            "domains": ["A"], "golden_sparql": "SELECT ?x WHERE {?x a ?t}",
            "assertions": {},
        }], f)
    with patch.object(gq, "_GOLDEN_PATH", p):
        out = json.loads(gq.check_golden_queries_exist())
    assert out["exists"] is True
    assert out["count"] == 1
    # 존재하면 input_guide 필요 없음 (null)
    assert out.get("input_guide") is None


# ── golden_queries_schema_example ──────────────────────

def test_schema_example_returns_full_case():
    from tools import golden_queries as gq
    out = json.loads(gq.golden_queries_schema_example())
    assert out["success"] is True
    ex = out["example"]
    assert isinstance(ex, list) and len(ex) >= 1
    case = ex[0]
    for k in ("id", "question", "difficulty", "domains",
              "golden_sparql", "assertions"):
        assert k in case


# ── add_golden_queries: 입력 검증 ──────────────────────

def test_add_rejects_empty_input(tmp_path):
    from tools import golden_queries as gq
    with patch.object(gq, "_GOLDEN_PATH", _path_in(str(tmp_path))):
        out = json.loads(gq.add_golden_queries(user_provided=""))
    assert out["success"] is False


def test_add_rejects_non_json(tmp_path):
    from tools import golden_queries as gq
    with patch.object(gq, "_GOLDEN_PATH", _path_in(str(tmp_path))):
        out = json.loads(gq.add_golden_queries(user_provided="this is not json"))
    assert out["success"] is False
    assert "JSON" in out["error"] or "파싱" in out["error"]


def test_add_rejects_object_instead_of_array(tmp_path):
    from tools import golden_queries as gq
    with patch.object(gq, "_GOLDEN_PATH", _path_in(str(tmp_path))):
        out = json.loads(gq.add_golden_queries(
            user_provided=json.dumps({"not": "an array"})))
    assert out["success"] is False


def test_add_rejects_case_missing_required_fields(tmp_path):
    from tools import golden_queries as gq
    bad = json.dumps([{
        "id": "x1", "question": "Q?",
        # difficulty/domains/golden_sparql/assertions 누락
    }])
    with patch.object(gq, "_GOLDEN_PATH", _path_in(str(tmp_path))):
        out = json.loads(gq.add_golden_queries(user_provided=bad))
    assert out["success"] is False
    for key in ("difficulty", "domains", "golden_sparql", "assertions"):
        assert key in out["error"]


def test_add_rejects_empty_sparql(tmp_path):
    from tools import golden_queries as gq
    bad = json.dumps([{
        "id": "x1", "question": "Q?", "difficulty": "easy",
        "domains": ["A"], "golden_sparql": "", "assertions": {},
    }])
    with patch.object(gq, "_GOLDEN_PATH", _path_in(str(tmp_path))):
        out = json.loads(gq.add_golden_queries(user_provided=bad))
    assert out["success"] is False
    assert "golden_sparql" in out["error"]


# ── add_golden_queries: 성공 경로 ──────────────────────

def _sample_case(cid: str = "case1") -> dict:
    return {
        "id": cid,
        "question": "테스트 질문?",
        "difficulty": "easy",
        "domains": ["A"],
        "golden_sparql": "SELECT ?x WHERE {?x a ?t}",
        # ``min_rows: 1`` — 예전 픽스처는 0 이었는데 그 형태는 이제 거부된다.
        # ``min_rows: 0`` 은 "0행도 정답" 이라 회귀 게이트를 무력화하므로
        # ``_validate_row_bound`` 가 입력 시점에 막는다 (2026-08-22).
        "assertions": {"min_rows": 1, "required_vars": [],
                        "sparql_must_contain": [], "sparql_must_not_contain": []},
    }


def test_add_creates_file_with_single_case(tmp_path):
    from tools import golden_queries as gq
    p = _path_in(str(tmp_path))
    with patch.object(gq, "_GOLDEN_PATH", p):
        out = json.loads(gq.add_golden_queries(
            user_provided=json.dumps([_sample_case()])))
    assert out["success"] is True
    assert out["added"] == 1
    assert out["total"] == 1
    assert out["mode"] == "append"
    with open(p, encoding="utf-8") as f:
        saved = json.load(f)
    assert len(saved) == 1


def test_add_append_merges_with_existing(tmp_path):
    from tools import golden_queries as gq
    p = _path_in(str(tmp_path))
    with open(p, "w", encoding="utf-8") as f:
        json.dump([_sample_case("existing")], f)
    with patch.object(gq, "_GOLDEN_PATH", p):
        out = json.loads(gq.add_golden_queries(
            user_provided=json.dumps([_sample_case("new")])))
    assert out["total"] == 2


def test_add_rejects_duplicate_id_in_append_mode(tmp_path):
    from tools import golden_queries as gq
    p = _path_in(str(tmp_path))
    with open(p, "w", encoding="utf-8") as f:
        json.dump([_sample_case("dup_id")], f)
    with patch.object(gq, "_GOLDEN_PATH", p):
        out = json.loads(gq.add_golden_queries(
            user_provided=json.dumps([_sample_case("dup_id")])))
    assert out["success"] is False
    assert "dup_id" in out["error"]


def test_add_replace_mode_overwrites_existing(tmp_path):
    from tools import golden_queries as gq
    p = _path_in(str(tmp_path))
    with open(p, "w", encoding="utf-8") as f:
        json.dump([_sample_case("old1"), _sample_case("old2")], f)
    with patch.object(gq, "_GOLDEN_PATH", p):
        out = json.loads(gq.add_golden_queries(
            user_provided=json.dumps([_sample_case("new_only")]),
            append=False))
    assert out["success"] is True
    assert out["mode"] == "replace"
    assert out["total"] == 1
    with open(p, encoding="utf-8") as f:
        saved = json.load(f)
    assert [c["id"] for c in saved] == ["new_only"]


# ── run_golden_queries: 없음일 때 가이드 ───────────────

def test_run_empty_returns_rich_guide(tmp_path):
    from tools import golden_queries as gq
    with patch.object(gq, "_GOLDEN_PATH", _path_in(str(tmp_path))):
        out = json.loads(gq.run_golden_queries())
    assert out["success"] is False
    assert "자기참조" in out["hint"] or "add_golden_queries" in out["hint"]
