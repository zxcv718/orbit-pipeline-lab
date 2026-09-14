#!/usr/bin/env python3
"""수집 결과로 정적 대시보드 페이지를 만든다 (표준 라이브러리만 사용).

별도 서버 없이 GitHub Pages에서 계속 열람할 수 있도록 정적 HTML로 생성한다.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "site" / "public"

CRON_MINUTE = 17  # collect.yml 의 "17 * * * *"
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
STATUS_LABEL = {"ok": "성공", "skipped": "건너뜀", "http_error": "HTTP 오류", "error": "오류"}


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    try:
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except Exception:
        return []
    return rows[-limit:] if limit else rows


def start_delay_minutes(rec: dict) -> float | None:
    """실제 시작 시각이 속한 매시 예약 시각(17분) 대비 지연(분).

    manifest의 scheduled_at은 2026-09-14 이전 기록에 계산 오류가 있어 쓰지 않는다.
    scheduled_at이 없는 기록은 수동 실행이므로 제외한다.
    """
    actual = rec.get("fetched_at")
    if not actual or not rec.get("scheduled_at"):
        return None
    try:
        t = datetime.strptime(actual, TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    slot = t.replace(minute=CRON_MINUTE, second=0, microsecond=0)
    if slot > t:
        slot -= timedelta(hours=1)
    return (t - slot).total_seconds() / 60


def bar_chart(values: list[tuple[str, float]], unit: str = "", width: int = 560) -> str:
    """막대 하나당 (라벨, 값). 외부 라이브러리 없이 인라인 SVG로 그린다."""
    if not values:
        return "<p class='muted'>아직 데이터가 없습니다.</p>"
    top = max(v for _, v in values) or 1
    rows = []
    for i, (label, v) in enumerate(values):
        w = max(2, int((v / top) * (width - 190)))
        y = i * 26
        rows.append(
            f"<text x='0' y='{y + 14}' class='lbl'>{escape(label)}</text>"
            f"<rect x='130' y='{y + 3}' width='{w}' height='15' rx='3' class='bar'/>"
            f"<text x='{135 + w}' y='{y + 15}' class='val'>{v:g}{unit}</text>"
        )
    h = len(values) * 26 + 6
    return f"<svg viewBox='0 0 {width} {h}' width='100%' height='{h}'>{''.join(rows)}</svg>"


def experiments_html() -> str:
    """실험 결과 파일이 있는 것만 표시한다."""
    parts = []

    gap = read_json(DATA / "results" / "publish_gap.json", {}) or {}
    runs = gap.get("runs", [])
    if runs:
        rows = "".join(
            "<tr>"
            f"<td>{r['setup']['documents']:,}</td>"
            f"<td class='warn'>{r['as_is_full_replace']['gap_window_ms']:,.0f}ms</td>"
            f"<td class='warn'>{r['as_is_full_replace']['incomplete_share_pct']}%</td>"
            f"<td class='ok'>{r['to_be_versioned_swap']['gap_window_ms']:,.0f}ms</td>"
            f"<td>{r['as_is_full_replace']['publish_s']}초 / {r['to_be_versioned_swap']['publish_s']}초</td>"
            "</tr>"
            for r in runs
        )
        parts.append(
            "<h2>결과 교체 중 조회 공백</h2>"
            "<p class='muted'>전체 교체는 기존 결과를 지운 뒤 다시 채우는 방식이고, 버전 교체는 새 버전에 모두 기록한 뒤 참조만 바꾸는 방식입니다. "
            "로컬 MongoDB 단일 노드와 합성 문서로 측정했습니다.</p>"
            "<div class='scroll'><table>"
            "<tr><th>문서 수</th><th>전체 교체 공백</th><th>불완전 조회 비율</th><th>버전 교체 공백</th><th>게시 시간 (전체 / 버전)</th></tr>"
            f"{rows}</table></div>"
        )

    fail = read_json(DATA / "results" / "source_failure.json", {}) or {}
    if fail.get("summary"):
        s = fail["summary"]
        alerts = sum(1 for t in fail.get("timeline", []) if t.get("to_be_alert"))
        parts.append(
            "<h2>데이터 소스 장애 시 동작</h2>"
            "<div class='note'>"
            f"<p><strong>기준 시각 표시 없이 진행</strong><br>{s.get('stale_cycles', '–')}개 주기 동안 최대 "
            f"{s.get('max_served_age_h', 0):g}시간 지난 데이터를 제공했고, 이를 알리는 신호가 없었습니다.</p>"
            f"<p><strong>기준 시각 표시와 경보</strong><br>같은 상황에서 경보가 {alerts}회 발생했고, 모든 응답에 기준 시각이 포함되었습니다.</p>"
            "<p class='muted'>403 응답에는 재시도하지 않습니다. 200이 아닌 응답을 받으면 요청을 중단하라는 제공처 정책을 코드에서 지킵니다.</p>"
            "</div>"
        )

    bench = read_json(DATA / "results" / "propagate_bench.json", {}) or {}
    if bench.get("full_recompute"):
        f, e, env = bench["full_recompute"], bench["extrapolation_1min_step"], bench["environment"]
        accel = "C++ 가속 사용" if env.get("sgp4_accelerated") else "C++ 가속 미사용"
        parts.append(
            "<h2>궤도 전파 비용</h2>"
            f"<p class='muted'>활성 객체 {bench['input']['satellites']:,}개를 {bench['input']['window_days']}일분, "
            f"{bench['input']['step_hours']}시간 간격으로 전파하는 데 <strong>{f['elapsed_s']}초</strong>가 걸렸습니다 "
            f"(상태벡터 {f['state_vectors']:,}개, 초당 {f['throughput_per_s']:,}개). "
            f"샘플 간격을 1분으로 줄이면 약 {e['estimated_s']}초, 결과를 모두 저장하면 약 {e['estimated_memory_GB_if_kept']}GB가 필요합니다. "
            f"측정 환경: Python {env['python']}, sgp4 {accel}, {env['machine']}</p>"
        )

    return "".join(parts)


def main() -> int:
    stats = read_json(DATA / "results" / "snapshot_stats.json", {}) or {}
    diff = read_json(DATA / "results" / "diff_latest.json", {}) or {}
    runs = read_jsonl(DATA / "manifest.jsonl")
    # 첫 수집은 비교 대상이 없어 변경률이 100%로 기록되므로 제외한다
    diffs = [d for d in read_jsonl(DATA / "results" / "diff_history.jsonl") if d.get("had_previous_index")]

    ok_runs = [r for r in runs if r.get("status") == "ok"]
    skipped = sum(1 for r in runs if r.get("status") == "skipped")
    delays = [d for d in (start_delay_minutes(r) for r in ok_runs) if d is not None]
    age = stats.get("e1_epoch_age_hours", {})
    e6 = stats.get("e6_schema_risk", {})

    cards = [
        ("활성 객체", f"{stats.get('objects_total', 0):,}", "CelesTrak active 그룹, 최근 수집 기준"),
        (
            "TLE로 받을 수 없는 객체",
            f"{e6.get('norad_over_99999', 0):,}",
            f"{e6.get('share_pct', 0):.1f}%, 6자리 카탈로그 번호 객체",
        ),
        ("궤도 요소 경과 시간 중앙값", f"{age.get('p50', '–')}시간", f"p90 {age.get('p90', '–')}시간, 원천과 재배포 지연 합산"),
        (
            "직전 수집 대비 변경 객체",
            f"{diff.get('recompute_share_pct', '–')}%",
            f"재계산 대상 {diff.get('recompute', 0):,}개" if diff else "스냅샷이 2개 이상 쌓이면 표시",
        ),
        ("수집 실행", f"{len(runs)}회", f"성공 {len(ok_runs)}회, 수집 간격 정책으로 건너뜀 {skipped}회"),
        (
            "예약 대비 시작 지연",
            f"{statistics.median(delays):.0f}분" if delays else "–",
            f"중앙값, 최대 {max(delays):.0f}분 (실행 {len(delays)}회)" if delays else "예약 실행 후 표시",
        ),
    ]
    card_html = "".join(
        f"<div class='card'><div class='k'>{escape(k)}</div><div class='v'>{escape(str(v))}</div>"
        f"<div class='s'>{escape(str(s))}</div></div>"
        for k, v, s in cards
    )

    age_chart = bar_chart(
        [
            ("2시간 이내", age.get("share_under_2h_pct", 0)),
            ("6시간 이내", age.get("share_under_6h_pct", 0)),
            ("12시간 이내", age.get("share_under_12h_pct", 0)),
            ("24시간 이내", age.get("share_under_24h_pct", 0)),
            ("48시간 이내", age.get("share_under_48h_pct", 0)),
        ],
        unit="%",
    )
    recompute_chart = bar_chart(
        [(d.get("measured_at", "")[5:16].replace("T", " "), d.get("recompute_share_pct", 0)) for d in diffs[-12:]],
        unit="%",
    )

    def run_row(r: dict) -> str:
        d = start_delay_minutes(r)
        cls = "ok" if r.get("status") == "ok" else "warn"
        status = str(r.get("status") or "")
        return (
            "<tr>"
            f"<td>{escape(str(r.get('fetched_at') or ''))}</td>"
            f"<td class='{cls}'>{escape(STATUS_LABEL.get(status, status))}</td>"
            f"<td>{escape(str(r.get('http_status', '–')))}</td>"
            f"<td>{r.get('objects', '–')}</td>"
            f"<td>{r.get('elapsed_s', '–')}</td>"
            f"<td>{'–' if d is None else round(d)}</td>"
            "</tr>"
        )

    run_rows = "".join(run_row(r) for r in reversed(runs[-24:]))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orbit Pipeline Lab</title>
<style>
:root {{ --bg:#fbfbfa; --fg:#1a1a19; --mut:#6b6b68; --line:#e4e4e1; --acc:#2f5ea8; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:32px 20px 64px; background:var(--bg); color:var(--fg);
  font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif; }}
.wrap {{ max-width:860px; margin:0 auto; }}
h1 {{ font-size:24px; margin:0 0 6px; letter-spacing:-.02em; }}
h2 {{ font-size:17px; margin:36px 0 12px; }}
.sub {{ color:var(--mut); margin:0 0 28px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
.card .k {{ font-size:12px; color:var(--mut); }}
.card .v {{ font-size:26px; font-weight:600; letter-spacing:-.02em; margin:2px 0; }}
.card .s {{ font-size:12px; color:var(--mut); }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); }}
th {{ color:var(--mut); font-weight:500; }}
.ok {{ color:#1f7a4d; }} .warn {{ color:#b0631a; }}
.bar {{ fill:var(--acc); }} .lbl {{ font-size:12px; fill:var(--mut); }} .val {{ font-size:12px; fill:var(--fg); }}
.muted {{ color:var(--mut); }}
.note {{ background:#fff; border:1px solid var(--line); border-left:3px solid var(--acc);
  border-radius:6px; padding:12px 14px; font-size:13px; }}
.note p {{ margin:0 0 10px; }} .note p:last-child {{ margin-bottom:0; }}
code {{ background:#f0f0ee; padding:1px 5px; border-radius:4px; font-size:12px; }}
.scroll {{ overflow-x:auto; }}
</style></head><body><div class="wrap">

<h1>Orbit Pipeline Lab</h1>
<p class="sub">공개 궤도 데이터(CelesTrak GP)로 측정한 데이터 파이프라인 실험 결과입니다. 마지막 갱신 {now}</p>

<div class="grid">{card_html}</div>

<h2>궤도 요소 경과 시간 분포</h2>
<p class="muted">각 객체의 EPOCH(궤도 결정 시각)부터 수집 시점까지 지난 시간입니다.
공개 GP 데이터에는 카탈로그 등재 시각이 없어 원천 지연과 재배포 지연이 합쳐진 값입니다.</p>
{age_chart}

<h2>수집 주기별 변경 비율 (UTC)</h2>
<p class="muted">직전 수집 대비 궤도 요소가 바뀐 객체의 비율입니다. 비율이 낮은 주기일수록 변경분만 계산해 줄일 수 있는 작업이 많습니다.</p>
{recompute_chart}

{experiments_html()}

<h2>수집 실행 기록</h2>
<div class="scroll"><table>
<tr><th>수집 시각(UTC)</th><th>결과</th><th>HTTP</th><th>객체 수</th><th>소요 시간(초)</th><th>예약 대비 지연(분)</th></tr>
{run_rows}
</table></div>

<h2>측정 조건과 한계</h2>
<div class="note">
<p><strong>페이지 목적</strong><br>정해진 시각에 전체를 다시 계산하는 방식과, 데이터가 바뀐 경우에만 변경분을 계산하는 방식의 차이를
공개 데이터로 측정한 실험입니다. 근접 분석(충돌 위험 계산)은 포함하지 않습니다.</p>
<p><strong>한계</strong><br>EPOCH는 게시 시각이 아니라 궤도를 결정한 기준 시각입니다.
CelesTrak은 2시간마다 갱신되며 다른 데이터 제공처와 갱신 주기가 다릅니다.
원본 스냅샷은 용량 문제로 저장소에 보관하지 않으며, <code>collect/fetch_gp.py</code>로 다시 받을 수 있습니다.
2026-09-14 이전 수집 기록의 예약 시각에는 계산 오류가 있어, 지연은 실제 시작 시각 기준으로 다시 계산해 표시합니다.
예약 지연은 GitHub Actions 무료 스케줄러의 관측값입니다.</p>
<p><strong>데이터 이용 정책</strong><br>CelesTrak 정책에 따라 2시간 갱신 주기당 1회만 요청하고,
200이 아닌 응답을 받으면 재시도하지 않고 직전 스냅샷을 사용합니다.</p>
</div>

</div></body></html>
"""

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "index.html").write_text(html, encoding="utf-8")
    (OUT / ".nojekyll").write_text("", encoding="utf-8")
    print(f"생성: {(OUT / 'index.html').relative_to(ROOT)} ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
