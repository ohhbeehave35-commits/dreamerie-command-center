"""Bug Doctor enforcement — keeps the scanner honest and the build gated.

The scanner was wired into this client on 31 Jul 2026 with a REVIEWED baseline
(tools/bug_doctor_baseline.txt) of inherited-from-flagship accepted debt plus
known false positives. From here the rule is one-directional: a NEW finding
fails the build. See the baseline header for provenance.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_file_location("bug_doctor", ROOT / "tools" / "bug_doctor.py")
bug_doctor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bug_doctor)


def test_no_new_hard_findings():
    """Fails the build on a NEW instance of a bug class already accepted."""
    findings = bug_doctor.run(include_advisory=False)
    new = [f for f in findings if f.key not in bug_doctor.KNOWN_FINDINGS]
    assert not new, (
        "Bug Doctor found issues not in the reviewed baseline:\n\n"
        + "\n".join(f"  {f}" for f in new)
        + "\n\nFix them, or add the key to tools/bug_doctor_baseline.txt WITH a reason."
    )


def test_baseline_only_shrinks():
    """Every baselined key must still correspond to a real current finding.
    A key that no longer reproduces is stale debt-hiding and must be deleted."""
    live = {f.key for f in bug_doctor.run(include_advisory=True)}
    stale = [k for k in bug_doctor.KNOWN_FINDINGS if k not in live]
    assert not stale, (
        "Baseline entries that no longer reproduce -- delete them:\n  "
        + "\n  ".join(sorted(stale))
    )


def test_the_scanner_can_actually_fail(tmp_path, monkeypatch):
    """A guard that can't fire is decoration. Plant a swallowed-error and
    confirm the swallowed-error check flags it."""
    planted = ROOT / "app" / "_doctor_selftest_tmp.py"
    planted.write_text(
        "def broken():\n    try:\n        return risky()\n    except Exception:\n        pass\n",
        encoding="utf-8")
    try:
        hits = [f for f in bug_doctor.check_swallowed_errors()
                if f.path == "_doctor_selftest_tmp.py"]
        assert hits, "swallowed-error check failed to flag a planted except: pass"
    finally:
        planted.unlink(missing_ok=True)
