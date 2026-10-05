"""``generate_table_class_map_report`` 가 실제로 동작하는지 회귀 가드.

배경 (2026-08-08 실측): 이 MCP 도구는 ``scripts/build_table_class_map_report.py`` 를
``exec_module`` 로 로드하는데, 그 파일이 **git 이력 전체에 존재한 적이 없었다**.
도구는 등록돼 있고 docstring 도 상세했지만 호출하면 언제나:

    {"success": false, "error": "[Errno 2] ... build_table_class_map_report.py"}

즉 100% 실패하는 도구가 MCP 표면에 노출돼 있었다. 실행하지 않으면 알 수 없고,
어떤 테스트도 이 도구를 호출하지 않았다.

이 파일은 (1) 스크립트 모듈의 계약과 (2) 도구가 실제로 ``success: true`` 를
반환하는지를 고정한다.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_generated_paths(tmp_path, monkeypatch):
    """보고서 테스트가 배포 ``data/generated`` 경로에 쓰지 못하게 한다."""
    import config

    generated = tmp_path / "generated"
    reports = generated / "reports"
    abox = generated / "abox"
    tbox = generated / "tbox" / "t_box.ttl"
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr(config, "GENERATED_REPORTS_DIR", str(reports))
    monkeypatch.setattr(config, "GENERATED_ABOX_DIR", str(abox))
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox))


def test_script_module_exists_and_exposes_contract():
    """도구가 기대하는 세 심볼 (``collect`` / ``render`` / ``_OUT``) 이 있어야 한다."""
    import importlib.util
    import os

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "build_table_class_map_report.py",
    )
    assert os.path.exists(script), (
        "도구가 로드하는 스크립트가 없다 — 호출 시 FileNotFoundError 로 100% 실패한다"
    )
    spec = importlib.util.spec_from_file_location("_tcmap_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.collect) and callable(module.render)
    assert isinstance(module._OUT, str) and module._OUT.endswith(".html")


def test_collect_returns_every_key_the_tool_reads():
    """``collect()`` 가 도구 응답 조립에 쓰이는 키를 모두 채운다.

    키 하나가 없으면 도구가 KeyError 로 죽는다 — 계약을 명시적으로 고정한다.
    """
    import importlib.util
    import os

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "build_table_class_map_report.py",
    )
    spec = importlib.util.spec_from_file_location("_tcmap_test2", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    data = module.collect()
    for key in ("rows", "total_columns", "total_rows", "total_instances",
                "total_links", "shared_columns", "derived", "abstract"):
        assert key in data, f"collect() 가 '{key}' 를 반환하지 않는다"
    for row in data["rows"]:
        # 도구가 row["base"] / row["rows"] / row["instances"] 를 직접 읽는다.
        for key in ("base", "rows", "instances"):
            assert key in row, f"row 에 '{key}' 가 없다"


def test_render_produces_standalone_html():
    """``render()`` 는 외부 의존 없는 단일 HTML 을 낸다 (오프라인 열람)."""
    import importlib.util
    import os

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "build_table_class_map_report.py",
    )
    spec = importlib.util.spec_from_file_location("_tcmap_test3", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    html = module.render(module.collect())
    assert html.startswith("<!DOCTYPE html")
    assert "</html>" in html
    assert "<script src=" not in html, "외부 스크립트 의존이 생기면 오프라인에서 깨진다"


def test_mcp_tool_succeeds(tmp_path, monkeypatch):
    """THE REGRESSION: 도구가 ``success: true`` 를 반환해야 한다.

    ``generate_table_class_map_report`` 는 출력 경로 파라미터가 없어 항상 배포
    경로 상수를 사용한다. autouse fixture가 config 경로를 ``tmp_path``로 바꿔
    end-to-end 동작을 검증하면서 실제 ``data/generated``는 건드리지 않는다.
    """
    from tools.local_artifacts import generate_table_class_map_report

    result = json.loads(generate_table_class_map_report(open_report=False))

    assert result.get("success") is True, (
        f"도구가 여전히 실패한다: {str(result.get('error'))[:160]}"
    )
    assert str(tmp_path) in result["path"]
    for key in ("path", "tables_mapped", "source_columns", "source_rows",
                "instances_created", "row_instance_mismatch",
                "classes_without_source"):
        assert key in result, f"응답에 '{key}' 가 없다"
    assert result["path"].endswith(".html")


def test_report_file_is_written(tmp_path):
    """보고서 파일이 격리된 임시 경로에 실제 생성된다."""
    import os

    from tools.local_artifacts import generate_table_class_map_report

    result = json.loads(generate_table_class_map_report(open_report=False))
    path = result["path"]
    assert str(tmp_path) in path
    assert os.path.exists(path) and os.path.getsize(path) > 500


@pytest.mark.parametrize("bom", [True, False])
def test_csv_shape_handles_bom(tmp_path, monkeypatch, bom):
    """헤더 BOM 이 컬럼 수 집계를 망가뜨리지 않는다."""
    import importlib.util
    import os

    script = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "build_table_class_map_report.py",
    )
    spec = importlib.util.spec_from_file_location("_tcmap_test4", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir()
    encoding = "utf-8-sig" if bom else "utf-8"
    (rawdata / "T_X.csv").write_text("A,B,C\n1,2,3\n", encoding=encoding)
    monkeypatch.setattr(module, "SOURCE_RAWDATA_DIR", str(rawdata))

    header, rows = module._csv_shape("T_X")
    assert header == ["A", "B", "C"], f"BOM={bom} 에서 헤더가 깨졌다: {header}"
    assert rows == 1
