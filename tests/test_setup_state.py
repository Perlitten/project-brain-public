"""State writes must preserve evidence from concurrent API/MCP processes."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.onboarding import setup_state


class SetupStateTests(unittest.TestCase):
    def test_non_object_json_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"BRAIN_SETUP_STATE_DIR": directory}):
                for value in ([], None, "bad", 42):
                    setup_state.state_path().write_text(json.dumps(value))
                    self.assertEqual(setup_state.load_state(), {})
                    setup_state.record_self_check("ready", "fp", {})
                    self.assertEqual(setup_state.load_state()["mcp_self_check"]["status"], "ready")

    def test_failed_replace_preserves_state_and_cleans_tempfile(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"BRAIN_SETUP_STATE_DIR": directory}):
                setup_state.record_self_check("ready", "old", {})
                with patch.object(setup_state.os, "replace", side_effect=OSError("write failed")):
                    with self.assertRaises(OSError):
                        setup_state.record_self_check("failed", "new", {})
                self.assertEqual(setup_state.load_state()["mcp_self_check"]["fingerprint"], "old")
                self.assertEqual(list(Path(directory).glob(".setup-state-*")), [])

    def test_concurrent_process_updates_are_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "BRAIN_SETUP_STATE_DIR": directory,
                           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            script = (
                "import sys,time; from brain.onboarding import setup_state; "
                "slot=sys.argv[1]; "
                "\n"
                "for i in range(8):\n"
                " def mutate(state):\n"
                "  time.sleep(0.01)\n"
                "  state.setdefault('writers', {})[slot] = i\n"
                " setup_state._save(mutate)\n"
            )
            processes = [subprocess.Popen([sys.executable, "-c", script, str(i)],
                                          env=environment, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE) for i in range(8)]
            for process in processes:
                _, error = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, error.decode())
            state = json.loads((Path(directory) / "setup_state.json").read_text())
            self.assertEqual(state["writers"], {str(i): 7 for i in range(8)})
            if os.name != "nt":
                self.assertEqual((Path(directory) / "setup_state.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
