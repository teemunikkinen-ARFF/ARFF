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
