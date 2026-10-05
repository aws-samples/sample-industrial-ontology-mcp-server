"""A-Box generation 보조 구조체 + dataclass — #17.

`tools/abox_generation.py::generate_abox`는 현재 ~460줄의 monolith이다.
이 모듈은 장기 리팩터를 위한 stable dataclass/타입 alias를 정의하고,
향후 점진적으로 blocks를 함수화할 때 closure 변수 대신 사용할 컨테이너를 제공.

현재 `generate_abox` 본체는 loop 시작부에 `AboxBuildContext`를 한 번 생성하고
이후 헬퍼 함수들이 context를 mutate 하도록 바꿀 수 있다.

#17은 이 구조체 + 기존 `_process_csv_row` / `_load_*` 헬퍼 유지로 리팩터
경로를 확정하고, 실제 함수 분해는 후속 세션에서 진행한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AboxBuildContext:
    """A-Box 생성 loop에서 사용하는 누적 상태.

    `generate_abox`의 closure 변수 30여 개를 1개 구조체로 집약.
    R18 에서 주 함수 3단 분해와 함께 실제 사용 시작.
    """

    # 카운터
    individual_count: int = 0
    tables_processed: int = 0
    object_property_count: int = 0
    promoted_fk_count: int = 0
    total_coercion_failures: int = 0
    total_sentinel_filtered: int = 0

    # 실패/이상 목록
    failed_tables: list = field(default_factory=list)
    unverified_fk_targets: dict[str, int] = field(default_factory=dict)
    duplicate_pk_uris: dict[str, int] = field(default_factory=dict)

    # FK 정합성 셀 단위 집계 (key = (source_class, source_column, target_class)).
    # 하나의 FK 값(=셀)이 fwd/inverse/unverified 튜플을 동시에 만들어도 시도/실패를
    # 각각 1회만 센다. referential_integrity = 1 - (실패 셀 / 시도 셀) 의 분모/분자.
    fk_attempt_detail: dict = field(default_factory=dict)
    fk_failure_detail: dict = field(default_factory=dict)

    # 분류
    master_classes: set[str] = field(default_factory=set)
    transaction_classes: set[str] = field(default_factory=set)

    # per-class 누적치
    per_class_instances: dict[str, int] = field(default_factory=dict)
    per_class_op_triples: dict[str, int] = field(default_factory=dict)
    class_table_map: dict[str, str] = field(default_factory=dict)
    per_class_col_coverage: dict[str, dict] = field(default_factory=dict)
    csv_row_counts: dict[str, int] = field(default_factory=dict)

    # Provenance (#9)
    provenance_data: dict[str, dict] = field(default_factory=dict)

    # FK 생성 과정에서 target 에 주입한 rdf:type 트리플 (R15 disjoint 가드)
    fk_added_types: list = field(default_factory=list)
    fk_added_types_seen: set = field(default_factory=set)

    # 모든 테이블의 PK 로 생성될 인스턴스 IRI 문자열 집합 (CSV 루프 전 1-pass 로 선수집).
    # 정방향 FK 가 이 집합에 없는 타겟 IRI 를 만들면 stub 이므로 OP·rdf:type 을 생성하지
    # 않고 unverified 로 카운트한다 (마스터 테이블이 없는 도메인의 stub 방지).
    known_instance_uris: set = field(default_factory=set)

    # 선수집이 **전수 읽기로** 결정한 테이블별 PK — ``{filename: pk_column}``.
    # streaming 경로는 20,000행 표본으로 PK 를 정하는데, 표본과 전수의 판정이
    # 갈리면 발행 IRI 가 위 집합에 하나도 없어 그 테이블로 향하는 FK 가 전부
    # stub 으로 버려진다 (2026-08-09 규명). 선수집의 결정을 재사용해 두 경로가
    # **구조적으로** 일치하게 만든다. 비어 있으면 기존 휴리스틱 폴백.
    precollected_pk_columns: dict = field(default_factory=dict)

    # stub 으로 분류돼 생성을 건너뛴 정방향 FK 트리플 수 (관측/진단용).
    skipped_stub_fk_count: int = 0

    # 차원(Dimension) 처리 통계 {dim_class: {instances, op_triples}} (5c 단계).
    dimension_stats: dict = field(default_factory=dict)

    # 값 커버리지 — 컬럼 데이터가 **실제로 실렸는가** (이름 해석이 아니라 값 기준).
    # ``column_coverage`` 는 컬럼→DP 이름 매칭만 세므로 매칭 후 값이 0건이어도
    # 1.0 이 나온다. 이 축은 각 (class, column) 을 literal / relation / missing
    # 으로 분류해 "OP 로 갔다" 와 "조용히 버려졌다" 를 구분한다
    # (``abox_generation._compute_value_coverage`` 참조).
    value_coverage: dict = field(default_factory=dict)

    # 컬럼별 coercion fail trace (R17)
    coercion_failures_by_column: dict[str, int] = field(default_factory=dict)

    def record_instance(self, class_name: str, count: int = 1) -> None:
        """클래스별 인스턴스 수를 누적. loop 본체가 직접 dict 조작하는 대신 이 메서드."""
        self.per_class_instances[class_name] = (
            self.per_class_instances.get(class_name, 0) + count
        )
        self.individual_count += count

    def record_op(self, class_name: str, count: int = 1) -> None:
        self.per_class_op_triples[class_name] = (
            self.per_class_op_triples.get(class_name, 0) + count
        )
        self.object_property_count += count

    def record_duplicate_pk(self, uri: str) -> None:
        self.duplicate_pk_uris[uri] = self.duplicate_pk_uris.get(uri, 0) + 1

    def record_unverified_fk(self, target_class: str) -> None:
        self.unverified_fk_targets[target_class] = (
            self.unverified_fk_targets.get(target_class, 0) + 1
        )

    def record_fk_attempt(
        self, source_class: str, source_column: str, target_class: str,
    ) -> None:
        """FK 셀 1건 처리 시도를 기록 (fwd/inverse 튜플 수와 무관하게 셀당 1회)."""
        key = (source_class, source_column, target_class)
        self.fk_attempt_detail[key] = self.fk_attempt_detail.get(key, 0) + 1

    def record_fk_failure(
        self, source_class: str, source_column: str, target_class: str,
    ) -> None:
        """FK 셀 1건이 타겟 인스턴스로 해소되지 못함을 기록 (셀당 1회)."""
        key = (source_class, source_column, target_class)
        self.fk_failure_detail[key] = self.fk_failure_detail.get(key, 0) + 1

    def to_summary_dict(self) -> dict:
        """generate_abox 응답의 'statistics' 필드용 요약."""
        return {
            "instances": self.individual_count,
            "tables_processed": self.tables_processed,
            "object_property_triples": self.object_property_count,
            "failed_tables": self.failed_tables,
            "unverified_fk_targets": self.unverified_fk_targets,
            "duplicate_pk_count": sum(
                1 for cnt in self.duplicate_pk_uris.values() if cnt > 1
            ),
            "master_classes": sorted(self.master_classes),
            "transaction_classes": sorted(self.transaction_classes),
            "per_class_instances": self.per_class_instances,
            "per_class_op_triples": self.per_class_op_triples,
            "class_table_map": self.class_table_map,
            "csv_row_counts": self.csv_row_counts,
            "fk_promoted_count": self.promoted_fk_count,
            "total_coercion_failures": self.total_coercion_failures,
        }
