"""공용 그래프 유틸리티 — 페이지네이션 쿼리, BFS 탐색, 텍스트 스캔 패턴"""
from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from collections.abc import Generator

from rdflib import OWL, RDF, RDFS, Graph, URIRef

logger = logging.getLogger(__name__)


def domain_predicate_pattern(
    names: set[str] | None = None,
) -> re.Pattern[str] | None:
    """도메인 프로퍼티를 **텍스트로** 세기 위한 정규식. 설정에서 prefix 를 읽는다.

    A-Box 는 수백 MB 라 rdflib 파싱 없이 정규식으로 이름 등장 횟수만 세는 곳이
    여럿 있다. 그 패턴은 두 직렬화를 **모두** 인식해야 한다:

    - 접두형 Turtle (``med:patientId``) — 이 리포의 A-Box 생성기 출력
    - 완전 IRI (``<http://.../patientId>``) — N-Triples / GraphDB export

    prefix 를 리터럴로 박으면 다른 도메인에서 스캔이 **조용히 0건** 을 반환하고,
    호출부가 그 0 을 "미사용" 으로 해석해 실제로 값이 있는 프로퍼티를 지우거나
    canonical 선택을 뒤집는다 (2026-08-08: step_12f 와 ``_load_abox_op_usage``
    양쪽에서 같은 버그가 발견됐다 — 그래서 이 헬퍼로 단일화한다).

    Args:
        names: 셀 대상 local name 집합. ``None`` 이면 임의의 이름을 캡처한다.

    Returns:
        컴파일된 패턴. 캡처 그룹 1 이 local name.
        네임스페이스 설정이 비어 판정이 불가능하면 ``None`` — 호출부는 이때
        추측하지 말고 작업을 보류해야 한다.
    """
    from domain.namespaces import DOMAIN_NS, NS_PREFIX

    alternatives: list[str] = []
    if DOMAIN_NS:
        alternatives.append(re.escape(f"<{DOMAIN_NS}"))
    if NS_PREFIX:
        alternatives.append(rf"\b{re.escape(NS_PREFIX)}:")
    if not alternatives:
        logger.warning(
            "도메인 네임스페이스 설정이 비어 predicate 텍스트 스캔 불가 — 호출부는 판정을 보류해야 한다",
        )
        return None

    if names:
        captured = "(" + "|".join(re.escape(n) for n in sorted(names)) + ")"
    else:
        captured = r"([A-Za-z_][A-Za-z0-9_]*)"
    return re.compile(rf"(?:{'|'.join(alternatives)}){captured}\b")


def strip_iri_brackets(text: str) -> tuple[str, bool]:
    """``<IRI>`` → ``(IRI, True)``. 그 외는 ``(원본, False)``.

    ## 왜 필요한가

    Turtle 에서 ``<...>`` 는 **IRI 를 뜻하는 정식 문법** 이다. LLM 은 DSL 값에
    그 관용을 그대로 쓴다 (``"on_property": "<https://…/Core/followedBy>"``).
    그런데 두 해석기가 브래킷을 몰라 각자 다르게 망가졌다 (2026-08-19 실측):

    - :func:`resolve_entity_name` — ``<https`` 를 **미등록 prefix** 로 읽고 콜론
      앞을 버려 ``<DOMAIN_NS>//spec…/followedBy>`` 라는 깨진 IRI 를 만든다.
      rdflib 이 ``Invalid IRI code point '>'`` 로 거부 → **지시가 폐기된다.**
    - :func:`parse_object_term` — 완전 IRI 판정(``startswith("http://"…)``)에
      걸리지 않아 조용히 **평문 Literal** 이 된다. 경고조차 없다.

    피해: 한 실행의 R4 에서 버려진 Jury 지시 7건 중 6건이 이것이고, 그중 4건이
    SME 가 4라운드 반복 요구한 **공정 흐름 체인** (고로→제강→연주→압연의
    ``followedBy``/``precededBy`` someValuesFrom) 이었다. 배포 T-Box 에
    ``followedBy`` 는 선언만 남고 ``owl:onProperty`` 사용 **0건** — 리뷰어는 매
    라운드 같은 결함을 다시 지적했고 veto 가 풀리지 않았다. 서버 로그 누적 136건.

    ``resolve_entity_name`` docstring 의 "미등록 prefix 는 LLM 오타" 가정이 여기서
    틀린다 — ``<https`` 는 **오타가 아니라 정상 문법** 이므로 prefix 폴백이 아니라
    브래킷 해제로 처리해야 한다.

    ## 판정 조건 (좁게 잡는다)

    양끝이 ``<``/``>`` 이고, 내부에 공백·꺾쇠가 없을 때만 해제한다. 리터럴로
    의도된 ``"< 5mm"`` 같은 값을 IRI 로 승격시키지 않기 위한 보수 조건이다.
    내부가 비면 (``<>``) 해제하되 빈 문자열을 돌려주므로 호출부의 기존 빈 값
    검증이 그대로 발동한다.

    Returns:
        ``(해제된 문자열, 해제했는가)``. 두 번째 값이 ``True`` 면 **Turtle 상
        IRI 가 확정** 이므로 호출부는 리터럴 폴백을 타지 않아야 한다.
    """
    if not isinstance(text, str):
        return text, False
    stripped = text.strip()
    if len(stripped) < 2 or stripped[0] != "<" or stripped[-1] != ">":
        return text, False
    inner = stripped[1:-1]
    if any(ch in inner for ch in " \t\n<>"):
        return text, False
    return inner, True


def resolve_entity_name(raw: str, graph: Graph | None = None) -> URIRef:
    """prefixed name / bare local name / 완전 IRI → ``URIRef``. **유일한** 해석기.

    LLM 이 반환하는 이름은 계약상 bare local name 이지만 실제로는 배포 prefix
    (``steel:Equipment``), 외래 온톨로지 prefix (``iof-core:MaterialArtifact``),
    완전 IRI 가 섞여 온다. 이 세 형태를 각자 다르게 처리하던 사본이 리포에 11곳
    있었고, 그중 6곳이 **콜론 앞을 판별 없이 버려** 외래 IRI 를 도메인 IRI 로
    뭉갰다 (2026-08-09 실측: IOF 참조 42 트리플이 미선언 도메인 클래스로 소실 —
    ``iof-core:MaterialArtifact`` 13건이 ``steel:MaterialArtifact`` 가 됐고 그
    클래스는 어디에도 선언돼 있지 않다).

    해석 순서 — **바꾸면 안 된다**:

    0. ``<...>`` 브래킷은 **먼저 벗긴다** (:func:`strip_iri_brackets`). Turtle 에서
       그것은 IRI 표기이므로 prefix 판정에 넘기면 ``<https`` 가 미등록 prefix 로
       읽혀 깨진 IRI 가 된다 (실측: 지시 폐기 136건). 벗긴 뒤에는 아래 순서를
       그대로 탄다 — 브래킷 안이 ``iof-core:X`` 같은 prefixed name 일 수도 있다.
    1. 완전 IRI (``http:`` / ``https:`` / ``urn:`` / ``file:``) → 그대로.
       ``urn:x:y`` 를 prefix ``urn`` 으로 읽으면 안 된다.
    2. 콜론 없음 → 도메인 NS. (계약상 정상 경로)
    3. 콜론 앞이 ``NS_PREFIX`` 와 **정확히 일치** → 도메인 NS.
       ``startswith`` / ``in`` 을 쓰면 안 된다 — prefix ``core`` 가
       ``iof-core:`` 에 부분일치해 외래 이름을 도메인으로 끌어온다 (실측).
       그래프 바인딩보다 **먼저** 판정한다: ``bind_namespaces`` 가
       rdflib 기본 prefix 와 충돌해 실패했을 때 그래프를 먼저 믿으면 도메인
       이름이 외래 NS 로 가는 역방향 사고가 난다.
    4. 콜론 앞이 그래프에 바인딩된 prefix → 그 네임스페이스.
    5. 콜론 앞이 ``FOREIGN_PREFIXES`` 의 prefix → 그 네임스페이스.
       (그래프가 없거나 바인딩이 유실된 경로의 안전망)
    6. 그 외 (미등록 prefix) → prefix 를 벗기고 도메인 NS. **WARNING 을 남긴다.**
       콜론을 보존하는 대안은 ``<DOMAIN_NS + "unbound:Equipment">`` 라는 유령을
       만드는데, 이 유령은 prefix 가 알려지지 않았으므로
       :func:`split_embedded_prefix` 도 복구 대상으로 보지 않는다 — 즉 어떤
       게이트도 못 잡는 미선언 참조가 영구히 남는다. 실측상 미등록 prefix 는
       LLM 오타이고 (2026-08-09 S2 산출물의 유령 prefix 는 전부 도메인/IOF),
       외래 의도가 실재한다면 옳은 해결은 ``FOREIGN_PREFIXES`` 에 등록하는
       것이다. 그래서 조용히 넘기지 않고 로그로 드러낸다.

    ``None`` 을 반환하지 않는다: 호출부 38곳이 결과를 곧바로 ``g.add`` 에 쓰며
    ``None`` 검사가 없어, 센티넬은 "적용된 것처럼 보이지만 안 됨" 이라는 원래
    실패 모드를 되살린다.

    Raises:
        ValueError: ``raw`` 가 문자열이 아니거나 빈 문자열/콜론뿐인 경우. 빈
            local name 은 ``<DOMAIN_NS>`` 자체를 가리키는 또 다른 유령이 된다.
    """
    from domain.namespaces import DOMAIN_NS, FOREIGN_PREFIXES, NS_PREFIX

    if not isinstance(raw, str):
        raise ValueError(f"entity name must be a string, got {type(raw).__name__}")
    name = raw.strip()
    # 0. Turtle IRI 표기 해제 — prefix 판정 **앞** 이어야 한다 (docstring 0번).
    name, _was_bracketed = strip_iri_brackets(name)
    name = name.strip()
    if not name or name == ":":
        raise ValueError(f"empty entity name: {raw!r}")

    if name.startswith(("http://", "https://", "urn:", "file:")):
        return URIRef(name)
    if ":" not in name:
        return URIRef(DOMAIN_NS + name)

    prefix, local = name.split(":", 1)
    if not local:
        raise ValueError(f"entity name has an empty local part: {raw!r}")
    if prefix == NS_PREFIX:
        return URIRef(DOMAIN_NS + local)
    if graph is not None:
        try:
            bound = dict(graph.namespace_manager.namespaces())
        except Exception as exc:  # noqa: BLE001 — 바인딩 조회 실패는 폴백으로
            logger.debug("prefix 바인딩 조회 실패, 정적 표로 폴백: %s", exc)
            bound = {}
        if prefix in bound:
            return URIRef(str(bound[prefix]) + local)
    if prefix in FOREIGN_PREFIXES:
        return URIRef(FOREIGN_PREFIXES[prefix] + local)
    logger.warning(
        "미등록 prefix '%s:' (%s) — prefix 를 벗기고 도메인 NS 로 해석한다. "
        "외래 온톨로지를 의도했다면 domain.namespaces.FOREIGN_PREFIXES 에 등록할 것",
        prefix, name,
    )
    return URIRef(DOMAIN_NS + local)


def split_embedded_prefix(
    iri: str, graph: Graph | None = None,
) -> tuple[str, str] | None:
    """``<DOMAIN_NS + "pfx:Local">`` 유령 IRI → ``(정상 네임스페이스, local)``.

    복구 방향 헬퍼. 해석기(:func:`resolve_entity_name`)가 막지 못한 과거
    산출물이나 외부 유입 TTL 에 남은 유령을 되살릴 때 쓴다.

    **콜론이 있다는 것만으로 유령이라고 판정하면 안 된다**:

    - Turtle 은 local name 에 콜론을 허용한다 (``ex:a:b`` → ``…#a:b``).
    - 인스턴스 IRI 는 timestamp 를 percent-encode 하지 않아 ``…#Proc_P001_
      2025-01-01T00:00:00`` 처럼 콜론이 정당하게 들어간다. 이것을 잘라내면
      A-Box 데이터가 손상된다.

    그래서 **콜론 앞이 알려진 prefix 이름일 때만** 유령으로 본다 (도메인
    prefix / 그래프 바인딩 / ``FOREIGN_PREFIXES``). 인스턴스 네임스페이스는
    아예 대상에서 제외한다.

    Returns:
        ``(namespace_iri, local_name)`` — 복구 대상일 때.
        ``None`` — 복구 대상이 아닐 때 (정상 IRI, 정당한 콜론, 인스턴스 IRI,
        빈 local name). 호출부는 ``None`` 이면 **손대지 않아야** 한다.
    """
    from domain.namespaces import (
        DOMAIN_INST_NS,
        DOMAIN_NS,
        FOREIGN_PREFIXES,
        NS_PREFIX,
    )

    if not isinstance(iri, str) or not DOMAIN_NS or not iri.startswith(DOMAIN_NS):
        return None
    # 인스턴스 NS 가 클래스 NS 의 하위 경로면 (예: …#inst/) 여기서 제외.
    if DOMAIN_INST_NS and iri.startswith(DOMAIN_INST_NS):
        return None
    rest = iri[len(DOMAIN_NS):]
    if ":" not in rest:
        return None
    prefix, local = rest.split(":", 1)
    if not local or not prefix:
        return None
    if prefix == NS_PREFIX:
        return (DOMAIN_NS, local)
    if graph is not None:
        try:
            bound = dict(graph.namespace_manager.namespaces())
        except Exception as exc:  # noqa: BLE001
            logger.debug("prefix 바인딩 조회 실패, 정적 표로 폴백: %s", exc)
            bound = {}
        if prefix in bound:
            return (str(bound[prefix]), local)
    if prefix in FOREIGN_PREFIXES:
        return (FOREIGN_PREFIXES[prefix], local)
    return None


#: ``(파일 서명, 후보 집합) -> 사용된 local name`` 캐시. A-Box 는 500MB+ 라
#: 한 번의 T-Box 개선에서 세 스텝이 각자 스캔하면 비용이 곱해진다.
_ABOX_USED_CACHE: dict[tuple, set[str]] = {}


def abox_used_local_names(candidates: set[str]) -> set[str] | None:
    """A-Box·마스터·tacit 을 스캔해 **실제 쓰이는** local name 집합을 돌려준다.

    프로퍼티/클래스 이름을 지우거나 선언을 억제하기 전에 "이게 실제로 쓰이는가" 를
    묻는 **유일한** 헬퍼. 같은 스캔이 ``step_12f`` / ``step_21b`` / ``step_15`` 에
    필요하고, 사본으로 두면 갈라진다 (이 리포는 텍스트 스캔 사본이 하드코딩 prefix
    때문에 타 도메인에서 조용히 0건을 반환한 사고가 두 번 있었다).

    **tacit 도 스캔한다**: tacit TTL 이 선언하는 관계는 A-Box 재생성과 무관하게
    보존되는 SME 지식이다. 이것을 빼면 실사용 프로퍼티가 "미사용" 으로 읽힌다
    (실측: ``hasSoilImpact`` object term 4,128건이 tacit 에 있는데 A-Box 만 보는
    판정은 0 으로 봤다).

    Returns:
        쓰이는 local name 집합. ``None`` — **판정 불가**(스캔 대상 파일 없음 /
        네임스페이스 미설정 / 읽기 실패). 호출부는 이때 **파괴적 동작을 보류** 해야
        한다. 0건과 판정불가를 구분하지 않으면 "미사용" 오판으로 산 데이터를 잃는다.
    """
    import glob
    import os

    if not candidates:
        return set()

    try:
        from config import ABOX_PATH, SOURCE_DIR
    except Exception as exc:  # noqa: BLE001
        logger.debug("A-Box 경로 설정 조회 실패: %s", exc)
        return None

    paths = [ABOX_PATH, os.path.join(os.path.dirname(ABOX_PATH), "master_data.ttl")]
    try:
        paths.extend(sorted(glob.glob(os.path.join(SOURCE_DIR, "tacit", "*.ttl"))))
    except Exception as exc:  # noqa: BLE001
        logger.debug("tacit 스캔 경로 조회 실패 (A-Box 만 사용): %s", exc)
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        return None

    signature: tuple = tuple(
        (p, os.stat(p).st_mtime, os.stat(p).st_size) for p in existing
    )
    cache_key = (signature, frozenset(candidates))
    if cache_key in _ABOX_USED_CACHE:
        return _ABOX_USED_CACHE[cache_key]

    pattern = domain_predicate_pattern(candidates)
    if pattern is None:
        logger.warning(
            "도메인 네임스페이스 설정이 비어 A-Box 사용 판정 불가 — 호출부는 "
            "파괴적 동작을 보류해야 한다",
        )
        return None

    used: set[str] = set()
    for path in existing:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    used.update(pattern.findall(line))
        except OSError as exc:
            logger.warning("A-Box 스캔 실패 (%s) — 판정 불가로 처리: %s", path, exc)
            return None
        if used == candidates:
            break                      # 전부 사용 중이면 더 읽을 필요 없다
    _ABOX_USED_CACHE[cache_key] = used
    return used


def csv_fk_class_pairs() -> set[tuple[str, str]] | None:
    """CSV FK 컬럼이 실제로 잇는 ``(source_class, target_class)`` 쌍.

    "이 관계가 데이터에 근거가 있는가" 를 묻는 **유일한** 헬퍼. OP 를 새로 만들거나
    근거를 판정하기 전에 이것을 쓴다.

    ``rules/domain/table_class_mapping.json`` 의 권위 매핑으로 테이블명을 클래스명으로
    바꾸고, ``_FK_PATTERNS`` 로 FK 컬럼의 타겟 테이블을 찾는다. 반환값은
    정규화된(밑줄 제거·소문자) 이름 쌍이라 표기 차이에 둔감하다.

    **왜 공용인가**: 같은 도출이 ``multi_agent_tbox._csv_fk_pairs`` (S2 CQ 스켈레톤
    게이트) 와 ``step_15`` (cross-domain OP 주입) 양쪽에 필요하다. 사본으로 두면
    한쪽만 고쳐져 갈라진다 — 이 리포는 그 계열의 사고가 반복됐다
    (``abox_used_local_names`` docstring 참조).

    Returns:
        정규화된 클래스명 쌍 집합. 방향은 CSV 가 가진 그대로 (source→target).
        ``None`` — **판정 불가** (CSV 디렉터리 없음 / 읽기 실패 / 매핑 없음).
        호출부는 이때 게이트를 적용하지 않아야 한다. 0건과 판정불가를 혼동하면
        정당한 주입을 전부 막는다.
    """
    import csv as _csv
    import glob as _glob
    import os as _os

    try:
        import config
        from tools.ontology_quality import _FK_PATTERNS
    except Exception as exc:  # noqa: BLE001
        logger.debug("CSV FK 쌍 조회 실패 (설정/패턴 로드): %s", exc)
        return None

    csv_dir = getattr(config, "SOURCE_RAWDATA_DIR", None)
    if not (csv_dir and _os.path.isdir(csv_dir)):
        return None
    files = sorted(_glob.glob(_os.path.join(csv_dir, "*.csv")))
    if not files:
        return None

    # 테이블명 → 클래스명 권위 매핑. 없으면 테이블명을 그대로 쓴다 (정규화가
    # 표기 차이를 흡수하므로 대개 일치한다).
    table_to_class: dict[str, str] = {}
    try:
        # 정본 로더에 위임. 예전 사본은 존재하지 않는 ``config.RULES_DIR`` 를 찾다가
        # **상대 경로 "rules"** 로 폴백했으므로 리포 루트가 아닌 cwd 에서는 조용히
        # 빈 매핑이 됐다. 로더가 prefix 벗기기·중첩 dict·캐시 무효화를 한 곳에서 한다.
        from domain.table_mapping import load_table_class_mapping

        table_to_class = dict(load_table_class_mapping())
    except Exception as exc:  # noqa: BLE001 — 매핑 없으면 테이블명 폴백
        logger.debug("table_class_mapping 로드 실패 (테이블명 폴백): %s", exc)

    def _norm(x: str) -> str:
        return x.replace("_", "").lower()

    pairs: set[tuple[str, str]] = set()
    for path in files:
        table = _os.path.basename(path)[:-4]
        src = table_to_class.get(table, table)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                header = next(_csv.reader(fh), [])
        except OSError as exc:
            logger.debug("CSV 헤더 읽기 실패 (%s): %s", path, exc)
            continue
        for col in header:
            target_table = _FK_PATTERNS.get(col.lower().replace("_", ""))
            if not target_table:
                continue
            tgt = table_to_class.get(target_table, target_table)
            if _norm(tgt) != _norm(src):
                pairs.add((_norm(src), _norm(tgt)))
    return pairs


def csv_fk_pair_columns() -> dict[tuple[str, str], str] | None:
    """``(source_class, target_class)`` → **그 관계를 만든 CSV FK 컬럼명**.

    ``csv_fk_class_pairs`` 와 같은 도출인데 컬럼명을 버리지 않는다. 쌍만 필요한
    호출부는 계속 그쪽을 쓰고, **근거를 기록** 해야 하는 쪽이 이것을 쓴다
    (OP 의 ``dcterms:source`` 역기록).

    같은 쌍을 여러 컬럼이 만들면 사전순 첫 컬럼을 남긴다 — 결정적이어야
    재실행 간 T-Box 가 흔들리지 않는다.

    Returns:
        정규화된 쌍 → 원본 헤더 문자열. ``None`` 은 ``csv_fk_class_pairs`` 와
        같은 의미로 **판정 불가** (호출부는 게이트를 적용하지 않는다).
    """
    import csv as _csv
    import glob as _glob
    import os as _os

    try:
        import config
        from tools.ontology_quality import _FK_PATTERNS
    except Exception as exc:  # noqa: BLE001
        logger.debug("CSV FK 컬럼 조회 실패 (설정/패턴 로드): %s", exc)
        return None

    csv_dir = getattr(config, "SOURCE_RAWDATA_DIR", None)
    if not (csv_dir and _os.path.isdir(csv_dir)):
        return None
    files = sorted(_glob.glob(_os.path.join(csv_dir, "*.csv")))
    if not files:
        return None

    table_to_class: dict[str, str] = {}
    try:
        # 정본 로더에 위임. 예전 사본은 존재하지 않는 ``config.RULES_DIR`` 를 찾다가
        # **상대 경로 "rules"** 로 폴백했으므로 리포 루트가 아닌 cwd 에서는 조용히
        # 빈 매핑이 됐다. 로더가 prefix 벗기기·중첩 dict·캐시 무효화를 한 곳에서 한다.
        from domain.table_mapping import load_table_class_mapping

        table_to_class = dict(load_table_class_mapping())
    except Exception as exc:  # noqa: BLE001
        logger.debug("table_class_mapping 로드 실패 (테이블명 폴백): %s", exc)

    def _norm(x: str) -> str:
        return x.replace("_", "").lower()

    out: dict[tuple[str, str], str] = {}
    for path in files:
        table = _os.path.basename(path)[:-4]
        src = table_to_class.get(table, table)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                header = next(_csv.reader(fh), [])
        except OSError as exc:
            logger.debug("CSV 헤더 읽기 실패 (%s): %s", path, exc)
            continue
        for col in sorted(header):
            target_table = _FK_PATTERNS.get(col.lower().replace("_", ""))
            if not target_table:
                continue
            tgt = table_to_class.get(target_table, target_table)
            if _norm(tgt) == _norm(src):
                continue
            key = (_norm(src), _norm(tgt))
            out.setdefault(key, col)
            # 역방향도 같은 컬럼이 근거다 (inverseOf 쌍).
            out.setdefault((_norm(tgt), _norm(src)), col)
    return out


def csv_fk_pair_all_columns() -> dict[tuple[str, str], list[str]] | None:
    """``(source_class, target_class)`` → **그 쌍을 잇는 CSV FK 컬럼 전부**.

    :func:`csv_fk_pair_columns` 는 같은 쌍을 여러 컬럼이 만들 때 사전순 첫 컬럼만
    남긴다 (결정성 목적). 그런데 **한 쌍을 두 컬럼이 서로 다른 의미로** 잇는 경우가
    있고, 그때 첫 컬럼을 모든 OP 에 적으면 **틀린 출처**가 된다.

    실측 (2026-08-30 배포 T-Box): ``Transportation.csv`` 에
    ``Origin_Warehouse`` / ``Destination_Warehouse`` 두 컬럼이 모두
    ``Transportation → WarehouseMaster`` 를 잇는다. 사전순 첫 컬럼이
    ``Destination_Warehouse`` 이므로 **Origin 방향 OP 3개**가
    ``dcterms:source "Destination_Warehouse"`` 를 갖게 됐다::

        transportationHasOriginWarehouse       source=Destination_Warehouse  ← 틀림
        isOriginWarehouseOfTransportation      source=Destination_Warehouse  ← 틀림
        warehouseIsOriginOfTransportation      source=Destination_Warehouse  ← 틀림

    잘못된 provenance 는 올바른 것과 **게이트 상 구분이 안 된다** (step_12e 는
    존재 여부만 세고, 컬럼이 CSV 에 실재하므로 resolvable 로도 집계된다). 그리고
    A-Box 는 이 값으로 컬럼을 찾으므로 출발/도착이 뒤바뀔 위험이 있다.

    호출부는 이 목록에서 **OP 이름과 가장 잘 맞는 컬럼**을 골라야 한다
    (``step_15d`` 의 ``_pick_column_for_op``).

    Returns:
        정규화된 쌍 → 원본 헤더 리스트 (사전순). ``None`` 은 판정 불가.
    """
    import csv as _csv
    import glob as _glob
    import os as _os

    try:
        import config
        from tools.ontology_quality import _FK_PATTERNS
    except Exception as exc:  # noqa: BLE001
        logger.debug("CSV FK 컬럼 조회 실패 (설정/패턴 로드): %s", exc)
        return None

    csv_dir = getattr(config, "SOURCE_RAWDATA_DIR", None)
    if not (csv_dir and _os.path.isdir(csv_dir)):
        return None
    files = sorted(_glob.glob(_os.path.join(csv_dir, "*.csv")))
    if not files:
        return None

    table_to_class: dict[str, str] = {}
    try:
        from domain.table_mapping import load_table_class_mapping

        table_to_class = dict(load_table_class_mapping())
    except Exception as exc:  # noqa: BLE001
        logger.debug("table_class_mapping 로드 실패 (테이블명 폴백): %s", exc)

    def _norm(x: str) -> str:
        return x.replace("_", "").lower()

    out: dict[tuple[str, str], list[str]] = {}

    def _add(key: tuple[str, str], col: str) -> None:
        bucket = out.setdefault(key, [])
        if col not in bucket:
            bucket.append(col)

    for path in files:
        table = _os.path.basename(path)[:-4]
        src = table_to_class.get(table, table)
        try:
            with open(path, encoding="utf-8-sig") as fh:
                header = next(_csv.reader(fh), [])
        except OSError as exc:
            logger.debug("CSV 헤더 읽기 실패 (%s): %s", path, exc)
            continue
        for col in sorted(header):
            target_table = _FK_PATTERNS.get(col.lower().replace("_", ""))
            if not target_table:
                continue
            tgt = table_to_class.get(target_table, target_table)
            if _norm(tgt) == _norm(src):
                continue
            _add((_norm(src), _norm(tgt)), col)
            _add((_norm(tgt), _norm(src)), col)
    return out


def literal_values_equal(a, b) -> bool:
    """두 리터럴이 **같은 값** 인가 — datatype 표기 차이를 흡수한다.

    RDF 1.1 에서 ``"x"`` 와 ``"x"^^xsd:string`` 은 **동일한 값** 이지만 rdflib 의
    ``==`` 는 datatype 이 ``None`` vs ``xsd:string`` 이라 다르다고 판정한다.
    이 리포의 기본 store 는 Oxigraph 이고 그것은 리터럴을 항상 ``xsd:string`` 을
    붙여 되돌려주므로, ``Literal(val)`` 과 비교하면 **영구히 불일치** 한다.

    실측 (2026-08-12): ``_inject_ontoclean_annotations`` 의 ``current == [lit]``
    가 매 실행 실패해 63개 클래스의 주석을 지우고 다시 쓰면서
    ``added=283 / replaced=283`` 을 보고했다 — 실제 그래프 변경은 **0** 이다
    (isomorphic 동일). 3회 연속 같은 수를 보고하므로 "무언가 고쳤다" 로 읽히지만
    영구 no-op 이고, 그 사이 진짜 변경은 카운터에 묻힌다. Memory store 에서는
    1회 후 0 이 나와 테스트가 이 결함을 놓쳤다.

    비교 규칙: 언어 태그가 있으면 태그까지 일치해야 하고(``"x"@ko`` ≠ ``"x"@en``),
    없으면 문자열 값과 "plain 또는 xsd:string" 여부로 판정한다. 그 외 datatype
    (``xsd:boolean`` 등) 은 표기가 의미를 바꾸므로 엄격히 비교한다.
    """
    from rdflib import Literal
    from rdflib.namespace import XSD

    if not (isinstance(a, Literal) and isinstance(b, Literal)):
        return a == b
    if (a.language or None) != (b.language or None):
        return False
    if str(a) != str(b):
        return False
    plain = (None, XSD.string)
    if a.datatype in plain and b.datatype in plain:
        return True
    return a.datatype == b.datatype


def ops_linking(graph: Graph, domain_uri, range_uri) -> set:
    """``(domain, range)`` **순서쌍** 을 잇는 기존 ObjectProperty 집합.

    같은 쌍에 동의어 OP 를 또 만들지 않으려는 멱등 가드용. CSV FK 컬럼은 하나뿐이라
    A-Box 는 동의어 중 **하나만** 채우고 나머지는 값 0건으로 남는다 — "빈 관계로
    질의하면 0건이 정답처럼" 반환되는 함정이 된다 (2026-08-11 실측: 중복 OP 42개,
    step_22d 게이트 허용치 0).

    **정확한 쌍 일치만 본다. subsumption 을 따라가지 않는다** — 배포 T-Box 에
    ``rdfs:domain owl:Thing`` OP 가 62개 있어 상위 클래스를 타면 거의 모든 쌍이
    겹치는 것으로 오판한다.

    방향을 구분하므로 **inverseOf 쌍은 막지 않는다**: ``hasX(A→B)`` 가 있어도
    ``isXOf(B→A)`` 조회는 빈 집합을 돌려준다. 단 ``domain == range`` 인
    self-referential OP 는 역방향이 같은 쌍이라 호출부가 별도로 판단해야 한다.
    """
    from rdflib import OWL, RDF, RDFS

    return {
        op for op in graph.subjects(RDF.type, OWL.ObjectProperty)
        if domain_uri in set(graph.objects(op, RDFS.domain))
        and range_uri in set(graph.objects(op, RDFS.range))
    }


def is_ancestor_of(graph: Graph, candidate, node) -> bool:
    """``candidate`` 가 ``node`` 의 ``rdfs:subClassOf`` 조상인가.

    ``node ⊑ candidate`` 를 추가하기 **전에** 물어야 하는 질문이다. 참이면 그
    추가는 순환을 만든다 (``candidate ⊑ … ⊑ node ⊑ candidate``).

    ## 왜 공용 헬퍼인가

    실측 (2026-08-23 S3): ``_apply_odp_abstract_groups`` 의
    ``child_name_patterns`` 는 부분 문자열 매칭이라 **상위 개념 자신을** 자식으로
    끌어온다 — 패턴 ``"Monitoring"`` 이 ``MonitoringManagement`` 를 매칭했고 그
    클래스는 이미 ``EnvironmentalMonitoring`` 의 부모였다. 그 결과 순환이 생겨
    ``check_quality_rules`` 가 **critical 12건** (한 SCC 를 조상마다 중복 보고)
    으로 S4 를 FAIL 시켰다.

    같은 판정이 계층을 만드는 다른 스텝(step_12 중간 추상 클래스 / step_14 서브그룹
    / jury 의 ``add_class_hierarchy``)에도 필요하다. 사본으로 두면 한 곳만 고쳐지고
    다른 곳이 다음 실행에서 같은 순환을 다시 만든다 — 이 리포에서 반복된 유형이다
    (``is_named_domain_class`` docstring 참조).

    **익명 클래스 표현식(named Restriction)은 타고 가지 않는다** — 그것은 공리이지
    분류 계층이 아니다. 타면 ``Union_*`` 를 경유해 무관한 클래스가 조상으로 보인다.

    Args:
        graph: 대상 그래프.
        candidate: 조상 여부를 판정할 노드.
        node: 기준 노드.

    Returns:
        ``candidate`` 가 ``node`` 의 조상(자기 자신 포함)이면 True.
    """
    if not (isinstance(candidate, URIRef) and isinstance(node, URIRef)):
        return False
    if candidate == node:
        return True
    seen: set = {node}
    frontier = [node]
    while frontier:
        current = frontier.pop()
        for parent in graph.objects(current, RDFS.subClassOf):
            if not isinstance(parent, URIRef) or parent in seen:
                continue
            if parent == candidate:
                return True
            # 공리 노드는 계층이 아니다 — 경유하면 무관한 조상이 보인다.
            if (parent, RDF.type, OWL.Restriction) in graph:
                continue
            seen.add(parent)
            frontier.append(parent)
    return False


def would_violate_disjoint(
    graph: Graph, child, new_parent,
) -> tuple[str, str] | None:
    """``child ⊑ new_parent`` 를 추가하면 **unsatisfiable** 이 되는가.

    두 조상이 같은 disjoint 그룹에 속하면 그 클래스는 논리적으로 인스턴스를 가질
    수 없다 (HermiT `unsatisfiable_classes`). A-Box 는 그 클래스의 인스턴스를
    만들 수 없으므로 해당 CSV 테이블이 KG 에서 사라진다.

    실측 (2026-08-11 S3): 중간 추상 클래스 스텝이 이미 ``EquipmentManagement``
    계열에 속한 ``FailureCause`` / ``MaintenanceHistory`` 에 ``MaintenanceManagement``
    를, ``MonitoringManagement`` 계열의 ``TagMaster`` 에 ``EquipmentAsset`` 을
    붙여 **3개 클래스가 unsatisfiable** 이 됐다. 8개 도메인 그룹은 S2 부터
    disjoint 로 선언돼 있었고, 스텝은 그것을 보지 않았다.

    Returns:
        ``(조상A, 조상B)`` — 충돌하는 두 조상의 local name. 충돌 없으면 ``None``.
    """
    import rdflib.collection
    from rdflib import OWL, RDF, RDFS, URIRef

    def _local(node) -> str:
        return str(node).split("#")[-1].split("/")[-1]

    def _ancestors(node, seen: frozenset = frozenset()) -> set[str]:
        out: set[str] = set()
        for parent in graph.objects(node, RDFS.subClassOf):
            if not isinstance(parent, URIRef) or parent in seen:
                continue
            out.add(_local(parent))
            out |= _ancestors(parent, seen | {parent})
        return out

    combined = (
        {_local(child), _local(new_parent)}
        | _ancestors(child) | _ancestors(new_parent)
    )

    groups: list[set[str]] = []
    for bnode in graph.subjects(RDF.type, OWL.AllDisjointClasses):
        members = list(graph.objects(bnode, OWL.members))
        if not members:
            continue
        try:
            groups.append({
                _local(m) for m in rdflib.collection.Collection(graph, members[0])
            })
        except Exception as exc:  # noqa: BLE001 — 파싱 불가 그룹은 건너뛴다
            logger.debug("AllDisjointClasses 파싱 실패, 건너뜀: %s", exc)
    for subj, _, obj in graph.triples((None, OWL.disjointWith, None)):
        groups.append({_local(subj), _local(obj)})

    for group in groups:
        hit = sorted(group & combined)
        if len(hit) >= 2:
            return (hit[0], hit[1])
    return None


def parse_object_term(raw, graph: Graph | None = None):
    """트리플 object 값 → ``URIRef`` 또는 ``Literal``. **삭제/교체 매칭용.**

    LLM 은 object 를 세 가지로 보낸다: prefixed name (``steel:Equipment``), 완전
    IRI, 그리고 **Turtle 리터럴 표기** (``"true"^^xsd:boolean`` / ``"라벨"@ko``).
    세 번째를 IRI 로 오해하면 삭제·교체가 영구히 실패한다 — 매칭 대상이 그래프에
    없는 유령이 되기 때문이다.

    실측 (2026-08-10 S2): Jury 가 ``owl:deprecated`` 를 ``"true"^^xsd:string`` →
    ``xsd:boolean`` 으로 고치려 했으나 두 값 모두 IRI 로 해석돼 "교체할 트리플
    없음" 으로 끝났다. 그 결과 deprecated 선언이 OWL 2 사양과 다른 타입으로 남아
    **추론기가 인식하지 못한다**.

    판정 순서:

    1. ``"..."`` 로 시작하면 **무조건 리터럴** — ``^^datatype`` / ``@lang`` 해석.
       콜론이 있어도 prefixed name 이 아니다.
    1.5. ``<...>`` 는 Turtle IRI 표기 → 해제 후 **IRI 로 확정**
       (:func:`strip_iri_brackets`). 예전에는 2번의 ``startswith`` 에 걸리지 않아
       ``<http://…>`` 가 **조용히 평문 Literal** 이 됐다 — 경고조차 없어서 삭제·
       교체 매칭이 영구 실패했다 (2026-08-19 실측).
    2. 완전 IRI (``http:`` / ``https:`` / ``urn:``) → ``URIRef``.
    3. ``pfx:local`` 형태이고 **prefix 가 알려진 것** 이면 IRI
       (:func:`resolve_entity_name` 위임). ``xsd:`` 는 데이터타입 네임스페이스라
       object 자리에서 값이 아니므로 제외한다.
    4. 그 외 → 평문 리터럴.

    Args:
        raw: 문자열이 아니면 그대로 ``Literal`` 로 감싼다 (숫자/bool 등).

    Returns:
        ``URIRef`` 또는 ``Literal``. ``None``/빈 문자열이면 ``None``.
    """
    from rdflib import Literal, URIRef

    from domain.namespaces import FOREIGN_PREFIXES, NS_PREFIX

    if raw is None:
        return None
    if not isinstance(raw, str):
        return Literal(raw)
    text = raw.strip()
    if not text:
        return None

    # 1. Turtle 리터럴 표기 — 따옴표로 시작하면 콜론이 있어도 리터럴이다.
    if text[0] in "\"'":
        quote = text[0]
        end = text.rfind(quote)
        if end > 0:
            value = text[1:end]
            suffix = text[end + 1:].strip()
            value = value.replace('\\"', '"').replace("\\'", "'")
            if suffix.startswith("^^"):
                dtype = suffix[2:].strip()
                dt_uri = resolve_entity_name(dtype, graph) if dtype else None
                return Literal(value, datatype=dt_uri) if dt_uri else Literal(value)
            if suffix.startswith("@"):
                return Literal(value, lang=suffix[1:].strip())
            return Literal(value)
        return Literal(text)

    # 1.7. Turtle 컬렉션 표기 ``( a b c )`` — RDF 리스트로 만든다.
    #
    # 이것 없이는 LLM 이 **문법적으로 올바른** Turtle 을 보냈는데 평문 리터럴이
    # 됐다. 실측 (2026-08-30 배포 T-Box): ``owl:disjointUnionOf`` 7건이
    # ``"(steel:A steel:B)"^^xsd:string`` 으로 박혀 있었다 — 같은 부모에 정상
    # 리스트와 문자열이 **공존**하고, jury 는 ``applied`` 를 보고했다. OWL 상
    # 리스트가 아니므로 그 공리는 추론에 아무 영향이 없다 (조용한 폐기).
    #
    # ``graph`` 가 없으면 리스트 노드를 만들 곳이 없다 — 그때는 리터럴 폴백보다
    # ``None``(해석 불가) 이 정직하다. 호출부가 "값을 해석할 수 없음" 으로 집계해
    # 드러난다.
    if text.startswith("(") and text.endswith(")"):
        inner = text[1:-1].strip()
        if not inner:
            from rdflib import RDF as _RDF
            return _RDF.nil
        if graph is None:
            logger.debug("컬렉션 표기인데 graph 가 없어 해석 불가: %s", text[:60])
            return None
        items = [
            resolve_entity_name(tok, graph)
            for tok in inner.split()
            if tok and tok not in (",",)
        ]
        items = [i for i in items if i is not None]
        if not items:
            return None
        from rdflib import BNode as _BNode
        from rdflib.collection import Collection as _Collection
        head_node = _BNode()
        _Collection(graph, head_node, items)
        return head_node

    # 1.5. Turtle IRI 표기 (<...>) — 해제하면 IRI 가 확정이므로 리터럴 폴백을
    # 타지 않는다. 안이 prefixed name 일 수도 있어 해석기에 위임한다.
    unbracketed, was_bracketed = strip_iri_brackets(text)
    if was_bracketed:
        inner = unbracketed.strip()
        if not inner:
            return None
        return resolve_entity_name(inner, graph)

    # 2. 완전 IRI
    if text.startswith(("http://", "https://", "urn:", "file:")):
        return URIRef(text)

    # 3. 알려진 prefix 만 IRI 로 승격 (xsd: 는 값이 아니라 타입 네임스페이스).
    head, sep, rest = text.partition(":")
    if sep and rest and " " not in text and head and head.replace("-", "").isalnum():
        known = head == NS_PREFIX or (head in FOREIGN_PREFIXES and head != "xsd")
        if not known and graph is not None and head != "xsd":
            try:
                known = head in dict(graph.namespace_manager.namespaces())
            except Exception as exc:  # noqa: BLE001 — 조회 실패 시 리터럴로
                logger.debug("prefix 바인딩 조회 실패, 리터럴로 처리: %s", exc)
        if known:
            return resolve_entity_name(text, graph)

    # 4. 평문 리터럴 ("true" / "설비: 고로" / 숫자 문자열)
    return Literal(text)


def match_object_in_graph(graph: Graph, subject, predicate, wanted):
    """``(s, p, wanted)`` 를 그래프에서 찾되 **리터럴 표기 차이를 흡수**한다.

    삭제·교체의 매칭 실패 대부분은 값이 아니라 **표기** 때문이다. 실측 사례:

    - ``"unwanted"`` 로 보냈지만 그래프에는 ``"unwanted"@ko`` 가 있다.
    - ``"true"`` (평문) vs 그래프의 ``"true"^^xsd:string``.
    - LLM 이 ``xsd:string`` 을 붙여 보냈지만 그래프는 평문 리터럴.

    정확 일치를 **먼저** 시도하고, 없을 때만 같은 문자열 값을 가진 리터럴로
    넓힌다. 여러 개가 매칭되면 ``None`` — 어느 것을 지울지 판별할 수 없으므로
    추측하지 않는다 (조용히 엉뚱한 값을 지우는 것보다 실패가 낫다).

    Returns:
        그래프에 실재하는 object term, 또는 ``None`` (없음/모호함).
    """
    from rdflib import Literal

    if wanted is None:
        return None
    if (subject, predicate, wanted) in graph:
        return wanted
    if not isinstance(wanted, Literal):
        return None
    target = str(wanted)
    candidates = [
        o for o in graph.objects(subject, predicate)
        if isinstance(o, Literal) and str(o) == target
    ]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        logger.warning(
            "리터럴 %r 이 %d 개 표기로 존재해 대상을 특정할 수 없다 — 건너뛴다",
            target, len(candidates),
        )
    return None


def clean_source_column(raw: str) -> str:
    """LLM 이 준 ``dcterms:source`` 값 → **CSV 헤더와 비교 가능한** 컬럼명.

    LLM 은 이 필드에 Turtle 직렬화 표기를 그대로 담아 보낸다. 실측 (2026-08-10
    배포 T-Box): ``dcterms:source "\\"Event_ID\\"^^xsd:string"^^xsd:string`` 형태가
    **34건**. A-Box 는 이 값을 ``str(src).strip().upper()`` 로 CSV 헤더와 직접
    비교하므로 (``abox_generation.py`` 출처 인덱스), 이런 값은 **어떤 헤더와도
    매칭되지 않고** 해당 컬럼이 조용히 transliteration 폴백으로 떨어진다 —
    dcterms:source 를 쓰는 이유(이름 추측 제거) 자체가 무력화된다.

    Step 12e 게이트도 이것을 잡지 못했다: 값이 **존재하긴** 하므로 커버리지에는
    포함되고, 형식은 검사하지 않았다.

    정규화 규칙: 감싼 따옴표 제거 → ``^^datatype`` 접미 제거 → ``@lang`` 제거 →
    공백 정리. 컬럼명에 정당하게 들어갈 수 있는 문자(밑줄·숫자·하이픈)는 보존한다.

    Returns:
        정리된 컬럼명. 정리 후 비면 ``""`` — 호출부는 빈 값을 기록하지 않아야 한다.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    # 중첩이 두 겹 이상일 수 있다 ("\"X\"^^xsd:string"^^xsd:string).
    for _ in range(3):
        before = text
        # ^^datatype / @lang 접미 제거 (따옴표 뒤에 붙은 경우만).
        text = re.sub(r'"\s*(?:\^\^\s*\S+|@[A-Za-z-]+)\s*$', '"', text).strip()
        # 감싼 따옴표 한 겹 제거.
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1].strip()
        # 이스케이프된 따옴표 해제.
        text = text.replace('\\"', '"').replace("\\'", "'").strip()
        if text == before:
            break
    # 접미가 따옴표 없이 남은 경우 (X^^xsd:string).
    text = re.sub(r"\^\^\s*\S+$", "", text).strip()
    return text.strip("\"' \t")


def humanize_local_name(name: str) -> str:
    """camelCase / PascalCase local name → 공백 분리 소문자 문자열.

    ``"hasEquipmentMaster"`` → ``"has equipment master"``,
    ``"GHGEmission"`` → ``"ghg emission"``.

    프로젝트의 **정식** 라벨 합성 규칙. 배포 T-Box 실측으로 확인된 관례다:
    OP @en 라벨 194/194, DP 246/246 이 공백 분리 문장이고 camelCase 는 0건.
    같은 로직이 ``jury_fixes._humanize_local_name`` 과
    ``ontology_quality._local_name_to_human`` 에 사본으로 존재했다 — 둘 다 이
    함수를 부르도록 통일한다.
    """
    import re as _re

    # 연속 대문자(축약어) 경계: GHGEmission → GHG Emission
    s = _re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    # camelCase 경계: hasEquipment → has Equipment
    s = _re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    return s.replace("_", " ").replace("-", " ").strip().lower()


def label_for_entity(uri: URIRef | str) -> str:
    """엔티티 IRI → @en 라벨 문자열.

    **원본 이름 문자열이 아니라 해석된 IRI 에서** 라벨을 만드는 것이 핵심이다.
    LLM 이 보낸 DSL 이름(``"steel:hasFoo"``)을 그대로 라벨에 쓰면 prefix 가 라벨로
    새어나온다 — 2026-08-09 배포 T-Box 에 ``rdfs:label "steel:…"@en`` 117건이
    그렇게 생겼고, 그 오염이 시맨틱 딕셔너리 ``label_en`` 107건까지 전파됐다
    (딕셔너리는 LLM NL→SPARQL 레퍼런스다).

    부수 효과로 bare ``"hasFoo"`` 와 prefixed ``"steel:hasFoo"`` 가 **같은**
    라벨을 얻는다 — 예전에는 각각 ``'hasFoo'`` / ``'steel:hasFoo'`` 였다.
    """
    local = str(uri).split("#")[-1].split("/")[-1]
    return humanize_local_name(local)


def paginated_query(graph: Graph, sparql: str, batch_size: int = 1000) -> Generator:
    """SPARQL SELECT를 페이지네이션으로 전수 실행. 메모리 O(batch_size).
    sparql에 OFFSET/LIMIT이 없어야 한다 (자동 추가됨).
    """
    offset = 0
    while True:
        paged = f"{sparql} OFFSET {offset} LIMIT {batch_size}"
        results = list(graph.query(paged))
        if not results:
            break
        yield from results
        if len(results) < batch_size:
            break
        offset += batch_size


#: skolemize 된 **익명 클래스 표현식** 의 이름 접두.
#:
#: ``_skolemize_bnodes`` 가 ``owl:Restriction`` / ``owl:unionOf`` bnode 에 IRI 를
#: 붙일 때 쓰는 규약 (``ontology_quality.py`` 의 ``Union_{cls}_{hash8}`` /
#: ``{cls}_{prop}_{type}`` 형태). 이름이 붙어도 **명명된 도메인 클래스가 아니다** —
#: ``ManufacturingProcessStep owl:equivalentClass [unionOf ...]`` 의 본체일 뿐이다.
_ANON_EXPR_PREFIXES = ("Union_",)


#: 익명 클래스 표현식을 가리키는 술어 — 하나라도 있으면 표현식의 본체다.
_CLASS_EXPR_PREDS = (
    OWL.unionOf, OWL.intersectionOf, OWL.complementOf, OWL.oneOf, OWL.onProperty,
)


def is_anonymous_class_expression(graph: Graph, node, domain_ns: str) -> bool:
    """``node`` 가 **skolemize 된 익명 클래스 표현식** 인가.

    :func:`is_named_domain_class` 의 판정 중 "표현식인가" 축만 떼어낸 것이다.
    클래스 여부·도메인 소속을 묻지 않으므로 **프로퍼티에도 안전하게** 쓸 수 있다 —
    ``is_named_domain_class`` 는 OP/DP 에도 ``False`` 를 돌려주므로 그것을 필터로
    쓰면 "deprecated 프로퍼티" 같은 **정당한 지적까지 침묵** 한다.

    도메인 밖 IRI 와 BNode 는 ``False`` — 판정 대상이 아니다 (호출부가 도메인
    소속을 이미 걸렀거나, 걸러야 한다).
    """
    if not isinstance(node, URIRef) or not str(node).startswith(domain_ns):
        return False
    if (node, RDF.type, OWL.Restriction) in graph:
        return True
    for pred in _CLASS_EXPR_PREDS:
        if (node, pred, None) in graph:
            return True
    return str(node)[len(domain_ns):].startswith(_ANON_EXPR_PREFIXES)


def is_named_domain_class(graph: Graph, node, domain_ns: str) -> bool:
    """``node`` 가 **명명된 도메인 클래스** 인가 (익명 표현식의 skolem 이름 제외).

    ## 왜 필요한가

    2026-08-19 S4.5 mutation 감사: ``check_quality_rules`` 가 baseline 에서 이미
    FAIL 이라 **어떤 mutant 도 구분하지 못하는 죽은 게이트** 였다 (적용 7개 중 1개만
    검출, 14.3%). 특히 생존한 M5 3건(label/comment 삭제)은 정확히 이 게이트의
    담당 영역인데, baseline 이 같은 규칙으로 이미 FAIL 이어서 추가 위반이 신호로
    보이지 않았다.

    원인은 **한 개** 노드였다. ``Union_ManufacturingProcessStep_18a9fee9`` —
    step_28 이 만든 ``equivalentClass [unionOf ...]`` 의 skolemize 된 본체가
    ``owl:Class`` 로 세어져 high 3건을 냈다:

    - ``missing_label`` @en / @ko — 익명 표현식에 라벨을 요구할 근거가 없다
    - ``deprecated_reference`` — step_21 이 "자식·용처 없음" 으로 deprecated 를
      붙였는데, ``ManufacturingProcessStep`` 이 ``equivalentClass`` 로 참조 중이다

    즉 후속 스텝들이 익명 표현식을 도메인 클래스로 오인해 어노테이션을 붙이고,
    게이트가 그 어노테이션 부재/모순을 결함으로 보고하는 자기충족 루프였다.

    ## 왜 공용 헬퍼인가

    실측: ``subjects(RDF.type, OWL.Class)`` 를 도는 지점이 23곳인데 **21곳이
    Restriction/Union 을 배제하지 않는다.** 한 곳만 고치면 다른 스텝이 다음 실행에서
    같은 오염을 다시 만든다 — 이 리포에서 "사본이 퍼진 로직" 이 반복된 유형이다
    (prefix 해석 사본 11개 사례와 같은 구조).

    Args:
        graph: 대상 그래프.
        node: 판정할 노드.
        domain_ns: 도메인 네임스페이스 문자열 (``ctx.domain_ns``).

    Returns:
        명명된 도메인 클래스면 True. 아래는 모두 False —
        도메인 밖 IRI / BNode / ``owl:Restriction`` 타입 / ``owl:unionOf``·
        ``intersectionOf``·``complementOf`` 를 가진 표현식 / skolem 접두 이름.
    """
    if not isinstance(node, URIRef) or not str(node).startswith(domain_ns):
        return False
    if (node, RDF.type, OWL.Restriction) in graph:
        return False
    # 클래스 표현식 술어를 **하나라도** 가지면 익명 표현식의 본체다.
    for pred in (OWL.unionOf, OWL.intersectionOf, OWL.complementOf,
                 OWL.oneOf, OWL.onProperty):
        if (node, pred, None) in graph:
            return False
    local = str(node)[len(domain_ns):]
    return not local.startswith(_ANON_EXPR_PREFIXES)


def named_domain_classes(graph: Graph, domain_ns: str) -> set:
    """``owl:Class`` 중 **명명된 도메인 클래스만** 반환.

    ``subjects(RDF.type, OWL.Class)`` 를 직접 도는 대신 이 함수를 써라 — 그러면
    익명 표현식의 skolem 이름이 어노테이션·게이트 대상에 섞이지 않는다.
    판정 근거는 :func:`is_named_domain_class` 참조.
    """
    return {
        c for c in graph.subjects(RDF.type, OWL.Class)
        if is_named_domain_class(graph, c, domain_ns)
    }


def build_class_adjacency(tbox: Graph) -> dict[URIRef, set[tuple[URIRef, str]]]:
    """T-Box ObjectProperty로 클래스 간 인접 그래프 구축."""
    adjacency: dict[URIRef, set[tuple[URIRef, str]]] = defaultdict(set)
    for op in tbox.subjects(RDF.type, OWL.ObjectProperty):
        domains = list(tbox.objects(op, RDFS.domain))
        ranges = list(tbox.objects(op, RDFS.range))
        if not domains or not ranges:
            continue
        domain = domains[0]
        range_ = ranges[0]
        adjacency[domain].add((range_, str(op)))
        for inv in tbox.objects(op, OWL.inverseOf):
            adjacency[range_].add((domain, str(inv)))
    return dict(adjacency)


def find_path_bfs(
    adjacency: dict[URIRef, set[tuple[URIRef, str]]],
    source: URIRef,
    target: URIRef,
    max_hops: int = 3,
) -> dict | None:
    """BFS로 두 클래스 간 최단 경로 탐색."""
    if source == target:
        return {"path": [source], "edges": [], "hops": 0}
    queue = deque([(source, [source], [])])
    visited = {source}
    while queue:
        current, path, edges = queue.popleft()
        if len(path) - 1 >= max_hops:
            continue
        for neighbor, via_op in adjacency.get(current, set()):
            if neighbor == target:
                return {
                    "path": path + [neighbor],
                    "edges": edges + [via_op],
                    "hops": len(path),
                }
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor], edges + [via_op]))
    return None
