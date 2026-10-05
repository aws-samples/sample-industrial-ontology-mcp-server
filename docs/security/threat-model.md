# Threat model

## Scope

The supported deployment is a local stdio MCP server processing synthetic or
explicitly approved files. Network service exposure and production operation
are outside scope.

## Assets

- Source CSV and rule files
- Generated T-Box, A-Box, inferred graph, and reports
- AWS and optional database credentials
- Model prompts and responses
- Local MCP client permissions

## Trust boundaries

1. User and MCP client
2. Local server process
3. Source and generated files
4. Amazon Bedrock
5. Optional Neo4j or GraphDB instances

## Threats and controls

| Threat | Control |
|---|---|
| Untrusted path escapes the project | Path validation and documented roots |
| Model output injects invalid RDF or queries | Syntax, SHACL, OWL, and query validation |
| Credentials enter Git | Ignore rules, secret scanning, and secret references |
| Customer identifiers enter the sample | Public-surface and customer-data gates |
| Remote write changes external state | Explicit confirmation and least privilege |
| Tool exposure exceeds the intended surface | Manifest-defined release and tool contract |
| Large compressed data hides reviewable text | Decompressed SHA-256 and documented provenance |
| Structurally valid tool input violates semantic constraints | Explicit path and suffix validation after schema validation; coverage is measured and partial |
| Tool output carries untrusted content into model context | Treat returned file or store data and model-generated RDF or queries as untrusted input |
| Tool implementation exceeds its published description | Manifest and registry contract, regression tests, and metadata-to-implementation review |
| Tool chaining turns earlier output or source data into a later prompt | Isolate tests from deployed artifacts, bound feedback, and limit source data to synthetic or approved files |
| Tool-description poisoning steers model tool selection or arguments | Manifest-defined exposure, fail-closed registration, code review, and MCP-specific scanning |

### Input schema validation

`register_public_tools()` passes each Python callable to `FastMCP.add_tool`
(`tools/registry.py:473-487`), so function type hints define the structural
input schema. A path declared as `str` can satisfy that schema while still
containing an absolute path, traversal, or symlink escape.

`resolve_child_path()` confines a filename to one direct child of a base
directory, while `resolve_path_within()` accepts absolute or relative paths
but resolves symlinks and requires containment (`tools/common.py:447-509`).
The shared helpers currently have 36 production call sites across 18 tool
modules. This is partial coverage, not a claim that all 129 published tools
use them. `tests/test_input_path_boundaries.py` exercises 30 public path
parameters against absolute, traversal, and symlink attacks; the GraphDB
import boundary has separate regression tests.

### Output schema validation

An MCP tool result is placed in the client model's context, so returned file
content and external-store responses are also model inputs. The repository
does not claim a generic output-schema firewall. Its current controls are
domain-specific: model-generated RDF, SPARQL, and Cypher remain untrusted and
must pass syntax, SHACL, OWL, and query validation before use. Path-validation
rejections for GraphDB import also avoid echoing the supplied or resolved path
(`tools/remote/graphdb.py:269-277`). Other returned text still requires
caller review before it influences a later tool invocation.

### Tool overreach

The implementation must not perform more than its published metadata says.
Before commit `140a3e5`, `graphdb_import_file` described five accepted
extensions (`.ttl`, `.nt`, `.nq`, `.rdf`, and `.jsonld`) but did not enforce
the list, and `_content_type_for()` silently treated an unknown extension as
`text/turtle`. An arbitrary local file could therefore be uploaded to the
optional external GraphDB store. Commit `140a3e5` added the suffix allowlist,
`DATA_DIR` containment, removed the silent fallback, aligned the docstring,
and added four regression cases
(`tests/test_graphdb_import_file_validation.py:30-96`).

The published surface is pinned to 129 names in `mcp-tools.toml`.
`tools/registry.py:413-461` resolves the explicit targets and fails closed if
the manifest and implementation name sets differ. This limits exposure drift,
but metadata-to-behavior agreement still depends on tests, code review, and
security scanning.

### Tool chaining

Tool output can become a later tool's model input. The documented feedback
chain is `test_domain_queries` to `append_iteration()` to
`cq_feedback.json`, followed by `load_active_suggestions()` into the next S2
Architect prompt (`tools/cq_feedback.py:1-5`,
`tools/multi_agent_tbox.py:3375-3402`). A recorded test incident contaminated
that deployed feedback file, which made test-fixture content an input to
T-Box generation.

The autouse guard in `tests/conftest.py:454-643` now blocks 11 write
primitives across helper or serialization writes, direct file writes,
replacement or rename, and directory creation. This enforces the invariant
that `data/generated/` is not test-fixture output. CSV headers and column
metadata also flow from source files into the T-Box generation prompt
(`tools/tbox_generation.py:1489-1540`, `tools/tbox_generation.py:1581-1618`);
the supported-use restriction to synthetic or explicitly approved files
limits that source-to-prompt boundary but does not make the content trusted.

### Tool-description poisoning

`_english_first_description()` builds each published tool description from a
fixed English sentence followed by the callable's source docstring
(`tools/registry.py:400-410`). A contributor who changes a docstring can
therefore influence how a client model selects a tool or constructs its
arguments.

The manifest fixes which names can be exposed, registration fails closed on a
name-set mismatch, and description changes require code review. An
MCP-specific security scanner reported zero poisoning findings in the
reviewed snapshot. That is point-in-time evidence, not a permanent guarantee,
because the manifest does not pin docstring contents.

## Implemented invariants

- `data/generated/` is not a test fixture output.
- Public files must not link to denied paths.
- Workshop code blocks and pre-generated artifacts are verified.
- The sample disclaimer and end-of-life section are release requirements.
- Public release candidates are derived from the manifest.
- Public MCP tool names must exactly match the manifest before registration.
- Tests must not write fixtures into the deployed `data/generated/` tree.
- Model and tool outputs remain untrusted until the applicable validator runs.
- Local SPARQL execution rejects SERVICE, FROM/FROM NAMED, LOAD, and USING on the
  exact final query string, using both the rdflib parse tree and an independent
  lexical check, and rejects text the two SPARQL parsers may tokenize differently.
- Generated HTML reports escape interpolated values with `html.escape`, embed
  script data with `tools/common.py` `json_for_script`, and escape values before
  client-side `innerHTML` rendering.
- Neo4j parity checks accept only identifier-shaped labels and run read-only.

## Production requirements

A production design needs authentication, authorization, transport security,
request limits, audit logging, tenant and data isolation, secret management,
backup and recovery, dependency maintenance, monitoring, incident response,
and a separate security review.

## Verification

```bash
python scripts/verify_workshop_sparql.py --ignore-placeholders
python -m pytest -q tests/public
python -m pytest -q tests/test_input_path_boundaries.py tests/test_generated_path_boundaries.py tests/test_query_egress.py tests/test_graphdb_import_file_validation.py
python -m pytest -q tests/test_sparql_egress_guard.py tests/test_html_output_escaping.py tests/test_neo4j_parity_injection.py tests/test_tool_path_containment_family.py
python -m ruff check app.py config.py server.py domain tools tests
bandit -r . -c pyproject.toml
pip-audit -r requirements.txt
pip-audit -r requirements-dev.txt
detect-secrets scan --baseline .secrets.baseline
```

Run security scanners from an isolated environment rather than installing
them into a live server environment. Bandit needs `-c pyproject.toml` to apply
the repository's test assertion exclusions; without it, more than 8,000
test-only `B101` records obscure the actionable report.

Every command above uses only files in this repository. Run them from the
repository root after installing the development requirements with
`python -m pip install -r requirements.txt -r requirements-dev.txt`.
