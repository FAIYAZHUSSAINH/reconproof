# Phase 1 — Working Prototype

**Goal:** a command-line system that reconciles a synthetic batch, proves every match,
abstains honestly, and prints a scorecard with a **false-match rate of 0**.

**Definition of done:** `python -m reconproof.run --no-llm --seed 42` completes,
writes `report.json` and `RESULTS.md`, and `pytest` is green. Nothing else.

**Budget:** ~14–16 working hours. Work through the steps in order.

---

## 0. The domain, in plain terms

A merchant sells things. Customers pay via a payment gateway. The gateway does **not**
send the merchant each payment individually. It **batches** them and sends one lump-sum
bank credit on a T+n cycle, **after deducting its cut**. The merchant's finance team
then has to answer: *which orders does this ₹4,14,382 credit actually represent?*

That is hard because a settlement is never a clean sum of sales:

| Deduction / addition | What it is |
|---|---|
| **MDR / gateway fee** | The gateway's percentage cut per payment. |
| **GST on the fee** | 18% tax charged *on the fee*, not on the sale. Rounded to the paisa — and the rounding mode is a real source of 1–2 paise residuals. |
| **Refunds** | Money returned to customers, deducted from a *later* settlement than the original sale. In India the original fee is normally **not** reversed with the refund. |
| **Chargeback deduction** | A disputed payment clawed back. |
| **Chargeback reversal** | If the merchant wins the dispute, the money comes back weeks later, often as a generic "adjustment" line with **no reference to the original payment**. This is the single hardest real-world case. |
| **TDS (B2B)** | A business customer withholds tax at source, so the *payment* is smaller than the *order*. Looks identical to a partial payment unless you reason about the exact percentage. |
| **Timing / netting** | One settlement may arrive as two bank credits, or two settlements as one. |

So the ledger identity we must prove for every bank credit is:

```
  Σ payment.amount
− Σ payment.fee
− Σ payment.tax                    (GST on fee)
− Σ refund.amount
− Σ chargeback_deduction.amount
+ Σ chargeback_reversal.amount
= settlement.net
= bank_credit.amount
```

If that balances to **exactly 0 paise**, the match is proven. If it does not, the
residual *is the diagnosis* — its size and sign tell you what is missing.

We run **two loops**. Loop A is mandatory; Loop B is a stretch inside Phase 1.

- **Loop A (core):** bank credit → settlement → its payment/refund/adjustment line items.
- **Loop B (stretch):** order → payment, to catch short-payments and detect TDS.

---

## Step 1 — Skeleton and the money type (1 h)

**`src/reconproof/money.py`**

- A `Paise = int` alias plus helpers. All amounts are integer paise, always.
- `def rupees(p: int) -> str` — format for display as `₹4,14,382.00` using the **Indian
  digit grouping** (2,2,3).
- `def gst_on_fee(fee_paise: int, rate_bp: int = 1800) -> int` — 18% GST with an
  **explicit, documented rounding mode**. Half-up: `(fee * 1800 + 5000) // 10000`.
- A guard: `assert_paise(x)` that raises if handed a `float`. Call it at every ingest
  boundary.

**Test now** (`tests/test_money.py`): Indian formatting, GST rounding at boundary
values, and that passing a float raises.

---

## Step 2 — Records and the Proof schema (1.5 h)

**`src/reconproof/models.py`** — pydantic models, all amounts `int` paise:

```
Order        order_id, customer_id, amount, created_at, is_b2b
Payment      payment_id, order_id, amount, fee, tax, method, captured_at, status
Refund       refund_id, payment_id, amount, created_at
Adjustment   adjustment_id, kind, amount, ref_payment_id (Optional!), created_at
Settlement   settlement_id, utr, settled_at, gross, fee, tax, refund_total,
             adjustment_total, net
BankTxn      bank_txn_id, value_date, narration, credit_amount
```

`Adjustment.kind` — `chargeback_deduction | chargeback_reversal | commission_correction`.
`ref_payment_id` is deliberately `Optional` — the unlabelled reversal is a core hard case.

**The Proof object** — the centrepiece. It must read like an accounting derivation,
because Phase 2 renders it as one:

```python
class ProofTerm(BaseModel):
    label: str          # "Gross payments", "Gateway fee", "GST on fee"
    sign: Literal[1, -1]
    amount: Paise
    source_ids: list[str]

class Proof(BaseModel):
    proof_id: str
    bank_txn_id: str
    settlement_id: str | None
    rule: str                      # e.g. R1_UTR_EXACT
    terms: list[ProofTerm]
    computed_net: Paise
    observed_credit: Paise
    residual: Paise                # computed_net - observed_credit
    members: dict[str, list[str]]
    evidence: list[dict]
    confidence: float
    verdict: Literal["PASS", "FAIL", "UNVERIFIED"] = "UNVERIFIED"
    verdict_reason: str | None = None
```

The proof must be **self-describing**: someone reading `report.json` with no code should
understand why the match holds.

---

## Step 3 — The synthetic data generator (3 h) — *this is your moat*

**`src/reconproof/generate.py`**, seeded, writes CSVs to `data/generated/` plus
`data/ground_truth.json`. Target ~140 payments across ~12 settlements and ~14 bank
credits. Ground truth maps each `bank_txn_id` → the exact member IDs and a `case_type`.

**Inject every one of these, at least twice each, and label them:**

| # | `case_type` | What to generate |
|---|---|---|
| 1 | `CLEAN` | N payments, fee + GST, one settlement, one bank credit. |
| 2 | `WITH_REFUND` | A refund on a payment from an *earlier* settlement. Fee not reversed. |
| 3 | `CHARGEBACK_DEDUCTION` | A disputed payment deducted, `ref_payment_id` present. |
| 4 | `UNLABELLED_REVERSAL` | Chargeback won weeks later. `ref_payment_id=None`. Only identifiable by amount + timing. |
| 5 | `SPLIT_SETTLEMENT` | One settlement paid out as two bank credits. |
| 6 | `MERGED_CREDIT` | Two settlements arriving as one bank credit. |
| 7 | `ROUNDING_DRIFT` | GST computed with a different rounding mode — 1–2 paise residual. |
| 8 | `AMOUNT_COLLISION` | Two settlements, same date, **identical** net. Must abstain if the UTR is unreadable. |
| 9 | `NARRATION_NOISE` | Bank narration with the UTR buried in junk. |
| 10 | `MISSING_BANK_CREDIT` | Settlement exists, no bank credit yet (T+1 timing). |
| 11 | `ORPHAN_CREDIT` | Bank credit with no settlement (a direct customer NEFT). |
| 12 | `TDS_SHORT_PAYMENT` | B2B: `payment.amount = order.amount − 10%`. Loop B. |
| 13 | `DUPLICATE_UTR` | Same UTR twice — one a credit, one a same-day reversal. |

Add `--difficulty easy|standard|hard`. Demo on `standard`.

**Do not** let the generator write ground truth into the CSVs. Ground truth is only
loaded by `report.py` for scoring.

---

## Step 4 — Ingest + candidate generation (2 h)

**`ingest.py`** — read CSVs into models, assert integers, build lookup dicts by ID.

**`candidates.py`** — deterministic. Generate **all** plausible (bank_txn, settlement,
member-set) candidates. Do not pick a winner; that is the verifier's job.

- `R1_UTR_EXACT` — extract a UTR from the narration by regex, exact match.
- `R2_AMOUNT_DATE` — net amount equal within a ±3-day value-date window.
- `R3_SPLIT` — a subset of bank credits in a window summing to one settlement net.
- `R4_MERGE` — a subset of settlements summing to one bank credit.
- `R5_LLM_HYPOTHESIS` — filled by the LLM layer in Step 6.

**Guardrail:** cap subset search at size 3 and window 5 days. Log every cap hit.

---

## Step 5 — Proof construction and THE VERIFIER (3 h) — the heart of the project

**`proof.py`** — turn a candidate into a `Proof`, terms in accounting order.

**`verify.py`** — write this file with real care. It is what you demo.

Rules for this file:

- Imports only `models`, `money`, stdlib. **No matcher, no LLM, no pandas.**
- For every member ID, fetch the record from `records` and use *its* amount.
- Recompute every term. A recomputed term that differs from the claim fails with
  `TERM_MISMATCH:<label>`.
- Recompute the net, compare to the observed credit, set `residual`.
- Verdicts: `residual == 0` → `PASS`; within an explicitly non-zero tolerance → `PASS`
  with `within_tolerance`; otherwise `FAIL` with a **classified** reason —
  `SUSPECT_UNAPPLIED_REFUND`, `ROUNDING_DRIFT`, `SUSPECT_TDS`,
  `SUSPECT_UNLABELLED_REVERSAL`, else `UNEXPLAINED_RESIDUAL`.
- Also check **member exclusivity** globally: no payment in two PASSing proofs.

**Test hard** (`tests/test_verify.py`) — these tests *are* the pitch:

1. A correct proof passes.
2. Mutating one payment amount flips that proof to FAIL, and the reason names the term.
3. A proof that lists a member it does not contain fails.
4. A proof with a hand-forged `computed_net` fails.
5. The same payment in two proofs triggers the exclusivity rule.

---

## Step 6 — Confidence, abstention, and the optional LLM layer (2 h)

**`confidence.py`** — deterministic scoring, no LLM. `R1` 0.98, `R2` 0.85, `R3/R4` 0.70,
+0.02 for a zero residual, **capped at 0.50** when a competing candidate exists, small
penalty for an unusually large member count. Threshold `--abstain-below 0.75`. **A FAIL
is never promoted to a match, whatever the confidence.**

**`adjudicate.py`** — the *optional* enhancement layer.

- Runs only when `--llm` is passed and `GROQ_API_KEY` is set.
- Input: one unmatched bank credit, its residual, and a compact window of nearby
  unmatched records. Never the whole dataset.
- Output: a strict JSON **hypothesis** — `{"add_members": [...], "reason": "..."}`.
- That hypothesis becomes a new `Proof` and goes **back through `verify()`**.
- Log every hypothesis with its outcome. Cache responses keyed by a prompt hash.

Model: `llama-3.3-70b-versatile` on Groq, `temperature=0`.

---

## Step 7 — Report and scorecard (2 h)

**`report.py`** loads `ground_truth.json` *only here* and emits `report.json`,
`RESULTS.md`, and a `rich` table to stdout.

Report all of: auto-match rate (records), value coverage, **false-match rate**,
exception count + taxonomy, ₹ at stake, calibration table, throughput, and the
**per-case-type breakdown** — the most persuasive artefact you will produce.

---

## Step 8 — CLI, tests, README (1.5 h)

**`run.py`** flags: `--seed`, `--no-llm/--llm`, `--difficulty`, `--abstain-below`,
`--tamper <record_id>`, `--out`.

`--tamper` mutates one source amount *after* proofs are built, then re-runs the
verifier, so a PASS flips to FAIL live.

**README first 20 lines must be runnable instructions.**

---

## Phase 1 acceptance checklist — all must be true before Phase 2

Signed off 2026-09-05, each line re-verified against the repo as it stands rather
than from memory of having done it.

- [x] `python -m reconproof.run --no-llm --seed 42` runs clean on a fresh venv
      — checked from a clean clone of the public repo, new venv, `requirements.txt` only
- [x] All 13 case types appear in the generated data — 13 rows in the per-case-type table
- [x] `report.json` and `RESULTS.md` written
- [x] **False-match rate is 0** — and at `easy`, `standard` and `hard`
- [x] Every exception has a typed reason and a residual — exception coverage 100.0%
- [x] Per-case-type breakdown present — weakest rows sorted to the top and flagged
- [x] `pytest` green, including the tamper test — 138 passed
- [x] `--tamper` visibly flips a proof to FAIL — CLI and dashboard, same `apply_tamper`
- [x] README runs in under 60 seconds, no API key — `--no-llm` is the default path
- [x] `WHAT_BROKE.md` has at least two real entries — nine
- [x] Committed in small increments with clear messages

If the match rate is lower than you hoped — **ship the honest number.**
