# LOCKED DECISIONS — Dreamerie Command Center

Durable record of decisions Vinny has locked for this 4-company build. Anything
here is settled; do not revisit without an explicit new instruction.

## Per-mode dashboard backgrounds (LOCKED 28 Jul 2026, commit 8857e0e)

Switching business mode swaps the faint centered dashboard watermark
(`#logoWatermark`) to **that company's own brand**, so the workspace visibly
becomes that business when you switch to it.

| Mode | Background asset | Opacity |
|------|------------------|---------|
| `bear_arms` | `static/mode-bg-bear_arms.png` (Bear Arms NYC badge) | 0.10 |
| `peptides`  | `static/mode-bg-peptides.png` (NS Peptides mark) | 0.10 |
| `dreamerie` / `suzy_d` / `combined` | house logo `static/logo.webp` | 0.06 |

- Implemented as `MODE_BG` map + `applyModeBackground()` in the mode switcher in
  `static/index.html`. Runs on switch and on initial load.
- Modes without their own art fall back to the house logo. **To add a brand's
  background:** drop its art at `static/mode-bg-<mode>.png` and add one line to
  `MODE_BG`.
- The **house logo** (`static/logo.webp`) is still Stinger's art pending real
  Dreamerie logo files — that only affects the Dreamerie/Suzy D/Combined views.

## Business identity notes (context for the compliance-first sub-agents)

- **NS Peptides** (`peptides` mode): research-use-only positioning. Agent makes
  ZERO health / medical / dosing / benefit claims, any phrasing.
  Domain nspeptides.com · info@nspeptides.com.

  **CANONICAL DISCLAIMER — reproduce exactly, this is the one true rendering:**

  > `RESEARCH USE ONLY · NOT FOR HUMAN CONSUMPTION · FOR LABORATORY RESEARCH ONLY · BUYER ASSUMES ALL RISK`

  Four clauses, `·` separators. An earlier version of this file listed only
  three (dropping FOR LABORATORY RESEARCH ONLY) while the shipped prompt used
  four — so the "locked" doc contradicted the code on a string the agent is
  ordered to reproduce *verbatim*, and the next session would have "corrected"
  the prompt back to the weaker form. Four clauses matches the owner's printed
  flyer and is now canonical. If you change it, change it in
  `app/agents.py` (both sites) and `app/annabelle_updates.json` in the same commit.

  **PROVENANCE of the product facts** (so a future reviewer does not re-flag
  them as unsourced — they are not in any repo file by design): the 16-compound
  catalog, the tagline, `info@nspeptides.com`, `>=98% purity`, and
  `batch-tested for purity and potency` all come from the owner's own NS
  Peptides flyers, supplied 28 Jul. The source images live at
  `Dropbox\Logos and Misc\Clients\NS Peptides\Artwork\`. **No prices exist** —
  none were supplied, and none may be invented.

  **DELIBERATELY NOT USED:** the flyer's "discreet shipping" line. It is fine on
  a printed flyer but an agent *offering* it signals a buyer who does not want
  the purchase seen — not a laboratory procurement motive — and next to
  pharmacologically active compounds it undercuts the research-use-only shield
  rather than supporting it. The prompt now explicitly forbids it.
- **Bear Arms** (`bear_arms` mode): NYC firearms **accessories** + apparel, LLC no
  FFL. Agent gives no legal advice → NY firearms attorney; FFL-to-FFL supply;
  ammo/accessories/merch lane only until an FFL exists; firearm-friendly processor.
