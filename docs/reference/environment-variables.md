# Environment variables

`.env.example` is a starter configuration. This reference also covers advanced
quality, performance, and diagnostic settings read by the application. Defaults
below apply when the variable is unset; empty strings and accepted boolean
spellings are not uniform across readers.

Set variables before starting the server. [config.py](../../config.py) loads
`DOTENV_PATH` at import without overriding existing environment values. Its
default `.env` is resolved against the process working directory, not the
project root. When launching `server.py` from another directory, set
`DOTENV_PATH` to an absolute path. Several modules capture settings at import,
so editing `.env` does not update an already running server.

When adding a setting or changing its interpretation, update this page and check
both direct readers and helpers that receive variable names:

```bash
rg -n 'getenv\(|environ\.get\(|_safe_int\(|_flag_off\(|_AGENT_MODEL_ENV|_ENV' \
  config.py app.py server.py domain/ tools/
```

Inspect the helper callers, role-to-variable mapping, and module constants such
as `_ENV_MODE` and `_FK_GROUNDING_ENV` too. SDK credential provider variables
are managed by the SDK and are outside this application catalog.

Pipeline artifacts such as the T-Box, A-Box, inferred graph, and
`semantic_dictionary.json` have no path variable of their own. Apart from
`DATA_DIR`, which sets the root of the data tree, the output path variables on
this page cover auxiliary files only: the progress heartbeat file
(`MULTI_AGENT_HEARTBEAT_FILE`), server log (`ONTOLOGY_SERVER_LOG_FILE`), and raw
chunk debug dumps (`TBOX_DEBUG_DUMP_DIR`).
`config.py` derives the input tree (`source/`) and most generated artifacts,
including those pipeline artifacts, from `DATA_DIR` (default
`<project>/data`). Some writers ignore `DATA_DIR` and always use
`<project>/data/generated`, for example `reports/cq_feedback.json`,
`tbox/debate_log.json`, `tbox/compromise_audit.json`, and the default heartbeat
file. Changing `DATA_DIR` therefore splits outputs across two trees.

## AWS and Bedrock

| Variable | Purpose |
|---|---|
| `AWS_REGION` | Default AWS Region for non-Bedrock AWS calls, default `ap-northeast-2` |
| `AWS_PROFILE` | Optional local profile. Unset uses the default credential provider chain |
| `BEDROCK_REGION` | Bedrock Region, default `us-east-1` |
| `BEDROCK_MODEL_ID` | Model or inference profile |
| `BEDROCK_READ_TIMEOUT` | Bedrock client read timeout in seconds, default `300` |
| `BEDROCK_MAX_TOKENS` | Default output token limit for model calls, default `32000` |
| `BEDROCK_MAX_RETRIES` | Default retry count for model calls, default `2` |
| `MULTI_AGENT_MODEL_ARCHITECT`, `MULTI_AGENT_MODEL_VALIDATOR`, `MULTI_AGENT_MODEL_SME`, `MULTI_AGENT_MODEL_JURY`, `MULTI_AGENT_MODEL_COMPROMISE` | Optional per-role model for collaborative T-Box design. An unset role uses `BEDROCK_MODEL_ID` |

## Collaborative T-Box progress

Readers: [multi_agent_tbox.py](../../tools/multi_agent_tbox.py) and
[tbox_generation.py](../../tools/tbox_generation.py).

| Variable | Purpose |
|---|---|
| `MULTI_AGENT_HEARTBEAT_SEC` | Interval in seconds between progress heartbeats during `generate_tbox_collaborative`, default `10`. `0` disables periodic heartbeats |
| `MULTI_AGENT_HEARTBEAT_FILE` | Heartbeat JSON path. Unset writes `<project>/data/generated/_heartbeat.json` regardless of `DATA_DIR`; an empty value disables the file |
| `MULTI_AGENT_DETERMINISM` | `strict` requests temperature `0`, records prompt hashes, and disables parallel Validator/SME execution. Unset keeps exploration behavior; it does not guarantee identical model responses |
| `MULTI_AGENT_PARALLEL` | `true` (default), `1`, or `yes` runs Validator and SME concurrently, unless determinism is `strict` |
| `MULTI_AGENT_NO_PROGRESS_INTERVENE` | `true` (default), `1`, or `yes` enables Jury intervention when rounds stop making progress |
| `S2_CQ_MAX_HOPS` | Maximum ObjectProperty hops in the static CQ reachability check, default `3`; integers below `1` become `1`, invalid integers fall back to `3` |
| `S2_EARLY_STOP` | Enables the data-expressibility early-stop check by default (`on`); `off`, `0`, or `false` disables it |
| `S2_GUARD_NORMALIZE_WITH_S3` | `on` (default), `1`, or `true` compares declarations with S3 normalization when checking draft capability loss |
| `TBOX_DEBUG_DUMP_DIR` | Unset or empty disables dumps. A directory enables raw model TTL response files per chunk, including responses that later fail parsing |

## Java validators

`JAVA_EXE` can point to a Java 25 or later executable. Java is optional for
server import, but full HermiT and Pellet paths require it.

| Variable | Purpose |
|---|---|
| `JAVA_EXE` | Java executable for HermiT and Pellet |
| `JAVA_MEMORY_MB` | JVM heap in MB for HermiT and Pellet. Unset or `0` keeps the owlready2 default |
| `OWL_IMPORT_LOCAL_DIR` | Local directory for `owl:imports` files. The first existing directory among this value, `IOF_LOCAL_DIR`, `data/external`, and the bundled `data/source/reference` is used |
| `IOF_LOCAL_DIR` | Fallback imports directory, checked after `OWL_IMPORT_LOCAL_DIR` |
| `PELLET_REALISATION_ENABLED` | `validate_owl_realisation` runs only when `true`; otherwise it returns a skip response |
| `OWL_KEEP_NAMED_RESTRICTIONS` | Default `false` anonymizes named OWL restrictions in reasoner input so owlready2 reconstructs the constraints. `true`, `1`, or `yes` retains the names; the canonical T-Box file is unchanged |

## T-Box quality

Readers: [ontology_quality.py](../../tools/ontology_quality.py) and the linked
[quality steps](../../tools/quality_steps/). These switches control graph edits
as well as diagnostics. `warn` retains a diagnostic without raising the
gate's failure; `relax`, `prune`, and enabled repair switches can change the
graph. The environment reference describes existing defaults, not a
recommendation to disable checks.

| Variable | Values |
|---|---|
| `TBOX_STRICT_CLASS_SPECIFIC` | `warn` (default), `rename`, `remove` |
| `TBOX_COVERAGE_GATE` | `warn` (default), `fail` |
| `TBOX_COVERAGE_THRESHOLD` | Decimal threshold, default `0.85` |
| `TBOX_OP_GROUNDING_GATE` | ObjectProperty evidence gate: `warn` (default), `fail` |
| `TBOX_OP_GROUNDING_MAX` | Ungrounded ObjectProperties allowed before the evidence gate warns or fails, default `40` |
| `TBOX_DUP_OP_GATE` | Duplicate `(domain, range)` ObjectProperty gate: `warn` (default), `fail` |
| `TBOX_DUP_OP_MAX_REDUNDANT` | Redundant ObjectProperties allowed before the duplicate gate warns or fails, default `0` |
| `TBOX_DUP_OP_PRUNE` | `on` removes duplicate ObjectProperties down to one per `(domain, range)`; default `off` only reports candidates |
| `TBOX_DUP_OP_PRUNE_KEEP` | Comma-separated property names to keep when pruning duplicates |

### Advanced T-Box steps

Use the documented `true`/`false` spellings for boolean switches. Some readers
accept only `false` to disable, while others also accept `0`, `off`, or `no`;
the linked implementation defines the exact alternatives.

| Variable | Default | Behavior / reader |
|---|---|---|
| `TBOX_MERGE_DUPLICATE_SOURCE_CLASSES` | `true` | Merge classes with identical source-column sets; `false` disables. [Step 11d](../../tools/quality_steps/step_11d_duplicate_source_class_merge.py) |
| `TBOX_AUTO_INJECT_MISSING_DPS` | `true` | Create missing CSV-backed DatatypeProperties; `false`, `0`, or `no` disables. [Step 12d](../../tools/quality_steps/step_12d_csv_dp_inject.py) |
| `TBOX_DP_SOURCE_BACKFILL` | `true` | Backfill `dcterms:source` from resolved column ownership. [Step 12d-3](../../tools/quality_steps/step_12d3_dp_source_backfill.py) |
| `TBOX_DP_SOURCE_GATE` | `warn` | `warn` or `fail` for insufficient DP source coverage. [Step 12e](../../tools/quality_steps/step_12e_dp_source_gate.py) |
| `TBOX_DP_SOURCE_THRESHOLD` | `0.95` | Minimum DP source coverage as a fraction. [Step 12e](../../tools/quality_steps/step_12e_dp_source_gate.py) |
| `TBOX_PRUNE_ORPHAN_DPS` | `true` | Remove eligible unused DPs with missing domain/source or redundant union domains; `false` disables the whole step. [Step 12f](../../tools/quality_steps/step_12f_orphan_dp_prune.py) |
| `TBOX_PRUNE_SOURCELESS_DPS` | `true` | Include eligible source-less DPs in step 12f pruning; `false` disables this subset. [Step 12f](../../tools/quality_steps/step_12f_orphan_dp_prune.py) |
| `TBOX_DP_RANGE_REALITY_AUTOFIX` | `true` | Widen a single declared DP range to `xsd:string` when every observed value fails conversion. [Step 12i](../../tools/quality_steps/step_12i_dp_range_csv_reality.py) |
| `TBOX_DP_RANGE_REALITY_GATE` | `warn` | `warn` or `fail` for remaining conversion failures after autofix. [Step 12i](../../tools/quality_steps/step_12i_dp_range_csv_reality.py) |
| `TBOX_DP_RANGE_REALITY_MAX_FAIL_RATIO` | `0.5` | Conversion-failure fraction above which a DP becomes a blocking candidate. [Step 12i](../../tools/quality_steps/step_12i_dp_range_csv_reality.py) |
| `TBOX_DUP_SOURCE_CLAIM` | `resolve` | Keep one DP claim per CSV column and remove the other `dcterms:source` values, preserving DP entities; `warn` reports, `off` skips. [Step 12k](../../tools/quality_steps/step_12k_duplicate_source_claim_resolve.py) |
| `TBOX_CARDINALITY_DATA_CHECK` | `relax` | `relax` removes data-incompatible DP minimum-cardinality restrictions; `warn` reports, `off` skips. [Step 13b](../../tools/quality_steps/step_13b_cardinality_data_reality.py) |
| `TBOX_CARDINALITY_MIN_FILL` | `0.95` | Classifies low-fill diagnostics. Step 13b still treats less than full fill as a minimum-cardinality violation; lowering this value does not make missing values satisfy that restriction. [Step 13b](../../tools/quality_steps/step_13b_cardinality_data_reality.py) |
| `TBOX_OP_MAX_CARD_CHECK` | `relax` | `relax` removes OP maximum-cardinality restrictions contradicted for all owners by CSV FK direction; `warn` reports, `off` skips. [Step 13c](../../tools/quality_steps/step_13c_op_max_cardinality_direction.py) |
| `TBOX_OP_FUNCTIONAL_FANOUT` | `remove` | Remove `owl:FunctionalProperty` from OPs whose tacit fan-out exceeds one value per subject; `warn` reports, `off` skips. [Step 13d](../../tools/quality_steps/step_13d_op_functional_fanout_audit.py) |
| `TBOX_EXISTENTIAL_AUDIT` | `prune` | Remove existing `someValuesFrom` participation axioms without CSV/tacit support; `warn` reports, `off` skips. [Step 13e](../../tools/quality_steps/step_13e_existential_axiom_audit.py) |
| `TBOX_CROSS_DOMAIN_FK_GATE` | `skip` | Suppress candidate cross-domain OPs lacking CSV FK evidence unless the name is already used in A-Box/tacit; `warn` permits creation and counts ungrounded candidates. The gate is not applied when FK evidence cannot be read. [Step 15](../../tools/quality_steps/step_15_cross_domain_ops.py) |
| `TBOX_UNDECLARED_OP_BACKFILL` | `true` | Add missing OP declarations from observed A-Box/tacit usage. [Step 15c](../../tools/quality_steps/step_15c_undeclared_op_backfill.py) |
| `TBOX_DEAD_STUB_PRUNE` | `true` | Remove class/individual stubs without supporting schema, data, or rule references. [Step 21b](../../tools/quality_steps/step_21b_dead_stub_prune.py) |
| `TBOX_UNDECLARED_REF_PRUNE` | `prune` | `prune` removes eligible hierarchy references to undeclared classes; `warn` reports, `off` skips. [Step 24c](../../tools/quality_steps/step_24c_undeclared_schema_ref_prune.py) |
| `TBOX_UNFILLABLE_GATE` | `warn` | `warn` or `fail` for classes whose modeled discriminator cannot be filled from the available data. [Step 31b](../../tools/quality_steps/step_31b_unfillable_class_audit.py) |
| `TBOX_UNFILLABLE_MAX` | `14` | Allowed unfillable-class count before the `fail` mode raises. [Step 31b](../../tools/quality_steps/step_31b_unfillable_class_audit.py) |

## A-Box generation and SHACL

Readers: [abox_generation.py](../../tools/abox_generation.py) and
[validation_core.py](../../tools/validation_core.py).

| Variable | Default | Behavior |
|---|---|---|
| `CSV_DATE_ORDER` | Unset | `dmy` or `mdy` selects day/month order. Unset or invalid values reject ambiguous dates rather than guessing their meaning |
| `SHACL_ENGINE` | `pyrudof` | Selects the Rust SHACL engine; `pyshacl` selects the Python implementation |
| `ABOX_PARALLEL_PREFETCH` | `true` | Prefetch CSV text concurrently when at least four files are available; `false` disables |
| `ABOX_PREFETCH_MAX_MB` | Adaptive | CSV prefetch memory limit in MiB. Unset uses 30% of available memory, bounded to 50 MiB–2 GiB; unavailable psutil or invalid integer uses 150 MiB. Explicit integers are not clamped |
| `ABOX_PROVENANCE_STREAM` | `true` | Stream cell provenance as NDJSON with a JSON metadata file. `false` uses the full JSON representation |
| `ABOX_PROVENANCE_GZIP` | `true` | Compress streamed provenance with gzip |
| `ABOX_MASTER_INCLUDE_INCOMING_OP` | `false` | Include transaction-to-master links and transaction types when exporting the master graph |
| `ABOX_APPLY_INVERSE_ON_GENERATE` | `false` | Add inverse triples during S7 before the SHACL cap/autofix checks |
| `ABOX_SHACL_AUTOFIX` | `true` | Run S7 SHACL validation and repair when within the instance cap |
| `ABOX_SHACL_MAX_INSTANCES` | `200000` | Skip the S7 SHACL repair stage above this instance count; invalid integers use the default |
| `ABOX_SHACL_INFER_TYPES` | `false` | Allow SHACL repair to infer missing types; disjointness checks still apply |

The A-Box boolean switches above enable on `true`, `1`, or `yes`.
`ABOX_WRITE_TBOX_INJECTIONS` is described below because it controls the canonical
T-Box write boundary.

## KG validation

Readers: [kg_validation.py](../../tools/kg_validation.py) and
[validation checks](../../tools/validation_support/checks/).

| Variable | Default | Behavior |
|---|---|---|
| `VALIDATE_KG_PARALLEL` | Unset (sequential) | `true`, `1`, or `yes` runs heavy checks concurrently; light checks remain sequential |
| `VALIDATE_KG_MAX_WORKERS` | `3` | Positive integer worker count for parallel checks |
| `FK_OP_COVERAGE_THRESHOLD` | `85.0` | Minimum FK-to-OP coverage in percent, not a fraction |
| `CW_MASTER_ORPHAN_THRESHOLD` | Tier policy | An explicit fraction replaces per-class tier evaluation with an overall orphan-rate threshold. Unset uses tier thresholds and excludes pure-source classes |
| `NUMERIC_OUTLIER_THRESHOLD` | `0.05` | Maximum numeric-outlier fraction |
| `REL_OUTLIER_THRESHOLD` | `0.05` | Maximum relationship-outlier fraction |
| `PROP_COMPLETENESS_MIN_PCT` | `30` | Minimum DP fill percentage. Applicable tier `property_coverage` thresholds override this value when tier information is supplied |

## Stores and inference

Readers: [tbox_utils.py](../../domain/tbox_utils.py),
[inference.py](../../tools/inference.py),
[inference_optd.py](../../tools/inference_optd.py), and
[semantic_dictionary.py](../../tools/semantic_dictionary.py).

| Variable | Purpose |
|---|---|
| `RDFLIB_STORE` | `Oxigraph` by default; `default` selects RDFLib's Python Memory store. `_new_graph()` uses memory unless an explicit disk path is supplied |
| `OXIGRAPH_STORE_PATH` | Unset/empty by default. Currently read into `_OXIGRAPH_PATH`, but `_new_graph()` uses its explicit `disk_path` argument; setting this variable alone does not enable disk persistence |
| `GRAPH_CACHE_MAX_ENTRIES` | Imported graph-cache limit, default `2`, minimum `1`. Must be an integer; each entry can hold a large graph |
| `SWRL_ENABLED` | Enables the opt-in Pellet SWRL path when `true` |
| `I2_PER_TRIPLE_JUSTIFICATION` | `false` disables per-triple inference justification; default `true` |
| `I2_MAX_REIFY` | Per-rule cap on reified justifications, default `200000`. `0` removes the cap |
| `UNIT_MISMATCH_RATIO` | Max/min value ratio that triggers a semantic dictionary unit warning, default `50`. `0` disables the warning |
| `ABOX_WRITE_TBOX_INJECTIONS` | Writes A-Box DP injections into the canonical T-Box only when `true`, `1`, or `yes`; default `false` keeps them in `data/generated/tbox/abox_injections.ttl` |
| `OPT_D_ENABLED` | Default off. `true`, `1`, `yes`, or `on` selects file-based OWL RL closure loading; its rdflib graph is empty, so graph-based postprocessing differs from the normal path |
| `OWL_CLOSURE_CHUNK` | Positive integer triples per closure-file write chunk, default `500000`; invalid integers use the default |
| `INFERENCE_SERIALIZE_FORMAT` | Default `turtle`; `nt`, `n-triples`, or `ntriples` selects N-Triples. The output path still ends in `all_inferred.ttl` |
| `INFERENCE_KEEP_CLOSURE_NT` | Default off. `true`, `1`, `yes`, or `on` retains the intermediate closure NT file after serialization |
| `SKIP_POST_RL_RESTRICTIONS` | Default off (stage runs). `true`, `1`, `yes`, or `on` skips restriction postprocessing |
| `SKIP_POST_RL_POLLUTION` | Default off (stage runs). The same true values skip type-pollution cleanup |
| `SKIP_POST_RL_PRUNE` | Default off (stage runs). The same true values skip inference-noise pruning |
| `SKIP_POST_RL_DELTA` | Default off (stages run). The same true values skip delta/loss-manifest generation and justification/provenance generation |

## Optional remote stores

| Variable | Purpose |
|---|---|
| `NEO4J_URI` | Neo4j Bolt URI. `.env.example` sets `bolt://localhost:7687` |
| `NEO4J_USER` | Neo4j user, default `neo4j` |
| `NEO4J_PASSWORD` | Neo4j password. Keep it in `.env` only |
| `GRAPHDB_BASE_URL` | GraphDB base URL, default `http://localhost:7200` |
| `GRAPHDB_REPOSITORY` | GraphDB repository. Unset uses `<namespace prefix>-kg` from the domain configuration, or `default-kg` when no prefix is set |
| `GRAPHDB_RULESET` | Default GraphDB inference ruleset, `owl2-rl-optimized` |
| `GRAPHDB_TIMEOUT` | Timeout in seconds for GraphDB import and query requests, default `600`. Full export waits four times this value |

## Paths

| Variable | Purpose |
|---|---|
| `DATA_DIR` | Root of the input (`source/`) and output (`generated/`) trees, default `<project>/data`. Some writers, such as the heartbeat file and `reports/cq_feedback.json`, still write under `<project>/data/generated`, so changing it splits outputs across two trees |
| `RULES_ROOT` | Root used by [rules_paths.py](../../domain/rules_paths.py), default `<project>/rules`. The default namespace configuration is resolved from this root; `DOMAIN_CONFIG_PATH` can override that file |
| `DOMAIN_CONFIG_PATH` | Alternate domain configuration |
| `DOTENV_PATH` | Explicit `.env` file |
| `ONTOLOGY_SERVER_LOG_FILE` | Server log path. Unset uses the OS temporary directory; an empty value disables file logging |
| `LOG_FORMAT` | `json` selects the structured formatter for a new stream handler created by [configure_logging](../../tools/common.py); unset uses text. The MCP entry point uses `server._configure_logging`, which does not read this variable, so setting it alone does not change the server format |

Never place secret values in documentation or committed configuration.
