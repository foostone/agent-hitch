"""Creation-time fenced OS process operations; no training dependencies."""
def process_identity(pid):
    import psutil

    try:
        process = psutil.Process(pid)
        return {"pid": pid, "createdAt": process.create_time()}
    except psutil.NoSuchProcess:
        return None


def owned_alive(identity):
    import psutil

    try:
        p = psutil.Process(identity["pid"])
        return p.create_time() == identity["createdAt"] and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def stop_owned(identities):
    import psutil

    processes = []
    for identity in identities:
        try:
            process = psutil.Process(identity["pid"])
            # Check creation time on the same psutil object used for signaling.
            # Constructing a second object after owned_alive could adopt a reused
            # PID; psutil's own signal methods guard subsequent reuse of this one.
            if (
                process.create_time() == identity["createdAt"]
                and process.status() != psutil.STATUS_ZOMBIE
            ):
                processes.append(process)
        except psutil.NoSuchProcess:
            pass
    for p in reversed(processes):
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(processes, timeout=5)
    for p in remaining:
        if any(i["pid"] == p.pid and owned_alive(i) for i in identities):
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
    psutil.wait_procs(remaining, timeout=5)
