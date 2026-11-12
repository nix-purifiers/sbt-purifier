import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("mode", Path(__file__).parents[1] / "pkgs/sbt/implementation-mode.py")
mode = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mode)


class ImplementationModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = {"HOME": str(self.root / "home")}
        self.home = self.root / "runner"
        self.system = self.root / "system-sbtopts"
        (self.home / "conf").mkdir(parents=True)

    def sources(self, arguments=()):
        return mode.client_sources(arguments, self.environment, self.root, self.home, self.system)

    def test_arguments_and_environment(self):
        self.assertEqual(self.sources(["--server", "test"]), [])
        self.assertEqual(self.sources(['set foo := "--client"']), [])
        for flag in ("--client", "-client", "--jvm-client"):
            self.assertIn("command arguments", self.sources([flag, "--server"]))
            self.environment["SBT_OPTS"] = flag
            self.assertIn("SBT_OPTS", self.sources(["--server"]))
        self.environment.clear()
        self.environment.update(HOME=str(self.root / "home"), SBT_NATIVE_CLIENT="true")
        self.assertIn("SBT_NATIVE_CLIENT", self.sources())

    def test_project_config_sticky_jvm_client(self):
        (self.root / ".sbtopts").write_text("--jvm-client\n")
        self.assertIn(str(self.root / ".sbtopts"), self.sources(["--server"]))
        (self.root / ".sbtopts").write_text("# --client\n-Dexample=--client # comment\n")
        self.assertEqual(self.sources(), [])

    def test_native_config_precedence(self):
        self.system.write_text("--client\n")
        self.assertIn(str(self.system), self.sources())
        configured = Path(self.environment["HOME"]) / ".config/sbt/sbtopts"
        configured.parent.mkdir(parents=True)
        configured.write_text("-Xmx1g\n")
        self.assertEqual(self.sources(), [])
        configured.write_text("--jvm-client\n")
        self.environment["XDG_CONFIG_HOME"] = ""
        self.assertIn(str(configured), self.sources())
        override = self.root / "explicit-sbtopts"
        override.write_text("--server\n")
        self.environment["SBT_ETC_FILE"] = str(override)
        self.assertEqual(self.sources(), [])

    def test_distribution_fallback(self):
        distribution = self.home / "conf/sbtopts"
        distribution.write_text("--client\n")
        self.assertIn(str(distribution), self.sources())
        self.system.write_text("--server\n")
        self.assertEqual(self.sources(), [])


if __name__ == "__main__":
    unittest.main()
