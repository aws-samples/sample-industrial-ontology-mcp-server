# Quality framework

Quality is evaluated across schema, instances, inference, query behavior, and
release evidence. A score is a summary, not proof.

## T-Box

- Turtle syntax
- Required labels, comments, domain, and range
- Class-specific DatatypeProperty policy
- CSV column coverage
- OWL consistency and classification
- SHACL conformance
- Baseline axiom inventory

## A-Box

`validate_kg` groups 25 checks into structural, referential, semantic,
statistical, and cardinality families. Results distinguish pass, fail, skip,
and non-applicable checks.

## Inference

Profile checks, consistency, entailment regression, and inferred-delta review
look for unsupported expansion or lost expected entailments.

## Query behavior

Competency-question tests measure graph connectivity. A schema-only path can
pass without instance rows, so `schema_only_connections` and
`cqs_with_schema_only_pass` require review.

## Evidence rules

- Do not weaken thresholds to improve a score.
- Do not count non-applicable checks as evidence.
- Compare failed node IDs with the recorded baseline.
- Preserve the command, raw output, and measurement conditions.

The repository does not claim an unsupported TAN metric. Metrics retained in
public documentation have an implementation and a reviewable interpretation.
