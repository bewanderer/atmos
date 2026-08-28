"""Unit harmonisation.

Sources disagree. FHMZ publishes CO in ug/m3, Tuzla Canton and RHMZ RS in mg/m3,
and Sensor.Community sends pressure in Pascals. Combining those without
converting gives errors of a factor of a thousand that look entirely plausible
on a chart.

Parsers record what the source published, exactly as published. Conversion
happens here, on the way into the database, and both the original value and the
factor applied are kept so any figure can be traced back.
"""

from __future__ import annotations

from decimal import Decimal

# Our canonical unit per parameter. Matches the parameters table.
CANONICAL: dict[str, str] = {
    "pm1": "ug/m3",
    "pm10": "ug/m3",
    "pm25": "ug/m3",
    "so2": "ug/m3",
    "no": "ug/m3",
    "no2": "ug/m3",
    "nox": "ug/m3",
    "o3": "ug/m3",
    "co": "mg/m3",
    "h2s": "ug/m3",
    "c6h6": "ug/m3",
    "temp": "degC",
    "rh": "pct",
    "press": "hPa",
    "wspd": "m/s",
    "wdir": "deg",
}

# from -> to -> multiplier. Exact decimals, no floats.
FACTORS: dict[tuple[str, str], Decimal] = {
    ("ug/m3", "mg/m3"): Decimal("0.001"),
    ("mg/m3", "ug/m3"): Decimal("1000"),
    ("Pa", "hPa"): Decimal("0.01"),
    ("hPa", "Pa"): Decimal("100"),
    ("kPa", "hPa"): Decimal("10"),
    ("mbar", "hPa"): Decimal("1"),
    ("mBar", "hPa"): Decimal("1"),
    ("%", "pct"): Decimal("1"),
    ("C", "degC"): Decimal("1"),
    ("°C", "degC"): Decimal("1"),
}


class UnknownConversion(Exception):
    """Raised when a unit pair has no defined factor.

    Deliberately fatal rather than silently passing the value through. A wrong
    unit is worse than a missing reading, because it looks like data.
    """


def canonical_unit(parameter_code: str) -> str | None:
    return CANONICAL.get(parameter_code)


def convert(value: Decimal, from_unit: str, parameter_code: str) -> tuple[Decimal, str, Decimal]:
    """Return (converted value, canonical unit, factor applied).

    A factor of 1 means the source already used our unit.
    """
    target = CANONICAL.get(parameter_code)
    if target is None:
        raise UnknownConversion(f"no canonical unit for parameter {parameter_code!r}")

    if from_unit == target:
        return value, target, Decimal(1)

    factor = FACTORS.get((from_unit, target))
    if factor is None:
        raise UnknownConversion(
            f"no factor from {from_unit!r} to {target!r} for {parameter_code!r}"
        )
    return value * factor, target, factor
