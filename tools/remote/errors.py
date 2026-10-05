"""원격 호출 공통 에러 계층.

목적: bare Exception + str(e) 매칭으로 분기하던 패턴을 타입 기반으로 정리한다.
- 호출자는 isinstance로 네트워크/인증/쿼리/부재 를 구분할 수 있다.
- cause를 필드로 보존해 원본 예외 정보 손실 없음.
"""
from __future__ import annotations


class RemoteError(Exception):
    """원격 서비스 호출 실패의 베이스."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.cause = cause

    def __str__(self) -> str:  # pragma: no cover — trivial
        if self.cause is not None:
            return f"{super().__str__()} (cause: {type(self.cause).__name__}: {self.cause})"
        return super().__str__()


class NetworkError(RemoteError):
    """네트워크 레벨 실패(연결 거부, DNS, TLS, 타임아웃 등)."""


class AuthError(RemoteError):
    """인증/인가 실패 (401/403, SigV4 서명 불일치 등)."""


class QueryError(RemoteError):
    """쿼리 자체의 오류 (SPARQL 구문, 타입 불일치 등)."""


class NotFoundError(RemoteError):
    """리소스/엔드포인트 부재 (404, DB 미생성 등)."""


__all__ = [
    "RemoteError",
    "NetworkError",
    "AuthError",
    "QueryError",
    "NotFoundError",
]
