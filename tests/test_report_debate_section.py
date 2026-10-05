"""S13 보고서가 S2 토론 궤적을 렌더한다 + 없어도 깨지지 않는다.

2026-08-22 감사 실측: ``grep -c "S2" tools/report.py`` → **0**.
``debate``/``consensus``/``veto`` 도 0건이었다. 같은 실행에서
``data/generated/tbox/debate_log.json`` 166KB 가 생성됐는데 보고서는 그 파일을
읽지 않았고, T-Box 에 각인된 ``debate_status`` (dcterms:description) 도 아무도
읽지 않았다. 사용자의 원 질문이 "토론 과정에서 문제가 잘 수정되고 있는지
모르겠다" 였는데, 최종 보고서에 그 질문에 답할 화면이 한 줄도 없었다.

## 이 파일이 고정하는 계약

- **배선**: ``generate_pipeline_report`` 산출물 HTML 에 S2 섹션이 실제로 들어간다.
  단위 함수만 통과하고 산출물이 안 바뀌는 사고를 막는다 (이 리포에서 두 번 있었다 —
  단위 테스트 14건 초록인데 산출물이 안 바뀌었다).
- **graceful degradation**: 기록 없음 / 빈 runs / 깨진 JSON 에서 보고서가 죽지
  않고, 섹션을 **조용히 생략하지 않고** "기록 없음" 을 알린다. 조용히 짧아지면
  운영자가 "토론이 잘 됐다" 로 오독한다.
- **판정 일치**: 인프라 중단이 "미합의" 로 위장되지 않는다. T-Box 에 각인된
  ``debate_status`` 우선순위와 같아야 한다 — 인프라 중단은 검토본이 미검토 초안으로
  조용히 교체된 사고이므로 신뢰도가 전혀 다르다.
- **정직한 결측**: 값이 없는 칸을 0 으로 위장하지 않는다 (``triples: None`` →
  "0" 이면 T-Box 가 비었다고 오독한다).
- **escape**: LLM 이 쓴 이슈 텍스트가 HTML 로 새지 않는다.
"""
from __future__ import annotations

import json

import pytest

from tools import report as R
from tools.debate_log_store import _save, append_debate_run, summarize_latest_run


def _round(
    rnd: int,
    *,
    v_issues: int = 3,
    v_ch: int = 2,
    s_issues: int = 2,
    approved: bool = False,
    cq: float | None = 60.0,
    triples: int | None = 3000,
    issue: dict | None = None,
) -> dict:
    issue = issue or {
        "severity": "critical", "category": "logic", "target": "EquipmentMaster",
        "symptom": "domain 누락", "fix": "rdfs:domain 추가",
    }
    return {
        "round": rnd,
        "validator": {
            "issues_count": v_issues, "critical_high": v_ch,
            "approved": approved, "summary": "v", "issues": [issue],
        },
        "sme": {
            "issues_count": s_issues, "critical_high": 1,
            "approved": approved, "summary": "s", "issues": [issue],
        },
        "cq_runtime": {"coverage_pct": cq, "unanswerable_count": 2},
        "revision": {"valid": True, "triples": triples, "no_progress": False},
        "quality_metrics": {"fair": {"overall": 72.9}, "farber": {"overall": 57.2}},
    }


@pytest.fixture
def logged_run(tmp_path, monkeypatch):
    """debate_log.json 이 tmp 에 있고 run 1건이 기록된 상태."""
    path = tmp_path / "debate_log.json"
    monkeypatch.setattr("tools.debate_log_store.DEBATE_LOG_PATH", str(path))
    return path


class TestGracefulDegradation:
    """기록이 없는 환경 (첫 실행 / 다른 도메인 / 수동 T-Box) 에서도 살아있다."""

    def test_missing_file_announces_absence_instead_of_vanishing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "tools.debate_log_store.DEBATE_LOG_PATH", str(tmp_path / "none.json"))
        html = R._render_debate_section()
        # 섹션이 사라지면 안 된다 — 침묵은 "잘 됐다" 로 오독된다.
        assert html, "기록 없음일 때 빈 문자열이면 운영자가 결측을 알 수 없다"
        assert "S2 토론 기록 없음" in html
        assert "generate_tbox_collaborative" in html

    def test_empty_runs_list_announces_absence(self, logged_run):
        _save({"version": 1, "runs": []}, str(logged_run))
        assert "S2 토론 기록 없음" in R._render_debate_section()

    def test_corrupt_json_does_not_raise(self, logged_run):
        logged_run.write_text("{ not json", encoding="utf-8")
        assert "S2 토론 기록 없음" in R._render_debate_section()

    def test_summarize_returns_none_when_no_record(self, tmp_path):
        assert summarize_latest_run(str(tmp_path / "none.json")) is None


class TestRoundTrajectoryIsRendered:
    """라운드별 궤적이 실제로 표에 나온다 — 이것이 사용자의 질문에 답한다."""

    def test_rounds_and_recurring_issue_appear(self, logged_run):
        append_debate_run(
            rounds=[_round(2), _round(3), _round(4)],
            consensus_reached=False, veto_lock_triggered=False,
            statistics={"classes": 53, "object_properties": 88},
            architect_initial={"classes": 55, "object_properties": 84},
            duration_seconds=2883.7, path=str(logged_run),
        )
        html = R._render_debate_section()
        assert "S2 다중 에이전트 토론 궤적" in html
        for label in ("R2", "R3", "R4"):
            assert f"<strong>{label}</strong>" in html
        # 3라운드 잔존 이슈가 반복 표에 나와야 한다 — "잘 수정되고 있는가" 의 답.
        assert "3회" in html
        assert "EquipmentMaster" in html
        # 초안 → 최종 델타가 보여야 한다 (토론이 무엇을 바꿨나).
        assert "84" in html and "88" in html

    def test_resolved_issues_report_no_recurrence(self, logged_run):
        """NEGATIVE 방향: 매 라운드 **다른** 이슈면 반복 0건이라고 정직히 말한다.

        "카운터 >= 1" 을 주장하면 항상 반복이 있다고 보고하는 버그를 통과시킨다.
        """
        rounds = [
            _round(2, issue={"severity": "high", "category": "logic",
                             "target": "A", "symptom": "x"}),
            _round(3, issue={"severity": "high", "category": "mapping",
                             "target": "B", "symptom": "y"}),
        ]
        append_debate_run(rounds=rounds, consensus_reached=True,
                          veto_lock_triggered=False, path=str(logged_run))
        summary = summarize_latest_run(str(logged_run))
        assert summary["recurring"] == []
        assert "2라운드 이상 잔존한 이슈 없음" in R._render_debate_section()

    def test_missing_numbers_are_not_rendered_as_zero(self, logged_run):
        """``triples: None`` 을 "0" 으로 쓰면 T-Box 가 비었다고 오독된다."""
        append_debate_run(rounds=[_round(2, triples=None, cq=None)],
                          consensus_reached=False, veto_lock_triggered=False,
                          path=str(logged_run))
        html = R._render_debate_section()
        assert "<td>—</td>" in html
        assert "<td>0</td>" not in html


class TestVerdictMatchesTboxAnnotation:
    """배지가 T-Box 의 ``debate_status`` 와 같은 우선순위를 쓴다."""

    def test_consensus(self):
        assert R._debate_verdict({"consensus_reached": True}) == ("pass", "합의 도달")

    def test_veto_lock(self):
        cls, label = R._debate_verdict(
            {"consensus_reached": False, "veto_lock_triggered": True})
        assert cls == "fail" and "veto" in label

    def test_max_rounds(self):
        cls, label = R._debate_verdict(
            {"consensus_reached": False, "veto_lock_triggered": False})
        assert cls == "fail" and "라운드 소진" in label

    def test_infra_abort_outranks_unresolved(self):
        """인프라 중단이 "미합의" 로 위장되면 미검토 초안 교체 사고를 놓친다."""
        cls, label = R._debate_verdict({
            "consensus_reached": False, "veto_lock_triggered": True,
            "infra_abort": {"phase": "validator", "round": 3},
        })
        assert cls == "fail"
        assert "인프라" in label, label

    def test_infra_abort_warning_is_shown(self, logged_run):
        append_debate_run(
            rounds=[_round(2)], consensus_reached=False, veto_lock_triggered=False,
            infra_abort={"phase": "validator", "round": 3, "error": "throttled"},
            path=str(logged_run),
        )
        html = R._render_debate_section()
        assert "인프라 장애로 중단" in html
        assert "S2 재실행" in html


class TestEscaping:
    """LLM 이 쓴 텍스트가 HTML 로 새지 않는다."""

    def test_issue_text_is_escaped(self, logged_run):
        evil = {"severity": "high", "category": "<script>alert(1)</script>",
                "target": "<img src=x onerror=1>", "symptom": "<b>bold</b>"}
        append_debate_run(rounds=[_round(2, issue=evil), _round(3, issue=evil)],
                          consensus_reached=False, veto_lock_triggered=False,
                          path=str(logged_run))
        html = R._render_debate_section()
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x onerror=1>" not in html
        assert "&lt;script&gt;" in html


class TestWiredIntoArtifact:
    """단위 함수 통과만으로는 부족하다 — 산출물 HTML 에 실제로 들어가야 한다."""

    def test_build_html_template_contains_debate_placeholder(self):
        import inspect
        src = inspect.getsource(R._build_html)
        assert "_render_debate_section()" in src, \
            "_build_html 이 S2 섹션을 만들지 않으면 산출물에 안 나온다"
        assert "{debate_html}" in src, \
            "섹션을 만들어도 템플릿에 끼우지 않으면 버려진다"

    def test_generate_pipeline_report_emits_section(self, tmp_path, monkeypatch):
        generated = tmp_path / "generated"
        reports = generated / "reports"
        reports.mkdir(parents=True)
        monkeypatch.setattr("config.GENERATED_DIR", str(generated))
        monkeypatch.setattr(R, "GENERATED_REPORTS_DIR", str(reports))
        monkeypatch.setattr(R, "webbrowser", type("_W", (), {"open": staticmethod(
            lambda *a, **k: True)})())
        monkeypatch.setattr(
            "tools.debate_log_store.DEBATE_LOG_PATH", str(tmp_path / "debate_log.json"))
        append_debate_run(rounds=[_round(2), _round(3)], consensus_reached=False,
                          veto_lock_triggered=False,
                          path=str(tmp_path / "debate_log.json"))
        out = json.loads(R.generate_pipeline_report(open_report=False))
        assert out["success"] is True, out.get("error")
        html = (reports / "pipeline_report.html").read_text(encoding="utf-8")
        assert "S2 다중 에이전트 토론 궤적" in html


class TestGoldenRegressionEscapeCrash:
    """``_build_html`` 안의 ``html.escape`` 는 로컬 ``html`` 을 가려 AttributeError 다.

    실측 2026-08-22: golden_history.json 에 regression 이 하나라도 있으면
    ``generate_pipeline_report`` 가 통째로 실패했다
    (``'str' object has no attribute 'escape'``). 회귀가 있을 때만 밟는 경로라
    지금까지 발견되지 않았다 — 정확히 회귀를 보고해야 할 때 보고서가 사라진다.
    """

    def test_report_survives_golden_regression(self, tmp_path, monkeypatch):
        generated = tmp_path / "generated"
        reports = generated / "reports"
        reports.mkdir(parents=True)
        monkeypatch.setattr("config.GENERATED_DIR", str(generated))
        monkeypatch.setattr(R, "GENERATED_REPORTS_DIR", str(reports))
        monkeypatch.setattr(R, "webbrowser", type("_W", (), {"open": staticmethod(
            lambda *a, **k: True)})())
        (reports / "golden_history.json").write_text(json.dumps([{
            "timestamp": "2026-08-22T09:00:00",
            "summary": {"total": 2, "passed": 1, "failed": 1, "pass_rate": 50.0},
            "regression": {"has_previous": True,
                           "regressions": [{"id": "<img src=x>", "from": "PASS",
                                            "to": "FAIL"}],
                           "recoveries": [{"id": "G2"}]},
            "results": [{}, {}],
        }]), encoding="utf-8")
        out = json.loads(R.generate_pipeline_report(open_report=False))
        assert out["success"] is True, out.get("error")
        html = (reports / "pipeline_report.html").read_text(encoding="utf-8")
        assert "회귀 감지" in html
        # user-provided id 는 escape 돼야 한다.
        assert "<img src=x>" not in html
        assert "&lt;img src=x&gt;" in html
