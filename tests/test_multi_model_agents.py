"""X1 — Multi-Agent 모델 다양화 검증.

역할별 MULTI_AGENT_MODEL_* env var 설정 시 해당 모델 ID 가 Bedrock 호출에
전달되는지 확인. env var 미설정이면 BEDROCK_MODEL_ID fallback (backward compat).
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch


def test_get_agent_model_returns_none_for_no_role():
    """agent_role=None 이면 None 반환 (caller fallback)."""
    from tools import multi_agent_tbox as mat
    assert mat._get_agent_model(None) is None
    assert mat._get_agent_model("") is None


def test_get_agent_model_returns_none_when_env_unset():
    """env var 없으면 None 반환 → caller 가 BEDROCK_MODEL_ID 기본값 사용."""
    from tools import multi_agent_tbox as mat
    with patch.dict(os.environ, {}, clear=False):
        # 명시적으로 3개 env var 제거
        for key in ("MULTI_AGENT_MODEL_ARCHITECT",
                    "MULTI_AGENT_MODEL_VALIDATOR",
                    "MULTI_AGENT_MODEL_SME"):
            os.environ.pop(key, None)
        assert mat._get_agent_model("architect") is None
        assert mat._get_agent_model("validator") is None
        assert mat._get_agent_model("sme") is None


def test_get_agent_model_uses_specific_env():
    """역할별 env var 설정 시 해당 모델 ID 반환."""
    from tools import multi_agent_tbox as mat
    with patch.dict(os.environ, {
        "MULTI_AGENT_MODEL_ARCHITECT": "us.anthropic.claude-opus-4-7-20260415-v1:0",
        "MULTI_AGENT_MODEL_VALIDATOR": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "MULTI_AGENT_MODEL_SME": "us.anthropic.claude-sonnet-4-6-20260101-v1:0",
    }):
        assert mat._get_agent_model("architect") == "us.anthropic.claude-opus-4-7-20260415-v1:0"
        assert mat._get_agent_model("validator") == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert mat._get_agent_model("sme") == "us.anthropic.claude-sonnet-4-6-20260101-v1:0"


def test_get_agent_model_case_insensitive():
    """역할 이름 대소문자 무시."""
    from tools import multi_agent_tbox as mat
    with patch.dict(os.environ, {
        "MULTI_AGENT_MODEL_ARCHITECT": "us.anthropic.claude-opus-4-7-20260415-v1:0",
    }):
        assert mat._get_agent_model("ARCHITECT") == "us.anthropic.claude-opus-4-7-20260415-v1:0"
        assert mat._get_agent_model("Architect") == "us.anthropic.claude-opus-4-7-20260415-v1:0"


def test_get_agent_model_unknown_role_returns_none():
    """알려지지 않은 role 은 None."""
    from tools import multi_agent_tbox as mat
    assert mat._get_agent_model("designer") is None
    assert mat._get_agent_model("unknown") is None


def test_invoke_bedrock_passes_model_id_to_wrapper():
    """_invoke_bedrock 이 agent_role 에 매칭된 모델 ID 를 invoke_bedrock_with_metadata 에 전달."""
    from tools import multi_agent_tbox as mat
    mock_ibm = MagicMock(return_value={"text": "ok", "stop_reason": "end_turn", "usage": {}})
    with patch.dict(os.environ, {
        "MULTI_AGENT_MODEL_VALIDATOR": "test-validator-model",
    }), patch("tools.bedrock.invoke_bedrock_with_metadata", mock_ibm):
        mat._invoke_bedrock("prompt", max_tokens=100, agent_role="validator")
    assert mock_ibm.called
    kwargs = mock_ibm.call_args.kwargs
    assert kwargs["model_id"] == "test-validator-model"


def test_invoke_bedrock_without_agent_role_passes_none():
    """agent_role 미지정이면 model_id=None → bedrock.py 가 BEDROCK_MODEL_ID 사용."""
    from tools import multi_agent_tbox as mat
    mock_ibm = MagicMock(return_value={"text": "ok", "stop_reason": "end_turn", "usage": {}})
    with patch("tools.bedrock.invoke_bedrock_with_metadata", mock_ibm):
        mat._invoke_bedrock("prompt", max_tokens=100)
    assert mock_ibm.called
    kwargs = mock_ibm.call_args.kwargs
    assert kwargs["model_id"] is None


def test_bedrock_wrapper_accepts_model_id_argument():
    """tools.bedrock.invoke_bedrock_with_metadata 가 model_id 인자를 받아 modelId 로 전달."""
    from tools import bedrock as br
    mock_client = MagicMock()
    mock_response = {"body": MagicMock()}
    mock_response["body"].read.return_value = b'{"content": [{"text": "ok"}], "stop_reason": "end_turn", "usage": {}}'
    mock_client.invoke_model.return_value = mock_response
    with patch.object(br, "_bedrock_client", return_value=mock_client):
        result = br.invoke_bedrock_with_metadata(
            "test prompt", max_tokens=100, model_id="custom-model-id"
        )
    assert result["text"] == "ok"
    call_kwargs = mock_client.invoke_model.call_args.kwargs
    assert call_kwargs["modelId"] == "custom-model-id"


def test_bedrock_wrapper_falls_back_to_bedrock_model_id_when_none():
    """model_id=None 이면 BEDROCK_MODEL_ID 로 호출 (backward compat)."""
    from config import BEDROCK_MODEL_ID
    from tools import bedrock as br
    mock_client = MagicMock()
    mock_response = {"body": MagicMock()}
    mock_response["body"].read.return_value = b'{"content": [{"text": "ok"}], "stop_reason": "end_turn", "usage": {}}'
    mock_client.invoke_model.return_value = mock_response
    with patch.object(br, "_bedrock_client", return_value=mock_client):
        br.invoke_bedrock_with_metadata("test prompt", max_tokens=100, model_id=None)
    call_kwargs = mock_client.invoke_model.call_args.kwargs
    assert call_kwargs["modelId"] == BEDROCK_MODEL_ID


def test_invoke_bedrock_text_supports_model_id():
    """invoke_bedrock_text 도 model_id 인자 지원."""
    from tools import bedrock as br
    mock_ibm = MagicMock(return_value={"text": "ok", "stop_reason": "end_turn", "usage": {}})
    with patch.object(br, "invoke_bedrock_with_metadata", mock_ibm):
        br.invoke_bedrock_text("prompt", model_id="custom-model")
    assert mock_ibm.called
    assert mock_ibm.call_args.kwargs["model_id"] == "custom-model"


def test_auto_extend_preserves_agent_role_in_retry():
    """max_tokens truncation 재호출 시 agent_role 유지."""
    from tools import multi_agent_tbox as mat
    # 첫 호출은 truncation, 두 번째는 성공
    call_count = {"n": 0}

    def _mock_ibm(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {"text": "partial", "stop_reason": "max_tokens", "usage": {}}
        return {"text": "full", "stop_reason": "end_turn", "usage": {}}

    with patch.dict(os.environ, {"MULTI_AGENT_MODEL_SME": "sme-model"}), \
         patch("tools.bedrock.invoke_bedrock_with_metadata", side_effect=_mock_ibm) as mock_ibm:
        result = mat._invoke_bedrock("prompt", max_tokens=100, agent_role="sme")
    assert result == "full"
    assert call_count["n"] == 2
    # 두 번째 호출도 sme 모델 사용
    second_call_kwargs = mock_ibm.call_args_list[1].kwargs
    assert second_call_kwargs["model_id"] == "sme-model"


def test_agent_model_env_mapping_completeness():
    """5개 역할 전부 env var 매핑 존재."""
    from tools import multi_agent_tbox as mat
    expected = {"architect", "validator", "sme", "jury", "compromise"}
    assert set(mat._AGENT_MODEL_ENV.keys()) == expected
    # env var 이름 형식 확인
    for role, env_var in mat._AGENT_MODEL_ENV.items():
        assert env_var.startswith("MULTI_AGENT_MODEL_")
        assert role.upper() in env_var
