"""validation.py — 후방 호환 re-export hub.

실제 구현은 validation_core.py, tbox_metrics.py, quality_dashboard.py에 있음.
기존 import 경로를 유지하기 위한 re-export 모듈.
"""
# validation_core
# quality_dashboard
from tools.quality_dashboard import (  # noqa: F401
    estimate_change_impact,
    generate_quality_dashboard,
    trace_quality_issues,
)

# tbox_metrics
from tools.tbox_metrics import (  # noqa: F401
    analyze_tbox,
    compare_tbox_baseline,
    detect_tbox_antipatterns,
    measure_tbox_metrics,
)
from tools.validation_core import (  # noqa: F401
    DETERMINISTIC_RULES,
    LLM_REQUIRED_RULES,
    RULES_DIR,
    SH,
    SHAPES_NS,
    _check_cardinality_with_completeness,
    _check_quality_rules_internal,
    check_quality_rules,
    validate_owl_cardinality,
    validate_shacl,
    validate_tbox_shacl,
    validate_ttl_syntax,
)
