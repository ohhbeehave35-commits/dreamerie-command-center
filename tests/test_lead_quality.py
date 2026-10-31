"""PARITY8E: duplicate management and contact scoring for the Leads table.

The real crm functions run against a fake Airtable (only the HTTP layer is
faked). Annabelle's tools run through annabelle_gaps.run with the same
draft-then-yes rule as every other write; the merge removes records, so it also
needs the typed word MERGE.
"""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import annabelle_gaps as ag  # noqa: E402
from app import crm, lead_quality  # noqa: E402
from tests.fake_airtable import FakeBase, install  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    ag.reset_state()
    crm._table_id_cache = None
    yield
    ag.reset_state()
    crm._table_id_cache = None


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setattr(crm, "AIRTABLE_TOKEN", "pat-fake")
    monkeypatch.setattr(crm, "AIRTABLE_BASE_ID", "appFAKE")
    b = FakeBase()
    b.seed_table("Leads", ["Name", "Phone", "Email", "Business", "Request", "Status",
                           "Source", "Notes", "SMS Opt-In"])
    install(monkeypatch, crm, b)
    return b


def put(base, **fields):
    base._rid += 1
    rid = f"rec{base._rid:06d}"
    base.tables["leads"]["records"][rid] = dict(fields)
    return rid


def draft_id(text):
    return text.split("DRAFT ", 1)[1].split(":", 1)[0]


# ------------------------------------------------------------ pure scoring --

def test_score_rises_with_how_reachable_and_how_far_along_the_lead_is():
    bare = lead_quality.score_lead({"Name": "Pat"})
    reachable = lead_quality.score_lead({"Name": "Pat", "Email": "p@x.com", "Phone": "(555) 010-2000"})
    booked = lead_quality.score_lead({"Name": "Pat", "Email": "p@x.com", "Phone": "5550102000",
                                      "Request": "Wedding cake", "Status": "Scheduled"})
    lost = lead_quality.score_lead({"Name": "Pat", "Email": "p@x.com", "Phone": "5550102000",
                                    "Request": "Wedding cake", "Status": "Lost"})
    assert bare["score"] < reachable["score"] < booked["score"]
    assert lost["score"] < booked["score"]
    assert lost["score"] <= 10 and lost["label"] == "cold"
    assert 0 <= bare["score"] and booked["score"] <= 100
    assert booked["label"] == "hot" and bare["label"] == "cold"
    assert any("email" in r for r in reachable["reasons"])


def test_a_malformed_email_earns_no_email_points():
    good = lead_quality.score_lead({"Name": "A", "Email": "a@x.com"})
    bad = lead_quality.score_lead({"Name": "A", "Email": "not-an-email"})
    assert bad["score"] < good["score"]


# ------------------------------------------------------- pure duplicate find --

def test_duplicates_match_on_email_phone_digits_or_name_and_business():
    leads = [
        {"id": "a", "name": "Pat Jones", "email": "Pat@X.com", "phone": "", "business": "Bear Arms"},
        {"id": "b", "name": "P. Jones", "email": "pat@x.com ", "phone": "", "business": ""},
        {"id": "c", "name": "Sam", "email": "", "phone": "(555) 010-2000", "business": ""},
        {"id": "d", "name": "Samuel", "email": "", "phone": "+1 555-010-2000", "business": ""},
        {"id": "e", "name": "Lee Park", "email": "", "phone": "", "business": "Peptides"},
        {"id": "f", "name": "lee  park", "email": "", "phone": "", "business": "Peptides"},
        {"id": "g", "name": "Lee Park", "email": "", "phone": "", "business": "Bear Arms"},
        {"id": "h", "name": "Solo", "email": "solo@x.com", "phone": "", "business": ""},
    ]
    groups = lead_quality.find_duplicate_groups(leads)
    sets = sorted(sorted(g["ids"]) for g in groups)
    assert sets == [["a", "b"], ["c", "d"], ["e", "f"]]
    reasons = {tuple(sorted(g["ids"])): g["reason"] for g in groups}
    assert "email" in reasons[("a", "b")]
    assert "phone" in reasons[("c", "d")]
    assert "name" in reasons[("e", "f")]


def test_a_blank_name_or_blank_contact_never_makes_a_duplicate():
    leads = [{"id": "a", "name": "", "email": "", "phone": "", "business": ""},
             {"id": "b", "name": "", "email": "", "phone": "", "business": ""},
             {"id": "c", "name": "N", "email": "", "phone": "12", "business": ""},
             {"id": "d", "name": "M", "email": "", "phone": "12", "business": ""}]
    assert lead_quality.find_duplicate_groups(leads) == []


def test_merge_keeps_the_keeper_fills_blanks_and_joins_notes():
    keep = {"Name": "Pat Jones", "Email": "", "Phone": "5550102000", "Notes": "First call"}
    other = {"Name": "P. Jones", "Email": "pat@x.com", "Phone": "9999999999", "Notes": "Wants cake"}
    merged = lead_quality.merge_fields(keep, [other])
    assert merged["Name"] == "Pat Jones"
    assert merged["Phone"] == "5550102000"
    assert merged["Email"] == "pat@x.com"
    assert "First call" in merged["Notes"] and "Wants cake" in merged["Notes"]


# ---------------------------------------------------------- crm, fake Airtable --

def test_crm_lists_scores_and_finds_duplicates_against_the_table(base):
    put(base, Name="Pat Jones", Email="pat@x.com", Phone="5550102000", Request="Cake", Status="New")
    put(base, Name="Pat J", Email="PAT@x.com", Status="New")
    put(base, Name="Solo", Email="solo@x.com", Status="New")
    scored = crm.score_leads()
    assert len(scored) == 3 and scored[0]["score"] >= scored[-1]["score"]
    assert all("label" in s and "reasons" in s for s in scored)
    dups = crm.find_duplicate_leads()
    assert len(dups) == 1 and len(dups[0]["leads"]) == 2


def test_merge_leads_updates_the_keeper_and_removes_the_others(base):
    a = put(base, Name="Pat Jones", Phone="5550102000", Notes="First call")
    b = put(base, Name="P Jones", Email="pat@x.com", Phone="5550102000", Notes="Wants cake")
    r = crm.merge_leads(a, [b])
    assert r["ok"] is True and r["removed"] == 1
    recs = base.tables["leads"]["records"]
    assert b not in recs and a in recs
    assert recs[a]["Email"] == "pat@x.com"
    assert "Wants cake" in recs[a]["Notes"] and "First call" in recs[a]["Notes"]


def test_merge_refuses_a_missing_keeper_or_keeper_listed_as_duplicate(base):
    a = put(base, Name="Pat")
    assert crm.merge_leads("recNOPE", [a])["ok"] is False
    assert crm.merge_leads(a, [a])["ok"] is False
    assert a in base.tables["leads"]["records"]


def test_merge_does_not_delete_when_the_keeper_save_fails(base, monkeypatch):
    a = put(base, Name="Pat")
    b = put(base, Name="Pat", Email="e@x.com")
    base.tables["leads"]["fields"].remove("Email")  # keeper update will 422
    r = crm.merge_leads(a, [b])
    assert r["ok"] is False
    assert b in base.tables["leads"]["records"]


# -------------------------------------------------------- Annabelle's tools --

def test_tools_exist_by_the_control_names():
    assert {"find_duplicate_leads", "score_leads", "merge_duplicate_leads"} <= ag.TOOL_NAMES
    assert "merge_duplicate_leads" in ag.DRAFT_TOOLS
    assert "find_duplicate_leads" not in ag.DRAFT_TOOLS and "score_leads" not in ag.DRAFT_TOOLS


def test_reads_answer_at_once_and_change_nothing(base):
    a = put(base, Name="Pat Jones", Email="pat@x.com", Phone="5550102000", Request="Cake")
    b = put(base, Name="Pat J", Email="pat@x.com")
    before = {k: dict(v) for k, v in base.tables["leads"]["records"].items()}
    out = ag.run("find_duplicate_leads", {}, "c", "t1")
    assert "Pat Jones" in out and a in out and b in out
    out2 = ag.run("score_leads", {}, "c", "t1")
    assert "Pat Jones" in out2 and "hot" in out2 or "warm" in out2 or "cold" in out2
    assert {k: dict(v) for k, v in base.tables["leads"]["records"].items()} == before


def test_merge_is_a_draft_that_needs_the_next_turn_and_the_typed_word(base):
    a = put(base, Name="Pat Jones", Phone="5550102000")
    b = put(base, Name="P Jones", Email="pat@x.com", Phone="5550102000")
    d = ag.run("merge_duplicate_leads", {"keep_id": a, "remove_ids": [b]}, "c", "turn1")
    assert d.startswith("DRAFT") and "MERGE" in d
    assert b in base.tables["leads"]["records"]
    assert "same turn" in ag.run("confirm_action", {"draft_id": draft_id(d), "typed": "MERGE"}, "c", "turn1")
    assert "MERGE" in ag.run("confirm_action", {"draft_id": draft_id(d)}, "c", "turn2")
    assert "MERGE" in ag.run("confirm_action", {"draft_id": draft_id(d), "typed": "yes"}, "c", "turn2")
    assert b in base.tables["leads"]["records"]
    out = ag.run("confirm_action", {"draft_id": draft_id(d), "typed": "MERGE"}, "c", "turn3")
    assert "merged" in out.lower()
    assert b not in base.tables["leads"]["records"]
    assert base.tables["leads"]["records"][a]["Email"] == "pat@x.com"
    assert "No held draft" in ag.run("confirm_action", {"draft_id": draft_id(d), "typed": "MERGE"}, "c", "turn4")


def test_merge_draft_validates_before_holding(base):
    a = put(base, Name="Pat")
    assert "keeper" in ag.run("merge_duplicate_leads", {"remove_ids": [a]}, "c", "t1").lower()
    assert "duplicate" in ag.run("merge_duplicate_leads", {"keep_id": a, "remove_ids": []}, "c", "t1").lower()
    assert "Nothing was changed" in ag.run("merge_duplicate_leads", {"keep_id": "recNOPE", "remove_ids": [a]}, "c", "t1")
    assert "Nothing was changed" in ag.run("merge_duplicate_leads", {"keep_id": a, "remove_ids": [a]}, "c", "t1")
    assert a in base.tables["leads"]["records"]


def test_the_crm_not_connected_answers_plainly(monkeypatch):
    monkeypatch.setattr(crm, "AIRTABLE_TOKEN", "")
    assert "isn't connected" in ag.run("find_duplicate_leads", {}, "c", "t1")
    assert "isn't connected" in ag.run("score_leads", {}, "c", "t1")


# ------------------------------------------------------------ owner JSON API --

def test_api_routes_are_owner_only_and_merge_needs_the_typed_word():
    from app import main
    assert "/api/leads/duplicates" in main._OWNER_ONLY_EXACT_PATHS
    assert "/api/leads/merge" in main._OWNER_ONLY_EXACT_PATHS
    assert "/api/leads/scores" in main._OWNER_ONLY_EXACT_PATHS


def test_merge_api_refuses_without_the_typed_word(base):
    from app import main
    a = put(base, Name="Pat")
    b = put(base, Name="Pat", Email="e@x.com")
    resp = main.leads_merge(main.LeadMergeRequest(keep_id=a, remove_ids=[b], typed="yes"))
    assert resp.status_code == 400
    assert b in base.tables["leads"]["records"]
    ok = main.leads_merge(main.LeadMergeRequest(keep_id=a, remove_ids=[b], typed="MERGE"))
    assert ok.status_code == 200 and b not in base.tables["leads"]["records"]
