"""Tests for A5 SHACL datatype casting extension.

Verifies 9 new XSD converters added to `_XSD_CONVERTERS`:
- nonNegativeInteger, positiveInteger
- unsignedInt / unsignedLong / unsignedShort (all ≥ 0)
- byte (-128..127), unsignedByte (0..255)
- gYear / gMonth / gDay (XSD gregorian part formats)
"""

from rdflib import XSD

from tools.abox_generation import _XSD_CONVERTERS

# ---------------------------------------------------------------------------
# xsd:nonNegativeInteger
# ---------------------------------------------------------------------------

def test_non_negative_int_converts_positive():
    converter = _XSD_CONVERTERS[str(XSD.nonNegativeInteger)]
    assert converter("5") == "5"


def test_non_negative_int_rejects_negative():
    converter = _XSD_CONVERTERS[str(XSD.nonNegativeInteger)]
    assert converter("-3") is None


def test_non_negative_int_accepts_float_string():
    """Numeric strings like '7.0' round-trip to '7'."""
    converter = _XSD_CONVERTERS[str(XSD.nonNegativeInteger)]
    assert converter("7.0") == "7"


def test_non_negative_int_rejects_non_numeric():
    converter = _XSD_CONVERTERS[str(XSD.nonNegativeInteger)]
    assert converter("abc") is None


# ---------------------------------------------------------------------------
# xsd:positiveInteger
# ---------------------------------------------------------------------------

def test_positive_int_rejects_zero():
    converter = _XSD_CONVERTERS[str(XSD.positiveInteger)]
    assert converter("0") is None


def test_positive_int_accepts_one():
    converter = _XSD_CONVERTERS[str(XSD.positiveInteger)]
    assert converter("1") == "1"


# ---------------------------------------------------------------------------
# xsd:unsignedInt / unsignedLong / unsignedShort
# ---------------------------------------------------------------------------

def test_unsigned_int_same_as_non_negative():
    for xsd_type in (XSD.unsignedInt, XSD.unsignedLong, XSD.unsignedShort):
        converter = _XSD_CONVERTERS[str(xsd_type)]
        assert converter("-1") is None, f"{xsd_type} should reject -1"
        assert converter("100") == "100", f"{xsd_type} should accept 100"


# ---------------------------------------------------------------------------
# xsd:byte (-128..127)
# ---------------------------------------------------------------------------

def test_byte_range_boundary():
    converter = _XSD_CONVERTERS[str(XSD.byte)]
    assert converter("127") == "127"
    assert converter("128") is None
    assert converter("-128") == "-128"
    assert converter("-129") is None


# ---------------------------------------------------------------------------
# xsd:unsignedByte (0..255)
# ---------------------------------------------------------------------------

def test_unsigned_byte_range():
    converter = _XSD_CONVERTERS[str(XSD.unsignedByte)]
    assert converter("0") == "0"
    assert converter("255") == "255"
    assert converter("256") is None
    assert converter("-1") is None


# ---------------------------------------------------------------------------
# xsd:gYear / gMonth / gDay — Gregorian part formats
# ---------------------------------------------------------------------------

def test_gyear_valid_and_invalid():
    converter = _XSD_CONVERTERS[str(XSD.gYear)]
    assert converter("2024") == "2024"
    assert converter("abc") is None
    # XSD gYear requires 4 digits.
    assert converter("24") is None


def test_gmonth_format():
    converter = _XSD_CONVERTERS[str(XSD.gMonth)]
    assert converter("--05") == "--05"
    # XSD gMonth requires "--MM" format.
    assert converter("5") is None


def test_gday_format():
    converter = _XSD_CONVERTERS[str(XSD.gDay)]
    assert converter("---15") == "---15"
    # XSD gDay requires "---DD" format.
    assert converter("15") is None


# ---------------------------------------------------------------------------
# Regression: existing converters still registered + working
# ---------------------------------------------------------------------------

def test_existing_converters_unchanged():
    decimal_converter = _XSD_CONVERTERS[str(XSD.decimal)]
    # `_convert_decimal` routes through float(v) -> str, so "3.14" stays "3.14".
    assert decimal_converter("3.14") == "3.14"


# ---------------------------------------------------------------------------
# Integration: converter is registered + callable for all 10 new types
# ---------------------------------------------------------------------------

def test_all_new_converters_registered():
    """Smoke test: every new XSD type must be present and callable."""
    new_types = [
        XSD.nonNegativeInteger,
        XSD.positiveInteger,
        XSD.unsignedInt,
        XSD.unsignedLong,
        XSD.unsignedShort,
        XSD.byte,
        XSD.unsignedByte,
        XSD.gYear,
        XSD.gMonth,
        XSD.gDay,
    ]
    for xsd_type in new_types:
        key = str(xsd_type)
        assert key in _XSD_CONVERTERS, f"{xsd_type} missing from _XSD_CONVERTERS"
        converter = _XSD_CONVERTERS[key]
        assert callable(converter), f"{xsd_type} converter not callable"
