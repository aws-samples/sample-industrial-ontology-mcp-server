"""T-Box 공통 유틸리티 — 여러 도구 모듈에서 공유.

아래 ruff 억제는 선재 부채다 (SIM103 로그 필터 / UP038 isinstance 튜플 2건) —
새 코드에 적용하지 말 것.
"""
# ruff: noqa: SIM103, UP038

import glob
import logging
import os
import threading
import time

from rdflib import OWL, RDF, RDFS, Graph

from config import ABOX_PATH, INFERRED_PATH, SOURCE_TACIT_DIR, TBOX_PATH
from domain.namespaces import DOMAIN_NS
from domain.uri_conventions import local_name

logger = logging.getLogger(__name__)


# R21-5: rdflib.term 의 xsd:time 리터럴 변환 실패 경고 억제. tacit 파일의
# `"24:00:00"^^xsd:time` 같은 합법적(OWL/XSD) 이지만 Python datetime.time
# 이 거부하는 값이 원인. 기능적 영향 없음 (lexical form 보존), 로그 스팸만
# 발생. domain.tbox_utils 는 T-Box/A-Box/validate 모두가 import 하므로
# 여기서 한 번만 filter 설치하면 전 파이프라인 커버.
class _SuppressTimeLiteralConvertWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        msg = record.getMessage()
        if "Failed to convert Literal lexical form" in msg and "#time" in msg:
            return False
        return True


_rdflib_term_logger = logging.getLogger("rdflib.term")
if not any(isinstance(f, _SuppressTimeLiteralConvertWarning)
            for f in _rdflib_term_logger.filters):
    _rdflib_term_logger.addFilter(_SuppressTimeLiteralConvertWarning())


# rdflib store backend. "Oxigraph" (Rust, 기본) or "default" (Python Memory).
# 환경변수 RDFLIB_STORE로 오버라이드 가능.
_RDFLIB_STORE = os.getenv("RDFLIB_STORE", "Oxigraph")

# Optional RocksDB-backed Oxigraph path. When set, _new_graph() returns a Graph
# whose Oxigraph store persists triples to that directory (RAM ≪ in-memory mode,
# at the cost of ~30% slower ingestion). The directory is created on first open.
# Use case: A-Box generation on multi-million-row CSVs where the in-memory
# triple count would otherwise blow past available RAM.
_OXIGRAPH_PATH = os.getenv("OXIGRAPH_STORE_PATH", "").strip()


def _new_graph(disk_path: str | None = None) -> Graph:
    """rdflib Graph 를 생성한다.

    Args:
        disk_path: 명시되면 해당 디렉토리를 RocksDB 백엔드로 사용 (Oxigraph
            store 한정). 메인 A-Box 그래프처럼 수백만 triple 을 적재할 때만
            사용 — 보조 그래프는 in-memory 유지 권장. 동일 경로에 동시 open
            불가능 (RocksDB single-writer lock) — 호출자가 직렬 사용 보장.
            None 이면 기존 in-memory 동작.

    환경변수:
        RDFLIB_STORE: "Oxigraph" (기본) | "default" (Python Memory)
    """
    if _RDFLIB_STORE and _RDFLIB_STORE != "default":
        if disk_path and _RDFLIB_STORE.lower() == "oxigraph":
            # oxrdflib plugin contract:
            #   - create=True  → path MUST NOT exist (it creates the directory)
            #   - create=False → path MUST exist (RocksDB opens existing data)
            # The Graph identifier MUST be stable across opens — otherwise
            # rdflib treats persisted quads as belonging to a different
            # context and len(g) returns 0 on reopen. We derive the identifier
            # from the disk path so that the same directory always reopens
            # with the same identity.
            from rdflib import URIRef as _U
            exists = os.path.exists(disk_path)
            graph_id = _U(f"urn:oxigraph-store:{os.path.abspath(disk_path)}")
            g = Graph(store=_RDFLIB_STORE, identifier=graph_id)
            g.open(disk_path, create=not exists)
            return g
        return Graph(store=_RDFLIB_STORE)
    return Graph()


def fast_serialize_turtle(g: Graph) -> str:
    """rdflib Graph 를 Turtle 로 직렬화하되, Oxigraph store 인 경우 Rust
    serializer 를 직접 호출하여 속도를 수십 배 가속한다.

    rdflib turtle serializer 는 대규모(>100K triples)에서 분 단위로 느리다.
    Oxigraph store 는 내부에 pyoxigraph.Store 를 보유하므로, 동일 데이터를
    Rust 엔진으로 바로 serialize 하면 된다.

    Args:
        g: rdflib Graph (store 가 Oxigraph 이든 Memory 이든 무관).

    Returns:
        Turtle 문자열. (UTF-8 디코딩).
    """
    # Oxigraph store 경로
    try:
        inner = getattr(g.store, "_inner", None)
        if inner is not None:
            import io

            from pyoxigraph import RdfFormat
            # rdflib 는 단일 default graph 를 Oxigraph 의 고유 BNode graph 로
            # 저장한다 (conjunctive graph 방식). 따라서 named_graphs() 의 첫
            # 그래프가 곧 우리가 원하는 그래프.
            graphs = list(inner.named_graphs())
            if not graphs:
                # bulk_load 등으로 default graph 에 직접 저장된 경우
                from pyoxigraph import DefaultGraph
                from_graph = DefaultGraph()
            else:
                from_graph = graphs[0]
            prefixes = {p: str(n) for p, n in g.namespaces()}
            buf = io.BytesIO()
            try:
                inner.dump(
                    buf, RdfFormat.TURTLE,
                    from_graph=from_graph, prefixes=prefixes,
                )
            except TypeError:
                # 구버전 pyoxigraph 는 prefixes 인자 미지원
                inner.dump(buf, RdfFormat.TURTLE, from_graph=from_graph)
            return buf.getvalue().decode("utf-8")
    except Exception as e:
        logger.debug("fast_serialize_turtle: Oxigraph 경로 실패 → rdflib fallback: %s", e)

    # Fallback: rdflib 기본 serializer
    return g.serialize(format="turtle")


def fast_parse_turtle(g: Graph, source: str | bytes, *,
                       is_path: bool = True) -> Graph:
    """Turtle 파일/바이트를 rdflib Graph 에 빠르게 로드한다.

    Oxigraph store 인 경우 pyoxigraph.Store.bulk_load 를 사용해
    Rust 파서로 직접 투입한다 (rdflib turtle parser 대비 ~10× 가속, 1.5M
    triples 기준 24.8s → 2.2s).

    Args:
        g: rdflib Graph (Oxigraph store 기반이면 fast path).
        source: 파일 경로(str) 또는 Turtle 바이트.
        is_path: True 면 ``source`` 를 파일 경로로, False 면 바이트로 취급.

    Returns:
        로드된 ``g`` (in-place 수정; chaining 편의를 위해 반환).
    """
    try:
        inner = getattr(g.store, "_inner", None)
        if inner is not None:
            from pyoxigraph import BlankNode, NamedNode, RdfFormat
            # rdflib.Graph(store="Oxigraph") 는 자체 identifier (BNode 또는
            # URIRef) 를 갖고 모든 triple 을 그 graph 에 저장한다. bulk_load 는
            # 기본적으로 Oxigraph default graph 에 넣기 때문에 rdflib 쪽에선
            # 못 본다 → to_graph 로 rdflib identifier 를 명시 전달.
            _gid = g.identifier
            _gid_str = str(_gid)
            # rdflib BNode (N…) vs URIRef 구분
            if isinstance(_gid, type(g).__mro__[0].__class__):
                pass  # fallthrough; handled below
            try:
                # rdflib.BNode 는 접두사 없는 값을 가짐 (예: "N43d94…")
                from rdflib import BNode as _RBNode
                from rdflib import URIRef as _URef
                if isinstance(_gid, _RBNode):
                    to_graph = BlankNode(_gid_str)
                elif isinstance(_gid, _URef):
                    to_graph = NamedNode(_gid_str)
                else:
                    to_graph = None  # 알 수 없으면 default graph fallback
            except Exception:
                to_graph = None
            if is_path:
                with open(source, "rb") as _fh:
                    inner.bulk_load(_fh, RdfFormat.TURTLE, to_graph=to_graph)
            else:
                import io
                buf = io.BytesIO(
                    source if isinstance(source, (bytes, bytearray)) else source.encode("utf-8"),
                )
                inner.bulk_load(buf, RdfFormat.TURTLE, to_graph=to_graph)
            return g
    except Exception as e:
        logger.debug("fast_parse_turtle: Oxigraph 경로 실패 → rdflib fallback: %s", e)

    # Fallback: rdflib 표준 parser
    if is_path:
        g.parse(source, format="turtle")
    else:
        data = source if isinstance(source, (bytes, bytearray)) else source.encode("utf-8")
        g.parse(data=data, format="turtle")
    return g


def load_object_properties(tbox_path: str | None = None) -> list[dict]:
    """T-Box TTL에서 ObjectProperty 목록을 추출한다.

    Returns:
        [{"name", "domain", "range", "inverse"}, ...]
    """
    path = tbox_path or TBOX_PATH
    try:
        g = _new_graph()
        g.parse(path, format="turtle")
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("T-Box 파싱 실패 (%s): %s", path, e)
        return []

    props = []
    steel_str = DOMAIN_NS
    for prop in g.subjects(RDF.type, OWL.ObjectProperty):
        if not str(prop).startswith(steel_str):
            continue
        name = local_name(str(prop))
        domains = [local_name(str(d)) for d in g.objects(prop, RDFS.domain) if str(d).startswith(steel_str)]
        ranges = [local_name(str(r)) for r in g.objects(prop, RDFS.range) if str(r).startswith(steel_str)]
        inverses = [local_name(str(i)) for i in g.objects(prop, OWL.inverseOf) if str(i).startswith(steel_str)]
        props.append({
            "name": name,
            "domain": domains[0] if domains else None,
            "range": ranges[0] if ranges else None,
            "inverse": inverses[0] if inverses else None,
        })
    return props


def ensure_inverse_triples(g: Graph, tbox_path: str = "") -> int:
    """그래프 내 ObjectProperty 트리플에 대해 누락된 inverse 트리플을 보완한다.

    T-Box의 owl:inverseOf 정의를 참조하여, 정방향 트리플이 있으면
    역방향 트리플을 자동 추가한다. OWL 추론 없이도 양방향 조회가 가능해진다.

    **안전장치**: T-Box 가 (fwd, rev) 쌍을 inverseOf 로 선언했더라도
    range(fwd) 와 domain(rev) 가 서로 맞물리지 않으면 역트리플 생성을
    skip 한다. 이것은 T-Box 자체의 결함 (잘못된 inverseOf 선언) 이 A-Box
    에 번지는 것을 막는다. 예: suppliesItem(range=ItemSupplierMap) 의 inverse
    를 isSuppliedBy(domain=ItemMaster) 로 선언한 경우, 역트리플 생성시
    (ItemSupplierMap 인스턴스, isSuppliedBy, ...) 가 만들어져 domain
    위반을 유발한다. 호환성 체크로 이를 차단.

    Returns:
        추가된 inverse 트리플 수
    """
    tbox = _new_graph()
    try:
        tbox.parse(tbox_path or TBOX_PATH, format="turtle")
    except FileNotFoundError:
        logger.warning("T-Box not found for inverse triples: %s", tbox_path or TBOX_PATH)
        return 0
    except Exception as e:
        logger.error("T-Box parse failed for inverse triples: %s", e)
        return 0

    steel_str = DOMAIN_NS
    from rdflib import RDFS, URIRef

    # subclass 체인: ancestor_set[x] = {x 와 그 모든 상위 클래스}
    subclass_of: dict[URIRef, set[URIRef]] = {}
    for sub, _, sup in tbox.triples((None, RDFS.subClassOf, None)):
        if isinstance(sub, URIRef) and isinstance(sup, URIRef):
            subclass_of.setdefault(sub, set()).add(sup)

    def _ancestors(cls: URIRef) -> set[URIRef]:
        seen = {cls}
        stack = [cls]
        while stack:
            cur = stack.pop()
            for p in subclass_of.get(cur, ()):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen

    # OP 별 domain / range (steel 네임스페이스 URIRef 만).
    # owl:Thing 은 universal class — 제약 없음을 뜻하므로 None 센티넬로
    # 표현한다 (미선언=빈 집합 과 구분). 미선언은 "불완전 선언" 으로
    # 보수적 skip 하지만, owl:Thing 은 명시적 "제약 없음" 이라 무엇과도
    # 호환이어야 한다. tools/validation_support/checks/structural.py 및
    # tools/tacit_op_validator.py 와 동일한 owl:Thing 의미론.
    def _dom_rng(prop: URIRef):
        def _classes(pred):
            vals = set(tbox.objects(prop, pred))
            if OWL.Thing in vals:
                return None  # universal → 무엇과도 호환
            return {x for x in vals
                    if isinstance(x, URIRef) and str(x).startswith(steel_str)}
        return _classes(RDFS.domain), _classes(RDFS.range)

    def _compatible(a, b) -> bool:
        """호환성 판정.

        - 한 쪽이라도 universal(None, owl:Thing): 제약 없음 → True
        - 양쪽 모두 비어 있으면: 제약 없음 → True (기존 동작 유지)
        - 한 쪽만 비어 있으면: T-Box 선언이 불완전 (blank-node unionOf 또는
          domain 누락) 이므로 보수적으로 호환성 미확인 = 역트리플 생성 skip.
          잘못된 inverseOf 선언이 도미노처럼 domain/range 위반을 유발하는
          것을 차단한다.
        - 양쪽 모두 있으면: subclass 체인 기반 호환성 검사.
        """
        if a is None or b is None:
            return True
        if not a and not b:
            return True
        if not a or not b:
            return False
        for cls_a in a:
            anc_a = _ancestors(cls_a)
            for cls_b in b:
                if cls_b in anc_a:
                    return True
                if cls_a in _ancestors(cls_b):
                    return True
        return False

    # inverseOf 쌍 수집 + 호환성 필터링
    inverse_pairs: list[tuple[URIRef, URIRef]] = []
    skipped_incompatible: list[tuple[str, str]] = []
    for prop in tbox.subjects(RDF.type, OWL.ObjectProperty):
        if not str(prop).startswith(steel_str):
            continue
        for inv in tbox.objects(prop, OWL.inverseOf):
            if not str(inv).startswith(steel_str):
                continue
            fwd_d, fwd_r = _dom_rng(prop)
            inv_d, inv_r = _dom_rng(inv)
            # range(fwd) 가 domain(inv) 와 호환되고
            # domain(fwd) 가 range(inv) 와 호환되어야 올바른 inverseOf
            if not _compatible(fwd_r, inv_d) or not _compatible(fwd_d, inv_r):
                skipped_incompatible.append((
                    str(prop).rsplit("#", 1)[-1],
                    str(inv).rsplit("#", 1)[-1],
                ))
                continue
            inverse_pairs.append((prop, inv))

    if skipped_incompatible:
        logger.warning(
            "inverseOf 호환성 미달로 역트리플 생성 skip: %d 쌍 — 예시: %s",
            len(skipped_incompatible), skipped_incompatible[:3],
        )

    # ``owl:inverseOf`` 는 의미론상 **대칭**이다 (OWL 2 Spec §9.2.3): A inverseOf B
    # 는 B inverseOf A 와 같은 공리다. 그런데 쌍 수집은 선언된 방향에서만 일어나므로,
    # 한쪽만 선언된 편도 쌍에서 **선언되지 않은 방향의 역트리플이 만들어지지 않았다.**
    #
    # 실측 (2026-08-28): T-Box 의 inverseOf 10쌍이 편도였고, 그 결과 필수참여 공리
    # 위반으로 집계됐다. 예: hasRealTimeTag(36,000 트리플)가 있는데
    # isTagOfRealTimeData 는 0 이라 TagMaster 50/50 이 위반으로 보고됐다 —
    # T-Box 는 isTagOfRealTimeData inverseOf hasRealTimeTag 만 선언하고 그 반대는
    # 없었다. CSV FK 는 이미 온전했고(Real_Time_Data.Tag_ID, blank 0) 결손은 선언
    # 한 줄이었다.
    #
    # 양방향으로 채운다. 호환성 검사는 이미 (fwd_r↔inv_d, fwd_d↔inv_r) 두 축을 모두
    # 확인하므로 대칭 적용이 새 domain/range 위반을 만들지 않는다.
    added = 0
    for fwd, rev in inverse_pairs:
        for src, dst in ((fwd, rev), (rev, fwd)):
            for s, _, o in g.triples((None, src, None)):
                if (o, dst, s) not in g:
                    g.add((o, dst, s))
                    added += 1
    return added


# ── 메모리 캐시 (mtime 기반 무효화) ──
# 단일 항목 LRU: 대용량 그래프 2개 이상 동시 상주하면 메모리 폭주(수 GB) → OOM.
# validate_kg는 inferred(2.8GB) + merge(1.15GB)를 연달아 로드하므로, 최근 1개만 유지한다.
# 캐시 값: (Graph, tacit_count, mtime, triple_count_at_cache_time)
_graph_cache: dict[tuple, tuple[Graph, int, float, int]] = {}
_graph_cache_lock = threading.Lock()
# LRU 크기: T-Box/추론 그래프를 번갈아 쓰는 호출자를 위해 2로 확장.
# 각 그래프가 수 GB일 수 있으므로 3 이상은 OOM 위험.
# 환경변수 GRAPH_CACHE_MAX_ENTRIES=1로 복귀 가능(저사양 환경 호환).
_GRAPH_CACHE_MAX_ENTRIES = max(1, int(os.getenv("GRAPH_CACHE_MAX_ENTRIES", "2")))

# ── tacit 전용 그래프 캐시 ────────────────────────────────────────────
# ``load_graph`` 는 T-Box + A-Box + tacit 을 합친다. S3 후처리 스텝은 A-Box(37MB /
# 709K 트리플) 가 필요 없고 tacit 만 재면 되므로 별 캐시를 둔다.
_tacit_cache: tuple[tuple, Graph] | None = None
_tacit_cache_lock = threading.Lock()


def _tacit_dir() -> str:
    """tacit 폴더를 **호출 시점에** 해석한다.

    모듈 로드 때 바인딩한 ``SOURCE_TACIT_DIR`` 을 쓰면 ``config.SOURCE_TACIT_DIR``
    을 patch 한 호출자(테스트/도메인 이식)가 무시된다 — 2026-09-04 에 실제로 이
    로더로 옮기면서 tacit 테스트 3개가 깨져서 드러났다.
    """
    import config

    return getattr(config, "SOURCE_TACIT_DIR", SOURCE_TACIT_DIR)


def tacit_signature() -> tuple:
    """tacit 폴더의 ``(경로, (파일명, mtime, size)...)`` 서명 — 캐시 키.

    파일이 바뀌면 서명이 달라져 자동 재로드된다. mtime 만 쓰지 않는 이유는 같은
    초에 덮어써진 변경을 놓칠 수 있기 때문이다 (size 가 그 축을 보완한다).
    폴더 경로를 서명에 넣는다 — 넣지 않으면 **서로 다른 빈 폴더**가 같은 서명 ``()``
    이 되어 앞의 캐시가 잘못 재사용된다.
    """
    directory = _tacit_dir()
    out = []
    if not os.path.isdir(directory):
        return (directory,)
    for path in sorted(glob.glob(os.path.join(directory, "*.ttl"))):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        out.append((os.path.basename(path), stat.st_mtime, stat.st_size))
    return (directory, tuple(out))


def load_tacit_graph() -> Graph:
    """암묵지 TTL **만** 병합한 그래프 (T-Box·A-Box 없음).

    S3 후처리 스텝이 "이 술어를 tacit 이 실제로 몇 개 채우는가" 를 재는 데 쓴다.
    tacit 은 S3 시점에 이미 존재하는 결정적 소스 입력이라 A-Box 를 기다릴 필요가
    없다 — 이 KG 는 A-Box OP 트리플의 절반 이상이 tacit 유래이므로, tacit 을 보지
    않는 근거 판정은 그 절반을 구조적으로 놓친다.

    호출자마다 파싱하면 21K 줄 파일 8장을 반복 읽으므로 서명 기반으로 캐시한다.
    사본을 만들지 말고 이 함수를 쓸 것 — 판정 근거가 갈라지면 스텝마다 다른 답을
    낸다.
    """
    global _tacit_cache

    signature = tacit_signature()
    with _tacit_cache_lock:
        if _tacit_cache is not None and _tacit_cache[0] == signature:
            return _tacit_cache[1]

    merged = _new_graph()
    for path in sorted(glob.glob(os.path.join(_tacit_dir(), "*.ttl"))):
        try:
            fast_parse_turtle(merged, path)
        except Exception as exc:  # noqa: BLE001 — 측정이 파이프라인을 막지 않는다
            logger.debug("tacit parse skip (%s): %s", path, exc)

    with _tacit_cache_lock:
        _tacit_cache = (signature, merged)
    return merged

# T-Box 전용 캐시 (load_graph_with_tbox에서 공유) — 크기가 작아 별도 유지
_tbox_cache: tuple[str, float, Graph] | None = None
_tbox_cache_lock = threading.Lock()


def _source_mtime(use_inferred: bool, tbox_path: str, abox_path: str) -> float:
    """캐시 키에 대응하는 소스 파일의 최신 mtime을 계산한다."""
    if use_inferred:
        return os.path.getmtime(INFERRED_PATH) if os.path.exists(INFERRED_PATH) else 0
    mtime = 0.0
    for p in (tbox_path or TBOX_PATH, abox_path or ABOX_PATH):
        if os.path.exists(p):
            mtime = max(mtime, os.path.getmtime(p))
    if os.path.isdir(SOURCE_TACIT_DIR):
        for f in glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")):
            mtime = max(mtime, os.path.getmtime(f))
    return mtime


def invalidate_graph_cache() -> None:
    """그래프 메모리 캐시를 무효화한다. 그래프를 수정한 후 호출."""
    global _tbox_cache
    with _graph_cache_lock:
        _graph_cache.clear()
    with _tbox_cache_lock:
        _tbox_cache = None


def load_graph(*, use_inferred: bool = False,
               tbox_path: str = "", abox_path: str = "") -> tuple[Graph, int]:
    """T-Box + A-Box + 암묵지를 하나의 그래프로 로드한다.

    mtime 기반 메모리 캐시로 반복 로드를 건너뛴다.

    WARNING: 반환된 Graph는 캐시 참조. 수정(expand, add, remove)하려면
    반드시 invalidate_graph_cache()를 호출하거나 copy()를 사용하세요.

    Args:
        use_inferred: True면 all_inferred.ttl을 사용 (이미 병합된 추론 결과).
        tbox_path: T-Box 경로 오버라이드.
        abox_path: A-Box 경로 오버라이드.

    Returns:
        (병합 그래프, 로드된 암묵지 파일 수)
    """
    key = (use_inferred, tbox_path, abox_path)

    with _graph_cache_lock:
        mtime = _source_mtime(use_inferred, tbox_path, abox_path)
        cached = _graph_cache.get(key)
        if cached is not None:
            cached_g, cached_tacit, cached_mtime, cached_len = cached
            if cached_mtime == mtime:
                current_len = len(cached_g)
                if current_len != cached_len:
                    logger.warning(
                        "그래프 캐시 변조 감지: 캐시 시점 %d → 현재 %d triples. "
                        "캐시를 무효화합니다. invalidate_graph_cache()를 호출하세요.",
                        cached_len, current_len,
                    )
                    del _graph_cache[key]
                else:
                    logger.debug("그래프 캐시 히트: %d triples", current_len)
                    return cached_g, cached_tacit

    if use_inferred and os.path.exists(INFERRED_PATH):
        start = time.monotonic()
        g = _new_graph()
        # Oxigraph bulk_load (Rust) 로 1.5M triples 24.8s → 2.2s (~11×)
        fast_parse_turtle(g, INFERRED_PATH)
        triple_count = len(g)
        logger.info("TTL 파싱: %s (%d triples, %.1fs)", INFERRED_PATH, triple_count, time.monotonic() - start)
        _store_in_cache(key, g, 0, mtime, triple_count)
        return g, 0

    tbox = tbox_path or TBOX_PATH
    abox = abox_path or ABOX_PATH

    start = time.monotonic()
    g = _new_graph()
    if os.path.exists(tbox):
        fast_parse_turtle(g, tbox)
    if os.path.exists(abox):
        fast_parse_turtle(g, abox)

    tacit_count = 0
    if os.path.isdir(SOURCE_TACIT_DIR):
        for ttl_file in sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl"))):
            fast_parse_turtle(g, ttl_file)
            tacit_count += 1

    # A-Box 데이터가 있으면 항상 inverse 트리플 보완 (암묵지 유무 무관)
    if os.path.exists(abox) or tacit_count > 0:
        ensure_inverse_triples(g, tbox_path=tbox or TBOX_PATH)

    triple_count = len(g)
    logger.info("TTL 병합 파싱: %d triples (%.1fs)", triple_count, time.monotonic() - start)
    _store_in_cache(key, g, tacit_count, mtime, triple_count)
    return g, tacit_count


def load_tbox(tbox_path: str = "") -> Graph:
    """T-Box만 파싱하여 반환 (mtime 기반 메모리 캐시).

    validate_kg처럼 병합 그래프와 별도로 T-Box 전용 쿼리가 필요한 경우
    사용. load_graph의 메인 캐시와 독립.
    """
    global _tbox_cache
    path = tbox_path or TBOX_PATH
    if not os.path.exists(path):
        return _new_graph()
    mtime = os.path.getmtime(path)
    with _tbox_cache_lock:
        if _tbox_cache is not None:
            cached_path, cached_mtime, cached_g = _tbox_cache
            if cached_path == path and cached_mtime == mtime:
                return cached_g
    g = _new_graph()
    g.parse(path, format="turtle")
    with _tbox_cache_lock:
        _tbox_cache = (path, mtime, g)
    return g


def load_graph_with_tbox(
    *, use_inferred: bool = False,
    tbox_path: str = "", abox_path: str = "",
) -> tuple[Graph, Graph, int]:
    """load_graph + T-Box 참조를 함께 반환.

    기존 load_graph를 그대로 호출한 뒤 load_tbox()로 T-Box를 얻어
    **재파싱 없이** 반환. validate_kg처럼 병합 그래프와 T-Box 양쪽을
    모두 쓰는 호출자가 이 함수를 쓰면 T-Box 중복 파싱을 제거할 수 있다.

    Returns:
        (merged_graph, tbox_graph, tacit_count)
    """
    g, tacit = load_graph(
        use_inferred=use_inferred,
        tbox_path=tbox_path, abox_path=abox_path,
    )
    tbox = load_tbox(tbox_path)
    return g, tbox, tacit


def invalidate_tbox_cache() -> None:
    global _tbox_cache
    with _tbox_cache_lock:
        _tbox_cache = None


def _store_in_cache(key: tuple, g: Graph, tacit_count: int, mtime: float, triple_count: int) -> None:
    """그래프를 캐시에 저장하되, LRU로 용량 제한 (대용량 그래프 중복 상주 방지)."""
    import gc
    with _graph_cache_lock:
        # 현재 크기를 고려한 LRU eviction. max_entries에 도달하면 가장 오래된 것부터 제거.
        # (dict는 파이썬 3.7+에서 삽입 순서 보존 — 이걸 그대로 사용.)
        while len(_graph_cache) >= _GRAPH_CACHE_MAX_ENTRIES and key not in _graph_cache:
            oldest = next(iter(_graph_cache))
            del _graph_cache[oldest]
        _graph_cache[key] = (g, tacit_count, mtime, triple_count)
    gc.collect()
