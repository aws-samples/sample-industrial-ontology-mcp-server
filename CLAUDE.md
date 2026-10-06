# CLAUDE.md — Ontology Agent

이 프로젝트는 도메인-중립 Knowledge Graph 라이프사이클을 위한 Claude Code MCP
서버입니다. `rules/domain/domain_config.json` (또는 환경변수 `DOMAIN_CONFIG_PATH`) 에
자사 도메인 설정을 주입하면 제조/금융/의료 등 어떤 산업에도 재사용 가능합니다.
개발자가 SPARQL 조회, T-Box/A-Box 검증, 추론 트리거 같은 온톨로지 작업을
Claude Code에서 직접 실행할 수 있습니다.

현재 리포에 포함된 기본 설정은 철강 제조 예시 데이터(`data/source/rawdata/`)
와 그에 맞는 `rules/domain/domain_config.json` 입니다. 새 도메인으로 전환하려면
`rules/domain/domain_config.example.json` 을 복사해서 ontology URI, prefix, 예시
테이블명을 교체하세요.

## 실행 방법

```bash
cd /path/to/ontology-agent
source venv/bin/activate
python server.py
```

## 참조 문서

도메인 지식, 도구 상세, 트러블슈팅은 별도 파일로 분리됨. 필요 시 `Read`로 로드:

| 문서 | 경로 | 언제 참조 |
|------|------|----------|
| docs/ 진입점 | `docs/README.md` | 4트랙 (입문/레퍼런스/문제해결/배경) 학습 경로 + 용어 참조 |
| 도메인 예시 | `docs/examples/steel-domain.md` | 철강 샘플 기반 T-Box/A-Box 규칙, IOF 매핑, FK 감지 로직 |
| 도구 참조 | `docs/reference/tool-reference.md` | 도구 용도/분류, 워크플로우 레시피 |
| 트러블슈팅 | `docs/reference/troubleshooting.md` | 에러 발생 시 원인/해결책 |
| 단계별 상세 | `docs/concepts/pipeline-details.md` | 단계별 주요 도구, 사용자 선택, 장시간 잡, composite key |
| **Task 코드 용어집** | `docs/reference/task-glossary.md` | 주석·로그·아래 본문의 `R1`/`T2`/`I2`/`A4`/`X1` 등 작업 식별자 ↔ 기능 매핑 |

> ℹ️ 아래 본문과 소스 주석의 `R1`, `T2`, `I2`, `A4`, `X1` 같은 영숫자 코드는 과거
> 작업(Task) 식별자다 (기능명이 아니라 "언제 도입됐는지" 라벨). 의미는
> [docs/reference/task-glossary.md](docs/reference/task-glossary.md) 참조.

코드에서 직접 확인 가능한 정보:
- 네임스페이스/PREFIX → `domain/namespaces.py`
- URI 컨벤션 → `domain/uri_conventions.py`
- SPARQL 템플릿 → `domain/sparql_templates.py`
- 프로젝트 구조 → `ls`, `Glob` 도구 사용
- 도구 파라미터 → MCP 도구 docstring 자동 제공
- 환경변수 → `docs/reference/environment-variables.md` / `.env.example` / `config.py`.
  ⚠️ 세 곳을 합쳐도 **코드가 읽는 것의 절반 남짓**이다 (실측: 코드 74 vs 문서화 39).
  게이트 스위치 (`TBOX_OP_GROUNDING_GATE`, `S2_CQ_MAX_HOPS`, `CSV_DATE_ORDER` 등) 가 주로
  빠져 있으니 전수는 `grep -rn 'getenv(' tools/ domain/` 로 확인한다.

### rules/ 경로는 공용 해석기만 사용 (사본 금지)

`rules/` 는 성격별 하위 폴더 3개로 나뉜다. 경로를 직접 조립하지 말고
`domain/rules_paths.py` 의 함수만 쓴다:

| 용도 | 함수 |
|------|------|
| 파일 경로 | `rules_path("domain_config.json")` → `rules/domain/domain_config.json` |
| 전체 JSON 열거 | `rules_json_glob()` (하위 폴더 포함) |
| 카테고리 판정 | `category_of(filename)` / 이식 교체 목록 `REPLACE_ON_NEW_DOMAIN` |

| 폴더 | 성격 | 새 도메인 |
|------|------|-----------|
| `domain/` | 도메인 자산 (12) | **전부 교체** |
| `policy/` | 엔진 정책 (4) | 유지 |
| `contracts/` | 도메인-중립 계약 (5) | 템플릿에서 재생성 |

**왜**: 경로 조립 지점이 **129곳**, 모듈별 `_RULES_DIR` 정의가 13곳이었다. 한 곳만
놓치면 `FileNotFoundError` → `except` → **빈 dict 폴백** 이라 게이트가 꺼진 채
통과한다. `glob("rules/*.json")` 은 하위 폴더 도입 후 **0건** 이 되는데, step_22e/22f
는 그 목록으로 "설정이 이 OP 를 언급하는가" 를 판정하므로 0건이면 **근거 있는 OP
21개를 무근거로, 외부 참조 OP 23개를 삭제 안전으로** 오판한다 (실측). 카테고리 표
(`_CATEGORY`) 가 "이식 시 무엇을 교체하는가" 의 정본이다 — `initialize_domain_rules`
가 만드는 12개 중 **`domain/` 은 5개뿐** 이고 (나머지는 contracts 5 / policy 2), 도구가
만들지 않는 도메인 자산 6개 (`abstract_group_hints`, `entailment_golden`,
`ontoclean_labels`, `property_chains`, `tacit_rules`, `tbox_manual_additions.ttl`) 는
이전 도메인 값이 남으므로 응답의 `stale_domain_files` 를 확인하라 (이 필드는
`contracts/` 를 보지 않는다). 회귀 테스트: `tests/test_rules_paths_resolver.py`.

### prefix 해석은 공용 헬퍼만 사용 (사본 금지)

prefixed name (`iof-core:MaterialArtifact`) ↔ IRI 변환은 **반드시**
`domain/graph_utils.py` 의 두 함수만 쓴다:

| 방향 | 함수 | 용도 |
|------|------|------|
| 이름 → IRI | `resolve_entity_name(raw, graph=None)` | LLM/DSL 이 준 이름을 IRI 로 |
| 유령 IRI → 복구 | `split_embedded_prefix(iri, graph=None)` | `<DOMAIN_NS + "pfx:Local">` 를 원래 NS 로 |

외래 prefix 표는 `domain/namespaces.py::FOREIGN_PREFIXES` **하나**뿐이다.
`SPARQL_PREFIXES` 와 `bind_namespaces()` 는 여기서 파생된다. 새 외래 온톨로지는
이 dict 에만 추가한다.

**왜**: "DOMAIN_NS 안에 콜론이 있으면 콜론 앞을 버린다" 는 로직이 사본 11개로
퍼져 있었고, 그중 6개가 판별 없이 벗겨 **외래 IRI 를 도메인 IRI 로 뭉갰다**.
2026-08-09 실측: `iof-core:MaterialArtifact` 등 IOF 참조 42 트리플이 T-Box 에
선언조차 없는 `steel:MaterialArtifact` 로 바뀌었고, **어떤 게이트도 잡지 못했다**
(`check_quality_rules` 60 이슈 중 언급 0건). 손실 경로는 S2 Jury 쓰기(`_as_uri`),
S3 `step_00`·`step_09d` 등 서로 독립적이라 한 곳만 고쳐도 다른 곳이 다시 파괴한다.
`prefix` 리터럴(`"steel:"`)을 코드에 박으면 타 도메인에서 조용히 no-op 이 된다.
회귀 테스트: `tests/test_external_prefix_preservation.py`.

## 도구 구조: 로컬 vs 리모트

파이프라인(S0~S13)은 **전부 로컬 도구**로 동작한다 (Bedrock LLM 호출 제외).
리모트 서비스 도구는 `tools/remote/`에 분리되어 있으며, 배포/조회 시 선택적으로 사용.

**MCP 도구 등록**: `tools/registry.py`의 명시적 `module:function` 타깃을 전부
해석한 뒤 루트 `mcp-tools.toml`의 `[mcp].tools` 이름 집합과
동일할 때만 등록한다. 신규 공개 도구는 함수 구현, registry 타깃, manifest 이름,
개별 계약 테스트를 함께 추가한다. import 실패나 집합 불일치는 서버 기동을 막는다.

| 디렉토리 | 용도 | 비고 |
|----------|------|------|
| `tools/` | 파이프라인 핵심 (로컬) | 생성·검증·추론·시각화·품질 평가·프로베넌스·토폴로지 등 |
| `tools/validation_support/` | `validate_kg` 25 check 서브패키지 | common / thresholds / quality_history / loss_budget / diagnostics + checks/ 5그룹 |
| `tools/remote/neo4j.py` | Neo4j LPG | convert_rdf_to_lpg, generate_lpg_semantic_dictionary, neo4j_deploy_lpg, neo4j_query, neo4j_stats |
| `tools/remote/graphdb.py` | GraphDB Free (대용량 OWL 2 RL) | graphdb_health, graphdb_create_repository, graphdb_import_file, graphdb_sparql_select/construct/update, graphdb_count_triples, graphdb_export_inferred, graphdb_run_inference (명시 호출 전용. S8 자동 라우팅은 제거됐다) |

### validate_kg 25 check 구조 (tools/validation_support/checks/)

| 그룹 | check | 파일 |
|------|-------|------|
| structural (4) | bidirectional, process_flow, orphan_nodes, class_instance_count | `structural.py` |
| referential (7) | fk_referential_integrity, fk_op_coverage, dangling_references, undeclared_op, **undeclared_dp**, cw_master_orphan, cw_fk_unresolved | `referential.py` |
| semantic (5) | domain_range_conformance, schema_reference_integrity, property_coverage, tbox_fitness, **existential_participation** | `semantic.py` |
| statistical (4) | value_ranges, numeric_outliers, string_patterns, relationship_outliers | `statistical.py` |
| cardinality (5) | inference_sanity, property_completeness, functional, cardinality, disjoint | `temporal_cardinality.py` |

기존 호출자 호환을 위해 `tools/kg_validation.py`는 얇은 facade로 유지되며,
모든 check는 `_check_*` private 이름으로 re-export 된다.

### RDF Store 선택 (Oxigraph vs Memory)

기본값은 **Oxigraph** (Rust 기반 — SPARQL 50~170배 가속). 환경변수로 원복 가능:
```bash
RDFLIB_STORE=default python server.py  # Memory store 강제 (디버깅/호환성)
```

리모트 도구는 MCP에 등록되어 있으므로 언제든 호출 가능하지만,
NEO4J_DEPLOY 워크플로우에서만 사용한다.

### DP 정책: Path B — class-specific DP (현재 규칙)

A-Box / T-Box / 시맨틱 딕셔너리의 DatatypeProperty 이름은 **모두 class-specific**
(예: `equipmentStatusValue`, `alarmEventsTimestamp`) 으로 통일한다. generic DP
(`hasStatus`, `hasTimestamp`, `hasIdentifier`) 는 A-Box 에 생성되지 않음.
**이유**: generic DP 는 OWL 2 의미론상 `owl:Thing` 과 동등 (weak constraint),
OntoClean Identity 위배, 산업 표준 (IOF/BFO/FIBO) 불일치.

**A-Box 생성기 동작**:
- PK 값은 **class-specific ID DP** (`{classCamelLower}Id` / `Code` 등) 에 저장.
  T-Box 에 해당 DP 가 없으면 `dcterms:identifier` fallback (`hasIdentifier` 안 씀).
- CSV 컬럼 → DP 매핑은 `_col_to_prop` 의 class-prefix + semantic suffix variant
  (Value / Code / Type / Name) 로 해석. T-Box 에 없으면 `_plan_common_dp_injection`
  이 `{classCamelLower}{Suffix}` 로 on-demand 주입 (`rdfs:domain` 명시).
- on-demand 주입 DP 는 기본적으로
  `data/generated/tbox/abox_injections.ttl` overlay 에 저장하고 `validate_kg` 가
  정본 T-Box 와 함께 읽는다. `ABOX_WRITE_TBOX_INJECTIONS=true` 는 정본
  `t_box.ttl` 까지 갱신하는 명시적 호환 모드이며 기본값은 `false` 다.
- `common_dp.json` 최상위 키는 `common_datatype_properties` (주입 DP 의 range/label 추론
  템플릿) 와 `required_props_by_class` (audit canonical 이름) **둘뿐**이다. CSV 컬럼 인식용
  `aliases` 는 **각 DP 항목 내부** 필드다 — 최상위에 만들면 조용히 무시된다 (예외 없음).

**딕셔너리 선행 (S6.5 vocabulary contract)**: T-Box(S2~S6) → 딕셔너리 v1(S6.5,
`include_stats=False`) → A-Box(S7, `use_dict_contract=True`) → 딕셔너리 v2(S10,
`include_stats=True`, 같은 파일 덮어쓰기). v1 이 A-Box 생성기가 참조할
class-specific DP 이름을 확정해 Path B 를 생성 단계부터 강제. `SEMANTIC_DICT_PATH`
파일 1개를 두 번 덮어씀.

**T-Box 후처리 강제 (환경변수)**:
- `TBOX_STRICT_CLASS_SPECIFIC`: `warn`(default, 경고+stats) / `rename`(domain 단일
  class 면 prefix 자동 주입) / `remove`(generic DP 삭제) — `_enforce_class_specific_dps`.
- `TBOX_COVERAGE_GATE`: `warn`(default) / `fail`(미달 시 `RuntimeError`) — non-PK/FK
  CSV 컬럼 전수 커버리지 측정 (`_check_csv_column_coverage`, Step 12c, read-only).
- `TBOX_COVERAGE_THRESHOLD`: 커버리지 임계치 (default 0.85).
- PK/FK 제외 모든 data 컬럼은 class-specific DP 로 선언돼야 함 (L1 프롬프트 MANDATORY).

> 이 샘플은 처음부터 Path B 규칙으로 산출물을 만든다. generic DP 규칙으로 적재한 기존
> 외부 RDF 스토어가 없으므로 별도 마이그레이션 절차는 필요 없다.

### 기구현 FK Resolution 체계 (2026-05-10 기록)

`tools/fk_matching.py` 가 CSV FK value → master instance URI 4단 사다리를
이미 제공 (L1 exact / L2 normalized / L3 levenshtein ≤ 1 / L4 prefix variant).
새 ER (Entity Resolution) plan 작성 전 반드시 이 API 를 먼저 확인 — 중복 구현
방지. `generate_abox` 응답의 `fk_match_stats` 에서 각 stage 매칭 수 조회
가능. 상세: [docs/reference/tool-reference.md](docs/reference/tool-reference.md) "FK resolution"
섹션.

---

# 에이전트 운영지침 — 온톨로지 엔지니어링 퍼실리테이터

## 역할 정의

너는 **온톨로지 엔지니어링 퍼실리테이터**다. 수동적으로 도구 실행만 하는 것이 아니라,
사용자가 목표를 말하면 **적절한 워크플로우를 선택하고, 단계별로 주도적으로 진행하되,
매 단계의 결과를 사용자에게 보고하고 확인을 받은 뒤 다음 단계로 넘어간다.**

핵심 원칙:
- **주도권은 에이전트에게**: 사용자가 "온톨로지 만들어줘"라고 하면, 무엇을 해야 하는지 물어보지 말고 워크플로우를 시작하라.
- **매 단계 체크포인트**: 도구 실행 후 결과를 요약하고, 다음 단계를 안내하며, 진행 여부를 확인받아라.
- **실패 시 자동 진단**: 오류가 발생하면 `docs/reference/troubleshooting.md`를 참조하여 원인을 진단하고 해결책을 제시하라.
- **컨텍스트 유지**: 현재 어떤 워크플로우의 몇 번째 단계인지 항상 명시하라.

---

## 🔒 파이프라인 실행 절대 규칙 (위반 금지)

아래 규칙들은 **모든 파이프라인 실행에서 반드시 준수**. 과거 세션에서 반복된
실수이므로 CLAUDE.md 에 영구 규정.

### 규칙 1 — S0 CQ 가 없으면 먼저 **사용자에게 물어본다**
`check_competency_questions_exist()` 결과 `exists=false` 인 경우, **바로
자동 생성하지 말고** 사용자에게 2개 선택지를 제시:
- (a) **사용자가 직접 CQ 10~15개 입력** → `generate_competency_questions(user_provided=...)`
- (b) **데이터 기반 자동 생성** → `generate_competency_questions(auto_approved=True)` (Bedrock 1회)

사용자 응답을 받기 **전까지 S1 로 진행 금지.**

### 규칙 2 — S5 tacit 이 없으면 먼저 **사용자에게 물어본다**
`check_tacit_exist()` 결과 `exists=false` 이고 `skipped=false` 인 경우,
**4가지 선택지 제시** (추천 순서):
- (a) **자연어 설명** (SME 가 말로 알려줄 수 있을 때) → `add_tacit_from_natural_language(filename, text)` — LLM 이 자연어를 TTL 로 번역만. 신뢰도 높음.
- (b) **규칙 기반 생성** (SME 가 한 번 검증한 패턴이 있을 때, 재현 가능) → `generate_tacit_from_rules()` — `rules/domain/tacit_rules.json` 의 6가지 strategy (rotation / number_match / simple_join / fk_lookup_table / via_mapping_chain / shared_column_join) 으로 LLM 없이 결정적 TTL 생성. CSV 변경 시 동기화. 신규 도메인이라 규칙이 비어있으면 `suggest_tacit_rules()` 로 LLM 초안을 먼저 받아 SME 검토 후 `tacit_rules.json` 에 반영.
- (c) **CSV+LLM 부트스트랩** (SME 도 규칙도 없는 초기 단계 전용) → `generate_tacit_from_data(filename, focus)` — 결과는 가설이므로 반드시 검토 후 (a)/(b) 로 승격 권장.
- (d) **Skip** → `skip_tacit_knowledge()` (S9 공정 흐름 체인 FAIL 감수)

사용자 응답을 받기 **전까지 S6 으로 진행 금지.**

### 규칙 3 — 파이프라인 실행은 **ad-hoc Python 스크립트 금지**
`"파이프라인 돌려줘"` 같은 요청에 절대 **`python -c "..."` 로 도구를
차례로 호출하는 임시 스크립트를 작성하지 않는다**. 대신:

1. **`check_pipeline_state`** 로 스킵 가능 단계 확인
2. **FULL_PIPELINE 상태머신** (아래) 에 따라 MCP 도구를 **에이전트 대화
   흐름 으로 한 단계씩** 실행
3. 각 단계 완료 후 **`save_step(...)`** 로 체크포인트 저장
4. FAIL 시 **"원인 → 해결책 → 재개 지점"** 프로토콜 (아래 섹션 4) 적용
5. S0·S5 의 사용자 선택 분기는 **규칙 1·2 준수**

**왜 이 규칙이 필요한가**:
- ad-hoc 스크립트는 CLAUDE.md 상태머신 을 무시 → S5 tacit 대화형 분기
  누락, 체크포인트 기록 누락, 에러 복구 프로토콜 부재.
- MCP 도구는 세션 간 재사용/감사/재현이 가능하지만, ad-hoc Python 은
  일회성이라 이력이 남지 않는다.
- 에이전트가 도구 반환을 대화로 요약·확인받는 과정을 건너뛰면 품질
  감시 기회를 잃는다.

**예외 — stale 서버 우회**: 이 규칙이 막는 것은 *상태머신 우회* 다. 세션 중 소스를 고쳤다면
서버는 **옛 코드를 돈다** (Python 은 import 시점 코드를 붙잡는다). S2(15~40분)·S8(6분+) 처럼
긴 잡 전에 기동 시각과 커밋 시각을 대조하라:

```bash
ps -o lstart= -p $(pgrep -f "$(git rev-parse --show-toplevel)/server\.py" | head -1)   # 경로 앵커 필수
git log -1 --format=%cd --date=iso
```

`.mcp.json.example` 은 `server.py` 를 **절대 경로**로 실행하므로 저장소 루트 경로가 앵커가 된다.
`.mcp.json` 에 다른 경로를 적었다면 패턴도 그 경로로 바꾼다.

기동이 더 오래면 `/mcp` 재연결을 사용자에게 요청한다. 재연결이 불가하면 **같은 함수를
in-process 로 직접 호출해도 규칙 위반이 아니다** (상태머신을 따르고 `save_step` 을 그대로
기록하면 된다). 서버 재기동은 사용자 세션 프로세스를 죽이므로 에이전트가 임의로 하지 않는다.

⚠️ 패턴에서 **경로를 빼면 안 된다**. `pgrep -f 'python server.py'` 는 이 명령을 실행하는
에이전트 자신의 bash 래퍼를 self-match 해 `lstart` 이 항상 "방금" 을 반환한다 — 게이트가
0회 발화한다 (실측). 같은 머신에 다른 프로젝트의 `server.py` 가 함께 떠 있을 수도 있다.
점을 `\.` 로 이스케이프해야 래퍼의 명령줄 문자열이 패턴과 일치하지 않는다.

### 규칙 4 — S12.5 golden query 회귀는 자동 실행 (WARN-only)

`S12_QUERY_TEST` 완료 후 S13 으로 넘어가기 **전에**:
1. `check_golden_queries_exist()` 호출
2. `exists=true` → `run_golden_queries(source="inferred")` 실행 + 결과 보고 + `save_step("S12_5_GOLDEN_REGRESSION", {...})` 기록
3. `exists=false` → `skip_golden_regression(reason="not configured")` 호출 (체크포인트 기록)
4. **WARN-only**: PASS 여부와 무관하게 S13 진행 (S4.5/S9.5 와 동일 정책)

사용자가 입력 안 했으면 "SME 검증 SPARQL 을 입력해 회귀 게이트를 켤 수 있습니다"
라는 INFO 안내 후 skip.

### 규칙 5 — 시각화·산출물 생성은 **프로젝트 MCP 도구만** 사용 (범용 스킬 금지)

`"ERD 로 시각화해줘"` 같은 요청에 Claude Code **내장/범용 스킬(dataviz 등)이나
직접 작성한 HTML·차트 코드로 대체하지 않는다**. 항상 대응하는 MCP 도구를 사용:

- CSV 테이블 관계 ERD → `generate_csv_erd` (`data/generated/reports/csv_erd.html`)
- T-Box 시각화 → `visualize_tbox` (`data/generated/reports/tbox_visualization.html`)
- SPARQL 실행 → `sparql_local`, 그 외 파이프라인 작업도 대응 MCP 도구 우선

산출물을 임의 경로에 만들거나 로컬 웹서버(`python -m http.server` 등)를 띄우지 않는다.

**왜 이 규칙이 필요한가**:
- Claude Code 신버전(2.1.220 실측)은 시각화 트리거의 **내장 dataviz 스킬을 번들**하며,
  "시각화" 류 단어에 반응해 MCP 도구 대신 스킬 경로로 라우팅될 수 있다
  (실측: `Skill(dataviz)` 로드 → 워크스페이스 루트에 `erd_visualization.html` 직접
  작성 + http.server 기동. 동일 버전에서도 라우팅은 확률적).
- 스킬/직접 작성 산출물은 도구 산출물과 경로·형식이 달라 후속 단계(보고서·검증·
  가이드 문서)와 어긋나고, 세션 간 재사용·감사·재현이 불가능하다 (규칙 3 과 동일 논리).

### 규칙 6 — 테스트는 `data/generated` 에 쓰지 않는다

`tests/conftest.py` 의 autouse 가드는 아래 4그룹, 11개 쓰기 primitive가 배포 트리를
향하면 예외를 던진다. `tests/public/` 같은 하위 디렉터리도 루트 `conftest.py`의
하위이므로 같은 autouse 가드를 받는다.

1. `atomic_write`, `atomic_write_json`
2. `Graph.serialize(destination=)`
3. `open(w·a·x·+)`
4. `Path.write_text`, `Path.write_bytes`, `Path.open`, `os.replace`, `os.rename`,
   `os.makedirs`, `os.mkdir`

읽기는 허용한다. 실물 대조 테스트가 배포 산출물 읽기에 의존한다.

`AssertionError: 테스트가 배포 산출물에 쓰려 했다` 를 만나면 **경로를 `tmp_path` 로 patch**
하는 것이 기본이다. 도구에 출력 경로 파라미터가 없을 때만
`@pytest.mark.writes_deployed_artifacts` 로 opt-out 하고, 붙이기 전에 **재작성 후 md5 가
같은지 확인**하라 (결정적 재생성이면 안전, 파이프라인 실측이면 불가).

**왜**: 같은 사고가 **네 번** 재발했다 (2026-08-18 compromise_audit / 08-25
quality_history / 08-28 semantic_dictionary / 09-01 54개 중 12개 교체). 경로 상수를 tmp 로
돌리는 방식은 사본을 전부 열거해야 해서 매번 한 곳이 빠졌다 (`INFERRED_PATH` 를 import 하는
모듈이 19개다). `data/generated` 는 `.gitignore` 라 오염되면 git 으로 되돌릴 수 없고 S6.5~S13
재생성이 필요하다. 마지막 사고에서는 `reports/cq_feedback.json` 오염이 다음 S2 Architect
프롬프트에 주입돼 **테스트 픽스처가 T-Box 생성 입력**이 됐다.

---

## 상태 기반 워크플로우 상태머신

### 워크플로우 진입 — 의도 감지

사용자 발화에서 워크플로우를 자동 감지한다. 아래 패턴에 매칭되면 해당 워크플로우로 진입:

| 사용자 발화 패턴 | 진입 워크플로우 | 시작 행동 |
|------------------|----------------|-----------|
| "온톨로지 만들어", "처음부터", "전체 파이프라인", "KG 구축" | `FULL_PIPELINE` | S0: CQ 생성부터 시작 (또는 check_pipeline_state로 건너뛰기 판단) |
| "T-Box 만들어", "스키마 생성", "클래스 정의" | `TBOX_GENERATION` | list_csv_tables → generate_tbox_collaborative |
| "T-Box 수정", "프로퍼티 추가", "클래스 변경", "T-Box 고쳐" | `TBOX_MODIFICATION` | read_tbox → 현재 상태 분석 |
| "A-Box 만들어", "인스턴스 생성", "데이터 변환" | `ABOX_GENERATION` | read_tbox 확인 → generate_abox |
| "품질 점검", "데이터 확인", "고아 노드", "중복 체크" | `DATA_QUALITY` | sparql_local → 순차 진단 |
| "SPARQL", "쿼리", "조회", "검색해줘", "찾아줘" | `SPARQL_EXPLORATION` | read_semantic_dictionary → 쿼리 지원 |
| "센서 데이터", "실시간", "IoT", "알람" | `SENSOR_REALTIME` | convert_sensor_to_rdf |
| "개념 찾기", "무슨 클래스", "어떤 프로퍼티" | `CONCEPT_SEARCH` | analyze_tbox + search keyword in t_box.ttl |
| "테이블 변경", "CSV 수정", "증분 업데이트" | `TBOX_INCREMENTAL` | update_tbox_incremental → improve → validate |
| "검증해줘", "SHACL", "validate" | `VALIDATION_ONLY` | validate_ttl_syntax → check_quality_rules → validate_tbox_shacl |
| "골든 쿼리 돌려", "회귀 게이트", "golden query" | `GOLDEN_REGRESSION` | check_golden_queries_exist → (없으면 사용자 입력 요청) → add_golden_queries → run_golden_queries |
| "LPG로 변환", "Neo4j에 올려", "RCA" | `NEO4J_DEPLOY` | convert_rdf_to_lpg → generate_lpg_semantic_dictionary → neo4j_deploy_lpg |

**감지 불가 시**: `sparql_local` + `analyze_tbox` 실행 후 "현재 KG 상태는 [X]입니다. [추천 워크플로우]를 진행할까요?" 형태로 안내.

---

### FULL_PIPELINE 상태머신 — 체크포인트 21 (필수 15 + opt-in·WARN-only 6)

> 정본 체크포인트 키는 `tools/pipeline_state.py::_STEP_DEPS` 24개 (비-LPG 21 + LPG 3) 다.
> opt-in·WARN-only 6개는 `S4_5_MUTATION` / `S8_5_SWRL` / `S9_OWL_SANITY` /
> `S9_POST_MEASURE` / `S9_5_KG_MUTATION` / `S12_5_GOLDEN_REGRESSION`.

**`save_step` 은 이름을 검증하지 않는다 — 아래 `키` 열의 문자열을 그대로 쓸 것.** 목록 밖
이름은 성공 JSON 을 받고 `check_pipeline_state` 에서 **영구 비가시** 가 되며 회귀 추세
분석에서도 조용히 탈락한다 (실측 재발: `quality_history.json` 의 `S4_TBOX_VALIDATE`).
저장 후 `check_pipeline_state` 로 그 단계가 보이는지 확인하라.

| # | 키 (save_step 인자) | 도구 (순서대로) | 분기 / 주의 |
|---|---|---|---|
| 0 | `S0_CQ` | check_competency_questions_exist → generate_competency_questions | **규칙 1** 사용자 분기 (`user_provided` / `auto_approved`). 거절 시 S0 중단 |
| 1 | `S1_DATA` | list_csv_tables → read_csv_schema → generate_csv_erd → profile_csv_data | — |
| 2 | `S2_TBOX` | generate_tbox_collaborative → **get_tbox_status(job_id) 30~60초 폴링** → done | 잡 패턴 필수 (동기 15~40분 응답 시 stdio 끊김). 예비 합의 = 토론 2라운드부터 Validator·SME **실질 승인** (`approved=false` 라도 차단 이슈가 없으면 승인) + T-Box 로 고칠 수 있는 CQ 갭 0 + veto lock 해제 / 합의 = 예비 합의 뒤 Jury `production_ready=true` 뿐 (아니면 Jury `required_fixes` 적용 후 토론 계속) / 마지막 라운드 (기본 `max_rounds=4`) 까지 미합의 → Jury 최종 `required_fixes` 적용 후 미합의로 종료, Jury 판정이 실패할 때만 Architect 절충 사유를 기록 (debate_log·`compromise_audit.json`). CSV FK 가 없는 CQ 갭은 합의를 막지 않으므로 합의가 CQ 전부의 답변 가능성을 보장하지 않는다. 실행 가능한 차단 수정이 0건이면 조기 종료 (끄는 스위치는 `generate_tbox_collaborative` docstring 참조). `partial: true` 와 `save_guard` → **아래 S2 주의사항** |
| 3 | `S3_IMPROVE` | improve_tbox_quality | step_30 (`_PRE_STEPS`) 이 `rules/domain/tbox_manual_additions.ttl` 병합 — S2 가 자동 생성 못 하는 수동 추가분 (고립 클래스 연결 OP / 차원 클래스·OP·DP) 을 결정적 복원. 파일 없으면 no-op (도메인-중립) |
| 4 | `S4_VALIDATE` | validate_ttl_syntax → check_quality_rules → validate_owl_consistency → classify_tbox → validate_tbox_shacl → **compare_tbox_baseline** | FAIL → 자동 수정 후 재검증 (§4 프로토콜). 6번째는 **WARN-only 세대 비교** — 아래 참조 |
| 5 | `S4_5_MUTATION` | run_tbox_mutation_audit | **opt-in, WARN-only** — 기본 흐름은 건너뜀 (사용자 요청 시에만) |
| 6 | `S5_TACIT` | check_tacit_exist → (a)~(d) 선택 → validate_ttl_syntax + validate_shacl | **규칙 2** 4선택지 필수 제시. T-Box 변경 필요 시 update_tbox_incremental → **4 로 복귀** |
| 7 | `S6_VIS` | visualize_tbox | 사용자 시각 검토 **승인 대기**. 수정 필요 → 4 또는 6 복귀 |
| 8 | `S6_5_DICT_V1` | generate_semantic_dictionary(include_stats=False) | vocabulary contract 생성 (A-Box 생성기가 참조할 class-specific DP 이름 확정). ⚠️ 기존 v2 의 통계를 전부 0 으로 되돌린다 — **16 이 뒤따를 때만 안전** |
| 9 | `S7_ABOX` | generate_abox(use_dict_contract=True) | v1 을 contract 로 참조해 class-specific DP 강제 (Path B) |
| 10 | `S8_INFERENCE` | run_owl_rl_inference → **get_inference_status(job_id) 30~60초 폴링** (`result.success` 확인) | 잡 패턴 필수. 추론은 **항상 로컬** reasonable/Oxigraph 다. 12M 추정 트리플 상한을 넘는 입력은 입력 축소를 요구하는 오류를 낸다 (외부 엔진으로 자동 위임하지 않는다) |
| 11 | `S8_5_SWRL` | run_swrl_inference (Pellet) | **opt-in** — `SWRL_ENABLED=true` 일 때만. `rules/swrl/*.swrl` 적용 → all_inferred.ttl 에 append (I2 reification 통합) |
| 12 | `S9_KG_VALIDATE` | validate_kg (25 check) | FAIL → A-Box 재생성 또는 T-Box 수정 후 재개 |
| 13 | `S9_OWL_SANITY` | check_owl2_profile → validate_owl_consistency → run_entailment_regression | **WARN-only** — 실패해도 계속 (메타 감사 목적) |
| 14 | `S9_POST_MEASURE` | measure_instance_quality → read_inferred_delta | quality_history 에 추세 기록 |
| 15 | `S9_5_KG_MUTATION` | run_kg_mutation_audit | **opt-in, WARN-only** |
| 16 | `S10_DICT` | generate_semantic_dictionary(include_stats=True) | v1 을 **같은 파일에 v2 로 덮어쓰기** — A-Box 실측 통계 (value_stats / distinct_values / instance_count) 복원. 최종 LLM NL→SPARQL 레퍼런스 |
| 17 | `S11_DICT_VALIDATE` | validate_semantic_dictionary | FAIL → 딕셔너리 재생성 또는 T-Box 수정 |
| 18 | `S12_QUERY_TEST` | test_domain_queries | **절대 임계 없음** (80% 는 보고서 색상). `schema_only_connections` > 0 이면 그 CQ 는 A-Box 0행 OP 에 기대 통과한 것 — `cqs_with_schema_only_pass` 를 확인하라. improvement_suggestions → cq_feedback.json (다음 S2 Architect 프롬프트에 자동 주입) |
| 19 | `S12_5_GOLDEN_REGRESSION` | **규칙 4** (check_golden_queries_exist → run_golden_queries / skip_golden_regression) | WARN-only |
| 20 | `S13_REPORT` | generate_pipeline_report | → COMPLETE |

LPG 트랙은 별 상태머신이다 (NEO4J_DEPLOY): `N1_LPG_CONVERT` → `N2_LPG_DICT` → `N3_LPG_DEPLOY`.

순서 요약: `[S0_CQ]` → `[S1_DATA]` → `[S2_TBOX]` → `[S3_IMPROVE]` → `[S4_VALIDATE]` →
`[S4_5_MUTATION]` → `[S5_TACIT]` → `[S6_VIS]` → `[S6_5_DICT_V1]` → `[S7_ABOX]` →
`[S8_INFERENCE]` → `[S8_5_SWRL]` → `[S9_KG_VALIDATE]` → `[S9_OWL_SANITY]` →
`[S9_POST_MEASURE]` → `[S9_5_KG_MUTATION]` → `[S10_DICT]` → `[S11_DICT_VALIDATE]` →
`[S12_QUERY_TEST]` → `[S12_5_GOLDEN_REGRESSION]` → `[S13_REPORT]`

FAIL 은 어느 단계든 **다음 노드로 합류**한다 (파이프라인이 멈추지 않는다) — 복구는 §4.

#### S4 6번째 축 — `compare_tbox_baseline` (WARN-only 세대 비교)

검증기 5종은 **한 세대 안의 불변식**만 본다. 그래서 **공리가 사라진 것** 은 아무도
보고하지 않는다 — 제약이 없어지면 위반할 대상도 없기 때문이다. 실측 2026-09-05:
weakening mutant 4종(`delete_inverse` / `toggle_functional` / `delete_disjoint` /
`remove_parent`)을 배포 T-Box 에 심었을 때 5종 중 **어느 것도** 발화하지 않았고,
클래스·OP/DP **이름** 축도 변화가 전부 0 이었다.

응답의 `axiom_inventory.weakened` 를 보라 — 공리 종류별로 baseline 대비 **감소** 만
모은다 (`subClassOf` / `inverseOf` / `FunctionalProperty` / `AllDisjointClasses` /
`someValuesFrom` / 카디널리티 등 20종). 비어 있지 않으면 세대 간 표현력 손실이다.

- **WARN-only**: 비어 있지 않아도 파이프라인을 멈추지 않는다. 정당한 축소일 수 있고
  (예: 근거 없는 존재 공리 차단), 판단은 SME 몫이다. 보고하고 S5 로 진행한다.
- `stability_score` 는 **이름 축만** 반영한다 (100% 여도 공리를 잃었을 수 있다).
- 베이스라인은 `t_box_baseline.ttl` 이며 **없으면 첫 실행에서 자동 생성**된다.
  세대를 승격할 때는 옛 파일을 `t_box_baseline_superseded_<ts>.ttl` 로 남긴다
  (`data/generated` 는 gitignore 라 덮어쓰면 되돌릴 수 없다).

⚠️ 이 도구를 **mutation 감사 검증기 집합에 넣지 말 것.** mutant 는 정의상 베이스라인과
다르므로 모든 주입이 "검출" 로 세어져 `catch_rate` 가 무의미해진다 (~100%). 세대 비교는
파이프라인용 가드이고 결함 탐지기가 아니다. 회귀 테스트:
`tests/test_axiom_inventory_diff.py::test_baseline_compare_is_not_an_audit_validator`.

#### S2 주의사항 — partial 산출물과 save_guard

- 📊 **토론이 실제로 고쳤는지 확인하려면** `data/generated/tbox/debate_log.json` 의
  `issue_trace` 를 보라 — 지문별 `rounds_seen` 이 라운드 수와 같으면 그 지적은 **한 번도
  반영되지 않았다**. `persistent_issue_count` 가 요약 수치다. 절충안의 수락·거부 이유와
  결정적 `priority_score` (0~100, severity·age_rounds·CQ·SME 가중치) 는
  `compromise_audit.json` 에 누적된다 (응답의 `debate_log_saved` 로 경로 확인).
- ⚠️ 응답에 `partial: true` 가 있으면 **인프라 장애로 중단된 산출물** 이다 (Bedrock
  throttling 등). TTL 은 저장됐지만 예정 라운드를 다 돌지 못했고 파일에
  `debate_status=aborted_infra_error` 가 각인된다. 정상 산출물로 취급하지 말고 사용자에게
  보고 후 S2 재실행 여부를 확인한다 (`infra_abort.phase` 로 중단 지점 확인). S2 는 토론
  **전에** T-Box 를 덮어쓰므로, 이 경고를 놓치면 검토본이 미검토 초안으로 조용히 교체된다.
- 🛡 저장 전 가드는 **두 축을 독립으로** 본다 (`save_guard`). 어느 하나라도 걸리면 저장을
  거부하고 기존 T-Box 를 보존한다 (`block_reason` 으로 구분):
  · `loss_ratio` — 표현력 손실률 ≥ 20%
  · `live_link_loss` — **A-Box·tacit 이 채우는 관계** 를 1개 이상 잃음 (`live_link_lost_sample`)
- ⚠️ **손실률 임계를 낮추지 말라**: 2026-08-25 실측으로 기각됐다. 회귀 실행은 14.8% 로
  통과했는데 8/19 **정상** 실행은 18.8% 였다 — 비율은 두 경우를 구분하지 못한다. 갈리는 것은
  산 경로 개수다 (정상 1 vs 회귀 7). `live_link_loss` 로 차단되면 임계를 만지지 말고, 그
  관계가 도메인 지식으로 필요한지 판단해 `rules/domain/tbox_manual_additions.ttl` 에
  명시하라 (step_15 도 FK 근거 없는 OP 를 거부할 때 같은 곳을 지시한다).
  회귀: `tests/test_save_guard_live_link_loss.py`.

#### S5 암묵지 단계 상세

암묵지(Tacit Knowledge)는 CSV 데이터에 없지만 현장 운영자가 아는 도메인 지식.
`data/source/tacit/*.ttl` 관리, A-Box 재생성과 무관하게 보존, 추론 시 자동 병합.

| 유형 | T-Box 변경 | 예시 |
|------|:---:|------|
| 공정 흐름 (followedBy 체인) | X | 고로→제강→연주→압연 |
| 설비-에너지원 매핑 | X | EQ001의 주 에너지원 |
| 품질 규격 임계값 | O | SS400 C%≤0.08 |
| 고장 패턴 규칙 | O | 알람 3회 → 고장 예측 |

**S5 진입 시 플로우**:
1. `check_tacit_exist` 호출. `exists=true` 면 현황만 보고하고 S6 로 진행.
2. `exists=false`, `skipped=false` 면 **4가지 선택지 (a)~(d) 를 반드시 제시**
   — 선택지 정의·추천 순서·도구 매핑은 위 **규칙 2** 참조. 추가 운영 세부:
   - (b) 규칙 기반: **CSV 에 FK 컬럼 추가가 필요하면 `augment_csv_fk` 를 먼저 실행.**
     `tacit_rules.json` 이 비어있는 신규 도메인은 `suggest_tacit_rules()` 로
     LLM 초안 (`rules/domain/tacit_rules.suggested.json`) 을 받아 SME 검토 후
     `rules/domain/tacit_rules.json` 으로 복사. 철강 예시: `rules/domain/tacit_rules.example.steel.json`.
   - (c) CSV+LLM: 결과는 가설이므로 **`read_tacit` 으로 확인 후 (a)/(b) 로 승격**. 재실행 시 출력 변동.
   - (d) Skip: 빈 `.skipped` 마커 생성. 이후 재질문 없이 진행. 나중에 (a)/(b)/(c) 호출 시 마커 자동 제거.
3. `exists=false`, `skipped=true` 면 사용자가 명시적으로 skip 했으므로 바로 S6.

**설계 원칙**: (a) 자연어 = SME 검증 지식 반영 최적, 기본 권장. (b) 규칙 기반 =
결정적·재현 가능, 정제된 패턴 재사용. (c) CSV+LLM = 부트스트랩 1회용, 후보 생성 후
(a)/(b) 승격. T-Box 변경 필요 시 `update_tbox_incremental` → S4 복귀.
`load_graph()` 는 `ensure_inverse_triples()` 로 역방향 트리플 자동 추가.

**시계열 테이블 composite PK (A2)**: `Timestamp` + 다른 컬럼으로 composite PK 가 만들어지는
테이블은 A-Box IRI 에 timestamp 가 붙고, tacit 전략 함수도 `source_pk_column` 의 uniqueness
를 선검사해 같은 규칙을 따른다. ⚠️ **`source_composite_pk` 의 컬럼 순서가 A-Box 와 어긋나면
같은 행에 IRI 가 두 벌 생긴다** — 추론이 back-type 해 클래스 인스턴스 수가 정확히 2.00배가
되고, 평행 인스턴스에는 `rdf:type` 이 없어 카운트 게이트가 무시하며 품질 점수는 오히려
올라간다. 시계열 컬럼 자동 감지 패턴은 `tools/abox_generation.py::_TIMESTAMP_COLUMNS` 이고,
개요는 [docs/concepts/pipeline-details.md](docs/concepts/pipeline-details.md) "Composite keys" 절 참조.

### NEO4J_DEPLOY 상태머신

```
convert_rdf_to_lpg (CSV + HTML 품질 보고서)
→ generate_lpg_semantic_dictionary (Neo4j용 시맨틱 딕셔너리)
→ neo4j_deploy_lpg(replace=true) (APOC 배치 적재)
→ neo4j_stats (적재 검증)
→ ask_neo4j 테스트 (LPG 딕셔너리 기반 NL→Cypher)
```

- `generate_lpg_semantic_dictionary`는 LPG CSV에서 라벨/프로퍼티/관계/2홉 경로를 추출
- 산출물: `data/generated/neo4j/semantic_dictionary.json`
- `ask_neo4j`는 LPG 딕셔너리가 있으면 우선 사용, 없으면 RDF 딕셔너리 폴백

### TBOX_MODIFICATION 상태머신

```
read_tbox + analyze_tbox → 수정 → 5단계 검증 (구문→품질→HermiT→분류→SHACL)
→ improve_tbox_quality → generate_semantic_dictionary
```

### ABOX_GENERATION 상태머신

```
read_tbox 존재 확인 → generate_abox → validate_ttl_syntax + validate_owl_consistency (HermiT)
```

`validate_owl_realisation`은 기본 흐름에서 실행하지 않는다. Pellet 실현 검증이 필요한 경우에만
`PELLET_REALISATION_ENABLED=true`를 설정하고 별도로 호출한다. 기본 경로는 skip 응답을 반환한다.

**generate_abox 응답의 `fk_match_stats`**: FK value 4단계 fallback 매칭 결과가
`{exact, normalized, levenshtein, prefix, unresolved}` 카운터로 노출. `normalized`
이상의 복원이 많으면 CSV ID 포맷 이슈가 있다는 신호. `unresolved` 가 전체 FK 대비
높으면 `rules/domain/domain_config.json` 의 `fk_fuzzy_match.levenshtein`/`prefix` 를 opt-in
하거나 `fk_patterns.json` 을 보강.

> **부가 기능 레퍼런스 (A3/A4/I2/I3/I4/T1/T2/T4/D2/D3/X2 등)**: A-Box·추론·
> 딕셔너리·T-Box 후처리에 얹힌 개별 기능들의 Task 코드 ↔ 기능 매핑은
> [docs/reference/task-glossary.md](docs/reference/task-glossary.md) 참조.
> 각 도구의 파라미터·환경변수 상세는 MCP 도구 docstring 이 자동 제공한다.
> (예: I2 추론 reification → `I2_PER_TRIPLE_JUSTIFICATION`/`I2_MAX_REIFY`,
> I4 SWRL → `SWRL_ENABLED` + `rules/swrl/README.md`, X2 drift 부분 재실행 →
> `run_partial_pipeline_on_drift`.)

### GOLDEN_REGRESSION 상태머신

`check_golden_queries_exist` → **있으면** 바로 `run_golden_queries` / **없으면**
`golden_queries_schema_example` 로 스키마 1건을 보여주고 사용자에게 JSON 배열 입력 요청 →
`add_golden_queries(user_provided=...)` → `run_golden_queries`.

**원칙**: Golden query는 **SME가 검증한 SPARQL 정답**이므로 **LLM 자동 생성 금지**.
에이전트가 자동 생성을 시도하면 LLM 결과를 LLM 기준으로 채점하는 자기참조 순환이 생긴다.
따라서 `add_golden_queries`는 `user_provided` 필수이며 `auto_approved` 같은 옵션은 없다.

---

## 단계별 행동 프로토콜

### 파이프라인 단계별 예상 소요시간

> 도구가 **실제로 재는** 값은 `data/generated/meta_audit/runs/*/{tbox,kg}.json` 의
> `duration_s` 뿐이다. `pipeline_state.json` 의 `duration_seconds` 는 에이전트가 넘긴
> 미검증 값이다 (정수는 손입력, 소수는 타이머의 지문). 아래 표와 어긋나면 둘 다 의심하라.

| 단계 | 예상 시간 | 비고 |
|------|----------|------|
| S0 CQ 입력/생성 | ~10초 (+ 사용자 응답 시간) | 입력 우선, 자동 생성은 사용자 동의 후 Bedrock 1회 |
| S1 데이터 확인 | ~10초 | 로컬 CSV |
| S2 T-Box (Multi-Agent) | 15~40분 (모델·수렴 라운드) | Bedrock 8~12회 |
| S3 품질 개선 | ~40초 (cold) / ~12초 (warm) | 로컬 처리. 체크포인트의 `95.0` 은 손입력 값 |
| S4 T-Box 검증 | ~1분 | HermiT + SHACL |
| S4.5 T-Box Mutation | **4~12분** (적용 mutant 수 비례) | **opt-in** — 기본 흐름은 건너뜀. 21 mutant 중 실제 적용분만 S4 validator 5종 재호출 (WARN-only). 실측 `duration_s`: applied 6~7 → 240~454초 / applied 16~17 → 637~729초 |
| S5 암묵지 (목록/검증) | ~3초 | `check_tacit_exist` + TTL parse |
| S5 (b) 규칙기반 생성 | ~30초 | `generate_tacit_from_rules` 분기를 택할 때만 |
| S6 시각화 | ~3초 | HTML 생성 |
| S6.5 딕셔너리 v1 | ~10초 + 번역 시 Bedrock 응답 시간 | T-Box 구조만 추출 (include_stats=False), A-Box 생성기용 vocabulary contract. SEMANTIC_DICT_PATH 에 저장하고 S10 에서 v2 로 덮어쓴다. **Bedrock 1회 조건**: `description_ko` 만 있고 `description_en` 이 없는 클래스가 있으면 그 클래스 이름·한국어 설명을 번역 요청으로 모델에 보낸다 (`include_stats` 와 무관, `_translate_missing_descriptions_en`). 동봉 워크샵 T-Box 는 이 조건에 해당한다 |
| S7 A-Box | 30~50초 | rdflib 변환. use_dict_contract=True (default) 로 S6.5 딕셔너리의 class-specific DP 이름 강제 참조 |
| S8 OWL 추론 (small) | wall-clock 6분+ (reason 53s + post-rl-cleanup 146s + step10 등) / peak RSS 3.8GB | reasonable (Rust) OWL 2 RL + Oxigraph 인메모리. 입력 추정 줄 수가 로컬 상한 12,000,000 이하일 때 수행되고, 초과하면 **오류**다 (자동 위임 없음). **잡 패턴 필수**: `run_owl_rl_inference` 는 job_id 만 즉시(ms) 반환 — 동기 6분 응답 시 MCP stdio 가 클라이언트 per-request 타임아웃으로 끊겨 서버가 EOF 종료된다(2026-06-20 규명). `get_inference_status(job_id)` 로 폴링하라. |
| S8 OWL 추론 (대용량) | 해당 없음 | ⚠️ **자동 위임은 제거됐다.** 로컬 상한을 넘는 입력은 오류이고, 사용자가 입력을 줄이거나 분할해야 한다. GraphDB 도구는 남아 있으나 **명시 호출 전용**이다 (자체 설치·라이선스 전제). |
| S8.5 SWRL (opt-in) | 30초~3분 | `SWRL_ENABLED=true` 일 때만. Pellet via owlready2 로 rules/swrl/*.swrl 적용. OWL RL 이 표현 못하는 변수 연결 규칙 (알람 3회 → PotentialFailure 등) 을 파생. |
| S9 KG 검증 | 40~90초 | 25가지 check (bidirectional, fk_ref, undeclared_op, schema_ref, functional, disjoint, cardinality, prop_completeness 등) |
| S9.1 OWL sanity | 1~3분 | check_owl2_profile + validate_owl_consistency (HermiT) + run_entailment_regression (WARN-only) |
| S9.2 품질 점수 기록 | ~15초 | measure_instance_quality (91.6/100 류 5-메트릭 스코어) + read_inferred_delta 샘플 → quality_history |
| S9.5 KG Mutation | **7~8분** | **opt-in** — 8 mutant 샘플 (M1/M2/M3/M6), validate_kg 재호출 (WARN-only). baseline validate_kg 1회가 ~65초라 mutant 수에 비례 (실측 `duration_s` 448·450초) |
| S10~S11 딕셔너리 | ~15초 + 번역 시 Bedrock 응답 시간 | T-Box 분석 + A-Box 실측 통계. S10 도 S6.5 와 같은 조건에서 Bedrock 번역 1회를 호출한다. S11 `validate_semantic_dictionary` 는 로컬 |
| S12 질의 테스트 | 10~15초 (기본 `verify_joins=True`) / <1초 (`False`) | **Bedrock 0회 — LLM 미사용**, 프로그래매틱 연결성 검사다 (`query_test.py` 가 `bedrock_calls` 를 상수 0 으로 반환) |
| S12.5 골든 회귀 | 5~30초 (케이스 수 × 복잡도) | 사용자 입력 없으면 <1초 skip. WARN-only |
| S13 보고서 | ~5초 | HTML 생성 |
| N1 LPG 변환 | 30~60초 | rdflib → CSV |
| N2 LPG 딕셔너리 | ~10초 | CSV 분석 |
| N3 LPG 배포 | 1~3분 | APOC 배치 적재 |

### 0. 파이프라인 시작 전 — 체크포인트 확인 (필수)

파이프라인 실행 요청 시 **반드시 `check_pipeline_state` 먼저 호출**.
건너뛸 수 있는 단계가 있으면 안내.

### 1. 단계 진입 시 — 안내 + 예상 시간

```
## [워크플로우명] Step N/Total: {단계명}
{1-2문장 설명}
예상 소요시간: {위 테이블 참조}
실행할 도구: `{tool_name}`
```

장시간 도구(1분+) 실행 전 반드시 예상 시간 안내 후 바로 실행.
**파괴적 작업(neo4j_deploy_lpg replace=true 등)만 실행 전 확인** 필수.

### 2. 도구 실행 후 — 결과 보고

**결과 보고 규칙 (컨텍스트 절약):**
- 도구 반환 JSON 전체를 붙여넣지 말 것 — **핵심 수치만** 테이블로 요약
- 상세 목록은 사용자가 요청할 때만
- 21단계 파이프라인을 끝까지 진행하려면 대화 컨텍스트를 아껴야 함

### 3. PASS 시 — save_step + 자동 진행

1. `save_step(<정본 키>, {핵심결과}, duration_seconds={소요초})` 호출. 키는 위 상태머신 표의
   `키` 열 문자열을 **그대로** 쓴다 (정본: `tools/pipeline_state.py::_STEP_DEPS`). `save_step`
   은 이름을 검증하지 않아 오타는 성공 JSON 을 받고 영구 비가시가 된다 — 저장 후
   `check_pipeline_state` 로 그 단계가 보이는지 확인하라.
2. "다음 단계로 진행합니다." + 바로 실행
3. **업로드/배포 단계 직전**에서만 멈추고 확인

**PASS 를 보고하기 전에 무엇이 그것을 통과시켰는지 확인하라** — 이 리포에서 가장 자주 재발한
결함은 "게이트가 형식만 재서 근거 없는 PASS 를 냈다" 다:

- `applicable: false` / `reason` 이 붙은 항목은 PASS 가 **아니라 미판정**이다. 그런데
  `passed: true` 와 쌍으로 와 점수 분자에 포함되므로, 최상위 수치만 보면 구분되지 않는다
  (`checks[]` 를 직접 열어야 보인다).
- `test_domain_queries` 의 `schema_only_connections` / `cqs_with_schema_only_pass` 가 0 이
  아니면 그 CQ 는 **A-Box 0행 OP 에 기대** 통과한 것이다 (실측: 0행 OP 하나 선언으로
  66.7%→75.0%, 데이터 증가 0).
- 점수를 올리려 임계·CQ 문구·게이트 키를 고치지 말 것 — **지표 매수**다. 0행보다 그럴듯한
  오답이 위험하다.
- 게이트 통과를 품질로 읽지 말 것 (실측: 20/24 · 95.5점인데 CQ 실질 충족은 33% 였다).

### 4. FAIL 시 — 구조화된 에러 복구

```
### 문제 발견

| 항목 | 값 |
|------|-----|
| 실패 단계 | S{N}: {단계명} |
| 에러 | {에러 메시지 1줄} |
| 원인 | {docs/reference/troubleshooting.md 참조} |

### 해결 방안

1. [자동 수정 가능] → "자동으로 수정하겠습니다" + 실행
2. [사용자 판단 필요] → 선택지 제시 + 추천 표시
3. [외부 작업 필요] → 구체적 명령어 제시

### 재개 지점

수정 후 S{N}부터 재개합니다.
```

- 에러 메시지만 던지지 말 것 — **원인 → 해결책 → 재개 지점** 항상 함께 안내
- 자동 수정 가능한 SHACL 위반(domain, label, comment)은 즉시 수정 후 재검증
- critical 이슈(순환 프로퍼티 등)는 사용자에게 선택지 제시
- 재개 시 `check_pipeline_state`로 건너뛸 단계 확인
- **미측정을 FAIL 로 보고하지 말 것** — 판정 불가는 그 사실로 보고한다 (`applicable: false`).
  상시 빨간불이 된 게이트는 아무도 보지 않는다.

### 5. 워크플로우 전환

진행 중 다른 워크플로우 필요 시 (예: A-Box 생성 중 T-Box 없음):
현재 위치, 문제, 제안을 안내하고 전환 여부 확인.

### 6. 파이프라인 체크포인트 (단계 건너뛰기)

- 건너뛰기 판정은 **`check_pipeline_state` 의 `skippable` 만 신뢰한다.** 축별 규칙을 문서에
  박지 말 것 — 정본은 `tools/pipeline_state.py::_STEP_DEPS` 이고 의존 축은 단계마다 다르다
  (8개 축: `csv_mtime` `tacit_mtime` `tbox_mtime` `abox_mtime` `inferred_mtime` `swrl_mtime`
  `dict_contract_fingerprint` `lpg_csv_mtime`). "CSV 미변경 → S7 skip" 같은 3축 요약은
  실측에서 **6곳이 전부 over-permissive** 였다 (예: S7 은 `tbox_mtime` 과 딕셔너리 지문에도
  의존한다).
- ⚠️ 응답에는 skip **불가 이유가 없다** (필드는 `step`/`completed`/`skippable`/`completed_at`/
  `duration_seconds` 뿐). 원인을 알려면 서버 로그나 `_STEP_DEPS` 를 봐야 한다.
- 전체 재실행 → `reset_pipeline_state`

---

## 비파괴/파괴 작업 구분

### 자동 실행 (확인 불필요)
- 읽기: read_tbox, read_abox, read_csv_schema, read_semantic_dictionary, read_lpg_semantic_dictionary, list_csv_tables 등
- 검증: validate_ttl_syntax, check_quality_rules, validate_tbox_shacl, validate_kg 등
- 분석: analyze_tbox, sparql_local, visualize_tbox, neo4j_stats, neo4j_query 등
- 검색/조회: ask_neo4j 등
- 체크포인트: check_pipeline_state, reset_pipeline_state
- 존재 확인: check_competency_questions_exist, check_golden_queries_exist, golden_queries_schema_example, check_tacit_exist
- 암묵지 skip: skip_tacit_knowledge (의도적 건너뛰기 마커 생성)
- Meta-audit (진단 전용, 기존 검증 수정 없음): run_tbox_mutation_audit, run_kg_mutation_audit, run_meta_audit, get_meta_audit_history, read_meta_audit
  - `run_meta_audit` 결과에 **action_playbook** 필드 포함 (X3): blind_spot / dead_check / redundant pair 를 우선순위별 구체 액션 제안으로 제시. S13 보고서에도 자동 렌더.

### 확인 후 실행 ("진행할까요?" 필수)
- 생성: generate_tbox, generate_abox, convert_rdf_to_lpg (시간/비용 안내)
- 품질 개선: improve_tbox_quality (TTL 변경)
- LPG: generate_lpg_semantic_dictionary (딕셔너리 생성)
- 암묵지: add_tacit_from_natural_language, generate_tacit_from_rules, generate_tacit_from_data, suggest_tacit_rules (사용자 선택 후 호출)
- CSV 보강: augment_csv_fk (FK 컬럼 결정적 추가, idempotent)
- T-Box 직접 수정: `update_tbox_incremental` — S2 의 `save_guard` (손실률·산 링크) 를
  **거치지 않고** `TBOX_PATH` 를 덮어쓴다 (백업 없음, gitignore 대상이라 되돌릴 수 없다).
  후속 `improve`/`validate` 도 그 손실을 재지 않는다. `run_partial_pipeline_on_drift` 가
  이것을 자동 호출하므로 먼저 `dry_run=True` 로 대상을 보라 (프리뷰는 baseline 을 전진시키지
  않는다).

### 명시적 승인 필수 (반드시 확인 대기)
- 재생성: `generate_semantic_dictionary` — **파라미터에 따라 방향이 반대다**:
  · S10 (`include_stats=True`) 은 파이프라인 본체이고 통계를 **복원**하는 방향 → 승인 불필요.
  · S6.5 (`include_stats=False`) 는 기존 v2 가 있으면 통계를 **전부 0 으로 되돌린다**
    (실측: populated 클래스 74→0, abox_triples 1.3M→0). S10 이 뒤따를 때만 안전하다.
    산출물은 유효한 v2 처럼 보이고 `validate_semantic_dictionary` 도 PASS 한다 —
    **어느 게이트도 잡지 않는다** (소비자가 코드가 아니라 LLM 이다).
  · 파이프라인 밖 단독 재생성은 승인 대기.
- Neo4j: neo4j_deploy_lpg (특히 replace=true)
- rules 초기화: `initialize_domain_rules` 를 **`overwrite=True`** 로 부를 때만.
  이 도구가 **생성하는** 12개를 기본값으로 되돌리므로 SME 가 다듬은 값이 소실된다.
  `retire_stale_domain_files` 는 **기본 True** 이지만 이전 도메인 자산 6개를
  `.bak_<ts>` 로 **옮기는** 것이라 되돌릴 수 있다 — 확인 없이 실행해도 되고,
  응답의 `retired_domain_files` 를 사용자에게 보고하면 된다.

---

## 초기 상태 진단

모호한 요청 시 자동 진단:
1. `sparql_local` (`SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }`) → 0이면 "빈 KG. FULL_PIPELINE 시작?"
2. `analyze_tbox` → T-Box 없으면 "T-Box부터 생성?"
3. `read_abox` → A-Box 없으면 "A-Box 생성?"
4. 정상이면 "KG 정상. 어떤 작업?"

---

## 최종 보고서 (워크플로우 완료 시)

```
## 완료: {워크플로우명}

### 실행 요약
| 단계 | 상태 | 핵심 결과 |

### KG 현재 상태
- 총 트리플, 클래스, OP, DP, 인스턴스 수

### 후속 작업 제안
```

---

## 대화 스타일

- **한국어로 안내**, 도구 이름/파라미터/TTL 코드는 영어 유지
- 불필요한 설명 없이 **결과 중심으로 간결하게**
- 수치는 **테이블 형식** 선호
- 선택지는 **번호 매기기** + 추천 표시 (예: "2. [추천] ...")
- 오류 발생 시 **원인 → 해결책 → 자동 수정 여부** 순서로 보고

---

# 테스트·커밋

수치가 아니라 **판정 규칙**을 보라 (건수는 낡는다).

- 테스트와 lint 는 README Quick start 의 저장소 `venv` 에서 실행한다. 인터프리터는
  `pyproject.toml` 의 `requires-python` 을 만족해야 하고, 개발 의존성을 함께 설치한다:
  `python -m pip install -r requirements.txt -r requirements-dev.txt`.
- 기본 실행은 저장소 루트에서 `python -m pytest -q --no-header -rfE` 다. `pyproject.toml`
  의 `addopts` 가 `requires_bedrock`, `requires_java`, `scalability` 를 기본 제외한다.
  전체 스위트는 오래 걸리므로 stdout·stderr 를 로그 파일로 리다이렉트해 끝까지 수집한다.
  `-x` 는 쓰지 않는다. 첫 실패에서 멈추면 변경 전후 실패 집합을 비교할 수 없다.
- 시간을 아끼려면
  `-m 'not slow and not requires_bedrock and not requires_java and not scalability'` 를
  쓴다. **`-m 'not slow'` 단독은 함정**이다. 명시한 `-m`은 `pyproject.toml`의
  `addopts` marker 식을 덮어써 scalability, Bedrock, Java 테스트를 조용히 되살린다.
- 외부 전제가 있는 suite는 기본 green claim과 분리한다.
  `pytest -m requires_java`, `pytest -m requires_bedrock`, `pytest -m scalability`을
  각각 별도 로그로 실행하고, 미실행 또는 실패를 기본 suite 통과에 합치지 않는다.
  Bedrock suite는 실제 모델 호출과 비용이 발생한다.
- **회귀는 변경 전후의 실패 node id 집합으로 판정한다.** 환경에 따라 변경과 무관하게
  실패하는 테스트가 있을 수 있으므로 건수가 아니라 집합을 비교한다. 변경 전 로그에서 기준
  집합과 skip 수를 뽑고, 변경 후 로그를 `tests/public/test_release_candidate.py` 로 판정하면
  기준에 없는 실패와 skip 증가가 보고된다:

  ```bash
  python -m pytest -q --no-header -rfE > /tmp/pytest-before.log 2>&1   # 변경 전
  grep -E '^(FAILED|ERROR) ' /tmp/pytest-before.log \
    | sed -E 's/^(FAILED|ERROR) //; s/ - .*//' | sort -u > /tmp/pytest-before-failed.txt
  SKIPS=$(grep -Eo '[0-9]+ skipped' /tmp/pytest-before.log | tail -1 | cut -d' ' -f1)
  python -m pytest -q --no-header -rfE > /tmp/pytest-after.log 2>&1    # 변경 후
  python tests/public/test_release_candidate.py /tmp/pytest-after.log \
    --baseline /tmp/pytest-before-failed.txt --max-skips "${SKIPS:-0}"
  ```

  CLAUDE.md 자체도 테스트 대상이다 (`tests/test_r1_day6_claude_md_pipeline.py`).
- **Ruff 스코프는 하나다**:
  `app.py config.py server.py domain tools tests`. 수동 실행과 pre-commit hook
  (`.pre-commit-config.yaml`) 모두
  `python -m ruff check app.py config.py server.py domain tools tests`
  와 같은 집합을 사용한다. hook 은 `pre-commit install` 로 켜거나
  `python -m pre_commit run --all-files` 로 수동 실행한다. hook 의 자동수정 후에는
  `git diff` 로 범위를 확인하라.
- 게이트·검증을 고쳤으면 **결함을 직접 심어 잡히는지 확인**하라. mutator 결과나 카운터 증가로
  대신하지 말 것 — 통과가 "mutation 대상의 우연" 이었던 사례가 있다.

# 코딩 원칙

- **SRP / LSP / 생성자 주입**: 한 클래스 한 책임, 자식은 부모 대체 가능, 의존성은 생성자로.
- **OCP — 이 리포의 확장 규약은 구체적이다**. 새 기능을 기존 함수에 `if` 로 얹지 말고
  등록 지점을 쓴다:
  · T-Box 후처리 → `tools/quality_steps/step_NN_*.py` 한 파일 한 책임 + step 리스트 등록
  · KG check → `tools/validation_support/checks/` 에 함수 추가 + `CheckRegistry.register(...)`
    한 줄 (레지스트리 도입 전에는 4곳을 고쳐야 했다)
  · MCP 도구 → 함수 구현 + `tools/registry.py` 타깃 + manifest 이름 + 계약 테스트
- **네이밍**: 함수·변수는 `snake_case` 동사+목적어 (`calculate_total_price`). 실측 1,499개
  전부 snake_case 이므로 camelCase 를 새로 들이지 말 것.
- **주석·docstring 은 한국어**. 식별자·타입·로그 키는 영문이다. 공개 문서는
  English-first 로 쓰고 한국어 sibling 을 둔다.
- 공개 소스 주석은 현재 불변식과 재현 가능한 근거만 설명한다. 날짜, 작업 회차,
  임시 marker, 사고 연대기는 공개 소스에 남기지 않는다. 변경 이력의 정본은 git
  history 다.
