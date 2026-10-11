"""Architect 청크 프롬프트가 CQ ``domains`` 목록의 비문자열 항목을 견디는 계약을 고정한다.

``_build_chunk_prompt`` 는 선택된 CQ 마다 ``관련 도메인`` 줄에 앞의 문자열 세 개를 보여 주고,
같은 CQ 로 필수 OP 경로 블록을 만든다. 목록 안의 비문자열 항목 하나가 표시 단계에서 예외를
내면 조립부가 CQ 블록 전체를 버려, 같은 청크의 정상 질문과 FK 경로까지 사라진다.
기본 재현은 ``tests/test_architect_cq_paths.py`` 가 맡고, 이 파일은 그 경계를 본다:

* 표시 한도는 문자열 세 개다. 비문자열 항목은 한도 앞에 있든 뒤에 있든 자리를 차지하지 않는다
* 표시는 원래 문자열의 철자와 순서를 그대로 쓴다 (prefix·IRI·한글 이름 포함)
* 선택은 기존 ``_cqs_touching_chunk`` 정책을 따르고, FK 경로는 표시 한도 밖의 유효 도메인도 쓴다
* 비문자열만 가진 CQ 를 범위 미정 CQ 로 넓혀 고르지 않고, 컨테이너 안의 문자열을 펼치지 않는다
* 매핑을 모르는 폴백에서는 비문자열만 가진 CQ 도 질문을 잃지 않는다
* 거부한 항목의 원래 값은 로그·프롬프트에서 제외하고, 질문 내용은 프롬프트에만 유지한다
* 경고는 질문 내용 없이 거부한 항목의 개수·위치만 남긴다
* 입력 CQ 객체를 바꾸지 않고, 정상 데이터의 출력과 ``cached_prefix`` 는 그대로다

입력은 임시 CSV header·CQ JSON·rules 파일이고, 프롬프트 조립·선택·FK 경로 분석은 실제 구현을
호출한다. prefix·namespace 는 설정 상수에서 만든다 (리터럴은 타 도메인에서 no-op 이 된다).
"""
from __future__ import annotations

import copy
import json
import logging
import re

import pytest

import config
from domain import rules_paths, table_mapping
from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools import multi_agent_tbox as mat
from tools import tbox_generation as tg

_FK_PATTERNS = {
    "patterns": {
        "equipmentid": "EquipmentMaster",
        "productid": "ProductMaster",
    },
}

#: 한글 원본 테이블명. 표시는 원래 철자를, FK 경로는 매핑된 클래스명을 쓰는지 가른다.
_HANGUL_TABLE = "설비_점검"

_TABLE_COLUMNS = {
    "Alarm_Events": ["Event_ID", "Equipment_ID", "Severity"],
    "Equipment_Master": ["Equipment_ID", "Name"],
    "Sensor_Readings": ["Reading_ID", "Equipment_ID", "Value"],
    "Product_Master": ["Product_ID", "Name"],
    _HANGUL_TABLE: ["Inspection_ID", "Equipment_ID"],
}
_TABLE_CLASSES = {
    "Alarm_Events": "AlarmEvents",
    "Equipment_Master": "EquipmentMaster",
    "Sensor_Readings": "SensorReadings",
    "Product_Master": "ProductMaster",
    _HANGUL_TABLE: "EquipmentInspection",
}

#: 표시 한도 안팎을 가르는 유효 문자열 네 개. 넷째는 표시되지 않지만 선택과 FK 경로에 쓰인다.
_PREFIXED = f"{NS_PREFIX}:AlarmEvents"
_BRACKETED_IRI = f"<{DOMAIN_NS}SensorReadings>"
_FOUR_STRINGS = [_PREFIXED, _HANGUL_TABLE, _BRACKETED_IRI, "Equipment_Master"]
#: 직접 연결 세 줄은 모두 넷째 도메인(``EquipmentMaster``)을 가리킨다.
_FOUR_STRINGS_PATHS = [
    ("hub", "AlarmEvents", "EquipmentMaster", "EquipmentInspection"),
    ("hub", "AlarmEvents", "EquipmentMaster", "SensorReadings"),
    ("direct", "AlarmEvents", "EquipmentMaster"),
    ("hub", "EquipmentInspection", "EquipmentMaster", "SensorReadings"),
    ("direct", "EquipmentInspection", "EquipmentMaster"),
    ("direct", "SensorReadings", "EquipmentMaster"),
]

#: 거부돼야 하는 원래 값의 고유 표식. 로그·프롬프트에 새면 문자열 검색으로 찾을 수 있다.
_OBJECT_MARK = "REJECTED-OBJECT-7f3a"
_LIST_MARK = "REJECTED-LIST-7f3a"
_INT_VALUE = 914273
_FLOAT_VALUE = 271828.5
_LEAK_MARKS = (_OBJECT_MARK, _LIST_MARK, str(_INT_VALUE), repr(_FLOAT_VALUE))

_ENTRY_RE = re.compile(
    r"^- \*\*CQ(?P<n>\d+)\*\*: (?P<question>.*)\n  - 관련 도메인: ?(?P<domains>.*)$",
    re.MULTILINE,
)
_HEADER_RE = re.compile(r"^\*\*(?P<cq>.+?)\*\* \(도메인:")
_DIRECT_RE = re.compile(r"^\s*- (\S+) → (\S+) \(FK 직접 연결\)$")
_HUB_RE = re.compile(r"^\s*- (\S+) → (\S+) ← (\S+) \(공유 FK 허브\)$")


def _object_item() -> dict:
    # 유효한 테이블명을 품은 컨테이너다. 펼쳐서 도메인으로 쓰면 선택과 경로가 넓어진다.
    return {"leak": _OBJECT_MARK, "table": "Alarm_Events"}


def _list_item() -> list:
    return [_LIST_MARK, "Alarm_Events"]


def _cq(cq_id: str, domains) -> dict:
    # 괄호로 감싸 한 질문이 다른 질문의 부분 문자열이 되지 않게 한다.
    return {"id": cq_id, "question_ko": f"[{cq_id}] 질문은 무엇인가?", "domains": domains}


def _rendered(variable: str, cqs: list[dict]) -> list[tuple[str, str]]:
    """CQ 블록을 ``[(cq_id, 표시 도메인 문자열)]`` 로 읽는다.

    번호가 1부터 빈틈없이 이어지는지, 해석하지 못한 CQ 줄이 없는지도 함께 본다.
    """
    by_question = {cq["question_ko"]: cq["id"] for cq in cqs}
    entries = list(_ENTRY_RE.finditer(variable))
    assert [int(m["n"]) for m in entries] == list(range(1, len(entries) + 1)), "CQ 번호가 이어지지 않는다"
    assert variable.count("- **CQ") == len(entries), "형식을 해석하지 못한 CQ 줄이 있다"
    return [(by_question[m["question"]], m["domains"]) for m in entries]


def _paths_by_cq(text: str) -> dict[str, list[tuple[str, ...]]]:
    """필수 OP 블록을 ``{cq_id: [("direct", a, b) | ("hub", a, hub, b)]}`` 로 읽는다.

    경로가 없는 CQ 는 결과에서 뺀다. 블록 안에서 해석하지 못한 화살표 줄은 실패로 올려
    형식 변경 때문에 부정 단언이 공허하게 통과하지 않게 한다.
    """
    paths: dict[str, list[tuple[str, ...]]] = {}
    current = None
    for line in text.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            current = header.group("cq")
            paths.setdefault(current, [])
            continue
        if not line.strip():
            current = None
            continue
        if current is None:
            continue
        direct = _DIRECT_RE.match(line)
        hub = _HUB_RE.match(line)
        if direct:
            paths[current].append(("direct", *direct.groups()))
        elif hub:
            paths[current].append(("hub", *hub.groups()))
        elif "→" in line:
            raise AssertionError(f"경로 줄 형식을 해석하지 못했다: {line!r}")
    return {cq_id: entries for cq_id, entries in paths.items() if entries}


def _builder_warnings(records) -> list[str]:
    return [
        record.getMessage() for record in records
        if record.name == tg.__name__ and record.levelno >= logging.WARNING
    ]


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
    """임시 rules·CSV·CQ 파일로 청크 프롬프트를 만드는 빌더."""
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "fk_patterns.json").write_text(json.dumps(_FK_PATTERNS), encoding="utf-8")
    (rules / "table_class_mapping.json").write_text(
        json.dumps({"table_class_mapping": _TABLE_CLASSES}, ensure_ascii=False), encoding="utf-8",
    )
    raw = tmp_path / "rawdata"
    raw.mkdir()
    for name, columns in _TABLE_COLUMNS.items():
        (raw / f"{name}.csv").write_text(",".join(columns) + "\n", encoding="utf-8")
    cq_path = tmp_path / "competency_questions.json"

    # 경로 상수를 import 시점에 복사한 모듈이 있어도 같은 fixture 를 보게 한다.
    for module in (config, tg, mat):
        monkeypatch.setattr(module, "COMPETENCY_QUESTIONS_PATH", str(cq_path), raising=False)
        monkeypatch.setattr(module, "SOURCE_RAWDATA_DIR", str(raw), raising=False)
    monkeypatch.setattr(mat, "_CQ_PATH", str(cq_path))
    monkeypatch.setattr(rules_paths, "RULES_ROOT", str(rules))
    monkeypatch.setattr(table_mapping, "RULES_ROOT", str(rules))
    monkeypatch.setattr(table_mapping, "_CACHE", {})
    monkeypatch.setattr(tg, "_RULES_DIR", str(rules))
    monkeypatch.setattr(mat, "_RULES_DIR", str(rules))
    monkeypatch.setattr(tg, "TBOX_BASELINE_PATH", str(tmp_path / "no_baseline.ttl"))

    class _Env:
        path = cq_path

        @staticmethod
        def write_cqs(cqs: list[dict]) -> None:
            cq_path.write_text(json.dumps(cqs, ensure_ascii=False), encoding="utf-8")

        @staticmethod
        def build(table: str = "Alarm_Events", mapping: dict | None = None,
                  chunk_idx: int = 1) -> tuple[str, str]:
            return tg._build_chunk_prompt(
                chunk_idx=chunk_idx,
                chunk_tables=[{"name": table, "columns": _TABLE_COLUMNS.get(table, ["Unmapped_ID"])}],
                total_chunks=3,
                iof_summary="fixture IOF context",
                schema_info={},
                relationships_info="",
                table_class_map=dict(_TABLE_CLASSES) if mapping is None else mapping,
            )

    return _Env()


# ── 표시 한도: 문자열 세 개 ─────────────────────────────────────────


def _four_strings_with_invalid(layout: str) -> list:
    s0, s1, s2, s3 = _FOUR_STRINGS
    if layout == "invalid_before_limit":
        return [None, _INT_VALUE, s0, s1, s2, s3]
    if layout == "invalid_between_strings":
        return [s0, _object_item(), s1, _list_item(), s2, s3]
    return [s0, s1, s2, None, s3, _FLOAT_VALUE]


@pytest.mark.parametrize(
    "table", ["Alarm_Events", "Equipment_Master"], ids=["first_string_matches", "fourth_string_matches"],
)
@pytest.mark.parametrize(
    "layout", ["invalid_before_limit", "invalid_between_strings", "invalid_after_limit"],
)
def test_display_limit_counts_strings_not_list_slots(prompt_env, layout, table):
    """표시는 앞의 문자열 세 개이고, 넷째 유효 도메인도 선택과 FK 경로에 쓰인다.

    비문자열이 한도 앞에 있어도 표시 자리를 차지하지 않는다. 표시용으로 자른 목록을 선택이나
    경로 분석에 재사용하면 넷째 도메인(``Equipment_Master``)의 직접 경로가 사라지고, 청크가
    넷째 도메인과 맞는 경우에는 질문 자체가 빠진다.
    """
    cqs = [
        _cq("MIXED", _four_strings_with_invalid(layout)),
        _cq("NEIGHBOUR", ["Equipment_Master", "Alarm_Events"]),
        _cq("OTHER", ["Product_Master"]),
    ]
    prompt_env.write_cqs(cqs)

    _, variable = prompt_env.build(table)

    assert _rendered(variable, cqs) == [
        ("MIXED", ", ".join(_FOUR_STRINGS[:3])),
        ("NEIGHBOUR", "Equipment_Master, Alarm_Events"),
    ]
    assert _paths_by_cq(variable) == {
        "MIXED": _FOUR_STRINGS_PATHS,
        "NEIGHBOUR": [("direct", "AlarmEvents", "EquipmentMaster")],
    }


# ── 선택 정책: 여러 malformed CQ 와 정상 CQ ─────────────────────────────


def test_several_malformed_cqs_keep_valid_context_without_widening(prompt_env):
    """malformed CQ 여럿이 섞여도 각자의 유효 도메인으로만 고르고 표시한다.

    비문자열만 가진 목록은 비어 있지 않으므로 기존 정책에서 범위 미정이 아니다. 먼저 거른
    뒤 빈 목록으로 판정하면 무관한 질문이 모든 청크로 퍼진다. 컨테이너 안의 테이블명을
    펼쳐 쓰면 ``OBJECT_OTHER`` 와 ``NESTED_ONLY`` 가 이 청크로 끌려온다.
    """
    cqs = [
        _cq("FIRST_BAD", [None, "Alarm_Events", _INT_VALUE, "Equipment_Master"]),
        _cq("NESTED_ONLY", [_list_item(), True]),
        _cq("OBJECT_OTHER", [_object_item(), "Product_Master"]),
        _cq("SECOND_BAD", [_FLOAT_VALUE, _BRACKETED_IRI, _object_item(), "Alarm_Events"]),
        _cq("ALL_SCALARS", [None, False, 0, 0.0]),
        _cq("GOOD", ["Alarm_Events", _HANGUL_TABLE]),
        _cq("UNSCOPED_EMPTY", []),
        _cq("UNSCOPED_NULL", None),
    ]
    prompt_env.write_cqs(cqs)

    cached, variable = prompt_env.build("Alarm_Events")

    rendered = _rendered(variable, cqs)
    assert rendered == [
        ("FIRST_BAD", "Alarm_Events, Equipment_Master"),
        ("SECOND_BAD", f"{_BRACKETED_IRI}, Alarm_Events"),
        ("GOOD", f"Alarm_Events, {_HANGUL_TABLE}"),
        ("UNSCOPED_EMPTY", ""),
        ("UNSCOPED_NULL", ""),
    ]
    policy = tg._cqs_touching_chunk(
        json.loads(prompt_env.path.read_text(encoding="utf-8")),
        [{"name": "Alarm_Events"}], dict(_TABLE_CLASSES),
    )
    assert [cq["id"] for cq in policy] == [cq_id for cq_id, _ in rendered]
    assert _paths_by_cq(variable) == {
        "FIRST_BAD": [("direct", "AlarmEvents", "EquipmentMaster")],
        "SECOND_BAD": [("hub", "SensorReadings", "EquipmentMaster", "AlarmEvents")],
        "GOOD": [("hub", "AlarmEvents", "EquipmentMaster", "EquipmentInspection")],
    }
    assert all(cq["question_ko"] not in cached for cq in cqs)


@pytest.mark.parametrize("fallback", ["empty_mapping", "unmapped_chunk"])
def test_all_invalid_cqs_survive_unknown_mapping_fallback(prompt_env, fallback):
    """청크 매핑을 모르면 전부 준다. 비문자열만 가진 CQ 도 빈 도메인 줄로 질문을 유지한다."""
    cqs = [
        _cq("ALL_INVALID_SCALARS", [None, _INT_VALUE, True]),
        _cq("ALL_INVALID_CONTAINERS", [_object_item(), _list_item(), _FLOAT_VALUE]),
        _cq("GOOD", ["Alarm_Events", "Equipment_Master"]),
        _cq("PARTIAL", [None, "Sensor_Readings", "Alarm_Events"]),
    ]
    prompt_env.write_cqs(cqs)

    if fallback == "empty_mapping":
        _, variable = prompt_env.build("Alarm_Events", mapping={})
    else:
        _, variable = prompt_env.build("Unmapped_Table")

    assert _rendered(variable, cqs) == [
        ("ALL_INVALID_SCALARS", ""),
        ("ALL_INVALID_CONTAINERS", ""),
        ("GOOD", "Alarm_Events, Equipment_Master"),
        ("PARTIAL", "Sensor_Readings, Alarm_Events"),
    ]
    assert _paths_by_cq(variable) == {
        "GOOD": [("direct", "AlarmEvents", "EquipmentMaster")],
        "PARTIAL": [("hub", "SensorReadings", "EquipmentMaster", "AlarmEvents")],
    }


# ── 경고: 메타데이터만 ────────────────────────────────────────────────


def test_rejected_items_leave_only_metadata_in_logs_and_prompt(prompt_env, caplog):
    """버린 값은 로그·프롬프트에 없고, 정상 질문은 프롬프트에만 남는다."""
    cqs = [
        _cq("LEAKY_A", [_object_item(), "Alarm_Events", _INT_VALUE, "Equipment_Master"]),
        _cq("LEAKY_B", [_list_item(), _FLOAT_VALUE, "Sensor_Readings", "Alarm_Events"]),
        _cq("GOOD", ["Alarm_Events", "Equipment_Master"]),
    ]
    prompt_env.write_cqs(cqs)

    with caplog.at_level(logging.DEBUG):
        cached, variable = prompt_env.build("Alarm_Events")

    assert [cq_id for cq_id, _ in _rendered(variable, cqs)] == ["LEAKY_A", "LEAKY_B", "GOOD"]
    warnings = _builder_warnings(caplog.records)
    assert warnings, "비문자열 domains 항목을 버렸다는 경고가 없다"
    assert any(ch.isdigit() for message in warnings for ch in message), "경고에 개수·위치 정보가 없다"
    record_texts = [repr(vars(record)) for record in caplog.records]
    record_texts += [record.getMessage() for record in caplog.records]
    for mark in _LEAK_MARKS:
        assert mark not in cached and mark not in variable, f"거부한 값이 프롬프트에 남았다: {mark}"
        assert not any(mark in text for text in record_texts), f"거부한 값이 로그에 남았다: {mark}"
    for cq in cqs:
        assert not any(cq["question_ko"] in text for text in record_texts), "질문 내용이 로그에 남았다"


def test_warning_is_absent_for_clean_data_and_tracks_rejections(prompt_env, caplog):
    """정상 데이터는 경고가 없고, 버린 항목이 달라지면 경고 메타데이터도 달라진다."""
    good = _cq("GOOD", ["Alarm_Events", "Equipment_Master"])

    def warnings_for(cqs: list[dict]) -> list[str]:
        prompt_env.write_cqs(cqs)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            prompt_env.build("Alarm_Events")
        return _builder_warnings(caplog.records)

    clean = warnings_for([_cq("TARGET", list(_FOUR_STRINGS)), good])
    single = warnings_for([_cq("TARGET", [None, "Alarm_Events", "Equipment_Master"]), good])
    several = warnings_for([
        _cq("TARGET", [None, "Alarm_Events", _INT_VALUE, "Equipment_Master", True]),
        good,
        _cq("SECOND", [_object_item(), "Sensor_Readings", _list_item(), "Alarm_Events", _FLOAT_VALUE]),
    ])

    assert clean == []
    assert single and several
    assert single != several, "경고가 버린 항목의 개수·위치를 구분하지 않는다"


def test_invalid_item_after_display_limit_is_still_reported(prompt_env, caplog):
    """표시에 영향이 없는 한도 뒤 항목도 버린 것이므로 경고에 들어간다."""
    cqs = [_cq("AFTER_LIMIT", [*_FOUR_STRINGS[:3], None, _FOUR_STRINGS[3]])]
    prompt_env.write_cqs(cqs)

    with caplog.at_level(logging.DEBUG):
        _, variable = prompt_env.build("Alarm_Events")

    assert _rendered(variable, cqs) == [("AFTER_LIMIT", ", ".join(_FOUR_STRINGS[:3]))]
    assert _builder_warnings(caplog.records), "표시 한도 뒤의 비문자열 항목을 경고 없이 버렸다"


# ── 정상 데이터 동등성·캐시·비변경 ─────────────────────────────────────


def test_clean_rendering_is_unchanged_and_ignored_items_leave_no_trace(prompt_env):
    """정상 데이터는 기존 형식 그대로이고, 비문자열을 끼워 넣어도 두 프롬프트가 같다."""
    clean = [
        _cq("FOUR", list(_FOUR_STRINGS)),
        _cq("TWO", ["Alarm_Events", "Product_Master"]),
        _cq("UNSCOPED", []),
    ]
    prompt_env.write_cqs(clean)
    cached_clean, variable_clean = prompt_env.build("Alarm_Events")

    for number, cq in enumerate(clean, start=1):
        line = f"- **CQ{number}**: {cq['question_ko']}\n  - 관련 도메인: {', '.join(cq['domains'][:3])}\n"
        assert line in variable_clean
    assert _paths_by_cq(variable_clean) == {"FOUR": _FOUR_STRINGS_PATHS}

    dirty = copy.deepcopy(clean)
    dirty[0]["domains"] = [None, *_FOUR_STRINGS[:2], _object_item(), *_FOUR_STRINGS[2:], _FLOAT_VALUE]
    dirty[1]["domains"] = ["Alarm_Events", _list_item(), True, "Product_Master", _INT_VALUE]
    prompt_env.write_cqs(dirty)
    cached_dirty, variable_dirty = prompt_env.build("Alarm_Events")

    assert variable_dirty == variable_clean
    assert cached_dirty == cached_clean


@pytest.mark.parametrize("chunk_idx", [0, 1])
def test_cached_prefix_is_independent_of_cq_domain_content(prompt_env, chunk_idx):
    """malformed CQ·매핑 폴백·다른 청크에서도 ``cached_prefix`` 는 CQ 없는 호출과 같다."""
    prompt_env.write_cqs([])
    cached_without_cqs, _ = prompt_env.build("Alarm_Events", chunk_idx=0)
    prompt_env.write_cqs([
        _cq("MIXED", [None, *_FOUR_STRINGS, _object_item()]),
        _cq("ALL_INVALID", [_list_item(), _INT_VALUE]),
    ])

    for table, mapping in (("Alarm_Events", None), ("Alarm_Events", {}), ("Equipment_Master", None)):
        cached, variable = prompt_env.build(table, mapping=mapping, chunk_idx=chunk_idx)
        assert "[MIXED]" in variable, f"{table}/{mapping!r} 호출에서 CQ 블록이 빠졌다"
        assert cached == cached_without_cqs
        assert "관련 도메인" not in cached and "[MIXED]" not in cached


def test_prompt_build_does_not_mutate_cq_objects(prompt_env, monkeypatch):
    """선택·표시·경로 분석이 CQ 객체를 제자리에서 바꾸지 않고 유효 도메인을 모두 넘긴다.

    선택과 경로 분석기는 실제 구현을 그대로 부르는 관찰자로 감싼다. 진입 시점 사본과
    호출 후 원본이 다르면 어느 단계가 ``domains`` 를 고쳐 쓴 것이다.
    """
    cqs = [
        _cq("FIRST_BAD", [None, "Alarm_Events", _object_item(), "Equipment_Master"]),
        _cq("SECOND_BAD", [_list_item(), "Sensor_Readings", _FLOAT_VALUE, "Alarm_Events"]),
        _cq("ALL_INVALID", [None, _INT_VALUE]),
        _cq("GOOD", [_PREFIXED, _HANGUL_TABLE, _BRACKETED_IRI, "Equipment_Master"]),
    ]
    prompt_env.write_cqs(cqs)
    original_bytes = prompt_env.path.read_bytes()
    real_select = tg._cqs_touching_chunk
    real_analyze = mat._analyze_cq_required_ops

    caller_owned = json.loads(original_bytes)
    caller_snapshot = copy.deepcopy(caller_owned)
    real_select(caller_owned, [{"name": "Alarm_Events"}], dict(_TABLE_CLASSES))
    assert caller_owned == caller_snapshot, "선택 함수가 호출자 목록을 바꿨다"

    seen: dict[str, list] = {"select": [], "analyze": []}

    def observe_select(items, *args, **kwargs):
        seen["select"].append((list(items), copy.deepcopy(list(items))))
        return real_select(items, *args, **kwargs)

    def observe_analyze(items, *args, **kwargs):
        seen["analyze"].append((list(items), copy.deepcopy(list(items))))
        return real_analyze(items, *args, **kwargs)

    monkeypatch.setattr(tg, "_cqs_touching_chunk", observe_select)
    monkeypatch.setattr(mat, "_analyze_cq_required_ops", observe_analyze)

    _, variable = prompt_env.build("Alarm_Events")

    assert seen["select"], "프롬프트 조립이 기존 선택 함수를 거치지 않았다"
    assert len(seen["analyze"]) == 1, "FK 경로 분석이 한 번 호출되지 않았다"
    for live, snapshot in seen["select"] + seen["analyze"]:
        assert live == snapshot, "프롬프트 조립이 CQ 객체를 제자리에서 바꿨다"
    received = {cq["id"]: cq for cq in seen["analyze"][0][1]}
    assert list(received) == ["FIRST_BAD", "SECOND_BAD", "GOOD"]
    for cq in cqs:
        if cq["id"] in received:
            got = [d for d in received[cq["id"]].get("domains") or [] if isinstance(d, str)]
            assert got == [d for d in cq["domains"] if isinstance(d, str)], cq["id"]
    assert _paths_by_cq(variable)["GOOD"] == _FOUR_STRINGS_PATHS
    assert prompt_env.path.read_bytes() == original_bytes
