"""Retain actual composition commands and selected outputs, including failures."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
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
        self.receipt = {'complete': False, 'logsSealed': False, 'commands': [], 'selectedOutputs': []}
        self.groups = {}
        self.closed = False
        self.save()

    def save(self):
        (self.directory / 'receipt.json').write_text(json.dumps(self.receipt, indent=2) + '\n')

    @staticmethod
    def _members(group):
        """Linux matrix: identify live session members without reading argv."""
        members = {}
        for path in Path('/proc').glob('[0-9]*/stat'):
            try:
                text = path.read_text()
                fields = text[text.rindex(')') + 2:].split()
                if fields[0] != 'Z' and int(fields[2]) == group and int(fields[3]) == group:
                    members[int(path.parent.name)] = fields[19]  # start time, not PID alone
            except (OSError, ValueError, IndexError):
                continue
        return members

    def _remember(self, group):
        members = self._members(group)
        if members:
            self.groups[group] = members

    def _stop_groups(self, groups):
        pending = set(groups)
        errors = []
        # A remembered live PID/start-time/session must still anchor each
        # group. A reused numeric group ID is never sufficient permission.
        def refresh():
            for group in list(pending):
                current = self._members(group)
                if not current:
                    pending.remove(group)
                    self.groups.pop(group, None)
                elif any(current.get(pid) == birth for pid, birth in self.groups[group].items()):
                    self.groups[group] = current
                else:
                    errors.append(f'group {group}: ownership no longer provable; not signalled')
                    pending.remove(group)
                    self.groups.pop(group, None)
        for sig, grace in [(signal.SIGTERM, 1), (signal.SIGKILL, 2)]:
            refresh()
            for group in pending:
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
            deadline = time.monotonic() + grace
            while pending and time.monotonic() < deadline:
                refresh()
                if pending:
                    time.sleep(.02)
        refresh()
        for group in pending:
            errors.append(f'group {group}: live members after bounded cleanup')
            self.groups.pop(group, None)
        if errors:
            self.receipt.setdefault('cleanupFailures', []).extend(errors)
        return errors

    def close(self):
        if not self.closed:
            self._stop_groups(self.groups)
            self.closed = True
            # Background SDK servers may have appended after a successful
            # client returned. Seal hashes only after owned writers stop.
            for entry in self.receipt['commands']:
                for name in ('stdout', 'stderr'):
                    path = self.directory / entry[name]
                    if path.exists():
                        entry[name + 'Sha256'] = sha256(path)
            self.receipt['logsSealed'] = not bool(self.receipt.get('cleanupFailures'))
            if not self.receipt['logsSealed']:
                self.receipt['complete'] = False
            self.save()
        return self.receipt.get('cleanupFailures', [])

    @contextmanager
    def case(self):
        try:
            yield
        finally:
            failed = sys.exc_info()[0] is not None
            errors = self.close()
            if errors and not failed:
                raise RuntimeError('owned case cleanup failed')

    def output(self, command, cwd=None, env=None, text=True, timeout=1800):
        entry = {'argv': [str(x) for x in command], 'cwd': str(Path(cwd or '.').resolve())}
        index = len(self.receipt['commands'])
        entry.update(stdout=f'command-{index:03}.stdout.log', stderr=f'command-{index:03}.stderr.log')
        self.receipt['commands'].append(entry)
        self.save()
        process = None
        try:
            if self.closed or len(self.groups) >= 128:
                raise RuntimeError('closed case or owned group registry limit reached')
            if not Path('/proc/self/stat').is_file():
                raise RuntimeError('composition group ownership requires Linux procfs')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, 0)
            with (self.directory / entry['stdout']).open('wb') as stdout, (self.directory / entry['stderr']).open('wb') as stderr:
                process = subprocess.Popen(entry['argv'], cwd=cwd, env=env, stdout=stdout,
                                           stderr=stderr, start_new_session=True)
                entry['returncode'] = process.wait(timeout=min(timeout, remaining))
                # Successful clients may deliberately leave an SDK server.
                # Preserve that native state until the entire case finishes.
                self._remember(process.pid)
            if entry['returncode']:
                raise subprocess.CalledProcessError(entry['returncode'], command)
            payload = (self.directory / entry['stdout']).read_bytes()
            return payload.decode() if text else payload
        except BaseException as error:
            entry['error'] = type(error).__name__ + ': ' + str(error)
            if process is not None:
                self._remember(process.pid)
                if process.pid in self.groups:
                    self._stop_groups([process.pid])
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    entry['cleanupError'] = 'owned direct child did not reap after bounded cleanup'
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
