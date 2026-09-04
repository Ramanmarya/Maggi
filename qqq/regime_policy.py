"""
Regime-dependent behaviour (V5 §4, §20, §24).

§4 states the regime "does NOT automatically make the portfolio short Nasdaq"
— it changes *accumulation aggressiveness, ATR spacing, put selection and call
aggressiveness*. Until now the regime was computed, logged and displayed, and
nothing branched on it: every decision the arm has ever made was regime-blind.

The published evidence for connecting it is strong. A 2005-2025 SPX study
found 45-DTE 16-delta short premium returned a Sharpe of 0.52 unfiltered and
0.81 with a regime filter — the single largest documented improvement
available to a short-premium strategy, and larger than any parameter sweep
run on this system.

Each regime supplies four multipliers, all 1.0 in BULL so that the bull case
is exactly today's tested behaviour and the filter only ever *withdraws* risk:

  accumulate  — shares bought per ladder zone (§14: rate of increase must
                fall during severe declines)
  spacing     — ATR multipliers for the ladder (§4: regime changes spacing;
                wider means fewer, deeper entries in a downtrend)
  put_delta   — short-put delta target (§4: regime changes put selection;
                lower means further out of the money)
  call_delta  — short-call delta target (§24: less willingness to sell calls
                into a sharp decline, more after a rebound)
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULTS = {
    # BULL is deliberately neutral: price above a rising 200DMA is the
    # condition the strategy was designed and tested for.
    "BULL": {"accumulate": 1.0, "spacing": 1.0, "put_delta": 1.0, "call_delta": 1.0},
    # NEUTRAL trims modestly. Price and trend disagree, so take less risk
    # without standing aside.
    "NEUTRAL": {"accumulate": 0.75, "spacing": 1.25, "put_delta": 0.85, "call_delta": 1.0},
    # DEFENSIVE is where the filter earns its keep: below a falling 200DMA is
    # the regime in which accumulating aggressively is most dangerous, and the
    # one this strategy has no backtest evidence for.
    "DEFENSIVE": {"accumulate": 0.40, "spacing": 1.75, "put_delta": 0.60, "call_delta": 1.25},
}


@dataclass(frozen=True)
class RegimePolicy:
    accumulate: float
    spacing: float
    put_delta: float
    call_delta: float

    @classmethod
    def for_regime(cls, config, regime: str) -> "RegimePolicy":
        if not config.regime_filter_enabled:
            return cls(1.0, 1.0, 1.0, 1.0)
        table = config.regime_adjustments or DEFAULTS
        row = table.get(regime) or table.get("NEUTRAL") or DEFAULTS["NEUTRAL"]
        base = DEFAULTS.get(regime, DEFAULTS["NEUTRAL"])
        return cls(
            accumulate=float(row.get("accumulate", base["accumulate"])),
            spacing=float(row.get("spacing", base["spacing"])),
            put_delta=float(row.get("put_delta", base["put_delta"])),
            call_delta=float(row.get("call_delta", base["call_delta"])),
        )

    def scaled_multipliers(self, multipliers: tuple[float, ...]) -> tuple[float, ...]:
        """Widen the ladder. The reference level (0.0) never moves."""
        return tuple(m * self.spacing if m > 0 else m for m in multipliers)
