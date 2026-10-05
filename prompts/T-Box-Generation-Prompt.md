# T-Box generation prompt

This prompt is assembled from the six files in `tbox-prompt-modules/`.

## Governing principles

1. Model from source evidence and reviewed domain meaning.
2. Prefer useful relationships over class-count inflation.
3. Follow OWL and IOF semantics, not target counts.
4. Enforce prohibited patterns strictly and keep other design choices
   evidence-based.

## Modules

## [Module 01] Role and task
<!-- 01-role-and-task.md 내용이 여기에 삽입됨 -->

## [Module 02] Turtle output contract
<!-- 02-ttl-guidelines.md 내용이 여기에 삽입됨 -->

## [Module 03] Class definitions
<!-- 03-class-definitions.md 내용이 여기에 삽입됨 -->

## [Module 04] Property rules
<!-- 04-property-rules.md 내용이 여기에 삽입됨 -->

## [Module 05] Quality axioms
<!-- 05-quality-axioms.md 내용이 여기에 삽입됨 -->

## [Module 06] Final checklist
<!-- 06-checklist.md 내용이 여기에 삽입됨 -->

## Prohibited patterns

- Do not combine `owl:TransitiveProperty` with `owl:inverseOf`.
- Do not use pairwise `owl:disjointWith`; use `owl:AllDisjointClasses`.
- Do not declare multiple `rdfs:domain` values on one property.
- Do not create programming-interface classes such as `Measurable` or
  `TimestampedRecord`.
- Do not insert `{ns_prefix}:ClassName` code into natural-language comments.

Generate a complete T-Box from the supplied schema and context.
