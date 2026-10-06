"""Competency Questions 생성 + 검증 도구 — 온톨로지 범위 정의

파이프라인 최초 단계(S0)에서 실행.
CSV 스키마와 도메인 컨텍스트를 분석하여, 온톨로지가 반드시 답할 수 있어야 하는
비즈니스 질문(Competency Questions)을 자동 생성한다.

CQ는 이후 단계에 영향:
- S2 T-Box 생성: CQ를 프롬프트에 포함하여 필요한 클래스/프로퍼티가 누락되지 않게 함
- S12 질의 테스트: CQ를 테스트 케이스로 변환하여 end-to-end 검증

validate_competency_questions: 생성된 CQ가 실제 KG에서 답변 가능한지 검증
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter

from config import (
    COMPETENCY_QUESTIONS_PATH,
    SEMANTIC_DICT_PATH,
    SOURCE_RAWDATA_DIR,
    TBOX_PATH,
)
from domain.namespaces import DOMAIN_CONFIG, prepend_prefixes
from domain.sparql_templates import reject_sparql_egress
from domain.tbox_utils import _new_graph
from tools.bedrock import invoke_bedrock_text
from tools.common import atomic_write, error_response
from tools.query_test import _is_local_name

logger = logging.getLogger(__name__)

_CQ_PATH = COMPETENCY_QUESTIONS_PATH


def _write_curated_competency_questions(valid_cqs: list[dict]) -> None:
    """CQ를 큐레이션 입력 경로에 저장한다.

    CQ는 S2 설계와 S12 커버리지 검증이 다시 소비하는 사용자 의도이므로
    생성 산출물 경계의 명시적 예외다. ``data/generated``로 옮기면 입력과
    실행 결과의 수명주기가 섞이므로 ``data/source/query_tests``에 유지한다.
    """
    os.makedirs(os.path.dirname(_CQ_PATH), exist_ok=True)
    atomic_write(
        _CQ_PATH,
        json.dumps(valid_cqs, ensure_ascii=False, indent=2),
    )


def _load_csv_summary() -> str:
    """CSV 파일 목록 + 컬럼 요약을 문자열로 반환."""
    import csv as csv_mod
    import glob

    csv_files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
    if not csv_files:
        return ""

    lines = []
    for csv_path in csv_files:
        table_name = os.path.basename(csv_path).replace(".csv", "")
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                reader = csv_mod.reader(f)
                header = next(reader, [])
                row_count = sum(1 for _ in reader)
            lines.append(f"- {table_name} ({row_count}행): {', '.join(header)}")
        except Exception:
            lines.append(f"- {table_name}: (읽기 실패)")

    return "\n".join(lines)


def _load_tacit_summary() -> str:
    """암묵지 파일 요약."""
    import glob

    from config import SOURCE_TACIT_DIR
    if not os.path.isdir(SOURCE_TACIT_DIR):
        return "암묵지 없음"

    files = sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")))
    if not files:
        return "암묵지 없음"

    return "\n".join(f"- {os.path.basename(f)}" for f in files)


def _parse_user_cqs(raw: str) -> list[dict]:
    """사용자가 제공한 CQ 문자열을 dict 리스트로 파싱한다.

    받을 수 있는 형식:
    1. JSON 배열: [{"question_ko": "...", "domains": [...]}, ...]
    2. plain text: 한 줄당 하나의 질문. 빈 줄/#으로 시작하는 주석 무시.
    """
    txt = (raw or "").strip()
    if not txt:
        return []
    # JSON 배열 우선
    if txt.startswith("["):
        data = json.loads(txt)
        if not isinstance(data, list):
            raise ValueError("user_provided는 JSON 배열이어야 합니다.")
        parsed: list[dict] = []
        for i, item in enumerate(data, 1):
            if isinstance(item, str):
                parsed.append({"question_ko": item.strip()})
            elif isinstance(item, dict):
                parsed.append(dict(item))
            else:
                raise ValueError(f"{i}번째 CQ 항목이 문자열 또는 객체가 아닙니다.")
        return parsed
    # plain text: 줄 단위
    lines: list[dict] = []
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append({"question_ko": line})
    return lines


def check_competency_questions_exist() -> str:
    """저장된 Competency Questions 파일이 존재하는지 + 개수를 반환한다.

    FULL_PIPELINE 진입 시 에이전트가 첫 번째로 호출해야 하는 경량 도구.
    결과에 따라 사용자에게 CQ 입력을 요청하거나 자동 생성 동의를 받는다.

    Returns:
        {"exists": bool, "count": int, "path": str}
    """
    try:
        if not os.path.exists(_CQ_PATH):
            return json.dumps({
                "success": True, "exists": False, "count": 0, "path": _CQ_PATH,
            }, ensure_ascii=False, indent=2)
        with open(_CQ_PATH, encoding="utf-8") as f:
            data = json.load(f)
        cqs = data if isinstance(data, list) else data.get("questions", [])
        return json.dumps({
            "success": True,
            "exists": len(cqs) > 0,
            "count": len(cqs),
            "path": _CQ_PATH,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def generate_competency_questions(
    count: int = 10,
    user_provided: str = "",
    auto_approved: bool = False,
) -> str:
    """Competency Questions를 저장한다. CQ는 온톨로지 범위를 정의하는 사용자 의도이므로
    **기본적으로 사용자 입력을 우선**한다.

    동작 모드:
    - user_provided 가 주어지면: LLM 호출 없이 파싱·검증 후 저장. source="user_provided".
    - user_provided 가 비어있고 auto_approved=False: 에러 반환 (사용자 동의 요구).
      에이전트는 먼저 사용자에게 CQ 입력을 요청하고, 거절 시 자동 생성 동의를 받아
      auto_approved=True 로 재호출해야 한다.
    - auto_approved=True: CSV 스키마를 분석해 CQ를 LLM으로 자동 생성. source="auto_generated".

    Args:
        count: 자동 생성 시 CQ 수. 기본 10개.
        user_provided: 사용자가 직접 제공하는 CQ. JSON 배열 또는 한 줄당 하나의 질문.
        auto_approved: 사용자 입력 없이 자동 생성하라는 명시적 동의.
    """
    try:
        # ── 모드 1: 사용자 제공 CQ ──────────────────────────
        if user_provided.strip():
            user_cqs = _parse_user_cqs(user_provided)
            if not user_cqs:
                return error_response(
                    "user_provided 파싱 결과가 비어있습니다.",
                    hint="JSON 배열 또는 줄 단위 질문 텍스트를 넘기세요.",
                    logger=logger,
                )
            valid_cqs = []
            for cq in user_cqs:
                if not cq.get("question_ko"):
                    continue
                cq.setdefault("id", f"CQ{len(valid_cqs) + 1:02d}")
                cq.setdefault("difficulty", "medium")
                cq.setdefault("domains", [])
                cq.setdefault("expected_answer_type", "list")
                cq.setdefault("business_value", "")
                cq["source"] = "user_provided"
                valid_cqs.append(cq)
            _write_curated_competency_questions(valid_cqs)
            logger.info("사용자 제공 CQ %d개 저장", len(valid_cqs))
            return json.dumps({
                "success": True,
                "source": "user_provided",
                "artifact_class": "curated_input",
                "path": _CQ_PATH,
                "count": len(valid_cqs),
                "questions": valid_cqs,
                "hint": ("domains 필드가 비어있는 항목은 이후 S12 검증에서 제한적입니다. "
                         "필요 시 CQ 편집 후 재호출하세요."),
            }, ensure_ascii=False, indent=2)

        # ── 모드 2: 명시적 동의 없으면 거부 ──────────────────
        if not auto_approved:
            return error_response(
                "CQ가 제공되지 않았고 자동 생성 동의도 없습니다.",
                hint=("Competency Questions는 온톨로지 범위를 결정하는 핵심 입력입니다. "
                      "다음 중 하나로 다시 호출하세요:\n"
                      "1) user_provided=<JSON 배열 또는 줄 단위 질문 텍스트>\n"
                      "2) auto_approved=True (CSV 스키마 기반 자동 생성 동의)"),
                logger=logger,
            )

        # ── 모드 3: 자동 생성 ──────────────────────────────
        csv_summary = _load_csv_summary()
        if not csv_summary:
            return error_response(
                "CSV 파일이 없습니다.",
                hint="data/source/rawdata/에 CSV 파일을 배치하세요.",
                logger=logger,
            )

        tacit_summary = _load_tacit_summary()
        domain = DOMAIN_CONFIG["domain"]
        upper = DOMAIN_CONFIG.get("upper_ontology", {})

        prompt = f"""당신은 {domain['name_ko']} 도메인의 온톨로지 엔지니어입니다.

## 작업
아래 데이터 테이블 구조를 분석하여, 이 데이터로 구축할 Knowledge Graph가
반드시 답할 수 있어야 하는 **Competency Questions (CQ)** {count}개를 생성하세요.

## 데이터 테이블
{csv_summary}

## 암묵지 (현장 도메인 지식)
{tacit_summary}

## 상위 온톨로지
{upper.get('framework', 'IOF/BFO')}

## CQ 생성 규칙
1. **크로스 도메인**: 2~3개 테이블을 교차하는 질문 (단일 테이블 조회 X)
2. **비즈니스 가치**: 현장에서 실제로 묻는 의사결정용 질문
3. **다양한 유형**: 집계(COUNT/SUM), 비교(FILTER), 연결(JOIN), 추세, 인과 관계
4. **범위 균형**: 설비, 공정, 품질, 에너지, 환경, 물류, 정비 도메인을 골고루 커버
5. **테이블 커버리지**: 모든 테이블이 최소 1개 CQ의 domains에 포함되어야 함. 위 테이블 목록을 하나도 빠짐없이 커버하세요.
6. **난이도 배분**: count에 따라 Easy 20%, Medium 40%, Hard 40% 비율
7. **SPARQL/Cypher로 답 가능**: 자연어 질문이지만, SPARQL SELECT 또는 Cypher MATCH로 답할 수 있어야 함

## 출력 형식 (JSON 배열)
```json
[
  {{
    "id": "CQ01",
    "question_ko": "한국어 질문",
    "question_en": "English question",
    "difficulty": "easy|medium|hard",
    "domains": ["Table1", "Table2"],
    "expected_answer_type": "list|count|comparison|trend|causal",
    "business_value": "이 질문이 왜 중요한지 1줄 설명"
  }}
]
```

JSON 배열만 출력하세요:"""

        text = invoke_bedrock_text(prompt, max_tokens=4096)

        # JSON 추출
        text = text.strip()
        if "```" in text:
            import re
            match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
            if match:
                text = match.group(1).strip()

        logger.info("CQ 생성 LLM 응답 길이: %d", len(text))
        cqs = json.loads(text)

        # 검증
        valid_cqs = []
        for cq in cqs:
            if "question_ko" in cq and "domains" in cq:
                cq.setdefault("id", f"CQ{len(valid_cqs) + 1:02d}")
                cq.setdefault("difficulty", "medium")
                cq.setdefault("expected_answer_type", "list")
                cq.setdefault("business_value", "")
                cq["source"] = "auto_generated"
                valid_cqs.append(cq)

        # 저장
        _write_curated_competency_questions(valid_cqs)

        # ── 분포 검증: 도메인/난이도 균형 체크 ──
        warnings = []

        # 난이도 분포
        from collections import Counter
        difficulty_dist = Counter(cq.get("difficulty", "medium") for cq in valid_cqs)
        for level in ("easy", "medium", "hard"):
            difficulty_dist.setdefault(level, 0)
        total_cqs = len(valid_cqs)
        if total_cqs > 0:
            for level, cnt in difficulty_dist.items():
                ratio = cnt / total_cqs
                if ratio > 0.6:
                    warnings.append(
                        f"난이도 '{level}'이 {cnt}/{total_cqs} ({ratio:.0%})로 편중됨"
                    )
            if difficulty_dist.get("hard", 0) == 0:
                warnings.append("Hard 난이도 CQ가 없음 — 크로스 도메인 질의 테스트 불가")

        # 도메인 분포
        domain_dist = Counter()
        for cq in valid_cqs:
            for d in cq.get("domains", []):
                domain_dist[d] += 1
        if domain_dist:
            max_domain_count = max(domain_dist.values())
            min_domain_count = min(domain_dist.values())
            if max_domain_count > 0 and min_domain_count / max_domain_count < 0.2:
                over = [d for d, c in domain_dist.items() if c == max_domain_count]
                under = [d for d, c in domain_dist.items() if c == min_domain_count]
                warnings.append(
                    f"도메인 편중: {over[0]}({max_domain_count}회) vs {under[0]}({min_domain_count}회)"
                )

        result = {
            "success": True,
            "source": "auto_generated",
            "artifact_class": "curated_input",
            "path": _CQ_PATH,
            "count": len(valid_cqs),
            "difficulty_distribution": dict(difficulty_dist),
            "domain_distribution": dict(domain_dist),
            "questions": valid_cqs,
        }
        if warnings:
            result["warnings"] = warnings
            logger.warning("CQ 분포 경고: %s", "; ".join(warnings))

        return json.dumps(result, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def read_competency_questions() -> str:
    """저장된 Competency Questions를 읽는다."""
    try:
        with open(_CQ_PATH, encoding="utf-8") as f:
            cqs = json.load(f)
        return json.dumps({
            "success": True,
            "count": len(cqs),
            "questions": cqs,
        }, ensure_ascii=False, indent=2)
    except FileNotFoundError:
        return error_response(
            "Competency Questions가 없습니다.",
            hint="generate_competency_questions로 먼저 생성하세요.",
            logger=logger,
        )
    except Exception as e:
        return error_response(e, logger=logger)


def _find_connecting_path(
    cls_a: str, cls_b: str, tbox, max_hops: int = 3,
) -> dict | None:
    """T-Box ObjectProperty 기반 BFS로 두 클래스 간 연결 경로 탐색.

    adjacency 그래프의 URI에서 local name을 매칭하므로,
    T-Box가 어떤 네임스페이스를 사용하든 동작한다.

    Args:
        cls_a: Source class local name (e.g., "Equipment")
        cls_b: Target class local name (e.g., "Maintenance")
        tbox: rdflib Graph with T-Box
        max_hops: Maximum path length

    Returns:
        dict with path/edges/hops, or None if no path found
    """
    from rdflib import URIRef

    from domain.graph_utils import build_class_adjacency, find_path_bfs

    adjacency = build_class_adjacency(tbox)

    # local name → URI 역매핑 (adjacency에 등장하는 모든 클래스 URI 수집)
    local_to_uri: dict[str, URIRef] = {}
    for cls_uri in adjacency:
        local = str(cls_uri).split("/")[-1].split("#")[-1]
        local_to_uri[local.lower()] = cls_uri
        # neighbor URI도 수집
        for neighbor, _ in adjacency[cls_uri]:
            n_local = str(neighbor).split("/")[-1].split("#")[-1]
            local_to_uri[n_local.lower()] = neighbor

    uri_a = local_to_uri.get(cls_a.lower())
    uri_b = local_to_uri.get(cls_b.lower())
    if uri_a is None or uri_b is None:
        return None

    return find_path_bfs(adjacency, uri_a, uri_b, max_hops=max_hops)


def _find_connecting_op(a: str, b: str, obj_props: dict) -> str | None:
    """두 클래스를 연결하는 ObjectProperty를 시맨틱 딕셔너리에서 찾는다.

    매칭 우선순위: exact match > prefix match.
    부분 문자열 매칭은 오탐이 많아 사용하지 않는다.
    """
    a_lower, b_lower = a.lower(), b.lower()
    for op_name, op_info in obj_props.items():
        raw_domain = op_info.get("domain") or ""
        raw_range = op_info.get("range") or ""
        op_domain = (raw_domain[0] if isinstance(raw_domain, list) else raw_domain).replace("_", "")
        op_range = (raw_range[0] if isinstance(raw_range, list) else raw_range).replace("_", "")
        op_domain_lower = op_domain.lower()
        op_range_lower = op_range.lower()
        # Exact match
        if (a_lower == op_domain_lower and b_lower == op_range_lower) or \
           (b_lower == op_domain_lower and a_lower == op_range_lower):
            return op_name
        # Prefix match: EquipmentMaster domain matches Equipment query
        if (op_domain_lower.startswith(a_lower) and op_range_lower.startswith(b_lower)) or \
           (op_domain_lower.startswith(b_lower) and op_range_lower.startswith(a_lower)):
            return op_name
    return None


def _invalid_name_entry(kind: str, names) -> dict:
    """SPARQL 에 보간하지 않은 이름을 실패 체크로 남기는 항목을 만든다.

    ``sparql`` 이 ``None`` 이므로 호출부는 이 항목을 실행하지 않고 실패로 기록한다.
    """
    return {
        "type": "invalid_name",
        "sparql": None,
        "description": f"{kind} 이름 형식 확인",
        "error": (
            "SPARQL 로컬 이름 형식이 아니어서 질의에서 제외: "
            + ", ".join(repr(str(name)[:80]) for name in names)
        ),
    }


def _iri_local(iri) -> str:
    """IRI 문자열의 마지막 ``/`` 또는 ``#`` 뒤 부분."""
    return str(iri).split("/")[-1].split("#")[-1]


def _build_connectivity_queries(
    cq: dict, obj_props: dict, sem_dict: dict | None = None, *, tbox=None,
) -> list[dict]:
    """CQ의 domains를 분석하여 KG 연결성 검증 SPARQL을 프로그래매틱으로 생성한다.

    LLM 호출 없음. 3단계 검증:
    1. 각 도메인 클래스에 인스턴스가 존재하는가 (instance_check)
    2. 클래스 간 연결 경로가 존재하는가 — BFS 우선, prefix match 폴백 (connectivity_check)
    3. 핵심 DatatypeProperty에 값이 채워져 있는가 (value_check)

    ``prefix:local`` 자리에 넣는 이름 (CQ domains, T-Box 경로의 클래스·프로퍼티,
    딕셔너리의 OP·DP) 은 ``tools.query_test._is_local_name`` 형식일 때만 보간한다.
    형식이 아닌 이름은 질의를 만들지 않고 ``type="invalid_name"`` 항목으로 남긴다.

    Args:
        tbox: Optional rdflib Graph for BFS connectivity (T-Box).
    """
    from domain.namespaces import NS_PREFIX
    pfx = NS_PREFIX
    domains = cq.get("domains", [])
    queries = []
    # CSV 테이블명 (snake_case) → PascalCase 클래스명 (abox_generation 컨벤션).
    # 이미 PascalCase 인 domains 를 망가뜨리지 않도록 _snake_to_class_name 사용
    # (``capitalize()`` 는 EquipmentMaster → Equipmentmaster 로 바꿔 없는 클래스를
    # 질의하게 만든다).
    cls_names: list[str] = []
    invalid_domains: list[object] = []
    for domain in domains:
        cls_name = _snake_to_class_name(domain) if isinstance(domain, str) else None
        if _is_local_name(cls_name):
            cls_names.append(cls_name)
        else:
            invalid_domains.append(domain)
    if invalid_domains:
        queries.append(_invalid_name_entry("CQ 도메인", invalid_domains))

    # ── 1. 각 도메인 클래스에 인스턴스가 있는지 ──
    for cls_name in cls_names:
        q = f"SELECT (COUNT(?x) AS ?cnt) WHERE {{ ?x a {pfx}:{cls_name} }}"
        queries.append({
            "type": "instance_check",
            "class": cls_name,
            "sparql": q,
            "description": f"{cls_name} 인스턴스 존재 확인",
        })

    # ── 2. 도메인 간 연결 경로 확인 (BFS → prefix match → 간접) ──
    for i in range(len(cls_names)):
        for j in range(i + 1, len(cls_names)):
            a, b = cls_names[i], cls_names[j]

            # --- BFS 우선: T-Box가 있으면 ObjectProperty 그래프에서 경로 탐색 ---
            bfs_result = _find_connecting_path(a, b, tbox) if tbox is not None else None

            if bfs_result and bfs_result["hops"] == 1:
                # BFS 1-hop: 직접 연결
                edge_local = _iri_local(bfs_result["edges"][0])
                if not _is_local_name(edge_local):
                    queries.append(_invalid_name_entry(
                        f"{a} ↔ {b} 연결 프로퍼티 (BFS)", [edge_local],
                    ))
                    continue
                q = (f"SELECT ?x ?y WHERE {{ "
                     f"?x a {pfx}:{a} . ?y a {pfx}:{b} . "
                     f"{{ ?x {pfx}:{edge_local} ?y }} UNION {{ ?y {pfx}:{edge_local} ?x }} "
                     f"}} LIMIT 1")
                queries.append({
                    "type": "connectivity_check",
                    "from": a, "to": b,
                    "property": edge_local,
                    "method": "bfs",
                    "sparql": q,
                    "description": f"{a} ↔ {b} 연결 확인 (via {edge_local}, BFS)",
                })
            elif bfs_result and bfs_result["hops"] >= 2:
                # BFS multi-hop: 중간 노드 경유
                path_locals = [_iri_local(p) for p in bfs_result["path"]]
                edge_locals = [_iri_local(e) for e in bfs_result["edges"]]
                mid = path_locals[1]
                # 2-hop SPARQL 생성 (첫 2 에지만)
                op1 = edge_locals[0]
                op2 = edge_locals[1] if len(edge_locals) > 1 else edge_locals[0]
                invalid = [n for n in (mid, op1, op2) if not _is_local_name(n)]
                if invalid:
                    queries.append(_invalid_name_entry(
                        f"{a} ↔ {b} 경유 경로 (BFS)", invalid,
                    ))
                    continue
                q = (f"SELECT ?x ?z WHERE {{ "
                     f"?x a {pfx}:{a} . ?m a {pfx}:{mid} . ?z a {pfx}:{b} . "
                     f"{{ ?x {pfx}:{op1} ?m }} UNION {{ ?m {pfx}:{op1} ?x }} . "
                     f"{{ ?m {pfx}:{op2} ?z }} UNION {{ ?z {pfx}:{op2} ?m }} "
                     f"}} LIMIT 1")
                queries.append({
                    "type": "multihop_check",
                    "from": a, "via": mid, "to": b,
                    "properties": edge_locals,
                    "hops": bfs_result["hops"],
                    "method": "bfs",
                    "sparql": q,
                    "description": f"{a} → {' → '.join(path_locals[1:-1])} → {b} BFS 멀티홉 연결 확인",
                })
            else:
                # --- Prefix match 폴백 ---
                connecting_op = _find_connecting_op(a, b, obj_props)

                if connecting_op and not _is_local_name(connecting_op):
                    queries.append(_invalid_name_entry(
                        f"{a} ↔ {b} 연결 프로퍼티", [connecting_op],
                    ))
                elif connecting_op:
                    q = (f"SELECT ?x ?y WHERE {{ "
                         f"?x a {pfx}:{a} . ?y a {pfx}:{b} . "
                         f"{{ ?x {pfx}:{connecting_op} ?y }} UNION {{ ?y {pfx}:{connecting_op} ?x }} "
                         f"}} LIMIT 1")
                    queries.append({
                        "type": "connectivity_check",
                        "from": a, "to": b,
                        "property": connecting_op,
                        "method": "prefix_match",
                        "sparql": q,
                        "description": f"{a} ↔ {b} 연결 확인 (via {connecting_op})",
                    })
                else:
                    # 멀티홉 시도: a→?mid→b (중간 클래스 경유)
                    multihop_found = False
                    for mid in cls_names:
                        if mid in (a, b):
                            continue
                        op_a_mid = _find_connecting_op(a, mid, obj_props)
                        op_mid_b = _find_connecting_op(mid, b, obj_props)
                        if not (op_a_mid and op_mid_b):
                            continue
                        multihop_found = True
                        invalid = [
                            op for op in (op_a_mid, op_mid_b) if not _is_local_name(op)
                        ]
                        if invalid:
                            queries.append(_invalid_name_entry(
                                f"{a} → {mid} → {b} 경유 프로퍼티", invalid,
                            ))
                            break
                        q = (f"SELECT ?x ?z WHERE {{ "
                             f"?x a {pfx}:{a} . ?m a {pfx}:{mid} . ?z a {pfx}:{b} . "
                             f"{{ ?x {pfx}:{op_a_mid} ?m }} UNION {{ ?m {pfx}:{op_a_mid} ?x }} . "
                             f"{{ ?m {pfx}:{op_mid_b} ?z }} UNION {{ ?z {pfx}:{op_mid_b} ?m }} "
                             f"}} LIMIT 1")
                        queries.append({
                            "type": "multihop_check",
                            "from": a, "via": mid, "to": b,
                            "properties": [op_a_mid, op_mid_b],
                            "method": "prefix_match",
                            "sparql": q,
                            "description": f"{a} → {mid} → {b} 멀티홉 연결 확인",
                        })
                        break

                    if not multihop_found:
                        # 공유 FK 간접 연결 폴백.
                        #
                        # ``?shared`` 를 무제약으로 두면 **아무 리터럴 우연 일치**
                        # 가 "공유 FK" 로 통과한다 — 같은 단위 문자열('mm'),
                        # 같은 수치(0), 같은 타임스탬프만으로 두 클래스가 연결됐다고
                        # 판정됐다 (2026-08-08 규명: 샘플에서 positive 판정이 전부
                        # 리터럴 우연이었고 true positive 는 0건).
                        #
                        # ``isIRI`` 단독은 반대 방향으로 틀린다: FK 가 아직
                        # master 로 해소되지 않아 식별자 **리터럴** 로 남아 있는
                        # 정당한 경우를 죽인다. 그래서 IRI **또는** 식별자 모양의
                        # 리터럴(길이 있는 문자열, 숫자 아님)만 허용한다.
                        q = (f"SELECT ?x ?y ?shared WHERE {{ "
                             f"?x a {pfx}:{a} . ?y a {pfx}:{b} . "
                             f"?x ?p ?shared . ?y ?q ?shared . "
                             f"FILTER(?p != rdf:type && ?q != rdf:type) "
                             f"FILTER(isIRI(?shared) || "
                             f"(isLiteral(?shared) && !isNumeric(?shared) && "
                             f"STRLEN(STR(?shared)) >= 3 && "
                             f"REGEX(STR(?shared), \"[A-Za-z]\") && "
                             f"REGEX(STR(?shared), \"[0-9]\"))) "
                             f"}} LIMIT 1")
                        queries.append({
                            "type": "indirect_connectivity",
                            "from": a, "to": b,
                            "sparql": q,
                            "description": f"{a} ↔ {b} 간접 연결 확인 (공유 FK)",
                        })

    # ── 3. 핵심 DatatypeProperty에 값이 채워져 있는지 ──
    classes_dict = (sem_dict or {}).get("classes", {})
    for cls_name in cls_names:
        cls_info = classes_dict.get(cls_name, {})
        dps = cls_info.get("datatype_properties", [])
        if not dps:
            continue
        # 첫 3개 DP만 검증 (전부 하면 느려짐)
        sample_dps = dps[:3] if isinstance(dps, list) else list(dps)[:3]
        dp_names = []
        invalid_dps = []
        for dp in sample_dps:
            dp_name = dp.get("name", dp) if isinstance(dp, dict) else str(dp)
            if _is_local_name(dp_name):
                dp_names.append(dp_name)
            else:
                invalid_dps.append(dp_name)
        if invalid_dps:
            queries.append(_invalid_name_entry(
                f"{cls_name} DatatypeProperty", invalid_dps,
            ))
        if dp_names:
            dp_patterns = " ".join(f"OPTIONAL {{ ?x {pfx}:{dp} ?v{i} }}" for i, dp in enumerate(dp_names))
            filters = " || ".join(f"BOUND(?v{i})" for i in range(len(dp_names)))
            q = (f"SELECT (COUNT(?x) AS ?cnt) WHERE {{ "
                 f"?x a {pfx}:{cls_name} . {dp_patterns} "
                 f"FILTER({filters}) }}")
            queries.append({
                "type": "value_check",
                "class": cls_name,
                "properties": dp_names,
                "sparql": q,
                "description": f"{cls_name} DatatypeProperty 값 존재 확인 ({', '.join(dp_names[:2])}...)",
            })

    return queries


# ── CQ 분석 헬퍼 (Bezerra et al. 2013, Wisniewski et al. 2019) ──────


# Wisniewski CQ type classification patterns
# Order matters: start-of-string patterns first, then content patterns.
# "list" is checked before "boolean" to avoid false matches (e.g. "어떤 ...인가?")
_CQ_TYPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("count", re.compile(r"^(몇\s*개|몇\s*건|몇\s*종|how\s+many)", re.IGNORECASE)),
    ("list", re.compile(r"^(어떤|무엇|무슨|which\s|what\s|목록|list)", re.IGNORECASE)),
    ("boolean", re.compile(r"(인가\??$|입니까\??$|^is\s|^does\s|^are\s|^can\s|가능한가)", re.IGNORECASE)),
    ("comparison", re.compile(r"(비교|compare|차이|difference|대비|versus|vs)", re.IGNORECASE)),
    ("aggregation", re.compile(r"(평균|최대|최소|합계|average|max\b|min\b|total|sum\b|통계)", re.IGNORECASE)),
    ("temporal", re.compile(r"(언제|when|기간|period|시점|추세|trend|변화|기간별)", re.IGNORECASE)),
    ("causal", re.compile(r"(왜|why|원인|cause|영향|impact|때문|이유)", re.IGNORECASE)),
]


def _classify_cq_type(question: str) -> str:
    """CQ 질문 텍스트에서 Wisniewski 유형을 분류한다."""
    for cq_type, pattern in _CQ_TYPE_PATTERNS:
        if pattern.search(question):
            return cq_type
    return "list"  # 기본값: 엔티티 목록 질의


def _load_tbox_entities() -> tuple[set[str], set[str]]:
    """T-Box에서 클래스 이름과 프로퍼티 이름을 로드한다.

    Returns:
        (class_local_names, property_local_names) 튜플.
    """
    from rdflib import OWL, RDF, URIRef

    from domain.namespaces import DOMAIN_NS

    classes: set[str] = set()
    properties: set[str] = set()
    steel_ns = DOMAIN_NS

    if not os.path.exists(TBOX_PATH):
        return classes, properties

    try:
        g = _new_graph()
        g.parse(TBOX_PATH, format="turtle")
        for cls in g.subjects(RDF.type, OWL.Class):
            if isinstance(cls, URIRef) and str(cls).startswith(steel_ns):
                classes.add(str(cls).split("#")[-1].split("/")[-1].lower())
        for prop_type in (OWL.ObjectProperty, OWL.DatatypeProperty):
            for prop in g.subjects(RDF.type, prop_type):
                if isinstance(prop, URIRef) and str(prop).startswith(steel_ns):
                    properties.add(str(prop).split("#")[-1].split("/")[-1].lower())
    except Exception:  # noqa: BLE001 — T-Box가 없거나 불완전하면 빈 어휘로 폴백한다
        pass

    return classes, properties


def _load_table_class_map() -> dict[str, str]:
    """Load rules/domain/table_class_mapping.json as {table_name: ClassLocalName}.

    CQ ``domains`` entries hold raw source table names (e.g.
    ``SOURCE_TABLE_007``) while the T-Box declares domain
    classes (``ProcessResultD``). Without this mapping every CQ is reported
    as a coverage gap even though the class exists. Prefix (``steel:``) is
    stripped so callers get bare local names.

    Returns an empty dict when the file is absent (domain-neutral no-op).
    """
    from domain.table_mapping import load_table_class_mapping
    return load_table_class_mapping()


def _resolve_domain_to_class(domain: str, table_class_map: dict[str, str]) -> str:
    """Resolve one CQ ``domains`` entry to a T-Box class local name.

    Falls back to stripping underscores when the domain is not a known source
    table, preserving the previous behaviour for hand-written class names.
    """
    mapped = table_class_map.get(domain)
    if mapped:
        return mapped
    return domain.replace("_", "")


def _snake_to_class_name(domain: str) -> str:
    """``snake_case`` 테이블명 → PascalCase 클래스명. 이미 PascalCase 면 그대로.

    ``str.capitalize()`` 는 **두 번째 글자 이후를 소문자로 내린다**. CQ ``domains``
    에 이미 클래스명이 들어있는 흔한 경우 (``EquipmentMaster``) 에 이 폴백을
    적용하면 ``Equipmentmaster`` 가 되어 ``per_class`` 조회가 전부 실패하고, 실제로
    인스턴스가 있는 CQ 가 "인스턴스 0" gap 으로 오보고됐다 (2026-08-08 규명).
    구분자가 없으면 변환하지 않는 것이 정답이다.
    """
    if "_" not in domain:
        return domain          # 이미 PascalCase 또는 단일 토큰 — 손대지 않는다
    return "".join(part[:1].upper() + part[1:] for part in domain.split("_") if part)


def _assess_cq_answerability(
    cq: dict,
    tbox_classes: set[str],
    tbox_properties: set[str],
    table_class_map: dict[str, str] | None = None,
) -> dict:
    """CQ의 T-Box 답변 가능성을 평가한다 (Bezerra et al. 2013 CQChecker).

    CQ의 domains와 질문 텍스트에서 도메인 언급을 추출하고,
    T-Box 클래스/프로퍼티와 매칭하여 3단계로 분류한다.

    Returns:
        {"level": "fully_answerable"|"partially_answerable"|"unanswerable",
         "matched_classes": [...], "missing_classes": [...],
         "matched_properties": [...], "missing_properties": [...]}
    """
    # CQ에서 참조하는 도메인 클래스 추출 (domains 필드 기반).
    # domains 는 원본 테이블명일 수 있어 table_class_mapping 을 먼저 경유한다.
    if table_class_map is None:
        table_class_map = _load_table_class_map()
    required_classes = set()
    for d in cq.get("domains", []):
        required_classes.add(
            _resolve_domain_to_class(d, table_class_map).lower()
        )

    # 질문 텍스트에서 추가 힌트 추출 (CamelCase / 한글 키워드)
    question = cq.get("question_ko", "") + " " + cq.get("question_en", "")

    # CamelCase 단어에서 클래스명 추출 시도
    camel_words = re.findall(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+", question)
    for w in camel_words:
        required_classes.add(w.lower())

    # 매칭
    matched_classes = sorted(required_classes & tbox_classes)
    missing_classes = sorted(required_classes - tbox_classes)

    # 프로퍼티 참조 체크: expected_answer_type 기반 추론
    required_properties: set[str] = set()
    cq.get("expected_answer_type", "")
    # camelCase 프로퍼티명이 질문에 직접 등장하는 경우
    camel_props = re.findall(r"[a-z]+(?:[A-Z][a-z]+)+", question)
    for p in camel_props:
        required_properties.add(p.lower())

    matched_properties = sorted(required_properties & tbox_properties)
    missing_properties = sorted(required_properties - tbox_properties)

    # 분류
    if not required_classes or not missing_classes:
        level = "fully_answerable"
    elif matched_classes:
        level = "partially_answerable"
    else:
        level = "unanswerable"

    return {
        "level": level,
        "matched_classes": matched_classes,
        "missing_classes": missing_classes,
        "matched_properties": matched_properties,
        "missing_properties": missing_properties,
    }


# ── CQ Feedback Loop — 파이프라인 단계별 커버리지 검증 ──────────────


#: 경로 탐색 최대 홉 수. CQ 조인은 보통 1~2홉이고, 3홉을 넘으면 "이어져 있다" 고
#: 말하기 어렵다 (임의의 두 클래스가 MasterData 류 허브를 경유해 연결돼 버린다).
_CQ_PATH_MAX_HOPS = 2

#: 응답에 실을 미연결 쌍 표본 상한.
_CQ_GAP_SAMPLE = 12


def _class_ancestors(tbox, local_lower: str) -> set[str]:
    """클래스와 그 조상들의 local name (lowercase) 집합.

    OP 의 domain/range 는 상위 클래스에 걸려 있을 수 있다 (예:
    ``directlyFollows`` 의 domain 이 ``ManufacturingProcessStep`` 이면 자식
    ``ProcessRolling`` 도 그 관계를 쓴다). 조상을 무시하면 정당한 경로를 놓친다.
    """
    from rdflib import OWL, RDF, RDFS, URIRef

    def _ln(u) -> str:
        return str(u).rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()

    by_local: dict[str, URIRef] = {}
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if isinstance(cls, URIRef):
            by_local[_ln(cls)] = cls
    start = by_local.get(local_lower)
    if start is None:
        return {local_lower}

    out = {local_lower}
    frontier = [start]
    seen = {start}
    while frontier:
        node = frontier.pop()
        for parent in tbox.objects(node, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent not in seen:
                seen.add(parent)
                out.add(_ln(parent))
                frontier.append(parent)
    return out


def _check_tbox_phase(
    cqs: list[dict], tbox_classes: set[str], tbox=None,
) -> list[dict]:
    """CQ 가 T-Box **만으로** 답할 수 있는지 확인한다.

    두 가지를 각각 본다:

    1. **클래스 존재** — CQ 가 지목한 도메인 클래스가 선언돼 있는가.
    2. **경로 존재** — 그 클래스들 **사이를 잇는 ObjectProperty 경로** 가 있는가.

    **2번이 없으면 CQ 는 답할 수 없다.** 클래스만 보는 판정은 2026-08-14 실측에서
    12/12 = 100% 를 보고했지만, 같은 CQ 의 클래스 쌍 102개 중 경로가 있는 것은
    46개(45.1%)뿐이었다 — ``ProductionPlan↔ProductionResult`` (계획 대비 실적),
    ``ChemicalAnalysis↔MechanicalProperties``, ``DimensionalData↔NDTResults`` 등이
    끊겨 있었다. 100% 라는 보고가 그 사실을 가렸다.

    ``status`` 는 **경로 기준** 으로 판정한다 (클래스만 있으면 covered 가 아니다).
    클래스 존재 여부는 ``missing_classes`` 로 계속 노출해 원인을 구분할 수 있게 한다.

    Args:
        cqs: CQ 리스트 (각 CQ에 "id", "domains" 필드 필요).
        tbox_classes: T-Box 클래스 local name 집합 (lowercase).
        tbox: rdflib Graph (T-Box). ``None`` 이면 경로 판정 **불가** 로 처리하고
            클래스 기준으로만 판정한다 (0% 로 내리면 안 된다 — 판정불가와 0건은
            다르다). 이때 ``path_check`` 는 ``"unavailable"``.

    Returns:
        CQ별 dict. 기존 키(``id``/``status``/``missing_classes``/``coverage``)는
        유지하고 경로 필드를 추가한다.
    """
    table_class_map = _load_table_class_map()
    import itertools

    results = []
    for cq in cqs:
        cq_id = cq.get("id", "unknown")
        domains = cq.get("domains", [])
        if not domains:
            results.append({
                "id": cq_id, "status": "covered",
                "missing_classes": [], "coverage": 1.0,
                "path_check": "skipped_no_domains",
                "pairs_total": 0, "pairs_connected": 0, "path_coverage": 1.0,
                "unconnected_pairs": [],
            })
            continue

        resolved = [_resolve_domain_to_class(d, table_class_map) for d in domains]
        missing = [
            d for d, r in zip(domains, resolved, strict=False)
            if r.lower() not in tbox_classes
        ]
        total = len(domains)
        class_coverage = round((total - len(missing)) / total, 2) if total else 1.0

        entry = {
            "id": cq_id,
            "missing_classes": missing,
            "class_coverage": class_coverage,
            # 기존 호출자 호환 — coverage 는 경로 기준으로 갱신된다 (아래).
            "coverage": class_coverage,
        }

        if tbox is None:
            # 판정 불가. 클래스 기준으로만 status 를 낸다.
            entry.update({
                "status": "covered" if not missing else "gap",
                "path_check": "unavailable",
                "pairs_total": 0, "pairs_connected": 0,
                "path_coverage": None, "unconnected_pairs": [],
            })
            results.append(entry)
            continue

        anc_cache: dict[str, set[str]] = {}

        def _anc(name: str, _tbox=tbox, _cache=anc_cache) -> set[str]:
            key = name.lower()
            if key not in _cache:
                _cache[key] = _class_ancestors(_tbox, key)
            return _cache[key]

        pairs = list(itertools.combinations(resolved, 2))
        connected = 0
        unconnected: list[str] = []
        for a, b in pairs:
            # 조상까지 고려해 경로를 찾는다 (상위 클래스에 걸린 OP 도 유효).
            hit = None
            for ca in _anc(a):
                for cb in _anc(b):
                    hit = _find_connecting_path(
                        ca, cb, tbox, max_hops=_CQ_PATH_MAX_HOPS,
                    )
                    if hit:
                        break
                if hit:
                    break
            if hit:
                connected += 1
            elif len(unconnected) < _CQ_GAP_SAMPLE:
                unconnected.append(f"{a}↔{b}")

        path_coverage = round(connected / len(pairs), 2) if pairs else 1.0
        # **경로 기준 판정.** 클래스가 다 있어도 경로가 끊겼으면 gap 이다.
        status = "covered" if (not missing and connected == len(pairs)) else "gap"
        entry.update({
            "status": status,
            "path_check": "ok",
            "pairs_total": len(pairs),
            "pairs_connected": connected,
            "path_coverage": path_coverage,
            "unconnected_pairs": unconnected,
            "coverage": path_coverage,
        })
        results.append(entry)
    return results


def _check_abox_phase(
    cqs: list[dict], abox_stats: dict,
) -> list[dict]:
    """CQ 도메인 클래스가 A-Box에서 인스턴스를 가지는지 확인한다.

    Args:
        cqs: CQ 리스트.
        abox_stats: {"per_class": {"ClassName": {"instance_count": N, ...}, ...}}.

    Returns:
        CQ별 {"id", "status", "zero_instance_classes"} 리스트.
    """
    per_class = abox_stats.get("per_class", {})
    table_class_map = _load_table_class_map()
    results = []
    for cq in cqs:
        cq_id = cq.get("id", "unknown")
        domains = cq.get("domains", [])
        zero_classes = []
        for d in domains:
            # 원본 테이블명 → T-Box 클래스명 (table_class_mapping 우선).
            # 미등록 테이블은 snake_case → PascalCase 로 폴백.
            cls_name = table_class_map.get(d) or _snake_to_class_name(d)
            cls_info = per_class.get(cls_name, {})
            if cls_info.get("instance_count", 0) <= 0:
                zero_classes.append(d)

        status = "covered" if not zero_classes else "gap"
        results.append({
            "id": cq_id,
            "status": status,
            "zero_instance_classes": zero_classes,
        })
    return results


def _pair_connected(
    a: str, b: str, obj_props: dict, graph, anc_cache: dict,
) -> bool:
    """두 클래스가 연결됐는지 — **그래프가 있으면 조상+다중홉** 으로 판정.

    딕셔너리의 1홉 exact/prefix 매칭(``_find_connecting_op``)만 쓰면 상위 클래스에
    걸린 OP 와 중간 클래스를 거치는 경로를 놓친다. ``_check_tbox_phase`` 가 이미
    그 이유로 경로 기반으로 교체됐다 (커밋 8c3c00a) — 같은 판정을 여기에도 쓴다.

    그래프가 없으면 딕셔너리 1홉으로 폴백한다 (판정 불가보다는 낫고, 호출부가
    ``path_check`` 로 어느 방식이었는지 보고한다).
    """
    if graph is None:
        return bool(_find_connecting_op(a, b, obj_props))

    def _anc(name: str) -> set[str]:
        key = name.lower()
        if key not in anc_cache:
            anc_cache[key] = _class_ancestors(graph, key)
        return anc_cache[key]

    for ca in _anc(a):
        for cb in _anc(b):
            if _find_connecting_path(ca, cb, graph, max_hops=_CQ_PATH_MAX_HOPS):
                return True
    return False


def _check_inferred_phase(
    cqs: list[dict], semantic_dict: dict, abox_stats: dict, graph=None,
) -> list[dict]:
    """CQ 도메인 쌍이 **추론 그래프에서** 연결됐는지 확인한다.

    ## 왜 그래프를 보는가 (2026-08-17 실측)

    예전에는 시맨틱 딕셔너리의 ``object_properties`` 에서 **1홉 exact/prefix 매칭**
    (``_find_connecting_op``) 만 봤다. 그래서 상위 클래스에 걸린 OP 와 중간 클래스를
    거치는 경로를 놓쳤다.

    결과가 **모순** 이었다: 추론 그래프는 T-Box 의 상위집합인데도

      tbox     covered  7/12,  쌍 연결 79/102 (77.5%)
      inferred covered  0/12,  쌍 연결 45/102 (44.1%)   ← 더 나쁘게 보고

    추론이 트리플을 71만 건 늘렸는데 커버리지가 **떨어지는** 보고는 측정 결함이다.
    ``_check_tbox_phase`` 가 같은 이유로 경로 기반으로 교체됐고(커밋 8c3c00a),
    이 단계는 그때 함께 고쳐지지 않았다.

    Args:
        cqs: CQ 리스트.
        semantic_dict: 딕셔너리 (graph 가 없을 때 1홉 폴백용).
        abox_stats: A-Box 통계 (현재 미사용, 향후 확장용).
        graph: rdflib Graph (추론 그래프). ``None`` 이면 딕셔너리 1홉 폴백.

    Returns:
        CQ별 {"id", "status", "connected_pairs", "total_pairs", "path_check"}.
    """
    obj_props = semantic_dict.get("object_properties", {})
    table_class_map = _load_table_class_map()
    anc_cache: dict[str, set[str]] = {}
    results = []
    for cq in cqs:
        cq_id = cq.get("id", "unknown")
        domains = cq.get("domains", [])
        cls_names = [
            _resolve_domain_to_class(d, table_class_map) for d in domains
        ]

        if len(cls_names) < 2:
            results.append({
                "id": cq_id, "status": "covered",
                "connected_pairs": 0, "total_pairs": 0,
            })
            continue

        total_pairs = 0
        connected_pairs = 0
        for i in range(len(cls_names)):
            for j in range(i + 1, len(cls_names)):
                total_pairs += 1
                if _pair_connected(
                    cls_names[i], cls_names[j], obj_props, graph, anc_cache,
                ):
                    connected_pairs += 1

        status = "covered" if connected_pairs == total_pairs else "gap"
        results.append({
            "id": cq_id,
            "status": status,
            "connected_pairs": connected_pairs,
            "total_pairs": total_pairs,
            "path_check": "graph" if graph is not None else "dictionary_only",
        })
    return results


def check_cq_coverage(phase: str = "tbox") -> str:
    """파이프라인 단계별 CQ 커버리지를 검증한다.

    T-Box/A-Box/추론 단계에서 CQ가 답변 가능한지 lightweight하게 체크.
    SPARQL, Bedrock, 그래프 로드 없이 순수 dict 조회만 수행한다.

    Args:
        phase: "tbox" | "abox" | "inferred". 검증 대상 파이프라인 단계.
    """
    try:
        if not os.path.exists(_CQ_PATH):
            return error_response(
                "Competency Questions가 없습니다.",
                hint="generate_competency_questions로 먼저 생성하세요.",
                logger=logger,
            )

        with open(_CQ_PATH, encoding="utf-8") as f:
            cqs = json.load(f)
        if not cqs:
            return error_response("CQ 파일이 비어있습니다.", logger=logger)

        if phase == "tbox":
            tbox_classes, _ = _load_tbox_entities()
            # 경로 판정에 T-Box 그래프가 필요하다. 로드 실패는 **판정 불가** 로
            # 넘기고(None) 클래스 기준으로만 본다 — 0% 로 내리면 "경로 없음" 과
            # "측정 못함" 이 구분되지 않는다.
            tbox_graph = None
            try:
                if os.path.exists(TBOX_PATH):
                    tbox_graph = _new_graph()
                    tbox_graph.parse(TBOX_PATH, format="turtle")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "T-Box 로드 실패 — CQ 경로 판정을 건너뛴다 (클래스 기준만): %s",
                    exc,
                )
                tbox_graph = None
            checks = _check_tbox_phase(cqs, tbox_classes, tbox=tbox_graph)
        elif phase == "abox":
            from config import GENERATED_ABOX_DIR
            stats_path = os.path.join(GENERATED_ABOX_DIR, "abox_stats.json")
            if not os.path.exists(stats_path):
                return error_response(
                    "abox_stats.json이 없습니다.",
                    hint="generate_abox로 A-Box를 먼저 생성하세요.",
                    logger=logger,
                )
            with open(stats_path, encoding="utf-8") as f:
                abox_stats = json.load(f)
            checks = _check_abox_phase(cqs, abox_stats)
        elif phase == "inferred":
            if not os.path.exists(SEMANTIC_DICT_PATH):
                return error_response(
                    "semantic_dictionary.json이 없습니다.",
                    hint="generate_semantic_dictionary로 먼저 생성하세요.",
                    logger=logger,
                )
            with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
                sem_dict = json.load(f)
            from config import GENERATED_ABOX_DIR
            stats_path = os.path.join(GENERATED_ABOX_DIR, "abox_stats.json")
            if os.path.exists(stats_path):
                with open(stats_path, encoding="utf-8") as f:
                    abox_stats = json.load(f)
            else:
                abox_stats = {"per_class": {}}
            # 경로 판정용 스키마 그래프. 추론 그래프는 수백만 트리플이라 이 경량
            # 도구가 로드하기엔 과하고, 클래스 계층·OP domain/range 는 T-Box 에
            # 전부 있으므로 T-Box 로 판정한다 (추론이 스키마 축을 늘리지 않는다).
            # 로드 실패는 **판정 불가** 로 폴백 — 0% 로 내리면 "연결 없음" 과
            # "측정 못함" 이 구분되지 않는다.
            infer_graph = None
            try:
                if os.path.exists(TBOX_PATH):
                    infer_graph = _new_graph()
                    infer_graph.parse(TBOX_PATH, format="turtle")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "T-Box 로드 실패 — CQ 경로 판정을 딕셔너리 1홉으로 폴백: %s", exc,
                )
                infer_graph = None
            checks = _check_inferred_phase(
                cqs, sem_dict, abox_stats, graph=infer_graph,
            )
        else:
            return error_response(
                f"지원하지 않는 phase: '{phase}'",
                hint="phase는 'tbox', 'abox', 'inferred' 중 하나여야 합니다.",
                logger=logger,
            )

        total = len(checks)
        covered = sum(1 for c in checks if c["status"] == "covered")
        gap = total - covered
        coverage_rate = round(covered / max(total, 1) * 100, 1)
        gaps = [c for c in checks if c["status"] == "gap"]

        summary: dict = {
            "total": total,
            "covered": covered,
            "gap": gap,
            "coverage_rate": coverage_rate,
        }

        # 경로 기준 집계 (tbox phase 에서만 산출). CQ 단위 covered/gap 은 "전부
        # 이어졌는가" 라는 all-or-nothing 이라, 어느 정도 이어졌는지는 쌍 단위로
        # 봐야 한다 — 개선을 추적할 수 있는 연속 지표가 필요하다.
        pair_total = sum(c.get("pairs_total", 0) for c in checks)
        if pair_total:
            pair_connected = sum(c.get("pairs_connected", 0) for c in checks)
            summary["class_pairs"] = {
                "total": pair_total,
                "connected": pair_connected,
                "unconnected": pair_total - pair_connected,
                "path_coverage_rate": round(pair_connected / pair_total * 100, 1),
                "max_hops": _CQ_PATH_MAX_HOPS,
            }
        if any(c.get("path_check") == "unavailable" for c in checks):
            summary["path_check"] = "unavailable"
            summary["caveat"] = (
                "T-Box 를 로드하지 못해 경로 판정을 건너뛰었다 — coverage_rate 는 "
                "클래스 존재 기준이며 CQ 응답 가능성을 과대평가한다"
            )

        return json.dumps({
            "success": True,
            "phase": phase,
            "summary": summary,
            "gaps": gaps,
            "details": checks,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)


def validate_competency_questions(graph_source: str = "merge") -> str:
    """Competency Questions가 KG에서 답변 가능한지 프로그래매틱으로 검증한다.

    LLM 호출 없이, CQ의 도메인 클래스를 분석하여:
    1. 각 클래스에 인스턴스가 존재하는가? (instance_check)
    2. 클래스 간 연결 경로가 존재하는가? — 직접/멀티홉/간접 (connectivity_check)
    3. 핵심 DatatypeProperty에 값이 채워져 있는가? (value_check)
    + CQ 커버리지: 전체 클래스 중 CQ가 테스트하는 비율

    로컬 이름 형식이 아닌 이름은 질의에 넣지 않고 실패 체크로 남긴다. 실행하는
    질의는 PREFIX 를 붙인 최종 문자열에 egress 가드를 적용한 뒤 실행한다.

    예상 소요시간: ~5초 (Bedrock 호출 없음)

    Args:
        graph_source: "merge" 또는 "inferred".
    """
    try:
        if not os.path.exists(_CQ_PATH):
            return error_response(
                "Competency Questions가 없습니다.",
                hint="generate_competency_questions로 먼저 생성하세요.",
                logger=logger,
            )
        with open(_CQ_PATH, encoding="utf-8") as f:
            cqs = json.load(f)
        if not cqs:
            return error_response("CQ 파일이 비어있습니다.", logger=logger)

        # 시맨틱 딕셔너리에서 OP 로드
        if os.path.exists(SEMANTIC_DICT_PATH):
            with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
                sem_dict = json.load(f)
        else:
            sem_dict = {}
        obj_props = sem_dict.get("object_properties", {})

        start_all = time.monotonic()

        from tools.sparql_local import _get_graph
        graph, load_msg = _get_graph(graph_source)

        # T-Box 엔티티 로드 (answerability 분석용)
        tbox_classes, tbox_properties = _load_tbox_entities()

        # T-Box 그래프 로드 (BFS 연결성 탐색용)
        tbox_graph = None
        if os.path.exists(TBOX_PATH):
            from rdflib import Graph as RdfGraph
            try:
                tbox_graph = RdfGraph()
                tbox_graph.parse(TBOX_PATH, format="turtle")
            except Exception:
                tbox_graph = None

        results = []
        answerable = 0
        not_answerable = 0
        answerability_per_cq: list[dict] = []
        cq_type_counter: Counter = Counter()

        for cq in cqs:
            cq_id = cq.get("id", "unknown")
            question = cq.get("question_ko", cq.get("question_en", ""))

            queries = _build_connectivity_queries(cq, obj_props, sem_dict, tbox=tbox_graph)

            checks = []
            all_passed = True
            for q_info in queries:
                if q_info.get("sparql") is None:
                    # 보간하지 않은 이름. 질의 없이 실패 체크로 남긴다.
                    checks.append({
                        "check": q_info["description"],
                        "passed": False,
                        "error": q_info.get("error", ""),
                    })
                    all_passed = False
                    continue
                full_sparql = prepend_prefixes(q_info["sparql"])
                try:
                    # 실행할 최종 문자열 그대로 가드한다. 거부되면 아래 except 가
                    # 실패 체크로 기록하고 질의는 실행하지 않는다.
                    reject_sparql_egress(full_sparql)
                    result = graph.query(full_sparql)
                    rows = list(result)
                    if q_info["type"] == "instance_check" or q_info["type"] == "value_check":
                        count = int(rows[0][0]) if rows else 0
                        passed = count > 0
                        checks.append({
                            "check": q_info["description"],
                            "passed": passed,
                            "count": count,
                        })
                    else:
                        passed = len(rows) > 0
                        checks.append({
                            "check": q_info["description"],
                            "passed": passed,
                            "connected": passed,
                        })
                    if not passed:
                        all_passed = False
                except Exception as e:
                    checks.append({
                        "check": q_info["description"],
                        "passed": False,
                        "error": str(e)[:100],
                    })
                    all_passed = False

            status = "ANSWERABLE" if all_passed else "NOT_ANSWERABLE"
            if all_passed:
                answerable += 1
            else:
                not_answerable += 1

            failed_checks = [c for c in checks if not c["passed"]]

            # ── CQ Type Classification (Wisniewski et al. 2019) ──
            cq_type = _classify_cq_type(question)
            cq_type_counter[cq_type] += 1

            # ── Partial Answerability (Bezerra et al. 2013 CQChecker) ──
            cq_answer = _assess_cq_answerability(cq, tbox_classes, tbox_properties)

            results.append({
                "id": cq_id,
                "question": question,
                "difficulty": cq.get("difficulty", ""),
                "domains": cq.get("domains", []),
                "status": status,
                "checks_total": len(checks),
                "checks_passed": len(checks) - len(failed_checks),
                "failed_checks": failed_checks if failed_checks else None,
                "reason": (
                    None if all_passed
                    else f"실패: {', '.join(c['check'] for c in failed_checks)}"
                ),
            })

            answerability_per_cq.append({
                "id": cq_id,
                "cq_type": cq_type,
                "answerability": cq_answer["level"],
                "matched_classes": cq_answer["matched_classes"],
                "missing_classes": cq_answer["missing_classes"],
            })

        duration = round(time.monotonic() - start_all, 1)
        total = len(cqs)
        answer_rate = round(answerable / max(total, 1) * 100, 1)

        # CQ 커버리지: CQ가 테스트하는 클래스 vs 전체 클래스
        cq_classes = set()
        for cq in cqs:
            for d in cq.get("domains", []):
                cq_classes.add(d.replace("_", ""))
        all_classes = set(sem_dict.get("classes", {}).keys())
        covered = cq_classes & all_classes if all_classes else cq_classes
        uncovered = sorted(all_classes - cq_classes) if all_classes else []

        # ── Answerability 집계 (Bezerra et al. 2013) ──
        answerability_dist = Counter(a["answerability"] for a in answerability_per_cq)
        for level in ("fully_answerable", "partially_answerable", "unanswerable"):
            answerability_dist.setdefault(level, 0)

        # ── CQ Type 분포 (Wisniewski et al. 2019) ──
        for cq_type in ("count", "list", "boolean", "comparison", "aggregation", "temporal", "causal"):
            cq_type_counter.setdefault(cq_type, 0)

        return json.dumps({
            "success": True,
            "method": "programmatic_connectivity_check",
            "summary": {
                "total": total,
                "answerable": answerable,
                "not_answerable": not_answerable,
                "answer_rate": answer_rate,
                "duration_seconds": duration,
                "graph_source": graph_source,
                "graph_info": load_msg,
                "bedrock_calls": 0,
            },
            "coverage": {
                "cq_classes": len(covered),
                "total_classes": len(all_classes),
                "coverage_rate": round(len(covered) / max(len(all_classes), 1) * 100, 1),
                "uncovered_classes": uncovered[:20],
            },
            "answerability": {
                "per_cq": answerability_per_cq,
                "distribution": dict(answerability_dist),
                "tbox_classes_available": len(tbox_classes),
                "tbox_properties_available": len(tbox_properties),
            },
            "cq_type_distribution": dict(cq_type_counter),
            "results": results,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return error_response(e, logger=logger)
