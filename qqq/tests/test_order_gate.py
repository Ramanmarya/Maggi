"""
Tests for the order gate — the layer that decides whether anything reaches
the broker at all. These are the tests that matter most: a bug in the
engines costs money slowly, a bug here places orders that should never exist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import kill_switch
from core.gated_broker import GatedBroker
from qqq.broker_adapter import SingleLegOrder
from qqq.preflight import StubBroker


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the gate at a throwaway TRADING_ENABLED + allocator.json."""
    enabled = tmp_path / "TRADING_ENABLED"
    alloc = tmp_path / "allocator.json"
    monkeypatch.setattr(kill_switch, "TRADING_ENABLED_PATH", enabled)
    monkeypatch.setattr(kill_switch, "ALLOCATOR_PATH", alloc)

    def configure(switch: str | None, phase: str | None, active: bool = True):
        if switch is not None:
            enabled.write_text(switch)
        if phase is not None:
            alloc.write_text(json.dumps({"allocations": {"qqq": {"phase": phase, "active": active}}}))
        return enabled, alloc

    return configure


def _probe():
    return SingleLegOrder(
        contract=None, symbol="QQQ", side="buy", qty=1,
        order_type="market", limit_price=None, client_order_id="test-probe",
    )


@pytest.mark.parametrize("phase", ["design", "scanner_only", "killed"])
def test_non_trading_phases_block_orders(sandbox, phase):
    sandbox("true", phase)
    assert kill_switch.check_order_gate("qqq").allowed is False


@pytest.mark.parametrize("phase", ["paper", "tiny_live", "live"])
def test_trading_phases_allow_orders(sandbox, phase):
    sandbox("true", phase)
    assert kill_switch.check_order_gate("qqq").allowed is True


def test_kill_switch_off_blocks_even_in_live(sandbox):
    sandbox("false", "live")
    gate = kill_switch.check_order_gate("qqq")
    assert gate.allowed is False
    assert "kill switch" in gate.reason


def test_inactive_arm_blocks_orders(sandbox):
    sandbox("true", "live", active=False)
    assert kill_switch.check_order_gate("qqq").allowed is False


def test_missing_files_fail_closed(sandbox, tmp_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "TRADING_ENABLED_PATH", tmp_path / "nope")
    monkeypatch.setattr(kill_switch, "ALLOCATOR_PATH", tmp_path / "also_nope")
    assert kill_switch.check_order_gate("qqq").allowed is False


def test_unknown_phase_fails_closed(sandbox):
    sandbox("true", "yolo")
    gate = kill_switch.check_order_gate("qqq")
    assert gate.allowed is False
    assert "unrecognized" in gate.reason


def test_garbage_kill_switch_value_fails_closed(sandbox):
    sandbox("maybe", "live")
    assert kill_switch.check_order_gate("qqq").allowed is False


def test_gated_broker_refuses_submission_when_blocked(sandbox):
    sandbox("true", "design")
    stub = StubBroker()
    result = GatedBroker(stub, arm="qqq").submit_single_leg(_probe())
    assert result.success is False
    assert result.status == "blocked"
    assert stub.submitted == []  # never reached the inner adapter


def test_gated_broker_passes_submission_when_open(sandbox):
    sandbox("true", "paper")
    stub = StubBroker()
    result = GatedBroker(stub, arm="qqq").submit_single_leg(_probe())
    assert result.success is True
    assert stub.submitted == ["single_leg"]


def test_closing_is_never_gated(sandbox):
    """A kill switch must not trap the book in positions it cannot exit."""
    sandbox("false", "killed")
    stub = StubBroker()
    result = GatedBroker(stub, arm="qqq").close_position("pos-1", None)
    assert result.success is True
    assert stub.submitted == ["close"]


def test_read_only_calls_pass_through_when_blocked(sandbox):
    sandbox("false", "design")
    broker = GatedBroker(StubBroker(price=418.0), arm="qqq")
    assert broker.get_underlying_price() == 418.0
    assert broker.get_current_positions().equity > 0
