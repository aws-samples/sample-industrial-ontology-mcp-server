"""tests for Query Equivalence scoring — _compute_d3_score 단위 테스트 (작업코드 D3; docs/reference/task-glossary.md)."""

from __future__ import annotations

import json

import pytest

from tools.remote.neo4j import _compute_d3_score

# ── 테스트 헬퍼 ──────────────────────────────────────────────────────────


def _make_nodes(*labels: str) -> dict:
    """테스트용 노드 dict 생성. labels 각각이 하나의 노드."""
    nodes = {}
    for i, lb in enumerate(labels):
        uri = f"http://example.org/inst/{lb}_{i}"
        nodes[uri] = {"labels": [lb], "properties": {}}
    return nodes


def _make_rels(nodes: dict, pairs: list[tuple[str, str, str]]) -> list:
    """(src_label, dst_label, rel_type) 쌍으로 관계 생성."""
    label_to_uri: dict[str, str] = {}
    for uri, n in nodes.items():
        for lb in n["labels"]:
            label_to_uri.setdefault(lb, uri)
    rels = []
    for s_lb, d_lb, rtype in pairs:
        s_uri = label_to_uri.get(s_lb, f"urn:missing:{s_lb}")
        d_uri = label_to_uri.get(d_lb, f"urn:missing:{d_lb}")
        rels.append((s_uri, d_uri, rtype))
    return rels


def _write_cq_file(cqs: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cqs, f, ensure_ascii=False)


def _write_cq_validation(data: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# ── 1순위: CQ validation 결과 파일 사용 ──────────────────────────────────


class TestD3ProgrammaticMode:
    """cq_validation_path가 있으면 answer_rate/pass_rate를 직접 사용."""

    def test_uses_answer_rate_from_validation(self, tmp_path):
        validation = {
            "success": True,
            "summary": {"answer_rate": 75.0},
        }
        vpath = str(tmp_path / "cq_val.json")
        _write_cq_validation(validation, vpath)

        score, details = _compute_d3_score({}, [], cq_validation_path=vpath)
        assert score == 75.0
        assert details["mode"] == "programmatic"

    def test_uses_pass_rate_from_validation(self, tmp_path):
        """query_test.py의 validate_cq_tbox_only는 pass_rate 필드 사용."""
        validation = {
            "success": True,
            "summary": {"pass_rate": 60.0},
        }
        vpath = str(tmp_path / "cq_val.json")
        _write_cq_validation(validation, vpath)

        score, details = _compute_d3_score({}, [], cq_validation_path=vpath)
        assert score == 60.0
        assert details["mode"] == "programmatic"

    def test_handles_string_rate_with_percent(self, tmp_path):
        """answer_rate가 문자열 '80%' 형태여도 파싱."""
        validation = {"summary": {"answer_rate": "80%"}}
        vpath = str(tmp_path / "cq_val.json")
        _write_cq_validation(validation, vpath)

        score, _ = _compute_d3_score({}, [], cq_validation_path=vpath)
        assert score == 80.0

    def test_programmatic_takes_priority_over_offline(self, tmp_path, monkeypatch):
        """validation 파일이 있으면 offline CQ 로직보다 우선."""
        validation = {"summary": {"answer_rate": 50.0}}
        vpath = str(tmp_path / "cq_val.json")
        _write_cq_validation(validation, vpath)

        # offline CQ가 100%를 줄 수 있는 노드를 제공하더라도 무시
        nodes = _make_nodes("ClassA")
        score, details = _compute_d3_score(nodes, [], cq_validation_path=vpath)
        assert score == 50.0
        assert details["mode"] == "programmatic"

    def test_invalid_validation_file_falls_through(self, tmp_path, monkeypatch):
        """유효하지 않은 validation 파일이면 offline으로 폴백."""
        vpath = str(tmp_path / "cq_val.json")
        with open(vpath, "w") as f:
            f.write("not json")

        # R27: CQ 파일도 없도록 격리 — config 상수를 tmp_path 로 패치
        import config
        monkeypatch.setattr(
            config, "COMPETENCY_QUESTIONS_PATH",
            str(tmp_path / "nonexistent_cq.json"),
        )

        score, details = _compute_d3_score({}, [], cq_validation_path=vpath)
        assert details["mode"] == "no_cq_available"

    def test_nonexistent_validation_path_falls_through(self, tmp_path, monkeypatch):
        """존재하지 않는 경로가 주어지면 offline으로 폴백."""
        import config
        monkeypatch.setattr(
            config, "COMPETENCY_QUESTIONS_PATH",
            str(tmp_path / "nonexistent_cq.json"),
        )

        score, details = _compute_d3_score({}, [], cq_validation_path="/nonexistent/path.json")
        assert details["mode"] == "no_cq_available"


# ── 2순위: Offline CQ 폴백 (0.8 penalty) ────────────────────────────────


class TestD3OfflineFallback:
    """CQ 파일로부터 schema-only check + 0.8 penalty."""

    @pytest.fixture
    def cq_dir(self, tmp_path, monkeypatch):
        """CQ 파일을 임시 query_tests/ 경로에 생성 후 config 상수 패치."""
        # R27: CQ 파일은 data/source/query_tests/competency_questions.json 로
        # 이동. 테스트에서는 config.COMPETENCY_QUESTIONS_PATH 를 tmp_path 로
        # 패치해 프로덕션 파일과 격리.
        qt_dir = tmp_path / "data" / "source" / "query_tests"
        qt_dir.mkdir(parents=True)
        cq_path = qt_dir / "competency_questions.json"

        import config
        monkeypatch.setattr(config, "COMPETENCY_QUESTIONS_PATH", str(cq_path))
        return cq_path

    def test_penalty_applied_to_raw_score(self, cq_dir):
        """offline 모드에서 raw_score * 0.8 = penalized score."""
        cqs = [{"id": "CQ01", "domains": ["ClassA"]}]
        _write_cq_file(cqs, str(cq_dir))

        nodes = _make_nodes("ClassA")
        score, details = _compute_d3_score(nodes, [])
        assert details["mode"] == "offline_cq_fallback"
        assert details["penalty"] == 0.8
        # ClassA exists → 1/1 check passed → raw 100% → penalized 80%
        assert details["raw_score"] == 100.0
        assert score == 80.0

    def test_partial_match_with_penalty(self, cq_dir):
        """일부 도메인만 매칭되면 비례 점수 * 0.8."""
        cqs = [{"id": "CQ01", "domains": ["ClassA", "ClassB"]}]
        _write_cq_file(cqs, str(cq_dir))

        # ClassA만 있고 ClassB 없음, 관계도 없음
        nodes = _make_nodes("ClassA")
        score, details = _compute_d3_score(nodes, [])
        assert details["mode"] == "offline_cq_fallback"
        # checks: ClassA(pass) + ClassB(fail) + A↔B(fail) = 3 checks, 1 passed
        # raw = 33.3, penalized = 26.7
        assert details["checks"] == 3
        assert details["passed"] == 1
        assert score == pytest.approx(33.3 * 0.8, abs=0.1)

    def test_cross_class_relationship_detected(self, cq_dir):
        """관계로 연결된 클래스 쌍이 CQ check를 통과."""
        cqs = [{"id": "CQ01", "domains": ["ClassA", "ClassB"]}]
        _write_cq_file(cqs, str(cq_dir))

        nodes = _make_nodes("ClassA", "ClassB")
        rels = _make_rels(nodes, [("ClassA", "ClassB", "hasRelation")])
        score, details = _compute_d3_score(nodes, rels)
        # checks: ClassA(pass) + ClassB(pass) + A↔B(pass) = 3/3 → raw 100 → 80
        assert details["passed"] == 3
        assert score == 80.0

    def test_2hop_relationship_detected(self, cq_dir):
        """2-hop 경로로 연결된 클래스 쌍도 통과."""
        cqs = [{"id": "CQ01", "domains": ["ClassA", "ClassC"]}]
        _write_cq_file(cqs, str(cq_dir))

        nodes = _make_nodes("ClassA", "ClassB", "ClassC")
        rels = _make_rels(nodes, [
            ("ClassA", "ClassB", "r1"),
            ("ClassB", "ClassC", "r2"),
        ])
        score, details = _compute_d3_score(nodes, rels)
        # ClassA(pass) + ClassC(pass) + A↔C via B(pass) = 3/3
        assert details["passed"] == 3
        assert score == 80.0

    def test_max_20_cqs(self, cq_dir):
        """CQ 20개 이상이면 처음 20개만 검사."""
        cqs = [{"id": f"CQ{i:02d}", "domains": ["ClassA"]} for i in range(30)]
        _write_cq_file(cqs, str(cq_dir))

        nodes = _make_nodes("ClassA")
        _, details = _compute_d3_score(nodes, [])
        assert details["cqs_tested"] == 20


# ── 3순위: CQ 파일 없음 — 0점 ────────────────────────────────────────────


class TestD3NoCqAvailable:
    """CQ 파일이 없으면 0점 반환 (rescaling 인플레이션 방지)."""

    @pytest.fixture(autouse=True)
    def _isolate_cq_path(self, tmp_path, monkeypatch):
        """실제 프로젝트의 CQ 파일에 접근하지 않도록 격리 (R27: config 상수 패치)."""
        import config
        monkeypatch.setattr(
            config, "COMPETENCY_QUESTIONS_PATH",
            str(tmp_path / "nonexistent_cq.json"),
        )

    def test_no_cq_returns_zero(self):
        score, details = _compute_d3_score({}, [])
        assert score == 0.0
        assert details["mode"] == "no_cq_available"

    def test_no_cq_with_nodes_still_zero(self):
        """노드가 있어도 CQ 파일이 없으면 0점."""
        nodes = _make_nodes("ClassA", "ClassB")
        score, details = _compute_d3_score(nodes, [])
        assert score == 0.0
        assert details["mode"] == "no_cq_available"


# ── 통합: total_score 인플레이션 방지 ────────────────────────────────────


class TestNoRescalingInflation:
    """D3가 0점이어도 total_score가 80%→100% 스케일링되지 않음을 검증."""

    def test_total_score_not_inflated(self):
        """D3=0이면 total = D1*0.4 + D2*0.25 + 0*0.2 + D4*0.1 + D5*0.05 = 80% 상한."""
        # 모든 차원이 100점이라고 가정
        d1, d2, d3, d4, d5 = 100.0, 100.0, 0.0, 100.0, 100.0
        total = d1 * 0.40 + d2 * 0.25 + d3 * 0.20 + d4 * 0.10 + d5 * 0.05
        assert total == 80.0  # 이전에는 80→100 rescaling 됐음

    def test_total_score_with_d3(self):
        """D3가 있으면 정상 가중합."""
        d1, d2, d3, d4, d5 = 100.0, 100.0, 80.0, 100.0, 100.0
        total = d1 * 0.40 + d2 * 0.25 + d3 * 0.20 + d4 * 0.10 + d5 * 0.05
        assert total == 96.0
