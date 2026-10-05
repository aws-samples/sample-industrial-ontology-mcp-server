"""R2 L2 Coverage Gate (2026-05-12) — improve_tbox_quality Step 12c 검증.

_check_csv_column_coverage 가 CSV non-PK/FK 컬럼 대비 class-specific DP 의
커버리지를 정확히 측정하고, env 로 임계치/모드를 제어할 수 있는지 검증.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.tbox_utils import _new_graph

STEEL_STR = "http://example.com/steel-ontology#"


def _cls(name: str) -> URIRef:
    return URIRef(STEEL_STR + name)


def _dp(name: str) -> URIRef:
    return URIRef(STEEL_STR + name)


def _write_csv(tmp_path, table: str, header: list[str], rows: list[list[str]]) -> str:
    """Write a CSV file into tmp_path/<table>.csv and return its absolute path."""
    import csv as _csv
    path = tmp_path / f"{table}.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
    return str(path)


def _graph_with_dps(class_name: str, dp_names: list[str]) -> Graph:
    """Minimal T-Box graph: declare class + given DPs with rdfs:domain=class."""
    g = _new_graph()
    g.add((_cls(class_name), RDF.type, OWL.Class))
    for name in dp_names:
        g.add((_dp(name), RDF.type, OWL.DatatypeProperty))
        g.add((_dp(name), RDFS.domain, _cls(class_name)))
    return g


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


def test_column_is_fk_detects_known_pattern():
    """_column_is_fk returns True for FK pattern known in rules/contracts/fk_patterns.json."""
    from tools.ontology_quality import _column_is_fk
    # equipment_id → EquipmentMaster (per fk_patterns.json entry)
    assert _column_is_fk("equipmentid") is True


def test_column_is_fk_rejects_pure_data_column():
    """_column_is_fk returns False for regular data column names."""
    from tools.ontology_quality import _column_is_fk
    assert _column_is_fk("hot_air_temp_c") is False
    assert _column_is_fk("tensile_strength") is False


def test_coverage_match_direct_and_class_prefixed():
    """_coverage_match finds direct, class-prefixed, suffix, contains variants."""
    from tools.ontology_quality import _coverage_match
    declared = {"equipmentstatusvalue", "equipmentmasterid", "status"}
    # 1. direct match
    assert _coverage_match("status", "equipmentstatus", declared) is True
    # 2. class-prefixed R1A rule: "value" under EquipmentStatus → equipmentStatusValue
    assert _coverage_match("value", "equipmentstatus", declared) is True
    # 3. suffix match: "masterid" ends like "equipmentmasterid"
    assert _coverage_match("masterid", "equipment", declared) is True
    # 4. no match
    assert _coverage_match("unrelated_field", "equipmentstatus", declared) is False


def test_coverage_match_handles_table_prefix_duplication():
    """CSV 컬럼 `equipment_name` + class `EquipmentMaster` → DP `equipmentMasterName`.

    CSV 컬럼이 테이블명 prefix 를 포함할 때 (equipment_*), Architect 는 중복을
    피하려고 DP 에서 테이블 prefix 를 드롭한다 (equipmentMasterName not
    equipmentMasterEquipmentName). matcher 는 이를 감지해야 한다.
    """
    from tools.ontology_quality import _coverage_match
    declared = {
        "equipmentmastername",
        "equipmentmasterequipmenttype",
        "supplierMastername".lower(),
        "maintenancehistorydate",
    }
    # equipment_name + EquipmentMaster → equipmentMasterName (name suffix only)
    assert _coverage_match("equipment_name", "equipmentmaster", declared) is True
    # supplier_name + SupplierMaster → supplierMasterName
    assert _coverage_match("supplier_name", "suppliermaster", declared) is True
    # maintenance_date + MaintenanceHistory (다른 prefix) → maintenanceHistoryDate
    assert _coverage_match("maintenance_date", "maintenancehistory", declared) is True


def test_coverage_match_handles_chemical_element_abbreviations():
    """CSV `C_Percent` + class `ChemicalAnalysis` → DP `chemicalAnalysisCarbonPercent`.

    Architect 가 화학 원소 축약 (C/Si/Mn/P/S/Cr/Ni) 을 풀네임으로 번역할 때도
    matcher 는 감지해야 한다.
    """
    from tools.ontology_quality import _coverage_match
    declared = {
        "chemicalanalysiscarbonpercent",
        "chemicalanalysissiliconpercent",
        "chemicalanalysismanganesepercent",
        "chemicalanalysisphosphoruspercent",
        "chemicalanalysissulfurpercent",
        "chemicalanalysischromiumpercent",
        "chemicalanalysisnickelpercent",
    }
    assert _coverage_match("c_percent", "chemicalanalysis", declared) is True
    assert _coverage_match("si_percent", "chemicalanalysis", declared) is True
    assert _coverage_match("mn_percent", "chemicalanalysis", declared) is True
    assert _coverage_match("p_percent", "chemicalanalysis", declared) is True
    assert _coverage_match("s_percent", "chemicalanalysis", declared) is True
    assert _coverage_match("cr_percent", "chemicalanalysis", declared) is True
    assert _coverage_match("ni_percent", "chemicalanalysis", declared) is True


def test_coverage_match_handles_abbreviation_in_middle():
    """CSV `surface_roughness_ra` → DP `surfaceQualityRoughnessRa` (suffix 유지)."""
    from tools.ontology_quality import _coverage_match
    declared = {"surfacequalityroughnessra"}
    assert _coverage_match("surface_roughness_ra", "surfacequality", declared) is True


def test_collect_expected_columns_excludes_pk_and_fk(tmp_path, monkeypatch):
    """PK + FK 컬럼은 expected 에서 제외, 나머지만 남는다."""
    from tools import ontology_quality as oq
    # EquipmentStatus: status_id (PK) + equipment_id (FK) + status + timestamp
    _write_csv(
        tmp_path, "EquipmentStatus",
        ["status_id", "equipment_id", "status", "timestamp"],
        [["S1", "EQ001", "RUN", "2026-01-01T00:00:00"],
         ["S2", "EQ002", "STOP", "2026-01-02T00:00:00"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    info = oq._collect_expected_columns_per_class()
    assert "EquipmentStatus" in info
    expected = info["EquipmentStatus"]["expected_columns"]
    # status_id = PK (uniqueness 기반), equipment_id = FK → 제외
    assert "status_id" not in expected
    assert "equipment_id" not in expected
    # 나머지 data 컬럼만 남아야 함
    assert "status" in expected
    assert "timestamp" in expected
    assert len(expected) == 2


# ---------------------------------------------------------------------------
# Coverage gate — end-to-end tests
# ---------------------------------------------------------------------------


def test_coverage_full_ratio_when_all_dps_declared(tmp_path, monkeypatch):
    """모든 data 컬럼이 class-specific DP 로 선언되어 있으면 ratio=1.0."""
    from tools import ontology_quality as oq
    _write_csv(
        tmp_path, "EquipmentStatus",
        ["status_id", "status", "timestamp"],
        [["S1", "RUN", "2026-01-01T00:00:00"],
         ["S2", "STOP", "2026-01-02T00:00:00"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    g = _graph_with_dps(
        "EquipmentStatus",
        ["equipmentStatusValue", "equipmentStatusTimestamp"],
    )
    stats = oq._check_csv_column_coverage(g, STEEL_STR)
    assert stats["coverage_total_expected"] == 2
    assert stats["coverage_total_matched"] == 2
    assert stats["coverage_ratio_overall"] == 1.0
    assert stats["coverage_classes_ok"] == 1
    assert stats["coverage_classes_below"] == 0


def test_coverage_detects_missing_columns(tmp_path, monkeypatch):
    """일부 컬럼의 DP 가 없으면 missing 에 올라오고 ratio < 1.0."""
    from tools import ontology_quality as oq
    # 5 data 컬럼 중 2개만 DP 선언
    _write_csv(
        tmp_path, "Process_Blast_Furnace",
        ["furnace_id", "hot_air_temp_c", "co_percent", "blow_rate",
         "oxygen_amount", "pressure"],
        [["F1", "1200", "30", "500", "200", "5.2"],
         ["F2", "1210", "31", "510", "210", "5.3"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    g = _graph_with_dps(
        "ProcessBlastFurnace",
        ["blastFurnaceHotAirTempC", "blastFurnaceCoPercent"],
    )
    stats = oq._check_csv_column_coverage(g, STEEL_STR)
    per = stats["coverage_per_class"][0]
    assert per["class"] == "ProcessBlastFurnace"
    assert per["expected"] == 5
    assert per["matched"] == 2
    assert per["ratio"] == 0.4
    assert "blow_rate" in per["missing"]
    assert "oxygen_amount" in per["missing"]
    assert "pressure" in per["missing"]
    assert per["below_threshold"] is True
    assert stats["coverage_classes_below"] == 1


def test_coverage_default_mode_is_warn_and_env_override(tmp_path, monkeypatch):
    """TBOX_COVERAGE_GATE default='warn', env 로 override 가능."""
    from tools import ontology_quality as oq
    _write_csv(
        tmp_path, "Sample",
        ["sample_id", "value"],
        [["S1", "10"], ["S2", "20"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    g = _graph_with_dps("Sample", ["sampleValue"])
    monkeypatch.delenv("TBOX_COVERAGE_GATE", raising=False)
    stats = oq._check_csv_column_coverage(g, STEEL_STR)
    assert stats["coverage_mode"] == "warn"

    monkeypatch.setenv("TBOX_COVERAGE_GATE", "fail")
    stats2 = oq._check_csv_column_coverage(g, STEEL_STR)
    assert stats2["coverage_mode"] == "fail"


def test_coverage_threshold_env_var(tmp_path, monkeypatch):
    """TBOX_COVERAGE_THRESHOLD 로 임계치 조정. 0.5 로 내리면 below_threshold 해소."""
    from tools import ontology_quality as oq
    _write_csv(
        tmp_path, "Sample",
        ["sample_id", "a", "b", "c", "d"],
        [["S1", "1", "2", "3", "4"], ["S2", "5", "6", "7", "8"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    # 4개 중 3개만 선언 → 75% 커버리지
    g = _graph_with_dps("Sample", ["sampleA", "sampleB", "sampleC"])

    # default 0.85 → below_threshold
    monkeypatch.delenv("TBOX_COVERAGE_THRESHOLD", raising=False)
    stats_default = oq._check_csv_column_coverage(g, STEEL_STR)
    assert stats_default["coverage_threshold"] == 0.85
    assert stats_default["coverage_classes_below"] == 1

    # 0.5 로 낮추면 OK
    monkeypatch.setenv("TBOX_COVERAGE_THRESHOLD", "0.5")
    stats_low = oq._check_csv_column_coverage(g, STEEL_STR)
    assert stats_low["coverage_threshold"] == 0.5
    assert stats_low["coverage_classes_below"] == 0
    assert stats_low["coverage_classes_ok"] == 1


def test_coverage_fail_mode_raises_when_below_threshold(tmp_path, monkeypatch):
    """TBOX_COVERAGE_GATE=fail + 임계치 미달 → RuntimeError 발생."""
    from tools import ontology_quality as oq
    _write_csv(
        tmp_path, "Sample",
        ["sample_id", "a", "b", "c", "d"],
        [["S1", "1", "2", "3", "4"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    # 4개 중 1개만 선언 → 25%
    g = _graph_with_dps("Sample", ["sampleA"])
    monkeypatch.setenv("TBOX_COVERAGE_GATE", "fail")
    with pytest.raises(RuntimeError, match="Coverage gate failed"):
        oq._check_csv_column_coverage(g, STEEL_STR)


def test_coverage_fail_mode_passes_when_all_above_threshold(tmp_path, monkeypatch):
    """fail 모드라도 모든 클래스가 임계치 이상이면 raise 하지 않음."""
    from tools import ontology_quality as oq
    _write_csv(
        tmp_path, "Sample",
        ["sample_id", "value"],
        [["S1", "1"], ["S2", "2"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    g = _graph_with_dps("Sample", ["sampleValue"])
    monkeypatch.setenv("TBOX_COVERAGE_GATE", "fail")
    stats = oq._check_csv_column_coverage(g, STEEL_STR)
    assert stats["coverage_ratio_overall"] == 1.0
    assert stats["coverage_classes_below"] == 0


def test_coverage_stats_shape_and_per_class_detail(tmp_path, monkeypatch):
    """stats 구조: 전체 지표 + per_class 세부 + missing 상위 20개."""
    from tools import ontology_quality as oq
    _write_csv(
        tmp_path, "AlarmEvents",
        ["event_id", "severity", "timestamp", "description"],
        [["E1", "HIGH", "2026-01-01T00:00:00", "foo"],
         ["E2", "LOW", "2026-01-02T00:00:00", "bar"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    g = _graph_with_dps("AlarmEvents", ["alarmEventsSeverity"])
    stats = oq._check_csv_column_coverage(g, STEEL_STR)

    required_keys = {
        "coverage_mode", "coverage_threshold",
        "coverage_total_expected", "coverage_total_matched",
        "coverage_ratio_overall", "coverage_classes_ok",
        "coverage_classes_below", "coverage_per_class",
    }
    assert required_keys.issubset(stats.keys())

    per = stats["coverage_per_class"][0]
    assert per["class"] == "AlarmEvents"
    assert per["table"] == "AlarmEvents"
    assert per["expected"] == 3  # severity, timestamp, description
    assert per["matched"] == 1
    assert "timestamp" in per["missing"]
    assert "description" in per["missing"]
    assert per["below_threshold"] is True


def test_coverage_multiple_classes_independent_ratios(tmp_path, monkeypatch):
    """여러 CSV 테이블은 각각 독립적으로 coverage 계산."""
    from tools import ontology_quality as oq
    # Class A: 100% 커버 (1/1)
    _write_csv(
        tmp_path, "TableA",
        ["a_id", "value"],
        [["A1", "1"], ["A2", "2"]],
    )
    # Class B: 50% 커버 (1/2)
    _write_csv(
        tmp_path, "TableB",
        ["b_id", "foo", "bar"],
        [["B1", "x", "y"], ["B2", "m", "n"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    g = _new_graph()
    # TableA: 모두 선언
    g.add((_cls("TableA"), RDF.type, OWL.Class))
    g.add((_dp("tableAValue"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("tableAValue"), RDFS.domain, _cls("TableA")))
    # TableB: foo 만 선언
    g.add((_cls("TableB"), RDF.type, OWL.Class))
    g.add((_dp("tableBFoo"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("tableBFoo"), RDFS.domain, _cls("TableB")))

    stats = oq._check_csv_column_coverage(g, STEEL_STR)
    per_by_class = {p["class"]: p for p in stats["coverage_per_class"]}
    assert per_by_class["TableA"]["ratio"] == 1.0
    assert per_by_class["TableA"]["below_threshold"] is False
    assert per_by_class["TableB"]["ratio"] == 0.5
    assert per_by_class["TableB"]["below_threshold"] is True
    assert stats["coverage_classes_ok"] == 1
    assert stats["coverage_classes_below"] == 1
    # 전체 비율: 2 matched / 3 expected = 0.6667
    assert stats["coverage_total_expected"] == 3
    assert stats["coverage_total_matched"] == 2


# ---------------------------------------------------------------------------
# Step 12d — auto-injection of missing class-specific DPs (R2 fix, 2026-05-30)
# ---------------------------------------------------------------------------


def test_step_12d_injects_missing_dps(tmp_path, monkeypatch):
    """Missing CSV columns become class-specific DPs with rdfs:domain set."""
    from tools import ontology_quality as oq
    from tools.quality_steps import step_12d_csv_dp_inject as step12d
    from tools.quality_steps._base import StepContext

    _write_csv(
        tmp_path, "ProductMaster",
        ["Product_ID", "Steel_Grade", "Specification", "Target_Tensile_MPa"],
        [["P1", "SS400", "JIS", "400"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))

    g = _new_graph()
    g.add((_cls("ProductMaster"), RDF.type, OWL.Class))

    ctx = StepContext(domain_ns=STEEL_STR)
    result = step12d.apply(g, ctx)

    assert result.stats["injected_dps"] == 3  # PK skipped
    assert result.stats["classes_touched"] == 1
    assert result.stats["by_class"]["ProductMaster"] == 3

    # Each injected DP must carry rdfs:domain ProductMaster
    expected_dps = {"productMasterSteelGrade", "productMasterSpecification",
                    "productMasterTargetTensileMPa"}
    for dp_name in expected_dps:
        dp_uri = _dp(dp_name)
        assert (dp_uri, RDF.type, OWL.DatatypeProperty) in g, f"missing {dp_name}"
        assert (dp_uri, RDFS.domain, _cls("ProductMaster")) in g, f"no domain on {dp_name}"


def test_step_12d_skips_already_declared_columns(tmp_path, monkeypatch):
    """When the column is already covered by a class-specific DP, nothing is added."""
    from tools import ontology_quality as oq
    from tools.quality_steps import step_12d_csv_dp_inject as step12d
    from tools.quality_steps._base import StepContext

    _write_csv(
        tmp_path, "EquipmentMaster",
        ["equipment_id", "equipment_name"],
        [["EQ1", "Furnace A"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))

    g = _new_graph()
    g.add((_cls("EquipmentMaster"), RDF.type, OWL.Class))
    g.add((_dp("equipmentMasterName"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("equipmentMasterName"), RDFS.domain, _cls("EquipmentMaster")))

    ctx = StepContext(domain_ns=STEEL_STR)
    result = step12d.apply(g, ctx)

    assert result.stats["injected_dps"] == 0


def test_step_12d_disabled_via_env(tmp_path, monkeypatch):
    """TBOX_AUTO_INJECT_MISSING_DPS=false short-circuits the step."""
    from tools import ontology_quality as oq
    from tools.quality_steps import step_12d_csv_dp_inject as step12d
    from tools.quality_steps._base import StepContext

    _write_csv(
        tmp_path, "Foo",
        ["foo_id", "value"],
        [["F1", "1"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))
    monkeypatch.setenv("TBOX_AUTO_INJECT_MISSING_DPS", "false")

    g = _new_graph()
    g.add((_cls("Foo"), RDF.type, OWL.Class))

    ctx = StepContext(domain_ns=STEEL_STR)
    result = step12d.apply(g, ctx)

    assert result.stats["injected_dps"] == 0
    assert result.stats.get("skipped") == "disabled_via_env"


def test_step_12d_skips_classes_not_in_tbox(tmp_path, monkeypatch):
    """CSV table without a matching T-Box class is silently ignored."""
    from tools import ontology_quality as oq
    from tools.quality_steps import step_12d_csv_dp_inject as step12d
    from tools.quality_steps._base import StepContext

    _write_csv(
        tmp_path, "OrphanTable",
        ["orphan_id", "value"],
        [["O1", "1"]],
    )
    monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(tmp_path))

    g = _new_graph()  # no class declared

    ctx = StepContext(domain_ns=STEEL_STR)
    result = step12d.apply(g, ctx)

    assert result.stats["injected_dps"] == 0
    assert result.stats["classes_touched"] == 0


def test_step_12d_range_inference_by_suffix():
    """Numeric / date suffixes drive xsd range selection."""
    from rdflib.namespace import XSD

    from tools.quality_steps.step_12d_csv_dp_inject import _infer_range

    assert _infer_range("casting_speed_mmin") == XSD.decimal
    assert _infer_range("efficiency_percent") == XSD.decimal
    assert _infer_range("downtime_hours") == XSD.integer
    assert _infer_range("test_datetime") == XSD.dateTime
    assert _infer_range("steel_grade") == XSD.string
