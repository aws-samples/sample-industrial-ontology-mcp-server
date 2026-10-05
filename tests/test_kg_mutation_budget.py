"""S9.5 가 표본을 실제로 주입하는가 — 예산이 표본을 구조적으로 막지 않게.

2026-08-29 실측. ``run_kg_mutation_audit(sample_size=8)`` 결과가 이랬다::

    sample_size: 8
    applied:     1          (요약에 이 필드가 없어서 보이지도 않았다)
    skipped_timeout: 7
    caught_by:   [] 전부   → "검출률 0%"

원인은 고정 예산이었다. ``validate_kg`` 1회가 A-Box 250k 트리플에서 **65초**인데
``budget_s=90.0`` 이라 baseline(67s) 만으로 예산을 소진했고, 첫 mutant 이후 7개가
주입 **전에** skip 됐다. 8개를 돌리려면 baseline 포함 9회 = 약 600초가 필요하다.

## 왜 위험한가

이 리포는 같은 실패를 이미 겪었다 (``mutation_audit_never_planted_defects``):
검출률 0% 는 "검증기가 아무것도 못 잡는다" 가 아니라 "결함이 심어지지 않았다" 였다.
그때의 교훈이 "applied/no_effect 를 먼저 세라" 였는데, S4.5 는 그 카운터를 갖고
S9.5 는 갖지 않아 같은 오독이 재발했다.

고정값을 키우는 것으로는 안 된다 — 비용이 A-Box 크기에 비례하므로 어떤 상수든 다른
도메인/규모에서 다시 틀린다. 실측 baseline 비용에서 유도해야 한다.

## 이 테스트의 방향

"예산을 늘렸다" 만 주장하면 무한 예산으로도 통과한다. 네 축을 고정한다:

* 유도 — 예산이 실측 baseline 비용 × 표본 수에서 나온다 (상수 아님)
* 도달 — 표본이 전부 주입 시도를 받는다 (예산으로 잘리지 않는다)
* 상한 — 명시 ``budget_s`` 는 여전히 강제된다 (무한 실행 방지)
* 정직 — 0개 주입 시 검출률은 ``0`` 이 아니라 ``None`` (미측정)
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tools import mutation_runner
from tools.mutation_runner import _BUDGET_SLACK, run_kg_mutations

TBOX = "tests/fixtures/mutation_minimal_tbox.ttl"


@pytest.fixture
def abox(tmp_path) -> str:
    path = tmp_path / "empty_abox.ttl"
    path.write_text("")
    return str(path)


def _stub_validators(monkeypatch, cost_s: float, *, catch_mutants: bool = False):
    """``_run_kg_validators`` 를 고정 비용으로 대체 — 실제 65초를 기다리지 않는다.

    시간은 ``time.monotonic`` 을 감싼 가짜 시계로 진행시킨다 (``time.sleep`` 을
    쓰면 테스트가 실제로 느려진다).

    ``catch_mutants=True`` 면 mutant 그래프(임시 파일 경로)에 FAIL 을 돌려 검출을
    만든다 — 검출 0 이면 어떤 분모를 써도 0% 라서 분모 축을 검증할 수 없다.
    """
    clock = {"t": 1000.0}

    def fake_monotonic():
        return clock["t"]

    def fake_validators(tbox_path, abox_path):
        clock["t"] += cost_s
        is_baseline = tbox_path == TBOX
        if catch_mutants and not is_baseline:
            return {"체크A": "FAIL", "체크B": "PASS"}
        return {"체크A": "PASS", "체크B": "PASS"}

    monkeypatch.setattr(mutation_runner.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(mutation_runner, "_run_kg_validators", fake_validators)
    return clock


# ── 유도: 예산이 실측 비용에서 나온다 ───────────────────────────────────


def test_budget_is_derived_from_measured_baseline(tmp_path, abox, monkeypatch):
    """THE REGRESSION: 65초 검증 × 8 mutant 이 90초 상수에 막히지 않는다."""
    _stub_validators(monkeypatch, cost_s=65.0)

    result = run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"))

    assert result["budget_derived"] is True
    assert result["baseline_cost_s"] == pytest.approx(65.0, abs=0.5)
    # baseline x (표본+1) x slack
    expected = 65.0 * (result["sample_size"] + 1) * _BUDGET_SLACK
    assert result["budget_s"] == pytest.approx(expected, rel=0.01)
    assert result["budget_s"] > 90.0, "이전 고정값보다 커야 한다"


def test_no_mutant_is_skipped_on_budget_with_expensive_validators(
    tmp_path, abox, monkeypatch,
):
    """표본 전체가 주입 시도를 받는가 — 예산으로 잘리면 감사가 무의미하다."""
    _stub_validators(monkeypatch, cost_s=65.0)

    result = run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"))

    timed_out = [m for m in result["mutants"] if m.get("reason") == "skipped_timeout"]
    assert not timed_out, f"예산으로 skip 된 mutant: {[m['mutant_id'] for m in timed_out]}"


def test_derived_budget_exceeds_bare_validator_cost(tmp_path, abox, monkeypatch):
    """예산이 검증 비용 합계보다 **엄격히 크다** — 여유가 없으면 마지막이 잘린다.

    유도식은 검증 호출 비용만 센다. 실제로는 mutant 마다 그래프 변형 + 직렬화 +
    임시파일 I/O 가 더 붙으므로, 합계와 딱 같은 예산이면 그 오버헤드만큼 뒤쪽
    mutant 가 skip 된다 — 예산 결함이 규모가 작아진 형태로 재발한다.

    (가짜 시계는 검증 스텁에서만 진행하므로 오버헤드가 보이지 않는다. 그래서
    시나리오가 아니라 불변식을 고정한다.)
    """
    _stub_validators(monkeypatch, cost_s=20.0)

    result = run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"))

    bare = result["baseline_cost_s"] * (result["sample_size"] + 1)
    assert result["budget_s"] > bare, (
        f"예산 {result['budget_s']}s 가 검증 비용 합계 {bare}s 를 초과하지 않는다 — "
        "변형/직렬화 오버헤드만큼 뒤쪽 mutant 가 잘린다"
    )
    assert _BUDGET_SLACK > 1.0, "slack 이 여유를 주지 않는다"


def test_derived_budget_scales_with_sample_size(tmp_path, abox, monkeypatch):
    """표본을 늘리면 예산도 늘어난다 — 상수라면 큰 표본에서 다시 잘린다."""
    _stub_validators(monkeypatch, cost_s=10.0)
    small = run_kg_mutations(TBOX, abox, sample_size=2, out_dir=str(tmp_path / "s"))

    _stub_validators(monkeypatch, cost_s=10.0)
    large = run_kg_mutations(TBOX, abox, sample_size=8, out_dir=str(tmp_path / "l"))

    assert large["sample_size"] > small["sample_size"]
    assert large["budget_s"] > small["budget_s"]


# ── 상한: 명시 예산은 강제된다 (NEGATIVE 방향) ──────────────────────────


def test_explicit_budget_is_still_enforced(tmp_path, abox, monkeypatch):
    """호출자가 준 ``budget_s`` 는 유도값이 아니라 상한으로 작동한다.

    이 축이 없으면 "예산을 없앴다" 로 통과해 병리적 mutant 가 감사를 무한정
    붙잡을 수 있다.
    """
    _stub_validators(monkeypatch, cost_s=65.0)

    result = run_kg_mutations(
        TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"), budget_s=100.0,
    )

    assert result["budget_derived"] is False
    assert result["budget_s"] == pytest.approx(100.0)
    timed_out = [m for m in result["mutants"] if m.get("reason") == "skipped_timeout"]
    assert timed_out, "명시 예산이 무시됐다 — 상한이 사라졌다"


def test_explicit_zero_budget_skips_everything(tmp_path, abox, monkeypatch):
    """``budget_s=0`` 이 유도로 넘어가지 않는다 (falsy 를 None 으로 오독하는 버그)."""
    _stub_validators(monkeypatch, cost_s=1.0)

    result = run_kg_mutations(
        TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"), budget_s=0.0,
    )

    assert result["budget_derived"] is False
    assert result["applied"] == 0


# ── 정직: 미주입은 0% 가 아니다 ─────────────────────────────────────────


def test_zero_injection_reports_none_not_zero_percent(tmp_path, abox, monkeypatch):
    """0개 주입 시 검출률이 ``None`` 이다 — ``0`` 은 "못 잡았다" 로 오독된다."""
    _stub_validators(monkeypatch, cost_s=1.0)

    result = run_kg_mutations(
        TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"), budget_s=0.0,
    )

    assert result["applied"] == 0
    assert result["catch_rate_pct"] is None, "미측정을 0% 로 보고했다"


def test_summary_counts_injection_before_detection(tmp_path, abox, monkeypatch):
    """요약에 ``applied`` / ``skipped`` 가 있는가 — S4.5 는 있고 S9.5 는 없었다."""
    _stub_validators(monkeypatch, cost_s=1.0)

    result = run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"))

    for field in ("applied", "skipped", "caught", "catch_rate_pct"):
        assert field in result, f"요약에 {field} 없음"
    assert isinstance(result["skipped"], dict)


def test_catch_rate_denominator_is_applied_not_sample_size(
    tmp_path, abox, monkeypatch,
):
    """검출률 분모가 ``applied`` 인가 — ``sample_size`` 면 미주입이 실패로 희석된다.

    두 분모를 실제로 갈라놓아야 판별된다: 예산으로 절반만 주입시키고, 주입된 것은
    전부 검출되게 만든다. 그러면 applied 분모는 100%, sample_size 분모는 50% 다.
    검출이 0 이면 어떤 분모든 0% 라서 이 축이 무력해진다 (첫 작성 시 실제로 mutant
    가 생존했다).
    """
    _stub_validators(monkeypatch, cost_s=10.0, catch_mutants=True)

    # baseline 10s + mutant 2회 = 30s → 3번째 mutant 부터 예산 초과
    result = run_kg_mutations(
        TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"), budget_s=25.0,
    )

    assert 0 < result["applied"] < result["sample_size"], (
        f"분모가 갈리지 않았다: applied={result['applied']} "
        f"sample_size={result['sample_size']}"
    )
    assert result["caught"] == result["applied"], "주입분이 전부 검출돼야 축이 갈린다"
    assert result["catch_rate_pct"] == 100.0, (
        f"주입 {result['applied']}개 전부 검출인데 {result['catch_rate_pct']}% — "
        "분모가 sample_size 다"
    )


# ── 계측 한계: baseline FAIL 체크는 검출을 등록할 수 없다 ────────────────


def test_baseline_failed_checks_are_reported(tmp_path, abox, monkeypatch):
    """이미 FAIL 인 체크를 노출하는가 — 없으면 0% 를 검증기 성능으로 오독한다.

    실측 2026-08-29: 25 체크 중 8개가 baseline FAIL 이었고, 그중
    ``schema_reference_integrity`` 는 로그에서 주입된 ``DOES_NOT_EXIST`` range 를
    **이름까지 지목**했다. 그런데 ``_caught_by`` 는 FAIL→FAIL 을 "변화 없음" 으로
    보므로 검출 0 으로 집계됐다. 계측의 눈먼 범위를 함께 보고해야 한다.
    """
    clock = {"t": 1000.0}

    def fake_monotonic():
        return clock["t"]

    def fake_validators(tbox_path, abox_path):
        clock["t"] += 1.0
        return {"이미빨강": "FAIL", "초록": "PASS", "초록2": "PASS"}

    monkeypatch.setattr(mutation_runner.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(mutation_runner, "_run_kg_validators", fake_validators)

    result = run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"))

    assert result["baseline_failed_checks"] == ["이미빨강"]
    assert result["baseline_failed_count"] == 1
    assert result["measurable_checks"] == 2, "측정 가능 체크 수가 틀렸다"


def test_all_green_baseline_reports_no_blind_checks(tmp_path, abox, monkeypatch):
    """baseline 이 전부 PASS 면 눈먼 체크가 0 이다 (과잉 보고 방지)."""
    _stub_validators(monkeypatch, cost_s=1.0)

    result = run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"))

    assert result["baseline_failed_checks"] == []
    assert result["baseline_failed_count"] == 0
    assert result["measurable_checks"] == 2  # 스텁의 체크A/체크B


def test_baseline_warn_is_not_counted_as_blind(tmp_path, abox, monkeypatch):
    """WARN 은 눈먼 것이 아니다 — WARN→FAIL 승격은 여전히 검출로 등록된다.

    ``!= "PASS"`` 로 넓게 세면 WARN 체크가 측정 불가로 분류돼 눈먼 범위가 과장되고,
    "검증기가 원래 못 잡는다" 는 알리바이가 된다 (지표 매수의 반대 방향 위험).
    """
    clock = {"t": 1000.0}

    def fake_monotonic():
        return clock["t"]

    def fake_validators(tbox_path, abox_path):
        clock["t"] += 1.0
        return {"노랑": "WARN", "빨강": "FAIL", "초록": "PASS"}

    monkeypatch.setattr(mutation_runner.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(mutation_runner, "_run_kg_validators", fake_validators)

    result = run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(tmp_path / "r"))

    assert result["baseline_failed_checks"] == ["빨강"], "WARN 을 눈먼 것으로 셌다"
    assert result["measurable_checks"] == 2, "WARN 체크가 측정 가능 수에서 빠졌다"


def test_written_json_carries_injection_counters(tmp_path, abox, monkeypatch):
    """kg.json 에도 카운터가 실린다 — 파일만 보는 소비자(meta_audit)가 오독하지 않게."""
    _stub_validators(monkeypatch, cost_s=1.0)
    out_dir = tmp_path / "r"

    run_kg_mutations(TBOX, abox, sample_size=4, out_dir=str(out_dir))

    written = json.loads((out_dir / "kg.json").read_text(encoding="utf-8"))
    assert "applied" in written and "catch_rate_pct" in written


# ── 배선: 상수 사본이 남지 않았다 ───────────────────────────────────────


def test_no_fixed_default_budget_remains():
    """``budget_s: float = 90.0`` 같은 고정 기본값이 남아 있지 않다."""
    src = pathlib.Path("tools/mutation_runner.py").read_text(encoding="utf-8")

    assert "budget_s: float | None = None" in src, "기본값이 유도형이 아니다"
    assert "budget_s: float = 90.0" not in src, "고정 기본값이 남아 있다"


def test_mcp_checkpoint_records_applied():
    """``save_step`` 이 applied 를 기록하는가 — 체크포인트만 보면 오독한다."""
    src = pathlib.Path("tools/mutation_runner.py").read_text(encoding="utf-8")
    tail = src.split("def run_kg_mutation_audit")[1]

    assert '"applied": summary["applied"]' in tail, "체크포인트에 applied 없음"
