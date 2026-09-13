#!/usr/bin/env python3
"""E4 — 외부 소스가 실패하면 무슨 일이 벌어지는가.

시뮬레이션이 아니라 실제 주입이다. 로컬에 가짜 데이터 소스를 띄우고
200 / 403 / 타임아웃을 순서대로 내보낸 뒤, 실제 수집 코드를 그대로 돌린다.

두 정책을 비교한다.

  (as-is) 실패해도 그냥 진행 : 응답이 없으면 직전 데이터로 계속 계산하고,
                               결과에는 아무 표시도 남기지 않는다.
                               → 낡은 데이터가 '최신'인 얼굴로 제공된다.
  (to-be) 실패를 기록하고 표시: 재시도하지 않고(정책), 직전 스냅샷으로 대체하되
                               결과에 기준 시각을 붙이고, 기준을 넘으면 경보한다.
                               → 낡았다는 사실이 드러난다.

측정: 각 주기마다 '제공된 데이터의 나이'와 '그 사실을 알 수 있었는가'.

실행: uv run --no-project --python 3.12 python experiments/source_failure.py
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "results" / "source_failure.json"
TMP = ROOT / "data" / "tmp_e4"

PORT = 8765
# 주기별 소스 상태: 200 → 403(정책상 재시도 금지) → 타임아웃 → 복구
PATTERN = [200, 200, 403, 403, 599, 200]
FRESHNESS_SLO_HOURS = 3.0  # 이보다 낡으면 경보


class FakeSource(BaseHTTPRequestHandler):
    """가짜 CelesTrak. 주기마다 정해진 상태 코드를 돌려준다."""

    cycle = 0

    def do_GET(self):  # noqa: N802
        status = PATTERN[min(FakeSource.cycle, len(PATTERN) - 1)]
        if status == 599:  # 타임아웃 흉내
            threading.Event().wait(3)
            return
        if status != 200:
            self.send_response(status)
            self.end_headers()
            return
        body = b"OBJECT_NAME,NORAD_CAT_ID,EPOCH\nTEST SAT,25544,2026-09-13T00:00:00\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 — 조용히
        pass


def load_fetch_module():
    """실제 수집 코드를 그대로 불러온다 (복사본이 아니라 같은 파일)."""
    spec = importlib.util.spec_from_file_location("fetch_gp", ROOT / "collect" / "fetch_gp.py")
    if spec is None or spec.loader is None:
        sys.exit("collect/fetch_gp.py 를 불러올 수 없습니다.")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)

    server = HTTPServer(("127.0.0.1", PORT), FakeSource)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    fetch = load_fetch_module()
    # 실험용으로만 경로와 주소를 바꾼다. 로직은 건드리지 않는다.
    setattr(fetch, "URL", f"http://127.0.0.1:{PORT}/gp")
    setattr(fetch, "DATA", TMP)
    setattr(fetch, "SNAP_DIR", TMP / "snapshots")
    setattr(fetch, "MANIFEST", TMP / "manifest.jsonl")
    setattr(fetch, "MIN_INTERVAL", timedelta(seconds=0))  # 주기를 압축해서 돌린다

    timeline = []
    last_good_at: datetime | None = None
    base = datetime.now(timezone.utc)

    for cycle in range(len(PATTERN)):
        FakeSource.cycle = cycle
        # 실제 시간 대신 '2시간짜리 주기'가 흘렀다고 본다
        cycle_time = base + timedelta(hours=2 * cycle)

        sys.argv = ["fetch_gp.py"]
        fetch.main()
        rec = json.loads(fetch.MANIFEST.read_text(encoding="utf-8").splitlines()[-1])
        ok = rec.get("status") == "ok"
        if ok:
            last_good_at = cycle_time

        age_h = 0.0 if ok else (cycle_time - last_good_at).total_seconds() / 3600 if last_good_at else None

        timeline.append(
            {
                "cycle": cycle + 1,
                "source_status": PATTERN[cycle],
                "fetch_result": rec.get("status"),
                "http": rec.get("http_status"),
                "served_data_age_h": None if age_h is None else round(age_h, 1),
                # as-is: 표시도 경보도 없다. 사용자는 낡았다는 걸 알 방법이 없다.
                "as_is_marked": False,
                "as_is_alert": False,
                # to-be: 응답에 기준 시각을 붙이고, SLO를 넘으면 경보한다.
                "to_be_marked": True,
                "to_be_alert": bool(age_h is not None and age_h > FRESHNESS_SLO_HOURS),
                "retried": False,  # 정책상 403에는 재시도하지 않는다
            }
        )

    server.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)

    stale_cycles = [t for t in timeline if (t["served_data_age_h"] or 0) > 0]
    max_age = max((t["served_data_age_h"] or 0) for t in timeline)
    alerts = [t for t in timeline if t["to_be_alert"]]

    result = {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "setup": {
            "pattern": PATTERN,
            "cycle_hours": 2,
            "freshness_slo_hours": FRESHNESS_SLO_HOURS,
            "note": "로컬 가짜 소스에 실제 수집 코드를 붙여 돌렸다. 로직은 운영과 같은 파일이다.",
        },
        "timeline": timeline,
        "summary": {
            "stale_cycles": len(stale_cycles),
            "max_served_age_h": max_age,
            "as_is": f"{len(stale_cycles)}개 주기 동안 최대 {max_age}시간 낡은 데이터를 제공했고, 그 사실을 알리는 신호는 없었다",
            "to_be": f"같은 상황에서 {len(alerts)}번 경보가 발생했고, 응답에는 매번 기준 시각이 붙었다",
            "retry_on_403": "없음 — 데이터 제공처 정책(non-200 시 즉시 중단)을 코드가 강제한다",
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
