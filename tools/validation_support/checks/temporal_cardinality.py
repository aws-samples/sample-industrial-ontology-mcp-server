"""Temporal/cardinality/completeness check 그룹.

시간 순서, OWL 카디널리티 제약, FunctionalProperty 위반,
AllDisjointClasses 위반, 프로퍼티 완전성, 추론 sanity.

공유 자원: SharedCheckContext.ns, superclass_map, instance_types.
설정 주입: inferred_path (inference_sanity용).
"""
from __future__ import annotations

import csv as _csv
import glob as _glob
import logging
import os
from collections import defaultdict
from functools import lru_cache

from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef
from rdflib.collection import Collection

from config import INFERRED_PATH, SOURCE_RAWDATA_DIR
from domain.namespaces import DOMAIN_NS, sparql_iri
from domain.rules_paths import RULES_ROOT, rules_path
from tools.validation_support.common import (
    SharedCheckContext,
    local,
    validate_prop_name,
)
from tools.validation_support.thresholds import get_tier_thresholds

DCTERMS = Namespace("http://purl.org/dc/terms/")

# Fallback only. The authoritative list lives in the A-Box generator; see
# _null_markers().
_FALLBACK_NULL_MARKERS: frozenset[str] = frozenset(
    {"null", "n/a", "#n/a", "na", "nan", "none", "undefined", "미정", "해당없음"},
)


@lru_cache(maxsize=1)
def _null_markers() -> frozenset[str]:
    """Return the NULL-sentinel set the A-Box generator actually applies.

    Loader and validator must agree on what counts as a value. Measured
    2026-07-26 they did not: this module treated only ``null``/``n/a``/``nan`` as
    empty, so one identifier column — 약 7천 of 약 1.3만 rows literally
    holding the string ``undefined`` — measured as 100% filled in CSV while the
    loader (correctly) skipped those rows. The 49.1% A-Box fill was then reported
    as a load loss (S9 FAIL) when nothing had been lost.

    ``"-"`` is deliberately excluded here: it is filtered per column, not
    globally (sign columns keep it as data), so folding it into a global marker
    set would re-introduce the opposite error.
    """
    try:
        from tools.abox_generation import _AMBIGUOUS_SENTINELS, _NULL_SENTINELS
        return frozenset(_NULL_SENTINELS) - frozenset(_AMBIGUOUS_SENTINELS)
    except Exception as e:                                   # pragma: no cover
        logger.debug("falling back to local NULL markers: %s", e)
        return _FALLBACK_NULL_MARKERS


def _is_filled(raw: object) -> bool:
    """True when a CSV cell holds a real value (not blank, not a NULL sentinel)."""
    text = (raw or "").strip() if isinstance(raw, str) else ""
    return bool(text) and text.lower() not in _null_markers()


logger = logging.getLogger(__name__)


def _rules_dir() -> str:
    """Absolute path of the repo's ``rules/`` directory."""
    return RULES_ROOT


def _mapping_signature() -> tuple:
    """``(path, mtime, size)`` of table_class_mapping.json — cache key.

    A bare ``lru_cache(maxsize=1)`` on a no-argument loader keys on the empty
    tuple, so the first result is pinned for the process lifetime: editing the
    mapping mid-session (the ordinary "S9 FAIL → fix mapping → re-run" loop)
    changed what S7 read but not what these checks read, and the stale baseline
    turned a real load loss into a PASS (2026-08-08 규명). Passing a signature as
    a real argument makes the cache invalidate when the file changes.
    """
    path = rules_path("table_class_mapping.json")
    try:
        stat = os.stat(path)
    except OSError:
        return (path, 0.0, 0)
    return (path, stat.st_mtime, stat.st_size)


def _rawdata_signature() -> tuple:
    """``(dir, (path, mtime, size)...)`` of the CSV inputs — cache key.

    Same rationale as :func:`_mapping_signature`. The directory itself is part of
    the key because tests monkeypatch ``SOURCE_RAWDATA_DIR``; keying only on file
    stats would return one temp dir's result for another.

    Reads the **module global**, not ``config``, because that is what the cached
    readers below use — a key derived from a different path than the one actually
    read is the exact mistake that made these caches wrong in the first place.
    """
    rawdata_dir = globals().get("SOURCE_RAWDATA_DIR") or ""
    if not os.path.isdir(rawdata_dir):
        return ("__missing__", rawdata_dir)
    entries: list[tuple] = [("__dir__", rawdata_dir, 0)]
    for path in sorted(_glob.glob(os.path.join(rawdata_dir, "*.csv"))):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append((path, stat.st_mtime, stat.st_size))
    return tuple(entries)


def invalidate_csv_caches() -> None:
    """Drop every CSV/mapping-derived cache in this module.

    Signature keys make this unnecessary in production (a changed file changes
    the key). It exists for tests that swap ``SOURCE_RAWDATA_DIR`` to a temp dir
    whose files can share mtime+size with a previous one.
    """
    for fn in (
        _table_class_map_cached,
        _csv_class_column_filled_count_cached,
        _csv_class_column_fill_rate_cached,
        _csv_column_fill_rate_cached,
    ):
        fn.cache_clear()


def _table_class_map() -> dict[str, str]:
    """Load rules/domain/table_class_mapping.json as {csv_basename_without_ext: ClassName}.

    Needed so a column's fill rate can be scoped to the class that actually
    sources it. Deployment prefixes (``med:Order``) are stripped to local names.
    """
    return _table_class_map_cached(_mapping_signature())


@lru_cache(maxsize=4)
def _table_class_map_cached(signature: tuple) -> dict[str, str]:
    from domain.table_mapping import load_table_class_mapping
    return load_table_class_mapping(_rules_dir())


def _csv_class_column_filled_count() -> dict[tuple[str, str], int]:
    """Return {(ClassName, UPPER_COLUMN): absolute filled-row count}.

    Cache invalidates on CSV/mapping change — see :func:`_rawdata_signature`.

    The *ratio* baseline breaks down at the rounding boundary because the A-Box
    denominator is instance count, not CSV row count, and inference can add an
    instance. Measured 2026-07-26:
    ``OrderToleranceSpec.MEASURE_COL_2`` loaded 약 5천 values —
    exactly the CSV's 약 5천 — yet 약 5천/약 1.2만 = 41.05% rounded below the
    약 5천/약 1.2만 = 41.06% baseline and was reported as a load loss.

    Comparing numerators makes the "nothing was lost" case exact regardless of
    denominator drift.
    """
    return _csv_class_column_filled_count_cached(
        _rawdata_signature(), _mapping_signature(),
    )


@lru_cache(maxsize=4)
def _csv_class_column_filled_count_cached(
    rawdata_sig: tuple, mapping_sig: tuple,
) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    table_to_class = _table_class_map()
    if not table_to_class:
        return counts
    try:
        if not os.path.isdir(SOURCE_RAWDATA_DIR):
            return counts
        for csv_path in _glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")):
            table = os.path.splitext(os.path.basename(csv_path))[0]
            cls = table_to_class.get(table)
            if not cls:
                continue
            try:
                with open(csv_path, encoding="utf-8-sig") as f:
                    rows = list(_csv.DictReader(f))
            except Exception as e:
                logger.debug("CSV read failed for %s: %s", csv_path, e)
                continue
            if not rows:
                continue
            for col in rows[0]:
                if not col:
                    continue
                counts[(cls, col.strip().upper())] = sum(
                    1 for r in rows if _is_filled(r.get(col)))
    except Exception as e:
        logger.debug("_csv_class_column_filled_count failed: %s", e)
    return counts


def _csv_class_column_fill_rate() -> dict[tuple[str, str], float]:
    """Return {(ClassName, UPPER_COLUMN): fill rate} scoped to the owning table.

    Cache invalidates on CSV/mapping change — see :func:`_rawdata_signature`.

    The global (unscoped) index in :func:`_csv_column_fill_rate` collapses a
    column name that appears in several tables down to the *lowest* observed
    rate. That is safe for raising alarms but unsafe for waiving them: measured
    2026-07-26, ``TIMESTAMP_COL_1`` is 2.0% filled in SRC_TBL_13 but 0.11% in
    another table, so the unscoped baseline would relax MaterialSpecA's
    threshold to 0.11% and hide a genuine 20x load loss.

    Scoping by class removes the ambiguity wherever the table→class mapping
    covers the class; callers fall back to the unscoped index otherwise.
    """
    return _csv_class_column_fill_rate_cached(
        _rawdata_signature(), _mapping_signature(),
    )


@lru_cache(maxsize=4)
def _csv_class_column_fill_rate_cached(
    rawdata_sig: tuple, mapping_sig: tuple,
) -> dict[tuple[str, str], float]:
    scoped: dict[tuple[str, str], float] = {}
    table_to_class = _table_class_map()
    if not table_to_class:
        return scoped
    try:
        if not os.path.isdir(SOURCE_RAWDATA_DIR):
            return scoped
        for csv_path in _glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")):
            table = os.path.splitext(os.path.basename(csv_path))[0]
            cls = table_to_class.get(table)
            if not cls:
                continue
            try:
                with open(csv_path, encoding="utf-8-sig") as f:
                    rows = list(_csv.DictReader(f))
            except Exception as e:
                logger.debug("CSV read failed for %s: %s", csv_path, e)
                continue
            if not rows:
                continue
            total = len(rows)
            for col in rows[0]:
                if not col:
                    continue
                filled = sum(1 for r in rows if _is_filled(r.get(col)))
                scoped[(cls, col.strip().upper())] = filled / total if total else 0.0
    except Exception as e:
        logger.debug("_csv_class_column_fill_rate failed: %s", e)
    return scoped


def _csv_column_fill_rate() -> dict[str, float]:
    """Return {column_key: observed fill rate [0.0, 1.0]} from CSV sources.

    Cache invalidates on CSV change — see :func:`_rawdata_signature`.

    When a CSV column is null for a significant fraction of rows (e.g.
    NDT_Results.Defect_Location is null on Pass samples), the corresponding
    DP's downstream completeness is legitimately capped at (1 - null_rate).
    Treating such DPs with the same strict threshold as always-populated PKs
    is a false positive.

    Each column is registered under two keys so both lookup paths hit:
      * ``UPPER_WITH_UNDERSCORES`` — matched against the DP's ``dcterms:source``
        annotation (the authoritative CSV column code).
      * ``lowercasenounderscore`` — legacy fallback for T-Boxes with no source
        annotation, where the DP local-name is a transliteration of the column.
    """
    return _csv_column_fill_rate_cached(_rawdata_signature())


@lru_cache(maxsize=4)
def _csv_column_fill_rate_cached(rawdata_sig: tuple) -> dict[str, float]:
    rates: dict[str, float] = {}

    def _register(key: str, rate: float) -> None:
        # Same column name in several tables → keep the lowest rate
        # (conservative: never raise a threshold above what any table sustains).
        prev = rates.get(key)
        if prev is None or rate < prev:
            rates[key] = rate

    try:
        if not os.path.isdir(SOURCE_RAWDATA_DIR):
            return rates
        for csv_path in _glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")):
            try:
                with open(csv_path, encoding="utf-8-sig") as f:
                    reader = _csv.DictReader(f)
                    rows = list(reader)
            except Exception as e:
                logger.debug("CSV read failed for %s: %s", csv_path, e)
                continue
            if not rows:
                continue
            total = len(rows)
            for col in rows[0]:
                if not col:
                    continue
                filled = sum(1 for r in rows if _is_filled(r.get(col)))
                rate = filled / total if total else 0.0
                _register(col.strip().upper(), rate)
                _register(col.replace("_", "").lower(), rate)
    except Exception as e:
        logger.debug("_csv_column_fill_rate failed: %s", e)
    return rates


def _dp_source_columns(tbox: Graph) -> dict[str, str]:
    """Return {dp_local_name: UPPER CSV column code} from ``dcterms:source``.

    Under the Path B naming policy every DP is class-prefixed
    (``materialSpecATypeCol1``), so the DP local-name can never
    be transliterated back to its column code (``TYPE_COL_1``).
    Completeness thresholds must therefore be resolved through the explicit
    provenance annotation, not through name similarity.

    A DP claiming several columns is skipped: without a single authoritative
    column there is no defensible fill-rate baseline, and picking one arbitrarily
    could silently relax the threshold using an unrelated column's sparsity.
    """
    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    for dp in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not (isinstance(dp, URIRef) and str(dp).startswith(DOMAIN_NS)):
            continue
        name = local(str(dp))
        for src in tbox.objects(dp, DCTERMS.source):
            col = str(src).strip().upper()
            if not col:
                continue
            if name in mapping and mapping[name] != col:
                ambiguous.add(name)
            mapping[name] = col
    for name in ambiguous:
        mapping.pop(name, None)
    if ambiguous:
        logger.debug("DPs with multiple dcterms:source skipped: %d", len(ambiguous))
    return mapping


def check_inference_sanity(
    g_inferred: Graph, raw_count: int | Graph,
    *, inferred_path: str | None = None,
) -> dict[str, object]:
    """6. 추론 sanity check — 추론 후 트리플 증가 확인.

    ## 이 체크는 **추론 그래프를 받았을 때만** 의미가 있다 (2026-08-31)

    ``validate_kg`` 는 기본이 ``use_inferred=False`` 이고 그 모드의 ``g`` 는 **추론
    전** 병합 그래프다. 그것을 자기 자신과 비교하면 증가가 항상 0 → 영구 FAIL 이고,
    ``passed = (passed == total)`` 이므로 **기본 모드는 절대 통과할 수 없었다**.
    게다가 ``mutation_runner`` 가 그 모드를 하드코딩해 S9.5 감사에서 이 체크는
    baseline 이 이미 FAIL — 어떤 mutant 도 잡지 못했다.

    그래서 "받은 그래프가 추론 그래프인가" 를 먼저 판정한다. 아니면 **판정하지 않고
    그 사실을 보고**한다 (``applicable: false``) — 미측정을 FAIL 로 내면 그 게이트는
    상시 빨간불이 되어 아무도 보지 않게 된다 (이 리포의 "0회 발동은 0회로 보고하라").

    판정 기준은 크기다: 추론 그래프는 raw 보다 커야 한다. 같거나 작으면 호출자가
    raw 를 넘긴 것이므로 (또는 추론이 실제로 아무것도 만들지 못한 것이므로) 그
    구분을 ``reason`` 으로 남긴다.
    """
    if isinstance(raw_count, Graph):
        raw_count = len(raw_count)
    inferred_count = len(g_inferred)

    path = inferred_path if inferred_path is not None else INFERRED_PATH
    if inferred_count == 0 and not os.path.exists(path):
        return {
            "name": "추론 sanity check",
            "passed": False,
            "message": "추론 결과 파일 없음 (all_inferred.ttl)",
            "raw_triples": raw_count,
            "inferred_triples": 0,
            "applicable": True,
        }

    if raw_count <= 0:
        # raw 를 알 수 없다 (매니페스트 부재/낡음). 판정하지 않는다 — 0 을 분모로
        # 쓰면 "증가 = 전체" 로 부풀어 거짓 PASS 가 된다.
        return {
            "name": "추론 sanity check",
            "passed": True,
            "applicable": False,
            "reason": (
                "추론 전 트리플 수를 알 수 없다 (inference_loss_manifest 부재 또는 "
                "all_inferred.ttl 보다 낡음) — 추론을 다시 돌리면 판정된다"
            ),
            "raw_triples": 0,
            "inferred_triples": inferred_count,
        }

    increase = inferred_count - raw_count
    ratio = round(increase / max(raw_count, 1) * 100, 1)

    if increase <= 0:
        # 받은 그래프가 raw 와 같거나 작다 — 호출자가 추론 전 그래프를 넘겼다는
        # 뜻이다 (validate_kg 기본 모드). 그 경우 이 축은 **적용 불가** 이고,
        # FAIL 로 내면 게이트가 영구 빨간불이 된다.
        near_raw = abs(increase) <= max(1, raw_count // 1000)   # ±0.1%
        return {
            "name": "추론 sanity check",
            "passed": True,
            "applicable": False,
            "reason": (
                "받은 그래프가 추론 전 그래프다 (use_inferred=False) — 이 축은 "
                "use_inferred=True 에서만 판정된다"
                if near_raw else
                f"추론 후 트리플이 줄었다 ({increase:+,}) — 추론 산출물을 확인하라"
            ),
            "raw_triples": raw_count,
            "inferred_triples": inferred_count,
            "increase": increase,
            "increase_ratio": f"{ratio}%",
        }

    return {
        "name": "추론 sanity check",
        "passed": True,
        "applicable": True,
        "raw_triples": raw_count,
        "inferred_triples": inferred_count,
        "increase": increase,
        "increase_ratio": f"{ratio}%",
    }


def check_property_completeness(
    g: Graph, tbox: Graph, *, class_tiers: dict[str, str] | None = None,
) -> dict[str, object]:
    """12. 프로퍼티별 완전성 — 클래스 인스턴스 대비 DP 값 채움 비율."""
    class_dps: dict[str, list[str]] = {}
    dp_uris: dict[str, str] = {}
    for prop in tbox.subjects(RDF.type, OWL.DatatypeProperty):
        if not isinstance(prop, URIRef) or not str(prop).startswith(DOMAIN_NS):
            continue
        name = local(str(prop))
        dp_uris[name] = str(prop)
        for domain_cls in tbox.objects(prop, RDFS.domain):
            if isinstance(domain_cls, URIRef) and str(domain_cls).startswith(DOMAIN_NS):
                cls_name = local(str(domain_cls))
                class_dps.setdefault(cls_name, []).append(name)

    if not class_dps:
        return {
            "name": "프로퍼티별 완전성",
            "passed": True,
            "message": "T-Box에 domain이 정의된 DatatypeProperty 없음",
            "checked_classes": 0,
            "incomplete_properties": [],
        }

    # 기본 최소 채움 비율 30%. 일부 optional DP (defect 기록, 특이사항 등) 는
    # 자연스럽게 0~50% 구간이어도 정상 데이터. 환경변수 PROP_COMPLETENESS_MIN_PCT
    # 로 조정 가능.
    import os as _os
    min_completeness = int(_os.getenv("PROP_COMPLETENESS_MIN_PCT", "30"))
    if class_tiers is not None:
        tier_thresholds = get_tier_thresholds()
        active_tiers = set(class_tiers.values())
        tier_cov = [
            tier_thresholds[t]["property_coverage"]
            for t in active_tiers if t in tier_thresholds
        ]
        if tier_cov:
            min_completeness = min(tier_cov)

    incomplete = []
    checked_classes = 0

    # Count instances where ?cls is the *most specific* asserted type (no strict
    # subtype of ?cls is also asserted for the same instance). OWL RL inflates
    # inst_count by propagating rdfs:subClassOf through A-Box, which makes DP
    # completeness collapse: an abstract parent with 1,000 inherited instances
    # will report 10% fill even if every direct-typed instance has the DP.
    #
    # Earlier implementation used SPARQL `FILTER NOT EXISTS { ... subClassOf+ ... }`
    # for both counts. That made rdflib walk the full transitive-subclass graph
    # per candidate binding, which on a 6.6M-triple inferred graph degenerated
    # to >1h (O(triples × classes × depth)). We now collect instance types and
    # DP-filled subjects in a single SPARQL pass each, pre-compute strict
    # subclass sets from the T-Box once, and resolve the most-specific type in
    # Python. Complexity drops to O(triples + classes²).
    strict_subclasses: dict[str, set[str]] = {}
    for sub, sup in tbox.subject_objects(RDFS.subClassOf):
        if not (isinstance(sub, URIRef) and isinstance(sup, URIRef)):
            continue
        if not (str(sub).startswith(DOMAIN_NS) and str(sup).startswith(DOMAIN_NS)):
            continue
        if sub == sup:
            continue
        strict_subclasses.setdefault(local(str(sup)), set()).add(local(str(sub)))
    # Transitive closure (reflexive-free).
    changed = True
    while changed:
        changed = False
        for _parent, subs in strict_subclasses.items():
            grandkids: set[str] = set()
            for sub in subs:
                grandkids.update(strict_subclasses.get(sub, ()))
            new = grandkids - subs
            if new:
                subs.update(new)
                changed = True

    # Single-pass SPARQL: per instance, collect all asserted steel classes.
    # GROUP_CONCAT keeps the scan linear; the most-specific resolution happens
    # in Python using strict_subclasses.
    inst_types: dict[str, set[str]] = {}
    q_inst = (
        "SELECT ?s (GROUP_CONCAT(DISTINCT STR(?cls); separator=\"|\") AS ?types) "
        "WHERE { "
        "  ?s a ?cls . "
        "  FILTER(isIRI(?s)) "
        f"  FILTER(STRSTARTS(STR(?cls), \"{DOMAIN_NS}\")) "
        "} GROUP BY ?s"
    )
    for row in g.query(q_inst):
        s, types_str = row[0], str(row[1])
        cls_names = {t.split("#")[-1] for t in types_str.split("|") if t}
        inst_types[str(s)] = cls_names

    def _most_specific(cls_set: set[str]) -> set[str]:
        """Drop any class that has a strict subclass also present in cls_set."""
        return {c for c in cls_set
                if not (strict_subclasses.get(c, set()) & cls_set)}

    # Known class local names — used to anchor URI-based authoritative typing.
    known_class_names = set()
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if isinstance(cls, URIRef) and str(cls).startswith(DOMAIN_NS):
            known_class_names.add(local(str(cls)))

    def _authoritative_class(inst_uri: str) -> str | None:
        """Return the primary class for inst_uri based on its URI convention.

        Instances follow the `<ClassName>_<pk>[_<suffix>]` pattern emitted by
        A-Box generation. When the URI prefix matches a known T-Box class, that
        class is authoritative — any additional class labels attached by the
        reasoner (e.g. ProductionResult propagated onto ProcessSteelmakingFurnace
        instances via producesProduct restriction chains) must not inflate
        completeness statistics for those foreign classes.
        """
        ln = inst_uri.split("#")[-1].split("/")[-1]
        # Greedy longest-prefix match so "MaintenanceHistory_..." wins over
        # ambiguous shorter prefixes.
        best: str | None = None
        for cand in known_class_names:
            if (ln == cand or ln.startswith(cand + "_")) and (best is None or len(cand) > len(best)):
                best = cand
        return best

    inst_counts: dict[str, int] = {}
    inst_primary: dict[str, set[str]] = {}
    for _s, cls_set in inst_types.items():
        anchor = _authoritative_class(_s)
        if anchor and anchor in cls_set:
            primary = {anchor}
        else:
            primary = _most_specific(cls_set)
        inst_primary[_s] = primary
        for c in primary:
            inst_counts[c] = inst_counts.get(c, 0) + 1

    # Filled counts: for each DP triple, attribute to every most-specific steel
    # class the subject carries.
    filled_by_cls_dp: dict[tuple[str, str], int] = {}
    # Track per-(cls, dp) the set of subjects to avoid double-counting when a
    # subject has several DP triples for the same property.
    seen: dict[tuple[str, str], set[str]] = {}
    q_fill = (
        "SELECT ?s ?p WHERE { "
        "  ?s ?p ?v . "
        "  FILTER(isLiteral(?v)) "
        f"  FILTER(STRSTARTS(STR(?p), \"{DOMAIN_NS}\")) "
        "}"
    )
    for row in g.query(q_fill):
        s_uri, p_uri = str(row[0]), str(row[1])
        primary = inst_primary.get(s_uri)
        if not primary:
            continue
        p_name = p_uri.split("#")[-1]
        for c in primary:
            key = (c, p_name)
            bucket = seen.setdefault(key, set())
            if s_uri not in bucket:
                bucket.add(s_uri)
                filled_by_cls_dp[key] = filled_by_cls_dp.get(key, 0) + 1

    # 0% 채움 DP 는 "데이터 소스 자체가 없는" 경우가 많음 (T-Box 는 선언했지만
    # CSV 에 해당 컬럼이 없는 DP, 혹은 optional DP 가 전혀 값이 안 들어온 경우).
    # 이런 DP 는 incomplete 로는 보고하되 PASS 판정에선 제외.
    missing_source: list[dict] = []
    csv_fill_rates = _csv_column_fill_rate()
    csv_scoped_rates = _csv_class_column_fill_rate()
    csv_filled_counts = _csv_class_column_filled_count()
    dp_to_column = _dp_source_columns(tbox)
    threshold_relaxed = 0
    fully_loaded = 0
    # Classes with no CSV table of their own: their instances are derived from
    # code values in other tables (e.g. FacilityOperation, Yard), so there is no
    # source column whose fill rate could serve as a baseline. Judging them
    # against the generic tier threshold reports derived-dimension sparsity as a
    # load defect (measured 2026-07-26: FacilityOperation.facilityOperationName
    # 8/17 → FAIL with nothing to fix). Report, don't fail.
    mapped_classes = set(_table_class_map().values())
    derived_classes: list[dict] = []
    for cls_name, dp_names in class_dps.items():
        validate_prop_name(cls_name)
        inst_count = inst_counts.get(cls_name, 0)
        if inst_count == 0:
            continue
        # "소스 테이블 없는 파생 분류" 로 제외하려면 두 조건이 함께 참이어야 한다:
        #   (a) 매핑에 이 클래스의 테이블이 없다, 그리고
        #   (b) 이 클래스의 DP 가 출처 컬럼을 **선언했다** (dcterms:source).
        #
        # (b) 가 필요한 이유: 출처 선언이 있으면 "컬럼은 지목하는데 그 컬럼을 담은
        # 테이블이 이 클래스엔 없다" 가 성립하므로 기준선이 없다고 단정할 수 있다.
        # 선언이 아예 없으면 (합성 그래프·출처 미표기 T-Box) 파생인지 알 수 없고,
        # 그때 제외하면 완전성 판정 자체가 조용히 사라져 PASS 로 보고된다 —
        # 이 check 가 막으려는 바로 그 미탐이다. 그런 경우는 일반 임계치로 판정한다.
        if (mapped_classes
                and cls_name not in mapped_classes
                and any(dp in dp_to_column for dp in dp_names)):
            derived_classes.append({"class": cls_name, "instances": inst_count,
                                    "reason": "no_source_table"})
            continue
        checked_classes += 1
        for dp_name in dp_names:
            filled = filled_by_cls_dp.get((cls_name, dp_name), 0)
            completeness_pct = round(filled / inst_count * 100, 1)
            if filled == 0:
                # 데이터 소스 자체 없음 — PASS 대상 제외, 별도 집계
                missing_source.append({
                    "class": cls_name,
                    "property": dp_name,
                    "instances": inst_count,
                    "reason": "no_source_data",
                })
                continue
            # Per-DP threshold adjustment for conditionally-nullable columns.
            # NDTResults.Defect_Location is null on Pass samples by design —
            # the upstream CSV shows a ~38% fill rate, so a 30%/50%/80% tier
            # threshold would flag legitimate sparse data as incomplete.
            # The effective threshold is min(tier_threshold, CSV fill rate),
            # so optional columns never fail unless they dip below their own
            # natural baseline.
            #
            # Resolution order matters, narrowest source first:
            #   1. (class, column) — the column in *this* class's own table.
            #   2. column code alone — column shared by tables, lowest rate wins.
            #   3. DP local-name transliteration — legacy pre-Path-B fallback.
            #
            # `dcterms:source` is authoritative for 1 and 2 because it is declared
            # by the T-Box generator from the column code itself; step 3 only works
            # for T-Boxes predating the class-specific (Path B) DP naming policy,
            # where a class-prefixed name like
            # `materialSpecATypeCol1` can never be matched back to
            # `TYPE_COL_1`.
            source_col = dp_to_column.get(dp_name)
            csv_rate = None
            if source_col is not None:
                csv_rate = csv_scoped_rates.get((cls_name, source_col))
                if csv_rate is None:
                    csv_rate = csv_fill_rates.get(source_col)
            if csv_rate is None:
                csv_rate = csv_fill_rates.get(dp_name.lower())
            effective_threshold = min_completeness
            if csv_rate is not None and csv_rate < 1.0:
                # CSV fill-rate is fraction; compare to pct.
                csv_threshold = round(csv_rate * 100, 1)
                if csv_threshold < effective_threshold:
                    effective_threshold = csv_threshold
                    threshold_relaxed += 1
            if completeness_pct < effective_threshold:
                # Exact-count escape hatch: if the A-Box holds every value the
                # CSV had, nothing was lost and the percentage shortfall is a
                # denominator artefact (inference-added instances / rounding).
                csv_filled = None
                if source_col is not None:
                    csv_filled = csv_filled_counts.get((cls_name, source_col))
                if csv_filled is not None and filled >= csv_filled:
                    fully_loaded += 1
                    continue
                incomplete.append({
                    "class": cls_name,
                    "property": dp_name,
                    "instances": inst_count,
                    "filled": filled,
                    "csv_filled": csv_filled,
                    "completeness_pct": completeness_pct,
                    "effective_threshold": effective_threshold,
                    "source_column": source_col,
                })

    return {
        "name": "프로퍼티별 완전성",
        "passed": len(incomplete) == 0,
        "checked_classes": checked_classes,
        "incomplete_count": len(incomplete),
        "incomplete_properties": incomplete[:20],
        "missing_source_count": len(missing_source),
        "missing_source_sample": missing_source[:10],
        # Diagnostics: how the sparse-column exemption was resolved. A large
        # dp_source_annotated with threshold_relaxed == 0 means the annotation
        # is present but points at columns absent from the CSV dir.
        "dp_source_annotated": len(dp_to_column),
        "threshold_relaxed": threshold_relaxed,
        # Classes excluded for lack of a source table (derived dimensions).
        "derived_classes_skipped": sorted(d["class"] for d in derived_classes),
        # DPs below threshold but holding every value the CSV had (denominator
        # artefact, not loss).
        "fully_loaded_below_threshold": fully_loaded,
    }


def check_functional_violations(g: Graph, tbox: Graph) -> dict[str, object]:
    """17. Functional Property 위반 — 2개 이상 값이 있는 인스턴스 탐지 (Färber 2018)."""
    functional_prop_uris: list[URIRef] = []
    for prop in tbox.subjects(RDF.type, OWL.FunctionalProperty):
        if isinstance(prop, URIRef) and str(prop).startswith(DOMAIN_NS):
            functional_prop_uris.append(prop)

    if not functional_prop_uris:
        return {
            "name": "Functional Property 위반",
            "passed": True,
            "message": "T-Box에 FunctionalProperty 선언 없음",
            "checked_properties": 0,
            "violations": [],
        }

    violations = []
    checked = len(functional_prop_uris)
    values_block = " ".join(sparql_iri(u) for u in functional_prop_uris)
    q = (
        "SELECT ?p ?s (COUNT(?o) AS ?cnt) WHERE { "
        f"  VALUES ?p {{ {values_block} }} "
        "  ?s ?p ?o . "
        "} GROUP BY ?p ?s HAVING (COUNT(?o) > 1)"
    )
    per_prop_kept: dict[str, int] = defaultdict(int)
    for row in g.query(q):
        p, s, cnt = row[0], row[1], row[2]
        prop_name = local(str(p))
        if per_prop_kept[prop_name] >= 5:
            continue
        per_prop_kept[prop_name] += 1
        violations.append({
            "property": prop_name,
            "subject": local(str(s)),
            "value_count": int(cnt),
        })

    return {
        "name": "Functional Property 위반",
        "passed": len(violations) == 0,
        "checked_properties": checked,
        "functional_property_count": len(functional_prop_uris),
        "violation_count": len(violations),
        "violations": violations[:20],
    }


def check_cardinality_constraints(
    g: Graph, tbox: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """19. OWL 카디널리티 제약 위반 — min/max/exactCardinality."""
    if shared is None:
        shared = SharedCheckContext(g, tbox)
    ns = shared.ns
    superclass_map = shared.superclass_map

    restrictions: list[dict] = []
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef) or not str(cls).startswith(ns):
            continue
        cls_name = local(str(cls))
        for parent in tbox.objects(cls, RDFS.subClassOf):
            on_prop = tbox.value(parent, OWL.onProperty)
            if on_prop is None:
                continue
            if not isinstance(on_prop, URIRef) or not str(on_prop).startswith(ns):
                continue
            prop_name = local(str(on_prop))

            for card_pred, card_type in [
                (OWL.minCardinality, "min"),
                (OWL.maxCardinality, "max"),
                (OWL.cardinality, "exact"),
            ]:
                card_val = tbox.value(parent, card_pred)
                if card_val is not None:
                    try:
                        card_int = int(card_val)
                    except (ValueError, TypeError):
                        continue
                    restrictions.append({
                        "class": cls_name,
                        "class_uri": str(cls),
                        "property": prop_name,
                        "property_uri": str(on_prop),
                        "type": card_type,
                        "value": card_int,
                    })

    if not restrictions:
        # 조기 반환에도 **같은 키 집합** 을 낸다 — 한쪽에만 있으면 소비자가 키 부재를
        # "정상" 으로 오독한다 (이 리포의 "필드 부재 ≠ 값 0" 함정. step_22f 도 같은
        # 이유로 조기 반환 경로에 자기점검 필드를 넣는다).
        return {
            "name": "카디널리티 제약 위반",
            "passed": True,
            "checked_classes": 0,
            "checked_instances": 0,
            "checked_axioms": 0,
            "violation_count": 0,
            "violated_axiom_count": 0,
            "violated_axioms": [],
            "violations": [],
            "violations_truncated": False,
        }

    subclass_map: dict[str, set[str]] = defaultdict(set)
    for child, parents in superclass_map.items():
        for parent in parents:
            subclass_map[parent].add(child)

    instance_types = shared.instance_types

    # 표본 상한은 **공리별** 이다. 예전에는 전체 20건에서 잘랐고 그 ``break`` 가
    # **restriction 루프 자체를** 빠져나갔다 — 첫 공리가 20건을 채우면 나머지 공리는
    # 아예 검사되지 않았다. 2026-08-30 실측 (maxCardinality 공리 32개, 위반 50건):
    #
    #     checked_classes: 1   ← 32개 중 1개만 봤다
    #     violations: 20       ← 전부 hasEquipmentStatus
    #     총계 필드: 없음        ← 위반 규모를 알 수 없다
    #
    # 같은 파일의 functional check 는 이미 property 별 상한(5) + ``violation_count``
    # 를 쓴다. 그 형태를 따른다: 검사는 전수, 보고는 공리별 표본.
    #
    # 응답 상한과 공리별 상한을 **함께** 정해야 한다. 처음엔 공리별 5만 두고 마지막에
    # ``violations[:20]`` 로 잘랐는데, 위반 공리가 5개면 표본 25개 중 앞 5개(= 첫
    # 공리 전부)가 잘려나가 **그 공리가 보고서에서 사라졌다** (실측: hasP0 이 0건).
    # 공리별 상한을 낮춰 맞추는 것은 정보를 더 버리는 쪽이므로, 응답 상한을 표본
    # 총량으로 올린다 — 이 리포에는 "silent truncation 은 covered everything 으로
    # 읽힌다" 는 원칙이 있다.
    _SAMPLES_PER_AXIOM = 5
    _MAX_REPORTED = 60

    violations: list[dict] = []
    violation_total = 0
    violated_axioms: set[tuple[str, str]] = set()
    checked_classes: set[str] = set()
    checked_instances: set[str] = set()

    for restr in restrictions:
        cls_name = restr["class"]
        prop_uri = URIRef(restr["property_uri"])
        card_type = restr["type"]
        card_value = restr["value"]
        checked_classes.add(cls_name)
        kept_for_axiom = 0

        target_classes = {cls_name} | subclass_map.get(cls_name, set())
        for inst_uri, types in instance_types.items():
            if not types & target_classes:
                continue
            checked_instances.add(inst_uri)

            count = sum(1 for _ in g.objects(URIRef(inst_uri), prop_uri))

            violated = False
            if card_type == "min" and count < card_value or card_type == "max" and count > card_value or card_type == "exact" and count != card_value:
                violated = True

            if violated:
                violation_total += 1
                violated_axioms.add((cls_name, restr["property"]))
                # 표본만 담고 **계속 센다** — 이 공리의 나머지 인스턴스도, 다음
                # 공리도 검사한다.
                if kept_for_axiom < _SAMPLES_PER_AXIOM:
                    kept_for_axiom += 1
                    violations.append({
                        "class": cls_name,
                        "instance": local(inst_uri),
                        "property": restr["property"],
                        "constraint": f"{card_type}Cardinality={card_value}",
                        "actual_count": count,
                    })

    return {
        "name": "카디널리티 제약 위반",
        "passed": violation_total == 0,
        "checked_classes": len(checked_classes),
        "checked_instances": len(checked_instances),
        "checked_axioms": len(restrictions),
        # 위반 **규모**. ``violations`` 는 공리별 표본이므로 그 길이로 규모를
        # 판단하면 안 된다 (functional check 의 ``violation_count`` 와 같은 역할).
        "violation_count": violation_total,
        "violated_axiom_count": len(violated_axioms),
        "violated_axioms": sorted(f"{c}.{p}" for c, p in violated_axioms)[:40],
        "violations": violations[:_MAX_REPORTED],
        # 절단 사실을 명시한다 — 조용히 자르면 "전부 봤다" 로 읽힌다.
        "violations_truncated": len(violations) > _MAX_REPORTED,
    }


def check_disjoint_class_violations(
    g: Graph, tbox: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """20. AllDisjointClasses 위반 — 동일 인스턴스가 서로소 클래스에 동시 속하는지 탐지."""
    if shared is None:
        shared = SharedCheckContext(g, tbox)
    ns = shared.ns

    disjoint_groups: list[list[str]] = []
    for dj in tbox.subjects(RDF.type, OWL.AllDisjointClasses):
        members_node = tbox.value(dj, OWL.members)
        if members_node is None:
            continue
        try:
            members = list(Collection(tbox, members_node))
        except Exception:  # noqa: BLE001 — 깨진 disjoint 목록은 해당 그룹만 건너뛴다
            continue
        group = []
        for m in members:
            if isinstance(m, URIRef) and str(m).startswith(ns):
                group.append(local(str(m)))
        if len(group) >= 2:
            disjoint_groups.append(group)

    # pairwise ``owl:disjointWith`` 도 수집한다. OWL 의 두 표준 표기 중 하나만 읽으면
    # 나머지로 선언된 disjointness 가 **전혀 검사되지 않는다**: 그룹이 0이면 아래
    # early return 이 그대로 PASS 를 반환하고, ``disjoint_groups=0`` 은 "선언이
    # 없다" 로 읽힌다 (2026-08-08 규명). 이 리포도 pairwise 를 정상 입력으로
    # 취급한다 — ``validation_core._rule_disjoint_style`` 이 그것을 WARNING 으로
    # 잡으면서 "AllDisjointClasses 로 변환 권장" 이라고 안내한다.
    #
    # 추론이 넣는 type pollution (한 인스턴스가 상호배타 클래스 2개에 속함) 이
    # 정확히 이 게이트가 잡아야 할 결함이다.
    pairwise_pairs = 0
    for subject, obj in tbox.subject_objects(OWL.disjointWith):
        if not (isinstance(subject, URIRef) and isinstance(obj, URIRef)):
            continue
        if not (str(subject).startswith(ns) and str(obj).startswith(ns)):
            continue
        pair = sorted({local(str(subject)), local(str(obj))})
        if len(pair) == 2 and pair not in disjoint_groups:
            disjoint_groups.append(pair)
            pairwise_pairs += 1

    if not disjoint_groups:
        return {
            "name": "AllDisjointClasses 위반",
            "passed": True,
            "disjoint_groups": 0,
            "pairwise_disjoint_pairs": pairwise_pairs,
            "checked_instances": 0,
            "violations": [],
        }

    instance_types = shared.instance_types

    violations: list[dict] = []
    checked_instances = len(instance_types)

    for inst_uri, types in instance_types.items():
        for group in disjoint_groups:
            group_set = set(group)
            overlapping = types & group_set
            if len(overlapping) >= 2:
                violations.append({
                    "instance": local(inst_uri),
                    "types": sorted(overlapping),
                    "disjoint_group": group,
                })
                if len(violations) >= 20:
                    break
        if len(violations) >= 20:
            break

    return {
        "name": "AllDisjointClasses 위반",
        "passed": len(violations) == 0,
        "disjoint_groups": len(disjoint_groups),
        # 두 표기 중 pairwise 로 들어온 쌍 수 — 어느 표기를 읽었는지 감사 가능하게.
        "pairwise_disjoint_pairs": pairwise_pairs,
        "checked_instances": checked_instances,
        "violations": violations[:20],
    }
