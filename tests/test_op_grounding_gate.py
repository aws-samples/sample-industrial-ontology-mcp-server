"""Step 22f — ObjectProperty 근거 게이트가 실제로 측정하는지 고정.

## 왜

DP 는 "CSV 컬럼과 연결됐는가" 를 강제·역기록·게이팅·딕셔너리 표시로 네 겹 확인하는데
OP 에는 그 넷이 하나도 없었다. 실측 (2026-08-12):

  CSV FK 로 표현 가능한 (source,target) 쌍 = 46
  선언된 OP = 229  /  A-Box 가 쓰는 OP = 14
  S2 작성 OP 사용률 4/160 (2%)  vs  같은 산출물의 DP 171/252 (68%)

근본 원인은 프롬프트다: ``04-property-rules.md`` 원칙 2 가 CSV FK 없이 24개 관계
쌍을 열거하고 원칙 5 가 모든 OP 에 역방향을 요구한다.

**이 게이트는 삭제하지 않는다** — 값 0건 OP 도 tacit·Restriction·설정·미래 CSV 라는
근거를 가질 수 있다. 근거를 네 갈래로 세고 **어떤 근거도 없는 것** 만 보고한다.
"""
from __future__ import annotations

import json
import sys

import pytest
from rdflib import RDFS, BNode, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.quality_steps import step_22f_op_grounding_gate as gate
from tools.quality_steps._base import StepContext

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _graph(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    return g


def _op(name: str) -> str:
    return (
        f"{NS_PREFIX}:{name} a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Alpha ; rdfs:range {NS_PREFIX}:Beta .\n"
    )


_CLASSES = f"{NS_PREFIX}:Alpha a owl:Class .\n{NS_PREFIX}:Beta a owl:Class .\n"


def _isolate(monkeypatch, *, used=frozenset(), configured=frozenset()):
    """A-Box·설정 신호를 픽스처로 고정 (실제 725,730 트리플 파싱 회피)."""
    monkeypatch.setattr(gate, "abox_used_local_names", lambda names: set(used),
                        raising=False)
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda names: set(used),
    )
    monkeypatch.setattr(gate, "_config_mentioned_names", lambda: set(configured))


def _run(g) -> dict:
    return gate.apply(g, StepContext(domain_ns=DOMAIN_NS)).stats


# ── THE REGRESSION: 근거 없는 OP 를 센다 ──────────────────────────────


def test_ungrounded_op_is_counted(monkeypatch):
    """핵심: 어떤 근거도 없는 OP 를 무근거로 센다."""
    _isolate(monkeypatch)
    stats = _run(_graph(_CLASSES + _op("imaginedOp")))
    assert stats["op_ungrounded"] == 1
    assert stats["op_ungrounded_names"] == ["imaginedOp"]


@pytest.mark.parametrize("signal", ["abox", "restriction", "config", "paired"])
def test_any_single_grounding_signal_clears_the_op(monkeypatch, signal):
    """PRESERVATION: 네 근거 중 **하나만** 있어도 무근거가 아니다.

    각 신호를 빼면 정당한 OP 가 무근거로 잘못 셈된다 — 그래서 넷이 모두 필요하다.
    """
    body = _CLASSES + _op("candidate")
    used, configured = set(), set()
    if signal == "abox":
        used = {"candidate"}
    elif signal == "restriction":
        body += (
            f"{NS_PREFIX}:Alpha rdfs:subClassOf "
            f"[ a owl:Restriction ; owl:onProperty {NS_PREFIX}:candidate ; "
            f"owl:someValuesFrom {NS_PREFIX}:Beta ] .\n"
        )
    elif signal == "config":
        configured = {"candidate"}
    elif signal == "paired":
        # 값 있는 OP 의 inverseOf 짝
        body += _op("busyOp") + (
            f"{NS_PREFIX}:candidate owl:inverseOf {NS_PREFIX}:busyOp .\n"
        )
        used = {"busyOp"}

    _isolate(monkeypatch, used=used, configured=configured)
    stats = _run(_graph(body))
    assert "candidate" not in stats["op_ungrounded_names"], (
        f"{signal} 근거가 있는데 무근거로 셌다: {stats}"
    )


def test_grounding_counts_are_reported_per_signal(monkeypatch):
    """각 근거의 개수를 따로 보고한다 — 합산하면 어느 축이 무너졌는지 안 보인다."""
    _isolate(monkeypatch, used={"usedOp"}, configured={"cfgOp"})
    body = _CLASSES + _op("usedOp") + _op("cfgOp") + _op("nakedOp")
    stats = _run(_graph(body))
    assert stats["op_grounded_abox"] == 1
    assert stats["op_grounded_config"] == 1
    assert stats["op_ungrounded"] == 1
    assert stats["op_grounding_checked"] == 3


# ── read-only 계약 ────────────────────────────────────────────────────


def test_gate_never_modifies_the_graph(monkeypatch):
    """측정 전용 — 그래프를 바꾸면 안 된다 (삭제는 위험하다고 판정됐다)."""
    _isolate(monkeypatch)
    g = _graph(_CLASSES + _op("imaginedOp"))
    snapshot = set(g)
    result = gate.apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert set(g) == snapshot, "read-only 게이트가 그래프를 수정했다"
    assert result.triples_delta == 0


# ── 게이트 정책 ───────────────────────────────────────────────────────


def test_warn_mode_does_not_raise(monkeypatch):
    """기본 ``warn`` 은 임계치를 넘어도 파이프라인을 세우지 않는다."""
    _isolate(monkeypatch)
    monkeypatch.setenv("TBOX_OP_GROUNDING_MAX", "0")
    monkeypatch.delenv("TBOX_OP_GROUNDING_GATE", raising=False)
    stats = _run(_graph(_CLASSES + _op("imaginedOp")))
    assert stats["op_grounding_gate_passed"] is False
    assert stats["op_grounding_gate_mode"] == "warn"


def test_fail_mode_raises_over_the_threshold(monkeypatch):
    """``fail`` 모드는 초과 시 ``RuntimeError``."""
    _isolate(monkeypatch)
    monkeypatch.setenv("TBOX_OP_GROUNDING_GATE", "fail")
    monkeypatch.setenv("TBOX_OP_GROUNDING_MAX", "0")
    with pytest.raises(RuntimeError, match="근거 없는 ObjectProperty"):
        _run(_graph(_CLASSES + _op("imaginedOp")))


def test_threshold_allows_the_current_baseline(monkeypatch):
    """임계치 이내면 PASS — 기준선을 잡아 **악화만** 잡는다.

    무근거 OP 는 프롬프트가 만든 것이라 S3 가 고칠 수 없다. 임계치를 0 으로 두면
    매 실행 FAIL 이라 아무도 보지 않게 된다.
    """
    _isolate(monkeypatch)
    monkeypatch.setenv("TBOX_OP_GROUNDING_MAX", "5")
    stats = _run(_graph(_CLASSES + _op("a") + _op("b")))
    assert stats["op_ungrounded"] == 2
    assert stats["op_grounding_gate_passed"] is True


def test_abox_signal_unavailable_is_reported(monkeypatch):
    """A-Box 판정 불가를 0건과 구분해 보고한다."""
    monkeypatch.setattr(
        "domain.graph_utils.abox_used_local_names", lambda names: None,
    )
    monkeypatch.setattr(gate, "_config_mentioned_names", lambda: set())
    stats = _run(_graph(_CLASSES + _op("imaginedOp")))
    assert stats["op_grounding_abox_signal_unavailable"] is True
    # 신호가 없어도 나머지 3개로 측정은 계속한다.
    assert stats["op_ungrounded"] == 1


def test_empty_tbox_is_a_clean_no_op(monkeypatch):
    """OP 가 없으면 조용히 0 을 보고한다."""
    _isolate(monkeypatch)
    stats = _run(_graph(_CLASSES))
    assert stats["op_grounding_checked"] == 0


def test_pipeline_output_stays_within_the_baseline(s3_output_stats):
    """실측 고정: 현재 S3 산출물의 무근거 OP 가 기준선을 넘지 않는다.

    이 수치가 오르면 프롬프트가 더 많은 유령 관계를 만들기 시작했다는 뜻이다.
    """
    if s3_output_stats is None:
        pytest.skip("S2 초안 픽스처 없음")
    stats = s3_output_stats
    assert stats["op_ungrounded"] <= stats["op_grounding_gate_max"], (
        f"무근거 OP {stats['op_ungrounded']}개가 기준선 "
        f"{stats['op_grounding_gate_max']} 을 넘었다: "
        f"{stats.get('op_ungrounded_names', [])[:5]}"
    )
    assert BNode and json and RDFS  # import 사용 표시


# ── 측정 지점 계약 (2026-08-30) ──────────────────────────────────────────
#
# 이 게이트는 **배포되는 T-Box** 를 재야 한다. ``step_22e_duplicate_op_prune`` 가
# OP 를 삭제하므로 그보다 앞에서 재면 배포되지 않는 중간 상태를 잰다:
#
#   22e 앞 (이전)  : OP 158 / 무근거 41  → baseline(40) 초과로 선재 실패
#   22e 뒤 (현재)  : OP 156 / 무근거 39  → 통과
#
# 즉 그 실패는 품질 악화가 아니라 **측정 지점 오류**였다. 순서를 되돌리면 같은
# 실패가 재발하므로 계약으로 고정한다.


def test_gate_runs_after_op_removing_steps():
    """THE REGRESSION: 게이트가 OP 삭제 스텝보다 뒤에 등록됐는가."""
    from tools.quality_steps import _POST_STEPS

    names = [fn.__module__.rsplit(".", 1)[-1] for fn in _POST_STEPS]
    assert "step_22f_op_grounding_gate" in names, names
    gate_at = names.index("step_22f_op_grounding_gate")
    for remover in ("step_22e_duplicate_op_prune", "step_21b_dead_stub_prune"):
        if remover not in names:
            continue
        assert names.index(remover) < gate_at, (
            f"{remover} 가 게이트보다 뒤에 있다 — 게이트가 배포되지 않는 "
            f"중간 상태를 잰다 (2026-08-30 에 이 배치로 baseline 2건 초과)"
        )


def test_gate_reports_its_measurement_position(monkeypatch):
    """자기점검 필드가 산출물에 남는가 — 순서가 바뀌면 드러나야 한다."""
    _isolate(monkeypatch)
    stats = _run(_graph(_CLASSES))

    assert stats["op_grounding_measured_after_prune"] is True


def test_position_selfcheck_detects_wrong_order(monkeypatch):
    """MUTATION: 삭제 스텝이 뒤에 오면 자기점검이 False 를 낸다.

    이 검사가 없으면 순서를 되돌려도 조용히 통과한다 — 실제로 ``.pyc`` 캐시 때문에
    순서 변경이 런타임에 반영되지 않은 채 "고쳤다" 로 읽힌 적이 있다.
    """
    import tools.quality_steps.step_22f_op_grounding_gate as gate

    # 소스 파싱 결과를 뒤바꾼 문자열로 대체한다 (파일은 건드리지 않는다).
    fake_source = (
        "    step_22f_op_grounding_gate.apply,\n"
        "    step_22e_duplicate_op_prune.apply,\n"
    )

    class _FakeInspect:
        @staticmethod
        def getsource(_mod):
            return fake_source

    monkeypatch.setitem(sys.modules, "inspect", _FakeInspect)
    try:
        assert gate._is_after_op_removing_steps() is False
    finally:
        monkeypatch.undo()


def test_position_selfcheck_fails_open_on_error(monkeypatch):
    """읽기 실패 시 True — 진단이 게이트를 시끄럽게 만들면 실제 신호가 묻힌다."""
    import tools.quality_steps.step_22f_op_grounding_gate as gate

    class _Boom:
        @staticmethod
        def getsource(_mod):
            raise OSError("no source")

    monkeypatch.setitem(sys.modules, "inspect", _Boom)
    try:
        assert gate._is_after_op_removing_steps() is True
    finally:
        monkeypatch.undo()


# ── 신호 품질을 구분해 보고하는가 (2026-08-30) ────────────────────────────
#
# ``op_grounding_abox_signal_unavailable`` 은 ``abox_used_local_names`` 가 ``None``
# 을 반환했는지만 본다. 그래서 세 상태가 같은 값을 낸다:
#
#   정상                          signal_unavailable=False
#   A-Box 파일 부재 + tacit 존재   signal_unavailable=False  ← 근거가 얇은데 정상처럼
#   신호 함수가 None              signal_unavailable=True
#
# 두 번째 경우 op_ungrounded 가 12 → 50 으로 튀는데 원인이 각인되지 않았다. S3 는 S7
# **앞** 이라 이 신호는 원래 이전 세대 A-Box 를 읽는다 — 배포 산출물 대조로 실손실은
# 0 이었지만(12f/21b candidates=0), 신호가 **비면** 임계를 넘긴다.


def test_abox_file_presence_is_reported(monkeypatch):
    """THE REGRESSION: A-Box 파일 존재 여부를 따로 각인한다."""
    _isolate(monkeypatch)
    stats = _run(_graph(_CLASSES + _op("candidate")))
    assert "op_grounding_abox_file_present" in stats, (
        "신호 품질이 각인되지 않았다 — signal_unavailable 만으로는 "
        "'정상' 과 '파일 없이 tacit 만' 이 구분되지 않는다"
    )


def test_missing_abox_file_is_distinguished_from_healthy_signal(monkeypatch, tmp_path):
    """파일이 없으면 signal_unavailable 이 False 여도 그 사실이 드러난다."""
    import tools.quality_steps.step_22f_op_grounding_gate as gate_mod

    _isolate(monkeypatch, used={"candidate"})
    monkeypatch.setattr(gate_mod, "ABOX_PATH", str(tmp_path / "missing.ttl"))
    stats = _run(_graph(_CLASSES + _op("candidate")))
    assert stats["op_grounding_abox_signal_unavailable"] is False
    assert stats["op_grounding_abox_file_present"] is False, (
        "A-Box 파일이 없는데 신호가 정상으로만 보고됐다"
    )


def test_signal_quality_field_present_in_early_return(monkeypatch):
    """OP 가 없는 조기 반환 경로에도 같은 키가 있어야 한다.

    한쪽에만 있으면 소비자가 키 부재를 "정상" 으로 오독한다
    (이 리포의 "필드 부재 ≠ 값 0" 함정 — 같은 이유로
    ``op_grounding_measured_after_prune`` 도 양쪽에 있다).
    """
    _isolate(monkeypatch)
    stats = _run(_graph(_CLASSES))
    assert stats["op_grounding_checked"] == 0
    assert "op_grounding_abox_file_present" in stats
