#!/usr/bin/env python3
"""최신 스냅샷 1개로 두 가지 통계를 계산한다.

1. 6자리 카탈로그 번호 객체 수
   2026-07-11에 TLE 5자리 카탈로그 번호가 소진되었고(CelesTrak), 100000번 이상 객체는
   TLE 형식으로 제공되지 않는다. TLE만 파싱하는 수집기는 이 객체를 오류 없이 누락하므로 개수를 센다.

2. 궤도 요소 경과 시간 분포
   각 객체의 EPOCH부터 측정 시점까지 지난 시간의 분포를 계산한다.
   공개 GP 데이터에는 등재 시각이 없어 원천 지연과 재배포 지연이 합쳐진 값이다.

한계:
  - EPOCH는 게시 시각이 아니라 궤도를 결정한 기준 시각이다.
  - CelesTrak은 Space-Track 데이터를 재배포하는 공개 소스이며 갱신 주기가 다르다.
"""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / "data" / "snapshots"
OUT_DIR = ROOT / "data" / "results"

# sgp4 라이브러리는 위성번호 339999를 넘으면 ValueError를 낸다(실측).
SGP4_SATNUM_LIMIT = 339999
# TLE 5자리로 표현 가능한 최대치.
TLE_5DIGIT_LIMIT = 99999


def parse_epoch(value: str) -> datetime | None:
    """OMM EPOCH 예: 2026-09-13T04:12:31.123456"""
    v = value.strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def latest_snapshot() -> Path:
    snaps = sorted(SNAP_DIR.glob("gp-*.csv"))
    if not snaps:
        sys.exit("스냅샷이 없습니다. 먼저 poc/collect/fetch_gp.py 를 실행하세요.")
    return snaps[-1]


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_snapshot()
    now = datetime.now(timezone.utc)

    ages_h: list[float] = []
    total = 0
    over_5digit = 0      # TLE 5자리로 표현할 수 없는 객체 수
    over_sgp4_limit = 0  # sgp4 라이브러리도 거부
    no_epoch = 0

    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            total += 1
            try:
                satnum = int(row["NORAD_CAT_ID"])
            except (KeyError, ValueError):
                satnum = -1
            if satnum > TLE_5DIGIT_LIMIT:
                over_5digit += 1
            if satnum > SGP4_SATNUM_LIMIT:
                over_sgp4_limit += 1

            epoch = parse_epoch(row.get("EPOCH", ""))
            if epoch is None:
                no_epoch += 1
                continue
            ages_h.append((now - epoch).total_seconds() / 3600.0)

    ages_h.sort()

    def pct(p: float) -> float:
        if not ages_h:
            return float("nan")
        idx = min(int(len(ages_h) * p), len(ages_h) - 1)
        return round(ages_h[idx], 2)

    def share_under(hours: float) -> float:
        if not ages_h:
            return float("nan")
        n = sum(1 for a in ages_h if a <= hours)
        return round(100.0 * n / len(ages_h), 2)

    result = {
        "snapshot": str(path.relative_to(ROOT)),
        "measured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "objects_total": total,
        "e6_schema_risk": {
            "norad_over_99999": over_5digit,
            "share_pct": round(100.0 * over_5digit / total, 3) if total else 0,
            "norad_over_sgp4_limit_339999": over_sgp4_limit,
            "meaning": "TLE 형식만 파싱하는 수집기가 오류 없이 누락하는 객체 수",
        },
        "e1_epoch_age_hours": {
            "count": len(ages_h),
            "no_epoch": no_epoch,
            "p50": pct(0.50),
            "p75": pct(0.75),
            "p90": pct(0.90),
            "p95": pct(0.95),
            "max": round(ages_h[-1], 2) if ages_h else None,
            "mean": round(statistics.fmean(ages_h), 2) if ages_h else None,
            "share_under_2h_pct": share_under(2),
            "share_under_6h_pct": share_under(6),
            "share_under_12h_pct": share_under(12),
            "share_under_24h_pct": share_under(24),
            "share_under_48h_pct": share_under(48),
        },
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "snapshot_stats.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 최신값만 남기면 측정 시점에 따른 변동이 사라진다(실제로 13~23시간을 오갔다).
    # 그래서 매 실행을 이력으로도 쌓는다. 같은 스냅샷을 두 번 재면 한 번만 남긴다.
    history = OUT_DIR / "stats_history.jsonl"
    seen = set()
    if history.exists():
        seen = {json.loads(l).get("snapshot") for l in history.read_text(encoding="utf-8").splitlines() if l.strip()}
    if path.name not in seen:
        compact = {k: result[k] for k in ("measured_at", "objects_total", "e6_schema_risk", "e1_epoch_age_hours")}
        with history.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"snapshot": path.name, **compact}, ensure_ascii=False) + "\n")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n저장: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
