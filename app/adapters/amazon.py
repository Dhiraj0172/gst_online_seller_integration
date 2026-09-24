"""Amazon Marketplace Tax Report (MTR) adapter.

Representative marketplace adapter implementing the PlatformAdapter contract:
- Platform identification and detection (filename tokens: 'amazon', 'mtr'; sheet: 'MTR')
- Header resolution against Amazon MTR schema
- Row parsing into ImportRow with raw source data and CanonicalTransaction
- Validation and error reporting
- Zero persistence / database coupling (handled by import_service)
"""
from .all_adapters import AmazonAdapter as BaseAmazonAdapter


class AmazonAdapter(BaseAmazonAdapter):
    """Representative adapter for Amazon MTR reports."""
    pass
