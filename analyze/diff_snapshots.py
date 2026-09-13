#!/usr/bin/env python3
"""변경 감지 — "무엇이 바뀌면 다시 계산해야 하는가"를 정하는 단계.

이 질문의 답이 증분 처리의 전부다.
너무 좁게 잡으면 바뀐 걸 놓치고(정확도 사고), 너무 넓게 잡으면 전부 다시 계산하게 된다(절감 0).

여기서는 직전 실행의 인덱스(객체번호 → 지문)와 현재 스냅샷을 비교해
  - 신규(new): 이전에 없던 객체
  - 변경(changed): 지문이 달라진 객체
  - 유지(unchanged): 그대로인 객체
  - 소멸(gone): 이번 스냅샷에 없는 객체
를 센다. 이 비율이 곧 "증분 처리로 아낄 수 있는 계산량"의 근거가 된다.
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / "data" / "snapshots"
STATE = ROOT / "data" / "state" / "epoch_index.json.gz"
OUT_DIR = ROOT / "data" / "results"
HISTORY = OUT_DIR / "diff_history.jsonl"

# ---------------------------------------------------------------------------
# 판단 지점 ①: 무엇을 '바뀐 객체'로 볼 것인가
#
# 후보 세 가지:
#   (a) EPOCH만 본다        — 새 궤도 해가 나온 것만 변경으로 친다. 가장 좁고 싸다.
#   (b) 궤도 6요소 + BSTAR  — 값이 실제로 달라진 것만. EPOCH만 갱신되고 값이 같은 경우를 걸러낸다.
#   (c) 전체 레코드 해시    — 무엇이든 달라지면 변경. 가장 안전하지만 잡음(메타데이터 변경)에 민감하다.
#
# 현재 기본값은 (b)다. 근거: 하류 계산(SGP4 전파)의 입력이 정확히 이 필드들이므로,
# 이 값들이 같으면 다시 계산해도 결과가 같다 = 다시 계산할 이유가 없다.
# 바꾸고 싶으면 FINGERPRINT_FIELDS를 수정하면 된다.
# ---------------------------------------------------------------------------
FINGERPRINT_FIELDS = [
    "EPOCH",
    "MEAN_MOTION",
    "ECCENTRICITY",
    "INCLINATION",
    "RA_OF_ASC_NODE",
    "ARG_OF_PERICENTER",
    "MEAN_ANOMALY",
    "BSTAR",
]


def fingerprint(row: dict) -> str:
    """하류 계산의 입력만으로 만든 지문."""
    return "|".join(row.get(f, "") for f in FINGERPRINT_FIELDS)


def load_index() -> dict[str, str]:
    if not STATE.exists():
        return {}
    with gzip.open(STATE, "rt", encoding="utf-8") as f:
        return json.load(f)


def save_index(index: dict[str, str]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(STATE, "wt", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))


def latest_snapshot() -> Path:
    snaps = sorted(SNAP_DIR.glob("gp-*.csv"))
    if not snaps:
        sys.exit("스냅샷이 없습니다. 먼저 collect/fetch_gp.py 를 실행하세요.")
    return snaps[-1]


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_snapshot()
    now = datetime.now(timezone.utc)

    prev = load_index()
    curr: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            curr[row["NORAD_CAT_ID"]] = fingerprint(row)

    new = [k for k in curr if k not in prev]
    changed = [k for k, v in curr.items() if k in prev and prev[k] != v]
    unchanged = [k for k, v in curr.items() if k in prev and prev[k] == v]
    gone = [k for k in prev if k not in curr]

    n = len(curr)
    recompute = len(new) + len(changed)

    # 전체 재계산 대비 증분 처리의 절감 추정.
    # 근접 스크리닝은 쌍 단위이므로, 바뀐 객체가 끼어 있는 쌍만 다시 보면 된다.
    #   전체 쌍   = n(n-1)/2
    #   관련 쌍   = k(n-k) + k(k-1)/2   (k = 다시 계산할 객체 수)
    k = recompute
    pairs_full = n * (n - 1) // 2
    pairs_incr = k * (n - k) + k * (k - 1) // 2

    result = {
        "measured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot": path.name,
        "had_previous_index": bool(prev),
        "objects": n,
        "new": len(new),
        "changed": len(changed),
        "unchanged": len(unchanged),
        "gone": len(gone),
        "recompute": k,
        "recompute_share_pct": round(100.0 * k / n, 2) if n else 0,
        "pairs_full": pairs_full,
        "pairs_incremental": pairs_incr,
        "pairs_saved_pct": round(100.0 * (1 - pairs_incr / pairs_full), 2) if pairs_full else 0,
        "fingerprint_fields": FINGERPRINT_FIELDS,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "diff_latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    save_index(curr)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
