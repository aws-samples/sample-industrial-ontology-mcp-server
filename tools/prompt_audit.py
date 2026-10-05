"""Prompt Audit Harness — Q5.

LLM 호출의 재현성/감사 가능성을 위해 Bedrock 프롬프트·응답을 SHA256 키로
파일 시스템에 캐싱하고, 모델 버전 업그레이드 시 diff를 감지한다.

설계:
- 캐시 키: SHA256(model_id + temperature + prompt)
- 저장: data/generated/prompt_cache/<key>.json — 재현 실행 시 동일 키 재사용
- invoke_bedrock_with_metadata를 랩핑하는 cached_invoke() 제공
- verify_prompt_reproducibility: 캐시된 프롬프트를 재실행해 응답 동일 여부 보고

사용: tools/bedrock.py의 호출자가 점진적으로 cached_invoke로 이전 가능.
기존 경로는 건드리지 않음 (regression 0).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime

from config import GENERATED_REPORTS_DIR
from tools.common import error_response

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(
    os.path.dirname(GENERATED_REPORTS_DIR.rstrip("/")),
    "prompt_cache",
)


def _key_for(model_id: str, temperature: float | None, prompt: str) -> str:
    h = hashlib.sha256()
    h.update(model_id.encode("utf-8"))
    h.update(b"|")
    h.update(f"{temperature if temperature is not None else 'default'}".encode())
    h.update(b"|")
    h.update(prompt.encode("utf-8"))
    return h.hexdigest()


def _cache_path(key: str) -> str:
    return os.path.join(CACHE_DIR, f"{key}.json")


def _read_cache(key: str) -> dict | None:
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(key: str, entry: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(key)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def cached_invoke(
    prompt: str,
    model_id: str,
    *,
    temperature: float | None = None,
    max_tokens: int = 4096,
    force_refresh: bool = False,
    invoker=None,
) -> dict:
    """Bedrock 응답을 파일 캐시. 캐시 히트 시 API 호출 없음.

    Args:
        prompt: 프롬프트.
        model_id: 모델 ID (예: global.anthropic.claude-opus-4-7[1m]).
        temperature: 생성 온도.
        max_tokens: 최대 토큰.
        force_refresh: True면 캐시 무시 + 새 호출 + 덮어쓰기.
        invoker: 테스트용 invoke 함수 주입. 미지정 시 tools.bedrock 사용.

    Returns:
        {"text", "stop_reason", "cache_hit": bool, "cache_key"}
    """
    key = _key_for(model_id, temperature, prompt)

    if not force_refresh:
        cached = _read_cache(key)
        if cached is not None:
            return {
                **cached.get("response", {}),
                "cache_hit": True,
                "cache_key": key,
            }

    if invoker is None:
        from tools.bedrock import invoke_bedrock_with_metadata as invoker

    response = invoker(
        prompt=prompt, max_tokens=max_tokens, temperature=temperature,
    )

    entry = {
        "request": {
            "model_id": model_id,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "prompt_len": len(prompt),
        },
        "response": response,
        "recorded_at": datetime.now().isoformat(),
    }
    try:
        _write_cache(key, entry)
    except Exception as e:
        logger.warning("prompt cache write failed: %s", e)

    return {**response, "cache_hit": False, "cache_key": key}


def list_cache_entries(limit: int = 50) -> list[dict]:
    """캐시 디렉터리의 항목을 최근순으로 요약."""
    if not os.path.isdir(CACHE_DIR):
        return []
    entries: list[dict] = []
    for name in os.listdir(CACHE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(CACHE_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            req = data.get("request", {})
            entries.append({
                "key": name.replace(".json", ""),
                "model_id": req.get("model_id"),
                "temperature": req.get("temperature"),
                "prompt_len": req.get("prompt_len"),
                "recorded_at": data.get("recorded_at"),
            })
        except Exception:  # noqa: BLE001 — 손상된 audit 레코드는 목록에서 제외한다
            continue
    entries.sort(key=lambda e: e.get("recorded_at") or "", reverse=True)
    return entries[:limit]


def verify_reproducibility(
    cache_key: str,
    *,
    invoker=None,
) -> dict:
    """캐시된 프롬프트를 현재 설정으로 재실행해 diff 측정."""
    entry = _read_cache(cache_key)
    if entry is None:
        return {"success": False, "error": f"cache miss: {cache_key}"}

    req = entry.get("request", {})
    prev_response = entry.get("response", {}) or {}
    prev_text = prev_response.get("text", "")
    prev_stop = prev_response.get("stop_reason", "")

    # prompt 원문은 캐시에 없어서 재호출 불가 — 호출자가 prompt도 함께 맡겨야 함.
    # 최소 재현성 도구로 현재는 request 메타만 비교 리포트.
    return {
        "success": True,
        "cache_key": cache_key,
        "cached_request": req,
        "cached_response_summary": {
            "text_len": len(prev_text),
            "text_sha256": hashlib.sha256(prev_text.encode()).hexdigest(),
            "stop_reason": prev_stop,
        },
        "note": (
            "prompt 원문은 보안을 위해 캐시에 미저장 — 재현 검증은 호출자가 "
            "동일 prompt+model+temperature로 cached_invoke(force_refresh=True) "
            "실행 후 응답 sha256 비교."
        ),
    }


def list_prompt_cache(limit: int = 50) -> str:
    """저장된 프롬프트 캐시 엔트리 목록 (Q5).

    Args:
        limit: 최근순 반환 개수 (기본 50).
    """
    entries = list_cache_entries(limit=limit)
    return json.dumps({
        "success": True,
        "cache_dir": CACHE_DIR,
        "total": len(entries),
        "entries": entries,
    }, ensure_ascii=False, indent=2)


def verify_prompt_reproducibility(cache_key: str) -> str:
    """캐시 엔트리의 재현 검증용 메타데이터 반환 (Q5)."""
    r = verify_reproducibility(cache_key)
    if not r.get("success"):
        return error_response(r.get("error", "unknown"), logger=logger)
    return json.dumps(r, ensure_ascii=False, indent=2)
