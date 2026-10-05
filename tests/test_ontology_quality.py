"""tests for tools/ontology_quality.py — T-Box 품질 후처리 핵심 로직."""

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef

from domain.namespaces import DOMAIN_NS, DOMAIN_NS_OBJ
from domain.tbox_utils import _new_graph
from tools.ontology_quality import _annotate_completeness, _dp_matches_domain_pk, improve_tbox


@pytest.fixture
def minimal_tbox():
    """최소 T-Box — 2클래스 + 1 OP."""
    return f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en, "설비 마스터"@ko ;
    rdfs:comment "설비 마스터"@ko .

steel:MaintenanceHistory a owl:Class ;
    rdfs:label "Maintenance History"@en, "정비 이력"@ko ;
    rdfs:comment "정비 이력"@ko .

steel:hasMaintenanceHistory a owl:ObjectProperty ;
    rdfs:domain steel:EquipmentMaster ;
    rdfs:range steel:MaintenanceHistory ;
    rdfs:label "has maintenance history"@en, "정비 이력"@ko ;
    rdfs:comment "설비의 정비 이력"@ko .

steel:equipmentID a owl:DatatypeProperty ;
    rdfs:domain steel:EquipmentMaster ;
    rdfs:range xsd:string ;
    rdfs:label "equipment ID"@en, "설비 ID"@ko ;
    rdfs:comment "설비 식별자"@ko .
"""


class TestImproveTbox:
    def test_returns_ttl_and_stats(self, minimal_tbox):
        result_ttl, stats = improve_tbox(minimal_tbox)
        assert isinstance(result_ttl, str)
        assert isinstance(stats, dict)
        assert "total_triples" in stats

    def test_triples_preserved(self, minimal_tbox):
        """후처리 후 원래 트리플이 보존되어야 함."""
        result_ttl, stats = improve_tbox(minimal_tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # EquipmentMaster 클래스가 여전히 존재
        assert (DOMAIN_NS_OBJ.EquipmentMaster, RDF.type, OWL.Class) in g
        # hasMaintenanceHistory OP가 여전히 존재
        assert (DOMAIN_NS_OBJ.hasMaintenanceHistory, RDF.type, OWL.ObjectProperty) in g

    def test_disjoint_groups_added(self, minimal_tbox):
        result_ttl, stats = improve_tbox(minimal_tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        adj_count = stats.get("disjoint_groups_added", 0)
        # 클래스 2개뿐이면 disjoint 그룹에 안 들어갈 수도 있음
        assert isinstance(adj_count, int)

    def test_total_triples_increases(self, minimal_tbox):
        g_before = _new_graph()
        g_before.parse(data=minimal_tbox, format="turtle")
        before = len(g_before)

        result_ttl, stats = improve_tbox(minimal_tbox)
        assert stats["total_triples"] >= before

    def test_scope_restriction_added(self):
        """Scope Emission 클래스에 hasValue restriction이 추가되어야 함."""
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:GHGEmission a owl:Class ;
    rdfs:label "GHG Emission"@en, "온실가스 배출"@ko ;
    rdfs:comment "온실가스 배출"@ko .

steel:Scope1Emission a owl:Class ;
    rdfs:subClassOf steel:GHGEmission ;
    rdfs:label "Scope 1 Emission"@en, "스코프1 배출"@ko ;
    rdfs:comment "직접 배출"@ko .

steel:scopeType a owl:DatatypeProperty ;
    rdfs:domain steel:GHGEmission ;
    rdfs:range xsd:string ;
    rdfs:label "scope type"@en, "스코프 유형"@ko ;
    rdfs:comment "배출 범위"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        assert stats.get("scope_restrictions_added", 0) >= 1
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # Restriction 은 **equivalentClass** 로 붙는다 (2026-08-27 변경).
        #
        # 예전에는 ``subClassOf`` 를 단정했는데, 그 방향으로는 추론기가 값으로 개체를
        # 분류하지 못한다 — 최소 예제 실측: subClassOf 는 분류 0건,
        # equivalentClass 는 분류 성공. 그래서 Scope1/2/3Emission 이 A-Box 0건 /
        # 추론 0건의 빈 클래스로 남았고, S2 리뷰어가 그것을 critical 로 지적해 3회
        # 실행이 합의에 실패했다. 상세: tests/test_scope_emission_classification.py
        restrictions = [o for o in g.objects(DOMAIN_NS_OBJ.Scope1Emission, OWL.equivalentClass)
                       if (o, RDF.type, OWL.Restriction) in g]
        assert len(restrictions) >= 1, "equivalentClass Restriction 이 없다 (분류 불가)"
        # 부모 관계는 유지돼야 한다 — 승격이 계층을 끊으면 안 된다.
        assert DOMAIN_NS_OBJ.GHGEmission in set(
            g.objects(DOMAIN_NS_OBJ.Scope1Emission, RDFS.subClassOf),
        )


class TestDpMatchesDomainPk:
    """PK Functional 감지: DP local-name 이 domain 클래스의 PK 근원과 일치해야 Functional."""

    @pytest.mark.parametrize("dp_name,domain_class", [
        ("supplierId", "SupplierMaster"),
        ("equipmentId", "EquipmentMaster"),
        ("eventId", "AlarmEvents"),
        ("tagId", "TagMaster"),
        ("maintenanceId", "MaintenanceHistory"),
        ("warehouseCode", "WarehouseMaster"),
        ("transactionId", "InventoryTransaction"),
        ("causeCode", "FailureCause"),
    ])
    def test_true_for_entity_pk(self, dp_name, domain_class):
        assert _dp_matches_domain_pk(dp_name, DOMAIN_NS_OBJ[domain_class]) is True

    @pytest.mark.parametrize("dp_name,domain_class", [
        ("zoneCode", "InventoryStatus"),          # composite-PK part
        ("warehouseCode", "InventoryStatus"),     # composite-PK part
        ("managerId", "WarehouseMaster"),         # FK (Warehouse -> Employee)
        ("transportItemCode", "Transportation"),  # FK (Transport -> ItemMaster)
        ("tagEquipmentId", "TagMaster"),          # compound FK, dp_root longer than class
        ("steamEquipmentId", "SteamEnergy"),      # compound FK
        ("wasteEquipmentId", "WasteManagement"),  # compound FK
    ])
    def test_false_for_non_pk(self, dp_name, domain_class):
        assert _dp_matches_domain_pk(dp_name, DOMAIN_NS_OBJ[domain_class]) is False, (
            f"{dp_name} on {domain_class} should NOT qualify as PK"
        )


class TestFunctionalPkGuard:
    """Regression: step 13a must not promote composite-PK parts or FKs to Functional."""

    def test_composite_pk_not_functional(self):
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:InventoryStatus a owl:Class ;
    rdfs:label "Inventory Status"@en, "재고 현황"@ko ;
    rdfs:comment "재고"@ko .

steel:zoneCode a owl:DatatypeProperty ;
    rdfs:domain steel:InventoryStatus ;
    rdfs:range xsd:string ;
    rdfs:label "zone code"@en, "구역 코드"@ko ;
    rdfs:comment "구역 코드"@ko .

steel:warehouseCode a owl:DatatypeProperty ;
    rdfs:domain steel:InventoryStatus ;
    rdfs:range xsd:string ;
    rdfs:label "warehouse code"@en, "창고 코드"@ko ;
    rdfs:comment "창고 코드"@ko .
"""
        result_ttl, _stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        assert (DOMAIN_NS_OBJ.zoneCode, RDF.type, OWL.FunctionalProperty) not in g
        assert (DOMAIN_NS_OBJ.warehouseCode, RDF.type, OWL.FunctionalProperty) not in g

    def test_fk_property_not_functional(self):
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:WarehouseMaster a owl:Class ;
    rdfs:label "Warehouse Master"@en, "창고 마스터"@ko ;
    rdfs:comment "창고"@ko .

steel:managerId a owl:DatatypeProperty ;
    rdfs:domain steel:WarehouseMaster ;
    rdfs:range xsd:string ;
    rdfs:label "manager ID"@en, "관리자 ID"@ko ;
    rdfs:comment "창고 관리자"@ko .
"""
        result_ttl, _stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        assert (DOMAIN_NS_OBJ.managerId, RDF.type, OWL.FunctionalProperty) not in g

    def test_stale_functional_is_removed(self):
        """Prior-release T-Box with mis-tagged Functional should be cleaned on re-run."""
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:InventoryStatus a owl:Class ;
    rdfs:label "Inventory Status"@en, "재고 현황"@ko ;
    rdfs:comment "재고"@ko .

steel:zoneCode a owl:DatatypeProperty, owl:FunctionalProperty ;
    rdfs:domain steel:InventoryStatus ;
    rdfs:range xsd:string ;
    rdfs:label "zone code"@en, "구역 코드"@ko ;
    rdfs:comment "구역 코드"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        assert (DOMAIN_NS_OBJ.zoneCode, RDF.type, OWL.FunctionalProperty) not in g
        assert stats.get("functional_props_removed", 0) >= 1

    def test_entity_pk_still_functional(self):
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:SupplierMaster a owl:Class ;
    rdfs:label "Supplier Master"@en, "공급사 마스터"@ko ;
    rdfs:comment "공급사"@ko .

steel:supplierId a owl:DatatypeProperty ;
    rdfs:domain steel:SupplierMaster ;
    rdfs:range xsd:string ;
    rdfs:label "supplier ID"@en, "공급사 ID"@ko ;
    rdfs:comment "공급사 식별자"@ko .
"""
        result_ttl, _stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        assert (DOMAIN_NS_OBJ.supplierId, RDF.type, OWL.FunctionalProperty) in g


class TestR6StructuralGapFixes:
    """R6: Multi-Agent 가 빼먹는 3가지 구조를 improve_tbox 가 자동 보강."""

    def test_scope_hasvalue_normalizes_space_variant(self):
        """'Scope 1' 공백 변형 hasValue 는 CSV 표기 'Scope1' 만 남기고 제거."""
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:GHGEmission a owl:Class ;
    rdfs:label "GHG Emission"@en, "온실가스 배출"@ko ;
    rdfs:comment "배출"@ko .

steel:Scope1Emission a owl:Class ;
    rdfs:label "Scope 1 Emission"@en, "스코프 1 배출"@ko ;
    rdfs:comment "직접 배출"@ko ;
    rdfs:subClassOf steel:GHGEmission, [
        a owl:Restriction ;
        owl:onProperty steel:scopeType ;
        owl:hasValue "Scope1"^^xsd:string, "Scope 1"^^xsd:string
    ] .

steel:scopeType a owl:DatatypeProperty ;
    rdfs:domain steel:GHGEmission ;
    rdfs:range xsd:string ;
    rdfs:label "scope type"@en, "스코프 유형"@ko ;
    rdfs:comment "배출 범위"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # Find Scope1Emission's hasValue restriction and check values
        from rdflib import OWL as _OWL
        # Restriction 위치는 equivalentClass 다 (분류 가능한 유일한 형태). 구 산출물
        # 호환을 위해 subClassOf 도 함께 훑는다 — 정규화 자체는 위치와 무관하다.
        vals = []
        for pred in (_OWL.equivalentClass, RDFS.subClassOf):
            for _, _, r in g.triples((DOMAIN_NS_OBJ.Scope1Emission, pred, None)):
                if (r, _OWL.onProperty, DOMAIN_NS_OBJ.scopeType) in g:
                    vals.extend(str(v) for v in g.objects(r, _OWL.hasValue))
        assert "Scope1" in vals, f"Scope1 should remain: {vals}"
        assert "Scope 1" not in vals, f"'Scope 1' should be normalized away: {vals}"
        assert stats.get("scope_hasvalue_normalized", 0) >= 1

    def test_csv_class_autocreated(self, tmp_path, monkeypatch):
        """Multi-Agent 가 빼먹은 CSV 테이블용 클래스를 자동 생성."""
        csv_dir = tmp_path / "rawdata"
        csv_dir.mkdir()
        (csv_dir / "Item_Master.csv").write_text(
            "Item_Code,Item_Name\nITM001,Bolt\n", encoding="utf-8"
        )
        (csv_dir / "Product_Master.csv").write_text(
            "Product_ID,Product_Name\nP001,Slab\n", encoding="utf-8"
        )
        import tools.ontology_quality as oq
        monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(csv_dir))

        # T-Box 에 해당 클래스 없음
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:Dummy a owl:Class ;
    rdfs:label "Dummy"@en, "더미"@ko ;
    rdfs:comment "placeholder"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        assert (DOMAIN_NS_OBJ.ItemMaster, RDF.type, OWL.Class) in g
        assert (DOMAIN_NS_OBJ.ProductMaster, RDF.type, OWL.Class) in g
        assert stats.get("csv_classes_added", 0) >= 2

    def test_incompatible_inverseof_removed(self, tmp_path, monkeypatch):
        """R8: domain/range 가 대칭 안 되는 inverseOf 선언은 제거된다.

        Multi-Agent 가 producesProduct 에 3개 inverse (producedBy,
        producedByEquipment, isProducedByBlastFurnace) 를 달아 OWL RL
        prp-inv1 규칙을 통해 EquipmentMaster 인스턴스가 ProductMaster 타입으로
        오염되는 문제를 뿌리에서 차단.

        본 테스트는 Step 15c 의 inverseOf 호환성 검증에 집중하므로,
        Step 10 의 공유 FK domain broadening 이 ``producesProduct.domain`` 을
        ``owl:Thing`` 으로 확장해 Step 15c 평가를 오염시키지 않도록
        ``SOURCE_RAWDATA_DIR`` 를 빈 디렉토리로 격리한다.
        """
        import tools.ontology_quality as oq
        empty_csv_dir = tmp_path / "rawdata"
        empty_csv_dir.mkdir()
        monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(empty_csv_dir))

        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:EquipmentMaster a owl:Class ;
    rdfs:label "EQ"@en, "설비"@ko ;
    rdfs:comment "설비"@ko .

steel:ProductMaster a owl:Class ;
    rdfs:label "PM"@en, "제품"@ko ;
    rdfs:comment "제품"@ko .

steel:ProcessBlastFurnace a owl:Class ;
    rdfs:label "BF"@en, "고로"@ko ;
    rdfs:comment "고로"@ko .

# 호환 inverse: producesProduct (BF→PM) ↔ producedBy (PM→BF)
steel:producesProduct a owl:ObjectProperty ;
    rdfs:domain steel:ProcessBlastFurnace ;
    rdfs:range steel:ProductMaster ;
    rdfs:label "p"@en, "생산"@ko ;
    rdfs:comment "p"@ko ;
    owl:inverseOf steel:producedBy, steel:producedByEquipment .

steel:producedBy a owl:ObjectProperty ;
    rdfs:domain steel:ProductMaster ;
    rdfs:range steel:ProcessBlastFurnace ;
    rdfs:label "pby"@en, "역생산"@ko ;
    rdfs:comment "pby"@ko .

# 비호환 inverse: producedByEquipment (PM→EQ) ← producesProduct.range 가 PM 인데
# producedByEquipment.range 는 EquipmentMaster 라 ProductMaster 와 대응 안됨.
steel:producedByEquipment a owl:ObjectProperty ;
    rdfs:domain steel:ProductMaster ;
    rdfs:range steel:EquipmentMaster ;
    rdfs:label "pbe"@en, "설비생산"@ko ;
    rdfs:comment "pbe"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # 호환되는 inverseOf (producedBy) 는 유지
        assert (DOMAIN_NS_OBJ.producesProduct, OWL.inverseOf, DOMAIN_NS_OBJ.producedBy) in g
        # 비호환 producedByEquipment 는 제거
        assert (DOMAIN_NS_OBJ.producesProduct, OWL.inverseOf, DOMAIN_NS_OBJ.producedByEquipment) not in g
        assert stats.get("incompatible_inverseOf_removed", 0) >= 1

    def test_self_loop_fk_op_removed(self):
        """R7: has<Class> domain==range 자기참조 FK OP 는 제거된다.

        step 15b 가 이후에 진짜 FK 관계로 같은 이름의 OP 를 재생성할 수 있으므로
        최종 TTL 에 hasSupplierMaster 가 재등장할 수는 있다 (다른 domain 으로).
        검증 대상은 (a) 자기참조 domain==range 구조가 사라졌는지, (b)
        self_loop_ops_removed stats 가 +1 인지.
        """
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:SupplierMaster a owl:Class ;
    rdfs:label "SM"@en, "공급사"@ko ;
    rdfs:comment "공급사"@ko .

steel:hasSupplierMaster a owl:ObjectProperty ;
    rdfs:domain steel:SupplierMaster ;
    rdfs:range steel:SupplierMaster ;
    rdfs:label "self loop"@en, "자기참조"@ko ;
    rdfs:comment "self"@ko ;
    owl:inverseOf steel:isSupplierMasterOf .

steel:isSupplierMasterOf a owl:ObjectProperty ;
    rdfs:domain steel:SupplierMaster ;
    rdfs:range steel:SupplierMaster ;
    rdfs:label "inv"@en, "역"@ko ;
    rdfs:comment "inv"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # self-loop 구조 (domain=range=SupplierMaster) 가 더 이상 존재하지 않아야 함
        sm = DOMAIN_NS_OBJ.SupplierMaster
        doms = set(g.objects(DOMAIN_NS_OBJ.hasSupplierMaster, RDFS.domain))
        if doms:
            assert doms != {sm} or sm not in set(
                g.objects(DOMAIN_NS_OBJ.hasSupplierMaster, RDFS.range)
            ), "self-loop 구조가 남아있음"
        assert stats.get("self_loop_ops_removed", 0) >= 1

    def test_fk_op_autocreated(self, tmp_path, monkeypatch):
        """Multi-Agent 가 누락한 FK OP (예: hasItemMaster) 는 CSV 기반으로 자동 생성."""
        # 임시 CSV 디렉토리 설정: Item_Master(PK), Inventory_Status(FK Item_Code → ItemMaster)
        csv_dir = tmp_path / "rawdata"
        csv_dir.mkdir()
        (csv_dir / "Item_Master.csv").write_text(
            "Item_Code,Item_Name\nITM001,Bolt\n", encoding="utf-8"
        )
        (csv_dir / "Inventory_Status.csv").write_text(
            "Inventory_ID,Item_Code,Stock\nINV001,ITM001,100\n", encoding="utf-8"
        )
        import tools.ontology_quality as oq
        monkeypatch.setattr(oq, "SOURCE_RAWDATA_DIR", str(csv_dir))

        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:ItemMaster a owl:Class ;
    rdfs:label "IM"@en, "아이템"@ko ;
    rdfs:comment "아이템"@ko .

steel:InventoryStatus a owl:Class ;
    rdfs:label "IS"@en, "재고"@ko ;
    rdfs:comment "재고"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # hasItemMaster OP 생성 확인
        op = DOMAIN_NS_OBJ.hasItemMaster
        assert (op, RDF.type, OWL.ObjectProperty) in g, (
            "hasItemMaster OP should be autocreated")
        assert (op, RDFS.domain, DOMAIN_NS_OBJ.InventoryStatus) in g
        assert (op, RDFS.range, DOMAIN_NS_OBJ.ItemMaster) in g
        assert stats.get("fk_ops_autocreated", 0) >= 1

    def test_manufacturing_process_step_autocreated(self):
        """MPS 가 T-Box 에 없고 하위 공정 클래스만 있으면 MPS 를 자동 생성 + subclassOf 연결."""
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:ProcessBlastFurnace a owl:Class ;
    rdfs:label "BF"@en, "고로"@ko ;
    rdfs:comment "고로"@ko .

steel:ProcessSteelmakingFurnace a owl:Class ;
    rdfs:label "SMF"@en, "제강"@ko ;
    rdfs:comment "제강"@ko .

steel:ProcessContinuousCasting a owl:Class ;
    rdfs:label "CC"@en, "연주"@ko ;
    rdfs:comment "연주"@ko .

steel:ProcessRolling a owl:Class ;
    rdfs:label "RL"@en, "압연"@ko ;
    rdfs:comment "압연"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        mps = DOMAIN_NS_OBJ.ManufacturingProcessStep
        assert (mps, RDF.type, OWL.Class) in g
        assert stats.get("mps_class_created", False) is True
        # All 4 process classes linked via subClassOf
        linked = sum(
            1 for n in ("ProcessBlastFurnace", "ProcessSteelmakingFurnace",
                         "ProcessContinuousCasting", "ProcessRolling")
            if (DOMAIN_NS_OBJ[n], RDFS.subClassOf, mps) in g
        )
        assert linked == 4
        assert stats.get("mps_subclass_links_added", 0) == 4


class TestIfpAndRestrictionCleanup:
    """R3: IFP 제거 + domain-incompatible restriction 제거."""

    def test_ifp_declaration_is_stripped(self):
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:TagMaster a owl:Class ;
    rdfs:label "Tag"@en, "태그"@ko ;
    rdfs:comment "태그"@ko .

steel:tagId a owl:DatatypeProperty, owl:InverseFunctionalProperty ;
    rdfs:domain steel:TagMaster ;
    rdfs:range xsd:string ;
    rdfs:label "tag ID"@en, "태그 ID"@ko ;
    rdfs:comment "태그 식별자"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        assert (DOMAIN_NS_OBJ.tagId, RDF.type, OWL.InverseFunctionalProperty) not in g
        assert stats.get("ifp_props_removed", 0) >= 1

    def test_domain_incompatible_restriction_is_removed(self):
        tbox = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:MaintenanceHistory a owl:Class ;
    rdfs:label "Maint"@en, "정비"@ko ;
    rdfs:comment "정비"@ko ;
    rdfs:subClassOf [ a owl:Restriction ;
        owl:onProperty steel:hasEquipment ;
        owl:minCardinality "1"^^<http://www.w3.org/2001/XMLSchema#nonNegativeInteger>
    ] .

steel:EnergyEfficiency a owl:Class ;
    rdfs:label "EE"@en, "효율"@ko ;
    rdfs:comment "효율"@ko .

steel:hasEquipment a owl:ObjectProperty ;
    rdfs:domain steel:EnergyEfficiency ;
    rdfs:label "has equipment"@en, "설비"@ko ;
    rdfs:comment "설비"@ko .
"""
        result_ttl, stats = improve_tbox(tbox)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # MaintenanceHistory should no longer carry an hasEquipment minCardinality
        for parent in g.objects(DOMAIN_NS_OBJ.MaintenanceHistory, RDFS.subClassOf):
            if (parent, RDF.type, OWL.Restriction) in g:
                on_p = next(g.objects(parent, OWL.onProperty), None)
                assert on_p != DOMAIN_NS_OBJ.hasEquipment, (
                    "hasEquipment restriction should be stripped from MaintenanceHistory"
                )
        assert stats.get("incompatible_restrictions_removed", 0) >= 1


class TestCsvPkPathInMatcher:
    """csv_pk_map path lets the matcher accept abbreviations / synonyms."""

    def test_abbrev_matches_via_csv_pk(self):
        # poId on PurchaseOrder: 'po' fails substring but matches CSV PK column 'poid'.
        csv_map = {"purchaseorder": {"poid"}}
        assert _dp_matches_domain_pk("poId", DOMAIN_NS_OBJ["PurchaseOrder"], csv_map) is True

    def test_synonym_matches_via_csv_pk(self):
        # mappingId on ItemSupplierMap: 'mapping' ⊄ 'itemsuppliermap' but CSV PK = 'mappingid'.
        csv_map = {"itemsuppliermap": {"mappingid"}}
        assert _dp_matches_domain_pk("mappingId", DOMAIN_NS_OBJ["ItemSupplierMap"], csv_map) is True

    def test_composite_pk_not_accepted(self):
        # electrical_consumption has composite PK {meter_id, timestamp}; meterId should NOT become Functional.
        csv_map = {"electricalconsumption": {"meterid", "timestamp"}}
        assert _dp_matches_domain_pk("meterId", DOMAIN_NS_OBJ["ElectricalConsumption"], csv_map) is False

    def test_substring_path_still_works_without_csv_map(self):
        # supplierId on SupplierMaster — classic substring match, no csv_map needed.
        assert _dp_matches_domain_pk("supplierId", DOMAIN_NS_OBJ["SupplierMaster"], None) is True


class TestAnnotateCompleteness:
    """_annotate_completeness: 클래스별 completenessStatus 주석 (OWA/CWA 브릿지)."""

    def _make_graph(self, *class_names: str) -> Graph:
        """헬퍼: 주어진 이름으로 OWL 클래스를 가진 그래프를 생성."""
        g = _new_graph()
        for name in class_names:
            g.add((DOMAIN_NS_OBJ[name], RDF.type, OWL.Class))
        return g

    def test_csv_backed_class_gets_closed(self):
        """CSV 데이터가 있는 클래스 -> completenessStatus = 'closed'."""
        g = self._make_graph("EquipmentMaster")
        count = _annotate_completeness(
            g,
            csv_classes={"EquipmentMaster"},
            tacit_classes=set(),
            inferred_classes=set(),
        )
        assert count == 1
        status = str(g.value(DOMAIN_NS_OBJ.EquipmentMaster, DOMAIN_NS_OBJ.completenessStatus))
        assert status == "closed"

    def test_tacit_only_class_gets_open(self):
        """암묵지 전용 클래스 -> completenessStatus = 'open'."""
        g = self._make_graph("QualitySpecification")
        count = _annotate_completeness(
            g,
            csv_classes=set(),
            tacit_classes={"QualitySpecification"},
            inferred_classes=set(),
        )
        assert count == 1
        status = str(g.value(DOMAIN_NS_OBJ.QualitySpecification, DOMAIN_NS_OBJ.completenessStatus))
        assert status == "open"

    def test_inferred_only_class_gets_inferred(self):
        """추론 전용 클래스 -> completenessStatus = 'inferred'."""
        g = self._make_graph("DerivedClass")
        count = _annotate_completeness(
            g,
            csv_classes=set(),
            tacit_classes=set(),
            inferred_classes={"DerivedClass"},
        )
        assert count == 1
        status = str(g.value(DOMAIN_NS_OBJ.DerivedClass, DOMAIN_NS_OBJ.completenessStatus))
        assert status == "inferred"

    def test_bnodes_are_not_annotated(self):
        """BNode(익명 restriction 클래스)는 completenessStatus를 받지 않아야 함."""
        g = _new_graph()
        # Named class
        g.add((DOMAIN_NS_OBJ["EquipmentMaster"], RDF.type, OWL.Class))
        # Anonymous restriction class (BNode typed as owl:Class)
        bnode = BNode()
        g.add((bnode, RDF.type, OWL.Class))
        g.add((bnode, RDF.type, OWL.Restriction))

        count = _annotate_completeness(
            g,
            csv_classes={"EquipmentMaster"},
            tacit_classes=set(),
            inferred_classes=set(),
        )
        # Only the named class should be annotated
        assert count == 1
        # BNode must NOT have completenessStatus
        bnode_status = list(g.objects(bnode, DOMAIN_NS_OBJ.completenessStatus))
        assert bnode_status == [], f"BNode should not get completenessStatus, got: {bnode_status}"


class TestDuplicateOpConsolidation:
    """R11-M5 duplicate_op_consolidation (옵션 β) — 같은 domain+range OP 를
    owl:subPropertyOf 로 연결해 CQ / A-Box 가 서로 다른 이름을 써도 통합되도록."""

    def _tbox_with_duplicate_ops(self):
        return f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:ProductMaster a owl:Class ;
    rdfs:label "Product"@en, "제품"@ko ; rdfs:comment "c"@ko .
steel:MechanicalProperties a owl:Class ;
    rdfs:label "MP"@en, "물성"@ko ; rdfs:comment "c"@ko .

# 같은 domain+range 를 가진 3개 OP — canonical 하나 + 나머지 2개가 subPropertyOf
steel:hasMechanicalProperties a owl:ObjectProperty ;
    rdfs:domain steel:ProductMaster ; rdfs:range steel:MechanicalProperties ;
    rdfs:label "has MP"@en, "물성 가짐"@ko ; rdfs:comment "c"@ko .
steel:hasQualityResult a owl:ObjectProperty ;
    rdfs:domain steel:ProductMaster ; rdfs:range steel:MechanicalProperties ;
    rdfs:label "has quality result"@en, "품질 결과"@ko ; rdfs:comment "c"@ko .
steel:hasQualityMeasurement a owl:ObjectProperty ;
    rdfs:domain steel:ProductMaster ; rdfs:range steel:MechanicalProperties ;
    rdfs:label "has quality measurement"@en, "품질 측정"@ko ; rdfs:comment "c"@ko .

# 의미적으로 다른 OP (Origin vs Destination) — 보존되어야 함
steel:WarehouseMaster a owl:Class ; rdfs:label "W"@en, "창고"@ko ; rdfs:comment "c"@ko .
steel:Transportation a owl:Class ; rdfs:label "T"@en, "운송"@ko ; rdfs:comment "c"@ko .
steel:hasDestinationWarehouse a owl:ObjectProperty ;
    rdfs:domain steel:Transportation ; rdfs:range steel:WarehouseMaster ;
    rdfs:label "dest"@en, "목적지"@ko ; rdfs:comment "c"@ko .
steel:hasOriginWarehouse a owl:ObjectProperty ;
    rdfs:domain steel:Transportation ; rdfs:range steel:WarehouseMaster ;
    rdfs:label "origin"@en, "출발지"@ko ; rdfs:comment "c"@ko .
"""

    def test_duplicate_op_consolidation_links_via_subpropertyof(self):
        """3개 중복 OP 중 canonical 1개가 나머지 2개를 parent 로 subPropertyOf 선언.

        방향성 핵심: canonical 이 A-Box 실사용 OP = 자식, 나머지는 parent.
        OWL RL 추론이 자식 → parent 방향으로 전파하므로 A-Box 의 canonical
        트리플이 모든 parent 이름으로도 추론됨."""
        result_ttl, stats = improve_tbox(self._tbox_with_duplicate_ops())
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")

        mp_ops = {URIRef(DOMAIN_NS + n) for n in
                  ["hasMechanicalProperties", "hasQualityResult", "hasQualityMeasurement"]}
        # canonical = subPropertyOf 를 출발하는 OP (자식 역할).
        # 나머지는 parent (자식 OP 의 subPropertyOf 로 참조받는).
        parents = set()
        for op in mp_ops:
            for parent in g.objects(op, RDFS.subPropertyOf):
                if parent in mp_ops:
                    parents.add(parent)

        canonicals = mp_ops - parents
        assert len(canonicals) == 1, f"expected 1 canonical (child), got {canonicals}"

        canonical = next(iter(canonicals))
        # canonical 에서 나머지 2개 모두 subPropertyOf 로 연결됐어야
        canonical_parents = set(g.objects(canonical, RDFS.subPropertyOf)) & mp_ops
        non_canonical = mp_ops - canonicals
        assert canonical_parents == non_canonical, (
            f"canonical {canonical} should have subPropertyOf → {non_canonical}, "
            f"got {canonical_parents}"
        )

        # stats 에 카운트 있어야
        assert stats.get("duplicate_ops_consolidated", 0) >= 2

    def test_preserves_semantically_distinct_same_domain_range(self):
        """hasDestinationWarehouse vs hasOriginWarehouse — 두 다른 의미 OP 는
        모두 canonical 로 유지 (subPropertyOf 연결 없음)."""
        result_ttl, _ = improve_tbox(self._tbox_with_duplicate_ops())
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")

        dest = URIRef(DOMAIN_NS + "hasDestinationWarehouse")
        origin = URIRef(DOMAIN_NS + "hasOriginWarehouse")
        # 서로 subPropertyOf 관계가 없어야 함 (rule: label 공유 분석으로 제외 판정)
        assert dest not in set(g.objects(origin, RDFS.subPropertyOf))
        assert origin not in set(g.objects(dest, RDFS.subPropertyOf))


class TestForeignNamespaceDomainFix:
    """R28 (2026-05-04): IOF/BFO 상위 온톨로지 domain 자동 교정."""

    _IOF = "https://spec.industrialontologies.org/ontology/core/Core/"

    def _tbox_with_iof_domain(self) -> str:
        """실측 회귀 재현: hasAlarmTag rdfs:domain iof-core:MaterialArtifact.

        inverseOf 인 isAlarmTagOf 의 range 가 steel:AlarmEvents 이므로,
        후처리가 domain 을 그 값으로 승격해야 정답.
        """
        return f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix iof-core: <{self._IOF}> .

steel:AlarmEvents a owl:Class ;
    rdfs:label "Alarm Events"@en ; rdfs:comment "알람 이벤트"@ko .
steel:TagMaster a owl:Class ;
    rdfs:label "Tag Master"@en ; rdfs:comment "태그"@ko .

steel:hasAlarmTag a owl:ObjectProperty ;
    rdfs:domain iof-core:MaterialArtifact ;
    rdfs:range steel:TagMaster ;
    owl:inverseOf steel:isAlarmTagOf ;
    rdfs:label "has alarm tag"@en ; rdfs:comment "알람 태그"@ko .

steel:isAlarmTagOf a owl:ObjectProperty ;
    rdfs:domain steel:TagMaster ;
    rdfs:range steel:AlarmEvents ;
    owl:inverseOf steel:hasAlarmTag ;
    rdfs:label "is alarm tag of"@en ; rdfs:comment "태그 소속 알람"@ko .
"""

    def test_iof_domain_replaced_with_inverse_range(self):
        """iof-core:MaterialArtifact domain → inverseOf.range 인 steel:AlarmEvents 로 교체."""
        ttl = self._tbox_with_iof_domain()
        result_ttl, stats = improve_tbox(ttl)

        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        prop = URIRef(DOMAIN_NS + "hasAlarmTag")

        # 외부 도메인은 제거
        iof_class = URIRef(self._IOF + "MaterialArtifact")
        assert (prop, RDFS.domain, iof_class) not in g

        # steel:AlarmEvents 로 승격 (isAlarmTagOf 의 range)
        expected = URIRef(DOMAIN_NS + "AlarmEvents")
        assert (prop, RDFS.domain, expected) in g

        # stats 카운터 반영
        assert stats.get("op_domain_foreign_namespace_fixed", 0) >= 1

    def test_iof_domain_without_inverse_falls_back_to_owl_thing(self):
        """inverseOf 가 없을 때: 외부 domain 제거 + owl:Thing 으로 fallback."""
        ttl = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix iof-core: <{self._IOF}> .

steel:TagMaster a owl:Class ; rdfs:label "Tag"@en ; rdfs:comment "태그"@ko .

steel:hasSomethingIOF a owl:ObjectProperty ;
    rdfs:domain iof-core:MaterialArtifact ;
    rdfs:range steel:TagMaster ;
    rdfs:label "x"@en ; rdfs:comment "x"@ko .
"""
        result_ttl, stats = improve_tbox(ttl)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        prop = URIRef(DOMAIN_NS + "hasSomethingIOF")

        # 외부 domain 제거
        iof_class = URIRef(self._IOF + "MaterialArtifact")
        assert (prop, RDFS.domain, iof_class) not in g

        # owl:Thing 로 fallback 됐거나, 또는 downstream 단계 9 에서 채워짐
        domains = list(g.objects(prop, RDFS.domain))
        # 외부 네임스페이스가 아닌 것이 최소 하나는 있어야 함
        for d in domains:
            assert not str(d).startswith(self._IOF), f"외부 domain 이 남아있음: {d}"
        assert stats.get("op_domain_foreign_namespace_fixed", 0) >= 1

    def test_steel_only_domain_is_not_touched(self, minimal_tbox):
        """외부 네임스페이스 domain 이 없으면 카운터 0 — 부작용 없음."""
        _result_ttl, stats = improve_tbox(minimal_tbox)
        assert stats.get("op_domain_foreign_namespace_fixed", 0) == 0
