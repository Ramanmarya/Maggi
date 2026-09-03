import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qqq.config import StrategyConfig
from qqq.exposure_curve import decline_from_reference, target_units_for_decline


def make_config() -> StrategyConfig:
    return StrategyConfig(alpaca_api_key="x", alpaca_secret_key="y")


def test_decline_from_reference():
    assert decline_from_reference(400.0, 380.0) == 0.05
    assert decline_from_reference(400.0, 420.0) == 0.0  # clamped, no negative decline
    assert decline_from_reference(None, 380.0) == 0.0


# ALGORITHM.md §6 as published. The live curve in rules.json is operator-
# tunable and has been lifted from this baseline, so the doc-conformance test
# pins the doc's own numbers rather than whatever is currently configured —
# otherwise tuning the strategy silently rewrites the specification.
DOC_TABLE = {0.00: 1.0, 0.05: 1.5, 0.10: 2.0, 0.15: 2.5, 0.20: 3.0, 0.25: 3.125, 0.30: 3.25}


def test_target_units_matches_doc_table():
    config = replace(make_config(), exposure_curve=dict(DOC_TABLE))
    for decline, expected in DOC_TABLE.items():
        assert target_units_for_decline(config, decline) == expected


def test_target_units_interpolates_between_points():
    config = replace(make_config(), exposure_curve=dict(DOC_TABLE))
    mid = target_units_for_decline(config, 0.075)  # halfway between 5% and 10%
    assert 1.5 < mid < 2.0


def test_live_curve_is_monotonic_and_starts_at_or_above_core():
    """Whatever the operator has tuned it to, the curve must still make sense.

    It has to rise with the decline — a curve that wants less exposure the
    further price falls would invert the whole accumulation thesis — and it
    must never target less than the core unit, which the strategy holds
    permanently and never sells.
    """
    config = make_config()
    points = sorted(config.exposure_curve.items())
    assert points[0][1] >= 1.0, "target at the highs is below the permanent core"
    values = [v for _, v in points]
    assert values == sorted(values), f"exposure curve is not monotonic: {values}"


def test_target_units_clamps_beyond_table():
    config = make_config()
    assert target_units_for_decline(config, 0.50) == target_units_for_decline(config, 0.30)
