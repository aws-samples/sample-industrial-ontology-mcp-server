"""Round 1 Architect 프롬프트에 CQ OP 경로 주입 검증."""
from __future__ import annotations

import json
from unittest.mock import patch

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


def test_cq_op_paths_injected_into_first_chunk_prompt(tmp_path):
    """is_first=True 청크에서 _analyze_cq_required_ops 결과가 프롬프트에 포함되어야 한다."""
    from tools import multi_agent_tbox as mat
    from tools import tbox_generation as tg

    rules_dir = _write_rules(tmp_path)
    _write_csv(tmp_path, "Alarm_Events", ["Event_ID", "Equipment_ID", "Severity"])
    _write_csv(tmp_path, "Equipment_Master", ["Equipment_ID", "Name"])

    cqs = [
        {"id": "CQ01",
         "question_ko": "설비별 알람 이벤트는?",
         "domains": ["Alarm_Events", "Equipment_Master"]},
    ]
    # R27: CQ 파일은 query_tests/ 서브디렉토리 + config 상수 COMPETENCY_QUESTIONS_PATH 로 읽음
    qt_dir = tmp_path / "query_tests"
    qt_dir.mkdir(exist_ok=True)
    cq_path = qt_dir / "competency_questions.json"
    cq_path.write_text(json.dumps(cqs, ensure_ascii=False), encoding="utf-8")

    import config
    with patch.object(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path)), \
         patch.object(tg, "COMPETENCY_QUESTIONS_PATH", str(cq_path)), \
         patch.object(mat, "_CQ_PATH", str(cq_path)), \
         patch.object(mat, "_RULES_DIR", rules_dir), \
         patch("config.SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata")):
        cached_prefix, variable_prompt = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "Alarm_Events",
                            "columns": ["Event_ID", "Equipment_ID"]}],
            total_chunks=1,
            iof_summary="(없음)",
            schema_info={},
            relationships_info="",
            table_class_map={"Alarm_Events": "AlarmEvents"},
        )
    prompt = cached_prefix + "\n" + variable_prompt

    # CQ 경로 블록의 시그니처 문자열
    assert "필수 ObjectProperty" in prompt or "필수 OP" in prompt
    # 경로 후보 중 하나는 최소 포함 (정확 포맷은 구현 의존 — 시그니처만 확인)
    assert "AlarmEvents" in prompt or "alarmevents" in prompt.lower()


def test_non_first_chunk_does_not_inject_cq_paths(tmp_path):
    """첫 청크가 아니면 CQ 경로 블록이 프롬프트에 포함되지 않아야 한다(토큰 절약)."""
    from tools import multi_agent_tbox as mat
    from tools import tbox_generation as tg

    rules_dir = _write_rules(tmp_path)
    _write_csv(tmp_path, "Alarm_Events", ["Event_ID", "Equipment_ID"])

    cqs = [{"id": "CQ01", "question_ko": "q?", "domains": ["Alarm_Events", "Equipment_Master"]}]
    qt_dir = tmp_path / "query_tests"
    qt_dir.mkdir(exist_ok=True)
    cq_path = qt_dir / "competency_questions.json"
    cq_path.write_text(json.dumps(cqs, ensure_ascii=False), encoding="utf-8")

    import config
    with patch.object(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path)), \
         patch.object(tg, "COMPETENCY_QUESTIONS_PATH", str(cq_path)), \
         patch.object(mat, "_CQ_PATH", str(cq_path)), \
         patch.object(mat, "_RULES_DIR", rules_dir), \
         patch("config.SOURCE_RAWDATA_DIR", str(tmp_path / "rawdata")):
        cached_prefix, variable_prompt = tg._build_chunk_prompt(
            chunk_idx=1,  # 두 번째 청크
            chunk_tables=[{"name": "Alarm_Events", "columns": ["Event_ID"]}],
            total_chunks=2,
            iof_summary="",
            schema_info={},
            relationships_info="",
            table_class_map={"Alarm_Events": "AlarmEvents"},
        )
    prompt = cached_prefix + "\n" + variable_prompt

    # is_first=False 일 때 Competency Questions 블록 자체가 비어야 함
    assert "Competency Questions (T-Box가 반드시" not in prompt


def test_cq_op_injection_failure_is_silent(tmp_path):
    """_analyze_cq_required_ops 가 예외를 내도 프롬프트 생성이 실패하지 않아야 한다."""
    from tools import multi_agent_tbox as mat
    from tools import tbox_generation as tg

    _write_csv(tmp_path, "Alarm_Events", ["Event_ID"])
    qt_dir = tmp_path / "query_tests"
    qt_dir.mkdir(exist_ok=True)
    cq_path = qt_dir / "competency_questions.json"
    cq_path.write_text(
        json.dumps([{"id": "CQ01", "question_ko": "q?", "domains": ["A", "B"]}],
                   ensure_ascii=False), encoding="utf-8")

    import config
    with patch.object(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path)), \
         patch.object(tg, "COMPETENCY_QUESTIONS_PATH", str(cq_path)), \
         patch.object(mat, "_CQ_PATH", str(cq_path)), \
         patch("tools.multi_agent_tbox._analyze_cq_required_ops",
                side_effect=RuntimeError("boom")):
        cached_prefix, variable_prompt = tg._build_chunk_prompt(
            chunk_idx=0,
            chunk_tables=[{"name": "Alarm_Events", "columns": ["Event_ID"]}],
            total_chunks=1,
            iof_summary="",
            schema_info={},
            relationships_info="",
            table_class_map={"Alarm_Events": "AlarmEvents"},
        )
    prompt = cached_prefix + "\n" + variable_prompt
    # 프롬프트 생성 자체는 성공. CQ 질문 목록은 여전히 포함.
    assert "Competency Questions" in prompt


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
