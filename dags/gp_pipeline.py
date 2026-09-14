"""수집과 후속 처리를 두 DAG로 나눈 Airflow 구성.

  gp_ingest     : 매시 실행된다. 수집 간격 정책을 확인해 수집하고,
                  직전 스냅샷과 비교해 변경이 있을 때만 GP_CHANGED Asset 이벤트를 발행한다.
  gp_downstream : 시간 일정 없이 GP_CHANGED 이벤트가 발행될 때만 실행된다.

태스크는 collect/, analyze/ 스크립트를 호출하고, DAG에는 재시도, 동시 실행 제한,
트리거 설정만 둔다. 같은 스크립트를 GitHub Actions에서도 그대로 실행한다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from airflow.sdk import Asset, dag, task

REPO = Path(__file__).resolve().parents[1]

# 후속 DAG의 실행 조건. 스냅샷 수신이 아니라 궤도 요소 변경이 있을 때 발행한다.
GP_CHANGED = Asset("orbit://gp/active/changed")


def run_script(relative: str, *args: str) -> dict:
    """스크립트를 실행하고 출력의 JSON 부분을 반환한다."""
    proc = subprocess.run(
        [sys.executable, str(REPO / relative), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    out = proc.stdout.strip()
    start = out.find("{")
    return json.loads(out[start:]) if start >= 0 else {}


@dag(
    dag_id="gp_ingest",
    # 매시 실행하고, 실제 요청 여부는 수집 스크립트가 이용 정책(2시간 갱신당 1회)을 확인해 정한다.
    schedule="17 * * * *",
    catchup=False,
    # 이전 실행이 끝나지 않았으면 새로 시작하지 않는다. 실행이 겹치면 같은 데이터를 중복 요청하게 된다.
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["ingest", "policy"],
)
def gp_ingest():
    @task(
        # 재시도하지 않는다. 제공처 정책상 200이 아닌 응답을 받으면 요청을 중단해야 한다.
        retries=0,
        # 외부 API 동시 호출 수를 pool로 제한한다.
        pool="external_api",
    )
    def fetch() -> dict:
        return run_script("collect/fetch_gp.py")

    @task
    def validate(fetch_result: dict) -> dict:
        """수집에 성공한 스냅샷의 통계(6자리 번호 객체 수, 경과 시간 분포)를 계산한다."""
        if fetch_result.get("status") != "ok":
            # 수집 실패는 예외로 처리하지 않고 상태로 넘긴다. 후속 처리는 직전 결과를 유지한다.
            return {"skipped": True, "reason": fetch_result.get("status")}
        return run_script("analyze/snapshot_stats.py")

    @task.short_circuit
    def changed(validation: dict) -> bool:
        """변경된 객체가 없으면 이후 태스크를 건너뛴다."""
        if validation.get("skipped"):
            return False
        diff = run_script("analyze/diff_snapshots.py")
        return diff.get("recompute", 0) > 0

    @task(outlets=[GP_CHANGED])
    def announce() -> None:
        """GP_CHANGED Asset 이벤트를 발행해 gp_downstream을 실행한다."""

    gate = changed(validate(fetch()))
    gate >> announce()  # type: ignore[operator]  (런타임에는 XComArg, 태스크 의존성 연결)


@dag(
    dag_id="gp_downstream",
    # 시간 일정 없이 GP_CHANGED 이벤트로만 실행한다.
    schedule=[GP_CHANGED],
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=1),
        "retry_exponential_backoff": True,
    },
    tags=["compute", "incremental"],
)
def gp_downstream():
    @task
    def propagate() -> dict:
        """궤도 전파를 실행한다. 입력이 같으면 결과가 같으므로 재시도해도 안전하다."""
        return run_script("analyze/propagate_bench.py")

    @task
    def screen(propagation: dict) -> dict:
        """변경 객체가 포함된 쌍의 수를 계산한다.

        실제 근접 계산은 하지 않고, 다시 계산할 범위가 얼마나 줄어드는지만 기록한다.
        """
        diff = json.loads((REPO / "data/results/diff_latest.json").read_text(encoding="utf-8"))
        return {
            "pairs_full": diff["pairs_full"],
            "pairs_incremental": diff["pairs_incremental"],
            "saved_pct": diff["pairs_saved_pct"],
            "propagated_states": propagation.get("full_recompute", {}).get("state_vectors"),
        }

    @task
    def publish(screening: dict) -> dict:
        """게시 단계. 버전 교체 방식의 조회 공백은 experiments/publish_gap.py에서 따로 측정했다."""
        return {"published": True, **screening}

    @task
    def observe(published: dict) -> None:
        """실행 결과를 출력하고 대시보드를 다시 생성한다."""
        print(f"재계산 대상 쌍 {published.get('pairs_incremental'):,} / "
              f"전체 {published.get('pairs_full'):,} ({published.get('saved_pct')}% 감소)")
        run_script("site/build_site.py")

    observe(publish(screen(propagate())))


gp_ingest()
gp_downstream()
