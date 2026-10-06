# 워크샵 강사 가이드 (3시간 20분)

## 🎯 한 줄 약속 (5개 문서 공통)

> **"참가자는 3시간 후 (a) Knowledge Graph 의 가치를 비유 없이 1분 안에 설명할
> 수 있고, (b) Claude 가 만든 SPARQL 의 정확성을 직접 검증할 수 있고, (c) 자사
> 도메인 PoC 의 Day 1 에 무엇부터 시작할지 정확히 안다."**

## 이 가이드의 목적 — 당일 진행 대본

본 가이드는 **워크샵 당일 강사가 참조할 진행 대본 + 즉석 디버깅 레퍼런스** 입니다.

| 문서 | 언제 | 무엇 |
|------|------|------|
| **이 문서** | **당일 진행 중** | 챕터 진행 대본, 멘트, 즉석 디버깅, 시간 관리 |
| [`participant-workbook.md`](participant-workbook.md) | 당일 참가자 사용 | 실습 명령, 코드, Activity — 강사도 화면 공유 |

> **자료 일치 보장**: 워크북에 이미 있는 표/예시는 본 가이드가 **재기재하지 않고 참조**.
> 워크북을 화면 공유하며 함께 보세요.

---

## SPARQL 디버깅 리허설 (당일 핵심)

Ch5 에서 참가자가 공개 디버깅을 요청하면 강사가 **즉석 풀이** 해야 합니다.
D-7 에 본인 환경에서 미리 풀어보세요. 상세 배경:
[`docs/reference/troubleshooting.md §10`](../docs/reference/troubleshooting.md#10-sparql-디버깅--자주-만나는-3가지-패턴).

### 시나리오 1 — 프로퍼티명 mismatch (가장 흔함, 70%+)

**참가자 증상**: "Claude 한테 '설비 이름 보여줘' 했는데 0건 나옵니다"

**즉석 풀이 5단계**:
1. **Claude 가 만든 SPARQL 보기** — "방금 실행한 SPARQL 보여줘"
2. 프로퍼티명 추출 (예: `steel:equipmentName`)
3. **실제 프로퍼티 확인** — `시맨틱 딕셔너리에서 EquipmentMaster 의 DP 보여줘`
4. 차이 발견 (실제는 `steel:equipmentMasterName` — class prefix 패턴)
5. 수정 후 재실행 → PASS

**강사 멘트**: "Claude 가 자주 클래스명 prefix 를 빠뜨립니다. 시맨틱 딕셔너리가 정답."

### 시나리오 2 — FILTER 과잉 (20%)

**참가자 증상**: "FILTER 추가했는데 결과가 사라졌어요"

**즉석 풀이 4단계**:
1. FILTER 줄 **임시 주석 처리** → 결과 수 확인 (예: 전체 1240건)
2. FILTER 한 조건씩 다시 추가 → 어느 조건에서 0 으로 떨어지는지 찾기
3. **값 범위 확인** — `?value 의 min/max 값 보여줘` (cheatsheet 방법 F)
4. FILTER 조건이 실제 데이터 범위 안인지 비교

**강사 멘트**: "FILTER 가 너무 엄격하면 0건. 데이터 분포부터 봅시다."

### 시나리오 3 — OPTIONAL 누락 (10%)

**참가자 증상**: "정비 이력 있는 설비만 나오고 신규 설비는 안 나와요"

**즉석 풀이**:
1. SPARQL 의 정비 이력 패턴 확인 (`?maint steel:hasMaintenanceEquipment ?eq`. 동봉 T-Box 에서 정비 → 설비 방향 OP 이름이다)
2. 이 패턴이 **필수** 로 들어가 있으면 정비 없는 설비는 매칭 0
3. 해당 패턴을 `OPTIONAL { ... }` 로 감싸기 → 정비 없어도 결과 포함
4. `FILTER (!BOUND(?maint))` 로 "정비 없는 설비" 만 필터 가능

**강사 멘트**: "OPTIONAL 은 SQL 의 LEFT JOIN. 모든 설비 보고 싶으면 OPTIONAL 필수."

### 디버깅 시연 원칙

- **차분히**: 즉석 해결 못 해도 괜찮음. "같이 살펴봅시다" 태도가 더 교육적.
- **도구 활용**: `read_semantic_dictionary`, SPARQL `COUNT(*)` 로 데이터 먼저 확인.
- **손절 기준**: 10분 넘게 막히면 "이건 숙제로. [sparql-cheatsheet 0건 디버깅](sparql-cheatsheet.md#0건-디버깅--가장-자주-만나는-3가지) 과 [troubleshooting.md §10](../docs/reference/troubleshooting.md#10-sparql-디버깅--자주-만나는-3가지-패턴) 참조" 로 넘기기.

---

## 타임라인 (3h 20m) + 보호/축소 우선순위

> **3h 20m (200m) 운영**: 온톨로지 무경험자 대상이라 **온톨로지 기초 강의(30m)** 를 앞에
> 둔다. 이 강의가 "왜 KG" 설명을 흡수하고, 환경 점검은 맨 앞 **세션 0** 으로 둔다. 셋업
> 낙오자는 기초 강의 30m 동안 강사가 1:1 백그라운드로 처리한다.
> ("3시간" 한 줄 약속은 *학습 성과* 표현이라 그대로 유지.)
>
> **강의 슬라이드는 리포에 동봉되지 않는다.** 기초 강의의 자료는 워크북
> "온톨로지 기초 강의" 절(5묶음 표 + 복습 카드)이다. 강사는 그 절을 화면 공유해 진행하거나
> 자체 슬라이드를 쓰고, 자체 슬라이드를 쓰면 묶음별 내용이 워크북 복습 카드와 같은지 D-7 에
> 확인한다. 강사 없이 자습하는 참가자는 이 30분에 그 절을 순서대로 읽는다.
>

| 시간 | 챕터 | 형태 |
|------|------|------|
| 0:00-0:10 | **세션 0 (10m)** 환경 점검 (낙오자는 기초 강의 중 1:1) | 실습 |
| 0:10-0:40 | **기초 강의 (30m)** 온톨로지 기초: 왜 온톨로지 / Triple·T-Box·A-Box / RDBMS vs KG (CWA·OWA) / OWL 추론 / 오늘의 흐름 | 강의 (워크북 복습 카드 기반) |
| 0:40-0:45 | **Ch1 (5m)** 첫 SPARQL 핸즈온 | 실습 |
| 0:45-1:03 | **Ch2 (18m)** 데이터 + CQ (자사 CQ 사전 워밍업 포함) | 실습 |
| 1:03-1:24 | **Ch3 (21m)** 스키마 — T-Box + 시각화 + 암묵지 | 분석 + 실습 |
| **1:24-1:36** | **휴식 (12m)** | (실 8m + 챕터 전환 4m) |
| 1:36-1:54 | **Ch4 (18m)** A-Box + 추론 + 검증 | 실습 |
| 1:54-2:34 | **Ch5 (40m)** SPARQL ⭐ 핵심 (보호) | 실습 |
| 2:34-3:00 | **Ch6 (26m)** 마무리 + Capstone (보호) | 실습 |
| 3:00-3:20 | 버퍼 + Q&A (20m) | 지연 흡수 + 심화 |
| **합계** | **세션0~Ch6 180m + 버퍼 20m = 200m (= 3h 20m)** | |

**시간 부족 시 축소 우선순위** (Ch5/Ch6 절대 보호):

| 우선순위 | 축소/제거 대상 | 절약 | 영향 |
|:---:|--------------|:---:|------|
| 1순위 | Ch3-3 암묵지: 4가지 방법 표만 보여주고 데모 생략 | 2분 | 낮음 |
| 2순위 | Ch3-2 시각화: OntoQA 메트릭 생략, HTML 만 | 2분 | 낮음 |
| 3순위 | Ch2-2 CQ 수집: 그룹 공유 생략, 개인 작성 후 바로 Ch3 | 2분 | 중간 |
| 4순위 | 버퍼 20m 흡수 | 최대 20분 | 낮음 (Q&A 축소) |
| **보호** | **Ch5 SPARQL (40m)** | 0 | **축소 금지** |
| **보호** | **Ch6 Capstone + 회고 (26m)** | 0 | **축소 금지** |

---

## 모델 호출 비용 (사전 결재 가이드)

모델 비용은 **청구되는 곳이 둘**이라 나눠서 계산한다.

**(1) 이 MCP 서버가 Amazon Bedrock 을 호출하는 도구** (`.env` 의 AWS 자격 증명 계정에 청구,
모델은 `BEDROCK_MODEL_ID`). 외부 서비스(Bedrock, Neo4j, GraphDB)를 호출하는 도구는
`tools/registry.py` 의 `_OPEN_WORLD_TOOLS` 에 모여 있고, 그중 워크샵 일정에서 Bedrock 을
호출하는 것은 아래와 같다:

| 항목 | 호출 횟수 |
|------|:--:|
| S0 `generate_competency_questions` (Ch2-2 자동 생성) | 1회 × 15명 |
| S2 `generate_tbox_collaborative` (D-1 사전 실행) | 강사 1명만 1회 (내부 Multi-Agent 토론에서 Bedrock 8~12회) |
| Ch3-3 `add_tacit_from_natural_language` (미니 핸즈온) | 1회 × 15명 |
| Ch6-2a `generate_semantic_dictionary` | 영문 설명(`rdfs:comment@en`)이 없는 클래스가 T-Box 에 있으면 번역 1회 × 15명. 동봉 T-Box 는 이 조건에 해당한다 |
| Ch6-2b `update_tbox_incremental` (T-Box 수정 체험) | 1회 이상 × 15명 (변경 테이블 재생성) |

**(2) MCP 클라이언트(Claude Code)의 모델 사용.** 참가자가 Claude Code 에 보내는 모든
자연어 요청(Ch5-4b 의 NL→SPARQL 변환 포함)은 Claude Code 가 자체 모델로 처리한다. 이
비용은 이 서버의 Bedrock 호출이 아니며, 각 참가자의 Claude Code 계정·백엔드 설정에 따라
별도로 발생한다. `sparql_local` 과 `test_domain_queries` 는 모델을 호출하지 않는다.

> **이 가이드는 고정 금액을 제시하지 않습니다.** 비용은 사용 모델, 리전, 입력·출력 토큰 수에
> 따라 달라지므로 (1) 의 호출 횟수에 [Amazon Bedrock 요금 페이지](https://aws.amazon.com/bedrock/pricing/)
> 의 현재 단가를 적용해 계산하고, (2) 는 참가자가 쓰는 Claude Code 요금제나 백엔드의 단가로
> 따로 계산하세요. 사전 결재 금액은 그 계산값에 자사 기준의 여유분을 더해 정합니다. S2 T-Box
> 생성은 D-1 강사 1회 실행이라 워크샵 당일 참가자 호출에는 포함되지 않습니다. D-1 T-Box
> 생성에 역할별 모델을 지정하면 (`.env` 의 `MULTI_AGENT_MODEL_*`) 모델마다 토큰 단가가 다르므로
> S2 는 그 모델들의 단가로 따로 계산하세요.

---

## 장애 대응 빠른 표

| 장애 | 증상 | 대응 |
|------|------|------|
| MCP 서버 안 뜸 | Claude Code 도구 인식 안 됨 | `.env` 경로, venv 경로, `.mcp.json` 확인 |
| Bedrock 제한 | T-Box 호출 ThrottlingException | pre-generated T-Box 사용 |
| sparql_local 느림 | 첫 호출 10초+ | 첫 호출만 느림 (캐시), `source="merge"` 권장 |
| 도메인 질의 FAIL | CQ 연결 경로 누락 | "FAIL 은 T-Box 보강 신호. improve_tbox_quality" |
| 8GB RAM OOM | A-Box 37MB + 추론 6GB+ → 스왑 | 다른 앱 종료 + `fast_mode=True`. 안 되면 fallback |
| Java 미설치 | HermiT 에러 | OWL 추론은 강사 화면, validate_owl_consistency 스킵 |
| `.env` 변경 안 됨 | 데스크톱 앱 재시작 필요 | **Quit 후 재시작**. CLI 는 새 셸 |

---

## 세션 0: 환경 점검 (0:00-0:10, 10m)

**목표:** 기초 강의 전 환경 이슈 즉시 발견 + D-1 미회신자 1:1 처치. **막혀도 그룹은 기초 강의로 진행**하고, 낙오자는 기초 강의 30m 동안 백그라운드로 처치한다.

**진행:**
- D-1 사전 적재 회신자: `"CSV 파일 목록 보여줘"` 동시 실행 (`list_csv_tables` 40개 반환 확인) — 1분 끝
- D-1 회신이 없는 참가자: 강사 1:1 (5분 내 fallback 적재 + verify). 안 끝나면 강사 노트북 화면 공유로 우회
- 10분 차에 전원 점검 시작 상태 확인 → **기초 강의 시작 (미해결자는 강의 중 계속 1:1)**

> **D-1 회신을 못 받으면 워크샵 첫 인상이 망가집니다.** setup-guide §8 체크리스트의
> D-1 fallback 적재 + verify 회신은 당일 1:1 지원이 필요한 참가자를 미리 파악하는 용도다.
> D-3 까지 회신 없는 사람은 D-2 강사 1:1 reminder.

## 기초 강의: 온톨로지 기초 (0:10-0:40, 30m)

**목표:** 온톨로지 무경험자에게 개념 토대 제공. **노트북 불필요한 강의식** — 이 시간이
셋업 낙오자 1:1 처치의 버퍼 역할도 함.

> 슬라이드는 동봉되지 않는다 (위 타임라인 박스 참조). 아래 5묶음은 워크북 "온톨로지 기초
> 강의" 절의 표·복습 카드와 1:1 로 대응한다.

**강의 5묶음** (워크북 "온톨로지 기초 강의" 섹션이 참가자 복습 카드):
1. (5분) **왜 온톨로지인가** — 데이터 사일로 → 관계 중심. 4-table JOIN SQL 의 고통
2. (8분) **핵심 개념** — Triple / T-Box / A-Box / KG (워크북 "핵심 5단어" 표와 1:1)
3. (7분) **RDBMS vs KG** — CWA/OWA, 이메일 NULL 비유 (워크북 "RDBMS vs KG 핵심 차이" 표)
4. (5분) **OWL 추론** — "묻지 않은 답까지" (Ch4-2 에서 손으로 확인 예고)
5. (5분) **오늘의 흐름** — 파이프라인 14단계 한눈 (워크북 부록 B)

> 오프닝 멘트 (강의 묶음 1번 앞): "오늘 3시간 20분 동안 (a) KG 가 어떻게 만들어지는지,
> (b) SPARQL 로 질의, (c) 자사 적용까지. 앞 30분만 강의, 이후 ~70% 핸즈온, 비싼
> T-Box/추론만 강사 화면."

## Ch1: 첫 SPARQL 핸즈온 (0:40-0:45, 5m)

**목표:** 기초 강의의 개념을 **본인이 첫 SPARQL 한 번 실행**으로 체감.

**진행** ([워크북 Ch1](participant-workbook.md)):
- **(5분) 본인 PC 첫 SPARQL** — Claude Code 에 자연어 `모든 설비의 ID 를 5개만 보여줘` → Claude 가 만든 SPARQL 함께 보기 + 결과 확인

> 첫 핸즈온 인상이 "강의를 듣는다" 가 아니라 "AI 가 SPARQL 을 짜는 걸 본다" 가 되도록.

**예상 Q&A:**
- Q: "그냥 Neo4j 쓰면?" — A: "Graph DB 는 저장/질의 엔진, 온톨로지는 의미 체계.
  Neo4j 는 추론 못함. 우리는 RDF 로 추론 후 Neo4j LPG 로 변환."
- Q: "LLM 만 쓰면?" — A: "LLM 은 SPARQL 생성 가능하지만 T-Box 모르면 hallucinate.
  온톨로지 = LLM 이 반드시 쓸 용어집."

---

## Ch2: 데이터 + CQ (0:45-1:03, 18m)

> **순서 주의**: 본 워크샵은 "데이터 → CQ" 순서. Grüninger & Fox 는 CQ 먼저지만,
> 초보 청중에게는 데이터 먼저가 직관적.

### Ch2-1. 데이터 탐색 (0:45-0:53, 8m)

**진행:**
1. (4분) 강사 데모: `list_csv_tables`, `read_csv_schema`, `generate_csv_erd` (ERD HTML)
2. (4분) 참가자 따라하기 + `profile_csv_data`

**핵심 멘트:**
- "`data/source/rawdata/` 40개 CSV. S3 가 아닌 **로컬 파일**."
- "Equipment_ID 가 여러 테이블에 → KG 의 ObjectProperty."

### Ch2-2. CQ 수집 (0:53-1:03, 10m)

**목표:** "이 데이터로 어떤 비즈니스 질문에 답할까" 도출. **표 A (자사 사전 워밍업) / 표 B (철강) 분리 작성** 강조.

**진행:**
1. (2분) CQ 개념 + 예시 5개
2. (5분) 개인 작업:
   - **표 A (자사 도메인) 3~5개 먼저 작성** — Capstone 6-2c 슬롯 (3분) 안에 처음부터 떠올리기 어려우므로 **이 슬롯에 사전 워밍업**
   - 표 B (철강) STL-1~STL-3 (필수) 작성 → `generate_competency_questions` 자동 10개와 비교
3. (1.5분) 화이트보드에 대표 CQ 5~10개 자원자 기록
4. (1.5분) 강사 sample 워킹라운드 — 5명 무작위 점검해 표 A 칸 채워졌는지 spot-check (15명 전수 무리. 미작성자만 D+1 reminder 명단)

**핵심 멘트:** "**표 A 는 자사 — 지금 5분 안에 3개라도 적어두면 Ch6 Capstone 이 복사+다듬기로 끝납니다.** 표 B 는 철강 (Ch5 변환 대상)."

**예상 Q&A:**
- Q: "5개 너무 적은 거 아닌가요?" — A: "실무는 10~15. 워크샵은 연습. 자사에서는
  SME 와 2~3 라운드 인터뷰."

---

## Ch3: 스키마 — T-Box + 시각화 + 암묵지 (1:03-1:24, 21m)

> **D-1 사전 실행 결과 사용.** Bedrock 라이브 데모는 시간 제약상 생략.

### Ch3-1. T-Box 결과 분석 + OP 예측 (1:03-1:13, 10m)

**진행:**
1. (1분) 도입: "어제 D-1 에 만든 T-Box 결과를 함께 분석합니다."
2. (5분) **Activity 3-A** (워크북): CSV FK 화살표 → OP 후보 5개 + 이유 (페어 토론)
3. (2분) D-1 결과 (`workshop/pre-generated/t_box.ttl`): 클래스/OP/DP 수 + Activity 정답 비교
4. (2분) `improve_tbox_quality` 후처리 결과 (스텝 0~30, 함수 69개) + 5단계 검증 한 줄씩 요약

> **상세 참조**:
> - **Multi-Agent 토론** — [post-workshop §8-2 안티패턴](post-workshop-guide.md#8-2-한-번에-40개-테이블-투입) (수렴 실패 케이스) + [§FAQ](post-workshop-guide.md#10-faq--자주-하는-질문) "토론 로그 보려면"
> - **후처리 스텝 파이프라인** — [post-workshop §5 의사결정 트리](post-workshop-guide.md#5-의사결정-트리) (`improve_tbox_quality` vs 수동 편집) + [§8-1 안티패턴](post-workshop-guide.md#8-1-t-box-수동-편집-후-improve_tbox_quality-재실행)
> - **5단계 검증** — [post-workshop §3 개선 루프](post-workshop-guide.md#3-t-box-품질-개선-사이클) (구문→품질→HermiT→분류→SHACL)

**예상 Q&A:**
- Q: "LLM 이 만든 온톨로지 신뢰?" — A: "LLM 은 초안 생성기. Multi-Agent 토론 +
  후처리 스텝 파이프라인 + 5단계 검증이 품질 보장."
- Q: "HermiT?" → A: "OWL DL 추론기. 논리 모순 탐지. HermiT 단독은 Java 11+ 로 동작하지만
  Pellet 경로 (S8.5 SWRL, `validate_owl_realisation`) 는 Java 25+ 가 필요해서 setup-guide 는
  Java 25+ 를 안내합니다."

**Bedrock 장애 시:** `workshop/pre-generated/t_box.ttl` 그대로 시연.

### Ch3-2. T-Box 시각화 + 분석 (1:13-1:21, 8m)

**진행:**
1. (3분) 강사 데모 `visualize_tbox` → 브라우저 HTML
2. (5분) 참가자 실습: `analyze_tbox` → 클래스/프로퍼티 수 / `measure_tbox_metrics` → DIT, RR

**핵심 멘트:** "엣지 = ObjectProperty, 노드 = 클래스. 클릭하면 DP 우측 패널. **Ch5 에서 가장 많이 등장하는 EquipmentMaster (접속 OP 42개 + DP 5개) 중 자주 쓸 OP 3-4개 + DP 2-3개를 미리 적어두면 5-1, 5-2 SPARQL 작성 속도가 빨라집니다. 단 5-3 이상은 새 클래스 (TagMaster / AlarmEvents / MaintenanceHistory 등) 가 등장하므로 딕셔너리 추가 조회는 필요.**"

> **OntoQA 정량 메트릭 (DIT/RR/AR)**: 시간 부족 시 생략. [post-workshop §3 — 메트릭별 개선 방법](post-workshop-guide.md#메트릭별-개선-방법) 참조.

### Ch3-3. 암묵지 (1:21-1:24, 3m)

**목표:** CSV 에 없는 도메인 지식 + **4가지 입력 방법** 이해 + **(a) 자연어 손맛 1회**.

**진행** ([워크북 §3-3 암묵지](participant-workbook.md#3-3-암묵지--4가지-입력-방법-3분-본인-pc) — 본인 PC 미니 핸즈온):
1. (1분) 개념 + 운영 순서 — (a) 부터 시도, 후보 모이면 (b)/(c) 확장
2. (2분) **본인 PC 미니 핸즈온** — `add_tacit_from_natural_language` 자연어 1줄 입력
   ```
   다음 암묵지를 my_first_tacit.ttl 에 자연어로 추가해줘:
   "고로 → 제강 → 연주 → 압연 순서로 공정이 진행된다"
   ```
   → 응답에서 생성된 트리플 수 + 파일 경로만 함께 확인

> 자사 암묵지 후보 작성은 [post-workshop §"D+1 첫 숙제"](post-workshop-guide.md#d1-첫-숙제--설정-파일-실제-편집-30분) 로 흡수 (30분 슬롯에 (a) 자연어 입력으로 직접 시도). 본 챕터는 시연 + 손맛 1회로 시간 절약 우선.

| 상황 | 추천 방법 |
|------|----------|
| SME 와 함께 워크샵 | (a) 자연어 |
| 대량 반복 매핑 | (b) 규칙 |
| CSV 만 들고 옴 | (c) 부트스트랩 |
| 일단 끝내기 | (d) skip |

> **시간 부족 시 단축 옵션**: 본인 PC 미니 핸즈온 (위 step 2) 을 생략하고 강사 시연으로 대체 가능. 단, 참가자가 (a) 자연어 입력을 직접 한 번 해본 체험이 있어야 자사 PoC 에서 자기주도 적용 가능성이 크게 올라가므로, 생략한 참가자는 워크샵 후 [post-workshop §5 — 의사결정 트리](post-workshop-guide.md#5-의사결정-트리) (4가지 암묵지 입력 방법 분기) 를 본인이 따로 학습할 것을 안내.

---

## 휴식 (1:24-1:36, 12m)

> 실제 휴식 8m + 챕터 전환·환경 재점검 4m. workbook 타임테이블과 정합.

---

## Ch4: A-Box + 추론 + 검증 (1:36-1:54, 18m)

### Ch4-1. A-Box 생성 (1:36-1:41, 5m)

**진행:** **본인 PC `generate_abox` 직접** (LLM 0회, 30~50초)
- 결과 JSON 의 `fk_match_stats` 5단계 카운터 분석 (워크북 4-1)
- FK 5단계 매칭 (exact / normalized / levenshtein / prefix / unresolved)

### Ch4-2. 추론 전/후 비교 (1:41-1:47, 6m)

**진행:** D-1 사전 결과 사용 (cold start 회피)
- `sparql_local` 두 모드 비교:
  - **`source="merge"`** (추론 전): T-Box + A-Box + tacit 병합 그래프. `sparql_local` 은 이 그래프를 로드할 때 `owl:inverseOf` 역방향 트리플을 채우므로 (`load_graph()` 의 `ensure_inverse_triples()`) 역방향 쿼리도 이 모드에서 동작한다. 상위 클래스 `rdf:type` 과 IOF 상위 술어는 아직 없다.
  - **`source="inferred"`** (추론 후, 기본값): `all_inferred.ttl` 로드. OWL RL 추론이 subClassOf 전이에 따른 상위 클래스 `rdf:type`, IOF 상위 술어 등을 더한 그래프.
- 원본 병합 그래프 대비 트리플 약 1.9배 (718K → 1.38M, +92%). `merge` 모드는 역방향 트리플을 채워 로드하므로 화면의 증가 폭은 이보다 작다 (아래 ⚠️ 박스). 두 모드의 차이는 `rdf:type` 술어 개수에서 가장 크게 보인다

> 추론 cold start와 로컬 환경 편차는 D-1 사전 실행으로 확인한다. 강사가 워크샵 1일 전 추론 1회 완주 필수.

**왜 추론이 트리플을 +92% (약 1.9배) 증가시키나** (강사 답변용):

추론 전 718,279 → 후 1,376,144 (동봉 pre-generated 산출물 실측. 분모는 T-Box 5,009 +
A-Box 713,276 + tacit 51,242 의 **합집합** 이고, 중복 제거 때문에 단순 합보다 작다).
아래 분해는 근사치가 아니라 `inferred - merge` 차집합의 술어별 실측이다 (총 658,155):
- **rdf:type**: 473,569 (72.0%). 인스턴스가 상위 클래스·someValuesFrom 클래스로 back-type
- **inverseOf 짝**: 110,542 (16.8%). inverseOf 선언 51쌍 (OP 108개 중). 선언의 양방향을 모두 센 값이며 아래 ⚠️ 박스의 로드 시 채움 수와 같다
- **IOF/외래 술어**: 74,042 (11.2%). 73,751 이 `hasParticipantAtSomeTime` / `hasOutput` 등 IOF 상위 술어다. 나머지 291 중 290 은 T-Box 의 blank node 구조 (`rdf:first`/`rdf:rest`, `owl:hasKey` 등) 가 파일마다 다른 blank node 이름으로 쓰여 차집합에 잡힌 것이라 추론 파생이 아니다
- 나머지(도메인 술어): 2 (0.0%). 병합 그래프와 값이 다른 DP 트리플 2건이며 추론 규칙의 파생이 아니다

> ⚠️ **파이프라인이 보고하는 "추론 전" 은 828,821 로 위 718,279 와 다르다.** `load_graph()`
> 가 `ensure_inverse_triples()` 로 역방향 트리플 110,542건을 **미리 채우기** 때문이다.
> 그 정의로 재면 위 `inverseOf 짝` 축이 0% 가 된다 — 추론이 만든 것이 아니라 로드가
> 만든 것이 되므로. 강의에서는 "무엇을 분모로 잡았는지" 를 먼저 말해야 한다.

> `someValuesFrom` 공리는 48개다. 이전 판(109개)에서 크게 줄었고 **그것이 개선이다** —
> FK 근거 없이 "모든 인스턴스가 이 관계를 갖는다" 고 단정한 공리를 제거한 결과다. 그
> 공리들은 위반 개체를 자기가 위반하는 클래스로 back-type 시켜 `rdf:type` 파생을
> 부풀렸고, 그 클래스로 필터하는 질의가 **조용히 틀린 결과**를 냈다. 2026-09-05 에는
> 근거 없는 3개를 사후 감사(`step_13e`)로 더 걷어내 추론 restriction 위반이 607건 → 0
> 이 됐다. 트리플 수가 아니라 근거가 품질이다.

**실무 영향: "인스턴스 1개당 추론된 트리플 9.3개"**: 파생 658,155 / 인스턴스 70,701 (인스턴스 네임스페이스의 고유 IRI) 실측. SPARQL 에서 "명시 없어도 질의 가능한 관계" 의 실체.

**예상 Q&A:**
- Q: "추론 결과에 쓸모없는 게 많은데?" — A: "`classify_inference_triples` 로 meaningful/trivial/
  suspicious 3분류. trivial 은 무시, suspicious 가 많으면 T-Box domain/range 점검 신호."
- Q: "OWL RL 못 하는 규칙은?" — A: "'알람 3회 → 고장' 같은 변수 연결 규칙. SWRL 로 가능.
  `SWRL_ENABLED=true` opt-in (활성화 4단계 + 규칙 작성 가이드: [`rules/swrl/README.md`](../rules/swrl/README.md))."

### Ch4-3. KG 검증 (1:47-1:54, 7m)

**진행** ([워크북 §4-3 KG 검증](participant-workbook.md#4-3-kg-검증-7분-본인-pc) — 본인 PC 병합 그래프 검증):
1. (1분) 25 check 5그룹 표 안내 (Structural 4 / Referential 7 / Semantic 5 / Statistical 4 / Cardinality 5)
2. (4-5분) **본인 PC 직접** `validate_kg(use_inferred=False)` (40-90초) — 워크북에 적힌 자연어 프롬프트 그대로 실행
3. (1~2분) 결과 해석 — PASS 개수 / FAIL 그룹명 / critical 위반만

> **자기 KG 의 PASS/FAIL 개수를 본인 손으로 확인** = 자신감 핵심. 추론 그래프 검증 (`use_inferred=True`, 5~8분) 은 D+1 숙제로.
> 병합 그래프 검증이 90초 넘게 걸리면 강사 D-1 결과로 fallback.

**핵심 멘트:**
- "철강 샘플은 **23/25 PASS (FAIL 2)** 이다 (2026-09-06 pre-generated 실측, `use_inferred=False`). 자사 첫 실행은 **18/25 이상이면 양호** — critical 3종 (functional/disjoint/subclass_cycle) 만 반드시 PASS."
- **FAIL 2건은 의도적으로 남긴 실제 갭이다** (강사 답변용): **고아 노드 31건** (`EnergySourceMaster` 대부분 — 카탈로그성 마스터라 참조되지 않는 항목이 정상적으로 있다) / **클래스별 인스턴스 수** (무인스턴스 클래스 48개 = 18.0%, 그중 다수는 값 기반 서브클래스라 추론으로만 채워진다). "샘플이니 전부 초록" 이 아니라 **게이트가 실제로 발화하는 상태**를 보여주는 것이 교육 목적이다 — 전부 PASS 인 샘플은 게이트가 살아있는지 알 수 없다.
- ⚠️ **이전 판은 FAIL 7 이었다** (2026-08-29: + FK 참조 무결성 / 추론 sanity / 스키마 참조 무결성 2건 / 필수참여 공리 11쌍 / 미선언 DP). 5건이 사라진 것은 **게이트가 느슨해진 것이 아니라 그 결함들을 고친 것**이다 (필수참여 위반 607건 → 0, 미선언 DP 축 신설, 추론 sanity 는 `applicable: false` 로 미판정 표기). 참가자가 "샘플은 왜 우리보다 잘 나오나" 물으면 이 이력을 쓸 것 — 23/25 는 도착점이지 출발점이 아니었다.
- "PASS 개수 = 결함 0 아님. validator 가 못 보는 게 있을 수 있음 → Mutation Audit ([post-workshop §7-3](post-workshop-guide.md#7-3-advanced-mutation-audit--검증-체계-자체를-감사)). 실측으로 S9.5 는 25 체크 중 8개가 이미 FAIL 이라 **17개만 mutant 를 검출할 수 있다**."
- "FAIL 그룹명 메모 — Capstone self-assessment 4번 답 입력으로 활용."

---

## Ch5: SPARQL 실습 (1:54-2:34, 40m) — 핵심!

**목표:** 참가자가 직접 SPARQL 작성/실행. CQ 를 SPARQL 로.

**이 챕터가 워크샵 하이라이트.** Claude 가 만든 SPARQL 이 0건/에러 나는 상황이
**반드시** 발생 — "버그" 가 아니라 "skill transfer 기회" (강사가 그 자리에서 같이
디버깅하는 모습을 보여주는 게 참가자에게 가장 큰 학습 순간).

**진행** — 난이도 4 단계 (5-1 → 5-4a) + CQ 자유 변환 (5-4b). 워크북 링크:
[§Ch5 SPARQL 실습](participant-workbook.md#실습--4단계의-난이도--cq-자유-변환).

| Sub | 시간 | 난이도 패턴 | 무엇을 시키는가 | 강사 행동 포인트 |
|-----|:---:|------------|----------------|-----------------|
| **5-1** | 5m | 기본 SELECT (단일 클래스 + DP) | "X 클래스의 모든 인스턴스 + 속성값" 류 한 줄 쿼리 | **이 단계만** SPARQL 코드에 한국어 주석 직접 달아서 시연. 이후엔 참가자가 작성. |
| **5-1.5** | (30초 멘트) | 5-1 → 5-2 다리 | "같은 변수 `?eq` 가 두 트리플에 나타나면 자동 JOIN — 5-1 과 5-2 의 유일한 차이" | 화이트보드에 두 트리플을 같은 변수로 그어 보여주기 (코드 추가 X) |
| **5-2** | 10m | 관계 + 집계 (GROUP BY/COUNT) | 두 클래스 연결 + 인스턴스 수 집계 | **"JOIN 없는 JOIN"** 강조 — SQL `JOIN` 키워드 없이 트리플 두 줄 나란히 쓰면 자동 join. 페르소나 B (도메인 SME, SPARQL 처음) 가 막히기 쉬운 지점이라 **2명씩 페어링**. |
| **5-3** | 10m | FILTER + 2-hop | `FILTER(?value > 100)` + 2단계 관계 따라가기 | `hasMaintenanceEquipment` (= `equipmentHasMaintenanceHistory` 의 역방향 OP) 강조. **역방향 조회 시연**: 원본 `a_box.ttl` 에는 정비 → 설비 방향(`hasMaintenanceEquipment`)만 있지만, `sparql_local` 은 로드할 때 `owl:inverseOf` 역방향을 채우므로 `?eq steel:equipmentHasMaintenanceHistory ?maint` 도 결과가 나온다 (`merge`·`inferred` 두 모드 모두). |
| **5-4a** | 7m | 크로스 도메인 (3-hop + OPTIONAL) | 3단계 관계 따라가기 + `OPTIONAL` 누락 허용 | **Tag hub 다이어그램** (여러 클래스가 공통 Tag 를 통해 연결되는 그림 — 워크북 수록) 먼저 화면 공유. 강사 데모 → 참가자 따라치기. **0건 발생 시 → 공개 디버깅** (워크북 "디버깅 체크리스트 5단계" 따라). 발표는 **자원자만**, 강제 지목 금지. 익명화 옵션 = Slack DM. |
| **5-4b** | 8m | CQ 자연어 변환 (자유 실습) | Ch2 화이트보드 표 B 의 **STL-1\~STL-3** (철강 샘플 CQ 3개) 를 자연어로 Claude 에 던져 SPARQL 받기 | 한 쿼리당 \~2분 + 디버깅 1회 흡수 가정. 결과는 워크북 표 B 의 PASS/FAIL 칸에 기록. |

**강사 팁:**
- 막히는 참가자 → `sparql-cheatsheet.md` 의 "Common Patterns" 섹션 안내
- 문법 오류가 나면 "Claude 한테 다시 물어보세요" 만 던지지 말 것 — **강사가 직접 손으로 고쳐 보여주는 시연이 최대 skill transfer**
- 참가자에게 **"Claude 가 만든 쿼리 보여줘"** 프롬프트 습관화 시키기 (결과만 보지 말고 매번 SPARQL 자체 확인)

---

## Ch6: 워크샵 클로징 + Capstone (2:34-3:00, 26m)

### Ch6-1. 도메인 질의 테스트 (2:34-2:39, 5m)

**진행** ([워크북 §6-1 도메인 질의 테스트](participant-workbook.md#6-1-도메인-질의-테스트-5분-본인-pc) — 본인 PC):
1. (1분) 개념 + FAIL 3가지 원인 (인스턴스 / 연결 경로 / DP 값)
2. (3분) **본인 PC 직접** `test_domain_queries` → HTML 보고서 생성. 기본값 `verify_joins=True` 는 스키마 검사를 통과한 다중 클래스 CQ 가 있으면 병합 그래프를 1회 로드해 조인 COUNT 를 실행하므로 1초보다 오래 걸릴 수 있다 (그래프 로드 없는 스키마 검사만 하면 `verify_joins=False`, <1초). 브라우저 자동 오픈은 `open_report=True` 일 때만이므로 (기본 False) 워크북 프롬프트가 "보고서를 브라우저로 열어줘" 를 함께 요청한다
3. (1분) PASS/FAIL 분포 함께 보기, FAIL 메모

**핵심 멘트:**
- "Claude Code 가 CQ 마다 SPARQL 을 만들어 실행하는 대신 **고정 연결성 검사로 한 번에 판정**. 이 도구 자체는 모델을 호출하지 않는다 (서버 Bedrock 0회)"
- "FAIL 은 T-Box 보강 / 암묵지 추가의 직접 가이드"
- "본인 브라우저에 보고서가 떠야 '내가 했다'의 감각이 박힘"

### Ch6-2. Capstone — 자사 도메인 구상 (2:19-2:36, 17m)

**목표:** 강사 도움 없이 파이프라인 직접 실행 + 자사 적용 구상.

**위치:** Ch6-3 클로징(5분 회고) 직전.

**전제 — Ch2-2 와의 연계**: Ch2-2 (10m) 슬롯에서 참가자가 **자사 CQ 표 A 3~5개를 미리 작성** 했다는 가정.
따라서 6-2c (3m) 는 표 A 를 TEMPLATE §2 에 **복사 + 다듬기** 만 하면 끝남 — 처음부터 떠올리지 않게 하는 인지 부하 분산 장치.

**진행** ([워크북 §6-2 Capstone](participant-workbook.md#6-2-capstone--자사-도메인-구상-17분-보호)):

**시간 배분 (총 17m)**:

| sub-step | 내용 | 시간 | 누적 |
|----------|------|:--:|:--:|
| 6-2a | 파이프라인 직접 실행 | 6m | 6m |
| 6-2b | T-Box 수정 체험 | 3m | 9m |
| 6-2c | 산출물 작성 | 3m | 12m |
| 6-2c | self-assessment 채점 | 3m | 15m |
| 6-2c | Capstone 제출 (선택) | 2m | 17m |

> **Capstone 제출은 선택 사항이다.** 참가자가 고객명, 실데이터 값, 기밀 스키마, 개인정보를
> 적지 않고 가명 또는 일반화한 값을 쓰도록 6-2c 시작 때 안내한다. 제출을 받는다면 주최 측이
> 지정한 승인된 채널 하나로만 받고, 파일명은 실명 대신 닉네임이나 참가번호를 쓰게 한다
> (워크북 §6-2c 와 같은 규칙).

1. **(6분) 6-2a 파이프라인 직접 실행**:
   - `check_pipeline_state` → 건너뛸 단계 확인
   - 시맨틱 딕셔너리 생성 + 검증 + 도메인 질의 + 최종 보고서
   - 참가자가 Claude Code 에 자연어로 직접 지시
2. **(3분) 6-2b T-Box 수정 체험**:
   - `update_tbox_incremental` 로 새 클래스 (SafetyIncident) 추가
   - 자동 검증 (구문+품질+SHACL) 사이클 체험 + 결과 해석

> **설정 파일 실제 편집**은 [post-workshop §D+1 첫 숙제 — 설정 파일 실제 편집 (30분)](post-workshop-guide.md#d1-첫-숙제--설정-파일-실제-편집-30분) 으로 이동 (16분 + 원복 까다로움). **자사 CQ dry-run 변환** 도 같은 D+1 슬롯에 흡수.

**핵심 메시지: "설정 파일 80% + 환경변수/SME 20%, 코드 수정 0%"**

**강사 팁:**
- 강사 최소 개입, 참가자 주도
- 막힘 → 워크북 §Ch6-2 프롬프트 예시 안내
- Neo4j LPG / SWRL / 전체 재실행은 post-workshop "(선택) 고급 기능"

**(시간 ≥ 3분 여유 시) Mutation Audit 티저:**
```
Meta-audit 실행해줘
```
→ `run_meta_audit` (~1초). 결과: "sensitivity matrix / blind-spots / dead-checks"
— "22 PASS 인데 감사하면 뭐가 나오는지" 1분 Aha 모먼트. 상세는 [post-workshop §7-3 — Mutation Audit](post-workshop-guide.md#7-3-advanced-mutation-audit--검증-체계-자체를-감사).

### Ch6-3. 회고 (2:56-3:00, 4m) — 클로징

**위치:** Ch6-2 Capstone 직후.

**진행:**
1. (2분) **CQ 달성률 점검**: 화이트보드의 Ch2 CQ 앞에서 PASS/FAIL 표시. "80%+ 면 T-Box 가 요구사항 잘 커버"
2. (1분) **옆 사람 1:1 교환** — "오늘 가장 놀라웠던 1가지"
3. (1분) **다음 단계 안내** — 4종 배포 자료 (workbook / cheatsheet / post-workshop / docs/README) 위치

> 세션 후 숙제 안내 [post-workshop §"D+1 첫 숙제"](post-workshop-guide.md#d1-첫-숙제--설정-파일-실제-편집-30분) 로 이동.

---

### 참가자 배포 자료 (4종)

- [`participant-workbook.md`](participant-workbook.md) — 실습 복습
- [`sparql-cheatsheet.md`](sparql-cheatsheet.md) — SPARQL 참조 카드
- [`post-workshop-guide.md`](post-workshop-guide.md) — 자사 PoC 시작 가이드 (Week 1/2 게이트 정의 포함)
- [`docs/README.md`](../docs/README.md) — 심화 참조 진입점 (4트랙 학습 경로)

---

## 파이프라인 요약 (참고)

| 단계 | 챕터 | 도구 |
|------|:---:|------|
| S1. 데이터 확인 | Ch2-1 | list_csv_tables, read_csv_schema, generate_csv_erd, profile_csv_data |
| S0. CQ 생성 | Ch2-2 | generate_competency_questions |
| S2. T-Box 생성 | Ch3-1 | generate_tbox_collaborative |
| S3. T-Box 품질 개선 | Ch3-1 | improve_tbox_quality (스텝 0~30) |
| S4. T-Box 검증 | Ch3-1 | validate_ttl_syntax, check_quality_rules, validate_owl_consistency, classify_tbox, validate_tbox_shacl |
| S5. 암묵지 | Ch3-3 | check_tacit_exist, list_tacit_files, read_tacit, add_tacit_from_natural_language, generate_tacit_from_rules, generate_tacit_from_data, augment_csv_fk, skip_tacit_knowledge |
| S6. 시각화 | Ch3-2 | visualize_tbox |
| S7. A-Box 생성 | Ch4-1 | generate_abox |
| S8. OWL 추론 | Ch4-2 | run_owl_rl_inference |
| S9. KG 검증 | Ch4-3 | validate_kg |
| S10. 시맨틱 딕셔너리 | Ch6-2 | generate_semantic_dictionary |
| S11. 딕셔너리 검증 | Ch6-2 | validate_semantic_dictionary |
| S12. 도메인 질의 테스트 | Ch6-1, Ch6-2 | test_domain_queries |
| S13. 최종 보고서 | Ch6-2 | generate_pipeline_report |
| **N1~N3. LPG / Neo4j** | (자체 학습) | convert_rdf_to_lpg, generate_lpg_semantic_dictionary, neo4j_deploy_lpg |
