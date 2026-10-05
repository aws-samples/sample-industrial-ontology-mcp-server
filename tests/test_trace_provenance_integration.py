"""A4 trace_provenance 통합 테스트 — A-Box 생성 후 역추적이 실제 동작하는지.

annotate_row_provenance + instance→row_uri 링크를 sidecar 에 심은 후
trace_instance_to_cell 이 csv_table / row_number 을 복원하는지 검증.
"""
from __future__ import annotations

from rdflib import Graph, URIRef

from tools.provenance import (
    PROV,
    annotate_row_provenance,
    make_row_provenance_uri,
    trace_instance_to_cell,
)


def test_trace_instance_to_cell_row_level():
    """Row-level provenance 매립 후 trace_instance_to_cell 로 복원."""
    sidecar = Graph()
    inst = URIRef("http://ex.org/steel-inst/EquipmentMaster_EQ001")
    row_uri = make_row_provenance_uri("Equipment_Master", 42)

    # A-Box 본체 역할: instance → row_uri 링크 (실제 pipeline 에선 a_box.ttl)
    sidecar.add((inst, URIRef(f"{PROV}wasDerivedFrom"), row_uri))

    # Sidecar 역할: row entity metadata
    annotate_row_provenance(sidecar, inst, "Equipment_Master", 42)

    result = trace_instance_to_cell(sidecar, inst)
    assert result["csv_table"] == "Equipment_Master"
    assert result["row_number"] == 42
    # cells 는 없음 — row-level only (cell-level 활성화 시 여기에 채워짐)
    assert result["cells"] == []


def test_trace_instance_missing_row_link():
    """A-Box 본체 링크 없으면 cell_table/row_number 모두 None."""
    sidecar = Graph()
    inst = URIRef("http://ex.org/steel-inst/NoLink_N001")
    # row entity 는 있지만 instance → row 링크가 없음
    annotate_row_provenance(sidecar, inst, "Test", 1)

    result = trace_instance_to_cell(sidecar, inst)
    assert result["csv_table"] is None
    assert result["row_number"] is None


def test_trace_instance_multi_table_independent():
    """서로 다른 row 에 매핑된 두 인스턴스를 각자 복원."""
    sidecar = Graph()
    inst_a = URIRef("http://ex.org/steel-inst/Equipment_EQ001")
    inst_b = URIRef("http://ex.org/steel-inst/Alarm_AL100")
    row_a = make_row_provenance_uri("Equipment", 10)
    row_b = make_row_provenance_uri("Alarm", 999)

    sidecar.add((inst_a, URIRef(f"{PROV}wasDerivedFrom"), row_a))
    sidecar.add((inst_b, URIRef(f"{PROV}wasDerivedFrom"), row_b))
    annotate_row_provenance(sidecar, inst_a, "Equipment", 10)
    annotate_row_provenance(sidecar, inst_b, "Alarm", 999)

    res_a = trace_instance_to_cell(sidecar, inst_a)
    res_b = trace_instance_to_cell(sidecar, inst_b)
    assert res_a["csv_table"] == "Equipment"
    assert res_a["row_number"] == 10
    assert res_b["csv_table"] == "Alarm"
    assert res_b["row_number"] == 999


def test_disk_roundtrip_trace_provenance(tmp_path):
    """A-Box TTL + sidecar TTL 을 디스크에 쓰고 파싱 후 trace_instance_to_cell 동작."""
    # A-Box body: inst → row_uri link
    abox = Graph()
    inst = URIRef("http://example.org/steel-inst#EquipmentMaster_EQ001")
    row_uri = make_row_provenance_uri("Equipment_Master", 42)
    abox.add((inst, URIRef(f"{PROV}wasDerivedFrom"), row_uri))

    # Sidecar: row entity metadata
    sidecar = Graph()
    annotate_row_provenance(sidecar, inst, "Equipment_Master", 42)

    # 디스크에 쓰고
    abox_path = tmp_path / "a_box.ttl"
    sidecar_path = tmp_path / "abox_provenance.ttl"
    abox.serialize(destination=str(abox_path), format="turtle")
    sidecar.serialize(destination=str(sidecar_path), format="turtle")

    # 파싱 후 두 그래프 병합
    reloaded = Graph()
    reloaded.parse(str(abox_path), format="turtle")
    reloaded.parse(str(sidecar_path), format="turtle")

    # 역추적 검증
    result = trace_instance_to_cell(reloaded, inst)
    assert result["csv_table"] == "Equipment_Master"
    assert result["row_number"] == 42
