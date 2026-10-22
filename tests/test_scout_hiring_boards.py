"""Job-board discovery: the right country's Indeed, and only real signal roles."""

import sys
from types import SimpleNamespace

import pytest

import harvey.agents.scout as scout_mod
from harvey.agents.scout import Scout


# ── which Indeed site to search ──

@pytest.mark.parametrize("geo, country", [
    ("Italy", "italy"),
    ("netherlands", "netherlands"),
    ("United States", "usa"),
    ("Europe", "usa"),   # not a country: jobspy's own default
    ("", "usa"),
])
def test_indeed_country(geo, country):
    pytest.importorskip("jobspy")
    assert scout_mod._indeed_country(geo) == country


def test_indeed_country_without_jobspy(monkeypatch):
    monkeypatch.setitem(sys.modules, "jobspy", None)
    monkeypatch.setitem(sys.modules, "jobspy.model", None)
    assert scout_mod._indeed_country("Italy") == "usa"


# ── only a posting for a signal role counts ──

class FakeFrame:
    def __init__(self, rows):
        self.rows = rows
        self.empty = not rows

    def to_dict(self, orient):
        return list(self.rows)


def board_scout(monkeypatch, rows):
    """A Scout whose job board, web lookups and DB are stand-ins."""
    calls = SimpleNamespace(search=None, companies=[], notes=[])

    def scrape_jobs(**kwargs):
        calls.search = kwargs
        return FakeFrame(rows)

    monkeypatch.setitem(sys.modules, "jobspy", SimpleNamespace(scrape_jobs=scrape_jobs))
    monkeypatch.setattr(scout_mod, "_indeed_country", lambda geo: f"indeed:{geo}")

    scout = Scout.__new__(Scout)
    scout.config = SimpleNamespace(icp=SimpleNamespace(
        hiring_signals=["AI Engineer", "ML Engineer"],
        titles=["CTO"],
        geography=["Italy"],
        industries=["SaaS"],
    ))
    scout._seen_domains = set()

    async def guess_domain(name):
        return name.lower().replace(" ", "") + ".com"

    async def company_exists(domain):
        return False

    async def scrape_info(domain):
        return {}

    async def ensure_company(**kw):
        calls.companies.append(kw["name"])
        return f"id-{kw['domain']}"

    async def update_signals(company_id, **kw):
        return None

    async def contacts(**kw):
        calls.notes.append(kw["signal_note"])
        return []

    scout._guess_domain = guess_domain
    scout._is_candidate_domain = lambda domain: bool(domain)
    scout._scrape_company_info = scrape_info
    scout._ensure_company = ensure_company
    scout._contacts_from_company = contacts
    scout.state = SimpleNamespace(
        company_exists=company_exists, update_company_signals=update_signals,
    )
    return scout, calls


@pytest.mark.asyncio
async def test_searches_the_geographys_indeed(monkeypatch):
    scout, calls = board_scout(monkeypatch, rows=[])
    await scout._prospect_via_hiring_boards()
    assert calls.search["location"] == "Italy"
    assert calls.search["country_indeed"] == "indeed:Italy"


@pytest.mark.asyncio
async def test_unrelated_postings_are_not_a_hiring_signal(monkeypatch):
    # Indeed answers "ai engineer" with whatever it has; a BU Director
    # posting must not become "saw you're hiring a BU Director".
    scout, calls = board_scout(monkeypatch, rows=[
        {"company": "Sandvik", "title": "BU Director"},
        {"company": "Emerson", "title": "Senior AI Engineer"},
        {"company": "Deltatre", "title": "Senior Project Manager"},
        {"company": "bit spa", "title": "Machine Learning Engineer / ML Engineer"},
    ])
    await scout._prospect_via_hiring_boards()
    assert calls.companies == ["Emerson", "bit spa"]
    assert all("Director" not in n and "Project Manager" not in n for n in calls.notes)
