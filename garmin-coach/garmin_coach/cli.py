"""Komentoriviliittymä."""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from .adapt import fmt_day
from .coach import run_analysis, update_plan
from .models import GOAL_KINDS, GOAL_LABELS, WEEKDAYS_FI, Goal, Profile
from .storage import DEFAULT_DB, Store

LEVEL_ICON = {"green": "VIHREÄ", "yellow": "KELTAINEN", "red": "PUNAINEN"}


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _weekday(s: str) -> int:
    s = s.lower()[:2]
    if s.isdigit():
        return int(s)
    en = ["mo", "tu", "we", "th", "fr", "sa", "su"]
    if s in WEEKDAYS_FI:
        return WEEKDAYS_FI.index(s)
    if s in en:
        return en.index(s)
    raise argparse.ArgumentTypeError("viikonpäivä: ma ti ke to pe la su")


def cmd_profile(st: Store, a) -> None:
    p = st.profile()
    if a.max_hr:
        p.max_hr = a.max_hr
    if a.age and not a.max_hr:
        p.max_hr = round(208 - 0.7 * a.age)  # Tanaka; mitattu maksimisyke on aina parempi
        print(f"Maksimisyke-arvio iästä: {p.max_hr} bpm (Tanaka). Mitattu arvo on tarkempi.")
    if a.resting_hr is not None:
        p.resting_hr = a.resting_hr or None
    if a.sleep_target:
        p.sleep_target_h = a.sleep_target
    st.save_profile(p)
    print(f"Profiili: maksimisyke {p.max_hr}, leposyke {p.resting_hr or 'mitattu perustaso'}, unitavoite {p.sleep_target_h} h")


def cmd_goal(st: Store, a) -> None:
    g = Goal(kind=a.type, start_date=a.start or date.today(), event_date=a.date, days_per_week=a.days,
             weekly_km=a.weekly_km, long_run_weekday=a.long_day, target_time=a.target_time)
    g.validate()
    st.save_goal(g)
    print(f"Tavoite tallennettu: {GOAL_LABELS[g.kind]}" + (f", kilpailu {g.event_date}" if g.event_date else ""))
    print("Ohjelma luodaan seuraavalla 'plan'-ajolla (tai heti, jos annoit --plan).")


def cmd_sync(st: Store, a) -> None:
    end = a.end or date.today()
    start = end - timedelta(days=a.days - 1)
    if a.source == "garmin":
        from .sources import garmin_connect
        print(f"Haetaan Garmin Connectista {start} – {end} ...")
        metrics, acts = garmin_connect.fetch(garmin_connect.connect(), start, end)
    elif a.source == "csv":
        from .sources import csv_import
        metrics = csv_import.read_daily(a.daily) if a.daily else []
        acts = csv_import.read_activities(a.activities) if a.activities else []
        if not metrics and not acts:
            raise SystemExit("Anna --daily ja/tai --activities.")
    else:
        from .sources import demo
        metrics, acts = demo.generate(end, a.days)
    print(f"Tallennettu {st.upsert_metrics(metrics)} päivää mittareita ja {st.upsert_activities(acts)} harjoitusta.")


def print_status(st: Store, as_of: date) -> None:
    an = run_analysis(st, as_of)
    r = an.readiness
    print(f"\nValmius {fmt_day(as_of)}: {r.score}/100 [{LEVEL_ICON[r.level]}]  (luotettavuus: {r.confidence})")
    for reason in r.reasons:
        print(f"  • {reason}")
    h, rh, s, ld = an.hrv, an.rhr, an.sleep, an.load
    if h.last_night_ms:
        band = f", normaali {h.band_ms[0]:.0f}–{h.band_ms[1]:.0f}" if h.band_ms else " (perustaso muodostuu)"
        print(f"  HRV      {h.last_night_ms:.0f} ms (7 vrk ka {h.avg7_ms:.0f}{band})")
    if rh.today:
        print(f"  Leposyke {rh.today:.0f} bpm" + (f" (perustaso {rh.baseline:.0f}, {rh.delta:+.0f})" if rh.delta is not None else ""))
    if s.last_night_h:
        print(f"  Uni      {s.last_night_h:.1f} h (7 vrk ka {s.avg7_h:.1f} h, vaje {s.debt7_h:.1f} h)")
    acwr = f"{ld.acwr:.2f}" if ld.acwr is not None else "–"
    print(f"  Kuorma   ATL {ld.atl:.0f} / CTL {ld.ctl:.0f}, ACWR {acwr}")


def cmd_plan(st: Store, a) -> None:
    as_of = a.as_of or date.today()
    res = update_plan(st, as_of)
    print_status(st, as_of)
    if res.regenerated:
        print("\nOhjelma luotu tavoitteen perusteella.")
    if res.notes:
        print("\nMukautukset:")
        for n in res.notes:
            print(f"  • {n}")
    if res.changes:
        print("\nMuutokset edelliseen versioon:")
        for c in res.changes:
            print(f"  ↻ {c}")
    elif not res.regenerated:
        print("\nEi muutoksia edelliseen versioon.")
    print(f"\nOhjelma {a.show} päivää:")
    for s in res.sessions[: a.show]:
        dist = "" if s.kind == "rest" else f"{s.distance_km:>5g} km"
        mark = " ↻" if s.adjusted else ""
        print(f"  {fmt_day(s.day):<9} {s.title:<22}{dist:>9}{mark}  {s.details if s.kind != 'rest' else ''}")


def cmd_status(st: Store, a) -> None:
    print_status(st, a.as_of or date.today())


def cmd_report(st: Store, a) -> Path:
    from .report import render
    as_of = a.as_of or date.today()
    notes = st.get_value("last_notes") or []
    an = run_analysis(st, as_of)
    html = render(
        as_of, an, st.goal(), st.profile(), st.metrics(as_of - timedelta(days=130), as_of),
        st.activities(as_of - timedelta(days=130), as_of), st.plan(as_of - timedelta(days=7), as_of - timedelta(days=1)),
        st.plan(as_of), notes, st.log_entries(15), a.chart_days,
    )
    out = Path(a.output)
    out.write_text(html, encoding="utf-8")
    print(f"Raportti: {out.resolve()}")
    return out


def cmd_daily(st: Store, a) -> None:
    """Päivittäinen ajo: synkronoi viimeiset päivät, päivitä ohjelma, kirjoita raportti."""
    a.days, a.end = a.sync_days, None
    cmd_sync(st, a)
    a.show = 7
    cmd_plan(st, a)
    cmd_report(st, a)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="garmin-coach", description="Garmin-datan analyysi ja mukautuva harjoitusohjelma.")
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"tietokanta (oletus {DEFAULT_DB}, tai GARMIN_COACH_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("profile", help="aseta maksimisyke, leposyke ja unitavoite")
    sp.add_argument("--max-hr", type=int)
    sp.add_argument("--age", type=int, help="arvioi maksimisyke iästä, jos mitattua ei ole")
    sp.add_argument("--resting-hr", type=int, help="kiinteä leposyke (0 = käytä mitattua)")
    sp.add_argument("--sleep-target", type=float, help="unitavoite tunteina")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("goal", help="aseta tavoite")
    sp.add_argument("--type", choices=GOAL_KINDS, required=True)
    sp.add_argument("--date", type=_date, help="kilpailupäivä YYYY-MM-DD")
    sp.add_argument("--start", type=_date, help="ohjelman aloituspäivä (oletus tänään)")
    sp.add_argument("--days", type=int, default=4, help="harjoituspäiviä viikossa (3–6)")
    sp.add_argument("--weekly-km", type=float, default=20.0, help="nykyinen juoksumäärä km/vko")
    sp.add_argument("--long-day", type=_weekday, default=6, help="pitkän lenkin päivä (ma–su)")
    sp.add_argument("--target-time", help="tavoiteaika, esim. 1:55:00 (tallennetaan tiedoksi)")
    sp.set_defaults(func=cmd_goal)

    def add_sync_args(sp):
        sp.add_argument("source", choices=("garmin", "csv", "demo"))
        sp.add_argument("--daily", help="CSV: päivittäiset mittarit")
        sp.add_argument("--activities", help="CSV: harjoitukset")

    sp = sub.add_parser("sync", help="tuo dataa (garmin | csv | demo)")
    add_sync_args(sp)
    sp.add_argument("--days", type=int, default=30, help="montako päivää taaksepäin")
    sp.add_argument("--end", type=_date)
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("status", help="näytä päivän valmius")
    sp.add_argument("--as-of", type=_date)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("plan", help="päivitä ohjelma uusimmalla datalla ja näytä muutokset")
    sp.add_argument("--as-of", type=_date)
    sp.add_argument("--show", type=int, default=14)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("report", help="kirjoita HTML-raportti")
    sp.add_argument("--as-of", type=_date)
    sp.add_argument("-o", "--output", default="garmin-coach-report.html")
    sp.add_argument("--chart-days", type=int, default=60)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("daily", help="sync + plan + report yhdellä komennolla (ajastukseen)")
    add_sync_args(sp)
    sp.add_argument("--sync-days", type=int, default=3)
    sp.add_argument("--as-of", type=_date)
    sp.add_argument("-o", "--output", default="garmin-coach-report.html")
    sp.add_argument("--chart-days", type=int, default=60)
    sp.set_defaults(func=cmd_daily)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    st = Store(args.db)
    try:
        args.func(st, args)
    except ValueError as e:
        print(f"Virhe: {e}", file=sys.stderr)
        return 2
    finally:
        st.close()
    return 0
