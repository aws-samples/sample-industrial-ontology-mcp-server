# Data Classification

This repository is a non-production MCP server sample. The included workshop
is a guided example, and the checked-in manufacturing inputs and their derived
outputs are synthetic. They do not represent production operations or
customer data.

## Tracked Data Inventory

| Asset | Classification | Measured inventory | Basis |
|---|---|---:|---|
| `data/source/rawdata/*.csv` | Synthetic data | 40 files | Workshop inputs created for the manufacturing example, not records from an operating environment. |
| `data/source/reference/` | Third-party ontology material | 5 third-party assets totaling 879,121 bytes | IOF, BFO, and an IOF-derived reference index. These assets retain their upstream licenses and are excluded from implied MIT-0 relicensing. The directory also contains an AWS-authored 1,956-byte attribution README. |
| `data/source/tacit/*.ttl` | Synthetic tacit knowledge | 8 files | Deterministic rule outputs and workshop assertions derived from the synthetic example. |
| `workshop/pre-generated/` | Synthetic derived output, including model-generated ontology content | 7 files totaling 7,058,620 bytes (6.7 MiB) | Pre-generated T-Box, A-Box, inferred graph, semantic dictionary, and reports derived from the synthetic inputs. The T-Box was generated with an Amazon Bedrock model and requires domain-expert review before use. |

The inventory was measured from tracked files for this release:

```bash
git ls-files 'data/source/rawdata/*.csv' | wc -l
git ls-files 'data/source/tacit/*.ttl' | wc -l
git ls-files 'workshop/pre-generated/**' | wc -l
wc -c data/source/reference/{Core.rdf,Maintenance.rdf,SupplyChain.rdf,bfo.owl,iof_reference.json}
git ls-files -z 'workshop/pre-generated/**' | xargs -0 wc -c
```

On 2026-09-10, the checked-in T-Box and inferred graph were manually corrected
without rerunning the model-assisted pipeline. The correction replaced the
ontology's CC BY 4.0 self-declaration with the repository's MIT-0 license URI
and moved internal debate telemetry out of `dcterms:description` into a concise
model-generation provenance statement. The RDF triple counts remained
unchanged, and the domain configuration now emits the same MIT-0 declaration
on the next normal regeneration.

The `Manufacturer` column of `data/source/rawdata/Equipment_Master.csv` uses
the neutral placeholder names `Manufacturer_A` through `Manufacturer_H`. The
checked-in A-Box, inferred graph, and semantic dictionary carry the same
values. They were updated by exact literal replacement, together with the
recorded checksum of that CSV, without rerunning the pipeline, so their RDF
triple counts are unchanged.

## Handling Requirements

- Use only synthetic, public, or explicitly authorized data with this sample.
- Do not commit credentials, customer data, production data, or generated
  outputs from sensitive inputs.
- Review model-assisted and generated artifacts before relying on them.
- Preserve the licenses and attribution documented in
  `data/source/reference/README.md` and `THIRD-PARTY-LICENSES.md`.
- Treat `data/generated/` as local working output. It is ignored by Git and
  excluded from the public release manifest.
