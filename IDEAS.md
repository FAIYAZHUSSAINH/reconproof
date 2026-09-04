# Ideas, deliberately not built

Things that came up during the build and were pushed here instead of into the
code. Each one has a reason for being out of scope, because "we ran out of
time" and "this would have made the system worse" are different answers and a
panel is entitled to know which one applies.

## Would strengthen the evidence

**A generator that varies the case mix by seed.** Right now `--seed` changes
amounts, dates, customers and narrations but not *how many* of each case type
the batch contains, so ten seeds produce ten identical match rates. That makes
a seed sweep useless as a robustness argument (see `README.md`, "It falls where
it should"). Making the mix seed-dependent would let the scorecard report a
mean and a spread rather than a single figure. Not done because it changes the
committed batch, and re-baselining every number in the repo on the last day is
how you end up quoting a scorecard you have not actually read.

**Confidence calibration that is fitted rather than measured.** The calibration
table compares confidence buckets against whether the proof was right. It is
honest but it is not a fit: nothing feeds the observed accuracy back into the
scores. With more labelled batches, the rule weights in `confidence.py` could
be set from data instead of by argument.

**Precision and recall per reason code.** Diagnosis accuracy is currently one
aggregate number. Knowing that `SUSPECT_UNLABELLED_REVERSAL` is right 100% of
the time and `UNEXPLAINED_RESIDUAL` is a bucket of last resort is more useful
to whoever works the queue.

## Would extend the domain

**Let the model see unassigned payments, not only refunds and adjustments.**
`_apply_hypothesis` drops any proposed ID that is not a refund or an
adjustment, so the model cannot add a payment to a settlement. That is a
deliberate line: payment membership is a matter of record, not of inference.
Relaxing it would help with split settlements — and it would also be the first
place a hallucinated match could do real damage, so it should only happen with
a matching-side rule that the verifier can check independently.

**Duplicate capture detection across orders.** Loop B compares each order
against its own payments. It cannot see the same card charged twice against two
different orders, which is a real and expensive failure.

**Multi-currency and cross-border settlements.** The whole money layer is
single-currency integer paise. Adding a currency would mean the identity has to
carry an FX rate and a conversion date, and every term would need a currency
tag before the verifier could recompute anything.

**A real settlement report parser.** The generator writes CSVs modelled on the
structure of a public Razorpay settlement report. Reading an actual export
would be the honest next step, and it is a different project: the reconciliation
core would not change, the ingest layer would.

## Deliberately rejected

**Auto-accepting rounding drift.** A one-paisa residual is still a residual.
`--tolerance-paise 5` exists so a human can make that policy call explicitly;
the default is 0 and should stay 0.

**Resolving `AMOUNT_COLLISION` by picking the earlier settlement.** It would
raise the match rate by two credits and be right about half the time. The
system has no evidence for either assignment — that is what `AMBIGUOUS_MATCH`
is for.

**A component library for the dashboard.** The UI is tables and rules. A kit
would have fought the design and added a build step, and the no-Node constraint
is worth more than the components.
