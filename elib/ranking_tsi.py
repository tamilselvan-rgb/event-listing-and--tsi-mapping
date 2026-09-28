"""Load TradeShowIntel mapper from combined-ranking-tsi-pipeline without ``src`` name clash."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

from elib.paths import PIPELINE_ROOT

_CACHE: ModuleType | None = None


def ranking_tsi_mapper() -> ModuleType:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    path = PIPELINE_ROOT / "src" / "tsi_mapper.py"
    if not path.is_file():
        raise ModuleNotFoundError(f"TSI mapper not found: {path}")
    pipeline_root = str(PIPELINE_ROOT)
    if pipeline_root not in sys.path:
        sys.path.insert(0, pipeline_root)
    spec = importlib.util.spec_from_file_location("ranking_tsi_mapper", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load TSI mapper from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    _CACHE = mod
    return mod
