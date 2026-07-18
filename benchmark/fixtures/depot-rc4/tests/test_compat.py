"""Tests for depot.compat: the kit incompatibility matrix."""

import pytest

from depot.compat import validate_kit
from depot.errors import DepotError
from depot.models import InstrumentClass


def test_compatible_kit_passes():
    """A kit with no incompatible pair validates without error."""
    validate_kit([InstrumentClass.THEODOLITE, InstrumentClass.GNSS_ROVER])


def test_incompatible_pair_raises():
    """DRONE_PHOTO and GNSS_ROVER interfere over radio and cannot share a kit."""
    with pytest.raises(DepotError) as exc_info:
        validate_kit([InstrumentClass.DRONE_PHOTO, InstrumentClass.GNSS_ROVER])
    assert exc_info.value.code == "DEP-013"


def test_empty_kit_raises():
    """A kit with no instruments at all is rejected."""
    with pytest.raises(DepotError) as exc_info:
        validate_kit([])
    assert exc_info.value.code == "DEP-012"
