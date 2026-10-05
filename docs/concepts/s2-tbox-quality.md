# S2 T-Box quality

S2 combines model-assisted design with deterministic review. An ontology
architect proposes a T-Box, a semantic validator checks OWL structure, and a
domain reviewer checks whether classes and relationships match the source
evidence and competency questions.

## Design sequence

1. Build class hierarchy and cross-domain structure.
2. Map source tables and foreign keys.
3. Cover every non-key column with a class-specific DatatypeProperty.
4. Add only evidence-backed restrictions.
5. Run deterministic post-processing and validation.

## Quality rules

- A relationship without a source FK or reviewed tacit rule remains a data gap.
- `someValuesFrom` requires a populated relationship for every class member.
- Inverse properties are declared only when queries need the inverse path.
- Generic DatatypeProperties are rejected or renamed according to
  `TBOX_STRICT_CLASS_SPECIFIC`.
- CSV column coverage is measured by `TBOX_COVERAGE_GATE` and
  `TBOX_COVERAGE_THRESHOLD`.

## Validation

S4 runs Turtle syntax, quality checks, OWL consistency, classification, SHACL,
and baseline comparison. Baseline comparison is warning-only because a
deliberate ontology simplification can remove axioms.

See [pipeline details](pipeline-details.md) and
[quality framework](../reference/quality-framework.md).
