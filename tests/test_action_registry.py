"""S2 편집 어휘 레지스트리 — 광고와 실행 능력이 갈리지 않는가 (Fix 4).

2026-08-30 실측. S2 에는 T-Box 를 고치는 엔진 2개와 action 카탈로그를 광고하는
프롬프트 2개가 있고 넷이 서로를 몰랐다. 결과:

1. 프롬프트가 ``add_inverse_functional_property`` 를 카탈로그에 올리는데 DSL 은
   100% 거부했다 (8런 40회 전량 폐기).
2. 같은 IFP 를 ``jury_fixes`` 는 **적용한다** — 한 엔진의 의도적 거부가 다른
   엔진에서 무효였고, ``_DSL_ONLY_JURY_ACTIONS`` 라우팅이 우연히 막고 있었다.
3. 리뷰어 차단 이슈 394건 중 24%가 존재하지 않는 action 을 제안했고 34%는 SME
   소유 config 를 건드렸다 — 절반 이상이 구조적으로 반영 불가였다.

## 이 테스트의 방향

레지스트리가 "정보를 새로 쓰는 것" 이 되면 네 번째 드리프트 지점이 된다. 그래서
**파생**을 주장한다:

* 능력은 두 엔진 코드에서 파생된다 (하드코딩 목록이 아니다)
* 프롬프트 카탈로그가 레지스트리와 일치한다
* 정책 거부는 **두 엔진 모두** 강제한다 (라우팅 우연에 의존하지 않는다)
* 분류기가 실제 이슈에서 의미 있는 신호를 낸다
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tools import action_registry as reg

DEBATE_LOG = pathlib.Path("data/generated/tbox/debate_log.json")


# ── 파생: 능력은 엔진 코드에서 온다 ─────────────────────────────────────


def test_dsl_actions_derived_from_source():
    """DSL action 을 소스에서 추출한다 — 손으로 적으면 분기 추가 시 빠뜨린다."""
    actions = reg.dsl_actions()

    assert len(actions) >= 10, f"DSL action 추출 실패로 보인다: {sorted(actions)}"
    # 실제 구현이 있는 것만 — 이 셋은 _apply_dsl_instructions 의 elif 분기다.
    for expected in ("add_object_property", "add_restriction", "rename_class"):
        assert expected in actions, f"{expected} 가 추출되지 않았다"


def test_jury_actions_come_from_dispatch():
    """jury action 은 ``_DISPATCH`` 가 권위다 (사본 금지)."""
    from tools.jury_fixes import _DISPATCH

    assert reg.jury_actions() == frozenset(_DISPATCH)


def test_executable_excludes_policy_rejected():
    """정책 거부 action 은 실행 가능 집합에 없다."""
    for action in reg.POLICY_REJECTED:
        assert action not in reg.executable_actions()


def test_canonical_names_drop_spelling_variants():
    """``add_datatypeproperty`` 같은 철자 변종은 노출하지 않는다.

    프롬프트가 변종을 광고하면 LLM 이 그것을 골라 쓰고 DSL 엔진에서 미지원이 된다.
    """
    names = set(reg.canonical_action_names())

    assert "add_datatype_property" in names
    assert "add_datatypeproperty" not in names
    assert "addObjectProperty" not in names


# ── 정책: 두 엔진 모두 강제한다 (THE REGRESSION) ────────────────────────


def test_policy_conflict_is_surfaced():
    """정책 거부가 다른 엔진에서 무효인 상태를 드러낸다.

    IFP 는 ``jury_fixes._DISPATCH`` 에 핸들러가 있으므로 conflict 로 보고돼야
    한다 — 그 사실이 보이지 않으면 라우팅이 바뀔 때 정책이 조용히 뒤집힌다.
    """
    conflicts = {c["action"] for c in reg.policy_conflicts()}

    assert "add_inverse_functional_property" in conflicts


def test_jury_engine_refuses_policy_rejected_action():
    """THE REGRESSION: jury_fixes 도 IFP 를 거부한다 (예전엔 적용했다)."""
    from tools.jury_fixes import apply_jury_fixes

    ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "steel:probeProp a owl:DatatypeProperty .\n"
    )

    result = apply_jury_fixes(
        ttl, [{"action": "add_inverse_functional_property", "property": "probeProp"}],
    )

    assert not result["applied"], "IFP 가 적용됐다 — 정책이 무력화됐다"
    assert len(result["skipped"]) == 1
    assert "정책" in str(result["skipped"][0]["reason"])
    assert "InverseFunctionalProperty" not in result["ttl"]


def test_jury_engine_still_applies_allowed_characteristic():
    """NEGATIVE 방향 — 정상 action 은 여전히 적용된다.

    정책 강제가 과잉이면 정당한 수정까지 막는다.
    """
    from tools.jury_fixes import apply_jury_fixes

    ttl = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "steel:probeProp a owl:DatatypeProperty .\n"
    )

    result = apply_jury_fixes(
        ttl, [{"action": "add_functional_property", "property": "probeProp"}],
    )

    assert len(result["applied"]) == 1
    assert "owl:FunctionalProperty" in result["ttl"]


# ── 프롬프트 일치 ───────────────────────────────────────────────────────


def test_architect_prompt_does_not_advertise_rejected_action():
    """프롬프트가 실행 못 하는 action 을 광고하지 않는다."""
    from tools.multi_agent_prompts import architect_static_prefix

    text = architect_static_prefix("steel")
    catalog = text.split("## 금지 action")[0]

    for action in reg.POLICY_REJECTED:
        assert f"`{action}`" not in catalog, (
            f"{action} 가 지원 카탈로그에 남아 있다 — 8런 40회 폐기의 원인이었다"
        )


def test_architect_prompt_states_the_prohibition():
    """금지를 **명시**한다 — 목록에서 지우기만 하면 LLM 이 다시 발명한다."""
    from tools.multi_agent_prompts import architect_static_prefix

    text = architect_static_prefix("steel")

    assert "## 금지 action" in text
    for action in reg.POLICY_REJECTED:
        assert action in text


def test_jury_prompt_action_list_is_generated():
    """Jury 목록도 레지스트리에서 생성된다 (손으로 적은 23개가 아니다)."""
    from tools.multi_agent_prompts import jury_static_prefix

    text = jury_static_prefix()
    section = text.split("## required_fixes action 이름")[1]

    for action in reg.POLICY_REJECTED:
        assert f"`{action}`" not in section.split("## 금지")[0]
    # dispatch 가 아는 정본이 노출된다.
    assert "`remove_object_property`" in section


def test_reviewer_prompts_carry_scope_guidance():
    """리뷰어가 고칠 수 없는 것을 critical 로 올리지 않게 안내한다."""
    from tools.multi_agent_prompts import sme_static_prefix, validator_static_prefix

    for text in (validator_static_prefix(), sme_static_prefix("cq", "csv", "")):
        assert "지적 범위" in text
        assert "disjoint_groups.json" in text


def test_validator_no_longer_directs_attention_at_sme_owned_axis():
    """검토 관점에서 AllDisjointClasses 축을 뺐다.

    그 항목이 리뷰어를 SME 소유 자산으로 유도해 차단 이슈 34%를 만들었다.
    """
    from tools.multi_agent_prompts import validator_static_prefix

    text = validator_static_prefix()
    scope = text.split("## 지적 범위")[0]

    assert "AllDisjointClasses**: 같은 인스턴스" not in scope


# ── 분류기 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ({"action": "add_object_property"}, "executable"),
        ({"action": "add_inverse_functional_property"}, "policy_rejected"),
        ({"action": "remove_op"}, "executable"),              # 별칭
        ({"action": "totally_made_up_action"}, "inexpressible"),
        ({"target": "X", "fix": "그냥 고쳐라"}, "unspecified"),
        ({"target": "AllDisjointClasses 축 혼재"}, "sme_owned"),
        ({"fix": "verify_fk_mapping 로 확인하라"}, "inexpressible"),
    ],
)
def test_classify_requirement(issue, expected):
    assert reg.classify_requirement(issue)["verdict"] == expected


def test_alias_resolution_is_reported():
    """별칭으로 해석했으면 그 사실을 남긴다 — 조용한 치환은 추적 불가."""
    result = reg.classify_requirement({"action": "remove_op"})

    assert result["verdict"] == "executable"
    assert result["canonical"] == "remove_object_property"
    assert "별칭" in result["detail"]


def test_verify_actions_are_not_aliased_to_edits():
    """``verify_*`` 는 조사 요청이지 편집이 아니다 — 매핑하면 오역이 된다."""
    for name in ("verify_fk_mapping", "verify_satisfiability", "verify_op_direction"):
        assert name not in reg.ACTION_ALIASES, (
            f"{name} 이 편집 action 으로 매핑됐다 — 리뷰어의 조사 요청이 "
            f"엉뚱한 편집으로 번역된다"
        )


def test_aliases_all_point_at_executable_actions():
    """별칭의 대상이 실제로 실행 가능한가 — 아니면 별칭이 무의미하다."""
    executable = reg.executable_actions()
    broken = {
        alias: target for alias, target in reg.ACTION_ALIASES.items()
        if target not in executable
    }

    assert not broken, f"실행 불가 대상을 가리키는 별칭: {broken}"


# ── 실측 대조: 배포 debate_log 에서 신호가 나오는가 ─────────────────────


def test_classifier_reduces_inexpressible_on_real_issues():
    """실제 8런 이슈에서 별칭 매핑이 표현불가를 줄이는가.

    24% → 8% 가 측정값이다. 이 검사는 절대 수치가 아니라 **분류가 의미 있는
    분포를 낸다** 는 것을 고정한다 (전부 한 버킷이면 분류기가 죽은 것이다).
    """
    if not DEBATE_LOG.exists():
        pytest.skip("debate_log 없음")

    log = json.loads(DEBATE_LOG.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for run in log.get("runs", []):
        for round_log in run.get("rounds", []):
            for who in ("validator", "sme"):
                for issue in (round_log.get(who) or {}).get("issues") or []:
                    if not isinstance(issue, dict):
                        continue
                    if issue.get("severity") not in ("critical", "high"):
                        continue
                    verdict = reg.classify_requirement(issue)["verdict"]
                    counts[verdict] = counts.get(verdict, 0) + 1

    total = sum(counts.values())
    assert total > 100, f"차단성 이슈가 너무 적다: {total}"
    # 네 버킷이 모두 등장해야 분류가 살아있다.
    for verdict in ("executable", "sme_owned", "unspecified", "inexpressible"):
        assert counts.get(verdict, 0) > 0, f"{verdict} 버킷이 비었다: {counts}"
    # 표현불가가 과반이면 별칭 매핑이 작동하지 않는 것이다.
    assert counts["inexpressible"] / total < 0.20, counts
