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
    for platform_name, adapter_class in _ADAPTER_REGISTRY.items():
        adapter = adapter_class()
        if adapter.detect(workbook_or_data, filename):
            return adapter
    return None

def list_platforms() -> List[Dict]:
    platforms = []
    for platform_name, adapter_class in _ADAPTER_REGISTRY.items():
        platforms.append({
            'name': platform_name,
            'supported_files': adapter_class.SUPPORTED_FILE_TYPES,
            'instructions': adapter_class.INSTRUCTIONS,
            'template_url': adapter_class.TEMPLATE_URL
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
                        register_adapter(item)
            except ImportError:
                pass

auto_register_adapters()
