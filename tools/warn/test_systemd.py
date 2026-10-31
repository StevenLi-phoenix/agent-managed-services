"""warn-unit-failed hands a failed systemd unit to `warn` (stubs stand in for systemctl and warn)."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "systemd" / "warn-unit-failed"


class UnitFailedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self.out = self.bin / "warn.out"
        self.stub("systemctl", 'echo "* $2 - stub status"; echo "Active: failed (Result: exit-code)"')
        self.stub("hostname", 'echo box1')
        self.stub("warn", 'echo "ARGS: $*" > "%s"; cat >> "%s"' % (self.out, self.out))

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n%s\n" % body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run_script(self, *args):
        env = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": self.tmp.name}
        return subprocess.run(["/bin/sh", str(SCRIPT)] + list(args), env=env, capture_output=True, text=True, timeout=20)

    def test_passes_unit_status_and_keys_by_hour(self):
        res = self.run_script("foo.service")
        self.assertEqual(res.returncode, 0, res.stderr)
        text = self.out.read_text()
        self.assertIn("--source box1.systemd", text)
        self.assertIn("--severity error", text)
        self.assertIn("systemd unit foo.service failed on box1", text)
        self.assertRegex(text, r"--key box1-foo\.service-\d{10}")
        self.assertIn("Active: failed", text)  # status text arrives on stdin as --text -

    def test_missing_unit_argument_is_an_error(self):
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(self.out.exists())

    def test_never_fails_when_warn_fails(self):
        self.stub("warn", "exit 7")
        self.assertEqual(self.run_script("foo.service").returncode, 0)

    def test_warn_host_overrides_hostname(self):
        env = {"PATH": "%s:/usr/bin:/bin" % self.bin, "HOME": self.tmp.name, "WARN_HOST": "phm"}
        subprocess.run(["/bin/sh", str(SCRIPT), "foo.service"], env=env, timeout=20)
        self.assertIn("--source phm.systemd", self.out.read_text())
        self.assertIn("failed on phm", self.out.read_text())

    def test_template_has_no_recursion_and_passes_the_literal_instance(self):
        unit = (HERE / "systemd" / "warn@.service").read_text()
        body = "\n".join(l for l in unit.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("OnFailure", body)
        # OnFailure=warn@%n.service already carries the literal unit name, so %i (not the unescaping %I,
        # which turns every dash into a slash) is the unit.
        self.assertIn("warn-unit-failed %i", unit)
        self.assertIn("Type=oneshot", unit)


if __name__ == "__main__":
    unittest.main()
