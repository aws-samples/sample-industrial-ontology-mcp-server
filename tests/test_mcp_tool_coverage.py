"""장기 실행 MCP 도구의 polling 계약 테스트."""

from __future__ import annotations

import json


def test_get_tbox_status_reports_unknown_job_without_side_effects():
    """없는 T-Box job id는 즉시 unknown 응답을 반환해야 한다."""
    from tools.multi_agent_tbox import get_tbox_status

    result = json.loads(get_tbox_status("tbox_missing"))

    assert result["job_id"] == "tbox_missing"
    assert result["status"] == "unknown"


def test_get_inference_status_reports_unknown_local_job():
    """없는 local inference job id는 즉시 unknown 응답을 반환해야 한다."""
    from tools.inference import get_inference_status

    result = json.loads(get_inference_status("infer_missing"))

    assert result["job_id"] == "infer_missing"
    assert result["status"] == "unknown"


def test_get_inference_status_routes_graphdb_job_ids():
    """GraphDB 접두 job id는 GraphDB registry에서 조회해야 한다."""
    from tools.inference import get_inference_status

    result = json.loads(get_inference_status("gdbinfer_missing"))

    assert result["job_id"] == "gdbinfer_missing"
    assert result["status"] == "unknown"
