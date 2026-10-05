# Security Scanner Triage

This note explains recurring Bandit findings that are expected in this sample
repository. It is intended to help maintainers distinguish actionable findings
from scanner heuristics without suppressing unrelated checks.

## Non-cryptographic MD5

MD5 is used only to create deterministic IRI suffixes and to compare file
contents before and after an isolated test. These calls explicitly pass
`usedforsecurity=False`. They do not protect credentials, signatures, or other
security-sensitive data.

## B105 hardcoded password false positives

The eight production-tree B105 findings are counters, result fields, or numeric
configuration thresholds. None contains a credential or secret.

| Location | Scanner trigger | Actual purpose |
|---|---|---|
| `scripts/verify_workshop_sparql.py:292` | `{"PASS": 0}` | Result counter |
| `tools/foops_fair.py:46` | `{"pass": True}` | Check result flag |
| `tools/foops_fair.py:50` | `{"pass": False}` | Check result flag |
| `tools/foops_fair.py:496` | `{"pass": False}` | Check result flag |
| `tools/mutation_runner.py:327` | `{"PASS": 0}` | Status ranking |
| `tools/rules_init.py:118` | `token_budget_ratio` | Generation budget threshold |
| `tools/validation_support/thresholds.py:76` | `pass_rate_warning_delta` | Quality trend threshold |
| `tools/validation_support/thresholds.py:77` | `pass_rate_critical_delta` | Quality trend threshold |

## Twelve-digit account ID scan

The broad `[0-9]{12}` grep over the 676-file public candidate reports 12
matching lines after percentile cleanup. Eleven are existing sample or test
fixtures, and one is this note: synthetic timestamps such as `20260101120000`, synthetic table names such as `TBSOURCETABLE007202601010000`, and the imported IOF GLN example `GLN:9012345000004`.
The former 122 semantic-dictionary matching lines were IEEE 754 interpolation
residue in percentile statistics derived from synthetic CSV values; storing
`p10`, `p50`, `p90`, and `example_values` to six decimal places removes them.

There are zero distinct exact 12-digit tokens and zero AWS account IDs. Longer
timestamps, table identifiers, and 13-digit GLNs still contain a 12-digit
substring, so the broad grep is expected to retain these documented matches.

## Subprocess calls

The two production-tree subprocess sites use argument lists and do not enable
`shell=True`.

| Location | Command shape | Purpose |
|---|---|---|
| `scripts/verify_workshop_sparql.py:242` | `["bash", "-n", "-c", block]` | Bash syntax validation only. `-n` parses without executing the block. |
| `tools/pipeline_state.py:707` | `[java_exe, "-XshowSettings:properties", "-version"]` | Reads the installed Java version and properties. |

## Broad exception handling

Bandit B110 and B112 findings are low-severity observability findings, not
security vulnerabilities by themselves. The reviewed sites fall into three
groups:

- Local tolerance: a malformed optional RDF list, audit record, CSV header, or
  numeric annotation is skipped while independent items continue.
- Observable fallback: failures that can replace rule-derived values with
  defaults must emit a warning so operators can see that the fallback was used.
- Confirmed defect: none was established during this review.

Intentional tolerance sites include a one-line reason beside the broad
exception. Rule configuration, report metadata, dictionary freshness, and CQ
score fallbacks emit warnings while preserving their previous fallback
behavior.
