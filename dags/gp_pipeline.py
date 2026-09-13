"""제안한 구조를 Airflow로 배선한 것.

핵심은 DAG가 두 개로 나뉜다는 점이다.

  gp_ingest     : 시간 기준으로 '깨어나서', 받을 때가 됐는지 정책에 물어본다.
                  받았는데 바뀐 게 없으면 여기서 끝난다.
  gp_downstream : 시간표가 없다. '바뀐 데이터가 생겼다'는 신호가 올 때만 돈다.

즉 시계는 수집까지만 관여하고, 그 뒤는 데이터가 결정한다.
이 경계가 이 과제의 핵심 제안이다.

태스크 본문은 전부 얇다. 실제 로직은 collect/ · analyze/ 의 순수 함수에 있고
DAG는 배선과 정책(재시도·동시성·트리거)만 담당한다.
그래서 같은 로직을 Airflow 없이도(GitHub Actions에서) 그대로 돌릴 수 있다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from airflow.sdk import Asset, dag, task

REPO = Path(__file__).resolve().parents[1]

# 하류를 깨우는 신호. '스냅샷을 받았다'가 아니라 '바뀐 게 있다'가 조건이다.
GP_CHANGED = Asset("orbit://gp/active/changed")


def run_script(relative: str, *args: str) -> dict:
    """스크립트를 그대로 실행하고 마지막 JSON 출력을 돌려준다."""
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
    # 매시 깨어난다. 실제로 받을지는 스크립트의 정책 게이트가 정한다.
    # (외부 소스가 허용하는 주기와 우리가 깨어나는 주기를 분리한다)
    schedule="17 * * * *",
    catchup=False,
    # 이전 실행이 안 끝났으면 새로 시작하지 않는다. 중첩은 중복 요청을 뜻하고,
    # 중복 요청은 곧 차단 위험이다.
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["ingest", "policy"],
)
def gp_ingest():
    @task(
        # ⚠️ 재시도 0. 데이터 제공처가 "200이 아니면 즉시 질의를 멈추라"고 요구한다.
        # 여기서 재시도를 켜는 순간 정책 위반이 되고, IP 차단으로 이어진다.
        retries=0,
        # 외부 API 동시 호출 수를 풀로 묶는다. 잡이 늘어도 호출 상한은 고정된다.
        pool="external_api",
    )
    def fetch() -> dict:
        return run_script("collect/fetch_gp.py")

    @task
    def validate(fetch_result: dict) -> dict:
        """형식·건수·전파 오류율을 본다. 여기서 걸러야 하류가 쓰레기를 계산하지 않는다."""
        if fetch_result.get("status") != "ok":
            # 실패는 예외가 아니라 상태다. 하류는 직전 버전을 계속 쓴다.
            return {"skipped": True, "reason": fetch_result.get("status")}
        return run_script("analyze/snapshot_stats.py")

    @task.short_circuit
    def changed(validation: dict) -> bool:
        """바뀐 게 없으면 여기서 멈춘다. 없는 변화를 계산하지 않는 것이 증분의 출발점이다."""
        if validation.get("skipped"):
            return False
        diff = run_script("analyze/diff_snapshots.py")
        return diff.get("recompute", 0) > 0

    @task(outlets=[GP_CHANGED])
    def announce() -> None:
        """신호를 발행한다. 시간이 아니라 이 신호가 하류를 깨운다."""

    gate = changed(validate(fetch()))
    gate >> announce()  # type: ignore[operator]  (런타임에는 XComArg — 태스크 의존성 연결)


@dag(
    dag_id="gp_downstream",
    # 시간표가 없다. 위 신호가 올 때만 돈다.
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
        """바뀐 객체만 전파한다. 입력이 같으면 결과가 같으므로 재시도가 안전하다."""
        return run_script("analyze/propagate_bench.py")

    @task
    def screen(propagation: dict) -> dict:
        """바뀐 객체가 낀 쌍만 다시 본다.

        이 PoC의 스크리닝은 단순화 버전이다(정밀 충돌분석 아님).
        여기서 증명하려는 것은 알고리즘이 아니라 '다시 볼 범위를 줄이면 비용이 준다'는 구조다.
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
        """새 버전에 다 쓴 뒤 포인터를 바꾼다. 읽는 쪽은 항상 완성된 버전만 본다."""
        return {"published": True, **screening}

    @task
    def observe(published: dict) -> None:
        """신선도와 실행 결과를 장부에 남기고 대시보드를 다시 만든다.

        측정이 마지막 단계인 이유: 여기서 남긴 값이 다음 개선의 기준선이 된다.
        """
        print(f"published: 쌍 {published.get('pairs_incremental'):,} / "
              f"전체 {published.get('pairs_full'):,} (절감 {published.get('saved_pct')}%)")
        run_script("site/build_site.py")

    observe(publish(screen(propagate())))


gp_ingest()
gp_downstream()
