# Turtle output contract

Define these prefixes:

```turtle
@prefix {ns_prefix}: <{class_ns}> .
@prefix {ns_inst_prefix}: <{instance_ns}> .
@prefix iof-core: <https://spec.industrialontologies.org/ontology/core/Core/> .
@prefix iof-maint: <https://spec.industrialontologies.org/ontology/maintenance/Maintenance/> .
@prefix iof-scro: <https://spec.industrialontologies.org/ontology/supplychain/SupplyChain/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
```

Declare the ontology with labels, imports, version IRI, version information,
creator, and license.

The response must start with a `turtle` code fence and end with the matching
fence. Put only Turtle inside the fence. Order content as prefixes, ontology,
classes, ObjectProperties, and DatatypeProperties. End each chunk at a complete
Turtle statement.
