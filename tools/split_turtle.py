"""split_turtle.py — large Turtle 파일을 Turtle 문법 안전하게 청크 분할.

Why: reasonable.PyReasoner.load_file() 의 Turtle 파서가 비선형 비용을
보임 (191MB→14초, 107MB→10분, 2.8GB→1.5시간+ 미완료). 큰 단일 파일을
여러 작은 파일로 쪼개어 각각 load_file 하면 누적 시간이 크게 줄어들
가능성. 측정 데이터 기반: 작은 파일 (~100MB) 은 선형에 가까움.

How: prefix 헤더를 추출하고, 트리플 boundary (`.` 로 끝나는 줄) 에서
~100MB 단위로 자른다. 각 chunk 에 같은 prefix 헤더를 prepend 해서
Turtle 문법 단독 valid 하게 보존.

Usage:
    python -m tools.split_turtle <input.ttl> <output_dir> [--chunk-mb 100]
"""
from __future__ import annotations

import argparse
import os
import sys
import time


def split_turtle(
    input_path: str, output_dir: str, chunk_size_mb: int = 100,
) -> list[str]:
    """Split a Turtle file into chunks of approximately chunk_size_mb each.

    Strategy:
      1. Read the file once and collect every line starting with `@prefix`
         or `@base` — these become the shared header.
      2. Stream the rest of the file. Cut whenever the running chunk size
         exceeds chunk_size_mb AND the current line ends with `.` (a triple
         terminator). This guarantees each chunk ends on a complete triple.
      3. Each chunk is written as `<basename>.partNN.ttl` with the shared
         header prepended.

    Returns the list of generated chunk paths.
    """
    chunk_target_bytes = chunk_size_mb * 1024 * 1024
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    os.makedirs(output_dir, exist_ok=True)

    # Pass 1: collect prefix header lines.
    header_lines: list[str] = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("@prefix") or stripped.startswith("@base"):
                header_lines.append(line)
            elif stripped:
                # First non-prefix non-empty line — header is done.
                break
    header = "".join(header_lines)
    header_size = len(header.encode("utf-8"))
    print(f"Header: {len(header_lines)} prefix lines, {header_size:,} bytes")

    # Pass 2: stream chunks.
    chunk_paths: list[str] = []
    chunk_idx = 0
    chunk_buf: list[str] = []
    chunk_bytes = 0
    in_header = True
    line_count = 0
    t0 = time.monotonic()
    total_size = os.path.getsize(input_path)

    def _flush_chunk() -> None:
        nonlocal chunk_idx, chunk_buf, chunk_bytes
        if not chunk_buf:
            return
        chunk_idx += 1
        out_path = os.path.join(
            output_dir, f"{base_name}.part{chunk_idx:02d}.ttl",
        )
        with open(out_path, "w", encoding="utf-8") as out:
            out.write(header)
            out.write("\n")  # spacer
            out.writelines(chunk_buf)
        chunk_paths.append(out_path)
        chunk_size = os.path.getsize(out_path)
        elapsed = time.monotonic() - t0
        print(
            f"  [{chunk_idx:02d}] {out_path}  "
            f"{chunk_size / 1024 / 1024:.0f} MB, {line_count:,} lines, "
            f"{elapsed:.1f}s elapsed",
        )
        chunk_buf = []
        chunk_bytes = 0

    with open(input_path, encoding="utf-8") as f:
        for line in f:
            line_count += 1
            stripped = line.strip()
            if in_header:
                if stripped.startswith("@prefix") or stripped.startswith("@base") or not stripped:
                    continue
                in_header = False
            chunk_buf.append(line)
            chunk_bytes += len(line.encode("utf-8"))

            # Cut at triple boundary when target size hit.
            if chunk_bytes >= chunk_target_bytes and stripped.endswith("."):
                _flush_chunk()

    # Last chunk.
    _flush_chunk()
    total_elapsed = time.monotonic() - t0
    print(
        f"\nTotal: {chunk_idx} chunks, {line_count:,} lines, "
        f"{total_size / 1024 / 1024:.0f} MB → {total_elapsed:.1f}s",
    )
    return chunk_paths


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Split a large Turtle file into chunks.")
    p.add_argument("input", help="path to input .ttl")
    p.add_argument("output_dir", help="directory for chunk files")
    p.add_argument(
        "--chunk-mb", type=int, default=100,
        help="approximate chunk size in MB (default 100)",
    )
    args = p.parse_args(argv)

    if not os.path.exists(args.input):
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 2
    chunks = split_turtle(args.input, args.output_dir, chunk_size_mb=args.chunk_mb)
    print(f"\nGenerated {len(chunks)} chunks under {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
