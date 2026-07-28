# Skills & Lessons — Dreamerie Command Center

Transferable engineering lessons for this build. Platform lessons proven on
the flagship are ported here **de-tenanted** — no other tenant's pricing,
client names, or internal figures ever land in this repo.

---

## 1. A Green Test Suite Can Be Worse Than a Red One

Proven on the flagship 28 Jul: 8,779 recorded results and a headline "90%
clean pass rate" — all measuring hardcoded stub endpoints that returned
constant values. Several "security" tests asserted nothing at all, because
the endpoints never parsed their request body.

**The rule:** a passing test proves a behavior is *correct*, never that the
behavior *exists*. Before trusting any suite here, prove one test can fail
for the right reason — break the thing it claims to check and watch it go red.

**Tell:** if the pass rate never moves no matter what you break, you are
measuring the harness, not the product.

## 2. Verify Every Red Before You "Fix" It

Two separate live tests counted a **disambiguating question** as a failure —
the assistant asked "which one do you mean?" instead of answering. That was
correct behavior, not a defect: the question genuinely was ambiguous.
"Fixing" either one would have made the assistant measurably worse.

**The rule:** a red test is a hypothesis, not a verdict. Read the actual
output before changing product code. Sometimes the model is right and the
test is wrong.

**Applies directly here:** this deployment runs five business modes (The
Dreamerie, Suzy D, Bear Arms, NS Peptides, Combined). A question that is
unambiguous in one mode is often ambiguous across all five, so the assistant
asking which business you mean is usually the *right* answer.

## 3. A Partial Answer Is a Wrong Answer

The flagship shipped a bug where a lookup returned only its single
best-scoring match. One question with two halves ("what does it cost?" =
setup fee **and** monthly fee) surfaced only one half, and the assistant then
truthfully reported the incomplete result it was handed — stating that the
missing half did not exist.

**The rule:** when a tool result looks like it only covers half the question,
say what you got *and* what is missing. Never let "the tool didn't return it"
become "it doesn't exist."

## 4. Never Invent a Figure to Fill a Gap

Some services legitimately have no locked price yet. The correct response is
the custom-quote fallback plus a route to the owner — **not** a number
borrowed from a different service to make the answer feel complete.

**Applies directly here:** NS Peptides and Bear Arms both have compliance
constraints that make invented specifics genuinely dangerous — see
`LOCKED-DECISIONS.md`. An honest "I don't have that; let me get it from you"
is always better than a plausible guess.

## 5. Sample N Times Before Calling Something Fixed

The same question asked three times produced three different answers on the
flagship — correct, incomplete, and flatly wrong. A single sample would have
supported whatever conclusion you wanted.

**The rule:** for any answer that touches money, compliance, or safety, ask
N≥5 times and grade the *agreement ratio*. Flakiness on a critical answer is
itself the defect, even when each individual run could pass. Grade severity
too: an answer that states something false is materially worse than one that
is merely incomplete.

## 6. Per-Mode Backgrounds (this build's own pattern)

Switching business mode swaps the dashboard watermark to that company's brand
so the workspace visibly becomes that business. Implemented as a `MODE_BG`
map plus `applyModeBackground()` in the mode switcher; modes without their
own art fall back to the house logo. To add a brand: drop art at
`static/mode-bg-<mode>.png` and add one line to `MODE_BG`. See
`LOCKED-DECISIONS.md`.

## 7. Every tool-dispatch branch must fail soft

A single tool crashing must never 500 the whole chat. On 28 Jul the
`run_diagnostic` branch called `diagnostic.run_all()`, which referenced an
undefined `PROBES` (a list defined in the flagship but not ported here). The
branch was unguarded, so the `NameError` escaped the handler as a bare
`{"detail":"internal error"}` 500 — and asking "is everything working?" took
Annabelle completely down. Every `elif block.name == …` branch that can raise
must wrap its work in `try/except` and return an honest tool-result string
(the drive-save branch is the model). The fix also defined `PROBES`; the guard
is what makes any *future* probe bug survivable. See also §1 — a passing suite
didn't catch this because nothing exercised the tool.

## 8. Access-code auth IS an identity on this build

Susan's deployment has no per-user login — the shared access code is the only
credential. Endpoints that demanded a `cc_session` (`/api/me`, `/api/history`,
and admin `/api/settings` saves) therefore refused her: history showed a false
"session expired" banner and every settings Save silently no-op'd.
`_identity_scope()` now maps a valid access cookie to the `access:` bucket, and
`is_owner` is true for a valid access-code request. When adding a per-user
endpoint here, decide what it does for an access-code request — do NOT assume a
session exists.

## 9. Business websites feed Annabelle

Settings → Business Websites stores one URL per business as `website__<brand>`.
`_website_context(mode)` injects the active business's site into the prompt so
Annabelle shares the real URL and never invents one. It is brand-scoped: in a
specific business mode she gets ONLY that business's site (never another's);
combined mode lists all. Keep that isolation if you touch it.

---

*When adding a lesson: keep it de-tenanted. If it requires naming another
client, their pricing, or another tenant's internal analysis to make sense,
it belongs in the flagship's `docs/SKILLS.md`, not here.*
