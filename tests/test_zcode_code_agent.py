from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from autodesign.agents import zcode_code_agent
from autodesign.agents.zcode_code_agent import _selected_model


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "autodesign" / "agents" / "zcode_code_agent.py"


class ZCodeCodeAgentTest(unittest.TestCase):
    def test_selects_requested_model_without_prompt_slash_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"model": "company-openai/gpt-5.4", "provider": {}}),
                encoding="utf-8",
            )
            prompt_path = root / "designer_author_prompt.md"
            prompt_path.write_text("Write poster.html.", encoding="utf-8")
            fake_zcode = root / "fake_zcode.py"
            fake_zcode.write_text(
                "\n".join(
                    [
                        f"#!{sys.executable}",
                        "import json, pathlib, sys",
                        "args = sys.argv[1:]",
                        "cwd = pathlib.Path(args[args.index('--cwd') + 1])",
                        f"config = json.loads(pathlib.Path({str(config_path)!r}).read_text(encoding='utf-8'))",
                        "(cwd / 'observed.json').write_text(json.dumps({'args': args, 'model': config['model']}), encoding='utf-8')",
                        "(cwd / 'poster.html').write_text('<!doctype html><html></html>', encoding='utf-8')",
                        "(cwd / 'designer_author_done.json').write_text('{}', encoding='utf-8')",
                    ]
                ),
                encoding="utf-8",
            )
            fake_zcode.chmod(0o755)

            result = subprocess.run(
                [
                    sys.executable,
                    str(WRAPPER),
                    "--zcode-bin",
                    str(fake_zcode),
                    "--model",
                    "company-openai/claude-opus-4-8",
                    "--config-path",
                    str(config_path),
                    "--prompt-file",
                    prompt_path.name,
                    "--target-file",
                    "poster.html",
                    "--done-file",
                    "designer_author_done.json",
                ],
                cwd=root,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            observed = json.loads((root / "observed.json").read_text(encoding="utf-8"))
            self.assertEqual(observed["model"], "company-openai/claude-opus-4-8")
            invocation_prompt = observed["args"][-1]
            self.assertNotIn("/model", invocation_prompt)
            for internal_term in (
                "plan mode",
                "enterplanmode",
                "exitplanmode",
                "subagent",
                "explore",
                "delegate",
            ):
                self.assertNotIn(internal_term, invocation_prompt.lower())
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8"))["model"],
                "company-openai/gpt-5.4",
            )

    def test_recovers_stale_model_selection_before_next_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"model": "stale-selected"}),
                encoding="utf-8",
            )
            recovery_path = root / ".config.json.autodesign-recovery.json"
            recovery_path.write_text(
                json.dumps({
                    "version": 1,
                    "previous_model": "original-model",
                    "selected_model": "stale-selected",
                }),
                encoding="utf-8",
            )
            with _selected_model(config_path, "new-model"):
                self.assertEqual(
                    json.loads(config_path.read_text(encoding="utf-8"))["model"],
                    "new-model",
                )
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8"))["model"],
                "original-model",
            )
            self.assertFalse(recovery_path.exists())

    def test_model_less_invocation_recovers_stale_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"model": "stale-selected"}),
                encoding="utf-8",
            )
            recovery_path = root / ".config.json.autodesign-recovery.json"
            recovery_path.write_text(
                json.dumps({
                    "version": 1,
                    "previous_model": "original-model",
                    "selected_model": "stale-selected",
                }),
                encoding="utf-8",
            )

            with _selected_model(config_path, ""):
                self.assertEqual(
                    json.loads(config_path.read_text(encoding="utf-8"))["model"],
                    "original-model",
                )

            self.assertFalse(recovery_path.exists())

    def test_model_less_invocation_without_recovery_needs_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_config = Path(temp_dir) / "missing-config.json"
            with _selected_model(missing_config, ""):
                pass
            self.assertFalse(missing_config.exists())

    def test_model_less_invocation_waits_for_temporary_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"model": "original-model"}),
                encoding="utf-8",
            )
            prompt_path = root / "prompt.md"
            prompt_path.write_text("Create files.", encoding="utf-8")
            observed_path = root / "observed-model.txt"
            fake_zcode = root / "fake_zcode.py"
            fake_zcode.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib\n"
                f"config = pathlib.Path({str(config_path)!r})\n"
                f"observed = pathlib.Path({str(observed_path)!r})\n"
                "observed.write_text(json.loads(config.read_text())['model'])\n",
                encoding="utf-8",
            )
            fake_zcode.chmod(0o755)

            with _selected_model(config_path, "temporary-model"):
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(WRAPPER),
                        "--zcode-bin",
                        str(fake_zcode),
                        "--config-path",
                        str(config_path),
                        "--prompt-file",
                        prompt_path.name,
                    ],
                    cwd=root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.3)
                self.assertIsNone(proc.poll())
                self.assertFalse(observed_path.exists())

            _stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertEqual(
                observed_path.read_text(encoding="utf-8"),
                "original-model",
            )

    def test_model_selection_preserves_config_symlink_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target_path = root / "real-config.json"
            target_path.write_text(
                json.dumps({"model": "original-model"}),
                encoding="utf-8",
            )
            target_path.chmod(0o600)
            config_path = root / "config.json"
            config_path.symlink_to(target_path)

            with _selected_model(config_path, "temporary-model"):
                self.assertEqual(
                    json.loads(target_path.read_text(encoding="utf-8"))["model"],
                    "temporary-model",
                )
                self.assertTrue(config_path.is_symlink())
                self.assertEqual(stat.S_IMODE(target_path.stat().st_mode), 0o600)

            self.assertTrue(config_path.is_symlink())
            self.assertEqual(stat.S_IMODE(target_path.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(target_path.read_text(encoding="utf-8"))["model"],
                "original-model",
            )

    def test_termination_signals_restore_selected_model(self) -> None:
        for termination_signal in (signal.SIGTERM, signal.SIGINT):
            with (
                self.subTest(signal=termination_signal),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                config_path = root / "config.json"
                config_path.write_text(
                    json.dumps({"model": "original-model"}),
                    encoding="utf-8",
                )
                prompt_path = root / "prompt.md"
                prompt_path.write_text("Create the required files.", encoding="utf-8")
                fake_zcode = root / "fake_zcode.py"
                fake_zcode.write_text(
                    "#!/usr/bin/env python3\n"
                    "import os, pathlib, time\n"
                    "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                fake_zcode.chmod(0o755)
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(WRAPPER),
                        "--zcode-bin",
                        str(fake_zcode),
                        "--model",
                        "temporary-model",
                        "--config-path",
                        str(config_path),
                        "--prompt-file",
                        prompt_path.name,
                    ],
                    cwd=root,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                deadline = time.time() + 5
                while time.time() < deadline:
                    current = json.loads(config_path.read_text(encoding="utf-8"))
                    if (
                        current.get("model") == "temporary-model"
                        and (root / "child.pid").is_file()
                    ):
                        break
                    time.sleep(0.05)
                else:
                    proc.kill()
                    self.fail("wrapper did not select the requested model")

                child_pid = int((root / "child.pid").read_text(encoding="utf-8"))
                os.killpg(proc.pid, termination_signal)
                _stdout, stderr = proc.communicate(timeout=5)

                self.assertEqual(
                    proc.returncode,
                    128 + termination_signal,
                    msg=stderr,
                )
                self.assertEqual(
                    json.loads(config_path.read_text(encoding="utf-8"))["model"],
                    "original-model",
                )
                self.assertFalse(
                    (root / ".config.json.autodesign-recovery.json").exists()
                )
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

    def test_termination_during_model_setup_restores_immediately(self) -> None:
        for termination_signal in (signal.SIGTERM, signal.SIGINT):
            for interruption_stage in ("recovery", "config"):
                with (
                    self.subTest(
                        signal=termination_signal,
                        stage=interruption_stage,
                    ),
                    tempfile.TemporaryDirectory() as temp_dir,
                ):
                    self._assert_model_setup_interruption_restores(
                        Path(temp_dir),
                        termination_signal,
                        interruption_stage,
                    )

    def _assert_model_setup_interruption_restores(
        self,
        root: Path,
        termination_signal: signal.Signals,
        interruption_stage: str,
    ) -> None:
        config_path = root / "config.json"
        config_path.write_text(
            json.dumps({"model": "original-model"}),
            encoding="utf-8",
        )
        original_write_config = zcode_code_agent._write_config
        recovery_path = root / ".config.json.autodesign-recovery.json"
        interrupted = False

        def interrupt_after_model_write(
            path: Path,
            payload: dict[str, object],
        ) -> None:
            nonlocal interrupted
            original_write_config(path, payload)
            should_interrupt = (
                interruption_stage == "recovery" and path == recovery_path
            ) or (
                interruption_stage == "config"
                and path == config_path
                and payload.get("model") == "temporary-model"
            )
            if not interrupted and should_interrupt:
                interrupted = True
                os.kill(os.getpid(), termination_signal)

        previous_handler = signal.getsignal(termination_signal)
        signal.signal(
            termination_signal,
            zcode_code_agent._raise_for_termination,
        )
        try:
            with (
                mock.patch.object(
                    zcode_code_agent,
                    "_write_config",
                    side_effect=interrupt_after_model_write,
                ),
                self.assertRaises(SystemExit) as raised,
            ):
                with _selected_model(config_path, "temporary-model"):
                    self.fail("signal should interrupt context setup")
        finally:
            signal.signal(termination_signal, previous_handler)

        self.assertEqual(raised.exception.code, 128 + termination_signal)
        self.assertEqual(
            json.loads(config_path.read_text(encoding="utf-8"))["model"],
            "original-model",
        )
        self.assertFalse(recovery_path.exists())

    def test_termination_during_popen_setup_reaps_child(self) -> None:
        for termination_signal in (signal.SIGTERM, signal.SIGINT):
            with (
                self.subTest(signal=termination_signal),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                config_path = root / "config.json"
                config_path.write_text(
                    json.dumps({"model": "original-model"}),
                    encoding="utf-8",
                )
                prompt_path = root / "prompt.md"
                prompt_path.write_text("Create files.", encoding="utf-8")
                child_script = root / "child.py"
                child_script.write_text(
                    "import time\ntime.sleep(30)\n",
                    encoding="utf-8",
                )
                original_popen = subprocess.Popen
                child_pid: int | None = None

                def interrupt_before_popen_returns(
                    *_args: object,
                    **_kwargs: object,
                ) -> subprocess.Popen[bytes]:
                    nonlocal child_pid
                    child = original_popen(
                        [sys.executable, str(child_script)],
                        start_new_session=True,
                    )
                    child_pid = child.pid
                    os.kill(os.getpid(), termination_signal)
                    return child

                with (
                    mock.patch.object(
                        zcode_code_agent.subprocess,
                        "Popen",
                        side_effect=interrupt_before_popen_returns,
                    ),
                    self.assertRaises(SystemExit) as raised,
                ):
                    zcode_code_agent.main([
                        "--zcode-bin",
                        str(child_script),
                        "--model",
                        "temporary-model",
                        "--config-path",
                        str(config_path),
                        "--prompt-file",
                        prompt_path.name,
                    ])

                self.assertEqual(raised.exception.code, 128 + termination_signal)
                self.assertIsNotNone(child_pid)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(child_pid), 0)
                self.assertEqual(
                    json.loads(config_path.read_text(encoding="utf-8"))["model"],
                    "original-model",
                )
                self.assertFalse(
                    (root / ".config.json.autodesign-recovery.json").exists()
                )

    def test_repeated_termination_signal_does_not_interrupt_child_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({"model": "original-model"}),
                encoding="utf-8",
            )
            prompt_path = root / "prompt.md"
            prompt_path.write_text("Create files.", encoding="utf-8")
            cleanup_called = False

            class FakeProcess:
                pid = 999999

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    os.kill(os.getpid(), signal.SIGTERM)
                    return 0

            def send_second_signal(_proc: FakeProcess) -> None:
                nonlocal cleanup_called
                os.kill(os.getpid(), signal.SIGINT)
                cleanup_called = True

            with (
                mock.patch.object(
                    zcode_code_agent.subprocess,
                    "Popen",
                    return_value=FakeProcess(),
                ),
                mock.patch.object(
                    zcode_code_agent,
                    "_terminate_process_group",
                    side_effect=send_second_signal,
                ),
                self.assertRaises(SystemExit) as raised,
            ):
                zcode_code_agent.main([
                    "--model",
                    "temporary-model",
                    "--config-path",
                    str(config_path),
                    "--prompt-file",
                    prompt_path.name,
                ])

            self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
            self.assertTrue(cleanup_called)
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8"))["model"],
                "original-model",
            )

    def test_cleanup_kills_descendant_after_session_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_script = root / "child.py"
            child_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            leader_script = root / "leader.py"
            child_pid_path = root / "child.pid"
            leader_script.write_text(
                "import pathlib, subprocess, sys\n"
                f"child = subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
                f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))\n",
                encoding="utf-8",
            )
            leader = subprocess.Popen(
                [sys.executable, str(leader_script)],
                start_new_session=True,
            )
            leader.wait(timeout=5)
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            try:
                zcode_code_agent._terminate_process_group(leader)
                deadline = time.time() + 2
                while time.time() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("descendant remained alive after process-group cleanup")
            finally:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
