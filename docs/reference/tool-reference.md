# MCP tool reference

The workshop uses 27 participant-facing tools listed in the
[root README](../../README.md#the-workshop). The server registers additional
tools for pipeline internals, diagnostics, optional stores, and advanced use.

## Common direct requests

| Goal | Tool |
|---|---|
| Inspect source tables | `list_csv_tables`, `read_csv_schema`, `profile_csv_data` |
| Inspect the ontology | `read_tbox`, `analyze_tbox`, `visualize_tbox` |
| Query locally | `sparql_local` |
| Validate a graph | `validate_kg` |
| Inspect state | `check_pipeline_state`, `ontology_agent_health_check` |

## Vocabulary contract

S6.5 calls `generate_semantic_dictionary(include_stats=False)` before A-Box
generation. S7 calls `generate_abox(use_dict_contract=True)`. S10 calls
`generate_semantic_dictionary(include_stats=True)` to add observed statistics.

## FK resolution

`tools/fk_matching.py` resolves a foreign-key value through exact, normalized,
edit-distance, and prefix strategies. `generate_abox` reports counts in
`fk_match_stats`.

## Local and optional remote tools

The main S0 through S13 pipeline runs locally except for configured Bedrock
calls. Neo4j and GraphDB tools are optional. Remote writes require explicit
confirmation and least-privilege non-production credentials.

## Workflow recipes

T-Box update:

```text
read_tbox -> update_tbox_incremental -> improve_tbox_quality -> validation
```

A-Box review:

```text
generate_abox -> validate_ttl_syntax -> validate_kg
```

Query exploration:

```text
read_semantic_dictionary -> sparql_local -> test_domain_queries
```

Tool parameters and return schemas are available from MCP tool descriptions.
