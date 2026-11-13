#!/usr/bin/env python3
"""Install portable configuration without importing credentials or runtime state."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

SOURCE = Path(__file__).resolve().parent
START = '<!-- BEGIN READY-TO-USE-AGENT -->'
END = '<!-- END READY-TO-USE-AGENT -->'
PROFILES = {'pi': '.pi/agent/AGENTS.md', 'claude': '.claude/CLAUDE.md', 'codex': '.codex/AGENTS.md'}
NATIVE_FILES = ('core.ts', 'implementation.ts', 'recovery.ts', 'index.ts')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode()


def private_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as out:
            os.chmod(temporary, mode)
            out.write(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def managed_block(current, body):
    block = START + '\n' + body.rstrip() + '\n' + END
    if current.count(START) != current.count(END) or current.count(START) > 1:
        raise ValueError('The existing managed instruction block is malformed')
    if START in current:
        left, tail = current.split(START, 1)
        _, right = tail.split(END, 1)
        return left + block + right
    return current.rstrip() + ('\n\n' if current.strip() else '') + block + '\n'


class Installation:
    def __init__(self, home, agents=('pi',), source=SOURCE, provider=None, model=None, threshold=200000):
        self.home = Path(home).expanduser().absolute()
        self.source = Path(source)
        self.agents = list(dict.fromkeys(agents))
        if not self.agents or any(a not in PROFILES for a in self.agents):
            raise ValueError('Select pi, claude, or codex profiles')
        if bool(provider) != bool(model):
            raise ValueError('Select both --provider and --model, or neither')
        if not isinstance(threshold, int) or not 1024 <= threshold <= 1000000:
            raise ValueError('The native threshold must be between 1024 and 1000000 tokens')
        self.provider, self.model, self.threshold = provider, model, threshold
        self.prefix = self.home / '.agents/ready-to-use-agent'
        self.manifest_path = self.target('.agents/ready-to-use-agent/install-manifest.json')
        self.previous = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {'files': {}}
        if not isinstance(self.previous, dict) or not isinstance(self.previous.get('files'), dict):
            raise ValueError('The installation manifest must contain a files object')
        if self.previous.get('version', 1) != 1:
            raise ValueError('The previous installation manifest has an unsupported version')
        self.actions = []

    def target(self, relative):
        relative_path = Path(relative)
        if relative_path.is_absolute() or '..' in relative_path.parts:
            raise ValueError('The installation target must be a safe relative path')
        path = self.home / relative_path
        if path != self.home and self.home not in path.parents:
            raise ValueError('The installation target is outside the selected home')
        for item in (path, *path.parents):
            if item == self.home:
                break
            if item.is_symlink():
                raise ValueError('Refusing a symlinked installation target: ' + str(item))
        return path

    def add(self, relative, data, merge=False, mode=0o600):
        target = self.target(relative)
        before = target.read_bytes() if target.exists() else None
        previous = self.previous.get('files', {}).get(relative)
        if before is not None and before != data and not merge:
            if not previous or digest(before) != previous.get('sha256'):
                raise ValueError('Refusing to replace an unmanaged or edited file: ' + str(target))
        self.actions.append({'relative': relative, 'target': target, 'before': before, 'data': data, 'mode': mode})

    def plan(self):
        self.actions = []
        for name in ('agent-setup.md', 'writing.md'):
            self.add('.agents/ready-to-use-agent/policies/' + name, (self.source / 'policies' / name).read_bytes())
        policy = self.prefix / 'policies/agent-setup.md'
        writing = self.prefix / 'policies/writing.md'
        skill = ('---\nname: ready-agent-setup\n'
                 'description: Apply shared writing, task ownership, review, validation, and safety rules.\n'
                 'license: MIT\n---\n\n# Agent setup\n\nRead `' + str(policy) + '` and `' + str(writing) + '` completely.\n'
                 'Preserve native permissions and all narrower task restrictions.\n')
        self.add('.agents/skills/ready-agent-setup/SKILL.md', skill.encode())
        if 'claude' in self.agents:
            self.add('.claude/skills/ready-agent-setup/SKILL.md', skill.encode())
        body = ('## Portable agent setup\n\nRead `' + str(policy) + '` and `' + str(writing) + '` before work.\n'
                'Preserve native permissions, folder trust, authentication, and all narrower task restrictions.\n'
                'Installation grants no PR, cloud, messaging, or production authority.\n')
        for agent in self.agents:
            relative = PROFILES[agent]
            path = self.target(relative)
            before = path.read_text() if path.exists() else ''
            updated = managed_block(before, body)
            previous = self.previous.get('files', {}).get(relative)
            if START in before:
                old_block = START + before.split(START, 1)[1].split(END, 1)[0] + END
                new_block = START + updated.split(START, 1)[1].split(END, 1)[0] + END
                expected = previous.get('block_sha256') if previous else None
                if (expected and digest(old_block.encode()) != expected) or (not expected and old_block != new_block):
                    raise ValueError('Refusing to replace an edited or unmanaged instruction block: ' + str(path))
            self.add(relative, updated.encode(), merge=True)
        if 'pi' in self.agents:
            self.pi_plan()
        return [{'path': str(a['target']), 'state': 'unchanged' if a['before'] == a['data'] else 'planned'} for a in self.actions]

    def pi_plan(self):
        relative = '.pi/agent/settings.json'
        path = self.target(relative)
        settings = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(settings, dict):
            raise ValueError('Pi settings must contain a JSON object')
        for package in settings.get('packages', []):
            source = package.get('source', '') if isinstance(package, dict) else str(package)
            if 'github.com/aaronlichen/ready-to-use-agent' in source.lower():
                raise ValueError('This Pi package is already installed. Use one installation method, not both.')
        settings.setdefault('theme', 'dark')
        settings.setdefault('defaultThinkingLevel', 'max')
        settings.setdefault('defaultProjectTrust', 'ask')
        compaction = settings.setdefault('compaction', {})
        if not isinstance(compaction, dict):
            raise ValueError('Pi compaction settings must contain a JSON object')
        for key, value in {'enabled': True, 'reserveTokens': 16384, 'keepRecentTokens': 20000}.items():
            compaction.setdefault(key, value)
        if self.provider:
            settings.update(defaultProvider=self.provider, defaultModel=self.model)
        self.add(relative, json_bytes(settings), merge=True)
        for name in NATIVE_FILES:
            self.add('.pi/workflows/openai-native-compaction/' + name,
                     (self.source / 'extensions/openai-native-compaction' / name).read_bytes())
        self.add('.pi/workflows/openai-native-compaction/package.json', json_bytes({'type': 'module'}))
        entry = ('import { installNativeCompaction } from "../../workflows/openai-native-compaction/index.ts";\n'
                 'export default function (pi) { installNativeCompaction(pi, { thresholdTokens: '
                 + str(self.threshold) + ' }); }\n')
        self.add('.pi/agent/extensions/openai-native-compaction.ts', entry.encode())

    def apply(self):
        if not self.actions:
            self.plan()
        installation_id = uuid.uuid4().hex
        records = dict(self.previous.get('files', {}))
        changed = []
        try:
            for action in self.actions:
                path, before, data = action['target'], action['before'], action['data']
                current = path.read_bytes() if path.exists() else None
                if current != before:
                    raise ValueError('A file changed after planning: ' + str(path))
                record = records.get(action['relative'])
                if record is None:
                    backup = None
                    if before is not None:
                        backup = self.prefix / 'backups' / installation_id / action['relative']
                        private_write(backup, before)
                    record = {'before_existed': before is not None, 'backup': str(backup) if backup else None,
                              'before_sha256': digest(before) if before is not None else None,
                              'before_mode': path.stat().st_mode & 0o777 if path.exists() else action['mode']}
                if before != data:
                    private_write(path, data, action['mode'])
                    changed.append(action)
                record['sha256'] = digest(data)
                if action['relative'] in PROFILES.values():
                    text = data.decode()
                    block = START + text.split(START, 1)[1].split(END, 1)[0] + END
                    record['block_sha256'] = digest(block.encode())
                records[action['relative']] = record
            manifest = {'version': 1, 'installed_at': datetime.now(timezone.utc).isoformat(),
                        'agents': self.agents, 'files': records}
            private_write(self.manifest_path, json_bytes(manifest))
        except Exception:
            for action in reversed(changed):
                path = action['target']
                if path.exists() and digest(path.read_bytes()) == digest(action['data']):
                    if action['before'] is None:
                        path.unlink()
                    else:
                        private_write(path, action['before'])
            raise
        return {'installed_files': len(self.actions), 'changed_files': len(changed), 'manifest': str(self.manifest_path)}

    def uninstall(self, dry_run=False):
        if not self.manifest_path.exists():
            return {'removed': [], 'preserved_edited_files': [], 'state': 'not_installed'}
        removed, preserved, pending, keep = [], [], [], {}
        for relative, record in self.previous.get('files', {}).items():
            path = self.target(relative)
            if not path.exists():
                continue
            if digest(path.read_bytes()) != record['sha256']:
                preserved.append(str(path))
                keep[relative] = record
                continue
            backup = Path(record['backup']) if record.get('backup') else None
            if backup and self.prefix.resolve() not in backup.resolve().parents:
                raise ValueError('The backup path is outside this installation')
            before = None
            if record.get('before_existed'):
                if not backup or not backup.exists():
                    raise ValueError('The original configuration backup is missing')
                before = backup.read_bytes()
                if record.get('before_sha256') and digest(before) != record['before_sha256']:
                    raise ValueError('The original configuration backup changed')
            pending.append((relative, path, record, before))
        for relative, path, record, before in pending:
            if digest(path.read_bytes()) != record['sha256']:
                preserved.append(str(path))
                keep[relative] = record
                continue
            if not dry_run:
                if before is not None:
                    private_write(path, before, record.get('before_mode', 0o600))
                else:
                    path.unlink()
            removed.append(str(path))
        if not dry_run:
            if keep:
                manifest = {**self.previous, 'files': keep}
                private_write(self.manifest_path, json_bytes(manifest))
            else:
                self.manifest_path.unlink()
        return {'removed': removed, 'preserved_edited_files': preserved, 'dry_run': dry_run}


def doctor(home):
    results = {'python': sys.version.split()[0], 'node': None, 'pi': None, 'provider_login': 'not tested',
               'schedulers': 'not started', 'credentials': 'not imported'}
    env = {**os.environ, 'PI_OFFLINE': '1'}
    for name in ('node', 'pi'):
        path = shutil.which(name)
        if path:
            try:
                proc = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=10, env=env)
                results[name] = proc.stdout.strip() if proc.returncode == 0 else 'version test failed'
            except (OSError, subprocess.TimeoutExpired):
                results[name] = 'version test failed'
    manifest = Path(home).expanduser() / '.agents/ready-to-use-agent/install-manifest.json'
    results['configuration_installed'] = manifest.exists()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--agents', nargs='+', choices=tuple(PROFILES), default=['pi'])
    parser.add_argument('--provider')
    parser.add_argument('--model')
    parser.add_argument('--native-threshold', type=int, default=200000)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--uninstall', action='store_true')
    parser.add_argument('--doctor', action='store_true')
    args = parser.parse_args()
    try:
        if args.doctor:
            result = doctor(args.home)
        else:
            installation = Installation(args.home, args.agents, provider=args.provider, model=args.model, threshold=args.native_threshold)
            result = installation.uninstall(args.dry_run) if args.uninstall else installation.plan() if args.dry_run else installation.apply()
        print(json.dumps(result, indent=2))
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
