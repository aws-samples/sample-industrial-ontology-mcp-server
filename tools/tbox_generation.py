"""T-Box 생성 도구 — Bedrock LLM 기반 OWL 온톨로지 스키마 생성

admin-webapp의 tbox_generation_service.py를 MCP 도구로 포팅.
적응형 청킹, 모듈식 프롬프트, SHACL 교정 루프를 포함한다.
"""

import glob
import json
import logging
import os
import re
import time
from collections import Counter
from difflib import SequenceMatcher

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef

from config import (
    COMPETENCY_QUESTIONS_PATH,
    SOURCE_MAPPING_DIR,
    SOURCE_RAWDATA_DIR,
    TBOX_BASELINE_PATH,
    TBOX_PATH,
)
from domain.namespaces import (
    DOMAIN_CONFIG,
    DOMAIN_NS,
    DOMAIN_NS_OBJ,
    IOF_CORE,
    IOF_MAINT,
    IOF_SCRO,
    NS_PREFIX,
)
from domain.rules_paths import RULES_ROOT, rules_path
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.bedrock import invoke_bedrock_with_metadata
from tools.common import atomic_write, error_response

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RULES_DIR = RULES_ROOT
_PROMPTS_DIR = os.path.join(_BASE_DIR, "prompts")


# ── Chunk progress hook ──────────────────────────
# _generate_chunk 내부 진행 상황(진입/완료/재귀분할/재시도)을 외부에 알리기 위한
# optional 모듈 스코프 콜백. multi_agent_tbox 의 _build_initial_draft 가 파일
# 하트비트를 끼워 넣는 용도. 하트비트 파일에 "chunk_idx/depth/stop_reason/event"
# 가 기록되면, 긴 LLM 호출이 실제로 어디서 멈춰 있는지 파악 가능.
#
# 시그니처: hook({"event": str, "chunk_idx": int, "depth": int,
#                "total_chunks": int, "tables": int, "stop_reason": str | None,
#                "ttl_chars": int | None, "elapsed_sec": float | None})
# event ∈ "chunk_start" | "chunk_done" | "chunk_split" | "chunk_retry" | "chunk_fail"
#
# 스레드 안전: generate_tbox 는 청크를 ThreadPoolExecutor 로 병렬 실행할 수 있다.
# hook 구현체는 파일 atomic write 를 쓰거나 자체 lock 을 가져야 한다.
_CHUNK_PROGRESS_HOOK = None


def set_chunk_progress_hook(fn) -> None:
    """_generate_chunk 진행 알림 콜백 설정. None 으로 해제."""
    global _CHUNK_PROGRESS_HOOK
    _CHUNK_PROGRESS_HOOK = fn


def _emit_chunk_progress(**payload) -> None:
    """등록된 hook 을 안전하게 호출. 예외는 본작업을 중단시키지 않는다."""
    if _CHUNK_PROGRESS_HOOK is None:
        return
    try:
        _CHUNK_PROGRESS_HOOK(payload)
    except Exception as e:
        logger.debug("chunk progress hook 실패 (무시): %s", e)


# ── Config / Rules 로딩 ──────────────────────────


def _load_json(filename):
    with open(rules_path(filename, base=_RULES_DIR), encoding="utf-8") as f:
        return json.load(f)


def _load_abstract_group_hints() -> list[dict]:
    """rules/domain/abstract_group_hints.json 이 있으면 groups 배열 반환, 없으면 [].

    도메인별 중간 추상 클래스 그룹 힌트. 존재하지 않거나 파싱 실패해도 파이프라인
    정상 진행. 반환 스키마는 프롬프트 렌더러가 안전하게 파싱하도록 dict 유지.
    """
    path = rules_path("abstract_group_hints.json", base=_RULES_DIR)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        groups = data.get("groups", [])
        if isinstance(groups, list):
            return groups
    except Exception as e:
        logger.debug("abstract_group_hints.json 로드 실패 (무시): %s", e)
    return []


def _load_op_name_hints() -> dict:
    """rules/policy/op_name_hints.json 이 있으면 dict, 없으면 {}.

    P0 — Architect 프롬프트에 주입할 표준 OP 이름 힌트. IOF 등 표준 vocab 에서
    자주 쓰는 이름을 재사용하도록 유도 (soft hint). 파일 없거나 손상 시 빈 dict
    → 기존 프롬프트 동작 완전 보존.
    """
    path = rules_path("op_name_hints.json", base=_RULES_DIR)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.debug("op_name_hints.json 로드 실패 (무시): %s", e)
    return {}


def _render_op_name_hints_block() -> str:
    """힌트가 있으면 Architect 프롬프트용 블록 렌더링. 없으면 빈 문자열.

    P0 — `prefer_these_names_when_applicable` 과 `avoid_these_patterns` 를
    구조화된 섹션으로 출력. LLM 이 의미가 맞으면 표준 이름을 재사용하도록 nudge.
    Constraint 가 아닌 preference — 도메인 고유 이름은 여전히 허용.
    """
    hints = _load_op_name_hints()
    if not hints:
        return ""
    prefer = hints.get("prefer_these_names_when_applicable") or []
    avoid = hints.get("avoid_these_patterns") or []
    if not prefer and not avoid:
        return ""
    lines = [
        "## ObjectProperty 네이밍 힌트 (표준 vocab 재사용 선호)",
        "",
        "아래 OP 이름은 IOF/BFO 같은 표준 온톨로지에 이미 존재합니다.",
        "의미가 맞으면 **재사용 권장** — 비슷한 의미를 다른 이름으로 재발명하지 마세요.",
        "이 힌트는 constraint 가 아닌 **default 선호** — 도메인 고유 의미가 필요하면 새 이름 OK.",
        "",
    ]
    if prefer:
        lines.append("### 선호 이름 목록")
        for h in prefer:
            if not isinstance(h, dict):
                continue
            name = h.get("name")
            meaning = h.get("meaning", "")
            if not name:
                continue
            lines.append(f"- `{name}`: {meaning}")
        lines.append("")
    if avoid:
        lines.append("### 피해야 할 패턴")
        for p in avoid:
            if not p:
                continue
            lines.append(f"- {p}")
        lines.append("")
    return "\n".join(lines)


def _render_hierarchy_hints_block() -> str:
    """힌트가 있으면 프롬프트용 한국어 블록 렌더링. 없으면 빈 문자열.

    도메인 이식성: 이 함수는 특정 산업의 클래스를 하드코딩하지 않는다. 힌트는
    rules/domain/abstract_group_hints.json 에서 로드하므로, 다른 도메인으로 교체 시엔
    해당 파일을 교체하거나 비우면 된다 (원칙만 적용).
    """
    groups = _load_abstract_group_hints()
    if not groups:
        return ""
    lines = [
        "## 도메인 중간 추상 클래스 힌트 (선택적 참고)",
        "",
        "아래는 **rules/domain/abstract_group_hints.json** 에서 로드한 도메인 그룹 힌트입니다.",
        "절대 필수가 아니지만, 최상위 클래스 비율 ≤ 20% 달성을 돕기 위해 **적절한 경우**",
        "아래 그룹을 상위 추상 클래스로 선언하고 관련 자식을 rdfs:subClassOf 로 묶으세요.",
        "",
    ]
    for g in groups:
        if not isinstance(g, dict):
            continue
        name = g.get("abstract_class")
        if not name:
            continue
        rationale = g.get("rationale", "")
        patterns = g.get("child_name_patterns") or []
        examples = g.get("child_examples") or []
        iof = g.get("iof_parent")
        lines.append(f"- **{name}**: {rationale}")
        if patterns:
            lines.append(f"  - 자식 후보 이름 패턴: {', '.join(str(p) for p in patterns)}")
        if examples:
            lines.append(f"  - 자식 예시: {', '.join(str(e) for e in examples[:8])}")
        if iof:
            lines.append(f"  - IOF 부모 추천: `{iof}` (사용 시 rdfs:subClassOf 로 연결)")
    lines.append("")
    lines.append(
        "**주의**: 위 힌트는 참고용. 실제 CSV/CQ 에 안 맞으면 생성하지 마세요. "
        "무리하게 맞추기보다 **원칙**(다음 섹션)이 우선.",
    )
    return "\n".join(lines)


def _load_config():
    return _load_json("tbox_generation_config.json")


def _table_name_to_class(table_name: str) -> str:
    """CSV 파일명 → PascalCase OWL 클래스명. 예: Soil_Monitoring → SoilMonitoring"""
    return "".join(part.capitalize() for part in table_name.replace("-", "_").split("_"))


def _load_table_class_mapping() -> dict:
    """CSV 파일에서 자동 생성 + rules/domain/table_class_mapping.json 오버라이드.

    1. data/source/rawdata/*.csv 에서 파일명으로 자동 매핑 생성
    2. table_class_mapping.json에 명시된 오버라이드를 덮어씀
    """
    # 1) CSV 파일에서 자동 생성
    csv_files = glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))
    auto_map = {}
    for csv_path in csv_files:
        table_name = os.path.basename(csv_path).replace(".csv", "")
        auto_map[table_name] = _table_name_to_class(table_name)

    # 2) 오버라이드 적용 (파일 없으면 자동 매핑만 사용).
    #    파싱은 ``domain.table_mapping`` 이 담당한다 — 예전엔 배포 prefix 만 벗겨서
    #    다른 prefix 가 붙은 값 (``other:Alpha``) 이 콜론째 클래스명이 됐고, 중첩
    #    dict (``{"class": ...}``) 는 무시돼 그 테이블이 사라졌다. S2 는 T-Box 를
    #    **생산** 하는 쪽이라 여기서 갈라지면 이후 전 단계가 다른 이름을 본다
    #    (2026-08-08 실측).
    from domain.table_mapping import load_table_class_mapping
    auto_map.update(load_table_class_mapping())

    return auto_map


# ── Bedrock 호출 (tools.bedrock 공유 헬퍼 사용) ──


def _invoke_bedrock(prompt: str, max_tokens: int = 32000, max_retries: int = 2,
                    temperature: float | None = 0.2,
                    cached_prefix: str | None = None) -> dict:
    """Bedrock Claude 호출. invoke_bedrock_with_metadata 위임.

    P9: T-Box Architect는 결정적 설계를 위해 기본 temperature=0.2.

    초안(Round 1) 생성도 Architect 작업이므로 MULTI_AGENT_MODEL_ARCHITECT 가
    설정돼 있으면 그 모델을 사용한다. 미설정 시 model_id=None 으로 위임되어
    BEDROCK_MODEL_ID 로 fallback (기존 동작 보존, backward compat).

    cached_prefix: 청크 독립 정적 프롬프트 블록. 동일 prefix 로 5분 TTL 내 재호출
    시 Bedrock prompt cache 적중 — 재귀 분할/병렬 청크에서 입력 토큰 재청구 없음.
    """
    architect_model = os.getenv("MULTI_AGENT_MODEL_ARCHITECT") or None
    return invoke_bedrock_with_metadata(
        prompt, max_tokens=max_tokens, max_retries=max_retries,
        temperature=temperature, cached_prefix=cached_prefix,
        model_id=architect_model,
    )


def _strip_prefix_and_ontology_decl(ttl: str) -> str:
    """@prefix / @base 선언과 owl:Ontology 정의 블록을 제거.

    `_generate_chunk` 분할 재시도 경로에서 half 2 의 @prefix 선언이 병합된
    TTL 중간에 그대로 남으면 후속 파서 호출이 깨질 수 있다. 이 헬퍼는
    라인 기반으로 @prefix/@base 를 떼고, `<...> a owl:Ontology` 로 시작하는
    블록(마침표로 끝) 역시 제거한다. 이미 first half 에 두 선언이 있기 때문.
    """
    lines = ttl.splitlines()
    out: list[str] = []
    skip_ontology_block = False
    for line in lines:
        stripped = line.lstrip()
        if not skip_ontology_block:
            if stripped.startswith("@prefix") or stripped.startswith("@base")\
                or stripped.startswith("PREFIX ") or stripped.startswith("BASE "):
                continue
            if "owl:Ontology" in line and (stripped.startswith("<") or ":" in stripped[:40]):
                skip_ontology_block = True
                # 마침표로 끝나면 한 줄짜리
                if line.rstrip().endswith("."):
                    skip_ontology_block = False
                continue
            out.append(line)
        else:
            if line.rstrip().endswith("."):
                skip_ontology_block = False
            continue
    return "\n".join(out)


def _extract_ttl_from_markdown(content: str) -> str:
    """마크다운 코드 블록에서 TTL 추출.

    우선순위:
    1) 닫힌 ```turtle``` 펜스 → 펜스 내부만 반환.
    2) 열린 ```turtle 만 있고 max_tokens 로 닫힘 누락 → 펜스 이후 전체.
    3) 펜스가 아예 없지만 응답 본문에 ``@prefix`` 나 ``@base`` 또는
       ``BASE``/``PREFIX`` (SPARQL 스타일) 가 등장 → 그 지점 이후로 간주.
       Sonnet 4.6 계열이 reasoning prelude("I will analyze...") 후 코드 펜스
       없이 바로 TTL 로 이어가는 회귀를 방어한다.
    4) 셋 다 실패하면 strip 결과 그대로 — 호출자 TTL 파서가 명시적 에러.
    """
    stripped = content.strip()
    # Try full fenced block first
    match = re.search(r"```(?:turtle|ttl)?\s*\n(.*?)```", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: opening fence present but closing fence missing (truncation).
    open_match = re.search(r"```(?:turtle|ttl)?\s*\n", stripped)
    if open_match:
        tail = stripped[open_match.end():]
        tail = re.sub(r"\n?```[a-zA-Z]*\s*$", "", tail)
        return tail.strip()
    # Fallback: no fences at all — look for TTL/SPARQL directive start.
    directive_match = re.search(r"(?im)^\s*(@prefix|@base|PREFIX\s|BASE\s)", stripped)
    if directive_match:
        return stripped[directive_match.start():].strip()
    return stripped


# ── 커버리지 리포트 ─────────────────────────────────


def _coverage_report(ttl: str, all_tables: list) -> dict:
    """생성된 T-Box가 입력 CSV 테이블/컬럼을 얼마나 커버하는지 측정한다.

    Returns:
        {
            "tables_total": int,
            "tables_covered": int,
            "tables_missing": [str],
            "columns_total": int,
            "columns_covered": int,
            "columns_missing": [{"table": str, "column": str}],
            "coverage_percent": float,
        }
    """
    g = _new_graph()
    g.parse(data=ttl, format="turtle")

    domain_ns = DOMAIN_NS

    # T-Box에 존재하는 클래스명 (lowercase)
    tbox_classes = set()
    for cls in g.subjects(RDF.type, OWL.Class):
        if str(cls).startswith(domain_ns):
            tbox_classes.add(_local_name(str(cls)).lower())

    # T-Box에 존재하는 DatatypeProperty명 (lowercase)
    tbox_props = set()
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        if str(prop).startswith(domain_ns):
            tbox_props.add(_local_name(str(prop)).lower())

    # 테이블 커버리지
    tables_missing = []
    for table in all_tables:
        table_name = table["name"]
        # PascalCase 변환
        pascal = "".join(part.capitalize() for part in table_name.replace("-", "_").split("_"))
        if pascal.lower() not in tbox_classes:
            tables_missing.append(table_name)

    # 컬럼 커버리지
    columns_total = 0
    columns_covered = 0
    columns_missing = []
    fuzzy_matches = []
    tbox_props_list = sorted(tbox_props)  # for fuzzy matching iteration
    for table in all_tables:
        for col in table.get("columns", []):
            columns_total += 1
            # snake_case → camelCase
            parts = col.lower().split("_")
            camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
            if camel.lower() in tbox_props:
                columns_covered += 1
            else:
                columns_missing.append({"table": table["name"], "column": col})
                # Fuzzy match: find similar T-Box property names for potential typos
                col_lower = camel.lower()
                best_ratio = 0.0
                best_match = ""
                for prop_name in tbox_props_list:
                    ratio = SequenceMatcher(None, col_lower, prop_name).ratio()
                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match = prop_name
                if best_ratio > 0.7:
                    fuzzy_matches.append({
                        "table": table["name"],
                        "column": col,
                        "similar_property": best_match,
                        "similarity": round(best_ratio, 3),
                    })

    tables_covered = len(all_tables) - len(tables_missing)
    coverage_pct = round(
        ((tables_covered + columns_covered) / (len(all_tables) + columns_total)) * 100, 1
    ) if (len(all_tables) + columns_total) > 0 else 0

    return {
        "tables_total": len(all_tables),
        "tables_covered": tables_covered,
        "tables_missing": tables_missing,
        "columns_total": columns_total,
        "columns_covered": columns_covered,
        "columns_missing": columns_missing[:20],  # 상위 20건만
        "columns_missing_count": len(columns_missing),
        "fuzzy_matches": fuzzy_matches[:20],  # 상위 20건만
        "fuzzy_matches_count": len(fuzzy_matches),
        "coverage_percent": coverage_pct,
    }


# ── 프롬프트 조립 ─────────────────────────────────


def _load_prompt_modules() -> dict:
    modules_dir = os.path.join(_PROMPTS_DIR, "tbox-prompt-modules")
    modules = {}
    for filename in sorted(os.listdir(modules_dir)):
        if filename.endswith(".md"):
            key = filename.replace(".md", "")
            with open(os.path.join(modules_dir, filename), encoding="utf-8") as f:
                modules[key] = f.read()
    return modules


def _domain_format_vars() -> dict:
    """domain_config.json에서 프롬프트 템플릿 치환 변수를 구성한다."""
    d = DOMAIN_CONFIG.get("domain", {})
    ns = DOMAIN_CONFIG.get("namespace", {})
    meta = DOMAIN_CONFIG.get("metadata", {})
    return {
        "domain_name": d.get("name", ""),
        "domain_name_ko": d.get("name_ko", ""),
        "industry": d.get("industry", ""),
        "industry_ko": d.get("industry_ko", ""),
        "ns_prefix": ns.get("prefix", ""),
        "ns_inst_prefix": ns.get("instance_prefix", ""),
        "class_ns": ns.get("class_ns", ""),
        "instance_ns": ns.get("instance_ns", ""),
        "ontology_uri": ns.get("ontology_uri", ""),
        "version": meta.get("version", ""),
        "creator": meta.get("creator", ""),
        "license_url": meta.get("license", ""),
    }


def _assemble_prompt(include_all: bool = True) -> str:
    """프롬프트 모듈 6개를 템플릿에 주입한다.

    Args:
        include_all: **더 이상 동작을 바꾸지 않는다** (하위 호환용). 항상 전체
            모듈을 주입한다. 아래 근거 참조.

    ## 왜 청크별 축약을 폐기했는가 (2026-08-26 실측)

    예전에는 ``chunk_idx == 0`` 만 전체를 받고 나머지는 ``04-property-rules`` ·
    ``06-checklist`` 만 받았다. 남은 4개 자리에는
    ``"(이전 청크에서 제공됨 — 생략)"`` 이 들어갔는데, **그 문구가 사실이 아니다** —
    청크마다 독립 LLM 호출이므로 이전 청크의 프롬프트를 본 적이 없다.

    실측 영향 (샘플 CSV 40개 → 청크 8개):

        청크 0      37,681자 (≈10,766 토큰)   생략 마커 0
        청크 1~7    ~21,700자 (≈6,200 토큰)   생략 마커 4
        빠지는 모듈  01-role-and-task · 02-ttl-guidelines ·
                    03-class-definitions · 05-quality-axioms
        영향 범위    테이블 34/40 (**85%**) 가 품질 공리·클래스 정의 규칙·
                    IOF 매핑표 없이 생성된다

    ## 절약 효과가 없다 (두 축 모두 손해)

    1. **토큰 예산**: 전체 주입 시 청크 1~7 이 약 8,800 토큰이 된다.
       ``max_tokens`` 는 32,000 이므로 27% 다 — 어느 청크도 초과하지 않는다
       (실측: 전체 주입 최대 청크가 16,598 토큰 추정).
    2. **캐시**: ``template`` 은 ``cached_prefix`` 안에 들어간다. 청크 0 과 1~7 이
       다른 prefix 를 만들어 **고유 prefix 2종** 이 되고, Bedrock prompt caching
       미스가 한 번 더 발생한다. 통일하면 1종이 되어 첫 호출만 미스, 이후 7회 적중.

    즉 축약은 토큰을 아끼지 못하면서 품질 규칙을 빼고 캐시를 깨뜨렸다. 도입 커밋
    (59c3877, 2026-03-30 초기 이식) 에 근거가 기록돼 있지 않다.

    파라미터를 지우지 않는 이유: 호출부가 ``include_all=is_first`` 로 넘기고 있고,
    시그니처를 바꾸면 그 호출부까지 동시에 손대야 한다. 인자는 남기고 **분기만**
    제거해 "왜 무시하는가" 를 이 docstring 에 남긴다.
    """
    modules = _load_prompt_modules()
    prompt_path = os.path.join(_PROMPTS_DIR, "T-Box-Generation-Prompt.md")
    with open(prompt_path, encoding="utf-8") as f:
        template = f.read()

    for key, content in modules.items():
        placeholder = f"<!-- {key}.md 내용이 여기에 삽입됨 -->"
        template = template.replace(placeholder, content)

    # domain_config.json 기반 템플릿 변수 치환
    fmt_vars = _domain_format_vars()
    template = template.format_map(fmt_vars)
    return template


# ── 적응형 청킹 ──────────────────────────────────


def _adaptive_chunking(tables: list, config: dict) -> list:
    budget = config["max_tokens"] * config["token_budget_ratio"]
    per_col = config["per_column_tokens"]
    per_table = config["per_table_tokens"]

    chunks = []
    current_chunk = []
    current_tokens = 0

    for table in tables:
        col_count = len(table.get("columns", []))
        table_tokens = per_table + (col_count * per_col)
        if current_tokens + table_tokens > budget and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            current_tokens = 0
        current_chunk.append(table)
        current_tokens += table_tokens

    if current_chunk:
        chunks.append(current_chunk)
    return chunks


# ── 청크별 생성 ───────────────────────────────────


def _cqs_touching_chunk(cqs: list, chunk_tables: list,
                        table_class_map: dict) -> list:
    """이 청크의 테이블이 매핑되는 클래스를 ``domains`` 에 가진 CQ 만 고른다.

    ## 왜 필요한가 (2026-08-26 실측)

    CQ 주입이 ``if is_first:`` 로 묶여 **청크 0 만** 받았다. 실측: CQ 12개가 요구하는
    도메인 클래스 40개 중 **33개(83%)가 청크 1~7 에 있다** — 즉 CQ 를 만족시켜야 하는
    클래스의 대부분이 요구사항을 본 적 없는 청크에서 생성된다. 그러고 나서 리뷰어가
    매 라운드 "CQ 필수 경로 누락" 을 critical 로 지적했다.

    ## 왜 전량이 아니라 필터인가

    전량 주입도 예산에는 들어간다 (청크당 6,200 → 8,218 토큰, ``max_tokens`` 32,000 의
    26%). 그러나 무관한 CQ 를 주면 LLM 이 **그 청크에 없는 클래스를 만들려 한다** —
    근거 없는 선언이 생기고, 그것이 이 리포가 반복 겪은 "유령 OP" 의 생성 경로다.
    청크가 실제로 만드는 클래스와 관련된 CQ 만 준다.

    매칭 실패 시(``domains`` 가 비었거나 클래스명이 안 맞음) **그 CQ 를 포함한다** —
    판정 불가를 배제로 읽으면 요구사항이 조용히 사라진다.
    """
    def _norm(name: str) -> str:
        return str(name).split(":")[-1].split("#")[-1].lower().replace("_", "")

    chunk_classes = {
        _norm(table_class_map.get(t["name"], ""))
        for t in chunk_tables
    }
    chunk_classes.discard("")
    if not chunk_classes:
        return list(cqs)          # 매핑을 모르면 전부 준다 (보수적)

    picked = []
    for cq in cqs:
        domains = cq.get("domains") or []
        if not domains:
            picked.append(cq)     # 판정 근거 없음 → 포함
            continue
        if any(_norm(d) in chunk_classes for d in domains):
            picked.append(cq)
    return picked


def _build_chunk_prompt(chunk_idx, chunk_tables, total_chunks,
                        iof_summary, schema_info, relationships_info,
                        table_class_map) -> tuple[str, str]:
    """청크 프롬프트 조립.

    Returns: (cached_prefix, variable_prompt)
        - cached_prefix: 청크별로 바뀌지 않는 정적 부분 (역할 도입부 + 규칙 + 금지어 +
          fence + 네이밍 금지). Bedrock prompt caching 대상.
        - variable_prompt: 청크별로 달라지는 부분 (IOF 매핑 요약, 테이블 스키마,
          베이스라인, CQ).
    호출자는 `invoke_bedrock_with_metadata(variable_prompt, cached_prefix=cached_prefix)`
    형태로 둘을 분리해 넘겨야 캐시 hit. 재귀 분할 호출이 같은 chunk_idx 로 여러 번
    들어와도 cached_prefix 는 동일하므로 5분 TTL 내 Bedrock 캐시 적중.
    """
    is_first = (chunk_idx == 0)
    # 모든 청크가 **같은** 템플릿을 받는다. 예전에는 ``include_all=is_first`` 로
    # 청크 1~7 이 모듈 4개를 잃었다 (근거는 _assemble_prompt docstring 참조).
    # 통일하면 cached_prefix 가 1종이 되어 Bedrock prompt cache 도 적중한다.
    template = _assemble_prompt()
    # 프롬프트 예시에 쓰이는 도메인 prefix (domain_config.json). 하드코딩 아님.
    _p = NS_PREFIX

    chunk_tables_info = json.dumps(
        {t["name"]: t.get("columns", []) for t in chunk_tables},
        ensure_ascii=False, indent=2,
    )
    chunk_class_mapping = "\n".join(
        f"  {t['name']} → {table_class_map.get(t['name'], 'UNKNOWN')}"
        for t in chunk_tables
    )

    # 표준항목 딕셔너리 메타: 컬럼코드 → 한글명 [값타입, 단위]. LLM 이
    # DP 이름/라벨/range 를 컬럼명만으로 추측하지 않도록 권위 있는 의미를 제공.
    # 컬럼 분할 청크에서는 해당 슬라이스의 컬럼만 노출(프롬프트 길이 절약).
    column_meta_block = ""
    meta_lines: list[str] = []
    for t in chunk_tables:
        cmeta = t.get("column_meta") or {}
        if not cmeta:
            continue
        slice_cols = set(t.get("columns", []))
        rows = [
            f"  {col}: {desc}" for col, desc in cmeta.items()
            if col in slice_cols
        ]
        if rows:
            meta_lines.append(f"### {t['name']}")
            meta_lines.extend(rows)
    if meta_lines:
        column_meta_block = (
            "\n\n## 컬럼 표준항목 사전 (공식 한글명 [값타입 N/C/D, 단위])\n"
            "아래는 각 컬럼의 **권위 있는 한글명과 값타입**입니다. DatatypeProperty 의\n"
            "rdfs:label(@ko) 와 rdfs:range 를 이 사전에 맞추세요. 값타입 N→xsd:decimal\n"
            "(정수성이면 xsd:integer), C→xsd:string, D→xsd:dateTime. 컬럼 코드 토큰을\n"
            "임의로 생략하지 말고 의미를 정확히 반영한 DP 이름을 지으세요.\n"
            + "\n".join(meta_lines)
        )

    # 베이스라인 T-Box에서 클래스 목록 추출 (일관성 유지 참고)
    baseline_context = ""
    if os.path.exists(TBOX_BASELINE_PATH):
        try:
            bg = _new_graph()
            bg.parse(TBOX_BASELINE_PATH, format="turtle")
            baseline_classes = sorted(
                {str(c).split("#")[-1] for c in bg.subjects(RDF.type, OWL.Class) if "#" in str(c)},
            )
            baseline_ops = sorted(
                {str(p).split("#")[-1] for p in bg.subjects(RDF.type, OWL.ObjectProperty) if "#" in str(p)},
            )
            if baseline_classes:
                baseline_context = (
                    f"\n\n## 베이스라인 T-Box 구조 (반드시 유지)\n"
                    f"아래 클래스와 ObjectProperty는 이전 버전에서 검증된 구조입니다.\n"
                    f"**이 구조를 유지하되, CQ 답변에 필요한 부분만 추가/수정하세요.**\n"
                    f"클래스명이나 프로퍼티명을 임의로 변경하지 마세요.\n\n"
                    f"### 베이스라인 클래스 ({len(baseline_classes)}개)\n"
                    f"{', '.join(baseline_classes)}\n\n"
                    f"### 베이스라인 ObjectProperty ({len(baseline_ops)}개)\n"
                    f"{', '.join(baseline_ops[:50])}"
                    f"{f'... 외 {len(baseline_ops) - 50}개' if len(baseline_ops) > 50 else ''}\n"
                )
        except Exception as exc:
            logger.warning("베이스라인 T-Box 로드 실패 (무시): %s", exc)

    # P5: Competency Questions 를 Architect 초안부터 반영.
    #
    # 예전에는 ``if is_first:`` 로 **청크 0 만** 받았다 (주석: "token 절약"). 실측
    # (2026-08-26): CQ 가 요구하는 도메인 클래스 40개 중 **33개(83%)가 청크 1~7 에
    # 있다** — CQ 를 만족시켜야 하는 클래스의 대부분이 요구사항을 본 적 없는 청크에서
    # 생성됐고, 그러고 나서 리뷰어가 매 라운드 "CQ 필수 경로 누락" 을 critical 로
    # 지적했다 (배포 3 run 승인 0건의 잔여 사유 중 최다).
    #
    # 절약 근거도 성립하지 않는다: CQ 블록은 ``variable_prompt`` 에 들어가 캐시 축과
    # 무관하고, 전량 주입해도 청크당 8,218 토큰 (``max_tokens`` 32,000 의 26%) 이다.
    # 다만 무관한 CQ 를 주면 LLM 이 그 청크에 없는 클래스를 만들려 하므로
    # ``_cqs_touching_chunk`` 로 **관련 CQ 만** 준다.
    cq_context = ""
    try:
        cq_path = COMPETENCY_QUESTIONS_PATH
        if os.path.exists(cq_path):
            with open(cq_path, encoding="utf-8") as cf:
                all_cqs = json.load(cf)
            cqs = _cqs_touching_chunk(
                all_cqs[:15] if isinstance(all_cqs, list) else [],
                chunk_tables, table_class_map,
            )
            if cqs:
                cq_lines = []
                for i, cq in enumerate(cqs):
                    q = cq.get("question_ko") or cq.get("question_en", "")
                    domains = ", ".join(cq.get("domains", [])[:3])
                    cq_lines.append(f"- **CQ{i+1}**: {q}\n  - 관련 도메인: {domains}")
                cq_context = f"""

## Competency Questions (T-Box가 반드시 답할 수 있어야 함)

다음 질문에 답하기 위한 클래스/ObjectProperty/DatatypeProperty 경로를
**T-Box 설계 시 처음부터 포함**시키세요.

{chr(10).join(cq_lines)}

### 각 CQ마다 설계 체크:
1. 필요한 클래스가 T-Box에 있는가?
2. 클래스 간 경유해야 할 ObjectProperty 경로가 선언됐는가?
3. 답변에 필요한 DatatypeProperty (값/수치)가 포함됐는가?

**SME의 라운드 2 지적을 피하려면 여기서 미리 반영하세요.**
"""
                # [P2] CQ 도메인 간 필수 OP 경로를 FK 패턴 기반으로 사전 분석해 주입.
                # Architect Round 1에서 누락된 2-hop OP 때문에 CQ 커버리지 15%로 시작하던
                # 문제 해결 (2026-04-18 회귀 분석 결과).
                try:
                    from tools.multi_agent_tbox import _analyze_cq_required_ops
                    cq_op_block = _analyze_cq_required_ops(cqs)
                    if cq_op_block:
                        cq_context += "\n\n" + cq_op_block + (
                            "\n\n**중요**: 위 OP 경로를 이번 청크에서 반드시 모두 선언하세요. "
                            "경로가 빠지면 S9 KG 검증과 S12 쿼리 테스트에서 대거 실패합니다."
                        )
                except Exception as _op_err:
                    logger.debug("CQ OP 경로 분석 skip: %s", _op_err)
    except Exception as e:
        logger.debug("CQ 로드 실패 (skip): %s", e)

    # ── cached_prefix (청크 독립 정적 부분) ────────────────────────────────
    # template + 금지어 규칙 + fence 규칙 + 출력 형식. is_first 여부만 반영되며,
    # 청크 번호/테이블 내용/베이스라인/CQ 는 포함하지 않는다. 재귀 분할 또는 parallel
    # 청크 호출 시 Bedrock prompt cache 적중 (5분 TTL).
    _reserved_rule = (
        "\n# 네이밍 금지 규칙 (**중요**)\n"
        "아래 local name 은 Python / owlready2 / HermiT 추론기와 충돌해 "
        "TypeError 를 유발하므로 Class / ObjectProperty / DatatypeProperty "
        "이름으로 **절대 사용 금지**:\n"
        "  type, class, object, subject, predicate,\n"
        "  description, location, severity\n"
        "이들 대신 도메인-특화 이름을 쓰세요 — 예: `type` → `alarmType` / "
        "`equipmentType`, `description` → `alarmDescription`, `severity` → "
        "`alarmSeverity`, `location` → `equipmentLocation`.\n"
    )
    # 중간 추상 클래스 계층 규칙 (R28 E — 도메인 중립). 최상위 클래스 비율
    # ≤ 20% 를 매 run 마다 초과하던 문제(실측 56~67%) 해결. 순수 원칙 + 선택적
    # 도메인 힌트(rules/domain/abstract_group_hints.json) 로 특정 산업에 국한되지 않게
    # 설계 — 예시 prefix 는 domain_config 에서 렌더.
    _hierarchy_rule = (
        "\n# 중간 추상 클래스 계층 규칙 (**최상위 비율 ≤ 20% 필수**)\n"
        "## 원칙\n"
        "- `rdfs:subClassOf` 선언이 없는 **최상위 클래스 비율 ≤ 20%**를 맞추세요.\n"
        "- 유사 관심사의 클래스 3개 이상이 모두 부모 없이 선언되면 **반드시**\n"
        "  상위 추상 클래스를 신설해 `rdfs:subClassOf` 로 묶으세요.\n"
        "- IOF 매핑이 가능한 경우: 신설 추상 클래스를 `rdfs:subClassOf iof-core:...`\n"
        "  또는 `iof-maint:...` 로 연결 (owl:imports 만으로는 부족 — 명시 매핑 필요).\n"
        "- 최상위 클래스(= `rdfs:subClassOf` 가 없는 클래스)는 오직 **도메인 최상위**\n"
        "  추상 개념만 허용 (예: MasterData, TransactionRecord, MonitoringMeasurement,\n"
        "  ProcessActivity — 구체 도메인 용어는 사용자 도메인에 맞게 네이밍).\n"
        "- 추상 클래스는 `rdfs:label`, `rdfs:comment` 필수. `owl:Class` 로 선언.\n"
        "\n## 자가 검증 체크 (출력 전 반드시 확인)\n"
        "1. 선언한 `owl:Class` 총 개수 세기\n"
        f"2. `rdfs:subClassOf {_p}:X` (도메인 네임스페이스) 없는 클래스 수 세기\n"
        "3. 비율 > 20% 면 추상 부모를 추가로 신설해 다시 묶기\n"
    )
    _hierarchy_hints = _render_hierarchy_hints_block()
    if _hierarchy_hints:
        _hierarchy_rule = _hierarchy_hints + "\n" + _hierarchy_rule

    # P0 — OP 네이밍 힌트 (rules/policy/op_name_hints.json). 파일 없으면 빈 문자열 →
    # 기존 프롬프트 동작 완전 보존. 청크 독립 정적 블록이므로 cached_prefix 에
    # 포함되어 prompt caching 적중.
    _op_name_hints_block = _render_op_name_hints_block()

    # Namespace 이중 접두사 방지 규칙 — R28 회귀 대응. Architect 가 반복적으로
    # `<{DOMAIN_NS}{NS_PREFIX}:X>` 같은 이중 접두사 URI 를 만들어 Restriction/subClassOf
    # 참조를 무효화시키는 문제가 실측 관찰됨. Jury 가 3라운드 연속 지적하고
    # production_ready=false 의 주원인이 되었다. 예시는 domain_config prefix/URI 로 렌더.
    _ns_rule = (
        "\n# 네임스페이스 사용 규칙 (**매우 중요** — 위반 시 T-Box 전체 무효)\n"
        "**반드시 PN(Prefixed Name) 형식만 사용**. full IRI 형식 금지.\n\n"
        "## 올바른 예 ✅\n"
        "```turtle\n"
        f"{_p}:ClassA rdf:type owl:Class .\n"
        f"{_p}:objPropertyX rdfs:domain {_p}:ClassA ;\n"
        f"               rdfs:range {_p}:ClassB .\n"
        f"{_p}:ClassC rdfs:subClassOf owl:Thing .\n"
        "```\n\n"
        "## 금지 예 ❌ (이중 접두사 — 같은 엔티티를 다른 URI 로 만들어버림)\n"
        "```turtle\n"
        f"# 아래 3줄은 모두 잘못됨. {_p}:X 와 충돌하는 별개 URI 생성:\n"
        f"<{DOMAIN_NS}{_p}:ClassA> rdf:type owl:Class .\n"
        f"<{DOMAIN_NS}{_p}:objPropertyX> rdfs:domain {_p}:ClassA .\n"
        f"<{DOMAIN_NS}owl:Thing> rdf:type owl:Class .\n"
        "```\n\n"
        "## 추가 금지\n"
        f"- `<#{_p}:X>` 같은 해시/상대 IRI 금지 (PN 형식 쓰세요)\n"
        f"- 같은 클래스/프로퍼티를 두 개 URI 로 선언 금지 (예: {_p}:dataPropertyY 와\n"
        "  otherDataProperty 를 동일 트리플에 같이 등장시키지 말 것)\n"
        "- owl:Thing, owl:Class 등 표준 용어는 반드시 표준 prefix 사용 (owl:Thing)\n"
        f"  절대 {_p}:Thing 이나 `<...#owl:Thing>` 형태 금지\n\n"
        "## 자가 검증 체크 (출력 전 반드시 확인)\n"
        "TTL 에서 `<http://` 로 시작하는 subject/object 가 있다면 거의 확실히 잘못.\n"
        f"모든 {_p}: 엔티티는 `{_p}:LocalName` 형식이어야 함.\n"
    )
    # domain/range 스코프 규칙 — R28 (2026-05-04 회귀 대응).
    # Architect 가 IOF/BFO 상위 온톨로지 클래스를 rdfs:domain 또는 rdfs:range 로
    # 잘못 선언하는 문제 (실측: <ns>:objProperty rdfs:domain iof-core:MaterialArtifact).
    # IOF 클래스는 개념 정의 역할이지 domain/range 에 직접 쓰이면 시맨틱 딕셔너리가
    # "알려지지 않은 클래스" 로 오해하고 LLM NL→SPARQL 쿼리가 hallucinate.
    _domain_scope_rule = (
        f"\n# domain/range 스코프 규칙 (**매우 중요**)\n"
        f"**ObjectProperty / DatatypeProperty 의 `rdfs:domain` 과 `rdfs:range` 는\n"
        f"반드시 `{_p}:` 네임스페이스 클래스만 사용.** IOF/BFO/Core 같은 상위\n"
        f"온톨로지 클래스는 `rdfs:subClassOf` 매핑 전용 — domain/range 에는 금지.\n"
        "\n## 올바른 예 ✅\n"
        "```turtle\n"
        "# (a) 도메인 클래스가 IOF 상위 클래스의 subClassOf 인 것은 OK.\n"
        f"{_p}:ClassA rdfs:subClassOf iof-core:MaterialArtifact .\n"
        "\n"
        f"# (b) OP 의 domain/range 는 반드시 {_p}: 클래스.\n"
        f"{_p}:objPropertyX rdfs:domain {_p}:ClassA ;\n"
        f"                  rdfs:range  {_p}:ClassB .\n"
        "```\n\n"
        "## 금지 예 ❌\n"
        "```turtle\n"
        "# 아래는 잘못됨. IOF 클래스를 domain 으로 직접 쓰면 안 됨.\n"
        f"{_p}:objPropertyX rdfs:domain iof-core:MaterialArtifact ;\n"
        f"                  rdfs:range  {_p}:ClassB .\n"
        "\n"
        "# 아래도 잘못됨 — BFO/Core 를 domain/range 에 직접 노출.\n"
        f"{_p}:objPropertyY rdfs:domain bfo:Entity .\n"
        f"{_p}:objPropertyZ rdfs:range iof-core:MaterialArtifact .\n"
        "```\n\n"
        "## 자가 검증 체크 (출력 전 반드시 확인)\n"
        f"모든 ObjectProperty/DatatypeProperty 선언에서 `rdfs:domain` 과 `rdfs:range`\n"
        f"값이 `{_p}:` 로 시작하는지 확인. `iof-core:`, `iof-maint:`, `bfo:`,\n"
        f"`obo:` 등이 domain/range 에 등장하면 해당 위치에 `{_p}:` 서브클래스를\n"
        f"대신 사용하세요 (없으면 신규 {_p}: 클래스를 선언하고 IOF 매핑은 subClassOf).\n"
    )
    _fence_rule = (
        "\n# 출력 형식 (반드시 준수)\n"
        "1. 응답의 **첫 줄**: 삼중 백틱 + `turtle` (```turtle).\n"
        "2. 응답의 **마지막 줄**: 삼중 백틱 (```).\n"
        "3. 펜스 **바깥**에 어떤 텍스트도 쓰지 말 것 — 'I will analyze...',\n"
        "   'Here is the T-Box:', 'Let me think...' 같은 설명 금지.\n"
        "4. 설명이 필요하면 TTL 내부 `#` 주석으로만.\n"
        "5. 펜스 안에는 순수 TTL(prefix, owl:Class 등) 만.\n"
    )
    if is_first:
        _output_req = (
            "\n# 출력 요구사항\n"
            "- 첫 번째 청크이므로 @prefix 선언과 owl:Ontology 선언을 포함할 것\n"
            "- 이번 청크 테이블의 owl:Class, ObjectProperty, DatatypeProperty를 모두 TTL로 출력\n"
        )
    else:
        _output_req = (
            "\n# 출력 요구사항\n"
            "- @prefix 선언을 포함하지 말 것 (첫 번째 청크에서 이미 정의됨)\n"
            "- 이번 청크 테이블의 owl:Class, ObjectProperty, DatatypeProperty를 TTL로 출력\n"
            "- 이전 청크에서 이미 정의된 클래스/프로퍼티는 중복 정의하지 말 것\n"
        )
    # ``_output_req`` 는 **cached_prefix 에 두지 않는다.** 청크 0 만 @prefix /
    # owl:Ontology 를 내야 하므로 청크별로 내용이 갈리고, 그것이 prefix 에 있으면
    # 고유 prefix 가 2종이 되어 Bedrock prompt cache 가 한 번 더 미스한다 (실측:
    # 30자 차이 하나로 27KB prefix 전체가 다른 캐시 키가 됐다). 청크별 지시는
    # variable_prompt 소관이다.
    cached_prefix = (
        "다음은 온톨로지 T-Box 생성 지침입니다.\n\n"
        f"{template}\n\n---\n"
        f"{_fence_rule}"
        f"{_reserved_rule}"
        f"{_ns_rule}"
        f"{_domain_scope_rule}"
        f"{_hierarchy_rule}"
        f"{_op_name_hints_block}"
    )

    # ── variable_prompt (청크별 동적 부분) ────────────────────────────────
    variable_prompt = f"""{_output_req}
# 이번 청크 정보 (청크 {chunk_idx + 1}/{total_chunks})

## IOF 매핑 요약
{iof_summary}

## 이번 청크 테이블 (클래스 매핑)
{chunk_class_mapping}

## 이번 청크 테이블 스키마
```json
{chunk_tables_info}
```
{column_meta_block}

## 테이블 간 관계
{relationships_info or "없음"}
{baseline_context}{cq_context}"""

    return cached_prefix, variable_prompt


def _generate_chunk(chunk_idx, chunk_tables, total_chunks,
                    iof_summary, schema_info, relationships_info,
                    table_class_map, config, depth=0) -> str:
    if depth > 3:
        _emit_chunk_progress(
            event="chunk_fail", chunk_idx=chunk_idx, depth=depth,
            total_chunks=total_chunks, tables=len(chunk_tables),
            stop_reason=None, ttl_chars=None, elapsed_sec=None,
            reason="depth_exceeded",
        )
        raise ValueError(f"청크 분할 깊이 초과 (depth={depth}). 테이블 스키마가 너무 큽니다.")

    _chunk_start_t = time.monotonic()
    _emit_chunk_progress(
        event="chunk_start", chunk_idx=chunk_idx, depth=depth,
        total_chunks=total_chunks, tables=len(chunk_tables),
        stop_reason=None, ttl_chars=None, elapsed_sec=None,
    )

    cached_prefix, variable_prompt = _build_chunk_prompt(
        chunk_idx, chunk_tables, total_chunks,
        iof_summary, schema_info, relationships_info,
        table_class_map,
    )
    result = _invoke_bedrock(
        variable_prompt,
        max_tokens=config["max_tokens"],
        max_retries=config["max_retries"],
        cached_prefix=cached_prefix,
    )
    ttl_text = result["text"]
    _llm_elapsed = round(time.monotonic() - _chunk_start_t, 1)
    _emit_chunk_progress(
        event="chunk_llm_done", chunk_idx=chunk_idx, depth=depth,
        total_chunks=total_chunks, tables=len(chunk_tables),
        stop_reason=result.get("stop_reason"),
        ttl_chars=len(ttl_text), elapsed_sec=_llm_elapsed,
    )
    # 디버그 덤프: TBOX_DEBUG_DUMP_DIR 환경변수가 설정되면 chunk raw 응답을 파일로 남긴다.
    # 이후 파싱 실패가 발생하면 이 덤프로 어느 chunk/어느 지점이 잘렸는지 역추적 가능.
    _dump_dir = os.getenv("TBOX_DEBUG_DUMP_DIR")
    if _dump_dir:
        try:
            import uuid as _uuid
            os.makedirs(_dump_dir, exist_ok=True)
            # 같은 (chunk_idx, depth) 쌍에서 두 개 half 가 동시에 파일 쓰지 않도록 uuid suffix.
            _dump_path = os.path.join(
                _dump_dir,
                f"chunk_{chunk_idx:02d}_depth{depth}_stop-{result['stop_reason']}_{_uuid.uuid4().hex[:8]}.ttl",
            )
            with open(_dump_path, "w", encoding="utf-8") as _df:
                _df.write(ttl_text)
            logger.info("chunk raw 덤프: %s (stop=%s, len=%d, first_table=%s)",
                         _dump_path, result["stop_reason"], len(ttl_text),
                         chunk_tables[0].get("name", "?") if chunk_tables else "?")
        except Exception as _e:
            logger.warning("chunk 덤프 실패: %s", _e)

    # max_tokens 도달 시 반으로 분할 재시도
    if result["stop_reason"] == "max_tokens":
        mid = len(chunk_tables) // 2
        if mid == 0:
            # 단일 테이블이 설정 max_tokens 초과.
            #
            # 2026-06-22: 와이드 테이블 (392컬럼 등) 대응. 한 테이블의
            # 컬럼이 수백 개면 컬럼당 class-specific DatatypeProperty 선언만으로도
            # 출력이 24K 토큰을 넘어 48K 재시도가 40분+ 걸리고 결국 빈 TTL 로
            # 누락된다. 테이블 리스트 분할 (mid)은 단일 테이블에선 불가하므로,
            # **같은 테이블의 컬럼 리스트를 절반으로 나눠** 같은 chunk_idx 로 재귀
            # 생성한다. 같은 클래스에 대한 DP 선언을 두 호출이 분담 → 각 호출의
            # 출력이 작아져 빠르고 누락 없이 완성된다. 두 결과는 _merge_chunks
            # 와 동일하게 prefix/ontology 중복을 제거하고 이어 붙인다.
            _cols = chunk_tables[0].get("columns", [])
            if len(_cols) > 1 and depth < 3:
                col_mid = len(_cols) // 2
                _base_tbl = chunk_tables[0]
                _emit_chunk_progress(
                    event="chunk_split", chunk_idx=chunk_idx, depth=depth,
                    total_chunks=total_chunks, tables=1,
                    stop_reason="max_tokens", ttl_chars=len(ttl_text),
                    elapsed_sec=_llm_elapsed,
                    split_into=2, split_kind="columns",
                    columns=len(_cols),
                )
                logger.warning(
                    "chunk %d depth %d 단일 와이드 테이블 %s (%d컬럼) max_tokens 초과 "
                    "— 컬럼 %d/%d 로 분할 재귀",
                    chunk_idx, depth, _base_tbl.get("name", "?"),
                    len(_cols), col_mid, len(_cols) - col_mid,
                )
                first_half_tbl = {**_base_tbl, "columns": _cols[:col_mid]}
                second_half_tbl = {**_base_tbl, "columns": _cols[col_mid:]}
                first = _generate_chunk(
                    chunk_idx, [first_half_tbl], total_chunks,
                    iof_summary, schema_info, relationships_info,
                    table_class_map, config, depth=depth + 1,
                )
                second = _generate_chunk(
                    chunk_idx, [second_half_tbl], total_chunks,
                    iof_summary, schema_info, relationships_info,
                    table_class_map, config, depth=depth + 1,
                )
                # second 의 @prefix / owl:Ontology 선언 제거 후 병합 (테이블 분할과 동일).
                # 두 half 모두 같은 클래스 선언을 포함하므로 merge 후 owl:Class 가 중복
                # 선언되지만, rdflib 파싱 시 동일 트리플로 dedup 되고 상위 _merge_chunks
                # 의 redefinition 카운터가 이를 감지/보고한다 (오류 아님). 아래 공통
                # 파싱 검증 + chunk_done emit 으로 fall-through (테이블 분할 경로와 동일).
                ttl_text = first + "\n\n" + _strip_prefix_and_ontology_decl(second)
            else:
                # 컬럼이 1개뿐이거나 depth 한계 — 기존 폴백: Opus 4.7
                # 이 verbose 하게 TTL 을 써서 7컬럼 테이블도 35K chars / 9K+ 토큰 이
                # 나오는 케이스 있음. 파이프라인을 전체 실패시키기보단 max_tokens 를
                # 2배로 한 번만 더 호출해서 완성을 시도 (R28 회귀 실측: 16K→32K 재시도 후
                # 대부분 end_turn 반환). 그래도 터지면 empty 반환해 상위 merge 가 해당
                # 청크만 빠진 채 진행.
                #
                # 2026-05-10 실측 회귀: depth 가 2 이상이면 같은 테이블이 이미
                # 분할 재귀로 좁혀진 상태인데도 overflow → 48K 재시도 자체가
                # 10-15분씩 걸리고 결국 실패. 무한 루프 방지 위해 depth >= 2
                # 에서는 재시도 없이 즉시 빈 TTL 반환.
                #
                # 2026-05-12: 위 정책 완화. depth == 2 까지는 한 번 max_tokens ×2
                # 재시도 허용 (depth 3 에서는 포기). 이유: _merge_chunks prefix
                # 보존 버그 fix 와 함께 적용해 빈 TTL 발생 시 전체 파이프라인이
                # 깨지지 않도록 한 뒤, overflow 시에도 실제로 TTL 을 채울 기회를
                # 한 번 더 주기 위함. depth 3 에서는 여전히 빈 반환.
                if depth >= 3:
                    _emit_chunk_progress(
                        event="chunk_fail", chunk_idx=chunk_idx, depth=depth,
                        total_chunks=total_chunks, tables=len(chunk_tables),
                        stop_reason="max_tokens",
                        ttl_chars=len(ttl_text),
                        elapsed_sec=_llm_elapsed,
                        reason="single_table_overflow_deep_no_retry",
                    )
                    logger.error(
                        "chunk %d depth %d 단일 테이블 %s max_tokens 초과 "
                        "— depth>=3 에서 재시도 생략, 빈 TTL 반환",
                        chunk_idx, depth, chunk_tables[0]["name"],
                    )
                    return ""

                _retry_max = min(64000, config["max_tokens"] * 2)
                _emit_chunk_progress(
                    event="chunk_retry", chunk_idx=chunk_idx, depth=depth,
                    total_chunks=total_chunks, tables=len(chunk_tables),
                    stop_reason="max_tokens", ttl_chars=len(ttl_text),
                    elapsed_sec=_llm_elapsed,
                    reason="single_table_overflow",
                    retry_max_tokens=_retry_max,
                )
                logger.warning(
                    "chunk %d depth %d 단일 테이블 %s max_tokens 초과 — max_tokens %d 로 재시도",
                    chunk_idx, depth, chunk_tables[0]["name"], _retry_max,
                )
                retry_result = _invoke_bedrock(
                    variable_prompt,
                    max_tokens=_retry_max,
                    max_retries=config["max_retries"],
                    cached_prefix=cached_prefix,
                )
                if retry_result.get("stop_reason") != "max_tokens":
                    ttl_text = retry_result["text"]
                    _emit_chunk_progress(
                        event="chunk_llm_done", chunk_idx=chunk_idx, depth=depth,
                        total_chunks=total_chunks, tables=len(chunk_tables),
                        stop_reason=retry_result.get("stop_reason"),
                        ttl_chars=len(ttl_text),
                        elapsed_sec=round(time.monotonic() - _chunk_start_t, 1),
                    )
                    # 재시도 성공 — 분할 로직 스킵하고 아래 파싱 검증으로 진행.
                else:
                    _emit_chunk_progress(
                        event="chunk_fail", chunk_idx=chunk_idx, depth=depth,
                        total_chunks=total_chunks, tables=len(chunk_tables),
                        stop_reason="max_tokens",
                        ttl_chars=len(retry_result.get("text") or ""),
                        elapsed_sec=round(time.monotonic() - _chunk_start_t, 1),
                        reason="single_table_overflow_after_retry",
                    )
                    logger.error(
                        "chunk %d depth %d 단일 테이블 %s 재시도도 max_tokens 초과 — 빈 TTL 반환",
                        chunk_idx, depth, chunk_tables[0]["name"],
                    )
                    return ""
        else:
            # mid > 0 — 정상 분할 경로.
            _emit_chunk_progress(
                event="chunk_split", chunk_idx=chunk_idx, depth=depth,
                total_chunks=total_chunks, tables=len(chunk_tables),
                stop_reason="max_tokens", ttl_chars=len(ttl_text),
                elapsed_sec=_llm_elapsed,
                split_into=2,
            )
            first = _generate_chunk(
                chunk_idx, chunk_tables[:mid], total_chunks,
                iof_summary, schema_info, relationships_info,
                table_class_map, config, depth=depth + 1,
            )
            second = _generate_chunk(
                chunk_idx, chunk_tables[mid:], total_chunks,
                iof_summary, schema_info, relationships_info,
                table_class_map, config, depth=depth + 1,
            )
            # second 의 @prefix 선언과 owl:Ontology 선언 라인을 제거 — first 가 이미
            # prefix 를 가지고 있으므로 중간 중복은 병합 TTL 의 파싱 안정성을 해친다.
            # 안전을 위해 `@base`, `@prefix` 로 시작하는 라인과 `<...> a owl:Ontology`
            # 블록(세미콜론 체인 + 마침표로 끝)을 잘라낸다.
            second = _strip_prefix_and_ontology_decl(second)
            ttl_text = first + "\n\n" + second

    # fence 유무와 무관하게 extractor 를 통과시켜 prelude/trailing 설명 제거.
    ttl_text = _extract_ttl_from_markdown(ttl_text)

    # 각 chunk 가 단독으로 파싱되는지 검증. 후속 chunk 에는 @prefix 선언이
    # 없을 수 있으므로, 검증을 위해 최소 prefix 들을 임시로 prepend 한다.
    # 실패 시 LLM 응답이 잘렸거나 분할 재시도 병합이 깨진 것이므로, 해당
    # chunk 를 빈 문자열로 반환해 merge 시 오염시키지 않는다. 호출 측
    # correction loop 가 이 부재를 감지하고 재생성하도록 위임.
    _prefix_preamble = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
        f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
        f"@prefix iof-core: <{IOF_CORE}> .\n"
        f"@prefix iof-maint: <{IOF_MAINT}> .\n"
        f"@prefix iof-scro: <{IOF_SCRO}> .\n"
        "@prefix bfo: <http://purl.obolibrary.org/obo/BFO_> .\n"
        "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
    )
    _validate_target = ttl_text if ttl_text.lstrip().startswith("@prefix") else (_prefix_preamble + ttl_text)
    try:
        _vg = _new_graph()
        _vg.parse(data=_validate_target, format="turtle")
    except Exception as _vexc:
        logger.error(
            "chunk %d depth %d 파싱 실패 — 불완전 응답 의심: %s", chunk_idx, depth, _vexc,
        )
        # 재시도: depth 가 여유 있으면 같은 청크를 한 번 더 생성. 그렇지 않으면 빈 반환.
        if depth < 3:
            logger.info("chunk %d depth %d 재시도 (단일 청크 재요청)", chunk_idx, depth)
            _emit_chunk_progress(
                event="chunk_retry", chunk_idx=chunk_idx, depth=depth,
                total_chunks=total_chunks, tables=len(chunk_tables),
                stop_reason=None, ttl_chars=len(ttl_text),
                elapsed_sec=None,
                reason="parse_error",
            )
            retry_result = _invoke_bedrock(
                variable_prompt,
                max_tokens=config["max_tokens"],
                max_retries=config["max_retries"],
                cached_prefix=cached_prefix,
            )
            ttl_text = _extract_ttl_from_markdown(retry_result["text"])
            _validate_target = ttl_text if ttl_text.lstrip().startswith("@prefix") else (_prefix_preamble + ttl_text)
            try:
                _vg2 = _new_graph()
                _vg2.parse(data=_validate_target, format="turtle")
            except Exception as _vexc2:
                logger.error(
                    "chunk %d depth %d 재시도도 파싱 실패, 빈 문자열 반환: %s",
                    chunk_idx, depth, _vexc2,
                )
                return ""
        else:
            return ""

    _emit_chunk_progress(
        event="chunk_done", chunk_idx=chunk_idx, depth=depth,
        total_chunks=total_chunks, tables=len(chunk_tables),
        stop_reason=result.get("stop_reason") if result else None,
        ttl_chars=len(ttl_text),
        elapsed_sec=round(time.monotonic() - _chunk_start_t, 1),
    )
    return ttl_text


def _merge_chunks(chunk_ttls: list) -> tuple:
    """청크 TTL을 병합하고, 클래스 중복 정의를 감지한다.

    Returns:
        (merged_ttl, merge_stats) 튜플.
        merge_stats = {
            "chunks_merged": int,
            "redefinitions": [{"class": str, "count": int}],
            "redefinition_count": int,
        }
    """
    if not chunk_ttls:
        return "", {"chunks_merged": 0, "redefinitions": [], "redefinition_count": 0}

    # 2026-05-12 Bug fix: chunks[0] 가 빈 문자열 (depth>=2 overflow skip 또는 파싱
    # 실패) 이면 merged = "" 로 시작해 이후 chunks 의 prefix 선언도 제거되어
    # 결과 TTL 에 @prefix 선언 0개. rdflib 파싱이 "<ns>: Prefix not bound" 로
    # 실패. 해결: 빈 문자열이 아닌 첫 청크를 seed 로 선택. 전부 비어 있으면
    # 최소 prefix preamble 을 명시 주입.
    _prefix_preamble = (
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
        f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
        f"@prefix iof-core: <{IOF_CORE}> .\n"
        f"@prefix iof-maint: <{IOF_MAINT}> .\n"
        f"@prefix iof-scro: <{IOF_SCRO}> .\n"
        "@prefix bfo: <http://purl.obolibrary.org/obo/BFO_> .\n"
        "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
    )
    # seed = 최초의 비어 있지 않은 청크 (또는 prefix preamble)
    seed_idx = next(
        (i for i, c in enumerate(chunk_ttls) if c and c.strip()),
        -1,
    )
    if seed_idx == -1:
        logger.warning("_merge_chunks: 모든 chunk 가 비어 있음 — prefix preamble 로 seed")
        merged = _prefix_preamble
        remaining = []
    else:
        seed = chunk_ttls[seed_idx]
        # seed 에 @prefix 선언이 없으면 preamble 을 prepend (방어선)
        if "@prefix " not in seed:
            logger.warning(
                "_merge_chunks: seed chunk(idx=%d) 에 @prefix 선언 없음 — preamble prepend",
                seed_idx,
            )
            merged = _prefix_preamble + "\n" + seed
        else:
            merged = seed
        remaining = [c for i, c in enumerate(chunk_ttls) if i != seed_idx]

    for chunk in remaining:
        if not chunk or not chunk.strip():
            continue
        lines = chunk.split("\n")
        non_prefix = [ln for ln in lines if not ln.strip().startswith("@prefix")]
        merged += "\n\n" + "\n".join(non_prefix)

    # 클래스 중복 정의 감지 — rdflib로 파싱 후 owl:Class 선언 카운트
    redefinitions = []
    try:
        g = _new_graph()
        g.parse(data=merged, format="turtle")

        class_decl_counts = Counter()
        for cls in g.subjects(RDF.type, OWL.Class):
            class_decl_counts[str(cls)] += 1

        for cls_uri, count in class_decl_counts.items():
            if count > 1:
                local = _local_name(cls_uri)
                logger.warning(
                    "Chunk merge: class %s defined %d times (redefinition)", local, count,
                )
                redefinitions.append({"class": local, "count": count})
    except Exception as e:
        logger.warning("Chunk merge: failed to parse merged TTL for redefinition check: %s", e)

    merge_stats = {
        "chunks_merged": len(chunk_ttls),
        "redefinitions": redefinitions,
        "redefinition_count": len(redefinitions),
    }

    return merged, merge_stats


# ── 교정 루프 ─────────────────────────────────────


def _deterministic_fix(ttl: str, violations: list) -> tuple:
    """rdflib 그래프 조작으로 기계적 위반 수정."""
    g = _new_graph()
    g.parse(data=ttl, format="turtle")
    fixes = []

    for v in violations:
        msg = v.get("message", "").lower()
        focus = v.get("focusNode", "")
        if not focus:
            continue
        focus_uri = URIRef(focus) if focus.startswith("http") else DOMAIN_NS_OBJ[focus]

        if "domain" in msg:
            g.add((focus_uri, RDFS.domain, OWL.Thing))
            fixes.append(f"added domain owl:Thing to {focus}")
        elif "label" in msg or "레이블" in msg:
            local = _local_name(focus)
            if "@en" in msg:
                g.add((focus_uri, RDFS.label, Literal(local, lang="en")))
                fixes.append(f"added label @en to {focus}")
            elif "@ko" in msg:
                g.add((focus_uri, RDFS.label, Literal(local, lang="ko")))
                fixes.append(f"added label @ko to {focus}")
            else:
                g.add((focus_uri, RDFS.label, Literal(local, lang="ko")))
                fixes.append(f"added label @ko to {focus}")
        elif "range" in msg:
            g.add((focus_uri, RDFS.range, XSD.string))
            fixes.append(f"added range xsd:string to {focus}")
        elif "comment" in msg or "코멘트" in msg:
            labels = list(g.objects(focus_uri, RDFS.label))
            text = str(labels[0]) if labels else _local_name(focus)
            g.add((focus_uri, RDFS.comment, Literal(text, lang="ko")))
            fixes.append(f"added comment @ko to {focus}")

    return g.serialize(format="turtle"), fixes


def _llm_correct(ttl: str, violations: list, config: dict) -> str:
    """Bedrock LLM으로 SHACL 위반 교정.

    **중요**: correction LLM 이 ``max_tokens`` 에 도달하거나 잘린 TTL 을
    내놓으면 이후 `_strip_reserved_entities` / `_run_correction_loop` 가
    모두 BadSyntax 로 터진다. 이전 회귀 (2026-04-30): chunk generation 은
    멀쩡했는데 correction LLM 응답이 잘려 "<ns>:... a owl:Object^Property"
    같은 토큰 경계 오류가 유입됨. 응답이 잘렸거나 rdflib 파싱에 실패하면
    원본 ``ttl`` 을 그대로 반환해 교정을 포기한다 (상위 loop 가 다음
    라운드에서 재시도).
    """
    prompt_path = os.path.join(_PROMPTS_DIR, "T-Box-Correction-Prompt.md")
    with open(prompt_path, encoding="utf-8") as f:
        template = f.read()

    violations_text = "\n".join(
        f"- [{v.get('severity', 'Violation')}] {v.get('focusNode', 'unknown')}: {v.get('message', '')}"
        for v in violations
    )
    prompt = template.replace("{violations}", violations_text).replace("{original_ttl}", ttl)
    result = _invoke_bedrock(prompt, max_tokens=config["max_tokens"], max_retries=config["max_retries"])
    corrected = result["text"]
    corrected = _extract_ttl_from_markdown(corrected)

    # 응답이 잘렸는지 확인. stop_reason 이 max_tokens 면 중간에 토큰 경계가
    # 깨진 구조를 가질 수 있다.
    if result.get("stop_reason") == "max_tokens":
        logger.warning(
            "LLM 교정 응답이 max_tokens 에 도달해 잘림 — 교정 포기, 원본 TTL 반환 "
            "(violations %d건은 다음 라운드로 이월)", len(violations),
        )
        return ttl

    # rdflib 로 파싱 시도. 실패하면 잘림/구조 손상으로 간주.
    try:
        _g = _new_graph()
        _g.parse(data=corrected, format="turtle")
    except Exception as _exc:
        logger.warning("LLM 교정 응답 파싱 실패 — 교정 포기, 원본 TTL 유지: %s", _exc)
        return ttl

    return corrected


def _strip_reserved_entities(ttl: str) -> tuple[str, dict]:
    """도메인 네임스페이스 reserved local name (type/description/... ) 을
    Class/Property 로 선언한 경우 그 엔티티와 관련 트리플을 모두 제거한다.

    LLM 이 네이밍 금지 규칙을 어겼을 때의 런타임 방어선. owlready2/HermiT
    가 Python 내장 'type' 과 충돌해 TypeError 를 내는 것을 사전 차단.

    Returns:
        (수정된 TTL, {"removed_count": int, "samples": [str, ...]})
    """
    from domain.namespaces import DOMAIN_NS
    from domain.uri_conventions import is_reserved_local

    g = _new_graph()
    g.parse(data=ttl, format="turtle")

    reserved_uris: list[URIRef] = []
    for s in g.subjects(RDF.type, None):
        if not isinstance(s, URIRef):
            continue
        s_str = str(s)
        if not s_str.startswith(DOMAIN_NS):
            continue
        local = s_str[len(DOMAIN_NS):]
        if is_reserved_local(local):
            reserved_uris.append(s)

    samples: list[str] = [str(u) for u in reserved_uris[:10]]
    removed = 0
    for u in reserved_uris:
        for _, p, o in list(g.triples((u, None, None))):
            g.remove((u, p, o))
            removed += 1
        for s, p, _ in list(g.triples((None, None, u))):
            g.remove((s, p, u))
            removed += 1

    if removed == 0:
        return ttl, {"removed_count": 0, "samples": []}

    from domain.tbox_utils import fast_serialize_turtle
    return fast_serialize_turtle(g), {
        "removed_count": removed,
        "reserved_entities": [str(u) for u in reserved_uris],
        "samples": samples,
    }


def _run_correction_loop(ttl: str, config: dict) -> tuple:
    """SHACL 검증 → 결정론적 수정 → LLM 교정."""
    from tools.validation import check_quality_rules

    result_json = check_quality_rules(ttl)
    result = json.loads(result_json)

    if result.get("error"):
        return ttl, {"error": result["error"]}
    if result["issues_count"] == 0:
        return ttl, {"shacl_violations_initial": 0, "corrections": []}

    corrections = []
    issues = result["issues"]

    # 결정론적 수정
    det_issues = [i for i in issues if i["severity"] in ("high", "warning")]
    if det_issues:
        # 위반을 focusNode/message 형식으로 변환
        violations = [{"focusNode": i.get("message", "").split(" ")[0], "message": i["message"]} for i in det_issues]
        ttl, fixes = _deterministic_fix(ttl, violations)
        corrections.extend(fixes)

    # LLM 교정 (최대 2라운드). LLM 이 TTL 대신 자연어를 반환해 재파싱 실패
    # (check_quality_rules 가 {"error": ..., success=False} 반환) 하면 loop 탈출.
    #
    # 조기 종료 조건 (R28 최적화):
    # - LLM 교정이 동일 TTL 반환 시 → stall 로 간주하고 즉시 break
    # - critical 이슈 수가 감소하지 않으면 → 다음 라운드도 무의미하므로 break
    # 이전엔 max_tokens 로 폴백된 경우도 round 카운터만 소모하고 끝까지 돌아
    # 80분 실행의 약 10~15분을 여기서 낭비했음.
    prev_critical_count: int | None = None
    for round_num in range(config.get("max_correction_rounds", 2)):
        result = json.loads(check_quality_rules(ttl))
        if result.get("error") or "issues_count" not in result:
            corrections.append(
                f"LLM correction round {round_num + 1} aborted: "
                f"check_quality_rules error ({result.get('error','missing issues_count')})",
            )
            break
        if result["issues_count"] == 0:
            break
        critical = [i for i in result["issues"] if i["severity"] == "critical"]
        if not critical:
            break
        # Stall 검사: 직전 라운드와 critical 개수 동일하면 더 안 줄 것으로 판정.
        if prev_critical_count is not None and len(critical) >= prev_critical_count:
            corrections.append(
                f"LLM correction round {round_num + 1} skipped: stall "
                f"(critical={len(critical)} unchanged from prev round)",
            )
            break
        prev_critical_count = len(critical)
        try:
            violations = [{"focusNode": "", "message": i["message"], "severity": "critical"} for i in critical]
            ttl_before = ttl
            ttl = _llm_correct(ttl, violations, config)
            # LLM 이 동일 TTL 반환 시 (max_tokens 폴백 / 파싱 실패 등) → 추가 라운드 무의미.
            if ttl == ttl_before:
                corrections.append(
                    f"LLM correction round {round_num + 1} no-op (unchanged TTL); stop",
                )
                break
            corrections.append(f"LLM correction round {round_num + 1}")
        except Exception as e:
            corrections.append(f"LLM correction failed: {e}")
            break

    final = json.loads(check_quality_rules(ttl))
    return ttl, {
        "shacl_violations_initial": result.get("issues_count", 0),
        "corrections": corrections,
        "remaining_issues": final.get("issues_count"),
        "finalize_error": final.get("error"),
    }


# ── 소스 데이터 로딩 ─────────────────────────────────


def _load_source_data():
    """CSV 헤더에서 스키마를 추출하고, IOF 매핑을 로드한다."""
    import csv

    # CSV 파일에서 테이블 스키마 추출
    csv_files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
    if not csv_files:
        raise ValueError(f"CSV 파일이 없습니다: {SOURCE_RAWDATA_DIR}/*.csv")

    # 표준항목 딕셔너리: 컬럼코드 → 한글명/값타입/단위. LLM 이 DP 이름을
    # 추측하지 않고 권위 있는 의미를 참조하도록 컬럼별 메타를 함께 싣는다.
    from domain import column_dictionary

    # 내용이 동일한 CSV 사본은 하나만 Architect 에 넘긴다 — 그러지 않으면 같은
    # 실체에 두 벌의 클래스/DP 가 생기고 그 둘을 잇는 성립 불가능한 OP 까지
    # 만들어진다. 상세 배경은 ``dedupe_identical_csvs`` docstring 참조.
    from tools.abox_generation import _load_table_class_mapping
    from tools.common import dedupe_identical_csvs
    try:
        preferred = set(_load_table_class_mapping())
    except Exception:  # noqa: BLE001 — 매핑 없어도 dedup 자체는 동작
        preferred = set()
    csv_files, _ = dedupe_identical_csvs(csv_files, preferred_tables=preferred)

    schema_info = {"tables": {}}
    for csv_path in csv_files:
        table_name = os.path.basename(csv_path).replace(".csv", "")
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader, None)
            if header:
                table_entry = {"columns": header}
                # column_meta: {COLUMN_CODE: "한글명 [type, unit]"} — 딕셔너리에
                # 매칭되는 컬럼만. 없으면 생략(LLM 은 컬럼명만으로 추론).
                col_meta: dict[str, str] = {}
                for col in header:
                    entry = column_dictionary.lookup(col)
                    if not entry:
                        continue
                    kor = entry.get("korean_name") or ""
                    vtp = entry.get("value_type") or ""
                    unit = entry.get("unit") or ""
                    desc = kor
                    extra = ", ".join(x for x in (vtp, unit) if x)
                    if extra:
                        desc = f"{kor} [{extra}]" if kor else f"[{extra}]"
                    if desc:
                        col_meta[col] = desc
                if col_meta:
                    table_entry["column_meta"] = col_meta
                schema_info["tables"][table_name] = table_entry
        except Exception:
            continue

    # IOF 매핑
    iof_path = os.path.join(SOURCE_MAPPING_DIR, "iof_masterdata_mapping.json")
    try:
        with open(iof_path, encoding="utf-8") as f:
            iof_data = json.load(f)
        iof_summary = json.dumps(iof_data, ensure_ascii=False)[:4000]  # 토큰 절약
    except Exception:
        iof_summary = "(IOF 매핑 파일 없음)"

    return schema_info, iof_summary, ""


# ── MCP 도구 ──────────────────────────────────────


def generate_tbox(tables: str = "", *, save_to_tbox_path: bool = True) -> str:
    """T-Box(OWL 스키마)를 생성한다. Bedrock LLM으로 테이블 스키마에서 OWL 클래스/프로퍼티를 생성.

    예상 소요시간: 3~5분 (40테이블 기준)

    로컬 파일시스템의 스키마·IOF 매핑을 입력으로 사용하며, 적응형 청킹·교정 루프를 수행한다.
    생성된 TTL은 반환만 하며 자동 업로드하지 않는다.

    Args:
        tables: 생성할 테이블명 (쉼표 구분). 비어있으면 전체 CSV 파일 대상.
        save_to_tbox_path: True(기본)면 결과를 ``TBOX_PATH`` 에 저장. False 면
            **저장하지 않고** 응답의 ``ttl`` 필드로만 돌려준다. S2 multi-agent
            토론처럼 "초안을 만들되 기존 산출물은 건드리지 않아야" 하는 호출자용.
            ⚠️ keyword-only 는 Python 호출 지점만 제약하므로 이 파라미터도 MCP
            스키마에 그대로 실린다 (``{"type": "boolean", "default": true}``).
            공개 계약에서 빼려면 시그니처에서 제거하고 내부 함수로 분리해야 한다.
    """
    try:
        pipeline_start = time.monotonic()
        timing = {}

        config = _load_config()
        table_class_map = _load_table_class_mapping()

        # 로컬 소스 데이터 로드
        t0 = time.monotonic()
        schema_info, iof_summary, relationships_info = _load_source_data()
        timing["load_data"] = round(time.monotonic() - t0, 1)

        # 테이블 목록 추출
        if tables:
            filter_names = {t.strip() for t in tables.split(",")}
        else:
            filter_names = None

        all_tables = []
        schema_tables = schema_info.get("tables", schema_info) if isinstance(schema_info, dict) else {}
        for table_name, table_data in schema_tables.items():
            if filter_names and table_name not in filter_names:
                continue
            columns = table_data.get("columns", []) if isinstance(table_data, dict) else []
            entry = {"name": table_name, "columns": columns}
            if isinstance(table_data, dict) and table_data.get("column_meta"):
                entry["column_meta"] = table_data["column_meta"]
            all_tables.append(entry)

        if not all_tables:
            return error_response(f"테이블이 없습니다. CSV 파일을 확인하세요: {SOURCE_RAWDATA_DIR}", logger=logger)

        # 적응형 청킹
        chunks = _adaptive_chunking(all_tables, config)

        # 청크별 생성 (2개 이상이면 병렬)
        chunk_ttls = [None] * len(chunks)
        chunk_times = [0.0] * len(chunks)

        if len(chunks) == 1:
            start = time.monotonic()
            chunk_ttls[0] = _generate_chunk(
                0, chunks[0], 1,
                iof_summary, schema_info, relationships_info,
                table_class_map, config,
            )
            chunk_times[0] = round(time.monotonic() - start, 1)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _gen_chunk(idx, tables):
                s = time.monotonic()
                ttl = _generate_chunk(
                    idx, tables, len(chunks),
                    iof_summary, schema_info, relationships_info,
                    table_class_map, config,
                )
                return idx, ttl, round(time.monotonic() - s, 1)

            executor = ThreadPoolExecutor(max_workers=min(len(chunks), 4))
            try:
                futures = [executor.submit(_gen_chunk, i, ct) for i, ct in enumerate(chunks)]
                for future in as_completed(futures):
                    idx, ttl, elapsed = future.result()
                    chunk_ttls[idx] = ttl
                    chunk_times[idx] = elapsed
            except Exception:
                # Cancel pending futures and shut down worker threads
                for f in futures:
                    f.cancel()
                executor.shutdown(wait=False)
                raise
            else:
                executor.shutdown(wait=True)

        timing["chunk_generation"] = chunk_times

        # 병합
        merged_ttl, merge_stats = _merge_chunks(chunk_ttls)

        # 구문 검증
        try:
            g = _new_graph()
            g.parse(data=merged_ttl, format="turtle")
        except Exception as e:
            return json.dumps({
                "success": False,
                "error": f"생성된 TTL 구문 오류: {e}",
                "partial_ttl": merged_ttl[:2000],
                "hint": "Bedrock 응답이 잘린 경우 tables 파라미터로 범위를 줄여 재시도하세요.",
            }, ensure_ascii=False, indent=2)

        # 교정 루프
        t0 = time.monotonic()
        corrected_ttl, correction_report = _run_correction_loop(merged_ttl, config)
        timing["correction_loop"] = round(time.monotonic() - t0, 1)

        # LLM 이 네이밍 금지 규칙을 어기고 reserved local name (type /
        # description / location / severity 등) 을 Class/Property 로 출력하면
        # owlready2 + HermiT 가 파싱 단계에서 TypeError. 파이프라인 후반에서
        # 불가해한 에러로 터지기 전에 여기서 제거.
        corrected_ttl, _reserved_report = _strip_reserved_entities(corrected_ttl)
        if _reserved_report["removed_count"] > 0:
            logger.warning(
                "reserved local name 엔티티 %d건 자동 제거: %s",
                _reserved_report["removed_count"],
                _reserved_report["samples"],
            )
            correction_report["reserved_stripped"] = _reserved_report

        # 통계
        g = _new_graph()
        g.parse(data=corrected_ttl, format="turtle")
        classes = len(list(g.subjects(RDF.type, OWL.Class)))
        obj_p = len(list(g.subjects(RDF.type, OWL.ObjectProperty)))
        data_p = len(list(g.subjects(RDF.type, OWL.DatatypeProperty)))

        # 커버리지 리포트
        t0 = time.monotonic()
        coverage = _coverage_report(corrected_ttl, all_tables)
        timing["coverage_report"] = round(time.monotonic() - t0, 1)

        timing["total"] = round(time.monotonic() - pipeline_start, 1)

        # TBOX_PATH 저장. S2(multi-agent) 는 이 초안을 **토론 입력** 으로만 쓰므로
        # save_to_tbox_path=False 로 호출한다. 예전엔 무조건 덮어써서, 토론이
        # 시작되기도 전에 S3 후처리 산출물이 미검토 초안으로 교체됐다
        # (2026-08-14 실측: S3 5,486 트리플/OP 229 → 초안이 덮어쓴 뒤 저장 가드는
        # 자기 초안과만 비교해 손실을 볼 수 없었다).
        if save_to_tbox_path:
            atomic_write(TBOX_PATH, corrected_ttl)

            # 베이스라인이 없으면 최초 저장
            if not os.path.exists(TBOX_BASELINE_PATH):
                atomic_write(TBOX_BASELINE_PATH, corrected_ttl)
                logger.info("T-Box 베이스라인 저장: %s", TBOX_BASELINE_PATH)
        else:
            logger.info(
                "T-Box 초안 생성 완료 — 호출자 요청으로 %s 를 덮어쓰지 않았다 "
                "(초안은 응답 ttl 필드로 전달)", TBOX_PATH,
            )

        result = {
            "success": True,
            "saved_to": TBOX_PATH if save_to_tbox_path else None,
            "tbox_path_written": bool(save_to_tbox_path),
            "statistics": {
                "classes": classes,
                "object_properties": obj_p,
                "data_properties": data_p,
                "triples": len(g),
                "chunks": len(chunks),
            },
            "coverage": coverage,
            "merge": merge_stats,
            "correction": correction_report,
            "timing": timing,
        }

        ttl_size = len(corrected_ttl.encode("utf-8"))
        if ttl_size <= 500_000:
            result["ttl"] = corrected_ttl

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


# ── 증분 업데이트 ────────────────────────────────────


def _remove_table_triples(g: Graph, class_name: str) -> int:
    """그래프에서 특정 클래스 관련 트리플을 모두 제거한다.

    제거 대상:
    - 클래스 선언 (<ns>:ClassName a owl:Class ...)
    - 해당 클래스를 domain으로 가진 DatatypeProperty
    - 해당 클래스를 domain 또는 range로 가진 ObjectProperty
    """
    cls_uri = DOMAIN_NS_OBJ[class_name]
    removed = 0

    # 1. 클래스 선언 트리플
    for s, p, o in list(g.triples((cls_uri, None, None))):
        g.remove((s, p, o))
        removed += 1

    # 2. 이 클래스를 domain으로 가진 프로퍼티
    for prop in list(g.subjects(RDFS.domain, cls_uri)):
        for s, p, o in list(g.triples((prop, None, None))):
            g.remove((s, p, o))
            removed += 1

    return removed


def update_tbox_incremental(changed_tables: str) -> str:
    """변경된 테이블만 T-Box에서 재생성한다 (증분 업데이트).

    전체 재생성 대신 특정 테이블의 클래스/프로퍼티만 제거 후 재생성하여
    시간과 비용을 절약한다.

    Args:
        changed_tables: 변경된 테이블명 (쉼표 구분). 예: "Equipment_Master,Tag_Master"
    """
    try:
        if not changed_tables.strip():
            return error_response("변경된 테이블명을 지정하세요.", logger=logger)

        # 기존 T-Box 로드
        if not os.path.exists(TBOX_PATH):
            return error_response(f"기존 T-Box가 없습니다: {TBOX_PATH}", hint="generate_tbox로 먼저 전체 T-Box를 생성하세요.", logger=logger)

        with open(TBOX_PATH, encoding="utf-8") as f:
            existing_ttl = f.read()

        table_class_map = _load_table_class_mapping()
        config = _load_config()
        schema_info, iof_summary, relationships_info = _load_source_data()

        changed_names = [t.strip() for t in changed_tables.split(",")]

        # 1. 기존 그래프에서 변경 테이블 트리플 제거
        g = _new_graph()
        g.parse(data=existing_ttl, format="turtle")
        triples_before = len(g)

        removed_total = 0
        for table_name in changed_names:
            class_name = table_class_map.get(table_name, _table_name_to_class(table_name))
            removed = _remove_table_triples(g, class_name)
            removed_total += removed

        triples_after_removal = len(g)

        # 2. 변경 테이블만 추출
        changed_table_data = []
        schema_tables = schema_info.get("tables", schema_info) if isinstance(schema_info, dict) else {}
        for table_name in changed_names:
            table_data = schema_tables.get(table_name, {})
            columns = table_data.get("columns", []) if isinstance(table_data, dict) else []
            changed_table_data.append({"name": table_name, "columns": columns})

        if not changed_table_data:
            return error_response(f"변경 테이블의 CSV를 찾을 수 없습니다: {changed_names}", logger=logger)

        # 3. 변경 테이블만 LLM으로 재생성
        start = time.monotonic()
        new_ttl = _generate_chunk(
            0, changed_table_data, 1,
            iof_summary, schema_info, relationships_info,
            table_class_map, config,
        )
        gen_time = round(time.monotonic() - start, 1)

        # 4. 재생성된 TTL에서 prefix 제거 후 기존 그래프에 병합
        try:
            g_new = _new_graph()
            g_new.parse(data=new_ttl, format="turtle")
            for s, p, o in g_new:
                g.add((s, p, o))
        except Exception:
            # prefix 문제 시 수동 병합
            lines = new_ttl.split("\n")
            non_prefix = "\n".join(
                ln for ln in lines if not ln.strip().startswith("@prefix")
            )
            existing_serialized = g.serialize(format="turtle")
            merged = existing_serialized + "\n\n" + non_prefix
            g = _new_graph()
            g.parse(data=merged, format="turtle")

        triples_after_merge = len(g)

        # 5. 저장
        merged_ttl = g.serialize(format="turtle")
        atomic_write(TBOX_PATH, merged_ttl)

        return json.dumps({
            "success": True,
            "saved_to": TBOX_PATH,
            "changed_tables": changed_names,
            "triples_before": triples_before,
            "triples_removed": removed_total,
            "triples_after_removal": triples_after_removal,
            "triples_regenerated": triples_after_merge - triples_after_removal,
            "triples_final": triples_after_merge,
            "generation_time_seconds": gen_time,
            "hint": "improve_tbox_quality + 검증을 실행하세요.",
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)
