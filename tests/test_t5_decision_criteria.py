"""T5 — Multi-Agent Compromise Decision Criteria 테스트.

Tests for:
  - `_issue_fingerprint`  — 같은 의미 이슈의 정규화 해시
  - `_compute_issue_persistence` — 라운드 간 연속 등장 추적
  - `_compute_issue_priority` — 결정적 우선순위 score (0~100)
  - `append_compromise_audit` — sidecar 누적 + age-out + size bound
  - `_architect_compromise` — priority_table 주입 경로 (mock LLM)

프로젝트 이슈 스키마: {severity, category, target, symptom, principle, impact, fix}.
Fingerprint 는 symptom 을 주 text 필드로 사용 (fallback: text/issue).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

# ── 관련 단계: helper functions ────────────────────────────


class TestIssueFingerprint:
    """_issue_fingerprint — normalized hash for round-to-round equality."""

    def test_same_issue_different_wording(self):
        """같은 의미 — 소문자/공백 정규화로 동일 fingerprint."""
        from tools.multi_agent_tbox import _issue_fingerprint

        i1 = {
            "symptom": "domain 누락 EquipmentMaster",
            "severity": "high",
            "target": "EquipmentMaster",
        }
        i2 = {
            "symptom": "Domain 누락  EquipmentMaster",  # 대소문자 + 공백
            "severity": "high",
            "target": "equipmentmaster",
        }
        assert _issue_fingerprint(i1) == _issue_fingerprint(i2)

    def test_severity_change_does_not_split_fingerprint(self):
        """severity 가 바뀌어도 **같은** fingerprint 다 (2026-08-22 계약 반전).

        예전 계약은 그 반대였다 ("severity 가 다르면 다른 지문"). 그것을
        의도적으로 뒤집는다 — LLM 이 같은 결함의 심각도를 라운드마다 바꾸면
        (high↔medium) 지문이 갈려 ``age_rounds`` 가 리셋되고, "3라운드 연속
        잔존" 이라는 가장 강한 우선순위 신호가 영구히 발화하지 못한다.

        심각도는 버리는 것이 아니라 **다른 축으로** 반영된다:
        ``_compute_issue_priority`` 가 critical +40 / high +25 / medium +10 을
        더하고, ``_collect_veto_issues`` 가 critical·high 만 veto 후보로 뽑는다.
        동일성 판정과 중요도 판정을 분리하는 것이 옳다.
        """
        from tools.multi_agent_tbox import _issue_fingerprint

        i1 = {"symptom": "같은 텍스트", "severity": "high", "target": "A"}
        i2 = {"symptom": "같은 텍스트", "severity": "low", "target": "A"}
        assert _issue_fingerprint(i1) == _issue_fingerprint(i2)

    def test_fallback_fields(self):
        """symptom 없으면 text → issue 로 fallback."""
        from tools.multi_agent_tbox import _issue_fingerprint

        # symptom 우선
        i_symp = {"symptom": "X", "severity": "high", "target": "T"}
        # fallback: text
        i_text = {"text": "X", "severity": "high", "target": "T"}
        # fallback: issue
        i_issue = {"issue": "X", "severity": "high", "target": "T"}
        # symptom + text/issue 모두 있으면 symptom 우선
        i_both = {"symptom": "X", "text": "Y", "severity": "high", "target": "T"}
        assert _issue_fingerprint(i_symp) == _issue_fingerprint(i_text)
        assert _issue_fingerprint(i_symp) == _issue_fingerprint(i_issue)
        assert _issue_fingerprint(i_symp) == _issue_fingerprint(i_both)


class TestIssuePersistence:
    """_compute_issue_persistence — round-over-round age_rounds tracking."""

    def test_age_rounds_three_consecutive(self):
        """같은 이슈가 3 라운드 연속 → age_rounds=3."""
        from tools.multi_agent_tbox import _compute_issue_persistence

        issue = {"symptom": "persistent issue", "severity": "high", "target": "X"}
        prior = [
            {"validator": {"issues": [issue]}, "sme": {"issues": []}},
            {"validator": {"issues": [issue]}, "sme": {"issues": []}},
        ]
        enriched = _compute_issue_persistence([issue], prior)
        assert enriched[0]["age_rounds"] == 3
        assert "fingerprint" in enriched[0]

    def test_gap_breaks_age(self):
        """연속성 끊기면 age 초기화."""
        from tools.multi_agent_tbox import _compute_issue_persistence

        i1 = {"symptom": "issue A", "severity": "high", "target": "X"}
        i2 = {"symptom": "issue B", "severity": "high", "target": "Y"}
        prior = [
            {"validator": {"issues": [i1]}, "sme": {"issues": []}},
            {"validator": {"issues": [i2]}, "sme": {"issues": []}},  # 끊김
        ]
        enriched = _compute_issue_persistence([i1], prior)
        assert enriched[0]["age_rounds"] == 1


class TestIssuePriority:
    """_compute_issue_priority — deterministic 0~100 score."""

    def test_critical_persistent_cq_sme(self):
        """critical + 3라운드 + CQ 키워드 + SME → cap 100."""
        from tools.multi_agent_tbox import _compute_issue_priority

        issue = {
            "severity": "critical",
            "age_rounds": 3,
            "symptom": "CQ 답변 불가",
            "_agent": "sme",
        }
        score = _compute_issue_priority(issue, cq_coverage_pct=0.7)
        # 40 + 45 + 20 + 5 = 110 → cap 100
        assert score == 100

    def test_low_severity_isolated(self):
        """low + 1라운드 + non-CQ + validator → 15."""
        from tools.multi_agent_tbox import _compute_issue_priority

        issue = {
            "severity": "low",
            "age_rounds": 1,
            "symptom": "naming convention",
            "_agent": "validator",
        }
        score = _compute_issue_priority(issue, cq_coverage_pct=0.9)
        # 0 + 15 + 0 + 0 = 15
        assert score == 15

    def test_deterministic(self):
        """같은 입력 → 같은 출력 (재현성)."""
        from tools.multi_agent_tbox import _compute_issue_priority

        issue = {
            "severity": "high",
            "age_rounds": 2,
            "symptom": "domain range",
            "_agent": "sme",
        }
        s1 = _compute_issue_priority(issue, cq_coverage_pct=0.85)
        s2 = _compute_issue_priority(issue, cq_coverage_pct=0.85)
        assert s1 == s2


# ── 관련 단계/4: audit sidecar integration ────────────────────────────


class TestCompromiseAuditSidecar:
    """append_compromise_audit — atomic write + age-out + size bound."""

    def test_roundtrip_append_and_load(self):
        """append 한 artifact 가 파일에 그대로 저장된다."""
        from tools.compromise_audit import (
            _load_audit,
            append_compromise_audit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "compromise_audit.json")
            artifact = {
                "consensus_reached": False,
                "rounds_conducted": 3,
                "decisions": [{"issue": "x", "priority_score": 80}],
                "overall_rationale": "테스트",
            }
            append_compromise_audit(artifact, path=path)
            data = _load_audit(path)
            assert "iterations" in data
            assert len(data["iterations"]) == 1
            saved = data["iterations"][0]
            assert saved["consensus_reached"] is False
            assert saved["rounds_conducted"] == 3
            # 자동 부여되는 필드 확인
            assert "timestamp" in saved
            assert saved["decisions"][0]["priority_score"] == 80

    def test_size_bound(self):
        """max_iterations 넘으면 오래된 것부터 drop."""
        from tools.compromise_audit import (
            _MAX_ITERATIONS,
            _load_audit,
            append_compromise_audit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "compromise_audit.json")
            for i in range(_MAX_ITERATIONS + 5):
                append_compromise_audit(
                    {"seq": i, "decisions": [], "overall_rationale": ""},
                    path=path,
                )
            data = _load_audit(path)
            # size bound 적용 — 가장 최근 _MAX_ITERATIONS 만 남음
            assert len(data["iterations"]) == _MAX_ITERATIONS
            # 최신 것이 꼬리에 있어야 함
            assert data["iterations"][-1]["seq"] == _MAX_ITERATIONS + 4

    def test_age_out(self):
        """_MAX_AGE_DAYS 를 넘는 항목은 자동 drop."""
        from tools.compromise_audit import (
            _MAX_AGE_DAYS,
            _load_audit,
            _save_audit,
            append_compromise_audit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "compromise_audit.json")
            # 오래된 항목 1개 + 최근 1개
            old_ts = (datetime.now() - timedelta(days=_MAX_AGE_DAYS + 1)).isoformat()
            data = {
                "version": 1,
                "iterations": [
                    {
                        "timestamp": old_ts,
                        "decisions": [],
                        "overall_rationale": "old",
                    },
                ],
            }
            _save_audit(data, path)  # save 는 age-out 을 수행
            data_after = _load_audit(path)
            # 오래된 항목은 save 시 제거돼야 함
            assert len(data_after["iterations"]) == 0

            # 최근 append — 살아남음
            append_compromise_audit(
                {"decisions": [], "overall_rationale": "fresh"}, path=path,
            )
            data_final = _load_audit(path)
            assert len(data_final["iterations"]) == 1
            assert data_final["iterations"][0]["overall_rationale"] == "fresh"

    def test_append_corrupted_file_recovers(self):
        """손상된 파일도 빈 구조로 graceful degrade → append 성공."""
        from tools.compromise_audit import (
            _load_audit,
            append_compromise_audit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "compromise_audit.json")
            # 손상된 JSON
            with open(path, "w") as f:
                f.write("{not valid json")

            append_compromise_audit(
                {"decisions": [], "overall_rationale": "recover"}, path=path,
            )
            data = _load_audit(path)
            assert len(data["iterations"]) == 1


class TestArchitectCompromisePromptIntegration:
    """_architect_compromise — priority_table 이 프롬프트에 반영된다."""

    def test_priority_table_reaches_prompt(self):
        """priority_table 전달 시 프롬프트 안에 priority_score 수치가 나타난다."""
        from tools.multi_agent_tbox import _architect_compromise

        priority_table = [
            {
                "issue_summary": "domain 누락 EquipmentMaster",
                "priority_score": 85,
                "severity": "high",
                "age_rounds": 3,
                "_agent": "validator",
            },
        ]
        captured_prompt: dict = {}

        def fake_invoke(prompt, max_tokens=8192, temperature=0.2, **kwargs):
            captured_prompt["text"] = prompt
            return json.dumps({
                "decisions": [{
                    "issue": "domain 누락 EquipmentMaster",
                    "agent": "validator",
                    "decision": "accept",
                    "action": "rdfs:domain 추가",
                    "rationale": "priority_score=85",
                    "priority_score": 85,
                }],
                "overall_rationale": "priority 기반 판단",
            })

        with patch("tools.multi_agent_tbox._invoke_bedrock", side_effect=fake_invoke):
            result = _architect_compromise(
                ttl="@prefix : <x> .",
                validator_result={"issues": [{
                    "severity": "high",
                    "symptom": "domain 누락 EquipmentMaster",
                    "target": "EquipmentMaster",
                }]},
                sme_result={"issues": []},
                debate_log=[{"round": 1, "validator": {"issues_count": 1},
                             "sme": {"issues_count": 0}}],
                priority_table=priority_table,
            )

        assert "priority_table" in captured_prompt["text"].lower() or \
               "priority_score" in captured_prompt["text"]
        assert "85" in captured_prompt["text"]
        assert "결정 기준" in captured_prompt["text"]
        # 기존 JSON 필드 보존
        assert "decisions" in result
        assert "overall_rationale" in result

    def test_backward_compat_no_priority_table(self):
        """priority_table 미전달 시 기존 동작 유지."""
        from tools.multi_agent_tbox import _architect_compromise

        def fake_invoke(prompt, max_tokens=8192, temperature=0.2, **kwargs):
            return json.dumps({
                "decisions": [],
                "overall_rationale": "no issues",
            })

        with patch("tools.multi_agent_tbox._invoke_bedrock", side_effect=fake_invoke):
            result = _architect_compromise(
                ttl="@prefix : <x> .",
                validator_result={"issues": []},
                sme_result={"issues": []},
                debate_log=[],
            )
        assert "decisions" in result
        assert "overall_rationale" in result

    def test_audit_sidecar_written_after_compromise(self):
        """compromise 완료 후 audit sidecar 파일이 기록된다 (non-blocking)."""
        from tools.multi_agent_tbox import _architect_compromise

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "compromise_audit.json")

            def fake_invoke(prompt, max_tokens=8192, temperature=0.2, **kwargs):
                return json.dumps({
                    "decisions": [{
                        "issue": "X",
                        "agent": "validator",
                        "decision": "accept",
                        "action": "fix",
                        "rationale": "r",
                        "priority_score": 70,
                    }],
                    "overall_rationale": "ok",
                })

            with patch(
                "tools.multi_agent_tbox._invoke_bedrock", side_effect=fake_invoke,
            ), patch(
                "tools.compromise_audit.COMPROMISE_AUDIT_PATH", path,
            ):
                _architect_compromise(
                    ttl="@prefix : <x> .",
                    validator_result={"issues": []},
                    sme_result={"issues": []},
                    debate_log=[],
                    priority_table=[{
                        "issue_summary": "X",
                        "priority_score": 70,
                        "severity": "high",
                        "age_rounds": 1,
                    }],
                )

            assert os.path.exists(path), "audit sidecar 파일이 생성돼야 함"
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            assert len(data["iterations"]) == 1
            # decisions_by_priority 집계 필드 존재
            it = data["iterations"][0]
            assert "decisions_by_priority" in it

    def test_build_priority_table_end_to_end(self):
        """_build_priority_table 이 V/SME 이슈를 enrich → score 내림차순 정렬한다."""
        from tools.multi_agent_tbox import _build_priority_table

        validator_result = {
            "issues": [{
                "severity": "low",
                "symptom": "naming convention",
                "target": "X",
            }],
        }
        sme_result = {
            "issues": [{
                "severity": "critical",
                "symptom": "CQ 답변 불가 경로",
                "target": "Y",
            }],
        }
        debate_log = [
            {
                "validator": {"issues": [{
                    "severity": "critical",
                    "symptom": "CQ 답변 불가 경로",
                    "target": "Y",
                }]},
                "sme": {"issues": []},
            },
        ]
        table = _build_priority_table(
            validator_result, sme_result, debate_log, cq_coverage_pct=0.7,
        )
        assert len(table) == 2
        # 내림차순 — critical/persistent/CQ/SME 가 앞
        assert table[0]["priority_score"] > table[1]["priority_score"]
        # SME + critical(40) + age=2(30) + CQ(20) + sme(5) + CQ_low_coverage(10) = 110 → cap 100
        # (cq_coverage_pct=0.7 < 0.8 이라 CQ 관련 이슈에 +10 추가 — 실제 의도된 동작)
        assert table[0]["_agent"] == "sme"
        assert table[0]["priority_score"] == 100
        # fingerprint 포함
        assert all("fingerprint" in t for t in table)

    def test_audit_sidecar_failure_non_blocking(self):
        """sidecar 쓰기 실패해도 compromise 결과는 정상 반환."""
        from tools.multi_agent_tbox import _architect_compromise

        def fake_invoke(prompt, max_tokens=8192, temperature=0.2, **kwargs):
            return json.dumps({
                "decisions": [],
                "overall_rationale": "ok",
            })

        def failing_append(*args, **kwargs):
            raise OSError("disk full")

        with patch(
            "tools.multi_agent_tbox._invoke_bedrock", side_effect=fake_invoke,
        ), patch(
            "tools.compromise_audit.append_compromise_audit",
            side_effect=failing_append,
        ):
            result = _architect_compromise(
                ttl="@prefix : <x> .",
                validator_result={"issues": []},
                sme_result={"issues": []},
                debate_log=[],
            )
        # 예외가 새어나오지 않음 — 결과는 정상
        assert "decisions" in result
