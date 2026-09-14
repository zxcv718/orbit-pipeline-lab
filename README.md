# Orbit Pipeline Lab

공개 궤도 데이터(CelesTrak GP)로 두 가지 파이프라인 실행 방식을 비교한 실험 저장소입니다.

- 정해진 시각에 전체를 다시 계산하는 방식
- 새 데이터가 들어오고 실제로 바뀐 경우에만 변경분을 계산하는 방식

궤도 데이터처럼 주기적으로 갱신되는 대용량 데이터를 처리할 때, 실행 방식이 데이터 신선도, 계산량, 조회 일관성에 주는 영향을 수치로 확인하는 것이 목적입니다.

> 특정 회사의 시스템을 재현한 것이 아닙니다. 입력은 모두 공개 데이터이며, 각 수치에 측정 조건과 한계를 함께 적었습니다.

결과 대시보드: https://zxcv718.github.io/orbit-pipeline-lab (매시 확인하고, 새 데이터가 있을 때 갱신합니다)

## 측정 결과 (2026-09-14 기준)

| 항목 | 결과 | 조건 |
|---|---|---|
| 활성 객체 | 16,563개 | CelesTrak `active` 그룹 |
| TLE 형식으로 받을 수 없는 객체 | 546개 (3.3%) | 2026-07-11 5자리 카탈로그 번호 소진 이후의 6자리 번호 객체 |
| 수신 시점 궤도 요소 경과 시간 | 중앙값 13.1~22.9시간 | 수집 시점 2회, 원천과 재배포 지연 합산 |
| 수집 주기별 변경 비율 | 대량 갱신 주기 평균 83.4%, 나머지 주기 0.11% 이하 | 변경 비교 5주기 |
| 전체 교체 중 조회 공백 | 문서 24만 건 기준 1,372ms, 버전 교체는 0ms | 로컬 MongoDB 8.2 단일 노드, 합성 문서 |
| 궤도 전파 시간 | 16,564개, 7일분, 1시간 간격 0.712초 | Python 3.12, sgp4 2.27 C++ 가속 |
| 예약 실행 | 매시 예약 15회 중 5회 실행, 시작 지연 중앙값 32분 | GitHub Actions 무료 스케줄러 |

최신 값은 대시보드에서 확인할 수 있습니다.

## 수신한 데이터의 경과 시간 구성

| 구간 | 내용 | 파이프라인에서 줄일 수 있는지 |
|---|---|---|
| 결정 → 등재 | 원천의 관측과 궤도 결정 | 불가 |
| 등재 → 수신 | 수집 주기 (하루 1회 수집 시 평균 12시간 추가, 계산값) | 가능 |
| 수신 → 게시 | 처리와 결과 게시 | 가능 |

공개 GP 데이터에는 카탈로그 등재 시각이 없어, 이 저장소의 경과 시간은 앞의 두 구간을 합산한 값입니다.

## 구성

| 파일 | 역할 |
|---|---|
| `collect/fetch_gp.py` | CelesTrak GP 수집. 이용 정책에 맞춰 요청 간격을 제한하고 모든 실행을 `data/manifest.jsonl`에 기록 |
| `analyze/snapshot_stats.py` | 6자리 번호 객체 수, 궤도 요소 경과 시간 분포 |
| `analyze/diff_snapshots.py` | 직전 스냅샷 대비 변경 객체 감지 (궤도 전파 입력 필드 비교, `FINGERPRINT_FIELDS`) |
| `analyze/live_summary.py` | 예약 실행 기록과 주기별 변경 패턴 요약 |
| `analyze/propagate_bench.py` | SGP4 궤도 전파 시간 측정 |
| `experiments/publish_gap.py` | 전체 교체와 버전 교체의 조회 공백 비교 (MongoDB) |
| `experiments/source_failure.py` | 데이터 소스 장애 시 두 방식의 동작 비교 |
| `dags/gp_pipeline.py` | Airflow 3 DAG 구성 |
| `site/build_site.py` | 정적 대시보드 생성 |

## Airflow DAG

`dags/gp_pipeline.py`는 수집과 후속 처리를 두 DAG로 나눕니다.

| DAG | 실행 조건 | 태스크 |
|---|---|---|
| `gp_ingest` | 매시 17분 (`17 * * * *`) | 수집 → 통계 → 변경 확인 → 변경이 있으면 `GP_CHANGED` Asset 이벤트 발행 |
| `gp_downstream` | `GP_CHANGED` Asset 이벤트 | 궤도 전파 → 재계산 대상 쌍 수 계산 → 게시 → 대시보드 갱신 |

- 수집 태스크는 재시도하지 않습니다. 200이 아닌 응답을 받으면 요청을 중단해야 하는 제공처 정책 때문입니다.
- 외부 호출은 `external_api` pool로 동시 실행을 제한하고, `max_active_runs=1`로 실행이 겹치지 않게 합니다.
- 후속 처리 태스크는 지수 백오프로 최대 2회 재시도합니다.
- 변경 확인에서 변경 객체가 없으면 `short_circuit`으로 이후 태스크를 건너뜁니다.
- 후속 DAG의 쌍 수 계산과 게시 단계는 흐름만 연결했습니다. 실제 근접 계산은 하지 않으며, 버전 교체 방식의 조회 공백은 `experiments/publish_gap.py`에서 따로 측정했습니다.

GitHub Actions(`.github/workflows/collect.yml`)는 같은 스크립트를 순서대로 실행해 결과를 커밋하고 대시보드를 배포합니다.

## 데이터 이용 정책

[CelesTrak 이용 정책](https://celestrak.org/usage-policy.php)을 따릅니다.

- GP 데이터는 2시간마다 갱신되므로 갱신 주기당 1회만 내려받습니다. 스크립트가 직전 성공 시각을 확인해 간격을 제한합니다.
- 200이 아닌 응답을 받으면 재시도하지 않고 기록만 남깁니다. 후속 처리는 직전 스냅샷을 사용합니다.
- 전송량을 줄이기 위해 CSV 형식을 사용합니다.
- `celestrak.org` 도메인만 사용합니다.

## 실행 방법

```bash
python3 collect/fetch_gp.py        # 스냅샷 수집 (직전 성공 후 2시간 이내면 건너뜀)
python3 analyze/snapshot_stats.py  # 6자리 번호 객체 수, 경과 시간 분포
python3 analyze/diff_snapshots.py  # 직전 스냅샷 대비 변경 비율
python3 analyze/live_summary.py    # 예약 실행 기록과 변경 패턴 요약
python3 site/build_site.py         # 정적 대시보드 생성
```

궤도 전파 측정에는 `sgp4`와 `numpy`가 필요합니다. Python 3.14에서는 sgp4의 C++ 가속이 꺼진 상태로 설치되어 측정값이 달라지므로 3.12를 사용합니다(로컬 확인: 3.12 `accelerated=True`, 3.14 `False`).

```bash
uv run --no-project --python 3.12 --with sgp4 --with numpy python analyze/propagate_bench.py
uv run --no-project --python 3.12 --with pymongo python experiments/publish_gap.py   # 로컬 MongoDB 필요
uv run --no-project --python 3.12 python experiments/source_failure.py
```

## 한계

- EPOCH는 게시 시각이 아니라 궤도를 결정한 기준 시각입니다.
- CelesTrak은 Space-Track 데이터를 재배포하는 공개 소스이며 갱신 주기가 다릅니다. 이 수치를 다른 제공처나 운영 시스템에 그대로 적용할 수는 없습니다.
- 근접 분석(충돌 위험 계산)은 구현하지 않았습니다. 궤도 전파와 변경 감지까지 다룹니다.
- 조회 공백 실험은 로컬 단일 노드와 합성 문서로 측정했습니다. 절대값보다 두 방식의 차이를 보기 위한 값입니다.
- 원본 스냅샷은 용량 때문에 저장소에 보관하지 않고, 통계와 변경 인덱스 같은 파생 결과만 남깁니다.
- 2026-09-14 00:30 UTC 이전 실행분은 예약 시각을 2시간 단위로 계산하던 오류 때문에 `data/manifest.jsonl`의 `scheduled_at`이 최대 1시간 이르게 기록되었습니다. `analyze/live_summary.py`와 대시보드는 이 필드 대신 실제 시작 시각이 속한 매시 예약 시각을 기준으로 지연을 다시 계산합니다.

## 데이터 출처

- 궤도 데이터: [CelesTrak GP](https://celestrak.org/NORAD/elements/) (Dr. T.S. Kelso)
- 카탈로그 번호 체계: [CelesTrak GP Data Formats](https://celestrak.org/NORAD/documentation/gp-data-formats.php)
- 궤도 전파: [python-sgp4](https://pypi.org/project/sgp4/) (Brandon Rhodes)
