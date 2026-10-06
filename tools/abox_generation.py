"""A-Box 생성 도구 — rdflib 기반 RDF 인스턴스 생성

admin-webapp의 abox_generation_service.py를 MCP 도구로 포팅.
T-Box TTL + CSV 데이터로부터 A-Box(인스턴스) TTL을 생성한다.
LLM 불필요 — 순수 rdflib 변환.

이 파일은 Namespace 상수를 import 앞에 정의하고 캐시 블록을 파일 중간에 두는 구조라
E402 를 피할 수 없다. 아래 억제는 그 이유이며 선재 부채(SIM115/SIM118/F841)도 함께
남아 있다 — 새 코드에 적용하지 말 것.
"""
# ruff: noqa: E402, SIM115, SIM118, F841

import csv
import glob
import json
import logging
import os
import re
import time
from datetime import datetime

from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef

# Path B-1: class-specific ID DP 없을 때 fallback 용 표준 vocab
_DCTERMS_NS = Namespace("http://purl.org/dc/terms/")
from config import (
    ABOX_PATH,
    MASTER_DATA_PATH,
    SOURCE_RAWDATA_DIR,
    SOURCE_TACIT_DIR,
    TBOX_PATH,
    resolve_generated_path,
)
from domain.namespaces import (
    DOMAIN_CONFIG,
    DOMAIN_INST_NS,
    DOMAIN_INST_NS_OBJ,
    DOMAIN_NS,
    DOMAIN_NS_OBJ,
    IOF_CORE_NS,
    NS_INST_PREFIX,
    NS_PREFIX,
    ONTOLOGY_URI,
)
from domain.rules_paths import RULES_ROOT, rules_path
from domain.uri_conventions import local_name as _local_name
from tools.abox_support import AboxBuildContext
from tools.common import (
    atomic_write,
    atomic_write_json,
    error_response,
    source_stamp,
    success_response,
)
from tools.provenance import (
    annotate_row_provenance as _annotate_row_provenance,
)
from tools.provenance import (
    build_row_uri_for_abox as _build_row_uri_for_abox,
)
from tools.validation_core import _run_shacl

SH = Namespace("http://www.w3.org/ns/shacl#")
PROV = Namespace("http://www.w3.org/ns/prov#")

logger = logging.getLogger(__name__)

# xsd:time 변환 경고 필터는 domain.tbox_utils 의 import 시 설치된다 (공용).
# 이 모듈에서도 test import 편의를 위해 재노출.
from domain.tbox_utils import _SuppressTimeLiteralConvertWarning  # noqa: F401

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RULES_DIR = RULES_ROOT


# ── A1: FK 퍼지 매칭 설정 로더 ───────────────────────────────────────
def _load_fuzzy_config() -> dict:
    """rules/domain/domain_config.json 에서 fk_fuzzy_match 설정을 로드.

    섹션이 없으면 안전한 기본값 (normalized ON, 나머지 OFF) 반환.
    generate_abox 마다 호출되지만 파일 크기가 작아 I/O 비용 무시 가능.
    """
    defaults = {"normalized": True, "levenshtein": False, "prefix": False}
    _config_path = os.environ.get(
        "DOMAIN_CONFIG_PATH", rules_path("domain_config.json", base=_RULES_DIR),
    )
    try:
        with open(_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        section = cfg.get("fk_fuzzy_match") or {}
        return {
            "normalized": bool(section.get("normalized", defaults["normalized"])),
            "levenshtein": bool(section.get("levenshtein", defaults["levenshtein"])),
            "prefix": bool(section.get("prefix", defaults["prefix"])),
        }
    except Exception:
        return defaults


# R19-16: 도메인 네임스페이스 접근 헬퍼. DOMAIN_NS_OBJ / DOMAIN_INST_NS_OBJ 를
# 코드 곳곳에서 직접 참조하던 것을 아래 2개 함수로 통합. 도메인 교체 시
# 이 두 함수만 바꾸면 되므로 future-proof 한 설계.
def _ns_cls(name: str) -> URIRef:
    """도메인 네임스페이스의 클래스 / DP / OP URI 를 반환."""
    return DOMAIN_NS_OBJ[name]


def _ns_inst(name: str) -> URIRef:
    """도메인 인스턴스 네임스페이스의 URI 를 반환."""
    return DOMAIN_INST_NS_OBJ[name]


# URI local-name 으로 쓸 수 없는 문자(공백 등)를 결정적으로 치환한다.
# 도메인 PK/FK 값에는 'MAT001 081' 처럼 공백이 포함된 경우가 있어 그대로
# DOMAIN_INST_NS_OBJ[...] 에 넣으면 rdflib Turtle 직렬화가 실패한다(2026-06-22 규명).
# PK 인스턴스 URI 와 FK 타깃 candidate URI 양쪽에 **동일하게** 적용해야 FK 가
# 가리키는 URI 가 PK URI 와 정확히 일치한다(매칭 일관성). 정규화(_normalize_fk_value)
# 와 달리 값을 소문자화/구분자제거하지 않고, URI 위반 문자만 '_' 로 치환한다.
_URI_UNSAFE_CHARS = re.compile(r"[\s<>\"{}|\\^`]+")


def _uri_safe_local(value: str) -> str:
    """PK/FK 값 → URI local-name 안전 문자열. 공백 등만 '_' 로 치환."""
    return _URI_UNSAFE_CHARS.sub("_", str(value).strip())


#: PK 값 → IRI local-name 조각. **비-ASCII 문자를 보존** 한다.
#:
#: 예전 패턴 ``[^a-zA-Z0-9_-]`` 는 한글·한자·키릴 등을 전부 ``_`` 로 바꿨다.
#: 그래서 **서로 다른 행이 같은 IRI 로 뭉쳤다** (실측 2026-08-23):
#:
#:     '철강판' → '___'      '구리선' → '___'      → MaterialMaster____ 하나로 병합
#:
#: 두 행의 속성이 한 인스턴스에 섞이고, ``duplicate_pk_rows`` 카운터에만 잡히므로
#: "PK 가 중복인 데이터" 와 구별되지 않는다. IRI 는 RFC 3987 상 Unicode 를 허용하고
#: rdflib/Turtle 라운드트립도 확인했다.
#:
#: ``\w`` 는 ASCII 영숫자·밑줄을 모두 포함하므로 **ASCII 입력에 대한 출력은 예전과
#: 완전히 동일** 하다 (``EQ.001`` → ``EQ_001``). 기존 도메인 IRI 는 바뀌지 않는다.
#: 이름이 소문자인 이유: pre-commit 유출 게이트가 SCREAMING_SNAKE 를 컬럼 코드로
#: 오탐한다 (report.py 의 _debate_absent_html 과 같은 사례).
_pk_unsafe_chars = re.compile(r"[^\w-]", re.UNICODE)


def _pk_safe_local(value: str) -> str:
    """PK 값을 IRI local-name 조각으로. 구두점만 ``_`` 로, 문자는 보존."""
    return _pk_unsafe_chars.sub("_", str(value))


def _load_rules_json(filename: str, *, required: bool = False) -> dict:
    """Load a JSON rule file from the rules/ directory.

    Args:
        filename: rules/ 디렉토리 내 파일 이름.
        required: True 이면 미존재 시 warning 로그. False 여도 파싱 실패는 경고.

    Returns {} on missing. 이전에는 조용히 {} 를 반환해 도메인 스위치 때
    "왜 rules 가 안 적용되지?" 디버깅이 어려웠다. 이제 required=True 이거나
    존재하지만 파싱 실패면 명시적 경고.
    """
    path = rules_path(filename, base=_RULES_DIR)
    if not os.path.exists(path):
        if required:
            logger.warning(
                "rules/%s 파일이 없음 — 기본값 폴백 사용. "
                "도메인 설정이 의도대로 반영되지 않을 수 있음.", filename,
            )
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("rules/%s 로드 실패: %s", filename, e)
        return {}


# 도메인 중립 휴리스틱 규칙 — rules/*.json 에서 로드.
# required=True 로 해서 미존재 시 명시적 경고.
_VALUE_HEURISTICS = _load_rules_json("value_heuristics.json", required=True)
_COMMON_DP_CFG = _load_rules_json("common_dp.json", required=True)

# 공통 DP 정보: name -> {"aliases": [...], "range": uri, "label_*": ..., "comment_*": ...}
_COMMON_DP_BY_NAME: dict[str, dict] = {}
# alias (lowercase) -> 공통 DP name
_COMMON_DP_BY_ALIAS: dict[str, str] = {}
for _dp_decl in _COMMON_DP_CFG.get("common_datatype_properties", []):
    _name = _dp_decl.get("name")
    if not _name:
        continue
    _COMMON_DP_BY_NAME[_name] = _dp_decl
    # name 자체도 alias 로 간주 (lowercase)
    _COMMON_DP_BY_ALIAS[_name.lower()] = _name
    for _alias in _dp_decl.get("aliases", []):
        _COMMON_DP_BY_ALIAS[str(_alias).lower().replace("_", "")] = _name

# 클래스별 필수 DP 요구사항.
# Value 는 list[str | dict] — dict 는 조건부 선언:
#   {"dp": "hasSeverity", "if": {"dp": "hasResult", "op": "!=", "value": "Pass"}}
# 조건 평가: 인스턴스가 {if.dp} 를 갖고 값이 {op}{value} 를 만족할 때만 검사.
#
# Key 는 클래스명 정확매칭 또는 fnmatch 패턴 ("*Monitoring", "Process*" 등).
# pattern 으로 선언하면 새 도메인 재사용 용이.
_REQUIRED_PROPS_BY_CLASS: dict[str, list] = {}
_req_cfg = _COMMON_DP_CFG.get("required_props_by_class", {})
if isinstance(_req_cfg, dict):
    for _cls, _dps in _req_cfg.items():
        if _cls.startswith("_"):
            continue
        if isinstance(_dps, list):
            normalized: list = []
            for _entry in _dps:
                if isinstance(_entry, str):
                    normalized.append({"dp": _entry, "if": None})
                elif isinstance(_entry, dict) and _entry.get("dp"):
                    normalized.append({"dp": str(_entry["dp"]), "if": _entry.get("if")})
            _REQUIRED_PROPS_BY_CLASS[_cls] = normalized


def _resolve_required_props_for_class(cls_name: str) -> list:
    """Resolve required_props specs for a concrete class name, expanding wildcards.

    정확매칭 → wildcard 패턴 순으로 수집. 같은 DP 가 여러 규칙에서 등장하면
    첫 번째 규칙 (정확매칭 우선) 을 채택.
    """
    import fnmatch as _fn
    seen: set[str] = set()
    result: list = []
    # 정확매칭 먼저
    if cls_name in _REQUIRED_PROPS_BY_CLASS:
        for spec in _REQUIRED_PROPS_BY_CLASS[cls_name]:
            dp = spec["dp"]
            if dp not in seen:
                seen.add(dp)
                result.append(spec)
    # wildcard pattern 확장
    for key, specs in _REQUIRED_PROPS_BY_CLASS.items():
        if key == cls_name:
            continue
        if ("*" in key or "?" in key) and _fn.fnmatch(cls_name, key):
            for spec in specs:
                dp = spec["dp"]
                if dp not in seen:
                    seen.add(dp)
                    result.append(spec)
    return result


# 값 정규화 규칙 — rules/contracts/value_normalizations.json
_VALUE_NORMALIZE_CFG = _load_rules_json("value_normalizations.json", required=True)
_NORMALIZE_GLOBAL = _VALUE_NORMALIZE_CFG.get("global_preprocess") or {}
_TRIM_VALUES = bool(_NORMALIZE_GLOBAL.get("trim", True))
_COLLAPSE_WS = bool(_NORMALIZE_GLOBAL.get("collapse_internal_whitespace", False))
_STRIP_BOM = bool(_NORMALIZE_GLOBAL.get("strip_bom", True))

# alias → canonical 역매핑 (col_key lowercase → {alias_lower: canonical})
_ENUM_SYNONYMS: dict[str, dict[str, str]] = {}
_enum_cfg = _VALUE_NORMALIZE_CFG.get("enum_synonyms") or {}
for _col_key, _mapping in _enum_cfg.items():
    if _col_key.startswith("_") or not isinstance(_mapping, dict):
        continue
    _reverse: dict[str, str] = {}
    for _canonical, _aliases in _mapping.items():
        if not isinstance(_aliases, list):
            continue
        for _alias in _aliases:
            _reverse[str(_alias).strip().lower()] = _canonical
        # canonical 자체도 alias 로 간주 (case-insensitive)
        _reverse[str(_canonical).strip().lower()] = _canonical
    _ENUM_SYNONYMS[_col_key.lower()] = _reverse


# 매핑 실패한 enum 값 누적 — (col_lower, value) → count.
# enum_synonyms 가 선언된 컬럼에서 매핑되지 않은 값을 추적해 loss_manifest
# 에 기록. 새 값이 나타났다는 것은 rules 확장 또는 데이터 오류 신호.
_UNKNOWN_ENUM_VALUES: dict[str, int] = {}


def _normalize_value(raw: str, col_name: str | None = None) -> str:
    """Apply declarative value normalization rules.

    - global trim / whitespace collapse / BOM strip
    - enum synonym substitution when ``col_name`` (or its suffix) matches a
      configured key in rules/contracts/value_normalizations.json#enum_synonyms
    - unknown enum values (선언된 컬럼이나 매핑 실패) 는 `_UNKNOWN_ENUM_VALUES`
      에 누적되어 loss_manifest 에 기록된다.
    """
    if raw is None:
        return raw
    v = raw
    if _STRIP_BOM and v.startswith("﻿"):
        v = v.lstrip("﻿")
    if _TRIM_VALUES:
        v = v.strip()
    if _COLLAPSE_WS and v:
        v = re.sub(r"\s+", " ", v)
    if not v or not col_name or not _ENUM_SYNONYMS:
        return v
    col_lower = col_name.lower()
    # 정확 매칭 우선, 그 다음 suffix 매칭
    reverse = _ENUM_SYNONYMS.get(col_lower)
    matched_key = col_lower if reverse is not None else None
    if reverse is None:
        for _k, _rev in _ENUM_SYNONYMS.items():
            if col_lower.endswith(_k):
                reverse = _rev
                matched_key = _k
                break
    if reverse is None:
        return v
    canonical = reverse.get(v.lower())
    if canonical is None:
        # 컬럼이 enum synonyms 에 등록돼 있는데 값이 canonical/alias 목록에
        # 없음 → unknown. 새 값 추적.
        key = f"{matched_key}:{v}"
        _UNKNOWN_ENUM_VALUES[key] = _UNKNOWN_ENUM_VALUES.get(key, 0) + 1
        return v
    return canonical


def _reset_unknown_enum_values() -> None:
    _UNKNOWN_ENUM_VALUES.clear()


def _snapshot_unknown_enum_values() -> dict[str, int]:
    return dict(_UNKNOWN_ENUM_VALUES)


class _BulkNTWriter:
    """Oxigraph store 대상 N-Triples 버퍼 라이터.

    rdflib Graph(store='Oxigraph')에 g.add()를 반복 호출하면 각 호출이
    rdflib→pyoxigraph FFI 왕복을 발생시킨다 (~120K triples/s). 본 라이터는
    메모리 버퍼에 N-Triples 라인을 누적하고 flush() 시 pyoxigraph
    Store.bulk_load로 단일 투입한다 (~460K triples/s, ~4×).

    사용법::

        g = _new_graph()
        w = _BulkNTWriter(g)
        w.add((s, p, o))
        w.add((s, p, o))
        w.flush()  # 이 시점 이후 g에 반영

    Memory store로 폴백할 때는 일반 g.add()로 처리된다.
    """

    __slots__ = ("_g", "_inner", "_to_graph", "_buf", "_count", "_threshold")

    # 개별 배치 최대 크기 (라인 수). 너무 크면 메모리 압박, 너무 작으면
    # bulk_load 호출 오버헤드가 증가. 100K → 수 MB, 실측 최적 수준.
    DEFAULT_FLUSH_THRESHOLD = 100_000

    def __init__(self, g: Graph, threshold: int = DEFAULT_FLUSH_THRESHOLD) -> None:
        self._g = g
        self._threshold = threshold
        self._buf: list[str] = []
        self._count = 0

        inner = getattr(g.store, "_inner", None)
        if inner is None:
            self._inner = None
            self._to_graph = None
            return

        try:
            from pyoxigraph import BlankNode, NamedNode
            from rdflib import BNode as _RBNode
            from rdflib import URIRef as _URef
            gid = g.identifier
            if isinstance(gid, _RBNode):
                self._to_graph = BlankNode(str(gid))
            elif isinstance(gid, _URef):
                self._to_graph = NamedNode(str(gid))
            else:
                self._to_graph = None
            self._inner = inner
        except Exception:
            self._inner = None
            self._to_graph = None

    def add(self, triple: tuple) -> None:
        """Oxigraph store면 N-Triples 버퍼에 누적, 아니면 즉시 g.add()."""
        if self._inner is None:
            self._g.add(triple)
            return
        s, p, o = triple
        self._buf.append(f"{s.n3()} {p.n3()} {o.n3()} .\n")
        self._count += 1
        if self._count >= self._threshold:
            self.flush()

    def flush(self) -> int:
        """버퍼를 Oxigraph store에 일괄 투입하고 버퍼를 비운다."""
        if not self._buf or self._inner is None:
            n = self._count
            self._buf.clear()
            self._count = 0
            return n
        import io

        from pyoxigraph import RdfFormat
        data = "".join(self._buf).encode("utf-8")
        self._inner.bulk_load(
            io.BytesIO(data), RdfFormat.N_TRIPLES, to_graph=self._to_graph,
        )
        n = self._count
        self._buf.clear()
        self._count = 0
        return n


# ── Config 로딩 ───────────────────────────────────


def _load_class_prop_mapping() -> dict:
    """table_class_mapping.json 의 ``class_prop_mapping``. 없으면 빈 dict.

    두 형제 로더 (``_load_table_pk_columns`` / ``_load_table_class_mapping``) 와
    같이 실패를 흡수한다. 예전엔 이 함수만 예외를 그대로 올려, 매핑 파일이 없는
    신규 도메인에서 ``_load_tbox_for_abox`` 의 ``error_response`` 계약을 뚫고
    최상위 blanket handler 까지 올라가 **hint 없는 OS 에러** 로 변환됐다
    (CLAUDE.md §4 의 "원인 → 해결책 → 재개 지점" 안내에 쓸 재료가 사라진다).
    반환값은 멤버십 검사로만 소비되므로 빈 dict 가 안전한 기본값이다.
    """
    from domain.table_mapping import load_class_prop_mapping
    return load_class_prop_mapping(_RULES_DIR)


# table_class_mapping.json 의 "table_pk_columns" (CSV 테이블명 → 권위 PK 컬럼 리스트).
# DDL 에서 추출한 명시적 PK 로, 휴리스틱 _detect_pk_column 보다 우선한다. 데이터
# 정제 과정에서 상수 컬럼이 제거되면 휴리스틱이 audit 컬럼을 PK 로 오선택할 수
# 있고, 그러면 서로 다른 행이 소수의 인스턴스로 뭉친다. 명시적 PK 가 이것을 막는다.
_TABLE_PK_COLUMNS_CACHE: dict | None = None
_TABLE_PK_COLUMNS_MTIME: float = 0.0


def _table_mapping_mtime() -> float:
    """table_class_mapping.json 의 mtime (없으면 0.0)."""
    path = rules_path("table_class_mapping.json", base=_RULES_DIR)
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _load_table_pk_columns() -> dict:
    """CSV 테이블명 → 명시적 PK 컬럼 리스트 매핑 (DDL 권위 소스).

    파일 mtime 이 바뀌면 캐시를 버린다. 장수명 MCP 서버 프로세스가 매핑을
    한 번만 읽고 영구 캐시하면, 운영 중 매핑을 고쳐도 서버 재시작 전까지
    반영되지 않는다. 그동안에는 새로 등록한 테이블 PK 대신 구 캐시의 휴리스틱
    PK 가 쓰여, 여러 행이 소수의 인스턴스로 뭉칠 수 있다.
    """
    global _TABLE_PK_COLUMNS_CACHE, _TABLE_PK_COLUMNS_MTIME
    mtime = _table_mapping_mtime()
    if _TABLE_PK_COLUMNS_CACHE is not None and mtime == _TABLE_PK_COLUMNS_MTIME:
        return _TABLE_PK_COLUMNS_CACHE
    from domain.table_mapping import load_table_pk_columns
    result = load_table_pk_columns(_RULES_DIR)
    _TABLE_PK_COLUMNS_CACHE = result
    _TABLE_PK_COLUMNS_MTIME = mtime
    return result


def _resolve_explicit_pk(
    filename: str, rows: list[dict]
) -> "str | list[str] | None":
    """명시적 권위 PK(table_pk_columns)를 해석한다.

    설정의 PK 컬럼이 실제 CSV 헤더에 모두 존재할 때만 채택하고, 하나라도
    없으면 None 을 반환해 휴리스틱 _detect_pk_column 으로 폴백한다. 반환값은
    _detect_pk_column 의 계약과 동일하게 lowercase 컬럼명(단일 str / 복합
    list[str]) 으로 정규화해 _detect_pk_value / _emit_pk_identifier 가
    row_lower 로 조회할 수 있게 한다.
    """
    configured = _load_table_pk_columns().get(filename)
    if not configured or not rows:
        return None
    headers_lower = {k.lower() for k in rows[0].keys()}
    pk_lower = [c.lower() for c in configured]
    missing = [c for c in pk_lower if c not in headers_lower]
    if missing:
        logger.warning(
            "테이블 %s: 명시적 PK 컬럼 %s 가 CSV 에 없음 — 휴리스틱 PK 로 폴백",
            filename, missing,
        )
        return None
    return pk_lower[0] if len(pk_lower) == 1 else pk_lower


# table_class_mapping.json 의 "table_class_mapping" (CSV 테이블명 → 도메인 클래스).
# A-Box 인스턴스 타이핑의 single source of truth. 이 매핑이 없으면 _table_to_class
# 가 PascalCase 테이블명 클래스로 폴백하는데, T-Box 에 S3(Step12d) 가 만든 테이블명
# 클래스(예: 원본 테이블명을 그대로 딴 고립 클래스)가 있으면 인스턴스가 그 고립
# 클래스로 들어가 FK/OP 가 도메인 클래스를 못 찾아 전부 unresolved 가 된다.
_TABLE_CLASS_MAP_CACHE: dict | None = None
_TABLE_CLASS_MAP_MTIME: float = 0.0


def _load_table_class_mapping() -> dict:
    """CSV 테이블명 → 도메인 클래스 local name 매핑 ('steel:' prefix 제거).

    ``_load_table_pk_columns`` 와 같은 이유로 mtime 기반 무효화를 적용한다.
    """
    global _TABLE_CLASS_MAP_CACHE, _TABLE_CLASS_MAP_MTIME
    mtime = _table_mapping_mtime()
    if _TABLE_CLASS_MAP_CACHE is not None and mtime == _TABLE_CLASS_MAP_MTIME:
        return _TABLE_CLASS_MAP_CACHE
    from domain.table_mapping import load_table_class_mapping
    result = load_table_class_mapping(_RULES_DIR)
    _TABLE_CLASS_MAP_CACHE = result
    _TABLE_CLASS_MAP_MTIME = mtime
    return result


def _load_object_properties() -> list[dict]:
    """생성된 T-Box에서 ObjectProperty 정보를 동적 추출한다."""
    from domain.tbox_utils import load_object_properties
    return load_object_properties()


# ── T-Box 파싱 ────────────────────────────────────


def _parse_tbox(tbox_ttl: str) -> dict:
    """T-Box TTL을 rdflib로 파싱하여 클래스/프로퍼티 매핑 추출."""
    import rdflib.collection as _rcoll

    g = _new_graph()
    g.parse(data=tbox_ttl, format="turtle")

    classes = {}
    for cls in g.subjects(RDF.type, OWL.Class):
        name = _local_name(str(cls))
        if name:
            classes[name.lower()] = name

    def _expand_domain_names(domain_node) -> set[str]:
        """Return set of named class local names from a domain node.

        Named class → {its local name}. unionOf bnode → members' local names.
        """
        if isinstance(domain_node, URIRef):
            nm = _local_name(str(domain_node))
            return {nm} if nm else set()
        names: set[str] = set()
        for u_list in g.objects(domain_node, OWL.unionOf):
            try:
                for m in _rcoll.Collection(g, u_list):
                    if isinstance(m, URIRef):
                        nm = _local_name(str(m))
                        if nm:
                            names.add(nm)
            except Exception:  # noqa: BLE001 — 깨진 unionOf domain은 해당 목록만 건너뛴다
                pass
        return names

    datatype_properties = {}
    class_props = {}
    dp_domains: dict[str, set[str]] = {}  # prop_local_lower -> {domain_local}
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        name = _local_name(str(prop))
        if not name:
            continue
        datatype_properties[name.lower()] = name
        for domain in g.objects(prop, RDFS.domain):
            for domain_name in _expand_domain_names(domain):
                class_props.setdefault(domain_name, {})[name.lower()] = name
                dp_domains.setdefault(name.lower(), set()).add(domain_name)

    object_properties = {}
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        name = _local_name(str(prop))
        if not name:
            continue
        object_properties[name.lower()] = name
        for domain in g.objects(prop, RDFS.domain):
            for domain_name in _expand_domain_names(domain):
                class_props.setdefault(domain_name, {})[name.lower()] = name

    dp_ranges = {}
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        name = _local_name(str(prop))
        if not name:
            continue
        for range_val in g.objects(prop, RDFS.range):
            dp_ranges[name.lower()] = str(range_val)

    # ── 출처 컬럼 인덱스 (dcterms:source) ──────────────────────────────
    # T-Box 생성 프롬프트 (04-property-rules.md 의 "DatatypeProperty declaration" 절)
    # 가 모든 DP 에 `dcterms:source "<CSV_COLUMN_CODE>"` 를 필수로 요구한다. 이 표기는 DP 이름
    # 추측을 없애는 authoritative 매핑 — LLM 이 권위 한글명 기반으로 의미 있는
    # 이름 (processStepABottomNozzleFlowQty1) 을 짓더라도 원본 컬럼
    # (QTY_COL_1) 과 직접 연결된다. 표기 없는 DP 는 이 인덱스에
    # 들어오지 않고 기존 transliteration 경로로 폴백한다 (하위 호환).
    #
    # 키는 (domain_class, UPPER(column)) — 같은 컬럼명이 여러 테이블에 등장해
    # 각각 다른 클래스의 DP 가 되므로 클래스로 분리해야 한다.
    dp_by_source: dict[tuple[str, str], str] = {}
    source_conflicts: dict[tuple[str, str], set[str]] = {}
    for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
        name = _local_name(str(prop))
        if not name:
            continue
        columns = {
            str(src).strip().upper()
            for src in g.objects(prop, _DCTERMS_NS.source)
            if str(src).strip()
        }
        if not columns:
            continue
        domains = set()
        for domain in g.objects(prop, RDFS.domain):
            domains |= _expand_domain_names(domain)
        for domain_name in domains or {""}:
            for column in columns:
                key = (domain_name, column)
                existing = dp_by_source.get(key)
                if existing is None:
                    dp_by_source[key] = name
                elif existing != name:
                    # 두 DP 가 같은 (class, column) 을 주장 — 어느 쪽이 정본인지
                    # 판별 불가하므로 양쪽 모두 신뢰하지 않고 폴백에 맡긴다.
                    source_conflicts.setdefault(key, {existing}).add(name)
    for key in source_conflicts:
        dp_by_source.pop(key, None)
    if source_conflicts:
        logger.warning(
            "dcterms:source 충돌 %d건 — 해당 컬럼은 transliteration 폴백 사용 "
            "(예: %s)",
            len(source_conflicts),
            list(source_conflicts.items())[:3],
        )

    # subClassOf 관계 수집 (domain 호환성 체크용)
    subclass_of: dict[str, set[str]] = {}
    for s, o in g.subject_objects(RDFS.subClassOf):
        s_name = _local_name(str(s))
        o_name = _local_name(str(o))
        if s_name and o_name:
            subclass_of.setdefault(s_name, set()).add(o_name)

    return {
        "classes": classes,
        "datatype_properties": datatype_properties,
        "object_properties": object_properties,
        "class_props": class_props,
        "dp_ranges": dp_ranges,
        "dp_domains": dp_domains,
        "subclass_of": subclass_of,
        "dp_by_source": dp_by_source,
        "dp_source_conflicts": {k: sorted(v) for k, v in source_conflicts.items()},
    }


def _class_matches_domain(class_name: str, domains: set[str], tbox_info: dict) -> bool:
    """해당 클래스가 DP domain 집합 중 하나와 호환되는지 확인.

    호환 = class_name 자신 OR 그 조상(subClassOf chain) 이 domain 에 포함.
    비어있는 domains 는 항상 호환 (domain 미지정 = owl:Thing).
    """
    if not domains:
        return True
    if class_name in domains:
        return True
    sub = tbox_info.get("subclass_of", {})
    seen = {class_name}
    stack = [class_name]
    while stack:
        cur = stack.pop()
        for sup in sub.get(cur, ()):
            if sup in domains:
                return True
            if sup not in seen:
                seen.add(sup)
                stack.append(sup)
    return False


# ── FK 감지 ───────────────────────────────────────


_TIMESTAMP_COLUMNS = set(
    _VALUE_HEURISTICS.get("timestamp_columns")
    or [
        "timestamp", "datetime", "measurementdatetime", "testdatetime",
        "occurrencedatetime", "departuretime", "generationdate",
        "maintenancedate", "orderdate", "plandate",
    ],
)

# 클래스명에서 PK 후보 base 를 떼기 위한 suffix 목록. `_detect_pk_column`,
# `_detect_pk_value`, provenance PK 컬럼 추정 등 여러 곳에서 공유된다.
_PK_CLASS_SUFFIXES: tuple[str, ...] = tuple(
    _VALUE_HEURISTICS.get("pk_class_suffixes")
    or (
        "master", "map", "history", "results", "events", "data",
        "status", "quality", "plan", "result", "monitoring",
        "management", "transaction",
    ),
)

# 파일명 suffix → 테이블 분류 (transaction / master)
_TRANSACTION_FILENAME_SUFFIXES: tuple[str, ...] = tuple(
    _VALUE_HEURISTICS.get("transaction_filename_suffixes")
    or (
        "_History", "_Transaction", "_Plan", "_Status", "_Results", "_Events",
        "_Data", "_Monitoring",
        # 비제조 도메인의 흔한 트랜잭션 접미 — 없으면 파일명 판정이 실패한다.
        "_Records", "_Record", "_Log", "_Logs", "_Entries",
    ),
)
_MASTER_FILENAME_SUFFIXES: tuple[str, ...] = tuple(
    _VALUE_HEURISTICS.get("master_filename_suffixes")
    or ("_Master", "_Map", "_Codes", "_Code", "_Types", "_Dim", "_Dimension"),
)


def _detect_timestamp(row_lower: dict) -> str | None:
    """CSV 행에서 타임스탬프 값을 감지한다."""
    # 1. 알려진 타임스탬프 컬럼
    for col in _TIMESTAMP_COLUMNS:
        if col in row_lower:
            val = str(row_lower[col]).strip()
            if val:
                return _pk_safe_local(val)
    # 2. 패턴 매칭 (*date*, *time*)
    for col, val in row_lower.items():
        if "date" in col or "time" in col:
            val = str(val).strip()
            if val and len(val) >= 10:  # 최소 날짜 길이
                return _pk_safe_local(val)
    return None


def _detect_pk_column(rows: list[dict], class_name: str) -> str | None | list[str]:
    """CSV 전체 행에서 PK 컬럼을 uniqueness 기반으로 선택한다.

    후보 컬럼을 모두 수집한 뒤, distinct value 비율이 가장 높은 컬럼을 PK로 선택.
    단일 컬럼으로 완전 unique 하지 않으면(distinct_ratio < 1.0) id/code 후보를
    순차로 조합해 복합 키(list[str])를 반환한다.

    Returns:
        - 단일 PK 컬럼명 (str) — unique 가능 시
        - 복합 PK 컬럼 list[str] — 단일로 unique 불가 시 (e.g. InventoryStatus:
          [item_code, warehouse_code, zone_code])
        - None — PK 후보 없음
    """
    if not rows:
        return None
    headers_lower = {k.lower(): k for k in rows[0].keys()}
    total = len(rows)
    if total == 0:
        return None

    candidates: list[str] = []

    # 1. 클래스명 기반 후보: 모든 suffix 매칭 (break 없이 전부 수집)
    class_lower = class_name.lower()
    for suffix in _PK_CLASS_SUFFIXES:
        if class_lower.endswith(suffix):
            base = class_lower[: -len(suffix)]
            if base:
                candidate = f"{base}id"
                if candidate in headers_lower:
                    candidates.append(candidate)

    # 1b. 클래스명 토큰 기반 후보: 클래스명이 PK 컬럼명과 직접 정렬되는 흔한
    #     컨벤션 ({Class}Id) 을 우선 후보로 수집한다. CamelCase 클래스명을
    #     토큰으로 분해해 "{tokens}id" 형태 ( split_materialA_id / splitmateriala_id )
    #     를 매칭. 여러 컬럼이 모두 unique 인 테이블 (audit 컬럼 id_col_3 /
    #     id_col_5 가 함께 100% unique) 에서 진짜 엔티티 PK 가
    #     audit 컬럼에 밀려 선택되던 문제를 막는다.
    _cls_tokens = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|[0-9]+", class_name)
    if _cls_tokens:
        joined = "".join(t.lower() for t in _cls_tokens)
        for cand in (f"{joined}id", f"{joined}code"):
            if cand in headers_lower and cand not in candidates:
                candidates.append(cand)

    # 2. 모든 id/code 컬럼 수집
    for col_lower in headers_lower:
        if (col_lower.endswith("id") or col_lower.endswith("code")) and col_lower not in candidates:
            candidates.append(col_lower)

    if not candidates:
        return None

    # 3. Uniqueness 순위: distinct values / total rows.
    #    동률 ratio 에서는 (a) audit/시스템 컬럼 (created_*, last_updated_*,
    #    *_object_id, *_program_id, transaction_id, wip_entity_id) 을 뒤로,
    #    (b) 후보 리스트 순서 (클래스명 매칭 후보가 앞) 를 존중한다. strict
    #    `>` 비교라 동률이면 먼저 평가된 후보가 유지되므로, 후보를 audit 여부 +
    #    원래 순서로 안정 정렬한 뒤 평가한다.
    #
    #    아래 토큰은 **ERP/MES 가 공통으로 쓰는 감사 컬럼명** 이며 특정 배포
    #    스키마에서 온 것이 아니다. 실제 컬럼명이어야 매칭되므로 가명으로 바꾸면
    #    PK 감지가 깨져 감사 컬럼이 엔티티 PK 로 선택된다.
    _AUDIT_PK_TOKENS = (
        "created_object_id", "created_program_id", "last_updated_object_id",
        "last_update_program_id", "transaction_id", "wip_entity_id",
        "archived_employee_num",
    )

    def _is_audit(col_lower: str) -> bool:
        return (
            col_lower in _AUDIT_PK_TOKENS
            or col_lower.endswith("object_id")
            or col_lower.endswith("program_id")
        )

    ordered_candidates = sorted(
        enumerate(candidates), key=lambda x: (_is_audit(x[1]), x[0])
    )
    best_col = None
    best_ratio = -1.0
    for _idx, col_lower in ordered_candidates:
        orig_col = headers_lower[col_lower]
        values = [str(row.get(orig_col, "")).strip() for row in rows if row.get(orig_col)]
        if not values:
            continue
        ratio = len(set(values)) / total
        if ratio > best_ratio:
            best_ratio = ratio
            best_col = col_lower

    if best_col is None:
        return None

    # 4. 단일 PK 가 unique 하면 그대로 반환
    if best_ratio >= 1.0:
        return best_col

    # 5. 복합 키 시도 — best_col + 다른 id/code 컬럼들을 순차 결합해 unique 가 되는
    #    최소 조합을 찾는다. InventoryStatus: (item_code, warehouse_code, zone_code).
    #
    #    결정론 보장: 동점 unique_count 에서 컬럼 순서에 따라 결과가 달라지지
    #    않도록, 후보를 알파벳 순으로 정렬하고 `(unique_count, col_name)` 튜플을
    #    이용해 tie-break 한다. 이전에는 `other` 리스트가 입력 dict 순서를
    #    그대로 따라 PK 결정이 비결정적이었다.
    composite = [best_col]
    # best_col 제외한 id/code 후보
    other = sorted(c for c in candidates if c != best_col)
    # timestamp/복합 키 확장 후보도 alphabetical 순으로 merge
    _extra_candidates = sorted({
        "zone_code", "warehouse_code", "timestamp",
        "datetime", "location", "eventid", "event_id",
    })
    for extra_candidate in _extra_candidates:
        if extra_candidate in headers_lower and extra_candidate not in composite and extra_candidate not in other:
            other.append(extra_candidate)
    while True:
        best_extra: str | None = None
        best_extra_key: tuple[int, str] | None = None
        cur_unique = len(set(zip(*[[str(r.get(headers_lower[c], "")).strip() for r in rows] for c in composite])))
        for c in other:
            if c in composite:
                continue
            trial = composite + [c]
            tuples = list(zip(*[[str(r.get(headers_lower[cc], "")).strip() for r in rows] for cc in trial]))
            unique = len(set(tuples))
            if unique <= cur_unique:
                continue
            # 동점 unique 에서는 컬럼명 사전순 선택 → 결정론 보장
            key = (unique, c)
            if best_extra_key is None or key > best_extra_key:
                best_extra_key = key
                best_extra = c
        if best_extra is None:
            break
        composite.append(best_extra)
        if best_extra_key and best_extra_key[0] >= total:
            return composite
    # 복합으로도 완전 unique 가 안 되면 best_col 단일 반환 (과거 호환)
    return composite if len(composite) > 1 else best_col


def _detect_pk_value(row: dict, class_name: str,
                     pk_column: "str | list[str] | None" = None,
                     pk_is_authoritative: bool = False) -> str | None:
    """CSV 행에서 PK 값을 감지하여 반환한다.

    Args:
        pk_column: _detect_pk_column 으로 미리 결정된 PK 컬럼.
                   단일 str 이거나 복합 키 list[str]. 복합 키이면 값들을
                   '_' 로 연결해 단일 문자열로 반환.
        pk_is_authoritative: True 면 pk_column 이 DDL 권위 PK(table_pk_columns)라
                   uniqueness 가 사전 검증된 것으로 보고, 복합 키여도 timestamp
                   tie-breaker 접미를 생략한다. FK 가 단일값으로 참조하는 타겟의
                   PK 와 IRI 가 어긋나 stub 인스턴스가 생기는 문제를 막는다.

    Notes:
        - 단일 str PK 컬럼이 주어진 경우(uniqueness=1.0 으로 확정) 그 값만으로
          URI 가 결정되며 timestamp 접미를 붙이지 않는다. `TXN00001` 이 이미
          unique 한데 `TXN00001_2025-09-01T17:04:35` 처럼 접미하면 인스턴스
          동일성 테스트가 깨진다.
        - 복합 PK 또는 PK 미지정 폴백 경로에서는 timestamp 접미가 중복 행
          충돌을 줄이는 tie-breaker 로 남아 있다.
    """
    row_lower = {k.lower(): v for k, v in row.items() if v and str(v).strip()}

    pk_value = None
    pk_is_unique_single = False
    # 2026-05-10 fix: composite PK 가 timestamp 컬럼을 이미 포함하면 아래에서
    # _detect_timestamp 접미를 skip — 이중 반복 방지.
    # (이 플래그는 composite 경로에서만 True 로 설정됨.)
    composite_has_ts = False

    # 복합 키 처리
    if isinstance(pk_column, list) and pk_column:
        parts: list[str] = []
        for col in pk_column:
            v = row_lower.get(col)
            if v is None or not str(v).strip():
                parts = []
                break
            parts.append(_pk_safe_local(str(v).strip()))
        if parts:
            pk_value = "_".join(parts)
            # composite 구성 컬럼 중 하나라도 timestamp 계열이면 flag on.
            composite_has_ts = any(
                (col.lower() in _TIMESTAMP_COLUMNS)
                or ("timestamp" in col.lower())
                or ("datetime" in col.lower())
                for col in pk_column
            )

    # 단일 PK 컬럼 사용 — uniqueness=1.0 이 보장된 케이스이므로 timestamp 생략
    if pk_value is None and isinstance(pk_column, str) and pk_column in row_lower:
        val = str(row_lower[pk_column]).strip()
        if val:
            pk_value = _pk_safe_local(val)
            pk_is_unique_single = True

    # 폴백: 기존 suffix 방식
    if pk_value is None:
        class_lower = class_name.lower()
        for suffix in _PK_CLASS_SUFFIXES:
            if class_lower.endswith(suffix):
                base = class_lower[: -len(suffix)]
                if base:
                    candidate = f"{base}id"
                    if candidate in row_lower:
                        val = str(row_lower[candidate]).strip()
                        if val:
                            pk_value = _pk_safe_local(val)
                break

    if pk_value is None:
        for col, val in row_lower.items():
            if col.endswith("id") or col.endswith("code"):
                val = str(val).strip()
                if val:
                    pk_value = _pk_safe_local(val)
                    break

    if pk_value is not None:
        if pk_is_unique_single:
            return pk_value
        # 권위 PK(table_pk_columns)는 uniqueness 가 사전 검증됐으므로 복합이어도
        # timestamp 접미 생략 — FK 단일값 참조와 IRI 정합 유지.
        if pk_is_authoritative:
            return pk_value
        # composite PK 에 timestamp 가 이미 포함되어 있으면 중복 접미 방지.
        if composite_has_ts:
            return pk_value
        ts = _detect_timestamp(row_lower)
        if ts:
            return f"{pk_value}_{ts}"
        return pk_value

    return None


def _load_fk_patterns() -> tuple[dict[str, str], list[dict], dict[str, dict]]:
    _path = rules_path("fk_patterns.json", base=_RULES_DIR)
    with open(_path, encoding="utf-8") as f:
        cfg = json.load(f)
    # fk_value_transforms: 컬럼 한정 결정적 값 변환 (전역 fk_fuzzy_match 와 별개).
    # "_comment" 같은 메타 키는 건너뛰고 dict 엔트리만 채택.
    transforms = {
        col: spec
        for col, spec in (cfg.get("fk_value_transforms") or {}).items()
        if not col.startswith("_") and isinstance(spec, dict)
    }
    return cfg.get("patterns", {}), cfg.get("suffix_rules", []), transforms

_FK_PATTERNS, _FK_SUFFIX_RULES, _FK_VALUE_TRANSFORMS = _load_fk_patterns()
# fk_patterns.json 의 디스크 mtime — 변경 감지용 (dimension_config 와 동일 패턴).
_FK_PATTERNS_MTIME: float = (
    os.path.getmtime(rules_path("fk_patterns.json", base=_RULES_DIR))
    if os.path.exists(rules_path("fk_patterns.json", base=_RULES_DIR))
    else 0.0
)


def _refresh_fk_patterns_if_stale() -> None:
    """fk_patterns.json 이 디스크에서 바뀌면 전역 FK 패턴을 재로드.

    _FK_PATTERNS 등은 모듈 import 시 1회 로드되므로, 장수 서버(MCP)가 뜬 뒤
    파일을 편집해도 반영되지 않았다. generate_abox 진입부에서 mtime 을 확인해
    변경 시에만 재로드한다 (dimension_config 의 mtime 캐시와 동일 전략).
    서버 재시작 없이 FK 패턴 수정이 즉시 반영된다.
    """
    global _FK_PATTERNS, _FK_SUFFIX_RULES, _FK_VALUE_TRANSFORMS, _FK_PATTERNS_MTIME
    path = rules_path("fk_patterns.json", base=_RULES_DIR)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    if mtime == _FK_PATTERNS_MTIME:
        return
    _FK_PATTERNS, _FK_SUFFIX_RULES, _FK_VALUE_TRANSFORMS = _load_fk_patterns()
    _FK_PATTERNS_MTIME = mtime
    logger.info("fk_patterns.json 변경 감지 — FK 패턴 재로드 (%d 패턴)", len(_FK_PATTERNS))


def _apply_fk_value_transform(fk_col_normalized: str, fk_value: str) -> str:
    """Apply a column-scoped deterministic transform to a FK value.

    Looks up ``fk_col_normalized`` in rules/contracts/fk_patterns.json#fk_value_transforms
    and applies the declared transform before the value is matched against
    master PKs. Currently supports ``strip_suffix_chars`` (drop the last N
    characters). Unknown / absent columns return the value unchanged, so this
    only affects FK columns explicitly declared in the rules file.
    """
    spec = _FK_VALUE_TRANSFORMS.get(fk_col_normalized)
    if not spec:
        return fk_value
    n = spec.get("strip_suffix_chars")
    if isinstance(n, int) and n > 0 and len(fk_value) > n:
        return fk_value[:-n]
    return fk_value


_FK_FALLBACK_LOGGED: set[tuple[str, str, str]] = set()


def _log_fk_fallback(original: str, target: str, rule: str) -> None:
    """Log FK fallback matches once per (original, target, rule)."""
    key = (original, target, rule)
    if key in _FK_FALLBACK_LOGGED:
        return
    _FK_FALLBACK_LOGGED.add(key)
    logger.info("FK fallback: %s → %s via %s", original, target, rule)


def _fk_column_to_class(col_lower: str) -> str | None:
    """FK 컬럼명 → 타깃 클래스명 변환. rules/contracts/fk_patterns.json 기반.

    1) exact pattern → 2) suffix rule → 3) separator 제거 → 4) underscore
    제거 → None. 폴백 경로(3/4)가 적중하면 `logger.info` 로 1회 기록해
    rules/contracts/fk_patterns.json 에 정식 엔트리로 올려야 하는 케이스를
    발견할 수 있게 한다.
    """
    # 1. 정확 매칭
    if col_lower in _FK_PATTERNS:
        return _FK_PATTERNS[col_lower]
    # 2. suffix 규칙 폴백
    for rule in _FK_SUFFIX_RULES:
        suffix = rule["suffix"]
        if col_lower.endswith(suffix):
            base = col_lower[:-len(suffix)]
            class_name = "".join(w.capitalize() for w in base.split("_"))
            return rule["template"].format(base=class_name)
    # 3. separator 제거 폴백: 하이픈/점 제거 후 재시도
    cleaned = col_lower.replace("-", "").replace(".", "")
    if cleaned != col_lower:
        if cleaned in _FK_PATTERNS:
            target = _FK_PATTERNS[cleaned]
            _log_fk_fallback(col_lower, target, "separator_strip+exact")
            return target
        for rule in _FK_SUFFIX_RULES:
            suffix = rule["suffix"]
            if cleaned.endswith(suffix):
                base = cleaned[:-len(suffix)]
                class_name = "".join(w.capitalize() for w in base.split("_"))
                target = rule["template"].format(base=class_name)
                _log_fk_fallback(col_lower, target, f"separator_strip+suffix({suffix})")
                return target
    # 4. underscore-joined 폴백: item_master_id → itemmasterid
    joined = col_lower.replace("_", "")
    if joined != col_lower and joined not in _FK_PATTERNS:
        for rule in _FK_SUFFIX_RULES:
            suffix = rule["suffix"]
            if joined.endswith(suffix) and len(joined) > len(suffix):
                base = joined[:-len(suffix)]
                class_name = "".join(w.capitalize() for w in base.split("_"))
                target = rule["template"].format(base=class_name)
                _log_fk_fallback(col_lower, target, f"underscore_strip+suffix({suffix})")
                return target
    return None


# T-Box 캐시군 — 원래 _tbox_classes_cache / _subclass_cache /
# _class_restriction_props_cache / _fk_skeleton_cache 가 각각 독립된
# mtime + Lock 을 들고 있어 동기화 누락 위험이 있었다. 아래 한 개의
# shared mtime anchor 와 Lock 으로 단일 진실원을 만든다. 각 캐시의
# 빌더 함수는 여전히 개별 dict 를 채우지만, 무효화는 `invalidate_tbox_caches`
# 가 공통으로 처리한다.
import threading as _threading

from domain.tbox_utils import _new_graph

_tbox_cache_lock = _threading.Lock()
_tbox_cache_mtime: float = 0.0


def _current_tbox_mtime() -> float:
    try:
        return os.path.getmtime(TBOX_PATH) if os.path.exists(TBOX_PATH) else 0.0
    except OSError:
        return 0.0


def _invalidate_tbox_caches_if_stale() -> None:
    """Clear all TBox-derived caches if TBOX_PATH mtime changed.

    Callers hold ``_tbox_cache_lock`` while running this if they mutate caches.
    """
    global _tbox_cache_mtime
    global _tbox_classes_cache, _tbox_classes_mtime
    global _subclass_cache, _subclass_cache_mtime
    global _class_restriction_props_cache, _class_restriction_props_mtime
    cur = _current_tbox_mtime()
    if cur == _tbox_cache_mtime:
        return
    _tbox_cache_mtime = cur
    _tbox_classes_cache = None
    _tbox_classes_mtime = cur
    _subclass_cache = None
    _subclass_cache_mtime = cur
    _class_restriction_props_cache = None
    _class_restriction_props_mtime = cur
    _fk_skeleton_cache.clear()


_tbox_classes_cache: set[str] | None = None
_tbox_classes_mtime: float = 0
_tbox_classes_lock = _threading.Lock()


def _class_exists_in_tbox(class_name: str) -> bool:
    """T-Box에 해당 클래스가 정의되어 있는지 확인한다. mtime 기반 캐시 (스레드 안전).

    DOMAIN_NS 네임스페이스에 한정하지 않고 T-Box 에 선언된 모든 OWL.Class 를
    수집한다. IOF 코어 클래스 (IOF_CORE_NS) 가 FK 타겟으로 등장하는 경우를
    놓치지 않기 위함. 로컬 이름이 클래스의 유일성을 담보하지 않을 수 있으나
    현재 T-Box 컨벤션(steel: 와 iof-core: 가 PascalCase 충돌 없음)에서는
    안전하다. 충돌이 생기면 `_fk_column_to_class` 가 반환하는 namespace
    정보를 함께 비교하도록 확장해야 한다.
    """
    global _tbox_classes_cache, _tbox_classes_mtime
    with _tbox_classes_lock:
        if os.path.exists(TBOX_PATH):
            current_mtime = os.path.getmtime(TBOX_PATH)
            if _tbox_classes_cache is None or _tbox_classes_mtime != current_mtime:
                g = _new_graph()
                g.parse(TBOX_PATH, format="turtle")
                names: set[str] = set()
                for c in g.subjects(RDF.type, OWL.Class):
                    if not isinstance(c, URIRef):
                        continue
                    nm = _local_name(str(c))
                    if nm:
                        names.add(nm)
                _tbox_classes_cache = names
                _tbox_classes_mtime = current_mtime
    return _tbox_classes_cache is not None and class_name in _tbox_classes_cache


# subClassOf 계층 캐시 (FK 매칭에서 상위 클래스 OP도 탐색용)
_subclass_cache: dict[str, set[str]] | None = None
_subclass_cache_mtime: float = 0
_subclass_cache_lock = _threading.Lock()


def _get_superclasses(class_name: str) -> set[str]:
    """클래스의 모든 상위 클래스(직접+간접)를 반환한다. 자기 자신도 포함.

    steel: 및 IOF 네임스페이스의 상위 클래스를 모두 포함한다.
    """
    global _subclass_cache, _subclass_cache_mtime

    with _subclass_cache_lock:
        # TBOX_PATH mtime 변경 시 캐시 무효화
        current_mtime: float = 0
        if os.path.exists(TBOX_PATH):
            current_mtime = os.path.getmtime(TBOX_PATH)
        if _subclass_cache is not None and current_mtime != _subclass_cache_mtime:
            logger.info(
                "T-Box 파일 변경 감지 (mtime %.1f → %.1f), subclass 캐시 무효화",
                _subclass_cache_mtime, current_mtime,
            )
            _subclass_cache = None

        if _subclass_cache is None:
            _subclass_cache_mtime = current_mtime
            _subclass_cache = {}
            try:
                g = _new_graph()
                g.parse(TBOX_PATH, format="turtle")
                for child in g.subjects(RDFS.subClassOf, None):
                    child_name = _local_name(str(child))
                    if not child_name:
                        continue
                    parents = _subclass_cache.setdefault(child_name, set())
                    for parent in g.objects(child, RDFS.subClassOf):
                        parent_name = _local_name(str(parent))
                        if parent_name:
                            parents.add(parent_name)
                max_iterations = len(_subclass_cache) + 1
                for _iteration in range(max_iterations):
                    changed = False
                    for cls, parents in _subclass_cache.items():
                        expanded = set()
                        for p in parents:
                            if p == cls:
                                continue
                            expanded |= _subclass_cache.get(p, set())
                        expanded.discard(cls)
                        new = expanded - parents
                        if new:
                            parents |= new
                            changed = True
                    if not changed:
                        break
                else:
                    logger.warning("subClassOf transitive closure: max iterations reached, possible cycle")
            except Exception as e:
                logger.warning("subClassOf cache build failed: %s", e)
    return _subclass_cache.get(class_name, set()) | {class_name}


# _detect_fk_property 결과의 "스켈레톤"(fk_value 무관 부분) 메모이제이션 캐시.
# (source_class, fk_col_normalized) → (target_class, [(kind, prop, inverse?), ...], unverified_target)
# 같은 테이블의 같은 FK 컬럼이 수만 행 반복되어도 domain/range/_get_superclasses 계산은 1회만.
_fk_skeleton_cache: "dict[tuple[str, str], tuple | None]" = {}


# class → {Restriction onProperty 로컬 이름 집합} 캐시.
# FK 후보 선정 시 "Jury 가 minCardinality 등 제약을 건 OP" 를 우선 선택하려
# 할 때 사용. mtime 기반 무효화.
_class_restriction_props_cache: dict[str, set[str]] | None = None
_class_restriction_props_mtime: float = 0.0
_class_restriction_props_lock = _threading.Lock()


def _get_source_class_restriction_props(class_name: str) -> set[str]:
    """해당 클래스에 걸린 owl:Restriction 의 onProperty 집합을 반환.

    subClassOf 체인을 따라 상위 클래스의 Restriction 도 포함한다 (OWL 2 상속).
    """
    global _class_restriction_props_cache, _class_restriction_props_mtime
    with _class_restriction_props_lock:
        current_mtime: float = (
            os.path.getmtime(TBOX_PATH) if os.path.exists(TBOX_PATH) else 0.0
        )
        if (_class_restriction_props_cache is None
                or current_mtime != _class_restriction_props_mtime):
            _class_restriction_props_cache = {}
            _class_restriction_props_mtime = current_mtime
            try:
                g = _new_graph()
                g.parse(TBOX_PATH, format="turtle")
                # 각 클래스 → Restriction 모음. superclass Restriction 도 수집.
                direct: dict[str, set[str]] = {}
                for cls, _, parent in g.triples((None, RDFS.subClassOf, None)):
                    cls_name = _local_name(str(cls))
                    if not cls_name:
                        continue
                    # parent 가 Restriction 이면 onProperty 수집
                    if (parent, RDF.type, OWL.Restriction) in g:
                        on_prop = g.value(parent, OWL.onProperty)
                        if on_prop is not None:
                            direct.setdefault(cls_name, set()).add(
                                _local_name(str(on_prop)),
                            )
                # 상속: 각 클래스에 대해 모든 조상의 direct Restriction 을 합침
                for c in list(direct.keys()):
                    all_props = set(direct.get(c, set()))
                    for anc in _get_superclasses(c):
                        all_props |= direct.get(anc, set())
                    _class_restriction_props_cache[c] = all_props
                # direct 에 없는 클래스도 super 의 restriction 있을 수 있음
                for cls in g.subjects(RDF.type, OWL.Class):
                    cls_name = _local_name(str(cls))
                    if not cls_name or cls_name in _class_restriction_props_cache:
                        continue
                    all_props = set()
                    for anc in _get_superclasses(cls_name):
                        all_props |= direct.get(anc, set())
                    if all_props:
                        _class_restriction_props_cache[cls_name] = all_props
            except Exception as e:
                logger.warning("Restriction cache build failed: %s", e)
    return _class_restriction_props_cache.get(class_name, set()) if _class_restriction_props_cache else set()


def _candidate_sort_key(item: "tuple[int, dict]") -> "tuple[int, str]":
    """FK OP 후보 정렬 키. 점수 내림차순, 동점이면 **이름 오름차순**.

    T-Box 는 같은 (domain, range) 를 갖는 동의어 OP 를 여럿 선언할 수 있다 (가상의
    예: ``isSlabOf`` / ``isSlabProducedByBofHeat`` 둘 다 MaterialA→ProcessStepA).
    이때 specificity 점수가 동점이면 어느 쪽이 선택되는지가 ``obj_props`` 의 입력
    순서에 달리고, 그 순서는 T-Box 를 다시 직렬화하면 흔들린다.

    그러면 T-Box 에 무관한 DP 를 추가하고 A-Box 를 재생성하는 것만으로도 FK 관계의
    술어 이름이 통째로 바뀐다. 한 OP 의 트리플이 전부 0건이 되고 동의어 OP 가 같은
    수를 넘겨받으므로, 데이터는 같아도 **기존 SPARQL 질의가 전부 0건**이 된다.
    LLM 이 지은 이름에 질의를 묶어 둔 경우와 같은 계열의 드리프트다.

    이름을 2차 키로 고정하면 T-Box 재직렬화·DP 추가 같은 무관한 변경에 대해
    선택이 불변이 된다.
    """
    score, prop = item
    return (-score, str(prop.get("name") or ""))


def _direction_tokens(text: str) -> frozenset[str]:
    """이름에서 방향성 토큰 추출 (``origin`` / ``destination`` 등).

    camelCase 와 snake_case 를 모두 단어로 쪼개 **단어 경계** 로만 매칭한다.
    부분 문자열로 보면 짧은 토큰이 무관한 단어에 걸린다 — ``to`` 가
    ``transportation`` 안에 있어(실측) 운송 컬럼이 전부 방향성으로 오판된다.
    ``ontology_quality._directional_tokens_in_labels`` 와 같은 원칙이며 토큰 사전도
    그쪽에서 가져온다 (사본 금지).

    한글 토큰은 조사·복합어로 붙어 쓰이므로(``출발`` → ``출발지``) 부분 문자열 매칭.

    구현은 ``ontology_quality._direction_tokens_in_text`` 에 위임한다 — 토큰 사전만
    가져오고 매칭 규칙을 여기서 다시 쓰면 사본이 된다. 실측 (2026-08-17): 순서 축
    (precede/follow) 을 추가할 때 사전과 축이 별개 리터럴이라 축만 늘고 추출이
    no-op 이 됐다. 규칙과 어휘를 한곳에 둔다.
    """
    from tools.ontology_quality import (  # lazy to avoid cycles
        _direction_tokens_in_text,
    )

    return _direction_tokens_in_text(text)


def _column_direction_affinity(fk_column: str, prop_name: str) -> int:
    """FK **컬럼 이름** 과 OP 이름의 방향이 맞는지 점수화.

    ## 왜 필요한가

    후보 순위는 ``(domain, range)`` 특이도로만 정해졌다 — **컬럼 이름은 보지 않았다**.
    그래서 같은 ``(domain, range)`` 를 갖는 OP 가 여럿이면 **모든 FK 컬럼이 같은
    승자를 고른다**.

    실측 (2026-08-17): ``Transportation.csv`` 의 두 FK 컬럼
    ``Origin_Warehouse`` / ``Destination_Warehouse`` 는 셋 다
    ``Transportation → WarehouseMaster`` 인 OP 3개
    (``hasDestinationWarehouse`` / ``transportationHasDestinationWarehouse`` /
    ``transportationHasOriginWarehouse``) 중에서 골라야 하는데, 이름 2차 키
    (알파벳 오름차순) 때문에 **둘 다** ``hasDestinationWarehouse`` 를 골랐다.

    결과는 단순한 "차선 선택" 이 아니라 **데이터 소실** 이다: 두 컬럼이 같은 술어를
    쓰므로 출발 창고 정보가 목적지와 뒤섞여 사라진다. CSV 300행의 출발 창고가 KG 에
    존재하지 않고, ``transportationHasOriginWarehouse`` 는 인스턴스 0건 유령 OP 로
    남는다 (S12 CQ 실패의 전형적 근원).

    ## 채점

    - 컬럼과 OP 가 **같은 축의 반대쪽** 토큰을 가지면 ``-20`` — 반대 방향 술어를 쓰는
      것은 차선이 아니라 **틀린 사실** 이므로 domain 특이도(최대 +2)나 Restriction
      가산점(+10) 을 눌러야 한다.
    - **같은 쪽** 이면 ``+5``.
    - 컬럼이나 OP 에 방향성 토큰이 없으면 ``0`` — 판정 근거가 없으면 개입하지 않고
      기존 순위를 그대로 둔다.

    방향 축 정의(``_DIRECTIONAL_AXES``)는 ``ontology_quality`` 의 것을 재사용한다.
    S3 의 중복 OP 게이트가 이 세 OP 를 살려둔 근거와 **같은 축** 을 써야 A-Box 선택과
    T-Box 보존 판정이 어긋나지 않는다.

    범용 이름 겹침(token overlap) 으로 넓히지 않는다: 그러면 무관한 T-Box 변경에
    선택이 흔들려 ``_candidate_sort_key`` 가 막으려던 드리프트(한 FK 관계의 술어
    이름이 통째로 바뀜)를 되살린다. 방향 대립은 **객관적으로 틀린** 경우로
    한정되므로 안전하다.
    """
    from tools.ontology_quality import _DIRECTIONAL_AXES  # lazy to avoid cycles

    col_tokens = _direction_tokens(fk_column)
    if not col_tokens:
        return 0
    op_tokens = _direction_tokens(prop_name)
    if not op_tokens:
        return 0
    matched = False
    for side_a, side_b in _DIRECTIONAL_AXES:
        col_a, col_b = col_tokens & side_a, col_tokens & side_b
        op_a, op_b = op_tokens & side_a, op_tokens & side_b
        if (col_a and op_b) or (col_b and op_a):
            return -20                      # 반대 방향 — 틀린 사실을 쓰게 된다
        if (col_a and op_a) or (col_b and op_b):
            matched = True
    return 5 if matched else 0


def _build_fk_skeleton(
    source_class: str, fk_col_normalized: str,
    obj_props: list[dict], available_classes: set[str] | None,
    fk_column_raw: str = "",
) -> "tuple[str, list[tuple], bool] | None":
    """fk_value 에 독립적인 FK 매칭 스켈레톤을 빌드. 캐시 가능.

    ``fk_column_raw`` 는 정규화 전 원본 컬럼명 (예: ``Origin_Warehouse``). 방향성
    토큰을 **단어 경계** 로 읽기 위해 필요하다 — 정규화된 ``originwarehouse`` 는
    부분 문자열 매칭을 강제하고, 그러면 ``to`` 가 ``transportationid`` 에 걸리는
    오탐이 생긴다. 빈 문자열이면 방향 판정을 건너뛴다(기존 동작).
    """
    target_class = _fk_column_to_class(fk_col_normalized)
    if not target_class:
        return None
    if not _class_exists_in_tbox(target_class):
        return None

    _target_unverified = (
        available_classes is not None and target_class not in available_classes
    )

    source_classes = _get_superclasses(source_class)
    target_classes = _get_superclasses(target_class)

    # T-Box Restriction 에서 참조된 OP 이름 집합. source_class 에 걸린
    # someValuesFrom / minCardinality Restriction 의 onProperty 를 읽어두고
    # FK 후보 선정 시 가산점을 준다. 이렇게 하면 Jury 가 minCardinality=1
    # 로 강제한 OP (예: producesProduct) 가 범용 OP 대신 선택돼, A-Box 생성
    # 후에도 카디널리티 제약이 만족된다.
    restr_props = _get_source_class_restriction_props(source_class)

    skeleton: list[tuple] = []  # fk_value 를 주입해 완성할 튜플 템플릿.
    # ("fwd", prop_name) / ("inv_pair", prop_name, inverse) / ("inv_only", inverse, fwd)

    # Rank candidates by domain specificity + restriction priority.
    # - Restriction 에서 참조되는 OP (+10) 이면 범용 OP 대신 선택.
    # - 그다음 domain 일치도로 순위: source_class 자신 (+2) > ancestor (+1) > owl:Thing/빈 (0).
    # 이렇게 하면 Jury 가 추가한 minCardinality 제약이 A-Box 생성 후에도
    # 만족되고, 범용 OP (domain=owl:Thing) 가 구체 OP 를 가로채지 않는다.
    def _direction_bonus(prop_name: str) -> int:
        """컬럼 이름과 OP 이름의 방향 일치도. 원본 컬럼명이 없으면 0."""
        if not fk_column_raw:
            return 0
        return _column_direction_affinity(fk_column_raw, prop_name)

    def _specificity(prop_name: str, prop_domain: str | None) -> int:
        score = _direction_bonus(prop_name)
        if prop_name in restr_props:
            score += 10
        if not prop_domain or prop_domain == "Thing":
            pass
        elif prop_domain == source_class:
            score += 2
        elif prop_domain in source_classes:
            score += 1
        return score

    def _specificity_target(prop_name: str, prop_domain: str | None) -> int:
        score = _direction_bonus(prop_name)
        # target 측 restriction 도 가산 (inverse 경로에서 중요)
        if prop_name in _get_source_class_restriction_props(target_class):
            score += 10
        if not prop_domain or prop_domain == "Thing":
            pass
        elif prop_domain == target_class:
            score += 2
        elif prop_domain in target_classes:
            score += 1
        return score

    # 정방향: domain ∈ source_classes, range ∈ target_classes.
    # domain 이 비어있는 (blank-node unionOf 또는 선언 누락) OP 도 restriction
    # 에 걸려 있으면 source 측 후보로 포함 — Jury 가 unionOf domain 으로
    # 만든 OP 를 놓치지 않기 위함.
    fwd_candidates: list[tuple[int, dict]] = []
    for prop in obj_props:
        prop_domain = prop.get("domain")
        prop_range = prop.get("range")
        name = prop.get("name", "")
        domain_ok = (
            prop_domain in source_classes
            or (prop_domain is None and name in restr_props)
        )
        if domain_ok and prop_range in target_classes:
            fwd_candidates.append((_specificity(name, prop_domain), prop))
    # inverse OP 가 역방향으로 실제 호환되는지 확인하는 헬퍼.
    # inv 의 domain 이 target_classes 와 맞고 range 가 source_classes 와 맞으면 OK.
    # 호환 안 되면 (T-Box 의 잘못된 inverseOf 선언) inv_pair 를 skip 하여
    # A-Box 에 range 위반 역트리플이 찍히지 않게 한다.
    def _inverse_is_compatible(fwd_prop: dict) -> bool:
        inv_name = fwd_prop.get("inverse")
        if not inv_name:
            return False
        inv_prop = None
        for p in obj_props:
            if p.get("name") == inv_name:
                inv_prop = p
                break
        if inv_prop is None:
            return False
        inv_d = inv_prop.get("domain")
        inv_r = inv_prop.get("range")
        # inv.domain 이 target 클래스와 호환, inv.range 가 source 와 호환해야 함
        dom_ok = inv_d is None or inv_d in target_classes
        rng_ok = inv_r is None or inv_r in source_classes
        return dom_ok and rng_ok

    matched = False
    if fwd_candidates:
        fwd_candidates.sort(key=_candidate_sort_key)
        prop = fwd_candidates[0][1]
        skeleton.append(("fwd", prop["name"]))
        if _inverse_is_compatible(prop):
            skeleton.append(("inv_pair", prop["name"], prop.get("inverse")))
        matched = True

    # 역방향 탐색
    if not matched:
        inv_candidates: list[tuple[int, dict]] = []
        for prop in obj_props:
            prop_domain = prop.get("domain")
            name = prop.get("name", "")
            if prop_domain in target_classes and prop.get("range") in source_classes:
                inv_candidates.append((_specificity_target(name, prop_domain), prop))
        if inv_candidates:
            inv_candidates.sort(key=_candidate_sort_key)
            prop = inv_candidates[0][1]
            inv = prop.get("inverse")
            if inv:
                skeleton.append(("fwd", inv))  # inverse 를 forward 로 사용
                # inv 가 fwd 라면 prop 은 이 prop 의 inverse. prop 의 domain/range
                # 가 (target, source) 인 게 이미 확인됐으므로 안전.
                skeleton.append(("inv_pair", inv, prop["name"]))
            matched = True

    if not matched:
        # no_domain_range_match sentinel
        skeleton.append(("unverified_nomatch",))

    return target_class, skeleton, _target_unverified


def _fk_skeleton(source_class: str, fk_col_normalized: str,
                  obj_props: list[dict],
                  available_classes: set[str] | None,
                  fk_column_raw: str = ""):
    key = (source_class, fk_col_normalized)
    if key in _fk_skeleton_cache:
        return _fk_skeleton_cache[key]
    result = _build_fk_skeleton(
        source_class, fk_col_normalized, obj_props, available_classes,
        fk_column_raw,
    )
    _fk_skeleton_cache[key] = result
    return result


def _reset_fk_skeleton_cache() -> None:
    """T-Box 변경 또는 테스트 격리 시 캐시 리셋."""
    _fk_skeleton_cache.clear()


def _detect_fk_property(source_class: str, fk_column: str, fk_value: str,
                        obj_props: list[dict],
                        available_classes: set[str] | None = None,
                        *,
                        master_instance_uris: set[str] | None = None,
                        master_value_index: dict[str, dict[str, str]] | None = None,
                        fuzzy_config: dict | None = None) -> list[tuple]:
    """FK 컬럼에서 ObjectProperty + target 인스턴스 URI를 양방향으로 감지.

    subClassOf 계층을 고려하여 상위 클래스의 OP도 매칭한다.
    예) ProcessBlastFurnace ⊂ ManufacturingProcessStep이면
        usesEquipment(domain=ManufacturingProcessStep)도 매칭.

    Args:
        available_classes: CSV 데이터에 존재하는 클래스명 집합.
            None이면 검증을 건너뛴다.
            target_class가 이 집합에 없으면 결과 튜플에 verified=False를 표시한다.
        master_instance_uris: master_data.ttl 에서 로드한 인스턴스 URI set.
            None 이면 퍼지 매칭 생략 (기존 동작).
        master_value_index: `build_master_value_index` 결과
            ({class: {normalized_suffix: original_uri}}).
        fuzzy_config: `{"normalized": bool, "levenshtein": bool, "prefix": bool}`.
            None 이면 기본 (normalized only) 적용. master_instance_uris 가 None 이면 미사용.

    Returns:
        [(prop_name, target_uri), ...] — 정방향 + inverseOf 역방향 모두 포함.
        역방향은 ("__inverse__", fwd_prop, inv_prop, target_uri) 형식.
        검증 불가 FK는 ("__unverified__", prop_name, target_uri, target_class) 형식이
        리스트 말미에 추가된다.

    성능: 같은 (source_class, fk_column) 조합이 반복되면 매칭 로직은 캐시에서 O(1).
    fk_value 로 target_uri 만 만들어 skeleton 과 결합한다.
    퍼지 매칭도 master_value_index 가 사전 구축돼 있어 O(1) 조회.
    """
    fk_col_normalized = fk_column.lower().replace("_", "")
    skel = _fk_skeleton(
        source_class, fk_col_normalized, obj_props, available_classes,
        fk_column,
    )
    if skel is None:
        return []
    target_class, skeleton, _target_unverified = skel

    # 컬럼 한정 결정적 변환 (자식 식별자의 끝 1글자를 떼면 부모 식별자가 되는 계층).
    # 전역 퍼지 매칭 이전에 적용해, 변환된 값이 exact/normalized 사다리를 탄다.
    fk_value = _apply_fk_value_transform(fk_col_normalized, fk_value)

    candidate_uri = DOMAIN_INST_NS_OBJ[f"{target_class}_{_uri_safe_local(fk_value)}"]

    # 퍼지 매칭: master_value_index 가 있으면 4단계 fallback 으로 실제 URI 결정.
    # master_instance_uris 미제공 시 기존 동작 (candidate 를 그대로 사용) 유지.
    target_uri = candidate_uri
    if master_instance_uris is not None and master_value_index is not None:
        from tools.fk_matching import resolve_fk_target
        cfg = fuzzy_config if fuzzy_config is not None else {"normalized": True}
        target_uri, _stage = resolve_fk_target(
            target_class, fk_value, candidate_uri,
            master_instance_uris, master_value_index, cfg,
        )

    results: list[tuple] = []
    for item in skeleton:
        kind = item[0]
        if kind == "fwd":
            results.append((item[1], target_uri))
        elif kind == "inv_pair":
            results.append(("__inverse__", item[1], item[2], target_uri))
        elif kind == "unverified_nomatch":
            results.append(("__unverified__", "<no_domain_range_match>",
                            target_uri, target_class))

    # no_match 였다면 그대로 반환 (후속 unverified 마커 추가 불필요)
    if any(i[0] == "unverified_nomatch" for i in skeleton):
        return results

    if _target_unverified and results:
        results.append(("__unverified__",
                        results[0][0] if len(results[0]) == 2 else results[0][1],
                        target_uri, target_class))

    return results


def _detect_fk_by_value_overlap(
    col_values: set[str],
    available_pk_index: dict[str, set[str]],
    threshold: float = 0.8,
) -> str | None:
    """값 오버랩 기반 FK 타겟 클래스 추정.

    suffix 패턴 매칭 실패 시 폴백으로 사용. 컬럼 값 집합과
    각 클래스의 PK 값 집합 간 교집합 비율이 threshold 이상이면 타겟 클래스로 판정.
    """
    if not col_values:
        return None
    best_class = None
    best_overlap = 0.0
    for cls_name, pk_values in available_pk_index.items():
        if not pk_values:
            continue
        overlap = len(col_values & pk_values) / len(col_values)
        if overlap >= threshold and overlap > best_overlap:
            best_overlap = overlap
            best_class = cls_name
    return best_class


def _should_promote_unverified_fk(target_uri: str, master_instance_uris: set[str]) -> bool:
    """Unverified FK를 master_data 인스턴스 존재 여부로 승격 판정."""
    return target_uri in master_instance_uris


# ── 값 변환 ───────────────────────────────────────


_NUMERIC_KEYWORDS = list(
    _VALUE_HEURISTICS.get("numeric_keywords")
    or [
        "amount", "rate", "temp", "pressure", "flow", "speed",
        "force", "level", "percent", "mpa", "kpa", "kwh", "kw",
        "hours", "count", "stock", "quantity", "cost", "price",
        "score", "factor", "value", "input", "output", "mm", "kg",
        "ton", "kn", "hz", "db", "efficiency", "basicity", "enthalpy",
        "calorific", "weight", "height", "width", "length",
        "thickness", "roughness", "strength", "elongation", "hardness",
        "temperature", "concentration", "capacity", "power",
        "voltage", "current", "min", "max", "avg", "total",
    ],
)


_NULL_SENTINELS: frozenset[str] = frozenset(
    _VALUE_HEURISTICS.get("null_sentinels")
    or {"n/a", "na", "null", "-", "none", "#n/a", "nan", "undefined", "미정", "해당없음"},
)


def _is_null_sentinel(value: str) -> bool:
    """Return True if ``value`` is a recognized CSV NULL sentinel (case-insensitive)."""
    if not value or not value.strip():
        return True
    return value.strip().lower() in _NULL_SENTINELS


# 일부 컬럼에서는 정당한 도메인 값이기도 한 센티널이라 전역이 아니라 컬럼별로
# 판정해야 하는 값. 현재는 "-" 하나뿐이다. 공차 컬럼에서는 ("+" 와 짝을 이루는)
# 마이너스 *부호* 이고, 자유 텍스트 컬럼에서는 일반 플레이스홀더다.
_AMBIGUOUS_SENTINELS: frozenset[str] = frozenset({"-"})

# 비어 있지 않은 값이 아주 작은 닫힌 집합에서만 나오면 그 컬럼은 "-" 를 데이터로
# 담는다고 본다. 부호 컬럼은 {-, +} 나 {-, 0, 1} 을 담고, 이름 같은 자유 텍스트
# 컬럼은 서로 다른 문자열을 많이 담으므로 플레이스홀더 해석이 이긴다.
_SENTINEL_VALUE_COLUMN_MAX_DISTINCT = 4


def _detect_sentinel_value_columns(rows: list[dict]) -> set[str]:
    """모호한 센티널이 실제 데이터인 컬럼의 소문자 이름 집합을 반환한다.

    판정 근거는 값 하나가 아니라 컬럼의 값 *어휘* 다. 부호 컬럼은 2~3개 기호로
    닫힌 도메인이라 그 안에서 ``-`` 는 "데이터 없음" 일 수 없다. 그 인코딩에는
    음수를 달리 표현할 방법이 없기 때문이다. 가상의 예로 ``TOL_SIGN_1`` 이
    {``-``, ``+``} 만 담는다면, ``-`` 를 NULL 로 거르는 순간 KG 의 음수 공차가
    전부 결측값과 구별되지 않고 양수만 남는다. 반대로 이름 같은 자유 텍스트 컬럼
    (``NAME_COL_1``: 서로 다른 값이 많고 ``-`` 는 드물다) 에서는 ``-`` 를
    플레이스홀더로 읽는다.

    streaming 모드에서는 ``rows`` 가 표본일 수 있다. 어휘가 넓은 컬럼은 처음
    수백 행 안에서 드러나므로 표본으로 충분하다.
    """
    if not rows:
        return set()
    per_col: dict[str, set[str]] = {}
    has_sentinel: set[str] = set()
    for row in rows:
        for col, raw in row.items():
            if not col:
                continue
            val = str(raw).strip() if raw is not None else ""
            if not val:
                continue
            key = col.lower()
            bucket = per_col.setdefault(key, set())
            # Cap the set: once past the threshold the verdict cannot change,
            # so stop accumulating to keep this O(rows × cols) scan cheap.
            if len(bucket) <= _SENTINEL_VALUE_COLUMN_MAX_DISTINCT:
                bucket.add(val)
            if val.lower() in _AMBIGUOUS_SENTINELS:
                has_sentinel.add(key)
    return {
        col for col in has_sentinel
        if len(per_col.get(col, ())) <= _SENTINEL_VALUE_COLUMN_MAX_DISTINCT
    }


def _format_value(
    value: str, prop_name: str, tbox_range: str = "",
    *, sentinel_is_value: bool = False,
) -> Literal | None:
    """CSV 값을 rdflib Literal로 변환. T-Box range를 우선 적용.

    ``sentinel_is_value``: 이 컬럼에서는 모호한 센티널("-")이 실제 값이므로
    필터하지 않는다. 판정은 :func:`_detect_sentinel_value_columns` 가 컬럼의 값
    어휘를 보고 내린다 (부호 컬럼 vs 자유 텍스트).
    """
    if not value or not value.strip():
        return None
    v = value.strip()

    # 센티널 값 필터 (CSV에서 NULL을 나타내는 관용 표현)
    lowered = v.lower()
    if lowered in _NULL_SENTINELS and not (
        sentinel_is_value and lowered in _AMBIGUOUS_SENTINELS
    ):
        return None

    # T-Box range가 있으면 우선 사용 (타입 일관성 보장)
    if tbox_range:
        converter = _XSD_CONVERTERS.get(tbox_range)
        if converter:
            converted = converter(v)
            if converted is not None:
                return Literal(converted, datatype=URIRef(tbox_range))
            return None  # 변환 불가 → 트리플 스킵 (타입 불일치 방지)
        # 컨버터 미등록 XSD 타입: 선언된 타입 그대로 사용 (타입 일관성 유지)
        logger.debug("T-Box range %s에 대한 컨버터 없음 (prop=%s), 선언 타입 그대로 적용", tbox_range, prop_name)
        return Literal(v, datatype=URIRef(tbox_range))

    # T-Box range 없으면 기존 휴리스틱 폴백
    prop_lower = prop_name.lower()

    is_numeric = any(re.search(rf'\b{re.escape(kw)}\b', prop_lower) for kw in _NUMERIC_KEYWORDS)
    if is_numeric:
        try:
            float(v)
            return Literal(v, datatype=XSD.decimal)
        except ValueError:
            pass

    if v.lower() in ("true", "false"):
        return Literal(v.lower() == "true", datatype=XSD.boolean)

    if re.match(r"\d{4}-\d{1,2}-\d{1,2}T\d{1,2}:\d{2}", v):
        return Literal(v, datatype=XSD.dateTime)
    if re.match(r"\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}", v):
        return Literal(v.replace(" ", "T"), datatype=XSD.dateTime)
    if re.match(r"\d{4}-\d{1,2}-\d{1,2}$", v):
        return Literal(v, datatype=XSD.date)

    return Literal(v, datatype=XSD.string)


# ── A-Box SHACL 검증 + 자동 수정 ─────────────────


def _expand_range_alternatives(tbox_g: Graph, range_node) -> list:
    """Return a list of concrete range URIs, expanding owl:unionOf blank nodes.

    - 단일 URIRef → [URIRef]
    - unionOf bnode → collection 멤버 중 URIRef 만 수집
    - 해석 실패 → []
    """
    import rdflib.collection as _rcoll
    if isinstance(range_node, URIRef):
        return [range_node]
    alternatives: list = []
    for u_list in tbox_g.objects(range_node, OWL.unionOf):
        try:
            for m in _rcoll.Collection(tbox_g, u_list):
                if isinstance(m, URIRef):
                    alternatives.append(m)
        except Exception:  # noqa: BLE001 — 깨진 unionOf range는 해당 목록만 건너뛴다
            continue
    return alternatives


def _build_abox_shapes(tbox_ttl: str) -> Graph:
    """T-Box TTL로부터 A-Box 검증용 SHACL shapes를 생성한다.

    생성되는 shape 규칙:
    - 각 클래스 인스턴스의 DatatypeProperty 값이 올바른 XSD 타입인지 확인
    - 각 클래스 인스턴스의 ObjectProperty 대상이 올바른 클래스 인스턴스인지 확인
    - owl:unionOf 로 선언된 range 는 sh:or 로 분기 (단일 ranges[0] 만 사용해
      합법 값을 오탐하던 문제 해결)
    - 모든 제약은 sh:Warning severity (A-Box 프로퍼티는 선택적)
    """
    tbox_g = _new_graph()
    tbox_g.parse(data=tbox_ttl, format="turtle")

    shapes_g = _new_graph()
    shapes_g.bind("sh", SH)
    shapes_g.bind(NS_PREFIX, DOMAIN_NS)
    shapes_g.bind(NS_INST_PREFIX, DOMAIN_INST_NS)
    shapes_g.bind("xsd", XSD)

    steel_ns = DOMAIN_NS

    def _add_or_alternatives(prop_shape, alt_uris: list, predicate) -> None:
        """Attach sh:or with alternative constraints via rdf:List."""
        import rdflib.collection as _rcoll
        or_list_head = BNode()
        alt_nodes = []
        for alt_uri in alt_uris:
            alt_bn = BNode()
            shapes_g.add((alt_bn, predicate, alt_uri))
            alt_nodes.append(alt_bn)
        _rcoll.Collection(shapes_g, or_list_head, alt_nodes)
        shapes_g.add((prop_shape, SH["or"], or_list_head))

    # 클래스별 shape 생성
    for cls in tbox_g.subjects(RDF.type, OWL.Class):
        cls_str = str(cls)
        if not cls_str.startswith(steel_ns):
            continue
        cls_name = _local_name(cls_str)
        if not cls_name:
            continue

        shape_uri = URIRef(f"{steel_ns}{cls_name}Shape")
        shapes_g.add((shape_uri, RDF.type, SH.NodeShape))
        shapes_g.add((shape_uri, SH.targetClass, cls))

        # DatatypeProperty constraints
        for prop in tbox_g.subjects(RDF.type, OWL.DatatypeProperty):
            domains = list(tbox_g.objects(prop, RDFS.domain))
            if cls not in domains:
                continue
            ranges = list(tbox_g.objects(prop, RDFS.range))
            if not ranges:
                continue

            # Collect concrete alternatives (expand unionOf bnodes)
            alt_uris: list = []
            for r in ranges:
                alt_uris.extend(_expand_range_alternatives(tbox_g, r))
            if not alt_uris:
                continue

            prop_shape = BNode()
            shapes_g.add((shape_uri, SH.property, prop_shape))
            shapes_g.add((prop_shape, SH.path, prop))
            shapes_g.add((prop_shape, SH.severity, SH.Warning))
            shapes_g.add((prop_shape, SH.minCount, Literal(0, datatype=XSD.integer)))
            if len(alt_uris) == 1:
                shapes_g.add((prop_shape, SH.datatype, alt_uris[0]))
            else:
                _add_or_alternatives(prop_shape, alt_uris, SH.datatype)

        # ObjectProperty constraints
        for prop in tbox_g.subjects(RDF.type, OWL.ObjectProperty):
            domains = list(tbox_g.objects(prop, RDFS.domain))
            if cls not in domains:
                continue
            ranges = list(tbox_g.objects(prop, RDFS.range))
            if not ranges:
                continue

            alt_uris: list = []
            for r in ranges:
                alt_uris.extend(_expand_range_alternatives(tbox_g, r))
            if not alt_uris:
                continue

            prop_shape = BNode()
            shapes_g.add((shape_uri, SH.property, prop_shape))
            shapes_g.add((prop_shape, SH.path, prop))
            shapes_g.add((prop_shape, SH.severity, SH.Warning))
            shapes_g.add((prop_shape, SH.minCount, Literal(0, datatype=XSD.integer)))
            if len(alt_uris) == 1:
                shapes_g.add((prop_shape, SH["class"], alt_uris[0]))
            else:
                _add_or_alternatives(prop_shape, alt_uris, SH["class"])

    return shapes_g


def _parse_abox_violations(results_graph: Graph) -> list:
    """SHACL 검증 결과 그래프에서 위반 사항을 추출한다.

    Returns:
        위반 목록. 각 항목은 dict:
        - focus_node: 위반이 발생한 인스턴스 URI
        - path: 위반 프로퍼티 URI
        - value: 위반 값 (있으면)
        - constraint: 위반된 제약 종류 (datatype, class 등)
        - expected: 기대값 (XSD 타입 URI 또는 클래스 URI)
        - message: SHACL 결과 메시지
        - severity: sh:Warning / sh:Violation 등
    """
    violations = []

    for result in results_graph.subjects(RDF.type, SH.ValidationResult):
        focus_nodes = list(results_graph.objects(result, SH.focusNode))
        paths = list(results_graph.objects(result, SH.resultPath))
        values = list(results_graph.objects(result, SH.value))
        messages = list(results_graph.objects(result, SH.resultMessage))
        severities = list(results_graph.objects(result, SH.resultSeverity))

        # constraint 종류 판별
        source_constraints = list(results_graph.objects(result, SH.sourceConstraintComponent))
        constraint_type = "unknown"
        expected = None
        for sc in source_constraints:
            sc_str = str(sc)
            if "DatatypeConstraintComponent" in sc_str:
                constraint_type = "datatype"
                # 기대 타입은 sourceShape에서 추출
                source_shapes = list(results_graph.objects(result, SH.sourceShape))
                for ss in source_shapes:
                    for dt in results_graph.objects(ss, SH.datatype):
                        expected = str(dt)
                        break
            elif "ClassConstraintComponent" in sc_str:
                constraint_type = "class"
                source_shapes = list(results_graph.objects(result, SH.sourceShape))
                for ss in source_shapes:
                    for cl in results_graph.objects(ss, SH["class"]):
                        expected = str(cl)
                        break

        violation = {
            "focus_node": str(focus_nodes[0]) if focus_nodes else None,
            "path": str(paths[0]) if paths else None,
            "value": str(values[0]) if values else None,
            "constraint": constraint_type,
            "expected": expected,
            "message": str(messages[0]) if messages else None,
            "severity": str(severities[0]) if severities else None,
        }
        violations.append(violation)

    return violations


def _is_numeric(v: str) -> bool:
    try:
        float(v)
        return True
    except (ValueError, TypeError):
        return False


def _normalize_boolean(v: str) -> str | None:
    v_lower = str(v).strip().lower()
    if v_lower in ("true", "1", "yes"):
        return "true"
    if v_lower in ("false", "0", "no"):
        return "false"
    return None


# XSD 타입 변환 맵: 대상 타입 → 변환 함수
# #20: 다양한 CSV 날짜 포맷을 자동 감지해 xsd:dateTime 으로 변환.
_DATE_INPUT_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",      # ISO full
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S.%f",   # space separator + fractional seconds (RDB dump)
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y.%m.%d %H:%M:%S.%f",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d",
    "%Y%m%d",                 # 압축
    "%Y%m%d%H%M%S",
]

#: 일/월 순서가 **값만 보고는 결정되지 않는** 포맷.
#:
#: ``03/04/2025`` 는 3월 4일(US)일 수도 4월 3일(EU)일 수도 있다. 예전에는
#: ``%d/%m/%Y`` 를 ``%m/%d/%Y`` 보다 먼저 시도해서, **같은 컬럼 안에서 값에 따라
#: 해석이 갈렸다** (실측):
#:
#:     '03/04/2025' → 2025-04-03   일/월로 읽음 (첫 포맷이 성공)
#:     '12/25/2025' → 2025-12-25   월/일로 읽음 (25 는 월이 될 수 없어 폴백)
#:
#: 경고도 오류도 없이 날짜가 뒤바뀐다. 한 컬럼을 두 규칙으로 읽는 것은 어떤
#: 해석을 고르든 틀린 동작이므로, 기본값은 **추측하지 않고 거부** 한다
#: (``None`` → ``type_coercion_failures`` 로 집계돼 loss manifest 에 드러난다).
#:
#: 거부는 **실제로 모호한 값에만** 적용한다. ``15/01/2024`` 는 15 가 월이 될 수
#: 없으므로 해석이 하나뿐이라 그대로 파싱한다 — 과잉 거부는 정상 데이터를 잃는
#: 또 다른 손실이다. 두 자리 모두 12 이하일 때만 판단을 보류한다.
#:
#: CSV 가 실제로 한 규칙을 따르면 환경변수로 명시한다:
#:   ``CSV_DATE_ORDER=dmy`` → 일/월 (유럽)
#:   ``CSV_DATE_ORDER=mdy`` → 월/일 (미국)
#:   미설정(기본) → 모호한 값만 거부, 나머지는 일/월로 해석.
_DAY_FIRST_FORMATS: tuple[str, ...] = ("%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y")
_MONTH_FIRST_FORMATS: tuple[str, ...] = ("%m/%d/%Y", "%m.%d.%Y", "%m-%d-%Y")

#: ``a/b/YYYY`` 형태 — 구분자는 ``/ . -`` 셋 다.
_TWO_PART_DATE_RE = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:[T ].*)?$")


def _is_ambiguous_day_month(value: str) -> bool:
    """두 자리 모두 12 이하라 일/월 순서를 값으로 결정할 수 없는가."""
    match = _TWO_PART_DATE_RE.match(value.strip())
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return 1 <= first <= 12 and 1 <= second <= 12


def _configured_date_order() -> str:
    """``CSV_DATE_ORDER`` 정규화 결과: ``"dmy"`` | ``"mdy"`` | ``""``(미설정).

    환경변수를 매 호출 읽는다 (모듈 로드 시점 고정 아님) — 테스트가 monkeypatch
    로 주입하고, 운영에서도 서버 재시작 없이 바뀌어야 한다.

    **오타는 미설정으로 취급한다.** ``CSV_DATE_ORDER=eu`` 같은 값에 기본 순서를
    적용하면, 사용자는 자기가 지정한 규칙이 쓰인다고 믿는데 실제로는 반대 규칙이
    조용히 적용될 수 있다 — 이 함수가 막으려는 바로 그 실패다.
    """
    order = (os.getenv("CSV_DATE_ORDER") or "").strip().lower()
    if order and order not in ("dmy", "mdy"):
        logger.warning(
            "CSV_DATE_ORDER=%r 은 지원하지 않는 값 — 미설정으로 취급해 모호한 "
            "날짜를 거부한다 (허용: dmy / mdy)", order,
        )
        return ""
    return order


def _date_input_formats() -> list[str]:
    """시도할 날짜 포맷 목록. ``CSV_DATE_ORDER`` 가 일/월 순서를 정한다.

    - ``dmy`` / ``mdy`` 명시: **그 순서만** 시도한다. 반대 순서로만 해석되는 값은
      거부된다 (``mdy`` 선언 하의 ``15/01`` 처럼) — 선언과 어긋나는 데이터를
      조용히 뒤집어 읽는 것을 막는다.
    - 미설정: **양쪽을 모두** 시도한다. 모호한 값은 그 앞의 가드가 이미 걸렀으므로,
      여기 오는 값은 해석이 하나뿐이다 (``12/25`` 는 월/일, ``15/01`` 은 일/월).
      한쪽만 시도하면 반대 순서의 정당한 값을 잃는다 — 과잉 거부도 손실이다.
    """
    order = _configured_date_order()
    if order == "mdy":
        tail: tuple[str, ...] = _MONTH_FIRST_FORMATS
    elif order == "dmy":
        tail = _DAY_FIRST_FORMATS
    else:
        tail = (*_DAY_FIRST_FORMATS, *_MONTH_FIRST_FORMATS)
    return [*_DATE_INPUT_FORMATS, *tail]


def _convert_datetime(v: str) -> str | None:
    """xsd:dateTime 변환: date-only 값에 T00:00:00 보정. 유효성 검증 포함.

    #20: 지원 포맷 확대 — ISO 외에도 `YYYY/MM/DD`, `DD-MM-YYYY`, 압축 `YYYYMMDD`
    등을 자동 파싱하여 동일한 `YYYY-MM-DDTHH:MM:SS` 로 정규화.
    """
    v = v.strip()
    if not v:
        return None
    # 빠른 경로: ISO 8601
    if re.match(r"\d{4}-\d{2}-\d{2}T", v):
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        # Z 타임존은 제거 (xsd:dateTime은 타임존 포함 가능하지만 여기서는 단순화)
        return v.rstrip("Z")
    if re.match(r"\d{4}-\d{2}-\d{2}$", v):
        try:
            datetime.fromisoformat(v)
        except ValueError:
            return None
        return v + "T00:00:00"

    # 일/월 순서가 값으로 결정되지 않으면 **추측하지 않는다**. 한 컬럼을 값마다
    # 다른 규칙으로 읽으면 경고 없이 날짜가 뒤바뀐다 (실측: 03/04 는 일/월,
    # 12/25 는 월/일로 읽혔다). None 은 type_coercion_failures 로 집계돼
    # loss manifest 에 드러나므로, 조용한 오답보다 보이는 손실이 낫다.
    if not _configured_date_order() and _is_ambiguous_day_month(v):
        logger.warning(
            "날짜 %r 의 일/월 순서를 결정할 수 없어 값을 버린다 — CSV 가 한 규칙을 "
            "따르면 CSV_DATE_ORDER=dmy 또는 mdy 로 명시하라.", v,
        )
        return None

    # 느린 경로: 다양한 포맷 try-parse.
    # isoformat() 으로 소수부(밀리초)를 보존 — 원본 타임스탬프는
    # 동일 행의 생성/갱신 시각이 밀리초로만 구분되므로 절삭하면 정보 손실.
    for fmt in _date_input_formats():
        try:
            dt = datetime.strptime(v, fmt)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def _convert_date(v: str) -> str | None:
    """#20: 다양한 CSV 포맷을 xsd:date(YYYY-MM-DD) 정규형으로 변환."""
    v = v.strip()
    if not v:
        return None
    if re.match(r"\d{4}-\d{2}-\d{2}$", v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            return None
        return v
    # 일/월 순서가 값으로 결정되지 않으면 **추측하지 않는다**. 한 컬럼을 값마다
    # 다른 규칙으로 읽으면 경고 없이 날짜가 뒤바뀐다 (실측: 03/04 는 일/월,
    # 12/25 는 월/일로 읽혔다). None 은 type_coercion_failures 로 집계돼
    # loss manifest 에 드러나므로, 조용한 오답보다 보이는 손실이 낫다.
    if not _configured_date_order() and _is_ambiguous_day_month(v):
        logger.warning(
            "날짜 %r 의 일/월 순서를 결정할 수 없어 값을 버린다 — CSV 가 한 규칙을 "
            "따르면 CSV_DATE_ORDER=dmy 또는 mdy 로 명시하라.", v,
        )
        return None

    # 같은 포맷 목록 재사용 (time 제거)
    for fmt in _date_input_formats():
        try:
            dt = datetime.strptime(v, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _convert_decimal(v: str) -> str | None:
    """xsd:decimal 변환: 숫자가 아닌 문자열은 제거(None)."""
    if _is_numeric(v):
        return str(float(v))
    return None  # "Primary" 등 비숫자 → 트리플 제거 대상


def _convert_time(v: str) -> str | None:
    """xsd:time (HH:MM:SS) 변환. dateTime 값을 받으면 시분초 부분만 추출.

    rdflib 의 fromisoformat 이 `"YYYY-MM-DD HH:MM:SS"` 를 xsd:time 로
    파싱하려다 실패하는 회귀를 방지. 입력이 dateTime 이면 time 부분만
    떼어내고, 이미 time 형식이면 그대로 사용.
    """
    v = v.strip()
    if not v:
        return None
    # 순수 time (HH:MM:SS or HH:MM:SS.fraction)
    if re.match(r"^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$", v):
        return v
    # dateTime → time 부분만 추출
    dt = _convert_datetime(v)
    if dt and "T" in dt:
        return dt.split("T", 1)[1]
    return None


def _convert_non_negative_int(v: str) -> str | None:
    """xsd:nonNegativeInteger / unsignedInt/Long/Short — accept n >= 0."""
    if not _is_numeric(v):
        return None
    try:
        n = int(float(v))
    except (ValueError, OverflowError):
        return None
    return str(n) if n >= 0 else None


def _convert_positive_int(v: str) -> str | None:
    """xsd:positiveInteger — accept n > 0."""
    if not _is_numeric(v):
        return None
    try:
        n = int(float(v))
    except (ValueError, OverflowError):
        return None
    return str(n) if n > 0 else None


def _convert_byte(v: str) -> str | None:
    """xsd:byte — accept -128 <= n <= 127."""
    if not _is_numeric(v):
        return None
    try:
        n = int(float(v))
    except (ValueError, OverflowError):
        return None
    return str(n) if -128 <= n <= 127 else None


def _convert_unsigned_byte(v: str) -> str | None:
    """xsd:unsignedByte — accept 0 <= n <= 255."""
    if not _is_numeric(v):
        return None
    try:
        n = int(float(v))
    except (ValueError, OverflowError):
        return None
    return str(n) if 0 <= n <= 255 else None


def _convert_gyear(v: str) -> str | None:
    """xsd:gYear — 4-digit year (e.g. '2024')."""
    v = v.strip()
    if re.match(r"^\d{4}$", v):
        return v
    return None


def _convert_gmonth(v: str) -> str | None:
    """xsd:gMonth — '--MM' format per XSD spec (e.g. '--05')."""
    v = v.strip()
    if re.match(r"^--(0[1-9]|1[0-2])$", v):
        return v
    return None


def _convert_gday(v: str) -> str | None:
    """xsd:gDay — '---DD' format per XSD spec (e.g. '---15')."""
    v = v.strip()
    if re.match(r"^---(0[1-9]|[12][0-9]|3[01])$", v):
        return v
    return None


_XSD_CONVERTERS = {
    str(XSD.decimal): _convert_decimal,
    str(XSD.float): _convert_decimal,
    str(XSD.double): _convert_decimal,
    str(XSD.integer): lambda v: str(int(float(v))) if _is_numeric(v) else None,
    str(XSD.long): lambda v: str(int(float(v))) if _is_numeric(v) else None,
    str(XSD.short): lambda v: str(int(float(v))) if _is_numeric(v) else None,
    str(XSD.boolean): lambda v: _normalize_boolean(v),
    str(XSD.string): lambda v: str(v),
    str(XSD.dateTime): _convert_datetime,
    str(XSD.date): lambda v: _convert_date(v),
    str(XSD.time): _convert_time,
    str(XSD.anyURI): lambda v: str(v),
    str(XSD.normalizedString): lambda v: str(v),
    # A5: restricted integer + gregorian part types.
    str(XSD.nonNegativeInteger): _convert_non_negative_int,
    str(XSD.positiveInteger): _convert_positive_int,
    str(XSD.unsignedInt): _convert_non_negative_int,
    str(XSD.unsignedLong): _convert_non_negative_int,
    str(XSD.unsignedShort): _convert_non_negative_int,
    str(XSD.byte): _convert_byte,
    str(XSD.unsignedByte): _convert_unsigned_byte,
    str(XSD.gYear): _convert_gyear,
    str(XSD.gMonth): _convert_gmonth,
    str(XSD.gDay): _convert_gday,
}


def _extract_disjoint_groups(tbox_ttl: str) -> list[set[str]]:
    """T-Box TTL에서 owl:AllDisjointClasses 그룹을 추출한다.

    Returns:
        [set(class_uri_str, ...), ...] — 각 set이 하나의 disjoint 그룹.
    """
    try:
        tbox_g = _new_graph()
        tbox_g.parse(data=tbox_ttl, format="turtle")
    except Exception:
        return []

    groups: list[set[str]] = []
    for disjoint_node in tbox_g.subjects(RDF.type, OWL.AllDisjointClasses):
        members_list_node = list(tbox_g.objects(disjoint_node, OWL.members))
        if not members_list_node:
            continue
        # RDF list 순회
        members: set[str] = set()
        node = members_list_node[0]
        while node and node != RDF.nil:
            first = list(tbox_g.objects(node, RDF.first))
            if first:
                members.add(str(first[0]))
            rest = list(tbox_g.objects(node, RDF.rest))
            node = rest[0] if rest else None
        if len(members) >= 2:
            groups.append(members)
    return groups


def _deterministic_fix_abox(g: Graph, violations: list,
                            disjoint_groups: list[set[str]] | None = None,
                            pre_added_types: list[tuple] | None = None,
                            infer_types: bool | None = None) -> dict:
    """SHACL 위반을 결정론적으로 자동 수정한다. Graph를 직접 수정(in-place).

    수정 가능 항목:
    - XSD 타입 미스매치: 리터럴 값을 올바른 타입으로 재캐스트
      (string->decimal, string->integer, boolean 정규화 등)
    - class constraint: 대상 인스턴스에 기대 rdf:type 추가 (opt-in)
    - 누락된 rdf:type: URI 패턴에서 클래스를 추론하여 추가 (opt-in)
      (EquipmentMaster_EQ001 → steel:EquipmentMaster)

    Args:
        pre_added_types: 호출 전 단계(FK 생성 등)에서 이미 주입된 rdf:type
            트리플 목록. disjoint 검사 시 함께 대상으로 삼는다.
        infer_types: class-constraint / URI 패턴 기반 rdf:type 추론을 수행할지.
            None 이면 ``ABOX_SHACL_INFER_TYPES`` 환경변수를 참조 (기본 off).
            SHACL 위반이 실제로는 FK 오매칭/컨벤션 위반의 신호일 수 있으므로
            기본값은 보수적(off) 이며, 디버깅/부트스트랩 용도로만 켠다.

    수정 후 AllDisjointClasses 일관성 스팟체크:
    - 새로 추가된 rdf:type이 기존 타입과 동일한 disjoint 그룹에 속하면 롤백.
    - FK 생성 단계에서 사전 주입된 타입도 동일 규칙으로 검증한다.

    Returns:
        수정 통계 dict
    """
    if infer_types is None:
        infer_types = os.getenv("ABOX_SHACL_INFER_TYPES", "false").lower() in ("true", "1", "yes")

    stats = {
        "type_recast": 0,
        "type_inferred": 0,
        "unfixable": 0,
        "disjoint_conflict_reverted": 0,
    }
    # 새로 추가된 rdf:type 트리플을 추적 (일관성 스팟체크용).
    # FK 생성 단계에서 이미 쌓인 트리플도 함께 검증 대상으로 삼는다.
    added_type_triples: list[tuple] = list(pre_added_types or [])

    for v in violations:
        constraint = v.get("constraint")
        focus_node = v.get("focus_node")
        path = v.get("path")
        value = v.get("value")
        expected = v.get("expected")

        if not focus_node or not path:
            stats["unfixable"] += 1
            continue

        focus_ref = URIRef(focus_node)
        path_ref = URIRef(path)

        # --- Fix 1: XSD 타입 미스매치 재캐스트 ---
        if constraint == "datatype" and expected and value is not None:
            converter = _XSD_CONVERTERS.get(expected)
            if converter:
                converted = converter(value)
                # 기존 트리플 제거
                old_triples = list(g.triples((focus_ref, path_ref, None)))
                removed = False
                for old_t in old_triples:
                    if str(old_t[2]) == value or str(old_t[2]).strip() == value.strip():
                        g.remove(old_t)
                        removed = True
                        break
                if converted is not None and removed:
                    # 올바른 타입으로 재캐스트
                    g.add((focus_ref, path_ref, Literal(converted, datatype=URIRef(expected))))
                    stats["type_recast"] += 1
                elif removed:
                    # 변환 불가 값 — 트리플 제거 (비정형 값: "Primary", "N/A" 등)
                    stats["type_recast"] += 1
                else:
                    stats["unfixable"] += 1
                continue

            stats["unfixable"] += 1
            continue

        # --- Fix 1b: class constraint — 대상 인스턴스에 기대 rdf:type 추가 ---
        # `infer_types=False` 가 기본값. 이 경로는 FK 매칭 오류가 target 클래스
        # 강제 주입으로 silent 하게 묻히는 것을 막기 위해 의도적으로 비활성.
        # 부트스트랩이 필요하면 ABOX_SHACL_INFER_TYPES=true 로 opt-in.
        if constraint == "class" and expected:
            if not infer_types:
                stats["unfixable"] += 1
                continue
            expected_cls = URIRef(expected)
            # value는 ObjectProperty의 대상 인스턴스 URI
            if value:
                target_ref = URIRef(value)
                existing_types = set(g.objects(target_ref, RDF.type))
                if expected_cls not in existing_types:
                    g.add((target_ref, RDF.type, expected_cls))
                    added_type_triples.append((target_ref, expected_cls))
                    stats["type_inferred"] += 1
                    continue
            stats["unfixable"] += 1
            continue

        if constraint != "unknown":
            stats["unfixable"] += 1

    # --- Fix 2: 누락된 rdf:type 추론 (URI 패턴 기반) — opt-in ---
    # URI naming convention 에만 의존하므로 컨벤션을 어긴 인스턴스에서는
    # 오추론 위험이 있다. 기본 off, ABOX_SHACL_INFER_TYPES=true 로 활성화.
    if infer_types:
        steel_inst_str = str(DOMAIN_INST_NS)
        for s in set(g.subjects()):
            s_str = str(s)
            if not s_str.startswith(steel_inst_str):
                continue
            # 이미 rdf:type이 있으면 스킵
            types = list(g.objects(s, RDF.type))
            if types:
                continue
            # URI 패턴에서 클래스명 추론: steel-inst:ClassName_PK → ClassName
            local = s_str[len(steel_inst_str):]
            parts = local.split("_", 1)
            if parts:
                candidate_class = parts[0]
                # PascalCase 확인 (첫 글자가 대문자)
                if candidate_class and candidate_class[0].isupper():
                    cls_uri = DOMAIN_NS_OBJ[candidate_class]
                    g.add((s, RDF.type, cls_uri))
                    added_type_triples.append((s, cls_uri))
                    stats["type_inferred"] += 1

    # --- Post-fix: AllDisjointClasses 일관성 스팟체크 ---
    # 새로 추가된 rdf:type이 기존 타입과 동일 disjoint 그룹에 속하면 롤백
    if disjoint_groups and added_type_triples:
        for subj, new_type_uri in added_type_triples:
            new_type_str = str(new_type_uri)
            existing_types = {str(t) for t in g.objects(subj, RDF.type)} - {new_type_str}
            for group in disjoint_groups:
                # 새 타입과 기존 타입이 모두 같은 disjoint 그룹에 있어야 충돌
                if new_type_str not in group:
                    continue
                conflict = existing_types & group
                if conflict:
                    g.remove((subj, RDF.type, new_type_uri))
                    stats["type_inferred"] -= 1
                    stats["disjoint_conflict_reverted"] += 1
                    logger.warning(
                        "Disjoint 충돌 롤백: %s — 추가된 타입 %s가 기존 타입 %s와 같은 disjoint 그룹",
                        str(subj), _local_name(new_type_str),
                        [_local_name(c) for c in conflict],
                    )
                    break

    return stats


def _validate_and_fix_abox(g: Graph, tbox_ttl: str,
                            pre_added_types: list[tuple] | None = None) -> dict | None:
    """A-Box Graph를 SHACL 검증 후 결정론적으로 in-place 수정한다.

    Graph 객체를 직접 전달받아 serialize→parse 왕복을 제거.
    수정 후 재검증은 unfixable 수로 대체하여 pyshacl 호출을 1회로 줄임.

    Args:
        pre_added_types: FK 생성 단계 등에서 사전에 주입된 rdf:type 트리플.
            disjoint 일관성 스팟체크 대상에 포함된다.

    Returns:
        수정 결과 dict 또는 None (검증 통과 또는 실패).
    """
    try:
        # 1. SHACL shapes 생성
        shapes_g = _build_abox_shapes(tbox_ttl)
        if len(shapes_g) == 0:
            logger.info("T-Box에서 SHACL shape가 생성되지 않음 — 검증 건너뜀")
            return None

        # 2. 초기 검증 — 기본 pyrudof (Rust, ~20-30× 가속).
        # pyshacl 로 되돌리려면 SHACL_ENGINE=pyshacl 환경변수.
        _abox_shacl_engine = os.getenv("SHACL_ENGINE", "pyrudof")
        conforms_before, results_graph, results_text = _run_shacl(
            data_graph=g,
            shacl_graph=shapes_g,
            engine=_abox_shacl_engine,
            abort_on_first=False,
        )

        if conforms_before:
            return None  # 위반 없음 — 수정 불필요

        # 3. 위반 추출
        violations = _parse_abox_violations(results_graph)
        violations_before = len(violations)

        if violations_before == 0:
            return None

        # 4. 결정론적 수정 (Graph를 직접 수정 — serialize/parse 없음)
        disjoint_groups = _extract_disjoint_groups(tbox_ttl)
        fix_stats = _deterministic_fix_abox(
            g, violations,
            disjoint_groups=disjoint_groups,
            pre_added_types=pre_added_types,
        )

        # 5. 수정 후 위반 수 = unfixable 수 (재검증 생략으로 성능 개선)
        violations_after = fix_stats["unfixable"]
        conforms_after = violations_after == 0

        return {
            "conforms_before": conforms_before,
            "conforms_after": conforms_after,
            "violations_before": violations_before,
            "violations_after": violations_after,
            "fix_stats": fix_stats,
        }

    except Exception as e:
        logger.warning("A-Box SHACL 검증/수정 실패: %s", e)
        return None


# ── 컬럼→프로퍼티 매핑 ────────────────────────────


def _col_to_prop(col_name: str, class_name: str, tbox_info: dict,
                 class_prop_mapping: dict,
                 dict_contract: dict[str, dict[str, str]] | None = None) -> str | None:
    """CSV 컬럼명 → T-Box DatatypeProperty명 변환.

    ★ Domain 호환성 가드: 찾은 DP 의 domain 이 현재 class_name (또는 조상) 을
    포함하지 않으면 None 반환. 이는 OWL RL prp-dom 규칙으로 class_name
    인스턴스에 엉뚱한 rdf:type 이 전파되어 AllDisjointClasses 위반을
    유발하는 것을 원천 차단한다.
    (예: itemCode 는 domain=ItemMaster 이므로 InventoryStatus 가 사용하면
    InventoryStatus → ItemMaster 타입 전파 → disjoint 위반.)

    기능 단계: dict_contract 제공 시 해당 클래스의 DP 매핑을 **최우선** 사용.
    contract 는 시맨틱 딕셔너리 v1 (S6.5) 에서 추출된 {class: {dp_lower:
    dp_name}} 매핑. contract 미제공 (None) 이면 기존 Path B 동작 그대로.

    Lookup 순서 (첫 성공에서 반환):
      0. **dcterms:source 출처 표기** — T-Box 가 선언한 authoritative 매핑.
         이름 추측이 개입하지 않으므로 항상 최우선.
      1. vocabulary contract (딕셔너리 v1)
      2. class_prop_mapping 수동 오버라이드
      3~5. transliteration 계열 (class prefix / 전체 / semantic suffix)

    step 0 이 존재하는 이유: step 1~5 는 모두 **컬럼명을 camelCase 로 기계
    변환** 해 DP 이름을 추측한다. 그런데 T-Box 생성기 (S2) 는 배포가 선택적으로
    제공하는 컬럼 사전의 권위 한글명을 근거로 작명할 수 있으므로 (가상의 예:
    `QTY_COL_1` → "설비 계측 유량" → `processStepABottomNozzleFlowQty1`) 두 이름
    생성 경로가 서로를 알지 못하면 매칭이 구조적으로 실패한다. 그러면 해당 CSV
    컬럼은 적재되지 않고, 그 컬럼을 위해 선언된 T-Box DP 는 값 0건으로 남는다.
    """
    parts = col_name.lower().split("_")
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    camel_lower = camel.lower()

    def _accept(prop_local_lower: str) -> str | None:
        """DP 가 class_name 의 domain 과 호환되면 original name, 아니면 None."""
        actual_name = tbox_info["datatype_properties"].get(prop_local_lower)
        if actual_name is None:
            return None
        domains = tbox_info.get("dp_domains", {}).get(prop_local_lower, set())
        if not domains or _class_matches_domain(class_name, domains, tbox_info):
            return actual_name
        return None

    # step 0: T-Box 의 dcterms:source 출처 표기 (authoritative).
    # 클래스 정확 일치를 먼저 보고, 없으면 조상 클래스가 선언한 DP 를 허용
    # (상속받은 속성). domain 미지정 DP 는 키 ("", COL) 로 색인돼 있다.
    source_index = tbox_info.get("dp_by_source") or {}
    if source_index:
        col_key = col_name.strip().upper()
        for domain_key in (class_name, ""):
            hit = source_index.get((domain_key, col_key))
            if hit is not None:
                accepted = _accept(hit.lower())
                if accepted is not None:
                    return accepted
        # 조상 클래스가 선언한 DP — subclass 가 상속해 쓰는 정상 케이스.
        for (domain_key, column), dp_name in source_index.items():
            if column != col_key or not domain_key:
                continue
            if _class_matches_domain(class_name, {domain_key}, tbox_info):
                accepted = _accept(dp_name.lower())
                if accepted is not None:
                    return accepted

    # 기능 단계 (step 1): vocabulary contract 우선 조회.
    # contract 에 매칭이 있으면 T-Box domain 가드도 통과하는지 확인 후 채택.
    if dict_contract:
        contract_dp = _contract_match_dp(col_name, class_name, dict_contract)
        if contract_dp is not None:
            accepted = _accept(contract_dp.lower())
            if accepted is not None:
                return accepted
            # contract 에 있는데 T-Box 에 없거나 domain 호환 실패 — 드물지만
            # 가능 (T-Box 가 나중에 바뀐 경우). log + fallback.
            logger.debug(
                "dict_contract 매칭 %s 가 T-Box 에 없거나 domain 불일치 — fallback",
                contract_dp,
            )

    # 2. 클래스별 수동 오버라이드 (도메인 호환 확인 후 반환)
    mapping_key = f"{class_name}/{camel_lower}"
    if mapping_key in class_prop_mapping:
        mapped = class_prop_mapping[mapping_key]
        candidate = _accept(mapped.lower())
        if candidate is not None:
            return candidate

    # R24: 클래스-specific prefix 매칭 우선 (step 2 보다 먼저)
    #   예: EnergySourceMaster.Energy_Source_ID → energySourceMasterId
    #   (T-Box 에 generic `energySourceId` + specific `energySourceMasterId` 가
    #   모두 존재할 때, specific 쪽이 해당 클래스의 PK/attribute 로 의도된
    #   것이므로 우선 선택. 이전 로직은 step 2 에서 generic 을 먼저 골라서
    #   specific DP 가 미사용으로 남아 완전성 검증 FAIL 을 냈다.)
    class_prefix_cands: list[str] = []
    if class_name:
        import re as _re
        cls_tokens = _re.findall(r"[A-Z][^A-Z]*", class_name)
        col_tokens = _re.findall(r"[A-Z][^A-Z]*|^[a-z]+", camel)  # ["energy","Source","Id"]
        if cls_tokens:
            # 1-token prefix: "Soil" + "Value" → "soilValue"
            class_prefix_cands.append(
                cls_tokens[0].lower() + camel[0].upper() + camel[1:]
            )
            # 2-token prefix: "airEmission" + Column → "airEmissionColumn"
            if len(cls_tokens) >= 2:
                prefix2 = cls_tokens[0][0].lower() + cls_tokens[0][1:] + cls_tokens[1]
                class_prefix_cands.append(prefix2 + camel[0].upper() + camel[1:])
            # Full-class suffix 매칭: 컬럼 토큰 중 class_name 에 포함되지 않은
            # 마지막 부분만 full-camel class 에 붙인다.
            #   예: class="EnergySourceMaster", col_tokens=["energy","Source","Id"]
            #       → 공통 prefix ["energy","Source"] 제거 후 suffix=["Id"]
            #       → "energySourceMaster" + "Id" = "energySourceMasterId"
            full_cls_camel = cls_tokens[0].lower() + "".join(cls_tokens[1:])
            cls_tokens_lower = [t.lower() for t in cls_tokens]
            # 공통 prefix 길이
            common_len = 0
            for i, ct in enumerate(col_tokens):
                if i < len(cls_tokens_lower) and ct.lower() == cls_tokens_lower[i]:
                    common_len = i + 1
                else:
                    break
            suffix_tokens = col_tokens[common_len:]
            if suffix_tokens:
                # 첫 suffix 토큰은 대문자 시작으로 연결
                suffix_camel = "".join(
                    (t[0].upper() + t[1:]) if t else "" for t in suffix_tokens
                )
                class_prefix_cands.append(full_cls_camel + suffix_camel)

            # Path B-2: LLM 이 자주 쓰는 의미 접미사 variant 추가. CSV 컬럼 `Status`
            # → T-Box DP `equipmentStatusValue` 같은 패턴을 매칭하기 위함.
            # 원래 camel (예: "status") + semantic suffix 조합을 full_cls_camel 에
            # 붙임: "equipmentStatus" + "statusValue" 대신 "equipmentStatus" 안에
            # "status" 가 이미 포함되므로 중복 회피 — 대신 "equipmentStatusValue"
            # ("{full_cls_camel}Value") 같은 pure 접미사 변형을 시도.
            for semantic_suffix in ("Value", "Code", "Type", "Name"):
                # 컬럼 이름 자체가 class 의 일부이면 접미사만 붙인 variant
                if camel_lower in full_cls_camel.lower():
                    class_prefix_cands.append(full_cls_camel + semantic_suffix)
                # 컬럼 이름 + 접미사 (예: "status" + "Value" = "statusValue")
                # 를 class camel 에 결합
                col_with_suffix = camel + semantic_suffix
                class_prefix_cands.append(
                    full_cls_camel
                    + col_with_suffix[0].upper() + col_with_suffix[1:]
                )
        for cand in class_prefix_cands:
            accepted = _accept(cand.lower())
            if accepted is not None:
                return accepted

    # 2. T-Box 전체 매칭 (도메인 호환 확인) — class-specific DP 가 아직도 못
    # 찾아졌을 때 generic 이름으로 동일 이름 매칭 시도 (e.g. "name", "label").
    candidate = _accept(camel_lower)
    if candidate is not None:
        return candidate

    # Path B-2: generic alias 폴백 제거. 과거엔 _COMMON_DP_BY_ALIAS 에 선언된
    # 공통 DP (예: "status" → hasStatus) 로 매핑했으나, 이는 시맨틱 딕셔너리의
    # class-specific DP 매핑과 불일치해 SPARQL 쿼리 0건 원인이 됨. class-specific
    # 경로에서 못 찾으면 caller 의 _plan_common_dp_injection 이 on-demand 주입
    # (Path B-3) 하거나 None 반환.
    return None


def _resolve_common_dp_name(col_name: str) -> str | None:
    """CSV 컬럼명을 공통 DP 이름(rules/contracts/common_dp.json 기반)으로 정규화.

    Returns the canonical DP name (e.g. 'hasTimestamp') if the column is a
    known alias, else None. T-Box 자동 주입 시 사용.
    """
    parts = col_name.lower().split("_")
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    return _COMMON_DP_BY_ALIAS.get(camel.lower())


def _plan_common_dp_injection(
    tbox_info: dict,
    csv_files: list[str],
    filter_names: set[str] | None,
) -> list[dict]:
    """CSV 헤더에서 unmapped 인 공통 DP 를 식별해 주입 계획을 반환.

    각 item: {"dp_name", "range", "domain_class", "label_en", "label_ko",
             "comment_en", "comment_ko", "source_columns": [(table, col), ...]}

    같은 (dp_name, domain_class) 는 한 번만 생성되도록 중복 제거.
    """
    plan_by_key: dict[tuple[str, str], dict] = {}
    for csv_path in csv_files:
        filename = os.path.basename(csv_path).replace(".csv", "")
        if filter_names and filename not in filter_names:
            continue
        cls_name = _table_to_class(filename, tbox_info)
        try:
            with open(csv_path, encoding="utf-8-sig") as f:
                header_line = f.readline()
        except Exception:
            continue
        if not header_line:
            continue
        for col in header_line.strip().split(","):
            col = col.strip().strip('"')
            if not col:
                continue
            # 이미 T-Box 에 해당 class 의 DP 로 매핑 가능하면 스킵
            mapping_key = f"{cls_name}/{col.lower()}"
            existing = _col_to_prop(col, cls_name, tbox_info, {})
            if existing is not None:
                continue
            # common_dp.json 의 generic entry 를 **range / label 추론용** 으로만
            # 사용. 실제 주입되는 DP 이름은 class-specific (Path B-3).
            common_name = _resolve_common_dp_name(col)
            if common_name is None:
                continue
            decl = _COMMON_DP_BY_NAME.get(common_name, {})
            # Path B-3: class-specific DP 이름 생성 — {classCamelLower}{CommonCamel}
            # 예: EquipmentStatus + hasTimestamp → equipmentStatusTimestamp
            #     AirEmissionMonitoring + hasTimestamp → airEmissionMonitoringTimestamp
            class_camel_lower = cls_name[:1].lower() + cls_name[1:] if cls_name else ""
            # common_name (hasXxx) 에서 "has" prefix 제거 후 class camel 뒤에 붙임
            bare_common = common_name[3:] if common_name.startswith("has") else common_name
            class_specific_name = class_camel_lower + bare_common
            key = (class_specific_name, cls_name)
            if key in plan_by_key:
                plan_by_key[key]["source_columns"].append((filename, col))
                continue
            plan_by_key[key] = {
                "dp_name": class_specific_name,
                "range": decl.get("range", str(XSD.string)),
                "domain_class": cls_name,
                # 라벨은 generic 메타데이터 상속 (의미는 같으므로)
                "label_en": decl.get("label_en", class_specific_name),
                "label_ko": decl.get("label_ko", class_specific_name),
                "comment_en": decl.get("comment_en", ""),
                "comment_ko": decl.get("comment_ko", ""),
                "source_columns": [(filename, col)],
                # 역추적용 — 어느 generic entry 를 템플릿으로 썼는지
                "_derived_from_common": common_name,
            }
    return list(plan_by_key.values())


def _inject_common_dps(
    tbox_ttl: str,
    injection_plan: list[dict],
) -> tuple[str, dict]:
    """주입 계획에 따라 class-specific DP 를 T-Box TTL 에 추가 (Path B-3).

    각 item 은 unique (class_specific_dp_name, domain_class) 쌍. 예:
        EquipmentStatus + Timestamp → equipmentStatusTimestamp (domain: EquipmentStatus)
        AlarmEvents + Timestamp → alarmEventsTimestamp (domain: AlarmEvents)

    Path A 의 "domain 비워둠 → owl:Thing 동등" 패턴을 폐기. rdfs:domain 을 명시해
    시맨틱 딕셔너리가 classes.X.datatype_properties 에 등재 가능하도록 함.

    Returns:
        (updated_ttl, stats dict with {"injected": int, "by_name": {...}}).
    """
    if not injection_plan:
        return tbox_ttl, {"injected": 0, "by_name": {}}

    g = _new_graph()
    g.parse(data=tbox_ttl, format="turtle")

    injected = 0
    by_name: dict[str, int] = {}
    declared_with_domain: set[tuple[str, str]] = set()

    # 이미 T-Box 에 선언된 (DP, domain) 쌍 집합
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        nm = _local_name(str(dp))
        if not nm:
            continue
        domains = [_local_name(str(d)) for d in g.objects(dp, RDFS.domain) if isinstance(d, URIRef)]
        if not domains:
            # domain 없는 기존 DP 는 (nm, "") 키로 관리 — 같은 이름 재선언 방지만
            declared_with_domain.add((nm, ""))
        else:
            for d in domains:
                declared_with_domain.add((nm, d))

    for item in injection_plan:
        dp_name = item["dp_name"]
        domain_class = item.get("domain_class", "")
        if (dp_name, domain_class) in declared_with_domain:
            continue
        # 같은 DP 이름이 다른 class 에 이미 선언되어 있어도 class-specific 이름은
        # 보통 unique (class prefix 포함) — (dp_name, "") 에 있으면 domain 만 추가.
        dp_uri = DOMAIN_NS_OBJ[dp_name]
        if (dp_name, "") not in declared_with_domain:
            # 새 DP 선언
            g.add((dp_uri, RDF.type, OWL.DatatypeProperty))
            g.add((dp_uri, RDFS.range, URIRef(item["range"])))
            if item.get("label_en"):
                g.add((dp_uri, RDFS.label, Literal(item["label_en"], lang="en")))
            if item.get("label_ko"):
                g.add((dp_uri, RDFS.label, Literal(item["label_ko"], lang="ko")))
            if item.get("comment_en"):
                g.add((dp_uri, RDFS.comment, Literal(item["comment_en"], lang="en")))
            if item.get("comment_ko"):
                g.add((dp_uri, RDFS.comment, Literal(item["comment_ko"], lang="ko")))
            # 출처 컬럼 표기 — 이 DP 를 유발한 CSV 컬럼. 다음 실행에서
            # `_col_to_prop` step 0 이 추측 없이 같은 매핑을 재현하고, Step 12e
            # 게이트가 주입분까지 커버리지에 포함한다.
            for _src_file, _src_col in item.get("source_columns") or ():
                g.add((dp_uri, _DCTERMS_NS.source, Literal(_src_col)))
        # Path B-3: rdfs:domain 명시 주입 — 시맨틱 딕셔너리가 class-DP 매핑 가능.
        if domain_class:
            g.add((dp_uri, RDFS.domain, DOMAIN_NS_OBJ[domain_class]))
            declared_with_domain.add((dp_name, domain_class))
        else:
            declared_with_domain.add((dp_name, ""))
        injected += 1
        by_name[dp_name] = by_name.get(dp_name, 0) + 1

    if injected == 0:
        return tbox_ttl, {"injected": 0, "by_name": {}}

    from domain.tbox_utils import fast_serialize_turtle
    updated = fast_serialize_turtle(g)
    return updated, {"injected": injected, "by_name": by_name}


# ── 테이블명→클래스명 ─────────────────────────────


def _table_to_class(table_name: str, tbox_info: dict) -> str:
    # 1순위: table_class_mapping.json 의 명시적 도메인 클래스 매핑.
    # T-Box 에 실제 존재하는 클래스일 때만 채택 — 매핑이 가리키는 도메인 클래스가
    # T-Box 에 있어야 인스턴스 타입이 FK/OP 대상과 일치한다.
    explicit = _load_table_class_mapping().get(table_name)
    if explicit and explicit.lower() in tbox_info["classes"]:
        return tbox_info["classes"][explicit.lower()]
    # 2순위(폴백): PascalCase 테이블명 → T-Box 동명 클래스 → 원본 PascalCase.
    parts = table_name.replace("-", "_").split("_")
    pascal = "".join(p.capitalize() for p in parts)
    return tbox_info["classes"].get(pascal.lower(), pascal)


# ── Path B-1: class-specific ID DP 탐지 ─────────────────────────────
# 기존 hasIdentifier 하드코딩 대신 T-Box 에 이미 선언된 class-specific
# ID DP ({classCamelLower}Id 등) 을 사용. 없으면 dcterms:identifier fallback.
# 이렇게 하면 시맨틱 딕셔너리의 classes.X.datatype_properties 에 자연스럽게
# 등재되어 LLM NL→SPARQL 이 정확한 프로퍼티로 쿼리 생성.

_ID_DP_CANDIDATE_SUFFIXES = ("id", "code", "number", "no")


def _class_specific_id_dp(
    class_name: str, tbox_info: dict,
    pk_column_hint: "list[str] | tuple[str, ...] | None" = None,
) -> str | None:
    """클래스의 T-Box 에 선언된 ID DP 이름을 찾는다.

    우선순위:
    1. `{classCamelLower}Id` (예: EquipmentMaster → equipmentMasterId)
    2. `{stripMasterCamelLower}Id` (예: EquipmentMaster → equipmentId)
    3. 이 클래스의 domain 에 속한 DP 중 suffix 가 id/code/number/no 이고
       **`dcterms:source` 가 PK 컬럼을 가리키는** 것 (pk_column_hint 필요)

    ``pk_column_hint``: 이 테이블의 PK 컬럼명. Candidate 3 이 무관한 DP 를 고르는
    것을 막는 데만 쓰인다 (없으면 Candidate 3 은 건너뛴다).

    Returns class-specific DP local name, or None if not found.
    Caller 는 None 시 dcterms:identifier 폴백.
    """
    if not class_name:
        return None

    class_props = tbox_info.get("class_props", {}).get(class_name, {})  # {lower: name}
    if not class_props:
        return None

    # Candidate 1: {classCamelLower}Id — 예: EquipmentMaster → equipmentmasterid
    class_camel_lower = class_name[:1].lower() + class_name[1:] if class_name else ""
    for suffix in _ID_DP_CANDIDATE_SUFFIXES:
        candidate_key = (class_camel_lower + suffix).lower()
        if candidate_key in class_props:
            return class_props[candidate_key]

    # Candidate 2: `Master` / `Events` / `History` 접미어 제거 후 재시도
    stripped = class_name
    for bare_suffix in ("Master", "Events", "Event", "History", "Record", "Status", "Data"):
        if stripped.endswith(bare_suffix) and len(stripped) > len(bare_suffix):
            stripped = stripped[: -len(bare_suffix)]
            break
    stripped_camel_lower = stripped[:1].lower() + stripped[1:] if stripped else ""
    if stripped_camel_lower and stripped_camel_lower != class_camel_lower:
        for suffix in _ID_DP_CANDIDATE_SUFFIXES:
            candidate_key = (stripped_camel_lower + suffix).lower()
            if candidate_key in class_props:
                return class_props[candidate_key]

    # Candidate 3: 해당 class 의 DP 중 id/code/number/no 접미사로 끝나는 것.
    #
    # 이 단계는 **PK 컬럼에서 유래한 DP 만** 받아들인다. 접미사만 보고 "가장 짧은
    # 이름" 을 고르면 무관한 DP 에 PK 값이 섞인다. 가상의 예:
    #
    #   MaterialADetail.materialADetailMcNo (기계번호)      "3"  +  "MAT003 541"  ← PK
    #   ProcessResultD.processResultDYpMid    "0"  +  "COIL001"    ← PK
    #   OrderToleranceSpec.…IdCol5  감사컬럼 + 주문번호     ← PK
    #
    # 값 2개가 한 DP 에 공존하면 그 DP 로 필터하는 질의가 조용히 틀리고, 딕셔너리
    # 통계(예시값·distinct)도 오염된다. 근거 없이 추측하는 것보다 dcterms:identifier
    # 폴백이 안전하므로, PK 출처가 확인되지 않으면 None 을 반환한다.
    pk_columns = {c.upper() for c in (pk_column_hint or ())}
    if pk_columns:
        # dp_by_source is {(class, UPPER_COLUMN): dp_local_name} — the same index
        # _col_to_prop resolves columns through, so PK provenance is exact here.
        by_source = tbox_info.get("dp_by_source") or {}
        pk_dps = {
            by_source[(class_name, col)] for col in pk_columns
            if (class_name, col) in by_source
        }
        matches = [
            name for key, name in class_props.items()
            if any(key.endswith(s) for s in _ID_DP_CANDIDATE_SUFFIXES)
            and name in pk_dps
        ]
        if matches:
            return min(matches, key=len)

    return None


# ── 기능 단계: 시맨틱 딕셔너리 vocabulary contract ──────────────────
# A-Box 생성기가 class-specific DP 이름만 사용하도록 강제. contract 는
# 시맨틱 딕셔너리 v1 (include_stats=False) 의 classes.X.datatype_properties
# 에서 추출한 {class_name: {dp_lower: dp_name}} 매핑. generate_abox 가
# 이 contract 를 참조해:
#   (a) CSV 컬럼이 contract 의 DP 로 매핑 가능하면 그것만 사용
#   (b) contract 에 없으면 T-Box 직접 참조로 fallback (backward compat)
# contract=None 이면 기존 Path B 동작 그대로.


def _load_dict_contract(path: str) -> dict[str, dict[str, str]]:
    """시맨틱 딕셔너리 파일에서 vocabulary contract 추출.

    Args:
        path: semantic_dictionary.json 경로.

    Returns:
        {class_name: {dp_lower: dp_name}} 매핑. 파일 없거나 손상 시 빈 dict.

    Contract 는 딕셔너리의 classes.X.datatype_properties 키를 집계해 만듦 —
    v1 (include_stats=False) 과 v2 (include_stats=True) 모두 호환 (같은 구조).
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("dict_contract 로드 실패 (%s): %s", path, e)
        return {}

    contract: dict[str, dict[str, str]] = {}
    classes = data.get("classes", {}) or {}
    if not isinstance(classes, dict):
        return {}
    for cls_name, info in classes.items():
        if not isinstance(info, dict):
            continue
        dt_props = info.get("datatype_properties", {}) or {}
        if not isinstance(dt_props, dict):
            continue
        class_entry = {
            dp_name.lower(): dp_name for dp_name in dt_props.keys()
        }
        # 딕셔너리가 실어온 출처 컬럼 (T-Box dcterms:source 유래) 을 조회 키로도
        # 등재. `_contract_match_dp` 는 컬럼명 camelCase 변환으로 조회하므로,
        # 원본 컬럼 코드를 같은 형태로 정규화해 넣어주면 이름 추측 없이 적중한다.
        # T-Box 인덱스 (step 0) 와 중복이지만, 딕셔너리를 계약서로 쓰는 배포에서
        # T-Box 를 다시 파싱하지 않고도 동작하게 하는 이중 안전망이다.
        for dp_name, dp_info in dt_props.items():
            if not isinstance(dp_info, dict):
                continue
            for column in dp_info.get("source_columns") or ():
                if not isinstance(column, str) or not column.strip():
                    continue
                parts = column.strip().lower().split("_")
                camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
                class_entry.setdefault(camel.lower(), dp_name)
        contract[cls_name] = class_entry
    return contract


# Category prefix 목록 — Architect 가 DP 이름에서 드롭할 수 있는 접두어.
# 예: class=ProcessBlastFurnace, DP=blastFurnaceHotAirTempC (Process 드롭).
# Matcher 는 이 접두어를 자동으로 벗겨 class variant 도 시도한다.
_CLASS_CATEGORY_PREFIXES: tuple[str, ...] = (
    "process", "inventory", "supply", "supplier",
    "energy", "equipment", "environment", "quality",
    "material", "manufacturing", "production",
)

# Category suffix 목록 — 클래스명 끝에 붙어있는 카테고리성 suffix.
# 예: class=ProcessSteelmakingFurnace, DP=steelmakingMoltenSteelTempC
# (Process prefix + Furnace suffix 둘 다 드롭).
_CLASS_CATEGORY_SUFFIXES: tuple[str, ...] = (
    "furnace", "master", "event", "events", "monitoring",
    "history", "transaction", "management", "data", "status",
    "mapping", "analysis", "result", "results", "properties",
    "quality", "consumption", "efficiency", "emission",
)


def _class_name_variants(class_name: str) -> list[str]:
    """Generate class name variants by stripping known category prefixes/suffixes.

    Architect 가 DP 이름에서 category prefix/suffix 를 선택적으로 드롭하는
    패턴을 따라간다. 예:
      - ProcessBlastFurnace → BlastFurnace (prefix drop)
      - ProcessSteelmakingFurnace → Steelmaking (prefix + suffix drop)
      - AlarmEvents → Alarm (suffix drop)

    Returns candidates in priority order (original first, then strips).
    """
    variants = [class_name]
    seen = {class_name.lower()}

    def _add(candidate: str) -> None:
        if not candidate or candidate[0].islower():
            return
        key = candidate.lower()
        if key in seen or len(candidate) < 3:
            return
        seen.add(key)
        variants.append(candidate)

    lower = class_name.lower()

    # Prefix strips
    prefix_stripped_list: list[str] = []
    for prefix in _CLASS_CATEGORY_PREFIXES:
        if lower.startswith(prefix) and len(class_name) > len(prefix) + 2:
            stripped = class_name[len(prefix):]
            if stripped and stripped[0].isupper():
                prefix_stripped_list.append(stripped)
                _add(stripped)

    # Suffix strips (applied to original + prefix-stripped variants)
    for base in [class_name] + prefix_stripped_list:
        base_lower = base.lower()
        for suffix in _CLASS_CATEGORY_SUFFIXES:
            if base_lower.endswith(suffix) and len(base) > len(suffix) + 2:
                stripped = base[: -len(suffix)]
                _add(stripped)

    return variants


def _column_camel_variants(col_name: str) -> list[str]:
    """Generate camelCase column variants with chemical element expansion.

    Returns list of (camelCase, camelLower) pairs for:
      1. Original tokens (as-is)
      2. Tokens with element abbrevs expanded (c→carbon, si→silicon 등)
    """
    # Reuse ontology_quality's element map (same chemistry, single source of truth)
    try:
        from tools.ontology_quality import _ELEMENT_ABBREVIATIONS
    except ImportError:
        _ELEMENT_ABBREVIATIONS = {}

    parts = col_name.lower().split("_")
    if not parts:
        return []

    def _to_camel(tokens: list[str]) -> str:
        return tokens[0] + "".join(p.capitalize() for p in tokens[1:])

    variants: list[list[str]] = [parts]
    # Expand element abbreviations in any position
    for idx, tok in enumerate(parts):
        if tok in _ELEMENT_ABBREVIATIONS:
            expanded = parts.copy()
            expanded[idx] = _ELEMENT_ABBREVIATIONS[tok]
            if expanded not in variants:
                variants.append(expanded)

    return [_to_camel(v) for v in variants]


def _contract_match_dp(
    col_name: str, class_name: str,
    contract: dict[str, dict[str, str]] | None,
) -> str | None:
    """Contract 의 class-specific DP 매핑 조회. 없으면 None (caller fallback).

    매칭 전략 (순차 시도):
      1. class 의 DP 목록에서 camelLower 컬럼명 정확 매치
      2. {classCamelLower}{ColumnCamel} 직접 구성 후 매치
      3. class name 에서 category prefix (Process/Inventory 등) 를 벗긴 variant
         로 2번 재시도 (예: ProcessBlastFurnace → BlastFurnace)
      4. 컬럼명의 화학 원소 축약 (C/Si/Mn 등) 을 풀네임 (carbon/silicon/manganese)
         으로 확장한 variant 로 2번 재시도 — ChemicalAnalysis 등 화학 도메인
      5. 실패 시 None
    """
    if not contract or not class_name:
        return None
    class_dps = contract.get(class_name)
    if not class_dps:
        return None

    # Column camelCase variants (original + element-expanded)
    col_camels = _column_camel_variants(col_name)
    if not col_camels:
        return None

    # 1. camelLower 정확 매칭 (variant 전부 시도)
    for camel in col_camels:
        if camel.lower() in class_dps:
            return class_dps[camel.lower()]

    # 2+3+4. class prefix + camel 조합 — class variants × column variants
    for cls_variant in _class_name_variants(class_name):
        class_camel_lower = cls_variant[:1].lower() + cls_variant[1:]
        for camel in col_camels:
            combined = class_camel_lower + camel[:1].upper() + camel[1:]
            if combined.lower() in class_dps:
                return class_dps[combined.lower()]

    # 5. 실패
    return None


# ── Loss Manifest ────────────────────────────────


def _load_csv_rows(
    csv_path: str, filename: str, prefetched: dict[str, str] | None,
    prefetch_limit_bytes: int, table_index: int, table_total: int,
    class_name: str,
) -> tuple[bool, list[dict], list[dict], int]:
    """CSV 로드 — streaming/prefetch 분기 + 데이터 없는 경우 skip 시그널.

    Returns: (use_streaming, rows, pk_sample_rows, csv_bytes).
    rows 는 비-streaming 시 전체 행, streaming 시 빈 list.
    pk_sample_rows 는 streaming 시 PK detect 용 첫 20K 행 샘플.

    데이터 없는 빈 CSV 면 (False, [], [], 0) 반환 — 호출자 가 skip 판단.
    """
    in_prefetch = prefetched is not None and csv_path in prefetched
    try:
        csv_bytes = os.path.getsize(csv_path)
    except OSError:
        csv_bytes = 0
    use_streaming = not in_prefetch and csv_bytes > prefetch_limit_bytes

    rows: list[dict] = []
    pk_sample_rows: list[dict] = []

    if use_streaming:
        with open(csv_path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for i, r in enumerate(reader):
                pk_sample_rows.append(r)
                if i + 1 >= 20000:
                    break
        if not pk_sample_rows:
            logger.info("테이블 %s: 데이터 없음 (헤더만), 건너뜀", filename)
            return use_streaming, rows, pk_sample_rows, csv_bytes
        logger.info(
            "A-Box 진행 [%d/%d] %s: streaming 모드 (%d.%d MB) → %s 생성 중...",
            table_index, table_total, filename,
            csv_bytes // (1024 * 1024),
            (csv_bytes % (1024 * 1024)) // (1024 * 100),
            class_name,
        )
        return use_streaming, rows, pk_sample_rows, csv_bytes

    if in_prefetch:
        csv_content = prefetched.pop(csv_path)
    else:
        with open(csv_path, encoding="utf-8-sig") as f:
            csv_content = f.read()
    if not csv_content.strip():
        return use_streaming, rows, pk_sample_rows, csv_bytes
    lines = csv_content.strip().split("\n")
    rows = list(csv.DictReader(lines))
    logger.info(
        "A-Box 진행 [%d/%d] %s: %d행 → %s 생성 중...",
        table_index, table_total, filename, len(rows), class_name,
    )
    if not rows:
        logger.info("테이블 %s: 데이터 없음 (헤더만), 건너뜀", filename)
    elif len(rows) > 100000:
        logger.warning("대용량 CSV: %s (%d행) — 메모리 사용 주의", filename, len(rows))
    return use_streaming, rows, pk_sample_rows, csv_bytes


def _classify_master_or_transaction(
    filename: str, class_name: str, ctx,
    use_streaming: bool, rows: list[dict], pk_sample_rows: list[dict],
    csv_bytes: int,
) -> None:
    """파일명 suffix + timestamp 컬럼 + 행 수로 master/transaction 결정 후 ctx 등록."""
    first_row_keys = (
        pk_sample_rows[0].keys() if use_streaming and pk_sample_rows
        else (rows[0].keys() if rows else [])
    )
    header_lower = {c.lower() for c in first_row_keys}
    has_timestamp = (
        "timestamp" in header_lower
        or any("datetime" in c for c in header_lower)
    )
    is_explicit_master = any(filename.endswith(s) for s in _MASTER_FILENAME_SUFFIXES)
    is_explicit_transaction = any(
        filename.endswith(s) for s in _TRANSACTION_FILENAME_SUFFIXES
    )
    if is_explicit_master:
        is_master = True
    elif is_explicit_transaction:
        is_master = False
    else:
        is_master = (
            not has_timestamp
            and (csv_bytes if use_streaming else len(rows)) <= 200
            and not use_streaming
        )
    if is_master:
        ctx.master_classes.add(class_name)
    else:
        ctx.transaction_classes.add(class_name)


def _record_column_coverage(
    class_name: str, ctx, tbox_info: dict, class_prop_mapping: dict,
    dict_contract: dict[str, dict[str, str]] | None,
    use_streaming: bool, rows: list[dict], pk_sample_rows: list[dict],
) -> None:
    """헤더 → 매핑 가능한 column / FK / unmapped 분류 후 ctx.per_class_col_coverage 기록."""
    mapped_cols = 0
    unmapped_cols = 0
    unmapped_col_names: list[str] = []
    header_keys = (
        list(pk_sample_rows[0].keys()) if use_streaming and pk_sample_rows
        else (list(rows[0].keys()) if rows else [])
    )
    for col in header_keys:
        if not col:
            continue
        cov_prop = _col_to_prop(
            col, class_name, tbox_info, class_prop_mapping,
            dict_contract=dict_contract,
        )
        if cov_prop is not None:
            mapped_cols += 1
            continue
        if _fk_column_to_class(col.lower().replace("_", "")) is not None:
            continue
        unmapped_cols += 1
        unmapped_col_names.append(col)
    total = mapped_cols + unmapped_cols
    ctx.per_class_col_coverage[class_name] = {
        "mapped": mapped_cols,
        "unmapped": unmapped_cols,
        "total": total,
        "rate": round(mapped_cols / max(total, 1) * 100, 1),
        "unmapped_columns": unmapped_col_names,
    }


def _is_composite_pk(pk_column) -> bool:
    """PK 가 두 컬럼 이상으로 합성되는가 (``"|"`` join 대상).

    단일 원소 리스트는 composite 가 아니다 — 값이 CSV 에 그대로 있으므로 조회 키로
    쓸 수 있다. 이 구분이 없으면 정상 PK 까지 식별자를 잃는다.
    """
    return isinstance(pk_column, list | tuple) and len(pk_column) > 1


def _emit_pk_identifier(
    pk_value: str | None, pk_column, row: dict, class_name: str,
    inst_uri: URIRef, tbox_info: dict, writer: _BulkNTWriter,
) -> None:
    """PK 식별 literal → class-specific ID DP / dcterms:identifier (Path B-1).

    composite PK 면 "|" 구분 join. 한 컬럼이라도 비어있으면 literal 생성 skip.

    ## composite PK 는 ``dcterms:identifier`` 폴백을 타지 않는다 (2026-08-28)

    실측: ``dcterms:identifier`` 59,280건(인스턴스의 84%)이 전부
    ``"TAG001|2025-09-01 00:00:00"`` 형태 합성 조인키였다. 6개 클래스가 그 폴백을
    탔고(RealTimeData 36,000 / EquipmentStatus 6,000 / Process_* 4×4,320) 전부
    composite PK 테이블이다.

    문제는 그 값이 **정보를 추가하지 않으면서 표준 어휘를 오염시킨다**는 것이다.
    같은 개체를 뜯어보면 구성 요소가 이미 온전히 있다::

        realTimeDataTagIdRef     "TAG001"                       ← 외부 조회 가능
        realTimeDataTimestamp    "2025-09-01T00:00:00"          ← 타입 있는 dateTime
        hasRealTimeTag           TagMaster_TAG001               ← OP 링크
        identifier               "TAG001|2025-09-01 00:00:00"   ← 위 둘을 뭉갠 중복

    ``dcterms:identifier`` 는 "이 개체의 식별자" 라는 표준 어휘이므로 외부 시스템
    (MES/ERP)이 그 값으로 조회한다. 파이프 합성값은 어느 시스템에도 없어 조인이
    불가능하고, LLM 도 그것을 조회 키로 오인한다. 개체 식별은 IRI 가 이미 한다
    (``RealTimeData_TAG001_2025-09-01_00_00_00``).

    단일 컬럼 PK 는 그대로 유지한다 — 그 값은 CSV 에 실제로 있는 조회 키다.

    ## composite PK 는 class-specific ID DP 에도 합성값을 쓰지 않는다 (2026-08-30)

    위 ``dcterms:identifier`` 폴백을 막은 뒤에도 **같은 합성값이 한 칸 옆으로
    옮겨갔을 뿐**이었다. 실측 15,180건이 class-specific ID DP 에 박혔고, 결과는
    한 개체가 **같은 DP 에 모순된 값 2개**를 갖는 상태다::

        ProcessSteelmakingFurnace_P015_2025-09-30_23_50_00
            steel:steelmakingProductId "P015"                        ← 컬럼 경로
            steel:steelmakingProductId "P015|2025-09-30 23:50:00"    ← 이 함수 (오염)

    ``steelmakingProductId`` 의 ``dcterms:source`` 는 ``Product_ID`` **한 컬럼**이므로
    합성값은 그 DP 의 선언 의미와 맞지 않는다. 게다가 값이 잉여다 — 컴포넌트가 이미
    각자의 DP 에 온전히 들어있다 (``steelmakingProductId`` + ``steelmakingTimestamp``).

    피해 5개 DP (steelmakingProductId 4,320 / rollingProductId 4,320 /
    continuousCastingProductId 4,320 / energyEfficiencyEquipmentId 1,500 /
    electricalConsumptionEquipmentId 720) 는 전부 clean 값과 **정확히 1:1** 로
    공존했다 — 즉 컬럼 경로가 이미 정답을 쓰고 있어 이 경로는 순수 가산 오염이다.

    **어떤 게이트도 잡지 못했다**: 그 ID DP 들이 ``owl:FunctionalProperty`` 가 아니라
    추론기가 침묵하고, ``column_coverage`` 는 이름 해석만 세므로 100% 를 보고한다.
    """
    row_lower_map = {k.lower(): k for k in row}
    pk_id_literal: str | None = None
    if isinstance(pk_column, str) and pk_column in row_lower_map:
        pk_raw_val = str(row.get(row_lower_map[pk_column], "")).strip()
        if pk_raw_val:
            pk_id_literal = pk_raw_val
    elif isinstance(pk_column, list) and len(pk_column) == 1:
        # 단일 원소 리스트는 composite 가 아니다 (_is_composite_pk 와 같은 판정) —
        # 값이 CSV 에 그대로 있으므로 조회 키로 쓸 수 있다.
        orig = row_lower_map.get(pk_column[0].lower())
        if orig is not None:
            v = str(row.get(orig, "")).strip()
            if v:
                pk_id_literal = v
    if not pk_id_literal:
        # composite PK 는 여기서 끝난다. 합성 조인키는 어느 시스템에도 없는 값이므로
        # ID DP 든 dcterms:identifier 든 **어디에도** 쓰지 않는다. 컴포넌트 값은
        # ``_process_row_columns`` 의 컬럼 경로가 각자의 DP 에 이미 적재한다.
        return
    pk_cols = (
        [pk_column] if isinstance(pk_column, str)
        else list(pk_column) if isinstance(pk_column, list) else []
    )
    id_dp_local = _class_specific_id_dp(class_name, tbox_info, pk_column_hint=pk_cols)
    if id_dp_local:
        # T-Box 의 선언 range 를 따른다. string 으로 고정하면 range 가 다른 DP 에서
        # **같은 값이 두 타입으로 중복 적재**된다. 가상의 예:
        #   processStepBIdCol3 "10000000001"^^xsd:decimal , "10000000001"
        # (컬럼 경로는 range=decimal 로, PK 경로는 string 으로 넣는 경우.) 값 개수가
        # 인스턴스 수의 2배가 되어 딕셔너리 통계가 어긋나고, 타입으로 필터하는
        # 질의가 한쪽만 잡는다. 변환 불가 시엔 string 으로 폴백한다.
        pk_range = (tbox_info.get("dp_ranges") or {}).get(id_dp_local.lower(), "")
        pk_literal = (
            _format_value(pk_id_literal, id_dp_local, tbox_range=pk_range)
            if pk_range else None
        )
        if pk_literal is None:
            pk_literal = Literal(pk_id_literal, datatype=XSD.string)
        writer.add((inst_uri, DOMAIN_NS_OBJ[id_dp_local], pk_literal))
    elif not _is_composite_pk(pk_column):
        # 단일 컬럼 PK 만 폴백한다 — 그 값은 CSV 에 실재하는 조회 키다.
        writer.add((
            inst_uri, _DCTERMS_NS.identifier,
            Literal(pk_id_literal, datatype=XSD.string),
        ))


def _is_fk_candidate(col_normalized: str) -> bool:
    """FK 후보 컬럼명인지 판정.

    판정 근거는 두 종류다:

    1. ``fk_patterns.json`` 의 **명시 매핑** — 운영자가 "이 컬럼은 이 클래스의 FK 다"
       라고 직접 선언한 것. 이름 규칙보다 **강한 근거** 이므로 접미사와 무관하게 채택한다.
    2. 접미사 휴리스틱 (``id`` / ``code``) — 선언되지 않은 컬럼을 자동 인식.

    ## 왜 1번이 필요한가 (2026-08-17 실측)

    예전에는 명시 매핑을 ``no``/``ref``/``num`` 접미사에만 곁들여 봤다. 그래서
    ``fk_patterns.json`` 에 등록됐는데도 접미사가 안 맞으면 **조용히 무시** 됐다 —
    설정을 읽지만 적용하지 않는 죽은 설정이다.

    실측 피해: ``Transportation.csv`` 의 ``Origin_Warehouse`` /
    ``Destination_Warehouse`` 둘 다 ``fk_patterns.json`` 에서
    ``WarehouseMaster`` 로 매핑돼 있는데 (접미사가 ``warehouse``) 이 판정이 거부해
    **FK 경로에 진입조차 못 했다**. 운송 300건의 창고 관계가 A-Box 에 없었고,
    KG 에 보이던 ``hasDestinationWarehouse`` 300건은 CSV 가 아니라
    ``tacit_cq_shortcuts.ttl`` 이 채워 넣은 것이었다 — 즉 CSV 사실이 아니라 수동
    보정이 그 자리를 덮고 있었다.

    ``fk_patterns.json`` 19개 엔트리 중 2개가 이 방식으로 죽어 있었고, 둘 다
    실제 CSV 에 존재하는 컬럼이었다.
    """
    if col_normalized in _FK_PATTERNS:
        return True                        # 명시 선언 — 이름 규칙보다 강한 근거
    return (
        col_normalized.endswith("id")
        or col_normalized.endswith("code")
    )


def _process_row_columns(
    row: dict, inst_uri: URIRef, class_name: str, pk_column,
    ctx, writer: _BulkNTWriter,
    tbox_info: dict, class_prop_mapping: dict, obj_props: list,
    available_classes: set[str], master_instance_uris: set[str],
    master_value_index: dict[str, dict[str, str]] | None,
    fuzzy_config: dict | None,
    dict_contract: dict[str, dict[str, str]] | None,
    sentinel_value_cols: set[str] | None = None,
) -> tuple[dict[str, dict], dict[str, dict], int, int]:
    """단일 row 의 모든 컬럼 처리 — FK OP / DP literal 추가 + provenance 수집.

    ``sentinel_value_cols``: 모호한 센티널("-")을 실제 값으로 적재할 컬럼
    (lowercased). :func:`_detect_sentinel_value_columns` 가 테이블 단위로 계산.

    Returns: (prov_props, prov_rels, coercion_fail_count, sentinel_filtered_count).
    """
    prov_props: dict[str, dict] = {}
    prov_rels: dict[str, dict] = {}
    fk_processed_cols: set[str] = set()
    coercion_fail_count = 0
    sentinel_filtered_count = 0

    for col_name, value in row.items():
        if not col_name or value is None or not str(value).strip():
            continue
        value = _normalize_value(str(value), col_name)
        if not value:
            continue
        col_lower = col_name.lower()

        if isinstance(pk_column, list):
            in_composite_pk = col_lower in pk_column
            is_self_pk = False
        else:
            in_composite_pk = False
            is_self_pk = (pk_column is not None and col_lower == pk_column)

        col_normalized = col_lower.replace("_", "")
        # PK 컬럼이 동시에 *다른* 클래스로의 FK 인 경우(PK=FK 1:1 관계,
        # 예: 자기 PK 이면서 동시에 다른 클래스로의 FK 인 컬럼)
        # FK 처리를 막으면 안 된다. 자기 클래스를 가리키는 self-reference 일
        # 때만 진짜 self-pk 로 보고 FK 처리에서 제외한다.
        fk_target_cls = _FK_PATTERNS.get(col_normalized)
        pk_is_foreign_fk = (
            is_self_pk
            and fk_target_cls is not None
            and fk_target_cls != class_name
        )
        if _is_fk_candidate(col_normalized) and (not is_self_pk or pk_is_foreign_fk):
            # **원본 컬럼명** 을 넘긴다 (``col_normalized`` 아님).
            # ``_detect_fk_property`` 는 내부에서 어차피 정규화하므로 결과는 같지만,
            # 방향 판정(``_column_direction_affinity``)은 **단어 경계** 를 봐야 한다.
            # ``originwarehouse`` 처럼 정규화된 문자열은 ``origin`` 을 단어로 인식할 수
            # 없어 판정이 조용히 무력화된다 (실측: 이 인자 때문에 출발 창고 300건이
            # 계속 소실됐다 — 단위 테스트는 raw 를 직접 넘겨 통과했다).
            fk_results = _detect_fk_property(
                class_name, col_name, value, obj_props,
                available_classes=available_classes,
                master_instance_uris=master_instance_uris,
                master_value_index=master_value_index,
                fuzzy_config=fuzzy_config,
            )
            if fk_results and not in_composite_pk:
                fk_processed_cols.add(col_lower)
            op_added, promoted_delta, stub_delta = _apply_fk_results(
                fk_results=fk_results,
                inst_uri=inst_uri,
                class_name=class_name,
                col_normalized=col_normalized,
                writer=writer,
                master_instance_uris=master_instance_uris,
                unverified_fk_targets=ctx.unverified_fk_targets,
                fk_added_types=ctx.fk_added_types,
                fk_added_types_seen=ctx.fk_added_types_seen,
                prov_rels=prov_rels,
                col_name=col_name,
                value=value,
                known_instance_uris=ctx.known_instance_uris or None,
                fk_attempt_detail=ctx.fk_attempt_detail,
                fk_failure_detail=ctx.fk_failure_detail,
            )
            if op_added:
                ctx.object_property_count += op_added
                ctx.per_class_op_triples[class_name] = (
                    ctx.per_class_op_triples.get(class_name, 0) + op_added
                )
            ctx.promoted_fk_count += promoted_delta
            ctx.skipped_stub_fk_count += stub_delta

        if col_lower in fk_processed_cols:
            continue
        prop_name = _col_to_prop(
            col_name, class_name, tbox_info, class_prop_mapping,
            dict_contract=dict_contract,
        )
        if prop_name is None:
            continue
        range_key = prop_name.lower() if prop_name else ""
        tbox_range = tbox_info.get("dp_ranges", {}).get(range_key, "")
        typed_literal = _format_value(
            value, prop_name, tbox_range=tbox_range,
            sentinel_is_value=bool(
                sentinel_value_cols and col_lower in sentinel_value_cols),
        )
        if typed_literal is None and value:
            if _is_null_sentinel(value):
                sentinel_filtered_count += 1
            else:
                coercion_fail_count += 1
                ckey = f"{class_name}/{col_name}"
                ctx.coercion_failures_by_column[ckey] = (
                    ctx.coercion_failures_by_column.get(ckey, 0) + 1
                )
        if typed_literal is not None:
            writer.add((inst_uri, DOMAIN_NS_OBJ[prop_name], typed_literal))
            prov_props[prop_name] = {
                "source_column": col_name,
                "raw_value": value,
                "coercion": (
                    str(typed_literal.datatype)
                    if typed_literal.datatype else "xsd:string"
                ),
                "coercion_loss": str(typed_literal) != value,
            }
    return prov_props, prov_rels, coercion_fail_count, sentinel_filtered_count


def _detect_provenance_pk_col(class_name: str, row: dict) -> str:
    """provenance 기록용 PK 컬럼 추정 — class suffix 우선, 폴백 id/code."""
    row_lower_keys = {k.lower(): k for k in row}
    class_lower = class_name.lower()
    for suffix in _PK_CLASS_SUFFIXES:
        if not class_lower.endswith(suffix):
            continue
        base = class_lower[: -len(suffix)]
        if not base:
            break
        candidate = f"{base}id"
        if candidate in row_lower_keys:
            return row_lower_keys[candidate]
        break
    for k_low, k_orig in row_lower_keys.items():
        if k_low.endswith("id") or k_low.endswith("code"):
            return k_orig
    return ""


def _emit_row_provenance(
    inst_uri: URIRef, row: dict, row_idx: int, filename: str,
    pk_value: str | None, class_name: str,
    prov_props: dict[str, dict], prov_rels: dict[str, dict],
    ctx, writer: _BulkNTWriter, provenance_ndjson_fh,
) -> None:
    """cell-level provenance + A4 row-level provenance triple (instance → row URI)."""
    prov_pk_col = _detect_provenance_pk_col(class_name, row)
    prov_record = _collect_instance_provenance(
        uri=str(inst_uri),
        table=filename,
        row_idx=row_idx,
        pk_col=prov_pk_col,
        pk_val=pk_value or f"{row_idx + 1:04d}",
        properties=prov_props,
        relationships=prov_rels,
    )
    ctx.provenance_data[str(inst_uri)] = prov_record
    if provenance_ndjson_fh is not None:
        provenance_ndjson_fh.write(
            json.dumps(
                {"uri": str(inst_uri), **prov_record},
                ensure_ascii=False,
            ) + "\n",
        )

    # A4: row-level provenance — instance → prov://<table>#row=<N>
    csv_table_for_prov = filename[:-4] if filename.endswith(".csv") else filename
    row_uri = _build_row_uri_for_abox(csv_table_for_prov, row_idx + 1)
    writer.add((inst_uri, PROV.wasDerivedFrom, row_uri))


def _process_single_csv(
    *,
    csv_path: str,
    filename: str,
    ctx,
    tbox_info: dict,
    class_prop_mapping: dict,
    obj_props: list,
    available_classes: set[str],
    master_instance_uris: set[str],
    writer: _BulkNTWriter,
    prefetched: dict[str, str] | None,
    prefetch_limit_bytes: int,
    provenance_ndjson_fh,
    table_index: int,
    table_total: int,
    master_value_index: dict[str, dict[str, str]] | None = None,
    fuzzy_config: dict | None = None,
    dict_contract: dict[str, dict[str, str]] | None = None,
) -> None:
    """단일 CSV 처리 — 결과를 ctx + writer 에 누적.

    R20 에서 generate_abox 의 inline block 을 별도 함수로 추출. 테이블당 실패는
    내부 catch + ctx.failed_tables 기록 (다음 테이블 진행).

    P1-1b 에서 7 helper 로 sub-step 분할:
      _load_csv_rows → _classify_master_or_transaction → _record_column_coverage
      → _detect_pk_column → row loop (
          _emit_pk_identifier → _process_row_columns → _emit_row_provenance
      )
    """
    tbl_start = time.monotonic()
    try:
        class_name = _table_to_class(filename, tbox_info)

        # 1. CSV 로드
        use_streaming, rows, pk_sample_rows, csv_bytes = _load_csv_rows(
            csv_path, filename, prefetched, prefetch_limit_bytes,
            table_index, table_total, class_name,
        )
        # streaming: pk_sample_rows 가 비어있으면 데이터 없음.
        # non-streaming: rows 가 비어있으면 데이터 없음.
        if use_streaming and not pk_sample_rows:
            return
        if not use_streaming and not rows:
            return

        # 2. master/transaction 분류 + row 수 기록
        _classify_master_or_transaction(
            filename, class_name, ctx,
            use_streaming, rows, pk_sample_rows, csv_bytes,
        )
        ctx.csv_row_counts[class_name] = (
            len(pk_sample_rows) if use_streaming else len(rows)
        )

        # 3. column coverage
        _record_column_coverage(
            class_name, ctx, tbox_info, class_prop_mapping, dict_contract,
            use_streaming, rows, pk_sample_rows,
        )

        # 4. PK detection — 명시적 권위 PK(table_pk_columns) 우선, 없으면 휴리스틱.
        pk_detect_source = pk_sample_rows if use_streaming else rows
        pk_column = _resolve_explicit_pk(filename, pk_detect_source)
        pk_is_authoritative = pk_column is not None
        if pk_is_authoritative:
            logger.info(
                "PK 결정 %s → 명시적 권위 PK %s (DDL 기반)",
                class_name, pk_column,
            )
        else:
            # 선수집이 **전수 읽기로** 정한 PK 를 우선 채택한다. streaming 은 20,000행
            # 표본으로만 판정하므로, 표본에서는 unique 하지만 전체에서는 중복인 컬럼을
            # PK 로 고를 수 있다 (2026-08-09 실측: 25,000행 CSV 에서 전수는 zeta_id,
            # 20K 표본은 alpha_code). 그러면 발행 IRI 가 known_instance_uris 에
            # 하나도 없어 그 테이블로 향하는 **정방향 FK 가 전부 stub 으로 버려진다**
            # (skipped_stub_fk_count 만 늘고 success 는 true).
            shared_pk = (ctx.precollected_pk_columns or {}).get(filename)
            if shared_pk is not None:
                pk_column = shared_pk
                if use_streaming:
                    logger.info(
                        "PK 결정 %s → 선수집 전수 판정 재사용 %s "
                        "(streaming 표본 판정과의 불일치 방지)",
                        class_name, shared_pk,
                    )
            else:
                pk_column = _detect_pk_column(pk_detect_source, class_name)
        if isinstance(pk_column, list):
            logger.info(
                "PK 결정 %s → composite %s (%d sample rows)",
                class_name, pk_column, len(pk_detect_source),
            )
        elif pk_column:
            logger.info(
                "PK 결정 %s → %s (%d sample rows)",
                class_name, pk_column, len(pk_detect_source),
            )
        else:
            logger.info(
                "PK 결정 %s → NONE — 행 번호 기반 URI 사용 (비결정적)",
                class_name,
            )

        # 4b. Ambiguous-sentinel columns — decided once per table from the same
        #     sample PK detection uses, then applied to every row.
        sentinel_value_cols = _detect_sentinel_value_columns(pk_detect_source)
        if sentinel_value_cols:
            logger.info(
                "센티널 예외 %s → %s 컬럼은 '-' 를 실제 값으로 적재 (닫힌 값 집합)",
                class_name, sorted(sentinel_value_cols),
            )

        # 5. row loop — instance + DP/OP triple + provenance 누적
        coercion_fail_count = 0
        sentinel_filtered_count = 0
        streaming_row_count = 0

        def _iter_rows():
            if use_streaming:
                with open(csv_path, encoding="utf-8-sig") as sf:
                    for j, sr in enumerate(csv.DictReader(sf)):
                        yield j, sr
            else:
                for j, sr in enumerate(rows):
                    yield j, sr

        for i, row in _iter_rows():
            streaming_row_count = i + 1
            pk_value = _detect_pk_value(
                row, class_name, pk_column=pk_column,
                pk_is_authoritative=pk_is_authoritative,
            )
            if pk_value:
                inst_uri = DOMAIN_INST_NS_OBJ[f"{class_name}_{_uri_safe_local(pk_value)}"]
            else:
                if i == 0:
                    logger.warning(
                        "테이블 %s: PK 컬럼 미감지, 행 번호로 URI 생성 (비결정적)",
                        filename,
                    )
                inst_uri = DOMAIN_INST_NS_OBJ[f"{class_name}_{i + 1:04d}"]
            writer.add((inst_uri, RDF.type, DOMAIN_NS_OBJ[class_name]))
            uri_str = str(inst_uri)
            ctx.duplicate_pk_uris[uri_str] = ctx.duplicate_pk_uris.get(uri_str, 0) + 1

            _emit_pk_identifier(
                pk_value, pk_column, row, class_name, inst_uri, tbox_info, writer,
            )

            prov_props, prov_rels, row_coercion, row_sentinel = _process_row_columns(
                row, inst_uri, class_name, pk_column,
                ctx, writer,
                tbox_info, class_prop_mapping, obj_props,
                available_classes, master_instance_uris,
                master_value_index, fuzzy_config, dict_contract,
                sentinel_value_cols=sentinel_value_cols,
            )
            coercion_fail_count += row_coercion
            sentinel_filtered_count += row_sentinel

            _emit_row_provenance(
                inst_uri, row, i, filename, pk_value, class_name,
                prov_props, prov_rels, ctx, writer, provenance_ndjson_fh,
            )

            ctx.individual_count += 1
            ctx.per_class_instances[class_name] = (
                ctx.per_class_instances.get(class_name, 0) + 1
            )

        # 6. 종결 통계 + flush
        ctx.total_coercion_failures += coercion_fail_count
        ctx.total_sentinel_filtered += sentinel_filtered_count
        ctx.class_table_map[class_name] = filename
        ctx.tables_processed += 1

        actual_rows = streaming_row_count if use_streaming else len(rows)
        if use_streaming:
            ctx.csv_row_counts[class_name] = actual_rows
            writer.flush()
        logger.info(
            "A-Box 완료 [%d/%d] %s: %d행 (%.2fs)",
            table_index, table_total, filename, actual_rows,
            time.monotonic() - tbl_start,
        )
    except Exception as e:
        ctx.failed_tables.append({
            "table": filename, "error": str(e), "rows_processed": 0,
        })


def _apply_fk_results(
    *,
    fk_results: list[tuple],
    inst_uri: URIRef,
    class_name: str,
    col_normalized: str,
    writer: _BulkNTWriter,
    master_instance_uris: set[str],
    unverified_fk_targets: dict[str, int],
    fk_added_types: list[tuple],
    fk_added_types_seen: set[tuple],
    prov_rels: dict[str, dict],
    col_name: str,
    value: str,
    known_instance_uris: "set[str] | None" = None,
    fk_attempt_detail: "dict | None" = None,
    fk_failure_detail: "dict | None" = None,
) -> tuple[int, int, int]:
    """FK 검출 결과 3-case (`__unverified__` / `__inverse__` / 정방향) 를
    통일된 방식으로 그래프에 적용.

    R15~R17 까지 row loop 중첩이 깊어 가독성이 떨어져 있었던 elif 블록을
    헬퍼로 분리. 부수 효과는 호출자의 writer / dict / list 에 직접 쓴다 —
    A-Box 처리량 경로라 불필요한 복사 없이 반영.

    Returns:
        (op_triples_added, promoted_fk_delta, skipped_stub_delta)

    known_instance_uris: 제공되면 정방향 FK 의 target_uri 가 이 집합에 없을 때
        stub 으로 보고 OP·rdf:type 생성을 건너뛴다 (None 이면 기존 동작 유지).

    fk_attempt_detail / fk_failure_detail: 제공되면 FK 셀(=한 번의 호출) 단위로
        시도/실패를 각각 1회만 집계한다. 한 셀이 fwd+inverse(+unverified) 튜플을
        동시에 만들어도 referential_integrity 분모/분자가 부풀지 않도록 한다.
        key = (source_class, source_column, target_class).
    """
    op_added = 0
    promoted = 0
    skipped_stub = 0
    # 셀 단위 집계: 이 FK 셀이 실재 타겟으로 해소됐는지 1회만 판정.
    fk_target_class = _fk_skeleton_cache.get((class_name, col_normalized))
    fk_target_class = fk_target_class[0] if fk_target_class else col_normalized
    cell_failed = False
    for fk_item in fk_results:
        kind = fk_item[0]
        if kind == "__unverified__":
            uv_cls = fk_item[3]
            uv_target_uri = str(fk_item[2])
            prop = fk_item[1]
            # sentinel prop — 실제 프로퍼티 아님, 통계만 기록
            if prop == "<no_domain_range_match>":
                unverified_fk_targets[uv_cls] = (
                    unverified_fk_targets.get(uv_cls, 0) + 1
                )
                cell_failed = True
                continue
            if _should_promote_unverified_fk(uv_target_uri, master_instance_uris):
                # master_data 에 타겟 존재 → 승격
                writer.add((inst_uri, DOMAIN_NS_OBJ[prop], fk_item[2]))
                op_added += 1
                promoted += 1
                # target rdf:type 명시 주입 (disjoint 가드 대상)
                t_uri = fk_item[2]
                t_cls = DOMAIN_NS_OBJ[uv_cls]
                key = (str(t_uri), str(t_cls))
                if key not in fk_added_types_seen:
                    writer.add((t_uri, RDF.type, t_cls))
                    fk_added_types.append((t_uri, t_cls))
                    fk_added_types_seen.add(key)
            else:
                unverified_fk_targets[uv_cls] = (
                    unverified_fk_targets.get(uv_cls, 0) + 1
                )
                cell_failed = True
        elif kind == "__inverse__":
            # 역방향 트리플은 S8 ensure_inverse_triples 에 위임.
            # target rdf:type 만 여기서 주입.
            _, _fwd, _inv_prop, target_uri = fk_item
            skel = _fk_skeleton_cache.get((class_name, col_normalized))
            # stub 게이트: 정방향과 동일하게 실재하지 않는 타겟 타이핑 생략.
            if (
                known_instance_uris is not None
                and str(target_uri) not in known_instance_uris
            ):
                skipped_stub += 1
                t_cls_name = skel[0] if skel is not None else col_normalized
                unverified_fk_targets[t_cls_name] = (
                    unverified_fk_targets.get(t_cls_name, 0) + 1
                )
                cell_failed = True
                continue
            if skel is not None:
                t_cls_name = skel[0]
                t_cls = DOMAIN_NS_OBJ[t_cls_name]
                key = (str(target_uri), str(t_cls))
                if key not in fk_added_types_seen:
                    writer.add((target_uri, RDF.type, t_cls))
                    fk_added_types.append((target_uri, t_cls))
                    fk_added_types_seen.add(key)
        else:
            # 정방향: source → prop → target
            prop_name, target_uri = fk_item
            skel = _fk_skeleton_cache.get((class_name, col_normalized))
            # stub 게이트: 타겟이 실재 인스턴스가 아니면 OP·rdf:type 생성 생략.
            # (마스터 없는 도메인에서 FK 미매칭 값이 가짜 타겟 인스턴스를 만들던 버그)
            if (
                known_instance_uris is not None
                and str(target_uri) not in known_instance_uris
            ):
                skipped_stub += 1
                t_cls_name = skel[0] if skel is not None else col_normalized
                unverified_fk_targets[t_cls_name] = (
                    unverified_fk_targets.get(t_cls_name, 0) + 1
                )
                cell_failed = True
                continue
            writer.add((inst_uri, DOMAIN_NS_OBJ[prop_name], target_uri))
            op_added += 1
            if skel is not None:
                t_cls_name = skel[0]
                t_cls = DOMAIN_NS_OBJ[t_cls_name]
                key = (str(target_uri), str(t_cls))
                if key not in fk_added_types_seen:
                    writer.add((target_uri, RDF.type, t_cls))
                    fk_added_types.append((target_uri, t_cls))
                    fk_added_types_seen.add(key)
            prov_rels[prop_name] = {
                "source_column": col_name,
                "fk_value": value,
                "target_uri": str(target_uri),
                "target_exists": None,  # finalize 시 보정
                "resolution_method": "exact_fk_pattern",
            }
    # 셀 단위 집계 — fk_results 가 비어있지 않으면(=FK 로 인식된 셀) 1회 시도로 본다.
    # fwd/inverse/unverified 튜플 수와 무관하게 시도/실패를 정확히 1회씩 기록.
    if fk_results and fk_attempt_detail is not None:
        key = (class_name, col_name, fk_target_class)
        fk_attempt_detail[key] = fk_attempt_detail.get(key, 0) + 1
        if cell_failed and fk_failure_detail is not None:
            fk_failure_detail[key] = fk_failure_detail.get(key, 0) + 1
    return op_added, promoted, skipped_stub


def _validate_instances(g: Graph) -> dict:
    """Sanity check on generated A-Box instances.

    Returns:
        {
            "untyped_instances": int,  # DOMAIN_INST_NS subjects without rdf:type
            "reserved_char_uris": [str, ...],  # sample (max 10)
            "reserved_char_uri_count": int,    # full count
            "duplicate_type_assertions": int,
        }
    """
    steel_inst_str = str(DOMAIN_INST_NS)
    untyped = 0
    reserved_sample: list[str] = []
    reserved_total = 0
    reserved_pattern = re.compile(r"[\s<>\"{}|\\^`]")
    seen_type_triples: set[tuple] = set()
    duplicate_type = 0
    for s in set(g.subjects()):
        s_str = str(s)
        if not s_str.startswith(steel_inst_str):
            continue
        types = list(g.objects(s, RDF.type))
        if not types:
            untyped += 1
        if reserved_pattern.search(s_str[len(steel_inst_str):]):
            reserved_total += 1
            if len(reserved_sample) < 10:
                reserved_sample.append(s_str)
        for t in types:
            key = (s_str, str(t))
            if key in seen_type_triples:
                duplicate_type += 1
            else:
                seen_type_triples.add(key)
    return {
        "untyped_instances": untyped,
        "reserved_char_uris": reserved_sample,
        "reserved_char_uri_count": reserved_total,
        "duplicate_type_assertions": duplicate_type,
    }


def _suggest_dp_name(col_name: str) -> str:
    """Suggest a canonical DP name for an unmapped column.

    First checks common_dp alias mapping, else derives a `has{Pascal}` name
    from the CSV column.
    """
    common = _resolve_common_dp_name(col_name)
    if common is not None:
        return common
    parts = [p for p in re.split(r"[\W_]+", col_name) if p]
    if not parts:
        return "hasValue"
    pascal = "".join(p.capitalize() for p in parts)
    if pascal[0].isdigit():
        pascal = "V" + pascal
    return f"has{pascal}"


def _csv_headers_by_class() -> dict[str, list[str]]:
    """``{ClassName: [CSV 헤더...]}`` — 값 커버리지 판정 대상 컬럼 목록.

    ``column_coverage`` 는 mapped/unmapped **개수** 만 담고 컬럼명을 남기지 않으므로
    (unmapped 만 이름이 있다), 원본 CSV 헤더에서 직접 읽는다.
    """
    import csv as _csv

    out: dict[str, list[str]] = {}
    try:
        from domain.table_mapping import load_table_class_mapping
        table_class = load_table_class_mapping()
    except Exception:  # noqa: BLE001
        return out
    for table, cls_name in (table_class or {}).items():
        path = os.path.join(SOURCE_RAWDATA_DIR, f"{table}.csv")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig", newline="") as handle:
                header = next(_csv.reader(handle), []) or []
        except Exception:  # noqa: BLE001 — 읽을 수 없는 선택적 CSV 헤더는 건너뛴다
            continue
        cols = [c.strip() for c in header if c and c.strip()]
        if cols:
            out.setdefault(cls_name, []).extend(cols)
    return out


def _compute_value_coverage(
    g: Graph,
    per_class_instances: dict[str, int],
    tbox_info: dict,
    column_coverage: dict,
) -> dict:
    """컬럼별 **데이터가 실제로 실렸는가** — 이름 해석이 아니라 값으로 판정.

    ## 왜 필요한가 (2026-08-30 실측)

    ``column_coverage`` 는 컬럼 → DP **이름 매칭** 성공률이다. 매칭 후 값이 한 건도
    안 실려도 1.0 이 나온다. 실측: DP 7개가 ``pct: 0.0`` 인데
    ``column_coverage: 1.0`` 이었다.

    그 7건은 **손실이 아니었다** — FK 컬럼이라 리터럴 DP 대신 관계 OP 로 실렸다
    (Path B 정상 동작). 문제는 이 지표가 **"OP 로 갔다" 와 "조용히 버려졌다" 를
    구분하지 못한다**는 것이다. 둘 다 1.0 이므로 진짜 손실이 이 지표를 통과한다
    (``itemSupplierMapPriority`` 98행 전량 소실이 그 경로였다).

    각 (class, column) 을 세 갈래로 분류한다:

    ``literal``
        그 컬럼의 DP 에 값이 실렸다 (정상).
    ``relation``
        리터럴은 0 이지만 같은 컬럼을 ``dcterms:source`` 로 갖는 **OP** 가 이 클래스
        인스턴스에서 간선을 만들었다 — FK 가 관계로 표현된 것이다 (정상).
    ``missing``
        어느 쪽에도 없다 — **진짜 손실**이다. 이것만 ``value_coverage`` 분모에서
        깎는다.

    ``relation`` 을 정상으로 세는 근거: FK 컬럼의 정보는 IRI 링크로 온전히 보존되고
    (``hasChemicalAnalysisProduct`` 150건 등), 리터럴 중복 적재는 Path B 가
    의도적으로 피하는 것이다. 다만 **어느 쪽으로 갔는지** 를 산출물에 남겨야
    "왜 이 DP 는 0% 인가" 를 사람이 매번 다시 조사하지 않는다.
    """
    from rdflib.namespace import DCTERMS as _DCTERMS

    dp_domains: dict[str, set[str]] = tbox_info.get("dp_domains", {})
    dp_sources: dict[str, set[str]] = {}
    op_sources: dict[str, set[str]] = {}
    try:
        tbox_graph = _new_graph()
        tbox_graph.parse(TBOX_PATH, format="turtle")
        for prop in tbox_graph.subjects(RDF.type, OWL.DatatypeProperty):
            local = str(prop).replace(str(DOMAIN_NS), "")
            cols = {
                str(o).strip().upper() for o in tbox_graph.objects(prop, _DCTERMS.source)
                if str(o).strip()
            }
            if cols:
                dp_sources[local] = cols
        for prop in tbox_graph.subjects(RDF.type, OWL.ObjectProperty):
            local = str(prop).replace(str(DOMAIN_NS), "")
            cols = {
                str(o).strip().upper() for o in tbox_graph.objects(prop, _DCTERMS.source)
                if str(o).strip()
            }
            if cols:
                op_sources[local] = cols
    except Exception as exc:  # noqa: BLE001 — 부가 지표가 생성을 막지 않는다
        logger.debug("value_coverage: T-Box 출처 색인 실패: %s", exc)
        return {}

    # class → declared DP local names
    class_to_dps: dict[str, list[str]] = {}
    for dp_lower, dp_name in tbox_info.get("datatype_properties", {}).items():
        for domain_cls in dp_domains.get(dp_lower, set()):
            class_to_dps.setdefault(domain_cls, []).append(dp_name)

    per_class: dict[str, dict] = {}
    totals = {"literal": 0, "relation": 0, "missing": 0}
    missing_items: list[dict] = []
    relation_items: list[dict] = []

    csv_headers = _csv_headers_by_class()
    for cls_name, _cov in sorted(column_coverage.items()):
        if per_class_instances.get(cls_name, 0) == 0:
            continue
        instances = list(g.subjects(RDF.type, DOMAIN_NS_OBJ[cls_name]))
        if not instances:
            continue
        sample = instances[: min(len(instances), 200)]
        # CSV 헤더가 판정 대상이다 — ``column_coverage`` 는 개수만 담고 컬럼명을
        # 남기지 않으므로(mapped/unmapped 카운트), 원본 헤더에서 직접 읽는다.
        columns = csv_headers.get(cls_name) or []
        if not columns:
            continue
        buckets = {"literal": [], "relation": [], "missing": []}
        for column in columns:
            col_up = str(column).strip().upper()
            if not col_up:
                continue
            # 1) 이 클래스의 DP 중 그 컬럼을 출처로 갖는 것에 값이 있는가.
            has_literal = False
            for dp_name in class_to_dps.get(cls_name, []):
                if col_up not in dp_sources.get(dp_name, set()):
                    continue
                dp_uri = DOMAIN_NS_OBJ[dp_name]
                if any((inst, dp_uri, None) in g for inst in sample):
                    has_literal = True
                    break
            if has_literal:
                buckets["literal"].append(column)
                continue
            # 2) 같은 컬럼을 출처로 갖는 OP 가 이 클래스에서 간선을 만들었는가.
            via_relation = None
            for op_name, cols in op_sources.items():
                if col_up not in cols:
                    continue
                op_uri = DOMAIN_NS_OBJ[op_name]
                if any((inst, op_uri, None) in g for inst in sample):
                    via_relation = op_name
                    break
            if via_relation:
                buckets["relation"].append(column)
                relation_items.append({
                    "class": cls_name, "column": column, "via_op": via_relation,
                })
                continue
            buckets["missing"].append(column)
            missing_items.append({"class": cls_name, "column": column})

        for key in totals:
            totals[key] += len(buckets[key])
        per_class[cls_name] = {
            k: v for k, v in buckets.items() if v
        }

    checked = sum(totals.values())
    return {
        "columns_checked": checked,
        "loaded_as_literal": totals["literal"],
        "loaded_as_relation": totals["relation"],
        "not_loaded": totals["missing"],
        # 이름 해석이 아니라 **값이 실렸는가** 로 계산한다. relation 은 정상 적재다.
        "value_coverage": (
            round((totals["literal"] + totals["relation"]) / checked, 4)
            if checked else 1.0
        ),
        "not_loaded_items": missing_items[:50],
        # FK → 관계 라우팅 목록. "왜 이 DP 는 0% 인가" 의 답이 여기 있다.
        "relation_routed_items": relation_items[:50],
        "per_class": per_class,
    }


def _compute_property_completeness_detail(
    g: Graph,
    per_class_instances: dict[str, int],
    tbox_info: dict,
) -> dict:
    """클래스별 property_completeness 상세 분포.

    T-Box 에 선언된 각 클래스의 DP 들을 모두 열거하고, 해당 클래스 인스턴스
    중 몇 % 가 그 DP 에 값을 가지는지 계산. R21-1: measure_instance_quality
    의 집계값 (68.2%) 이 어디서 내려가는지 가시화.

    Returns dict:
      { class_name: { "mean_coverage_pct": float, "low_coverage_dps":
        [(dp_name, pct), ...] } }
    """
    dp_domains: dict[str, set[str]] = tbox_info.get("dp_domains", {})
    # class → declared DPs (canonical camelCase)
    class_to_dps: dict[str, list[str]] = {}
    for dp_lower, dp_name in tbox_info.get("datatype_properties", {}).items():
        domains = dp_domains.get(dp_lower, set())
        if not domains:
            continue
        for d in domains:
            class_to_dps.setdefault(d, []).append(dp_name)

    detail: dict[str, dict] = {}
    for cls_name, total in per_class_instances.items():
        if total == 0 or cls_name not in class_to_dps:
            continue
        cls_uri = DOMAIN_NS_OBJ[cls_name]
        instances = list(g.subjects(RDF.type, cls_uri))
        if not instances:
            continue
        dp_coverage: list[tuple[str, float]] = []
        for dp_name in class_to_dps[cls_name]:
            dp_uri = DOMAIN_NS_OBJ[dp_name]
            with_val = sum(1 for inst in instances if (inst, dp_uri, None) in g)
            pct = round(with_val / len(instances) * 100, 1)
            dp_coverage.append((dp_name, pct))
        if not dp_coverage:
            continue
        mean = round(sum(p for _, p in dp_coverage) / len(dp_coverage), 1)
        low = [(n, p) for n, p in dp_coverage if p < 50.0]
        low.sort(key=lambda x: x[1])
        detail[cls_name] = {
            "mean_coverage_pct": mean,
            "dp_count": len(dp_coverage),
            "low_coverage_dps": [{"dp": n, "pct": p} for n, p in low[:10]],
        }
    return detail


def _re_sub_camel_to_upper_snake(name: str) -> str:
    """``sourceType`` → ``SOURCE_TYPE`` — ``dcterms:source`` 색인 조회용 키.

    ``common_dp.json`` 의 alias 는 CSV 컬럼을 camelCase (``sourceType``) 또는
    snake_case (``source_type``) 로 적는데, T-Box 의 ``dcterms:source`` 는 CSV
    헤더 그대로 (``Source_Type``) 이고 색인 키는 UPPER 다. 표기를 맞추지 않으면
    역인덱스 조회가 전부 빗나간다.
    """
    if not name:
        return ""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return spaced.replace("-", "_").upper()


def _check_required_props_coverage(
    g: Graph,
    per_class_instances: dict[str, int],
    tbox_info: dict | None = None,
) -> dict:
    """rules/contracts/common_dp.json::required_props_by_class 기준 클래스별 필수 DP
    커버리지 감사. alias-aware — canonical DP 이름(예: hasStatus) 이 T-Box
    에 없더라도 동일 의미의 alias(equipmentStatus 등) 가 T-Box 에 존재하고
    인스턴스가 그 DP 를 가지면 만족된 것으로 간주.

    Args:
        tbox_info: `_parse_tbox` 결과. alias 해상도를 위해 사용.

    Returns:
        {
            "classes_checked": int,
            "violations": [
                {"class", "dp", "checked_dps", "instances_with_dp",
                 "instances_total", "coverage_pct"}
            ],
            "coverage_by_class": {cls: {dp: pct, ...}},
        }
    """
    if not _REQUIRED_PROPS_BY_CLASS:
        return {"classes_checked": 0, "violations": [], "coverage_by_class": {}}
    # Build alias-resolution map: canonical name → list of T-Box DP local names
    # that satisfy it (derived from common_dp.json aliases + canonical itself).
    tbox_dps = tbox_info.get("datatype_properties", {}) if tbox_info else {}
    # 요구 항목이 OP 축으로 모델링된 경우를 함께 본다 (_candidate_ops 참조).
    tbox_ops = tbox_info.get("object_properties", {}) if tbox_info else {}
    # OP 의 domain 판정에 T-Box 그래프가 필요하다. A-Box 그래프(``g``)에는 스키마
    # 트리플이 없으므로 별도로 읽는다 — 실패 시 OP 축은 조용히 비활성(기존 동작).
    g_tbox: Graph | None = None
    if tbox_ops:
        try:
            g_tbox = Graph()
            g_tbox.parse(TBOX_PATH, format="turtle")
        except Exception as exc:  # noqa: BLE001
            logger.debug("required_props: T-Box 로드 실패 (OP 축 skip): %s", exc)
            g_tbox = None

    def _candidate_dps(canonical: str, for_class: str | None = None) -> list[str]:
        """canonical DP 가 T-Box 에 없더라도 alias 로 존재하면 그걸 후보에 포함.

        검사 순서:
        1. canonical 이름 자체 (예: hasTimestamp)
        2. rules/contracts/common_dp.json 의 aliases (timestamp, datetime 등) camelize
        3. class-prefix 패턴 (for_class 제공 시):
           class_name 의 앞 토큰 + canonical 에서 'has' 제거한 나머지.
           예: class=NoiseVibrationMonitoring, canonical=hasTimestamp →
           'noiseTimestamp', 'noiseVibrationTimestamp' 후보. T-Box 에
           실제 존재하면 채택. R19 까지는 class-prefix alias 를 audit 가
           못 잡아서 false-positive violations 발생했었음.
        """
        candidates = [canonical]
        decl = _COMMON_DP_BY_NAME.get(canonical)
        if decl is not None:
            for alias in decl.get("aliases", []):
                parts = str(alias).lower().split("_")
                camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
                actual = tbox_dps.get(camel.lower())
                if actual and actual not in candidates:
                    candidates.append(actual)
        # class-prefix 패턴 — canonical 의 'has' stripped suffix 를 class 토큰
        # prefix 에 붙여 lookup.
        #
        # ## 2026-08-30: 1·2 토큰 하드코딩이 위반 14건을 전부 오탐으로 만들었다
        #
        # 예전 구현은 ``tokens[0]`` 과 ``tokens[0]+tokens[1]`` **두 개만** 시도했다.
        # 토큰이 3개 이상인 클래스는 실제 DP 이름에 도달할 수 없다::
        #
        #   AirEmissionMonitoring/hasTimestamp
        #     시도  airTimestamp, airEmissionTimestamp
        #     실제  airEmissionMonitoringTimestamp   (1,560건 존재)
        #   ProcessBlastFurnace/hasTimestamp
        #     시도  processTimestamp, processBlastTimestamp
        #     실제  blastFurnaceTimestamp            (4,320건 존재)
        #   NDTResults/hasResult
        #     시도  nResult, nDResult  ← 약어가 N/D/T 로 쪼개진다
        #     실제  ndtResultsResult                 (100건 존재)
        #
        # 결과: Path B class-specific DP 커버리지를 재는 **유일한 게이트**가 위반
        # 14/14 를 오탐으로 보고했고, 진짜 위반이 생겨도 그 노이즈와 구분되지
        # 않았다. ``checked_dps`` 에 후보가 1개만 실린 것이 해석 실패의 지문이다.
        if for_class and canonical.startswith("has") and len(canonical) > 3:
            suffix = canonical[3:]  # "Timestamp"
            import re as _re

            # 약어를 한 토큰으로 유지한다 (``NDTResults`` → ['NDT','Results']).
            # ``[A-Z][^A-Z]*`` 는 연속 대문자를 글자마다 쪼개 'nResult' 를 만든다.
            tokens = _re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[0-9]+", for_class)
            # 누적 prefix 전개 — 1..n 토큰을 모두 시도한다. 3토큰 이상 클래스명이
            # 대부분이므로 상한을 두면 같은 결함이 다시 생긴다.
            for take in range(1, len(tokens) + 1):
                prefix = "".join(tokens[:take])
                prefix_camel = prefix[:1].lower() + prefix[1:]
                actual = tbox_dps.get((prefix_camel + suffix).lower())
                if actual and actual not in candidates:
                    candidates.append(actual)
            # 접미 토큰 조합도 본다 — S2 는 클래스명 앞부분을 버리고 뒷부분만
            # prefix 로 쓰는 경우가 있다 (``ProcessBlastFurnace`` →
            # ``blastFurnaceTimestamp``). 위 누적 전개로는 도달 불가하다.
            for drop in range(1, len(tokens)):
                prefix = "".join(tokens[drop:])
                prefix_camel = prefix[:1].lower() + prefix[1:]
                actual = tbox_dps.get((prefix_camel + suffix).lower())
                if actual and actual not in candidates:
                    candidates.append(actual)

        # ``dcterms:source`` 역인덱스 — **이름 추측보다 강한 근거**.
        #
        # T-Box 는 DP 마다 유래 CSV 컬럼을 ``dcterms:source`` 로 기록한다
        # (04-property-rules.md 의 "DatatypeProperty source column" 절). canonical 의
        # alias 는 곧 CSV 컬럼명이므로 (``hasTimestamp`` → ``timestamp``/``datetime``),
        # (클래스, 컬럼) 으로 조회하면
        # 이름 규칙과 무관하게 정확한 DP 를 얻는다. 위 전개가 못 맞춘 형태도 여기서
        # 해결된다.
        if for_class:
            by_source = tbox_info.get("dp_by_source") if tbox_info else None
            if by_source:
                alias_cols = {canonical[3:] if canonical.startswith("has") else canonical}
                if decl is not None:
                    alias_cols.update(str(a) for a in decl.get("aliases", []))
                for alias in alias_cols:
                    # CSV 컬럼 표기는 ``Source_Type`` / ``sourceType`` 등으로 갈리므로
                    # 두 형태를 모두 조회한다 (색인 키는 UPPER).
                    variants = {
                        alias.upper(),
                        _re_sub_camel_to_upper_snake(alias),
                    }
                    for col in variants:
                        actual = by_source.get((for_class, col))
                        if actual and actual not in candidates:
                            candidates.append(actual)
        return candidates

    def _candidate_ops(canonical: str, for_class: str) -> list[str]:
        """canonical 요구사항을 만족하는 **ObjectProperty** 후보.

        요구 항목이 DP 축이 아니라 OP 축으로 모델링되는 경우가 있다. FK 컬럼은
        Path B 에서 리터럴 DP 가 아니라 관계 OP 가 되므로, DP 만 찾으면 정상
        모델링을 위반으로 보고한다.

        2026-08-30 실측: ``Transportation`` 의 ``hasOrigin`` / ``hasDestination`` 이
        그 경우다. CSV 의 ``Origin_Warehouse`` / ``Destination_Warehouse`` 는
        ``transportationHasOriginWarehouse`` / ``hasDestinationWarehouse`` OP 로
        (각 300건) 정확히 모델링됐는데 DP 축만 보는 감사가 0% 로 보고했다.

        ``dcterms:source`` 를 근거로 쓰지 **않는다** — OP 의 출처 표기가 오염돼
        있다 (실측: ``transportationHasOriginWarehouse`` 가
        ``Destination_Warehouse`` 를 주장). 대신 이름 토큰으로 찾는다: canonical 의
        의미 토큰(Origin/Destination)이 OP 이름에 있고 그 OP 의 domain 이 이
        클래스면 후보다.
        """
        if not tbox_ops:
            return []
        token = (canonical[3:] if canonical.startswith("has") else canonical).lower()
        if not token:
            return []
        out: list[str] = []
        for op_lower, op_name in tbox_ops.items():
            if token not in op_lower:
                continue
            op_domains = {
                str(d)[len(str(DOMAIN_NS)):]
                for d in g_tbox.objects(DOMAIN_NS_OBJ[op_name], RDFS.domain)
                if str(d).startswith(str(DOMAIN_NS))
            } if g_tbox is not None else set()
            # domain 정보가 없으면 이름만으로 채택하지 않는다 — 다른 클래스의
            # 동명 관계를 잘못 인정하면 진짜 누락을 덮는다.
            if op_domains and for_class not in op_domains:
                continue
            if not op_domains:
                continue
            out.append(op_name)
        return out

    def _eval_condition(inst, cond: dict | None, for_class: str | None = None) -> bool:
        """Evaluate a simple conditional predicate for an instance.

        cond schema: {"dp": <name>, "op": "==" | "!=", "value": <str>}
        When ``cond`` is None → always applicable.
        When the referenced DP doesn't exist on the instance → condition False
        (so the required DP check is skipped for that instance).
        """
        if cond is None:
            return True
        dep_cands = _candidate_dps(cond.get("dp", ""), for_class=for_class)
        dep_uris = [DOMAIN_NS_OBJ[c] for c in dep_cands]
        op = cond.get("op", "==")
        target = cond.get("value", "")
        for u in dep_uris:
            for obj in g.objects(inst, u):
                val = str(obj)
                if op == "==" and val == target:
                    return True
                if op == "!=" and val != target:
                    return True
        return False

    violations: list[dict] = []
    coverage_by_class: dict[str, dict] = {}
    checked = 0
    # 실제 CSV 에서 처리된 클래스만 대상 — wildcard 패턴은 concrete 클래스
    # 에 대해 해결(resolve) 된다.
    for cls_name in sorted(per_class_instances.keys()):
        required_specs = _resolve_required_props_for_class(cls_name)
        if not required_specs:
            continue
        checked += 1
        cls_uri = _ns_cls(cls_name)
        instances = list(g.subjects(RDF.type, cls_uri))
        total = len(instances)
        if total == 0:
            continue
        per_dp: dict[str, float] = {}
        for spec in required_specs:
            dp_name = spec["dp"]
            cond = spec.get("if")
            cands = _candidate_dps(dp_name, for_class=cls_name)
            # OP 축 후보를 함께 센다 — FK 는 Path B 에서 리터럴 DP 가 아니라
            # 관계 OP 로 모델링되므로, DP 만 보면 정상 모델링을 위반으로 읽는다.
            op_cands = _candidate_ops(dp_name, cls_name)
            cand_uris = [DOMAIN_NS_OBJ[c] for c in (*cands, *op_cands)]
            # 조건부 필수: 조건이 걸린 인스턴스만 검사 대상
            if cond is None:
                applicable = instances
            else:
                applicable = [i for i in instances if _eval_condition(i, cond, for_class=cls_name)]
            applicable_total = len(applicable)
            if applicable_total == 0:
                # 조건에 해당하는 인스턴스 없음 → 스킵, 통과로 간주
                per_dp[dp_name] = 100.0
                continue
            with_dp = sum(
                1 for inst in applicable
                if any((inst, u, None) in g for u in cand_uris)
            )
            pct = round(with_dp / applicable_total * 100, 1)
            per_dp[dp_name] = pct
            if with_dp < applicable_total:
                violations.append({
                    "class": cls_name,
                    "dp": dp_name,
                    "checked_dps": cands,
                    # OP 축 후보를 따로 남긴다 — 후보가 **1개뿐** 이면 이름 해석이
                    # 실패한 것이고, 그것이 오탐의 지문이다 (2026-08-30: 위반 14건
                    # 전부 checked_dps 길이 1 이었다).
                    "checked_ops": op_cands,
                    "condition": cond,
                    "instances_with_dp": with_dp,
                    "instances_applicable": applicable_total,
                    "instances_total": total,
                    "coverage_pct": pct,
                })
        coverage_by_class[cls_name] = per_dp
    return {
        "classes_checked": checked,
        "violations": violations,
        "coverage_by_class": coverage_by_class,
    }


def _build_loss_manifest(
    warnings: dict,
    column_coverage: dict,
    total_rows: int,
    total_cols: int,
    skipped_rows: int = 0,
    coercion_failures: int = 0,
    sentinel_filtered: int = 0,
    required_props_audit: dict | None = None,
    common_dp_injection: dict | None = None,
    coercion_failures_by_column: dict | None = None,
    fk_failures_detail: list | None = None,
    fk_attempts_total: int | None = None,
) -> dict:
    """A-Box 생성 손실 명세를 구성한다.

    ``coercion_failures`` 는 실제 형식 오류만 집계하고, CSV NULL 센티널
    (예: "미정", "N/A") 은 ``sentinel_filtered`` 에 별도 기록한다. 이전에는
    두 경로가 모두 coercion_failures 로 합산되어 40건이 오류인지 의도된
    스킵인지 분간되지 않았다.

    ``fk_failures_detail`` / ``fk_attempts_total`` 이 제공되면 FK 정합성은
    셀 단위 집계로 계산된다 (fwd/inverse 이중 계상 없음):
      - items = [{source_class, column, target, count}, ...] (디버깅 가능한 출처)
      - referential_integrity = 1 - (실패 셀 / 시도 셀) → [0,1] 보장.
    미제공 시(레거시 호출) ``warnings['unverified_fk']`` + total_rows 분모로 폴백.

    ## ``column_coverage`` 는 **이름 해석만** 센다 (2026-08-30)

    이 값이 1.0 이어도 데이터가 실렸다는 뜻이 아니다. 컬럼 → DP **이름 매칭**이
    성공했는지만 보므로, 매칭 후 값이 한 건도 안 실려도 만점이다. 실측: DP 7개가
    ``property_completeness_detail`` 에서 ``pct: 0.0`` 인데
    ``column_coverage: 1.0`` 이었다::

        airEmissionPointId (Point_ID)            0.0%
        alarmEventsTagId (Tag_ID)                0.0%
        chemicalAnalysisProductId (Product_ID)   0.0%
        continuousCastingEquipmentId (…)         0.0%   …외 3건

    이 7건은 **손실이 아니다** — FK 컬럼이라 리터럴 DP 대신 관계 OP 로 실렸다
    (``hasChemicalAnalysisProduct`` 150건 등 전수 확인). 즉 Path B 의 정상 동작이다.

    문제는 ``column_coverage`` 가 **"OP 로 갔다" 와 "조용히 버려졌다" 를 구분하지
    못한다**는 것이다. 둘 다 1.0 이므로 진짜 손실이 생겨도 이 지표로는 안 보인다
    (그것이 ``itemSupplierMapPriority`` 98행 전량 소실이 이 지표를 통과한 경로다).

    그래서 ``value_coverage`` 축을 신설한다 — 각 (class, column) 이 **리터럴로도,
    관계로도 실리지 않았는가** 를 센다. 이름 해석 성공은 분자에 넣지 않는다.
    """
    # FK referential failures — 셀 단위 detail 우선, 없으면 레거시 클래스 누적.
    if fk_failures_detail is not None:
        fk_items = list(fk_failures_detail)
        fk_total = sum(it.get("count", 0) for it in fk_items)
    else:
        fk_failures = warnings.get("unverified_fk", {}) or {}
        fk_total = sum(
            v.get("count", 0) if isinstance(v, dict) else 0
            for v in fk_failures.values()
        )
        fk_items = [
            {"column": col, "target": v.get("target", ""), "count": v.get("count", 0)}
            for col, v in fk_failures.items()
            if isinstance(v, dict)
        ]

    # Unmapped columns — include suggested DP name for agent-assisted remediation
    unmapped_items: list[dict] = []
    unmapped_total = 0
    for cls, cov in column_coverage.items():
        for col in cov.get("unmapped_columns", []):
            unmapped_items.append({
                "class": cls,
                "column": col,
                "suggested_dp_name": _suggest_dp_name(col),
            })
            unmapped_total += 1

    # Fidelity scores
    mapped_total = sum(c.get("mapped", 0) for c in column_coverage.values())
    total_mapped_unmapped = mapped_total + unmapped_total
    col_cov = mapped_total / total_mapped_unmapped if total_mapped_unmapped else 1.0
    row_cov = (total_rows - skipped_rows) / total_rows if total_rows else 1.0
    # referential_integrity: 셀 단위 시도 분모(권장)면 [0,1] 보장. 레거시는 행 수 분모.
    if fk_attempts_total is not None:
        ref_int = 1.0 - (fk_total / fk_attempts_total) if fk_attempts_total else 1.0
    else:
        ref_int = 1.0 - (fk_total / max(total_rows, 1))

    fk_loss: dict = {"count": fk_total, "items": fk_items}
    if fk_attempts_total is not None:
        # 셀 단위 분모/분자 노출 — referential_integrity 의 출처를 투명하게.
        fk_loss["attempts"] = fk_attempts_total
    result: dict = {
        "losses": {
            "unmapped_columns": {"count": unmapped_total, "items": unmapped_items},
            "fk_referential_failures": fk_loss,
            "type_coercion_failures": {"count": coercion_failures},
            "sentinel_filtered": {"count": sentinel_filtered},
            "duplicate_pk_rows": {"count": warnings.get("duplicate_pk_count", 0)},
            "skipped_rows": {"count": skipped_rows},
        },
        "fidelity_score": {
            "row_coverage": round(min(max(row_cov, 0), 1), 4),
            "column_coverage": round(min(max(col_cov, 0), 1), 4),
            "referential_integrity": round(min(max(ref_int, 0), 1), 4),
        },
    }
    if required_props_audit:
        result["required_props_audit"] = required_props_audit
    # ``is not None`` — 주입 0건이어도 기록한다. truthiness 로 걸러면 "주입 없음" 이
    # 키 부재가 되어 "미측정" 과 구분되지 않는다 (``_inject_common_dps_into_tbox_memory``
    # 의 조기 반환 주석 참조). S7 이 T-Box 를 썼는지 산출물로 판정하려면 이 키가
    # 항상 있어야 한다.
    if common_dp_injection is not None:
        result["common_dp_injection"] = common_dp_injection
    if coercion_failures_by_column:
        result["losses"]["type_coercion_failures"]["by_column"] = coercion_failures_by_column
    if warnings.get("instance_audit"):
        result["instance_audit"] = warnings["instance_audit"]
    if warnings.get("unknown_enum_values"):
        result["unknown_enum_values"] = warnings["unknown_enum_values"]
    if warnings.get("duplicate_pk_by_class"):
        result["duplicate_pk_by_class"] = warnings["duplicate_pk_by_class"]
    if warnings.get("property_completeness_detail"):
        result["property_completeness_detail"] = warnings["property_completeness_detail"]
    if warnings.get("value_coverage"):
        value_cov = warnings["value_coverage"]
        result["value_coverage"] = value_cov
        # ``fidelity_score`` 에 값 기준 축을 함께 노출한다. ``column_coverage`` 만
        # 있으면 이름 해석 성공이 데이터 적재로 오독된다 (이 함수 docstring 참조).
        result["fidelity_score"]["value_coverage"] = value_cov.get(
            "value_coverage", 1.0,
        )
    return result


# ── Cell-Level Provenance ────────────────────────


def _collect_instance_provenance(uri: str, table: str, row_idx: int,
                                  pk_col: str, pk_val: str,
                                  properties: dict, relationships: dict) -> dict:
    """인스턴스 하나의 cell-level provenance를 수집한다."""
    return {
        "source_table": table,
        "source_row": row_idx,
        "pk_column": pk_col,
        "pk_value": pk_val,
        "properties": properties,
        "relationships": relationships,
    }


# ── Finalize 헬퍼 (generate_abox 분해) ─────────────
#
# `generate_abox` 가 500+ 줄 monolith 였던 문제(S1)를 완화하기 위해, 후처리
# 단계의 독립 블록을 함수로 추출한다. 각 헬퍼는 부수효과가 명확히 드러나는
# 시그니처를 갖고(순수 계산 vs 파일 쓰기 분리), loop 본체는 call-site 만
# 남아 로컬 변수 30여 개를 따라가는 게 수월해진다. 완전한 AboxBuildContext
# 전환은 후속 세션(#17)에서 계속 진행한다.


def _save_abox_and_master(g: Graph, ctx) -> tuple[int, int, int]:
    """A-Box 본체 + master subgraph 직렬화 후 저장.

    Returns: (abox_size_bytes, master_size_bytes, master_triple_count).
    """
    from domain.tbox_utils import fast_serialize_turtle
    # 메모리 피크 억제: master 직렬화 버퍼(master_g + master_ttl)를 본체 직렬화
    # **전에** 해제해, 두 대형 TTL 문자열이 동시에 메모리에 상주하지 않게 한다.
    # (대용량 A-Box 에서 g + master_g + 2개 TTL 문자열 동시 보유 = 피크 ~2.5x)
    master_g, master_count, _ = _split_master_subgraph(g, ctx.master_classes)
    master_ttl = fast_serialize_turtle(master_g)
    master_path = resolve_generated_path("abox/master_data.ttl")
    atomic_write(master_path, master_ttl)
    master_size = os.path.getsize(master_path)
    del master_ttl, master_g

    ttl = fast_serialize_turtle(g)
    ttl_size = len(ttl.encode("utf-8"))
    atomic_write(resolve_generated_path("abox/a_box.ttl"), ttl)
    del ttl
    return ttl_size, master_size, master_count


def _save_per_class_stats(g: Graph, ctx) -> None:
    """abox_stats.json 저장 (클래스별 인스턴스 / OP / coverage)."""
    abox_stats = _build_abox_stats_dict(
        individual_count=ctx.individual_count,
        g=g,
        object_property_count=ctx.object_property_count,
        tables_processed=ctx.tables_processed,
        per_class_instances=ctx.per_class_instances,
        csv_row_counts=ctx.csv_row_counts,
        per_class_op_triples=ctx.per_class_op_triples,
        master_classes=ctx.master_classes,
        class_table_map=ctx.class_table_map,
        per_class_col_coverage=ctx.per_class_col_coverage,
    )
    # 소스 계보 각인. A-Box 는 CSV·T-Box·딕셔너리 contract 에서 나오고, tacit TTL 을
    # 병합한다 (실측: 관계 트리플 90,094 중 51,223 이 tacit 유래). 그 세대가
    # 기록되지 않으면 abox_stats 가 어느 입력을 서술하는지 알 수 없다.
    # ⚠️ 딕셔너리는 **파일 해시로 각인하지 않는다.** A-Box 가 의존하는 것은 파일
    # 전체가 아니라 ``classes.*.datatype_properties`` 이름 집합(contract)이다. S10 이
    # 같은 파일을 v2(통계 포함)로 덮어쓰므로 파일 해시를 각인하면 **매 실행 어긋남**
    # 이 되고, 탐지기가 상시 빨간불이 된다 (실측 2026-09-03: 세대 고정 직후 이 축이
    # 어긋남으로 보고됐다). contract 는 v1↔v2 에서 안정적이라 값으로 기록한다.
    abox_stats["_source"] = source_stamp(tbox=TBOX_PATH, abox=ABOX_PATH)
    abox_stats["_dict_contract_fingerprint"] = _dict_contract_fp()
    path = resolve_generated_path("abox/abox_stats.json")
    atomic_write_json(path, abox_stats)
    logger.info(
        "A-Box 통계 저장: %s (%d 클래스)", path, len(abox_stats["per_class"]),
    )


def _audit_required_and_completeness(
    g: Graph, ctx, tbox_info: dict,
) -> tuple[dict | None, dict | None, dict | None]:
    """required_props 감사 + property_completeness 상세 + instance audit.

    각 단계는 실패해도 다음 단계 진행 (warn 로그). Returns: 3개 dict (None 가능).
    """
    required_audit: dict | None = None
    try:
        required_audit = _check_required_props_coverage(
            g, ctx.per_class_instances, tbox_info=tbox_info,
        )
    except Exception as e:
        logger.warning("required_props 감사 실패: %s", e)

    # R21-1: low-coverage 컬럼 가시화 — property_completeness 추적용.
    property_completeness_detail: dict | None = None
    try:
        property_completeness_detail = _compute_property_completeness_detail(
            g, ctx.per_class_instances, tbox_info,
        )
    except Exception as e:
        logger.warning("property_completeness detail 계산 실패: %s", e)

    # 값 커버리지 — ``column_coverage`` 가 이름 해석만 세는 것을 보완한다.
    # 실패해도 진행 (부가 지표).
    try:
        value_cov = _compute_value_coverage(
            g, ctx.per_class_instances, tbox_info, ctx.per_class_col_coverage,
        )
        if value_cov:
            ctx.value_coverage = value_cov
            if value_cov["not_loaded"]:
                logger.warning(
                    "값이 실리지 않은 컬럼 %d개 (리터럴도 관계도 없음) — "
                    "column_coverage 는 이름 해석만 세므로 이것을 만점으로 "
                    "덮는다. 예: %s",
                    value_cov["not_loaded"], value_cov["not_loaded_items"][:5],
                )
            if value_cov["loaded_as_relation"]:
                logger.info(
                    "FK 컬럼 %d개가 리터럴 대신 관계 OP 로 적재됐다 (Path B 정상) "
                    "— property_completeness 의 0%% DP 가 이것이다",
                    value_cov["loaded_as_relation"],
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("value_coverage 계산 실패: %s", e)

    instance_audit: dict | None = None
    try:
        instance_audit = _validate_instances(g)
        if instance_audit["untyped_instances"] > 0:
            logger.warning(
                "untyped instances: %d개", instance_audit["untyped_instances"],
            )
        if instance_audit["reserved_char_uris"]:
            logger.warning(
                "reserved char URIs: %s", instance_audit["reserved_char_uris"][:5],
            )
    except Exception as e:
        logger.warning("instance 검증 실패: %s", e)
    return required_audit, property_completeness_detail, instance_audit


def _summarize_duplicate_pks(ctx) -> tuple[int, dict[str, int]]:
    """전체 중복 PK 카운트 + 클래스 별 집계 (R19-12).

    duplicate_pk_uris 는 full URI 단위 → URI → 클래스명 추출 후 count.
    """
    total = sum(1 for c in ctx.duplicate_pk_uris.values() if c > 1)
    by_class: dict[str, int] = {}
    inst_str = str(DOMAIN_INST_NS)
    for uri, cnt in ctx.duplicate_pk_uris.items():
        if cnt <= 1 or not uri.startswith(inst_str):
            continue
        local = uri[len(inst_str):]
        cls_name = local.split("_", 1)[0]
        by_class[cls_name] = by_class.get(cls_name, 0) + 1
    return total, by_class


def _emit_loss_manifest(
    ctx, common_dp_stats: dict | None,
    required_audit: dict | None, property_completeness_detail: dict | None,
    instance_audit: dict | None, dup_count: int, dup_by_class: dict[str, int],
) -> None:
    """A-Box loss manifest JSON 작성."""
    # 레거시 클래스 누적(unverified_fk_targets)은 다른 소비자 호환용으로 유지.
    lm_fk: dict[str, dict] = {
        cls: {"target": cls, "count": cnt}
        for cls, cnt in ctx.unverified_fk_targets.items()
    }
    # 셀 단위 detail — (source_class, column, target_class) 별 실패 셀 수.
    # column 필드에 실제 CSV 컬럼명을 담아 self-target 라벨 버그를 해소한다.
    fk_failures_detail = [
        {"source_class": sc, "column": col, "target": tc, "count": cnt}
        for (sc, col, tc), cnt in sorted(
            ctx.fk_failure_detail.items(), key=lambda kv: kv[1], reverse=True,
        )
    ]
    fk_attempts_total = sum(ctx.fk_attempt_detail.values())
    unknown_enums = _snapshot_unknown_enum_values()
    lm_warnings = {
        "unverified_fk": lm_fk,
        "failed_tables": ctx.failed_tables,
        "duplicate_pk_count": dup_count,
        "duplicate_pk_by_class": dup_by_class or None,
        "instance_audit": instance_audit,
        "unknown_enum_values": unknown_enums or None,
        "property_completeness_detail": property_completeness_detail or None,
        "value_coverage": ctx.value_coverage or None,
    }
    total_rows = sum(ctx.csv_row_counts.values())
    total_cols = sum(
        c.get("total", 0) for c in ctx.per_class_col_coverage.values()
    )
    manifest = _build_loss_manifest(
        lm_warnings, ctx.per_class_col_coverage,
        total_rows=total_rows, total_cols=total_cols,
        coercion_failures=ctx.total_coercion_failures,
        sentinel_filtered=ctx.total_sentinel_filtered,
        required_props_audit=required_audit,
        common_dp_injection=common_dp_stats,
        coercion_failures_by_column=ctx.coercion_failures_by_column or None,
        fk_failures_detail=fk_failures_detail,
        fk_attempts_total=fk_attempts_total,
    )
    manifest["_source"] = source_stamp(tbox=TBOX_PATH, abox=ABOX_PATH)
    manifest["_dict_contract_fingerprint"] = _dict_contract_fp()
    atomic_write_json(
        resolve_generated_path("abox/abox_loss_manifest.json"),
        manifest,
    )


def _dict_contract_fp() -> str:
    """A-Box 가 실제로 소비하는 딕셔너리 contract 의 지문.

    정본은 ``tools.pipeline_state._dict_contract_fingerprint`` 다 (S7 의 의존 축과
    같은 값이어야 하므로 사본을 만들지 않는다).
    """
    try:
        from tools.pipeline_state import _dict_contract_fingerprint
        return _dict_contract_fingerprint()
    except Exception:  # pragma: no cover — 계보 각인 실패가 A-Box 생성을 막지 않는다
        return ""


def _emit_cell_provenance(ctx) -> None:
    """Cell-level provenance JSON 저장 (streaming ON: meta 만 / OFF: full)."""
    stream_on = os.getenv("ABOX_PROVENANCE_STREAM", "true").lower() in (
        "true", "1", "yes",
    )
    gzip_on = os.getenv("ABOX_PROVENANCE_GZIP", "true").lower() in (
        "true", "1", "yes",
    )
    path = resolve_generated_path("abox/abox_provenance.json")
    if stream_on:
        # NDJSON 은 이미 row loop 중 작성. JSON 은 스키마/위치 요약만.
        nd_name = "abox_provenance.ndjson.gz" if gzip_on else "abox_provenance.ndjson"
        meta = {
            "format": "ndjson.gz" if gzip_on else "ndjson",
            "ndjson_path": str(resolve_generated_path(f"abox/{nd_name}")),
            "compression": "gzip" if gzip_on else "none",
            "instance_count": len(ctx.provenance_data),
            "schema_keys": (
                list(next(iter(ctx.provenance_data.values())).keys())
                if ctx.provenance_data else []
            ),
        }
        atomic_write_json(path, meta)
        logger.info("A-Box provenance meta 저장: %s (streaming mode)", path)
    else:
        atomic_write_json(path, ctx.provenance_data)
        logger.info(
            "A-Box cell-level provenance 저장: %s (%d 인스턴스)",
            path, len(ctx.provenance_data),
        )


def _emit_row_provenance_sidecar(ctx) -> None:
    """A4 row-level provenance sidecar (abox_provenance.ttl).

    A-Box 본체의 ``inst → prov://<table>#row=N`` 링크와 짝을 이루는 row entity
    메타데이터를 RDF 로 기록. trace_provenance 도구가 이 파일을 먼저 읽어
    instance → CSV row 역추적에 사용.
    """
    try:
        sidecar_g = _new_graph()
        sidecar_g.bind("prov", PROV)
        sidecar_path = resolve_generated_path("abox/abox_provenance.ttl")
        row_entity_count = 0
        triples_added = 0
        for inst_uri_str, prov_record in ctx.provenance_data.items():
            tbl_name = prov_record.get("source_table")
            row_idx = prov_record.get("source_row")
            if tbl_name is None or row_idx is None:
                continue
            csv_table = (
                tbl_name[:-4] if tbl_name.endswith(".csv") else tbl_name
            )
            csv_path = os.path.join(SOURCE_RAWDATA_DIR, f"{csv_table}.csv")
            added = _annotate_row_provenance(
                sidecar_g,
                URIRef(inst_uri_str),
                csv_table,
                int(row_idx) + 1,  # 0-based → 1-based
                csv_path=csv_path,
            )
            if added > 0:
                row_entity_count += 1
                triples_added += added
        from domain.tbox_utils import fast_serialize_turtle as _fast_ttl
        atomic_write(sidecar_path, _fast_ttl(sidecar_g))
        logger.info(
            "A-Box row-level provenance sidecar 저장: %s (%d row entity, %d triple)",
            sidecar_path, row_entity_count, triples_added,
        )
    except Exception as e:
        logger.warning("row-level provenance sidecar 저장 실패: %s", e)


def _build_finalize_result_dict(
    g: Graph, ctx, ttl_size: int, master_size: int, master_count: int,
    duration_seconds: float, validation_stats: dict | None,
    fk_match_stats: dict | None, dup_count: int,
) -> dict:
    """generate_abox 의 최종 응답 dict 빌드."""
    result = {
        "success": True,
        "partial": bool(ctx.failed_tables),
        "output_path": str(resolve_generated_path("abox/a_box.ttl")),
        "size_bytes": ttl_size,
        "statistics": {
            "individuals": ctx.individual_count,
            "tables_processed": ctx.tables_processed,
            "triples": len(g),
            "object_properties": ctx.object_property_count,
            "promoted_fk_count": ctx.promoted_fk_count,
            "generation_time_seconds": round(duration_seconds, 1),
        },
        "master_data": {
            "path": str(resolve_generated_path("abox/master_data.ttl")),
            "size_bytes": master_size,
            "triples": master_count,
            "classes": sorted(ctx.master_classes),
        },
        "transaction_data": {"classes": sorted(ctx.transaction_classes)},
        "column_coverage": dict(ctx.per_class_col_coverage),
        "instance_completeness": {
            cls: {
                "csv_rows": ctx.csv_row_counts.get(cls, 0),
                "instances": ctx.per_class_instances.get(cls, 0),
                "rate": round(
                    ctx.per_class_instances.get(cls, 0)
                    / max(ctx.csv_row_counts.get(cls, 0), 1) * 100, 1,
                ),
            }
            for cls in sorted(ctx.per_class_instances.keys())
        },
        "warnings": {
            "failed_tables": ctx.failed_tables,
            "failed_count": len(ctx.failed_tables),
            "unverified_fk": {
                "count": sum(ctx.unverified_fk_targets.values()),
                "targets": ctx.unverified_fk_targets,
            } if ctx.unverified_fk_targets else None,
            "duplicate_pk_count": dup_count,
            "duplicate_pk_top10": sorted(
                [
                    (uri.split("/")[-1], cnt)
                    for uri, cnt in ctx.duplicate_pk_uris.items() if cnt > 1
                ],
                key=lambda x: -x[1],
            )[:10] if dup_count > 0 else [],
        },
        "hint": (
            f"전체 A-Box: {ABOX_PATH} / 마스터만: {MASTER_DATA_PATH} "
            "(별도 그래프 분리용)"
        ),
    }
    if validation_stats:
        result["validation"] = validation_stats
    if fk_match_stats is not None:
        # A1: FK value 퍼지 매칭 단계별 히트 수 노출.
        # exact / normalized / levenshtein / prefix / unresolved 카운터.
        result["fk_match_stats"] = fk_match_stats
    return result


def _finalize_abox_outputs(
    *,
    g: Graph,
    ctx,
    tbox_ttl: str,
    common_dp_stats: dict | None,
    tbox_info: dict,
    master_instance_uris: set[str],
    validation_stats: dict | None,
    duration_seconds: float,
    fk_match_stats: dict | None = None,
) -> dict:
    """A-Box 빌드 최종 단계 — master 분리, stats/manifest 저장, 결과 dict 생성.

    Args:
        g: 완성된 전체 A-Box 그래프 (writer 이미 flush 완료).
        ctx: AboxBuildContext 누적 상태.
        tbox_ttl: 공통 DP 주입까지 반영된 T-Box TTL 문자열.
        common_dp_stats: ``_inject_common_dps`` 결과 (None 가능).
        tbox_info: 마지막 재파싱된 ``_parse_tbox`` 결과.
        master_instance_uris: master_data.ttl 에서 로드한 기존 인스턴스 URI set.
        validation_stats: SHACL 자동수정 결과 dict (None 가능).
        duration_seconds: 전체 소요 시간.
        fk_match_stats: A1 — FK value 퍼지 매칭 stage counter (None 가능).

    Returns:
        success_response 로 감싸기 전 raw result dict.
    """
    # 1. master 분리 + A-Box 본체 직렬화
    ttl_size, master_size, master_count = _save_abox_and_master(g, ctx)

    # 2. abox_stats.json
    _save_per_class_stats(g, ctx)

    # 3. required_props + property_completeness + instance audit
    required_audit, property_completeness_detail, instance_audit = (
        _audit_required_and_completeness(g, ctx, tbox_info)
    )

    # 4. duplicate PK 집계 + loss manifest
    dup_count, dup_by_class = _summarize_duplicate_pks(ctx)
    _emit_loss_manifest(
        ctx, common_dp_stats, required_audit, property_completeness_detail,
        instance_audit, dup_count, dup_by_class,
    )

    # 5. provenance (cell-level + row-level sidecar)
    _emit_cell_provenance(ctx)
    _emit_row_provenance_sidecar(ctx)

    # 6. 결과 dict
    return _build_finalize_result_dict(
        g, ctx, ttl_size, master_size, master_count,
        duration_seconds, validation_stats, fk_match_stats, dup_count,
    )


def _split_master_subgraph(
    g: Graph,
    master_classes: set[str],
) -> tuple[Graph, int, set[URIRef]]:
    """Extract a master-only sub-graph from the full A-Box.

    - master 인스턴스의 outgoing 트리플 전부 복사
    - master ↔ master 역트리플도 포함 (예: Warehouse → location of → Item)
    - R21-4: ``ABOX_MASTER_INCLUDE_INCOMING_OP`` (기본 true) 이면 transaction
      → master 역방향 OP 트리플도 복사 + 해당 transaction 인스턴스의
      **rdf:type 만** 함께 보존. 이렇게 하면 master_data.ttl 을 독립적으로
      별도 그래프로 분리해도 master 가 "외로운 노드" 로 나오지 않고
      "이 master 를 참조한 transaction class 들" 까지 탐색 가능.
      이전(R15) 의 "transaction 이 untyped subject 로 등장" 문제는 rdf:type
      도 같이 넣어 해결.
    - incoming OP 보존을 원치 않으면 env var 로 off.

    Returns:
        (master_graph, triple_count, master_instance_uris)
    """
    master_g = _new_graph()
    from domain.namespaces import bind_namespaces
    bind_namespaces(master_g)
    master_count = 0
    master_inst_uris: set[URIRef] = set()
    for cls_name in master_classes:
        cls_uri = DOMAIN_NS_OBJ[cls_name]
        for inst in g.subjects(RDF.type, cls_uri):
            master_inst_uris.add(inst)
            for s, p, o in g.triples((inst, None, None)):
                master_g.add((s, p, o))
                master_count += 1
    for m in master_inst_uris:
        for s, p, o in g.triples((None, None, m)):
            if p == RDF.type:
                continue
            if s in master_inst_uris:
                master_g.add((s, p, o))
                master_count += 1

    # R21-4: 기본 false — master_data 가 FailureCause 같이 outgoing OP 없는
    # master 클래스에서 Closed-World validate_kg check 를 유발. true 로
    # 옵트인하면 master-only 그래프 분리 시 transaction 연결 질의 가능.
    include_incoming = os.getenv("ABOX_MASTER_INCLUDE_INCOMING_OP", "false").lower() in (
        "true", "1", "yes",
    )
    if include_incoming:
        # 외부(transaction) 인스턴스 → master 역방향 OP 포함.
        # untyped 문제 방지를 위해 해당 transaction 의 rdf:type 도 함께.
        added_txn_types: set[URIRef] = set()
        for m in master_inst_uris:
            for s, p, o in g.triples((None, None, m)):
                if p == RDF.type:
                    continue
                if s in master_inst_uris:
                    continue  # already added above
                master_g.add((s, p, o))
                master_count += 1
                if s not in added_txn_types:
                    # transaction 의 rdf:type 만 복사 (모든 DP 복사 시 용량 폭증)
                    for _t in g.objects(s, RDF.type):
                        master_g.add((s, RDF.type, _t))
                        master_count += 1
                    added_txn_types.add(s)
    return master_g, master_count, master_inst_uris


def _build_abox_stats_dict(
    individual_count: int,
    g: Graph,
    object_property_count: int,
    tables_processed: int,
    per_class_instances: dict[str, int],
    csv_row_counts: dict[str, int],
    per_class_op_triples: dict[str, int],
    master_classes: set[str],
    class_table_map: dict[str, str],
    per_class_col_coverage: dict[str, dict],
) -> dict:
    """Assemble the per-class stats JSON written to abox_stats.json."""
    return {
        "generated_at": datetime.now().isoformat(),
        "total_individuals": individual_count,
        "total_triples": len(g),
        "total_object_properties": object_property_count,
        "tables_processed": tables_processed,
        "per_class": {
            cls: {
                "instance_count": per_class_instances.get(cls, 0),
                "csv_rows": csv_row_counts.get(cls, 0),
                "instance_completeness": round(
                    per_class_instances.get(cls, 0) / max(csv_row_counts.get(cls, 0), 1) * 100, 1,
                ),
                "op_triples": per_class_op_triples.get(cls, 0),
                "is_master": cls in master_classes,
                "source_table": class_table_map.get(cls, ""),
                "column_coverage": per_class_col_coverage.get(cls, {}),
            }
            for cls in sorted(set(list(per_class_instances.keys()) + list(per_class_op_triples.keys())))
        },
    }


# ── MCP 도구 ──────────────────────────────────────


def _load_tbox_for_abox(use_dict_contract: bool):
    """T-Box 로드 + parse + class_prop_mapping / obj_props / dict_contract 준비.

    Returns: (tbox_ttl, tbox_info, class_prop_mapping, obj_props, dict_contract)
    또는 (None, None, None, None, error_response_str) — 실패 시 마지막이 str.
    """
    try:
        with open(TBOX_PATH, encoding="utf-8") as f:
            tbox_ttl = f.read()
    except Exception as e:
        return None, None, None, None, error_response(
            f"T-Box 로드 실패: {e}",
            hint=f"{TBOX_PATH} 파일이 있어야 합니다. generate_tbox로 먼저 생성하세요.",
            logger=logger,
        )

    tbox_info = _parse_tbox(tbox_ttl)
    class_prop_mapping = _load_class_prop_mapping()
    obj_props = _load_object_properties()

    # 기능 단계: vocabulary contract 로드 — 시맨틱 딕셔너리 v1 (S6.5) 의
    # classes.X.datatype_properties 에서 class-specific DP 이름 매핑 추출.
    dict_contract: dict[str, dict[str, str]] | None = None
    if use_dict_contract:
        from config import SEMANTIC_DICT_PATH as _SEM_DICT_PATH
        dict_contract = _load_dict_contract(_SEM_DICT_PATH) or None
        if dict_contract:
            logger.info(
                "R1B: vocabulary contract 로드 — %d classes",
                len(dict_contract),
            )

    # xsd:time 사용 여부 감지 — 미사용 시 _convert_time 경로 dead-code 경고만.
    _xsd_time_str = str(XSD.time)
    if not any(r == _xsd_time_str for r in tbox_info.get("dp_ranges", {}).values()):
        logger.debug("T-Box 에 xsd:time range DP 없음 — _convert_time 경로 미사용")

    return tbox_ttl, tbox_info, class_prop_mapping, obj_props, dict_contract


def _write_tbox_injection_overlay(tbox_ttl: str, dp_names: list[str]) -> str:
    """A-Box가 추가한 DP 선언만 별도 T-Box overlay로 저장한다."""
    overlay = _new_graph()
    from domain.namespaces import bind_namespaces

    bind_namespaces(overlay)
    if dp_names:
        complete = _new_graph()
        complete.parse(data=tbox_ttl, format="turtle")
        for name in dp_names:
            uri = DOMAIN_NS_OBJ[name]
            for triple in complete.triples((uri, None, None)):
                overlay.add(triple)

    overlay_path = resolve_generated_path("tbox/abox_injections.ttl")
    atomic_write(overlay_path, overlay.serialize(format="turtle"))
    return str(overlay_path)


def _inject_common_dps_into_tbox_memory(
    tbox_ttl: str, tbox_info: dict, csv_files: list, filter_names: set | None,
) -> tuple[str, dict, dict | None]:
    """공통 DP (rules/contracts/common_dp.json) 자동 주입 — T-Box 메모리 그래프 보강.

    CSV 헤더에 Timestamp / Value / Unit 같은 alias 가 있으나 T-Box 에 해당
    DP 가 없을 때 in-memory 주입 후 ``_col_to_prop`` 매칭이 성공한다. 주입분은
    기본적으로 ``data/generated/tbox/abox_injections.ttl`` overlay에 저장하고,
    ``validate_kg``가 정본 T-Box와 함께 읽는다. 환경변수
    ``ABOX_WRITE_TBOX_INJECTIONS=true``를 명시한 경우에만 정본 T-Box에도 쓴다.

    ## 이 쓰기는 **스키마 단계를 인스턴스 단계가 되쓰는** 것이다 (2026-08-30)

    S7 은 자기 **입력**(T-Box)을 실행 중에 수정한다. 부작용 셋:

    1. ``t_box.ttl`` mtime 이 ``a_box.ttl`` 보다 **나중**이 되어, mtime 기반
       staleness 판정이 "T-Box 가 A-Box 보다 새롭다 = A-Box 가 낡았다" 로 읽는다.
       실측 이력: A-Box 8/26 vs T-Box 8/27 skew 로 A-Box 가 쓰는 DP 18건이 T-Box
       에 없었는데 24개 check 중 아무것도 발화하지 않았다.
    2. S3 가 확정한 T-Box 와 배포 T-Box 가 달라지므로, S4 검증 결과가 **실제
       배포본이 아닌 것**에 대한 것이 된다.
    3. 무엇이 왜 추가됐는지 산출물에 남지 않았다 — 로그 한 줄뿐이라 다음 사람이
       "이 DP 는 어디서 왔나" 를 추적할 수 없다.

    그래서 선언 자체를 버리지 않고 정본과 분리된 overlay로 보존한다. 그렇지 않으면
    후속 ``validate_kg``가 A-Box가 쓰는 DP를 못 보고 ``undeclared_dp``를 대량
    발화한다. overlay는 다음 정보를 남긴다:

    * 주입된 DP 마다 ``skos:note`` 로 출처를 각인 (누가·왜 추가했는지)
    * ``injection_audit`` 을 반환해 응답/loss manifest 에 실린다
    * 쓰기 전후 mtime 을 기록해 staleness 판정이 이 쓰기를 구분할 수 있게 한다

    Returns: (tbox_ttl_after_inject, tbox_info_after_inject, common_dp_stats).
    """
    plan = _plan_common_dp_injection(tbox_info, csv_files, filter_names)
    if not plan:
        # 이전 실행의 overlay를 비워야 삭제된 주입 DP가 선언된 채 남지 않는다.
        overlay_path = _write_tbox_injection_overlay(tbox_ttl, [])
        return tbox_ttl, tbox_info, {
            "injected": 0,
            "by_name": {},
            "injection_audit": {
                "injected_dps": [],
                "injected_count": 0,
                "tbox_file_written": False,
                "overlay_path": overlay_path,
                "overlay_written": True,
                "tbox_mtime_before": (
                    os.path.getmtime(TBOX_PATH)
                    if os.path.exists(TBOX_PATH) else None
                ),
                "reason": (
                    "CSV 컬럼에 대응하는 DP 가 T-Box 에 모두 있어 주입할 것이 "
                    "없었다 — S7 은 T-Box 파일을 쓰지 않았다."
                ),
            },
        }

    tbox_mtime_before = (
        os.path.getmtime(TBOX_PATH) if os.path.exists(TBOX_PATH) else None
    )
    tbox_ttl, common_dp_stats = _inject_common_dps(tbox_ttl, plan)
    if common_dp_stats["injected"] > 0:
        logger.info(
            "A-Box: 공통 DP %d개 T-Box 메모리 주입 (%s)",
            common_dp_stats["injected"],
            ", ".join(sorted(common_dp_stats["by_name"].keys())),
        )
        injected_names = sorted(common_dp_stats["by_name"].keys())
        # 주입 출처를 T-Box 에 각인한다 — 로그만 남기면 다음 사람이 "이 DP 는
        # 어디서 왔나" 를 추적할 수 없고, S2 재생성 때 사라져도 아무도 모른다.
        tbox_ttl = _annotate_injected_dps(tbox_ttl, injected_names)
        tbox_info = _parse_tbox(tbox_ttl)
        write_enabled = os.getenv(
            "ABOX_WRITE_TBOX_INJECTIONS", "false",
        ).lower() in ("true", "1", "yes")
        overlay_path = _write_tbox_injection_overlay(tbox_ttl, injected_names)
        audit = {
            "injected_dps": injected_names,
            "injected_count": common_dp_stats["injected"],
            "tbox_file_written": False,
            "overlay_path": overlay_path,
            "overlay_written": True,
            "tbox_mtime_before": tbox_mtime_before,
            "reason": (
                "CSV 컬럼에 대응하는 DP 가 T-Box 에 없어 A-Box 생성기가 "
                "on-demand 주입하고 별도 overlay에 저장했다 (Path B). "
                "validate_kg는 정본 T-Box와 overlay를 함께 읽는다. 다음 S2 "
                "재생성에서 같은 DP를 만들지 않으면 이 주입이 반복되며, 근본 "
                "수정은 S2 프롬프트나 rules/domain/tbox_manual_additions.ttl 이다."
            ),
        }
        if write_enabled:
            try:
                canonical_tbox_path = resolve_generated_path("tbox/t_box.ttl")
                atomic_write(canonical_tbox_path, tbox_ttl)
                audit["tbox_file_written"] = True
                audit["tbox_mtime_after"] = os.path.getmtime(canonical_tbox_path)
                logger.warning(
                    "S7 이 T-Box 파일을 수정했다 (공통 DP %d개 persist): %s. "
                    "기본값은 false이며 이 호환 모드는 t_box.ttl mtime을 바꾼다. "
                    "일반 경로에서는 overlay를 사용하라.",
                    common_dp_stats["injected"], canonical_tbox_path,
                )
            except Exception as e:
                audit["error"] = str(e)[:200]
                logger.warning("T-Box 파일 persist 실패 (무시하고 진행): %s", e)
        common_dp_stats["injection_audit"] = audit
    return tbox_ttl, tbox_info, common_dp_stats


def _annotate_injected_dps(tbox_ttl: str, dp_names: list[str]) -> str:
    """주입된 DP 에 ``skos:note`` 로 출처를 각인한다.

    각인하지 않으면 "이 DP 는 S2 가 만든 것인가, A-Box 가 주입한 것인가" 를
    산출물만 보고 구분할 수 없다. 그 구분이 필요한 이유: 주입분은 **매 실행마다
    다시 만들어지는 임시 보정**이고, 근본 수정은 S2 프롬프트나 수동 추가분이다.
    """
    if not dp_names:
        return tbox_ttl
    try:
        from rdflib import Literal as _Literal
        from rdflib.namespace import SKOS as _SKOS

        from domain.tbox_utils import _new_graph as _ng

        graph = _ng()
        graph.parse(data=tbox_ttl, format="turtle")
        note = (
            "A-Box 생성기가 on-demand 주입한 DP (rules/contracts/common_dp.json "
            "템플릿 기반). S2 가 이 컬럼의 DP 를 만들지 않아 보정된 것이므로, "
            "영구히 두려면 rules/domain/tbox_manual_additions.ttl 에 옮기라."
        )
        for name in dp_names:
            uri = DOMAIN_NS_OBJ[name]
            if (uri, _SKOS.note, None) in graph:
                continue
            graph.add((uri, _SKOS.note, _Literal(note, lang="ko")))
        return graph.serialize(format="turtle")
    except Exception as exc:  # noqa: BLE001 — 각인 실패가 A-Box 생성을 막지 않는다
        logger.debug("주입 DP 출처 각인 실패 (무시): %s", exc)
        return tbox_ttl


def _collect_available_classes_and_tacit(
    csv_files: list, filter_names: set | None, tbox_info: dict,
) -> tuple[set, list]:
    """FK target 검증용 available_classes + tacit triple 수집.

    R19: tacit 그래프를 단순 클래스명 수집에 그치지 않고 실제 트리플도 보관 →
    이후 A-Box 그래프에 병합 (R14 의 본래 의도 실현).

    Returns: (available_classes set, tacit_triples list).
    """
    available_classes: set[str] = set()
    for csv_path in csv_files:
        fname = os.path.basename(csv_path).replace(".csv", "")
        if filter_names and fname not in filter_names:
            continue
        available_classes.add(_table_to_class(fname, tbox_info))

    tacit_triples: list[tuple] = []
    try:
        if not os.path.isdir(SOURCE_TACIT_DIR):
            return available_classes, tacit_triples
        for tacit_path in glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")):
            tacit_g = _new_graph()
            try:
                tacit_g.parse(tacit_path, format="turtle")
            except Exception as e:
                logger.warning("tacit parse 실패 %s: %s", tacit_path, e)
                continue
            for _, _, t_cls in tacit_g.triples((None, RDF.type, None)):
                if not isinstance(t_cls, URIRef):
                    continue
                nm = _local_name(str(t_cls))
                if nm:
                    available_classes.add(nm)
            for s, p, o in tacit_g:
                tacit_triples.append((s, p, o))
        if tacit_triples:
            logger.info(
                "tacit 그래프 병합 예정: %d 트리플 (%d 클래스 인식)",
                len(tacit_triples), len(available_classes),
            )
    except Exception as e:
        logger.warning("tacit 수집 실패: %s", e)
    return available_classes, tacit_triples


def _init_abox_graph(tacit_triples: list) -> tuple[Graph, "_BulkNTWriter", URIRef]:
    """rdflib Graph + bulk writer + abox_uri 초기화. tacit triple 즉시 병합."""
    g = _new_graph()
    g.bind(NS_PREFIX, DOMAIN_NS)
    g.bind(NS_INST_PREFIX, DOMAIN_INST_NS)
    g.bind("iof-core", IOF_CORE_NS)
    g.bind("owl", OWL)
    g.bind("rdf", RDF)
    g.bind("rdfs", RDFS)
    g.bind("xsd", XSD)
    g.bind("prov", PROV)

    abox_uri = URIRef(f"{ONTOLOGY_URI}/abox")
    w = _BulkNTWriter(g)
    w.add((abox_uri, RDF.type, OWL.Ontology))
    w.add((
        abox_uri, RDFS.label,
        Literal(f"{DOMAIN_CONFIG['domain']['name']} A-Box", lang="ko"),
    ))

    if tacit_triples:
        for t in tacit_triples:
            w.add(t)
        logger.info("tacit triples 병합 완료: %d 건", len(tacit_triples))
    return g, w, abox_uri


def _open_provenance_ndjson_fh():
    """A-Box provenance NDJSON streaming 파일 핸들 (gzip 옵션).

    환경변수 ABOX_PROVENANCE_STREAM=false 면 None 반환 (메모리 dict 만 사용).
    Returns: (file handle or None, path).
    """
    if os.getenv("ABOX_PROVENANCE_STREAM", "true").lower() not in ("true", "1", "yes"):
        return None, ""
    use_gzip = os.getenv("ABOX_PROVENANCE_GZIP", "true").lower() in ("true", "1", "yes")
    filename = "abox_provenance.ndjson.gz" if use_gzip else "abox_provenance.ndjson"
    path = resolve_generated_path(f"abox/{filename}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if use_gzip:
        import gzip as _gzip
        fh = _gzip.open(path, "wt", encoding="utf-8")
    else:
        fh = open(path, "w", encoding="utf-8")
    return fh, str(path)


def _load_master_index(available_classes: set) -> tuple[set, dict, dict]:
    """master_data.ttl 에서 instance URI set + fuzzy match index + config 로드.

    Returns: (master_instance_uris, master_value_index, fuzzy_config).
    """
    master_instance_uris: set[str] = set()
    if os.path.exists(MASTER_DATA_PATH):
        try:
            from domain.tbox_utils import _new_graph as _ng
            from domain.tbox_utils import fast_parse_turtle
            mg = _ng()
            fast_parse_turtle(mg, MASTER_DATA_PATH)
            master_instance_uris = {
                str(s) for s in mg.subjects(RDF.type, None)
                if str(s).startswith(DOMAIN_INST_NS)
            }
            del mg
        except Exception:
            pass

    from tools.fk_matching import build_master_value_index as _build_mv_idx
    master_value_index = _build_mv_idx(
        master_instance_uris, available_classes=available_classes,
    )
    fuzzy_config = _load_fuzzy_config()
    logger.debug(
        "FK fuzzy match config=%s, index classes=%d",
        fuzzy_config, len(master_value_index),
    )
    return master_instance_uris, master_value_index, fuzzy_config


def _resolve_prefetch_limit_bytes() -> int:
    """ABOX_PREFETCH_MAX_MB 명시 → 고정값 / 미명시 → psutil 적응형 (avail × 30%).

    경계 [50MB, 2GB]. psutil 미설치 시 150MB.
    """
    env_cap = os.getenv("ABOX_PREFETCH_MAX_MB")
    if env_cap:
        try:
            return int(env_cap) * 1024 * 1024
        except ValueError:
            return 150 * 1024 * 1024
    try:
        import psutil  # type: ignore[import-not-found]
        available = psutil.virtual_memory().available
        cap = int(available * 0.3)
        cap = max(50 * 1024 * 1024, min(cap, 2 * 1024 * 1024 * 1024))
        logger.debug(
            "prefetch cap adaptive: %.1f MB (available %.1f GB × 30%%)",
            cap / (1024 * 1024), available / (1024 * 1024 * 1024),
        )
        return cap
    except Exception:
        return 150 * 1024 * 1024


def _prefetch_csv_files(csv_files: list, limit_bytes: int) -> dict[str, str] | None:
    """병렬 CSV prefetch — 합계가 limit 이하인 파일만 RAM 에 로드.

    환경변수 ABOX_PARALLEL_PREFETCH=false 또는 csv 4개 미만이면 None.
    """
    if os.getenv("ABOX_PARALLEL_PREFETCH", "true").lower() not in ("true", "1", "yes"):
        return None
    if len(csv_files) < 4:
        return None

    def _read_csv_text(path: str) -> tuple[str, str]:
        # utf-8-sig: BOM 을 헤더 키에 섞지 않는다. 일반 utf-8 로 읽으면 첫 컬럼이
        # ``﻿Equipment_ID`` 가 되어 DP/FK 매핑이 전부 실패한다 (검증 쪽은
        # utf-8-sig 를 쓰므로 생산자만 다르게 보였다 — 2026-08-08 규명).
        with open(path, encoding="utf-8-sig") as f:
            return path, f.read()

    total_size = 0
    to_prefetch: list[str] = []
    for p in csv_files:
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if total_size + sz > limit_bytes:
            continue
        total_size += sz
        to_prefetch.append(p)
    if not to_prefetch:
        return None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = min(4, len(to_prefetch))
    prefetched: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        pending = {executor.submit(_read_csv_text, p): p for p in to_prefetch}
        for fut in as_completed(pending):
            p, txt = fut.result()
            prefetched[p] = txt
    if len(to_prefetch) < len(csv_files):
        logger.info(
            "CSV prefetch: %d/%d 파일(%.1f MB) 메모리 로드, 나머지 %d개는 스트리밍",
            len(to_prefetch), len(csv_files), total_size / (1024 * 1024),
            len(csv_files) - len(to_prefetch),
        )
    return prefetched


def _emit_csv_provenance(
    g: Graph, w: "_BulkNTWriter", abox_uri: URIRef, ctx: "AboxBuildContext",
    tbox_ttl: str,
) -> None:
    """CSV 소스별 PROV-O Entity + checksum + buildId 기록.

    R19-14: build-level provenance — UUID + T-Box digest. CSV mtime 으로
    generatedAtTime 기록 (데이터 최종 수정 시각이 prov 의미). sha256 checksum
    으로 재현성 검증.
    """
    import hashlib as _hashlib
    import uuid as _uuid
    csv_checksum = URIRef(f"{ONTOLOGY_URI}/prov/csvSha256")
    tbox_checksum = URIRef(f"{ONTOLOGY_URI}/prov/tboxSha256")
    build_id_pred = URIRef(f"{ONTOLOGY_URI}/prov/buildId")

    build_id = str(_uuid.uuid4())
    try:
        tbox_sha = _hashlib.sha256(tbox_ttl.encode("utf-8")).hexdigest()
    except Exception:
        tbox_sha = ""
    w.add((abox_uri, build_id_pred, Literal(build_id, datatype=XSD.string)))
    if tbox_sha:
        w.add((abox_uri, tbox_checksum, Literal(tbox_sha, datatype=XSD.string)))

    for _cls_name, tbl_name in ctx.class_table_map.items():
        src_uri = URIRef(f"{ONTOLOGY_URI}/source/{tbl_name}")
        csv_path = os.path.join(SOURCE_RAWDATA_DIR, f"{tbl_name}.csv")
        w.add((src_uri, RDF.type, PROV.Entity))
        w.add((src_uri, RDFS.label, Literal(f"{tbl_name}.csv", lang="en")))
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(csv_path)).isoformat()
            w.add((
                src_uri, PROV.generatedAtTime,
                Literal(mtime, datatype=XSD.dateTime),
            ))
            h = _hashlib.sha256()
            with open(csv_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            w.add((
                src_uri, csv_checksum,
                Literal(h.hexdigest(), datatype=XSD.string),
            ))
        except Exception as e:
            logger.warning("CSV provenance 기록 실패 %s: %s", tbl_name, e)
            w.add((
                src_uri, PROV.generatedAtTime,
                Literal(datetime.now().isoformat(), datatype=XSD.dateTime),
            ))


def _backfill_fk_target_exists(g: Graph, ctx: "AboxBuildContext", master_instance_uris: set) -> None:
    """FK target_exists 후보정 — 모든 인스턴스가 그래프에 들어간 시점에 정확 판정.

    이전엔 컬럼명 vs 클래스명 차원 불일치로 항상 True 였던 버그를 회피.
    """
    all_known: set[str] = set(master_instance_uris)
    inst_str = str(DOMAIN_INST_NS)
    for s in g.subjects():
        s_str = str(s)
        if s_str.startswith(inst_str):
            all_known.add(s_str)
    for pd in ctx.provenance_data.values():
        for rel in pd.get("relationships", {}).values():
            if rel.get("target_exists") is None:
                t_uri = rel.get("target_uri")
                rel["target_exists"] = bool(t_uri) and t_uri in all_known


def _apply_abox_shacl_autofix(g: Graph, tbox_ttl: str, ctx: "AboxBuildContext") -> dict | None:
    """A-Box SHACL validation + deterministic fix (대용량 cap, opt-in/opt-out).

    환경변수:
      - ABOX_SHACL_AUTOFIX=true (default) — 활성화
      - ABOX_SHACL_MAX_INSTANCES=200000 (default) — 초대형 보호
      - ABOX_APPLY_INVERSE_ON_GENERATE=true — S7 자체 inverse 보강 (default off)

    Returns: validation_stats dict 또는 None (skip 시).
    """
    try:
        shacl_cap = int(os.getenv("ABOX_SHACL_MAX_INSTANCES", "200000"))
    except ValueError:
        shacl_cap = 200000

    if os.getenv("ABOX_APPLY_INVERSE_ON_GENERATE", "false").lower() in ("true", "1", "yes"):
        try:
            from domain.tbox_utils import ensure_inverse_triples
            added = ensure_inverse_triples(g, tbox_path=TBOX_PATH)
            logger.info("S7 ensure_inverse_triples 적용: %d 역트리플 추가", added)
        except Exception as e:
            logger.warning("ensure_inverse_triples 실패: %s", e)

    if ctx.individual_count > shacl_cap:
        logger.info(
            "A-Box SHACL autofix 건너뜀 — 인스턴스 %d > cap %d (ABOX_SHACL_MAX_INSTANCES).",
            ctx.individual_count, shacl_cap,
        )
        return None
    if os.getenv("ABOX_SHACL_AUTOFIX", "true").lower() not in ("true", "1", "yes"):
        logger.info("A-Box SHACL autofix 건너뜀 (ABOX_SHACL_AUTOFIX=true 로 활성화 가능)")
        return None

    validation_result = _validate_and_fix_abox(
        g, tbox_ttl, pre_added_types=ctx.fk_added_types,
    )
    if not validation_result:
        return None
    return {
        "conforms_before": validation_result["conforms_before"],
        "conforms_after": validation_result["conforms_after"],
        "violations_before": validation_result["violations_before"],
        "violations_after": validation_result["violations_after"],
        "fix_stats": validation_result["fix_stats"],
    }


def _precollect_known_instance_uris(
    csv_files: list[str], filter_names: "set[str] | None", tbox_info: dict,
    pk_columns_out: "dict | None" = None,
) -> set[str]:
    """CSV 루프 전 1-pass: 모든 테이블의 PK 로 생성될 인스턴스 IRI 문자열 집합 수집.

    row loop(_process_single_csv)와 동일한 규칙으로 IRI 를 계산한다:
    class_name = _table_to_class, pk = _resolve_explicit_pk(우선)/_detect_pk_column,
    IRI = DOMAIN_INST_NS#{class_name}_{_uri_safe_local(pk_value)}.
    PK 미감지 행은 row loop 가 행 번호로 비결정적 IRI 를 만들므로 (FK 타겟이 될 수
    없음) 여기서 생략한다.

    이 집합은 정방향 FK 가 실제 존재하는 타겟만 OP·rdf:type 으로 생성하도록
    게이팅하는 데 쓰인다 (마스터 테이블이 없는 도메인의 stub 방지).
    """
    known: set[str] = set()
    inst_prefix = str(DOMAIN_INST_NS)
    for csv_path in csv_files:
        filename = os.path.basename(csv_path).replace(".csv", "")
        if filter_names and filename not in filter_names:
            continue
        try:
            with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception as exc:
            logger.debug("known-URI 선수집 실패 %s: %s", filename, exc)
            continue
        if not rows:
            continue
        class_name = _table_to_class(filename, tbox_info)
        pk_column = _resolve_explicit_pk(filename, rows)
        pk_is_authoritative = pk_column is not None
        if not pk_is_authoritative:
            pk_column = _detect_pk_column(rows, class_name)
        if pk_columns_out is not None and pk_column is not None:
            # 전수 읽기로 정한 PK 를 row loop 에 넘겨준다 — streaming 경로가 20K
            # 표본으로 다르게 판정하면 그 테이블의 FK 가 전부 stub 이 된다.
            pk_columns_out[filename] = pk_column
        for row in rows:
            pk_value = _detect_pk_value(
                row, class_name, pk_column=pk_column,
                pk_is_authoritative=pk_is_authoritative,
            )
            if pk_value:
                known.add(f"{inst_prefix}{class_name}_{_uri_safe_local(pk_value)}")
    logger.info("known-URI 선수집 완료: %d 개 인스턴스 IRI", len(known))
    return known


# (dimensions, mtime) — 파일이 바뀌면 재로드 (서버 재시작 없이 설정 반영).
_DIMENSION_CONFIG_CACHE: "tuple[dict, float] | None" = None


def _load_dimension_config() -> dict:
    """rules/domain/dimension_config.json 로드 (차원 정의: 코드 컬럼 → 차원 클래스/OP/라벨).

    파일 mtime 기반 캐시 — 파일이 디스크에서 바뀌면 재로드한다 (column_dictionary
    와 동일 패턴). 파일 없으면 {} 반환 → 차원 처리 자체를 skip (기존 동작 유지).
    """
    global _DIMENSION_CONFIG_CACHE
    path = rules_path("dimension_config.json", base=_RULES_DIR)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _DIMENSION_CONFIG_CACHE = ({}, 0.0)
        return {}
    if _DIMENSION_CONFIG_CACHE is not None and _DIMENSION_CONFIG_CACHE[1] == mtime:
        return _DIMENSION_CONFIG_CACHE[0]
    result: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            result = json.load(f).get("dimensions", {}) or {}
    except Exception as exc:
        logger.warning("dimension_config.json 로드 실패: %s", exc)
    _DIMENSION_CONFIG_CACHE = (result, mtime)
    return result


def _process_dimensions(
    csv_files: list[str], filter_names: "set[str] | None", tbox_info: dict,
    ctx, writer: "_BulkNTWriter",
) -> dict:
    """차원(Dimension) 처리 — 트랜잭션 코드 컬럼을 차원 인스턴스로 승격 + Fact→차원 OP.

    rules/domain/dimension_config.json 의 각 차원에 대해:
      1. Fact 테이블들의 source_column 에서 distinct 코드값 수집 → 차원 인스턴스
         생성 (rdf:type + 라벨 DP). IRI = {DimClass}_{uri_safe(code)}.
      2. 각 Fact 행마다 코드값 → Fact→차원 OP (정방향 + inverse) emit.
      3. 생성한 차원 IRI 를 ctx.known_instance_uris 에 등록 (다른 stub 게이트 호환).

    마스터 테이블이 없어도 코드값만으로 차원을 세우는 경로. label 은 config 의
    code_labels(있으면) 로 부착, 없으면 생략(코드값이 곧 식별자).

    Returns: {dim_class: {"instances": n, "op_triples": n}} 통계.
    """
    dims = _load_dimension_config()
    if not dims:
        return {}
    inst_prefix = str(DOMAIN_INST_NS)
    stats: dict = {}
    # 파일명 → 클래스 매핑 + 행 캐시 (테이블당 1회 로드)
    table_rows: dict[str, list[dict]] = {}
    table_class: dict[str, str] = {}
    for csv_path in csv_files:
        filename = os.path.basename(csv_path).replace(".csv", "")
        if filter_names and filename not in filter_names:
            continue
        cls = _table_to_class(filename, tbox_info)
        table_class[filename] = cls

    def _rows_for_class(cls_name: str) -> "list[tuple[str, list[dict]]]":
        out = []
        for csv_path in csv_files:
            filename = os.path.basename(csv_path).replace(".csv", "")
            if filter_names and filename not in filter_names:
                continue
            if table_class.get(filename) != cls_name:
                continue
            if filename not in table_rows:
                try:
                    with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
                        table_rows[filename] = list(csv.DictReader(f))
                except Exception:
                    table_rows[filename] = []
            out.append((filename, table_rows[filename]))
        return out

    for dim_class, spec in dims.items():
        src_col = spec.get("source_column")
        fact_classes = spec.get("fact_classes", [])
        op = spec.get("object_property", {})
        fwd, inv = op.get("name"), op.get("inverse")
        label_dp = spec.get("label_dp")
        code_labels = spec.get("code_labels", {})
        if not (src_col and fwd and fact_classes):
            continue
        dim_cls_uri = DOMAIN_NS_OBJ[dim_class]
        emitted_codes: set[str] = set()
        inst_n = 0
        op_n = 0
        for fact_cls in fact_classes:
            for _fname, rows in _rows_for_class(fact_cls):
                for row in rows:
                    raw = str(row.get(src_col, "")).strip()
                    if not raw:
                        continue
                    code = _uri_safe_local(raw)
                    dim_uri = DOMAIN_INST_NS_OBJ[f"{dim_class}_{code}"]
                    # 차원 인스턴스 1회 생성 (type + label)
                    if code not in emitted_codes:
                        emitted_codes.add(code)
                        writer.add((dim_uri, RDF.type, dim_cls_uri))
                        ctx.known_instance_uris.add(str(dim_uri))
                        if label_dp and raw in code_labels:
                            writer.add((
                                dim_uri, DOMAIN_NS_OBJ[label_dp],
                                Literal(code_labels[raw], datatype=XSD.string),
                            ))
                        inst_n += 1
                    # Fact PK 로 Fact IRI 복원 → Fact→차원 OP
                    fact_pk = _resolve_explicit_pk(_fname, rows)
                    fact_auth = fact_pk is not None
                    if not fact_auth:
                        fact_pk = _detect_pk_column(rows, fact_cls)
                    pkv = _detect_pk_value(
                        row, fact_cls, pk_column=fact_pk,
                        pk_is_authoritative=fact_auth,
                    )
                    if not pkv:
                        continue
                    fact_uri = DOMAIN_INST_NS_OBJ[f"{fact_cls}_{_uri_safe_local(pkv)}"]
                    writer.add((fact_uri, DOMAIN_NS_OBJ[fwd], dim_uri))
                    op_n += 1
                    if inv:
                        writer.add((dim_uri, DOMAIN_NS_OBJ[inv], fact_uri))
                        op_n += 1
        stats[dim_class] = {"instances": inst_n, "op_triples": op_n}
        ctx.record_instance(dim_class, inst_n)
        ctx.object_property_count += op_n
        # 차원은 마스터/참조 데이터 → master_data.ttl 분리 대상으로 등록.
        # (CSV 테이블이 아니라 코드로 생성하므로 _classify_master_or_transaction
        #  경로를 거치지 않아 여기서 명시적으로 master_classes 에 추가한다.)
        if inst_n > 0:
            ctx.master_classes.add(dim_class)
        logger.info(
            "차원 %s: 인스턴스 %d, OP %d (source=%s) — master_classes 등록",
            dim_class, inst_n, op_n, src_col,
        )
    return stats


def generate_abox(tables: str = "", use_dict_contract: bool = True) -> str:
    """A-Box(RDF 인스턴스)를 생성한다. 로컬 T-Box + CSV 데이터로부터 rdflib로 변환.

    예상 소요시간: 30~50초

    LLM 불필요. T-Box에 정의된 클래스/프로퍼티를 기반으로 CSV 행을 RDF 인스턴스로 매핑.
    FK 컬럼은 자동으로 ObjectProperty 관계로 변환.
    생성된 TTL은 반환만 하며 자동 업로드하지 않는다.

    Args:
        tables: 생성할 테이블명 (쉼표 구분). 비어있으면 전체 CSV 파일 대상.
        use_dict_contract: True (default) 면 기능 단계 — 시맨틱 딕셔너리 v1
            (SEMANTIC_DICT_PATH) 을 **vocabulary contract** 로 참조. A-Box 가
            contract 의 class-specific DP 이름만 사용 (Path B 강제). 딕셔너리
            파일 없으면 자동으로 기존 Path B 동작 (T-Box 직접 참조) 으로 폴백.
            False 면 contract 무시 (backward compat).
    """
    try:
        start_time = time.monotonic()
        # 캐시 무효화 + counter 초기화 — 반복 호출 시 누적 방지.
        with _tbox_cache_lock:
            _invalidate_tbox_caches_if_stale()
        # fk_patterns.json 이 디스크에서 바뀌었으면 재로드 (서버 재시작 불필요).
        _refresh_fk_patterns_if_stale()
        _reset_fk_skeleton_cache()
        _reset_unknown_enum_values()
        from tools.fk_matching import clear_stats as _fk_clear_stats
        _fk_clear_stats()

        # 1. T-Box 로드
        tbox_ttl, tbox_info, class_prop_mapping, obj_props, dict_contract = (
            _load_tbox_for_abox(use_dict_contract)
        )
        if tbox_ttl is None:
            return dict_contract  # type: ignore[return-value]  # error str

        # 2. CSV 목록 + 공통 DP 주입
        csv_files = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
        if not csv_files:
            return error_response(
                f"CSV 파일이 없습니다: {SOURCE_RAWDATA_DIR}/*.csv",
                logger=logger,
            )
        # 내용이 동일한 CSV 사본은 하나만 적재한다 — S2 와 같은 규칙을 써서
        # T-Box 클래스 집합과 A-Box 인스턴스 집합이 어긋나지 않게 한다.
        # (사본을 둘 다 적재하면 같은 실체가 두 클래스로 나뉘어 한쪽은 관계
        #  0건인 고립 인스턴스가 된다.)
        from tools.common import dedupe_identical_csvs
        csv_files, _ = dedupe_identical_csvs(
            csv_files, preferred_tables=set(_load_table_class_mapping()),
        )
        filter_names = {t.strip() for t in tables.split(",")} if tables else None
        tbox_ttl, tbox_info, common_dp_stats = _inject_common_dps_into_tbox_memory(
            tbox_ttl, tbox_info, csv_files, filter_names,
        )

        # 3. available_classes + tacit triples
        available_classes, tacit_triples = _collect_available_classes_and_tacit(
            csv_files, filter_names, tbox_info,
        )

        # 4. Graph + bulk writer + tacit 즉시 병합
        g, w, abox_uri = _init_abox_graph(tacit_triples)

        # 5. ctx + provenance NDJSON + master index + prefetch
        ctx = AboxBuildContext()
        provenance_ndjson_fh, provenance_ndjson_path = _open_provenance_ndjson_fh()
        master_instance_uris, master_value_index, fuzzy_config = _load_master_index(
            available_classes,
        )
        prefetch_limit_bytes = _resolve_prefetch_limit_bytes()
        prefetched = _prefetch_csv_files(csv_files, prefetch_limit_bytes)

        # 5b. known-URI 선수집 (1-pass) — 정방향 FK 가 실재 타겟만 가리키도록 게이팅.
        #     선수집이 **전수 읽기로** 정한 PK 를 함께 받아 row loop 에 물려준다:
        #     streaming 경로가 20K 표본으로 다른 PK 를 고르면 발행 IRI 가 이 집합에
        #     하나도 없어 그 테이블로 향하는 FK 가 전부 stub 으로 버려진다.
        ctx.known_instance_uris = _precollect_known_instance_uris(
            csv_files, filter_names, tbox_info,
            pk_columns_out=ctx.precollected_pk_columns,
        )

        # 5c. 차원(Dimension) 처리 — 코드 컬럼 → 차원 인스턴스 + Fact→차원 OP.
        # known-URI 선수집 직후 실행 (차원 IRI 를 known 에 등록해 stub 게이트 호환).
        ctx.dimension_stats = _process_dimensions(
            csv_files, filter_names, tbox_info, ctx, w,
        )

        # 6. CSV 루프 → _process_single_csv 호출
        tbl_idx = 0
        tbl_total = sum(
            1 for p in csv_files
            if not filter_names
            or os.path.basename(p).replace(".csv", "") in filter_names
        )
        for csv_path in csv_files:
            filename = os.path.basename(csv_path).replace(".csv", "")
            if filter_names and filename not in filter_names:
                continue
            tbl_idx += 1
            _process_single_csv(
                csv_path=csv_path,
                filename=filename,
                ctx=ctx,
                tbox_info=tbox_info,
                class_prop_mapping=class_prop_mapping,
                obj_props=obj_props,
                available_classes=available_classes,
                master_instance_uris=master_instance_uris,
                writer=w,
                prefetched=prefetched,
                prefetch_limit_bytes=prefetch_limit_bytes,
                provenance_ndjson_fh=provenance_ndjson_fh,
                table_index=tbl_idx,
                table_total=tbl_total,
                master_value_index=master_value_index,
                fuzzy_config=fuzzy_config,
                dict_contract=dict_contract,
            )

        # 7. PROV-O CSV provenance + flush + FK backfill
        _emit_csv_provenance(g, w, abox_uri, ctx, tbox_ttl)
        w.flush()
        _backfill_fk_target_exists(g, ctx, master_instance_uris)

        # 8. 경고 로그 (실패 테이블 / 중복 PK / 미검증 FK)
        if ctx.failed_tables:
            logger.warning(
                "A-Box 생성 중 %d개 테이블 실패: %s",
                len(ctx.failed_tables),
                [ft["table"] for ft in ctx.failed_tables],
            )
        if sum(1 for c in ctx.duplicate_pk_uris.values() if c > 1) > 0:
            logger.warning(
                "PK 중복 %d건 감지 — 동일 URI 인스턴스의 DP 값이 덮어씌워짐",
                sum(1 for c in ctx.duplicate_pk_uris.values() if c > 1),
            )
        if ctx.unverified_fk_targets:
            logger.warning(
                "FK target 미검증 — CSV 데이터에 없는 타깃 클래스: %s",
                ", ".join(
                    f"{cls}({cnt}건)"
                    for cls, cnt in ctx.unverified_fk_targets.items()
                ),
            )

        # 9. SHACL autofix
        validation_stats = _apply_abox_shacl_autofix(g, tbox_ttl, ctx)

        # 10. NDJSON 닫고 finalize
        if provenance_ndjson_fh is not None:
            provenance_ndjson_fh.close()
            logger.info("A-Box provenance NDJSON 저장: %s", provenance_ndjson_path)

        duration = time.monotonic() - start_time
        from tools.fk_matching import get_and_clear_stats as _fk_get_stats
        result = _finalize_abox_outputs(
            g=g,
            ctx=ctx,
            tbox_ttl=tbox_ttl,
            common_dp_stats=common_dp_stats,
            tbox_info=tbox_info,
            master_instance_uris=master_instance_uris,
            validation_stats=validation_stats,
            duration_seconds=duration,
            fk_match_stats=_fk_get_stats(),
        )
        return success_response(result)

    except Exception as e:
        return error_response(e, logger=logger)
