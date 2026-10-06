"""Multi-Agent T-Box 생성 — 4 에이전트 협업/챌린지 구조.

작업코드 표기(P0~P9, T3, T5, R28 등)는 docs/reference/task-glossary.md 참조.

Ontology Architect (작성자, temperature=0.2):
  IOF/BFO 기반 Turtle 코드 생성. CQ를 초안부터 반영 (P5).
  확장 DSL로 수정 지시 (P2): add_restriction, add_disjoint_classes,
  rename_class/property, merge_classes 등.

Semantic Validator (논리 비평가, temperature=0.5):
  OWL 논리/추론/중복성/Restriction 중심.
  증명 책무: 1라운드에 CRITICAL 1건 + HIGH 2건 이상 의무 (P0).
  OntoQA 메트릭은 코드에서 계산해 주입 (P1) — LLM 재계산 금지.
  이슈 4층 스키마: symptom/principle/impact/fix (P7).

Manufacturing SME (현장 비평가, temperature=0.7):
  CSV 매핑 / CQ 답변 가능성 / 공정 흐름 중심.
  시나리오 의무: 각 지적에 실운영 상황 1문장 (P0).
  Validator 이슈를 받아 현장 관점에서 반박/보강 (P8).

Jury (독립 심판, temperature=0.4):
  Architect의 자기평가 대신 제3자 관점에서 production_ready 판단 (P4).
  required_fixes DSL 강제 가능.

흐름:
  Round 1: Architect → T-Box 초안 (CQ 반영)
  Round 2+: Validator 순차 → SME (Validator 이슈 주입) → Architect 수정
  합의 후보: Validator/SME 실질 승인 + 고칠 수 있는 CQ 갭 0건 + veto lock 해제 상태
  + MIN_ROUNDS 충족 (CSV FK 부재로 고칠 수 없는 CQ 갭은 막지 않는다)
  → Jury 최종 판정 (production_ready 여부 + required_fixes)
  최대 라운드 후 미합의: Jury 최종 → 실패 시 Architect compromise 폴백

Prompt context:
  - TTL cutoff 50,000자 (P3, 기존 15,000에서 확장)
  - 이전 이슈 "잊고 신선한 눈으로" anchoring 억제 지시 (P6)
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC

from rdflib import OWL, RDF, Graph, Literal, Namespace, URIRef

from config import COMPETENCY_QUESTIONS_PATH, TBOX_BASELINE_PATH, TBOX_PATH
from domain.graph_utils import clean_source_column, label_for_entity
from domain.namespaces import (
    DOMAIN_NS,
    IOF_CORE,
    IOF_MAINT,
    NS_PREFIX,
    bind_namespaces,
)
from domain.rules_paths import RULES_ROOT, rules_path
from domain.tbox_utils import _new_graph
from domain.uri_conventions import is_reserved_local
from tools.common import JobRegistry, atomic_write, error_response

_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")


def _strict_determinism() -> bool:
    """MULTI_AGENT_DETERMINISM=strict 일 때만 True.

    strict 모드에서는 temperature를 0으로 강제하고, 프롬프트 해시를 기록해
    CI/회귀 비교에서 재현성을 확보한다. 기본은 기존 동작(exploration).
    """
    return os.getenv("MULTI_AGENT_DETERMINISM", "").lower() == "strict"


# ── X1: Multi-Agent 모델 다양화 ───────────────────────────────────────
# Architect / Validator / SME 세 에이전트가 동일 모델을 쓰면 편향이 3배로 증폭된다
# (Du et al. 2023, "Improving Factuality and Reasoning in Language Models through
# Multiagent Debate"). 역할별 환경변수로 다른 모델 패밀리를 지정할 수 있다.
# 환경변수 미설정 시 BEDROCK_MODEL_ID (기존 동작) 로 fallback — backward compat.
_AGENT_MODEL_ENV = {
    "architect": "MULTI_AGENT_MODEL_ARCHITECT",
    "validator": "MULTI_AGENT_MODEL_VALIDATOR",
    "sme": "MULTI_AGENT_MODEL_SME",
    # Jury 와 compromise 는 합의 실패 시 최종 심판/절충 역할이라 일관성 우선.
    # 역할별 env var 없으면 BEDROCK_MODEL_ID 사용. 필요 시 명시적으로 설정 가능.
    "jury": "MULTI_AGENT_MODEL_JURY",
    "compromise": "MULTI_AGENT_MODEL_COMPROMISE",
}

# 리뷰 역할(Validator/SME/Jury/Compromise)의 출력 예산.
#
# **왜 8192/10000 이 아닌가**: adaptive thinking 모델은 ``max_tokens`` 를 thinking 과
# 응답이 **공유** 한다. 2026-08-18 실측 (sonnet-5, SME 프롬프트 54,788자):
# 정상 실행의 출력이 약 7,600 토큰 (thinking 2,800~3,900자 + text 5,000~5,700자).
# 즉 예전 SME 값 10,000 은 여유가 24% 뿐이고, thinking 길이는 실행마다 변동하므로
# 넘치는 실행이 나온다 — 실제로 S2 Round 2 SME 가 blocks=['thinking'] 로 죽어
# 9분치 진행이 버려졌다. 16,000 이면 같은 프롬프트에서 여유 8,400 토큰이다.
#
# 상향 비용은 낮다: 출력 토큰은 **실제 생성분만** 청구되므로 상한을 올려도 평소
# 지출은 그대로다 (절단 시의 재호출 1회를 없애 오히려 절약된다).
_REVIEW_MAX_TOKENS = 16_000


def _get_agent_model(agent_role: str | None) -> str | None:
    """에이전트 역할별 Bedrock 모델 ID 반환. 미설정 시 None 반환 (caller fallback).

    Args:
        agent_role: "architect" / "validator" / "sme" / "jury" / "compromise" /
                    None (기존 동작).

    Returns:
        환경변수에 설정된 모델 ID 또는 None. None 이면 caller 가 BEDROCK_MODEL_ID
        기본값 사용.
    """
    if not agent_role:
        return None
    env_var = _AGENT_MODEL_ENV.get(agent_role.lower())
    if not env_var:
        return None
    model_id = os.getenv(env_var)
    return model_id or None


def _invoke_bedrock(prompt: str, max_tokens: int = 8192,
                    temperature: float | None = None,
                    cached_prefix: str | None = None,
                    _auto_extend: bool = True,
                    agent_role: str | None = None) -> str:
    """Bedrock 호출 래퍼. strict 모드 처리 + max_tokens truncation 자동 확장.

    stop_reason == "max_tokens" 이면 응답이 잘렸다는 뜻. 자동으로 max_tokens 를
    1.5배 늘려 1회 재호출해 JSON 파싱 실패 원인을 제거한다.
    _auto_extend=False 로 재귀 방지.

    agent_role: X1 — "architect"/"validator"/"sme"/"jury"/"compromise" 중 하나.
    역할별 env var 설정 시 다른 모델 사용, 미설정 시 BEDROCK_MODEL_ID fallback.
    """
    from tools.bedrock import invoke_bedrock_with_metadata as _ibm

    # strict 결정 모드: temperature 0 강제. 프롬프트 해시는 logger.debug로 남긴다.
    if _strict_determinism():
        import hashlib as _hashlib
        h = _hashlib.sha256(
            ((cached_prefix or "") + prompt).encode("utf-8")
        ).hexdigest()[:16]
        logger.debug("bedrock[strict] prompt_sha=%s len=%d", h, len(prompt))
        temperature = 0.0

    model_id = _get_agent_model(agent_role)
    if model_id and agent_role:
        logger.debug("bedrock[%s] model=%s", agent_role, model_id)

    result = _ibm(
        prompt, max_tokens=max_tokens, temperature=temperature,
        cached_prefix=cached_prefix, model_id=model_id,
    )
    text = result.get("text", "")
    stop_reason = result.get("stop_reason", "")

    if stop_reason == "max_tokens" and _auto_extend:
        # 절단에는 두 등급이 있다. **text 를 조금 낳고 잘린 것** 은 1.5배로 충분하지만,
        # **text 를 아예 못 낳은 것** (adaptive thinking 이 예산을 전부 소진 —
        # blocks=['thinking']) 은 1.5배도 thinking 에 다 먹힐 수 있다. 후자는 상한까지
        # 한 번에 올린다: 재확장이 1회뿐이라(_auto_extend=False) 두 번째 기회가 없고,
        # 여기서 부족하면 빈 문자열이 파서로 흘러가 "모델이 침묵했다" 로 오진된다.
        # 2026-08-18 실측: SME(sonnet-5, 10000) 가 blocks=['thinking'] 으로 죽었다.
        _BUDGET_CEILING = 32_000
        if not text:
            extended = _BUDGET_CEILING
            logger.warning(
                "Bedrock truncation — text 블록 자체가 없음 (thinking 이 예산 "
                "소진). max_tokens %d → %d (상한) 로 1회 재호출",
                max_tokens, extended,
            )
        else:
            extended = min(int(max_tokens * 1.5), _BUDGET_CEILING)
            logger.warning(
                "Bedrock truncation 감지 — max_tokens %d → %d 로 1회 재호출",
                max_tokens, extended,
            )
        if extended > max_tokens:
            return _invoke_bedrock(
                prompt, max_tokens=extended, temperature=temperature,
                cached_prefix=cached_prefix, _auto_extend=False,
                agent_role=agent_role,
            )
        # 이미 상한이면 재호출은 같은 결과다 — 무의미한 1회를 태우지 않는다.
        logger.error(
            "Bedrock truncation 이 상한 max_tokens=%d 에서 발생 — 확장 불가. "
            "text_len=%d (0 이면 파서가 빈 입력을 받는다)", max_tokens, len(text),
        )
    return text

logger = logging.getLogger(__name__)


def _record_op_fk_source(g, op_uri, fk_column) -> None:
    """OP 의 FK 근거를 ``dcterms:source`` 로 기록.

    프롬프트(``add_object_property``)가 ``fk_column`` 을 필수로 요구한다 — 코드가
    읽지 않으면 그 지시는 조용히 폐기된다 (이 리포에서 네 번 반복된 실패 유형).
    DP 의 ``dcterms:source`` 와 대칭으로 남겨 ``step_22f`` 근거 게이트와 사람이
    "이 관계는 무엇으로 채워지나" 를 추적할 수 있게 한다.

    A-Box 는 OP 에 대해 이 값을 아직 소비하지 않는다 (FK 매칭은 컬럼 스캔으로 한다).
    그래도 기록하는 이유는 **근거를 잃지 않는 것** 이다: 값이 없으면 다음 세션이
    "이 OP 는 왜 있나" 를 알 수 없고, 그래서 유령 OP 229개가 쌓였다.

    ``none:<이유>`` 처럼 근거 부재를 명시한 값은 그대로 남긴다 — 숨기지 않는 것이
    목적이다.
    """
    from rdflib import Literal

    from domain.graph_utils import clean_source_column

    if not isinstance(fk_column, str):
        return
    cleaned = clean_source_column(fk_column)
    if cleaned:
        g.add((op_uri, _DCTERMS_NS.source, Literal(cleaned)))

_CQ_PATH = COMPETENCY_QUESTIONS_PATH
_RULES_DIR = RULES_ROOT


# ── Read-only TTL parse cache ─────────────────────────────────────────────
# 한 라운드 안에서 같은 TTL 이 4곳(메트릭/구조게이트/round inline/CQ reachability)
# 에서 재파싱된다. Graph 객체는 공유하면 예상치 못한 mutation 을 일으키므로, 캐시는
# 트리플 리스트 를 보관하고 각 호출부에서 새 Graph 에 add 하는 방식으로 복원한다.
# TTL 이 바뀌면 dict 교체 — 이전 TTL 캐시는 자동 폐기 (메모리 경계).
_TBOX_PARSE_CACHE: dict[str, list[tuple]] = {}
_TBOX_PARSE_CACHE_MAX = 4  # 직전 TTL + 이전 라운드 TTL 정도만 유지


def _parse_ttl_readonly(ttl: str) -> Graph:
    """Read-only 목적으로 TTL 을 파싱해 Graph 를 반환. 동일 TTL 재파싱 시 캐시 사용.

    **주의**: 반환된 Graph 를 수정하는 호출부는 이 함수 대신 `_new_graph()+parse` 를
    직접 사용해야 한다. 이 함수는 metric/reachability 계산 같은 read-only 용도 전용.
    """
    cached = _TBOX_PARSE_CACHE.get(ttl)
    g = _new_graph()
    if cached is not None:
        for triple in cached:
            g.add(triple)
        return g
    g.parse(data=ttl, format="turtle")
    # 캐시 용량 관리: FIFO 로 가장 오래된 항목 제거.
    if len(_TBOX_PARSE_CACHE) >= _TBOX_PARSE_CACHE_MAX:
        oldest = next(iter(_TBOX_PARSE_CACHE))
        _TBOX_PARSE_CACHE.pop(oldest, None)
    _TBOX_PARSE_CACHE[ttl] = list(g)
    return g


def _load_competency_questions() -> list[dict]:
    """Competency Questions를 로드한다."""
    if not os.path.exists(_CQ_PATH):
        return []
    with open(_CQ_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── [개선 1] CQ 기반 필수 OP 경로 사전 분석 ──────────────


def _analyze_cq_required_ops(cqs: list[dict]) -> str:
    """CQ 도메인 클래스 간 필수 OP 경로를 FK 패턴 기반으로 분석한다.

    Returns:
        Architect 프롬프트에 삽입할 '필수 ObjectProperty 목록' 문자열.
    """
    if not cqs:
        return ""
    # FK 패턴 로드
    fk_path = rules_path("fk_patterns.json", base=_RULES_DIR)
    if not os.path.exists(fk_path):
        return ""
    with open(fk_path, encoding="utf-8") as f:
        fk_data = json.load(f)
    fk_map = fk_data.get("patterns", {})  # equipmentid → EquipmentMaster

    # CSV 스키마에서 테이블별 FK 컬럼 추출
    import csv as csv_mod
    import glob

    from config import SOURCE_RAWDATA_DIR
    from tools.competency_questions import (
        _load_table_class_map,
        _resolve_domain_to_class,
    )
    table_class_map = _load_table_class_map()
    table_fks: dict[str, list[tuple[str, str]]] = {}  # TableClass → [(fk_col, TargetClass)]
    for csv_file in sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))):
        table_name = os.path.splitext(os.path.basename(csv_file))[0]
        cls_name = _resolve_domain_to_class(table_name, table_class_map)
        with open(csv_file, encoding="utf-8") as cf:
            reader = csv_mod.reader(cf)
            headers = next(reader, [])
        fks = []
        for col in headers:
            col_lower = col.lower().replace("_", "")
            target = fk_map.get(col_lower)
            if target and target != cls_name:
                fks.append((col, target))
        if fks:
            table_fks[cls_name] = fks

    # CQ 도메인 간 필요한 연결 경로 분석
    lines = ["## 필수 ObjectProperty (CQ 답변을 위해 반드시 포함)", ""]
    for cq in cqs[:10]:
        cq_id = cq.get("id", "?")
        domains = [
            _resolve_domain_to_class(d, table_class_map)
            for d in cq.get("domains", [])
        ]
        if len(domains) < 2:
            continue
        # 도메인 클래스 간 FK 경로 탐색
        ops_needed = []
        for i, a in enumerate(domains):
            for b in domains[i + 1:]:
                # 직접 FK: a의 FK가 b를 가리키거나 그 반대
                direct = False
                for cls, fks in table_fks.items():
                    if cls == a:
                        for _, target in fks:
                            if target == b:
                                ops_needed.append(f"  - {a} → {b} (FK 직접 연결)")
                                direct = True
                                break
                    if cls == b and not direct:
                        for _, target in fks:
                            if target == a:
                                ops_needed.append(f"  - {b} → {a} (FK 직접 연결)")
                                direct = True
                                break
                if not direct:
                    # 간접: 공유 FK 허브 (예: 둘 다 EquipmentMaster FK를 가짐)
                    a_targets = {t for _, t in table_fks.get(a, [])}
                    b_targets = {t for _, t in table_fks.get(b, [])}
                    shared = a_targets & b_targets
                    if shared:
                        hub = sorted(shared)[0]
                        ops_needed.append(f"  - {a} → {hub} ← {b} (공유 FK 허브)")
        if ops_needed:
            lines.append(f"**{cq_id}** (도메인: {', '.join(domains)}):")
            lines.extend(ops_needed)
            lines.append("")

    if len(lines) <= 2:
        return ""
    lines.append("위 경로가 T-Box에 ObjectProperty로 반드시 포함되어야 합니다.")
    lines.append("각 ObjectProperty에는 inverseOf 역방향 프로퍼티도 정의하세요.")
    return "\n".join(lines)


# ── [개선 2] T-Box 골격 템플릿 생성 ─────────────────


def _load_anomaly_hints() -> str:
    """rules/domain/anomaly_hints.json에서 과거 이상 이력 요약을 Architect/SME에 주입.

    SME 가 사전에 정리한 과거 KG 이상 이력 힌트를 Multi-Agent 프롬프트에
    전달해 같은 결함이 재발하지 않도록 유도. 파일이 없거나 비어 있으면
    빈 문자열.
    """
    path = rules_path("anomaly_hints.json", base=_RULES_DIR)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug("anomaly_hints 로드 실패: %s", e)
        return ""
    known = data.get("known_anomalies", []) or []
    if not known:
        return ""
    lines = [
        "## 과거 이상 이력 힌트 (이번 T-Box에서 재발 방지해야 함)",
        "",
    ]
    for item in known[:10]:
        cls = item.get("domain_class", "?")
        pat = item.get("pattern", "")
        schema = item.get("recommended_schema", {})
        lines.append(f"- **{cls}**: {pat}")
        if schema:
            if "add_class" in schema:
                lines.append(f"  - 권장 클래스: {schema['add_class']}")
            if "add_op" in schema:
                lines.append(f"  - 권장 OP: {', '.join(schema['add_op'])}")
            if "rationale" in schema:
                lines.append(f"  - 이유: {schema['rationale']}")
    lines.append("")
    lines.append("위 패턴을 수용할 수 있도록 클래스/OP를 포함하세요.")
    return "\n".join(lines)


def _generate_tbox_skeleton() -> str:
    """design_patterns.json에서 T-Box 골격 TTL을 생성한다.

    Returns:
        Architect 프롬프트에 삽입할 '사전 정의 클래스 계층' 문자열.
    """
    dp_path = rules_path("design_patterns.json", base=_RULES_DIR)
    if not os.path.exists(dp_path):
        return ""
    with open(dp_path, encoding="utf-8") as f:
        patterns = json.load(f)

    hierarchy = patterns.get("domain_hierarchy", {})
    lines = [
        "## 사전 정의 클래스 계층 (반드시 포함)",
        "",
        "아래 추상 클래스와 계층 구조를 T-Box에 **반드시** 포함하세요.",
        "CSV 테이블 클래스를 적절한 도메인 추상 클래스의 하위로 배치하세요.",
        "",
    ]
    for domain_ko, cfg in hierarchy.items():
        abstract = cfg.get("abstract_class")
        if not abstract:
            continue
        parent = cfg.get("iof_parent", "owl:Thing")
        lines.append(f"### {domain_ko} 도메인")
        lines.append(f"- **{abstract}** (rdfs:subClassOf {parent})")
        sub_groups = cfg.get("sub_groups", {})
        if sub_groups:
            for sg_name, children in sub_groups.items():
                lines.append(f"  - **{sg_name}** (rdfs:subClassOf {abstract})")
                for child in children:
                    lines.append(f"    - {child} (rdfs:subClassOf {sg_name})")
        else:
            # 서브그룹 없으면 직접 자식
            children_tables = cfg.get("children_tables", [])
            if children_tables:
                for child in children_tables:
                    lines.append(f"  - {child} (rdfs:subClassOf {abstract})")

        # 크로스 도메인 OP
        cross_ops = cfg.get("cross_domain_ops", [])
        if cross_ops:
            lines.append(f"  - 크로스 도메인 OP: {', '.join(cross_ops)}")
        lines.append("")

    if len(lines) <= 5:
        return ""
    return "\n".join(lines)


# ── [개선 6] 사전 구조 게이트 ────────────────────────


def _compute_tbox_metrics(ttl: str) -> dict:
    """Validator/Architect 프롬프트에 주입할 OntoQA 메트릭을 코드로 계산.

    LLM이 직접 계산하면 결정적으로 틀릴 수 있고 토큰도 낭비되므로 외부화.

    Returns:
        {class_count, op_count, dp_count, rr, dit, root_pct, functional_pk_count,
         restriction_count, annotation_coverage_pct, parse_error}
    """
    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import RDFS as _RDFS
    metrics: dict = {
        "class_count": 0, "op_count": 0, "dp_count": 0,
        "rr": 0.0, "dit": 0, "root_pct": 0.0,
        "functional_pk_count": 0, "restriction_count": 0,
        "annotation_coverage_pct": 0.0,
        "parse_error": None,
    }
    try:
        g = _parse_ttl_readonly(ttl)
    except Exception as e:
        metrics["parse_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return metrics

    steel_str = DOMAIN_NS
    classes = [c for c in g.subjects(_RDF.type, _OWL.Class)
               if isinstance(c, URIRef) and str(c).startswith(steel_str)]
    ops = [p for p in g.subjects(_RDF.type, _OWL.ObjectProperty)
           if isinstance(p, URIRef) and str(p).startswith(steel_str)]
    dps = [p for p in g.subjects(_RDF.type, _OWL.DatatypeProperty)
           if isinstance(p, URIRef) and str(p).startswith(steel_str)]
    metrics["class_count"] = len(classes)
    metrics["op_count"] = len(ops)
    metrics["dp_count"] = len(dps)
    total_props = len(ops) + len(dps)
    metrics["rr"] = round(len(ops) / max(total_props, 1), 3)

    # DIT
    parent_map: dict[str, str] = {}
    for c in classes:
        for p in g.objects(c, _RDFS.subClassOf):
            if isinstance(p, URIRef) and str(p).startswith(steel_str):
                parent_map[str(c)] = str(p)

    def _depth(uri: str, visited: set | None = None) -> int:
        if visited is None:
            visited = set()
        if uri in visited or uri not in parent_map:
            return 0
        visited.add(uri)
        return 1 + _depth(parent_map[uri], visited)

    metrics["dit"] = max((_depth(str(c)) for c in classes), default=0)

    # 최상위(부모 없는) 클래스 비율
    roots = [c for c in classes if str(c) not in parent_map]
    metrics["root_pct"] = round(
        len(roots) / max(len(classes), 1) * 100, 1,
    )

    # FunctionalProperty 선언 수 (PK 후보)
    metrics["functional_pk_count"] = sum(
        1 for p in g.subjects(_RDF.type, _OWL.FunctionalProperty)
        if isinstance(p, URIRef) and str(p).startswith(steel_str)
    )

    # owl:Restriction 개수 (BNode 포함)
    metrics["restriction_count"] = sum(
        1 for _ in g.subjects(_RDF.type, _OWL.Restriction)
    )

    # 어노테이션 커버리지: label + comment를 가진 클래스 비율
    labeled = 0
    for c in classes:
        has_label = any(True for _ in g.objects(c, _RDFS.label))
        has_comment = any(True for _ in g.objects(c, _RDFS.comment))
        if has_label and has_comment:
            labeled += 1
    metrics["annotation_coverage_pct"] = round(
        labeled / max(len(classes), 1) * 100, 1,
    )
    return metrics


def _format_metrics_block(m: dict) -> str:
    """Validator/SME 프롬프트에 삽입할 메트릭 블록 문자열."""
    if m.get("parse_error"):
        return (
            "## 구조 메트릭 (코드 계산)\n"
            f"- TTL 파싱 실패: {m['parse_error']}\n"
            "- 메트릭은 계산 불가. 구문 문제를 먼저 지적하세요.\n"
        )
    rr_target = "≥ 0.30"
    dit_target = "≥ 2"
    root_target = "≤ 20%"
    ann_target = "≥ 90%"
    return (
        "## 구조 메트릭 (코드 계산 결과 — 재계산 금지)\n"
        f"- 클래스: {m['class_count']}, ObjectProperty: {m['op_count']}, DatatypeProperty: {m['dp_count']}\n"
        f"- RR (OP/(OP+DP)): **{m['rr']}** (목표 {rr_target})\n"
        f"- DIT (최대 상속 깊이): **{m['dit']}** (목표 {dit_target})\n"
        f"- 최상위 클래스 비율: **{m['root_pct']}%** (목표 {root_target})\n"
        f"- FunctionalProperty 선언: {m['functional_pk_count']}\n"
        f"- owl:Restriction 수: {m['restriction_count']}\n"
        f"- 어노테이션 커버리지 (label+comment): **{m['annotation_coverage_pct']}%** (목표 {ann_target})\n"
        "\n"
        "**수치는 위가 참입니다. 직접 재계산하지 말고 미달 항목에 대한 원인 진단만 하세요.**\n"
    )


#: 인벤토리 블록에 넣을 항목 상한 — 프롬프트 폭주 방지.
_INVENTORY_MAX = 400


def _axiom_inventory(g, local, in_domain) -> dict[str, list[str]]:
    """계층·공리 축을 한 줄 표현으로 뽑는다 — 리뷰어가 대조할 수 있게.

    ## 왜 필요한가 (2026-08-27 실측)

    인벤토리는 **클래스 목록 + OP domain→range** 두 축만 담고 있었다. 그래서 리뷰어가
    검증할 수 없는 것을 매 라운드 지적했다:

        축                     실제      인벤토리
        subClassOf             69건      **없음**
        AllDisjointClasses     46그룹    개수만 (members 0%)
        someValuesFrom         30건      **없음**
        inverseOf             106건      **없음**

    리뷰어가 보는 TTL 발췌는 원문의 **29%** 다(50,000자 상한). 즉 이 축들은 발췌에도
    인벤토리에도 없어 "확인 불가" 로 지적되고, 그 지적이 차단 이슈로 세어져 승인을
    막는다 — 2회 실행에서 잔여 차단의 최다 성격이었다.

    ``AllDisjointClasses`` 는 **members 를 반드시 펼친다.** 개수만 주면 "축이 혼재
    됐는가" 를 판정할 수 없는데, 그 판정이 정확히 리뷰어가 해야 하는 일이다 (이 리포에서
    직교 축 혼재로 6개 클래스가 unsat 이 된 이력이 있다).

    비용: 네 축 합계 약 13,180자 ≈ 3,766 토큰. 리뷰어 입력이 17,433 → 21,199 토큰이
    되고 **입력은 출력 예산(_REVIEW_MAX_TOKENS)과 별개**다. TTL 발췌가 이미 14,304
    토큰(82%)을 쓰면서 29%만 보여주는 것에 비하면 전수 목록이 훨씬 효율적이다.
    """
    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import RDFS as _RDFS
    from rdflib.collection import Collection as _Collection

    sub = sorted({
        f"{local(s)} ⊑ {local(o)}"
        for s, o in g.subject_objects(_RDFS.subClassOf)
        if in_domain(s) and in_domain(o)
    })
    # inverseOf 는 방향 쌍이므로 정렬된 튜플로 중복 제거 (A↔B 와 B↔A 는 한 건).
    inv_pairs = {
        tuple(sorted((local(s), local(o))))
        for s, o in g.subject_objects(_OWL.inverseOf)
        if in_domain(s) and in_domain(o)
    }
    inv = sorted(f"{a} ↔ {b}" for a, b in inv_pairs)

    disjoint: list[str] = []
    for node in g.subjects(_RDF.type, _OWL.AllDisjointClasses):
        # g.objects 로 **전부** 열거한다. g.value 는 첫 리스트만 보므로 병합 잔여물이
        # 있으면 members 를 놓친다 (이 리포에서 skolemize 병합이 members 를 중복시켜
        # HermiT 로드가 실패한 이력이 있다).
        for lst in g.objects(node, _OWL.members):
            try:
                names = sorted(local(x) for x in _Collection(g, lst))
            except Exception:  # noqa: BLE001 — 깨진 리스트는 건너뛴다
                continue
            if len(names) >= 2:
                disjoint.append(", ".join(names))
    disjoint = sorted(set(disjoint))

    svf: set[str] = set()
    for restr, filler in g.subject_objects(_OWL.someValuesFrom):
        props = [local(p) for p in g.objects(restr, _OWL.onProperty)]
        holders = [local(c) for c in g.subjects(_RDFS.subClassOf, restr)
                   if in_domain(c)]
        for holder in holders:
            for prop in props:
                svf.add(f"{holder} ∃{prop}.{local(filler)}")

    return {
        "subclass": sub,
        "inverse": inv,
        "disjoint": disjoint,
        "some_values": sorted(svf),
    }


def _format_declaration_inventory(ttl: str) -> str:
    """"무엇이 이미 선언돼 있는지" 를 **코드 계산으로** 프롬프트에 주입한다.

    리뷰어는 TTL 의 앞부분만 본다 (``_TTL_PROMPT_MAX`` = 50,000자, 실측 150,132자
    T-Box 의 33%). 그래서 절단선 밖의 선언을 **"없다"고 판정** 한다. 2026-08-10
    실행에서 Jury 가 veto lock 으로 지목한 항목들의 첫 등장 위치는
    ``gasEnergyIsFromEquipment`` @123,924 / ``fuelConsumptionIsFromEquipment``
    @123,575 / ``owl:AllDisjointClasses`` @146,580 — 전부 밖이었다. Jury 의
    "TTL 에서 확인되지 않음" 은 자기가 받은 33% 에 대해서는 **참** 이었다.

    상한을 올리는 대신 인벤토리를 주입하는 이유: 목록은 TTL 원문보다 훨씬 짧고
    (관계는 ``domain→range`` 한 줄), 절단 위치와 무관하게 **전수** 를 담는다.
    """
    try:
        g = _parse_ttl_readonly(ttl)
    except Exception as exc:  # noqa: BLE001 — 인벤토리는 보조 정보다
        # 실패를 debug 로 삼키면 안 된다. 2026-08-14 실측: 호출부가 마크다운
        # 블록을 TTL 앞에 붙여 넘겨 이 파싱이 **항상** 깨졌고 (8,389자 → 0자),
        # 리뷰어 두 명이 인벤토리 없이 33% 발췌만 보고 "선언 없음" 을 단정했다.
        # 그 허위 지적이 veto lock 으로 5라운드를 태웠다. 조용한 빈 문자열은
        # 기능이 죽은 것과 정상 동작을 구분 불가하게 만든다 → error + 마커.
        logger.error(
            "선언 인벤토리 생성 실패 (파싱 불가) — 리뷰어가 전수 목록 없이 "
            "발췌만 보게 된다: %s", exc,
        )
        return (
            "## ⚠️ 선언 인벤토리 생성 실패\n"
            "T-Box 전수 스캔에 실패했습니다. 아래 TTL 발췌는 **전체가 아닙니다**.\n"
            "발췌에 안 보인다는 이유로 '선언 없음' 이라고 단정하지 마세요.\n"
        )

    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import RDFS as _RDFS

    def _local(node) -> str:
        return str(node).split("#")[-1].split("/")[-1]

    classes = sorted({
        _local(c) for c in g.subjects(_RDF.type, _OWL.Class)
        if str(c).startswith(DOMAIN_NS)
    })
    links: set[str] = set()
    for op in g.subjects(_RDF.type, _OWL.ObjectProperty):
        if not str(op).startswith(DOMAIN_NS):
            continue
        for dom in g.objects(op, _RDFS.domain):
            for rng in g.objects(op, _RDFS.range):
                links.add(f"{_local(dom)}→{_local(rng)}: {_local(op)}")
    disjoint_groups = len(set(g.subjects(_RDF.type, _OWL.AllDisjointClasses)))
    sourced = len({
        s for s, _, _ in g.triples((None, _DCTERMS_NS.source, None))
    })

    def _in_domain(node) -> bool:
        return isinstance(node, URIRef) and str(node).startswith(DOMAIN_NS)

    axioms = _axiom_inventory(g, _local, _in_domain)

    parts = [
        "## 이미 선언된 것 (코드 계산 — TTL 발췌보다 이 목록이 정확합니다)\n",
        "아래는 **TTL 전체** 를 스캔한 결과입니다. 위 TTL 발췌는 앞부분만 보여주므로,\n"
        "발췌에 안 보인다고 '없다'고 판단하지 마세요. **이 목록에 있으면 존재합니다.**\n",
        f"\n### 클래스 ({len(classes)}개)\n{', '.join(classes[:_INVENTORY_MAX])}\n",
        f"\n### ObjectProperty 관계 ({len(links)}개, domain→range: 이름)\n",
    ]
    parts.append("\n".join(f"- {x}" for x in sorted(links)[:_INVENTORY_MAX]) + "\n")
    if len(links) > _INVENTORY_MAX:
        parts.append(f"... (총 {len(links)}개 중 {_INVENTORY_MAX}개 표시)\n")
    # 계층·공리 축. 예전에는 AllDisjointClasses 를 **개수만** 알려주고 나머지 세 축은
    # 아예 없었다 — 리뷰어는 발췌(원문의 29%)에도 없는 것을 "확인 불가" 로 지적하고,
    # 그 지적이 차단 이슈로 세어져 승인을 막았다 (2회 실행에서 잔여 차단의 최다 성격).
    _AXIS_LABELS = (
        ("subclass", "클래스 계층 (subClassOf)", "자식 ⊑ 부모"),
        ("disjoint", "AllDisjointClasses 그룹 (members 전개)",
         "한 줄이 한 그룹 — **분류 축이 혼재됐는지 여기서 판정하세요**"),
        ("inverse", "owl:inverseOf 쌍", "A ↔ B (방향 쌍은 한 줄)"),
        ("some_values", "someValuesFrom Restriction", "보유클래스 ∃프로퍼티.대상"),
    )
    for key, title, hint in _AXIS_LABELS:
        items = axioms[key]
        parts.append(f"\n### {title} ({len(items)}개, {hint})\n")
        if not items:
            parts.append("- (선언 없음 — 이 축은 실제로 비어 있습니다)\n")
            continue
        parts.append("\n".join(f"- {x}" for x in items[:_INVENTORY_MAX]) + "\n")
        if len(items) > _INVENTORY_MAX:
            parts.append(
                f"... (총 {len(items)}개 중 {_INVENTORY_MAX}개 표시 — "
                "표시되지 않은 것도 **존재합니다**)\n"
            )

    parts.append(
        f"\n### 기타\n- owl:AllDisjointClasses 그룹: {disjoint_groups}개\n"
        f"- dcterms:source 표기된 프로퍼티: {sourced}개\n"
        "\n**같은 domain→range 를 잇는 OP 가 이미 있으면 동의어를 추가하지 마세요.**\n"
        "\n⚠️ 위 목록은 **TTL 전수 스캔** 결과입니다. 계층·disjoint·inverseOf·"
        "Restriction 을 '발췌에서 확인되지 않음' 이라는 이유로 누락으로 지적하지 마세요 "
        "— 목록에 있으면 선언돼 있습니다. 목록에 **없을 때만** 누락입니다.\n"
    )
    return "".join(parts)


def _check_structural_gate(ttl: str) -> tuple[bool, str]:
    """Architect 초안의 구조적 품질을 사전 검증한다.

    Returns:
        (통과 여부, 피드백 메시지)
    """
    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import RDFS as _RDFS
    try:
        g = _parse_ttl_readonly(ttl)
        steel_str = DOMAIN_NS
        cls_set = {c for c in g.subjects(_RDF.type, _OWL.Class) if isinstance(c, URIRef) and str(c).startswith(steel_str)}
        ops = {p for p in g.subjects(_RDF.type, _OWL.ObjectProperty) if isinstance(p, URIRef) and str(p).startswith(steel_str)}
        dps = {p for p in g.subjects(_RDF.type, _OWL.DatatypeProperty) if isinstance(p, URIRef) and str(p).startswith(steel_str)}

        total_props = len(ops) + len(dps)
        rr = len(ops) / max(total_props, 1)

        # 최대 계층 깊이
        parent_map: dict[str, str] = {}
        for c in cls_set:
            for p in g.objects(c, _RDFS.subClassOf):
                if isinstance(p, URIRef) and str(p).startswith(steel_str):
                    parent_map[str(c)] = str(p)

        def _depth(uri: str, visited: set | None = None) -> int:
            if visited is None:
                visited = set()
            if uri in visited or uri not in parent_map:
                return 0
            visited.add(uri)
            return 1 + _depth(parent_map[uri], visited)

        max_dit = max((_depth(str(c)) for c in cls_set), default=0)

        issues = []
        if max_dit < 2:
            issues.append(f"DIT={max_dit} < 2: 도메인별 추상 부모 클래스를 추가하세요")
        if rr < 0.25:
            issues.append(f"RR={rr:.2f} < 0.25: ObjectProperty가 부족합니다. 크로스 도메인 OP를 추가하세요")

        if issues:
            return False, "\n".join(issues)
        return True, f"구조 게이트 통과 (DIT={max_dit}, RR={rr:.2f})"
    except Exception as e:
        return True, f"구조 게이트 검사 실패 (무시): {e}"


# P3: TTL 컨텍스트 상한. 115KB 한국어 T-Box ≈ 40,000자 수준이라 50000 기본값.
_TTL_PROMPT_MAX = 50000


class _GraphAwareNamespace(Namespace):
    """DSL 값을 **그래프의 prefix 바인딩** 으로 해석하는 네임스페이스 어댑터.

    LLM 이 반환하는 DSL 지시의 값은 계약상 local name 이지만 실제로는
    ``steel:Equipment`` (배포 prefix), ``iof-core:MaterialArtifact`` (외부 온톨로지
    prefix), ``http://…#Equipment`` (완전 IRI) 가 섞여 온다. raw ``Namespace`` 는
    그것을 그대로 이어붙여 ``http://…#steel:Equipment`` 라는 **별개 리소스** 를
    만들고, 그렇게 생성된 클래스·프로퍼티는 정상 IRI 와 연결되지 않아 유령으로
    남는다.

    2026-08-09 실측 (S2 1회 실행): 유령 subject 181개 / 이중 접두사 574회 등장.
    subClassOf 144개 중 93개, owl:Restriction 14개 **전부** 가 유령에 붙어 무효였고
    ObjectProperty 229개 중 101개(44%)의 domain/range 가 선언되지 않은 클래스를
    가리켰다. 즉 Jury/Validator 가 지시한 구조 수정이 적용된 것처럼 보이지만 실제로는
    허공에 적용됐다.

    **단순히 prefix 를 벗기면 안 된다**: ``iof-core:MaterialArtifact`` 를 local name
    으로 줄이면 도메인 네임스페이스의 다른 클래스가 되어 IOF 매핑이 깨진다. 그래서
    ``jury_fixes._resolve_prefixed_or_uri`` 를 쓴다 — 그래프에 바인딩된 prefix 는
    제대로 해석하고, 미등록 prefix 와 bare name 만 도메인 NS 로 폴백한다.

    ``rdflib.Namespace`` 는 ``str`` 서브클래스이므로 ``__getitem__`` 만 덮으면 기존
    27개 호출 지점(``steel_ns[...]``)이 그대로 안전해진다.
    """

    def __new__(cls, value, graph=None):  # noqa: D102 — Namespace 는 str 서브클래스
        self = super().__new__(cls, value)
        self._graph = graph
        return self

    def __getitem__(self, key):  # noqa: D105 — 동작은 클래스 docstring 참조
        return _resolve_dsl_name(str(key), self._graph, str(self))

    def __getattr__(self, name):  # noqa: D105
        if name.startswith("_"):
            raise AttributeError(name)
        return _resolve_dsl_name(name, self._graph, str(self))


def _resolve_dsl_name(raw: str, graph, domain_ns: str):
    """DSL 값 → URIRef. 그래프 prefix 바인딩 우선, 없으면 도메인 NS 폴백."""
    if graph is not None:
        try:
            from tools.jury_fixes import _resolve_prefixed_or_uri
            return _resolve_prefixed_or_uri(raw, graph)
        except Exception:  # noqa: BLE001 — 해석 실패 시 아래 폴백
            pass
    if raw.startswith(("http://", "https://", "urn:")):
        return URIRef(raw)
    # prefix 껍데기만 벗겨 도메인 NS 에 붙인다 (그래프 없을 때의 최소 방어).
    return URIRef(domain_ns + raw.rsplit(":", 1)[-1])


def _format_ttl_diff_block(prev_ttl: str, curr_ttl: str,
                            max_diff_chars: int = 20000,
                            context_lines: int = 2) -> str:
    """라운드 N≥2에서 full TTL 대신 이전 라운드 대비 diff만 프롬프트에 보낸다.

    diff가 커서 상한을 넘기면 full TTL을 fallback으로 사용한다 (큰 변화 시 부분만
    보이면 판단이 오히려 어려움).
    diff가 너무 작으면 (< 200자) "변경 없음"을 명시.
    """
    import difflib as _dl
    diff = "\n".join(_dl.unified_diff(
        prev_ttl.splitlines(),
        curr_ttl.splitlines(),
        fromfile="prev_round",
        tofile="curr_round",
        n=context_lines,
        lineterm="",
    ))
    if len(diff) > max_diff_chars:
        return _format_ttl_block(curr_ttl)  # 큰 변화 — full 보내기
    # + / - 로 시작하는 실제 변경 라인 수가 0이면 변경 없음.
    change_lines = sum(
        1 for line in diff.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    if change_lines == 0:
        return (
            "## T-Box 변경 없음\n"
            "이전 라운드 대비 T-Box가 실질적으로 바뀌지 않았습니다. "
            "이전 이슈 중 해결이 누락된 항목을 우선 재지적하세요."
        )
    return (
        "## T-Box 변경 사항 (이전 라운드 대비 unified diff)\n"
        "아래는 이전 라운드 TTL 대비 변경된 부분만 표시합니다. "
        "전체 TTL을 보고 싶다면 '[FULL_TTL_REQUEST]' 마커를 응답에 포함하세요.\n\n"
        f"```diff\n{diff}\n```"
    )


def _format_ttl_block(ttl: str, limit: int = _TTL_PROMPT_MAX) -> str:
    """TTL을 프롬프트에 삽입할 코드블럭 문자열로 반환 (cutoff 표시 포함)."""
    if len(ttl) <= limit:
        return f"```turtle\n{ttl}\n```"
    return (
        f"```turtle\n{ttl[:limit]}\n```\n"
        f"... (총 {len(ttl)} 문자, 상위 {limit}자 표시 — 뒷부분은 다음 라운드에서 검토)"
    )


def _validator_review(
    ttl: str, round_num: int,
    previous_issues: list | None = None,
    metrics: dict | None = None,
    prev_ttl: str | None = None,
    preamble: str = "",
) -> dict:
    """Semantic Validator — OWL 논리적 일관성 검토.

    P0 비대칭 강제 + P1 메트릭 코드화 + P3 TTL 상한 확대 +
    P6 anchoring 억제 + P7 이슈 4층 스키마 + P9 temperature 0.5.

    Args:
        ttl: **순수 TTL only**. 마크다운을 앞에 붙여 넘기지 마라 — 이 문자열은
            ``_format_declaration_inventory`` / ``_format_ttl_diff_block`` 이
            rdflib 로 파싱한다. 서술 텍스트는 ``preamble`` 로 넘긴다.
        preamble: 메트릭 피드백 등 프롬프트 앞머리에 넣을 마크다운. 파싱 대상이
            아니다.
    """
    # 라운드별 집중
    round_focus = ""
    if round_num == 1:
        round_focus = "\n**[이번 라운드 집중]**: 클래스 계층 구조, ObjectProperty 스키마, 도메인별 추상 클래스 존재 여부. **DatatypeProperty도 파싱 가능하면 같이 보세요** (Architect가 이미 DP까지 생성했을 수 있음).\n"
    elif round_num == 2:
        round_focus = "\n**[이번 라운드 집중]**: domain/range 정합성, DatatypeProperty 매핑, FK 기반 ObjectProperty 완전성, someValuesFrom Restriction.\n"
    else:
        round_focus = "\n**[이번 라운드 집중]**: 공리(FunctionalProperty, someValuesFrom), 어노테이션 완전성, IOF 표준 정렬, 잔존 critical/high.\n"

    # P6/P0/P7: 공통 프롬프트 블록은 multi_agent_prompts로 추출됨
    from tools.multi_agent_prompts import burden_of_proof_block
    from tools.multi_agent_prompts import (
        previous_issues_block as _prev_issues,
    )
    previous_issues_block = _prev_issues(previous_issues, include=(round_num >= 2))
    burden_of_proof = burden_of_proof_block("validator", round_num)
    # P1: OntoQA 메트릭을 LLM 계산이 아닌 코드에서 주입
    metrics_block = _format_metrics_block(metrics) if metrics else ""
    # 이슈 JSON 스키마는 ``validator_static_prefix`` 안에서 이미
    # ``issue_schema_block("validator")`` 로 주입된다 (multi_agent_prompts.py:213).
    # 여기서 또 계산하던 지역변수는 어디에도 쓰이지 않는 리팩터 잔재였다.

    # 정적 prefix(역할 도입부 + 검토 관점 + 출력 형식)는 캐싱 대상.
    # 라운드마다 바뀌는 부분(round_focus/burden/metrics/previous/TTL)만 variable.
    from tools.multi_agent_prompts import validator_static_prefix
    cached_prefix = validator_static_prefix()
    # 라운드 2+ 이고 prev_ttl이 주어지면 diff 블록으로 토큰 절감.
    if round_num >= 2 and prev_ttl is not None:
        ttl_section = _format_ttl_diff_block(prev_ttl, ttl)
    else:
        ttl_section = "## T-Box TTL\n" + _format_ttl_block(ttl)
    # 발췌/diff 는 전체가 아니다 — 무엇이 이미 선언됐는지는 코드 계산으로 준다.
    ttl_section += "\n\n" + _format_declaration_inventory(ttl)

    variable_prompt = f"""{preamble}{round_focus}
{burden_of_proof}

아래 구조 메트릭은 **이미 코드로 계산**되어 있습니다. 미달 항목은 **원인 진단** 이슈로 만들되, 숫자 자체를 재계산하지 마세요.

{metrics_block}
## 검토 라운드: {round_num}
{previous_issues_block}
{ttl_section}

JSON만 출력하세요:"""

    # P9: Validator temperature = 0.5 (다양한 오류 탐색)
    text = _invoke_bedrock(
        variable_prompt, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.5,
        cached_prefix=cached_prefix, agent_role="validator",
    )

    def _retry_validator(retry_prompt_override: str) -> str:
        # **1차보다 줄이지 않는다.** 예전엔 8192 → 4096 (-50%) 이었는데, 1차가
        # 길이 때문에 잘려 파싱에 실패했다면 절반으로 줄인 재시도는 더 잘린다.
        # 실측 (2026-08-17 R4): Validator 가 파싱 실패해 이슈 13건이 0으로
        # 기록되고 Architect 가 수정 지시를 못 받았다. temperature 만 낮춰
        # 형식 안정성을 올린다.
        return _invoke_bedrock(
            retry_prompt_override, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.2,
            cached_prefix=cached_prefix, agent_role="validator",
        )

    # 재시도 시 원본 컨텍스트(variable_prompt)를 다시 포함 — 안내문만 보내면
    # 모델이 빈 입력을 받아 분석 없이 빈 issues 를 반환하는 회귀가 발생한다.
    retry_prompt = (
        variable_prompt
        + "\n\n[재시도] 이전 응답이 JSON 파싱에 실패했습니다. 아래 지시를 엄격히 지키세요:\n"
        "- 반드시 단일 JSON 객체 하나만 출력.\n"
        "- 코드블록, 마크다운, 설명 텍스트 모두 금지.\n"
        "- 필드: issues (list), approved (bool), summary (string).\n"
        "- issues[] 필수 필드: severity, category, target, symptom, principle, impact, fix.\n"
        "JSON 만 출력하세요:"
    )
    return _parse_review_json(
        text, "Semantic Validator",
        retry_invoker=_retry_validator,
        retry_prompt=retry_prompt,
    )


def _sme_review(
    ttl: str, csv_summary: str, cqs: list[dict], round_num: int,
    previous_issues: list | None = None,
    validator_issues: list | None = None,
    prev_ttl: str | None = None,
    preamble: str = "",
) -> dict:
    """Manufacturing SME — 현장 공정 적합성 검토.

    P0 scenario 의무 + P3 TTL 상한 확대 + P6 anchoring 억제 +
    P7 이슈 4층 스키마 + P8 Validator 이슈 주입 + P9 temperature 0.7.

    Args:
        ttl: **순수 TTL only**. 마크다운 서술은 ``preamble`` 로 넘긴다
            (``_validator_review`` 와 같은 이유 — 인벤토리 파싱이 깨진다).
        preamble: 프롬프트 앞머리 마크다운. 파싱 대상이 아니다.
    """
    cq_text = "\n".join(f"- CQ{i+1}: {cq['question_ko']}" for i, cq in enumerate(cqs[:10]))

    round_focus = ""
    if round_num == 1:
        round_focus = "\n**[이번 라운드 집중]**: CQ 답변에 필요한 클래스 간 ObjectProperty 연결 경로.\n"
    elif round_num == 2:
        round_focus = "\n**[이번 라운드 집중]**: CSV 컬럼 누락, 데이터 타입 정확성, FK 관계의 현장 적합성.\n"
    else:
        round_focus = "\n**[이번 라운드 집중]**: 크로스 도메인 관계 완전성, 현장 운영 시나리오 충족 여부.\n"

    # P6/P0/P7/P8: 공통 프롬프트 블록은 multi_agent_prompts로 추출됨
    from tools.multi_agent_prompts import burden_of_proof_block
    from tools.multi_agent_prompts import (
        previous_issues_block as _prev_issues,
    )
    from tools.multi_agent_prompts import (
        validator_issues_block as _vi_block,
    )
    sme_admonition = (
        "**위 지적은 잊고**, 현재 TTL을 처음 보는 눈으로 재평가하세요. "
        "새로운 관점의 이슈를 우선 찾고, 이전 이슈는 **명백히 해결 안 된 것만** 다시 언급하세요."
    )
    previous_issues_block = _prev_issues(
        previous_issues, include=(round_num >= 2), admonition=sme_admonition,
    )
    # SME는 반례(counterexample) + 실운영 시나리오 강제 — 확장된 예시
    scenario_obligation = burden_of_proof_block("sme", round_num) + """
시나리오 예시:
- "알람이 5초에 100개 터지면 이 OP 설계로 '설비 X의 24시간 내 알람 수' 쿼리가 가능한가?"
- "공정 중간에 제품 등급이 바뀌면 이 T-Box로 '등급별 수율' 집계가 SPARQL 한 방에 되는가?"
- "운전자가 시프트 교대할 때 담당 설비 이력을 이 스키마로 추적 가능한가?"
"""
    # 이슈 JSON 스키마는 ``sme_static_prefix`` 안에서 이미 주입된다
    # (multi_agent_prompts.py:256). 여기 지역변수는 리팩터 잔재였다.
    validator_issues_block = _vi_block(validator_issues)

    # Cross-domain OP 목록
    _cross_domain_lines = []
    try:
        _dp_path = rules_path("design_patterns.json", base=_RULES_DIR)
        with open(_dp_path, encoding="utf-8") as _dpf:
            _dp_data = json.load(_dpf)
        _hierarchy = _dp_data.get("domain_hierarchy", {})
        for _domain_ko, _cfg in _hierarchy.items():
            _ops = _cfg.get("cross_domain_ops", [])
            if _ops:
                _cross_domain_lines.append(f"   - {_domain_ko}: {', '.join(_ops)}")
    except Exception as e:
        logger.debug("design_patterns.json 로드 실패: %s", e)
    _cross_domain_block = "\n".join(_cross_domain_lines) if _cross_domain_lines else "   (design_patterns.json 로드 실패)"

    # 정적 prefix(역할 + CQ + CSV 요약 + 크로스 도메인 + 출력 형식)는 캐싱 대상.
    # variable = round_focus + scenario_obligation + validator_issues + previous_issues + TTL.
    from tools.multi_agent_prompts import sme_static_prefix
    cached_prefix = sme_static_prefix(
        cq_text=cq_text, csv_summary=csv_summary,
        cross_domain_block=_cross_domain_block,
    )
    if round_num >= 2 and prev_ttl is not None:
        ttl_section = _format_ttl_diff_block(prev_ttl, ttl)
    else:
        ttl_section = "## T-Box TTL\n" + _format_ttl_block(ttl)
    # 발췌/diff 는 전체가 아니다 — 무엇이 이미 선언됐는지는 코드 계산으로 준다.
    ttl_section += "\n\n" + _format_declaration_inventory(ttl)

    variable_prompt = f"""{preamble}{round_focus}
{scenario_obligation}

## 검토 라운드: {round_num}
{validator_issues_block}
{previous_issues_block}
{ttl_section}

JSON만 출력하세요:"""

    # P9: SME temperature = 0.7 (현장 시나리오 창의적).
    # 예산은 _REVIEW_MAX_TOKENS (16,000). 이력: 8192 → 10000 (unanswerable_cqs 가
    # 길어지면 잘리는 사례) → 16000 (thinking 모델이 예산을 공유 — 상단 상수 주석).
    text = _invoke_bedrock(
        variable_prompt, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.7,
        cached_prefix=cached_prefix, agent_role="sme",
    )

    def _retry_sme(retry_prompt_override: str) -> str:
        # 재시도: temperature 0.3 으로 안정성 ↑. max_tokens 는 **1차와 동일** —
        # 예전엔 10000 → 6000 (-40%) 이었는데 "스키마 축소 전제" 는 재시도
        # 프롬프트가 원본 컨텍스트를 다시 포함하도록 고쳐진 뒤로 성립하지 않는다
        # (bca1f69). 줄이면 1차가 길이로 실패한 경우 더 잘린다.
        return _invoke_bedrock(
            retry_prompt_override, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.3,
            cached_prefix=cached_prefix, agent_role="sme",
        )

    # 재시도 시 원본 컨텍스트(variable_prompt)를 다시 포함 — 안내문만 보내면
    # 모델이 빈 입력을 받아 분석 없이 빈 issues 를 반환하는 회귀가 발생한다.
    retry_prompt = (
        variable_prompt
        + "\n\n[재시도] 이전 응답이 JSON 파싱에 실패했습니다. 아래 지시를 엄격히 지키세요:\n"
        "- 반드시 단일 JSON 객체 하나만 출력.\n"
        "- 코드블록, 마크다운, 설명 텍스트 모두 금지.\n"
        "- 필드: issues (list), approved (bool), summary (string).\n"
        "- issues[] 최대 8개로 제한, 필수 필드: severity, category, target, symptom, principle, impact, fix.\n"
        "- scenario 필드는 있으면 좋지만 없어도 허용.\n"
        "- unanswerable_cqs 는 최대 5건. validator_agreement 는 생략.\n"
        "- summary 는 한국어 2문장 이내로 간결히.\n"
        "JSON 만 출력하세요:"
    )
    return _parse_review_json(
        text, "Manufacturing SME",
        retry_invoker=_retry_sme,
        retry_prompt=retry_prompt,
    )


_PREFIX_MAP = {
    f"{NS_PREFIX}:": DOMAIN_NS,
    "rdfs:": "http://www.w3.org/2000/01/rdf-schema#",
    "owl:": "http://www.w3.org/2002/07/owl#",
    "rdf:": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "xsd:": "http://www.w3.org/2001/XMLSchema#",
    "iof-core:": IOF_CORE,
    "iof-maint:": IOF_MAINT,
}


def _resolve_uri(prefixed: str) -> URIRef:
    """Prefixed name (e.g. 'steel:ClassName') -> rdflib URIRef."""
    for prefix, ns in _PREFIX_MAP.items():
        if prefixed.startswith(prefix):
            return URIRef(str(ns) + prefixed[len(prefix):])
    return URIRef(prefixed)


def _resolve_uri_or_literal(value: str):
    """Prefixed name -> URIRef, otherwise -> Literal."""
    if any(value.startswith(p) for p in _PREFIX_MAP):
        return _resolve_uri(value)
    # Typed literal: "value"^^xsd:type
    if "^^" in value:
        val, dtype = value.rsplit("^^", 1)
        return Literal(val.strip('"'), datatype=_resolve_uri(dtype))
    # Language-tagged literal: "text"@ko
    if value.startswith('"') and "@" in value:
        m = re.match(r'^"(.*)"@(\w+)$', value)
        if m:
            return Literal(m.group(1), lang=m.group(2))
    if value.startswith('"') or value.startswith("'"):
        return Literal(value.strip("\"'"))
    return Literal(value)


def _apply_patches(ttl: str, patches: list[dict]) -> str:
    """패치 목록을 rdflib Graph에 적용하여 수정된 TTL을 반환한다."""
    g = _new_graph()
    g.parse(data=ttl, format="turtle")
    bind_namespaces(g)

    applied = 0
    for patch in patches:
        action = patch.get("action", "")
        try:
            s = _resolve_uri(patch["subject"])
            p = _resolve_uri(patch["predicate"])
            o = _resolve_uri_or_literal(patch["object"])
        except (KeyError, ValueError) as exc:
            logger.warning("패치 스킵 (파싱 오류): %s — %s", patch, exc)
            continue

        if action == "add_triple":
            g.add((s, p, o))
            applied += 1
        elif action == "remove_triple":
            g.remove((s, p, o))
            applied += 1
        else:
            logger.warning("알 수 없는 패치 action: %s", action)

    logger.info("패치 %d/%d건 적용 완료", applied, len(patches))
    return g.serialize(format="turtle")


#: ``_apply_high_level_instructions`` **만** 아는 action. 나머지는 전부
#: ``jury_fixes`` 로 보낸다 (그쪽에 방어 가드가 있다).
#:
#: 두 엔진이 아는 action 이 8개 겹치는데, DSL 쪽에는 가드가 없다. 예전에는 같은
#: 리스트를 DSL → jury_fixes 순서로 **둘 다** 태워서, DSL 이 가드 없이 적용한 뒤
#: jury_fixes 가 "이미 존재/트리플 없음" no-op 만 보고했다 — 가드가 있는데도
#: 무력화된 상태다.
#:
#: 실증 (2026-08-18): ``remove_triple <OP> rdfs:range <Class>`` (Jury 가 실제로
#: 4건 요청) 은 jury_fixes 단독이면 **거부**되지만("제거하면 제약 없는 껍데기가
#: 되어 A-Box 생성기·conformance 검사·중복 게이트가 모두 그 프로퍼티를 무시한다"),
#: DSL 선행 시 그대로 삭제돼 ``range=[]`` 가 됐다.
#:
#: ``add_object_property``/``add_restriction`` 은 Jury 가 ``property``/``class``
#: 키를 쓰고 DSL 은 ``name``/``target_class`` 를 요구해 DSL 에서 KeyError 로
#: 실패한다 — 즉 우연히 안전했다. 우연에 의존하지 않도록 라우팅을 명시한다.
#: ``add_inverse_functional_property`` 는 2026-08-30 에 이 집합에서 빠졌다.
#: 여기 있는 동안 IFP 는 "DSL 로 보내면 DSL 이 거부한다" 는 **우연**으로 막혀
#: 있었다 — jury_fixes 에는 핸들러가 있었으므로 이 한 줄이 바뀌면 정책이 조용히
#: 뒤집혔다. 이제 정책은 :data:`tools.action_registry.POLICY_REJECTED` 가 정본이고
#: 두 엔진이 각자 강제한다 (라우팅과 무관).
_DSL_ONLY_JURY_ACTIONS = frozenset({
    "add_equivalent_class_union",
    "add_transitive_property",
    "merge_classes",
    "rename_class",
    "rename_property",
})


def _route_jury_fixes(required_fixes: list[dict]) -> tuple[list[dict], list[dict]]:
    """Jury 의 ``required_fixes`` 를 ``(dsl_only, jury_bound)`` 로 나눈다.

    - ``jury_fixes`` 가 아는 action → **jury_fixes 로만** (가드 보존)
    - ``jury_fixes`` 가 모르고 DSL 만 아는 action → DSL 로
    - 어느 쪽도 모르는 action → jury_fixes 로 (거기서 미지원으로 집계돼 보인다.
      DSL 로 보내면 조용히 무시되거나 예외가 나고 운영자는 요청이 어디로
      사라졌는지 알 수 없다.)

    action 이름 변종(``addObjectProperty`` 등)은 ``jury_fixes._DISPATCH`` 가 이미
    62개 키로 흡수하므로 그 집합을 권위 소스로 쓴다 (사본 금지).
    """
    try:
        from tools.jury_fixes import _DISPATCH as _JURY_DISPATCH
        jury_known = set(_JURY_DISPATCH)
    except Exception:  # noqa: BLE001 — import 실패 시 전부 jury 로 (가드 우선)
        jury_known = set()

    dsl_only: list[dict] = []
    jury_bound: list[dict] = []
    for fix in required_fixes or []:
        action = str((fix or {}).get("action") or "")
        if action in jury_known:
            jury_bound.append(fix)
        elif action in _DSL_ONLY_JURY_ACTIONS:
            dsl_only.append(fix)
        else:
            jury_bound.append(fix)
    return dsl_only, jury_bound


def _apply_high_level_instructions(ttl: str, instructions: list[dict]) -> str:
    """:func:`_apply_dsl_instructions` 의 TTL-only 래퍼 (기존 호출부 호환).

    통계가 필요한 호출부는 :func:`_apply_dsl_instructions` 를 직접 쓴다.
    """
    return _apply_dsl_instructions(ttl, instructions)["ttl"]


def _apply_dsl_instructions(ttl: str, instructions: list[dict]) -> dict:
    """고수준 지시를 결정론적으로 T-Box에 적용하고 **적용/폐기 내역**을 반환한다.

    Returns:
        ``{"ttl": str, "applied": [...], "skipped": [...], "failed": [...],
        "unknown": [...]}``. 각 리스트 항목은 ``{"action", "reason", "target"}``.

        ``skipped`` = 의도된 거부(예약어·중복·필수 필드 누락 등 가드 발동),
        ``failed`` = 예외, ``unknown`` = 이 함수가 모르는 action.

    ## 왜 dict 인가 (2026-08-26)

    예전에는 ``return g.serialize(...)`` 로 **TTL 문자열만** 돌려줬다. 그래서 지시가
    버려져도 호출부·응답·``round_log`` 어디에도 흔적이 없었다. 실측: 이 함수 안에
    ``applied`` 를 세는 코드는 15군데인데 **폐기를 세는 코드는 0군데**였고, 폐기
    경로 11곳은 로그만 남겼다 (그중 2곳은 ``logger.debug`` — 배포 로그 레벨이
    INFO 이므로 보이지 않는다).

    바로 옆의 ``jury_fixes.apply_jury_fixes`` 는 같은 성격의 엔진인데
    ``applied/skipped/failed/noop`` 를 반환하고 그 주석에 이렇게 적혀 있다 —
    *"실패를 카운터로만 남기면 조용히 유실된다"*. **같은 교훈이 한쪽 엔진에만**
    적용돼 있었다. S2 에서 "LLM 지시가 조용히 폐기됨" 이 네 번 재발한 지점이 전부
    이 함수다 (유령 IRI 183건 / 외래 prefix 뭉갬 42 트리플 / ``_to_node`` 101건 /
    Jury 키 불일치 37건). 그 진단 비용은 전부 이 반환 계약에서 나왔다.

    지원 action (P2 확장):
    - add_object_property: name, domain, range, inverse, label_ko
    - add_datatype_property: name, domain, range(xsd 타입), label_ko
    - add_subclass: child, parent
    - remove_class: name
    - add_triple / remove_triple: 저수준 호환
    - add_restriction: target_class, on_property, type(min|max|exact|someValuesFrom|hasValue|allValuesFrom), value
    - add_disjoint_classes: members (list)
    - add_functional_property: name
    - add_inverse_functional_property: name
    - add_transitive_property: name
    - rename_class / rename_property: old, new (해당 URI를 쓰는 모든 트리플 치환)
    - merge_classes: into, from (list) — from의 인스턴스 정의를 into로 rename
    - add_equivalent_class_union: parent, members (list) — unionOf로 묶어 equivalentClass
    """
    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import RDFS as _RDFS
    from rdflib import XSD as _XSD
    from rdflib import BNode as _BNode
    from rdflib.collection import Collection as _Collection

    g = _new_graph()
    g.parse(data=ttl, format="turtle")
    # **정규화 네임스페이스**: LLM 이 DSL 값에 prefixed name (``steel:Foo``) 이나 완전
    # IRI 를 넣어 보내면 raw ``Namespace[...]`` 는 그것을 그대로 이어붙여
    # ``http://…#steel:Foo`` 라는 **별개 리소스** 를 만든다. 정상 IRI 와 다른
    # 리소스이므로 그렇게 만들어진 클래스·프로퍼티는 어디에서도 참조되지 않는
    # 유령이 된다.
    #
    # 2026-08-09 실측: 한 번의 S2 실행에서 이 경로로 이중 접두사 IRI 183건이
    # 생성돼 ObjectProperty 229개 중 101개가 사용 불가 상태였다 (실사용 128개).
    # 즉 Jury/Validator 가 지시한 구조 수정이 **적용된 것처럼 보이지만 무효** 였다.
    # DSL 값은 local name 이 계약이지만 LLM 출력을 신뢰할 수 없으므로, 네임스페이스
    # 경계에서 한 번만 벗겨낸다 (28개 호출 지점을 개별 수정하는 대신).
    steel_ns = _GraphAwareNamespace(DOMAIN_NS, g)
    bind_namespaces(g)
    applied = 0
    applied_log: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    unknown: list[dict] = []

    def _target_of(instr: dict) -> str:
        """지시가 가리키는 대상 이름 — 사유 목록에서 무엇이 버려졌는지 알아야 한다.

        두 DSL 의 필드 이름이 다르므로(Architect: ``name``/``target_class``,
        Jury: ``property``/``class``) 양쪽을 본다. 하나만 보면 한쪽 엔진의 지시가
        전부 ``""`` 로 보고돼 목록이 무의미해진다.
        """
        for key in ("name", "target_class", "property", "class", "child",
                    "old", "into", "parent", "subject"):
            val = instr.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:80]
        members = instr.get("members")
        if isinstance(members, list) and members:
            return ", ".join(str(m) for m in members[:3])[:80]
        return ""

    def _skip(instr: dict, reason: str) -> None:
        """가드가 의도적으로 거부한 지시를 기록한다 (로그만 남기지 않는다)."""
        skipped.append({
            "action": instr.get("action", ""),
            "target": _target_of(instr),
            "reason": reason,
        })

    def _xsd_of(name: str):
        """xsd:string 같은 DP range 문자열을 URIRef로 변환."""
        if ":" in name:
            pfx, local = name.split(":", 1)
            if pfx == "xsd":
                return getattr(_XSD, local, _XSD.string)
        return getattr(_XSD, name, _XSD.string)

    def _add_dsl_labels(uri, raw_name: str, label_ko_raw: str | None) -> None:
        """DSL 로 만든 엔티티에 라벨/주석 주입 — **해석된 IRI** 기준으로.

        예전에는 원본 DSL 문자열을 그대로 ``Literal(name, lang="en")`` 에 넣어
        prefix 가 라벨로 새어나왔다 (실측 117건). 그리고 ``label_ko`` 가 없으면
        같은 원본 문자열이 @ko 라벨과 rdfs:comment 에까지 들어갔다.

        이미 같은 언어의 라벨이 있으면 **덮어쓰지 않는다**. 라운드 반복이나
        S3 의 유령 병합으로 사람이 다듬은 라벨(예: ``'waste quantity'``)이
        기계 합성값으로 회귀하는 것을 막는다.
        """
        existing = {
            getattr(o, "language", None)
            for o in g.objects(uri, _RDFS.label) if isinstance(o, Literal)
        }
        if "en" not in existing:
            g.add((uri, _RDFS.label, Literal(label_for_entity(uri), lang="en")))
        if "ko" not in existing:
            # @ko 는 SME 가 준 값만 신뢰한다. 없으면 영문 합성 라벨을 placeholder
            # 로 두고 (improve_tbox 의 _KO_HINTS 가 나중에 대체), 원본 DSL
            # 문자열은 절대 쓰지 않는다.
            ko = (label_ko_raw or "").strip() or label_for_entity(uri)
            g.add((uri, _RDFS.label, Literal(ko, lang="ko")))
        if label_ko_raw and not list(g.objects(uri, _RDFS.comment)):
            g.add((uri, _RDFS.comment, Literal(label_ko_raw, lang="ko")))

    def _rename_in_all_triples(old_uri, new_uri):
        """old_uri를 사용한 모든 위치(s/p/o)를 new_uri로 치환."""
        triples = list(g.triples((old_uri, None, None)))
        for s, p, o in triples:
            g.remove((s, p, o))
            g.add((new_uri, p, o))
        triples = list(g.triples((None, None, old_uri)))
        for s, p, o in triples:
            g.remove((s, p, o))
            g.add((s, p, new_uri))
        # predicate 위치
        triples = list(g.triples((None, old_uri, None)))
        for s, p, o in triples:
            g.remove((s, p, o))
            g.add((s, new_uri, o))

    for instr in instructions:
        action = instr.get("action", "")
        # 적용 성공은 ``applied`` 증가로 판정한다. 15개 분기에 개별 기록을 넣으면
        # 새 action 을 추가할 때 빠뜨리고, 그 누락이 "적용됐는데 목록에 없음" 으로
        # 조용히 나타난다 — 이 함수가 겪은 실패 유형과 같다.
        applied_before = applied
        try:
            if action == "add_object_property":
                name = instr["name"]
                if is_reserved_local(name):
                    logger.warning(
                        "add_object_property: reserved local name '%s' 건너뜀 "
                        "(owlready2/HermiT 충돌 방지)",
                        name,
                    )
                    _skip(instr, "reserved local name (owlready2/HermiT 충돌)")
                    continue
                op_uri = steel_ns[name]
                dom_uri = steel_ns[instr["domain"]]
                rng_uri = steel_ns[instr["range"]]
                # OP 추가
                g.add((op_uri, _RDF.type, _OWL.ObjectProperty))
                g.add((op_uri, _RDFS.domain, dom_uri))
                g.add((op_uri, _RDFS.range, rng_uri))
                _add_dsl_labels(op_uri, name, instr.get("label_ko"))
                _record_op_fk_source(g, op_uri, instr.get("fk_column"))
                # inverse
                inv_name = instr.get("inverse")
                if inv_name and is_reserved_local(inv_name):
                    logger.warning(
                        "add_object_property inverse: reserved '%s' 건너뜀", inv_name,
                    )
                    inv_name = None
                if inv_name:
                    inv_uri = steel_ns[inv_name]
                    g.add((inv_uri, _RDF.type, _OWL.ObjectProperty))
                    g.add((inv_uri, _RDFS.domain, rng_uri))
                    g.add((inv_uri, _RDFS.range, dom_uri))
                    _add_dsl_labels(inv_uri, inv_name, None)
                    g.add((op_uri, _OWL.inverseOf, inv_uri))
                    g.add((inv_uri, _OWL.inverseOf, op_uri))
                applied += 1

            elif action == "add_subclass":
                child_uri = steel_ns[instr["child"]]
                parent_uri = steel_ns[instr["parent"]]
                # self-loop 방지: Jury 가 iof-core:X 의 자식으로 steel:X (동명)
                # 를 넣으면 parent 가 steel_ns 로 강제돼 steel:X ⊑ steel:X 자기
                # 순환이 생긴다(2026-06-23). child==parent 면 skip.
                if child_uri != parent_uri:
                    g.add((child_uri, _RDFS.subClassOf, parent_uri))
                    applied += 1

            elif action == "remove_class":
                cls_uri = steel_ns[instr["name"]]
                for p, o in list(g.predicate_objects(cls_uri)):
                    g.remove((cls_uri, p, o))
                for s, p in list(g.subject_predicates(cls_uri)):
                    g.remove((s, p, cls_uri))
                applied += 1

            elif action in ("add_triple", "remove_triple"):
                try:
                    s = _dsl_term(instr.get("subject"), g, as_iri=True)
                    p = _dsl_term(instr.get("predicate"), g, as_iri=True)
                    raw_obj = instr.get("object")
                    # dcterms:source 값은 CSV 헤더와 직접 비교되므로 Turtle 표기가
                    # 섞이면 매칭이 영구히 실패한다 (실측: `"Event_ID"^^xsd:string`).
                    if p == _DCTERMS_NS.source and isinstance(raw_obj, str):
                        cleaned = clean_source_column(raw_obj)
                        o = Literal(cleaned) if cleaned else None
                    elif (
                        action == "add_triple"
                        and isinstance(raw_obj, str)
                        and _ANON_EXPR_RE.match(raw_obj)
                    ):
                        # Turtle 익명 노드 표현식 (`[ a owl:Restriction ; … ]`).
                        # ``_dsl_term`` 은 이것을 평문 Literal 로 격하시키고 "적용 성공"
                        # 을 보고했다 — 배포 T-Box 의 equivalentClass 40건 중 15건이
                        # 그렇게 저장된 Turtle 소스 문자열이다 (OWL DL 위반, 추론 무효).
                        # 구조로 파싱하고, 실패하면 **문자열로 저장하지 않고** skip 한다.
                        o = _parse_anon_expression(raw_obj, g)
                        if o is None:
                            _skip(instr, "익명 노드 표현식 파싱 실패 (Turtle 문법 오류)")
                            continue
                    else:
                        o = _dsl_term(raw_obj, g)
                    if s is None or p is None or o is None:
                        logger.warning("add/remove_triple: subject/predicate/object 누락, 건너뜀")
                        _skip(instr, "subject/predicate/object 중 해석 불가 항목")
                        continue
                    if action == "add_triple":
                        g.add((s, p, o))
                    else:
                        g.remove((s, p, o))
                    applied += 1
                except Exception as exc:
                    logger.warning("Triple 적용 실패: %s — %s", instr, exc)

            elif action == "add_datatype_property":
                name = instr["name"]
                if is_reserved_local(name):
                    logger.warning(
                        "add_datatype_property: reserved local name '%s' 건너뜀 "
                        "(owlready2/HermiT 충돌 방지). 도메인-특화 이름 권장 "
                        "(예: alarmType, equipmentLocation).",
                        name,
                    )
                    _skip(instr, "reserved local name (owlready2/HermiT 충돌)")
                    continue
                dp_uri = steel_ns[name]
                dom_uri = steel_ns[instr["domain"]]
                rng_uri = _xsd_of(instr.get("range", "xsd:string"))
                g.add((dp_uri, _RDF.type, _OWL.DatatypeProperty))
                g.add((dp_uri, _RDFS.domain, dom_uri))
                g.add((dp_uri, _RDFS.range, rng_uri))
                _add_dsl_labels(dp_uri, name, instr.get("label_ko"))
                # 출처 CSV 컬럼 — A-Box 가 컬럼↔DP 를 잇는 확정 근거
                # (04-property-rules.md 의 "DatatypeProperty source column" 절).
                # 라운드 수정으로 추가되는 DP 도 초안 DP 와 동일하게 표기해야
                # Step 12e 게이트를 통과한다.
                # LLM 은 이 필드에 Turtle 표기를 담아 보낸다 ("\"X\"^^xsd:string").
                # A-Box 는 값을 CSV 헤더와 직접 비교하므로 그대로 쓰면 매칭이
                # 영구히 실패하고 컬럼이 조용히 폴백으로 떨어진다 (실측 34건).
                source_col = clean_source_column(instr.get("source") or "")
                if source_col:
                    g.add((dp_uri, _DCTERMS_NS.source, Literal(source_col)))
                else:
                    logger.warning(
                        "add_datatype_property: '%s' 에 source (CSV 컬럼) 누락 — "
                        "A-Box 가 이름 추측으로 매칭을 시도하며 실패 시 해당 컬럼이 "
                        "적재되지 않는다",
                        name,
                    )
                applied += 1

            elif action == "add_restriction":
                # Restriction BNode: subClassOf 체인에 붙인다.
                # 동일 (target, on_property, type, value) 가 이미 존재하면
                # 새로 만들지 않고 건너뛴다. 중복 restriction 은 skolemize 후
                # 수많은 별개 클래스로 취급되어 OWL RL closure 폭발을 유발한다.
                target = steel_ns[instr["target_class"]]
                on_prop = steel_ns[instr["on_property"]]
                r_type = instr.get("type", "someValuesFrom")
                value = instr.get("value")

                # 기존 restriction 조회 — target 의 subClassOf parent 중 동일한
                # (onProperty, restriction_type, target_value) 이 있는지 확인.
                #
                # 루프 변수(on_prop/r_type/value)를 **기본 인자로 바인딩** 한다.
                # 이 클로저는 같은 iteration 안에서만 쓰이므로 late-binding 버그가
                # 실제로 발생하진 않지만, 바인딩해 두면 나중에 누가 호출을 루프
                # 밖으로 옮겨도 조용히 잘못된 값을 읽지 않는다 (ruff B023).
                # 같은 파일의 ``_on_phase`` 가 이미 이 관행을 쓴다.
                def _restr_value_matches(
                    rest_node, on_prop=on_prop, r_type=r_type, value=value,
                ) -> bool:
                    if (rest_node, _OWL.onProperty, on_prop) not in g:
                        return False
                    if r_type == "someValuesFrom":
                        return (rest_node, _OWL.someValuesFrom, steel_ns[value]) in g if value else False
                    if r_type == "allValuesFrom":
                        return (rest_node, _OWL.allValuesFrom, steel_ns[value]) in g if value else False
                    if r_type == "hasValue":
                        return any(
                            (rest_node, _OWL.hasValue, obj) in g
                            for obj in (Literal(value), steel_ns[str(value).split(":", 1)[-1]])
                        )
                    if r_type in ("min", "minCardinality"):
                        return (rest_node, _OWL.minCardinality,
                                Literal(int(value), datatype=_XSD.nonNegativeInteger)) in g
                    if r_type in ("max", "maxCardinality"):
                        return (rest_node, _OWL.maxCardinality,
                                Literal(int(value), datatype=_XSD.nonNegativeInteger)) in g
                    if r_type in ("exact", "cardinality"):
                        return (rest_node, _OWL.cardinality,
                                Literal(int(value), datatype=_XSD.nonNegativeInteger)) in g
                    return False

                already_exists = False
                for parent in g.objects(target, _RDFS.subClassOf):
                    if (parent, _RDF.type, _OWL.Restriction) in g and _restr_value_matches(parent):
                        already_exists = True
                        break
                if already_exists:
                    logger.debug(
                        "add_restriction: 중복 건너뜀 target=%s prop=%s type=%s value=%s",
                        instr["target_class"], instr["on_property"], r_type, value,
                    )
                    _skip(instr, f"동일 Restriction 이 이미 존재 ({r_type})")
                    continue

                rest = _BNode()
                g.add((rest, _RDF.type, _OWL.Restriction))
                g.add((rest, _OWL.onProperty, on_prop))
                if r_type == "someValuesFrom" and value:
                    g.add((rest, _OWL.someValuesFrom, steel_ns[value]))
                elif r_type == "allValuesFrom" and value:
                    g.add((rest, _OWL.allValuesFrom, steel_ns[value]))
                elif r_type == "hasValue" and value:
                    # value가 "true"/"false"/숫자면 Literal
                    if value in ("true", "false"):
                        g.add((rest, _OWL.hasValue, Literal(value == "true")))
                    elif str(value).isdigit():
                        g.add((rest, _OWL.hasValue, Literal(int(value))))
                    elif ":" in str(value):
                        pfx, local = str(value).split(":", 1)
                        g.add((rest, _OWL.hasValue,
                               steel_ns[local] if pfx == NS_PREFIX else Literal(value)))
                    else:
                        g.add((rest, _OWL.hasValue, Literal(value)))
                elif r_type in ("min", "minCardinality"):
                    g.add((rest, _OWL.minCardinality,
                           Literal(int(value), datatype=_XSD.nonNegativeInteger)))
                elif r_type in ("max", "maxCardinality"):
                    g.add((rest, _OWL.maxCardinality,
                           Literal(int(value), datatype=_XSD.nonNegativeInteger)))
                elif r_type in ("exact", "cardinality"):
                    g.add((rest, _OWL.cardinality,
                           Literal(int(value), datatype=_XSD.nonNegativeInteger)))
                g.add((target, _RDFS.subClassOf, rest))
                applied += 1

            elif action == "add_disjoint_classes":
                members = instr.get("members", [])
                if len(members) < 2:
                    logger.warning("add_disjoint_classes: members<2, 건너뜀")
                    _skip(instr, f"members 가 {len(members)}개 (2개 이상 필요)")
                    continue
                dj = _BNode()
                g.add((dj, _RDF.type, _OWL.AllDisjointClasses))
                coll_head = _BNode()
                _Collection(g, coll_head, [steel_ns[m] for m in members])
                g.add((dj, _OWL.members, coll_head))
                applied += 1

            elif action == "add_functional_property":
                name = instr["name"]
                if is_reserved_local(name):
                    logger.warning("add_functional_property: reserved '%s' 건너뜀", name)
                    _skip(instr, "reserved local name (owlready2/HermiT 충돌)")
                    continue
                g.add((steel_ns[name], _RDF.type, _OWL.FunctionalProperty))
                applied += 1

            elif action == "add_inverse_functional_property":
                # Intentionally rejected. The OWL 2 RL reasoner used downstream
                # (`reasonable`) mis-implements prp-ifp and collapses every
                # subject carrying an IFP into one equivalence class, even for
                # distinct object values. Uniqueness constraints belong in
                # SHACL; IFP has no safe home in this pipeline.
                logger.warning("add_inverse_functional_property '%s' ignored — "
                               "IFP triggers reasoner sameAs explosion", instr.get("name"))
                _skip(instr, "IFP 는 추론기 sameAs 폭발을 유발해 의도적으로 거부")
                continue

            elif action == "add_transitive_property":
                name = instr["name"]
                if is_reserved_local(name):
                    logger.warning("add_transitive_property: reserved '%s' 건너뜀", name)
                    _skip(instr, "reserved local name (owlready2/HermiT 충돌)")
                    continue
                g.add((steel_ns[name], _RDF.type, _OWL.TransitiveProperty))
                applied += 1

            elif action == "rename_class":
                new_name = instr["new"]
                if is_reserved_local(new_name):
                    logger.warning(
                        "rename_class: target '%s' 가 reserved — 건너뜀", new_name,
                    )
                    _skip(instr, "새 이름이 reserved local name")
                    continue
                old_uri = steel_ns[instr["old"]]
                new_uri = steel_ns[new_name]
                _rename_in_all_triples(old_uri, new_uri)
                # Class 타입 선언 보장
                g.add((new_uri, _RDF.type, _OWL.Class))
                applied += 1

            elif action == "rename_property":
                new_name = instr["new"]
                if is_reserved_local(new_name):
                    logger.warning(
                        "rename_property: target '%s' 가 reserved — 건너뜀", new_name,
                    )
                    _skip(instr, "새 이름이 reserved local name")
                    continue
                old_uri = steel_ns[instr["old"]]
                new_uri = steel_ns[new_name]
                _rename_in_all_triples(old_uri, new_uri)
                applied += 1

            elif action == "merge_classes":
                into = steel_ns[instr["into"]]
                for from_name in instr.get("from", []):
                    from_uri = steel_ns[from_name]
                    _rename_in_all_triples(from_uri, into)
                g.add((into, _RDF.type, _OWL.Class))
                applied += 1

            elif action == "add_equivalent_class_union":
                parent = steel_ns[instr["parent"]]
                members = instr.get("members", [])
                if len(members) < 2:
                    logger.warning("add_equivalent_class_union: members<2 건너뜀")
                    _skip(instr, f"members 가 {len(members)}개 (2개 이상 필요)")
                    continue
                eq = _BNode()
                g.add((parent, _OWL.equivalentClass, eq))
                g.add((eq, _RDF.type, _OWL.Class))
                union_head = _BNode()
                _Collection(g, union_head, [steel_ns[m] for m in members])
                g.add((eq, _OWL.unionOf, union_head))
                applied += 1

            else:
                if action:
                    # 예전에는 logger.debug 였다 — 배포 로그 레벨이 INFO 이므로
                    # (server.py) 어느 action 이 버려졌는지 **아무 흔적도 없었다**.
                    # 미지원 action 은 두 DSL 의 이름 불일치를 드러내는 신호이므로
                    # (실측: 리뷰어 fix 제안 중 상당수가 어느 엔진도 모르는 이름)
                    # WARNING 으로 올리고 반환값에도 싣는다.
                    logger.warning("지원하지 않는 action: %s — %s", action, instr)
                    unknown.append({
                        "action": action,
                        "target": _target_of(instr),
                        "reason": "이 엔진이 모르는 action 이름",
                    })

        except (KeyError, Exception) as e:
            logger.warning("지시 적용 실패 (%s): %s — %s", action, instr, e)
            failed.append({
                "action": action,
                "target": _target_of(instr),
                "reason": f"{type(e).__name__}: {str(e)[:160]}",
            })
        else:
            if applied > applied_before:
                applied_log.append({
                    "action": action,
                    "target": _target_of(instr),
                })

    # 폐기를 **카운터가 아니라 사유 목록으로** 남긴다. 정수 3개만 있으면
    # "{applied:4, failed:37} 이 왜인지" 를 알 수 없다 (jury_fixes 가 같은 이유로
    # failed_details 를 추가했다).
    dropped = len(skipped) + len(failed) + len(unknown)
    if dropped:
        logger.info(
            "고수준 지시 %d/%d건 적용 — 폐기 %d건 (skipped=%d failed=%d unknown=%d)",
            applied, len(instructions), dropped,
            len(skipped), len(failed), len(unknown),
        )
    else:
        logger.info("고수준 지시 %d/%d건 적용", applied, len(instructions))
    return {
        "ttl": g.serialize(format="turtle"),
        "applied": applied_log,
        "applied_count": applied,
        "requested": len(instructions),
        "skipped": skipped,
        "failed": failed,
        "unknown": unknown,
    }


def _architect_revise(
    ttl: str, validator_review: dict, sme_review: dict, round_num: int,
    veto_targets: list[str] | None = None,
    dsl_stats_out: dict | None = None,
) -> str:
    """Ontology Architect — 챌린지를 반영하여 T-Box를 수정.

    [개선 3] 고수준 지시 방식: LLM이 "무엇을 추가/삭제할지"만 결정, 코드가 TTL 생성.

    Args:
        veto_targets: #1 Veto Lock — 2라운드 연속 잔존한 critical/high 이슈의
            category:target 키 목록. 이 target은 반드시 수정 대상이 되며
            "수정 거부" 옵션을 프롬프트에서 제거한다.
        dsl_stats_out: 주면 DSL 적용/폐기 통계를 여기에 **채워 넣는다**
            (``applied``/``skipped``/``failed``/``unknown`` + ``dropped_details``).
            반환값을 dict 로 바꾸면 호출부 계약이 깨지므로 out-param 을 쓴다.

            이 경로가 S2 최대 폐기 지점이다 — Architect DSL 은 라운드마다 수십 건을
            보내는데 예전에는 통계가 로그로만 나갔고, 폐기 사유는 ``round_log`` 에
            남지 않아 "왜 T-Box 가 안 바뀌었나" 를 사후에 알 수 없었다.
    """
    v_issues = json.dumps(validator_review.get("issues", []), ensure_ascii=False, indent=2)
    s_issues = json.dumps(sme_review.get("issues", []), ensure_ascii=False, indent=2)
    veto_block = ""
    if veto_targets:
        veto_block = (
            "\n## ⛔ Veto Lock (2라운드 연속 잔존 critical/high — 반드시 수정)\n"
            + "\n".join(f"- {t}" for t in veto_targets)
            + "\n\n**위 target은 수정 거부 불가**. DSL action으로 반드시 처리하세요.\n"
            "타당한 반박이 있더라도 현재 라운드에서는 구조적 수정을 우선 시도하세요.\n"
        )

    # 정적 prefix(역할 + 수정 규칙 + DSL 카탈로그 + 예시)는 라운드마다 동일하므로
    # Bedrock prompt caching 대상. 라운드/veto/이슈/TTL 만 variable.
    from tools.multi_agent_prompts import architect_static_prefix
    cached_prefix = architect_static_prefix(NS_PREFIX)

    variable_prompt = f"""## 수정 라운드: {round_num}
{veto_block}
## Semantic Validator 지적 사항 (논리 관점)
{v_issues}

## Manufacturing SME 지적 사항 (현장 관점)
{s_issues}

## 현재 T-Box TTL (참고용)
{_format_ttl_block(ttl)}

{_format_declaration_inventory(ttl)}

JSON만 출력하세요:"""

    # P9: Architect temperature = 0.2 (결정적)
    #
    # max_tokens 는 리뷰어와 **같은 예산**을 쓴다. 예전에는 8,192 하드코딩이었고
    # 리뷰어만 16,000 이었다 — 그런데 긴 JSON 목록을 내야 하는 쪽은 Architect 다
    # (Jury 가 라운드당 52~60건을 요청하므로 유사 규모의 instructions 배열이 필요).
    # thinking 모델(opus-5/sonnet-5)은 예산에서 thinking 을 먼저 쓰므로 남는 몫이
    # 더 줄어든다. 상향 비용은 없다: 출력 토큰은 **실제 생성분만** 청구되므로 상한을
    # 올려도 평소 지출은 그대로고, 절단 시의 재호출 1회가 없어져 오히려 절약된다
    # (_REVIEW_MAX_TOKENS 주석과 같은 근거).
    text = _invoke_bedrock(
        variable_prompt, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.2,
        cached_prefix=cached_prefix, agent_role="architect",
    )

    # JSON 추출 — **공용 추출기**를 쓴다 (사본 금지).
    #
    # 예전에는 여기에 fence 정규식 한 줄만 있었다. ``_extract_json_candidate`` 는
    # 같은 파일 안에서 3단 복구(fence → raw_decode → 괄호 매칭)를 하는데 Architect
    # 만 그것을 쓰지 않았다. 실측 비교 (LLM 이 실제로 내는 7가지 형태):
    #
    #     케이스                자체 정규식   공용 추출기
    #     fence 정상               OK          OK
    #     fence 없음               OK          OK
    #     뒤에 해설               **FAIL**      OK
    #     앞에 해설               **FAIL**      OK
    #     앞뒤 해설               **FAIL**      OK
    #     fence 미닫힘(절단)       **FAIL**      OK
    #     thinking 후 fence        OK          OK
    #
    # 4/7 에서 실패하고, 실패하면 아래 ``except`` 가 **전체 TTL 폴백** 으로 떨어져
    # 그 라운드 지시가 전량 소실된다. 2026-08-27 검증 실행에서 3라운드 전부 이
    # 경로였다 (outcome=ttl_fallback, 트리플 3690→3690→3674, no_progress 2연속 —
    # 변화는 전부 Jury 결정적 수정이 만들었고 Architect 기여는 0).
    text_stripped = _extract_json_candidate(text)

    def _note(outcome: str, **extra) -> None:
        """어느 반환 경로를 탔는지 out-param 에 남긴다.

        **왜 모든 경로에 필요한가** (2026-08-27 실측): 처음에는 DSL 경로에만 통계를
        채웠는데, 검증 실행에서 ``revision.dsl`` 이 3라운드 전부 비었다. Architect 가
        DSL 이 아니라 **폴백 경로** 를 탔기 때문이다 (트리플 3690 → 3690 → 3674,
        no_progress 2연속, 변화는 전부 Jury 결정적 수정이 만들었다).

        즉 "지시가 몇 건 버려졌나" 보다 먼저 답해야 할 질문이 **"Architect 가 지시를
        내기는 했나"** 였고, 그것을 알려주는 필드가 없었다. outcome 축이 없으면
        no_progress 의 원인을 영원히 구분할 수 없다:

          dsl_applied     지시를 냈고 적용됐다 (몇 건 버려졌는지는 skipped/failed 로)
          empty           LLM 이 빈 지시 목록을 반환했다
          legacy_patches  구식 patch 형식으로 왔다
          json_error      응답이 JSON 이 아니어서 전체 TTL 폴백을 시도했다
          ttl_fallback    폴백이 성공했다 (지시는 전량 소실)
          ttl_unusable    폴백도 실패해 원본을 유지했다
        """
        if dsl_stats_out is None:
            return
        dsl_stats_out["outcome"] = outcome
        dsl_stats_out.update(extra)

    try:
        data = json.loads(text_stripped)
        instructions = data.get("instructions", data.get("patches", []))
        if not instructions:
            logger.warning("Architect가 빈 지시 목록 반환, 원본 유지")
            _note("empty", requested=0, applied=0)
            return ttl

        # 고수준 지시 포함 여부로 분기 (P2 확장 DSL 포함)
        _HIGH_LEVEL_ACTIONS = {
            "add_object_property", "add_datatype_property", "add_subclass",
            "remove_class", "add_restriction", "add_disjoint_classes",
            "add_functional_property", "add_inverse_functional_property",
            "add_transitive_property", "rename_class", "rename_property",
            "merge_classes", "add_equivalent_class_union",
            # add_triple/remove_triple도 _apply_high_level_instructions에서 처리
            "add_triple", "remove_triple",
        }
        has_high_level = any(i.get("action") in _HIGH_LEVEL_ACTIONS
                             for i in instructions)
        if has_high_level:
            logger.info("고수준 지시 %d건 수신, 결정론적 적용", len(instructions))
            result = _apply_dsl_instructions(ttl, instructions)
            if dsl_stats_out is not None:
                dsl_stats_out.update({
                    "applied": result["applied_count"],
                    "requested": result["requested"],
                    "skipped": len(result["skipped"]),
                    "failed": len(result["failed"]),
                    "unknown": len(result["unknown"]),
                })
                dropped = (result["skipped"] + result["failed"]
                           + result["unknown"])
                if dropped:
                    dsl_stats_out["dropped_details"] = dropped[:20]
            _note("dsl_applied")
            return result["ttl"]
        else:
            # 기존 패치 호환
            logger.info("기존 패치 %d건 수신, 적용", len(instructions))
            _note("legacy_patches", requested=len(instructions))
            return _apply_patches(ttl, instructions)

    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("지시 JSON 파싱 실패 (%s), 전체 TTL 폴백 시도", exc)
        # fence 유무와 관계없이 extractor 로 prelude 제거. Sonnet 4.6 계열이
        # ```turtle fence 없이 reasoning 뒤에 TTL 을 이어붙이는 회귀 대응.
        from tools.tbox_generation import _extract_ttl_from_markdown
        fallback = _extract_ttl_from_markdown(text)
        try:
            g = _new_graph()
            g.parse(data=fallback, format="turtle")
            logger.info("전체 TTL 폴백 성공 (%d 트리플)", len(g))
            # 이 경로는 **지시가 전량 소실** 된 것이다 — LLM 이 JSON 대신 TTL 을 냈고
            # 그 TTL 을 그대로 받았으므로, 리뷰어가 요구한 개별 수정이 반영됐는지
            # 알 수 없다. 실측에서 이 경로가 no_progress 2연속의 원인이었다.
            _note("ttl_fallback", parse_error=f"{type(exc).__name__}: {str(exc)[:120]}")
            return fallback
        except Exception:
            logger.warning("전체 TTL 폴백도 실패, 원본 유지")
            _note("ttl_unusable",
                  parse_error=f"{type(exc).__name__}: {str(exc)[:120]}")
            return ttl


def _jury_decide(
    ttl: str, validator_result: dict, sme_result: dict,
    architect_history: list, debate_log: list, metrics: dict | None = None,
    veto_targets: list[str] | None = None,
    cq_runtime: dict | None = None,
) -> dict:
    """P4 Jury — Architect의 자기평가를 대신하는 제3자 심판.

    Architect가 자신의 작품에 우호적일 수 있으므로 Jury가 독립적으로:
    1. Validator/SME 잔여 이슈를 재평가 (accept/reject/partial)
    2. Architect의 수정 이력이 정당한지 판단
    3. 현재 TTL의 production_ready 여부 최종 결정
    4. 필요 시 Architect에게 강제할 수정 DSL 반환

    Returns:
        {"production_ready": bool, "required_fixes": [dsl_action...],
         "decisions": [...], "rationale": str}
    """
    remaining_v = json.dumps(validator_result.get("issues", []), ensure_ascii=False, indent=2)
    remaining_s = json.dumps(sme_result.get("issues", []), ensure_ascii=False, indent=2)
    history_summary = json.dumps(
        [{"round": r.get("round"),
          "v_issues": r.get("validator", {}).get("issues_count"),
          "s_issues": r.get("sme", {}).get("issues_count")}
         for r in debate_log], ensure_ascii=False)
    metrics_block = _format_metrics_block(metrics) if metrics else ""

    veto_block = ""
    if veto_targets:
        veto_block = (
            "\n## ⛔ Veto Lock 발동 (2라운드 연속 critical/high 잔존)\n"
            + "\n".join(f"- {t}" for t in veto_targets[:15])
            + "\n\n**위 target에 대해서는 반드시 `required_fixes` DSL action을 생성하세요.**\n"
            "Architect가 3라운드 동안 해결하지 못한 이슈이므로 Jury가 구조적 수정을 강제합니다.\n"
            "production_ready=true는 이 이슈들이 해소됐을 때만 허용됩니다.\n"
        )

    cq_block = ""
    if cq_runtime and cq_runtime.get("unanswerable"):
        unansw = cq_runtime["unanswerable"][:10]
        cq_block = (
            "\n## ⚠️ CQ Reachability 미달\n"
            f"CQ 커버리지: {cq_runtime.get('coverage_pct', 0)}%\n"
            "답변 불가 CQ (정적 그래프 분석):\n"
            + "\n".join(f"- {cid}: {reason}" for cid, reason in unansw)
            + "\n\n**위 CQ를 답변 가능하게 할 ObjectProperty/subClassOf를 `required_fixes`에 포함하세요.**\n"
        )

    # 정적 prefix(역할 도입부 + 판단 원칙 + 출력 형식)는 매 Jury 호출 동일하므로
    # prompt caching 대상. 토론 경과/메트릭/veto/CQ/이슈/TTL 은 variable.
    from tools.multi_agent_prompts import jury_static_prefix
    cached_prefix = jury_static_prefix()

    variable_prompt = f"""## 토론 경과
{history_summary}

## 구조 메트릭 (코드 계산)
{metrics_block}
{veto_block}
{cq_block}
## Semantic Validator 잔여 이슈
{remaining_v}

## Manufacturing SME 잔여 이슈
{remaining_s}

## 현재 T-Box TTL
{_format_ttl_block(ttl)}

{_format_declaration_inventory(ttl)}

JSON만 출력하세요:"""

    # Jury는 균형 판단 — temperature 중간값
    text = _invoke_bedrock(
        variable_prompt, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.4,
        cached_prefix=cached_prefix, agent_role="jury",
    )

    def _retry_jury(retry_prompt_override: str) -> str:
        # 1차와 동일한 max_tokens (Validator/SME 와 같은 이유 — 재시도가 원본
        # 컨텍스트를 다시 포함하므로 응답 길이가 줄어들 근거가 없다).
        return _invoke_bedrock(
            retry_prompt_override, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.2,
            cached_prefix=cached_prefix, agent_role="jury",
        )

    # 재시도 프롬프트에 원본 컨텍스트(variable_prompt: TTL/Validator·SME 이슈/
    # 메트릭)를 다시 포함한다. 안내문만 보내면 모델이 빈 입력을 받아 "no TTL/
    # issues provided" 로 placeholder 를 생성하는 회귀가 발생한다.
    retry_prompt = (
        variable_prompt
        + "\n\n[재시도] 이전 응답이 JSON 파싱에 실패했습니다. 단일 JSON 객체 하나만 "
        "출력하세요.\n"
        "필드: decisions (list), jury_issues (list), required_fixes (list), "
        "rationale (string), production_ready (bool).\n"
        "JSON 만 출력하세요:"
    )
    result = _parse_review_json(
        text, "Jury",
        retry_invoker=_retry_jury, retry_prompt=retry_prompt,
    )
    logger.info(
        "[Jury] production_ready=%s, decisions=%d, jury_issues=%d, required_fixes=%d",
        result.get("production_ready"),
        len(result.get("decisions", [])),
        len(result.get("jury_issues", [])),
        len(result.get("required_fixes", [])),
    )
    return result


# ── T5: Compromise decision criteria helpers ────────────────────────────
#
# Fingerprint/persistence/priority 는 `_architect_compromise` 의 결정 기준을
# 코드화해 LLM 의 편차를 완화하기 위한 결정적 헬퍼. 같은 입력 → 같은 출력.
# 이슈 스키마: {severity, category, target, symptom, principle, impact, fix}.
# symptom 이 표준 text 필드. `_agent` 는 enrichment 시 외부에서 주입한다.


#: ``prefix:LocalName`` 형태의 식별자 추출. **ASCII 전용이다** — ``\w`` 를 쓰면
#: 한글 조사가 이름에 접착된다 (실측: ``equipmentStatusMaintenanceFlag의``).
_ENTITY_ATOM_RE = re.compile(r"([A-Za-z][A-Za-z0-9_-]*):([A-Za-z][A-Za-z0-9_]*)")

#: prefix 없이 쓰인 PascalCase 클래스명 (``EquipmentMaster``). prefixed name 이
#: 하나도 없을 때만 폴백으로 쓴다.
_PASCAL_ATOM_RE = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]+)+)\b")

#: 표준 어휘 prefix — 이쪽 이름은 **결함의 대상이 아니라 서술 수단** 이다.
#: ``owl:Restriction`` / ``rdfs:domain`` 을 원자로 넣으면 같은 결함이 표현에 따라
#: 갈린다. 도메인 prefix 는 여기 없으므로 그대로 원자가 된다 (도메인-중립).
_STD_VOCAB_PREFIXES = frozenset({
    "owl", "rdf", "rdfs", "xsd", "skos", "dcterms", "dc", "foaf", "sh",
})

#: 지문의 텍스트 축 절단 길이. **60자다 (200 아님).**
#:
#: 200자로 자르면 LLM 이 뒤쪽 문장을 조금 바꿀 때마다 지문이 갈린다 — 실측
#: (2026-08-22, 이번 실행 90건): 200자 축으로는 **2라운드 이상 잔존 0건** 이었는데
#: 이슈 수는 15→17→14→17 로 반복하고 있었다. 60자는 도입부(무엇이 문제인지)만
#: 보므로 같은 결함의 재표현을 흡수하면서도 서로 다른 결함은 가른다.
#:
#: 왜 텍스트 축을 아예 버리지 않는가 — **과잉 병합을 막는 유일한 장치다.**
#: 실측 비교 (같은 90건):
#:
#: ===================================  ======  =====  ============
#: 정규화 변형                            그룹수   2R+    증상 혼재
#: ===================================  ======  =====  ============
#: 현행 (텍스트 200자 + severity + raw)     90      0       0
#: category + 엔티티 (텍스트 버림)          71     15       **8**
#: category + 엔티티 + severity            75     12       **8**
#: **category + 엔티티 + 텍스트 60자**      82      8       **0**
#: ===================================  ======  =====  ============
#:
#: "증상 혼재" 는 서로 다른 결함이 한 그룹으로 병합된 수다. 텍스트를 버리면
#: ``logic:restriction`` 하나에 AirEmissionMonitoring / EquipmentMaster /
#: PurchaseOrder 의 **서로 다른 3개 결함** 이 합쳐진다 — age_rounds 숫자는 오르지만
#: persistence 신호가 거짓이 된다 (지표 매수).
_issue_text_fp_max = 60


def _issue_atoms(raw) -> tuple[str, ...]:
    """서술형 문자열에서 **엔티티 식별자만** 추출 (정렬·소문자·중복 제거).

    LLM 은 ``target`` 에 식별자가 아니라 문장을 넣는다 — 실측 90건 중 **54건**
    (예: ``"AirEmissionMonitoring rdfs:subClassOf 내 owl:Restriction 중복
    (diff로 확인됨)"``). 문장을 그대로 키에 쓰면 같은 결함이 라운드마다 다른
    키를 얻는다.

    추출 순서: ``prefix:Local`` → PascalCase → **단일 토큰 전체**.

    마지막 폴백이 필요한 이유: LLM 이 ``target`` 을 소문자로 적으면
    (``"equipmentmaster"``) 위 두 패턴이 모두 0건이라 엔티티 축이 비고, 그러면
    같은 결함이 대소문자만 다른 표기로 갈린다. 공백 없는 한 덩어리이고 서술이
    아닐 때만(길이 제한) 그것을 이름으로 받아들인다.

    정렬하는 이유: LLM 이 ``"A ↔ B"`` 와 ``"B ↔ A"`` 를 섞어 쓴다.
    """
    text = str(raw or "")
    # 표준 어휘(owl:Restriction / rdfs:domain …)는 **결함의 대상이 아니라 서술** 이다.
    # 포함하면 같은 결함이 "owl:Restriction 중복" 과 "rdfs:subClassOf 내 중복" 으로
    # 갈린다 (실측: 제외 시 반복 검출 8→9건, 과잉 병합은 여전히 0).
    found = [
        m.group(2) for m in _ENTITY_ATOM_RE.finditer(text)
        if m.group(1).lower() not in _STD_VOCAB_PREFIXES
    ]
    if not found:
        found = _PASCAL_ATOM_RE.findall(text)
    if not found:
        stripped = text.strip()
        # 공백이 없고 ASCII 식별자 문자로만 이뤄진 짧은 토큰 = 이름으로 본다.
        if stripped and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", stripped):
            found = [stripped]
    return tuple(sorted({x.lower() for x in found}))


def _issue_identity(issue: dict) -> tuple[str, tuple[str, ...], str]:
    """이슈의 동일성 3요소 → ``(category, 엔티티 원자, 텍스트 해시)``.

    :func:`_issue_fingerprint` 와 :func:`_issue_key` 가 **같은 판정** 을 쓰도록
    하는 공용 축이다. 예전에는 두 함수가 서로 다른 키를 만들어, 같은 실행에서
    ``persistent_issue_count=0`` 과 ``veto_lock`` 잔존 2건이 **모순** 됐다.

    텍스트 해시를 **항상** 포함한다 (원자가 있어도) — 그것이 과잉 병합을 막는
    장치다. 근거는 :data:`_issue_text_fp_max` 참조.

    해시로 만드는 이유: 원문을 키에 넣으면 제어문자·개행이 섞여 T-Box 각인
    (``_build_debate_status_literal``) 을 통해 RDF 직렬화를 깨뜨린다.
    """
    category = str(issue.get("category", "") or "").strip().lower()
    atoms = _issue_atoms(
        issue.get("target") or issue.get("affected_class") or issue.get("class"),
    )
    text = (
        issue.get("symptom", "")
        or issue.get("text", "")
        or issue.get("issue", "")
    )
    # 비-str 강제 변환 이유는 아래 _issue_fingerprint docstring 참조.
    normalized = re.sub(
        r"\s+|[^\w가-힣]", "", str(text or "")[:_issue_text_fp_max],
    ).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return category, atoms, digest


def _issue_fingerprint(issue: dict) -> str:
    """Issue 를 정규화된 해시로 변환 — 라운드 간 동일성 판정.

    :func:`_issue_identity` 3요소를 해시한다. **severity 는 넣지 않는다** —
    LLM 이 같은 결함의 심각도를 라운드마다 바꾸면(high↔medium) 지문이 갈려
    persistence 가 리셋된다. 심각도는 우선순위 가중치로 별도 반영된다
    (``_compute_issue_priority``).
    """
    # 비-str 방어는 ``_issue_identity`` 안에서 한다 — 그 함수가 지문·키 두 소비자의
    # 유일한 입력 경로이므로 거기서 막으면 3개 소비자(veto / persistence / 보고서)가
    # 함께 보호된다. 예전엔 이 자리에서 직접 슬라이싱해 ``symptom: 12345`` 같은 값에
    # TypeError 로 죽었고, 보고서의 blanket except 가 **정상 라운드 궤적 표까지**
    # 안내 HTML 로 강등했다 (2026-08-22 실측).
    category, atoms, digest = _issue_identity(issue)
    key = f"{category}|{','.join(atoms)}|{digest}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


#: 라운드 로그에 남길 이슈 수 상한 (역할별). 실측 라운드당 V 17 / S 11 이므로
#: 넉넉하다. 상한이 필요한 이유는 이 로그가 도구 응답 JSON 에 실려 나가기
#: 때문이다 — 무제한이면 응답이 커져 MCP stdio 를 끊는다 (S8 25MB 이력).
_ROUND_LOG_MAX_ISSUES = 40

#: 지문 계산과 사후 판독에 필요한 키만 보존한다. **지문 입력 키를 빠뜨리면
#: persistence 판정이 조용히 깨진다** — :func:`_issue_fingerprint` 가 읽는
#: (symptom|text|issue) / severity / (target|affected_class|class) 는 전부 필수다.
_ISSUE_KEEP_KEYS: tuple[str, ...] = (
    # 지문 입력 (하나라도 빠지면 age_rounds 가 리셋된다)
    "symptom", "text", "issue", "severity", "target", "affected_class", "class",
    # 사후 판독·우선순위 계산용
    "category", "action", "fix", "deferred_to_s3",
)

#: 서술형 필드 절단 길이. 지문은 앞 200자만 쓰므로 200 은 **지문-안전** 하다.
_ISSUE_TEXT_MAX = 200


def _slim_issue(issue: dict) -> dict:
    """이슈 dict → 지문 계산에 필요한 키만 남긴 사본 (장문은 절단).

    절단 길이가 :data:`_ISSUE_TEXT_MAX` = 200 인 것은 우연이 아니다 —
    :func:`_issue_fingerprint` 가 텍스트의 앞 200자만 해시하므로, 이 절단은
    지문을 바꾸지 않는다. 값을 줄이면 라운드 간 동일성 판정이 깨진다.
    """
    if not isinstance(issue, dict):
        return {}
    out: dict = {}
    for key in _ISSUE_KEEP_KEYS:
        if key not in issue:
            continue
        val = issue[key]
        out[key] = val[:_ISSUE_TEXT_MAX] if isinstance(val, str) else val
    return out


def _slim_issues(issues: list | None) -> list[dict]:
    """이슈 목록을 상한까지 슬림화. persistence 판정용 최소 표현."""
    if not isinstance(issues, list):
        return []
    return [_slim_issue(i) for i in issues[:_ROUND_LOG_MAX_ISSUES]
            if isinstance(i, dict)]


def _compute_issue_persistence(
    current_issues: list[dict],
    prior_rounds: list[dict],
) -> list[dict]:
    """각 이슈에 ``fingerprint`` + ``age_rounds`` 필드 부여.

    Args:
        current_issues: 이번 라운드 V/S 이슈 dict 목록.
        prior_rounds: 이전 debate_log entries (각 entry 는
            {"validator": {"issues": [...]}, "sme": {"issues": [...]}} 구조).

    Returns:
        current_issues 의 shallow copy. 각 dict 에
        {"fingerprint": <hex>, "age_rounds": <int>} 추가.
        연속성이 끊기면 age_rounds=1 로 리셋.
    """
    enriched: list[dict] = []
    for issue in current_issues:
        fp = _issue_fingerprint(issue)
        age = 1
        for prev in reversed(prior_rounds):
            prev_v = (prev.get("validator", {}) or {}).get("issues", []) or []
            prev_s = (prev.get("sme", {}) or {}).get("issues", []) or []
            prev_fps = {_issue_fingerprint(i) for i in (prev_v + prev_s)}
            if fp in prev_fps:
                age += 1
            else:
                break  # 연속성 끊김 — 이전 단절 지점 이전은 무시
        enriched.append({**issue, "fingerprint": fp, "age_rounds": age})
    return enriched


# CQ 관련 키워드 — priority_score 가산 대상.
# 한국어/영어 모두 커버. CQ 답변 가능성 저하는 도메인 품질의 핵심 지표.
_CQ_RELATED_KEYWORDS: tuple[str, ...] = (
    "cq", "질의", "답변", "조회", "쿼리", "query", "competency",
)


def _compute_issue_priority(issue: dict, cq_coverage_pct: float) -> int:
    """이슈 우선순위 0~100. 규칙 기반 — 같은 입력 → 같은 출력.

    Weights:
        - severity: critical=+40, high=+25, medium=+10, low=+0
        - age_rounds: 라운드당 +15 (cap +45 — 3라운드 연속 이상)
        - CQ 관련 키워드 포함 (symptom / action / fix 중 하나): +20
        - cq_coverage_pct < 0.8 이면서 CQ 관련 이슈: +10 (답변률 낮을수록 긴급)
        - _agent == "sme": +5 (도메인 정합성 가산)

    총점 100 cap. cq_coverage_pct 는 0~1 범위 (예: 0.65 = 65% 답변 가능).
    """
    score = 0

    sev = str(issue.get("severity", "medium") or "medium").lower()
    score += {"critical": 40, "high": 25, "medium": 10, "low": 0}.get(sev, 10)

    age = int(issue.get("age_rounds", 1) or 1)
    score += min(age * 15, 45)

    # CQ 관련 키워드는 여러 text 필드를 모아 스캔
    text_blob = " ".join(
        str(issue.get(k, "") or "") for k in (
            "symptom", "text", "issue", "action", "fix", "impact",
        )
    ).lower()
    is_cq_related = any(k in text_blob for k in _CQ_RELATED_KEYWORDS)
    if is_cq_related:
        score += 20
        # CQ 답변률 저조 시 CQ 관련 이슈에 추가 가중치
        if cq_coverage_pct < 0.8:
            score += 10

    if str(issue.get("_agent", "") or "").lower() == "sme":
        score += 5

    return min(score, 100)


def _build_priority_table(
    validator_result: dict,
    sme_result: dict,
    debate_log: list,
    cq_coverage_pct: float = 0.0,
) -> list[dict]:
    """V/SME 잔여 이슈 → priority_table (compromise 프롬프트/audit 용).

    각 entry: {issue_summary, priority_score, severity, age_rounds, _agent, fingerprint}.
    priority_score 내림차순 정렬 (LLM 이 상단부터 훑을 수 있도록).
    """
    v_issues = [
        {**i, "_agent": "validator"}
        for i in (validator_result.get("issues", []) or [])
    ]
    s_issues = [
        {**i, "_agent": "sme"}
        for i in (sme_result.get("issues", []) or [])
    ]
    all_issues = v_issues + s_issues
    enriched = _compute_issue_persistence(all_issues, debate_log or [])

    table: list[dict] = []
    for issue in enriched:
        score = _compute_issue_priority(issue, cq_coverage_pct)
        summary = (
            issue.get("symptom", "")
            or issue.get("text", "")
            or issue.get("issue", "")
            or ""
        )[:160]
        table.append({
            "issue_summary": summary,
            "priority_score": score,
            "severity": issue.get("severity", "medium"),
            "age_rounds": issue.get("age_rounds", 1),
            "_agent": issue.get("_agent", "?"),
            "fingerprint": issue.get("fingerprint", ""),
        })
    table.sort(key=lambda p: -p["priority_score"])
    return table


def _architect_compromise(ttl: str, validator_result: dict, sme_result: dict,
                          debate_log: list,
                          priority_table: list[dict] | None = None) -> dict:
    """Architect가 미합의 이슈에 대해 절충안을 결정하고 이유를 기록한다 (Jury 실패 시 폴백).

    Args:
        priority_table: 이슈별 {issue_summary, priority_score, severity, age_rounds,
            _agent} 목록. 제공되면 프롬프트에 "결정 기준" + 우선순위 표로 주입되어
            LLM 이 결정적 기준을 참조. None 이면 기존 동작 유지 (backward compat).
    """
    remaining_v = json.dumps(validator_result.get("issues", []), ensure_ascii=False)
    remaining_s = json.dumps(sme_result.get("issues", []), ensure_ascii=False)
    rounds_summary = json.dumps(
        [{"round": r["round"],
          "v_issues": r["validator"]["issues_count"],
          "s_issues": r["sme"]["issues_count"]}
         for r in debate_log], ensure_ascii=False)

    # T5: priority_table 이 제공되면 "결정 기준" 섹션을 프롬프트에 주입.
    # 없으면 기존 프롬프트 그대로 (backward compat).
    if priority_table:
        priority_rows = []
        for p in priority_table:
            priority_rows.append(
                f"- [{p.get('_agent', '?')}] priority_score={p.get('priority_score', 0)} "
                f"severity={p.get('severity', '?')} age_rounds={p.get('age_rounds', 1)} "
                f"issue=\"{p.get('issue_summary', '')}\""
            )
        priority_block = (
            "\n## 결정 기준 (우선순위 순)\n\n"
            "아래 priority_score 는 **결정적 규칙** 으로 사전 계산된 값입니다.\n"
            "모든 결정은 이 수치를 **반드시 참조** 해야 합니다.\n\n"
            "1. **priority_score ≥ 70** 이슈는 **원칙적으로 accept** — "
            "critical/persistent/CQ-관련이므로 무시하면 품질 저하 직결.\n"
            "2. **priority_score 40~69** 는 **SME 이슈 우선 accept** (도메인 정합성). "
            "reject 시 명확한 근거 필수.\n"
            "3. **priority_score < 40** 는 reject 허용 (단 근거 기록).\n\n"
            "## 이슈 + Priority Score\n\n"
            + "\n".join(priority_rows)
            + "\n\n각 decision 의 JSON 에 `priority_score` 필드를 포함하세요 "
              "(위 표에서 복사).\n"
        )
    else:
        priority_block = ""

    prompt = f"""당신은 **Ontology Architect (작성자)**입니다.
{len(debate_log) + 1}라운드의 토론이 끝났지만 두 비평가와 완전한 합의에 도달하지 못했습니다.

## 토론 경과
{rounds_summary}

## Semantic Validator 잔여 이슈
{remaining_v}

## Manufacturing SME 잔여 이슈
{remaining_s}
{priority_block}
## 작업
남은 이슈 각각에 대해 **절충안을 결정**하고 그 이유를 기록하세요.
- 수정할 것: 어떻게 수정하는지
- 수정하지 않을 것: 왜 현재 상태가 더 적절한지

## 출력 형식 (JSON)
```json
{{
  "decisions": [
    {{
      "issue": "이슈 요약",
      "agent": "validator|sme",
      "decision": "accept|reject|partial",
      "action": "수정 내용 또는 유지 사유",
      "rationale": "논리적 근거",
      "priority_score": 0
    }}
  ],
  "overall_rationale": "전체 절충 판단 근거 1-2문장"
}}
```

JSON만 출력하세요:"""

    # Architect compromise도 결정적이어야 — P9 temp 0.2
    text = _invoke_bedrock(prompt, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.2,
                            agent_role="compromise")

    def _retry_compromise(retry_prompt_override: str) -> str:
        # 1차와 동일한 max_tokens — 축소하면 1차가 길이로 실패한 경우 더 잘린다.
        return _invoke_bedrock(
            retry_prompt_override, max_tokens=_REVIEW_MAX_TOKENS, temperature=0.0,
            agent_role="compromise",
        )

    # 재시도 시 원본 컨텍스트(prompt)를 다시 포함 — 안내문만 보내면 모델이 빈
    # 입력을 받아 근거 없는 결정을 생성하는 회귀가 발생한다.
    retry_prompt = (
        prompt
        + "\n\n[재시도] 이전 응답이 JSON 파싱에 실패했습니다. 단일 JSON 객체 하나만 출력하세요.\n"
        "필드: decisions (list), overall_rationale (string).\n"
        "JSON 만 출력하세요:"
    )
    result = _parse_review_json(
        text, "Architect Compromise",
        retry_invoker=_retry_compromise, retry_prompt=retry_prompt,
    )
    logger.info("[절충안] %d건 결정: %s",
                len(result.get("decisions", [])),
                result.get("overall_rationale", ""))

    # T5: non-blocking audit sidecar. 실패해도 compromise 경로는 계속.
    try:
        # X1: 역할별 모델 ID 기록 — 편향 감사용. 환경변수 미설정이면 기본(BEDROCK_MODEL_ID).
        from config import BEDROCK_MODEL_ID as _DEFAULT_MODEL
        from tools.compromise_audit import append_compromise_audit
        agent_models = {
            role: (_get_agent_model(role) or _DEFAULT_MODEL)
            for role in ("architect", "validator", "sme", "jury", "compromise")
        }
        artifact = {
            "consensus_reached": False,
            "rounds_conducted": len(debate_log),
            "overall_rationale": result.get("overall_rationale", ""),
            "decisions": result.get("decisions", []),
            # X1: Multi-Agent 모델 다양화 감사 필드
            "agent_models": agent_models,
            # priority_table 은 audit 보조 계산용 — 저장 후 _ 접두어로 유지
            "_priority_table": priority_table or [],
        }
        append_compromise_audit(artifact)
    except Exception as audit_err:
        logger.warning("[Compromise Audit] 저장 실패 (non-blocking): %s", audit_err)

    return result


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _extract_json_candidate(text: str) -> str:
    """LLM 응답에서 JSON 본문 추출. 코드블록 → 원문 → 괄호 매칭 순 fallback.

    모든 복구 경로를 한 곳에 모아 호출자가 다른 에이전트에도 재사용하도록.
    """
    text = text.strip()
    # 1) ```json ... ``` 블록
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        inner = m.group(1).strip()
        if inner:
            return inner
    # 2) 응답이 JSON 으로 **시작** 하면 그 값만 정확히 떼어낸다.
    #
    # 예전엔 여기서 ``return text`` 로 원문을 그대로 돌려줘, 뒤에 해설이 붙은
    # 무-fence 응답이 파싱 실패했다 (실측 2026-08-18: ``{...}\n\n위와 같이
    # 검토했습니다.`` → JSONDecodeError). **앞** 에 해설이 붙으면 branch 3 이
    # 살려내는데 **뒤** 에 붙으면 branch 2 가 short-circuit 해서 못 살리는
    # 비대칭이었다. LLM 이 흔히 내는 형태다.
    #
    # ``raw_decode`` 는 첫 유효 JSON 값의 끝을 정확히 알려주므로 탐욕 정규식보다
    # 안전하다 (문자열 안의 중괄호에 속지 않는다).
    if text.startswith(("{", "[")):
        try:
            _, end = json.JSONDecoder().raw_decode(text)
            return text[:end]
        except ValueError:
            # 잘린 JSON 등 — branch 3 의 괄호 매칭에 기회를 준다.
            pass
    # 3) 본문 중 첫 `{...}` 또는 `[...]` 블록 (탐욕 매칭 → 마지막 괄호까지)
    obj = _JSON_OBJECT_RE.search(text)
    arr = _JSON_ARRAY_RE.search(text)
    if obj and (not arr or obj.start() < arr.start()):
        return obj.group(0)
    if arr:
        return arr.group(0)
    return text


def _parse_review_json(
    text: str,
    agent_name: str,
    retry_invoker: Callable[[str], str] | None = None,
    retry_prompt: str | None = None,
) -> dict:
    """LLM 응답에서 리뷰 JSON 을 파싱. 실패 시 한 번 재시도.

    Args:
        text: 1차 LLM 응답.
        agent_name: 로그 식별용.
        retry_invoker: 재시도에 사용할 callable(prompt -> text). 실패 시 한 번 호출.
        retry_prompt: 재시도 시 전달할 프롬프트 (보통 "JSON 배열/객체만 반환하세요").
    """
    candidate = _extract_json_candidate(text)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        logger.warning("%s 리뷰 JSON 1차 파싱 실패 (%s), 재시도 가능 여부: %s",
                       agent_name, e.msg, "yes" if retry_invoker else "no")

    # 재시도: 이전 응답 + 명시적 JSON-only 재요청
    if retry_invoker and retry_prompt:
        try:
            retry_text = retry_invoker(retry_prompt)
        except Exception as e:
            logger.warning("%s 재시도 중 예외: %s", agent_name, e)
        else:
            candidate = _extract_json_candidate(retry_text)
            try:
                result = json.loads(candidate)
                logger.info("%s 리뷰 JSON 재시도 성공", agent_name)
                return result
            except json.JSONDecodeError as e:
                logger.warning("%s 리뷰 JSON 재시도도 실패: %s", agent_name, e.msg)

    logger.error("%s 리뷰 JSON 최종 파싱 실패 — 빈 결과 반환", agent_name)
    return {
        "issues": [], "approved": False,
        "summary": "파싱 실패 — 재검토 필요",
        "parse_error": True,
    }


def _merge_review_snapshot(
    prev_issues: list[dict] | None, review: dict,
) -> list[dict]:
    """다음 라운드로 넘길 이슈 스냅샷 — 파싱 실패면 **이전 것을 유지**한다.

    ``_parse_review_json`` 은 최종 파싱 실패 시 ``issues: []`` 센티넬을 돌려준다.
    그것을 스냅샷에 그대로 쓰면 두 가지가 깨진다 (실측 2026-08-17 R4):

    1. ``_detect_persistent_veto`` 가 ``if not prev_issues: return []`` 로 빠져
       persistence 판정이 **리셋**된다 → 이슈 수가 13→19→0→13 으로 진동한다.
    2. 다음 라운드 Architect 가 "이전 지적" 을 못 받는다.

    ``parse_error`` 가 **아닌** 빈 리스트는 보존하지 않는다 — 그건 "이슈 없음" 을
    정상 보고한 합의 신호이고, 유지하면 승인된 T-Box 가 영구히 미해결 이슈를 갖는다.
    """
    if review.get("parse_error"):
        return list(prev_issues or [])
    return list(review.get("issues") or [])


def _review_for_architect(
    review: dict, prev_issues: list[dict] | None,
) -> dict:
    """Architect 수정 프롬프트에 넘길 리뷰 — 파싱 실패면 이전 이슈로 채운다.

    ``_architect_revise`` 는 ``validator_review["issues"]`` 를 직접 직렬화한다.
    센티넬을 그대로 넘기면 **수정 지시가 사라져** Architect 가 아무것도 바꾸지
    않는다 (실측: R4·R5 revision triples 가 3222 로 동일).

    ``approved``/``parse_error`` 는 조작하지 않는다 — approved 를 True 로 바꾸면
    fail-open 이 되고, parse_error 를 지우면 라운드 로그가 거짓이 된다.
    """
    if not review.get("parse_error"):
        return review
    carried = list(prev_issues or [])
    if not carried:
        return review
    merged = dict(review)
    merged["issues"] = carried
    merged["summary"] = (
        f"{review.get('summary') or '파싱 실패'} — 이전 라운드 미해결 이슈 "
        f"{len(carried)}건을 그대로 전달한다 (이번 라운드 리뷰는 유실됐다)"
    )
    return merged


def _count_issues(review: dict, min_severity: str = "high") -> int:
    """리뷰에서 특정 심각도 이상의 이슈 수를 센다."""
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    threshold = severity_rank.get(min_severity, 3)
    return sum(1 for issue in review.get("issues", [])
               if severity_rank.get(issue.get("severity", "low"), 1) >= threshold)


def _issue_key(issue: dict) -> str:
    """이슈의 안정 키. veto 지속 판정 + 보고서 반복 집계용.

    :func:`_issue_identity` 와 **같은 축** 을 쓴다 (지문과 판정이 갈리지 않게).
    다만 사람이 읽는 라벨이므로 전체를 해시하지 않고 ``category:엔티티#해시``
    형태로 남긴다 — veto 로그와 보고서 표에 그대로 찍힌다.

    예전에는 ``target`` 원문을 그대로 썼다. LLM 이 그 필드에 서술 문장을 넣기
    때문에(실측 90건 중 54건) 같은 결함이 라운드마다 다른 키를 얻었고, 같은
    실행에서 ``persistent_issue_count=0`` 과 ``veto_lock`` 잔존 2건이 **모순**
    됐다. 엔티티 원자로 정규화하면 그 모순이 사라진다.

    텍스트 해시를 붙이는 이유는 과잉 병합 방지다 — 근거는
    :data:`_issue_text_fp_max` 의 실측 표 참조.
    """
    category, atoms, digest = _issue_identity(issue)
    return f"{category}:{','.join(atoms)}#{digest}"


def _collect_veto_issues(review: dict) -> list[dict]:
    """approved=false 리뷰에서 critical/high 이슈를 veto 후보로 추출."""
    if review.get("approved", False):
        return []
    severity_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    return [
        i for i in review.get("issues", [])
        if severity_rank.get(i.get("severity", "low"), 1) >= 3
    ]


def _detect_persistent_veto(
    prev_issues: list[dict] | None,
    curr_issues: list[dict],
) -> list[dict]:
    """2라운드 연속 동일 target+category로 잔존하는 critical/high 이슈 반환.

    Architect가 의도적으로 무시했거나 기술적으로 해결 불가한 이슈를 잡는다.
    """
    if not prev_issues:
        return []
    prev_keys = {_issue_key(i) for i in prev_issues}
    return [i for i in curr_issues if _issue_key(i) in prev_keys]


_DEBATE_STATUS_PRED = URIRef("http://purl.org/dc/terms/description")
_DEBATE_STATUS_PREFIX = "multi_agent_debate: "


def _build_debate_status_literal(
    *,
    consensus_reached: bool,
    veto_lock_triggered: bool,
    veto_targets: list[str],
    total_rounds: int,
    infra_abort: dict | None = None,
    early_stop_reason: str | None = None,
) -> Literal:
    """debate 상태를 문자열 리터럴로 직렬화. key=value 쌍을 | 로 구분.

    ``infra_abort`` 가 있으면 ``aborted_infra_error`` 상태로 기록한다. 이것이
    없으면 인프라 장애로 3라운드에서 끊긴 산출물이 5라운드를 정상 소화하고
    합의에 이르지 못한 산출물과 **바이트 단위로 구분 불가** 하다 — 둘의 신뢰도는
    전혀 다르다.

    ``early_stop_reason`` 도 같은 이유로 구분해 각인한다. "고칠 수 있는 것이 없어
    2라운드에서 끝난" 산출물과 "5라운드를 다 돌고도 미합의인" 산출물은 신뢰도가
    다르다 — 전자는 남은 이슈가 T-Box 편집으로 해결 불가인 것이고, 후자는
    Architect 가 반영에 실패한 것이다.
    """
    status_value = "consensus_reached" if consensus_reached else (
        "aborted_infra_error" if infra_abort else (
            "debate_stopped_no_expressible_fix" if early_stop_reason
            else "debate_unresolved_veto_lock" if veto_lock_triggered
            else "debate_unresolved_max_rounds"
        )
    )
    parts = [
        f"debate_status={status_value}",
        f"total_rounds={total_rounds}",
        f"consensus_reached={str(consensus_reached).lower()}",
    ]
    if early_stop_reason:
        parts.append(f"early_stop_reason={early_stop_reason}")
    if infra_abort:
        parts.append(f"aborted_phase={infra_abort.get('phase', '?')}")
        parts.append(f"aborted_round={infra_abort.get('round', '?')}")
        parts.append(f"aborted_error={str(infra_abort.get('error', ''))[:120]}")
    if veto_lock_triggered and veto_targets:
        parts.append(
            "veto_persistent_targets="
            + "; ".join(str(t)[:80] for t in veto_targets[:5])
        )
    return Literal(_DEBATE_STATUS_PREFIX + " | ".join(parts))


def _annotate_consensus_status(
    ttl: str,
    *,
    consensus_reached: bool,
    veto_lock_triggered: bool,
    veto_targets: list[str],
    total_rounds: int,
    infra_abort: dict | None = None,
    early_stop_reason: str | None = None,
) -> str:
    """합의 실패/성공 상태를 TTL 의 owl:Ontology 노드에 dcterms:description 으로 기록.

    rdflib 로 파싱 → 수정 → serialize 하여 문자열 포맷 의존성 제거.
    owl:Ontology 노드가 없으면 <urn:steel:ontology> 을 새로 만들어 추가.
    기존 `multi_agent_debate:` 로 시작하는 설명은 제거해서 중복 누적 방지.
    """
    try:
        g = _new_graph()
        g.parse(data=ttl, format="turtle")
    except Exception as e:
        logger.warning("annotate_consensus_status: TTL 파싱 실패, 원본 반환 — %s", e)
        return ttl

    ontology_nodes = list(g.subjects(RDF.type, OWL.Ontology))
    if not ontology_nodes:
        fallback = URIRef("urn:steel:ontology")
        g.add((fallback, RDF.type, OWL.Ontology))
        ontology_nodes = [fallback]

    # dcterms prefix 바인딩 (없다면) — serialize 후 가독성 향상.
    from rdflib import Namespace as _NS
    g.bind("dcterms", _NS("http://purl.org/dc/terms/"), override=False)

    new_literal = _build_debate_status_literal(
        consensus_reached=consensus_reached,
        veto_lock_triggered=veto_lock_triggered,
        veto_targets=veto_targets,
        total_rounds=total_rounds,
        infra_abort=infra_abort,
        early_stop_reason=early_stop_reason,
    )

    for onto in ontology_nodes:
        # 이전 debate 상태 description 은 제거 (중복 축적 방지).
        for _, _, o in list(g.triples((onto, _DEBATE_STATUS_PRED, None))):
            if isinstance(o, Literal) and str(o).startswith(_DEBATE_STATUS_PREFIX):
                g.remove((onto, _DEBATE_STATUS_PRED, o))
        g.add((onto, _DEBATE_STATUS_PRED, new_literal))

    return g.serialize(format="turtle")


#: CQ 도달성 판정의 기본 홉 상한.
#:
#: 2 였을 때 S2 5라운드 내내 커버리지가 58.3% 로 **한 번도 움직이지 않았다**
#: (2026-08-18 실측). 미답변 5건은 전부 "2-hop OP 부재" 였는데, 선언된
#: domain/range 로 BFS 하면 **5쌍 전부 경로가 실재한다** — 3~5홉이었다. 예:
#: ``ProcessBlastFurnace → EquipmentMaster → TagMaster → AlarmEvents``.
#: Architect 는 어떤 OP 를 추가해도 오르지 않는 목표를 보고 있었고, 그래서
#: 토론이 수렴할 수 없었다.
#:
#: 3 으로 정한 근거 (실측 — CQ 갭 해소 / 전체 클래스쌍 연결률):
#:   2 → 0/5 / 17.5%   |   **3 → 5/5 / 25.3%**   |   4 → 5/5 / 32.9%
#:   6 → 5/5 / 41.7%
#: 3 에서 이미 전부 해소되고 더 올려도 이득 없이 연결률만 오른다. 연결률이
#: 포화하면 "연결됨" 판정이 무의미해지므로(게이트가 꺼진 것과 같다) 최소값을 쓴다.
#: ``S2_CQ_MAX_HOPS`` 로 조절 가능 — 포화가 관측되면 즉시 되돌릴 수 있다.
_CQ_MAX_HOPS_DEFAULT = 3


def _cq_max_hops() -> int:
    """CQ 도달성 홉 상한 (환경변수 우선, 파싱 실패 시 기본값)."""
    try:
        return max(1, int(os.getenv("S2_CQ_MAX_HOPS", str(_CQ_MAX_HOPS_DEFAULT))))
    except (TypeError, ValueError):
        return _CQ_MAX_HOPS_DEFAULT


def _cq_runtime_check(current_ttl: str, cqs: list[dict]) -> dict:
    """CQ 도메인 연결 가능성을 T-Box 그래프만으로 판정.

    각 CQ에 대해 도메인 클래스 간 ObjectProperty 경로가 ``_cq_max_hops()`` 홉
    안에 있는지 rdflib 로 정적 검사. LLM 재호출 없이 결정적.

    Returns:
        {answerable: [cq_id...], unanswerable: [(cq_id, reason)...],
         coverage_pct: float}
    """
    if not cqs:
        return {"answerable": [], "unanswerable": [], "coverage_pct": 100.0}
    from rdflib import OWL as _O
    from rdflib import RDF as _R
    from rdflib import RDFS as _RS
    try:
        g = _parse_ttl_readonly(current_ttl)
    except Exception as e:
        return {"answerable": [], "unanswerable": [("ALL", f"parse error: {e}")],
                "coverage_pct": 0.0}

    steel_str = DOMAIN_NS
    # 클래스 집합 (steel: 네임스페이스)
    cls_local: set[str] = set()
    for c in g.subjects(_R.type, _O.Class):
        if isinstance(c, URIRef) and str(c).startswith(steel_str):
            cls_local.add(str(c)[len(steel_str):].lower())

    # 명명된 owl:Restriction 은 **계층 노드가 아니다**. 이 리포의 T-Box 는
    # 클래스별 someValuesFrom 을 명명 Restriction 으로 선언하므로(현행 111개)
    # 그것을 클래스처럼 타면 조상/자손 집합이 오염된다 — 실측: 확장 대상에
    # 넣으면 인접 edges 가 145 → 6,137 로 42배 폭증하고, 같은 Restriction 을
    # 부모로 갖는 무관한 클래스들이 "연결됨" 으로 보인다.
    named_restrictions: set[str] = {
        str(r)[len(steel_str):].lower()
        for r in g.subjects(_R.type, _O.Restriction)
        if isinstance(r, URIRef) and str(r).startswith(steel_str)
    }

    # 부모 그래프 (subClassOf) — 추상 클래스 해소에 사용
    parent_of: dict[str, set[str]] = {}
    for c in g.subjects(_R.type, _O.Class):
        if not (isinstance(c, URIRef) and str(c).startswith(steel_str)):
            continue
        key = str(c)[len(steel_str):].lower()
        for p in g.objects(c, _RS.subClassOf):
            if isinstance(p, URIRef) and str(p).startswith(steel_str):
                plocal = str(p)[len(steel_str):].lower()
                # Restriction 이 **부모로 나오는 간선만** 끊으면 충분하다. 그것을
                # 경유하는 경로는 어느 방향으로도 성립하지 않으므로, 자식 쪽
                # (``key in named_restrictions``) 검사는 죽은 코드다 —
                # 실측으로 확인(무력화해도 어떤 테스트도 red 가 되지 않았다).
                if plocal in named_restrictions:
                    continue
                parent_of.setdefault(key, set()).add(plocal)

    def _descendants(name: str, _seen: set[str] | None = None) -> set[str]:
        """name의 모든 자손(자기 자신 포함) 로컬명.

        _seen 가드: T-Box 에 subClassOf 순환(A⊑B, B⊑A)이 있으면 가드 없이는
        무한 재귀로 RecursionError 가 난다(2026-06-23 규명: materialADesignSpec ↔
        materialADesignSpecification 순환). 방문한 노드를 재진입하지 않는다.
        """
        if _seen is None:
            _seen = set()
        if name in _seen:
            return set()
        _seen.add(name)
        out = {name}
        for cls, parents in parent_of.items():
            if name in parents:
                out |= _descendants(cls, _seen)
        return out

    # OP 인접 그래프: (domain_local, range_local)
    op_edges: list[tuple[str, str]] = []
    for op in g.subjects(_R.type, _O.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(steel_str)):
            continue
        doms = [str(d)[len(steel_str):].lower()
                for d in g.objects(op, _RS.domain)
                if isinstance(d, URIRef) and str(d).startswith(steel_str)]
        rngs = [str(r)[len(steel_str):].lower()
                for r in g.objects(op, _RS.range)
                if isinstance(r, URIRef) and str(r).startswith(steel_str)]
        # domain/range 의 모든 자손 포함 (추상 클래스 대응)
        expanded_doms = set()
        for d in doms:
            expanded_doms |= _descendants(d)
        expanded_rngs = set()
        for r in rngs:
            expanded_rngs |= _descendants(r)
        for d in expanded_doms:
            for r in expanded_rngs:
                op_edges.append((d, r))
                op_edges.append((r, d))  # inverseOf 여부 무관하게 양방향 탐색

    adjacency: dict[str, set[str]] = {}
    for a, b in op_edges:
        adjacency.setdefault(a, set()).add(b)

    def _reachable(a: str, b: str, max_hops: int | None = None) -> bool:
        if max_hops is None:
            max_hops = _cq_max_hops()
        if a == b:
            return True
        if a not in adjacency:
            return False
        # BFS up to max_hops
        frontier = {a}
        visited = {a}
        for _ in range(max_hops):
            nxt = set()
            for node in frontier:
                for nb in adjacency.get(node, ()):
                    if nb == b:
                        return True
                    if nb not in visited:
                        nxt.add(nb)
                        visited.add(nb)
            frontier = nxt
            if not frontier:
                break
        return False

    # CQ ``domains`` 는 원본 테이블명일 수 있어 table_class_mapping 을 경유한다.
    # 이 변환 없이 밑줄만 제거하면 (테이블명이 그대로 →
    # TBSOURCETABLE007...) T-Box 에 없는 이름이 되어 CQ 가 전부 미답변으로
    # 오판되고, 그 결과 skeleton injection 이 테이블명 클래스를 만들어낸다.
    from tools.competency_questions import (
        _load_table_class_map,
        _resolve_domain_to_class,
    )
    table_class_map = _load_table_class_map()

    answerable: list[str] = []
    unanswerable: list[tuple[str, str]] = []
    for cq in cqs:
        cq_id = cq.get("id", "?")
        domains = [
            _resolve_domain_to_class(d, table_class_map).lower()
            for d in cq.get("domains", [])
        ]
        if len(domains) < 2:
            # 단일 도메인 CQ — 해당 클래스가 T-Box에 있는지만 확인
            if domains and domains[0] in cls_local:
                answerable.append(cq_id)
            elif domains:
                unanswerable.append(
                    (cq_id, f"클래스 누락: {domains[0]}"))
            continue
        reachable_all = True
        missing_pair = None
        for i in range(len(domains)):
            for j in range(i + 1, len(domains)):
                a, b = domains[i], domains[j]
                if a not in cls_local:
                    reachable_all = False
                    missing_pair = f"클래스 누락: {a}"
                    break
                if b not in cls_local:
                    reachable_all = False
                    missing_pair = f"클래스 누락: {b}"
                    break
                hops = _cq_max_hops()
                if not _reachable(a, b, max_hops=hops):
                    reachable_all = False
                    # 상한을 메시지에 실어야 한다 — 하드코딩하면 상한을 바꿔도
                    # 메시지가 거짓말을 하고 Architect 가 잘못된 목표를 본다.
                    missing_pair = (
                        f"{a} → {b} 경로 없음 ({hops}홉 내 OP 연결 부재)"
                    )
                    break
            if not reachable_all:
                break
        if reachable_all:
            answerable.append(cq_id)
        else:
            unanswerable.append((cq_id, missing_pair or "연결 경로 불명"))

    total = len(cqs)
    coverage = round(len(answerable) / max(total, 1) * 100, 1)
    return {
        "answerable": answerable,
        "unanswerable": unanswerable,
        "coverage_pct": coverage,
    }


# Map CamelCase local name back to "CamelCase" from lowercase for readability.
# _cq_runtime_check drops underscores+case, so we restore display form from CQ domains.
def _csv_fk_pairs() -> set[tuple[str, str]] | None:
    """CSV FK 컬럼이 실제로 잇는 ``(source_class, target_class)`` 쌍.

    Returns:
        정규화된(밑줄 제거·소문자) 클래스명 쌍 집합.
        ``None`` — CSV 를 읽을 수 없어 **판정 불가**. 호출부는 이때 게이트를 적용
        하지 않는다 (0건과 판정 불가를 혼동하면 정당한 주입을 전부 막는다).
    """
    import csv as _csv
    import glob as _glob
    import os as _os

    try:
        import config
        from tools.ontology_quality import _FK_PATTERNS
    except Exception as exc:  # noqa: BLE001
        logger.debug("CSV FK 쌍 조회 실패: %s", exc)
        return None
    csv_dir = config.SOURCE_RAWDATA_DIR
    if not (csv_dir and _os.path.isdir(csv_dir)):
        return None

    norm = lambda x: x.replace("_", "").lower()   # noqa: E731
    pairs: set[tuple[str, str]] = set()
    files = sorted(_glob.glob(_os.path.join(csv_dir, "*.csv")))
    if not files:
        return None
    for path in files:
        src = norm(_os.path.basename(path)[:-4])
        try:
            with open(path, encoding="utf-8-sig") as fh:
                header = next(_csv.reader(fh), [])
        except OSError as exc:
            logger.debug("CSV 헤더 읽기 실패 (%s): %s", path, exc)
            continue
        for col in header:
            target = _FK_PATTERNS.get(col.lower().replace("_", ""))
            if target and norm(target) != src:
                pairs.add((src, norm(target)))
    return pairs


#: CQ 미답변 사유에서 클래스 쌍을 뽑는 패턴 — ``_cq_runtime_check`` 가 만든 형태.
_CQ_GAP_PAIR_RE = re.compile(r"^\s*(\S+)\s*→\s*(\S+)\s+경로 없음")


def _split_cq_gaps_by_fk_evidence(
    unanswerable: list,
) -> tuple[list, list]:
    """CQ 갭을 ``(fixable, data_gap)`` 으로 나눈다.

    **왜 필요한가**: 합의 조건이 ``not cq_ops_missing`` 을 요구하는데, 갭 중
    일부는 **CSV 에 FK 가 없어 T-Box 수정으로 해소 불가능**하다. 같은 리포의
    ``_generate_op_skeletons_from_cq_gaps`` 는 이미 그 판정을 해서 주입을
    거부한다 — 즉 코드가 "고칠 수 없다" 고 알면서 Architect 에게 고치라고
    요구하고, 못 고치면 합의를 막았다.

    실측 (2026-08-17): veto persistent targets 2건이 CSV 컬럼 부재였고
    (Air_Emission_Monitoring 에 Equipment_ID 없음) 3라운드 연속 잔존해
    48분 중 후반 절반을 태웠다.

    판정은 ``_csv_fk_pairs()`` 를 재사용한다 (사본 금지). 그 함수의 규약대로
    ``None`` 은 **판정 불가**이므로 면제하지 않는다 — CSV 접근 실패가 조용히
    모든 갭을 면제하면 게이트가 꺼진 것과 같다. 사유 문자열에서 쌍을 못 뽑는
    경우도 보수적으로 fixable 로 둔다.
    """
    if not unanswerable:
        return [], []
    fk_pairs = _csv_fk_pairs()
    if fk_pairs is None:
        return list(unanswerable), []

    def _norm(name: str) -> str:
        return name.replace("_", "").lower()

    fixable: list = []
    data_gap: list = []
    for item in unanswerable:
        reason = item[1] if isinstance(item, list | tuple) and len(item) > 1 else ""
        m = _CQ_GAP_PAIR_RE.match(str(reason))
        if not m:
            fixable.append(item)          # 판정 근거 없음 → 보수적으로 유지
            continue
        a, b = _norm(m.group(1)), _norm(m.group(2))
        if (a, b) in fk_pairs or (b, a) in fk_pairs:
            fixable.append(item)
        else:
            data_gap.append(item)
    return fixable, data_gap


def _graph_fingerprint(ttl: str) -> str:
    """TTL 의 **트리플 집합** 지문. 직렬화 차이에 불변.

    무진전 판정에 트리플 **수** 를 쓰면 add 1 / remove 1 이 상쇄돼 변경을
    놓친다. 정규화된 집합 해시로 내용 동일성을 본다.

    파싱 실패 시 원문 해시로 폴백한다 (판정 자체를 막지 않는다).
    """
    import hashlib

    try:
        g = _parse_ttl_readonly(ttl)
        blob = "\n".join(sorted(f"{s}|{p}|{o}" for s, p, o in g))
    except Exception:
        blob = ttl
    return hashlib.md5(
        blob.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()


def _generate_op_skeletons_from_cq_gaps(
    cq_runtime: dict, cqs: list[dict],
) -> list[dict]:
    """Translate CQ path gaps into deterministic add_object_property DSL actions.

    _cq_runtime_check emits ("CQ07", "process_rolling → product_master ...")
    with lowercased/underscored-removed local names. Recover CamelCase form
    from the CQ's original `domains` list (e.g., "Process_Rolling" → "ProcessRolling")
    to produce valid Steel namespace URIs.
    """
    unanswerable = cq_runtime.get("unanswerable") or []
    if not unanswerable:
        return []

    # CQ ``domains`` 의 원본 테이블명을 T-Box 클래스명으로 해석. 밑줄 제거만
    # 하면 테이블명이 그대로 클래스명이 되어 (밑줄만 사라진 형태 →
    # TBSOURCETABLE007...) 존재하지 않는 클래스를 domain/range 로 참조하는
    # OP 가 주입되고, 후처리가 그 클래스를 실제로 만들어 계층을 오염시킨다
    # (2026-07-25 실측: 테이블명 클래스 6개).
    from tools.competency_questions import (
        _load_table_class_map,
        _resolve_domain_to_class,
    )
    table_class_map = _load_table_class_map()

    display_by_key: dict[str, str] = {}
    for cq in cqs:
        for raw in cq.get("domains", []):
            if not isinstance(raw, str):
                continue
            display = _resolve_domain_to_class(raw, table_class_map)
            display_by_key[display.lower()] = display

    # **CSV FK 가 뒷받침하는 쌍만 주입한다.**
    #
    # 이 함수는 예전에 CQ 경로 갭을 보면 **무조건** OP 를 만들었다. A-Box 생성기는
    # FK 컬럼에서만 관계 트리플을 만들므로 FK 없는 쌍에 OP 를 선언하면 영구히 값
    # 0건이고, 그 관계로 질의하면 0건이 오류 없이 "정답처럼" 반환된다.
    #
    # 실측 (2026-08-13 S2 재실행): 이 경로가 주입한 OP 28개 중 A-Box 값을 가진
    # 것은 **1개** 였다. 그리고 SME 프롬프트는 같은 라운드에 "CSV FK 부재 — 데이터
    # 수집 또는 tacit 지식 필요" 라고 정확히 보고했는데 이 코드가 그 판단을 무시해
    # R1 초안의 OP 87개가 R5 에서 135개로 되돌아갔다 (스켈레톤 54개 주입).
    #
    # FK 가 없는 갭은 **데이터 갭** 이다 — 빈 관계로 가리면 CQ 커버리지 지표만
    # 오르고 질의는 계속 0건이다. 그래서 주입하지 않고 로그로 드러낸다.
    fk_pairs = _csv_fk_pairs()
    norm = lambda x: x.replace("_", "").lower()   # noqa: E731
    ungrounded_gaps: list[str] = []

    skeletons: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for item in unanswerable:
        if not isinstance(item, list | tuple) or len(item) < 2:
            continue
        cq_id, reason = item[0], item[1]
        if not isinstance(reason, str) or "→" not in reason:
            continue
        left, right = (s.strip() for s in reason.split("→", 1))
        right = right.split("경로")[0].strip().rstrip()
        if not left or not right:
            continue
        dom_display = display_by_key.get(left.lower())
        rng_display = display_by_key.get(right.lower())
        if not dom_display or not rng_display or dom_display == rng_display:
            continue
        pair_key = (dom_display, rng_display)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        # FK 근거 확인 — 양방향 중 하나라도 FK 가 있으면 주입한다 (FK 는 한쪽
        # 테이블에만 컬럼으로 존재하므로 방향을 따지면 정당한 쌍을 놓친다).
        # ``fk_pairs is None`` 은 판정 불가 → 게이트를 적용하지 않는다.
        if fk_pairs is not None:
            a, b = norm(dom_display), norm(rng_display)
            if (a, b) not in fk_pairs and (b, a) not in fk_pairs:
                ungrounded_gaps.append(f"{cq_id}: {dom_display}↔{rng_display}")
                continue

        # Use pair-specific names so inverse OPs can't collide across CQs.
        # Example: (ProcessContinuousCasting, NDTResults) and
        # (ProcessContinuousCasting, SurfaceQuality) would otherwise share
        # "isOfProcessContinuousCasting" and accumulate conflicting inverseOf
        # triples on a single OP.
        forward_prop = f"has{rng_display}For{dom_display}"
        inverse_prop = f"is{rng_display}For{dom_display}"
        skeletons.append({
            "action": "add_object_property",
            "property": forward_prop,
            "domain": dom_display,
            "range": rng_display,
            "label_en": f"has {rng_display} for {dom_display}",
            "label_ko": f"{dom_display}의 {rng_display} 보유",
            "source": f"cq_gap:{cq_id}",
        })
        # Declare inverse OP with explicit domain/range so later jury/architect
        # edits never leave it as owl:Thing-typed.
        skeletons.append({
            "action": "add_object_property",
            "property": inverse_prop,
            "domain": rng_display,
            "range": dom_display,
            "label_en": f"is {rng_display} for {dom_display}",
            "label_ko": f"{dom_display}의 {rng_display}임",
            "source": f"cq_gap:{cq_id}",
        })
        skeletons.append({
            "action": "add_inverse_property",
            "property": forward_prop,
            "inverseOf": inverse_prop,
            "source": f"cq_gap:{cq_id}",
        })

    if ungrounded_gaps:
        logger.info(
            "CQ 경로 갭 %d건은 CSV FK 근거가 없어 OP 를 주입하지 않았다 — "
            "빈 관계로 가리면 CQ 커버리지 지표만 오르고 질의는 계속 0건이다. "
            "데이터 수집 또는 tacit 지식이 필요하다: %s",
            len(ungrounded_gaps), ungrounded_gaps[:5],
        )
    return skeletons


# ── generate_tbox_collaborative 리팩토링 구조 ───────────────────────────────
#
# MCP 클라이언트에게 라운드 단위로 진행 상황을 보고하기 위해 본문을 3개 sync
# 헬퍼로 분리하고, 공개 진입점은 async 래퍼로 구현한다.
#
#   _build_initial_draft(tables) → state_dict
#       Architect 초안 생성 + CSV/CQ 로드 + 구조 게이트 + extra_context 준비.
#
#   _run_one_debate_round(state, round_num, max_rounds) → round_result
#       한 토론 라운드: Validator+SME → Veto/CQ 체크 → Jury → Architect 수정.
#       결과에 consensus/break_loop/round_log 를 담아 호출자에게 제어 반환.
#
#   _finalize_and_save(state) → final_result_json
#       합의 상태 annotate + TTL 저장 + 베이스라인 기록 + 최종 통계 계산.
#
# 라운드 카운팅 규약 (revised):
#   - state["initial_draft_rounds"] = 1  (Architect 초안)
#   - max_rounds                    = 토론 루프 횟수 (기본 3)
#   - total_rounds                  = 1 + 실제 실행된 토론 횟수
#   - state["debate_log"][*]["round"] 는 계속 2..N 으로 기록 (기존 호환).


def _load_previous_pass_rate() -> float | None:
    """T3: cq_feedback.json 에서 최근 iteration 의 pass_rate 조회.

    파일이 없거나 비어있으면 None. Architect 프롬프트에서
    pass_rate=None 이면 "통과율" 줄이 생략되므로 첫 실행 영향 없음.
    """
    try:
        from tools.cq_feedback import _load_feedback
        data = _load_feedback()
        if data.get("iterations"):
            return data["iterations"][-1].get("pass_rate")
    except Exception:  # noqa: BLE001 — pass_rate 조회 실패는 silent fallback
        pass
    return None


def _build_initial_draft(tables: str,
                          file_hb: _FileHeartbeat | None = None) -> dict:
    """Round 1: Architect 초안 생성 + 보조 context 준비. 블로킹 호출.

    Args:
        file_hb: 주입되면 chunk 진행 hook 을 등록해 파일 하트비트에 chunk_idx/depth/
            stop_reason 을 실시간 기록. LLM 청크 호출이 오래 걸릴 때 어디서 stall 되는지
            관측 가능.

    반환 state 는 _run_one_debate_round 가 변경 가능한 가변 컨테이너로 사용한다.
    """
    from tools.competency_questions import _load_csv_summary
    from tools.tbox_generation import (
        generate_tbox,
        set_chunk_progress_hook,
    )

    logger.info("[Round 1] Ontology Architect — T-Box 초안 생성")

    # chunk hook 을 임시로 등록해 generate_tbox 내부의 _generate_chunk 진행을
    # 파일 하트비트에 반영. 등록은 try/finally 로 감싸 예외가 나도 전역 hook 이
    # 해제되도록.
    if file_hb is not None:
        def _chunk_hook(payload: dict, _hb=file_hb) -> None:
            event = payload.get("event", "?")
            idx = payload.get("chunk_idx")
            depth = payload.get("depth")
            total = payload.get("total_chunks")
            stop = payload.get("stop_reason")
            ttl_chars = payload.get("ttl_chars")
            elapsed = payload.get("elapsed_sec")
            # 최신 이벤트를 sub_phase 에 풀어넣고 원본 payload 전체도 보관.
            _hb.set(
                sub_phase=f"chunk_{idx}/{total} depth={depth} event={event}",
                chunk_progress={
                    "event": event, "chunk_idx": idx, "depth": depth,
                    "total_chunks": total, "stop_reason": stop,
                    "ttl_chars": ttl_chars, "elapsed_sec": elapsed,
                    "tables": payload.get("tables"),
                },
            )
        set_chunk_progress_hook(_chunk_hook)
    try:
        # save_to_tbox_path=False — 초안은 토론 입력일 뿐이다. 예전엔 여기서
        # TBOX_PATH 를 덮어써, 토론 전에 S3 후처리 산출물(hasKey / equivalentProperty /
        # step_15b 가 선언한 OP 등)이 미검토 초안으로 교체됐다. 그 결과 저장 가드
        # (_guard_before_save)는 "직전 파일" 로 자기 초안을 보게 되어 손실을 구조적으로
        # 볼 수 없었다. 이제 최종 저장은 _finalize_and_save 한 곳에서만 일어난다.
        gen_result = json.loads(generate_tbox(tables, save_to_tbox_path=False))
    finally:
        if file_hb is not None:
            set_chunk_progress_hook(None)

    if not gen_result.get("success") and "error" in gen_result:
        return {"error": gen_result}

    # 초안 TTL 은 응답에서 받는다. 500KB 초과로 ttl 필드가 생략된 경우에만
    # 파일 폴백 — 그때는 초안이 저장돼 있지 않으므로 명시적으로 실패시킨다
    # (조용히 기존 T-Box 를 초안으로 오인해 토론하는 경로를 만들지 않는다).
    current_ttl = gen_result.get("ttl")
    if not current_ttl:
        return {"error": {
            "success": False,
            "error": "Architect 초안 TTL 을 응답에서 받지 못했다 "
                     "(500KB 초과로 생략되었을 수 있음). S2 를 중단한다 — "
                     "기존 T-Box 를 초안으로 오인해 토론하지 않기 위함.",
            "statistics": gen_result.get("statistics"),
        }}

    architect_stats = {
        "classes": gen_result.get("statistics", {}).get("classes", 0),
        "object_properties": gen_result.get("statistics", {}).get("object_properties", 0),
        "data_properties": gen_result.get("statistics", {}).get("data_properties", 0),
        "generation_time": gen_result.get("timing", {}).get("total", 0),
    }

    csv_summary = _load_csv_summary()
    cqs = _load_competency_questions()

    gate_pass, gate_msg = _check_structural_gate(current_ttl)
    if not gate_pass:
        logger.warning("[구조 게이트 미통과] %s", gate_msg)
    logger.info("[구조 게이트] %s", gate_msg)

    cq_op_context = _analyze_cq_required_ops(cqs)
    if cq_op_context:
        logger.info("[CQ OP 분석] 필수 ObjectProperty 경로 분석 완료")
    skeleton_context = _generate_tbox_skeleton()
    if skeleton_context:
        logger.info("[T-Box 골격] 도메인 계층 골격 생성 완료")
    anomaly_context = _load_anomaly_hints()
    if anomaly_context:
        logger.info("[Anomaly Hints] rules/domain/anomaly_hints.json 로드")

    # T3: 이전 iteration CQ 실패 패턴을 Architect 프롬프트에 주입.
    # cq_feedback.json 이 없거나 비어있으면 빈 문자열 → 프롬프트 영향 없음.
    feedback_context = ""
    try:
        from tools.cq_feedback import (
            format_feedback_for_prompt as _fb_format,
        )
        from tools.cq_feedback import (
            load_active_suggestions as _fb_load,
        )
        active_sugs = _fb_load()
        if active_sugs:
            prev_pass_rate = _load_previous_pass_rate()
            feedback_context = _fb_format(active_sugs, pass_rate=prev_pass_rate)
            logger.info(
                "[CQ Feedback] 이전 %d 건 suggestion 주입 (pass_rate=%s)",
                len(active_sugs),
                prev_pass_rate,
            )
    except Exception as e:  # noqa: BLE001 — feedback 실패는 T-Box 생성을 막지 않는다
        logger.warning("[CQ Feedback] 로드 실패 (비차단): %s", e)
        feedback_context = ""

    extra_context = ""
    if cq_op_context:
        extra_context += "\n" + cq_op_context
    if feedback_context:
        extra_context += "\n\n" + feedback_context
    if skeleton_context:
        extra_context += "\n" + skeleton_context
    if anomaly_context:
        extra_context += "\n" + anomaly_context
    if not gate_pass:
        extra_context += (
            f"\n\n## ⚠️ 구조 게이트 미통과\n{gate_msg}\n"
            "위 문제를 최우선으로 해결하세요."
        )

    return {
        "current_ttl": current_ttl,
        "architect_stats": architect_stats,
        "csv_summary": csv_summary,
        "cqs": cqs,
        "extra_context": extra_context,
        "debate_log": [],
        "consensus_reached": False,
        "compromise_reason": None,
        "prev_validator_issues": None,
        "prev_sme_issues": None,
        "prev_round_ttl": None,
        # 직전 라운드에 "CSV 데이터 부재" 로 판정된 CQ 갭. 다음 라운드 리뷰어
        # 프롬프트에 주입해 S2 가 해결할 수 없는 갭으로 승인이 막히는 것을 끊는다.
        "prev_cq_data_gap": [],
        "veto_lock_triggered": False,
        "veto_persistent_targets": [],
        "initial_draft_rounds": 1,
        # 인프라 장애로 토론이 중단된 경우 {phase, round, error}. 저장 시
        # debate_status=aborted_infra_error 로 산출물에 각인된다.
        "infra_abort": None,
        # 조기 종료 사유 (``_should_stop_no_expressible``). 저장 시
        # debate_status=debate_stopped_no_expressible_fix 로 각인된다.
        "early_stop_reason": None,
        # 예정 라운드 수 — 조기 종료로 몇 라운드를 아꼈는지 계산에 쓴다.
        # ``_finalize_and_save`` 스코프에 max_rounds 가 없으므로 state 에 둔다.
        "max_rounds_planned": 0,
    }


# Minimum debate loops enforced regardless of early approval.
_MIN_DEBATE_ROUNDS = 2

#: 수정본이 이전 라운드와 완전히 동일한 상태가 이만큼 연속되면 조기 종료한다.
#:
#: 실측 (2026-08-17): R4·R5 의 revision triples 가 3222 로 동일했고 Validator 가
#: "4라운드에서도 T-Box 변경이 없어 이전 critical 이슈가 잔존" 이라고 보고했지만
#: 코드는 감지하지 못해 남은 라운드를 태웠다(20분+). 2 로 두는 이유: 1 이면
#: 일시적 무변경(예: Architect 가 이번 라운드엔 지시가 없다고 판단)에 과민하게
#: 반응하고, 3 이상이면 max_rounds=4 에서 사실상 발동하지 않는다.
_NO_PROGRESS_STREAK_LIMIT = 2

#: ``approved=false`` 를 정당화하지 **못하는** 이슈 category. 리뷰어 프롬프트가
#: 이미 "이 항목만으로 approved:false 를 내지 마라" 고 지시하지만(s3_deferred_block
#: 지시 3, cq_data_gap_block 지시 3) LLM 이 지키지 않는다 — 프롬프트로 지시하고
#: 코드로 검증하지 않으면 지켜지는지 알 수 없다.
#:
#: 실측 (2026-08-26, 배포 3 run / 11라운드): ``approved=True`` 가 **한 번도**
#: 나오지 않았다. 리뷰어가 든 사유는 dcterms:source 근거율 0%(S3 step_15d 가 84%로
#: 채운다), RR 미달, inverseOf 불완전, IOF/BFO 정렬, 중간 추상 클래스 부재(S3 가
#: 클래스 47→67 로 만든다) — 전부 S2 가 구조적으로 할 수 없는 일이다. LLM 자신도
#: ``deferred_to_s3=true`` 를 25/221 건에 표시했지만 severity 는 critical/high 로
#: 남겨 승인을 막았다.
_NON_BLOCKING_ISSUE_CATEGORIES = frozenset({"metric", "data_gap", "deferred"})


def _blocking_issues(review: dict) -> list[dict]:
    """``approved=false`` 를 정당화하는 critical/high 이슈만 남긴다.

    제외 대상 (프롬프트가 이미 지시한 것을 코드로 강제):

    * ``deferred_to_s3`` 가 참인 이슈 — LLM 이 스스로 "S3 담당" 이라고 표시한 것
    * ``category`` 가 :data:`_NON_BLOCKING_ISSUE_CATEGORIES` 인 이슈

    **severity 를 신뢰하지 않는 이유**: 프롬프트는 이 항목들을 medium 이하로 두라고
    지시하는데 실측 25건 중 8건이 critical/high 로 왔다. 표시(``deferred_to_s3`` /
    ``category``) 는 지켜지는 편이므로 그쪽을 판정 근거로 쓴다.

    이슈를 **지우지 않는다** — 반환값은 판정용이고 ``round_log`` 와 ``issue_trace``
    에는 원본이 그대로 남는다. 침묵시키면 S3 가 실패했을 때 아무도 모른다.
    """
    out: list[dict] = []
    for issue in review.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        if issue.get("severity") not in ("critical", "high"):
            continue
        if issue.get("deferred_to_s3"):
            continue
        category = str(issue.get("category") or "").strip().lower()
        if category in _NON_BLOCKING_ISSUE_CATEGORIES:
            continue
        out.append(issue)
    return out


def _effective_approval(review: dict) -> tuple[bool, list[dict]]:
    """리뷰어의 실질 승인 여부 — ``(approved, 차단 이슈)``.

    LLM 의 ``approved`` 가 True 면 그대로 존중한다 (승인을 코드가 취소하지는 않는다).
    False 일 때만 **차단 이슈가 실제로 있는지** 확인한다. 하나도 없으면 거부 근거가
    S3 담당 항목뿐이므로 승인으로 본다.

    한 방향으로만 뒤집는다: 거부 → 승인. 반대 방향(승인 → 거부)은 하지 않는다 —
    리뷰어가 승인했는데 코드가 막으면 그것이 다시 "승인 0건" 을 만든다. 최종 관문은
    Jury 이고(``_handle_potential_consensus``), 이 함수는 Jury 에 **도달**시키는 것이
    목적이다. Jury 는 여전히 독립적으로 production_ready 를 판정한다.
    """
    if review.get("approved"):
        return True, []
    blocking = _blocking_issues(review)
    return (not blocking), blocking


def _measure_expressibility(
    v_blocking: list[dict], s_blocking: list[dict],
) -> dict:
    """차단 이슈를 **현재 어휘로 실행 가능한가** 로 분류한다.

    정본은 :mod:`tools.action_registry` 다. 여기서는 집계만 한다.

    ## 왜 이 계측이 필요한가 (2026-08-30, 8런 394 차단성 이슈 전수 분류)

    ====================== ===== ====================================
    분류                     비율   의미
    ====================== ===== ====================================
    ``executable``          41%   실제로 고칠 수 있다
    ``sme_owned``           34%   SME config 소유 — T-Box 편집 무효
    ``unspecified``         17%   action 제안 없음 (자연어만)
    ``inexpressible``        8%   실행 엔진이 없는 action 제안
    ====================== ===== ====================================

    ``approved=false`` 는 이 394건 때문에 고정되는데 그중 절반 이상이 구조적으로
    반영 불가였다. 그래서 8런 30라운드 전체에서 ``approved=true`` 가 0건이고,
    라운드를 더 돌아도 같은 지적이 반복된다 (``persistent_issue_count`` 최대 27).

    ``unspecified`` 를 "고칠 수 없음" 으로 세지 **않는다** — 자연어 지적은
    Architect 가 DSL 로 번역할 수 있으므로 실행 가능성을 여기서 판정할 수 없다.
    보수적으로 "고칠 수 있을 가능성" 쪽에 넣어야 조기 종료가 과감해지지 않는다.
    """
    counts = {
        "executable": 0, "sme_owned": 0,
        "inexpressible": 0, "unspecified": 0, "policy_rejected": 0,
    }
    samples: dict[str, list[str]] = {}
    try:
        from tools.action_registry import classify_requirement
    except Exception as exc:  # noqa: BLE001 — 계측 실패가 토론을 막지 않는다.
        logger.debug("표현가능성 계측 skip: %s", exc)
        return {"blocking_total": 0, "measured": False, **counts}

    for issue in [*(v_blocking or []), *(s_blocking or [])]:
        # 개별 분류 실패는 그 이슈를 ``unspecified`` 로 둔다 — 계측이 토론을
        # 막아서는 안 되고, ``unspecified`` 는 actionable 로 세므로 **조기 종료를
        # 억제하는** 안전한 쪽이다 (분류를 못 했는데 "고칠 수 없다" 로 세면 라운드를
        # 잘못 자른다).
        try:
            verdict = classify_requirement(issue).get("verdict", "unspecified")
        except Exception as exc:  # noqa: BLE001
            logger.debug("이슈 분류 실패 — unspecified 로 집계: %s", exc)
            verdict = "unspecified"
        counts[verdict] = counts.get(verdict, 0) + 1
        if len(samples.setdefault(verdict, [])) < 3:
            samples[verdict].append(
                str(issue.get("target") or issue.get("summary") or "")[:70]
            )

    total = sum(counts.values())
    # "고칠 여지가 있는" 것 = 실행가능 + 미지정 (번역 가능성).
    actionable = counts["executable"] + counts["unspecified"]
    return {
        "blocking_total": total,
        "measured": True,
        **counts,
        "actionable": actionable,
        "executable_pct": round(100 * counts["executable"] / total, 1) if total else 0.0,
        "samples": samples,
    }


#: 조기 종료 전 최소 라운드. ``_MIN_DEBATE_ROUNDS`` 와 같은 이유로 1라운드
#: 리뷰만 보고 끝내지 않는다 — 첫 라운드 리뷰는 TTL 발췌만 보고 쓰이므로
#: 표현가능성 판정의 근거로 삼기에 이르다.
_MIN_ROUNDS_BEFORE_EARLY_STOP = 2


def _should_stop_no_expressible(
    state: dict, expressibility: dict, round_num: int, round_log: dict,
) -> bool:
    """남은 차단 이슈에 **고칠 수 있는 것이 없으면** 토론을 끝낸다.

    ## 왜 안전한가

    조기 종료가 품질을 깎는 경우는 "더 돌면 나아졌을 텐데 멈춘" 것이다. 여기서
    멈추는 조건은 ``actionable == 0`` 즉 **실행 가능한 수정도 없고 번역 가능한
    자연어 지적도 없는** 상태다. 그 상태에서 Architect 를 한 번 더 부르면 할 수
    있는 일이 없으므로 TTL 이 그대로거나(``no_progress``) 무관한 변경만 생긴다.

    실측(8런): 그런 라운드가 9개였고 (run1 R4/R5, run2 R5, run4 R2, run5 R5,
    run6 R2/R4/R5, run7 R4) 라운드당 평균 711초 = 총 1.78시간을 소모했다.

    ## 왜 고정 라운드 상한이 아닌가

    "무조건 2라운드" 로 자르는 방안은 기각했다. run8 은 CQ 커버리지가 3라운드째
    75.0 → 83.3 으로 올랐고 run2/run3 의 상승도 후반 라운드였다 — 라운드의 이득은
    0이 아니라 **비결정적**이다. 그래서 이득이 구조적으로 불가능한 라운드만
    자른다. 이득 가능성이 남아 있으면 계속 돈다.

    ``_MIN_ROUNDS_BEFORE_EARLY_STOP`` 미만에서는 멈추지 않는다 (첫 리뷰는 TTL
    발췌 기반이라 판정 근거가 약하다).

    환경변수 ``S2_EARLY_STOP=off`` 로 끌 수 있다 — A/B 비교용.
    """
    import os

    if os.getenv("S2_EARLY_STOP", "on").strip().lower() in ("off", "0", "false"):
        return False
    if not expressibility.get("measured"):
        return False
    if round_num < _MIN_ROUNDS_BEFORE_EARLY_STOP:
        return False
    total = expressibility.get("blocking_total", 0)
    if not total:
        # 차단 이슈가 아예 없으면 합의 분기가 판정한다 — 여기서 가로채지 않는다.
        return False
    if expressibility.get("actionable", 0) > 0:
        return False

    reason = (
        f"남은 차단 이슈 {total}건 중 실행 가능한 수정이 0건이다 "
        f"(SME소유 {expressibility['sme_owned']} / "
        f"표현불가 {expressibility['inexpressible']} / "
        f"정책거부 {expressibility.get('policy_rejected', 0)}). "
        f"Architect 가 할 수 있는 일이 없어 라운드를 더 돌아도 같은 지적이 "
        f"반복된다 — 토론을 종료하고 현재 T-Box 를 저장한다."
    )
    logger.warning("[조기 종료] Round %d: %s", round_num + 1, reason)
    round_log["early_stop"] = {
        "reason": "no_expressible_blocking_fix",
        "detail": reason,
        "expressibility": expressibility,
    }
    state["early_stop_reason"] = "no_expressible_blocking_fix"
    return True


def _compute_metrics_feedback(current_ttl: str, round_num: int) -> str:
    """T-Box 의 클래스/OP/DP 카운트 + RR + 최상위 클래스 비율을 계산해 Architect
    프롬프트에 주입할 한국어 피드백 블록을 생성. 실패 시 빈 문자열.
    """
    feedback = ""
    try:
        from rdflib import OWL as _OWL
        from rdflib import RDF as _RDF
        from rdflib import RDFS as _RDFS
        from rdflib import URIRef as _URIRef
        mg = _parse_ttl_readonly(current_ttl)
        domain_str = DOMAIN_NS
        cls_set = {c for c in mg.subjects(_RDF.type, _OWL.Class)
                   if isinstance(c, _URIRef) and str(c).startswith(domain_str)}
        ops = {p for p in mg.subjects(_RDF.type, _OWL.ObjectProperty)
               if isinstance(p, _URIRef) and str(p).startswith(domain_str)}
        dps = {p for p in mg.subjects(_RDF.type, _OWL.DatatypeProperty)
               if isinstance(p, _URIRef) and str(p).startswith(domain_str)}
        total_props = len(ops) + len(dps)
        rr = round(len(ops) / max(total_props, 1), 2)
        root_count = sum(1 for c in cls_set if not any(
            isinstance(p, _URIRef) and str(p).startswith(domain_str)
            for p in mg.objects(c, _RDFS.subClassOf)))
        root_pct = round(root_count / max(len(cls_set), 1) * 100, 1)

        # **RR 목표를 CSV FK 상한으로 보정한다.**
        #
        # 실측 (2026-08-18): DP 239 / CSV FK 쌍 46 인 이 도메인에서 RR ≥ 0.3 은
        # OP 102개를 요구하지만 FK 근거로 만들 수 있는 최대는 정/역 합쳐 92개다.
        # 즉 목표 자체가 **근거 없는 OP 10개 이상을 만들어야만** 달성 가능하고,
        # "⚠️ OP가 부족합니다" 피드백이 매 라운드 Architect 를 그 방향으로 밀었다
        # (그 결과가 phantom OP 36개 / 중복 OP 23개다).
        #
        # 상한은 FK 쌍 × 2 (정·역) 로 잡는다. 판정 불가(None)면 보정하지 않는다.
        fk_pairs = _csv_fk_pairs()
        rr_ceiling = None
        op_ceiling = None
        if fk_pairs is not None and fk_pairs:
            op_ceiling = len(fk_pairs) * 2
            rr_ceiling = round(op_ceiling / max(op_ceiling + len(dps), 1), 2)
        # 목표는 "0.3" 과 "데이터로 도달 가능한 값" 중 **작은 쪽**.
        rr_target = min(0.3, rr_ceiling) if rr_ceiling is not None else 0.3
        at_ceiling = op_ceiling is not None and len(ops) >= op_ceiling
        rr_line = (
            f"- RR (관계 풍부도): {rr} "
            f"{'✅' if rr >= rr_target else f'⚠️ 목표: ≥{rr_target}'}"
        )
        if rr_ceiling is not None:
            rr_line += (
                f"\n- CSV FK 쌍 {len(fk_pairs)}개 → 근거 있는 OP 상한 "
                f"{op_ceiling}개 (RR 상한 {rr_ceiling}). "
                f"현재 OP {len(ops)}개"
                f"{' — **상한 도달**, 더 만들면 근거 없는 OP 가 된다' if at_ceiling else ''}"
            )

        # OP 근거율 — 개수 목표만 보여주면 관심이 개수에 머문다. 근거율을 노출해
        # 지적 대상을 개수에서 근거로 옮긴다 (DP 는 99.6%, OP 는 실측 21%).
        # ``none:`` 접두사는 "근거 없음" 을 **명시한** 값이다 (step_12e 가 DP 에
        # 대해 쓰는 규약). 근거로 세면 지표가 부풀린다.
        _dc_source = _URIRef("http://purl.org/dc/terms/source")

        def _has_real_source(p) -> bool:
            val = str(next(mg.objects(p, _dc_source), "") or "")
            return bool(val) and not val.startswith("none:")

        grounded = sum(1 for p in ops if _has_real_source(p))
        ground_pct = round(grounded / max(len(ops), 1) * 100, 1)
        ground_line = (
            f"- OP 근거율 (dcterms:source 보유): {grounded}/{len(ops)} "
            f"({ground_pct}%) {'✅' if ground_pct >= 60 else '⚠️ 목표: ≥60% — '
             'CSV FK 컬럼을 dcterms:source 로 명시하라'}"
        )

        op_warn = (
            "⚠️ OP가 부족합니다. 크로스 도메인 ObjectProperty를 추가하세요."
            if (rr < rr_target and not at_ceiling) else ""
        )
        feedback = f"""
## 현재 T-Box 메트릭 (자동 측정)
- 클래스: {len(cls_set)}, OP: {len(ops)}, DP: {len(dps)}
{rr_line}
{ground_line}
- 최상위 클래스 비율: {root_pct}% {'✅' if root_pct <= 20 else '⚠️ 목표: ≤20%'}
- {op_warn}
- {'⚠️ 중간 추상 클래스가 부족합니다. 도메인별 부모 클래스를 추가하세요.' if root_pct > 20 else ''}
"""
        logger.info(
            "[Round %d] 메트릭 피드백: classes=%d, OP=%d, DP=%d, RR=%.2f, root=%.1f%%",
            round_num + 1, len(cls_set), len(ops), len(dps), rr, root_pct,
        )
    except Exception as e:
        logger.warning("메트릭 피드백 계산 실패: %s", e)

    if round_num > 1:
        gate_pass, gate_msg = _check_structural_gate(current_ttl)
        if not gate_pass:
            logger.warning("[Round %d 구조 게이트 미통과] %s", round_num + 1, gate_msg)
            feedback += f"\n⚠️ 구조 게이트 경고: {gate_msg}\n"
    return feedback


def _run_validator_and_sme(
    state: dict, ttl_for_review: str, prev_ttl_for_diff: str,
    ontoqa_metrics: dict, round_num: int, parallel_enabled: bool,
    preamble: str = "",
) -> tuple[dict, dict]:
    """Validator 와 SME 리뷰를 병렬/순차로 실행. (validator_result, sme_result).

    Args:
        ttl_for_review: **순수 TTL only.** 마크다운이 섞이면 두 리뷰어의 선언
            인벤토리·diff 파싱이 깨진다.
        preamble: 메트릭 피드백 등 서술 블록. 프롬프트 앞머리로만 간다.
    """
    cqs = state["cqs"]
    csv_summary = state["csv_summary"]
    if parallel_enabled:
        logger.info(
            "[Round %d] Validator ⟂ SME 병렬 실행 (SME 에 이전 라운드 V 이슈 주입)",
            round_num + 1,
        )
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as executor:
            fut_v = executor.submit(
                _validator_review,
                ttl_for_review, round_num, state["prev_validator_issues"],
                metrics=ontoqa_metrics,
                prev_ttl=prev_ttl_for_diff,
                preamble=preamble,
            )
            fut_s = executor.submit(
                _sme_review,
                ttl_for_review, csv_summary, cqs, round_num,
                previous_issues=state["prev_sme_issues"],
                validator_issues=state["prev_validator_issues"],
                prev_ttl=prev_ttl_for_diff,
                preamble=preamble,
            )
            return fut_v.result(), fut_s.result()
    logger.info("[Round %d] Semantic Validator (temp 0.5) 검토", round_num + 1)
    validator_result = _validator_review(
        ttl_for_review, round_num, state["prev_validator_issues"],
        metrics=ontoqa_metrics,
        prev_ttl=prev_ttl_for_diff,
        preamble=preamble,
    )
    logger.info(
        "[Round %d] Manufacturing SME (temp 0.7, +Validator 이슈 주입) 검토",
        round_num + 1,
    )
    sme_result = _sme_review(
        ttl_for_review, csv_summary, cqs, round_num,
        previous_issues=state["prev_sme_issues"],
        validator_issues=validator_result.get("issues", []),
        prev_ttl=prev_ttl_for_diff,
        preamble=preamble,
    )
    return validator_result, sme_result


def _build_round_log(
    validator_result: dict, sme_result: dict,
    round_num: int, parallel_enabled: bool,
) -> dict:
    """이번 라운드 결과 요약 dict 빌드 (debate_log 에 append 될 entry).

    ``parse_error`` 를 **명시적으로 기록**한다. ``issues_count: 0`` 만 남기면
    나중에 사람과 코드가 "이 라운드는 깨끗했다" 로 오독한다 — 실측(2026-08-17 R4)
    에서 Validator 0 이슈가 그렇게 보였고, 실제로는 리뷰가 유실된 라운드였다.

    ## ``issues`` 본문을 저장하는 이유 (age_rounds 가 영구히 1 이었다)

    :func:`_compute_issue_persistence` 는 ``prev["validator"]["issues"]`` 를 읽어
    같은 지문이 몇 라운드 연속 잔존했는지 센다. 그런데 이 함수가 ``issues_count``
    만 남기고 **본문을 버려서** 그 조회가 항상 빈 리스트를 받았고, 첫 비교에서
    ``break`` 해 ``age_rounds`` 가 **입력과 무관하게 항상 1** 이었다 (2026-08-22
    실측: 바이트 동일 이슈를 4라운드 반복해도 1).

    그 결과 ``_compute_issue_priority`` 의 age 가중치(라운드당 +15, 최대 +45)가
    한 번도 발화하지 않았다 — "3라운드 연속 잔존" 이라는 가장 강한 우선순위 신호가
    죽어 있었고, ``compromise_audit`` 의 ``persistent_issues_count`` 도 과소보고됐다.

    ``_ROUND_LOG_MAX_ISSUES`` 로 상한을 둔다: 이 로그는 도구 응답 JSON 에 실려
    나가므로(``_finalize_and_save``) 무제한이면 응답이 인스턴스 비례로 커진다 —
    이 리포에서 25MB 응답이 MCP stdio 를 끊은 이력이 있다. persistence 판정에
    필요한 필드만 남기고(``_slim_issue``) 서술형 장문은 잘라낸다.
    """
    log: dict = {
        "round": round_num + 1,
        "review_mode": "parallel" if parallel_enabled else "sequential",
        "validator": {
            "issues_count": len(validator_result.get("issues", [])),
            "critical_high": _count_issues(validator_result, "high"),
            "approved": validator_result.get("approved", False),
            "summary": validator_result.get("summary", ""),
            "issues": _slim_issues(validator_result.get("issues", [])),
        },
        "sme": {
            "issues_count": len(sme_result.get("issues", [])),
            "critical_high": _count_issues(sme_result, "high"),
            "approved": sme_result.get("approved", False),
            "summary": sme_result.get("summary", ""),
            "unanswerable_cqs": sme_result.get("unanswerable_cqs", []),
            "issues": _slim_issues(sme_result.get("issues", [])),
        },
    }
    for key, result in (("validator", validator_result), ("sme", sme_result)):
        if result.get("parse_error"):
            log[key]["parse_error"] = True
    return log


def _compute_round_quality_metrics(current_ttl: str, round_num: int) -> dict:
    """라운드별 FAIR + Färber 품질 메트릭 측정. tempfile 로 직렬화 후 평가.

    Returns: {"fair": {...}, "farber": {...}} 또는 {"error": str} (실패 시).
    """
    try:
        import tempfile

        from tools.farber_dimensions import evaluate_farber
        from tools.foops_fair import evaluate_fair
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ttl", delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write(current_ttl)
            tmp_path = tmp.name
        try:
            fair_report = evaluate_fair(tmp_path, deep=False)
            farber_report = evaluate_farber(tmp_path)
            metrics = {
                "fair": {
                    "overall": fair_report.get("overall_score"),
                    "F": fair_report.get("findable", {}).get("score"),
                    "A": fair_report.get("accessible", {}).get("score"),
                    "I": fair_report.get("interoperable", {}).get("score"),
                    "R": fair_report.get("reusable", {}).get("score"),
                },
                "farber": {
                    "overall": farber_report.get("overall_score"),
                    "dimensions": farber_report.get("dimensions"),
                },
            }
            logger.info(
                "[Round %d] FAIR=%s, Färber=%s",
                round_num + 1,
                metrics["fair"].get("overall"),
                metrics["farber"].get("overall"),
            )
            return metrics
        finally:
            import contextlib as _ctx
            import os as _os_tmp
            with _ctx.suppress(OSError):
                _os_tmp.unlink(tmp_path)
    except Exception as metric_err:
        return {"error": str(metric_err)}


def _detect_and_record_veto(
    state: dict, validator_result: dict, sme_result: dict,
    prev_v_snapshot: list, prev_s_snapshot: list,
    round_num: int, round_log: dict,
) -> None:
    """2 라운드 연속 critical/high 동일 이슈 잔존 시 veto lock 발동 → state mutate.

    **매 라운드 재계산한다** (2026-08-18). 예전에는 True 로만 세팅하고 False 로
    되돌리는 코드가 소스 전체에 없어 **단방향 래치** 였다. 합의 조건에
    ``not veto_lock_triggered`` 가 걸려 있으므로, 어떤 이슈든 2라운드 연속
    잔존한 순간부터 남은 라운드는 어떤 수정으로도 합의에 도달할 수 없었다
    (실측 2026-08-17: R3 에 락 → R4·R5 20분+ 낭비).

    해제 사실은 ``round_log["veto_lock_released"]`` 에 남긴다 — 조용한 상태
    변화는 debate_log 만 보고 진단할 수 없다.

    **판정 불가 라운드는 락 상태를 건드리지 않는다.** 리뷰 JSON 파싱이 실패하면
    ``issues: []`` 센티넬이 오는데, 그걸 "이슈가 해소됐다" 로 읽으면 락이 **거짓으로
    해제**된다 (실측 2026-08-18: sentinel 2개로 호출하니 락이 풀리고
    ``veto_lock_released=True`` 가 기록됐다). 강화도 하지 않는다 — 보존된 스냅샷을
    현재 이슈로 넘기면 ``_detect_persistent_veto(prev, prev)`` 가 항상 persistent 를
    반환해 1회 관측 이슈를 "2라운드 연속" 으로 조작한다(반증에서 실측 확인).
    보류만 하고, 다음 정상 라운드에서 실제 상태로 재계산한다.
    """
    if validator_result.get("parse_error") or sme_result.get("parse_error"):
        round_log["veto_lock_hold"] = {
            "reason": "review_parse_error",
            "locked": bool(state.get("veto_lock_triggered")),
        }
        logger.warning(
            "[Veto Lock] 리뷰 파싱 실패 라운드 — 락 상태를 유지한다 "
            "(현재 locked=%s). 해제/강화 판정은 다음 정상 라운드로 미룬다.",
            bool(state.get("veto_lock_triggered")),
        )
        return
    v_veto_candidates = _collect_veto_issues(validator_result)
    s_veto_candidates = _collect_veto_issues(sme_result)
    persistent_v = _detect_persistent_veto(
        prev_v_snapshot, v_veto_candidates,
    ) if round_num >= 2 else []
    persistent_s = _detect_persistent_veto(
        prev_s_snapshot, s_veto_candidates,
    ) if round_num >= 2 else []
    if not (persistent_v or persistent_s):
        # 잔존 이슈가 사라졌으면 락을 해제한다. 이 라운드에 락을 걸 근거가
        # 없는데 이전 라운드 상태를 유지하면 그것이 래치다.
        if state.get("veto_lock_triggered"):
            state["veto_lock_triggered"] = False
            state["veto_persistent_targets"] = []
            round_log["veto_lock_released"] = True
            logger.info(
                "[Veto Lock] 잔존 critical/high 이슈 해소 — 락 해제 (Round %d)",
                round_num + 1,
            )
        return
    state["veto_lock_triggered"] = True
    state["veto_persistent_targets"] = [
        _issue_key(i) for i in (persistent_v + persistent_s)
    ]
    round_log["veto_lock"] = {
        "persistent_validator": len(persistent_v),
        "persistent_sme": len(persistent_s),
        "targets": state["veto_persistent_targets"][:10],
    }
    logger.warning(
        "[Veto Lock] 2라운드 연속 critical/high 잔존: v=%d, s=%d, targets=%s",
        len(persistent_v), len(persistent_s),
        state["veto_persistent_targets"][:5],
    )


def _apply_jury_required_fixes(
    state: dict, required_fixes: list[dict], round_log: dict,
    origin: str = "consensus",
) -> dict:
    """Jury 의 ``required_fixes`` 를 현재 TTL 에 적용하고 요약을 round_log 에 기록.

    **함수로 추출한 이유**: 예전에는 이 로직이 ``_handle_potential_consensus``
    안에 인라인이었고, 그래서 **예비 합의 분기에서만** 호출됐다. 중간 라운드에서
    Architect 수정이 정체돼도 아무도 개입하지 않았다 — 2026-08-19 실측: R1~R3 의
    ``jury_fixes_summary`` 가 전부 None 이고 R4 에서야 128건이 한꺼번에 적용됐다
    (94 적용). 정체 해소용으로 재사용하려면 사본을 만들 게 아니라 여기로 모아야
    한다 (사본이 갈라지면 한쪽만 라우팅 가드를 타는 사고가 이 리포에 있었다).

    Args:
        origin: 호출 맥락 — ``"consensus"`` (예비 합의 분기) /
                ``"no_progress"`` (무진전 개입). 로그·요약에 남겨 어느 경로가
                T-Box 를 바꿨는지 사후에 구분할 수 있게 한다.

    Returns:
        기록한 요약 dict (테스트가 산출물로 검증할 수 있게 반환).
    """
    # **라우팅**: 두 엔진이 아는 action 은 jury_fixes 로만 보낸다 —
    # DSL 에는 방어 가드가 없어서, 둘 다 태우면 DSL 이 가드 없이 적용한 뒤
    # jury_fixes 가 no-op 만 보고한다 (``_route_jury_fixes`` 주석 참조).
    dsl_only, jury_bound = _route_jury_fixes(required_fixes)
    ttl_after_dsl = state["current_ttl"]
    dsl_result: dict | None = None
    if dsl_only:
        try:
            dsl_result = _apply_dsl_instructions(state["current_ttl"], dsl_only)
            ttl_after_dsl = dsl_result["ttl"]
            state["current_ttl"] = ttl_after_dsl
        except Exception as jury_apply_err:
            ttl_after_dsl = state["current_ttl"]
            logger.warning(
                "[Jury] required_fixes DSL 적용 실패: %s", jury_apply_err,
            )
    summary: dict = {
        "requested": len(required_fixes),
        "routed_dsl": len(dsl_only),
        "routed_jury": len(jury_bound),
        "origin": origin,
    }
    # DSL 엔진의 폐기 내역을 jury_fixes 와 **같은 자리에** 싣는다. 예전에는 이 엔진이
    # TTL 문자열만 반환해 폐기가 어디에도 남지 않았고, 그것이 S2 에서 "지시가 조용히
    # 폐기됨" 이 네 번 재발한 진원지였다.
    if dsl_result is not None:
        summary["dsl"] = {
            "applied": dsl_result["applied_count"],
            "skipped": len(dsl_result["skipped"]),
            "failed": len(dsl_result["failed"]),
            "unknown": len(dsl_result["unknown"]),
        }
        dropped = (dsl_result["skipped"] + dsl_result["failed"]
                   + dsl_result["unknown"])
        if dropped:
            # 카운터가 아니라 **사유** 를 남긴다 — 정수만으로는 왜인지 알 수 없다.
            summary["dsl_dropped_details"] = dropped[:20]
    try:
        from tools.jury_fixes import apply_jury_fixes
        fx = apply_jury_fixes(ttl_after_dsl, jury_bound)
        state["current_ttl"] = fx["ttl"]
        summary.update({
            "applied": len(fx["applied"]),
            "noop": len(fx.get("noop") or []),
            "skipped": len(fx["skipped"]),
            "failed": len(fx["failed"]),
        })
        if fx["failed"]:
            # 실패를 카운터로만 남기면 조용히 유실된다 — 사유를 함께 기록한다
            # (실측: 19건 전부 ``add_functional_property: target 누락``).
            summary["failed_details"] = fx["failed"][:20]
        logger.info(
            "[Jury:%s] fixes — 요청=%d (dsl=%d jury=%d) ok=%d noop=%d failed=%d",
            origin, len(required_fixes), len(dsl_only), len(jury_bound),
            len(fx["applied"]),
            len(fx.get("noop") or []), len(fx["failed"]),
        )
    except Exception as fallback_err:
        summary["error"] = f"{type(fallback_err).__name__}: {str(fallback_err)[:200]}"
        logger.warning(
            "[Jury:%s] jury_fixes 폴백 실패 (DSL 결과 유지): %s",
            origin, fallback_err,
        )
    # 같은 라운드에서 두 경로가 모두 돌 수 있으므로 덮어쓰지 않고 누적한다.
    existing = round_log.get("jury_fixes_summary")
    if existing is None:
        round_log["jury_fixes_summary"] = summary
    elif isinstance(existing, list):
        existing.append(summary)
    else:
        round_log["jury_fixes_summary"] = [existing, summary]
    return summary


def _handle_potential_consensus(
    state: dict, validator_result: dict, sme_result: dict,
    ontoqa_metrics: dict, cq_runtime: dict, round_num: int,
    round_log: dict, summary: dict, set_phase: Callable[[str], None],
) -> bool:
    """예비 합의 분기 — Jury 호출 후 production_ready 면 break, 아니면 fix 적용.

    Returns: 합의 종료 여부 (True 면 caller 가 즉시 return).
    """
    set_phase("jury")
    try:
        jury_result = _jury_decide(
            state["current_ttl"], validator_result, sme_result,
            architect_history=state["debate_log"], debate_log=state["debate_log"],
            metrics=ontoqa_metrics,
            veto_targets=state["veto_persistent_targets"] if state["veto_lock_triggered"] else None,
            cq_runtime=cq_runtime,
        )
        round_log["jury"] = jury_result
        if jury_result.get("production_ready", False):
            logger.info("[Jury 승인] Round %d 최종 합의", round_num + 1)
            round_log["consensus"] = True
            state["consensus_reached"] = True
            summary["break_loop"] = True
            summary["consensus"] = True
            return True
        required_fixes = jury_result.get("required_fixes", [])
        if required_fixes:
            logger.info(
                "[Jury] production_ready=false, required_fixes %d건 적용",
                len(required_fixes),
            )
            _apply_jury_required_fixes(
                state, required_fixes, round_log, origin="consensus",
            )
        return False
    except Exception as e:
        # **fail closed.** 예전엔 여기서 consensus=True 를 선언했다 — Bedrock
        # throttling·timeout 같은 인프라 오류가 **만장일치 승인** 으로 번역돼,
        # 아무도 검토하지 않은 T-Box 가 "합의됨" 으로 저장됐다 (2026-08-09 규명).
        # Jury 는 최종 관문이므로, 판정을 못 얻었다면 합의가 아니다. 토론을 계속하고
        # (남은 라운드가 있으면) 없으면 최종 미합의 경로가 절충안을 만든다.
        logger.error(
            "[Jury] 판단 실패 — 합의로 간주하지 않고 토론 계속: %s", e,
        )
        round_log["consensus"] = False
        round_log["jury_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        state["consensus_reached"] = False
        summary["break_loop"] = False
        summary["consensus"] = False
        return False


class _JuryUndecided(RuntimeError):
    """Jury 가 판정을 내지 못했음 (파싱 실패 등) — 절충 경로로 넘기는 신호.

    예외로 만드는 이유: 기존 절충(compromise) 로직이 ``except`` 절에 있는데, 파싱
    실패는 센티넬 dict 로 **정상 반환** 되어 그 절을 건너뛰었다. 같은 처리 경로를
    타게 해 "판정 없음" 이 곧 "절충안 생성" 으로 이어지게 한다.
    """


def _handle_final_round_no_consensus(
    state: dict, validator_result: dict, sme_result: dict,
    ontoqa_metrics: dict, cq_runtime: dict, round_num: int, max_rounds: int,
    round_log: dict, summary: dict, set_phase: Callable[[str], None],
) -> None:
    """마지막 라운드에서도 미합의 — Jury 최종 판정 또는 Architect 절충."""
    logger.info("[미합의] %d라운드 완료, Jury 최종 판정 시도", max_rounds)
    set_phase("jury_final")
    try:
        jury_result = _jury_decide(
            state["current_ttl"], validator_result, sme_result,
            architect_history=state["debate_log"], debate_log=state["debate_log"],
            metrics=ontoqa_metrics,
            veto_targets=state["veto_persistent_targets"] if state["veto_lock_triggered"] else None,
            cq_runtime=cq_runtime,
        )
        round_log["jury_final"] = jury_result
        if jury_result.get("parse_error"):
            # 파싱 실패는 **판정이 아니다.** ``_parse_review_json`` 은 예외를 올리지
            # 않고 ``{"approved": false, "parse_error": true}`` 센티넬을 돌려주므로,
            # 아래 except 절에 걸린 절충(compromise) 경로가 실행되지 않았다. 그래서
            # 마지막 라운드가 **아무 결정도 내리지 못하고** 끝났다 (2026-08-09 실측:
            # jury_final = "파싱 실패 — 재검토 필요", compromise = null).
            # 명시적으로 절충 경로로 넘긴다.
            raise _JuryUndecided(
                str(jury_result.get("summary") or "Jury 응답 파싱 실패"),
            )
        required_fixes = jury_result.get("required_fixes", [])
        if required_fixes:
            # 중간 라운드와 **같은 라우터**를 쓴다 — 한쪽만 고치면 다른 경로에서
            # 같은 가드 우회가 계속된다 (두 지점이 같은 코드를 복사한 형태였다).
            dsl_only, jury_bound = _route_jury_fixes(required_fixes)
            ttl_after_dsl = state["current_ttl"]
            final_dsl_result: dict | None = None
            if dsl_only:
                try:
                    final_dsl_result = _apply_dsl_instructions(
                        state["current_ttl"], dsl_only,
                    )
                    ttl_after_dsl = final_dsl_result["ttl"]
                except Exception as e:
                    logger.warning("[Jury 최종] DSL 적용 실패 (fallback 진행): %s", e)
            try:
                from tools.jury_fixes import apply_jury_fixes
                fx = apply_jury_fixes(ttl_after_dsl, jury_bound)
                state["current_ttl"] = fx["ttl"]
                # 실패는 **사유까지** 기록한다. 예전엔 정수 3개만 남아
                # {applied:4, failed:37} 이 왜인지 알 수 없었고, 계산된 reason
                # 문자열은 대입 시점에 버려졌다 (2026-08-10 규명).
                #
                # no-op 도 사유를 남긴다 — 실측 (2026-08-17) 42건 중 29건이
                # no-op 이었는데 정수뿐이라 "가드에 막혔나 / 이미 만족됐나 /
                # 대상이 없나" 를 구분할 수 없었다.
                round_log["jury_fixes_summary"] = {
                    "requested": len(required_fixes),
                    "routed_dsl": len(dsl_only),
                    "routed_jury": len(jury_bound),
                    "applied": len(fx["applied"]),
                    "noop": len(fx.get("noop") or []),
                    "skipped": len(fx["skipped"]),
                    "failed": len(fx["failed"]),
                    "failed_details": [
                        {
                            "action": (f["action"].get("action", "?")
                                       if isinstance(f.get("action"), dict) else "?"),
                            "reason": str(f.get("reason"))[:160],
                        }
                        for f in (fx["failed"] + fx["skipped"])[:10]
                    ],
                    "noop_details": [
                        {
                            "action": (n["action"].get("action", "?")
                                       if isinstance(n.get("action"), dict) else "?"),
                            "reason": str(n.get("reason"))[:160],
                        }
                        for n in (fx.get("noop") or [])[:10]
                    ],
                }
                # DSL 엔진 통계도 같은 자리에. 이 경로는 **마지막 라운드** 이므로
                # 여기서 버려진 지시는 다시 시도되지 않는다 — 기록이 특히 중요하다.
                if final_dsl_result is not None:
                    round_log["jury_fixes_summary"]["dsl"] = {
                        "applied": final_dsl_result["applied_count"],
                        "skipped": len(final_dsl_result["skipped"]),
                        "failed": len(final_dsl_result["failed"]),
                        "unknown": len(final_dsl_result["unknown"]),
                    }
                    dropped = (final_dsl_result["skipped"]
                               + final_dsl_result["failed"]
                               + final_dsl_result["unknown"])
                    if dropped:
                        round_log["jury_fixes_summary"]["dsl_dropped_details"] = (
                            dropped[:20]
                        )
                logger.info(
                    "[Jury 최종] fixes 적용 — 요청=%d (dsl=%d jury=%d) "
                    "ok=%d noop=%d skipped=%d failed=%d",
                    len(required_fixes), len(dsl_only), len(jury_bound),
                    len(fx["applied"]),
                    len(fx.get("noop") or []), len(fx["skipped"]), len(fx["failed"]),
                )
            except Exception as e:
                logger.warning("[Jury 최종] jury_fixes 폴백 실패: %s", e)
                state["current_ttl"] = ttl_after_dsl
    except Exception as e:
        logger.warning("[Jury 최종] 판단 실패: %s", e)
        # T5: 이슈 fingerprint + persistence + priority 를 사전 계산해
        # Architect 에게 결정적 기준을 주입한다.
        #
        # 이 블록은 **이미 except 절 안** 이라 여기서 raise 되면 아래
        # break_loop=True / consensus=False 두 줄이 실행되지 않고 라운드를
        # 탈출한다 (실측: jury 와 compromise 가 같은 throttling 으로 연속 실패).
        # 절충안은 감사 기록일 뿐 TTL 을 바꾸지 않으므로, 실패해도 저장 경로를
        # 막아선 안 된다.
        try:
            priority_table = _build_priority_table(
                validator_result, sme_result, state["debate_log"],
                cq_coverage_pct=cq_runtime.get("coverage_pct", 0.0) / 100.0,
            )
            compromise_reason = _architect_compromise(
                state["current_ttl"], validator_result, sme_result,
                state["debate_log"],
                priority_table=priority_table,
            )
            state["compromise_reason"] = compromise_reason
            round_log["compromise"] = compromise_reason
            round_log["compromise_priority_table"] = priority_table
        except Exception as comp_err:
            detail = f"{type(comp_err).__name__}: {str(comp_err)[:200]}"
            logger.error(
                "[Jury 최종] 절충안 생성도 실패 — 산출물은 그대로 저장한다: %s",
                detail,
            )
            round_log["compromise_error"] = detail
            state["infra_abort"] = {
                "phase": "architect_compromise",
                "round": round_num + 1,
                "error": detail,
            }
    summary["break_loop"] = True
    summary["consensus"] = False


def _inject_cq_skeletons_if_gap(
    state: dict, cq_runtime: dict, cqs: list, round_num: int, round_log: dict,
) -> None:
    """답변 불가 CQ 가 있으면 OP skeleton 을 생성해 jury_fixes 로 주입."""
    if not cq_runtime.get("unanswerable"):
        return
    cq_skeletons = _generate_op_skeletons_from_cq_gaps(cq_runtime, cqs)
    if not cq_skeletons:
        return
    try:
        from tools.jury_fixes import apply_jury_fixes
        fx = apply_jury_fixes(state["current_ttl"], cq_skeletons)
        state["current_ttl"] = fx["ttl"]
        round_log["cq_skeleton_injection"] = {
            "generated": len(cq_skeletons),
            "applied": len(fx["applied"]),
            "skipped": len(fx["skipped"]),
            "failed": len(fx["failed"]),
        }
        logger.info(
            "[Round %d] CQ gap 스켈레톤 주입 — gen=%d applied=%d",
            round_num + 1, len(cq_skeletons), len(fx["applied"]),
        )
    except Exception as inj_err:
        logger.warning(
            "[Round %d] CQ 스켈레톤 주입 실패: %s", round_num + 1, inj_err,
        )


#: DSL 리터럴 값에 붙어 오는 언어 태그 (``"설비 이름@ko"``).
_LANG_SUFFIX_RE = re.compile(r"^(.*)@([a-z]{2}(?:-[A-Za-z0-9]+)?)$")

#: Turtle 익명 노드 표현식 (``[ a owl:Restriction ; … ]``) 또는 컬렉션 (``( a b )``).
#: LLM 이 클래스 정의 공리를 이 형태로 보낸다.
_ANON_EXPR_RE = re.compile(r"^\s*(\[.*\]|\(.*\))\s*$", re.DOTALL)


def _parse_anon_expression(raw: str, graph: Graph):
    """Turtle 익명 노드 표현식을 **실제 그래프 구조** 로 파싱해 루트 노드를 준다.

    ## 왜 필요한가 (2026-08-27 실측)

    LLM 은 클래스 정의 공리를 Turtle 익명 노드로 보낸다:

        {"action": "add_triple", "subject": "steel:RunningEquipmentStatus",
         "predicate": "owl:equivalentClass",
         "object": "[ a owl:Restriction ; owl:onProperty steel:equipmentStatusStatus ;
                      owl:hasValue \\"Running\\" ]"}

    ``_dsl_term`` → ``parse_object_term`` 은 이것을 판정 순서 4번("그 외 → 평문
    리터럴")으로 떨어뜨려 **문자열로 저장**했다. 그리고 ``applied += 1`` 로
    **"적용 1/1 성공"** 을 보고했다 — 조용한 손실이다.

    배포 T-Box 실측: ``owl:equivalentClass`` 40건 중 **15건의 object 가 Literal** 이고,
    전부 Turtle 소스 텍스트다. ``RunningEquipmentStatus`` · ``Scope1/2/3Emission`` ·
    ``HighSeverityAlarmEvent`` 등 값 기반 서브클래스가 여기 몰려 있다. OWL DL 위반이고
    추론기·SPARQL 에 무의미하므로, 그 클래스들은 **A-Box 0건 / 추론 0건**의 빈 클래스로
    남았다. S2 리뷰어가 그것을 "불완전 분할" critical 26건으로 지적해 3회 실행이 합의에
    실패했다.

    이 리포가 이미 겪은 계열이다 — 익명 클래스 표현식 하나가 클래스 열거를 오염시켜
    품질 게이트를 죽인 이력이 있다.

    ## 왜 parse_object_term 을 고치지 않는가

    그 함수의 계약은 "**삭제/교체 매칭용** term 해석" 이다. 익명 표현식은 매칭 대상이
    아니라 **새 구조를 만드는 것** 이므로 성격이 다르고, 그래프에 노드를 추가하는 부작용을
    그 함수에 넣으면 삭제 경로가 그래프를 오염시킨다. 호출부에서 분기한다.

    Returns:
        파싱 성공 시 루트 노드(BNode). 실패하면 ``None`` — 호출부가 skip 으로 기록한다
        (문자열로 저장하는 것보다 낫다: 그것이 이번 결함이었다).
    """
    from rdflib import Graph as _RG

    text = raw.strip()
    # 앵커 트리플을 만들어 조각을 파싱한다. prefix 는 대상 그래프의 바인딩을 재사용해야
    # 하고(steel: / owl: / xsd: …), 그러지 않으면 미등록 prefix 로 파싱이 실패한다.
    prefixes = "".join(
        f"@prefix {p}: <{u}> .\n" for p, u in graph.namespaces()
    )
    # 앵커는 **http 스킴** 을 쓴다. urn: 형태는 rdflib 가 "유효 URI 로 보이지 않는다" 며
    # 경고하고 직렬화가 깨진다 (실측).
    anchor_s = "http://oa.local/anon#anchor"
    anchor_p = "http://oa.local/anon#root"
    snippet = f"{prefixes}<{anchor_s}> <{anchor_p}> {text} .\n"
    tmp = _RG()
    try:
        tmp.parse(data=snippet, format="turtle")
    except Exception as exc:  # noqa: BLE001 — 파싱 실패는 skip 사유로 보고한다
        logger.warning(
            "익명 표현식 파싱 실패 — 문자열로 저장하지 않고 건너뛴다: %s (%s)",
            text[:80], exc,
        )
        return None
    root = tmp.value(URIRef(anchor_s), URIRef(anchor_p))
    if root is None:
        return None
    # 앵커 트리플만 빼고 나머지 구조를 대상 그래프로 옮긴다.
    moved = 0
    for s, p, o in tmp:
        if str(s) == anchor_s:
            continue
        graph.add((s, p, o))
        moved += 1
    logger.info(
        "익명 표현식을 구조로 파싱했다 — 트리플 %d개 추가 (%s)", moved, text[:60],
    )
    return root


def _dsl_term(val, graph=None, *, as_iri: bool = False):
    """``add_triple`` / ``remove_triple`` DSL 값 → rdflib term.

    이전 구현(``_to_node``)은 **도메인 prefix 만** IRI 로 인식하고 나머지는 전부
    ``Literal`` 로 격하시켰다. 그래서 LLM 이 흔히 쓰는 ``rdfs:subClassOf`` /
    ``rdfs:domain`` / ``dcterms:source`` 같은 predicate 가 Literal 이 되어 rdflib
    이 트리플을 거부했고, 지시는 **조용히 버려졌다** — S2 재실행 실측 101건
    (subClassOf 44 / domain·range 26 / dcterms:source 15 등 구조 수정이 대부분).
    "적용된 것처럼 보이지만 안 됨" 이라는 유령 IRI 와 같은 실패 계열이다.

    Args:
        as_iri: subject/predicate 처럼 **IRI 여야 하는** 자리. 리터럴로 해석될
            값이 와도 IRI 로 강제한다 — 그 자리에 Literal 을 넣으면 rdflib 이
            트리플 전체를 거부해 지시가 사라진다.

    object 자리는 IRI 와 리터럴이 모두 정당하므로 prefixed name / 완전 IRI 만
    IRI 로 보고, 나머지는 ``"텍스트@ko"`` 언어 태그를 해석해 Literal 로 만든다.
    """
    if val is None or (isinstance(val, str) and not val.strip()):
        logger.warning("add_triple/remove_triple: 빈 값 무시")
        return None
    if not isinstance(val, str):
        return Literal(val)

    text = val.strip()
    if as_iri:
        return _resolve_dsl_name(text, graph, DOMAIN_NS)
    if text.startswith(("http://", "https://", "urn:")):
        return URIRef(text)
    # object 자리는 IRI 와 리터럴이 모두 정당하므로 **알려진 prefix 일 때만** IRI 로
    # 승격한다. 미등록 prefix 까지 승격하면 값이 조용히 IRI 가 된다 — 실측: LLM 이
    # `dcterms:source` 값으로 `"xsd:string"` 을 보냈고, 그것을 IRI 로 승격해
    # `steel:string` 이 컬럼명 자리에 들어갔다 (배포 T-Box 27건). A-Box 는 이 값을
    # CSV 헤더와 비교하므로 매칭이 영구히 실패한다.
    #
    # xsd: 는 **데이터타입 네임스페이스** 라 object 자리에서는 값이 아니다
    # (range 자리는 as_iri=True 경로가 따로 처리한다) → 리터럴로 둔다.
    head, _, rest = text.partition(":")
    if rest and " " not in text and head and head.replace("-", "").isalnum():
        from domain.namespaces import FOREIGN_PREFIXES as _FP
        if head == NS_PREFIX or (head in _FP and head != "xsd"):
            return _resolve_dsl_name(text, graph, DOMAIN_NS)
        if graph is not None and head != "xsd":
            try:
                if head in dict(graph.namespace_manager.namespaces()):
                    return _resolve_dsl_name(text, graph, DOMAIN_NS)
            except Exception as exc:  # noqa: BLE001 — 조회 실패 시 리터럴로
                logger.debug("prefix 바인딩 조회 실패, 리터럴로 처리: %s", exc)
    m = _LANG_SUFFIX_RE.match(text)
    if m:
        return Literal(m.group(1), lang=m.group(2))
    return Literal(text)


def _abort_round_on_infra_fault(
    state: dict, exc: Exception, *, phase: str, round_num: int,
    metrics_feedback: str = "", round_log: dict | None = None,
) -> dict | None:
    """인프라 장애로 라운드를 중단하되 **지금까지의 산출물은 살린다**.

    2026-08-09 규명: Jury 실패를 fail-closed 로 고친 뒤에도, 바로 뒤에 오는
    ``_architect_revise`` / ``_run_validator_and_sme`` / ``_architect_compromise``
    가 같은 throttling 으로 raise 하면 예외가 라운드를 탈출해
    ``_finalize_and_save`` 가 아예 실행되지 않았다. 그 결과 **검토를 마친 이전
    T-Box 가 이 실행의 미검토 초안으로 대체된 채 남는다** — ``generate_tbox`` 가
    토론 시작 전에 ``TBOX_PATH`` 를 무조건 덮어쓰고(:1594 상당),
    ``t_box_baseline.ttl`` 은 두 writer 모두 ``if not exists`` 라 최초 실행에
    동결돼 있어 롤백 경로가 아니다.

    2026-08-14 보강: 초안이 더 이상 ``TBOX_PATH`` 를 덮어쓰지 않는다
    (``generate_tbox(save_to_tbox_path=False)``). 따라서 라운드 도중 예외로
    ``_finalize_and_save`` 에 도달하지 못하면 **기존 T-Box 가 그대로 남는다** —
    조용한 downgrade 경로가 닫혔다. 이 함수의 역할은 이제 "살릴 만한 검토
    산출물이 있을 때만 저장" 으로 좁아진다.

    Returns:
        정상 종료용 summary dict — 호출부는 그대로 ``return`` 한다.
        ``None`` — **이 예외는 삼켜서는 안 된다**. 호출부는 ``raise`` 해야 한다.
        두 경우:

        - 인프라 장애가 아님 (``KeyError`` 등 계약 위반, ``MemoryError``):
          코드 버그를 "합의 실패로 저장됨" 으로 숨기면 영구히 안 보인다.
        - 토론 기록이 0건: 라운드 1의 리뷰조차 못 한 상태라 저장할 "검토된"
          산출물이 없다. 미검토 초안을 ``success`` 로 보고하는 것이 더 나쁘다.
    """
    from tools.bedrock import is_infra_fault

    if not is_infra_fault(exc):
        return None
    if not state.get("debate_log"):
        logger.error(
            "[Round %d] %s 인프라 장애 — 토론 기록이 없어 저장할 검토 산출물이 "
            "없다. 예외를 전파한다: %s", round_num + 1, phase, exc,
        )
        return None

    detail = f"{type(exc).__name__}: {str(exc)[:200]}"
    logger.error(
        "[Round %d] %s 인프라 장애 — 이전 TTL 유지하고 토론을 중단한다 "
        "(지금까지의 산출물은 저장됨): %s", round_num + 1, phase, detail,
    )
    state["infra_abort"] = {
        "phase": phase, "round": round_num + 1, "error": detail,
    }
    state["consensus_reached"] = False
    log = round_log if round_log is not None else {"round": round_num + 1}
    log["infra_error"] = detail
    log["consensus"] = False
    if round_log is None:
        state["debate_log"].append(log)

    # 호출부(:3358~3390)가 break_loop 검사 **전에** 7개 키를 모두 읽으므로
    # 부분 dict 를 반환하면 KeyError 로 같은 손실이 되살아난다.
    prev = state["debate_log"][-1] if state["debate_log"] else {}
    return {
        "round_log": log,
        "break_loop": True,
        "consensus": False,
        "metrics_feedback": metrics_feedback,
        "v_issues_count": len(state.get("prev_validator_issues") or []),
        "s_issues_count": len(state.get("prev_sme_issues") or []),
        "v_approved": False,
        "s_approved": False,
        "cq_coverage_pct": (prev.get("cq_runtime") or {}).get("coverage_pct", 0.0),
    }


def _intervene_on_no_progress(
    state: dict, validator_result: dict, sme_result: dict,
    ontoqa_metrics: dict, cq_runtime: dict, round_num: int,
    round_log: dict, streak: int,
) -> bool:
    """무진전 라운드에서 Jury 를 즉시 호출해 결정적 수정으로 정체를 뚫는다.

    **왜 필요한가** (2026-08-19 실측, 58.5분 실행):

    ==== ======== ==== ==================================
    R    V이슈    SME  Architect 수정
    ==== ======== ==== ==================================
    R1   12       7    트리플 2,833
    R2   14       9    3,365
    R3   12       5    3,365 — ``no_progress``
    R4   14       5    ``revision: null``
    ==== ======== ==== ==================================

    이슈 수가 12→14→12→14 로 **전혀 수렴하지 않았고**, SME 는 R2·R4 에서
    "T-Box 가 변경되지 않아" 를 명시했다 (지문 비교로 사실 확인됨). 무진전 감지
    장치는 이미 있었지만 **조기 종료에만** 쓰여, 정체를 해소하려는 시도는 없었다.
    ``jury_fixes_summary`` 가 R1~R3 전부 None 이고 마지막 라운드에서야 128건이
    한꺼번에 적용됐다 (94 반영) — 그 개입이 정체 시점에 있었다면 남은 라운드가
    바뀐 T-Box 를 리뷰하는 유효한 토론이 됐다.

    **왜 Jury 인가**: Architect 는 LLM 이 TTL 을 다시 써야 하는데 이미 두 번
    실패했다 (같은 프롬프트로 세 번째를 시도할 근거가 없다). Jury 의
    ``required_fixes`` 는 **코드가 결정적으로 적용** 하므로 LLM 의 재작성 능력에
    의존하지 않는다.

    **비용**: Jury 호출 1회 (약 1~2분). 절감은 무효 라운드 2개 (약 20분).

    Returns:
        실제로 T-Box 가 바뀌었으면 True. 개입 실패/변경 없음이면 False —
        호출부는 이 값으로 streak 를 리셋하지 않는다 (거짓 진전 금지).
    """
    if os.getenv("MULTI_AGENT_NO_PROGRESS_INTERVENE", "true").lower() not in (
        "true", "1", "yes",
    ):
        logger.info("[정체 개입] 비활성 (MULTI_AGENT_NO_PROGRESS_INTERVENE)")
        return False

    before_fp = _graph_fingerprint(state["current_ttl"])
    try:
        jury_result = _jury_decide(
            state["current_ttl"], validator_result, sme_result,
            architect_history=state["debate_log"], debate_log=state["debate_log"],
            metrics=ontoqa_metrics,
            # 정체 시점에는 veto target 을 **항상** 넘긴다. 그것이 Architect 가
            # 두 라운드 동안 못 뚫은 항목이고, Jury 가 강제할 대상이다.
            veto_targets=state.get("veto_persistent_targets") or None,
            cq_runtime=cq_runtime,
        )
    except Exception as exc:
        # 개입 실패는 라운드를 죽이지 않는다 — 정체 상태 그대로 진행/종료한다.
        # (인프라 오류를 진전으로 오해하면 안 되므로 False 를 돌린다.)
        logger.warning(
            "[정체 개입] Jury 호출 실패 — 정체 상태 유지: %s", exc,
        )
        round_log["no_progress_intervention"] = {
            "attempted": True, "streak": streak,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
        return False

    required_fixes = jury_result.get("required_fixes") or []
    record: dict = {
        "attempted": True,
        "streak": streak,
        "jury_production_ready": bool(jury_result.get("production_ready")),
        "required_fixes": len(required_fixes),
    }
    if not required_fixes:
        # Jury 가 고칠 것이 없다고 판단했다 — 그런데 리뷰어는 미승인이다.
        # 이 불일치는 사람이 봐야 하는 신호이므로 조용히 넘기지 않는다.
        logger.warning(
            "[정체 개입] Jury 가 required_fixes 를 내지 않았다 — 리뷰어는 미승인 "
            "(v=%s, s=%s) 인데 Jury 는 강제할 수정이 없다고 본다. 정체가 "
            "모델링 판단 차이일 수 있다.",
            validator_result.get("approved"), sme_result.get("approved"),
        )
        record["changed"] = False
        round_log["no_progress_intervention"] = record
        return False

    logger.info(
        "[정체 개입] Round %d — Jury required_fixes %d건을 즉시 적용한다 "
        "(Architect 가 %d라운드 연속 무변경)",
        round_num + 1, len(required_fixes), streak,
    )
    _apply_jury_required_fixes(
        state, required_fixes, round_log, origin="no_progress",
    )

    # **산출물로 판정한다.** 카운터(applied>0)를 믿지 않는다 — 이 리포에서 적용
    # 보고와 실제 그래프가 어긋난 사고가 반복됐다. 지문이 바뀌었는지만 본다.
    after_fp = _graph_fingerprint(state["current_ttl"])
    changed = after_fp != before_fp
    record["changed"] = changed
    round_log["no_progress_intervention"] = record
    if changed:
        # 다음 라운드의 무진전 판정 기준을 개입 후 상태로 갱신한다. 하지 않으면
        # 다음 라운드가 "개입으로 바뀐 것" 을 자기 진전으로 오인한다.
        state["last_revision_fingerprint"] = after_fp
        logger.info(
            "[정체 개입] T-Box 가 실제로 변경됐다 — 다음 라운드는 바뀐 T-Box 를 "
            "리뷰한다",
        )
    else:
        logger.warning(
            "[정체 개입] required_fixes %d건을 적용했지만 **그래프가 바뀌지 "
            "않았다** (전부 no-op 이거나 실패). 정체는 해소되지 않았다.",
            len(required_fixes),
        )
    return changed


def _run_one_debate_round(
    state: dict, round_num: int, max_rounds: int,
    phase_setter: Callable[[str], None] | None = None,
) -> dict:
    """한 토론 라운드를 실행. state를 변경(mutate)하고 요약 dict 반환.

    Args:
        phase_setter: 호출되면 현재 phase 라벨을 외부 공유 변수에 기록. 하트비트 리포터가
            이 라벨을 읽어 "Round K 진행 중 [phase=validator_sme] — 경과 Xs" 식으로 브로드캐스트.
            None 이면 phase 전환 기록 생략.

    Returns:
        {round_log, break_loop, consensus, metrics_feedback,
         v_issues_count, s_issues_count, v_approved, s_approved, cq_coverage_pct}
    """
    def set_phase(label: str) -> None:
        if phase_setter is None:
            return
        try:
            phase_setter(label)
        except Exception as _pe:
            logger.debug("phase_setter 실패 (무시): %s", _pe)

    current_ttl = state["current_ttl"]
    cqs = state["cqs"]

    logger.info(
        "[Round %d] Semantic Validator + Manufacturing SME — 챌린지", round_num + 1,
    )

    metrics_feedback = _compute_metrics_feedback(current_ttl, round_num)
    ontoqa_metrics = _compute_tbox_metrics(current_ttl)

    extra = state["extra_context"] if round_num == 1 else ""
    # 마크다운(메트릭 피드백 / extra_context)은 **절대** TTL 앞에 붙이지 않는다.
    # 붙이면 _format_declaration_inventory 와 _format_ttl_diff_block 의 rdflib
    # 파싱이 첫 줄에서 깨져 리뷰어가 전수 선언 목록을 못 받는다 (2026-08-14 실측:
    # 8,389자 → 0자). 서술은 preamble 로, TTL 은 순수하게 각각 전달한다.
    # 직전 라운드에 "CSV 데이터 부재" 로 판정된 CQ 갭을 리뷰어에게 알린다. 이것이
    # 없으면 리뷰어가 S2 로 해결 불가능한 갭을 critical 로 지적하고 approved=false
    # 를 고정한다 (실측: 3 run 11라운드 승인 0건, 그중 10라운드가 전부 data_gap).
    from tools.multi_agent_prompts import cq_data_gap_block
    data_gap_block = cq_data_gap_block(state.get("prev_cq_data_gap"))
    review_preamble = ""
    if metrics_feedback or extra or data_gap_block:
        review_preamble = metrics_feedback + extra + data_gap_block + "\n"
    ttl_for_review = current_ttl
    prev_ttl_for_diff = state["prev_round_ttl"]

    parallel_enabled = (
        os.getenv("MULTI_AGENT_PARALLEL", "true").lower() in ("true", "1", "yes")
        and not _strict_determinism()
    )

    set_phase("validator+sme")
    try:
        validator_result, sme_result = _run_validator_and_sme(
            state, ttl_for_review, prev_ttl_for_diff,
            ontoqa_metrics, round_num, parallel_enabled,
            preamble=review_preamble,
        )
    except Exception as exc:
        summary = _abort_round_on_infra_fault(
            state, exc, phase="validator_sme", round_num=round_num,
            metrics_feedback=metrics_feedback,
        )
        if summary is None:
            raise
        return summary
    state["prev_round_ttl"] = current_ttl

    # Veto 감지는 업데이트 전 비교가 필요 — 스냅샷 먼저 확보 후 state 갱신.
    #
    # **파싱 실패 라운드는 이전 이슈를 유지한다.** 예전에는 무조건 덮어써서
    # ``parse_error`` 센티넬의 빈 리스트가 들어갔고, 다음 라운드
    # ``_detect_persistent_veto`` 가 ``if not prev_issues: return []`` 로 빠져
    # persistence 판정이 리셋됐다 (실측: 이슈 수 13→19→0→13 진동).
    prev_v_snapshot = state["prev_validator_issues"]
    prev_s_snapshot = state["prev_sme_issues"]
    state["prev_validator_issues"] = _merge_review_snapshot(
        prev_v_snapshot, validator_result,
    )
    state["prev_sme_issues"] = _merge_review_snapshot(
        prev_s_snapshot, sme_result,
    )
    if validator_result.get("parse_error") or sme_result.get("parse_error"):
        logger.error(
            "[Round %d] 리뷰 JSON 파싱 실패 (validator=%s sme=%s) — 이전 라운드 "
            "미해결 이슈 %d/%d 건을 유지해 Architect 에게 전달한다. 이 라운드의 "
            "리뷰 내용은 유실됐다.",
            round_num + 1,
            bool(validator_result.get("parse_error")),
            bool(sme_result.get("parse_error")),
            len(state["prev_validator_issues"]), len(state["prev_sme_issues"]),
        )

    # LLM 의 approved 를 그대로 쓰지 않는다. 거부 근거가 S3 담당 항목·데이터 갭
    # 뿐이면 승인으로 본다 — 프롬프트가 이미 그렇게 지시하지만 실측 3 run 11라운드
    # 에서 승인이 **0건** 이었다 (사유는 dcterms:source 0%, RR 미달, IOF 정렬 등
    # 전부 S3 가 하는 일). 승인 → 거부 방향으로는 뒤집지 않는다.
    v_approved, v_blocking = _effective_approval(validator_result)
    s_approved, s_blocking = _effective_approval(sme_result)
    if v_approved and not validator_result.get("approved"):
        logger.info(
            "[승인 재판정] Validator approved=false 이지만 차단 이슈 0건 — "
            "거부 근거가 S3 담당/데이터 갭뿐이므로 승인으로 본다",
        )
    if s_approved and not sme_result.get("approved"):
        logger.info(
            "[승인 재판정] SME approved=false 이지만 차단 이슈 0건 — "
            "거부 근거가 S3 담당/데이터 갭뿐이므로 승인으로 본다",
        )

    round_log = _build_round_log(
        validator_result, sme_result, round_num, parallel_enabled,
    )
    state["debate_log"].append(round_log)
    logger.info(
        "[Round %d] Validator: %d이슈 (approved=%s), SME: %d이슈 (approved=%s)",
        round_num + 1,
        len(validator_result.get("issues", [])), v_approved,
        len(sme_result.get("issues", [])), s_approved,
    )

    cq_runtime = _cq_runtime_check(current_ttl, cqs)
    round_log["cq_runtime"] = {
        "coverage_pct": cq_runtime["coverage_pct"],
        "unanswerable_count": len(cq_runtime["unanswerable"]),
        "unanswerable_sample": cq_runtime["unanswerable"][:3],
    }
    logger.info(
        "[Round %d] CQ 런타임: %.1f%% (%d/%d 답변 가능)",
        round_num + 1, cq_runtime["coverage_pct"],
        len(cq_runtime["answerable"]), len(cqs) if cqs else 0,
    )

    quality_metrics = _compute_round_quality_metrics(current_ttl, round_num)
    if quality_metrics:
        round_log["quality_metrics"] = quality_metrics

    cq_ops_missing = cq_runtime["unanswerable"] or sme_result.get("unanswerable_cqs", [])
    # 갭을 **고칠 수 있는 것** 과 **CSV 데이터 갭** 으로 나눈다. 후자는 T-Box
    # 수정으로 해소 불가능하므로 합의를 막지 않는다 — 막으면 Architect 가
    # 존재하지 않는 해법을 찾으며 라운드를 태운다(실측: 48분 중 후반 절반).
    # 대신 별도 필드로 보고해 사람이 CSV 보강(augment_csv_fk / tacit)을 판단한다.
    cq_block_fixable, cq_block_data_gap = _split_cq_gaps_by_fk_evidence(cq_ops_missing)
    if cq_ops_missing:
        logger.warning(
            "[CQ 강제] 답변 불가 CQ %d건 (고칠 수 있음 %d / 데이터 갭 %d — "
            "데이터 갭은 합의를 막지 않는다): %s",
            len(cq_ops_missing), len(cq_block_fixable), len(cq_block_data_gap),
            cq_ops_missing[:3],
        )
        round_log["cq_block"] = cq_ops_missing
        if cq_block_data_gap:
            round_log["cq_block_data_gap"] = cq_block_data_gap
        if cq_block_fixable:
            round_log["cq_block_fixable"] = cq_block_fixable

    # 다음 라운드 리뷰어 프롬프트에 넘긴다. 이 라운드에 넣을 수는 없다 — 리뷰어
    # 호출이 이 판정보다 앞선다. 판정 결과를 리뷰어가 못 보면 "CSV 에 데이터가 없어
    # 고칠 수 없는 CQ" 를 critical 로 지적하고, 그것이 approved=false 를 고정한다
    # (실측: 3 run 11라운드 중 10라운드가 전부 data_gap 이었는데 승인 0건).
    state["prev_cq_data_gap"] = cq_block_data_gap

    _detect_and_record_veto(
        state, validator_result, sme_result,
        prev_v_snapshot, prev_s_snapshot, round_num, round_log,
    )

    summary = {
        "round_log": round_log,
        "break_loop": False,
        "consensus": False,
        "metrics_feedback": metrics_feedback,
        "v_issues_count": len(validator_result.get("issues", [])),
        "s_issues_count": len(sme_result.get("issues", [])),
        "v_approved": v_approved,
        "s_approved": s_approved,
        "cq_coverage_pct": cq_runtime["coverage_pct"],
    }
    # 재판정이 실제로 발화했는지 산출물에서 확인할 수 있게 남긴다. 카운터만으로는
    # "차단 이슈가 없어서 승인" 과 "LLM 이 승인" 을 구분할 수 없다.
    round_log["approval_recheck"] = {
        "validator": {
            "llm_approved": bool(validator_result.get("approved")),
            "effective": v_approved,
            "blocking": len(v_blocking),
            "blocking_sample": [
                str(i.get("target") or i.get("summary") or "")[:80] for i in v_blocking[:5]
            ],
        },
        "sme": {
            "llm_approved": bool(sme_result.get("approved")),
            "effective": s_approved,
            "blocking": len(s_blocking),
            "blocking_sample": [
                str(i.get("target") or i.get("summary") or "")[:80] for i in s_blocking[:5]
            ],
        },
    }

    # 표현가능성 계측 — 남은 차단 이슈를 **현재 어휘로 고칠 수 있는가** 로 나눈다.
    # 이 수치가 조기 종료의 판정 근거다 (_expressibility_verdict 참조).
    expressibility = _measure_expressibility(v_blocking, s_blocking)
    round_log["expressibility"] = expressibility
    if expressibility["blocking_total"]:
        logger.info(
            "[Round %d] 차단 이슈 표현가능성: 실행가능 %d / SME소유 %d / "
            "표현불가 %d / 미지정 %d (실행가능 %.0f%%)",
            round_num + 1, expressibility["executable"],
            expressibility["sme_owned"], expressibility["inexpressible"],
            expressibility["unspecified"], expressibility["executable_pct"],
        )

    # 조기 종료 — 남은 차단 이슈 중 **고칠 수 있는 것이 하나도 없으면** 더 도는
    # 것이 무의미하다. 8런 실측: 그런 라운드가 9개 있었고 라운드당 평균 711초를
    # 소모했다 (총 1.78시간). 자세한 근거는 _should_stop_no_expressible 참조.
    if _should_stop_no_expressible(state, expressibility, round_num, round_log):
        summary["break_loop"] = True
        summary["stop_reason"] = "no_expressible_blocking_fix"
        return summary

    # 합의 분기 — 차단 조건은 **고칠 수 있는** 갭만 본다.
    if (v_approved and s_approved and round_num >= _MIN_DEBATE_ROUNDS
            and not cq_block_fixable and not state["veto_lock_triggered"]):
        logger.info(
            "[예비 합의] Round %d 두 비평가 pass. Jury 독립 심판 호출", round_num + 1,
        )
        if _handle_potential_consensus(
            state, validator_result, sme_result, ontoqa_metrics,
            cq_runtime, round_num, round_log, summary, set_phase,
        ):
            return summary

    # 마지막 라운드에서도 미합의
    if round_num == max_rounds:
        _handle_final_round_no_consensus(
            state, validator_result, sme_result, ontoqa_metrics,
            cq_runtime, round_num, max_rounds, round_log, summary, set_phase,
        )
        return summary

    # CQ gap skeleton injection (pre-Architect revise)
    _inject_cq_skeletons_if_gap(state, cq_runtime, cqs, round_num, round_log)

    # Architect 수정
    logger.info("[Round %d] Ontology Architect — 챌린지 반영 수정", round_num + 1)
    set_phase("architect")
    veto_targets_for_revise = (
        state["veto_persistent_targets"] if state["veto_lock_triggered"] else None
    )
    # 파싱 실패한 리뷰는 이전 라운드 이슈로 채워 넘긴다 — 센티넬을 그대로 주면
    # ``_architect_revise`` 가 빈 issues 를 직렬화해 **수정 지시가 사라진다**
    # (실측: R4·R5 revision triples 가 3222 로 동일 = Architect 무변경).
    # Architect DSL 의 적용/폐기 통계를 받는다. 이 경로가 S2 최대 폐기 지점인데
    # 예전에는 통계가 로그로만 나가 round_log·응답에 흔적이 없었다.
    architect_dsl_stats: dict = {}
    try:
        revised_ttl = _architect_revise(
            state["current_ttl"],
            _review_for_architect(validator_result, prev_v_snapshot),
            _review_for_architect(sme_result, prev_s_snapshot),
            round_num,
            veto_targets=veto_targets_for_revise,
            dsl_stats_out=architect_dsl_stats,
        )
    except Exception as exc:
        # 이 라운드의 리뷰는 이미 debate_log 에 들어갔고 current_ttl 도 온전하다.
        # 수정본만 못 받았으므로 직전 TTL 을 그대로 최종 산출물로 저장한다.
        aborted = _abort_round_on_infra_fault(
            state, exc, phase="architect_revise", round_num=round_num,
            metrics_feedback=metrics_feedback, round_log=round_log,
        )
        if aborted is None:
            raise
        summary.update(aborted)
        return summary
    try:
        g = _new_graph()
        g.parse(data=revised_ttl, format="turtle")
        state["current_ttl"] = revised_ttl
        round_log["revision"] = {"valid": True, "triples": len(g)}
        # DSL 통계를 **no_progress 와 같은 자리에** 둔다. "T-Box 가 안 바뀌었다" 와
        # "지시 N건이 버려졌다" 를 나란히 봐야 원인이 Architect 무응답인지 폐기인지
        # 갈린다 — 예전에는 후자를 알 방법이 없어 전자로 오진했다.
        #
        # 빈 dict 도 기록한다. 2026-08-27 검증 실행에서 이 자리가 3라운드 전부
        # 비었는데, 원인은 "폐기가 0" 이 아니라 Architect 가 **DSL 경로를 타지 않은**
        # 것이었다 (outcome=ttl_fallback). 필드 부재와 값 0 을 구분할 수 없으면
        # no_progress 의 원인을 영원히 오진한다.
        round_log["revision"]["dsl"] = architect_dsl_stats or {"outcome": "not_invoked"}
        # 무진전 감지: 트리플 **집합** 지문을 이전 라운드와 비교한다. 수만 보면
        # add 1 / remove 1 이 상쇄돼 변경을 놓친다. 실측(2026-08-17): R4·R5 가
        # 3222 로 동일했는데 아무도 알아채지 못하고 20분+ 를 태웠다.
        fingerprint = _graph_fingerprint(revised_ttl)
        prev_fp = state.get("last_revision_fingerprint")
        no_progress = prev_fp is not None and fingerprint == prev_fp
        round_log["revision"]["no_progress"] = no_progress
        state["last_revision_fingerprint"] = fingerprint
        streak = (state.get("no_progress_streak", 0) + 1) if no_progress else 0
        state["no_progress_streak"] = streak
        if no_progress:
            round_log["revision"]["no_progress_streak"] = streak
            logger.warning(
                "[Round %d] 수정본이 이전 라운드와 **완전히 동일** (연속 %d회) — "
                "Architect 가 아무것도 바꾸지 않았다. 남은 라운드는 같은 결과일 "
                "가능성이 높다.",
                round_num + 1, streak,
            )
            # **정체 해소 개입.** 감지만 하고 종료를 기다리면 남은 라운드가 전부
            # 같은 결과를 반복한다 — 2026-08-19 실측: 트리플이 3365 로 멈춘 뒤
            # Validator/SME 가 같은 지적을 두 라운드 더 했고 (12→14→12→14, 무수렴),
            # Jury 개입은 **마지막 라운드에서야** 일어나 128건을 한꺼번에 적용했다
            # (94건 반영). 그 개입이 정체 시점에 있었다면 남은 라운드가 바뀐 T-Box
            # 를 리뷰하는 유효한 토론이 된다.
            #
            # Architect 가 못 바꾼 것을 Jury 의 결정적 DSL 로 뚫는 구조다 (Jury 는
            # required_fixes 를 코드가 적용하므로 LLM 이 TTL 을 다시 쓸 필요가 없다).
            _intervene_on_no_progress(
                state, validator_result, sme_result, ontoqa_metrics, cq_runtime,
                round_num, round_log, streak,
            )
            if streak >= _NO_PROGRESS_STREAK_LIMIT:
                summary["break_loop"] = True
                round_log["early_stop"] = {
                    "reason": "no_progress",
                    "streak": streak,
                    "limit": _NO_PROGRESS_STREAK_LIMIT,
                }
                logger.error(
                    "[조기 종료] %d라운드 연속 무변경 — 남은 라운드를 태우지 않고 "
                    "종료한다. 미합의 상태로 사람에게 인계된다.",
                    streak,
                )
    except Exception as e:
        logger.warning(
            "[Round %d] 수정본 구문 오류, 이전 버전 유지: %s", round_num + 1, e,
        )
        round_log["revision"] = {"valid": False, "error": str(e)}

    return summary


#: 저장 직전 붕괴 판정 임계치 — 기존 T-Box 대비 이 비율 미만이면 덮어쓰지 않는다.
#: 0.5 는 "절반 이상 사라졌다" 는 뜻으로, 정상적인 라운드 수정(수백 트리플)과
#: 응답 유실로 인한 붕괴(수천 → 수십)를 명확히 가른다.
_SAVE_COLLAPSE_RATIO = 0.5

#: 선언 손실 판정 임계치 — 이전 T-Box 대비 **표현력** 이 사라진 비율.
#: 트리플 비율만 보는 판정으로는 안 잡히는 손실이 있다 (2026-08-14 실측:
#: S3 산출 5,599 트리플/OP 249 → S2 저장 3,180/115 은 56.8% 라 0.5 문턱을
#: 통과했지만 OP 230개·hasKey 30건·completenessStatus 65건이 사라졌다).
#:
#: **이름 집합이 아니라 관계 커버리지로 재는 이유**: baseline→current 실측에서
#: 이름 소실 OP 44개 중 42개는 같은 ``domain→range`` 를 잇는 OP 가 새 이름으로
#: 남아 있었다 (``isWaterMonitoringPointOf`` 류 리네이밍). 이름으로 재면 정상적인
#: S2 재생성이 44% 손실로 차단되고, 차단되는 게이트는 결국 꺼진다. 관계 단위로
#: 재면 리네이밍은 손실 0, 실제 관계 소멸만 걸린다.
_SAVE_DECL_LOSS_RATIO = 0.2

#: 응답/로그에 실을 소실 항목 표본 상한.
_SAVE_DECL_LOSS_SAMPLE = 40

#: 산 데이터 경로 소실 차단 임계 — 손실률과 **독립** 으로 세는 두 번째 축.
#:
#: **왜 비율 축만으로는 부족한가** (2026-08-25 실측). S2 재실행이 op_links 18개를
#: 잃었는데 ``worst_ratio`` 는 14.8% 로 0.2 문턱을 통과했다. 그 18개 중 7개는
#: A-Box 가 실제로 채우는 관계였고, 그 결과 S9 ``cw_master_orphan`` 이
#: ``ItemSupplierMap`` 98/98 = 100% 고아로 FAIL 했다 (전체 고아율 4.32% → 17.99%).
#:
#: **임계를 낮추는 것은 답이 아니다**: 같은 방식으로 8/19 정상 실행을 재계산하면
#: raw 기준 op_links 손실이 **18.8%** 로 이번(14.8%)보다 **높다**. 0.1 로 낮추면
#: 그 실행도 차단되고, 0.15 로 낮춰도 이번 건은 통과한다 — 비율은 두 경우를
#: 구분하지 못한다. 같은 단계(S3 정규화)로 공정 비교하면 갈리는 것은 비율이 아니라
#: **산 경로 개수** 다: 8/19 정상 1건 vs 이번 7건.
#:
#: 그래서 "A-Box·tacit 이 채우는 관계를 몇 개 잃었나" 를 절대 개수로 센다. 비율이
#: 아닌 이유는 분모(전체 op_links)가 S2 재생성마다 흔들려 같은 손실이 다른 비율로
#: 보이기 때문이다. 1 은 "산 데이터 경로는 한 개도 잃지 말라" 는 뜻이며, 리네이밍은
#: ``op_links`` 가 관계 단위라 애초에 손실로 세지 않는다.
#:
#: phantom(A-Box 0건) 손실은 이 축에서 **제외** 한다 — 그것이 사라지는 것은 개선이다
#: (이번 실행의 11건: ``blastFurnaceFollowedBySteelmaking`` 등).
_SAVE_LIVE_LINK_LOSS_MAX = 1


def _declaration_names(g) -> dict[str, set[str]]:
    """그래프의 도메인 Class/OP/DP local name 집합.

    트리플 수가 아니라 **선언 이름** 을 센다. 관찰/보고용 — 판정에는
    ``_declaration_capabilities`` 의 관계 커버리지를 쓴다 (리네이밍 오탐 방지).
    """
    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF

    def _locals(rdf_type) -> set[str]:
        return {
            str(s).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            for s in g.subjects(_RDF.type, rdf_type)
            if isinstance(s, URIRef) and str(s).startswith(DOMAIN_NS)
        }

    return {
        "classes": _locals(_OWL.Class),
        "object_properties": _locals(_OWL.ObjectProperty),
        "data_properties": _locals(_OWL.DatatypeProperty),
    }


def _declaration_capabilities(g) -> dict[str, set]:
    """그래프가 **표현할 수 있는 것** 의 집합 — 이름이 아니라 능력 단위.

    - ``classes``: 도메인 클래스 local name (클래스는 리네이밍이 곧 계약 파괴라
      이름 그대로 센다 — ``table_class_mapping.json`` 이 권위 매핑이다).
    - ``op_links``: OP 가 잇는 ``(domain, range)`` 쌍. 이름이 바뀌어도 같은 관계를
      잇는 OP 가 있으면 표현력은 유지된 것으로 본다.
    - ``dp_slots``: DP 의 ``(domain, local_name)``. DP 는 CSV 컬럼과 1:1 계약
      (``dcterms:source`` 99.2% 보유) 이므로 이름을 유지해야 A-Box 가 매칭한다.
    """
    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import RDFS as _RDFS

    def _local(node) -> str:
        return str(node).rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    def _dom(p) -> list[str]:
        return [_local(o) for o in g.objects(p, _RDFS.domain)]

    classes = {
        _local(c) for c in g.subjects(_RDF.type, _OWL.Class)
        if isinstance(c, URIRef) and str(c).startswith(DOMAIN_NS)
    }
    op_links: set = set()
    for p in g.subjects(_RDF.type, _OWL.ObjectProperty):
        if not (isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS)):
            continue
        doms = _dom(p) or [None]
        rngs = [_local(o) for o in g.objects(p, _RDFS.range)] or [None]
        for d in doms:
            for r in rngs:
                op_links.add((d, r))
    dp_slots: set = set()
    for p in g.subjects(_RDF.type, _OWL.DatatypeProperty):
        if not (isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS)):
            continue
        for d in (_dom(p) or [None]):
            dp_slots.add((d, _local(p)))
    return {"classes": classes, "op_links": op_links, "dp_slots": dp_slots}


def _live_link_losses(prev, missing_links: set) -> tuple[list[str], bool]:
    """손실된 ``(domain, range)`` 관계 중 **A-Box·tacit 이 실제로 채우는** 것.

    ``op_links`` 손실률은 정상 재생성과 실제 회귀를 구분하지 못한다
    (:data:`_SAVE_LIVE_LINK_LOSS_MAX` 주석의 실측 참조). 그래서 "그 관계를 담당했던
    OP 가 산 데이터를 갖고 있었나" 를 별도 축으로 본다.

    정방향뿐 아니라 ``owl:inverseOf`` 짝의 사용량도 본다. 역방향 트리플은 A-Box
    생성기가 아니라 ``load_graph()`` 의 ``ensure_inverse_triples()`` 가 만들므로
    A-Box 파일에는 정방향만 있다 — 역방향 OP 자신의 사용량은 0 이지만 짝이 데이터를
    가지면 그 관계는 살아 있다 (실측: ``isItemOfSupplierMap`` 0건이지만 짝
    ``itemSupplierMapHasItem`` 98건).

    Args:
        prev: 이전 T-Box 그래프 (손실 판정의 기준 — 여기서 OP 이름을 얻는다).
        missing_links: 사라진 ``(domain, range)`` 쌍 집합.

    Returns:
        ``(산 경로 관계 설명 목록, 신호 판정불가 여부)``. 판정불가면 이 축으로
        차단하지 않는다 — A-Box 가 아직 없는 첫 실행에서 S2 를 영구 차단하면
        부트스트랩이 불가능해진다.
    """
    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import RDFS as _RDFS

    from domain.graph_utils import abox_used_local_names

    if not missing_links:
        return [], False

    def _local(node) -> str:
        return str(node).rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    # 손실 관계별로 그것을 담당했던 OP 이름을 모은다 (짝 포함).
    link_ops: dict[tuple, set[str]] = {}
    for p in prev.subjects(_RDF.type, _OWL.ObjectProperty):
        if not (isinstance(p, URIRef) and str(p).startswith(DOMAIN_NS)):
            continue
        doms = [_local(o) for o in prev.objects(p, _RDFS.domain)] or [None]
        rngs = [_local(o) for o in prev.objects(p, _RDFS.range)] or [None]
        names = {_local(p)}
        for inv in prev.objects(p, _OWL.inverseOf):
            names.add(_local(inv))
        for inv in prev.subjects(_OWL.inverseOf, p):
            names.add(_local(inv))
        for d in doms:
            for r in rngs:
                if (d, r) in missing_links:
                    link_ops.setdefault((d, r), set()).update(names)

    if not link_ops:
        return [], False

    all_names: set[str] = set()
    for names in link_ops.values():
        all_names.update(names)
    used = abox_used_local_names(all_names)
    if used is None:
        # 판정 불가 — A-Box 없음/네임스페이스 미설정. 파괴적 동작(차단) 보류.
        return [], True

    live: list[str] = []
    for (d, r), names in sorted(link_ops.items(), key=lambda kv: str(kv[0])):
        hit = sorted(names & used)
        if hit:
            live.append(f"{d}→{r} (사용 중: {', '.join(hit[:3])})")
    return live, False


def _preserve_rejected_draft(ttl: str, guard: dict) -> None:
    """차단된 초안을 디스크에 남기고 경로를 ``guard`` 에 기록한다.

    두 차단 분기는 ``final_ttl`` 을 버리고 기존 파일 내용을 반환하므로, 이 함수가
    없으면 **48분치 산출물이 함수 지역 변수째로 사라진다**. 응답의 ``statistics``
    조차 차단 후 재파싱한 기존 파일 값이라(``_finalize_and_save``), 새 초안이
    클래스 몇 개였는지를 응답만 보고 알 수 없었다 (2026-08-17 실측).

    실패해도 저장 경로를 막지 않는다 — 보존은 부가 기능이다.
    """
    try:
        import datetime as _dt

        stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
        path = os.path.join(
            os.path.dirname(TBOX_PATH) or ".", f"t_box_rejected_{stamp}.ttl"
        )
        atomic_write(path, ttl)
        guard["rejected_draft_path"] = path
        guard["rejected_draft_chars"] = len(ttl)
        logger.warning("차단된 초안을 보존했다 → %s (%d chars)", path, len(ttl))
    except Exception as exc:  # noqa: BLE001 — 보존 실패가 저장 판정을 막지 않는다
        logger.warning("차단된 초안 보존 실패 — %s", exc)


def _normalize_with_s3(ttl: str) -> tuple[str, dict]:
    """초안을 S3 후처리에 통과시킨 결과를 돌려준다 (파일에 쓰지 않는다).

    손실 판정을 **같은 파이프라인 단계끼리** 하기 위한 정규화다. ``TBOX_PATH`` 에는
    S3 후처리 완료본이 들어 있는데(초안은 더 이상 그 파일을 덮어쓰지 않는다),
    비교 대상인 새 TTL 은 S2 초안이라 S3 산물이 아직 없다.

    ⚠️ 순수 함수 ``improve_tbox`` 를 쓴다. ``improve_tbox_quality`` 는
    ``atomic_write(TBOX_PATH, ...)`` 로 **파일을 덮어쓰므로**
    (tools/ontology_quality.py:3627) 차단 판정 중에 호출하면 가드가 보호하려는
    T-Box 를 가드가 망가뜨린다.
    """
    from tools.ontology_quality import improve_tbox

    return improve_tbox(ttl)


def _guard_before_save(final_ttl: str) -> tuple[str, dict]:
    """저장 직전 최소 안전 검사 — 유령 IRI 복구 + 붕괴 시 저장 거부.

    S2 는 지금까지 **자기가 만든 TTL 을 검증하지 않고** 저장했다. 그래서 두 종류의
    사고가 조용히 파일에 반영됐다 (2026-08-09 실측):

    1. **유령 IRI**: DSL 적용 경로가 ``http://…#steel:Foo`` 를 만들어 한 실행에서
       183건이 저장됐다 (ObjectProperty 229개 중 101개 사용 불가). 같은 복구를
       ``jury_fixes._apply_fix_namespace_bulk`` 가 하지만 **S3 에서만** 돌았다.
       근본 원인은 ``_LocalNameNamespace`` 로 막았고, 여기서는 남은/외부 유입분에
       대한 최종 그물이다.
    2. **붕괴 저장**: 응답 유실로 TTL 이 사실상 비어도 그대로 덮어썼다. 기존
       T-Box 가 있는데 트리플이 절반 미만으로 줄면 **저장하지 않는다** — 50분치
       작업물보다 기존 산출물이 낫다.

    Returns:
        ``(저장할 TTL, 판정 dict)``. 판정은 응답에 실려 운영자가 볼 수 있다.
    """
    guard: dict = {"phantom_iris_repaired": 0, "collapse_blocked": False}
    try:
        from rdflib import Graph as _RG
        from rdflib import URIRef as _URI
        g = _RG()
        g.parse(data=final_ttl, format="turtle")
    except Exception as exc:  # noqa: BLE001 — 파싱 불가면 판정 불가, 기존 동작 유지
        guard["parse_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.error("저장 전 검증: TTL 파싱 실패 — 검사 없이 저장 (%s)", exc)
        return final_ttl, guard

    # 유령 판정은 공용 헬퍼가 한다. 예전엔 marker = f"{NS_PREFIX}:" 의 **부분
    # 문자열** 검사였고 두 방향으로 틀렸다 (2026-08-09 실측):
    #   - 놓침: `<…#iof-core:MaterialArtifact>` 는 도메인 prefix 를 포함하지 않아
    #     탐지 0건 → 외래 매핑 유령이 그대로 저장됐다.
    #   - 오탐: prefix 가 'core' 인 도메인에서 `iof-core:` 가 부분일치해
    #     **정상 외래 유령을** 도메인 유령으로 오분류했다.
    from domain.graph_utils import split_embedded_prefix

    phantom = {
        s for s in set(g.subjects()) | set(g.objects())
        if isinstance(s, _URI) and split_embedded_prefix(str(s), g) is not None
    }
    if phantom:
        try:
            from tools.jury_fixes import _apply_fix_namespace_bulk
            ok, msg = _apply_fix_namespace_bulk(g, {})
            guard["phantom_repair_msg"] = msg[:160]
            if ok:
                final_ttl = g.serialize(format="turtle")
                # 복구 **후** 실측으로 센다. 예전엔 탐지 수(len(phantom))를 그대로
                # 기록해, 복구가 0건이어도 "N건 복구" 라고 보고했다 — 운영자가
                # 불완전한 복구를 정상으로 읽는 경로였다.
                remaining = {
                    s for s in set(g.subjects()) | set(g.objects())
                    if isinstance(s, _URI)
                    and split_embedded_prefix(str(s), g) is not None
                }
                guard["phantom_iris_repaired"] = len(phantom) - len(remaining)
                guard["phantom_iris_remaining"] = len(remaining)
                logger.warning(
                    "저장 전 검증: 유령 IRI %d건 탐지 → %d건 복구, %d건 잔존 (%s)",
                    len(phantom), guard["phantom_iris_repaired"],
                    len(remaining), msg[:120],
                )
            else:
                guard["phantom_iris_repaired"] = 0
                guard["phantom_iris_remaining"] = len(phantom)
                logger.error(
                    "저장 전 검증: 유령 IRI %d건 탐지했으나 **복구 실패** (%s)",
                    len(phantom), msg[:120],
                )
        except Exception as exc:  # noqa: BLE001 — 복구 실패해도 저장은 진행
            guard["phantom_iris_remaining"] = len(phantom)
            logger.warning("저장 전 검증: 유령 IRI 복구 실패 — %s", exc)

    # 붕괴 판정: 기존 T-Box 가 있고 새 TTL 이 절반 미만이면 덮어쓰지 않는다.
    # + 선언 집합 손실 판정 (트리플 비율이 통과시키는 손실을 잡는다).
    try:
        if os.path.exists(TBOX_PATH):
            prev = _RG()
            prev.parse(TBOX_PATH, format="turtle")
            new_g = _RG()
            new_g.parse(data=final_ttl, format="turtle")
            guard["triples_prev"], guard["triples_new"] = len(prev), len(new_g)
            if len(prev) > 0 and len(new_g) < len(prev) * _SAVE_COLLAPSE_RATIO:
                guard["collapse_blocked"] = True
                _preserve_rejected_draft(final_ttl, guard)
                logger.error(
                    "저장 전 검증: 트리플 %d → %d (기준 %.0f%% 미만) — **저장 거부**. "
                    "기존 T-Box 를 보존한다.",
                    len(prev), len(new_g), _SAVE_COLLAPSE_RATIO * 100,
                )
                with open(TBOX_PATH, encoding="utf-8") as fh:
                    return fh.read(), guard

            # 표현력 손실: 트리플 비율은 통과해도 관계/속성이 사라질 수 있다.
            #
            # **비교 축 정규화** (2026-08-18): ``TBOX_PATH`` 에는 S3 후처리 완료본이
            # 들어 있고 새 TTL 은 S2 초안이다 — 서로 다른 파이프라인 단계다. 정규화
            # 없이 재면 S3 가 만드는 것을 초안에 요구한다. 실측 (2026-08-17 48분
            # 실행): 클래스 66→47 (-30.3%) 로 차단됐으나, 손실로 지목된 20개를 현행
            # T-Box 에서 지우고 S3 만 돌리면 **17개가 결정적으로 복원된다**
            # (MasterData / EquipmentManagement / SupplyChainMaster 등 중간 추상
            # 계층은 step_12/step_14 소관). 즉 30.3% 는 측정 오차이고, 그 오차가
            # 48분 산출물을 흔적 없이 폐기했다.
            #
            # 이름 목록을 하드코딩하지 않는 이유: design_patterns.json 만 보면
            # 15/20 만 잡히고(실측) MaintenanceManagement 등은 다른 경로가 만든다 —
            # 경로가 늘 때마다 목록이 낡는다. 파이프라인을 실제로 돌려 비교한다.
            #
            # 저장하는 것은 **초안 원본** 이다 (S3 는 S3 단계에서 다시 돈다).
            # 실패·off 시 폴백은 raw 비교(기존 동작)이며 guard 에 기록된다.
            # 정규화는 **면제 집합** 을 구하는 데만 쓴다. 분모는 raw prev 로 고정한다.
            #
            # 양쪽을 정규화해 그대로 비교하면 게이트가 둔감해진다 — S3 의 step_15c 가
            # 실제 A-Box 를 보고 OP 를 대량 주입하므로 분모가 부푼다. 실측: 픽스처
            # prev 의 op_links 8 → 82 (59개 주입), 그 결과 관계 6개 소실이 75% →
            # 8.8% 로 희석돼 정당한 차단이 풀렸다 (기존 테스트
            # ``test_blocks_relation_loss_that_ratio_guard_misses`` 가 red).
            #
            # 그래서 분자만 면제한다: raw 기준 손실 중 "초안을 S3 에 통과시키면
            # 되살아나는 것" 을 뺀다. 이러면
            #   - S3 소관 항목(중간 추상 계층 등)은 손실로 세지 않고 (실측 27.0% → 0%)
            #   - 실제 관계 소멸은 분모가 그대로라 여전히 잡힌다 (6/8 = 75%)
            #
            # ``restored_by_s3`` 로 무엇이 면제됐는지 응답에 남긴다.
            prev_cap = _declaration_capabilities(prev)
            new_cap = _declaration_capabilities(new_g)
            exempt: dict[str, set] = {axis: set() for axis in prev_cap}
            guard["compared_against"] = "raw_draft"
            if os.getenv("S2_GUARD_NORMALIZE_WITH_S3", "on").lower() in ("1", "on", "true"):
                try:
                    norm_ttl, _ = _normalize_with_s3(final_ttl)
                    norm_g = _RG()
                    norm_g.parse(data=norm_ttl, format="turtle")
                    norm_cap = _declaration_capabilities(norm_g)
                    for axis, prev_items in prev_cap.items():
                        # raw 에서는 없지만 S3 통과 후 되살아나는 것 = S3 소관
                        exempt[axis] = (prev_items - new_cap[axis]) & norm_cap[axis]
                    guard["compared_against"] = "s3_normalized"
                    guard["triples_normalized"] = len(norm_g)
                    guard["restored_by_s3"] = {
                        axis: len(items) for axis, items in exempt.items()
                    }
                except Exception as exc:  # noqa: BLE001 — 정규화 실패는 raw 비교로 폴백
                    exempt = {axis: set() for axis in prev_cap}
                    guard["compared_against"] = "raw_draft"
                    logger.warning(
                        "저장 전 검증: S3 정규화 실패 — raw 초안으로 비교한다 (%s)", exc,
                    )
            loss_detail: dict[str, dict] = {}
            lost_sample: dict[str, list[str]] = {}
            worst_ratio = 0.0
            for axis, prev_items in prev_cap.items():
                missing = (prev_items - new_cap[axis]) - exempt[axis]
                # 분모는 raw prev — 정규화가 분모를 부풀려 게이트를 무력화하지 않게.
                ratio = len(missing) / len(prev_items) if prev_items else 0.0
                loss_detail[axis] = {
                    "prev": len(prev_items),
                    "new": len(new_cap[axis]),
                    "lost": len(missing),
                    "lost_pct": round(ratio * 100, 1),
                }
                if missing:
                    lost_sample[axis] = [
                        str(x) for x in sorted(missing, key=str)[:_SAVE_DECL_LOSS_SAMPLE]
                    ]
                worst_ratio = max(worst_ratio, ratio)
            # 관찰용 이름 집합 diff 도 함께 남긴다 (리네이밍 추적).
            guard["capability_loss"] = loss_detail
            prev_names = _declaration_names(prev)
            new_names = _declaration_names(new_g)
            guard["renamed_or_removed"] = {
                axis: len(prev_names[axis] - new_names[axis])
                for axis in prev_names
            }
            if lost_sample:
                guard["capability_lost_sample"] = lost_sample
                logger.warning(
                    "저장 전 검증: 표현력 소실 — class %d / OP관계 %d / DP슬롯 %d "
                    "(최대 손실률 %.1f%%)",
                    loss_detail["classes"]["lost"],
                    loss_detail["op_links"]["lost"],
                    loss_detail["dp_slots"]["lost"],
                    worst_ratio * 100,
                )
                for axis, names in lost_sample.items():
                    logger.warning("  소실 %s: %s", axis, ", ".join(names))
            # 두 번째 축 — 손실률과 **독립**. 비율이 문턱 아래여도 A-Box 가 채우는
            # 관계를 잃었으면 차단한다 (2026-08-25 실측: 14.8% 통과 → S9 FAIL).
            missing_links = (
                prev_cap["op_links"] - new_cap["op_links"]
            ) - exempt["op_links"]
            live_lost, live_signal_unavailable = _live_link_losses(prev, missing_links)
            guard["live_link_loss"] = {
                "lost": len(live_lost),
                "max_allowed": _SAVE_LIVE_LINK_LOSS_MAX,
                "signal_unavailable": live_signal_unavailable,
            }
            if live_lost:
                guard["live_link_lost_sample"] = live_lost[:_SAVE_DECL_LOSS_SAMPLE]
                logger.warning(
                    "저장 전 검증: A-Box 가 채우는 관계 %d개 소실 — %s",
                    len(live_lost), "; ".join(live_lost[:5]),
                )
            live_blocked = len(live_lost) >= _SAVE_LIVE_LINK_LOSS_MAX
            if worst_ratio >= _SAVE_DECL_LOSS_RATIO or live_blocked:
                guard["declaration_loss_blocked"] = True
                guard["collapse_blocked"] = True
                guard["block_reason"] = (
                    "live_link_loss" if live_blocked else "loss_ratio"
                )
                _preserve_rejected_draft(final_ttl, guard)
                if live_blocked:
                    logger.error(
                        "저장 전 검증: A-Box 가 채우는 관계 %d개 소실 >= 기준 %d "
                        "(손실률 %.1f%% 는 문턱 %.0f%% 미달이라 비율 축은 통과했다, "
                        "비교=%s) — **저장 거부**. 산 데이터 경로를 잃는 초안은 "
                        "받지 않는다. 목록은 save_guard.live_link_lost_sample, "
                        "차단된 초안은 save_guard.rejected_draft_path 참조. "
                        "도메인 지식으로 필요한 관계면 "
                        "rules/domain/tbox_manual_additions.ttl 에 명시하라.",
                        len(live_lost), _SAVE_LIVE_LINK_LOSS_MAX,
                        worst_ratio * 100, _SAVE_DECL_LOSS_RATIO * 100,
                        guard.get("compared_against"),
                    )
                else:
                    logger.error(
                        "저장 전 검증: 표현력 손실률 %.1f%% >= 기준 %.0f%% (비교=%s) — "
                        "**저장 거부**. 기존 T-Box 를 보존한다. 소실 목록은 "
                        "save_guard.capability_lost_sample, 차단된 초안은 "
                        "save_guard.rejected_draft_path 참조.",
                        worst_ratio * 100, _SAVE_DECL_LOSS_RATIO * 100,
                        guard.get("compared_against"),
                    )
                with open(TBOX_PATH, encoding="utf-8") as fh:
                    return fh.read(), guard
    except Exception as exc:  # noqa: BLE001
        # **검사기 고장은 "이상 없음" 이 아니다.** 이 except 는 손실 판정 전체
        # (선언 능력 비교 · S3 정규화 면제 · live_link 축, 약 160줄) 를 감싼다.
        # 예전에는 logger.debug 한 줄만 남기고 초안을 그대로 반환했다 — 서버
        # 로그 레벨이 INFO 이므로(server.py:70) 그 사실이 **어디에도 남지 않았다**.
        #
        # fault injection 실측 (2026-08-26): 같은 초안에 대해 baseline 은
        # `blocked=True / block_reason=live_link_loss` (산 경로 19개 소실) 인데,
        # `_declaration_capabilities` 에 예외 하나를 주입하면 `blocked=False` 로
        # 뒤집혀 초안이 저장됐다. 즉 검사기 버그 한 개가 가드 전체를 무력화한다.
        #
        # 그래서 fail-CLOSED 로 바꾼다: 판정할 수 없으면 저장하지 않는다.
        #
        # 부트스트랩(기존 T-Box 없음)은 여기서 다시 확인하지 않는다 — ``try`` 의
        # 첫 줄이 ``if os.path.exists(TBOX_PATH)`` 이므로 파일이 없으면 검사 자체를
        # 건너뛰고 이 except 에 **도달하지 않는다**. 여기에 존재 검사를 한 번 더
        # 두면 죽은 분기가 되고, 실제로 그렇게 썼다가 뮤테이션이 생존했다(어느
        # 방향으로 바꿔도 산출물이 같았다).
        guard["check_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        guard["collapse_blocked"] = True
        guard["block_reason"] = "check_error"
        logger.error(
            "저장 전 검증: 손실 판정이 예외로 실패했다 — **저장 거부**하고 기존 "
            "T-Box 를 보존한다 (%s). 검사기를 고친 뒤 재실행하라. 초안은 "
            "save_guard.rejected_draft_path 로 보존된다.", exc,
        )
        with contextlib.suppress(Exception):
            _preserve_rejected_draft(final_ttl, guard)
        try:
            with open(TBOX_PATH, encoding="utf-8") as fh:
                return fh.read(), guard
        except OSError as read_exc:
            # 보존할 대상을 읽지 못하면 차단해도 남는 것이 없다 — 초안이라도 살린다.
            guard["collapse_blocked"] = False
            guard["block_reason"] = None
            guard["preserve_failed"] = str(read_exc)[:120]
            logger.error(
                "저장 전 검증: 기존 T-Box 를 읽지 못해 보존이 불가능하다 — "
                "초안을 저장한다 (%s)", read_exc,
            )
    return final_ttl, guard


def _record_s2_quality_history(
    state: dict, statistics: dict, duration: float, debate_record: dict,
) -> None:
    """S2 결과를 ``quality_history.json`` 에 남긴다 (예전엔 0건이었다).

    ## 왜 필요한가

    실측 (2026-08-22 파이프라인 한 바퀴): ``quality_history.json`` 의 step 분포는
    ``S3_IMPROVE / S4_VALIDATE / S4_5_MUTATION / S9_POST_MEASURE / S12_QUERY_TEST``
    였고 **S2 는 0건** 이었다. 48분(2,883.7초)이 걸린 단계가 추세 추적에서 통째로
    빠져 있어 ``get_pipeline_quality_history`` 의 회귀 탐지가 S2 에는 발화할 수
    없었다. ``_compute_round_quality_metrics`` 가 라운드마다 FAIR/Färber 를 계산해
    ``round_log["quality_metrics"]`` 에 넣지만 그 값이 ``_append_quality_history``
    로 넘어가는 경로가 없었다.

    ## 최종 라운드 값을 쓰는 이유

    평균이 아니다. 평균은 나쁜 초기 라운드로 **최종 산출물** 점수를 희석해 실제
    T-Box 품질을 왜곡한다 (실측 예: R2 fair 70.0 / R3 72.9 → 평균 71.45 는 어느
    산출물의 점수도 아니다). 기록 대상은 "저장된 T-Box" 이므로 마지막 값이 맞다.

    ## 측정 실패를 0 으로 채우지 않는다

    FAIR/Färber 계산이 실패한 라운드는 ``{"error": ...}`` 를 담는다. 그것을 0 으로
    기록하면 회귀 탐지가 "점수 폭락" 이라는 **거짓 경보** 를 낸다. 키를 아예
    생략해 "측정 안 됨" 과 "0점" 을 구분한다 (이 리포의 "0건과 판정불가를 구분"
    원칙과 같다).

    fail-open — 기록 실패가 S2 산출물 반환을 막지 않는다 (T-Box 저장이 우선).
    """
    try:
        metrics: dict = dict(statistics or {})
        rounds = (state or {}).get("debate_log") or []
        metrics["debate_rounds"] = len(rounds)
        metrics["consensus_reached"] = bool((state or {}).get("consensus_reached"))
        metrics["veto_lock_triggered"] = bool(
            (state or {}).get("veto_lock_triggered"),
        )
        persistent = (debate_record or {}).get("persistent_issues")
        if isinstance(persistent, int):
            metrics["persistent_issues"] = persistent

        # 최종 라운드의 측정값만 채택. 뒤에서부터 훑어 첫 유효 값을 쓴다 —
        # 마지막 라운드에서 측정이 실패했어도 그 앞 라운드 값이 남아 있다.
        for entry in reversed(rounds):
            qm = entry.get("quality_metrics") if isinstance(entry, dict) else None
            if not isinstance(qm, dict) or qm.get("error"):
                continue
            fair = (qm.get("fair") or {}).get("overall")
            farber = (qm.get("farber") or {}).get("overall")
            if isinstance(fair, int | float):
                metrics["fair_overall"] = fair
            if isinstance(farber, int | float):
                metrics["farber_overall"] = farber
            if "fair_overall" in metrics or "farber_overall" in metrics:
                break

        from tools.pipeline_state import _append_quality_history

        _append_quality_history("S2_TBOX", metrics, duration)
        logger.info(
            "S2 품질 이력 기록: rounds=%d consensus=%s fair=%s farber=%s",
            metrics["debate_rounds"], metrics["consensus_reached"],
            metrics.get("fair_overall"), metrics.get("farber_overall"),
        )
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 저장을 막지 않는다
        logger.warning("S2 품질 이력 기록 실패 (무시하고 진행): %s", exc)


def _snapshot_s2_output(final_ttl: str) -> str | None:
    """S2 산출물을 ``t_box_s2out_<ts>.ttl`` 로 따로 남긴다 (덮어쓰지 않는 사본).

    ## 왜 필요한가 (2026-09-03 실측)

    S3 는 ``TBOX_PATH`` 를 **제자리에서** 덮어쓴다. 그래서 S3 이후에 "S3 를 다시
    돌려야 하는" 상황이 오면 입력이 이미 사라져 있다. 그날 실제로 그 일이 겪혔다:
    S3 스텝 두 개를 고친 뒤 재적용해야 했는데 S2 출력이 없어서 S3 산출물에 S3 를
    한 번 더 돌렸고, ``step_13`` 의 비멱등성이 발화해 근거 없는 존재 공리가 생겨
    추론에서 위반 34,562건이 나왔다.

    S2 는 15~40분 / Bedrock 8~12회다 — 그 산출물을 잃으면 재현 비용이 그대로 든다.
    스냅샷은 250KB 수준이라 보관 비용이 무시할 만하다. 오래된 것을 **지우지 않는다**
    (파이프라인 스텝이 조용히 파일을 삭제하지 않는다는 이 리포의 관행).

    스냅샷 실패가 S2 저장을 되돌리게 해서는 안 되므로 예외를 삼킨다 (품질 이력
    기록과 같은 정책).

    Returns:
        저장 경로, 실패하면 ``None``.
    """
    try:
        ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        path = os.path.join(os.path.dirname(TBOX_PATH), f"t_box_s2out_{ts}.ttl")
        atomic_write(path, final_ttl)
        logger.info("S2 산출물 스냅샷: %s", path)
        return path
    except Exception as exc:  # noqa: BLE001 — 스냅샷 실패가 저장을 막지 않는다
        logger.warning("S2 산출물 스냅샷 실패 (무시하고 진행): %s", exc)
        return None


def _finalize_and_save(state: dict, start_monotonic: float) -> str:
    """합의 상태 annotate + TTL 저장 + 최종 통계 계산 → JSON 문자열 반환."""
    current_ttl = state["current_ttl"]
    infra_abort = state.get("infra_abort")
    final_ttl = _annotate_consensus_status(
        current_ttl,
        consensus_reached=state["consensus_reached"],
        veto_lock_triggered=state["veto_lock_triggered"],
        veto_targets=state["veto_persistent_targets"],
        total_rounds=len(state["debate_log"]) + 1,
        infra_abort=infra_abort,
        early_stop_reason=state.get("early_stop_reason"),
    )
    final_ttl, save_guard = _guard_before_save(final_ttl)
    state["save_guard"] = save_guard
    atomic_write(TBOX_PATH, final_ttl)
    state["s2_snapshot"] = _snapshot_s2_output(final_ttl)

    if not os.path.exists(TBOX_BASELINE_PATH):
        atomic_write(TBOX_BASELINE_PATH, final_ttl)
        logger.info("T-Box 베이스라인 저장: %s", TBOX_BASELINE_PATH)

    from rdflib import OWL as _OWL
    from rdflib import RDF as _RDF
    from rdflib import Graph as _RG
    final_g = _RG()
    final_g.parse(data=final_ttl, format="turtle")
    steel_str = DOMAIN_NS
    final_classes = len([c for c in final_g.subjects(_RDF.type, _OWL.Class)
                         if str(c).startswith(steel_str)])
    final_ops = len([p for p in final_g.subjects(_RDF.type, _OWL.ObjectProperty)
                     if str(p).startswith(steel_str)])
    final_dps = len([p for p in final_g.subjects(_RDF.type, _OWL.DatatypeProperty)
                     if str(p).startswith(steel_str)])

    debate_rounds = len(state["debate_log"])
    duration = round(time.monotonic() - start_monotonic, 1)

    statistics = {
        "classes": final_classes,
        "object_properties": final_ops,
        "data_properties": final_dps,
        "triples": len(final_g),
    }

    # **토론 기록을 디스크에 남긴다.** 예전에는 이 아래 응답 JSON 에만 실려
    # 인메모리 JobRegistry(max_finished=8) 가 evict 하면 5라운드 59분치 궤적이
    # 소멸했다. 라운드별 이슈 본문·veto·jury 실패 사유가 전부 사라져 "같은 지적이
    # 몇 라운드 반복됐나" 를 서버 로그 grep 없이는 답할 수 없었다.
    # fail-open — 기록 실패가 T-Box 저장을 막지 않는다.
    from tools.debate_log_store import append_debate_run

    debate_record = append_debate_run(
        rounds=state["debate_log"],
        consensus_reached=state["consensus_reached"],
        veto_lock_triggered=state["veto_lock_triggered"],
        veto_targets=state["veto_persistent_targets"],
        statistics=statistics,
        architect_initial=state["architect_stats"],
        duration_seconds=duration,
        infra_abort=infra_abort,
        save_guard=save_guard,
    )
    # 추세 추적·회귀 탐지를 위해 quality_history 에도 남긴다 — 보고서가
    # debate_log.json 을 직접 읽어도 이 경로는 우회되지 않는다 (실측: S2 항목 0건).
    _record_s2_quality_history(state, statistics, duration, debate_record)

    return json.dumps({
        "success": True,
        # 인프라 장애로 중단됐다면 산출물은 저장됐지만 **예정된 라운드를 다 돌지
        # 못했다**. 운영자가 clean 산출물과 구분할 수 있어야 하고, 저장 가드 판정도
        # 응답에 실려야 한다 (예전엔 state 에만 담겨 아무도 못 봤다).
        "partial": bool(infra_abort),
        "infra_abort": infra_abort,
        "save_guard": save_guard,
        "saved_to": TBOX_PATH,
        # S3 는 TBOX_PATH 를 제자리에서 덮어쓴다 — 이 사본이 S2 출력을 되찾는
        # 유일한 경로다 (없어서 S3 를 두 번 돌린 사고: _snapshot_s2_output docstring).
        "s2_snapshot": state.get("s2_snapshot"),
        # 토론 기록 저장 결과 — 어디를 보면 라운드 궤적이 있는지 알려준다.
        "debate_log_saved": debate_record,
        "statistics": statistics,
        "architect_initial": state["architect_stats"],
        "debate": {
            # Round counting clarified:
            # - initial_draft_rounds: Architect 초안 (항상 1)
            # - debate_rounds: 실제 실행된 토론 라운드 수
            # - total_rounds: 초안 + 토론 (기존 호환용)
            "initial_draft_rounds": state["initial_draft_rounds"],
            "debate_rounds": debate_rounds,
            "total_rounds": debate_rounds + state["initial_draft_rounds"],
            "consensus_reached": state["consensus_reached"],
            "compromise": state["compromise_reason"],
            "min_rounds_enforced": _MIN_DEBATE_ROUNDS,
            "veto_lock_triggered": state["veto_lock_triggered"],
            "veto_persistent_targets": state["veto_persistent_targets"][:20],
            # 조기 종료했다면 **왜** 인지 — "라운드를 아꼈다" 와 "고칠 수 없어
            # 멈췄다" 는 다른 이야기다. 남은 이슈가 SME 소유/표현불가라는 뜻이므로
            # 운영자는 config 를 보거나 엔진에 핸들러를 추가해야 한다.
            "early_stop_reason": state.get("early_stop_reason"),
            "rounds_saved_by_early_stop": (
                max(0, state.get("max_rounds_planned", 0) - debate_rounds)
                if state.get("early_stop_reason") else 0
            ),
            "rounds": state["debate_log"],
        },
        "competency_questions_used": len(state["cqs"]),
        "duration_seconds": duration,
    }, ensure_ascii=False, indent=2)


# Progress callback type: async function taking (progress, total, message).
# Sync caller passes None; MCP async wrapper passes ctx.report_progress.
_ProgressCB = Callable[[float, float, str], Awaitable[None]]


async def _report(progress_cb: _ProgressCB | None,
                  progress: float, total: float, message: str) -> None:
    """Progress 콜백 호출 헬퍼. None이거나 실패해도 본 작업을 중단하지 않는다."""
    if progress_cb is None:
        return
    try:
        await progress_cb(progress, total, message)
    except Exception as e:
        logger.debug("progress callback 실패 (무시): %s", e)


def _heartbeat_interval() -> float:
    """라운드 내부 하트비트 간격(초). 0 이하면 비활성."""
    raw = os.getenv("MULTI_AGENT_HEARTBEAT_SEC", "10")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 10.0


# ── 파일 기반 하트비트 ─────────────────────────────────────────────────────
# Claude Code MCP 클라이언트는 progressToken 을 전송하지 않아 ctx.report_progress
# 가 서버에서 조용히 드롭된다(FastMCP server.py:1172 참조). 클라이언트 변경 없이
# 진행 상황을 관측할 수 있도록, 동일한 phase/elapsed 정보를 JSON 파일로도 기록한다.
# 사용자는 다른 터미널에서 `watch -n 2 cat data/generated/_heartbeat.json` 으로 확인.
_DEFAULT_HEARTBEAT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "generated", "_heartbeat.json",
)


def _heartbeat_file_path() -> str | None:
    """하트비트 파일 경로. 환경변수 MULTI_AGENT_HEARTBEAT_FILE 로 오버라이드.

    빈 문자열이면 비활성. 미설정 시 data/generated/_heartbeat.json.
    """
    raw = os.getenv("MULTI_AGENT_HEARTBEAT_FILE", None)
    if raw is None:
        return _DEFAULT_HEARTBEAT_FILE
    if raw.strip() == "":
        return None
    return raw


def _write_heartbeat_file(path: str | None, payload: dict) -> None:
    """하트비트 JSON 을 atomic 하게 파일에 기록한다. 실패해도 본작업 중단 없음.

    atomic: 같은 디렉토리에 tmp 파일을 쓰고 os.replace 로 교체 — 관측 측에서
    partial-write 를 읽지 않도록 보장.
    """
    if not path:
        return
    try:
        dirpath = os.path.dirname(path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("heartbeat 파일 기록 실패 (무시): %s", e)


class _FileHeartbeat:
    """S2 실행 동안 공유되는 하트비트 파일 writer.

    이벤트 기반 기록(시작/종료/phase 전환)과 주기적 기록(10초)을 한 곳에서 관리.
    start_monotonic 을 공유해 elapsed_total 을 단조 증가시킨다.
    """

    def __init__(self, path: str | None, max_rounds: int) -> None:
        self.path = path
        self.max_rounds = max_rounds
        self.t0 = time.monotonic()
        self.state: dict = {
            "status": "starting",
            "phase": "init",
            "round": 0,
            "max_rounds": max_rounds,
            "step": "준비",
            "started_at": _isoformat_now(),
            "elapsed_total_sec": 0.0,
            "elapsed_phase_sec": 0.0,
            "pid": os.getpid(),
        }
        self._phase_t0 = self.t0
        self.flush()

    def flush(self) -> None:
        """현재 state 스냅샷을 파일에 기록 + 로거에 1-line 요약 append.

        JSON 파일은 atomic replace 로 덮어쓰기 때문에 `tail -f` 가 inode 를
        따라가지 못해 사용자가 아무것도 못 본다. 같은 내용을 append-only 인
        서버 로그 (기본 /tmp/ontology-agent-server.log) 에도 info 레벨로 찍어
        `tail -f /tmp/ontology-agent-server.log` 로 실시간 관측 가능하게 한다.
        """
        now = time.monotonic()
        self.state["elapsed_total_sec"] = round(now - self.t0, 1)
        self.state["elapsed_phase_sec"] = round(now - self._phase_t0, 1)
        self.state["updated_at"] = _isoformat_now()
        _write_heartbeat_file(self.path, dict(self.state))
        # 로그 1-line 요약: phase + round + elapsed + step + (있으면) chunk 세부.
        try:
            parts = [
                f"status={self.state.get('status')}",
                f"phase={self.state.get('phase')}",
                f"round={self.state.get('round')}/{self.state.get('max_rounds')}",
                f"elapsed={self.state.get('elapsed_total_sec')}s",
            ]
            step = self.state.get("step")
            if step:
                parts.append(f"step={step}")
            cp = self.state.get("chunk_progress")
            if cp:
                parts.append(
                    f"chunk={cp.get('chunk_idx')}/{cp.get('total_chunks')}"
                    f" depth={cp.get('depth')} event={cp.get('event')}"
                    f" stop={cp.get('stop_reason')}"
                )
            logger.info("[S2 heartbeat] %s", " | ".join(parts))
        except Exception as _he:
            logger.debug("heartbeat 로그 포맷 실패 (무시): %s", _he)

    def set(self, **kwargs) -> None:
        """state 필드 갱신 후 flush. phase 가 바뀌면 phase 타이머 reset."""
        if "phase" in kwargs and kwargs["phase"] != self.state.get("phase"):
            self._phase_t0 = time.monotonic()
        self.state.update(kwargs)
        self.flush()

    def error(self, message: str) -> None:
        """에러 상태 기록 — 다음 flush 이전에 즉시 파일 반영."""
        self.state["status"] = "error"
        self.state["error"] = message
        self.flush()

    def finish(self, status: str = "completed") -> None:
        self.state["status"] = status
        self.state["phase"] = "done"
        self.state["step"] = "완료"
        self.flush()


def _isoformat_now() -> str:
    from datetime import datetime
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


async def _heartbeat_loop(
    progress_cb: _ProgressCB | None,
    step: float, total_steps: float,
    label_fn: Callable[[], str],
    interval: float,
    file_hb: _FileHeartbeat | None = None,
) -> None:
    """Worker thread 가 블로킹 중일 때 주기적으로 경과/phase 를 브로드캐스트.

    두 채널로 기록:
    1. MCP progress_cb — Claude Code UI (현재는 progressToken 부재로 silent drop).
    2. 파일 하트비트 — 항상 동작하는 대안 관측 채널.

    MCP 스펙: progress 값은 notification 마다 **반드시 증가**해야 한다. 동일 값을
    보내면 클라이언트가 중복으로 간주해 드롭한다. 따라서 하트비트는 step 이 아닌
    step + fractional (0.01 간격) 로 송신해 다음 step 을 침범하지 않으면서
    단조 증가를 보장한다. 최대 0.99 로 clamp.

    취소(asyncio.CancelledError)되면 조용히 종료. 본작업이 정상 완료되거나 에러
    를 내면 호출자가 이 태스크를 cancel 하므로 무한 루프를 걱정할 필요는 없다.
    """
    if interval <= 0:
        return
    t0 = time.monotonic()
    count = 0
    try:
        while True:
            await asyncio.sleep(interval)
            count += 1
            elapsed = round(time.monotonic() - t0, 1)
            if file_hb is not None:
                # 파일 쪽은 elapsed 자동 갱신 — 추가 필드 없이 flush 만.
                file_hb.flush()
            if progress_cb is not None:
                sub = min(0.99, count * 0.01)
                msg = f"{label_fn()} — 경과 {elapsed}s"
                await _report(progress_cb, step + sub, total_steps, msg)
    except asyncio.CancelledError:
        return


async def _run_with_heartbeat(
    progress_cb: _ProgressCB | None,
    step: float, total_steps: float,
    label_fn: Callable[[], str],
    worker: Callable[[], object],
    file_hb: _FileHeartbeat | None = None,
):
    """blocking worker 를 to_thread 로 실행하면서 하트비트 태스크를 병행.

    Worker 가 예외를 던지면 하트비트는 취소되고 예외가 그대로 상위로 전파된다.
    파일 하트비트가 있으면 에러 메시지도 파일에 기록.
    """
    interval = _heartbeat_interval()
    hb = asyncio.create_task(
        _heartbeat_loop(progress_cb, step, total_steps, label_fn, interval, file_hb)
    )
    try:
        return await asyncio.to_thread(worker)
    finally:
        hb.cancel()
        # 하트비트 태스크의 종료 예외는 본 작업 결과에 영향을 주지 않는다.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb


_PHASE_KO = {
    "starting": "라운드 준비 중",
    "validator+sme": "Validator + SME 병렬 검토",
    "jury": "Jury 독립 심판",
    "jury_final": "Jury 최종 판정",
    "architect": "Architect 수정 반영",
    "finalizing": "결과 집계 중",
}


async def _generate_tbox_collaborative_async(
    tables: str,
    max_rounds: int,
    progress_cb: _ProgressCB | None,
) -> str:
    """async 진입점. 블로킹 sync 헬퍼를 to_thread 로 감싸고 라운드마다 진행 보고.

    하트비트: 초안/각 라운드/저장 단계에서 MULTI_AGENT_HEARTBEAT_SEC (기본 10) 초
    마다 "Round K [phase=...] — 경과 Xs" 메시지를 report_progress 로 브로드캐스트.
    라운드 내부에서 phase 가 validator+sme → jury → architect 순으로 바뀌므로
    클라이언트는 어떤 서브 에이전트가 돌고 있는지 실시간으로 알 수 있다.

    total_steps = 1(초안) + max_rounds(토론) + 1(저장).
    """
    start = time.monotonic()
    total_steps = 1 + max_rounds + 1
    step = 0

    file_hb = _FileHeartbeat(_heartbeat_file_path(), max_rounds=max_rounds)
    try:
        file_hb.set(status="running", phase="initial_draft", round=1,
                    step="Round 1: Architect — T-Box 초안 생성 시작")
        await _report(progress_cb, step, total_steps,
                      "Round 1: Architect — T-Box 초안 생성 시작")
        state = await _run_with_heartbeat(
            progress_cb, step, total_steps,
            label_fn=lambda: "Round 1 [phase=initial_draft]",
            worker=lambda: _build_initial_draft(tables, file_hb=file_hb),
            file_hb=file_hb,
        )
        if "error" in state:
            file_hb.error(str(state["error"])[:500])
            return json.dumps(state["error"], ensure_ascii=False, indent=2)
        step = 1
        # 조기 종료가 몇 라운드를 아꼈는지 계산하려면 예정 수를 알아야 한다.
        state["max_rounds_planned"] = max_rounds
        a = state["architect_stats"]
        draft_msg = (f"Round 1 완료: 클래스 {a['classes']}, OP {a['object_properties']}, "
                     f"DP {a['data_properties']} (초안 생성)")
        file_hb.set(step=draft_msg,
                    architect_stats=a)
        await _report(progress_cb, step, total_steps, draft_msg)

        for round_num in range(1, max_rounds + 1):
            round_label = f"Round {round_num + 1}: Validator + SME 검토 중"
            file_hb.set(phase="validator+sme", round=round_num + 1, step=round_label)
            await _report(progress_cb, step, total_steps, round_label)

            # Worker thread 가 _set_phase 로 업데이트할 공유 상태.
            # 파일 하트비트는 메인 이벤트 루프에서만 쓰지만, phase 라벨은 worker 에서
            # 도착하므로 mutable dict 로 전달한다.
            phase_state: dict[str, str] = {"phase": "starting"}

            def _on_phase(p: str, _hb=file_hb, _rn=round_num,
                          _ps=phase_state) -> None:
                _ps["phase"] = p
                # 파일 쓰기는 GIL 하 atomic path 이지만 느림 — hb.set 은 flush 를
                # 포함하므로 worker thread 에서 직접 호출해도 메인 루프와
                # race 없다 (파일은 atomic replace).
                _hb.set(phase=p, round=_rn + 1,
                        step=f"Round {_rn + 1} phase={_PHASE_KO.get(p, p)}")

            def _label(_ps=phase_state, _rn=round_num) -> str:
                phase = _ps.get("phase", "?")
                phase_ko = _PHASE_KO.get(phase, phase)
                return f"Round {_rn + 1} [phase={phase_ko}]"

            summary = await _run_with_heartbeat(
                progress_cb, step, total_steps,
                label_fn=_label,
                worker=lambda _rn=round_num: _run_one_debate_round(
                    state, _rn, max_rounds,
                    phase_setter=_on_phase,
                ),
                file_hb=file_hb,
            )
            step += 1
            approved_flag = (
                "✅합의" if summary["consensus"]
                else ("⚠️veto" if state["veto_lock_triggered"] else "진행")
            )
            round_done_msg = (
                f"Round {round_num + 1} 완료 [{approved_flag}]: "
                f"V이슈 {summary['v_issues_count']} (approved={summary['v_approved']}), "
                f"S이슈 {summary['s_issues_count']} (approved={summary['s_approved']}), "
                f"CQ {summary['cq_coverage_pct']}%"
            )
            file_hb.set(
                step=round_done_msg,
                last_round_summary={
                    "round": round_num + 1,
                    "v_issues": summary["v_issues_count"],
                    "s_issues": summary["s_issues_count"],
                    "v_approved": summary["v_approved"],
                    "s_approved": summary["s_approved"],
                    "cq_coverage_pct": summary["cq_coverage_pct"],
                    "consensus": summary["consensus"],
                },
            )
            await _report(progress_cb, step, total_steps, round_done_msg)
            if summary["break_loop"]:
                # early exit: remaining rounds 스킵 — progress는 현재 step 유지
                break

        file_hb.set(phase="finalizing", step="최종 저장 + 통계 계산 중")
        await _report(progress_cb, total_steps - 1, total_steps,
                      "최종 저장 + 통계 계산 중")
        result = await _run_with_heartbeat(
            progress_cb, total_steps - 1, total_steps,
            label_fn=lambda: "저장/통계 [phase=finalizing]",
            worker=lambda: _finalize_and_save(state, start),
            file_hb=file_hb,
        )
        duration = round(time.monotonic() - start, 1)
        file_hb.finish(status="completed")
        await _report(progress_cb, total_steps, total_steps,
                      f"완료 (소요 {duration}초)")
        return result
    except Exception as e:
        file_hb.error(f"{type(e).__name__}: {e}")
        raise


# S2 T-Box 협업 생성 잡 레지스트리 (공용 JobRegistry). generate_tbox_collaborative
# 는 15~40분 걸려 동기 응답 시 MCP stdio 가 끊긴다(S8 과 동일 메커니즘) → 잡
# 패턴으로 job_id 즉시 반환 + 백그라운드 워커 + get_tbox_status 폴링.
_TBOX_JOBS = JobRegistry(
    name="tbox", poll_with="get_tbox_status", logger=logger,
)


def _tbox_job_key(tables: str, max_rounds: int) -> str:
    """single-flight 키 — 동일 입력 중복 호출을 한 잡으로 합치기 위함."""
    return f"{tables}|{max_rounds}"


def generate_tbox_collaborative(
    tables: str = "",
    max_rounds: int = 4,
) -> str:
    """Architect·Validator·SME 토론과 Jury 판정으로 T-Box 를 **백그라운드로** 생성한다.

    ⚠️ 비동기 잡 패턴: 이 도구는 생성을 끝까지 돌리지 않고 daemon 워커를 띄운 뒤
    **즉시 job_id 를 반환**한다(수십 ms). 실제 협업은 15~40분(Bedrock LLM 8~12회)
    걸리므로 동기 응답하면 MCP stdio 타임아웃으로 서버가 끊긴다(2026-06-20 규명).
    완료 여부·결과는 ``get_tbox_status(job_id)`` 로 폴링하라 (30~60초 간격 권장).

    진행 관측: data/generated/_heartbeat.json 에 phase/round/elapsed 가 10초마다
    atomic 기록된다. `watch -n 2 cat data/generated/_heartbeat.json` 으로 실시간
    관측 가능 (MULTI_AGENT_HEARTBEAT_FILE 로 경로 오버라이드).

    라운드 규약:
      - Round 1 (초안): Architect 가 generate_tbox 로 T-Box 초안 생성
      - Round 2..max_rounds+1 (토론): Validator + SME 리뷰 → Architect 수정
      - 예비 합의 조건 (토론 2라운드부터): 두 리뷰어의 실질 승인 + T-Box 로 고칠 수
        있는 CQ 갭 0건 + veto lock 해제 상태. veto lock 은 같은 critical/high
        이슈가 2라운드 연속 남으면 걸리고 매 라운드 다시 판정한다.
      - 실질 승인: 리뷰어가 approved=false 를 내도 차단 이슈가 없으면 승인으로
        본다. 차단 이슈는 critical/high 중 S3 담당 (deferred_to_s3) 표시나
        metric·data_gap·deferred 범주가 아닌 이슈다.
      - 합의: 예비 합의 뒤 Jury 가 production_ready=true 로 판정한 경우뿐이다.
      - CSV FK 가 없어 T-Box 로 해소할 수 없는 CQ 갭은 합의를 막지 않고
        ``debate.rounds[*].cq_block_data_gap`` 에 보고된다. 따라서 합의가 모든 CQ 의
        답변 가능성을 보장하지 않는다.
      - 남은 차단 이슈 중 실행 가능한 수정이 0건이면 합의 없이 조기 종료한다
        (``S2_EARLY_STOP=off`` 로 끈다).
      - 마지막 라운드까지 합의하지 못하면 Jury 최종 판정의 required_fixes 를
        적용하고 합의 없이 끝낸다. Jury 판정이 실패하면 Architect 절충 사유를
        감사 기록으로 남긴다.

    Args:
        tables: 생성할 테이블명 (쉼표 구분). 비어있으면 전체.
        max_rounds: 최대 토론 라운드 수 (초안 제외). 기본 4 → 최대 총 5 라운드.

    Returns:
        {"started": bool, "job_id": str, "status": "running"|"reused",
         "poll_with": "get_tbox_status", "message": str}
    """
    key = _tbox_job_key(tables, max_rounds)
    return _TBOX_JOBS.dispatch(
        key=key,
        worker=lambda: _generate_tbox_collaborative_sync(tables, max_rounds),
    )


def get_tbox_status(job_id: str) -> str:
    """generate_tbox_collaborative 백그라운드 잡의 상태/결과를 조회한다 (즉시 반환).

    Args:
        job_id: generate_tbox_collaborative 가 반환한 job_id.

    Returns:
        running/done/failed/unknown 분기 JSON. done 시 result 에 기존
        generate_tbox_collaborative 응답(consensus/rounds/debate 등)이 담긴다.
    """
    # done 응답은 라운드 수(≤max_rounds+1)에 비례할 뿐 인스턴스-비례 거대
    # 리스트가 없어 slim_fn 불필요.
    return _TBOX_JOBS.status(job_id)


def _generate_tbox_collaborative_sync(
    tables: str = "", max_rounds: int = 4,
) -> str:
    """테스트/스크립트용 + 잡 워커용 동기 진입점 — progress 보고 없음.

    기존 동기 호출 패턴(tests/test_multi_agent_tbox.py)과 잡 워커 클로저가
    공유한다. daemon 워커 스레드에서 자체 이벤트 루프(asyncio.run)를 돌린다.
    """
    try:
        return asyncio.run(
            _generate_tbox_collaborative_async(tables, max_rounds, progress_cb=None)
        )
    except Exception as e:
        return error_response(e, logger=logger)
