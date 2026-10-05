# Third-Party Reference Assets

The repository root license applies only to AWS-authored material. Files in
this directory are third-party assets and remain under their own licenses and
attribution requirements. The root `LICENSE` and `NOTICE` files are managed by
the repository publication workflow and do not replace these terms.

| File | Bytes | License | Source and attribution |
|---|---:|---|---|
| `bfo.owl` | 124,897 | CC BY 4.0 | Basic Formal Ontology 2020, BFO contributors. The file declares `https://creativecommons.org/licenses/by/4.0/` and identifies `https://github.com/bfo-ontology/bfo-2020` as its upstream repository. |
| `Core.rdf` | 355,345 | MIT | Industrial Ontology Foundry Core Ontology. Copyright 2022, 2023, 2024, 2025 Open Applications Group. |
| `Maintenance.rdf` | 44,397 | MIT | Industrial Ontology Foundry Maintenance Reference Ontology. Copyright 2022, 2023, 2024, 2025 Open Applications Group. |
| `SupplyChain.rdf` | 269,510 | MIT | Industrial Ontology Foundry Supply Chain Ontology. Copyright 2023 Open Applications Group. |
| `iof_reference.json` | 84,972 | MIT source material | Derived reference index generated from the three IOF RDF files above. Its `description_en` values are upstream IOF descriptions, so this file is treated as third-party material rather than AWS-authored prose. |

The five tracked files total 879,121 bytes. `Core.rdf` imports the IOF
`AnnotationVocabulary`, but that ontology is not vendored here. When a local
copy is not configured, ontology tooling may resolve that import from the
network.

`bfo.owl` is retained for offline import resolution. The vendored IOF
ontologies import BFO, and removing the local file can turn reasoner execution
into an unexpected network dependency.

See [`THIRD-PARTY-LICENSES.md`](../../../THIRD-PARTY-LICENSES.md) for the
Python dependency inventory, manual scanner corrections, and optional external
products that are not bundled with this repository.
