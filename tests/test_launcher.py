"""The `gh-nightshift` launcher finds a usable Python, including on Windows where it is `python` or `py -3`."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
LAUNCHER = os.path.join(ROOT, "gh-nightshift")
TOOLS = ("bash", "dirname", "readlink", "uname", "env")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class Launcher(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp()
        for tool in TOOLS:
            os.symlink(shutil.which(tool), os.path.join(self.bin, tool))

    def shim(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!" + os.path.join(self.bin, "bash") + "\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def launch(self, **env):
        return subprocess.run([os.path.join(self.bin, "bash"), LAUNCHER, "--version"], capture_output=True, text=True,
                              env={"PATH": self.bin, **env})

    def test_py_launcher_is_used_when_python3_is_a_store_stub(self):
        # Seen on Windows: `python3` exists but only prints a Microsoft Store hint and fails.
        self.shim("python3", 'echo "Python was not found; run without arguments to install from the Microsoft Store"; exit 9')
        self.shim("py", f'[ "$1" = "-3" ] || exit 2; shift; exec "{sys.executable}" "$@"')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Microsoft Store", result.stdout)
        self.assertRegex(result.stdout, r"\d+\.\d+")

    def test_plain_python_is_used(self):
        self.shim("python", f'exec "{sys.executable}" "$@"')
        self.assertEqual(self.launch().returncode, 0)

    def test_override_may_carry_arguments(self):
        self.shim("py", f'[ "$1" = "-3" ] || exit 2; shift; exec "{sys.executable}" "$@"')
        self.shim("python3", "exit 9")
        self.assertEqual(self.launch(NIGHTSHIFT_PYTHON="py -3").returncode, 0)

    def test_no_python_names_what_was_tried(self):
        result = self.launch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("python and py -3", result.stderr)


if __name__ == "__main__":
    unittest.main()
