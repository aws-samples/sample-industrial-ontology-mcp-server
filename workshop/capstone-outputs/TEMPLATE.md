# Capstone 산출물 — <본인 이름> / <YYYY-MM-DD>

> **목적**: 3시간 워크샵의 visible artifact. D+1 매니저 1:1 의 첨부 자료 + 강사 회수 (enablement 객관 산출물).
> **작성 시점**: Ch6-2c Capstone 의 3분 슬롯에 본인이 직접 작성 → 2분 슬롯에 강사 채널 회수.
> **저장 위치**: `workshop/capstone-outputs/<본인이름>_<YYYY-MM-DD>.md`
>
> **필수 3개 (1, 2, 5번)** 만 채워도 D+1 출발 가능. **선택 4개 (3, 4, 6, 7번)** 는 시간 남으면 / D+1 30분 슬롯에.
>
> 이 파일을 본인이 채우고 (3분 작업), 강사가 사전 안내한 채널 (Slack DM / 공유 드라이브 / 이메일 중 하나) 로 첨부 발송.
> 본인 보관용 HTML 1페이지가 필요하면 `python scripts/render_capstone.py --name <이름> --date <날짜>`.

---

## 1. 본인 도메인명 + prefix [**필수**]

| 항목 | 값 |
|------|-----|
| 도메인명 (한글) | _____________________ (예: "내 부서 제조") |
| 도메인명 (영문) | _____________________ (예: "My Department Manufacturing") |
| **prefix** (영문 소문자, 3~6자) | _________ (예: `pharma`, `fin`, `mydept`) |
| instance prefix | __________________ (보통 prefix + `-inst`, 예: `pharma-inst`. T-Box 의 클래스 vs A-Box 의 인스턴스 네임스페이스 분리용 — 같은 도메인 내에서 스키마와 데이터를 구별) |

## 2. 표 A 의 자사 CQ 3~5개 (Ch2-2 작성) [**필수**]

워크샵 Ch2-2 의 표 A 에서 작성한 본인 도메인용 자연어 질문 3~5개를 그대로 복사 (3개라도 OK):

| ID | 자연어 질문 | 분류 | 우선순위 |
|:--:|----------|:--:|:--:|
| OWN-1 | | | |
| OWN-2 | | | |
| OWN-3 | | | |
| OWN-4 | | | |
| OWN-5 | | | |

## 3. FK 패턴 1줄 (Ch6-2 + post-workshop 숙제) [선택]

본인 CSV 의 FK 컬럼명 → 타깃 클래스 매핑:

```json
{
  "patterns": {
    "<정규화된 컬럼명>": "<타깃 클래스명>",
    "예: customerid": "예: Customer"
  }
}
```

## 4. 별도 도메인 폴더 경로 (post-workshop §"D+1 첫 숙제") [선택]

```bash
mkdir -p rules/my-domain
cp rules/domain/domain_config.json rules/my-domain/domain_config.json
export DOMAIN_CONFIG_PATH=rules/my-domain/domain_config.json
# fk_patterns.json 은 격리 대상이 아님 — 메인 rules/contracts/fk_patterns.json 직접 편집·원복
```

본인이 적용할 폴더 이름: `rules/_____________/`

## 5. D+30 까지 도달하고 싶은 상태 (1줄) [**필수**]

```
예시 1: "validate_kg 18/22 + CQ 응답률 60% + SME 1명 합류"
예시 2: "PoC 결과 임원 보고 완료, GO/NO-GO 결정"
예시 3: "본인 도메인이 KG 와 안 맞다는 결론도 OK — D+30 자문 회신 받기"

본인 목표: ____________________________________________
```

## 6. 본인이 만든 미니 산출물 [선택]

페르소나별 옵션 1개 선택, 본인이 처음부터 끝까지 만든 결과물 (페르소나 정의는 workbook 상단 표):

- **옵션 A (페르소나 A 데이터 엔지니어 / C 데이터 사이언티스트 / D 솔루션 아키텍트)**: 표 B (철강) CQ 1개 손수 SPARQL 변환 → 결과 첨부
- **옵션 B (페르소나 B 도메인 SME 권장)**: tacit TTL 1줄 자연어 입력 → 본인 KG 반영 확인

```
(여기에 본인 산출물 붙여넣기)
```

## 7. 자사 CQ dry-run 변환 [선택]

표 A 의 OWN-1 자연어 → Claude 가 변환한 가상 SPARQL (실행 X, prefix `myind:` 가정):

```sparql
(Claude 변환 결과 붙여넣기)
```

---

**작성 완료 후 다음 단계** — D+1 매니저 1:1 에 가져가세요. 3시간이 종이 1장으로 박제됩니다.
