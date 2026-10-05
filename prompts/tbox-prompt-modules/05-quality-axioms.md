# Quality axioms

Use axioms only when their semantics are supported by the source.

| Metric | Target | Rule |
|---|---|---|
| Axiom richness | at least 2 per class | Add `someValuesFrom` only under the conditions below |

## Existential restrictions

`someValuesFrom` is a universal participation condition. Add it only when:

1. A real FK column exists in the source schema.
2. The relationship is populated in every row represented by the class.
3. A time column is not used as relationship evidence.

If any condition is uncertain, omit the restriction. Adding an unsupported
restriction to improve a metric is metric gaming.

## Disjointness

Use `owl:AllDisjointClasses` for genuinely disjoint sibling classes. Do not use
pairwise `owl:disjointWith`.

## Defined and union classes

Use `owl:equivalentClass` with `owl:Restriction` for evidence-backed value
classes. Use `owl:unionOf` for a meaningful union class.

## Safety

Do not combine transitivity with inverse properties. Keep one natural-language
comment per entity and avoid code fragments in comments.
