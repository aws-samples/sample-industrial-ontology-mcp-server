"""Tests for CQ Feedback Loop — _check_tbox_phase, _check_abox_phase,
_check_inferred_phase, check_cq_coverage MCP tool.

TDD: 테스트 먼저 작성 후 구현.
"""

import json
import os
from unittest.mock import patch

import pytest

from tools.competency_questions import (
    _check_abox_phase,
    _check_inferred_phase,
    _check_tbox_phase,
    check_cq_coverage,
)

# ── 공유 테스트 픽스처 ──────────────────────────────────────────────

_TTL_PREFIX = """\
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""

#: 세 클래스가 OP 로 이어진 T-Box (happy path).
_TBOX_CONNECTED = _TTL_PREFIX + """
steel:EquipmentMaster a owl:Class .
steel:AlarmEvents a owl:Class .
steel:EnergyUsage a owl:Class .

steel:hasAlarm a owl:ObjectProperty ;
    rdfs:domain steel:EquipmentMaster ;
    rdfs:range steel:AlarmEvents .

steel:hasEnergyUsage a owl:ObjectProperty ;
    rdfs:domain steel:EquipmentMaster ;
    rdfs:range steel:EnergyUsage .
"""

#: 클래스는 있지만 이어주는 OP 가 없는 T-Box — 옛 판정이 100% 로 오보고했던 형태.
_TBOX_NO_LINKS = _TTL_PREFIX + """
steel:EquipmentMaster a owl:Class .
steel:AlarmEvents a owl:Class .
steel:EnergyUsage a owl:Class .
"""

#: OP 가 상위 클래스에 걸려 있고 CQ 는 자식 클래스를 지목하는 T-Box.
_TBOX_SUPERCLASS_LINK = _TTL_PREFIX + """
steel:ParentA a owl:Class .
steel:ParentB a owl:Class .
steel:ChildA a owl:Class ; rdfs:subClassOf steel:ParentA .
steel:ChildB a owl:Class ; rdfs:subClassOf steel:ParentB .

steel:parentLink a owl:ObjectProperty ;
    rdfs:domain steel:ParentA ;
    rdfs:range steel:ParentB .
"""

_SAMPLE_CQS = [
    {
        "id": "CQ01",
        "question_ko": "어떤 설비가 가장 많은 알람을 발생시키는가?",
        "question_en": "Which equipment has the most alarms?",
        "domains": ["EquipmentMaster", "AlarmEvents"],
    },
    {
        "id": "CQ02",
        "question_ko": "설비별 월간 에너지 사용량은?",
        "question_en": "Monthly energy usage per equipment?",
        "domains": ["EquipmentMaster", "EnergyUsage"],
    },
    {
        "id": "CQ03",
        "question_ko": "고로 설비의 정비 이력은?",
        "question_en": "Maintenance history of blast furnace equipment?",
        "domains": ["EquipmentMaster"],
    },
]


# ── _check_tbox_phase ────────────────────────────────────────────────


class TestCheckTboxPhase:
    """_check_tbox_phase: CQ 도메인 클래스가 T-Box에 존재하는지 확인."""

    def test_all_classes_covered(self):
        tbox_classes = {"equipmentmaster", "alarmevents", "energyusage"}
        results = _check_tbox_phase(_SAMPLE_CQS, tbox_classes)
        assert len(results) == 3
        for r in results:
            assert r["status"] == "covered"
            assert r["missing_classes"] == []
            assert r["coverage"] == 1.0

    def test_partial_coverage(self):
        tbox_classes = {"equipmentmaster"}  # AlarmEvents, EnergyUsage 누락
        results = _check_tbox_phase(_SAMPLE_CQS, tbox_classes)
        cq01 = next(r for r in results if r["id"] == "CQ01")
        assert cq01["status"] == "gap"
        assert "AlarmEvents" in cq01["missing_classes"]
        assert cq01["coverage"] == 0.5  # 1/2

    def test_no_classes_in_tbox(self):
        results = _check_tbox_phase(_SAMPLE_CQS, set())
        for r in results:
            assert r["status"] == "gap"
            assert r["coverage"] == 0.0

    def test_single_domain_cq_fully_covered(self):
        """단일 도메인 CQ: 해당 클래스가 있으면 covered."""
        tbox_classes = {"equipmentmaster"}
        results = _check_tbox_phase(_SAMPLE_CQS, tbox_classes)
        cq03 = next(r for r in results if r["id"] == "CQ03")
        assert cq03["status"] == "covered"
        assert cq03["coverage"] == 1.0

    def test_empty_cqs(self):
        results = _check_tbox_phase([], {"equipmentmaster"})
        assert results == []

    def test_case_insensitive_matching(self):
        """T-Box 클래스는 lowercase로 저장되어 있으므로 CQ domains도 lowercase 변환."""
        tbox_classes = {"equipmentmaster", "alarmevents"}
        cqs = [{"id": "CQ01", "domains": ["EquipmentMaster", "AlarmEvents"]}]
        results = _check_tbox_phase(cqs, tbox_classes)
        assert results[0]["status"] == "covered"

    def test_underscore_removal(self):
        """CQ domains에 언더스코어가 있어도 제거 후 매칭."""
        tbox_classes = {"equipmentmaster"}
        cqs = [{"id": "CQ01", "domains": ["Equipment_Master"]}]
        results = _check_tbox_phase(cqs, tbox_classes)
        assert results[0]["status"] == "covered"


# ── _check_abox_phase ────────────────────────────────────────────────


class TestCheckAboxPhase:
    """_check_abox_phase: CQ 도메인 클래스가 인스턴스를 가지는지 확인."""

    def test_all_classes_have_instances(self):
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 50},
                "AlarmEvents": {"instance_count": 120},
                "EnergyUsage": {"instance_count": 80},
            }
        }
        results = _check_abox_phase(_SAMPLE_CQS, abox_stats)
        assert len(results) == 3
        for r in results:
            assert r["status"] == "covered"
            assert r["zero_instance_classes"] == []

    def test_some_classes_zero_instances(self):
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 50},
                "AlarmEvents": {"instance_count": 0},
                "EnergyUsage": {"instance_count": 80},
            }
        }
        results = _check_abox_phase(_SAMPLE_CQS, abox_stats)
        cq01 = next(r for r in results if r["id"] == "CQ01")
        assert cq01["status"] == "gap"
        assert "AlarmEvents" in cq01["zero_instance_classes"]

    def test_class_missing_from_stats(self):
        """per_class에 아예 없는 클래스는 zero_instance로 간주."""
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 50},
            }
        }
        results = _check_abox_phase(_SAMPLE_CQS, abox_stats)
        cq01 = next(r for r in results if r["id"] == "CQ01")
        assert cq01["status"] == "gap"
        assert "AlarmEvents" in cq01["zero_instance_classes"]

    def test_empty_per_class(self):
        abox_stats = {"per_class": {}}
        results = _check_abox_phase(_SAMPLE_CQS, abox_stats)
        for r in results:
            assert r["status"] == "gap"

    def test_single_domain_with_instances(self):
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 10},
            }
        }
        results = _check_abox_phase(_SAMPLE_CQS, abox_stats)
        cq03 = next(r for r in results if r["id"] == "CQ03")
        assert cq03["status"] == "covered"

    def test_empty_cqs(self):
        results = _check_abox_phase([], {"per_class": {}})
        assert results == []

    def test_case_insensitive_class_lookup(self):
        """per_class 키는 원본 케이스 유지, CQ domains도 원본 비교."""
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 10},
            }
        }
        cqs = [{"id": "CQ01", "domains": ["EquipmentMaster"]}]
        results = _check_abox_phase(cqs, abox_stats)
        assert results[0]["status"] == "covered"


# ── _check_inferred_phase ────────────────────────────────────────────


class TestCheckInferredPhase:
    """_check_inferred_phase: CQ 도메인 쌍 간 OP 연결성 확인."""

    def test_all_pairs_connected(self):
        sem_dict = {
            "object_properties": {
                "hasAlarm": {"domain": "EquipmentMaster", "range": "AlarmEvents"},
                "hasEnergyUsage": {"domain": "EquipmentMaster", "range": "EnergyUsage"},
            }
        }
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 50},
                "AlarmEvents": {"instance_count": 120},
                "EnergyUsage": {"instance_count": 80},
            }
        }
        results = _check_inferred_phase(_SAMPLE_CQS, sem_dict, abox_stats)
        cq01 = next(r for r in results if r["id"] == "CQ01")
        assert cq01["status"] == "covered"
        assert cq01["connected_pairs"] == 1
        assert cq01["total_pairs"] == 1

    def test_single_domain_always_covered(self):
        """단일 도메인 CQ는 쌍이 없으므로 항상 covered."""
        sem_dict = {"object_properties": {}}
        abox_stats = {"per_class": {}}
        results = _check_inferred_phase(_SAMPLE_CQS, sem_dict, abox_stats)
        cq03 = next(r for r in results if r["id"] == "CQ03")
        assert cq03["status"] == "covered"
        assert cq03["connected_pairs"] == 0
        assert cq03["total_pairs"] == 0

    def test_missing_op_connection(self):
        """OP가 없으면 gap."""
        sem_dict = {"object_properties": {}}
        abox_stats = {"per_class": {}}
        results = _check_inferred_phase(_SAMPLE_CQS, sem_dict, abox_stats)
        cq01 = next(r for r in results if r["id"] == "CQ01")
        assert cq01["status"] == "gap"
        assert cq01["connected_pairs"] == 0
        assert cq01["total_pairs"] == 1

    def test_partial_connection_in_multi_domain(self):
        """3-domain CQ에서 일부만 연결."""
        cqs = [
            {
                "id": "CQ10",
                "domains": ["EquipmentMaster", "AlarmEvents", "EnergyUsage"],
            }
        ]
        sem_dict = {
            "object_properties": {
                "hasAlarm": {"domain": "EquipmentMaster", "range": "AlarmEvents"},
                # EnergyUsage와의 연결 없음
            }
        }
        abox_stats = {"per_class": {}}
        results = _check_inferred_phase(cqs, sem_dict, abox_stats)
        r = results[0]
        assert r["status"] == "gap"
        assert r["connected_pairs"] == 1  # Equipment-Alarm만
        assert r["total_pairs"] == 3  # C(3,2) = 3

    def test_empty_cqs(self):
        results = _check_inferred_phase([], {"object_properties": {}}, {"per_class": {}})
        assert results == []

    def test_reverse_direction_op_matches(self):
        """OP의 domain/range가 반대여도 연결 감지."""
        sem_dict = {
            "object_properties": {
                "belongsToEquipment": {"domain": "AlarmEvents", "range": "EquipmentMaster"},
            }
        }
        abox_stats = {"per_class": {}}
        cqs = [{"id": "CQ01", "domains": ["EquipmentMaster", "AlarmEvents"]}]
        results = _check_inferred_phase(cqs, sem_dict, abox_stats)
        assert results[0]["status"] == "covered"
        assert results[0]["connected_pairs"] == 1


# ── check_cq_coverage MCP tool ──────────────────────────────────────


class TestCheckCqCoverageTool:
    """check_cq_coverage: 파이프라인 단계별 CQ 커버리지 검증 MCP 도구."""

    def test_no_cq_file_returns_error(self):
        with patch("tools.competency_questions._CQ_PATH", "/nonexistent/cq.json"):
            result = json.loads(check_cq_coverage(phase="tbox"))
        assert result["success"] is False

    @patch("tools.competency_questions._load_tbox_entities")
    def test_tbox_phase_happy_path(self, mock_load_tbox, tmp_path):
        """클래스가 있고 **경로도 이어져 있으면** covered.

        경로 판정 도입(2026-08-14) 이후로는 T-Box 를 명시적으로 준다. 예전에는
        클래스 집합만 mock 해서 배포 T-Box 가 새어들어왔고, 픽스처의
        ``EnergyUsage`` 가 거기에 없어 결과가 환경에 의존했다.
        """
        mock_load_tbox.return_value = (
            {"equipmentmaster", "alarmevents", "energyusage"},
            {"hasequipment"},
        )
        tbox = tmp_path / "t_box.ttl"
        tbox.write_text(_TBOX_CONNECTED, encoding="utf-8")
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("tools.competency_questions.TBOX_PATH", str(tbox)):
            result = json.loads(check_cq_coverage(phase="tbox"))
        assert result["success"] is True
        assert result["summary"]["total"] == 3
        assert result["summary"]["covered"] == 3, (
            f"경로가 이어졌는데 gap 으로 판정됐다: "
            f"{[c for c in result['details'] if c['status'] == 'gap']}"
        )
        assert result["summary"]["gap"] == 0
        assert result["summary"]["coverage_rate"] == 100.0

    @patch("tools.competency_questions._load_tbox_entities")
    def test_classes_present_but_path_missing_is_gap(self, mock_load_tbox, tmp_path):
        """**핵심 회귀**: 클래스가 다 있어도 경로가 끊겼으면 gap 이다.

        2026-08-14 실측: 클래스만 보는 판정이 배포 T-Box 에서 12/12 = 100% 를
        보고했지만, 같은 CQ 의 클래스 쌍 102개 중 직접 경로가 있는 것은
        45개(44.1%)뿐이었다. 100% 라는 오보고가 ``ProductionPlan↔ProductionResult``
        같은 끊긴 조인을 가렸다.
        """
        mock_load_tbox.return_value = (
            {"equipmentmaster", "alarmevents", "energyusage"},
            set(),
        )
        tbox = tmp_path / "t_box.ttl"
        tbox.write_text(_TBOX_NO_LINKS, encoding="utf-8")
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("tools.competency_questions.TBOX_PATH", str(tbox)):
            result = json.loads(check_cq_coverage(phase="tbox"))

        assert result["success"] is True
        # CQ01/CQ02 는 쌍이 있고 경로가 없다 → gap. CQ03 은 도메인 1개라 쌍이 없다.
        by_id = {c["id"]: c for c in result["details"]}
        assert by_id["CQ01"]["status"] == "gap", (
            "경로가 없는데 covered 로 보고했다 — 클래스만 보는 옛 판정이다"
        )
        assert by_id["CQ01"]["missing_classes"] == [], (
            "클래스는 전부 존재해야 이 테스트가 경로 축을 검사한다"
        )
        assert by_id["CQ01"]["pairs_connected"] == 0
        assert "EquipmentMaster↔AlarmEvents" in by_id["CQ01"]["unconnected_pairs"]
        assert result["summary"]["coverage_rate"] < 100.0
        cp = result["summary"]["class_pairs"]
        assert cp["connected"] == 0 and cp["unconnected"] == cp["total"]

    @patch("tools.competency_questions._load_tbox_entities")
    def test_missing_tbox_reports_unavailable_not_zero(
        self, mock_load_tbox, tmp_path,
    ):
        """T-Box 를 못 읽으면 **판정 불가** 로 표시한다 (0% 로 내리지 않는다).

        판정불가와 "경로 0건" 을 같은 수치로 보고하면 운영자가 게이트 고장을
        데이터 문제로 오독한다.
        """
        mock_load_tbox.return_value = ({"equipmentmaster", "alarmevents", "energyusage"}, set())
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("tools.competency_questions.TBOX_PATH", str(tmp_path / "nope.ttl")):
            result = json.loads(check_cq_coverage(phase="tbox"))

        assert result["success"] is True
        assert result["summary"]["path_check"] == "unavailable"
        assert "과대평가" in result["summary"]["caveat"]
        for c in result["details"]:
            assert c["path_check"] in ("unavailable", "skipped_no_domains")
            assert c.get("path_coverage") is None or c["pairs_total"] == 0

    @patch("tools.competency_questions._load_tbox_entities")
    def test_path_via_superclass_op_counts(self, mock_load_tbox, tmp_path):
        """상위 클래스에 걸린 OP 로 이어지면 연결로 센다 (NEGATIVE 방향).

        조상을 무시하면 정당한 경로를 놓쳐 과소보고한다 — 예: domain 이
        ``ManufacturingProcessStep`` 인 OP 는 자식 ``ProcessRolling`` 도 쓴다.
        """
        mock_load_tbox.return_value = ({"childa", "childb"}, set())
        tbox = tmp_path / "t_box.ttl"
        tbox.write_text(_TBOX_SUPERCLASS_LINK, encoding="utf-8")
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps([
            {"id": "CQX", "domains": ["ChildA", "ChildB"]},
        ]), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("tools.competency_questions.TBOX_PATH", str(tbox)):
            result = json.loads(check_cq_coverage(phase="tbox"))

        c = result["details"][0]
        assert c["pairs_connected"] == 1, (
            f"상위 클래스 OP 경로를 놓쳤다 (과소보고): {c}"
        )
        assert c["status"] == "covered"

    @patch("tools.competency_questions._load_tbox_entities")
    def test_tbox_phase_with_gaps(self, mock_load_tbox, tmp_path):
        mock_load_tbox.return_value = ({"equipmentmaster"}, set())
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)):
            result = json.loads(check_cq_coverage(phase="tbox"))
        assert result["success"] is True
        assert result["summary"]["gap"] > 0
        assert len(result["gaps"]) > 0

    def test_abox_phase_happy_path(self, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 50},
                "AlarmEvents": {"instance_count": 120},
                "EnergyUsage": {"instance_count": 80},
            }
        }
        abox_stats_file = tmp_path / "abox_stats.json"
        abox_stats_file.write_text(json.dumps(abox_stats), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("config.GENERATED_ABOX_DIR", str(tmp_path)):
            result = json.loads(check_cq_coverage(phase="abox"))
        assert result["success"] is True
        assert result["summary"]["covered"] == 3

    def test_abox_phase_missing_stats_file(self, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("config.GENERATED_ABOX_DIR", str(tmp_path)):
            result = json.loads(check_cq_coverage(phase="abox"))
        assert result["success"] is False

    def test_inferred_phase_happy_path(self, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        sem_dict = {
            "object_properties": {
                "hasAlarm": {"domain": "EquipmentMaster", "range": "AlarmEvents"},
                "hasEnergyUsage": {"domain": "EquipmentMaster", "range": "EnergyUsage"},
            }
        }
        abox_stats = {
            "per_class": {
                "EquipmentMaster": {"instance_count": 50},
                "AlarmEvents": {"instance_count": 120},
                "EnergyUsage": {"instance_count": 80},
            }
        }
        sem_file = tmp_path / "semantic_dictionary.json"
        sem_file.write_text(json.dumps(sem_dict), encoding="utf-8")
        abox_stats_file = tmp_path / "abox_stats.json"
        abox_stats_file.write_text(json.dumps(abox_stats), encoding="utf-8")
        # ``TBOX_PATH`` 도 격리한다 — 2026-08-17 부터 inferred phase 는 경로 판정을
        # 위해 스키마 그래프를 읽는다. 배포 T-Box 를 읽으면 이 픽스처의 합성
        # 딕셔너리(EquipmentMaster→EnergyUsage 등)와 어긋나 테스트가 환경에
        # 의존하게 된다. 존재하지 않는 경로로 두면 딕셔너리 1홉 폴백을 타며,
        # 이 테스트가 원래 검증하던 계약(딕셔너리 기반 연결성)이 그대로 유지된다.
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("tools.competency_questions.SEMANTIC_DICT_PATH", str(sem_file)), \
             patch("tools.competency_questions.TBOX_PATH",
                   str(tmp_path / "no_tbox.ttl")), \
             patch("config.GENERATED_ABOX_DIR", str(tmp_path)):
            result = json.loads(check_cq_coverage(phase="inferred"))
        assert result["success"] is True
        assert result["summary"]["covered"] == 3  # CQ03 single-domain always covered
        assert result["details"][0]["path_check"] == "dictionary_only", (
            "T-Box 가 없으면 딕셔너리 1홉 폴백이어야 한다"
        )

    def test_inferred_phase_missing_sem_dict(self, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("tools.competency_questions.SEMANTIC_DICT_PATH", "/nonexistent/sd.json"):
            result = json.loads(check_cq_coverage(phase="inferred"))
        assert result["success"] is False

    def test_invalid_phase(self, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)):
            result = json.loads(check_cq_coverage(phase="invalid"))
        assert result["success"] is False

    def test_gaps_contain_cq_details(self, tmp_path):
        """gaps 리스트에 CQ id와 구체적 gap 정보가 포함."""
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)), \
             patch("tools.competency_questions._load_tbox_entities",
                   return_value=(set(), set())):
            result = json.loads(check_cq_coverage(phase="tbox"))
        assert result["success"] is True
        gaps = result["gaps"]
        assert len(gaps) > 0
        assert all("id" in g for g in gaps)


class TestInferredPhaseUsesGraphPaths:
    """inferred phase 가 **경로 기반** 으로 판정하는지 — 1홉 딕셔너리 매칭 회귀 가드.

    ## 배경 (2026-08-17 실측)

    inferred phase 는 딕셔너리의 ``object_properties`` 에서 **1홉 exact/prefix
    매칭** (``_find_connecting_op``) 만 봤다. 그래서 상위 클래스에 걸린 OP 와 중간
    클래스를 거치는 경로를 놓쳤다.

    결과가 **모순** 이었다 — 추론 그래프는 T-Box 의 상위집합인데:

      tbox     covered  7/12,  쌍 79/102 (77.5%)
      inferred covered  0/12,  쌍 45/102 (44.1%)   ← 더 나쁘게 보고

    추론이 71만 트리플을 늘렸는데 커버리지가 **떨어지는** 보고는 측정 결함이다.
    ``_check_tbox_phase`` 는 커밋 8c3c00a 에서 같은 이유로 경로 기반으로 교체됐고,
    이 단계는 그때 함께 고쳐지지 않았다.
    """

    def _graph_with_ancestor_op(self):
        """자식 클래스 사이에 직접 OP 가 없고 **부모** 에만 있는 최소 재현."""
        from rdflib import Graph

        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        g = Graph()
        g.parse(data=f"""
            @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            {NS_PREFIX}:ParentA a owl:Class .
            {NS_PREFIX}:ParentB a owl:Class .
            {NS_PREFIX}:ChildA a owl:Class ; rdfs:subClassOf {NS_PREFIX}:ParentA .
            {NS_PREFIX}:ChildB a owl:Class ; rdfs:subClassOf {NS_PREFIX}:ParentB .
            {NS_PREFIX}:linksParents a owl:ObjectProperty ;
                rdfs:domain {NS_PREFIX}:ParentA ; rdfs:range {NS_PREFIX}:ParentB .
        """, format="turtle")
        return g

    def test_ancestor_op_counts_as_connected(self):
        """THE REGRESSION: 부모에 걸린 OP 도 자식 쌍을 연결한다."""
        from tools.competency_questions import _check_inferred_phase

        cqs = [{"id": "CQ_X", "domains": ["ChildA", "ChildB"]}]
        empty_dict = {"object_properties": {}}   # 딕셔너리에는 단서가 없다

        # 1홉 딕셔너리만 보면 연결을 못 찾는다.
        no_graph = _check_inferred_phase(cqs, empty_dict, {}, graph=None)
        assert no_graph[0]["connected_pairs"] == 0, "이 테스트의 전제 확인"

        # 그래프를 주면 조상 경로로 찾아야 한다.
        with_graph = _check_inferred_phase(
            cqs, empty_dict, {}, graph=self._graph_with_ancestor_op())
        assert with_graph[0]["connected_pairs"] == 1, (
            "부모에 걸린 OP 를 못 찾았다 — 1홉 딕셔너리 매칭으로 회귀했다"
        )
        assert with_graph[0]["status"] == "covered"
        assert with_graph[0]["path_check"] == "graph"

    def test_unconnected_pair_still_reported_as_gap(self):
        """**정당한 판정 보존**: 진짜 끊긴 쌍은 계속 gap 이어야 한다.

        경로 판정을 넓히면서 무엇이든 연결로 세면 게이트가 무의미해진다.
        """
        from rdflib import Graph

        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        from tools.competency_questions import _check_inferred_phase

        g = Graph()
        g.parse(data=(
            f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            f"{NS_PREFIX}:Lonely1 a owl:Class .\n"
            f"{NS_PREFIX}:Lonely2 a owl:Class .\n"
        ), format="turtle")
        cqs = [{"id": "CQ_Y", "domains": ["Lonely1", "Lonely2"]}]
        r = _check_inferred_phase(cqs, {"object_properties": {}}, {}, graph=g)
        assert r[0]["connected_pairs"] == 0
        assert r[0]["status"] == "gap"

    def test_deployed_inferred_not_worse_than_tbox(self):
        """**산출물 기반**: inferred 커버리지가 tbox 보다 낮으면 측정 결함이다.

        추론은 스키마 축을 줄이지 않는다. 낮게 나오면 판정 방식이 다른 것이다.
        """
        import json as _json

        import config as _cfg
        from tools.competency_questions import check_cq_coverage

        if not (os.path.exists(_cfg.TBOX_PATH)
                and os.path.exists(_cfg.SEMANTIC_DICT_PATH)):
            pytest.skip("T-Box 또는 딕셔너리 없음")

        t = _json.loads(check_cq_coverage(phase="tbox"))
        i = _json.loads(check_cq_coverage(phase="inferred"))
        if not (t.get("success") and i.get("success")):
            pytest.skip("CQ 파일 없음")

        t_pairs = sum(d.get("pairs_connected", 0) for d in t.get("details", []))
        i_pairs = sum(d.get("connected_pairs", 0) for d in i.get("details", []))
        assert i_pairs >= t_pairs, (
            f"inferred 연결 쌍 {i_pairs} < tbox {t_pairs} — 추론이 스키마를 줄일 수 "
            "없으므로 이것은 측정 방식 불일치다 (1홉 딕셔너리 매칭 회귀)"
        )
