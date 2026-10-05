"""Golden regression tests (task code R20; see docs/reference/task-glossary.md).

R19 후 남은 3개 follow-up 처리 회귀 테스트.

- R20-1 `_process_single_csv` 헬퍼 존재 및 호출 가능
- R20-2 class-prefix alias 확장 (_candidate_dps for_class param)
- R20-3 status/severity 추가 enum 값 정규화
"""
from __future__ import annotations

from rdflib import RDF, Literal

from domain.namespaces import DOMAIN_INST_NS_OBJ, DOMAIN_NS_OBJ
from domain.tbox_utils import _new_graph

# ── R20-1: _process_single_csv 추출 ────────────────


class TestProcessSingleCsvHelperExists:
    def test_symbol_importable(self):
        from tools.abox_generation import _process_single_csv
        assert callable(_process_single_csv)


# ── R20-2: class-prefix alias audit ────────────────


class TestClassPrefixAliasAudit:
    def test_audit_recognizes_class_prefix_dp(self):
        """T-Box 에 `noiseTimestamp` 만 선언되고 hasTimestamp 는 없는 상황에
        인스턴스가 noiseTimestamp 로 값을 가지면 required_props(hasTimestamp)
        체크가 만족으로 간주되어야 한다 (class-prefix alias 확장).
        """
        from tools.abox_generation import _check_required_props_coverage
        g = _new_graph()
        cls_uri = DOMAIN_NS_OBJ["NoiseVibrationMonitoring"]
        inst = DOMAIN_INST_NS_OBJ["NoiseVibrationMonitoring_X1"]
        g.add((inst, RDF.type, cls_uri))
        g.add((inst, DOMAIN_NS_OBJ["noiseTimestamp"],
               Literal("2025-09-01T00:00:00")))

        tbox_info = {
            "datatype_properties": {"noisetimestamp": "noiseTimestamp"},
            "dp_domains": {},
            "dp_ranges": {},
            "classes": {"noisevibrationmonitoring": "NoiseVibrationMonitoring"},
            "object_properties": {},
            "class_props": {},
            "subclass_of": {},
        }
        audit = _check_required_props_coverage(
            g, {"NoiseVibrationMonitoring": 1}, tbox_info=tbox_info,
        )
        # hasTimestamp 는 *Monitoring wildcard 로 체크되는데 noiseTimestamp
        # 가 인스턴스에 있으므로 violation 이어선 안 됨.
        ts_violations = [v for v in audit["violations"]
                         if v["dp"] == "hasTimestamp"
                         and v["class"] == "NoiseVibrationMonitoring"]
        assert ts_violations == [], f"class-prefix alias 확장 실패: {ts_violations}"


# ── R20-3: enum synonyms 확장 ──────────────────────


class TestEnumSynonymsExtended:
    def test_running_canonicalized(self):
        from tools.abox_generation import _normalize_value
        # CSV 값 "Running" → canonical "Running" (변화 없음, 등록됨)
        assert _normalize_value("Running", "status") == "Running"

    def test_running_lowercase_canonicalized(self):
        from tools.abox_generation import _normalize_value
        assert _normalize_value("running", "status") == "Running"

    def test_maintenance_tracked(self):
        from tools.abox_generation import _normalize_value
        assert _normalize_value("MAINTENANCE", "status") == "Maintenance"

    def test_severity_high_low(self):
        from tools.abox_generation import _normalize_value
        assert _normalize_value("High", "severity") == "High"
        assert _normalize_value("Low", "severity") == "Low"
        assert _normalize_value("Medium", "severity") == "Medium"

    def test_in_progress_synonym(self):
        from tools.abox_generation import _normalize_value
        assert _normalize_value("In_Progress", "status") == "InProgress"
        assert _normalize_value("IN_PROGRESS", "status") == "InProgress"


# ── R20 통합: unknown_enum_values 비어있음 보장 ────


class TestNoUnknownEnumAfterR20:
    def test_common_status_values_not_flagged(self):
        """Running/Maintenance/Standby 같은 값을 정규화했을 때
        unknown_enum 에 등록되지 않아야 한다."""
        from tools.abox_generation import (
            _normalize_value,
            _reset_unknown_enum_values,
            _snapshot_unknown_enum_values,
        )
        _reset_unknown_enum_values()
        for val in ("Running", "Maintenance", "Standby", "Stopped",
                     "Medium", "High", "Low"):
            _normalize_value(val, "status" if val in ("Running", "Maintenance", "Standby", "Stopped")
                             else "severity")
        snap = _snapshot_unknown_enum_values()
        assert snap == {}, f"R20-3 enum 확장 후 unknown_enum 잔존: {snap}"
