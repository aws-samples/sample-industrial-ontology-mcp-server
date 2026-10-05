"""rules/ 설정 파일 자동 생성 — CSV 분석 + Bedrock LLM으로 9개 파일 초기화."""

from __future__ import annotations

import csv
import glob
import json
import logging
import os
import re
import time

from config import SOURCE_RAWDATA_DIR
from domain.namespaces import IOF_CORE, IOF_MAINT
from domain.rules_paths import (
    CONTRACT_DIR,
    DOMAIN_DIR,
    POLICY_DIR,
    REPLACE_ON_NEW_DOMAIN,
    RULES_ROOT,
    category_of,
    rules_path,
)
from tools.common import atomic_write, error_response

logger = logging.getLogger(__name__)

_RULES_DIR = RULES_ROOT

# ── Phase 1 헬퍼: 프로그래매틱 생성 ─────────────────────────────────────────



def _scan_csv_tables() -> dict[str, list[str]]:
    """CSV 파일 스캔 → {table_name: [columns...]}."""
    tables: dict[str, list[str]] = {}
    for path in sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))):
        name = os.path.basename(path).replace(".csv", "")
        with open(path, encoding="utf-8") as f:
            header = next(csv.reader(f), None)
        if header:
            tables[name] = header
    return tables


def _to_pascal(name: str) -> str:
    """Table_Name → TableName."""
    return "".join(part.capitalize() for part in name.split("_"))


def _normalize_col(col: str) -> str:
    """Column_Name → columnname (소문자, 언더스코어 제거)."""
    return re.sub(r"[_\s]", "", col).lower()


def _gen_domain_config(
    domain_name: str,
    domain_name_ko: str,
    namespace_uri: str,
    prefix: str,
    industry: str,
    industry_ko: str,
) -> dict:
    ns = namespace_uri.rstrip("/").rstrip("#")
    return {
        "domain": {
            "name": domain_name,
            "name_ko": domain_name_ko,
            "industry": industry,
            "industry_ko": industry_ko,
            "description_en": f"IOF-aligned ontology for {industry}.",
            "description_ko": f"IOF 기반 {industry_ko} 온톨로지",
        },
        "namespace": {
            "ontology_uri": ns,
            "class_ns": f"{ns}#",
            "instance_ns": f"{ns}/instances#",
            "prefix": prefix,
            "instance_prefix": f"{prefix}-inst",
        },
        "metadata": {
            "version": "1.0.0",
            "creator": f"{domain_name} Ontology Team",
            # FAIR gate를 유지하는 MIT-0 기본값. 채택자는 자기 온톨로지의
            # 실제 라이선스 URI로 교체해야 한다.
            "license": "https://spdx.org/licenses/MIT-0.html",
        },
        "upper_ontology": {
            "framework": "IOF/BFO",
            "imports": [
                IOF_CORE,
                IOF_MAINT,
            ],
        },
        "sparql_variable_names": {
            "count": "?count",
            "class": "?class",
            "parent": "?parent",
        },
        "fk_fuzzy_match": {
            "normalized": True,
            "levenshtein": False,
            "prefix": False,
            "_comment": (
                "A-Box 생성 시 FK value 4단계 fallback 매칭. normalized (기본 ON) "
                "는 하이픈/언더스코어/대소문자 차이를 자동 흡수. levenshtein/prefix "
                "는 false positive 우려로 opt-in (generate_abox 응답의 fk_match_stats 참고)."
            ),
        },
    }


def _gen_tbox_generation_config() -> dict:
    return {
        "max_tokens": 24000,
        "max_retries": 2,
        "max_correction_rounds": 2,
        "per_column_tokens": 140,
        "per_table_tokens": 400,
        "token_budget_ratio": 0.5,
    }


def _gen_table_class_mapping(tables: dict[str, list[str]], prefix: str) -> dict:
    mapping = {}
    for table_name in tables:
        cls = _to_pascal(table_name)
        mapping[table_name] = f"{prefix}:{cls}"
    return {"table_class_mapping": mapping, "class_prop_mapping": {}}


def _gen_fk_patterns(tables: dict[str, list[str]]) -> dict:
    table_classes = {_normalize_col(t): _to_pascal(t) for t in tables}

    patterns: dict[str, str] = {}
    for table_name, columns in tables.items():
        for col in columns:
            norm = _normalize_col(col)
            if not (norm.endswith("id") or norm.endswith("code")):
                continue
            # 자기 테이블의 PK는 건너뛰기 (첫 번째 컬럼)
            if col == columns[0]:
                continue
            if norm in patterns:
                continue
            # 정확 매칭: equipmentid → 테이블 이름에서 equipment 포함하는 것 찾기
            base = re.sub(r"(id|code)$", "", norm)
            if not base:
                continue
            for tbl_norm, tbl_class in table_classes.items():
                if base in tbl_norm and "master" in tbl_norm:
                    patterns[norm] = tbl_class
                    break
            else:
                # master 없으면 base로 시작하는 테이블 (base 5자 이상만)
                if len(base) >= 5:
                    for tbl_norm, tbl_class in table_classes.items():
                        if tbl_norm.startswith(base) and _to_pascal(table_name) != tbl_class:
                            patterns[norm] = tbl_class
                            break

    suffix_rules = [
        {
            "suffix": "id",
            "template": "{base}Master",
            "_comment": "예: equipmentid → base='equipment' → EquipmentMaster",
        }
    ]
    return {"patterns": patterns, "suffix_rules": suffix_rules}


def _gen_value_ranges(tables: dict[str, list[str]]) -> dict:
    """수치 컬럼의 min/max를 프로파일링하고 단위 패턴별로 그룹핑."""
    # 단위 패턴 → (range_key, description, default_min, default_max)
    unit_patterns: list[tuple[str, str, str, float, float]] = [
        (r"temp.*c$|_c$", "temperatureC", "온도 (섭씨)", -50, 2500),
        (r"pressure.*kpa|_kpa$", "pressureKpa", "압력 (kPa)", 0, 1000),
        (r"pressure.*mpa|_mpa$", "pressureMpa", "압력 (MPa)", 0, 100),
        (r"percent$|_percent$|rate$", "percentValue", "비율/백분율 (%)", 0, 100),
        (r"flow.*m3|_m3h$|_m3min$", "flowRate", "유량 (m3)", 0, 100000),
        (r"weight.*ton|_ton$", "weightTon", "중량 (ton)", 0, 100000),
        (r"_mm$|thickness|width|length", "dimensionMm", "치수 (mm)", 0, 50000),
        (r"_kn$|force", "forceKn", "힘 (kN)", 0, 100000),
        (r"_kwh$|consumption", "energyKwh", "전력 (kWh)", 0, 1000000),
        (r"cost|price|amount", "monetaryValue", "금액", 0, 10000000),
        (r"_db$|level_db", "noiseDb", "소음 (dB)", 0, 200),
        (r"_hz$|frequency", "frequencyHz", "주파수 (Hz)", 0, 100000),
    ]

    ranges: dict[str, dict] = {}
    for _table_name, columns in tables.items():
        for col in columns:
            norm = _normalize_col(col)
            for pattern, key, desc, default_min, default_max in unit_patterns:
                if re.search(pattern, norm):
                    if key not in ranges:
                        ranges[key] = {
                            "min": default_min,
                            "max": default_max,
                            "description": desc,
                        }
                    break

    # 최소한 기본 범위는 포함
    if not ranges:
        ranges["genericNumeric"] = {
            "min": 0,
            "max": 1000000,
            "description": "기본 수치 범위",
        }

    return {
        "description": "도메인별 DatatypeProperty 값 범위 규칙",
        # 신규 도메인이 같은 함정에 빠지지 않게 규약을 파일에 적어 둔다. Path B
        # 정책상 실제 DP 는 class-prefixed 이므로 (``gasEnergyTemperatureC``) 아래
        # 키는 접미 매칭으로 해상된다. 단위가 갈리는 키는 이름으로 판별할 수 없어
        # ``applies_to`` 가 필요하다 — 실측: carbonContent[0,1] 규칙이 실제로는
        # 퍼센트 DP 를 가리켜, 명시 없이는 오탐이거나 미매칭이 된다.
        "_applies_to_note": (
            "키는 class prefix 를 뺀 이름으로 적는다 (temperatureC). 실제 "
            "class-specific DP 는 접미 매칭으로 해상된다. 단위가 갈리는 키 "
            "(percent vs ratio) 는 \"applies_to\": [\"실제DP이름\"] 으로 명시하라. "
            "어떤 규칙도 DP 에 닿지 못하면 validate_kg 의 value_ranges check 가 "
            "FAIL 로 보고한다 (꺼진 게이트를 드러내기 위해)."
        ),
        "ranges": ranges,
    }


# 도메인-중립 범용 DatatypeProperty 템플릿 (Path B: A-Box 에 직접 주입되지 않고
# class-specific DP 의 range/label/comment 추론용 템플릿 + CSV 컬럼 인식용 alias).
_COMMON_DATATYPE_PROPERTIES: list[dict] = [
    {"name": "hasTimestamp", "aliases": ["timestamp", "datetime", "eventTime", "eventDatetime", "recordTimestamp"], "range": "http://www.w3.org/2001/XMLSchema#dateTime", "label_en": "has timestamp", "label_ko": "시각", "comment_en": "Generic timestamp property for measurements, events, and transactions.", "comment_ko": "측정·이벤트·거래의 발생 시각을 나타내는 범용 DP."},
    {"name": "hasValue", "aliases": ["value", "measurementValue", "readingValue"], "range": "http://www.w3.org/2001/XMLSchema#decimal", "label_en": "has value", "label_ko": "값", "comment_en": "Generic numeric measurement value.", "comment_ko": "측정값을 나타내는 범용 DP."},
    {"name": "hasUnit", "aliases": ["unit", "uom", "unitOfMeasure"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has unit", "label_ko": "단위", "comment_en": "Generic measurement unit as a code or symbol.", "comment_ko": "측정 단위를 문자열 코드로 나타낸다."},
    {"name": "hasStatus", "aliases": ["status", "equipmentStatus", "planStatus", "resultStatus", "orderStatus", "poStatus"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has status", "label_ko": "상태", "comment_en": "Current status string.", "comment_ko": "현재 상태 문자열."},
    {"name": "hasLocation", "aliases": ["location", "place"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has location", "label_ko": "위치", "comment_en": "Location descriptor (string). Prefer geo: for coordinates.", "comment_ko": "위치 설명 문자열. 좌표는 geo: 스킴 권장."},
    {"name": "hasQuantity", "aliases": ["quantity", "qty", "count"], "range": "http://www.w3.org/2001/XMLSchema#decimal", "label_en": "has quantity", "label_ko": "수량", "comment_en": "Quantity as a decimal number.", "comment_ko": "수량을 10진 숫자로 표현."},
    {"name": "hasUnitPrice", "aliases": ["unitPrice", "unit_price", "pricePerUnit"], "range": "http://www.w3.org/2001/XMLSchema#decimal", "label_en": "has unit price", "label_ko": "단가", "comment_en": "Unit price as a decimal number.", "comment_ko": "단가를 10진 숫자로 표현."},
    {"name": "hasProcessType", "aliases": ["processType", "process_type"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has process type", "label_ko": "공정 유형", "comment_en": "Process type/category identifier.", "comment_ko": "공정의 유형/범주 식별자."},
    {"name": "hasSourceType", "aliases": ["sourceType", "source_type"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has source type", "label_ko": "원천 유형", "comment_en": "Type identifier of the source (emission source, data source, etc).", "comment_ko": "원천의 유형 식별자."},
    {"name": "hasResult", "aliases": ["result"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has result", "label_ko": "결과", "comment_en": "Test or inspection result label (e.g. Pass/Fail).", "comment_ko": "시험 또는 검사 결과 라벨."},
    {"name": "hasSeverity", "aliases": ["severity"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has severity", "label_ko": "심각도", "comment_en": "Severity level string (e.g. Critical/Major/Minor).", "comment_ko": "심각도 레벨 문자열."},
    {"name": "hasPriority", "aliases": ["priority"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has priority", "label_ko": "우선순위", "comment_en": "Priority level.", "comment_ko": "우선순위 레벨."},
    {"name": "hasIdentifier", "aliases": ["id", "identifier", "uuid", "code"], "range": "http://www.w3.org/2001/XMLSchema#string", "label_en": "has identifier", "label_ko": "식별자", "comment_en": "Primary identifier string. Typically the same value used in the instance URI's local name.", "comment_ko": "주 식별자 문자열. 일반적으로 인스턴스 URI local name 과 동일한 값."},
]


def _gen_value_heuristics() -> dict:
    """A-Box 생성 시 CSV 값/컬럼 타입 추정용 휴리스틱 (도메인-중립).

    numeric/timestamp/null 키워드는 산업 무관 보편값이라 그대로 재사용 가능.
    filename suffix 는 *_Master / *_History 류 관례 — 안 맞아도 무해(미사용 폴백).
    도메인 특화가 필요하면 이 파일만 교체.
    """
    return {
        "_comment": "A-Box 생성 시 CSV 값/컬럼 타입 추정용 휴리스틱 (도메인-중립). 도메인 스위치 시 이 파일만 교체하면 된다.",
        "numeric_keywords": [
            "amount", "rate", "temp", "pressure", "flow", "speed",
            "force", "level", "percent", "mpa", "kpa", "kwh", "kw",
            "hours", "count", "stock", "quantity", "cost", "price",
            "score", "factor", "value", "input", "output", "mm", "kg",
            "ton", "kn", "hz", "db", "efficiency", "weight", "height",
            "width", "length", "thickness", "roughness", "strength",
            "temperature", "concentration", "capacity", "power",
            "voltage", "current", "min", "max", "avg", "total",
            "wth", "lth", "thk", "wgt", "tmp", "len", "qty",
        ],
        "timestamp_columns": [
            "timestamp", "datetime", "measurementdatetime", "testdatetime",
            "occurrencedatetime", "departuretime", "generationdate",
            "maintenancedate", "orderdate", "plandate", "creationtimestamp",
            "lastupdatetimestamp", "ddlinedate",
        ],
        "pk_class_suffixes": [
            "master", "map", "history", "results", "events", "data",
            "status", "quality", "plan", "result", "monitoring",
            "management", "transaction",
        ],
        # ``_Records`` / ``_Log(s)`` 는 비제조 도메인에서 가장 흔한 트랜잭션
        # 접미다 (병원 Admission_Records, 금융 Transaction_Logs). 목록에 없으면
        # 파일명 판정을 통과하지 못하고 행수·타임스탬프 휴리스틱으로 떨어져
        # 검증 티어가 잘못 배정된다 (실측 2026-08-23).
        "transaction_filename_suffixes": [
            "_History", "_Transaction", "_Plan", "_Status",
            "_Results", "_Events", "_Data", "_Monitoring",
            "_Records", "_Record", "_Log", "_Logs", "_Entries",
        ],
        # ``_Code(s)`` / ``_Type(s)`` / ``_Dim`` 은 코드·차원 테이블로, 행이 적고
        # 갱신되지 않는 master 성격이다.
        "master_filename_suffixes": [
            "_Master", "_Map", "_Codes", "_Code", "_Types", "_Dim", "_Dimension",
        ],
        "null_sentinels": [
            "n/a", "na", "null", "-", "none", "#n/a", "nan", "undefined",
            "미정", "해당없음",
        ],
    }


def _gen_common_dp() -> dict:
    """class-specific DP 의 range/label 추론 템플릿 + audit canonical 이름.

    common_datatype_properties 는 도메인-중립 범용 템플릿 (재사용).
    required_props_by_class 는 클래스별 필수 DP audit 대상 — 도메인마다 다르므로
    빈 템플릿으로 생성하고 SME 가 채운다 (예: "ProcessStep": ["hasTimestamp"]).
    """
    return {
        "_comment": "Path B (class-specific DP) 정책. common_datatype_properties = on-demand 주입되는 class-specific DP 의 range/label/comment 추론 템플릿 + CSV 컬럼 인식용 alias. A-Box 에 generic DP 이름은 주입되지 않는다. required_props_by_class = 클래스별 필수 DP coverage audit 대상 (canonical 이름; class-prefix 해상도로 class-specific DP 와 매칭). 도메인별로 SME 가 채운다.",
        "common_datatype_properties": _COMMON_DATATYPE_PROPERTIES,
        "required_props_by_class": {
            "_comment": "클래스명(또는 *wildcard) → 필수 DP canonical 이름 목록. 예: {\"*Monitoring\": [\"hasTimestamp\", \"hasValue\"]}. 비어 있으면 coverage audit 이 skip 된다.",
        },
    }


def _gen_value_normalizations() -> dict:
    """CSV 값 전처리 규칙 (A-Box Literal 변환 직전 적용).

    global_preprocess (trim/whitespace/BOM) 는 보편값이라 항상 켠다.
    enum_synonyms 는 도메인-특화 (status/severity 등 enum 동의어 통합) 이므로
    빈 dict 로 생성하고 SME 가 채운다. abox_generation 이 required=True 로 읽지만
    미존재 시 {} 폴백 → 이 파일을 생성해 명시적 계약을 만든다.
    """
    return {
        "_comment": "CSV 값 전처리 규칙. A-Box 생성 시 Literal 변환 직전 적용. 같은 의미의 값이 표기 차이로 충돌하는 것을 방지. enum_synonyms 는 도메인별로 SME 가 채운다.",
        "global_preprocess": {
            "trim": True,
            "collapse_internal_whitespace": True,
            "strip_bom": True,
        },
        "enum_synonyms": {
            "_comment": "특정 컬럼의 값 동의어 통합. (col pattern → {canonical: [aliases]}). pattern 은 소문자 컬럼명 정확 매칭 또는 suffix 매칭. 예: {\"status\": {\"Active\": [\"active\", \"운영중\"]}}. 비어 있으면 enum 정규화를 건너뛴다.",
        },
    }


_TBOX_SHAPES_TEMPLATE = """\
@prefix : <urn:ontology-agent:shapes#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

# =============================================================================
# T-Box SHACL Shapes (domain-agnostic)
# =============================================================================

:ClassShape a sh:NodeShape ;
    sh:targetClass owl:Class ;
    sh:property [
        sh:path rdfs:label ;
        sh:minCount 1 ;
        sh:message "owl:Class must have at least one rdfs:label"@en ;
    ] ;
    sh:property [
        sh:path rdfs:comment ;
        sh:minCount 1 ;
        sh:severity sh:Warning ;
        sh:message "owl:Class should have at least one rdfs:comment"@en ;
    ] .

:DatatypePropertyShape a sh:NodeShape ;
    sh:targetClass owl:DatatypeProperty ;
    sh:property [
        sh:path rdfs:domain ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:message "owl:DatatypeProperty must have exactly one rdfs:domain"@en ;
    ] ;
    sh:property [
        sh:path rdfs:range ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:message "owl:DatatypeProperty must have exactly one rdfs:range (xsd type)"@en ;
    ] ;
    sh:property [
        sh:path rdfs:label ;
        sh:minCount 1 ;
        sh:message "owl:DatatypeProperty must have rdfs:label"@en ;
    ] .

:ObjectPropertyShape a sh:NodeShape ;
    sh:targetClass owl:ObjectProperty ;
    sh:property [
        sh:path rdfs:domain ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:message "owl:ObjectProperty must have exactly one rdfs:domain"@en ;
    ] ;
    sh:property [
        sh:path rdfs:range ;
        sh:minCount 1 ;
        sh:maxCount 1 ;
        sh:message "owl:ObjectProperty must have exactly one rdfs:range"@en ;
    ] ;
    sh:property [
        sh:path rdfs:label ;
        sh:minCount 1 ;
        sh:message "owl:ObjectProperty must have rdfs:label"@en ;
    ] .
"""


# ── Phase 2 헬퍼: LLM 생성 ─────────────────────────────────────────────────


def _build_llm_prompt(
    tables: dict[str, list[str]],
    fk_patterns: dict[str, str],
    domain_name: str,
    domain_name_ko: str,
    industry: str,
    industry_ko: str,
) -> str:
    # 테이블 요약 (테이블명 + 컬럼 5개까지)
    table_lines = []
    for tbl, cols in tables.items():
        preview = ", ".join(cols[:7])
        if len(cols) > 7:
            preview += f", ... (+{len(cols) - 7}개)"
        table_lines.append(f"  - {tbl}: [{preview}]")
    tables_str = "\n".join(table_lines)

    fk_str = json.dumps(fk_patterns, ensure_ascii=False, indent=2)

    return f"""\
당신은 온톨로지 엔지니어입니다. 아래 CSV 데이터 구조를 분석하여 3개의 JSON을 생성하세요.

## 도메인 정보
- 도메인: {domain_name} ({domain_name_ko})
- 산업: {industry} ({industry_ko})

## CSV 테이블 ({len(tables)}개)
{tables_str}

## 이미 탐지된 FK 관계
{fk_str}

## 생성할 파일 3개

### 1. table_labels.json
각 테이블의 한국어 레이블. 테이블명을 보고 도메인 맥락에 맞게 번역하세요.
```json
{{"Table_Name": "한국어 레이블", ...}}
```

### 2. disjoint_groups.json
의미적으로 겹칠 수 없는 클래스를 그룹화합니다.
규칙:
- 같은 그룹 내 클래스는 owl:AllDisjointClasses로 선언됨
- 서로 다른 도메인(설비, 공정, 품질 등)의 테이블끼리 그룹화
- 공정 단계 클래스가 있으면 process_step_parent와 process_step_classes도 식별

```json
{{
  "groups": [
    {{"label": "그룹명", "classes": ["PascalCase클래스명", ...]}}
  ],
  "process_step_parent": "부모클래스명 또는 null",
  "process_step_classes": ["순서대로"]
}}
```
클래스명은 테이블명을 PascalCase로 변환 (예: Process_Blast_Furnace → ProcessBlastFurnace).

### 3. design_patterns.json
온톨로지 디자인 패턴(ODP) 라이브러리.
```json
{{
  "description": "온톨로지 디자인 패턴 (ODP) 라이브러리",
  "domain_hierarchy": {{
    "도메인 한국어명": {{
      "abstract_class": "추상부모클래스명",
      "iof_parent": "IOF 상위클래스 (iof-core:MaterialArtifact 등)",
      "sub_groups": {{
        "서브그룹명": ["클래스1", "클래스2"]
      }},
      "cross_domain_ops": ["도메인간ObjectProperty명"]
    }}
  }},
  "axiom_patterns": {{
    "pk_functional": "PK 컬럼(*Id, *Code)은 owl:FunctionalProperty로 선언",
    "fk_some_values_from": "FK ObjectProperty의 domain에 owl:someValuesFrom 제약 추가",
    "inverse_of": "모든 ObjectProperty에 owl:inverseOf 역방향 프로퍼티 정의"
  }},
  "metric_targets": {{
    "dit": {{"min": 2, "max": 5, "description": "Depth of Inheritance Tree"}},
    "rr": {{"min": 0.3, "max": 0.6, "description": "Relationship Richness"}},
    "ar": {{"min": 3, "max": 15, "description": "Attribute Richness"}},
    "axiom": {{"min": 2, "description": "클래스당 평균 axiom 수"}},
    "annotation": {{"min": 100, "description": "모든 엔티티에 label+comment"}}
  }}
}}
```

IOF 매핑 가이드:
- 설비/장비 → iof-core:MaterialArtifact
- 제조 공정 → iof-core:ManufacturingProcess
- 측정/품질/환경 → iof-core:MeasurementInformationContentEntity
- 계획/생산 → iof-core:PlannedProcess
- 정비 → iof-maint:MaintenanceProcess
- 정보/문서/물류 → iof-core:InformationContentEntity

cross_domain_ops: 도메인 간 연결 ObjectProperty (camelCase).
예: 설비→정비 = "requiresMaintenance", 공정→제품 = "producesProduct"

## 출력 형식
각 파일을 아래 태그로 감싸서 출력하세요:

<table_labels>
JSON
</table_labels>

<disjoint_groups>
JSON
</disjoint_groups>

<design_patterns>
JSON
</design_patterns>
"""


def _parse_llm_response(text: str) -> dict[str, dict]:
    """LLM 응답에서 태그별 JSON 추출."""
    results = {}
    for tag in ("table_labels", "disjoint_groups", "design_patterns"):
        pattern = rf"<{tag}>\s*([\s\S]*?)\s*</{tag}>"
        match = re.search(pattern, text)
        if match:
            raw = match.group(1).strip()
            # 코드펜스 제거
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            try:
                results[tag] = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("JSON 파싱 실패 (%s): %s", tag, e)
    return results


# ── MCP 도구 ────────────────────────────────────────────────────────────────


def initialize_domain_rules(
    domain_name: str,
    domain_name_ko: str,
    namespace_uri: str,
    prefix: str,
    industry: str = "",
    industry_ko: str = "",
    overwrite: bool = False,
    retire_stale_domain_files: bool = True,
) -> str:
    """CSV 데이터를 분석하여 rules/ 설정 파일 12개를 자동 생성한다.

    새로운 도메인에 파이프라인을 적용할 때, data/source/rawdata/에 CSV를 배치한 뒤
    이 도구를 실행하면 카테고리별 하위 폴더 (``rules/domain`` / ``policy`` /
    ``contracts``) 에 설정 파일이 생성된다.

    Phase 1 (프로그래매틱): domain_config, tbox_generation_config, table_class_mapping,
    fk_patterns, value_ranges, tbox_shapes.ttl, value_heuristics, common_dp,
    value_normalizations — CSV 구조에서 기계적으로 생성. value_heuristics/common_dp/
    value_normalizations 는 abox_generation 이 required=True 로 로드하는 도메인-중립
    계약 파일 (enum_synonyms / required_props_by_class 는 빈 템플릿, SME 가 채움).

    Phase 2 (Bedrock LLM 1회): table_labels, disjoint_groups, design_patterns
    — 도메인 이해가 필요한 파일을 LLM이 생성.

    **이 도구가 만들지 않는 도메인 자산** 은 응답의 ``domain_files_not_generated``
    에 나온다. 이전 도메인 파일이 남아 있으면 조용히 오염시키므로 (실측:
    ``abstract_group_hints.json`` 의 ``child_name_patterns`` 는 ``"Monitoring"`` /
    ``"Energy"`` 같은 **일반 영어 단어** 라 다른 산업에서도 매칭된다) 응답의
    ``stale_domain_files`` 를 반드시 확인하고 교체·삭제하라. 없으면 해당 기능이
    안전하게 no-op 한다.

    Args:
        domain_name: 도메인 영문명 (예: "Example Semiconductor Manufacturing")
        domain_name_ko: 도메인 한국어명 (예: "예시 반도체 제조")
        namespace_uri: 온톨로지 네임스페이스 URI (예: "http://example.com/semi-ontology")
        prefix: RDF PREFIX (예: "semi")
        industry: 산업 영문명 (예: "semiconductor manufacturing"). 비어있으면 domain_name 사용.
        industry_ko: 산업 한국어명 (예: "반도체 제조"). 비어있으면 domain_name_ko 사용.
        overwrite: True면 기존 파일 덮어쓰기. False면 기존 파일 건너뛰기.
        retire_stale_domain_files: **기본 True.** 이 도구가 만들지 않는 도메인 자산 중
            이전 도메인 값이 남아 있는 것(응답의 ``stale_domain_files``)을
            ``<파일명>.bak_<UTC타임스탬프>`` 로 옮겨 경로에서 치운다. **삭제가 아니라
            이름 변경** 이므로 되돌릴 수 있고, ``.bak_*`` 는 gitignore 대상이다.

            **왜 기본으로 켜는가**: 그 6개는 이식 시 이전 도메인 값이 그대로 남고,
            ``abstract_group_hints.json`` 은 ``Master``/``Quality``/``Analysis``/
            ``Monitoring`` 같은 **일반 영어 단어** 를 패턴으로 써서 타 도메인 클래스에도
            매칭된다. 실측 (2026-08-25): 철강 파일이 남은 상태로 병원 클래스에 적용하면
            ``VitalMonitoring ⊑ EnvironmentalMonitoring`` /
            ``LabAnalysis ⊑ QualityManagement`` 등 **6건** 이 에러 없이 주입된다.
            은퇴 후 같은 입력에 주입은 **0건** 이다.

            그리고 README 의 이식 절차가 이미 "철강 규칙을 **먼저 비우거나 삭제**
            하는 것이 첫 단계" 라고 안내한다 — 도구가 그것을 해주지 않으면 문서와
            동작이 어긋난다.

            **``overwrite`` 와 무엇이 다른가**: ``overwrite`` 는 이 도구가 **생성하는**
            12개를 기본값으로 되돌린다 (SME 가 다듬은 값 소실). 이쪽은 이 도구가
            **만들지 않는** 6개를 백업하고 치운다. 전자는 파괴, 후자는 되돌릴 수 있는
            이동이라 기본값이 다르다 (``overwrite`` 는 여전히 False).

            **``False`` 로 둬야 하는 경우**: S5 까지 진행해 ``tacit_rules.json`` 을
            채운 뒤 CSV 변경 때문에 ``rules`` 초기화만 다시 돌리는 **부분 재초기화**.
            정규 흐름은 아니지만 (작성은 S5, 초기화는 1단계) 실재하는 순서다. 그때도
            백업이 남으므로 ``retired_domain_files`` 를 보고 되돌릴 수 있다.
    """
    try:
        return _initialize_domain_rules(
            domain_name, domain_name_ko, namespace_uri, prefix,
            industry, industry_ko, overwrite,
            retire_stale_domain_files=retire_stale_domain_files,
        )
    except Exception as exc:
        return error_response(exc, hint="CSV가 data/source/rawdata/에 있는지 확인하세요.", logger=logger)


def _initialize_domain_rules(
    domain_name: str,
    domain_name_ko: str,
    namespace_uri: str,
    prefix: str,
    industry: str,
    industry_ko: str,
    overwrite: bool,
    *,
    retire_stale_domain_files: bool = True,
) -> str:
    # 기본값 처리
    industry = industry or domain_name
    industry_ko = industry_ko or domain_name_ko

    # CSV 스캔
    tables = _scan_csv_tables()
    if not tables:
        return error_response(
            "CSV 파일을 찾을 수 없습니다.",
            hint=f"data/source/rawdata/ 경로에 CSV를 배치하세요. 현재 경로: {SOURCE_RAWDATA_DIR}",
        )

    for category in (DOMAIN_DIR, POLICY_DIR, CONTRACT_DIR):
        os.makedirs(os.path.join(_RULES_DIR, category), exist_ok=True)

    created: list[str] = []
    skipped: list[str] = []

    def _write(filename: str, content: str) -> None:
        # rules_path 가 카테고리를 결정한다 — 이 함수가 하위 폴더를 몰라도 되고,
        # 예전 위치(루트 직하)에 파일이 있으면 그것을 존중해 중복 생성하지 않는다.
        path = rules_path(filename, base=_RULES_DIR)
        if not overwrite and os.path.exists(path):
            skipped.append(filename)
            return
        atomic_write(path, content)
        created.append(os.path.join(category_of(filename), filename))

    def _write_json(filename: str, data: dict) -> None:
        _write(filename, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    # ── Phase 1: 프로그래매틱 생성 ──────────────────────────────────────

    # 1. domain_config.json
    domain_config = _gen_domain_config(
        domain_name, domain_name_ko, namespace_uri, prefix, industry, industry_ko,
    )
    _write_json("domain_config.json", domain_config)

    # 2. tbox_generation_config.json
    _write_json("tbox_generation_config.json", _gen_tbox_generation_config())

    # 3. table_class_mapping.json
    tcm = _gen_table_class_mapping(tables, prefix)
    _write_json("table_class_mapping.json", tcm)

    # 4. fk_patterns.json
    fk = _gen_fk_patterns(tables)
    _write_json("fk_patterns.json", fk)

    # 5. value_ranges.json
    vr = _gen_value_ranges(tables)
    _write_json("value_ranges.json", vr)

    # 6. tbox_shapes.ttl
    _write("tbox_shapes.ttl", _TBOX_SHAPES_TEMPLATE)

    # 7. value_heuristics.json (abox_generation 이 required=True 로 로드)
    _write_json("value_heuristics.json", _gen_value_heuristics())

    # 8. common_dp.json (class-specific DP 추론 템플릿)
    _write_json("common_dp.json", _gen_common_dp())

    # 9. value_normalizations.json (CSV 값 전처리 계약)
    _write_json("value_normalizations.json", _gen_value_normalizations())

    phase1_count = len(created)

    # ── Phase 2: LLM 생성 ──────────────────────────────────────────────

    llm_files = ["table_labels.json", "disjoint_groups.json", "design_patterns.json"]
    need_llm = overwrite or any(
        not os.path.exists(rules_path(f, base=_RULES_DIR)) for f in llm_files
    )

    if need_llm:
        from tools.bedrock import invoke_bedrock_text

        prompt = _build_llm_prompt(
            tables, fk["patterns"], domain_name, domain_name_ko, industry, industry_ko,
        )
        llm_text = invoke_bedrock_text(prompt, max_tokens=8000, temperature=0)
        parsed = _parse_llm_response(llm_text)

        if "table_labels" in parsed:
            _write_json("table_labels.json", parsed["table_labels"])

        if "disjoint_groups" in parsed:
            dg = parsed["disjoint_groups"]
            if "_comment" not in dg:
                dg["_comment"] = "AllDisjointClasses 그룹 정의. 다른 산업 적용 시 이 파일만 교체."
            _write_json("disjoint_groups.json", dg)

        if "design_patterns" in parsed:
            dp = parsed["design_patterns"]
            if "reference" not in dp:
                dp["reference"] = "Gangemi, A. (2005) Ontology Design Patterns for Semantic Web Content"
            _write_json("design_patterns.json", dp)

        # LLM 파싱 실패한 파일 보고
        for f in llm_files:
            tag = f.replace(".json", "")
            if tag not in parsed and f not in skipped:
                skipped.append(f"{f} (LLM 파싱 실패)")
    else:
        for f in llm_files:
            if os.path.exists(rules_path(f, base=_RULES_DIR)):
                skipped.append(f)

    # ── 결과 요약 ───────────────────────────────────────────────────────

    summary = {
        "tables_scanned": len(tables),
        "fk_patterns_found": len(fk["patterns"]),
        "value_ranges_found": len(vr["ranges"]),
        "phase1_files": phase1_count,
        "phase2_files": len(created) - phase1_count,
    }

    # 이 도구가 만들지 않는 도메인 자산을 드러낸다. 침묵이 위험한 이유: 이전 도메인
    # 파일이 남아 있으면 예외도 경고도 없이 **잘못된 계층/규칙** 을 주입한다.
    generated = {os.path.basename(f).split(" ")[0] for f in created + skipped}
    not_generated = sorted(REPLACE_ON_NEW_DOMAIN - generated)
    stale = [f for f in not_generated if os.path.exists(rules_path(f, base=_RULES_DIR))]

    # ── 이전 도메인 자산 은퇴 (opt-in) ────────────────────────────────
    # 이 6개는 도구가 만들지 않으므로 이식 시 **이전 도메인 값이 그대로 남는다**.
    # 실측 (2026-08-25): 철강 ``abstract_group_hints.json`` 을 병원 클래스에 적용하면
    # ``VitalMonitoring ⊑ EnvironmentalMonitoring`` / ``LabAnalysis ⊑
    # QualityManagement`` 등 **6건** 이 주입된다 — 패턴이 ``Master`` / ``Quality`` /
    # ``Analysis`` / ``Monitoring`` 같은 일반 영어 단어라서다. 에러도 경고도 없다.
    #
    # **왜 기본값이 아닌가**: 이 도구의 계약은 "``overwrite=True`` 없이 SME 편집을
    # 파괴하지 않는다" 다 (tests/test_rules_init_overwrite_guard.py). 특히
    # ``tacit_rules.json`` 은 SME 가 검증한 지식이 들어가는 자리이고,
    # ``suggest_tacit_rules`` → 검토 → 승격 흐름에서 **초기화보다 먼저 작성될 수
    # 있다**. 무조건 지우면 그 작업이 사라진다.
    #
    # **왜 삭제가 아니라 백업인가**: 파일이 없으면 해당 기능이 안전하게 no-op 하므로
    # 목적은 "경로에서 치우기" 이고 소실이 아니다. ``.bak_*`` 는 이미 gitignore 대상
    # 이라 (``.gitignore``: ``rules/**/*.bak_*``) 커밋에 섞이지 않는다.
    retired: list[str] = []
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if retire_stale_domain_files and stale:
        for name in stale:
            src = rules_path(name, base=_RULES_DIR)
            dest = f"{src}.bak_{ts}"
            try:
                os.replace(src, dest)
                retired.append(os.path.join(category_of(name), name))
            except OSError as exc:  # noqa: PERF203 — 한 파일 실패가 나머지를 막지 않는다
                logger.warning("이전 도메인 파일 은퇴 실패 (%s): %s", name, exc)
        stale = [
            f for f in not_generated
            if os.path.exists(rules_path(f, base=_RULES_DIR))
        ]

    return json.dumps(
        {
            "success": True,
            "files_created": created,
            "files_skipped": skipped,
            "summary": summary,
            "domain_files_not_generated": not_generated,
            "stale_domain_files": stale,
            "retired_domain_files": retired,
            "retire_backup_suffix": f".bak_{ts}" if retired else "",
            "stale_warning": (
                f"이전 도메인 파일 {len(stale)}개가 남아 있습니다 — 교체하거나 "
                f"삭제하세요 (없으면 해당 기능이 no-op 합니다): {stale}"
            ) if stale else "",
        },
        ensure_ascii=False,
        indent=2,
    )
