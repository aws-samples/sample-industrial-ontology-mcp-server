# Final checklist

## Classes

- [ ] Every CSV table maps to a class.
- [ ] Every class has an appropriate IOF or BFO parent.
- [ ] Added classes represent real domain categories.
- [ ] No programming-interface classes were introduced.

## ObjectProperties

- [ ] Every source FK is represented.
- [ ] Every property has exactly one domain and one range.
- [ ] Every declared inverse is semantically required and reversed correctly.
- [ ] Every relationship has source or reviewed domain evidence.

## DatatypeProperties

- [ ] Every non-PK and non-FK column is covered.
- [ ] Every property is class-specific.
- [ ] Every property has an XSD range and exact `dcterms:source`.

## Axioms and annotations

- [ ] `someValuesFrom` restrictions satisfy the complete-population rule.
- [ ] Disjointness uses `owl:AllDisjointClasses`.
- [ ] No transitive property is paired with an inverse.
- [ ] Every class and property has required labels and one Korean comment.
