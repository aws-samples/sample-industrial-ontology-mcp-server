# Ontology concepts

## Core terms

| Term | Meaning |
|---|---|
| RDF triple | Subject, predicate, object |
| T-Box | Classes, properties, and logical constraints |
| A-Box | Instances and their asserted relationships |
| ObjectProperty | Relationship between resources |
| DatatypeProperty | Literal-valued attribute |
| Competency question | A question the graph is expected to answer |

## T-Box rules

- Map each source table to at least one meaningful class.
- Use one `rdfs:domain` and one `rdfs:range` per property.
- Create ObjectProperties only when source relationships or reviewed domain
  knowledge support them.
- Use class-specific DatatypeProperties such as
  `equipmentStatusValue`, not generic names such as `hasStatus`.
- Add `dcterms:source` with the exact CSV header for each generated
  DatatypeProperty.
- Use `owl:AllDisjointClasses` for sibling disjointness.

## A-Box rules

Primary keys form stable instance identifiers. Foreign-key values resolve to
master instances through exact, normalized, edit-distance, and prefix
strategies. Non-key columns map through the T-Box vocabulary contract.

## Reasoning

OWL RL reasoning can derive inverse relationships, subclass types, equivalent
class membership, and other entailed triples. Open-world semantics means that
missing evidence is not automatically false. Validation and domain review are
therefore separate from reasoning.

## Standards

The sample aligns selected concepts with IOF and BFO. External classes retain
their original namespace. They must not be rewritten into the sample domain
namespace.

Continue with [pipeline details](pipeline-details.md).
