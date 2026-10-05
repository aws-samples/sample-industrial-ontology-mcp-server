"""``rules/domain/table_class_mapping.json`` 단일 로더.

이 파일 하나가 CSV 테이블 ↔ 도메인 클래스 매핑의 **정본 해석기** 다. 예전에는 10개
모듈이 각자 파싱했고, 같은 입력에 **서로 다른 답** 을 냈다 (2026-08-08 실측):

    입력: {"T_ALPHA": "steel:Alpha", "T_BETA": "Beta",
           "T_GAMMA": "other:Gamma", "T_DELTA": {"class": "steel:Delta"}}

    abox_generation      → {T_ALPHA: Alpha, T_BETA: Beta, T_GAMMA: 'other:Gamma'}   # T_DELTA 소실
    competency_questions → {T_ALPHA: Alpha, T_BETA: Beta, T_GAMMA: Gamma, T_DELTA: Delta}
    temporal_cardinality → {T_ALPHA: Alpha, T_BETA: Beta, T_GAMMA: Gamma}           # T_DELTA 소실

세 가지가 다르면 S7 (A-Box 생성) 과 S9/S12 (검증·질의) 가 **다른 클래스 이름을 보고**
있다는 뜻이다. 중첩 dict 형태를 못 읽는 쪽은 그 테이블을 통째로 잃고, 접두어를
NS_PREFIX 로만 벗기는 쪽은 ``other:Gamma`` 를 콜론이 박힌 클래스 이름으로 만든다
(그 이름의 클래스는 존재하지 않으므로 이후 조회가 전부 실패한다).

정본 규칙:
  - **접두어는 종류를 가리지 않고 벗긴다** — ``ns:Local`` / ``…#Local`` / ``…/Local``.
    로컬 이름에는 이 구분자가 들어갈 수 없으므로 정보 손실이 없다.
  - **중첩 dict 를 허용한다** — ``{"class": "ns:Local"}``. 한 로더만 지원했는데,
    지원 안 하는 쪽에서는 그 테이블이 조용히 사라졌다.
  - **최상위 래핑을 관용한다** — ``{"table_class_mapping": {...}}`` 와 평평한 dict 둘 다.
  - **실패는 흡수한다** — 파일 부재/손상은 빈 dict. 매핑은 힌트이지 필수 입력이 아니고,
    여기서 예외를 올리면 상위 blanket handler 가 hint 없는 OS 에러로 바꿔버린다.

캐시는 ``(경로, mtime, size)`` 로 무효화된다. 운영 중 매핑을 고치면 다음 호출에
반영된다 — 영구 캐시는 "S9 FAIL → 매핑 수정 → 재실행" 루프에서 옛 값을 계속
돌려줘 stale 판정을 만들었다.
"""

from __future__ import annotations

import json
import logging
import os

from domain.rules_paths import RULES_ROOT, rules_path

logger = logging.getLogger(__name__)

_FILENAME = "table_class_mapping.json"

#: ``{(rules_dir, mtime, size): 파싱 결과}`` — 파일이 바뀌면 키가 바뀐다.
_CACHE: dict[tuple, dict] = {}

#: 캐시 상한. 배포당 rules_dir 는 1~2개이고 테스트가 임시 디렉토리를 쓰므로 넉넉하다.
_CACHE_MAX = 32


def _default_rules_dir() -> str:
    """리포의 ``rules/`` 절대 경로."""
    return RULES_ROOT


def strip_prefix(value: str) -> str:
    """``ns:Local`` / ``…#Local`` / ``…/Local`` → ``Local``.

    구분자 종류를 가리지 않는다. 배포 prefix 만 벗기면 다른 prefix 가 붙은 값이
    콜론째로 남아 존재하지 않는 클래스 이름이 된다.
    """
    return value.split(":")[-1].split("#")[-1].split("/")[-1]


def _resolve_path(location: str) -> str:
    """디렉토리든 파일 경로든 실제 매핑 파일 경로로 해석한다.

    호출부 중 일부는 파일 경로를 직접 들고 있고 (테스트가 그 경로를 임시 파일로
    monkeypatch 한다), 나머지는 ``rules/`` 디렉토리를 넘긴다. 둘 다 받는다 —
    디렉토리만 받으면 파일명을 다르게 쓰는 호출부의 설정이 조용히 무시된다.

    디렉토리를 받으면 :func:`domain.rules_paths.rules_path` 에 위임한다 —
    ``rules/domain/`` 하위와 루트 직하를 모두 찾으므로, 평평한 tmp_path 를 주는
    테스트와 카테고리로 나뉜 배포가 같은 코드로 동작한다.
    """
    if location.endswith(".json"):
        return location
    return rules_path(_FILENAME, base=location)


def _read_raw(location: str) -> dict:
    """매핑 파일 전체를 dict 로. 없거나 깨졌으면 빈 dict."""
    path = _resolve_path(location)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — 손상된 설정이 파이프라인을 막지 않는다
        logger.debug("%s 로드 실패, 빈 매핑으로 진행: %s", _FILENAME, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _signature(location: str) -> tuple:
    """``(경로, mtime, size)`` — 캐시 키."""
    path = _resolve_path(location)
    try:
        stat = os.stat(path)
    except OSError:
        return (path, 0.0, 0)
    return (path, stat.st_mtime, stat.st_size)


def _parsed(rules_dir: str | None) -> dict:
    """세 섹션을 한 번에 파싱해 캐시한다."""
    target = rules_dir or _default_rules_dir()
    key = _signature(target)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    raw = _read_raw(target)
    # 최상위 래핑 관용: 전용 키가 있으면 그것, 없으면 평평한 dict 로 본다.
    entries = raw.get("table_class_mapping", raw) if raw else {}
    table_class: dict[str, str] = {}
    if isinstance(entries, dict):
        for table, target_value in entries.items():
            if isinstance(target_value, dict):
                target_value = target_value.get("class")
            if isinstance(target_value, str) and target_value.strip():
                table_class[str(table)] = strip_prefix(target_value.strip())

    pk_columns: dict[str, list[str]] = {}
    for table, cols in (raw.get("table_pk_columns") or {}).items():
        if isinstance(cols, str):
            cols = [cols]
        if isinstance(cols, list):
            pk_columns[str(table)] = [str(c) for c in cols if isinstance(c, str)]

    result = {
        "table_class": table_class,
        "pk_columns": pk_columns,
        "class_prop": raw.get("class_prop_mapping") or {},
    }
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = result
    return result


def load_table_class_mapping(rules_dir: str | None = None) -> dict[str, str]:
    """``{CSV 테이블명: 클래스 local name}``. 없으면 빈 dict."""
    return _parsed(rules_dir)["table_class"]


def load_table_pk_columns(rules_dir: str | None = None) -> dict[str, list[str]]:
    """``{CSV 테이블명: [권위 PK 컬럼]}``. 없으면 빈 dict."""
    return _parsed(rules_dir)["pk_columns"]


def load_class_prop_mapping(rules_dir: str | None = None) -> dict:
    """``class_prop_mapping`` 섹션 원본. 없으면 빈 dict."""
    return _parsed(rules_dir)["class_prop"]


def mapped_class_names(rules_dir: str | None = None) -> set[str]:
    """매핑이 정본으로 지정한 클래스 local name 집합."""
    return set(load_table_class_mapping(rules_dir).values())


def mapping_mtime(rules_dir: str | None = None) -> float:
    """매핑 파일 mtime (없으면 0.0). 호출부 자체 캐시 무효화용."""
    return _signature(rules_dir or _default_rules_dir())[1]


def invalidate() -> None:
    """캐시 전체 폐기. 서명 키 덕에 운영에선 불필요 — 테스트용.

    임시 디렉토리를 재사용하는 테스트는 mtime+size 가 우연히 같을 수 있다.
    """
    _CACHE.clear()
