# Airflow 실행 기록 (2026-09-13 02:52 UTC)

`airflow dags test gp_ingest`, Airflow 3.3.1, Python 3.12

## 결과

| 태스크 | 결과 | 설명 |
|---|---|---|
| `fetch` | `status: skipped` | 직전 수집 후 2시간이 지나지 않아 요청하지 않았습니다 |
| `validate` | `{'skipped': True, 'reason': 'skipped'}` | 수집하지 않은 경우를 예외가 아닌 상태 값으로 전달합니다 |
| `changed` | `False`, `Skipping downstream tasks` | 변경이 없어 이후 태스크(`announce`)를 건너뛰었습니다 |
| DagRun | `state=success`, 3.05초 | 수집할 데이터가 없을 때의 정상 종료입니다 |

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

## 확인한 내용

새 데이터가 없는 실행에서는 `GP_CHANGED` 이벤트가 발행되지 않아 `gp_downstream`이 실행되지 않았습니다.
정해진 시각에 전체를 처리하는 구성이었다면 같은 상황에서도 궤도 전파와 후속 계산이 다시 실행됩니다.

## 설정값

| 설정 | 값 | 이유 |
|---|---|---|
| `retries` (`fetch`) | 0 | 제공처 정책상 200이 아닌 응답을 받으면 요청을 중단해야 합니다. 재시도하면 정책 위반과 접속 차단 위험이 있습니다 |
| `pool` | `external_api` (슬롯 1) | 태스크가 늘어도 외부 API 동시 호출 수를 1로 제한합니다 |
| `max_active_runs` | 1 | 실행이 겹치면 같은 데이터를 중복 요청하게 됩니다 |
| `schedule` (`gp_downstream`) | `[GP_CHANGED]` | 시간 일정 없이 변경 이벤트가 발행될 때만 실행합니다 |
| `retries` (`gp_downstream`) | 2, 지수 백오프 | 내부 계산은 입력이 같으면 결과가 같아 재시도해도 안전합니다 |
