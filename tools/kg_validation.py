"""KG 자동 검증 도구 — 추론 후 데이터 품질 검증

T-Box + A-Box + 암묵지 + 추론 결과를 대상으로
25가지 필수 검증 쿼리를 실행한다.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from config import (
    # ⚠️ ``ABOX_PATH`` 는 이 모듈이 직접 참조하지 않지만 **제거하지 말 것**.
    # ``tools/mutation_runner.py`` 가 ``kgv.ABOX_PATH`` 를 monkeypatch 해 mutant
    # A-Box 를 주입한다 (그 파일의 ``orig["kgv_abox"]`` 계약). 2026-08-31 에 ruff 가
    # "미사용 import" 로 판정해 지웠고 그 즉시 mutation runner 가
    # ``AttributeError: module has no attribute 'ABOX_PATH'`` 로 죽었다 —
    # 모듈 속성 patch 는 정적 분석에 보이지 않는 사용이다.
    ABOX_PATH,  # noqa: F401 — mutation_runner 의 monkeypatch 대상
    GENERATED_ABOX_DIR,
    INFERRED_PATH,
    MASTER_DATA_PATH,
    SOURCE_RAWDATA_DIR,
    SOURCE_TACIT_DIR,
    TBOX_PATH,
    resolve_generated_path,
)
from domain.namespaces import DOMAIN_NS, NS_PREFIX
from domain.rules_paths import RULES_ROOT
from tools.common import error_response

# ── 공용 유틸 re-export (Session 9 이전) ─────────────
# 실제 구현은 tools.validation_support.common 에 있음.
#
# [NEW] 신규 코드는 public 이름을 `validation_support.common`에서 직접 import 하세요.
#       예) from tools.validation_support.common import SharedCheckContext
# [DEPRECATED] 아래 _접두 alias는 기존 호출자(특히 tests)와의 호환을 위해 유지되는 중.
#       신규 코드는 tools.validation_support 의 공개 이름을 직접 쓴다.
from tools.validation_support.common import (  # noqa: E402
    SharedCheckContext,
    build_superclass_map,
    detect_domain_ns,
    local,
    query,
    validate_prop_name,
)

_SharedCheckContext = SharedCheckContext
_build_superclass_map = build_superclass_map
_detect_domain_ns = detect_domain_ns
_local = local
_query = query
_validate_prop_name = validate_prop_name

# 내부 상수는 common 에서 이미 정의됨. 하위 호환을 위한 re-export.
from tools.validation_support.common import (  # noqa: E402
    _SAFE_NAME_RE,  # noqa: E402, F401
    _STANDARD_NS_PREFIXES,  # noqa: E402, F401
)

RULES_DIR = Path(RULES_ROOT)

logger = logging.getLogger(__name__)

# ── 적응형 품질 게이트 (M6) ──────────────────────────

# rules/policy/quality_thresholds.json에서 로드되는 티어별 임계값 (중앙 설정).
# 하위 호환을 위해 모듈 레벨 상수로도 노출한다.
from tools.validation_support.thresholds import (  # noqa: E402
    get_master_tier_max_count,
    get_tier_thresholds,
)

TIER_THRESHOLDS = get_tier_thresholds()


_TXN_LIKE_SUFFIXES = (
    "History", "Plan", "Transaction", "Map", "Mapping", "Log", "Record",
    "Order", "Result", "Event", "Events",
)


def _load_catalog_classes() -> set[str]:
    """domain_config.validation.catalog_classes 에 선언된 클래스 집합 반환.

    카탈로그 클래스는 코드 테이블 성격이라 대부분의 엔트리가 참조되지 않는 게
    자연스럽다. 예: EnergySourceMaster 50종 중 실제 사용되는 것은 소수.
    """
    from domain.namespaces import DOMAIN_CONFIG
    validation_cfg = DOMAIN_CONFIG.get("validation") or {}
    catalog = validation_cfg.get("catalog_classes") or []
    return set(catalog) if isinstance(catalog, list) else set()


def _classify_class_tiers(abox_stats: dict) -> dict[str, str]:
    """abox_stats.json의 per_class 인스턴스 수로 클래스 티어 분류.

    - catalog: domain_config.validation.catalog_classes 선언 (최우선)
    - instance_count == 0 → "inferred"
    - 0 < count ≤ master_max → "master" (단, 이름 접미사가 시계열/이벤트를
      암시하면 transaction 으로 override)
    - count > master_max → "transaction"

    접미사 override: ProductionPlan(157), MaintenanceHistory(102) 같은
    이력/계획 테이블은 instance 수로는 master 범위지만 참조되지 않는
    레코드가 자연스럽게 많음. `History`, `Plan`, `Transaction`, `Map` 등
    접미사를 가지면 transaction tier 로 재분류해 orphan_rate 임계치 60%
    를 적용한다.
    """
    master_max = get_master_tier_max_count()
    catalog_classes = _load_catalog_classes()
    tiers: dict[str, str] = {}
    for cls_name, stats in abox_stats.get("per_class", {}).items():
        # catalog 선언은 최우선 — 인스턴스 수/접미사와 무관하게 catalog tier.
        if cls_name in catalog_classes:
            tiers[cls_name] = "catalog"
            continue
        count = stats.get("instance_count", 0)
        if count == 0:
            tiers[cls_name] = "inferred"
            continue
        if count > master_max:
            tiers[cls_name] = "transaction"
            continue
        # instance 수는 master 범위이나 접미사 기반 재분류
        if any(cls_name.endswith(sfx) for sfx in _TXN_LIKE_SUFFIXES):
            tiers[cls_name] = "transaction"
        else:
            tiers[cls_name] = "master"
    return tiers


def _load_graph(use_inferred: bool) -> Graph:
    """검증 대상 그래프를 로드한다."""
    from domain.tbox_utils import load_graph
    g, _ = load_graph(use_inferred=use_inferred)
    return g


def _load_tbox_with_abox_overlay(tbox_path: str = "") -> Graph:
    """정본 T-Box와 A-Box 주입 DP overlay를 병합해 검증용 그래프로 읽는다."""
    graph = Graph()
    canonical_path = tbox_path or TBOX_PATH
    if os.path.exists(canonical_path):
        graph.parse(canonical_path, format="turtle")

    overlay_path = resolve_generated_path("tbox/abox_injections.ttl")
    if overlay_path.exists():
        graph.parse(overlay_path, format="turtle")
    return graph


# _load_tacit_classes_cached → semantic.py 로 이전.
from tools.validation_support.checks.semantic import (  # noqa: E402
    load_tacit_classes_cached as _load_tacit_classes_cached_impl,
)


def _load_tacit_classes_cached() -> set[str]:
    """Wrapper: 모듈 로컬 SOURCE_TACIT_DIR 주입 (test patch 호환)."""
    return _load_tacit_classes_cached_impl(source_tacit_dir=SOURCE_TACIT_DIR)


# 4개 structural check는 tools.validation_support.checks.structural 로 이전됨.
# 기존 private 이름 (_check_*)과 test patch 호환을 위해 얇은 wrapper 유지.
from tools.validation_support.checks.structural import (  # noqa: E402
    check_bidirectional_op as _check_bidirectional_op,
)
from tools.validation_support.checks.structural import (  # noqa: E402
    check_class_instance_count as _check_class_instance_count_impl,
)
from tools.validation_support.checks.structural import (  # noqa: E402
    check_orphan_nodes as _check_orphan_nodes,
)
from tools.validation_support.checks.structural import (  # noqa: E402
    check_process_flow as _check_process_flow,
)


def _check_class_instance_count(
    g, *, class_tiers=None, tbox=None, shared=None,
):
    """kg_validation 모듈 로컬 TBOX_PATH(테스트 patch 가능)를 주입하는 wrapper."""
    return _check_class_instance_count_impl(
        g, class_tiers=class_tiers, tbox=tbox, shared=shared,
        tbox_path=TBOX_PATH,
    )


# _check_fk_referential_integrity → tools.validation_support.checks.referential 로 이전
from tools.validation_support.checks.referential import (  # noqa: E402
    check_fk_referential_integrity as _check_fk_referential_integrity,
)

# _check_inference_sanity → temporal_cardinality.py 로 이전 (INFERRED_PATH 주입 wrapper).
from tools.validation_support.checks.temporal_cardinality import (  # noqa: E402
    check_inference_sanity as _check_inference_sanity_impl,
)


def _check_inference_sanity(g_inferred, raw_count) -> dict[str, object]:
    return _check_inference_sanity_impl(
        g_inferred, raw_count, inferred_path=INFERRED_PATH,
    )


def _count_triples_in_file(path: str) -> int:
    """TTL 파일을 로드하지 않고 트리플 수를 근사 추정한다.

    rdflib Graph.parse는 대용량 TTL에서 2GB+ 메모리를 쓰므로,
    추론 sanity check에서 원본 그래프의 전체 트리플 수가 필요할 때
    파일 라인 수를 trivially 근사치로 사용한다 (정확도는 2% 이내).
    """
    if not os.path.exists(path):
        return 0
    try:
        count = 0
        with open(path, "rb") as f:
            for line in f:
                stripped = line.strip()
                # TTL 트리플은 대체로 ' .' 또는 ' ;' 로 끝남 (한 트리플 1라인 형태)
                if stripped.endswith(b" .") or stripped.endswith(b";"):
                    count += 1
        return count
    except OSError:
        return 0


def _raw_triples_from_inference_manifest(inferred_triples: int | None = None) -> int:
    """추론 **전** 트리플 수 — 추론기가 기록한 실측값.

    ``run_owl_rl_inference`` 가 ``inference_loss_manifest.json`` 에 같은 실행의
    입력/출력 크기를 남긴다. 같은 로더로 잰 값이라 축이 정확히 맞는다 — 파일 라인수
    근사는 ``ensure_inverse_triples`` 산출(58,993 트리플)을 담을 수 없어 10.7% 어긋난다.

    **매니페스트가 추론 파일보다 오래되면 0 을 반환한다.** 낡은 값으로 판정하면
    "추론이 줄었다" 같은 거짓 신호가 난다 — 이 리포의 "낡은 baseline 이 진짜 손실을
    PASS 로 바꿨다" 와 같은 함정이다.

    ## mtime 순서는 **오염을 막지 못한다** (2026-09-01)

    시각 비교는 "낡음" 한 방향만 본다. 그런데 실제로 일어난 사고는 반대 방향이다 —
    테스트가 이 파일을 배포 경로에 덮어쓰면 매니페스트가 추론 산출물보다 **더 새것**
    이 되므로 위 부등호를 영원히 만족하지 않는다. 실측 (배포 파일)::

        all_inferred.ttl   08-31 06:22   실제 1,305,608 트리플
        manifest           09-01 03:10   input 합계 1  ← 테스트 픽스처

    그 raw=1 로 판정하면 ``check_inference_sanity`` 가 ``applicable: false`` (정직한
    미판정) 대신 ``increase_ratio: "130560700.0%"`` 라는 **그럴듯한 PASS** 를 낸다.
    FAIL 이 아니라 초록불이라 아무도 보지 않는다.

    그래서 시각이 아니라 **실행 동일성**을 본다: 매니페스트의 ``output.total_triples``
    는 그 실행이 만든 추론 그래프 크기다. 호출자가 지금 로드한 그래프 크기와 크게
    다르면 두 파일은 **다른 실행**의 산출물이므로 판정 근거가 없다.

    Args:
        inferred_triples: 호출자가 로드한 추론 그래프의 트리플 수. 주면 실행 동일성을
            대조한다. ``None`` 이면 대조를 건너뛴다 — 파일 라인수 근사로 대신하면
            안 된다 (Turtle 축약형 때문에 실측 대비 35.2% 어긋난다: 근사 846,444 vs
            실제 1,305,608). 근사로 대조하면 정상 실행이 오염으로 오판된다.
    """
    manifest = os.path.join(os.path.dirname(INFERRED_PATH),
                            "inference_loss_manifest.json")
    if not (os.path.exists(manifest) and os.path.exists(INFERRED_PATH)):
        return 0
    try:
        if os.path.getmtime(manifest) < os.path.getmtime(INFERRED_PATH) - 60:
            logger.warning(
                "inference_loss_manifest 가 all_inferred.ttl 보다 낡았다 — 추론 "
                "sanity 판정을 건너뛴다 (낡은 raw 로 비교하면 거짓 신호가 난다)",
            )
            return 0
        with open(manifest, encoding="utf-8") as handle:
            data = json.load(handle)

        # 소스 계보 각인이 있으면 그것이 **가장 강한** 판정이다. 매니페스트는
        # 자기가 서술하는 all_inferred.ttl 의 sha256 을 들고 있으므로 "같은 실행의
        # 산출물인가" 를 오차 없이 답한다. 위 mtime·트리플수 대조는 각인이 없는
        # 옛 매니페스트를 위한 폴백으로 남긴다 (각인 부재를 "일치" 로 읽지 않으려면
        # 두 축이 둘 다 필요하다 — 각인만 요구하면 옛 파일이 전부 미판정이 된다).
        from tools.common import stale_sources
        stamp = data.get("_source")
        if isinstance(stamp, dict):
            drifted = [d for d in stale_sources(stamp) if d["name"] == "inferred"]
            if drifted:
                logger.warning(
                    "inference_loss_manifest 가 지금의 all_inferred.ttl 을 서술하지 "
                    "않는다 (%s) — 추론 sanity 판정을 건너뛴다",
                    drifted[0].get("reason"),
                )
                return 0

        src = data.get("input") or {}
        from tools.inference import manifest_arithmetic_error
        arithmetic_error = manifest_arithmetic_error(src, data.get("output") or {})
        if arithmetic_error:
            logger.warning(
                "inference_loss_manifest 값이 실행에서 나올 수 없다 (%s) — 추론 "
                "sanity 판정을 건너뛴다. 테스트가 배포 파일을 덮어썼을 수 있다",
                arithmetic_error,
            )
            return 0

        if inferred_triples is not None and inferred_triples > 0:
            recorded = (data.get("output") or {}).get("total_triples")
            if isinstance(recorded, int) and recorded > 0:
                skew = abs(recorded - inferred_triples) / max(inferred_triples, 1)
                if skew > 0.05:
                    logger.warning(
                        "inference_loss_manifest 가 다른 실행의 산출물이다 "
                        "(기록 %d vs 로드 %d, 오차 %.1f%%) — 추론 sanity 판정을 "
                        "건너뛴다",
                        recorded, inferred_triples, skew * 100,
                    )
                    return 0

        total = sum(
            int(src.get(k) or 0)
            for k in ("tbox_triples", "abox_triples", "tacit_triples")
        )
        return total
    except Exception as exc:  # noqa: BLE001 — 판정 실패가 검증을 막지 않는다
        logger.debug("inference manifest 읽기 실패: %s", exc)
        return 0


# _SharedCheckContext, _build_superclass_map, _detect_domain_ns,
# _STANDARD_NS_PREFIXES 는 tools.validation_support.common 으로 이전됨.
# 파일 상단의 re-export로 기존 참조 계속 작동.


# _check_domain_range_conformance → semantic.py 로 이전
from tools.validation_support.checks.semantic import (  # noqa: E402
    check_domain_range_conformance as _check_domain_range_conformance,
)

# _check_existential_participation → semantic.py (25번째 check)
from tools.validation_support.checks.semantic import (  # noqa: E402
    check_existential_participation as _check_existential_participation,
)

# _check_property_coverage → semantic.py 로 이전
from tools.validation_support.checks.semantic import (  # noqa: E402
    check_property_coverage as _check_property_coverage,
)

# _check_schema_reference_integrity → semantic.py (23번째 check)
from tools.validation_support.checks.semantic import (  # noqa: E402
    check_schema_reference_integrity as _check_schema_reference_integrity,
)

# _check_value_ranges → statistical.py 로 이전 (RULES_DIR 주입 wrapper).
from tools.validation_support.checks.statistical import (  # noqa: E402
    check_value_ranges as _check_value_ranges_impl,
)


def _check_value_ranges(g: Graph) -> dict[str, object]:
    return _check_value_ranges_impl(g, rules_dir=str(RULES_DIR))


# _check_tbox_fitness → semantic.py 로 이전 (CSV/tacit 디렉토리 주입 wrapper).
from tools.validation_support.checks.semantic import (  # noqa: E402
    check_tbox_fitness as _check_tbox_fitness_impl,
)


def _check_tbox_fitness(tbox: Graph, *, class_tiers=None, shared=None) -> dict[str, object]:
    return _check_tbox_fitness_impl(
        tbox, class_tiers=class_tiers, shared=shared,
        source_rawdata_dir=SOURCE_RAWDATA_DIR,
        source_tacit_dir=SOURCE_TACIT_DIR,
    )


# _check_fk_op_coverage → tools.validation_support.checks.referential 로 이전.
# Wrapper: RULES_DIR / SOURCE_RAWDATA_DIR 모듈 로컬 값 주입 (test patch 호환).
from tools.validation_support.checks.referential import (  # noqa: E402
    check_fk_op_coverage as _check_fk_op_coverage_impl,
)


def _check_fk_op_coverage(tbox: Graph) -> dict[str, object]:
    return _check_fk_op_coverage_impl(
        tbox,
        rules_dir=str(RULES_DIR),
        source_rawdata_dir=SOURCE_RAWDATA_DIR,
    )


# _check_property_completeness → temporal_cardinality.py 로 이전
# _load_master_instance_uris / _check_closed_world_* → referential 로 이전.
# MASTER_DATA_PATH가 모듈 로컬 patch 가능하도록 wrapper.
from tools.validation_support.checks.referential import (  # noqa: E402
    check_closed_world_fk_unresolved as _check_closed_world_fk_unresolved_impl,
)
from tools.validation_support.checks.referential import (  # noqa: E402
    check_closed_world_master_orphan as _check_closed_world_master_orphan_impl,
)

# _check_dangling_references → tools.validation_support.checks.referential
from tools.validation_support.checks.referential import (  # noqa: E402
    check_dangling_references as _check_dangling_references,
)

# _check_undeclared_op → tools.validation_support.checks.referential
from tools.validation_support.checks.referential import (  # noqa: E402
    check_undeclared_dp as _check_undeclared_dp,
)
from tools.validation_support.checks.referential import (  # noqa: E402
    check_undeclared_op as _check_undeclared_op,
)
from tools.validation_support.checks.referential import (  # noqa: E402
    load_master_instance_uris as _load_master_instance_uris_impl,
)

# _NUMERIC_XSD + _check_numeric_outliers → statistical.py 로 이전
from tools.validation_support.checks.statistical import (  # noqa: E402
    _NUMERIC_XSD,  # noqa: F401
)
from tools.validation_support.checks.statistical import (  # noqa: E402
    check_numeric_outliers as _check_numeric_outliers,
)

# _check_string_patterns → statistical.py 로 이전
from tools.validation_support.checks.statistical import (  # noqa: E402
    check_string_patterns as _check_string_patterns,
)

# _check_cardinality_constraints → temporal_cardinality.py
from tools.validation_support.checks.temporal_cardinality import (  # noqa: E402
    check_cardinality_constraints as _check_cardinality_constraints,
)

# _check_disjoint_class_violations → temporal_cardinality.py
from tools.validation_support.checks.temporal_cardinality import (  # noqa: E402
    check_disjoint_class_violations as _check_disjoint_class_violations,
)

# _check_functional_violations → temporal_cardinality.py 로 이전
from tools.validation_support.checks.temporal_cardinality import (  # noqa: E402
    check_functional_violations as _check_functional_violations,
)
from tools.validation_support.checks.temporal_cardinality import (  # noqa: E402
    check_property_completeness as _check_property_completeness,
)


def _load_master_instance_uris() -> set[URIRef]:
    return _load_master_instance_uris_impl(master_data_path=MASTER_DATA_PATH)


def _check_closed_world_master_orphan(
    g, tbox=None, *, shared=None, class_tiers=None,
) -> dict[str, object]:
    return _check_closed_world_master_orphan_impl(
        g, tbox, shared=shared, master_data_path=MASTER_DATA_PATH,
        class_tiers=class_tiers,
    )


def _check_closed_world_fk_unresolved(
    g, tbox=None, *, shared=None,
) -> dict[str, object]:
    return _check_closed_world_fk_unresolved_impl(
        g, tbox, shared=shared, master_data_path=MASTER_DATA_PATH,
    )


# _check_relationship_outliers → statistical.py 로 이전
from tools.validation_support.checks.statistical import (  # noqa: E402
    check_relationship_outliers as _check_relationship_outliers,
)
from tools.validation_support.registry import CheckRegistry  # noqa: E402


def _namespace_mismatch_warning(tbox) -> dict | None:
    """T-Box 의 실제 도메인 네임스페이스가 설정과 다르면 경고 dict, 같으면 None.

    23개 check 중 대부분은 모듈 상수 ``DOMAIN_NS`` (설정값) 로 필터링한다. T-Box/
    A-Box 가 다른 네임스페이스를 쓰면 그 필터가 **0행** 을 반환하므로 게이트들이
    통과해 버리고, 결과적으로 **점수가 오른다** — 운영자는 개선으로 읽는다.
    2026-08-08 실측: 같은 결함(prov 트리플만 가진 인스턴스)이 설정 네임스페이스에서
    FAIL, 외래 네임스페이스에서 PASS 였다.

    도메인 전환(이 리포의 핵심 기능)이나 ``domain_config.json`` 오타에서 발생한다.
    개별 check 를 전부 ``shared.ns`` 로 옮기는 것이 정답이지만 54곳을 손대야 하므로,
    우선 **점수를 신뢰할 수 없다는 사실** 을 결과에 드러낸다.
    """
    try:
        from tools.validation_support.common import detect_domain_ns
        actual = detect_domain_ns(tbox)
    except Exception as exc:  # noqa: BLE001 — 진단이 검증을 막아선 안 된다
        logger.debug("네임스페이스 감지 skip: %s", exc)
        return None
    if not actual or actual == DOMAIN_NS:
        return None
    logger.error(
        "네임스페이스 불일치: T-Box=%s / 설정=%s — 대부분의 check 가 설정값으로 "
        "필터링하므로 조용히 통과한다. 점수를 신뢰할 수 없다.",
        actual, DOMAIN_NS,
    )
    return {
        "tbox_namespace": actual,
        "configured_namespace": DOMAIN_NS,
        "impact": (
            "대부분의 check 가 설정 네임스페이스로 필터링한다. 불일치 시 해당 "
            "check 들은 0행을 보고 통과하므로 점수가 실제보다 높게 나온다."
        ),
        "action": (
            "rules/domain/domain_config.json 의 namespace.class_ns 를 T-Box 와 일치시키고 "
            "validate_kg 를 다시 실행하라."
        ),
    }


def _run_checks(registry: CheckRegistry) -> list[dict]:
    """Registry의 check들을 실행. VALIDATE_KG_PARALLEL=true 시 heavy check만 ThreadPool.

    - 기본: 기존 동작과 동일 (순차).
    - 병렬 모드: heavy check들을 ThreadPoolExecutor로 동시 실행 + light는 순차.
      rdflib Graph는 read-only 쿼리에서 스레드 안전. SharedCheckContext는 frozen.
      환경변수 VALIDATE_KG_MAX_WORKERS(기본 3)로 워커 수 조정.
    """
    import os as _os
    if _os.getenv("VALIDATE_KG_PARALLEL", "").lower() not in ("true", "1", "yes"):
        checks: list[dict] = []
        for spec in registry:
            checks.append(spec.runner())
            if spec.is_heavy:
                gc.collect()
        return checks

    from concurrent.futures import ThreadPoolExecutor
    max_workers = int(_os.getenv("VALIDATE_KG_MAX_WORKERS", "3"))
    heavy_specs = [s for s in registry if s.is_heavy]
    light_specs = [s for s in registry if not s.is_heavy]

    # heavy를 병렬로 먼저 실행 (큰 순회가 중첩되지 않도록 완료 후 gc 1회).
    heavy_results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(s.runner): s.key for s in heavy_specs}
        for fut in futures:
            key = futures[fut]
            heavy_results[key] = fut.result()
    gc.collect()

    # light는 순차 (구성 비용 낮고, GIL 하 병렬 이득 적음).
    light_results: dict[str, dict] = {s.key: s.runner() for s in light_specs}

    # 등록 순서 보존: registry 순회 기준으로 병합.
    ordered: list[dict] = []
    for spec in registry:
        if spec.is_heavy:
            ordered.append(heavy_results[spec.key])
        else:
            ordered.append(light_results[spec.key])
    return ordered


def _build_check_registry(
    *,
    g, tbox, shared, class_tiers, raw_triple_count,
) -> CheckRegistry:
    """validate_kg의 25개 check를 레지스트리에 등록한다.

    기존 check_funcs 튜플 리스트의 대체. 새 check 추가 시 여기 한 군데만
    registry.register(...) 한 줄 넣으면 된다 (기존 4곳 수정 → 1곳).

    Note: 각 check의 lambda는 외부 인자를 클로저로 바인딩한다. 런너는 dict을 반환.
    CheckResult 값 객체로의 점진 이행은 validation_support.registry.CheckResult 참조.
    """
    reg = CheckRegistry()
    reg.register("bidirectional",
                 lambda: _check_bidirectional_op(g, tbox, shared=shared),
                 is_heavy=True, diagnosis_hint="schema_gap")
    reg.register("process_flow",
                 lambda: _check_process_flow(g, shared=shared),
                 diagnosis_hint="tacit_chain")
    reg.register("orphan",
                 lambda: _check_orphan_nodes(g, shared=shared),
                 is_heavy=True, diagnosis_hint="data_quality")
    reg.register("class_count",
                 lambda: _check_class_instance_count(
                     g, class_tiers=class_tiers, tbox=tbox, shared=shared),
                 diagnosis_hint="data_quality")
    reg.register("fk_ref",
                 lambda: _check_fk_referential_integrity(g, tbox, shared=shared),
                 diagnosis_hint="referential")
    reg.register("inference",
                 lambda: _check_inference_sanity(g, raw_triple_count),
                 diagnosis_hint="inference")
    reg.register("domain_range",
                 lambda: _check_domain_range_conformance(g, tbox, shared=shared),
                 is_heavy=True, diagnosis_hint="schema_gap")
    reg.register("schema_ref",
                 lambda: _check_schema_reference_integrity(tbox, shared=shared),
                 diagnosis_hint="schema_gap")
    reg.register("existential_participation",
                 lambda: _check_existential_participation(g, tbox, shared=shared),
                 is_heavy=True, diagnosis_hint="schema_gap")
    reg.register("property_coverage",
                 lambda: _check_property_coverage(g, tbox, class_tiers=class_tiers),
                 diagnosis_hint="schema_gap")
    reg.register("value_ranges",
                 lambda: _check_value_ranges(g),
                 diagnosis_hint="data_quality")
    reg.register("tbox_fitness",
                 lambda: _check_tbox_fitness(tbox, class_tiers=class_tiers, shared=shared),
                 diagnosis_hint="schema_gap")
    reg.register("fk_op",
                 lambda: _check_fk_op_coverage(tbox),
                 diagnosis_hint="schema_gap")
    reg.register("prop_completeness",
                 lambda: _check_property_completeness(g, tbox, class_tiers=class_tiers),
                 is_heavy=True, diagnosis_hint="data_quality")
    reg.register("numeric_outliers",
                 lambda: _check_numeric_outliers(g),
                 is_heavy=True, diagnosis_hint="data_quality")
    reg.register("dangling",
                 lambda: _check_dangling_references(g, class_tiers=class_tiers, shared=shared),
                 diagnosis_hint="referential")
    reg.register("undeclared_op",
                 lambda: _check_undeclared_op(g, tbox, shared=shared),
                 diagnosis_hint="schema_gap")
    # DP 거울상. undeclared_op 은 IRI 값 술어만 보므로 "A-Box 가 리터럴 값으로 쓰는데
    # T-Box 에 없는 DP" 는 23 check 전체에서 사각지대였다 (실측 18건, 최다 4,320 트리플).
    reg.register("undeclared_dp",
                 lambda: _check_undeclared_dp(g, tbox, shared=shared),
                 diagnosis_hint="schema_gap")
    reg.register("string_patterns",
                 lambda: _check_string_patterns(g, class_tiers=class_tiers, tbox=tbox),
                 is_heavy=True, diagnosis_hint="data_quality")
    reg.register("functional",
                 lambda: _check_functional_violations(g, tbox),
                 diagnosis_hint="cardinality")
    reg.register("relationship_outliers",
                 lambda: _check_relationship_outliers(g),
                 is_heavy=True, diagnosis_hint="data_quality")
    reg.register("cardinality",
                 lambda: _check_cardinality_constraints(g, tbox, shared=shared),
                 diagnosis_hint="cardinality")
    reg.register("disjoint",
                 lambda: _check_disjoint_class_violations(g, tbox, shared=shared),
                 diagnosis_hint="data_quality")
    reg.register("cw_master_orphan",
                 lambda: _check_closed_world_master_orphan(
                     g, tbox, shared=shared, class_tiers=class_tiers),
                 diagnosis_hint="referential")
    reg.register("cw_fk_unresolved",
                 lambda: _check_closed_world_fk_unresolved(g, tbox, shared=shared),
                 diagnosis_hint="referential")
    return reg


def validate_kg(use_inferred: bool = False) -> str:
    """추론 후 KG 데이터 품질을 25가지 필수 검증으로 확인한다.

    예상 소요시간: 5~8분 (추론 그래프 기준)

    1. ObjectProperty 양방향 연결
    2. 암묵지 공정 흐름 체인
    3. 고아 노드 탐지
    4. 클래스별 인스턴스 수
    5. FK 참조 무결성
    6. 추론 sanity check
    7. domain/range 타입 정합성
    8. 프로퍼티 사용 커버리지
    9. 값 범위 검증
    10. T-Box Fitness
    11. FK-OP Gap 분석
    12. 프로퍼티별 완전성
    13. 수치 이상치 탐지
    14. 시간 정합성
    15. 댕글링 참조 탐지
    16. 문자열 패턴 검증
    17. Functional Property 위반 (Farber 2018)
    18. Relationship Pattern 이상치 (Paulheim 2017)
    19. 카디널리티 제약 위반 (OWL min/max/exactCardinality)
    20. AllDisjointClasses 위반

    Args:
        use_inferred: True면 all_inferred.ttl 사용, False면 T-Box+A-Box+tacit 병합.
    """
    try:
        start = time.monotonic()

        # 그래프 로드 — 단일 그래프만 유지. merge와 inferred 두 거대 그래프를 동시에 상주시키면
        # 4GB+ RSS로 맥북이 다운됨. raw 트리플 수는 파일 라인 수로 근사한다.
        g = _load_graph(use_inferred)
        tbox = _load_tbox_with_abox_overlay(TBOX_PATH)

        # 추론 sanity check 용 raw 트리플 수.
        #
        # ## 두 결함이 이 한 줄에 있었다 (2026-08-31 수정)
        #
        # **(1) 기본 모드가 자기 자신과 비교했다.** ``use_inferred=False`` 에서
        # ``raw_triple_count = len(g)`` 였고 같은 ``g`` 가 ``g_inferred`` 로 넘어가
        # ``increase`` 가 **항상 0** → 이 체크는 영구 FAIL 이었다. 그리고
        # ``passed = (passed == total)`` 이므로 **기본 모드는 절대 통과할 수 없었다**.
        # 게다가 ``mutation_runner`` 가 그 모드를 하드코딩하므로 S9.5 뮤테이션
        # 감사에서 이 체크는 baseline 이 이미 FAIL — 어떤 mutant 도 잡지 못했다.
        #
        # 설계가 아니라 회귀다: 원본(f2f7744)은 두 번째 그래프를 로드해 넘겼고,
        # ``344fd46`` 이 RSS 를 줄이려고 그것을 없애며 같은 그래프를 양쪽에 연결했다.
        #
        # **(2) 추론 모드의 근사가 출처 집합과 어긋났다.** 라인수 근사는
        # ``master_data.ttl`` 을 포함하는데 ``load_graph`` 는 그것을 안 읽고, 반대로
        # tacit 8파일(51,242 트리플)은 근사에서 빠졌다. 실측 2026-08-18: 근사
        # 725,547 vs 실제 병합 856,745 — **오차 15.3%**.
        #
        # ## 근사를 버리고 추론기의 실측 기록을 쓴다 (2026-08-31)
        #
        # 파일 라인수 근사는 이 축에 맞지 않는다. 파일별 합은 오차 3.8% 로 정확한데
        # 병합 그래프는 그보다 **58,993 트리플 크다** — ``load_graph`` 가
        # ``ensure_inverse_triples`` 로 역방향을 로드 시점에 만들고 그것은 **어떤
        # 파일에도 없다**. 즉 "파일 근사 vs 병합 그래프" 는 원리상 축이 어긋난다
        # (실측: 근사 739,864 vs 실제 828,433 = 10.7% 오차).
        #
        # 그런데 **추론기 자신이 양쪽 실측값을 이미 기록한다**:
        #
        #     data/generated/inferred/inference_loss_manifest.json
        #       input : {tbox_triples, abox_triples, tacit_triples}
        #       output: {total_triples, inferred_new}
        #
        # 그 값은 같은 실행에서 같은 로더로 잰 것이므로 축이 정확히 맞다. 근사도
        # 두 번째 로드도 필요 없다 (344fd46 의 RSS 목표 유지).
        #
        # ## 실행 동일성을 대조한다 (2026-09-01)
        #
        # 매니페스트를 정본으로 삼자 그 파일의 **출처**가 새 공격면이 됐다. 테스트가
        # 배포 경로에 덮어쓰면 mtime 가드는 발화하지 않는다 (오염은 항상 "더 새것").
        # ``use_inferred=True`` 일 때만 ``len(g)`` 가 추론 그래프 크기이므로, 그
        # 경우에만 매니페스트가 기록한 산출물 크기와 대조해 같은 실행인지 확인한다.
        raw_triple_count = _raw_triples_from_inference_manifest(
            len(g) if use_inferred else None,
        )
        if raw_triple_count <= 0:
            # 매니페스트가 없거나 낡았다 — 판정 근거가 없다. 자기비교(len(g))로
            # 폴백하면 increase 가 항상 0 이 되어 **영구 FAIL** 이 되므로 (그것이
            # 원래 결함이다) 0 을 넘겨 "파일 부재/미측정" 분기로 보낸다.
            raw_triple_count = 0

        # 적응형 품질 게이트: abox_stats.json에서 클래스 티어 분류
        class_tiers: dict[str, str] | None = None
        try:
            _abox_stats = _load_abox_stats()
            if _abox_stats is not None:
                class_tiers = _classify_class_tiers(_abox_stats)
        except Exception:
            class_tiers = None

        # 공유 캐시: instance_types는 check 7, 19, 20에서 재사용 (전수 재구축은 메모리/CPU 낭비)
        shared_ctx = _SharedCheckContext(g, tbox)

        # 25가지 검증 실행. Registry를 통해 메타데이터 + 실행 callable을 관리한다.
        # is_heavy=True인 check는 실행 직후 gc.collect()로 큰 자료구조를 즉시 회수한다.
        registry = _build_check_registry(
            g=g,
            tbox=tbox,
            shared=shared_ctx,
            class_tiers=class_tiers,
            raw_triple_count=raw_triple_count,
        )
        # 장기 실행(5~8분) heartbeat — 사용자가 진행 상황 판단 가능.
        from tools.heartbeat import Heartbeat
        with Heartbeat(stage="S9_KG_VALIDATE", interval=60.0, tool_logger=logger):
            checks = _run_checks(registry)

        # 공유 캐시 해제 — instance_types 등 대형 자료구조 즉시 GC
        shared_ctx.release()
        gc.collect()

        duration = round(time.monotonic() - start, 1)

        passed = sum(1 for c in checks if c["passed"])
        total = len(checks)

        # End-to-end loss budget 집계
        loss_budget = _load_loss_budget()

        # Provenance 역추적: 실패한 검증의 위반 인스턴스를 CSV 소스까지 추적
        provenance_traces = []
        for check in checks:
            if check["passed"]:
                continue
            violations = check.get("violations", check.get("missing", []))
            if violations:
                traces = _trace_violations_to_provenance(violations, max_traces=5)
                if traces:
                    provenance_traces.extend(traces)

        # 측정 회계 — "재지 않았다" 와 "통과했다" 를 분리한다.
        #
        # ``score`` 문자열은 **형식을 유지** 한다. quality_history.parse_total 이
        # ``int(score.split("/")[1])`` 이라 뒤에 무엇이든 붙이면 회귀 탐지가 그 항목을
        # 통째로 버린다. 회계는 별 필드로만 낸다.
        from tools.validation_support.common import (
            measurability_violations,
            summarize_measurability,
        )
        measurability = summarize_measurability(checks)

        result = {
            "success": True,
            "passed": passed == total,
            "score": f"{passed}/{total}",
            # 분모에서 미판정 축을 뺀 점수. score 와 다르면 그 차이가 미측정분이다.
            "score_measured": measurability["score_measured"],
            "measured_total": measurability["measured_total"],
            "unmeasured_checks": measurability["unmeasured_checks"],
            "duration_seconds": duration,
            "source": "inferred" if use_inferred else "merge",
            "triples": len(g),
            "checks": checks,
        }
        # 미판정 선언이 세탁으로 쓰이면 그 사실을 최상위로 올린다 (조용히 두면
        # 상시 빨간불을 applicable=false 로 재분류하는 것이 최단 경로가 된다).
        laundering = measurability_violations(checks)
        if laundering:
            result["measurability_violations"] = laundering
            result["score_reliable"] = False
        mismatch = _namespace_mismatch_warning(tbox)
        if mismatch:
            # 점수를 신뢰할 수 없다는 사실을 결과에 **명시** 한다. 조용히 두면
            # 침묵한 게이트들이 오히려 점수를 올려, 운영자가 개선으로 읽는다.
            result["namespace_mismatch"] = mismatch
            result["score_reliable"] = False
        if loss_budget:
            result["loss_budget"] = loss_budget
        if provenance_traces:
            result["provenance_traces"] = provenance_traces[:20]

        # 진단 엔진: 실패 check별 원인 분류 + 수정 제안
        diagnoses = []
        for check in checks:
            if not check["passed"]:
                d = _diagnose_check_failure(check)
                d["check_name"] = check["name"]
                diagnoses.append(d)
        if diagnoses:
            result["diagnoses"] = diagnoses
            result["auto_fixable_count"] = sum(1 for d in diagnoses if d["auto_fixable"])

        # #12: OntoClean 파이프라인 통합 — T-Box 메타-속성 감사 결과 요약
        try:
            from tools.ontoclean import analyze_ontoclean
            oc = analyze_ontoclean()
            if "error" not in oc:
                result["ontoclean"] = {
                    "classes_total": oc["classes_total"],
                    "labeling_coverage_pct": oc["labeling_coverage_pct"],
                    "violations_total": oc["violations_total"],
                    "by_severity": oc.get("by_severity", {}),
                    # 첫 3건만 인라인, 전체는 ontoclean_report.json 참조
                    "top_violations": oc.get("violations", [])[:3],
                }
                # Gate: critical OntoClean 위반이 있으면 전체 passed=false
                critical_oc = oc.get("by_severity", {}).get("critical", 0)
                if critical_oc > 0:
                    result["ontoclean"]["critical_block"] = True
                    result["passed"] = False
        except Exception as _oc_err:
            logger.debug("OntoClean 통합 실패 (선택적): %s", _oc_err)

        # 품질 이력 저장 + regression 감지
        try:
            _append_quality_history(result)
            history_path = os.path.join(os.path.dirname(GENERATED_ABOX_DIR), "quality_history.json")
            if os.path.exists(history_path):
                with open(history_path, encoding="utf-8") as f:
                    history = json.load(f)
                regression = _detect_regression(history)
                if regression:
                    result["regression"] = regression
        except Exception as _qh_err:
            logger.debug("Quality history 처리 실패: %s", _qh_err)

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def get_quality_history(last_n: int = 10) -> str:
    """KG 검증 이력을 조회한다.

    Args:
        last_n: 최근 N건 조회 (기본 10)
    """
    try:
        history_path = os.path.join(os.path.dirname(GENERATED_ABOX_DIR), "quality_history.json")
        if not os.path.exists(history_path):
            return json.dumps({"success": True, "history": [], "message": "이력 없음"}, ensure_ascii=False, indent=2)

        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)

        recent = history[-last_n:] if last_n > 0 else history
        regression = _detect_regression(history)

        return json.dumps({
            "success": True,
            "total_entries": len(history),
            "showing": len(recent),
            "history": recent,
            "regression": regression,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def _load_loss_budget() -> dict | None:
    """Delegates to tools.validation_support.loss_budget (thin wrapper).

    모듈 로컬 GENERATED_ABOX_DIR / INFERRED_PATH (테스트 patch 가능) 전달.
    """
    from tools.validation_support.loss_budget import load_loss_budget
    return load_loss_budget(
        generated_abox_dir=GENERATED_ABOX_DIR,
        inferred_path=INFERRED_PATH,
    )


def _diagnose_check_failure(check: dict) -> dict:
    """Delegate to tools.validation_support.diagnostics (thin wrapper)."""
    from tools.validation_support.diagnostics import diagnose_check_failure
    return diagnose_check_failure(check)


# history/regression 함수들은 tools.validation.quality_history로 분리됨.
# 기존 호출자 호환성을 위해 얇은 래퍼만 유지.
def _parse_score(score_str: str) -> int:
    from tools.validation_support.quality_history import parse_score
    return parse_score(score_str)


def _detect_regression(history: list) -> dict | None:
    from tools.validation_support.quality_history import detect_regression
    return detect_regression(history)


def _append_quality_history(result: dict):
    from tools.validation_support.quality_history import append_quality_history
    # 모듈 로컬 GENERATED_ABOX_DIR(테스트가 patch할 수 있음)을 사용.
    append_quality_history(result, base_dir=os.path.dirname(GENERATED_ABOX_DIR))


def _trace_violations_to_provenance(
    violations: list[dict], max_traces: int = 10,
) -> list[dict]:
    """Delegate to tools.validation_support.diagnostics (thin wrapper).

    모듈 로컬 GENERATED_ABOX_DIR(테스트 patch 가능)을 명시 전달.
    """
    from tools.validation_support.diagnostics import trace_violations_to_provenance
    return trace_violations_to_provenance(
        violations,
        max_traces,
        provenance_path=os.path.join(GENERATED_ABOX_DIR, "abox_provenance.json"),
    )


# ── 스키마 기반 테스트 생성 (RDFUnit 방식) ────────────────────


def _extract_tbox_property_declarations(tbox: Graph) -> dict[str, dict]:
    """T-Box에서 모든 프로퍼티의 domain/range/functional 선언을 추출한다."""
    props: dict[str, dict] = {}

    for prop_type_uri, prop_kind in [
        (OWL.ObjectProperty, "OP"),
        (OWL.DatatypeProperty, "DP"),
    ]:
        for prop in tbox.subjects(RDF.type, prop_type_uri):
            if not isinstance(prop, URIRef) or not str(prop).startswith(DOMAIN_NS):
                continue
            name = _local(str(prop))
            domains = [
                _local(str(d)) for d in tbox.objects(prop, RDFS.domain)
                if isinstance(d, URIRef) and str(d).startswith(DOMAIN_NS)
            ]
            ranges_uris = list(tbox.objects(prop, RDFS.range))
            is_functional = (prop, RDF.type, OWL.FunctionalProperty) in tbox
            is_inverse_functional = (prop, RDF.type, OWL.InverseFunctionalProperty) in tbox

            if prop_kind == "OP":
                range_classes = [
                    _local(str(r)) for r in ranges_uris
                    if isinstance(r, URIRef) and str(r).startswith(DOMAIN_NS)
                ]
                props[name] = {
                    "kind": "OP",
                    "domain": domains[0] if domains else None,
                    "range_class": range_classes[0] if range_classes else None,
                    "range_datatype": None,
                    "functional": is_functional,
                    "inverse_functional": is_inverse_functional,
                }
            else:
                # DP: range는 XSD 데이터타입
                range_dt = None
                for r in ranges_uris:
                    if isinstance(r, URIRef):
                        r_str = str(r)
                        if r_str.startswith(str(XSD)):
                            range_dt = r_str.replace(str(XSD), "xsd:")
                            break
                props[name] = {
                    "kind": "DP",
                    "domain": domains[0] if domains else None,
                    "range_class": None,
                    "range_datatype": range_dt,
                    "functional": is_functional,
                    "inverse_functional": is_inverse_functional,
                }

    return props


def _run_domain_test(
    g: Graph, prop_name: str, decl: dict, superclass_map: dict[str, set[str]],
    max_violations: int,
) -> dict | None:
    """Domain test: 프로퍼티 사용 주체가 선언된 domain 클래스(또는 하위 클래스)인지 검증."""
    domain_cls = decl.get("domain")
    if not domain_cls:
        return None

    _validate_prop_name(prop_name)
    _validate_prop_name(domain_cls)

    rows = _query(g, f'''
        SELECT ?s ?sType WHERE {{
            ?s {NS_PREFIX}:{prop_name} ?o .
            ?s a ?sType .
            FILTER(STRSTARTS(STR(?sType), "{DOMAIN_NS}"))
        }} LIMIT 200
    ''')

    if not rows:
        return None

    # 인스턴스별 타입 수집
    instance_types: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        s_uri = row.get("s", "")
        s_type = _local(row.get("sType"))
        if s_type:
            instance_types[s_uri].add(s_type)

    def _is_compatible(actual: str, expected: str) -> bool:
        if actual == expected:
            return True
        return expected in superclass_map.get(actual, set())

    violations = []
    for s_uri, types in instance_types.items():
        if not any(_is_compatible(t, domain_cls) for t in types):
            violations.append({
                "subject": _local(s_uri),
                "actual_types": sorted(types),
                "expected_domain": domain_cls,
            })
            if len(violations) >= max_violations:
                break

    if not violations:
        return None

    return {
        "test": f"domain({prop_name}) = {domain_cls}",
        "violation_count": len(violations),
        "samples": violations,
    }


def _run_range_test_op(
    g: Graph, prop_name: str, decl: dict, superclass_map: dict[str, set[str]],
    max_violations: int,
) -> dict | None:
    """Range test for ObjectProperty: 대상이 선언된 range 클래스(또는 하위 클래스)인지 검증."""
    range_cls = decl.get("range_class")
    if not range_cls:
        return None

    _validate_prop_name(prop_name)
    _validate_prop_name(range_cls)

    rows = _query(g, f'''
        SELECT ?o ?oType WHERE {{
            ?s {NS_PREFIX}:{prop_name} ?o .
            FILTER(isIRI(?o))
            ?o a ?oType .
            FILTER(STRSTARTS(STR(?oType), "{DOMAIN_NS}"))
        }} LIMIT 200
    ''')

    if not rows:
        return None

    instance_types: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        o_uri = row.get("o", "")
        o_type = _local(row.get("oType"))
        if o_type:
            instance_types[o_uri].add(o_type)

    def _is_compatible(actual: str, expected: str) -> bool:
        if actual == expected:
            return True
        return expected in superclass_map.get(actual, set())

    violations = []
    for o_uri, types in instance_types.items():
        if not any(_is_compatible(t, range_cls) for t in types):
            violations.append({
                "object": _local(o_uri),
                "actual_types": sorted(types),
                "expected_range": range_cls,
            })
            if len(violations) >= max_violations:
                break

    if not violations:
        return None

    return {
        "test": f"range({prop_name}) = {range_cls}",
        "violation_count": len(violations),
        "samples": violations,
    }


def _run_range_test_dp(
    g: Graph, prop_name: str, decl: dict, max_violations: int,
) -> dict | None:
    """Range test for DatatypeProperty: 값의 XSD 타입이 선언과 일치하는지 검증."""
    range_dt = decl.get("range_datatype")
    if not range_dt:
        return None

    _validate_prop_name(prop_name)
    prop_uri = URIRef(f"{DOMAIN_NS}{prop_name}")

    violations = []
    checked = 0
    for _, _, obj in g.triples((None, prop_uri, None)):
        if not isinstance(obj, Literal):
            continue
        checked += 1
        actual_dt = str(obj.datatype) if obj.datatype else None
        expected_dt = range_dt.replace("xsd:", str(XSD))
        if actual_dt and actual_dt != expected_dt:
            violations.append({
                "value": str(obj)[:80],
                "actual_datatype": actual_dt.replace(str(XSD), "xsd:") if actual_dt else "none",
                "expected_datatype": range_dt,
            })
            if len(violations) >= max_violations:
                break

    if not violations:
        return None

    return {
        "test": f"datatype({prop_name}) = {range_dt}",
        "checked_values": checked,
        "violation_count": len(violations),
        "samples": violations,
    }


def _run_functional_test(
    g: Graph, prop_name: str, max_violations: int,
) -> dict | None:
    """Functional test: FunctionalProperty에 2개 이상 값이 있는 인스턴스를 탐지."""
    _validate_prop_name(prop_name)
    rows = _query(g, f'''
        SELECT ?s (COUNT(?o) AS ?cnt) WHERE {{
            ?s {NS_PREFIX}:{prop_name} ?o
        }} GROUP BY ?s HAVING (COUNT(?o) > 1)
        LIMIT {max_violations}
    ''')

    if not rows:
        return None

    violations = [
        {"subject": _local(row.get("s")), "value_count": int(row.get("cnt", 0))}
        for row in rows
    ]

    return {
        "test": f"functional({prop_name}): max 1 value per instance",
        "violation_count": len(violations),
        "samples": violations,
    }


def _run_inverse_functional_test(
    g: Graph, prop_name: str, max_violations: int,
) -> dict | None:
    """InverseFunctional test: 동일 값이 2개 이상 주체에서 나타나는지 탐지."""
    _validate_prop_name(prop_name)
    rows = _query(g, f'''
        SELECT ?o (COUNT(?s) AS ?cnt) WHERE {{
            ?s {NS_PREFIX}:{prop_name} ?o
        }} GROUP BY ?o HAVING (COUNT(?s) > 1)
        LIMIT {max_violations}
    ''')

    if not rows:
        return None

    violations = [
        {"value": _local(row.get("o")) or str(row.get("o", ""))[:80],
         "subject_count": int(row.get("cnt", 0))}
        for row in rows
    ]

    return {
        "test": f"inverseFunctional({prop_name}): each value maps to at most 1 subject",
        "violation_count": len(violations),
        "samples": violations,
    }


def generate_schema_driven_tests(use_inferred: bool = False, max_violations_per_test: int = 5) -> str:
    """RDFUnit 방식 — T-Box 선언에서 A-Box 프로퍼티별 테스트를 자동 생성·실행한다.

    T-Box의 domain/range/datatype/functional 선언 각각에 대해 SPARQL 테스트를 생성하고
    A-Box에 대해 실행하여 위반을 탐지한다. (Kontokostas et al., 2014 "Test-Driven Evaluation of Linked Data Quality")

    Args:
        use_inferred: True면 추론 결과 그래프 사용.
        max_violations_per_test: 테스트당 최대 위반 샘플 수.
    """
    try:
        start = time.monotonic()

        g = _load_graph(use_inferred)

        # T-Box 로드 (캐시 재사용)
        if not os.path.exists(TBOX_PATH):
            return json.dumps({
                "success": False,
                "error": f"T-Box 파일 없음: {TBOX_PATH}",
                "hint": "generate_tbox를 먼저 실행하세요.",
            }, ensure_ascii=False, indent=2)
        from domain.tbox_utils import load_tbox
        tbox = load_tbox(TBOX_PATH)

        # 프로퍼티 선언 추출
        prop_decls = _extract_tbox_property_declarations(tbox)
        superclass_map = _build_superclass_map(tbox)

        test_count = 0
        passed_count = 0
        failed_tests: list[dict] = []

        for prop_name, decl in prop_decls.items():
            # 1) Domain test
            if decl.get("domain"):
                test_count += 1
                result = _run_domain_test(g, prop_name, decl, superclass_map, max_violations_per_test)
                if result:
                    failed_tests.append(result)
                else:
                    passed_count += 1

            # 2) Range test (OP)
            if decl["kind"] == "OP" and decl.get("range_class"):
                test_count += 1
                result = _run_range_test_op(g, prop_name, decl, superclass_map, max_violations_per_test)
                if result:
                    failed_tests.append(result)
                else:
                    passed_count += 1

            # 3) Range test (DP datatype)
            if decl["kind"] == "DP" and decl.get("range_datatype"):
                test_count += 1
                result = _run_range_test_dp(g, prop_name, decl, max_violations_per_test)
                if result:
                    failed_tests.append(result)
                else:
                    passed_count += 1

            # 4) Functional test
            if decl.get("functional"):
                test_count += 1
                result = _run_functional_test(g, prop_name, max_violations_per_test)
                if result:
                    failed_tests.append(result)
                else:
                    passed_count += 1

            # 5) InverseFunctional test
            if decl.get("inverse_functional"):
                test_count += 1
                result = _run_inverse_functional_test(g, prop_name, max_violations_per_test)
                if result:
                    failed_tests.append(result)
                else:
                    passed_count += 1

        duration = round(time.monotonic() - start, 1)
        failed_count = len(failed_tests)

        return json.dumps({
            "success": True,
            "passed": failed_count == 0,
            "test_count": test_count,
            "passed_count": passed_count,
            "failed_count": failed_count,
            "duration_seconds": duration,
            "source": "inferred" if use_inferred else "merge",
            "properties_analyzed": len(prop_decls),
            "failed_tests": failed_tests,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


# ── 인스턴스 품질 측정 (Zaveri 2016 + Debattista 2016) ──────


def _load_abox_stats() -> dict | None:
    """A-Box 통계 JSON을 로드한다 (generate_abox에서 생성)."""
    stats_path = os.path.join(GENERATED_ABOX_DIR, "abox_stats.json")
    if not os.path.exists(stats_path):
        return None
    try:
        with open(stats_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _richness_to_score(richness: float) -> float:
    """description richness (인스턴스당 실제/기대 프로퍼티 비율) → 0~100 점.

    ``richness == 1.0`` 은 "기대한 만큼 채워졌다" 이고 만점이다. 1.0 을 넘는 것은
    기대보다 **더** 서술적이라는 뜻이므로 감점 대상이 아니다.

    ## 이전 식은 1.0 을 넘는 순간 절반으로 떨어졌다 (2026-08-29 실측)

        richness * 100                    if richness <= 1     # 0.97 → 97.0
        min(richness / 2 * 100, 100)      if richness  > 1     # 1.01 → 50.5 (!)

    1.0 에서 100점이던 것이 1.01 에서 50.5점이 되고 2.0 에서야 100점으로 복귀하는
    V자 함정이다. 파이프라인 재실행에서 richness 가 0.97 → 1.23 으로 **개선**됐는데
    그 때문에 35.5점을 잃어 총점이 95.5 → 89.6 으로 하락했다 — 나머지 4개 메트릭은
    같거나 개선(property_completeness 80.5 → 86.7)이었다. 즉 점수 하락이 품질 저하로
    오독될 수 있었다.

    지금은 1.0 이상을 만점으로 clamp 한다 — 단조 증가이고 불연속이 없다.
    """
    if richness <= 0:
        return 0.0
    return min(richness * 100, 100.0)


def measure_instance_quality(use_inferred: bool = False) -> str:
    """A-Box 인스턴스 품질을 정량적으로 측정한다.

    Zaveri et al. (2016) "Quality Assessment for Linked Data" +
    Debattista et al. (2016) "Luzzu" 기반 메트릭:

    1. Population Completeness: CSV 행 수 대비 인스턴스 수 (abox_stats.json 참조)
    2. Property Completeness: 인스턴스당 채움 프로퍼티 비율
    3. Description Richness: 인스턴스당 실제 프로퍼티 수 / 스키마 기대 프로퍼티 수
    4. Interlinking: ObjectProperty로 연결된 인스턴스 비율
    5. Datatype Consistency: 선언된 XSD 타입과 실제 값 타입 일치율

    Args:
        use_inferred: True면 추론 결과 그래프 사용.
    """
    try:
        start = time.monotonic()

        g = _load_graph(use_inferred)

        # T-Box 로드 (캐시 재사용)
        if not os.path.exists(TBOX_PATH):
            return json.dumps({
                "success": False,
                "error": f"T-Box 파일 없음: {TBOX_PATH}",
                "hint": "generate_tbox를 먼저 실행하세요.",
            }, ensure_ascii=False, indent=2)
        from domain.tbox_utils import load_tbox
        tbox = load_tbox(TBOX_PATH)

        # abox_stats.json 로드 (CSV 행 수 참조용)
        abox_stats = _load_abox_stats()
        per_class_stats = abox_stats.get("per_class", {}) if abox_stats else {}

        # T-Box에서 클래스별 기대 프로퍼티(DP+OP) 추출
        class_expected_props: dict[str, set[str]] = defaultdict(set)
        prop_range_datatypes: dict[str, str] = {}

        for prop_type_uri in [OWL.ObjectProperty, OWL.DatatypeProperty]:
            for prop in tbox.subjects(RDF.type, prop_type_uri):
                if not isinstance(prop, URIRef) or not str(prop).startswith(DOMAIN_NS):
                    continue
                prop_name = _local(str(prop))
                for domain_cls in tbox.objects(prop, RDFS.domain):
                    if isinstance(domain_cls, URIRef) and str(domain_cls).startswith(DOMAIN_NS):
                        cls_name = _local(str(domain_cls))
                        class_expected_props[cls_name].add(prop_name)
                # DP range datatype 기록
                if prop_type_uri == OWL.DatatypeProperty:
                    for r in tbox.objects(prop, RDFS.range):
                        if isinstance(r, URIRef) and str(r).startswith(str(XSD)):
                            prop_range_datatypes[prop_name] = str(r)
                            break

        # ── 메트릭 계산 ──────────────────────────────

        # 1. Population Completeness (클래스별)
        pop_completeness: dict[str, dict] = {}
        for cls_name, stats in per_class_stats.items():
            csv_rows = stats.get("csv_rows", 0)
            inst_count = stats.get("instance_count", 0)
            if csv_rows > 0:
                pop_completeness[cls_name] = {
                    "csv_rows": csv_rows,
                    "instances": inst_count,
                    "completeness": round(inst_count / csv_rows * 100, 1),
                }

        avg_pop = (
            round(statistics.mean([v["completeness"] for v in pop_completeness.values()]), 1)
            if pop_completeness else 0
        )

        # 2. Property Completeness + 3. Description Richness (인스턴스 순회)
        class_prop_fill: dict[str, list[float]] = defaultdict(list)
        class_desc_richness: dict[str, list[float]] = defaultdict(list)
        total_instances = 0
        instances_with_op = 0

        # OP URI 집합 (interlinking 판정용)
        op_uris: set[URIRef] = set()
        for prop in tbox.subjects(RDF.type, OWL.ObjectProperty):
            if isinstance(prop, URIRef) and str(prop).startswith(DOMAIN_NS):
                op_uris.add(prop)

        for cls_name, expected_props in class_expected_props.items():
            if not expected_props:
                continue
            cls_uri = URIRef(f"{DOMAIN_NS}{cls_name}")
            expected_count = len(expected_props)

            for inst in g.subjects(RDF.type, cls_uri):
                total_instances += 1

                # 인스턴스의 모든 프로퍼티 수집
                inst_props: set[str] = set()
                has_op = False
                for p, _ in g.predicate_objects(inst):
                    if str(p).startswith(DOMAIN_NS):
                        inst_props.add(_local(str(p)))
                    if p in op_uris:
                        has_op = True

                if has_op:
                    instances_with_op += 1

                # 채움 비율 (기대 프로퍼티 중 실제로 있는 비율)
                filled = len(inst_props & expected_props)
                fill_rate = filled / expected_count if expected_count > 0 else 0
                class_prop_fill[cls_name].append(fill_rate)

                # Description richness (실제 프로퍼티 수 / 기대 프로퍼티 수)
                actual_count = len(inst_props)
                richness = actual_count / expected_count if expected_count > 0 else 0
                class_desc_richness[cls_name].append(richness)

        # 클래스별 Property Completeness 평균
        prop_completeness_per_class: dict[str, float] = {}
        for cls_name, rates in class_prop_fill.items():
            if rates:
                prop_completeness_per_class[cls_name] = round(statistics.mean(rates) * 100, 1)
        avg_prop_completeness = (
            round(statistics.mean(prop_completeness_per_class.values()), 1)
            if prop_completeness_per_class else 0
        )

        # 클래스별 Description Richness 평균
        desc_richness_per_class: dict[str, float] = {}
        for cls_name, rates in class_desc_richness.items():
            if rates:
                desc_richness_per_class[cls_name] = round(statistics.mean(rates), 2)
        avg_desc_richness = (
            round(statistics.mean(desc_richness_per_class.values()), 2)
            if desc_richness_per_class else 0
        )

        # 4. Interlinking
        interlinking_pct = (
            round(instances_with_op / max(total_instances, 1) * 100, 1)
        )

        # 5. Datatype Consistency
        dt_total = 0
        dt_match = 0
        for prop_name, expected_dt_uri in prop_range_datatypes.items():
            prop_uri = URIRef(f"{DOMAIN_NS}{prop_name}")
            for _, _, obj in g.triples((None, prop_uri, None)):
                if not isinstance(obj, Literal):
                    continue
                dt_total += 1
                actual_dt = str(obj.datatype) if obj.datatype else None
                if actual_dt == expected_dt_uri:
                    dt_match += 1

        dt_consistency_pct = round(dt_match / max(dt_total, 1) * 100, 1)

        # ── 종합 점수 ──────────────────────────────
        # 5개 메트릭의 가중 평균 (0~100)
        scores = [
            avg_pop,
            avg_prop_completeness,
            _richness_to_score(avg_desc_richness),
            interlinking_pct,
            dt_consistency_pct,
        ]
        overall_score = round(statistics.mean(scores), 1) if scores else 0

        duration = round(time.monotonic() - start, 1)

        return json.dumps({
            "success": True,
            "overall_score": overall_score,
            "duration_seconds": duration,
            "source": "inferred" if use_inferred else "merge",
            "total_instances_analyzed": total_instances,
            "metrics": {
                "population_completeness": {
                    "avg_pct": avg_pop,
                    "per_class": dict(sorted(
                        pop_completeness.items(),
                        key=lambda x: x[1]["completeness"],
                    )[:20]),
                },
                "property_completeness": {
                    "avg_pct": avg_prop_completeness,
                    "per_class": dict(sorted(
                        prop_completeness_per_class.items(),
                        key=lambda x: x[1],
                    )[:20]),
                },
                "description_richness": {
                    "avg_ratio": avg_desc_richness,
                    "per_class": dict(sorted(
                        desc_richness_per_class.items(),
                        key=lambda x: x[1],
                    )[:20]),
                },
                "interlinking": {
                    "pct": interlinking_pct,
                    "instances_with_op": instances_with_op,
                    "total_instances": total_instances,
                },
                "datatype_consistency": {
                    "pct": dt_consistency_pct,
                    "matching_values": dt_match,
                    "total_values": dt_total,
                },
            },
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)
