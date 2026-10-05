"""R25 regression — cross_domain_ops CSV-backed matching bugfix.

Prior behaviour: `_csv_backed_classes` set contained raw filenames
(`Air_Emission_Monitoring`), but comparison to PascalCase class names
(`AirEmissionMonitoring`) used substring matching that never matched —
all CSV-backed checks evaluated False, so cross_domain_ops were never
added despite valid domain/range classes existing.

Fix: normalize both sides by also storing underscore-stripped variant.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS


def _make_tbox_with_classes(class_names: list[str]) -> str:
    ttl_prefix = f"""
    @prefix steel: <{DOMAIN_NS}> .
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    @prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
    """
    body_lines = []
    for cls in class_names:
        body_lines.append(
            f"steel:{cls} a owl:Class ; rdfs:label \"{cls}\"@en, \"{cls}\"@ko ; rdfs:comment \"test\"@ko ."
        )
    return ttl_prefix + "\n" + "\n".join(body_lines)


def test_cross_domain_ops_injected_when_csv_files_exist(tmp_path, monkeypatch):
    """R25: cross_domain_ops 가 CSV 파일에 매칭되는 클래스에 대해 실제로 추가된다.

    이 테스트가 지키는 버그는 **underscore 표기 불일치** 다 (파일명
    ``Air_Emission_Monitoring`` vs 클래스 ``AirEmissionMonitoring``). FK 근거
    게이트(2026-08-14 신설)와는 다른 축이므로, 게이트를 ``warn`` 으로 두어
    원래의 판별력을 유지한다 — 게이트가 켜져 있으면 이 픽스처의 CSV 에 FK
    컬럼이 없어 항상 skip 되고, underscore 버그를 되살려도 테스트가 통과한다.
    """
    # 가짜 CSV 디렉토리 생성 (underscore 파일명 사용 — 실제 프로젝트 네이밍 반영)
    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    (csv_dir / "Air_Emission_Monitoring.csv").write_text("Monitor_ID\nM001\n")
    (csv_dir / "Waste_Management.csv").write_text("Waste_ID\nW001\n")

    # T-Box 에 두 클래스 존재
    ttl = _make_tbox_with_classes(["AirEmissionMonitoring", "WasteManagement"])

    # SOURCE_RAWDATA_DIR 를 가짜 dir 로 패치
    import config
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(csv_dir))
    monkeypatch.setenv("TBOX_CROSS_DOMAIN_FK_GATE", "warn")

    # _CROSS_DOMAIN_OP_META 에 테스트용 OP 추가 — correlatedWithWaste 는 R25 에서
    # 이미 추가됐으므로 그대로 사용
    from tools.ontology_quality import improve_tbox

    improved_ttl, stats = improve_tbox(ttl)

    g = Graph()
    g.parse(data=improved_ttl, format="turtle")

    # correlatedWithWaste 가 실제로 주입됐어야 함
    op_uri = URIRef(f"{DOMAIN_NS}correlatedWithWaste")
    assert (op_uri, RDF.type, OWL.ObjectProperty) in g, (
        "correlatedWithWaste OP not injected — CSV-backed filter likely rejecting "
        "PascalCase class names due to underscore mismatch"
    )
    # domain / range 확인
    domains = list(g.objects(op_uri, RDFS.domain))
    ranges = list(g.objects(op_uri, RDFS.range))
    assert URIRef(f"{DOMAIN_NS}AirEmissionMonitoring") in domains
    assert URIRef(f"{DOMAIN_NS}WasteManagement") in ranges


def test_cross_domain_ops_skipped_when_csv_file_missing(tmp_path, monkeypatch):
    """CSV 파일이 없으면 OP 를 추가하지 않아야 한다 (regression 안전장치)."""
    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    # AirEmissionMonitoring 만 있고 WasteManagement CSV 는 없음
    (csv_dir / "Air_Emission_Monitoring.csv").write_text("Monitor_ID\nM001\n")

    ttl = _make_tbox_with_classes(["AirEmissionMonitoring", "WasteManagement"])

    import config
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(csv_dir))

    from tools.ontology_quality import improve_tbox
    improved_ttl, stats = improve_tbox(ttl)

    g = Graph()
    g.parse(data=improved_ttl, format="turtle")

    op_uri = URIRef(f"{DOMAIN_NS}correlatedWithWaste")
    # WasteManagement CSV 가 없으므로 OP 추가 안 됨
    assert (op_uri, RDF.type, OWL.ObjectProperty) not in g
