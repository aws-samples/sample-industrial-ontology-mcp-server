# Pre-generated workshop artifacts

These files provide the workshop fallback when model-assisted generation is
unavailable.

| File | Purpose |
|---|---|
| `t_box.ttl` | Sample ontology schema |
| `a_box.ttl.gz` | Compressed sample instance graph |
| `all_inferred.ttl.gz` | Compressed inferred graph |
| `semantic_dictionary.json` | Query vocabulary contract |
| `tbox_visualization.html` | T-Box review view |
| `query_test_report.html` | Query test report |

The A-Box is intentionally compressed. Compression reduces the tracked file
from 37,996,777 bytes to about 2 MB, but it also removes the instance text from
ordinary secret scanners and casual human review. The release gate therefore
checks the decompressed bytes.

Expected SHA-256 after decompressing `a_box.ttl.gz`:

```text
8a448d45aa09d87674b41d3d928b892a758650eebb01af415530e5c469a17b30
```

Verify the complete package:

```bash
python scripts/verify_workshop_sparql.py --ignore-placeholders
```
