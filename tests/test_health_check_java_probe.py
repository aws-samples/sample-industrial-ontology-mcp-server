"""health_check 의 Java 항목이 Pellet 실행 가능성을 실제로 판정하는가.

2026-08-28 실측. ``health_check`` 은 ``os.path.exists(JAVA_EXE)`` 만 보고 ``ok`` 를
보고했다. 그런데 그 시점에 Pellet 을 쓰는 모든 도구가 깨져 있었다:

    validate_owl_realisation  →  success=false
        "UnsupportedClassVersionError: org/apache/jena/riot/lang/LangRDFXML
         has been compiled by a more recent version of the Java Runtime
         (class file version 69.0) ... only recognizes up to 65.0"

원인은 owlready2 0.50 이 CVE-2021-39239 (Jena RDF/XML XXE) 를 막으며 ``LangRDFXML``
3개 클래스를 parse() 가 즉시 return 하는 스텁으로 교체했고, 그 재컴파일을 JDK 25 에서
``--release`` 없이 수행한 것이다. 2013년 Java 6 (major 50) jar 안에 major 69 클래스
3개가 섞였고, Pellet 의 Jena 로더는 **입력과 무관하게** 정적 초기화 시점에 자기 설정
파일(``etc/ont-policy.rdf``)을 RDF/XML 로 읽으므로 그 클래스를 반드시 로드한다.

## 이 테스트가 고정하는 것

게이트의 값은 "초록을 켜는 것" 이 아니라 **파손을 파손으로 부르는 것** 이다. 세 축을
함께 고정한다 — 하나만 주장하면 반대 방향으로 망가져도 통과한다:

* 검출   — 요구 미달 런타임을 ``degraded`` 로 부른다
* 보존   — 충분한 런타임을 ``degraded`` 로 부르지 **않는다** (과잉 경보 방지)
* 진단   — detail 이 "무엇이 왜 안 되는지" 를 담는다

## HermiT 이 신호가 되지 못한다는 점

``_HERMIT_CLASSPATH`` 는 hermit/ 디렉토리와 HermiT.jar 두 항목뿐이고 오염된 jena-arq
가 없다 (Pellet 만 ``os.listdir(pellet/)`` 와일드카드로 21개 jar 를 끌어온다). HermiT.jar
의 class major 최대치는 50 이라 Java 21 에서도 정상 동작한다. 즉 **S4 통과는 Pellet
상태에 대한 정보를 담지 않는다** — 그래서 이 게이트가 따로 필요하다.
"""
from __future__ import annotations

import json

import pytest

from tools.pipeline_state import (
    _check_java,
    _major_to_jdk,
    _required_class_major,
    _runtime_class_major,
    health_check,
)

MODULE = "tools.pipeline_state"


# ── 검출: 요구 미달을 degraded 로 부른다 ────────────────────────────────


def test_runtime_below_requirement_is_degraded(monkeypatch, tmp_path):
    """THE REGRESSION: class 65 런타임 + class 69 요구 → degraded.

    실측 재현: Java 21 (class 65) 에서 validate_owl_realisation 이 success=false
    였는데 health_check 은 ok 였다.
    """
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{MODULE}._runtime_class_major", lambda _p: 65)
    monkeypatch.setattr(f"{MODULE}._required_class_major", lambda: 69)

    result = _check_java(str(java))

    assert result["status"] == "degraded", "파손을 ok 로 보고했다"


def test_degraded_detail_names_the_broken_tools(monkeypatch, tmp_path):
    """detail 이 무엇이 안 되는지 말한다 — 원인 없는 경보는 무시된다."""
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{MODULE}._runtime_class_major", lambda _p: 65)
    monkeypatch.setattr(f"{MODULE}._required_class_major", lambda: 69)

    detail = _check_java(str(java))["detail"]

    assert "Pellet" in detail
    assert "validate_owl_realisation" in detail, "깨진 도구 이름이 없다"
    assert "65" in detail and "69" in detail, "실측·요구 버전이 없다"


def test_degraded_detail_warns_that_s4_is_not_a_signal(monkeypatch, tmp_path):
    """HermiT 통과를 Pellet 정상으로 오독하지 않게 경고한다.

    이 오독이 실제 사고였다 — S4 5종 통과를 보고 Pellet 도 괜찮다고 넘어갔다.
    """
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{MODULE}._runtime_class_major", lambda _p: 65)
    monkeypatch.setattr(f"{MODULE}._required_class_major", lambda: 69)

    detail = _check_java(str(java))["detail"]

    assert "HermiT" in detail and "S4" in detail


# ── 보존: 충분한 런타임을 깎아내리지 않는다 (NEGATIVE 방향) ──────────────


def test_sufficient_runtime_is_ok(monkeypatch, tmp_path):
    """class 69 런타임 + class 69 요구 → ok. 과잉 경보를 막는다."""
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{MODULE}._runtime_class_major", lambda _p: 69)
    monkeypatch.setattr(f"{MODULE}._required_class_major", lambda: 69)

    assert _check_java(str(java))["status"] == "ok"


def test_newer_runtime_is_ok(monkeypatch, tmp_path):
    """요구보다 새로운 런타임도 ok — 상한을 만들면 JDK 26 에서 오발화한다."""
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{MODULE}._runtime_class_major", lambda _p: 70)
    monkeypatch.setattr(f"{MODULE}._required_class_major", lambda: 69)

    assert _check_java(str(java))["status"] == "ok"


def test_unmeasurable_requirement_does_not_degrade(monkeypatch, tmp_path):
    """jar 를 못 읽으면(``required=None``) degraded 로 몰지 않는다.

    측정 실패를 파손으로 부르면 게이트가 무의미해진다 — 이 리포는 "검증기 실패가
    검증 통과로 읽히는" 사고를 겪었고, 그 반대편 실패(측정 불가를 파손으로 단정)도
    같은 종류의 오보다.
    """
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{MODULE}._runtime_class_major", lambda _p: 65)
    monkeypatch.setattr(f"{MODULE}._required_class_major", lambda: None)

    assert _check_java(str(java))["status"] == "ok"


def test_unreadable_runtime_is_unknown_not_ok(monkeypatch, tmp_path):
    """런타임 버전을 못 읽으면 ``unknown`` — 조용히 ok 로 넘기지 않는다."""
    java = tmp_path / "java"
    java.write_text("#!/bin/sh\n")
    monkeypatch.setattr(f"{MODULE}._runtime_class_major", lambda _p: None)

    result = _check_java(str(java))

    assert result["status"] == "unknown"
    assert result["status"] != "ok"


# ── 경로 문제는 기존 동작을 유지한다 ────────────────────────────────────


def test_missing_path_is_not_found(tmp_path):
    result = _check_java(str(tmp_path / "nope"))
    assert result["status"] == "not_found"
    assert "경로 없음" in result["detail"]


def test_empty_config_is_not_configured():
    assert _check_java("")["status"] == "not_configured"


# ── 버전 매핑 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("major", "expected"),
    [(50, "6"), (52, "8"), (65, "21"), (69, "25"), (70, "26")],
)
def test_major_to_jdk_mapping(major, expected):
    """class major → JDK feature. 오프바이원이면 메시지가 사람을 오도한다."""
    assert _major_to_jdk(major) == expected


# ── 실측: 배포 환경에서 실제로 도는가 ──────────────────────────────────


def test_required_major_is_measured_from_real_jars():
    """owlready2 의 실제 jar 에서 요구 버전을 읽어낸다.

    하드코딩하지 않는 이유: 0.49 는 전부 class 50 이었고 0.50 이 3개를 69 로 올렸다.
    저자의 빌드 JDK 를 따라 또 올라갈 수 있으므로 측정해야 자동 대응한다.
    """
    pytest.importorskip("owlready2")

    required = _required_class_major()

    assert required is not None, "jar 스캔이 실패했다"
    assert required >= 50, f"2013년 Jena 기준선(50) 미만: {required}"


@pytest.mark.requires_java
def test_runtime_major_reads_installed_jvm():
    """설치된 JVM 의 java.class.version 을 실제로 읽는다."""
    import shutil

    java = shutil.which("java")
    if not java:
        pytest.skip("java 미설치")

    assert _runtime_class_major(java) is not None


def test_health_check_reports_java_status():
    """배선 확인 — health_check 응답에 Java 항목이 실제로 실린다."""
    entries = [
        c for c in json.loads(health_check()).get("checks", [])
        if "Java" in c.get("component", "")
    ]

    assert len(entries) == 1, f"Java 항목이 {len(entries)}개다"
    assert entries[0]["status"] in {
        "ok", "degraded", "unknown", "not_found", "not_configured",
    }
