import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class RuntimeDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_survive_outside_the_executable_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = textwrap.dedent(
                """
                import sys
                from pathlib import Path
                from runtime_diagnostics import enable_runtime_diagnostics, write_runtime_diagnostic

                path = enable_runtime_diagnostics(
                    "ui", directory=Path(sys.argv[1]), client_build="test-build"
                )
                try:
                    raise RuntimeError("role switch failed")
                except RuntimeError:
                    write_runtime_diagnostic("message_dispatch_failed", exception=sys.exc_info())
                print(path)
                """
            )
            subprocess.check_call([sys.executable, "-c", script, temporary])
            content = (Path(temporary) / "dps_fault_ui.log").read_text(
                encoding="utf-8"
            )

        self.assertIn("process_started component=ui build=test-build", content)
        self.assertIn("message_dispatch_failed", content)
        self.assertIn("RuntimeError: role switch failed", content)


if __name__ == "__main__":
    unittest.main()
