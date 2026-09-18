"""Make the test suite dependency-free: no pytest-asyncio, no plugin to install."""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def pytest_pyfunc_call(pyfuncitem):  # pragma: no cover - test plumbing
    func = pyfuncitem.obj
    if inspect.iscoroutinefunction(func):
        kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(func(**kwargs))
        return True
    return None
