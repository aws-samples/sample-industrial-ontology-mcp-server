"""Tests for owl:hasKey injection in improve_tbox (Step 27).

Business keys from CSV primary-key detection are promoted into OWL axioms
so that the reasoner can enforce instance uniqueness at the T-Box level,
complementing the DP-side FunctionalProperty declarations. Composite PKs
become multi-property keys; tables without a detectable PK are skipped.
"""
from __future__ import annotations

from rdflib import OWL, Graph, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS
from tools.ontology_quality import _inject_owl_haskey


def _build_graph(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def _has_key_properties(g: Graph, cls_uri: URIRef) -> list[URIRef]:
    """Return the ordered property URIs declared as owl:hasKey for cls_uri."""
    key_lists = list(g.objects(cls_uri, OWL.hasKey))
    if not key_lists:
        return []
    return list(Collection(g, key_lists[0]))


class TestHasKeySingleColumn:
    def test_single_column_pk_emits_haskey(self):
        # EquipmentMaster has a single-column PK (equipmentId) in the CSV.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:EquipmentMaster a owl:Class .
        steel:equipmentId a owl:DatatypeProperty ;
            rdfs:domain steel:EquipmentMaster .
        """
        g = _build_graph(ttl)
        pk_map = {"equipmentmaster": {"equipmentid"}}
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        keys = _has_key_properties(g, URIRef(f"{DOMAIN_NS}EquipmentMaster"))
        assert keys == [URIRef(f"{DOMAIN_NS}equipmentId")]
        assert stats["haskey_added"] == 1


class TestHasKeyCompositeColumn:
    def test_composite_pk_emits_multi_property_haskey(self):
        # InventoryStatus has a composite PK: (itemCode, warehouseCode, zoneCode).
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:InventoryStatus a owl:Class .
        steel:itemCode a owl:DatatypeProperty ;
            rdfs:domain steel:InventoryStatus .
        steel:warehouseCode a owl:DatatypeProperty ;
            rdfs:domain steel:InventoryStatus .
        steel:zoneCode a owl:DatatypeProperty ;
            rdfs:domain steel:InventoryStatus .
        """
        g = _build_graph(ttl)
        pk_map = {
            "inventorystatus": {"itemcode", "warehousecode", "zonecode"},
        }
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        keys = _has_key_properties(g, URIRef(f"{DOMAIN_NS}InventoryStatus"))
        # Composite key must include all PK components (order-agnostic check).
        assert set(keys) == {
            URIRef(f"{DOMAIN_NS}itemCode"),
            URIRef(f"{DOMAIN_NS}warehouseCode"),
            URIRef(f"{DOMAIN_NS}zoneCode"),
        }
        assert stats["haskey_added"] == 1


class TestHasKeyIdempotent:
    def test_existing_haskey_not_duplicated(self):
        # When run twice, the second run must not append a second list.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:EquipmentMaster a owl:Class .
        steel:equipmentId a owl:DatatypeProperty ;
            rdfs:domain steel:EquipmentMaster .
        """
        g = _build_graph(ttl)
        pk_map = {"equipmentmaster": {"equipmentid"}}
        _inject_owl_haskey(g, csv_pk_map=pk_map)
        stats2 = _inject_owl_haskey(g, csv_pk_map=pk_map)
        # Second pass should be a no-op at the Class level.
        key_lists = list(g.objects(URIRef(f"{DOMAIN_NS}EquipmentMaster"), OWL.hasKey))
        assert len(key_lists) == 1
        assert stats2["haskey_added"] == 0
        assert stats2["haskey_skipped_existing"] >= 1


class TestHasKeySkipWhenMissingProperty:
    def test_skip_when_pk_property_absent_from_tbox(self):
        # Class declared but the DP for the PK column is missing — we can't
        # emit hasKey because rdflib would still accept it syntactically but
        # it would reference an undeclared property, which defeats the purpose.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        steel:EquipmentMaster a owl:Class .
        """
        g = _build_graph(ttl)
        pk_map = {"equipmentmaster": {"equipmentid"}}
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        keys = list(g.objects(URIRef(f"{DOMAIN_NS}EquipmentMaster"), OWL.hasKey))
        assert keys == []
        assert stats["haskey_added"] == 0
        assert stats["haskey_skipped_missing_dp"] >= 1


class TestHasKeySkipWhenNoClassInGraph:
    def test_class_not_in_graph_noop(self):
        # CSV table exists on disk but the class never made it into the T-Box.
        # hasKey must not be emitted because it would reference a non-existent class.
        ttl = f"""
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix steel: <{DOMAIN_NS}> .
        steel:SomeUnrelatedClass a owl:Class .
        """
        g = _build_graph(ttl)
        pk_map = {"equipmentmaster": {"equipmentid"}}
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        assert stats["haskey_added"] == 0


class TestHasKeyForClassSuffixPattern:
    """LLM-generated T-Boxes often rename a shared PK column as
    {col}For{ClassWord}. The resolver must recognise this so classes like
    ChemicalAnalysis (whose sampleId was renamed to sampleIdForChemical)
    still get hasKey.
    """

    def test_suffix_for_pattern_single_column(self):
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:ChemicalAnalysis a owl:Class .
        steel:sampleIdForChemical a owl:DatatypeProperty ;
            rdfs:domain steel:ChemicalAnalysis .
        """
        g = _build_graph(ttl)
        pk_map = {"chemicalanalysis": {"sampleid"}}
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        keys = list(g.objects(URIRef(f"{DOMAIN_NS}ChemicalAnalysis"), OWL.hasKey))
        assert len(keys) == 1
        assert stats["haskey_added"] == 1

    def test_suffix_for_pattern_with_class_alias(self):
        # ChemicalAnalysis → "chemical" portion matches the suffix,
        # not the full class name.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:ProcessBlastFurnace a owl:Class .
        steel:productIdForBlastFurnace a owl:DatatypeProperty ;
            rdfs:domain steel:ProcessBlastFurnace .
        steel:blastFurnaceTimestamp a owl:DatatypeProperty ;
            rdfs:domain steel:ProcessBlastFurnace .
        """
        g = _build_graph(ttl)
        pk_map = {"processblastfurnace": {"productid", "timestamp"}}
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        assert stats["haskey_added"] == 1


class TestHasKeyExternalAncestorDomain:
    """When a DP's rdfs:domain is a non-steel URI (typically an IOF upper-
    ontology class) and the target class is a subClassOf that external
    class, accept — the ancestor restricts membership, the subClass honours it.
    """

    def test_accept_when_domain_is_ancestor_of_target(self):
        ttl = """
        @prefix steel: <http://example.com/steel-ontology#> .
        @prefix iofcore: <https://spec.industrialontologies.org/ontology/core/Core/> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        iofcore:MeasurementProcess a owl:Class .
        steel:MonitoringPointMaster a owl:Class ;
            rdfs:subClassOf iofcore:MeasurementProcess .
        steel:pointId a owl:DatatypeProperty ;
            rdfs:domain iofcore:MeasurementProcess .
        """
        g = _build_graph(ttl)
        pk_map = {"monitoringpointmaster": {"pointid"}}
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        keys = list(g.objects(URIRef(f"{DOMAIN_NS}MonitoringPointMaster"), OWL.hasKey))
        assert len(keys) == 1
        assert stats["haskey_added"] == 1

    def test_reject_when_domain_is_unrelated_class(self):
        # sampleId domain is SoilMonitoring only; ChemicalAnalysis is unrelated.
        # Using it as a key for ChemicalAnalysis would be semantically wrong.
        ttl = f"""
        @prefix steel: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        steel:SoilMonitoring a owl:Class .
        steel:ChemicalAnalysis a owl:Class .
        steel:sampleId a owl:DatatypeProperty ;
            rdfs:domain steel:SoilMonitoring .
        """
        g = _build_graph(ttl)
        pk_map = {"chemicalanalysis": {"sampleid"}}
        stats = _inject_owl_haskey(g, csv_pk_map=pk_map)
        keys = list(g.objects(URIRef(f"{DOMAIN_NS}ChemicalAnalysis"), OWL.hasKey))
        assert keys == []
        assert stats["haskey_added"] == 0
