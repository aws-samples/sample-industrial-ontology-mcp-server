"""Tests for domain/tbox_utils load_tbox caching."""
from __future__ import annotations

import os
from unittest.mock import patch

from rdflib import URIRef
from rdflib.namespace import OWL, RDF

from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import (
    _new_graph,
    invalidate_graph_cache,
    load_graph_with_tbox,
    load_tbox,
)


def _write_tbox(tmp_path, extra_class: str = "A"):
    g = _new_graph()
    g.add((URIRef(f"{DOMAIN_NS}{extra_class}"), RDF.type, OWL.Class))
    p = tmp_path / "tbox.ttl"
    g.serialize(destination=str(p), format="turtle")
    return str(p)


def test_load_tbox_returns_graph(tmp_path):
    path = _write_tbox(tmp_path)
    invalidate_graph_cache()
    g = load_tbox(path)
    assert len(g) >= 1


def test_load_tbox_cache_hits_same_instance(tmp_path):
    """mtime 변화 없으면 동일 Graph 객체 반환."""
    path = _write_tbox(tmp_path)
    invalidate_graph_cache()
    g1 = load_tbox(path)
    g2 = load_tbox(path)
    assert g1 is g2


def test_load_tbox_reparse_on_mtime_change(tmp_path):
    path = _write_tbox(tmp_path, extra_class="A")
    invalidate_graph_cache()
    g1 = load_tbox(path)
    # 파일 수정 + mtime을 명시적으로 과거로 되돌려 비결정적 타이밍 제거
    old_stat = os.stat(path)
    _write_tbox(tmp_path, extra_class="B")
    # mtime을 이전 값보다 명확히 뒤로(1초) 옮겨 캐시 무효화 확실화
    os.utime(path, (old_stat.st_atime, old_stat.st_mtime + 1))
    g2 = load_tbox(path)
    # 새 파싱 → 다른 객체
    assert g1 is not g2


def test_load_tbox_missing_file_empty_graph(tmp_path):
    g = load_tbox(str(tmp_path / "nope.ttl"))
    assert len(g) == 0


def test_invalidate_clears_tbox_cache(tmp_path):
    path = _write_tbox(tmp_path)
    invalidate_graph_cache()
    g1 = load_tbox(path)
    invalidate_graph_cache()
    g2 = load_tbox(path)
    # 캐시 무효화 후는 새 객체
    assert g1 is not g2


def test_load_graph_with_tbox_returns_three_tuple(tmp_path):
    """(merged, tbox, tacit_count) 튜플 반환."""
    tbox_path = _write_tbox(tmp_path)
    abox_path = tmp_path / "abox.ttl"
    abox_path.write_text("")  # 빈 파일
    invalidate_graph_cache()

    with patch("domain.tbox_utils.TBOX_PATH", tbox_path), \
         patch("domain.tbox_utils.ABOX_PATH", str(abox_path)), \
         patch("domain.tbox_utils.SOURCE_TACIT_DIR", str(tmp_path / "_none")):
        g, tbox, tacit = load_graph_with_tbox()
    assert hasattr(g, "triples")
    assert hasattr(tbox, "triples")
    assert isinstance(tacit, int)
