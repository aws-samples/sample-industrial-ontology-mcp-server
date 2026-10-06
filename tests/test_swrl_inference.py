"""I4 — SWRL 추론 도구 단위 테스트 (tools/swrl_inference.py).

tests cover:
- SWRL_ENABLED 환경변수로 opt-in 분기
- .swrl 파일 파싱 (라벨, 주석, 공백 라인)
- 빈 디렉토리 처리
- Pellet 실제 호출은 @pytest.mark.slow 로 분리
"""

import json

import pytest

try:
    import owlready2  # noqa: F401
    _OWLREADY2_AVAILABLE = True
except Exception:
    _OWLREADY2_AVAILABLE = False


# ── 환경변수 분기 ────────────────────────────────────


class TestSwrlEnabledFlag:
    """SWRL_ENABLED 환경변수로 skip 여부 결정."""

    def test_swrl_disabled_by_default(self, monkeypatch):
        """환경변수 미설정 → skip."""
        from tools.swrl_inference import run_swrl_inference

        monkeypatch.delenv("SWRL_ENABLED", raising=False)
        result = json.loads(run_swrl_inference())
        assert result["success"] is True
        assert result.get("skipped") is True
        assert "SWRL_ENABLED" in result.get("reason", "")

    def test_swrl_disabled_explicit_false(self, monkeypatch):
        """SWRL_ENABLED=false → skip."""
        from tools.swrl_inference import run_swrl_inference

        monkeypatch.setenv("SWRL_ENABLED", "false")
        result = json.loads(run_swrl_inference())
        assert result["success"] is True
        assert result.get("skipped") is True

    def test_swrl_enabled_accepts_truthy_variants(self, monkeypatch, tmp_path):
        """SWRL_ENABLED=true/TRUE/True 등 대소문자 무시."""
        from tools.swrl_inference import run_swrl_inference

        # empty dir — no rules → zero rules (not skipped by env flag)
        # swrl_dir 는 SWRL_DIR_DEFAULT 자신이나 그 하위만 받으므로 기준을 tmp_path 로 옮긴다.
        monkeypatch.setattr("tools.swrl_inference.SWRL_DIR_DEFAULT", str(tmp_path))
        monkeypatch.setenv("SWRL_ENABLED", "TRUE")
        result = json.loads(run_swrl_inference(swrl_dir=str(tmp_path)))
        assert result["success"] is True
        # skipped=False (env ok) but rules_loaded=0
        assert result.get("skipped") is not True
        assert result.get("rules_loaded") == 0


# ── _load_swrl_rules 파서 ────────────────────────────


class TestLoadSwrlRules:
    """_load_swrl_rules: .swrl 텍스트 파일 파싱."""

    def test_empty_dir_returns_zero(self):
        from tools.swrl_inference import _load_swrl_rules

        rules = _load_swrl_rules("/nonexistent/path/to/swrl")
        assert rules == []

    def test_nonexistent_dir_returns_zero(self, tmp_path):
        from tools.swrl_inference import _load_swrl_rules

        rules = _load_swrl_rules(str(tmp_path / "not_there"))
        assert rules == []

    def test_empty_dir_exists_but_no_files(self, tmp_path):
        from tools.swrl_inference import _load_swrl_rules

        rules = _load_swrl_rules(str(tmp_path))
        assert rules == []

    def test_single_rule_with_label(self, tmp_path):
        from tools.swrl_inference import _load_swrl_rules

        (tmp_path / "r1.swrl").write_text(
            "rule_a: steel:Equipment(?e) -> steel:PotentialFailure(?e)\n"
        )
        rules = _load_swrl_rules(str(tmp_path))
        assert len(rules) == 1
        assert rules[0]["label"] == "rule_a"
        assert "Equipment(?e)" in rules[0]["dl"]

    def test_single_rule_no_label_uses_filename(self, tmp_path):
        from tools.swrl_inference import _load_swrl_rules

        (tmp_path / "process_chain.swrl").write_text(
            "steel:followedBy(?a, ?b) ^ steel:followedBy(?b, ?c) -> steel:chainedTo(?a, ?c)\n"
        )
        rules = _load_swrl_rules(str(tmp_path))
        assert len(rules) == 1
        # label fallback contains filename
        assert "process_chain" in rules[0]["label"]

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        from tools.swrl_inference import _load_swrl_rules

        (tmp_path / "r.swrl").write_text(
            "# comment line\n"
            "\n"
            "   # indented comment\n"
            "rule_x: steel:A(?x) -> steel:B(?x)\n"
            "\n"
        )
        rules = _load_swrl_rules(str(tmp_path))
        assert len(rules) == 1
        assert rules[0]["label"] == "rule_x"

    def test_multiple_files_sorted(self, tmp_path):
        from tools.swrl_inference import _load_swrl_rules

        (tmp_path / "b.swrl").write_text("rb: steel:X(?x) -> steel:Y(?x)\n")
        (tmp_path / "a.swrl").write_text("ra: steel:P(?x) -> steel:Q(?x)\n")
        rules = _load_swrl_rules(str(tmp_path))
        assert len(rules) == 2
        labels = [r["label"] for r in rules]
        # sorted by filename → a.swrl first
        assert labels == ["ra", "rb"]

    def test_multiple_rules_per_file(self, tmp_path):
        from tools.swrl_inference import _load_swrl_rules

        (tmp_path / "r.swrl").write_text(
            "r1: steel:A(?x) -> steel:B(?x)\n"
            "r2: steel:C(?x) -> steel:D(?x)\n"
        )
        rules = _load_swrl_rules(str(tmp_path))
        assert len(rules) == 2
        assert {r["label"] for r in rules} == {"r1", "r2"}

    def test_invalid_line_skipped_gracefully(self, tmp_path):
        """Rule line without '->' 는 스킵되고 다른 규칙은 로드."""
        from tools.swrl_inference import _load_swrl_rules

        (tmp_path / "r.swrl").write_text(
            "bad_line_without_arrow\n"
            "good: steel:A(?x) -> steel:B(?x)\n"
        )
        rules = _load_swrl_rules(str(tmp_path))
        # only "good" parses
        assert len(rules) == 1
        assert rules[0]["label"] == "good"


# ── prefix normalization (owlready2 set_as_rule 호환) ──


class TestNormalizeDlForOwlready:
    """_normalize_dl_for_owlready: 도메인 prefix 를 local name 으로 축약."""

    def test_strips_domain_prefix(self):
        from tools.swrl_inference import _normalize_dl_for_owlready

        prefixes = {"steel": "http://example.com/steel-ontology#"}
        dl = "steel:Equipment(?e) ^ steel:hasAlarm(?e, ?a) -> steel:Failure(?e)"
        out = _normalize_dl_for_owlready(dl, prefixes)
        assert out == "Equipment(?e) ^ hasAlarm(?e, ?a) -> Failure(?e)"

    def test_leaves_variables_intact(self):
        from tools.swrl_inference import _normalize_dl_for_owlready

        prefixes = {"steel": "http://example.com/steel-ontology#"}
        dl = "steel:X(?x) ^ greaterThan(?x, 0.08) -> steel:Y(?x)"
        out = _normalize_dl_for_owlready(dl, prefixes)
        assert "?x" in out and "greaterThan" in out

    def test_multiple_prefixes_ignored_if_not_matched(self):
        from tools.swrl_inference import _normalize_dl_for_owlready

        prefixes = {"steel": "http://example.com/steel-ontology#"}
        dl = "foo:A(?x) -> steel:B(?x)"
        out = _normalize_dl_for_owlready(dl, prefixes)
        # foo: 는 건드리지 않음, steel: 만 벗김
        assert "foo:A" in out
        assert "B(?x)" in out and "steel:B" not in out


# ── 파이프라인 통합: pipeline_state 에 S8_5_SWRL 등록 ──


class TestPipelineStageRegistered:
    """S8_5_SWRL 단계가 _STEP_DEPS 에 등록되어 있는지."""

    def test_s8_5_swrl_in_step_deps(self):
        from tools.pipeline_state import _STEP_DEPS

        assert "S8_5_SWRL" in _STEP_DEPS
        deps = _STEP_DEPS["S8_5_SWRL"]
        assert "inferred_mtime" in deps
        assert "swrl_mtime" in deps

    def test_swrl_mtime_in_current_input_state(self, tmp_path, monkeypatch):
        """_current_input_state 가 swrl_mtime 을 포함하는지."""
        # Import lazily to avoid module-level side effect
        from tools import pipeline_state

        state = pipeline_state._current_input_state()
        assert "swrl_mtime" in state


# ── Pellet 실제 호출 (옵션: @slow) ──────────────────


@pytest.mark.slow
@pytest.mark.requires_java
@pytest.mark.skipif(not _OWLREADY2_AVAILABLE, reason="owlready2 필요")
class TestPelletIntegration:
    """Pellet 실제 호출 — Java 25+ + owlready2 필요."""

    def test_quality_violation_rule_fires(self, monkeypatch, tmp_path):
        """간단한 quality_violation 규칙 → 실제 new triple 생성 검증."""
        monkeypatch.setenv("SWRL_ENABLED", "true")

        from rdflib import Graph

        from tools.swrl_inference import _run_pellet_on_graph

        base_ttl = """
@prefix : <http://test.org/onto#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
<http://test.org/onto> a owl:Ontology .
:Sample a owl:Class .
:QualityViolation a owl:Class .
:carbonContent a owl:DatatypeProperty ; rdfs:range xsd:decimal .
:s1 a :Sample , owl:NamedIndividual ; :carbonContent "0.09"^^xsd:decimal .
:s2 a :Sample , owl:NamedIndividual ; :carbonContent "0.05"^^xsd:decimal .
"""
        g = Graph()
        g.parse(data=base_ttl, format="turtle")
        rules = [{
            "label": "q_test",
            "dl": "Sample(?s) ^ carbonContent(?s, ?v) ^ greaterThan(?v, 0.08) -> QualityViolation(?s)",
            "source": "test",
        }]

        new_triples, errors = _run_pellet_on_graph(g, rules)
        # s1 should get QualityViolation type
        matches = [
            (s, p, o) for s, p, o in new_triples
            if "s1" in str(s) and "QualityViolation" in str(o)
        ]
        assert len(matches) >= 1, f"Expected QualityViolation on s1, got new triples: {new_triples[:5]}"


class TestFilterDomainTriples:
    """_filter_domain_triples — startswith 정확도."""

    def test_prefix_match_excludes_similar_prefix(self):
        """substring 이 아닌 startswith — 유사 IRI 오탐 방지."""
        from rdflib import URIRef

        from tools.swrl_inference import _filter_domain_triples

        triples = [
            # 도메인 IRI prefix — 포함돼야
            (URIRef("http://test.org/onto#A"),
             URIRef("http://test.org/onto#hasX"),
             URIRef("http://test.org/onto#B")),
            # 유사 prefix (확장 도메인) — 제외돼야
            (URIRef("http://test.org/onto-extra#A"),
             URIRef("http://test.org/onto-extra#hasX"),
             URIRef("http://test.org/onto-extra#B")),
            # 완전 다른 네임스페이스
            (URIRef("http://example.com/other#X"),
             URIRef("http://example.com/other#Y"),
             URIRef("http://example.com/other#Z")),
        ]
        filtered = _filter_domain_triples(triples, "http://test.org/onto#")
        # startswith 는 정확히 첫 번째만 매칭
        assert len(filtered) == 1
        assert "http://test.org/onto#A" in str(filtered[0][0])


class TestBackwardCompat:
    """SWRL 비활성화 시 기존 산출물 무변경 보장."""

    def test_disabled_leaves_inferred_dir_untouched(self, monkeypatch, tmp_path):
        """SWRL_ENABLED=false → 기존 파일 mtime 변화 없음."""
        monkeypatch.setenv("SWRL_ENABLED", "false")
        import json

        from tools.swrl_inference import run_swrl_inference

        # 기존 파일 흉내
        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        all_inf = inferred_dir / "all_inferred.ttl"
        prov = inferred_dir / "inference_provenance.ttl"
        all_inf.write_text("# placeholder\n")
        prov.write_text("# placeholder\n")
        before_inf_mtime = all_inf.stat().st_mtime
        before_prov_mtime = prov.stat().st_mtime

        result = json.loads(run_swrl_inference())
        assert result.get("skipped") is True

        # 파일 건드리지 않음 — SWRL 비활성 시 early return
        assert all_inf.stat().st_mtime == before_inf_mtime
        assert prov.stat().st_mtime == before_prov_mtime
