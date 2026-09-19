import os
import unittest
import tempfile
import shutil
from pathlib import Path

def safe_open_path(docs_root: Path, target_path: Path, mode: str = 'w'):
    """
    The proposed safe_open logic.
    """
    real_docs_root = docs_root.resolve()
    
    # 1. Check if target is a symlink
    if target_path.is_symlink():
        raise OSError(f"Security violation: {target_path} is a symlink")

    # 2. Check if any ancestor is a symlink
    for parent in target_path.parents:
        if parent.is_symlink():
             raise OSError(f"Security violation: Ancestor {parent} is a symlink")

    # 3. Resolve real path to check containment
    real_target = target_path.resolve()
    if not str(real_target).startswith(str(real_docs_root)):
        raise OSError(f"Security violation: Resolved path {real_target} escapes {real_docs_root}")

    return open(target_path, mode)

class TestSymlinkSafety(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())
        self.docs_dir = self.test_dir / "docs"
        self.docs_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_normal_write(self):
        log_file = self.docs_dir / "logs" / "test.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with safe_open_path(self.docs_dir, log_file, 'w') as f:
            f.write("test")
        self.assertEqual(log_file.read_text(), "test")

    def test_symlink_attack(self):
        # Create a symlink to a file outside docs
        external_file = self.test_dir / "evil.txt"
        external_file.write_text("secret")
        
        # Make 'logs' a symlink to the test_dir
        log_dir = self.docs_dir / "logs"
        log_dir.symlink_to(self.test_dir)
        
        target_file = log_dir / "evil.log"
        
        with self.assertRaises(OSEError) as cm:
            with safe_open_path(self.docs_dir, target_file, 'w') as f:
                f.write("attack")
        self.assertIn("is a symlink", str(cm.exception).lower())

    def test_escape_attack(self):
        # Create a path that resolves outside docs
        tmp_outside = Path(tempfile.gettempdir()) / "outside_file.txt"
        tmp_outside.write_text("outside")
        
        # Create a symlink inside docs pointing outside
        fake_path = self.docs_dir / "leak"
        fake_path.symlink_to(tmp_outside)
        
        with self.assertRaises(OSError) as cm:
            with safe_open_path(self.docs_dir, fake_path, 'w') as f:
                f.write("leak")
        self.assertIn("escapes", str(cm.exception).lower())

if __name__ == "__main__":
    unittest.main()
