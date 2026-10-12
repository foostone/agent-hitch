"""Supervise one Docker command across loss of its Harbor parent.

The parent owns stdin's write end and sends one byte only after both output
pipes close. EOF without that handshake means cancellation or parent death.
Inherited cache locks remain held here until the command group is terminated.
This file deliberately depends only on the Python standard library.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time


def group_has_live_members(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    if sys.platform != "linux":
        return True
    # An orphan zombie can remain until init reaps it, but cannot hold files
    # or execute. A live D-state helper, however, can outlast SIGKILL.
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    with open(f"{entry.path}/stat") as stream:
                        fields = stream.read().rpartition(")")[2].split()
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                    return True
    except (OSError, ValueError, IndexError):
        # Failed observation cannot authorize release of build ownership.
        return True
    return False


async def stop_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.02)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()
    while group_has_live_members(process.pid):
        await asyncio.sleep(0.05)


async def supervise(command):
    loop = asyncio.get_running_loop()
    interrupted = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, interrupted.set)
    reader = asyncio.StreamReader()
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    process = None
    pending = []
    try:
        # The liveness pipe and inherited locks belong to this supervisor only;
        # Docker and its plugins receive neither, including through stdin.
        process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
        os.close(sys.stdout.fileno())
        os.close(sys.stderr.fileno())
        exited = asyncio.create_task(process.wait())
        control = asyncio.create_task(reader.read(1))
        stopped = asyncio.create_task(interrupted.wait())
        pending = [exited, control, stopped]
        acknowledged = False
        while True:
            watched = [control, stopped] if exited.done() and not acknowledged else [exited, control, stopped]
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if stopped in done:
                return 125
            if control in done:
                if control.result() != b"1" or acknowledged:
                    return 125
                acknowledged = True
                control = asyncio.create_task(reader.read(1))
                pending.append(control)
            if acknowledged and exited.done():
                code = exited.result()
                return code if code >= 0 else 128 - code
    finally:
        # A successful leader and closed output pipes do not prove that a
        # plugin's silent helper stopped. Reclaim the group on every exit.
        if process is not None:
            await stop_group(process)
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        transport.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(supervise(sys.argv[1:])))
