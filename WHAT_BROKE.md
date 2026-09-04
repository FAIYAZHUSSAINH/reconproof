# What broke

A running log, written while it was happening. Only real problems: things that
were wrong and that I had to diagnose, not routine implementation. Each entry
ends with the number that got worse, where there was one, because a fix that
only ever improves the metrics is usually a fix to the metrics.

---

## 2026-09-04 — The verifier called a moved bank credit a forgery

**Symptom.** `--tamper bnk_0007` — the demo the whole project is built around —
printed `FORGED_OBSERVED_CREDIT` instead of a residual. Three verifier tests
failed with the same reason code where they expected `ROUNDING_DRIFT`,
`UNEXPLAINED_RESIDUAL`, and a tolerance-accepted PASS.

**Diagnosis.** `_claim_failure` compared the proof's cached `observed_credit`
against the bank record and failed on any difference. That folded two
completely different situations into one verdict:

- the proof contradicts *itself* — its terms, net and residual do not add up,
  which nothing in this repo can produce, so the file was edited by hand;
- the proof disagrees with *the data* — a source record moved after the proof
  was built, which is the entire thing this product exists to detect.

The second one is not a forgery. It is a residual, and a residual is the
diagnosis. Worse, the check made a legitimate outcome unreachable: if a credit
and a payment both changed such that the books balanced again, the verifier
would still have refused the proof for a bookkeeping reason unrelated to the
money.

**Fix.** Split the two questions. Forgery is now a self-consistency test —
`residual == computed_net - observed_credit` must hold, and it cannot fail on
a proof this code produced. Everything else uses the record's amount, as
CLAUDE.md rule 3 requires, and classifies whatever is left over. The
disagreement is still recorded, as evidence on the proof rather than as a
verdict.

**The number that got worse.** The verifier lost a check. It no longer refuses
a proof merely for disagreeing with the current bank credit, so a stale proof
carried over from an earlier run now reports a residual instead of being
rejected outright. That is one fewer place the system says no, and it is the
right trade: `--tamper bnk_0007` now says *"₹412.00 unexplained"* instead of
*"forged"*, which is the sentence a finance team can act on.

---

## 2026-09-04 — Every model-proposed match was silently thrown away

**Symptom.** With a stub adjudicator that always names the record which
actually balances, the verifier accepted three hypotheses — and the match rate
did not move. 16 accepted matches with the model on, 16 with it off.

**Diagnosis.** `R5_LLM_HYPOTHESIS` scored a base of 0.70. Plus the
zero-residual bonus that makes 0.72, and the abstention threshold is 0.75. So
every hypothesis the verifier had *already balanced to exactly zero paise* was
then abstained on confidence grounds. The layer was structurally inert, and the
report line I was most pleased with — *"the model proposed N, the verifier kept
M"* — was measuring nothing, because nothing the model proposed could ever
become a match no matter how right it was.

This is the failure mode the project is most exposed to, in reverse. I spent
all my attention on making sure the model could not sneak a match past the
verifier, and did not check that a *correct* model answer could get through it.

**Fix.** `R5_LLM_HYPOTHESIS` base 0.75, so a verified hypothesis lands on 0.77
after the bonus. That is deliberately the lowest score the system will accept
and it sits one notch above the default threshold, so raising `--abstain-below`
at all removes every model-proposed match from the scorecard.

**The number that got worse.** The lowest confidence attached to an accepted
match fell from 0.78 (a subset match with no rival grouping) to 0.77, and three
matches in the `--llm` run now rest on a record a model chose. They still only
count because the verifier closed the identity to zero paise — but the weakest
thing in the batch is now something an LLM suggested, and the scorecard should
say so.

---

## 2026-09-04 — The committed scorecard was measured on data I had corrupted

**Symptom.** `RESULTS.md` claimed a `₹1.00` `UNEXPLAINED_RESIDUAL` on `bnk_0001`
and a `₹1.00` overpayment on `ord_0003`, and reported a 63.0% match rate with
`CLEAN` at 4 of 5. None of it reproduced. A fresh `--seed 42` run gave 66.7%,
`CLEAN` 5 of 5, and no such exceptions anywhere.

**Diagnosis.** `--tamper` shifts one source amount by 100 paise and re-verifies,
and then the run writes `report.json` and `RESULTS.md` exactly as it always
does. So a demo run of `--tamper pay_0003` had quietly overwritten the
committed scorecard with numbers measured on data I had deliberately broken.
`ord_0003` is the order behind `pay_0003`, and `bnk_0001` is the credit whose
settlement contains it — the two phantom exceptions were the same 100 paise,
seen from both loops. The evidence was right there in the report and I had read
past it twice, because ₹1.00 is small and a reconciliation tool is *supposed*
to produce residuals.

**Fix.** A tampered run now writes `report.tampered.json` and
`RESULTS.tampered.md`, both gitignored, and says so on stdout. Overwriting the
real artefacts requires passing `--out`/`--results` explicitly.

**The number that got worse.** None — and that is the uncomfortable part. The
correction moved the headline match rate *up*, from 63.0% to 66.7%. I would
rather have caught this as a number that fell. What actually got worse is my
confidence in every figure I had quoted before running the check: the honest
statement is not "the match rate is 66.7%", it is "the match rate is 66.7% and
for several hours I was quoting 63.0% from a measurement I had broken myself
and not noticed."

---

## 2026-09-04 — "Zero false matches" was hiding a coin flip

**Symptom.** Writing up the cost of abstention, I wanted the line *"abstaining costs 7
points of match rate and buys a false-match rate of zero."* So I measured it:
`--abstain-below 0` gives 74.1% instead of 66.7% — and **zero false matches either
way**. The sentence I was about to publish was not true.

**Diagnosis.** The two extra credits at threshold 0 are the `AMOUNT_COLLISION` pair:
two settlements on the same date with identical nets and no readable UTR. Building all
four candidate pairings and verifying each one shows why the metric looked clean —
**all four balance to exactly zero paise.** Two of them are wrong. With the 0.50
competition cap removed, both proofs become eligible, member exclusivity has to break
the tie, and it breaks it by sorting proof IDs. On this seed that ordering happens to
select the correct permutation. The system guessed, got it right, and the false-match
counter recorded a success.

That is the exact failure the track brief warns about — *one cherry-picked match proves
nothing* — arriving through the metric rather than through the demo. A different seed
flips a coin and my headline number becomes a lie.

**Fix.** No code change; the cap was already doing the right thing. What was missing was
evidence, so `TestAmbiguityIsNotResolvedByLuck` now asserts all four permutations
verify PASS, that exactly two of them are wrong, that the system abstains on both
credits at the default threshold, and that at threshold 0 the accepted proofs carry a
confidence of exactly 0.50. And `ARCHITECTURE.md` now states the trade-off as measured
rather than as assumed.

**The number that got worse.** The claim did. "Abstention buys a false-match rate of
zero" became "abstention costs 7.4 points of match rate and buys nothing measurable on
this batch — what it buys is the refusal to answer a question the data cannot answer."
That is a weaker sentence and a true one. The honest version of the argument for
abstaining is not that it improves a metric; it is that the metric could not have told
me the difference.
