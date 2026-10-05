"""Tests for tools/tbox_postprocess/__init__.py — #7 단계 registry."""
import pytest

from tools.tbox_postprocess import (
    STEP_REGISTRY,
    count_steps,
    describe_step,
    list_steps,
)


def test_count_steps_19():
    # 0 ~ 18 = 19 단계
    assert count_steps() == 19


def test_every_step_has_name_and_purpose():
    for i in range(19):
        info = describe_step(i)
        assert info["name"]
        assert info["purpose"]
        assert isinstance(info["affects"], list)


def test_step_numbers_continuous():
    numbers = sorted(STEP_REGISTRY.keys())
    assert numbers == list(range(19))


def test_unknown_step_raises():
    with pytest.raises(KeyError):
        describe_step(99)


def test_list_steps_ordered():
    all_steps = list_steps()
    assert [s["number"] for s in all_steps] == list(range(19))


def test_citations_present_on_key_steps():
    # Step 8 (xsd:date) + 17 (Completeness) 은 citation 필요
    assert describe_step(8)["citation"]
    assert describe_step(17)["citation"]
