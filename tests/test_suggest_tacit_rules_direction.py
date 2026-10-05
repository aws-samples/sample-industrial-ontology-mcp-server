"""Tests for suggest_tacit_rules — OP 방향 제약 프롬프트 + write-time 검증.

2026-05-10 fix: suggest_tacit_rules 가 LLM 에게 ``op`` 의 domain/range 를
source/target 과 일치시키도록 프롬프트 제약을 명시. 응답 저장 전에
validate_op_direction 으로 각 mapping 을 검증하고, reversed 면 inverse 로
auto-correct + `_auto_correction` annotation, invalid 면
`_direction_warning` 필드 추가 (삭제는 안 함 — SME 검토용).
"""
from __future__ import annotations

import json
from pathlib import Path

_TBOX_TTL = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:TagMaster a owl:Class .
steel:EquipmentMaster a owl:Class .
steel:AlarmEvents a owl:Class .

steel:equipmentHasTag a owl:ObjectProperty ;
    rdfs:domain steel:EquipmentMaster ;
    rdfs:range steel:TagMaster ;
    owl:inverseOf steel:tagBelongsToEquipment .

steel:tagBelongsToEquipment a owl:ObjectProperty ;
    rdfs:domain steel:TagMaster ;
    rdfs:range steel:EquipmentMaster ;
    owl:inverseOf steel:equipmentHasTag .

steel:alarmOccurredOnEquipment a owl:ObjectProperty ;
    rdfs:domain steel:AlarmEvents ;
    rdfs:range steel:EquipmentMaster .
"""


def _setup_project_files(tmp_path: Path, monkeypatch, tbox_ttl: str = _TBOX_TTL):
    """Create minimal project structure for suggest_tacit_rules tests."""
    import csv

    import config
    import tools.tacit_rules as tr

    rawdata = tmp_path / "rawdata"
    tacit = tmp_path / "tacit"
    tbox_path = tmp_path / "t_box.ttl"
    cq_path = tmp_path / "competency_questions.json"
    suggested_path = tmp_path / "rules" / "tacit_rules.suggested.json"
    suggested_path.parent.mkdir(parents=True)

    rawdata.mkdir()
    tacit.mkdir()
    tbox_path.write_text(tbox_ttl, encoding="utf-8")
    cq_path.write_text(json.dumps({
        "questions": [
            {"id": "CQ01", "domains": ["equipment", "tag"], "question_ko": "test"},
        ],
    }), encoding="utf-8")

    # Minimal CSVs
    for name in ("Tag_Master.csv", "Equipment_Master.csv"):
        with open(rawdata / name, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if name == "Tag_Master.csv":
                w.writerow(["Tag_ID", "Equipment_ID"])
                w.writerow(["TAG001", "EQ001"])
            else:
                w.writerow(["Equipment_ID", "Equipment_Name"])
                w.writerow(["EQ001", "Motor"])

    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox_path))
    monkeypatch.setattr(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path))
    monkeypatch.setattr(tr, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tr, "SOURCE_TACIT_DIR", str(tacit))
    # _SUGGESTED_RULES_PATH 는 모듈 레벨 상수일 수 있음 — 존재하면 override
    if hasattr(tr, "_SUGGESTED_RULES_PATH"):
        monkeypatch.setattr(tr, "_SUGGESTED_RULES_PATH", str(suggested_path))

    return {
        "rawdata": rawdata,
        "tacit": tacit,
        "tbox_path": tbox_path,
        "cq_path": cq_path,
        "suggested_path": suggested_path,
    }


def _patch_bedrock(monkeypatch, llm_response_json: dict):
    """invoke_bedrock_with_metadata 를 stub — 주어진 JSON 을 LLM 응답으로."""
    import tools.bedrock as bedrock_mod

    def _fake_invoke(prompt, max_tokens=4096, temperature=0.2, **kwargs):
        # 프롬프트 저장 (테스트 검증용)
        _fake_invoke.last_prompt = prompt
        return {
            "text": json.dumps(llm_response_json),
            "stop_reason": "end_turn",
        }

    _fake_invoke.last_prompt = None
    monkeypatch.setattr(bedrock_mod, "invoke_bedrock_with_metadata", _fake_invoke)
    return _fake_invoke


# ── 프롬프트 제약 테스트 ────────────────────────────────


def test_prompt_includes_direction_constraint(tmp_path, monkeypatch):
    """프롬프트에 'source = op.domain, target = op.range' 방향 제약이 명시된다."""
    _setup_project_files(tmp_path, monkeypatch)
    fake = _patch_bedrock(monkeypatch, {"mappings": []})

    from tools.tacit_rules import suggest_tacit_rules
    suggest_tacit_rules(max_rules=3, save_to_suggested=False)

    prompt = fake.last_prompt
    assert prompt is not None
    # 방향 제약 키워드
    assert "domain" in prompt.lower() and "range" in prompt.lower()
    # 제약 문구 중 하나라도
    direction_signals = [
        "source_class 는 op 의 domain",
        "source = op.domain",
        "방향",
        "direction",
        "reversed",
    ]
    assert any(s.lower() in prompt.lower() for s in direction_signals), (
        "프롬프트에 OP 방향 제약을 강제하는 문구가 없음"
    )


def test_prompt_lists_ops_with_domain_range(tmp_path, monkeypatch):
    """T-Box OP 목록이 (op, domain, range) 3-tuple 로 프롬프트에 포함."""
    _setup_project_files(tmp_path, monkeypatch)
    fake = _patch_bedrock(monkeypatch, {"mappings": []})

    from tools.tacit_rules import suggest_tacit_rules
    suggest_tacit_rules(max_rules=3, save_to_suggested=False)

    prompt = fake.last_prompt
    # tagBelongsToEquipment (TagMaster → EquipmentMaster) 가 프롬프트에 있어야
    assert "tagBelongsToEquipment" in prompt
    # 방향 표기 (→ 또는 →) 가 있어야
    assert ("→" in prompt or "->" in prompt) and "TagMaster" in prompt


# ── Write-time 검증 테스트 ────────────────────────────────


def test_reversed_op_auto_corrected_on_save(tmp_path, monkeypatch):
    """LLM 이 reversed op 를 제안하면 저장 시 inverse 로 교정 + annotation."""
    files = _setup_project_files(tmp_path, monkeypatch)
    # LLM 이 "source=TagMaster, target=EquipmentMaster, op=equipmentHasTag" 제안 (reversed)
    _patch_bedrock(monkeypatch, {
        "mappings": [{
            "name": "rule_reversed",
            "strategy": "simple_join",
            "source_csv": "Tag_Master.csv",
            "source_class": "TagMaster",
            "source_pk_column": "Tag_ID",
            "source_fk_column": "Equipment_ID",
            "target_class": "EquipmentMaster",
            "op": "equipmentHasTag",  # ⚠ reversed
            "output_file": "tacit_test.ttl",
            "_confidence": "high",
        }],
    })

    from tools.tacit_rules import suggest_tacit_rules
    result = json.loads(suggest_tacit_rules(max_rules=3, save_to_suggested=True))

    assert result["success"] is True
    # 저장된 파일 확인
    saved = json.loads(files["suggested_path"].read_text(encoding="utf-8"))
    mapping = saved["mappings"][0]
    assert mapping["op"] == "tagBelongsToEquipment"  # 교정됨
    assert "_auto_correction" in mapping
    assert mapping["_auto_correction"]["original_op"] == "equipmentHasTag"
    assert mapping["_auto_correction"]["corrected_op"] == "tagBelongsToEquipment"


def test_invalid_op_annotated_with_warning_but_kept(tmp_path, monkeypatch):
    """Invalid op (존재하지 않는 OP) 는 삭제하지 않고 _direction_warning 만 annotate.

    SME 가 검토하면서 T-Box 에 OP 를 추가하거나 op 이름을 수정할 수 있어야 함.
    """
    files = _setup_project_files(tmp_path, monkeypatch)
    _patch_bedrock(monkeypatch, {
        "mappings": [{
            "name": "rule_new_op",
            "strategy": "simple_join",
            "source_csv": "Tag_Master.csv",
            "source_class": "TagMaster",
            "source_pk_column": "Tag_ID",
            "source_fk_column": "Equipment_ID",
            "target_class": "EquipmentMaster",
            "op": "brandNewOpNotInTBox",  # T-Box 에 없음 → pass-through (validator 관대)
            "output_file": "tacit_test.ttl",
        }],
    })

    from tools.tacit_rules import suggest_tacit_rules
    result = json.loads(suggest_tacit_rules(max_rules=3, save_to_suggested=True))

    assert result["success"] is True
    saved = json.loads(files["suggested_path"].read_text(encoding="utf-8"))
    mapping = saved["mappings"][0]
    # validator 는 T-Box 미선언 op 에 대해 관대 (pass-through) → 경고 없음
    # 하지만 op 는 그대로 유지
    assert mapping["op"] == "brandNewOpNotInTBox"


def test_valid_op_untouched(tmp_path, monkeypatch):
    """올바른 방향의 op 는 교정/경고 없이 그대로 저장."""
    files = _setup_project_files(tmp_path, monkeypatch)
    _patch_bedrock(monkeypatch, {
        "mappings": [{
            "name": "rule_valid",
            "strategy": "simple_join",
            "source_csv": "Tag_Master.csv",
            "source_class": "TagMaster",
            "source_pk_column": "Tag_ID",
            "source_fk_column": "Equipment_ID",
            "target_class": "EquipmentMaster",
            "op": "tagBelongsToEquipment",  # ✓ correct
            "output_file": "tacit_test.ttl",
        }],
    })

    from tools.tacit_rules import suggest_tacit_rules
    result = json.loads(suggest_tacit_rules(max_rules=3, save_to_suggested=True))

    assert result["success"] is True
    saved = json.loads(files["suggested_path"].read_text(encoding="utf-8"))
    mapping = saved["mappings"][0]
    assert mapping["op"] == "tagBelongsToEquipment"
    assert "_auto_correction" not in mapping
    assert "_direction_warning" not in mapping


def test_validation_report_in_return_payload(tmp_path, monkeypatch):
    """반환 JSON 에 validation_summary (corrections/warnings 카운트) 포함."""
    _setup_project_files(tmp_path, monkeypatch)
    _patch_bedrock(monkeypatch, {
        "mappings": [
            {
                "name": "rule1",
                "strategy": "simple_join",
                "source_csv": "Tag_Master.csv",
                "source_class": "TagMaster",
                "source_pk_column": "Tag_ID",
                "source_fk_column": "Equipment_ID",
                "target_class": "EquipmentMaster",
                "op": "equipmentHasTag",  # reversed → 교정
                "output_file": "tacit_test.ttl",
            },
            {
                "name": "rule2",
                "strategy": "simple_join",
                "source_csv": "Tag_Master.csv",
                "source_class": "TagMaster",
                "source_pk_column": "Tag_ID",
                "source_fk_column": "Equipment_ID",
                "target_class": "EquipmentMaster",
                "op": "tagBelongsToEquipment",  # valid
                "output_file": "tacit_test.ttl",
            },
        ],
    })

    from tools.tacit_rules import suggest_tacit_rules
    result = json.loads(suggest_tacit_rules(max_rules=3, save_to_suggested=True))

    assert "validation_summary" in result
    summary = result["validation_summary"]
    assert summary["auto_corrected"] == 1
    assert summary["direction_warnings"] == 0
    assert summary["total"] == 2
