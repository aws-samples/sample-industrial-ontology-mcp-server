# 트러블슈팅

> CLAUDE.md에서 분리된 참조 문서. 에러 발생 시 `Read`로 로드.

## 1. OWL 추론 무한루프

**증상**: 추론 무한 진행, CPU 100%, 타임아웃
**원인**: `owl:TransitiveProperty` + `owl:inverseOf` 조합
**확인**: `check_quality_rules`가 `circular_property` (critical)로 자동 감지
**해결**: TransitiveProperty 또는 inverseOf 중 하나 제거

## 2. SHACL 검증 실패 유형별 대응

| 위반 유형 | 해결 | 자동 수정 |
|-----------|------|----------|
| domain/range 미스매치 | T-Box에서 수정 | O |
| missing label/comment | `rdfs:label`/`rdfs:comment` 추가 | O |
| subClassOf 이슈 | T-Box 재생성 또는 수동 수정 | X (LLM 필요) |
| dateTime 미스매치 | `T00:00:00` 자동 보정 | O (`_convert_datetime`) |
| decimal 비정형 값 | 해당 트리플 제거 | O (`_convert_decimal`) |

## 3. A-Box FK 감지 이슈

**증상**: A-Box에 ObjectProperty 연결 누락 (0 rows)
**원인**:
1. 컬럼명 정규화 누락 (`Item_Code` → `itemcode` 변환 실패)
2. subClassOf 미고려 (`ProcessBlastFurnace` ⊂ `ManufacturingProcessStep` OP 매칭 안 됨)
3. T-Box domain 과소 설정 (`hasEquipment` domain이 단일 클래스로 제한)

**해결**: `improve_tbox_quality` Step 10에서 공유 OP domain 자동 확장

## 3-b. DP 누락으로 인한 카디널리티 위반

**증상**: `validate_kg` 의 "카디널리티 제약 위반" check 가 FAIL. 특정 마스터
클래스 (예: EquipmentMaster) 의 전체 인스턴스가 PK DP (`equipmentId`) 를 못 가짐.
**원인**: T-Box 에서 해당 DP 의 `rdfs:domain` 이 bnode `owl:unionOf(...)` 로 선언되어
있고, A-Box 생성 로직의 `_parse_tbox` 가 bnode 자체만 domain 집합에 담아
`_class_matches_domain("EquipmentMaster", domains)` 가 모든 union member 에
False 를 반환 → 모든 EquipmentMaster 행에서 DP 가 생성되지 않음.
**해결**: `tools/abox_generation.py::_parse_tbox` 가 domain node 를
`_expand_domain_names` 로 처리해 unionOf bnode 의 member 클래스 local name 을
`dp_domains` set 에 풀어 넣습니다. 증상 재발 시 T-Box 의 DP 도메인 선언이 정말
union 인지 확인 후 A-Box 를 재생성하세요.

## 3-c. Closed-World master 고립 FAIL: reference catalog

**증상**: `validate_kg` 의 "Closed-World master 고립" check 가 FAIL. 특정 마스터
클래스(예: `EnergySourceMaster` 50개 중 30개 미사용 = 60%) 가 master tier
임계값 50% 를 넘겨 경보 발생.
**원인**: 해당 클래스가 **reference catalog** 성격임. 50 종을 정의했지만 실제
transaction 에서 소수만 사용되는 게 자연스러움.
**해결**: 코드 테이블임을 `rules/domain/domain_config.json` 의 `validation.catalog_classes`
배열에 선언. `_classify_class_tiers` 가 이를 최우선으로 catalog tier 로 분류하고,
master_orphan_rate 임계값이 85% 로 완화됩니다. 선언 후 `validate_kg` 재실행만으로
해결. (상세: [../reference/quality-framework.md](../reference/quality-framework.md) C.4)

## 3-d. Domain/Range FAIL 또는 AllDisjoint 위반: 다중 inverseOf

**증상**: `validate_kg` 의 "domain/range 타입 정합성" 이나 "AllDisjointClasses 위반"
check 가 FAIL. 이상한 inverseOf 관계가 잘못된 타입 전파 유발.
**원인**: `improve_tbox_quality` 의 Step 15c 에서 inverseOf 후보가 여러 개 남았을 때
느슨한 compat 기준으로 cross-domain inverseOf 를 잘못 유지.
**해결**: `_exact_match_score()` 를 0~3 점수제로 재설계 + `kept_one` 플래그로
"all-member compat + best score" 를 정확히 1개만 남김. 증상 재발 시
`improve_tbox_quality` 를 다시 실행한 뒤 재검증. 그래도 FAIL 이면 T-Box 수동 수정
후 기존 SHACL 파이프라인 통과 확인.

## 4. Bedrock 토큰 초과

**증상**: `stop_reason: "max_tokens"`, TTL 잘림
**해결**: `BEDROCK_MAX_TOKENS` 환경변수 조정 (기본 32000)
**inference profile 필수** (ap-northeast-2): `global.anthropic.claude-sonnet-4-6` 등 사용

## 5. 데이터 불일치

**T-Box/A-Box 버전 미스매치**: T-Box 변경 시 반드시 A-Box도 재생성
**로컬 데이터 경로**: `data/source/` (원본), `data/generated/` (산출물) — `config.py` 참조

## 6. 시맨틱 딕셔너리 이슈

**재생성 필요**: T-Box/A-Box 변경 후 `generate_semantic_dictionary` 실행
**클래스 0개**: `analyze_tbox`로 T-Box 확인, 네임스페이스가 `http://example.com/steel-ontology#`인지 확인

## 7. OWL 추론기 (HermiT / Pellet)

**Java 25+ 필수**: `JAVA_EXE` 환경변수로 경로 지정. 두 추론기의 요구가 다르다 —
HermiT 은 11+ 로 동작하지만 **Pellet 은 25 미만에서 전혀 동작하지 않는다** (아래 참조).
`ontology_agent_health_check` 의 Java 항목이 `degraded` 면 Pellet 경로가 죽어 있다.

**UnsupportedClassVersionError (class file version 69.0 / up to 65.0)**: Pellet 호출 시 발생.
Java 가 낡아서가 아니라 **owlready2 배포본 자체가 Java 25 바이트코드를 담고 있어서**다 —
0.50 이 CVE-2021-39239 (Jena RDF/XML XXE) 를 막으며 `LangRDFXML` 3개 클래스를 파싱이
비어있는 스텁으로 교체했고, 그 재컴파일을 JDK 25 에서 `--release` 없이 수행했다. 2013년
Java 6 (major 50) jar 안에 major 69 클래스 3개가 섞여 있다 (`jena-arq-fixed2.10.0.jar`).
Pellet 의 Jena 로더는 **입력과 무관하게** 정적 초기화 때 자기 설정 파일
(`etc/ont-policy.rdf`) 을 RDF/XML 로 읽으므로 그 클래스를 반드시 로드한다.

→ 해결: Java 25+ 설치 (`sudo dnf install -y java-25-amazon-corretto-headless`).
jar 를 0.49 판으로 되돌리는 우회는 **CVE 패치를 되돌리는 것**이고, 0.51 도 같은 jar 라
업그레이드로 해결되지 않는다. `--loader OWLAPIv3` 우회는 N-Triples 이스케이프를 조용히
파괴하므로 금지 (`"AA\nBB"` → `AAnBB`).

⚠️ **S4 통과를 Pellet 정상으로 읽지 말 것**: `_HERMIT_CLASSPATH` 는 오염된 jena-arq 를
포함하지 않으므로 (Pellet 만 `pellet/` 전체를 와일드카드로 끌어온다) HermiT 기반 게이트
5종은 Java 21 에서도 전부 통과한다.

**추론 시간**: HermiT ~0.5초 (워밍업 후), Pellet ~3초 (realization 포함)

### 7-a. `validate_owl_consistency` degraded 모드

**문제**: OWL 일관성 검사에 쓰는 `owlready2` 라이브러리에 버그가 있어, 복잡한
T-Box 를 만나면 reasoner 가 전체 결과를 반환하지 못하고 중간에 에러로 뻗는
경우가 있습니다.

**해결** (3단계 graceful degradation — 모든 걸 잃는 대신 얻을 수 있는 만큼은 반환):

| 시도 | 하는 일 | 얻는 결과 |
|---|---|---|
| 1차 | 전체 검사 (property value inference 포함) | 성공 시 완전한 결과 |
| 2차 (1차 실패 시) | 자동 재시도 — `infer_property_values=False` 로 무거운 단계 끔 | consistency + classification 만 반환 |
| 3차 (2차도 실패 시) | 부분 결과라도 `success: true` 로 리턴 | 가능한 만큼만 (결과 JSON 에 degraded 플래그 표시) |

**결과 JSON 에 보이는 플래그**:
- `property_value_inference_degraded`: 2차 재시도로 폴백됨
- `reasoner_post_merge_failed`: reasoner 가 완전히 뻗음 (부분 결과만 있음)

**언제 걱정해야 하나**: Property value inference 결과가 **실제로 필요한 경우**만.
이 경우 `validate_owl_realisation` (다른 reasoner 인 Pellet 사용) 으로 교차
확인하세요. Consistency 체크나 클래스 분류만 원했다면 degraded 모드 결과로
충분합니다.

## 8. generic DP 관련 이슈 (class-specific DP 정책)

### 8-a. SPARQL 쿼리가 0건 리턴 — `steel:hasStatus` / `hasTimestamp` / `hasIdentifier` 등

**증상**: 기존 SPARQL 쿼리가 갑자기 모두 빈 결과 반환. 특히 Path B 재적재 이후.
**원인**: Path B (2026-05-11) 이후 generic DP 이름 (`steel:hasStatus`, `steel:hasTimestamp`, `steel:hasIdentifier`) 는 A-Box 에 더 이상 생성되지 않음. Class-specific 이름 (`equipmentStatusValue`, `alarmEventsTimestamp`, `equipmentMasterId` 등) 으로 전환됨.
**해결**:
1. `data/generated/semantic_dictionary.json` 에서 해당 class 의 `datatype_properties` 확인 → 실제 이름 조회
2. SPARQL 쿼리를 class-specific 이름으로 재작성. 딕셔너리의 `datatype_properties` 가 정본 이름이므로, generic 이름을 그 값으로 1:1 치환하면 된다
3. `ask_ontology` MCP 도구는 딕셔너리 기반으로 자동 대응 — 자연어 쿼리는 재작성 불필요

### 8-b. `improve_tbox_quality` 로그에 "generic DP detected" warning

**증상**:
```
[Step 12b] generic DP detected: hasStatus (domain=EquipmentStatus) — TBOX_STRICT_CLASS_SPECIFIC=warn
```
**원인**: Architect LLM 이 프롬프트 규칙을 지키지 않고 generic 이름으로 DP 생성. `_enforce_class_specific_dps` 후처리가 감지.
**해결**:
- `TBOX_STRICT_CLASS_SPECIFIC=rename` 환경변수로 자동 교정 (domain 단일 class 인 경우만)
- `TBOX_STRICT_CLASS_SPECIFIC=remove` 로 generic DP 삭제 후 다음 라운드 재생성 유도
- 혹은 T-Box 수동 수정 + `improve_tbox_quality` 재실행. 상세: [../reference/quality-framework.md](../reference/quality-framework.md) Appendix C.4b

### 8-c. coverage gate 미달 — `coverage_ratio_overall < 0.85`

**증상**:
```
[Step 12c] coverage_ratio_overall=0.62 (threshold=0.85) — 15 classes below gate
```
**원인**: Architect LLM 이 numeric 센서 / 화학 성분 / 공정 파라미터 컬럼을 DP 로 선언하지 않음 (특히 ChemicalAnalysis 의 `C_Percent`/`Si_Percent`/`Mn_Percent`, EquipmentMaster 의 `equipment_name`/`type`). CSV 컬럼 커버리지 게이트 (`_check_csv_column_coverage`) 가 감지.
**해결**:
1. **warn 모드 (기본)** — stats 기록만. 재생성은 SME 판단. 다음 S2 실행 시 L1 프롬프트 (`prompts/tbox-prompt-modules/04-property-rules.md` 의 "Path B naming" 절 전수 매핑 + "Coverage self-check" 절) 가 gap 자동 해소 유도
2. **fail 모드** — `TBOX_COVERAGE_GATE=fail` 로 CI/CD 엄격 검증. 미달 시 `RuntimeError` 로 파이프라인 중단
3. 임계치 조정 — `TBOX_COVERAGE_THRESHOLD=0.75` 등으로 완화 가능 (권장 X)
4. 개별 class 미달 시 `coverage_per_class` stats 에서 `missing` 컬럼 목록 확인 → T-Box 수동 DP 추가

### 8-d. `tacit` TTL 에 하드코딩된 generic DP

**증상**: `data/source/tacit/*.ttl` 에 `steel:hasStatus`, `steel:hasIdentifier` 참조 있음.
**확인**:
```bash
grep -r "hasStatus\|hasIdentifier\|hasTimestamp" data/source/tacit/*.ttl
```
**해결**: 수동으로 class-specific 이름으로 교체. Path B 이후 A-Box 가 class-specific DP 만 생성하므로 tacit 의 generic 참조는 orphan triple 이 되어 추론 결과가 불완전해짐.

## 9. Python scope 함정 — 함수 스코프 `from X import Y` 가 만든 silent fail

**증상**: 어떤 step / 로직이 try/except 로 감싸져 있고, 로그에 다음과 같은 WARNING 이 자주 나오면 의심.

```text
WARNING tools.ontology_quality:ontology_quality.py:NNNN
  공유 FK domain 확장 실패: cannot access local variable
  'SOURCE_RAWDATA_DIR' where it is not associated with a value
```

해당 step 의 stat (예: `domain_broadened`) 이 항상 0 으로 보고되는데도 코드 경로가 정상 실행돼야 할 조건이 갖춰진 경우.

**근본 원인** — Python 의 [naming-and-binding](https://docs.python.org/3/reference/executionmodel.html#naming-and-binding) 규칙:

> "If a name is bound in a block, it is a local variable of that block, unless declared as nonlocal or global."

`from X import Y` 는 **Y 에 대한 binding** 이므로, 함수 본문 어디든 한 번이라도 `from X import Y` 가 있으면 **그 함수의 모든 코드에서 Y 는 로컬 이름**이 된다. 모듈 최상단에서 이미 `from X import Y` 로 import 했어도, 함수 본문에서 같은 이름이 재 import 되면 그 함수에서는 **로컬 binding 이전에 사용 시 `UnboundLocalError`** 가 발생.

함수 본문이 try/except 로 광범위하게 감싸져 있다면 이 오류가 silent fail (stat 0, WARNING 만) 로 묻힌다.

**예시 (실측 — 2026-05-10 발견, 2026-05-16 수정)**

```python
def improve_tbox(ttl_content: str) -> tuple:
    # L40 module-level import (다른 step 들이 이미 사용)
    from config import SOURCE_RAWDATA_DIR  # 모듈 scope 가 아니라 함수 정의 위쪽

    # ... Step 10 (line ~3000): SOURCE_RAWDATA_DIR 참조
    try:
        for csv_path in glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")):
            ...  # ← UnboundLocalError, except 가 삼킴
    except Exception as e:
        logger.warning("공유 FK domain 확장 실패: %s", e)

    # ... Step 15 (line ~3800):
    try:
        from config import SOURCE_RAWDATA_DIR  # ← 함수 전체에서 SOURCE_RAWDATA_DIR 를
                                                #   local 로 만든 binding
        ...
    except Exception:
        ...
```

**해결**:

1. **함수 본문의 재 import 전부 제거** — 모듈 최상단 import 만 사용:

   ```python
   # 모듈 최상단에 이미 import 됨:
   # from config import SOURCE_RAWDATA_DIR

   def improve_tbox(...):
       # ... 함수 본문에서 추가 import 금지
   ```

2. **재 import 가 꼭 필요하면 (예: 테스트가 ``monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", ...)`` 로 동적 patching)**:

   ```python
   def some_helper(g, ctx):
       import config  # 모듈 자체를 import — 이름 binding 이 ``config`` 라
                      # ``SOURCE_RAWDATA_DIR`` 의 함수-스코프 바인딩에 영향 없음
       SOURCE_RAWDATA_DIR = config.SOURCE_RAWDATA_DIR  # 동적 lookup
       ...
   ```

3. **거대 함수 분할** — 한 함수가 너무 길어 같은 이름을 여러 번 import 하는 게 자연스러워지는 상황 자체가 anti-pattern. 함수를 step 모듈로 분리하면 import 충돌이 자동 해소.

**재발 방지 회귀 테스트** (`tests/test_improve_tbox_step10_activation.py`):

```python
def test_sourcerawdata_dir_not_reimported_in_improve_tbox():
    """원인 A 재발 방지 — improve_tbox 내 ``from config import SOURCE_RAWDATA_DIR`` 0건."""
    import inspect
    from tools import ontology_quality as oq
    source = inspect.getsource(oq.improve_tbox)
    assert "from config import SOURCE_RAWDATA_DIR" not in source

def test_step_10_runs_without_unbound_local(caplog):
    """Step 10 실행 시 UnboundLocalError WARNING 미발생."""
    from tools.ontology_quality import improve_tbox
    with caplog.at_level("WARNING"):
        improve_tbox("...")
    assert all(
        "공유 FK domain 확장 실패" not in r.message for r in caplog.records
    )
```

## 10. SPARQL 디버깅 — 자주 만나는 3가지 패턴

자연어 질의로 만든 SPARQL 이 **0건 반환** 또는 **에러** 일 때. 강사가 워크샵 중
참가자 앞에서 시연하는 경우가 많아 별도 정리.

### 10-a. 프로퍼티명 mismatch (가장 흔함)

**증상**:
```sparql
SELECT ?eq ?tag WHERE { ?eq steel:hasSensor ?tag }
```
→ 0건.

**원인**: T-Box 가 `steel:hasTag` 로 생성됐는데 직관으로 `hasSensor` 를 작성. LLM 이
T-Box 미참조 상태로 생성 시 자주 발생.

**해결**:
1. `read_semantic_dictionary` → `object_properties` 키 펼쳐 실제 프로퍼티명 확인
2. 또는 `sparql_local("SELECT ?p (COUNT(*) AS ?c) WHERE { ?s ?p ?o } GROUP BY ?p ORDER BY DESC(?c) LIMIT 20")` 로 실제 사용 프로퍼티 탐색
3. 쿼리 수정: `steel:hasSensor` → `steel:hasTag`

### 10-b. FILTER 과잉 (조건 중복)

**증상**:
```sparql
SELECT ?eq WHERE {
  ?eq a steel:EquipmentMaster ;
      steel:location ?loc .
  FILTER (?loc = "Plant1" && CONTAINS(?loc, "Plant"))
}
```
→ 0건.

**원인**: `?loc` 가 literal 인데 `CONTAINS` 가 type 불일치. 또는 실제 값이
`"Plant 1"` (공백 포함) 이라 `=` 매칭 실패.

**해결**:
1. FILTER 제거하고 `?eq steel:location ?loc` 만 실행 → 실제 값 확인
2. 실제 값이 `"Plant 1"` 이면 `FILTER(CONTAINS(?loc, "Plant"))` 만 남김
3. 일반 원칙: **"FILTER 하나씩 끄면서 0건이 언제 생기는지 관찰"**

### 10-c. OPTIONAL 누락 (INNER JOIN 으로 인한 누락)

**증상**:
```sparql
SELECT ?eq ?maint WHERE {
  ?eq a steel:EquipmentMaster .
  ?eq steel:hasMaintenanceHistory ?maint .
}
```
→ 일부 설비만 나옴 (전체의 20%).

**원인**: 정비 이력이 없는 설비는 INNER JOIN 되어 제외. SQL `LEFT JOIN` 에 해당하는
SPARQL `OPTIONAL` 이 필요.

**해결**:
```sparql
SELECT ?eq ?maint WHERE {
  ?eq a steel:EquipmentMaster .
  OPTIONAL { ?eq steel:hasMaintenanceHistory ?maint }
}
```
정비 이력 없는 설비는 `?maint` = UNBOUND 로 표시됩니다. **SPARQL `OPTIONAL` =
SQL `LEFT JOIN`** 을 기억하세요.

### 일반 디버깅 원칙

| 단계 | 점검 |
|------|------|
| 1 | "Claude 가 만든 SPARQL 보여줘" — 자동 생성된 쿼리부터 확인 |
| 2 | 시맨틱 딕셔너리에서 **실제 프로퍼티명** 검색 |
| 3 | FILTER 하나씩 제거 → 어디서 0건이 되는지 |
| 4 | INNER JOIN 누락 의심 시 `OPTIONAL` 로 감싸기 |
| 5 | 10분 넘게 막히면 — 깊은 원인 (T-Box 결함) 의 신호. `validate_kg` 의 domain/range check 결과 확인 |
