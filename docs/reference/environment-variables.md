# Environment variables

`.env.example` documents common settings. Source code remains the complete
reference because some diagnostic and quality switches are intentionally
advanced.

## AWS and Bedrock

| Variable | Purpose |
|---|---|
| `AWS_REGION` | Default AWS Region |
| `AWS_PROFILE` | Optional local profile |
| `BEDROCK_REGION` | Bedrock Region |
| `BEDROCK_MODEL_ID` | Model or inference profile |
| `BEDROCK_HEARTBEAT_ENABLED` | Long-running job heartbeat |

## Java validators

`JAVA_EXE` can point to a Java 25 or later executable. Java is optional for
server import, but full HermiT and Pellet paths require it.

## T-Box quality

| Variable | Values |
|---|---|
| `TBOX_STRICT_CLASS_SPECIFIC` | `warn`, `rename`, `remove` |
| `TBOX_COVERAGE_GATE` | `warn`, `fail` |
| `TBOX_COVERAGE_THRESHOLD` | Decimal threshold, default `0.85` |
| `TBOX_OP_GROUNDING_GATE` | ObjectProperty evidence policy |

## Stores and inference

| Variable | Purpose |
|---|---|
| `RDFLIB_STORE` | `oxigraph` by default, `default` for memory |
| `SWRL_ENABLED` | Enables the opt-in Pellet SWRL path |
| `ABOX_WRITE_TBOX_INJECTIONS` | Writes A-Box DP injections into the canonical T-Box only when `true`; default `false` keeps them in `data/generated/tbox/abox_injections.ttl` |

## Paths

| Variable | Purpose |
|---|---|
| `DOMAIN_CONFIG_PATH` | Alternate domain configuration |
| `DOTENV_PATH` | Explicit `.env` file |
| `SEMANTIC_DICT_PATH` | Vocabulary contract path |
| `ONTOLOGY_SERVER_LOG_FILE` | Server log path. Unset uses the OS temporary directory; an empty value disables file logging |

Never place secret values in documentation or committed configuration.
