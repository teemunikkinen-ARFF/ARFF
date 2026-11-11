"""Tietomallit: päivittäiset terveysmittaukset, harjoitukset, profiili, tavoite ja harjoitus­ohjelman sessiot."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

RUN_SPORT_HINTS = ("run", "juoksu")

GOAL_KINDS = ("5k", "10k", "half", "marathon", "fitness")
GOAL_LABELS = {
    "5k": "5 km",
    "10k": "10 km",
    "half": "Puolimaraton",
    "marathon": "Maraton",
    "fitness": "Yleiskunto",
}
GOAL_DISTANCE_KM = {"5k": 5.0, "10k": 10.0, "half": 21.1, "marathon": 42.2, "fitness": 0.0}

WEEKDAYS_FI = ["ma", "ti", "ke", "to", "pe", "la", "su"]


@dataclass
class DailyMetrics:
    """Yhden vuorokauden palautumismittarit. Uni kirjataan heräämispäivälle (kuten Garmin tekee)."""

    day: date
    resting_hr: float | None = None
    hrv_ms: float | None = None  # yön keskiarvo (rMSSD, ms)
    sleep_hours: float | None = None
    sleep_score: float | None = None
    deep_sleep_hours: float | None = None
    rem_sleep_hours: float | None = None


@dataclass
class Activity:
    activity_id: str
    day: date
    sport: str
    duration_min: float
    distance_km: float | None = None
    avg_hr: float | None = None
    load: float | None = None  # Garminin oma training load, jos saatavilla

    @property
    def is_run(self) -> bool:
        s = self.sport.lower()
        return any(h in s for h in RUN_SPORT_HINTS)


@dataclass
class Profile:
    max_hr: int = 190
    resting_hr: int | None = None  # None = käytetään mitattua leposykkeen perustasoa
    sleep_target_h: float = 8.0


@dataclass
class Goal:
    kind: str
    start_date: date
    event_date: date | None = None
    days_per_week: int = 4
    weekly_km: float = 20.0  # nykyinen viikkovolyymi aloitushetkellä
    long_run_weekday: int = 6  # 0 = maanantai ... 6 = sunnuntai
    target_time: str | None = None

    def validate(self) -> None:
        if self.kind not in GOAL_KINDS:
            raise ValueError(f"Tuntematon tavoite '{self.kind}'. Vaihtoehdot: {', '.join(GOAL_KINDS)}")
        if not 3 <= self.days_per_week <= 6:
            raise ValueError("Harjoituspäiviä viikossa pitää olla 3–6.")
        if not 0 <= self.long_run_weekday <= 6:
            raise ValueError("Pitkän lenkin viikonpäivä 0–6 (ma–su).")
        if self.weekly_km < 0:
            raise ValueError("Viikkovolyymi ei voi olla negatiivinen.")
        if self.kind != "fitness" and self.event_date is None:
            raise ValueError("Kilpailutavoitteelle tarvitaan päivämäärä (--date).")
        if self.event_date is not None and self.event_date <= self.start_date:
            raise ValueError("Kilpailupäivän pitää olla aloituspäivän jälkeen.")


SESSION_LABELS = {
    "rest": "Lepo",
    "recovery": "Palauttava",
    "easy": "Peruslenkki",
    "strides": "Peruslenkki + vedot",
    "long": "Pitkä lenkki",
    "tempo": "Tempo",
    "intervals": "Intervallit",
    "sharpen": "Terävöitys",
    "race": "KILPAILU",
}
QUALITY_KINDS = {"tempo", "intervals", "sharpen"}


@dataclass
class Session:
    day: date
    kind: str
    distance_km: float
    title: str
    details: str
    phase: str
    week_index: int
    adjusted: str | None = None  # syy, jos sessiota on mukautettu datan perusteella

    @property
    def is_rest(self) -> bool:
        return self.kind == "rest"

    @property
    def is_quality(self) -> bool:
        return self.kind in QUALITY_KINDS
