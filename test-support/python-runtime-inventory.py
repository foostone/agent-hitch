import importlib.util
import marshal
from pathlib import Path
import py_compile
import sys
import sysconfig
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "integrations/harbor/hitch_python_runtime.py"
spec = importlib.util.spec_from_file_location("runtime_inventory", MODULE)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
sysconfig.get_config_vars()


class RuntimeInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.launcher = self.root / "harbor"
        self.launcher.write_text("#!/python\n# fixed launcher, fixed version\n")
        self.source = self.root / "dependency.py"
        self.source.write_text("VALUE = 1\n")
        self.original_path = sys.path
        sys.path = [str(self.root)]
        self.addCleanup(setattr, sys, "path", self.original_path)
        original_cache_prefix = sys.pycache_prefix
        sys.pycache_prefix = None
        self.addCleanup(setattr, sys, "pycache_prefix", original_cache_prefix)

    def observe(self):
        return runtime.inventory(self.launcher)

    def test_same_version_package_bytes_change_identity(self):
        first = self.observe()
        self.assertEqual(first, self.observe())
        self.source.write_text("VALUE = 2\n")
        self.assertNotEqual(first["integrity"], self.observe()["integrity"])
        self.assertEqual(first["python_version"], self.observe()["python_version"])

    def test_generated_caches_preserve_identity(self):
        first = self.observe()
        for optimize in (0, 1, 2):
            py_compile.compile(str(self.source), doraise=True, optimize=optimize)
            self.assertEqual(first, self.observe())

    def test_bytecode_which_disagrees_with_source_is_rejected(self):
        cache = Path(py_compile.compile(str(self.source), doraise=True))
        header = cache.read_bytes()[:16]
        cache.write_bytes(header + marshal.dumps(compile("VALUE = 99\n", str(self.source), "exec")))
        with self.assertRaisesRegex(ValueError, "disagrees"):
            self.observe()

    def test_added_and_removed_files_change_identity(self):
        first = self.observe()
        added = self.root / "new_module.py"
        added.write_text("VALUE = 1\n")
        self.assertNotEqual(first["integrity"], self.observe()["integrity"])
        added.unlink()
        self.assertEqual(first, self.observe())

    def test_symlink_dependency_content_changes_identity(self):
        with tempfile.TemporaryDirectory() as outside:
            file = Path(outside) / "module.py"
            file.write_text("VALUE = 1\n")
            (self.root / "linked.py").symlink_to(file)
            first = self.observe()
            file.write_text("VALUE = 2\n")
            self.assertNotEqual(first["integrity"], self.observe()["integrity"])

    def test_relative_module_paths_are_rejected(self):
        sys.path.append("relative")
        with self.assertRaisesRegex(ValueError, "relative"):
            self.observe()

    def test_external_cache_directory_is_rejected(self):
        sys.pycache_prefix = str(self.root / "outside-module-search-path")
        with self.assertRaisesRegex(ValueError, "external Python cache"):
            self.observe()

    def test_special_files_and_resource_limits_fail_closed(self):
        import os

        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.observe()
        fifo.unlink()
        original = runtime.MAX_FILES
        try:
            runtime.MAX_FILES = 1
            with self.assertRaisesRegex(ValueError, "limit"):
                self.observe()
        finally:
            runtime.MAX_FILES = original


if __name__ == "__main__":
    unittest.main()
