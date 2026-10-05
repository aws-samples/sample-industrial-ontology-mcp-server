# Class definitions

Map table names to PascalCase classes in the `{ns_prefix}:` namespace. Follow
any explicit table-to-class mapping supplied with the current chunk.

Align classes to appropriate IOF or BFO categories:

| Source concept | Typical parent |
|---|---|
| Equipment or physical object | `iof-core:MaterialArtifact` |
| State | `iof-core:MaterialState` |
| Resource | `iof-core:MaterialResource` |
| Product | `iof-core:MaterialProduct` |
| Manufacturing process | `iof-core:ManufacturingProcess` |
| Measurement process | `iof-core:MeasurementProcess` |
| Event | `iof-core:Event` |
| Measurement data | `iof-core:MeasurementInformationContentEntity` |
| Maintenance activity | `iof-maint:MaintenanceActivity` |
| Supply-chain agreement | `iof-scro:Agreement` |

Add data-supported value classes and process unions where useful. Every class
must have an English label, a Korean label, and one Korean natural-language
comment. Comments must not contain ontology code.
