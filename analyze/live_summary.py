#!/usr/bin/env python3
"""누적된 수집 기록에서 두 가지를 요약한다.

1. 예약 실행 기록
   - 매시 예약 횟수 대비 실제 실행 횟수
   - 실행된 경우 예약 시각 대비 시작 지연
   GitHub Actions 무료 스케줄러의 관측값이므로 Airflow 운영 환경에 그대로 적용하지 않는다.

2. 주기별 변경 패턴
   - 대량 갱신이 들어온 주기와 변경이 거의 없는 주기의 비율
   - 변경이 거의 없는 주기를 건너뛰었을 때 줄일 수 있는 계산량

실행: python3 analyze/live_summary.py
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "manifest.jsonl"
DIFFS = ROOT / "data" / "results" / "diff_history.jsonl"
OUT = ROOT / "data" / "results" / "live_summary.json"

CRON_MINUTE = 17          # collect.yml 의 "17 * * * *"
QUIET_MAX_PCT = 1.0       # 변경률이 이 값 이하인 주기를 변경이 거의 없는 주기로 분류
BURST_MIN_PCT = 50.0      # 이 이상 바뀐 주기 = 대량 갱신 주기
FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse(ts: str) -> datetime:
    return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc)


def slot_for(actual: datetime) -> datetime:
    """실제 시작 시각이 속한 매시 예약 슬롯.

    한계: 지연이 1시간을 넘으면 다음 슬롯의 실행으로 오인될 수 있다.
    그래서 지연은 '최소 지연'으로 해석해야 한다.
    """
    slot = actual.replace(minute=CRON_MINUTE, second=0, microsecond=0)
    if slot > actual:
        slot -= timedelta(hours=1)
    return slot


def schedule_reliability(rows: list[dict]) -> dict:
    ci = [r for r in rows if r.get("scheduled_at")]  # 로컬 실행은 제외
    if len(ci) < 2:
        return {"note": "스케줄 실행 기록이 2개 미만"}

    actual = sorted(parse(r["fetched_at"]) for r in ci)
    slots = [slot_for(a) for a in actual]
    delays_min = [(a - s).total_seconds() / 60 for a, s in zip(actual, slots)]

    first, last = slots[0], slots[-1]
    expected = int((last - first).total_seconds() // 3600) + 1
    ran = len(set(slots))

    return {
        "window": {"from": first.strftime(FMT), "to": last.strftime(FMT)},
        "expected_hourly_slots": expected,
        "actual_runs": ran,
        "execution_rate_pct": round(100.0 * ran / expected, 1),
        "missing_slots": expected - ran,
        "start_delay_minutes": {
            "min": round(min(delays_min), 1),
            "median": round(statistics.median(delays_min), 1),
            "mean": round(statistics.fmean(delays_min), 1),
            "max": round(max(delays_min), 1),
        },
        "caveat": "GitHub Actions 무료 스케줄러 관측값. 운영 스케줄러와 동일하다고 주장하지 않음. "
        "지연이 1시간을 넘으면 슬롯 귀속이 모호하므로 지연은 최솟값으로 해석.",
    }


def change_pattern(diffs: list[dict]) -> dict:
    cycles = [d for d in diffs if d.get("had_previous_index")]
    if not cycles:
        return {"note": "비교 가능한 주기가 아직 없음"}

    rows = []
    prev_time = None
    for d in diffs:
        t = parse(d["measured_at"])
        if d.get("had_previous_index") and prev_time:
            rows.append(
                {
                    "at": d["measured_at"],
                    "hours_since_previous": round((t - prev_time).total_seconds() / 3600, 1),
                    "recompute_pct": d["recompute_share_pct"],
                    "pairs_saved_pct": d["pairs_saved_pct"],
                }
            )
        prev_time = t

    quiet = [r for r in rows if r["recompute_pct"] <= QUIET_MAX_PCT]
    burst = [r for r in rows if r["recompute_pct"] >= BURST_MIN_PCT]

    return {
        "cycles": rows,
        "quiet_cycles": len(quiet),
        "burst_cycles": len(burst),
        "other_cycles": len(rows) - len(quiet) - len(burst),
        "quiet_share_pct": round(100.0 * len(quiet) / len(rows), 1),
        "pairs_saved_in_quiet_pct": round(statistics.fmean(r["pairs_saved_pct"] for r in quiet), 2) if quiet else None,
        "recompute_in_burst_pct": round(statistics.fmean(r["recompute_pct"] for r in burst), 1) if burst else None,
        "reading": "변경은 대량 갱신 주기와 변경이 거의 없는 주기로 나뉜다. "
        "증분 처리의 이득은 대부분 변경이 없는 주기를 건너뛰는 데서 나온다.",
    }


def main() -> int:
    rows = [json.loads(l) for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    diffs = [json.loads(l) for l in DIFFS.read_text(encoding="utf-8").splitlines() if l.strip()]

    result = {
        "measured_at": datetime.now(timezone.utc).strftime(FMT),
        "schedule_reliability": schedule_reliability(rows),
        "change_pattern": change_pattern(diffs),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
