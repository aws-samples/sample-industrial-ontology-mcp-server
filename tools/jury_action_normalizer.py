"""LLM 이 낸 fix-action dict 를 핸들러가 읽는 정규 형태로 접는 경계 계층.

2026-08-10 S2 실행 실측: Jury 가 낸 `required_fixes` 41건 중 **37건이 폐기**됐다
(`ok=4 failed=37`). 원인은 논리가 아니라 **키 이름**이다 — Jury 는 `uri` /
`filler` / `restriction_type` 을 보내고 핸들러는 `class` / `someValuesFrom` /
`type` 을 읽는다. `jury_static_prefix` 는 action **이름만** 나열하고 필드 스키마를
전혀 문서화하지 않으므로(R28 D 에서 의도적으로 제거: 전체 카탈로그를 넣으면 Jury 가
빈 배열을 반환했다) Jury 는 키를 추측한다. 즉 프롬프트만으로는 고칠 수 없다.

**왜 핸들러 50개를 각각 고치지 않는가**: 같은 action 이름을 두 소비자가 서로 다른
키로 읽는다 — `_apply_high_level_instructions`(DSL) 는 `name`/`target_class`/
`on_property`, `jury_fixes` 는 `property`/`class`/`onProperty`. 핸들러를 고치면 두
갈래를 영구히 동기화해야 하고, 그것이 이 리포가 이미 네 번 값을 치른 사본-분기
실패 계열이다 (유령 IRI / 외래 prefix 뭉갬 / `_to_node` 리터럴 격하 / 이번 건).

## 설계 제약 — 조사에서 실측으로 확인된 함정 (바꾸면 안 된다)

1. **alias 는 action 별이다. 전역 표는 위험하다.** 세 키는 역할 의존이라
   접기 대상에서 **완전히 제외**한다:
   - `property`: 식별자(add_object_property) vs onProperty(add_restriction) vs
     **삭제 필터**(remove_all_restrictions). 후자에서 접으면 표적 삭제가
     전면 삭제로 번지면서 `ok` 로 보고된다.
   - `target`: 프로퍼티 목록(add_functional_property) vs 트리플 subject(replace_triple).
   - `range`: 클래스(OP) vs 데이터타입(DP).
   `targets` 도 마찬가지다 — `remove_annotations` 의 고유 컨테이너 키다.
2. **단방향이다.** 양방향으로 만들면 최종 라운드가 같은 리스트를 두 엔진에
   순차 적용하므로 **동일 action 이 두 번 적용**된다 (실측: `add_restriction`
   하나가 restriction 2개, `add_disjoint_classes` 하나가 AllDisjointClasses 2개).
   따라서 LLM 표기 → 핸들러 표기 방향만 접고, 이미 정규형인 dict 는 손대지 않는다.
3. **비파괴다.** 정규 키가 없을 때만 쓰고 alias 키는 남긴다. 그래서 정규형 입력은
   정규화 후에도 동일하다 (테스트로 고정).

이 모듈은 그래프를 만지지 않는다 — 키 모양만 다루는 순수 함수라, 기록된 Jury
payload 를 TTL 픽스처 없이 회귀 테스트로 쓸 수 있다.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: action 별 정규 키 ← 허용 alias. **전역 표를 만들지 말 것** (모듈 docstring 참조).
_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "add_class": {"class": ("uri", "name", "iri"), "superClass": ("subClassOf", "parent")},
    "remove_class": {"class": ("uri", "name", "iri")},
    "delete_class": {"class": ("uri", "name", "iri")},
    "add_subclass": {"class": ("child", "uri"), "superClass": ("parent", "subClassOf")},
    "modify_subclass": {"class": ("uri", "child"), "newSuperClass": ("parent", "superClass")},
    "add_object_property": {"property": ("uri", "name", "iri")},
    "add_datatype_property": {"property": ("uri", "name", "iri")},
    "add_inverse_property": {"property": ("uri", "name"), "inverseOf": ("inverse",)},
    "remove_object_property": {"property": ("uri", "name")},
    "keep_no_domain": {"property": ("uri", "name")},
    "add_functional_property": {"target": ("name", "uri")},
    "remove_duplicate": {"target": ("uri", "name")},
    "split_property": {"target": ("uri", "name")},
    "add_restriction": {
        "class": ("target_class", "uri", "on_class"),
        "onProperty": ("on_property", "property", "prop"),
        "type": ("restriction_type", "kind"),
    },
    "remove_all_restrictions": {"class": ("target_class", "uri", "name")},
    "add_triple": {"subject": ("s",), "predicate": ("p",), "object": ("o",)},
    "remove_triple": {"subject": ("s",), "predicate": ("p",), "object": ("o",)},
    "delete_triple": {"subject": ("s",), "predicate": ("p",), "object": ("o",)},
    "rename_uri": {"from": ("old", "old_uri", "from_uri"), "to": ("new", "new_uri", "to_uri")},
}

#: 어떤 action 에서도 접지 않는 키 — 특정 핸들러의 고유 컨테이너이거나 역할 의존.
#: 접으면 표적 삭제가 전면 삭제로 번지는 등 조용한 손상이 난다.
_NEVER_FOLD: frozenset[str] = frozenset({
    "changes", "hierarchy", "mappings", "members", "splits", "subjects",
    "children", "properties", "targets", "triples", "axiom_type",
    "newSuperClass", "onProperty", "from", "to",
})

#: `add_restriction` 의 제약값은 `type` 이 가리키는 키에 담겨야 한다.
#: Jury 는 `filler` / `value` 로 보낸다.
_RESTRICTION_VALUE_KEY: dict[str, str] = {
    "somevaluesfrom": "someValuesFrom",
    "allvaluesfrom": "allValuesFrom",
    "hasvalue": "hasValue",
    "min": "minCardinality",
    "mincardinality": "minCardinality",
    "max": "maxCardinality",
    "maxcardinality": "maxCardinality",
    "exact": "cardinality",
    "cardinality": "cardinality",
}

#: action 별 배치 컨테이너 키 → 단일 action N개로 전개. **action-scoped 필수** —
#: `targets` 를 전역 배치 키로 두면 `remove_annotations` 의 고유 계약이 파괴된다
#: (실측: added=[predicate,subject,value] lost=[targets] = 의미 변형).
_BATCH_KEYS: dict[str, tuple[str, ...]] = {
    "add_object_property": ("triples", "properties", "items"),
    "add_datatype_property": ("triples", "properties", "items"),
    "add_class": ("classes", "items"),
    "remove_class": ("classes", "targets", "items"),
    "delete_class": ("classes", "targets", "items"),
    "add_restriction": ("restrictions", "items"),
    "add_subclass": ("items", "pairs"),
    "add_inverse_property": ("items", "pairs"),
    "add_triple": ("triples",),
    "remove_triple": ("triples",),
    "delete_triple": ("triples",),
}

#: 배치 스칼라 항목(`targets: ["steel:Duplicate"]`)이 들어갈 정규 키.
_PRIMARY_KEY: dict[str, str] = {
    "add_class": "class", "remove_class": "class", "delete_class": "class",
    "add_object_property": "property", "add_datatype_property": "property",
    "add_functional_property": "target", "remove_duplicate": "target",
}


def canonical_action_name(raw: str) -> str:
    """``addObjectProperty`` / ``add_objectproperty`` → ``add_object_property``.

    dispatch 표에는 이런 표기 변종이 12개 등록돼 있으나 ``_SUPPORTED_ACTIONS``
    게이트(50개)가 먼저 걸러 **도달하지 못한다**. 이름을 정규화하면 그 사본들이
    필요 없어진다.
    """
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    if not name:
        return ""
    # camelCase → snake_case
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and name[i - 1] != "_":
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _fold_keys(action: dict) -> dict:
    """정규 키가 비어 있을 때만 alias 값을 복사한다 (비파괴)."""
    name = action.get("action", "")
    table = _ALIASES.get(name)
    if not table:
        return action
    folded = dict(action)
    for canonical, aliases in table.items():
        if folded.get(canonical):
            continue
        for alias in aliases:
            if alias in _NEVER_FOLD:
                continue
            value = folded.get(alias)
            if value:
                folded[canonical] = value
                break
    return folded


def _fold_restriction_value(action: dict) -> dict:
    """``{type: someValuesFrom, filler: X}`` → ``{someValuesFrom: X}``.

    핸들러는 제약 종류마다 **별개 키**를 읽는다. Jury 는 종류를 ``type`` 에 두고
    값을 ``filler``/``value`` 로 보내므로 여기서 옮겨 준다.
    """
    if action.get("action") != "add_restriction":
        return action
    rtype = str(action.get("type") or "").strip()
    if not rtype:
        return action
    target_key = _RESTRICTION_VALUE_KEY.get(
        rtype.split(":")[-1].replace("_", "").lower()
    )
    if not target_key or action.get(target_key) is not None:
        return action
    for src in ("filler", "value"):
        if action.get(src) is not None:
            out = dict(action)
            out[target_key] = action[src]
            return out
    return action


def _expand_batch(action: dict) -> list[dict]:
    """배치 컨테이너를 단일 action N개로 전개. 대상 아니면 ``[action]``."""
    name = action.get("action", "")
    for key in _BATCH_KEYS.get(name, ()):
        items = action.get(key)
        if not isinstance(items, list) or not items:
            continue
        shared = {k: v for k, v in action.items() if k != key}
        out: list[dict] = []
        for item in items:
            if isinstance(item, dict):
                out.append({**shared, **item})
            elif isinstance(item, str) and name in _PRIMARY_KEY:
                out.append({**shared, _PRIMARY_KEY[name]: item})
            else:
                # 형태를 모르면 버리지 않고 원본을 그대로 남긴다.
                return [action]
        return out
    return [action]


def normalize_jury_actions(
    required_fixes: list[Any],
) -> tuple[list[dict], list[dict]]:
    """LLM action 리스트 → (정규화된 action 리스트, 거부된 항목).

    정규형 입력은 **그대로 통과**한다 (1:1, 비파괴). 배치 형태만 1:N 으로 늘어난다.

    Returns:
        ``(normalized, rejected)``. ``rejected`` 는 ``{"action", "reason"}`` —
        dict 가 아닌 항목만 담는다. 호출부가 그 사유를 노출해야 한다 (조용히
        버리면 이번 결함이 되살아난다).
    """
    normalized: list[dict] = []
    rejected: list[dict] = []
    for raw in required_fixes or []:
        if not isinstance(raw, dict):
            rejected.append({"action": raw, "reason": "action 이 dict 가 아님"})
            continue
        action = dict(raw)
        canonical = canonical_action_name(action.get("action", ""))
        if canonical:
            action["action"] = canonical
        for item in _expand_batch(action):
            normalized.append(_fold_restriction_value(_fold_keys(item)))
    return normalized, rejected


__all__ = ("canonical_action_name", "normalize_jury_actions")
