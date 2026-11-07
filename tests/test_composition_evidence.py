import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from composition_evidence import Evidence, sha256, observed_origins, terminal_text


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.evidence = Evidence(self.root / 'receipts')
        self.addCleanup(self.evidence.close)

    def test_failure_retains_original_status_logs_and_incomplete_receipt(self):
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.evidence.output([sys.executable, '-c', 'import sys; print("out"); print("err",file=sys.stderr); sys.exit(7)'])
        self.assertEqual(caught.exception.returncode, 7)
        receipt = json.loads((self.evidence.directory / 'receipt.json').read_text())
        self.assertFalse(receipt['complete'])
        command = receipt['commands'][0]
        self.assertEqual(command['returncode'], 7)
        self.assertEqual((self.evidence.directory / command['stdout']).read_text(), 'out\n')
        self.assertEqual((self.evidence.directory / command['stderr']).read_text(), 'err\n')
        self.assertEqual(command['stdoutSha256'], sha256(self.evidence.directory / command['stdout']))

    def test_timeout_kills_descendant_even_when_parent_exits_on_term(self):
        pidfile = self.root / 'child.pid'
        child = 'import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid())); time.sleep(60)'
        parent = 'import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,"-c",sys.argv[1]]);\nwhile not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\ntime.sleep(60)'
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.evidence.output([sys.executable, '-c', parent, child, str(pidfile)], timeout=1)
        self.assertLess(time.monotonic() - started, 8)
        pid = int(pidfile.read_text())
        status = Path(f'/proc/{pid}/stat')
        deadline = time.monotonic() + 2
        while status.exists() and status.read_text().split()[2] != 'Z' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
        receipt = json.loads((self.evidence.directory / 'receipt.json').read_text())
        self.assertIn('TimeoutExpired', receipt['commands'][0]['error'])
        self.assertFalse(receipt['complete'])

    def test_exhausted_total_budget_fails_before_starting_command(self):
        self.evidence.deadline = time.monotonic() - 1
        with self.assertRaises(subprocess.TimeoutExpired):
            self.evidence.output([sys.executable, '-c', 'raise AssertionError("must not run")'])
        self.assertNotIn('returncode', self.evidence.receipt['commands'][0])

    def test_successful_client_preserves_server_until_case_cleanup_and_log_seal(self):
        pidfile = self.root / 'server.pid'
        trigger = self.root / 'next-command'
        survived = self.root / 'survived'
        child = ('import os,signal,time,pathlib; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                 'pathlib.Path(' + repr(str(pidfile)) + ').write_text(str(os.getpid()));\n'
                 'while not pathlib.Path(' + repr(str(trigger)) + ').exists(): time.sleep(.01)\n'
                 'print("server preserved across commands",flush=True)\n'
                 'pathlib.Path(' + repr(str(survived)) + ').write_text("yes")\n'
                 'time.sleep(60)')
        parent = ('import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,"-c",sys.argv[1]]);\n'
                  'while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)')
        with self.evidence.case():
            self.evidence.output([sys.executable, '-c', parent, child, str(pidfile)])
            pid = int(pidfile.read_text())
            status = Path(f'/proc/{pid}/stat')
            self.assertTrue(status.exists() and status.read_text().split()[2] != 'Z')
            self.evidence.output([sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(trigger)) + ').touch()'])
            deadline = time.monotonic() + 2
            while not survived.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(survived.exists(), 'native server was killed between successful commands')
            self.assertFalse(self.evidence.receipt['logsSealed'])
        self.assertTrue(not status.exists() or status.read_text().split()[2] == 'Z')
        self.assertTrue(self.evidence.receipt['logsSealed'])
        first = self.evidence.receipt['commands'][0]
        self.assertIn('server preserved', (self.evidence.directory / first['stdout']).read_text())
        self.assertEqual(first['stdoutSha256'], sha256(self.evidence.directory / first['stdout']))

    def test_reused_group_identity_is_never_signalled(self):
        self.evidence.groups = {123: {456: 'old-start-time'}}
        with patch.object(self.evidence, '_members', return_value={456: 'new-start-time'}), patch('composition_evidence.os.killpg') as signal_group:
            self.assertTrue(self.evidence.close())
            signal_group.assert_not_called()
        self.assertFalse(self.evidence.receipt['logsSealed'])

    def test_late_case_failure_cannot_retain_complete_true(self):
        path = Path(__file__).with_name('composition-native.py')
        spec = importlib.util.spec_from_file_location('composition_driver_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        results = self.root / 'late-failure'
        def fail_after_operations(args, evidence):
            evidence.receipt['complete'] = True
            raise RuntimeError('late case cleanup failed')
        with patch.object(sys, 'argv', ['composition', '--results', str(results)]), patch.object(module, 'compose', fail_after_operations):
            with self.assertRaisesRegex(RuntimeError, 'late case cleanup failed'):
                module.main()
        receipt = json.loads((results / 'receipt.json').read_text())
        self.assertFalse(receipt['complete'])
        self.assertIn('late case cleanup failed', receipt['failure'])
        self.assertTrue(receipt['logsSealed'])

    def test_selected_output_cannot_borrow_an_unrelated_derivation(self):
        path = str(Path('/').joinpath(*Path(sys.executable).resolve().parts[1:4]))
        drv = '/nix/store/' + 'a'*32 + '-unrelated.drv'
        info = {path: {'deriver': drv, 'narHash': 'sha256-' + 'A'*43 + '='}}
        definition = {'derivations': {Path(drv).name: {
            'system': 'x86_64-linux', 'outputs': {'out': {'path': 'b'*32 + '-other'}}}}}
        self.evidence.receipt['system'] = 'x86_64-linux'
        with patch.object(self.evidence, 'output', side_effect=[json.dumps(info), json.dumps(definition)]):
            with self.assertRaisesRegex(ValueError, 'does not match actual derivation'):
                self.evidence.selected('consumer', path)
        self.assertEqual(self.evidence.receipt['selectedOutputs'], [])

    def test_sbt2_erase_line_controls_do_not_change_exact_version_or_origin(self):
        output = '\x1b[0J\x1b[0m[\x1b[0minfo\x1b[0m] \x1b[0m2.0.12\x1b[0m\x1b[0J\n'
        cleaned = terminal_text(output)
        self.assertRegex(cleaned, r'(?m)^\[info\]\s*2\.0\.12\s*$')
        self.assertNotRegex(cleaned, r'(?m)^\[info\]\s*2\.0\.13\s*$')
        origin = '\x1b[0J[info] COMPOSITION_ORIGIN|proof.Marker|file:/deleted.jar|' + 'a'*64 + '\x1b[0J\n'
        self.assertEqual(observed_origins(origin, {'proof.Marker'})['proof.Marker']['sha256'], 'a'*64)

    def test_runtime_hash_survives_sbt_shadow_jar_removal(self):
        jar = self.root / 'shadow.jar'
        jar.write_bytes(b'actual runtime payload')
        checksum = sha256(jar)
        output = f'COMPOSITION_ORIGIN|proof.Marker|{jar.as_uri()}|{checksum}\n'
        jar.unlink()
        self.assertEqual(observed_origins(output, {'proof.Marker'})['proof.Marker']['sha256'], checksum)
        with self.assertRaisesRegex(ValueError, 'invalid runtime origin'):
            observed_origins(output.replace(checksum, 'directory'), {'proof.Marker'})
        with self.assertRaisesRegex(ValueError, 'missing runtime origin'):
            observed_origins('[info] [nix] warning: ' + output, {'proof.Marker'})


if __name__ == '__main__':
    unittest.main()
