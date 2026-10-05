from __future__ import annotations

import json
import logging
import os
import re

from botocore.config import Config
from rdflib import OWL

from config import (
    BEDROCK_MAX_RETRIES,
    BEDROCK_MAX_TOKENS,
    BEDROCK_MODEL_ID,
    BEDROCK_READ_TIMEOUT,
    BEDROCK_REGION,
    LPG_SEMANTIC_DICT_PATH,
    SEMANTIC_DICT_PATH,
    TBOX_PATH,
    get_boto3_session,
)
from domain.namespaces import (
    DOMAIN_CONFIG,
    DOMAIN_INST_NS,
    DOMAIN_NS,
    NS_INST_PREFIX,
    NS_PREFIX,
)
from domain.rules_paths import rules_path
from domain.tbox_utils import _new_graph
from tools.common import cached_client, error_response

logger = logging.getLogger(__name__)

_domain = DOMAIN_CONFIG["domain"]
_ns_cfg = DOMAIN_CONFIG["namespace"]

# 센서 변환 설정: rules/domain/domain_config.json의 "sensor" 키에서 로드, 없으면 기본값 사용
_SENSOR_CONFIG_PATH = rules_path("domain_config.json")

def _load_sensor_config() -> dict:
    """센서 관련 설정을 domain_config.json에서 로드한다. 없으면 기본값 반환."""
    defaults = {
        "class_keywords": ["alarm", "event", "equipment", "tag", "realtime", "sensor", "monitor"],
        "properties": ["hasEquipmentID", "hasMeasurement", "hasSeverity", "hasEventType", "hasTag", "hasAlarm"],
        "quality_codes": {"0": "Good", "1": "Drift", "2": "Fault"},
        "severities": ["critical", "warning", "high"],
    }
    try:
        with open(_SENSOR_CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("sensor", defaults)
    except Exception:
        return defaults

_SENSOR_CFG = _load_sensor_config()


def _load_domain_classes() -> list[str]:
    """도메인 클래스 목록을 동적으로 로드한다.

    1순위: 시맨틱 딕셔너리 JSON (classes 키)
    2순위: T-Box TTL 파싱 (owl:Class 추출)
    """
    # 1. 시맨틱 딕셔너리에서 로드
    if os.path.exists(SEMANTIC_DICT_PATH):
        try:
            with open(SEMANTIC_DICT_PATH, encoding="utf-8") as f:
                sd = json.load(f)
            classes = list(sd.get("classes", {}).keys())
            if classes:
                return classes
        except Exception:
            logger.debug("시맨틱 딕셔너리 로드 실패, T-Box 폴백")

    # 2. T-Box TTL에서 owl:Class 추출
    if os.path.exists(TBOX_PATH):
        try:
            g = _new_graph()
            g.parse(TBOX_PATH, format="turtle")
            classes = []
            for s in g.subjects(predicate=None, object=OWL.Class):
                uri = str(s)
                if uri.startswith(DOMAIN_NS):
                    classes.append(uri.replace(DOMAIN_NS, ""))
            if classes:
                return sorted(classes)
        except Exception:
            logger.debug("T-Box 파싱 실패")

    return []


@cached_client
def _bedrock_client():
    session = get_boto3_session()
    config = Config(read_timeout=BEDROCK_READ_TIMEOUT, connect_timeout=30)
    return session.client("bedrock-runtime", region_name=BEDROCK_REGION, config=config)


_RETRYABLE_CODES = frozenset({
    "ThrottlingException", "ServiceUnavailableException",
    "InternalServerException", "ModelNotReadyException",
    "TooManyRequestsException",
})


def is_infra_fault(exc: BaseException) -> bool:
    """예외가 **인프라 장애** (재시도 대상) 인가 — 코드 버그와 구분한다.

    호출자가 "클라우드가 잠깐 안 된 것" 과 "우리 코드가 틀린 것" 을 갈라야 할 때
    쓴다. 장시간 잡(S2 는 50분)은 인프라 장애로 진행분을 버리면 안 되지만,
    ``KeyError`` 같은 계약 위반을 같이 삼키면 버그가 영구히 숨는다.

    ``invoke_bedrock_with_metadata`` 의 재시도 판정과 **같은 집합** 이어야 하므로
    모듈 레벨로 승격했다 (예전엔 함수 내부 중첩 함수라 재사용이 불가능했고,
    호출부가 문자열 매칭 사본을 만들 수밖에 없었다).

    ``MemoryError`` 는 ``Exception`` 하위라 조용히 삼켜질 수 있는데, S2 는 peak
    RSS 최대 단계이므로 **인프라 장애로 보지 않는다** — 반쯤 만들어진 그래프를
    정상 산출물로 저장하면 안 된다.
    """
    try:
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except Exception:  # pragma: no cover — botocore 미설치(로컬 테스트) 환경
        ClientError = BotoCoreError = ()  # type: ignore

    if isinstance(exc, MemoryError):
        return False
    if ClientError and isinstance(exc, ClientError):
        code = ""
        if hasattr(exc, "response"):
            code = exc.response.get("Error", {}).get("Code", "")
        if code in _RETRYABLE_CODES:
            return True
    if BotoCoreError and isinstance(exc, BotoCoreError):
        return True
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    if type(exc).__name__ in _RETRYABLE_CODES:
        return True
    # bedrock.py 가 재시도 예산 소진 시 내는 문구 (동적 예외 호환 폴백).
    return isinstance(exc, RuntimeError) and "최대 재시도 초과" in str(exc)


def _extract_text(resp_body: dict, model_id: str) -> str:
    """응답 content 배열에서 **text 블록만** 순서대로 이어붙여 반환한다.

    예전에는 ``resp_body["content"][0]["text"]`` 로 **첫 블록이 텍스트라고 가정**
    했다. adaptive thinking 이 기본 ON 인 모델(claude-opus-5 / claude-sonnet-5)은
    첫 블록이 ``{"type": "thinking"}`` 이라, 그 가정이 ``KeyError: 'text'`` 로
    깨진다. 2026-08-18 실측: ``us.anthropic.claude-opus-5`` 는 blocks=
    ``['thinking', 'text']`` 을 반환하고 Jury 호출이 즉사했다. ``thinking``
    ``{"type": "disabled"}`` 를 보내면 회피되지만, 그러면 새 모델을 쓰는 이유가
    사라지고 tool call 이 평문 텍스트로 새는 별개 결함을 부른다.

    **첫 text 블록만** 집는 대신 전부 이어붙이는 이유: 블록이 쪼개졌을 때
    앞부분만 취하면 조용한 절단이 된다 (이 리포에서 반복된 실패 유형 —
    ``_format_ttl_block`` 절단이 리뷰어에게 "선언 없음" 을 단정하게 만든 사례).
    기존 모델은 text 블록이 1개라 결과가 동일하다 (동작 보존).

    text 블록이 하나도 없으면 **빈 문자열을 반환하지 않고 raise 한다**: 호출부는
    이 값을 JSON/TTL 로 파싱하므로, 빈 문자열은 "모델이 아무 말도 안 했다" 와
    "우리가 파싱을 못 했다" 를 구분 불가하게 만든다. ``stop_reason`` 을 메시지에
    넣어 refusal(안전 분류기 거부, content 가 빈 배열) 을 식별 가능하게 한다.
    """
    blocks = resp_body.get("content") or []
    # 판정을 **"명시적으로 non-text 인 블록만 건너뛴다"** 로 좁힌다. ``type`` 이
    # 없는 블록은 text 로 간주한다 — 예전 ``content[0]["text"]`` 는 type 을 아예
    # 보지 않았으므로, type 을 요구하면 동작 보존이 깨진다 (실측: 그 엄격한 형태가
    # 기존 테스트 2건을 깼다). thinking 블록은 본문을 ``thinking`` 키에 담고
    # ``type`` 을 항상 명시하므로 이 관용으로 새어 들어오지 않는다.
    texts = [b["text"] for b in blocks
             if isinstance(b, dict) and "text" in b
             and b.get("type", "text") == "text"]
    if texts:
        skipped = [b.get("type") for b in blocks
                   if isinstance(b, dict) and b.get("type", "text") != "text"]
        if skipped:
            # 정상 경로다 (thinking 모델). warning 은 라운드마다 스팸이 되므로 debug.
            logger.debug(
                "bedrock[%s] 비-text 블록 %s 건너뜀, text 블록 %d개 결합",
                model_id, skipped, len(texts),
            )
        return "".join(texts)

    stop_reason = resp_body.get("stop_reason", "?")
    block_types = [b.get("type") for b in blocks if isinstance(b, dict)]

    # ``max_tokens`` 절단은 **복구 가능한** 상태다: thinking 이 예산을 다 써서 text
    # 를 못 낳았을 뿐이므로, 호출부의 자동 확장(max_tokens 1.5배 재호출)이 답이다.
    # 여기서 raise 하면 그 경로에 **도달하지 못한다** — 2026-08-18 실측: S2 Round 2
    # SME(sonnet-5) 가 blocks=['thinking'] + stop_reason=max_tokens 로 죽어 9분치
    # 진행이 버려졌다. thinking 길이는 실행마다 변동해 같은 요청이 5/5 성공하기도
    # 하므로(확률적), 재시도 경로를 살리는 것이 유일하게 신뢰할 수 있는 대응이다.
    #
    # 빈 문자열이 조용한 실패로 새지 않는 근거: 호출부는 ``stop_reason`` 을 함께
    # 받아 ``max_tokens`` 면 재호출하고, 재시도 후에도 비면 JSON 파싱이 실패해
    # 각 역할의 파서가 명시적으로 보고한다.
    if stop_reason == "max_tokens":
        logger.warning(
            "bedrock[%s] max_tokens 절단으로 text 블록 없음 (blocks=%s) — "
            "빈 문자열 반환, 호출부의 max_tokens 확장 재시도에 맡긴다",
            model_id, block_types,
        )
        return ""

    raise RuntimeError(
        f"Bedrock 응답에 text 블록이 없다 (model={model_id}, "
        f"stop_reason={stop_reason}, blocks={block_types}). "
        "stop_reason=refusal 이면 안전 분류기가 요청을 거부한 것이고, "
        "그 외라면 응답 계약이 바뀐 것이다."
    )


def invoke_bedrock_with_metadata(
    prompt: str,
    max_tokens: int = BEDROCK_MAX_TOKENS,
    max_retries: int = BEDROCK_MAX_RETRIES,
    temperature: float | None = None,
    cached_prefix: str | None = None,
    model_id: str | None = None,
) -> dict:
    """Bedrock Claude 호출. text + stop_reason + usage dict 반환. 재시도 포함.

    Args:
        prompt: 변수 구간(라운드마다 다름). cached_prefix가 주어진 경우 이 텍스트는
                캐시되지 않는다.
        max_tokens: 최대 출력 토큰 수.
        max_retries: ThrottlingException 시 재시도 횟수.
        temperature: 생성 온도 (None이면 모델 기본값, 0이면 결정적).
        cached_prefix: 재사용 가능한 공통 prefix(skeleton, CQ, 도메인 스펙).
                       content 배열의 앞 블록에 cache_control=ephemeral 으로 삽입되어
                       5분 캐시 TTL 내 재호출 시 prefix 입력 토큰이 재청구되지 않는다.
                       None이면 기존 동작(프롬프트 전체를 단일 text 블록).
        model_id: 호출할 Bedrock 모델 ID. None 이면 BEDROCK_MODEL_ID (기본) 사용.
                  X1 (Multi-Agent 모델 다양화): 역할별 다른 모델 호출 시 명시.

    Returns:
        {"text", "stop_reason", "usage"} dict. usage는 input_tokens / output_tokens /
        cache_read_input_tokens / cache_creation_input_tokens 을 포함 (Bedrock이
        반환한 필드만).
    """
    import time
    client = _bedrock_client()
    # content 구성: cached_prefix 있으면 [cached_block, variable_block], 없으면 단일 text.
    if cached_prefix:
        content_blocks: list[dict] = [
            {"type": "text", "text": cached_prefix,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": prompt},
        ]
    else:
        # 하위 호환: 기존 문자열 형식 유지 (cache 미사용).
        content_blocks = prompt  # type: ignore[assignment]

    body_dict: dict = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content_blocks}],
    }
    if temperature is not None:
        body_dict["temperature"] = temperature

    # 재시도 판정은 모듈 레벨 is_infra_fault 를 쓴다 — 호출부(S2 라운드 핸들러 등)
    # 와 **같은 집합** 이어야 하고, 사본이 갈라지면 한쪽만 재시도하는 불일치가 난다.
    _is_retryable = is_infra_fault

    resolved_model_id = model_id or BEDROCK_MODEL_ID
    attempt = 0
    while attempt <= max_retries:
        try:
            response = client.invoke_model(
                modelId=resolved_model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body_dict),
            )
            resp_body = json.loads(response["body"].read())
            usage = resp_body.get("usage", {}) or {}
            return {
                "text": _extract_text(resp_body, resolved_model_id),
                "stop_reason": resp_body.get("stop_reason", "end_turn"),
                "usage": {
                    "input_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                },
            }
        except Exception as exc:
            # 신형 모델(opus-4-7/sonnet-4-6 등)은 temperature 파라미터를 거부한다
            # ("temperature is deprecated for this model"). 해당 파라미터만 제거하고
            # 즉시 재시도해 모델 ID 하드코딩 없이 자동 대응한다. 결정성은 모델 기본값.
            if "temperature" in body_dict and "temperature" in str(exc).lower():
                body_dict.pop("temperature", None)
                logger.warning(
                    "모델 %s 가 temperature 미지원 — 파라미터 제거 후 재시도",
                    resolved_model_id,
                )
                # 재시도 예산을 소비하지 않고 즉시 재호출 (1회성 파라미터 정정).
                continue
            if _is_retryable(exc) and attempt < max_retries:
                time.sleep(2 ** (attempt + 1))
                attempt += 1
                continue
            raise
    raise RuntimeError("Bedrock 호출 실패: 최대 재시도 초과")


def invoke_bedrock_text(
    prompt: str,
    max_tokens: int = 4096,
    temperature: float | None = 0,
    max_retries: int = BEDROCK_MAX_RETRIES,
    cached_prefix: str | None = None,
    model_id: str | None = None,
) -> str:
    """Bedrock Claude를 호출하여 텍스트만 반환한다. temperature=0 기본(결정적).

    cached_prefix: 재사용 가능한 공통 prefix. 5분 내 재호출 시 prefix 입력 토큰을
    재청구하지 않는 Anthropic prompt caching을 활성화한다.
    model_id: None 이면 BEDROCK_MODEL_ID 사용 (X1 Multi-Agent 모델 다양화 지원).
    """
    result = invoke_bedrock_with_metadata(
        prompt, max_tokens=max_tokens, max_retries=max_retries,
        temperature=temperature, cached_prefix=cached_prefix,
        model_id=model_id,
    )
    return result["text"]


def _invoke(prompt: str, max_tokens: int = 4096) -> str:
    result = invoke_bedrock_with_metadata(prompt, max_tokens=max_tokens)
    text = result["text"]
    if result["stop_reason"] == "max_tokens":
        text += "\n\n⚠️ WARNING: Bedrock 응답이 max_tokens에 도달하여 잘렸을 수 있습니다. 출력을 검증하세요."
    return text


def convert_sensor_to_rdf(sensor_event_json: str) -> str:
    """센서 이벤트 JSON을 RDF(Turtle) 형식으로 변환한다.

    Args:
        sensor_event_json: EventBridge 센서 이상 감지 이벤트 JSON 문자열.
    """
    try:
        classes = _load_domain_classes()
        keywords = _SENSOR_CFG.get("class_keywords",
            ["alarm", "event", "equipment", "tag", "realtime", "sensor", "monitor"])
        sensor_classes = [c for c in classes if any(
            kw in c.lower() for kw in keywords
        )]
        if not sensor_classes:
            sensor_classes = classes[:10]
        class_list = ", ".join(sensor_classes)

        props = _SENSOR_CFG.get("properties",
            ["hasEquipmentID", "hasMeasurement", "hasSeverity", "hasEventType", "hasTag", "hasAlarm"])
        prop_list = ", ".join(props)

        qcodes = _SENSOR_CFG.get("quality_codes", {"0": "Good", "1": "Drift", "2": "Fault"})
        qcode_str = ", ".join(f"{k}={v}" for k, v in qcodes.items())

        severities = _SENSOR_CFG.get("severities", ["critical", "warning", "high"])
        severity_str = ", ".join(severities)

        prompt = f"""다음 센서 이벤트 JSON을 RDF Turtle 형식으로 변환하세요.

사용할 네임스페이스:
- @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
- @prefix {NS_INST_PREFIX}: <{DOMAIN_INST_NS}> .

관련 클래스: {class_list}
관련 프로퍼티: {prop_list}

Quality Code: {qcode_str}
Severity: {severity_str}

입력 JSON:
{sensor_event_json}

TTL만 출력하세요 (설명 없이):"""

        return _invoke(prompt, max_tokens=4096)
    except Exception as e:
        return error_response(e, hint=_bedrock_error_hint(e), logger=logger)


def _load_dict(path: str) -> dict | None:
    """JSON 딕셔너리를 로드한다. 없거나 파싱 실패 시 None."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.debug("딕셔너리 로드 실패: %s", path)
    return None


def _build_cypher_schema_context(sd: dict) -> str:
    """시맨틱 딕셔너리에서 Cypher 생성에 필요한 스키마 컨텍스트를 구성한다."""
    lines: list[str] = []

    # 클래스별 요약 (class_quick_reference)
    cqr = sd.get("class_quick_reference", {})
    if cqr:
        lines.append("## Neo4j 노드 라벨 (=OWL 클래스) + 프로퍼티 + 관계")
        for cls, summary in cqr.items():
            if summary:
                lines.append(f"  :{cls} — {summary}")

    # ObjectProperty 요약
    ops = sd.get("object_properties", {})
    if ops:
        lines.append("\n## 관계 타입 (=ObjectProperty) → 방향")
        for prop, info in ops.items():
            domain = info.get("domain", "?")
            range_ = info.get("range", "?")
            label = info.get("label_ko", "")
            inv = info.get("inverseOf", "")
            line = f"  -[:{prop}]-> ({domain} → {range_})"
            if label:
                line += f"  # {label}"
            if inv:
                line += f"  (inverse: {inv})"
            lines.append(line)

    # [I5] IOF 네임스페이스 → Neo4j 관계 타입 prefix 매핑
    lines.append("\n## 중요: IOF 관계 타입은 Neo4j에서 prefix가 붙음")
    lines.append("  - iof-core 관계: iof_ prefix (예: hasQuality → iof_hasQuality)")
    lines.append("  - iof-sc 관계: iof_sc_ prefix (예: hasSupplier → iof_sc_hasSupplier)")
    lines.append("  - iof-maint 관계: iof_maint_ prefix")
    lines.append("  - rdfs 관계: rdfs_ prefix (예: subClassOf → rdfs_subClassOf)")
    lines.append("  - 도메인(steel:) 관계: prefix 없음 (예: hasMaintenanceHistory)")

    # 추론 전용 클래스
    sparql_guide = sd.get("sparql_guide", {})
    inf_classes = sparql_guide.get("engine_compatibility", {}).get("inference_only_classes", {})
    if inf_classes.get("classes"):
        lines.append("\n## 추론 전용 라벨 (all_inferred.ttl 임포트 시 사용 가능)")
        for cls, info in inf_classes["classes"].items():
            lines.append(f"  :{cls} — 부모: {info.get('parent', '?')}, 조건: {info.get('filter', '?')}")

    return "\n".join(lines)


def _build_cypher_schema_context_lpg(sd: dict) -> str:
    """LPG 시맨틱 딕셔너리에서 Cypher 생성용 스키마 컨텍스트를 구성한다."""
    lines: list[str] = []
    labels = sd.get("labels", {})
    qr = sd.get("label_quick_reference", {})

    # ── 주요 라벨 (core tier) 상세 + 측정 라벨 (data tier) 요약 ──
    core = {lb: info for lb, info in labels.items() if info.get("tier") == "core"}
    data = {lb: info for lb, info in labels.items() if info.get("tier") == "data"}

    if core:
        lines.append("## 주요 노드 라벨 (쿼리 대상)")
        for lb, info in core.items():
            summary = qr.get(lb, "")
            if summary:
                lines.append(f"  :{lb} [{info['count']:,}건] — {summary}")
    if data:
        lines.append("\n## 측정/센서 라벨 (필터 대상)")
        lines.append(
            "  " + ", ".join(f":{lb}({info['count']:,})" for lb, info in data.items())
        )

    # ── 라벨 공존 (멀티라벨 노드, 상위 30개만) ──
    cooc_lines: list[str] = []
    for lb, info in core.items():
        cooc = info.get("cooccurs_with", [])
        if cooc and len(cooc_lines) < 30:
            cooc_lines.append(f"  :{lb} 노드 = :{' + :'.join(cooc[:5])}")
    if cooc_lines:
        lines.append("\n## 라벨 공존 (같은 노드에 동시 존재 → 어느 라벨로든 MATCH 가능)")
        lines.extend(cooc_lines)

    # ── 관계 타입 (역방향 그룹핑) ──
    rels = sd.get("relationship_types", {})
    shown_rels: set[str] = set()
    if rels:
        lines.append("\n## 관계 타입 (방향, 건수)")
        for rtype, info in rels.items():
            if rtype in shown_rels:
                continue
            src = info.get("source_labels", ["?"])
            tgt = info.get("target_labels", ["?"])
            cnt = info.get("count", 0)
            inv = info.get("inverse", "")
            line = f"  (:{src[0]})-[:{rtype}]->(:{tgt[0]}) [{cnt:,}건]"
            if inv:
                line += f"  (역: {inv})"
                shown_rels.add(inv)
            shown_rels.add(rtype)
            lines.append(line)

    # ── 주요 프로퍼티 예시값 (core 라벨, coverage ≥ 80%, 라벨당 3개, 전체 50개) ──
    example_lines: list[str] = []
    for lb, info in core.items():
        lb_count = 0
        for pname, pinfo in sorted(
            info.get("properties", {}).items(),
            key=lambda x: -x[1].get("coverage", 0),
        ):
            if lb_count >= 3 or len(example_lines) >= 50:
                break
            samples = pinfo.get("distinct_samples", [])
            if samples and pinfo.get("coverage", 0) >= 80:
                example_lines.append(
                    f"  {lb}.{pname}: {', '.join(repr(s) for s in samples[:3])}"
                )
                lb_count += 1
    if example_lines:
        lines.append("\n## 주요 프로퍼티 예시값 (WHERE 필터 참조)")
        lines.extend(example_lines)

    # ── 2홉 경로 (core 라벨 관련만, 최대 50개) ──
    two_hop = sd.get("two_hop_paths", [])
    if two_hop:
        core_names = set(core.keys())
        filtered = [
            p for p in two_hop
            if any(f":{lb})" in p or f":{lb})-" in p for lb in core_names)
        ][:50]
        if filtered:
            lines.append("\n## 주요 2홉 경로 (직접 관계가 없을 때 이 경로를 사용)")
            for path in filtered:
                lines.append(f"  {path}")

    return "\n".join(lines)


def _bedrock_error_hint(e: Exception) -> str:
    error_str = str(e)
    if "timeout" in error_str.lower() or "ReadTimeoutError" in error_str:
        return "Bedrock 응답 타임아웃. BEDROCK_READ_TIMEOUT 값을 늘리거나 max_tokens를 줄여보세요."
    return "Bedrock 호출 실패. 모델 ID, AWS 자격증명, 리전 설정을 확인하세요."


def ask_neo4j(question: str, execute: bool = False) -> str:
    """자연어 질문을 Cypher 쿼리로 변환한다. 시맨틱 딕셔너리를 참조하여 정확한 라벨/프로퍼티/관계를 사용.

    Args:
        question: 자연어 질문 (예: "Manufacturer_G 설비 중 고장 3회 이상인 것은?")
        execute: True면 생성된 Cypher를 neo4j_query로 즉시 실행. False면 Cypher만 반환.
    """
    try:
        # LPG 딕셔너리 우선 사용 → 없으면 RDF 딕셔너리 폴백
        lpg_sd = _load_dict(LPG_SEMANTIC_DICT_PATH)
        if lpg_sd:
            schema_context = _build_cypher_schema_context_lpg(lpg_sd)
            domain_name = lpg_sd.get("metadata", {}).get("domain_ko", "")
            rules = """## Cypher 작성 규칙
1. 노드 라벨은 반드시 위 스키마의 라벨명을 사용
2. 프로퍼티명은 반드시 위 스키마의 프로퍼티명을 사용 (camelCase)
3. 관계 타입은 반드시 위 스키마의 관계 타입명을 사용 (역방향은 inverse 참조)
4. 관계 방향(→)을 반드시 스키마와 일치시킬 것
5. 값 필터 시 예시값에 있는 정확한 값을 사용
6. 직접 관계가 없으면 "2홉 경로" 섹션의 경로를 참조
7. "라벨 공존" 섹션에 표시된 라벨은 같은 노드에 존재하므로 어느 라벨로든 MATCH 가능
8. 집계 시 WITH + WHERE 패턴 사용"""
        else:
            return error_response(
                "LPG 시맨틱 딕셔너리가 없습니다.",
                hint="convert_rdf_to_lpg → generate_lpg_semantic_dictionary를 먼저 실행하세요. "
                     "RDF 딕셔너리는 Cypher 생성에 부적합합니다 (네임스페이스 불일치).",
            )

        prompt = f"""당신은 {domain_name} Knowledge Graph의 Cypher 쿼리 전문가입니다.

아래 Neo4j 스키마를 참조하여 사용자 질문에 맞는 Cypher 쿼리를 생성하세요.

{schema_context}

{rules}

## 출력 형식
<cypher>
쿼리
</cypher>

<explanation>
쿼리 설명 (한국어, 2-3문장)
</explanation>

질문: {question}"""

        response = invoke_bedrock_text(prompt, max_tokens=4096, temperature=0)

        # Cypher 추출: <cypher> 태그 우선, 마크다운 코드블록 폴백
        cypher_match = re.search(r"<cypher>\s*([\s\S]*?)\s*</cypher>", response)
        if not cypher_match:
            cypher_match = re.search(r"```cypher\s*([\s\S]*?)\s*```", response)
        explanation_match = re.search(r"<explanation>\s*([\s\S]*?)\s*</explanation>", response)

        cypher = cypher_match.group(1).strip() if cypher_match else ""
        explanation = explanation_match.group(1).strip() if explanation_match else ""

        # [I3] LLM이 태그 형식을 따르지 않은 경우
        if not cypher:
            return json.dumps({
                "success": True,
                "cypher": "",
                "explanation": "LLM이 Cypher를 생성하지 못했습니다.",
                "raw_response": response[:500],
            }, ensure_ascii=False, indent=2)

        # Hallucination Detection: Cypher 내 라벨/관계/프로퍼티가 스키마에 존재하는지 검증
        hallucination_warnings = []
        if lpg_sd:
            _known_labels = set(lpg_sd.get("labels", {}).keys())
            _known_rels = set(lpg_sd.get("relationship_types", {}).keys())
            _known_props = set()
            for lb_info in lpg_sd.get("labels", {}).values():
                _known_props.update(lb_info.get("properties", {}).keys())

            # Cypher에서 :Label 패턴 추출
            _cypher_labels = set(re.findall(r":\s*([A-Z][a-zA-Z0-9_]*)", cypher))
            for lb in _cypher_labels:
                if lb not in _known_labels and lb not in ("LPGNode", "OWLClass"):
                    hallucination_warnings.append(f"라벨 '{lb}'이 스키마에 없음")

            # Cypher에서 [:RelType] 패턴 추출
            _cypher_rels = set(re.findall(r"\[(?:\w+:)?(\w+)\]", cypher))
            for rt in _cypher_rels:
                if rt not in _known_rels and not rt.startswith("r") and len(rt) > 1:
                    hallucination_warnings.append(f"관계 '{rt}'가 스키마에 없음")

            # Cypher에서 n.propName 패턴 추출
            _cypher_props = set(re.findall(r"\.\s*([a-z][a-zA-Z0-9_]*)", cypher))
            _ignore_props = {"uri", "labels", "id", "count", "cnt", "sum", "avg", "min", "max", "type", "keys", "size", "length"}
            for prop in _cypher_props:
                if prop not in _known_props and prop not in _ignore_props:
                    hallucination_warnings.append(f"프로퍼티 '{prop}'가 스키마에 없음")

        result: dict = {
            "success": True,
            "cypher": cypher,
            "explanation": explanation,
        }
        if hallucination_warnings:
            result["hallucination_warnings"] = hallucination_warnings
            result["hallucination_count"] = len(hallucination_warnings)

        # 실행 요청 시 neo4j_query 호출
        if execute:
            # [I4] 쓰기 쿼리 감지
            _WRITE_KEYWORDS = ("CREATE", "MERGE", "DELETE", "SET ", "REMOVE ", "DETACH", "DROP ")
            # 주석/문자열 제거 후 키워드 검사 (대소문자 무관)
            _cypher_no_comments = re.sub(r"//.*$", "", cypher, flags=re.MULTILINE)
            _cypher_no_strings = re.sub(r"'[^']*'|\"[^\"]*\"", "", _cypher_no_comments)
            _cypher_upper = _cypher_no_strings.upper()
            if any(kw in _cypher_upper for kw in _WRITE_KEYWORDS):
                result["error"] = "생성된 Cypher에 쓰기 작업이 포함되어 있어 자동 실행하지 않습니다."
            else:
                from tools.remote.neo4j import neo4j_query
                query_result = neo4j_query(cypher)
                result["result"] = json.loads(query_result) if query_result else []

        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, hint=_bedrock_error_hint(e), logger=logger)


def ask_ontology(question: str) -> str:
    """온톨로지 관련 질문에 도메인 컨텍스트를 포함하여 답변한다.

    Args:
        question: 온톨로지 관련 질문 (예: "EquipmentMaster 클래스의 주요 프로퍼티는?")
    """
    try:
        classes = _load_domain_classes()
        class_list = ", ".join(classes) if classes else "(T-Box 또는 시맨틱 딕셔너리를 먼저 생성하세요)"

        upper = DOMAIN_CONFIG.get("upper_ontology", {})
        upper_framework = upper.get("framework", "IOF/BFO")

        prompt = f"""당신은 {_domain['name_ko']} 온톨로지 전문가입니다.

이 온톨로지는 {upper_framework} 기반이며,
마스터 데이터 테이블을 OWL 클래스로 매핑합니다.

네임스페이스:
- {NS_PREFIX}: <{DOMAIN_NS}>
- {NS_INST_PREFIX}: <{DOMAIN_INST_NS}>
- iof-core: <https://spec.industrialontologies.org/ontology/core/Core/>

주요 클래스: {class_list}

질문: {question}

한국어로 답변하세요."""

        # D3: 한국어 동의어 힌트 주입 (파일 없으면 빈 문자열 — 기존 동작 유지)
        from tools.korean_synonyms import format_synonym_hint, resolve_keywords
        hint = format_synonym_hint(resolve_keywords(question))
        if hint:
            prompt = prompt + hint

        return _invoke(prompt, max_tokens=4096)
    except Exception as e:
        return error_response(e, hint=_bedrock_error_hint(e), logger=logger)
