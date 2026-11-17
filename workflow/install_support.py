"""Non-destructive workflow installation. Service activation is a separate operator action."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import sys

FILES = ('workflow.py', 'configuration.py', 'prompts.py', 'probes.py', 'ado.py', 'ado_write.py', 'clients.py',
         'action_notices.py', 'auth_recovery.py', 'github_auth.py', 'ado_intake.py', 'github_intake.py',
         'parallel_dispatch.py', 'teams_intake.py', 'teams_coverage.py', 'teams_reader.py',
         'worker_recovery.py', 'claude_hook.py', 'validation.py', 'write_guard.py', 'service.py', 'SKILL.md')
ADAPTERS = ('az', 'gh', 'git', 'teams-reader')
LABEL = 'org.ready-to-use-agent.workflow'
UNIT = 'ready-agent-workflow'
PREFIX = '.agents/ready-to-use-agent'
STATE = PREFIX + '/state/workflow'


def discover(name, path=None):
    candidates = [Path(path)] if path else [Path(folder) / name for folder in os.get_exec_path()]
    for value in candidates:
        if value.is_file() and os.access(value, os.X_OK) and not value.parent.name == 'bin' and value.parent.parent.name == 'workflow':
            return str(value.absolute())
        if value.is_file() and os.access(value, os.X_OK) and value.parent.parent.name not in ('autoworkflow', 'workflow'):
            return str(value.absolute())
    return None


def registry(pi=None):
    rows = []
    for name in ('pi', 'claude', 'codex', 'copilot', 'opencode', 'aider', 'gemini', 'cursor-agent'):
        path = pi if name == 'pi' and pi else discover(name)
        if path:
            rows.append({'kind': name, 'executables': [str(Path(path).resolve())],
                         'install_roots': [], 'native_names': []})
    return {'version': 1, 'backend': 'pi', 'native_stop_protection': ['pi', 'claude'], 'clients': rows}


def scheduler_files(home, code, python, platform, executable_path):
    root = home / STATE
    env = {'AUTOWORKFLOW_ROOT': str(root), 'PI_CODING_AGENT_DIR': str(home / '.pi/agent'),
           'PATH': executable_path}
    if platform == 'darwin':
        relative = 'Library/LaunchAgents/' + LABEL + '.plist'
        data = {'Label': LABEL, 'ProgramArguments': [python, '-I', str(code / 'service.py'), 'run'],
                'StartInterval': 60, 'RunAtLoad': False, 'Disabled': True, 'WorkingDirectory': str(code),
                'EnvironmentVariables': env, 'StandardOutPath': str(root / 'logs/service.log'),
                'StandardErrorPath': str(root / 'logs/service-error.log')}
        return {relative: plistlib.dumps(data)}
    if platform.startswith('linux'):
        quote = lambda value: '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('\n', '') + '"'
        service = ('[Unit]\nDescription=Portable agent workflow\n\n[Service]\nType=oneshot\n'
                   'WorkingDirectory=' + quote(str(code)) + '\n'
                   'ExecStart=' + ' '.join(quote(value) for value in (python, '-I', str(code / 'service.py'), 'run')) + '\n'
                   + ''.join('Environment=' + quote(key + '=' + value) + '\n' for key, value in env.items())
                   + 'UMask=0077\nTimeoutStartSec=infinity\n')
        timer = ('[Unit]\nDescription=Portable agent workflow timer\n\n[Timer]\n'
                 'OnActiveSec=60\nOnUnitActiveSec=60\nAccuracySec=10\nPersistent=true\n'
                 'Unit=' + UNIT + '.service\n\n[Install]\nWantedBy=timers.target\n')
        return {'.config/systemd/user/' + UNIT + '.service': service.encode(),
                '.config/systemd/user/' + UNIT + '.timer': timer.encode()}
    raise ValueError('The workflow controller requires macOS, Linux, or WSL2. Native Windows supports core profiles only.')


def initial(installation, relative, value):
    path = installation.target(relative)
    if path.exists():
        current = json.loads(path.read_text())
        if not isinstance(current, dict):
            raise ValueError('Existing workflow configuration must contain an object')
        value = current
    installation.add(relative, installation_json(value), merge=True)


def installation_json(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def hook_plan(installation, code, python):
    relative = '.claude/settings.json'
    path = installation.target(relative)
    settings = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError('Claude settings must contain an object')
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Claude hooks must contain an object')
    command = shlex.quote(python) + ' -I ' + shlex.quote(str(code / 'claude_hook.py'))
    managed = list(installation.previous.get('files', {}).get(relative, {}).get('managed_hooks', []))
    for event in ('Stop', 'SessionEnd'):
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError('Claude hook event settings must contain an array')
        present = any(any(hook.get('command') == command for hook in entry.get('hooks', []) if isinstance(hook, dict))
                      for entry in entries if isinstance(entry, dict))
        if not present:
            entries.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]})
            managed.append({'event': event, 'command': command, 'timeout': 15})
    installation.add(relative, installation_json(settings), merge=True)
    installation.actions[-1]['managed_hooks'] = managed


def remove_hooks(data, records):
    """Remove only unchanged managed hooks. Preserve all other settings and hook edits."""
    settings = json.loads(data)
    changed = False
    for record in records:
        entries = settings.get('hooks', {}).get(record['event'], [])
        retained = []
        for entry in entries:
            if not isinstance(entry, dict):
                retained.append(entry)
                continue
            hooks = entry.get('hooks', [])
            remaining = [hook for hook in hooks if hook != {'type': 'command', 'command': record['command'], 'timeout': record['timeout']}]
            if len(remaining) != len(hooks):
                changed = True
                if remaining:
                    retained.append({**entry, 'hooks': remaining})
            else:
                retained.append(entry)
        if changed:
            settings['hooks'][record['event']] = retained
    return installation_json(settings) if changed else None


def plan(installation, platform=None, pi_executable=None, claude_hooks=False):
    platform = platform or sys.platform
    if platform != 'darwin' and not platform.startswith('linux'):
        raise ValueError('Native Windows supports core profiles only. Use WSL2 for workflow automation.')
    if 'pi' not in installation.agents:
        raise ValueError('Select the Pi profile for the scheduled coding backend')
    if claude_hooks and 'claude' not in installation.agents:
        raise ValueError('Select the Claude profile for optional Claude hooks')
    state = installation.target(STATE)
    previous = installation.previous.get('files', {})
    managed_before = any(name.startswith(PREFIX + '/workflow/') for name in previous)
    if not managed_before and state.exists() and any(state.iterdir()):
        raise ValueError('Existing workflow state has no installation ownership. Coordinate migration; do not import jobs.')
    legacy = installation.home / '.pi/workflows/autoworkflow'
    if not managed_before and legacy.exists():
        raise ValueError('An existing workflow controller needs coordinated migration. Do not install a competing controller.')
    code = installation.home / PREFIX / 'workflow'
    python = str(Path(sys.executable).absolute())
    for name in FILES:
        source = installation.source / 'workflow' / name
        if source.is_symlink() or not source.is_file():
            raise ValueError('A required public workflow source file is missing or symlinked: ' + name)
        installation.add(PREFIX + '/workflow/' + name, source.read_bytes())
    for name in ADAPTERS:
        source = installation.source / 'workflow/bin' / name
        if source.is_symlink() or not source.is_file():
            raise ValueError('A required public CLI adapter is missing or symlinked: ' + name)
        installation.add(PREFIX + '/workflow/bin/' + name, source.read_bytes(), mode=0o700)
    launcher = '#!/bin/sh\nexec ' + shlex.quote(python) + ' -I ' + shlex.quote(str(code / 'workflow.py')) + ' "$@"\n'
    installation.add('.local/bin/agent-workflow', launcher.encode(), mode=0o700)
    locked_pi = installation.source / 'node_modules/.bin/pi'
    pi = discover('pi', pi_executable or (str(locked_pi) if locked_pi.is_file() else None))
    installation.add(PREFIX + '/clients.json', installation_json(registry(pi)))
    skill = ('---\nname: portable-pr-workflow\ndescription: Use the installed portable PR controller for authorized implementation and review.\n---\n\n'
             'Read `' + str(code / 'SKILL.md') + '` completely.\nInstallation grants no action authority.\n')
    installation.add('.agents/skills/portable-pr-workflow/SKILL.md', skill.encode())
    if 'claude' in installation.agents:
        installation.add('.claude/skills/portable-pr-workflow/SKILL.md', skill.encode())
    settings_path = installation.target('.pi/agent/settings.json')
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    initial(installation, STATE + '/controller-config.json', {
        'version': 1, 'backend': 'pi', 'background_enabled': False, 'pi_executable': pi,
        'defaultProvider': installation.provider or settings.get('defaultProvider'),
        'defaultModel': installation.model or settings.get('defaultModel'),
        'native_compaction': str(installation.home / '.pi/agent/extensions/openai-native-compaction.ts'),
        'executables': {name: path for name in ('gh', 'git', 'az') if (path := discover(name))},
        'delegation': {'comments': False, 'approvals': False}, 'external_watches': []})
    initial(installation, STATE + '/scheduler-config.json', {'max_parallel_workers': 4, 'global_cycles_per_day': 48,
                                                          'probe_interval': 300, 'dispatch_interval': 5})
    initial(installation, STATE + '/ado-config.json', {'enabled': False, 'auto_start_reviews': False, 'account': '', 'organizations': []})
    initial(installation, STATE + '/github-config.json', {'enabled': False, 'auto_start_reviews': False, 'logins': [],
                                                        'repository_watches': []})
    initial(installation, STATE + '/teams-config.json', {
        'enabled': False, 'auto_start_reviews': False, 'account': '', 'self_id': '', 'since': '',
        'display_names': [], 'github_owners': [], 'ado_organizations': [], 'reader_command': [],
        'reader_contract': {'version': 1, 'read_only': True, 'silent_only': True},
        'scan_all_sources': True, 'background_silent_only': True})
    initial(installation, STATE + '/action-notices-config.json', {'enabled': False, 'desktop': False,
                                                               'sources': ['teams', 'ado', 'github', 'explicit']})
    installation.add('.pi/agent/extensions/ready-agent-workflow.ts',
                     ('export { default } from "../../../.agents/ready-to-use-agent/workflow/ownership.ts";\n').encode())
    extension = installation.source / 'extensions/workflow-ownership/index.ts'
    if extension.is_symlink():
        raise ValueError('Refusing a symlinked ownership extension')
    installation.add(PREFIX + '/workflow/ownership.ts', extension.read_bytes())
    paths = scheduler_files(installation.home, code, python, platform, os.environ.get('PATH', os.defpath))
    for relative, data in paths.items():
        installation.add(relative, data)
    installation.add(STATE + '/service-manifest.json', installation_json({'version': 1, 'platform': platform, 'home': str(installation.home),
            'files': {relative: hashlib.sha256(data).hexdigest() for relative, data in paths.items()}}))
    if claude_hooks:
        hook_plan(installation, code, python)


def guard_uninstall(installation):
    root = installation.home / STATE
    config = root / 'controller-config.json'
    if config.exists() and json.loads(config.read_text()).get('background_enabled'):
        raise ValueError('Disable the workflow service explicitly before uninstalling it')
    for path in (root / 'jobs').glob('*.json'):
        if json.loads(path.read_text()).get('status') != 'done':
            raise ValueError('An enrolled task still retains ownership. Preserve its controller until merge, close, or coordinated migration.')
