"""tools.validation_support 패키지: kg_validation 의 25 check 분해.

이름이 ``validation_support`` 인 이유: 기존 ``tools/validation.py`` re-export
hub 와 네임스페이스 충돌 회피.

본 ``__init__`` 은 facade — 호출자가 깊은 import 경로 (``checks.referential``
등) 대신 ``tools.validation_support`` 한 곳에서 가져갈 수 있도록 25 check
함수 + 보조 유틸 + dataclass 를 모두 re-export 한다.

서브패키지:
- ``checks/``: validate_kg 에 등록되는 25 check (structural / referential /
  semantic / statistical / temporal_cardinality 5그룹) + linked_data 부가 check
  (interlinking / licensing / understandability — validate_kg 미등록, FAIR 평가용)
- ``common``: SharedCheckContext, validate_prop_name, build_superclass_map 등
- ``thresholds``: 티어 임계값 + TypedDict
- ``quality_history``: 점수 이력 / regression 감지
- ``loss_budget``: 3-stage 손실 manifest 집계
- ``diagnostics``: 실패 진단 + provenance 역추적
- ``registry``: CheckRegistry (메타-감사용 check 카탈로그)
"""
from __future__ import annotations

# ── checks (등록 25 + linked_data 부가 3 함수) ──
from tools.validation_support.checks import (  # noqa: F401
    check_bidirectional_op,
    check_cardinality_constraints,
    check_class_instance_count,
    check_closed_world_fk_unresolved,
    check_closed_world_master_orphan,
    check_dangling_references,
    check_disjoint_class_violations,
    check_domain_range_conformance,
    check_existential_participation,
    check_fk_op_coverage,
    check_fk_referential_integrity,
    check_functional_violations,
    check_inference_sanity,
    check_interlinking,
    check_licensing,
    check_numeric_outliers,
    check_orphan_nodes,
    check_process_flow,
    check_property_completeness,
    check_property_coverage,
    check_relationship_outliers,
    check_schema_reference_integrity,
    check_string_patterns,
    check_tbox_fitness,
    check_undeclared_dp,
    check_undeclared_op,
    check_understandability,
    check_value_ranges,
    load_master_instance_uris,
    load_tacit_classes_cached,
)

# ── common 유틸 + dataclass ──
from tools.validation_support.common import (  # noqa: F401
    SharedCheckContext,
    build_superclass_map,
    detect_domain_ns,
    local,
    query,
    validate_prop_name,
)

# ── 진단 / 손실 / 이력 ──
from tools.validation_support.diagnostics import (  # noqa: F401
    diagnose_check_failure,
    trace_violations_to_provenance,
)
from tools.validation_support.loss_budget import load_loss_budget  # noqa: F401
from tools.validation_support.quality_history import (  # noqa: F401
    append_quality_history,
    detect_regression,
    parse_score,
)

# ── 임계값 ──
from tools.validation_support.thresholds import (  # noqa: F401
    RegressionThresholds,
    ReportLimits,
    TierThresholds,
    get_master_tier_max_count,
    get_regression_thresholds,
    get_report_limits,
    get_tier_thresholds,
    reload_cache,
)
