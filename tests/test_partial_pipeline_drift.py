import json
from unittest.mock import patch

import pytest

from tools.drift_partial_pipeline import run_partial_pipeline_on_drift


def _fake_drift(per_table):
    return {
        "tables": len(per_table),
        "total_added_values": sum(
            sum(d.get("added_count", 0) for d in t.get("delta", {}).values())
            for t in per_table
        ),
        "total_emerging_values": 0,
        "per_table": per_table,
    }


def test_no_drift_returns_empty_list():
    fake = _fake_drift([
        {"table": "Alpha", "had_previous": True, "delta": {}, "drift_scores": {}},
    ])
    with patch("tools.drift_partial_pipeline.monitor_all_csvs", return_value=fake):
        result = json.loads(run_partial_pipeline_on_drift(dry_run=True))
    assert result["success"] is True
    assert result["incremental_tables"] == []
    assert result["drift_detected_tables"] == []


def test_threshold_filter():
    # Table has 5 added values across columns, threshold=10 → skip
    fake = _fake_drift([
        {"table": "Alpha", "had_previous": True,
         "delta": {"col1": {"added_count": 5, "removed_count": 0}},
         "drift_scores": {}},
    ])
    with patch("tools.drift_partial_pipeline.monitor_all_csvs", return_value=fake):
        result = json.loads(run_partial_pipeline_on_drift(
            added_value_threshold=10, dry_run=True,
        ))
    assert result["drift_detected_tables"] == ["Alpha"]
    assert result["incremental_tables"] == []


def test_threshold_pass():
    fake = _fake_drift([
        {"table": "Alpha", "had_previous": True,
         "delta": {"col1": {"added_count": 20, "removed_count": 0}},
         "drift_scores": {}},
        {"table": "Beta", "had_previous": True,
         "delta": {"col1": {"added_count": 3, "removed_count": 0}},
         "drift_scores": {}},
    ])
    with patch("tools.drift_partial_pipeline.monitor_all_csvs", return_value=fake):
        result = json.loads(run_partial_pipeline_on_drift(
            added_value_threshold=10, dry_run=True,
        ))
    assert "Alpha" in result["incremental_tables"]
    assert "Beta" not in result["incremental_tables"]


def test_dry_run_skips_tbox_update():
    fake = _fake_drift([
        {"table": "Alpha", "had_previous": True,
         "delta": {"col1": {"added_count": 20, "removed_count": 0}},
         "drift_scores": {}},
    ])
    with patch("tools.drift_partial_pipeline.monitor_all_csvs", return_value=fake), \
         patch("tools.drift_partial_pipeline.update_tbox_incremental") as upd:
        result = json.loads(run_partial_pipeline_on_drift(
            added_value_threshold=10, dry_run=True,
        ))
    assert result["tbox_update_result"] is None
    upd.assert_not_called()


def test_non_dry_run_invokes_tbox_update():
    fake = _fake_drift([
        {"table": "Alpha", "had_previous": True,
         "delta": {"col1": {"added_count": 20, "removed_count": 0}},
         "drift_scores": {}},
    ])
    with patch("tools.drift_partial_pipeline.monitor_all_csvs", return_value=fake), \
         patch(
            "tools.drift_partial_pipeline.update_tbox_incremental",
            return_value=json.dumps({"success": True, "updated": ["Alpha"]}),
         ) as upd:
        result = json.loads(run_partial_pipeline_on_drift(
            added_value_threshold=10, dry_run=False,
        ))
    upd.assert_called_once_with(changed_tables="Alpha")
    assert result["tbox_update_result"] is not None
    assert result["tbox_update_result"].get("success") is True


def test_multi_table_joined_with_comma():
    """2개 이상 테이블이 threshold 를 넘으면 쉼표 구분 string 으로 전달."""
    fake = _fake_drift([
        {"table": "Alpha", "had_previous": True,
         "delta": {"col1": {"added_count": 20, "removed_count": 0}},
         "drift_scores": {}},
        {"table": "Beta", "had_previous": True,
         "delta": {"col1": {"added_count": 15, "removed_count": 0}},
         "drift_scores": {}},
        {"table": "Gamma", "had_previous": True,
         "delta": {"col1": {"added_count": 30, "removed_count": 0}},
         "drift_scores": {}},
    ])
    with patch("tools.drift_partial_pipeline.monitor_all_csvs", return_value=fake), \
         patch(
            "tools.drift_partial_pipeline.update_tbox_incremental",
            return_value=json.dumps({"success": True}),
         ) as upd:
        result = json.loads(run_partial_pipeline_on_drift(
            added_value_threshold=10, dry_run=False,
        ))
    # 세 테이블 모두 incremental 대상
    assert result["incremental_tables"] == ["Alpha", "Beta", "Gamma"]
    # update_tbox_incremental 은 comma-joined string 으로 한 번만 호출
    upd.assert_called_once_with(changed_tables="Alpha,Beta,Gamma")


def test_multi_column_added_count_aggregated():
    """한 테이블의 여러 컬럼 added_count 합계가 threshold 판정에 쓰인다."""
    fake = _fake_drift([
        # col1=5, col2=7 → sum=12 >= 10 → include
        {"table": "Alpha", "had_previous": True,
         "delta": {
             "col1": {"added_count": 5, "removed_count": 0},
             "col2": {"added_count": 7, "removed_count": 0},
         },
         "drift_scores": {}},
        # col1=3, col2=4 → sum=7 < 10 → skip
        {"table": "Beta", "had_previous": True,
         "delta": {
             "col1": {"added_count": 3, "removed_count": 0},
             "col2": {"added_count": 4, "removed_count": 0},
         },
         "drift_scores": {}},
    ])
    with patch("tools.drift_partial_pipeline.monitor_all_csvs", return_value=fake):
        result = json.loads(run_partial_pipeline_on_drift(
            added_value_threshold=10, dry_run=True,
        ))
    assert result["incremental_tables"] == ["Alpha"]
    assert "Beta" in result["drift_detected_tables"]  # delta 존재하므로 detected


# ── dry_run 이 근거를 소모하지 않는가 (2026-08-30) ────────────────────────
#
# ``snapshot_csv`` 가 delta 계산 직후 **무조건** baseline 을 덮어썼다. 그래서 같은
# CSV 를 두 번 재면 두 번째는 drift 0건이 된다. 특히 ``dry_run=True`` 는 "대상 테이블만
# 보고 T-Box 는 건드리지 않는다" 고 문서화돼 있는데, 그 **프리뷰 행위 자체가** 실제
# 실행에 필요한 근거를 지웠다. 격리 실험:
#
#     dry_run 호출        → incremental_tables=['T']
#     바로 다음 동일 호출  → []            ← 근거 소모
#
# 위 테스트들은 ``monitor_all_csvs`` 를 mock 하므로 이 배선을 볼 수 없다 — 실제 파일로
# 확인해야 한다 (이 리포의 "산출물로 확인하라": 함수 경계 아래 배선은 mock 으로 안 잡힌다).


def _write_csv(path, rows: int):
    import csv

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ID", "STATUS"])
        for i in range(rows):
            writer.writerow([f"R{i}", f"V{i}"])


@pytest.fixture
def drift_sandbox(tmp_path, monkeypatch):
    """격리된 rawdata + drift 스냅샷 디렉토리."""
    import tools.drift_monitor as dm

    raw = tmp_path / "rawdata"
    drift = tmp_path / "drift"
    raw.mkdir()
    drift.mkdir()
    monkeypatch.setattr(dm, "DRIFT_DIR", str(drift))
    monkeypatch.setattr(dm, "SOURCE_RAWDATA_DIR", str(raw))
    monkeypatch.setattr(
        "tools.drift_partial_pipeline.update_tbox_incremental",
        lambda **kwargs: '{"success": true}',
    )
    csv_path = raw / "T.csv"
    _write_csv(csv_path, 2)
    dm.snapshot_csv(str(csv_path))       # baseline 확립
    _write_csv(csv_path, 20)             # 신규 값 18개
    return str(raw), csv_path


def test_dry_run_does_not_consume_the_drift_evidence(drift_sandbox):
    """THE REGRESSION: dry_run 을 반복해도 대상 테이블이 유지된다."""
    raw, _ = drift_sandbox
    first = json.loads(run_partial_pipeline_on_drift(rawdata_dir=raw, dry_run=True))
    second = json.loads(run_partial_pipeline_on_drift(rawdata_dir=raw, dry_run=True))
    assert first["incremental_tables"] == ["T"], first
    assert second["incremental_tables"] == ["T"], (
        "프리뷰가 baseline 을 전진시켜 근거를 소모했다 — 두 번째 호출이 비었다"
    )


def test_real_run_advances_the_baseline(drift_sandbox):
    """PRESERVATION: 실제 실행은 baseline 을 전진시킨다.

    ``save=False`` 를 무조건 쓰면 drift 가 영구히 재감지돼 매 실행 T-Box 를 갱신한다.
    """
    raw, _ = drift_sandbox
    real = json.loads(run_partial_pipeline_on_drift(rawdata_dir=raw, dry_run=False))
    assert real["incremental_tables"] == ["T"], real
    after = json.loads(run_partial_pipeline_on_drift(rawdata_dir=raw, dry_run=True))
    assert after["incremental_tables"] == [], (
        "실제 실행 후에도 같은 drift 가 남았다 — baseline 이 전진하지 않았다"
    )


def test_snapshot_saved_is_reported(drift_sandbox):
    """부수효과를 응답에 드러낸다 — "왜 두 번째가 0건인가" 를 추적할 수 있어야 한다."""
    import tools.drift_monitor as dm

    raw, csv_path = drift_sandbox
    preview = dm.snapshot_csv(str(csv_path), save=False)
    assert preview["snapshot_saved"] is False
    committed = dm.snapshot_csv(str(csv_path), save=True)
    assert committed["snapshot_saved"] is True


def test_preview_mode_leaves_the_snapshot_file_untouched(drift_sandbox):
    """파일 수준 확인: save=False 는 스냅샷 파일을 바꾸지 않는다."""
    import tools.drift_monitor as dm

    _, csv_path = drift_sandbox
    snap = dm._snapshot_path("T")

    def _read() -> str:
        with open(snap, encoding="utf-8") as handle:
            return handle.read()

    before = _read()
    dm.snapshot_csv(str(csv_path), save=False)
    assert _read() == before, "save=False 인데 스냅샷 파일이 바뀌었다"
    dm.snapshot_csv(str(csv_path), save=True)
    assert _read() != before, "save=True 인데 스냅샷이 갱신되지 않았다"


def test_dry_run_wiring_passes_save_false():
    """배선 계약: dry_run 이 실제로 ``save=not dry_run`` 을 넘기는가.

    소스 문자열 검사로는 주석에 매칭될 수 있으므로 AST 로 호출 키워드를 본다
    (같은 함정을 test_dict_sync 에서 겪었다).
    """
    import ast
    import inspect
    import textwrap

    from tools import drift_partial_pipeline as dpp

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(dpp.run_partial_pipeline_on_drift)))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "monitor_all_csvs"
    ]
    assert calls, "monitor_all_csvs 를 부르지 않는다"
    assert any(kw.arg == "save" for call in calls for kw in call.keywords), (
        "monitor_all_csvs 에 save 를 넘기지 않는다 — 프리뷰가 baseline 을 소모한다"
    )


# ── cold start 를 drift 로 오독하지 않는가 (2026-08-30) ───────────────────
#
# ``_total_added`` 가 ``had_previous`` 를 보지 않아, baseline 이 없으면 **모든 값이
# 신규**로 집계됐다. 배포 상태 실측: ``data/generated/drift/`` 가 존재하지 않으므로
# run_partial_pipeline_on_drift 첫 호출에서 **40개 전 테이블**이 incremental 로
# 판정된다. 그 경로(update_tbox_incremental)는 S2 의 save_guard(표현력 손실률·산 링크
# 검사)를 거치지 않고 T-Box 를 직접 쓰므로, cold start 한 번이 T-Box 를 대량
# 재작성할 수 있다.


def test_cold_start_is_not_treated_as_drift(tmp_path, monkeypatch):
    """THE REGRESSION: baseline 이 없으면 T-Box 갱신을 트리거하지 않는다."""
    import tools.drift_monitor as dm

    raw = tmp_path / "rawdata"
    raw.mkdir()
    monkeypatch.setattr(dm, "DRIFT_DIR", str(tmp_path / "drift"))
    monkeypatch.setattr(dm, "SOURCE_RAWDATA_DIR", str(raw))
    calls: list = []
    monkeypatch.setattr(
        "tools.drift_partial_pipeline.update_tbox_incremental",
        lambda **kwargs: calls.append(kwargs) or '{"success": true}',
    )
    _write_csv(raw / "T.csv", 30)

    result = json.loads(
        run_partial_pipeline_on_drift(rawdata_dir=str(raw), dry_run=False))
    assert result["incremental_tables"] == [], (
        "최초 관측을 drift 로 읽어 T-Box 갱신 대상에 넣었다"
    )
    assert calls == [], "cold start 가 update_tbox_incremental 을 호출했다"
    assert result["no_baseline_tables"] == ["T"], (
        "판정 불가를 '변화 없음' 과 구분해 보고하지 않았다"
    )


def test_real_drift_after_baseline_is_still_detected(tmp_path, monkeypatch):
    """PRESERVATION: baseline 이 생긴 뒤의 진짜 drift 는 그대로 잡는다.

    cold start 를 무시하는 가드가 실제 drift 까지 삼키면 게이트를 끈 것이다.
    """
    import tools.drift_monitor as dm

    raw = tmp_path / "rawdata"
    raw.mkdir()
    monkeypatch.setattr(dm, "DRIFT_DIR", str(tmp_path / "drift"))
    monkeypatch.setattr(dm, "SOURCE_RAWDATA_DIR", str(raw))
    calls: list = []
    monkeypatch.setattr(
        "tools.drift_partial_pipeline.update_tbox_incremental",
        lambda **kwargs: calls.append(kwargs) or '{"success": true}',
    )
    csv_path = raw / "T.csv"
    _write_csv(csv_path, 30)
    run_partial_pipeline_on_drift(rawdata_dir=str(raw), dry_run=False)  # baseline

    _write_csv(csv_path, 60)                                            # 신규 30
    result = json.loads(
        run_partial_pipeline_on_drift(rawdata_dir=str(raw), dry_run=False))
    assert result["incremental_tables"] == ["T"], result
    assert result["no_baseline_tables"] == []
    assert len(calls) == 1, "진짜 drift 인데 T-Box 갱신을 부르지 않았다"


def test_no_baseline_note_is_reported(tmp_path, monkeypatch):
    """판정 불가 사실을 사람이 읽을 안내로 남긴다 (조용한 skip 방지)."""
    import tools.drift_monitor as dm

    raw = tmp_path / "rawdata"
    raw.mkdir()
    monkeypatch.setattr(dm, "DRIFT_DIR", str(tmp_path / "drift"))
    monkeypatch.setattr(dm, "SOURCE_RAWDATA_DIR", str(raw))
    monkeypatch.setattr(
        "tools.drift_partial_pipeline.update_tbox_incremental",
        lambda **kwargs: '{"success": true}',
    )
    _write_csv(raw / "T.csv", 5)
    result = json.loads(
        run_partial_pipeline_on_drift(rawdata_dir=str(raw), dry_run=True))
    assert "baseline" in result["note"], result["note"]
