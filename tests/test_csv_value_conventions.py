"""CSV 값 해석의 두 조용한 오류 — 모호한 날짜, 비-ASCII PK 충돌.

두 결함의 공통점: **에러도 경고도 없이 데이터가 틀어진다.** 파이프라인은 정상
완주하고 어떤 게이트도 발화하지 않는다.

## 1. 같은 컬럼을 값마다 다른 규칙으로 읽었다

``_DATE_INPUT_FORMATS`` 가 ``%d/%m/%Y`` 를 ``%m/%d/%Y`` 보다 먼저 시도해서, 첫
포맷이 성공하면 일/월, 실패하면 월/일로 읽혔다 (실측 2026-08-23)::

    '03/04/2025' → 2025-04-03    일/월 (첫 포맷 성공)
    '12/25/2025' → 2025-12-25    월/일 (25 는 월이 될 수 없어 폴백)

한 컬럼에 두 규칙이 적용되면 어느 해석을 고르든 절반이 틀린다. 그래서 순서를
**값으로 결정할 수 없을 때는 거부** 하고 (``None`` → ``type_coercion_failures`` 로
집계돼 loss manifest 에 드러난다), CSV 가 한 규칙을 따르면 ``CSV_DATE_ORDER`` 로
명시하게 했다. 조용한 오답보다 보이는 손실이 낫다.

**과잉 거부는 또 다른 손실이다.** ``15/01/2024`` 는 15 가 월이 될 수 없어 해석이
하나뿐이므로 설정 없이도 파싱해야 한다. 아래 테스트의 절반이 그 반대 방향 회귀를
막는다.

## 2. 서로 다른 행이 같은 IRI 로 뭉쳤다

PK 정규화 패턴 ``[^a-zA-Z0-9_-]`` 가 한글·한자·키릴을 전부 ``_`` 로 바꿨다::

    '철강판' → '___'      '구리선' → '___'      → MaterialMaster____ 하나

두 행의 속성이 한 인스턴스에 섞이고, ``duplicate_pk_rows`` 카운터에만 잡혀
"PK 가 실제로 중복인 데이터" 와 구별되지 않는다. IRI 는 RFC 3987 상 Unicode 를
허용하므로 문자는 보존하고 구두점만 치환한다.

``\\w`` 는 ASCII 영숫자·밑줄을 포함하므로 **ASCII 입력의 출력은 예전과 완전히
동일** 하다 — 기존 배포 IRI 가 바뀌지 않는 것이 요건이다 (실측: 배포 CSV 490,248
값 중 IRI 에 닿는 값의 변화 0건).
"""
from __future__ import annotations

import re

import pytest

from tools.abox_generation import (
    _convert_date,
    _convert_datetime,
    _detect_pk_value,
    _is_ambiguous_day_month,
    _pk_safe_local,
    _uri_safe_local,
)


@pytest.fixture(autouse=True)
def _no_ambient_date_order(monkeypatch):
    """환경에 남은 ``CSV_DATE_ORDER`` 가 테스트를 오염시키지 않게 한다."""
    monkeypatch.delenv("CSV_DATE_ORDER", raising=False)


# ──────────────────────────────────────────────────────────────────
# 1. 모호성 판정
# ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "03/04/2025", "12/11/2025", "01/02/2025", "1/2/2025",
    "03.04.2025", "03-04-2025",
])
def test_two_digits_under_13_are_ambiguous(value):
    assert _is_ambiguous_day_month(value) is True


@pytest.mark.parametrize("value", [
    "15/01/2024",      # 15 는 월이 될 수 없다
    "12/25/2025",      # 25 는 월이 될 수 없다
    "2025-03-04",      # ISO — 순서가 고정
    "20240115",        # 압축
    "",                # 빈 값
    "invalid",
    "00/04/2025",      # 0 은 유효한 월/일이 아니다
])
def test_unambiguous_values_are_not_flagged(value):
    assert _is_ambiguous_day_month(value) is False


# ──────────────────────────────────────────────────────────────────
# 2. 기본 동작 — 모호하면 거부, 아니면 파싱 (과잉 거부 방지가 주 방향)
# ──────────────────────────────────────────────────────────────────

def test_ambiguous_date_is_refused_not_guessed():
    """THE REGRESSION: 일/월을 못 정하면 값을 버린다."""
    assert _convert_datetime("03/04/2025") is None
    assert _convert_date("03/04/2025") is None


def test_the_two_values_that_disagreed_no_longer_disagree():
    """실측 재현 — 예전에는 이 둘이 서로 다른 규칙으로 읽혔다.

    이제 하나는 거부되고 하나는 유일한 해석으로 파싱된다. 중요한 것은 **한
    컬럼에 두 규칙이 동시에 적용되지 않는다** 는 점이다.
    """
    assert _convert_datetime("03/04/2025") is None          # 모호 → 거부
    assert _convert_datetime("12/25/2025") == "2025-12-25T00:00:00"  # 월/일뿐


@pytest.mark.parametrize(("value", "expected"), [
    ("2024-01-15", "2024-01-15T00:00:00"),
    ("2024-01-15T14:30:00", "2024-01-15T14:30:00"),
    ("2024-01-15T14:30:00Z", "2024-01-15T14:30:00"),
    ("2024/01/15", "2024-01-15T00:00:00"),
    ("2024.01.15 10:20:30", "2024-01-15T10:20:30"),
    ("20240115", "2024-01-15T00:00:00"),
    ("15-01-2024", "2024-01-15T00:00:00"),
    ("15/01/2024", "2024-01-15T00:00:00"),
    ("25.12.2025", "2025-12-25T00:00:00"),
])
def test_unambiguous_dates_still_parse(value, expected):
    """가드가 정당한 값을 막지 않는다 — 과잉 거부 방지 (주 방향)."""
    assert _convert_datetime(value) == expected


# ──────────────────────────────────────────────────────────────────
# 3. CSV_DATE_ORDER 명시
# ──────────────────────────────────────────────────────────────────

def test_dmy_order_parses_ambiguous_as_day_first(monkeypatch):
    monkeypatch.setenv("CSV_DATE_ORDER", "dmy")
    assert _convert_datetime("03/04/2025") == "2025-04-03T00:00:00"
    assert _convert_date("03/04/2025") == "2025-04-03"


def test_mdy_order_parses_ambiguous_as_month_first(monkeypatch):
    monkeypatch.setenv("CSV_DATE_ORDER", "mdy")
    assert _convert_datetime("03/04/2025") == "2025-03-04T00:00:00"
    assert _convert_date("03/04/2025") == "2025-03-04"


def test_mdy_order_rejects_day_first_only_value(monkeypatch):
    """``mdy`` 선언 하에 ``15/01`` 은 15 월이 없으므로 버린다 (조용한 뒤바뀜 방지)."""
    monkeypatch.setenv("CSV_DATE_ORDER", "mdy")
    assert _convert_datetime("15/01/2024") is None


def test_typo_in_order_is_treated_as_unset(monkeypatch):
    """오타는 추측을 켜지 않는다.

    ``CSV_DATE_ORDER=eu`` 에 기본 순서를 적용하면, 사용자는 자기가 지정한 규칙이
    쓰인다고 믿는데 실제로는 반대 규칙이 조용히 적용될 수 있다 — 이 기능이 막으려는
    바로 그 실패다.
    """
    monkeypatch.setenv("CSV_DATE_ORDER", "eu")
    assert _convert_datetime("03/04/2025") is None


def test_env_is_read_per_call_not_at_import(monkeypatch):
    """모듈 로드 시점에 고정되면 테스트·운영 모두 주입이 안 먹는다."""
    assert _convert_datetime("03/04/2025") is None
    monkeypatch.setenv("CSV_DATE_ORDER", "dmy")
    assert _convert_datetime("03/04/2025") == "2025-04-03T00:00:00"


# ──────────────────────────────────────────────────────────────────
# 4. 비-ASCII PK — 충돌 해소
# ──────────────────────────────────────────────────────────────────

def test_distinct_korean_pks_get_distinct_iris():
    """THE REGRESSION: 예전에는 셋 다 ``___`` 로 뭉쳤다."""
    locals_ = {_pk_safe_local(v) for v in ("철강판", "구리선", "알루미늄")}
    assert len(locals_) == 3, f"서로 다른 PK 가 같은 IRI 로 뭉쳤다: {locals_}"


def test_end_to_end_pk_detection_does_not_collide():
    """``_detect_pk_value`` → IRI local name 경로 전체를 확인한다."""
    seen: dict[str, list[str]] = {}
    for value in ("철강판", "구리선", "알루미늄"):
        pk = _detect_pk_value(
            {"Material_ID": value}, "MaterialMaster",
            pk_column="Material_ID", pk_is_authoritative=True,
        )
        iri = f"MaterialMaster_{_uri_safe_local(pk)}"
        seen.setdefault(iri, []).append(value)
    collisions = {k: v for k, v in seen.items() if len(v) > 1}
    assert not collisions, f"IRI 충돌: {collisions}"


@pytest.mark.parametrize("value", [
    "EQ001", "AM00001", "P-001", "abc_def", "2025-01-02T10:00:00",
    "EQ.001", "EQ 001", "a/b", "12345",
])
def test_ascii_output_is_byte_identical_to_previous_behaviour(value):
    """ASCII 입력의 출력이 예전과 완전히 같다 — 기존 배포 IRI 불변이 요건.

    이 단정이 깨지면 철강 배포의 인스턴스 IRI 가 바뀌어, 저장된 SPARQL 과
    tacit TTL 의 참조가 한꺼번에 끊긴다.
    """
    previous = re.sub(r"[^a-zA-Z0-9_-]", "_", value)
    assert _pk_safe_local(value) == previous


def test_punctuation_is_still_normalized():
    """문자는 보존하되 구두점은 여전히 ``_`` 로 바꾼다 (IRI 안전성 유지)."""
    assert _pk_safe_local("EQ.001") == "EQ_001"
    assert _pk_safe_local("a/b") == "a_b"
    assert _pk_safe_local("x y") == "x_y"


def test_mixed_script_pk_is_preserved():
    assert _pk_safe_local("강판-A01") == "강판-A01"


# ──────────────────────────────────────────────────────────────────
# 5. 트랜잭션/마스터 접미 — 비제조 도메인
# ──────────────────────────────────────────────────────────────────

def test_records_suffix_is_recognized_as_transaction():
    """``_Records`` 는 비제조 도메인에서 가장 흔한 트랜잭션 접미다.

    목록에 없으면 파일명 판정을 통과하지 못하고 행수·타임스탬프 휴리스틱으로
    떨어져 검증 티어가 잘못 배정된다 (실측: 병원 Admission_Records).
    """
    from tools.abox_generation import _TRANSACTION_FILENAME_SUFFIXES
    for suffix in ("_Records", "_Log", "_Logs", "_Entries"):
        assert suffix in _TRANSACTION_FILENAME_SUFFIXES, suffix


def test_code_table_suffixes_are_recognized_as_master():
    from tools.abox_generation import _MASTER_FILENAME_SUFFIXES
    for suffix in ("_Master", "_Map", "_Codes", "_Types", "_Dim"):
        assert suffix in _MASTER_FILENAME_SUFFIXES, suffix


def test_deployed_contract_matches_code_fallback():
    """배포 계약 파일과 코드 폴백이 어긋나면 도메인마다 판정이 달라진다."""
    import json

    from domain.rules_paths import rules_path
    with open(rules_path("value_heuristics.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    for suffix in ("_Records", "_Log", "_Logs"):
        assert suffix in cfg["transaction_filename_suffixes"], suffix
    for suffix in ("_Codes", "_Types", "_Dim"):
        assert suffix in cfg["master_filename_suffixes"], suffix


def test_code_fallback_itself_carries_the_new_suffixes():
    """폴백 리터럴을 **소스에서** 확인한다 — 계약 파일이 그것을 가린다.

    모듈 상수는 계약 파일이 있으면 그 값을 쓰므로, 위 테스트들은 폴백 리터럴이
    비어도 통과한다 (실측: 폴백에서 ``_Records`` 를 지우는 mutant 가 생존했다).
    계약 파일이 없는 신규 도메인은 폴백만 쓰기 때문에 그 경로를 따로 고정한다.

    함수 경계 아래의 값은 소스 검사로만 잡힌다.
    """
    import inspect

    import tools.abox_generation as ab

    source = inspect.getsource(ab)
    # 폴백 블록만 잘라 본다 (계약 파일에서 온 값과 혼동하지 않도록).
    start = source.index('_TRANSACTION_FILENAME_SUFFIXES: tuple')
    end = source.index('def _detect_timestamp', start)
    fallback_block = source[start:end]
    for suffix in ("_Records", "_Log", "_Logs", "_Entries"):
        assert f'"{suffix}"' in fallback_block, (
            f"트랜잭션 폴백 리터럴에 {suffix} 가 없다 — 계약 파일이 없는 신규 "
            f"도메인에서 파일명 판정이 실패한다"
        )
    for suffix in ("_Codes", "_Types", "_Dim"):
        assert f'"{suffix}"' in fallback_block, (
            f"master 폴백 리터럴에 {suffix} 가 없다"
        )


def test_new_domain_without_contract_still_classifies_records(tmp_path, monkeypatch):
    """계약 파일이 없을 때 폴백이 실제로 동작하는지 재로드로 확인한다."""
    import importlib

    import tools.abox_generation as ab

    monkeypatch.setenv("RULES_ROOT", str(tmp_path))   # 빈 rules 루트
    monkeypatch.setenv("DOMAIN_CONFIG_PATH", "")      # namespaces 는 이미 로드됨
    reloaded = importlib.reload(ab)
    try:
        assert "_Records" in reloaded._TRANSACTION_FILENAME_SUFFIXES
        assert "_Codes" in reloaded._MASTER_FILENAME_SUFFIXES
    finally:
        monkeypatch.undo()
        importlib.reload(ab)   # 다른 테스트를 위해 원상 복구
