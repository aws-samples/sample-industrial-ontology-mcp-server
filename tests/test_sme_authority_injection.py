"""SME 문서 내용이 시맨틱 딕셔너리에 실리는지 고정하는 회귀 가드.

**배경 (감사 2026-07-28)**: SME 가 준 문서(SME 제공 업무사전 / 항목사전 / 코드체계 문서
MD 6개)의 상당 부분이 딕셔너리에 반영돼 있지 않았다. 컬럼↔DP 매핑은 양호했지만:

- 판정 규칙 24개 중 20개가 **텍스트로도 없었다**. 계상일 경계(교대 기준 시각) 를
  모르면 일자별 집계 전부가 어긋나고, '처리 방법'(구분 A/B) 을 모르면 코드값만
  보고는 구분할 수 없다.
- 표준항목 권위 정의문이 1,534 DP 에서 소실됐다 — LLM 이 **라벨** 을 풀어쓴 한 줄이
  ``description`` 이 되고, 공식·조건을 담은 ``STD_DEFINITION_COL`` 는 버려졌다.
  ``UNIT_WGT_COL`` 은 Scarfing재는 실측·Non-Scarfing재는 이론중량이라는 이중 의미가 있는데
  "공정 진행 슬래브 중량(kg)이다." 로는 알 수 없다.
- 복합코드 자릿수 구조가 없어 ``SUBSTR`` 기반 SME 판정식이 전부 무의미했다.
- ``column_to_classes`` 가 다중소유 컬럼만 남겨 ``OUTPUT_WGT_COL``(처리량 필수 제외 필터)
  같은 단일소유 핵심 컬럼이 역색인에서 조회 불가였다.

이 테스트는 **주입 경로가 살아 있는지** 만 검증한다 (실데이터 유무와 무관하게
동작하도록 픽스처를 쓴다). 규칙 파일의 내용 자체는 SME 소관이다.
"""
from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from domain.rules_paths import rules_path
from tools import semantic_dictionary as sd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clear_rule_caches():
    """모듈 레벨 캐시를 비워 테스트 간 규칙 파일 주입이 섞이지 않게 한다."""
    sd._code_meanings_cache = None
    sd._canonical_hints_cache = None
    sd._std_item_cache = None
    sd._sme_rules_cache = None
    yield
    sd._code_meanings_cache = None
    sd._canonical_hints_cache = None
    sd._std_item_cache = None
    sd._sme_rules_cache = None


def _classes(spec: dict) -> dict:
    return {
        cls: {"datatype_properties": {
            dp: {"source_columns": cols} for dp, cols in dps.items()
        }}
        for cls, dps in spec.items()
    }


# ── column_to_classes: 단일소유 컬럼도 등재 ────────────────────────────


def test_column_index_includes_single_owner_columns():
    """단일 클래스만 가진 컬럼도 역색인에 있어야 한다.

    이전 구현은 "선택 고민이 없다" 는 이유로 다중소유만 남겨, 처리량 집계의 필수
    제외 필터 컬럼(단일소유) 이 조회 불가였다.
    """
    classes = _classes({
        "Result": {"resultCoilWeight": ["OUTPUT_QTY_COL"]},          # 단일소유
        "A": {"aSharedCode": ["SHARED"]},
        "B": {"bSharedCode": ["SHARED"]},                      # 다중소유
    })

    index = sd._build_column_to_classes_index(classes)

    assert index["OUTPUT_QTY_COL"] == ["Result.resultCoilWeight"]
    assert len(index["SHARED"]) == 2


def test_column_index_marks_empty_dps():
    """값이 없는 DP 는 표시해 질의에 쓰지 않게 한다."""
    classes = {"C": {"datatype_properties": {
        "cEmpty": {"source_columns": ["COL"], "is_populated": False},
    }}}

    assert sd._build_column_to_classes_index(classes)["COL"] == ["C.cEmpty (∅비어있음)"]


# ── 표준항목 권위 메타데이터 주입 ──────────────────────────────────────


def test_std_item_authority_attached_to_entry():
    """정의문·관리방법·단위·자릿수구조가 DP entry 에 붙는다."""
    std = {
        "items": {"WGT_COL": {
            "definition": "두께 × 폭 × 길이 × 비중. Scarfing재는 실측, Non-Scarfing재는 이론중량.",
            "management": "전단완료 시점에 조업DB 에 Set 한다.",
            "unit": "kg",
            "unit_class": "Mass",
        }},
        "code_structure": {"WGT_COL": {
            "total_length": 4,
            "elements": [{"seq": 1, "position": 1, "length": 4, "name": "중량"}],
        }},
    }
    entry: dict = {"description": "중량(kg)을 나타낸다."}

    sd._attach_std_item_authority(entry, std, "WGT_COL")

    assert "Scarfing재는 실측" in entry["sme_definition"]
    assert entry["derivation_note"].startswith("전단완료")
    assert entry["unit"] == "kg"
    assert entry["unit_class"] == "Mass"
    assert entry["code_structure"]["total_length"] == 4
    # 기존 description 은 덮어쓰지 않는다 — 권위 정의문은 별도 필드다.
    assert entry["description"] == "중량(kg)을 나타낸다."


def test_std_item_authority_noop_when_absent():
    """권위 데이터가 없으면 아무 필드도 추가하지 않는다 (도메인-중립)."""
    entry: dict = {"label_ko": "x"}
    sd._attach_std_item_authority(entry, {"items": {}, "code_structure": {}}, "ANY")
    assert entry == {"label_ko": "x"}


def test_numbered_column_inherits_base_definition():
    """번호 접미 컬럼은 base 항목의 정의를 물려받고, 출처를 표시한다.

    Oracle 스키마는 한 표준항목을 열거형 컬럼으로 펼치는데 (``EXTRA_LEAD_DAYS1..10``)
    사전은 번호 없는 base 만 등록한다. 2026-07-29 실측: 미정의 680 컬럼 중 **588개가
    이 경우** — 권위 정의문이 있는데 exact 매칭으로만 찾아서 못 붙였다.
    """
    std = {
        "items": {"EXTRA_LEAD_DAYS": {
            "definition": "표준 기간 외 추가 소요 일수를 관리하는 항목",
            "unit": "day",
        }},
        "code_structure": {},
    }
    entry: dict = {}

    sd._attach_std_item_authority(entry, std, "EXTRA_LEAD_DAYS1")

    assert entry["sme_definition"].startswith("표준 기간")
    assert entry["unit"] == "day"
    # base 유래임을 밝혀야 한다 — 그 인덱스만의 정의로 오독하면 안 된다.
    assert entry["_definition_from_base"] == "EXTRA_LEAD_DAYS"


def test_exact_match_wins_over_base():
    """컬럼 자체가 등록돼 있으면 base 를 보지 않는다 (출처 표시도 없음)."""
    std = {
        "items": {
            "COL1": {"definition": "1번 전용 정의"},
            "COL": {"definition": "base 정의"},
        },
        "code_structure": {},
    }
    entry: dict = {}
    sd._attach_std_item_authority(entry, std, "COL1")
    assert entry["sme_definition"] == "1번 전용 정의"
    assert "_definition_from_base" not in entry


def test_base_fallback_requires_registered_base():
    """base 를 임의 추측하지 않는다 — 사전에 없으면 아무것도 붙이지 않는다."""
    std = {"items": {"OTHER": {"definition": "무관"}}, "code_structure": {}}
    entry: dict = {}
    sd._attach_std_item_authority(entry, std, "UNKNOWN_COL7")
    assert entry == {}
    assert sd._base_column_code("UNKNOWN_COL7", std["items"]) is None


def test_unnumbered_column_has_no_base():
    """번호 접미가 없으면 base 추론 자체를 하지 않는다."""
    std_items = {"SLAB": {"definition": "x"}}
    assert sd._base_column_code("UNIT_WGT_COL", std_items) is None


def test_ambiguous_column_contributes_nothing():
    """한 컬럼코드를 서로 다른 표준항목이 공유하면 정의문을 붙이지 않는다.

    추출 단계가 그런 코드를 ``items`` 에서 제외하므로 주입도 일어나지 않아야 한다 —
    틀린 권위 정의문은 없는 것보다 나쁘다 (독자가 의심할 근거가 없다).
    """
    entry: dict = {}
    sd._attach_std_item_authority(
        entry, {"items": {}, "code_structure": {}}, "PARTY_NAME_COL",
    )
    assert "sme_definition" not in entry


# ── SME 업무판정 규칙 섹션 ────────────────────────────────────────────


def test_business_rules_resolves_columns_to_current_dps(tmp_path, monkeypatch):
    """규칙의 컬럼 참조가 현재 DP 이름으로 재해석된다."""
    rules = {
        "derived_measures": {
            "처리량": {
                "rule": "SPREAD_QTY_COL 합, 단 OUTPUT_QTY_COL != 0",
                "columns": ["SPREAD_QTY_COL", "OUTPUT_QTY_COL"],
                "kg_feasible": True,
            },
        },
    }
    path = tmp_path / "sme_business_rules.json"
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(path))
    monkeypatch.setattr(sd, "_USER_TERM_DICT_PATH", str(tmp_path / "none.json"))

    classes = _classes({"Result": {
        "resultMaterialSpreadWeight": ["SPREAD_QTY_COL"],
        "resultCoilWeight": ["OUTPUT_QTY_COL"],
    }})
    index = sd._build_column_to_classes_index(classes)

    section = sd._build_business_rules(classes, index)
    measure = section["derived_measures"]["처리량"]

    assert measure["_resolution"] == "ok"
    assert measure["resolved_properties"]["OUTPUT_QTY_COL"] == ["Result.resultCoilWeight"]
    assert "_resolution_warning" not in measure


def test_business_rules_flags_columns_absent_from_kg(tmp_path, monkeypatch):
    """원본 컬럼이 KG 에 없는 규칙은 absent 로 드러낸다.

    엔진이 "답할 수 없음" 을 알아야 대체 컬럼으로 근사하지 않는다.
    """
    rules = {
        "derived_measures": {
            "재공": {
                "rule": "STATUS 앞2자리로 판정",
                "columns": ["STATUS_CD", "REAL_WEIGHT"],
                "kg_feasible": False,
            },
        },
    }
    path = tmp_path / "sme_business_rules.json"
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(path))
    monkeypatch.setattr(sd, "_USER_TERM_DICT_PATH", str(tmp_path / "none.json"))

    classes = _classes({"Result": {"resultCoilWeight": ["OUTPUT_QTY_COL"]}})
    index = sd._build_column_to_classes_index(classes)

    measure = sd._build_business_rules(classes, index)["derived_measures"]["재공"]
    assert measure["_resolution"] == "absent"
    assert "resolved_properties" not in measure


def test_business_rules_warns_on_feasibility_mismatch(tmp_path, monkeypatch):
    """kg_feasible=true 선언과 실측 재해석이 어긋나면 경고를 남긴다."""
    rules = {
        "derived_measures": {
            "혼합": {"columns": ["OUTPUT_QTY_COL", "GONE_COL"], "kg_feasible": True},
        },
    }
    path = tmp_path / "sme_business_rules.json"
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(path))
    monkeypatch.setattr(sd, "_USER_TERM_DICT_PATH", str(tmp_path / "none.json"))

    classes = _classes({"Result": {"resultCoilWeight": ["OUTPUT_QTY_COL"]}})
    index = sd._build_column_to_classes_index(classes)

    measure = sd._build_business_rules(classes, index)["derived_measures"]["혼합"]
    assert measure["_resolution"] == "partial"
    assert "_resolution_warning" in measure


def test_business_rules_flags_column_resolved_but_empty(tmp_path, monkeypatch):
    """DP 는 존재하나 A-Box 가 전부 비어 있으면 ok 로 보고하지 않는다.

    SME 의 '매수'(MATERIAL_NO 개수) 가 실제로 이 경우다 — 세 클래스가 선언하는데 전부
    비어 있어, COUNT 하면 에러 없이 0건이 나온다. ok 로 표시하면 "실행 가능한
    규칙" 처럼 보여 0건을 '해당 없음' 으로 오독하게 된다.
    """
    rules = {
        "derived_measures": {
            "매수": {"columns": ["MATERIAL_NO"], "kg_feasible": True},
        },
    }
    path = tmp_path / "sme_business_rules.json"
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(path))
    monkeypatch.setattr(sd, "_USER_TERM_DICT_PATH", str(tmp_path / "none.json"))

    classes = {"C": {"datatype_properties": {
        "cMtlNo": {"source_columns": ["MATERIAL_NO"], "is_populated": False},
    }}}
    index = sd._build_column_to_classes_index(classes)

    measure = sd._build_business_rules(classes, index)["derived_measures"]["매수"]
    assert measure["_resolution"] == "absent"
    assert measure["_empty_in_abox"]["columns"] == ["MATERIAL_NO"]
    assert "_resolution_warning" in measure


def test_partial_when_one_column_populated_and_one_empty(tmp_path, monkeypatch):
    """일부만 채워져 있으면 partial — 어느 컬럼이 빈지 함께 알려준다."""
    rules = {
        "derived_measures": {
            "혼합": {"columns": ["GOOD", "EMPTY"], "kg_feasible": True},
        },
    }
    path = tmp_path / "sme_business_rules.json"
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(path))
    monkeypatch.setattr(sd, "_USER_TERM_DICT_PATH", str(tmp_path / "none.json"))

    classes = {"C": {"datatype_properties": {
        "cGood": {"source_columns": ["GOOD"], "is_populated": True},
        "cEmpty": {"source_columns": ["EMPTY"], "is_populated": False},
    }}}
    index = sd._build_column_to_classes_index(classes)

    measure = sd._build_business_rules(classes, index)["derived_measures"]["혼합"]
    assert measure["_resolution"] == "partial"
    assert measure["_empty_in_abox"]["columns"] == ["EMPTY"]
    assert measure["resolved_properties"]["GOOD"] == ["C.cGood"]


def test_business_rules_empty_without_rule_files(tmp_path, monkeypatch):
    """규칙 파일이 없는 도메인에서는 섹션이 생기지 않는다."""
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(tmp_path / "a.json"))
    monkeypatch.setattr(sd, "_USER_TERM_DICT_PATH", str(tmp_path / "b.json"))
    assert sd._build_business_rules({}, {}) == {}


# ── 구조적으로 xlsx 가 줄 수 없는 코드값의 SME 문서 보강 ──────────────


def test_sme_code_meanings_merge_and_override(tmp_path, monkeypatch):
    """SME 문서의 코드값이 병합되고, 충돌 시 SME 가 정본이다.

    복합/참조 코드는 코드 설명 시트에 자기 값 행이 없어 결정적 추출로는 못 얻는다.
    또 SME 문서 간 표기가 충돌할 때(FAC_A1_OLD vs FAC_A1) 검증된 쪽을 써야 한다.
    """
    extract = {"columns": {"FAC_CD": {"FAC_A1_OLD": "1차 압연공장(구표기)"}}}
    extract_path = tmp_path / "code_meanings.json"
    extract_path.write_text(json.dumps(extract, ensure_ascii=False), encoding="utf-8")

    sme = {"code_meanings": {
        "FAC_CD": {"_source": "sme-doc-v2", "codes": {"FAC_A1": "1차 압연공장"}},
        "ROLL_UNIT": {"codes": {"C": "modeC", "E": "modeE"}},
    }}
    sme_path = tmp_path / "sme_business_rules.json"
    sme_path.write_text(json.dumps(sme, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(sd, "_CODE_MEANINGS_PATH", str(extract_path))
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(sme_path))
    monkeypatch.setattr(sd, "_CANONICAL_HINTS_PATH", str(tmp_path / "none.json"))

    columns = sd._load_code_meanings()["columns"]

    # 추출물에만 있던 코드는 보존, SME 코드가 추가된다.
    assert columns["FAC_CD"] == {"FAC_A1_OLD": "1차 압연공장(구표기)", "FAC_A1": "1차 압연공장"}
    # 추출물이 전혀 모르는 코드체계도 실린다.
    assert columns["ROLL_UNIT"]["C"] == "modeC"


def test_sme_business_groups_merge(tmp_path, monkeypatch):
    """SME 롤업(그룹 A/B 등)이 business_groups 로 병합된다."""
    sme = {"business_groups": {
        "HANDLE_TP": {"groupB": ["7", "8"], "groupA": ["3", "4"]},
    }}
    sme_path = tmp_path / "sme_business_rules.json"
    sme_path.write_text(json.dumps(sme, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(sme_path))
    monkeypatch.setattr(sd, "_CODE_MEANINGS_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(sd, "_CANONICAL_HINTS_PATH", str(tmp_path / "none.json"))

    groups = sd._load_code_meanings()["business_groups"]
    assert groups["HANDLE_TP"]["groupA"] == ["3", "4"]


def test_meta_keys_excluded_from_payload(tmp_path, monkeypatch):
    """``_`` 로 시작하는 주석 키는 페이로드로 새지 않는다."""
    sme = {"code_meanings": {
        "_comment": "설명문",
        "COL": {"codes": {"A": "가"}},
    }}
    sme_path = tmp_path / "sme_business_rules.json"
    sme_path.write_text(json.dumps(sme, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(sme_path))
    monkeypatch.setattr(sd, "_CODE_MEANINGS_PATH", str(tmp_path / "none.json"))
    monkeypatch.setattr(sd, "_CANONICAL_HINTS_PATH", str(tmp_path / "none.json"))

    assert "_COMMENT" not in sd._load_code_meanings()["columns"]


def test_failed_read_is_not_cached(tmp_path, monkeypatch):
    """읽기 실패를 캐시하면 프로세스 내내 규칙이 사라진다 — 캐시하지 않아야 한다.

    실제로 발생한 버그: 생성기 자체 유닛테스트가 ``builtins.open`` 을 패치하므로
    그 시점의 규칙 읽기가 ``{}`` 를 반환하고, **같은 프로세스의 이후 실제 딕셔너리
    생성이 그 빈 값을 재사용** 해 business_rules 섹션이 반쪽만 나왔다
    (derived_measures 누락, user_term_to_property 만 남음).
    """
    rules = {"derived_measures": {"m": {"columns": ["C"], "kg_feasible": True}}}
    path = tmp_path / "sme_business_rules.json"
    path.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(path))

    with mock.patch("builtins.open", create=True) as mocked:
        handle = mock.MagicMock()
        mocked.return_value.__enter__ = lambda _self: handle
        mocked.return_value.__exit__ = mock.MagicMock(return_value=False)
        assert sd._load_sme_business_rules() == {}  # 실패해도 예외는 안 난다

    # 모킹이 풀린 뒤에는 실제 내용이 읽혀야 한다.
    assert sd._load_sme_business_rules()["derived_measures"]["m"]["kg_feasible"]


def test_successful_read_is_cached(tmp_path, monkeypatch):
    """정상 로드는 캐시한다 — 파일이 사라져도 같은 값을 준다."""
    path = tmp_path / "sme_business_rules.json"
    path.write_text(json.dumps({"derived_measures": {"m": {}}}), encoding="utf-8")
    monkeypatch.setattr(sd, "_SME_BUSINESS_RULES_PATH", str(path))

    assert "derived_measures" in sd._load_sme_business_rules()
    path.unlink()
    assert "derived_measures" in sd._load_sme_business_rules()


# ── 실제 도메인 규칙 파일이 있을 때의 정합성 (선택적) ──────────────────


@pytest.mark.skipif(
    not os.path.exists(rules_path("std_item_metadata.json")),
    reason="std_item_metadata.json 없음 (도메인 데이터 미주입 환경)",
)
def test_extracted_code_structure_has_no_position_collision():
    """자릿수 레이아웃에 위치 충돌이 없어야 한다.

    같은 position 을 두 구성요소가 주장하면 ``SUBSTR`` 독자가 어느 이름을 쓸지
    알 수 없다. 강종별 변종 레이아웃은 ``variants`` 로 분리해야 한다.
    """
    with open(
        rules_path("std_item_metadata.json"), encoding="utf-8",
    ) as f:
        structure = json.load(f).get("code_structure") or {}

    for code, spec in structure.items():
        layouts = (
            [spec] if "elements" in spec else list(spec.get("variants", {}).values())
        )
        for layout in layouts:
            positions = [e["position"] for e in layout["elements"]]
            assert len(positions) == len(set(positions)), (
                f"{code}: position 중복 {positions} — variants 분리 필요"
            )
