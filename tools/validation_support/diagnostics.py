"""검증 실패 진단 + provenance 역추적 유틸.

두 함수 모두 kg_validation.py 내부에서만 쓰이지만 그래프/T-Box에 의존하지
않아 순수 함수로 분리하기 적합.
"""
from __future__ import annotations

import json
import logging
import os

from config import GENERATED_ABOX_DIR

logger = logging.getLogger(__name__)


def diagnose_check_failure(check: dict) -> dict:
    """실패한 check에 원인 분류와 수정 제안을 첨부한다.

    Categories:
    - schema_gap: T-Box에 누락된 선언 (domain, range, inverseOf 등)
    - data_quality: CSV 원본 데이터 문제 (무효값, 중복, 누락)
    - fk_broken: FK 참조 대상 미존재
    - inference_noise: 추론 결과로 인한 위반
    """
    name = check.get("name", "")
    diagnosis: dict = {"category": "unknown", "fix_suggestion": "", "auto_fixable": False}

    if "양방향" in name or "inverseof" in name.lower():
        diagnosis.update(category="schema_gap",
                         fix_suggestion="improve_tbox_quality로 inverseOf 양방향 보장 실행",
                         auto_fixable=True)
    elif "고아" in name or "orphan" in name.lower():
        diagnosis.update(category="data_quality",
                         fix_suggestion="A-Box에서 고아 인스턴스의 FK 컬럼 확인")
    elif "댕글링" in name or "dangling" in name.lower():
        diagnosis.update(category="fk_broken",
                         fix_suggestion="FK 참조 대상 클래스가 A-Box에 존재하는지 확인")
    elif "domain" in name.lower() or "range" in name.lower():
        diagnosis.update(category="schema_gap",
                         fix_suggestion="T-Box에서 해당 프로퍼티의 domain/range 선언 확인",
                         auto_fixable=True)
    elif "커버리지" in name or "coverage" in name.lower():
        diagnosis.update(category="data_quality",
                         fix_suggestion="CSV 데이터에서 해당 프로퍼티 값이 비어있는 행 확인")
    elif "이상치" in name or "outlier" in name.lower():
        diagnosis.update(category="data_quality",
                         fix_suggestion="CSV에서 해당 값의 범위를 확인하고 rules/contracts/value_ranges.json 조정")
    elif "Fitness" in name:
        diagnosis.update(category="schema_gap",
                         fix_suggestion="T-Box 클래스와 CSV 테이블명 매핑 확인")
    elif "문자열" in name or "string" in name.lower():
        diagnosis.update(category="data_quality",
                         fix_suggestion="CSV에서 N/A, null 등 무효값 정리")
    elif "Functional" in name:
        diagnosis.update(category="data_quality",
                         fix_suggestion="PK 컬럼에 중복값이 있는지 CSV 확인")
    elif "카디널리티" in name or "cardinality" in name.lower():
        diagnosis.update(category="schema_gap",
                         fix_suggestion="T-Box Restriction의 min/maxCardinality 값 검토")
    elif "Disjoint" in name:
        diagnosis.update(category="inference_noise",
                         fix_suggestion="추론 후 AllDisjointClasses 위반 — T-Box 클래스 계층 재검토")
    elif "공정" in name or "process" in name.lower():
        diagnosis.update(category="data_quality",
                         fix_suggestion="data/source/tacit/ 폴더의 공정 흐름 TTL 확인")
    elif "FK" in name or "fk" in name.lower():
        diagnosis.update(category="fk_broken",
                         fix_suggestion="FK 패턴 파일 (rules/contracts/fk_patterns.json) 확인")
    elif "추론" in name or "inference" in name.lower():
        diagnosis.update(category="inference_noise",
                         fix_suggestion="run_owl_rl_inference 결과 확인 — 추론 전후 트리플 수 비교")
    elif "시간" in name or "temporal" in name.lower():
        diagnosis.update(category="data_quality",
                         fix_suggestion="CSV의 timestamp/datetime 컬럼 형식 확인 (xsd:dateTime)")
    elif "Relationship" in name:
        diagnosis.update(category="data_quality",
                         fix_suggestion="특정 클래스에 관계가 과도하게 집중되지 않았는지 확인")
    else:
        diagnosis.update(category="data_quality",
                         fix_suggestion=f"'{name}' 검증 실패 — 상세 위반 목록 참조")

    return diagnosis


def trace_violations_to_provenance(
    violations: list[dict],
    max_traces: int = 10,
    *,
    provenance_path: str | None = None,
) -> list[dict]:
    """검증 위반 인스턴스를 abox_provenance.json으로 역추적한다.

    위반 인스턴스 URI → 원본 CSV 테이블/행/컬럼까지 추적.

    Args:
        violations: 검증 결과의 violations/samples 목록.
        max_traces: 역추적할 최대 건수.
        provenance_path: abox_provenance.json 경로. None이면 config 기본값.
    """
    prov_path = provenance_path or os.path.join(
        GENERATED_ABOX_DIR, "abox_provenance.json",
    )
    if not os.path.exists(prov_path):
        return []

    try:
        with open(prov_path, encoding="utf-8") as f:
            provenance = json.load(f)
    except Exception:
        return []

    traces: list[dict] = []
    for v in violations[:max_traces]:
        inst_local = (
            v.get("subject") or v.get("entity") or v.get("instance") or v.get("object") or ""
        )
        if not inst_local:
            continue

        matched_prov = None
        for uri, prov in provenance.items():
            if uri.endswith(f"/{inst_local}") or uri.endswith(f"#{inst_local}"):
                matched_prov = prov
                break

        if matched_prov:
            traces.append({
                "instance": inst_local,
                "source_table": matched_prov.get("source_table", ""),
                "source_row": matched_prov.get("source_row", -1),
                "pk_column": matched_prov.get("pk_column", ""),
                "pk_value": matched_prov.get("pk_value", ""),
                "violation": v.get("issue") or v.get("type") or v.get("restriction") or "unknown",
            })

    return traces
