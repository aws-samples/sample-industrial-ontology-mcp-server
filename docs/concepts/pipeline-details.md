# Pipeline details

The guided workflow uses canonical checkpoint keys from
`tools/pipeline_state.py::_STEP_DEPS`.

| Stage | Purpose | Primary tools |
|---|---|---|
| S0 | Competency questions | `check_competency_questions_exist`, `generate_competency_questions` |
| S1 | Source inspection | `list_csv_tables`, `read_csv_schema`, `generate_csv_erd`, `profile_csv_data` |
| S2 | Collaborative T-Box design | `generate_tbox_collaborative`, `get_tbox_status` |
| S3 | Deterministic T-Box improvement | `improve_tbox_quality` |
| S4 | T-Box validation | syntax, quality, consistency, classification, SHACL, baseline comparison |
| S4.5 | Optional mutation audit | `run_tbox_mutation_audit` |
| S5 | Tacit knowledge choice | natural language, deterministic rules, bootstrap, or skip |
| S6 | Visualization | `visualize_tbox` |
| S6.5 | Vocabulary contract v1 | `generate_semantic_dictionary(include_stats=False)` |
| S7 | A-Box generation | `generate_abox(use_dict_contract=True)` |
| S8 | OWL reasoning | `run_owl_rl_inference`, `get_inference_status` |
| S8.5 | Optional SWRL | `run_swrl_inference` |
| S9 | KG validation | `validate_kg` |
| S9.1 | OWL sanity checks | profile, consistency, entailment regression |
| S9.2 | Quality measurement | instance score and inferred delta |
| S9.5 | Optional KG mutation audit | `run_kg_mutation_audit` |
| S10 | Vocabulary contract v2 | `generate_semantic_dictionary(include_stats=True)` |
| S11 | Dictionary validation | `validate_semantic_dictionary` |
| S12 | Programmatic query tests | `test_domain_queries` |
| S12.5 | Golden-query regression | run or explicitly skip |
| S13 | Final report | `generate_pipeline_report` |

## Required user choices

S0 waits when no competency questions exist. The user chooses direct input or
data-based generation. S5 waits when no tacit source exists. The user chooses
natural-language input, deterministic rules, CSV and model bootstrap, or skip.

## Long-running jobs

S2 and S8 return a `job_id`. Poll their status tools instead of holding one
stdio request open. A partial S2 result is not a reviewed T-Box.

## Vocabulary contract

S6.5 writes structural vocabulary without A-Box statistics. S7 consumes it to
enforce class-specific property names. S10 overwrites the same file with
observed statistics. Running S6.5 by itself after S10 removes those statistics.

## Composite keys

Time-series tables can use timestamp plus another column as a composite key.
Tacit rules and A-Box generation must use the same ordered columns, or duplicate
instance IRIs can be created.

## Failure handling

Report the failed stage, error, cause, remediation, and restart point. Do not
treat `applicable: false` as a pass, and do not buy a higher score by weakening
a threshold.
