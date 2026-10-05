"""S2 산출물 스냅샷 — S3 가 제자리에서 덮어쓰기 전의 사본을 남기는가.

## 왜 (2026-09-03 실측)

S3 는 ``TBOX_PATH`` 를 제자리에서 덮어쓴다. 그래서 "S3 를 고쳐서 다시 돌려야 한다"
는 상황이 오면 입력(S2 출력)이 이미 없다. 그날 실제로 겪었다: S3 스텝 두 개를 고친
뒤 재적용해야 했는데 S2 출력이 없어서 **S3 산출물에 S3 를 한 번 더** 돌렸고,
``step_13`` 비멱등성이 발화해 근거 없는 존재 공리가 생겨 추론 위반 34,562건이 났다.

S2 는 15~40분 / Bedrock 8~12회다. 그 산출물을 잃으면 재현 비용이 그대로 든다.
"""
from __future__ import annotations

import os

from tools import multi_agent_tbox as mat


def test_snapshot_written_next_to_tbox(tmp_path, monkeypatch):
    """스냅샷이 T-Box 와 같은 폴더에 ``t_box_s2out_<ts>.ttl`` 로 저장된다."""
    fake_tbox = tmp_path / "tbox" / "t_box.ttl"
    fake_tbox.parent.mkdir(parents=True)
    monkeypatch.setattr(mat, "TBOX_PATH", str(fake_tbox))

    ttl = "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    path = mat._snapshot_s2_output(ttl)

    assert path is not None, "스냅샷 경로가 None 이다"
    assert os.path.exists(path), path
    name = os.path.basename(path)
    assert name.startswith("t_box_s2out_") and name.endswith(".ttl"), name
    assert os.path.dirname(path) == str(fake_tbox.parent)
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == ttl, "스냅샷 내용이 저장 대상과 다르다"


def test_snapshot_does_not_overwrite_previous(tmp_path, monkeypatch):
    """두 번 호출하면 **덮어쓰지 않고** 사본이 유지된다 (타임스탬프가 다르다).

    같은 초에 두 번 부르면 같은 이름이 되므로 여기서는 시각을 고정하지 않고
    "이전 파일이 사라지지 않는다" 만 주장한다 — 스텝이 조용히 파일을 지우지 않는
    것이 이 장치의 요건이다.
    """
    fake_tbox = tmp_path / "tbox" / "t_box.ttl"
    fake_tbox.parent.mkdir(parents=True)
    monkeypatch.setattr(mat, "TBOX_PATH", str(fake_tbox))

    existing = fake_tbox.parent / "t_box_s2out_20200101000000.ttl"
    existing.write_text("old", encoding="utf-8")

    mat._snapshot_s2_output("new content")

    assert existing.exists(), "기존 스냅샷을 지웠다"
    assert existing.read_text(encoding="utf-8") == "old"


def test_snapshot_failure_does_not_raise(tmp_path, monkeypatch):
    """스냅샷 실패가 S2 저장을 되돌리게 해서는 안 된다 (예외를 삼킨다)."""
    monkeypatch.setattr(mat, "TBOX_PATH", str(tmp_path / "tbox" / "t_box.ttl"))

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mat, "atomic_write", _boom)
    assert mat._snapshot_s2_output("x") is None


def test_finalize_reports_snapshot_path():
    """응답에 ``s2_snapshot`` 이 실려야 한다 — 없으면 아무도 사본을 못 찾는다."""
    import inspect

    src = inspect.getsource(mat._finalize_and_save)
    assert '"s2_snapshot": state.get("s2_snapshot")' in src, (
        "_finalize_and_save 응답에 s2_snapshot 이 없다"
    )
    assert "_snapshot_s2_output(final_ttl)" in src, (
        "저장 직후 스냅샷을 호출하지 않는다"
    )
    # 스냅샷은 atomic_write(TBOX_PATH) **뒤** 여야 한다 (저장된 내용과 같아야 한다).
    assert src.index("atomic_write(TBOX_PATH, final_ttl)") < src.index(
        "_snapshot_s2_output(final_ttl)"
    )
