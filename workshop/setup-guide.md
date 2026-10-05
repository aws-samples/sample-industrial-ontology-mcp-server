# 워크샵 사전 준비 가이드

## 이 문서는 무엇인가

워크샵 당일은 "AI 가 CSV 로 온톨로지를 만드는 과정을 직접 보고 쿼리하는" 실습입니다.
**이 가이드는 그 실습을 시작할 수 있도록 사전에 PC 에 도구 4가지를 설치하는 안내서** 입니다.

| 설치 항목 | 무엇에 쓰나 | 필수/권장 | 예상 시간 |
|-----------|-------------|:--:|:--------:|
| Amazon Bedrock 접근 | T-Box 를 만들 AI(Claude Sonnet) 호출 | 필수¹ | **개인 1\~2h, 회사 1\~3일** ← 가장 먼저 |
| Python 3.11+ | 서버 런타임 | 필수 | 10분 |
| Java 25+ (HermiT 단독은 11+, Pellet 은 25+) | OWL 추론기(HermiT/Pellet) | 권장² | 10분 |
| Claude Code | MCP 클라이언트 | 필수 | 5분 |

¹ Bedrock 미승인자는 §5.4 fallback 산출물로 SPARQL/시각화/검증 슬롯 본인 PC 진행 가능. 단 D-1 사전 적재 필수.
² Java 없으면 5단계 검증 중 2개 (`validate_owl_consistency`, `classify_tbox`) 스킵. Bedrock fallback 사용자는 강사 화면으로 충분하므로 생략 가능. Java 11~24 는 HermiT 만 동작하고 Pellet 경로는 실패하므로 새로 설치한다면 25 이상을 설치하세요 (§1.3).

## 하드웨어 사전 점검

| 항목 | 권장 | 최소 | 미달 시 |
|------|------|------|--------|
| **RAM** | 16GB+ | 8GB | 8GB Mac 은 Ch4 OOM 위험 — `RDFLIB_STORE=oxigraph` (기본) 유지 + 다른 앱 종료. 그래도 OOM 이면 §5.4 의 pre-generated fallback 산출물로 우회 |
| **CPU** | 8-core+ | 4-core | Bedrock 호출은 네트워크 바운드 — CPU 영향 적음 |
| **디스크 여유** | 10GB+ | 5GB | 추론 결과 (all_inferred.ttl) \~150MB + 프로젝트 클론 \~500MB |
| **OS** | macOS / Ubuntu / Windows 11 | (Windows 10도 가능) | 모든 OS 지원, 단 `.env` `JAVA_EXE` 경로 분기 (§3.3) |

## Prerequisite — 본 워크샵이 가정하는 사전 지식

워크샵 3시간 안에 다음을 처음부터 가르치지 않습니다. 미달자에게 사전 학습 자료 안내.

| 항목 | 본 워크샵 사용처 | 미달 시 사전 학습 |
|------|---------------|----------------|
| **SQL JOIN / GROUP BY 작성** | Ch1-2 4-table JOIN 비교, Ch5 SPARQL ↔ SQL 매핑 | https://www.w3schools.com/sql/sql_join.asp (~30분). **페르소나 B (도메인 SME) 는 면제** — Ch5 의 SPARQL 5단어 표만 외워도 진행 가능. 5-2/5-4a 슬롯에서 페르소나 A/C/D 와 페어링 |
| **Python venv 사용** | setup-guide §3 가상환경 활성화 | `python3 -m venv venv` + `source venv/bin/activate` (~10분 학습) |
| **JSON 편집** | post-workshop "D+1 첫 숙제" `domain_config.json` / `fk_patterns.json` | 텍스트 에디터로 JSON 편집 (~10분) |
| **CLI 기본** | setup-guide 모든 단계 | `cd`, `ls`, `cat`, `cp`, `mkdir` 명령 (~30분) |
| **OWL/RDF** | (없어도 됨) | Ch1-2 에서 5단어로 처음 배움 |

**먼저 읽기 — 이 파일이 가정하는 것:**
- 위 prerequisite 4항목 충족 (SQL JOIN / Python venv / JSON 편집 / CLI 기본)
- 회사에서 AWS 계정 사용 권한을 가지고 있거나 받을 예정
- 외부 인터넷 접근 가능 (Bedrock, pip, Docker Hub, vis.js CDN)

---

## 0. Amazon Bedrock 모델 접근 (가장 오래 걸림, 먼저 하세요)

T-Box 생성 단계(S2)는 Bedrock Claude Sonnet 을 ~10회 호출합니다. 이 단계 없이는
워크샵이 의미가 없으므로, **가장 먼저** 다음을 확인하세요.

### 0.1 AWS 계정 + IAM 권한

회사 IT 담당자/AWS admin 에게 다음을 요청:
- IAM 사용자 또는 SSO 프로파일 (본인 이름으로)
- 정책: `bedrock:InvokeModel` (최소) 또는 `AmazonBedrockFullAccess` (편의)
- 리전 접근: **us-east-1** (Claude Sonnet 이 여기만 있는 경우가 많음)

### 0.2 Bedrock 모델 활성화 (승인 필요)

https://console.aws.amazon.com/bedrock/home?region=us-east-1#/modelaccess

1. "Request model access" 클릭
2. **Anthropic Claude Sonnet 4.5** 체크 + 용도 기재 (예: "Internal training workshop")
3. **승인 대기 시간**:
   - **개인 AWS 계정**: 1~2시간
   - **회사 AWS 계정**: **1~3일 영업일** (회사 정책/IT 승인 라우팅에 따라 달라짐)
4. 상태가 **"Access granted"** 가 되면 완료

> **권장**: 회사 계정 사용자는 **워크샵 1주 전 신청** 필수. "1~2시간" 가이드는
> 개인 계정 케이스이며, 회사 계정에서 3일 전 신청은 위험.

### 0.3 접근 확인

```bash
# AWS CLI 로 Bedrock 모델 목록 조회 — Claude Sonnet 이 보여야 정상
aws bedrock list-foundation-models --region us-east-1 \
  --query 'modelSummaries[?contains(modelId, `claude-sonnet`)].modelId' \
  --output table
```

**기대 결과:** `anthropic.claude-sonnet-4-6...` / `us.anthropic.claude-sonnet-4-6` 같은 항목이 나옴
**실패 시:** 0.2 의 승인이 아직 안 났거나, 리전이 맞지 않음

---

## 1. 필수 소프트웨어 설치

### 1.1 설치 확인 (먼저 실행)

```bash
# macOS/Linux
python3 --version   # 3.11+
java -version       # 25+ (HermiT 단독은 11+, Pellet 은 25+)
git --version
aws --version       # v2
claude --version    # Claude Code CLI
```

```powershell
# Windows (PowerShell) — python3 가 아니라 py 런처 또는 python 사용
py --version        # 3.11+  (또는: python --version)
java -version
git --version
aws --version
claude --version
```

모두 버전이 나오면 2단계로. 하나라도 "command not found" / "term ... is not
recognized" 면 1.2~1.6 참조.

> **Windows 사용자 주의 — 설정 파일 편집기**: 이후 `.env` / `.mcp.json` 을 편집할 때
> **메모장(notepad.exe)을 쓰지 마세요.** 메모장은 줄바꿈을 CRLF 로, 때로 BOM 을
> 추가해 경로·키 값이 깨질 수 있습니다. **VS Code, Notepad++** 등 개발용 에디터를
> 쓰고, 가능하면 줄바꿈을 LF 로 저장하세요.

### 1.2 Python 3.11+

| OS | 설치 |
|----|------|
| macOS | `brew install python@3.11` |
| Ubuntu/Debian | `sudo apt install python3.11 python3.11-venv` |
| Windows | https://www.python.org/downloads/ → 설치 시 "Add to PATH" 체크 |

### 1.3 Java 25+ (HermiT 단독은 11+, Pellet 은 25+): **권장 (Bedrock 사용자) / 생략 가능 (fallback 사용자)**

S4 단계에서 HermiT 추론기로 OWL 논리 일관성을 검증합니다. Java 없으면 5 validator
중 2개 (`validate_owl_consistency`, `classify_tbox`) 가 빠집니다.

> **버전 요구가 추론기마다 다릅니다**: HermiT 은 Java 11+ 로 동작하지만 Pellet 경로
> (S8.5 SWRL `run_swrl_inference`, `validate_owl_realisation`) 는 **Java 25 미만에서
> `UnsupportedClassVersionError` 로 실패**합니다. owlready2 배포본의 Jena jar 에 Java 25
> 바이트코드가 들어 있기 때문입니다. 그래서 S4 (HermiT) 통과를 Pellet 정상으로 읽으면
> 안 됩니다. 새로 설치한다면 아래 표처럼 **버전 25 를 지정해** 설치하세요. 상세 원인은
> `docs/reference/troubleshooting.md` 의 "OWL 추론기 (HermiT / Pellet)" 절을 참고하세요.

> **워크샵 본 챕터에서는 Java 직접 호출 슬롯이 적습니다** — Ch3-1 5단계 검증은
> 강사 D-1 결과 분석, Ch4 추론은 OWL RL (Java 무관). Bedrock fallback 사용자는
> 강사 화면으로 충분하므로 §1.3 을 생략하고 §1.4 로 넘어가도 됩니다. 단 자사
> PoC (post-workshop) 단계에서는 Java 가 있어야 5단계 검증 풀세트 가능.

**HermiT 가 무엇이고 왜 필요한가**:

HermiT 는 옥스포드 대학에서 개발한 **OWL 2 DL** 호환 추론기 (Java 구현).
T-Box 가 "논리적으로 일관된가" 를 검증:
- **Unsatisfiable class 탐지**: 예를 들어 "Car is-a Vehicle" + "Car is-a Animal"
  + "Vehicle disjointWith Animal" 선언 시 Car 는 **어떤 인스턴스도 가질 수 없는**
  빈 클래스임을 감지. 운영 전 T-Box 설계 오류 차단의 마지막 관문
- **Classification (계층 추론)**: 명시적 subClassOf 외에 **암시적으로 유도되는**
  subClassOf 관계를 찾음 (예: unionOf 로 정의된 클래스가 다른 클래스의 superclass
  임을 자동 도출). S4 classify_tbox 가 이 결과를 반환
- **이 프로젝트에서 불가능한 것**: 순수 Python rdflib 는 OWL RL (규칙 기반)
  추론만 가능. DL 수준의 검증은 HermiT/Pellet 같은 Java 기반 reasoner 필수

**Java 대체 불가** — owlready2 (Python) 가 내부적으로 Java 프로세스를 띄워 HermiT 를
호출하는 구조. 따라서 Python 환경에서 쓰더라도 JRE/JDK 설치는 필수.

| OS | 설치 명령 (버전 25 지정) | `JAVA_EXE` 경로 예시 |
|----|----------|---------------------|
| macOS (Homebrew) | `brew install --cask corretto@25` | `/Library/Java/JavaVirtualMachines/amazon-corretto-25.jdk/Contents/Home/bin/java` |
| Ubuntu/Debian | 아래 "Ubuntu/Debian 에 Corretto 25 설치" 명령 (`java-25-amazon-corretto-jdk` 패키지) | `readlink -f "$(which java)"` 결과 사용 |
| Windows (Chocolatey) | `choco install corretto25jdk` | `C:\Program Files\Amazon Corretto\jdk25.0.0_xx\bin\java.exe`, 또는 `where.exe java` 결과 사용 |
| Linux (tarball) | adoptium.net 에서 **25 이상** 버전을 골라 다운로드 | 압축 풀린 디렉토리의 `bin/java` |

> Ubuntu 기본 저장소의 `default-jdk` 는 배포판에 따라 Java 25 미만을 설치하므로 쓰지 마세요.

**Ubuntu/Debian 에 Corretto 25 설치** (Amazon Corretto 25 사용자 가이드의 apt 절차,
https://docs.aws.amazon.com/corretto/latest/corretto-25-ug/generic-linux-install.html):
```bash
# Corretto apt 저장소의 서명 키와 저장소를 등록
wget -O - https://apt.corretto.aws/corretto.key | sudo gpg --dearmor -o /usr/share/keyrings/corretto-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/corretto-keyring.gpg] https://apt.corretto.aws stable main" | sudo tee /etc/apt/sources.list.d/corretto.list

# 버전 25 패키지를 이름으로 지정해 설치
sudo apt-get update
sudo apt-get install -y java-25-amazon-corretto-jdk

# "25." 로 시작하는 버전이 보여야 정상. 다른 버전이 나오면 기본 java 를 바꾼다
java -version
# sudo update-alternatives --config java
```

**본인 경로 확인:**
```bash
# macOS/Linux
/usr/libexec/java_home -v 25  # macOS: Java 25 home 출력
which java                    # 모든 Unix: java 실행 파일 경로
readlink -f "$(which java)"   # Linux: 심볼릭 링크를 풀어 실제 경로 출력

# Windows (PowerShell)
where.exe java
```

### 1.4 AWS CLI v2

| OS | 설치 |
|----|------|
| macOS | `brew install awscli` |
| Linux | https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2-linux.html |
| Windows | `choco install awscli` 또는 [msi installer](https://awscli.amazonaws.com/AWSCLIV2.msi) |

### 1.5 Claude Code CLI

**공식 다운로드:** https://claude.ai/download (Mac/Windows 데스크톱 앱) 또는
https://docs.anthropic.com/en/docs/claude-code (CLI 설치 가이드)

설치 후 `claude --version` 이 동작해야 합니다.

### 1.6 (선택) Docker — Neo4j 자체 학습 항목

| OS | 설치 |
|----|------|
| macOS/Windows | https://www.docker.com/products/docker-desktop |
| Ubuntu/Debian | `sudo apt install docker.io` + 재로그인 |

> Neo4j LPG 변환은 **워크샵 본 챕터에서 다루지 않습니다** —
> post-workshop-guide §7-2 "Neo4j LPG 배포 (RCA 분석용)" 의 자체 학습 항목.
> Docker 미설치자는 워크샵 진행에 지장 없음. Ch6-1 은 도메인 질의 테스트
> (`test_domain_queries`) 챕터이며 Neo4j 와 무관.

---

## 2. AWS 자격증명 설정

```bash
aws configure --profile ontology-workshop
# AWS Access Key ID: <본인 IAM>
# AWS Secret Access Key: <본인 IAM>
# Region: us-east-1          ← Bedrock 리전과 맞춤
# Output: json

# 연결 확인
aws sts get-caller-identity --profile ontology-workshop
```

**기대 결과:**
```json
{
    "UserId": "AIDA...",
    "Account": "<AWS_ACCOUNT_ID>",
    "Arn": "arn:aws:iam::...:user/your-name"
}
```

**실패 시:** 0단계의 Bedrock 승인과 별개 — AWS 자격증명 자체의 문제. 회사 IT/admin 문의.

---

## 3. ontology-agent 설치

### 3.1 클론 + 가상환경

<!-- placeholder-ok: repo-url-from-instructor -->
```bash
# 프로젝트 클론
git clone <강사가 안내하는 repo URL>
cd ontology-agent

# 가상환경 생성 + 활성화
# macOS/Linux
python3.11 -m venv venv
source venv/bin/activate

# Windows (PowerShell) — python3.11 대신 py 런처 사용
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1

# 프롬프트가 (venv) 로 시작하면 활성화 성공

# 의존성 설치 (2~5분)
pip install -r requirements.txt
```

### 3.2 절대 경로 구하기

MCP 등록 단계 (5절) 에서 이 프로젝트의 **절대 경로** 가 필요합니다.

```bash
# macOS/Linux
pwd
# 출력 예: /Users/yourname/projects/ontology-agent

# Windows (PowerShell)
echo $PWD
# 출력 예: C:\Users\yourname\projects\ontology-agent
```

이 값을 어딘가 메모해 두세요.

### 3.3 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일을 편집 — **본인 환경 기준으로** 다음 값 채우기:

```bash
# AWS — 2단계에서 만든 프로파일 이름 사용
AWS_PROFILE=ontology-workshop
AWS_REGION=us-east-1

# Bedrock — 0단계에서 승인받은 모델 ID
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
BEDROCK_REGION=us-east-1

# Java — 1.3 의 표에서 본인 OS 경로
# 본인 OS 에 맞는 줄만 활성화하고 나머지는 # 주석 처리한 상태로 둘 것
# macOS 예시:
JAVA_EXE=/Library/Java/JavaVirtualMachines/amazon-corretto-25.jdk/Contents/Home/bin/java
# Linux 예시 (Corretto 25 가 기본 java 일 때. 실제 경로는 readlink -f "$(which java)" 로 확인):
# JAVA_EXE=/usr/bin/java
# Windows 예시 (백슬래시 그대로, 따옴표로 감싸지 말 것 — 아래 주의):
# JAVA_EXE=C:\Program Files\Amazon Corretto\jdk25.0.0_17\bin\java.exe
# 자동 감지: `which java` (macOS/Linux) 또는 `where.exe java` (Windows) 의 출력값을 위 JAVA_EXE= 에 그대로 붙여넣기
# ⚠ Windows 주의: 경로를 큰따옴표로 감싸면 \b \j 등이 escape 로 해석돼 깨집니다
#   (예: \bin → 백스페이스). 따옴표 없이 백슬래시 단일로 그대로 두세요.

# (선택) Neo4j 등은 강사 안내에 따라 추가

# (선택) 신규 기능 플래그 — 기본 비활성, 필요 시 주석 해제
# SWRL_ENABLED=true           # S8.5 SWRL 규칙 추론 (opt-in, Pellet)
# UNIT_MISMATCH_RATIO=50      # 시맨틱 딕셔너리 단위 불일치 경고 임계값 (0=비활성)
# I2_MAX_REIFY=1000           # 추론 triple 정당화(reification) 상한 (규칙별 reify 개수)
```

**검증:** `.env` 의 JAVA_EXE 경로가 실제로 동작하는지 확인. (JAVA_EXE 는 셸
환경변수가 아니라 .env 파일 값이므로, .env 에서 값을 읽어와 실행해야 함.)

```bash
# macOS/Linux
JAVA_EXE=$(grep -E '^JAVA_EXE=' .env | tail -1 | cut -d= -f2-)
"$JAVA_EXE" -version 2>&1 | head -1
```

```powershell
# Windows (PowerShell)
$javaExe = (Select-String '^JAVA_EXE=' .env | Select-Object -Last 1).Line -replace '^JAVA_EXE=',''
& $javaExe -version 2>&1 | Select-Object -First 1
```

> 기대: `openjdk version "25..."` 처럼 25 이상 버전 문자열이 출력되면 정상.
> `"21..."` 같은 11~24 버전이면 HermiT (S4) 은 동작하지만 Pellet 경로는 실패하므로
> §1.3 의 버전 25 설치 명령으로 바꾸세요.
> Java 미설치/경로 오류면 "command not found" / "No such file" / PowerShell 의
> "term ... is not recognized" — §1.3 참조.

---

## 4. 로컬 데이터 확인

원본 데이터는 `data/source/rawdata/` 에 40개 CSV 로 포함되어 있습니다 (git 추적).

```bash
# macOS/Linux
ls data/source/rawdata/*.csv | wc -l
```

```powershell
# Windows (PowerShell)
(Get-ChildItem data\source\rawdata\*.csv).Count
```

**기대 결과:** `40`

`0` 이면 repo 가 제대로 받아지지 않은 상태입니다 (CSV 는 일반 파일로 git 추적됨 —
LFS 미사용). `pwd` 로 `ontology-agent` 루트에 있는지 확인하고 `git pull` 로 최신
상태를 받으세요. 그래도 0 이면 sparse/partial checkout 가능성 — 강사 문의.

---

## 5. Claude Code 에 MCP 서버 등록

### 5.1 방법 A — 프로젝트별 설정 (권장)

**`.mcp.json` 은 `.gitignore` 에 의해 git 추적 제외**되어 (개인 환경별 절대 경로
포함) 처음엔 존재하지 않습니다. repo 에 포함된 `.mcp.json.example` 을 복사 후
편집하세요:

```bash
# macOS/Linux
cp .mcp.json.example .mcp.json

# Windows (PowerShell)
Copy-Item .mcp.json.example .mcp.json
```

`.mcp.json` 의 `/ABSOLUTE/PATH/TO/ontology-agent` 3곳을 **3.2절 `pwd` 결과** 로
일괄 치환:

```json
{
  "mcpServers": {
    "ontology-agent": {
      "command": "<3.2절 pwd 결과>/venv/bin/python",
      "args": ["<3.2절 pwd 결과>/server.py"],
      "env": {
        "DOTENV_PATH": "<3.2절 pwd 결과>/.env"
      }
    }
  }
}
```

**Windows 는 `venv\Scripts\python.exe` + 백슬래시 주의:**
```json
{
  "mcpServers": {
    "ontology-agent": {
      "command": "C:\\Users\\yourname\\projects\\ontology-agent\\venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\yourname\\projects\\ontology-agent\\server.py"],
      "env": {
        "DOTENV_PATH": "C:\\Users\\yourname\\projects\\ontology-agent\\.env"
      }
    }
  }
}
```

### 5.2 Claude Code 재시작

- Claude Code 데스크톱 앱: 완전 종료 (Quit) 후 재시작
- CLI: 새 셸 세션에서 `claude`

### 5.3 MCP 등록 troubleshooting

다음 테스트가 실패하면, 아래 순서대로 점검:

```
"CSV 파일 목록 보여줘"
```

**기대:** `list_csv_tables` 도구 호출 + 40개 CSV 반환

**도구가 아예 안 보이는 경우:**

1. **경로 확인:** `.mcp.json` 의 command/args 경로가 실제로 존재하는지:
   <!-- placeholder-ok: absolute-path-fillin -->
   ```bash
   # macOS/Linux
   ls -la <절대경로>/venv/bin/python
   ls -la <절대경로>/server.py

   # Windows
   Test-Path <절대경로>\venv\Scripts\python.exe
   ```

2. **수동 기동 테스트:** venv 활성화 후 server.py 가 직접 실행되는지:
   <!-- placeholder-ok: absolute-path-fillin -->
   ```bash
   cd <절대경로>
   source venv/bin/activate    # 또는 Windows: .\venv\Scripts\Activate.ps1
   python server.py
   # 에러 메시지가 나오면 그것이 MCP 등록 실패 원인
   # Ctrl+C 로 종료
   ```

3. **로그 확인:** Claude Code 의 MCP 로그 위치 (플랫폼마다 다름) — 강사 문의

### 5.4 Fallback 산출물 사용법 (Bedrock 미승인자 / 8GB RAM 사용자)

다음 두 케이스 중 하나라도 해당되면 본인 PC 에서 T-Box/A-Box 생성을 직접 실행할 수 없습니다:

- **Bedrock 모델 접근이 워크샵 당일까지 미승인** — Claude Sonnet 호출 불가
- **RAM 8GB Mac** — A-Box 로드 + 추론 시 6GB+ 필요해 OOM 위험 (하드웨어 사전 점검 §RAM 행 참조)

이 경우 강사가 미리 만들어둔 산출물 (`workshop/pre-generated/`) 을 본인 환경
`data/generated/` 에 복사해 SPARQL/시각화/검증 실습은 정상 진행 가능합니다.

> **⚠️ D-1 까지 사전 적재 필수**: 워크샵 당일 Ch1-1 슬롯 (10m) 안에 fallback 적재 +
> verify 스크립트까지 끝내는 것은 사내망/처음 만난 환경에서 거의 불가능합니다.
> **D-1 까지** 본인 PC 에서 아래 명령을 실행해 `verify` exit 0 결과를 강사에게
> 회신하세요 (Slack DM 또는 이메일). D-1 회신 안 한 미승인자는 워크샵 첫 인상이
> 심하게 망가집니다.

**fallback 산출물 적재 (D-1 까지, 5~10분):**

<!-- placeholder-ok: absolute-path-fillin -->
```bash
# === macOS / Linux ===
# repo 가 최신 상태인지 먼저 확인 — 강사가 워크샵 직전에 새 산출물 push 했을 수 있음
cd <ontology-agent 절대경로>
git pull

# pre-generated 산출물을 정상 경로로 복사
mkdir -p data/generated/tbox data/generated/abox data/generated/inferred data/generated/reports
cp workshop/pre-generated/t_box.ttl                   data/generated/tbox/
cp workshop/pre-generated/semantic_dictionary.json    data/generated/
cp workshop/pre-generated/tbox_visualization.html     data/generated/reports/
cp workshop/pre-generated/query_test_report.html      data/generated/reports/

# a_box.ttl 과 all_inferred.ttl 은 용량이 커서 .gz 로만 commit 됨. 풀어서 정상 경로에 둔다
gunzip -kc workshop/pre-generated/a_box.ttl.gz        > data/generated/abox/a_box.ttl
gunzip -kc workshop/pre-generated/all_inferred.ttl.gz > data/generated/inferred/all_inferred.ttl
```

```powershell
# === Windows (PowerShell) ===
# ⚠ Windows 에는 gunzip/cp/mkdir -p 가 없으므로 PowerShell + Python 으로 진행.
cd <ontology-agent 절대경로>
git pull

# 디렉토리 생성 (-Force 는 이미 있어도 에러 안 남)
New-Item -ItemType Directory -Force -Path data\generated\tbox, data\generated\abox, data\generated\inferred, data\generated\reports | Out-Null

# 산출물 복사
Copy-Item workshop\pre-generated\t_box.ttl                data\generated\tbox\
Copy-Item workshop\pre-generated\semantic_dictionary.json data\generated\
Copy-Item workshop\pre-generated\tbox_visualization.html data\generated\reports\
Copy-Item workshop\pre-generated\query_test_report.html  data\generated\reports\

# a_box.ttl.gz / all_inferred.ttl.gz 압축 해제. gunzip 대신 Python 사용(이미 설치됨, 크로스플랫폼)
python -c "import gzip, shutil; f=open('data/generated/abox/a_box.ttl','wb'); shutil.copyfileobj(gzip.open('workshop/pre-generated/a_box.ttl.gz','rb'), f); f.close()"
python -c "import gzip, shutil; f=open('data/generated/inferred/all_inferred.ttl','wb'); shutil.copyfileobj(gzip.open('workshop/pre-generated/all_inferred.ttl.gz','rb'), f); f.close()"
```

> **gzip 해제는 Python 방식이 가장 안전**합니다 (gunzip/7-Zip 설치 불필요).
> macOS/Linux 사용자도 위 `python -c ...` 두 줄로 통일해 쓸 수 있습니다.

**검증:**

```bash
# macOS/Linux
ls -la data/generated/tbox/t_box.ttl data/generated/abox/a_box.ttl \
       data/generated/inferred/all_inferred.ttl data/generated/semantic_dictionary.json
```

```powershell
# Windows (PowerShell)
Get-ChildItem data\generated\tbox\t_box.ttl, data\generated\abox\a_box.ttl, `
  data\generated\inferred\all_inferred.ttl, data\generated\semantic_dictionary.json |
  Select-Object Name, Length
```

→ 4개 파일 모두 존재 + size > 0 이면 정상. 추가로 워크북 코드블록 검증
(`python` 명령이라 OS 무관 — Windows 도 그대로):

```bash
python scripts/verify_workshop_sparql.py --ignore-placeholders
```

→ exit 0 이면 모든 SPARQL/JSON/TTL/bash 코드블록이 본인 환경에서 실행 가능.
(Windows 에 bash 가 없으면 bash 블록은 "skipped" 로 표시되며 PASS 처리 —
구문 검증만 생략될 뿐 적재 자체엔 영향 없음.)
FAIL 결과 해석:
- `placeholder` → 강사가 채우지 않은 자리. 강사에게 알림.
- `zero_rows` (zero-rows-ok 주석 없음) → 본인 환경의 OP 이름이 워크북과 어긋남. `read_semantic_dictionary` 로 확인.
- `parse_error` → JSON syntax 오류. verify 스크립트가 자동 감지.

> **워크샵 당일 강사 안내 시점**: **Ch1-1 10분 슬롯** (workbook timeline 참조) 에
> 강사가 D-1 회신 명단 대비 **누락자 점검**. D-1 사전 적재한 사용자는 `list_csv_tables`
> 즉시 실행 가능하며, 사전 적재 안 한 사용자는 강사 1:1 (5분 안에 안 끝나면 강사
> 노트북 화면 공유로 fallback). Ch1-2 시작 시점에 **모두 본인 PC 에서 SPARQL 가능**
> 상태여야 함.

---

## 6. 연결 확인 테스트

Claude Code 에서 다음 프롬프트 입력:

### 테스트 1: 로컬 데이터 읽기
```
CSV 파일 목록 보여줘.
```
**기대:** `list_csv_tables` 실행 + 40개 파일 반환

### 테스트 2: T-Box 분석
```
T-Box 를 분석해줘.
```
**기대:** §5.4 fallback 산출물을 적재했다면 `analyze_tbox` 가 클래스/OP/DP 수를
정상 반환합니다 (철강 샘플: 클래스 89 / OP 108 / DP 272 — 2026-09-06 산출물). 아직 적재 전이라
"T-Box 파일이 없습니다" 가 나오면 §5.4 절차로 `data/generated/tbox/t_box.ttl` 을
먼저 적재하세요.

> **참고**: T-Box 생성 (Bedrock 15~40분) 은 **강사가 D-1 에 미리 실행**하며,
> 워크샵 당일 참가자는 본인 PC 에서 **생성하지 않고 결과만 분석/시각화** 합니다
> (workbook Ch3-1/3-2 참조). 따라서 이 테스트는 사전 적재한 T-Box 를 분석하는
> 것이지, 당일 새로 생성하는 단계가 아닙니다.

### 테스트 3: (선택) Mutation 감사 도구 존재 확인
```
사용 가능한 MCP 도구 중 mutation 관련 도구를 알려줘.
```
**기대:** `run_tbox_mutation_audit`, `run_kg_mutation_audit`, `run_meta_audit`,
`get_meta_audit_history`, `read_meta_audit` — 5개가 언급되면 Mutation Audit 기능 정상 등록.

---

## 7. (선택) Neo4j — RCA 분석 데모용

```bash
docker --version  # 없으면 1.6 참조

# 1) Neo4j 비밀번호를 직접 정해 이 셸의 환경변수 NEO4J_PASSWORD 에 둔다.
#    입력은 화면과 셸 기록에 남지 않는다. Neo4j 5 의 기본 최소 길이는 8자다.
printf 'Neo4j 비밀번호 (8자 이상): '; stty -echo; read -r NEO4J_PASSWORD; stty echo; echo
export NEO4J_PASSWORD

# 2) 포트는 127.0.0.1 (이 PC) 에만 연다. 같은 네트워크 (회의장 Wi-Fi 등) 의 다른 PC 는 접속할 수 없다.
docker run -d --name neo4j-rdf \
  -p 127.0.0.1:7474:7474 -p 127.0.0.1:7687:7687 \
  -e NEO4J_AUTH="neo4j/${NEO4J_PASSWORD}" \
  -e NEO4J_PLUGINS='["apoc"]' \
  -e NEO4J_server_memory_heap_max__size=2G \
  neo4j:5-community
```

`.env` 의 Neo4j 항목을 채웁니다 (`.env.example` 에는 `NEO4J_PASSWORD` 만 빈 값으로 들어 있습니다).
`NEO4J_PASSWORD` 에는 위에서 `NEO4J_PASSWORD` 환경변수에 넣은 값을 그대로 씁니다:
```
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<위 NEO4J_PASSWORD 환경변수에 넣은 값>
```

**확인:** http://127.0.0.1:7474 에서 Neo4j Browser 접속 (ID: neo4j / PW: 위 `NEO4J_PASSWORD` 환경변수에 넣은 값)

> 포트를 모든 인터페이스에 열거나 (`-p 7474:7474` 처럼 IP 를 생략) 문서에 적힌 값을 비밀번호로
> 쓰지 마세요. 같은 네트워크의 누구나 그 비밀번호로 그래프를 조회하고 지울 수 있습니다.

> Neo4j 가 없어도 워크샵 진행에 지장 없습니다 — 강사가 데모합니다.

---

## 8. 최종 체크리스트

**필수**
- [ ] 0단계 Bedrock 모델 접근 승인 확인 (`aws bedrock list-foundation-models` 에 Claude Sonnet)
- [ ] Python 3.11+ 설치
- [ ] AWS CLI 설정 완료 (`aws sts get-caller-identity` 성공)
- [ ] ontology-agent 클론 + venv 의존성 설치 완료
- [ ] `.env` 파일에 AWS_PROFILE / BEDROCK_MODEL_ID 설정
- [ ] 로컬 데이터 40개 CSV 확인
- [ ] Claude Code 재시작 후 `list_csv_tables` 테스트 성공
- [ ] **(D-1 까지)** Bedrock 미승인 또는 8GB RAM 사용자는 §5.4 fallback 산출물 사전 적재 + `python scripts/verify_workshop_sparql.py --ignore-placeholders` exit 0 회신 (강사 회수)

**권장**
- [ ] Java 25+ 설치 (HermiT 단독은 11+, Pellet 은 25+) + `"$JAVA_EXE" -version` 동작 (없으면 5단계 검증 중 2개 스킵. 워크샵 본 챕터 진행에 지장 없음)
- [ ] `.env` 의 `JAVA_EXE` 경로 설정 (Java 설치한 경우)

**선택**
- [ ] Neo4j Docker 실행 (127.0.0.1 바인딩 + 직접 정한 비밀번호) + http://127.0.0.1:7474 접속
- [ ] Mutation Audit 도구 5종 등록 확인 (테스트 3)

**모든 항목이 ✓ 여야 워크샵 당일 환경 이슈 없이 진행됩니다.**

---

## 9. 자주 막히는 지점

| 증상 | 원인 | 해결 |
|------|------|------|
| `list_csv_tables` 가 보이지 않음 | MCP 등록 실패 (경로 오류 또는 venv 문제) | 5.3 절 순서대로 점검 |
| `aws sts get-caller-identity` 실패 | AWS credentials 미설정 | 회사 IT/admin 에 IAM 요청 |
| Bedrock 호출 시 AccessDeniedException | 모델 접근 미승인 | 0.2 절 — Bedrock console 에서 "Request access" |
| `JAVA_EXE` 경로가 없다고 에러 | 경로가 잘못됨 | `which java` (mac/Linux) 또는 `where.exe java` (Win) 로 재확인 |
| Pellet 호출 시 `UnsupportedClassVersionError (class file version 69.0)` | Java 25 미만 (HermiT 은 11+ 로 동작해 S4 는 통과할 수 있음) | §1.3 의 버전 25 설치 명령으로 설치 후 `.env` 의 `JAVA_EXE` 를 새 경로로 갱신 + Claude Code 재시작 |
| Windows 에서 venv activate 시 실행 정책 오류 | PowerShell 실행 정책 | `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` |
| pip install 시 타임아웃 | 회사 프록시 | `pip install --proxy <proxy_url> -r requirements.txt` |
| vis.js 시각화가 빈 화면 | CDN 차단 (사내 네트워크) | 워크샵 당일 강사 노트북 화면 공유로 대체 |
| `.env` 변경했는데 적용 안 됨 | MCP 서버는 첫 기동 시 `.env` 만 읽음 | **Claude Code 재시작 필수** — 데스크톱 앱 완전 종료(Quit) 후 재시작. CLI 는 새 셸 세션 |
| A-Box 생성/추론 후 메모리 부족 (8GB Mac) | 37MB a_box.ttl 로드 + 추론 시 6GB+ 사용 | RAM 16GB 이상 권장. 부족 시 `RDFLIB_STORE=oxigraph` (기본값) 유지 + 다른 앱 종료. 그래도 OOM 이면 `run_owl_rl_inference(fast_mode=True)` 로 마스터 테이블만 추론 |
| 한국어 질의가 엉뚱한 클래스로 매핑됨 | `ask_ontology` 가 한국어 약어 인식 못함 | `rules/domain/korean_synonyms.json` 추가 — 자세한 형식은 `sparql-cheatsheet.md` 의 "한국어 용어 정확도 팁" 박스 참조. opt-in 이라 파일 없으면 동작 변화 없음 |
