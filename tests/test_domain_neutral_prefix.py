"""Step 12f 의 A-Box 사용 판정이 배포 prefix 에 종속되지 않는지 회귀 가드.

배경 (2026-08-08 실측): ``_abox_used_predicates`` 가 ``r"steel:(...)"`` 로
predicate 를 셌다. 이 리포는 도메인-중립이 설계 전제이고 prefix 는
``rules/domain/domain_config.json`` 에서 오는데, 정규식만 한 도메인 이름을 박아둔 것이다.

결과가 조용한 데이터 손실이라 위험도가 높다. 호출부가

    prunable = candidates - used

로 제거 대상을 정하므로, 스캔이 0건을 반환하면 **실제로 값이 적재된 DP 가 전부
제거 대상**이 된다. prefix 가 ``steel`` 이 아닌 배포에서는 A-Box 가 정상인데도
T-Box 에서 DP 가 사라진다 — 예외도 경고도 없이.

완전 IRI 직렬화(N-Triples / GraphDB export)도 같은 이유로 0건이었다. prefix 가
일치하는 배포에서조차 export 산출물을 스캔하면 손실이 난다.
"""
from __future__ import annotations

import sys

import pytest
from rdflib import OWL, RDF, URIRef

from domain.tbox_utils import _new_graph
from tools.quality_steps import step_12f_orphan_dp_prune as s12f
from tools.quality_steps._base import StepContext

DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")

#: Modules whose namespace snapshot this file must not leak into.
_SNAPSHOT_SCOPES = ("tools.", "domain.")


@pytest.fixture(autouse=True)
def _isolate_namespace_snapshots():
    """Keep the fake prefixes below from escaping into other test modules.

    ``monkeypatch`` restores ``domain.namespaces`` only. Every module that did
    ``from domain.namespaces import NS_PREFIX`` holds its own binding, taken at
    import time — so a module first imported *inside* a patched window keeps the
    fake value for the rest of the process.

    ``step_12f`` imports ``tools.abox_generation`` lazily, which is exactly that
    case. Measured: running this file before
    ``tests/test_cardinality_data_reality.py`` left
    ``tools.abox_generation.NS_PREFIX == "med"``, breaking the ``steel:`` prefix
    strip in ``_load_table_class_mapping`` and failing 10 of its tests. A full
    alphabetical run hides it because ``cardinality`` sorts first.

    Importing eagerly makes that snapshot happen with the real config; restoring
    the captured values afterwards covers any other module a step imports lazily.
    """
    import tools.abox_generation  # noqa: F401 — bind snapshot before patching

    saved = [
        (module, module.NS_PREFIX, getattr(module, "DOMAIN_NS", None))
        for module in list(sys.modules.values())
        if getattr(module, "__name__", "").startswith(_SNAPSHOT_SCOPES)
        and getattr(module, "NS_PREFIX", None) is not None
    ]
    yield
    for module, prefix, namespace in saved:
        module.NS_PREFIX = prefix
        if namespace is not None:
            module.DOMAIN_NS = namespace


def _orphan_graph(ns: str, dp_local: str):
    """A-Box 에서 쓰이는 DP 1개 — domain·source 없음 (유형 1 후보)."""
    g = _new_graph()
    dp = URIRef(ns + dp_local)
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    return g, dp


@pytest.mark.parametrize(
    ("prefix", "ns", "serialisation"),
    [
        # 배포 prefix 가 무엇이든 접두형 직렬화를 인식해야 한다.
        ("med", "http://hospital.org/med#", "{pfx}-inst:i {pfx}:{dp} \"v\" .\n"),
        ("fin", "http://bank.example/fin#", "{pfx}-inst:i {pfx}:{dp} \"v\" .\n"),
        # 같은 데이터의 완전 IRI 형태 (N-Triples, GraphDB export).
        ("med", "http://hospital.org/med#", "<{ns}i> <{ns}{dp}> \"v\" .\n"),
    ],
)
def test_abox_usage_detected_for_any_prefix(
    tmp_path, monkeypatch, prefix, ns, serialisation,
):
    """THE REGRESSION: 사용 중인 DP 를 어떤 배포에서도 제거하지 않는다."""
    import config
    from domain import namespaces

    monkeypatch.setattr(namespaces, "NS_PREFIX", prefix, raising=False)
    monkeypatch.setattr(namespaces, "DOMAIN_NS", ns, raising=False)

    dp_local = "patientBloodType"
    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        serialisation.format(pfx=prefix, ns=ns, dp=dp_local), encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    s12f._ABOX_USED_CACHE.clear()

    g, dp = _orphan_graph(ns, dp_local)
    result = s12f.apply(g, StepContext(domain_ns=ns))

    assert result.stats["pruned_names"] == [], (
        "A-Box 에 값이 있는 DP 가 제거 대상으로 판정됐다 — 데이터 손실 경로"
    )
    assert (dp, RDF.type, OWL.DatatypeProperty) in g


def test_unused_dp_still_pruned_under_other_prefix(tmp_path, monkeypatch):
    """반대 방향: 정말 미사용이면 다른 prefix 에서도 제거된다 (기능 보존)."""
    import config
    from domain import namespaces

    ns = "http://hospital.org/med#"
    monkeypatch.setattr(namespaces, "NS_PREFIX", "med", raising=False)
    monkeypatch.setattr(namespaces, "DOMAIN_NS", ns, raising=False)

    abox = tmp_path / "a_box.ttl"
    abox.write_text("med-inst:i med:otherProperty \"v\" .\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    s12f._ABOX_USED_CACHE.clear()

    g, dp = _orphan_graph(ns, "neverLoadedProperty")
    result = s12f.apply(g, StepContext(domain_ns=ns))

    assert result.stats["pruned_names"] == ["neverLoadedProperty"]
    assert (dp, RDF.type, OWL.DatatypeProperty) not in g


def test_other_namespace_predicate_does_not_count(tmp_path, monkeypatch):
    """같은 local name 이라도 남의 네임스페이스면 '사용' 이 아니다."""
    import config
    from domain import namespaces

    ns = "http://hospital.org/med#"
    monkeypatch.setattr(namespaces, "NS_PREFIX", "med", raising=False)
    monkeypatch.setattr(namespaces, "DOMAIN_NS", ns, raising=False)

    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        "<http://elsewhere.org/x#i> <http://elsewhere.org/x#sharedName> \"v\" .\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    s12f._ABOX_USED_CACHE.clear()

    g, _dp = _orphan_graph(ns, "sharedName")
    result = s12f.apply(g, StepContext(domain_ns=ns))

    assert result.stats["pruned_names"] == ["sharedName"]


def test_pruning_withheld_when_namespace_unconfigured(tmp_path, monkeypatch):
    """네임스페이스 설정이 비면 판정 불가 → 제거 보류 (추측하면 데이터가 사라진다)."""
    import config
    from domain import namespaces

    ns = "http://hospital.org/med#"
    monkeypatch.setattr(namespaces, "NS_PREFIX", "", raising=False)
    monkeypatch.setattr(namespaces, "DOMAIN_NS", "", raising=False)

    abox = tmp_path / "a_box.ttl"
    abox.write_text("med-inst:i med:patientBloodType \"v\" .\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    s12f._ABOX_USED_CACHE.clear()

    g, dp = _orphan_graph(ns, "patientBloodType")
    result = s12f.apply(g, StepContext(domain_ns=ns))

    assert result.stats["pruned_names"] == []
    assert (dp, RDF.type, OWL.DatatypeProperty) in g


#: Text-scan sites that must derive the prefix from config, never hardcode it.
#: Both carried the identical bug; ``_load_abox_op_usage`` was found 2026-08-08
#: after step_12f had already been fixed, so the guard now covers every site.
_SCAN_SITES = ("step_12f._abox_used_predicates", "ontology_quality._load_abox_op_usage")


def test_no_hardcoded_prefix_literal_in_scan_sites():
    """A-Box 텍스트 스캔 함수에 배포 prefix 리터럴이 되살아나지 않게 고정한다.

    prefix 리터럴은 눈에 잘 띄지 않고 실패도 조용해서, 동작 테스트만으로는
    다음 편집에서 되돌아오는 것을 막기 어렵다.

    두 함수 모두 ``domain_predicate_pattern`` 에 위임하므로, 위임이 사라지는
    것(= 자체 패턴을 다시 만드는 것)까지 함께 막는다.
    """
    import inspect

    from tools.ontology_quality import _load_abox_op_usage

    for label, fn in (
        (_SCAN_SITES[0], s12f._abox_used_predicates),
        (_SCAN_SITES[1], _load_abox_op_usage),
    ):
        source = inspect.getsource(fn)
        assert "steel:" not in source, (
            f"{label}: 배포 prefix 리터럴이 A-Box 스캔에 다시 들어왔다 — 다른 "
            "도메인에서 사용 중인 프로퍼티가 조용히 미사용으로 집계된다"
        )
        assert "domain_predicate_pattern" in source, (
            f"{label}: 공용 패턴 헬퍼 위임이 사라졌다 — 사본이 다시 갈라진다"
        )


def test_shared_pattern_withholds_when_namespace_unset(monkeypatch):
    """네임스페이스가 비면 헬퍼가 None 을 반환해 호출부가 판정을 보류하게 한다."""
    from domain import namespaces
    from domain.graph_utils import domain_predicate_pattern

    monkeypatch.setattr(namespaces, "NS_PREFIX", "", raising=False)
    monkeypatch.setattr(namespaces, "DOMAIN_NS", "", raising=False)
    assert domain_predicate_pattern({"anyName"}) is None


@pytest.mark.parametrize(
    ("prefix", "ns", "line"),
    [
        ("med", "http://hospital.org/med#", "med-inst:i med:patientBloodType 'v' ."),
        ("fin", "http://bank.example/fin#", "fin-inst:i fin:patientBloodType 'v' ."),
        ("med", "http://hospital.org/med#",
         "<http://hospital.org/med#i> <http://hospital.org/med#patientBloodType> 'v' ."),
    ],
)
def test_op_usage_counts_under_any_prefix(tmp_path, monkeypatch, prefix, ns, line):
    """THE SIBLING REGRESSION: OP usage 집계가 어떤 배포에서도 0 이 되지 않는다.

    이 카운트는 step_22 의 canonical OP 선택과 step_22e 의 삭제 대상 결정에
    쓰인다. 0 으로 접히면 값이 있는 OP 가 비어 있는 것으로 취급된다.
    """
    from domain import namespaces
    from tools import ontology_quality as oq

    monkeypatch.setattr(namespaces, "NS_PREFIX", prefix, raising=False)
    monkeypatch.setattr(namespaces, "DOMAIN_NS", ns, raising=False)
    abox = tmp_path / "a_box.ttl"
    abox.write_text(line + "\n", encoding="utf-8")
    oq._ABOX_OP_USAGE_CACHE.clear()

    assert oq._load_abox_op_usage(str(abox)).get("patientBloodType") == 1, (
        "사용 중인 프로퍼티가 미사용(0)으로 집계됐다 — canonical 선택이 뒤집히고 "
        "step_22e 는 값이 있는 OP 를 지운다"
    )
