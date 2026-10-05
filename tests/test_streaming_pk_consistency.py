"""streaming PK 판정이 선수집(전수 읽기)과 일치하는지 회귀 가드.

배경 (2026-08-09 실측): A-Box 생성의 두 경로가 **서로 다른 데이터** 로 PK 를 정했다.

  - ``_load_csv_rows``: streaming 모드에서 앞 **20,000행 표본** 으로 판정
  - ``_precollect_known_instance_uris``: CSV **전수** 를 읽어 판정

표본에서는 unique 하지만 전체에서는 중복인 컬럼이 있으면 두 판정이 갈린다:

    25,000행 CSV — alpha_code 는 앞 20K 에서만 unique, zeta_id 는 전 구간 unique
    전수   판정 -> zeta_id
    20K 표본 판정 -> alpha_code      ← 불일치

그러면 row loop 이 발행하는 인스턴스 IRI 가 ``known_instance_uris`` 에 **하나도
없다**. 정방향 FK 게이트는 타겟이 그 집합에 없으면 stub 으로 보고 OP·rdf:type 을
만들지 않으므로, 그 테이블로 향하는 **FK 가 전부 사라진다** —
``skipped_stub_fk_count`` 만 늘고 응답은 ``success: true`` 다.

수정: 선수집이 전수 읽기로 정한 PK 를 ``ctx.precollected_pk_columns`` 로 넘겨
row loop 이 재사용한다. 두 경로가 **구조적으로** 일치한다.
"""
from __future__ import annotations

import csv

import pytest

from tools.abox_generation import _detect_pk_column, _precollect_known_instance_uris

_TBOX_INFO = {"classes": {"Widget": {}}}


def _divergent_csv(path, *, sample_rows: int = 20000, extra: int = 5000) -> None:
    """앞 ``sample_rows`` 행에서만 ``alpha_code`` 가 unique 한 CSV."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["alpha_code", "zeta_id", "qty"])
        for i in range(sample_rows):
            writer.writerow([f"AC{i:06d}", f"ZK{i:06d}", 1])
        for i in range(extra):                       # alpha_code 중복 발생
            writer.writerow([f"AC{i:06d}", f"ZK{sample_rows + i:06d}", 2])


def _rows(path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def test_sample_and_full_really_can_diverge(tmp_path):
    """전제 확인: 표본/전수 판정이 실제로 갈린다.

    이 테스트가 실패하면 (휴리스틱이 바뀌어 갈리지 않게 되면) 아래 가드의 전제가
    사라지므로 명시적으로 고정한다.
    """
    path = tmp_path / "Widget.csv"
    _divergent_csv(path)
    rows = _rows(path)

    full_pk = _detect_pk_column(rows, "Widget")
    sample_pk = _detect_pk_column(rows[:20000], "Widget")

    assert full_pk != sample_pk, (
        f"표본/전수 판정이 같아져 전제가 무효 (both={full_pk})"
    )
    assert full_pk == "zeta_id" and sample_pk == "alpha_code"


def test_precollect_exports_the_full_file_pk(tmp_path):
    """THE REGRESSION: 선수집이 전수 판정 PK 를 호출부에 넘겨준다."""
    path = tmp_path / "Widget.csv"
    _divergent_csv(path)

    exported: dict = {}
    known = _precollect_known_instance_uris(
        [str(path)], None, _TBOX_INFO, pk_columns_out=exported,
    )

    assert exported.get("Widget") == "zeta_id", (
        f"선수집 PK 가 공유되지 않았다 (exported={exported}) — row loop 이 표본으로 "
        "다시 판정하면 FK 가 전부 stub 이 된다"
    )
    # 전수 판정이므로 25,000개 전부 IRI 를 갖는다.
    assert len(known) == 25000


def test_shared_pk_makes_emitted_iris_match_known_set(tmp_path):
    """공유 PK 를 쓰면 row loop 이 발행할 IRI 가 known 집합에 포함된다.

    row loop 전체를 돌리지 않고 IRI 조립 규칙으로 대조한다 — 두 경로가 같은 PK 를
    쓰면 같은 IRI 가 나온다는 것이 핵심이다.
    """
    from tools.abox_generation import _detect_pk_value, _uri_safe_local

    path = tmp_path / "Widget.csv"
    _divergent_csv(path)
    exported: dict = {}
    known = _precollect_known_instance_uris(
        [str(path)], None, _TBOX_INFO, pk_columns_out=exported,
    )
    shared_pk = exported["Widget"]
    rows = _rows(path)

    # 공유 PK 로 발행한 IRI 는 전부 known 에 있다.
    matched = 0
    for row in rows[:50]:
        value = _detect_pk_value(row, "Widget", pk_column=shared_pk,
                                 pk_is_authoritative=False)
        if not value:
            continue
        suffix = f"Widget_{_uri_safe_local(value)}"
        if any(uri.endswith(suffix) for uri in known):
            matched += 1
    assert matched == 50, f"공유 PK 로 만든 IRI 가 known 에 없다 ({matched}/50)"

    # 표본 PK 로 발행하면 어긋난다 — 수정 전 상태의 재현.
    sample_pk = _detect_pk_column(rows[:20000], "Widget")
    mismatched = 0
    for row in rows[20000:20050]:
        value = _detect_pk_value(row, "Widget", pk_column=sample_pk,
                                 pk_is_authoritative=False)
        if not value:
            continue
        suffix = f"Widget_{_uri_safe_local(value)}"
        if not any(uri.endswith(suffix) for uri in known):
            mismatched += 1
    assert mismatched > 0, (
        "표본 PK 로도 모든 IRI 가 일치하면 이 결함 시나리오가 성립하지 않는다"
    )


def test_ctx_exposes_the_shared_pk_field():
    """``AboxBuildContext`` 에 공유 필드가 있어야 한다 (배선 계약)."""
    from tools.abox_support import AboxBuildContext

    ctx = AboxBuildContext()
    assert hasattr(ctx, "precollected_pk_columns")
    assert ctx.precollected_pk_columns == {}


def test_row_loop_prefers_shared_pk_over_heuristic():
    """row loop 소스에 공유 PK 재사용 분기가 있어야 한다 (소스 레벨 고정).

    동작 테스트만으로는 다음 편집에서 분기가 사라지는 것을 막기 어렵다.
    """
    import inspect

    from tools.abox_generation import _process_single_csv

    source = inspect.getsource(_process_single_csv)
    assert "precollected_pk_columns" in source, (
        "row loop 이 선수집 PK 를 재사용하지 않는다 — 표본/전수 불일치가 재발한다"
    )


@pytest.mark.parametrize("size", [100, 1000])
def test_small_files_are_unaffected(tmp_path, size):
    """20K 미만 파일은 표본=전수이므로 동작이 바뀌지 않는다."""
    path = tmp_path / "Widget.csv"
    _divergent_csv(path, sample_rows=size, extra=0)
    exported: dict = {}
    _precollect_known_instance_uris(
        [str(path)], None, _TBOX_INFO, pk_columns_out=exported,
    )
    rows = _rows(path)
    assert exported.get("Widget") == _detect_pk_column(rows, "Widget")
