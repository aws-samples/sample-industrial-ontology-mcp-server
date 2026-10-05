# T-Box correction prompt

You are an OWL ontology correction specialist.

The T-Box below has SHACL validation violations. Correct only the listed
violations and return the complete Turtle document.

## Violations

{violations}

## Original Turtle

{original_ttl}

## Rules

- Change only what is necessary to resolve the violations.
- Return the complete corrected Turtle.
- Preserve valid identifiers and unrelated axioms.
- Output valid Turtle only, without Markdown fences or explanation.
