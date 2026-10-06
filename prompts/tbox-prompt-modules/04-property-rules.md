# Property rules

## ObjectProperties

### ObjectProperty evidence

Create an ObjectProperty **only when supported by a CSV FK**, reviewed tacit
rule, or supplied competency question. An unsupported relationship
produces a schema-only path and misleading zero-row answers.

### ObjectProperty declaration

Every ObjectProperty needs exactly one domain, one range, English and Korean
labels, and one Korean comment. Add an inverse only when a competency question
or required traversal needs that direction. When declared, inverse domain and
range must be reversed.

Use IOF super-properties where semantically valid. Do not infer a relationship
from timestamp equality alone.

### One ObjectProperty per domain and range

Declare at most one ObjectProperty for each `(rdfs:domain, rdfs:range)` pair.
An FK column fills only one property, so a second property on the same pair
stays empty and its queries return zero rows that look like a valid answer.
Declare more than one only when all three conditions hold: the properties run
in opposite directions, each has its own FK column, and each English label
contains its direction word from one of these pairs: origin/destination,
source/target, from/to, input/output, sender/receiver, supplier/buyer, or
precedes/follows. Any other second property on the same pair, such as a
primary and a backup supplier, is treated as a duplicate even when it has its
own FK column. Declare one property for such a pair.

## DatatypeProperties

### Path B naming

Map every non-PK and non-FK source column. This is the **Path B** policy:
class-specific DP names are mandatory. Use:

```text
{{Class}}.{{Column}} -> {{classCamelLower}}{{ColumnCamel}}
```

Examples:

- `EquipmentStatus.Status` -> `equipmentStatusValue`
- `AlarmEvents.Timestamp` -> `alarmEventsTimestamp`
- `ChemicalAnalysis.C_Percent` -> `chemicalAnalysisCarbonPercent`

Never create these generic names: `hasValue`, `hasTemperature`, `hasPressure`,
`hasFlow`, `hasQuantity`, `hasTimestamp`, `hasDate`, `hasTime`, `hasStatus`,
`hasCode`, `hasName`, `hasIdentifier`, `hasId`, `hasUnit`, `hasLocation`,
`hasResult`, `hasSeverity`, `hasPriority`, `hasDescription`, `hasType`,
`hasRate`, or `hasAmount`.

### DatatypeProperty declaration

Every DatatypeProperty requires:

- exactly one class domain
- exactly one XSD range
- English and Korean labels
- one Korean comment
- `dcterms:source` containing the exact CSV header

`rdfs:domain` is required. An absent domain and `rdfs:domain owl:Thing` are
prohibited.

Use `xsd:string` for identifiers and text, `xsd:decimal` for measurements,
`xsd:integer` for counts, `xsd:dateTime` for timestamps, and `xsd:boolean` for
flags.

### DatatypeProperty source column

Copy the CSV header into `dcterms:source` without changing its case,
underscores, or numeric suffix. Each DatatypeProperty names exactly one source
column, and no two DatatypeProperties of the same class name the same column.
Numbered columns such as `_1` and `_2` get separate properties.

### Coverage self-check

Before output, compare each class's declared properties with all source
non-key columns. If any column is missing, add its class-specific property
before returning the Turtle.

## Shared columns

When the same column name appears in three or more classes, create a separate
class-specific DP for each class. An abstract parent plus `subClassOf` can be
used only when the parent represents a real shared domain category.
