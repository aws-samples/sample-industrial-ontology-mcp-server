"""P2b — resolve_entity_references MCP 도구 단위 테스트."""
from __future__ import annotations

import json


def _make_abox_ttl(satellite_triples: list[tuple[str, str, str]]) -> str:
    """minimal A-Box TTL 생성."""
    prefix = """@prefix steel: <http://example.com/steel-ontology#> .
@prefix steelI: <http://example.com/steel-ontology/instances#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
"""
    body = "\n".join(f"steelI:{s} rdf:type steel:{t} ." for s, t in [
        ("EquipmentMaster_EQ001", "EquipmentMaster"),
        ("EquipmentMaster_EQ002", "EquipmentMaster"),
        ("MaintenanceHistory_MT001", "MaintenanceHistory"),
    ])
    body += "\n" + "\n".join(satellite_triples)
    return prefix + "\n" + body


def _resolve(monkeypatch, tmp_path, **kwargs):
    """tmp_path 를 A-Box 디렉터리로 두고 파일명만 넘겨 MCP 도구를 호출한다."""
    import tools.entity_resolution as entity_resolution

    monkeypatch.setattr(entity_resolution, "GENERATED_ABOX_DIR", str(tmp_path))
    return entity_resolution.resolve_entity_references(**kwargs)


def test_all_fk_resolved_when_master_contains_all_targets(tmp_path, monkeypatch):
    """모든 satellite FK 가 master 에 존재 → resolved 100%."""
    abox = tmp_path / "abox.ttl"
    master = tmp_path / "master.ttl"
    master.write_text("""@prefix steel: <http://example.com/steel-ontology#> .
@prefix steelI: <http://example.com/steel-ontology/instances#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steelI:EquipmentMaster_EQ001 rdf:type steel:EquipmentMaster .
steelI:EquipmentMaster_EQ002 rdf:type steel:EquipmentMaster .
""", encoding="utf-8")
    abox.write_text(_make_abox_ttl([
        "steelI:MaintenanceHistory_MT001 steel:maintenanceOfEquipment steelI:EquipmentMaster_EQ001 .",
    ]), encoding="utf-8")

    r = _resolve(monkeypatch, tmp_path, abox_path=abox.name, master_path=master.name)

    assert r["canonical_sources_detected"] == 2
    assert r["fk_match_stages"]["resolved"] >= 1
    assert r["fk_match_stages"]["unresolved"] == 0
    # unresolved 원인의 추천은 없어야 함 (P2a declaration_check 추천은 별개)
    unresolved_recs = [x for x in r["recommendations"] if "unresolved" in x]
    assert unresolved_recs == []


def test_unresolved_fk_produces_recommendation(tmp_path, monkeypatch):
    """master 에 없는 target 참조 → unresolved + 추천 생성."""
    abox = tmp_path / "abox.ttl"
    master = tmp_path / "master.ttl"
    master.write_text("""@prefix steel: <http://example.com/steel-ontology#> .
@prefix steelI: <http://example.com/steel-ontology/instances#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steelI:EquipmentMaster_EQ001 rdf:type steel:EquipmentMaster .
""", encoding="utf-8")
    # satellite 가 master 에 없는 EQ999 참조
    abox.write_text(_make_abox_ttl([
        "steelI:MaintenanceHistory_MT001 steel:maintenanceOfEquipment steelI:EquipmentMaster_EQ999 .",
    ]), encoding="utf-8")

    r = _resolve(monkeypatch, tmp_path, abox_path=abox.name, master_path=master.name)

    assert r["fk_match_stages"]["unresolved"] >= 1
    assert len(r["unresolved_samples"]) >= 1
    assert r["recommendations"]  # 추천 1건 이상


def test_missing_abox_returns_error(tmp_path, monkeypatch):
    """A-Box 파일 없으면 error_response."""
    r = json.loads(_resolve(monkeypatch, tmp_path, abox_path="nonexistent.ttl"))
    assert r["success"] is False
    assert "A-Box not found" in r["error"]


def test_missing_master_uses_empty_set(tmp_path, monkeypatch):
    """master_data.ttl 없어도 A-Box 만으로 실행 (warning + 전체 unresolved)."""
    abox = tmp_path / "abox.ttl"
    abox.write_text(_make_abox_ttl([
        "steelI:MaintenanceHistory_MT001 steel:maintenanceOfEquipment steelI:EquipmentMaster_EQ001 .",
    ]), encoding="utf-8")

    r = _resolve(monkeypatch, tmp_path, abox_path=abox.name, master_path="no_master.ttl")
    # master 0 → 모든 FK link 가 unresolved
    assert r["canonical_sources_detected"] == 0
    assert r["fk_match_stages"]["unresolved"] >= 1


def test_top_sample_capped_at_10(tmp_path, monkeypatch):
    """샘플은 상위 10건으로 제한."""
    abox = tmp_path / "abox.ttl"
    master = tmp_path / "master.ttl"
    master.write_text("@prefix x: <x#> .\n", encoding="utf-8")
    # 15개 unresolved
    triples = [
        f"steelI:MaintenanceHistory_MT{i:03d} steel:relatesTo steelI:EquipmentMaster_EQ{i:03d} ."
        for i in range(15)
    ]
    abox.write_text(_make_abox_ttl(triples), encoding="utf-8")
    r = _resolve(monkeypatch, tmp_path, abox_path=abox.name, master_path=master.name)
    assert len(r["unresolved_samples"]) <= 10


def test_report_includes_file_paths_for_debugging(tmp_path, monkeypatch):
    """리포트에 사용된 abox_path / master_path 포함."""
    abox = tmp_path / "abox.ttl"
    master = tmp_path / "master.ttl"
    abox.write_text("@prefix x: <x#> .\n", encoding="utf-8")
    master.write_text("@prefix x: <x#> .\n", encoding="utf-8")
    r = _resolve(monkeypatch, tmp_path, abox_path=abox.name, master_path=master.name)
    assert r["abox_path"] == str(abox)
    assert r["master_path"] == str(master)


# ── P2a: declarative canonical_masters 대조 ────────────────────────


def test_compare_declared_vs_discovered_all_match():
    """선언과 discovery 가 완전 일치 → consistent=True."""
    from tools.entity_resolution import _compare_declared_vs_discovered
    declared = {
        "EquipmentMaster": {"master_csv": "Equipment_Master", "pk_column": "Equipment_ID"},
        "ProductMaster": {"master_csv": "Product_Master", "pk_column": "Product_ID"},
    }
    discovered = {
        "EquipmentMaster": {"eq001": "uri1"},
        "ProductMaster": {"p001": "uri2"},
    }
    result = _compare_declared_vs_discovered(declared, discovered)
    assert result["consistent"] is True
    assert result["missing_in_discovery"] == []
    assert result["missing_in_declaration"] == []


def test_compare_declared_but_not_discovered_flagged():
    """선언됐으나 master_data.ttl 에 인스턴스 없음 → missing_in_discovery."""
    from tools.entity_resolution import _compare_declared_vs_discovered
    declared = {
        "EquipmentMaster": {"master_csv": "Equipment_Master", "pk_column": "Equipment_ID"},
        "MissingMaster": {"master_csv": "Missing", "pk_column": "ID"},
    }
    discovered = {"EquipmentMaster": {"eq001": "uri1"}}
    result = _compare_declared_vs_discovered(declared, discovered)
    assert "MissingMaster" in result["missing_in_discovery"]
    assert result["consistent"] is False


def test_compare_discovered_but_not_declared_flagged():
    """master_data.ttl 에 있으나 선언 없음 → missing_in_declaration."""
    from tools.entity_resolution import _compare_declared_vs_discovered
    declared = {}
    discovered = {"PurchaseOrder": {"po001": "uri1"}}
    result = _compare_declared_vs_discovered(declared, discovered)
    assert "PurchaseOrder" in result["missing_in_declaration"]
    assert result["consistent"] is False


def test_load_canonical_masters_declaration_returns_dict():
    """실제 rules/contracts/fk_patterns.json 로드 — dict 반환 확인."""
    from tools.entity_resolution import _load_canonical_masters_declaration
    decl = _load_canonical_masters_declaration()
    assert isinstance(decl, dict)
    # 프로덕션 파일에 선언 있으면 schema 검증
    if decl:
        for _cls, entry in decl.items():
            assert "master_csv" in entry
            assert "pk_column" in entry


def test_resolve_report_includes_declaration_check(tmp_path, monkeypatch):
    """리포트에 declaration_check 섹션 포함."""
    abox = tmp_path / "abox.ttl"
    master = tmp_path / "master.ttl"
    abox.write_text("@prefix x: <x#> .\n", encoding="utf-8")
    master.write_text("@prefix x: <x#> .\n", encoding="utf-8")
    r = _resolve(monkeypatch, tmp_path, abox_path=abox.name, master_path=master.name)
    assert "declaration_check" in r
    assert "declared_classes" in r["declaration_check"]
    assert "discovered_classes" in r["declaration_check"]
    assert "consistent" in r["declaration_check"]
