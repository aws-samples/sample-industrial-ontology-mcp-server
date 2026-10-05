"""tools/tacit_input.py 테스트."""
from __future__ import annotations

import json
import os
from unittest.mock import patch


def _patch_tacit(tmp_path):
    """tacit 관련 경로를 tmp_path 로 리다이렉트하는 patch 컨텍스트."""
    from tools import tacit_input as ti
    tacit_dir = str(tmp_path / "tacit")
    return patch.multiple(
        ti,
        SOURCE_TACIT_DIR=tacit_dir,
        _SKIP_MARKER_PATH=os.path.join(tacit_dir, ".skipped"),
    )


# ── check_tacit_exist ──────────────────────────────────

def test_check_returns_empty_with_guide_when_no_files(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path):
        out = json.loads(ti.check_tacit_exist())
    assert out["success"] is True
    assert out["exists"] is False
    assert out["count"] == 0
    assert out["skipped"] is False
    assert "input_guide" in out
    assert "자연어" in out["input_guide"]


def test_check_returns_files_when_present(tmp_path):
    from tools import tacit_input as ti
    tacit_dir = tmp_path / "tacit"
    tacit_dir.mkdir()
    (tacit_dir / "process_flow.ttl").write_text(
        "@prefix : <http://x/> .\n:A a :B .\n", encoding="utf-8")
    with _patch_tacit(tmp_path):
        out = json.loads(ti.check_tacit_exist())
    assert out["exists"] is True
    assert out["count"] == 1
    assert "process_flow.ttl" in out["files"]
    # 파일 있으면 가이드 생략
    assert "input_guide" not in out


def test_check_respects_skip_marker(tmp_path):
    from tools import tacit_input as ti
    tacit_dir = tmp_path / "tacit"
    tacit_dir.mkdir()
    (tacit_dir / ".skipped").write_text("x", encoding="utf-8")
    with _patch_tacit(tmp_path):
        out = json.loads(ti.check_tacit_exist())
    assert out["exists"] is False
    assert out["skipped"] is True
    # skip 상태면 재질문 방지 — 가이드 없음
    assert "input_guide" not in out


# ── skip_tacit_knowledge ──────────────────────────────

def test_skip_creates_marker(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path):
        out = json.loads(ti.skip_tacit_knowledge())
    assert out["success"] is True
    assert out["skipped"] is True
    assert os.path.exists(out["marker_path"])


# ── add_tacit_from_natural_language ───────────────────

_VALID_TTL = """@prefix steel: <http://example.com/steel-ontology#> .
@prefix steel-inst: <http://example.com/steel-ontology/instances#> .
steel-inst:Step_BF a steel:ProcessBlastFurnace .
"""

_BROKEN_TTL = "this is not turtle @@@ not valid"


def test_add_nl_rejects_empty_text(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path):
        out = json.loads(ti.add_tacit_from_natural_language("flow", ""))
    assert out["success"] is False


def test_add_nl_rejects_unsafe_filename(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path):
        out = json.loads(ti.add_tacit_from_natural_language("../evil", "x"))
    assert out["success"] is False


def test_add_nl_saves_valid_ttl(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path), \
         patch.object(ti, "_load_tbox_context", return_value="# tbox stub"), \
         patch.object(ti, "invoke_bedrock_text",
                      return_value="```turtle\n" + _VALID_TTL + "```"):
        out = json.loads(ti.add_tacit_from_natural_language(
            "process_flow",
            "고로 공정 후 제강 공정이 이어짐"))
    assert out["success"] is True
    assert out["source"] == "natural_language"
    assert out["filename"] == "process_flow.ttl"
    assert out["syntax_valid"] is True
    with open(out["path"], encoding="utf-8") as f:
        saved = f.read()
    assert "steel-inst:Step_BF" in saved


def test_add_nl_rejects_invalid_generated_ttl(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path), \
         patch.object(ti, "_load_tbox_context", return_value="# tbox stub"), \
         patch.object(ti, "invoke_bedrock_text",
                      return_value="```turtle\n" + _BROKEN_TTL + "```"):
        out = json.loads(ti.add_tacit_from_natural_language(
            "flow", "무엇인가"))
    assert out["success"] is False
    assert "구문" in out["error"] or "syntax" in out["error"].lower()
    # 저장된 파일 없음
    tacit_dir = tmp_path / "tacit"
    assert not (tacit_dir / "flow.ttl").exists()


def test_add_nl_overwrite_guard(tmp_path):
    from tools import tacit_input as ti
    tacit_dir = tmp_path / "tacit"
    tacit_dir.mkdir()
    (tacit_dir / "existing.ttl").write_text(_VALID_TTL, encoding="utf-8")
    with _patch_tacit(tmp_path), \
         patch.object(ti, "_load_tbox_context", return_value="# stub"), \
         patch.object(ti, "invoke_bedrock_text",
                      return_value="```turtle\n" + _VALID_TTL + "```"):
        out = json.loads(ti.add_tacit_from_natural_language(
            "existing", "abc", overwrite=False))
    assert out["success"] is False
    assert "존재" in out["error"]


def test_add_nl_clears_skip_marker(tmp_path):
    from tools import tacit_input as ti
    tacit_dir = tmp_path / "tacit"
    tacit_dir.mkdir()
    (tacit_dir / ".skipped").write_text("x", encoding="utf-8")
    with _patch_tacit(tmp_path), \
         patch.object(ti, "_load_tbox_context", return_value="# stub"), \
         patch.object(ti, "invoke_bedrock_text",
                      return_value="```turtle\n" + _VALID_TTL + "```"):
        json.loads(ti.add_tacit_from_natural_language("flow", "x"))
    # 자동 제거
    assert not (tacit_dir / ".skipped").exists()


# ── generate_tacit_from_data ──────────────────────────

def test_generate_from_data_saves_ttl(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path), \
         patch.object(ti, "_load_tbox_context", return_value="# tbox"), \
         patch.object(ti, "_load_csv_summary", return_value="- TableA (10): a, b"), \
         patch.object(ti, "invoke_bedrock_text",
                      return_value="```turtle\n" + _VALID_TTL + "```"):
        out = json.loads(ti.generate_tacit_from_data("auto_flow"))
    assert out["success"] is True
    assert out["source"] == "auto_from_data"
    assert "현장 검증" in out["hint"]


def test_generate_from_data_rejects_invalid_ttl(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path), \
         patch.object(ti, "_load_tbox_context", return_value="# tbox"), \
         patch.object(ti, "_load_csv_summary", return_value="- TableA (10): a"), \
         patch.object(ti, "invoke_bedrock_text",
                      return_value="```turtle\n" + _BROKEN_TTL + "```"):
        out = json.loads(ti.generate_tacit_from_data("auto_flow"))
    assert out["success"] is False


def test_generate_from_data_respects_filename_safety(tmp_path):
    from tools import tacit_input as ti
    with _patch_tacit(tmp_path):
        out = json.loads(ti.generate_tacit_from_data("has spaces!"))
    assert out["success"] is False


def test_generate_from_data_clears_skip_marker(tmp_path):
    from tools import tacit_input as ti
    tacit_dir = tmp_path / "tacit"
    tacit_dir.mkdir()
    (tacit_dir / ".skipped").write_text("x", encoding="utf-8")
    with _patch_tacit(tmp_path), \
         patch.object(ti, "_load_tbox_context", return_value="# tbox"), \
         patch.object(ti, "_load_csv_summary", return_value="- A: x"), \
         patch.object(ti, "invoke_bedrock_text",
                      return_value="```turtle\n" + _VALID_TTL + "```"):
        json.loads(ti.generate_tacit_from_data("auto_flow"))
    assert not (tacit_dir / ".skipped").exists()
