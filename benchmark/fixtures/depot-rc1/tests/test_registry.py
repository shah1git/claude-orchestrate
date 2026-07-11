"""Tests for depot.registry: serial validation and the instrument registry."""

import pytest

from depot.errors import DepotError
from depot.models import InstrumentClass, InstrumentState
from depot.registry import InstrumentRegistry


def test_register_and_get_instrument():
    """A well-formed serial with the correct class prefix registers cleanly
    and can be retrieved, starting out CLEARED.
    """
    registry = InstrumentRegistry()
    instrument = registry.register("TH-0001", InstrumentClass.THEODOLITE)
    assert instrument.serial == "TH-0001"
    assert instrument.state is InstrumentState.CLEARED
    assert registry.get("TH-0001") is instrument


def test_invalid_serial_format_raises():
    """A serial that doesn't match the XX-9999 shape is rejected."""
    registry = InstrumentRegistry()
    with pytest.raises(DepotError) as exc_info:
        registry.register("bad-serial", InstrumentClass.THEODOLITE)
    assert exc_info.value.code == "DEP-001"


def test_wrong_prefix_raises():
    """A well-formed serial with the wrong class prefix is rejected."""
    registry = InstrumentRegistry()
    with pytest.raises(DepotError) as exc_info:
        registry.register("GR-0001", InstrumentClass.THEODOLITE)
    assert exc_info.value.code == "DEP-002"


def test_duplicate_serial_raises():
    """Registering the same serial twice is rejected."""
    registry = InstrumentRegistry()
    registry.register("LV-0001", InstrumentClass.LEVEL)
    with pytest.raises(DepotError) as exc_info:
        registry.register("LV-0001", InstrumentClass.LEVEL)
    assert exc_info.value.code == "DEP-003"


def test_get_unknown_serial_raises():
    """Looking up an unregistered serial is rejected."""
    registry = InstrumentRegistry()
    with pytest.raises(DepotError) as exc_info:
        registry.get("TH-9999")
    assert exc_info.value.code == "DEP-004"
