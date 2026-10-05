"""Multi-agent T-Box debate의 프롬프트 공통 블록 빌더.

tools/multi_agent_tbox.py의 _validator_review / _sme_review / _architect_revise /
_jury_decide 에서 중복으로 생성되던 프롬프트 섹션(이전 이슈 주입, 이슈 4층 스키마,
burden of proof 등)을 한 곳에서 관리한다.

원칙:
- 함수 하나당 섹션 하나. 짧게, 순수 함수로.
- 각 함수는 프롬프트 단편(문자열)만 반환한다. f-string 보간은 호출자 쪽에서 수행.
- 테스트하기 쉬운 단위.
"""
from __future__ import annotations

import json


def previous_issues_block(previous_issues: list | None, *, include: bool,
                          admonition: str | None = None) -> str:
    """직전 라운드 이슈 JSON 블록.

    Args:
        previous_issues: 이슈 dict 목록.
        include: False면 빈 문자열 반환 (라운드 1에서 생략용).
        admonition: 하단 경고 문구. 기본값은 "신선한 눈으로 재평가"류.
    """
    if not include or not previous_issues:
        return ""
    prev_json = json.dumps(previous_issues, ensure_ascii=False, indent=2)
    warn = admonition or (
        "**위 지적은 잊고, 현재 TTL을 처음 보는 눈으로 재평가하세요.** "
        "이전과 동일한 지적을 하더라도 좋지만, **새로운 관점의 이슈를 우선 찾으세요**. "
        "수정 여부만 확인하는 '관성 검토'를 피하세요."
    )
    return f"""
## 이전 라운드 이슈 (참고용)
{prev_json}

{warn}
"""


def validator_issues_block(validator_issues: list | None) -> str:
    """Validator가 지적한 이슈를 SME 프롬프트에 주입 (P8)."""
    if not validator_issues:
        return ""
    v_json = json.dumps(validator_issues, ensure_ascii=False, indent=2)
    return f"""
## Semantic Validator가 방금 지적한 이슈 (현장 관점에서 반박/보강 가능)
{v_json}

**당신의 역할**:
- 위 지적 중 현장 관점에서 **동의**하면 같은 `target`에 scenario를 추가해 보강 이슈로 제출
- **반박**하면 category="relevance"로 "Validator 지적이 현장에서는 부적합한 이유" 이슈 등록
- **무관**하면 자신만의 새 이슈에 집중 (겹치는 주제 피하기)
"""


def burden_of_proof_block(role: str, round_num: int) -> str:
    """비평가 역할의 증명 책무 블록.

    Args:
        role: "validator" | "sme"
        round_num: 현재 라운드 (1-indexed).
    """
    if role == "validator":
        if round_num == 1:
            return """
## 증명 책무 (Burden of Proof)
완벽한 T-Box는 존재하지 않습니다. 당신은 **비평가**이므로:
- 최소 **CRITICAL 1건 + HIGH 2건 이상** 의 이슈를 반드시 찾아야 합니다.
- "문제 없음 / approved=true"는 **3라운드 이후에만 허용**됩니다.
- medium/low만 나열하면 비평으로 인정되지 않습니다.
"""
        return """
## 증명 책무 (Burden of Proof)
- approved=true 선언 전에 **잔존 critical/high가 0건인지 재확인**하세요.
- 자신의 이전 판단에 anchor되지 말고, 새 관점이 떠오르면 반드시 이슈화하세요.
- sycophancy(비위 맞춤)를 경계하세요 — 무난함은 품질이 아닙니다.
"""
    # role == "sme"
    return """
## 반례 의무 (Scenario Obligation)
각 지적에는 **"실제 운영 시나리오"** 1문장을 `scenario` 필드에 첨부하세요.
시나리오 없는 이슈는 **무효**입니다.
완벽한 T-Box는 현장에서 찾기 어렵습니다 — **approved=true는 3라운드 이후, 모든 CQ가 답변 가능할 때만** 허용.
"""


def issue_schema_block(role: str) -> str:
    """이슈 4/5층 스키마 설명 블록 (P7)."""
    if role == "validator":
        return """
## 이슈 작성 규칙 (모든 이슈 공통)
각 이슈는 다음 4층을 모두 포함해야 합니다:
1. **symptom**: 구체적인 트리플/선언 (예: "steel:hasAlarm의 rdfs:range 누락")
2. **principle**: 위배된 OWL 규칙 또는 모범사례 (예: "OWL 2 DL: ObjectProperty는 domain+range 선언 권장")
3. **impact**: 어떤 추론/쿼리에서 실제 문제가 발생하는가 (예: "인스턴스 타입 추론 시 owl:Thing으로 귀결 → 후속 SPARQL 필터 실패")
4. **fix**: Architect DSL action 제안 (예: "action=add_triple, predicate=rdfs:range")

4층 누락 이슈는 무효입니다.
"""
    # role == "sme"
    return """
## 이슈 작성 규칙
각 이슈는 다음 필드를 모두 포함:
1. **symptom**: 구체 트리플/선언/누락 컬럼
2. **principle**: 현장 데이터 흐름/공정 규칙
3. **impact**: 어떤 CQ/쿼리/의사결정에 방해가 되는가
4. **fix**: Architect DSL action 제안
5. **scenario**: 실운영 상황 1문장 (반례)

필드 누락 이슈는 무효입니다.
"""


#: S3(``improve_tbox_quality``) 후처리가 **결정적으로** 처리하는 항목.
#:
#: 각 항목은 실제 스텝 모듈과 2026-08-19 실측 결과로 뒷받침된다 (추측 매핑 금지 —
#: 없는 자동화를 있다고 알리면 리뷰어가 진짜 결함을 침묵한다):
#:
#: ==================================== =========================== ===============
#: 항목                                  스텝                        실측 (S2→S3)
#: ==================================== =========================== ===============
#: inverseOf 양방향                      step_02                     inverseOf 106쌍
#: OP dcterms:source (근거)              step_15d                    21% → 86%
#: OP domain/range 보완                  step_03, step_09b           —
#: IOF/BFO 정렬·범주 충돌                step_02b, step_12h          62 IRI, 충돌 11 제거
#: owl:Restriction 커버리지              step_13                     46개 추가
#: RR (관계 풍부도)                      step_15                     0.29 → 0.38
#: DIT (상속 깊이)                       step_12, step_14            1 → 3
#: 미선언 OP 선언                        step_15c                    40개
#: ==================================== =========================== ===============
_S3_AUTOMATED_ITEMS = (
    "inverseOf 양방향 선언",
    "ObjectProperty 의 dcterms:source (FK 근거) 역기록",
    "ObjectProperty domain/range 누락 보완",
    "IOF/BFO 정렬 및 BFO 범주 충돌 제거",
    "owl:Restriction (someValuesFrom / 카디널리티) 커버리지",
    "RR (관계 풍부도) — 크로스 도메인 OP 자동 생성",
    "DIT / NOC — 중간 추상 클래스·서브그룹 자동 생성",
    "A-Box 가 쓰지만 T-Box 에 없는 OP 선언",
)


def s3_deferred_block() -> str:
    """"S3 가 결정적으로 처리하는 항목" 을 리뷰어에게 알리는 블록.

    ## 왜 필요한가

    2026-08-19 실측 (S2 58.5분): 이슈 수가 12→14→12→14 로 **전혀 수렴하지 않았고**
    veto lock 8개 target 중 **7개가 S3 가 자동으로 하는 일** 이었다. Architect 는
    S2 단계에서 CSV FK 스캔·config 참조 같은 결정적 로직이 없어 그것을 잘 못 하고,
    리뷰어는 매 라운드 같은 지적을 반복했다 — 4라운드 × 8 LLM 호출을 태우고 실질
    개선은 1회뿐이었다.

    ## 침묵시키지 않는다

    "지적 금지" 가 아니라 **severity 하향 + ``deferred_to_s3`` 표시** 다.
    Validator 의 "DIT=1" 지적 자체는 **정확했다** (S2 산출물의 DIT 는 실제로 1).
    S3 가 고친다는 이유로 침묵시키면, S3 가 어떤 이유로 실패했을 때 아무도
    모른다 — 이 리포에서 "게이트가 조용해서 결함이 덮인" 사고가 반복됐다.
    기록은 남기고 합의만 막지 않게 한다.

    ## 무엇이 목록에 없는가 (중요)

    ``InverseFunctionalProperty`` 선언은 대응 S3 스텝이 **없다**. 실측 veto target
    8개 중 이 하나만 자동화 대상이 아니므로 리뷰어가 계속 지적해야 한다.
    """
    items = "\n".join(f"- {x}" for x in _S3_AUTOMATED_ITEMS)
    return f"""## ⚙️ S3 후처리가 결정적으로 처리하는 항목 (이 단계에서 판단 기준)

아래 항목들은 T-Box 생성 **직후 S3 (`improve_tbox_quality`)** 가 CSV FK 스캔과
config 를 근거로 **결정적으로** 보완한다. LLM 이 이 단계에서 손으로 채우려 하면
근거 없는 선언이 생기고, 매 라운드 같은 지적이 반복돼 토론이 정체된다:

{items}

**이 항목들에 대한 지시**:
1. 발견하면 **보고하라** — 침묵하지 마라. S3 가 실패했을 때 이 기록이 유일한 단서다.
2. 단, `severity` 는 **medium 이하** 로 두고 `category` 뒤에 `deferred_to_s3` 를
   함께 표기하라 (예: `"category": "metric", "deferred_to_s3": true`).
3. **이 항목만으로 `approved: false` 를 내지 마라.** S2 가 구조적으로 해결할 수
   없는 항목으로 합의를 막으면 남은 라운드가 전부 같은 결과를 반복한다.

**목록에 없는 것은 그대로 critical/high 로 지적하라.** 특히
`InverseFunctionalProperty` 선언은 S3 자동화 대상이 **아니다**.

## 🎯 이 단계(S2)에서만 판단할 수 있는 것 — 여기에 집중하라

- **클래스 경계와 의미**: 이 클래스가 도메인에서 실재하는 개체 종류인가, 테이블
  이름을 그대로 옮긴 것인가?
- **분류 축의 직교성**: 서로 다른 축(예: 데이터 성격 vs 업무 도메인)을 한
  `AllDisjointClasses` 에 섞지 않았는가? 섞으면 두 축에 동시 소속인 클래스가
  **unsatisfiable** 이 되고 A-Box 가 그 테이블의 인스턴스를 만들 수 없다
  (2026-08-19 실측: 이 오류로 6개 클래스가 unsat 이 됐고, 리뷰어 4라운드 동안
  아무도 지적하지 않았다 — 메트릭 잔소리에 집중하는 동안 놓쳤다).
- **관계의 방향과 의미**: OP 의 domain→range 가 현장 사실과 맞는가? 이름이
  방향을 거꾸로 말하고 있지 않은가?
- **CQ 답변 가능성**: 각 CQ 가 실제로 그래프 경로로 답변되는가?
- **FK 해석**: CSV FK 가 나타내는 관계를 올바른 카디널리티로 옮겼는가?
"""


def cq_data_gap_block(data_gap: list | None) -> str:
    """직전 라운드에 **CSV 데이터 부재**로 판정된 CQ 갭을 리뷰어에게 알린다.

    ## 왜 필요한가 (2026-08-26 실측)

    코드는 답변 불가 CQ 를 ``_split_cq_gaps_by_fk_evidence`` 로 ``fixable`` /
    ``data_gap`` 으로 나눈다. 후자는 **CSV 에 FK 컬럼이 없어 T-Box 수정으로 해소
    불가능**한 것이라 합의를 막지 않는다 (``multi_agent_tbox.py`` 합의 분기는
    ``cq_block_fixable`` 만 본다).

    그런데 그 판정 결과가 **리뷰어에게 전달되지 않았다.** ``cq_block_data_gap`` 은
    이 파일에 한 번도 등장하지 않았다. 배포 실행 3회 / 11라운드 실측:

        답변 불가 CQ 는 11라운드 중 **10라운드에서 전부 data_gap** 이었고
        (fixable 은 run2 R5 의 3건뿐), 그런데도 리뷰어는 매 라운드
        "CQ 필수 경로 누락" 을 critical 로 지적했다. approved=True 는 3 run
        11라운드에서 **한 번도 나오지 않았다**.

    즉 코드는 "고칠 수 없다" 고 알면서 리뷰어에게는 말하지 않았고, 리뷰어는 고칠 수
    없는 것으로 승인을 거부했다. Architect 는 존재하지 않는 해법을 찾으며 라운드를
    태웠다.

    ## 왜 직전 라운드인가

    ``_run_one_debate_round`` 는 리뷰어를 먼저 호출하고(:4047) CQ 판정을 나중에
    한다(:4119). 같은 라운드의 판정을 그 라운드 프롬프트에 넣을 수 없으므로 직전
    라운드 결과를 넘긴다. 1라운드에는 빈 문자열이 되고, 그때는 이 블록이 없어도
    ``_MIN_DEBATE_ROUNDS`` 가 합의를 막으므로 손실이 없다.

    ## 침묵시키지 않는다

    ``s3_deferred_block`` 과 같은 원칙이다 — 지적을 금지하는 것이 아니라
    **severity 를 낮추고 합의를 막지 않게** 한다. 데이터 갭은 사람이
    ``augment_csv_fk`` / tacit 으로 해소할 문제이므로 기록이 남아야 한다.
    """
    if not data_gap:
        return ""
    lines = []
    for item in data_gap[:12]:
        if isinstance(item, list | tuple) and len(item) > 1:
            lines.append(f"- **{item[0]}**: {item[1]}")
        else:
            lines.append(f"- {item}")
    more = f"\n  (외 {len(data_gap) - 12}건)" if len(data_gap) > 12 else ""
    return f"""## 🚫 CSV 데이터 부재로 T-Box 가 해결할 수 없는 CQ 갭 (직전 라운드 판정)

아래 CQ 는 답변 경로가 없지만, **원인이 CSV 에 FK 컬럼이 없는 것**이다 (코드가
`_csv_fk_pairs()` 실측으로 판정). T-Box 에 OP 를 추가해도 A-Box 가 채울 값이 없어
빈 관계가 되고, 그것으로 질의하면 **0건이 정답처럼 반환**된다.

{chr(10).join(lines)}{more}

**이 항목들에 대한 지시**:
1. **보고는 하라** — 데이터 보강(`augment_csv_fk` / tacit) 판단의 근거다.
2. `severity` 는 **medium 이하**, `category` 는 `data_gap` 으로 표기하라.
3. **이 갭으로 `approved: false` 를 내지 마라.** S2 가 해결할 수 없다.
4. 근거 없는 OP 를 만들어 메우지도 마라 — 근거율만 오르고 게이트가 무의미해진다.

**주의**: 위 목록에 **없는** CQ 갭은 CSV FK 가 존재하므로 T-Box 로 해결 가능하다.
그것은 그대로 critical/high 로 지적하라.
"""


def _reviewer_scope_block() -> str:
    """리뷰어에게 **고칠 수 없는 것을 지적하지 말라** 고 알린다.

    ## 왜 필요한가 (2026-08-30 실측, 8런 394 차단성 이슈 분류)

    ====================== ===== ==============================================
    분류                     비율   의미
    ====================== ===== ==============================================
    ``sme_owned``           34%   SME 소유 config 를 건드림 — T-Box 편집 무효
    ``inexpressible``       24%   존재하지 않는 action 제안 (별칭 매핑 후 8%)
    ``executable``          25%   실제로 실행 가능 (매핑 후 41%)
    ``unspecified``         17%   action 제안 없음
    ====================== ===== ==============================================

    즉 라운드당 지적의 절반 이상이 **구조적으로 반영될 수 없었다**. 그런데
    ``approved=false`` 는 그 지적들 때문에 고정되므로 토론이 수렴하지 못한다
    (8런 30라운드 전체 ``approved=true`` 0건).

    가장 큰 단일 원인은 이 프롬프트 자체였다: 검토 관점에 "AllDisjointClasses
    가 올바르게 분리되어 있는가" 를 명시해 리뷰어를 SME 소유 자산으로 유도했다.
    그 축을 관점에서 빼고, 지적하려면 config 를 지목하라고 알린다.

    **이슈를 금지하는 것이 아니다** — 지적 자체는 유효할 수 있으므로 남기되,
    수정 주체가 SME 임을 명시하게 한다. 그래야 차단 판정에서 분리된다.
    """
    try:
        from tools.action_registry import POLICY_REJECTED, SME_OWNED
    except Exception:  # noqa: BLE001
        return ""
    lines = [
        "## 지적 범위 — 고칠 수 없는 것은 차단 이슈로 올리지 마세요",
        "",
        "아래는 **T-Box 수정으로 해결되지 않습니다**. 문제가 보이면 지적은 하되",
        "`severity` 를 medium 이하로 두고 `fix` 에 수정 주체를 명시하세요 —",
        "critical/high 로 올리면 Architect 가 고칠 수 없는 것을 고치려 라운드를",
        "소모하고 합의가 영구히 불가능해집니다.",
        "",
    ]
    for _axis, detail in sorted(SME_OWNED.items()):
        lines.append(f"- {detail}")
    if POLICY_REJECTED:
        lines.append("")
        lines.append("아래 action 은 정책상 거부되므로 `fix` 에 제안하지 마세요:")
        for action, reason in sorted(POLICY_REJECTED.items()):
            lines.append(f"- `{action}`: {reason}")
    lines.append("")
    lines.append(
        "그리고 `fix` 의 action 이름은 **Architect 가 실행할 수 있는 것**이어야 "
        "합니다. `verify_*` / `check_*` 같은 조사 요청은 편집 action 이 아니므로 "
        "실행되지 않습니다 — 무엇을 어떻게 바꿀지로 쓰세요."
    )
    return "\n".join(lines)


def validator_static_prefix() -> str:
    """Validator 역할의 정적 프롬프트 블록.

    round/ttl/metrics/previous_issues 를 제외한 role 도입부 + 검토 관점 + 출력 형식.
    이 블록은 협업 1회 내에서 3회 반복 호출되므로 Bedrock prompt caching 대상.
    """
    return """당신은 OWL 온톨로지의 **Semantic Validator (논리 비평가)**입니다.
아래 T-Box TTL을 논리/추론 관점에서만 검토하세요. 현장 시나리오(CSV 매핑, 실운영 적합성)는 SME의 관점이므로 건드리지 마세요.

""" + issue_schema_block("validator") + """

""" + s3_deferred_block() + """

## 검토 관점 (논리 중심)
1. **OWL 논리 일관성**: domain/range 조합이 의도하지 않은 타입 추론을 만드는가?
2. **추론 가능성**: inverseOf, TransitiveProperty 등이 올바르게 정의되어 추론 시 올바른 결과를 내는가?
3. **중복성**: 동일한 의미의 클래스나 프로퍼티가 다른 이름으로 정의되어 있는가?
4. **Restriction**: someValuesFrom/hasValue/minCardinality 표현이 의도대로 추론되는가?
5. **표준 준수**: IOF/BFO subClassOf 매핑이 적절한가?

""" + _reviewer_scope_block() + """

## 출력 형식 (JSON — 모든 이슈는 4층 필드 필수)
```json
{
  "issues": [
    {
      "severity": "critical|high|medium|low",
      "category": "logic|inference|redundancy|disjoint|cardinality|standard|metric|data_gap|deferred",
      "target": "클래스명 또는 프로퍼티명",
      "symptom": "구체 트리플/선언 묘사",
      "principle": "위배된 OWL 규칙/모범사례",
      "impact": "실제 쿼리/추론에 미치는 영향",
      "fix": "Architect DSL action 제안 (action=... 형태)",
      "deferred_to_s3": false
    }
  ],
  "approved": false,
  "summary": "전체 평가 요약 1-2문장"
}
```
"""


def sme_static_prefix(cq_text: str, csv_summary: str, cross_domain_block: str) -> str:
    """SME 역할의 정적 프롬프트 블록.

    CQ/CSV 요약/크로스 도메인 블록은 협업 1회 내에서 라운드별로 안 바뀌므로 캐시 가능.
    """
    from domain.namespaces import DOMAIN_CONFIG
    return f"""당신은 **Manufacturing SME (현장 비평가)**입니다.
{DOMAIN_CONFIG['domain']['industry_ko']} 현장의 MES/ERP를 운영하는 도메인 전문가로서 T-Box를 **현장 관점에서만** 검토하세요.
OWL 논리/추론은 Validator의 관점이므로 당신은 CSV 매핑, 공정 흐름, CQ 답변 가능성, 실운영 시나리오에 집중합니다.

{issue_schema_block("sme")}

{s3_deferred_block()}

## 검토 관점 (현장 중심)
1. **데이터 매핑**: CSV 컬럼 → T-Box 프로퍼티 매핑 정합, 누락 컬럼
   - **출처 표기 (필수)**: 모든 DatatypeProperty 에 `dcterms:source "<CSV 컬럼명>"`
     이 있는지 확인. 이 표기가 A-Box 적재의 유일한 확정 근거이므로, 없으면
     `category: "mapping"`, `severity: "high"` 이슈로 등록하고 `fix` 에 해당 DP 와
     출처 컬럼을 명시하세요. 표기 문자열이 CSV 헤더와 다르면 (대소문자/밑줄/숫자
     접미사) 같은 등급으로 지적하세요.
   - **번호 계열 접힘**: `*_1` ~ `*_N` 컬럼들이 DP 하나로 합쳐졌으면 (표준항목
     사전이 계열 전체에 같은 한글명을 부여해 발생) 인덱스별 분리를 요구하세요.
2. **공정 흐름 적합성**: 실제 공정 순서/이벤트를 표현 가능한가
3. **FK 관계의 현실성**: 현장에서 실제 존재하는 연결인가
4. **CQ 답변 가능성**: 아래 CQ에 이 T-Box로 답할 수 있는가
   - **Pairwise OP 경로 규칙 (필수)**: 각 CQ의 `domains` 배열에 포함된 클래스들 사이의
     **모든 쌍(unordered pair)**이 1-hop 또는 2-hop ObjectProperty 경로로 연결되어야
     답변 가능. 예: domains=[A, B, C, D] 이면 6개 쌍(4C2) 모두 경로 필수.
     공유 허브(예: 둘 다 EquipmentMaster FK)가 있으면 2-hop 경로로 인정됩니다.
   - **경로가 없을 때 — OP 신설을 요구하기 전에 CSV 근거를 먼저 확인하세요 (필수)**:
     새 ObjectProperty 는 **CSV FK 컬럼이 뒷받침할 때만** 값이 채워집니다. A-Box
     생성기는 FK 컬럼에서만 관계 트리플을 만들기 때문에, FK 없는 관계를 선언하면
     **영구히 값 0건**으로 남고 그 관계로 질의하면 0건이 오류 없이 "정답처럼"
     반환됩니다. 실측: 이 규칙으로 만들어진 OP 229개 중 A-Box 가 실제로 쓰는 것은
     14개(6%)였고, 같은 T-Box 의 DP 는 68%가 쓰였습니다.
     그래서 경로가 없으면 다음 순서로 판단하세요:
       (a) 두 클래스를 잇는 **CSV FK 컬럼이 있는가** → 있으면 그 컬럼을 근거로
           OP 신설을 요구하고 `fix` 에 **컬럼명을 명시**하세요.
       (b) FK 는 없지만 **공유 허브를 경유하는 2-hop** 이 되는가 → 그 경로를
           `fix` 에 적고 OP 신설은 요구하지 마세요.
       (c) 둘 다 아니면 → **OP 를 신설하라고 요구하지 마세요.** 대신
           `unanswerable_cqs` 에 등록하고 `fix` 에 "CSV FK 부재 — 데이터 수집 또는
           tacit 지식 필요" 로 적으세요. 이것은 T-Box 결함이 아니라 **데이터 갭**
           이며, 빈 관계를 선언해 가리면 CQ 통과율만 올라가고 질의는 계속 0건입니다.
5. **크로스 도메인 관계 완전성**:
{cross_domain_block}

## Competency Questions (반드시 답변 가능해야 함)
{cq_text if cq_text else "(CQ 없음)"}

## CSV 테이블 요약
{csv_summary[:6000]}

## 출력 형식 (JSON — 모든 이슈 필드 필수)
```json
{{
  "issues": [
    {{
      "severity": "critical|high|medium|low",
      "category": "mapping|relevance|relationship|competency|process|scenario|data_gap|deferred",
      "target": "클래스명 또는 프로퍼티명",
      "symptom": "구체 증상",
      "principle": "현장 규칙/흐름",
      "impact": "쿼리/CQ/의사결정 영향",
      "fix": "Architect DSL action 제안",
      "deferred_to_s3": false,
      "scenario": "실운영 상황 1문장 (필수)",
      "related_cq": "CQ번호 (해당 시)"
    }}
  ],
  "unanswerable_cqs": ["답할 수 없는 CQ 번호와 이유"],
  "validator_agreement": [
    {{"v_target": "Validator 지적 target", "agree": true, "note": "왜 동의/반박하는가"}}
  ],
  "approved": false,
  "summary": "전체 평가 요약 1-2문장"
}}
```

{_reviewer_scope_block()}
"""


def architect_static_prefix(ns_prefix: str) -> str:
    """Architect 역할의 정적 프롬프트 블록.

    역할 도입부 + 수정 규칙 + DSL action 카탈로그 + 예시 — 라운드마다 동일하므로
    Bedrock prompt caching 대상. 라운드 번호, veto_block, validator/SME 지적 사항,
    현재 TTL 은 호출자 variable_prompt 에 둔다.
    """
    return f"""당신은 **Ontology Architect (작성자)**입니다.
두 비평가의 챌린지를 반영하여 T-Box를 수정합니다.

## 수정 규칙
1. critical/high 이슈는 반드시 수정
2. medium 이슈는 가능하면 수정
3. 네임스페이스 PREFIX `{ns_prefix}:` 유지
4. 비평가 이슈의 `fix` 필드에 DSL action 제안이 있으면 우선 채택
5. SME의 `validator_agreement` 정보로 반박된 Validator 지적은 채택 전 재평가

## 출력 형식 — 고수준 지시 목록 (JSON)

TTL을 직접 생성하지 마세요. **무엇을 추가/삭제할지만** 지정하세요.

지원 action (P2 확장 DSL):

### 기본
- `add_object_property`: {{name, domain, range, inverse, label_ko, fk_column}}
  - **`fk_column` 필수** — 이 관계를 채울 **CSV FK 컬럼명** (예: "Equipment_ID").
    A-Box 생성기는 FK 컬럼에서만 관계 트리플을 만들므로, FK 근거가 없는 OP 는
    영구히 값 0건으로 남고 그 관계로 질의하면 0건이 오류 없이 "정답처럼"
    반환된다. 실측: 이 근거 없이 만든 OP 229개 중 A-Box 가 쓰는 것은 14개(6%).
  - FK 컬럼이 없는 관계는 **tacit 지식**(SME 가 별도 입력) 또는 **추론 결과**로
    표현할 것이며 여기서 선언하지 않는다. 정말 필요하면
    `fk_column: "none:<이유>"` 로 근거 부재를 **명시**하라 — 그러면 후처리가
    무근거 OP 로 집계해 드러낸다 (숨기지 않는다).
- `add_datatype_property`: {{name, domain, range, label_ko, source}}
  - range는 "xsd:string", "xsd:decimal" 등
  - **`source` 필수** — 이 DP 가 유래한 CSV 컬럼명 **원문 그대로** (헤더에 적힌
    대소문자·밑줄·숫자 접미사까지 변형 없이).
    `dcterms:source` 로 기록되며 A-Box 가 컬럼↔DP 를 잇는 유일한 확정 근거다.
    누락하면 해당 컬럼 데이터가 KG 에 적재되지 않는다.
- `add_subclass`: {{child, parent}}
- `remove_class`: {{name}}
- `add_triple` / `remove_triple`: {{subject, predicate, object}}

### OWL 공리/제약
- `add_restriction`: {{target_class, on_property, type, value}}
  - type ∈ someValuesFrom | allValuesFrom | hasValue | minCardinality | maxCardinality | cardinality
- `add_disjoint_classes`: {{members: [...]}}  (AllDisjointClasses, 2개 이상)
- `add_functional_property`: {{name}}
- `add_transitive_property`: {{name}}
- `add_equivalent_class_union`: {{parent, members: [...]}}  (unionOf)

### 리팩터
- `rename_class`: {{old, new}}  (모든 subject/object/predicate 위치 치환)
- `rename_property`: {{old, new}}
- `merge_classes`: {{into, from: [list]}}  (from의 선언/참조를 into로 rename)

## 예시

```json
{{
  "instructions": [
    {{"action": "add_object_property", "name": "hasEquipment",
      "domain": "AlarmEvents", "range": "EquipmentMaster",
      "inverse": "hasAlarm", "label_ko": "설비를 가진다"}},
    {{"action": "add_restriction", "target_class": "EquipmentMaster",
      "on_property": "hasStatus", "type": "someValuesFrom", "value": "EquipmentStatus"}},
    {{"action": "add_disjoint_classes", "members": ["EquipmentMaster", "AlarmEvents"]}},
    {{"action": "add_functional_property", "name": "equipmentID"}},
    {{"action": "rename_class", "old": "Duplicate_Cls", "new": "CanonicalCls"}}
  ]
}}
```

{_policy_block()}
"""


def _jury_action_names() -> str:
    """Jury 프롬프트의 action 이름 목록 — 레지스트리 × **필드 스키마 문서화** 교집합.

    이전에는 23개를 손으로 적어 두었고, 그 목록이 ``jury_fixes._DISPATCH`` (65키)
    와 갈라져 정당한 action 을 "발명 금지" 로 막거나 정책 거부 action 을 광고할
    위험이 있었다. 능력에서 파생시켜 그 드리프트를 막는다.

    다만 **능력 전체를 광고하지 않는다**. ``tests/test_jury_prompt_dispatch_contract``
    가 고정하는 계약이 있다: 열거된 모든 action 은 아래 "필드 이름" 절에 스키마가
    있어야 한다. 없으면 Jury 가 키를 **추측** 하고, 그 추측이 실패로 이어진다
    (2026-08-19 실측: 실패 3건 전부 스키마 미문서 action 이었다).

    그래서 교집합을 쓴다: **디스패치가 알고 + 스키마가 문서화된** action.
    새 action 을 노출하려면 스키마를 먼저 문서화해야 한다 — 그 순서가 계약이다.

    표기 변종(``addObjectProperty``)도 노출하지 않는다 — LLM 이 변종을 고르면
    다른 엔진에서 미지원이 된다.
    """
    try:
        from tools.action_registry import canonical_action_names, jury_actions
    except Exception:  # noqa: BLE001
        return "(레지스트리 로드 실패 — jury_fixes._DISPATCH 참조)"
    known = jury_actions()
    documented = _documented_jury_field_schemas()
    names = [
        n for n in canonical_action_names()
        if n in known and n in documented
    ]
    # 6개씩 줄바꿈해 프롬프트 가독성 유지.
    rows = [
        " | ".join(f"`{n}`" for n in names[i:i + 6])
        for i in range(0, len(names), 6)
    ]
    return "\n".join(rows)


def _documented_jury_field_schemas() -> frozenset[str]:
    """"필드 이름" 절이 스키마를 적어 둔 action 이름.

    ``_JURY_FIELD_SCHEMA_BLOCK`` 을 파싱한다 — 목록을 따로 두면 그것이 또 하나의
    드리프트 지점이 된다 (이 모듈이 막으려는 결함).
    """
    import re

    return frozenset(re.findall(r"`([a-z][a-z0-9_]+)`", _JURY_FIELD_SCHEMA_BLOCK))


def _policy_block() -> str:
    """금지 action + SME 소유 자산 블록 — 레지스트리에서 생성.

    이 블록이 없던 동안 프롬프트가 ``add_inverse_functional_property`` 를 지원
    목록에 올리고 엔진이 조용히 버렸다 (8런 40회 전량). 광고와 실행 능력을 한
    곳에서 파생시키는 것이 :mod:`tools.action_registry` 의 존재 이유다.
    """
    try:
        from tools.action_registry import POLICY_REJECTED, SME_OWNED
    except Exception:  # noqa: BLE001 — 프롬프트가 import 실패로 깨지지 않게.
        return ""
    lines: list[str] = []
    if POLICY_REJECTED:
        lines.append("## 금지 action (제안해도 폐기된다)")
        for action, reason in sorted(POLICY_REJECTED.items()):
            lines.append(f"- `{action}`: {reason}")
    if SME_OWNED:
        lines.append("")
        lines.append("## 편집 대상이 아닌 자산")
        lines.append(
            "아래는 SME 가 소유하는 설정에서 결정론적으로 재생성된다. "
            "T-Box 를 고쳐도 다음 후처리에서 되살아나므로 **지적하지 마세요** — "
            "구성이 틀렸다고 판단되면 `issues` 의 `fix` 에 "
            "\"SME 가 config 를 검토해야 한다\" 라고만 쓰세요."
        )
        for _axis, detail in sorted(SME_OWNED.items()):
            lines.append(f"- {detail}")
    return "\n".join(lines)


#: Jury required_fixes 의 **필드 스키마** — 이 블록이 문서화의 정본이다.
#:
#: 모듈 상수로 뽑은 이유: ``_jury_action_names`` 가 이 텍스트를 파싱해 "스키마가
#: 있는 action" 만 광고한다. 목록을 따로 두면 그것이 또 하나의 드리프트 지점이
#: 되고, 이 모듈은 바로 그 드리프트를 없애려고 만든 것이다.
#:
#: 새 action 을 Jury 에게 노출하려면 **여기에 스키마를 먼저 추가**하라 — 그 순서가
#: ``tests/test_jury_prompt_dispatch_contract`` 가 고정하는 계약이다. 스키마 없이
#: 이름만 열거하면 Jury 가 키를 추측하고 그 추측이 실패로 이어진다 (2026-08-19
#: 실측: 실패 3건 전부 스키마 미문서 action).
#:
#: ``{{`` / ``}}`` 는 f-string 이 아닌 곳에서도 형태를 통일하려고 이스케이프된
#: 상태로 둔다 — 이 상수를 f-string 에 넣는 호출부가 있다.
_JURY_FIELD_SCHEMA_BLOCK = """## required_fixes 필드 이름 (자주 틀리는 것만)
아래 표기를 **그대로** 쓰세요. 다른 이름을 쓰면 그 수정은 적용되지 않습니다.
- 식별자 필드는 `uri` 가 아니라 **`property`** (프로퍼티) / **`class`** (클래스)
- `add_object_property` / `add_datatype_property`: {property, domain, range, source}
  — `source` 는 유래한 CSV 컬럼명 원문 (A-Box 적재 근거)
- `add_class`: {class, superClass} · `add_subclass`: {class, superClass}
- `add_restriction`: {class, onProperty, <제약키>} — 제약값은 **종류 이름 키에
  직접** 담습니다: `someValuesFrom` / `allValuesFrom` / `hasValue` /
  `minCardinality` / `maxCardinality` / `cardinality`. `filler` 나 `value` 는 쓰지
  않습니다. 예: {"class":"A","onProperty":"p","someValuesFrom":"B"}
- `add_inverse_property`: {property, inverseOf} · `remove_class`: {class}
- `add_triple` / `remove_triple`: {subject, predicate, object} — **한 건씩**.
  여러 건은 action 을 여러 개 만드세요 (`triples` 배열 금지).
- `add_class_hierarchy`: {hierarchy: [{parent, children:[...]}]}
- `add_disjoint_classes`: {members: [...]} — **2개 이상** 이어야 합니다
  (OWL 2 DL 요구). `classes` 나 `disjointWith` 가 아닙니다.
- `add_subclass_batch`: {parent, children:[...]}
- `add_functional_property`: {property}
- `add_iof_mappings`: {mappings: [{class, iofParent}]} — 리스트가 없으면
  설명만으로는 생성할 수 없어 버려집니다.
- `remove_object_property`: {property} · `remove_all_restrictions`: {class}
  (선택 `property` 로 범위 한정)
- `remove_annotations`: {targets: [...]}
- `remove_duplicate`: {target} — 남길 **정본** 이름을 target 에 씁니다.
- `rename_uri`: {from, to} · `split_property`: {target, splits:[...]}
- `modify_triple` / `replace_triple`: {subject, predicate, old_object, new_object}
  (`replace_triple` 은 subject 를 `target` 으로도 받습니다)
- `fix_namespace_bulk`: 추가 필드 없음 — 유령 IRI 를 전수 스캔해 복구합니다."""


def jury_static_prefix() -> str:
    """Jury 역할의 정적 프롬프트 블록.

    역할 도입부 + 판단 원칙 + 출력 형식 — 합의 후보/최종 라운드마다 동일하므로
    prompt caching 대상. 토론 경과, 메트릭, veto/CQ 블록, TTL 은 variable_prompt 에 둔다.

    **R28 D 수정**: 이전엔 action 카탈로그 시그니처 전체(~2K tokens) 를 prefix 에
    담았더니 Jury LLM 이 카탈로그에 집중하고 `decisions`/`required_fixes` 를 빈
    배열로 반환하는 회귀 발생. 카탈로그는 이름 목록만 짧게 유지하고, 빈 배열 금지
    규칙을 추가해 Jury 의 본 책무(토론 평가 + 수정 지시)를 복원.
    """
    return """당신은 온톨로지 debate의 **독립 Jury (심판)**입니다.
Architect(작성자), Validator(논리 비평), SME(현장 비평) 모두와 무관한 제3자 관점에서
현재 T-Box의 production 배포 가능 여부를 판단합니다.

## 판단 원칙
- Architect에게 유리한 판단을 하지 마세요. Architect는 자신의 작품을 과대평가할 수 있습니다.
- Validator/SME가 "approved=true"로 선언했더라도, 잔여 이슈의 심각도를 **독립적으로** 재평가하세요.
- "합의는 품질이 아닙니다." 합의됐어도 critical/high가 남아 있으면 production_ready=false로 선언.
- 반대로, 에이전트들이 서로 anchoring 되어 지적 못한 새 이슈가 보이면 `jury_issues`로 등록.

## 출력 필수 조건 (위반 시 무효 처리)
- `decisions`: Validator/SME 잔여 이슈 각각에 대한 accept/reject/partial 판단 — **빈 배열 금지**.
  이슈가 정말 없으면 production_ready=true + rationale 로 설명.
- `required_fixes`: production_ready=false 이면 **최소 1건 이상** 의 수정 action 을 제안.
  "debate 내용이 불충분하다" 류의 회피성 응답 금지 — 주어진 TTL 과 이슈로 판단.
- Jury 가 판단 못하겠으면 jury_issues 에 명확한 critical 이슈로 기록하고 production_ready=false.

## required_fixes action 이름 (목록)
""" + _jury_action_names() + """

**위 이름 외의 action 은 skipped 처리 → 실효 없음.** 새 이름 발명 금지.
자주 틀리는 alias: `replace_iri_batch` → `fix_namespace_bulk`,
`add_intermediate_classes` → `add_class_hierarchy`, `remove_resource` → `remove_class`.

""" + _policy_block() + """

""" + _JURY_FIELD_SCHEMA_BLOCK + """

**IRI 를 `<...>` 로 감싸지 마세요.** 값은 prefixed name (`steel:Foo`,
`iof-core:Bar`) 또는 bare local name 으로 쓰세요. (해석기가 브래킷을 벗기지만,
불필요한 표기는 오해의 소지를 남깁니다.)

**이미 선언된 것을 다시 요청하지 마세요.** 특히 같은 domain→range 를 잇는
ObjectProperty 가 이미 있으면 동의어를 추가하지 않습니다 — CSV FK 컬럼은 하나뿐이라
나머지는 값이 0건으로 남고, 그 관계로 질의하면 0건이 정답처럼 반환됩니다.
보이는 TTL 이 전체가 아닐 수 있으니, 없다고 단정하기 전에 제시된 메트릭을 함께 보세요.

## 출력 형식 (JSON)
```json
{
  "production_ready": false,
  "decisions": [
    {
      "issue_target": "Validator/SME가 지적한 target",
      "agent": "validator|sme",
      "decision": "accept|reject|partial",
      "rationale": "Jury의 독립 판단 근거"
    }
  ],
  "jury_issues": [
    {
      "severity": "critical|high|medium",
      "target": "클래스/프로퍼티",
      "description": "에이전트들이 놓친 문제",
      "fix": "어떤 action 이름으로 어떻게 고칠지 간단히"
    }
  ],
  "required_fixes": [
    {"action": "add_class_hierarchy",
     "hierarchy": [{"parent": "steel:XxxGroup", "children": ["steel:A", "steel:B"]}]}
  ],
  "rationale": "전체 판단 근거 2-3문장 — 주어진 TTL/이슈 기반"
}
```
"""


__all__: tuple[str, ...] = (
    "previous_issues_block",
    "validator_issues_block",
    "burden_of_proof_block",
    "issue_schema_block",
    "validator_static_prefix",
    "sme_static_prefix",
    "architect_static_prefix",
    "jury_static_prefix",
)
