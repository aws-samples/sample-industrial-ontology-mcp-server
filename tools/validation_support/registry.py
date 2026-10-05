"""Check registry for validate_kg.

validate_kg의 23개 check를 다루기 위한 경량 레지스트리 + 데이터클래스.

설계 원칙:
- 기존 check 함수는 dict을 반환한다(후방 호환). 여기서는 Check 메타데이터만 다룬다.
- 새 check를 추가하려면 CheckRegistry.register(...) 또는 @check 데코레이터 한 줄만 쓰면 된다.
- validate_kg은 registry.iter_checks()로 순회 → 기존 22개 튜플 나열을 대체.

기존 _check_* wrapper는 모듈 로컬 설정 주입(test patch)을 위해 유지한다.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CheckResult:
    """Check 1건의 실행 결과. 기존 dict 반환과 호환되도록 변환 가능.

    기존 check들은 {"name", "passed", ...violations} 형태 dict을 반환한다.
    CheckResult는 그 구조를 명시화한 값 객체로, 점진 도입을 위해 dict로도 내보낼 수 있다.
    """

    name: str
    passed: bool
    details: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> CheckResult:
        return cls(
            name=d.get("name", "<unnamed>"),
            passed=bool(d.get("passed", False)),
            details={k: v for k, v in d.items() if k not in ("name", "passed")},
        )

    def to_dict(self) -> dict:
        out = {"name": self.name, "passed": self.passed}
        out.update(self.details)
        return out


@dataclass(frozen=True)
class CheckSpec:
    """검증 check 1건의 메타데이터.

    Attributes:
        key: 고유 키 (bidirectional, process_flow 등).
        runner: 실제 실행 callable (validate_kg이 lambda 주입).
        is_heavy: True면 실행 후 gc.collect()로 큰 자료구조 즉시 회수.
        diagnosis_hint: diagnostics에서 이 check의 원인 분류 힌트.
    """

    key: str
    runner: Callable[[], dict]
    is_heavy: bool = False
    diagnosis_hint: str = ""


class CheckRegistry:
    """검증 check 컬렉션을 보관/순회하는 경량 레지스트리.

    validate_kg 내부에서 인스턴스 1개를 만들고 23개 check를 등록한 뒤 순회한다.
    lambda 주입 방식이므로 Check가 필요로 하는 인자(g, tbox, class_tiers, shared 등)는
    클로저로 바인딩된다.
    """

    def __init__(self) -> None:
        self._specs: list[CheckSpec] = []
        self._seen: set[str] = set()

    def register(
        self,
        key: str,
        runner: Callable[[], dict],
        *,
        is_heavy: bool = False,
        diagnosis_hint: str = "",
    ) -> None:
        if key in self._seen:
            raise ValueError(f"duplicate check key: {key}")
        self._seen.add(key)
        self._specs.append(
            CheckSpec(key=key, runner=runner, is_heavy=is_heavy, diagnosis_hint=diagnosis_hint)
        )

    def check(self, key: str, *, is_heavy: bool = False, diagnosis_hint: str = ""):
        """데코레이터 형태의 register. 함수는 0-인자 callable이어야 한다.

        예)
            @registry.check("my_check", is_heavy=True)
            def my_check():
                return {"name": "my_check", "passed": True}
        """
        def _decorator(fn: Callable[[], dict]) -> Callable[[], dict]:
            self.register(key, fn, is_heavy=is_heavy, diagnosis_hint=diagnosis_hint)
            return fn
        return _decorator

    def __iter__(self) -> Iterator[CheckSpec]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def keys(self) -> list[str]:
        return [s.key for s in self._specs]
