"""GraphDB 파일 import 입력 경계 회귀 테스트."""

from __future__ import annotations

import os
from pathlib import Path

from tools.remote import graphdb


class _SuccessfulResponse:
    """GraphDB 성공 응답의 최소 테스트 대역."""

    def raise_for_status(self) -> None:
        return None


def _block_requests(monkeypatch) -> list[tuple[object, ...]]:
    """거부 입력이 GraphDB 요청까지 도달하면 호출 기록을 남긴다."""
    calls: list[tuple[object, ...]] = []

    def record_request(*args, **kwargs):
        calls.append((args, kwargs))
        return _SuccessfulResponse()

    monkeypatch.setattr(graphdb.requests, "request", record_request)
    return calls


def test_graphdb_import_file_accepts_ttl_inside_data_dir(tmp_path, monkeypatch):
    """DATA_DIR 안의 허용 확장자 파일은 GraphDB stream upload까지 진행한다."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ttl_path = data_dir / "sample.TTL"
    ttl_path.write_text("@prefix ex: <urn:example:> .", encoding="utf-8")
    monkeypatch.setattr(graphdb, "DATA_DIR", str(data_dir), raising=False)

    calls = _block_requests(monkeypatch)
    result = graphdb.graphdb_import_file(str(ttl_path))

    assert result.startswith("import 완료")
    assert len(calls) == 1
    assert calls[0][1]["headers"]["Content-Type"] == "text/turtle"


def test_graphdb_import_file_rejects_disallowed_extension(tmp_path, monkeypatch):
    """확장자 allowlist 검증이 빠지면 .env 파일이 upload되는 회귀를 잡는다."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_path = data_dir / ".env"
    env_path.write_text("TOKEN=not-a-real-token", encoding="utf-8")
    monkeypatch.setattr(graphdb, "DATA_DIR", str(data_dir), raising=False)

    calls = _block_requests(monkeypatch)
    result = graphdb.graphdb_import_file(str(env_path))

    assert "허용 확장자" in result
    assert calls == []


def test_graphdb_import_file_rejects_relative_path_escape(tmp_path, monkeypatch):
    """정규화 전 문자열 비교로 바뀌면 DATA_DIR 밖의 상대 경로가 통과하는 회귀를 잡는다."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside_path = tmp_path / "outside.ttl"
    outside_path.write_text("@prefix ex: <urn:outside:> .", encoding="utf-8")
    relative_escape = os.path.relpath(outside_path, start=Path.cwd())
    assert ".." in Path(relative_escape).parts
    monkeypatch.setattr(graphdb, "DATA_DIR", str(data_dir), raising=False)

    calls = _block_requests(monkeypatch)
    result = graphdb.graphdb_import_file(relative_escape)

    assert "허용된 데이터 디렉터리 밖" in result
    assert relative_escape not in result
    assert str(outside_path) not in result
    assert calls == []


def test_graphdb_import_file_rejects_symlink_to_outside(tmp_path, monkeypatch):
    """resolve 후 containment가 빠지면 DATA_DIR 안의 외부 symlink가 통과하는 회귀를 잡는다."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside_path = tmp_path / "outside.ttl"
    outside_path.write_text("@prefix ex: <urn:outside:> .", encoding="utf-8")
    symlink_path = data_dir / "linked.ttl"
    symlink_path.symlink_to(outside_path)
    monkeypatch.setattr(graphdb, "DATA_DIR", str(data_dir), raising=False)

    calls = _block_requests(monkeypatch)
    result = graphdb.graphdb_import_file(str(symlink_path))

    assert "허용된 데이터 디렉터리 밖" in result
    assert str(symlink_path) not in result
    assert str(outside_path) not in result
    assert calls == []
