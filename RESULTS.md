# RESULTS

Committed scorecard for `--seed 42 --difficulty standard --no-llm`. Reproduce it with that command; the generator is seeded, so these numbers are the numbers you will get - every line below except the throughput row, which is wall-clock and depends on the machine.

## Headline

| Metric | Value |
| --- | --- |
| Auto-match rate (bank credits) | **66.7%** (18/27) |
| Value coverage | **69.8%** (₹12,49,287.65 of ₹17,90,945.93) |
| **False-match rate** | **0.0%** (0 of 16 accepted) |
| Settlements closed | 66.7% (18/27) |
| Exceptions filed | 12 (₹2,98,223.05 at stake) |
| Exception coverage | 100.0% of unmatched credits carry a typed reason |
| Diagnosis accuracy | 100.0% (12/12 unmatched units given the right reason code) |
| Handled correctly | 100.0% (30/30 labelled units either proved or correctly flagged) |
| Throughput | 366 records in 0.011s (31930.5 records/sec) |
| LLM adjudication | disabled (`--no-llm`) |

## Across difficulty

One number on one batch proves nothing. These rows are the same pipeline re-run on `--difficulty easy|standard|hard`, measured during this run rather than typed in. The match rate falls as the hard cases multiply; the false-match rate does not move. (The seed varies amounts, dates and narrations but not the mix of case types, so varying it gives identical rates - which confirms determinism and says nothing about robustness.)

| Difficulty | Records | Match rate | Value coverage | Exceptions | False matches |
| --- | ---: | ---: | ---: | ---: | ---: |
| easy | 474 | 72.7% (24/33) | 78.8% | 12 | **0** |
| standard *(this run)* | 366 | 66.7% (18/27) | 69.8% | 12 | **0** |
| hard | 465 | 54.5% (18/33) | 64.9% | 18 | **0** |

## Per case type

The point of this table is the rows at the bottom. Every case type is injected at least twice, so a low rate here is a statement about the system, not a sampling artefact.

| Case type | Expected | Units | Matched | Exceptions | Correctly diagnosed | Handled |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| AMOUNT_COLLISION | match | 2 | 0 | 2 | 2 | 100.0% |
| CHARGEBACK_DEDUCTION | match | 2 | 2 | 0 | 0 | 100.0% |
| CLEAN | match | 5 | 5 | 0 | 0 | 100.0% |
| DUPLICATE_UTR | match | 2 | 2 | 0 | 0 | 100.0% |
| MERGED_CREDIT | match | 2 | 2 | 0 | 0 | 100.0% |
| MISSING_BANK_CREDIT | exception | 2 | 0 | 2 | 2 | 100.0% |
| NARRATION_NOISE | match | 2 | 2 | 0 | 0 | 100.0% |
| ORPHAN_CREDIT | exception | 2 | 0 | 2 | 2 | 100.0% |
| ROUNDING_DRIFT | match | 2 | 0 | 2 | 2 | 100.0% |
| SHORT_PAYMENT | exception | 1 | 0 | 1 | 1 | 100.0% |
| SPLIT_SETTLEMENT | match | 2 | 2 | 0 | 0 | 100.0% |
| TDS_SHORT_PAYMENT | match | 2 | 2 | 0 | 0 | 100.0% |
| UNLABELLED_REVERSAL | match | 2 | 0 | 2 | 2 | 100.0% |
| WITH_REFUND | match | 2 | 1 | 1 | 1 | 100.0% |

## Exceptions by reason

| Reason code | Count | At stake |
| --- | ---: | ---: |
| MISSING_BANK_CREDIT | 2 | ₹1,42,403.11 |
| AMBIGUOUS_MATCH | 2 | ₹1,31,575.10 |
| SUSPECT_UNLABELLED_REVERSAL | 2 | ₹14,520.12 |
| ORPHAN_CREDIT | 2 | ₹7,087.47 |
| SUSPECT_UNAPPLIED_REFUND | 1 | ₹2,617.36 |
| SHORT_PAYMENT | 1 | ₹19.87 |
| ROUNDING_DRIFT | 2 | ₹0.02 |

## The exception list

Sorted by rupees at stake. This is the queue a human works through.

| Subject | Reason | Residual | At stake | What to check |
| --- | --- | ---: | ---: | --- |
| `set_0025` | MISSING_BANK_CREDIT | ₹90,331.72 | ₹90,331.72 | The gateway settled set_0025 but no credit has landed. Expected ₹90,331.72 on or after the settlement date - chase the bank if it is more than one working day late. |
| `bnk_0018` | AMBIGUOUS_MATCH | ₹0.00 | ₹65,787.55 | More than one settlement balances against this credit (set_0016, set_0017) and the narration carries no readable UTR. Ask the bank for the full reference; amount and date cannot separate these. |
| `bnk_0019` | AMBIGUOUS_MATCH | ₹0.00 | ₹65,787.55 | More than one settlement balances against this credit (set_0016, set_0017) and the narration carries no readable UTR. Ask the bank for the full reference; amount and date cannot separate these. |
| `set_0011` | MISSING_BANK_CREDIT | ₹52,071.39 | ₹52,071.39 | The gateway settled set_0011 but no credit has landed. Expected ₹52,071.39 on or after the settlement date - chase the bank if it is more than one working day late. |
| `bnk_0030` | SUSPECT_UNLABELLED_REVERSAL | (₹9,745.94) | ₹9,745.94 | The residual equals adjustment adj_0003, adj_0004, which carries no payment reference. Check whether a won chargeback was credited back in this payout. |
| `bnk_0015` | SUSPECT_UNLABELLED_REVERSAL | (₹4,774.18) | ₹4,774.18 | The residual equals adjustment adj_0001, adj_0002, which carries no payment reference. Check whether a won chargeback was credited back in this payout. |
| `bnk_0031` | ORPHAN_CREDIT | (₹4,763.95) | ₹4,763.95 | No settlement matches this credit and the narration carries no UTR. It looks like a direct transfer from a customer - check the receivables ledger, not the gateway. |
| `bnk_0020` | SUSPECT_UNAPPLIED_REFUND | ₹2,617.36 | ₹2,617.36 | The residual equals refund rfnd_0002 exactly. Check whether the gateway netted that refund off this payout while your books date it to an earlier cycle. |
| `bnk_0017` | ORPHAN_CREDIT | (₹2,323.52) | ₹2,323.52 | No settlement matches this credit and the narration carries no UTR. It looks like a direct transfer from a customer - check the receivables ledger, not the gateway. |
| `ord_0033` | SHORT_PAYMENT | ₹19.87 | ₹19.87 | Order ord_0033 was paid short by ₹19.87 and is not a B2B invoice, so TDS does not explain it. Check for a partial payment or a failed retry. |
| `bnk_0011` | ROUNDING_DRIFT | ₹0.01 | ₹0.01 | Residual of ₹0.01. GST appears to have been rounded on the batch fee instead of per payment. Re-run with --tolerance-paise 5 to accept differences this size. |
| `bnk_0026` | ROUNDING_DRIFT | (₹0.01) | ₹0.01 | Residual of ₹0.01. GST appears to have been rounded on the batch fee instead of per payment. Re-run with --tolerance-paise 5 to accept differences this size. |

## Calibration

Every proof that balanced, bucketed by confidence, against whether it was actually right. The bucket at the abstention cap is the argument for abstaining.

| Confidence | Proofs | Correct | Accuracy | Accepted |
| --- | ---: | ---: | ---: | --- |
| 0.50-0.70 | 4 | 2 | 50.0% | no (abstained) |
| 0.70-0.85 | 3 | 3 | 100.0% | yes |
| 0.85-0.95 | 2 | 2 | 100.0% | yes |
| 0.95-1.00 | 11 | 11 | 100.0% | yes |

## Loop B - order versus payment

151 orders checked, 3 paid short: 2 explained as tax withheld at source, 1 filed as exceptions.

## Run detail

```
seed              42
difficulty        standard
abstain below     0.75
tolerance (paise) 0
records           366
candidates        26
proofs            26 (20 passed, 6 failed)
reversal pairs    2 neutralised
subset cap hits   0
python            3.12.10 on Windows
```
