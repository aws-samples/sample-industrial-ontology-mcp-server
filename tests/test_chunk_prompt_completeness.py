"""모든 청크가 같은 규칙과 관련 CQ 를 받는다 — 프롬프트 축약 회귀 가드.

2026-08-26 실측. ``_build_chunk_prompt`` 는 ``is_first = (chunk_idx == 0)`` 으로
청크 0 만 우대했고, 그 결과 세 가지가 어긋났다.

## ① 프롬프트 모듈 4개가 청크 1~7 에서 빠졌다

``_assemble_prompt(include_all=is_first)`` 가 ``04-property-rules`` ·
``06-checklist`` 만 남기고 나머지 자리에 ``"(이전 청크에서 제공됨 — 생략)"`` 를
넣었다. **그 문구는 사실이 아니다** — 청크마다 독립 LLM 호출이라 이전 청크
프롬프트를 본 적이 없다.

    청크 0      37,681자    생략 마커 0
    청크 1~7    ~21,700자   생략 마커 4
    빠진 모듈    01-role-and-task · 02-ttl-guidelines ·
                03-class-definitions · 05-quality-axioms
    영향        테이블 34/40 (**85%**) 가 품질 공리·클래스 정의 규칙 없이 생성

## ② CQ 가 청크 0 만 받았다 (더 심각)

주석은 "token 절약" 이었다. 실측: CQ 12개가 요구하는 도메인 클래스 40개 중
**33개(83%)가 청크 1~7 에 있다.** CQ 를 만족시켜야 하는 클래스의 대부분이 요구사항을
본 적 없는 청크에서 생성됐고, 그러고 나서 리뷰어가 매 라운드 "CQ 필수 경로 누락" 을
critical 로 지적했다 (배포 3 run 승인 0건의 잔여 사유 중 최다).

## ③ 절약 근거가 두 축 모두 성립하지 않았다

* **예산**: 전체 주입 시 청크당 9,101~10,147 토큰. ``max_tokens`` 는 32,000 이라
  32% 다 — 어느 청크도 초과하지 않는다.
* **캐시**: ``template`` 은 ``cached_prefix`` 안에 들어간다. 청크 0 과 1~7 이 다른
  prefix 를 만들어 **고유 prefix 2종** 이 됐고 Bedrock prompt cache 가 한 번 더
  미스했다. ``_output_req`` (청크 0 만 @prefix 를 낸다는 지시) 도 같은 이유로
  ``variable_prompt`` 로 옮겼다 — 30자 차이 하나가 27KB prefix 전체의 캐시 키를
  바꿨다.

즉 축약은 토큰을 아끼지 못하면서 품질 규칙을 빼고 캐시를 깨뜨렸다. 도입 커밋
(59c3877, 초기 이식) 에 근거가 없다.

## 이 테스트의 방향

"모든 청크가 길다" 만 주장하면 CQ 를 전량 주입해도 통과한다. 세 축을 함께 고정한다:

* 완전성 — 생략 마커가 **0** 이고 모듈 6개가 전부 들어간다
* 관련성 — CQ 는 **그 청크와 관련된 것만** (무관한 CQ 는 유령 클래스를 유발한다)
* 캐시 — ``cached_prefix`` 가 **모든 청크에서 동일**하다
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import os

import pytest

import tools.tbox_generation as tg


@pytest.fixture(scope="module")
def chunk_prompts():
    """실제 샘플 CSV 로 전 청크 프롬프트를 빌드한다."""
    cfg_path = "rules/policy/tbox_generation_config.json"
    map_path = "rules/domain/table_class_mapping.json"
    if not (os.path.exists(cfg_path) and os.path.exists(map_path)):
        pytest.skip("도메인 설정 없음")
    with open(cfg_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    tables = []
    for path in sorted(glob.glob("data/source/rawdata/*.csv")):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                header = next(csv.reader(fh))
        except Exception:
            continue
        tables.append({"name": os.path.basename(path)[:-4], "columns": header})
    if len(tables) < 2:
        pytest.skip("CSV 샘플이 부족해 청크가 나뉘지 않는다")

    chunks = tg._adaptive_chunking(tables, cfg)
    with open(map_path, encoding="utf-8") as fh:
        tcm = json.load(fh)["table_class_mapping"]
    built = []
    for i, chunk in enumerate(chunks):
        cached, variable = tg._build_chunk_prompt(
            i, chunk, len(chunks), "", "", "", tcm,
        )
        built.append({"idx": i, "cached": cached, "variable": variable,
                      "tables": chunk})
    return built


# ── 완전성: 모든 청크가 같은 규칙을 받는다 ──────────────────────────────


def test_no_chunk_gets_the_omission_marker(chunk_prompts):
    """THE REGRESSION: "(이전 청크에서 제공됨 — 생략)" 이 어디에도 없다.

    청크마다 독립 호출이므로 그 문구는 사실이 아니었고, 실제로 규칙 4개가 빠졌다.
    """
    for p in chunk_prompts:
        both = p["cached"] + p["variable"]
        assert "이전 청크에서 제공됨" not in both, (
            f"청크 {p['idx']} 가 축약 프롬프트를 받는다"
        )


def test_all_prompt_modules_are_injected(chunk_prompts):
    """모듈 6개의 placeholder 가 하나도 남지 않았다."""
    modules = tg._load_prompt_modules()
    assert len(modules) >= 4, "프롬프트 모듈이 로드되지 않았다 (경로 문제)"
    for p in chunk_prompts:
        for key in modules:
            placeholder = f"<!-- {key}.md 내용이 여기에 삽입됨 -->"
            assert placeholder not in p["cached"], (
                f"청크 {p['idx']} 에 {key} placeholder 가 미치환 상태로 남았다"
            )


def test_assemble_prompt_ignores_include_all():
    """``include_all`` 인자가 더 이상 내용을 바꾸지 않는다.

    호출부 호환을 위해 인자는 남겼지만 동작은 통일했다. 두 값이 다른 문자열을 내면
    축약 분기가 되살아난 것이다.
    """
    assert tg._assemble_prompt(True) == tg._assemble_prompt(False)
    assert tg._assemble_prompt() == tg._assemble_prompt(True)


def test_quality_axioms_module_reaches_every_chunk(chunk_prompts):
    """빠졌던 모듈의 **실제 내용**이 전 청크에 있는가.

    placeholder 검사만으로는 부족하다 — 모듈 파일이 비어도 통과한다. 내용에서
    고유 문구를 뽑아 대조한다.
    """
    modules = tg._load_prompt_modules()
    probe_keys = [k for k in ("05-quality-axioms", "03-class-definitions",
                              "01-role-and-task", "02-ttl-guidelines")
                  if k in modules]
    assert probe_keys, "검증할 모듈이 없다"
    for key in probe_keys:
        body = modules[key].strip()
        # 모듈 본문에서 충분히 고유한 한 줄을 고른다. ``{...}`` 를 포함한 줄은
        # ``format_map`` 으로 치환되므로 원문과 일치하지 않는다 — 제외한다.
        probe = next(
            (ln.strip() for ln in body.splitlines()
             if len(ln.strip()) > 25 and "{" not in ln and "}" not in ln),
            None,
        )
        if not probe:
            continue
        for p in chunk_prompts:
            assert probe in p["cached"], (
                f"청크 {p['idx']} 에 {key} 내용이 없다: {probe[:40]!r}"
            )


def test_existential_axiom_conditions_reach_every_chunk(chunk_prompts):
    """someValuesFrom 3조건이 전 청크 프롬프트에 실리는가.

    2026-08-28 실측으로 배포 T-Box 의 근거 없는 필수참여 공리 54개 중 **53개가 S3
    유래**이고 LLM 유래는 1개임이 확인됐다 (S2 직후 산출물 대조). 즉 LLM 은 FK 가
    있는 곳에만 만들었다 — 프롬프트가 주된 원인은 아니었다.

    그래도 원래 문구("FK → someValuesFrom")는 조건 없이 읽히므로 전수·근거 요구를
    명시했다. 그 지시가 **모든 청크에 도달**하는지 고정한다 — 이 리포는 청크 85%가
    프롬프트 45%를 잃은 사고를 겪었고, 지시가 조용히 빠지면 LLM 이 조건을 모른다.
    """
    required = [
        "universal participation condition",
        "real FK column exists",
        "populated in every row",
        "time column",
        "metric gaming",
    ]
    for p in chunk_prompts:
        for phrase in required:
            assert phrase in p["cached"], (
                f"청크 {p['idx']} 에 필수참여 조건 문구가 없다: {phrase!r}"
            )


def test_axiom_metric_row_is_conditional():
    """Axiom 지표 행이 무조건 지시로 읽히지 않는가.

    "PK → FunctionalProperty, FK → someValuesFrom" 만 있으면 지표를 채우려고 근거
    없는 공리를 넣게 된다. 조건 참조가 같은 줄에 있어야 한다.
    """
    modules = tg._load_prompt_modules()
    body = modules.get("05-quality-axioms", "")
    assert body, "05-quality-axioms 모듈이 없다"
    row = next(
        (ln for ln in body.splitlines() if "someValuesFrom" in ln and "Axiom" in ln),
        None,
    )
    assert row, "Axiom 지표 행을 찾지 못했다"
    assert "conditions" in row, f"지표 행에 조건 참조가 없다: {row.strip()[:80]}"


# ── 관련성: CQ 는 관련된 것만 ───────────────────────────────────────────


def test_every_chunk_gets_competency_questions(chunk_prompts):
    """CQ 블록이 청크 0 에만 있지 않다.

    CQ 도메인 클래스의 83% 가 청크 1~7 에 있었다 — 요구사항을 못 본 청크가 그
    클래스를 만들었다.
    """
    from config import COMPETENCY_QUESTIONS_PATH
    if not os.path.exists(COMPETENCY_QUESTIONS_PATH):
        pytest.skip("CQ 파일 없음")
    with_cq = [p["idx"] for p in chunk_prompts
               if "Competency Questions" in p["variable"]]
    assert len(with_cq) > 1, f"CQ 를 받은 청크가 {with_cq} 뿐이다"


def test_cq_selection_is_filtered_not_broadcast(chunk_prompts):
    """청크마다 CQ 개수가 달라야 한다 — 전량 주입이면 전부 같다.

    무관한 CQ 를 주면 LLM 이 그 청크에 없는 클래스를 만들려 하고, 그것이 이 리포가
    반복 겪은 "유령 OP" 의 생성 경로다.
    """
    from config import COMPETENCY_QUESTIONS_PATH
    if not os.path.exists(COMPETENCY_QUESTIONS_PATH):
        pytest.skip("CQ 파일 없음")
    counts = {p["idx"]: p["variable"].count("- **CQ") for p in chunk_prompts}
    assert len(set(counts.values())) > 1, (
        f"모든 청크가 같은 수의 CQ 를 받았다 (전량 주입 의심): {counts}"
    )


def test_cq_filter_matches_by_domain_class():
    """``_cqs_touching_chunk`` 가 domains ↔ 클래스 매핑으로 고르는가."""
    cqs = [
        {"question_ko": "A 관련", "domains": ["steel:Alpha"]},
        {"question_ko": "B 관련", "domains": ["steel:Beta"]},
    ]
    tables = [{"name": "T_Alpha"}]
    tcm = {"T_Alpha": "steel:Alpha", "T_Beta": "steel:Beta"}
    got = tg._cqs_touching_chunk(cqs, tables, tcm)
    assert len(got) == 1
    assert got[0]["question_ko"] == "A 관련"


def test_cq_filter_normalises_prefix_and_underscore():
    """``steel:Foo`` / ``Foo`` / ``FOO_BAR`` 표기 차이를 흡수한다."""
    cqs = [{"question_ko": "q", "domains": ["Equipment_Master"]}]
    got = tg._cqs_touching_chunk(
        cqs, [{"name": "T"}], {"T": "steel:EquipmentMaster"},
    )
    assert len(got) == 1, "표기 차이로 CQ 를 놓쳤다"


def test_cq_without_domains_is_kept():
    """``domains`` 가 없으면 포함한다 — 판정 불가를 배제로 읽으면 요구사항이 사라진다."""
    cqs = [{"question_ko": "도메인 미지정"}]
    got = tg._cqs_touching_chunk(cqs, [{"name": "T"}], {"T": "steel:Alpha"})
    assert len(got) == 1


def test_cq_filter_keeps_all_when_mapping_unknown():
    """클래스 매핑을 모르면 전부 준다 (보수적)."""
    cqs = [{"question_ko": "q1", "domains": ["steel:Alpha"]},
           {"question_ko": "q2", "domains": ["steel:Beta"]}]
    got = tg._cqs_touching_chunk(cqs, [{"name": "Unmapped"}], {})
    assert len(got) == 2


# ── 캐시: prefix 가 모든 청크에서 같다 ──────────────────────────────────


def test_cached_prefix_is_identical_across_chunks(chunk_prompts):
    """``cached_prefix`` 가 1종이어야 Bedrock prompt cache 가 적중한다.

    예전에는 template 축약 + ``_output_req`` 두 가지가 prefix 를 갈랐다.
    """
    sigs = {hashlib.sha256(p["cached"].encode()).hexdigest() for p in chunk_prompts}
    assert len(sigs) == 1, f"cached_prefix 가 {len(sigs)}종 — 캐시 미스가 늘어난다"


def test_first_chunk_output_requirement_is_in_variable_part(chunk_prompts):
    """청크별로 갈리는 출력 지시는 ``variable_prompt`` 에 있다.

    ``cached_prefix`` 에 두면 30자 차이가 27KB prefix 전체의 캐시 키를 바꾼다.
    """
    first = next(p for p in chunk_prompts if p["idx"] == 0)
    assert "첫 번째 청크이므로" in first["variable"]
    assert "첫 번째 청크이므로" not in first["cached"]
    for p in chunk_prompts:
        if p["idx"] == 0:
            continue
        assert "첫 번째 청크이므로" not in p["variable"], (
            f"청크 {p['idx']} 가 첫 청크 지시를 받았다"
        )


def test_prompt_fits_the_token_budget(chunk_prompts):
    """전체 주입이 입력 상한을 넘지 않는가 — 축약 폐기의 전제.

    넘으면 축약이 아니라 청크 크기를 줄이는 것이 답이다.
    """
    with open("rules/policy/tbox_generation_config.json", encoding="utf-8") as fh:
        cfg = json.load(fh)
    limit = cfg["max_tokens"]
    for p in chunk_prompts:
        approx = (len(p["cached"]) + len(p["variable"])) / 3.5
        assert approx < limit * 0.6, (
            f"청크 {p['idx']} 프롬프트가 {approx:,.0f} 토큰 — 상한 {limit:,} 의 60% 초과"
        )
