#!/usr/bin/env python3
"""Capstone 마크다운을 단일 파일 HTML 1페이지로 렌더링한다.

사용법:
    python scripts/render_capstone.py --name <닉네임 또는 참가번호> --date YYYY-MM-DD

workshop/capstone-outputs/<name>_<date>.md 를 읽어 같은 이름의 .html 을 만든다. 마크다운
파일이 없으면 TEMPLATE.md 를 그 경로로 복사한다. 결과물은 참가자 본인 보관용이고, 제출은
워크북 6-2c 의 규칙을 따르는 선택 사항이다.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

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


_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_LINK_SLOT_RE = re.compile("\x00(\\d+)\x00")

#: ``href`` 로 내보내는 scheme. 나머지(javascript:, data:, vbscript:, file: 등)는 링크로 만들지 않는다.
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https"})
#: 브라우저는 URL 양끝의 C0 제어 문자와 공백을 버리고 중간의 탭·개행을 지운 뒤 scheme 을 읽는다.
_URL_EDGE_CHARS = "".join(chr(code) for code in range(0x21))
_URL_DROPPED_CHARS = str.maketrans("", "", "\t\n\r")


def _safe_href(url: str) -> str | None:
    """링크 대상이 http(s) URL 이나 상대 참조면 브라우저가 읽을 형태로, 아니면 None 을 돌려준다.

    scheme 없는 대상이 ``//`` 로 시작하면 슬래시 개수와 무관하게 거부하고, 역슬래시도 거부한다.
    브라우저는 역슬래시를 ``/`` 로 읽는다. 이 HTML 은 ``file:`` 로 열리므로 ``//host`` 는 다른
    호스트를 가리키고, ``////host`` 처럼 netloc 이 비어 보이는 형태도 Windows 에서 UNC 경로로
    열린다. 그래서 ``urlsplit`` 의 netloc 만으로는 판정하지 않는다.
    """
    normalized = url.strip(_URL_EDGE_CHARS).translate(_URL_DROPPED_CHARS)
    if not normalized or "\\" in normalized:
        return None
    try:
        parts = urlsplit(normalized)
    except ValueError:
        return None
    if parts.scheme:
        return normalized if parts.scheme.lower() in _ALLOWED_LINK_SCHEMES else None
    return None if parts.netloc or normalized.startswith("//") else normalized


def _render_emphasis(escaped: str) -> str:
    """이미 escape 한 텍스트에 굵게와 인라인 코드를 적용한다."""
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"`([^`]+?)`", r"<code>\1</code>", escaped)


def render_inline(text: str) -> str:
    """Inline markdown: bold / code / links.

    링크는 굵게·코드 처리 전에 자리표시자로 빼 둔다. 그래서 URL 안의 ``**`` 나 backtick 이
    속성 값 안에 태그를 만들지 않는다. 대상이 ``_safe_href`` 를 통과할 때만 ``<a href>`` 가 되고,
    통과하지 못하면 링크 텍스트만 남는다. 자리표시자 구분자인 NUL 은 입력에서 지운다.
    """
    anchors: list[str] = []

    def stash(match: re.Match[str]) -> str:
        label = _render_emphasis(html.escape(match.group(1)))
        href = _safe_href(match.group(2))
        anchors.append(label if href is None else f'<a href="{html.escape(href)}">{label}</a>')
        return f"\x00{len(anchors) - 1}\x00"

    body = _LINK_RE.sub(stash, text.replace("\x00", ""))
    body = _render_emphasis(html.escape(body))
    return _LINK_SLOT_RE.sub(lambda slot: anchors[int(slot.group(1))], body)


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
  📎 워크샵 Capstone 본인 보관용 1페이지. 제출은 선택 사항이며, 제출한다면 데이터 취급 규칙을
  지킨 파일을 주최 측이 지정한 승인된 채널로 보냅니다.<br>
  🎯 워크샵 목표: (a) KG 가치 1분 설명, (b) Claude 가 만든 SPARQL 검증, (c) 자사 PoC 첫날 계획.
</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        required=True,
        help="닉네임 또는 참가번호 (실명 대신). 파일 이름 <name>_<date>.md 에 쓰인다",
    )
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
            f"[INFO] {md_path} not found; copying TEMPLATE.md to that path.\n"
            f"       Edit it, then re-run this script."
        )
        # TEMPLATE.md 는 UTF-8 한국어 문서다. 인코딩을 생략하면 Windows 기본 locale
        # 인코딩 (cp949, cp1252) 으로 디코드하다 UnicodeDecodeError 가 난다.
        md_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
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
        "     → 본인 보관용입니다. 제출은 선택 사항이며, 강사가 회수를 요청한 경우에만\n"
        "       데이터 취급 규칙을 지킨 파일을 주최 측이 지정한 승인된 채널로 보냅니다 (워크북 6-2c)."
    )
    return 0


if __name__ == "__main__":
    # 출력에는 한국어와 기호 (→) 가 있다. 출력을 파일이나 파이프로 돌리면 Windows 는
    # locale 인코딩으로 쓰려다 UnicodeEncodeError 를 내므로 UTF-8 로 고정한다.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    sys.exit(main())
