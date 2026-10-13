"""Read-only content inventory for the interpreter which launches Harbor.

Invoked with the actual launcher interpreter, before starting a task. Generated
bytecode caches do not change identity; a cache that can disagree with its source
is rejected instead of being silently omitted. No imported package is trusted to
report its own version or integrity.
"""

import hashlib
import importlib.util
import json
import marshal
import os
from pathlib import Path
import re
import stat
import sys
import sysconfig
import types


MAX_FILES = 250_000
MAX_BYTES = 2 * 1024**3


def inventory(launcher):
    launcher = Path(launcher).absolute()
    if not launcher.is_file():
        raise ValueError("missing launcher")
    if sys.pycache_prefix is not None:
        raise ValueError("external Python cache directories are unsupported")
    # A -c probe starts with cwd; a script starts with its containing directory.
    # Never accept relative PYTHONPATH entries: execution uses a different cwd.
    search_path = list(sys.path) if getattr(sys.flags, "safe_path", False) else [str(launcher.resolve(strict=True).parent), *sys.path[1:]]
    if any(not os.path.isabs(value) for value in search_path):
        raise ValueError("relative Python module path")
    records = {}
    snapshots = {}
    directories = {}
    active = set()
    byte_count = 0

    def read(file):
        nonlocal byte_count
        before = file.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("non-regular Python runtime file")
        if byte_count + before.st_size > MAX_BYTES:
            raise ValueError("Python runtime inventory exceeds byte limit")
        data = file.read_bytes()
        byte_count += len(data)
        after = file.stat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
        if signature(before) != signature(after):
            raise ValueError("Python runtime changed while reading")
        snapshots[str(file)] = signature(after)
        return data

    def cache_is_derived(file):
        if file.suffix != ".pyc" or file.parent.name != "__pycache__":
            return False
        tag = re.escape(sys.implementation.cache_tag)
        match = re.fullmatch(r"(.+)\." + tag + r"(?:\.opt-([012]))?\.pyc", file.name)
        if not match:
            # Another interpreter's tagged cache cannot be imported here.
            return True
        source = file.parent.parent / (match[1] + ".py")
        if not source.is_file():
            # CPython does not import orphan files inside __pycache__.
            return True
        data = read(file)
        if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
            raise ValueError("unverifiable Python bytecode cache")
        code = marshal.loads(data[16:])
        if not isinstance(code, types.CodeType):
            raise ValueError("invalid Python bytecode cache")
        expected = compile(read(source), code.co_filename, "exec", dont_inherit=True, optimize=int(match[2] or 0))
        if code != expected:
            raise ValueError("Python bytecode cache disagrees with source")
        return True

    def visit(file):
        name = str(file)
        if name in records:
            return
        if len(records) >= MAX_FILES:
            raise ValueError("Python runtime inventory exceeds file limit")
        if file.is_symlink():
            target = file.resolve(strict=True)
            records[name] = ["link", os.readlink(file), str(target)]
            visit(target)
        elif file.is_dir():
            if name in active:
                raise ValueError("cyclic Python runtime directory")
            active.add(name)
            names = sorted(os.listdir(file))
            directories[name] = names
            # Cache directories and verified caches are derived, not inputs.
            if file.name != "__pycache__":
                records[name] = ["directory"]
            for child in names:
                visit(file / child)
            active.remove(name)
        elif file.is_file():
            if cache_is_derived(file):
                return
            data = read(file)
            records[name] = ["file", stat.S_IMODE(file.stat().st_mode), hashlib.sha256(data).hexdigest()]
        elif file.exists():
            raise ValueError("unsupported Python runtime file")
        else:
            records[name] = ["missing"]

    for file in [launcher, Path(sys.executable), Path(sys.prefix) / "pyvenv.cfg"]:
        visit(file)
    for value in search_path:
        visit(Path(value))
    # Startup hooks (for example Homebrew sitecustomize) may have been imported
    # from a directory later removed from sys.path. Include their actual files.
    for module in list(sys.modules.values()):
        origin = getattr(module, "__file__", None)
        if isinstance(origin, str):
            file = Path(origin)
            if not file.is_absolute():
                file = file.absolute()
            visit(file)
    library = Path(sysconfig.get_config_var("LIBDIR") or sys.base_prefix) / (sysconfig.get_config_var("LDLIBRARY") or "libpython")
    visit(library)
    for name, expected in snapshots.items():
        s = Path(name).stat()
        if (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode) != expected:
            raise ValueError("Python runtime changed during inventory")
    for name, expected in directories.items():
        if sorted(os.listdir(name)) != expected:
            raise ValueError("Python runtime directory changed during inventory")
    identity = {
        "schema_version": "1",
        "implementation": sys.implementation.name,
        "version": sys.version,
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "search_path": search_path,
        "files": sorted(records.items()),
    }
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return {
        "schema_version": "1",
        "integrity": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "file_count": sum(value[0] == "file" for value in records.values()),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(inventory(sys.argv[1]), separators=(",", ":")))
    except Exception:
        # Paths or package import errors can contain credentials or private data.
        print("Harbor Python runtime cannot be verified", file=sys.stderr)
        sys.exit(1)
