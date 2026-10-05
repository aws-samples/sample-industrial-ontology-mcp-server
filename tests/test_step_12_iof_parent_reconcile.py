"""Step 12 — 추상 클래스의 IOF 부모를 design_patterns.json 에 맞추는지 고정.

## 배경 (2026-08-17 실측)

``cfg["parent"]`` 는 클래스를 **새로 만들 때만** 적용됐다. S2 가 같은 이름의 추상
클래스를 먼저 만들면(프롬프트에 골격이 주입되므로 흔하다) 이 스텝은 "이미 존재" 로
스킵하고 S2 가 상상한 IOF 부모가 그대로 배포된다 — config 가 읽히지만 적용되지 않는
**죽은 설정** 이 된다.

배포 T-Box 실측: config 의 ``iof_parent`` 와 실제 ``rdfs:subClassOf`` 가 **9건 불일치**.
추상 '관리/모니터링' 카테고리 5개가 물리 객체 ``MaterialArtifact`` 로 선언돼 있었다.

## 이것이 만든 논리 오류

IOF 를 **실제로 로드** 하면 ``SupplierMaster ⊑ Organization`` 과
``⊑ SupplyChainManagement ⊑ MaterialArtifact`` 가 충돌해 unsatisfiable 이 된다
(HermiT 실측; ``Organization`` ⊥ ``MaterialArtifact``). 교정 후 unsat 0.

배포 상태에서는 ``owl:imports`` 가 네임스페이스 드리프트로 한 트리플도 로드하지 않아
(``Core.rdf`` = ``/ontology/construct/``, T-Box = ``/ontology/core/Core/``) 이 모순이
조용히 숨어 있었다. "HermiT consistent" 는 공리가 없어서 얻은 침묵이었다.
"""
from __future__ import annotations

import json

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from domain.rules_paths import rules_path
from tools.quality_steps import step_12_intermediate_abstract as step

CORE = "https://spec.industrialontologies.org/ontology/core/Core/"
MAINT = "https://spec.industrialontologies.org/ontology/maintenance/Maintenance/"


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _ln(u) -> str:
    return str(u).rsplit("/", 1)[-1].rsplit("#", 1)[-1]


class TestReconcileHelper:
    """``_reconcile_iof_parent`` 단위 — 외부 부모만, config 기준으로."""

    def _g(self, extra: str = "") -> Graph:
        g = Graph()
        g.parse(data=(
            f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            f"@prefix iof: <{CORE}> .\n"
            f"{NS_PREFIX}:AbstractCat a owl:Class .\n" + extra
        ), format="turtle")
        return g

    def test_replaces_wrong_iof_parent(self):
        """config 미승인 외부 부모는 제거하고 승인된 것을 넣는다."""
        g = self._g(
            f"{NS_PREFIX}:AbstractCat rdfs:subClassOf iof:MaterialArtifact .\n"
        )
        cfg = {"class": "AbstractCat", "parent": CORE + "PlannedProcess"}
        n, s = step._reconcile_iof_parent(g, D("AbstractCat"), cfg, DOMAIN_NS)
        parents = {_ln(o) for o in g.objects(D("AbstractCat"), RDFS.subClassOf)}
        assert parents == {"PlannedProcess"}, f"교정 실패: {parents}"
        assert n == 2 and any("-MaterialArtifact" in x for x in s)

    def test_adds_missing_iof_parent(self):
        """외부 부모가 없으면 config 것을 추가한다."""
        g = self._g()
        cfg = {"class": "AbstractCat", "parent": CORE + "MeasurementInformationContentEntity"}
        n, _ = step._reconcile_iof_parent(g, D("AbstractCat"), cfg, DOMAIN_NS)
        assert (D("AbstractCat"), RDFS.subClassOf,
                URIRef(cfg["parent"])) in g
        assert n == 1

    def test_preserves_domain_parents(self):
        """도메인(steel:) 부모는 건드리지 않는다 (NEGATIVE 방향).

        이 함수의 권한은 IOF 정렬뿐이다. 도메인 계층을 지우면 DIT/NOC 가 무너진다.
        """
        g = self._g(
            f"{NS_PREFIX}:AbstractCat rdfs:subClassOf {NS_PREFIX}:DomainParent, "
            f"iof:MaterialArtifact .\n"
            f"{NS_PREFIX}:DomainParent a owl:Class .\n"
        )
        cfg = {"class": "AbstractCat", "parent": CORE + "PlannedProcess"}
        step._reconcile_iof_parent(g, D("AbstractCat"), cfg, DOMAIN_NS)
        assert (D("AbstractCat"), RDFS.subClassOf, D("DomainParent")) in g, (
            "도메인 부모를 지웠다 — 계층이 무너진다"
        )

    def test_noop_when_already_correct(self):
        """이미 맞으면 아무것도 하지 않는다."""
        g = self._g(
            f"{NS_PREFIX}:AbstractCat rdfs:subClassOf iof:PlannedProcess .\n"
        )
        cfg = {"class": "AbstractCat", "parent": CORE + "PlannedProcess"}
        n, _ = step._reconcile_iof_parent(g, D("AbstractCat"), cfg, DOMAIN_NS)
        assert n == 0

    def test_noop_when_config_has_no_iof_parent(self):
        """config 에 parent 가 없거나 도메인 IRI 면 손대지 않는다."""
        g = self._g(
            f"{NS_PREFIX}:AbstractCat rdfs:subClassOf iof:MaterialArtifact .\n"
        )
        for cfg in ({"class": "AbstractCat", "parent": ""},
                    {"class": "AbstractCat", "parent": DOMAIN_NS + "Local"}):
            n, _ = step._reconcile_iof_parent(g, D("AbstractCat"), cfg, DOMAIN_NS)
            assert n == 0, f"config={cfg['parent']!r} 인데 교정했다"
        assert (D("AbstractCat"), RDFS.subClassOf,
                URIRef(CORE + "MaterialArtifact")) in g


class TestDeployedTBoxMatchesConfig:
    """**산출물 기반**: 배포 T-Box 의 IOF 부모가 config 와 일치한다."""

    def test_no_mismatch_against_design_patterns(self):
        import os

        import config as _cfg
        tbox = _cfg.TBOX_PATH
        if not os.path.exists(tbox):
            import pytest
            pytest.skip("배포 T-Box 없음")
        g = Graph()
        g.parse(tbox, format="turtle")
        with open(rules_path("design_patterns.json"), encoding="utf-8") as fh:
            dp = json.load(fh)

        mismatch = []
        for _domain, cfg in dp.get("domain_hierarchy", {}).items():
            ac = cfg.get("abstract_class")
            ip = cfg.get("iof_parent")
            if not (ac and ip):
                continue
            if (D(ac), RDF.type, OWL.Class) not in g:
                continue
            actual = sorted(
                _ln(o) for o in g.objects(D(ac), RDFS.subClassOf)
                if str(o).startswith((CORE, MAINT))
            )
            expected = ip.split(":")[-1]
            if actual != [expected]:
                mismatch.append((ac, expected, actual))
        assert not mismatch, (
            "배포 T-Box 의 IOF 부모가 design_patterns.json 과 어긋난다 — config 가 "
            f"죽은 설정이 됐다: {mismatch}"
        )

    def test_config_governed_categories_are_not_material_artifacts(self):
        """config 가 관리하는 추상 카테고리는 승인되지 않은 물리 객체 부모를 갖지 않는다.

        ``SupplyChainManagement ⊑ MaterialArtifact`` 가 IOF 로드 시
        ``Organization`` ⊥ ``MaterialArtifact`` 충돌을 만들어 ``SupplierMaster`` 를
        unsatisfiable 로 만들었다.

        **범위를 config 관리 대상으로 한정한다**: ``EquipmentManagement`` 는 config 가
        ``MaterialArtifact`` 를 명시 승인했고(설비는 실제로 물리 객체다) 그것은 결함이
        아니다. 이름 패턴만으로 판정하면 정당한 모델링을 결함으로 오판한다.
        config 밖 클래스(예: S2 가 만든 ``QualityManagement``)는 이 계약의 대상이
        아니므로 별도 판단이 필요하다.
        """
        import os

        import config as _cfg
        if not os.path.exists(_cfg.TBOX_PATH):
            import pytest
            pytest.skip("배포 T-Box 없음")
        g = Graph()
        g.parse(_cfg.TBOX_PATH, format="turtle")
        with open(rules_path("design_patterns.json"), encoding="utf-8") as fh:
            dp = json.load(fh)
        approved = {
            cfg["abstract_class"]: (cfg.get("iof_parent") or "").split(":")[-1]
            for cfg in dp.get("domain_hierarchy", {}).values()
            if cfg.get("abstract_class")
        }
        ma = URIRef(CORE + "MaterialArtifact")
        bad = [
            _ln(s) for s in g.subjects(RDFS.subClassOf, ma)
            if _ln(s) in approved and approved[_ln(s)] != "MaterialArtifact"
        ]
        assert not bad, (
            "config 가 다른 부모를 지정했는데 MaterialArtifact 로 선언됐다 — "
            f"IOF 로드 시 unsatisfiable 위험: {bad}"
        )
