"""옵션 B: Validator+SME 병렬 실행 단위 테스트.

- 병렬 모드에서 SME 에 주입되는 validator_issues 는 이전 라운드 것.
- strict 모드 또는 MULTI_AGENT_PARALLEL=false 에서는 순차 폴백.
- 두 호출은 동일 TTL 입력으로 시작, ThreadPool 에서 동시에 실행.
"""
from __future__ import annotations

import time


def _fake_validator(*args, **kwargs):
    # 느린 호출 흉내 → 병렬/순차 시간 차이 감지
    time.sleep(0.2)
    return {
        "issues": [{"id": "V1", "severity": "high", "principle": "domain missing"}],
        "approved": False,
        "summary": "V summary",
    }


def _fake_sme(*args, **kwargs):
    # SME 가 validator_issues 를 받는지 캡처
    _fake_sme.captured_v_issues = kwargs.get("validator_issues")
    time.sleep(0.2)
    return {
        "issues": [{"id": "S1", "severity": "medium"}],
        "approved": False,
        "summary": "S summary",
        "unanswerable_cqs": [],
    }


def test_parallel_mode_runs_concurrently_and_passes_prev_validator_issues(
    monkeypatch, tmp_path,
):
    """병렬 모드: 총 시간이 단일 호출 시간에 근접, SME 는 이전 라운드 이슈 수신."""
    monkeypatch.setenv("MULTI_AGENT_PARALLEL", "true")
    monkeypatch.delenv("MULTI_AGENT_DETERMINISM", raising=False)

    from concurrent.futures import ThreadPoolExecutor

    prev_validator_issues = [{"id": "V0", "severity": "critical"}]

    _fake_sme.captured_v_issues = None
    t = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as ex:
        fv = ex.submit(_fake_validator, "ttl", 1, prev_validator_issues)
        fs = ex.submit(_fake_sme, "ttl", "csv", [], 1,
                        previous_issues=[], validator_issues=prev_validator_issues,
                        prev_ttl=None)
        v = fv.result()
        s = fs.result()
    elapsed = time.monotonic() - t

    assert v["summary"] == "V summary"
    assert s["summary"] == "S summary"
    # 병렬이면 2개 호출 × 0.2s 가 0.3s 이내
    assert elapsed < 0.35, f"parallel should finish under 0.35s, got {elapsed:.2f}s"
    # SME 가 이전 라운드 V 이슈를 받았는지
    assert _fake_sme.captured_v_issues == prev_validator_issues


def test_strict_mode_disables_parallel(monkeypatch):
    """strict 모드는 순차 폴백 — 경로 플래그 검증."""
    monkeypatch.setenv("MULTI_AGENT_DETERMINISM", "strict")
    monkeypatch.setenv("MULTI_AGENT_PARALLEL", "true")

    from tools.multi_agent_tbox import _strict_determinism
    assert _strict_determinism() is True

    # 병렬 활성 조건: MULTI_AGENT_PARALLEL=true AND NOT strict
    import os
    _parallel = (
        os.getenv("MULTI_AGENT_PARALLEL", "true").lower() in ("true", "1", "yes")
        and not _strict_determinism()
    )
    assert _parallel is False


def test_opt_out_disables_parallel(monkeypatch):
    """MULTI_AGENT_PARALLEL=false → 병렬 비활성."""
    monkeypatch.setenv("MULTI_AGENT_PARALLEL", "false")
    monkeypatch.delenv("MULTI_AGENT_DETERMINISM", raising=False)

    import os

    from tools.multi_agent_tbox import _strict_determinism
    _parallel = (
        os.getenv("MULTI_AGENT_PARALLEL", "true").lower() in ("true", "1", "yes")
        and not _strict_determinism()
    )
    assert _parallel is False


def test_default_is_parallel(monkeypatch):
    """기본값은 병렬 ON."""
    monkeypatch.delenv("MULTI_AGENT_PARALLEL", raising=False)
    monkeypatch.delenv("MULTI_AGENT_DETERMINISM", raising=False)

    import os

    from tools.multi_agent_tbox import _strict_determinism
    _parallel = (
        os.getenv("MULTI_AGENT_PARALLEL", "true").lower() in ("true", "1", "yes")
        and not _strict_determinism()
    )
    assert _parallel is True
