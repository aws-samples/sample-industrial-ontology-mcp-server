"""S0 CQ 입력 우선 정책 테스트."""
from __future__ import annotations

import json
import os
from unittest.mock import patch


def _cq_path_in(tmp: str) -> str:
    return os.path.join(tmp, "competency_questions.json")


def test_check_exist_false_when_file_missing(tmp_path):
    from tools import competency_questions as cq
    with patch.object(cq, "_CQ_PATH", _cq_path_in(str(tmp_path))):
        out = json.loads(cq.check_competency_questions_exist())
    assert out["success"] is True
    assert out["exists"] is False
    assert out["count"] == 0


def test_check_exist_true_after_write(tmp_path):
    from tools import competency_questions as cq
    p = _cq_path_in(str(tmp_path))
    with open(p, "w", encoding="utf-8") as f:
        json.dump([{"question_ko": "테스트 질문"}], f, ensure_ascii=False)
    with patch.object(cq, "_CQ_PATH", p):
        out = json.loads(cq.check_competency_questions_exist())
    assert out["exists"] is True
    assert out["count"] == 1


def test_generate_rejects_when_no_input_no_approval(tmp_path):
    from tools import competency_questions as cq
    with patch.object(cq, "_CQ_PATH", _cq_path_in(str(tmp_path))):
        out = json.loads(cq.generate_competency_questions())
    assert out["success"] is False
    assert "제공" in out["error"] or "동의" in out["error"]
    # Bedrock 호출 없이 빠르게 실패해야 함 (LLM mock 없이도 통과)


def test_generate_from_user_provided_json(tmp_path):
    from tools import competency_questions as cq
    user = json.dumps([
        {"question_ko": "Q1?", "domains": ["TableA"], "difficulty": "hard"},
        {"question_ko": "Q2?"},
    ], ensure_ascii=False)
    with patch.object(cq, "_CQ_PATH", _cq_path_in(str(tmp_path))):
        out = json.loads(cq.generate_competency_questions(user_provided=user))
    assert out["success"] is True
    assert out["source"] == "user_provided"
    assert out["count"] == 2
    assert all(q["source"] == "user_provided" for q in out["questions"])
    # 저장된 파일도 동일 내용
    with open(out["path"], encoding="utf-8") as f:
        saved = json.load(f)
    assert len(saved) == 2


def test_generate_from_user_provided_plain_lines(tmp_path):
    from tools import competency_questions as cq
    user = "# 주석 무시\n설비별 가동률은?\n\n에너지 사용량 추세는?"
    with patch.object(cq, "_CQ_PATH", _cq_path_in(str(tmp_path))):
        out = json.loads(cq.generate_competency_questions(user_provided=user))
    assert out["success"] is True
    assert out["count"] == 2
    ids = {q["id"] for q in out["questions"]}
    assert ids == {"CQ01", "CQ02"}


def test_generate_auto_approved_triggers_llm_path(tmp_path):
    from tools import competency_questions as cq
    # CSV summary를 빈 것이 아닌 값으로 mock, invoke_bedrock_text도 mock
    with patch.object(cq, "_CQ_PATH", _cq_path_in(str(tmp_path))), \
         patch.object(cq, "_load_csv_summary", return_value="- TableA (10행): col1, col2"), \
         patch.object(cq, "_load_tacit_summary", return_value="없음"), \
         patch.object(cq, "invoke_bedrock_text",
                      return_value='[{"question_ko": "자동 생성된 질문?", "domains": ["TableA"], "difficulty": "medium"}]'):
        out = json.loads(cq.generate_competency_questions(count=1, auto_approved=True))
    assert out["success"] is True
    assert out["source"] == "auto_generated"
    assert out["questions"][0]["source"] == "auto_generated"


def test_empty_user_provided_still_requires_approval(tmp_path):
    from tools import competency_questions as cq
    with patch.object(cq, "_CQ_PATH", _cq_path_in(str(tmp_path))):
        out = json.loads(cq.generate_competency_questions(user_provided="   \n   "))
    assert out["success"] is False
    assert "동의" in out["error"] or "제공" in out["error"]


def test_user_provided_empty_lines_only_errors(tmp_path):
    from tools import competency_questions as cq
    with patch.object(cq, "_CQ_PATH", _cq_path_in(str(tmp_path))):
        # 실제 값은 있지만 파싱 결과 비어있음 → 주석만 있는 경우
        out = json.loads(cq.generate_competency_questions(user_provided="# comment only"))
    assert out["success"] is False
