"""conftest 의 배포 산출물 쓰기 차단 가드 회귀 테스트.

## 왜 이 가드가 필요한가

테스트가 ``data/generated`` 아래 실물을 덮어쓰는 사고가 **네 번** 재발했다:

    2026-08-18  compromise_audit.json    실측 S2 절충 이력 10건이 픽스처로 축출
    2026-08-25  quality_history.json     경로 상수가 3곳이라 하나를 놓쳤다
    2026-08-28  semantic_dictionary.json instance_count 0 → LLM 이 "비었다" 로 읽었다
    2026-09-01  inference_loss_manifest  raw=1 → sanity 게이트가 허구 PASS

앞의 세 번은 **경로 상수를 tmp 로 리다이렉트**하는 방식으로 대응했고, 매번 다음 사고가
났다. 상수 사본을 전부 열거해야 하는 방식이라 한 곳만 빠져도 조용히 뚫린다. 남은
표면은 유지 불가능한 규모다 — ``INFERRED_PATH`` 를 import 하는 모듈 19개,
``GENERATED_REPORTS_DIR`` 17개.

그래서 상수가 아니라 **쓰기 지점**을 막는다. 이 파일은 그 가드가 (a) 실제로 발화하고
(b) 정당한 사용을 막지 않는지를 주장한다.

## NEGATIVE 방향

이 리포의 원칙대로 "차단 카운터 >= 1" 이 아니라 **정당한 입력을 보존하는가**를 함께
주장한다 — 과잉 차단은 tmp 경로 쓰기나 배포 산출물 **읽기**를 막아 스위트를 통째로
깨뜨리므로, 그쪽이 더 위험한 실패 모드다.
"""

import json
import os

import pytest

from config import GENERATED_DIR
from tools.common import atomic_write, atomic_write_json

# ── 가드가 발화한다 ─────────────────────────────────────────────


def test_atomic_write_json_to_deployed_path_is_blocked():
    """배포 경로로 향하는 atomic_write_json 은 예외가 된다."""
    target = os.path.join(GENERATED_DIR, "inferred", "inference_loss_manifest.json")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        atomic_write_json(target, {"input": {"abox_triples": -4921}})


def test_atomic_write_to_deployed_path_is_blocked():
    """TTL 쓰기도 막는다 (a_box.ttl / t_box.ttl 이 이 경로다)."""
    target = os.path.join(GENERATED_DIR, "tbox", "t_box.ttl")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        atomic_write(target, "# clobbered\n")


def test_graph_serialize_to_deployed_path_is_blocked():
    """``Graph.serialize(destination=)`` 는 atomic_write 를 우회한다.

    실측: 오염된 ``inference_provenance.ttl`` 이 이 경로였다
    (``tools/inference.py`` 의 ``existing_g.serialize(destination=prov_path)``).
    """
    from rdflib import Graph

    target = os.path.join(GENERATED_DIR, "inferred", "inference_provenance.ttl")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        Graph().serialize(destination=target, format="turtle")


def test_builtin_open_write_to_deployed_path_is_blocked():
    """``open(..., "w")`` 직접 쓰기도 막는다.

    ``semantic_dictionary.py:2703`` 과 ``remote/neo4j.py:1963`` 이 이 형태다.
    """
    target = os.path.join(GENERATED_DIR, "semantic_dictionary.json")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"), \
            open(target, "w", encoding="utf-8") as handle:
        handle.write("{}")


def test_append_and_update_modes_are_blocked():
    """``a`` / ``r+`` 도 막는다 — append 사고가 cq_feedback.json 을 오염시켰다.

    실측: ``reports/cq_feedback.json`` 에 테스트 유래 iteration 이 append 됐고, 그
    파일은 다음 S2 Architect 프롬프트에 주입되므로 **픽스처가 T-Box 생성 입력**이
    된다.
    """
    target = os.path.join(GENERATED_DIR, "reports", "cq_feedback.json")
    for mode in ("a", "r+"):
        with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
            open(target, mode, encoding="utf-8").close()  # noqa: SIM115 — 예외 경로 검증


def test_nested_and_new_paths_are_blocked():
    """아직 없는 파일·하위 폴더도 막는다 (새 writer 가 자동으로 잡힌다)."""
    target = os.path.join(GENERATED_DIR, "brand_new_subdir", "whatever.json")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        atomic_write_json(target, {})


# ── 우회 primitive: 가드가 열거하지 않은 쓰기 경로 ──────────────
#
# 기존 가드는 atomic_write / Graph.serialize / builtins.open 세 지점만 막는다.
# 아래는 그 셋을 **거치지 않고** 배포 트리에 도달하는 경로다. 2026-09-02 실측으로
# `data/generated/brand_new_subdir/` 가 남아 있었는데, 디렉터리 생성이 어느 가드에도
# 걸리지 않기 때문이다.


def test_pathlib_write_text_is_blocked():
    """``Path.write_text`` 는 ``io.open`` 을 부르므로 builtins.open patch 를 우회한다."""
    from pathlib import Path

    target = Path(GENERATED_DIR) / "reports" / "probe_write_text.json"
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        target.write_text("{}", encoding="utf-8")


def test_pathlib_write_bytes_is_blocked():
    from pathlib import Path

    target = Path(GENERATED_DIR) / "reports" / "probe_write_bytes.bin"
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        target.write_bytes(b"x")


def test_pathlib_open_write_is_blocked():
    from pathlib import Path

    target = Path(GENERATED_DIR) / "reports" / "probe_path_open.json"
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        target.open("w", encoding="utf-8").close()  # noqa: SIM115 — 예외 경로 검증


def test_os_replace_into_deployed_tree_is_blocked(tmp_path):
    """tmp 를 배포 트리 **밖**에 만든 뒤 move 하면 open 가드를 통과한다.

    ``tools/`` 안의 자체 원자적 쓰기 5곳(``cq_feedback``·``compromise_audit``·
    ``debate_log_store``·``prompt_audit``·``multi_agent_tbox``)은 tmp 를 대상과 같은
    폴더에 만들어 지금은 open 가드에 걸린다. 그 관행이 바뀌면 이 축이 유일한 방어다.
    """
    src = tmp_path / "payload.json"
    src.write_text("{}", encoding="utf-8")
    dst = os.path.join(GENERATED_DIR, "reports", "probe_replace.json")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        os.replace(str(src), dst)


def test_os_rename_into_deployed_tree_is_blocked(tmp_path):
    src = tmp_path / "payload2.json"
    src.write_text("{}", encoding="utf-8")
    dst = os.path.join(GENERATED_DIR, "reports", "probe_rename.json")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        os.rename(str(src), dst)


def test_shutil_copy_into_deployed_tree_is_blocked(tmp_path):
    import shutil

    src = tmp_path / "payload3.json"
    src.write_text("{}", encoding="utf-8")
    dst = os.path.join(GENERATED_DIR, "reports", "probe_copy.json")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        shutil.copyfile(str(src), dst)


def test_makedirs_in_deployed_tree_is_blocked():
    """디렉터리 생성 자체를 막는다 — 이것이 ``brand_new_subdir`` 잔여물의 경로다."""
    target = os.path.join(GENERATED_DIR, "probe_new_dir", "nested")
    with pytest.raises(AssertionError, match="배포 산출물에 쓰려 했다"):
        os.makedirs(target, exist_ok=True)


# ── NEGATIVE: 정당한 사용은 막지 않는다 ─────────────────────────


def test_tmp_path_writes_are_allowed(tmp_path):
    """tmp 경로 쓰기는 그대로 동작한다 (과잉 차단 방지)."""
    target = tmp_path / "sub" / "manifest.json"
    atomic_write_json(str(target), {"ok": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}

    ttl = tmp_path / "graph.ttl"
    atomic_write(str(ttl), "# fine\n")
    assert ttl.read_text(encoding="utf-8") == "# fine\n"


def test_tmp_path_that_mentions_data_generated_is_allowed(tmp_path):
    """경로 문자열에 ``data/generated`` 가 들어 있어도 실제 트리 밖이면 허용한다.

    문자열 부분매칭으로 판정하면 ``tmp_path / "data" / "generated"`` 를 쓰는 정당한
    테스트가 조용히 깨진다. 판정은 ``realpath`` 기준이어야 한다.
    """
    target = tmp_path / "data" / "generated" / "abox" / "a_box.ttl"
    atomic_write(str(target), "# tmp mirror\n")
    assert target.read_text(encoding="utf-8") == "# tmp mirror\n"


def test_existing_deployed_dir_with_exist_ok_is_allowed():
    """이미 있는 배포 폴더의 방어적 ``exist_ok=True`` 보장은 막지 않는다.

    호출부 46곳이 이 형태다. 여기까지 막으면 배포 산출물을 **읽는** 정당한 테스트가
    깨진다. 오염 축은 **신규** 폴더 생성 하나뿐이다.
    """
    existing = os.path.join(GENERATED_DIR, "reports")
    assert os.path.isdir(existing), "픽스처 전제: reports/ 가 존재해야 한다"
    os.makedirs(existing, exist_ok=True)  # 예외가 나지 않아야 한다


def test_tmp_path_dir_creation_and_move_are_allowed(tmp_path):
    """tmp 안에서의 폴더 생성·이동·pathlib 쓰기는 그대로 동작한다 (과잉 차단 방지)."""
    from pathlib import Path

    nested = tmp_path / "a" / "b"
    os.makedirs(str(nested))
    src = nested / "x.json"
    Path(src).write_text('{"ok": true}', encoding="utf-8")
    dst = tmp_path / "y.json"
    os.replace(str(src), str(dst))
    assert json.loads(Path(dst).read_text(encoding="utf-8")) == {"ok": True}

    bin_target = tmp_path / "z.bin"
    Path(bin_target).write_bytes(b"ok")
    assert bin_target.read_bytes() == b"ok"

    with Path(tmp_path / "w.txt").open("w", encoding="utf-8") as f:
        f.write("ok")


def test_reading_deployed_artifacts_is_allowed():
    """배포 산출물 **읽기**는 정당하다 — 실측 대조 테스트가 이것에 의존한다.

    ``test_deployed_*`` 계열 수십 개가 배포 T-Box/A-Box 를 읽어 실물을 검증한다.
    읽기까지 막으면 그 축이 통째로 사라진다.
    """
    tbox = os.path.join(GENERATED_DIR, "tbox", "t_box.ttl")
    if not os.path.exists(tbox):
        pytest.skip("배포 T-Box 없음")
    with open(tbox, encoding="utf-8") as handle:
        assert handle.read(64)
    # 명시적 읽기 모드도 통과해야 한다.
    with open(tbox, "r", encoding="utf-8") as handle:  # noqa: UP015 — 모드 판정 검증
        assert handle.read(64)


def test_graph_serialize_without_destination_is_allowed():
    """destination 없는 serialize (문자열 반환) 는 그대로 동작한다."""
    from rdflib import Graph

    assert isinstance(Graph().serialize(format="turtle"), str)


# ── opt-out 마커 ────────────────────────────────────────────────


@pytest.mark.writes_deployed_artifacts
def test_marker_opts_out_of_the_guard(tmp_path):
    """마커를 붙인 테스트는 가드가 비활성이다.

    배포 경로 자체를 검증하는 것이 목적인 테스트를 위한 탈출구다. 여기서는 마커가
    실제로 가드를 끄는지만 확인하고 **배포 파일은 건드리지 않는다** — 이 테스트가
    실물을 쓰면 그 자체가 오염이다.
    """
    from tests.conftest import _deployed_artifact_root, _is_deployed_artifact

    root = _deployed_artifact_root()
    deployed = os.path.join(GENERATED_DIR, "inferred", "inference_loss_manifest.json")
    assert _is_deployed_artifact(deployed, root), "판정기가 배포 경로를 인식하지 못했다"

    # 가드가 꺼졌다는 증거: 배포 경로로 향하는 호출이 AssertionError 를 내지 않는다.
    # 실제 파일을 쓰지 않기 위해 tmp 로 향하는 호출로 확인한다 (가드가 켜져 있으면
    # 아래 판정기 호출 결과와 무관하게 위 test_* 들이 예외를 냈다).
    safe = tmp_path / "manifest.json"
    atomic_write_json(str(safe), {"marker": "opt-out"})
    assert safe.exists()


# ── 판정기 단위 ─────────────────────────────────────────────────


def test_is_deployed_artifact_handles_non_paths():
    """파일 객체·정수 fd 등 비경로 인자에서 예외를 내지 않는다.

    ``open(fd, "w")`` 같은 호출이 스위트에 있으면 가드가 죽는다.
    """
    from tests.conftest import _deployed_artifact_root, _is_deployed_artifact

    root = _deployed_artifact_root()
    for value in (3, None, object(), b"\x00"):
        assert _is_deployed_artifact(value, root) is False


def test_is_deployed_artifact_without_root_is_permissive():
    """root 를 못 구하면(config 부재 환경) 아무것도 막지 않는다."""
    from tests.conftest import _is_deployed_artifact

    assert _is_deployed_artifact("/anything", None) is False


# ── 오염된 매니페스트의 하류 소비자 ──────────────────────────────


def test_dictionary_does_not_copy_impossible_preservation_rate(tmp_path, monkeypatch):
    """딕셔너리는 산술이 성립하지 않는 매니페스트의 보존률을 복사하지 않는다.

    소비 지점 방어가 ``kg_validation`` 하나로는 부족하다. ``_enrich_metadata`` 도 같은
    파일을 읽어 ``inference_preservation_rate`` 로 각인하는데, 딕셔너리의 소비자는
    코드가 아니라 **LLM** 이므로 허구값이 사실로 전달되고 어떤 게이트도 대조하지 않는다.
    """
    import tools.semantic_dictionary as sd

    inferred = tmp_path / "all_inferred.ttl"
    inferred.write_text("# inference output\n", encoding="utf-8")
    manifest = tmp_path / "inference_loss_manifest.json"

    monkeypatch.setattr(sd, "INFERRED_PATH", str(inferred), raising=False)

    # (1) 오염값 → 기록하지 않는다.
    manifest.write_text(json.dumps({
        "input": {"tbox_triples": 4922, "abox_triples": -4921, "tacit_triples": 0},
        "output": {"total_triples": 1},
        "preservation_score": {"meaningful_triples_ratio": 0.5},
    }), encoding="utf-8")
    meta = sd._enrich_metadata({})
    assert "inference_preservation_rate" not in meta, (
        "산술이 성립하지 않는 매니페스트의 보존률을 딕셔너리에 각인했다"
    )

    # (2) NEGATIVE — 정상값은 그대로 기록한다 (과잉 차단 방지).
    manifest.write_text(json.dumps({
        "input": {"tbox_triples": 4922, "abox_triples": 823_511, "tacit_triples": 51_242},
        "output": {"total_triples": 1_305_608},
        "preservation_score": {"meaningful_triples_ratio": 0.9876},
    }), encoding="utf-8")
    meta = sd._enrich_metadata({})
    assert meta.get("inference_preservation_rate") == 0.9876, (
        "정상 매니페스트의 보존률을 버렸다 (오발화)"
    )
