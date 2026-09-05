# Phase 2 — Aggressive Polish

**Do not start until every box in the Phase 1 acceptance checklist is ticked.**

**Goal:** make the work *legible*. Phase 2 adds no reconciliation logic. It adds a
dashboard, a live tamper demo, documentation, and the submission artefacts.

**The rule for this phase:** if a change touches `verify.py`, `proof.py`, or
`candidates.py`, it is out of scope. Write it in `IDEAS.md` and move on.

---

## Part A — The dashboard

### A1. Design direction (follow this; do not substitute a generic dashboard theme)

The subject matter is **accounting**, and accounting has a real, centuries-old visual
language that almost no software uses any more: greenbar ledger paper, right-aligned
figures in tabular columns, negatives in parentheses, a single rule above a subtotal and
a double rule under a final total, and a stamped verdict. We are building a machine that
*proves its arithmetic*, so the interface should look like a proof, not like a SaaS
analytics product.

**Palette** (define as CSS custom properties, use nothing outside this set):

| Token | Hex | Use |
|---|---|---|
| `--paper` | `#F3F6F1` | Page background — greenbar ledger stock |
| `--stripe` | `#E4EBE0` | Alternating ledger row band, hairline rules |
| `--ink` | `#16211C` | All primary text and figures |
| `--muted` | `#6B7A72` | Labels, secondary text |
| `--stamp-pass` | `#2F6B4F` | Verified verdict |
| `--stamp-fail` | `#9B2C2C` | Failed verdict, false-match warnings |
| `--flag` | `#A2691E` | Residuals, exceptions needing attention |

No gradients. No drop shadows. Borders and rules carry the structure.

**Type:** IBM Plex Sans for interface text; IBM Plex Mono **for figures only**. Set
`font-variant-numeric: tabular-nums` on every numeric cell. Scale 13 / 15 / 18 / 24 /
34px. Weights 400 and 600 only. Sentence case throughout.

**Number formatting rules:**
- Indian digit grouping: `₹4,14,382.00`, not `₹414,382.00`.
- Negatives in parentheses, accounting style: `(₹9,000.00)`.
- Right-align every figure column.
- Zero residual renders as `—`, not `0.00`.

**Layout:** ledger-first. The main surface is a full-width table, not a grid of cards.
The proof derivation opens in a panel below the selected row, pushing content down
rather than covering it.

**Spend the boldness in one place:** the verdict stamp. A 2px outlined rounded
rectangle, letter-spaced text, rotated about −4°, in `--stamp-pass` or `--stamp-fail`.
When the tamper demo flips a verdict, animate *only this element* (~180ms). Respect
`prefers-reduced-motion`.

Every source ID in the derivation is a control that reveals the raw record.
Traceability is the product.

### A2. Screens

Build in this order; earlier screens matter more.

1. **Ledger** — the table, with the inline derivation panel. Filter chips:
   All / Verified / Exceptions. Sort by amount, date, or residual.
2. **Exceptions** — the honest exception list, sorted by ₹ at stake descending.
   Columns: reason code, residual, records involved, *what a human should check*.
   Grouped by reason code with counts. This screen answers the track bar directly.
3. **Scorecard** — match rate (records **and** value), false-match rate stated as `0`,
   throughput, LLM proposed/kept, and the **per-case-type breakdown table**. Show
   calibration as a plain table, not a chart.
4. **Tamper** — one control: pick a record, corrupt it, re-run verification live. The
   affected proof's stamp flips to FAIL and the reason names the failing term. Add a
   "Restore" button. This is the demo climax, and it must call the real verifier.

### A3. Stack

- `serve.py` — FastAPI. Endpoints: `GET /api/report`, `POST /api/rerun`,
  `POST /api/tamper`, `POST /api/restore`, `GET /api/record/{id}`.
- Frontend: **no build step and no Node.** Plain HTML, CSS and ES modules served
  straight from `web/` by FastAPI, so **one command runs the whole thing**:
  `python -m reconproof.serve`. A judge should not need a toolchain to see the demo,
  and a committed `dist/` nobody can rebuild is worse than no `dist/` at all.
- Do not add a component library. This UI is tables and rules.

Accessibility floor: visible keyboard focus, semantic `<table>` markup, contrast
checked against `--paper`, works down to a 1280px laptop screen. Mobile is not
required — state that in the README rather than half-doing it.

---

## Part B — Repository polish

A public repo *is* the submission. Treat the README as the primary deliverable.

**`README.md`** — in this order: one sentence on what it does and which track; **run
instructions inside the first 20 lines**; the headline scorecard as a table with honest
numbers; "how it works" in six numbered stages; **"the trust boundary"** and how to
confirm the claim mechanically; **"what it cannot do"**; screenshots.

**`ARCHITECTURE.md`** — one diagram showing the six stages with the trust boundary drawn
as a line the LLM cannot cross, then a short "decisions and trade-offs" list: integer
paise, verifier isolation, abstention over coverage, seeded data.

**`RESULTS.md`** — the committed scorecard from `--seed 42`.

**Repo hygiene:** pinned `requirements.txt`, sensible `.gitignore`, `.env.example`, MIT
licence, no secrets in history, no commented-out dead code, clean commit messages. Add a
short "how to add a new exception type" section — it signals you designed for extension.

---

## Part C — The 5-minute video

Record at 1080p. Rehearse twice. Do not exceed 5:00.

| Time | Beat |
|---|---|
| 0:00–0:35 | **The problem, concretely.** A settlement is one lump credit, not a list of sales. Show a real derivation: fee, 18% GST on the fee, a refund from a different cycle. |
| 0:35–1:10 | **The thesis.** Track 04 says verification is the bottleneck. So I did not build an AI that guesses matches — I built one that proves them and refuses when it can't. *The model proposes, the verifier disposes.* |
| 1:10–2:30 | **Live run.** `--no-llm --seed 42`, then the ledger. Walk a verified derivation to zero residual. Then open an exception: the residual, the reason, the record to check. |
| 2:30–3:10 | **The tamper demo.** Corrupt one payment amount. Re-verify. The stamp flips to FAIL and names the term. *"The verifier re-reads the source records. It doesn't trust the proof — including its own."* |
| 3:10–4:00 | **The scorecard.** Match rate by record and by value. False-match rate: zero. Per-case-type breakdown — point at the weakest row and say so out loud. Then the LLM proposed/kept line. |
| 4:00–5:00 | **What broke.** One real story from `WHAT_BROKE.md`: symptom, diagnosis, and the number that got worse. Close on one honest limitation. |

Speak plainly. Do not read a script word for word.

---

## Part D — Submission

- **Project name:** ReconProof
- **Track:** 04 — AI Finance Controller
- **What it solves:** two or three sentences, no marketing.
- **GitHub URL:** public, verified in an incognito window before pasting.
- **Video:** unlisted. Check the link works logged out.
- **6 or 12 months:** 12.

**"What broke, and how you got out"** — they read this first, so write it last and write
it properly. One specific incident. Symptom → diagnosis → fix → **the number that got
worse**. Roughly 150–250 words. Concrete, technical, unembellished, no moral at the end.

**Final checks:**

- [x] Clone the repo into a clean folder and run it. Actually do this.
      — done from the public URL, not from a local copy: fresh venv, `pytest` green,
      `run.ps1` reproduced the committed scorecard to the paisa
- [ ] Video link works while logged out — **submitter's, not the build's**
- [x] README numbers match `RESULTS.md` — every figure cross-checked line by line
- [x] No API keys in git history — no `.env` tracked, no key-shaped strings, no home paths
- [x] Repo is public
- [ ] Resume attached — **submitter's, not the build's**
