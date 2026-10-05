"""사이드카 소스 계보 각인 — 세대 어긋남을 구조적으로 탐지 가능하게 한다.

## 무엇이 틀렸었나 (실측 2026-09-02)

배포 트리의 사이드카 6종이 실제 산출물을 서술하지 않았다:

  reports/ontoclean_report.json     {"error": "T-Box not found: /nonexistent/t.ttl"}
  reports/fair_score.json           tbox_path = /tmp/pytest-…/tbox.ttl
  reports/canonical_compare.json    path_a/b 가 pytest tmpdir, triples 3
  inferred/inference_loss_manifest   abox_triples -4921 인데 보존율 1.0
  inferred/inference_provenance.ttl  23줄, totalInferred 0 (실제 폐쇄 1,305,608)
  inferred/inference_justifications  전부 0

원인 세 겹:
  ① 도구가 임의 입력 경로를 받고 출력은 배포 상수에 쓴다
  ② 사이드카에 소스 지문이 없어 세대 어긋남이 구조적으로 탐지 불가
  ③ 에러 응답이 그대로 보고서로 저장된다

## 이 테스트가 지키는 두 방향

- POSITIVE: 세대가 어긋나면 대조가 그것을 이름과 사유로 말한다.
- NEGATIVE: 정당한 기본 호출은 막지 않고, 각인 부재를 "일치" 로 읽지 않는다.
"""

from __future__ import annotations

import json
import os

import pytest

from tools.common import (
    add_source_artifacts,
    artifact_fingerprint,
    is_deployed_input,
    source_stamp,
    stale_sources,
    write_deployed_sidecar,
)

# ── 지문 ────────────────────────────────────────────────────────


def test_fingerprint_records_content_not_just_mtime(tmp_path):
    """오염은 항상 **지금** 쓰이므로 mtime 으로는 낡아 보이지 않는다.

    내용 해시가 유일하게 신뢰 가능한 축이다 (kg_validation 의
    ``_raw_triples_from_inference_manifest`` docstring 이 같은 사고를 기록한다).
    """
    f = tmp_path / "a.ttl"
    f.write_text("gen1", encoding="utf-8")
    first = artifact_fingerprint(str(f))
    assert first["exists"] and first["sha256"]

    # mtime 을 과거로 되돌려도 내용이 같으면 지문은 같다
    os.utime(f, (1, 1))
    assert artifact_fingerprint(str(f))["sha256"] == first["sha256"]

    # 내용이 바뀌면 mtime 이 과거여도 지문이 다르다
    f.write_text("gen2", encoding="utf-8")
    os.utime(f, (1, 1))
    assert artifact_fingerprint(str(f))["sha256"] != first["sha256"]


def test_fingerprint_of_missing_file_is_not_an_error(tmp_path):
    fp = artifact_fingerprint(str(tmp_path / "nope"))
    assert fp["exists"] is False
    assert "sha256" not in fp


# ── 대조 ────────────────────────────────────────────────────────


def test_unchanged_sources_report_no_drift(tmp_path):
    f = tmp_path / "a.ttl"
    f.write_text("x", encoding="utf-8")
    assert stale_sources(source_stamp(tbox=str(f))) == []


def test_changed_source_is_named_with_reason(tmp_path):
    f = tmp_path / "a.ttl"
    f.write_text("x", encoding="utf-8")
    stamp = source_stamp(tbox=str(f))
    f.write_text("y", encoding="utf-8")
    drift = stale_sources(stamp)
    assert [d["name"] for d in drift] == ["tbox"]
    assert drift[0]["reason"] == "내용이 달라졌다"


def test_deleted_and_appeared_sources_are_both_drift(tmp_path):
    present = tmp_path / "a.ttl"
    present.write_text("x", encoding="utf-8")
    absent = tmp_path / "b.ttl"
    stamp = source_stamp(kept=str(present), later=str(absent))
    present.unlink()
    absent.write_text("new", encoding="utf-8")
    reasons = {d["name"]: d["reason"] for d in stale_sources(stamp)}
    assert reasons["kept"] == "각인 당시 있던 파일이 지금 없다"
    assert reasons["later"] == "각인 당시 없던 파일이 지금 있다"


@pytest.mark.parametrize("stamp", [None, {}, {"artifacts": "not a dict"}, 42])
def test_absent_stamp_is_drift_not_agreement(stamp):
    """"대조할 수 없다" 를 "일치한다" 로 읽지 않는다."""
    drift = stale_sources(stamp)
    assert [d["name"] for d in drift] == ["_stamp"]


# ── 배포 입력 판정 ──────────────────────────────────────────────


def test_empty_input_means_deployed(tmp_path):
    """빈 값은 "기본값을 쓰라" 는 뜻이다."""
    deployed = str(tmp_path / "t.ttl")
    assert is_deployed_input("", deployed) is True
    assert is_deployed_input(None, deployed) is True


def test_symlink_cannot_dodge_the_deployed_check(tmp_path):
    real = tmp_path / "t.ttl"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.ttl"
    os.symlink(real, link)
    assert is_deployed_input(str(link), str(real)) is True


def test_other_path_is_not_deployed(tmp_path):
    assert is_deployed_input(str(tmp_path / "other.ttl"), str(tmp_path / "t.ttl")) is False


# ── 쓰기 게이트: 세 조건 ────────────────────────────────────────


def test_error_payload_is_not_persisted(tmp_path):
    out = tmp_path / "report.json"
    r = write_deployed_sidecar(
        str(out), {"error": "T-Box not found: /nonexistent/t.ttl"},
        sources={"tbox": str(tmp_path / "t.ttl")},
    )
    assert r["written"] is False
    assert "에러 응답" in r["reason"]
    assert not out.exists(), "실패한 분석이 보고서로 남으면 다음 소비자가 사실로 읽는다"


def test_adhoc_input_does_not_overwrite_deployed(tmp_path):
    out = tmp_path / "report.json"
    out.write_text('{"keep": true}', encoding="utf-8")
    r = write_deployed_sidecar(
        str(out), {"ok": True},
        sources={"graph": "/tmp/whatever.ttl"},
        inputs_are_deployed=False,
    )
    assert r["written"] is False
    assert json.loads(out.read_text(encoding="utf-8")) == {"keep": True}


def test_deployed_write_stamps_and_preserves_payload(tmp_path):
    src = tmp_path / "t.ttl"
    src.write_text("x", encoding="utf-8")
    out = tmp_path / "report.json"
    payload = {"score": 1}
    r = write_deployed_sidecar(str(out), payload, sources={"tbox": str(src)})
    assert r["written"] is True
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["score"] == 1
    assert stale_sources(saved["_source"]) == []
    # 호출자 dict 를 변형하지 않는다
    assert "_source" not in payload


# ── 출력 각인 덧붙이기 (사이드카가 자기 출력보다 먼저 쓰이는 경우) ──


def test_add_source_artifacts_merges_into_existing_stamp(tmp_path):
    """추론 사이드카는 all_inferred.ttl **직렬화 전**에 쓰인다.

    그 시점에 출력을 해시하면 이전 세대가 찍힌다. 그래서 입력은 쓰기 시점,
    출력은 직렬화 이후에 덧붙인다.
    """
    tbox = tmp_path / "t.ttl"
    tbox.write_text("t", encoding="utf-8")
    out = tmp_path / "manifest.json"
    out.write_text(json.dumps({"n": 1, "_source": source_stamp(tbox=str(tbox))}), encoding="utf-8")

    inferred = tmp_path / "all_inferred.ttl"
    inferred.write_text("i", encoding="utf-8")
    assert add_source_artifacts(str(out), inferred=str(inferred)) is True

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["n"] == 1
    assert set(saved["_source"]["artifacts"]) == {"tbox", "inferred"}
    assert stale_sources(saved["_source"]) == []

    # 출력만 다음 세대로 교체하면 대조가 그것을 짚는다
    inferred.write_text("i2", encoding="utf-8")
    assert [d["name"] for d in stale_sources(saved["_source"])] == ["inferred"]


def test_add_source_artifacts_creates_stamp_when_absent(tmp_path):
    out = tmp_path / "m.json"
    out.write_text('{"n": 1}', encoding="utf-8")
    f = tmp_path / "a.ttl"
    f.write_text("x", encoding="utf-8")
    assert add_source_artifacts(str(out), inferred=str(f)) is True
    assert stale_sources(json.loads(out.read_text(encoding="utf-8"))["_source"]) == []


@pytest.mark.parametrize("content", ["not json", "[1,2,3]"])
def test_add_source_artifacts_is_quiet_on_unusable_files(tmp_path, content):
    """각인 실패가 추론 결과 반환을 막아서는 안 된다."""
    out = tmp_path / "m.json"
    out.write_text(content, encoding="utf-8")
    assert add_source_artifacts(str(out), inferred=str(tmp_path / "x")) is False


def test_add_source_artifacts_on_missing_file_is_false(tmp_path):
    assert add_source_artifacts(str(tmp_path / "nope.json"), inferred="x") is False
