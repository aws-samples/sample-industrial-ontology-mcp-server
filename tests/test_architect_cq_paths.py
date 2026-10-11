"""각 Architect 청크에 관련 CQ와 실제 FK 경로가 전달되는지 검증한다."""
from __future__ import annotations

import json

import pytest

_FK_PATTERNS = {
    "patterns": {
        "equipmentid": "EquipmentMaster",
        "productid": "ProductMaster",
    }
}


def _write_rules(tmp_path):
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "fk_patterns.json").write_text(json.dumps(_FK_PATTERNS), encoding="utf-8")
    return str(rules)


def _write_csv(tmp_path, name: str, headers: list[str]) -> None:
    raw = tmp_path / "rawdata"
    raw.mkdir(exist_ok=True)
    p = raw / f"{name}.csv"
    p.write_text(",".join(headers) + "\n", encoding="utf-8")


_TABLE_COLUMNS = {
    "Alarm_Events": ["Event_ID", "Equipment_ID", "Severity"],
    "Equipment_Master": ["Equipment_ID", "Name"],
    "Sensor_Readings": ["Reading_ID", "Equipment_ID", "Value"],
    "Product_Master": ["Product_ID", "Name"],
}
_TABLE_CLASSES = {
    "Alarm_Events": "AlarmEvents",
    "Equipment_Master": "EquipmentMaster",
    "Sensor_Readings": "SensorReadings",
    "Product_Master": "ProductMaster",
}


@pytest.fixture(scope="function")
def cq_inputs(tmp_path, monkeypatch):
    import config
    from domain import rules_paths, table_mapping
    from tools import multi_agent_tbox as mat
    from tools import tbox_generation as tg

    rules_dir = _write_rules(tmp_path)
    mapping_path = tmp_path / "rules" / "table_class_mapping.json"
    mapping_path.write_text(
        json.dumps({"table_class_mapping": _TABLE_CLASSES}), encoding="utf-8",
    )
    for name, columns in _TABLE_COLUMNS.items():
        _write_csv(tmp_path, name, columns)
    cqs = [
        {"id": "DIRECT_ALARM", "question_ko": "해당 설비의 알람을 연결하는 직접 경로는?",
         "domains": ["Alarm_Events", "Equipment_Master"]},
        {"id": "SHARED_SENSOR", "question_ko": "같은 설비를 공유하는 알람과 센서 경로는?",
         "domains": ["Alarm_Events", "Sensor_Readings"]},
        {"id": "PRODUCT_ONLY", "question_ko": "제품 마스터의 제품 이름은?",
         "domains": ["Product_Master"]},
        {"id": "UNSCOPED", "question_ko": "도메인 범위가 아직 지정되지 않은 질문은?",
         "domains": []},
    ]
    cq_path = tmp_path / "competency_questions.json"
    cq_path.write_text(json.dumps(cqs, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path))
    monkeypatch.setattr(tg, "COMPETENCY_QUESTIONS_PATH", str(cq_path))
    monkeypatch.setattr(mat, "_CQ_PATH", str(cq_path))
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata"))
    monkeypatch.setattr(rules_paths, "RULES_ROOT", rules_dir)
    monkeypatch.setattr(table_mapping, "RULES_ROOT", rules_dir)
    monkeypatch.setattr(table_mapping, "_CACHE", {})
    monkeypatch.setattr(tg, "_RULES_DIR", rules_dir)
    monkeypatch.setattr(mat, "_RULES_DIR", rules_dir)
    monkeypatch.setattr(tg, "TBOX_BASELINE_PATH", str(tmp_path / "no_baseline.ttl"))
    return {"path": cq_path, "questions": cqs}


def _build_cq_prompt(chunk_idx, table="Alarm_Events", mapping=None):
    from tools import tbox_generation as tg

    return tg._build_chunk_prompt(
        chunk_idx=chunk_idx,
        chunk_tables=[{"name": table, "columns": _TABLE_COLUMNS[table]}],
        total_chunks=6,
        iof_summary="fixture IOF context",
        schema_info={},
        relationships_info="",
        table_class_map=_TABLE_CLASSES if mapping is None else mapping,
    )


@pytest.mark.parametrize("chunk_idx", [0, 1, 5])
def test_relevant_cq_paths_reach_first_and_later_chunks(cq_inputs, chunk_idx):
    """각 독립 호출에 관련 질문과 실제 CSV에서 도출한 직접·공유 FK 경로를 준다."""
    cached, variable = _build_cq_prompt(chunk_idx)
    direct, shared, unrelated, unscoped = cq_inputs["questions"]

    for cq in (direct, shared, unscoped):
        assert cq["question_ko"] in variable
    assert unrelated["question_ko"] not in variable
    assert "AlarmEvents → EquipmentMaster (FK 직접 연결)" in variable
    assert "AlarmEvents → EquipmentMaster ← SensorReadings (공유 FK 허브)" in variable
    assert '"Alarm_Events"' in variable and '"Equipment_ID"' in variable
    assert all(cq["question_ko"] not in cached for cq in cq_inputs["questions"])


@pytest.mark.parametrize("chunk_idx", [0, 1, 5])
def test_other_chunk_excludes_unrelated_cqs_and_paths(cq_inputs, chunk_idx):
    _, variable = _build_cq_prompt(chunk_idx, table="Product_Master")
    direct, shared, product, unscoped = cq_inputs["questions"]

    assert product["question_ko"] in variable
    assert unscoped["question_ko"] in variable
    assert direct["question_ko"] not in variable
    assert shared["question_ko"] not in variable
    assert "AlarmEvents → EquipmentMaster" not in variable


def test_cq_changes_leave_cached_prefix_stable(cq_inputs):
    cached_first, _ = _build_cq_prompt(0)
    questions = cq_inputs["questions"]
    questions[0]["question_ko"] = "수정된 알람 질문을 새 호출에 반영했는가?"
    cq_inputs["path"].write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")

    cached_later, variable = _build_cq_prompt(1)

    assert cached_later == cached_first
    assert questions[0]["question_ko"] in variable
    assert questions[0]["question_ko"] not in cached_later


def test_unknown_chunk_mapping_preserves_cq_requirements(cq_inputs):
    _, variable = _build_cq_prompt(1, mapping={})

    assert all(cq["question_ko"] in variable for cq in cq_inputs["questions"])


@pytest.mark.parametrize("chunk_idx", [0, 1, 5])
def test_noncanonical_class_mapping_preserves_cqs_and_fk_paths(cq_inputs, tmp_path, chunk_idx):
    """비정규 테이블 매핑에서도 질문 선택과 실제 FK 분석이 같은 클래스를 사용한다."""
    mapping = {**_TABLE_CLASSES, "Alarm_Events": "ProcessResultD"}
    (tmp_path / "rules" / "table_class_mapping.json").write_text(
        json.dumps({"table_class_mapping": mapping}), encoding="utf-8",
    )

    cached, variable = _build_cq_prompt(chunk_idx, mapping=mapping)
    direct, shared, unrelated, unscoped = cq_inputs["questions"]

    assert direct["question_ko"] in variable
    assert shared["question_ko"] in variable
    assert unscoped["question_ko"] in variable
    assert unrelated["question_ko"] not in variable
    assert "ProcessResultD → EquipmentMaster (FK 직접 연결)" in variable
    assert "ProcessResultD → EquipmentMaster ← SensorReadings (공유 FK 허브)" in variable
    assert direct["question_ko"] not in cached


@pytest.mark.parametrize("chunk_idx", [0, 1])
@pytest.mark.parametrize("rules_mapping", ["different", "absent"])
def test_chunk_mapping_is_also_used_for_fk_paths(
    cq_inputs, tmp_path, chunk_idx, rules_mapping,
):
    """호출자가 준 별칭을 파일의 오래된 값이나 파일 부재가 덮어쓰면 실패한다."""
    mapping = {**_TABLE_CLASSES, "Alarm_Events": "ProcessResultD"}
    if rules_mapping == "absent":
        (tmp_path / "rules" / "table_class_mapping.json").unlink()

    cached, variable = _build_cq_prompt(chunk_idx, mapping=mapping)
    direct, shared, unrelated, _ = cq_inputs["questions"]

    assert direct["question_ko"] in variable
    assert shared["question_ko"] in variable
    assert unrelated["question_ko"] not in variable
    assert "ProcessResultD → EquipmentMaster (FK 직접 연결)" in variable
    assert "ProcessResultD → EquipmentMaster ← SensorReadings (공유 FK 허브)" in variable
    assert "AlarmEvents → EquipmentMaster" not in variable
    assert direct["question_ko"] not in cached


@pytest.mark.parametrize("chunk_idx", [0, 1])
@pytest.mark.parametrize("notation", ["prefix", "iri", "bracketed_iri"])
def test_qualified_cq_domains_keep_their_fk_paths(cq_inputs, chunk_idx, notation):
    """같은 클래스의 namespace 표기 차이가 질문이나 FK 경로를 누락시키면 실패한다."""
    from domain.namespaces import DOMAIN_NS, NS_PREFIX

    questions = cq_inputs["questions"]
    for cq, classes in zip(
        questions[:2],
        [("AlarmEvents", "EquipmentMaster"), ("AlarmEvents", "SensorReadings")],
        strict=True,
    ):
        if notation == "prefix":
            cq["domains"] = [f"{NS_PREFIX}:{name}" for name in classes]
        elif notation == "iri":
            cq["domains"] = [f"{DOMAIN_NS}{name}" for name in classes]
        else:
            cq["domains"] = [f"<{DOMAIN_NS}{name}>" for name in classes]
    cq_inputs["path"].write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")

    cached, variable = _build_cq_prompt(chunk_idx)

    assert questions[0]["question_ko"] in variable
    assert questions[1]["question_ko"] in variable
    assert questions[2]["question_ko"] not in variable
    assert "**DIRECT_ALARM**" in variable
    assert "**SHARED_SENSOR**" in variable
    assert "AlarmEvents → EquipmentMaster (FK 직접 연결)" in variable
    assert "AlarmEvents → EquipmentMaster ← SensorReadings (공유 FK 허브)" in variable
    assert questions[0]["question_ko"] not in cached


@pytest.mark.parametrize("qualified_target", [False, True])
def test_declared_fk_class_name_keeps_underscores(cq_inputs, tmp_path, qualified_target):
    """FK 패턴에 선언된 클래스명에 bare 테이블명의 폴백을 적용하지 않는다."""
    from domain.namespaces import NS_PREFIX

    mapping = {**_TABLE_CLASSES, "Equipment_Master": "Equipment_Node"}
    target = f"{NS_PREFIX}:Equipment_Node" if qualified_target else "Equipment_Node"
    (tmp_path / "rules" / "fk_patterns.json").write_text(json.dumps({
        "patterns": {"equipmentid": target, "productid": "ProductMaster"},
    }), encoding="utf-8")

    _, variable = _build_cq_prompt(1, mapping=mapping)

    assert "AlarmEvents → Equipment_Node (FK 직접 연결)" in variable
    assert "AlarmEvents → Equipment_Node ← SensorReadings (공유 FK 허브)" in variable


def test_equivalent_class_references_keep_fks_from_both_tables(cq_inputs, tmp_path):
    """같은 클래스의 bare·prefix 표기가 합쳐져도 각 CSV의 FK를 모두 보존한다."""
    from domain.namespaces import NS_PREFIX

    mapping = {
        **_TABLE_CLASSES,
        "Alarm_Events": "EventRecord",
        "Sensor_Readings": f"{NS_PREFIX}:EventRecord",
    }
    _write_csv(tmp_path, "Sensor_Readings", ["Reading_ID", "Product_ID"])
    questions = cq_inputs["questions"]
    questions[1]["domains"] = ["Sensor_Readings", "Product_Master"]
    cq_inputs["path"].write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")

    _, variable = _build_cq_prompt(1, mapping=mapping)

    assert "EventRecord → EquipmentMaster (FK 직접 연결)" in variable
    assert "EventRecord → ProductMaster (FK 직접 연결)" in variable


@pytest.mark.parametrize("chunk_idx", [0, 1])
def test_null_domains_do_not_drop_other_cqs_or_fk_paths(cq_inputs, chunk_idx):
    """범위 미정 CQ 한 건이 같은 청크의 정상 질문과 경로를 지우면 실패한다."""
    questions = cq_inputs["questions"]
    questions[-1]["domains"] = None
    cq_inputs["path"].write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")

    _, variable = _build_cq_prompt(chunk_idx)

    for cq in (questions[0], questions[1], questions[-1]):
        assert cq["question_ko"] in variable
    assert questions[2]["question_ko"] not in variable
    assert "AlarmEvents → EquipmentMaster (FK 직접 연결)" in variable
    assert "AlarmEvents → EquipmentMaster ← SensorReadings (공유 FK 허브)" in variable


@pytest.mark.parametrize("chunk_idx", [0, 1])
def test_cq_questions_survive_optional_path_analysis_failure(cq_inputs, monkeypatch, chunk_idx):
    from tools import multi_agent_tbox as mat

    calls = []

    def fail_path_analysis(_cqs, table_class_map=None):
        calls.append(table_class_map)
        raise RuntimeError("fixture path analysis failed")

    monkeypatch.setattr(mat, "_analyze_cq_required_ops", fail_path_analysis)

    _, variable = _build_cq_prompt(chunk_idx)

    assert calls == [_TABLE_CLASSES]
    assert cq_inputs["questions"][0]["question_ko"] in variable
    assert cq_inputs["questions"][1]["question_ko"] in variable
    assert "AlarmEvents → EquipmentMaster" not in variable


# ── R28 E: 중간 추상 클래스 계층 규칙 ─────────────────────────────────


def test_hierarchy_rule_always_present_in_cached_prefix(tmp_path):
    """원칙 블록은 힌트 유무와 무관하게 항상 cached_prefix 에 들어간다."""
    # 힌트 파일 없는 환경 — _RULES_DIR 를 빈 tmp_path 로 교체
    from unittest.mock import patch

    from tools import tbox_generation as tg
    with patch.object(tg, "_RULES_DIR", str(tmp_path)):
        cached_prefix, _variable = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "T1", "columns": ["a"]}],
            total_chunks=1, iof_summary="",
            schema_info={}, relationships_info="",
            table_class_map={"T1": "T1"},
        )
    # 원칙 블록 시그니처
    assert "최상위 비율 ≤ 20%" in cached_prefix
    assert "자가 검증 체크" in cached_prefix


def test_hierarchy_hints_injected_when_file_present(tmp_path):
    """rules/domain/abstract_group_hints.json 이 있으면 그룹 힌트가 cached_prefix 에 주입."""
    import json as _j

    from tools import tbox_generation as tg

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "abstract_group_hints.json").write_text(_j.dumps({
        "groups": [
            {"abstract_class": "MyAbstract",
             "rationale": "테스트 그룹",
             "child_examples": ["ChildA", "ChildB"]},
        ],
    }), encoding="utf-8")

    from unittest.mock import patch
    with patch.object(tg, "_RULES_DIR", str(rules)):
        cached_prefix, _var = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "T1", "columns": ["a"]}],
            total_chunks=1, iof_summary="",
            schema_info={}, relationships_info="",
            table_class_map={"T1": "T1"},
        )
    assert "MyAbstract" in cached_prefix
    assert "테스트 그룹" in cached_prefix
    assert "도메인 중간 추상 클래스 힌트" in cached_prefix


def test_hierarchy_hints_absent_when_file_missing(tmp_path):
    """힌트 파일 없으면 힌트 블록은 비어있지만 원칙은 유지 — 도메인 중립 기본값."""
    from unittest.mock import patch

    from tools import tbox_generation as tg
    with patch.object(tg, "_RULES_DIR", str(tmp_path)):
        cached_prefix, _var = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "T1", "columns": ["a"]}],
            total_chunks=1, iof_summary="",
            schema_info={}, relationships_info="",
            table_class_map={"T1": "T1"},
        )
    assert "도메인 중간 추상 클래스 힌트" not in cached_prefix
    # 원칙은 여전히 있음
    assert "중간 추상 클래스 계층 규칙" in cached_prefix


def test_hierarchy_hints_empty_groups_treated_as_absent(tmp_path):
    """groups=[] 빈 배열이면 힌트 블록 생략."""
    import json as _j

    from tools import tbox_generation as tg

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "abstract_group_hints.json").write_text(
        _j.dumps({"groups": []}), encoding="utf-8")

    from unittest.mock import patch
    with patch.object(tg, "_RULES_DIR", str(rules)):
        cached_prefix, _var = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "T1", "columns": ["a"]}],
            total_chunks=1, iof_summary="",
            schema_info={}, relationships_info="",
            table_class_map={"T1": "T1"},
        )
    assert "도메인 중간 추상 클래스 힌트" not in cached_prefix


def test_load_abstract_group_hints_returns_empty_on_parse_error(tmp_path):
    """JSON 파싱 실패해도 예외 던지지 않고 [] 반환 — 파이프라인 정지 방지."""
    from tools import tbox_generation as tg

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "abstract_group_hints.json").write_text(
        "this is not json", encoding="utf-8")

    from unittest.mock import patch
    with patch.object(tg, "_RULES_DIR", str(rules)):
        result = tg._load_abstract_group_hints()
    assert result == []


# ── R28 (2026-05-04): domain/range 스코프 규칙 ────────────────────────


def test_domain_scope_rule_in_cached_prefix(tmp_path):
    """Architect 프롬프트에 IOF/BFO domain 금지 규칙이 포함되는지.

    실측 회귀 (2026-05-04): steel:hasAlarmTag rdfs:domain iof-core:MaterialArtifact.
    cached_prefix 에 _domain_scope_rule 이 들어가 Architect 가 처음부터 IOF 를
    domain 으로 쓰지 않도록 가이드.
    """
    from unittest.mock import patch

    from tools import tbox_generation as tg

    with patch.object(tg, "_RULES_DIR", str(tmp_path)):
        cached_prefix, _var = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "T1", "columns": ["a"]}],
            total_chunks=1, iof_summary="",
            schema_info={}, relationships_info="",
            table_class_map={"T1": "T1"},
        )
    # 핵심 시그니처 문자열
    assert "domain/range 스코프 규칙" in cached_prefix
    # IOF 상위 온톨로지 클래스 언급
    assert "iof-core" in cached_prefix or "IOF" in cached_prefix
    # 올바른 예와 금지 예 모두 포함
    assert "올바른 예" in cached_prefix and "금지 예" in cached_prefix
    # 자가 검증 체크도 포함
    assert "자가 검증 체크" in cached_prefix
