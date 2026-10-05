"""의존 축(``_STEP_DEPS``) 판정의 방향성 검증.

## 무엇이 틀렸었나 (실측 2026-09-02)

배포 상태에서 ``S9_POST_MEASURE`` 만 ``skippable=True`` 였다. 같은 T-Box 변경이
14단계를 무효화하는데, **점수(96.2)를 만드는 단계만** 건너뛸 수 있었다. 원인은 축이
``inferred_mtime`` 하나였던 것이다 — ``measure_instance_quality`` 는 기본
``use_inferred=False`` 로 ``load_graph`` 를 부르고 그것은 T-Box + A-Box + tacit 을
병합한다. 즉 자기가 읽는 세 파일이 축에 없었다.

같은 형태로 rules/ 계열 축이 **0개** 였다. SME 가 ``tacit_rules.json`` 이나
``tbox_manual_additions.ttl`` 을 고쳐도 어느 단계도 재실행 대상이 되지 않았다.

## 이 테스트가 지키는 두 방향

- POSITIVE: 그 단계가 **실제로 읽는** 파일이 바뀌면 차단되고, 사유가 이름으로 나온다.
- NEGATIVE: 무관한 축이 바뀌어도 차단하지 않고, 새 축의 부재가 기존 체크포인트를
  무효화하지 않는다 (그러면 S2 46분 잡이 이유 없이 재실행 대상이 된다).
"""

from __future__ import annotations

import json
import os

import pytest

from tools.pipeline_state import _STEP_DEPS, _rules_fingerprint, evaluate_skip


def _state(step: str, saved: dict) -> dict:
    return {"completed_steps": {step: {"input_state": saved}}, "input_state": {}}


# ── POSITIVE: 읽는 파일이 바뀌면 차단되고 사유가 이름으로 나온다 ──────


@pytest.mark.parametrize(
    ("step", "axis"),
    [
        # 00b973f 가 S9_KG_VALIDATE 에 대해 고친 결함의 형제 3개.
        ("S9_POST_MEASURE", "tbox_mtime"),
        ("S9_POST_MEASURE", "abox_mtime"),
        ("S9_POST_MEASURE", "tacit_mtime"),
        ("S9_5_KG_MUTATION", "tbox_mtime"),
        # generate_abox 는 tacit TTL 을 a_box.ttl 에 병합한다 (실측: 관계 트리플
        # 90,094 중 51,223 이 tacit 유래, 15 술어 전부 A-Box 독자 생성 0건).
        ("S7_ABOX", "tacit_mtime"),
        # S5 의 축은 자기 출력 하나뿐이었다.
        ("S5_TACIT", "csv_mtime"),
        ("S5_TACIT", "rules_fingerprint"),
        # 엔진 정책·골든셋·shape 정본이 전부 rules/ 아래다.
        ("S3_IMPROVE", "rules_fingerprint"),
        ("S4_VALIDATE", "rules_fingerprint"),
        ("S9_OWL_SANITY", "rules_fingerprint"),
        ("S9_KG_VALIDATE", "rules_fingerprint"),
        # S13 은 tacit 통계를 렌더한다 (report.py::_collect_tacit_stats).
        ("S13_REPORT", "tacit_mtime"),
        # S12 는 CQ 정의와 딕셔너리 본문을 읽는다.
        ("S12_QUERY_TEST", "cq_mtime"),
        ("S12_QUERY_TEST", "dict_fingerprint"),
        ("S11_DICT_VALIDATE", "dict_fingerprint"),
        ("S12_5_GOLDEN_REGRESSION", "golden_mtime"),
        ("S2_TBOX", "cq_mtime"),
    ],
)
def test_changed_axis_blocks_and_names_itself(step, axis):
    """축 값이 달라지면 skip 불가이고, 어느 축 때문인지 응답에 이름이 있다."""
    assert axis in _STEP_DEPS[step], f"{step} 의 축 목록에 {axis} 가 없다"
    saved = dict.fromkeys(_STEP_DEPS[step], "same")
    current = dict(saved)
    current[axis] = "different"

    verdict = evaluate_skip(step, state=_state(step, saved), current_input=current)
    assert verdict["skippable"] is False
    assert verdict["blocked_by"] == [axis]
    assert verdict["unverified_axes"] == []


# ── NEGATIVE: 과잉 차단하지 않는다 ──────────────────────────────


def test_unrelated_axis_change_does_not_block():
    """무관한 축(LPG CSV)이 바뀌어도 S9_POST_MEASURE 는 건너뛸 수 있다."""
    step = "S9_POST_MEASURE"
    saved = dict.fromkeys(_STEP_DEPS[step], "same")
    current = dict(saved, lpg_csv_mtime="different", swrl_mtime="different")

    verdict = evaluate_skip(step, state=_state(step, saved), current_input=current)
    assert verdict["skippable"] is True
    assert verdict["blocked_by"] == []


def test_absent_axis_is_unverified_not_blocking():
    """새 축이 **기존** 체크포인트에 없다는 사실만으로 무효화하지 않는다.

    이 규칙이 없으면 축을 하나 추가할 때마다 24단계가 한꺼번에 재실행 대상이 되고,
    S2 는 46분 잡이라 그 손실이 그대로 시간이다. 키의 부재는 "그때 기록하지
    않았다" 는 뜻이지 "바뀌었다" 가 아니다 — 판정 불가를 판정으로 보고하지 않는다.
    """
    step = "S9_POST_MEASURE"
    saved = {"inferred_mtime": "same"}  # 구 스키마: 축이 하나뿐이던 시절
    current = {
        "inferred_mtime": "same",
        "tbox_mtime": "x",
        "abox_mtime": "y",
        "tacit_mtime": "z",
    }
    verdict = evaluate_skip(step, state=_state(step, saved), current_input=current)
    assert verdict["skippable"] is True
    assert verdict["blocked_by"] == []
    assert set(verdict["unverified_axes"]) == {
        "tbox_mtime", "abox_mtime", "tacit_mtime",
    }


def test_absent_axis_still_blocks_on_a_recorded_axis():
    """미검증 축이 있어도, **기록된** 축이 달라지면 차단은 그대로 동작한다."""
    step = "S9_POST_MEASURE"
    saved = {"inferred_mtime": "old"}
    current = {"inferred_mtime": "new", "tbox_mtime": "x"}
    verdict = evaluate_skip(step, state=_state(step, saved), current_input=current)
    assert verdict["skippable"] is False
    assert verdict["blocked_by"] == ["inferred_mtime"]


def test_never_completed_step_is_not_skippable():
    verdict = evaluate_skip("S9_POST_MEASURE", state={"completed_steps": {}})
    assert verdict["skippable"] is False
    assert verdict["blocked_by"] == []


# ── rules_fingerprint: 무엇을 세고 무엇을 무시하는가 ─────────────


def _seed_rules(root, monkeypatch):
    """``rules/`` 구조를 tmp 에 재현하고 해석기 루트를 그쪽으로 돌린다."""
    for sub in ("domain", "policy", "contracts", "mutations/M1_domain_range"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    files = {
        "domain/tacit_rules.json": {"mappings": []},
        "domain/domain_config.json": {"ns": "x"},
        "policy/quality_thresholds.json": {"t": 1},
        "contracts/common_dp.json": {"common_datatype_properties": {}},
    }
    for rel, payload in files.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    for rel, text in {
        "domain/tbox_manual_additions.ttl": "# manual\n",
        "policy/tbox_shapes.ttl": "# shapes\n",
        "mutations/M1_domain_range/delete_domain.sparql": "DELETE {}\n",
        # 참조용 사본 — 지문에서 제외돼야 한다.
        "domain/tacit_rules.example.steel.json": "{}",
        "domain/tacit_rules.suggested.json": "{}",
    }.items():
        with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
            f.write(text)
    monkeypatch.setattr("domain.rules_paths.RULES_ROOT", str(root))


@pytest.mark.parametrize(
    "target",
    [
        "domain/tacit_rules.json",
        "domain/tbox_manual_additions.ttl",   # SME 탈출구 — 정본이 TTL 이라 *.json glob 밖
        "policy/tbox_shapes.ttl",             # SHACL shape 정본
        "policy/quality_thresholds.json",
        "contracts/common_dp.json",
        "mutations/M1_domain_range/delete_domain.sparql",
    ],
)
def test_rules_fingerprint_reacts_to_engine_inputs(tmp_path, monkeypatch, target):
    _seed_rules(tmp_path, monkeypatch)
    before = _rules_fingerprint()
    with open(tmp_path / target, "a", encoding="utf-8") as f:
        f.write("\n# edited\n")
    assert _rules_fingerprint() != before, f"{target} 편집이 지문에 반영되지 않았다"


@pytest.mark.parametrize(
    "target",
    ["domain/tacit_rules.example.steel.json", "domain/tacit_rules.suggested.json"],
)
def test_rules_fingerprint_ignores_reference_copies(tmp_path, monkeypatch, target):
    """예시·초안 사본 편집은 파이프라인 산출물에 영향이 없으므로 무시한다.

    이것을 세면 예시 파일을 손질할 때마다 S2(46분)가 재실행 대상이 된다.
    """
    _seed_rules(tmp_path, monkeypatch)
    before = _rules_fingerprint()
    with open(tmp_path / target, "a", encoding="utf-8") as f:
        f.write("\n")
    assert _rules_fingerprint() == before, f"{target} 는 지문에서 제외돼야 한다"


def test_rules_fingerprint_reacts_to_file_removal(tmp_path, monkeypatch):
    """파일이 사라지는 것도 변경이다 — glob 이 0건을 정상 반환하는 함정 대응."""
    _seed_rules(tmp_path, monkeypatch)
    before = _rules_fingerprint()
    os.remove(tmp_path / "domain" / "tacit_rules.json")
    assert _rules_fingerprint() != before


# ── 축 목록 자체의 불변식 ────────────────────────────────────────


def test_every_step_declares_at_least_one_axis():
    for step, axes in _STEP_DEPS.items():
        assert axes, f"{step} 에 의존 축이 없다 — 영구 skippable 이 된다"


def test_no_step_depends_only_on_its_own_output():
    """자기 출력만 보는 축 구성은 입력 변경을 영구히 놓친다 (S5 의 과거 상태)."""
    own_output_only = {
        "S5_TACIT": {"tacit_mtime"},
        "S9_POST_MEASURE": {"inferred_mtime"},
        "S9_5_KG_MUTATION": {"inferred_mtime"},
    }
    for step, output_axes in own_output_only.items():
        assert set(_STEP_DEPS[step]) - output_axes, (
            f"{step} 의 축이 자기 출력({output_axes})뿐이다"
        )
