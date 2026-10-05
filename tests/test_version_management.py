"""Tests for tools/version_management.py — #6 T-Box 버전 관리."""
from __future__ import annotations

import json
from unittest.mock import patch

from domain.namespaces import DOMAIN_NS, ONTOLOGY_URI
from tools.version_management import (
    _append_changelog,
    _bump_patch,
    _compute_commit_hash,
    _diff_tbox,
    apply_version_metadata,
    bump_tbox_version,
)

TTL_OLD = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY_URI}> a owl:Ontology ; owl:versionInfo "1.0.0" .
steel:A a owl:Class .
steel:B a owl:Class .
steel:hasX a owl:ObjectProperty .
"""


TTL_NEW = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
<{ONTOLOGY_URI}> a owl:Ontology ; owl:versionInfo "1.0.0" .
steel:A a owl:Class .
steel:C a owl:Class .
steel:hasY a owl:ObjectProperty .
"""


class TestBumpPatch:
    def test_patch_increment(self):
        assert _bump_patch("1.2.3") == "1.2.4"

    def test_non_semver_untouched(self):
        assert _bump_patch("1.0") == "1.0"
        assert _bump_patch("abc") == "abc"


class TestCommitHash:
    def test_whitespace_insensitive(self):
        assert _compute_commit_hash("a\nb") == _compute_commit_hash("a \n  b  ")

    def test_different_content_different_hash(self):
        assert _compute_commit_hash("a") != _compute_commit_hash("b")


class TestDiffTbox:
    def test_add_remove_detected(self):
        diff = _diff_tbox(TTL_OLD, TTL_NEW)
        assert f"{DOMAIN_NS}C" in diff["added_classes"]
        assert f"{DOMAIN_NS}B" in diff["removed_classes"]
        assert f"{DOMAIN_NS}hasY" in diff["added_ops"]
        assert f"{DOMAIN_NS}hasX" in diff["removed_ops"]

    def test_empty_old(self):
        diff = _diff_tbox("", TTL_NEW)
        assert len(diff["added_classes"]) == 2
        assert diff["removed_classes"] == []


class TestApplyVersionMetadata:
    def test_patch_bump(self):
        new_ttl, meta = apply_version_metadata(
            TTL_NEW, old_ttl=TTL_OLD, strategy="patch",
        )
        assert meta["prior_version"] == "1.0.0"
        assert meta["new_version"] == "1.0.1"
        assert "priorVersion" in new_ttl
        assert meta["commit_hash"]

    def test_keep_strategy(self):
        new_ttl, meta = apply_version_metadata(
            TTL_NEW, old_ttl=TTL_OLD, strategy="keep",
        )
        assert meta["new_version"] == "1.0.0"

    def test_deprecated_mark(self):
        new_ttl, meta = apply_version_metadata(
            TTL_NEW, old_ttl=TTL_OLD, strategy="patch",
            mark_deprecated=True,
        )
        assert meta["deprecated_marks"] >= 1
        assert "deprecated" in new_ttl

    def test_no_prior_version(self):
        _nt, meta = apply_version_metadata(
            TTL_NEW, old_ttl="", strategy="patch",
        )
        assert meta["prior_version"] == ""


class TestProvenanceMetadata:
    """Extended FAIR-compliant provenance annotations on the ontology node."""

    def test_issued_and_modified_timestamps(self):
        new_ttl, _ = apply_version_metadata(
            TTL_NEW, old_ttl="", strategy="patch",
        )
        assert "dcterms:modified" in new_ttl or "dct:modified" in new_ttl
        assert "dcterms:issued" in new_ttl or "dct:issued" in new_ttl

    def test_modified_updates_on_bump_issued_preserved(self):
        # First bump from scratch: both issued and modified are set.
        first_ttl, _ = apply_version_metadata(
            TTL_NEW, old_ttl="", strategy="patch",
        )
        # Extract the issued timestamp so we can verify it survives a re-bump.
        from rdflib import URIRef as _U

        from domain.namespaces import ONTOLOGY_URI as _ONT
        from tools.version_management import DCTERMS as _DC
        from tools.version_management import _new_graph
        g1 = _new_graph()
        g1.parse(data=first_ttl, format="turtle")
        issued_before = list(g1.objects(_U(_ONT), _DC.issued))
        assert issued_before, "first bump should set dcterms:issued"

        # Second bump: issued must not change, modified may change.
        second_ttl, _ = apply_version_metadata(
            first_ttl, old_ttl=TTL_OLD, strategy="patch",
        )
        g2 = _new_graph()
        g2.parse(data=second_ttl, format="turtle")
        issued_after = list(g2.objects(_U(_ONT), _DC.issued))
        assert str(issued_after[0]) == str(issued_before[0]), (
            "dcterms:issued must be preserved across bumps (first-published date)"
        )

    def test_prov_was_derived_from_on_bump(self):
        new_ttl, _ = apply_version_metadata(
            TTL_NEW, old_ttl=TTL_OLD, strategy="patch",
        )
        # Must record lineage: this version was derived from the prior versionIRI.
        assert "wasDerivedFrom" in new_ttl
        assert "1.0.0" in new_ttl  # prior versionIRI segment

    def test_no_prov_when_no_prior(self):
        new_ttl, _ = apply_version_metadata(
            TTL_NEW, old_ttl="", strategy="patch",
        )
        # No prior → no wasDerivedFrom (can't derive from nothing).
        assert "wasDerivedFrom" not in new_ttl


class TestAppendChangelog:
    def test_creates_file(self, tmp_path):
        meta = {"commit_hash": "abc123",
                "prior_version": "1.0.0", "new_version": "1.0.1",
                "diff": {"added_classes": ["x"], "removed_classes": [],
                         "added_ops": [], "removed_ops": [],
                         "added_dps": [], "removed_dps": []}}
        path = tmp_path / "CHANGELOG.ttl"
        result = _append_changelog(meta, changelog_path=str(path))
        assert result == str(path)
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "commit_abc123" in content

    def test_dedup_by_commit(self, tmp_path):
        meta = {"commit_hash": "same", "prior_version": "a", "new_version": "b",
                "diff": {}}
        path = tmp_path / "CHANGELOG.ttl"
        _append_changelog(meta, changelog_path=str(path))
        size1 = path.stat().st_size
        _append_changelog(meta, changelog_path=str(path))
        size2 = path.stat().st_size
        assert size1 == size2


class TestMCPTool:
    def test_missing_tbox(self, tmp_path):
        with patch("tools.version_management.TBOX_PATH",
                   str(tmp_path / "nonexistent.ttl")):
            result = json.loads(bump_tbox_version())
            assert result["success"] is False

    def test_invalid_strategy(self, tmp_path):
        tbox = tmp_path / "tbox.ttl"
        tbox.write_text(TTL_NEW, encoding="utf-8")
        with (
            patch("tools.version_management.TBOX_PATH", str(tbox)),
            patch(
                "tools.version_management.TBOX_BASELINE_PATH",
                str(tmp_path / "baseline.ttl"),
            ),
        ):
            result = json.loads(bump_tbox_version(strategy="invalid"))
            assert result["success"] is False

    def test_full_flow(self, tmp_path):
        tbox = tmp_path / "tbox.ttl"
        baseline = tmp_path / "baseline.ttl"
        baseline.write_text(TTL_OLD, encoding="utf-8")
        tbox.write_text(TTL_NEW, encoding="utf-8")
        changelog = tmp_path / "CHANGELOG.ttl"
        with (
            patch("tools.version_management.TBOX_PATH", str(tbox)),
            patch("tools.version_management.TBOX_BASELINE_PATH", str(baseline)),
            patch(
                "tools.version_management._append_changelog",
                return_value=str(changelog),
            ),
        ):
            result = json.loads(bump_tbox_version(strategy="patch"))
            assert result["success"] is True
            assert result["new_version"] == "1.0.1"
            assert result["prior_version"] == "1.0.0"
