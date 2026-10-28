"""
Finds every available adapter: the built-in ones plus any drop-in plugin
module placed in integrations/plugins/. A plugin is just a .py file that
defines one or more `Integration` subclasses with a unique `adapter` id.
"""
import importlib
import inspect
import pkgutil

from .base import Integration

_ADAPTERS = {}


def register(cls):
    """Class decorator (or plain call) that makes an adapter available."""
    if not cls.adapter:
        raise ValueError(f'{cls.__name__} must set an `adapter` id')
    _ADAPTERS[cls.adapter] = cls
    return cls


def _discover_plugins():
    from . import plugins
    for mod_info in pkgutil.iter_modules(plugins.__path__):
        if mod_info.name.startswith('_'):
            continue
        try:
            module = importlib.import_module(f'{plugins.__name__}.{mod_info.name}')
        except Exception as e:  # a broken plugin must never take the POS down
            print(f'⚠️  Could not load integration plugin {mod_info.name}: {e}')
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Integration) and obj is not Integration and obj.adapter:
                _ADAPTERS.setdefault(obj.adapter, obj)


_loaded = False


def adapters():
    """Return {adapter_id: class} for every known adapter."""
    global _loaded
    if not _loaded:
        from . import mpesa, etims, webhook  # noqa: F401  (registers built-ins)
        _discover_plugins()
        _loaded = True
    return dict(_ADAPTERS)


def get_adapter(adapter_id):
    return adapters().get(adapter_id)
