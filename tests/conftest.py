"""测试公共设施：以包的形式导入插件目录（目录名可能含连字符）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import importlib
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))
PACKAGE = ROOT.name


def load(module: str) -> Any:
    return importlib.import_module(f"{PACKAGE}.{module}")
