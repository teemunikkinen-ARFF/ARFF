import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from garmin_coach.adapt import adapt_plan, diff_plans
from garmin_coach.analysis import (
    Analysis, HrvState, LoadState, Readiness, RhrState, SleepState, analyze, analyze_hrv, analyze_rhr,
    compute_readiness, ewma_series, hr_zones, trimp,
)
from garmin_coach.coach import update_plan
from garmin_coach.models import Activity, DailyMetrics, Goal, Profile
from garmin_coach.planner import PHASE_DELOAD, PHASE_RACE, build_reference_plan, monday, week_plan
from garmin_coach.sources import csv_import, demo, garmin_connect
from garmin_coach.storage import Store

ZONES = hr_zones(190, 50)
TODAY = date(2026, 11, 11)  # keskiviikko


def metrics_series(n, end, hrv=60.0, rhr=50.0, sleep=7.8, tail=None):
    out = []
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        wiggle = 1 + 0.05 * math.sin(i)
        out.append(DailyMetrics(day=d, hrv_ms=hrv * wiggle, resting_hr=rhr + (i % 3) - 1, sleep_hours=sleep))
    if tail:
        for m, overrides in zip(out[-len(tail):], tail):
            for k, v in overrides.items():
                setattr(m, k, v)
    return out


def neutral_analysis(level="green", score=90, hrv_status="normal", acwr=1.0, confidence="high"):
    return Analysis(
        as_of=TODAY, hrv=HrvState(status=hrv_status), rhr=RhrState(status="normal"),
        sleep=SleepState(status="good"), load=LoadState(atl=30, ctl=30, tsb=0, acwr=acwr),
        readiness=Readiness(score=score, level=level, reasons=[], confidence=confidence), resting_hr_used=50,
    )


# --- analyysi -------------------------------------------------------------------------

def test_trimp_increases_with_intensity():
    assert trimp(60, 170, 190, 50) > trimp(60, 130, 190, 50) > 0
    assert trimp(60, 40, 190, 50) == 0


def test_ewma_converges_to_constant_load():
    start = date(2026, 1, 1)
    daily = {start + timedelta(days=i): 50.0 for i in range(200)}
    s = ewma_series(daily, start, start + timedelta(days=199), 7)
    assert abs(s[start + timedelta(days=199)] - 50) < 0.01


def test_hrv_insufficient_without_baseline():
    assert analyze_hrv(metrics_series(10, TODAY), TODAY).status == "insufficient"


def test_hrv_detects_low_trend_and_acute_drop():
    low = [{"hrv_ms": 42.0}] * 7
    st = analyze_hrv(metrics_series(70, TODAY, tail=low), TODAY)
    assert st.status == "low"
    assert st.acute_drop
    assert analyze_hrv(metrics_series(70, TODAY), TODAY).status == "normal"


def test_rhr_elevated():
    st = analyze_rhr(metrics_series(40, TODAY, tail=[{"resting_hr": 60.0}]), TODAY)
    assert st.status == "elevated" and st.delta >= 7


def test_readiness_levels():
    good = compute_readiness(HrvState("normal"), RhrState("normal"), SleepState("good"), LoadState(30, 30, 0, 1.0))
    assert good.level == "green" and good.score == 100
    bad = compute_readiness(
        HrvState("low", 45, 45, 60, (55, 65), acute_drop=True), RhrState("elevated", 60, 50, 10),
        SleepState("very_short", 5.0, 6.0, 8.0), LoadState(60, 30, -30, 2.0),
    )
    assert bad.level == "red"
    assert len(bad.reasons) >= 4


def test_readiness_low_confidence_without_data():
    r = compute_readiness(HrvState("insufficient"), RhrState("insufficient"), SleepState("insufficient"), LoadState(0, 0, 0, None))
    assert r.confidence == "low"


def test_analyze_end_to_end_with_demo():
    m, a = demo.generate(TODAY, 90)
    an = analyze(m, a, Profile(max_hr=188), TODAY)
    assert an.readiness.level in ("yellow", "red")  # demossa tarkoituksella heikentynyt loppu
    assert an.load.ctl > 0


# --- perusohjelma -------------------------------------------------------------------

def half_goal(**kw):
    base = dict(kind="half", start_date=date(2026, 9, 7), event_date=date(2027, 3, 14), days_per_week=4, weekly_km=25)
    base.update(kw)
    return Goal(**base)


def test_weekly_volume_progression_is_capped():
    _, weeks = week_plan(half_goal())
    prev = None
    for phase, vol in weeks:
        if phase == PHASE_DELOAD or phase == PHASE_RACE:
            continue
        if prev:
            assert vol <= prev * 1.08 + 1e-6
        prev = vol


@pytest.mark.parametrize("days", [3, 4, 5, 6])
@pytest.mark.parametrize("kind", ["5k", "10k", "half", "marathon"])
def test_reference_plan_structure(kind, days):
    g = half_goal(kind=kind, days_per_week=days, long_run_weekday=5)
    plan = build_reference_plan(g, ZONES)
    by_day = {s.day: s for s in plan}
    assert by_day[g.event_date].kind == "race"
    assert by_day[g.event_date - timedelta(days=1)].kind == "rest"
    assert max(s.day for s in plan) >= g.event_date
    for wk in {monday(s.day) for s in plan}:
        week = [s for s in plan if monday(s.day) == wk]
        if week[0].phase == PHASE_RACE:
            continue
        assert sum(1 for s in week if not s.is_rest) == days
        longs = [s for s in week if s.kind == "long"]
        assert len(longs) == 1 and longs[0].day.weekday() == 5
        for s in week:
            if s.kind in ("easy", "strides"):
                assert s.distance_km <= 0.8 * longs[0].distance_km + 0.5
        q = sorted(s.day for s in week if s.is_quality)
        for a, b in zip(q, q[1:]):
            assert (b - a).days > 1, "laatuharjoitukset peräkkäin"


def test_fitness_goal_without_event():
    g = Goal(kind="fitness", start_date=date(2026, 1, 5), days_per_week=3, weekly_km=15)
    plan = build_reference_plan(g, ZONES, until=TODAY + timedelta(weeks=8))
    assert plan[-1].day >= TODAY + timedelta(weeks=7)
    assert not any(s.kind == "race" for s in plan)


def test_goal_validation():
    with pytest.raises(ValueError):
        Goal(kind="half", start_date=TODAY).validate()
    with pytest.raises(ValueError):
        half_goal(days_per_week=7).validate()


# --- mukautus -------------------------------------------------------------------------

def build_phase_plan():
    g = half_goal(start_date=date(2026, 6, 1))
    return g, build_reference_plan(g, ZONES)


def _with_today(plan, kind):
    """Varmistaa testin kannalta, että tänään on haluttu sessio."""
    from garmin_coach.planner import make_session
    out = []
    for s in plan:
        if s.day == TODAY:
            s = make_session(TODAY, kind, 8.0, s.phase, s.week_index, half_goal(), ZONES)
        if s.day in (TODAY + timedelta(days=1), TODAY + timedelta(days=2), TODAY - timedelta(days=1)):
            s = make_session(s.day, "rest", 0, s.phase, s.week_index, half_goal(), ZONES)
        out.append(s)
    return out


def test_red_readiness_rests_and_relocates_quality():
    g, plan = build_phase_plan()
    plan = _with_today(plan, "intervals")
    new, notes, overrides = adapt_plan(plan, g, ZONES, TODAY, neutral_analysis("red", 30), [], [], {})
    by = {s.day: s for s in new}
    assert by[TODAY].kind == "rest" and by[TODAY].adjusted
    moved = [s for s in new if s.adjusted and s.adjusted.startswith("Siirretty")]
    assert len(moved) == 1 and moved[0].day > TODAY + timedelta(days=1)
    assert moved[0].day.isoformat() in overrides
    # Seuraavana päivänä siirto pysyy voimassa, vaikka valmius olisi normaali.
    nxt = TODAY + timedelta(days=1)
    new2, _, ov2 = adapt_plan(plan, g, ZONES, nxt, neutral_analysis(), [], [], overrides)
    assert {s.day: s for s in new2}[moved[0].day].kind == "intervals"
    assert ov2


def test_yellow_readiness_softens_quality():
    g, plan = build_phase_plan()
    plan = _with_today(plan, "tempo")
    new, notes, _ = adapt_plan(plan, g, ZONES, TODAY, neutral_analysis("yellow", 60), [], [], {})
    t = {s.day: s for s in new}[TODAY]
    assert t.kind == "easy" and t.distance_km < 8.0


def test_low_confidence_does_not_change_today():
    g, plan = build_phase_plan()
    plan = _with_today(plan, "tempo")
    new, _, _ = adapt_plan(plan, g, ZONES, TODAY, neutral_analysis("red", 30, confidence="low"), [], [], {})
    assert {s.day: s for s in new}[TODAY].kind == "tempo"


def test_low_compliance_caps_future_volume():
    g, plan = build_phase_plan()
    # Juostu vain 5 km / vko kahdella edellisellä viikolla
    acts = [Activity(f"a{i}", monday(TODAY) - timedelta(days=d), "running", 30, 5.0) for i, d in enumerate((3, 10))]
    new, notes, _ = adapt_plan(plan, g, ZONES, TODAY, neutral_analysis(), acts, [], {})
    ref_week = sum(s.distance_km for s in plan if monday(s.day) == monday(TODAY) + timedelta(weeks=1))
    new_week = sum(s.distance_km for s in new if monday(s.day) == monday(TODAY) + timedelta(weeks=1))
    assert new_week < ref_week
    assert any("jäi alle" in n for n in notes)


def test_high_acwr_softens_next_days():
    g, plan = build_phase_plan()
    plan = _with_today(plan, "intervals")
    new, notes, _ = adapt_plan(plan, g, ZONES, TODAY, neutral_analysis(acwr=1.8), [], [], {})
    assert {s.day: s for s in new}[TODAY].kind == "easy"
    assert any("ACWR" in n for n in notes)


def test_race_day_never_changed():
    g = half_goal()
    plan = build_reference_plan(g, ZONES)
    new, notes, _ = adapt_plan(plan, g, ZONES, g.event_date, neutral_analysis("red", 10), [], [], {})
    assert {s.day: s for s in new}[g.event_date].kind == "race"


def test_diff_plans_reports_changes():
    _, plan = build_phase_plan()
    s = next(x for x in plan if x.day >= TODAY and x.kind == "long")
    changed = [replace(x, kind="rest", distance_km=0, title="Lepo", adjusted="testi") if x is s else x for x in plan]
    out = diff_plans(plan, changed)
    assert len(out) == 1 and "Pitkä lenkki" in out[0] and "testi" in out[0]


# --- tallennus ja orkestrointi -------------------------------------------------------

def test_store_upsert_keeps_existing_values():
    st = Store(":memory:")
    st.upsert_metrics([DailyMetrics(TODAY, resting_hr=50, hrv_ms=60)])
    st.upsert_metrics([DailyMetrics(TODAY, sleep_hours=7.5)])
    m = st.metrics()[0]
    assert (m.resting_hr, m.hrv_ms, m.sleep_hours) == (50, 60, 7.5)


def test_update_plan_logs_changes_after_new_data():
    st = Store(":memory:")
    st.save_profile(Profile(max_hr=188))
    st.save_goal(half_goal(start_date=date(2026, 6, 1)))
    m, a = demo.generate(TODAY, 90, stressed_days=0)
    st.upsert_metrics(m)
    st.upsert_activities(a)
    first = update_plan(st, TODAY)
    assert first.regenerated and first.sessions[0].day == TODAY

    again = update_plan(st, TODAY)
    assert not again.regenerated and again.changes == []

    # Huono yö tänään -> ohjelma muuttuu ja muutos kirjataan lokiin
    st.upsert_metrics([DailyMetrics(TODAY, resting_hr=62, hrv_ms=30, sleep_hours=4.0)])
    third = update_plan(st, TODAY)
    today = third.sessions[0]
    if first.sessions[0].kind not in ("rest",):
        assert today.kind in ("rest", "easy") and third.changes
        assert any(TODAY.isoformat() == d for d, _ in st.log_entries())

    # Tavoitteen muutos -> ohjelma luodaan uudelleen
    st.save_goal(half_goal(start_date=date(2026, 6, 1), days_per_week=5))
    assert update_plan(st, TODAY).regenerated


# --- datalähteet ----------------------------------------------------------------------

def test_garmin_parsers():
    assert garmin_connect.parse_hrv({"hrvSummary": {"lastNightAvg": 58, "weeklyAvg": 60}}) == 58.0
    assert garmin_connect.parse_hrv({}) is None
    sl = garmin_connect.parse_sleep({"dailySleepDTO": {
        "sleepTimeSeconds": 27000, "deepSleepSeconds": 5400, "remSleepSeconds": 6300,
        "sleepScores": {"overall": {"value": 82}}}})
    assert sl == {"sleep_hours": 7.5, "deep_sleep_hours": 1.5, "rem_sleep_hours": 1.75, "sleep_score": 82.0}
    assert garmin_connect.parse_sleep(None)["sleep_hours"] is None
    assert garmin_connect.parse_resting_hr({"restingHeartRate": 47}) == 47.0
    assert garmin_connect.parse_resting_hr(
        {"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 49}]}}}) == 49.0
    act = garmin_connect.parse_activity({
        "activityId": 123, "startTimeLocal": "2026-11-10 07:15:00", "activityType": {"typeKey": "running"},
        "duration": 2700.0, "distance": 8200.0, "averageHR": 142.0, "activityTrainingLoad": 88.5})
    assert act.day == date(2026, 11, 10) and act.is_run and act.distance_km == 8.2 and act.load == 88.5
    assert garmin_connect.parse_activity({"activityId": 1}) is None


def test_csv_import(tmp_path):
    d = tmp_path / "daily.csv"
    d.write_text("date,resting_hr,hrv_ms,sleep_hours,sleep_score\n2026-11-01,48,62,\"7,5\",81\n2026-11-02,,,,\n")
    rows = csv_import.read_daily(d)
    assert rows[0].sleep_hours == 7.5 and rows[1].resting_hr is None
    a = tmp_path / "acts.csv"
    a.write_text("date,sport,duration_min,distance_km,avg_hr,load,id\n2026-11-01,running,45,8.2,142,,\n")
    acts = csv_import.read_activities(a)
    assert acts[0].distance_km == 8.2 and acts[0].activity_id.startswith("csv-")
    assert csv_import.read_activities(a)[0].activity_id == acts[0].activity_id  # vakaa tunniste -> ei duplikaatteja
    bad = tmp_path / "bad.csv"
    bad.write_text("date,resting_hr\nnot-a-date,50\n")
    with pytest.raises(ValueError):
        csv_import.read_daily(bad)


def test_cli_end_to_end(tmp_path):
    from garmin_coach.cli import main
    db = str(tmp_path / "t.db")
    out = tmp_path / "r.html"
    assert main(["--db", db, "profile", "--max-hr", "188"]) == 0
    assert main(["--db", db, "goal", "--type", "10k", "--date", "2027-02-01", "--start", "2026-09-01"]) == 0
    assert main(["--db", db, "sync", "demo", "--days", "90", "--end", TODAY.isoformat()]) == 0
    assert main(["--db", db, "plan", "--as-of", TODAY.isoformat()]) == 0
    assert main(["--db", db, "report", "--as-of", TODAY.isoformat(), "-o", str(out)]) == 0
    html = out.read_text()
    assert "<svg" in html and "Ohjelman muutosloki" in html
    assert main(["--db", db, "goal", "--type", "half"]) == 2  # päivämäärä puuttuu


class FakeGarmin:
    def get_heart_rates(self, d):
        return {"restingHeartRate": 47}

    def get_hrv_data(self, d):
        return {"hrvSummary": {"lastNightAvg": 61}}

    def get_sleep_data(self, d):
        raise RuntimeError("palvelin ei vastannut")

    def get_activities_by_date(self, s, e):
        return [{"activityId": 9, "startTimeLocal": "2026-11-10 07:00:00", "activityType": {"typeKey": "running"},
                 "duration": 1800, "distance": 5000, "averageHR": 140}]


def test_garmin_check_reports_values_and_errors():
    lines = "\n".join(garmin_connect.check(FakeGarmin(), TODAY))
    assert "47.0" in lines and "61.0" in lines
    assert "VIRHE RuntimeError" in lines  # yksittäinen virhe ei kaada tarkistusta
    assert "1 haettu, 1 tulkittu" in lines


def test_garmin_fetch_survives_partial_failures():
    m, a = garmin_connect.fetch(FakeGarmin(), TODAY - timedelta(days=1), TODAY, log=lambda *_: None)
    assert len(m) == 2 and m[0].resting_hr == 47 and m[0].hrv_ms == 61 and m[0].sleep_hours is None
    assert len(a) == 1


def test_heavy_previous_day_softens_today():
    g, plan = build_phase_plan()
    plan = _with_today(plan, "tempo")
    an = neutral_analysis()
    an.load.daily[TODAY - timedelta(days=1)] = 250.0  # ctl = 30 -> 250 >= 3 * 30
    new, notes, _ = adapt_plan(plan, g, ZONES, TODAY, an, [], [], {})
    t = {s.day: s for s in new}[TODAY]
    assert t.kind == "easy" and "Eilen raskas" in t.adjusted
    # tavallinen päivä ei laukaise sääntöä
    an.load.daily[TODAY - timedelta(days=1)] = 60.0
    new, _, _ = adapt_plan(plan, g, ZONES, TODAY, an, [], [], {})
    assert {s.day: s for s in new}[TODAY].kind == "tempo"


def test_activity_override_survives_resync():
    st = Store(":memory:")
    a = Activity("garmin-1", TODAY, "hyrox", 75, None, 150, 90.0)
    st.upsert_activities([a])
    st.set_activity_override("garmin-1", sport="smoke_diving", load=300.0)
    st.upsert_activities([a])  # uusi synkronointi kirjoittaa alkuperäisen takaisin
    got = st.activities()[0]
    assert got.sport == "smoke_diving" and got.load == 300.0 and not got.is_run


# --- pyöräily + voima ---------------------------------------------------------------------
from garmin_coach import cycling  # noqa: E402
from garmin_coach.adapt import session_done  # noqa: E402


def bike_goal(**kw):
    base = dict(kind="cycling", start_date=date(2026, 11, 9), days_per_week=3, strength_days=2,
                weekly_hours=4.0, long_run_weekday=6, ftp=255)
    base.update(kw)
    return Goal(**base)


@pytest.mark.parametrize("bikes,strength", [(3, 0), (3, 2), (3, 3), (4, 2), (5, 1)])
@pytest.mark.parametrize("long_day", [4, 5, 6])
def test_cycling_week_structure(bikes, strength, long_day):
    g = bike_goal(days_per_week=bikes, strength_days=strength, long_run_weekday=long_day)
    plan = build_reference_plan(g, ZONES, until=date(2027, 2, 14))
    assert len({s.day for s in plan}) == len(plan), "kaksi sessiota samalle päivälle"
    for wk in sorted({monday(s.day) for s in plan}):
        week = sorted((s for s in plan if monday(s.day) == wk), key=lambda s: s.day)
        assert len(week) == 7
        assert sum(1 for s in week if s.sport == "bike") == bikes
        assert sum(1 for s in week if s.sport == "strength") == strength
        assert any(s.kind == "rest" for s in week)
        longs = [s for s in week if s.kind == "long"]
        assert len(longs) == 1 and longs[0].day.weekday() == long_day
        q = sorted(s.day for s in week if s.is_quality)
        for a, b in zip(q, q[1:]):
            assert (b - a).days > 1
        by = {s.day: s for s in plan}
        for s in week:
            nxt = by.get(s.day + timedelta(days=1))
            if s.sport == "strength" and nxt and (nxt.is_quality or nxt.kind == "long"):
                assert "ylävartalo" in s.title, "alavartalovoima ennen laatu-/pitkää ajoa"
            if s.sport == "bike":
                assert s.distance_km == 0 and s.duration_min >= cycling.MIN_BIKE_MIN


def test_cycling_volume_progression_deload_and_test_week():
    g = bike_goal()
    _, weeks = cycling.week_plan(g, until=date(2027, 3, 1))
    cap = min(max(4.0 * 1.5, 4.0 + 2), 12.0)
    for i, w in enumerate(weeks):
        assert w.hours <= cap + 1e-6
        if w.deload:
            assert w.hours < weeks[i - 1].hours * 0.8 or i == 0
    assert [w.phase for w in weeks[:12]][:4] == ["Peruskausi"] * 3 + ["Kevyt viikko"]
    assert weeks[11].test and weeks[11].phase == "Testiviikko"
    plan = build_reference_plan(g, ZONES, until=date(2027, 3, 1))
    test_week = [s for s in plan if s.week_index == 11]
    assert any(s.kind == "test" and "profile --ftp" in s.details for s in test_week)
    assert not any(s.is_quality and s.kind != "test" for s in test_week)
    assert any(s.kind == "vo2" for s in plan), "huippukaudella VO2max"
    assert any(s.kind == "threshold" for s in plan)


def test_cycling_watts_follow_ftp():
    p250 = build_reference_plan(bike_goal(ftp=250), ZONES, until=date(2026, 12, 6))
    p300 = build_reference_plan(bike_goal(ftp=300), ZONES, until=date(2026, 12, 6))
    ss250 = next(s for s in p250 if s.kind == "sweetspot")
    ss300 = next(s for s in p300 if s.kind == "sweetspot")
    assert f"{cycling.r5(250 * 0.88)}–{cycling.r5(250 * 0.94)} W" in ss250.details
    assert f"{cycling.r5(300 * 0.88)}–{cycling.r5(300 * 0.94)} W" in ss300.details
    no_ftp = next(s for s in build_reference_plan(bike_goal(ftp=None), ZONES, until=date(2026, 12, 6)) if s.kind == "sweetspot")
    assert "% FTP" in no_ftp.details and " W (" not in no_ftp.details


def test_cycling_goal_validation():
    for kw in ({"days_per_week": 6}, {"days_per_week": 5, "strength_days": 2}, {"strength_days": 4},
               {"event_date": date(2027, 6, 1)}, {"weekly_hours": 1.0}, {"max_weekly_hours": 2.0}):
        with pytest.raises(ValueError):
            bike_goal(**kw).validate()
    bike_goal().validate()


def _bike_plan_and_day(kind):
    g = bike_goal()
    plan = build_reference_plan(g, ZONES, until=date(2027, 1, 31))
    day = next(s.day for s in plan if s.kind == kind and s.day > g.start_date)
    return g, plan, day


def test_bike_red_readiness_rests_and_yellow_softens():
    g, plan, day = _bike_plan_and_day("sweetspot")
    new, notes, _ = adapt_plan(plan, g, ZONES, day, neutral_analysis("red", 30), [], [], {})
    assert {s.day: s for s in new}[day].kind == "rest" and notes
    new, notes, _ = adapt_plan(plan, g, ZONES, day, neutral_analysis("yellow", 60), [], [], {})
    t = {s.day: s for s in new}[day]
    orig = {s.day: s for s in plan}[day]
    assert t.kind == "easy" and t.sport == "bike" and t.duration_min < orig.duration_min and "W" in t.details


def test_strength_is_lightened_not_dropped_on_yellow():
    g = bike_goal()
    plan = build_reference_plan(g, ZONES, until=date(2027, 1, 31))
    day = next(s.day for s in plan if s.sport == "strength" and s.day > g.start_date)
    new, notes, _ = adapt_plan(plan, g, ZONES, day, neutral_analysis("yellow", 60), [], [], {})
    t = {s.day: s for s in new}[day]
    assert t.kind == "strength" and "Kevyt versio" in t.details and t.adjusted


def test_bike_compliance_uses_bike_minutes_and_ignores_strength():
    g = bike_goal(start_date=date(2026, 10, 5))
    plan = build_reference_plan(g, ZONES, until=date(2027, 1, 31))
    as_of = date(2026, 11, 11)
    w0 = monday(as_of)
    # Ajettu vain 60 min kahdella viikolla, mutta voimaharjoituksia tehty paljon: ei saa laskea toteumaa
    acts = [Activity("r", w0 - timedelta(days=5), "Ride", 60, None, 140)] + [
        Activity(f"s{i}", w0 - timedelta(days=i), "WeightTraining", 70, None, 100) for i in range(1, 8)]
    new, notes, _ = adapt_plan(plan, g, ZONES, as_of, neutral_analysis(), acts, [], {})
    assert any("pyöräilyaika" in n for n in notes)
    nxt = monday(as_of) + timedelta(weeks=1)
    ref_min = sum(s.duration_min for s in plan if monday(s.day) == nxt and s.sport == "bike")
    new_min = sum(s.duration_min for s in new if monday(s.day) == nxt and s.sport == "bike")
    assert new_min < ref_min
    strength_ref = [(s.day, s.duration_min) for s in plan if monday(s.day) == nxt and s.sport == "strength"]
    strength_new = [(s.day, s.duration_min) for s in new if monday(s.day) == nxt and s.sport == "strength"]
    assert strength_ref == strength_new
    # Riittävä pyöräilymäärä: ei rajoitusta
    planned = sum(x.duration_min for x in plan if x.sport == "bike" and w0 - timedelta(days=14) <= x.day < w0)
    ok = [Activity(f"r{i}", w0 - timedelta(days=i), "Ride", planned * 1.1 / 4, None, 140) for i in range(1, 5)]
    _, notes2, _ = adapt_plan(plan, g, ZONES, as_of, neutral_analysis(), ok, [], {})
    assert not any("pyöräilyaika" in n for n in notes2)


def test_sport_classification_and_session_done():
    bikes = ["Ride", "VirtualRide", "cycling", "indoor_cycling", "road_biking", "mountain_biking", "gravel_cycling"]
    assert all(Activity("x", TODAY, s, 10).is_bike for s in bikes)
    assert all(Activity("x", TODAY, s, 10).is_strength for s in ["WeightTraining", "strength_training"])
    for s in ["HighIntensityIntervalTraining", "hyrox", "smoke_diving", "running", "hiit"]:
        a = Activity("x", TODAY, s, 10)
        assert not a.is_bike and not a.is_strength
    g, plan, day = _bike_plan_and_day("sweetspot")
    s = {x.day: x for x in plan}[day]
    assert session_done(s, [Activity("a", day, "Ride", s.duration_min * 0.8)]) is True
    assert session_done(s, [Activity("a", day, "WeightTraining", 90)]) is False
    assert session_done(s, [Activity("a", day, "HighIntensityIntervalTraining", 90)]) is False


def test_store_migrates_old_plan_table(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""CREATE TABLE plan_sessions (day TEXT PRIMARY KEY, kind TEXT NOT NULL, distance_km REAL NOT NULL,
        title TEXT NOT NULL, details TEXT NOT NULL, phase TEXT NOT NULL, week_index INTEGER NOT NULL, adjusted TEXT);
        INSERT INTO plan_sessions VALUES ('2026-11-12','easy',8.0,'Peruslenkki','8 km','Peruskausi',1,NULL);""")
    c.commit(); c.close()
    st = Store(db)
    s = st.plan()[0]
    assert s.sport == "run" and s.distance_km == 8.0 and s.duration_min == 0.0 and s.daytype == ""
    st.replace_plan_from(date(2026, 11, 12), [replace(s, sport="bike", duration_min=60.0, distance_km=0.0)])
    assert st.plan()[0].sport == "bike"


def test_cycling_end_to_end_with_smoke_dive_correction(tmp_path):
    from garmin_coach.cli import main
    db, out = str(tmp_path / "b.db"), tmp_path / "b.html"
    assert main(["--db", db, "profile", "--max-hr", "190", "--ftp", "255"]) == 0
    assert main(["--db", db, "goal", "--type", "cycling", "--start", "2026-11-09", "--hours", "4", "--strength-days", "2"]) == 0
    assert main(["--db", db, "sync", "demo", "--days", "60", "--end", TODAY.isoformat()]) == 0
    st = Store(db)
    st.upsert_activities([Activity("hiit1", TODAY - timedelta(days=1), "HighIntensityIntervalTraining", 21, None, 119)])
    st.close()
    assert main(["--db", db, "tag", "hiit1", "--sport", "smoke_diving", "--intensity", "moderate"]) == 0
    assert main(["--db", db, "plan", "--as-of", TODAY.isoformat()]) == 0
    assert main(["--db", db, "report", "--as-of", TODAY.isoformat(), "-o", str(out)]) == 0
    html = out.read_text()
    assert "Pyöräilykunto" in html and "FTP 255 W" in html and "Voima:" in html
    assert main(["--db", db, "goal", "--type", "cycling", "--days", "6"]) == 2


# --- työvuorokalenteri ----------------------------------------------------------------------
from garmin_coach.shifts import ShiftCalendar, day_type, post_shift_days  # noqa: E402
from garmin_coach.cycling import assign_rides  # noqa: E402


def test_shift_calendar_projection_and_day_types():
    cal = ShiftCalendar([date(2026, 11, 10)], cycle=4)
    sh = cal.expand(date(2026, 11, 30))
    assert sh == {date(2026, 11, d) for d in (10, 14, 18, 22, 26, 30)}
    assert [day_type(date(2026, 11, d), sh) for d in (10, 11, 12, 13, 14)] == ["shift", "post", "free", "free", "shift"]
    # Tunnettu myöhempi vuoro ohittaa vanhan rytmin ennusteen
    cal2 = ShiftCalendar([date(2026, 11, 10), date(2026, 11, 15)], cycle=4)
    assert date(2026, 11, 14) not in cal2.expand(date(2026, 11, 30)) and date(2026, 11, 19) in cal2.expand(date(2026, 11, 30))
    assert post_shift_days({date(2026, 11, 10)}) == {date(2026, 11, 11)}


def shift_goal(**kw):
    sh = ShiftCalendar([date(2026, 11, 10)], 4).expand(date(2027, 3, 1))
    return bike_goal(shift_days=frozenset(sh), **kw)


@pytest.mark.parametrize("bikes,strength", [(3, 2), (3, 0), (4, 1), (3, 3)])
def test_shift_plan_respects_work_rhythm(bikes, strength):
    g = shift_goal(days_per_week=bikes, strength_days=strength)
    plan = build_reference_plan(g, ZONES, until=date(2027, 2, 14))
    by = {s.day: s for s in plan}
    assert len(by) == len(plan)
    for s in plan:
        dt = day_type(s.day, g.shift_days)
        assert s.daytype == dt
        if s.sport == "bike":
            assert dt == "free", "ajo vuoro- tai vuoron jälkeiselle päivälle"
        if dt == "post":
            assert s.kind == "rest"
        if dt == "shift" and s.kind != "rest":
            assert s.sport == "strength" and s.duration_min <= 45 and "valinnainen" in s.title
        nxt = by.get(s.day + timedelta(days=1))
        if s.is_quality and nxt:
            assert not nxt.is_quality, "laatu laatun perään"
        if s.kind == "long" and nxt:
            assert not nxt.is_quality, "laatuajo pitkän ajon perään"
        if s.sport == "strength" and "alavartalo" in s.title and nxt:
            assert not (nxt.is_quality or nxt.kind == "long" or "alavartalo" in nxt.title)
    for wk in sorted({monday(s.day) for s in plan}):
        week = [s for s in plan if monday(s.day) == wk]
        rides = [s for s in week if s.sport == "bike"]
        assert 1 <= len(rides) <= bikes
        assert sum(1 for s in week if s.sport == "strength") <= strength
        hours = sum(s.duration_min for s in rides) / 60
        w = next(x for x in cycling.week_plan(g, until=date(2027, 2, 14))[1] if x.index == week[0].week_index)
        assert 0.7 * w.hours <= hours <= 1.45 * w.hours, (wk, hours, w.hours)


def test_assign_rides_never_puts_hard_before_hard():
    d = lambda n: date(2026, 11, n)  # noqa: E731
    kinds = {"q1": "sweetspot", "long": "long", "q2": "tempo"}
    res = assign_rides(kinds, [d(9), d(10), d(12)], {d(13)})
    assert res is not None
    days = sorted(res.items(), key=lambda kv: kv[1])
    for (na, da), (nb, db) in zip(days, days[1:]):
        if (db - da).days == 1:
            assert na != "long" and not (kinds[na] != "long" and kinds[nb] != "long")
    # kaksi laatuajoa ja vain kaksi peräkkäistä vapaata päivää: ei mahdollinen
    assert assign_rides({"q1": "tempo", "q2": "threshold"}, [d(9), d(10)], set()) is None


def test_shift_plan_drops_lowest_priority_ride_when_few_free_days():
    # Vuoro joka kolmas päivä: vapaita päiviä vain ~2,3 viikossa
    sh = frozenset(date(2026, 11, 9) + timedelta(days=3 * i) for i in range(40))  # vuoro, palautus, 1 vapaa
    g = bike_goal(shift_days=sh, days_per_week=4, strength_days=0)
    plan = build_reference_plan(g, ZONES, until=date(2026, 12, 6))
    for wk in sorted({monday(s.day) for s in plan}):
        week = [s for s in plan if monday(s.day) == wk]
        rides = [s for s in week if s.sport == "bike"]
        assert all(s.daytype == "free" for s in rides)
        assert len(rides) < 4 and any("Vapaita päiviä vain" in (s.adjusted or "") for s in rides)


def test_post_shift_data_is_excluded_from_analysis():
    m = metrics_series(70, TODAY)
    post_days = {TODAY - timedelta(days=i) for i in (0, 4, 8)}
    for x in m:
        if x.day in post_days:  # vuoroyö: huono uni, matala HRV, korkea leposyke
            x.sleep_hours, x.hrv_ms, x.resting_hr = 3.5, 30.0, 62.0
    plain = analyze(m, [], Profile(max_hr=188), TODAY)
    fair = analyze(m, [], Profile(max_hr=188), TODAY, exclude_days=post_days)
    assert plain.readiness.level != "green"
    assert fair.hrv.status != "low" and fair.sleep.debt7_h < plain.sleep.debt7_h
    assert fair.readiness.reasons[0].startswith("Työvuoron jälkeinen päivä")


def test_relocation_never_lands_on_shift_or_post_day():
    g = shift_goal()
    plan = build_reference_plan(g, ZONES, until=date(2027, 1, 31))
    day = next(s.day for s in plan if s.kind in ("sweetspot", "tempo") and s.day > date(2026, 11, 20))
    new, notes, ov = adapt_plan(plan, g, ZONES, day, neutral_analysis("red", 30), [], [], {})
    moved = [s for s in new if s.adjusted and s.adjusted.startswith("Siirretty")]
    assert all(s.daytype == "free" for s in moved)


def test_cli_shifts_and_cycling_plan_with_calendar(tmp_path, capsys):
    from garmin_coach.cli import main
    db = str(tmp_path / "s.db")
    assert main(["--db", db, "profile", "--max-hr", "190", "--ftp", "240"]) == 0
    assert main(["--db", db, "goal", "--type", "cycling", "--start", "2026-11-09"]) == 0
    assert main(["--db", db, "shifts", "set", "--first", "2026-11-10", "--count", "3"]) == 0
    assert main(["--db", db, "shifts", "add", "2026-11-23"]) == 0
    assert main(["--db", db, "shifts", "remove", "2026-11-18"]) == 0
    assert main(["--db", db, "shifts", "show", "--as-of", "2026-11-11", "--weeks", "2"]) == 0
    out = capsys.readouterr().out
    assert "TYÖVUORO" in out and "vuoron jälkeen" in out
    assert Store(db).shift_calendar().dates == [date(2026, 11, 10), date(2026, 11, 14), date(2026, 11, 23)]
    assert main(["--db", db, "sync", "demo", "--days", "40", "--end", "2026-11-10"]) == 0
    assert main(["--db", db, "plan", "--as-of", "2026-11-11", "--show", "10"]) == 0
    out = capsys.readouterr().out
    assert "Vuoron jälkeinen päivä" in out and "Työvuoro" in out
    assert main(["--db", db, "shifts", "add"]) == 2
