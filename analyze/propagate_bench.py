#!/usr/bin/env python3
"""전파 비용 측정 — "전체를 다시 계산한다"가 실제로 얼마인지.

무엇을 재는가:
  1. 전체 카탈로그를 7일치 전파하는 데 걸리는 시간과 만들어지는 상태 벡터의 수
     (샘플 간격 1시간 = 참고 논문에 적힌 방식)
  2. 같은 계산을 '바뀐 객체만' 했을 때의 비용 (증분 처리의 이득)
  3. 샘플 간격을 1분으로 줄였을 때의 외삽값 — 간격이 비용을 지배한다는 것을 보이기 위함

측정 환경을 반드시 함께 기록한다. 같은 sgp4 버전이라도 C++ 가속 여부에 따라 자릿수가 달라진다.

실행:
  uv run --python 3.12 --with sgp4 python analyze/propagate_bench.py
"""

from __future__ import annotations

import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from sgp4 import omm
    from sgp4.api import SGP4_ERRORS, Satrec, SatrecArray, accelerated, jday
except ImportError:
    sys.exit("sgp4가 없습니다. 실행: uv run --python 3.12 --with sgp4 python analyze/propagate_bench.py")

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "results" / "propagate_bench.json"

SGP4_SATNUM_LIMIT = 339999
DAYS = 7
STEP_HOURS = 1  # 참고 논문과 같은 샘플 간격


def load_satellites(path: Path) -> tuple[list[Satrec], int]:
    sats, skipped = [], 0
    with path.open(encoding="utf-8", newline="") as f:
        for fields in omm.parse_csv(f):
            try:
                if int(fields["NORAD_CAT_ID"]) > SGP4_SATNUM_LIMIT:
                    skipped += 1
                    continue
            except (KeyError, ValueError):
                skipped += 1
                continue
            sat = Satrec()
            try:
                omm.initialize(sat, fields)
            except Exception:
                skipped += 1
                continue
            sats.append(sat)
    return sats, skipped


def propagate(sats: list[Satrec], jd: np.ndarray, fr: np.ndarray) -> tuple[float, int, int]:
    """(소요 시간, 상태 벡터 수, 오류 수)"""
    arr = SatrecArray(sats)
    t0 = time.perf_counter()
    e, r, v = arr.sgp4(jd, fr)
    elapsed = time.perf_counter() - t0
    return elapsed, int(e.size), int(np.count_nonzero(e))


def main() -> int:
    snaps = sorted(SNAP_DIR.glob("gp-*.csv"))
    if not snaps:
        sys.exit("스냅샷이 없습니다. 먼저 collect/fetch_gp.py 를 실행하세요.")
    path = snaps[-1]

    sats, skipped = load_satellites(path)
    n = len(sats)

    steps = DAYS * 24 // STEP_HOURS + 1
    now = datetime.now(timezone.utc)
    jd0, fr0 = jday(now.year, now.month, now.day, now.hour, now.minute, now.second)
    offsets = np.arange(steps) * (STEP_HOURS / 24.0)
    jd = np.full(steps, jd0)
    fr = fr0 + offsets

    full_elapsed, full_states, full_errors = propagate(sats, jd, fr)

    # 증분: 직전 변경 감지 결과가 있으면 그 비율을 그대로 쓴다.
    diff_path = ROOT / "data" / "results" / "diff_latest.json"
    share = None
    if diff_path.exists():
        try:
            share = json.loads(diff_path.read_text(encoding="utf-8")).get("recompute_share_pct")
        except Exception:
            share = None
    k = max(1, int(n * (share or 100) / 100))
    incr_elapsed, incr_states, _ = propagate(sats[:k], jd, fr)

    result = {
        "measured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": {
            "python": platform.python_version(),
            "sgp4_accelerated": bool(accelerated),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "input": {
            "snapshot": path.name,
            "satellites": n,
            "skipped": skipped,
            "window_days": DAYS,
            "step_hours": STEP_HOURS,
            "steps": steps,
        },
        "full_recompute": {
            "elapsed_s": round(full_elapsed, 3),
            "state_vectors": full_states,
            "throughput_per_s": round(full_states / full_elapsed),
            "propagation_errors": full_errors,
            "state_memory_MB_if_kept": round(full_states * 6 * 8 / 1024 / 1024, 1),
        },
        "incremental": {
            "recompute_share_pct": share,
            "satellites": k,
            "elapsed_s": round(incr_elapsed, 3),
            "state_vectors": incr_states,
        },
        "extrapolation_1min_step": {
            "steps": DAYS * 24 * 60 + 1,
            "state_vectors": n * (DAYS * 24 * 60 + 1),
            "estimated_s": round(
                (n * (DAYS * 24 * 60 + 1)) / (full_states / full_elapsed), 1
            ),
            "estimated_memory_GB_if_kept": round(
                n * (DAYS * 24 * 60 + 1) * 6 * 8 / 1024**3, 1
            ),
            "note": "전파 자체보다 간격이 비용을 지배한다. 저장까지 하면 메모리가 먼저 터진다.",
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
