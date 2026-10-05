from __future__ import annotations

import json
import os

from rdflib import Graph, Namespace

from domain.rules_paths import RULES_ROOT, rules_path

# ── 도메인 설정 로드 ──────────────────────────────
# 환경변수 DOMAIN_CONFIG_PATH 로 외부 설정 주입 가능.
# 미지정 시 rules/domain/domain_config.json 사용 (기본 프로젝트 설정).
_RULES_DIR = RULES_ROOT
_DEFAULT_CONFIG_PATH = rules_path("domain_config.json", base=_RULES_DIR)
_DOMAIN_CONFIG_PATH = os.getenv("DOMAIN_CONFIG_PATH") or _DEFAULT_CONFIG_PATH

try:
    with open(_DOMAIN_CONFIG_PATH, encoding="utf-8") as _f:
        DOMAIN_CONFIG = json.load(_f)
except FileNotFoundError as exc:
    raise RuntimeError(
        f"도메인 설정 파일을 찾을 수 없습니다: {_DOMAIN_CONFIG_PATH}. "
        f"DOMAIN_CONFIG_PATH 환경변수로 경로를 지정하거나 {_DEFAULT_CONFIG_PATH} 를 생성하세요."
    ) from exc
except json.JSONDecodeError as _e:
    raise RuntimeError(
        f"도메인 설정 JSON 파싱 실패: {_DOMAIN_CONFIG_PATH} — {_e}"
    ) from _e

_ns_cfg = DOMAIN_CONFIG.get("namespace", {})
if not _ns_cfg:
    raise RuntimeError(f"도메인 설정에 'namespace' 키가 없습니다: {_DOMAIN_CONFIG_PATH}")

# ── URI 문자열 상수 (domain_config.json 기반) ─────
# 도메인-중립 이름이 정식 이름. 신규 코드는 DOMAIN_NS / DOMAIN_INST_NS /
# DOMAIN_NS_OBJ / DOMAIN_INST_NS_OBJ 를 사용할 것.
# 아래 STEEL* alias 는 외부 호출자 호환 목적으로만 남아있는 deprecated 심볼 —
# 새 코드에서 import 하지 말 것. 정본은 아래 DOMAIN_NS 계열이다.
DOMAIN_NS = _ns_cfg.get("class_ns", "")
DOMAIN_INST_NS = _ns_cfg.get("instance_ns", "")
if not DOMAIN_NS or not DOMAIN_INST_NS:
    raise RuntimeError(f"도메인 설정에 class_ns/instance_ns가 필요합니다: {_DOMAIN_CONFIG_PATH}")
ONTOLOGY_URI = _ns_cfg.get("ontology_uri", DOMAIN_NS.rstrip("#"))
NS_PREFIX = _ns_cfg.get("prefix", "ex")
NS_INST_PREFIX = _ns_cfg.get("instance_prefix", "ex-inst")

# Deprecated alias — 외부 호출자 호환 목적. 새 코드는 DOMAIN_NS / DOMAIN_INST_NS 사용.
STEEL = DOMAIN_NS
STEEL_INST = DOMAIN_INST_NS

#: IOF 는 Core / Maintenance / SupplyChain 을 **하나의 공용 네임스페이스** 에
#: 선언한다 — ``/ontology/construct/``. 모듈별로 나뉜 IRI 는 실재하지 않는다.
#:
#: 실측 (2026-08-18): 리포에 동봉된 ``Core.rdf`` / ``Maintenance.rdf`` /
#: ``SupplyChain.rdf`` 의 subject 를 세면 **전부** ``/ontology/construct/`` 다
#: (Core 143 / Maintenance 23 / SupplyChain 127; 모듈별 IRI 는 온톨로지 헤더 1건씩).
#: 예전 값(``/ontology/core/Core/`` 등)으로는 T-Box 가 참조하는 IOF 용어 24개 중
#: **3개만** 해소됐고(그 3개도 헤더 IRI), 나머지 21개는 공리 없는 고립 리프였다.
#: 그래서 ``owl:imports`` 3건이 한 트리플도 로드하지 못했고 HermiT 의 침묵은
#: "일관됨" 이 아니라 **공리가 없어서 얻은 침묵** 이었다
#: (step_12_intermediate_abstract.py 주석이 이미 기술했으나 코드에 반영되지 않았다).
#:
#: 교정 후 실측: 참조 해소 3/24 → **18/24**, IOF 병합 시 **7,529 트리플** 이 실제로
#: 로드되고 그 15개 IOF 클래스 전부가 BFO 상위 공리를 갖는다. HermiT 는
#: consistent=true / unsat 0 (step_12 주석이 예고한 SupplierMaster unsat 은 없었다).
#:
#: 세 상수를 유지하는 이유: prefix (``iof-core:`` / ``iof-maint:`` / ``iof-scro:``)
#: 는 SPARQL·프롬프트·설정에 이미 퍼져 있고 **의미 구분 라벨** 로 쓰인다. IRI 만
#: 같은 값으로 모은다 (RDF 상 동일 네임스페이스이므로 해석 결과가 같다).
#: IOF 가 발행하는 모든 IRI 의 공통 접두사. IOF 여부는 이 접두사로 판정한다.
#: ``"industrialontologies.org" in uri`` 같은 부분 문자열 검사는 경로나 쿼리에 같은
#: 문자열이 들어간 임의 IRI 까지 IOF 로 분류한다.
IOF_IRI_ROOT = "https://spec.industrialontologies.org/"
_IOF_CONSTRUCT = IOF_IRI_ROOT + "ontology/construct/"
IOF_CORE = _IOF_CONSTRUCT
IOF_MAINT = _IOF_CONSTRUCT
IOF_SCRO = _IOF_CONSTRUCT

#: ``owl:imports`` 대상 = IOF **온톨로지** IRI (용어 네임스페이스와 다르다).
#:
#: 위 상수는 용어 IRI 의 네임스페이스이고, 이것은 문서 자체의 이름이다. 동봉
#: 파일의 ``owl:Ontology`` 선언과 정확히 일치하므로 (실측 2026-08-18:
#: ``Core.rdf`` → ``…/core/Core/``, ``Maintenance.rdf`` → ``…/maintenance/Maintenance/``,
#: ``SupplyChain.rdf`` → ``…/supplychain/SupplyChain/``) 추론기가 오프라인에서도
#: ``onto_path`` 로 해석한다.
#:
#: **용어 네임스페이스를 imports 에 넣으면 안 된다.** 그 IRI 는 어떤 파일의 온톨로지
#: 선언도 아니어서 owlready2 가 네트워크로 나가고, 응답이 HTML 리다이렉트(307)라
#: HermiT 이 "Cannot download" 로 **전체 검증을 포기한다** — 검증이 사라지는 침묵이다.
IOF_ONTOLOGY_IRIS: tuple[str, ...] = (
    "https://spec.industrialontologies.org/ontology/core/Core/",
    "https://spec.industrialontologies.org/ontology/maintenance/Maintenance/",
    "https://spec.industrialontologies.org/ontology/supplychain/SupplyChain/",
)
PROV = "http://www.w3.org/ns/prov#"
DCTERMS = "http://purl.org/dc/terms/"
SKOS = "http://www.w3.org/2004/02/skos/core#"
BFO = "http://purl.obolibrary.org/obo/"

# ── rdflib Namespace 객체 ─────────────────────────
DOMAIN_NS_OBJ = Namespace(DOMAIN_NS)
DOMAIN_INST_NS_OBJ = Namespace(DOMAIN_INST_NS)
IOF_CORE_NS = Namespace(IOF_CORE)
IOF_MAINT_NS = Namespace(IOF_MAINT)
IOF_SCRO_NS = Namespace(IOF_SCRO)
PROV_NS = Namespace(PROV)

# ── 외래 prefix 단일 레지스트리 ───────────────────
# prefixed name (``iof-core:MaterialArtifact``) 을 IRI 로 해석하는 **유일한** 출처.
# 이전에는 같은 표가 7곳에 흩어져 있었고 (bind_namespaces / SPARQL_PREFIXES /
# multi_agent_tbox._PREFIX_MAP / jury_fixes prefix_map / step_30._EXTRA_PREFIXES /
# tbox_generation._prefix_preamble / semantic_dictionary metadata) 서로 내용이 달라,
# 한쪽이 아는 prefix 를 다른 쪽이 몰라 **외래 IRI 를 도메인 클래스로 뭉개는** 사고가
# 났다 (2026-08-09 실측: IOF 참조 42 트리플이 미선언 도메인 클래스로 소실).
#
# 도메인 prefix 는 **의도적으로 제외** — 호출부가 NS_PREFIX 정확 일치를 먼저
# 판정해야 하며, 이 표에 넣으면 우선순위가 흐려진다 (resolve_entity_name 참조).
FOREIGN_PREFIXES: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "dcterms": DCTERMS,
    "skos": SKOS,
    "prov": PROV,
    "iof-core": IOF_CORE,
    "iof-maint": IOF_MAINT,
    "iof-scro": IOF_SCRO,
    "obo": BFO,
}

#: 같은 IRI 를 여러 prefix 가 가리킬 때 **직렬화에 쓸 정본 라벨**.
#:
#: rdflib 의 namespace manager 는 IRI → prefix 를 1:1 로만 들고 있어, 마지막
#: bind 가 이긴다. IOF 세 모듈은 실제로 한 네임스페이스이므로 그대로 두면
#: ``MaterialArtifact`` 가 ``iof-scro:`` 로 직렬화된다 (의미 오해 + dict 순서에
#: 따라 산출물 diff 가 흔들림). ``bind_namespaces`` 가 마지막에 이 표를 다시
#: 적용해 라벨을 고정한다. 도메인-중립: 값은 위 상수에서 파생한다.
_CANONICAL_SERIALIZATION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("iof-core", IOF_CORE),
)

# Deprecated alias — 외부 호출자 호환 목적. 새 코드는 DOMAIN_NS_OBJ / DOMAIN_INST_NS_OBJ 사용.
STEEL_NS = DOMAIN_NS_OBJ
STEEL_INST_NS = DOMAIN_INST_NS_OBJ

# ── SPARQL PREFIX 블록 (동적 생성) ────────────────
# 외래 prefix 는 FOREIGN_PREFIXES 에서 파생 — 표를 두 번 쓰면 한쪽만 갱신돼
# "쿼리는 아는데 해석기는 모르는" prefix 가 생긴다 (실제로 iof-scro 가 그랬다).
SPARQL_PREFIXES = (
    f"PREFIX {NS_PREFIX}: <{DOMAIN_NS}>\n"
    f"PREFIX {NS_INST_PREFIX}: <{DOMAIN_INST_NS}>\n"
    + "".join(f"PREFIX {p}: <{ns}>\n" for p, ns in FOREIGN_PREFIXES.items())
)


def prepend_prefixes(query: str) -> str:
    """쿼리에 PREFIX 선언이 없으면 표준 PREFIX를 추가한다."""
    import re as _re
    if _re.search(r'^\s*PREFIX\s+', query, _re.IGNORECASE | _re.MULTILINE):
        return query
    return SPARQL_PREFIXES + "\n" + query


def sanitize_sparql_value(value: str) -> str:
    """SPARQL 문자열 리터럴 보간에 안전하도록 특수 문자를 이스케이프한다.

    **전제 조건**: 결과값이 SPARQL 문자열 리터럴("...") 내부에 삽입되는 경우에만
    사용하세요. URI(<...>) 내부나 식별자 자리에 넣어서는 안 됩니다.

    이스케이프 범위 (SPARQL 1.1 ``STRING_LITERAL2`` 규칙):
    - backslash / double quote: 리터럴 경계를 벗어나지 못하게 한다.
    - newline / carriage return / tab / backspace / form feed: 제어문자를 ECHAR 로 바꾼다.
    - NULL 문자(\\x00): 쿼리 파서 중단 방지를 위해 제거한다.

    작은따옴표와 ``<`` ``>`` 는 큰따옴표 리터럴 안에서 의미가 없으므로 그대로 둔다.
    rdflib 파서는 큰따옴표 리터럴 안의 ``\\'`` 를 거부하고, ``\\uXXXX`` 코드포인트
    이스케이프는 실행 전 egress 가드 (``domain.sparql_templates.reject_sparql_egress``)
    가 파서 간 해석 차이 때문에 거부한다. 따라서 이 함수는 두 표기를 만들지 않는다.
    """
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("\n", "\\n")
    value = value.replace("\r", "\\r")
    value = value.replace("\t", "\\t")
    value = value.replace("\b", "\\b")
    value = value.replace("\f", "\\f")
    value = value.replace("\x00", "")
    return value


def bind_namespaces(graph: Graph) -> None:
    """rdflib Graph에 도메인 + 외래 네임스페이스를 바인딩한다.

    ``replace=True`` 가 필수다. rdflib 은 새 Graph 에 29개 prefix 를 미리
    바인딩해 두는데 (``time`` / ``org`` / ``schema`` / ``prov`` / ``skos`` /
    ``dcterms`` / ``geo`` / ``qb`` / ``void`` / ``sosa`` / ``brick`` / ``dc``
    등), ``Graph.bind`` 는 기본값이 ``replace=False`` 라 **같은 이름이 이미 있으면
    조용히 무시**한다. 도메인 prefix 가 그중 하나와 겹치면 (예:
    ``domain_config.json`` 의 prefix 가 ``time``) 도메인 바인딩이 실패하고,
    그래프 바인딩을 신뢰하는 해석기가 도메인 이름을 **외래 네임스페이스로**
    보내버린다 — 지금 고치는 버그의 정확한 역방향이다.
    """
    graph.bind(NS_PREFIX, DOMAIN_NS_OBJ, replace=True)
    graph.bind(NS_INST_PREFIX, DOMAIN_INST_NS_OBJ, replace=True)
    # 모든 prefix 를 bind 한다 — 파싱 시 ``iof-maint:`` / ``iof-scro:`` 를 써서
    # 저작된 TTL·SPARQL 을 해석해야 하고, 그 표기는 설정·프롬프트에 퍼져 있다.
    for prefix, ns in FOREIGN_PREFIXES.items():
        graph.bind(prefix, Namespace(ns), replace=True)
    # **직렬화 라벨은 정본으로 되돌린다.** IOF 세 모듈은 실제로 하나의
    # 네임스페이스(``/ontology/construct/``)이고, rdflib 은 같은 IRI 에 여러
    # prefix 가 걸리면 ``replace=True`` 순차 bind 의 **마지막** 것만 남긴다.
    # 그대로 두면 ``MaterialArtifact`` 가 ``iof-scro:`` 로 직렬화돼 의미가 오해되고,
    # dict 순서가 바뀌면 산출물 diff 가 흔들린다. 마지막에 정본을 다시 bind 해
    # 라벨을 고정한다 (iof-core 가 상위 개념을 담는 정본 모듈이다).
    for prefix, ns in _CANONICAL_SERIALIZATION_PREFIXES:
        graph.bind(prefix, Namespace(ns), replace=True)
