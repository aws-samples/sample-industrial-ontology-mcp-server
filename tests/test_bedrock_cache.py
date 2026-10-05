"""Bedrock prompt caching (cached_prefix) 테스트."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch


def _mock_client_response(text: str = "OK", usage: dict | None = None) -> MagicMock:
    body = {
        "content": [{"text": text}],
        "stop_reason": "end_turn",
        "usage": usage or {},
    }
    mock_body = MagicMock()
    mock_body.read.return_value = json.dumps(body).encode("utf-8")
    client = MagicMock()
    client.invoke_model.return_value = {"body": mock_body}
    return client


def test_content_is_plain_string_when_no_cached_prefix():
    client = _mock_client_response()
    with patch("tools.bedrock._bedrock_client", return_value=client):
        from tools.bedrock import invoke_bedrock_with_metadata
        invoke_bedrock_with_metadata("hello world", cached_prefix=None)
    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert body["messages"][0]["content"] == "hello world"


def test_content_uses_block_array_when_cached_prefix_given():
    client = _mock_client_response()
    with patch("tools.bedrock._bedrock_client", return_value=client):
        from tools.bedrock import invoke_bedrock_with_metadata
        invoke_bedrock_with_metadata("variable_body", cached_prefix="PREFIX_STATIC")
    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["text"] == "PREFIX_STATIC"
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[1]["text"] == "variable_body"
    assert "cache_control" not in content[1]


def test_usage_dict_is_exposed():
    usage = {
        "input_tokens": 12,
        "output_tokens": 34,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 0,
    }
    client = _mock_client_response(usage=usage)
    with patch("tools.bedrock._bedrock_client", return_value=client):
        from tools.bedrock import invoke_bedrock_with_metadata
        result = invoke_bedrock_with_metadata("p", cached_prefix="x")
    assert result["usage"]["input_tokens"] == 12
    assert result["usage"]["cache_read_input_tokens"] == 100


def test_invoke_text_passes_cached_prefix():
    client = _mock_client_response(text="RESULT")
    with patch("tools.bedrock._bedrock_client", return_value=client):
        from tools.bedrock import invoke_bedrock_text
        out = invoke_bedrock_text("body", cached_prefix="PREFIX")
    assert out == "RESULT"
    body = json.loads(client.invoke_model.call_args.kwargs["body"])
    assert isinstance(body["messages"][0]["content"], list)
