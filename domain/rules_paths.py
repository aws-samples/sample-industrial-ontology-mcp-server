"""rules/ 설정 파일의 **유일한** 경로 해석기.

## 왜 필요한가

``rules/`` 는 3개월간 3개 → 26개 파일로 늘었고(20개 기능이 각자 추가), 한 폴더에
성격이 다른 세 종류가 섞였다:

- **domain/** — 도메인 자산. 새 도메인으로 옮기면 **전부 교체** 해야 한다.
- **policy/** — 엔진 정책(점수 공식 / SHACL / mutation 카탈로그). 도메인이 바뀌어도
  **유지** 한다.
- **contracts/** — 도메인-중립 계약(값 휴리스틱 / FK 패턴). 템플릿에서 **재생성** 한다.

섞여 있으면 "이 파일을 건드려도 되는가" 를 파일명으로 알 수 없다. 실측 사고
(2026-08-23): ``abstract_group_hints.json`` 의 ``child_name_patterns`` 는
``"Monitoring"`` / ``"Energy"`` / ``"Consumption"`` 처럼 **일반 영어 단어** 라
철강 파일이 새 도메인에 남아 있으면 조용히 오염시킨다 — 가상 금융 클래스로 검증:
``RiskMonitoring ⊑ EnvironmentalMonitoring`` / ``EnergyDerivative ⊑
EnergyManagement``. 파일이 **없으면** 안전하게 no-op 하지만, 있으면 잘못된 계층을
만든다. 그 파일은 ``initialize_domain_rules`` 의 12개 목록에도 없어서 도메인 이식
시 아무도 교체하지 않는다.

## 왜 해석기를 하나 두는가

경로를 만드는 지점이 코드 전체에 **129곳** 이고, 모듈마다 자기 ``_RULES_DIR`` 을
따로 계산했다 (실측: 정의 10곳 이상). 파일을 하위 폴더로 옮기면서 129곳을 손으로
고치면 한 곳만 놓쳐도 조용한 ``FileNotFoundError`` → 빈 dict 폴백 → **게이트가
꺼진 채 통과** 한다 (이 리포에서 반복된 실패 모드).

그래서 **파일 이름만 주면 경로를 찾아주는** 함수 하나로 단일화한다. 카테고리는
:data:`_CATEGORY` 표가 안다. 호출부는 하위 폴더 구조를 몰라도 된다.

## 하위 호환

``rules/`` 루트에 파일이 남아 있으면 그것을 먼저 쓴다 (마이그레이션 중이거나
사용자가 예전 위치에 둔 경우). 새 위치 → 루트 순으로 찾고, 둘 다 없으면 **새
위치 경로를 반환** 한다 — 호출부의 ``os.path.exists`` 검사와 "없으면 빈 값" 폴백이
그대로 동작하고, 생성하는 쪽은 새 구조에 쓴다.
"""
from __future__ import annotations

import glob
import os

#: 프로젝트 루트 (이 파일은 <root>/domain/rules_paths.py).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: ``rules/`` 루트. ``RULES_ROOT`` 환경변수로 오버라이드 가능 (테스트/멀티 도메인).
RULES_ROOT = os.getenv("RULES_ROOT") or os.path.join(_PROJECT_ROOT, "rules")

#: 카테고리별 하위 디렉터리 이름.
DOMAIN_DIR = "domain"
POLICY_DIR = "policy"
CONTRACT_DIR = "contracts"

#: 파일 → 카테고리. **이 표가 "새 도메인에서 무엇을 교체해야 하는가" 의 정본이다.**
#:
#: - ``domain``: 도메인 자산. 이식 시 전부 교체. 철강 잔재가 남으면 오염된다.
#: - ``policy``: 엔진 정책. 도메인과 무관하므로 유지. 바꾸면 판정 기준이 바뀐다
#:   (지표 매수 위험 — 근거 없이 손대지 말 것).
#: - ``contract``: 도메인-중립 계약. 구조는 같고 값만 도메인에 맞게 재생성.
_CATEGORY: dict[str, str] = {
    # ── 도메인 자산 (이식 시 교체) ──────────────────────────
    "domain_config.json": DOMAIN_DIR,
    "table_class_mapping.json": DOMAIN_DIR,
    "table_labels.json": DOMAIN_DIR,
    "design_patterns.json": DOMAIN_DIR,
    "disjoint_groups.json": DOMAIN_DIR,
    "abstract_group_hints.json": DOMAIN_DIR,
    "ontoclean_labels.json": DOMAIN_DIR,
    "property_chains.json": DOMAIN_DIR,
    "tacit_rules.json": DOMAIN_DIR,
    "entailment_golden.json": DOMAIN_DIR,
    "anomaly_hints.json": DOMAIN_DIR,
    "tbox_manual_additions.ttl": DOMAIN_DIR,
    # ── 도메인 자산 (선택) — 이 리포에는 없다. 있으면 도메인 전용이므로 교체 대상.
    #    ``REPLACE_ON_NEW_DOMAIN`` 에는 넣지 않는다 (부재가 정상이라 경고가 소음이 된다).
    "column_dictionary.json": DOMAIN_DIR,
    "code_meanings.json": DOMAIN_DIR,
    "canonical_hints.json": DOMAIN_DIR,
    "korean_synonyms.json": DOMAIN_DIR,
    "ddl_schema.json": DOMAIN_DIR,
    "std_item_metadata.json": DOMAIN_DIR,
    "sme_business_rules.json": DOMAIN_DIR,
    "user_term_dictionary.json": DOMAIN_DIR,
    "dimension_config.json": DOMAIN_DIR,
    "tacit_rules.suggested.json": DOMAIN_DIR,
    # ── 엔진 정책 (유지) ───────────────────────────────────
    "quality_thresholds.json": POLICY_DIR,
    "tbox_shapes.ttl": POLICY_DIR,
    "tbox_generation_config.json": POLICY_DIR,
    "op_name_hints.json": POLICY_DIR,
    # ── 도메인-중립 계약 (재생성) ───────────────────────────
    "common_dp.json": CONTRACT_DIR,
    "fk_patterns.json": CONTRACT_DIR,
    "value_heuristics.json": CONTRACT_DIR,
    "value_normalizations.json": CONTRACT_DIR,
    "value_ranges.json": CONTRACT_DIR,
}

#: 부재가 정상인 선택 파일 — 이 리포에 없고, 없으면 해당 기능이 no-op 한다.
_OPTIONAL: frozenset[str] = frozenset({
    "anomaly_hints.json",          # 빈 배열이 정상
    "column_dictionary.json", "code_meanings.json", "canonical_hints.json",
    "korean_synonyms.json", "ddl_schema.json", "std_item_metadata.json",
    "sme_business_rules.json", "user_term_dictionary.json",
    "dimension_config.json", "tacit_rules.suggested.json",
})

#: 이식 시 **반드시 교체** 해야 하는 파일 (도메인 자산 중 비어 있으면 안 되는 것).
REPLACE_ON_NEW_DOMAIN: frozenset[str] = frozenset(
    f for f, c in _CATEGORY.items()
    if c == DOMAIN_DIR and f not in _OPTIONAL
)


def category_of(filename: str) -> str:
    """파일의 카테고리 (``domain`` / ``policy`` / ``contracts``).

    표에 없는 파일은 ``domain`` 으로 본다 — 신규 파일이 도메인 자산일 확률이
    높고, 오분류의 대가가 비대칭이다: 도메인 자산을 policy 로 두면 이식 시
    교체 대상에서 빠져 **조용히 오염** 되지만, 반대는 불필요한 교체 안내뿐이다.
    """
    return _CATEGORY.get(os.path.basename(filename), DOMAIN_DIR)


def rules_path(filename: str, base: str | None = None) -> str:
    """rules 설정 파일의 절대 경로. **호출부는 하위 구조를 몰라도 된다.**

    탐색 순서:

    1. 카테고리 하위 디렉터리 (``rules/domain/…`` 등) — 새 구조.
    2. ``base`` 루트 직하 — 하위 호환. 마이그레이션 중이거나, 사용자가 예전
       위치에 뒀거나, **테스트가 평평한 tmp_path 를 주입** 한 경우.
    3. 둘 다 없으면 **1번 경로를 반환**. 호출부의 ``exists`` 검사가 그대로 동작하고,
       파일을 생성하는 쪽은 새 구조에 쓴다.

    Args:
        filename: 파일 이름만 (``"domain_config.json"``). 경로가 섞여 와도
            basename 만 쓴다.
        base: rules 루트 오버라이드. 모듈이 자기 ``_RULES_DIR`` 을 들고 있으면
            그것을 넘긴다 — 테스트가 그 상수를 monkeypatch 하는 주입 지점이
            그대로 살아 있어야 하기 때문이다 (실측 20+ 테스트). 평평한 tmp_path
            를 받아도 2번 규칙이 찾아낸다. ``None`` 이면 :data:`RULES_ROOT`.
    """
    name = os.path.basename(filename)
    root = base or RULES_ROOT
    categorized = os.path.join(root, category_of(name), name)
    if os.path.exists(categorized):
        return categorized
    flat = os.path.join(root, name)
    if os.path.exists(flat):
        return flat
    return categorized


def rules_subdir(category: str) -> str:
    """카테고리 디렉터리 절대 경로 (``mutations`` / ``swrl`` 같은 것도 받는다)."""
    return os.path.join(RULES_ROOT, category)


def rules_json_glob(base: str | None = None) -> list[str]:
    """모든 rules JSON 파일 경로 — **하위 카테고리 폴더를 포함** 한다.

    ``glob(rules/*.json)`` 을 쓰던 호출부를 위한 것이다. 하위 폴더로 나눈 순간
    그 패턴은 **0건** 이 되는데, 두 호출부(step_22e 중복 OP 정리 / step_22f OP
    근거 게이트)가 "설정이 이 OP 이름을 언급하는가" 를 이 목록으로 판정한다.
    0건이면 "아무 설정도 이 OP 를 안 쓴다" 로 읽혀 **SME 가 선언한 OP 를 근거
    없음으로 지우거나 감점** 한다 — 이 리포에서 반복된 "게이트가 조용히 꺼졌다"
    실패 모드다.

    루트 직하도 함께 훑는다 (하위 호환). 정렬해 결정적 순서를 보장한다.
    """
    root = base or RULES_ROOT
    found = set(glob.glob(os.path.join(root, "*.json")))
    for category in (DOMAIN_DIR, POLICY_DIR, CONTRACT_DIR):
        found.update(glob.glob(os.path.join(root, category, "*.json")))
    return sorted(found)


__all__ = (
    "RULES_ROOT",
    "DOMAIN_DIR",
    "POLICY_DIR",
    "CONTRACT_DIR",
    "REPLACE_ON_NEW_DOMAIN",
    "category_of",
    "rules_path",
    "rules_subdir",
    "rules_json_glob",
)
