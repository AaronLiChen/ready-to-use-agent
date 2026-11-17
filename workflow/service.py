#!/usr/bin/env python3
"""Opt-in macOS and Linux user-service control. No service starts during installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import workflow as w
import configuration

LABEL = 'org.ready-to-use-agent.workflow'
UNIT = 'ready-agent-workflow'


def manifest():
    path = w.ROOT / 'service-manifest.json'
    data = w.load(path)
    if data.get('version') != 1 or data.get('platform') not in ('darwin', 'linux'):
        raise ValueError('No supported installed service manifest')
    home = Path(data['home'])
    for relative, expected in data['files'].items():
        path = home / relative
        if Path(relative).is_absolute() or '..' in Path(relative).parts or path.is_symlink():
            raise ValueError('Invalid installed service path')
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('An installed service file changed. Preserve the edit and review it before activation.')
    return data


def invoke(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('User-service operation failed. No successful activation is assumed.')


def change(action):
    if any(os.environ.get(key) for key in ('AUTOWORKFLOW_JOB', 'AUTOWORKFLOW_INTAKE', 'AUTOWORKFLOW_AUTH_ID')):
        raise ValueError('Scoped workers cannot activate or disable services')
    data = manifest()
    if action == 'disable':
        import parallel_dispatch
        if parallel_dispatch.active()[0]:
            raise ValueError('A live worker or foreground owner must hand off before service removal')
    with w.lock('service-control'):
        config_path = w.ROOT / 'controller-config.json'
        config = configuration.controller()
        previous = config.get('background_enabled', False)
        config['background_enabled'] = action == 'enable'
        w.save(config_path, config)
        launchd_changed = False
        try:
            if data['platform'] == 'darwin':
                target = 'gui/' + str(os.getuid())
                plist = str(Path(data['home']) / ('Library/LaunchAgents/' + LABEL + '.plist'))
                result = subprocess.run(['launchctl', 'print', target + '/' + LABEL], capture_output=True, text=True, timeout=5)
                expected = str(Path(data['home']) / '.agents/ready-to-use-agent/workflow/service.py')
                if result.returncode == 0 and expected not in result.stdout:
                    raise ValueError('Another registered service owns this label. Coordinate migration before activation.')
                if action == 'enable':
                    launchd_changed = True
                    invoke(['launchctl', 'enable', target + '/' + LABEL])
                    if result.returncode:
                        invoke(['launchctl', 'bootstrap', target, plist])
                else:
                    invoke(['launchctl', 'disable', target + '/' + LABEL])
                    if result.returncode == 0:
                        invoke(['launchctl', 'bootout', target + '/' + LABEL])
            else:
                invoke(['systemctl', '--user', 'daemon-reload'])
                invoke(['systemctl', '--user', 'enable' if action == 'enable' else 'disable', '--now', UNIT + '.timer'])
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            if launchd_changed and not previous:
                try:
                    invoke(['launchctl', 'disable', target + '/' + LABEL])
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    pass
            config['background_enabled'] = previous
            w.save(config_path, config)
            raise
        config['background_enabled'] = action == 'enable'
        w.save(config_path, config)
    return {'state': 'enabled' if action == 'enable' else 'disabled', 'intake_authority': 'unchanged',
            'comment_and_approval_delegation': 'unchanged'}


def run():
    if any(os.environ.get(key) for key in ('AUTOWORKFLOW_JOB', 'AUTOWORKFLOW_INTAKE')):
        raise ValueError('Scoped workers cannot start a competing scheduler')
    if configuration.controller().get('background_enabled') is not True:
        return {'state': 'disabled', 'provider_reads': 'not run', 'workers': 'not started'}
    with w.lock('service-tick', nonblocking=True) as acquired:
        if not acquired:
            return {'state': 'busy'}
        import auth_recovery, github_auth, parallel_dispatch
        auth_recovery.tick()
        github_auth.health_tick()
        path = w.ROOT / 'service-state.json'
        state = w.load(path) if path.exists() else {}
        if time.time() - state.get('last_intake_epoch', 0) >= 300:
            errors = {}
            for kind in ('ado', 'github', 'teams'):
                try:
                    adapter = w.intake_adapter({'source_kind': kind})
                    adapter.poll()
                except Exception as error:
                    errors[kind] = type(error).__name__
                    if state.get('intake_errors', {}).get(kind) != errors[kind]:
                        w.notify('Review intake is incomplete for ' + kind + '. Inspect its configuration and coverage.')
            state['intake_errors'] = errors
            state['last_intake_epoch'] = time.time()
            w.save(path, state)
        parallel_dispatch.run()
    return {'state': 'cycle_complete', 'terminal_results': 'provider-verified only',
            'intake_errors': state.get('intake_errors', {})}


def status():
    result = {'background_enabled': configuration.controller().get('background_enabled') is True,
              'intake': {}, 'delegation': configuration.delegation(), 'service_installed': False}
    try:
        data = manifest()
        result.update(service_installed=True, platform=data['platform'])
    except (OSError, ValueError):
        result['service_files'] = 'absent or edited'
    for kind in ('ado', 'github', 'teams'):
        path = w.ROOT / (kind + '-config.json')
        result['intake'][kind] = bool(path.exists() and w.load(path).get('enabled'))
    return result


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'enable', 'disable', 'status'))
    args = parser.parse_args()
    try:
        print(json.dumps(run() if args.action == 'run' else status() if args.action == 'status' else change(args.action), indent=2))
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
