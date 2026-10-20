"""The spend guard. A question starts only when the spend so far, plus the estimate of the
questions in flight, plus this question's estimate (every arm asked + one judge call each)
stays under --max-usd. Nothing paid is ever retried.

The spend counts each answer's measured cost wherever there is one, else its estimate:
  A, A2   measured: the cost in luccy's run record for the answer (serving/pricing.py prices)
  C       measured: the eval reply's `cost_usd` when its `cost_source` is "measured";
          else that `cost_usd` as an estimate, else luccy's estimate + CHAT_OVERHEAD_USD
  judge   measured from the judge's token usage, priced with serving/pricing.py
  refused a reply refused before any work (arms.FREE_STATUSES) cost nothing
Only measured costs of answers that came back feed the estimates: after three of them an
arm's estimate is their mean; before that its prior.
"""
from __future__ import annotations

import statistics
import threading

# USD per answer before anything is measured. luccy: its gold baseline is $0.046 a question,
# rounded up.
PRIOR_USD = {"luccy": 0.06, "judge": 0.003}
# DataGov's chat model turns around the luccy tool call (arm C), on top of luccy's own answer.
CHAT_OVERHEAD_USD = 0.03
MIN_SAMPLES = 3


def cost_key(arm: str) -> str:
    return "luccy" if arm in ("A", "A2") else arm


class Budget:
    def __init__(self, max_usd: float | None):
        self.max_usd = max_usd
        self.spent = 0.0
        self.inflight = 0.0
        self.samples: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _measured(self, key: str) -> float | None:
        values = self.samples.get(key) or []
        return statistics.fmean(values) if len(values) >= MIN_SAMPLES else None

    def _estimate_key(self, key: str) -> float:
        measured = self._measured(key)
        if measured is not None:
            return measured
        if key == "C":
            return self._estimate_key("luccy") + CHAT_OVERHEAD_USD
        return PRIOR_USD[key]

    def estimate_arm(self, arm: str) -> float:
        with self.lock:
            return self._estimate_key(cost_key(arm))

    def estimate(self, arms) -> float:
        with self.lock:
            return sum(self._estimate_key(cost_key(a)) + self._estimate_key("judge") for a in arms)

    def reserve(self, arms) -> float | None:
        """The estimate held for one question, or None when it would pass the cap."""
        need = self.estimate(arms)
        with self.lock:
            if self.max_usd is not None and self.spent + self.inflight + need > self.max_usd:
                return None
            self.inflight += need
            return need

    def settle(self, reserved: float, costs) -> None:
        """`costs`: (key, usd, measured) triples; only measured costs feed the estimates."""
        with self.lock:
            self.inflight = max(0.0, self.inflight - reserved)
            for key, usd, measured in costs:
                self.spent += usd
                if measured:
                    self.samples.setdefault(key, []).append(usd)
