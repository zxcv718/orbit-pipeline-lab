#!/usr/bin/env python3
"""수집 결과를 정적 페이지 하나로 만든다 (의존성 없음, stdlib만).

왜 정적인가:
  평가·면접 기간 내내 열려 있어야 한다. 서버가 있으면 잠들거나 죽는다.
  "스케줄링을 안정적으로 만들겠다"는 문서의 데모가 죽어 있으면 그 자체가 반증이다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "site" / "public"


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


def delay_seconds(rec: dict) -> float | None:
    """예약 시각 대비 실제 시작 지연. 시간 기반 트리거가 실제로 얼마나 늦는지의 실측."""
    sched, actual = rec.get("scheduled_at"), rec.get("fetched_at")
    if not sched or not actual:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(actual, fmt) - datetime.strptime(sched, fmt)).total_seconds()
    except Exception:
        return None


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


def main() -> int:
    stats = read_json(DATA / "results" / "snapshot_stats.json", {}) or {}
    diff = read_json(DATA / "results" / "diff_latest.json", {}) or {}
    runs = read_jsonl(DATA / "manifest.jsonl")
    diffs = read_jsonl(DATA / "results" / "diff_history.jsonl")

    ok_runs = [r for r in runs if r.get("status") == "ok"]
    delays = [d for d in (delay_seconds(r) for r in ok_runs) if d is not None]
    age = stats.get("e1_epoch_age_hours", {})
    e6 = stats.get("e6_schema_risk", {})

    cards = [
        ("활성 객체", f"{stats.get('objects_total', 0):,}", "CelesTrak active 그룹"),
        (
            "TLE로 표현 불가",
            f"{e6.get('norad_over_99999', 0):,}",
            f"{e6.get('share_pct', 0)}% · 6자리 번호 (2026-07-11 5자리 소진)",
        ),
        ("요소 나이 중앙값", f"{age.get('p50', '–')}h", f"p90 {age.get('p90', '–')}h · 원천 데이터 자체의 나이"),
        (
            "이번 주기 재계산 대상",
            f"{diff.get('recompute_share_pct', '–')}%",
            f"쌍 기준 절감 {diff.get('pairs_saved_pct', '–')}%" if diff else "스냅샷 2개부터 의미 있음",
        ),
        ("수집 실행", f"{len(runs)}회", f"성공 {len(ok_runs)}회"),
        (
            "예약 대비 시작 지연",
            f"{round(sum(delays) / len(delays))}s" if delays else "–",
            f"최대 {round(max(delays))}s" if delays else "스케줄 실행 후 측정",
        ),
    ]
    card_html = "".join(
        f"<div class='card'><div class='k'>{escape(k)}</div><div class='v'>{escape(str(v))}</div>"
        f"<div class='s'>{escape(str(s))}</div></div>"
        for k, v, s in cards
    )

    age_chart = bar_chart(
        [
            ("≤ 2시간", age.get("share_under_2h_pct", 0)),
            ("≤ 6시간", age.get("share_under_6h_pct", 0)),
            ("≤ 12시간", age.get("share_under_12h_pct", 0)),
            ("≤ 24시간", age.get("share_under_24h_pct", 0)),
            ("≤ 48시간", age.get("share_under_48h_pct", 0)),
        ],
        unit="%",
    )
    recompute_chart = bar_chart(
        [(d.get("measured_at", "")[5:16], d.get("recompute_share_pct", 0)) for d in diffs[-12:]],
        unit="%",
    )

    def run_row(r: dict) -> str:
        d = delay_seconds(r)
        cls = "ok" if r.get("status") == "ok" else "warn"
        return (
            "<tr>"
            f"<td>{escape(r.get('fetched_at', ''))}</td>"
            f"<td class='{cls}'>{escape(r.get('status', ''))}</td>"
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
code {{ background:#f0f0ee; padding:1px 5px; border-radius:4px; font-size:12px; }}
.scroll {{ overflow-x:auto; }}
</style></head><body><div class="wrap">

<h1>Orbit Pipeline Lab</h1>
<p class="sub">공개 궤도 데이터(CelesTrak GP)로 재본 데이터 파이프라인의 신선도·증분 처리 실험 · 갱신 {now}</p>

<div class="grid">{card_html}</div>

<h2>궤도 요소의 나이 분포</h2>
<p class="muted">각 객체의 EPOCH가 지금으로부터 얼마나 지났는지. <strong>원천 데이터 자체가 이미 하루 가까이 묵어 있다</strong>는 뜻이고,
수집 주기를 늘리면 이 위에 지연이 더 얹힌다.</p>
{age_chart}

<h2>주기당 재계산 대상 비율</h2>
<p class="muted">직전 스냅샷과 비교해 궤도 요소가 실제로 바뀐 객체의 비율. 낮을수록 증분 처리의 이득이 크다.</p>
{recompute_chart}

<h2>수집 실행 원장</h2>
<div class="scroll"><table>
<tr><th>수집 시각(UTC)</th><th>상태</th><th>HTTP</th><th>객체 수</th><th>소요(s)</th><th>예약 대비 지연(s)</th></tr>
{run_rows}
</table></div>

<h2>읽는 법과 한계</h2>
<div class="note">
<p><strong>이 페이지는 무엇인가</strong> — 시간 기반 일괄 처리와 데이터 기반 증분 처리의 차이를
공개 데이터로 직접 재보기 위한 실험입니다. 정밀 충돌분석이 아니며, 스크리닝은 계산 부하를 보기 위한 단순화 버전입니다.</p>
<p><strong>한계</strong> — ① EPOCH는 발행 시각이 아니라 궤도 상태의 기준 시각입니다.
② CelesTrak은 2시간마다 갱신되며, 다른 데이터 제공처와 갱신 주기가 다릅니다.
③ 원본 스냅샷은 레포에 보관하지 않습니다(용량). 재현은 <code>collect/fetch_gp.py</code>로 가능합니다.</p>
<p><strong>데이터 이용 정책</strong> — CelesTrak 정책에 따라 갱신 주기(2시간)당 1회만 요청하고,
200이 아닌 응답을 받으면 재시도하지 않고 직전 스냅샷으로 대체합니다.</p>
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
