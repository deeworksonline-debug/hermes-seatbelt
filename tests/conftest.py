"""Load the repo root as the package 'seatbelt_plugin' (it has relative imports) without installing it."""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent / "seatbelt"
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load():
    if "seatbelt_plugin" in sys.modules:
        return sys.modules["seatbelt_plugin"]
    spec = importlib.util.spec_from_file_location(
        "seatbelt_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["seatbelt_plugin"] = module
    spec.loader.exec_module(module)
    return module


_load()


@pytest.fixture
def sb():
    """Namespace of the plugin's modules."""
    import seatbelt_plugin.adapters as adapters
    import seatbelt_plugin.commands as commands
    import seatbelt_plugin.engine as engine
    import seatbelt_plugin.guard_self as guard_self
    import seatbelt_plugin.intent as intent
    import seatbelt_plugin.policy as policy
    import seatbelt_plugin.runtime as runtime
    import seatbelt_plugin.state as state

    class NS:
        pass

    ns = NS()
    for name, mod in dict(adapters=adapters, commands=commands, engine=engine, guard_self=guard_self,
                          intent=intent, policy=policy, runtime=runtime, state=state).items():
        setattr(ns, name, mod)
    return ns


@pytest.fixture
def rt(sb, tmp_path):
    """A Runtime on an in-memory DB with the shipped adapters."""
    state = sb.state.State(sqlite3.connect(":memory:", check_same_thread=False))
    adapters = sb.adapters.load_adapters(ROOT / "venues")
    protected = [tmp_path / "plugin-data" / "seatbelt"]
    return sb.runtime.Runtime(state, adapters, sb.guard_self.SelfGuard(protected))
