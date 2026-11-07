"""Retain actual composition commands and selected outputs, including failures."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.parse import urlparse


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def terminal_text(output):
    """Remove terminal CSI controls without changing version or origin text."""
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)


def observed_origins(output, expected):
    """The JVM hashes CodeSource bytes before sbt removes shadow JARs."""
    output = terminal_text(output)
    observed = {}
    for line in output.splitlines():
        if line.startswith('[info] '):
            line = line[len('[info] '):]
        if not line.startswith('COMPOSITION_ORIGIN|'):
            continue
        _, name, uri, checksum = line.split('|')
        if name not in expected:
            continue
        parsed = urlparse(uri)
        if (name in observed or parsed.scheme != 'file' or parsed.netloc
                or re.fullmatch(r'[0-9a-f]{64}', checksum) is None):
            raise ValueError('invalid runtime origin observation')
        observed[name] = {'origin': uri, 'sha256': checksum}
    if set(observed) != set(expected):
        raise ValueError('missing runtime origin observation')
    return observed


class Evidence:
    def __init__(self, directory, budget=3000):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.deadline = time.monotonic() + budget
        self.receipt = {'complete': False, 'commands': [], 'selectedOutputs': []}
        self.save()

    def save(self):
        (self.directory / 'receipt.json').write_text(json.dumps(self.receipt, indent=2) + '\n')

    def output(self, command, cwd=None, env=None, text=True, timeout=1800):
        entry = {'argv': [str(x) for x in command], 'cwd': str(Path(cwd or '.').resolve())}
        index = len(self.receipt['commands'])
        entry.update(stdout=f'command-{index:03}.stdout.log', stderr=f'command-{index:03}.stderr.log')
        self.receipt['commands'].append(entry)
        self.save()
        process = None
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            with (self.directory / entry['stdout']).open('wb') as stdout, (self.directory / entry['stderr']).open('wb') as stderr:
                process = subprocess.Popen(entry['argv'], cwd=cwd, env=env, stdout=stdout,
                                           stderr=stderr, start_new_session=True)
                try:
                    entry['returncode'] = process.wait(timeout=min(timeout, remaining))
                finally:
                    # Own the whole group: direct-child exit is not proof that
                    # descendants stopped. Files avoid inherited-pipe EOF waits.
                    for sig in (signal.SIGTERM, signal.SIGKILL):
                        try:
                            os.killpg(process.pid, sig)
                        except ProcessLookupError:
                            pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        # Keep the original command timeout/exception visible.
                        entry['cleanupError'] = 'owned direct child did not reap after SIGKILL'
            if entry['returncode']:
                raise subprocess.CalledProcessError(entry['returncode'], command)
            payload = (self.directory / entry['stdout']).read_bytes()
            return payload.decode() if text else payload
        except BaseException as error:
            entry['error'] = type(error).__name__ + ': ' + str(error)
            if process is not None:
                entry['returncode'] = process.returncode
            for name in ('stdout', 'stderr'):
                path = self.directory / entry[name]
                if path.exists():
                    print(path.read_text(errors='replace')[-20000:], flush=True)
            raise
        finally:
            for name in ('stdout', 'stderr'):
                path = self.directory / entry[name]
                if path.exists():
                    entry[name + 'Sha256'] = sha256(path)
            self.save()

    def run(self, command, **kwargs):
        kwargs.pop('check', None)
        self.output(command, **kwargs)

    def selected(self, role, path):
        path = str(Path(path).resolve(strict=True))
        info = json.loads(self.output(['nix', 'path-info', '--offline', '--json', '--json-format', '1', path]))
        record = info[path]
        drv = record.get('deriver')
        if (not path.startswith('/nix/store/') or not drv or not drv.endswith('.drv')
                or re.fullmatch(r'sha256-[A-Za-z0-9+/]{43}=', record.get('narHash', '')) is None):
            raise ValueError('selected output lacks actual derivation/NAR identity: ' + path)
        definition = json.loads(self.output(['nix', 'derivation', 'show', '--offline', drv]))
        subject = definition.get('derivations', {}).get(Path(drv).name)
        if (subject is None or not any(value.get('path') in (path, Path(path).name)
                for value in subject['outputs'].values())
                or subject['system'] != self.receipt['system']):
            raise ValueError('selected output does not match actual derivation/platform: ' + path)
        entry = {'role': role, 'path': path, 'drvPath': drv, 'narHash': record['narHash'],
                 'pathInfo': info, 'derivation': definition}
        self.receipt['selectedOutputs'].append(entry)
        self.save()
        return entry

    def files(self, label, directory):
        files = {}
        for path in sorted(Path(directory).rglob('*')):
            if path.is_file():
                files[str(path.relative_to(directory))] = {'sha256': sha256(path),
                    'executable': bool(path.stat().st_mode & 0o111)}
        (self.directory / (label + '.json')).write_text(json.dumps(files, indent=2) + '\n')
        return files
