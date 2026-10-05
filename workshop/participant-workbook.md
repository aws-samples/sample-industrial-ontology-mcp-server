# 산업용 온톨로지 구축 워크샵 — 참가자 워크북

## 🎯 한 줄 약속 (5개 문서 공통)

> **"당신은 3시간 후 (a) Knowledge Graph 의 가치를 비유 없이 1분 안에 설명할 수 있고, (b) Claude 가 만든 SPARQL 의 정확성을 직접 검증할 수 있고, (c) 자사 도메인 PoC 의 Day 1 에 무엇부터 시작할지 정확히 안다."**

<details><summary><strong>📋 페르소나 — 본인이 어디에 해당하는지 (1분 확인)</strong></summary>

본 가이드는 4가지 페르소나를 가정합니다. Ch5 페어링 / Capstone (Ch6 최종 실습 — 자사 도메인 적용 구상) 옵션 선택 시 참조:

| 페르소나 | 정의 | SQL/SPARQL | 본 워크샵 주된 가치 |
|:--:|------|:--:|------|
| **A** | 데이터 엔지니어 (CSV/RDBMS 익숙, 그래프 처음) | SQL ⭕ / SPARQL ❌ | Ch5 SQL ↔ SPARQL 매핑 + 자사 PoC 파이프라인 |
| **B** | 도메인 SME (현업 운영자, SQL/SPARQL 처음) | SQL ❌ / SPARQL ❌ | Ch3 시각화 + Ch3-3 자연어 암묵지 입력 + CQ 작성 |
| **C** | 데이터 사이언티스트 (분석 익숙, 추론·온톨로지 처음) | SQL ⭕ / SPARQL △ | Ch4 OWL 추론 가치 + Ch6 도메인 질의 테스트 |
| **D** | 솔루션 아키텍트 / 파트너 SA (다도메인 적용 책임) | SQL ⭕ / SPARQL △ | 1주만에 자사 도메인 적용 검증 + Capstone |

> 본인이 SPARQL 처음이라면 **B 페르소나** 입니다. 5-2/5-4a 슬롯에서 **2명씩 페어링** 권장 (강사 안내). B 페르소나는 SQL 도 처음이지만 SPARQL 5단어 표만 외워도 진행 가능 — setup-guide §Prerequisite 참조.

</details>

## 핵심 5단어 (이것만 손에 쥐고 시작)

| 단어 | 한 줄 | 비유 |
|------|------|------|
| **T-Box** | 스키마 (어떤 클래스가 있나) | 건물 **설계도** |
| **A-Box** | 인스턴스 (실제 데이터) | 설계도대로 지은 **건물** |
| **KG** | T-Box + A-Box 합친 그래프 | 설계도 + 건물 |
| **SPARQL** | KG 질의어 | 그래프용 SQL |
| **MCP** | Claude 가 외부 도구를 부르는 표준 | AI 의 "도구 손잡이" |

다른 용어 (OWL / IOF / SHACL 등) → [부록 C 용어 사전](#부록-c-용어-사전).

## 타임테이블

> **온톨로지 무경험자 대상** — Ch1 앞에 **온톨로지 기초 PPTX 강의(30분)** 를 둡니다.
> 이 강의가 기존 "왜 KG 인가" 개념 설명을 대체하고 환경 점검을 맨 앞 **세션 0** 으로 빼서, 셋업
> 낙오자는 PPTX 강의 중 강사가 1:1 로 처리합니다.

| 시간 | 세션 | 내용 | 형식 |
|:---:|------|------|------|
| 10m | **세션 0** | 환경 점검 (낙오자는 PPTX 중 강사 1:1) | 실습 [본인 PC] |
| 30m | **PPTX** 🆕 | 온톨로지 기초 강의 — 왜 온톨로지 / Triple·T-Box·A-Box / RDBMS vs KG (CWA·OWA) / OWL 추론 / 오늘의 흐름 | 강의 (강사 PPTX) |
| 5m | **Ch1** | 첫 SPARQL 핸즈온 (PPTX 개념을 손으로 확인) | 실습 [본인 PC] |
| 18m | **Ch2** | 데이터를 보고 질문을 만든다 (CSV → CQ + 자사 CQ 사전 워밍업) | 실습 |
| 21m | **Ch3** | 스키마를 만든다 (T-Box + 시각화 + 암묵지) | 분석 + 실습 |
| 12m | 휴식 | | |
| 18m | **Ch4** | 데이터로 채우고 추론한다 (A-Box + OWL) | 실습 |
| 40m | **Ch5** | SPARQL — 질문에 답한다 ⭐ 핵심 | 실습 (보호) |
| 26m | **Ch6** | 워크샵 클로징 + Capstone (자사 도메인 적용 최종 실습) | 실습 (보호) |
| 20m | 버퍼 | 지연 흡수 + 심화 Q&A (Ch5 구제용) | |
| **200m** | **합계** | **= 3시간 20분** | |


---

# 세션 0. 환경 점검 (10분) [본인 PC]

> 이 슬롯은 **PPTX 기초 강의 앞**에 둡니다. 점검이 막혀도 이어지는 30분 PPTX 동안
> 강사가 1:1 로 해소하므로 그룹 진행을 막지 않습니다.

> **D-1 까지 fallback 산출물 사전 적재 회신을 강사에게 했어야 합니다** —
> 미회신자는 본 슬롯에서 강사 1:1 (5분 안에 안 끝나면 강사 노트북 화면 공유로 우회).
> setup-guide §5.4 + §8 체크리스트 참조.

Claude Code 에서 두 줄을 실행합니다. 강사도 같은 명령을 화면에 띄움.

```
CSV 파일 목록 보여줘
```
→ `list_csv_tables` 실행, 40개 CSV 파일 반환되면 정상.

```
파이프라인 상태 확인해줘
```
→ `check_pipeline_state` 가 모든 단계 미완료(`completed: false`) 배열을 반환하면 정상 (아직 아무 단계도 실행하지 않았으므로).

**(강력 권장) 워크샵 코드블록 일괄 검증** (실측 47.663초):
```bash
python scripts/verify_workshop_sparql.py --ignore-placeholders
```
→ `exit 0` 이면 본 워크북의 모든 SPARQL/JSON/TTL/bash 코드블록이 본인 환경에서 실행 가능.

### 점검 실패 시

| 증상 | 즉시 대응 |
|------|---------|
| "도구가 없습니다" / MCP 서버 미등록 | setup-guide §5.3 → **Claude Code 완전 재시작** |
| AccessDeniedException | Bedrock 미승인 → **강사 1:1 호출** (D-1 사전 적재 회신했어야 함) |
| CSV 0건 | `git pull` 또는 `data/source/rawdata/` 폴더 확인 |

> **D-1 까지 fallback 적재 + verify 회신이 사전 등록의 일부**입니다. 본 슬롯에서 적재
> 명령을 처음 실행하면 사내망 환경에서 5분 안에 끝나기 어려움 — 강사 1:1 (5분 안에 안
> 끝나면 강사 노트북 화면 공유로 우회). 적재 명령 본문은 [setup-guide §5.4](setup-guide.md#54-fallback-산출물-사용법-bedrock-미승인자--8gb-ram-사용자) 참조.

---

# 온톨로지 기초 강의 (30분) — 강사 PPTX

> 📊 **이 30분은 강사 PPTX 강의입니다.** 노트북 없이 들으셔도 됩니다 (세션 0 환경
> 점검이 안 끝났으면 이 시간에 강사가 1:1 로 마저 도와드립니다). 아래는 PPTX 가
> 다루는 5가지로, **워크북의 이 단락들이 그 복습 카드**입니다 — 강의 중·후에 참조하세요.

| PPTX 묶음 | 핵심 | 워크북 복습 위치 |
|---|---|---|
| 1. 왜 온톨로지인가 | 데이터 사일로 → 관계 중심 | 아래 "왜 KG 인가" 박스 |
| 2. 핵심 개념 | Triple / T-Box / A-Box / KG | [핵심 5단어](#핵심-5단어-이것만-손에-쥐고-시작) + [부록 C](#부록-c-용어-사전) |
| 3. RDBMS vs KG | CWA / OWA (NULL 비유) | 아래 "RDBMS vs KG 핵심 차이" 표 |
| 4. OWL 추론 | "묻지 않은 답까지" | Ch4-2 에서 손으로 확인 |
| 5. 오늘의 흐름 | 파이프라인 14단계 | 아래 "오늘 체험할 흐름" + [부록 B](#부록-b-파이프라인-14단계) |

**오늘 체험할 흐름**: **데이터 → CQ → T-Box → 품질 개선 → 검증 → 암묵지 → 시각화 → A-Box → 추론 → KG 검증 → 딕셔너리 → 딕셔너리 검증 → 도메인 질의 → 보고서** (14단계). 상세는 [부록 B](#부록-b-파이프라인-14단계).

> **왜 데이터 → CQ?** 방법론상 Grüninger & Fox (1995) 는 CQ 먼저지만, 초보 청중에게는 데이터 구조를 본 뒤 "이걸로 뭘 물을까" 가 직관적. 자사 PoC 에서는 SME 가 있으면 CQ 먼저, 없으면 데이터 먼저가 안전 (post-workshop §0-1 Week 1 Day 1~2).

### (복습 카드) 왜 KG 인가 — PPTX 묶음 1·3 정리

당신의 고객이 묻습니다:
> "설비 EQ001 에 어떤 센서가 달려있고, 최근 알람이 뭐가 있었고, 정비는 언제 했는지 한번에 보고 싶어요."

기존 RDBMS 에서는 **4개 테이블 JOIN** 이 필요:
```sql
SELECT e.equipment_name, t.tag_name, a.alarm_type, a.severity,
       m.maintenance_type, m.maintenance_date
FROM Equipment_Master e
JOIN Tag_Master t              ON e.equipment_id = t.equipment_id
JOIN Alarm_Events a            ON t.tag_id = a.tag_id
JOIN Maintenance_History m     ON e.equipment_id = m.equipment_id
WHERE e.equipment_id = 'EQ001';
```

> **차별화 (왜 KG 인가)**:
> 1. **관계가 데이터의 1등 시민**: RDBMS 는 "어느 컬럼 = 어느 컬럼" 을
>    매 쿼리에서 다시 적지만, KG 는 `:EQ001 :hasTag :T1` 자체가 사실로
>    저장됨 — 쿼리는 그물 따라가기.
> 2. **T-Box 가 비즈니스 사전**: 컬럼 이름 (`equipment_id`) 대신
>    의미 있는 관계 (`:hasTag`, `:hasAlarm`) — 도메인 전문가도 읽힘.
> 3. **추론으로 묻지 않은 답까지** (Ch4-2 에서 자세히): T-Box 규칙
>    하나로 "EQ001 에 알람이 있다" 가 자동 도출. RDBMS 는 매번
>    JOIN 으로 재계산하지만, KG 는 한 번 추론 → 영구 저장.
>
> 실제 SPARQL 은 Ch5-4a 에서 풀이.

### RDBMS vs KG 핵심 차이

| 차이 | RDBMS (CWA — 닫힌 세계) | KG (OWA — 열린 세계) |
|---|---|---|
| **관계 추가** (예: 설비에 담당자 정보 추가) | `ALTER TABLE Equipment ADD assigned_to ...` 후 마이그레이션 | `:EQ001 :assignedTo :Kim .` 트리플 한 줄 추가로 끝 |
| **데이터에 없는 사실의 의미** (예: EQ001 의 정비기록이 없음) | "정비를 안 했다" 로 **확정** 처리 | "기록이 없을 뿐, 했을 수도 있음" — **모름** 으로 처리 |
| **"정비 안 한 설비" 질의 결과** | `WHERE NOT EXISTS (...)` → 정비 안 한 설비 깔끔하게 반환 | `FILTER NOT EXISTS` → "**기록상**" 정비 안 한 설비만. 실제 정비 여부와 다를 수 있음 |

> **왜 중요한가**: KG 는 "데이터가 불완전할 수 있음" 을 인정하는 모델 —
> 여러 시스템에서 데이터가 **점진적으로 들어올 때** 안전합니다. 반대로
> RDBMS 의 CWA 는 "내 DB 가 진실의 전부" 라는 가정이 깔려 있어, 외부
> 데이터와 통합 시 잘못된 결론을 낼 수 있습니다.
>
> ⚠️ `FILTER NOT EXISTS` 결과를 "확실히 없음" 으로 읽으면 안 됨 —
> "**우리가 알고 있는 한** 없음" 으로 해석.

---

# Ch1. 첫 SPARQL 핸즈온 (5분) [본인 PC]

> PPTX 에서 들은 개념을 **손으로 한 번** 확인합니다. Claude Code 에 자연어로 던지고,
> **Claude 가 만든 SPARQL 을 함께 봅니다**:

```
모든 설비의 ID 를 5개만 보여줘.
```

Claude 가 다음과 비슷한 SPARQL 을 만듭니다:
```sparql
SELECT ?eq WHERE {
  ?eq a steel:EquipmentMaster .
}
LIMIT 5
```

읽는 법: `?eq` = 변수, `a` = `rdf:type` 약어, `steel:EquipmentMaster` = 클래스. **5개 설비 URI 반환** — 이게 SPARQL 의 본질. Ch5 에서 점점 복잡해질 뿐 기본은 같음.

---

# Ch2. 데이터를 보고 질문을 만든다 (18분)

## 2-1. 데이터 탐색 (8분) [본인 PC]

Claude Code 에 4개 명령을 차례로:

```
CSV 파일 목록을 보여줘.
```
→ `list_csv_tables` — 40개 파일.

```
Equipment_Master 테이블의 스키마를 보여줘.
```
→ `read_csv_schema` — 컬럼명/타입.

```
CSV 테이블 간 관계를 ERD로 시각화해줘.
```
→ `generate_csv_erd` — vis.js HTML 자동 오픈. **FK 관계가 화살표** (Ch3 OP 예측의 입력).

> **어디부터 봐야?** 40개 테이블이라 막막할 때 — **`Equipment_Master` 노드부터 클릭**.
> 거기서 뻗는 FK 화살표가 본 워크샵의 핵심 도메인 (Tag → Alarm → Maintenance) 입니다.

```
CSV 데이터 품질을 프로파일링해줘.
```
→ `profile_csv_data` — NULL률/유니크률/타입 불일치/FK 유효성. (Ch4 A-Box 의 SHACL 자동 수정 근거)

## 2-2. CQ (Competency Questions) 작성 (10분)

온톨로지의 존재 이유는 **질문에 답하는 것**. CQ 없이 T-Box 만들면 기술적으로 올바르지만 실무적으로 쓸모없는 KG 가 나온다.

> **표 A 와 표 B 를 분리해서 작성** — 표 A 는 자사 (Capstone 6-2c 입력), 표 B 는 철강 (Ch5 변환 대상). 표 A 를 **이 슬롯에 미리 작성** 해두면 Ch6 Capstone (3분) 이 "복사 + 다듬기" 로 단순해짐.

### 📝 표 A — 자사 도메인 CQ 3~5개 (Ch6 Capstone 입력, 사전 워밍업)

본인 회사 비즈니스 질문 — 본인 부서/제품/고객/공정 등 (철강 X). **3개라도 채워두면 Capstone 출발 가능**:

| ID | 자연어 질문 (자사) | 분류 | 우선순위 |
|:--:|----------------|:--:|:--:|
| OWN-1 | | | |
| OWN-2 | | | |
| OWN-3 | | | |
| OWN-4 (선택) | | | |
| OWN-5 (선택) | | | |

> 막히면 "내가 이 데이터로 답하고 싶은 첫 질문은?" 자문. 정답 없음 — Ch6 에서 다듬을 시간 있음.

### 📝 표 B — 철강 CQ (Ch5 SPARQL 변환 입력)

**필수 STL-1~3** (Ch5-4b 슬롯 8분에 변환) + **선택 STL-4/5** (D+1 숙제). 설비/공정/품질/에너지/이벤트 중에서:

| ID | 자연어 질문 (철강) | 분류 | 우선순위 | Ch5 결과 |
|:--:|-----------------|:--:|:--:|:--:|
| STL-1 (필수) | | | | □ PASS  □ FAIL |
| STL-2 (필수) | | | | □ PASS  □ FAIL |
| STL-3 (필수) | | | | □ PASS  □ FAIL |
| STL-4 (선택, D+1) | | | | □ PASS  □ FAIL |
| STL-5 (선택, D+1) | | | | □ PASS  □ FAIL |

**예시** (참고용 — 본인이 직접 3개 작성):

| ID | 질문 | 분류 | 우선순위 |
|----|------|------|---------|
| CQ-001 | 설비 EQ001 에 연결된 센서와 현재 상태는? | 설비 | P0 |
| CQ-002 | 최근 30일 알람 중 critical 이상은? | 이벤트 | P0 |
| CQ-003 | 고로→제강→연주→압연 공정 순서와 담당 설비? | 공정 | P1 |
| CQ-004 | 에너지 소비 상위 10개 설비? | 에너지 | P1 |
| CQ-005 | 특정 제품의 화학 분석 + 기계적 성질? | 품질 | P0 |

**AI 생성과 비교** — 작성 후 Claude Code 에:
```
CQ를 자동 생성해줘.
```
→ `generate_competency_questions` — 10개 자동 CQ. **본인 3개와 비교** — 어떤 비즈니스 관점이 누락됐나? AI 생성 10개 중 마음에 드는 것을 STL 표에 옮겨도 OK.


---

# Ch3. 스키마를 만든다 — T-Box + 시각화 + 암묵지 (21분)

> Ch3 는 **강사 D-1 결과 분석** 비중이 높음. T-Box 생성 (Bedrock 15~40분, 모델·수렴 라운드에 따라) 은 D-1 사전 실행, 워크샵에서는 결과만 분석.

## 3-1. T-Box 결과 분석 + OP 예측 활동 (10분)

> 📖 **이 챕터(Ch3)는 읽기·분석 위주입니다.** T-Box 생성은 비싸서 (Bedrock 15~40분) 강사가
> D-1 에 미리 돌린 결과를 함께 해석합니다. 본인 손으로 직접 하는 핸즈온은 3-2 시각화와
> **3-3 암묵지 미니 핸즈온(2분)** 입니다. "핸즈온 75%" 비율은 워크샵 전체 기준이며 Ch3 만
> 분석 비중이 높은 게 정상입니다.

> **OP / DP 1줄 정의** (지금 외워두기):
> - **OP (ObjectProperty)** — 인스턴스 ↔ 인스턴스 관계 (예: `EQ001 hasTag T01`)
> - **DP (DatatypeProperty)** — 인스턴스 → 값 (예: `EQ001 equipmentMasterName "Pump-A"`)

> **왜 OP 가 핵심?** 클래스는 CSV 파일명 → 표면 변환. **OP 는 관계 방향성** 결정 — LLM 이 가장 자주 실수하는 차원이고 SPARQL 0건의 80% 원인. 이 활동이 Ch5 디버깅 능력으로 직결.

### Activity 3-A [핵심, 5분]: ObjectProperty 예측

옆 사람과 함께 Ch2 ERD 의 FK 화살표 3개 고르고, 각 FK 가 만들 OP 후보 5개 작성:

| # | OP 후보 | domain → range | 이유 (1줄) |
|:-:|---------|---------------|----------|
| 1 | 예: `hasTag` | EquipmentMaster → TagMaster | "설비가 태그를 보유" |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |

### 강사 D-1 결과 공유 (5분)

강사가 차례로 보여줍니다 — **참가자는 결과 해석만**. 단, 무엇이 **파일로 남는지** vs **도구 실행 응답(화면)으로만 나오는지** 구분해 둡니다 (자사 PoC 에서 본인이 직접 돌릴 때 어디서 찾을지가 달라짐).

**A. 파일로 남는 산출물** — 본인 PC 에도 동일 경로에 적재돼 있어 직접 열어볼 수 있음:

| 산출물 | 경로 | 여는 법 |
|--------|------|--------|
| T-Box | `data/generated/tbox/t_box.ttl` | `"생성된 T-Box를 보여줘"` (`read_tbox`) 또는 VS Code 에서 더블클릭 |

- **생성된 클래스/OP** — `owl:Class` / `owl:ObjectProperty` 선언. Activity 3-A 에서 예측한 OP 후보(`hasTag` 등)가 실제로 있는지, `rdfs:domain`/`rdfs:range` 방향이 본인 예측과 같은지 비교. **빠지거나 뒤집힌 관계 = Ch5 SPARQL 0건의 후보 원인.**
- **`improve_tbox_quality` 자동 후처리 흔적** — `t_box.ttl` 안의 `owl:inverseOf` / `owl:AllDisjointClasses` / ODP ([→용어](#부록-c-용어-사전)) / `owl:FunctionalProperty`. 사람이 쓴 게 아니라 후처리가 자동 추가한 것.

**B. 도구 실행 응답으로만 나오는 것** — **파일이 아님.** `generate_tbox_collaborative` 를 직접 실행할 때 그 **응답(화면)** 에만 나타나므로, 미리 만든 산출물 파일에는 없음. 강사 화면(D-1 실행 로그)으로 봄:

- **debate_log** ([→용어](#부록-c-용어-사전): Multi-Agent 토론 기록) — Validator 비판을 본인 OP 예측과 비교 (Validator 가 가장 자주 잡는 결함이 OP 방향).
- **5단계 검증 결과** — `validate_ttl_syntax` → `check_quality_rules` → `validate_owl_consistency` → `classify_tbox` → `validate_tbox_shacl` 통과 여부.

> Multi-Agent (핵심 토론자 Architect + Validator + SME, 합의 보조 Jury + Compromise) 토론 규칙 + debate_log 읽기는 [post-workshop-guide §10 FAQ](post-workshop-guide.md#10-faq--자주-하는-질문), 17단계 후처리 vs 수동 편집은 [§5 의사결정 트리](post-workshop-guide.md#5-의사결정-트리), unsatisfiable class ([→용어](#부록-c-용어-사전): 인스턴스를 가질 수 없는 모순 클래스) 디버깅은 [§4 흔한 에러 Top 10](post-workshop-guide.md#4-흔한-에러-top-10).

## 3-2. T-Box 시각화 + 분석 (8분) [본인 PC]

```
T-Box를 시각화해줘.
```
→ `visualize_tbox` — 인터랙티브 HTML. 왼쪽=IOF ([→용어](#부록-c-용어-사전): 산업 표준 상위 온톨로지) 상위 클래스 트리 / 가운데=vis.js 네트워크 / 오른쪽=노드 클릭 시 DP/OP. **"공정 흐름" 버튼** 으로 고로→제강→연주→압연 하이라이트.

```
T-Box를 분석해줘.
```
→ `analyze_tbox` — 철강 샘플: 클래스 89 / OP 108 / DP 272 (2026-09-06 산출물).

```
T-Box 품질을 평가해줘.
```
→ `measure_tbox_metrics` (Tartir 2005 OntoQA 6 메트릭). **꼭 기억할 2개**:

| 메트릭 | 의미 | 정상 |
|--------|------|------|
| **DIT** (계층 깊이) | 가장 깊은 subClassOf 체인 | 2~5 |
| **RR** (관계 풍부도) | OP / 전체 프로퍼티 비율 | 0.3~0.6 |

DIT ≤ 1 = 평면 / DIT ≥ 6 = 과세분화 / RR < 0.3 = 단순 테이블 나열. 나머지 4 메트릭 + 등급 기준은 [post-workshop §3 T-Box 품질 개선](post-workshop-guide.md#3-t-box-품질-개선-사이클).


## 3-3. 암묵지 — 4가지 입력 방법 (3분, 본인 PC)

CSV 에 없지만 **현장 운영자가 아는 도메인 지식** (공정 흐름 / 품질 규격 / 고장 패턴). 추론 시 자동 병합. 철강 샘플은 이미 7개 TTL 있음.

**운영 순서** — 자사 적용 시 (a) 부터 시도, 후보가 모이면 (b)/(c) 로 확장:

| 방법 | MCP 도구 | 언제 |
|------|----------|------|
| **(a) 자연어** ⭐ 1순위 | `add_tacit_from_natural_language` | SME 가 말로 설명 가능 (가장 정확) |
| (b) 규칙 기반 | `generate_tacit_from_rules` | 검증된 패턴 + CSV 자주 갱신 |
| (c) CSV+LLM 부트스트랩 | `generate_tacit_from_data` | SME/규칙 둘 다 없는 초기 (반드시 (a)/(b) 로 승격) |
| (d) Skip | `skip_tacit_knowledge` | S9 공정 체인 FAIL 감수 |

### 미니 핸즈온 — (a) 자연어 한 번 직접 입력 (2분)

Claude Code 에 그대로:
```
다음 암묵지를 my_first_tacit.ttl 에 자연어로 추가해줘:
"고로 → 제강 → 연주 → 압연 순서로 공정이 진행된다"
```
→ `add_tacit_from_natural_language` 호출 → TTL 파일 1개 생성. 응답에서 **생성된 트리플 수 + 파일 경로** 만 확인.

---

# **휴식 (12분)**

---

# Ch4. 데이터로 채우고 추론한다 — A-Box + OWL (18분)

A-Box 는 본인 PC에서 직접 (LLM 0회, 30~50초) 생성하고, 추론(S8) 은 cold start 위험으로 강사 D-1 결과 (`pre-generated/all_inferred.ttl.gz`) 사용.

## 4-1. A-Box 생성 (5분) [본인 PC]

```
CSV 데이터를 RDF 인스턴스로 변환해서 A-Box를 생성해줘.
```
→ `generate_abox` (rdflib 기반). 응답 JSON 의 **`fk_match_stats`** 는 **4단계 fallback** + 실패 카운터를 갖는다:

| 단계 | 키 | 무엇 | 권장 |
|:-:|------|------|------|
| L1 | `exact` | 그대로 연결 (이상적) | 많을수록 좋음 |
| L2 | `normalized` | NFKC + 대소문자/하이픈/언더스코어 정규화 후 일치 (default ON) | 살아남은 건 자동 복원 |
| L3 | `levenshtein` | edit distance ≤ 1 (opt-in, `EQ001` ↔ `EQ01`) | 많으면 CSV 포맷 정합 검토 |
| L4 | `prefix` | 모호하지 않은 prefix 일치 (opt-in, false positive 위험) | 카운터 + 로그 직접 확인 |
| — | `unresolved` | 앞 4단계 모두 실패 — 트리플 생성 안 됨 | 수정 필요 (`fk_patterns.json` 또는 CSV) |

> 자사 CSV 의 비표준 컬럼명 (`eqp_num`, `machineKey`) 도 `rules/contracts/fk_patterns.json` 에 1줄 추가로 매핑 가능. fuzzy 단계 (Levenshtein/prefix) 활성화는 `domain_config.json` 의 `fk_fuzzy_match` 섹션 — 상세는 [post-workshop §10 구축 단계 FAQ](post-workshop-guide.md#10-faq--자주-하는-질문).

## 4-2. 추론 전/후 비교 [본인 PC, 6분, 핵심]

같은 SPARQL 을 두 모드로 실행해 OWL RL 추론의 효과를 본다.

```
sparql_local 로 다음 쿼리 실행해줘 (source="merge", 추론 전):
SELECT ?p (COUNT(*) AS ?c) WHERE { ?s ?p ?o } GROUP BY ?p ORDER BY DESC(?c) LIMIT 20
```

```
같은 쿼리 다시 실행해줘 (source="inferred", 추론 후):
SELECT ?p (COUNT(*) AS ?c) WHERE { ?s ?p ?o } GROUP BY ?p ORDER BY DESC(?c) LIMIT 20
```

> **추론이 만드는 3가지** (이 3개로 OWL RL 의 80% 설명):
> - **inverseOf**: `Tag isTagOf EQ` 가 있으면 `EQ hasTag Tag` 자동 생성 (양방향)
> - **subClassOf 전이**: `EQ001 a EquipmentMaster` + `EquipmentMaster ⊑ Equipment` → `EQ001 a Equipment` 자동
> - **domain/range 타입**: `hasTag` 의 domain=Equipment 라면 `?x hasTag ?y` 의 `?x` 는 자동으로 Equipment 타입

| 차이 | 의미 |
|------|------|
| 트리플 수 약 1.9배 증가 (718K → 1.38M, +92%) | OWL RL 자동 도출 |
| `tagEquipment` 옆에 역관계 `equipmentHasTag` 도 등장 | `inverseOf` 자동 채움 |
| 인스턴스 `rdf:type` 가 상위 클래스로도 출력 | `subClassOf` 전이 |

> **핵심**: 명시되지 않은 관계가 자동 도출. OWL RL 4규칙 (subClassOf 체인 / domain·range 타입 / inverseOf / Transitive) 상세는 [부록 B 파이프라인 14단계](#부록-b-파이프라인-14단계) S8.

## 4-3. KG 검증 (7분) [본인 PC]

내 KG 가 25 체크 중 몇 개를 PASS 하는지 **본인 손으로 확인**. 40~90초 소요.

```
KG 를 검증해줘.
```
→ `validate_kg(use_inferred=False)` — 25 check 결과를 그룹별 summary 로 반환 (T-Box+A-Box+tacit 병합 그래프). **PASS 개수 / FAIL 개수 / critical 위반** 만 보면 충분. (추론 그래프 검증은 `use_inferred=True`, 5~8분.)

| 그룹 | check 수 | 대표 |
|------|:---:|----------|
| **Structural** | 4 | bidirectional OP, 공정 흐름 체인, 고아 노드, 클래스별 인스턴스 수 |
| **Referential** | 7 | FK 무결성, FK-OP 커버리지, dangling, 미선언 OP, **미선언 DP**, CW master/FK unresolved |
| **Semantic** | 5 | domain/range 정합성, **스키마 참조 무결성**, 프로퍼티 커버리지, T-Box fitness, **필수참여 공리** |
| **Statistical** | 4 | 값 범위, 수치/문자열/관계 이상치 |
| **Cardinality** | 5 | 추론 sanity, 완전성, Functional, cardinality, AllDisjoint |

**합격선**:
- 철강 샘플은 **23/25 PASS (FAIL 2)** 이다 (2026-09-06 실측) — FAIL 2건(고아 노드 31건 / 무인스턴스 클래스 18.0%)은 의도적으로 남긴 실제 갭이라 "게이트가 살아있다" 는 증거다
- **자사 첫 실행은 18/25 이상이면 양호** — critical 3종 (functional / disjoint / subclass_cycle)은 반드시 PASS 해야함.
- 샘플의 23/25 는 **출발점이 아니라 도착점**이다. 같은 샘플이 2026-08-29 에는 18/25 (FAIL 7) 였고, 그 5건을 실제로 고쳐서 올린 값이다 — 첫 실행이 18/25 라면 정상 궤도다.

> "PASS 개수가 정말 건강함을 의미하나?" — Mutation Audit 으로 검증 체계 자체의 blind-spot 측정. [post-workshop §7-3](post-workshop-guide.md#7-3-advanced-mutation-audit--검증-체계-자체를-감사).

---

# Ch5. SPARQL — 질문에 답한다 (40분) ⭐ 핵심

이 챕터의 모든 쿼리는 `sparql_local` 로 로컬 실행 (외부 endpoint 없음). **0건 나오면 디버깅 체크리스트 5단계로 풀기**.

## SPARQL 어휘 5단어 (이 5개로 모든 쿼리 읽힘)

| 기호 | 의미 | 예시 |
|------|-----|------|
| `?eq` | 변수 (찾고 싶은 것) | `?eq` |
| `a` | `rdf:type` 약어 | `?eq a steel:EquipmentMaster` |
| `;` | 같은 주어 계속 | `?eq a Cls ; name ?n` |
| `.` | 트리플 끝 | `?eq a Cls .` |
| `prefix:` | 네임스페이스 약어 | `steel:Equipment` |

나머지는 SQL 과 비슷 (`SELECT`, `WHERE`, `FILTER`, `GROUP BY`, `LIMIT`).

## 디버깅 체크리스트 (책상 옆에 붙여두기)

자연어로 쿼리 요청했는데 **결과가 이상한 경우** (0건 / 예상과 다름 / 에러):

1. **Claude 가 만든 SPARQL 직접 보여달라고 요청** → "방금 실행한 SPARQL 보여줘"
2. **프로퍼티명이 실제 T-Box 와 일치하는가?** → "시맨틱 딕셔너리 보여줘"
3. **데이터가 실제 있는가?** → `SELECT ?p (COUNT(*) AS ?c) WHERE { ?s ?p ?o } GROUP BY ?p ORDER BY DESC(?c)`
4. **FILTER 가 너무 엄격한가?** → 임시 제거 후 한 조건씩 추가
5. **OPTIONAL 이 필요한가?** (NULL 허용 컬럼) → 필수 패턴만 있으면 해당 데이터 없는 인스턴스 빠짐

## 실습 — 4단계의 난이도 + CQ 자유 변환

**시간 배분**: 5-1 (5m) / 5-2 (10m) / 5-3 (10m) / 5-4a (7m) / 5-4b (8m) = 40m

> ⏱️ **강사·참가자 시간 안내**: 5-4a(3-hop OPTIONAL)·5-4b(CQ 변환)는 본 워크샵 **최난도**
> 구간으로 7~8분 배분이 빠듯합니다. 막히면 **타임테이블의 20분 버퍼에서 끌어쓰는 것이 정상**
> (버퍼는 사실상 Ch5 구제용). SPARQL 이 처음인 B 페르소나는 5-4a 를 **직접 작성 대신 강사
> 시연 + 따라보기**로 전환해도 OK — 핵심은 "OPTIONAL 로 가지를 친다"는 개념 이해이지 7분 내
> 완성이 아닙니다.

### 5-1. 기본 SELECT (단일 클래스 + DP) — 5분

```
모든 설비의 ID와 이름을 조회해줘.
```

```sparql
SELECT ?id ?name WHERE {        # ?id, ?name 두 개를 결과로
  ?eq a steel:EquipmentMaster   # ?eq 는 EquipmentMaster 인스턴스
  ; steel:equipmentMasterId   ?id        # ?eq 의 ID
  ; steel:equipmentMasterName ?name .    # ?eq 의 이름 (마침표=끝)
} LIMIT 20
```

> **여기서만** 한국어 주석 명시. 5-2 부터는 위 5단어 표만 참조해서 머릿속 번역.

### 5-1.5. 5-1 과 5-2 의 다리 (강사 30초 멘트)

> **같은 변수 `?eq` 가 두 트리플에 나타나면 자동 JOIN** — 5-1 과 5-2 의 유일한 차이.
> SQL 처럼 `JOIN ... ON` 키워드 없이 변수 이름만으로 join 효과. 5-2 부터는 이 원리만 반복 응용.

### 5-2. 관계 탐색 + 집계 (JOIN 없는 JOIN) — 10분

```
설비별 연결된 센서 태그 목록을 조회해줘.
```
```sparql
SELECT ?eqName ?tagName WHERE {
  ?eq a steel:EquipmentMaster ; steel:equipmentMasterName ?eqName .
  ?tag steel:tagEquipment ?eq ; steel:tagMasterName ?tagName .
} LIMIT 20
```

> **방향 주의**: Tag → tagEquipment → Equipment (TagMaster 가 domain). 추론 후 (`source="inferred"`) 에는 `inverseOf` (`equipmentHasTag`) 로 양방향.

```
설비 상태별 개수를 조회해줘.
```
```sparql
SELECT ?status (COUNT(*) AS ?cnt) WHERE {
  ?es a steel:EquipmentStatus ; steel:equipmentStatusValue ?status .
} GROUP BY ?status ORDER BY DESC(?cnt)
```

### 5-3. FILTER + 2-hop — 10분

```sparql
# 효율 < 80% 인 설비
SELECT ?eqName ?efficiency WHERE {
  ?eq a steel:EquipmentMaster ; steel:equipmentMasterName ?eqName .
  ?ee a steel:EnergyEfficiency ;
      steel:hasEfficiencyEquipment ?eq ;
      steel:energyEfficiencyEfficiencyPercent ?efficiency .
  FILTER (?efficiency < 80)
} LIMIT 20
```

```sparql
# equipmentMasterEquipmentType 에 "Pump" 포함된 설비
SELECT ?eqId ?name ?type WHERE {
  ?eq a steel:EquipmentMaster ;
      steel:equipmentMasterId ?eqId ;
      steel:equipmentMasterName ?name ;
      steel:equipmentMasterEquipmentType ?type .
  FILTER (CONTAINS(LCASE(?type), "pump"))
} LIMIT 10
```

**2-hop (설비 → 정비)** — 실제 OP 는 `hasMaintenanceEquipment` (정비 → 설비 방향):
```sparql
SELECT ?eqName ?maintType ?maintDate WHERE {
  ?eq a steel:EquipmentMaster ; steel:equipmentMasterName ?eqName .
  ?maint steel:hasMaintenanceEquipment ?eq ;
         steel:maintenanceHistoryType ?maintType ;
         steel:maintenanceHistoryDate ?maintDate .
} LIMIT 20
```

> **OWL 추론 활용**: `inverseOf` 추론 적용 시 `?eq steel:equipmentHasMaintenanceHistory ?maint` 정방향도 작동 (`source="inferred"`).

### 5-4a. 크로스 도메인 (3-hop + OPTIONAL) — 7분 (최난도, 막히면 강사 시연 전환 OK)

> **이 슬롯은 전원 완성이 목표가 아닙니다.** 직접 작성이 막히면 강사가 화면으로 시연하고
> 따라보세요. 가져갈 한 가지: **"중심 인스턴스(`?eq`)에서 가지마다 `OPTIONAL` 로 감싼다"**.

KG 의 진짜 힘. Alarm 이 EQ 에 직접 연결되지 않고 Tag 를 통해 간접 연결되는 구조:

```
                    ?eq (EquipmentMaster)
                  ▲                         ▲
          hasMaintenanceEquipment       tagEquipment
                  │                         │
              ?maint                      ?tag
                                            ▲
                                      hasAlarmTag
                                            │
                                         ?alarm
```

```
설비별 정비 이력과 연결된 태그의 알람 이력을 함께 조회해줘.
```
```sparql
SELECT ?eqName ?maintType ?alarmType ?severity WHERE {
  ?eq a steel:EquipmentMaster ; steel:equipmentMasterName ?eqName .
  OPTIONAL {
    ?maint steel:hasMaintenanceEquipment ?eq ;
           steel:maintenanceHistoryType ?maintType .
  }
  OPTIONAL {
    ?tag steel:tagEquipment ?eq .
    ?alarm steel:hasAlarmTag ?tag ;
           steel:alarmEventsAlarmType ?alarmType ;
           steel:alarmEventsSeverity ?severity .
  }
} LIMIT 30
```

> `OPTIONAL { ... }` 은 "있으면 채우고 없으면 비워둠" — 정비기록이 없는 설비도 빠지지 않고 결과에 나옴. 가운데 `?eq` 를 중심으로 정비/태그/알람이 가지처럼 뻗어 나가는 구조라, 가지마다 따로 `OPTIONAL` 로 감싼다.

### 5-4b. Ch2 의 철강 CQ 3개를 자연어로 변환 — 8분

Ch2 의 표 B 에 작성한 STL-1~STL-3 을 Claude Code 에 자연어로 던집니다. **8분 안에 3개를
다 못 풀어도 정상** — 1개라도 PASS 시키면 성공, 나머지는 STL-4/5 와 함께 D+1 숙제로 흡수
(디버깅이 길어지면 버퍼 사용):
```
[Ch2 의 STL-1 자연어를 그대로 입력]
```
Claude Code 가 `sparql_local` 도구로 SPARQL 생성+실행. **성공/실패를 Ch2 표 B 에 기록.**

> **CQ 달성률 = PASS / 전체 (3개 기준).** STL-4/5 는 D+1 숙제로 흡수.
>
> 0건 / 에러 시 위 "디버깅 체크리스트" 1번 (Claude 의 SPARQL 보여달라 요청) 부터.

---

# Ch6. 워크샵 클로징 + Capstone (26분)

## 6-1. 도메인 질의 테스트 (5분) [본인 PC]

CQ 가 현재 KG 구조로 답변 가능한지 **그래프 로드 없이** 빠르게 검증 (`test_domain_queries`).

```
도메인 질의 테스트를 실행해줘.
```
→ HTML 보고서가 본인 브라우저에 자동 오픈. CQ 별 PASS/FAIL + 연결 경로 (direct/multihop/indirect) + 누락된 클래스/프로퍼티. **80%+ PASS 목표**.

FAIL 3 가지 원인 — Capstone self-assessment 입력으로 활용:
- 인스턴스 누락 → A-Box 의 CSV 누락
- 연결 경로 없음 → T-Box 에 크로스 도메인 OP 부족
- DP 값 없음 → CSV 컬럼이 DP 로 매핑 안 됨

> **FILTER 쿼리가 0건이면?** (예: `FILTER (?val > 80)`)
> 임계값이 실제 데이터 범위 밖일 수 있음.
> · `semantic_dictionary.json` 에 DP 마다 `value_stats` (min/max/p10/p50/p90) 이 있음
> · 필터 쓰기 전에 실제 값 범위 한번 확인
> · 더 많은 패턴은 [sparql-cheatsheet.md](sparql-cheatsheet.md) "방법 F".

## 6-2. Capstone — 자사 도메인 구상 (17분, 보호)

> 본 모듈의 "자사 도메인" 은 파트너사 SA 라면 **고객 도메인**, 자사 IT 직원이라면 **본인 부서 도메인**.

> **사전 워밍업 활용**: Ch2-2 슬롯에 작성한 **표 A (자사 CQ 3~5개)** 를 6-2c 슬롯에서 TEMPLATE 의 §2 에 **복사 + 다듬기** 만 하면 됨. 처음부터 떠올리지 마세요.

### 6-2a. 파이프라인 직접 실행 (6분)

```
파이프라인 상태를 확인해줘.
```
→ `check_pipeline_state` — 이전 단계 산출물이 있으므로 일부 건너뜀.

이미 T-Box, A-Box 있으므로 시맨틱 딕셔너리부터:
```
시맨틱 딕셔너리를 생성하고 검증해줘.
```
```
도메인 질의 테스트를 실행해줘.
```
```
최종 보고서를 생성해줘.
```

### 6-2b. T-Box 수정 체험 (3분)

```
T-Box에 "SafetyIncident" 클래스를 추가해줘. EquipmentMaster와 연결하는 ObjectProperty도 만들어.
```
→ `update_tbox_incremental` → 자동 검증 (구문+품질+SHACL).

> **(post-workshop 숙제) 설정 파일 실제 편집**: `rules/domain/domain_config.json` (별도 폴더 격리 `rules/my-domain/`) + 메인 `rules/contracts/fk_patterns.json` (직접 편집·원복) 을 자사 값으로 — [post-workshop §"D+1 첫 숙제"](post-workshop-guide.md#d1-첫-숙제--설정-파일-실제-편집-30분) 30분.
>
> **교훈**: **"설정 파일 80% + 환경변수/SME 20%, 코드 수정 0%"**.

### 6-2c. 본인 명의 산출물 작성 (3분) + Capstone 회수 (2분)

`workshop/capstone-outputs/<본인이름>_<날짜>.md` 1페이지로 작성.
템플릿: `workshop/capstone-outputs/TEMPLATE.md`.

**필수 3개** — TEMPLATE 의 **§1, §2, §5** (이것만 채워도 D+1 출발 가능):
- TEMPLATE §1 — 본인 도메인명 + prefix (예: `pharma:`)
- TEMPLATE §2 — 자사 도메인 CQ 3~5개. **Ch2-2 의 표 A 를 그대로 복사** (이미 작성했음)
- TEMPLATE §5 — D+30 까지 도달하고 싶은 상태 (1줄)

**선택 4개** — TEMPLATE 의 **§3, §4, §6, §7** (시간 남으면 / D+1 30분 슬롯에):
- TEMPLATE §3 — FK 패턴 1줄 (자사 CSV 의 FK 컬럼 → 타깃 클래스)
- TEMPLATE §4 — 별도 도메인 폴더 경로 (`rules/my-domain/...`)
- TEMPLATE §6 — 미니 산출물 (페르소나별 옵션)
- TEMPLATE §7 — 자사 CQ dry-run 변환

**회수 (2분)**: 작성한 md 파일을 강사가 사전 안내한 채널 (Slack DM / 공유 드라이브 / 이메일 중 하나) 로 첨부 발송. 사전 등록자 명단 대비 제출률이 enablement 측정의 객관 산출물.

### ✅ Capstone self-assessment (3분, 채점 5문항 + 메타 2문항)

답할 수 있으면 **자사 적용 준비 OK**. **채점은 사실 5문항만**, 메타 2문항은 본인 회고용 (정답 없음).

**(채점 사실 5문항)**
1. 본인 도메인 prefix 를 어떻게 정할 건가? (영문 소문자, 짧게)
2. 본인 CSV 의 FK 컬럼 1개를 어떻게 `fk_patterns.json` 에 추가하나?
3. 자사를 별도 폴더로 격리하는 환경변수 이름은?
4. `validate_kg` 가 25 체크 전부 PASS 아니어도 PoC 진행 가능한 합격선은?
5. SPARQL 0건 나올 때 가장 먼저 확인할 것은? (Ch5 디버깅 체크리스트 1번)

**(메타 2문항, 정답 없음)**
6. 워크샵에서 **가장 이해 못 한** 1가지는?
7. 자사 적용 시 **본인이 가장 막힐 단계** 는? (Ch1~Ch6 중 1개 + 이유)

<details><summary>정답 (사실 문항만)</summary>

1. `rules/domain/domain_config.json` 의 `prefix` 필드. 영문 소문자, 3~6자.
2. `rules/contracts/fk_patterns.json` 의 `patterns` dict 에 `"<정규화된 컬럼명>": "<타깃 클래스명>"` 1줄.
3. `DOMAIN_CONFIG_PATH=rules/my-domain/domain_config.json`
4. 18/25 (critical 3종 functional/disjoint/subclass_cycle 은 반드시 PASS).
5. "Claude 가 만든 SPARQL 직접 보여달라고 요청" — 프로퍼티명이 실제 T-Box 와 일치하는지.
</details>

### 📍 점수별 다음 행동 (자신감 분기)

5문항 채점 후 본인 위치 확인 — **점수는 외움도 일부 반영하므로 합격선은 출발점 안내일 뿐, 최종 판단은 본인 자신감**:

| 정답 수 | 다음 행동 | 입구 |
|:---:|----------|------|
| **4~5 / 5** | 운영 디테일 OK — D+1 첫 숙제 바로 시작 | [post-workshop-guide §"D+1 첫 숙제"](post-workshop-guide.md#d1-첫-숙제--설정-파일-실제-편집-30분) |
| **3 / 5** | 개념은 OK, 자사 데이터로 가볍게 try + 막히면 강사 채널 / 부록 A Quick Check 다시 풀기 | [부록 A](#부록-a-quick-check-워크샵-후-self-study) |
| **0~2 / 5** | 워크샵 핵심 5단어 (T-Box/A-Box/KG/SPARQL/MCP) 부터 — post-workshop §0 처음부터 | [post-workshop-guide §0](post-workshop-guide.md#0-먼저-당신의-목적지부터-고르세요) |

### 🔧 #7번 답 (가장 막힐 단계) — 단계별 1줄 처방

본인이 막힌다고 답한 챕터의 행에서 시작:

| 막힐 곳 | 1줄 처방 | 가세요 |
|---------|---------|--------|
| **세션 0 환경 점검** | MCP/Bedrock 설정 — 강사 채널 D+1~3 자문 우선 | [setup-guide §5 MCP 등록](setup-guide.md#5-claude-code-에-mcp-서버-등록) |
| **Ch2 CQ 3~5개 안 나옴** | 데이터 먼저 → "이걸로 뭘 물을까" 역방향. 자동 생성 10개와 비교 | [§2-2 CQ 작성](#2-2-cq-competency-questions-작성-10분) |
| **Ch3 T-Box 결과 해석** | Multi-Agent 토론 / 17단계 후처리 / unsatisfiable 디버깅 | [post-workshop §3 T-Box 품질 개선](post-workshop-guide.md#3-t-box-품질-개선-사이클) |
| **Ch4 추론 결과 0건** | A-Box 의 FK 매칭 / SHACL 자동 수정 로그 | [post-workshop §10 FAQ 구축 단계](post-workshop-guide.md#10-faq--자주-하는-질문) |
| **Ch5 SPARQL 0건** | 디버깅 5단계 + 시맨틱 딕셔너리 prefix/DP 명 확인 | [sparql-cheatsheet 0건 디버깅](sparql-cheatsheet.md#0건-디버깅--가장-자주-만나는-3가지) |
| **Ch6 자사 도메인 매핑** | `domain_config.json` + `fk_patterns.json` 만 교체, 코드 0% | [post-workshop §"D+1 첫 숙제"](post-workshop-guide.md#d1-첫-숙제--설정-파일-실제-편집-30분) |

## 6-3. 회고 (4분) — 클로징

### CQ 달성률 점검 (2분)

Ch5 에서 철강 CQ (STL-1~STL-3) 중 몇 개를 SPARQL 로 답할 수 있었나?
- 80%+ : T-Box/A-Box 가 요구사항 잘 커버
- 60-79%: 추가 프로퍼티/관계 정의 필요
- 60% 미만: T-Box 재설계 필요


### 다음 단계 안내 (1분)

**4종 배포 자료 위치**:
- [participant-workbook.md](participant-workbook.md) — 본 문서, 실습 복습용
- [sparql-cheatsheet.md](sparql-cheatsheet.md) — SPARQL 막힐 때 즉시 참조
- [post-workshop-guide.md](post-workshop-guide.md) — 자사 PoC 시작 가이드 (경로 A/B/C, Week 1/2 게이트)
- [docs/README.md](../docs/README.md) — 심화 참조 진입점 (4트랙 학습 경로)

---

# 부록 A: Quick Check (워크샵 후 self-study)

문제를 먼저 풀고 정답 박스를 펼쳐 확인.

> **합격선 (자사 PoC 진행 기준)**:
> - **5~6 / 6 챕터** 의 문항을 절반 이상 답할 수 있으면 자사 PoC 진행 OK
> - **3~4 / 6 챕터** 만 통과하면 부족 챕터 본문 + post-workshop §3 (T-Box 개선) 1회 다시 읽기 후 try
> - **2 / 6 이하** 면 [post-workshop §0](post-workshop-guide.md#0-먼저-당신의-목적지부터-고르세요) 처음부터
>
> 100% 정답 목표 아니지만 "절반"의 절반은 자사 PoC 시작 단계에서 위험 — **챕터 통과율** 이 정확한 기준.

<details><summary><strong>Ch1 — KG / OWA / 핵심 5단어</strong></summary>

1. RDBMS 의 `email IS NULL` vs OWL KG 의 "email 트리플 없음" 의미 차이는?
2. T-Box 와 A-Box 의 비유를 한 줄로?
3. CQ 가 왜 파이프라인 초입에 배치되나?

**정답:**
1. RDBMS(CWA) 는 "이메일 없음 확정", OWL(OWA) 는 "이메일 정보가 아직 이 KG 에 기록 안 됨 — 있을 수도 있음".
2. T-Box = 설계도, A-Box = 그 설계도로 지은 건물.
3. CQ 없으면 T-Box 에 뭘 넣을지 판단 근거 없고 S12 질의 테스트 자체가 불가.
</details>

<details><summary><strong>Ch2 — 데이터 탐색 + CQ</strong></summary>

1. `profile_csv_data` 가 왜 A-Box 생성 **전에** 실행돼야 하나?
2. ERD 에서 FK 관계가 화살표로 안 보이면 무엇을 의심?
3. AI 생성 CQ 와 본인 CQ 의 보통 차이는?

**정답:**
1. NULL률/타입 불일치/FK 무효를 미리 알아야 S7 A-Box 생성기가 SHACL 자동 수정 범위를 정함.
2. `rules/contracts/fk_patterns.json` 에 FK 컬럼 네이밍 규칙 누락. 파이프라인은 `_id` 같은 관례 패턴으로 감지.
3. AI 는 크로스 도메인 조인을 잘 찾고, 인간은 비즈니스 컨텍스트/뉘앙스를 잘 잡음 — 보완적.
</details>

<details><summary><strong>Ch3 — T-Box + 시각화 + 암묵지</strong></summary>

1. Multi-Agent 핵심 토론자 3명 (Architect/Validator/SME) 중 한 명만 있으면 왜 안 되나? (합의 실패 시 Jury/Compromise 가 보조)
2. OntoQA 의 DIT 1 이하 / 6 이상이면 무엇이 문제?
3. 암묵지 (a) 자연어 vs (c) CSV+LLM 부트스트랩 — 어느 게 신뢰도 높은가?

**정답:**
1. 단일 에이전트는 자기 합리화 편향(hallucination). Validator 가 누락/모순 잡고 SME 가 도메인 부합 검증 — 견제/토론.
2. DIT ≤ 1: 평면 (모든 클래스가 Thing 직속). DIT ≥ 6: 과세분화.
3. (a) 자연어: SME 가 "문장으로 말한 것" 을 LLM 이 TTL 로 번역 — SME 지식 그대로. (c): CSV 만 보고 LLM 추측 — 가설. (a) 가 신뢰도 높음.
</details>

<details><summary><strong>Ch4 — A-Box + 추론 + 검증</strong></summary>

1. 첫 S8 추론이 20분 걸린 이유? 두 번째는 왜 7~30초?
2. OWL 추론으로 트리플 약 2배 늘어나는 대표 규칙 3개?
3. 자사 첫 실행에서 25 체크 전부 PASS 안 될 때 합격선?

**정답:**
1. 첫 실행은 reasonable 라이브러리 초기화 + Rust 네이티브 cold start. 이후 캐시.
2. `owl:inverseOf` (역관계), `rdfs:subClassOf` 전이, `rdfs:domain`/`range` 타입 추론.
3. 18/25+. critical (functional, subclass_cycle 등) 은 반드시 PASS.
</details>

<details><summary><strong>Ch5 — SPARQL</strong></summary>

1. SPARQL 이 0건 반환할 때 체크할 3가지?
2. `OPTIONAL` 과 SQL 의 `LEFT JOIN` 의 차이?

**정답:**
1. (a) 프로퍼티명이 실제 T-Box 와 일치하는지, (b) FILTER 가 너무 타이트하지 않은지, (c) prefix 선언됐는지.
2. 동일. SPARQL 은 매칭되면 바인딩, 없으면 UNBOUND. `NOT BOUND()` 로 누락 감지.
</details>

<details><summary><strong>Ch6 — Capstone</strong></summary>

1. `reset_pipeline_state` 후 반드시 전체 재실행해야 하나?
2. `rules/domain/domain_config.json` 만 바꾸고 코드 수정 없이 다른 산업에 적용 가능한가?

**정답:**
1. 아니오. 체크포인트가 초기화돼도 산출물 (t_box.ttl 등) 은 남음. `check_pipeline_state` 로 재개점 확인.
2. 가능. `domain_config.json` (도메인명/네임스페이스), `fk_patterns.json` (FK 패턴), `data/source/rawdata/*.csv` 만 교체. 코드 수정 0%. 본 시스템의 핵심 가치.
</details>

---

# 부록 B: 파이프라인 14단계

오늘 워크샵에서 체험한 코어 14단계 + 자동 sub-step 7개. 본문 챕터는 이 흐름의 부분집합.

> **순서 모순처럼 보이는 이유**: PPTX·본문의 흐름은 "**데이터 → CQ → ...**" (UX/체험
> 순서 — 초보 청중에 직관적), 아래 14단계의 S 번호는 "**S0 CQ → S1 데이터 → ...**"
> ([CLAUDE.md](../CLAUDE.md) 의 파이프라인 ID — 논리 의존성 순서). **두 순서가 다른 것이
> 정상**입니다. S 번호는 단계 식별자이지 실행 강제 순서가 아닙니다.

### 코어 14단계

```
S0  CQ 생성 ────── generate_competency_questions (Bedrock)
S1  데이터 확인 ── CSV 40개 스키마 + ERD
S2  T-Box 생성 ── Multi-Agent (핵심 토론자 3: Architect+Validator+SME / 합의 보조 2: Jury+Compromise)
S3  품질 개선 ──── 17단계 후처리 (ODP, OntoQA 등)
S4  T-Box 검증 ── 구문 + 품질 + HermiT + SHACL
S5  암묵지 ─────── 현장 도메인 지식 TTL
S6  시각화 ────── vis.js HTML
S7  A-Box 생성 ── CSV → RDF (FK 매칭, SHACL 자동 수정)
S8  OWL 추론 ─── OWL RL 4규칙 (subClassOf 체인 / domain·range 타입 / inverseOf / Transitive)
S9  KG 검증 ──── 25 check (4+7+5+4+5)
S10 시맨틱 딕셔너리 ─ 메타데이터 참조 문서
S11 딕셔너리 검증 ── T-Box 정합성
S12 도메인 질의 테스트 ─ Zero-Graph-Load CQ 연결성 (<1초)
S13 최종 보고서 ── HTML
```

핵심 설계: **값싼 (S0\~S6 스키마) → 비싼 (S7\~S8 데이터/추론)** 순서.

### 자동 sub-step 7개

| sub-step | 내용 | WARN-only? |
|------|------|:--:|
| S4.5 | T-Box mutation audit (21 mutant × 5 validator) | ✅ |
| S6.5 | 시맨틱 딕셔너리 v1 (T-Box 어휘 contract) | — |
| S8.5 | SWRL 규칙 추론 (`SWRL_ENABLED=true` 일 때만) | — |
| S9.1 | OWL sanity (HermiT consistency) | ✅ |
| S9.2 | 인스턴스 품질 점수 | — |
| S9.5 | KG mutation audit (8 mutant 샘플) | ✅ |
| S12.5 | golden query 회귀 (SME 검증 SPARQL) | ✅ |

전체 상태머신 + sub-step 트리거 조건은 [CLAUDE.md `FULL_PIPELINE`](../CLAUDE.md).

---

# 부록 C: 용어 사전

| 용어 | 한 줄 | 비유 / 더 보기 |
|------|------|------|
| **T-Box** | 스키마 | 건물 설계도 |
| **A-Box** | 인스턴스 | 설계도대로 지은 건물 |
| **KG** | T-Box + A-Box | 설계도 + 건물 |
| **Triple** | 최소 단위: 주어 → 술어 → 목적어 | `:EQ001 :hasTag :TAG001 .` ([심화](#부록-c-1-심화-triple-읽고-쓰기와-rdfrdfsowl-층위)) |
| **SPARQL** | KG 질의어 (W3C 표준) | 그래프용 SQL ([cheatsheet](sparql-cheatsheet.md)) |
| **MCP** | Claude 가 외부 도구 부르는 표준 | AI 의 "도구 손잡이" |
| **CQ** | KG 가 답해야 할 비즈니스 질문 | 요구사항 체크리스트 (Ch2) |
| **Capstone** | 워크샵 최종 실습 — 자사 도메인 적용 구상 1페이지 | Ch6 마무리 과제 |
| **debate_log** | Multi-Agent 토론 기록 (Architect↔Validator↔SME 주고받은 비판/수정) | T-Box 생성 산출물, 결함 원인 추적용 (Ch3) |
| **unsatisfiable class** | 논리 모순으로 **인스턴스를 가질 수 없는** 클래스 | HermiT 가 감지, T-Box 설계 오류 신호 |
| **cold start** | 첫 실행 시 라이브러리/엔진 초기화로 느림 (이후 캐시) | S8 첫 추론 20분 → 이후 초 단위 (Ch4) |
| **OWA** | "값 없음" = "아직 모름" | RDBMS NULL 과 반대 (Ch1) |
| **CWA** | RDBMS 가정 — 안 보이면 없는 것 | OWA 와 반대 |
| **OWL 추론** | "A→B, B→C" → "A→C" 자동 도출 | 숨은 관계 자동 발견 (Ch4) |
| **OWL RL** | OWL 2 가벼운 추론 프로파일 | 본 워크샵 추론 엔진 |
| **SWRL** | 변수 연결 규칙 (알람 3회 → 고장) | OWL RL 표현 못 하는 규칙 (opt-in) |
| **HermiT** | OWL DL 일관성 추론기 | unsatisfiable class 감지 |
| **SHACL** | RDF 데이터 검증 언어 | DB 의 CHECK 제약 |
| **OntoQA** | 온톨로지 품질 평가 (DIT/RR/NOC/AR/Anno/Axiom) | Tartir 2005 6 메트릭 (Ch3) |
| **ODP** | Ontology Design Pattern | 재사용 가능 설계 패턴 |
| **IOF / BFO** | 산업 표준 상위 온톨로지 | "분류 체계의 분류 체계" |
| **OP / DP** | ObjectProperty / DatatypeProperty | hasTag (OP, 목적어=노드) / tagUnit (DP, 목적어=값) ([심화](#부록-c-1-심화-triple-읽고-쓰기와-rdfrdfsowl-층위)) |
| **PK / FK** | Primary / Foreign Key | CSV 분석의 기반 |
| **rdf:type (`a`)** | "~의 일종이다" — 인스턴스를 클래스에 소속 | `:EQ001 a steel:Equipment` (A-Box↔T-Box 다리) |
| **RDF / RDFS / OWL** | 표현력이 다른 어휘 3층 (사다리) | RDF=사실 / RDFS=계층 / OWL=논리 제약 ([심화](#부록-c-1-심화-triple-읽고-쓰기와-rdfrdfsowl-층위)) |
| **Turtle (.ttl)** | Triple 을 사람이 읽기 좋게 적는 문법 | `a` · `;` · `.` 축약 ([심화](#부록-c-1-심화-triple-읽고-쓰기와-rdfrdfsowl-층위)) |
| **prefix** | 네임스페이스 약어 | `steel:` = `http://example.com/steel-ontology#` |
| **MCP 도구** | `list_csv_tables`, `validate_kg`, `sparql_local` 등 | [docs/reference/tool-reference.md](../docs/reference/tool-reference.md) |

---

## 부록 C-1 심화: Triple 읽고 쓰기와 RDF/RDFS/OWL 층위

> PPTX 기초 강의에서 본 Triple·T-Box·A-Box 를 한 단계 더 들여다봅니다. SPARQL·TTL 코드를
> 처음 읽을 때 막히는 부분(기호, 따옴표, `a`, 어휘의 층)을 모았습니다. 강의 흐름에는
> 필수가 아니므로, 코드가 낯설 때 펼쳐 보면 됩니다.

### ① Triple — 기호 하나까지 읽기

KG 의 모든 사실은 **주어 → 술어 → 목적어** 한 줄(= 그래프의 화살표 1개)로 적습니다.

```text
:EQ001  :hasTag  :TAG001 .
```

| 위치 | 값 | 읽는 법 |
|------|-----|--------|
| 주어 (subject) | `:EQ001` | 설비 EQ001 은 |
| 술어 (predicate) | `:hasTag` | 태그를 보유한다 |
| 목적어 (object) | `:TAG001` | 태그 TAG001 을 |

→ **"EQ001 은 TAG001 이라는 태그를 보유한다."** 사실 한 조각입니다.

낯선 것은 보통 **기호**입니다.

| 기호 | 이름 | 역할 | 빠뜨리면 |
|------|------|------|---------|
| `:` (값 앞) | prefix 구분자 | 긴 URI 의 축약 표시 (`:EQ001` = `http://…#EQ001`) | URI 를 매번 풀어 써야 함 |
| 항목 사이 공백 | 구분자 | 주어·술어·목적어를 띄어 구분 | 파서가 토큰을 못 나눔 |
| `.` (맨 끝) | 트리플 종결자 | "이 사실 끝" (문장의 마침표) | 구문 에러 (다음 줄과 붙음) |

> 💡 **마침표 `.` 하나 = 사실 하나의 끝 = 화살표 하나.**

### ② 따옴표 유무 = OP vs DP (가장 흔한 혼동)

목적어에 **따옴표가 있느냐**로 의미가 갈립니다.

```text
:EQ001  :hasTag        :TAG001 .     # 목적어가 노드(사물) → ObjectProperty (OP)
:EQ001  :equipmentType "Motor" .     # 목적어가 값(문자열) → DatatypeProperty (DP)
```

| 작성 | 목적어 | 프로퍼티 종류 |
|------|--------|--------------|
| `… :hasTag :TAG001 .` | 다른 **노드** (따옴표 없음) | ObjectProperty — 노드끼리 연결 |
| `… :equipmentType "Motor" .` | **리터럴 값** (따옴표 있음) | DatatypeProperty — 값 부여 |

→ T-Box(설계도) 예시 `steel:EquipmentMaster a owl:Class .` 처럼, A-Box(건물)는
`steel:EQ001 a steel:EquipmentMaster .` 로 "EQ001 이 그 클래스의 실제 인스턴스"임을
선언합니다. 여기서 `a` 가 A-Box 와 T-Box 를 잇는 다리입니다 (아래 ④).

### ③ Turtle — Triple 을 적는 문법 (`;` `,` 축약)

**Turtle**(`.ttl`)은 추상적인 Triple 을 사람이 읽기 좋게 적는 표준 문법입니다. RDF 를 적는
방식은 여러 가지(N-Triples, RDF/XML, JSON-LD)지만, 가장 간결해 이 워크샵 산출물(`t_box.ttl`,
`a_box.ttl`)이 Turtle 을 씁니다. 핵심은 **축약 기호**입니다.

```text
# 풀어 쓰면 — 트리플 3개
steel:EQ001  a  steel:Equipment .
steel:EQ001  steel:equipmentType      "Motor" .
steel:EQ001  steel:equipmentLocation  "BF_Area" .

# Turtle 축약 — ; 로 주어 반복 생략 (의미는 동일)
steel:EQ001  a  steel:Equipment ;
             steel:equipmentType      "Motor" ;
             steel:equipmentLocation  "BF_Area" .
```

| 기호 | 의미 |
|------|------|
| `;` | 주어 반복 (술어·목적어만 이어 적음) |
| `,` | 주어+술어 반복 (목적어만 이어 적음) |
| `.` | 트리플 종결 |

→ `;` `,` 는 **트리플 개수를 바꾸지 않습니다.** 펼치면 똑같습니다 — 순전히 표기 편의입니다.

### ④ rdf:type 과 `a` — 같은 것

`a` 는 술어 `rdf:type` 의 Turtle 축약입니다. 세 표기 모두 **완전히 같은 트리플**입니다.

```text
steel:EQ001  rdf:type  steel:Equipment .   # 정식
steel:EQ001  a         steel:Equipment .   # Turtle 축약 (가장 흔함)
```

읽기: "EQ001 은 Equipment 클래스의 **인스턴스다**." 추론기는 이 한 줄을 출발점으로
"EQ001 은 Equipment 이므로 Equipment 에 걸린 규칙을 모두 따른다"를 유도합니다.

### ⑤ RDF / RDFS / OWL — 어휘의 표현력 사다리

RDF·RDFS·OWL 은 **서로 다른 문법이 아니라**, 표현력이 다른 같은 계열의 **어휘 3층**입니다.
셋 다 동일하게 **Triple 로(②③) Turtle 에 적힙니다.** 위로 갈수록 기계가 더 똑똑하게 추론합니다.

| 층 | prefix | 더해주는 능력 | 대표 어휘 |
|----|--------|--------------|----------|
| **RDF** (바닥) | `rdf:` | 사실을 트리플로 적기 | `rdf:type` (=`a`) |
| **RDFS** | `rdfs:` | 클래스 계층·정의역/치역 | `rdfs:subClassOf`, `rdfs:domain`, `rdfs:range`, `rdfs:label` |
| **OWL** (꼭대기) | `owl:` | 논리 제약·추론 | `owl:Class`, `owl:ObjectProperty`, `owl:inverseOf`, `owl:disjointWith`, `owl:TransitiveProperty` |

→ T-Box 의 `a owl:Class` / `a owl:ObjectProperty` 가 바로 OWL 층 어휘입니다. OWL 은 RDFS 가
못 하는 **논리 제약**(역방향 `inverseOf`, 배타 `disjointWith`, 추이 `TransitiveProperty` 등)을
표현해, 모순 탐지·자동 분류를 가능하게 합니다 — 이것이 Ch4 OWL 추론의 토대입니다.

> 💡 **가장 흔한 오해**: "RDF=문법, OWL=다른 문법" 이 아닙니다. 문법(Turtle)과 의미(RDF/RDFS/OWL
> 어휘)는 **직교**합니다 — 어느 층의 어휘든 모두 같은 Turtle 로 적힙니다.

### ⑥ 그 밖의 핵심 용어 심화 (SHACL · CWA/OWA · 추론 · OntoQA · 표준)

부록 C 표에서 한 줄로 본 용어 중, 본문에서 자주 마주치지만 한 줄로는 부족한 것을 한 단계 풀었습니다.

**SHACL — 데이터가 규칙을 지키는지 검사**
SHACL(Shapes Constraint Language, W3C 표준)은 RDF 데이터가 정해둔 모양(shape)을 지키는지 검사합니다.
입력은 **데이터 그래프**(검사 대상 A-Box)와 **셰이프 그래프**("올바른 데이터란 이런 것" 제약 선언) 두 개이고,
출력은 **검증 보고서**(역시 그래프)입니다. 셰이프는 노드 셰이프(node shape)와 프로퍼티 셰이프(property shape,
`sh:path` 로 경로 지정)로 나뉘고, 대표 제약은 `sh:minCount`(최소 개수)·`sh:datatype`(자료형)·`sh:class`(타입)입니다.
→ **OWL 추론(열린 세계, 새 사실 도출)과 달리 SHACL 은 닫힌 세계에서 "규칙 위반 검사"** 를 합니다. RDBMS 의 `CHECK` 제약에 가깝습니다.

**CWA vs OWA — "없음"을 읽는 두 방식**

| | 폐쇄세계가정 (CWA) | 개방세계가정 (OWA) |
|---|---|---|
| 데이터에 없는 사실 | "없다"로 **확정** | "아직 **모른다**" |
| 대표 | RDBMS (`NULL`, `NOT EXISTS`) | RDF/OWL (KG) |

→ `FILTER NOT EXISTS` 결과는 "확실히 없음"이 아니라 **"우리가 아는 한 없음"** 으로 읽어야 합니다 (Ch1).

**OWL RL vs OWL DL, HermiT**
OWL 은 표현력·비용에 따라 프로파일로 나뉩니다. **OWL RL** 은 규칙 기반(빠름) — 본 워크샵 Ch4 추론 엔진.
**OWL DL** 은 완전한 기술논리(느림) — **HermiT**(Java 추론기)로 **논리 일관성**을 검증하며, 특히
**unsatisfiable class**(논리 모순으로 어떤 인스턴스도 가질 수 없는 클래스)를 감지해 T-Box 설계 오류를 잡습니다 (강사 데모).

**SWRL — OWL RL 이 표현 못 하는 규칙**
SWRL(Semantic Web Rule Language)은 변수를 연결한 조건부 규칙(예: "알람 3회 이상 → 잠재 고장")을 적습니다.
OWL RL 로 표현하기 어려운 도메인 규칙용이며, 본 워크샵에서는 선택(opt-in) 기능입니다.

**OntoQA — T-Box 품질을 숫자로 (Tartir et al. 2005)**

| 메트릭 | 뜻 | 정상 범위 |
|--------|-----|:--:|
| **DIT** (Depth of Inheritance Tree) | 가장 깊은 `subClassOf` 체인 깊이 | 2~5 |
| **NOC** (Number of Children) | 클래스당 직접 자식 수 | 2~10 |
| **RR** (Relationship Richness) | ObjectProperty ÷ 전체 프로퍼티 | 0.3~0.6 |
| **AR** (Attribute Richness) | 클래스당 평균 DatatypeProperty 수 | 3~15 |

→ RR 이 낮으면 관계가 빈약한 평면적 스키마, DIT 가 너무 얕으면 분류 체계 부재 신호입니다. Ch3 T-Box 품질 개선이 이 수치를 끌어올립니다.

**ODP · IOF · BFO — 표준에 기대기**
- **ODP**(Ontology Design Pattern): 반복되는 모델링 문제의 재사용 가능한 설계 패턴.
- **BFO**(Basic Formal Ontology): **ISO/IEC 21838-2:2021** 표준 상위 온톨로지. 모든 것을 **continuant**(지속 객체 — 설비)와 **occurrent**(시간에 걸친 과정 — 공정)로 나눕니다.
- **IOF**(Industrial Ontology Foundry): 제조 도메인 공개 참조 온톨로지 협의체. **BFO 를 상위 온톨로지로 채택**해 회사·시스템 간 의미 호환을 보장합니다.

---

# 부록 D: 다음 단계 진입로

본인 상황에 맞는 행을 골라 거기부터:

| 본인이 다음에 하고 싶은 것 | 가세요 |
|---------------------------|--------|
| 워크샵에서 학습한 것 점검 (10~20분) | [부록 A Quick Check](#부록-a-quick-check-워크샵-후-self-study) |
| 본문에서 모르는 용어 만났을 때 | [부록 C 용어 사전](#부록-c-용어-사전) |
| Triple/Turtle/rdf:type/OWL 코드가 낯설 때 | [부록 C-1 심화](#부록-c-1-심화-triple-읽고-쓰기와-rdfrdfsowl-층위) |
| 자사 도메인 적용 첫 30분 (D+1 첫 숙제) | [post-workshop-guide.md §"D+1 첫 숙제"](post-workshop-guide.md#d1-첫-숙제--설정-파일-실제-편집-30분) |
| 자사 PoC 7~8주 청사진 | [post-workshop-guide.md §0 경로 선택](post-workshop-guide.md#0-먼저-당신의-목적지부터-고르세요) |
| T-Box 품질 개선 사이클 깊이 | [post-workshop-guide.md §3](post-workshop-guide.md#3-t-box-품질-개선-사이클) |
| PASS 개수가 정말 건강한가 — Mutation Audit | [post-workshop-guide.md §7-3](post-workshop-guide.md#7-3-advanced-mutation-audit--검증-체계-자체를-감사) |
| OWL RL 못 푸는 변수 규칙 — SWRL | [post-workshop-guide.md "(선택) 고급 기능"](post-workshop-guide.md#선택-고급-기능--깊이-들어가고-싶을-때) |
| Neo4j LPG + Cypher | [post-workshop-guide.md "(선택) 고급 기능"](post-workshop-guide.md#선택-고급-기능--깊이-들어가고-싶을-때) |
| MCP 도구 전체 카탈로그 | [docs/reference/tool-reference.md](../docs/reference/tool-reference.md) |
| 4트랙 학습 경로 | [docs/README.md](../docs/README.md) |
| SPARQL 막힐 때 즉시 참조 | [sparql-cheatsheet.md](sparql-cheatsheet.md) |
| 시맨틱 웹 외부 자료 | https://www.industrialontologies.org/ (IOF) |
