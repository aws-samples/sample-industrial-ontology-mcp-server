"""Tests for tools/abox_support.py — #17 리팩터 기반 구조체."""
from tools.abox_support import AboxBuildContext


def test_context_default_empty():
    ctx = AboxBuildContext()
    assert ctx.individual_count == 0
    assert ctx.per_class_instances == {}


def test_record_instance_accumulates():
    ctx = AboxBuildContext()
    ctx.record_instance("EqM", 3)
    ctx.record_instance("EqM", 2)
    ctx.record_instance("Alarm", 1)
    assert ctx.per_class_instances == {"EqM": 5, "Alarm": 1}
    assert ctx.individual_count == 6


def test_record_op():
    ctx = AboxBuildContext()
    ctx.record_op("EqM", 10)
    ctx.record_op("EqM", 5)
    assert ctx.per_class_op_triples["EqM"] == 15
    assert ctx.object_property_count == 15


def test_record_duplicate_pk():
    ctx = AboxBuildContext()
    ctx.record_duplicate_pk("http://x/a")
    ctx.record_duplicate_pk("http://x/a")
    ctx.record_duplicate_pk("http://x/b")
    assert ctx.duplicate_pk_uris == {"http://x/a": 2, "http://x/b": 1}


def test_record_unverified_fk():
    ctx = AboxBuildContext()
    ctx.record_unverified_fk("TargetX")
    ctx.record_unverified_fk("TargetX")
    ctx.record_unverified_fk("TargetY")
    assert ctx.unverified_fk_targets == {"TargetX": 2, "TargetY": 1}


def test_summary_shape():
    ctx = AboxBuildContext()
    ctx.record_instance("A", 1)
    ctx.record_duplicate_pk("u1")
    ctx.record_duplicate_pk("u1")  # duplicate
    ctx.master_classes.add("A")
    summary = ctx.to_summary_dict()
    assert summary["instances"] == 1
    assert "per_class_instances" in summary
    assert summary["duplicate_pk_count"] == 1
    assert summary["master_classes"] == ["A"]
