"""render_capstone 이 locale 기본 인코딩과 무관하게 UTF-8 한국어 문서를 다루는가.

Windows 의 locale 기본 인코딩(cp949, cp1252)에서 인코딩을 생략한 ``Path.read_text`` 는
UTF-8 한국어 문서를 디코드하다 ``UnicodeDecodeError`` 를 내고, ``Path.write_text`` 는
cp949 에 없는 문자에서 ``UnicodeEncodeError`` 를 내거나 다른 바이트로 쓴다. 두 메서드가
인코딩 인자 없이 불리면 cp949 를 쓰도록 바꿔 그 환경을 재현한다. 모든 쓰기는 ``tmp_path``
안에서 일어난다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

import scripts.render_capstone as render_capstone

_LEGACY_ENCODING = "cp949"


def _contains_hangul(text: str) -> bool:
    return any("가" <= char <= "힣" for char in text)


@pytest.fixture
def legacy_locale(monkeypatch):
    """인코딩을 생략한 ``Path.read_text`` / ``Path.write_text`` 가 cp949 를 쓰게 한다."""
    original_read_text = Path.read_text
    original_write_text = Path.write_text

    def _read_text(self, encoding=None, *args, **kwargs):
        return original_read_text(self, encoding or _LEGACY_ENCODING, *args, **kwargs)

    def _write_text(self, data, encoding=None, *args, **kwargs):
        return original_write_text(self, data, encoding or _LEGACY_ENCODING, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    monkeypatch.setattr(Path, "write_text", _write_text)


@pytest.fixture
def capstone_dir(tmp_path, monkeypatch):
    """배포 TEMPLATE.md 의 바이트를 tmp 디렉터리로 옮기고 스크립트의 CAPSTONE_DIR 을 돌린다."""
    shipped = render_capstone.CAPSTONE_DIR / "TEMPLATE.md"
    template_bytes = shipped.read_bytes()
    target = tmp_path / "capstone-outputs"
    target.mkdir()
    (target / "TEMPLATE.md").write_bytes(template_bytes)
    monkeypatch.setattr(render_capstone, "CAPSTONE_DIR", target)
    return target, template_bytes


def _run_main(monkeypatch, name: str, date: str) -> int:
    monkeypatch.setattr(sys, "argv", ["render_capstone.py", "--name", name, "--date", date])
    return render_capstone.main()


def test_template_copy_keeps_korean_utf8_under_legacy_locale(capstone_dir, legacy_locale, monkeypatch):
    """마크다운이 없으면 TEMPLATE.md 를 바이트 그대로 복사한다."""
    target, template_bytes = capstone_dir
    template_text = template_bytes.decode("utf-8")
    assert _contains_hangul(template_text), "TEMPLATE.md 에 한국어가 없으면 이 재현은 의미가 없다"
    with pytest.raises(UnicodeDecodeError):
        template_bytes.decode(_LEGACY_ENCODING)

    assert _run_main(monkeypatch, "tester", "2000-01-01") == 0

    created = target / "tester_2000-01-01.md"
    assert created.read_bytes() == template_bytes
    assert not created.with_suffix(".html").exists()


def test_render_keeps_korean_utf8_under_legacy_locale(capstone_dir, legacy_locale, monkeypatch):
    """기존 한국어 마크다운을 읽어 UTF-8 HTML 로 렌더링한다."""
    target, _ = capstone_dir
    markdown = "# 캡스톤 결과: 설비 KG\n\n- 고로 설비 **10개** 를 조회했다\n"
    (target / "tester_2000-01-01.md").write_bytes(markdown.encode("utf-8"))

    assert _run_main(monkeypatch, "tester", "2000-01-01") == 0

    rendered = (target / "tester_2000-01-01.html").read_bytes().decode("utf-8")
    assert '<meta charset="UTF-8">' in rendered
    assert "<h1>캡스톤 결과: 설비 KG</h1>" in rendered
    assert "<li>고로 설비 <strong>10개</strong> 를 조회했다</li>" in rendered
