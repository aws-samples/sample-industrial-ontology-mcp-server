"""tacit_rules engine tests — augment_csv_fk + generate_tacit_from_rules (작업코드 R26; docs/reference/task-glossary.md).

Uses tmp_path + monkeypatch to isolate SOURCE_RAWDATA_DIR/SOURCE_TACIT_DIR
so the suite never mutates the actual project data.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _setup_dirs(tmp_path: Path, monkeypatch):
    rawdata = tmp_path / "rawdata"
    tacit = tmp_path / "tacit"
    rawdata.mkdir()
    tacit.mkdir()
    import config
    import tools.tacit_rules as tr
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(tr, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tr, "SOURCE_TACIT_DIR", str(tacit))
    return rawdata, tacit


# ── augment_csv_fk ──────────────────────────────────────


def test_augment_csv_fk_adds_rotation_fk(tmp_path, monkeypatch):
    rawdata, _ = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(rawdata / "Source.csv", ["id"], [{"id": f"S{i}"} for i in range(5)])
    _write_csv(rawdata / "Target.csv", ["code"], [{"code": "T1"}, {"code": "T2"}])

    from tools.tacit_rules import augment_csv_fk
    result = json.loads(augment_csv_fk("Source.csv", "target_fk", "Target.csv", "code"))
    assert result["success"] is True
    assert result["action"] == "added"
    assert result["rows_updated"] == 5

    # CSV 확인: rotation T1,T2,T1,T2,T1
    with open(rawdata / "Source.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["target_fk"] for r in rows] == ["T1", "T2", "T1", "T2", "T1"]


def test_augment_csv_fk_idempotent(tmp_path, monkeypatch):
    rawdata, _ = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(rawdata / "Source.csv", ["id", "target_fk"], [{"id": "S1", "target_fk": "existing"}])
    _write_csv(rawdata / "Target.csv", ["code"], [{"code": "T1"}])

    from tools.tacit_rules import augment_csv_fk
    result = json.loads(augment_csv_fk("Source.csv", "target_fk", "Target.csv", "code"))
    assert result["action"] == "skipped"
    with open(rawdata / "Source.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["target_fk"] == "existing"  # preserved


def test_augment_csv_fk_with_filter(tmp_path, monkeypatch):
    rawdata, _ = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(rawdata / "Source.csv", ["id"], [{"id": f"S{i}"} for i in range(4)])
    _write_csv(
        rawdata / "Target.csv",
        ["code", "type"],
        [
            {"code": "T1", "type": "A"},
            {"code": "T2", "type": "B"},
            {"code": "T3", "type": "A"},
        ],
    )

    from tools.tacit_rules import augment_csv_fk
    json.loads(augment_csv_fk(
        "Source.csv", "target_fk", "Target.csv", "code",
        filter_column="type", filter_value="A",
    ))

    with open(rawdata / "Source.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # A 타입만: T1, T3 rotation
    assert [r["target_fk"] for r in rows] == ["T1", "T3", "T1", "T3"]


def test_augment_csv_fk_rejects_path_traversal(tmp_path, monkeypatch):
    rawdata, _ = _setup_dirs(tmp_path, monkeypatch)
    outside = tmp_path / "Outside.csv"
    _write_csv(outside, ["id"], [{"id": "unchanged"}])
    _write_csv(rawdata / "Target.csv", ["code"], [{"code": "T1"}])

    from tools.tacit_rules import augment_csv_fk
    result = json.loads(
        augment_csv_fk("../Outside.csv", "target_fk", "Target.csv", "code")
    )

    assert result["success"] is False
    with open(outside, encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == [{"id": "unchanged"}]


# ── generate_tacit_from_rules: strategy 별 ──────────────


def _write_rules(tmp_path: Path, mappings: list[dict]) -> Path:
    p = tmp_path / "tacit_rules.json"
    p.write_text(json.dumps({"mappings": mappings}), encoding="utf-8")
    return p


def _read_ttl(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_strategy_number_match(tmp_path, monkeypatch):
    rawdata, tacit = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(
        rawdata / "Monitoring_Point_Master.csv",
        ["Point_ID"],
        [{"Point_ID": f"MP{i:03d}"} for i in (1, 2, 3)],
    )
    rules_path = _write_rules(tmp_path, [{
        "name": "pe",
        "strategy": "number_match",
        "source_csv": "Monitoring_Point_Master.csv",
        "source_class": "MonitoringPointMaster",
        "source_pk_column": "Point_ID",
        "target_class": "EquipmentMaster",
        "target_pk_template": "EQ{n:03d}",
        "ops": ["pointLocatesEquipment", "pointMonitorsEquipment"],
        "output_file": "pe.ttl",
    }])

    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))
    assert result["success"] is True
    assert result["per_rule"][0]["triples"] == 6  # 3 MP × 2 ops

    ttl = _read_ttl(tacit / "pe.ttl")
    assert "MonitoringPointMaster_MP001" in ttl
    assert "EquipmentMaster_EQ001" in ttl
    assert "pointLocatesEquipment" in ttl
    assert "pointMonitorsEquipment" in ttl


def test_strategy_rotation_with_filter(tmp_path, monkeypatch):
    rawdata, tacit = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(
        rawdata / "Steam_Energy.csv",
        ["Steam_ID"],
        [{"Steam_ID": f"ST{i:04d}"} for i in (1, 2, 3, 4)],
    )
    _write_csv(
        rawdata / "Energy_Source_Master.csv",
        ["Energy_Source_ID", "Source_Type"],
        [
            {"Energy_Source_ID": "ES001", "Source_Type": "Electricity"},
            {"Energy_Source_ID": "ES009", "Source_Type": "Steam"},
            {"Energy_Source_ID": "ES019", "Source_Type": "Steam"},
        ],
    )
    rules_path = _write_rules(tmp_path, [{
        "name": "steam",
        "strategy": "rotation",
        "source_csv": "Steam_Energy.csv",
        "source_class": "SteamEnergy",
        "source_pk_column": "Steam_ID",
        "target_csv": "Energy_Source_Master.csv",
        "target_class": "EnergySourceMaster",
        "target_pk_column": "Energy_Source_ID",
        "target_filter_column": "Source_Type",
        "target_filter_value": "Steam",
        "op": "hasSteamEnergySource",
        "output_file": "steam.ttl",
    }])

    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))
    assert result["per_rule"][0]["triples"] == 4

    ttl = _read_ttl(tacit / "steam.ttl")
    # 4 steam → 2 sources rotation: ES009, ES019, ES009, ES019
    assert ttl.count("EnergySourceMaster_ES009") == 2
    assert ttl.count("EnergySourceMaster_ES019") == 2
    assert "EnergySourceMaster_ES001" not in ttl  # filter 제외


def test_strategy_simple_join(tmp_path, monkeypatch):
    rawdata, tacit = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(
        rawdata / "Transportation.csv",
        ["Transport_ID", "Destination_Warehouse"],
        [
            {"Transport_ID": "TRP001", "Destination_Warehouse": "WH001"},
            {"Transport_ID": "TRP002", "Destination_Warehouse": "WH002"},
        ],
    )
    rules_path = _write_rules(tmp_path, [{
        "name": "td",
        "strategy": "simple_join",
        "source_csv": "Transportation.csv",
        "source_class": "Transportation",
        "source_pk_column": "Transport_ID",
        "source_fk_column": "Destination_Warehouse",
        "target_class": "WarehouseMaster",
        "op": "destinationWarehouse",
        "output_file": "td.ttl",
    }])

    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))
    assert result["per_rule"][0]["triples"] == 2

    ttl = _read_ttl(tacit / "td.ttl")
    assert "Transportation_TRP001" in ttl
    assert "WarehouseMaster_WH001" in ttl
    assert "destinationWarehouse" in ttl


def test_strategy_via_mapping_chain(tmp_path, monkeypatch):
    rawdata, tacit = _setup_dirs(tmp_path, monkeypatch)
    # 1. upstream: MP → EQ (number_match)
    _write_csv(
        rawdata / "MP.csv",
        ["Point_ID"],
        [{"Point_ID": "MP001"}, {"Point_ID": "MP002"}],
    )
    # 2. intermediate FK: Maintenance_History has EQ → Maintenance_ID
    _write_csv(
        rawdata / "MH.csv",
        ["Maintenance_ID", "Equipment_ID"],
        [
            {"Maintenance_ID": "MNT01", "Equipment_ID": "EQ001"},
            {"Maintenance_ID": "MNT02", "Equipment_ID": "EQ001"},  # EQ001 두 번째는 무시됨 (first wins)
            {"Maintenance_ID": "MNT03", "Equipment_ID": "EQ002"},
        ],
    )
    # 3. source: WQM with Point_ID
    _write_csv(
        rawdata / "WQM.csv",
        ["Sample_ID", "Point_ID"],
        [
            {"Sample_ID": "WS001", "Point_ID": "MP001"},
            {"Sample_ID": "WS002", "Point_ID": "MP002"},
        ],
    )

    rules_path = _write_rules(tmp_path, [
        {
            "name": "pe_chain",
            "strategy": "number_match",
            "source_csv": "MP.csv",
            "source_class": "MP",
            "source_pk_column": "Point_ID",
            "target_class": "EQ",
            "target_pk_template": "EQ{n:03d}",
            "ops": ["pointLocatesEquipment"],
            "output_file": "chain_pe.ttl",
        },
        {
            "name": "wqm_mh",
            "strategy": "via_mapping_chain",
            "source_csv": "WQM.csv",
            "source_class": "WQM",
            "source_pk_column": "Sample_ID",
            "source_link_column": "Point_ID",
            "via_mapping": "pe_chain",
            "intermediate_fk_csv": "MH.csv",
            "intermediate_fk_source_column": "Equipment_ID",
            "intermediate_fk_target_column": "Maintenance_ID",
            "target_class": "MH",
            "op": "hasMaintenanceHistory",
            "output_file": "chain_wqm.ttl",
        },
    ])

    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))
    rule_results = {r["name"]: r for r in result["per_rule"]}
    assert rule_results["pe_chain"]["triples"] == 2
    assert rule_results["wqm_mh"]["triples"] == 2

    ttl = _read_ttl(tacit / "chain_wqm.ttl")
    assert "WQM_WS001" in ttl
    assert "MH_MNT01" in ttl  # first MNT for EQ001
    assert "MH_MNT03" in ttl
    assert "hasMaintenanceHistory" in ttl


def test_strategy_fk_lookup_table_with_composite_pk(tmp_path, monkeypatch):
    """fk_lookup_table: source CSV 의 join column → lookup CSV 에서 target PK 조회.

    CQ10 실사례 모사: InventoryStatus(composite PK = Item_Code+Warehouse_Code)
    → Item_Supplier_Map.Item_Code → Supplier_ID.
    """
    rawdata, tacit = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(
        rawdata / "Inventory_Status.csv",
        ["Item_Code", "Warehouse_Code", "Stock"],
        [
            {"Item_Code": "ITM001", "Warehouse_Code": "WH001", "Stock": "100"},
            {"Item_Code": "ITM002", "Warehouse_Code": "WH002", "Stock": "200"},
            {"Item_Code": "ITM003", "Warehouse_Code": "WH001", "Stock": "300"},  # no mapping
        ],
    )
    _write_csv(
        rawdata / "Item_Supplier_Map.csv",
        ["Item_Code", "Supplier_ID"],
        [
            {"Item_Code": "ITM001", "Supplier_ID": "SUP008"},
            {"Item_Code": "ITM001", "Supplier_ID": "SUP002"},  # duplicate — first wins
            {"Item_Code": "ITM002", "Supplier_ID": "SUP010"},
        ],
    )
    rules_path = _write_rules(tmp_path, [{
        "name": "is_supplier",
        "strategy": "fk_lookup_table",
        "source_csv": "Inventory_Status.csv",
        "source_class": "InventoryStatus",
        "source_composite_pk": ["Item_Code", "Warehouse_Code"],
        "source_join_column": "Item_Code",
        "lookup_csv": "Item_Supplier_Map.csv",
        "lookup_key_column": "Item_Code",
        "lookup_value_column": "Supplier_ID",
        "target_class": "SupplierMaster",
        "op": "hasSupplier",
        "output_file": "is_sup.ttl",
    }])
    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))
    assert result["per_rule"][0]["triples"] == 2  # ITM003 skipped (no mapping)

    ttl = _read_ttl(tacit / "is_sup.ttl")
    assert "InventoryStatus_ITM001_WH001" in ttl  # composite PK preserved
    assert "InventoryStatus_ITM002_WH002" in ttl
    assert "SupplierMaster_SUP008" in ttl  # first-win applied
    assert "SupplierMaster_SUP002" not in ttl
    assert "ITM003" not in ttl  # no mapping → no triple


def test_unknown_strategy_is_recorded_not_crashed(tmp_path, monkeypatch):
    _, _ = _setup_dirs(tmp_path, monkeypatch)
    rules_path = _write_rules(tmp_path, [{
        "name": "bogus",
        "strategy": "typo_strategy",
        "output_file": "nope.ttl",
    }])
    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))
    assert result["success"] is True
    assert result["per_rule"][0]["error"].startswith("unknown strategy")
    assert result["per_rule"][0]["triples"] == 0


def test_missing_rules_file_returns_error(tmp_path, monkeypatch):
    _setup_dirs(tmp_path, monkeypatch)
    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(tmp_path / "nope.json")))
    assert result["success"] is False
    assert "not found" in result["error"].lower()


def test_suggest_tacit_rules_parses_llm_json_and_saves(tmp_path, monkeypatch):
    """R28-C: suggest_tacit_rules 가 LLM 응답을 파싱하고 suggested.json 저장."""
    rawdata, tacit = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(rawdata / "MP.csv", ["Point_ID"], [{"Point_ID": "MP001"}])
    _write_csv(rawdata / "EQ.csv", ["Equipment_ID"], [{"Equipment_ID": "EQ001"}])
    # T-Box 최소 (empty file 도 괜찮음 — 없으면 ops 가 비어짐)
    import config
    tbox_path = tmp_path / "t_box.ttl"
    tbox_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox_path))

    # CQ 최소
    qt_dir = tmp_path / "query_tests"
    qt_dir.mkdir()
    cq_path = qt_dir / "cq.json"
    cq_path.write_text(json.dumps({
        "questions": [
            {"id": "CQ01", "domains": ["MP", "EQ"], "question_ko": "MP와 EQ 매핑?"}
        ]
    }), encoding="utf-8")
    monkeypatch.setattr(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path))

    # Bedrock LLM 호출 mock
    fake_response = json.dumps({
        "mappings": [
            {
                "name": "point_equipment",
                "strategy": "number_match",
                "source_csv": "MP.csv",
                "source_class": "MP",
                "source_pk_column": "Point_ID",
                "target_class": "EQ",
                "target_pk_template": "EQ{n:03d}",
                "ops": ["mapsToEquipment"],
                "output_file": "point_equipment.ttl",
                "_confidence": "high",
                "_reasoning": "번호 매칭 1:1 패턴",
            }
        ]
    })

    def fake_invoke(prompt, **kwargs):
        return {"text": fake_response, "stop_reason": "end_turn", "usage": {}}

    monkeypatch.setattr("tools.bedrock.invoke_bedrock_with_metadata", fake_invoke)

    # suggested.json 경로를 tmp_path 로 유도 — __file__ 기반이므로 별도 패치 불필요
    # 대신 test 후 cleanup 하도록 save_to_suggested=False 로 실행
    from tools.tacit_rules import suggest_tacit_rules
    result = json.loads(suggest_tacit_rules(max_rules=5, save_to_suggested=False))

    assert result["success"] is True
    assert result["mappings_count"] == 1
    assert result["preview"][0]["name"] == "point_equipment"
    assert "sme_checklist" in result
    assert len(result["sme_checklist"]) >= 4
    assert result["truncated"] is False
    assert result["truncation_recovered"] is False


def test_suggest_tacit_rules_handles_malformed_llm_response(tmp_path, monkeypatch):
    """R28-C: LLM 응답이 JSON 파싱 실패 시 에러 응답 + 가이드."""
    rawdata, _ = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(rawdata / "MP.csv", ["Point_ID"], [{"Point_ID": "MP001"}])
    import config
    tbox_path = tmp_path / "t_box.ttl"
    tbox_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox_path))
    qt_dir = tmp_path / "query_tests"
    qt_dir.mkdir()
    cq_path = qt_dir / "cq.json"
    cq_path.write_text(json.dumps({"questions": [{"id": "CQ01", "domains": ["MP"]}]}), encoding="utf-8")
    monkeypatch.setattr(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path))

    def fake_invoke_bad(prompt, **kwargs):
        return {"text": "I'll analyze... not a valid JSON at all",
                "stop_reason": "end_turn", "usage": {}}

    monkeypatch.setattr("tools.bedrock.invoke_bedrock_with_metadata", fake_invoke_bad)

    from tools.tacit_rules import suggest_tacit_rules
    result = json.loads(suggest_tacit_rules(max_rules=3, save_to_suggested=False))

    assert result["success"] is False
    assert "파싱" in result["error"] or "JSON" in result["error"]


def test_suggest_tacit_rules_recovers_truncated_response(tmp_path, monkeypatch):
    """R28-D: LLM 응답이 max_tokens 에 걸려 JSON 중간에 끊겨도 완성된 mapping 은 복구.

    실측 회귀: Opus 4.7 이 8 rules 요청에 verbose 하게 응답하다 max_tokens 에 걸림.
    완성된 mapping 3개까지는 있는데 네 번째가 _reasoning 문자열에서 끊긴 패턴.
    """
    rawdata, _ = _setup_dirs(tmp_path, monkeypatch)
    _write_csv(rawdata / "MP.csv", ["Point_ID"], [{"Point_ID": "MP001"}])
    import config
    tbox_path = tmp_path / "t_box.ttl"
    tbox_path.write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox_path))
    qt_dir = tmp_path / "query_tests"
    qt_dir.mkdir()
    cq_path = qt_dir / "cq.json"
    cq_path.write_text(
        json.dumps({"questions": [{"id": "CQ01", "domains": ["MP"]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path))

    # 실측 패턴 재현 — 2개 완성 + 3번째 _reasoning 에서 끊김.
    truncated = """{
      "mappings": [
        {"name": "r1", "strategy": "number_match", "source_csv": "MP.csv",
         "source_class": "MP", "source_pk_column": "Point_ID",
         "target_class": "EQ", "target_pk_template": "EQ{n:03d}",
         "ops": ["mapsToEq"], "output_file": "r1.ttl"},
        {"name": "r2", "strategy": "simple_join", "source_csv": "MP.csv",
         "source_class": "MP", "source_fk_column": "Equipment_ID",
         "target_class": "EQ", "op": "hasEq", "output_file": "r2.ttl"},
        {"name": "r3", "strategy": "rotation", "source_csv": "MP.csv",
         "source_class": "MP", "target_class": "EQ",
         "_reasoning": "Equipment_ID 가 Equipment_Master"""  # 중간에 끊김

    def fake_invoke(prompt, **kwargs):
        return {"text": truncated, "stop_reason": "max_tokens", "usage": {}}

    monkeypatch.setattr("tools.bedrock.invoke_bedrock_with_metadata", fake_invoke)

    from tools.tacit_rules import suggest_tacit_rules
    result = json.loads(suggest_tacit_rules(max_rules=8, save_to_suggested=False))

    assert result["success"] is True, f"복구 실패: {result}"
    assert result["truncated"] is True
    assert result["truncation_recovered"] is True
    # 완성된 2개 mapping 복구
    assert result["mappings_count"] == 2
    names = [m["name"] for m in result["preview"]]
    assert "r1" in names and "r2" in names


def test_recover_truncated_mappings_json_empty_returns_none():
    from tools.tacit_rules import _recover_truncated_mappings_json
    assert _recover_truncated_mappings_json("not json") is None
    assert _recover_truncated_mappings_json('{"other": []}') is None


def test_recover_truncated_mappings_json_complete_object_returned():
    from tools.tacit_rules import _recover_truncated_mappings_json
    truncated = '{"mappings": [{"name": "a", "x": 1}, {"name": "b"'
    recovered = _recover_truncated_mappings_json(truncated)
    assert recovered is not None
    parsed = json.loads(recovered)
    assert len(parsed["mappings"]) == 1
    assert parsed["mappings"][0]["name"] == "a"


def test_empty_mappings_returns_noop_with_guide(tmp_path, monkeypatch):
    """R28-B: 빈 mappings (신규 도메인 초기 상태) 는 에러 아닌 no-op + 가이드."""
    _setup_dirs(tmp_path, monkeypatch)
    rules_path = _write_rules(tmp_path, [])
    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))
    assert result["success"] is True
    assert result["rules_applied"] == 0
    assert result["rules_with_output"] == 0
    assert result["files_written"] == []
    # 사용자 가이드 문자열이 포함되어야 함
    assert "note" in result
    assert "suggest_tacit_rules" in result["note"] or "example" in result["note"].lower()
