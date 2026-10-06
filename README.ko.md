# Ontology Agent MCP 서버

Ontology Agent는 도메인 중립 지식 그래프 라이프사이클을 위한 **로컬 stdio MCP
서버** 샘플입니다. CSV 프로파일링, T-Box 설계, A-Box 생성, 검증과 품질 게이트,
OWL 추론, SPARQL 질의와 리포트를 하나의 워크플로로 제공합니다.

이 저장소는 하나의 개념과 워크플로를 시연하는 sample code이며, AWS 서비스를
조작하는 범용 유틸리티나 라이브러리가 아닙니다. 동봉된 200분 가이드 워크샵은
데이터 엔지니어, 지식 그래프 엔지니어, 솔루션 아키텍트, 도메인 전문가가 MCP
클라이언트와 Amazon Bedrock을 사용해 이 워크플로를 처음부터 끝까지 수행하는
예시입니다. 서버가 무엇을 하는지 먼저 읽고, 실습은
[동봉 워크샵](#동봉-워크샵-가이드-예시)을 사용하세요.

> [!WARNING]
> 이 코드는 비프로덕션 용도의 샘플입니다. 배포 전 조직의 보안, 법무, 규제,
> 컴플라이언스 요구사항을 별도로 검토하세요.

## 동봉 워크샵 (가이드 예시)

다음 문서를 순서대로 사용해 서버 워크플로를 실습합니다. 강의 슬라이드는 동봉되지
않습니다. 30분 온톨로지 기초 세션의 자료는 참가자 워크북의
[온톨로지 기초 강의](workshop/participant-workbook.md#온톨로지-기초-강의-30분) 절이며,
강사가 이 절로 진행하거나 참가자가 혼자 읽을 수 있습니다.

1. [설치 가이드](workshop/setup-guide.md)
2. [참가자 워크북](workshop/participant-workbook.md)
3. [SPARQL 치트시트](workshop/sparql-cheatsheet.md)
4. [워크샵 이후 가이드](workshop/post-workshop-guide.md)
5. [강사 가이드](workshop/instructor-guide.md)

| 시간 | 활동 |
|---:|---|
| 10분 | 환경 점검 |
| 30분 | 온톨로지 기초 |
| 55분 | 데이터와 T-Box 실습 |
| 45분 | A-Box, 추론, 검증 |
| 40분 | SPARQL과 CQ |
| 20분 | Capstone |

참가자는 `check_pipeline_state`, `generate_competency_questions`,
`list_csv_tables`, `read_csv_schema`, `generate_csv_erd`,
`profile_csv_data`, `generate_tbox_collaborative`, `read_tbox`,
`analyze_tbox`, `improve_tbox_quality`, `measure_tbox_metrics`,
`update_tbox_incremental`, `validate_ttl_syntax`, `check_quality_rules`,
`validate_owl_consistency`, `classify_tbox`, `validate_tbox_shacl`,
`add_tacit_from_natural_language`, `generate_tacit_from_rules`,
`generate_tacit_from_data`, `skip_tacit_knowledge`, `visualize_tbox`,
`generate_abox`, `validate_kg`, `sparql_local`, `test_domain_queries`,
`reset_pipeline_state`의 27개 도구를 직접 사용합니다. 나머지 도구는 파이프라인
내부, 선택 통합, 진단, 워크샵 이후 심화 작업용입니다.

```bash
python scripts/verify_workshop_sparql.py --ignore-placeholders
```

## 비용 고려사항

프로파일링, 변환, 검증, 추론, 질의 작업은 대부분 로컬에서 실행됩니다. Amazon
Bedrock 모델 요금은 모델을 사용하는 도구, 특히 competency question 생성과 협업
T-Box 설계에서 발생합니다. 파이프라인이 A-Box 생성 전과 추론 후에 실행하는
`generate_semantic_dictionary`도 `description_ko`만 있고 `description_en`이 없는
클래스가 있으면 Bedrock을 호출합니다. 이 호출은 해당 클래스 이름과 설명을 번역
요청으로 모델에 보냅니다. 동봉 pre-generated 패키지는 질의·검증 실습에서 협업 T-Box
단계를 건너뛰게 해 줄 뿐, 모든 모델 호출을 없애지는 않습니다. 동봉 T-Box 는 대부분의
클래스에 한국어 설명만 있어서, 워크북 6-2a 처럼 이 T-Box 로 시맨틱 딕셔너리를 다시
생성하면 해당 클래스가 번역 요청으로 Bedrock 에 전송됩니다. CQ 생성, 자연어 암묵지
추가, T-Box 증분 수정 실습 단계도 Bedrock 을 호출합니다. 선택 원격 저장소는 별도
인프라 요금이 발생할 수 있습니다.

## 빠른 시작

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

`.mcp.json.example`을 `.mcp.json`으로 복사하고 절대경로를 설정한 뒤 MCP
클라이언트를 재시작합니다.

## 보안

합성 또는 명시적으로 승인된 데이터만 사용하고, 자격 증명과 생성 산출물을 Git에
넣지 않습니다. 모델이 생성한 RDF, SPARQL, Cypher는 검증 전까지 신뢰하지 않습니다.
자세한 내용은 [위협 모델](docs/security/threat-model.md)을 참조하세요.

## 프로젝트 상태 및 수명 종료

이 저장소는 MCP 서버 샘플이 운영되고 서버 워크플로와 동봉 워크샵 예시를 지원되는
의존성에서 검증할 수 있는 동안 유지합니다. 샘플이 종료되거나 시연 워크플로가 후속
샘플로 이전되거나 필수 의존성을 안전하게 유지할 수 없게 되면 수명 종료를
검토합니다. 이후 최종 상태 공지를 게시한 뒤 저장소를 archive할 수 있습니다. 이는
조건 선언이며 특정 종료 날짜의 약속이 아닙니다.

이 저장소에는 테스트가 있지만 hosted CI workflow는 게시하지 않습니다. 릴리스 전
검증 명령은 관리자가 명시적으로 실행합니다.

## 정리

```bash
rm -r data/generated
deactivate
```

원격 저장소나 데이터베이스를 삭제할 때는 정확한 대상을 먼저 확인하세요.

## 문제 해결

MCP 도구가 보이지 않으면 `.mcp.json`의 절대경로를 확인하고 클라이언트를 완전히
재시작합니다. Bedrock 권한 오류는 Region, model access, IAM 권한을 확인합니다.
문서 인덱스는 [docs/README.ko.md](docs/README.ko.md)를 참조하세요.

## 라이선스

[MIT-0](LICENSE)으로 배포됩니다. 제3자 고지는
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)를 참조하세요.
