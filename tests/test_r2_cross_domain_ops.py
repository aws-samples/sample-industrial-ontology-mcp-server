"""Tests for R2 — cross-domain OP auto-injection via design_patterns.json.

이 세션에서 수동 추가한 10쌍 cross-domain OP 가 재생성 시 자동 복원되는지
검증. 설정 방법:
  1. rules/domain/design_patterns.json 의 cross_domain_ops 목록에 OP 이름 추가
  2. tools/ontology_quality.py 의 _CROSS_DOMAIN_OP_META 딕셔너리에 메타 추가
  3. Step 15 가 자동으로 정방향 + 역방향 OP 를 T-Box 에 주입
"""
from __future__ import annotations

import json

from domain.rules_paths import rules_path

# CQ06/09/10/11 에서 요구된 10쌍 cross-domain OP
R2_CROSS_DOMAIN_OPS = [
    # (op_name, domain, range)
    ("hasGHGAirEmissionMonitoring", "GHGEmission", "AirEmissionMonitoring"),
    ("hasFuelAirEmissionMonitoring", "FuelConsumption", "AirEmissionMonitoring"),
    ("hasEnergySourceAirEmissionMonitoring", "EnergySourceMaster", "AirEmissionMonitoring"),
    ("hasEnergySourceMonitoringPoint", "EnergySourceMaster", "MonitoringPointMaster"),
    ("hasRealTimeDataChemicalAnalysis", "RealTimeData", "ChemicalAnalysis"),
    ("hasTagMasterChemicalAnalysis", "TagMaster", "ChemicalAnalysis"),
    ("hasTagMasterProduct", "TagMaster", "ProductMaster"),
    ("hasNoiseVibrationSteamEnergy", "NoiseVibrationMonitoring", "SteamEnergy"),
    ("hasNoiseVibrationWasteManagement", "NoiseVibrationMonitoring", "WasteManagement"),
    ("hasFailureCauseInventoryTransaction", "FailureCause", "InventoryTransaction"),
]


class TestR2OpsRegisteredInDesignPatterns:
    """Design patterns JSON 에 10쌍 OP 이름이 등록되어 있어야 한다."""

    def test_all_ten_ops_present_in_design_patterns(self):
        with open(rules_path("design_patterns.json"), encoding="utf-8") as f:
            dp = json.load(f)
        all_ops: set[str] = set()
        for _domain_ko, cfg in dp.get("domain_hierarchy", {}).items():
            for op_name in cfg.get("cross_domain_ops", []):
                all_ops.add(op_name)

        expected = {op for op, _, _ in R2_CROSS_DOMAIN_OPS}
        missing = expected - all_ops
        assert not missing, f"design_patterns.json 에 OP 누락: {missing}"


class TestR2OpsMetaRegistered:
    """_CROSS_DOMAIN_OP_META 에 10쌍 OP 메타데이터 (domain/range/label 등) 등록."""

    def test_all_ten_ops_have_meta(self):
        from tools.ontology_quality import _CROSS_DOMAIN_OP_META
        for op_name, expected_dom, expected_rng in R2_CROSS_DOMAIN_OPS:
            assert op_name in _CROSS_DOMAIN_OP_META, \
                f"_CROSS_DOMAIN_OP_META 에 {op_name} 누락"
            meta = _CROSS_DOMAIN_OP_META[op_name]
            # (domain, range, label_en, label_ko, comment_ko, inverse)
            assert meta[0] == expected_dom, \
                f"{op_name} domain 불일치: expected={expected_dom}, got={meta[0]}"
            assert meta[1] == expected_rng, \
                f"{op_name} range 불일치: expected={expected_rng}, got={meta[1]}"
            # label_en + label_ko + comment_ko + inverse 모두 존재
            assert len(meta) == 6
            assert all(isinstance(x, str) and x for x in meta), \
                f"{op_name} 메타 필드 누락/빈 문자열: {meta}"


class TestR2OpsAutoInjectionIntoTBox:
    """실제 improve_tbox 호출 시 Step 15 가 10쌍 OP 를 T-Box 에 자동 주입.

    **게이트 도입 후 (2026-08-14)**: 이 10쌍은 CSV FK 가 잇지 않는 쌍이고
    A-Box·tacit 사용량도 전부 0 이다 (실측). 그래서 기본 모드
    (``TBOX_CROSS_DOMAIN_FK_GATE=skip``) 에서는 **의도적으로 주입되지 않는다** —
    값 0건 OP 는 질의가 0건을 정답처럼 돌려주고 딕셔너리·CQ 를 오염시킨다.

    주입 경로 자체는 살아 있어야 하므로 (도메인 지식 관계를 의도적으로 넣고 싶은
    운영자를 위해) ``warn`` 모드에서 주입을 검증하고, 기본 모드에서는 차단을
    검증한다. 두 방향을 모두 주장해야 게이트가 켜졌는지/꺼졌는지 구분된다.
    """

    def test_step15_injects_all_ten_ops_when_gate_disabled(self, tmp_path, monkeypatch):
        """CSV 40개 + 모든 필수 class 가 있는 최소 T-Box 로 injection 확인 (warn 모드)."""
        monkeypatch.setenv("TBOX_CROSS_DOMAIN_FK_GATE", "warn")
        from rdflib import OWL, RDF, RDFS, URIRef

        from domain.tbox_utils import _new_graph
        from tools.ontology_quality import improve_tbox

        DOMAIN_NS = "http://example.com/steel-ontology#"

        # 10쌍 OP 의 domain/range 에 등장하는 모든 class
        all_classes = set()
        for _op, dom, rng in R2_CROSS_DOMAIN_OPS:
            all_classes.add(dom)
            all_classes.add(rng)

        # 최소 T-Box: class 만 선언, OP 없음 (Step 15 가 채워야 함)
        g = _new_graph()
        for cls in all_classes:
            g.add((URIRef(DOMAIN_NS + cls), RDF.type, OWL.Class))
            g.add((URIRef(DOMAIN_NS + cls), RDFS.label, g.namespace_manager.graph.term(cls) if False
                   else __import__("rdflib").Literal(cls)))
        ttl = g.serialize(format="turtle")

        new_ttl, stats = improve_tbox(ttl)
        new_g = _new_graph()
        new_g.parse(data=new_ttl, format="turtle")

        # 10쌍 OP 전부 ObjectProperty 로 등록되어 있어야 함
        missing = []
        for op_name, _dom, _rng in R2_CROSS_DOMAIN_OPS:
            op_uri = URIRef(DOMAIN_NS + op_name)
            if (op_uri, RDF.type, OWL.ObjectProperty) not in new_g:
                missing.append(op_name)
        assert not missing, f"Step 15 가 주입 못한 OP: {missing}"

    def test_default_gate_blocks_ungrounded_ops(self, tmp_path, monkeypatch):
        """기본 모드에서는 FK 근거 없는 10쌍을 주입하지 않는다 (NEGATIVE 방향).

        실측 (2026-08-14): 게이트 없이 S3 를 돌리면 신규 OP 53개 중 39개가 CSV FK
        근거도 A-Box·tacit 데이터도 없었고, OP 사용률이 39.1% → 35.1% 로 떨어지는
        동시에 RR 은 0.323 → 0.412 로 **올랐다**. 지표가 좋아지며 정합이 나빠지는
        구조라, 차단을 명시적으로 주장해 둔다.
        """
        monkeypatch.delenv("TBOX_CROSS_DOMAIN_FK_GATE", raising=False)
        from rdflib import OWL, RDF, RDFS, Literal, URIRef

        from domain.tbox_utils import _new_graph
        from tools.ontology_quality import improve_tbox

        DOMAIN_NS = "http://example.com/steel-ontology#"
        all_classes: set[str] = set()
        for _op, dom, rng in R2_CROSS_DOMAIN_OPS:
            all_classes.add(dom)
            all_classes.add(rng)

        g = _new_graph()
        for cls in all_classes:
            g.add((URIRef(DOMAIN_NS + cls), RDF.type, OWL.Class))
            g.add((URIRef(DOMAIN_NS + cls), RDFS.label, Literal(cls)))
        new_ttl, stats = improve_tbox(g.serialize(format="turtle"))
        new_g = _new_graph()
        new_g.parse(data=new_ttl, format="turtle")

        injected = [
            op_name for op_name, _d, _r in R2_CROSS_DOMAIN_OPS
            if (URIRef(DOMAIN_NS + op_name), RDF.type, OWL.ObjectProperty) in new_g
        ]
        assert not injected, (
            f"FK 근거 없는 OP 가 기본 모드에서 주입됐다: {injected} — "
            "게이트가 꺼졌거나 판정이 무력화됐다"
        )
        assert stats.get("cross_domain_ops_skipped_no_fk", 0) > 0, (
            "차단 카운터가 0 이다 — 게이트가 돌지 않았다 "
            f"(mode={stats.get('cross_domain_fk_gate_mode')}, "
            f"signal_unavailable={stats.get('cross_domain_fk_signal_unavailable')})"
        )
