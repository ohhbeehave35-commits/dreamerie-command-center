"""run_all() must actually run.

diagnostic.run_all() iterated a PROBES list that was defined in the flagship
but never ported here, so it raised NameError on the first probe. The
run_diagnostic tool is unguarded-adjacent code, so that NameError propagated
out of the chat handler and returned HTTP 500 -- every time Annabelle was asked
"is everything working?", which is precisely what a user asks when things feel
broken. Plain chat was fine, so it looked intermittent.

These would have failed against the shipped code.
"""

import pytest

from app import diagnostic


def test_probes_is_defined_and_non_empty():
    assert hasattr(diagnostic, "PROBES")
    assert isinstance(diagnostic.PROBES, list)
    assert len(diagnostic.PROBES) >= 1


def test_every_probe_is_callable():
    for p in diagnostic.PROBES:
        assert callable(p), f"{p!r} in PROBES is not callable"


def test_run_all_does_not_raise_and_returns_the_expected_shape():
    r = diagnostic.run_all()
    assert set(r) >= {"summary", "counts", "services"}
    assert set(r["counts"]) >= {"green", "red", "unconfigured", "total"}
    # main.py reads exactly these; a missing key would 500 the chat just like
    # the original NameError did.
    assert r["counts"]["total"] == len(r["services"]) == len(diagnostic.PROBES)


def test_a_crashing_probe_is_isolated_not_fatal(monkeypatch):
    """run_all wraps each probe; one bad probe must not take the report down."""
    def boom():
        raise RuntimeError("simulated probe failure")
    boom.__name__ = "_probe_boom"
    monkeypatch.setattr(diagnostic, "PROBES", [boom] + diagnostic.PROBES[:2])
    r = diagnostic.run_all()
    assert r["counts"]["total"] == 3
    assert any("probe crashed" in (s.get("error") or "") for s in r["services"])
