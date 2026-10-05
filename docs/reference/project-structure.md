# Project structure

```text
.
├── app.py
├── config.py
├── server.py
├── domain/
├── tools/
│   ├── validation_support/
│   └── remote/
├── rules/
│   ├── domain/
│   ├── policy/
│   ├── contracts/
│   └── swrl/
├── data/
│   └── source/
├── docs/
├── workshop/
├── prompts/
├── scripts/
└── tests/
```

## Entry points

| Area | Entry point |
|---|---|
| MCP registration | `server.py` |
| Runtime configuration | `config.py` |
| Namespace handling | `domain/namespaces.py` |
| Prefix resolution | `domain/graph_utils.py` |
| Rules paths | `domain/rules_paths.py` |
| Pipeline tools | `tools/` |
| Validation checks | `tools/validation_support/checks/` |
| Workshop | `workshop/setup-guide.md` |

Generated artifacts live under `data/generated/` and are not committed.
Tests must not use that directory as a fixture output.
