"""Real process regressions for cancellation, parent death and build locks."""
import asyncio
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BRIDGE = Path(__file__).resolve().parents[1] / "integrations/harbor"
sys.path.insert(0, str(BRIDGE))
import hitch_image_cache as cache_module
real_docker = cache_module.docker


class BuildProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    async def test_output_backpressure_errors_and_closed_output_timeout(self):
        executable = self.root / "docker"
        executable.write_text(
            f"#!{sys.executable}\nimport os, sys, time\n"
            "if sys.argv[1] == 'output':\n"
            "    sys.stdout.write('x' * 262144); sys.stderr.write('y' * 262144)\n"
            "elif sys.argv[1] == 'error':\n"
            "    sys.stderr.write('private build details'); sys.exit(7)\n"
            "else:\n"
            "    os.close(1); os.close(2); time.sleep(60)\n"
        )
        executable.chmod(0o700)
        env = {**os.environ, "PATH": str(self.root)}
        self.assertEqual(await real_docker(["output"], env=env), "x" * 262144)
        with self.assertRaisesRegex(RuntimeError, r"^Hitch image preparation Docker error failed \(7\)$"):
            await real_docker(["error"], env=env)
        self.assertIsNone(await real_docker(["error"], env=env, check=False))
        with self.assertRaises(asyncio.TimeoutError):
            await real_docker(["closed"], env=env, timeout=0.3)

    async def test_parent_death_keeps_locks_until_stubborn_build_stops(self):
        fifo = self.root / "alive"
        os.mkfifo(fifo)
        alive = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, alive)
        marker = self.root / "child"
        executable = self.root / "docker"
        executable.write_text(
            f"#!{sys.executable}\nimport os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"alive = os.open({str(fifo)!r}, os.O_WRONLY)\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "os.write(alive, b'1')\ntime.sleep(60)\n"
        )
        executable.chmod(0o700)
        paths = [self.root / "key.lock", self.root / "slot.lock"]
        code = """
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hitch_image_cache import docker, lock
async def main():
    async with lock([Path(sys.argv[2])]):
        async with lock([Path(sys.argv[3])]):
            await docker(['buildx', 'build'], env=dict(os.environ), timeout=None)
asyncio.run(main())
"""
        parent = subprocess.Popen([sys.executable, "-B", "-c", code, str(BRIDGE), *map(str, paths)],
                                  env={**os.environ, "PATH": str(self.root)}, start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        if os.read(alive, 1) == b"1":
                            break
                    except BlockingIOError:
                        pass
                    await asyncio.sleep(0.01)
            os.killpg(parent.pid, signal.SIGKILL)
            await asyncio.to_thread(parent.wait, 5)
            async with asyncio.timeout(8):
                while True:
                    try:
                        if os.read(alive, 1) == b"":
                            break
                    except BlockingIOError:
                        pass
                    for path in paths:
                        with path.open("a") as handle:
                            try:
                                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                continue
                            # Recheck after acquiring: cleanup may have closed
                            # the writer between the first read and flock.
                            try:
                                self.assertEqual(os.read(alive, 1), b"")
                            except BlockingIOError:
                                self.fail("live build lost its parent-owned lock")
                    await asyncio.sleep(0.01)
                for path in paths:
                    while True:
                        with path.open("a") as handle:
                            try:
                                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                break
                            except BlockingIOError:
                                pass
                        await asyncio.sleep(0.01)
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                await asyncio.to_thread(parent.wait, 5)
            if marker.exists() and marker.read_text():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    async def test_successful_leader_cannot_release_locks_with_silent_work_alive(self):
        fifo = self.root / "alive"
        os.mkfifo(fifo)
        alive = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, alive)
        marker = self.root / "child"
        executable = self.root / "docker"
        executable.write_text(
            f"#!{sys.executable}\nimport os, signal, time\n"
            "if os.fork(): os._exit(0)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"alive = os.open({str(fifo)!r}, os.O_WRONLY)\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "os.write(alive, b'1')\nos.close(1); os.close(2)\ntime.sleep(60)\n"
        )
        executable.chmod(0o700)
        key = self.root / "key.lock"
        async def build():
            async with cache_module.lock([key]):
                return await real_docker(["buildx", "build"], env={**os.environ, "PATH": str(self.root)})
        task = asyncio.create_task(build())
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        if os.read(alive, 1) == b"1":
                            break
                    except BlockingIOError:
                        pass
                    await asyncio.sleep(0.01)
            self.assertEqual(await asyncio.wait_for(task, 8), "")
            # EOF on a separate lifetime FD proves the helper stopped; output
            # EOF alone did not, and would let the next caller take this lock.
            self.assertEqual(os.read(alive, 1), b"")
            with key.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if marker.exists() and marker.read_text():
                try:
                    os.kill(int(marker.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancellation_during_spawn_retains_ownership(self):
        executable = self.root / "docker"
        executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        executable.chmod(0o700)
        started, release = asyncio.Event(), asyncio.Event()
        spawn = asyncio.create_subprocess_exec
        processes = []

        async def delayed_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            started.set()
            await release.wait()
            return process

        with patch.object(cache_module.asyncio, "create_subprocess_exec", delayed_spawn):
            task = asyncio.create_task(real_docker(["info"], env={**os.environ, "PATH": str(self.root)}))
            try:
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 8)
                self.assertIsNotNone(processes[0].returncode)
                self.assertTrue(processes[0].stdout.at_eof())
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancelling_docker_waits_for_process_exit(self):
        executable = self.root / "docker"; marker = self.root / "pid"
        executable.write_text(f'#!{sys.executable}\nimport os,time\nopen({str(marker)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
        executable.chmod(0o700)
        task = asyncio.create_task(real_docker(["buildx", "build"], env={**os.environ, "PATH": str(self.root)}))
        async with asyncio.timeout(5):
            while not marker.exists() or not marker.read_text(): await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    async def test_repeated_cancellation_waits_for_stubborn_child(self):
        executable = self.root / "docker"; marker = self.root / "pid"
        executable.write_text(f'#!{sys.executable}\nimport os,time,signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen({str(marker)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
        executable.chmod(0o700)
        task = asyncio.create_task(real_docker(["buildx", "build"], env={**os.environ, "PATH": str(self.root)}))
        async with asyncio.timeout(5):
            while not marker.exists() or not marker.read_text(): await asyncio.sleep(0.01)
        pid = int(marker.read_text())
        task.cancel(); await asyncio.sleep(0.05); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, 8)
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    async def test_cancellation_stops_descendant_after_docker_cli_exits(self):
        executable = self.root / "docker"
        marker = self.root / "child"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import os, signal, time\n"
            "leader = os.getpid()\n"
            "if os.fork(): os._exit(0)\n"
            "while os.getppid() == leader: time.sleep(0.01)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        executable.chmod(0o700)
        processes = []
        spawn = asyncio.create_subprocess_exec

        async def observed_spawn(*args, **kwargs):
            process = await spawn(*args, **kwargs)
            processes.append(process)
            return process

        with patch.object(cache_module.asyncio, "create_subprocess_exec", observed_spawn):
            task = asyncio.create_task(real_docker(["buildx", "build"], env={**os.environ, "PATH": str(self.root)}))
            pid = None
            try:
                async with asyncio.timeout(5):
                    while not marker.exists() or not marker.read_text() or not processes:
                        await asyncio.sleep(0.01)
                pid = int(marker.read_text())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 8)
                # The orphan can briefly remain a zombie until init reaps it.
                # A closed inherited pipe proves it cannot continue the build.
                self.assertTrue(processes[0].stdout.at_eof())
                self.assertTrue(processes[0].stderr.at_eof())
            finally:
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__": unittest.main(verbosity=2)
