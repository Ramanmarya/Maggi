"""
Zone re-arm tests.

The defect being fixed: §5 spends a zone until the ladder recenters on a new
high, so a one-directional decline consumes every zone in its first few
percent and the engine then does nothing for the rest of it — measured at the
2025-04-08 trough as 5/5 zones filled, target 3.32 units, held 1.00, zero
spreads written across the whole Feb–May 2025 decline.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from qqq.config import StrategyConfig
from qqq.ladder import AcquisitionLadder
from qqq.state import PortfolioState

REF, ATR = 600.0, 6.0
DAY = date(2025, 3, 3)


def _setup(rearm_days: int, trade_at_reference: bool = False):
    config = replace(
        StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y"),
        ladder_zone_rearm_days=rearm_days,
        ladder_trade_at_reference=trade_at_reference,
        ladder_atr_multipliers=(0.0, 1.5, 3.0, 5.0),
    )
    ladder = AcquisitionLadder(broker=None, config=config)
    state = PortfolioState()
    state = ladder.maybe_recenter(state, REF, ATR)
    return ladder, state, config


def test_zones_are_built_below_the_reference():
    ladder, state, _ = _setup(0)
    assert state.acquisition_ladder == [600.0, 591.0, 582.0, 570.0]


def test_an_unused_zone_is_offered_when_price_reaches_it():
    ladder, state, _ = _setup(0)
    assert ladder.unused_zone_at_or_below(state, 590.0, DAY) == 591.0


def test_shallowest_zone_first_on_a_gap_down():
    """§5: gradual accumulation, not the deepest zone in one session."""
    ladder, state, _ = _setup(0)
    assert ladder.unused_zone_at_or_below(state, 565.0, DAY) == 591.0


def test_with_rearm_disabled_a_used_zone_stays_spent_forever():
    """The source doc's behaviour, preserved as the default."""
    ladder, state, _ = _setup(0)
    state = ladder.mark_zone_filled(state, 591.0, DAY)
    assert ladder.unused_zone_at_or_below(state, 590.0, DAY + timedelta(days=3650)) is None


def test_a_used_zone_rearms_after_the_cooldown():
    ladder, state, _ = _setup(10)
    state = ladder.mark_zone_filled(state, 591.0, DAY)
    assert ladder.unused_zone_at_or_below(state, 590.0, DAY + timedelta(days=10)) == 591.0


def test_a_used_zone_is_still_spent_before_the_cooldown():
    """Otherwise 'gradual' is lost and the engine writes daily."""
    ladder, state, _ = _setup(10)
    state = ladder.mark_zone_filled(state, 591.0, DAY)
    assert ladder.unused_zone_at_or_below(state, 590.0, DAY + timedelta(days=9)) is None


def test_deeper_zones_stay_available_while_a_shallow_one_cools_off():
    ladder, state, _ = _setup(10)
    state = ladder.mark_zone_filled(state, 591.0, DAY)
    assert ladder.unused_zone_at_or_below(state, 565.0, DAY + timedelta(days=1)) == 582.0


def test_all_zones_spent_leaves_nothing_until_they_rearm():
    """Reproduces the measured 2025-04-08 state: 5/5 filled, nothing offered."""
    ladder, state, _ = _setup(10)
    for zone in state.acquisition_ladder:
        state = ladder.mark_zone_filled(state, zone, DAY)
    assert ladder.unused_zone_at_or_below(state, 500.0, DAY + timedelta(days=5)) is None
    assert ladder.unused_zone_at_or_below(state, 500.0, DAY + timedelta(days=10)) is not None


def test_recentering_on_a_new_high_clears_every_fill_record():
    ladder, state, _ = _setup(10)
    state = ladder.mark_zone_filled(state, 591.0, DAY)
    state = ladder.maybe_recenter(state, REF + 5 * ATR, ATR)
    assert state.filled_zones == []
    assert state.zone_filled_on == {}


def test_missing_fill_date_does_not_rearm_a_zone():
    """Old state files carry filled_zones with no dates. Fail closed."""
    ladder, state, _ = _setup(10)
    state.filled_zones.append(591.0)  # no zone_filled_on entry
    assert ladder.unused_zone_at_or_below(state, 590.0, DAY + timedelta(days=365)) is None


def test_reference_level_is_excluded_unless_opted_in():
    ladder, state, _ = _setup(0, trade_at_reference=False)
    assert ladder.unused_zone_at_or_below(state, REF, DAY) is None
    ladder, state, _ = _setup(0, trade_at_reference=True)
    assert ladder.unused_zone_at_or_below(state, REF, DAY) == REF
