from .registry import register_adapter, get_adapter, detect_platform, list_platforms, auto_register_adapters
from .all_adapters import get_adapter_readiness_matrix
from .base import PlatformAdapter, ImportResult, ImportRow, ImportRowStatus
from .canonical import CanonicalTransaction
from .amazon import AmazonAdapter

__all__ = [
    'register_adapter',
    'get_adapter',
    'detect_platform',
    'list_platforms',
    'auto_register_adapters',
    'get_adapter_readiness_matrix',
    'PlatformAdapter',
    'AmazonAdapter',
    'ImportResult',
    'ImportRow',
    'ImportRowStatus',
    'CanonicalTransaction',
]
