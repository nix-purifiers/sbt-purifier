"""Offline release identity, archive and publication-gate regression tests."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("source", Path(__file__).with_name("source.py"))
source = importlib.util.module_from_spec(spec)
spec.loader.exec_module(source)
WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/release.yml"
SOURCE_HELPER = Path(__file__).with_name("source.py").resolve()


class ReleaseSourceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release@example.invalid")
        for name in ("pkgs/sbt/default.nix", "configs/flake/default.nix", "tests/dependencies/flake.lock", "src/Plugin.scala"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old\n")
        self.sha = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args]).decode().strip()

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_runtime_and_exported_test_inputs_change_identity_without_plugin_change(self):
        old = source.fingerprint(self.sha)
        plugin = (self.root / "src/Plugin.scala").read_bytes()
        for name in ("pkgs/sbt/default.nix", "configs/flake/default.nix", "tests/dependencies/flake.lock"):
            with self.subTest(name=name):
                (self.root / name).write_text("changed\n")
                new = self.commit()
                self.assertNotEqual(old, source.fingerprint(new))
                self.assertEqual(plugin, (self.root / "src/Plugin.scala").read_bytes())
                old = source.fingerprint(new)

    def test_mode_changes_and_noop(self):
        identity = source.fingerprint(self.sha)
        self.assertEqual(identity, source.fingerprint("HEAD"))
        self.git("update-index", "--chmod=+x", "src/Plugin.scala")
        self.git("commit", "-qm", "mode")
        self.assertNotEqual(identity, source.fingerprint("HEAD"))

    def test_archive_is_exact_commit_and_checksums_leave_maven_sums_alone(self):
        destination = self.root / "assets"
        destination.mkdir()
        (destination / "SHA256SUMS").write_bytes(b"legacy Maven checksums\n")
        # Dirty and untracked files must not leak into the released source.
        (self.root / "src/Plugin.scala").write_text("dirty\n")
        (self.root / "untracked").write_text("secret\n")
        source.prepare("example/sbt-purifier", self.sha, "0.1.22", destination)
        archive = (destination / "source.tar.gz").read_bytes()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            self.assertEqual(tar.extractfile("sbt-purifier/src/Plugin.scala").read(), b"old\n")
            self.assertNotIn("sbt-purifier/untracked", tar.getnames())
        manifest = json.loads((destination / "release-manifest.json").read_text())
        self.assertEqual(manifest["commit"], self.sha)
        self.assertEqual(manifest["mavenVersion"], "0.1.22")
        self.assertEqual(manifest["fingerprint"], source.fingerprint(self.sha))
        for line in (destination / "SOURCE-SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((destination / name).read_bytes()).hexdigest())
        self.assertEqual((destination / "SHA256SUMS").read_bytes(), b"legacy Maven checksums\n")
        source.prepare("example/sbt-purifier", self.sha, "0.1.22", destination)
        self.assertEqual(archive, (destination / "source.tar.gz").read_bytes())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.sha)
        self.assertEqual(self.git("tag", "--list"), "")

    def test_publication_gates_require_exact_completed_main_check(self):
        good = {"head_sha": self.sha, "head_branch": "main", "status": "completed",
                "conclusion": "success", "event": "push"}
        def check(run, main=None, requested=None):
            with patch.object(source, "api", side_effect=[{"object": {"sha": main or self.sha}},
                                                         {"workflow_runs": [run]}]):
                source.guard("example/sbt-purifier", requested or self.sha)
        check(good)
        for field, value in (("head_sha", "0" * 40), ("head_branch", "other"),
                             ("status", "in_progress"), ("conclusion", "failure"), ("event", "pull_request")):
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                check({**good, field: value})
        with self.assertRaises(RuntimeError):
            check(good, main="0" * 40)
        with self.assertRaises(RuntimeError):
            check(good, requested="0" * 40)

    def test_actual_workflow_decision_with_equal_maven_bytes(self):
        # Execute the production Bash decision, replacing only external API
        # reads and the expensive Nix build with finite offline fixtures.
        release = self.root / ".github/release"
        release.mkdir(parents=True)
        shutil.copyfile(SOURCE_HELPER, release / "source.py")
        (release / "series").write_text("0.1\n")
        build = release / "build.sh"
        build.write_text('#!/usr/bin/env bash\nset -eu\nmkdir -p "$2"\nprintf "same Maven bytes\\n" >"$2/SHA256SUMS"\n')
        build.chmod(0o755)
        self.sha = self.commit()
        self.git("tag", "v0.1.21")
        workflow = WORKFLOW.read_text()
        block = workflow.split("      - name: Decide the version\n", 1)[1].split("      - name: Build and create the release\n", 1)[0]
        shell = "\n".join(line[10:] for line in block.split("        run: |\n", 1)[1].splitlines())
        with tempfile.TemporaryDirectory() as sandbox:
            sandbox = Path(sandbox)
            executable = sandbox / "gh"
            executable.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
sha = os.environ["SHA"]
if args[0] == "api":
    if args[1].endswith("git/ref/heads/main"):
        print(json.dumps({"object": {"sha": sha}}))
    else:
        print(json.dumps({"workflow_runs": [{"head_sha": sha, "head_branch": "main", "status": "completed", "conclusion": "success", "event": "push"}]}))
elif args[:2] == ["release", "download"]:
    destination = pathlib.Path(args[args.index("--dir") + 1])
    destination.mkdir(parents=True)
    (destination / "SHA256SUMS").write_text("same Maven bytes\\n")
else:
    raise SystemExit("Unexpected external action: " + repr(args))
''')
            executable.chmod(0o755)
            def decide(label):
                runner = sandbox / label
                runner.mkdir()
                output = runner / "outputs"
                environment = {**os.environ, "PATH": str(sandbox) + os.pathsep + os.environ["PATH"],
                               "SHA": self.git("rev-parse", "HEAD"), "GITHUB_REPOSITORY": "example/sbt-purifier",
                               "GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(runner), "SITE_ONLY": "false"}
                subprocess.run(["bash", "-c", shell], env=environment, check=True, capture_output=True)
                return dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(decide("same"), {"release": "false"})
            (self.root / "pkgs/sbt/default.nix").write_text("native runtime changed\n")
            self.commit()
            self.assertEqual(decide("runtime"), {"version": "0.1.22", "release": "true"})
            self.assertEqual(self.git("tag", "--list"), "v0.1.21")


if __name__ == "__main__":
    unittest.main()
