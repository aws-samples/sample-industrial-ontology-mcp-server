# Configuration

## Precedence

1. Environment variables select runtime-specific values.
2. `rules/domain/domain_config.json` defines domain vocabulary and heuristics.
3. `rules/policy/` defines reusable engine policy.
4. `rules/contracts/` contains generated or validated contracts.
5. `config.py` supplies code defaults.

## Rules directories

Use `domain/rules_paths.py`; do not construct `rules/` paths independently.

| Directory | Responsibility | New domain |
|---|---|---|
| `rules/domain/` | Domain-owned assets | Replace |
| `rules/policy/` | Engine policy | Keep |
| `rules/contracts/` | Domain-neutral contracts | Regenerate |

Run `initialize_domain_rules` without overwrite first. Review
`stale_domain_files`. `overwrite=True` can replace reviewed domain assets and
requires explicit approval.

## Secrets

Use `.env` only for local runtime values and secret references. Do not commit
`.env`, `.mcp.json`, tokens, passwords, or private keys.

## Validation

After configuration changes, restart the MCP server, call
`ontology_agent_health_check`, inspect `check_pipeline_state`, and run focused
tests for the changed contract.
