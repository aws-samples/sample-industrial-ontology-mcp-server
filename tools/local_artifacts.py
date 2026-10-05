"""로컬 아티팩트 read + CSV 프로파일링/ERD 도구.

data/source/ — 원본 입력 데이터 (CSV, 스키마, IOF 매핑, tacit)
data/generated/ — 생성된 산출물 (T-Box, A-Box, 추론 결과, 시맨틱 딕셔너리)
"""
from __future__ import annotations

import glob
import html
import json
import logging
import os
import re

from config import (
    ABOX_PATH,
    INFERRED_PATH,
    SOURCE_MAPPING_DIR,
    SOURCE_RAWDATA_DIR,
    SOURCE_TACIT_DIR,
    TBOX_PATH,
    resolve_generated_path,
)
from domain.rules_paths import rules_path
from tools.common import (
    ESCAPE_HTML_JS,
    VIS_NETWORK_SCRIPT_TAG,
    error_response,
    json_for_script,
    resolve_child_path,
    safe_read_file,
)

logger = logging.getLogger(__name__)


# ── 읽기 도구 ──────────────────────────────────────


def read_tbox() -> str:
    """로컬에서 현재 T-Box TTL 파일을 읽는다. (data/generated/tbox/t_box.ttl)"""
    return safe_read_file(TBOX_PATH, hint="generate_tbox로 T-Box를 먼저 생성하세요.", logger=logger)


def read_abox() -> str:
    """로컬에서 현재 A-Box TTL 파일을 읽는다. (data/generated/abox/a_box.ttl)"""
    return safe_read_file(ABOX_PATH, hint="generate_abox로 A-Box를 먼저 생성하세요.", logger=logger)


def read_inferred() -> str:
    """로컬에서 추론 결과 TTL 파일을 읽는다. (data/generated/inferred/all_inferred.ttl)"""
    return safe_read_file(INFERRED_PATH, hint="run_owl_rl_inference 를 먼저 실행해 추론 결과를 생성하세요.", logger=logger)


def list_csv_tables() -> str:
    """로컬 data/source/rawdata/ 폴더의 CSV 파일 목록을 반환한다."""
    try:
        csv_files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
        if not csv_files:
            return error_response(f"CSV 파일이 없습니다: {SOURCE_RAWDATA_DIR}/", hint="data/source/rawdata/ 에 CSV 파일을 배치하세요.", logger=logger)
        files = []
        for path in csv_files:
            stat = os.stat(path)
            files.append({
                "name": os.path.basename(path),
                "path": path,
                "size": stat.st_size,
            })
        return json.dumps(files, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


_csv_schema_cache = None


def read_csv_schema() -> str:
    """CSV 파일 헤더에서 테이블 스키마를 추출하여 JSON으로 반환한다. 결과를 캐시한다."""
    import csv as csv_mod
    global _csv_schema_cache
    if _csv_schema_cache is not None:
        return _csv_schema_cache
    try:
        csv_files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
        if not csv_files:
            return error_response(f"CSV 파일이 없습니다: {SOURCE_RAWDATA_DIR}/", hint="data/source/rawdata/ 에 CSV 파일을 배치하세요.", logger=logger)
        schema = {"tables": {}}
        for csv_path in csv_files:
            table_name = os.path.basename(csv_path).replace(".csv", "")
            try:
                with open(csv_path, encoding="utf-8-sig") as f:
                    header = next(csv_mod.reader(f), None)
                if header:
                    schema["tables"][table_name] = {"columns": header}
            except Exception:  # noqa: BLE001 — 읽을 수 없는 CSV는 나머지 스키마 수집에서 제외한다
                continue
        schema["success"] = True
        result = json.dumps(schema, ensure_ascii=False, indent=2)
        _csv_schema_cache = result
        return result
    except Exception as e:
        return error_response(e, logger=logger)


def read_iof_mapping() -> str:
    """로컬에서 IOF 매핑 결과 JSON을 읽는다. (data/source/mapping/iof_masterdata_mapping.json)"""
    path = os.path.join(SOURCE_MAPPING_DIR, "iof_masterdata_mapping.json")
    return safe_read_file(path, hint="data/source/mapping/ 에 iof_masterdata_mapping.json을 배치하세요.", logger=logger)


# ── 암묵지 도구 ───────────────────────────────────


def list_tacit_files() -> str:
    """로컬 파일시스템의 data/source/tacit/ TTL 파일 목록과 metadata를 읽는다.

    파일시스템 작업은 지정 디렉터리의 read/list/stat으로 제한되며 파일을
    생성, 수정, 삭제하지 않는다.

    암묵지 파일은 CSV에 없는 도메인 지식(공정 흐름, 품질 규격 등)을 담은 TTL.
    추론 시 T-Box + A-Box와 함께 자동으로 병합된다.
    """
    try:
        ttl_files = sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")))
        if not ttl_files:
            return json.dumps({
                "success": True,
                "count": 0,
                "files": [],
                "hint": "암묵지 파일이 없습니다. data/source/tacit/ 에 TTL 파일을 추가하세요.",
            }, ensure_ascii=False)
        files = []
        for path in ttl_files:
            stat = os.stat(path)
            files.append({
                "name": os.path.basename(path),
                "path": path,
                "size": stat.st_size,
            })
        return json.dumps({
            "success": True,
            "count": len(files),
            "files": files,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def read_tacit(filename: str = "") -> str:
    """암묵지 TTL 파일을 읽는다.

    Args:
        filename: 읽을 파일명 (예: process_flow.ttl). 비어있으면 모든 암묵지 파일을 합쳐서 반환.
    """
    try:
        if filename:
            path = resolve_child_path(
                SOURCE_TACIT_DIR,
                filename,
                allowed_suffixes=(".ttl",),
            )
            if not os.path.exists(path):
                return error_response(f"파일이 없습니다: {path}", hint="list_tacit_files로 사용 가능한 파일을 확인하세요.", logger=logger)
            with open(path, encoding="utf-8") as f:
                return f.read()
        else:
            ttl_files = sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")))
            if not ttl_files:
                return error_response("암묵지 파일이 없습니다.", hint="data/source/tacit/ 에 TTL 파일을 추가하세요.", logger=logger)
            contents = []
            for path in ttl_files:
                with open(path, encoding="utf-8") as f:
                    contents.append(f"# === {os.path.basename(path)} ===\n{f.read()}")
            return "\n\n".join(contents)
    except Exception as e:
        return error_response(e, logger=logger)


# ── CSV ERD 시각화 ────────────────────────────────


# ERD 보조 매핑 — **도메인 종속이라 코드에 두지 않는다.**
#
# 이전에는 예시 데이터의 테이블명이 그대로 박혀 있었다 (``Equipment_ID`` →
# ``Equipment_Master`` 등 9쌍, 도메인 그룹 10개). 다른 산업으로 전환하면 전부
# 매칭되지 않아 ERD 의 FK 표시와 색상 구분이 조용히 사라지고, 그 사실이
# 어디에도 드러나지 않았다. 설정으로 옮겨 "없으면 없다고 보이게" 한다.
#
# rules/domain/domain_config.json 형식 (모두 선택):
#   "erd_hints": {
#     "fk_columns": {"<COLUMN>": "<target table>", ...},
#     "table_groups": {"<group name>": ["<table>", ...], ...}
#   }
# 색상은 ``erd_stage_grouping.colors`` 가 담당한다 (아래).
# FK 는 rules/contracts/fk_patterns.json 이 우선이고 이 힌트는 보조다.


def _load_erd_hints() -> tuple[dict[str, str], dict[str, list]]:
    """(fk column → target table, group → tables). 설정이 없으면 빈 매핑."""
    try:
        from domain.namespaces import DOMAIN_CONFIG
        cfg = (DOMAIN_CONFIG or {}).get("erd_hints") or {}
    except Exception as exc:  # noqa: BLE001 — ERD 생성이 설정 때문에 실패하면 안 된다
        logger.debug("erd_hints 로드 실패 (보조 매핑 생략): %s", exc)
        # 2-tuple 계약. 예전엔 이 경로만 3개를 반환해서, 설정 로드가 실패하면
        # 모듈 최상위 언패킹(`_FK_MAP, _DOMAIN_GROUPS = _load_erd_hints()`) 이
        # ValueError 로 터지면 명시적 registry가 fail-closed로 기동을 중단한다.
        return {}, {}
    fk = {k: v for k, v in (cfg.get("fk_columns") or {}).items() if isinstance(v, str)}
    groups = {k: v for k, v in (cfg.get("table_groups") or {}).items() if isinstance(v, list)}
    return fk, groups


_FK_MAP, _DOMAIN_GROUPS = _load_erd_hints()

# ERD 를 공정 단계별로 색칠하기 위한 클래스 → 단계 매핑. **도메인 종속 설정**이라
# 코드에 두지 않고 rules/domain/domain_config.json 의 ``erd_stage_grouping`` 에서 읽는다
# (없으면 단계 구분 없이 단색 — 도메인-중립 배포에서 no-op).
#
# 설정 형식:
#   "erd_stage_grouping": {
#     "stages": {"<ClassName>": "<단계명>", ...},
#     "colors": {"<단계명>": "#RRGGBB", ..., "기타": "#95A5A6"}
#   }
_DEFAULT_STAGE_COLOR = "#95A5A6"


def _load_erd_stage_grouping() -> tuple[dict[str, str], dict[str, str]]:
    """(class → stage, stage → color). 설정이 없으면 빈 매핑."""
    try:
        from domain.namespaces import DOMAIN_CONFIG
        cfg = (DOMAIN_CONFIG or {}).get("erd_stage_grouping") or {}
    except Exception as exc:  # noqa: BLE001 — ERD 생성이 설정 때문에 실패하면 안 된다
        logger.debug("erd_stage_grouping 로드 실패 (단계 구분 생략): %s", exc)
        return {}, {}
    stages = {k: v for k, v in (cfg.get("stages") or {}).items() if isinstance(v, str)}
    colors = {k: v for k, v in (cfg.get("colors") or {}).items() if isinstance(v, str)}
    return stages, colors


_STEEL_STAGE, _STAGE_COLORS = _load_erd_stage_grouping()

_ERD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_ERD_NUM_RE = re.compile(r"^-?\d+\.?\d*$")


def _erd_dominant_type(values: list) -> str:
    """Infer the dominant XSD-ish type of a column from sample values."""
    counts: dict[str, int] = {}
    for v in values:
        if not v:
            t = "null"
        elif _ERD_DATE_RE.match(v):
            t = "dateTime"
        elif _ERD_NUM_RE.match(v):
            t = "decimal" if "." in v else "integer"
        else:
            t = "string"
        counts[t] = counts.get(t, 0) + 1
    counts.pop("null", None)
    return max(counts, key=counts.get) if counts else "string"


def _get_domain(table_name: str) -> str:
    for domain, tables in _DOMAIN_GROUPS.items():
        if table_name in tables:
            return domain
    return "기타"


# 테이블 한글명 — rules/domain/table_labels.json에서 로드, 없으면 파일명에서 자동 생성
_TABLE_LABELS_PATH = rules_path("table_labels.json")
try:
    with open(_TABLE_LABELS_PATH, encoding="utf-8") as _f:
        _TABLE_KO: dict[str, str] = json.load(_f)
except (FileNotFoundError, json.JSONDecodeError):
    _TABLE_KO = {}


def _table_display_name(table_name: str) -> str:
    """테이블 한글 표시명을 반환한다. 매핑이 없으면 파일명에서 생성."""
    if table_name in _TABLE_KO:
        return _TABLE_KO[table_name]
    return table_name.replace("_", " ")

# 컬럼 한글 요약 패턴
_COL_KO_PATTERNS = [
    ("Equipment_ID", "설비 ID", "FK"),
    ("Equipment_Name", "설비명", "string"),
    ("Equipment_Type", "설비 유형", "string"),
    ("Tag_ID", "태그 ID", "FK"),
    ("Tag_Name", "태그명", "string"),
    ("Product_ID", "제품 ID", "FK"),
    ("Item_Code", "자재 코드", "FK"),
    ("Supplier_ID", "공급사 ID", "FK"),
    ("Warehouse_Code", "창고 코드", "FK"),
    ("Energy_Source_ID", "에너지원 ID", "FK"),
    ("Point_ID", "모니터링 포인트 ID", "FK"),
    ("Monitor_ID", "모니터 ID", "FK"),
    ("Timestamp", "측정 시각", "datetime"),
    ("Status", "상태", "string"),
    ("Location", "위치", "string"),
    ("Manufacturer", "제조사", "string"),
    ("Severity", "심각도", "string"),
    ("Message", "메시지", "string"),
    ("Unit", "단위", "string"),
    ("Description", "설명", "string"),
    ("Category", "분류", "string"),
    ("Specification", "규격", "string"),
    ("Type", "유형", "string"),
]

# 키워드 기반 타입/한글 추론
_COL_KO_KEYWORDS = {
    "temp": ("온도", "decimal"), "pressure": ("압력", "decimal"),
    "flow": ("유량", "decimal"), "speed": ("속도", "decimal"),
    "force": ("힘", "decimal"), "percent": ("비율(%)", "decimal"),
    "rate": ("비율", "decimal"), "cost": ("비용", "decimal"),
    "price": ("가격", "decimal"), "weight": ("중량", "decimal"),
    "quantity": ("수량", "decimal"), "stock": ("재고량", "decimal"),
    "hours": ("시간", "decimal"), "count": ("건수", "integer"),
    "score": ("점수", "decimal"), "factor": ("계수", "decimal"),
    "level": ("수위/레벨", "decimal"), "thickness": ("두께", "decimal"),
    "width": ("폭", "decimal"), "length": ("길이", "decimal"),
    "voltage": ("전압", "decimal"), "current": ("전류", "decimal"),
    "power": ("전력", "decimal"), "efficiency": ("효율", "decimal"),
    "strength": ("강도", "decimal"), "hardness": ("경도", "decimal"),
    "elongation": ("연신율", "decimal"), "roughness": ("조도", "decimal"),
    "concentration": ("농도", "decimal"), "amount": ("양", "decimal"),
    "consumption": ("소비량", "decimal"), "emission": ("배출", "decimal"),
    "calorific": ("열량", "decimal"), "enthalpy": ("엔탈피", "decimal"),
    "basicity": ("염기도", "decimal"), "date": ("날짜", "datetime"),
    "time": ("시각", "datetime"),
}


def _col_info(col_name: str, table_name: str) -> dict:
    """컬럼의 한글명, 타입, 역할(PK/FK/일반)을 추론한다."""
    # 정확 매핑
    for pattern, ko, dtype in _COL_KO_PATTERNS:
        if col_name == pattern:
            role = "FK" if dtype == "FK" else "일반"
            return {"name": col_name, "ko": ko, "type": dtype, "role": role}

    # FK 확인
    if col_name in _FK_MAP and _FK_MAP[col_name] != table_name:
        ko = col_name.replace("_", " ")
        return {"name": col_name, "ko": ko, "type": "FK", "role": "FK"}

    # 키워드 기반 추론
    col_lower = col_name.lower()
    for kw, (ko, dtype) in _COL_KO_KEYWORDS.items():
        if kw in col_lower:
            return {"name": col_name, "ko": ko, "type": dtype, "role": "일반"}

    # ID/Code로 끝나면 PK 후보
    if col_lower.endswith("_id") or col_lower.endswith("_code"):
        ko = col_name.replace("_", " ")
        return {"name": col_name, "ko": ko, "type": "string", "role": "PK"}

    return {"name": col_name, "ko": col_name.replace("_", " "), "type": "string", "role": "일반"}


def profile_csv_data(table_name: str = "") -> str:
    """CSV 데이터 품질 프로파일링 — A-Box 생성 전 데이터 이슈를 사전 탐지한다.

    컬럼별 NULL/빈값 비율, 유니크 비율, 데이터 타입 불일치, FK 값 유효성을 분석.
    table_name이 비어있으면 전체 테이블 요약, 지정하면 해당 테이블 상세 분석.

    Args:
        table_name: CSV 파일명 (확장자 제외). 비어있으면 전체 요약.
    """
    import csv as csv_mod
    import re

    _DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
    _NUMBER_RE = re.compile(r"^-?\d+\.?\d*$")

    def _infer_type(value: str) -> str:
        if not value:
            return "null"
        if value.lower() in ("true", "false"):
            return "boolean"
        if _DATE_RE.match(value):
            return "dateTime"
        if _NUMBER_RE.match(value):
            return "decimal" if "." in value else "integer"
        return "string"

    def _profile_table(csv_path: str) -> dict:
        tname = os.path.basename(csv_path).replace(".csv", "")
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv_mod.reader(f)
            header = next(reader, None)
            if not header:
                return {"table": tname, "error": "헤더 없음"}
            rows = list(reader)

        row_count = len(rows)
        if row_count == 0:
            return {"table": tname, "rows": 0, "columns": len(header), "issues": ["데이터 없음"]}

        col_profiles = []
        issues = []
        for col_idx, col_name in enumerate(header):
            values = [row[col_idx] if col_idx < len(row) else "" for row in rows]
            non_empty = [v for v in values if v.strip()]
            null_count = row_count - len(non_empty)
            null_rate = round(null_count / row_count * 100, 1)
            unique_count = len(set(non_empty))
            unique_rate = round(unique_count / max(len(non_empty), 1) * 100, 1)

            # 타입 추론 (샘플 최대 100행)
            type_counts: dict[str, int] = {}
            for v in non_empty[:100]:
                t = _infer_type(v)
                type_counts[t] = type_counts.get(t, 0) + 1
            dominant_type = max(type_counts, key=type_counts.get) if type_counts else "null"
            mixed_types = len([t for t in type_counts if t != "null"]) > 1

            profile = {
                "column": col_name,
                "null_rate": null_rate,
                "unique_rate": unique_rate,
                "unique_count": unique_count,
                "dominant_type": dominant_type,
                "mixed_types": mixed_types,
            }
            col_profiles.append(profile)

            # 이슈 탐지
            if null_rate > 80:
                issues.append({"column": col_name, "issue": "high_null", "rate": null_rate})
            if mixed_types:
                issues.append({"column": col_name, "issue": "mixed_types", "types": type_counts})
            if unique_rate == 100 and len(non_empty) > 10:
                profile["potential_pk"] = True

        return {
            "table": tname,
            "rows": row_count,
            "columns": len(header),
            "column_profiles": col_profiles,
            "issues": issues,
            "quality_score": _calc_table_score(col_profiles, row_count),
        }

    def _calc_table_score(col_profiles: list, row_count: int) -> float:
        if not col_profiles or row_count == 0:
            return 0
        # 점수: (1 - 평균 NULL률) * 100, 타입 혼합 패널티
        avg_null = sum(c["null_rate"] for c in col_profiles) / len(col_profiles)
        mixed_penalty = sum(5 for c in col_profiles if c.get("mixed_types")) / len(col_profiles)
        return round(max(0, (100 - avg_null) - mixed_penalty), 1)

    def _check_fk_validity(csv_files: list[str]) -> list[dict]:
        """FK 컬럼의 값이 참조 대상 테이블에 실제로 존재하는지 확인."""
        # PK 값 수집 (각 테이블의 첫 번째 *_ID/*_Code 컬럼)
        pk_values: dict[str, set[str]] = {}
        for csv_path in csv_files:
            tname = os.path.basename(csv_path).replace(".csv", "")
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv_mod.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                pk_idx = None
                for i, col in enumerate(header):
                    cl = col.lower().replace("_", "").replace(" ", "")
                    if cl.endswith("id") or cl.endswith("code"):
                        pk_idx = i
                        break
                if pk_idx is not None:
                    vals = set()
                    for row in reader:
                        if pk_idx < len(row) and row[pk_idx].strip():
                            vals.add(row[pk_idx].strip())
                    pk_values[tname] = vals

        # FK 매핑 패턴
        # FK 패턴을 rules/contracts/fk_patterns.json에서 로드
        import json as json_mod
        _fk_path = rules_path("fk_patterns.json")
        with open(_fk_path, encoding="utf-8") as _f:
            _fk_cfg = json_mod.load(_f)
        # class_name → CSV 파일명 변환 (PascalCase → Snake_Case)
        import re as re_mod
        fk_patterns = {}
        for col_key, cls_name in _fk_cfg.get("patterns", {}).items():
            # EquipmentMaster → Equipment_Master
            csv_name = re_mod.sub(r'(?<!^)(?=[A-Z])', '_', cls_name)
            fk_patterns[col_key] = csv_name

        fk_issues = []
        for csv_path in csv_files:
            tname = os.path.basename(csv_path).replace(".csv", "")
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv_mod.reader(f)
                header = next(reader, None)
                if not header:
                    continue
                rows = list(reader)

            for col_idx, col_name in enumerate(header):
                col_key = col_name.lower().replace("_", "").replace(" ", "")
                if col_key not in fk_patterns:
                    continue
                target_table = fk_patterns[col_key]
                if target_table not in pk_values:
                    continue

                fk_vals = {row[col_idx].strip() for row in rows
                           if col_idx < len(row) and row[col_idx].strip()}
                invalid = fk_vals - pk_values[target_table]
                if invalid:
                    fk_issues.append({
                        "source": tname,
                        "column": col_name,
                        "target": target_table,
                        "total_fk_values": len(fk_vals),
                        "invalid_count": len(invalid),
                        "invalid_rate": round(len(invalid) / max(len(fk_vals), 1) * 100, 1),
                        "invalid_samples": sorted(invalid)[:5],
                    })

        return fk_issues

    try:
        csv_files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
        if not csv_files:
            return error_response(
                f"CSV 파일이 없습니다: {SOURCE_RAWDATA_DIR}/",
                hint="data/source/rawdata/ 에 CSV 파일을 배치하세요.",
                logger=logger,
            )

        if table_name:
            # 특정 테이블 상세 분석
            target = resolve_child_path(
                SOURCE_RAWDATA_DIR,
                f"{table_name}.csv",
                allowed_suffixes=(".csv",),
            )
            if not os.path.exists(target):
                return error_response(f"테이블을 찾을 수 없습니다: {table_name}")
            result = _profile_table(target)
            result["success"] = True
            return json.dumps(result, ensure_ascii=False, indent=2)

        # 전체 요약
        summaries = []
        total_issues = 0
        for csv_path in csv_files:
            profile = _profile_table(csv_path)
            total_issues += len(profile.get("issues", []))
            summaries.append({
                "table": profile["table"],
                "rows": profile.get("rows", 0),
                "columns": profile.get("columns", 0),
                "quality_score": profile.get("quality_score", 0),
                "issue_count": len(profile.get("issues", [])),
            })

        # FK 유효성 검사
        fk_issues = _check_fk_validity(csv_files)

        avg_score = round(sum(s["quality_score"] for s in summaries) / max(len(summaries), 1), 1)

        return json.dumps({
            "success": True,
            "tables": len(summaries),
            "total_rows": sum(s["rows"] for s in summaries),
            "avg_quality_score": avg_score,
            "total_issues": total_issues,
            "fk_issues": fk_issues,
            "table_summaries": sorted(summaries, key=lambda x: x["quality_score"]),
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def generate_csv_erd(open_report: bool = False) -> str:
    """CSV 테이블 간 FK 관계를 ERD(Entity Relationship Diagram) HTML로 생성한다.

    S1_DATA 단계에서 원본 데이터의 테이블 관계를 시각화.
    vis.js 네트워크 그래프 + 테이블 클릭 시 우측 상세 패널 (컬럼 한글명, 타입, FK).

    CSV 파일명·헤더·셀 값과 공정 단계 설정은 그대로 싣지 않는다. 서버 쪽 HTML 은
    ``html.escape``, script 블록의 JSON 은 ``json_for_script``, 브라우저에서 조립하는
    innerHTML 은 ``escapeHtml`` 을 거친다.

    Args:
        open_report: True면 HTML을 브라우저에서 자동 오픈. 기본값은 False.
    """
    import csv as csv_mod
    import webbrowser

    try:
        csv_files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
        if not csv_files:
            return error_response("CSV 파일이 없습니다.", hint="data/source/rawdata/ 확인", logger=logger)

        # 룰 파일 로드 — 현재 도메인의 클래스 매핑 / PK / 검증된 FK 패턴.
        # (_FK_MAP/_DOMAIN_GROUPS 는 설정 기반 보조 힌트 — 미설정 시 빈 매핑.)
        fk_patterns: dict = {}
        from domain.table_mapping import (
            load_table_class_mapping,
            load_table_pk_columns,
        )
        table_to_class = load_table_class_mapping()
        pk_cols = load_table_pk_columns()
        try:
            with open(rules_path("fk_patterns.json"), encoding="utf-8") as rf:
                fk_patterns = json.load(rf).get("patterns", {})
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        class_to_table = {c: t for t, c in table_to_class.items()}

        def _norm(col: str) -> str:
            return col.lower().replace("_", "").replace(" ", "")

        # 테이블 정보 수집 — 전체 행 1회 스캔으로 컬럼 프로파일(NULL/고유/타입) 집계.
        tables = {}
        for f in csv_files:
            table = os.path.basename(f).replace(".csv", "")
            with open(f, encoding="utf-8") as fh:
                reader = csv_mod.reader(fh)
                header = next(reader, None)
                if not header:
                    continue
                ncol = len(header)
                col_values = [[] for _ in range(ncol)]   # non-empty values per column
                col_nonempty = [0] * ncol
                col_uniq = [set() for _ in range(ncol)]
                samples = []
                row_count = 0
                for row in reader:
                    row_count += 1
                    if len(samples) < 3:
                        samples.append(row)
                    for ci in range(ncol):
                        val = row[ci].strip() if ci < len(row) else ""
                        if val:
                            col_nonempty[ci] += 1
                            if len(col_uniq[ci]) < 100000:
                                col_uniq[ci].add(val)
                            if len(col_values[ci]) < 200:
                                col_values[ci].append(val)

            cls = table_to_class.get(table, "")
            tbl_pk = set(pk_cols.get(table, []))
            cols_detail = []
            for ci, col in enumerate(header):
                base = _col_info(col, table)
                ne = col_nonempty[ci]
                null_rate = round((row_count - ne) / row_count * 100, 1) if row_count else 0.0
                uniq = len(col_uniq[ci])
                # 역할 판정: rules PK > FK 패턴 > 레거시 추론
                if col in tbl_pk:
                    role = "PK"
                elif _norm(col) in fk_patterns and class_to_table.get(fk_patterns[_norm(col)]) not in (None, table):
                    role = "FK"
                else:
                    role = base.get("role", "일반")
                    if role in ("PK", "FK"):   # 레거시 추론은 신뢰하지 않음
                        role = "일반"
                cols_detail.append({
                    "name": col,
                    "ko": base.get("ko", col.replace("_", " ")),
                    "type": _erd_dominant_type(col_values[ci]),
                    "role": role,
                    "null_rate": null_rate,
                    "unique": uniq,
                    "samples": col_values[ci][:3],
                })
            stage = _STEEL_STAGE.get(cls, "기타")
            tables[table] = {
                "columns": header,
                "cols_detail": cols_detail,
                "rows": row_count,
                "ko": _table_display_name(table),
                "cls": cls,
                "stage": stage,
                "uniq_sets": col_uniq,    # for FK match-rate (dropped before serialize)
                "header": header,
            }

        # FK 관계 감지 + 매칭률 산출 (검증된 fk_patterns 기반).
        fk_relations = []
        for table, info in tables.items():
            for ci, col in enumerate(info["header"]):
                key = _norm(col)
                target_cls = fk_patterns.get(key)
                if not target_cls:
                    continue
                target_tbl = class_to_table.get(target_cls)
                if not target_tbl or target_tbl == table or target_tbl not in tables:
                    continue
                # 매칭률: source FK 값이 target PK 값 집합에 존재하는 비율
                src_vals = info["uniq_sets"][ci]
                tgt_info = tables[target_tbl]
                tgt_pk = pk_cols.get(target_tbl, [])
                match_rate = None
                if tgt_pk and tgt_pk[0] in tgt_info["header"] and src_vals:
                    pk_idx = tgt_info["header"].index(tgt_pk[0])
                    tgt_vals = tgt_info["uniq_sets"][pk_idx]
                    hit = len(src_vals & tgt_vals)
                    match_rate = round(hit / len(src_vals) * 100, 1)
                fk_relations.append({
                    "source": table, "target": target_tbl,
                    "fk_column": col, "match_rate": match_rate,
                })

        # 테이블 상세 JSON (JavaScript에서 사용) — uniq_sets 는 제외(직렬화 불가/대용량)
        table_details_js = {}
        for table, info in tables.items():
            fks_in = [r for r in fk_relations if r["target"] == table]
            fks_out = [r for r in fk_relations if r["source"] == table]
            table_details_js[table] = {
                "ko": info["ko"],
                "cls": info["cls"],
                "stage": info["stage"],
                "domain": info["stage"],
                "rows": info["rows"],
                "columns": info["cols_detail"],
                "fk_in": [{"from": r["source"], "column": r["fk_column"], "match_rate": r["match_rate"]} for r in fks_in],
                "fk_out": [{"to": r["target"], "column": r["fk_column"], "match_rate": r["match_rate"]} for r in fks_out],
            }

        # vis.js 노드/엣지 — 공정 단계별 색상.
        nodes_js = []
        for table, info in tables.items():
            color = _STAGE_COLORS.get(info["stage"], "#BDC3C7")
            label_cls = info["cls"] or table
            nodes_js.append({
                "id": table,
                "label": f"{info['ko']}\n{label_cls}\n({info['rows']:,} rows · {len(info['columns'])} cols)",
                "group": info["stage"],
                "color": {"background": color, "border": color,
                          "highlight": {"background": "#fff", "border": color}},
                "font": {"size": 12, "face": "sans-serif", "multi": True},
                "shape": "box", "margin": 12,
            })

        edges_js = []
        for rel in fk_relations:
            mr = rel["match_rate"]
            # 매칭률에 따른 엣지 색상: ≥95% 초록, ≥80% 주황, 그 외 빨강
            if mr is None:
                ecolor = "#999"
            elif mr >= 95:
                ecolor = "#2ECC71"
            elif mr >= 80:
                ecolor = "#E8A838"
            else:
                ecolor = "#E74C3C"
            elabel = rel["fk_column"] + (f"\n{mr}%" if mr is not None else "")
            edges_js.append({
                "from": rel["source"], "to": rel["target"],
                "label": elabel, "arrows": "to",
                "font": {"size": 10, "color": "#555", "multi": True},
                "color": {"color": ecolor, "highlight": "#1a1a2e"},
                "width": 2,
                "smooth": {"type": "curvedCW", "roundness": 0.2},
            })

        # 공정 단계별 통계
        domain_stats = {}
        for t in tables.values():
            domain_stats[t["stage"]] = domain_stats.get(t["stage"], 0) + 1

        # 범례 정렬 순서 = 설정의 colors 선언 순서 (공정 흐름 순으로 적어둔다).
        # 코드에 단계명을 박으면 도메인이 바뀔 때 정렬이 무너진다.
        _stage_order = {name: i for i, name in enumerate(_STAGE_COLORS)}
        legend_html = " ".join(
            f'<span style="display:inline-block;margin:3px 8px;padding:3px 10px;border-radius:4px;'
            f'background:{html.escape(_STAGE_COLORS.get(d, "#BDC3C7"))};color:white;font-size:12px;">'
            f'{html.escape(str(d))} ({c})</span>'
            for d, c in sorted(domain_stats.items(), key=lambda x: _stage_order.get(x[0], 9))
        )
        # FK 엣지 색상 범례 추가
        legend_html += (
            ' &nbsp;|&nbsp; <span style="font-size:11px;color:#666;">FK 매칭률:</span>'
            ' <span style="color:#2ECC71;font-weight:bold;">━ ≥95%</span>'
            ' <span style="color:#E8A838;font-weight:bold;">━ ≥80%</span>'
            ' <span style="color:#E74C3C;font-weight:bold;">━ &lt;80%</span>'
        )

        page_html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>CSV ERD — 테이블 관계도</title>
{VIS_NETWORK_SCRIPT_TAG}
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f5f5f5; }}
  .header {{ background: #1a1a2e; color: white; padding: 15px 25px; }}
  .header h1 {{ margin: 0 0 8px 0; font-size: 22px; }}
  .stats {{ display: flex; gap: 15px; flex-wrap: wrap; }}
  .stat {{ background: rgba(255,255,255,0.1); padding: 4px 12px; border-radius: 4px; font-size: 13px; }}
  .legend {{ padding: 8px 25px; background: #eee; border-bottom: 1px solid #ddd; }}
  .main {{ display: flex; height: calc(100vh - 120px); }}
  #erd {{ flex: 1; }}
  #detail {{ width: 440px; background: white; border-left: 2px solid #ddd; overflow-y: auto;
             padding: 0; display: none; }}
  #detail.active {{ display: block; }}
  .detail-header {{ padding: 15px 18px; border-bottom: 1px solid #eee; }}
  .detail-header h2 {{ margin: 0 0 4px 0; font-size: 18px; }}
  .detail-header .subtitle {{ color: #666; font-size: 13px; }}
  .detail-header .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
                           font-size: 11px; color: white; margin-right: 5px; margin-top: 4px; }}
  .detail-section {{ padding: 12px 18px; border-bottom: 1px solid #f0f0f0; }}
  .detail-section h3 {{ margin: 0 0 8px 0; font-size: 14px; color: #333; }}
  .col-table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; }}
  .col-table th {{ text-align: left; padding: 5px 6px; background: #f8f8f8; border-bottom: 1px solid #eee;
                   font-weight: 600; color: #555; position: sticky; top: 0; }}
  .col-table td {{ padding: 4px 6px; border-bottom: 1px solid #f5f5f5; vertical-align: top; }}
  .col-table tr:hover {{ background: #f0f7ff; }}
  .role-pk {{ color: #E74C3C; font-weight: bold; }}
  .role-fk {{ color: #3498DB; font-weight: bold; }}
  .sample {{ color: #999; font-size: 11px; font-style: italic; }}
  .nullbar {{ display: inline-block; height: 8px; border-radius: 2px; background: #E74C3C; vertical-align: middle; }}
  .nulltxt {{ font-size: 10px; color: #999; margin-left: 4px; }}
  .fk-list {{ list-style: none; padding: 0; margin: 0; }}
  .fk-list li {{ padding: 4px 0; font-size: 12px; }}
  .fk-list .arrow {{ color: #3498DB; }}
  .mr {{ font-size: 10px; padding: 1px 6px; border-radius: 8px; color: white; margin-left: 4px; }}
  .colfilter {{ width: 100%; padding: 5px 8px; margin-bottom: 8px; border: 1px solid #ddd;
                border-radius: 4px; font-size: 12px; }}
  .hint {{ text-align: center; padding: 40px 20px; color: #999; font-size: 14px; }}
</style>
</head>
<body>
<div class="header">
  <h1>CSV ERD — 원천 테이블 관계도</h1>
  <div class="stats">
    <span class="stat">테이블 {len(tables)}개</span>
    <span class="stat">FK 관계 {len(fk_relations)}개</span>
    <span class="stat">총 행 {sum(t['rows'] for t in tables.values()):,}개</span>
    <span class="stat">총 컬럼 {sum(len(t['columns']) for t in tables.values()):,}개</span>
    <span class="stat">PK {sum(1 for t in tables.values() for c in t['cols_detail'] if c['role'] == 'PK')}개 · FK {sum(1 for t in tables.values() for c in t['cols_detail'] if c['role'] == 'FK')}개</span>
  </div>
</div>
<div class="legend">{legend_html}</div>
<div class="main">
  <div id="erd"></div>
  <div id="detail">
    <div class="hint">테이블을 클릭하면<br>상세 정보가 표시됩니다</div>
  </div>
</div>
<script>
var tableDetails = {json_for_script(table_details_js)};

var nodes = new vis.DataSet({json_for_script(nodes_js)});
var edges = new vis.DataSet({json_for_script(edges_js)});
var container = document.getElementById('erd');
var data = {{ nodes: nodes, edges: edges }};
var options = {{
  physics: {{
    enabled: true,
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {{ gravitationalConstant: -50, centralGravity: 0.005, springLength: 200, springConstant: 0.08 }},
    stabilization: {{ iterations: 200 }}
  }},
  interaction: {{ hover: true, tooltipDelay: 200, navigationButtons: true, keyboard: true }},
  layout: {{ improvedLayout: true }}
}};
var network = new vis.Network(container, data, options);

var stageColors = {json_for_script(_STAGE_COLORS)};

{ESCAPE_HTML_JS}

function mrBadge(mr) {{
  if (mr === null || mr === undefined) return '';
  var bg = mr >= 95 ? '#2ECC71' : mr >= 80 ? '#E8A838' : '#E74C3C';
  return '<span class="mr" style="background:' + bg + '">' + escapeHtml(mr) + '%</span>';
}}

function renderCols(d, filter) {{
  var colRows = '';
  d.columns.forEach(function(c) {{
    if (filter && (c.name + ' ' + c.ko).toLowerCase().indexOf(filter) < 0) return;
    var roleClass = c.role === 'PK' ? 'role-pk' : c.role === 'FK' ? 'role-fk' : '';
    var roleLabel = c.role === 'PK' ? '<span class="role-pk">PK</span>' :
                    c.role === 'FK' ? '<span class="role-fk">FK</span>' : '';
    var sampleStr = c.samples && c.samples.length > 0
      ? '<span class="sample">' + c.samples.slice(0,2).map(escapeHtml).join(', ') + '</span>' : '';
    // NULL-rate mini bar (width up to 40px)
    var nr = (c.null_rate || 0);
    var bar = '<span class="nullbar" style="width:' + Math.max(1, Math.round(nr * 0.4)) + 'px;opacity:' +
              (nr > 0 ? 1 : 0.15) + '"></span><span class="nulltxt">' + escapeHtml(nr) + '%</span>';
    var uniq = escapeHtml((c.unique || 0).toLocaleString());
    colRows += '<tr><td>' + roleLabel + '</td><td class="' + roleClass + '">' + escapeHtml(c.name) +
               '</td><td>' + escapeHtml(c.ko) + '</td><td>' + escapeHtml(c.type) + '</td><td>' + bar +
               '</td><td>' + uniq + '</td><td>' + sampleStr + '</td></tr>';
  }});
  return colRows;
}}

function showDetail(tableId) {{
  var d = tableDetails[tableId];
  if (!d) return;
  var panel = document.getElementById('detail');
  panel.classList.add('active');

  var color = stageColors[d.stage] || '#BDC3C7';

  var fkOutHtml = '';
  d.fk_out.forEach(function(f) {{
    var targetKo = tableDetails[f.to] ? tableDetails[f.to].ko : f.to;
    fkOutHtml += '<li><span class="arrow">&rarr;</span> <b>' + escapeHtml(f.column) + '</b> &rarr; ' +
                 escapeHtml(targetKo) + mrBadge(f.match_rate) + '</li>';
  }});

  var fkInHtml = '';
  d.fk_in.forEach(function(f) {{
    var srcKo = tableDetails[f.from] ? tableDetails[f.from].ko : f.from;
    fkInHtml += '<li><span class="arrow">&larr;</span> ' + escapeHtml(srcKo) + ' &rarr; <b>' + escapeHtml(f.column) +
                '</b>' + mrBadge(f.match_rate) + '</li>';
  }});

  panel.innerHTML =
    '<div class="detail-header">' +
    '  <h2>' + escapeHtml(d.ko) + '</h2>' +
    '  <div class="subtitle">' + escapeHtml(tableId) + '</div>' +
    '  <div class="subtitle" style="color:#1a1a2e;font-weight:600;">→ 온톨로지 클래스: ' + escapeHtml(d.cls || '(미매핑)') + '</div>' +
    '  <span class="badge" style="background:' + escapeHtml(color) + '">' + escapeHtml(d.stage) + '</span>' +
    '  <span class="badge" style="background:#555">' + escapeHtml(d.rows.toLocaleString()) + ' rows</span>' +
    '  <span class="badge" style="background:#555">' + d.columns.length + ' cols</span>' +
    '</div>' +
    '<div class="detail-section">' +
    '  <h3>컬럼 상세 (' + d.columns.length + ')</h3>' +
    '  <input class="colfilter" placeholder="컬럼 검색…">' +
    '  <div style="max-height:48vh;overflow-y:auto;">' +
    '  <table class="col-table" id="coltbl">' +
    '    <tr><th></th><th>컬럼명</th><th>한글명</th><th>타입</th><th>NULL</th><th>고유값</th><th>샘플</th></tr>' +
    renderCols(d, '') +
    '  </table>' +
    '  </div>' +
    '</div>' +
    (fkOutHtml || fkInHtml ? '<div class="detail-section">' +
    '  <h3>관계 (FK · 데이터 매칭률)</h3>' +
    (fkOutHtml ? '  <div style="margin-bottom:8px;font-size:12px;color:#666;">나가는 관계 (이 테이블 → 부모):</div><ul class="fk-list">' + fkOutHtml + '</ul>' : '') +
    (fkInHtml ? '  <div style="margin-bottom:8px;font-size:12px;color:#666;margin-top:10px;">들어오는 관계 (자식 → 이 테이블):</div><ul class="fk-list">' + fkInHtml + '</ul>' : '') +
    '</div>' : '');

  // 테이블 ID 는 인라인 핸들러 문자열에 넣지 않고 클로저로 넘긴다
  var filterInput = panel.querySelector('.colfilter');
  if (filterInput) filterInput.addEventListener('input', function() {{ filterCols(tableId, this.value); }});
}}

function filterCols(tableId, val) {{
  var d = tableDetails[tableId];
  if (!d) return;
  var tbl = document.getElementById('coltbl');
  if (!tbl) return;
  tbl.innerHTML = '<tr><th></th><th>컬럼명</th><th>한글명</th><th>타입</th><th>NULL</th><th>고유값</th><th>샘플</th></tr>' +
                  renderCols(d, (val || '').toLowerCase());
}}

network.on("selectNode", function(params) {{
  showDetail(params.nodes[0]);
  var connEdges = network.getConnectedEdges(params.nodes[0]);
  network.selectEdges(connEdges);
}});

network.on("deselectNode", function() {{
  var panel = document.getElementById('detail');
  panel.innerHTML = '<div class="hint">테이블을 클릭하면<br>상세 정보가 표시됩니다</div>';
}});
</script>
</body>
</html>"""

        # 생성 산출물은 중앙 경계 해석기를 거쳐 data/generated 아래에만 쓴다.
        report_path = resolve_generated_path("reports/csv_erd.html")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(page_html)

        if open_report:
            from tools.common import path_to_file_uri
            webbrowser.open(path_to_file_uri(report_path))

        return json.dumps({
            "success": True,
            "path": str(report_path),
            "tables": len(tables),
            "total_rows": sum(t["rows"] for t in tables.values()),
            "total_columns": sum(len(t["columns"]) for t in tables.values()),
            "fk_relations": len(fk_relations),
            "fk_match_rates": [
                {"from": r["source"], "col": r["fk_column"],
                 "to": r["target"], "match_rate": r["match_rate"]}
                for r in fk_relations
            ],
            "stages": domain_stats,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def generate_table_class_map_report(open_report: bool = False) -> str:
    """원천 CSV → 온톨로지 클래스 매핑을 HTML 보고서로 생성한다.

    T-Box 생성 결과를 검토할 때 "내 테이블이 어느 클래스가 됐는지"를 한 화면에서
    확인한다. ``rules/domain/table_class_mapping.json`` 의 원시 매핑에 없는 검토 정보를
    함께 싣는다:

    - 테이블별 컬럼/행 수, 사용된 기준키(PK), 생성된 인스턴스/속성/관계 수
    - **행 수 = 인스턴스 수 일치 여부** (적재 누락 검출)
    - 동명 컬럼 비율 (같은 이름이 여러 테이블에 있어 값이 다름)
    - 원천 테이블 없이 만들어진 클래스 (코드 마스터 승격 / 추상 상위)

    Args:
        open_report: True면 HTML을 브라우저에서 자동 오픈.
    """
    import importlib.util
    import webbrowser

    try:
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "build_table_class_map_report.py",
        )
        spec = importlib.util.spec_from_file_location("_tcmap", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        data = module.collect()
        report_path = resolve_generated_path("reports/table_class_map.html")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(module.render(data))

        if open_report:
            from tools.common import path_to_file_uri
            webbrowser.open(path_to_file_uri(report_path))

        mismatched = [
            r["base"] for r in data["rows"] if r["rows"] != r["instances"]
        ]
        return json.dumps({
            "success": True,
            "path": str(report_path),
            "tables_mapped": len(data["rows"]),
            "source_columns": data["total_columns"],
            "source_rows": data["total_rows"],
            "instances_created": data["total_instances"],
            "class_links": data["total_links"],
            "row_instance_mismatch": mismatched,
            "shared_column_names": data["shared_columns"],
            "classes_without_source": {
                "code_master_promoted": len(data["derived"]),
                "abstract_parent": len(data["abstract"]),
            },
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)
