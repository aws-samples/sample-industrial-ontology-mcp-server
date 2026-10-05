"""ttl_path 를 받는 검증 도구 3종의 입력 경로 경계 회귀 테스트.

``validate_owl_justification`` · ``validate_owl_realisation`` · ``validate_shacl`` 은
``ttl_path`` 를 ``GENERATED_TBOX_DIR`` 바로 아래 ``.ttl`` 파일명으로만 받는다.
경계 밖 파일을 열면 rdflib 파싱 오류 메시지에 원문 일부가 실려 MCP 응답으로
돌아가므로, 경계 검사는 파일 로드보다 먼저 끝나야 한다. 그래서 거부 사례에서는
로더가 한 번도 호출되지 않아야 하고, 정상 파일명은 경계 안의 정규화 경로로 로드돼야
한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import tools.common as common
import tools.owl_reasoner as owl_reasoner
import tools.validation_core as validation_core

# 경계 밖 설정 파일을 흉내 낸 합성 payload. Turtle 이 아니므로 rdflib 파싱 오류 메시지에
# 원문 조각이 실린다. 표식은 비밀 스캐너가 자격 증명으로 오인하지 않는 형태로 둔다.
_SENTINEL_MARKER = "PATH-CONTAINMENT-SENTINEL-7Q2"
_SENTINEL_PAYLOAD = (
    "[default]\n"
    f"marker_line = {_SENTINEL_MARKER}\n"
    "second_line = synthetic-value-for-path-containment-test\n"
)

_VALID_TTL = """\
@prefix ex: <https://example.com/test#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Thing a owl:Class .
"""

_REJECT_PHRASES = ("디렉터리 구분자", "허용된 디렉터리 밖")

_TOOLS = [
    (owl_reasoner, "validate_owl_justification"),
    (owl_reasoner, "validate_owl_realisation"),
    (validation_core, "validate_shacl"),
]


@pytest.fixture
def tbox_dir(tmp_path, monkeypatch) -> Path:
    """두 모듈의 T-Box 기준 디렉터리와 기본 T-Box 를 tmp 로 격리한다."""
    tbox = tmp_path / "generated" / "tbox"
    tbox.mkdir(parents=True)
    default_tbox = tbox / "t_box.ttl"
    default_tbox.write_text(_VALID_TTL, encoding="utf-8")
    for module in (owl_reasoner, validation_core):
        monkeypatch.setattr(module, "GENERATED_TBOX_DIR", str(tbox))
    monkeypatch.setattr(validation_core, "TBOX_PATH", str(default_tbox))
    # realisation 은 기본 skip 이므로 경계 검사 경로까지 들어가도록 opt-in 한다.
    monkeypatch.setenv("PELLET_REALISATION_ENABLED", "true")
    return tbox


@pytest.fixture
def load_calls(monkeypatch) -> list[str]:
    """TTL 로더에 넘어간 ttl_path 를 기록한다 (로드 동작은 그대로 위임)."""
    calls: list[str] = []
    original = common.load_ttl_content

    def _spy(ttl_content: str, ttl_path: str, default_path: str = "") -> str:
        calls.append(ttl_path)
        return original(ttl_content, ttl_path, default_path)

    # owl_reasoner 는 호출 시점에 tools.common 에서 import 하고,
    # validation_core 는 모듈 import 시점에 이름을 바인딩한다.
    monkeypatch.setattr(common, "load_ttl_content", _spy)
    monkeypatch.setattr(validation_core, "load_ttl_content", _spy)
    return calls


def _outside_secret(tmp_path: Path, name: str) -> Path:
    """허용 디렉터리 밖에 비밀 형식 파일을 만든다."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(exist_ok=True)
    target = outside_dir / name
    target.write_text(_SENTINEL_PAYLOAD, encoding="utf-8")
    return target


def _attack_value(attack: str, tbox: Path, tmp_path: Path) -> str:
    """절대경로, 상위 경로, 외부 symlink, 확장자 없는 비밀 파일 공격값을 만든다."""
    if attack == "absolute":
        return str(_outside_secret(tmp_path, "credentials.ttl"))
    if attack == "absolute_no_suffix":
        return str(_outside_secret(tmp_path, "credentials"))
    if attack == "traversal":
        outside = _outside_secret(tmp_path, "credentials.ttl")
        # 기존 로더는 상대경로를 CWD 기준으로 열므로 CWD 에서 경계 밖 파일로 가는 경로를 쓴다.
        value = os.path.relpath(outside, Path.cwd())
        assert value.startswith(".."), value
        return value
    if attack == "traversal_from_base":
        _outside_secret(tmp_path, "credentials.ttl")
        return "../../outside/credentials.ttl"
    link = tbox / "outside-link.ttl"
    link.symlink_to(_outside_secret(tmp_path, "credentials.ttl"))
    return link.name


@pytest.mark.parametrize(("module", "function_name"), _TOOLS)
@pytest.mark.parametrize(
    "attack",
    ["absolute", "absolute_no_suffix", "traversal", "traversal_from_base", "symlink"],
)
def test_ttl_path_outside_tbox_dir_is_rejected_before_load(
    module, function_name, attack, tbox_dir, load_calls, tmp_path
):
    """경계 밖 ttl_path 는 파일을 열기 전에 거부되고 원문이 응답에 실리지 않는다."""
    value = _attack_value(attack, tbox_dir, tmp_path)

    raw = getattr(module, function_name)(ttl_path=value)
    result = json.loads(raw)

    assert result["success"] is False
    assert any(phrase in result["error"] for phrase in _REJECT_PHRASES), result["error"]
    assert _SENTINEL_MARKER not in raw
    assert load_calls == []


def test_validate_shacl_reads_relative_name_inside_tbox_dir(tbox_dir, load_calls):
    """정상 파일명은 경계 안의 정규화 경로로 로드되고 검증 결과가 그대로 나온다."""
    (tbox_dir / "case.ttl").write_text(_VALID_TTL, encoding="utf-8")

    result = json.loads(validation_core.validate_shacl(ttl_path="case.ttl"))

    assert result["success"] is True
    assert result["conforms"] is True
    assert load_calls == [str((tbox_dir / "case.ttl").resolve())]


def test_validate_owl_justification_reads_relative_name_inside_tbox_dir(
    tbox_dir, load_calls, monkeypatch
):
    """정상 파일명은 경계 안에서 로드된다 (HermiT 호출은 일관 판정으로 대체)."""
    (tbox_dir / "case.ttl").write_text(_VALID_TTL, encoding="utf-8")
    seen: list[str] = []

    def _consistent(ttl: str) -> bool:
        seen.append(ttl)
        return False

    monkeypatch.setattr(owl_reasoner, "_is_inconsistent", _consistent)

    result = json.loads(owl_reasoner.validate_owl_justification(ttl_path="case.ttl"))

    assert result["success"] is True
    assert result["consistent"] is True
    assert load_calls == [str((tbox_dir / "case.ttl").resolve())]
    assert seen == [_VALID_TTL]


def test_validate_owl_realisation_reads_relative_name_inside_tbox_dir(
    tbox_dir, load_calls, monkeypatch
):
    """정상 파일명은 경계 안에서 로드된 뒤 Pellet 단계로 넘어간다 (Pellet 진입은 sentinel 로 차단)."""
    (tbox_dir / "case.ttl").write_text(_VALID_TTL, encoding="utf-8")
    seen: list[str] = []

    def _stop_before_pellet(ttl: str, *_args, **_kwargs):
        seen.append(ttl)
        raise RuntimeError("load-reached")

    monkeypatch.setattr(owl_reasoner, "_ttl_to_owlready", _stop_before_pellet)

    result = json.loads(owl_reasoner.validate_owl_realisation(ttl_path="case.ttl"))

    assert result["success"] is False
    assert "load-reached" in result["error"]
    assert load_calls == [str((tbox_dir / "case.ttl").resolve())]
    assert seen == [_VALID_TTL]
