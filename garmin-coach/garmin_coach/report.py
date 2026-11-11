"""Itsenäinen HTML-raportti (ei ulkoisia riippuvuuksia, toimii offline ja puhelimessa)."""
from __future__ import annotations

import html
from datetime import date, timedelta

from .adapt import fmt_day, session_done
from .analysis import Analysis, hr_zones
from .models import GOAL_LABELS, Activity, DailyMetrics, Goal, Profile, Session

LEVEL_FI = {"green": "Valmis harjoittelemaan", "yellow": "Kevennä tänään", "red": "Palaudu tänään"}
CONF_FI = {"high": "korkea", "medium": "kohtalainen", "low": "matala"}


def _e(s) -> str:
    return html.escape(str(s))


def line_chart(series: list[tuple[str, dict[date, float], str]], start: date, end: date, unit: str,
               band: tuple[float, float] | None = None, height: int = 160) -> str:
    """Yksinkertainen SVG-viivakaavio. series = [(nimi, {päivä: arvo}, css-väri-muuttuja)]."""
    w, h, pl, pr, pt, pb = 640, height, 40, 10, 10, 22
    vals = [v for _, s, _ in series for d, v in s.items() if start <= d <= end and v is not None]
    if band:
        vals += list(band)
    if not vals:
        return '<p class="muted">Ei dataa tältä jaksolta.</p>'
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.1 or 1
    lo, hi = lo - pad, hi + pad
    ndays = max(1, (end - start).days)
    x = lambda d: pl + (w - pl - pr) * (d - start).days / ndays  # noqa: E731
    y = lambda v: pt + (h - pt - pb) * (1 - (v - lo) / (hi - lo))  # noqa: E731
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">']
    for i in range(4):
        v = lo + (hi - lo) * i / 3
        parts.append(f'<line x1="{pl}" x2="{w - pr}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
                     f'<text x="{pl - 6}" y="{y(v) + 4:.1f}" class="axis" text-anchor="end">{v:.0f}</text>')
    if band:
        parts.append(f'<rect x="{pl}" width="{w - pl - pr}" y="{y(band[1]):.1f}" height="{y(band[0]) - y(band[1]):.1f}" class="band"/>')
    for d in (start, start + (end - start) / 2, end):
        parts.append(f'<text x="{x(d):.1f}" y="{h - 6}" class="axis" text-anchor="middle">{d.day}.{d.month}.</text>')
    for name, s, color in series:
        pts = [(x(d), y(v)) for d, v in sorted(s.items()) if start <= d <= end and v is not None]
        if not pts:
            continue
        path = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{py:.1f}" for i, (px, py) in enumerate(pts))
        parts.append(f'<path d="{path}" fill="none" stroke="var({color})" stroke-width="2" stroke-linejoin="round"/>')
        lx, ly = pts[-1]
        parts.append(f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="var({color})"/>')
    parts.append("</svg>")
    legend = " ".join(f'<span class="key"><i style="background:var({c})"></i>{_e(n)}</span>' for n, _, c in series)
    if band:
        legend += ' <span class="key"><i class="bandkey"></i>normaalialue</span>'
    return "".join(parts) + f'<div class="legend">{legend} <span class="muted">({_e(unit)})</span></div>'


def bar_chart(values: dict[date, float], start: date, end: date, height: int = 120) -> str:
    w, h, pl, pb = 640, height, 40, 22
    days = [(start + timedelta(days=i)) for i in range((end - start).days + 1)]
    mx = max([values.get(d, 0) for d in days] + [1])
    bw = (w - pl - 10) / len(days)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="chart" role="img">']
    for i, d in enumerate(days):
        v = values.get(d, 0)
        bh = (h - pb - 10) * v / mx
        if v:
            parts.append(f'<rect x="{pl + i * bw + 1:.1f}" y="{h - pb - bh:.1f}" width="{max(1, bw - 2):.1f}" height="{bh:.1f}" rx="2" fill="var(--c2)"/>')
    parts.append(f'<text x="{pl - 6}" y="16" class="axis" text-anchor="end">{mx:.0f}</text>')
    for d in (start, end):
        parts.append(f'<text x="{pl + (days.index(d) + 0.5) * bw:.1f}" y="{h - 6}" class="axis" text-anchor="middle">{d.day}.{d.month}.</text>')
    parts.append("</svg>")
    return "".join(parts)


def _metric(label: str, value: str, sub: str = "", cls: str = "") -> str:
    return f'<div class="metric {cls}"><div class="mlabel">{_e(label)}</div><div class="mval">{value}</div><div class="msub">{sub}</div></div>'


def render(as_of: date, analysis: Analysis, goal: Goal | None, profile: Profile, metrics: list[DailyMetrics],
           activities: list[Activity], past_plan: list[Session], future_plan: list[Session],
           notes: list[str], log: list[tuple[str, str]], chart_days: int = 60) -> str:
    a = analysis
    rd = a.readiness
    start = as_of - timedelta(days=chart_days - 1)
    m_by = {m.day: m for m in metrics}
    series = lambda attr: {d: getattr(m, attr) for d, m in m_by.items() if getattr(m, attr) is not None}  # noqa: E731

    hrv_txt = f"{a.hrv.last_night_ms:.0f} ms" if a.hrv.last_night_ms else "–"
    hrv_sub = (f"7 vrk ka {a.hrv.avg7_ms:.0f} · normaali {a.hrv.band_ms[0]:.0f}–{a.hrv.band_ms[1]:.0f}"
               if a.hrv.band_ms else "perustaso muodostuu (väh. 14 yötä)")
    rhr_txt = f"{a.rhr.today:.0f} bpm" if a.rhr.today else "–"
    rhr_sub = f"perustaso {a.rhr.baseline:.0f} ({a.rhr.delta:+.0f})" if a.rhr.delta is not None else "ei perustasoa"
    sl_txt = f"{a.sleep.last_night_h:.1f} h" if a.sleep.last_night_h else "–"
    sl_sub = f"7 vrk ka {a.sleep.avg7_h:.1f} h · vaje {a.sleep.debt7_h:.1f} h" if a.sleep.avg7_h else ""
    acwr = f"{a.load.acwr:.2f}" if a.load.acwr is not None else "–"

    zones = hr_zones(profile.max_hr, a.resting_hr_used)
    zones_html = " · ".join(f"{z} {lo}–{hi}" for z, (lo, hi) in zones.items())

    goal_html = "<p>Tavoitetta ei ole asetettu.</p>"
    if goal:
        when = f" · {goal.event_date.day}.{goal.event_date.month}.{goal.event_date.year}" if goal.event_date else ""
        left = f" · {(goal.event_date - as_of).days} pv jäljellä" if goal.event_date and goal.event_date >= as_of else ""
        freq = (f"{goal.days_per_week} ajoa + {goal.strength_days} voimaharjoitusta/vko" if goal.kind == "cycling"
                else f"{goal.days_per_week} harjoitusta/vko")
        ftp = f" · FTP {profile.ftp} W" if goal.kind == "cycling" and profile.ftp else ""
        goal_html = f"<p><b>{_e(GOAL_LABELS[goal.kind])}</b>{when}{left} · {freq}{ftp}</p>"

    rows = []
    for s in past_plan[-7:]:
        done = session_done(s, activities)
        mark = "" if done is None else ('<span class="ok">✓ tehty</span>' if done else '<span class="miss">✗ ei tehty</span>')
        rows.append(f'<tr class="past"><td>{fmt_day(s.day)}</td><td>{_e(s.title)} {mark}</td><td>{_e(s.label)}</td></tr>')
    for s in future_plan[:21]:
        cls = "today" if s.day == as_of else ""
        adj = f'<div class="adj">↻ {_e(s.adjusted)}</div>' if s.adjusted else ""
        dist = _e(s.label)
        det = "" if s.kind == "rest" else f'<div class="det">{_e(s.details)}</div>'
        rows.append(f'<tr class="{cls} k-{s.kind}"><td>{fmt_day(s.day)}</td><td><b>{_e(s.title)}</b> '
                    f'<span class="phase">{_e(s.phase)}</span>{det}{adj}</td><td>{dist}</td></tr>')

    notes_html = "".join(f"<li>{_e(n)}</li>" for n in notes) or "<li>Ei muutoksia perusohjelmaan.</li>"
    log_html = "".join(f"<li><span class='muted'>{_e(d)}</span> {_e(m)}</li>" for d, m in log) or "<li>Ei merkintöjä.</li>"
    reasons = "".join(f"<li>{_e(r)}</li>" for r in rd.reasons)

    return f"""<!doctype html>
<html lang="fi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Garmin Coach</title>
<style>
:root{{--bg:#f6f7f9;--surf:#fff;--edge:#e3e6ea;--text:#1d2329;--muted:#6a7380;--c1:#2a6fdb;--c2:#0f9d8a;--c3:#d9534f;
--green:#1f9d55;--yellow:#c98a00;--red:#d64545;--bandc:rgba(42,111,219,.12)}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0f1318;--surf:#171c23;--edge:#262d36;--text:#e3e8ee;
--muted:#8b95a3;--c1:#6ea2ff;--c2:#3cc8b2;--c3:#ff7b72;--green:#3fbf75;--yellow:#e3b341;--red:#ff6b6b;--bandc:rgba(110,162,255,.15)}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:960px;margin:0 auto;padding:16px}}h1{{font-size:22px;margin:4px 0}}h2{{font-size:16px;margin:0 0 10px}}
.card{{background:var(--surf);border:1px solid var(--edge);border-radius:12px;padding:16px;margin:12px 0}}
.muted{{color:var(--muted)}}.grid4{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.metric{{border:1px solid var(--edge);border-radius:10px;padding:10px}}.mlabel{{font-size:12px;color:var(--muted)}}
.mval{{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}}.msub{{font-size:12px;color:var(--muted)}}
.ready{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}.score{{font-size:44px;font-weight:700;font-variant-numeric:tabular-nums}}
.green .score{{color:var(--green)}}.yellow .score{{color:var(--yellow)}}.red .score{{color:var(--red)}}
.chart{{width:100%;height:auto;display:block}}.grid{{stroke:var(--edge)}}.axis{{fill:var(--muted);font-size:11px}}
.band{{fill:var(--bandc)}}.legend{{font-size:12px;color:var(--muted);margin-top:4px}}
.key{{margin-right:12px}}.key i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}}
.bandkey{{background:var(--bandc);border:1px solid var(--c1)}}
.tbl{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}td{{padding:8px 6px;border-top:1px solid var(--edge);vertical-align:top}}
td:nth-child(1){{white-space:nowrap;color:var(--muted)}}td:nth-child(3){{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.det{{color:var(--muted);font-size:13px;margin-top:2px}}.phase{{font-size:11px;color:var(--muted)}}tr.past{{opacity:.6}}
tr.today{{background:var(--bandc)}}tr.k-rest td:nth-child(2){{color:var(--muted)}}.adj{{color:var(--yellow);font-size:12px;margin-top:2px}}
.ok{{color:var(--green)}}.miss{{color:var(--red)}}ul{{margin:0;padding-left:20px}}
@media (max-width:600px){{.axis{{font-size:20px}}td{{padding:8px 4px}}}}
</style></head><body><main>
<h1>Garmin Coach</h1><div class="muted">Tilanne {as_of.day}.{as_of.month}.{as_of.year}</div>

<section class="card {rd.level}"><div class="ready"><div class="score">{rd.score}</div>
<div><h2 style="margin:0">{LEVEL_FI[rd.level]}</h2><div class="muted">Arvion luotettavuus: {CONF_FI[rd.confidence]}</div></div></div>
<ul style="margin-top:10px">{reasons}</ul></section>

<section class="card"><div class="grid4">
{_metric("HRV viime yö", hrv_txt, hrv_sub)}{_metric("Leposyke", rhr_txt, rhr_sub)}
{_metric("Uni viime yö", sl_txt, sl_sub)}{_metric("Kuorma ATL / CTL", f"{a.load.atl:.0f} / {a.load.ctl:.0f}", f"ACWR {acwr} · TSB {a.load.tsb:+.0f}")}
</div></section>

<section class="card"><h2>Tavoite ja ohjelma</h2>{goal_html}
<h2 style="margin-top:12px">Tämän päivän mukautukset</h2><ul>{notes_html}</ul>
<div class="tbl" style="margin-top:12px"><table>{''.join(rows)}</table></div>
<p class="muted" style="font-size:12px">Sykealueet (Karvonen, max {profile.max_hr}, lepo {a.resting_hr_used:.0f}): {zones_html}</p></section>

<section class="card"><h2>HRV ({chart_days} vrk)</h2>{line_chart([("HRV", series("hrv_ms"), "--c1")], start, as_of, "ms", a.hrv.band_ms)}</section>
<section class="card"><h2>Leposyke</h2>{line_chart([("Leposyke", series("resting_hr"), "--c3")], start, as_of, "bpm")}</section>
<section class="card"><h2>Uni</h2>{line_chart([("Uni", series("sleep_hours"), "--c2")], start, as_of, "h", (profile.sleep_target_h - 0.75, profile.sleep_target_h + 1))}</section>
<section class="card"><h2>Harjoituskuorma</h2>{bar_chart(a.load.daily, start, as_of)}
{line_chart([("ATL (7 vrk)", a.load.atl_series, "--c3"), ("CTL (42 vrk)", a.load.ctl_series, "--c1")], start, as_of, "kuorma")}</section>

<section class="card"><h2>Ohjelman muutosloki</h2><ul>{log_html}</ul></section>
<p class="muted" style="font-size:12px">Valmiusarvio on heuristinen apuväline, ei lääketieteellinen arvio. Sairauden oireissa älä harjoittele.</p>
</main></body></html>"""
