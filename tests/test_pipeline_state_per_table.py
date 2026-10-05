"""pipeline_state 테이블별 해시/지문 테스트."""
from __future__ import annotations

import os
from unittest.mock import patch


def _write(path: str, text: str = "a,b\n1,2\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_per_table_fingerprint_lists_all_tables(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(str(rawdata / "TableA.csv"))
    _write(str(rawdata / "TableB.csv"))
    from tools import pipeline_state as ps
    with patch.object(ps, "SOURCE_RAWDATA_DIR", str(rawdata)):
        fp = ps._per_table_fingerprint()
    assert set(fp.keys()) == {"TableA", "TableB"}
    # 각 값은 (mtime, size) 튜플
    for v in fp.values():
        assert isinstance(v, tuple)
        assert len(v) == 2


def test_changed_tables_detects_content_change(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(str(rawdata / "A.csv"), "x,y\n1,2\n")
    _write(str(rawdata / "B.csv"), "x,y\n3,4\n")
    from tools import pipeline_state as ps
    with patch.object(ps, "SOURCE_RAWDATA_DIR", str(rawdata)):
        fp_before = ps._per_table_fingerprint()
        # B 만 수정
        path_b = str(rawdata / "B.csv")
        _write(path_b, "x,y\n3,4\n5,6\n")
        # mtime 차이 보장
        os.utime(path_b, (fp_before["B"][0] + 1, fp_before["B"][0] + 1))
        changed = ps.changed_tables_since(fp_before)
    assert changed == ["B"]


def test_changed_tables_detects_new_file(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(str(rawdata / "A.csv"))
    from tools import pipeline_state as ps
    with patch.object(ps, "SOURCE_RAWDATA_DIR", str(rawdata)):
        fp_before = ps._per_table_fingerprint()
        _write(str(rawdata / "C.csv"))
        changed = ps.changed_tables_since(fp_before)
    assert "C" in changed


def test_changed_tables_detects_removed_file(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(str(rawdata / "A.csv"))
    _write(str(rawdata / "B.csv"))
    from tools import pipeline_state as ps
    with patch.object(ps, "SOURCE_RAWDATA_DIR", str(rawdata)):
        fp_before = ps._per_table_fingerprint()
        os.remove(str(rawdata / "B.csv"))
        changed = ps.changed_tables_since(fp_before)
    assert "B" in changed


def test_changed_tables_none_means_first_run(tmp_path):
    rawdata = tmp_path / "rawdata"
    _write(str(rawdata / "A.csv"))
    from tools import pipeline_state as ps
    with patch.object(ps, "SOURCE_RAWDATA_DIR", str(rawdata)):
        changed = ps.changed_tables_since(None)
    assert changed == ["A"]
