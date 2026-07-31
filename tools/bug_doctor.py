#!/usr/bin/env python3
"""
Bug Doctor -- scans for the specific ways THIS codebase has actually failed.

Not a linter. Every check below is a bug that really happened here and really
cost something, encoded so it cannot happen quietly again:

  discarded-save      Generated images vanished. /api/content/image called
                      add_asset() and threw the result away, so a failed write
                      returned "ok" and finished work looked like forgetfulness.
  swallowed-error     Add User failed for weeks. add_user() caught everything,
                      print()ed it, returned bare False, and the endpoint said
                      "user already exists or error creating user" -- one string
                      for a name clash, an outage, and a missing column.
  read-truncation     A saved founder story was unreadable. list_posts renders
                      Content[:80], which is right for a list and useless for
                      reading, and nothing else could read one post.
  missing-migration   Airtable 422s a write naming a column that doesn't exist
                      and will NOT add it. A table created by an older deploy
                      whose _ensure_*_table skips _ensure_field on the
                      already-exists branch can never accept a new record.
  dead-frontend       Nine nav items were href="#" with no handler. Clicking
                      Settings did nothing: no panel, no error, no console line.
  unwired-tool        149 tools advertised, 68 dispatched. Delegates to the
                      coverage guard.
  mocked-subject      A test monkeypatched the exact function that was broken,
                      so it could never fail. Reported, not enforced -- mocking
                      is often correct, and only a human can say which is which.

Run it:  python tools/bug_doctor.py            (report, exit 1 if new findings)
         python tools/bug_doctor.py --all      (include known/accepted)

tests/test_bug_doctor.py runs the same checks so the build fails on a NEW
finding, while KNOWN_FINDINGS records what's already accepted. That list may
only shrink -- a stale entry fails the suite, so it can never become a place
where debt goes to be forgotten.
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "app"
STATIC = ROOT / "static"
TESTS = ROOT / "tests"

# Functions whose return value carries a success/failure signal. Calling one
# and discarding the result is the discarded-save bug.
SAVE_FUNCS = {
    "add_asset", "create_artifact", "save_strategy", "log_verification",
    "create_build_request", "log_skill_note", "create_draft", "set_setting",
    "set_user_setting", "increment_search_count", "record_answer", "save_page",
    "kb_create", "create_ticket", "update_ticket",
}
# ...except these, where fire-and-forget is the documented intent.
SAVE_DISCARD_OK = {"log_verification", "increment_search_count", "log_skill_note"}

READ_PREFIXES = ("get_", "read_", "find_", "fetch_", "load_")

# Pre-existing findings, recorded in tools/bug_doctor_baseline.txt rather than
# in code -- a 130-entry Python set is unmaintainable and nobody would read it.
#
# The baseline is DEBT MADE VISIBLE, not a mute button. Two tests hold it:
# a NEW finding fails the build, and a finding that no longer reproduces must
# be DELETED from the file. So it can only shrink, and it can never become the
# place where problems go to be forgotten.
#
# Regenerate deliberately (after reviewing every line):
#     python tools/bug_doctor.py --write-baseline
BASELINE_FILE = ROOT / "tools" / "bug_doctor_baseline.txt"


def load_baseline() -> set:
    if not BASELINE_FILE.exists():
        return set()
    return {ln.strip() for ln in BASELINE_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


KNOWN_FINDINGS: set = load_baseline()


class Finding:
    def __init__(self, check: str, path: str, line: int, symbol: str, detail: str):
        self.check, self.path, self.line = check, path, line
        self.symbol, self.detail = symbol, detail

    @property
    def key(self) -> str:
        return f"{self.check}:{self.path}:{self.symbol}"

    def __str__(self) -> str:
        return f"[{self.check}] {self.path}:{self.line}  {self.symbol}\n      {self.detail}"


def _py_files(folder: pathlib.Path):
    for p in sorted(folder.glob("*.py")):
        if p.name.startswith("test_"):
            continue
        yield p


# --------------------------------------------------------------------------
def check_discarded_saves() -> list[Finding]:
    """A write whose result nobody looks at is a write that can fail silently."""
    out = []
    for path in _py_files(APP):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # bare `foo.save(...)` as a statement -- result thrown away
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            fn = node.value.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in SAVE_FUNCS and name not in SAVE_DISCARD_OK:
                out.append(Finding(
                    "discarded-save", path.name, node.lineno, name,
                    f"{name}() result is discarded -- if the write fails, nothing "
                    f"anywhere will say so and the caller reports success."))
    return out


def check_swallowed_errors() -> list[Finding]:
    """except: that returns a value with no failure signal in it."""
    out = []
    for path in _py_files(APP):
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            body = node.body
            # a handler that only logs/prints and returns a plain value
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
            for r in returns:
                if r.value is None:
                    continue
                # returning a bare False/None/"" hides WHICH failure happened
                if isinstance(r.value, ast.Constant) and r.value.value in (False, None, ""):
                    fname = _enclosing_func(tree, r)
                    out.append(Finding(
                        "swallowed-error", path.name, r.lineno, fname,
                        f"except: returns bare {r.value.value!r} -- the caller cannot tell "
                        f"a name clash from an outage from a missing column."))
            if not returns and len(body) == 1 and isinstance(body[0], ast.Pass):
                out.append(Finding(
                    "swallowed-error", path.name, node.lineno, "except: pass",
                    "exception silently swallowed -- no log, no signal, no trace."))
    return out


def _enclosing_func(tree: ast.AST, node: ast.AST) -> str:
    best, best_line = "<module>", -1
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.lineno <= getattr(node, "lineno", 0) and n.lineno > best_line:
                best, best_line = n.name, n.lineno
    return best


def check_read_truncation() -> list[Finding]:
    """A reader that silently truncates makes stored data unreadable."""
    out = []
    for path in _py_files(APP):
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not fn.name.startswith(READ_PREFIXES) and "list_" not in fn.name:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
                    continue
                sl = node.slice
                if sl.lower is None and isinstance(sl.upper, ast.Constant) \
                        and isinstance(sl.upper.value, int) and sl.upper.value <= 400:
                    out.append(Finding(
                        "read-truncation", path.name, node.lineno, fn.name,
                        f"truncates to [:{sl.upper.value}] inside a READ function -- if this "
                        f"is the only way to read the record, the rest is unreachable."))
    return out


def check_missing_migration() -> list[Finding]:
    """_ensure_*_table that skips _ensure_field on the already-exists path."""
    out = []
    for path in _py_files(APP):
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            if not (fn.name.startswith("_ensure") and fn.name.endswith("table")):
                continue
            body = ast.get_source_segment(src, fn) or ""
            creates = "meta/bases" in body and "fields" in body
            # Migration may be one level of indirection away -- tickets.py and
            # onboarding.py both loop their field list inside a _migrate()
            # helper. Flagging those was a false positive, and false positives
            # are how a scanner gets ignored.
            # Migration takes three shapes in this repo, all legitimate:
            # the crm._ensure_field helper, a module-local _migrate*() loop, or
            # a direct POST to .../tables/<id>/fields. Missing the third one
            # made prospects.py a false positive after it was correctly fixed;
            # requiring the helper be named exactly _migrate( then made
            # crm._migrate_fields() a false positive the moment the real
            # 30 Jul missing-column bug was fixed. Match any _migrate* helper.
            migrates = ("_ensure_field" in body
                        or re.search(r"\b_migrate\w*\s*\(", body)
                        or "/fields" in body)
            if creates and not migrates:
                out.append(Finding(
                    "missing-migration", path.name, fn.lineno, fn.name,
                    "defines columns only when CREATING the table. Airtable 422s a write "
                    "naming a column that doesn't exist and will not add it -- a table from "
                    "an older deploy can never accept a new record."))
    return out


def check_dead_frontend() -> list[Finding]:
    """Clickable ids the page's JavaScript never mentions at all.

    Deliberately conservative. The first version flagged 24 elements, nearly
    all wired by patterns it didn't recognise -- a submit button handled by its
    form's onsubmit, a variable assigned once and bound later. A scanner that
    cries wolf gets ignored, which is worse than not having one. So the test is
    now the weakest honest signal: does the id appear ANYWHERE in a <script>?
    If JS never says its name, nothing can be listening to it.
    """
    out = []
    for page in sorted(STATIC.glob("*.html")):
        html = page.read_text(encoding="utf-8", errors="replace")
        scripts = "\n".join(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                                       html, re.S))
        # elements that do nothing unless JS wires them
        candidates = set()
        for m in re.finditer(r'<(a|button)\b([^>]*)>', html, re.I):
            attrs = m.group(2)
            mid = re.search(r'id="([^"]+)"', attrs)
            if not mid:
                continue
            if "onclick" in attrs.lower():
                continue                                   # inline handler
            if re.search(r'type="submit"', attrs, re.I):
                continue                                   # the form submits it
            if m.group(1).lower() == "a" and not re.search(r'href="#"', attrs):
                continue                                   # a real link navigates
            candidates.add(mid.group(1))
        for el_id in sorted(candidates):
            if el_id in scripts:
                continue
            line = html[:html.index(f'id="{el_id}"')].count("\n") + 1
            out.append(Finding(
                "dead-frontend", page.name, line, el_id,
                "clickable element whose id never appears in any script on the page -- "
                "nothing can be listening, so clicking it does nothing: no action, no "
                "error, no console line."))
    return out


def check_fully_mocked_tests() -> list[Finding]:
    """A test that stubs everything it touches proves nothing.

    The Add User test monkeypatched users.add_user -- the exact function that
    was broken -- so it asserted the ROUTE returned success while stubbing the
    save that was failing. It could not fail. The smell is a test with lots of
    stubs and few real assertions about behaviour.
    """
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
                continue
            body = ast.get_source_segment(src, fn) or ""
            stubs = len(re.findall(r"monkeypatch\.setattr\(", body))
            asserts = len([n for n in ast.walk(fn) if isinstance(n, ast.Assert)])
            if stubs >= 3 and asserts <= stubs:
                out.append(Finding(
                    "fully-mocked-test", path.name, fn.lineno, fn.name,
                    f"{stubs} stubs vs {asserts} assertion(s) -- check this isn't mocking "
                    f"away the very thing it claims to prove. A test that stubs the "
                    f"function under test can never fail."))
    return out


def check_unwired_tools() -> list[Finding]:
    """Advertised to the model, no dispatch branch."""
    sys.path.insert(0, str(ROOT))
    try:
        from app.agents import (DELEGATION_TOOLS, OHH_BEEHAVE_MODE_TOOLS,
                                PUBLIC_TOOLS, STINGER_MODE_TOOLS, TOOL_NAME_TO_AGENT_KEY)
    except Exception as e:
        return [Finding("unwired-tool", "app/agents.py", 0, "<import>", f"could not import: {e}")]
    src = (APP / "main.py").read_text(encoding="utf-8", errors="replace")
    handled = set(re.findall(r'block\.name\s*==\s*"([a-z0-9_]+)"', src))
    for grp in re.findall(r'block\.name\s+in\s*\(([^)]*)\)', src):
        handled |= set(re.findall(r'"([a-z0-9_]+)"', grp))
    handled |= set(TOOL_NAME_TO_AGENT_KEY)
    # The unbuilt registry is a real dispatch form: `block.name in
    # unbuilt.REGISTRY` returns an honest refusal that names the requirement
    # and the alternative. Those tools ARE handled.
    try:
        from app.unbuilt import REGISTRY
        handled |= set(REGISTRY)
    except Exception:
        pass
    defined = {t["name"] for t in DELEGATION_TOOLS}
    offered = (defined & (STINGER_MODE_TOOLS | OHH_BEEHAVE_MODE_TOOLS)) | {
        t["name"] for t in PUBLIC_TOOLS}
    return [Finding("unwired-tool", "app/agents.py", 0, name,
                    "advertised to the model with no handler -- returns 'Unknown tool'.")
            for name in sorted(offered - handled)]


def check_mocked_subject() -> list[Finding]:
    """Tests that stub an app function -- REPORT ONLY.

    Mocking is usually right. But the Add User test monkeypatched
    users.add_user, which is where the bug was, so it could never fail. Only a
    human can tell those apart, so this never fails the build.
    """
    out = []
    for path in sorted(TESTS.glob("test_*.py")):
        src = path.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'monkeypatch\.setattr\(\s*[\w.]*\.?(\w+),\s*"(\w+)"', src):
            out.append(Finding(
                "mocked-subject", path.name, src[:m.start()].count("\n") + 1,
                f"{m.group(1)}.{m.group(2)}",
                "stubbed in a test -- confirm the test isn't mocking away the thing it "
                "is supposed to be proving."))
    return out


CHECKS = {
    "discarded-save": check_discarded_saves,
    "swallowed-error": check_swallowed_errors,
    "read-truncation": check_read_truncation,
    "missing-migration": check_missing_migration,
    "dead-frontend": check_dead_frontend,
    "unwired-tool": check_unwired_tools,
}
ADVISORY = {
    "mocked-subject": check_mocked_subject,
    "fully-mocked-test": check_fully_mocked_tests,
}


def run(include_advisory: bool = False) -> list[Finding]:
    found = []
    for fn in CHECKS.values():
        found.extend(fn())
    if include_advisory:
        for fn in ADVISORY.values():
            found.extend(fn())
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="Scan for this codebase's known failure modes.")
    ap.add_argument("--all", action="store_true", help="include known/accepted findings")
    ap.add_argument("--advisory", action="store_true", help="include report-only checks")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record every CURRENT finding as accepted debt (review first)")
    args = ap.parse_args()

    if args.write_baseline:
        items = sorted({f.key for f in run(include_advisory=True)})
        BASELINE_FILE.write_text(
            "# Bug Doctor baseline -- pre-existing findings, accepted for now.\n"
            "# This file may only SHRINK. A new finding fails the build; a finding\n"
            "# that no longer reproduces must be deleted from here or the suite fails.\n"
            "# Each line is check:file:symbol. Fix one, delete its line.\n"
            + "\n".join(items) + "\n", encoding="utf-8")
        print(f"wrote {len(items)} findings to {BASELINE_FILE.name}")
        return 0

    findings = run(include_advisory=args.advisory)
    new = [f for f in findings if f.key not in KNOWN_FINDINGS] if not args.all else findings
    by_check: dict[str, list[Finding]] = {}
    for f in new:
        by_check.setdefault(f.check, []).append(f)

    print("=" * 72)
    print("BUG DOCTOR")
    print("=" * 72)
    if not new:
        print("\nNo findings. Every check below came from a real bug in this repo:\n")
        for name in list(CHECKS) + (list(ADVISORY) if args.advisory else []):
            print(f"  clean  {name}")
        return 0
    for check, items in sorted(by_check.items()):
        print(f"\n--- {check}  ({len(items)}) ---")
        for f in items:
            print(f"  {f}")
    print(f"\n{len(new)} finding(s).")
    hard = [f for f in new if f.check in CHECKS]
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
