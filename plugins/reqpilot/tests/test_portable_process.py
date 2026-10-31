"""Timeout cleanup keeps the fixed task child isolated on both supported OSes."""
from pathlib import Path
import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import portable_process


class PortableProcessTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX process-group exercise")
    def test_real_child_process_stops_after_timeout_signal(self):
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   **portable_process.isolation_kwargs())
        try:
            portable_process.stop_timed_out_process(process, force=False)
            self.assertIsNotNone(process.wait(timeout=5))
        finally:
            portable_process.stop_timed_out_process(process, force=True)

    def test_isolation_selects_native_process_group(self):
        self.assertEqual(portable_process.isolation_kwargs(windows=False),
                         {"start_new_session": True})
        self.assertEqual(portable_process.isolation_kwargs(windows=True),
                         {"creationflags": 0x00000200})

    def test_posix_timeout_signals_process_group(self):
        process = Mock(pid=1234)
        process.poll.return_value = None
        with patch.object(portable_process.os, "killpg") as kill:
            portable_process.stop_timed_out_process(process, force=False, windows=False)
            portable_process.stop_timed_out_process(process, force=False, interrupt=True, windows=False)
            portable_process.stop_timed_out_process(process, force=True, windows=False)
        self.assertEqual(kill.call_args_list[0].args, (1234, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (1234, signal.SIGINT))
        self.assertEqual(kill.call_args_list[2].args, (1234, signal.SIGKILL))

    def test_windows_timeout_escalates_to_child_tree_kill(self):
        process = Mock(pid=1234)
        process.poll.return_value = None
        with patch.object(portable_process.subprocess, "run") as run:
            portable_process.stop_timed_out_process(process, force=False, windows=True)
            process.send_signal.assert_called_once()
            portable_process.stop_timed_out_process(process, force=True, windows=True)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "1234", "/T", "/F"])
        process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
