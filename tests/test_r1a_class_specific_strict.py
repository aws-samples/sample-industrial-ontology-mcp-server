"""기능 단계 — 프롬프트 강화 검증.

prompts/tbox-prompt-modules/04-property-rules.md 가 Path B 정책을 명시하고
generic DP 이름을 금지하는지 검증. 프롬프트가 render 되어 cached_prefix 에
실제 포함되는지도 확인.
"""
from __future__ import annotations

import os


def _read_prompt() -> str:
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "prompts", "tbox-prompt-modules", "04-property-rules.md",
    )
    return open(path, encoding="utf-8").read()


def test_prompt_declares_path_b_class_specific_principle():
    """프롬프트에 'class-specific DP 필수 (Path B 정책)' 섹션 존재."""
    prompt = _read_prompt()
    assert "class-specific DP 필수" in prompt or "class-specific" in prompt.lower()
    assert "Path B" in prompt, "Path B 정책 명시 필요"


def test_prompt_lists_forbidden_generic_names():
    """금지되는 generic 이름 목록이 명시적으로 열거됨."""
    prompt = _read_prompt()
    forbidden = [
        "hasValue", "hasTemperature", "hasStatus", "hasTimestamp",
        "hasIdentifier", "hasQuantity", "hasUnit", "hasLocation",
        "hasResult", "hasSeverity",
    ]
    for name in forbidden:
        assert name in prompt, f"{name} 금지 목록에 없음"


def test_prompt_no_longer_allows_owl_thing_fallback():
    """예전 L75 'domain 없이 정의하거나 owl:Thing' 지시가 삭제됨.

    대신 owl:Thing 이 **명시적 금지** 섹션에 포함되어야 함 (`**owl:Thing` 금지 또는
    `rdfs:domain owl:Thing` 금지).
    """
    prompt = _read_prompt()
    # 구 L75 fallback 지시 자체 금지
    assert "domain 없이 정의하거나 owl:Thing" not in prompt, \
        "구 L75 fallback 지시가 여전히 남아있음"
    # 금지 섹션에 owl:Thing 이 명시되어야 — 다음 문구 중 최소 1개
    forbid_phrases = [
        "owl:Thing` 금지",
        "`owl:Thing` 금지",
        "rdfs:domain owl:Thing",  # 금지 대상 열거에 나타남
        "owl:Thing 금지",
    ]
    assert any(p in prompt for p in forbid_phrases), \
        f"owl:Thing 의 명시적 금지 문구 부재. 필요 문구 중 하나: {forbid_phrases}"


def test_prompt_requires_rdfs_domain():
    """rdfs:domain 필수 + absent / owl:Thing 금지 명시."""
    prompt = _read_prompt()
    assert "rdfs:domain" in prompt
    # 'absent 금지' 또는 유사 표현
    assert ("absent 금지" in prompt or "rdfs:domain` 필수" in prompt or
            "domain` **필수**" in prompt or "비어 있지" in prompt or
            "absent" in prompt.lower())


def test_prompt_provides_class_prefix_naming_examples():
    """class-specific 변환 예시 (equipmentStatusValue 등) 제공."""
    prompt = _read_prompt()
    # 핵심 예시들
    assert "equipmentStatusValue" in prompt
    assert "alarmEventsTimestamp" in prompt


def test_prompt_covers_shared_column_handling():
    """3+ 클래스 공유 컬럼 처리 방법 (class 별 별도 DP / abstract parent) 명시."""
    prompt = _read_prompt()
    assert "Shared columns" in prompt
    # 두 가지 방법 중 최소 하나 명시
    assert ("abstract parent" in prompt or "subClassOf" in prompt or
            "각 class 별 별도" in prompt or "별도 DP 생성" in prompt)


def test_prompt_renders_in_cached_prefix():
    """_build_chunk_prompt 호출 시 04-property-rules.md 내용이 cached_prefix 에 포함됨."""
    import csv as _csv

    from config import SOURCE_RAWDATA_DIR
    from tools.tbox_generation import _build_chunk_prompt, _load_table_class_mapping

    csv_path = os.path.join(SOURCE_RAWDATA_DIR, "Chemical_Analysis.csv")
    if not os.path.exists(csv_path):
        import pytest
        pytest.skip("Chemical_Analysis.csv missing — skipping render test")

    with open(csv_path, encoding="utf-8") as f:
        header = next(_csv.reader(f))
    cached_prefix, _ = _build_chunk_prompt(
        chunk_idx=0,
        chunk_tables=[{"name": "Chemical_Analysis", "columns": header}],
        total_chunks=10,
        iof_summary="",
        schema_info={},
        relationships_info="",
        table_class_map=_load_table_class_mapping(),
    )
    # Path B 핵심 문구가 cached_prefix 에 실제 렌더됨
    assert "class-specific DP names are mandatory" in cached_prefix
    assert "hasValue" in cached_prefix
    assert "equipmentStatusValue" in cached_prefix


def test_prompt_class_prefix_naming_format_escaped():
    """{Class}.{Column} 같은 .format_map() 충돌 패턴은 이스케이프되어야 함.

    04-property-rules.md 는 _assemble_prompt 에서 template.format_map() 으로
    렌더되므로 {} 안의 식별자가 실제 변수 (industry_ko, ns_prefix) 외엔 {{}}
    로 이스케이프되어야 KeyError 방지.
    """
    prompt = _read_prompt()
    # 실제 변수만 허용 — 나머지 {x} 는 {{x}} 로 이스케이프돼야
    import re
    # 단일 중괄호 변수 패턴 추출
    singles = re.findall(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})", prompt)
    allowed_variables = {"industry_ko", "ns_prefix"}
    for name in singles:
        assert name in allowed_variables, (
            f"{{{name}}} 은 .format_map() 에서 KeyError 유발 — "
            f"{{{{{name}}}}} 로 이스케이프 필요"
        )
