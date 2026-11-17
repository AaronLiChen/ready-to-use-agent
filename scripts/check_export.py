#!/usr/bin/env python3
"""Reject private state and obvious secret material without printing the material."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    'personal-machine-path': re.compile(r'/[U]sers/lichen5|/[h]ome/lichen5|C:\\\\[U]sers\\\\lichen5', re.I),
    'personal-work-identity': re.compile(r'lichen5@microsoft\.com', re.I),
    'internal-source-reference': re.compile(r'go\.goms\.io|msazure\.visualstudio\.com|dev\.azure\.com/msazure|[C]loudNativeCompute'),
    'private-package-mirror': re.compile(r'packagefeedpro[x]y|ms-feed-\d+\.pkgs\.visualstudio\.com', re.I),
    'github-token': re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    'api-secret': re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b'),
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}
FORBIDDEN_PARTS = {'sessions', 'sessions-archive', 'runtime-state', 'node_modules', 'backups', '__pycache__'}
FORBIDDEN_NAMES = {'auth.json', 'models-store.json', 'local-config.json', 'installed.json', '.env'}


def findings(path, root=ROOT):
    relative = path.relative_to(root)
    if any(part in FORBIDDEN_PARTS for part in relative.parts) or path.name in FORBIDDEN_NAMES or path.suffix in ('.jsonl', '.log', '.pem', '.key', '.pfx', '.p12'):
        return ['private-state-file']
    if path.is_symlink():
        return ['symlink-export']
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        return ['binary-export']
    if '\0' in text:
        return ['binary-export']
    return [name for name, pattern in RULES.items() if pattern.search(text)]


def check(root=ROOT):
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            capture_output=True, check=True)
    errors = []
    for name in set(result.stdout.decode().split('\0')) - {''}:
        path = root / name
        if path.exists() or path.is_symlink():
            errors.extend((name, rule) for rule in findings(path, root))
    return sorted(errors)


def main():
    errors = check()
    for name, rule in errors:
        print(name + ': ' + rule)
    if errors:
        print('Export validation failed. No matched secret values were displayed.')
        return 1
    print('Export validation passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
