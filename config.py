import os
from pathlib import Path

from dotenv import load_dotenv


def reload_dotenv(path: str | None = None, override: bool = False) -> None:
    """dotenv를 명시적으로 다시 로드한다. 테스트에서 환경을 교체할 때 사용.

    Args:
        path: .env 경로. None이면 DOTENV_PATH 환경변수 또는 ".env".
        override: True면 기존 os.environ 값을 .env 값으로 덮어씀.
    """
    target = path or os.getenv("DOTENV_PATH", ".env")
    load_dotenv(target, override=override)


# 모듈 import 시 기본 .env 1회 로드(기존 동작 유지). 테스트가 바꾸려면 reload_dotenv() 호출.
reload_dotenv()

# 프로젝트 루트 (config.py가 위치한 디렉토리)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# AWS
AWS_REGION = os.getenv("AWS_REGION", "ap-northeast-2")
AWS_PROFILE = os.getenv("AWS_PROFILE")

def _safe_int(env_name: str, default: int) -> int:
    raw = os.getenv(env_name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "env %s=%r is not an integer; falling back to default %d", env_name, raw, default
        )
        return default


# 로컬 데이터 경로
DATA_DIR = os.getenv("DATA_DIR", os.path.join(PROJECT_ROOT, "data"))
SOURCE_DIR = os.path.join(DATA_DIR, "source")          # 원본 입력 데이터
GENERATED_DIR = os.path.join(DATA_DIR, "generated")     # 생성된 산출물


def resolve_generated_path(relative: str | os.PathLike[str]) -> Path:
    """상대 경로를 ``data/generated`` 아래의 안전한 산출물 경로로 해석한다.

    절대경로, 빈 경로, ``..`` 탈출과 외부를 가리키는 symlink를 거부한다.
    디렉터리는 만들지 않으며 호출자가 실제 쓰기 직전에 생성한다.
    """
    raw = os.fspath(relative)
    if not raw or "\x00" in raw:
        raise ValueError("생성 산출물 상대 경로는 비어 있을 수 없습니다.")

    candidate_relative = Path(raw)
    if candidate_relative.is_absolute() or candidate_relative in {Path("."), Path("..")}:
        raise ValueError("생성 산출물 경로는 data/generated 아래의 상대 경로여야 합니다.")

    root = Path(GENERATED_DIR).resolve()
    candidate = (root / candidate_relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("생성 산출물 경로가 data/generated 밖을 가리킵니다.") from exc
    if candidate == root:
        raise ValueError("생성 산출물 경로는 파일이나 하위 디렉터리를 지정해야 합니다.")
    return candidate


# source 하위 경로
SOURCE_RAWDATA_DIR = os.path.join(SOURCE_DIR, "rawdata")    # 39개 CSV
SOURCE_REFERENCE_DIR = os.path.join(SOURCE_DIR, "reference") # IOF 참조
SOURCE_MAPPING_DIR = os.path.join(SOURCE_DIR, "mapping")     # IOF 매핑
SOURCE_TACIT_DIR = os.path.join(SOURCE_DIR, "tacit")         # 암묵지 TTL
SOURCE_QUERY_TESTS_DIR = os.path.join(SOURCE_DIR, "query_tests")  # CQ + Golden SPARQL

# CQ/Golden 파일 경로 (편의 상수 — tools/* 전체에서 재사용)
COMPETENCY_QUESTIONS_PATH = os.path.join(
    SOURCE_QUERY_TESTS_DIR, "competency_questions.json"
)
GOLDEN_QUERIES_PATH = os.path.join(
    SOURCE_QUERY_TESTS_DIR, "golden_queries.json"
)

# generated 하위 경로
GENERATED_TBOX_DIR = os.path.join(GENERATED_DIR, "tbox")          # T-Box TTL
GENERATED_ABOX_DIR = os.path.join(GENERATED_DIR, "abox")          # A-Box TTL
GENERATED_INFERRED_DIR = os.path.join(GENERATED_DIR, "inferred")  # 추론 결과
GENERATED_INCREMENTAL_DIR = os.path.join(GENERATED_DIR, "incremental")  # 실시간
GENERATED_REPORTS_DIR = os.path.join(GENERATED_DIR, "reports")          # HTML 보고서/시각화

# 주요 파일 경로 (편의 상수)
TBOX_PATH = os.path.join(GENERATED_TBOX_DIR, "t_box.ttl")
TBOX_BASELINE_PATH = os.path.join(GENERATED_TBOX_DIR, "t_box_baseline.ttl")
ABOX_PATH = os.path.join(GENERATED_ABOX_DIR, "a_box.ttl")
MASTER_DATA_PATH = os.path.join(GENERATED_ABOX_DIR, "master_data.ttl")
INFERRED_PATH = os.path.join(GENERATED_INFERRED_DIR, "all_inferred.ttl")
SEMANTIC_DICT_PATH = os.path.join(GENERATED_DIR, "semantic_dictionary.json")
LPG_SEMANTIC_DICT_PATH = os.path.join(GENERATED_DIR, "neo4j", "semantic_dictionary.json")

# Bedrock
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
BEDROCK_READ_TIMEOUT = _safe_int("BEDROCK_READ_TIMEOUT", 300)
BEDROCK_MAX_TOKENS = _safe_int("BEDROCK_MAX_TOKENS", 32000)
BEDROCK_MAX_RETRIES = _safe_int("BEDROCK_MAX_RETRIES", 2)

# Neo4j (선택 — RCA 분석/그래프 시각화용)
NEO4J_URI = os.getenv("NEO4J_URI", "")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")

# Oracle (선택적 OBDA 실행기용. SQL 계획만 사용할 때는 연결 불필요).
ORACLE_DSN = os.getenv("ORACLE_DSN", "")
ORACLE_USER = os.getenv("ORACLE_USER", "")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "")

# Java (OWL 추론기용 — Java 25+ 필요)
JAVA_EXE = os.getenv("JAVA_EXE", "")

#: JVM heap (MB) for HermiT / Pellet. owlready2's own default is 2000, which
#: OOMs on this project's inferred graph — 2026-08-28 실측: 1.37M 트리플에서
#: "OutOfMemoryError thrown from the UncaughtExceptionHandler" 로 Pellet 이 죽었고
#: 24000 에서 통과했다. 0 이하 또는 비수치면 owlready2 기본값을 그대로 둔다.
JAVA_MEMORY_MB = _safe_int("JAVA_MEMORY_MB", 0)

# GraphDB Free (대용량 OWL 2 RL 추론용 — reasonable 한계 회피)
GRAPHDB_BASE_URL = os.getenv("GRAPHDB_BASE_URL", "http://localhost:7200")


def _derive_default_repo() -> str:
    """domain_config.json 의 namespace.prefix 로 repository 이름 유도.

    우선순위:
    1. GRAPHDB_REPOSITORY 환경변수 (caller 가 직접 지정한 경우 우선)
    2. domain_config.json 의 namespace.prefix → '{prefix}-kg'
    3. fallback: 'default-kg'

    이전엔 특정 배포의 repo ID 가 코드 기본값이었는데, 도메인 중립 원칙
    위배라 prefix 기반 자동 유도로 교체.
    """
    try:
        from domain.namespaces import DOMAIN_CONFIG
        prefix = DOMAIN_CONFIG.get("namespace", {}).get("prefix", "").strip()
        if prefix:
            return f"{prefix}-kg"
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "domain prefix 기반 GraphDB repository 이름 유도 실패; default-kg 사용: %s",
            exc,
        )
    return "default-kg"


GRAPHDB_REPOSITORY = os.getenv("GRAPHDB_REPOSITORY") or _derive_default_repo()
GRAPHDB_RULESET = os.getenv("GRAPHDB_RULESET", "owl2-rl-optimized")
GRAPHDB_TIMEOUT = _safe_int("GRAPHDB_TIMEOUT", 600)

_boto3_session_cache: list = []
_boto3_session_lock_holder: list = []


def get_boto3_session():
    """boto3 Session을 지연 생성 + 프로세스 내 싱글턴으로 재사용한다.

    재사용 근거: Session은 credentials lookup 비용이 있는 상대적으로 무거운 객체.
    동일 프로세스에서 Bedrock 모듈이 각자 호출 시 세션 남발 방지.
    테스트에서 초기화가 필요하면 reset_boto3_session()을 호출.
    """
    if _boto3_session_cache:
        return _boto3_session_cache[0]
    import threading

    import boto3  # 지연 임포트: 로컬 도구만 사용 시 boto3 불필요
    if not _boto3_session_lock_holder:
        _boto3_session_lock_holder.append(threading.Lock())
    lock = _boto3_session_lock_holder[0]
    with lock:
        if _boto3_session_cache:
            return _boto3_session_cache[0]
        if AWS_PROFILE:
            session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
        else:
            session = boto3.Session(region_name=AWS_REGION)
        _boto3_session_cache.append(session)
        return session


def reset_boto3_session() -> None:
    """테스트에서 세션 캐시를 비운다."""
    _boto3_session_cache.clear()


# ─────────────────────────────────────────────────────────
# Grouped config views (read-only) — 서브시스템별 일괄 조회 + 검증 진입점
# 기존 `from config import X` 호출자는 계속 동작. 새 코드는 아래 함수 사용을 권장.
# ─────────────────────────────────────────────────────────

from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402


@dataclass(frozen=True)
class AWSConfig:
    region: str
    profile: str | None


@dataclass(frozen=True)
class BedrockConfig:
    model_id: str
    region: str
    read_timeout: int
    max_tokens: int
    max_retries: int


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str


def aws_config() -> AWSConfig:
    return AWSConfig(region=AWS_REGION, profile=AWS_PROFILE)


def bedrock_config() -> BedrockConfig:
    return BedrockConfig(
        model_id=BEDROCK_MODEL_ID, region=BEDROCK_REGION,
        read_timeout=BEDROCK_READ_TIMEOUT, max_tokens=BEDROCK_MAX_TOKENS,
        max_retries=BEDROCK_MAX_RETRIES,
    )


def neo4j_config() -> Neo4jConfig:
    return Neo4jConfig(uri=NEO4J_URI, user=NEO4J_USER, password=NEO4J_PASSWORD)


def validate_config(required: tuple[str, ...] = ()) -> dict[str, Any]:
    """필수 서브시스템 환경 변수의 유무를 검증한다.

    Args:
        required: ("bedrock", "neo4j") 중 필수로 삼을 서브시스템 키.
                  해당 서비스의 핵심 env가 비어 있으면 결과 dict에 "missing" 목록으로 표시.

    Returns:
        {"ok": bool, "missing": [...]} 형태. 서버 기동 스크립트가 판단용으로 사용.
    """
    missing: list[str] = []
    if "bedrock" in required and not BEDROCK_MODEL_ID:
        missing.append("BEDROCK_MODEL_ID")
    if "neo4j" in required and not NEO4J_URI:
        missing.append("NEO4J_URI")
    return {"ok": not missing, "missing": missing}
