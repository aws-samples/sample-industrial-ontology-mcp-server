"""Architect 청크 프롬프트의 CQ 선택과 FK 경로 분석이 같은 이름 해석을 쓰는지 고정한다.

``_build_chunk_prompt`` 는 호출자가 준 ``table_class_map`` 으로 관련 CQ 를 고른 뒤 같은
CQ 로 필수 OP 경로 블록을 만든다. 두 단계가 다른 매핑이나 다른 이름 정규화를 쓰면
선택된 질문의 경로가 빠지거나, 같은 클래스가 두 이름으로 갈라져 가짜 경로가 생긴다.
핵심 재현은 ``tests/test_architect_cq_paths.py`` 가 맡고, 이 파일은 그 경계를 본다:

* 명시한 빈 매핑과 매핑 미지정(기본 rules 파일 로드)의 구분, 한 인자 호출 호환
* prefix / IRI / 원본 테이블 alias 가 같은 로컬 클래스일 때 중복·자기 연결이 없다
* 등록된 외래 namespace 의 같은 local name 은 로컬 클래스와 합치지 않는다
* 미등록 prefix 는 ``resolve_entity_name`` 의 WARNING + 도메인 NS 폴백을 따른다
* 잘못된 domain 항목 하나가 다른 경로를 지우지 않는다

입력은 임시 CSV header·CQ JSON·rules 파일이고, 필터와 경로 분석기는 실제 구현을 그대로
호출한다. prefix·namespace 는 설정 상수에서 만든다 (리터럴은 타 도메인에서 no-op 이 된다).
"""
from __future__ import annotations

import json
import logging
import re

import pytest

import config
from domain import rules_paths, table_mapping
from domain.namespaces import DOMAIN_NS, FOREIGN_PREFIXES, NS_PREFIX
from tools import multi_agent_tbox as mat
from tools import tbox_generation as tg

_FK_PATTERNS = {
    "patterns": {
        "equipmentid": "EquipmentMaster",
        "productid": "ProductMaster",
    },
}

_TABLE_COLUMNS = {
    "Alarm_Events": ["Event_ID", "Equipment_ID", "Severity"],
    "Equipment_Master": ["Equipment_ID", "Name"],
    "Sensor_Readings": ["Reading_ID", "Equipment_ID", "Value"],
    "Material_Artifact": ["Artifact_ID", "Equipment_ID"],
    "Product_Master": ["Product_ID", "Name"],
}

#: rules 파일 매핑. ``Alarm_Events`` 는 밑줄 제거 폴백(``AlarmEvents``)과 다른 클래스로
#: 보내 "어느 매핑을 썼는가" 가 출력 이름으로 드러나게 한다.
_FILE_CLASSES = {
    "Alarm_Events": "ProcessResultD",
    "Equipment_Master": "EquipmentMaster",
    "Sensor_Readings": "SensorReadings",
    "Material_Artifact": "MaterialArtifact",
    "Product_Master": "ProductMaster",
}

_IOF_NS = FOREIGN_PREFIXES["iof-core"]
_UNREGISTERED_PREFIX = "zzfixture"

_HEADER_RE = re.compile(r"^\*\*(?P<cq>.+?)\*\* \(도메인:")
_DIRECT_RE = re.compile(r"^\s*- (\S+) → (\S+) \(FK 직접 연결\)$")
_HUB_RE = re.compile(r"^\s*- (\S+) → (\S+) ← (\S+) \(공유 FK 허브\)$")


def _cq(cq_id: str, domains) -> dict:
    # 괄호로 감싸 ``[DIRECT]`` 가 ``[SAME_DIRECT]`` 의 부분 문자열이 되지 않게 한다.
    return {"id": cq_id, "question_ko": f"[{cq_id}] 질문은 무엇인가?", "domains": domains}


def _paths_by_cq(text: str) -> dict[str, list[tuple[str, ...]]]:
    """필수 OP 블록을 ``{cq_id: [("direct", a, b) | ("hub", a, hub, b)]}`` 로 읽는다.

    경로가 없는 CQ 는 결과에서 뺀다. 블록 안에서 해석하지 못한 화살표 줄은 실패로
    올린다. 형식이 바뀌어 부정 단언이 공허하게 통과하는 것을 막는다.
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


def _selected_ids(variable: str, cqs: list[dict]) -> list[str]:
    return [cq["id"] for cq in cqs if cq["question_ko"] in variable]


@pytest.fixture
def prompt_env(tmp_path, monkeypatch):
    """임시 rules·CSV·CQ 파일로 청크 프롬프트를 만드는 빌더."""
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "fk_patterns.json").write_text(json.dumps(_FK_PATTERNS), encoding="utf-8")
    (rules / "table_class_mapping.json").write_text(
        json.dumps({"table_class_mapping": _FILE_CLASSES}), encoding="utf-8",
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
        @staticmethod
        def write_cqs(cqs: list[dict]) -> None:
            cq_path.write_text(json.dumps(cqs, ensure_ascii=False), encoding="utf-8")

        @staticmethod
        def build(tables: list[str], mapping: dict) -> tuple[str, str]:
            return tg._build_chunk_prompt(
                chunk_idx=1,
                chunk_tables=[{"name": t, "columns": _TABLE_COLUMNS[t]} for t in tables],
                total_chunks=3,
                iof_summary="fixture IOF context",
                schema_info={},
                relationships_info="",
                table_class_map=mapping,
            )

    return _Env()


# ── 매핑 출처: 명시한 빈 매핑 vs 미지정 ─────────────────────────────────


def test_explicit_empty_mapping_is_not_replaced_by_rules_file(prompt_env):
    """호출자가 ``{}`` 를 주면 선택과 경로 모두 그 빈 매핑을 쓴다.

    선택은 빈 매핑을 "모름" 으로 보고 질문을 전부 보존한다. 경로 분석이 여기서 rules
    파일을 다시 읽으면 선택이 본 적 없는 클래스 이름(``ProcessResultD``)이 프롬프트에
    들어간다. 빈 매핑을 미지정과 같게 취급하는 ``mapping or load()`` 형태가 이 경계다.
    """
    cqs = [
        _cq("DIRECT", ["Alarm_Events", "Equipment_Master"]),
        _cq("SHARED", ["Alarm_Events", "Sensor_Readings"]),
        _cq("UNSCOPED", None),
        _cq("PRODUCT", ["Product_Master"]),
    ]
    prompt_env.write_cqs(cqs)

    cached_empty, variable = prompt_env.build(["Alarm_Events"], {})

    assert _selected_ids(variable, cqs) == ["DIRECT", "SHARED", "UNSCOPED", "PRODUCT"]
    assert _paths_by_cq(variable) == {
        "DIRECT": [("direct", "AlarmEvents", "EquipmentMaster")],
        "SHARED": [("hub", "AlarmEvents", "EquipmentMaster", "SensorReadings")],
    }
    assert "ProcessResultD" not in variable

    cached_file, _ = prompt_env.build(["Alarm_Events"], dict(_FILE_CLASSES))
    assert cached_empty == cached_file, "매핑 차이가 정적 prefix 캐시 키를 바꿨다"


def test_single_argument_analysis_still_loads_rules_mapping(prompt_env):
    """기존 한 인자 호출(S2 Multi-Agent 경로)은 기본 rules 파일 매핑을 쓴다."""
    cqs = [
        _cq("DIRECT", ["Alarm_Events", "Equipment_Master"]),
        _cq("SHARED", ["Alarm_Events", "Sensor_Readings"]),
        _cq("PRODUCT", ["Product_Master"]),
        _cq("UNSCOPED", None),
    ]

    block = mat._analyze_cq_required_ops(cqs)

    assert _paths_by_cq(block) == {
        "DIRECT": [("direct", "ProcessResultD", "EquipmentMaster")],
        "SHARED": [("hub", "ProcessResultD", "EquipmentMaster", "SensorReadings")],
    }
    assert mat._analyze_cq_required_ops([]) == ""


def test_single_argument_analysis_resolves_prefixed_domain_classes(prompt_env):
    """한 인자 호출도 prefix·도메인 IRI 로 적은 클래스를 FK 경로의 같은 클래스로 맞춘다."""
    cqs = [
        _cq("PREFIXED", [f"{NS_PREFIX}:ProcessResultD", "Equipment_Master"]),
        _cq("IRI", [DOMAIN_NS + "ProcessResultD", f"{NS_PREFIX}:SensorReadings"]),
    ]

    block = mat._analyze_cq_required_ops(cqs)

    assert _paths_by_cq(block) == {
        "PREFIXED": [("direct", "ProcessResultD", "EquipmentMaster")],
        "IRI": [("hub", "ProcessResultD", "EquipmentMaster", "SensorReadings")],
    }


# ── 같은 로컬 클래스의 여러 표기 ─────────────────────────────────────


def test_same_class_in_several_notations_yields_one_path_without_self_link(prompt_env):
    """원본 alias·prefix·IRI·클래스명이 한 클래스면 경로는 한 번만, 자기 연결 없이 나온다.

    공유 허브 판정은 두 도메인의 FK 대상 교집합만 보므로 같은 클래스가 두 번 들어가면
    ``X → Hub ← X`` 가 만들어진다. 이름 정규화만 추가하고 중복을 정리하지 않으면 이
    가짜 경로가 오히려 늘어난다.
    """
    cqs = [
        _cq("SAME_ONLY", [
            "Alarm_Events", f"{NS_PREFIX}:ProcessResultD",
            DOMAIN_NS + "ProcessResultD", "ProcessResultD",
        ]),
        _cq("SAME_HUB", ["Alarm_Events", f"{NS_PREFIX}:ProcessResultD", "Sensor_Readings"]),
        _cq("SAME_DIRECT", [DOMAIN_NS + "ProcessResultD", "Alarm_Events", "Equipment_Master"]),
        _cq("UNRELATED", ["Product_Master"]),
        _cq("UNSCOPED", None),
    ]
    prompt_env.write_cqs(cqs)

    cached, variable = prompt_env.build(["Alarm_Events"], dict(_FILE_CLASSES))

    assert _selected_ids(variable, cqs) == ["SAME_ONLY", "SAME_HUB", "SAME_DIRECT", "UNSCOPED"]
    assert _paths_by_cq(variable) == {
        "SAME_HUB": [("hub", "ProcessResultD", "EquipmentMaster", "SensorReadings")],
        "SAME_DIRECT": [("direct", "ProcessResultD", "EquipmentMaster")],
    }
    assert all(cq["question_ko"] not in cached for cq in cqs)
    assert "FK 직접 연결" not in cached and "공유 FK 허브" not in cached


# ── 외래 namespace 의 같은 local name ─────────────────────────────────


def test_foreign_local_name_does_not_select_local_class_chunk(prompt_env):
    """``iof-core:MaterialArtifact`` 는 로컬 ``MaterialArtifact`` 청크의 질문이 아니다."""
    cqs = [
        _cq("LOCAL_PREFIXED", [f"{NS_PREFIX}:MaterialArtifact"]),
        _cq("LOCAL_IRI", [DOMAIN_NS + "MaterialArtifact"]),
        _cq("FOREIGN_PREFIXED", ["iof-core:MaterialArtifact"]),
        _cq("FOREIGN_IRI", [_IOF_NS + "MaterialArtifact"]),
    ]
    prompt_env.write_cqs(cqs)

    _, variable = prompt_env.build(["Material_Artifact"], dict(_FILE_CLASSES))

    assert _selected_ids(variable, cqs) == ["LOCAL_PREFIXED", "LOCAL_IRI"]


def test_foreign_local_name_does_not_borrow_local_fk_paths(prompt_env):
    """다른 도메인으로 선택된 CQ 에서도 외래 클래스는 로컬 클래스의 FK 경로를 얻지 않는다."""
    cqs = [
        _cq("LOCAL_HUB", [f"{NS_PREFIX}:MaterialArtifact", "Sensor_Readings"]),
        _cq("FOREIGN_HUB", ["iof-core:MaterialArtifact", "Sensor_Readings"]),
        _cq("FOREIGN_IRI_HUB", [_IOF_NS + "MaterialArtifact", "Sensor_Readings"]),
    ]
    prompt_env.write_cqs(cqs)

    _, variable = prompt_env.build(["Sensor_Readings"], dict(_FILE_CLASSES))

    assert _selected_ids(variable, cqs) == ["LOCAL_HUB", "FOREIGN_HUB", "FOREIGN_IRI_HUB"]
    assert _paths_by_cq(variable) == {
        "LOCAL_HUB": [("hub", "MaterialArtifact", "EquipmentMaster", "SensorReadings")],
    }


# ── 미등록 prefix 와 잘못된 항목 ──────────────────────────────────────


def test_unregistered_prefix_keeps_warning_and_domain_fallback(prompt_env, caplog):
    """미등록 prefix 는 공용 해석기 계약대로 WARNING 후 도메인 클래스로 해석한다."""
    assert _UNREGISTERED_PREFIX not in FOREIGN_PREFIXES
    assert _UNREGISTERED_PREFIX != NS_PREFIX
    cqs = [_cq("TYPO", [f"{_UNREGISTERED_PREFIX}:ProcessResultD", "Equipment_Master"])]
    prompt_env.write_cqs(cqs)

    with caplog.at_level(logging.WARNING, logger="domain.graph_utils"):
        _, variable = prompt_env.build(["Alarm_Events"], dict(_FILE_CLASSES))

    assert _selected_ids(variable, cqs) == ["TYPO"]
    assert _paths_by_cq(variable) == {
        "TYPO": [("direct", "ProcessResultD", "EquipmentMaster")],
    }
    assert any(
        record.name == "domain.graph_utils"
        and record.levelno == logging.WARNING
        and _UNREGISTERED_PREFIX in record.getMessage()
        for record in caplog.records
    ), "미등록 prefix 경고가 공용 해석기에서 나오지 않았다"


@pytest.mark.parametrize("bad", ["", ":"], ids=["empty", "colon"])
def test_malformed_domain_entry_does_not_erase_other_paths(prompt_env, bad):
    """선택이 건너뛰는 항목은 경로 분석도 건너뛰고, 같은 CQ·다른 CQ 경로를 지우지 않는다.

    공용 해석기는 빈 이름을 ``ValueError`` 로 거부한다. 경로 분석 전체가 그 예외로
    중단되면 프롬프트 조립부가 블록을 조용히 버려 정상 경로까지 사라진다.
    """
    cqs = [
        _cq("BAD", [bad, "Alarm_Events", "Equipment_Master"]),
        _cq("GOOD", ["Alarm_Events", "Sensor_Readings"]),
    ]
    prompt_env.write_cqs(cqs)

    _, variable = prompt_env.build(["Alarm_Events"], dict(_FILE_CLASSES))

    assert _selected_ids(variable, cqs) == ["BAD", "GOOD"]
    assert _paths_by_cq(variable) == {
        "BAD": [("direct", "ProcessResultD", "EquipmentMaster")],
        "GOOD": [("hub", "ProcessResultD", "EquipmentMaster", "SensorReadings")],
    }
