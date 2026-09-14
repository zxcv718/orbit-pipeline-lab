#!/usr/bin/env python3
"""CelesTrak GP 스냅샷 수집기. 이용 정책에 맞춰 요청 간격과 실패 처리를 제한한다.

정책 근거 (https://celestrak.org/usage-policy.php , 2026-09-11 확인):
  - GP 데이터는 2시간마다 갱신되고 "only download data once per update".
  - HTTP 200이 아닌 응답을 받으면 "should immediately stop querying". 재시도하지 않는다.
  - 하루 100MB를 넘기지 않도록 JSON보다 작은 CSV 형식을 사용한다.
  - celestrak.com은 301로 이동하므로 .org만 사용한다.

동작:
  - 수집 한 단계만 수행하고, 실패해도 예외로 종료하지 않는다.
  - 성공, 건너뜀, 실패 모두 data/manifest.jsonl에 한 줄씩 기록한다.
    실패한 주기에는 후속 처리가 직전 스냅샷을 사용한다.

사용법:
  python3 poc/collect/fetch_gp.py            # 정책상 받을 때가 됐으면 받는다
  python3 poc/collect/fetch_gp.py --force    # 간격 검사를 건너뛴다 (실험용, 남용 금지)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GROUP = "active"
URL = f"https://celestrak.org/NORAD/elements/gp.php?GROUP={GROUP}&FORMAT=CSV"
USER_AGENT = "orbit-pipeline-lab/0.1 (+https://github.com/zxcv718/orbit-pipeline-lab)"

# CelesTrak GP 갱신 주기는 2시간. 갱신당 1회만 받는다.
MIN_INTERVAL = timedelta(hours=2)

# 이 파일은 <repo>/collect/fetch_gp.py 위치에 있다.
# 작업 폴더에서는 poc/ 디렉터리가, 공개 저장소에서는 저장소 루트가 ROOT가 된다.
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SNAP_DIR = DATA / "snapshots"
MANIFEST = DATA / "manifest.jsonl"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def last_ok_fetch() -> datetime | None:
    """마지막으로 성공한 수집 시각. 정책 준수를 위한 자체 게이트."""
    if not MANIFEST.exists():
        return None
    last = None
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("status") == "ok":
            last = datetime.strptime(rec["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
    return last


def append_manifest(record: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="간격 검사 건너뛰기")
    args = parser.parse_args()

    now = utcnow()
    # GitHub Actions가 넘겨주면 '예약 시각 대비 실제 시작 지연'을 잴 수 있다.
    scheduled_at = os.environ.get("SCHEDULED_AT")

    prev = last_ok_fetch()
    if prev and not args.force and now - prev < MIN_INTERVAL:
        rec = {
            "fetched_at": iso(now),
            "scheduled_at": scheduled_at,
            "status": "skipped",
            "reason": f"last ok fetch {int((now - prev).total_seconds())}s ago < {int(MIN_INTERVAL.total_seconds())}s",
        }
        append_manifest(rec)
        print(json.dumps(rec, ensure_ascii=False))
        return 0

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(URL, headers={"User-Agent": USER_AGENT})
    started = time.monotonic()

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:
        # 정책: 200이 아니면 즉시 중단. 재시도하지 않는다.
        rec = {
            "fetched_at": iso(now),
            "scheduled_at": scheduled_at,
            "status": "http_error",
            "http_status": e.code,
            "note": "정책상 재시도하지 않음. 후속 처리는 직전 스냅샷 사용.",
        }
        append_manifest(rec)
        print(json.dumps(rec, ensure_ascii=False))
        return 0
    except Exception as e:  # 네트워크 오류 등
        rec = {
            "fetched_at": iso(now),
            "scheduled_at": scheduled_at,
            "status": "error",
            "error": f"{type(e).__name__}: {e}",
        }
        append_manifest(rec)
        print(json.dumps(rec, ensure_ascii=False))
        return 0

    elapsed = round(time.monotonic() - started, 3)
    path = SNAP_DIR / f"gp-{GROUP}-{now.strftime('%Y%m%dT%H%M%SZ')}.csv"
    path.write_bytes(body)

    text = body.decode("utf-8", errors="replace")
    lines = text.splitlines()
    objects = max(len(lines) - 1, 0)  # 헤더 1줄 제외

    rec = {
        "fetched_at": iso(now),
        "scheduled_at": scheduled_at,
        "status": "ok",
        "http_status": status,
        "url": URL,
        "path": str(path.relative_to(ROOT)),
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "objects": objects,
        "elapsed_s": elapsed,
    }
    append_manifest(rec)
    print(json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
