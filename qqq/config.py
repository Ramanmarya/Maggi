"""
StrategyConfig — every tunable for the QQQ drift-harvest arm.

Values resolve in this order, first match wins:
    1. environment variable (for secrets and one-off overrides)
    2. rules.json  (the operator-facing tuning surface)
    3. the dataclass default below

Secrets come from the environment only; strategy parameters live in
rules.json so a tuning change is a reviewable diff of a data file rather
than a code edit. The .env loader is stdlib-only on purpose: the pure-logic
modules (risk, ladder, exposure curve, regime) must import and unit-test
with no third-party packages installed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = Path(os.getenv("QQQ_RULES_PATH", PROJECT_ROOT / "qqq" / "rules.json"))
ENV_PATH = PROJECT_ROOT / ".env"


def _load_dotenv(path: Path = ENV_PATH) -> None:
    """Minimal stdlib .env loader. Existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv()


def _rules() -> dict:
    try:
        with RULES_PATH.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


_R = _rules()


def _rule(section: str, key: str, default):
    return _R.get(section, {}).get(key, default)


def _env_or(name: str, value, cast=None):
    raw = os.getenv(name)
    if raw is None:
        return value
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw) if cast else raw


def _first_env(*names: str, default: str = "") -> str:
    """Return the first environment variable that is set and non-empty.

    Accepts both this project's ALPACA_* names and Alpaca's own APCA_*
    names so an existing .env can be reused verbatim.
    """
    for n in names:
        val = os.getenv(n)
        if val:
            return val
    return default


def _exposure_curve() -> dict:
    raw = _R.get("exposure_curve", {}).get("points")
    if not raw:
        return {0.00: 1.0, 0.05: 1.5, 0.10: 2.0, 0.15: 2.5, 0.20: 3.0, 0.25: 3.125, 0.30: 3.25}
    return {float(k): float(v) for k, v in raw.items()}


@dataclass(frozen=True)
class StrategyConfig:
    # --- Identity / instrument (§2) ---
    symbol: str = field(default_factory=lambda: _rule("instrument", "symbol", "QQQ"))
    core_unit_shares: int = field(
        default_factory=lambda: _env_or("CORE_UNIT_SHARES", _rule("instrument", "core_unit_shares", 100), int)
    )
    # How many units the permanent core holds. 1.0 is ALGORITHM.md §3. 0
    # makes this a pure premium-selling program with no Nasdaq exposure —
    # a different strategy, not a tuning of this one.
    core_units_target: float = field(
        default_factory=lambda: float(_rule("instrument", "core_units", 1.0))
    )

    # --- Alpaca connection ---
    alpaca_api_key: str = field(
        default_factory=lambda: _first_env("ALPACA_API_KEY", "APCA_API_KEY_ID")
    )
    alpaca_secret_key: str = field(
        default_factory=lambda: _first_env("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")
    )
    # Hardcoded paper endpoint as a safety rail. Going live is a deliberate
    # code change here, not an env-var flip.
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    # --- Polygon connection (backtest only) ---
    polygon_api_key: str = field(default_factory=lambda: os.getenv("POLYGON_API_KEY", ""))

    # --- Cadence (§10) ---
    intraday_check_interval_minutes: int = field(
        default_factory=lambda: _rule("cadence", "intraday_check_interval_minutes", 15)
    )

    # --- Persistence ---
    state_file_path: Path = field(
        default_factory=lambda: Path(os.getenv("STATE_FILE_PATH", PROJECT_ROOT / "state" / "qqq_state.json"))
    )

    # --- Ladder (§5) ---
    atr_period_days: int = field(default_factory=lambda: _rule("ladder", "atr_period_days", 20))
    recenter_trigger_atr_mult: float = field(
        default_factory=lambda: _rule("ladder", "recenter_trigger_atr_mult", 0.5)
    )
    ladder_atr_multipliers: tuple[float, ...] = field(
        default_factory=lambda: tuple(_rule("ladder", "atr_multipliers", [0.0, 1.5, 3.0, 5.0]))
    )
    # False keeps ALGORITHM.md 5 behaviour: the reference level marks "at the
    # highs" and is not itself a buy zone. True lets the engine sell premium
    # there. See rules.json:ladder._trade_at_reference_note.
    ladder_trade_at_reference: bool = field(
        default_factory=lambda: bool(_rule("ladder", "trade_at_reference", False))
    )
    # Days before a used zone becomes available again. 0 reproduces the
    # source doc: a zone is spent until the ladder recenters on a new high,
    # which in a one-directional decline means never. See rules.json.
    ladder_zone_rearm_days: int = field(
        default_factory=lambda: int(_rule("ladder", "zone_rearm_days", 0))
    )
    # Shares bought outright each time a ladder zone fires below target.
    # 0 disables accumulation entirely, which is the behaviour every backtest
    # up to now measured. See rules.json for why assignment cannot do this job.
    ladder_accumulate_shares_per_zone: int = field(
        default_factory=lambda: int(_rule("ladder", "accumulate_shares_per_zone", 0))
    )

    # --- Regime (§4) ---
    regime_slope_lookback_days: int = field(
        default_factory=lambda: _rule("regime", "slope_lookback_days", 20)
    )
    regime_filter_enabled: bool = field(
        default_factory=lambda: bool(_rule("regime", "filter_enabled", False))
    )
    regime_adjustments: dict = field(
        default_factory=lambda: _rule("regime", "adjustments", {}) or {}
    )
    regime_slope_flat_band: float = field(
        default_factory=lambda: _rule("regime", "slope_flat_band", 0.0)
    )

    # --- Target exposure curve (§6) ---
    exposure_curve: dict = field(default_factory=_exposure_curve)

    # --- Put engine (§7) ---
    put_spread_dte_range: tuple[int, int] = field(
        default_factory=lambda: tuple(_rule("put_engine", "dte_range", [21, 35]))
    )
    put_spread_short_delta_target: float = field(
        default_factory=lambda: _rule("put_engine", "short_delta_target", 0.20)
    )
    put_spread_protective_delta_target: float = field(
        default_factory=lambda: _rule("put_engine", "protective_delta_target", 0.05)
    )
    put_spread_max_risk_reward_ratio: float | None = field(
        default_factory=lambda: _rule("put_engine", "max_risk_reward_ratio", 10.0)
    )
    put_spread_profit_capture_pct: float = field(
        default_factory=lambda: _rule("put_engine", "profit_capture_pct", 0.60)
    )
    put_spread_close_dte: int = field(default_factory=lambda: _rule("put_engine", "close_dte", 3))
    put_spread_contracts: int = field(
        default_factory=lambda: int(_rule("put_engine", "contracts_per_signal", 1))
    )
    # How the protective leg is chosen when the 5-delta target does not fit
    # the max-loss cap: "widest" takes the most premium the cap allows,
    # "best_risk_reward" takes the best loss-to-profit ratio. They differ
    # because risk/reward is U-shaped in width, not monotonic.
    # False writes NAKED short puts — Rajat's structure, and explicitly not
    # part of V5's base strategy (§8: "Naked puts are NOT part of the base
    # strategy"). Exists only to answer §39 Q1: does the protective leg cost
    # more in premium than it saves in tail risk?
    # "spread" is V5 §8's default. "cash_secured" sells an unspread put with
    # the full strike value held in cash — NOT the naked put §8 excludes: same
    # maximum loss, but assignment is always affordable and the broker can
    # never liquidate the position mid-decline. It is also the only structure
    # that can deliver inventory, which Engines A and C both depend on.
    # "ladder"    — V5 §8: write only when price touches an unused zone.
    # "scheduled" — write on a cadence while below the target position count.
    # "both"      — either trigger fires.
    # The ladder alone pinned trade count at 79-84 regardless of expiry, so it,
    # not DTE, was always the limiter on premium collected.
    put_entry_mode: str = field(
        default_factory=lambda: str(_rule("put_engine", "entry_mode", "ladder"))
    )
    put_target_open_positions: int = field(
        default_factory=lambda: int(_rule("put_engine", "target_open_positions", 3))
    )
    put_min_days_between_entries: int = field(
        default_factory=lambda: int(_rule("put_engine", "min_days_between_entries", 7))
    )
    put_structure: str = field(
        default_factory=lambda: str(_rule("put_engine", "structure", "spread"))
    )
    put_protective_leg: bool = field(
        default_factory=lambda: bool(_rule("put_engine", "protective_leg", True))
    )
    put_spread_leg_selection: str = field(
        default_factory=lambda: str(_rule("put_engine", "protective_leg_selection", "widest"))
    )

    # --- Call engine (§8) ---
    call_dte_range: tuple[int, int] = field(
        default_factory=lambda: tuple(_rule("call_engine", "dte_range", [21, 35]))
    )
    call_short_delta_target: float = field(
        default_factory=lambda: _rule("call_engine", "short_delta_target", 0.20)
    )
    call_short_delta_target_post_rebound: float = field(
        default_factory=lambda: _rule("call_engine", "short_delta_target_post_rebound", 0.25)
    )
    call_profit_capture_pct: float = field(
        default_factory=lambda: _rule("call_engine", "profit_capture_pct", 0.60)
    )
    rebound_retracement_pct: float = field(
        default_factory=lambda: _rule("call_engine", "rebound_retracement_pct", 0.50)
    )
    # False is V5 §2/§19: the core unit is never capped, preserving upside.
    # True lets calls be written against total inventory including the core.
    call_cover_core: bool = field(
        default_factory=lambda: bool(_rule("call_engine", "cover_core", False))
    )
    call_min_effective_sale_vs_reference: float = field(
        default_factory=lambda: _rule("call_engine", "min_effective_sale_vs_reference", 1.0)
    )

    # --- Ex-dividend safety (§8, QQQ-specific) ---
    exdiv_min_extrinsic_over_dividend_ratio: float = field(
        default_factory=lambda: _rule("dividend", "min_extrinsic_over_dividend_ratio", 1.25)
    )

    # --- Risk hard caps (§9) ---
    max_loss_per_spread_pct: float = field(
        default_factory=lambda: _rule("risk", "max_loss_per_spread_pct", 0.01)
    )
    max_aggregate_put_risk_pct: float = field(
        default_factory=lambda: _rule("risk", "max_aggregate_put_risk_pct", 0.05)
    )
    max_crash_stress_pct: float = field(
        default_factory=lambda: _rule("risk", "max_crash_stress_pct", 0.15)
    )
    # §31 caps the loss under a -20% shock specifically. §30's other shocks
    # are simulated for information. Capping the WORST of them instead is
    # materially stricter than the specification.
    crash_stress_binding_shock: float = field(
        default_factory=lambda: float(_rule("risk", "crash_stress_binding_shock", -0.20))
    )
    crash_stress_shocks: tuple[float, ...] = field(
        default_factory=lambda: tuple(_rule("risk", "crash_stress_shocks", [-0.05, -0.10, -0.15, -0.20, -0.30]))
    )
    # When set, the risk gates size against this figure instead of live equity.
    # See rules.json:risk._equity_basis_note — this can authorise losses larger
    # than the account can absorb, so it is paper-only.
    equity_basis_override: float | None = field(
        default_factory=lambda: _rule("risk", "equity_basis_override", None)
    )

    # --- Platform-level breaker limits (not from the source doc) ---
    daily_loss_limit_pct: float = field(
        default_factory=lambda: _rule("risk", "daily_loss_limit_pct", 0.06)
    )
    max_drawdown_pct: float = field(default_factory=lambda: _rule("risk", "max_drawdown_pct", 0.25))

    # --- Idle-cash sweep (not from the source doc) ---
    cash_sweep_enabled: bool = field(
        default_factory=lambda: bool(_rule("cash_sweep", "enabled", False))
    )
    cash_sweep_symbol: str = field(
        default_factory=lambda: str(_rule("cash_sweep", "symbol", "SGOV"))
    )
    cash_sweep_buffer: float = field(
        default_factory=lambda: float(_rule("cash_sweep", "reserve_buffer", 5000.0))
    )
    # How many cash-secured positions the sweep must keep collateral for.
    # Only consulted when put_structure is "cash_secured".
    cash_secured_reserve_positions: int = field(
        default_factory=lambda: int(_rule("cash_sweep", "cash_secured_reserve_positions", 3))
    )
    cash_sweep_min_trade: float = field(
        default_factory=lambda: float(_rule("cash_sweep", "min_trade", 1000.0))
    )

    # --- Backtest-only (§11) ---
    backtest_risk_free_rate: float = field(
        default_factory=lambda: _rule("backtest", "risk_free_rate", 0.045)
    )
    backtest_dividend_yield_estimate: float = field(
        default_factory=lambda: _rule("backtest", "dividend_yield_estimate", 0.006)
    )
    backtest_chain_strike_band_pct: float = field(
        default_factory=lambda: _rule("backtest", "chain_strike_band_pct", 0.20)
    )
    polygon_max_workers: int = field(
        default_factory=lambda: _rule("backtest", "polygon_max_workers", 1)
    )
    polygon_min_interval_seconds: float = field(
        default_factory=lambda: _rule("backtest", "polygon_min_interval_seconds", 0.7)
    )
    backtest_source: str = field(
        default_factory=lambda: str(_rule("backtest", "source", "alpaca"))
    )
    polygon_use_quotes: bool = field(
        default_factory=lambda: bool(_rule("backtest", "polygon_use_quotes", False))
    )

    def validate(self) -> list[str]:
        """Return human-readable problems; empty list means the config is sane."""
        problems: list[str] = []
        if "paper" not in self.alpaca_base_url:
            problems.append(
                "SAFETY: alpaca_base_url does not contain 'paper' — refusing to "
                "treat this as a validated paper-trading config."
            )
        if not self.alpaca_api_key or not self.alpaca_secret_key:
            problems.append(
                "Missing Alpaca credentials. Set ALPACA_API_KEY/ALPACA_SECRET_KEY "
                "(or APCA_API_KEY_ID/APCA_API_SECRET_KEY) in .env."
            )
        if self.max_aggregate_put_risk_pct < self.max_loss_per_spread_pct:
            problems.append(
                "max_aggregate_put_risk_pct is smaller than max_loss_per_spread_pct "
                "— the aggregate cap must be >= a single spread's cap."
            )
        if self.max_crash_stress_pct < self.max_aggregate_put_risk_pct:
            problems.append(
                "max_crash_stress_pct is smaller than max_aggregate_put_risk_pct "
                "— check these are ordered sensibly."
            )
        if not (0 < self.put_spread_protective_delta_target < self.put_spread_short_delta_target):
            problems.append(
                "protective_delta_target must be > 0 and < short_delta_target, "
                "otherwise the 'protective' leg is not further OTM than the short leg."
            )
        if self.put_spread_dte_range[0] > self.put_spread_dte_range[1]:
            problems.append("put_engine.dte_range is inverted (min > max).")
        if self.call_dte_range[0] > self.call_dte_range[1]:
            problems.append("call_engine.dte_range is inverted (min > max).")
        return problems
