"""Tests for tools/tbox_generation.py — T-Box 생성 도구 테스트."""

import json
from unittest.mock import patch

from domain.namespaces import DOMAIN_NS, NS_PREFIX

# ── _extract_ttl_from_markdown ────────────────────


class TestExtractTtlFromMarkdown:
    """마크다운 코드 블록에서 TTL 추출."""

    def test_turtle_code_fence(self):
        from tools.tbox_generation import _extract_ttl_from_markdown

        content = """Some text before

```turtle
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<#Test> a owl:Class .
```

Some text after"""
        result = _extract_ttl_from_markdown(content)
        assert "@prefix owl:" in result
        assert "owl:Class" in result
        assert "Some text before" not in result

    def test_ttl_code_fence(self):
        from tools.tbox_generation import _extract_ttl_from_markdown

        content = "```ttl\n@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n```"
        result = _extract_ttl_from_markdown(content)
        assert "@prefix rdfs:" in result

    def test_generic_code_fence(self):
        from tools.tbox_generation import _extract_ttl_from_markdown

        content = "```\n@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n```"
        result = _extract_ttl_from_markdown(content)
        assert "@prefix xsd:" in result

    def test_no_code_fence_returns_original(self):
        """코드 블록 없으면 원본 반환."""
        from tools.tbox_generation import _extract_ttl_from_markdown

        content = "@prefix owl: <http://www.w3.org/2002/07/owl#> ."
        result = _extract_ttl_from_markdown(content)
        assert result == content

    def test_truncated_fence_recovered(self):
        """Bedrock 응답이 max_tokens 로 잘려 닫는 ``` 가 없어도 내용 추출."""
        from tools.tbox_generation import _extract_ttl_from_markdown

        content = (
            "Some preamble\n"
            "```turtle\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "steel:A a owl:Class .\n"
            # no closing fence — truncated
        )
        result = _extract_ttl_from_markdown(content)
        assert "@prefix owl:" in result
        assert "owl:Class" in result
        assert "Some preamble" not in result
        assert "```" not in result

    def test_empty_string(self):
        from tools.tbox_generation import _extract_ttl_from_markdown

        assert _extract_ttl_from_markdown("") == ""

    def test_prelude_without_fence(self):
        """Sonnet 4.6 회귀 — reasoning prelude 뒤에 펜스 없이 TTL 이 이어짐.

        2026-04-30 관찰: Sonnet 4.6 이 "I need to analyze..." 같은 자연어
        reasoning 을 먼저 쓰고, 코드 펜스 없이 바로 @prefix 를 이어서 생성
        하는 회귀가 발생. extractor 는 첫 directive 시점부터 반환해야 한다.
        """
        from tools.tbox_generation import _extract_ttl_from_markdown

        content = (
            "I need to analyze the disjoint_groups.json rules first to "
            "find the appropriate patterns.\n\n"
            "@prefix steel: <http://example.com/steel#> .\n"
            "steel:Equipment a owl:Class ."
        )
        result = _extract_ttl_from_markdown(content)
        assert result.startswith("@prefix steel:")
        assert "I need to analyze" not in result

    def test_prelude_with_base_directive(self):
        from tools.tbox_generation import _extract_ttl_from_markdown

        content = (
            "Let me think about this step by step.\n\n"
            "@base <http://example.com/> .\n"
            "<C> a owl:Class ."
        )
        result = _extract_ttl_from_markdown(content)
        assert result.startswith("@base")
        assert "Let me think" not in result


# ── _table_name_to_class ──────────────────────────


class TestTableNameToClass:
    """CSV 파일명 → PascalCase 변환."""

    def test_underscore_separated(self):
        from tools.tbox_generation import _table_name_to_class

        assert _table_name_to_class("Equipment_Master") == "EquipmentMaster"

    def test_already_pascal(self):
        from tools.tbox_generation import _table_name_to_class

        assert _table_name_to_class("EquipmentMaster") == "Equipmentmaster"

    def test_dash_separated(self):
        from tools.tbox_generation import _table_name_to_class

        assert _table_name_to_class("tag-master") == "TagMaster"

    def test_single_word(self):
        from tools.tbox_generation import _table_name_to_class

        assert _table_name_to_class("alarm") == "Alarm"

    def test_multiple_underscores(self):
        from tools.tbox_generation import _table_name_to_class

        assert _table_name_to_class("a_b_c_d") == "ABCD"


# ── _adaptive_chunking ───────────────────────────


class TestAdaptiveChunking:
    """적응형 청킹 로직."""

    def test_single_chunk_for_small_tables(self):
        from tools.tbox_generation import _adaptive_chunking

        tables = [{"name": "t1", "columns": ["a", "b"]}]
        config = {
            "max_tokens": 32000,
            "token_budget_ratio": 0.5,
            "per_column_tokens": 100,
            "per_table_tokens": 500,
        }
        chunks = _adaptive_chunking(tables, config)
        assert len(chunks) == 1
        assert len(chunks[0]) == 1

    def test_multiple_chunks_for_large_tables(self):
        """토큰 예산 초과 시 여러 청크로 분할."""
        from tools.tbox_generation import _adaptive_chunking

        # 각 테이블이 많은 토큰 소비
        tables = [
            {"name": f"t{i}", "columns": [f"col_{j}" for j in range(50)]}
            for i in range(10)
        ]
        config = {
            "max_tokens": 32000,
            "token_budget_ratio": 0.3,   # budget = 9600
            "per_column_tokens": 100,    # 50 cols * 100 = 5000 per table
            "per_table_tokens": 500,     # total ~5500 per table
        }
        chunks = _adaptive_chunking(tables, config)
        assert len(chunks) > 1
        # 모든 테이블이 포함되는지 확인
        total_tables = sum(len(c) for c in chunks)
        assert total_tables == 10

    def test_empty_tables(self):
        from tools.tbox_generation import _adaptive_chunking

        config = {
            "max_tokens": 32000,
            "token_budget_ratio": 0.5,
            "per_column_tokens": 100,
            "per_table_tokens": 500,
        }
        chunks = _adaptive_chunking([], config)
        assert chunks == []


# ── _merge_chunks ─────────────────────────────────


class TestMergeChunks:
    """청크 병합 테스트."""

    def test_single_chunk(self, sample_tbox_ttl):
        from tools.tbox_generation import _merge_chunks

        merged, stats = _merge_chunks([sample_tbox_ttl])
        assert merged == sample_tbox_ttl
        assert stats["chunks_merged"] == 1
        assert stats["redefinition_count"] == 0

    def test_two_chunks_merged(self, sample_tbox_ttl):
        """두 청크 병합 시 prefix 제거 확인."""
        from tools.tbox_generation import _merge_chunks

        chunk2 = f"""{NS_PREFIX}:NewClass a owl:Class ;
    rdfs:label "New"@en .
"""
        merged, stats = _merge_chunks([sample_tbox_ttl, chunk2])
        assert stats["chunks_merged"] == 2
        # prefix 줄은 제거됨 (두 번째 청크의 @prefix 없음)
        assert "NewClass" in merged

    def test_empty_list(self):
        from tools.tbox_generation import _merge_chunks

        merged, stats = _merge_chunks([])
        assert merged == ""
        assert stats["chunks_merged"] == 0

    def test_empty_first_chunk_preserves_prefix(self, sample_tbox_ttl):
        """2026-05-12 bug fix: chunks[0] 가 빈 문자열이어도 이후 청크 prefix 보존.

        과거엔 merged = "" 로 시작해 chunks[1:] 의 prefix 가 모두 제거되어
        최종 TTL 에 @prefix 선언 0개 → rdflib 파싱 실패 ("steel: Prefix not bound").
        수정 후: 비어있지 않은 첫 청크를 seed 로, 또는 prefix_preamble 로 대체.
        """
        from tools.tbox_generation import _merge_chunks

        # chunks[0] 는 빈 문자열 (depth>=3 overflow skip 모사)
        merged, stats = _merge_chunks(["", sample_tbox_ttl])
        assert stats["chunks_merged"] == 2
        assert "@prefix" in merged
        # seed = sample_tbox_ttl 자체 (비어 있지 않은 첫 청크)
        assert "EquipmentMaster" in merged or "@prefix" in merged

    def test_all_chunks_empty_uses_preamble(self):
        """모든 청크가 빈 문자열이면 최소 prefix preamble 로 seed."""
        from domain.namespaces import NS_PREFIX
        from tools.tbox_generation import _merge_chunks

        merged, stats = _merge_chunks(["", "", ""])
        assert stats["chunks_merged"] == 3
        # preamble 에 포함된 기본 prefix 선언 확인
        assert "@prefix owl:" in merged
        assert f"@prefix {NS_PREFIX}:" in merged

    def test_seed_without_prefix_gets_preamble_prepended(self):
        """seed chunk 에 @prefix 없으면 preamble prepend (방어선).

        LLM 이 @prefix 선언 없이 클래스 선언만 반환한 edge case 보호.
        """
        from tools.tbox_generation import _merge_chunks

        no_prefix_chunk = "steel:FooClass a owl:Class .\n"
        merged, stats = _merge_chunks([no_prefix_chunk])
        assert "@prefix owl:" in merged
        assert "FooClass" in merged


# ── _coverage_report ──────────────────────────────


class TestCoverageReport:
    """T-Box 커버리지 리포트."""

    def test_full_coverage(self, sample_tbox_ttl):
        from tools.tbox_generation import _coverage_report

        tables = [
            {"name": "Equipment_Master", "columns": ["equipmentID", "status"]},
            {"name": "Equipment_Status", "columns": ["status"]},
        ]
        report = _coverage_report(sample_tbox_ttl, tables)
        assert report["tables_total"] == 2
        assert "coverage_percent" in report

    def test_missing_table(self, sample_tbox_ttl):
        from tools.tbox_generation import _coverage_report

        tables = [
            {"name": "Equipment_Master", "columns": ["equipmentID"]},
            {"name": "NonExistent_Table", "columns": ["col1"]},
        ]
        report = _coverage_report(sample_tbox_ttl, tables)
        assert "NonExistent_Table" in report["tables_missing"]

    def test_empty_tables(self, sample_tbox_ttl):
        from tools.tbox_generation import _coverage_report

        report = _coverage_report(sample_tbox_ttl, [])
        assert report["tables_total"] == 0
        assert report["coverage_percent"] == 0


# ── _deterministic_fix ────────────────────────────


class TestDeterministicFix:
    """결정론적 SHACL 위반 수정."""

    def test_add_missing_label(self, sample_tbox_missing_labels):
        from tools.tbox_generation import _deterministic_fix

        violations = [{
            "focusNode": f"{DOMAIN_NS}EquipmentMaster",
            "message": "label @en missing",
        }]
        fixed_ttl, fixes = _deterministic_fix(sample_tbox_missing_labels, violations)
        assert len(fixes) >= 1
        assert any("label" in f for f in fixes)

    def test_add_missing_domain(self, sample_tbox_missing_domain_range):
        from tools.tbox_generation import _deterministic_fix

        violations = [{
            "focusNode": f"{DOMAIN_NS}hasEquipmentStatus",
            "message": "domain missing",
        }]
        fixed_ttl, fixes = _deterministic_fix(sample_tbox_missing_domain_range, violations)
        assert len(fixes) >= 1
        assert any("domain" in f for f in fixes)

    def test_empty_violations(self, sample_tbox_ttl):
        from tools.tbox_generation import _deterministic_fix

        fixed_ttl, fixes = _deterministic_fix(sample_tbox_ttl, [])
        assert fixes == []


# ── generate_tbox MCP 도구 ────────────────────────


class TestGenerateTbox:
    """generate_tbox() 통합 테스트."""

    @patch("tools.tbox_generation._load_source_data")
    @patch("tools.tbox_generation._load_config")
    @patch("tools.tbox_generation._invoke_bedrock")
    @patch("tools.tbox_generation._run_correction_loop")
    @patch("tools.tbox_generation.atomic_write")
    @patch("os.path.exists", return_value=False)
    def test_happy_path(
        self, mock_exists, mock_write, mock_correction, mock_bedrock,
        mock_config, mock_source, sample_tbox_ttl,
    ):
        from tools.tbox_generation import generate_tbox

        mock_config.return_value = {
            "max_tokens": 32000,
            "token_budget_ratio": 0.5,
            "per_column_tokens": 100,
            "per_table_tokens": 500,
            "max_retries": 1,
            "max_correction_rounds": 1,
        }
        mock_source.return_value = (
            {"tables": {"Equipment_Master": {"columns": ["equipmentID"]}}},
            "(IOF summary)",
            "",
        )
        mock_bedrock.return_value = {
            "text": sample_tbox_ttl,
            "stop_reason": "end_turn",
        }
        mock_correction.return_value = (sample_tbox_ttl, {
            "shacl_violations_initial": 0,
            "corrections": [],
            "remaining_issues": 0,
        })

        result = json.loads(generate_tbox("Equipment_Master"))
        assert result["success"] is True
        assert "statistics" in result
        assert result["statistics"]["classes"] >= 1

    @patch("tools.tbox_generation._load_source_data")
    @patch("tools.tbox_generation._load_config")
    def test_no_csv_files(self, mock_config, mock_source):
        """CSV 없으면 에러."""
        from tools.tbox_generation import generate_tbox

        mock_config.return_value = {
            "max_tokens": 32000,
            "token_budget_ratio": 0.5,
            "per_column_tokens": 100,
            "per_table_tokens": 500,
            "max_retries": 1,
        }
        mock_source.return_value = ({"tables": {}}, "", "")

        result = json.loads(generate_tbox())
        assert "error" in result


class TestCorrectionLoopEarlyExit:
    """R28 — _run_correction_loop 조기 종료 경로 검증.

    이전엔 max_correction_rounds=2 만 보고 루프를 끝까지 돌려 80분 실행에서
    10~15분을 낭비했다. 세 가지 조기 종료 조건을 검증:
    1. _llm_correct 가 동일 TTL 반환 → 즉시 break (no-op 감지)
    2. critical 이슈 수가 감소 안 함 → stall 로 판정하고 break
    3. critical 이슈 0 → 통상 종료
    """

    _TTL_WITH_CRITICAL = (
        f"@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        f"@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        f"@prefix steel: <{DOMAIN_NS}> .\n"
        "steel:A a owl:Class .\n"
    )

    def test_breaks_on_no_op_correction(self):
        """_llm_correct 가 동일 TTL 반환 시 즉시 break — 루프 1회로 끝."""
        from tools.tbox_generation import _run_correction_loop

        critical_issue = {"severity": "critical", "message": "missing range"}
        # check_quality_rules: 매번 critical 1건 반환 (진척 없음)
        def _cq_rules(_ttl):
            return json.dumps({"success": True, "issues_count": 1,
                               "issues": [critical_issue]})
        llm_calls = {"n": 0}
        def _llm(_ttl, _violations, _config):
            llm_calls["n"] += 1
            return _ttl  # 원본 그대로 반환 = no-op

        with patch("tools.validation.check_quality_rules", _cq_rules), \
             patch("tools.tbox_generation._llm_correct", _llm):
            _ttl, report = _run_correction_loop(
                self._TTL_WITH_CRITICAL,
                {"max_correction_rounds": 5},
            )
        # _llm_correct 은 정확히 1번만 호출됨 (no-op 감지 후 break)
        assert llm_calls["n"] == 1
        assert any("no-op" in c for c in report["corrections"])

    def test_breaks_on_stall(self):
        """critical 수가 감소 안 하면 LLM 호출 전에 stall 판정 → 2번째 라운드 skip."""
        from tools.tbox_generation import _run_correction_loop

        # 매번 critical 2건 반환 (개수 고정).
        critical = {"severity": "critical", "message": "missing range"}
        def _cq_rules(_ttl):
            return json.dumps({"success": True, "issues_count": 2,
                               "issues": [critical, critical]})
        llm_calls = {"n": 0}
        def _llm(_ttl, _violations, _config):
            llm_calls["n"] += 1
            # 임의의 변화로 no-op 조기종료를 회피하고 stall 경로를 타도록.
            return _ttl + "\n# changed\n"

        with patch("tools.validation.check_quality_rules", _cq_rules), \
             patch("tools.tbox_generation._llm_correct", _llm):
            _ttl, report = _run_correction_loop(
                self._TTL_WITH_CRITICAL,
                {"max_correction_rounds": 5},
            )
        # Round 1: critical=2 → LLM 호출. Round 2: critical=2 여전히 ≥ prev=2 → stall break.
        assert llm_calls["n"] == 1
        assert any("stall" in c for c in report["corrections"])

    def test_completes_on_zero_critical(self):
        """critical 이 0 이면 즉시 종료."""
        from tools.tbox_generation import _run_correction_loop

        def _cq_rules(_ttl):
            return json.dumps({"success": True, "issues_count": 0, "issues": []})
        with patch("tools.validation.check_quality_rules", _cq_rules):
            _ttl, report = _run_correction_loop(
                self._TTL_WITH_CRITICAL,
                {"max_correction_rounds": 5},
            )
        # LLM 호출 없이 종료 (corrections 는 빈 리스트 유지)
        assert report["shacl_violations_initial"] == 0


class TestGenerateChunkDeepOverflow:
    """2026-05-10 회귀 방어 — depth>=2 단일 테이블 overflow 시 재시도 생략."""

    _CONFIG = {
        "max_tokens": 24000,
        "max_retries": 2,
        "per_column_tokens": 140,
        "per_table_tokens": 800,
        "token_budget_ratio": 0.35,
    }

    def _fake_overflow(self, *_args, **_kwargs):
        # Bedrock overflow 모사: text + stop_reason=max_tokens
        return {"text": "@prefix : <x#> .\n", "stop_reason": "max_tokens"}

    def test_depth3_single_table_overflow_skips_retry(self):
        """depth>=3 + 단일 테이블 overflow → 재시도 없이 빈 TTL 반환.

        2026-05-12 정책 변경: depth 2 까지는 retry 허용, depth 3 이상만 skip.
        """
        from tools.tbox_generation import _generate_chunk

        call_count = {"n": 0}
        def _invoke(*args, **kwargs):
            call_count["n"] += 1
            return self._fake_overflow()

        tables = [{"name": "BigTable", "columns": ["c1", "c2", "c3"]}]
        with patch("tools.tbox_generation._invoke_bedrock", side_effect=_invoke):
            result = _generate_chunk(
                chunk_idx=0,
                chunk_tables=tables,
                total_chunks=6,
                iof_summary="",
                schema_info="",
                relationships_info="",
                table_class_map={"BigTable": "BigTable"},
                config=self._CONFIG,
                depth=3,
            )
        assert result == ""
        # depth>=3 에서는 첫 Bedrock 호출 1번만 — retry 생략
        assert call_count["n"] == 1

    def test_depth2_single_table_overflow_retries_once(self):
        """depth==2 + 단일 테이블 overflow → max_tokens ×2 재시도 1회 허용.

        2026-05-12 신규 정책: depth 2 에서도 overflow 시 한 번 retry 기회 제공.
        _merge_chunks prefix 보존 버그 fix 와 함께 적용.
        """
        from tools.tbox_generation import _generate_chunk

        call_count = {"n": 0}
        def _invoke(*args, **kwargs):
            call_count["n"] += 1
            return self._fake_overflow()

        tables = [{"name": "BigTable", "columns": ["c1", "c2", "c3"]}]
        with patch("tools.tbox_generation._invoke_bedrock", side_effect=_invoke):
            result = _generate_chunk(
                chunk_idx=0,
                chunk_tables=tables,
                total_chunks=6,
                iof_summary="",
                schema_info="",
                relationships_info="",
                table_class_map={"BigTable": "BigTable"},
                config=self._CONFIG,
                depth=2,
            )
        assert result == ""
        # depth=2: 1차 overflow + retry 1회 → 총 2회
        assert call_count["n"] == 2

    def test_shallow_single_table_overflow_retries_with_larger_budget(self):
        """depth<2 + 단일 테이블 overflow → 기존대로 _retry_max 재시도."""
        from tools.tbox_generation import _generate_chunk

        call_count = {"n": 0}
        def _invoke(*args, **kwargs):
            call_count["n"] += 1
            return self._fake_overflow()

        tables = [{"name": "BigTable", "columns": ["c1", "c2", "c3"]}]
        with patch("tools.tbox_generation._invoke_bedrock", side_effect=_invoke):
            result = _generate_chunk(
                chunk_idx=0,
                chunk_tables=tables,
                total_chunks=6,
                iof_summary="",
                schema_info="",
                relationships_info="",
                table_class_map={"BigTable": "BigTable"},
                config=self._CONFIG,
                depth=0,
            )
        assert result == ""
        # depth=0: 1차 호출 + _retry_max 재시도 1번 → 총 2회
        assert call_count["n"] == 2
