# Property rules

## ObjectProperties

Create an ObjectProperty **only when supported by a CSV FK**, reviewed tacit
rule, or supplied competency question. An unsupported relationship
produces a schema-only path and misleading zero-row answers.

Every ObjectProperty needs exactly one domain, one range, English and Korean
labels, and one Korean comment. Add an inverse only when a competency question
or required traversal needs that direction. When declared, inverse domain and
range must be reversed.

Use IOF super-properties where semantically valid. Do not infer a relationship
from timestamp equality alone.

## DatatypeProperties

Map every non-PK and non-FK source column. This is the **Path B** policy:
class-specific DP names are mandatory. Use:

```text
{{Class}}.{{Column}} -> {{classCamelLower}}{{ColumnCamel}}
```

Examples:

- `EquipmentStatus.Status` -> `equipmentStatusValue`
- `AlarmEvents.Timestamp` -> `alarmEventsTimestamp`
- `ChemicalAnalysis.C_Percent` -> `chemicalAnalysisCarbonPercent`

Do not create generic names such as `hasValue`, `hasTemperature`, `hasStatus`,
`hasTimestamp`, `hasIdentifier`, `hasQuantity`, `hasUnit`, `hasLocation`,
`hasResult`, or `hasSeverity`.

Every DatatypeProperty requires:

- exactly one class domain
- an XSD range
- English and Korean labels
- one Korean comment
- `dcterms:source` containing the exact CSV header

`rdfs:domain` is required. An absent domain and `rdfs:domain owl:Thing` are
prohibited.

Use `xsd:string` for identifiers and text, `xsd:decimal` for measurements,
`xsd:integer` for counts, `xsd:dateTime` for timestamps, and `xsd:boolean` for
flags.

Before output, compare each class's declared properties with all source
non-key columns. If any column is missing, add its class-specific property
before returning the Turtle.

## Shared columns

When the same column name appears in three or more classes, create a separate
class-specific DP for each class. An abstract parent plus `subClassOf` can be
used only when the parent represents a real shared domain category.
