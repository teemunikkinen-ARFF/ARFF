"""Työvuorokalenteri: 24 h vuoro + vapaat (perusrytmi 24 h töissä, 72 h vapaata = 4 päivän sykli).

Päivätyypit:
* shift: vuoro alkaa tänä päivänä (valinnainen lyhyt voimaharjoitus, ei ajoja)
* post:  vuoron jälkeinen päivä (palautuminen; yön uni ja HRV eivät ole vertailukelpoisia)
* free:  vapaa päivä (tärkeät ajot ja voima)

Kalenteri on lista tunnetuista vuoroista. Viimeisen tunnetun vuoron jälkeen rytmiä jatketaan oletussyklillä (4 pv),
joten vuorolistaa tarvitsee päivittää vain, kun aikataulu poikkeaa rytmistä.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

DEFAULT_CYCLE = 4


@dataclass
class ShiftCalendar:
    dates: list[date] = field(default_factory=list)
    cycle: int = DEFAULT_CYCLE

    def expand(self, until: date) -> set[date]:
        """Tunnetut vuorot + rytmin jatko viimeisestä tunnetusta vuorosta `until`-päivään."""
        out = set(self.dates)
        if self.dates and self.cycle > 0:
            d = max(self.dates) + timedelta(days=self.cycle)
            while d <= until:
                out.add(d)
                d += timedelta(days=self.cycle)
        return out

    def to_json(self) -> dict:
        return {"dates": sorted(d.isoformat() for d in self.dates), "cycle": self.cycle}

    @classmethod
    def from_json(cls, d: dict) -> "ShiftCalendar":
        return cls(dates=sorted(date.fromisoformat(x) for x in d.get("dates", [])), cycle=d.get("cycle", DEFAULT_CYCLE))


def day_type(d: date, shifts) -> str:
    if d in shifts:
        return "shift"
    if d - timedelta(days=1) in shifts:
        return "post"
    return "free"


def post_shift_days(shifts) -> set[date]:
    return {d + timedelta(days=1) for d in shifts if d + timedelta(days=1) not in shifts}
