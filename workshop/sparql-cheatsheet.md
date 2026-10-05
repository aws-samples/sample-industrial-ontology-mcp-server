# SPARQL 치트시트 — SQL 엔지니어용

> **워크샵 패키지 버전:** 4.4 (2026-05-30 — cheatsheet 가독성 reorg: 두 패턴 섹션 분리 / 상단 박스 5→2 압축 / 0건 디버깅 위치 이동 / 방법 A~F 3그룹 재구성 / 엔진 호환성 부록 처리)

## 🎯 이 워크샵의 한 줄 약속 (5개 문서 공통)

> **"당신은 3시간 후 (a) Knowledge Graph 의 가치를 비유 없이 1분 안에 설명할 수 있고, (b) Claude 가 만든 SPARQL 의 정확성을 직접 검증할 수 있고, (c) 자사 도메인 PoC 의 Day 1 에 무엇부터 시작할지 정확히 안다."**

본 치트시트는 약속 (b) 의 **핵심 도구** — Claude SPARQL 의 정확성 검증.

## SPARQL 어휘 5단어 — 처음 본 사람도 1분 안에 (Ch5 시작 5분 마이크로 강의)

| 기호 | 의미 | 예시 | 한국어 풀이 |
|------|-----|------|---------|
| `?eq` | 변수 (찾고 싶은 것) | `?eq` | "이 자리에 들어갈 것을 찾아줘" |
| `a` | `rdf:type` 약어 | `?eq a steel:EquipmentMaster` | "?eq 의 타입은 EquipmentMaster" |
| `;` | 같은 주어 계속 | `?eq a Cls ; name ?n` | "?eq 에 대해 또 한 가지 더" |
| `.` | 트리플 끝 (마침표) | `?eq a Cls .` | "여기까지 한 트리플 끝" |
| `prefix:` | 네임스페이스 약어 | `steel:EquipmentMaster` | "steel: = http://..." |

**이 5개 + SQL 기본 키워드 (`SELECT`/`WHERE`/`FILTER`/`GROUP BY`/`LIMIT`) 면 본 치트시트의 90% 가 읽힙니다.** 나머지 10% (OPTIONAL/COUNT/SUM 등) 는 본문에서 등장 시 풀이.

## SQL 과 SPARQL 의 3가지 차이 (30초)

| 개념 | SQL | SPARQL |
|------|------|--------|
| **데이터 단위** | 행(row) | 트리플 `(subject, predicate, object)` |
| **JOIN** | 명시적 `JOIN ON` | 자동 — 같은 변수명이 곧 JOIN |
| **스키마** | 고정 테이블 | 유연 — 스키마 변경에 SELECT 수정 불필요 |

**핵심 비유**: SPARQL 의 `WHERE { ... }` 블록은 **그래프 퍼즐 조각** 을 묘사합니다. 같은 이름의 변수(`?x`) 가 두 곳에 나오면 "둘을 같게 만드는 값을 찾아줘" 라는 뜻 → SQL JOIN 과 동일한 효과를 **변수 이름만으로** 표현.

> **자사 도메인 적용 시** — 모든 코드 블록의 `steel:` 를 본인 prefix (`rules/domain/domain_config.json`) 로 일괄 치환하면 그대로 동작. 인스턴스 네임스페이스는 보통 prefix + `-inst`.

> **본인이 `generate_tbox_collaborative` 를 다시 돌리면** Multi-Agent 가 다른 OP 이름을 만들 수 있습니다. 본인 환경 OP/DP 확인은 아래 §"프로퍼티명 확인하는 법" 의 6가지 방법 참조.

---

## SQL ↔ SPARQL 1:1 대응 (SQL 엔지니어용 빠른 사전)

SQL 을 이미 아는 엔지니어가 30초 안에 SPARQL 변환 감을 잡기 위한 표. 세부 패턴은 아래 섹션 참조.

| SQL | SPARQL | 비고 |
|-----|--------|------|
| `SELECT ... FROM` | `SELECT ... WHERE { }` | 트리플 패턴 매칭 |
| `JOIN ... ON` | 변수 공유 (자연스러운 JOIN) | `?eq`가 두 패턴에 나타나면 자동 JOIN |
| `LEFT JOIN` | `OPTIONAL { }` | 없어도 결과 포함 |
| `WHERE col = 'val'` | `FILTER (?var = "val")` | 또는 트리플 패턴에서 직접 값 지정 |
| `LIKE '%검색%'` | `FILTER(CONTAINS(?var, "검색"))` | 대소문자 무시: `LCASE()` |
| `GROUP BY ... COUNT(*)` | `GROUP BY ?var (COUNT(*) AS ?cnt)` | 동일 |
| `ORDER BY col DESC` | `ORDER BY DESC(?var)` | 동일 |
| `LIMIT 10` | `LIMIT 10` | 동일 |
| `IN ('a','b')` | `FILTER(?var IN ("a", "b"))` | VALUES 절도 가능 |
| `WHERE col IS NULL` | `FILTER NOT EXISTS { ?s :col ?o }` | OWA: 트리플 자체 부재 — SQL 의 NULL 과 의미가 다름. "값 없음" 이 아니라 "이 그래프에 기록 안 됨" |
| `WHERE col IS NOT NULL` | 트리플 패턴 존재 (`?s :col ?o`) | 단순 패턴 매칭으로 충분 |
| `UNION ALL` | `{ } UNION { }` | OR 조건 |
| `INSERT INTO` | `INSERT DATA { }` | 트리플 추가 |
| `DELETE FROM` | `DELETE DATA { }` | 트리플 삭제 |

---

## SPARQL 기본 syntax (입문)

처음 SPARQL 을 작성할 때 가장 자주 쓰는 6가지 패턴. SQL 의 기본 syntax 와 1:1 대응.

### 기본 조회 (SELECT)
```sparql
SELECT ?id ?name WHERE {
  ?eq a steel:EquipmentMaster ;
      steel:equipmentMasterId ?id ;
      steel:equipmentMasterName ?name .
}
```

### JOIN (트리플 패턴 연결)
```sparql
# 설비 → 센서 태그 (변수 ?eq 로 자연 JOIN)
# 주의: pre-generated 에서 OP 방향은 Tag → tagEquipment → Equipment (TagMaster 가 domain)
SELECT ?eqName ?tagName WHERE {
  ?eq a steel:EquipmentMaster ; steel:equipmentMasterName ?eqName .
  ?tag steel:tagEquipment ?eq ; steel:tagMasterName ?tagName .
}
```

### LEFT JOIN (OPTIONAL)
```sparql
SELECT ?name ?date WHERE {
  ?eq a steel:EquipmentMaster ; steel:equipmentMasterName ?name .
  OPTIONAL { ?m steel:hasMaintenanceEquipment ?eq ; steel:maintenanceHistoryDate ?date . }
}
```

### 필터링
```sparql
# 숫자 비교
FILTER (?temp > 1500)

# 문자열 검색
FILTER (CONTAINS(LCASE(?name), "펌프"))

# 날짜 범위 (일부 SPARQL 엔진은 YEAR()/MONTH() 미지원)
FILTER (?date >= "2025-09-01T00:00:00"^^xsd:dateTime &&
        ?date <  "2025-10-01T00:00:00"^^xsd:dateTime)
```

### 집계 (GROUP BY)
```sparql
SELECT ?status (COUNT(*) AS ?cnt) WHERE {
  ?es a steel:EquipmentStatus ; steel:equipmentStatusValue ?status .
} GROUP BY ?status ORDER BY DESC(?cnt)
```

### 추론 결과 확인

<!-- zero-rows-ok: inverseOf-only-after-inference -->
```sparql
# inverseOf로 자동 생성된 역방향 관계
# pre-generated 의 native 방향은 hasEfficiencyEquipment (EnergyEfficiency→EQ, 1,500건) 이고
# inverse 는 equipmentHasEnergyEfficiencyRecord (EQ→EnergyEfficiency). 추론 후
# (sparql_local default source="inferred") 에는 역방향도 자동 채워짐. 본 cheatsheet 의
# verify 스크립트는 pre-generated 만 로드해 0 행이 정상 — 실제 워크샵 환경
# (추론 산출물 로드) 에서는 결과 나옴
SELECT ?eq ?profile WHERE {
  ?eq steel:equipmentHasEnergyEfficiencyRecord ?profile .  # 명시적으로 입력하지 않은 관계 (inverseOf 추론)
} LIMIT 10
```

---

## 0건 디버깅 — 가장 자주 만나는 3가지

자연어로 쿼리 요청했는데 0건 리턴되면 **이 순서로 점검** (3분 안에 80% 해결):

1. **값 범위 확인** (아래 §"프로퍼티명 확인하는 법" 방법 F) — `FILTER(?t >= 2000)` 인데 실제 값 max 가 1680?
2. **OP 이름 확인** (방법 A) — `?eq hasAlarm ?a` 인데 실제는 `?alarm hasAlarmTag ?tag . ?tag isTagOf ?eq` (Tag hub) 2-hop?
3. **FILTER 제거 후 결과 수** — 한 조건씩 다시 추가

> **팁**: SPARQL 을 직접 작성하지 않아도 됩니다. Claude Code 에 자연어로 물어보면 SPARQL 자동 생성·실행. 0건 리턴 시 "방금 실행한 SPARQL 보여줘" 로 검증.

---

## 온톨로지 진단 쿼리 6가지 (품질 검증)

KG 가 건강한지 확인할 때 쓰는 6가지 쿼리. 단순 조회가 아니라 **각 결과의 정상 vs 비정상 해석** 까지 포함.

### 1. 클래스별 인스턴스 분포 — "우리 KG 에 뭐가 얼마나 있나?"

```sparql
SELECT ?cls (COUNT(DISTINCT ?i) AS ?cnt) WHERE {
  ?i a ?cls . FILTER (!isBlank(?cls))
} GROUP BY ?cls ORDER BY DESC(?cnt)
```

> **진단 신호**: 결과에 `cnt = 0` 인 클래스가 많으면 → T-Box 에는 선언됐지만 A-Box
> 생성 시 매핑 안 된 "빈 클래스". `rules/domain/table_class_mapping.json` 에 누락 매핑 확인.
> 반대로 상위 소수 클래스가 전체 인스턴스의 80%+ 를 차지하면 **파레토 쏠림** —
> 정상 (master data < transaction data 는 자연스러움).

### 2. 실제 쓰이는 프로퍼티 확인 — "T-Box 스펙 말고 A-Box 에서 진짜 등장하는 것"

```sparql
SELECT ?p (COUNT(*) AS ?c) WHERE { ?s ?p ?o }
GROUP BY ?p ORDER BY DESC(?c) LIMIT 30
```

> **진단 신호**: `analyze_tbox` 와 비교해 **T-Box 에 선언됐는데 여기 없는 프로퍼티** =
> dead property (사용 안 됨 → 제거 후보). 반대로 여기 나오는데 T-Box 에 없으면
> 스키마 누락 (A-Box 가 상상으로 만든 관계 — 버그).

### 3. FK 무결성 체크 — "참조되는 ID 가 실제 존재하나?"

<!-- zero-rows-ok: clean-pre-generated -->
```sparql
# 예: isTagOf 가 참조하는 EquipmentMaster 인스턴스가 없으면 dangling
# pre-generated 는 정상 데이터라 0 행이 정상 — 자사 도메인 첫 실행 시 종종 발견
SELECT ?subject ?missing WHERE {
  ?subject steel:tagEquipment ?missing .
  FILTER NOT EXISTS { ?missing a steel:EquipmentMaster }
} LIMIT 20
```

> **왜 중요한가**: dangling FK 는 A-Box 품질의 **심각한 결함**입니다.
> 영향: (1) OWL 추론 시 해당 FK 기반 신규 트리플 생성 실패 (2) SPARQL JOIN
> 결과가 **자동으로 축소** — 사용자가 "데이터 누락" 을 인지하지 못한 채 잘못된
> 분석 수행 (3) CQ 답변 가능성 감소.
> 자동 탐지: `validate_kg` 의 `fk_referential_integrity` + `dangling_references` check.

### 4. 필수 값 누락 탐지 (OWA 주의) — "label 없는 클래스 있나?"

<!-- zero-rows-ok: 공리 노드를 제외하면 실제 label 누락이 0 (실측 2026-08-29) -->
```sparql
# 명명된 클래스 중 label 이 없는 것 — 0 행이 정상이다.
# 공리 노드 필터를 빼면 7 행이 나오는데 전부 `Union_*` 다 (아래 주의 참조).
SELECT ?cls WHERE {
  ?cls a owl:Class .
  FILTER NOT EXISTS { ?cls rdfs:label ?l }
  FILTER (!isBlank(?cls))
  # 공리 노드 제외: owl:unionOf / owl:intersectionOf / owl:onProperty 를 가진
  # 것은 skolemize 된 표현식이지 분류 클래스가 아니다.
  FILTER NOT EXISTS { ?cls owl:unionOf ?u }
  FILTER NOT EXISTS { ?cls owl:intersectionOf ?i }
  FILTER NOT EXISTS { ?cls owl:onProperty ?p }
}
```

> ⚠️ **`isBlank` 만으로는 부족할 수 있다**: 파이프라인은 익명 클래스 표현식에 이름을
> 붙여 저장할 수 있고 (예: `steel:Union_RealTimeData_01877de4`) 그러면 `isBlank`
> 필터를 통과한다. 이들은 `owl:unionOf` 를 담은 **공리 노드**라 사람이 읽는 label 이
> 없는 것이 정상이다. 이 구분을 놓치면 "label 누락 N건" 이라는 허위 결함이 잡히고,
> 반대로 클래스 수를 세는 질의는 실제보다 부풀어 나온다 (2026-08-29 산출물 실측:
> 명명 클래스 90 vs `owl:Class` 전체 97 — 차이 7건이 전부 공리 노드였다).
>
> **현재 동봉된 산출물(2026-09-06)에는 명명 공리 노드가 0개다**: `owl:Class` 89개
> 전부가 `rdfs:label` 을 가진 분류 클래스이므로 위 질의는 0행이 정상이고 위 3개
> `FILTER NOT EXISTS` 는 no-op 이다. 그래도 필터를 지우지 말 것 — 세대마다 달라지는
> 축이고, 자사 T-Box 에서는 다시 나타난다.

> **OWA 주의**: "결과 없음" 이 "label 이 모두 있음" 을 의미하지 않을 수 있음
> (Open World — 어딘가 다른 그래프에서 label 이 선언됐을 가능성). 로컬 그래프만
> 볼 때는 신뢰할 수 있지만, 외부 SPARQL 엔드포인트에 배포된 후 다른 그래프와
> 합쳐지면 결과가 달라질 수 있음. SHACL 의 `sh:closed` 로 닫힌 세계 검증이 필요한 이유.

### 5. 고아 노드 — "아무데도 연결 안 된 인스턴스"

<!-- zero-rows-ok: clean-pre-generated -->
```sparql
# pre-generated 의 모든 인스턴스는 최소 1개 OP/DP 보유 → 0 행이 정상
SELECT ?i WHERE {
  ?i a ?cls .
  FILTER NOT EXISTS { ?i ?p ?o . FILTER (?p != rdf:type) }
  FILTER NOT EXISTS { ?s ?p ?i }
} LIMIT 20
```

> **진단 신호**: 고아 노드 = **DP (DatatypeProperty) 또는 OP 가 완전히 빠진 인스턴스**.
> 원인 후보: (1) CSV 행이 모두 null (데이터 품질 문제) (2) T-Box 에 해당 클래스의
> 프로퍼티가 선언 안 됐음 (AR 낮음) (3) FK 매칭 실패로 OP 가 생성 안 됨.
> 해결: 먼저 `profile_csv_data` 로 CSV null 비율 확인, 그 다음 `measure_tbox_metrics`
> 의 AR 점수 확인.

### 6. 인스턴스 전체 속성 덤프 — "이 ID 가 뭘 가지고 있나?"

```sparql
# Claude Code: sparql_local — SELECT ?p ?o WHERE { steel-inst:EquipmentMaster_EQ001 ?p ?o }
SELECT ?p ?o WHERE { steel-inst:EquipmentMaster_EQ001 ?p ?o }
```

> **디버깅 팁**: 추론 전 / 추론 후 결과를 비교해 "OWL 추론이 실제로 뭘 추가했는가"
> 확인 가능. 예: 추론 전엔 `hasEquipment` 만 있다가 추론 후엔 `isEquipmentOf`
> (inverseOf) + 상위 클래스 `rdf:type` 트리플이 생김.

---

## 프로퍼티명 확인하는 법 (필수)

T-Box 는 LLM 이 매번 다르게 생성하므로, 이 치트시트의 `steel:tagEquipment` 같은 이름이
본인 KG 에는 `steel:ownsEquipment` 로 돼 있을 수 있습니다. **쿼리 전에 한 번 확인:**

### 본인 환경 OP/DP 매핑 표 (3분 작업, 권장)

자사 적용 첫날 가장 시간 잡아먹는 작업. 본인 KG 의 핵심 OP/DP 를 종이/노트에 미리
적어두면 SPARQL 작성이 빠릅니다 (Ch3-2 의 메모 prompt 와 동일):

| 분류 | 본인 환경 OP/DP | 워크북/cheatsheet 의 동등 이름 (참고) |
|------|---------------|---------------------------------|
| OP (관계 1) | 예: `myind:isOwnedBy` | `steel:tagEquipment` |
| OP (관계 2) | | `steel:hasMaintenanceEquipment` |
| OP (관계 3) | | `steel:hasAlarmTag` |
| DP (값 1) | 예: `myind:productName` | `steel:equipmentMasterName` |
| DP (값 2) | | `steel:alarmEventsAlarmType` |

이 표를 한 번 작성하면 cheatsheet 의 `steel:` SPARQL 을 본인 환경 prefix + 이름으로
변환할 때 (post-workshop §"D+1 첫 숙제" 의 sed 치환) 빠르게 매핑됩니다.

### 그룹 1 — 이름 확인 (방법 A~D, 4가지 경로)

본인 KG 의 OP/DP 이름을 찾는 4가지 fallback 경로. **A 부터 시도** → 안 되면 B → C → D.

#### 방법 A — Claude Code 에 자연어 요청 (가장 빠름, 권장)
```
시맨틱 딕셔너리 보여줘
```
→ `read_semantic_dictionary` 도구 실행 → `data/generated/semantic_dictionary.json` 내용 반환.
클래스/프로퍼티 목록과 SPARQL 예시가 포함됨.

#### 방법 B — T-Box 분석 도구
```
T-Box 분석해줘
```
→ `analyze_tbox` 실행 → 클래스/OP/DP 전체 목록 + 계층 구조 + DIT/RR/AR 메트릭.

#### 방법 C — 파일 직접 조회
```bash
# 실제 쓰이는 프로퍼티 로컬에서 grep
grep "^steel:" data/generated/tbox/t_box.ttl | head -50
```

#### 방법 D — SPARQL 로 직접 탐색

OP 전체 목록:
```sparql
SELECT ?p WHERE { ?p a owl:ObjectProperty } ORDER BY ?p
```

DP 전체 목록:
```sparql
SELECT ?p WHERE { ?p a owl:DatatypeProperty } ORDER BY ?p
```

### 그룹 2 — 2-hop 경로 (방법 E)

직접 관계 (`?eq hasAlarm ?a`) 가 0건 나올 때 — Tag 같은 **공유 노드를 거치는 경로** 로 우회.

#### 방법 E — 2-hop 경로로 우회

**자연어로 요청**:
```
EquipmentMaster 의 2-hop 경로 보여줘.
AlarmEvents 와 어떻게 연결되는지.
```
→ Claude 가 시맨틱 딕셔너리의 2-hop 경로 정보 + 예시 SPARQL 반환.

**예시 (pre-generated 실제 schema)**: "설비별 최근 알람" 요청 →
Claude 가 `?eq steel:hasAlarm ?a` 생성 (0건, 그런 OP 없음) →
실제로는 `?tag steel:tagEquipment ?eq . ?alarm steel:hasAlarmTag ?tag` (Tag 를 공유 노드로
2-hop join) 필요. EquipmentMaster 와 AlarmEvents 가 Tag 를 통해 간접 연결되는 구조.

### 그룹 3 — 값 범위 검증 (방법 F)

`FILTER(?t >= 2000)` 같은 숫자 조건이 실제 데이터 범위 안인지 사전 확인.

#### 방법 F — 값 범위 확인

**자연어로 요청**:
```
temperature DP 의 값 범위 (min/max/p10/p90) 보여줘.
```
→ Claude 가 시맨틱 딕셔너리의 `value_stats` 를 추출해 분포 표시.

실측 분포 예시 (한 entry):

<!-- json-fragment-ok: dictionary-entry-snippet -->
```json
"temperature": {
  "range": "xsd:decimal",
  "value_stats": {"count": 8421, "min": 1200.5, "max": 1680.3,
                  "p10": 1280.4, "p50": 1430.1, "p90": 1620.8}
}
```

**증상**: 자연어로 "온도 2000 이상 조회" 요청 → Claude 가 `FILTER(?t >= 2000)` 생성 → 0건.
**원인**: 실제 `temperature.max` 가 1680 이라 2000 이상 데이터 자체가 없음 ("no data" 가 아니라 **"out of range"**).

**해결**: 쿼리 전 min/max/p10/p90 확인:
- 범위 밖이면 쿼리 조건 재검토 (단위 오류? p90 기준으로 완화?)
- 범위 안인데 0건이면 다른 원인 (FK 누락, 조건 과잉 등)

---

## 로컬 SPARQL (`sparql_local`) — 외부 엔드포인트 없이 실행

외부 SPARQL 엔드포인트 연결 없이 로컬 rdflib/Oxigraph 기반으로 SPARQL을 실행할 수 있다.
`data/generated/`의 TTL 파일을 직접 로드하여 쿼리한다.

**Claude Code 자연어 예시** (실제 워크샵 사용 패턴):

```
sparql_local 로 다음 쿼리 실행해줘:
SELECT ?eq WHERE { ?eq a steel:EquipmentMaster } LIMIT 5
```

```
sparql_local 로 다음 쿼리 실행해줘 (source="merge", 추론 전 가벼움):
SELECT ?cls (COUNT(?i) AS ?cnt) WHERE { ?i a ?cls } GROUP BY ?cls ORDER BY DESC(?cnt)
```

> Claude 가 알아서 도구 호출하므로 **자연어 + SPARQL 만 적어도 OK** — 함수 호출 syntax (`sparql_local("...")`) 외울 필요 없음.

**참고: `ensure_inverse_triples`**
`sparql_local`은 로드 시점에 `owl:inverseOf`로 선언된 역방향 트리플을 자동 생성한다.
따라서 A-Box에 `steel:tagEquipment`만 입력해도 `steel:equipmentHasTag` 방향으로 쿼리할 수 있다.
외부 SPARQL 엔드포인트 일부에서는 추론 엔진이 자동 처리하지만, 본 프로젝트는 이 함수가 동일 역할을 한다.

**캐시:**
- 한 번 로드한 그래프는 메모리에 캐시됨
- TTL 파일을 변경한 후에는 `sparql_local_reload()`로 캐시를 리프레시

---

## 크로스 도메인 쿼리 패턴 (실전 활용)

단일 테이블이 아닌 여러 도메인을 JOIN하는 쿼리. KG의 진짜 가치가 여기에 있다.

### 설비 + 알람 + 고장 원인 (3-도메인 JOIN, pre-generated 실제 OP 사용)
```sparql
# 설비별 알람 건수 + 고장 건수 — 고장 잦은 설비 식별
# Alarm 은 Tag 를 통해 설비와 간접 연결 (Tag→isTagOf→EQ + Alarm→hasAlarmTag→Tag),
# FailureCause 는 isFailureRecordOf 로 EQ 에 직접
SELECT ?eqName (COUNT(DISTINCT ?alarm) AS ?alarmCnt) (COUNT(DISTINCT ?failure) AS ?failureCnt)
WHERE {
  ?eq a steel:EquipmentMaster ;
      steel:equipmentMasterName ?eqName .
  OPTIONAL {
    ?tag steel:tagEquipment ?eq .
    ?alarm a steel:AlarmEvents ; steel:hasAlarmTag ?tag .
  }
  OPTIONAL {
    ?failure a steel:FailureCause ;
             steel:failureCauseRefersToEquipment ?eq .
  }
}
GROUP BY ?eqName
ORDER BY DESC(?alarmCnt)
LIMIT 20
```

### 제품 + 화학 분석 (FILTER 비교)
```sparql
# 탄소 함량 상위 — 고탄소강 (high-carbon steel) 식별
# pre-generated 의 실측 chemicalAnalysisCarbonPercent 분포 (semantic_dictionary.json
# 의 value_stats 참조). 일반 탄소강 임계 0.2 이상이 ~10% 정도
SELECT ?steelGrade ?cPercent
WHERE {
  ?chem a steel:ChemicalAnalysis ;
        steel:hasChemicalAnalysisProduct ?product ;
        steel:chemicalAnalysisCarbonPercent ?cPercent .
  ?product a steel:ProductMaster ;
           steel:productMasterSteelGrade ?steelGrade .
  FILTER (?cPercent > 0.2)
}
ORDER BY DESC(?cPercent)
LIMIT 20
```

> **자사 적용 시**: 임계값 0.2 는 철강 샘플의 실측 분포 기준입니다. 본인 데이터의
> 분포는 `semantic_dictionary.json` 의 `chemicalAnalysisCarbonPercent.value_stats`
> 또는 SPARQL 의 `MIN/MAX/AVG` 로 먼저 확인 후 threshold 조정.

### 설비 + 온실가스 + 폐기물 (서브쿼리 집계 후 JOIN, pre-generated 실제 OP 사용)
```sparql
# 설비별 온실가스 배출량 + 폐기물량: 환경 영향 상위 설비
# GHG 는 hasGHGEquipment (120건), Waste 는 hasWasteEquipment (150건) 으로 EQ 와 연결됨
# 배출량 = 활동자료(Activity_Data) x 배출계수(Emission_Factor). 단위는 원천 CSV 정의를 따른다
# ⚠️ 서로 독립인 다건 관계 둘(GHG, Waste)을 같은 ?eq 에 바로 붙여 SUM 하면 행이
# GHG 건수 x Waste 건수로 곱해져, 각 합계가 상대편 건수만큼 부풀어 오른다.
# 그래서 관계마다 서브쿼리에서 설비별로 먼저 집계한 뒤 ?eq 로 조인한다.
# ⚠️ OP 이름은 T-Box 재생성마다 갈린다. OPTIONAL 안의 이름이 틀리면 전체 쿼리는
# 행을 반환하지만 그 블록만 조용히 비어 합계가 공백이 된다. verify 스크립트의
# "≥1행" 검사로는 잡히지 않는다.
SELECT ?eqName ?totalGHG ?totalWaste
WHERE {
  ?eq a steel:EquipmentMaster ;
      steel:equipmentMasterName ?eqName .
  OPTIONAL {
    SELECT ?eq (SUM(?ad * ?ef) AS ?totalGHG)
    WHERE {
      ?ghg a steel:GHGEmission ;
           steel:hasGHGEquipment ?eq ;
           steel:ghgEmissionActivityData ?ad ;
           steel:ghgEmissionFactor ?ef .
    }
    GROUP BY ?eq
  }
  OPTIONAL {
    SELECT ?eq (SUM(?wQty) AS ?totalWaste)
    WHERE {
      ?w a steel:WasteManagement ;
         steel:hasWasteEquipment ?eq ;
         steel:wasteManagementQuantity ?wQty .
    }
    GROUP BY ?eq
  }
}
ORDER BY DESC(?totalGHG)
LIMIT 10
```

> **CSV 로 대조하기**: 결과 첫 행은 `Equipment_16` (EQ016) 이고 `totalGHG` 33437.956806,
> `totalWaste` 146.86 입니다. 두 값 모두 `GHG_Emission.csv` (`Activity_Data` x
> `Emission_Factor` 합계) 와 `Waste_Management.csv` (`Quantity` 합계) 로 직접 계산한 값과 같습니다.
> 원천 CSV 에서 EQ016 은 GHG 6행, Waste 4행이라, 두 OPTIONAL 을 서브쿼리 없이 바로 붙이면
> GHG 합계는 4배, 폐기물 합계는 6배로 부풀어 오릅니다. 10위 밖의 설비를 대조하려면 `LIMIT 10`
> 을 지우거나 바깥 `WHERE` 에 `FILTER(?eqName = "Equipment_1")` 처럼 조건을 넣어 실행합니다.

---

## 암묵지(Tacit Knowledge) 쿼리 패턴

암묵지는 CSV에 없는 현장 지식으로, `data/source/tacit/*.ttl`에 저장된다.
추론 시 T-Box + A-Box와 자동 병합되므로 동일한 SPARQL로 조회 가능.

> **로드 모드 안내**: 아래 암묵지 쿼리들은 `data/source/tacit/*.ttl` 이 로드된
> 환경 (`sparql_local` default `source="inferred"` 또는 `source="merge"`) 에서만
> 결과가 나옵니다. 본 cheatsheet 의 verify 스크립트는 `pre-generated` 만 로드해
> 0 행이 정상이지만, 실제 워크샵 환경에서는 결과가 나옵니다.

### 공정 흐름 체인 (followedBy)

<!-- zero-rows-ok: tacit-not-in-pre-generated -->
```sparql
# 공정 순서 조회 (예: 제조 공정 체인)
SELECT ?from ?to WHERE {
  ?from steel:followedBy ?to .
}
```

<!-- zero-rows-ok: tacit-not-in-pre-generated -->
```sparql
# 클래스 레벨 공정 체인 (data/source/tacit/tacit_process_flow.ttl 의 실제 트리플 패턴)
# tacit 은 인스턴스가 아닌 ProcessBlastFurnace, ProcessSteelmakingFurnace 등 클래스 간 followedBy 를 정의
SELECT ?next WHERE {
  steel:ProcessBlastFurnace steel:followedBy+ ?next .
}
```

### 고장 원인 (실제 데이터: FailureCause)
```sparql
# 설비별 고장 원인 + 다운타임 (pre-generated 실제 데이터, OP 는 isFailureRecordOf)
SELECT ?eqName ?causeCode ?downtimeHours ?occurrenceDateTime WHERE {
  ?fc a steel:FailureCause ;
      steel:failureCauseRefersToEquipment ?eq ;
      steel:failureCauseCauseCode ?causeCode ;
      steel:failureCauseDowntimeHours ?downtimeHours ;
      steel:failureCauseOccurrenceDateTime ?occurrenceDateTime .
  ?eq steel:equipmentMasterName ?eqName .
}
ORDER BY DESC(?downtimeHours)
LIMIT 20
```

> **참고**: `steel:hasFailurePattern` / `steel:hasOperationalRule` 같은 OP 는
> 본 워크샵의 pre-generated 산출물에는 포함되지 않습니다 (도메인 SME 가
> tacit_rules.json 또는 add_tacit_from_natural_language 로 추가하는 항목).
> 실제 KG 에 OP 가 존재하는지 먼저 확인:
> `SELECT ?p (COUNT(*) AS ?c) WHERE { ?s ?p ?o } GROUP BY ?p ORDER BY DESC(?c)`

---

## (부록) SPARQL 엔진 호환성 안티패턴 — 외부 RDF 스토어 배포 시

> 본 워크샵의 `sparql_local` (rdflib/Oxigraph) 사용자에게는 모두 동작 — **외부
> SPARQL 엔드포인트로 배포할 때만** 참조 ([post-workshop §7-1](post-workshop-guide.md#7-1-외부-rdf-스토어-배포-선택)).

| 안 되는 엔진 있음 | 대안 |
|-----------|------|
| `YEAR(?date)` | 날짜 범위 비교 |
| `MONTH(?date)` | 날짜 범위 비교 |
| `HOUR(?ts)` | `STRAFTER(STR(?ts), "T")` 문자열 비교 |
| `NOW() - duration` | 고정 날짜 사용 |
| `ROUND(?x, 2)` | `ROUND(?x)` (인자 1개만) |
| `GROUP_CONCAT` | 미지원 가능 |
