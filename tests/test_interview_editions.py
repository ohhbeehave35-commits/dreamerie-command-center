"""The onboarding interview had a silent, invisible bug.

app/onboarding.py has supported two editions (established / startup) all
along: `questions_for(edition)` filters on it and three questions are
startup-only. But `edition` was never exposed as a client_interview tool
parameter and main.py never passed it, so every signature fell through to its
`edition="established"` default. Result: the startup edition was unreachable
dead code and a brand-new business got asked what its best customers say.

Nothing failed. Nothing logged. It just quietly ran one edition forever --
the same implemented-but-withheld shape as the 149-offered/68-dispatched gap.
These tests make the wiring itself the thing under test.

Second bug covered here: she wrote "saved that exactly as you said it" three
times in one live interview before ever calling record. The general
NEVER-NARRATE rule was already in the prompt and lost anyway, so the
prohibition now lives in the tool description she reads while interviewing.
"""

import pathlib
import re

import app.agents as A
import app.onboarding as O

TOOL = [t for t in A.DELEGATION_TOOLS if t["name"] == "client_interview"][0]
MAIN = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(
    encoding="utf-8", errors="replace")


def test_editions_actually_differ():
    """If both editions produced the same questions there'd be nothing to fix."""
    est = O.ids_for("established")
    start = O.ids_for("startup")
    assert est != start
    assert set(start) - set(est), "startup must have questions established does not"


def test_startup_only_questions_exist():
    startup_only = [q["id"] for q in O.QUESTIONS if q.get("edition") == "startup"]
    assert startup_only, "no startup-only questions -- the edition axis is pointless"


def test_edition_is_exposed_to_the_model():
    """THE BUG: the code supported startup, the model was never told it could ask."""
    props = TOOL["input_schema"]["properties"]
    assert "edition" in props, "startup edition is unreachable without this param"
    assert set(props["edition"]["enum"]) == set(O.EDITIONS)


def test_dispatch_threads_edition_rather_than_defaulting():
    """A param the model can send but the handler drops is the same bug again."""
    assert "_edition" in MAIN, "dispatch never reads the edition input"
    for call in ("next_question", "readiness", "build_persona"):
        hits = re.findall(rf"_ob\.{call}\(_client([^)]*)\)", MAIN)
        assert hits, f"no _ob.{call} call found in dispatch"
        for args in hits:
            assert "_edition" in args, (
                f"_ob.{call}() is called without _edition -- it will silently "
                f"fall back to the established default")


def test_unknown_edition_falls_back_safely():
    """A junk value must not crash or return an empty interview."""
    assert O.questions_for("nonsense") == O.questions_for("established")
    assert O.questions_for("") == O.questions_for("established")


def test_description_forbids_claiming_a_save_before_the_tool_returns():
    d = TOOL["description"]
    assert "NEVER write 'saved'" in d
    assert "ALREADY RETURNED" in d


def test_description_tells_her_to_establish_the_edition_first():
    d = TOOL["description"]
    assert "ESTABLISHED" in d and "STARTUP" in d
