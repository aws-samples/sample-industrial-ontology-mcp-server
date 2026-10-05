"""Tests for tools/local_artifacts.py — 로컬 아티팩트 read + CSV 도구 테스트."""

import json
from unittest.mock import patch

import pytest

# ── 읽기 도구 테스트 ──────────────────────────────


class TestReadTbox:
    def test_happy_path(self, tmp_path):
        ttl = "@prefix steel: <http://example.org/test-ontology#> ."
        f = tmp_path / "t_box.ttl"
        f.write_text(ttl, encoding="utf-8")

        with patch("tools.local_artifacts.TBOX_PATH", str(f)):
            from tools.local_artifacts import read_tbox
            result = read_tbox()
        assert "@prefix steel:" in result

    def test_file_not_found(self, tmp_path):
        with patch("tools.local_artifacts.TBOX_PATH", str(tmp_path / "nonexistent.ttl")):
            from tools.local_artifacts import read_tbox
            result = read_tbox()
        data = json.loads(result)
        assert "error" in data


class TestReadAbox:
    def test_happy_path(self, tmp_path):
        ttl = "@prefix steel-inst: <http://example.org/test-ontology/instances#> ."
        f = tmp_path / "a_box.ttl"
        f.write_text(ttl, encoding="utf-8")

        with patch("tools.local_artifacts.ABOX_PATH", str(f)):
            from tools.local_artifacts import read_abox
            result = read_abox()
        assert "steel-inst" in result

    def test_file_not_found(self, tmp_path):
        with patch("tools.local_artifacts.ABOX_PATH", str(tmp_path / "nonexistent.ttl")):
            from tools.local_artifacts import read_abox
            result = read_abox()
        data = json.loads(result)
        assert "error" in data


class TestReadInferred:
    def test_happy_path(self, tmp_path):
        ttl = "@prefix steel: <http://example.org/test-ontology#> ."
        f = tmp_path / "all_inferred.ttl"
        f.write_text(ttl, encoding="utf-8")

        with patch("tools.local_artifacts.INFERRED_PATH", str(f)):
            from tools.local_artifacts import read_inferred
            result = read_inferred()
        assert "steel" in result

    def test_file_not_found(self, tmp_path):
        with patch("tools.local_artifacts.INFERRED_PATH", str(tmp_path / "nonexistent.ttl")):
            from tools.local_artifacts import read_inferred
            result = read_inferred()
        data = json.loads(result)
        assert "error" in data


class TestReadIofMapping:
    def test_happy_path(self, tmp_path):
        mapping = '{"mappings": []}'
        f = tmp_path / "iof_masterdata_mapping.json"
        f.write_text(mapping, encoding="utf-8")

        with patch("tools.local_artifacts.SOURCE_MAPPING_DIR", str(tmp_path)):
            from tools.local_artifacts import read_iof_mapping
            result = read_iof_mapping()
        assert "mappings" in result

    def test_file_not_found(self, tmp_path):
        with patch("tools.local_artifacts.SOURCE_MAPPING_DIR", str(tmp_path / "nonexistent")):
            from tools.local_artifacts import read_iof_mapping
            result = read_iof_mapping()
        data = json.loads(result)
        assert "error" in data


# ── CSV 도구 테스트 ──────────────────────────────


class TestListCsvTables:
    def test_happy_path(self, tmp_path):
        (tmp_path / "Equipment_Master.csv").write_text("col1,col2\na,b\n", encoding="utf-8")
        (tmp_path / "Tag_Master.csv").write_text("col1\nx\n", encoding="utf-8")

        with patch("tools.local_artifacts.SOURCE_RAWDATA_DIR", str(tmp_path)):
            from tools.local_artifacts import list_csv_tables
            result = json.loads(list_csv_tables())
        assert len(result) == 2
        names = [f["name"] for f in result]
        assert "Equipment_Master.csv" in names

    def test_no_csv(self, tmp_path):
        with patch("tools.local_artifacts.SOURCE_RAWDATA_DIR", str(tmp_path)):
            from tools.local_artifacts import list_csv_tables
            result = list_csv_tables()
        data = json.loads(result)
        assert "error" in data


class TestReadCsvSchema:
    def test_with_files(self, tmp_path):
        (tmp_path / "Equipment_Master.csv").write_text("Equipment_ID,Name,Type\nEQ001,A,B\n", encoding="utf-8")

        with patch("tools.local_artifacts.SOURCE_RAWDATA_DIR", str(tmp_path)):
            import tools.local_artifacts as s3_mod
            s3_mod._csv_schema_cache = None  # reset cache
            from tools.local_artifacts import read_csv_schema
            result = read_csv_schema()
            s3_mod._csv_schema_cache = None  # cleanup
        data = json.loads(result)
        assert "tables" in data
        assert "Equipment_Master" in data["tables"]

    def test_no_csv(self, tmp_path):
        with patch("tools.local_artifacts.SOURCE_RAWDATA_DIR", str(tmp_path)):
            import tools.local_artifacts as s3_mod
            s3_mod._csv_schema_cache = None
            from tools.local_artifacts import read_csv_schema
            result = read_csv_schema()
            s3_mod._csv_schema_cache = None
        data = json.loads(result)
        assert "error" in data


class TestProfileCsvData:
    @pytest.mark.parametrize("table_name", ["../outside", "/tmp/outside", "nested/table"])
    def test_rejects_paths_outside_rawdata_directory(self, tmp_path, table_name):
        with patch("tools.local_artifacts.SOURCE_RAWDATA_DIR", str(tmp_path)):
            from tools.local_artifacts import profile_csv_data
            result = profile_csv_data(table_name)

        data = json.loads(result)
        assert data["success"] is False


# ── 암묵지 도구 테스트 ──────────────────────────────


class TestListTacitFiles:
    def test_happy_path(self, tmp_path):
        (tmp_path / "process_flow.ttl").write_text("# flow", encoding="utf-8")

        with patch("tools.local_artifacts.SOURCE_TACIT_DIR", str(tmp_path)):
            from tools.local_artifacts import list_tacit_files
            result = json.loads(list_tacit_files())
        assert result["count"] == 1

    def test_no_files(self, tmp_path):
        with patch("tools.local_artifacts.SOURCE_TACIT_DIR", str(tmp_path)):
            from tools.local_artifacts import list_tacit_files
            result = json.loads(list_tacit_files())
        assert result["count"] == 0


class TestReadTacit:
    def test_read_single(self, tmp_path):
        (tmp_path / "flow.ttl").write_text("# flow content", encoding="utf-8")

        with patch("tools.local_artifacts.SOURCE_TACIT_DIR", str(tmp_path)):
            from tools.local_artifacts import read_tacit
            result = read_tacit("flow.ttl")
        assert "flow content" in result

    def test_read_all(self, tmp_path):
        (tmp_path / "a.ttl").write_text("# aaa", encoding="utf-8")
        (tmp_path / "b.ttl").write_text("# bbb", encoding="utf-8")

        with patch("tools.local_artifacts.SOURCE_TACIT_DIR", str(tmp_path)):
            from tools.local_artifacts import read_tacit
            result = read_tacit()
        assert "aaa" in result
        assert "bbb" in result

    def test_file_not_found(self, tmp_path):
        with patch("tools.local_artifacts.SOURCE_TACIT_DIR", str(tmp_path)):
            from tools.local_artifacts import read_tacit
            result = read_tacit("nonexistent.ttl")
        data = json.loads(result)
        assert "error" in data

    @pytest.mark.parametrize(
        "filename",
        ["../secret.ttl", "/tmp/secret.ttl", "nested/secret.ttl", r"..\secret.ttl"],
    )
    def test_rejects_paths_outside_tacit_directory(self, tmp_path, filename):
        secret = tmp_path.parent / "secret.ttl"
        secret.write_text("outside content", encoding="utf-8")

        with patch("tools.local_artifacts.SOURCE_TACIT_DIR", str(tmp_path)):
            from tools.local_artifacts import read_tacit
            result = read_tacit(filename)

        data = json.loads(result)
        assert data["success"] is False
        assert "outside content" not in result

    def test_rejects_symlink_outside_tacit_directory(self, tmp_path):
        secret = tmp_path.parent / "secret.ttl"
        secret.write_text("outside content", encoding="utf-8")
        (tmp_path / "linked.ttl").symlink_to(secret)

        with patch("tools.local_artifacts.SOURCE_TACIT_DIR", str(tmp_path)):
            from tools.local_artifacts import read_tacit
            result = read_tacit("linked.ttl")

        data = json.loads(result)
        assert data["success"] is False
        assert "outside content" not in result
