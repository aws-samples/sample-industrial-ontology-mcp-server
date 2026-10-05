"""검사기가 고장나면 저장을 **거부** 한다 — 손실 판정의 fail-open 회귀 가드.

2026-08-26 fault injection 실측. ``_guard_before_save`` 의 손실 판정 블록
(선언 능력 비교 · S3 정규화 면제 · live_link 축, 약 160줄) 은 하나의 ``try`` 로
감싸여 있고, 그 ``except`` 는 ``logger.debug`` 한 줄만 남기고 초안을 그대로
반환했다. 즉 **검사기 버그 한 개가 가드 전체를 무력화** 했다.

같은 초안(op_links 90개를 AnnotationProperty 로 바꿔 트리플 수는 보존)으로 측정:

    baseline                          blocked=True   block_reason=live_link_loss
    _declaration_capabilities 예외    blocked=False  block_reason=None   ← 저장됨

그 사실은 어디에도 남지 않았다. ``server.py:70`` 이 root 로거를 INFO 로 두므로
``logger.debug`` 는 stderr·파일 핸들러 양쪽에서 사라진다. 이 리포가 반복 기록한
"검증기 실패 = 검증 없음" 이 그대로 재현된 형태다.

## 왜 fail-closed 인가

가드의 의도는 "기존 산출물이 미검증 초안보다 낫다" 다 (docstring: *50분치 작업물보다
기존 산출물이 낫다*). 판정 불가는 그 전제를 뒤집지 않는다 — 오히려 **판정을 못 했다는
사실이 위험 신호** 다. 반대 방향(fail-open)은 "검사가 죽었으니 통과" 이고, 그것은
게이트가 아니라 장식이다.

## 예외 — 기존 T-Box 가 없을 때

보존할 것이 없으면 초안을 살린다. 부트스트랩(첫 실행)에서 fail-closed 로 막으면
어떤 T-Box 도 만들 수 없다. 같은 논리를 ``_live_link_losses`` 의
``signal_unavailable`` 도 쓴다 — 신호가 없으면 차단하지 않는다.

## 이 테스트의 방향

"차단 카운터 >= 1" 로는 부족하다 (게이트를 항상-차단으로 만들어도 통과한다).
세 축을 함께 주장한다:

* POSITIVE — 검사기 예외 시 차단되고 기존 파일이 반환된다
* NEGATIVE — **정상 초안은 여전히 통과한다** (항상-차단 아님)
* 경계 — 기존 파일이 없으면 차단하지 않는다 (부트스트랩 보호)
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Namespace

from domain.namespaces import DOMAIN_NS
from tools import multi_agent_tbox as mat

NS = Namespace(str(DOMAIN_NS))


def _tbox(n_ops: int = 6, n_classes: int = 8) -> str:
    """도메인 클래스 n개 + (Ci → Cj) OP n개를 가진 최소 T-Box."""
    g = Graph()
    for i in range(n_classes):
        g.add((NS[f"C{i}"], RDF.type, OWL.Class))
    for i in range(n_ops):
        op = NS[f"op{i}"]
        g.add((op, RDF.type, OWL.ObjectProperty))
        g.add((op, RDFS.domain, NS[f"C{i}"]))
        g.add((op, RDFS.range, NS[f"C{(i + 1) % n_classes}"]))
    return g.serialize(format="turtle")


def _degrade(ttl: str, keep_ops: int) -> str:
    """OP 선언을 AnnotationProperty 로 바꿔 **트리플 수는 유지** 하며 능력만 깎는다.

    트리플 수를 유지하는 것이 핵심이다 — 그러지 않으면 앞단의 붕괴 검사(50% 미만)가
    먼저 차단해서 손실 판정 블록에 도달하지 못하고, 주입한 예외가 관측되지 않는다.
    """
    g = Graph()
    g.parse(data=ttl, format="turtle")
    ops = sorted(set(g.subjects(RDF.type, OWL.ObjectProperty)), key=str)
    for op in ops[keep_ops:]:
        g.remove((op, RDF.type, OWL.ObjectProperty))
        g.add((op, RDF.type, OWL.AnnotationProperty))
    return g.serialize(format="turtle")


@pytest.fixture()
def deployed_tbox(tmp_path, monkeypatch):
    """``TBOX_PATH`` 에 기존 T-Box 가 있는 상태. 경로를 반환한다."""
    path = tmp_path / "t_box.ttl"
    path.write_text(_tbox(), encoding="utf-8")
    monkeypatch.setattr(mat, "TBOX_PATH", str(path))
    # S3 정규화는 무거우니 끈다 — 이 테스트의 관심축이 아니다.
    monkeypatch.setenv("S2_GUARD_NORMALIZE_WITH_S3", "off")
    return path


def _all_live(names):
    """A-Box 가 모든 OP 를 쓴다고 보는 스텁 — live 축을 확실히 발화시킨다."""
    return set(names)


# ── POSITIVE: 검사기 고장이 차단으로 이어진다 ──────────────────────────


def test_checker_exception_blocks_save(deployed_tbox, monkeypatch):
    """THE REGRESSION: 손실 판정이 예외로 죽으면 저장을 거부한다."""
    monkeypatch.setattr("domain.graph_utils.abox_used_local_names", _all_live)
    draft = _degrade(deployed_tbox.read_text(encoding="utf-8"), keep_ops=1)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("injected checker failure")

    monkeypatch.setattr(mat, "_declaration_capabilities", _boom)
    ttl, guard = mat._guard_before_save(draft)

    assert guard["collapse_blocked"] is True, "검사기 고장이 통과로 읽혔다 (fail-open)"
    assert guard["block_reason"] == "check_error"
    assert "RuntimeError" in guard["check_error"]
    assert ttl.strip() != draft.strip(), "차단인데 초안이 반환됐다"


def test_blocked_draft_is_preserved(deployed_tbox, monkeypatch):
    """차단해도 초안은 디스크에 남는다 — 40분 산출물이 사라지면 안 된다."""
    monkeypatch.setattr("domain.graph_utils.abox_used_local_names", _all_live)
    draft = _degrade(deployed_tbox.read_text(encoding="utf-8"), keep_ops=1)
    monkeypatch.setattr(
        mat, "_declaration_capabilities",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    _, guard = mat._guard_before_save(draft)
    assert guard.get("rejected_draft_path"), "차단된 초안 경로가 기록되지 않았다"


def test_existing_tbox_content_is_returned(deployed_tbox, monkeypatch):
    """차단 시 반환값이 **기존 파일 내용** 이어야 한다 (호출자가 그것을 저장한다)."""
    monkeypatch.setattr("domain.graph_utils.abox_used_local_names", _all_live)
    prev = deployed_tbox.read_text(encoding="utf-8")
    draft = _degrade(prev, keep_ops=1)
    monkeypatch.setattr(
        mat, "_declaration_capabilities",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    ttl, _ = mat._guard_before_save(draft)
    g_ret, g_prev = Graph(), Graph()
    g_ret.parse(data=ttl, format="turtle")
    g_prev.parse(data=prev, format="turtle")
    assert len(g_ret) == len(g_prev)


# ── NEGATIVE: 항상-차단이 아니다 ────────────────────────────────────────


def test_healthy_draft_still_passes(deployed_tbox, monkeypatch):
    """정상 초안은 통과한다 — 수정이 게이트를 항상-차단으로 만들지 않았는가.

    이 주장이 없으면 ``_guard_before_save`` 를 ``return prev, {"blocked": True}``
    로 바꿔도 위 세 테스트가 전부 통과한다.
    """
    monkeypatch.setattr("domain.graph_utils.abox_used_local_names", _all_live)
    prev = deployed_tbox.read_text(encoding="utf-8")
    ttl, guard = mat._guard_before_save(prev)   # 같은 내용 = 손실 0
    assert guard["collapse_blocked"] is False, "손실 없는 초안이 차단됐다"
    assert guard.get("block_reason") is None
    assert guard.get("check_error") is None
    assert ttl.strip() == prev.strip()


def test_parse_error_path_still_reports(deployed_tbox, monkeypatch):
    """TTL 자체가 파싱 불가일 때의 별도 분기가 흔적을 남기는가.

    이 분기(:4502)는 손실 판정보다 앞서고 성격이 다르다 — 판정 대상이 없으므로
    fail-closed 로 바꾸지 않았다. 다만 ``parse_error`` 기록은 유지돼야 한다.
    """
    _, guard = mat._guard_before_save("!!! not turtle at all @@@")
    assert guard.get("parse_error"), "파싱 실패가 기록되지 않았다"


# ── 경계: 부트스트랩 보호 ───────────────────────────────────────────────


def test_missing_tbox_does_not_block(tmp_path, monkeypatch):
    """기존 T-Box 가 없으면 차단하지 않는다 — 첫 실행을 막으면 안 된다.

    이 경로는 ``except`` 에 **도달하지 않는다** — ``try`` 첫 줄의
    ``if os.path.exists(TBOX_PATH)`` 가 손실 검사 전체를 건너뛰므로 주입한 예외가
    발생할 기회조차 없다. 따라서 ``check_error`` 도 남지 않아야 한다: 남았다면
    부트스트랩이 차단 경로로 들어갔다는 뜻이다.
    """
    monkeypatch.setattr(mat, "TBOX_PATH", str(tmp_path / "absent.ttl"))
    monkeypatch.setenv("S2_GUARD_NORMALIZE_WITH_S3", "off")
    monkeypatch.setattr("domain.graph_utils.abox_used_local_names", _all_live)
    monkeypatch.setattr(
        mat, "_declaration_capabilities",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    draft = _tbox()
    ttl, guard = mat._guard_before_save(draft)
    assert guard["collapse_blocked"] is False
    assert ttl.strip() == draft.strip()
    assert "check_error" not in guard, (
        "부트스트랩인데 손실 판정 예외 경로로 들어갔다"
    )
    assert "rejected_draft_path" not in guard, (
        "부트스트랩 초안을 '차단된 초안' 으로 보존했다"
    )
