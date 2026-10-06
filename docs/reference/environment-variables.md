# Environment variables

`.env.example` documents common settings. This page lists the variables most
users need. Source code remains the complete reference because some diagnostic
and quality switches are intentionally advanced. To find variables that are not
listed here, search the source:

```bash
grep -rnE 'getenv\(|environ\.get\(|_safe_int\(' config.py server.py domain/ tools/
```

Pipeline artifacts such as the T-Box, A-Box, inferred graph, and
`semantic_dictionary.json` have no path variable of their own. Apart from
`DATA_DIR`, which sets the root of the data tree, the output path variables on
this page cover auxiliary files only: the progress heartbeat file
(`MULTI_AGENT_HEARTBEAT_FILE`) and the server log (`ONTOLOGY_SERVER_LOG_FILE`).
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

| Variable | Purpose |
|---|---|
| `MULTI_AGENT_HEARTBEAT_SEC` | Interval in seconds between progress heartbeats during `generate_tbox_collaborative`, default `10`. `0` disables periodic heartbeats |
| `MULTI_AGENT_HEARTBEAT_FILE` | Heartbeat JSON path. Unset writes `<project>/data/generated/_heartbeat.json` regardless of `DATA_DIR`; an empty value disables the file |

## Java validators

`JAVA_EXE` can point to a Java 25 or later executable. Java is optional for
server import, but full HermiT and Pellet paths require it.

| Variable | Purpose |
|---|---|
| `JAVA_EXE` | Java executable for HermiT and Pellet |
| `JAVA_MEMORY_MB` | JVM heap in MB for HermiT and Pellet. Unset or `0` keeps the owlready2 default |
| `OWL_IMPORT_LOCAL_DIR` | Local directory for `owl:imports` files. The first existing directory among this value, `IOF_LOCAL_DIR`, `data/external`, and the bundled `data/source/reference` is used |
| `PELLET_REALISATION_ENABLED` | `validate_owl_realisation` runs only when `true`; otherwise it returns a skip response |

## T-Box quality

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

## Stores and inference

| Variable | Purpose |
|---|---|
| `RDFLIB_STORE` | `Oxigraph` by default, `default` for the in-memory store |
| `SWRL_ENABLED` | Enables the opt-in Pellet SWRL path when `true` |
| `I2_PER_TRIPLE_JUSTIFICATION` | `false` disables per-triple inference justification; default `true` |
| `I2_MAX_REIFY` | Per-rule cap on reified justifications, default `200000`. `0` removes the cap |
| `UNIT_MISMATCH_RATIO` | Max/min value ratio that triggers a semantic dictionary unit warning, default `50`. `0` disables the warning |
| `ABOX_WRITE_TBOX_INJECTIONS` | Writes A-Box DP injections into the canonical T-Box only when `true`, `1`, or `yes`; default `false` keeps them in `data/generated/tbox/abox_injections.ttl` |

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
| `DOMAIN_CONFIG_PATH` | Alternate domain configuration |
| `DOTENV_PATH` | Explicit `.env` file |
| `ONTOLOGY_SERVER_LOG_FILE` | Server log path. Unset uses the OS temporary directory; an empty value disables file logging |

Never place secret values in documentation or committed configuration.
