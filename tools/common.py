"""도구 모듈 공통 유틸리티 — 에러 응답, 클라이언트 캐싱 등."""

import contextlib
import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


# ── HTML 보고서의 외부 스크립트 (버전 고정 + SRI) ────────────────
#
# 생성 HTML 은 vis-network 를 CDN 에서 받는다. 버전을 빼면 unpkg 가
# `max-age=60` 으로 latest 를 재해석하므로 upstream 변경이 사용자
# 브라우저에 그대로 들어온다. `integrity` 는 그 변경을 브라우저가
# 거부하게 만들고, cross-origin 스크립트에서 integrity 를 검사하려면
# `crossorigin` 이 함께 있어야 한다 (없으면 응답이 opaque 라 검사
# 자체가 불가능하다).
#
# 버전을 올릴 때 해시도 같이 바꾼다. 재계산:
#   curl -sfL "https://unpkg.com/vis-network@<VER>/standalone/umd/vis-network.min.js" \
#     | openssl dgst -sha384 -binary | openssl base64 -A
VIS_NETWORK_VERSION = "10.1.2"
VIS_NETWORK_SRI = (
    "sha384-RDdG1CLOxjNlTHh4JYx/rnAueaMHbkBHmeHwrEyljMQw3LF0it4SkuNotIY/FPxD"
)
VIS_NETWORK_SCRIPT_TAG = (
    f'<script src="https://unpkg.com/vis-network@{VIS_NETWORK_VERSION}'
    f'/standalone/umd/vis-network.min.js" integrity="{VIS_NETWORK_SRI}"'
    f' crossorigin="anonymous"></script>'
)


# ── HTML 보고서에 싣는 데이터의 출력 인코딩 ───────────────────────
#
# 보고서 데이터(T-Box label·comment, CSV 헤더·셀·파일명, 도메인 이름)는 사용자
# 입력이나 LLM 출력이다. 문맥마다 인코딩이 다르다:
#   - 서버에서 만드는 HTML 텍스트·속성 값 → ``html.escape``
#   - ``<script>`` 블록 안의 JSON          → ``json_for_script``
#   - 브라우저에서 innerHTML 로 조립하는 값  → ``ESCAPE_HTML_JS`` 의 ``escapeHtml``

#: ``json.dumps`` 는 ``</script>`` 와 ``<!--`` 를 그대로 둔다. HTML 토크나이저는 JSON
#: 문자열 안이라는 사실을 모르므로 그 조각에서 script 요소를 닫는다. ``<``, ``>``,
#: ``&`` 를 JSON 유니코드 이스케이프로 바꾸면 토크나이저가 볼 태그가 남지 않고,
#: JavaScript 가 읽는 값은 바뀌지 않는다. U+2028/U+2029 는 ES2019 이전 엔진에서
#: 문자열 리터럴 안의 줄바꿈이라 함께 바꾼다 (Django ``json_script`` 와 같은 방식).
_SCRIPT_JSON_ESCAPES = {
    ord("<"): "\\u003c",
    ord(">"): "\\u003e",
    ord("&"): "\\u0026",
    0x2028: "\\u2028",
    0x2029: "\\u2029",
}


def json_for_script(data: object) -> str:
    """``<script>`` 블록 안에 그대로 넣어도 요소 경계를 만들지 않는 JSON 을 만든다.

    ``json.loads`` 로 되읽으면 원래 값과 같다. 한글은 ``ensure_ascii=False`` 로 읽을 수
    있게 둔다.
    """
    return json.dumps(data, ensure_ascii=False).translate(_SCRIPT_JSON_ESCAPES)


#: 브라우저에서 innerHTML 문자열을 조립할 때 데이터 값마다 거치는 escape 함수.
#: 텍스트와 따옴표로 감싼 속성 값 양쪽에 안전하도록 ``& < > " '`` 다섯 문자를 바꾼다.
#: 인라인 ``on*=`` 핸들러 문자열 안의 값은 JavaScript 문맥이라 이 함수로 막을 수 없다.
#: 그런 값은 ``data-*`` 속성에 싣고 ``addEventListener`` 로 읽는다.
ESCAPE_HTML_JS = (
    "function escapeHtml(value) {\n"
    "  return String(value === null || value === undefined ? '' : value)\n"
    "    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')\n"
    "    .replace(/\"/g, '&quot;').replace(/'/g, '&#39;');\n"
    "}"
)


def dedupe_identical_csvs(
    csv_paths: list[str],
    *,
    preferred_tables: set[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """내용이 동일한 CSV 사본을 제거하고 정본 하나만 남긴다.

    파일명만 다른 사본을 별개 테이블로 취급하면 파이프라인 전체가 같은 실체를
    두 번 모델링한다 (2026-07-25 실측: ``Facility_Master_A.csv`` 와
    ``Facility_Master_B.csv`` 가 md5 동일한데 S2 가
    ``FacilityEquipmentMaster`` / ``ProductionProcessMaster`` 두 클래스에 DP 9개씩
    대칭 생성 + 둘을 잇는 성립 불가능한 참조 OP 까지 만들었고, 결국 인스턴스 0 인
    죽은 클래스가 남았다).

    정본 선택 규칙:
      1. ``preferred_tables`` (예: ``table_class_mapping.json`` 등록 테이블) 에
         있는 쪽 — SME 가 지정한 정본이므로 최우선
      2. 그 외에는 파일명 사전순 첫 번째 (호출자가 정렬해 넘기면 결정적)

    Args:
        csv_paths: 검사할 CSV 경로 목록 (정렬해서 넘기면 결과가 결정적).
        preferred_tables: 정본으로 승격할 테이블명 집합 (확장자 없는 basename).

    Returns:
        ``(kept_paths, skipped)`` — ``skipped`` 는 ``{제외된 테이블명: 채택된
        테이블명}``. 중복이 없으면 빈 dict.
    """
    preferred = preferred_tables or set()
    seen: dict[str, str] = {}            # md5 → 채택된 table_name
    kept: list[str] = []
    skipped: dict[str, str] = {}

    def _table_of(path: str) -> str:
        return os.path.basename(path).replace(".csv", "")

    for path in csv_paths:
        table = _table_of(path)
        try:
            with open(path, "rb") as handle:
                digest = hashlib.md5(
                    handle.read(),
                    usedforsecurity=False,
                ).hexdigest()
        except OSError as exc:
            # 읽기 실패는 dedup 대상에서 빼고 그대로 통과 — 호출자의 기존
            # 예외 처리에 맡긴다.
            logger.debug("CSV dedup 해시 실패, 그대로 통과 (%s): %s", table, exc)
            kept.append(path)
            continue

        incumbent = seen.get(digest)
        if incumbent is None:
            seen[digest] = table
            kept.append(path)
            continue
        if table in preferred and incumbent not in preferred:
            # 등록된 테이블을 정본으로 승격, 기존 채택분을 물린다.
            skipped[incumbent] = table
            kept = [p for p in kept if _table_of(p) != incumbent]
            seen[digest] = table
            kept.append(path)
        else:
            skipped[table] = incumbent

    if skipped:
        logger.warning(
            "동일 내용 CSV %d개 제외 (중복 클래스/인스턴스 생성 방지): %s",
            len(skipped),
            {k: f"→ {v} 채택" for k, v in skipped.items()},
        )
    return kept, skipped


def atomic_write(path: str, content: str) -> None:
    """Write content to file atomically using tempfile + os.replace."""
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=dir_name, delete=False,
                                      suffix='.tmp', encoding='utf-8') as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def atomic_write_json(path: str, data: object, *, indent: int = 2) -> None:
    """JSON 객체를 파일에 원자적으로 저장한다.

    대량(>1MB) JSON 에 대해 stdlib json.dumps 보다 orjson 이 5~30× 빠르다.
    orjson 미설치 시 stdlib 로 fallback.

    Args:
        path: 저장 경로.
        data: JSON 직렬화 대상 (dict/list 등).
        indent: 2(기본) 또는 0(압축). orjson 은 OPT_INDENT_2 만 지원하므로
                0 이외 값은 모두 2 로 처리된다.
    """
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    try:
        import orjson
        option = orjson.OPT_INDENT_2 if indent else 0
        payload = orjson.dumps(data, option=option)
        with tempfile.NamedTemporaryFile(mode='wb', dir=dir_name, delete=False,
                                          suffix='.tmp') as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
    except ImportError:
        payload_str = json.dumps(data, ensure_ascii=False, indent=indent or None)
        with tempfile.NamedTemporaryFile(mode='w', dir=dir_name, delete=False,
                                          suffix='.tmp', encoding='utf-8') as tmp:
            tmp.write(payload_str)
            tmp_path = tmp.name
    os.replace(tmp_path, path)


# ── 사이드카 계보 (소스 지문 각인) ──────────────────────────────
#
# ## 왜 필요한가 (실측 2026-09-02)
#
# 배포 트리의 사이드카 6종이 실제 산출물을 서술하지 않았다:
#
#   reports/ontoclean_report.json      {"error": "T-Box not found: /nonexistent/t.ttl"}
#   reports/fair_score.json            tbox_path = /tmp/pytest-…/tbox.ttl
#   reports/canonical_compare.json     path_a/b 가 pytest tmpdir
#   inferred/inference_loss_manifest   abox_triples = -4921, total_triples = 1,
#                                      그런데 meaningful_triples_ratio = 1.0
#   inferred/inference_provenance.ttl  23줄, totalInferred 0 (실제 폐쇄 1,305,608)
#   inferred/inference_justifications  전부 0
#
# 원인은 두 겹이다.
#
#   ① 도구가 **임의 입력 경로**를 받고 출력은 **배포 상수**에 쓴다. 그래서
#      `compare_canonical('/tmp/a','/tmp/b')` 한 번이 배포 감사 증거를 지웠다.
#   ② 사이드카에 소스 산출물의 지문이 **없어서** 세대 어긋남이 구조적으로 탐지
#      불가였다. 어느 게이트도 발화하지 않았다.
#
# 세 번째 표면도 있다: 에러 응답이 그대로 보고서로 저장됐다 (ontoclean). 실패한
# 분석이 성공한 보고서와 같은 형태로 남으면 다음 소비자가 그것을 사실로 읽는다.

_FINGERPRINT_CHUNK = 1 << 20


def artifact_fingerprint(path: str | os.PathLike) -> dict:
    """산출물 하나의 지문 — ``{path, exists, size, mtime, mtime_iso, sha256}``.

    ``sha256`` 은 전체 내용이다. 표본 해시를 쓰지 않는다 — 파일 중간 변경을 놓치고,
    이 리포는 "근사로 지문을 대조" 해서 틀린 전례가 있다. 최대 입력이 121MB 이고
    S8 은 6분 잡이므로 전체 해시 비용은 무시할 수 있다.
    """
    p = os.fspath(path)
    try:
        st = os.stat(p)
    except OSError:
        return {"path": p, "exists": False}
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            while chunk := f.read(_FINGERPRINT_CHUNK):
                h.update(chunk)
    except OSError:
        return {"path": p, "exists": False}
    from datetime import datetime
    return {
        "path": p,
        "exists": True,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_iso": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
        "sha256": h.hexdigest(),
    }


def source_stamp(**sources: str) -> dict:
    """사이드카에 각인할 ``_source`` 블록.

    Args:
        **sources: ``논리명=경로``. 예: ``tbox=TBOX_PATH, abox=ABOX_PATH``.

    Returns:
        ``{"stamped_at", "artifacts": {논리명: 지문}}``.
    """
    from datetime import datetime
    return {
        "stamped_at": datetime.now(tz=UTC).isoformat(),
        "artifacts": {name: artifact_fingerprint(path) for name, path in sources.items()},
    }


def stale_sources(stamp: dict | None) -> list[dict]:
    """각인된 지문과 **현재** 파일을 대조해 어긋난 것을 반환한다.

    각인이 없으면 ``[{"name": "_stamp", "reason": "지문 각인 없음"}]`` 을 낸다 —
    "대조할 수 없다" 를 "일치한다" 로 읽지 않기 위함이다.
    """
    if not isinstance(stamp, dict) or not isinstance(stamp.get("artifacts"), dict):
        return [{"name": "_stamp", "reason": "지문 각인 없음 — 세대 대조 불가"}]
    out: list[dict] = []
    for name, recorded in stamp["artifacts"].items():
        if not isinstance(recorded, dict):
            out.append({"name": name, "reason": "각인 형태가 지문이 아니다"})
            continue
        current = artifact_fingerprint(recorded.get("path", ""))
        if not current["exists"]:
            if recorded.get("exists"):
                out.append({"name": name, "reason": "각인 당시 있던 파일이 지금 없다"})
            continue
        if not recorded.get("exists"):
            out.append({"name": name, "reason": "각인 당시 없던 파일이 지금 있다"})
            continue
        if current["sha256"] != recorded.get("sha256"):
            out.append({
                "name": name,
                "reason": "내용이 달라졌다",
                "stamped_mtime": recorded.get("mtime_iso"),
                "current_mtime": current["mtime_iso"],
            })
    return out


def is_deployed_input(given: str | None, deployed: str) -> bool:
    """호출자가 준 입력 경로가 **배포 산출물** 인가.

    빈 값은 "기본값을 쓰라" 는 뜻이므로 배포로 본다. 그 밖에는 실경로로 비교한다
    (심링크·상대경로가 배포 판정을 우회하지 못하게).
    """
    if not given:
        return True
    try:
        return os.path.realpath(os.fspath(given)) == os.path.realpath(deployed)
    except (TypeError, ValueError):
        return False


def add_source_artifacts(
    path: str, *, writer: Callable[[str, object], None] | None = None, **sources: str,
) -> bool:
    """이미 저장된 JSON 사이드카의 ``_source.artifacts`` 에 지문을 **덧붙인다**.

    왜 별 함수인가: 사이드카가 자기 출력보다 **먼저** 쓰이는 경우가 있다.
    ``inference.py`` 는 step 7~8 에서 loss manifest·justifications 를 쓰고 step 9 에서
    ``all_inferred.ttl`` 을 직렬화한다. 그 시점에 출력 지문을 각인하면 **이전 세대**
    파일을 해시한다 — 세대 대조를 하려고 만든 장치가 세대를 거짓으로 기록하는 셈이다.
    그래서 입력은 쓰기 시점에, 출력은 직렬화 **이후** 이 함수로 덧붙인다.

    Args:
        path: 갱신할 JSON 사이드카.
        writer: 쓰기 primitive 주입. 호출 모듈이 자기 ``atomic_write_json`` 을 넘기면
            그 모듈을 patch 한 테스트가 이 쓰기도 그대로 가로챈다. 넘기지 않으면
            ``tools.common`` 의 것을 쓴다 — 그 경우 모듈별 patch 를 우회하므로
            배포 트리에 실제 쓰기가 발생한다 (실측 2026-09-03: 쓰기 가드가 잡았다).
        **sources: ``논리명=경로``.

    Returns:
        갱신했으면 True. 파일이 없거나 JSON 이 아니면 False (조용히 넘긴다 —
        사이드카 각인 실패가 추론 결과 반환을 막아서는 안 된다).
    """
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    stamp = payload.get("_source")
    if not isinstance(stamp, dict) or not isinstance(stamp.get("artifacts"), dict):
        stamp = source_stamp()
    for name, src in sources.items():
        stamp["artifacts"][name] = artifact_fingerprint(src)
    payload["_source"] = stamp
    try:
        (writer or atomic_write_json)(path, payload)
    except OSError:
        return False
    return True


def write_deployed_sidecar(
    out_path: str,
    payload: dict,
    *,
    sources: dict[str, str],
    inputs_are_deployed: bool = True,
    logger: logging.Logger | None = None,
) -> dict:
    """배포 사이드카를 **세 조건을 모두 만족할 때만** 쓴다.

    1. ``payload`` 가 에러 응답이 아니다 (``error`` 키 부재).
    2. ``inputs_are_deployed`` — 임의 경로 입력으로 계산한 결과를 배포 경로에 쓰지
       않는다. 애드혹 호출 한 번이 감사 증거를 지우는 경로가 이것이었다.
    3. ``_source`` 지문을 각인한다 (payload 를 복사해 붙이므로 호출자 dict 는
       변형하지 않는다).

    Returns:
        ``{"written": bool, "path": str, "reason": str}`` — 도구가 응답에 실어
        운영자가 "왜 리포트가 안 갱신됐나" 를 알 수 있게 한다.
    """
    log = logger or globals()["logger"]
    if not isinstance(payload, dict):
        return {"written": False, "path": out_path, "reason": "payload 가 dict 가 아니다"}
    if "error" in payload:
        # 실패한 분석을 성공한 보고서와 같은 형태로 남기면 다음 소비자가 사실로 읽는다.
        return {
            "written": False, "path": out_path,
            "reason": "에러 응답은 배포 보고서로 저장하지 않는다",
        }
    if not inputs_are_deployed:
        return {
            "written": False, "path": out_path,
            "reason": "임의 경로 입력으로 계산한 결과다 — 배포 사이드카를 덮어쓰지 않는다",
        }
    stamped = dict(payload)
    stamped["_source"] = source_stamp(**sources)
    try:
        atomic_write_json(out_path, stamped)
    except OSError as e:
        log.warning("사이드카 저장 실패 (%s): %s", out_path, e)
        return {"written": False, "path": out_path, "reason": f"저장 실패: {e}"}
    return {"written": True, "path": out_path, "reason": ""}


def success_response(data: dict, **kwargs: object) -> str:
    """표준 성공 JSON 응답을 생성한다.

    Args:
        data: 응답 데이터 dict. "success": True가 자동 추가됨.
        **kwargs: data에 병합할 추가 키-값.

    Returns:
        '{"success": true, ...}' 형태의 JSON 문자열
    """
    result = {"success": True, **data, **kwargs}
    return json.dumps(result, ensure_ascii=False, indent=2)


def error_response(error: Exception | str, *, hint: str | None = None, logger: logging.Logger | None = None) -> str:
    """표준 에러 JSON 응답을 생성한다.

    Returns:
        '{"success": false, "error": "...", "hint": "..."}' 형태의 JSON 문자열
    """
    error_str = str(error)
    if logger:
        logger.error(error_str, exc_info=isinstance(error, Exception))
    result: dict[str, object] = {"success": False, "error": error_str}
    if hint:
        result["hint"] = hint
    return json.dumps(result, ensure_ascii=False)


def safe_read_file(path: str, *, hint: str = "", logger: logging.Logger | None = None) -> str:
    """파일을 읽어 문자열로 반환한다. FileNotFoundError 시 표준 에러 JSON 반환."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return error_response(f"파일이 없습니다: {path}", hint=hint, logger=logger)
    except Exception as e:
        return error_response(e, logger=logger)


def resolve_child_path(
    base_dir: str,
    filename: str,
    *,
    allowed_suffixes: tuple[str, ...] = (),
) -> str:
    """사용자 파일명을 ``base_dir`` 바로 아래의 안전한 경로로 해석한다.

    절대경로, 하위 디렉터리, ``..`` 및 base 밖을 가리키는 symlink를 거부한다.
    """
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("filename은 비어 있을 수 없습니다.")

    name = filename.strip()
    if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("filename은 디렉터리 구분자를 포함할 수 없습니다.")
    if allowed_suffixes and not name.lower().endswith(
        tuple(suffix.lower() for suffix in allowed_suffixes)
    ):
        expected = ", ".join(allowed_suffixes)
        raise ValueError(f"filename 확장자는 다음 중 하나여야 합니다: {expected}")

    base = Path(base_dir).resolve()
    candidate = (base / name).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("filename이 허용된 디렉터리 밖을 가리킵니다.") from exc
    return str(candidate)


def resolve_path_within(
    base_dir: str,
    file_path: str,
    *,
    allowed_suffixes: tuple[str, ...] = (),
) -> str:
    """파일 경로를 symlink 해석 후 ``base_dir`` 아래의 안전한 경로로 해석한다.

    절대경로와 상대경로를 모두 정규화하며, ``..`` 탈출과 base 밖을 가리키는
    symlink를 거부한다.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("파일 경로는 비어 있을 수 없습니다.")

    raw_path = file_path.strip()
    if "\x00" in raw_path:
        raise ValueError("파일 경로에 NUL 문자를 포함할 수 없습니다.")

    base = Path(base_dir).resolve()
    candidate = Path(raw_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("파일 경로가 허용된 데이터 디렉터리 밖을 가리킵니다.") from exc
    if candidate == base:
        raise ValueError("파일 경로는 허용된 데이터 디렉터리 안의 파일이어야 합니다.")

    normalized_suffixes = tuple(suffix.lower() for suffix in allowed_suffixes)
    if normalized_suffixes and candidate.suffix.lower() not in normalized_suffixes:
        expected = ", ".join(allowed_suffixes)
        raise ValueError(f"파일 확장자는 허용 확장자 중 하나여야 합니다: {expected}")
    return str(candidate)


def path_to_file_uri(path_str: str) -> str:
    """OS 경로를 크로스플랫폼 file:// URI로 변환한다.

    macOS/Linux: file:///Users/... , Windows: file:///C:/Users/...
    """
    return Path(path_str).resolve().as_uri()


def load_ttl_content(ttl_content: str, ttl_path: str, default_path: str = "") -> str:
    """ttl_content, ttl_path, 또는 기본 경로에서 TTL 문자열을 로드한다.

    우선순위: ttl_content → ttl_path → default_path
    """
    if ttl_content:
        return ttl_content
    if ttl_path:
        with open(ttl_path, encoding="utf-8") as f:
            return f.read()
    if not default_path:
        from config import TBOX_PATH
        default_path = TBOX_PATH
    with open(default_path, encoding="utf-8") as f:
        return f.read()


def safe_call(
    fn: Callable[..., T],
    *args,
    fallback: T | None = None,
    log_level: str = "warning",
    logger: logging.Logger | None = None,
    context: str = "",
    **kwargs,
) -> T | None:
    """예외를 삼키지 않으면서 통일된 로깅 + fallback 반환하는 래퍼.

    내부 헬퍼에서 "실패해도 계속 진행" 패턴을 일관되게 만들기 위한 유틸.
    MCP 경계에서는 error_response를 그대로 사용.

    Args:
        fn: 호출할 함수.
        fallback: 예외 시 반환할 값 (기본 None).
        log_level: "debug" | "info" | "warning" | "error".
        logger: 로깅 대상 logger. None이면 stdlib root.
        context: 에러 메시지 접두 문자열.

    Returns:
        fn(*args, **kwargs) 결과. 예외 발생 시 fallback.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        lg = logger or logging.getLogger(fn.__module__)
        msg = f"{context}: {type(e).__name__}: {e}" if context else f"{type(e).__name__}: {e}"
        level_fn = {
            "debug": lg.debug,
            "info": lg.info,
            "warning": lg.warning,
            "error": lg.error,
        }.get(log_level, lg.warning)
        level_fn(msg)
        return fallback


class StructuredLogFormatter(logging.Formatter):
    """JSON 구조화 로그 — #23.

    LOG_FORMAT=json 환경변수 설정 시 사용. 기본은 표준 포매터 유지.
    - timestamp (ISO 8601)
    - level
    - logger (module name)
    - message
    - stage (LogRecord.stage가 있으면 포함 — extra={"stage": "S4"})
    - duration_sec (extra에 있으면 포함)
    - exception (있으면)
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        import json as _j
        from datetime import datetime
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
                          .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k in ("stage", "duration_sec", "tool", "kg_triples"):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return _j.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | int = "INFO") -> None:
    """루트 로거 초기화 — LOG_FORMAT 환경변수로 포맷 선택.

    - LOG_FORMAT=json → StructuredLogFormatter
    - 기본 → `%(asctime)s %(levelname)s %(name)s :: %(message)s`

    중복 핸들러 방지: 이미 등록된 StreamHandler가 있으면 교체하지 않고 종료.
    """
    root = logging.getLogger()
    lvl = level if isinstance(level, int) else logging.getLevelName(str(level).upper())
    root.setLevel(lvl)

    has_stream = any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    if has_stream:
        return

    handler = logging.StreamHandler()
    fmt = os.environ.get("LOG_FORMAT", "").lower()
    if fmt == "json":
        handler.setFormatter(StructuredLogFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s :: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))
    root.addHandler(handler)


def cached_client(factory: Callable[[], T]) -> Callable[[], T]:
    """스레드 안전한 싱글턴 클라이언트 캐시 데코레이터.

    Usage:
        @cached_client
        def _get_my_client():
            return ExpensiveClient()

    테스트에서 캐시 초기화가 필요한 경우 래핑된 함수의 `.reset()`을 호출.
    """
    _lock = threading.Lock()
    _instance: list = []  # mutable container for nonlocal

    def wrapper() -> T:
        if not _instance:
            with _lock:
                if not _instance:
                    _instance.append(factory())
        return _instance[0]

    def _reset() -> None:
        with _lock:
            _instance.clear()

    wrapper.reset = _reset  # type: ignore[attr-defined]
    return wrapper


# ─────────────────────────────────────────────────────────
# Remote call policy (HIGH 2-3)
# ─────────────────────────────────────────────────────────
import random as _random  # noqa: E402
import time as _time  # noqa: E402
from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class RemoteCallPolicy:
    """원격 호출 공통 정책.

    timeout: 요청당 타임아웃(초)
    retries: 재시도 횟수(첫 시도 제외)
    backoff_base: 지수 백오프 기준 초. 대기 시간 = backoff_base * (2 ** attempt) + jitter
    jitter: 0~jitter 초 랜덤 추가. 0이면 jitter 비활성
    retry_on: 재시도 대상 예외 타입 튜플. 기본은 (TimeoutError, ConnectionError).
    """

    timeout: float = 30.0
    retries: int = 0
    backoff_base: float = 1.0
    jitter: float = 0.2
    retry_on: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)

    def call(self, fn: Callable[[], T], *, logger: logging.Logger | None = None,
             context: str = "") -> T:
        """fn()을 정책에 따라 호출한다. 재시도 + 백오프 포함.

        Args:
            fn: 0-인자 callable. 결과가 반환되면 그대로 돌려준다.
            logger: 재시도 로그용.
            context: 로그 접두 문자열.
        """
        last_exc: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                return fn()
            except self.retry_on as e:
                last_exc = e
                if attempt >= self.retries:
                    break
                sleep_for = self.backoff_base * (2 ** attempt)
                if self.jitter > 0:
                    sleep_for += _random.uniform(0, self.jitter)
                if logger:
                    logger.warning(
                        "%s retry %d/%d after %s: %.2fs sleep",
                        context or "remote call", attempt + 1, self.retries,
                        type(e).__name__, sleep_for,
                    )
                _time.sleep(sleep_for)
        assert last_exc is not None
        raise last_exc


DEFAULT_REMOTE_POLICY = RemoteCallPolicy(timeout=30.0, retries=2, backoff_base=1.0, jitter=0.2)


# ── 장기 실행 MCP 도구용 잡(Job) 레지스트리 ──────────────────────────
#
# MCP stdio 는 단일 CallToolRequest 가 수 분간 응답을 끌면 클라이언트
# 타임아웃으로 연결을 끊고, 서버가 stdin EOF 로 종료된다 (2026-06-20 규명).
# 6분+ 걸리는 도구(추론 S8, T-Box 협업생성 S2, GraphDB 추론 S8-large)는
# 동기 응답 대신 이 레지스트리로 job_id 를 즉시 반환하고 백그라운드 daemon
# 스레드에서 실행한 뒤, 별도 status 도구로 폴링한다. 응답 윈도우 자체가
# 사라져 타임아웃이 발화하지 않는다.
#
# 도구-무관 공통 로직(레지스트리 dict+lock, single-flight, eviction, worker
# 래핑, status 조회)만 담는다. 도구 특화 부분은 주입으로 흡수:
#   - worker: 0-인자 클로저 (도구별 인자를 caller 가 캡처, JSON 문자열 반환)
#   - key:    caller 가 만든 single-flight 키 문자열 (불투명)
#   - slim_fn: done 결과의 거대 리스트 필드 in-place 요약 (status 호출 시 주입)
class JobRegistry:
    """장기 실행 도구를 백그라운드 잡으로 돌리는 thread-safe 레지스트리.

    각 장기 도구 모듈이 모듈 레벨 인스턴스 1개를 만들어 공유한다.
    """

    def __init__(self, *, name: str, poll_with: str, max_finished: int = 8,
                 logger: logging.Logger | None = None) -> None:
        """
        Args:
            name: job_id 접두 + 스레드 이름 (예: "infer", "tbox").
            poll_with: 상태 조회 도구 이름 — 반환 JSON 의 poll_with 필드용.
            max_finished: 완료(done/failed) 잡 보관 상한 (메모리 누적 방지).
            logger: 잡 시작/실패 로깅용 (선택).
        """
        self._name = name
        self._poll_with = poll_with
        self._max_finished = max_finished
        self._logger = logger
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _evict_locked(self) -> None:
        """완료 잡이 상한 초과 시 오래된 것부터 제거. lock 보유 가정."""
        finished = [
            (jid, j) for jid, j in self._jobs.items()
            if j["status"] in ("done", "failed")
        ]
        if len(finished) <= self._max_finished:
            return
        finished.sort(key=lambda kv: kv[1].get("finished_at", 0.0))
        for jid, _ in finished[: len(finished) - self._max_finished]:
            self._jobs.pop(jid, None)

    def _run(self, job_id: str, worker: "Callable[[], str]") -> None:
        """daemon 스레드 본문 — worker() 실행 후 결과/에러를 잡에 기록."""
        try:
            result_json = worker()
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    # 작은 JSON 문자열만 보관 — 대용량 그래프 참조는 남기지 않음.
                    job["status"] = "done"
                    job["result"] = result_json
                    job["finished_at"] = _time.monotonic()
                    self._evict_locked()
        except BaseException as exc:  # noqa: BLE001 — 워커 최후 방어선.
            # BaseException: MemoryError/SystemExit 같은 것도 기록해야 한다. 예전엔
            # Exception 만 잡아서, 그 밖의 실패는 스레드만 죽고 잡은 영원히
            # "running" 으로 남았다 (2026-08-09 규명). 기록 후 재전파한다.
            if self._logger:
                self._logger.exception("%s job %s 실패", self._name, job_id)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["error"] = f"{type(exc).__name__}: {exc}"
                    job["finished_at"] = _time.monotonic()
                    self._evict_locked()
            if not isinstance(exc, Exception):
                # KeyboardInterrupt/SystemExit 류는 삼키지 않는다 — 기록만 하고
                # 인터프리터의 원래 종료 의도를 존중한다.
                raise

    def dispatch(self, *, key: str, worker: "Callable[[], str]") -> str:
        """잡을 등록·시작하고 job_id 를 즉시(ms) 반환한다.

        single-flight: 동일 key 가 running 이면 그 잡을 재사용한다.

        Args:
            key: single-flight 키 (도구별 입력으로 caller 가 구성).
            worker: 0-인자 클로저. 동기 본문을 실행하고 결과 JSON 문자열 반환.

        Returns:
            {"started": bool, "job_id", "status": "running"|"reused",
             "poll_with", "message"}  (JSON 문자열)
        """
        with self._lock:
            for jid, job in self._jobs.items():
                if job["key"] == key and job["status"] == "running":
                    return json.dumps({
                        "started": False, "job_id": jid, "status": "reused",
                        "poll_with": self._poll_with,
                        "message": f"동일 입력 작업이 이미 실행 중입니다. 기존 job_id 로 {self._poll_with} 폴링하세요.",
                    }, ensure_ascii=False)
            job_id = f"{self._name}_{int(_time.monotonic() * 1000)}_{len(self._jobs)}"
            self._jobs[job_id] = {
                "key": key, "status": "running", "result": None,
                "error": None, "started_at": _time.monotonic(), "finished_at": None,
                # 워커 스레드 핸들 — status() 가 "죽었는데 running" 을 판별한다.
                "thread": None,
            }
        thread = threading.Thread(
            target=self._run, args=(job_id, worker),
            name=f"{self._name}-{job_id}", daemon=True,
        )
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["thread"] = thread
        thread.start()
        if self._logger:
            self._logger.info("%s 잡 시작: %s", self._name, job_id)
        return json.dumps({
            "started": True, "job_id": job_id, "status": "running",
            "poll_with": self._poll_with,
            "message": f"백그라운드에서 시작했습니다. {self._poll_with}(job_id) 로 폴링하세요 (30~60초 간격).",
        }, ensure_ascii=False)

    def status(self, job_id: str, *,
               slim_fn: "Callable[[dict], None] | None" = None) -> str:
        """잡의 상태/결과를 즉시 조회한다 (그래프 미접근, 빠른 dict 조회).

        Args:
            job_id: dispatch 가 반환한 job_id.
            slim_fn: done 결과 dict 를 in-place 요약하는 콜백 (거대 리스트 cap).
                MCP stdio 가 응답을 한 줄로 원자 전송하므로 MB 급 result 는
                파이프를 막는다 — 인스턴스-비례 필드가 있으면 반드시 주입.

        Returns:
            running/done/failed/unknown 분기 JSON 문자열.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return json.dumps({
                    "job_id": job_id, "status": "unknown",
                    "error": "해당 job_id 가 없습니다 (만료되었거나 잘못된 ID). 작업을 다시 시작하세요.",
                }, ensure_ascii=False)
            status = job["status"]
            started = job.get("started_at") or 0.0
            finished = job.get("finished_at")
            elapsed = round((finished or _time.monotonic()) - started, 1)
            if status == "running":
                # 스레드가 이미 죽었는데 결과도 에러도 기록되지 않았다면 워커가
                # 비정상 종료한 것이다. 예전엔 이 상태를 영원히 "running" 으로
                # 보고했고, single-flight 가 같은 key 의 **재시도까지 차단** 해
                # 서버 재시작 전까지 그 작업을 다시 돌릴 수 없었다 (2026-08-09 규명).
                # S2 는 50분 잡이라 이 오진이 특히 비싸다.
                thread = job.get("thread")
                if thread is not None and not thread.is_alive():
                    job["status"] = "failed"
                    job["error"] = (
                        "워커 스레드가 결과를 기록하지 않고 종료했습니다 "
                        "(프로세스 재시작·OOM 등). 작업을 다시 시작하세요."
                    )
                    job["finished_at"] = _time.monotonic()
                    if self._logger:
                        self._logger.error(
                            "%s job %s: 워커 사망 감지 — failed 로 전환", self._name, job_id,
                        )
                    return json.dumps({
                        "job_id": job_id, "status": "failed",
                        "elapsed_seconds": elapsed, "error": job["error"],
                    }, ensure_ascii=False)
                return json.dumps({
                    "job_id": job_id, "status": "running", "elapsed_seconds": elapsed,
                    "message": "진행 중. 30~60초 후 다시 폴링하세요.",
                }, ensure_ascii=False)
            if status == "failed":
                return json.dumps({
                    "job_id": job_id, "status": "failed",
                    "elapsed_seconds": elapsed, "error": job.get("error"),
                }, ensure_ascii=False)
            result_str = job.get("result") or "{}"
        # lock 밖에서 파싱/직렬화 (큰 result 가 lock 을 오래 잡지 않도록).
        try:
            result_obj = json.loads(result_str)
        except Exception:
            result_obj = {"raw": result_str}
        if slim_fn is not None and isinstance(result_obj, dict):
            with contextlib.suppress(Exception):
                slim_fn(result_obj)
        return json.dumps({
            "job_id": job_id, "status": "done", "elapsed_seconds": elapsed,
            "result": result_obj,
        }, ensure_ascii=False)

    def reset(self) -> None:
        """레지스트리를 비운다 (테스트 격리용)."""
        with self._lock:
            self._jobs.clear()
