"""
온톨로지 품질 개선 모듈 -- T-Box 후처리

generate_tbox() 이후에 호출하여 T-Box TTL 품질을 향상시킨다.

후처리 단계 (주요):
 0. 안티패턴 자동 수정 (transitive+inverseOf 충돌, redundant subClassOf, double-prefix)
 1. AllDisjointClasses 완전화 (도메인별 그룹)
 2. InverseOf 양방향 선언 보장
 3. ObjectProperty 누락 domain 보완
 7. 온톨로지 메타데이터 보강 (owl:imports, versionIRI, dcterms)
 8. xsd:date → xsd:dateTime (HermiT OWL 2 호환)
13. PK FunctionalProperty + OP someValuesFrom 제약
14. 2차 계층 서브그룹 생성
15. 크로스 도메인 ObjectProperty 추가
17. Label synthesis
18. Completeness Annotation (CWA/OWA 브릿지)
19. BNode skolemization
22. Duplicate OP consolidation (R11-M5)
24. Abstract category annotation (R12 C1/H1)
25. OntoClean meta-annotation (R12 C3)
26. Module membership annotation (R12 M1)
27. owl:hasKey injection (R13 — CSV PK → OWL axiom)
28. owl:disjointUnionOf promotion (R13 — 분류 완전성 강제)
"""

import csv
import glob
import hashlib
import json
import logging
import os
import re

import rdflib
import rdflib.collection
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

from config import SOURCE_RAWDATA_DIR, TBOX_PATH
from domain.graph_utils import is_ancestor_of, resolve_entity_name
from domain.namespaces import (
    DOMAIN_NS,
    DOMAIN_NS_OBJ,
    ONTOLOGY_URI,
)
from domain.rules_paths import RULES_ROOT, rules_path
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.common import atomic_write, error_response

# Re-use abstract-group hint loader from T-Box generation (T4 ODP auto-apply).
# Aliased as module-level name so tests can patch via
# `tools.ontology_quality._load_abstract_group_hints`.
from tools.tbox_generation import _load_abstract_group_hints as _load_abstract_group_hints

logger = logging.getLogger(__name__)

# ── 네임스페이스 ─────────────────────────────────

DC = Namespace("http://purl.org/dc/terms/")
ONT = URIRef(ONTOLOGY_URI)

# ── 규칙 데이터 로드 ─────────────────────────────

_RULES_DIR = RULES_ROOT


def _load_object_properties_from_tbox() -> list:
    """생성된 T-Box에서 ObjectProperty 정보를 동적 추출한다."""
    from domain.tbox_utils import load_object_properties
    return load_object_properties()


def _load_config_inverse_pairs() -> list[tuple[str, str]]:
    """``rules/domain/domain_config.json`` 의 ``inverse_pairs``. 없으면 빈 리스트.

    도메인-중립 기본값은 "선언 없음" 이다 — 어떤 inverse 쌍이 정본인지는 배포마다
    다르므로 코드가 디스크 산출물에서 추측하면 안 된다.
    """
    try:
        from domain.namespaces import DOMAIN_CONFIG
        raw = DOMAIN_CONFIG.get("inverse_pairs") or []
    except Exception as exc:  # noqa: BLE001 — 설정 부재가 S3 를 막아선 안 된다
        logger.debug("inverse_pairs 로드 skip: %s", exc)
        return []
    if not isinstance(raw, list):
        return []
    return [
        (str(x[0]), str(x[1])) for x in raw
        if isinstance(x, list | tuple) and len(x) == 2 and x[0] and x[1]
    ]


def _build_inverse_pairs() -> list:
    """설정에 선언된 inverse 쌍 목록. **배포 T-Box 를 읽지 않는다.**

    예전에는 ``data/generated/tbox/t_box.ttl`` 을 읽어 S3 가 **자기 이전 출력에
    의존**했다. 실측 (2026-08-11): 같은 S2 초안으로 S3 를 돌려도 배포 파일이
    있으면 5044 트리플, 없으면 5045 가 나오고 **S4 5종 전부가 그 차이에
    무반응** 이다 (품질 이슈 106 / critical 1 / HermiT consistent / SHACL
    conforms — 양쪽 동일). 배포본 자리에 임의의 ``owl:inverseOf zzzGhost`` 쌍만
    담은 파일을 놓으면 그 유령 이름이 **최종 TTL 까지 통과** 한다 (2 트리플) —
    step_15 의 dangling 정리는 cross-domain OP 이름에만 반응하는 부분 방어다.

    **그래프에서 추출하는 안은 폐기했다.** ``step_00`` 의 0b
    (``_fix_asymmetric_inverse``) 가 그래프에 있는 모든 ``owl:inverseOf`` 를 이미
    대칭화하고 미선언 subject 도 이미 거부하며, 그것이 **먼저** 실행된다. 실측:
    그래프 기반 step_02 의 산출물은 step_02 를 아예 끈 것과 isomorphic 동일 —
    0b 의 사본이 된다. 게다가 그래프 추출은 초안의 다중 inverse OP 를 승격해
    ``storedIn`` 에 inverse 파트너 2개(``storesItem`` rng=ItemMaster +
    ``warehouseStores`` rng=InventoryStatus)와 새 비대칭 leg 를 만든다.

    역할 분리: **0b = 그래프에 있는 것 대칭화(data-driven)** /
    **step_02 = 설정에 선언된 것 강제(config-driven)**.

    Returns:
        [("hasEquipmentStatus", "isStatusOf"), ...]
    """
    pairs = list(_load_config_inverse_pairs())
    # 공정 흐름 체인은 S9 검증의 전제라 설정이 비어도 보장한다. 현 철강 예시에는
    # 이 네 이름이 T-Box 에 없어(grep 0건) 확정적 no-op 이다 — 다른 도메인이나
    # tacit 승격 경로를 위해 남긴다.
    known = {(fwd, inv) for fwd, inv in pairs}
    if ("followedBy", "precededBy") not in known:
        pairs.append(("followedBy", "precededBy"))
    if ("performsProcess", "performedByEquipment") not in known:
        pairs.append(("performsProcess", "performedByEquipment"))
    return pairs


def _build_domain_fixes() -> dict:
    """T-Box에서 property name -> domain class 매핑을 추출한다.

    improve_tbox에서 domain이 누락된 ObjectProperty에 자동 부여할 때 사용.

    보조 seed 는 ``rules/domain/domain_config.json`` 의 ``op_domain_seeds``
    (``{"propertyName": "ClassLocalName"}``) 에서만 온다. 예전엔 두 쌍
    (``locatedIn``/``usesEquipment`` → 한 철강 예시 클래스) 이 코드에 박혀
    있었는데, 그 클래스가 없는 도메인에서는 존재하지 않는 클래스를 domain 으로
    선언하고 step_09d 가 그것을 같은 prefix 의 엉뚱한 클래스로 "교정" 해
    inverse 에서 유도될 정답 domain 을 덮어썼다 (2026-08-08 규명).

    Returns:
        ``{property_name: class_local_name}``. seed 는 T-Box 에서 추출된 실측
        매핑을 덮어쓰지 않는다 (``setdefault``).
    """
    props = _load_object_properties_from_tbox()
    fixes = {}
    for p in props:
        name = p.get("name")
        domain = p.get("domain")
        if name and domain:
            fixes[name] = domain
    for prop_name, class_name in _load_op_domain_seeds().items():
        fixes.setdefault(prop_name, class_name)
    return fixes


def _load_op_domain_seeds() -> dict:
    """``rules/domain/domain_config.json`` 의 ``op_domain_seeds`` 매핑. 없으면 빈 dict.

    도메인-중립 기본값은 "seed 없음" 이다 — 어떤 클래스가 존재하는지는 배포마다
    다르므로 코드가 추측하면 안 된다.
    """
    try:
        from domain.namespaces import DOMAIN_CONFIG
        raw = DOMAIN_CONFIG.get("op_domain_seeds") or {}
    except Exception as exc:  # noqa: BLE001 — seed 부재가 S3 를 막아선 안 된다
        logger.debug("op_domain_seeds 로드 skip: %s", exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): str(v) for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and k and v
    }


# ── 도메인별 Disjoint 그룹 (rules/domain/disjoint_groups.json에서 로드) ────

def _load_disjoint_config() -> dict:
    _path = rules_path("disjoint_groups.json", base=_RULES_DIR)
    with open(_path, encoding="utf-8") as f:
        return json.load(f)


def _load_property_chains() -> list[dict]:
    """Load rules/domain/property_chains.json. Missing file → empty list."""
    _path = rules_path("property_chains.json", base=_RULES_DIR)
    if not os.path.exists(_path):
        return []
    with open(_path, encoding="utf-8") as f:
        data = json.load(f) or {}
    return list(data.get("chains") or [])

def _load_fk_patterns() -> dict[str, str]:
    _path = rules_path("fk_patterns.json", base=_RULES_DIR)
    with open(_path, encoding="utf-8") as f:
        return json.load(f).get("patterns", {})

_FK_PATTERNS = _load_fk_patterns()


def _load_hierarchy_config() -> dict:
    """design_patterns.json에서 도메인 계층 설정을 로드한다."""
    dp_path = rules_path("design_patterns.json", base=_RULES_DIR)
    with open(dp_path, encoding="utf-8") as f:
        dp = json.load(f)
    # sub_groups가 없는 도메인의 직접 자식 목록 (JSON에 sub_groups로 표현되지 않는 경우)
    _DIRECT_CHILDREN: dict[str, list[str]] = {
        "생산": ["ProductMaster", "ProductionPlan", "ProductionResult"],
        "정비": ["MaintenanceHistory", "FailureCause"],
    }
    config = {}
    for domain_ko, cfg in dp.get("domain_hierarchy", {}).items():
        abstract = cfg.get("abstract_class")
        if not abstract:
            continue
        iof_parent = cfg.get("iof_parent", "")
        # prefixed name → IRI 해석은 **공용 헬퍼만** 쓴다 (CLAUDE.md 규칙).
        #
        # 예전에는 여기서 IRI 를 하드코딩했고, 그 값이 실재하지 않는 모듈별
        # 네임스페이스(``/ontology/core/Core/`` 등)였다. 실측 (2026-08-18):
        # ``domain/namespaces.py`` 상수와 T-Box 파일을 정본으로 고친 뒤에도 이
        # 블록이 **매 S3 실행마다 구 IRI 를 8건 재주입** 해서 마이그레이션
        # 스텝(step_02b)이 무한히 같은 일을 반복했다. 사본이 진실을 되돌리는
        # 전형적 형태다.
        parent_uri = (
            resolve_entity_name(iof_parent) if iof_parent else ""
        )
        # Collect all children from sub_groups
        children = []
        for sg_children in cfg.get("sub_groups", {}).values():
            children.extend(sg_children)
        # sub_groups가 없는 도메인은 직접 자식 목록 사용
        if not children and domain_ko in _DIRECT_CHILDREN:
            children = _DIRECT_CHILDREN[domain_ko]
        config[domain_ko] = {
            "class": abstract,
            "label_en": abstract.replace("Management", " Management").replace("Step", " Step").strip(),
            "label_ko": domain_ko,
            "comment_ko": f"{domain_ko} 도메인 상위 클래스",
            "parent": parent_uri,
            "children_tables": children,
        }
    return config


def _load_sub_group_config() -> dict:
    """design_patterns.json에서 서브그룹 설정을 로드한다."""
    dp_path = rules_path("design_patterns.json", base=_RULES_DIR)
    with open(dp_path, encoding="utf-8") as f:
        dp = json.load(f)
    config = {}
    for domain_ko, cfg in dp.get("domain_hierarchy", {}).items():
        abstract = cfg.get("abstract_class")
        if not abstract or not cfg.get("sub_groups"):
            continue
        subgroups = []
        for sg_name, children in cfg["sub_groups"].items():
            subgroups.append({
                "class": sg_name,
                "label_en": sg_name,
                "label_ko": sg_name,
                "comment_ko": f"{domain_ko} - {sg_name}",
                "children": children,
            })
        config[abstract] = subgroups
    return config


def _load_cross_domain_ops() -> list:
    """design_patterns.json에서 크로스 도메인 OP 이름 목록을 로드한다.

    ``cross_domain_ops`` 는 두 형태를 지원한다:

    - **문자열** — 이름만. domain/range 메타는 ``_CROSS_DOMAIN_OP_META`` (코드)
      에서 찾는다. 메타가 없으면 ``step_15`` 가 조용히 skip 한다.
    - **객체** — ``{"name", "domain", "range", "label_en", "label_ko",
      "comment_ko", "inverse"}``. 설정에 메타까지 있으면 코드 수정 없이 도메인을
      바꿀 수 있다. ``_load_cross_domain_op_meta`` 가 이 형태를 읽는다.

    이름만 주는 형태는 **도메인 전환 시 어긋난다** — 2026-07-25 실측: 배포 설정의
    OP 22개가 전부 코드 META 에 없어 ``step_15`` 가 전량 skip 했다 (교집합 0).
    """
    dp_path = rules_path("design_patterns.json", base=_RULES_DIR)
    with open(dp_path, encoding="utf-8") as f:
        dp = json.load(f)
    ops = []
    for _domain_ko, cfg in dp.get("domain_hierarchy", {}).items():
        for entry in cfg.get("cross_domain_ops", []):
            if isinstance(entry, dict):
                name = entry.get("name")
                if name:
                    ops.append(name)
            elif entry:
                ops.append(entry)
    return ops


def _load_cross_domain_op_meta() -> dict[str, tuple[str, str, str, str, str, str]]:
    """설정에 선언된 cross-domain OP 메타 — 코드 하드코딩 META 를 덮어쓴다.

    ``design_patterns.json`` 의 ``cross_domain_ops`` 항목이 객체 형태로
    domain/range 를 제공하면 그것을 쓴다. 도메인을 바꿀 때 소스를 고치지 않아도
    되게 하는 경로다 (문자열 형태만 쓰면 코드의 데모 META 에 의존한다).

    Returns:
        ``{op_name: (domain, range, label_en, label_ko, comment_ko, inverse)}``
    """
    dp_path = rules_path("design_patterns.json", base=_RULES_DIR)
    try:
        with open(dp_path, encoding="utf-8") as handle:
            dp = json.load(handle)
    except Exception as exc:  # noqa: BLE001 — 설정 부재가 파이프라인을 막지 않는다
        logger.debug("design_patterns 로드 skip (cross-domain meta): %s", exc)
        return {}

    meta: dict[str, tuple[str, str, str, str, str, str]] = {}
    for _domain_ko, cfg in dp.get("domain_hierarchy", {}).items():
        for entry in cfg.get("cross_domain_ops", []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            domain = entry.get("domain")
            range_ = entry.get("range")
            if not (name and domain and range_):
                continue          # domain/range 없으면 OP 를 만들 수 없다
            meta[name] = (
                domain, range_,
                entry.get("label_en") or _local_name_to_human(name),
                entry.get("label_ko") or name,
                entry.get("comment_ko") or "",
                entry.get("inverse") or "",
            )
    return meta


# OP 이름 → (domain, range, label_en, label_ko, comment_ko, inverse) 매핑
# design_patterns.json 이 이름만 주는 경우의 폴백 메타 (데모 철강 예시).
# 설정이 객체 형태로 domain/range 를 주면 _load_cross_domain_op_meta 가 우선한다.
_CROSS_DOMAIN_OP_META: dict[str, tuple[str, str, str, str, str, str]] = {
    "requiresMaintenance": ("EquipmentMaster", "MaintenanceHistory",
        "requires maintenance", "정비가 필요하다", "설비가 정비 이력을 가지는 관계", "isMaintenanceOf"),
    "triggersMaintenanceAction": ("AlarmEvents", "MaintenanceHistory",
        "triggers maintenance action", "정비 조치를 유발한다", "알람이 정비 조치를 유발하는 관계", "maintenanceTriggeredBy"),
    "hasEnergyProfile": ("EquipmentMaster", "EnergyEfficiency",
        "has energy profile", "에너지 프로파일을 가진다", "설비의 에너지 효율 프로파일", "isEnergyProfileOf"),
    "processConsumesElectricity": ("ManufacturingProcessStep", "ElectricalConsumption",
        "process consumes electricity", "공정이 전력을 소비한다", "제조 공정의 전력 소비", "electricityConsumedByProcess"),
    "processConsumesGas": ("ManufacturingProcessStep", "GasEnergy",
        "process consumes gas", "공정이 가스를 소비한다", "제조 공정의 가스 에너지 소비", "gasConsumedByProcess"),
    "processConsumesSteam": ("ManufacturingProcessStep", "SteamEnergy",
        "process consumes steam", "공정이 스팀을 소비한다", "제조 공정의 스팀 에너지 소비", "steamConsumedByProcess"),
    "producesQualityResult": ("ManufacturingProcessStep", "QualityMeasurement",
        "produces quality result", "품질 결과를 생성한다", "제조 공정의 품질 측정 결과", "qualityResultOf"),
    "hasChemicalComposition": ("ProductMaster", "ChemicalAnalysis",
        "has chemical composition", "화학 성분을 가진다", "제품의 화학 성분 분석", "isChemicalCompositionOf"),
    "hasDimensionalSpec": ("ProductMaster", "DimensionalData",
        "has dimensional specification", "치수 규격을 가진다", "제품의 치수 측정 데이터", "isDimensionalSpecOf"),
    "hasNDTResult": ("ProductMaster", "NDTResults",
        "has NDT result", "비파괴검사 결과를 가진다", "제품의 비파괴검사 결과", "isNDTResultOf"),
    "requiresMaterial": ("ProductionPlan", "ItemMaster",
        "requires material", "자재가 필요하다", "생산 계획에 필요한 자재", "materialRequiredBy"),
    "producesForInventory": ("ProductionResult", "InventoryTransaction",
        "produces for inventory", "재고로 입고한다", "생산 실적의 재고 입고", "inventoryFromProduction"),
    "generatesEmission": ("ManufacturingProcessStep", "GHGEmission",
        "generates emission", "배출을 발생시킨다", "제조 공정의 온실가스 배출", "emissionGeneratedBy"),
    "generatesWaste": ("ManufacturingProcessStep", "WasteManagement",
        "generates waste", "폐기물을 발생시킨다", "제조 공정의 폐기물 발생", "wasteGeneratedBy"),
    "monitorsAirQuality": ("MonitoringPointMaster", "AirEmissionMonitoring",
        "monitors air quality", "대기 품질을 모니터링한다", "측정 포인트의 대기 배출 모니터링", "airQualityMonitoredBy"),
    "monitorsWaterQuality": ("MonitoringPointMaster", "WaterQualityMonitoring",
        "monitors water quality", "수질을 모니터링한다", "측정 포인트의 수질 모니터링", "waterQualityMonitoredBy"),
    "monitorsNoise": ("MonitoringPointMaster", "NoiseVibrationMonitoring",
        "monitors noise vibration", "소음진동을 모니터링한다", "측정 포인트의 소음진동 모니터링", "noiseMonitoredBy"),
    "storedIn": ("ItemMaster", "WarehouseMaster",
        "stored in", "보관된다", "자재의 창고 보관 관계", "storesItem"),
    "transportedVia": ("ItemMaster", "Transportation",
        "transported via", "운송된다", "자재의 운송 관계", "transportsItem"),
    "hasRealTimeData": ("TagMaster", "RealTimeData",
        "has real time data", "실시간 데이터를 가진다", "센서 태그의 실시간 측정 데이터", "realTimeDataOf"),
    "hasSoilImpact": ("EquipmentMaster", "SoilMonitoring",
        "has soil impact", "토양 영향이 있다", "설비 운영의 토양 영향", "soilImpactFrom"),
    "meetsQualitySpec": ("ProductionResult", "QualityMeasurement",
        "meets quality specification", "품질 규격을 충족한다", "생산 실적의 품질 기준 충족", "qualitySpecMetBy"),
    # R25: CQ coverage 개선 cross-domain OP 추가 (환경·에너지 도메인)
    "correlatedWithWaste": ("AirEmissionMonitoring", "WasteManagement",
        "correlated with waste", "폐기물과 연관된다",
        "대기 배출 모니터링 지점과 설비의 폐기물 발생이 같은 설비에서 관찰되는 관계",
        "wasteCorrelatedWithAirEmission"),
    "hasSteamEnergySource": ("SteamEnergy", "EnergySourceMaster",
        "has steam energy source", "스팀 에너지원을 가진다",
        "스팀 에너지 소비 기록이 가리키는 에너지원 마스터",
        "isSteamEnergySourceOf"),
    "hasGasEnergySource": ("GasEnergy", "EnergySourceMaster",
        "has gas energy source", "가스 에너지원을 가진다",
        "가스 에너지 소비 기록이 가리키는 에너지원 마스터",
        "isGasEnergySourceOf"),
    # R2 (2026-05-12): CQ06/09/10/11 cross-domain 연결 자동 복원
    "hasGHGAirEmissionMonitoring": ("GHGEmission", "AirEmissionMonitoring",
        "has GHG air emission monitoring", "온실가스 대기 배출 모니터링 보유",
        "GHG 배출 이벤트와 해당 배출원 설비의 대기 배출 모니터링 데이터를 연결하는 프로퍼티.",
        "isAirEmissionMonitoringOfGHG"),
    "hasFuelAirEmissionMonitoring": ("FuelConsumption", "AirEmissionMonitoring",
        "has fuel air emission monitoring", "연료 연소 대기 배출 모니터링 보유",
        "연료 소비 이벤트와 해당 설비의 대기 배출 모니터링 데이터를 연결하는 프로퍼티.",
        "isAirEmissionMonitoringOfFuel"),
    "hasEnergySourceAirEmissionMonitoring": ("EnergySourceMaster", "AirEmissionMonitoring",
        "has energy source air emission monitoring", "에너지원 대기 배출 모니터링 보유",
        "에너지원 (가스/석탄/전기 등) 과 해당 에너지원 사용 설비의 대기 배출 모니터링을 연결.",
        "isAirEmissionMonitoringOfEnergySource"),
    "hasEnergySourceMonitoringPoint": ("EnergySourceMaster", "MonitoringPointMaster",
        "has energy source monitoring point", "에너지원 모니터링 지점 보유",
        "에너지원 설비에 설치된 환경 모니터링 지점을 연결하는 프로퍼티.",
        "isMonitoringPointOfEnergySource"),
    "hasRealTimeDataChemicalAnalysis": ("RealTimeData", "ChemicalAnalysis",
        "has real time data chemical analysis", "실시간 데이터-화학성분 분석 연결",
        "실시간 센서 데이터와 동일 배치/시점의 화학 성분 분석 결과를 연결 (배치별 성분 조성 추적).",
        "hasChemicalAnalysisRealTimeData"),
    "hasTagMasterChemicalAnalysis": ("TagMaster", "ChemicalAnalysis",
        "has tag master chemical analysis", "태그-화학성분 분석 연결",
        "특정 센서 태그가 모니터링하는 공정의 화학 성분 분석 결과를 연결.",
        "hasChemicalAnalysisTagMaster"),
    "hasTagMasterProduct": ("TagMaster", "ProductMaster",
        "has tag master product", "태그-제품 연결",
        "센서 태그가 측정하는 공정에서 생산되는 제품을 연결.",
        "hasProductMasterTag"),
    "hasNoiseVibrationSteamEnergy": ("NoiseVibrationMonitoring", "SteamEnergy",
        "has noise vibration steam energy", "소음진동-스팀에너지 연결",
        "소음·진동 모니터링 지점 인근 설비의 스팀 에너지 소비 데이터를 연결.",
        "hasSteamEnergyNoiseVibration"),
    "hasNoiseVibrationWasteManagement": ("NoiseVibrationMonitoring", "WasteManagement",
        "has noise vibration waste management", "소음진동-폐기물관리 연결",
        "소음·진동 모니터링 지점 인근 설비의 폐기물 관리 데이터를 연결.",
        "hasWasteManagementNoiseVibration"),
    "hasFailureCauseInventoryTransaction": ("FailureCause", "InventoryTransaction",
        "has failure cause inventory transaction", "고장원인-재고거래 연결",
        "설비 고장으로 유발된 정비 부품 재고 거래 (수리에 쓰인 부품 출고) 를 연결.",
        "isInventoryTransactionOfFailure"),
    # design_patterns.json에만 있고 기존 하드코딩에 없던 OP (메타 정보 없음 → 스킵 대상)
}


_DISJOINT_CFG = _load_disjoint_config()
DISJOINT_GROUPS = [g["classes"] for g in _DISJOINT_CFG["groups"]]
PROCESS_STEP_CLASSES = _DISJOINT_CFG.get("process_step_classes", [])

# ── 헬퍼 ─────────────────────────────────────────


def _add_if_missing(g, s, p, o):
    """트리플이 없으면 추가하고 1 반환, 이미 있으면 0 반환."""
    if (s, p, o) not in g:
        g.add((s, p, o))
        return 1
    return 0


# ── BNode Skolemization ─────────────────────────────


def _remove_rdf_collection(g: Graph, head) -> int:
    """RDF List 의 ``rdf:first``/``rdf:rest`` 트리플을 전부 제거한다.

    중복 컬렉션을 폐기할 때 head 링크만 끊으면 **고아 리스트** 가 남아 다른
    소비자가 그것을 빈 컬렉션으로 읽는다 (실측: HermiT 가 "멤버 0개
    DisjointClasses" 로 읽고 로드를 거부).

    Returns: 제거한 트리플 수.
    """
    removed = 0
    node = head
    seen = set()
    while node is not None and node != RDF.nil and node not in seen:
        seen.add(node)
        nxt = next(g.objects(node, RDF.rest), None)
        for t in list(g.triples((node, RDF.first, None))):
            g.remove(t)
            removed += 1
        for t in list(g.triples((node, RDF.rest, None))):
            g.remove(t)
            removed += 1
        node = nxt
    return removed


def _skolemize_bnodes(g: Graph) -> dict:
    """의미 있는 BNode을 결정론적 named IRI로 변환.

    RDF List 내부 BNode(rdf:first/rdf:rest)는 보존한다.

    처리 대상:
    1. owl:Restriction BNode → ``DOMAIN_NS_OBJ["{cls}_{prop}_{type}"]``
    2. owl:AllDisjointClasses BNode → ``DOMAIN_NS_OBJ["AllDisjoint_{hash8}"]``
    3. equivalentClass/unionOf BNode → ``DOMAIN_NS_OBJ["Union_{cls}_{hash8}"]``

    Args:
        g: rdflib Graph (in-place 수정).

    Returns:
        ``{"total_skolemized": int, "by_type": {"Restriction": int, "AllDisjointClasses": int, "Union": int}}``
    """
    counts = {"Restriction": 0, "AllDisjointClasses": 0, "Union": 0}

    def _replace(old: BNode, new: URIRef) -> None:
        """BNode의 모든 출현(subject/object)을 named IRI로 교체."""
        for p, o in list(g.predicate_objects(old)):
            g.remove((old, p, o))
            g.add((new, p, o))
        for s, p in list(g.subject_predicates(old)):
            g.remove((s, p, old))
            g.add((s, p, new))

    # ── 1. owl:Restriction BNode ────────────────────
    # 의미론적으로 동일한 restriction(동일 class, onProperty, restriction type,
    # target value)은 하나의 URI 로 병합한다. 과거 "충돌 방지" 로직이
    # hash suffix 를 붙여 동일 의미의 restriction 을 여러 개의 별개 클래스로
    # 분기시켜 추론 단계에서 조합 폭발을 유발했다.
    #
    # 병합 키:
    #   (class_local, prop_local, restriction_type, target_value_str)
    # 이미 매핑된 키가 나오면 해당 BNode 를 기존 URI 로 대체하고 중복 triple
    # 은 rdflib.add 의 set 의미로 자동 dedup 된다.
    restriction_key_map: dict[tuple, URIRef] = {}
    merged_duplicates = 0
    for bnode in list(g.subjects(RDF.type, OWL.Restriction)):
        if not isinstance(bnode, BNode):
            continue

        prop_node = g.value(bnode, OWL.onProperty)
        if prop_node is None:
            continue

        # 참조 클래스 탐색 (subClassOf | equivalentClass)
        # 이 BNode 를 참조하는 클래스 전체를 모은다. 여러 클래스가 같은 BNode 를
        # 공유하면 (Architect 가 동일 restriction 노드를 재사용) 첫 번째 클래스
        # 이름만 붙고 나머지는 **남의 이름표를 상속** 하게 된다.
        # 실측 (2026-07-25): MaterialSpecA 과 MaterialB 이 하나의
        # operationStartDatetime minCardinality BNode 를 공유해
        # ``MaterialSpecA_operationStartDatetime_minCardinality`` 로 스콜렘화
        # 됐고, MaterialB 이 자기 것이 아닌 필수 제약을 강요받아 카디널리티
        # 위반이 발생했다. 그래서 **클래스별로 복제** 한다.
        owner_by_pred: list[tuple[URIRef, URIRef]] = []
        for pred in (RDFS.subClassOf, OWL.equivalentClass):
            for s in list(g.subjects(pred, bnode)):
                if isinstance(s, URIRef):
                    owner_by_pred.append((s, pred))
        if not owner_by_pred:
            continue  # orphan restriction — 건너뜀

        if len(owner_by_pred) > 1:
            # 2번째 이후 소유자에게는 같은 내용의 named restriction 을 새로
            # 만들어 붙이고, BNode 참조는 끊는다. 1번째 소유자는 아래 기존
            # 경로가 처리한다.
            body = [(p, o) for p, o in g.predicate_objects(bnode)]
            for extra_owner, extra_pred in owner_by_pred[1:]:
                extra_local = _local_name(str(extra_owner))
                extra_prop = _local_name(str(prop_node))
                extra_type = "Restriction"
                for rtype in ("someValuesFrom", "hasValue", "allValuesFrom",
                              "minCardinality", "maxCardinality",
                              "minQualifiedCardinality", "maxQualifiedCardinality",
                              "qualifiedCardinality"):
                    if g.value(bnode, OWL[rtype]) is not None:
                        extra_type = rtype
                        break
                clone = DOMAIN_NS_OBJ[f"{extra_local}_{extra_prop}_{extra_type}"]
                for p, o in body:
                    g.add((clone, p, o))
                g.remove((extra_owner, extra_pred, bnode))
                g.add((extra_owner, extra_pred, clone))
                counts["Restriction"] += 1
            logger.info(
                "restriction BNode 를 %d개 클래스가 공유 — 클래스별로 분리: %s",
                len(owner_by_pred),
                [_local_name(str(o)) for o, _ in owner_by_pred],
            )

        cls_node = owner_by_pred[0][0]
        cls_local = _local_name(str(cls_node))
        prop_local = _local_name(str(prop_node))

        # restriction 타입 + target 값
        restriction_type = "Restriction"
        target_value = ""
        for rtype in ("someValuesFrom", "hasValue", "allValuesFrom",
                       "minCardinality", "maxCardinality",
                       "minQualifiedCardinality", "maxQualifiedCardinality",
                       "qualifiedCardinality"):
            target = g.value(bnode, OWL[rtype])
            if target is not None:
                restriction_type = rtype
                target_value = str(target)
                break

        # 병합 키 — 의미론적 동일성 판정
        merge_key = (cls_local, prop_local, restriction_type, target_value)

        if merge_key in restriction_key_map:
            # 이미 같은 의미의 restriction 이 존재 → BNode 를 기존 URI 로 대체
            new_uri = restriction_key_map[merge_key]
            _replace(bnode, new_uri)
            merged_duplicates += 1
            continue

        new_uri = DOMAIN_NS_OBJ[f"{cls_local}_{prop_local}_{restriction_type}"]

        # 다른 class 에서 같은 (prop, type, target) 을 먼저 매핑한 경우엔 여전히
        # 이름 충돌이 가능하다. 이 때는 class prefix 가 새 URI 에 이미 포함되어
        # 있으므로 기본 전략은 충돌이 생기지 않지만, 안전망으로 남긴다.
        if (new_uri, None, None) in g and new_uri not in restriction_key_map.values():
            # 동일 URI 가 이미 있지만 우리 매핑이 아니라면, 이름이
            # 같은 다른 정의를 건드리지 않도록 hash suffix 사용.
            hash8 = hashlib.md5(
                f"{cls_local}|{prop_local}|{restriction_type}|{target_value}".encode(),
                usedforsecurity=False,
            ).hexdigest()[:8]
            new_uri = DOMAIN_NS_OBJ[
                f"{cls_local}_{prop_local}_{restriction_type}_{hash8}"
            ]

        restriction_key_map[merge_key] = new_uri
        _replace(bnode, new_uri)
        counts["Restriction"] += 1
    counts["Restriction_merged_duplicates"] = merged_duplicates

    # ── 2. owl:AllDisjointClasses BNode ─────────────
    for bnode in list(g.subjects(RDF.type, OWL.AllDisjointClasses)):
        if not isinstance(bnode, BNode):
            continue

        members_node = g.value(bnode, OWL.members)
        if members_node is None:
            continue

        try:
            members = list(rdflib.collection.Collection(g, members_node))
        except Exception as e:
            logger.debug("AllDisjointClasses collection 파싱 실패: %s", e)
            continue

        sorted_names = sorted(_local_name(str(m)) for m in members if isinstance(m, URIRef))
        if not sorted_names:
            continue

        hash8 = hashlib.md5(
            "_".join(sorted_names).encode(),
            usedforsecurity=False,
        ).hexdigest()[:8]
        new_uri = DOMAIN_NS_OBJ[f"AllDisjoint_{hash8}"]

        # **같은 멤버 집합이면 같은 IRI 로 병합된다** (의도된 동작 — Restriction
        # 경로와 동일). 그런데 각 BNode 의 ``owl:members`` 리스트가 **둘 다** 그
        # IRI 에 붙으면, 둘째 리스트는 ``_replace`` 후 ``rdf:first`` 를 잃고
        # rest-only 체인으로 남아 HermiT 가 "멤버 0개 DisjointClasses" 로 읽는다.
        #
        # 실측 (2026-08-18): 배포 T-Box 의 3개 노드가 이 상태여서 HermiT 가 **로드
        # 자체를 거부** 했다 ("A DisjointClasses axiom in OWL 2 DL must have at
        # least two classes as parameters"). S3 재실행은 자기수리하지 못한다.
        #
        # 이미 같은 IRI 가 리스트를 갖고 있으면 이 BNode 의 리스트를 **폐기** 한다
        # (내용이 동일하므로 손실 없음). 병합 자체는 막지 않는다.
        if next(g.objects(new_uri, OWL.members), None) is not None:
            _remove_rdf_collection(g, members_node)
            g.remove((bnode, OWL.members, members_node))
            for t in list(g.triples((bnode, None, None))):
                g.remove(t)
            for t in list(g.triples((None, None, bnode))):
                g.remove(t)
            counts["AllDisjointClasses_merged_duplicates"] = (
                counts.get("AllDisjointClasses_merged_duplicates", 0) + 1
            )
            continue

        _replace(bnode, new_uri)
        counts["AllDisjointClasses"] += 1

    # ── 3. equivalentClass/unionOf BNode ────────────
    for cls_node in list(g.subjects(OWL.equivalentClass, None)):
        if not isinstance(cls_node, URIRef):
            continue
        for bnode in list(g.objects(cls_node, OWL.equivalentClass)):
            if not isinstance(bnode, BNode):
                continue
            union_list_node = g.value(bnode, OWL.unionOf)
            if union_list_node is None:
                continue

            try:
                members = list(rdflib.collection.Collection(g, union_list_node))
            except Exception as e:
                logger.debug("unionOf collection 파싱 실패: %s", e)
                continue

            sorted_names = sorted(_local_name(str(m)) for m in members if isinstance(m, URIRef))
            hash8 = hashlib.md5(
                "_".join(sorted_names).encode(),
                usedforsecurity=False,
            ).hexdigest()[:8]
            cls_local = _local_name(str(cls_node))
            new_uri = DOMAIN_NS_OBJ[f"Union_{cls_local}_{hash8}"]

            _replace(bnode, new_uri)
            counts["Union"] += 1

    total = sum(counts.values())
    return {"total_skolemized": total, "by_type": counts}


def _dedup_named_restrictions(g: Graph) -> dict:
    """named(URIRef) owl:Restriction 중 의미론적으로 동일한 것을 병합.

    병합 키: (onProperty, restriction_type, target_value_str).
    동일 키를 가지는 restriction URI 들이 있으면, 알파벳 순으로 가장 앞선
    URI 를 "canonical" 로 삼고 나머지 URI 들이 참조되는 모든 위치를 canonical
    로 대체한다. subClassOf 중복은 rdflib set 의미로 자동 dedup.

    ★ 주의: URI 가 합쳐지면 그 URI 의 definition(rdf:type owl:Restriction,
      onProperty, someValuesFrom, …)도 canonical 쪽에만 남는다.
    """
    from collections import defaultdict

    key_to_uris: dict[tuple, list[URIRef]] = defaultdict(list)
    for s in g.subjects(RDF.type, OWL.Restriction):
        if not isinstance(s, URIRef):
            continue
        prop = g.value(s, OWL.onProperty)
        if prop is None:
            continue
        target_value = ""
        restriction_type = ""
        for rtype in ("someValuesFrom", "hasValue", "allValuesFrom",
                       "minCardinality", "maxCardinality",
                       "minQualifiedCardinality", "maxQualifiedCardinality",
                       "qualifiedCardinality"):
            tgt = g.value(s, OWL[rtype])
            if tgt is not None:
                restriction_type = rtype
                target_value = str(tgt)
                break
        if not restriction_type:
            continue
        key = (str(prop), restriction_type, target_value)
        key_to_uris[key].append(s)

    merged = 0
    removed = 0
    for _key, uris in key_to_uris.items():
        if len(uris) <= 1:
            continue
        canonical = sorted(uris, key=lambda u: str(u))[0]
        for dup in uris:
            if dup == canonical:
                continue
            # subject 위치의 dup 정의는 제거 (canonical 에 이미 동일 정의 존재)
            for p, o in list(g.predicate_objects(dup)):
                g.remove((dup, p, o))
                removed += 1
            # object 위치에서 dup 를 참조하는 모든 triple 을 canonical 로 교체
            for sb, pr in list(g.subject_predicates(dup)):
                g.remove((sb, pr, dup))
                g.add((sb, pr, canonical))
            merged += 1

    return {"merged": merged, "removed": removed}


# ── Duplicate OP Consolidation (R11-M5, 옵션 β) ──────────

#: 방향성 토큰의 **대립 짝**. 중복 OP 를 별개 의미로 갈라야 하는 근거는 "한쪽에
#: origin, 다른 쪽에 destination" 처럼 **서로 반대되는 토큰이 동시에 존재**할
#: 때뿐이다. 한 OP 에만 토큰이 있고 나머지엔 없는 것은 의미 차이의 증거가 아니다.
#:
#: 실측 (2026-07-26): ``steelmakingResultRefersToBofHeat`` 는 라벨 "steelmaking
#: result refers to BOF heat" 의 전치사 ``to`` 때문에 방향 토큰 보유로 판정돼
#: 같은 ``(MaterialA, ProcessStepA)`` 중복 그룹에서 홀로 분리됐다. 대립 짝이 없는데도
#: 분리된 것이라, 통합 대상에서 빠져 값 0건 빈 관계로 남았다.
#: 대립 축을 **(A쪽 동의어, B쪽 동의어)** 로 표현한다. 같은 축의 두 쪽은 반대
#: 의미이고, 한 쪽 안의 토큰들은 언어만 다른 **동의어** 다.
#:
#: 언어를 섞어야 하는 이유 — 한 OP 의 라벨은 영문만, 다른 OP 는 한글만 가질 수 있다
#: (실측: step_15c 가 주입한 OP 는 이름 기반 영문 라벨 "has destination warehouse",
#: 기존 OP 는 SME 가 쓴 "운송 출발 창고"). 2026-08-15 실측: 언어별로 짝을 가두면
#: ``destination`` 과 ``출발`` 이 동시에 있어도 대립이 탐지되지 않아 출발/목적 창고가
#: owl:equivalentProperty 로 동일시됐다.
#:
#: 반대로 두 쪽을 하나의 집합으로 **합치면** ``origin`` vs ``출발`` (같은 뜻, 다른
#: 언어) 이 대립으로 오탐된다 — 그러면 정당한 동의어 통합이 막힌다. 그래서 축 단위로
#: 쪽을 구분해 둔다.
_DIRECTIONAL_AXES: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (frozenset({"origin", "출발"}), frozenset({"destination", "목적"})),
    (frozenset({"source", "근원"}), frozenset({"target", "대상"})),
    (frozenset({"from"}), frozenset({"to"})),
    (frozenset({"input", "입력"}), frozenset({"output", "출력"})),
    (frozenset({"sender", "공급"}), frozenset({"receiver", "수신"})),
    (frozenset({"supplier"}), frozenset({"buyer"})),
    # 시간 순서 축. ``_would_collapse_direction`` 이 방향 소멸(대칭)은 막지만,
    # **어느 쪽이 부모로 남는지** 는 결정하지 못한다 — 실측 (2026-08-17): 공정 흐름
    # 4개 OP 를 24개 입력 순서로 돌리면 ``followedBy ⊑ directlyFollows`` 와
    # ``followedBy ⊑ directlyPrecedes`` 가 정확히 12:12 로 갈렸다. 전자는 의미가
    # 반대다 (T-Box 주석: directlyFollows = "바로 뒤에", followedBy 는 주어가 앞).
    # 순서 술어를 축으로 선언하면 정방향/역방향 그룹이 갈려 잘못된 부모가 애초에
    # 생기지 않는다. 축은 앞/뒤 두 쪽이 **모두** 관측될 때만 발화한다.
    (frozenset({"precede", "precedes", "preceded", "preceding", "선행", "앞"}),
     frozenset({"follow", "follows", "followed", "following", "후행", "뒤"})),
)

#: 하위 호환 — 축의 양쪽을 합친 집합. 토큰 사전(``_DIRECTIONAL_TOKENS``) 파생 등
#: "이 토큰이 방향성인가" 만 묻는 용도. 대립 판정에는 ``_DIRECTIONAL_AXES`` 를 쓴다.
_DIRECTIONAL_OPPOSITES: tuple[frozenset[str], ...] = tuple(
    a | b for a, b in _DIRECTIONAL_AXES
)

#: 방향성을 나타내는 라벨 토큰. 서로 다른 토큰을 가진 OP 는 의미상 구분된 것으로
#: 간주해 consolidation 에서 제외한다.
#:
#: **축에서 파생한다** — 손으로 나열한 사본이면 축에 쪽을 추가할 때 사전 갱신을
#: 잊고, 그러면 축은 늘었는데 토큰이 추출되지 않아 판정이 조용히 no-op 이 된다
#: (실측 2026-08-17: 순서 축을 추가했는데 4개 OP 전부 tokens=∅ 로 나와 분리가
#: 발화하지 않았다). 추출과 판정의 어휘는 한 곳에서만 정의한다.
_DIRECTIONAL_TOKENS: frozenset[str] = frozenset().union(*_DIRECTIONAL_OPPOSITES)


def _directional_tokens_in_labels(g: Graph, op_uri: URIRef) -> frozenset[str]:
    """OP 의 모든 rdfs:label 에서 방향성 토큰 추출 (소문자).

    영문 토큰은 **단어 경계** 로만 매칭한다. 부분 문자열로 보면 짧은 토큰이
    무관한 단어에 걸린다 — ``to`` 가 ``inventory`` / ``transportation`` 안에 있어
    (실측) 창고·재고 관계가 "방향 대립" 으로 잘못 면제될 수 있다. 면제는 중복 OP
    게이트를 통과시키는 것이라, 오탐은 진짜 중복을 숨긴다.

    한글 토큰은 조사·복합어로 붙어 쓰이므로(``출발`` → ``출발지``, ``목적`` →
    ``목적지``) 부분 문자열 매칭을 유지한다.
    """
    tokens: set[str] = set()
    ascii_tokens = {t for t in _DIRECTIONAL_TOKENS if t.isascii()}
    for lab in g.objects(op_uri, RDFS.label):
        s = str(lab).lower()
        words = set(re.findall(r"[a-z]+", s))
        for tok in _DIRECTIONAL_TOKENS:
            hit = tok in words if tok in ascii_tokens else tok in s
            if hit:
                tokens.add(tok)
    return frozenset(tokens)


def _has_opposing_directions(token_sets: "list[frozenset[str]]") -> bool:
    """토큰 집합들 사이에 **대립하는** 방향 토큰이 실제로 존재하는가.

    True 인 경우에만 중복 OP 그룹을 방향별로 갈라야 한다. 한쪽에만 토큰이 있는
    (대립 짝 없는) 상황은 전치사 등 우연한 일치가 대부분이므로 갈라선 안 된다 —
    갈라지면 통합에서 빠져 값 0건 빈 관계가 남는다.
    """
    for side_a, side_b in _DIRECTIONAL_AXES:
        # **축의 양쪽이 각각 관측돼야** 대립이다. 같은 쪽의 토큰만 여러 개 보이는
        # 것은 언어 차이(origin / 출발)일 뿐 의미 차이가 아니다 — 그것을 대립으로
        # 읽으면 정당한 동의어 통합이 막힌다.
        has_a = any(t & side_a for t in token_sets)
        has_b = any(t & side_b for t in token_sets)
        if has_a and has_b:
            return True
    return False


def _pick_canonical_op(
    g: Graph,
    candidates: list[URIRef],
    abox_usage: dict[str, int] | None = None,
) -> URIRef:
    """중복 OP 그룹에서 canonical 하나 선택.

    우선순위:
      1. A-Box 사용 횟수 최대 (있을 때)
      2. 이름 길이 긴 것 (구체적 이름 선호; e.g. hasMechanicalProperties > hasQualityResult)
      3. 알파벳 순
    """
    def _score(op):
        name = str(op).split("#")[-1]
        usage = (abox_usage or {}).get(name, 0)
        return (usage, len(name), name)
    return max(candidates, key=_score)


#: A-Box OP usage 캐시 — ``{path: (mtime, size, counts)}``. 한 T-Box 개선 실행에서
#: 여러 step 이 같은 파일을 요구하고, 테스트는 같은 프로세스에서 수십 번 호출한다.
#: mtime+size 로 무효화하므로 A-Box 재생성은 자동 반영된다.
_ABOX_OP_USAGE_CACHE: dict[str, tuple[float, int, dict[str, int]]] = {}


def _load_abox_op_usage(abox_path: str | None = None) -> dict[str, int]:
    """A-Box TTL 에서 도메인 프로퍼티 사용 횟수 집계. 파일 없으면 빈 dict.

    **텍스트 스캔으로 세는 이유**: rdflib 파싱은 그래프 전체를 메모리에 올려
    500MB+ A-Box 에서 수 분 + 수 GB RSS 가 든다 (2026-07-25 실측: 533MB A-Box 를
    파싱하다 T-Box 후처리 step 22 가 사실상 정지 — 12 트리플짜리 단위 테스트도
    타임아웃). 여기서 필요한 것은 **이름별 등장 횟수** 뿐이고 그래프 구조가
    아니므로, 한 번의 순차 읽기로 정규식 카운트한다 (step_12f 와 같은 방식).
    결과는 mtime+size 키로 캐시해 같은 실행 안의 반복 호출을 막는다.

    부정확성 감수: 리터럴 값 안에 ``med:foo`` 같은 문자열이 있으면 과다 계수될
    수 있다. 이 값은 canonical OP 선택의 **순위 신호** 로만 쓰이므로 (동점 시
    이름 길이·사전순으로 결정) 영향이 없다.

    패턴은 배포 설정에서 prefix 를 읽어 만든다 (``domain_predicate_pattern``).
    이전엔 한 도메인의 prefix 를 정규식에 리터럴로 박아, 다른 배포에서는 접두형
    Turtle A-Box (= 이 리포 생성기의 출력) 를 한 건도 세지 못했다. 호출부가 그
    빈 결과를 "미사용" 으로 읽어 step_22 의 canonical 방향을 뒤집고 step_22e 는
    값이 있는 OP 를 지웠다 (2026-08-08 규명). 네임스페이스 설정이 비어 판정이
    불가능하면 빈 dict 를 반환하되 헬퍼가 경고를 남긴다 — 호출부는 usage 를
    순위 신호로만 쓰므로 안전하다.
    """
    import os
    if not abox_path:
        from config import ABOX_PATH
        abox_path = ABOX_PATH
    if not abox_path or not os.path.exists(abox_path):
        return {}
    try:
        stat = os.stat(abox_path)
    except OSError as exc:
        logger.debug("A-Box stat 실패 (OP usage 건너뜀): %s", exc)
        return {}
    cached = _ABOX_OP_USAGE_CACHE.get(abox_path)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    from domain.graph_utils import domain_predicate_pattern
    pattern = domain_predicate_pattern()
    if pattern is None:
        return {}

    # ``master_data.ttl`` 과 tacit TTL 도 센다. A-Box 본문만 보면 마스터 인스턴스와
    # SME 지식이 쓰는 프로퍼티가 "미사용" 으로 읽힌다 — 실측 (2026-08-11):
    # ``hasSoilImpact`` 는 object term 4,128건이 tacit 에 있는데 이 함수는 0 을
    # 반환했고, step_22e 는 그 0 을 근거로 값 0건인 동의어를 정본으로 골랐다.
    # tacit 은 A-Box 재생성과 무관하게 보존되는 지식이므로 반드시 포함해야 한다.
    scan_paths = [abox_path, os.path.join(os.path.dirname(abox_path), "master_data.ttl")]
    try:
        import glob

        from config import SOURCE_DIR
        scan_paths.extend(
            sorted(glob.glob(os.path.join(SOURCE_DIR, "tacit", "*.ttl"))),
        )
    except Exception as exc:  # noqa: BLE001 — tacit 없으면 A-Box 만
        logger.debug("tacit 경로 조회 실패 (A-Box 만 계수): %s", exc)

    counts: dict[str, int] = {}
    for path in scan_paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    for name in pattern.findall(line):
                        counts[name] = counts.get(name, 0) + 1
        except OSError as exc:
            logger.debug("스캔 실패 (%s), 계속: %s", path, exc)
    if not counts:
        return {}
    _ABOX_OP_USAGE_CACHE[abox_path] = (stat.st_mtime, stat.st_size, counts)
    return counts


def _would_create_subpropertyof_cycle(
    g: Graph, new_child: URIRef, new_parent: URIRef
) -> bool:
    """new_child rdfs:subPropertyOf new_parent 추가 시 사이클 발생 여부.

    사이클 조건: new_parent 의 descendant 중에 new_child 가 존재.
    즉 new_parent 에서 subPropertyOf 체인을 따라 자식 방향으로 내려가다
    new_child 에 도달 가능하면 역방향 edge 추가 시 사이클.
    """
    visited: set[URIRef] = set()
    stack: list[URIRef] = [new_parent]
    while stack:
        node = stack.pop()
        if node == new_child:
            return True
        if node in visited:
            continue
        visited.add(node)
        for desc in g.subjects(RDFS.subPropertyOf, node):
            if isinstance(desc, URIRef):
                stack.append(desc)
    return False


def _inverses_of(g: Graph, op: URIRef) -> "set[URIRef]":
    """op 의 역관계 집합. ``owl:inverseOf`` 는 대칭이므로 양방향을 본다."""
    out = {o for o in g.objects(op, OWL.inverseOf) if isinstance(o, URIRef)}
    out |= {s for s in g.subjects(OWL.inverseOf, op) if isinstance(s, URIRef)}
    return out


def _subproperty_ancestors(g: Graph, op: URIRef) -> "set[URIRef]":
    """op 의 subPropertyOf 조상 전체 (자신 제외, 순환 안전)."""
    seen: set[URIRef] = set()
    stack = [o for o in g.objects(op, RDFS.subPropertyOf) if isinstance(o, URIRef)]
    while stack:
        node = stack.pop()
        if node in seen or node == op:
            continue
        seen.add(node)
        stack.extend(
            o for o in g.objects(node, RDFS.subPropertyOf) if isinstance(o, URIRef)
        )
    return seen


def _would_collapse_direction(g: Graph, child: URIRef, new_parent: URIRef) -> bool:
    """``child rdfs:subPropertyOf new_parent`` 추가가 방향을 소멸시키는가.

    두 경로로 소멸한다. 둘 다 ``owl:inverseOf`` 가 **형식적** 방향 증거라는
    같은 사실에서 나온다 — 라벨 토큰(origin/destination)은 선택적 힌트지만
    inverseOf 는 공리다.

    1. **자기 역관계의 하위**: ``P ⊑ inv(P)`` ⟹ P 는 대칭. ``A P B`` 가
       ``B P A`` 를 함의한다.
    2. **역관계 쌍의 공통 하위**: ``P ⊑ Q``, ``P ⊑ R``, ``Q = inv(R)`` ⟹
       ``A P B`` 가 ``A Q B`` 와 ``A R B`` 를 함의하고, R = inv(Q) 이므로
       ``B Q A`` 까지 함의한다. 세 관계 전부 대칭이 된다.

    실측 손상 (2026-08-17): 공정 흐름 4개 OP 는 domain = range =
    ``ManufacturingProcessStep`` 이라 같은 버킷에 들어가고 방향 토큰이 없어
    ``_has_opposing_directions`` 가 갈라내지 못했다. 경로 2 를 통해
    ``followedBy ⊑ {directlyFollows, directlyPrecedes, precededBy}`` 가 생기고
    (``directlyFollows = inv(directlyPrecedes)``), tacit 의 정방향 3건
    (고로→제강→연주→압연) 이 추론 후 양방향으로 붕괴했다. all_inferred.ttl 에
    ``ProcessSteelmakingFurnace directlyPrecedes ProcessBlastFurnace`` 가 실재했고
    HermiT / SHACL / check_quality_rules 어느 것도 잡지 못했다.

    ``step_15c`` 는 equivalentProperty 경로에 같은 판정을 이미 갖고 있었다
    (경로 1 만). subPropertyOf 경로에는 없었다 — 사본이 아니라 이 헬퍼를 호출하라.

    조상까지 보는 이유: 이 함수는 루프 중간에 호출되고 앞선 반복이 이미 부모를
    추가했다. 직접 부모만 보면 같은 그룹의 세 번째 OP 에서 경로 2 를 놓친다.
    """
    parent_inverses = _inverses_of(g, new_parent)
    if not parent_inverses:
        return False
    if child in parent_inverses:                      # 경로 1
        return True
    existing = {
        o for o in g.objects(child, RDFS.subPropertyOf) if isinstance(o, URIRef)
    } | _subproperty_ancestors(g, child)
    return bool(existing & parent_inverses)           # 경로 2


def _consolidate_duplicate_ops(g: Graph, steel_str: str) -> dict:
    """같은 (domain, range) 를 가진 OP 그룹을 consolidate.

    - canonical 하나를 선택하고 나머지를 owl:subPropertyOf canonical 로 연결
    - 기존 subPropertyOf / inverseOf 트리플은 보존
    - 방향성 토큰이 서로 다른 OP 쌍은 제외 (Origin vs Destination 등)
    - **역관계 가드**: 방향을 소멸시키는 연결은 skip (`_would_collapse_direction`).
      라벨 토큰이 없어도 owl:inverseOf 로 판정한다 — self-referential OP
      (domain == range) 는 토큰 가드가 원리상 무력하다.
    - Cycle guard: 역방향 subPropertyOf 가 이미 있으면 skip (무한 반복 실행 시
      A-Box usage 변화로 canonical 이 뒤집혀도 사이클 방지).
    """
    from collections import defaultdict
    abox_usage = _load_abox_op_usage()

    # (domain, range) 버킷 수집
    buckets: dict[tuple[frozenset[str], frozenset[str]], list[URIRef]] = defaultdict(list)
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
            continue
        domains = frozenset(
            str(d).split("#")[-1]
            for d in g.objects(op, RDFS.domain)
            if isinstance(d, URIRef) and str(d).startswith(steel_str)
        )
        ranges = frozenset(
            str(r).split("#")[-1]
            for r in g.objects(op, RDFS.range)
            if isinstance(r, URIRef) and str(r).startswith(steel_str)
        )
        if not domains or not ranges:
            continue
        buckets[(domains, ranges)].append(op)

    consolidated = 0
    skipped_directional = 0
    skipped_cycle = 0
    skipped_inverse = 0
    groups_processed = 0
    for (_d, _r), ops in buckets.items():
        if len(ops) < 2:
            continue
        groups_processed += 1

        # 방향성 토큰으로 의미 구분된 OP 는 별도 그룹으로 분리.
        # 단, **대립 짝이 실제로 존재할 때만** 분리한다 (origin↔destination 등).
        # 한쪽에만 토큰이 있는 경우는 전치사 우연 일치가 대부분이라 분리하면
        # 통합에서 빠져 값 0건 빈 관계가 남는다 (_has_opposing_directions 주석).
        token_sets = [_directional_tokens_in_labels(g, op) for op in ops]
        by_direction: dict[frozenset[str], list[URIRef]] = defaultdict(list)
        if _has_opposing_directions(token_sets):
            for op, toks in zip(ops, token_sets):
                by_direction[toks].append(op)
        else:
            by_direction[frozenset()] = list(ops)

        for tokens, sub_ops in by_direction.items():
            if len(sub_ops) < 2:
                # 방향성 토큰으로 유일해지면 consolidation 불필요
                if tokens and len(by_direction) > 1:
                    skipped_directional += len([o for o in ops if o not in sub_ops])
                continue
            # canonical = A-Box 에서 실제 사용되는 OP (자식 역할).
            # 나머지 OP 들이 parent 가 되도록 canonical rdfs:subPropertyOf parent.
            # OWL RL 추론이 child→parent 전파하므로 A-Box 의 canonical 트리플이
            # 모든 parent OP 이름으로도 추론됨. CQ 가 parent 이름을 참조해도 JOIN 가능.
            canonical = _pick_canonical_op(g, sub_ops, abox_usage)
            for op in sub_ops:
                if op == canonical:
                    continue
                # 이미 canonical 이 op 의 subPropertyOf 이면 skip
                if op in set(g.objects(canonical, RDFS.subPropertyOf)):
                    continue
                # Direction guard: 방향을 소멸시키는 연결은 통합하지 않는다.
                # 라벨 토큰 가드로는 못 잡는다 — self-referential OP 는 방향
                # 토큰이 없어도 inverseOf 로 대립이 선언돼 있다. 그룹 전체를
                # 포기하지 않고 이 쌍만 건너뛴다 (무관한 나머지는 통합 유지).
                if _would_collapse_direction(g, canonical, op):
                    skipped_inverse += 1
                    continue
                # Cycle guard: 역방향이 이미 있거나 간접 경로로 연결되어
                # 있으면 추가 시 사이클이 된다. A-Box usage 변화로 canonical
                # 선택이 뒤집힐 때 발생하는 근본 문제를 막는다.
                if _would_create_subpropertyof_cycle(g, canonical, op):
                    skipped_cycle += 1
                    continue
                g.add((canonical, RDFS.subPropertyOf, op))
                consolidated += 1

    return {
        "duplicate_ops_consolidated": consolidated,
        "duplicate_op_groups_processed": groups_processed,
        "duplicate_ops_skipped_directional": skipped_directional,
        "duplicate_ops_skipped_cycle": skipped_cycle,
        "duplicate_ops_skipped_inverse": skipped_inverse,
    }


def _direction_tokens_in_text(text: str) -> frozenset[str]:
    """임의 문자열에서 방향 토큰 추출. camelCase / snake_case 를 단어로 쪼갠다.

    영문은 **단어 경계** 로만 매칭한다 — 부분 문자열로 보면 짧은 토큰이 무관한
    단어에 걸린다 (실측: ``to`` 가 ``transportation`` 안에 있어 운송 컬럼이 전부
    방향성으로 오판됐다). 한글은 조사·복합어로 붙어 쓰이므로(``출발`` →
    ``출발지``) 부분 문자열 매칭을 유지한다.

    ``_directional_tokens_in_labels`` 는 rdfs:label 을, 이 함수는 IRI local name
    이나 CSV 컬럼명처럼 **라벨이 아닌 텍스트** 를 본다. 어휘
    (``_DIRECTIONAL_TOKENS``) 는 둘이 공유한다 — 사본을 만들면 축에 쪽을 추가할 때
    한쪽만 갱신돼 판정이 갈라진다.
    """
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", text)
    words = set(re.findall(r"[a-z]+", spaced.lower()))
    lowered = text.lower()
    return frozenset(
        t for t in _DIRECTIONAL_TOKENS
        if (t in words if t.isascii() else t in lowered)
    )


def _direction_side(g: Graph, op: URIRef) -> "tuple[int, int] | None":
    """op 가 어느 방향 축의 어느 쪽인지. ``(축 index, 0|1)`` 또는 None.

    ``_DIRECTIONAL_AXES`` 를 재사용한다 — 방향 어휘의 사본을 만들면 축에 쪽을
    추가할 때 한쪽만 갱신돼 판정이 갈라진다.

    라벨을 먼저 보고, 없으면 **IRI local name** 을 본다. 라벨이 camelCase 그대로인
    OP (기계 생성분) 는 단어 경계 매칭에 걸리지 않아 라벨만으로는 판정 불가다.
    그때 이름을 보지 않으면 폴백이 방향이 반대인 부모를 남긴다 — 대칭이 아니어서
    다른 검사는 전부 통과하는, 조용히 뒤집힌 계층이 된다.
    """
    toks = _directional_tokens_in_labels(g, op)
    if not toks:
        toks = _direction_tokens_in_text(str(op).rsplit("#", 1)[-1].rsplit("/", 1)[-1])
    if not toks:
        return None
    for idx, (side_a, side_b) in enumerate(_DIRECTIONAL_AXES):
        if toks & side_a:
            return (idx, 0)
        if toks & side_b:
            return (idx, 1)
    return None


def _repair_direction_collapse(g: Graph, steel_str: str) -> dict:
    """이미 박힌 **방향 소멸** subPropertyOf 를 제거한다 (step_22g).

    `_consolidate_duplicate_ops` 의 가드(`_would_collapse_direction`)는 **신규
    추가만** 막는다. 이미 T-Box 에 기록된 링크는 S3 재실행으로 사라지지 않는다
    (실측 2026-08-17: 재실행 전후 ``followedBy`` 의 부모 3개가 동일했다).
    step_22b(cycle breaker)와 같은 이유로 사후 정리가 따로 필요하다.

    제거 대상은 두 형태다. 둘 다 ``owl:inverseOf`` 공리로 판정한다:

    1. **자기 역관계 부모** — ``P ⊑ inv(P)`` ⟹ P 가 대칭이 된다.
    2. **역관계 쌍 부모** — ``P ⊑ Q``, ``P ⊑ R``, ``Q = inv(R)`` ⟹ P·Q·R 전부
       대칭. 이때는 **한쪽만 남긴다** (둘 다 지우면 정당한 계층까지 잃는다).

    남길 쪽은 라벨의 방향 축으로 고른다 — ``followedBy``(주어가 앞) 의 올바른
    부모는 ``directlyPrecedes``("바로 앞에") 이고 ``directlyFollows``("바로 뒤에")
    는 반대다. 아무거나 남기면 절반의 확률로 의미가 뒤집힌 계층이 남고, 그것은
    대칭이 아니어서 다른 검사를 전부 통과한다. 라벨로 판정할 수 없으면 이름
    정렬로 결정론만 확보한다 (임의 선택이라도 재실행 간 흔들리지 않게).

    ``owl:inverseOf`` 자체는 절대 지우지 않는다 — 그것이 방향의 근거다.

    실측 손상: tacit 정방향 3건(고로→제강→연주→압연)이 추론 후 양방향으로
    붕괴해 ``all_inferred.ttl`` 에 ``ProcessSteelmakingFurnace directlyPrecedes
    ProcessBlastFurnace`` 가 실재했다. HermiT / SHACL / check_quality_rules
    어느 것도 잡지 못했다.

    Returns:
        {"direction_collapse_repaired": N, "direction_collapse_self_inverse": N,
         "direction_collapse_inverse_pairs": N}
    """
    removed = 0
    self_inverse = 0
    inverse_pairs = 0

    children = sorted(
        {
            s for s in g.subjects(RDFS.subPropertyOf, None)
            if isinstance(s, URIRef) and str(s).startswith(steel_str)
        },
        key=str,
    )
    for child in children:
        # ① 자기 역관계 부모 제거.
        child_inverses = _inverses_of(g, child)
        for parent in sorted(
            (o for o in g.objects(child, RDFS.subPropertyOf) if isinstance(o, URIRef)),
            key=str,
        ):
            if parent in child_inverses:
                g.remove((child, RDFS.subPropertyOf, parent))
                removed += 1
                self_inverse += 1

        # ② 역관계 쌍 부모 중 방향이 맞는 하나만 남긴다.
        #    루프 안에서 그래프를 바꾸므로 매 반복 부모를 다시 읽는다.
        while True:
            parents = sorted(
                (o for o in g.objects(child, RDFS.subPropertyOf)
                 if isinstance(o, URIRef)),
                key=str,
            )
            pair = next(
                (
                    (a, b)
                    for i, a in enumerate(parents)
                    for b in parents[i + 1:]
                    if b in _inverses_of(g, a)
                ),
                None,
            )
            if pair is None:
                break
            keep, drop = _pick_direction_aligned_parent(g, child, pair)
            g.remove((child, RDFS.subPropertyOf, drop))
            removed += 1
            inverse_pairs += 1
            del keep

    return {
        "direction_collapse_repaired": removed,
        "direction_collapse_self_inverse": self_inverse,
        "direction_collapse_inverse_pairs": inverse_pairs,
    }


def _pick_direction_aligned_parent(
    g: Graph, child: URIRef, pair: "tuple[URIRef, URIRef]"
) -> "tuple[URIRef, URIRef]":
    """역관계 쌍 ``pair`` 중 child 와 방향이 맞는 부모를 고른다 → ``(keep, drop)``.

    child 와 **같은 쪽** 토큰을 가진 부모가 맞는 부모다. ``followedBy``(follow 쪽)
    의 부모로는 같은 축의 precede 쪽인 ``directlyPrecedes`` 가 맞다 — 순서 축에서
    "A 뒤에 B" 와 "A 가 B 앞에" 는 같은 사실이므로, 축 위에서 child 와 부모는
    **반대 쪽** 라벨을 갖는다. 판정 불가면 이름 정렬로 결정론만 확보한다.
    """
    a, b = pair
    child_side = _direction_side(g, child)
    if child_side is not None:
        axis, side = child_side
        for cand, other in ((a, b), (b, a)):
            cand_side = _direction_side(g, cand)
            if cand_side is not None and cand_side[0] == axis and cand_side[1] != side:
                return cand, other
    keep, drop = sorted((a, b), key=str)
    return keep, drop


def _break_sub_property_cycles(g: Graph, steel_str: str) -> dict:
    """subPropertyOf 순환을 제거한다 (R25).

    Multi-Agent 가 의미 동일한 OP/DP 를 duplicate 생성 후 양방향
    subPropertyOf 로 연결하면 critical sub_property_cycle 이슈가 발생.
    `_consolidate_duplicate_ops` 는 라벨 기반으로 묶지만, 라벨이 달라서
    놓친 케이스가 남음. 이 step 은 순수하게 그래프 순환을 탐지하고 제거.

    전략:
      1. steel-ns 내 subPropertyOf edge 를 directed graph 로 본다.
      2. Tarjan SCC 로 강연결 성분 중 size ≥ 2 를 찾는다.
      3. 각 SCC 에서 가장 "잎에 가까운" (outgoing subPropertyOf 가
         가장 많은) OP 의 SCC 내부로 나가는 edge 만 제거.

    Returns:
        {"sub_property_cycles_broken": N}
    """
    all_props = set(g.subjects(RDF.type, OWL.ObjectProperty)) | set(
        g.subjects(RDF.type, OWL.DatatypeProperty)
    )
    nodes = {p for p in all_props
             if isinstance(p, URIRef) and str(p).startswith(steel_str)}
    # adjacency: child → parents
    adj: dict[URIRef, set[URIRef]] = {n: set() for n in nodes}
    for n in nodes:
        for parent in g.objects(n, RDFS.subPropertyOf):
            if isinstance(parent, URIRef) and parent in nodes:
                adj[n].add(parent)

    # Tarjan SCC
    index_counter = [0]
    stack: list[URIRef] = []
    on_stack: set[URIRef] = set()
    index: dict[URIRef, int] = {}
    lowlink: dict[URIRef, int] = {}
    sccs: list[list[URIRef]] = []

    def _strongconnect(v: URIRef) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, ()):
            if w not in index:
                _strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp: list[URIRef] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    for n in nodes:
        if n not in index:
            _strongconnect(n)

    broken = 0
    for scc in sccs:
        if len(scc) < 2:
            continue
        # SCC 내 OP 중 "가장 많이 outgoing subPropertyOf 를 가진 쪽" 의
        # SCC 내부 edge 를 제거 (hierarchy 의 leaf 에 가까운 쪽부터 정리).
        scc_set = set(scc)
        # score: 해당 node 에서 scc 내부로 가는 edge 수
        scored = [(len(adj[p] & scc_set), p) for p in scc]
        scored.sort(reverse=True)  # high outgoing first
        for _score, p in scored:
            removed_this = 0
            for parent in list(adj[p] & scc_set):
                g.remove((p, RDFS.subPropertyOf, parent))
                adj[p].discard(parent)
                broken += 1
                removed_this += 1
            # 이 노드 제거 후 SCC 내 cycle 이 끊어지면 break
            # 다시 SCC 를 평가하는 대신 leaf 1개만 정리해도 cycle 해제됨 (대부분)
            if removed_this:
                break

    return {"sub_property_cycles_broken": broken}


def _break_sub_class_cycles(g: Graph, steel_str: str) -> dict:
    """rdfs:subClassOf 순환을 제거한다 (2026-06-23).

    Multi-Agent 가 의미가 거의 같은 두 클래스(예: MaterialADesignSpec ↔
    MaterialADesignSpecification)를 서로의 부모로 선언하면 subClassOf 순환이 생긴다.
    OWL 의미론상 두 클래스가 equivalent 가 되어 추론을 오염시키고, 클래스 계층을
    순회하는 코드(CQ runtime check 의 _descendants 등)를 무한 재귀시킨다.

    `_break_sub_property_cycles` 와 동일한 Tarjan SCC 전략:
      1. steel-ns subClassOf edge 를 directed graph 로 본다.
      2. SCC size ≥ 2 (순환) 를 찾는다.
      3. 각 SCC 에서 outgoing subClassOf 가 가장 많은 (가장 하위) 클래스의
         SCC 내부 edge 를 제거해 순환만 끊고 나머지 계층은 보존.

    Returns:
        {"sub_class_cycles_broken": N}
    """
    nodes = {
        c for c in g.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(steel_str)
    }
    # self-loop (X subClassOf X) 는 항상 무의미하므로 SCC 분석 전에 직접 제거.
    # add_class_hierarchy / someValuesFrom restriction 생성이 네임스페이스를
    # 혼동해 steel:MaterialArtifact ⊑ steel:MaterialArtifact 를 만드는 케이스
    # (2026-06-23). self-loop 는 adj 에서 제외되므로 SCC 로는 안 잡힌다.
    self_loops = 0
    for n in nodes:
        if (n, RDFS.subClassOf, n) in g:
            g.remove((n, RDFS.subClassOf, n))
            self_loops += 1

    adj: dict[URIRef, set[URIRef]] = {n: set() for n in nodes}
    for n in nodes:
        for parent in g.objects(n, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent in nodes and parent != n:
                adj[n].add(parent)

    index_counter = [0]
    stack: list[URIRef] = []
    on_stack: set[URIRef] = set()
    index: dict[URIRef, int] = {}
    lowlink: dict[URIRef, int] = {}
    sccs: list[list[URIRef]] = []

    def _strongconnect(v: URIRef) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in adj.get(v, ()):
            if w not in index:
                _strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp: list[URIRef] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            sccs.append(comp)

    for n in nodes:
        if n not in index:
            _strongconnect(n)

    broken = 0
    for scc in sccs:
        if len(scc) < 2:
            continue
        scc_set = set(scc)
        scored = [(len(adj[c] & scc_set), c) for c in scc]
        scored.sort(reverse=True)  # 가장 하위(outgoing 많은) 클래스부터 정리
        for _score, c in scored:
            removed_this = 0
            for parent in list(adj[c] & scc_set):
                g.remove((c, RDFS.subClassOf, parent))
                adj[c].discard(parent)
                broken += 1
                removed_this += 1
            if removed_this:
                break

    return {
        "sub_class_cycles_broken": broken + self_loops,
        "sub_class_self_loops_removed": self_loops,
    }


# ── Duplicate DP Normalization (R12, 전문가 리뷰 C2) ──────
# 같은 @en/@ko 라벨을 공유하는 DatatypeProperty 들을 subPropertyOf chain
# 으로 묶어 재사용성/일관성 확보. domain 이 다른 tenant property 들이지만
# 의미적으로 동일한 필드(예: equipmentId, sampleId, value, timestamp) 가
# CSV 파싱 경로에 따라 여러 개 생긴 현상 정규화.


def _extract_common_label(g: Graph, dp: URIRef) -> str | None:
    """DP 의 @en 라벨 (없으면 @ko) 을 소문자로 normalize 해 반환."""
    for lab in g.objects(dp, RDFS.label):
        if isinstance(lab, Literal):
            lang = getattr(lab, "language", None) or ""
            if lang.startswith("en"):
                return str(lab).strip().lower()
    for lab in g.objects(dp, RDFS.label):
        if isinstance(lab, Literal):
            return str(lab).strip().lower()
    return None


def _consolidate_duplicate_dps(g: Graph, steel_str: str) -> dict:
    """같은 @en 라벨을 공유하는 DatatypeProperty 그룹을 canonical parent 로 묶는다.

    전략:
      1. steel-ns DP 를 label 로 grouping (@en 우선, fallback @ko).
      2. 2개 이상이면 canonical DP 선택 (이름 짧은 쪽 = 더 일반적, 또는 FK-like '*Id').
      3. canonical 이 T-Box 에 없으면 라벨 기반으로 신규 abstract DP 생성
         (예: label='equipment id' → :equipmentId canonical).
      4. 나머지 DP 는 canonical 의 owl:subPropertyOf 로 연결.
      5. subPropertyOf cycle guard 는 기존 _would_create_subpropertyof_cycle 재사용.

    **Path B 가드 (domain 일치 필수)**:
      라벨이 같아도 rdfs:domain 이 다른 DP 는 묶지 않는다. domain 이 다른
      class-specific DP 를 subPropertyOf 로 연결하면 OWL RL prp-spo1(자식 값을
      부모로 복사) + prp-dom(부모 domain 타입 부여) 가 결합해 인스턴스 1개가
      형제 클래스 타입 전부를 획득하는 타입 폭발이 발생한다 (2026-06-26 규명).
      Path B(class-specific DP) 정책상 DP 는 정확히 1개 domain 을 가지므로,
      domain 교집합이 비면 같은 라벨이어도 의미가 다른 별개 속성으로 간주한다.
    """
    from collections import defaultdict
    buckets: dict[str, list[URIRef]] = defaultdict(list)
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        lab = _extract_common_label(g, dp)
        if lab:
            buckets[lab].append(dp)

    consolidated = 0
    skipped_cycle = 0
    skipped_domain = 0
    groups_processed = 0
    for _lab, dps in buckets.items():
        if len(dps) < 2:
            continue
        groups_processed += 1
        # Pick canonical: prefer the shortest local name (most generic),
        # then alphabetic for determinism.
        canonical = min(
            dps,
            key=lambda d: (len(str(d).split("#")[-1]), str(d).split("#")[-1]),
        )
        canon_domains = {d for d in g.objects(canonical, RDFS.domain)
                         if isinstance(d, URIRef)}
        for dp in dps:
            if dp == canonical:
                continue
            # canonical 이 이미 dp 의 subPropertyOf 이면 skip
            if canonical in set(g.objects(dp, RDFS.subPropertyOf)):
                continue
            # Path B 가드: domain 이 다르면(교집합 공집합) 묶지 않는다.
            # 둘 중 하나라도 domain 이 없으면(미선언) 보수적으로 허용.
            dp_domains = {d for d in g.objects(dp, RDFS.domain)
                          if isinstance(d, URIRef)}
            if canon_domains and dp_domains and not (canon_domains & dp_domains):
                skipped_domain += 1
                continue
            # cycle guard: dp → canonical 추가 시 사이클 방지
            if _would_create_subpropertyof_cycle(g, dp, canonical):
                skipped_cycle += 1
                continue
            g.add((dp, RDFS.subPropertyOf, canonical))
            consolidated += 1

    return {
        "duplicate_dps_consolidated": consolidated,
        "duplicate_dp_groups_processed": groups_processed,
        "duplicate_dps_skipped_cycle": skipped_cycle,
        "duplicate_dps_skipped_domain_mismatch": skipped_domain,
    }


def _remove_cross_domain_subproperties(g: Graph, steel_str: str) -> dict:
    """rdfs:domain 이 disjoint 한 DP/OP 간의 rdfs:subPropertyOf 를 제거한다 (Path B).

    Step 23(dup_dp) 의 과거 동작이나 LLM add_triple 로 생성된, domain 이 서로
    다른 class-specific property 사이의 subPropertyOf 는 OWL RL prp-spo1 +
    prp-dom 결합으로 타입 폭발(인스턴스 1개가 형제 클래스 타입 전부 획득)을
    일으킨다 (2026-06-26 규명: ProcessStepC 인스턴스가 84개 타입 획득).

    제거 규칙: sub 와 super 양쪽 모두 rdfs:domain 이 선언돼 있고 그 교집합이
    공집합이면 해당 subPropertyOf triple 제거. 한쪽이라도 domain 미선언이면
    보수적으로 보존(상위 추상 property 일 수 있음). 같은 domain(정당한 정규화)
    및 OntoClean abstract canonical 은 영향 없음.
    """
    removed = 0
    preserved_no_domain = 0
    for sub, sup in list(g.subject_objects(RDFS.subPropertyOf)):
        if not (isinstance(sub, URIRef) and str(sub).startswith(steel_str)):
            continue
        if not (isinstance(sup, URIRef) and str(sup).startswith(steel_str)):
            continue
        sub_domains = {d for d in g.objects(sub, RDFS.domain)
                       if isinstance(d, URIRef)}
        sup_domains = {d for d in g.objects(sup, RDFS.domain)
                       if isinstance(d, URIRef)}
        if not sub_domains or not sup_domains:
            preserved_no_domain += 1
            continue
        if not (sub_domains & sup_domains):
            g.remove((sub, RDFS.subPropertyOf, sup))
            removed += 1

    return {
        # 원래 계약 키 — 기존 호출자·테스트가 이 이름을 참조한다. 이름을 바꾸면
        # 조용히 KeyError 가 되므로 유지하고, 신규 지표만 덧붙인다.
        "cross_domain_subproperties_removed": removed,
        "cross_domain_subprop_removed": removed,
        "cross_domain_subprop_preserved_no_domain": preserved_no_domain,
    }


# ── Abstract Category Annotation (R12, 전문가 리뷰 C1/H1) ──────
# Lazy class 는 자신만의 DP/OP 가 없는 classification umbrella. 제거 대신
# "abstract category" 로 명시하고 skos:Concept multi-type 부여하여 의도적
# grouping 이라는 점을 downstream 에 알린다.
# Management 접미사 클래스는 BFO 범주 혼동 (process / role / topic) 회피
# 를 위해 topic/subject concept 으로 표시한다.


_CATEGORY_COMMENT_EN = (
    "Abstract category (classification umbrella) — no discriminating "
    "data/object property of its own; serves as grouping parent."
)
_CATEGORY_COMMENT_KO = "추상 카테고리(분류 묶음) — 자체 판별 속성 없이 그룹 부모 역할만 수행."


def _apply_odp_abstract_groups(g: Graph, steel_str: str) -> dict:
    """T-Box 에 abstract_group_hints.json 기반 ODP 그룹 구조 결정적으로 주입 (T4).

    Architect 프롬프트가 놓친 계층화를 후처리가 보강:
    - hints 의 abstract_class 가 그래프에 없으면 생성 (skos:Concept + label +
      scopeNote + steel-oc:autoLabel true).
    - child_name_patterns / child_examples 로 매칭된 자식에 subClassOf 주입.
    - 이미 명시적 부모가 있는 자식은 skip (기존 구조 존중).
    - 자식이 여러 group 에 매칭되면 첫 group 만 적용 (deterministic order).

    Args:
        g: T-Box 그래프 (in-place 수정).
        steel_str: 도메인 namespace prefix (string).

    Returns:
        {
          "odp_abstract_classes_created": int,
          "odp_subclass_links_added": int,
          "odp_children_matched": int,
          "odp_groups_applied": int,
          "odp_groups_skipped": int,
        }
    """
    from rdflib.namespace import SKOS

    stats = {
        "odp_abstract_classes_created": 0,
        "odp_subclass_links_added": 0,
        "odp_children_matched": 0,
        "odp_groups_applied": 0,
        "odp_groups_skipped": 0,
        "odp_cycle_links_skipped": 0,
    }

    groups = _load_abstract_group_hints()
    if not groups:
        return stats

    # Collect all domain-namespace classes.
    all_domain_classes: dict[str, URIRef] = {
        str(c): c
        for c in g.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(steel_str)
    }

    # Children that already have an explicit domain-namespace parent — skip them
    # to respect existing structure.
    has_explicit_parent: set[str] = {
        str(s)
        for s, _, o in g.triples((None, RDFS.subClassOf, None))
        if isinstance(s, URIRef) and isinstance(o, URIRef)
        and str(s).startswith(steel_str) and str(o).startswith(steel_str)
    }

    # First-group-wins: once a child is assigned, later groups skip it.
    assigned: set[str] = set()
    oc_ns = steel_str.rstrip("#") + "-ontoclean#"
    # NB: T4 uses a dedicated `autoCreated` predicate (class was built by
    # _apply_odp_abstract_groups) — deliberately distinct from T2's
    # `autoLabel` (the OntoClean quadruple was assigned by the heuristic).
    # Separating the two predicates prevents Step 25 (OntoClean) from wiping
    # T4's provenance when a class is later promoted to manual OntoClean
    # labels.
    p_auto_created = URIRef(oc_ns + "autoCreated")

    for group in groups:
        abstract_local = group.get("abstract_class")
        patterns = group.get("child_name_patterns", []) or []
        examples = group.get("child_examples", []) or []
        rationale = group.get("rationale", "")

        if not abstract_local:
            continue

        abstract_uri = URIRef(steel_str + abstract_local)
        abstract_uri_str = str(abstract_uri)

        # Candidate children: examples (if present in graph) + pattern substring
        # matches on local names. `assigned` filtering is applied once, after
        # both sources merge — single source of truth for first-group-wins.
        candidates: set[str] = set()
        for ex in examples:
            ex_uri_str = steel_str + ex
            if ex_uri_str in all_domain_classes:
                candidates.add(ex_uri_str)
        for uri_str in all_domain_classes:
            local = uri_str[len(steel_str):]
            if local == abstract_local:
                continue
            for pat in patterns:
                if pat and pat in local:
                    candidates.add(uri_str)
                    break

        # Single consolidated filter:
        # - remove children with explicit domain parent (respect existing structure)
        # - remove abstract class itself (self-subclass defense, incl. pathological
        #   examples listing the abstract in its own child_examples)
        # - remove children already assigned to an earlier group (first-group-wins)
        candidates = {
            c for c in candidates
            if c not in has_explicit_parent
            and c != abstract_uri_str
            and c[len(steel_str):] not in assigned
        }

        if not candidates:
            stats["odp_groups_skipped"] += 1
            continue

        # Create abstract class only if missing.
        if (abstract_uri, RDF.type, OWL.Class) not in g:
            g.add((abstract_uri, RDF.type, OWL.Class))
            g.add((abstract_uri, RDF.type, SKOS.Concept))
            g.add((abstract_uri, RDFS.label, Literal(abstract_local, lang="en")))
            if rationale:
                g.add((abstract_uri, SKOS.scopeNote, Literal(rationale, lang="ko")))
            # autoCreated flag — ODP provenance, coexists with T2 autoLabel.
            g.add((abstract_uri, p_auto_created, Literal(True)))
            stats["odp_abstract_classes_created"] += 1

        # Inject subClassOf links in deterministic order.
        for child_uri_str in sorted(candidates):
            child_uri = URIRef(child_uri_str)
            if (child_uri, RDFS.subClassOf, abstract_uri) in g:
                continue
            # **후보가 이미 abstract_class 의 (간접) 부모면 넣지 않는다 — 순환이 된다.**
            #
            # ``child_name_patterns`` 는 부분 문자열 매칭이라 **상위 개념 자신을**
            # 자식으로 끌어온다. 실측 (2026-08-23 S3): 패턴 ``"Monitoring"`` 이
            # ``MonitoringManagement`` 를 매칭했는데 그 클래스는 이미
            # ``EnvironmentalMonitoring`` 의 부모였다 → ``MonitoringManagement ⊑
            # EnvironmentalMonitoring ⊑ MonitoringManagement`` 순환.
            #
            # 그 순환은 ``check_quality_rules`` 에 **critical 12건** 으로 떴고
            # (한 SCC 를 조상마다 중복 보고), S4 를 FAIL 로 만들었다. step_22c 가
            # 순환을 끊지만 이 스텝이 **그 뒤에** 실행돼 다시 만든다 — 이 리포의
            # "수정이 다음 결함을 만든다" 패턴이다. 사후 정리(22c)만으로는 부족하고
            # 생성 지점에서 막아야 한다.
            if is_ancestor_of(g, child_uri, abstract_uri):
                stats["odp_cycle_links_skipped"] += 1
                logger.info(
                    "ODP subClassOf 주입 건너뜀: %s ⊑ %s — %s 가 이미 %s 의 "
                    "조상이라 순환이 된다 (child_name_patterns 부분 매칭이 상위 "
                    "개념을 자식으로 끌어온 경우)",
                    _local_name(child_uri_str), abstract_local,
                    _local_name(child_uri_str), abstract_local,
                )
                continue
            g.add((child_uri, RDFS.subClassOf, abstract_uri))
            stats["odp_subclass_links_added"] += 1
            stats["odp_children_matched"] += 1
            assigned.add(child_uri_str[len(steel_str):])

        stats["odp_groups_applied"] += 1

    return stats


# ── 기능 단계: class-specific DP enforcement (Path B sanity validator) ──

# generic DP 이름 패턴. 04-property-rules.md 의 "Path B naming" 절에 있는
# "Never create these generic names" 목록과 동기화한다.
# 이 목록은 "class prefix 없이 단일 개념만 담은 has* / 또는 완전 generic" 이름.
_R1A_GENERIC_DP_NAMES: frozenset[str] = frozenset({
    "hasValue", "hasTemperature", "hasPressure", "hasFlow", "hasQuantity",
    "hasTimestamp", "hasDate", "hasTime",
    "hasStatus", "hasCode", "hasName", "hasIdentifier", "hasId",
    "hasUnit", "hasLocation", "hasResult", "hasSeverity", "hasPriority",
    "hasDescription", "hasType", "hasRate", "hasAmount",
})


def _enforce_class_specific_dps(g: Graph, steel_str: str) -> dict:
    """Path B — class-specific DP 위반 감지 + 모드별 대응 (기능 단계).

    T-Box 생성 후처리에서 LLM Architect 가 04-property-rules.md 의 금지
    지시를 어기고 generic DP (hasValue 등) 또는 orphan/owl:Thing domain DP
    를 만든 경우 탐지하고, 환경변수 `TBOX_STRICT_CLASS_SPECIFIC` 에 따라
    대응한다.

    환경변수 `TBOX_STRICT_CLASS_SPECIFIC`:
      - "warn" (default): 경고 로그 + stats 기록, T-Box 유지
      - "rename": domain 이 단일 steel class 인 경우 해당 class prefix 를
                  붙여 rename (triple subject/object 일괄 치환)
      - "remove": generic DP + 관련 triple 전부 삭제 (Multi-Agent 재생성 유도)

    Detection:
      1. generic 이름 (hasValue / hasStatus / hasTimestamp 등 — _R1A_GENERIC_DP_NAMES)
      2. rdfs:domain 없음 (orphan DP)
      3. rdfs:domain = owl:Thing

    Args:
        g: T-Box 그래프 (in-place 수정).
        steel_str: 도메인 네임스페이스 prefix.

    Returns:
        {
          "class_specific_mode": str,                    # warn/rename/remove
          "class_specific_violations_generic_name": int,
          "class_specific_violations_orphan": int,
          "class_specific_violations_owl_thing": int,
          "class_specific_renamed": int,
          "class_specific_removed": int,
          "class_specific_sample_violations": [...],     # 상위 10건
        }
    """
    mode = os.getenv("TBOX_STRICT_CLASS_SPECIFIC", "warn").lower()
    if mode not in ("warn", "rename", "remove"):
        logger.warning(
            "TBOX_STRICT_CLASS_SPECIFIC='%s' 알 수 없음 — 'warn' 으로 폴백",
            mode,
        )
        mode = "warn"

    stats: dict = {
        "class_specific_mode": mode,
        "class_specific_violations_generic_name": 0,
        "class_specific_violations_orphan": 0,
        "class_specific_violations_owl_thing": 0,
        "class_specific_renamed": 0,
        "class_specific_removed": 0,
        "class_specific_sample_violations": [],
    }

    violations: list[dict] = []
    # 모든 도메인 네임스페이스 DP 순회
    for dp in list(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        local = _local_name(str(dp))
        if not local:
            continue

        # 3가지 위반 유형 감지
        is_generic = local in _R1A_GENERIC_DP_NAMES
        domains = [d for d in g.objects(dp, RDFS.domain) if isinstance(d, URIRef)]
        is_orphan = not domains
        has_owl_thing = any(str(d) == str(OWL.Thing) for d in domains)

        if not (is_generic or is_orphan or has_owl_thing):
            continue

        if is_generic:
            stats["class_specific_violations_generic_name"] += 1
        if is_orphan:
            stats["class_specific_violations_orphan"] += 1
        if has_owl_thing:
            stats["class_specific_violations_owl_thing"] += 1

        # domain 이 정확히 1개인 경우 rename 가능
        domain_classes = [
            _local_name(str(d)) for d in domains
            if str(d).startswith(steel_str)
        ]
        can_rename = len(domain_classes) == 1 and bool(domain_classes[0])

        violations.append({
            "dp": local,
            "reasons": [
                r for r, v in (
                    ("generic_name", is_generic),
                    ("orphan_domain", is_orphan),
                    ("owl_thing_domain", has_owl_thing),
                ) if v
            ],
            "domain_classes": domain_classes,
            "can_rename": can_rename,
        })

        if mode == "rename" and can_rename:
            cls = domain_classes[0]
            # class prefix 주입 — classCamelLower + Bare
            class_camel_lower = cls[:1].lower() + cls[1:]
            # generic 이름의 'has' prefix 제거 후 결합
            bare = local[3:] if local.startswith("has") else local
            new_local = class_camel_lower + bare
            new_uri = URIRef(steel_str + new_local)
            if (new_uri, RDF.type, OWL.DatatypeProperty) in g:
                # 충돌 — 이미 존재하는 이름. skip rename.
                continue
            # 모든 위치에서 subject/predicate/object 치환
            triples_to_add: list = []
            triples_to_remove: list = []
            for s, p, o in g:
                if s == dp:
                    triples_to_add.append((new_uri, p, o))
                    triples_to_remove.append((s, p, o))
                elif p == dp:
                    triples_to_add.append((s, new_uri, o))
                    triples_to_remove.append((s, p, o))
                elif o == dp:
                    triples_to_add.append((s, p, new_uri))
                    triples_to_remove.append((s, p, o))
            for t in triples_to_remove:
                g.remove(t)
            for t in triples_to_add:
                g.add(t)
            stats["class_specific_renamed"] += 1
            logger.info(
                "Step 12b: %s → %s (class-specific rename)",
                local, new_local,
            )

        elif mode == "remove":
            # 해당 DP 의 모든 triple 삭제 (subject / predicate / object 모든 위치)
            for s, p, o in list(g):
                if s == dp or p == dp or o == dp:
                    g.remove((s, p, o))
            stats["class_specific_removed"] += 1
            logger.warning(
                "Step 12b: %s 삭제 (generic/orphan/owl-Thing DP, remove 모드)",
                local,
            )

        else:
            # warn 모드 — T-Box 유지, 경고만
            logger.warning(
                "Step 12b: %s 가 Path B 위반 (reasons=%s) — warn 모드라 유지",
                local, violations[-1]["reasons"],
            )

    stats["class_specific_sample_violations"] = violations[:10]
    return stats


# Columns excluded from coverage denominator — PK 는 인스턴스 IRI 생성에
# 쓰이므로 DP 로 선언될 필요 없음. FK 는 ObjectProperty 로 변환됨.
_COVERAGE_EXCLUDED_SUFFIXES: tuple[str, ...] = ("id", "code")


def _column_is_fk(col_lower: str) -> bool:
    """CSV 컬럼이 FK 인지 rules/contracts/fk_patterns.json 기반으로 판별.

    ObjectProperty 로 변환되어 DP 커버리지 계산에서 제외.
    """
    try:
        from tools.abox_generation import _fk_column_to_class
        return _fk_column_to_class(col_lower) is not None
    except Exception as e:
        logger.debug("_column_is_fk 실패 (%s): %s", col_lower, e)
        return False


def _column_is_pk(col_lower: str, pk_columns: set[str]) -> bool:
    """해당 컬럼이 테이블의 PK (단일 혹은 복합 PK 구성 요소) 인지 여부."""
    return col_lower in pk_columns


#: ``_collect_expected_columns_per_class`` 결과 캐시 — ``(signature, result)``.
#: 이 함수는 rawdata CSV **전체 행** 을 메모리에 올려 PK uniqueness 를 판정하므로
#: (실측 2026-07-25: 13 테이블 4.2초) 한 T-Box 개선 실행에서 여러 step 이 호출하면
#: 그만큼 곱해진다. 디렉토리 내 CSV 의 (경로, mtime, size) 목록을 서명으로 삼아
#: 무효화한다 — CSV 가 바뀌면 자동 재계산된다.
_EXPECTED_COLUMNS_CACHE: tuple[object, dict[str, dict]] | None = None


def _rawdata_signature(rawdata_dir: str | None = None) -> tuple:
    """rawdata CSV 집합의 (경로, mtime, size) 서명 — 캐시 무효화용.

    캐시 키는 캐시되는 함수가 **실제로 읽는** 디렉토리를 반영해야 한다. 이 모듈의
    로더들은 모듈 전역 ``SOURCE_RAWDATA_DIR`` 을 참조하고 테스트는 그 전역을
    monkeypatch 하므로, 기본값도 모듈 전역이다. ``config`` 를 직접 읽으면 키가
    엉뚱한 디렉토리를 가리켜 **다른 디렉토리의 결과를 캐시 히트로 반환** 한다
    (2026-07-25: 이 실수로 CSV 를 임시 디렉토리로 바꾼 테스트 2개가 실데이터 결과를
    받았다). 다른 모듈에서 쓸 때는 그 모듈이 읽는 경로를 명시로 넘겨라.

    디렉토리 경로 자체를 서명 첫 항목에 넣는다 — 빈 디렉토리끼리 구분되지 않으면
    같은 문제가 재발한다.
    """
    target = rawdata_dir if rawdata_dir is not None else SOURCE_RAWDATA_DIR
    if not os.path.isdir(target):
        return ("__missing__", target)
    entries: list[tuple] = [("__dir__", target, 0)]
    for path in sorted(glob.glob(os.path.join(target, "*.csv"))):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append((path, stat.st_mtime, stat.st_size))
    return tuple(entries)


def _collect_expected_columns_per_class() -> dict[str, dict]:
    """CSV 스캔 → {class_name: {table, expected_columns, pk_columns, fk_columns}}.

    expected_columns: PK/FK 를 제외한 순수 데이터 컬럼 집합 (class-specific DP
    로 반드시 등장해야 할 대상). class_name 은 abox_generation 컨벤션 (PascalCase,
    ``table.replace("_", "")``) 을 따른다.

    결과는 rawdata 서명 기준으로 캐시된다 (``_EXPECTED_COLUMNS_CACHE``).

    Returns:
        dict keyed by class name (case-preserved), each value = {
          "table": str,                         # CSV table name (원본)
          "pk_columns": set[str],               # lowercased PK 컬럼 이름들
          "fk_columns": set[str],               # lowercased FK 컬럼 이름들
          "expected_columns": set[str],         # lowercased data 컬럼 이름들
          "all_columns": list[str],             # 헤더 원본 순서 유지
        }
    """
    global _EXPECTED_COLUMNS_CACHE

    from tools.abox_generation import (  # lazy to avoid cycles
        _detect_pk_column,
        _load_table_class_mapping,
    )

    signature = _rawdata_signature()
    if _EXPECTED_COLUMNS_CACHE is not None and _EXPECTED_COLUMNS_CACHE[0] == signature:
        return _EXPECTED_COLUMNS_CACHE[1]

    # table_class_mapping.json 의 도메인 클래스명을 A-Box 인스턴스 타이핑과 동일하게
    # 사용한다. 이게 없으면 클래스명이 table.replace("_","") (테이블명 클래스) 가 되어
    # step_12d 가 인스턴스(도메인 클래스)와 다른 클래스에 DP 를 주입 → A-Box 커버리지
    # 0% (2026-06-23 규명: MaterialSpecA 등 와이드 테이블). A-Box _table_to_class
    # 와 같은 매핑을 공유해 일관성 보장.
    _table_class_map = _load_table_class_mapping()

    result: dict[str, dict] = {}
    if not os.path.isdir(SOURCE_RAWDATA_DIR):
        _EXPECTED_COLUMNS_CACHE = (signature, result)
        return result

    for csv_path in sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))):
        table_name = os.path.basename(csv_path).replace(".csv", "")
        # 1순위: table_class_mapping 의 도메인 클래스 (A-Box 와 동일). 없으면 폴백.
        class_name = _table_class_map.get(table_name) or table_name.replace("_", "")
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception as e:
            logger.debug("coverage: CSV read failed for %s: %s", csv_path, e)
            continue
        if not rows:
            continue

        all_columns = list(rows[0].keys())

        # PK detection — uniqueness 기반 (단일 또는 복합)
        pk = _detect_pk_column(rows, class_name.lower())
        if pk is None:
            pk_columns: set[str] = set()
        elif isinstance(pk, str):
            pk_columns = {pk.lower()}
        else:  # list[str] — composite
            pk_columns = {c.lower() for c in pk}

        # FK detection — rules/contracts/fk_patterns.json 경유
        fk_columns: set[str] = set()
        data_columns: set[str] = set()
        for col in all_columns:
            col_lower = col.lower()
            col_nosep = col_lower.replace("_", "")
            if _column_is_pk(col_lower, pk_columns) or _column_is_pk(col_nosep, pk_columns):
                continue
            if _column_is_fk(col_nosep) or _column_is_fk(col_lower):
                fk_columns.add(col_lower)
                continue
            data_columns.add(col_lower)

        result[class_name] = {
            "table": table_name,
            "pk_columns": pk_columns,
            "fk_columns": fk_columns,
            "expected_columns": data_columns,
            "all_columns": all_columns,
        }
    _EXPECTED_COLUMNS_CACHE = (signature, result)
    return result


def _count_class_dps(g: Graph, steel_str: str, class_name: str) -> set[str]:
    """Graph 에서 domain=class_name 인 DatatypeProperty 의 local name 집합 반환.

    Class prefix 를 제거한 **bare suffix** 도 같이 반환하면 coverage 매칭에
    유리하나, 현재는 prefix-stripped form 으로 CSV 컬럼명 lowercase 와 비교.
    """
    class_uri = URIRef(steel_str + class_name)
    declared: set[str] = set()
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        domains = list(g.objects(dp, RDFS.domain))
        if class_uri not in domains:
            continue
        local = _local_name(str(dp))
        if local:
            declared.add(local.lower())
    return declared


def _collect_class_dp_source_columns(
    g: Graph, steel_str: str, class_name: str,
) -> set[str]:
    """domain=class_name 인 DP 들이 ``dcterms:source`` 로 선언한 컬럼 코드 집합.

    반환값은 대문자 정규화된 CSV 컬럼 코드. ``_coverage_match`` 의 step 0 이
    이름 유사도 대신 이 집합으로 커버리지를 확정한다 (S2 의 한글명 기반 작명이
    컬럼 코드와 달라도 정확히 매칭). 표기 없는 T-Box 에서는 빈 집합이 반환되어
    기존 이름 유사도 경로로 폴백한다.
    """
    class_uri = URIRef(steel_str + class_name)
    columns: set[str] = set()
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(steel_str)):
            continue
        if class_uri not in list(g.objects(dp, RDFS.domain)):
            continue
        for src in g.objects(dp, DC.source):
            text = str(src).strip()
            if text:
                columns.add(text.upper())
    return columns


# Chemical element abbreviation → full name mapping. 화학 성분 CSV 컬럼
# (C_Percent / Si_Percent 등) 을 Architect 가 풀네임으로 번역할 때 matcher 도
# 같은 매핑을 적용해야 coverage 감지가 가능.
_ELEMENT_ABBREVIATIONS: dict[str, str] = {
    "c": "carbon",
    "si": "silicon",
    "mn": "manganese",
    "p": "phosphorus",
    "s": "sulfur",
    "cr": "chromium",
    "ni": "nickel",
    "mo": "molybdenum",
    "fe": "iron",
    "cu": "copper",
    "al": "aluminum",
    "ti": "titanium",
    "v": "vanadium",
    "w": "tungsten",
    "co": "cobalt",
    "zn": "zinc",
    "mg": "magnesium",
    "ca": "calcium",
    "n": "nitrogen",
    "o": "oxygen",
    "h": "hydrogen",
}


def _expand_csv_column_tokens(csv_col_lower: str) -> list[str]:
    """CSV 컬럼명의 토큰 분해 + 화학 원소 축약 풀네임 확장.

    `c_percent` → ["c", "percent"] + ["carbon", "percent"]
    두 variant 모두 매칭 후보로 사용한다.

    Returns:
        매칭에 쓸 후보 조합 리스트 (underscore 제거, 소문자, 연결됨).
        예: `c_percent` → ["cpercent", "carbonpercent"]
    """
    tokens = [t for t in csv_col_lower.split("_") if t]
    if not tokens:
        return []
    variants: list[list[str]] = [tokens]
    # 화학 원소 축약 확장 — 어느 토큰이든 축약어면 풀네임으로 치환한 variant 생성
    for idx, tok in enumerate(tokens):
        if tok in _ELEMENT_ABBREVIATIONS:
            expanded = tokens.copy()
            expanded[idx] = _ELEMENT_ABBREVIATIONS[tok]
            if expanded not in variants:
                variants.append(expanded)
    return ["".join(v) for v in variants]


def _csv_column_prefix_overlap(csv_col_lower: str, class_camel_lower: str) -> str | None:
    """CSV 컬럼의 첫 토큰이 class prefix 의 일부와 겹칠 때 중복을 제거한 꼬리 반환.

    예: col=`equipment_name`, class=`equipmentmaster` → 첫 토큰 `equipment` 가
    class prefix 의 시작과 일치 → 중복 제거된 suffix `name` 반환.
    겹침 없으면 None.
    """
    tokens = [t for t in csv_col_lower.split("_") if t]
    if not tokens:
        return None
    first = tokens[0]
    if len(first) >= 3 and class_camel_lower.startswith(first):
        # 첫 토큰이 class prefix 머리와 일치 — 드롭하고 나머지 연결
        rest = tokens[1:]
        if rest:
            return "".join(rest)
    return None


def resolve_column_owner(
    csv_col_lower: str,
    class_camel_lower: str,
    declared_dps_lower: set[str],
    source_columns_upper: set[str] | None = None,
    *,
    max_stage: int = 6,
) -> tuple[str, int, tuple[str, ...]] | None:
    """CSV 컬럼을 **어느 DP 가 담당하는지** 판정한다. 커버리지 사다리의 정본.

    :func:`_coverage_match` 는 이 함수의 bool 래퍼다 — 사다리 본문을 여기 한 곳에만
    두어 두 판정이 갈라지지 않게 한다 (게이트와 기록자가 서로 다른 답을 내면 안 된다).

    판정 순서 (짧은 회로, 먼저 성립한 단계에서 멈춘다):
      0. **출처 표기** (``dcterms:source``) — 이미 확정. 이름 추측 불필요.
      1. direct               : ``status`` 그대로
      2. class-prefixed       : ``{classCamelLower}{Column}`` (R1A 규칙, 가장 엄격)
      3. prefix-dedup         : 첫 토큰이 class prefix 와 겹치면 드롭 후 재시도
      4. element-abbr         : 화학 원소 축약 확장 (``c_percent`` → ``carbonPercent``)
      5. suffix               : DP 가 컬럼명으로 끝남 — **느슨함**
      6. contains             : DP 가 컬럼명을 포함 — **가장 느슨함**

    Args:
        max_stage: 이 단계까지만 인정한다. 기록(write) 용도로 부를 때는 6(contains)을
            배제해 과매칭을 막는다 — 판정(read) 용도인 커버리지 게이트는 기존
            동작을 유지해야 하므로 기본값 6.

    Returns:
        ``(dp_local_lower, stage, candidates)`` — stage 0 은 출처 표기가 이미 있는
        경우로 ``dp_local_lower`` 가 빈 문자열이다 (담당 DP 를 특정하지 않음).
        후보가 2개 이상이면 ``dp_local_lower`` 가 빈 문자열이고 ``candidates`` 에
        전부 담긴다 — 호출부는 **기록하지 않아야** 한다.
        매칭 없으면 ``None``.
    """
    col_nosep = csv_col_lower.replace("_", "")
    if not col_nosep:
        return None

    # 0. 출처 표기 (authoritative)
    if source_columns_upper and csv_col_lower.upper() in source_columns_upper:
        return ("", 0, ())

    def _one(stage: int, hits: set[str]) -> tuple[str, int, tuple[str, ...]] | None:
        if stage > max_stage or not hits:
            return None
        ordered = tuple(sorted(hits))
        return (ordered[0] if len(ordered) == 1 else "", stage, ordered)

    # 1. direct
    got = _one(1, {col_nosep} & declared_dps_lower)
    if got:
        return got

    # 2. class-prefixed R1A 규칙
    prefixed = (class_camel_lower + col_nosep).lower()
    got = _one(2, {prefixed} & declared_dps_lower)
    if got:
        return got

    # 3. prefix-dedup
    dedup_tail = _csv_column_prefix_overlap(csv_col_lower, class_camel_lower)
    if dedup_tail:
        cands = {
            c for c in ((class_camel_lower + dedup_tail).lower(), dedup_tail)
            if c in declared_dps_lower
        }
        got = _one(3, cands)
        if got:
            return got

    # 4. element abbreviation expansion
    for variant in _expand_csv_column_tokens(csv_col_lower):
        if variant == col_nosep:
            continue
        cands = {
            c for c in ((class_camel_lower + variant).lower(), variant)
            if c in declared_dps_lower
        }
        if len(variant) >= 3:
            cands |= {
                dp for dp in declared_dps_lower
                if dp.endswith(variant) and len(dp) > len(variant)
            }
        got = _one(4, cands)
        if got:
            return got

    # 5. suffix
    if len(col_nosep) >= 3:
        got = _one(5, {
            dp for dp in declared_dps_lower
            if dp.endswith(col_nosep) and len(dp) > len(col_nosep)
        })
        if got:
            return got

    # 6. contains — 3자 미만은 과매칭 위험으로 skip
    if len(col_nosep) >= 4:
        got = _one(6, {dp for dp in declared_dps_lower if col_nosep in dp})
        if got:
            return got
    return None


def _coverage_match(csv_col_lower: str, class_camel_lower: str,
                    declared_dps_lower: set[str],
                    source_columns_upper: set[str] | None = None) -> bool:
    """CSV 컬럼이 T-Box 에 DP 로 선언되어 있는지 매칭 (bool).

    사다리 본문은 :func:`resolve_column_owner` 에 있다 — 이 함수는 그 결과를
    bool 로 좁힌 얇은 래퍼다. 두 곳에 사다리를 복제하면 게이트와 기록자가 서로
    다른 답을 내게 되므로 **절대 여기에 로직을 되돌리지 말 것**.
    """
    return resolve_column_owner(
        csv_col_lower, class_camel_lower, declared_dps_lower,
        source_columns_upper,
    ) is not None


def _cleanup_redundant_domain_after_union(g: Graph) -> int:
    """Remove URIRef domain/range that duplicates a unionOf BNode member.

    Step 9e (``_resolve_multi_domain``) creates ``owl:unionOf`` blank nodes
    when a property has ≥2 ``rdfs:domain`` declarations with no common
    ancestor. However, downstream steps (Step 13 owl:someValuesFrom
    restrictions, or manual post-hoc OP additions) may re-add URIRef
    domain/range triples that duplicate classes already inside the union.

    This leaves the property with BOTH:
      - ``<prop> rdfs:domain [ owl:unionOf (ClassA ClassB) ]``
      - ``<prop> rdfs:domain ClassA``  ← redundant, violates SHACL MaxCount(1)

    This cleanup iterates every property, detects BNode union domain/range
    with sibling URIRef domain/range triples whose URI appears in the
    union member list, and removes the URIRef triple. URIRef domains/ranges
    NOT covered by the union are preserved (safe guard for legitimate
    multi-domain scenarios that Step 9e did not process).

    Returns:
        Count of redundant triples removed (domain + range combined).
    """
    from rdflib.collection import Collection as _Coll

    removed = 0

    def _clean_one_predicate(pred: URIRef) -> None:
        nonlocal removed
        # Find all (prop, pred, *) triples once to avoid iteration mutation
        props_with_pred: set[URIRef] = set()
        for s, _p, _o in g.triples((None, pred, None)):
            if isinstance(s, URIRef):
                props_with_pred.add(s)

        for prop in props_with_pred:
            values = list(g.objects(prop, pred))
            bnode_unions = [v for v in values if isinstance(v, BNode)]
            uri_values = [v for v in values if isinstance(v, URIRef)]
            if not bnode_unions or not uri_values:
                continue
            # Collect union member URIs across all BNode unions for this prop
            union_members: set[URIRef] = set()
            for bn in bnode_unions:
                union_list = g.value(bn, OWL.unionOf)
                if union_list is None:
                    continue
                try:
                    members = list(_Coll(g, union_list))
                except Exception as e:
                    logger.debug("unionOf collection 파싱 실패 (%s): %s", bn, e)
                    continue
                for m in members:
                    if isinstance(m, URIRef):
                        union_members.add(m)
            # Remove URIRef values that are already inside the union
            for uri_val in uri_values:
                if uri_val in union_members:
                    g.remove((prop, pred, uri_val))
                    removed += 1

    _clean_one_predicate(RDFS.domain)
    _clean_one_predicate(RDFS.range)

    return removed


def _check_csv_column_coverage(g: Graph, steel_str: str,
                                threshold: float = 0.85) -> dict:
    """L2 Coverage Gate — class-specific DP 가 CSV 컬럼을 충분히 커버하는지 측정.

    각 CSV 테이블의 non-PK/FK 컬럼이 대응 ObjectClass 의 DatatypeProperty 로
    선언되었는지 검사. 85% 미만 커버리지면 경고 기록.

    환경변수 ``TBOX_COVERAGE_GATE``:
      - "warn" (default): 경고 로그 + stats 기록, T-Box 유지
      - "fail": coverage 미달 시 예외 발생 (파이프라인 중단 유도)

    환경변수 ``TBOX_COVERAGE_THRESHOLD``: 0.0~1.0 임계치 (default 0.85).

    Returns:
        {
          "coverage_mode": str,
          "coverage_threshold": float,
          "coverage_total_expected": int,
          "coverage_total_matched": int,
          "coverage_ratio_overall": float,
          "coverage_classes_ok": int,           # threshold 이상
          "coverage_classes_below": int,        # threshold 미만
          "coverage_per_class": [
            {
              "class": str,
              "table": str,
              "expected": int,
              "matched": int,
              "ratio": float,
              "missing": [col, ...],            # 상위 20개
              "below_threshold": bool,
            },
            ...
          ],
        }
    """
    mode = os.getenv("TBOX_COVERAGE_GATE", "warn").lower()
    if mode not in ("warn", "fail"):
        logger.warning(
            "TBOX_COVERAGE_GATE='%s' 알 수 없음 — 'warn' 으로 폴백", mode,
        )
        mode = "warn"

    try:
        threshold_env = os.getenv("TBOX_COVERAGE_THRESHOLD")
        if threshold_env is not None:
            threshold = max(0.0, min(1.0, float(threshold_env)))
    except ValueError:
        logger.warning(
            "TBOX_COVERAGE_THRESHOLD='%s' 파싱 실패 — default %.2f 유지",
            threshold_env, threshold,
        )

    expected_by_class = _collect_expected_columns_per_class()

    per_class: list[dict] = []
    total_expected = 0
    total_matched = 0
    classes_ok = 0
    classes_below = 0

    for class_name, info in expected_by_class.items():
        expected_cols: set[str] = info["expected_columns"]
        if not expected_cols:
            continue
        class_camel_lower = class_name[:1].lower() + class_name[1:]
        declared = _count_class_dps(g, steel_str, class_name)
        declared_sources = _collect_class_dp_source_columns(
            g, steel_str, class_name,
        )
        matched_cols: set[str] = set()
        missing_cols: list[str] = []
        for col_lower in expected_cols:
            if _coverage_match(col_lower, class_camel_lower, declared,
                               declared_sources):
                matched_cols.add(col_lower)
            else:
                missing_cols.append(col_lower)
        ratio = len(matched_cols) / len(expected_cols) if expected_cols else 1.0
        total_expected += len(expected_cols)
        total_matched += len(matched_cols)
        below = ratio < threshold
        if below:
            classes_below += 1
            logger.warning(
                "Step 12c coverage: %s (%s) — %d/%d (%.1f%%) < %.1f%% 임계치, "
                "누락 컬럼 상위: %s",
                class_name, info["table"], len(matched_cols), len(expected_cols),
                ratio * 100, threshold * 100,
                sorted(missing_cols)[:5],
            )
        else:
            classes_ok += 1
        per_class.append({
            "class": class_name,
            "table": info["table"],
            "expected": len(expected_cols),
            "matched": len(matched_cols),
            "ratio": round(ratio, 4),
            "missing": sorted(missing_cols)[:20],
            "below_threshold": below,
        })

    overall_ratio = (total_matched / total_expected) if total_expected else 1.0
    stats = {
        "coverage_mode": mode,
        "coverage_threshold": threshold,
        "coverage_total_expected": total_expected,
        "coverage_total_matched": total_matched,
        "coverage_ratio_overall": round(overall_ratio, 4),
        "coverage_classes_ok": classes_ok,
        "coverage_classes_below": classes_below,
        "coverage_per_class": per_class,
    }

    if mode == "fail" and classes_below > 0:
        # 파이프라인 중단 유도. caller (Step 12c) 가 예외 포착 후 로그만 남기고
        # 재발 방지는 S2 재실행에 의존한다. 여기서는 raise.
        raise RuntimeError(
            f"Coverage gate failed: {classes_below} class(es) below "
            f"{threshold * 100:.0f}% threshold — S2 재실행 또는 프롬프트 재검토 필요"
        )

    return stats


def _annotate_abstract_categories(g: Graph, steel_str: str) -> dict:
    """Lazy class + Management 접미사 클래스에 abstract category 어노테이션 부여.

    - 자신이 DP/OP 의 domain 또는 OP 의 range 로 참조되지 않는 steel-ns 클래스.
    - skos:Concept 타입 추가 + rdfs:comment 보강 (기존 주석 보존).
    - Management/Measurement/Monitoring/Event/Asset 등 카테고리 suffix 는
      특별히 scheme_hint annotation 까지 부여 (downstream 시각화 힌트).
    """
    lazy_annotated = 0
    management_tagged = 0

    from rdflib.namespace import SKOS

    # (DP/OP) domain/range 에서 참조되는 클래스 수집
    referenced: set[URIRef] = set()
    for p in (RDFS.domain, RDFS.range):
        for _, _, obj in g.triples((None, p, None)):
            if isinstance(obj, URIRef):
                referenced.add(obj)

    all_classes = [
        c for c in g.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(steel_str)
    ]

    _CATEGORY_SUFFIXES = (
        "Management", "Measurement", "Monitoring", "Event", "Asset",
        "Infrastructure", "Consumption", "Transaction", "Master",
        "Mapping", "Inspection", "Property", "Pattern", "Specification",
        "ProcessStep", "Rule",
    )

    for cls in all_classes:
        if cls in referenced:
            continue
        local = str(cls).split("#")[-1]
        # SKOS:Concept 타입 추가 (이미 있으면 no-op)
        if (cls, RDF.type, SKOS.Concept) not in g:
            g.add((cls, RDF.type, SKOS.Concept))
        # 카테고리 설명 보강 — 기존 주석이 없을 때만 (주석 중복 방지)
        has_en_comment = any(
            isinstance(c, Literal) and (getattr(c, "language", None) or "").startswith("en")
            and "abstract category" in str(c).lower()
            for c in g.objects(cls, RDFS.comment)
        )
        if not has_en_comment:
            g.add((cls, SKOS.scopeNote, Literal(_CATEGORY_COMMENT_EN, lang="en")))
            g.add((cls, SKOS.scopeNote, Literal(_CATEGORY_COMMENT_KO, lang="ko")))
            lazy_annotated += 1
        # Management/Category suffix 에 scheme hint (visualize/search 힌트)
        if any(local.endswith(sfx) for sfx in _CATEGORY_SUFFIXES):
            hint_lit = Literal("category", lang="en")
            if (cls, SKOS.notation, hint_lit) not in g:
                g.add((cls, SKOS.notation, hint_lit))
                management_tagged += 1

    return {
        "abstract_categories_annotated": lazy_annotated,
        "category_suffix_tagged": management_tagged,
    }


# ── OntoClean Labeling (R12, 전문가 리뷰 C3) ──────
# rules/domain/ontoclean_labels.json 의 rigidity/unity/identity/dependence 를
# T-Box 에 steel-ontoclean: annotation 으로 주입. validate_ontoclean 검증기
# 가 읽어 C1/C2/C3 규칙 위반을 자동 탐지하게 된다.


def _load_ontoclean_labels() -> dict:
    path = rules_path("ontoclean_labels.json", base=_RULES_DIR)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("class_labels", {})
    except Exception as e:
        logger.warning("ontoclean_labels.json 로드 실패: %s", e)
        return {}


# ── Heuristic suffix dictionaries for auto-annotation (T2) ────────
# Class name suffixes map deterministically to OntoClean meta-properties.
# Manual labels in rules/domain/ontoclean_labels.json always override these defaults.

_RIGIDITY_ANTI_SUFFIXES = ("Status", "State", "Phase", "Mode", "Role")
_UNITY_AGGREGATE_SUFFIXES = ("Group", "Collection", "Set")
_IDENTITY_STRONG_SUFFIXES = ("Master", "Event", "History", "Record")
_DEPENDENCE_DEPENDENT_SUFFIXES = (
    "Event", "History", "Record", "Transaction",
    "Monitoring", "Consumption", "Result", "Status", "Observation",
    "Measurement", "Analysis", "Inspection",
)


def _heuristic_ontoclean_labels(
    g: Graph, steel_str: str, manual: set[str]
) -> dict[str, dict]:
    """Derive OntoClean labels for domain classes missing manual entries.

    Looks at class local-name suffix plus presence of owl:hasKey to emit
    rigidity / unity / identity / dependence defaults. Classes outside
    the domain namespace or already present in ``manual`` are skipped so
    hand-curated labels always win.
    """
    auto: dict[str, dict] = {}
    for cls in g.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        cls_str = str(cls)
        if not cls_str.startswith(steel_str):
            continue
        local = cls_str[len(steel_str):]
        if not local or local in manual:
            continue

        rigidity = "-R" if local.endswith(_RIGIDITY_ANTI_SUFFIXES) else "+R"
        unity = "-U" if local.endswith(_UNITY_AGGREGATE_SUFFIXES) else "+U"
        has_key = (cls, OWL.hasKey, None) in g
        identity = "+I" if (has_key or local.endswith(_IDENTITY_STRONG_SUFFIXES)) else "-I"
        dependence = "+D" if local.endswith(_DEPENDENCE_DEPENDENT_SUFFIXES) else "-D"

        auto[local] = {
            "rigidity": rigidity,
            "unity": unity,
            "identity": identity,
            "dependence": dependence,
        }
    return auto


def _inject_ontoclean_annotations(g: Graph, steel_str: str) -> dict:
    """rules/domain/ontoclean_labels.json 의 meta-property 를 T-Box 에 주입.

    Manual labels (rules/domain/ontoclean_labels.json) take priority. Classes
    not covered there fall through to `_heuristic_ontoclean_labels` so
    validate_ontoclean keeps coverage on new domains.

    Update semantics: 같은 (cls, pred) 에 기존 값이 있으면 제거 후 새 값 주입.
    (labels.json 이 수정되어도 T-Box 가 최신 값으로 동기화되도록 한다.)
    """
    from domain.graph_utils import literal_values_equal

    labels = _load_ontoclean_labels()
    auto_labels = _heuristic_ontoclean_labels(
        g, steel_str, manual=set(labels.keys())
    )
    # manual 우선: auto 를 먼저 펼치고 labels 로 덮어쓴다.
    merged: dict[str, dict] = {**auto_labels, **labels}
    if not merged:
        return {
            "ontoclean_classes_labeled": 0,
            "ontoclean_classes_manual": 0,
            "ontoclean_classes_auto_labeled": 0,
            "ontoclean_triples_added": 0,
            "ontoclean_triples_replaced": 0,
        }
    oc_ns = steel_str.rstrip("#") + "-ontoclean#"
    P_RIGIDITY = URIRef(oc_ns + "rigidity")
    P_UNITY = URIRef(oc_ns + "unity")
    P_IDENTITY = URIRef(oc_ns + "identity")
    P_IDENTITY_BY = URIRef(oc_ns + "identityCriterion")
    P_DEPENDENCE = URIRef(oc_ns + "dependence")
    # T2: auto-labeled 클래스에 고정 플래그 주입 — analyze_ontoclean 이
    # manual curated vs heuristic fallback 을 분리해 coverage 해석 가능.
    P_AUTO_LABEL = URIRef(oc_ns + "autoLabel")
    labeled = 0
    triples_added = 0
    triples_replaced = 0
    for cls_local, meta in merged.items():
        cls_uri = URIRef(steel_str + cls_local)
        if (cls_uri, RDF.type, OWL.Class) not in g:
            continue
        is_auto = cls_local in auto_labels and cls_local not in labels
        any_changed = False
        mapping = [
            (P_RIGIDITY, meta.get("rigidity")),
            (P_UNITY, meta.get("unity")),
            (P_IDENTITY, meta.get("identity")),
            (P_IDENTITY_BY, meta.get("identity_by")),
            (P_DEPENDENCE, meta.get("dependence")),
        ]
        for pred, val in mapping:
            if not val:
                continue
            lit = Literal(val)
            current = list(g.objects(cls_uri, pred))
            # **값 기준으로 비교한다.** ``current == [lit]`` 는 기본 store
            # (Oxigraph) 가 리터럴에 ``xsd:string`` 을 붙여 되돌려주기 때문에
            # 영구히 실패했다 — 매 실행 63개 클래스를 지우고 다시 쓰면서
            # ``added=283 / replaced=283`` 을 보고하는데 실제 그래프는 변하지
            # 않는다 (isomorphic 동일, 3회 연속 재현). Memory store 에서는 1회 후
            # 0 이 나와 테스트가 이 결함을 놓쳤다.
            if len(current) == 1 and literal_values_equal(current[0], lit):
                continue  # already correct
            # remove stale values + add new
            for old in current:
                g.remove((cls_uri, pred, old))
                triples_replaced += 1
            g.add((cls_uri, pred, lit))
            triples_added += 1
            any_changed = True
        # auto flag: True 로 toggle (auto 에서 manual 로 승격 시 제거)
        auto_flag = Literal(True)
        has_auto_flag = auto_flag in g.objects(cls_uri, P_AUTO_LABEL)
        if is_auto and not has_auto_flag:
            g.add((cls_uri, P_AUTO_LABEL, auto_flag))
        elif not is_auto and has_auto_flag:
            g.remove((cls_uri, P_AUTO_LABEL, auto_flag))
        if any_changed:
            labeled += 1
    manual_applied = len([c for c in labels if c in merged])
    auto_applied = len([c for c in auto_labels if c not in labels])
    return {
        "ontoclean_classes_labeled": labeled,
        "ontoclean_classes_manual": manual_applied,
        "ontoclean_classes_auto_labeled": auto_applied,
        "ontoclean_triples_added": triples_added,
        "ontoclean_triples_replaced": triples_replaced,
    }


# ── Module Annotation (R12, 전문가 리뷰 M1) ──────
# 단일 T-Box 파일 유지하되 dcterms:isPartOf 로 논리적 module 소속을 표시해
# downstream 도구가 module 별 필터링/시각화 가능하게 한다. 실제 파일 분리는
# 파이프라인 전체 재설계가 필요하므로 점진적 접근.


_MODULE_KEYWORDS: dict[str, list[str]] = {
    "equipment": [
        "Equipment", "Tag", "Alarm", "Maintenance", "Failure",
    ],
    "production": [
        "Production", "Process", "Product", "Manufacturing", "Shift",
    ],
    "quality": [
        "Quality", "Inspection", "Material", "Chemical", "Mechanical",
        "Dimensional", "Surface", "NDT",
    ],
    "energy": [
        "Energy", "Fuel", "Electrical", "Gas", "Steam", "Consumption",
    ],
    "environment": [
        "Emission", "Monitoring", "Waste", "Noise", "Vibration", "Soil",
        "Water", "Air", "Atmospheric", "GHG", "Scope", "Environmental",
    ],
    "supply": [
        "Supply", "Item", "Supplier", "Warehouse", "Inventory",
        "Purchase", "Transportation", "Transport",
    ],
    "sensor": [
        "RealTime", "MonitoringPoint",
    ],
}


def _annotate_module_membership(g: Graph, steel_str: str) -> dict:
    """클래스/프로퍼티 local name 의 prefix 매칭으로 module 소속 어노테이션.

    각 entity 에 dcterms:isPartOf <steel-ns#module-{name}> 추가.
    module URI 자체도 owl:Ontology 타입으로 선언해 visualize 도구가 인식.
    """
    dcterms_isPartOf = URIRef("http://purl.org/dc/terms/isPartOf")
    module_uris: dict[str, URIRef] = {
        name: URIRef(steel_str + f"module-{name}") for name in _MODULE_KEYWORDS
    }
    # 각 module URI 선언 — skos:Collection 타입 사용.
    # owl:Ontology 로 선언하면 FAIR evaluator 가 module URI 를 메인 Ontology
    # 대신 평가 대상으로 잘못 선택하는 regression 발생.
    # 이전 실행에서 주입된 owl:Ontology 타입이 남아있으면 제거 (migration).
    from rdflib.namespace import SKOS as _SKOS
    for name, uri in module_uris.items():
        if (uri, RDF.type, OWL.Ontology) in g:
            g.remove((uri, RDF.type, OWL.Ontology))
        if (uri, RDF.type, _SKOS.Collection) not in g:
            g.add((uri, RDF.type, _SKOS.Collection))
            g.add((uri, RDFS.label, Literal(f"{name} module", lang="en")))

    def _infer_module(local: str) -> str | None:
        for mod, kws in _MODULE_KEYWORDS.items():
            for kw in kws:
                if kw in local:
                    return mod
        return None

    annotated = 0
    for entity_type in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty):
        for s in g.subjects(RDF.type, entity_type):
            if not (isinstance(s, URIRef) and str(s).startswith(steel_str)):
                continue
            local = str(s).split("#")[-1]
            if local.startswith("module-"):
                continue
            mod = _infer_module(local)
            if mod is None:
                continue
            target_uri = module_uris[mod]
            if (s, dcterms_isPartOf, target_uri) not in g:
                g.add((s, dcterms_isPartOf, target_uri))
                annotated += 1
    return {
        "module_annotated": annotated,
        "modules_declared": len(module_uris),
    }


# ── Label Synthesis (라벨 누락 자동 생성) ──────


def _local_name_to_human(name: str) -> str:
    """camelCase / PascalCase local name → 공백 분리 소문자 문자열 (공용 헬퍼 별칭).

    예: "hasEquipmentMaster" → "has equipment master"
        "GHGEmission" → "ghg emission"
    """
    from domain.graph_utils import humanize_local_name
    return humanize_local_name(name)


_KO_HINTS = {
    "master": "마스터", "equipment": "설비", "product": "제품", "process": "공정",
    "alarm": "알람", "event": "이벤트", "failure": "고장", "maintenance": "정비",
    "emission": "배출", "quality": "품질", "analysis": "분석", "energy": "에너지",
    "fuel": "연료", "gas": "가스", "steam": "스팀", "electrical": "전력",
    "transaction": "거래", "inventory": "재고", "supplier": "공급업체",
    "warehouse": "창고", "monitoring": "모니터링", "point": "지점",
    "batch": "배치", "continuous": "연속", "blast": "고로", "furnace": "로",
    "rolling": "압연", "casting": "주조", "chemical": "화학", "mechanical": "기계",
    "dimensional": "치수", "surface": "표면", "ndt": "비파괴검사",
    "data": "데이터", "code": "코드", "status": "상태", "history": "이력",
    "has": "가진", "uses": "사용", "monitors": "모니터링", "produces": "생산",
    "requires": "필요", "triggered": "촉발", "consumed": "소비",
    "air": "대기", "water": "수질", "soil": "토양", "noise": "소음", "vibration": "진동",
    "waste": "폐기물", "safety": "안전", "stock": "재고", "item": "품목",
    "purchase": "구매", "order": "주문", "transport": "운송",
}


def _synthesize_korean_label(english_text: str) -> str:
    """영문 human-readable label → 한국어 단어 치환 추정. 완벽하지 않아도 placeholder."""
    parts = english_text.split()
    translated = []
    for p in parts:
        lower = p.lower()
        translated.append(_KO_HINTS.get(lower, p))
    return " ".join(translated)


def _inject_missing_labels(g: Graph, steel_str: str) -> dict[str, int]:
    """라벨 없는 owl:Class / ObjectProperty / DatatypeProperty 에 rdfs:label 주입.

    @en 라벨이 하나도 없으면 local name → human-readable 문자열로 생성.
    @ko 라벨이 하나도 없으면 _synthesize_korean_label 로 생성.
    이미 같은 언어 태그가 있는 엔티티는 건드리지 않는다.

    Returns: {added_en: int, added_ko: int, subjects_fixed: int}
    """
    added_en = 0
    added_ko = 0
    fixed = set()
    targets = (
        list(g.subjects(RDF.type, OWL.Class))
        + list(g.subjects(RDF.type, OWL.ObjectProperty))
        + list(g.subjects(RDF.type, OWL.DatatypeProperty))
    )
    for subj in targets:
        if not (isinstance(subj, URIRef) and str(subj).startswith(steel_str)):
            continue
        existing = list(g.objects(subj, RDFS.label))
        langs = {
            getattr(lab, "language", None)
            for lab in existing if isinstance(lab, Literal)
        }
        local = str(subj).split("#")[-1].split("/")[-1]
        human = _local_name_to_human(local)
        if "en" not in langs:
            g.add((subj, RDFS.label, Literal(human, lang="en")))
            added_en += 1
            fixed.add(subj)
        if "ko" not in langs:
            ko = _synthesize_korean_label(human)
            g.add((subj, RDFS.label, Literal(ko, lang="ko")))
            added_ko += 1
            fixed.add(subj)
    return {
        "added_en": added_en,
        "added_ko": added_ko,
        "subjects_fixed": len(fixed),
    }


# ── Completeness Annotation (CWA/OWA 브릿지) ────


def _annotate_completeness(
    g: Graph,
    csv_classes: set[str],
    tacit_classes: set[str],
    inferred_classes: set[str],
) -> int:
    """클래스별 completenessStatus 주석 추가 (OWA/CWA 브릿지).

    OWL은 Open World Assumption이지만 CSV 기반 데이터는 사실상 Closed World다.
    각 클래스에 steel:completenessStatus를 부여하여 검증 시 심각도를 분기한다.

    Args:
        g: T-Box rdflib Graph (in-place 수정).
        csv_classes: CSV 데이터로 뒷받침되는 클래스 로컬 이름 집합 → "closed".
        tacit_classes: 암묵지에서만 정의된 클래스 로컬 이름 집합 → "open".
        inferred_classes: 추론으로만 생성되는 클래스 로컬 이름 집합 → "inferred".

    ## 술어를 **선언**한다 — 참조하면 선언하라 (2026-08-31)

    예전에는 84개 클래스에 이 술어를 쓰면서 T-Box 에 선언하지 않았다. 그래서
    ``check_undeclared_dp`` 가 매 실행 위반 1건으로 FAIL 했다 (사용 84회).

    **``owl:AnnotationProperty`` 로 선언한다** — DP 가 아니다. 이 술어의 주어는
    84개 전부 ``owl:Class`` 이고 (실측), DatatypeProperty 는 **개체**에 붙는 것이라
    클래스에 쓰면 OWL 2 DL 위반이다 (punning). 어노테이션은 클래스에 붙어도 되고
    추론기가 무시하므로 의미론에 영향이 없다 — 이 술어의 용도(검증 심각도 분기)에
    정확히 맞다.

    ``check_undeclared_dp`` 는 이미 ``AnnotationProperty`` 를 선언으로 인정한다
    (``referential.py`` 의 ``prop_type`` 루프) — 게이트를 느슨하게 하는 것이 아니라
    올바른 종류로 선언하는 것이다.

    Returns:
        주석이 추가된 클래스 수.
    """
    # 술어 자체를 먼저 선언한다. 재실행 안전 — rdflib 는 같은 트리플을 중복 저장하지
    # 않는다. 주석을 하나도 붙이지 않는 경우(클래스 0개)에는 선언도 남기지 않으려면
    # 루프 뒤로 옮겨야 하지만, 선언만 있고 사용이 없는 상태는 무해하고(어노테이션은
    # 추론에 영향 없음) 사용만 있고 선언이 없는 상태는 게이트를 깨뜨린다.
    g.add((DOMAIN_NS_OBJ.completenessStatus, RDF.type, OWL.AnnotationProperty))
    g.add((DOMAIN_NS_OBJ.completenessStatus, RDFS.label,
           Literal("완전성 상태", lang="ko")))
    g.add((DOMAIN_NS_OBJ.completenessStatus, RDFS.label,
           Literal("completeness status", lang="en")))
    g.add((DOMAIN_NS_OBJ.completenessStatus, RDFS.comment, Literal(
        "이 클래스의 인스턴스 집합이 닫혀 있는가 — closed(CSV 전수) / "
        "open(암묵지·부분) / inferred(추론 전용, 검증 스킵). 카디널리티 위반의 "
        "심각도를 분기하는 데 쓴다 (OWA↔CWA 브릿지). 주어가 클래스이므로 "
        "DatatypeProperty 가 아니라 AnnotationProperty 다.", lang="ko")))

    count = 0
    for cls in g.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue  # skip BNodes (anonymous restriction classes)
        local = _local_name(str(cls))
        if not local:
            continue
        # 재실행 안전: 기존 주석 제거
        g.remove((cls, DOMAIN_NS_OBJ.completenessStatus, None))

        if local in csv_classes:
            status = "closed"
        elif local in tacit_classes and local not in csv_classes:
            status = "open"
        elif local in inferred_classes and local not in csv_classes and local not in tacit_classes:
            status = "inferred"
        else:
            status = "open"

        g.add((cls, DOMAIN_NS_OBJ.completenessStatus, Literal(status)))
        count += 1
    return count


# ── 메인 함수 ────────────────────────────────────


def _compute_steel_parents_map(g: Graph, steel_str: str) -> dict[URIRef, set[URIRef]]:
    """steel 네임스페이스 내 클래스와 그 부모(rdfs:subClassOf, steel 내부) 매핑.

    Redundant subClassOf 탐지용 transitive closure 계산의 기반.
    """
    classes = {
        c for c in g.subjects(RDF.type, OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(steel_str)
    }
    parents: dict[URIRef, set[URIRef]] = {}
    for cls in classes:
        parents[cls] = {
            p for p in g.objects(cls, RDFS.subClassOf)
            if isinstance(p, URIRef) and p in classes
        }
    return parents


def _is_ancestor_via(parents: dict[URIRef, set[URIRef]],
                    start: URIRef, target: URIRef) -> bool:
    """start의 조상 중 target이 포함되는지 DFS로 판정."""
    visited: set[URIRef] = set()
    stack: list[URIRef] = [start]
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for gp in parents.get(cur, set()):
            if gp == target:
                return True
            stack.append(gp)
    return False


def _remove_redundant_subclass(g: Graph, steel_str: str) -> int:
    """부모 중 다른 부모의 조상에 해당하는 연결을 제거한다.

    예) A subClassOf B, A subClassOf C, C subClassOf B → (A, subClassOf, B) 제거.
    반환: 제거된 트리플 수.
    """
    parents = _compute_steel_parents_map(g, steel_str)
    removed = 0
    for cls, parent_set in parents.items():
        if len(parent_set) < 2:
            continue
        to_remove: set[tuple[URIRef, URIRef, URIRef]] = set()
        for pa in parent_set:
            for pb in parent_set:
                if pa == pb:
                    continue
                if _is_ancestor_via(parents, pa, pb):
                    to_remove.add((cls, RDFS.subClassOf, pb))
        for triple in to_remove:
            g.remove(triple)
            removed += 1
    return removed


_PK_SUFFIX_RE = re.compile(r"(Id|ID|Code)$")
_CLASS_TAIL_RE = re.compile(r"(Master|History|Monitoring|Consumption|Emission|Events|Event|Transaction|Plan|Order|Result|Pattern|Map|Quality|Energy|Management|Log|Record|Specification|Status|Data)$")


#: ``_build_csv_pk_map`` 결과 캐시 — ``(rawdata 서명, result)``.
#: 이 함수도 CSV 전체 행을 읽어 PK uniqueness 를 판정하므로 (실측 3.4초) 여러
#: step 이 호출하면 그만큼 곱해진다.
_CSV_PK_MAP_CACHE: tuple[object, dict[str, set[str]]] | None = None


def _build_csv_pk_map() -> dict[str, set[str]]:
    """Return {class_name_lower: {pk_column_lower, ...}} from CSV uniqueness.

    Uses the same uniqueness-based detection as the A-Box generator to discover
    the real PK column(s) of each source table.  Compound PK tables contribute
    all their key components so that any single component still qualifies for
    lookup; the Functional guard uses _functional_gate_from_pk to reject
    composite-PK components at decision time.

    Cached by rawdata signature (see ``_CSV_PK_MAP_CACHE``).
    """
    global _CSV_PK_MAP_CACHE

    from tools.abox_generation import _detect_pk_column  # lazy to avoid cycles

    signature = _rawdata_signature()
    if _CSV_PK_MAP_CACHE is not None and _CSV_PK_MAP_CACHE[0] == signature:
        return _CSV_PK_MAP_CACHE[1]

    pk_map: dict[str, set[str]] = {}
    try:
        if not os.path.isdir(SOURCE_RAWDATA_DIR):
            return pk_map
        for csv_path in sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))):
            table_name = os.path.basename(csv_path).replace(".csv", "")
            class_name = table_name.replace("_", "").lower()
            try:
                with open(csv_path, encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    rows = list(reader)
            except Exception as e:
                logger.debug("CSV read failed for %s: %s", csv_path, e)
                continue
            if not rows:
                continue
            pk = _detect_pk_column(rows, class_name)
            if pk is None:
                continue
            cols = {pk} if isinstance(pk, str) else set(pk)
            pk_map[class_name] = {c.replace("_", "").lower() for c in cols}
    except Exception as e:
        logger.debug("_build_csv_pk_map failed: %s", e)
    _CSV_PK_MAP_CACHE = (signature, pk_map)
    return pk_map


def _inject_property_chain_axioms(g: Graph, chains: list[dict]) -> dict:
    """Declare ``owl:propertyChainAxiom`` for each chain whose parts all exist.

    Each chain dict names a target ObjectProperty and an ordered list of
    ObjectProperty local names whose composition entails the target::

        {"property": "hasAncestor",
         "chain": ["hasPredecessor", "hasPredecessor"]}

    Optionally includes ``declare_target`` block — when present and the target
    OP is missing, a skeleton declaration is injected (``a owl:ObjectProperty``
    + domain/range/label/comment/inverseOf). This lets the rules file carry
    the *intent* of a new derived property without requiring the LLM T-Box
    generator to emit it first.

    The axiom is emitted only when:
      - the target exists (or is declared via ``declare_target``);
      - every chain segment is already declared as ``owl:ObjectProperty``
        (otherwise the axiom would reference a non-existent resource and
        just produce noise);
      - the chain has length ≥ 2 (shorter chains are just subPropertyOf);
      - the target does not already carry a propertyChainAxiom (idempotent).

    Args:
        g: T-Box graph, mutated in place.
        chains: list of chain dicts, typically loaded from
            rules/domain/property_chains.json.

    Returns:
        Stats: chains_added, chains_skipped_missing_property,
               chains_skipped_missing_segment, chains_skipped_existing,
               targets_declared.
    """
    stats = {
        "chains_added": 0,
        "chains_skipped_missing_property": 0,
        "chains_skipped_missing_segment": 0,
        "chains_skipped_existing": 0,
        "targets_declared": 0,
    }
    for entry in chains or []:
        target = entry.get("property")
        segments = entry.get("chain") or []
        if not target or len(segments) < 2:
            continue
        target_uri = DOMAIN_NS_OBJ[target]
        # R25: target OP 가 없으면 declare_target 스펙으로 자동 선언
        if (target_uri, RDF.type, OWL.ObjectProperty) not in g:
            decl = entry.get("declare_target")
            if not decl:
                stats["chains_skipped_missing_property"] += 1
                continue
            # 기본 skeleton: a owl:ObjectProperty + domain + range + 라벨/주석
            g.add((target_uri, RDF.type, OWL.ObjectProperty))
            if decl.get("domain"):
                g.add((target_uri, RDFS.domain, DOMAIN_NS_OBJ[decl["domain"]]))
            if decl.get("range"):
                g.add((target_uri, RDFS.range, DOMAIN_NS_OBJ[decl["range"]]))
            if decl.get("label_en"):
                g.add((target_uri, RDFS.label, Literal(decl["label_en"], lang="en")))
            if decl.get("label_ko"):
                g.add((target_uri, RDFS.label, Literal(decl["label_ko"], lang="ko")))
            if decl.get("comment_ko"):
                g.add((target_uri, RDFS.comment, Literal(decl["comment_ko"], lang="ko")))
            if decl.get("inverse"):
                inv_name = decl["inverse"]
                inv_uri = DOMAIN_NS_OBJ[inv_name]
                # inverse 도 미선언이면 같이 선언
                if (inv_uri, RDF.type, OWL.ObjectProperty) not in g:
                    g.add((inv_uri, RDF.type, OWL.ObjectProperty))
                    # inverse 의 domain/range 는 target 의 것과 뒤집어 주입
                    if decl.get("range"):
                        g.add((inv_uri, RDFS.domain, DOMAIN_NS_OBJ[decl["range"]]))
                    if decl.get("domain"):
                        g.add((inv_uri, RDFS.range, DOMAIN_NS_OBJ[decl["domain"]]))
                    # R26: inverse OP 에도 SHACL MinCount(1) 충족을 위해 label 자동 생성
                    # (decl 에 inverse_label_* 이 명시되면 우선, 없으면 camelCase 분해한 기본값)
                    inv_lbl_en = decl.get(
                        "inverse_label_en",
                        re.sub(r"(?<!^)(?=[A-Z])", " ", inv_name).lower(),
                    )
                    inv_lbl_ko = decl.get(
                        "inverse_label_ko",
                        (decl.get("label_ko", "") + " (역)") if decl.get("label_ko") else inv_name,
                    )
                    inv_cmt_ko = decl.get(
                        "inverse_comment_ko",
                        (decl.get("comment_ko", "") + " (역방향)") if decl.get("comment_ko") else f"{inv_name} (역방향 관계)",
                    )
                    g.add((inv_uri, RDFS.label, Literal(inv_lbl_en, lang="en")))
                    g.add((inv_uri, RDFS.label, Literal(inv_lbl_ko, lang="ko")))
                    g.add((inv_uri, RDFS.comment, Literal(inv_cmt_ko, lang="ko")))
                g.add((target_uri, OWL.inverseOf, inv_uri))
                g.add((inv_uri, OWL.inverseOf, target_uri))
            stats["targets_declared"] += 1
        if list(g.objects(target_uri, OWL.propertyChainAxiom)):
            stats["chains_skipped_existing"] += 1
            continue
        resolved: list[URIRef] = []
        segment_missing = False
        for seg in segments:
            seg_uri = DOMAIN_NS_OBJ[seg]
            if (seg_uri, RDF.type, OWL.ObjectProperty) not in g:
                segment_missing = True
                break
            resolved.append(seg_uri)
        if segment_missing:
            stats["chains_skipped_missing_segment"] += 1
            continue
        list_node = BNode()
        rdflib.collection.Collection(g, list_node, resolved)
        g.add((target_uri, OWL.propertyChainAxiom, list_node))
        stats["chains_added"] += 1
    return stats


def _dp_domain_includes(g: Graph, dp: URIRef, cls_uri: URIRef) -> bool:
    """Return True iff dp's rdfs:domain covers cls_uri.

    Handles four cases:
    - No domain declared → accept (DP is polymorphic; downstream steps may set it).
    - Direct ``dp rdfs:domain cls_uri`` → accept.
    - ``dp rdfs:domain _:b ; _:b owl:unionOf ( ... cls_uri ... )`` → accept if
      cls_uri appears in the union list.
    - ``dp rdfs:domain D`` where D is an ancestor of cls_uri via subClassOf.
      Common with IOF upper-ontology parents (e.g. pointId's domain is
      iof-core:MeasurementProcess and MonitoringPointMaster subClassOf that).
      Restricted to one-step ancestors plus their own subClassOf closure so
      cycles can't blow up.
    """
    domains = list(g.objects(dp, RDFS.domain))
    if not domains:
        return True
    # Pre-compute ancestors of cls_uri (transitive subClassOf closure, bounded).
    ancestors: set = {cls_uri}
    stack = [cls_uri]
    seen: set = set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for parent in g.objects(node, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent not in ancestors:
                ancestors.add(parent)
                stack.append(parent)
    for d in domains:
        if d in ancestors:
            return True
        if isinstance(d, BNode):
            for union_list in g.objects(d, OWL.unionOf):
                try:
                    members = list(rdflib.collection.Collection(g, union_list))
                except Exception:  # noqa: BLE001 — 깨진 unionOf 목록은 일치하지 않는 것으로 본다
                    continue
                if cls_uri in members:
                    return True
    return False


def _inject_disjoint_union_of(g: Graph, groups: list[dict]) -> dict:
    """Promote selected disjoint groups to ``owl:disjointUnionOf`` axioms.

    A group configured with ``union_parent`` claims that its members together
    partition the parent: every instance of the parent is exactly one of the
    children. That is a strictly stronger statement than pairwise disjoint +
    subClassOf, so we only emit it when the T-Box already declares
    ``child rdfs:subClassOf parent`` for every child in the group. Otherwise
    the axiom would accidentally close the parent over classes not yet
    materialised, triggering spurious disjointness violations.

    Args:
        g: T-Box graph, mutated in place.
        groups: iterable of group dicts from rules/domain/disjoint_groups.json.
                Only groups with a ``union_parent`` key are considered.

    ## 구조가 아니라 **데이터 커버리지** 가 진짜 전제조건이다 (2026-08-30)

    위 구조 가드(모든 member 가 parent 의 직접 하위) 를 통과해도, member 의 정의
    공리가 **CSV 실값을 다 덮지 못하면** 파티션은 개체를 논리적으로 배제한다.
    실측 — 공리가 주장하는 값 vs CSV 실값:

    ========================= ============================ ==================
    parent                     공리 값                        CSV 에만 있는 값
    ========================= ============================ ==================
    ``RealTimeData``           ``GOOD``                     ``0`` 30,615 /
                                                            ``1`` 3,621 /
                                                            ``2`` 1,764
    ``InventoryTransaction``   ``IN`` / ``OUT``             ``Inbound`` 177 /
                                                            ``Outbound`` 181 /
                                                            ``Transfer`` 130 …
    ``EquipmentStatus``        Running/Stopped/Maintenance  ``Standby`` 1,227
    ``WasteManagement``        Landfill/Recycle             ``Incineration`` 33 …
    ``MaintenanceHistory``     Corrective/Preventive        ``Emergency`` 16 /
                                                            ``Predictive`` 31
    ``NDTResults``             Pass/Fail                    ``Conditional`` 9
    ``TagMaster``              ANALOG/DIGITAL               ``FLOAT`` 50
    ========================= ============================ ==================

    즉 LLM 이 **CSV 를 보지 않고 값 어휘를 발명했다** (``IN``/``OUT`` vs 실제
    ``Inbound``/``Outbound``, ``GOOD`` vs 실제 ``0``/``1``/``2``). 정의 공리의 DP 가
    ``owl:FunctionalProperty`` 이므로 미포함 값을 가진 개체는 OWL DL 상 모순이고,
    inconsistent 온톨로지에서는 **어떤 entailment 도 신뢰할 수 없다**.

    합계 38,051 개체가 배제된다. 그런데 배포 상태(명명 Restriction)에서는 HermiT 이
    이것을 보지 못해 ``consistent: true`` 를 반환했다 —
    ``check_reasoner_blindness`` 가 실증하는 눈멂이다.

    **공리 값을 CSV 에 맞춰 고치는 것은 지양한다** — 그것은 SME 판단이고, 값 하나를
    추가해도 다음 CSV 갱신에서 또 어긋난다. 대신 커버리지가 불완전하면 파티션을
    **만들지 않는다** (parent 는 open-world 로 남는다). 그 결론은 이미 SME 가
    도달한 것이기도 하다 (``t_box_rejected_20260827034233.ttl``: "현장 데이터에
    표준 외 예외값이 존재하므로 부모 클래스를 open-world 로 유지").

    Returns:
        Stats with counters: disjoint_unions_added,
        disjoint_unions_skipped_incomplete, disjoint_unions_skipped_existing,
        disjoint_unions_skipped_missing_parent,
        disjoint_unions_skipped_value_gap.
    """
    stats = {
        "disjoint_unions_added": 0,
        "disjoint_unions_skipped_incomplete": 0,
        "disjoint_unions_skipped_existing": 0,
        "disjoint_unions_skipped_missing_parent": 0,
        "disjoint_unions_skipped_value_gap": 0,
        "disjoint_union_value_gaps": [],
    }
    for group in groups or []:
        parent_local = group.get("union_parent")
        if not parent_local:
            continue
        members = group.get("classes") or []
        if len(members) < 2:
            continue
        parent_uri = DOMAIN_NS_OBJ[parent_local]
        # Parent must be declared as a class in the current graph.
        if (parent_uri, RDF.type, OWL.Class) not in g:
            stats["disjoint_unions_skipped_missing_parent"] += 1
            continue
        # Idempotent: skip if parent already has a disjointUnionOf list.
        if list(g.objects(parent_uri, OWL.disjointUnionOf)):
            stats["disjoint_unions_skipped_existing"] += 1
            continue
        # Every member must exist AND be a direct subClassOf the parent.
        resolved: list[URIRef] = []
        complete = True
        for cls_name in members:
            child_uri = DOMAIN_NS_OBJ[cls_name]
            if (child_uri, RDF.type, OWL.Class) not in g:
                complete = False
                break
            if (child_uri, RDFS.subClassOf, parent_uri) not in g:
                complete = False
                break
            resolved.append(child_uri)
        if not complete or len(resolved) < 2:
            stats["disjoint_unions_skipped_incomplete"] += 1
            continue
        # 값 커버리지 전제조건 — member 정의 공리가 CSV 실값을 다 덮는가.
        gap = _partition_value_gap(g, parent_local, list(resolved))
        if gap:
            stats["disjoint_unions_skipped_value_gap"] += 1
            stats["disjoint_union_value_gaps"].append(gap)
            logger.warning(
                "Step 28: %s 파티션 생성 보류 — 정의 공리가 CSV 값 %s 를 덮지 "
                "못한다 (개체 %d건). 파티션을 만들면 그 개체들이 논리적으로 "
                "배제되고 온톨로지가 inconsistent 가 되어 추론 결과 전체를 "
                "신뢰할 수 없다. 값 어휘를 SME 가 확정해야 한다.",
                parent_local, gap["uncovered_values"], gap["uncovered_rows"],
            )
            continue
        # Emit parent owl:disjointUnionOf ( c1 c2 ... ).
        list_node = BNode()
        rdflib.collection.Collection(g, list_node, resolved)
        g.add((parent_uri, OWL.disjointUnionOf, list_node))
        stats["disjoint_unions_added"] += 1
    return stats


def _partition_value_gap(
    g: Graph, parent_local: str, members: list,
) -> dict | None:
    """파티션 member 의 정의 공리가 CSV 실값을 다 덮는가 → 갭 정보 또는 None.

    판정 경로: member 의 ``owl:equivalentClass`` → ``owl:Restriction`` →
    (``owl:onProperty`` DP, ``owl:hasValue`` 값) 를 모아 "공리가 인정하는 값 집합" 을
    만들고, 그 DP 의 ``dcterms:source`` 컬럼에서 CSV 실값과 비교한다.

    ``None`` 을 반환하는 경우 (= 파티션 허용):

    * member 에 정의 공리가 없다 — 추론이 값으로 분류하지 않으므로 모순이 생기지
      않는다 (실측: ``ManufacturingProcessStep`` 이 그 경우이고 안전하다).
    * DP 의 출처 컬럼이나 CSV 를 찾을 수 없다 — **판정 불가와 갭 없음을 혼동하면
      정당한 파티션을 막는다**. 근거가 없으면 허용한다.
    * 모든 CSV 값이 공리 값에 포함된다 (실측: ``GHGEmission`` /
      ``NoiseVibrationMonitoring`` 이 그 경우다).
    """
    import csv as _csv
    import os as _os

    from rdflib.namespace import DCTERMS as _DCTERMS

    claimed: set[str] = set()
    on_props: set[str] = set()
    for member in members:
        for eq in g.objects(member, OWL.equivalentClass):
            if (eq, RDF.type, OWL.Restriction) not in g:
                continue
            for prop in g.objects(eq, OWL.onProperty):
                on_props.add(str(prop))
            for value in g.objects(eq, OWL.hasValue):
                claimed.add(str(value))
    if not claimed or len(on_props) != 1:
        # 정의 공리가 없거나 여러 DP 에 걸쳐 있으면 판정하지 않는다.
        return None

    prop_uri = URIRef(next(iter(on_props)))
    columns = [
        str(s).strip() for s in g.objects(prop_uri, _DCTERMS.source)
        if str(s).strip()
    ]
    if not columns:
        return None

    try:
        from domain.table_mapping import load_table_class_mapping
        table_class = load_table_class_mapping()
    except Exception:  # noqa: BLE001
        return None
    table = next(
        (t for t, cls in (table_class or {}).items() if cls == parent_local), None,
    )
    if not table:
        return None

    csv_path = _os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
    if not _os.path.exists(csv_path):
        return None
    try:
        with open(csv_path, encoding="utf-8-sig", newline="") as handle:
            rows = list(_csv.DictReader(handle))
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    header = {k.lower(): k for k in rows[0] if k}
    column = header.get(columns[0].lower())
    if not column:
        return None

    uncovered: dict[str, int] = {}
    for row in rows:
        value = str(row.get(column, "")).strip()
        if not value or value in claimed:
            continue
        uncovered[value] = uncovered.get(value, 0) + 1
    if not uncovered:
        return None
    return {
        "parent": parent_local,
        "on_property": str(prop_uri).rsplit("#", 1)[-1],
        "csv_column": column,
        "claimed_values": sorted(claimed),
        "uncovered_values": sorted(uncovered),
        "uncovered_rows": sum(uncovered.values()),
    }


def _inject_owl_haskey(
    g: Graph,
    csv_pk_map: dict[str, set[str]] | None = None,
) -> dict:
    """Promote CSV-detected primary keys into ``owl:hasKey`` axioms.

    For each steel: Class in g whose name matches a CSV table, look up the
    table's primary-key column(s) and — if every PK column is declared as a
    steel: DatatypeProperty in g — emit::

        cls owl:hasKey ( dp1 dp2 ... ) .

    Composite PKs become multi-property keys; single-column PKs become
    single-element lists. The step is idempotent: a class that already carries
    an ``owl:hasKey`` triple is skipped.

    When csv_pk_map is None, the real CSV directory is scanned via
    ``_build_csv_pk_map``. Passing an explicit map keeps unit tests hermetic.

    Args:
        g: the T-Box graph, mutated in place.
        csv_pk_map: optional {class_name_lower: {pk_col_lower_no_underscore, ...}}
            override. When None, derived from CSVs on disk.

    Returns:
        Stats dict with counters: haskey_added, haskey_skipped_existing,
        haskey_skipped_missing_dp, haskey_skipped_no_pk.
    """
    stats = {
        "haskey_added": 0,
        "haskey_skipped_existing": 0,
        "haskey_skipped_missing_dp": 0,
        "haskey_skipped_no_pk": 0,
    }
    pk_map = csv_pk_map if csv_pk_map is not None else _build_csv_pk_map()
    if not pk_map:
        return stats

    steel_str = str(DOMAIN_NS)
    # Build {class_name_lower_nounderscore: (URIRef, original_local_name)}.
    class_index: dict[str, tuple[URIRef, str]] = {}
    for cls in g.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef) or not str(cls).startswith(steel_str):
            continue
        local = str(cls).split("#")[-1]
        key = local.replace("_", "").lower()
        class_index.setdefault(key, (cls, local))

    # Build {dp_local_name_lower_nounderscore: URIRef} restricted to properties
    # whose rdfs:domain includes the target class. A DP with no domain defaults
    # to usable on any class since downstream steps may still fill domain in.
    dp_by_name: dict[str, list[URIRef]] = {}
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not isinstance(dp, URIRef) or not str(dp).startswith(steel_str):
            continue
        local = str(dp).split("#")[-1]
        dp_by_name.setdefault(local.replace("_", "").lower(), []).append(dp)

    def _resolve_dp(col: str, cls_uri: URIRef, class_local: str) -> URIRef | None:
        """Find a DP matching col on cls_uri, with three fallback patterns.

        LLM-generated T-Boxes commonly rename shared columns to distinguish
        per-class ownership. This resolver tries:

          1. Exact column name (``timestamp`` → ``steel:timestamp``).
          2. Prefix pattern ``{classTokenN}{Col}``, sweeping every class
             token (``EnergyEfficiency`` → tries both ``energyTimestamp`` and
             ``efficiencyTimestamp``).
          3. Suffix pattern ``{col}For{ClassTokenGroup}``, sweeping both
             individual tokens and adjacent pairs (``ProcessContinuousCasting``
             → tries ``productIdForCasting`` and ``productIdForContinuousCasting``).

        Each candidate still has to pass ``_dp_domain_includes`` so a DP
        pointing at a sibling class is rejected. Acronym-style class names
        (``NDTResults``) are tokenised with a second regex so the whole
        acronym ``NDT`` becomes one token instead of three.
        """
        # 1. Exact name match with domain compatibility.
        for dp in dp_by_name.get(col, []):
            if _dp_domain_includes(g, dp, cls_uri):
                return dp
        # Acronym-aware tokeniser: groups consecutive capitals (NDT) together,
        # then CamelCase words. "NDTResults" → ["NDT", "Results"].
        # "EnergyEfficiency" → ["Energy", "Efficiency"].
        tokens = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]*|[A-Z]+", class_local) or [class_local]
        col_capitalised = col[:1].upper() + col[1:]
        # Drop common "table-ish" tail tokens so the suffix pattern targets the
        # semantic core (ChemicalAnalysis → {sampleIdForChemical}, not
        # {sampleIdForChemicalAnalysis}).
        _SUFFIX_DROP = {
            "Master", "History", "Data", "Results", "Events", "Quality",
            "Monitoring", "Management", "Analysis", "Properties",
            "Transaction", "Status", "Plan", "Result", "Point",
        }
        # Strip generic category prefixes so classes like ProcessBlastFurnace
        # contribute "BlastFurnace" (matches LLM naming).
        _PREFIX_DROP = {"Process"}
        core_tokens = [
            t for t in tokens
            if t not in _SUFFIX_DROP and t not in _PREFIX_DROP
        ] or tokens
        # Build prefix candidates. We sweep EVERY core token (not just the
        # first) because LLM-generated DPs pick whichever class word makes
        # the most semantic sense, not always the leading one. Also try
        # adjacent pairs — ProcessBlastFurnace + timestamp can become
        # ``blastFurnaceTimestamp``.
        fuzzy_candidates: list[str] = []
        # R1A class-specific DP pattern: ``{classCamelLower}{Suffix}``.
        # CSV PK columns are normalised by ``_build_csv_pk_map`` to
        # ``{classlower}id`` (e.g. ``equipmentid``). When the col already
        # carries the class prefix, peel it off so the suffix (``Id``) can
        # be re-attached to the canonical class-camel form.
        full_lower = class_local[:1].lower() + class_local[1:]
        col_lower = col.lower()
        cls_token_candidates = {class_local.lower()}
        cls_token_candidates.update(t.lower() for t in tokens)
        cls_token_candidates.update(t.lower() for t in core_tokens)
        for cls_tok in cls_token_candidates:
            if not cls_tok or not col_lower.startswith(cls_tok):
                continue
            suffix = col_lower[len(cls_tok):]
            if not suffix:
                continue
            suffix_capitalised = suffix[:1].upper() + suffix[1:]
            # Try ``{full_lower}{Suffix}`` — e.g. equipmentMasterId.
            fuzzy_candidates.append(full_lower + suffix_capitalised)
            # Also try ``{cls_tok}{Suffix}`` covered by core-token loop below;
            # adding here keeps full-class-name first.
        # Always try ``{full_lower}{ColCapitalised}`` as fallback for cols
        # that don't share a class prefix (e.g. ``timestamp`` → equipmentMasterTimestamp).
        fuzzy_candidates.append(full_lower + col_capitalised)
        for source in (core_tokens, tokens):
            for i, tok in enumerate(source):
                fuzzy_candidates.append(tok.lower() + col_capitalised)
                if i + 1 < len(source):
                    fuzzy_candidates.append(
                        tok[:1].lower() + tok[1:] + source[i + 1]
                        + col_capitalised,
                    )
        # Suffix candidates: {col}For{ClassWord(s)}. Single tokens + adjacent
        # pairs, swept across both the core and raw token lists.
        suffix_bases: list[str] = []
        for source in (core_tokens, tokens):
            for i in range(len(source)):
                suffix_bases.append(source[i])
                if i + 1 < len(source):
                    suffix_bases.append(source[i] + source[i + 1])
        # Deduplicate (ordered).
        seen_b: set = set()
        suffix_bases = [
            b for b in suffix_bases if not (b in seen_b or seen_b.add(b))
        ]
        for base in suffix_bases:
            fuzzy_candidates.append(f"{col}For{base}")
        # Deduplicate candidates while keeping order.
        seen_c: set = set()
        fuzzy_candidates = [
            c for c in fuzzy_candidates if not (c in seen_c or seen_c.add(c))
        ]
        for cand in fuzzy_candidates:
            key = cand.replace("_", "").lower()
            for dp in dp_by_name.get(key, []):
                if _dp_domain_includes(g, dp, cls_uri):
                    return dp
        return None

    for class_key, pk_cols in pk_map.items():
        entry = class_index.get(class_key)
        if entry is None:
            continue
        cls_uri, class_local = entry
        # Idempotency: skip if the class already has hasKey.
        if list(g.objects(cls_uri, OWL.hasKey)):
            stats["haskey_skipped_existing"] += 1
            continue
        if not pk_cols:
            stats["haskey_skipped_no_pk"] += 1
            continue
        # Resolve every PK column to a DP URI whose domain is compatible.
        resolved: list[URIRef] = []
        missing = False
        for col in pk_cols:
            chosen = _resolve_dp(col, cls_uri, class_local)
            if chosen is None:
                missing = True
                break
            resolved.append(chosen)
        if missing or not resolved:
            stats["haskey_skipped_missing_dp"] += 1
            continue
        # Emit owl:hasKey ( dp1 dp2 ... ).
        key_list = BNode()
        rdflib.collection.Collection(g, key_list, resolved)
        g.add((cls_uri, OWL.hasKey, key_list))
        stats["haskey_added"] += 1
    return stats


def _dp_matches_domain_pk(
    dp_local_name: str,
    domain_uri: URIRef,
    csv_pk_map: dict[str, set[str]] | None = None,
) -> bool:
    """Return True iff dp_local_name looks like the PK of domain_uri.

    A PK names (part of) its owning class, never the other way round.  So we
    only promote a DP to Functional when its root (local-name minus trailing
    Id/ID/Code) is contained in the class name.  Accepting the reverse
    direction mis-tagged compound-FK columns like tagEquipmentId on TagMaster
    or steamEquipmentId on SteamEnergy.

    When csv_pk_map is provided, a DP whose full local-name matches the
    table's real PK column also qualifies — this catches synonyms (meterId vs
    ElectricalConsumption) and abbreviations (poId vs PurchaseOrder) that
    substring matching cannot reach.  Tables with composite PKs contribute
    every component, but this function still rejects them because the composite
    case is decided in the call site (length of the PK set).

    Accepts:
    - supplierId on SupplierMaster  (supplier ⊂ suppliermaster)
    - eventId   on AlarmEvents      (event    ⊂ alarmevents)
    - causeCode on FailureCause     (cause    ⊂ failurecause)
    - meterId   on ElectricalConsumption — via csv_pk_map
    - poId      on PurchaseOrder          — via csv_pk_map
    - mappingId on ItemSupplierMap        — via csv_pk_map

    Rejects:
    - managerId         on WarehouseMaster      (manager ⊄ warehousemaster)
    - zoneCode / warehouseCode on InventoryStatus (composite-PK components)
    - transportItemCode on Transportation        (transportitem ⊄ transportation)
    - tagEquipmentId    on TagMaster             (tagequipment ⊄ tagmaster)
    """
    class_name = str(domain_uri).split("#")[-1]
    class_full = class_name.lower()
    class_root = _CLASS_TAIL_RE.sub("", class_name).lower() or class_full
    # CSV-pk path: authoritative, bypasses short-root guard (e.g. poId).
    if csv_pk_map:
        pk_cols = csv_pk_map.get(class_full)
        if pk_cols and len(pk_cols) == 1:
            dp_name_norm = dp_local_name.replace("_", "").lower()
            if dp_name_norm in pk_cols:
                return True
    dp_root = _PK_SUFFIX_RE.sub("", dp_local_name).lower()
    if len(dp_root) < 3:
        return False
    if dp_root in (class_full, class_root):
        return True
    return dp_root in class_full or dp_root in class_root


def improve_tbox(ttl_content: str) -> tuple:
    """T-Box TTL 문자열을 받아 품질 개선 후 반환.

    주요 후처리 단계:
     1. AllDisjointClasses 완전화 (도메인별 그룹)
     2. InverseOf 양방향 선언 보장
     3. ObjectProperty 누락 domain 보완
     7. 온톨로지 메타데이터 보강 (owl:imports, versionIRI, dcterms)
     8. xsd:date → xsd:dateTime 변환 (HermiT OWL 2 호환)
    13. PK FunctionalProperty + OP someValuesFrom 제약
    14. 2차 계층 서브그룹 생성
    15. 크로스 도메인 ObjectProperty 추가
    17. Label synthesis
    18. Completeness Annotation
    19. BNode skolemization
    22. Duplicate OP consolidation
    24. Abstract category annotation
    25. OntoClean meta-annotation
    26. Module membership annotation
    27. owl:hasKey injection (R13)
    28. owl:disjointUnionOf promotion (R13)

    Args:
        ttl_content: 개선할 T-Box TTL 문자열.

    Returns:
        (improved_ttl: str, stats: dict)
    """
    g = _new_graph()
    g.parse(data=ttl_content, format="turtle")
    # 단일 레지스트리로 바인딩한다. 예전엔 여기서 4개를 손으로 바인딩해
    # iof-scro / skos / prov 가 빠졌고, prefix 를 그래프 바인딩으로 판정하는
    # 스텝(step_00 유령 복구 등)이 그 prefix 를 "모르는 이름" 으로 취급했다.
    from domain.namespaces import bind_namespaces as _bind_ns
    _bind_ns(g)
    stats: dict = {}
    change_log: list[dict] = []

    from tools.quality_steps import (
        _MAIN_POST_STEP9,
        _MAIN_PRE_STEP9,
        _POST_STEPS,
        _PRE_STEPS,
        _STEP9_GROUP,
        StepContext,
        run_step_pipeline,
        run_step_pipeline_grouped,
    )

    ctx = StepContext(domain_ns=DOMAIN_NS, change_log=change_log)

    # ── Phase 8 list-driven pipeline ──
    # _PRE_STEPS: graph 로드 직후 (현재 비어있음, Phase 9+ 확장 hook).
    # _MAIN_PRE_STEP9: step 0~8.
    # _STEP9_GROUP: step 9~9f — 묶음 change_log entry "9 / domain_range_completion".
    # _MAIN_POST_STEP9: step 10~16.
    # _POST_STEPS: step 17~29 (Phase 2 부터 등록됨).
    run_step_pipeline(_PRE_STEPS, g, ctx, stats, change_log)
    run_step_pipeline(_MAIN_PRE_STEP9, g, ctx, stats, change_log)
    run_step_pipeline_grouped(
        _STEP9_GROUP, g, ctx, stats, change_log,
        group_step=9, group_label="domain_range_completion",
    )
    run_step_pipeline(_MAIN_POST_STEP9, g, ctx, stats, change_log)
    run_step_pipeline(_POST_STEPS, g, ctx, stats, change_log)

    stats["change_log"] = change_log

    # ── 직렬화 ──
    result_ttl = g.serialize(format="turtle")
    # rdflib 직렬화에서 dc1: 등 비표준 prefix 보정
    result_ttl = result_ttl.replace("@prefix dc1:", "@prefix dcterms:")
    result_ttl = result_ttl.replace("dc1:", "dcterms:")

    stats["total_triples"] = len(g)
    return result_ttl, stats


# ── MCP 도구 등록 ────────────────────────────────


def improve_tbox_quality(ttl_content: str = "") -> str:
    """T-Box TTL의 품질을 개선한다. (AllDisjointClasses, inverseOf 양방향, 메타데이터 보강, 공유 FK domain 확장)

    후처리 단계:
    1. AllDisjointClasses 완전화 (도메인별 그룹)
    2. InverseOf 양방향 선언 보장
    3. ObjectProperty 누락 domain 보완
    4. 중복 한국어 Comment 통합 (가장 긴 것 유지)
    5. Comment 내 코드 패턴 정리
    6. ManufacturingProcessStep unionOf -> equivalentClass 변환
    7. 온톨로지 메타데이터 보강 (owl:imports, versionIRI, dcterms)
    8. xsd:date → xsd:dateTime 변환 (HermiT OWL 2 호환)
    9. 공유 프로퍼티 domain/range 보완 (owl:Thing, phantom class 수정)
    10. 공유 FK ObjectProperty domain 확장 (CSV FK 컬럼 스캔)
    11. 암묵지 클래스 T-Box 보충 (QualitySpecification, FailurePattern)
    12. 도메인별 중간 추상 클래스 생성 (DIT/NOC 개선, design_patterns.json 참조)
    13. PK FunctionalProperty + OP someValuesFrom 제약 추가
    14. 2차 계층 서브그룹 생성 (NOC/DIT 추가 최적화)
    15. 크로스 도메인 ObjectProperty 추가 (RR 개선, design_patterns.json 참조)
    16. Scope Emission hasValue Restriction 추가
    17. Completeness Annotation (CWA/OWA 브릿지)

    Args:
        ttl_content: 개선할 T-Box TTL. 비어있으면 로컬 파일(TBOX_PATH)에서 현재 T-Box를 읽어 개선.
    """
    try:
        # ttl_content가 비어있으면 로컬 파일에서 읽기
        if not ttl_content.strip():
            try:
                with open(TBOX_PATH, encoding="utf-8") as f:
                    ttl_content = f.read()
            except FileNotFoundError:
                return json.dumps({
                    "success": False,
                    "error": f"T-Box 파일을 찾을 수 없습니다: {TBOX_PATH}",
                    "hint": "ttl_content를 직접 전달하거나 T-Box 파일 경로를 확인하세요.",
                }, ensure_ascii=False, indent=2)
            except Exception as e:
                return json.dumps({
                    "success": False,
                    "error": f"T-Box 읽기 실패: {e}",
                    "hint": "ttl_content를 직접 전달하거나 T-Box 파일 경로를 확인하세요.",
                }, ensure_ascii=False, indent=2)

        improved_ttl, stats = improve_tbox(ttl_content)

        # 개선 후 자동 재검증 — 새로운 위반이 없는지 확인
        post_validation = {}
        try:
            from tools.validation_core import check_quality_rules
            post_result = json.loads(check_quality_rules(improved_ttl))
            if post_result.get("success"):
                post_issues = post_result.get("issues", [])
                post_critical = [i for i in post_issues if i.get("severity") == "critical"]
                post_high = [i for i in post_issues if i.get("severity") == "high"]
                post_validation = {
                    "total_issues": len(post_issues),
                    "critical": len(post_critical),
                    "high": len(post_high),
                    "new_critical_issues": post_critical[:5],
                    "passed": len(post_critical) == 0,
                }
                if post_critical:
                    logger.warning("improve_tbox 후 CRITICAL 위반 %d건 발견: %s",
                                   len(post_critical),
                                   [i.get("message", "")[:80] for i in post_critical[:3]])
        except Exception as e:
            logger.warning("improve_tbox 후 재검증 실패: %s", e)
            post_validation = {"error": str(e)}

        atomic_write(TBOX_PATH, improved_ttl)

        return json.dumps({
            "success": True,
            "message": "T-Box 품질 개선 완료",
            "statistics": stats,
            "post_validation": post_validation,
            "saved_to": TBOX_PATH,
            "improved_ttl": improved_ttl,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


# ── Duque-Ramos 2013 OQuaRE (ISO/IEC 25000 기반) ───


# Standard vocabulary prefixes for Transferability scoring.
# Mirrors tools/foops_fair.py:_check_interoperable's standard_prefixes tuple
# so OQuaRE Transferability and FOOPS! I2 use consistent vocabularies.
_STANDARD_VOCAB_PREFIXES = (
    "http://purl.obolibrary.org/obo/",
    "http://xmlns.com/foaf/",
    "http://purl.org/dc/",
    "http://www.w3.org/ns/prov",
    "https://spec.industrialontologies.org/",
)


def _compute_transferability(g: Graph) -> float:
    """OQuaRE Transferability sub-score (1~5 scale) based on owl:imports.

    Duque-Ramos et al. (2013) §2.1.1 은 Transferability 를 온톨로지를 한
    환경에서 다른 환경으로 옮길 수 있는 정도로 정의하고 Portability 와
    Adaptability 를 하위 특성으로 든다. owl:imports 와 표준 vocabulary 재사용으로
    점수를 매기는 아래 선형식은 이 구현의 근사이며 상한은 5.0 이다:

        base = 1.0  # minimum floor
        + 0.5  if owl:Ontology IRI declared (portable identifier)
        + min(2.0, imports_count * 0.5)         # any reuse (max +2)
        + min(2.0, standard_vocab_count * 1.0)  # standard reuse (max +2)
        cap 5.0

    A graph with no ontology IRI → 1.0 (no owl:imports possible to attribute).
    """
    onto_iri = None
    for s in g.subjects(RDF.type, OWL.Ontology):
        onto_iri = s
        break

    if onto_iri is None:
        return 1.0

    score = 1.0 + 0.5  # base + ontology IRI bonus
    imports = list(g.objects(onto_iri, OWL.imports))
    imports_count = len(imports)
    standard_vocab_count = sum(
        1 for imp in imports
        if any(str(imp).startswith(p) for p in _STANDARD_VOCAB_PREFIXES)
    )

    score += min(2.0, imports_count * 0.5)
    score += min(2.0, standard_vocab_count * 1.0)
    return min(5.0, round(score, 2))


def _oquare_normalize(value: float, low: float, high: float) -> int:
    """OQuaRE 1-5 scale 정규화.

    SQuaRE 는 점수를 1(최저)~5(최고) 범위로 주고, metric 값을 이 범위로 옮기는
    방식은 적용하는 쪽이 정한다 (Duque-Ramos et al. 2013 §1). 호출측이 넘기는
    low/high 구간은 이 구현의 선택이다.

    value가 low..high 범위 밖이면 1(최저)/5(최고). 내부는 선형 매핑.
    값이 높을수록 좋은 metric 기준. 반대인 metric (예: 낮은 게 좋은 결합도)
    은 호출측에서 `high - value + low` 를 넘기면 됨.
    """
    if value <= low:
        return 1
    if value >= high:
        return 5
    return max(1, min(5, int(round(1 + (value - low) / (high - low) * 4))))


def _oquare_characteristics(metrics: dict, g: Graph | None = None) -> dict:
    """OQuaRE 7 characteristic 별 sub-metric 합산 (Duque-Ramos et al. 2013 §2.1).

    OQuaRE 는 SQuaRE (ISO/IEC 25000) characteristic 6개 (Reliability,
    Operability, Maintainability, Compatibility, Transferability, Functional
    adequacy) 를 온톨로지에 맞게 고치고, SQuaRE 에 없는 Structural 을 더했다.
    아래 sub-metric 배정은 이 구현이 Tartir OntoQA 메트릭으로 정한 것이다:
    - Structural       ← DIT, NOC, IR, TAN
    - Functional adequacy ← RR, AR, axiom_richness
    - Reliability      ← CC, cou (connectivity)
    - Operability      ← annotation completeness
    - Maintainability  ← axiom_richness, tan (multi-parent)
    - Compatibility    ← IR (inheritance richness)
    - Transferability  ← owl:imports + 표준 vocabulary 재사용률 (g 주입 시)

    g 가 None 이면 Transferability=3.0 (backward compat placeholder).
    g 가 제공되면 `_compute_transferability(g)` 로 동적 계산.

    각 characteristic 점수 = 해당 sub-metric 1-5 scale 평균.
    """
    # 정규화된 sub-metric 점수
    dit = metrics.get("dit", {}).get("value", 0)
    noc_avg = metrics.get("noc_avg", {}).get("value", 0)
    rr = metrics.get("rr", {}).get("value", 0)
    ar = metrics.get("ar", {}).get("value", 0)
    ann = metrics.get("annotation_completeness", {}).get("value", 0)
    axiom = metrics.get("axiom_richness", {}).get("value", 0)
    ir = metrics.get("ir", {}).get("value", 0)
    cc = metrics.get("cc", {}).get("value", 0)
    tan = metrics.get("tan", {}).get("value", 0)

    sub = {
        "dit": _oquare_normalize(dit, 1, 5),
        # noc_avg 는 낮아도 구조 정상이므로 target window 1~5 로 관대하게
        "noc_avg": _oquare_normalize(noc_avg, 0.5, 5),
        "rr": _oquare_normalize(rr, 0.1, 0.6),
        "ar": _oquare_normalize(ar, 2, 12),
        "annotation_completeness": _oquare_normalize(ann, 50, 100),
        "axiom_richness": _oquare_normalize(axiom, 1, 5),
        "ir": _oquare_normalize(ir * 100, 10, 60),  # % scale
        "cc": _oquare_normalize(cc * 100, 20, 80),
        # TAN 은 낮을수록 좋음 (20% 이하가 목표)
        "tan_inverted": 5 - _oquare_normalize(tan, 0, 40) + 1,
    }

    characteristics = {
        "Structural": round(sum(sub[k] for k in (
            "dit", "noc_avg", "ir", "tan_inverted",
        )) / 4, 2),
        "Functional_Adequacy": round(sum(sub[k] for k in (
            "rr", "ar", "axiom_richness",
        )) / 3, 2),
        "Reliability": round(sum(sub[k] for k in ("cc",)) / 1, 2),
        "Operability": round(sum(sub[k] for k in (
            "annotation_completeness",
        )) / 1, 2),
        "Maintainability": round(sum(sub[k] for k in (
            "axiom_richness", "tan_inverted",
        )) / 2, 2),
        "Compatibility": round(sum(sub[k] for k in ("ir",)) / 1, 2),
        # Transferability: g 주입 시 owl:imports 기반 동적 계산, 아니면 3.0
        # placeholder (backward compat).
        "Transferability": _compute_transferability(g) if g is not None else 3.0,
    }
    return characteristics, sub


def evaluate_oquare() -> str:
    """Duque-Ramos 2013 OQuaRE (ISO/IEC 25000 기반) 점수 평가.

    인용: Duque-Ramos, A., Fernández-Breis, J. T., Iniesta, M., Dumontier, M.,
    Egaña Aranguren, M., Schulz, S., Aussenac-Gilles, N., Stevens, R. (2013).
    "Evaluation of the OQuaRE framework for ontology quality." Expert Systems
    with Applications 40(7):2696-2703. DOI:10.1016/j.eswa.2012.11.004

    기존 Tartir OntoQA 메트릭(measure_tbox_metrics 결과)을 재활용해
    OQuaRE 7 characteristic 별로 1-5 scale 점수 산출 후
    동등 가중치 평균으로 overall 점수 계산.

    Tartir 메트릭 사용 가능해야 하므로 내부에서 measure_tbox_metrics 호출.

    Returns:
        {characteristics: {Structural, Functional_Adequacy, ...},
         sub_metrics: {...}, overall_1_to_5, overall_pct, grade}
    """
    try:
        from tools.tbox_metrics import measure_tbox_metrics
        raw = json.loads(measure_tbox_metrics())
        if not raw.get("success"):
            return error_response(
                "measure_tbox_metrics 실패 — OQuaRE 계산 불가",
                logger=logger,
            )
        metrics = raw.get("metrics", {})

        # Load T-Box graph for dynamic Transferability (owl:imports based).
        # If the T-Box file is unavailable, fall back to the placeholder by
        # passing g=None.
        g: Graph | None = None
        try:
            if os.path.exists(TBOX_PATH):
                g = Graph()
                g.parse(TBOX_PATH, format="turtle")
        except Exception as e:
            logger.warning(
                f"T-Box graph load failed for OQuaRE Transferability "
                f"(fallback to placeholder 3.0): {e}"
            )
            g = None

        characteristics, sub = _oquare_characteristics(metrics, g=g)
        # 동등 가중치는 이 구현의 기본값이다. Duque-Ramos et al. (2013) §4.4 는
        # subcharacteristic·metric 수와 가중치를 바꿔 새 평가 방법을 정의할 수
        # 있다고 적는다.
        overall = round(
            sum(characteristics.values()) / len(characteristics), 2,
        )
        pct = round((overall - 1) / 4 * 100, 1)

        grade = (
            "A" if overall >= 4.3
            else "B" if overall >= 3.5
            else "C" if overall >= 2.7
            else "D" if overall >= 1.8
            else "E"
        )

        return json.dumps({
            "success": True,
            "framework": "OQuaRE (ISO/IEC 25000)",
            "characteristics_1_to_5": characteristics,
            "sub_metrics_1_to_5": sub,
            "overall_1_to_5": overall,
            "overall_pct": pct,
            "grade": grade,
            "weights": "equal (default)",
            "citation": (
                "Duque-Ramos, A., Fernández-Breis, J. T., Iniesta, M., "
                "Dumontier, M., Egaña Aranguren, M., Schulz, S., "
                "Aussenac-Gilles, N., Stevens, R. (2013). Evaluation of the "
                "OQuaRE framework for ontology quality. Expert Systems with "
                "Applications 40(7):2696-2703. DOI:10.1016/j.eswa.2012.11.004"
            ),
            "notes": (
                "Tartir OntoQA metrics를 OQuaRE 7 characteristic"
                "(SQuaRE 6개 + Structural)으로 "
                "재집계. Transferability는 owl:imports + 표준 vocabulary "
                "(PROV/FOAF/DCTERMS/OBO/IOF) 재사용률로 1~5 scale 동적 계산."
            ),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)
