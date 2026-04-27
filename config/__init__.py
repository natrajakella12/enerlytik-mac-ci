# Re-export from config.py at project root so that `import config; config.get_connection()` works
# when config/ package shadows config.py module.
import importlib.util as _ilu
import os as _os

_config_py = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "config.py")
if _os.path.exists(_config_py):
    _spec = _ilu.spec_from_file_location("_config_root", _config_py)
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    # Re-export all public attributes
    for _attr in dir(_mod):
        if not _attr.startswith("_"):
            globals()[_attr] = getattr(_mod, _attr)
