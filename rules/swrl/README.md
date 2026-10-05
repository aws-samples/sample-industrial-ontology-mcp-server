# SWRL rules

SWRL is an opt-in extension for rules that OWL RL cannot express, such as
multi-variable joins across observations.

## Enable

Set:

```dotenv
SWRL_ENABLED=true
```

Then call `run_swrl_inference` after OWL RL inference. The default pipeline does
not require SWRL or Pellet.

## Rule files

Place reviewed `.swrl` rules in this directory. Keep rules domain-specific,
bounded, and testable. A rule must not introduce identifiers, predicates, or
classes outside the configured vocabulary without a matching T-Box update.

## Safety

- Use synthetic or approved data.
- Review every rule with a domain expert.
- Compare inferred deltas before accepting results.
- Treat Pellet as an optional AGPL component and keep it out of the default
  execution path.
- Do not use SWRL to hide missing source relationships or to manufacture a
  quality score.
