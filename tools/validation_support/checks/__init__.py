"""Validation check 함수 서브패키지.

kg_validation.py의 25 check를 주제별 모듈로 분해.
각 모듈의 check_* 함수는 kg_validation.py에서 _check_* 이름으로 re-export된다.

현재 포함:
- structural: bidirectional, process_flow, orphan_nodes, class_instance_count
- referential: fk_referential_integrity, fk_op_coverage, dangling_references,
               undeclared_op, closed_world_master_orphan, closed_world_fk_unresolved
- semantic: domain_range_conformance, property_coverage, tbox_fitness,
            schema_reference_integrity, existential_participation
- statistical: value_ranges, numeric_outliers, string_patterns, relationship_outliers
- temporal_cardinality: inference_sanity, property_completeness,
                       functional_violations, cardinality_constraints,
                       disjoint_class_violations
"""
from __future__ import annotations

from tools.validation_support.checks.linked_data import (  # noqa: F401
    check_interlinking,
    check_licensing,
    check_understandability,
)
from tools.validation_support.checks.referential import (  # noqa: F401
    check_closed_world_fk_unresolved,
    check_closed_world_master_orphan,
    check_dangling_references,
    check_fk_op_coverage,
    check_fk_referential_integrity,
    check_undeclared_dp,
    check_undeclared_op,
    load_master_instance_uris,
)
from tools.validation_support.checks.semantic import (  # noqa: F401
    check_domain_range_conformance,
    check_existential_participation,
    check_property_coverage,
    check_schema_reference_integrity,
    check_tbox_fitness,
    load_tacit_classes_cached,
)
from tools.validation_support.checks.statistical import (  # noqa: F401
    check_numeric_outliers,
    check_relationship_outliers,
    check_string_patterns,
    check_value_ranges,
)
from tools.validation_support.checks.structural import (  # noqa: F401
    check_bidirectional_op,
    check_class_instance_count,
    check_orphan_nodes,
    check_process_flow,
)
from tools.validation_support.checks.temporal_cardinality import (  # noqa: F401
    check_cardinality_constraints,
    check_disjoint_class_violations,
    check_functional_violations,
    check_inference_sanity,
    check_property_completeness,
)
