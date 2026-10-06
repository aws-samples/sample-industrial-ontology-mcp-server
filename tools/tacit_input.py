"""암묵지(Tacit Knowledge) 입력 가이드 도구.

암묵지는 CSV에 없지만 현장 운영자가 아는 도메인 지식 (공정 흐름, 설비-에너지원 매핑,
품질 규격, 고장 패턴 등)을 담는 TTL 파일이다. data/source/tacit/ 디렉토리에 저장되며
추론 시 T-Box + A-Box 와 함께 병합된다.

FULL_PIPELINE S5 단계 진입 시 폴더가 비어있으면 에이전트는 사용자에게 3가지 경로를
안내해야 한다:
  1) 자연어 입력 → LLM 이 TTL 로 변환 (add_tacit_from_natural_language)
  2) 데이터 기반 자동 생성 (generate_tacit_from_data)
  3) Skip (skip_tacit_knowledge) — 빈 placeholder 생성해 이후 재요청 방지

각 모드는 독립된 MCP 도구로 노출되어 에이전트가 사용자의 선택을 명시적으로 기록.
"""
from __future__ import annotations

import contextlib
import glob
import json
import logging
import os
import re

from config import SOURCE_RAWDATA_DIR, SOURCE_TACIT_DIR, TBOX_PATH
from domain.namespaces import DOMAIN_CONFIG, DOMAIN_INST_NS, DOMAIN_NS, NS_INST_PREFIX, NS_PREFIX
from tools.bedrock import invoke_bedrock_text
from tools.common import atomic_write, error_response, resolve_child_path

logger = logging.getLogger(__name__)


_SKIP_MARKER_PATH = os.path.join(SOURCE_TACIT_DIR, ".skipped")
_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _tacit_ttl_files() -> list[str]:
    """현재 저장된 암묵지 TTL 파일 경로 목록."""
    if not os.path.isdir(SOURCE_TACIT_DIR):
        return []
    return sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")))


def _is_skipped() -> bool:
    return os.path.exists(_SKIP_MARKER_PATH)


def _load_tbox_context(max_chars: int = 8000) -> str:
    """LLM 프롬프트용 T-Box 구조화 요약.

    기존에는 T-Box TTL 을 앞에서 8000 자 자르는 방식이었다. 이 경우 LLM 이
    각 DP 의 domain 을 놓치기 쉽고, 결과적으로 암묵지 인스턴스가 엉뚱한
    클래스에 속한 DP 를 사용 → OWL RL prp-dom 규칙이 상충되는 rdf:type 을
    추가 → AllDisjointClasses / Cardinality 위반 유발.

    이 함수는 T-Box 를 다음 구조로 정돈해 반환한다:
      - 클래스 목록 (최상위 클래스, 각 클래스 허용 DP/OP 목록)
      - ObjectProperty (name, domain, range)
      - DatatypeProperty (name, domain, range)
    """
    if not os.path.exists(TBOX_PATH):
        return "(T-Box 없음 — S2 단계 선행 필요)"

    try:
        from rdflib import Graph, URIRef
        from rdflib.namespace import OWL, RDF, RDFS

        from domain.namespaces import DOMAIN_NS
        g = Graph()
        g.parse(TBOX_PATH, format="turtle")
    except Exception as e:
        return f"(T-Box 파싱 실패: {e})"

    def _ln(u) -> str:
        s = str(u)
        if "#" in s:
            return s.split("#")[-1]
        if "/" in s:
            return s.split("/")[-1]
        return s

    classes: list[str] = []
    for c in g.subjects(RDF.type, OWL.Class):
        if isinstance(c, URIRef) and str(c).startswith(DOMAIN_NS):
            classes.append(_ln(c))
    classes = sorted(set(classes))

    # DP: (name, domain_class_name)
    dps: list[tuple[str, str, str]] = []
    for p in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not isinstance(p, URIRef) or not str(p).startswith(DOMAIN_NS):
            continue
        dom = g.value(p, RDFS.domain)
        rng = g.value(p, RDFS.range)
        dps.append((_ln(p), _ln(dom) if dom else "?", _ln(rng) if rng else "?"))
    dps.sort()

    # OP: (name, domain, range)
    ops: list[tuple[str, str, str]] = []
    for p in g.subjects(RDF.type, OWL.ObjectProperty):
        if not isinstance(p, URIRef) or not str(p).startswith(DOMAIN_NS):
            continue
        dom = g.value(p, RDFS.domain)
        rng = g.value(p, RDFS.range)
        ops.append((_ln(p), _ln(dom) if dom else "?", _ln(rng) if rng else "?"))
    ops.sort()

    # 클래스별 허용 DP/OP 그룹핑
    from collections import defaultdict
    dp_by_class: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, dom, rng in dps:
        dp_by_class[dom].append((name, rng))
    op_by_class: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for name, dom, rng in ops:
        op_by_class[dom].append((name, rng))

    lines: list[str] = []
    lines.append(f"## 전체 요약: 클래스 {len(classes)}개, OP {len(ops)}개, DP {len(dps)}개")
    lines.append("")
    lines.append("## 클래스 목록 (암묵지 인스턴스가 속할 수 있는 타입)")
    for c in classes:
        lines.append(f"- {NS_PREFIX}:{c}")
    lines.append("")
    lines.append("## 클래스별 허용 속성 (★ 매우 중요: 해당 클래스에 한정된 DP/OP만 사용해야 T-Box 일관성 유지)")
    for c in classes:
        d_list = dp_by_class.get(c, [])
        o_list = op_by_class.get(c, [])
        if not d_list and not o_list:
            continue
        lines.append(f"### {c}")
        if d_list:
            lines.append("  DP:")
            for n, rng in d_list[:20]:
                lines.append(f"    - {NS_PREFIX}:{n} (range: {rng})")
        if o_list:
            lines.append("  OP:")
            for n, rng in o_list[:20]:
                lines.append(f"    - {NS_PREFIX}:{n} → {rng}")
    lines.append("")
    lines.append("## 제약 (★ 준수 필수)")
    lines.append("- 인스턴스에 DP 를 붙일 때, 그 DP 의 domain 클래스와 일치하는 rdf:type 을 선언할 것.")
    lines.append(f"- 예: {NS_PREFIX}:equipmentType 의 domain 이 EquipmentMaster 이면, 해당 DP 를 쓰는 인스턴스는 EquipmentMaster 타입.")
    lines.append("- 반대로 FailurePattern 에 equipmentType 을 쓰면 OWL RL 이 FailurePattern → EquipmentMaster 로 타입 확산시켜 AllDisjoint 위반.")
    lines.append("- 만약 어떤 DP 가 여러 클래스에 공통으로 필요하면 '새 DP' 를 만들지 말고 '주체 클래스를 domain 에 맞게 선택' 하거나 literal 을 OP 대상으로 재모델링.")

    result = "\n".join(lines)
    if len(result) <= max_chars:
        return result
    return result[:max_chars] + f"\n# ...(총 {len(result)}자 중 상위 {max_chars}자만)"


def _load_csv_summary(max_chars: int = 5000) -> str:
    """자동 생성 모드용 CSV 헤더 요약."""
    import csv as csv_mod
    files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
    if not files:
        return "(CSV 없음)"
    lines: list[str] = []
    remaining = max_chars
    for p in files:
        name = os.path.basename(p).replace(".csv", "")
        try:
            with open(p, encoding="utf-8") as f:
                header = next(csv_mod.reader(f), [])
                row_count = sum(1 for _ in csv_mod.reader(f))
        except OSError:
            continue
        line = f"- {name} ({row_count}행): {', '.join(header)}"
        if remaining - len(line) < 0:
            lines.append("- ...(이하 생략)")
            break
        lines.append(line)
        remaining -= len(line)
    return "\n".join(lines)


def _safe_filename(name: str) -> str:
    """사용자가 준 파일명을 TTL 저장용으로 정규화. 허용: 영숫자/_/-"""
    if not isinstance(name, str):
        raise ValueError("filename 은 문자열이어야 합니다.")
    cleaned = name.strip()
    if cleaned.endswith(".ttl"):
        cleaned = cleaned[:-4]
    if not _SAFE_FILENAME_RE.match(cleaned):
        raise ValueError(
            f"filename '{name}' 은 영숫자/언더스코어/하이픈만 허용됩니다."
        )
    return cleaned + ".ttl"


def _tacit_dest(safe_name: str) -> str:
    """정규화된 파일명을 SOURCE_TACIT_DIR 바로 아래 경로로 해석한다.

    같은 이름의 symlink 가 디렉터리 밖을 가리키면 거부한다 (ValueError).
    """
    return resolve_child_path(SOURCE_TACIT_DIR, safe_name, allowed_suffixes=(".ttl",))


def _validate_tbox_compliance(ttl: str) -> tuple[bool, list[dict]]:
    """암묵지 TTL 의 각 DP/OP 사용이 T-Box domain 과 일치하는지 확인.

    위반이 있으면 (False, [{"instance", "property", "instance_types",
    "expected_domain"}]) 반환. 없으면 (True, []).

    이 검증을 저장 전에 수행하여 OWL RL 추론이 암묵지 인스턴스에 엉뚱한
    클래스 타입을 전파하는 것을 원천 차단한다.
    """
    try:
        from rdflib import Graph, URIRef
        from rdflib.namespace import OWL, RDF, RDFS

        from domain.namespaces import DOMAIN_NS

        # 암묵지 + T-Box 병합 (class subsumption 도 고려)
        g_merge = Graph()
        g_merge.parse(TBOX_PATH, format="turtle")
        g_merge.parse(data=ttl, format="turtle")

        # 각 DP 의 domain 수집
        dp_domain: dict[URIRef, set[URIRef]] = {}
        for p in g_merge.subjects(RDF.type, OWL.DatatypeProperty):
            if not isinstance(p, URIRef) or not str(p).startswith(DOMAIN_NS):
                continue
            doms = {d for d in g_merge.objects(p, RDFS.domain) if isinstance(d, URIRef)}
            if doms:
                dp_domain[p] = doms

        # subclass 관계 전개 (A is-subclass-of B → A 가 B 의 domain 을 만족)
        def _ancestors(cls: URIRef) -> set[URIRef]:
            seen = {cls}
            stack = [cls]
            while stack:
                cur = stack.pop()
                for sup in g_merge.objects(cur, RDFS.subClassOf):
                    if isinstance(sup, URIRef) and sup not in seen:
                        seen.add(sup)
                        stack.append(sup)
            return seen

        # 암묵지에서 생성된 인스턴스 트리플 순회
        # (T-Box 와 병합됐으므로 DOMAIN_INST_NS 네임스페이스 인스턴스만 타겟)
        from domain.namespaces import DOMAIN_INST_NS
        violations: list[dict] = []
        # 각 (subject) 의 rdf:type 캐시
        subj_types: dict[URIRef, set[URIRef]] = {}
        for s in g_merge.subjects():
            if not isinstance(s, URIRef) or not str(s).startswith(DOMAIN_INST_NS):
                continue
            if s in subj_types:
                continue
            types = {t for t in g_merge.objects(s, RDF.type) if isinstance(t, URIRef)}
            # 자기 타입 + 그 조상 포함 (OWL RL prp-dom 이 기대하는 범위)
            expanded: set[URIRef] = set()
            for t in types:
                expanded |= _ancestors(t)
            subj_types[s] = expanded

        # 암묵지 TTL 에서 실제로 "지금 추가된" DP 사용을 검사
        g_tacit_only = Graph()
        g_tacit_only.parse(data=ttl, format="turtle")
        for s, p, _o in g_tacit_only:
            if not isinstance(s, URIRef) or not str(s).startswith(DOMAIN_INST_NS):
                continue
            if not isinstance(p, URIRef) or p not in dp_domain:
                continue
            allowed_doms = dp_domain[p]
            inst_types = subj_types.get(s, set())
            if not (allowed_doms & inst_types):
                violations.append({
                    "instance": str(s).split("#")[-1],
                    "property": str(p).split("#")[-1],
                    "instance_types": sorted(str(t).split("#")[-1] for t in inst_types
                                              if str(t).startswith(DOMAIN_NS)),
                    "expected_domain": sorted(str(d).split("#")[-1] for d in allowed_doms),
                })
        return len(violations) == 0, violations
    except Exception as e:
        logger.debug("_validate_tbox_compliance 실패, 스킵: %s", e)
        return True, []  # 실패 시 pass-through (기존 동작 유지)


def _validate_ttl_syntax(ttl: str) -> tuple[bool, str]:
    """rdflib 파싱으로 TTL 구문 검증. 성공 여부 + 에러 메시지."""
    try:
        from domain.tbox_utils import _new_graph
        g = _new_graph()
        g.parse(data=ttl, format="turtle")
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _decode_embedded_byte_literals(text: str) -> str:
    """LLM 응답에 섞여 나오는 `b'...'` / `b"..."` Python bytes literal 을 디코드.

    Sonnet 4.6 이 간혹 한국어나 유니코드 문자열을 `b'\\xec\\x97\\xb0...'` 같은
    bytes literal repr 로 출력하는 병리 현상이 있다. 이 경우 TTL 파서는
    quote 기대 위치에서 BadSyntax 를 낸다. literal 을 UTF-8 로 디코드해 정상
    문자열로 복원한다. literal 이 없으면 원본 반환.
    """
    def _sub(m: re.Match) -> str:
        body = m.group(1)
        # 실제 bytes literal 처럼 eval 하는 대신 escape 디코드만 수행 (안전)
        try:
            return body.encode("latin-1", errors="replace").decode("unicode_escape").encode("latin-1", errors="replace").decode("utf-8", errors="replace")
        except Exception:
            return body
    # b'...' 또는 b"..."  (quote 안의 문자는 non-greedy)
    pattern = re.compile(r"b['\"]((?:\\.|[^'\"\\])*)['\"]")
    return pattern.sub(_sub, text)


def _extract_ttl_from_response(raw: str) -> str:
    """LLM 응답에서 Turtle 본문만 추출.

    표준 경로: ``` ```turtle\n...\n``` ``` 코드블록 닫힘까지 매칭.
    Fallback 1: max_tokens 로 잘려 닫는 ``` 가 없으면, 여는 ``` 이후 끝까지 추출.
    Fallback 2: 첫 줄이 ``` 로 시작하면 그 줄 삭제하고 나머지 반환
                (마지막 ``` 도 있으면 잘라냄).
    Fallback 3: 모델이 preamble ("I'll analyze...") 과 fenced block 을 섞어 냈을 때
                첫 `@prefix`/`@base`/`PREFIX`/`BASE` 위치부터 TTL 로 간주 (R16 과 동일 전략).
    Sanitize: `b'...'` bytes-literal 이 섞여 있으면 UTF-8 로 복원.
    """
    ttl = _decode_embedded_byte_literals(raw.strip())
    # 1) 표준 매칭 — 가장 긴 코드 블록을 우선 (preamble 안에 또다른 ``` 가 있을 수도 있음)
    blocks = re.findall(r"```(?:turtle|ttl)?\s*\n?(.*?)```", ttl, re.DOTALL)
    if blocks:
        # TTL 시그니처(@prefix 등) 를 포함한 가장 긴 블록 선택
        ttl_blocks = [b for b in blocks if re.search(r"@prefix|@base|PREFIX\s|BASE\s", b, re.IGNORECASE)]
        chosen = max(ttl_blocks or blocks, key=len)
        return chosen.strip()
    # 2) 여는 ``` 만 있는 경우 — 그 이후 전부
    m_open = re.search(r"```(?:turtle|ttl)?\s*\n", ttl)
    if m_open:
        body = ttl[m_open.end():]
        # 혹시 다른 ``` 가 있으면 거기까지
        close_idx = body.find("```")
        if close_idx != -1:
            body = body[:close_idx]
        return body.strip()
    # 3) TTL 시그니처 기반 fallback (preamble 만 있고 코드펜스 없는 케이스)
    m_prefix = re.search(r"(?m)^\s*(@prefix|@base|PREFIX\s|BASE\s)", ttl)
    if m_prefix:
        return ttl[m_prefix.start():].strip()
    # 4) 원본이 TTL 같으면 그대로
    return ttl


def _ensure_tacit_dir() -> None:
    os.makedirs(SOURCE_TACIT_DIR, exist_ok=True)


def _clear_skip_marker() -> None:
    """실제 암묵지를 추가하면 skip 마커 제거."""
    if os.path.exists(_SKIP_MARKER_PATH):
        with contextlib.suppress(OSError):
            os.remove(_SKIP_MARKER_PATH)


def _input_guide() -> str:
    return (
        "암묵지는 CSV에 없지만 현장 운영자가 아는 도메인 지식입니다.\n"
        "예) 공정 흐름(고로→제강→연주→압연), 설비-에너지원 매핑, 품질 규격 임계값,\n"
        "    고장 패턴 규칙 등.\n"
        "\n"
        "선택지 4가지 (추천 순서):\n"
        "  (a) 자연어로 알려주기 → add_tacit_from_natural_language(filename, text)\n"
        "      SME 검증된 지식. LLM 이 자연어를 TTL 로 번역만 하므로 신뢰도 높음.\n"
        "      예) text='고로 공정 후 제강 공정이 이어지고, 제강 후 연주 공정이 진행됨'\n"
        "  (b) 규칙 기반 생성 → generate_tacit_from_rules()\n"
        "      rules/domain/tacit_rules.json 의 결정적 규칙 (rotation / number_match /\n"
        "      simple_join / via_mapping_chain) 으로 LLM 없이 TTL 생성. 재현 가능.\n"
        "      이미 SME 가 한 번 검증한 패턴을 파이프라인에 박제할 때 사용.\n"
        "  (c) CSV+LLM 부트스트랩 → generate_tacit_from_data(filename, focus)\n"
        "      SME 도 규칙도 없는 초기 단계 전용. 결과는 가설이므로 반드시 검토\n"
        "      후 (a) 또는 (b) 로 승격 권장. 재실행 시 출력이 바뀔 수 있음.\n"
        "  (d) Skip → skip_tacit_knowledge() — 암묵지 없이 진행. 재질문 없음.\n"
        "      나중에 (a)/(b)/(c) 호출 시 skip 마커 자동 제거."
    )


# ── MCP 도구 ───────────────────────────────────────────


def check_tacit_exist() -> str:
    """암묵지 폴더(data/source/tacit/) 상태를 반환.

    FULL_PIPELINE S5 진입 시 에이전트가 먼저 호출해야 하는 경량 도구.
    없을 때는 input_guide 로 사용자에게 4가지 선택지를 안내
    (자연어 / 규칙 기반 / CSV+LLM 부트스트랩 / skip).

    Returns:
        {exists, count, files, skipped, input_guide?}
    """
    try:
        ttl_files = _tacit_ttl_files()
        skipped = _is_skipped()
        result = {
            "success": True,
            "exists": len(ttl_files) > 0,
            "count": len(ttl_files),
            "skipped": skipped,
            "files": [os.path.basename(p) for p in ttl_files],
            "path": SOURCE_TACIT_DIR,
        }
        # 파일도 없고 skip 마커도 없을 때만 가이드 포함
        if not ttl_files and not skipped:
            result["input_guide"] = _input_guide()
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def skip_tacit_knowledge() -> str:
    """암묵지 추가를 명시적으로 건너뛴다. 빈 .skipped 마커를 생성.

    이후 check_tacit_exist 는 skipped=True 를 반환하여 파이프라인이 재질문하지 않는다.
    add_tacit_from_natural_language / generate_tacit_from_data 를 나중에 호출하면
    마커는 자동 제거된다.
    """
    try:
        _ensure_tacit_dir()
        with open(_SKIP_MARKER_PATH, "w", encoding="utf-8") as f:
            f.write("tacit knowledge skipped by user\n")
        logger.info("암묵지 skip 마커 생성: %s", _SKIP_MARKER_PATH)
        return json.dumps({
            "success": True,
            "skipped": True,
            "marker_path": _SKIP_MARKER_PATH,
            "hint": "나중에 추가하려면 add_tacit_from_natural_language 또는 "
                    "generate_tacit_from_data 를 호출하세요. 마커는 자동 제거됩니다.",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def add_tacit_from_natural_language(
    filename: str,
    text: str,
    overwrite: bool = False,
) -> str:
    """사용자가 자연어로 설명한 암묵지를 LLM 이 TTL 로 변환해 저장한다.

    생성된 TTL 은 rdflib 로 구문 검증 후 저장. 실패하면 원본 자연어와 함께 반환.

    Args:
        filename: 저장할 파일명 (확장자 제외, 영숫자/_/-만). 예: "process_flow".
            data/source/tacit 바로 아래에 저장하며, 같은 이름의 symlink 가 밖을
            가리키면 거부한다.
        text: 자연어 설명. 여러 규칙을 한 번에 담아도 됨.
        overwrite: True 면 동일 파일명 덮어쓰기. False 면 존재 시 에러.
    """
    try:
        if not (text or "").strip():
            return error_response(
                "text 가 비어있습니다.", hint=_input_guide(), logger=logger,
            )
        try:
            safe_name = _safe_filename(filename)
        except ValueError as ve:
            return error_response(str(ve), hint=_input_guide(), logger=logger)

        _ensure_tacit_dir()
        try:
            dest = _tacit_dest(safe_name)
        except ValueError as ve:
            return error_response(str(ve), hint=_input_guide(), logger=logger)
        if os.path.exists(dest) and not overwrite:
            return error_response(
                f"파일이 이미 존재합니다: {dest}",
                hint="overwrite=True 로 덮어쓰거나 다른 filename 을 지정하세요.",
                logger=logger,
            )

        domain = DOMAIN_CONFIG["domain"]
        DOMAIN_CONFIG["namespace"]
        tbox_summary = _load_tbox_context()

        prompt = f"""당신은 OWL 온톨로지 엔지니어입니다.
{domain['name_ko']} 도메인의 현장 운영자가 자연어로 설명한 **암묵지**를
{NS_PREFIX}/{NS_INST_PREFIX} 네임스페이스를 사용한 **RDF Turtle** 트리플로 변환하세요.

## 네임스페이스 (필수 prefix)
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix {NS_INST_PREFIX}: <{DOMAIN_INST_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

## T-Box 컨텍스트 (★ 클래스별 허용 속성 참조 — 이탈 금지)
{tbox_summary}

## 사용자 자연어 설명
{text}

## 변환 규칙 (★ 위반 시 OWL RL 추론에서 AllDisjointClasses/Cardinality 오염 유발)
1. 반드시 유효한 Turtle 구문으로 출력 (rdflib 파싱 가능).
2. 위 prefix 블록을 파일 상단에 포함.
3. T-Box 에 정의된 클래스/프로퍼티만 재사용. **새 클래스/DP/OP 정의 금지.**
4. **★ Domain 준수 (가장 중요)**: 인스턴스에 DatatypeProperty 를 붙일 때,
   해당 DP 의 domain 클래스와 인스턴스의 rdf:type 이 일치해야 한다.
   - 예: {NS_PREFIX}:equipmentType 의 domain 이 EquipmentMaster 라면,
     `{NS_INST_PREFIX}:XYZ a {NS_PREFIX}:FailurePattern ; {NS_PREFIX}:equipmentType "..."` 는 금지.
   - 대신 "triggerCondition", "failureCauseCategory" 같은 **FailurePattern domain 의 DP** 만 사용.
5. 인스턴스는 {NS_INST_PREFIX}:{{ClassName}}_{{ID}} 패턴.
6. rdfs:label (@ko, @en) 과 rdfs:comment (@ko) 로 의미를 주석화.
7. 설명이 여러 규칙이면 각각 별도 트리플로 분해.
8. 만약 적합한 DP/OP 가 T-Box 에 없으면, 해당 사실은 rdfs:comment 에 자연어로만 기록하고
   DP/OP 로 표현하지 말 것.

## 출력 형식
Turtle 코드 블록만 출력. 설명/주석 줄은 TTL 안의 # 로만 허용. 다른 텍스트 금지.

```turtle
<여기에 TTL>
```"""

        raw = invoke_bedrock_text(prompt, max_tokens=6000, temperature=0.2)
        ttl = _extract_ttl_from_response(raw)

        ok, err = _validate_ttl_syntax(ttl)
        if not ok:
            return error_response(
                f"생성된 TTL 구문 오류: {err}",
                hint="자연어 설명을 더 구체적으로 써주거나 overwrite=True 로 재시도하세요. "
                     "원본 자연어는 유지되며 파일은 저장되지 않았습니다.",
                logger=logger,
            )

        # T-Box domain 준수 검증 — 위반 시 저장 거부.
        # (LLM 이 FailurePattern 인스턴스에 EquipmentMaster domain DP 를 쓰는 등
        # 타입 오염 유발 패턴을 사전 차단.)
        compliant, viols = _validate_tbox_compliance(ttl)
        if not compliant:
            _preview_lines = [
                f"- {v['instance']}.{v['property']} → 인스턴스 타입 {v['instance_types']} "
                f"가 DP domain {v['expected_domain']} 과 불일치"
                for v in viols[:10]
            ]
            return error_response(
                f"T-Box domain 위반 {len(viols)}건 — 저장 거부. "
                f"OWL RL 추론 시 AllDisjointClasses/Cardinality 오염을 유발하므로 "
                f"LLM 출력을 수정하여 재시도하세요.\n" + "\n".join(_preview_lines),
                hint="자연어 설명을 해당 클래스 domain 의 속성만 쓰도록 구체화하거나, "
                     "암묵지를 더 작은 단위(속성 없는 인스턴스 + rdfs:comment)로 분해하세요.",
                logger=logger,
            )

        atomic_write(dest, ttl)
        _clear_skip_marker()
        logger.info("암묵지 저장: %s (%d자)", dest, len(ttl))
        return json.dumps({
            "success": True,
            "source": "natural_language",
            "path": dest,
            "filename": safe_name,
            "ttl_size_chars": len(ttl),
            "syntax_valid": True,
            "tbox_compliance": "ok",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def generate_tacit_from_data(
    filename: str,
    focus: str = "",
    overwrite: bool = False,
) -> str:
    """CSV 스키마 + T-Box 를 LLM 에 보여 암묵지 후보 TTL 을 부트스트랩한다.

    **위치 (S5 선택지 중 c 번째):**
      - (a) ``add_tacit_from_natural_language`` — SME 가 말로 설명 → LLM translator
      - (b) ``generate_tacit_from_rules`` — 결정적 JSON 규칙 기반 (추천, 재현 가능)
      - **(c) 이 도구** — 아직 SME 도 규칙도 없을 때 LLM 이 "뭐라도" 만듦
      - (d) ``skip_tacit_knowledge`` — 암묵지 없이 진행

    **용도는 부트스트랩 한 번뿐**:
      - 신규 도메인 파트너가 CSV 만 들고 왔을 때 초안 후보 생성.
      - 출력을 사람이 읽고 교정해 ``rules/domain/tacit_rules.json`` 으로 승격하거나
        ``add_tacit_from_natural_language`` 입력으로 옮겨 담는 게 정상 흐름.

    **한계**:
      - LLM 이 추측한 값이라 현장 검증 필수 (rdfs:comment 의 숫자/임계값 등은 가설).
      - 재실행 시 같은 입력이어도 출력이 바뀔 수 있음 (비결정적).
      - 응답에 preamble 이나 bytes-literal 이 섞여 파싱 실패할 수 있음
        (``_extract_ttl_from_response`` 가 R23 강화로 대부분 복구하지만 완벽하지 않음).

    Args:
        filename: 저장할 파일명 (확장자 제외, 영숫자/_/-만). data/source/tacit 바로
            아래에 저장하며, 같은 이름의 symlink 가 밖을 가리키면 거부한다.
        focus: 강조할 암묵지 영역 (예: "공정 흐름", "설비-에너지원 매핑"). 비어있으면 범용.
        overwrite: 덮어쓰기 허용.
    """
    try:
        try:
            safe_name = _safe_filename(filename)
        except ValueError as ve:
            return error_response(str(ve), hint=_input_guide(), logger=logger)

        _ensure_tacit_dir()
        try:
            dest = _tacit_dest(safe_name)
        except ValueError as ve:
            return error_response(str(ve), hint=_input_guide(), logger=logger)
        if os.path.exists(dest) and not overwrite:
            return error_response(
                f"파일이 이미 존재합니다: {dest}",
                hint="overwrite=True 또는 다른 filename.",
                logger=logger,
            )

        tbox_summary = _load_tbox_context()
        csv_summary = _load_csv_summary()
        domain = DOMAIN_CONFIG["domain"]
        DOMAIN_CONFIG["namespace"]
        focus_block = f"\n## 강조 영역\n{focus}" if focus.strip() else ""

        prompt = f"""당신은 {domain['name_ko']} 도메인의 시니어 온톨로지 엔지니어입니다.
CSV 데이터 스키마와 T-Box 를 참고해, CSV 에 **기록되지 않은** 현장 암묵지
(공정 흐름 체인, 설비-에너지원 매핑, 품질 규격 임계값, 고장 패턴 규칙 등)를
추론 가능한 **후보**를 RDF Turtle 로 제안하세요.

## 중요 규칙
- **확실히 알 수 있는 사실만** 포함 — 모호하거나 가상의 규칙은 금지.
- CSV 에 이미 있는 값은 반복하지 말 것 (암묵지는 CSV 밖 지식).
- 생성된 TTL 은 이후 사용자가 검토/수정할 수 있다는 전제.

## 네임스페이스
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix {NS_INST_PREFIX}: <{DOMAIN_INST_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

## T-Box
{tbox_summary}

## CSV 스키마
{csv_summary}
{focus_block}

## 출력 형식
Turtle 코드 블록만 (rdflib 파싱 가능). 설명은 TTL 내부 # 주석으로만.
rdfs:label (@ko, @en), rdfs:comment (@ko) 로 각 암묵지에 근거를 명시.

```turtle
<여기에 TTL>
```"""

        raw = invoke_bedrock_text(prompt, max_tokens=10000, temperature=0.3)
        # bytes 가 흘러들어오면 디코드 (방어). str 은 그대로.
        if isinstance(raw, bytes | bytearray):
            raw = raw.decode("utf-8", errors="replace")
        ttl = _extract_ttl_from_response(str(raw))

        ok, err = _validate_ttl_syntax(ttl)
        if not ok:
            return error_response(
                f"생성된 TTL 구문 오류: {err}",
                hint="focus 를 더 좁혀 재시도하거나 add_tacit_from_natural_language 로 "
                     "직접 입력하세요.",
                logger=logger,
            )

        atomic_write(dest, ttl)
        _clear_skip_marker()
        logger.info("암묵지 자동 생성 저장: %s (%d자)", dest, len(ttl))
        return json.dumps({
            "success": True,
            "source": "auto_from_data",
            "path": dest,
            "filename": safe_name,
            "ttl_size_chars": len(ttl),
            "syntax_valid": True,
            "hint": "자동 생성된 내용은 현장 검증이 필요합니다. read_tacit 으로 "
                    "내용을 확인하고 필요 시 수정하세요.",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


__all__ = (
    "check_tacit_exist",
    "skip_tacit_knowledge",
    "add_tacit_from_natural_language",
    "generate_tacit_from_data",
)
