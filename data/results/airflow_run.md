# Airflow 실행 기록 (2026-09-13 02:52 UTC)

`airflow dags test gp_ingest` — Airflow 3.3.1, Python 3.12

## 결과

| 태스크 | 결과 | 의미 |
|---|---|---|
| `fetch` | `status: skipped` | 정책 게이트 작동: 마지막 수집 후 2시간이 지나지 않아 **요청 자체를 하지 않음** |
| `validate` | `{'skipped': True, 'reason': 'skipped'}` | 실패를 예외가 아니라 상태로 다룸 |
| `changed` | `False` → `Skipping downstream tasks` | **바뀐 게 없으므로 하류를 깨우지 않음** |
| DagRun | `state=success`, 3.05초 | 아무 일도 안 한 것이 정상 동작 |

## 원본 로그 (발췌)

```
[DAG TEST] end task task_id=validate
Done. Returned value was: {'skipped': True, 'reason': 'skipped'}

[DAG TEST] starting task_id=changed
Done. Returned value was: False
Condition result is False
Skipping downstream tasks
Downstream tasks skipped  tasks_skipped=1

DagRun Finished: dag_id=gp_ingest, run_duration=3.049237, state=success
```

## 이 기록이 증명하는 것

**새 데이터가 없으면 하류가 아예 돌지 않는다.**

시간표 기준으로 도는 구조였다면 같은 상황에서 전체 전파와 스크리닝을 한 번 더 돌렸을 것이다.
"데이터가 트리거한다"는 제안이 문장이 아니라 실행 결과로 확인된 지점이다.

## 설정값과 그 이유

| 설정 | 값 | 이유 |
|---|---|---|
| `retries` (fetch) | **0** | 데이터 제공처가 "200이 아니면 즉시 질의 중단"을 요구. 재시도가 곧 정책 위반이자 IP 차단 위험 |
| `pool` | `external_api` (슬롯 1) | 잡이 늘어도 외부 API 동시 호출 상한은 고정 |
| `max_active_runs` | **1** | 실행 중첩 = 중복 요청 = 차단 위험 |
| `schedule` (하류) | `[Asset]` | 시간표 없음. '변경 있음' 신호가 올 때만 실행 |
| `retries` (하류) | 2 + 지수 백오프 | 내부 계산은 재시도가 안전하다 (멱등 쓰기가 전제) |
