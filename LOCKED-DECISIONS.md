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
  ZERO health / medical / dosing / benefit claims, any phrasing. "Research use
  only · not for human consumption · buyer assumes all risk." Domain nspeptides.com.
- **Bear Arms** (`bear_arms` mode): NYC firearms **accessories** + apparel, LLC no
  FFL. Agent gives no legal advice → NY firearms attorney; FFL-to-FFL supply;
  ammo/accessories/merch lane only until an FFL exists; firearm-friendly processor.
