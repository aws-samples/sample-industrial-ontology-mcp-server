# 워크샵 이후 운영 가이드

> **워크샵 패키지 버전:** 4.4 (2026-05-30 — cheatsheet 가독성 reorg: 두 패턴 섹션 분리 / 상단 박스 5→2 압축 / 0건 디버깅 위치 이동 / 방법 A~F 3그룹 재구성 / 엔진 호환성 부록 처리)
> **대상:** 3시간 온톨로지 워크샵을 수료한 파트너 엔지니어
> **목적:** 자체 프로젝트에서 ontology-agent를 독립적으로 운용하기 위한 실전 가이드
> **전제:** 워크샵에서 14단계 코어 파이프라인 (+자동 sub-step 7개 — S4.5/S6.5/S8.5/S9.1/S9.2/S9.5/S12.5) 을 체험했고, `participant-workbook.md`를 보유

## 🎯 이 워크샵의 한 줄 약속 (5개 문서 공통)

> **"당신은 3시간 후 (a) Knowledge Graph 의 가치를 비유 없이 1분 안에 설명할 수 있고, (b) Claude 가 만든 SPARQL 의 정확성을 직접 검증할 수 있고, (c) 자사 도메인 PoC 의 Day 1 에 무엇부터 시작할지 정확히 안다."**

이 한 줄을 D+1 매니저 / D+30 임원 보고에 그대로 인용. 본 post-workshop-guide 는
약속 (c) 의 실현을 위한 운영 가이드입니다.

## 강사 follow-up 매트릭스 — 막혔을 때 어디로 (v3.1)

워크샵 종료 후 막히는 시점별 강사 채널 + 자체 진행 가능 여부:

| 시점 | 막힘 유형 | 1차 채널 | 자체 진행 가능 |
|------|--------|--------|-----------|
| **D+1~3** | 환경 이슈 (Ch1-1 막혔던 사람) | setup-guide §10 강사 이메일/Slack | ❌ — 환경 없으면 진행 불가 |
| **D+7** | 경로 A/B/C 선택 망설임 | 강사 이메일 회신 | ⭕ (이 문서 §0 박스 참조) |
| **D+14** | Week 1 Go/No-Go 미달 (validate_kg < 18/25 또는 CQ < 60%) | 강사 Slack 채널 (D+10 그룹 office-hour 가능) | ⭕ (§3 T-Box 개선 사이클) |
| **D+30** | PoC 결과 회수 시점 | 설문 + 1:1 시연 | (시연 후 다음 iteration 결정) |

> **(v3.1) 그룹 office-hour**: D+10 시점에 N% 이상이 막힘 응답이면 강사가 1시간
> Zoom session 개최. 같은 막힘이 3명+ 면 common pattern → 다음 회 워크샵에 흡수.

## 이 문서는 무엇인가

워크샵은 **철강 샘플로 파이프라인 체험**, 이 문서는 **자사 데이터로 PoC 만들기** 가이드입니다.
목적지 3가지 중 **하나만 골라** 집중하는 것을 권장합니다.

---

## D+1 첫 숙제 — 설정 파일 실제 편집 (30분)

워크샵 다음 날 30분 슬롯에 진행. 자사 도메인 적용 핵심 단계 — 이 실습이 끝나면
"우리 회사 prefix / FK 패턴" 으로 파이프라인이 동작하는 것을 본인 PC 에서 확인.

> **권장 패턴: `domain_config.json` 은 별도 도메인 폴더로 격리** — 메인 `rules/`
> 를 건드리지 않고 새 디렉토리만 사용. 원복 강제 없음 + 학습 효과 손상 없음.
> 단, `fk_patterns.json` 은 `DOMAIN_CONFIG_PATH` 로 리다이렉트되지 않고 항상 메인
> `rules/contracts/` 에서 로드되므로(`RULES_ROOT` 환경변수로만 교체 가능), Step 2 는 메인 `rules/contracts/fk_patterns.json`
> 을 직접 편집하고 실습 후 원복한다.

### 별도 폴더 격리 방법 (권장, 5분)

```bash
# rules/my-domain/ 새 디렉토리 + domain_config.json 복사
mkdir -p rules/my-domain
cp rules/domain/domain_config.json rules/my-domain/domain_config.json

# 환경변수로 격리 (domain_config.json 만 리다이렉트됨)
export DOMAIN_CONFIG_PATH=rules/my-domain/domain_config.json
```

### Step 1 — domain_config.json 편집 (10분)

`rules/my-domain/domain_config.json` 의 다음 필드 변경:

```json
{
  "domain": {
    "name": "My Industry Manufacturing",
    "name_ko": "내 산업 제조",
    "industry": "manufacturing"
  },
  "namespace": {
    "ontology_uri": "http://my-company.com/my-ontology",
    "class_ns": "http://my-company.com/my-ontology#",
    "instance_ns": "http://my-company.com/my-ontology/instances#",
    "prefix": "myind",
    "instance_prefix": "myind-inst"
  }
}
```

> **prefix 주의**: `prefix` 는 SPARQL 축약형(`steel:`)의 앞부분이자 생성 KG 의
> 네임스페이스 기준이다. 비우면 기본값 `ex` 로 폴백하지만, 자사 값으로 바꾸면
> 네임스페이스가 함께 바뀐다. 영문 소문자 + 짧게 (예: `steel`, `pharma`, `fin`).

**verify (JSON syntax 차단):**
```bash
python -m json.tool rules/my-domain/domain_config.json > /dev/null && echo "OK"
```

### Step 2 — fk_patterns.json 편집 (10분)

메인 `rules/contracts/fk_patterns.json` 에 본인 도메인 FK 패턴 추가 (이 파일은 `DOMAIN_CONFIG_PATH`
격리 대상이 아니라 항상 메인 `rules/` 에서 로드됨 — 실습 후 원복):

```json
{
  "patterns": {
    "equipmentid": "EquipmentMaster",
    "tagid": "TagMaster",
    "myrefid": "OtherTable"
  }
}
```

> **키 표기**: 컬럼명을 소문자로 적는다 (예: CSV 의 `Equipment_ID` → `equipmentid`).
> 하이픈/언더스코어가 섞여도 `_fk_column_to_class` 가 폴백으로 맞추지만, 소문자로
> 붙여 쓴 형태가 가장 확실하다. `tools/abox_generation.py` 가 이 dict 를 직접 lookup.

**verify:**
```bash
python -m json.tool rules/contracts/fk_patterns.json > /dev/null && echo "OK"
```

### Step 3 — 파이프라인 체크포인트 초기화 + 영향 확인 (5분)

```
파이프라인 상태를 초기화해줘.
```
→ `reset_pipeline_state` 실행

```
파이프라인 상태를 확인해줘.
```
→ `check_pipeline_state` — 변경 영향이 어디에 미치는지 확인.

### 원복 필수

`fk_patterns.json` 은 메인 `rules/` 를 편집했으므로 실습 후 반드시 원복:
```bash
git checkout rules/contracts/fk_patterns.json
```
`domain_config.json` 도 격리 폴더가 아닌 메인 `rules/` 를 직접 편집했다면 함께 원복:
```bash
git checkout rules/domain/domain_config.json
```
원복 안 하면 다음 사용 시 SPARQL 0건 리턴 (`t_box.ttl` 의 `steel:` prefix·FK 패턴 과
변경된 `class_ns`·`patterns` 불일치).

> **교훈**: **"설정 파일 80% + 환경변수/SME 20%, 코드 수정 0%"** 만 바꾸면 다른
> 도메인 적용 가능. 이것이 파트너 프로젝트의 시작점.

---

## 0. 먼저: 당신의 목적지부터 고르세요

워크샵을 마쳤다고 해서 모든 기능을 한 번에 적용할 필요는 없습니다. 자신의 상황에
맞춰 **하나의 경로**를 선택하고 집중하세요. 다른 경로는 그 다음에.

### 경로 A — 자사 도메인 PoC 구축 (기술 작업 풀타임 2\~3주 / 병행 4\~6주, **+ 비기술 선행조건 1\~3주**)
**이런 사람:** "우리 회사 데이터로 처음부터 온톨로지를 만들어 보고 싶다"
**순서:** 1절 (환경 / CSV / rules — 한 줄 안내 + README 링크) → 2절 (파이프라인 실행) → 3절 (T-Box 개선)
**성공 기준:** `validate_kg` 에서 18/25+ check PASS + `test_domain_queries` 에서 CQ 80%+ 응답
**현실 체크:** 첫 파이프라인 실행은 30\~45분 × 여러 번 반복 + S4 실패 복구 2\~3회 + T-Box 개선 사이클.
- **기술 작업만**: 풀타임 2\~3주, 병행 (하루 2\~3h) 4\~6주.
- **현실 종합 (대기업 기준)**: 풀타임 5\~8주, 병행 8\~16주. 비기술 선행조건이 시간을 잡아먹습니다 (아래 박스).

> **⚠️ 비기술 선행조건 — 임원에 약속하기 전 확인**: 기술 작업 시작 전에 다음
> 항목이 1~3주 분량 별도로 들어갑니다:
> - **AWS IAM / Bedrock 모델 접근 권한** — 회사 IT 승인 (1주 ~ 2주)
> - **자사 SME (도메인 전문가) 합류** — CQ 인터뷰 + tacit knowledge 검증 (2~5일 분산)
> - **CSV 데이터 ownership / 외부 반출 승인** — 일부 회사는 보안 검토 1~2주
> - **이해관계자 정렬** — 어느 도메인부터? 어느 SME 와? PoC 결과 누구에 시연?
>
> 이 작업이 끝난 시점부터 "Week 1 Day 1" 시작. 임원 보고 시 **"기술 5주 + 사전
> 작업 2\~3주 = 7\~8주"** 로 약속해야 신뢰 손상 없음. 풀타임 2\~3주만 약속하면 거의
> 항상 미달.

### 경로 B — RCA 분석 환경 구축 (1주)
**이런 사람:** "Neo4j 이미 쓰고 있는데 RDF 를 LPG 로 연결하고 싶다"
**순서:** 1절 → 2절 (철강 샘플로 1회 전체 파이프라인) → 6절 Neo4j 배포 섹션 →
Cypher/ask_neo4j 실습
**성공 기준:** `ask_neo4j` 로 자연어 RCA 질의가 동작

### 경로 C — 고급 SPARQL / 검증 체계 감사 (1~2주)
**이런 사람:** "기본 파이프라인은 이미 돌려봤고, 검증/쿼리 품질을 끌어올리고 싶다"
**순서:** 1절 → 2절 → `sparql-cheatsheet.md` 심화 → **9절 Mutation Audit**
**성공 기준:** `run_meta_audit` 로 자사 T-Box 의 blind-spot 식별 + 우선순위 결정

> **세 경로를 동시에 하지 마세요.** 하나 끝내고 다음으로 넘어가는 게 실제로 빠릅니다.

---

## 0-1. 경로 A — Week-by-Week Day-by-Day 가이드

풀타임 2\~3주 기준 일정 제안. 병행 업무라면 각 Day 를 2\~3일로 늘리세요.

### Week 1 — 데이터 준비 + 첫 파이프라인 완주

| Day | 작업 | 산출물 | 목표 시간 |
|:---:|------|------|:---------:|
| 1 | CSV 5~10개 선정 (핵심 도메인만) + 컬럼 표준화 | `data/source/rawdata/*.csv` | 4시간 |
| 2 | `"rules 초기화해줘"` → domain_config 자동 생성 + fk_patterns 작성 | `rules/domain/domain_config.json`, `rules/contracts/fk_patterns.json` | 3시간 |
| 3 | 첫 파이프라인 실행 (`"온톨로지 만들어줘"`) | T-Box 초안 + A-Box + validate_kg 결과 | 2~3시간 (대부분 대기) |
| 4 | S4 실패 지점 분석 → T-Box 재생성 (1~2회 반복) | PASS 되는 T-Box | 4시간 |
| 5 | SPARQL 로 자체 CQ 10개 쿼리 시도 → 누락된 프로퍼티 식별 + **SME 와 함께 Golden Query 3~5개 작성** (P0 우선, `add_golden_queries`) | CQ 응답률 리스트 + golden_queries.json | 4시간 |

**Week 1 Go/No-Go 게이트**: `validate_kg` 18/25+ + CQ 응답률 60%+ + **Golden Query 3~5개 100% PASS**
(§0-2 항목 7번) 달성. 미달 시 Week 2 에 데이터 범위 축소 (5개 → 3개 테이블) 고려.

> **Golden Query 작업량 가이드**: Week 1 Day 5 는 **P0 (반드시) 3~5개** 만 작성.
> Workshop Ch5 에서 STL-1\~3 (3개) 변환 체험했으므로 비슷한 규모로 시작. 5\~10개로
> 확장은 D+10 office-hour 후 또는 Week 2 Day 9 에. Workshop 본 모듈에서는 다루지
> 않은 도구이므로 본 가이드 §7-3 (없으면 CLAUDE.md GOLDEN_REGRESSION 워크플로우)
> 참조 후 SME 와 함께 작성.

### Week 2 — 품질 개선 + 테이블 확장

| Day | 작업 | 산출물 | 목표 시간 |
|:---:|------|------|:---------:|
| 6 | 암묵지 추가 (a 자연어 입력 위주) | `data/source/tacit/*.ttl` | 3~4시간 |
| 7 | T-Box 개선 사이클 1회 (DIT/RR/AR 지표 개선) | OntoQA A 등급 T-Box | 4시간 |
| 8 | 테이블 확장 (10 → 20개) + 재실행 | 확장된 KG | 3시간 (재실행 대기) |
| 9 | `test_domain_queries` CQ 80% 달성 위한 보강 | 딕셔너리 + 크로스 도메인 OP | 4시간 |
| 10 | 최종 검증 + HTML 보고서 공유 | pipeline_report.html | 2시간 |

**Week 2 Go/No-Go 게이트**: `validate_kg` 20/22+ + CQ 응답률 80%+. 달성 시 PoC → PoV 준비.

### Week 3 (선택): PoC 결과 정리와 다음 단계 검토

| Day | 작업 |
|:---:|------|
| 11 | Neo4j LPG 배포 실험 (validate_kg 재확인 후) |
| 12 | Mutation audit (`run_tbox_mutation_audit`) 로 검증 체계 blind-spot 점검 |
| 13 | 이해관계자 시연 + 피드백 수집 |
| 14 | 다음 iteration 범위 결정 |

---

## 0-2. PoC 품질 점검 체크리스트

> **이 저장소는 비프로덕션 용도의 샘플 코드입니다.** 아래 점검표는 PoC 결과의 품질을 보는
> 기준일 뿐 프로덕션 준비 상태를 판정하지 않습니다. PoC 결과나 이 샘플에서 출발한 코드를
> 실제 업무에 쓰려면, 그 전에 조직의 자체 보안 검토와 법무 검토 (규제·컴플라이언스 요구사항
> 포함) 를 거쳐야 합니다. 루트 README 의 비프로덕션 안내를 함께 참조하세요.

경로 A 완료 후 PoC 결과를 이해관계자에게 보고하거나 다음 iteration (PoV) 범위를 정하기 전에 점검:

| # | 항목 | 기준 | 측정 도구 |
|:--:|------|------|---------|
| 1 | KG 검증 | 25 check 중 18+ PASS (critical 전부 PASS) | `validate_kg` |
| 2 | 논리 일관성 | unsatisfiable class 0, disjoint 위반 0 | `validate_owl_consistency` |
| 3 | T-Box 품질 | OntoQA 종합 ≥ 80 | `measure_tbox_metrics` |
| 4 | CQ 응답률 | ≥ 80% PASS | `test_domain_queries` |
| 5 | FAIR 점수 | 4축 평균 ≥ 70 | `evaluate_fair_score` |
| 6 | Mutation Audit | blind-spot 파악 + 대응 계획 수립 | `run_meta_audit` |
| 7 | Golden Query 회귀 | SME 검증 P0 CQ 3\~5개 (Week 1) → 5\~10개 (Week 2) 100% PASS | `run_golden_queries` |
| 8 | 이해관계자 승인 | 최소 1명 SME + 1명 운영자 시연 확인 | (조직 내부) |

**8개 모두 ✓ 이면 PoC 품질 기준 충족** (프로덕션 배포 판단이 아님). 7개 이하 달성이면 PoC 연장.

---

## 1. 환경 준비 + CSV + 도메인 설정 — 한 줄 안내

PoC 시작 전 다음 3가지는 **루트 README 와 setup-guide 에 이미 상세 안내**되어
있습니다. 이 문서에서 다시 풀어쓰지 않고 링크로 참조:

| 단계 | 어디 보나 |
|------|----------|
| ✅ 환경 준비 (Python / Java / AWS / Claude Code / .env / MCP 등록) | [setup-guide.md](setup-guide.md) — 워크샵 사전 준비 |
| ✅ CSV 데이터 준비 + PoC 권장 규모 (5~10 테이블) | [docs/reference/configuration.md](../docs/reference/configuration.md) (CSV 배치와 도메인 설정) |
| ✅ 도메인 설정 (`rules/*.json`) | [docs/reference/configuration.md](../docs/reference/configuration.md) + [docs/reference/configuration.md](../docs/reference/configuration.md) |

**이 문서만의 핵심 권장**:
- **5~10 테이블로 시작** — 40 테이블 직진은 잘못된 가정 위에서 시작 시 재작업 비용 3배
- T-Box 1회 생성 15~40분 (모델·테이블 수·수렴 라운드에 따라) × 여러 번 반복이 정상
- SME 1명 + 운영자 1명 검토 인력 확보 (위 0-2 PoC 품질 점검의 8번 항목)

---

## 2. 파이프라인 실행

### 실행 방법

Claude Code에서 한 문장이면 됩니다:

```
온톨로지 만들어줘
```

에이전트가 `check_pipeline_state`로 현재 상태를 확인한 후 14단계를 순서대로 진행합니다.

### ⚠️ 첫 실행에서 실패할 수 있는 지점 (정상적인 상황)

자사 도메인 첫 실행은 **약 10~20% 확률로 S4 검증 단계에서 한 번 막힙니다.** 대부분은
HermiT 가 발견하는 unsatisfiable class (disjoint + domain 충돌) 입니다. 이건 "내가
뭘 잘못했다" 가 아니라 **LLM 이 만든 초안을 다듬는 정상적인 과정**입니다.

| 어디서 실패 | 대처 |
|------------|------|
| S2 T-Box 생성 `ThrottlingException` | 3~5분 기다렸다가 "파이프라인 이어서 진행해줘" |
| S4 HermiT unsatisfiable class | 에이전트가 제시하는 수정안 확인 → 대부분 `disjoint_groups.json` 조정으로 해결 |
| S4 SHACL violation (label 누락 등) | `improve_tbox_quality` 가 대부분 자동 수정. 재검증만 진행 |
| S7 A-Box FK 매칭률 < 50% | `rules/contracts/fk_patterns.json` 에 해당 FK 컬럼 추가 → A-Box 재생성 ([docs/reference/configuration.md](../docs/reference/configuration.md) 참조) |
| S9 KG 검증 FAIL | 4절 트러블슈팅 Top 10 또는 6절 FAIL 항목별 안내 |

**원칙:** 에이전트가 멈춰서 선택지를 제시하면 **그걸 읽고 번호로 응답하세요.** 에이전트의
판단을 일단 따라 보고, 결과가 기대와 다르면 다시 조정. 처음부터 혼자 디버깅하려 하지
마세요 — 에이전트가 만든 진단/수정 제안이 보통 정확합니다.

### 14단계 흐름과 예상 시간

| 단계 | 도구 | 예상 시간 | 사용자 개입 |
|------|------|----------|-----------|
| S0 | `generate_competency_questions` | ~10초 | 생성된 CQ 검토 |
| S1 | `list_csv_tables` + `read_csv_schema` + `generate_csv_erd` | ~10초 | 없음 |
| S2 | `generate_tbox_collaborative` | 15~40분 (모델·테이블 수) | 없음 (대기) |
| S3 | `improve_tbox_quality` | ~5초 | 없음 |
| S4 | 5단계 검증 체인 | ~1분 | FAIL 시 선택지 확인 |
| S5 | `check_tacit_exist` + 4가지 선택지 (자연어 / 규칙 / CSV+LLM / skip) | \~3초\~수분 | 암묵지 입력 방법 선택 ([CLAUDE.md S5 섹션](../CLAUDE.md) 참조) |
| S6 | `visualize_tbox` | ~3초 | 시각적 검토 후 승인 |
| S7 | `generate_abox` | 30~50초 | 없음 |
| S8 | `run_owl_rl_inference` | 6분+ (입력 크기와 로컬 엔진 상태에 따라 변동) | 없음 (대기) |
| S9 | `validate_kg` | 40\~90초 (병합 그래프 `use_inferred=False`) / 5\~8분 (추론 그래프 `use_inferred=True`) | FAIL 시 선택지 확인. 워크샵 Ch4-3 (7분 슬롯) 은 병합 그래프 데모 — 추론 그래프 검증은 PoC 환경에서만 |
| S10 | `generate_semantic_dictionary` | ~15초 | 없음 |
| S11 | `validate_semantic_dictionary` | ~5초 | 없음 |
| S12 | `test_domain_queries` | <1초 | 결과 검토 |
| S13 | `generate_pipeline_report` | ~5초 | 최종 보고서 확인 |

**첫 실행:** 약 30~45분
**재실행 (CSV/T-Box 미변경):** 약 15분 (체크포인트 건너뛰기)

### 체크포인트 규칙

건너뛸 수 있는지는 **`check_pipeline_state` 의 `skippable` 필드만** 보고 판단한다.

"CSV 미변경 → S0/S1/S7 skip" 같은 3축 요약표를 쓰지 않는 이유: 실제 의존 축은 8개
(`csv_mtime` `tacit_mtime` `tbox_mtime` `abox_mtime` `inferred_mtime` `swrl_mtime`
`dict_contract_fingerprint` `lpg_csv_mtime`) 이고 단계마다 다르다. 요약표는 실측에서
**6곳이 전부 "건너뛰어도 된다" 는 잘못된 방향**이었다 — 예를 들어 S7 은 CSV 뿐 아니라
`tbox_mtime` 과 딕셔너리 지문에도 의존하므로, T-Box 만 바뀐 상태에서 S7 을 건너뛰면
낡은 A-Box 가 남는다.

정본은 `tools/pipeline_state.py::_STEP_DEPS` 다. 전체 재실행은 `reset_pipeline_state`.

---

## 3. T-Box 품질 개선 사이클

T-Box 품질은 반복적으로 개선합니다. 한 번에 완벽하지 않아도 됩니다.

### 개선 루프

```
measure_tbox_metrics
     │
     ▼
 점수 확인 (A등급 90+ 목표)
     │
     ├── 점수 충분 → 완료
     │
     └── 점수 부족 ──▶ improve_tbox_quality
                           │
                           ▼
                      5단계 재검증
                      (구문→품질→HermiT→분류→SHACL)
                           │
                           ▼
                    measure_tbox_metrics (재측정)
```

### 메트릭별 개선 방법

| 메트릭 | 낮으면 | 개선 방법 |
|--------|-------|----------|
| DIT (계층 깊이) | 클래스가 평탄하게 나열됨 | `design_patterns.json`에 `abstract_class` 추가 |
| NOC (자식 수) | 한 부모에 자식이 너무 많음 | `sub_groups`로 중간 그룹 도입 |
| RR (관계 풍부도) | OP 비율이 낮음 | `cross_domain_ops`에 도메인 간 관계 추가 |
| AR (속성 풍부도) | 클래스당 DP가 적음 | CSV 컬럼 매핑 누락 확인 |
| Annotation | label/comment 누락 | `improve_tbox_quality`가 자동 보강 |
| Axiom | restriction 부족 | `improve_tbox_quality`가 자동 추가 |

**왜 각 메트릭을 올려야 하는가** (근거):

- **DIT 개선 → 추론 정확도 향상**: 추상 부모 클래스가 생기면 `subClassOf` 전이 규칙이
  활성화돼 "Process_Blast_Furnace 는 Process 이고, Process 는 Activity" 같은 은닉
  사실이 쿼리에 노출됨. 평탄한 구조에서는 각 클래스마다 직접 쿼리해야 함
- **NOC 개선 → 유지보수 용이**: 한 부모에 30개 자식이 있으면 새 자식 추가 시 기존
  30개와 모두 호환 확인 필요. 서브그룹으로 10개씩 묶으면 영향 범위 **1/3 축소**
- **RR 개선 → Cross-domain 쿼리 가능**: CQ 중 "공정 A 의 설비 중 에너지 B 를 쓰는 것"
  같은 질문은 `Process ↔ Equipment ↔ EnergySource` 3-hop OP 경로가 있어야 답변 가능.
  RR < 0.3 이면 이런 cross-domain join SPARQL 자체가 작성 불가
- **AR 개선 → 데이터 보존**: CSV 컬럼이 T-Box DP 에 매핑 안 되면 S7 A-Box 변환에서
  해당 컬럼이 **버려짐**. "측정 타임스탬프" 같은 핵심 컬럼이 빠지면 시계열 쿼리 불가
- **Annotation 개선 → SPARQL 결과 해석 가능**: 결과가 `steel:xQ7A9b` 같은 URI 만
  나오면 사람이 읽을 수 없음. label/comment 있어야 대시보드/보고서에 그대로 출력 가능.
  FAIR 원칙의 R (Reusable) 정량 기준
- **Axiom 개선 → 데이터 품질 자동 검증**: Restriction (예: `someValuesFrom`) 이 있으면
  OWL 추론기가 "이 인스턴스는 반드시 이 관계를 가져야 함" 을 체크. 없으면 SHACL 에서
  추가 검증 shape 을 수동으로 작성해야 함

### 수동 편집이 필요한 경우

자동 개선으로 해결되지 않는 경우:

```
T-Box에 ProcessCleaning 클래스를 추가해줘. ManufacturingProcessStep의 하위 클래스로.
```

에이전트가 `read_tbox` → TTL 수정 → 5단계 재검증을 자동 수행합니다.

### T-Box 재생성 시 자동 피드백

S12 `test_domain_queries` 에서 FAIL 한 CQ 의 `improvement_suggestions`
(`missing_connection` / `missing_instances` / `missing_dp_values`) 는
`data/generated/reports/cq_feedback.json` 에 자동 기록됩니다. **다음 S2
T-Box 재생성 시** Architect 프롬프트에 "이전에 놓친 연결/속성" 섹션이
자동 주입되므로, 사용자는 `"T-Box 다시 만들어줘"` 한 마디로 개선 루프를
진행할 수 있습니다 (수동 프롬프트 작성 불필요).

**Rotation 방지**: 같은 suggestion 이 3번 이상 연속 나오면 `⚠️반복`
badge 가 붙고 S13 보고서에 "반복 실패 패턴" 경고 섹션이 표시됩니다.
LLM 이 해결 못 하는 구조 결함이라는 신호이므로, 수동 T-Box 편집 또는
CQ 재작성을 고려하세요.

**파일 관리**: 7일 이상 된 iteration 은 자동 삭제되고, 최대 10개 저장
됩니다. 전체 리셋이 필요하면 `data/generated/reports/cq_feedback.json`
을 직접 삭제하면 됩니다 (다음 S12 완료 시 재생성).

---

## 4. 흔한 에러 Top 10

| # | 증상 | 원인 | 해결 |
|---|------|------|------|
| 1 | MCP 서버 연결 안 됨 | `.env` 경로 오류, venv 미활성화, `settings.json` 설정 오류 | `.env` 절대경로 확인, `source venv/bin/activate` 실행, `settings.json`의 `command`/`args` 경로 재확인 |
| 2 | `ThrottlingException` (Bedrock) | API 호출 속도 제한 초과 | 2~3분 대기 후 재시도. S2에서 빈번 → 자동 재시도 내장 |
| 3 | `AccessDeniedException` (Bedrock) | 모델 접근 권한 없음 | AWS Console > Bedrock > Model access에서 사용할 모델 접근 요청 |
| 4 | HermiT unsatisfiable class | `AllDisjointClasses`와 OP domain/range 충돌 | `disjoint_groups.json` 조정, 또는 다중 domain을 `owl:unionOf`로 변환. `improve_tbox_quality`가 대부분 자동 해결 |
| 5 | SHACL violation 다수 | label 누락, domain/range 미선언 | `improve_tbox_quality` 실행으로 자동 수정. 잔여분은 에이전트 안내에 따라 수동 수정 |
| 6 | A-Box FK 매칭률 낮음 | FK 컬럼명이 자동 감지 패턴과 불일치, 또는 FK **값** 포맷 변형 (EQ-001 vs EQ001) | ① `rules/contracts/fk_patterns.json`에 해당 패턴 추가 ② ID 포맷이 들쭉날쭉하면 `domain_config.json` 의 `fk_fuzzy_match` 활성화 — `normalized` 는 기본 ON (하이픈/언더스코어/대소문자 흡수), 매칭률이 여전히 낮으면 `levenshtein=true` 로 opt-in. `generate_abox` 응답의 `fk_match_stats` 에서 단계별 히트 수 확인 |
| 7 | OWL 추론 15분 초과 | 트리플 수 과다 (>50만) | `run_owl_rl_inference`에 `fast_mode=True` 전달 → 마스터 테이블만 추론 |
| 8 | KG 검증(S9) FAIL | 고아 노드, FK 무결성 실패, 양방향 OP 누락 | 실패 항목에 따라: T-Box 수정(OP 추가) 또는 A-Box 재생성(FK 패턴 수정) |
| 9 | 질의 테스트(S12) FAIL | 크로스 도메인 OP 부족으로 CQ 경로 단절 | `improve_tbox_quality` 재실행 또는 `design_patterns.json`에 `cross_domain_ops` 추가 |
| 10 | Java 관련 에러 (HermiT/Pellet) | `JAVA_EXE` 경로 오류, Java 미설치, 또는 Java 25 미만 (Pellet 경로의 `UnsupportedClassVersionError`) | `.env`의 `JAVA_EXE` 경로 확인. `java -version`으로 25+ 확인 (HermiT 단독은 11+, Pellet 은 25+). macOS: `brew install --cask corretto@25`, 다른 OS 는 [setup-guide §1.3](setup-guide.md) 의 버전 25 설치 명령 |

---

## 5. 의사결정 트리

### 무엇을 수정해야 하는가?

```
문제 발생
  │
  ├── 스키마(구조) 문제인가?
  │     예 → T-Box 수정
  │     │
  │     ├── 클래스/프로퍼티 추가/삭제/변경
  │     │     → "T-Box에 X 추가해줘" (에이전트가 수정 + 재검증)
  │     │
  │     └── 대규모 구조 변경
  │           → design_patterns.json 수정 → 전체 파이프라인 재실행
  │
  ├── 데이터(인스턴스) 문제인가?
  │     예 → A-Box 재생성
  │     │
  │     ├── FK 매칭 실패
  │     │     → fk_patterns.json 수정 → generate_abox
  │     │
  │     └── CSV 데이터 자체의 문제
  │           → CSV 수정 → generate_abox
  │
  └── CSV에 없는 도메인 지식인가?
        예 → 암묵지 추가 (4가지 방법 중 선택)
        │
        ├── SME 가 말로 설명 가능
        │     → add_tacit_from_natural_language (권장)
        │
        ├── 이미 검증한 패턴을 반복 적용
        │     → rules/domain/tacit_rules.json 작성 → generate_tacit_from_rules
        │
        ├── CSV 에 FK 컬럼 자체가 없음
        │     → augment_csv_fk 로 FK 컬럼 주입 → generate_abox 자동 OP 변환
        │
        ├── SME 도 규칙도 없는 초기 단계
        │     → generate_tacit_from_data (부트스트랩, 결과 검토 필수)
        │
        └── T-Box 변경 필요 (새 클래스/프로퍼티)
              → update_tbox_incremental → S4 재검증 → 암묵지 TTL 작성
```

### `improve_tbox_quality` vs 수동 편집

| 상황 | 방법 |
|------|------|
| OntoQA 점수가 전반적으로 낮음 | `improve_tbox_quality` (스텝 0~30 자동 후처리) |
| 특정 클래스 1~2개 추가/수정 | "T-Box에 X 추가해줘" (에이전트 수동 편집) |
| domain/range 변경 | 수동 편집 (자동 후처리가 덮어쓸 수 있음) |
| disjoint 그룹 조정 | `disjoint_groups.json` 수정 → `improve_tbox_quality` |
| 크로스 도메인 OP 추가 | `design_patterns.json` 수정 → `improve_tbox_quality` |

### 전체 재실행 vs 부분 재실행

| 상황 | 실행 방법 |
|------|----------|
| CSV 데이터 교체 | `reset_pipeline_state` → 전체 재실행 |
| CSV 컬럼 추가 (테이블 구조 변경) | `update_tbox_incremental` → S4~S13 |
| T-Box 소규모 수정 | S4(검증)부터 재개 |
| A-Box만 재생성 | S7부터 재개 (`generate_abox`) |
| 암묵지만 추가 | S5에서 TTL 추가 → S8(추론)부터 재개 |
| 설정 파일(rules/) 변경 | `reset_pipeline_state` → 전체 재실행 |

---

## 6. 파이프라인이 끝난 다음: 산출물 해석

파이프라인이 "완료" 라고 해서 곧 "준비 완료" 는 아닙니다. 몇 가지를 **반드시 확인**하세요.

### 6-1. 품질 체크포인트 (3분)

```
KG 상태 확인해줘
T-Box 품질 점수 확인해줘
CQ 테스트 결과 보여줘
```

| 확인 항목 | 합격선 | 미달 시 |
|-----------|--------|---------|
| `validate_kg` 25 check 중 PASS | 18/25 이상 | 3절 (T-Box 개선) 재진행 |
| T-Box OntoQA 점수 | 70+ (A는 90+) | `improve_tbox_quality` 재실행 |
| CQ 응답률 (`test_domain_queries`) | 80%+ | `design_patterns.json` 에 cross_domain_ops 보강 |
| 고아 노드 비율 | < 5% | FK 매칭 재점검 (`fk_patterns.json`) |

### 6-2. 시각적 검토 (5분)

```
T-Box 시각화 보여줘
pipeline_report.html 보여줘
```

**체크포인트:**
- 클래스 계층이 평평하지 않은가? (DIT ≥ 2)
- 도메인 간 연결(cross_domain_ops) 이 의미 있게 보이는가?
- 예상한 클래스들이 모두 있는가?

기대와 다르면 경로 A 의 3절 (T-Box 개선 사이클) 로 돌아갑니다.

---

## 7. 다음 단계

### 7-1. 외부 RDF 스토어 배포 (선택)

로컬에서 검증이 완료된 TTL 산출물 (`data/generated/tbox/t_box.ttl`,
`data/generated/abox/a_box.ttl`, `data/generated/inferred/all_inferred.ttl`,
`data/generated/abox/abox_provenance.ttl`) 은 외부 SPARQL 엔드포인트의
import 도구로 직접 적재할 수 있습니다. 본 프로젝트는 외부 스토어 직접
통합은 제공하지 않으며, 일상 쿼리는 `sparql_local` 로 충분합니다.

### 7-2. Neo4j LPG 배포 (RCA 분석용)

RDF Knowledge Graph를 Neo4j에 적재하여 그래프 시각화와 자연어 Cypher 쿼리를 사용합니다:

```
LPG로 변환해줘.
```

에이전트가 아래 3단계를 순서대로 진행합니다:

```
N1. convert_rdf_to_lpg               → RDF→LPG CSV 변환 + 5차원 품질 점수
N2. generate_lpg_semantic_dictionary  → Neo4j용 시맨틱 딕셔너리 생성
N3. neo4j_deploy_lpg                 → APOC 배치 적재
```

**사전 조건:** Neo4j Docker 실행 필요. 비밀번호는 직접 정하고 포트는 127.0.0.1 (이 PC) 에만
엽니다 (setup-guide §7 과 같은 절차).

```bash
# 비밀번호를 직접 정해 환경변수 NEO4J_PASSWORD 에 둔다 (화면·셸 기록에 남지 않음, 8자 이상)
printf 'Neo4j 비밀번호 (8자 이상): '; stty -echo; read -r NEO4J_PASSWORD; stty echo; echo
export NEO4J_PASSWORD

# 127.0.0.1 에만 바인딩해 같은 네트워크의 다른 PC 가 접속하지 못하게 한다
docker run -d --name neo4j-rdf -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
  -e NEO4J_AUTH="neo4j/${NEO4J_PASSWORD}" -e NEO4J_PLUGINS='["apoc"]' \
  -e NEO4J_server_memory_heap_max__size=2G neo4j:5-community
```

`.env` 의 Neo4j 항목을 채웁니다. `NEO4J_PASSWORD` 에는 위에서 정한 값을 그대로 씁니다:
```
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<위 NEO4J_PASSWORD 환경변수에 넣은 값>
```

**자연어 질의:**
```
정비 이력이 가장 많은 설비 상위 5개는?
```
→ `ask_neo4j`가 LPG 시맨틱 딕셔너리를 참조하여 Cypher를 자동 생성하고 실행합니다.

**Neo4j Browser:** http://127.0.0.1:7474 에서 그래프를 시각적으로 탐색할 수 있습니다
(ID: neo4j / PW: 위 `NEO4J_PASSWORD` 환경변수에 넣은 값).

### 7-3. (Advanced) Mutation Audit — 검증 체계 자체를 감사

파이프라인의 22 validate_kg check 가 "PASS" 라고 보고해도, **그 check 들이 실제로
잘못된 데이터를 잡아낼 수 있는지** 는 별개 문제입니다. Mutation audit 은 의도적으로
결함을 주입한 T-Box 를 만들어 validator 가 얼마나 감지하는지 측정합니다.

**왜 이게 중요한가** (일반 검증과 다른 층위):

일반 검증: "현재 데이터에 결함이 있나?"
Mutation audit: "**검증기가 결함을 잡아낼 능력이 있나?**"

비유: 자동차 브레이크 점검
- 일반 검증 = "지금 브레이크가 작동하나요?" (네)
- Mutation audit = "**고장 났을 때 감지 시스템이 경고 울릴까요?**" (이게 더 중요)

Software Engineering 의 **mutation testing** (DeMillo et al. 1978) 원리 —
프로덕션 코드에 인위적 버그를 심고 테스트 스위트가 잡는지 측정. "테스트 커버리지
100%" 도 버그의 40~60%만 잡는다는 실증 연구 결과가 있어, coverage 가 아닌
**mutation score** 가 품질의 진짜 척도라는 합의가 있음.

우리 프로젝트에 적용: "validator PASS 개수가 진짜 건강한 KG 인가?
아니면 validator 가 보는 것만 보는가?"

**언제 실행:**
- PoC 결과를 이해관계자에게 보고하거나 다음 iteration 범위를 정하기 전
- 중요한 의사결정에 KG 를 쓰기 전 ("이 분석 결과를 경영진에 보여도 되나?")
- T-Box 대규모 변경 후

**실행:**
```
T-Box mutation 감사 실행해줘
```
→ `run_tbox_mutation_audit` (3~5분, WARN-only). 21 mutant (7 카테고리 × 3 패턴) 를
  T-Box 에 주입 후 S4 validator 5개가 각각 얼마나 잡는지 측정.

```
KG mutation 감사 실행해줘
```
→ `run_kg_mutation_audit` (~60초). KG-전파 mutant 8개를 샘플링해 `validate_kg` 의
  실제 검출력 측정.

```
Meta-audit 실행해줘
```
→ `run_meta_audit`. 위 두 결과 + 기존 quality_history 를 집계해 다음을 리포트:

| 섹션 | 의미 | 활용 |
|------|------|------|
| **Sensitivity matrix** | check × category 별 catch 율 | 어떤 종류 결함을 잘 / 못 잡는지 |
| **Blind-spots** | 아무 check 도 못 잡은 mutator 카테고리 | 신규 check 추가 우선순위 |
| **Dead-checks** | 한 번도 발동 안 한 check | 제거 후보 (감산 판단) |
| **Pairwise correlation** | 동시 FAIL 하는 check 쌍 | 중복 후보 (감산 판단) |
| **ROI column** | check 당 (weighted catches / 실행 시간) | 비용 대비 가치 낮은 check |

**예시 실제 결과 (2026-04-27, 철강 샘플):**
- **M4 disjoint blind-spot 2/2 uncaught** → AllDisjointClasses 위반 감지 check 신설 필요
- **M5 annotation blind-spot 1/3 uncaught** → 라벨을 URI 문자열로 치환한 mutant 놓침

**출력 위치:** `data/generated/meta_audit/latest.json` + S13 보고서 맨 아래 섹션.

**주의:**
- WARN-only 이므로 파이프라인을 FAIL 시키지 않습니다.
- dead-check / correlation 판정은 quality_history 가 5회 이상 누적되어야 의미 있습니다
  (초반에는 `insufficient_history` 로 표시됨). 단 그 5건은 **`failed_checks` 를 담은
  엔트리** 여야 합니다 — `save_step` 이 남기는 `{step, metrics}` 스키마는 신호가 없어
  분모에 세지 않습니다.
- **`dead_checks` 의 판정은 4가지이고 그중 하나만 제거를 뜻합니다**:

  | 판정 | 의미 | 해야 할 일 |
  |------|------|-----------|
  | `insufficient_exposure` | 그 check 를 자극하는 mutant 노출이 부족 | mutation 커버리지를 늘린다 (제거 검토 대상 아님) |
  | `structurally_undetectable` | baseline 이 이미 FAIL → 등급 상승 불가 | **baseline 을 먼저 고친다** (check 잘못이 아니다) |
  | `no_probe_available` | 그 축의 mutant 가 카탈로그에 없음 (`syntax` 등) | mutation 카테고리를 추가한다 |
  | `consider_removal` | 충분히 노출 + baseline 건강 + 그래도 0 catch | 그때 비로소 제거를 검토한다 |

  노출은 **온톨로지를 강화(트리플 추가)하고 그 check 와 관련된 카테고리** 의 mutant만
  셉니다. OWL 은 단조 논리라 공리 삭제로는 HermiT 가 unsatisfiable 을 보고할 수 없고,
  M5(label) mutant 를 30번 돌린 것이 FK 게이트를 시험한 것은 아니기 때문입니다.

### 7-4. 실시간 센서 데이터 연동

IoT/SCADA 센서 데이터를 RDF로 변환하여 KG에 추가:

```
센서 데이터를 RDF로 변환해줘.
```

`convert_sensor_to_rdf` 도구가 JSON/CSV 센서 데이터를 SSN/SOSA 온톨로지 기반 RDF로 변환합니다.

### 7-5. 참조 문서

| 문서 | 위치 | 용도 |
|------|------|------|
| SPARQL 치트시트 | `workshop/sparql-cheatsheet.md` | 실전 쿼리 패턴 모음 |
| docs/ 진입점 | `docs/README.md` | 4트랙 (입문/레퍼런스/문제해결/배경) 학습 경로 |
| 도메인 예시 (철강) | `docs/examples/steel-domain.md` | T-Box/A-Box 규칙, IOF 매핑 |
| 도구 참조 | `docs/reference/tool-reference.md` | 도구별 용도, 워크플로우 레시피 |
| 트러블슈팅 | `docs/reference/troubleshooting.md` | 에러별 원인/해결책 |
| 4트랙 학습 진입점 | `docs/README.md` | 입문 / 레퍼런스 / 문제해결 / 배경 트랙 + 용어 참조 |

---

## 8. 안티패턴 — "이렇게 하지 마세요" 5가지

운영 단계에서 반복 관찰된 **실패 패턴** 입니다. PoC 시작 전에 훑어두면 시간 절약.

### 8-1. T-Box 수동 편집 후 `improve_tbox_quality` 재실행

**증상:** 직접 편집한 내용이 사라짐.
**원인:** `improve_tbox_quality` 는 후처리 스텝 전체를 *재적용* 하므로 수동 편집을 덮어씀.
**올바른 방법:**
- 수동 편집이 필요하면 **`update_tbox_incremental`** 사용 (변경 영향 자동 검증)
- 또는 개선 로직을 `rules/domain/design_patterns.json` / `rules/domain/disjoint_groups.json` 등에 반영해 파이프라인이 재현하도록

### 8-2. 한 번에 40개 테이블 투입

**증상:** S2 T-Box 생성이 30분+ 걸리고, Multi-Agent 토론이 수렴 안 함. S4 실패율 40%+.
**원인:** LLM context window 초과, Multi-Agent 가 40 테이블 관계를 한 번에 토론 불가.
**올바른 방법:**
- **5~10개 핵심 테이블로 시작** → 통과 후 10 → 20 → 40 확장
- Week 1 는 3 도메인 (설비/공정/품질 등) 만 집중

### 8-3. `disjoint_groups.json` 에 모든 클래스 나열

**증상:** HermiT 추론에서 unsatisfiable class 폭증. S4 검증 실패.
**원인:** 모든 클래스 쌍을 disjoint 로 선언 → 실제 데이터가 두 클래스에 동시 속하면 모순.
**올바른 방법:**
- **조심스럽게** disjoint 선언 — "정말로 동시 속할 수 없는" 쌍만
- 예: "설비 ≠ 공정" OK, "EquipmentMaster ≠ Tag" 는 위험 (센서가 설비의 일부일 수 있음)

### 8-4. 암묵지 TTL 을 손으로 작성 후 파이프라인 재실행 안 함

**증상:** 시각화/쿼리에 암묵지 내용이 안 보임.
**원인:** `data/source/tacit/*.ttl` 추가해도 S8 OWL 추론을 다시 돌리지 않으면 역방향 트리플이 생성 안 됨.
**올바른 방법:**
- TTL 추가/수정 후 `"추론 다시 돌려줘"` 또는 `reset_pipeline_state` → 전체 재실행
- 또는 `sparql_local_reload` 로 캐시 리프레시

### 8-5. KG 가 자사 산업에 적합하지 않은 5가지 시그널

워크샵은 "철강 도메인 + class-specific DP / FK / OWL 추론" 이 잘 맞는 케이스로 진행.
그러나 일부 산업/도메인은 본질적으로 KG 와 안 맞을 수 있음. **honest 평가**:

| # | 시그널 | 권장 대안 |
|:-:|------|---------|
| 1 | CSV 의 70%+ 가 비정형 텍스트 (자유서술, 진료기록, 고객문의 등) | **벡터 검색 엔진** (vector database) + LLM RAG. KG 는 entity / relation 위주 |
| 2 | FK 비율 < 5% (entity 간 관계가 약함). 주로 transaction 로그/이벤트 스트림 | **시계열 데이터베이스** (time-series database) + 분석. KG 의 RR 메트릭이 안 올라감 |
| 3 | 응답속도 우선 (p99 < 100ms) — 실시간 추천/광고 입찰 등 | **graph DB native** (Neo4j Cypher) 또는 캐시. OWL 추론 비용 부담 |
| 4 | 규제나 계약 조건 때문에 도메인 간 데이터 격리가 필요할 수 있음 (예: 의료·금융 데이터) | 하나의 통합 KG 를 전제로 하지 말고 도메인별 격리된 별도 KG 또는 federation 을 검토. 어떤 격리가 필요한지는 자사 컴플라이언스 팀과 확인 |
| 5 | 데이터 갱신 빈도 > 10 records/sec | 추론 재실행 비용 폭증. KG 는 batch 갱신 환경에 최적 |

> **이 워크샵의 가장 honest 한 결론은 "우리 도메인에 KG 가 안 맞다"** 일 수도 있습니다.
> 그것도 가치 있는 결론 — 7~8주 PoC 비용을 사전 회피. 위 5가지 시그널 중 2개+ 해당
> 하면 PoC 진행 전 강사와 적합성 자문 (D+1~7 channel) 권장.

### 8-6. 외부 스토어/Neo4j 에 `validate_kg` 없이 배포

**증상:** 적재한 그래프 스토어에서 엉뚱한 쿼리 결과 (0건 또는 비정상 많은 건수).
**원인:** 로컬 검증 없이 배포 → FK dangling, functional 위반, disjoint 위반이 남아 있음.
**올바른 방법:**
- 배포 직전 **반드시 `validate_kg(use_inferred=True)`** 재실행 (18/25+ PASS)
- critical check 3종 (functional/disjoint/subclass_cycle) 은 반드시 PASS
- PoC 품질 점검 체크리스트 (위 0-2절) 전항목 확인

---

## 9. Mutation Audit 결과 액션 플레이북

`run_meta_audit` 로 blind-spot 발견 시 구체 대응 3단계.

> **TIP**: `run_meta_audit` 결과 JSON 의 `action_playbook` 필드를 참조하면
> 아래 플레이북이 **우선순위별 (high/medium/low) 기계 판독 가능한 entry** 로
> 자동 제시됩니다. S13 보고서 (`pipeline_report.html`) 에도 해당 섹션이
> 자동 렌더되므로 수동 해석 필요 없습니다.

### Step 1 — 발견된 blind-spot 분류

결과 JSON 의 `blind_spots` 배열을 보고 아래 3가지로 분류:

| 유형 | 증상 | 예시 |
|------|------|------|
| **Dead check** | validator 가 아무 mutant 도 catch 안 함 | M5 annotation 1/3 uncaught |
| **Partial blind-spot** | 특정 카테고리만 놓침 | M4 disjoint 2/2 uncaught |
| **Correlation gap** | 두 check 가 항상 같이 PASS/FAIL (중복) | fk_ref 과 dangling 이 상관계수 0.98 |

### Step 2 — 대응 방법 선택

| 유형 | 대응 |
|------|------|
| Dead check | **validator 코드 확인** — 실제로 트리거 조건이 맞는가? `rules/mutations/M5_annotation/*.sparql` 을 읽어 기대 동작과 비교 |
| Partial blind-spot | **신규 check 추가 고려** — `tools/validation_support/checks/` 하위에 추가. 또는 기존 check 의 threshold 조정 |
| Correlation gap | **check 통합 고려** — 한쪽을 deprecated 처리하거나 한 번만 호출. 총 check 수를 줄여 실행 시간 단축 |

### Step 3 — 회귀 방지

- 수정 후 `run_tbox_mutation_audit` 재실행 → 해당 mutant 이 이제 catch 되는지 확인
- `get_meta_audit_history` 로 trend 관찰 (blind-spot 수가 매 실행마다 감소하는 방향?)

### 실제 첫 라이브 사례

- **발견**: M4 disjoint 2/2 uncaught (disjoint_class_violations check 의 탐지 범위 제한)
- **대응**: `check_disjoint_class_violations` 에 간접 추론 (subClassOf 를 통한 disjoint 전파) 추가
- **결과**: 다음 audit 에서 M4 100% catch, blind-spot 2 → 0

---

## 10. FAQ — 자주 하는 질문

### 구축 단계

**Q: CSV 에 FK 컬럼이 없어요. 어떻게 관계를 만들죠?**
A: 3가지 방법. (1) `augment_csv_fk` 로 결정적 FK 컬럼 주입 (2) 암묵지 TTL 로 관계 명시
(3) `rules/domain/tacit_rules.json` 에 `via_mapping_chain` strategy 작성. 구조적으로 없으면 CSV
자체를 재검토.

**Q: FK 컬럼은 있는데 값 포맷이 ERP 와 달라서 매칭 안 돼요 (예: CSV 는 `EQ-001`, master 는 `EQ001`).**
A: `rules/domain/domain_config.json` 의 `fk_fuzzy_match` 를 활용. `normalized` 는 기본 ON 이라
하이픈 / 언더스코어 / 도트 / 공백 / 대소문자 차이는 자동으로 흡수됩니다 (NFKC 정규화로
fullwidth `ＥＱ－００１` 이나 em/en dash `EQ—001` 도 함께 흡수). 그래도 매칭률이
낮으면 `levenshtein=true` 로 opt-in (edit distance ≤ 1, 예: `EQ001` ↔ `EQ01`), 또는
`prefix=true` (모호하지 않은 prefix 일치). `generate_abox` 응답 JSON 의 `fk_match_stats` 에
`{exact, normalized, levenshtein, prefix, unresolved}` 카운터가 나오니 어느 단계에서 몇 건이
복원됐는지 확인. unresolved 가 많으면 CSV 자체의 FK 값을 재검토.

> **prefix 매칭 주의**: `prefix: true` 로 켜도 false positive 위험이 있습니다.
> 새 ID (`EQ0013` 같은) 가 기존 master (`EQ001`) 에 매칭될 수 있으므로 반드시
> `fk_match_stats.prefix` 카운터와 로그를 확인하세요. 3글자 이상 source 만
> 매칭되고, source 가 master 의 짧은 prefix 인 방향만 허용합니다.

**Q: 암묵지와 T-Box 수정 중 어느 게 먼저?**
A: **암묵지 먼저**. T-Box 변경은 A-Box 전체 재생성을 유발 (40~80초). 암묵지는 TTL 파일
추가만 하면 됨 (< 1초). 암묵지로 해결 안 되면 T-Box 수정.

**Q: 테이블을 늘리면 기존 T-Box 는 어떻게 되나요?**
A: `update_tbox_incremental` 로 증분 업데이트. 전체 재생성 (`reset_pipeline_state`) 보다
빠르고 기존 클래스/OP 보존. 단 Multi-Agent 토론 없이 단순 추가라 품질은 약함 — 주요
변경 후엔 `improve_tbox_quality` 재실행 권장.

**Q: 시계열 테이블 (Timestamp 포함) 의 tacit 규칙 작성 시 주의점?**
A: 최근 업데이트(2026-05-04) 이후 **uniqueness 기반 자동 감지**로 동작합니다. 전략 함수가
`source_pk_column` 값이 CSV 전체에서 unique 한지 선검사하고, A-Box 의 동일 판정
(`pk_is_unique_single` 브랜치) 과 맞춰 timestamp 접미 여부를 자동 결정합니다.
- **unique PK 테이블** (Alarm_Events 의 `Event_ID` 등) → A-Box 가 ts 를 생략하므로
  tacit 도 `auto_timestamp=False` 로 전달 → `AlarmEvents_EVT00001` (일치).
- **중복 PK 테이블** (`Product_ID` 가 여러 row 에 중복되는 시계열) → A-Box 폴백
  경로가 ts 를 덧붙이므로 tacit 도 ts 접미 유지 → `Class_P001_2025-...`.
- **여전히 명시 권장하는 경우**: 엄격 검증 / 결정적 재현 / 여러 timestamp 컬럼
  중 선택 필요. `"source_composite_pk": ["Product_ID", "Timestamp"]` 형태.
- **자동 감지 패턴**: `timestamp`, `datetime`, `measurementdatetime`,
  `testdatetime`, `occurrencedatetime`, `departuretime`, `generationdate`,
  `maintenancedate`, `orderdate`, `plandate` (substring 매칭 포함).
- `suggest_tacit_rules` 의 LLM 프롬프트에는 시계열 테이블 목록이 자동 삽입되어
  LLM 이 초안에서 composite PK 를 포함하도록 유도. `generate_tacit_from_rules`
  실행 시에도 시계열 감지 + 누락 상황을 INFO 로그로 기록.

### 검증 단계

**Q: 자사 도메인에서 `validate_kg` 가 15/22 나왔어요. 재생성해야 하나요?**
A: **어떤 check 가 FAIL 인지가 중요**. critical 3종 (functional/disjoint/subclass_cycle) 은
무조건 수정. structural 실패는 T-Box 재설계, statistical 실패는 CSV 품질 문제.
PoC 품질 점검 체크리스트 (0-2절) 의 1~3 항목이 통과되면 PoV 범위 검토 가능.

**Q: OntoQA 점수 70 도 안 돼요.**
A: T-Box 개선 사이클 (3절 T-Box Improvement) 1~2회 돌리면 대부분 80+ 도달. 그래도
안 되면 `rules/domain/design_patterns.json` 의 도메인 계층을 단순화 (중간 추상 클래스 제거).

**Q: CQ 80% 못 넘는데요?**
A: `test_domain_queries` 의 FAIL 유형 확인. (1) 인스턴스 누락 → CSV 보강, (2) 경로 없음 →
`design_patterns.json` 에 크로스 도메인 OP 추가, (3) DP 값 없음 → `fk_patterns.json` / CSV
null 값 확인.

### 운영 단계

**Q: KG 를 만든 뒤 원천 CSV 가 변경되면?**
A: 대부분은 **A-Box 만** 재생성 (T-Box 불변). `reset_pipeline_state` + S7 이후만 실행.
스키마 수준 변화 (새 컬럼 등장) 는 `monitor_csv_drift` 로 자동 감지되며, 중대한 변화면
전체 재실행.

**Q: Neo4j 와 외부 RDF 스토어 동시 배포는?**
A: 가능. `convert_rdf_to_lpg` + `neo4j_deploy_lpg` 는 외부 RDF 스토어와 무관.
단 두 스토어의 동기화는 매뉴얼 — 배포 시 `verify_cross_store_parity` 로
sparql_local 결과와 Cypher 결과의 수치 일치 확인.

**Q: Bedrock 비용이 걱정돼요.**
A: 이 문서는 고정 금액을 제시하지 않습니다. 비용은 사용 모델, 리전, 입력·출력 토큰 수에
따라 달라지므로 다음 호출 횟수에 [Amazon Bedrock 요금 페이지](https://aws.amazon.com/bedrock/pricing/)
의 현재 단가를 적용해 계산하세요. Bedrock 호출은 S0 CQ 자동 생성 (1회) 과 S2 T-Box 생성
(Multi-Agent 토론, 8~12회) 에 몰려 있습니다. T-Box 는 생성 후 재사용하므로 S2 를 반복하지
않으면 호출이 늘지 않습니다. A-Box 재생성 (S7~S9) 은 LLM 호출 0회입니다. 공통 prefix 를
재사용하는 호출에는 prompt caching (5분 TTL) 이 적용되어 반복 호출의 입력 비용을 줄입니다.

**Q: 자사 SME 가 TTL 을 못 읽어요.**
A: `generate_semantic_dictionary` → JSON 형태로 클래스/프로퍼티 설명. 한국어 rdfs:label
/ rdfs:comment 로 설명 붙이면 더 친숙. `visualize_tbox` HTML 시각화 공유.

**Q: 워크샵에서 본 Multi-Agent 토론을 로그로 보려면?**
A: `/tmp/ontology-agent-server.log` 에 라운드별 Architect/Validator/SME 발화 기록.
`MULTI_AGENT_DETERMINISM=strict` 환경변수로 재현 가능 모드.

**Q: MCP 서버가 자꾸 죽어요.**
A: Python 메모리 문제일 가능성. 40 테이블 + 추론 후 graph 객체가 2~3GB. `RDFLIB_STORE=default`
(Memory store) 대신 기본 Oxigraph 가 더 효율적. venv 재생성 + `pip install -r requirements.txt --force-reinstall`.

## (선택) 고급 기능 — 깊이 들어가고 싶을 때

PoC 첫 사이클을 마친 뒤 더 알아두면 좋은 기능들. **루트 README 와 docs 에서
이미 다루므로 한 줄 안내만**:

| 기능 | 언제 쓰나 | 어디 보나 |
|------|----------|----------|
| **S0 부터 전체 파이프라인 재실행** | 워크샵에서 체크포인트 건너뛰기로 일부만 봤다면, 자사 도메인은 처음부터 한 번 끝까지 돌려보기 권장 (30\~45분) | `"전체 파이프라인을 처음부터 시작해줘"` → S0\~S13 자동 진행 |
| **Neo4j LPG 변환 + 자연어 Cypher** | RDF KG → Neo4j 로 배포해 그래프 시각화 + RCA. 사전 조건: `docker ps \| grep neo4j` 실행 중 | [setup-guide.md](setup-guide.md) §7 "(선택) Neo4j" + 루트 README "Neo4j LPG 변환 워크플로우" |
| **SWRL 규칙 추론** (opt-in) | "알람 3회 → PotentialFailure" 같은 변수 연결 규칙. OWL 2 RL 표현력 너머. 사전 조건: `SWRL_ENABLED=true` + MCP 서버 재시작 | `rules/swrl/README.md` |
| **추론 triple 품질 감사** | S8 추론 결과의 trivial / suspicious / meaningful 3분류 | 루트 README "자연어 명령 카탈로그 C" 의 `classify_inference_triples` |
| **CSV drift 증분 업데이트** | CSV 주기 갱신 환경에서 변경 테이블만 T-Box 증분 업데이트 | 같은 카탈로그의 `run_partial_pipeline_on_drift` |
| **한국어 동의어 사전** (opt-in) | `ask_ontology` 가 한국어 도메인 약어를 클래스명에 매핑 못 할 때 | 루트 README "다른 산업에 적용하기 → 한국어 동의어 사전" |
