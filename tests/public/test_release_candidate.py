"""pytest 실행 결과와 optional marker 선택 계약을 검증한다."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_NODE_ID = re.compile(r"^tests/.+::.+$")
_FAILURE_LINE = re.compile(
    r"^(?:FAILED|ERROR) (tests/.+?)(?: - .*)?$"
)
_SUMMARY_COUNT = re.compile(
    r"(?P<count>\d+) "
    r"(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
)

_JAVA_NODE_IDS = frozenset(
    {
        "tests/test_health_check_java_probe.py::"
        "test_runtime_major_reads_installed_jvm",
        "tests/test_hermit_cache.py::"
        "test_consistency_response_records_its_scope",
        "tests/test_hermit_cache.py::"
        "test_cardinality_blindness_is_reported",
        "tests/test_hermit_cache.py::"
        "test_scope_survives_the_result_cache",
        "tests/test_swrl_inference.py::"
        "TestPelletIntegration::test_quality_violation_rule_fires",
    }
)
_BEDROCK_NODE_IDS = frozenset(
    {
        "tests/public/test_workflow_smoke.py::"
        "test_configured_bedrock_model_responds",
    }
)
_DEFAULT_CONTROL_NODE_ID = (
    "tests/public/test_workflow_smoke.py::"
    "test_server_exposes_workshop_workflow_tools"
)
_MOCKED_REASONER_NODE_ID = (
    "tests/test_hermit_cache.py::"
    "test_validate_owl_consistency_cache_hits_second_call"
)


@dataclass(frozen=True)
class PytestRun:
    """pytest 원시 로그에서 읽은 수락 판정 입력."""

    failed_node_ids: frozenset[str]
    skipped: int
    executed: int


def parse_baseline_node_ids(text: str) -> frozenset[str]:
    """Markdown 또는 일반 텍스트에서 허용 node id 집합을 읽는다."""
    return frozenset(
        line.strip()
        for line in text.splitlines()
        if _NODE_ID.fullmatch(line.strip())
    )


def parse_pytest_log(text: str) -> PytestRun:
    """pytest short summary와 마지막 결과 요약을 구조화한다."""
    failed = frozenset(
        match.group(1)
        for line in text.splitlines()
        if (match := _FAILURE_LINE.match(line))
    )
    summaries = [
        {
            match.group("kind"): int(match.group("count"))
            for match in _SUMMARY_COUNT.finditer(line)
        }
        for line in text.splitlines()
        if _SUMMARY_COUNT.search(line)
    ]
    if not summaries:
        raise ValueError("pytest 결과 요약을 찾을 수 없습니다.")

    summary = summaries[-1]
    executed = sum(
        summary.get(kind, 0)
        for kind in (
            "passed",
            "failed",
            "error",
            "errors",
            "skipped",
            "xfailed",
            "xpassed",
        )
    )
    if executed == 0:
        raise ValueError("실행된 테스트가 없습니다.")
    return PytestRun(
        failed_node_ids=failed,
        skipped=summary.get("skipped", 0),
        executed=executed,
    )


def evaluate_pytest_run(
    run: PytestRun,
    allowed_failures: frozenset[str],
    max_skips: int,
) -> tuple[str, ...]:
    """신규 실패와 skip 상한 위반을 사람이 읽을 수 있게 반환한다."""
    problems = [
        f"신규 실패: {node_id}"
        for node_id in sorted(run.failed_node_ids - allowed_failures)
    ]
    if run.skipped > max_skips:
        problems.append(
            f"skip 상한 초과: actual={run.skipped}, allowed={max_skips}"
        )
    return tuple(problems)


def _collect_node_ids(marker: str | None = None) -> frozenset[str]:
    """현재 인터프리터로 실제 pytest collection 결과를 읽는다."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_health_check_java_probe.py",
        "tests/test_hermit_cache.py",
        "tests/test_swrl_inference.py",
        "tests/public/test_workflow_smoke.py",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
    ]
    if marker is not None:
        command.extend(["-m", marker])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return frozenset(
        line.strip()
        for line in completed.stdout.splitlines()
        if _NODE_ID.fullmatch(line.strip())
    )


def test_default_collection_excludes_optional_prerequisites():
    """기본 수집은 외부 Java와 Bedrock 호출을 선택하지 않아야 한다."""
    collected = _collect_node_ids()

    assert _DEFAULT_CONTROL_NODE_ID in collected
    assert collected.isdisjoint(_JAVA_NODE_IDS | _BEDROCK_NODE_IDS)


def test_requires_java_selects_only_real_java_dependencies():
    """Java marker는 실제 JVM 호출만 고르고 mocked reasoner 결함은 남긴다."""
    collected = _collect_node_ids("requires_java")

    assert collected == _JAVA_NODE_IDS
    assert _MOCKED_REASONER_NODE_ID not in collected


def test_requires_bedrock_selects_the_live_model_smoke():
    """Bedrock marker는 명시적인 실제 모델 호출 smoke만 고른다."""
    assert _collect_node_ids("requires_bedrock") == _BEDROCK_NODE_IDS


def test_acceptance_rejects_failure_outside_baseline():
    """선재 실패 목록에 없는 node id는 수락되지 않아야 한다."""
    run = parse_pytest_log(
        "FAILED tests/test_new.py::test_regression - AssertionError\n"
        "1 failed, 3 passed, 2 skipped in 1.00s\n"
    )

    assert evaluate_pytest_run(run, frozenset(), max_skips=2) == (
        "신규 실패: tests/test_new.py::test_regression",
    )


def test_acceptance_rejects_collection_error():
    """module-level import 오류도 파일 node id로 수락을 막아야 한다."""
    run = parse_pytest_log(
        "ERROR tests/test_validation.py - ImportError\n"
        "1 error, 3 passed in 1.00s\n"
    )

    assert evaluate_pytest_run(run, frozenset(), max_skips=0) == (
        "신규 실패: tests/test_validation.py",
    )


def test_acceptance_rejects_skip_growth():
    """실패가 0이어도 skip 상한을 넘으면 수락되지 않아야 한다."""
    run = parse_pytest_log("4 passed, 3 skipped in 1.00s\n")

    assert evaluate_pytest_run(run, frozenset(), max_skips=2) == (
        "skip 상한 초과: actual=3, allowed=2",
    )


def test_acceptance_allows_only_baseline_failures_within_skip_limit():
    """허용 node id와 skip 상한을 모두 만족한 실행만 수락한다."""
    node_id = "tests/test_known.py::test_existing_failure"
    run = parse_pytest_log(
        f"FAILED {node_id} - AssertionError\n"
        "1 failed, 4 passed, 2 skipped in 1.00s\n"
    )

    assert evaluate_pytest_run(run, frozenset({node_id}), max_skips=2) == ()


def main(argv: list[str] | None = None) -> int:
    """전체 pytest 원시 로그를 baseline과 skip 상한으로 판정한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pytest_log", type=Path)
    parser.add_argument(
        "--baseline",
        type=Path,
        help="허용 선재 실패 node id가 있는 Markdown 또는 텍스트 파일",
    )
    parser.add_argument("--max-skips", type=int, required=True)
    args = parser.parse_args(argv)

    try:
        run = parse_pytest_log(args.pytest_log.read_text(encoding="utf-8"))
        allowed = (
            parse_baseline_node_ids(
                args.baseline.read_text(encoding="utf-8")
            )
            if args.baseline
            else frozenset()
        )
    except (OSError, ValueError) as exc:
        print(f"PYTEST CONTRACT FAILED: {exc}")
        return 2

    problems = evaluate_pytest_run(run, allowed, args.max_skips)
    if problems:
        for problem in problems:
            print(problem)
        print(f"PYTEST CONTRACT FAILED: {len(problems)} problem(s)")
        return 1

    print(
        "PYTEST CONTRACT PASSED: "
        f"executed={run.executed}, failed={len(run.failed_node_ids)}, "
        f"skipped={run.skipped}/{args.max_skips}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
