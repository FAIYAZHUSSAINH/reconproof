"""The optional LLM layer: a proposer, never a decider.

The model is given one unexplained residual and a short list of records the
deterministic cycle rule could not place, and asked a single question: which of
these, if any, would make this payout balance?

It cannot answer "matched". It can only name records. Whatever it names is
rebuilt into a proof by `proof.py` and recomputed by `verify.py` against the
raw data, and it is accepted only if the arithmetic comes out at zero paise.
The report states how many of its hypotheses survived that check, because that
number - not a vibe about model quality - is the measure of how much the model
is worth here.

Nothing in this file is load-bearing. With `--no-llm`, or with no API key, the
pipeline runs end to end and prints a full scorecard.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_CACHE = Path(".llm_cache.json")

SYSTEM_PROMPT = """\
You are a reconciliation analyst for an Indian payment gateway. A bank credit
does not match the sum of its settlement's line items, and some refund and
adjustment records could not be assigned to any settlement by date.

Your only job is to propose which of the listed unassigned records, if any,
belong to this payout. You do not decide anything: a deterministic verifier
recomputes the whole ledger identity from the raw records and rejects your
proposal unless it balances to exactly zero paise.

Rules:
- All amounts are integer paise.
- The identity is: gross payments - fee - GST on fee - refunds
  - chargeback deductions + chargeback reversals = the bank credit.
- A positive residual means the computed net is too high, so something that
  should have been deducted is missing (usually a refund or a chargeback).
- A negative residual means the computed net is too low, so something that
  should have been added is missing (usually a chargeback reversal).
- Propose at most 3 records, and only IDs from unassigned_candidates.
- If nothing on the list explains the residual, return an empty list. An empty
  answer is a correct answer, and it is better than a wrong one.

Reply with JSON only, in this shape:
{"add_members": ["rfnd_0044"], "reason": "one short sentence"}
"""


@dataclass
class Hypothesis:
    add_members: list[str]
    reason: str
    model: str


class AdjudicatorUnavailable(RuntimeError):
    """No API key, or the groq client is not installed."""


class Adjudicator:
    """A thin, cached wrapper around one Groq chat completion."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        cache_path: Path | str = DEFAULT_CACHE,
        temperature: float = 0.0,
    ) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise AdjudicatorUnavailable(
                "GROQ_API_KEY is not set. Run with --no-llm for the full "
                "deterministic scorecard."
            )
        try:
            from groq import Groq
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise AdjudicatorUnavailable(
                "the groq package is not installed; `pip install groq` or run "
                "with --no-llm"
            ) from exc

        self.model = model
        self.temperature = temperature
        self.cache_path = Path(cache_path)
        self.cache = self._load_cache()
        self.calls = 0
        self.cache_hits = 0
        self._client = Groq(api_key=key)

    # -- cache ------------------------------------------------------------

    def _load_cache(self) -> dict[str, str]:
        if self.cache_path.exists():
            try:
                with self.cache_path.open(encoding="utf-8") as handle:
                    return json.load(handle)
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_cache(self) -> None:
        with self.cache_path.open("w", newline="", encoding="utf-8") as handle:
            json.dump(self.cache, handle, indent=2, sort_keys=True)
            handle.write("\n")

    # -- the call ---------------------------------------------------------

    def propose(self, context: dict) -> Hypothesis | None:
        """Ask for one hypothesis about one residual."""
        prompt = json.dumps(context, indent=2, sort_keys=True)
        key = hashlib.sha256(
            f"{self.model}|{self.temperature}|{SYSTEM_PROMPT}|{prompt}".encode()
        ).hexdigest()

        if key in self.cache:
            self.cache_hits += 1
            raw = self.cache[key]
        else:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = response.choices[0].message.content or ""
            self.calls += 1
            self.cache[key] = raw
            self._save_cache()

        return parse_hypothesis(raw, context, self.model)


def parse_hypothesis(raw: str, context: dict, model: str) -> Hypothesis | None:
    """Parse and constrain the model's answer.

    Two constraints, applied before the verifier ever sees it: the reply has to
    be JSON in the shape asked for, and every ID has to be one that was
    actually offered. A model that invents `rfnd_9999` gets nothing added; a
    model that names a record from outside the window does not get to reach
    into the dataset through this door.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None

    offered = {entry["id"] for entry in context.get("unassigned_candidates", [])}
    proposed = payload.get("add_members") or []
    if not isinstance(proposed, list):
        return None

    allowed = [
        record_id
        for record_id in proposed
        if isinstance(record_id, str) and record_id in offered
    ]
    reason = str(payload.get("reason", ""))[:300]
    if not allowed:
        return None
    return Hypothesis(add_members=allowed, reason=reason, model=model)


def build_adjudicator(
    enabled: bool,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    cache_path: Path | str = DEFAULT_CACHE,
) -> Adjudicator | None:
    """Return an adjudicator, or None if the layer is off or unavailable."""
    if not enabled:
        return None
    return Adjudicator(api_key=api_key, model=model, cache_path=cache_path)
