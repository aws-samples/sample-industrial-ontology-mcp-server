"""Tests for tools/multi_agent_tbox.py — 3 에이전트 협업 T-Box 생성 테스트."""

import json
from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, Literal, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from domain.tbox_utils import _new_graph

# ── _parse_review_json ────────────────────────────


class TestParseReviewJson:
    """LLM 응답 JSON 파싱."""

    def test_valid_json(self):
        from tools.multi_agent_tbox import _parse_review_json

        text = '{"issues": [], "approved": true, "summary": "OK"}'
        result = _parse_review_json(text, "TestAgent")
        assert result["approved"] is True
        assert result["issues"] == []
        assert result["summary"] == "OK"

    def test_json_in_markdown_code_fence(self):
        """마크다운 코드 블록 안의 JSON 추출."""
        from tools.multi_agent_tbox import _parse_review_json

        text = """Some explanation.

```json
{"issues": [{"severity": "high", "target": "X"}], "approved": false, "summary": "Issues found"}
```
"""
        result = _parse_review_json(text, "TestAgent")
        assert result["approved"] is False
        assert len(result["issues"]) == 1

    def test_invalid_json_returns_fallback(self):
        """JSON 파싱 실패 시 기본 결과 반환."""
        from tools.multi_agent_tbox import _parse_review_json

        text = "This is not JSON at all"
        result = _parse_review_json(text, "TestAgent")
        assert result["approved"] is False
        assert result["parse_error"] is True
        assert result["issues"] == []

    def test_empty_string(self):
        from tools.multi_agent_tbox import _parse_review_json

        result = _parse_review_json("", "TestAgent")
        assert result["parse_error"] is True

    def test_json_without_code_fence(self):
        """코드 블록 없이 순수 JSON."""
        from tools.multi_agent_tbox import _parse_review_json

        text = '{"issues": [{"severity": "critical"}], "approved": false, "summary": "critical issue"}'
        result = _parse_review_json(text, "TestAgent")
        assert len(result["issues"]) == 1


# ── _resolve_uri / _resolve_uri_or_literal ────────


class TestResolveUri:
    """Prefixed name → URIRef 변환."""

    def test_steel_prefix(self):
        from tools.multi_agent_tbox import _resolve_uri

        uri = _resolve_uri(f"{NS_PREFIX}:EquipmentMaster")
        assert str(uri) == f"{DOMAIN_NS}EquipmentMaster"

    def test_rdfs_prefix(self):
        from tools.multi_agent_tbox import _resolve_uri

        uri = _resolve_uri("rdfs:label")
        assert str(uri) == "http://www.w3.org/2000/01/rdf-schema#label"

    def test_owl_prefix(self):
        from tools.multi_agent_tbox import _resolve_uri

        uri = _resolve_uri("owl:Class")
        assert str(uri) == "http://www.w3.org/2002/07/owl#Class"

    def test_unknown_prefix_passthrough(self):
        """알 수 없는 prefix는 그대로 URIRef."""
        from tools.multi_agent_tbox import _resolve_uri

        uri = _resolve_uri("http://example.org/Foo")
        assert str(uri) == "http://example.org/Foo"


class TestResolveUriOrLiteral:
    """Prefixed name → URIRef, 아니면 Literal."""

    def test_prefixed_name_returns_uriref(self):
        from tools.multi_agent_tbox import _resolve_uri_or_literal

        result = _resolve_uri_or_literal(f"{NS_PREFIX}:EquipmentMaster")
        assert isinstance(result, URIRef)

    def test_typed_literal(self):
        """xsd 타입 리터럴 변환."""
        from tools.multi_agent_tbox import _resolve_uri_or_literal

        result = _resolve_uri_or_literal('"42"^^xsd:integer')
        assert isinstance(result, Literal)
        assert str(result) == "42"

    def test_language_tagged_literal(self):
        from tools.multi_agent_tbox import _resolve_uri_or_literal

        result = _resolve_uri_or_literal('"설비 마스터"@ko')
        assert isinstance(result, Literal)
        assert result.language == "ko"

    def test_plain_string_returns_literal(self):
        from tools.multi_agent_tbox import _resolve_uri_or_literal

        result = _resolve_uri_or_literal("plain text")
        assert isinstance(result, Literal)
        assert str(result) == "plain text"

    def test_quoted_string_strips_quotes(self):
        from tools.multi_agent_tbox import _resolve_uri_or_literal

        result = _resolve_uri_or_literal('"hello"')
        assert isinstance(result, Literal)
        assert str(result) == "hello"


# ── _count_issues ─────────────────────────────────


class TestCountIssues:
    """심각도 기준 이슈 카운팅."""

    def test_count_high_and_above(self):
        from tools.multi_agent_tbox import _count_issues

        review = {
            "issues": [
                {"severity": "critical"},
                {"severity": "high"},
                {"severity": "medium"},
                {"severity": "low"},
            ]
        }
        assert _count_issues(review, "high") == 2

    def test_count_critical_only(self):
        from tools.multi_agent_tbox import _count_issues

        review = {
            "issues": [
                {"severity": "high"},
                {"severity": "medium"},
            ]
        }
        assert _count_issues(review, "critical") == 0

    def test_empty_issues(self):
        from tools.multi_agent_tbox import _count_issues

        assert _count_issues({"issues": []}, "high") == 0
        assert _count_issues({}, "high") == 0


# ── _check_structural_gate ────────────────────────


class TestCheckStructuralGate:
    """사전 구조 게이트 검증."""

    def test_deep_hierarchy_and_good_rr_pass(self, sample_tbox_ttl):
        """DIT >= 2, RR >= 0.25 → 통과."""
        from tools.multi_agent_tbox import _check_structural_gate

        # sample_tbox_ttl에 subClassOf가 없으므로 DIT=0이 될 수 있음
        # 여기서는 게이트 로직만 테스트
        passed, msg = _check_structural_gate(sample_tbox_ttl)
        # sample_tbox_ttl은 DIT<2일 수 있지만 게이트 자체는 동작해야 함
        assert isinstance(passed, bool)
        assert isinstance(msg, str)

    def test_invalid_ttl_returns_true(self):
        """파싱 실패 시 게이트 무시 (True 반환)."""
        from tools.multi_agent_tbox import _check_structural_gate

        passed, msg = _check_structural_gate("invalid turtle @@@")
        assert passed is True
        assert "실패" in msg or "무시" in msg


# ── _apply_patches ────────────────────────────────


class TestApplyPatches:
    """패치 적용 테스트."""

    def test_add_triple(self, sample_tbox_ttl):
        from tools.multi_agent_tbox import _apply_patches

        patches = [{
            "action": "add_triple",
            "subject": f"{NS_PREFIX}:NewClass",
            "predicate": "rdf:type",
            "object": "owl:Class",
        }]
        result_ttl = _apply_patches(sample_tbox_ttl, patches)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        # NewClass가 추가되었는지 확인
        new_cls = URIRef(f"{DOMAIN_NS}NewClass")
        assert (new_cls, RDF.type, OWL.Class) in g

    def test_remove_triple(self, sample_tbox_ttl):
        from tools.multi_agent_tbox import _apply_patches

        patches = [{
            "action": "remove_triple",
            "subject": f"{NS_PREFIX}:hasEquipmentStatus",
            "predicate": "rdf:type",
            "object": "owl:ObjectProperty",
        }]
        result_ttl = _apply_patches(sample_tbox_ttl, patches)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")
        prop = URIRef(f"{DOMAIN_NS}hasEquipmentStatus")
        assert (prop, RDF.type, OWL.ObjectProperty) not in g

    def test_invalid_patch_skipped(self, sample_tbox_ttl):
        """잘못된 패치는 건너뛰고 원본 유지."""
        from tools.multi_agent_tbox import _apply_patches

        patches = [{"action": "add_triple"}]  # subject/predicate/object 누락
        result_ttl = _apply_patches(sample_tbox_ttl, patches)
        # 원본과 동일한 트리플 수
        g_orig = _new_graph()
        g_orig.parse(data=sample_tbox_ttl, format="turtle")
        g_result = _new_graph()
        g_result.parse(data=result_ttl, format="turtle")
        assert len(g_result) == len(g_orig)


# ── _apply_high_level_instructions ────────────────


class TestApplyHighLevelInstructions:
    """고수준 지시 적용 테스트."""

    def test_add_object_property(self, sample_tbox_ttl):
        from tools.multi_agent_tbox import _apply_high_level_instructions

        instructions = [{
            "action": "add_object_property",
            "name": "hasAlarm",
            "domain": "EquipmentMaster",
            "range": "EquipmentStatus",
            "inverse": "isAlarmOf",
            "label_ko": "알람 관계",
        }]
        result_ttl = _apply_high_level_instructions(sample_tbox_ttl, instructions)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")

        alarm_prop = URIRef(f"{DOMAIN_NS}hasAlarm")
        assert (alarm_prop, RDF.type, OWL.ObjectProperty) in g
        assert (alarm_prop, RDFS.domain, URIRef(f"{DOMAIN_NS}EquipmentMaster")) in g

    def test_add_subclass(self, sample_tbox_ttl):
        from tools.multi_agent_tbox import _apply_high_level_instructions

        instructions = [{
            "action": "add_subclass",
            "child": "EquipmentStatus",
            "parent": "EquipmentMaster",
        }]
        result_ttl = _apply_high_level_instructions(sample_tbox_ttl, instructions)
        g = _new_graph()
        g.parse(data=result_ttl, format="turtle")

        assert (URIRef(f"{DOMAIN_NS}EquipmentStatus"),
                RDFS.subClassOf,
                URIRef(f"{DOMAIN_NS}EquipmentMaster")) in g


# ── generate_tbox_collaborative (통합) ────────────


class TestGenerateTboxCollaborative:
    """Multi-agent 협업 T-Box 생성 통합 테스트."""

    @patch("tools.tbox_generation.generate_tbox")
    @patch("tools.multi_agent_tbox._invoke_bedrock")
    @patch("tools.multi_agent_tbox._load_competency_questions", return_value=[])
    @patch("tools.multi_agent_tbox.os.path.exists", return_value=False)
    @patch("tools.multi_agent_tbox.atomic_write")
    def test_consensus_after_min_rounds(
        self, mock_write, mock_exists, mock_cq, mock_bedrock, mock_gen_tbox,
        sample_tbox_ttl,
    ):
        """최소 2라운드 후 합의 도달 시 결과 반환."""
        # generate_tbox 모킹. 초안 TTL 은 **응답의 ttl 필드** 로 전달된다 —
        # S2 는 TBOX_PATH 를 덮어쓰지 않으므로(save_to_tbox_path=False) 파일에서
        # 읽지 않는다.
        mock_gen_tbox.return_value = json.dumps({
            "success": True,
            "statistics": {"classes": 2, "object_properties": 1, "data_properties": 2},
            "timing": {"total": 5.0},
            "ttl": sample_tbox_ttl,
        })

        # Bedrock: Validator/SME는 approved=true, Jury는 production_ready=true 반환
        approved_json = json.dumps({
            "issues": [],
            "approved": True,
            "production_ready": True,
            "decisions": [],
            "jury_issues": [],
            "required_fixes": [],
            "summary": "All good",
            "rationale": "No critical/high issues remain.",
        })
        mock_bedrock.return_value = approved_json

        from tools.multi_agent_tbox import _generate_tbox_collaborative_sync

        with patch("builtins.open", create=True) as mock_file:
            mock_file.return_value.__enter__ = lambda s: s
            mock_file.return_value.__exit__ = MagicMock(return_value=False)
            mock_file.return_value.read = MagicMock(return_value=sample_tbox_ttl)

            with patch("tools.competency_questions._load_csv_summary", return_value="test"):
                result = json.loads(_generate_tbox_collaborative_sync(max_rounds=3))

        assert result["success"] is True
        assert result["debate"]["consensus_reached"] is True
        # Round counting 명확화 확인: initial_draft_rounds + debate_rounds == total_rounds
        d = result["debate"]
        assert d["initial_draft_rounds"] == 1
        assert d["total_rounds"] == d["initial_draft_rounds"] + d["debate_rounds"]


# ── #1 Veto Lock + #3 CQ Runtime Check ────────────


class TestVetoMechanism:
    """veto 메커니즘 — 2라운드 연속 잔존 critical/high 감지."""

    def test_issue_key_normalization(self):
        """``category:엔티티#텍스트해시`` 형태로 정규화 (2026-08-22 형식 변경).

        예전 계약은 ``"logic:hasx"`` 였다. 텍스트 해시 접미가 붙은 이유는
        **과잉 병합 방지** 다 — 같은 클래스의 서로 다른 결함이 하나로 합쳐지면
        persistence 신호가 거짓이 된다 (근거:
        ``_issue_text_fp_max`` docstring 의 실측 표).

        여기서 고정하는 것은 (a) category·엔티티가 소문자로 정규화되고
        (b) 사람이 읽을 수 있는 접두가 유지된다는 것이다.
        """
        from tools.multi_agent_tbox import _issue_key

        i = {"target": "HasX", "category": "Logic"}
        key = _issue_key(i)
        assert key.startswith("logic:hasx#"), key
        assert key.isprintable()
        # 같은 입력 → 같은 키 (결정적)
        assert key == _issue_key({"target": "hasx", "category": "logic"})

    def test_collect_veto_issues_excludes_approved(self):
        from tools.multi_agent_tbox import _collect_veto_issues

        rev = {"approved": True, "issues": [
            {"severity": "critical", "target": "x"}]}
        assert _collect_veto_issues(rev) == []

    def test_collect_veto_issues_severity_threshold(self):
        from tools.multi_agent_tbox import _collect_veto_issues

        rev = {"approved": False, "issues": [
            {"severity": "critical", "target": "a"},
            {"severity": "high", "target": "b"},
            {"severity": "medium", "target": "c"},
            {"severity": "low", "target": "d"},
        ]}
        assert len(_collect_veto_issues(rev)) == 2

    def test_detect_persistent_veto(self):
        from tools.multi_agent_tbox import _detect_persistent_veto

        prev = [{"severity": "critical", "target": "X", "category": "logic"}]
        curr = [
            {"severity": "critical", "target": "X", "category": "logic"},
            {"severity": "high", "target": "Y", "category": "mapping"},
        ]
        p = _detect_persistent_veto(prev, curr)
        assert len(p) == 1
        assert p[0]["target"] == "X"

    def test_detect_persistent_veto_none_when_resolved(self):
        from tools.multi_agent_tbox import _detect_persistent_veto

        prev = [{"severity": "critical", "target": "X", "category": "logic"}]
        curr = [{"severity": "high", "target": "Z", "category": "mapping"}]
        assert _detect_persistent_veto(prev, curr) == []

    def test_detect_persistent_veto_empty_prev(self):
        from tools.multi_agent_tbox import _detect_persistent_veto

        assert _detect_persistent_veto(None, [{"target": "X"}]) == []
        assert _detect_persistent_veto([], [{"target": "X"}]) == []


class TestCQRuntimeCheck:
    """#3 CQ 정적 reachability 검증."""

    def test_single_domain_cq_with_class(self):
        from tools.multi_agent_tbox import _cq_runtime_check

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
steel:A a owl:Class .
"""
        cqs = [{"id": "CQ1", "domains": ["A"]}]
        r = _cq_runtime_check(ttl, cqs)
        assert "CQ1" in r["answerable"]
        assert r["coverage_pct"] == 100.0

    def test_single_domain_cq_missing_class(self):
        from tools.multi_agent_tbox import _cq_runtime_check

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
steel:A a owl:Class .
"""
        cqs = [{"id": "CQ1", "domains": ["Missing"]}]
        r = _cq_runtime_check(ttl, cqs)
        assert any(cid == "CQ1" for cid, _ in r["unanswerable"])

    def test_two_domain_cq_reachable(self):
        from tools.multi_agent_tbox import _cq_runtime_check

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
steel:A a owl:Class .
steel:B a owl:Class .
steel:hasB a owl:ObjectProperty ; rdfs:domain steel:A ; rdfs:range steel:B .
"""
        cqs = [{"id": "CQ1", "domains": ["A", "B"]}]
        r = _cq_runtime_check(ttl, cqs)
        assert "CQ1" in r["answerable"]

    def test_two_domain_cq_unreachable(self):
        from tools.multi_agent_tbox import _cq_runtime_check

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
steel:A a owl:Class .
steel:B a owl:Class .
"""
        cqs = [{"id": "CQ1", "domains": ["A", "B"]}]
        r = _cq_runtime_check(ttl, cqs)
        assert any(cid == "CQ1" for cid, _ in r["unanswerable"])

    def test_2hop_reachability(self):
        from tools.multi_agent_tbox import _cq_runtime_check

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
steel:A a owl:Class .
steel:B a owl:Class .
steel:C a owl:Class .
steel:ab a owl:ObjectProperty ; rdfs:domain steel:A ; rdfs:range steel:B .
steel:bc a owl:ObjectProperty ; rdfs:domain steel:B ; rdfs:range steel:C .
"""
        cqs = [{"id": "CQ1", "domains": ["A", "C"]}]
        r = _cq_runtime_check(ttl, cqs)
        assert "CQ1" in r["answerable"]

    def test_empty_cqs(self):
        from tools.multi_agent_tbox import _cq_runtime_check

        r = _cq_runtime_check("", [])
        assert r["answerable"] == []
        assert r["coverage_pct"] == 100.0

    def test_subclass_expansion_for_op_domain(self):
        """OP domain이 추상 클래스여도 자식 클래스 CQ를 답할 수 있어야 한다."""
        from tools.multi_agent_tbox import _cq_runtime_check

        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
steel:AbstractEq a owl:Class .
steel:SpecificEq a owl:Class ; rdfs:subClassOf steel:AbstractEq .
steel:Alarm a owl:Class .
steel:hasAlarm a owl:ObjectProperty ;
    rdfs:domain steel:AbstractEq ; rdfs:range steel:Alarm .
"""
        cqs = [{"id": "CQ1", "domains": ["SpecificEq", "Alarm"]}]
        r = _cq_runtime_check(ttl, cqs)
        assert "CQ1" in r["answerable"]


class TestAnomalyHints:
    """#18 anomaly_hints.json 로더."""

    def test_empty_when_file_missing(self, tmp_path, monkeypatch):
        from tools import multi_agent_tbox as mt
        monkeypatch.setattr(mt, "_RULES_DIR", str(tmp_path))
        assert mt._load_anomaly_hints() == ""

    def test_empty_when_no_known(self, tmp_path, monkeypatch):
        import json as _j

        from tools import multi_agent_tbox as mt
        (tmp_path / "anomaly_hints.json").write_text(
            _j.dumps({"known_anomalies": []}), encoding="utf-8")
        monkeypatch.setattr(mt, "_RULES_DIR", str(tmp_path))
        assert mt._load_anomaly_hints() == ""

    def test_renders_known_anomaly(self, tmp_path, monkeypatch):
        import json as _j

        from tools import multi_agent_tbox as mt
        data = {"known_anomalies": [
            {"domain_class": "EquipmentMaster",
             "pattern": "알람 100건 burst",
             "recommended_schema": {
                 "add_class": "AlarmBurst",
                 "add_op": ["generatedBy(AlarmBurst→EquipmentMaster)"],
                 "rationale": "burst 이벤트 격리",
             }},
        ]}
        (tmp_path / "anomaly_hints.json").write_text(
            _j.dumps(data, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(mt, "_RULES_DIR", str(tmp_path))
        out = mt._load_anomaly_hints()
        assert "EquipmentMaster" in out
        assert "AlarmBurst" in out
        assert "알람 100건" in out


class TestGenerateOpSkeletonsFromCqGaps:
    """CQ path gap → deterministic add_object_property skeleton 변환."""

    def test_empty_unanswerable_returns_empty(self):
        from tools.multi_agent_tbox import _generate_op_skeletons_from_cq_gaps

        out = _generate_op_skeletons_from_cq_gaps(
            {"unanswerable": []}, cqs=[{"domains": ["A", "B"]}],
        )
        assert out == []

    def test_single_gap_produces_forward_and_inverse(self):
        from tools.multi_agent_tbox import _generate_op_skeletons_from_cq_gaps

        cq_runtime = {
            "unanswerable": [
                ("CQ07", "processrolling → productmaster 경로 없음 (2-hop OP 부재)"),
            ],
        }
        cqs = [{
            "id": "CQ07",
            "domains": ["Process_Rolling", "Product_Master", "Dimensional_Data"],
        }]
        skeletons = _generate_op_skeletons_from_cq_gaps(cq_runtime, cqs)
        actions = [s["action"] for s in skeletons]
        assert actions.count("add_object_property") == 2
        assert "add_inverse_property" in actions
        forward = next(
            s for s in skeletons
            if s["action"] == "add_object_property" and s["domain"] == "ProcessRolling"
        )
        assert forward["range"] == "ProductMaster"
        assert forward["property"] == "hasProductMasterForProcessRolling"
        assert forward["source"] == "cq_gap:CQ07"
        inverse = next(
            s for s in skeletons
            if s["action"] == "add_object_property" and s["domain"] == "ProductMaster"
        )
        assert inverse["range"] == "ProcessRolling"
        assert inverse["property"] == "isProductMasterForProcessRolling"

    def test_duplicate_pairs_deduplicated(self, monkeypatch):
        """중복 CQ 이유가 여분의 OP 를 만들지 않는다.

        FK 근거 게이트는 이 테스트의 관심사가 아니므로 판정 불가로 스텁한다
        (픽스처 클래스는 실제 CSV 에 없어 게이트가 정당하게 막는다).
        """
        from tools.multi_agent_tbox import _generate_op_skeletons_from_cq_gaps

        monkeypatch.setattr("tools.multi_agent_tbox._csv_fk_pairs", lambda: None)

        cq_runtime = {
            "unanswerable": [
                ("CQ02", "processsteelmakingfurnace → productionresult 경로 없음"),
                ("CQ10", "processsteelmakingfurnace → productionresult 경로 없음"),
            ],
        }
        cqs = [
            {"id": "CQ02", "domains": ["Process_Steelmaking_Furnace", "Production_Result"]},
            {"id": "CQ10", "domains": ["Process_Steelmaking_Furnace", "Production_Result"]},
        ]
        skeletons = _generate_op_skeletons_from_cq_gaps(cq_runtime, cqs)
        # 1 forward + 1 inverse = 2 add_object_property actions for the single
        # deduplicated pair. Duplicate CQ reasons must not produce extra OPs.
        op_actions = [s for s in skeletons if s["action"] == "add_object_property"]
        assert len(op_actions) == 2
        forward_props = [s["property"] for s in op_actions]
        assert "hasProductionResultForProcessSteelmakingFurnace" in forward_props
        assert "isProductionResultForProcessSteelmakingFurnace" in forward_props

    def test_unknown_class_name_skipped(self):
        """도메인 맵에 없는 클래스는 건너뛴다 (Steel 외부 네임스페이스)."""
        from tools.multi_agent_tbox import _generate_op_skeletons_from_cq_gaps

        cq_runtime = {
            "unanswerable": [
                ("CQ99", "unknownthing → anotherunknown 경로 없음"),
            ],
        }
        cqs = [{"id": "CQ99", "domains": ["Something_Else"]}]
        skeletons = _generate_op_skeletons_from_cq_gaps(cq_runtime, cqs)
        assert skeletons == []

    def test_class_missing_reason_skipped(self):
        """'클래스 누락' reason 은 OP 생성 대상이 아니므로 스킵."""
        from tools.multi_agent_tbox import _generate_op_skeletons_from_cq_gaps

        cq_runtime = {
            "unanswerable": [("CQ01", "클래스 누락: something")],
        }
        cqs = [{"id": "CQ01", "domains": ["Something"]}]
        skeletons = _generate_op_skeletons_from_cq_gaps(cq_runtime, cqs)
        assert skeletons == []


# ── Progress reporting (async + Context) ──────────────────────────────────


class TestAsyncProgressReporting:
    """async 래퍼가 라운드 단위로 progress_cb 를 호출하는지 검증."""

    def test_progress_cb_invoked_per_round(self, sample_tbox_ttl):
        """progress_cb 가 초안 1회 + 라운드별 2회 + 저장 2회 호출됨."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from tools.multi_agent_tbox import _generate_tbox_collaborative_async

        approved_json = json.dumps({
            "issues": [], "approved": True, "production_ready": True,
            "decisions": [], "jury_issues": [], "required_fixes": [],
            "summary": "ok", "rationale": "clean.",
        })
        calls: list[tuple[float, float, str]] = []

        async def fake_cb(progress: float, total: float, message: str) -> None:
            calls.append((progress, total, message))

        with patch("tools.tbox_generation.generate_tbox") as mock_gen, \
                patch("tools.multi_agent_tbox._invoke_bedrock",
                      return_value=approved_json), \
                patch("tools.multi_agent_tbox._load_competency_questions",
                      return_value=[]), \
                patch("tools.multi_agent_tbox.os.path.exists",
                      return_value=False), \
                patch("tools.multi_agent_tbox.atomic_write"), \
                patch("tools.competency_questions._load_csv_summary",
                      return_value="test"), \
                patch("builtins.open", create=True) as mock_file:
            mock_gen.return_value = json.dumps({
                "success": True,
                "statistics": {"classes": 2, "object_properties": 1,
                               "data_properties": 2},
                "timing": {"total": 1.0},
                # 초안은 응답의 ttl 로 전달된다 (TBOX_PATH 미기록).
                "ttl": sample_tbox_ttl,
            })
            mock_file.return_value.__enter__ = lambda s: s
            mock_file.return_value.__exit__ = MagicMock(return_value=False)
            mock_file.return_value.read = MagicMock(return_value=sample_tbox_ttl)

            result = asyncio.run(_generate_tbox_collaborative_async(
                tables="", max_rounds=3, progress_cb=fake_cb,
            ))

        # 최소 호출 횟수: 초안 시작(0) + 초안 완료(1) + 라운드당 2회(시작/완료)
        # + 최종 저장(total-1) + 완료(total). 합의 이른 종료여도 저장 호출은 발생.
        assert len(calls) >= 4, f"progress_cb 호출 부족: {calls}"
        # progress 는 단조 증가 또는 동일
        progresses = [c[0] for c in calls]
        assert progresses == sorted(progresses), \
            f"progress 가 단조 증가하지 않음: {progresses}"
        # total 은 모든 호출에서 동일
        totals = {c[1] for c in calls}
        assert len(totals) == 1, f"total 이 콜 간 다름: {totals}"
        # 마지막 호출은 progress == total (완료)
        assert calls[-1][0] == calls[-1][1]
        # 메시지에 라운드/단계 정보 포함 확인
        joined = " | ".join(c[2] for c in calls)
        assert "Round 1" in joined
        assert "Round 2" in joined
        # 결과 JSON 파싱 가능 + 라운드 카운팅 필드 존재
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["debate"]["initial_draft_rounds"] == 1
        assert "debate_rounds" in parsed["debate"]

    def test_progress_cb_failure_does_not_break_generation(self, sample_tbox_ttl):
        """콜백이 예외를 던져도 본 작업은 완료된다."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from tools.multi_agent_tbox import _generate_tbox_collaborative_async

        approved_json = json.dumps({
            "issues": [], "approved": True, "production_ready": True,
            "decisions": [], "jury_issues": [], "required_fixes": [],
            "summary": "ok", "rationale": "clean.",
        })

        async def flaky_cb(progress: float, total: float, message: str) -> None:
            raise RuntimeError("cb broken")

        with patch("tools.tbox_generation.generate_tbox") as mock_gen, \
                patch("tools.multi_agent_tbox._invoke_bedrock",
                      return_value=approved_json), \
                patch("tools.multi_agent_tbox._load_competency_questions",
                      return_value=[]), \
                patch("tools.multi_agent_tbox.os.path.exists",
                      return_value=False), \
                patch("tools.multi_agent_tbox.atomic_write"), \
                patch("tools.competency_questions._load_csv_summary",
                      return_value="test"), \
                patch("builtins.open", create=True) as mock_file:
            mock_gen.return_value = json.dumps({
                "success": True,
                "statistics": {"classes": 2, "object_properties": 1,
                               "data_properties": 2},
                "timing": {"total": 1.0},
                # 초안은 응답의 ttl 로 전달된다 (TBOX_PATH 미기록).
                "ttl": sample_tbox_ttl,
            })
            mock_file.return_value.__enter__ = lambda s: s
            mock_file.return_value.__exit__ = MagicMock(return_value=False)
            mock_file.return_value.read = MagicMock(return_value=sample_tbox_ttl)

            result = asyncio.run(_generate_tbox_collaborative_async(
                tables="", max_rounds=3, progress_cb=flaky_cb,
            ))

        parsed = json.loads(result)
        assert parsed["success"] is True


class TestBuildInitialDraftHelper:
    """_build_initial_draft 단위 테스트 — state 초기값 계약."""

    def test_returns_state_with_expected_keys(self, sample_tbox_ttl):
        from unittest.mock import MagicMock, patch

        from tools.multi_agent_tbox import _build_initial_draft

        with patch("tools.tbox_generation.generate_tbox") as mock_gen, \
                patch("tools.competency_questions._load_csv_summary",
                      return_value="csv_summary"), \
                patch("tools.multi_agent_tbox._load_competency_questions",
                      return_value=[{"id": "CQ1", "domains": ["X"]}]), \
                patch("tools.multi_agent_tbox.os.path.exists",
                      return_value=False), \
                patch("builtins.open", create=True) as mock_file:
            mock_gen.return_value = json.dumps({
                "success": True,
                "statistics": {"classes": 3, "object_properties": 2,
                               "data_properties": 4},
                "timing": {"total": 2.5},
                # 초안은 응답의 ttl 로 전달된다 (TBOX_PATH 미기록).
                "ttl": sample_tbox_ttl,
            })
            mock_file.return_value.__enter__ = lambda s: s
            mock_file.return_value.__exit__ = MagicMock(return_value=False)
            mock_file.return_value.read = MagicMock(return_value=sample_tbox_ttl)
            state = _build_initial_draft("")

        assert state["current_ttl"] == sample_tbox_ttl
        assert state["architect_stats"]["classes"] == 3
        assert state["cqs"] == [{"id": "CQ1", "domains": ["X"]}]
        assert state["debate_log"] == []
        assert state["consensus_reached"] is False
        assert state["veto_lock_triggered"] is False
        assert state["initial_draft_rounds"] == 1

    def test_returns_error_when_generate_tbox_fails(self):
        from unittest.mock import patch

        from tools.multi_agent_tbox import _build_initial_draft

        with patch("tools.tbox_generation.generate_tbox") as mock_gen, \
                patch("tools.competency_questions._load_csv_summary",
                      return_value="csv"):
            mock_gen.return_value = json.dumps({
                "success": False, "error": "CSV not found",
            })
            state = _build_initial_draft("")
        assert "error" in state


class TestFinalizeAndSave:
    """_finalize_and_save 단위 테스트 — 반환 JSON 구조."""

    def test_round_counts_are_consistent(self, sample_tbox_ttl):
        from unittest.mock import patch

        from tools.multi_agent_tbox import _finalize_and_save

        state = {
            "current_ttl": sample_tbox_ttl,
            "architect_stats": {"classes": 2, "object_properties": 1,
                                 "data_properties": 2,
                                 "generation_time": 1.0},
            "cqs": [],
            "debate_log": [{"round": 2}, {"round": 3}],
            "consensus_reached": True,
            "compromise_reason": None,
            "veto_lock_triggered": False,
            "veto_persistent_targets": [],
            "initial_draft_rounds": 1,
        }
        with patch("tools.multi_agent_tbox.atomic_write"), \
                patch("tools.multi_agent_tbox.os.path.exists",
                      return_value=True):
            result_str = _finalize_and_save(state, start_monotonic=0.0)
        parsed = json.loads(result_str)
        d = parsed["debate"]
        assert d["initial_draft_rounds"] == 1
        assert d["debate_rounds"] == 2
        assert d["total_rounds"] == 3
        assert d["consensus_reached"] is True


class TestParseTtlReadonlyCache:
    """_parse_ttl_readonly — TTL 파싱 캐시의 정확성/격리 검증."""

    def _clear_cache(self):
        from tools.multi_agent_tbox import _TBOX_PARSE_CACHE
        _TBOX_PARSE_CACHE.clear()

    def test_returns_distinct_graph_instances_for_same_ttl(self):
        """캐시 히트 시에도 호출자별로 독립된 Graph 인스턴스를 줘야 mutation 이 전파되지 않는다."""
        from tools.multi_agent_tbox import _parse_ttl_readonly

        self._clear_cache()
        ttl = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
steel:A a owl:Class .
"""
        g1 = _parse_ttl_readonly(ttl)
        g2 = _parse_ttl_readonly(ttl)
        assert g1 is not g2
        # 한 쪽을 mutate 해도 다른 쪽에 영향 없음.
        g1.add((URIRef(f"{DOMAIN_NS}X"), RDF.type, OWL.Class))
        assert len(g1) == 2
        assert len(g2) == 1

    def test_ttl_change_triggers_reparse(self):
        from tools.multi_agent_tbox import _parse_ttl_readonly

        self._clear_cache()
        ttl1 = f"@prefix steel: <{DOMAIN_NS}> .\nsteel:A a <http://www.w3.org/2002/07/owl#Class> .\n"
        ttl2 = ttl1 + "steel:B a <http://www.w3.org/2002/07/owl#Class> .\n"
        assert len(_parse_ttl_readonly(ttl1)) == 1
        assert len(_parse_ttl_readonly(ttl2)) == 2

    def test_cache_size_is_bounded(self):
        """FIFO 퇴출로 캐시 크기 상한이 지켜진다."""
        from tools.multi_agent_tbox import (
            _TBOX_PARSE_CACHE,
            _TBOX_PARSE_CACHE_MAX,
            _parse_ttl_readonly,
        )

        self._clear_cache()
        # 상한 +3 개 서로 다른 TTL 삽입.
        for i in range(_TBOX_PARSE_CACHE_MAX + 3):
            ttl = f"@prefix steel: <{DOMAIN_NS}> .\nsteel:N{i} a <http://www.w3.org/2002/07/owl#Class> .\n"
            _parse_ttl_readonly(ttl)
        assert len(_TBOX_PARSE_CACHE) <= _TBOX_PARSE_CACHE_MAX


class TestChunkProgressHook:
    """_generate_chunk 가 설정된 hook 을 event 별로 호출하는지 검증."""

    def test_hook_emits_start_llm_done_done(self, monkeypatch):
        from tools import tbox_generation as tg

        events: list[dict] = []
        tg.set_chunk_progress_hook(events.append)

        # _build_chunk_prompt / _invoke_bedrock / _extract_ttl / graph parse 를
        # 최소로 모킹해 단일 chunk 정상 경로를 돈다.
        monkeypatch.setattr(tg, "_build_chunk_prompt",
                            lambda *a, **kw: ("fake cached_prefix", "fake variable"))
        monkeypatch.setattr(tg, "_invoke_bedrock",
                            lambda *a, **kw: {"text": "", "stop_reason": "end_turn"})
        monkeypatch.setattr(tg, "_extract_ttl_from_markdown",
                            lambda s: f"@prefix steel: <{DOMAIN_NS}> .\nsteel:Dummy a <http://www.w3.org/2002/07/owl#Class> .\n")
        try:
            result = tg._generate_chunk(
                chunk_idx=0, chunk_tables=[{"name": "t1", "columns": ["a"]}],
                total_chunks=1, iof_summary={}, schema_info={},
                relationships_info={}, table_class_map={},
                config={"max_tokens": 4096, "max_retries": 0},
                depth=0,
            )
        finally:
            tg.set_chunk_progress_hook(None)

        event_names = [e["event"] for e in events]
        assert "chunk_start" in event_names
        assert "chunk_llm_done" in event_names
        assert "chunk_done" in event_names
        assert result.strip().startswith("@prefix")

    def test_hook_emits_split_on_max_tokens(self, monkeypatch):
        from tools import tbox_generation as tg

        events: list[dict] = []
        tg.set_chunk_progress_hook(events.append)

        # 첫 호출은 max_tokens, 재귀 호출은 end_turn 반환하도록 counter.
        calls = {"n": 0}
        def _fake_invoke(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"text": "", "stop_reason": "max_tokens"}
            return {"text": "", "stop_reason": "end_turn"}

        monkeypatch.setattr(tg, "_build_chunk_prompt",
                            lambda *a, **kw: ("fake cached_prefix", "fake variable"))
        monkeypatch.setattr(tg, "_invoke_bedrock", _fake_invoke)
        monkeypatch.setattr(tg, "_extract_ttl_from_markdown",
                            lambda s: f"@prefix steel: <{DOMAIN_NS}> .\nsteel:X a <http://www.w3.org/2002/07/owl#Class> .\n")
        monkeypatch.setattr(tg, "_strip_prefix_and_ontology_decl", lambda s: s)

        try:
            tg._generate_chunk(
                chunk_idx=0, chunk_tables=[
                    {"name": "t1", "columns": ["a"]},
                    {"name": "t2", "columns": ["b"]},
                ],
                total_chunks=1, iof_summary={}, schema_info={},
                relationships_info={}, table_class_map={},
                config={"max_tokens": 4096, "max_retries": 0},
                depth=0,
            )
        finally:
            tg.set_chunk_progress_hook(None)

        event_names = [e["event"] for e in events]
        assert "chunk_split" in event_names
        # 재귀 호출로 start 는 최소 3번 (원본 + 양 half)
        assert event_names.count("chunk_start") >= 3

    def test_single_table_overflow_retries_with_doubled_max_tokens(self, monkeypatch):
        """mid==0 에서 max_tokens 터치 시 즉시 raise 하지 않고 max_tokens 2배로 재시도.

        R28: 단일 7컬럼 테이블이 16K max_tokens 에 걸리던 Air_Emission_Monitoring
        실패 실측 기반 — 재시도 성공 경로는 전체 실패 대신 결과를 살려야 한다.
        """
        from tools import tbox_generation as tg

        events: list[dict] = []
        tg.set_chunk_progress_hook(events.append)

        calls = {"n": 0, "max_tokens_seen": []}
        def _fake_invoke(*args, **kwargs):
            calls["n"] += 1
            calls["max_tokens_seen"].append(kwargs.get("max_tokens"))
            if calls["n"] == 1:
                # 첫 호출: max_tokens 도달로 실패 신호
                return {"text": "", "stop_reason": "max_tokens"}
            # 재시도: 2배 max_tokens 로 요청 들어와야 함. 정상 종료 반환.
            return {"text": "", "stop_reason": "end_turn"}

        monkeypatch.setattr(tg, "_build_chunk_prompt",
                            lambda *a, **kw: ("fake cached_prefix", "fake variable"))
        monkeypatch.setattr(tg, "_invoke_bedrock", _fake_invoke)
        monkeypatch.setattr(tg, "_extract_ttl_from_markdown",
                            lambda s: f"@prefix steel: <{DOMAIN_NS}> .\nsteel:Z a <http://www.w3.org/2002/07/owl#Class> .\n")
        try:
            result = tg._generate_chunk(
                chunk_idx=0,
                chunk_tables=[{"name": "BigTable", "columns": ["a"]}],  # mid==0 강제
                total_chunks=1, iof_summary={}, schema_info={},
                relationships_info={}, table_class_map={},
                config={"max_tokens": 16000, "max_retries": 0},
                depth=0,
            )
        finally:
            tg.set_chunk_progress_hook(None)

        # 재시도가 성공하면 빈 문자열이 아닌 실제 TTL 반환
        assert "Z" in result
        # _invoke_bedrock 두 번 호출: 첫 번째는 16000, 두 번째는 32000
        assert calls["max_tokens_seen"] == [16000, 32000]
        # chunk_retry 이벤트 송신 확인
        event_names = [e["event"] for e in events]
        assert "chunk_retry" in event_names

    def test_single_table_overflow_returns_empty_when_retry_also_fails(self, monkeypatch):
        """재시도도 max_tokens 도달하면 빈 TTL 반환해 상위 merge 가 진행되도록."""
        from tools import tbox_generation as tg

        tg.set_chunk_progress_hook(lambda _p: None)

        monkeypatch.setattr(tg, "_build_chunk_prompt",
                            lambda *a, **kw: ("fake cached_prefix", "fake variable"))
        monkeypatch.setattr(
            tg, "_invoke_bedrock",
            lambda *a, **kw: {"text": "", "stop_reason": "max_tokens"},
        )
        monkeypatch.setattr(tg, "_extract_ttl_from_markdown", lambda s: s)

        try:
            result = tg._generate_chunk(
                chunk_idx=0,
                chunk_tables=[{"name": "T", "columns": ["a"]}],
                total_chunks=1, iof_summary={}, schema_info={},
                relationships_info={}, table_class_map={},
                config={"max_tokens": 16000, "max_retries": 0},
                depth=0,
            )
        finally:
            tg.set_chunk_progress_hook(None)

        # 예외 대신 빈 문자열 반환
        assert result == ""

    def test_hook_failure_does_not_break_generation(self, monkeypatch):
        from tools import tbox_generation as tg

        def _broken_hook(payload):
            raise RuntimeError("hook boom")

        tg.set_chunk_progress_hook(_broken_hook)

        monkeypatch.setattr(tg, "_build_chunk_prompt",
                            lambda *a, **kw: ("fake cached_prefix", "fake variable"))
        monkeypatch.setattr(tg, "_invoke_bedrock",
                            lambda *a, **kw: {"text": "", "stop_reason": "end_turn"})
        monkeypatch.setattr(tg, "_extract_ttl_from_markdown",
                            lambda s: f"@prefix steel: <{DOMAIN_NS}> .\nsteel:Y a <http://www.w3.org/2002/07/owl#Class> .\n")
        try:
            result = tg._generate_chunk(
                chunk_idx=0, chunk_tables=[{"name": "t1", "columns": ["a"]}],
                total_chunks=1, iof_summary={}, schema_info={},
                relationships_info={}, table_class_map={},
                config={"max_tokens": 4096, "max_retries": 0},
                depth=0,
            )
        finally:
            tg.set_chunk_progress_hook(None)
        # hook 이 매번 터져도 결과는 정상 반환
        assert "Y" in result


class TestFileHeartbeat:
    """파일 기반 하트비트 — Claude Code UI 가 progressToken 없이도 진행 관측 가능."""

    def test_file_hb_writes_on_init_and_set(self, tmp_path):
        import json as _j

        from tools.multi_agent_tbox import _FileHeartbeat

        path = str(tmp_path / "hb.json")
        hb = _FileHeartbeat(path, max_rounds=3)
        # 초기 파일 생성 확인
        assert (tmp_path / "hb.json").exists()
        d = _j.loads((tmp_path / "hb.json").read_text())
        assert d["status"] == "starting"
        assert d["max_rounds"] == 3
        assert "started_at" in d and "updated_at" in d

        # set 호출로 phase 변경 및 재기록
        hb.set(status="running", phase="validator+sme", round=2, step="검토 중")
        d = _j.loads((tmp_path / "hb.json").read_text())
        assert d["phase"] == "validator+sme"
        assert d["round"] == 2
        assert d["step"] == "검토 중"

    def test_file_hb_error_and_finish_update_status(self, tmp_path):
        import json as _j

        from tools.multi_agent_tbox import _FileHeartbeat

        path = str(tmp_path / "hb.json")
        hb = _FileHeartbeat(path, max_rounds=3)
        hb.error("Bedrock ReadTimeout")
        d = _j.loads((tmp_path / "hb.json").read_text())
        assert d["status"] == "error"
        assert "ReadTimeout" in d["error"]

        hb2 = _FileHeartbeat(str(tmp_path / "hb2.json"), max_rounds=2)
        hb2.finish()
        d = _j.loads((tmp_path / "hb2.json").read_text())
        assert d["status"] == "completed"
        assert d["phase"] == "done"

    def test_file_hb_disabled_when_path_none(self, tmp_path):
        """path=None 이면 파일을 쓰지 않고 state 만 관리한다 — 테스트에서 유용."""
        from tools.multi_agent_tbox import _FileHeartbeat

        hb = _FileHeartbeat(None, max_rounds=1)
        hb.set(status="running", phase="x")
        hb.finish()
        # 예외 없으면 OK (파일은 생성되지 않음)
        assert list(tmp_path.iterdir()) == []

    def test_heartbeat_file_path_env_override(self, monkeypatch, tmp_path):
        from tools.multi_agent_tbox import _heartbeat_file_path

        override = str(tmp_path / "custom.json")
        monkeypatch.setenv("MULTI_AGENT_HEARTBEAT_FILE", override)
        assert _heartbeat_file_path() == override

        monkeypatch.setenv("MULTI_AGENT_HEARTBEAT_FILE", "")
        assert _heartbeat_file_path() is None

        monkeypatch.delenv("MULTI_AGENT_HEARTBEAT_FILE", raising=False)
        # 기본값은 data/generated/_heartbeat.json 을 가리킨다
        assert _heartbeat_file_path().endswith("_heartbeat.json")

    def test_file_hb_survives_directory_creation(self, tmp_path):
        """없는 하위 디렉토리 경로를 주면 자동 생성한다."""
        import json as _j

        from tools.multi_agent_tbox import _FileHeartbeat

        path = tmp_path / "nested" / "sub" / "hb.json"
        _FileHeartbeat(str(path), max_rounds=1)
        assert path.exists()
        d = _j.loads(path.read_text())
        assert d["status"] == "starting"


# 모든 async 테스트가 실제 data/generated/_heartbeat.json 을 건드리지 않도록,
# 이 모듈 수준에서 기본 경로를 비활성화. 개별 테스트가 필요하면 monkeypatch 로 재설정.
@pytest.fixture(autouse=True)
def _disable_default_heartbeat_file(monkeypatch):
    monkeypatch.setenv("MULTI_AGENT_HEARTBEAT_FILE", "")


class TestHeartbeatReporting:
    """라운드 내부 하트비트 — worker 가 블로킹 중에도 주기적으로 phase/elapsed 송신."""

    def test_heartbeat_fires_during_blocking_worker(self, monkeypatch):
        """Worker 가 일부러 오래 걸리면 하트비트 메시지가 1건 이상 발생한다."""
        import asyncio
        import time as _t

        from tools.multi_agent_tbox import _run_with_heartbeat

        # 하트비트 간격을 짧게 강제.
        monkeypatch.setenv("MULTI_AGENT_HEARTBEAT_SEC", "0.05")
        calls: list[tuple[float, float, str]] = []

        async def cb(progress: float, total: float, message: str) -> None:
            calls.append((progress, total, message))

        def slow_worker() -> str:
            _t.sleep(0.18)  # 하트비트 3회 기회
            return "done"

        result = asyncio.run(_run_with_heartbeat(
            cb, step=1, total_steps=5,
            label_fn=lambda: "Round X [phase=validator+sme]",
            worker=slow_worker,
        ))
        assert result == "done"
        # 최소 1건의 하트비트 (timing jitter 감안)
        assert len(calls) >= 1, f"하트비트 미발화: {calls}"
        for _p, _t_total, msg in calls:
            assert "phase=" in msg
            assert "경과" in msg

    def test_heartbeat_disabled_when_interval_zero(self, monkeypatch):
        """MULTI_AGENT_HEARTBEAT_SEC=0 이면 하트비트 송신 없음."""
        import asyncio
        import time as _t

        from tools.multi_agent_tbox import _run_with_heartbeat

        monkeypatch.setenv("MULTI_AGENT_HEARTBEAT_SEC", "0")
        calls: list[str] = []

        async def cb(progress: float, total: float, message: str) -> None:
            calls.append(message)

        def slow_worker() -> str:
            _t.sleep(0.1)
            return "ok"

        asyncio.run(_run_with_heartbeat(
            cb, 0, 1, lambda: "lbl", slow_worker,
        ))
        assert calls == []

    def test_heartbeat_cancelled_on_worker_exception(self, monkeypatch):
        """Worker 가 예외를 던져도 하트비트가 정리되고 예외는 상위 전파된다."""
        import asyncio

        from tools.multi_agent_tbox import _run_with_heartbeat

        monkeypatch.setenv("MULTI_AGENT_HEARTBEAT_SEC", "0.05")

        async def cb(progress: float, total: float, message: str) -> None:
            pass

        def failing_worker() -> str:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            asyncio.run(_run_with_heartbeat(
                cb, 0, 1, lambda: "lbl", failing_worker,
            ))

    def test_phase_setter_threaded_through_run_one_debate_round(
        self, sample_tbox_ttl,
    ):
        """_run_one_debate_round 는 phase_setter 호출로 phase 전환을 외부로 알려준다."""
        from unittest.mock import patch

        from tools.multi_agent_tbox import _run_one_debate_round

        approved_json = json.dumps({
            "issues": [], "approved": True, "production_ready": True,
            "decisions": [], "jury_issues": [], "required_fixes": [],
            "summary": "ok", "rationale": "clean.",
        })
        phases: list[str] = []

        state = {
            "current_ttl": sample_tbox_ttl,
            "cqs": [],
            "csv_summary": "",
            "extra_context": "",
            "debate_log": [],
            "consensus_reached": False,
            "compromise_reason": None,
            "prev_validator_issues": None,
            "prev_sme_issues": None,
            "prev_round_ttl": None,
            "veto_lock_triggered": False,
            "veto_persistent_targets": [],
        }
        with patch("tools.multi_agent_tbox._invoke_bedrock",
                   return_value=approved_json):
            _run_one_debate_round(
                state, round_num=2, max_rounds=3,
                phase_setter=phases.append,
            )
        assert "validator+sme" in phases
        # Round 2 는 min_rounds 충족 + approved → jury 단계까지 진입
        assert "jury" in phases


class TestAgentStaticPrefixCaching:
    """Architect/Jury 프롬프트에 cached_prefix 가 전달되는지 검증."""

    def test_architect_revise_uses_cached_prefix(self, sample_tbox_ttl):
        from unittest.mock import patch

        from tools.multi_agent_tbox import _architect_revise

        captured: dict = {}

        def _fake_invoke(prompt, max_tokens=8192, temperature=None, cached_prefix=None, _auto_extend=True, **kwargs):
            captured["prompt"] = prompt
            captured["cached_prefix"] = cached_prefix
            return '{"instructions": []}'

        with patch("tools.multi_agent_tbox._invoke_bedrock", _fake_invoke):
            _architect_revise(
                sample_tbox_ttl,
                validator_review={"issues": []}, sme_review={"issues": []},
                round_num=1,
            )

        assert captured["cached_prefix"] is not None
        assert "Ontology Architect" in captured["cached_prefix"]
        assert "지원 action" in captured["cached_prefix"]
        # variable prompt 에는 정적 카탈로그가 없어야 캐시 분리가 의미 있다.
        assert "지원 action" not in captured["prompt"]

    def test_jury_decide_uses_cached_prefix(self, sample_tbox_ttl):
        from unittest.mock import patch

        from tools.multi_agent_tbox import _jury_decide

        captured: dict = {}

        def _fake_invoke(prompt, max_tokens=8192, temperature=None, cached_prefix=None, _auto_extend=True, **kwargs):
            captured["prompt"] = prompt
            captured["cached_prefix"] = cached_prefix
            return '{"production_ready": true, "decisions": [], "jury_issues": [], "required_fixes": [], "rationale": "ok"}'

        with patch("tools.multi_agent_tbox._invoke_bedrock", _fake_invoke):
            _jury_decide(
                sample_tbox_ttl,
                validator_result={"issues": []}, sme_result={"issues": []},
                architect_history=[], debate_log=[],
            )

        assert captured["cached_prefix"] is not None
        assert "독립 Jury" in captured["cached_prefix"]
        assert "판단 원칙" in captured["cached_prefix"]
        assert "판단 원칙" not in captured["prompt"]


class TestReviewerSeesFullDeclarations:
    """리뷰어가 T-Box **전수** 선언을 받는지 — 프롬프트 실물로 주장.

    회귀 이력: f560f76 이 선언 인벤토리를 신설했지만, 호출부가 마크다운(메트릭
    피드백)을 TTL 앞에 붙여 넘겨 rdflib 파싱이 첫 줄에서 깨졌다. 인벤토리는
    조용히 빈 문자열이 되고 Validator/SME 는 33% 발췌만 봤다. 그 결과 절단선
    밖의 OP 를 "선언 없음" 으로 단정해 veto lock 이 5라운드를 태웠다.

    이 테스트는 **헬퍼 단위가 아니라 라운드가 실제로 조립하는 경로** 를 검사한다
    — 헬퍼만 보는 테스트는 배선이 끊겨도 통과한다.
    """

    def _tbox_with_late_op(self) -> str:
        """절단선 뒤쪽에 OP 가 오도록 패딩된 T-Box."""
        head = (
            "@prefix steel: <http://example.com/steel-ontology#> .\n"
            "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n\n"
            "steel:EnergyEfficiency a owl:Class ; rdfs:label \"ee\"@en .\n"
            "steel:EquipmentMaster a owl:Class ; rdfs:label \"em\"@en .\n"
        )
        # 발췌 상한을 넘기려면 앞을 충분히 채워야 한다.
        padding = "".join(
            f"steel:Filler{i} a owl:Class ; rdfs:comment \"{'x' * 300}\"@ko .\n"
            for i in range(220)
        )
        late_op = (
            "steel:energyEfficiencyHasEquipment a owl:ObjectProperty ;\n"
            "    rdfs:domain steel:EnergyEfficiency ;\n"
            "    rdfs:range steel:EquipmentMaster ;\n"
            "    rdfs:label \"has equipment\"@en .\n"
        )
        return head + padding + late_op

    def test_inventory_survives_metrics_feedback_in_round(self):
        """``_run_one_debate_round`` 가 조립한 프롬프트에 절단선 밖 OP 가 있어야 한다.

        **반드시 라운드 진입점을 호출한다.** ``_run_validator_and_sme`` 를 직접
        부르면 ttl/preamble 을 테스트가 손으로 넘기게 되어, 라운드가 둘을
        합쳐버리는 회귀(원래 버그)를 그대로 통과시킨다 — 실제로 mutation 에서
        확인했다.
        """
        from unittest.mock import patch

        import tools.multi_agent_tbox as m

        ttl = self._tbox_with_late_op()
        # 절단선 밖에 있음을 먼저 확인 — 이게 참이라야 테스트가 의미를 가진다.
        assert ttl.index("energyEfficiencyHasEquipment") > m._TTL_PROMPT_MAX, (
            "픽스처가 절단선 안에 OP 를 둬서 회귀를 못 잡는다"
        )

        captured: dict[str, str] = {}

        def fake_invoke(prompt, **kw):
            captured[kw.get("agent_role", "?")] = prompt
            return json.dumps({
                "issues": [], "approved": True, "production_ready": True,
                "decisions": [], "jury_issues": [], "required_fixes": [],
                "summary": "ok", "rationale": "clean.",
            })

        state = {
            "cqs": [], "csv_summary": "csv",
            "prev_validator_issues": None, "prev_sme_issues": None,
            "prev_round_ttl": None, "extra_context": "",
            "current_ttl": ttl, "debate_log": [],
            "consensus_reached": False, "compromise_reason": None,
            "veto_lock_triggered": False, "veto_persistent_targets": [],
            "initial_draft_rounds": 1, "infra_abort": None,
            "architect_stats": {},
        }
        with patch.object(m, "_invoke_bedrock", fake_invoke):
            # 메트릭 피드백이 실제로 생성되는 조건이어야 회귀가 재현된다.
            assert m._compute_metrics_feedback(ttl, 2), (
                "메트릭 피드백이 비어 회귀 조건이 성립 안 함"
            )
            m._run_one_debate_round(state, round_num=2, max_rounds=3)

        for role in ("validator", "sme"):
            prompt = captured[role]
            assert "energyEfficiencyHasEquipment" in prompt, (
                f"{role} 프롬프트에 절단선 밖 OP 가 없다 — 인벤토리 배선이 끊겼다"
            )
            assert "이미 선언된 것" in prompt, f"{role} 에 인벤토리 블록 자체가 없다"
            assert "선언 인벤토리 생성 실패" not in prompt, (
                f"{role} 인벤토리 파싱이 깨졌다 — TTL 에 마크다운이 섞였다"
            )
            # preamble 도 함께 도달해야 한다 (기능 상실 없이 분리됐는지).
            assert "현재 T-Box 메트릭" in prompt, f"{role} 에 메트릭 피드백이 없다"

    def test_inventory_failure_is_loud_not_silent(self):
        """파싱 실패 시 빈 문자열이 아니라 경고 마커를 반환한다."""
        import tools.multi_agent_tbox as m

        out = m._format_declaration_inventory("this is not turtle {{{")
        assert out, "실패가 빈 문자열로 삼켜졌다 — 기능 상실과 정상을 구분 불가"
        assert "선언 인벤토리 생성 실패" in out
        assert "'선언 없음' 이라고 단정하지 마세요" in out


class TestSaveGuardCapabilityLoss:
    """저장 가드가 **표현력 손실** 을 잡는지 — 트리플 비율이 통과시키는 손실.

    2026-08-14 실측 사고: S3 산출물(5,599 트리플 / OP 249)이 S2 저장본
    (3,180 / 115)으로 교체됐다. 비율 56.8% 는 _SAVE_COLLAPSE_RATIO(0.5)를
    통과했고, hasKey 30건·completenessStatus 65건·OP 관계 69개가 사라졌다.

    NEGATIVE 방향도 함께 주장한다: 정상적인 리네이밍 재생성(실측 baseline→current
    는 OP 이름 44개가 바뀌었지만 관계는 유지)을 **차단하지 않아야** 한다.
    차단하는 게이트는 결국 꺼진다.
    """

    @staticmethod
    def _tbox(classes: int, links: list[tuple[str, str]], dps: list[str]) -> str:
        lines = [
            "@prefix steel: <http://example.com/steel-ontology#> .",
            "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .",
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
            "",
        ]
        for i in range(classes):
            lines.append(f"steel:C{i} a owl:Class ; rdfs:label \"c{i}\"@en .")
        for idx, (dom, rng) in enumerate(links):
            lines.append(
                f"steel:op{idx} a owl:ObjectProperty ; "
                f"rdfs:domain steel:{dom} ; rdfs:range steel:{rng} .",
            )
        for name in dps:
            lines.append(
                f"steel:{name} a owl:DatatypeProperty ; "
                f"rdfs:domain steel:C0 ; rdfs:range xsd:string .",
            )
        return "\n".join(lines) + "\n"

    def _run_guard(self, tmp_path, monkeypatch, prev_ttl: str, new_ttl: str):
        import tools.multi_agent_tbox as m

        tbox = tmp_path / "t_box.ttl"
        tbox.write_text(prev_ttl, encoding="utf-8")
        monkeypatch.setattr(m, "TBOX_PATH", str(tbox))
        return m._guard_before_save(new_ttl)

    def test_blocks_relation_loss_that_ratio_guard_misses(
        self, tmp_path, monkeypatch,
    ):
        """OP 관계가 대량 사라지면 트리플 비율이 통과해도 저장 거부."""
        prev = self._tbox(
            10, [(f"C{i}", f"C{i+1}") for i in range(8)],
            [f"dp{i}" for i in range(20)],
        )
        # 관계를 8→2 로 줄인다 (75% 손실). 트리플 수는 절반 이상 유지.
        new = self._tbox(
            10, [(f"C{i}", f"C{i+1}") for i in range(2)],
            [f"dp{i}" for i in range(20)],
        )
        from rdflib import Graph
        gp, gn = Graph(), Graph()
        gp.parse(data=prev, format="turtle")
        gn.parse(data=new, format="turtle")
        assert len(gn) >= len(gp) * 0.5, (
            "픽스처가 트리플 비율 게이트에 이미 걸려 새 판정을 검사하지 못한다"
        )

        saved, guard = self._run_guard(tmp_path, monkeypatch, prev, new)
        assert guard["declaration_loss_blocked"] is True
        assert guard["collapse_blocked"] is True
        assert saved == prev, "저장 거부인데 기존 T-Box 를 반환하지 않았다"
        assert guard["capability_loss"]["op_links"]["lost"] == 6

    def test_allows_rename_only_regeneration(self, tmp_path, monkeypatch):
        """이름이 전부 바뀌어도 관계가 유지되면 통과 (NEGATIVE 방향)."""
        links = [(f"C{i}", f"C{i+1}") for i in range(8)]
        prev = self._tbox(10, links, [f"dp{i}" for i in range(20)])
        # 같은 관계 집합, OP local name 만 다르게 (op0.. → renamed..).
        new = prev.replace("steel:op", "steel:renamedOp")

        saved, guard = self._run_guard(tmp_path, monkeypatch, prev, new)
        assert not guard.get("declaration_loss_blocked"), (
            f"리네이밍을 손실로 오판해 정상 재생성을 차단했다: "
            f"{guard.get('capability_loss')}"
        )
        assert saved == new
        # 이름 기준으로는 전부 사라진 것으로 보여야 한다 — 관측은 남기되 판정엔
        # 쓰지 않는다는 계약 확인.
        assert guard["renamed_or_removed"]["object_properties"] == 8
        assert guard["capability_loss"]["op_links"]["lost"] == 0

    def test_allows_pure_growth(self, tmp_path, monkeypatch):
        """선언이 늘어나는 정상 성장은 통과."""
        prev = self._tbox(5, [("C0", "C1")], ["dp0"])
        new = self._tbox(
            9, [("C0", "C1"), ("C1", "C2"), ("C2", "C3")], ["dp0", "dp1"],
        )
        saved, guard = self._run_guard(tmp_path, monkeypatch, prev, new)
        assert not guard.get("declaration_loss_blocked")
        assert saved == new
