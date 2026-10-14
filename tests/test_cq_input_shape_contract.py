"""S2 CQ 소비자가 저장 CQ 문서의 구조 오류를 항목 단위로 격리하는 계약의 경계를 고정한다.

``_build_chunk_prompt`` (Architect 청크 프롬프트) 와 ``multi_agent_tbox._load_competency_questions``
(협업 loader) 는 같은 저장 파일을 읽는다. 문서 최상위는 배열, 항목은 객체이고 ``domains`` 는
미지정·null·배열만 허용한다. 그 밖의 형태는 해당 항목만 버리고 나머지 정상 CQ 와 FK 경로는
그대로 둔다. 기본 재현은 ``tests/test_architect_cq_paths.py`` 와 ``tests/test_multi_agent_tbox.py``
가 맡고, 이 파일은 그 경계를 본다:

* 프롬프트 창은 원본 파일의 첫 15개 항목이다. 창 안의 항목을 버려도 16번째로 채우지 않는다
* 경고는 앞선 항목을 버린 뒤에도 원본 배열의 1-based 순번을 쓴다
* 경고는 고정된 사유 문구뿐이다. 원래 값·질문·id 가 달라져도 경고 문구는 같다
* 버린 항목은 처음부터 없던 것과 같다. 목록 내부 혼합 타입 처리와 매핑 폴백 선택은 그대로다
* S0 사용자 입력 parser 의 plain text·JSON 문자열 항목 형식과 저장 결과는 그대로다
* loader 는 정상 항목을 원래 순서·내용 그대로 돌려주고 입력 파일을 바꾸지 않는다
* 배열이 아닌 최상위는 빈 목록과 경고가 된다. JSON 구문·I/O 오류는 기존 경계 동작을 유지한다

입력은 임시 CSV header·CQ JSON·rules 파일이고, 프롬프트 조립·선택·FK 경로 분석·loader 는
실제 구현을 호출한다. prefix·namespace 는 설정 상수에서 만든다.
"""
from __future__ import annotations

import json
import logging
import re

import pytest

import config
from domain import rules_paths, table_mapping
from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools import competency_questions as cqm
from tools import multi_agent_tbox as mat
from tools import tbox_generation as tg

_FK_PATTERNS = {
    "patterns": {
        "equipmentid": "EquipmentMaster",
        "productid": "ProductMaster",
    },
}

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

#: 청크 선택 방식. 매핑 폴백 두 가지는 선택이 모든 CQ 를 넘기므로 구조 검증만 항목을 거른다.
_MODES = {
    "mapped": ("Alarm_Events", _TABLE_CLASSES),
    "empty_mapping": ("Alarm_Events", {}),
    "unmapped_chunk": ("Unmapped_Table", _TABLE_CLASSES),
}

_CQ_HEADER = "## Competency Questions"
_PAIR_DOMAINS = ["Alarm_Events", "Equipment_Master"]

_ENTRY_RE = re.compile(
    r"^- \*\*CQ(?P<n>\d+)\*\*: (?P<question>.*)\n  - 관련 도메인: ?(?P<domains>.*)$",
    re.MULTILINE,
)
_HEADER_RE = re.compile(r"^\*\*(?P<cq>.+?)\*\* \(도메인:")
_DIRECT_RE = re.compile(r"^\s*- (\S+) → (\S+) \(FK 직접 연결\)$")
_HUB_RE = re.compile(r"^\s*- (\S+) → (\S+) ← (\S+) \(공유 FK 허브\)$")


def _cq(cq_id: str, domains) -> dict:
    # 괄호로 감싸 한 질문이 다른 질문의 부분 문자열이 되지 않게 한다.
    return {"id": cq_id, "question_ko": f"[{cq_id}] 질문은 무엇인가?", "domains": domains}


def _bad_cq(mark: str, domains) -> dict:
    return {"id": f"BAD-ID-{mark}", "question_ko": f"[BAD-Q-{mark}] 버려야 하는 질문은?", "domains": domains}


#: 거부할 ``domains`` 형태. 문자열·dict 는 실제 테이블명을 품어 문자 단위·key 단위 순회를 드러낸다.
_DOMAIN_SHAPES = {
    "str": lambda mark, number: f"Alarm_Events-{mark}",
    "dict": lambda mark, number: {"Alarm_Events": f"VALUE-{mark}", f"KEY-{mark}": "Equipment_Master"},
    "int": lambda mark, number: number,
    "float": lambda mark, number: number + 0.5,
    "true": lambda mark, number: True,
    "zero": lambda mark, number: 0,
    "false": lambda mark, number: False,
    "empty_str": lambda mark, number: "",
    "empty_dict": lambda mark, number: {},
}
#: 거부할 비객체 항목. 목록 항목은 정상 CQ 객체를 품어 펼침 여부를 드러낸다.
_ITEM_SHAPES = {
    "str": lambda mark, number: f"Alarm_Events 질문 ITEM-{mark}",
    "list": lambda mark, number: [f"ITEM-{mark}", _bad_cq(mark, ["Alarm_Events"])],
    "int": lambda mark, number: number,
    "null": lambda mark, number: None,
}
_BAD_KINDS = [f"domains:{shape}" for shape in _DOMAIN_SHAPES] + [f"item:{shape}" for shape in _ITEM_SHAPES]


def _bad_entry(kind: str, mark: str, number: int):
    group, _, shape = kind.partition(":")
    if group == "domains":
        return _bad_cq(mark, _DOMAIN_SHAPES[shape](mark, number))
    return _ITEM_SHAPES[shape](mark, number)


def _rendered(variable: str, entries: list) -> list[tuple[str, str]]:
    """CQ 블록을 ``[(cq_id, 표시 도메인 문자열)]`` 로 읽는다. 모르는 질문은 ``?질문`` 으로 남긴다."""
    by_question = {
        entry["question_ko"]: entry.get("id")
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("question_ko"), str)
    }
    found = list(_ENTRY_RE.finditer(variable))
    assert [int(m["n"]) for m in found] == list(range(1, len(found) + 1)), "CQ 번호가 이어지지 않는다"
    assert variable.count("- **CQ") == len(found), "형식을 해석하지 못한 CQ 줄이 있다"
    return [(by_question.get(m["question"], "?" + m["question"]), m["domains"]) for m in found]


def _rendered_ids(variable: str, entries: list) -> list[str]:
    return [cq_id for cq_id, _ in _rendered(variable, entries)]


def _paths_by_cq(text: str) -> dict[str, list[tuple[str, ...]]]:
    """필수 OP 블록을 ``{cq_id: [("direct", a, b) | ("hub", a, hub, b)]}`` 로 읽는다 (헤더 순서 유지)."""
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


def _warnings(records) -> list[str]:
    return [
        record.getMessage() for record in records
        if record.levelno >= logging.WARNING and record.name.startswith("tools")
    ]


def _record_texts(records) -> list[str]:
    # 타임스탬프·thread id 같은 숫자 필드와 우연히 겹치지 않도록 메시지와 인자만 본다.
    texts: list[str] = []
    for record in records:
        texts += [record.getMessage(), str(record.msg), repr(record.args), record.exc_text or ""]
    return texts


@pytest.fixture
def cq_env(tmp_path, monkeypatch):
    """임시 rules·CSV·CQ 파일로 청크 프롬프트와 협업 loader 를 호출하는 환경."""
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
    for module in (config, tg, mat, cqm):
        monkeypatch.setattr(module, "COMPETENCY_QUESTIONS_PATH", str(cq_path), raising=False)
        monkeypatch.setattr(module, "SOURCE_RAWDATA_DIR", str(raw), raising=False)
    for module in (mat, cqm):
        monkeypatch.setattr(module, "_CQ_PATH", str(cq_path))
    monkeypatch.setattr(rules_paths, "RULES_ROOT", str(rules))
    monkeypatch.setattr(table_mapping, "RULES_ROOT", str(rules))
    monkeypatch.setattr(table_mapping, "_CACHE", {})
    monkeypatch.setattr(tg, "_RULES_DIR", str(rules))
    monkeypatch.setattr(mat, "_RULES_DIR", str(rules))
    monkeypatch.setattr(tg, "TBOX_BASELINE_PATH", str(tmp_path / "no_baseline.ttl"))

    class _Env:
        path = cq_path

        @staticmethod
        def write(document) -> None:
            cq_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        @staticmethod
        def build(mode: str = "mapped") -> tuple[str, str]:
            table, mapping = _MODES[mode]
            return tg._build_chunk_prompt(
                chunk_idx=1,
                chunk_tables=[{"name": table, "columns": _TABLE_COLUMNS.get(table, ["Unmapped_ID"])}],
                total_chunks=3,
                iof_summary="fixture IOF context",
                schema_info={},
                relationships_info="",
                table_class_map=dict(mapping),
            )

        @staticmethod
        def load():
            return mat._load_competency_questions()

    return _Env()


# ── 원본 창: 첫 15개 항목을 거르기 전에 자른다 ─────────────────────────────


@pytest.mark.parametrize("mode", ["mapped", "unmapped_chunk"])
@pytest.mark.parametrize(
    ("kind", "position"),
    [(None, None), ("item:str", 3), ("domains:str", 15), ("domains:zero", 1), ("item:null", 16)],
    ids=["clean", "item_inside", "domains_at_edge", "falsey_first", "item_outside"],
)
def test_prompt_window_is_first_fifteen_source_entries(cq_env, mode, kind, position):
    """창 안의 항목을 버리면 그 자리가 비고 16번째 항목이 올라오지 않는다.

    거른 뒤 자르면 16번째가 들어와 원본 창이 바뀐다. FK 경로 블록은 기존대로 선택된 CQ 의
    앞 10개를 쓴다.
    """
    entries = [_cq(f"S{n:02d}", list(_PAIR_DOMAINS)) for n in range(1, 17)]
    if kind is not None:
        entries[position - 1] = _bad_entry(kind, "a41c", 5123)
    cq_env.write(entries)

    _, variable = cq_env.build(mode)

    expected = [f"S{n:02d}" for n in range(1, 16) if n != position]
    assert _rendered_ids(variable, entries) == expected
    assert list(_paths_by_cq(variable)) == expected[:10]
    assert "[S16]" not in variable
    assert "BAD-Q-a41c" not in variable and "ITEM-a41c" not in variable


# ── 경고: 원본 1-based 순번 ─────────────────────────────────────────────


@pytest.mark.parametrize("consumer", ["builder", "loader"])
def test_warning_names_original_position_after_prior_rejection(cq_env, caplog, consumer):
    """앞 항목을 버린 뒤에도 경고는 원본 배열의 1-based 순번(4, 6)을 쓴다.

    0-based 는 3·5, 남은 항목 기준 순번은 4·5 또는 3·4 가 되어 6번째 항목을 가리키지 못한다.
    """
    # 표식에 숫자를 넣지 않는다. 원래 값이 새더라도 순번 검사가 그 숫자로 통과하지 않게 한다.
    valid = {n: _cq(f"P{n}", list(_PAIR_DOMAINS)) for n in (1, 2, 3, 5, 7)}
    entries = [
        valid[1], valid[2], valid[3],
        _bad_entry("item:str", "qzkw", 0),
        valid[5],
        _bad_entry("domains:dict", "qzkw", 0),
        valid[7],
    ]
    cq_env.write(entries)

    with caplog.at_level(logging.DEBUG):
        if consumer == "builder":
            _, variable = cq_env.build("unmapped_chunk")
            assert _rendered_ids(variable, entries) == ["P1", "P2", "P3", "P5", "P7"]
        else:
            assert cq_env.load() == list(valid.values())

    warnings = _warnings(caplog.records)
    for position in (4, 6):
        assert any(re.search(rf"(?<!\d){position}(?!\d)", message) for message in warnings), (
            f"원본 {position}번째 항목을 가리키는 경고가 없다: {warnings}"
        )


# ── 경고: 고정 사유뿐, 원래 값·질문·id 없음 ──────────────────────────────


@pytest.mark.parametrize("kind", _BAD_KINDS)
@pytest.mark.parametrize("consumer", ["builder", "loader"])
def test_rejection_warning_is_static_and_carries_no_payload(cq_env, caplog, consumer, kind):
    """같은 자리의 같은 종류 오류는 원래 값·질문·id 가 달라도 같은 경고를 낸다.

    경고 형식과 무관하게, 문구가 payload 에 따라 바뀌면 원래 값이 새는 것이다. 버린 항목과
    정상 항목의 질문·id 는 어떤 수준의 로그에도 없고, 버린 질문은 프롬프트에도 없다.
    """
    keep = [_cq("KEEP-FIRST", list(_PAIR_DOMAINS)), _cq("KEEP-LAST", ["Alarm_Events"])]

    def run(mark: str, number: int):
        entries = [keep[0], _bad_entry(kind, mark, number), keep[1]]
        cq_env.write(entries)
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            if consumer == "builder":
                _, variable = cq_env.build("unmapped_chunk")
                assert _rendered_ids(variable, entries) == ["KEEP-FIRST", "KEEP-LAST"]
                assert mark not in variable
            else:
                assert cq_env.load() == keep
        records = list(caplog.records)
        texts = _record_texts(records)
        for token in (mark, str(number)):
            assert not any(token in text for text in texts), f"버린 원래 값이 로그에 남았다: {token}"
        for cq in keep:
            assert not any(cq["id"] in text or cq["question_ko"] in text for text in texts), (
                "정상 CQ 의 질문·id 가 로그에 남았다"
            )
        return _warnings(records)

    first = run("7f3a", 9142731)
    second = run("9c1d", 8253641)

    assert first, f"{kind} 항목을 경고 없이 버렸다"
    assert first == second, "경고 문구가 원래 값·질문·id 에 따라 달라진다"


# ── 버린 항목은 없던 것과 같다 (혼합 목록·매핑 폴백 유지) ──────────────────


def _neighbourhood() -> list[dict]:
    return [
        _cq("PAIR", list(_PAIR_DOMAINS)),
        _cq("MIXED", [None, "Sensor_Readings", 5, {"k": "Product_Master"}, "Alarm_Events"]),
        _cq("NESTED_ONLY", [["Alarm_Events"], True]),
        {"id": "MISSING", "question_ko": "[MISSING] 질문은 무엇인가?"},
        _cq("NULL", None),
        _cq("EMPTY", []),
        _cq("OTHER", ["Product_Master"]),
    ]


_UNSCOPED = [("MISSING", ""), ("NULL", ""), ("EMPTY", "")]
_MAPPED_RENDER = [
    ("PAIR", "Alarm_Events, Equipment_Master"),
    ("MIXED", "Sensor_Readings, Alarm_Events"),
    *_UNSCOPED,
]
_FALLBACK_RENDER = [
    ("PAIR", "Alarm_Events, Equipment_Master"),
    ("MIXED", "Sensor_Readings, Alarm_Events"),
    ("NESTED_ONLY", ""),
    *_UNSCOPED,
    ("OTHER", "Product_Master"),
]
_NEIGHBOUR_PATHS = {
    "PAIR": [("direct", "AlarmEvents", "EquipmentMaster")],
    "MIXED": [("hub", "SensorReadings", "EquipmentMaster", "AlarmEvents")],
}


@pytest.mark.parametrize("mode", list(_MODES))
@pytest.mark.parametrize("kind", _BAD_KINDS)
def test_rejected_entry_matches_removed_entry_beside_mixed_lists(cq_env, kind, mode):
    """잘못된 항목 하나가 있는 파일의 프롬프트는 그 항목을 지운 파일과 같다.

    목록 내부 혼합 타입은 기존대로 받아들이고(문자열만 표시·선택·경로에 쓴다), 매핑을 모르는
    청크는 기존대로 모든 정상 CQ 를 받는다. falsey ``domains`` 를 범위 미정으로 넓히거나, 문자열을
    글자로·dict 를 key 로 순회하거나, 예외로 CQ 블록 전체를 잃으면 두 프롬프트가 갈린다.
    """
    kept = _neighbourhood()
    cq_env.write(kept)
    cached_clean, variable_clean = cq_env.build(mode)
    cq_env.write([kept[0], _bad_entry(kind, "5e2b", 6317), *kept[1:]])
    cached_bad, variable_bad = cq_env.build(mode)

    assert _rendered(variable_clean, kept) == (_MAPPED_RENDER if mode == "mapped" else _FALLBACK_RENDER)
    assert _paths_by_cq(variable_clean) == _NEIGHBOUR_PATHS
    assert variable_bad == variable_clean
    assert cached_bad == cached_clean


# ── S0 parser 호환과 저장 결과 소비 ──────────────────────────────────────


def test_s0_parser_formats_and_saved_entries_reach_s2_consumers(cq_env, caplog):
    """S0 parser 는 기존 형식을 그대로 받고, 저장된 결과를 S2 소비자가 항목 단위로 읽는다.

    구조 검증은 저장 CQ 소비 경계의 일이다. parser 와 저장 API 는 문자열 항목과 객체 항목을
    기존대로 받으므로, 문자열 ``domains`` 를 가진 저장 항목은 소비 단계에서 그 항목만 빠진다.
    """
    assert cqm._parse_user_cqs("  첫 질문 \n# 주석\n\n둘째 질문") == [
        {"question_ko": "첫 질문"}, {"question_ko": "둘째 질문"},
    ]
    assert cqm._parse_user_cqs('{"question_ko": "객체 하나"}') == [{"question_ko": '{"question_ko": "객체 하나"}'}]
    mixed = json.dumps([" 문자열 질문 ", {"question_ko": "객체 질문", "domains": "Alarm_Events", "extra": 1}],
                       ensure_ascii=False)
    assert cqm._parse_user_cqs(mixed) == [
        {"question_ko": "문자열 질문"},
        {"question_ko": "객체 질문", "domains": "Alarm_Events", "extra": 1},
    ]
    assert cqm._parse_user_cqs("   ") == []
    with pytest.raises(ValueError):
        cqm._parse_user_cqs("[1]")
    with pytest.raises(json.JSONDecodeError):
        cqm._parse_user_cqs('[{"question_ko": ')

    provided = json.dumps([
        "[S0-TEXT] 문자열 질문은?",
        {"question_ko": "[S0-PAIR] 알람과 설비 경로는?", "domains": list(_PAIR_DOMAINS)},
        {"question_ko": "[S0-BAD] 문자열 domains 질문은?", "domains": "Alarm_Events"},
    ], ensure_ascii=False)
    result = json.loads(cqm.generate_competency_questions(user_provided=provided))
    assert result["success"] is True and result["count"] == 3
    saved = json.loads(cq_env.path.read_text(encoding="utf-8"))
    assert [cq["id"] for cq in saved] == ["CQ01", "CQ02", "CQ03"]
    assert saved[0]["domains"] == [] and saved[2]["domains"] == "Alarm_Events"

    with caplog.at_level(logging.DEBUG):
        loaded = cq_env.load()
        _, variable = cq_env.build("mapped")

    assert loaded == saved[:2]
    assert "[S0-TEXT] 문자열 질문은?" in variable and "[S0-PAIR]" in variable
    assert "[S0-BAD]" not in variable
    assert "AlarmEvents → EquipmentMaster (FK 직접 연결)" in variable
    assert _warnings(caplog.records), "S0 가 저장한 문자열 domains 항목을 경고 없이 버렸다"


# ── loader: 정상 항목 그대로, 입력 파일 불변 ─────────────────────────────


def test_loader_returns_valid_entries_unchanged_and_leaves_file_intact(cq_env):
    """loader 는 정상 항목을 원래 순서·내용으로 돌려주고, 질문·id·추가 필드 형태는 따지지 않는다.

    ``domains`` 미지정은 미지정으로, null 은 null 로 남는다. 반환 객체를 고쳐도 다음 호출은 파일
    내용을 다시 돌려주고, loader·프롬프트 조립 모두 입력 파일을 바꾸지 않는다.
    """
    keep = [
        {"id": "UNICODE", "question_ko": "[UNICODE] 설비별 알람 ✓ 경로는?",
         "domains": [f"{NS_PREFIX}:AlarmEvents", "iof-core:MaterialArtifact",
                     f"<{DOMAIN_NS}SensorReadings>", _HANGUL_TABLE],
         "extra": {"nested": [1, {"deep": True}]}},
        {"question_en": "[NO-ID] Which alarms belong to equipment?", "domains": None},
        {"id": 17, "question_ko": None},
        {"id": "MIXED", "question_ko": "[MIXED] 혼합 목록은?",
         "domains": [None, "Alarm_Events", {"k": "v"}, 3, ["Equipment_Master"]]},
        {"id": "EMPTY", "domains": []},
        {},
    ]
    document = [
        keep[0], keep[1], "[DROP-ITEM] 문자열 항목", keep[2], keep[3],
        {"id": "DROP", "question_ko": "[DROP-DOMAINS] 질문은?", "domains": "Alarm_Events"},
        keep[4], keep[5],
    ]
    cq_env.write(document)
    original = cq_env.path.read_bytes()

    first = cq_env.load()
    assert first == keep
    first[0]["domains"].append("MUTATED")
    first.append({"id": "APPENDED"})
    assert cq_env.load() == keep

    _, variable = cq_env.build("unmapped_chunk")
    expected_display = f"  - 관련 도메인: {NS_PREFIX}:AlarmEvents, iof-core:MaterialArtifact, <{DOMAIN_NS}SensorReadings>\n"
    assert expected_display in variable
    assert "[NO-ID] Which alarms belong to equipment?" in variable and "[MIXED] 혼합 목록은?" in variable
    assert "[DROP-ITEM]" not in variable and "[DROP-DOMAINS]" not in variable
    assert cq_env.path.read_bytes() == original


# ── 최상위 형태와 JSON 구문·I/O 경계 ─────────────────────────────────────


@pytest.mark.parametrize(
    "root",
    [{"questions": [_cq("WRAPPED", ["Alarm_Events"])]}, "Alarm_Events", 42, None],
    ids=["object", "string", "number", "null"],
)
@pytest.mark.parametrize("consumer", ["builder", "loader"])
def test_non_list_root_yields_no_cqs_with_warning(cq_env, caplog, consumer, root):
    """배열이 아닌 최상위는 빈 목록이다. dict key 나 문자열 글자를 CQ 로 쓰지 않고 경고를 남긴다."""
    cq_env.write(root)

    with caplog.at_level(logging.DEBUG):
        if consumer == "builder":
            cached, variable = cq_env.build("unmapped_chunk")
            assert _CQ_HEADER not in variable and "관련 도메인" not in variable
            assert "[WRAPPED]" not in variable and "[WRAPPED]" not in cached
        else:
            assert cq_env.load() == []

    assert _warnings(caplog.records), "배열이 아닌 최상위를 경고 없이 버렸다"


@pytest.mark.parametrize("case", ["broken_json", "invalid_utf8", "directory", "missing"])
def test_syntax_and_io_errors_keep_existing_boundary(cq_env, case):
    """구조 검증은 JSON 구문·I/O 오류를 삼키지 않는다.

    loader 는 기존대로 예외를 올리고(파일이 없을 때만 빈 목록), 청크 조립은 기존 경계에서
    CQ 블록 없이 프롬프트를 만든다.
    """
    if case == "broken_json":
        cq_env.path.write_text('[{"id": "A", "domains": [', encoding="utf-8")
    elif case == "invalid_utf8":
        cq_env.path.write_bytes(b'[{"id": "\xff"}]')
    elif case == "directory":
        cq_env.path.mkdir()
    expected = {
        "broken_json": json.JSONDecodeError,
        "invalid_utf8": UnicodeDecodeError,
        "directory": OSError,
    }

    if case == "missing":
        assert cq_env.load() == []
    else:
        with pytest.raises(expected[case]):
            cq_env.load()
    _, variable = cq_env.build("unmapped_chunk")
    assert _CQ_HEADER not in variable
