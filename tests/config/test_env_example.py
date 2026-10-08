""".env.example lists every environment variable the service reads, with placeholders only.

The standard: "Any new setting is in .env.example and covered by the startup config check."
This reads the code instead of trusting a list: every Settings field, plus every
os.environ / os.getenv read under app/, must have a line in .env.example.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"

# Set by the platform, never by us; documented in .env.example as comments only.
PLATFORM_SET = {"WEBSITE_INSTANCE_ID", "HOSTNAME"}


def _example_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            keys[match.group(1)] = match.group(2).strip()
    return keys


def _environ_reads() -> set[str]:
    """Names read via os.environ.get / os.environ[...] / os.getenv anywhere in app/."""
    names: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            t.id: node.value.value
            for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for t in node.targets if isinstance(t, ast.Name)
        }

        def resolve(arg: ast.AST, constants: dict[str, str] = constants) -> str | None:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
            if isinstance(arg, ast.Name):
                return constants.get(arg.id)
            return None

        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Call) and node.args:
                func = ast.unparse(node.func)
                if func in ("os.environ.get", "os.getenv", "environ.get"):
                    target = node.args[0]
            elif isinstance(node, ast.Subscript) and ast.unparse(node.value) == "os.environ":
                target = node.slice
            if target is not None:
                name = resolve(target)
                assert name, f"{path.name}: an environment read whose name is not a constant"
                names.add(name)
    return names


def test_every_setting_is_in_env_example():
    keys = _example_keys()
    missing = sorted(name.upper() for name in Settings.model_fields if name.upper() not in keys)
    assert not missing, f".env.example is missing: {missing}"


def test_every_direct_environment_read_is_in_env_example():
    keys = _example_keys()
    reads = _environ_reads()
    assert "APPLICATIONINSIGHTS_CONNECTION_STRING" in reads  # the scan works
    missing = sorted(reads - set(keys) - PLATFORM_SET)
    assert not missing, f".env.example is missing: {missing}"


def test_env_example_holds_placeholders_not_secrets():
    for key, value in _example_keys().items():
        assert "InstrumentationKey=" not in value or "00000000-0000-0000-0000-000000000000" in value, key
        assert not re.search(r"://[^/\s]*:[^@/\s<]+@", value), f"{key} carries a password"
        assert "AccountKey=" not in value, key
