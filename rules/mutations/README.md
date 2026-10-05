# Mutation Catalog

SPARQL UPDATE templates used by `tools/mutation_runner.py` to seed defects
into a copy of the T-Box for sensitivity testing.

## Format

Each file is a standalone SPARQL UPDATE. The runner treats each file as one
mutator; it applies the UPDATE to a cloned T-Box, then re-runs validators.

Placeholders that the runner fills (by sorted URI, deterministic):

- `?TARGET_PROP` — resolved to a `rdf:Property | owl:DatatypeProperty | owl:ObjectProperty` URI
- `?TARGET_CLASS` — resolved to an `owl:Class` URI (used by M4/M5 mutators)
- `?FOREIGN_CLASS` — resolved to any `owl:Class` URI, intended to be unrelated to the target (used by swap-style mutators; selection semantics are runner-enforced and may evolve — see `tools/mutation_runner.py::_PLACEHOLDER_QUERIES`)

Any other `?var` in a mutator file (e.g. `?cons`, `?prev`, `?dom`) is a local
WHERE-bound variable, NOT a runner placeholder. Do not prefix local variables
with `TARGET_` or `FOREIGN_`.

## Adding a mutator

1. Drop a `.sparql` file in the relevant category directory.
2. Add a fixture case in `tests/fixtures/mutation_minimal_tbox.ttl` that the
   mutator can target.
3. `pytest tests/test_mutation_runner.py::test_catalog_discovers_all_mutators`
   picks it up automatically.

## Categories

| Dir | Target | Expected catchers |
|-----|--------|-------------------|
| M1_domain_range | rdfs:domain / rdfs:range | domain_range_conformance, property_coverage |
| M2_inverse | owl:inverseOf | bidirectional, inference_sanity |
| M3_cardinality | owl:FunctionalProperty, owl:Restriction | functional, cardinality |
| M4_disjoint | owl:AllDisjointClasses, rdfs:subClassOf | disjoint, tbox_fitness |
| M5_annotation | rdfs:label, rdfs:comment | (blind-spot probe — expect 0 catchers) |
| M6_reference | FK OP rdfs:range | fk_referential_integrity, fk_op_coverage |
| M7_restriction | owl:someValuesFrom, owl:onClass | validate_owl_consistency, cardinality |
