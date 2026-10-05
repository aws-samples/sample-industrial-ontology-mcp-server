"""Tests for tools/semantic_dictionary.py + tools/semantic_dict_validation.py — 시맨틱 딕셔너리 테스트."""

import json
from collections import defaultdict
from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph

# ── 테스트 픽스처 ─────────────────────────────────


def _make_tbox():
    """시맨틱 딕셔너리 테스트용 T-Box 그래프."""
    g = _new_graph()
    ns = DOMAIN_NS

    # 클래스
    eq = URIRef(f"{ns}EquipmentMaster")
    st = URIRef(f"{ns}EquipmentStatus")
    g.add((eq, RDF.type, OWL.Class))
    g.add((eq, RDFS.label, Literal("Equipment Master", lang="en")))
    g.add((eq, RDFS.label, Literal("설비 마스터", lang="ko")))
    g.add((eq, RDFS.comment, Literal("Master data for equipment", lang="en")))
    g.add((eq, RDFS.comment, Literal("설비 기본 정보", lang="ko")))

    g.add((st, RDF.type, OWL.Class))
    g.add((st, RDFS.label, Literal("Equipment Status", lang="en")))
    g.add((st, RDFS.label, Literal("설비 상태", lang="ko")))
    g.add((st, RDFS.comment, Literal("설비 상태", lang="ko")))

    # ObjectProperty
    has_status = URIRef(f"{ns}hasEquipmentStatus")
    g.add((has_status, RDF.type, OWL.ObjectProperty))
    g.add((has_status, RDFS.domain, eq))
    g.add((has_status, RDFS.range, st))
    g.add((has_status, RDFS.label, Literal("has equipment status", lang="en")))
    g.add((has_status, RDFS.label, Literal("설비 상태 관계", lang="ko")))
    g.add((has_status, RDFS.comment, Literal("설비 상태 관계", lang="ko")))

    # DatatypeProperty
    eid = URIRef(f"{ns}equipmentId")
    g.add((eid, RDF.type, OWL.DatatypeProperty))
    g.add((eid, RDFS.domain, eq))
    g.add((eid, RDFS.range, XSD.string))
    g.add((eid, RDFS.label, Literal("equipment ID", lang="en")))
    g.add((eid, RDFS.label, Literal("설비 ID", lang="ko")))
    g.add((eid, RDFS.comment, Literal("설비 식별자", lang="ko")))

    status_dp = URIRef(f"{ns}status")
    g.add((status_dp, RDF.type, OWL.DatatypeProperty))
    g.add((status_dp, RDFS.domain, st))
    g.add((status_dp, RDFS.range, XSD.string))
    g.add((status_dp, RDFS.label, Literal("status", lang="en")))
    g.add((status_dp, RDFS.label, Literal("상태", lang="ko")))
    g.add((status_dp, RDFS.comment, Literal("상태 값", lang="ko")))

    return g


def _make_abox(tbox):
    """시맨틱 딕셔너리 테스트용 A-Box 그래프."""
    g = _new_graph()
    ns = DOMAIN_NS
    inst = DOMAIN_INST_NS

    eq1 = URIRef(f"{inst}EquipmentMaster_EQ001")
    st1 = URIRef(f"{inst}EquipmentStatus_EQ001")

    g.add((eq1, RDF.type, URIRef(f"{ns}EquipmentMaster")))
    g.add((eq1, URIRef(f"{ns}equipmentId"), Literal("EQ001")))
    g.add((eq1, URIRef(f"{ns}hasEquipmentStatus"), st1))

    g.add((st1, RDF.type, URIRef(f"{ns}EquipmentStatus")))
    g.add((st1, URIRef(f"{ns}status"), Literal("Running")))

    return g


# ── _get_labels / _get_comments ───────────────────


class TestHelperFunctions:
    """라벨/코멘트 추출 헬퍼."""

    def test_get_labels(self):
        from tools.semantic_dictionary import _get_labels

        tbox = _make_tbox()
        en, ko = _get_labels(tbox, URIRef(f"{DOMAIN_NS}EquipmentMaster"))
        assert en == "Equipment Master"
        assert ko == "설비 마스터"

    def test_get_labels_missing(self):
        from tools.semantic_dictionary import _get_labels

        g = _new_graph()
        en, ko = _get_labels(g, URIRef("http://example.org/NoLabels"))
        assert en == ""
        assert ko == ""

    def test_get_comments(self):
        from tools.semantic_dictionary import _get_comments

        tbox = _make_tbox()
        en, ko = _get_comments(tbox, URIRef(f"{DOMAIN_NS}EquipmentMaster"))
        assert en == "Master data for equipment"
        assert ko == "설비 기본 정보"


# ── _compute_stats ────────────────────────────────


class TestComputeStats:
    """프로퍼티 값 통계 계산."""

    def test_string_values_few_distinct(self):
        from tools.semantic_dictionary import _compute_stats

        values = ["Running", "Stopped", "Running"]
        stats = _compute_stats(values, "string")
        assert stats["count"] == 3
        assert "distinct_values" in stats
        assert set(stats["distinct_values"]) == {"Running", "Stopped"}

    def test_string_values_many_distinct(self):
        from tools.semantic_dictionary import _compute_stats

        values = [f"val_{i}" for i in range(30)]
        stats = _compute_stats(values, "string")
        assert stats["distinct_count"] == 30
        assert len(stats["example_values"]) == 5

    def test_decimal_values(self):
        from tools.semantic_dictionary import _compute_stats

        values = ["10.0", "20.5", "30.0"]
        stats = _compute_stats(values, "decimal")
        assert stats["min"] == 10.0
        assert stats["max"] == 30.0

    def test_empty_values(self):
        from tools.semantic_dictionary import _compute_stats

        stats = _compute_stats([], "string")
        assert stats["count"] == 0


# ── _extract_classes ──────────────────────────────


class TestExtractClasses:
    """T-Box에서 클래스 및 서브클래스 맵 추출."""

    def test_extracts_steel_classes(self):
        from tools.semantic_dictionary import _extract_classes

        tbox = _make_tbox()
        classes, subclass_map = _extract_classes(tbox)
        class_names = [str(c) for c in classes]
        assert any("EquipmentMaster" in c for c in class_names)
        assert any("EquipmentStatus" in c for c in class_names)
        assert len(classes) == 2

    def test_ignores_non_steel_classes(self):
        from tools.semantic_dictionary import _extract_classes

        g = _new_graph()
        g.add((URIRef("http://example.org/Other"), RDF.type, OWL.Class))
        classes, _ = _extract_classes(g)
        assert len(classes) == 0


# ── _extract_properties ──────────────────────────


class TestExtractProperties:
    """T-Box/A-Box에서 프로퍼티 정보 추출."""

    def test_extracts_dt_and_obj_props(self):
        from tools.semantic_dictionary import _extract_properties

        tbox = _make_tbox()
        abox = _make_abox(tbox)
        props = _extract_properties(tbox, abox)

        assert "equipmentId" in props["dt_prop_info"]
        assert "status" in props["dt_prop_info"]
        assert len(props["obj_props"]) == 1
        assert props["obj_props"][0]["name"] == "hasEquipmentStatus"

    def test_op_counts_from_abox(self):
        from tools.semantic_dictionary import _extract_properties

        tbox = _make_tbox()
        abox = _make_abox(tbox)
        props = _extract_properties(tbox, abox)

        assert props["op_counts"].get("hasEquipmentStatus", 0) >= 1


# ── _single_pass_abox_stats ──────────────────────


class TestSinglePassAboxStats:
    """A-Box 단일 패스 통계."""

    def test_counts_instances_and_props(self):
        from tools.semantic_dictionary import _single_pass_abox_stats

        tbox = _make_tbox()
        abox = _make_abox(tbox)
        dt_prop_info = {
            "equipmentId": {"range": "string", "domains": ["EquipmentMaster"]},
            "status": {"range": "string", "domains": ["EquipmentStatus"]},
        }
        obj_prop_names = {"hasEquipmentStatus"}

        inst_counts, prop_values, op_counts = _single_pass_abox_stats(
            abox, DOMAIN_NS, dt_prop_info, obj_prop_names
        )

        eq_cls = URIRef(f"{DOMAIN_NS}EquipmentMaster")
        assert inst_counts.get(eq_cls, 0) >= 1
        assert op_counts.get("hasEquipmentStatus", 0) >= 1

    def test_empty_abox(self):
        from tools.semantic_dictionary import _single_pass_abox_stats

        abox = _new_graph()
        inst_counts, prop_values, op_counts = _single_pass_abox_stats(
            abox, DOMAIN_NS, {}, set()
        )
        assert inst_counts == {}
        assert op_counts == {}


# ── _build_common_mistakes ────────────────────────


class TestBuildCommonMistakes:
    """공통 실수 딕셔너리 동적 생성."""

    def test_generates_class_aliases(self):
        from tools.semantic_dictionary import _build_common_mistakes

        tbox = _make_tbox()
        class_names = {"EquipmentMaster", "EquipmentStatus"}
        result = _build_common_mistakes(tbox, class_names)

        # "EquipmentMaster" → "Equipment" 약칭이 생성되어야 함
        assert "Equipment" in result["classes"]
        assert result["classes"]["Equipment"] == "EquipmentMaster"

    def test_generates_function_hints(self):
        from tools.semantic_dictionary import _build_common_mistakes

        tbox = _make_tbox()
        result = _build_common_mistakes(tbox, set())
        assert "YEAR(?date)" in result["functions"]


# ── generate_semantic_dictionary MCP 도구 ─────────


class TestGenerateSemanticDictionary:
    """generate_semantic_dictionary() 통합 테스트."""

    @patch("tools.semantic_dictionary._load_ontology_data")
    @patch("tools.semantic_dictionary.os.makedirs")
    @patch("builtins.open", create=True)
    def test_happy_path(self, mock_open, mock_makedirs, mock_load):
        from tools.semantic_dictionary import generate_semantic_dictionary

        tbox = _make_tbox()
        abox = _make_abox(tbox)
        mock_load.return_value = (tbox, abox)

        # 파일 쓰기 모킹
        mock_file = MagicMock()
        mock_open.return_value.__enter__ = lambda s: mock_file
        mock_open.return_value.__exit__ = MagicMock(return_value=False)

        result = json.loads(generate_semantic_dictionary())
        assert result["success"] is True
        assert "statistics" in result
        assert result["statistics"]["classes"] >= 2
        assert result["statistics"]["object_properties"] >= 1

    @patch("tools.semantic_dictionary._load_ontology_data")
    def test_tbox_not_found(self, mock_load):
        from tools.semantic_dictionary import generate_semantic_dictionary

        mock_load.side_effect = FileNotFoundError("T-Box not found")

        result = json.loads(generate_semantic_dictionary())
        assert result["success"] is False
        assert "error" in result


# ── validate_semantic_dictionary (dict_validation) ─


class TestValidateSemanticDictionary:
    """시맨틱 딕셔너리 검증."""

    def test_valid_dictionary(self, tmp_path):
        """유효한 딕셔너리 — 모든 필수 섹션 존재."""
        from tools.semantic_dict_validation import _validate

        dict_data = {
            "metadata": {"version": "2.0"},
            "classes": {
                "EquipmentMaster": {
                    "label_en": "Equipment Master",
                    "label_ko": "설비 마스터",
                    "description_ko": "설비 정보",
                },
                "EquipmentStatus": {
                    "label_en": "Equipment Status",
                    "label_ko": "설비 상태",
                    "description_ko": "설비 상태",
                },
            },
            "object_properties": {
                "hasEquipmentStatus": {
                    "label_ko": "상태 관계",
                    "domain": ["EquipmentMaster"],
                    "range": ["EquipmentStatus"],
                },
            },
            "sparql_guide": {
                "engine_compatibility": {"key": "value"},
                "anti_patterns": {"key": "value"},
                "common_patterns": {"key": "value"},
            },
            "common_mistakes": {"classes": {}},
            "question_templates": {t: {} for t in range(10)},
            "class_quick_reference": {
                "EquipmentMaster": "equipmentId(string)",
                "EquipmentStatus": "status(string)",
            },
            "process_flow": {},
            "functional_properties": {},
        }

        # T-Box 파일 생성
        tbox_path = str(tmp_path / "tbox.ttl")
        tbox = _make_tbox()
        tbox.serialize(tbox_path, format="turtle")

        result = _validate(dict_data, tbox_path, "/nonexistent/abox.ttl")
        assert result["summary"]["critical"] == 0

    def test_missing_section_critical(self):
        """필수 섹션 누락 → critical."""
        from tools.semantic_dict_validation import _validate

        dict_data = {"classes": {}}  # 대부분 누락
        result = _validate(dict_data, "/nonexistent/tbox.ttl", "/nonexistent/abox.ttl")
        assert result["summary"]["critical"] >= 1
        assert result["passed"] is False

    def test_class_missing_in_dict(self, tmp_path):
        """T-Box 클래스가 딕셔너리에 없으면 high."""
        from tools.semantic_dict_validation import _validate

        tbox_path = str(tmp_path / "tbox.ttl")
        tbox = _make_tbox()
        tbox.serialize(tbox_path, format="turtle")

        dict_data = {
            "metadata": {},
            "classes": {},  # T-Box 클래스 누락
            "object_properties": {},
            "sparql_guide": {
                "engine_compatibility": {},
                "anti_patterns": {},
                "common_patterns": {},
            },
            "common_mistakes": {},
            "question_templates": {},
            "class_quick_reference": {},
            "process_flow": {},
        }

        result = _validate(dict_data, tbox_path, "/nonexistent/abox.ttl")
        missing = [i for i in result["issues"] if i["rule"] == "class_missing_in_dict"]
        assert len(missing) >= 2  # EquipmentMaster, EquipmentStatus

    def test_op_domain_not_in_classes(self):
        """ObjectProperty domain이 클래스에 없으면 high."""
        from tools.semantic_dict_validation import _validate

        dict_data = {
            "metadata": {},
            "classes": {"EquipmentMaster": {"label_ko": "X", "label_en": "Y"}},
            "object_properties": {
                "hasEquipmentStatus": {
                    "label_ko": "상태",
                    "domain": ["NonExistentClass"],
                    "range": ["EquipmentMaster"],
                },
            },
            "sparql_guide": {
                "engine_compatibility": {},
                "anti_patterns": {},
                "common_patterns": {},
            },
            "common_mistakes": {},
            "question_templates": {},
            "class_quick_reference": {"EquipmentMaster": ""},
            "process_flow": {},
        }

        result = _validate(dict_data, "/nonexistent/tbox.ttl", "/nonexistent/abox.ttl")
        domain_issues = [i for i in result["issues"] if i["rule"] == "op_domain_not_in_classes"]
        assert len(domain_issues) >= 1

    def test_coverage_stats(self, tmp_path):
        """커버리지 통계 반환."""
        from tools.semantic_dict_validation import _validate

        tbox_path = str(tmp_path / "tbox.ttl")
        tbox = _make_tbox()
        tbox.serialize(tbox_path, format="turtle")

        dict_data = {
            "metadata": {},
            "classes": {"EquipmentMaster": {"label_ko": "X", "label_en": "Y", "description_ko": "Z"}},
            "object_properties": {},
            "sparql_guide": {
                "engine_compatibility": {},
                "anti_patterns": {},
                "common_patterns": {},
            },
            "common_mistakes": {},
            "question_templates": {},
            "class_quick_reference": {"EquipmentMaster": ""},
            "process_flow": {},
        }

        result = _validate(dict_data, tbox_path, "/nonexistent/abox.ttl")
        assert "coverage" in result
        assert result["coverage"]["tbox_classes"] == 2
        assert result["coverage"]["dict_classes"] == 1


# ── D1 통합 테스트 (2홉 경로 확인) ─────────────────


def test_generated_dict_has_2hop_paths():
    """generate_semantic_dictionary 결과 JSON 파일에 object_properties_2hop 필드 포함."""
    import os

    from config import ABOX_PATH, SEMANTIC_DICT_PATH, TBOX_PATH
    if not os.path.exists(TBOX_PATH) or not os.path.exists(ABOX_PATH):
        pytest.skip("T-Box 또는 A-Box 파일 없음")

    import json

    from tools.semantic_dictionary import generate_semantic_dictionary
    result = json.loads(generate_semantic_dictionary())
    assert result["success"] is True

    # Load actual dictionary from disk (도구는 파일 경로만 반환, 실제 딕셔너리는 파일에 저장)
    with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
        full_dict = json.load(f)
    classes = full_dict.get("classes", {})
    assert classes, "dictionary has classes"
    has_2hop = any("object_properties_2hop" in c for c in classes.values())
    assert has_2hop, "적어도 한 클래스는 2홉 경로를 가져야 함"


def test_common_patterns_has_two_hop_example():
    """sparql_guide.common_patterns 에 two_hop_path 예시 포함."""
    import os

    from config import SEMANTIC_DICT_PATH, TBOX_PATH
    if not os.path.exists(TBOX_PATH):
        pytest.skip("T-Box 파일 없음")

    import json

    from tools.semantic_dictionary import generate_semantic_dictionary
    json.loads(generate_semantic_dictionary())  # just trigger write

    with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
        full_dict = json.load(f)
    classes = full_dict.get("classes", {})
    if any("object_properties_2hop" in c for c in classes.values()):
        patterns = full_dict.get("sparql_guide", {}).get("common_patterns", {})
        assert "two_hop_path" in patterns, "2홉 경로가 있으면 sample 도 common_patterns 에 있어야 함"


# ── 결정성: 같은 입력 → 같은 딕셔너리 (2026-08-30) ────────────────────────
#
# 두 곳이 PYTHONHASHSEED 에 좌우됐다:
#
#   (A) _build_question_templates 가 `set(dt_props.keys())` 로 dict 순서를 버리고,
#       그 set 이 `fk_props[0]` 으로 흘러 **선택**을 바꿨다. 실측 5-seed:
#       대기 배출 모니터링.key_join ∈ {…StackId(FK), …MonitorId(자기 PK)} —
#       순서 차이가 아니라 **의미가 다른 값**이 번갈아 나왔다.
#   (B) _descendant_names 가 docstring 은 BFS 인데 queue.pop() 으로 LIFO 였고,
#       subclass_map 의 값이 set 이라 형제 순서가 무작위였다. 호출자가 [:3] 으로
#       자르므로 순서가 곧 선택 — class_quick_reference 83개 중 23개 흔들림,
#       12개는 내용까지 달랐다.
#
# 이 값들은 tools/bedrock.py 가 LLM 프롬프트에 직접 주입하고, 딕셔너리 파일 자체도
# read_semantic_dictionary 가 원문 그대로 LLM 컨텍스트로 넘긴다.


def _shuffled_dict(d: dict) -> dict:
    """키 순서를 뒤집은 같은 내용의 dict — 삽입 순서 의존을 드러낸다."""
    return {k: d[k] for k in reversed(list(d))}


def test_descendant_names_is_breadth_first_and_sorted():
    """THE REGRESSION (B): 형제를 이름 순으로, 진짜 BFS 로 방문한다."""
    from tools.semantic_dictionary import _descendant_names

    ns = DOMAIN_NS
    parent = URIRef(f"{ns}Parent")
    subclass_map = {
        parent: {URIRef(f"{ns}Zeta"), URIRef(f"{ns}Alpha"), URIRef(f"{ns}Mid")},
        URIRef(f"{ns}Alpha"): {URIRef(f"{ns}AlphaChild")},
    }
    out = _descendant_names("Parent", subclass_map, ns)
    # 1세대가 이름 순으로 먼저, 그 다음 2세대 (BFS)
    assert out[:3] == ["Alpha", "Mid", "Zeta"], out
    assert out[3] == "AlphaChild", (
        f"2세대가 1세대보다 먼저 나왔다 — LIFO(DFS) 다: {out}"
    )


def test_descendant_names_stable_under_set_iteration_order():
    """set 순회 순서가 결과를 바꾸지 않는다 (같은 집합, 다른 삽입 순서)."""
    from tools.semantic_dictionary import _descendant_names

    ns = DOMAIN_NS
    names = ["Delta", "Beta", "Omega", "Gamma", "Alpha"]
    first = None
    for rotation in range(len(names)):
        rotated = names[rotation:] + names[:rotation]
        subclass_map = {
            URIRef(f"{ns}Parent"): {URIRef(f"{ns}{n}") for n in rotated},
        }
        out = _descendant_names("Parent", subclass_map, ns)
        if first is None:
            first = out
        assert out == first, f"삽입 순서에 따라 결과가 달라졌다: {out} vs {first}"
    assert first == sorted(names)


def test_question_templates_do_not_depend_on_dp_insertion_order():
    """THE REGRESSION (A): DP 삽입 순서가 key_join/key_properties 선택을 바꾸지 않는다.

    ``set(...)`` 이 남아 있으면 이 테스트는 PYTHONHASHSEED 에 따라 통과/실패가
    갈리므로, dict 순서를 **명시적으로 뒤집어** 결정성을 직접 주장한다.
    """
    from tools.semantic_dictionary import _build_question_templates

    tbox = _make_tbox()
    dps = {
        "fuelConsumptionSourceId": {},
        "fuelConsumptionMeterCode": {},
        "fuelConsumptionQuantity": {},
        "fuelConsumptionRate": {},
        "fuelConsumptionYield": {},
    }
    classes = {
        "FuelConsumption": {
            "label_ko": "연료 소비",
            "instance_count": 10,
            "datatype_properties": dps,
            "object_properties_outgoing": [],
        },
    }
    forward = _build_question_templates(tbox, classes)
    reversed_classes = {
        "FuelConsumption": {**classes["FuelConsumption"],
                            "datatype_properties": _shuffled_dict(dps)},
    }
    backward = _build_question_templates(tbox, reversed_classes)
    assert forward == backward, (
        "DP 삽입 순서가 템플릿을 바꿨다 — set 순회가 남아 있다:\n"
        f"  forward : {json.dumps(forward, ensure_ascii=False)}\n"
        f"  backward: {json.dumps(backward, ensure_ascii=False)}"
    )
    # 선택이 실제로 결정적인지 (첫 후보가 사전순 최소인지) 확인
    entry = forward.get("연료 소비") or {}
    if "key_join" in entry:
        assert entry["key_join"] == "fuelConsumptionMeterCode", entry


def test_class_quick_reference_is_deterministic():
    """상속 표기 대표 후손이 실행마다 바뀌지 않는다.

    LLM 프롬프트(tools/bedrock.py)에 주입되는 값이므로 진동은 곧 "같은 KG 인데
    다른 속성을 안내" 다.
    """
    from tools.semantic_dictionary import _build_class_quick_reference

    ns = DOMAIN_NS
    children = ["GasEnergy", "FuelConsumption", "SteamEnergy", "ElectricalConsumption"]
    classes = {"EnergyConsumption": {"datatype_properties": {},
                                     "object_properties_outgoing": []}}
    for name in children:
        classes[name] = {
            "datatype_properties": {f"{name[0].lower()}{name[1:]}Value": {}},
            "object_properties_outgoing": [],
        }

    first = None
    for rotation in range(len(children)):
        rotated = children[rotation:] + children[:rotation]
        subclass_map = {
            URIRef(f"{ns}EnergyConsumption"): {URIRef(f"{ns}{n}") for n in rotated},
        }
        out = _build_class_quick_reference(classes, subclass_map, ns)
        if first is None:
            first = out
        assert out == first, (
            "후손 방문 순서가 요약 문자열을 바꿨다 — [:3] 절단이 다른 후손을 고른다"
        )


def test_deployed_dictionary_sections_are_reproducible():
    """실측 고정: 배포 딕셔너리의 두 섹션을 재계산하면 같은 값이 나온다.

    단위 픽스처가 통과해도 실제 산출물 규모에서 흔들릴 수 있다 (이 리포의
    "산출물로 확인하라"). 재계산 2회를 비교한다 — 프로세스 내 재현이므로 시드
    변동은 못 잡지만, 입력 무변경에 대한 안정성은 확인된다.
    """
    import os

    from config import SEMANTIC_DICT_PATH
    from domain.tbox_utils import load_tbox
    from tools.semantic_dictionary import (
        _build_class_quick_reference,
        _build_question_templates,
    )

    if not os.path.exists(SEMANTIC_DICT_PATH):
        pytest.skip("배포 딕셔너리 없음")
    with open(SEMANTIC_DICT_PATH, encoding="utf-8") as handle:
        classes = json.load(handle).get("classes") or {}
    if not classes:
        pytest.skip("classes 비어 있음")

    tbox = load_tbox()
    subclass_map: dict = defaultdict(set)
    for child, _, parent in tbox.triples((None, RDFS.subClassOf, None)):
        if isinstance(parent, URIRef):
            subclass_map[parent].add(child)

    qt_a = _build_question_templates(tbox, classes)
    qt_b = _build_question_templates(tbox, classes)
    assert qt_a == qt_b

    qr_a = _build_class_quick_reference(classes, subclass_map, DOMAIN_NS)
    qr_b = _build_class_quick_reference(classes, subclass_map, DOMAIN_NS)
    assert qr_a == qr_b
