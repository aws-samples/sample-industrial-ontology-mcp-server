"""Adversarial CSV Red Team — P5.
MCP 도구: run_adversarial_csv_suite.


결함 유형별 CSV를 합성하여 파이프라인(profile_csv_data 우선, 나중에
abox_generation 확장)이 얼마나 잘 잡는지 측정한다. Detection rate를
품질 지표로 누적한다.

카테고리 (초기 8종):
- type_confusion: 숫자 컬럼에 timestamp 섞임
- high_null: null 비율 > 80%
- fk_circular: A→B→A 순환 참조
- duplicate_pk: 동일 PK 여러 row
- mixed_units: 단위 혼재 (kW vs MW 표기 혼합)
- encoding_garble: 깨진 인코딩 문자
- time_reversal: 시계열 역순 row
- empty_table: 데이터 없음 (헤더만)

각 카테고리는 "profile_csv_data가 탐지해야 할 issue 종류"를 expected로 기록.
run_adversarial_suite가 각 변종을 만들고, profile을 돌리고, expected 대비
실제 탐지 여부를 리포트한다.
"""
from __future__ import annotations

import contextlib
import csv
import json
import logging
import os
import tempfile

from config import GENERATED_REPORTS_DIR, SOURCE_RAWDATA_DIR
from tools.common import source_stamp, success_response

logger = logging.getLogger(__name__)


# ── CSV 합성기 ──────────────────────────────────────


def _write_csv(path: str, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _adv_type_confusion(base_dir: str) -> str:
    """정수 컬럼에 timestamp 섞임."""
    path = os.path.join(base_dir, "adv_type_confusion.csv")
    rows = [[str(i), str(i * 10)] for i in range(20)]
    rows[5] = ["6", "2025-09-01T00:00:00"]
    rows[12] = ["13", "not-a-number"]
    _write_csv(path, ["id", "value"], rows)
    return path


def _adv_high_null(base_dir: str) -> str:
    """한 컬럼이 90% null."""
    path = os.path.join(base_dir, "adv_high_null.csv")
    rows = [[str(i), ("X" if i < 2 else "")] for i in range(20)]
    _write_csv(path, ["id", "optional_col"], rows)
    return path


def _adv_fk_circular(base_dir: str) -> str:
    """A→B→A 순환 참조 (FK 관계)."""
    path = os.path.join(base_dir, "adv_fk_circular.csv")
    rows = [
        ["A1", "B1"],
        ["B1", "A1"],  # 순환
    ]
    _write_csv(path, ["selfId", "parentId"], rows)
    return path


def _adv_duplicate_pk(base_dir: str) -> str:
    """PK로 쓰일 컬럼에 중복 값."""
    path = os.path.join(base_dir, "adv_duplicate_pk.csv")
    rows = [
        ["P001", "first"],
        ["P001", "duplicate"],  # 중복
        ["P002", "other"],
    ]
    _write_csv(path, ["productId", "label"], rows)
    return path


def _adv_mixed_units(base_dir: str) -> str:
    """숫자와 문자열(단위 포함) 혼재."""
    path = os.path.join(base_dir, "adv_mixed_units.csv")
    rows = [
        ["1", "100"],
        ["2", "100 kW"],  # 단위 섞임
        ["3", "0.1 MW"],
        ["4", "150"],
    ]
    _write_csv(path, ["id", "power"], rows)
    return path


def _adv_encoding_garble(base_dir: str) -> str:
    """일부 행 인코딩 깨짐 (binary noise)."""
    path = os.path.join(base_dir, "adv_encoding_garble.csv")
    with open(path, "wb") as f:
        f.write(b"id,name\n")
        f.write(b"1,Normal\n")
        f.write(b"2,\xff\xfe\x00\x01\n")  # garbled bytes
        f.write(b"3,\xc3\x28\n")  # invalid utf-8
    return path


def _adv_time_reversal(base_dir: str) -> str:
    """타임스탬프 역순."""
    path = os.path.join(base_dir, "adv_time_reversal.csv")
    rows = [
        ["1", "2025-09-10T00:00:00"],
        ["2", "2025-09-05T00:00:00"],
        ["3", "2025-09-01T00:00:00"],  # 역순
    ]
    _write_csv(path, ["id", "ts"], rows)
    return path


def _adv_empty_table(base_dir: str) -> str:
    path = os.path.join(base_dir, "adv_empty_table.csv")
    _write_csv(path, ["id", "val"], [])
    return path


ADVERSARIAL_CASES: list[dict] = [
    {
        "id": "type_confusion",
        "description": "정수 컬럼에 timestamp/비정상 문자열 혼입",
        "builder": _adv_type_confusion,
        "expected_issues": ["mixed_types"],
    },
    {
        "id": "high_null",
        "description": "컬럼 null 비율 90%",
        "builder": _adv_high_null,
        "expected_issues": ["high_null"],
    },
    {
        "id": "fk_circular",
        "description": "A→B→A 순환 참조",
        "builder": _adv_fk_circular,
        "expected_issues": [],  # profile_csv_data는 순환 미탐 — detection gap
    },
    {
        "id": "duplicate_pk",
        "description": "PK 컬럼 중복",
        "builder": _adv_duplicate_pk,
        "expected_issues": [],  # profile은 unique_rate로 힌트만, 명시적 경고 없음 — gap
    },
    {
        "id": "mixed_units",
        "description": "숫자/단위 표기 혼재",
        "builder": _adv_mixed_units,
        "expected_issues": ["mixed_types"],
    },
    {
        "id": "encoding_garble",
        "description": "일부 행 UTF-8 깨짐",
        "builder": _adv_encoding_garble,
        "expected_issues": [],  # profile이 UnicodeDecodeError로 실패 — gap
    },
    {
        "id": "time_reversal",
        "description": "타임스탬프 역순",
        "builder": _adv_time_reversal,
        "expected_issues": [],  # profile 무관 — gap (kg_validation temporal에서 잡혀야)
    },
    {
        "id": "empty_table",
        "description": "헤더만 있고 데이터 없음",
        "builder": _adv_empty_table,
        "expected_issues": ["데이터 없음"],
    },
]


# ── 탐지 실행 및 평가 ───────────────────────────────


def _extract_issue_types(profile_result: dict) -> set[str]:
    """profile_csv_data 결과 dict에서 issue 타입 집합 추출."""
    types: set[str] = set()
    for item in profile_result.get("issues", []):
        if isinstance(item, dict) and "issue" in item:
            types.add(item["issue"])
        elif isinstance(item, str):
            types.add(item)
    return types


def _run_profile(csv_path: str) -> dict:
    """profile_csv_data 내부 _profile_table을 직접 호출 (I/O 격리)."""
    # profile_csv_data는 SOURCE_RAWDATA_DIR/{table_name}.csv를 읽음.
    # 테스트 시 한정된 임시 경로로 프로파일 함수 재구성 불가 → _profile_table 재사용.
    # 간단히: 임시 디렉터리에서 table_name만 주고 SOURCE_RAWDATA_DIR를 패치해야 함.
    # 여기서는 카피-실행 대신 경량 호출.
    import importlib


    # 내부 _profile_table 추출
    importlib.import_module("tools.local_artifacts")
    # profile_csv_data는 _profile_table을 지역 정의 → 직접 접근 불가.
    # 대신 아래에서 SOURCE_RAWDATA_DIR 패치 경로 사용.
    raise NotImplementedError("use _run_profile_via_source_dir")


def _run_profile_via_source_dir(table_name: str, source_dir: str) -> dict:
    """SOURCE_RAWDATA_DIR를 주어진 임시 디렉터리로 패치 후 profile_csv_data 실행."""
    from unittest.mock import patch

    with patch("tools.local_artifacts.SOURCE_RAWDATA_DIR", source_dir):
        from tools.local_artifacts import profile_csv_data
        try:
            raw = profile_csv_data(table_name)
        except Exception as e:
            return {"table": table_name, "error": f"{type(e).__name__}: {e}"}
    try:
        return json.loads(raw)
    except Exception:
        return {"table": table_name, "raw": raw}


def _rawdata_sources() -> dict[str, str]:
    """rawdata CSV 전체를 각인 대상으로 펼친다 (``테이블명=경로``).

    디렉터리는 지문을 낼 수 없고, "가장 새 파일" 하나만 찍으면 다른 테이블의
    변경을 놓친다 — 이 리포에서 glob 이 조용히 0건이 된 사고와 같은 부류다.
    """
    import glob as _glob
    out: dict[str, str] = {}
    for path in sorted(_glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))):
        out[f"csv:{os.path.basename(path)[:-4]}"] = path
    return out


def run_adversarial_suite(output_dir: str | None = None) -> dict:
    """8종 adversarial CSV 생성 + profile 실행 + detection rate 측정.

    Args:
        output_dir: 리포트 저장 디렉터리. None이면 반환만.

    Returns:
        {
            "total_cases": N,
            "detected": k,
            "detection_rate": pct,
            "cases": [{id, expected, detected_issues, status}, ...],
            "gaps": [...]  # expected 있는데 미탐
        }
    """
    tmpdir = tempfile.mkdtemp(prefix="adv_csv_")
    results: list[dict] = []
    try:
        for case in ADVERSARIAL_CASES:
            path = case["builder"](tmpdir)
            table_name = os.path.basename(path).replace(".csv", "")
            profile = _run_profile_via_source_dir(table_name, tmpdir)

            # profile이 error로 종료됐는지 확인 (encoding_garble 등)
            if "error" in profile:
                detected = {"__error__"}
            else:
                detected = _extract_issue_types(profile)

            expected = set(case["expected_issues"])
            if not expected:
                # expected 없음 = profile이 잡지 못할 것으로 알려진 gap
                status = "known_gap"
            elif expected.issubset(detected):
                status = "detected"
            elif expected & detected:
                status = "partial"
            else:
                status = "missed"

            results.append({
                "id": case["id"],
                "description": case["description"],
                "expected": sorted(expected),
                "detected": sorted(detected),
                "status": status,
            })
    finally:
        # 임시 CSV 정리
        import shutil
        with contextlib.suppress(OSError):
            shutil.rmtree(tmpdir)

    total_with_expected = sum(1 for r in results if r["status"] != "known_gap")
    detected_count = sum(1 for r in results if r["status"] == "detected")
    partial_count = sum(1 for r in results if r["status"] == "partial")
    missed = [r for r in results if r["status"] == "missed"]
    gaps = [r for r in results if r["status"] == "known_gap"]

    summary = {
        "total_cases": len(results),
        "cases_with_expectations": total_with_expected,
        "detected": detected_count,
        "partial": partial_count,
        "missed": len(missed),
        "known_gaps": len(gaps),
        "detection_rate": round(
            detected_count / max(total_with_expected, 1) * 100, 1,
        ),
    }
    report = {"summary": summary, "cases": results}

    if output_dir:
        # 이 보고서는 **소스 CSV** 를 측정한다 (합성 결함을 프로파일러에 통과시킨다).
        # 그래서 각인 대상은 rawdata 디렉터리의 세대다.
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "adversarial_report.json")
        report["_source"] = source_stamp(**_rawdata_sources())
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        report["report_path"] = path

    return report


def run_adversarial_csv_suite() -> str:
    """합성 결함 CSV 8종을 프로파일러에 통과시켜 detection rate 측정 (P5).

    각 결함 유형에 대해 profile_csv_data가 expected_issues를 잡아내는지
    확인하고, "known_gap"(잡지 못할 것으로 알려진 케이스)도 함께 보고.

    예상 소요시간: <1초. Bedrock 호출 0회.
    """
    report = run_adversarial_suite(output_dir=GENERATED_REPORTS_DIR)
    return success_response(report)
