"""Operator configuration and executable discovery. Installation grants no authority."""
import json
import os
from pathlib import Path
import re
import shutil

DEFAULT_HOME = Path.home() / '.agents/ready-to-use-agent'


def controller():
    import workflow as w
    path = w.ROOT / 'controller-config.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict) or data.get('version', 1) != 1:
        raise ValueError('Unsupported controller configuration')
    if data.get('backend', 'pi') != 'pi':
        raise ValueError('The scheduled coding backend must remain Pi')
    for key in ('background_enabled',):
        if key in data and type(data[key]) is not bool:
            raise ValueError('Invalid controller setting: ' + key)
    delegation = data.get('delegation', {})
    if not isinstance(delegation, dict) or any(type(v) is not bool for v in delegation.values()):
        raise ValueError('Delegation must contain explicit Boolean permissions')
    return data


def require_background():
    if controller().get('background_enabled') is not True:
        raise ValueError('Background workers are disabled. Activate the controller explicitly first.')


def delegation(job=None):
    configured = controller().get('delegation', {})
    selected = (job or {}).get('delegation', configured)
    return {key: selected.get(key) is True for key in ('comments', 'approvals')}


def native(name):
    """Find native tools without calling a managed adapter recursively."""
    import workflow as w
    configured = controller().get('executables', {}).get(name)
    override = os.environ.get('AUTOWORKFLOW_NATIVE_' + name.upper())
    if override or configured:
        values = [override or configured]
    else:
        values = [str(Path(directory) / name) for directory in os.get_exec_path()]
    managed = (w.CODE / 'bin').resolve()
    for value in values:
        path = Path(value).expanduser()
        if path.is_file() and os.access(path, os.X_OK) and managed not in path.resolve().parents:
            return str(path.absolute())
    raise FileNotFoundError('Native ' + name + ' executable unavailable')


def intake(kind, data=None, require_enabled=False):
    import workflow as w
    path = w.ROOT / (kind + '-config.json')
    data = w.load(path) if data is None and path.exists() else (data or {})
    if not isinstance(data, dict):
        raise ValueError('Intake configuration must contain an object')
    if require_enabled and data.get('enabled') is not True:
        raise ValueError('Reviewer intake is disabled')
    if data.get('enabled') or require_enabled:
        if kind == 'github':
            logins = data.get('logins', [])
            if not logins or any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}', v) for v in logins):
                raise ValueError('Configure explicit GitHub operator logins before activation')
        else:
            if not isinstance(data.get('account'), str) or not data['account'].strip():
                raise ValueError('Configure the operator account before activation')
            if kind == 'ado' and not data.get('organizations'):
                raise ValueError('Configure ADO organizations before activation')
            if kind == 'teams':
                if not data.get('self_id') or not data.get('since'):
                    raise ValueError('Configure Teams identity and an explicit intake cutoff')
                reader = data.get('reader_command')
                if not isinstance(reader, list) or not reader or any(not isinstance(v, str) or not v or '\0' in v for v in reader):
                    raise ValueError('Configure a reviewed silent reader command before Teams activation')
                if data.get('reader_contract') != {'version': 1, 'read_only': True, 'silent_only': True}:
                    raise ValueError('Teams requires explicit read-only, silent-reader configuration')
                if Path(reader[0]).name.lower() in ('workiq', 'workiq.exe', 'scout', 'scout.exe'):
                    raise ValueError('The native interactive Teams CLI is not a silent reader')
    return data


def activate_intake(kind):
    """Activate collection only. Review writes require separate delegation."""
    import workflow as w
    path = w.ROOT / (kind + '-config.json')
    with w.lock(kind + '-config'):
        data = w.load(path) if path.exists() else {}
        intake(kind, {**data, 'enabled': True}, require_enabled=True)
        data.update(enabled=True, auto_start_reviews=True)
        w.save(path, data)
    return {kind + '_intake_enabled': True, 'existing_pr_watches': 'unchanged'}


def worker_extensions():
    """Load only installed native compaction. Do not discover foreground extensions."""
    path = os.environ.get('AUTOWORKFLOW_NATIVE_COMPACTION') or controller().get('native_compaction')
    return ['--extension', str(Path(path).expanduser())] if path and Path(path).expanduser().is_file() else []


def clean_worker_env(env):
    """Keep PR credentials out of the model-provider environment."""
    result = dict(env)
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'AZURE_DEVOPS_EXT_PAT', 'SYSTEM_ACCESSTOKEN',
                'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS', 'GIT_TRACE', 'GIT_TRACE_PACKET',
                'GIT_TRACE_CURL', 'GIT_CURL_VERBOSE', 'PYTHONPATH', 'PYTHONHOME'):
        result.pop(key, None)
    for key in list(result):
        if key.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_', 'GIT_TRACE')) or key in ('GH_DEBUG', 'AZURE_LOG_LEVEL'):
            result.pop(key)
    return result
