"""tests for LPG Co-design — self-loop 제거, sparse column pruning, inverse 중복 제거."""

from __future__ import annotations

import json

import pytest
from rdflib import OWL, Namespace

from domain.tbox_utils import _new_graph
from tools.remote.neo4j import (
    _build_lpg_loss_manifest,
    _dedup_inverse_relationships,
    _filter_self_loops,
    _load_canonical_inverses,
    _prune_sparse_columns,
    _sanitize_neo4j_ident,
)

EX = Namespace("http://example.org/")


# ── _filter_self_loops ──────────────────────────────────────────────────────


class TestFilterSelfLoops:
    def test_removes_self_loop(self):
        """subject == object인 관계가 제거되어야 함."""
        rels = [("A", "B", "rel1"), ("C", "C", "rel2"), ("D", "E", "rel3")]
        filtered, removed = _filter_self_loops(rels)
        assert removed == 1
        assert len(filtered) == 2
        assert ("C", "C", "rel2") not in filtered

    def test_no_self_loops(self):
        """self-loop이 없으면 전부 유지."""
        rels = [("A", "B", "r1"), ("C", "D", "r2")]
        filtered, removed = _filter_self_loops(rels)
        assert removed == 0
        assert filtered == rels

    def test_all_self_loops(self):
        """모든 관계가 self-loop이면 빈 리스트."""
        rels = [("A", "A", "r1"), ("B", "B", "r2")]
        filtered, removed = _filter_self_loops(rels)
        assert removed == 2
        assert filtered == []

    def test_empty_input(self):
        """빈 입력."""
        filtered, removed = _filter_self_loops([])
        assert removed == 0
        assert filtered == []

    def test_preserves_order(self):
        """필터 후에도 원래 순서 유지."""
        rels = [("A", "B", "r1"), ("C", "C", "r2"), ("D", "E", "r3"), ("F", "G", "r4")]
        filtered, _ = _filter_self_loops(rels)
        assert filtered == [("A", "B", "r1"), ("D", "E", "r3"), ("F", "G", "r4")]


# ── _prune_sparse_columns ──────────────────────────────────────────────────


class TestPruneSparseColumns:
    def _make_nodes(self, n: int, sparse_key: str = "rare_prop", sparse_count: int = 0):
        """n개 노드 생성. sparse_count개만 sparse_key를 가짐."""
        nodes = {}
        for i in range(n):
            props = {"common": f"val_{i}"}
            if i < sparse_count:
                props[sparse_key] = f"sparse_{i}"
            nodes[f"node_{i}"] = {"labels": {"TestLabel"}, "properties": props}
        return nodes

    def test_prunes_below_threshold(self):
        """커버리지 < 1%인 프로퍼티가 _metadata로 이동."""
        # 200개 노드 중 1개만 rare_prop → 0.5% < 1%
        nodes = self._make_nodes(200, sparse_count=1)
        result, pruned_keys, coverage = _prune_sparse_columns(nodes, min_coverage=0.01)
        assert "rare_prop" in pruned_keys
        assert coverage["rare_prop"] == pytest.approx(1 / 200)
        # node_0만 _metadata에 rare_prop 포함
        meta = json.loads(result["node_0"]["properties"]["_metadata"])
        assert "rare_prop" in meta
        # node_1은 rare_prop 없었으므로 _metadata 없음
        assert "_metadata" not in result["node_1"]["properties"]

    def test_keeps_above_threshold(self):
        """커버리지 >= 1%인 프로퍼티는 유지."""
        # 100개 노드 중 100개 모두 common → 100% → 유지
        nodes = self._make_nodes(100)
        _, pruned_keys, _ = _prune_sparse_columns(nodes, min_coverage=0.01)
        assert "common" not in pruned_keys

    def test_empty_nodes(self):
        """빈 노드 딕셔너리."""
        result, pruned_keys, coverage = _prune_sparse_columns({})
        assert result == {}
        assert pruned_keys == []
        assert coverage == {}

    def test_none_values_not_counted(self):
        """None, '', 'N/A' 값은 커버리지 계산에서 제외."""
        nodes = {
            "n1": {"labels": set(), "properties": {"x": "val"}},
            "n2": {"labels": set(), "properties": {"x": None}},
            "n3": {"labels": set(), "properties": {"x": ""}},
            "n4": {"labels": set(), "properties": {"x": "N/A"}},
        }
        # x는 4개 노드 중 1개만 유효 → 25%
        _, pruned, _ = _prune_sparse_columns(nodes, min_coverage=0.30)
        assert "x" in pruned

    def test_metadata_json_roundtrip(self):
        """_metadata JSON이 올바르게 직렬화/역직렬화됨."""
        nodes = self._make_nodes(200, sparse_count=1)
        result, _, _ = _prune_sparse_columns(nodes, min_coverage=0.01)
        meta_str = result["node_0"]["properties"]["_metadata"]
        meta = json.loads(meta_str)
        assert meta["rare_prop"] == "sparse_0"

    def test_pruned_key_removed_from_properties(self):
        """pruning 후 원래 프로퍼티에서 키가 제거됨."""
        nodes = self._make_nodes(200, sparse_count=1)
        result, _, _ = _prune_sparse_columns(nodes, min_coverage=0.01)
        assert "rare_prop" not in result["node_0"]["properties"] or \
               result["node_0"]["properties"].get("rare_prop") is None
        # rare_prop은 properties에서 직접 접근 불가 (pop됨)
        # _metadata에서만 접근 가능
        assert "_metadata" in result["node_0"]["properties"]

    def test_custom_threshold(self):
        """커스텀 threshold 적용."""
        # 10개 노드 중 3개가 rare_prop → 30%
        nodes = self._make_nodes(10, sparse_count=3)
        # threshold 50% → rare_prop pruning
        _, pruned50, _ = _prune_sparse_columns(nodes, min_coverage=0.50)
        assert "rare_prop" in pruned50
        # threshold 20% → rare_prop 유지
        nodes2 = self._make_nodes(10, sparse_count=3)
        _, pruned20, _ = _prune_sparse_columns(nodes2, min_coverage=0.20)
        assert "rare_prop" not in pruned20


# ── _dedup_inverse_relationships ────────────────────────────────────────────


class TestDedupInverseRelationships:
    @pytest.fixture
    def tbox_with_inverse(self):
        """inverseOf 쌍이 있는 T-Box 그래프."""
        g = _new_graph()
        g.add((EX.hasChild, OWL.inverseOf, EX.isChildOf))
        return g

    def test_removes_non_canonical(self, tbox_with_inverse):
        """알파벳순으로 뒤쪽인 관계가 제거됨."""
        # hasChild < isChildOf 이므로 isChildOf가 non-canonical
        rels = [
            ("A", "B", "hasChild"),
            ("B", "A", "isChildOf"),
            ("C", "D", "otherRel"),
        ]
        deduped, stats = _dedup_inverse_relationships(rels, tbox_with_inverse)
        assert len(deduped) == 2
        rel_types = {t for _, _, t in deduped}
        assert "hasChild" in rel_types
        assert "isChildOf" not in rel_types
        assert stats["pairs_found"] == 1
        assert stats["relationships_removed"] == 1

    def test_no_inverse_pairs(self):
        """inverseOf 없으면 변경 없음."""
        g = _new_graph()
        rels = [("A", "B", "rel1"), ("C", "D", "rel2")]
        deduped, stats = _dedup_inverse_relationships(rels, g)
        assert deduped == rels
        assert stats["pairs_found"] == 0
        assert stats["relationships_removed"] == 0

    def test_multiple_inverse_pairs(self):
        """여러 inverseOf 쌍 처리."""
        g = _new_graph()
        g.add((EX.hasParent, OWL.inverseOf, EX.isParentOf))
        g.add((EX.contains, OWL.inverseOf, EX.isContainedIn))
        rels = [
            ("A", "B", "hasParent"),
            ("B", "A", "isParentOf"),
            ("X", "Y", "contains"),
            ("Y", "X", "isContainedIn"),
        ]
        deduped, stats = _dedup_inverse_relationships(rels, g)
        rel_types = {t for _, _, t in deduped}
        # hasParent < isParentOf → isParentOf 제거
        assert "hasParent" in rel_types
        assert "isParentOf" not in rel_types
        # contains < isContainedIn → isContainedIn 제거
        assert "contains" in rel_types
        assert "isContainedIn" not in rel_types
        assert stats["pairs_found"] == 2
        assert stats["relationships_removed"] == 2

    def test_empty_relationships(self, tbox_with_inverse):
        """빈 관계 리스트."""
        deduped, stats = _dedup_inverse_relationships([], tbox_with_inverse)
        assert deduped == []
        assert stats["relationships_removed"] == 0

    def test_canonical_direction_is_alphabetical(self, tbox_with_inverse):
        """canonical = min(s_name, o_name) 알파벳순."""
        # hasChild < isChildOf → hasChild가 canonical
        rels = [("B", "A", "isChildOf")]
        deduped, stats = _dedup_inverse_relationships(rels, tbox_with_inverse)
        assert len(deduped) == 0  # isChildOf는 non-canonical
        assert stats["relationships_removed"] == 1

    def test_bidirectional_inverse_declaration(self):
        """inverseOf가 양방향으로 선언되어도 동일하게 처리."""
        g = _new_graph()
        g.add((EX.hasChild, OWL.inverseOf, EX.isChildOf))
        g.add((EX.isChildOf, OWL.inverseOf, EX.hasChild))
        rels = [
            ("A", "B", "hasChild"),
            ("B", "A", "isChildOf"),
        ]
        deduped, stats = _dedup_inverse_relationships(rels, g)
        rel_types = {t for _, _, t in deduped}
        assert "hasChild" in rel_types
        assert "isChildOf" not in rel_types
        assert stats["pairs_found"] == 1


# ── _sanitize_neo4j_ident ───────────────────────────────────────────────────


class TestSanitizeNeo4jIdent:
    def test_normal_identifier_unchanged(self):
        assert _sanitize_neo4j_ident("Equipment") == "Equipment"
        assert _sanitize_neo4j_ident("has_operator") == "has_operator"
        assert _sanitize_neo4j_ident("temperature_1") == "temperature_1"

    def test_non_alnum_replaced(self):
        assert _sanitize_neo4j_ident("has-operator") == "has_operator"
        assert _sanitize_neo4j_ident("prop name") == "prop_name"
        assert _sanitize_neo4j_ident("a.b.c") == "a_b_c"

    def test_digit_start_gets_underscore_prefix(self):
        assert _sanitize_neo4j_ident("123abc") == "_123abc"
        assert _sanitize_neo4j_ident("1Prop") == "_1Prop"

    def test_cypher_reserved_gets_underscore_suffix(self):
        # "MATCH" / "RETURN" 등은 대소문자 무관 예약어
        assert _sanitize_neo4j_ident("match") == "match_"
        assert _sanitize_neo4j_ident("MATCH") == "MATCH_"
        assert _sanitize_neo4j_ident("return") == "return_"
        assert _sanitize_neo4j_ident("node") == "node_"

    def test_empty_input_returns_underscore(self):
        assert _sanitize_neo4j_ident("") == "_"

    def test_korean_characters_replaced(self):
        # 한국어 로컬 네임이 들어오면 비영숫자로 간주되어 _로 치환
        assert _sanitize_neo4j_ident("설비_id") == "___id"


# ── _load_canonical_inverses ────────────────────────────────────────────────


class TestLoadCanonicalInverses:
    def test_returns_dict(self):
        mapping = _load_canonical_inverses()
        assert isinstance(mapping, dict)


# ── _dedup_inverse_relationships with canonical override ───────────────────


class TestDedupWithOverride:
    def test_alphabetical_fallback_when_no_override(self, monkeypatch):
        """override 없으면 기존 알파벳 min() 유지."""
        monkeypatch.setattr(
            "tools.remote.neo4j._load_canonical_inverses", lambda: {}
        )
        g = _new_graph()
        g.add((EX.zebra, OWL.inverseOf, EX.apple))
        rels = [("X", "Y", "zebra"), ("Y", "X", "apple")]
        deduped, stats = _dedup_inverse_relationships(rels, g)
        rel_types = {t for _, _, t in deduped}
        # apple < zebra → apple 이 canonical, zebra 제거
        assert "apple" in rel_types
        assert "zebra" not in rel_types
        assert stats["canonical_overrides_applied"] == 0

    def test_override_beats_alphabetical(self, monkeypatch):
        """config override 가 알파벳 순서를 뒤집는다."""
        # hasOperator < operatedBy 는 알파벳순이라 기본 동작에서도 같지만,
        # override 를 명시했을 때 override_applied 카운트가 증가해야 함.
        monkeypatch.setattr(
            "tools.remote.neo4j._load_canonical_inverses",
            lambda: {"operatedBy": "hasOperator"},
        )
        g = _new_graph()
        g.add((EX.hasOperator, OWL.inverseOf, EX.operatedBy))
        rels = [("A", "B", "hasOperator"), ("B", "A", "operatedBy")]
        deduped, stats = _dedup_inverse_relationships(rels, g)
        rel_types = {t for _, _, t in deduped}
        assert "hasOperator" in rel_types
        assert "operatedBy" not in rel_types
        assert stats["canonical_overrides_applied"] == 1

    def test_override_reverses_alphabetical(self, monkeypatch):
        """SME 가 의미적 active 를 알파벳 역방향으로 지정."""
        # 알파벳순으로는 apple 이 canonical 인데, override 로 zebra 를 유지.
        monkeypatch.setattr(
            "tools.remote.neo4j._load_canonical_inverses",
            lambda: {"apple": "zebra"},
        )
        g = _new_graph()
        g.add((EX.zebra, OWL.inverseOf, EX.apple))
        rels = [("X", "Y", "zebra"), ("Y", "X", "apple")]
        deduped, stats = _dedup_inverse_relationships(rels, g)
        rel_types = {t for _, _, t in deduped}
        assert "zebra" in rel_types
        assert "apple" not in rel_types
        assert stats["canonical_overrides_applied"] == 1


# ── _build_lpg_loss_manifest with new fields ────────────────────────────────


class TestLossManifestNewFields:
    def test_sparse_pruning_in_manifest(self):
        manifest = _build_lpg_loss_manifest(
            tracker={},
            pruned_sparse_columns={"rare_prop": 0.005, "medium_prop": 0.008},
        )
        sparse = manifest["losses"].get("sparse_column_pruning")
        assert sparse is not None
        assert sparse["count"] == 2
        # 낮은 coverage 순으로 정렬
        assert sparse["columns"][0]["property"] == "rare_prop"
        assert sparse["columns"][0]["coverage_ratio"] == 0.005

    def test_ident_renames_in_manifest(self):
        manifest = _build_lpg_loss_manifest(
            tracker={},
            ident_renames={"123abc": "_123abc", "match": "match_"},
        )
        renames = manifest["losses"].get("identifier_renames")
        assert renames is not None
        assert renames["count"] == 2
        pairs = {r["from"]: r["to"] for r in renames["renames"]}
        assert pairs["123abc"] == "_123abc"
        assert pairs["match"] == "match_"

    def test_no_sparse_pruning_omitted(self):
        manifest = _build_lpg_loss_manifest(tracker={})
        assert "sparse_column_pruning" not in manifest["losses"]
        assert "identifier_renames" not in manifest["losses"]
