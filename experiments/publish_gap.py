#!/usr/bin/env python3
"""결과를 갱신하는 동안의 조회 결과를 두 가지 게시 방식으로 비교한다.

  전체 교체 : 기존 컬렉션을 비우고 새로 채운다. 채우는 동안 조회하면 빈 결과나 일부 결과가 반환된다.
  버전 교체 : 새 버전 컬렉션에 모두 기록한 뒤 현재 버전을 가리키는 포인터만 바꾼다.

게시가 진행되는 동안 계속 조회하면서 다음을 센다.
  - 빈 결과를 받은 횟수
  - 불완전한 결과를 받은 횟수
  - 불완전한 상태가 이어진 시간

전제: 로컬 MongoDB 단일 노드, 합성 문서
실행: uv run --no-project --python 3.12 --with pymongo python experiments/publish_gap.py
"""

from __future__ import annotations

import csv
import json
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from pymongo import MongoClient
except ImportError:
    sys.exit("pymongo가 없습니다. uv run --no-project --python 3.12 --with pymongo python experiments/publish_gap.py")

ROOT = Path(__file__).resolve().parents[1]
SNAP_DIR = ROOT / "data" / "snapshots"
OUT = ROOT / "data" / "results" / "publish_gap.json"

MONGO_URI = "mongodb://127.0.0.1:27017"
DB_NAME = "orbit_pipeline_lab"
# 게시할 근접 이벤트 수. 인자로 바꿔가며 돌리면 "공백이 규모에 비례하는가"를 볼 수 있다.
DOCS = int(sys.argv[1]) if len(sys.argv) > 1 else 60_000
BATCH = 5_000
POLL_INTERVAL = 0.01   # 조회 간격 10ms


def real_object_ids(limit: int = 4000) -> list[int]:
    """실제 카탈로그 번호를 쓴다. 합성 데이터라도 형태는 현실과 같게."""
    snaps = sorted(SNAP_DIR.glob("gp-*.csv"))
    if not snaps:
        return list(range(10000, 10000 + limit))
    ids = []
    with snaps[-1].open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                ids.append(int(row["NORAD_CAT_ID"]))
            except (KeyError, ValueError):
                continue
            if len(ids) >= limit:
                break
    return ids


def make_docs(run_id: str, ids: list[int]) -> list[dict]:
    rnd = random.Random(42)
    now = time.time()
    return [
        {
            "run_id": run_id,
            "primary": rnd.choice(ids),
            "secondary": rnd.choice(ids),
            "tca": now + rnd.random() * 7 * 86400,
            "miss_km": round(rnd.random() * 5, 3),
            "pc": rnd.random() * 1e-4,
        }
        for _ in range(DOCS)
    ]


class Reader(threading.Thread):
    """게시가 도는 동안 계속 조회하는 쪽. 실제 서비스의 API 요청에 해당한다."""

    def __init__(self, db, mode: str):
        super().__init__(daemon=True)
        self.db, self.mode = db, mode
        self.stop_flag = threading.Event()
        self.samples: list[tuple[float, int]] = []

    def current_count(self) -> int:
        if self.mode == "as-is":
            return self.db["results"].count_documents({})
        pointer = self.db["meta"].find_one({"_id": "current"})
        if not pointer:
            return 0
        return self.db[pointer["coll"]].count_documents({})

    def run(self) -> None:
        while not self.stop_flag.is_set():
            t = time.perf_counter()
            try:
                self.samples.append((t, self.current_count()))
            except Exception:
                self.samples.append((t, -1))
            time.sleep(POLL_INTERVAL)


def summarize(samples: list[tuple[float, int]], expected: int) -> dict:
    empty = sum(1 for _, c in samples if c == 0)
    partial = sum(1 for _, c in samples if 0 < c < expected)
    complete = sum(1 for _, c in samples if c >= expected)
    bad = [t for t, c in samples if c < expected]
    gap_ms = round((max(bad) - min(bad)) * 1000, 1) if len(bad) > 1 else 0.0
    return {
        "polls": len(samples),
        "empty": empty,
        "partial": partial,
        "complete": complete,
        "incomplete_share_pct": round(100.0 * (empty + partial) / len(samples), 2) if samples else 0,
        "gap_window_ms": gap_ms,
    }


def run_as_is(db, docs: list[dict]) -> dict:
    """전체 교체: 기존 결과를 지우고 다시 채운다."""
    db["results"].drop()
    db["results"].insert_many(make_docs("v0", [d["primary"] for d in docs[:100]]))  # 기존 버전
    reader = Reader(db, "as-is")
    reader.start()
    time.sleep(0.3)

    t0 = time.perf_counter()
    db["results"].delete_many({})                      # ← 여기서부터 조회하는 쪽은 빈 결과를 본다
    for i in range(0, len(docs), BATCH):
        db["results"].insert_many(docs[i : i + BATCH])
    elapsed = time.perf_counter() - t0

    time.sleep(0.3)
    reader.stop_flag.set()
    reader.join()
    return {"publish_s": round(elapsed, 3), **summarize(reader.samples, DOCS)}


def run_to_be(db, docs: list[dict]) -> dict:
    """버전 교체: 새 컬렉션에 다 쓴 뒤 포인터만 바꾼다."""
    for name in db.list_collection_names():
        if name.startswith("results_v"):
            db[name].drop()
    db["results_v0"].insert_many(make_docs("v0", [d["primary"] for d in docs[:100]]))
    db["meta"].replace_one({"_id": "current"}, {"_id": "current", "coll": "results_v0"}, upsert=True)

    reader = Reader(db, "to-be")
    reader.start()
    time.sleep(0.3)

    t0 = time.perf_counter()
    new_coll = "results_v1"
    db[new_coll].drop()
    for i in range(0, len(docs), BATCH):
        db[new_coll].insert_many(docs[i : i + BATCH])
    # 다 쓴 뒤 포인터 한 번만 바꾼다 = 조회하는 쪽에서는 순간이다
    db["meta"].replace_one({"_id": "current"}, {"_id": "current", "coll": new_coll}, upsert=True)
    elapsed = time.perf_counter() - t0

    time.sleep(0.3)
    reader.stop_flag.set()
    reader.join()

    # 이전 버전은 유예 후 정리한다 (읽던 요청이 끝날 시간을 준다)
    db["results_v0"].drop()
    return {"publish_s": round(elapsed, 3), **summarize(reader.samples, 100)}


def main() -> int:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    db = client[DB_NAME]

    ids = real_object_ids()
    docs = make_docs("v1", ids)

    result = {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "setup": {
            "documents": DOCS,
            "batch": BATCH,
            "poll_interval_ms": POLL_INTERVAL * 1000,
            "mongodb": client.server_info().get("version"),
            "note": "조회하는 쪽은 게시가 도는 동안 계속 결과 건수를 센다",
        },
        "as_is_full_replace": run_as_is(db, docs),
        "to_be_versioned_swap": run_to_be(db, docs),
    }

    gap = result["as_is_full_replace"]["gap_window_ms"]
    result["headline"] = (
        f"전체 교체는 {gap:.0f}ms 동안 조회 결과가 비거나 불완전했고, "
        f"버전 교체의 불완전 조회 비율은 {result['to_be_versioned_swap']['incomplete_share_pct']}%"
    )

    # 규모별로 여러 번 돌린 결과를 한 파일에 쌓는다 (비례 관계를 보기 위함)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text(encoding="utf-8"))
            history = prev.get("runs", [])
        except Exception:
            history = []
    history = [r for r in history if r.get("setup", {}).get("documents") != DOCS] + [result]
    history.sort(key=lambda r: r.get("setup", {}).get("documents", 0))
    OUT.write_text(
        json.dumps({"runs": history}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    client.drop_database(DB_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
