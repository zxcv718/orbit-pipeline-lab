#!/usr/bin/env python3
"""데이터 소스 장애 시 두 방식의 동작을 비교한다.

로컬에 모의 데이터 소스를 띄워 200, 403, 타임아웃 응답을 차례로 반환하고,
실제 수집 코드(collect/fetch_gp.py)를 그대로 실행한다.

  as-is : 수집에 실패해도 직전 데이터로 계속 처리하고, 결과에 아무 표시도 하지 않는다.
  to-be : 재시도하지 않고 직전 스냅샷을 사용하되, 결과에 기준 시각을 붙이고
          경과 시간이 기준을 넘으면 경보를 낸다.

주기마다 제공된 데이터의 경과 시간과, 사용자가 이를 알 수 있었는지를 기록한다.

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
FRESHNESS_SLO_HOURS = 3.0  # 경과 시간이 이 값을 넘으면 경보


class FakeSource(BaseHTTPRequestHandler):
    """가짜 CelesTrak. 주기마다 정해진 상태 코드를 돌려준다."""

    cycle = 0

    def do_GET(self):  # noqa: N802
        status = PATTERN[min(FakeSource.cycle, len(PATTERN) - 1)]
        if status == 599:  # 응답을 지연시켜 타임아웃 재현
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

    def log_message(self, format, *args):  # noqa: A002  요청 로그 출력 생략
        pass


def load_fetch_module():
    """collect/fetch_gp.py를 그대로 불러온다."""
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
                # as-is: 기준 시각 표시와 경보가 없어 사용자가 데이터 경과 시간을 알 수 없다.
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
            "note": "로컬 모의 소스에 실제 수집 코드(collect/fetch_gp.py)를 연결해 실행",
        },
        "timeline": timeline,
        "summary": {
            "stale_cycles": len(stale_cycles),
            "max_served_age_h": max_age,
            "as_is": f"{len(stale_cycles)}개 주기 동안 최대 {max_age}시간 지난 데이터 제공, 알림 신호 없음",
            "to_be": f"같은 상황에서 경보 {len(alerts)}회, 모든 응답에 기준 시각 포함",
            "retry_on_403": "재시도 없음 (200이 아닌 응답 시 요청을 중단하는 제공처 정책)",
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
