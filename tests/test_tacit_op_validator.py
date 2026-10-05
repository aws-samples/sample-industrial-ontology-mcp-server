"""Tests for tacit OP direction validator — T-Box domain/range 기반 검증.

2026-05-10 회귀 분석에서 발견:
  rules/domain/tacit_rules.json 의 rule 이 source/target 순서와 반대인 op 를 지정하면
  생성된 tacit TTL 이 T-Box domain/range 를 위반하는 triple 을 삽입.
  예: TagMaster → equipmentHasTag → EquipmentMaster (equipmentHasTag domain=EquipmentMaster)

Validator 가 해결:
  - valid direction → op 그대로 사용
  - reversed direction (op 의 inverse 가 매칭) → 자동으로 inverse op 사용
  - 둘 다 invalid → rule 전체 skip + warning
"""
from __future__ import annotations

from tools.tacit_op_validator import validate_op_direction

_TBOX_TTL = """
@prefix : <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

:TagMaster a owl:Class .
:EquipmentMaster a owl:Class .
:ProductMaster a owl:Class .

:equipmentHasTag a owl:ObjectProperty ;
    rdfs:domain :EquipmentMaster ;
    rdfs:range :TagMaster ;
    owl:inverseOf :tagBelongsToEquipment .

:tagBelongsToEquipment a owl:ObjectProperty ;
    rdfs:domain :TagMaster ;
    rdfs:range :EquipmentMaster ;
    owl:inverseOf :equipmentHasTag .

:hasLonelyOp a owl:ObjectProperty ;
    rdfs:domain :TagMaster ;
    rdfs:range :EquipmentMaster .
# No inverse declared.

:unrelatedOp a owl:ObjectProperty ;
    rdfs:domain :ProductMaster ;
    rdfs:range :EquipmentMaster .
"""


def test_valid_direction_returns_same_op():
    """op 의 domain/range 가 source/target 과 일치 → 그대로 사용."""
    result = validate_op_direction(
        op="tagBelongsToEquipment",
        source_class="TagMaster",
        target_class="EquipmentMaster",
        tbox_ttl=_TBOX_TTL,
    )
    assert result["valid"] is True
    assert result["op"] == "tagBelongsToEquipment"
    assert result["corrected"] is False


def test_reversed_direction_auto_corrects_to_inverse():
    """op 방향이 반대이고 inverse op 가 맞다 → inverse 로 교정."""
    result = validate_op_direction(
        op="equipmentHasTag",  # domain=EquipmentMaster, range=TagMaster (reversed)
        source_class="TagMaster",  # rule intent: TagMaster → EquipmentMaster
        target_class="EquipmentMaster",
        tbox_ttl=_TBOX_TTL,
    )
    assert result["valid"] is True
    assert result["op"] == "tagBelongsToEquipment"
    assert result["corrected"] is True
    assert result["original_op"] == "equipmentHasTag"
    assert "reason" in result


def test_reversed_without_inverse_fails():
    """op 방향이 반대이고 inverse 선언도 없음 → invalid."""
    # hasLonelyOp: domain=TagMaster, range=EquipmentMaster, no inverse
    # rule 이 source=EquipmentMaster, target=TagMaster 로 썼다고 가정
    result = validate_op_direction(
        op="hasLonelyOp",
        source_class="EquipmentMaster",
        target_class="TagMaster",
        tbox_ttl=_TBOX_TTL,
    )
    assert result["valid"] is False
    assert result["op"] is None
    assert "no inverse" in result["reason"].lower() or "reversed" in result["reason"].lower()


def test_unknown_op_passes_through_with_note():
    """T-Box 에 없는 op → backward compat 으로 통과 (기존 rule 환경 보호).

    엄격 모드가 필요하면 호출자가 별도 체크. 여기서는 validator 가 rule 을
    silently kill 하지 않도록 보수적으로 처리.
    """
    result = validate_op_direction(
        op="nonExistentOp",
        source_class="TagMaster",
        target_class="EquipmentMaster",
        tbox_ttl=_TBOX_TTL,
    )
    assert result["valid"] is True
    assert result["op"] == "nonExistentOp"
    assert result["corrected"] is False
    assert "not declared" in result["reason"].lower()


def test_op_not_matching_either_direction():
    """op 가 source/target 중 어느 쪽과도 호환 안됨 → invalid."""
    # unrelatedOp: domain=ProductMaster, range=EquipmentMaster
    # source=TagMaster 는 ProductMaster 아니고, target=EquipmentMaster 는 맞지만 domain 틀림
    result = validate_op_direction(
        op="unrelatedOp",
        source_class="TagMaster",
        target_class="EquipmentMaster",
        tbox_ttl=_TBOX_TTL,
    )
    assert result["valid"] is False
    # 엄밀 체크: 불일치 이유가 포함되는지
    assert result["op"] is None


def test_empty_tbox_passes_through():
    """T-Box 가 비어있거나 로드 실패 → valid=True 로 통과 (backward compat)."""
    result = validate_op_direction(
        op="anyOp",
        source_class="A",
        target_class="B",
        tbox_ttl="",  # empty
    )
    # empty T-Box 는 검증 불가 → 호환성을 위해 통과 (legacy behaviour)
    assert result["valid"] is True
    assert result["op"] == "anyOp"


def test_subclass_domain_matching():
    """source 가 domain 의 하위 클래스면 valid — subclass-aware matching."""
    tbox_with_hierarchy = _TBOX_TTL + """
:SpecificTag rdfs:subClassOf :TagMaster .
"""
    # rule 이 source=SpecificTag, target=EquipmentMaster, op=tagBelongsToEquipment
    # tagBelongsToEquipment: domain=TagMaster (parent of SpecificTag) → should match
    result = validate_op_direction(
        op="tagBelongsToEquipment",
        source_class="SpecificTag",
        target_class="EquipmentMaster",
        tbox_ttl=tbox_with_hierarchy,
    )
    assert result["valid"] is True
    assert result["op"] == "tagBelongsToEquipment"


# ── generate_tacit_from_rules 통합 ──────────────────────────


def test_integration_reversed_op_auto_corrected_in_generation(tmp_path, monkeypatch):
    """generate_tacit_from_rules 가 reversed op 자동 교정하고 결과 TTL 이 올바른 방향.

    2026-05-10 회귀 재현: rule 이 equipmentHasTag (TagMaster→EquipmentMaster 로
    잘못 쓰임) 를 지정하면 validator 가 tagBelongsToEquipment 로 교정해야 함.
    """
    import csv
    import json
    from pathlib import Path

    # Setup fake project dirs
    rawdata = tmp_path / "rawdata"
    tacit = tmp_path / "tacit"
    tbox_path = tmp_path / "t_box.ttl"
    rules_path = tmp_path / "tacit_rules.json"
    rawdata.mkdir()
    tacit.mkdir()

    # T-Box 작성
    tbox_path.write_text(_TBOX_TTL, encoding="utf-8")

    # source CSV 작성 (Tag_Master 형식)
    with open(rawdata / "Tag_Master.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Tag_ID", "Equipment_ID"])
        w.writerow(["TAG001", "EQ001"])
        w.writerow(["TAG002", "EQ002"])

    # Rule 작성 — 의도적으로 reversed op 사용
    rules_path.write_text(json.dumps({
        "mappings": [{
            "name": "rule_tag_reversed",
            "strategy": "simple_join",
            "source_csv": "Tag_Master.csv",
            "source_class": "TagMaster",
            "source_pk_column": "Tag_ID",
            "source_fk_column": "Equipment_ID",
            "target_class": "EquipmentMaster",
            "op": "equipmentHasTag",  # ⚠ reversed — validator 가 교정해야
            "output_file": "tacit_test.ttl",
        }],
    }), encoding="utf-8")

    # Patch dirs + TBOX_PATH
    import config
    import tools.tacit_rules as tr
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox_path))
    monkeypatch.setattr(tr, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tr, "SOURCE_TACIT_DIR", str(tacit))

    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))

    # 교정 기록 확인
    assert result["success"] is True
    assert result["op_corrections_count"] == 1
    assert result["op_corrections"][0]["original_op"] == "equipmentHasTag"
    assert result["op_corrections"][0]["corrected_op"] == "tagBelongsToEquipment"

    # 생성된 TTL 에 올바른 OP 가 사용됐는지 확인
    output = Path(tacit / "tacit_test.ttl").read_text(encoding="utf-8")
    assert "tagBelongsToEquipment" in output
    assert "equipmentHasTag" not in output
    # subject 는 TagMaster, object 는 EquipmentMaster 여야 (source/target 방향 유지)
    assert "TagMaster_TAG001" in output
    assert "EquipmentMaster_EQ001" in output


def test_non_steel_domain_prefix():
    """도메인 중립성 — 다른 prefix 에서도 validator 가 동작."""
    tbox_other = """
@prefix : <https://example.org/ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

:CustomerMaster a owl:Class .
:OrderMaster a owl:Class .

:orderHasCustomer a owl:ObjectProperty ;
    rdfs:domain :OrderMaster ;
    rdfs:range :CustomerMaster ;
    owl:inverseOf :customerHasOrder .

:customerHasOrder a owl:ObjectProperty ;
    rdfs:domain :CustomerMaster ;
    rdfs:range :OrderMaster ;
    owl:inverseOf :orderHasCustomer .
"""
    # rule 이 source=OrderMaster, target=CustomerMaster, op=customerHasOrder
    # (reversed) → orderHasCustomer 로 교정되어야.
    result = validate_op_direction(
        op="customerHasOrder",
        source_class="OrderMaster",
        target_class="CustomerMaster",
        tbox_ttl=tbox_other,
        domain_prefix="https://example.org/ontology#",
    )
    assert result["valid"] is True
    assert result["op"] == "orderHasCustomer"
    assert result["corrected"] is True


def test_self_relation_forward_takes_precedence():
    """source=target 자기 관계 (예: followedBy) — forward 체크가 먼저 통과."""
    tbox_self = """
@prefix : <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

:Process a owl:Class .
:followedBy a owl:ObjectProperty ;
    rdfs:domain :Process ;
    rdfs:range :Process ;
    owl:inverseOf :precededBy .
:precededBy a owl:ObjectProperty ;
    rdfs:domain :Process ;
    rdfs:range :Process ;
    owl:inverseOf :followedBy .
"""
    result = validate_op_direction(
        op="followedBy",
        source_class="Process",
        target_class="Process",
        tbox_ttl=tbox_self,
    )
    # self-relation 은 forward 가 valid (domain=Process ⊇ source, range=Process ⊇ target)
    assert result["valid"] is True
    assert result["op"] == "followedBy"
    assert result["corrected"] is False


def test_integration_valid_op_not_corrected(tmp_path, monkeypatch):
    """올바른 방향의 op 는 교정하지 않고 그대로 사용."""
    import csv
    import json
    from pathlib import Path

    rawdata = tmp_path / "rawdata"
    tacit = tmp_path / "tacit"
    tbox_path = tmp_path / "t_box.ttl"
    rules_path = tmp_path / "tacit_rules.json"
    rawdata.mkdir()
    tacit.mkdir()
    tbox_path.write_text(_TBOX_TTL, encoding="utf-8")

    with open(rawdata / "Tag_Master.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Tag_ID", "Equipment_ID"])
        w.writerow(["TAG001", "EQ001"])

    # 올바른 방향의 op
    rules_path.write_text(json.dumps({
        "mappings": [{
            "name": "rule_tag_correct",
            "strategy": "simple_join",
            "source_csv": "Tag_Master.csv",
            "source_class": "TagMaster",
            "source_pk_column": "Tag_ID",
            "source_fk_column": "Equipment_ID",
            "target_class": "EquipmentMaster",
            "op": "tagBelongsToEquipment",  # ✓ correct direction
            "output_file": "tacit_test.ttl",
        }],
    }), encoding="utf-8")

    import config
    import tools.tacit_rules as tr
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(config, "SOURCE_TACIT_DIR", str(tacit))
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox_path))
    monkeypatch.setattr(tr, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tr, "SOURCE_TACIT_DIR", str(tacit))

    from tools.tacit_rules import generate_tacit_from_rules
    result = json.loads(generate_tacit_from_rules(str(rules_path)))

    assert result["success"] is True
    assert result["op_corrections_count"] == 0
    output = Path(tacit / "tacit_test.ttl").read_text(encoding="utf-8")
    assert "tagBelongsToEquipment" in output
