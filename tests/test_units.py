"""Unit conversion tests.

Small module, high consequence. A wrong factor here is a thousandfold error that
looks entirely plausible on a chart, which is the failure this exists to prevent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from atmos.connectors import fhmz, rhmzrs, sensorcommunity, tuzla
from atmos.core.units import CANONICAL, UnknownConversion, canonical_unit, convert


def test_every_parameter_has_a_canonical_unit() -> None:
    for code in ("pm1", "pm10", "pm25", "so2", "no", "no2", "nox", "o3", "co",
                 "h2s", "c6h6", "temp", "rh", "press", "wspd", "wdir"):
        assert canonical_unit(code), f"{code} has no canonical unit"


def test_matching_unit_passes_through_untouched() -> None:
    value, unit, factor = convert(Decimal("37.5"), "ug/m3", "pm25")
    assert value == Decimal("37.5")
    assert unit == "ug/m3"
    assert factor == 1


def test_co_micrograms_to_milligrams() -> None:
    """FHMZ publishes ug/m3, canonical is mg/m3."""
    value, unit, factor = convert(Decimal("1051"), "ug/m3", "co")
    assert value == Decimal("1.051")
    assert unit == "mg/m3"
    assert factor == Decimal("0.001")


def test_co_from_the_other_two_sources_needs_no_change() -> None:
    """Tuzla and RHMZ already publish mg/m3."""
    value, unit, factor = convert(Decimal("1.051"), "mg/m3", "co")
    assert value == Decimal("1.051")
    assert factor == 1


def test_the_two_co_conventions_meet_at_the_same_number() -> None:
    """The exact failure this module exists to prevent."""
    from_fhmz, _, _ = convert(Decimal("1051"), "ug/m3", "co")
    from_tuzla, _, _ = convert(Decimal("1.051"), "mg/m3", "co")
    assert from_fhmz == from_tuzla


def test_pressure_pascals_to_hectopascals() -> None:
    """Sensor.Community sends Pa."""
    value, unit, factor = convert(Decimal("98500"), "Pa", "press")
    assert value == Decimal("985.00")
    assert unit == "hPa"
    assert factor == Decimal("0.01")


def test_millibar_is_hectopascal() -> None:
    """Tuzla labels its pressure column mBar."""
    value, unit, _ = convert(Decimal("1013.2"), "mBar", "press")
    assert value == Decimal("1013.2")
    assert unit == "hPa"


def test_conversion_is_exact_not_floating_point() -> None:
    value, _, _ = convert(Decimal("0.646"), "mg/m3", "co")
    assert value == Decimal("0.646")
    assert str(value) == "0.646"


def test_round_trip_returns_the_original() -> None:
    there, _, _ = convert(Decimal("1042"), "ug/m3", "co")
    back = there * Decimal("1000")
    assert back == Decimal("1042.000")


def test_unknown_unit_is_refused_not_passed_through() -> None:
    """A wrong unit is worse than a missing reading, because it looks like data."""
    with pytest.raises(UnknownConversion):
        convert(Decimal("1"), "furlongs", "pm10")


def test_unknown_parameter_is_refused() -> None:
    with pytest.raises(UnknownConversion):
        convert(Decimal("1"), "ug/m3", "not_a_parameter")


# Tuzla is absent on purpose: it reads units from each page header rather than a
# static table, so it is covered by test_tuzla_header_units_are_all_convertible.
@pytest.mark.parametrize(
    "module",
    [fhmz, rhmzrs, sensorcommunity],
    ids=["fhmz", "rhmzrs", "sensorcommunity"],
)
def test_every_unit_a_connector_emits_can_be_converted(module) -> None:
    """Guards against a new connector inventing a unit nothing can handle."""
    units: set[tuple[str, str]] = set()

    params = getattr(module, "PARAMETERS", {})
    for value in params.values():
        if isinstance(value, tuple) and len(value) == 2:
            units.add((value[0], value[1]))
        elif isinstance(value, str):
            unit = getattr(module, "PUBLISHED_UNIT", None)
            if unit:
                units.add((value, unit))

    assert units, f"no units discovered for {module.__name__}"
    for code, unit in units:
        if code not in CANONICAL:
            pytest.fail(f"{module.__name__} emits unknown parameter {code!r}")
        convert(Decimal("1"), unit, code)  # must not raise


def test_tuzla_header_units_are_all_convertible() -> None:
    """Tuzla reads units from the page header, so they are not a fixed list."""
    from pathlib import Path

    from atmos.connectors.base import FetchTarget

    fixture = Path(__file__).parent / "fixtures" / "tuzla" / "skver-yesterday.html"
    conn = tuzla.TuzlaConnector()
    for o in conn.parse(fixture.read_bytes(),
                        FetchTarget(id="skver-yesterday", url="x", station_hint="skver")):
        convert(o.value, o.unit, o.parameter_code)  # must not raise
