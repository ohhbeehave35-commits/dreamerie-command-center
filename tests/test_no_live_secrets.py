"""No live secret may sit in a tracked file. Ever.

WHY THIS EXISTS: on 31 Jul 2026 GitGuardian flagged the LIVE Ohh BeeHave
Facebook Zapier catch hook sitting in tests/test_per_brand_social_webhooks.py
-- in a PUBLIC repo. A Zapier catch hook is unauthenticated: anyone holding
the URL can publish to the connected Facebook Page. The rule ("never commit
real credentials") already existed and already failed once, which per the
house rules means it is a MISSING MECHANISM, not a reminder problem.

This is the mechanism. It runs in the deterministic suite, which the pre-push
hook refuses to bypass silently.

Two classes of failure:
1. ANY complete Zapier catch-hook URL, real or fake. Test fixtures must
   assemble the URL from pieces at runtime ("https://hooks.zapier.com" +
   "/hooks/catch/" + "000000/fakehook/") so a scanner -- or a copy-paste --
   never sees a working-shaped URL in source. A prefix alone (validation
   code, placeholder text) is fine.
2. Key material with a known prefix and a real-length body (Anthropic,
   Stripe live/restricted, webhook signing secrets, xAI, Airtable PATs,
   Google OAuth, AWS, GitHub, Slack webhooks). Short obviously-fake bodies
   in fixtures ("sk-ant-verysecret") stay legal.

The scanner builds its own patterns by concatenation so it never matches
itself.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Assembled so this file never contains the thing it hunts.
_CATCH = "hooks\\.zapier\\.com/hooks/" + "catch/" + r"\d+/[A-Za-z0-9]+"
_HOOK_RX = re.compile(_CATCH)

_KEY_RXS = [
    re.compile("sk-" + "ant-" + r"[A-Za-z0-9_\-]{30,}"),
    re.compile("sk_" + "live_" + r"[A-Za-z0-9]{20,}"),
    re.compile("rk_" + "live_" + r"[A-Za-z0-9]{20,}"),
    re.compile("wh" + "sec_" + r"[A-Za-z0-9]{20,}"),
    re.compile("xai" + "-" + r"[A-Za-z0-9]{30,}"),
    re.compile("pat" + r"[A-Za-z0-9]{14}\.[a-f0-9]{40,}"),      # Airtable PAT
    re.compile("GOCSPX" + "-" + r"[A-Za-z0-9_\-]{20,}"),        # Google OAuth
    re.compile("AKIA" + r"[0-9A-Z]{16}"),                        # AWS access key
    re.compile("gh" + r"[pous]_[A-Za-z0-9]{30,}"),               # GitHub tokens
    re.compile("github_" + "pat_" + r"[A-Za-z0-9_]{40,}"),
    re.compile("AIza" + r"[A-Za-z0-9_\-]{30,}"),                 # Google API key
    re.compile("hooks\\.slack\\.com/" + "services/" + r"T[A-Za-z0-9/]{20,}"),
]

_TEXT_SUFFIXES = {".py", ".html", ".md", ".txt", ".json", ".jsonl", ".yml",
                  ".yaml", ".js", ".css", ".cmd", ".sh", ".ps1", ".toml",
                  ".ini", ".cfg", ".example", ".sql", ".csv", ".xml", ".svg"}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True).stdout
    files = []
    for rel in out.split("\0"):
        if not rel:
            continue
        p = ROOT / rel
        if p.suffix.lower() in _TEXT_SUFFIXES and p.is_file():
            files.append(p)
    # If git ever returns nothing we are scanning the wrong directory --
    # that must be loud, not a silent green.
    assert len(files) > 50, f"only {len(files)} tracked text files found under {ROOT}"
    return files


def test_no_complete_catch_hook_url_anywhere():
    bad = []
    for p in _tracked_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for m in _HOOK_RX.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            bad.append(f"{p.relative_to(ROOT)}:{line}: complete catch-hook URL "
                       f"(assemble fixtures from pieces instead)")
    assert not bad, "\n".join(bad)


def test_no_real_length_key_material_anywhere():
    bad = []
    for p in _tracked_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for rx in _KEY_RXS:
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                shown = m.group(0)[:10]
                bad.append(f"{p.relative_to(ROOT)}:{line}: real-length key "
                           f"material starting {shown!r}")
    assert not bad, "\n".join(bad)


def test_the_scanner_can_actually_fail():
    """A guard that can't fire is decoration (see: the 8,779-pass suite)."""
    planted = ("https://hooks.zapier.com" + "/hooks/catch/" + "12345/realish/")
    assert _HOOK_RX.search(planted), "hook regex failed to match a planted URL"
    planted_key = "sk-" + "ant-" + ("a" * 40)
    assert any(rx.search(planted_key) for rx in _KEY_RXS), \
        "key regexes failed to match a planted Anthropic-shaped key"
