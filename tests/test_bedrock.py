"""Tests for tools/bedrock.py — mocks boto3 bedrock-runtime client."""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

from tools.bedrock import _invoke, ask_ontology, convert_sensor_to_rdf


def _make_bedrock_response(text: str, stop_reason: str = "end_turn"):
    """Build a mock Bedrock invoke_model response."""
    body_dict = {
        "content": [{"text": text}],
        "stop_reason": stop_reason,
    }
    mock_response = {
        "body": BytesIO(json.dumps(body_dict).encode("utf-8")),
    }
    return mock_response


class TestInvoke:
    @patch("tools.bedrock._bedrock_client")
    def test_happy_path(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response("Hello world")
        mock_client_fn.return_value = mock_client

        result = _invoke("test prompt")
        assert result == "Hello world"

    @patch("tools.bedrock._bedrock_client")
    def test_max_tokens_warning(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(
            "Truncated output", stop_reason="max_tokens"
        )
        mock_client_fn.return_value = mock_client

        result = _invoke("test prompt")
        assert "Truncated output" in result
        assert "WARNING" in result
        assert "max_tokens" in result

    @patch("tools.bedrock._bedrock_client")
    def test_normal_stop_no_warning(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response(
            "Complete output", stop_reason="end_turn"
        )
        mock_client_fn.return_value = mock_client

        result = _invoke("test prompt")
        assert "WARNING" not in result

    @patch("tools.bedrock._bedrock_client")
    def test_custom_max_tokens(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.invoke_model.return_value = _make_bedrock_response("ok")
        mock_client_fn.return_value = mock_client

        _invoke("test", max_tokens=8192)
        call_body = json.loads(mock_client.invoke_model.call_args[1]["body"])
        assert call_body["max_tokens"] == 8192


class TestConvertSensorToRdf:
    @patch("tools.bedrock._invoke")
    def test_happy_path(self, mock_invoke):
        mock_invoke.return_value = "@prefix steel: <http://example.org/test-ontology#> . steel:x steel:p steel:o ."
        sensor_json = json.dumps({"equipment_id": "EQ001", "temperature": 1200.5})
        result = convert_sensor_to_rdf(sensor_json)
        assert "steel:" in result

    @patch("tools.bedrock._invoke")
    def test_error_timeout(self, mock_invoke):
        mock_invoke.side_effect = Exception("ReadTimeoutError: timeout")
        result = convert_sensor_to_rdf('{"test": 1}')
        data = json.loads(result)
        assert "error" in data
        assert "타임아웃" in data["hint"]

    @patch("tools.bedrock._invoke")
    def test_error_generic(self, mock_invoke):
        mock_invoke.side_effect = Exception("ModelNotFound")
        result = convert_sensor_to_rdf('{"test": 1}')
        data = json.loads(result)
        assert "error" in data
        assert "모델 ID" in data["hint"]


class TestAskOntology:
    @patch("tools.bedrock._invoke")
    def test_happy_path(self, mock_invoke):
        mock_invoke.return_value = "EquipmentMaster는 설비의 기본 정의입니다."
        result = ask_ontology("EquipmentMaster 클래스란?")
        assert "EquipmentMaster" in result

    @patch("tools.bedrock._invoke")
    def test_error_timeout(self, mock_invoke):
        mock_invoke.side_effect = Exception("ReadTimeoutError")
        result = ask_ontology("질문")
        data = json.loads(result)
        assert "error" in data
        assert "타임아웃" in data["hint"]

    @patch("tools.bedrock._invoke")
    def test_error_generic(self, mock_invoke):
        mock_invoke.side_effect = Exception("Unknown error")
        result = ask_ontology("질문")
        data = json.loads(result)
        assert "error" in data
