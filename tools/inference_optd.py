"""inference_optd.py — OPT-D pipeline helpers for run_owl_rl_inference.

Four responsibilities, each kept as a thin pure function so the main
inference orchestrator (tools/inference.py::run_owl_rl_inference) stays
readable:

1. ``stream_load_inputs`` — hand every TTL under data/source/tacit/ to a
   PyReasoner instance one file at a time. The reasoner accumulates them
   in Rust memory, so we never need an intermediate rdflib Graph for the
   triples — that was the largest in-memory cost in the previous pipeline.

2. ``build_or_get_tbox_closure`` — compute the OWL 2 RL closure of the
   T-Box alone and persist it as N-Triples next to the T-Box file. The
   cached file is reused unless the source T-Box mtime changes. The
   closure is a few thousand to a few hundred-thousand triples, so the
   on-disk artefact is tiny (sub-MB) and saves the reasoner from
   rediscovering subClassOf chains every run.

3. ``stream_closure_to_nt_file`` — drain a reason() result list to an
   N-Triples file in bounded-memory chunks, ``del`` each slice so peak
   Python RSS stays at chunk-size order rather than the whole closure.

4. ``bulk_load_nt_into_graph`` — feed that NT file back into an
   Oxigraph-backed rdflib.Graph via Rust streaming bulk_load.

Side-effect free wrt the rest of the codebase: nothing here imports the
main run_owl_rl_inference function or mutates module state.
"""
from __future__ import annotations

import glob
import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid hard import at module load
    from rdflib import Graph  # noqa: F401 — referenced in string type hints
    from reasonable import PyReasoner  # noqa: F401

from config import SOURCE_TACIT_DIR, TBOX_PATH

logger = logging.getLogger(__name__)


def stream_load_inputs(reasoner: PyReasoner) -> int:
    """Feed every tacit TTL into the reasoner via load_file (Rust-side).

    Returns the number of tacit files loaded so the caller can report it
    alongside other stats.

    Logs are emitted *before* and *after* each load_file call so a
    multi-GB Turtle file (which can sit in load_file for tens of minutes)
    leaves a clear trail in tail -f. Otherwise the silence between the
    last small-file completion and the next file's completion was being
    misread as a hang inside reason().
    """
    count = 0
    if not os.path.isdir(SOURCE_TACIT_DIR):
        return count
    files = sorted(glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl")))
    for idx, ttl_file in enumerate(files, start=1):
        try:
            size_mb = os.path.getsize(ttl_file) / 1024 / 1024
        except OSError:
            size_mb = 0.0
        logger.info(
            "OPT-D: tacit 로드 시작 [%d/%d] — %s (%.0f MB)",
            idx, len(files), os.path.basename(ttl_file), size_mb,
        )
        t0 = time.monotonic()
        reasoner.load_file(ttl_file)
        elapsed = time.monotonic() - t0
        logger.info(
            "OPT-D: tacit 로드 완료 [%d/%d] — %s (%.0f MB, %.1fs)",
            idx, len(files), os.path.basename(ttl_file), size_mb, elapsed,
        )
        count += 1
    return count


def build_or_get_tbox_closure(tbox_path: str = "") -> str:
    """Persist a one-time OWL RL closure of the T-Box and return its path.

    Strategy: when the cache is fresher than the source T-Box, return the
    cached path immediately. Otherwise re-run a small, T-Box-only reasoner
    pass and dump the closure as N-Triples. The cache lives next to the
    T-Box file as ``<tbox>.tbox_closure.nt``.

    The returned path is informational — the caller doesn't have to feed
    it back into the main reasoner. Keeping the cache means a later run
    can choose to load the closure directly (skipping subClassOf
    transitive expansion) when only the A-Box has changed.
    """
    src = tbox_path or TBOX_PATH
    cache = src + ".tbox_closure.nt"

    if (
        os.path.exists(cache)
        and os.path.exists(src)
        and os.path.getmtime(cache) >= os.path.getmtime(src)
    ):
        logger.info("OPT-D: T-Box closure 캐시 사용 — %s", cache)
        return cache

    if not os.path.exists(src):
        logger.warning("OPT-D: T-Box 파일 없음, closure 캐시 skip — %s", src)
        return cache

    from reasonable import PyReasoner

    t0 = time.monotonic()
    r = PyReasoner()
    r.load_file(src)
    closure = r.reason()
    del r

    with open(cache, "w", encoding="utf-8") as f:
        for s, p, o in closure:
            f.write(f"{s.n3()} {p.n3()} {o.n3()} .\n")

    logger.info(
        "OPT-D: T-Box closure 캐시 생성 — %s (%d triples, %.1fs)",
        cache, len(closure), time.monotonic() - t0,
    )
    del closure
    return cache


def stream_closure_to_nt_file(
    closure_list: list, nt_path: str, *, logger=logger,
) -> int:
    """Drain a reason() result list directly to a streaming N-Triples file.

    Implementation: chunk-by-chunk Python ``f"{s.n3()} {p.n3()} {o.n3()} ."``
    serialization. ``del closure_list[:chunk]`` after each batch returns the
    consumed list pages to the allocator so peak Python memory is bounded
    by ``OWL_CLOSURE_CHUNK`` (default 500k).

    A previous attempt (OPT-I-1) added ``gc.collect()`` per chunk; it
    slowed the loop ~3× (582k/s → 177k/s) without reducing peak RSS, so
    cyclic GC is left to Python's default cadence. A second attempt
    (OPT-I-2) routed serialization through a transient pyoxigraph.Store
    for Rust speed; bulk_load + dump roundtrip cost outweighed the win
    on the closure shape we hit, so the simple Python loop stayed.

    Args:
        closure_list: result of `PyReasoner.reason()`.
        nt_path: destination file path (caller responsible for cleanup).
        logger: optional logger; defaults to module logger.

    Returns:
        Number of triples written to ``nt_path``.
    """
    try:
        chunk_size = int(os.getenv("OWL_CLOSURE_CHUNK", "500000"))
    except ValueError:
        chunk_size = 500_000

    total = len(closure_list)
    written = 0
    t0 = time.monotonic()

    with open(nt_path, "w", encoding="utf-8") as fh:
        while closure_list:
            chunk = closure_list[:chunk_size]
            del closure_list[:chunk_size]
            lines = "".join(
                f"{s.n3()} {p.n3()} {o.n3()} .\n" for s, p, o in chunk
            )
            fh.write(lines)
            written += len(chunk)
            if total >= chunk_size:
                rate = written / max(time.monotonic() - t0, 1e-6)
                logger.info(
                    "  closure → NT file: %d/%d (%.1f%%) — %.0fk/s",
                    written, total, 100.0 * written / max(total, 1),
                    rate / 1000,
                )
            del chunk, lines
    return written


def bulk_load_nt_into_graph(
    g: Graph, nt_path: str, *, logger=logger,
) -> int:
    """Bulk-load a streaming N-Triples file into an Oxigraph-backed graph.

    Used as the second half of the streaming closure pipeline (paired with
    ``stream_closure_to_nt_file``). pyoxigraph's bulk_load reads the file
    in Rust, never materializing the entire NT content in Python memory.

    Returns: number of triples added (post-load `len(g) - pre_count`).
    """
    from pyoxigraph import RdfFormat

    pre_count = len(g)
    inner = getattr(g.store, "_inner", None)
    t0 = time.monotonic()
    if inner is not None:
        # Direct pyoxigraph bulk_load — Rust path, file streamed in.
        gid = g.identifier
        try:
            from pyoxigraph import BlankNode as _BN
            from pyoxigraph import NamedNode as _NN
            from rdflib import BNode as _RBN
            from rdflib import URIRef as _UR
            if isinstance(gid, _RBN):
                to_graph = _BN(str(gid))
            elif isinstance(gid, _UR):
                to_graph = _NN(str(gid))
            else:
                to_graph = None
        except Exception:
            to_graph = None
        with open(nt_path, "rb") as fh:
            inner.bulk_load(fh, RdfFormat.N_TRIPLES, to_graph=to_graph)
    else:
        g.parse(nt_path, format="nt")

    added = len(g) - pre_count
    elapsed = time.monotonic() - t0
    logger.info(
        "  NT bulk_load → store: %d triples (%.1fs, %.0fk/s)",
        added, elapsed, (added / max(elapsed, 1e-6)) / 1000,
    )
    return added
