import sys
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


def test_target_units_matches_doc_table():
    config = make_config()
    assert target_units_for_decline(config, 0.00) == 1.0
    assert target_units_for_decline(config, 0.05) == 1.5
    assert target_units_for_decline(config, 0.10) == 2.0
    assert target_units_for_decline(config, 0.15) == 2.5
    assert target_units_for_decline(config, 0.20) == 3.0


def test_target_units_interpolates_between_points():
    config = make_config()
    mid = target_units_for_decline(config, 0.075)  # halfway between 5% and 10%
    assert 1.5 < mid < 2.0


def test_target_units_clamps_beyond_table():
    config = make_config()
    assert target_units_for_decline(config, 0.50) == target_units_for_decline(config, 0.30)
