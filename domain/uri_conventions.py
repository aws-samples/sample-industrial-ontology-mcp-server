"""URI 생성 규칙.

이 프로젝트의 URI 컨벤션:
- 클래스: steel:{PascalCase}           예) steel:EquipmentMaster
- ObjectProperty: steel:{camelCase}    예) steel:hasEquipmentStatus
- DatatypeProperty: steel:{camelCase}  예) steel:equipmentID
- 마스터 인스턴스: steel-inst:{ClassName}_{PK}
- 트랜잭션 인스턴스: steel-inst:{ClassName}_{PK}_{YYYYMMDDHHMMSS}
- 관계 인스턴스: steel-inst:{ClassName}_{PK1}_{PK2}

URI 타입 별칭(ClassURI/PropertyURI/InstanceURI)은 정적 타입 검사 힌트 용도.
런타임에는 str과 동일하므로 기존 코드 수정 없이 점진 도입 가능.
"""

import urllib.parse
from typing import NewType

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS

# 타입 검사용 NewType — 런타임에는 str과 동일
ClassURI = NewType("ClassURI", str)
PropertyURI = NewType("PropertyURI", str)
InstanceURI = NewType("InstanceURI", str)


_PK_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"


# Python/RDF/owlready2 예약어와 충돌하는 local name.
# 이 이름으로 Class / Property 를 만들면 owlready2 + HermiT 가 Python 내장 type
# 과 섞이면서 "type.__bases__ must be tuple of classes, not 'Restriction'" 같은
# 이해하기 어려운 TypeError 를 낸다. 생성 단계에서 차단해 근본 원인 제거.
# reserved 발견 시 더 도메인-특화된 이름 (e.g. alarmType, equipmentLocation,
# alarmSeverity) 사용 권장.
RESERVED_LOCAL_NAMES: frozenset[str] = frozenset({
    "type", "class", "object", "subject", "predicate",
    "description", "location", "severity",
})


def is_reserved_local(name: str) -> bool:
    """local name 이 owlready2/Python 충돌 예약어인지 검사.

    prefixed name("steel:type") 과 절대 URI 모두 허용. 비교는 case-insensitive.
    """
    if not name:
        return False
    local = name.rsplit(":", 1)[-1].rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return local.lower() in RESERVED_LOCAL_NAMES


def class_uri(class_name: str) -> ClassURI:
    """클래스 URI 생성. PascalCase 강제."""
    if not class_name:
        raise ValueError("class_name cannot be empty")
    name = class_name[0].upper() + class_name[1:]
    return ClassURI(f"{DOMAIN_NS}{name}")


def property_uri(prop_name: str) -> PropertyURI:
    """프로퍼티 URI 생성. camelCase 강제."""
    if not prop_name:
        raise ValueError("prop_name cannot be empty")
    name = prop_name[0].lower() + prop_name[1:]
    return PropertyURI(f"{DOMAIN_NS}{name}")


def instance_uri(class_name: str, primary_key: str) -> InstanceURI:
    """마스터 인스턴스 URI 생성. 유니코드 PK는 percent-encoding으로 보존."""
    pk_str = str(primary_key)
    # ASCII-safe 문자는 그대로, 유니코드는 percent-encoding
    safe_pk = urllib.parse.quote(pk_str, safe=_PK_SAFE)
    return InstanceURI(f"{DOMAIN_INST_NS}{class_name}_{safe_pk}")


def transaction_instance_uri(class_name: str, primary_key: str, timestamp: str) -> InstanceURI:
    """트랜잭션 인스턴스 URI 생성. timestamp는 YYYYMMDDHHMMSS 형식."""
    pk_str = str(primary_key)
    safe_pk = urllib.parse.quote(pk_str, safe=_PK_SAFE)
    return InstanceURI(f"{DOMAIN_INST_NS}{class_name}_{safe_pk}_{timestamp}")


def local_name(uri: str) -> str:
    """URI에서 로컬 이름(# 또는 / 뒤)을 추출한다.

    `#` 우선 (RDF/XML namespace convention), 없으면 마지막 `/` 뒤. 둘 다 없는
    URI(예: ``urn:foo:bar``) 는 입력 그대로 반환 — 호출자가 별도로 처리.
    """
    if "#" in uri:
        return uri.rsplit("#", 1)[1]
    if "/" in uri:
        return uri.rsplit("/", 1)[1]
    return uri


__all__ = [
    "ClassURI",
    "PropertyURI",
    "InstanceURI",
    "class_uri",
    "property_uri",
    "instance_uri",
    "transaction_instance_uri",
    "local_name",
    "is_reserved_local",
    "RESERVED_LOCAL_NAMES",
]
