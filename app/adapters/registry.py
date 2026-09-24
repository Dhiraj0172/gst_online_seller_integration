import os
import importlib
from typing import Dict, List, Optional
from .base import PlatformAdapter

_ADAPTER_REGISTRY: Dict[str, type] = {}

def register_adapter(adapter_class: type) -> None:
    if issubclass(adapter_class, PlatformAdapter) and adapter_class.PLATFORM_NAME:
        _ADAPTER_REGISTRY[adapter_class.PLATFORM_NAME] = adapter_class

def get_adapter(platform_name: str) -> Optional[PlatformAdapter]:
    adapter_class = _ADAPTER_REGISTRY.get(platform_name)
    if adapter_class:
        return adapter_class()
    return None

def detect_platform(workbook_or_data, filename: str) -> Optional[PlatformAdapter]:
    """Return the most specific adapter that can handle this file.

    Ranking: an explicit filename match wins over a sheet/header-format match,
    and catch-all adapters (e.g. Custom Excel) are only used when nothing more
    specific claimed the file. Without this ranking the catch-all could shadow a
    real platform adapter purely because of registration order.
    """
    matches = []
    for order, (platform_name, adapter_class) in enumerate(_ADAPTER_REGISTRY.items()):
        adapter = adapter_class()
        if not adapter.detect(workbook_or_data, filename):
            continue
        matches.append((_detection_rank(adapter, filename), order, adapter))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1]))
    return matches[0][2]


def _detection_rank(adapter: PlatformAdapter, filename: str) -> int:
    name = (filename or '').lower()
    if any(token in name for token in getattr(adapter, 'FILENAME_TOKENS', ())):
        return 0
    if getattr(adapter, 'CATCH_ALL_DETECT', False):
        return 2
    return 1

def list_platforms() -> List[Dict]:
    platforms = []
    for platform_name, adapter_class in _ADAPTER_REGISTRY.items():
        platforms.append({
            'name': platform_name,
            'supported_files': adapter_class.SUPPORTED_FILE_TYPES,
            'instructions': adapter_class.INSTRUCTIONS,
            'template_url': adapter_class.TEMPLATE_URL,
            'format_documented': getattr(adapter_class, 'FORMAT_DOCUMENTED', False),
        })
    return platforms

def auto_register_adapters() -> None:
    current_dir = os.path.dirname(__file__)
    for filename in os.listdir(current_dir):
        if filename.endswith(".py") and filename not in ("__init__.py", "base.py", "registry.py"):
            module_name = f"app.adapters.{filename[:-3]}"
            try:
                module = importlib.import_module(module_name)
                for item_name in dir(module):
                    item = getattr(module, item_name)
                    if isinstance(item, type) and issubclass(item, PlatformAdapter) and item is not PlatformAdapter:
                        if getattr(item, '__module__', None) == module.__name__:
                            register_adapter(item)
            except ImportError:
                pass

auto_register_adapters()
