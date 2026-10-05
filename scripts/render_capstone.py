#!/usr/bin/env python3
"""Render Capstone markdown output to standalone HTML 1-pager.

Usage:
    python scripts/render_capstone.py --name <participant-name> --date 2026-XX-XX

Reads workshop/capstone-outputs/<name>_<date>.md, renders to single-file HTML
that participant emails to themselves as the visible artifact of the workshop.

참가자가 눈으로 확인할 수 있는 산출물을 남기는 것이 이 스크립트의 목적이다.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPSTONE_DIR = REPO_ROOT / "workshop" / "capstone-outputs"


def render_markdown_to_html(md: str) -> str:
    """Minimal markdown → HTML conversion (no external deps)."""
    lines = md.splitlines()
    out: list[str] = []
    in_code = False
    in_table = False

    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                lang = line[3:].strip()
                out.append(f'<pre><code class="lang-{html.escape(lang)}">')
                in_code = True
            continue

        if in_code:
            out.append(html.escape(line))
            continue

        # Tables — naive: detect "|" rows
        is_table_row = "|" in line and line.strip().startswith("|")
        if is_table_row:
            if not in_table:
                out.append("<table>")
                in_table = True
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # Skip separator rows
            if all(re.match(r"^:?-+:?$", c) for c in cells):
                continue
            row = "<tr>" + "".join(
                f"<td>{render_inline(c)}</td>" for c in cells
            ) + "</tr>"
            out.append(row)
            continue
        elif in_table:
            out.append("</table>")
            in_table = False

        # Headers
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            text = render_inline(m.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            continue

        # Blockquote
        if line.startswith("> "):
            out.append(f"<blockquote>{render_inline(line[2:])}</blockquote>")
            continue

        # Horizontal rule
        if line.strip() == "---":
            out.append("<hr>")
            continue

        # List items
        m = re.match(r"^[\-\*]\s+(.*)$", line)
        if m:
            out.append(f"<li>{render_inline(m.group(1))}</li>")
            continue

        # Paragraph
        if line.strip():
            out.append(f"<p>{render_inline(line)}</p>")
        else:
            out.append("")

    if in_code:
        out.append("</code></pre>")
    if in_table:
        out.append("</table>")

    return "\n".join(out)


def render_inline(text: str) -> str:
    """Inline markdown: bold / italic / code / links."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Capstone — {name} ({date})</title>
<style>
  body {{ font-family: -apple-system, "Helvetica Neue", sans-serif; max-width: 760px;
          margin: 40px auto; padding: 0 24px; line-height: 1.6; color: #222; }}
  h1, h2 {{ border-bottom: 2px solid #4a90e2; padding-bottom: 6px; }}
  h2 {{ margin-top: 36px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
  td {{ border: 1px solid #ddd; padding: 8px 12px; }}
  pre {{ background: #f4f6f8; padding: 12px; border-radius: 4px; overflow-x: auto; }}
  code {{ font-family: "SF Mono", Menlo, monospace; font-size: 0.92em; }}
  blockquote {{ border-left: 4px solid #4a90e2; margin: 12px 0; padding: 8px 16px;
                background: #f0f4f8; color: #444; }}
  .meta {{ color: #888; font-size: 0.9em; margin-bottom: 24px; }}
  .footer {{ margin-top: 48px; padding-top: 16px; border-top: 1px solid #ddd;
             color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
<div class="meta">
  Capstone Output · {name} · {date} · ontology-agent workshop v3.2
</div>
{body}
<div class="footer">
  📎 3.5시간 워크샵의 visible artifact — D+1 매니저 1:1 에 첨부.<br>
  🎯 한 줄 약속: "당신은 3.5시간 후 (a) KG 가치 1분 설명, (b) Claude SPARQL 검증,
  (c) 자사 PoC Day 1 청사진 — 모두 가능."
</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Participant name (kebab-case)")
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="Workshop date (YYYY-MM-DD), default today",
    )
    args = parser.parse_args()

    md_path = CAPSTONE_DIR / f"{args.name}_{args.date}.md"
    if not md_path.exists():
        # Fallback to TEMPLATE.md
        template = CAPSTONE_DIR / "TEMPLATE.md"
        if not template.exists():
            print(f"[ERROR] Neither {md_path} nor TEMPLATE.md found.", file=sys.stderr)
            return 1
        print(
            f"[INFO] {md_path} not found — copying TEMPLATE.md to that path.\n"
            f"       Edit it (5 minutes) then re-run this script."
        )
        md_path.write_text(template.read_text(), encoding="utf-8")
        print(f"[OK] Created {md_path}")
        return 0

    md = md_path.read_text(encoding="utf-8")
    body = render_markdown_to_html(md)

    html_out = HTML_TEMPLATE.format(
        name=html.escape(args.name),
        date=html.escape(args.date),
        body=body,
    )

    out_path = md_path.with_suffix(".html")
    out_path.write_text(html_out, encoding="utf-8")
    print(f"[OK] Rendered {out_path}")
    print(
        "     → 본인 노트북에 저장 + 이메일/Slack 으로 자기 자신에게 발송.\n"
        "     → D+1 매니저 1:1 에 첨부 (workbook §M11 1-pager 5줄과 함께)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
