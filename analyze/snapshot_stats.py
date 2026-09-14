#!/usr/bin/env python3
"""스냅샷 1개로 낼 수 있는 근거 두 가지.

E6 — 스키마 위험:
  2026-07-11에 TLE 5자리 카탈로그 번호가 소진됐다(CelesTrak).
  100000번 이상 객체는 TLE 형식으로 제공되지 않으므로, TLE만 파싱하는 수집 잡은
  이 객체들을 '오류 없이' 놓친다. 지금 데이터에 몇 개나 있는지 센다.

E1(1차) — 신선도:
  각 객체의 EPOCH(궤도 요소의 기준 시각)가 지금으로부터 얼마나 오래됐는지 분포를 낸다.
  이 분포는 "소스가 얼마나 자주 갱신되는가"의 하한선을 보여준다.
  스냅샷이 여러 개 쌓이면 '주기당 실제 변경 비율'로 정밀화한다(E2).

주의(문서에 반드시 병기할 한계):
  - EPOCH는 '발행 시각'이 아니라 '궤도 상태의 기준 시각'이다.
  - CelesTrak은 스페이스맵이 쓰는 Space-Track의 대체 공개 소스이며 갱신 주기가 다르다.
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
    over_5digit = 0      # TLE로 표현 불가 (E6의 핵심 수치)
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
            "meaning": "TLE 형식만 파싱하는 수집 잡이 조용히 놓치는 객체 수",
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
