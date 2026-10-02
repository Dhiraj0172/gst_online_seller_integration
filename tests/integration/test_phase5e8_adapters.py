import pytest
import os
from app.adapters.registry import detect_platform, get_adapter

ADAPTERS_TO_TEST = [
    ("custom_excel", "Custom_Excel", True),
    ("jiomart", "JioMart", False),
    ("snapdeal", "Snapdeal", False),
    ("tatacliq", "TataCLiQ", False),
    ("limeroad", "Limeroad", False),
    ("shopdeck", "Shopdeck", False),
    ("glowroad", "GlowRoad", False),
    ("snapmint", "Snapmint", False),
    ("citymall", "Citymall", False),
    ("roposo", "Roposo", False)
]

@pytest.mark.parametrize("filename_prefix, expected_platform, is_doc", ADAPTERS_TO_TEST)
def test_minor_adapter_detection_and_parsing(app, filename_prefix, expected_platform, is_doc):
    with app.app_context():
        filename = f"{filename_prefix}_sample.xlsx"
        filepath = os.path.join(app.root_path, '..', 'tests', 'fixtures', filename)

        adapter = detect_platform(filepath, filename)
        assert adapter is not None
        assert adapter.PLATFORM_NAME == expected_platform
        assert getattr(adapter, 'FORMAT_DOCUMENTED', False) == is_doc

        result = adapter.parse(filepath, filename)
        assert len(result.errors) == 0
        assert len(result.rows) == 1

        row = result.rows[0]
        assert row.status.value == 'SUCCESS'
