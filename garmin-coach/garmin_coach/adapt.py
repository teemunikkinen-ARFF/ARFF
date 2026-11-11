"""Ohjelman mukauttaminen datan perusteella.

Tasot, lyhyimmästä pisimpään vaikutukseen:
1. Päivän valmius (HRV, leposyke, uni, kuorma) → tämän päivän sessio kevennetään tai vaihdetaan lepoon,
   ja väliin jäänyt laatuharjoitus siirretään viikon myöhempään vapaaseen päivään, jos se mahtuu turvallisesti.
1a. Raskas edellinen päivä (kuorma ≥ 3 × normaali) → tämän päivän teho/pitkä sessio kevenee.
2. HRV:n 7 vrk trendi alle normaalin → kuluvan viikon loput tehoharjoitukset muutetaan peruslenkeiksi/-ajoiksi.
3. Akuutti kuorma liian korkea (ACWR > 1,5) → seuraavat 3 päivää kevyesti ja lyhyemmin.
4. Toteuma: jos kahden edellisen viikon päälajin määrä (juoksu km / pyöräily min) jäi alle 80 % suunnitellusta,
   tulevien viikkojen volyymi nostetaan toteutuneesta tasosta maltillisesti (+8 %/vko).

Ylisuorittaminen ei kasvata ohjelmaa: volyymia ei koskaan nosteta perusohjelman yläpuolelle.
Voimaharjoittelu kevenee (2 sarjaa/liike) aina kun sessiota mukautetaan; sitä ei lasketa päälajin toteumaan.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, timedelta

from . import cycling
from .analysis import Analysis
from .models import WEEKDAYS_FI, Activity, Goal, Session
from .planner import PHASE_RACE, WEEKLY_INCREASE, describe, monday, r05

COMPLIANCE_THRESHOLD = 0.8
HEAVY_DAY_FACTOR = 3.0  # edellisen päivän kuorma vs. CTL (keskimääräinen päiväkuorma)
HEAVY_DAY_MIN_CTL = 20.0
MIN_SESSION_KM = 3.0


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS_FI[d.weekday()]} {d.day}.{d.month}."


def primary_sport(goal: Goal) -> str:
    return "bike" if goal.kind == "cycling" else "run"


def fmt_amount(x: float, sport: str) -> str:
    return f"{x / 60:.1f} h" if sport == "bike" else f"{x:.0f} km"


def _rebuild(s: Session, kind: str, amount: float, goal: Goal, zones, reason: str) -> Session:
    """Rakentaa session uudelleen toisena lajina/määränä. `amount` on km (juoksu) tai min (pyörä/voima)."""
    prev = f"{s.adjusted}; " if s.adjusted else ""
    if kind == "rest":
        title, details = describe("rest", 0, s.phase, goal.kind, zones) if s.sport == "run" else (
            "Lepo", "Lepo tai kevyt liikkuvuus/venyttely.")
        return replace(s, kind="rest", distance_km=0.0, duration_min=0.0, title=title, details=details,
                       adjusted=prev + reason)
    if s.sport == "run":
        km = r05(max(MIN_SESSION_KM, amount))
        title, details = describe(kind, km, s.phase, goal.kind, zones)
        return replace(s, kind=kind, distance_km=km, title=title, details=details, adjusted=prev + reason)
    if s.sport == "strength":
        mins = cycling.r5(max(30, amount))
        emphasis = "lower" if "alavartalo" in s.title.lower() else "upper"
        title, details = cycling.describe_strength(mins, emphasis, light=True)
        return replace(s, kind="strength", duration_min=float(mins), title=title, details=details,
                       adjusted=prev + reason)
    mins = cycling.r5(max(cycling.MIN_BIKE_MIN, amount))
    title, details = cycling.describe_bike(kind, mins, s.phase, goal.ftp, zones)
    return replace(s, kind=kind, duration_min=float(mins), title=title, details=details, adjusted=prev + reason)


def _is_fixed(s: Session) -> bool:
    """Pyörän tehoajoja ei lyhennetä volyymirajoituksessa: intensiteetti kehittää FTP:tä, volyymi karsitaan
    peruskestävyys- ja pitkistä ajoista."""
    return s.sport == "bike" and s.is_quality


def _scale(s: Session, factor: float, goal: Goal, zones, reason: str) -> Session:
    if s.kind in ("rest", "race") or s.sport == "strength" or _is_fixed(s) or factor >= 0.999:
        return s
    floor = MIN_SESSION_KM if s.sport == "run" else cycling.MIN_BIKE_MIN
    new = max(floor, s.amount * factor)
    new = r05(new) if s.sport == "run" else cycling.r5(new)
    if new >= s.amount:
        return s
    return _rebuild(s, s.kind, new, goal, zones, reason)


def _soften(s: Session, goal: Goal, zones, reason: str, quality_to_easy: bool = True) -> Session:
    """Kevennä sessiota: laatu → peruskestävyys 80 %, muut 80 %, voima → kevyt versio."""
    if s.sport == "strength":
        return _rebuild(s, "strength", s.amount, goal, zones, reason)
    kind = "easy" if (s.is_quality or s.kind == "strides") and quality_to_easy else s.kind
    return _rebuild(s, kind, s.amount * 0.8, goal, zones, reason)


def run_km(activities: list[Activity], start: date, end: date) -> float:
    return sum(a.distance_km or 0.0 for a in activities if a.is_run and start <= a.day <= end)


def bike_minutes(activities: list[Activity], start: date, end: date) -> float:
    return sum(a.duration_min for a in activities if a.is_bike and start <= a.day <= end)


def strength_minutes(activities: list[Activity], start: date, end: date) -> float:
    return sum(a.duration_min for a in activities if a.is_strength and start <= a.day <= end)


def done_amount(activities: list[Activity], start: date, end: date, sport: str) -> float:
    return bike_minutes(activities, start, end) if sport == "bike" else run_km(activities, start, end)


def session_done(s: Session, activities: list[Activity]) -> bool | None:
    """True/False menneille harjoituspäiville, None lepopäiville."""
    if s.kind == "rest":
        return None
    if s.sport == "bike":
        got = bike_minutes(activities, s.day, s.day)
    elif s.sport == "strength":
        got = strength_minutes(activities, s.day, s.day)
    else:
        got = run_km(activities, s.day, s.day)
    return got >= 0.5 * s.amount if s.amount else got > 0


def adapt_plan(
    reference: list[Session],
    goal: Goal,
    zones: dict[str, tuple[int, int]],
    as_of: date,
    analysis: Analysis,
    activities: list[Activity],
    stored_past: list[Session],
    overrides: dict[str, dict],
) -> tuple[list[Session], list[str], dict[str, dict]]:
    """Palauttaa (mukautettu ohjelma, huomiot, päivitetyt siirrot)."""
    notes: list[str] = []
    plan = {s.day: s for s in reference}
    week0 = monday(as_of)
    sport = primary_sport(goal)
    in_sport = lambda s: s.sport == sport and s.kind != "race"  # noqa: E731

    # Aiemmin tehdyt siirrot (esim. väliin jäänyt intervalli siirretty torstaille) pysyvät voimassa.
    # Tämän päivän siirto lasketaan aina uudelleen tuoreimmalla datalla.
    overrides = {k: v for k, v in overrides.items() if v["from"] != as_of.isoformat() and date.fromisoformat(k) >= as_of}
    for day_s, ov in overrides.items():
        d = date.fromisoformat(day_s)
        if d in plan:
            sd = dict(ov["session"])
            sd["day"] = d
            plan[d] = Session(**sd)

    # 4. Toteuma kahdelta edelliseltä viikolta
    w_start, w_end = week0 - timedelta(days=14), week0 - timedelta(days=1)
    if goal.start_date <= w_start:
        past_by_day = {s.day: s for s in stored_past}
        planned = 0.0
        d = w_start
        while d <= w_end:
            s = past_by_day.get(d) or plan.get(d)
            if s and in_sport(s):
                planned += s.amount
            d += timedelta(days=1)
        actual = done_amount(activities, w_start, w_end, sport)
        if planned > 0 and actual < COMPLIANCE_THRESHOLD * planned:
            anchor = max(actual / 2, 0.4 * planned / 2)
            reason = f"toteuma {fmt_amount(actual, sport)}/{fmt_amount(planned, sport)} kahdella viikolla"
            ref_peak = max(_week_volumes(reference, sport).values())
            new_peak = 0.0
            for k in range(0, 60):
                ws = week0 + timedelta(weeks=k)
                days = [ws + timedelta(days=i) for i in range(7)]
                week = [plan[x] for x in days if x in plan]
                if not week:
                    break
                vol = sum(s.amount for s in week if in_sport(s))
                fixed = sum(s.amount for s in week if in_sport(s) and _is_fixed(s))
                cap = anchor * WEEKLY_INCREASE ** (k + 1)
                if week[0].phase != PHASE_RACE and vol > cap:
                    f = cap / vol if not fixed else max(0.5, (cap - fixed) / max(vol - fixed, 1e-9))
                    for s in week:
                        if s.day >= as_of:
                            plan[s.day] = _scale(s, f, goal, zones, f"Volyymi rajattu ({reason})")
                new_peak = max(new_peak, sum(plan[x].amount for x in days if x in plan and in_sport(plan[x])))
            notes.append(
                f"Edellisten kahden viikon {'pyöräilyaika' if sport == 'bike' else 'juoksumäärä'} "
                f"{fmt_amount(actual, sport)} jäi alle suunnitellun {fmt_amount(planned, sport)}. "
                "Tulevien viikkojen volyymi nousee toteutuneesta tasosta enintään 8 % viikossa."
            )
            if goal.event_date and new_peak < 0.8 * ref_peak:
                notes.append(
                    f"Huippuviikon volyymi jää nyt n. {fmt_amount(new_peak, sport)}:iin "
                    f"(alkuperäinen {fmt_amount(ref_peak, sport)}). "
                    "Harkitse tavoitteen keventämistä tai kilpailun siirtämistä."
                )

    # 3. Akuutti kuorma
    acwr = analysis.load.acwr
    if acwr is not None and acwr > 1.5:
        reason = f"Kuorma noussut liian nopeasti (ACWR {acwr:.2f})"
        changed = False
        for i in range(3):
            d = as_of + timedelta(days=i)
            s = plan.get(d)
            if s and s.kind not in ("rest", "race"):
                plan[d] = _soften(s, goal, zones, reason)
                changed = True
        if changed:
            notes.append(f"{reason}: seuraavat 3 päivää kevyesti ja n. 20 % lyhyempinä (voima kevyenä).")

    # 2. HRV-trendi
    if analysis.hrv.status == "low":
        reason = "HRV:n 7 vrk trendi alle normaalialueen"
        changed = False
        for i in range(7 - as_of.weekday()):
            d = as_of + timedelta(days=i)
            s = plan.get(d)
            if s and s.is_quality and s.phase != PHASE_RACE:
                plan[d] = _rebuild(s, "easy", s.amount, goal, zones, reason)
                changed = True
        if changed:
            notes.append(f"{reason}: tämän viikon tehoharjoitukset vaihdettu peruskestävyydeksi.")
        else:
            notes.append(f"{reason}: pidä loppuviikko kevyenä, vaikka ohjelmassa ei ole tehoharjoituksia.")

    # 1a. Raskas edellinen päivä (esim. työharjoitus + treeni samana päivänä). Aamun HRV/leposyke ei välttämättä
    # ehdi näyttää rasitusta, joten kevennetään tämän päivän tehoa/pitkää sessiota ennakoiden.
    y_load = analysis.load.daily.get(as_of - timedelta(days=1), 0.0)
    if y_load >= HEAVY_DAY_FACTOR * max(analysis.load.ctl, HEAVY_DAY_MIN_CTL):
        t = plan.get(as_of)
        reason = f"Eilen raskas päivä (kuorma {y_load:.0f}, normaali {analysis.load.ctl:.0f}/pv)"
        if t and t.kind not in ("rest", "race"):
            plan[as_of] = _soften(t, goal, zones, reason)
            if t.sport == "strength":
                notes.append(f"{reason}: tämän päivän voimaharjoitus kevyeksi (2 sarjaa/liike).")
            elif t.is_quality:
                notes.append(f"{reason}: tämän päivän {t.title.lower()} vaihdettu kevyeksi peruskestävyydeksi.")
            elif t.kind == "long":
                notes.append(f"{reason}: pitkää sessiota lyhennetty 20 %.")
            else:
                notes.append(f"{reason}: tämän päivän {t.title.lower()} lyhennetty 20 %, pidä se tosi rauhallisena.")

    # 1. Päivän valmius
    today = plan.get(as_of)
    rd = analysis.readiness
    if today and today.kind not in ("rest", "race") and rd.confidence != "low":
        if rd.level == "red":
            reason = f"Valmius {rd.score}/100 (punainen)"
            plan[as_of] = _rebuild(today, "rest", 0, goal, zones, reason)
            msg = f"{reason}: tämän päivän {today.title.lower()} vaihdettu lepoon."
            if today.is_quality:
                target = _find_slot(plan, as_of, goal)
                if target:
                    moved = replace(today, day=target, adjusted=f"Siirretty päivältä {fmt_day(as_of)} (huono valmius)")
                    plan[target] = moved
                    sd = asdict(moved)
                    sd["day"] = target.isoformat()
                    overrides[target.isoformat()] = {"from": as_of.isoformat(), "session": sd}
                    msg += f" {today.title} siirretty päivälle {fmt_day(target)}."
                else:
                    msg += " Laatuharjoitus jätetään tältä viikolta väliin."
            notes.append(msg)
        elif rd.level == "yellow" and today.adjusted is None:  # ei kevennetä kahteen kertaan
            reason = f"Valmius {rd.score}/100 (keltainen)"
            if today.sport == "strength":
                plan[as_of] = _soften(today, goal, zones, reason)
                notes.append(f"{reason}: voimaharjoitus kevyeksi (2 sarjaa/liike).")
            elif today.is_quality:
                plan[as_of] = _soften(today, goal, zones, reason)
                notes.append(f"{reason}: tämän päivän {today.title.lower()} vaihdettu kevyeksi peruskestävyydeksi.")
            elif today.kind == "long":
                plan[as_of] = _soften(today, goal, zones, reason)
                notes.append(f"{reason}: pitkää sessiota lyhennetty 20 %.")

    # Lisätään ennakkovaroitus ennen kisaa, mutta kisapäivää ei koskaan muuteta automaattisesti.
    if today and today.kind == "race" and rd.level != "green":
        notes.append("Kilpailupäivä ja valmius heikentynyt – aloita varovasti ja kuuntele kehoa.")

    return [plan[d] for d in sorted(plan)], notes, overrides


def _find_slot(plan: dict[date, Session], as_of: date, goal: Goal) -> date | None:
    """Etsii kuluvalta viikolta lepopäivän, jonka naapuripäivinä ei ole kovaa harjoitusta."""
    week_end = monday(as_of) + timedelta(days=6)
    d = as_of + timedelta(days=2)  # vähintään yksi palautumispäivä välissä
    while d <= week_end:
        s = plan.get(d)
        if s and s.kind == "rest":
            neighbours = [plan.get(d - timedelta(days=1)), plan.get(d + timedelta(days=1))]
            if all(n is None or (n.kind not in ("long", "race") and not n.is_quality) for n in neighbours):
                return d
        d += timedelta(days=1)
    return None


def _week_volumes(sessions: list[Session], sport: str) -> dict[date, float]:
    out: dict[date, float] = {}
    for s in sessions:
        if s.kind != "race" and s.phase != PHASE_RACE and s.sport == sport:
            out[monday(s.day)] = out.get(monday(s.day), 0.0) + s.amount
    return out or {date.min: 0.0}


def diff_plans(old: list[Session], new: list[Session]) -> list[str]:
    """Ihmisluettava lista muutoksista vanhan ja uuden ohjelman välillä (vain päivät, jotka ovat molemmissa)."""
    old_by = {s.day: s for s in old}
    out = []
    for n in new:
        o = old_by.get(n.day)
        if not o:
            continue
        tol = 0.5 if n.sport == "run" and o.sport == "run" else 5.0
        if o.kind != n.kind or o.sport != n.sport or abs(o.amount - n.amount) >= tol:
            what = lambda s: s.title if s.kind == "rest" else f"{s.title} {s.label}"  # noqa: E731
            why = f" ({n.adjusted})" if n.adjusted else ""
            out.append(f"{fmt_day(n.day)}: {what(o)} → {what(n)}{why}")
    return out
