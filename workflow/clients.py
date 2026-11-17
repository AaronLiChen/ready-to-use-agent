"""Installed frontend identity. The persistent worker backend remains Pi."""
import json
import os
from pathlib import Path
import shutil

REGISTRY = Path(os.environ.get('AUTOWORKFLOW_CLIENTS', str(Path.home() / '.agents/ready-to-use-agent/clients.json')))
INTERPRETERS = {"node", "bun", "python", "python3", "python3.9", "python3.14"}


def installed_clients():
    try:
        data = json.loads(REGISTRY.read_text())
        if data.get("version") == 1 and isinstance(data.get("clients"), list):
            return data["clients"]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return []


def resolved(value, executable=False):
    if not isinstance(value, str) or not value or value.startswith("-"):
        return None
    if executable and "/" not in value:
        value = shutil.which(value)
        if not value:
            return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def harness_matches(argv, clients=None):
    """Match an executable or interpreter entrypoint, never arbitrary goal text."""
    if not argv:
        return False
    executable = resolved(argv[0], executable=True)
    candidate = executable
    if Path(argv[0]).name in INTERPRETERS:
        # Do not mistake code or positional data passed to an evaluator for a CLI.
        script = None
        for arg in argv[1:]:
            if arg in ("-e", "--eval", "-p", "--print", "-c", "-m"):
                return False
            if not arg.startswith("-"):
                script = arg
                break
        candidate = resolved(script)
    if candidate is None:
        return False
    for client in installed_clients() if clients is None else clients:
        if not isinstance(client, dict):
            continue
        for path in client.get("executables", []):
            if candidate == resolved(path):
                return True
        if candidate.name in client.get("native_names", []):
            for path in client.get("install_roots", []):
                root = resolved(path)
                if root is not None and (root == candidate or root in candidate.parents):
                    return True
    return False
