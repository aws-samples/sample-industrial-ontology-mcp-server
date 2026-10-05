# Steel manufacturing sample domain

The checked-in data is synthetic and demonstrates a manufacturing ontology. It
is not affiliated with a manufacturer and contains no production or customer
data.

## Domain areas

- Equipment and maintenance
- Process and production
- Product quality
- Energy and environmental measurements
- Inventory and supply chain

## Namespaces

| Prefix | Purpose |
|---|---|
| `steel:` | Sample T-Box entities |
| `steel-inst:` | Sample instances |
| `iof-core:` | IOF Core references |
| `iof-maint:` | IOF Maintenance references |
| `iof-scro:` | IOF Supply Chain references |

External namespace references remain external. The prefix resolver must not
collapse an IOF IRI into the sample namespace.

## Example competency questions

- Which equipment is associated with a recent alarm?
- Which process records have a quality result outside a selected range?
- Which energy measurements relate to a production step?

Use [the participant workbook](../../workshop/participant-workbook.md) to
explore the sample.
