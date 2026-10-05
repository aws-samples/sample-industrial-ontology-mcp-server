"""CLAUDE.md 파이프라인 다이어그램 + 시간표 + 문서 업데이트 검증 (작업코드 기능 단계; docs/reference/task-glossary.md).

상태머신·인프라가 모두 정렬된 후, 에이전트가 실행할 **순서** 가 CLAUDE.md
에 명시되어야 비로소 '파이프라인 순서 변경 완료'. 이 파일은 문서 회귀 방지용.
"""
from __future__ import annotations

import os


def _read_claude_md() -> str:
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "CLAUDE.md",
    )
    return open(path, encoding="utf-8").read()


def test_claude_md_pipeline_diagram_includes_s6_5():
    """FULL_PIPELINE 다이어그램에 S6_5_DICT_V1 블록 존재."""
    md = _read_claude_md()
    assert "[S6_5_DICT_V1]" in md, "파이프라인 다이어그램에 S6_5_DICT_V1 블록 없음"


def test_claude_md_s6_5_placed_between_s6_and_s7():
    """다이어그램에서 S6_5 가 S6_VIS 와 S7_ABOX 사이에 배치.

    라벨은 **`save_step` 정본 키**(`tools/pipeline_state.py::_STEP_DEPS`)여야 한다 — 예전엔
    `[S6_VISUALIZE]` / `[S7_ABOX_GEN]` 이었고 그 이름으로 `save_step` 을 부르면 성공 JSON 을
    받고 `check_pipeline_state` 에서 영구 비가시가 된다.

    ``find`` 가 -1 을 반환하면 부등식이 자동 성립해 **라벨이 사라져도 통과**하므로
    (실측: 이 테스트는 그 상태로 아무것도 지키지 않았다) 존재를 먼저 단정한다.
    """
    md = _read_claude_md()
    for label in ("[S6_VIS]", "[S6_5_DICT_V1]", "[S7_ABOX]"):
        assert label in md, f"다이어그램에 {label} 라벨이 없다 (정본 키를 써야 한다)"
    assert md.find("[S6_VIS]") < md.find("[S6_5_DICT_V1]") < md.find("[S7_ABOX]"), \
        "순서 위반: S6_VIS < S6_5_DICT_V1 < S7_ABOX 이어야 함"


def test_claude_md_diagram_labels_are_canonical_step_keys():
    """THE REGRESSION: 다이어그램 라벨 전부가 `save_step` 이 받는 정본 키다.

    `save_step` 은 이름을 검증하지 않는다 (화이트리스트 없음). 문서가 비정본 라벨을 쓰면
    에이전트가 그것을 그대로 넘겨 체크포인트가 조용히 유실된다 — 실측 재발 증거가
    `quality_history.json` 에 남아 있다 (`S4_TBOX_VALIDATE` 1건이 2분 뒤 정본
    `S4_VALIDATE` 로 재기록됐다).
    """
    import re

    from tools.pipeline_state import _ALL_STEPS

    md = _read_claude_md()
    labels = set(re.findall(r"\[(S\d[0-9A-Za-z_]*)\]", md))
    assert labels, "다이어그램 라벨을 하나도 찾지 못했다 — 정규식이나 문서 형식을 확인하라"
    non_canonical = sorted(label for label in labels if label not in _ALL_STEPS)
    assert not non_canonical, (
        f"비정본 라벨 {non_canonical} — save_step 이 이 이름을 받으면 성공 JSON 을 내고 "
        f"check_pipeline_state 에서 사라진다. 정본: {sorted(_ALL_STEPS)}"
    )


def test_claude_md_timing_table_has_s6_5_row():
    """파이프라인 단계별 예상 소요시간 표에 S6.5 행 존재."""
    md = _read_claude_md()
    assert "S6.5 딕셔너리 v1" in md or "S6.5" in md
    # 시간표 맥락 — S6 시각화와 S7 A-Box 사이
    s6_vis_idx = md.find("| S6 시각화 |")
    s65_row_idx = md.find("S6.5 딕셔너리")
    s7_abox_idx = md.find("| S7 A-Box |")
    # 시간표 섹션은 한 번만 있다고 가정
    if s6_vis_idx > 0 and s7_abox_idx > 0:
        assert s6_vis_idx < s65_row_idx < s7_abox_idx, \
            "시간표에서 S6.5 행이 S6 / S7 사이에 없음"


def test_claude_md_declares_vocabulary_contract_role():
    """S6.5 의 역할 'vocabulary contract' 명시."""
    md = _read_claude_md()
    assert "vocabulary contract" in md, "vocabulary contract 역할 설명 부재"


def test_claude_md_s7_references_use_dict_contract():
    """S7 이 use_dict_contract 인자 사용 명시."""
    md = _read_claude_md()
    assert "use_dict_contract=True" in md, \
        "S7 의 use_dict_contract=True 언급 부재"


def test_claude_md_s10_uses_include_stats_true():
    """S10 이 include_stats=True 로 v2 덮어쓰기 명시."""
    md = _read_claude_md()
    assert "include_stats=True" in md, \
        "S10 의 include_stats=True 언급 부재"
