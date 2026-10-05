"""Quality threshold 중앙 로더 + TypedDict 시그니처.

rules/policy/quality_thresholds.json을 단일 진실원(single source of truth)로
사용. 기존 kg_validation.TIER_THRESHOLDS 등 모듈 레벨 dict를 대체.

로드 실패 시 코드 내 safe defaults로 폴백 (프로젝트 환경에서 rules/
디렉토리가 없는 단위 테스트용).
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import TypedDict

from domain.rules_paths import RULES_ROOT, rules_path

logger = logging.getLogger(__name__)


class TierThresholds(TypedDict, total=False):
    dangling_rate: float
    property_coverage: int
    no_instance_allowed: bool
    concentration_alert: float
    master_orphan_rate: float


class ReportLimits(TypedDict, total=False):
    samples_per_check: int
    violations_per_property: int
    provenance_traces: int


class RegressionThresholds(TypedDict, total=False):
    total_score_warning_delta: int
    total_score_critical_delta: int
    pass_rate_warning_delta: int
    pass_rate_critical_delta: int
    violations_count_warning_increase: int
    violations_count_critical_increase: int


_DEFAULTS: dict = {
    "class_tiers": {
        # True reference master (EquipmentMaster, SupplierMaster 등).
        # catalog 타입 master 는 50%까지 자연스럽게 미사용될 수 있음.
        "master": {"dangling_rate": 0.01, "property_coverage": 80,
                   "no_instance_allowed": False, "concentration_alert": 0.95,
                   "master_orphan_rate": 0.50},
        # Reference catalog (EnergySourceMaster, MeasurementUnit 등). 전체 엔트리
        # 중 실제 사용되는 비율이 낮은 게 자연스러움. domain_config.validation.
        # catalog_classes 로 명시 선언된 클래스에만 적용.
        "catalog": {"dangling_rate": 0.01, "property_coverage": 80,
                    "no_instance_allowed": False, "concentration_alert": 0.95,
                    "master_orphan_rate": 0.85},
        # 트랜잭션성 인스턴스 (ProductionPlan, MaintenanceHistory, PurchaseOrder
        # 등) 가 master_data.ttl 에 섞이면 이 tier 로 떨어진다. 트랜잭션은
        # 시계열이라 상당 비율이 참조 없이 존재하는 게 자연 — 60% 임계치.
        "transaction": {"dangling_rate": 0.05, "property_coverage": 50,
                        "no_instance_allowed": False, "concentration_alert": 0.85,
                        "master_orphan_rate": 0.60},
        "inferred": {"dangling_rate": 0.10, "property_coverage": 30,
                     "no_instance_allowed": True, "concentration_alert": 0.90,
                     "master_orphan_rate": 0.70},
    },
    "report_limits": {
        "samples_per_check": 20,
        "violations_per_property": 5,
        "provenance_traces": 10,
    },
    "regression": {
        "total_score_warning_delta": 5,
        "total_score_critical_delta": 10,
        "pass_rate_warning_delta": 10,
        "pass_rate_critical_delta": 20,
        "violations_count_warning_increase": 3,
        "violations_count_critical_increase": 6,
    },
}


_RULES_DIR = RULES_ROOT
_THRESHOLDS_PATH = rules_path("quality_thresholds.json", base=_RULES_DIR)


def _thresholds_signature() -> tuple:
    """``(경로, mtime, size)`` — 캐시 키.

    인자 없는 함수에 ``lru_cache`` 를 걸면 키가 빈 튜플이라 파일을 프로세스당 딱
    한 번만 읽는다. CLAUDE.md §4 의 복구 루프는 "S9 FAIL → 임계치 조정 → 재실행"
    인데, 장수명 MCP 서버는 첫 읽기를 계속 들고 있어 **같은 FAIL 이 반복된다**.
    운영자는 이미 완화한 임계치를 만족시키려고 데이터를 고치기 시작한다
    (2026-08-08 규명). 서명을 실인자로 넘겨 파일 변경 시 캐시가 무효화되게 한다.
    """
    try:
        stat = os.stat(_THRESHOLDS_PATH)
    except OSError:
        return (_THRESHOLDS_PATH, 0.0, 0)
    return (_THRESHOLDS_PATH, stat.st_mtime, stat.st_size)


def _load() -> dict:
    """quality_thresholds.json + defaults 병합 결과. 파일 변경 시 자동 재로드."""
    return _load_cached(_thresholds_signature())


@lru_cache(maxsize=4)
def _load_cached(signature: tuple) -> dict:
    if not os.path.exists(_THRESHOLDS_PATH):
        logger.debug("quality_thresholds.json 없음 — defaults 사용: %s", _THRESHOLDS_PATH)
        return _DEFAULTS
    try:
        with open(_THRESHOLDS_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        # Deep merge: section 별로 nested dict 도 defaults 로 보완.
        # class_tiers 가 "master": {...} 처럼 중첩이라 shallow merge 로는
        # 새 키(master_orphan_rate 등) 가 유실된다.
        merged: dict = {}
        for section, defaults in _DEFAULTS.items():
            loaded_section = loaded.get(section, {})
            if isinstance(defaults, dict) and isinstance(loaded_section, dict):
                out_section: dict = {}
                # 모든 nested key (tier 이름) 에 대해 defaults + loaded 병합
                all_keys = set(defaults.keys()) | set(loaded_section.keys())
                for key in all_keys:
                    d_val = defaults.get(key, {})
                    l_val = loaded_section.get(key, {})
                    if isinstance(d_val, dict) and isinstance(l_val, dict):
                        out_section[key] = {**d_val, **l_val}
                    else:
                        out_section[key] = l_val if key in loaded_section else d_val
                merged[section] = out_section
            else:
                merged[section] = loaded_section if section in loaded else defaults
        return merged
    except Exception as e:
        logger.warning("quality_thresholds.json 로드 실패, defaults 사용: %s", e)
        return _DEFAULTS


def get_tier_thresholds() -> dict[str, TierThresholds]:
    """클래스 티어별 임계값 반환 (master / transaction / inferred)."""
    return {
        t: {k: v for k, v in cfg.items() if not k.startswith("_") and k not in
            ("max_instance_count", "min_instance_count")}
        for t, cfg in _load()["class_tiers"].items()
    }


def get_report_limits() -> ReportLimits:
    return {k: v for k, v in _load()["report_limits"].items() if not k.startswith("_")}  # type: ignore[return-value]


def get_regression_thresholds() -> RegressionThresholds:
    return {k: v for k, v in _load()["regression"].items() if not k.startswith("_")}  # type: ignore[return-value]


def get_master_tier_max_count() -> int:
    """master tier로 분류되는 인스턴스 수 상한."""
    return int(_load()["class_tiers"].get("master", {}).get("max_instance_count", 500))


def reload_cache() -> None:
    """캐시 강제 폐기. 서명 키 덕에 운영에선 불필요 — 테스트가 임시 파일의
    mtime+size 를 재사용할 때만 필요하다."""
    _load_cached.cache_clear()
