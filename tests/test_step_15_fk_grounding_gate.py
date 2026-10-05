"""Step 15 FK 근거 게이트 — CSV FK 가 잇지 않는 관계는 만들지 않는다.

## 배경 (실측)

게이트 없이 S3 를 1회 실행한 결과 (2026-08-14, 배포 T-Box 기준):
  신규 OP 53개 = FK 근거 8 / A-Box·tacit 데이터 보유 14 / **둘 다 없음 39**
  OP 사용률 39.1% → 35.1% (악화)   RR 0.323 → 0.412 (개선)

RR 게이트가 요구하는 지표는 좋아지는데 실제 데이터 정합은 나빠진다. 값 0건 OP 는
(1) 질의가 0건을 정답처럼 돌려주고 (2) 딕셔너리·CQ 를 오염시키고 (3) 중복 쌍을
만들어 A-Box 의 OP 이름 선택을 흔든다.

## 이 테스트가 주장하는 것

양방향을 모두 본다 — 차단만 주장하면 "전부 차단" 이라는 과잉 게이트를 통과시킨다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

DOMAIN_NS = "http://example.com/steel-ontology#"


def _tbox(classes: list[str]) -> Graph:
    from domain.tbox_utils import _new_graph
    g = _new_graph()
    for cls in classes:
        g.add((URIRef(DOMAIN_NS + cls), RDF.type, OWL.Class))
        g.add((URIRef(DOMAIN_NS + cls), RDFS.label, Literal(cls)))
    return g


def _apply(g: Graph):
    from tools.quality_steps import step_15_cross_domain_ops as s15
    from tools.quality_steps._base import StepContext
    return s15.apply(g, StepContext(domain_ns=DOMAIN_NS))


def _op_names(g: Graph) -> set[str]:
    return {
        str(p).rsplit("#", 1)[-1]
        for p in g.subjects(RDF.type, OWL.ObjectProperty)
    }


class TestFkGroundingGate:

    def test_pair_without_fk_or_data_is_skipped(self, monkeypatch):
        """CSV FK 근거도 데이터도 없으면 만들지 않는다."""
        monkeypatch.delenv("TBOX_CROSS_DOMAIN_FK_GATE", raising=False)
        # FK 쌍은 실제 CSV 에서 온다. Steam↔EnergySource 는 FK 컬럼이 없다 (실측).
        g = _tbox(["SteamEnergy", "EnergySourceMaster"])
        before = _op_names(g)
        res = _apply(g)
        added = _op_names(g) - before
        assert "hasSteamEnergySource" not in added, (
            f"FK 근거 없는 OP 가 주입됐다: {sorted(added)}"
        )
        assert res.stats.get("cross_domain_ops_skipped_no_fk", 0) >= 1

    def test_data_bearing_pair_survives_gate(self, monkeypatch):
        """A-Box·tacit 이 쓰는 이름은 FK 근거가 없어도 만든다 (NEGATIVE 방향).

        값 있는 관계를 막으면 T-Box 미선언 술어가 되어 중복보다 나쁘다 —
        step_15c 백필이 다시 선언해야 하는 왕복이 생긴다.
        실측: ``hasSoilImpact`` 는 tacit 4,128 트리플을 가지지만
        EquipmentMaster→SoilMonitoring 에 CSV FK 는 없다.
        """
        monkeypatch.delenv("TBOX_CROSS_DOMAIN_FK_GATE", raising=False)
        g = _tbox(["EquipmentMaster", "SoilMonitoring"])
        before = _op_names(g)
        _apply(g)
        added = _op_names(g) - before
        assert "hasSoilImpact" in added, (
            "tacit 이 4,128 트리플에서 쓰는 관계가 차단됐다 — 미선언 술어가 된다. "
            f"added={sorted(added)}"
        )

    def test_warn_mode_restores_previous_behaviour(self, monkeypatch):
        """warn 모드는 기존 동작 (만들고 카운트만)."""
        monkeypatch.setenv("TBOX_CROSS_DOMAIN_FK_GATE", "warn")
        g = _tbox(["SteamEnergy", "EnergySourceMaster"])
        before = _op_names(g)
        res = _apply(g)
        added = _op_names(g) - before
        assert "hasSteamEnergySource" in added, (
            f"warn 모드인데 주입되지 않았다: {sorted(added)}"
        )
        assert res.stats.get("cross_domain_ops_skipped_no_fk", 0) == 0

    def test_gate_leaves_no_dangling_inverse(self, monkeypatch):
        """쌍 단위로 건너뛰므로 dangling inverseOf 가 생기지 않는다.

        leg 단위로 판정하면 정방향의 ``owl:inverseOf`` 가 선언되지 않은 이름을
        가리켜 역방향 트리플이 사라진다 (2026-08-11 실측: 0 → 15).
        """
        monkeypatch.delenv("TBOX_CROSS_DOMAIN_FK_GATE", raising=False)
        g = _tbox([
            "SteamEnergy", "EnergySourceMaster", "GasEnergy",
            "EquipmentMaster", "SoilMonitoring", "AirEmissionMonitoring",
            "WasteManagement",
        ])
        _apply(g)
        ops = _op_names(g)
        dangling = [
            (str(a).rsplit("#", 1)[-1], str(b).rsplit("#", 1)[-1])
            for a, b in g.subject_objects(OWL.inverseOf)
            if str(a).rsplit("#", 1)[-1] not in ops
            or str(b).rsplit("#", 1)[-1] not in ops
        ]
        assert not dangling, f"dangling inverseOf: {dangling}"


class TestSharedFkPairHelper:
    """FK 쌍 도출은 공용 헬퍼 하나만 쓴다 (사본 갈라짐 방지)."""

    def test_helper_matches_s2_private_derivation(self):
        from domain.graph_utils import csv_fk_class_pairs
        from tools.multi_agent_tbox import _csv_fk_pairs

        shared = csv_fk_class_pairs()
        s2 = _csv_fk_pairs()
        assert shared is not None and s2 is not None, "실측 CSV 로 판정 가능해야 한다"
        assert shared == s2, (
            "공용 헬퍼와 S2 도출이 갈라졌다 — 한쪽만 고쳐진 것이다. "
            f"공용에만={sorted(shared - s2)[:5]} S2에만={sorted(s2 - shared)[:5]}"
        )

    def test_unavailable_signal_is_none_not_empty(self, monkeypatch):
        """판정 불가는 None — 0건과 구분돼야 게이트가 정당한 주입을 막지 않는다."""
        import config
        from domain.graph_utils import csv_fk_class_pairs
        monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", "/nonexistent/path/xyz")
        assert csv_fk_class_pairs() is None
