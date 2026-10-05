"""S2 편집 어휘의 **단일 정본** — 두 엔진의 실제 능력에서 파생한다.

S2 에는 T-Box 를 고치는 엔진이 **두 개** 있고, action 카탈로그를 광고하는
프롬프트가 **두 개** 있다:

===================== ================================================
엔진                   위치
===================== ================================================
DSL                   ``multi_agent_tbox._apply_dsl_instructions`` (15 action)
jury_fixes            ``jury_fixes._DISPATCH`` (65 키 = 별칭 포함)
===================== ================================================

===================== ================================================
프롬프트               위치
===================== ================================================
Architect DSL 카탈로그  ``multi_agent_prompts.architect_static_prefix``
Jury action 목록        ``multi_agent_prompts.jury_static_prefix``
===================== ================================================

넷이 서로를 모르면 다음이 **동시에** 성립한다 (2026-08-30 실측):

1. **광고했는데 실행 못 한다** — 프롬프트가 ``add_inverse_functional_property``
   를 카탈로그에 올리는데 DSL 은 100% 거부한다 (8런 40회 전량 폐기,
   ``multi_agent_tbox.py`` 의 ``_skip(... "IFP 는 추론기 sameAs 폭발을 유발해
   의도적으로 거부")``).
2. **한 엔진의 의도적 거부가 다른 엔진에서 무효다** — 같은 IFP 를
   ``jury_fixes._apply_property_characteristic`` 은 **적용한다**. 현재
   ``_DSL_ONLY_JURY_ACTIONS`` 가 IFP 를 DSL 로 보내서 우연히 막히고 있을 뿐,
   그 라우팅 한 줄이 바뀌면 정책이 조용히 뒤집힌다.
3. **요구는 있는데 어휘가 없다** — 리뷰어 차단 이슈의 53% (마지막 런 68건 중
   36건) 가 disjointness 지적인데 그것을 **제거**할 action 이 두 엔진 어디에도
   없다 (``remove_disjoint_classes`` 29회 전량 "이 엔진이 모르는 action 이름").

이 모듈이 하는 일은 **정보를 새로 쓰는 것이 아니라 파생시키는 것**이다. 능력의
정본은 여전히 두 엔진의 코드이고, 여기서는 그것을 읽어 (a) 프롬프트 카탈로그와
(b) "이 요구는 표현 가능한가" 판정에 같은 답을 준다. 사본을 만들면 이 모듈이
네 번째 드리프트 지점이 된다.

## 정책 축 — 왜 능력만으로는 부족한가

``POLICY_REJECTED`` 는 "엔진이 할 수 있지만 **하지 않기로 한**" action 이다. IFP
가 그 예다. 능력 집합에서 빼면 "구현이 없다" 와 구분되지 않고, 넣어두면
프롬프트가 계속 광고한다. 별도 축으로 둬야 프롬프트에서 **금지로** 표시하고,
표현가능성 판정에서 "이 요구는 정책상 거부됨" 으로 분류할 수 있다.

``SME_OWNED`` 는 "SME 가 소유해 LLM 이 편집 대상으로 삼아서는 안 되는" 자산이다.
리뷰어가 매 라운드 disjointness 를 지적하지만 그 요구의 대부분은
``rules/domain/disjoint_groups.json`` 정본을 건드리고, S3 ``step_01`` 이 config
대로 재생성한다. 즉 action 을 추가해도 LLM 이 지우고 S3 가 되살리는 왕복만
늘어난다 — 이 리포의 "수정이 다음 결함을 만든다" 의 정확한 형태다. 그래서
**액션을 주는 대신 요구 자체를 소거**한다.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

#: 엔진이 구현했지만 **정책상 거부**하는 action → 거부 이유.
#:
#: 프롬프트는 이것을 "금지" 로 표시하고, 표현가능성 판정은 "정책 거부" 로
#: 분류한다. 능력 집합에서 빼지 않는 이유: 빼면 "구현 없음" 과 구분되지 않아
#: 나중에 누군가 "구현하면 되겠네" 하고 되살린다.
POLICY_REJECTED: dict[str, str] = {
    "add_inverse_functional_property": (
        "IFP 는 추론기 sameAs 폭발을 유발한다 — OWL RL 의 prp-ifp 규칙이 같은 값을 "
        "가진 개체를 전부 동일시해 인스턴스가 병합된다. 식별자 유일성은 "
        "owl:hasKey 나 A-Box 생성 시 PK 검사로 표현하라."
    ),
}

#: 리뷰어가 발명하는 이름 → 같은 일을 하는 **실행 가능한** action.
#:
#: 8런 실측: 차단성 이슈 394건 중 95건(24%)이 존재하지 않는 action 을 제안했고,
#: 그 대부분은 **어휘 부재가 아니라 이름 불일치**였다 (``remove_op`` /
#: ``delete_object_property`` → ``remove_object_property``). 이름을 매핑하면 그
#: 지적이 비로소 실행된다.
#:
#: ``jury_fixes._DISPATCH`` 도 62개 표기 변종을 흡수하지만 그것은 **jury 경로
#: 한정**이고 표기 변형(camelCase 등)만 다룬다. 여기는 **의미가 같은 다른 단어**를
#: 다루며 표현가능성 판정에도 쓰인다.
#:
#: 매핑하지 **않는** 것: ``verify_*`` 계열(15건)은 "확인해봐라" 라는 지시이지
#: 편집이 아니다. 그것을 아무 action 에 매핑하면 리뷰어의 조사 요청이 엉뚱한
#: 편집으로 번역된다 — 이름이 없는 것이 정직하다.
ACTION_ALIASES: dict[str, str] = {
    # ObjectProperty 제거
    "remove_op": "remove_object_property",
    "delete_object_property": "remove_object_property",
    "remove_dangling_property": "remove_object_property",
    "remove_objectproperty_declaration": "remove_object_property",
    # 계층
    "add_subclass_hierarchy": "add_class_hierarchy",
    "remove_redundant_subclass": "remove_triple",
    "remove_subclass": "remove_triple",
    # range/domain 교정 — 값 교체이므로 modify_triple
    "fix_range": "modify_triple",
    "fix_domain": "modify_triple",
    "fix_datatype_property_range": "modify_triple",
    "fix_datatype_property": "modify_triple",
    "fix_class_definition": "modify_triple",
    "fix_restriction": "modify_triple",
    # Restriction 정리
    "remove_duplicate_restriction": "remove_all_restrictions",
    # 중복 OP 통합 — 정본 하나를 남기는 것이므로 remove_duplicate
    "merge_redundant_op": "remove_duplicate",
    "merge_object_properties": "remove_duplicate",
    # IOF 정렬
    "add_iof_alignment": "add_iof_mappings",
    # OP 추가
    "add_missing_op": "add_object_property",
    "add_missing_cross_domain_op": "add_object_property",
    "add_cross_domain_op": "add_object_property",
    # inverseOf 선언
    "add_inverse_properties": "add_inverse_property",
    "add_inverse_of_declarations": "add_inverse_property",
    # 계층/선언 추가 (단수·복수·표현 변형)
    "add_subclass_definition": "add_subclass",
    "add_subclass_declaration": "add_subclass",
    "add_cardinality_restriction": "add_restriction",
    # 중복 OP 통합 (단수형·표현 변형)
    "merge_object_property": "remove_duplicate",
    "merge_duplicate_object_properties": "remove_duplicate",
    "remove_redundant_op": "remove_duplicate",
    # 잘못된 선언 제거 — 트리플 단위 제거
    "remove_invalid_domain": "remove_triple",
    "remove_string_literals_from_subpropertyof": "remove_triple",
    "remove_dangling_stub": "remove_class",
    # 값 교체
    "replace_owl_equivalentclass": "replace_triple",
    "fix_property_declaration": "modify_triple",
    "fix_dcterms_source_format": "modify_triple",
    # 주석/라벨
    "add_annotation": "add_triple",
    # "그대로 둬라" 지시 — jury_fixes 의 keep_no_domain 과 같은 의도
    "keep_maintenance_flag_as_dp": "keep_no_domain",
}

#: SME 가 소유하는 자산 → 그 자산을 건드리는 요구를 어떻게 처리할지 안내.
#:
#: 리뷰어가 이것을 편집 대상으로 지적하면 표현가능성 판정이 "불가침" 으로
#: 분류해 차단 이슈에서 제외한다. **이슈를 지우지 않는다** — 분류만 한다
#: (침묵시키면 정말 config 가 틀렸을 때 아무도 모른다).
SME_OWNED: dict[str, str] = {
    "disjoint_groups": (
        "AllDisjointClasses 그룹은 rules/domain/disjoint_groups.json 이 정본이고 "
        "S3 step_01 이 그 config 대로 재생성한다. LLM 이 지워도 되살아나므로 "
        "T-Box 편집으로는 해결되지 않는다 — 그룹 구성이 틀렸다면 config 를 "
        "SME 가 고쳐야 한다."
    ),
}

#: ``SME_OWNED`` 판정에 쓰는 이슈 텍스트 신호. 축(axis)별로 소유 자산에 매핑.
_SME_OWNED_SIGNALS: dict[str, tuple[str, ...]] = {
    "disjoint_groups": (
        "alldisjoint", "disjoint", "서로소", "배타",
    ),
}


@lru_cache(maxsize=1)
def dsl_actions() -> frozenset[str]:
    """``_apply_dsl_instructions`` 가 실제로 처리하는 action 이름.

    소스를 파싱해 얻는다 — 목록을 손으로 적으면 분기를 추가할 때 빠뜨리고, 그
    누락이 "광고했는데 실행 안 됨" 으로 나타난다 (이 모듈이 막으려는 결함).
    """
    import tools.multi_agent_tbox as mat

    try:
        import inspect
        source = inspect.getsource(mat._apply_dsl_instructions)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DSL action 추출 실패 — 빈 집합 반환: %s", exc)
        return frozenset()
    found = set(re.findall(r'action == "([a-zA-Z_]+)"', source))
    for group in re.findall(r"action in \(([^)]*)\)", source):
        found |= set(re.findall(r'"([a-zA-Z_]+)"', group))
    return frozenset(found)


@lru_cache(maxsize=1)
def jury_actions() -> frozenset[str]:
    """``jury_fixes._DISPATCH`` 가 아는 action 이름 (표기 변종 포함)."""
    try:
        from tools.jury_fixes import _DISPATCH
        return frozenset(_DISPATCH)
    except Exception as exc:  # noqa: BLE001
        logger.warning("jury action 추출 실패 — 빈 집합 반환: %s", exc)
        return frozenset()


#: 프롬프트에 **노출하지 않는** 표기 변종. 같은 핸들러로 가는 다른 철자다.
#:
#: ``jury_fixes._DISPATCH`` 는 LLM 이 발명하는 철자를 흡수하려고 62개 변종을
#: 등록해 두었다. 그것은 **관용**이지 어휘가 아니므로 프롬프트가 광고하면 LLM 이
#: 변종을 골라 쓰고, 그 변종은 DSL 엔진에서 미지원이 된다. 정본 하나만 노출한다.
#:
#: 판정 기준: 같은 핸들러를 가리키는 이름들 중 밑줄로 단어가 분리된 가장 긴 것을
#: 정본으로 본다 (``add_datatype_property`` > ``add_datatypeproperty``).
def _is_spelling_variant(name: str, canonical_pool: set[str]) -> bool:
    """``name`` 이 pool 안 다른 이름의 철자 변종인가."""
    squashed = name.replace("_", "")
    for other in canonical_pool:
        if other == name:
            continue
        if other.replace("_", "") == squashed and other.count("_") > name.count("_"):
            return True
    return False


@lru_cache(maxsize=1)
def canonical_action_names() -> tuple[str, ...]:
    """프롬프트에 노출할 **정본** action 이름 (변종·정책거부 제외, 정렬됨)."""
    pool = set(dsl_actions() | jury_actions())
    out = [
        n for n in pool
        if n == n.lower()
        and n not in POLICY_REJECTED
        and not _is_spelling_variant(n, pool)
    ]
    return tuple(sorted(out))


@lru_cache(maxsize=1)
def executable_actions() -> frozenset[str]:
    """어느 엔진이든 **실제로 실행하는** action (정책 거부 제외).

    프롬프트가 광고해도 되는 집합이자, 표현가능성 판정의 기준이다.
    """
    return frozenset(
        (dsl_actions() | jury_actions()) - set(POLICY_REJECTED)
    )


def engines_for(action: str) -> tuple[str, ...]:
    """이 action 을 실행할 수 있는 엔진 이름들."""
    out = []
    if action in dsl_actions():
        out.append("dsl")
    if action in jury_actions():
        out.append("jury_fixes")
    return tuple(out)


def policy_conflicts() -> list[dict]:
    """**정책 거부가 다른 엔진에서 무효인** action 목록.

    한 엔진이 의도적으로 거부하는데 다른 엔진이 같은 action 을 적용하면, 라우팅
    한 줄이 바뀔 때 정책이 조용히 뒤집힌다. 라우팅의 우연에 의존하지 않도록
    이것을 드러낸다 (2026-08-30 실측: IFP 가 그 상태였다).
    """
    conflicts = []
    for action, reason in POLICY_REJECTED.items():
        engines = engines_for(action)
        if len(engines) > 1 or (engines and "jury_fixes" in engines):
            conflicts.append({
                "action": action,
                "reason": reason,
                "engines_that_would_apply": list(engines),
            })
    return conflicts


def sme_owned_axis(text: str) -> str | None:
    """이슈 텍스트가 SME 소유 자산을 건드리는가 → 자산 키 또는 None."""
    lowered = (text or "").lower()
    for axis, signals in _SME_OWNED_SIGNALS.items():
        if any(sig in lowered for sig in signals):
            return axis
    return None


def classify_requirement(issue: dict) -> dict:
    """리뷰어 이슈 하나가 **현재 어휘로 실행 가능한가** 판정한다.

    Returns 딕셔너리의 ``verdict``:

    ``executable``
        제안된 action 을 어느 엔진이 실행한다.
    ``policy_rejected``
        엔진이 구현했지만 정책상 거부한다 (IFP 등).
    ``sme_owned``
        SME 소유 자산을 건드린다 — T-Box 편집으로 해결되지 않는다.
    ``inexpressible``
        어느 엔진도 모르는 action 을 제안했다.
    ``unspecified``
        action 제안이 없다 — 자연어 지적만. Architect 가 번역해야 하므로
        실행 가능 여부를 여기서 판정할 수 없다 (차단 판정에서 제외하지 않는다).

    **이슈를 지우지 않는다** — 분류만 반환한다. 침묵시키면 정말 고쳐야 할 것을
    아무도 모른다 (이 리포의 ``_blocking_issues`` 와 같은 원칙).
    """
    if not isinstance(issue, dict):
        return {"verdict": "unspecified", "action": None, "detail": ""}

    text_parts = [
        str(issue.get(key) or "")
        for key in ("target", "summary", "description", "issue", "fix")
    ]
    text = " ".join(text_parts)

    axis = sme_owned_axis(text)
    if axis:
        return {
            "verdict": "sme_owned",
            "action": _proposed_action(issue),
            "axis": axis,
            "detail": SME_OWNED[axis],
        }

    action = _proposed_action(issue)
    if not action:
        return {"verdict": "unspecified", "action": None, "detail": ""}
    if action in POLICY_REJECTED:
        return {
            "verdict": "policy_rejected",
            "action": action,
            "detail": POLICY_REJECTED[action],
        }
    canonical = ACTION_ALIASES.get(action, action)
    if canonical in POLICY_REJECTED:
        return {
            "verdict": "policy_rejected",
            "action": action,
            "canonical": canonical,
            "detail": POLICY_REJECTED[canonical],
        }
    if canonical in executable_actions():
        result = {
            "verdict": "executable",
            "action": action,
            "engines": list(engines_for(canonical)),
            "detail": "",
        }
        if canonical != action:
            result["canonical"] = canonical
            result["detail"] = (
                f"'{action}' 는 '{canonical}' 의 별칭으로 해석됐다"
            )
        return result
    return {
        "verdict": "inexpressible",
        "action": action,
        "detail": (
            f"'{action}' 를 실행하는 엔진이 없다 — 이 지적은 현재 어휘로 "
            f"고칠 수 없다. 실행 가능한 action 으로 다시 제안하거나 "
            f"엔진에 핸들러를 추가해야 한다."
        ),
    }


#: 이슈의 ``fix`` 가 자연어일 때 그 안에서 action 이름을 찾는 정규식.
#: LLM 은 ``fix: "add_disjoint_classes 로 ..."`` 처럼 이름을 문장에 섞어 넣는다.
_ACTION_NAME_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+){1,4})\b")


def _proposed_action(issue: dict) -> str | None:
    """이슈가 제안하는 action 이름 (없으면 None).

    구조화 필드(``action``)를 먼저 보고, 없으면 ``fix`` 자연어에서 **알려진
    action 이름 또는 그 형태의 토큰** 을 찾는다. 후자를 보는 이유: 리뷰어가
    ``remove_disjoint_classes`` 같은 **존재하지 않는** 이름을 제안하는 것이
    핵심 신호이므로, 알려진 이름만 찾으면 그 신호를 놓친다.
    """
    direct = issue.get("action")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    fix = str(issue.get("fix") or "")
    if not fix:
        return None
    known = executable_actions() | set(POLICY_REJECTED) | set(ACTION_ALIASES)
    candidates = _ACTION_NAME_RE.findall(fix)
    # 알려진 이름이 있으면 그것을 우선 (자연어에 잡음 토큰이 섞인다).
    for token in candidates:
        if token in known:
            return token
    # 없으면 action 처럼 보이는 첫 토큰 — 미지원 이름을 드러내기 위한 경로.
    for token in candidates:
        if token.split("_")[0] in (
            "add", "remove", "delete", "modify", "replace", "rename",
            "merge", "split", "fix", "keep", "introduce", "deduplicate",
            "link", "verify", "validate", "run",
        ):
            return token
    return None


def catalog_for_prompt(*, style: str = "dsl") -> str:
    """프롬프트에 넣을 action 카탈로그 텍스트를 **능력에서 생성**한다.

    Args:
        style: ``"dsl"`` (Architect) 또는 ``"jury"`` (Jury). 두 프롬프트가
            시그니처 표기를 달리 쓰므로 (Architect ``name``/``target_class`` vs
            Jury ``property``/``class``) 이름 목록만 생성하고 시그니처는 각
            프롬프트가 유지한다 — 표기를 통합하면 두 핸들러를 동시에 고쳐야 한다.

    금지 목록을 **함께** 낸다. 이것이 이 함수의 핵심이다: 이전에는 프롬프트가
    ``add_inverse_functional_property`` 를 지원 목록에 올리고 엔진이 조용히
    버렸다 (40회).
    """
    if style == "jury":
        names = sorted(jury_actions() - set(POLICY_REJECTED))
    else:
        names = sorted(dsl_actions() - set(POLICY_REJECTED))
    # 표기 변종(addObjectProperty 등)은 프롬프트에 노출하지 않는다 — LLM 이
    # 변종을 골라 쓰면 다른 엔진에서 미지원이 된다.
    canonical = [n for n in names if "_" in n or n.islower()]
    canonical = [n for n in canonical if n == n.lower()]

    lines = [" | ".join(f"`{n}`" for n in canonical)]
    if POLICY_REJECTED:
        lines.append("")
        lines.append("**금지 action (제안해도 폐기된다 — 이유가 있다):**")
        for action, reason in sorted(POLICY_REJECTED.items()):
            lines.append(f"- `{action}`: {reason}")
    if SME_OWNED:
        lines.append("")
        lines.append("**편집 대상이 아닌 자산 (지적해도 T-Box 수정으로 해결 불가):**")
        for _axis, detail in sorted(SME_OWNED.items()):
            lines.append(f"- {detail}")
    return "\n".join(lines)


def summarize() -> dict:
    """레지스트리 상태 요약 — 응답/로그에 실어 드리프트를 눈에 보이게 한다."""
    dsl, jury = dsl_actions(), jury_actions()
    return {
        "dsl_action_count": len(dsl),
        "jury_action_count": len(jury),
        "executable_count": len(executable_actions()),
        "dsl_only": sorted(dsl - jury),
        "jury_only_sample": sorted(jury - dsl)[:10],
        "policy_rejected": sorted(POLICY_REJECTED),
        "policy_conflicts": policy_conflicts(),
        "sme_owned_axes": sorted(SME_OWNED),
    }
