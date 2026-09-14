#!/usr/bin/env python3
"""실제로 돌려서 쌓인 기록에서 두 가지를 읽는다.

1. 스케줄 신뢰성 — 시간 기반 트리거는 예약대로 실행되는가
   - 기대한 실행 횟수(매시 슬롯 수) 대비 실제 실행 횟수
   - 실행된 것의 예약 시각 대비 지연
   주의: GitHub Actions 무료 스케줄러의 값이다. 운영 Airflow와 같다고 주장하지 않는다.
   말하려는 것은 "예약 시각 = 실행 시각"이라는 가정이 깨질 수 있다는 사실 하나다.

2. 변경 패턴 — 주기마다 실제로 얼마나 바뀌는가
   - 소스가 대량 갱신한 주기와 조용한 주기로 갈리는지
   - 조용한 주기를 건너뛰면 얼마나 아끼는지

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
QUIET_MAX_PCT = 1.0       # 이 이하로 바뀐 주기 = 조용한 주기
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
        "reading": "변경은 연속적이지 않고 두 갈래로 갈린다. 증분의 이득은 대부분 "
        "'바뀐 일부만 계산'이 아니라 '안 바뀐 주기를 통째로 건너뛰기'에서 나온다.",
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
