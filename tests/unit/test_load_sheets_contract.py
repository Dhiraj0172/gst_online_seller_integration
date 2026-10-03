"""Regression test for _load_sheets() 5-tuple contract.

Phase 5E-9R.1: Verifies every _load_sheets() return path yields exactly
a 5-tuple (sheets, file_name, errors, diagnostics, wb_ref), including
the unsupported-data-type fallback that previously returned only 4 elements.
"""
import pytest
from app.adapters.all_adapters import BaseGenericAdapter


class TestLoadSheetsTupleArity:
    """Prove _load_sheets always returns a 5-tuple for all input categories."""

    def _assert_5_tuple(self, result, label):
        assert isinstance(result, tuple), f"{label}: expected tuple, got {type(result)}"
        assert len(result) == 5, (
            f"{label}: expected 5-tuple, got {len(result)}-tuple: {result!r}"
        )

    def test_unsupported_type_integer(self):
        """Integer input must return a 5-tuple with an error, not crash."""
        adapter = BaseGenericAdapter()
        result = adapter._load_sheets(12345)
        self._assert_5_tuple(result, "integer input")
        sheets, _name, errors, _diag, wb_ref = result
        assert sheets == []
        assert any("Unsupported data type" in e for e in errors)
        assert wb_ref is None

    def test_unsupported_type_object(self):
        """Arbitrary object input must return a 5-tuple with an error."""
        adapter = BaseGenericAdapter()
        result = adapter._load_sheets(object())
        self._assert_5_tuple(result, "object input")
        sheets, _name, errors, _diag, wb_ref = result
        assert sheets == []
        assert any("Unsupported data type" in e for e in errors)
        assert wb_ref is None

    def test_none_input(self):
        """None input must return a 5-tuple."""
        adapter = BaseGenericAdapter()
        result = adapter._load_sheets(None)
        self._assert_5_tuple(result, "None input")

    def test_empty_list_input(self):
        """Empty list input must return a 5-tuple."""
        adapter = BaseGenericAdapter()
        result = adapter._load_sheets([])
        self._assert_5_tuple(result, "empty list input")

    def test_missing_file_path(self):
        """Non-existent file path must return a 5-tuple."""
        adapter = BaseGenericAdapter()
        result = adapter._load_sheets("/nonexistent/path/file.xlsx")
        self._assert_5_tuple(result, "missing file path")
        sheets, _name, errors, _diag, wb_ref = result
        assert sheets == []
        assert len(errors) > 0
        assert wb_ref is None
