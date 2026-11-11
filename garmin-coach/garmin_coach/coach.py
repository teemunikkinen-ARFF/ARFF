"""Orkestrointi: analyysi → perusohjelma → mukautus → tallennus ja muutosloki."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta

from .adapt import adapt_plan, diff_plans
from .analysis import Analysis, analyze, hr_zones
from .models import Session
from .planner import build_reference_plan
from .storage import Store

HISTORY_DAYS = 130
FITNESS_HORIZON_WEEKS = 8


@dataclass
class UpdateResult:
    analysis: Analysis
    sessions: list[Session]  # tallennettu ohjelma päivästä as_of eteenpäin
    changes: list[str]
    notes: list[str]
    regenerated: bool  # True = tavoite uusi/muuttunut, ohjelma luotiin alusta


def run_analysis(store: Store, as_of: date) -> Analysis:
    metrics = store.metrics(as_of - timedelta(days=HISTORY_DAYS), as_of)
    activities = store.activities(as_of - timedelta(days=HISTORY_DAYS), as_of)
    return analyze(metrics, activities, store.profile(), as_of)


def update_plan(store: Store, as_of: date) -> UpdateResult:
    goal = store.goal()
    if goal is None:
        raise ValueError("Tavoitetta ei ole asetettu. Aja ensin: garmin-coach goal ...")
    profile = store.profile()
    analysis = run_analysis(store, as_of)
    zones = hr_zones(profile.max_hr, analysis.resting_hr_used)

    until = goal.event_date or (as_of + timedelta(weeks=FITNESS_HORIZON_WEEKS))
    goal_eff = replace(goal, ftp=profile.ftp)  # FTP elää profiilissa; ohjelma rakennetaan aina tuoreella arvolla
    reference = build_reference_plan(goal_eff, zones, until)

    goal_sig = json.dumps(asdict(goal), default=str, sort_keys=True)
    regenerated = store.get_value("goal_sig") != goal_sig
    overrides = {} if regenerated else (store.get_value("overrides") or {})

    activities = store.activities(as_of - timedelta(days=HISTORY_DAYS), as_of)
    stored_past = store.plan(as_of - timedelta(days=21), as_of - timedelta(days=1))
    sessions, notes, overrides = adapt_plan(
        reference, goal_eff, zones, as_of, analysis, activities, stored_past, overrides
    )
    future = [s for s in sessions if s.day >= as_of]

    if regenerated:
        changes: list[str] = []
        store.log(as_of, f"Ohjelma luotu tavoitteelle {goal.kind} ({len(future)} päivää).")
        store.set_value("goal_sig", goal_sig)
    else:
        changes = diff_plans(store.plan(as_of), future)
        for c in changes:
            store.log(as_of, c)

    prev_notes = store.get_value("last_notes") or []
    for n in notes:
        if n not in prev_notes:
            store.log(as_of, n)
    store.set_value("last_notes", notes)
    store.set_value("overrides", overrides)
    store.replace_plan_from(as_of, future)
    return UpdateResult(analysis=analysis, sessions=future, changes=changes, notes=notes, regenerated=regenerated)
