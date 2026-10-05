"""Scalability regression tests for tools/kg_validation.py.

대용량 합성 그래프를 생성하여 validate_kg()가:
1. 트리플 수가 늘어도 **선형적**으로 메모리를 쓰는지 (지수 폭주 방지)
2. 검증 로직 자체에 **새로운 누수**가 생기지 않는지 (tracemalloc 출처 추적)
3. 실측 RSS가 **합리적 상한** 안에 들어오는지

기본 테스트 스위트에서는 skip (수 분 소요). 실행:
    pytest -m scalability -v tests/test_validation_scalability.py
"""
from __future__ import annotations

import gc
import resource
import tracemalloc
from pathlib import Path
from unittest.mock import patch

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph

pytestmark = pytest.mark.scalability


# ── 합성 그래프 생성기 ────────────────────────────────


def _build_synthetic_tbox(num_classes: int = 10, num_ops: int = 15, num_dps: int = 20) -> Graph:
    """다양한 OP/DP가 정의된 합성 T-Box를 생성한다."""
    g = _new_graph()
    classes = []
    for i in range(num_classes):
        cls = URIRef(f"{DOMAIN_NS}Class{i:03d}")
        g.add((cls, RDF.type, OWL.Class))
        classes.append(cls)
        if i > 0:
            g.add((cls, RDFS.subClassOf, classes[i - 1]))

    # ObjectProperty + inverseOf 쌍
    for i in range(num_ops):
        fwd = URIRef(f"{DOMAIN_NS}hasRelation{i:03d}")
        inv = URIRef(f"{DOMAIN_NS}isRelationOf{i:03d}")
        dom = classes[i % num_classes]
        rng = classes[(i + 1) % num_classes]
        g.add((fwd, RDF.type, OWL.ObjectProperty))
        g.add((inv, RDF.type, OWL.ObjectProperty))
        g.add((fwd, RDFS.domain, dom))
        g.add((fwd, RDFS.range, rng))
        g.add((inv, RDFS.domain, rng))
        g.add((inv, RDFS.range, dom))
        g.add((fwd, OWL.inverseOf, inv))
        g.add((inv, OWL.inverseOf, fwd))

    # DatatypeProperty (수치 + 문자열 혼합)
    for i in range(num_dps):
        dp = URIRef(f"{DOMAIN_NS}value{i:03d}")
        g.add((dp, RDF.type, OWL.DatatypeProperty))
        g.add((dp, RDFS.domain, classes[i % num_classes]))
        g.add((dp, RDFS.range, XSD.decimal if i % 2 == 0 else XSD.string))

    return g


def _build_synthetic_abox(
    tbox: Graph,
    num_instances: int,
    num_classes: int = 10,
    num_ops: int = 15,
    num_dps: int = 20,
) -> Graph:
    """N개 인스턴스 × 프로퍼티 평균 사용률을 가진 합성 A-Box를 생성한다."""
    g = _new_graph()
    for s, p, o in tbox:
        g.add((s, p, o))

    instances = []
    for i in range(num_instances):
        cls_idx = i % num_classes
        inst = URIRef(f"{DOMAIN_INST_NS}inst_{i:07d}")
        cls = URIRef(f"{DOMAIN_NS}Class{cls_idx:03d}")
        g.add((inst, RDF.type, cls))
        instances.append((inst, cls_idx))

    # ObjectProperty 트리플: 각 인스턴스가 평균 2개의 관계를 가짐
    for i, (inst, _cls_idx) in enumerate(instances):
        for k in range(2):
            op_idx = (i + k) % num_ops
            target_i = (i + 1 + k) % num_instances
            target_inst = instances[target_i][0]
            fwd = URIRef(f"{DOMAIN_NS}hasRelation{op_idx:03d}")
            inv = URIRef(f"{DOMAIN_NS}isRelationOf{op_idx:03d}")
            g.add((inst, fwd, target_inst))
            g.add((target_inst, inv, inst))

    # DatatypeProperty 트리플: 각 인스턴스 평균 3개
    for i, (inst, _cls_idx) in enumerate(instances):
        for k in range(3):
            dp_idx = (i + k) % num_dps
            dp = URIRef(f"{DOMAIN_NS}value{dp_idx:03d}")
            if dp_idx % 2 == 0:
                g.add((inst, dp, Literal(float(i + k), datatype=XSD.decimal)))
            else:
                g.add((inst, dp, Literal(f"val_{i}_{k}", datatype=XSD.string)))

    return g


def _rss_mb() -> float:
    """현재 프로세스의 peak RSS (MB). macOS는 bytes, Linux는 KB 단위."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS 반환값이 바이트 단위 — heuristic: 값이 매우 크면 bytes로 간주
    return rss / 1024 / 1024 if rss > 1_000_000 else rss / 1024


def _self_leaked_mb(snapshot: tracemalloc.Snapshot) -> float:
    """tracemalloc snapshot에서 tools/kg_validation.py 출처 할당 총량 (MB)."""
    total = 0
    target = "tools/kg_validation.py"
    for stat in snapshot.statistics("filename"):
        if target in str(stat.traceback):
            total += stat.size
    return total / 1024 / 1024


# ── 확장성 테스트 ─────────────────────────────────────


@pytest.fixture
def synthetic_files(tmp_path: Path):
    """tmp_path에 T-Box + A-Box TTL 파일을 써서 validate_kg가 읽을 수 있게 한다."""
    def _make(num_instances: int) -> dict:
        tbox = _build_synthetic_tbox()
        abox = _build_synthetic_abox(tbox, num_instances=num_instances)

        tbox_path = tmp_path / f"tbox_{num_instances}.ttl"
        abox_path = tmp_path / f"abox_{num_instances}.ttl"
        tbox.serialize(destination=str(tbox_path), format="turtle")
        abox.serialize(destination=str(abox_path), format="turtle")
        return {
            "tbox_path": str(tbox_path),
            "abox_path": str(abox_path),
            "triples": len(abox),
            "instances": num_instances,
        }
    return _make


def _run_validate_with_paths(tbox_path: str, abox_path: str) -> dict:
    """임의 경로의 TTL을 대상으로 validate_kg를 실행하고 결과를 파싱한다."""
    import json

    # load_graph 캐시 제거 (각 테스트 독립성)
    from domain.tbox_utils import invalidate_graph_cache
    invalidate_graph_cache()

    # tools.kg_validation이 참조하는 경로 상수를 패치
    with patch("tools.kg_validation.TBOX_PATH", tbox_path), \
         patch("tools.kg_validation.ABOX_PATH", abox_path), \
         patch("tools.kg_validation.MASTER_DATA_PATH", "/nonexistent/master.ttl"), \
         patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/inferred.ttl"), \
         patch("tools.kg_validation.SOURCE_RAWDATA_DIR", "/nonexistent/raw"), \
         patch("tools.kg_validation.SOURCE_TACIT_DIR", "/nonexistent/tacit"), \
         patch("domain.tbox_utils.TBOX_PATH", tbox_path), \
         patch("domain.tbox_utils.ABOX_PATH", abox_path), \
         patch("domain.tbox_utils.INFERRED_PATH", "/nonexistent/inferred.ttl"), \
         patch("domain.tbox_utils.SOURCE_TACIT_DIR", "/nonexistent/tacit"):
        from tools.kg_validation import validate_kg
        raw = validate_kg(use_inferred=False)
    return json.loads(raw)


class TestValidateKgCorrectness:
    """합성 그래프 기반 기능 검증 (소규모)."""

    def test_small_synthetic_graph_passes(self, synthetic_files):
        """소규모 합성 그래프에서 22개 check가 모두 실행되는지 (validate_kg registry=22)."""
        spec = synthetic_files(num_instances=1_000)
        result = _run_validate_with_paths(spec["tbox_path"], spec["abox_path"])
        assert result["success"] is True
        assert len(result["checks"]) == 23
        # 모든 check에 name, passed 필드가 있어야 함
        for c in result["checks"]:
            assert "name" in c
            assert "passed" in c


class TestValidateKgLinearScaling:
    """규모 증가에 따른 self-leak / 메모리 선형성 검증."""

    @pytest.mark.parametrize("num_instances", [10_000, 50_000])
    def test_no_self_leak(self, synthetic_files, num_instances):
        """tools/kg_validation.py 자체의 할당이 규모와 선형 관계인지.

        validate_kg 종료 후 해당 파일 출처 tracemalloc 블록이
        인스턴스당 일정 상한(5KB) 이하여야 한다.
        """
        spec = synthetic_files(num_instances=num_instances)

        tracemalloc.start()
        try:
            gc.collect()
            result = _run_validate_with_paths(spec["tbox_path"], spec["abox_path"])
            gc.collect()
            snapshot = tracemalloc.take_snapshot()
        finally:
            tracemalloc.stop()

        assert result["success"] is True

        leaked_mb = _self_leaked_mb(snapshot)
        # 인스턴스당 5KB × N 상한. 2배 마진 포함.
        budget_mb = max(5.0, num_instances * 5 / 1024 * 2)
        assert leaked_mb < budget_mb, (
            f"kg_validation.py self-allocated {leaked_mb:.1f} MB "
            f"exceeds budget {budget_mb:.1f} MB (instances={num_instances}). "
            f"잠재적 누수 또는 자료구조 폭주."
        )

    def test_memory_grows_linearly_with_triples(self, synthetic_files):
        """규모 5배 증가 시 kg_validation 자체 할당이 5배를 크게 넘지 않는지."""
        small = synthetic_files(num_instances=5_000)
        large = synthetic_files(num_instances=25_000)

        def measure(spec: dict) -> float:
            tracemalloc.start()
            try:
                gc.collect()
                _run_validate_with_paths(spec["tbox_path"], spec["abox_path"])
                gc.collect()
                snap = tracemalloc.take_snapshot()
            finally:
                tracemalloc.stop()
            return _self_leaked_mb(snap)

        small_mb = measure(small)
        large_mb = measure(large)

        ratio_triples = large["triples"] / max(small["triples"], 1)
        ratio_mem = large_mb / max(small_mb, 0.1)

        # 메모리 증가율이 트리플 증가율의 2배를 넘으면 비선형 폭주로 간주
        assert ratio_mem < ratio_triples * 2, (
            f"비선형 메모리 증가: triples {ratio_triples:.2f}x → mem {ratio_mem:.2f}x "
            f"(small={small_mb:.1f}MB @ {small['triples']:,}, "
            f"large={large_mb:.1f}MB @ {large['triples']:,})"
        )


class TestValidateKgRssBudget:
    """절대 RSS 상한 — 환경 변동 흡수하기 위해 관대한 마진 적용."""

    def test_rss_budget_for_50k_instances(self, synthetic_files):
        """5만 인스턴스 합성 그래프에서 peak RSS 상한 (환경차 마진 포함)."""
        spec = synthetic_files(num_instances=50_000)

        gc.collect()
        _run_validate_with_paths(spec["tbox_path"], spec["abox_path"])
        gc.collect()

        peak_mb = _rss_mb()
        # 5만 인스턴스 × 10~15 트리플 = ~60만 트리플, rdflib Memory store 기준 ~500MB.
        # CI 환경 변동 + 파이썬 런타임 포함 2GB 상한.
        assert peak_mb < 2048, f"Peak RSS {peak_mb:.0f}MB exceeds 2GB budget"
