"""
Content-addressed build cache.

Object files are stored in the cache directory keyed by their SHA-256 hash
of the (source, command line, headers) fingerprint.  This allows:
  • Reusing objects across different build directories
  • Skipping compilation when nothing has actually changed
  • Sharing cache between developers
"""

import hashlib
import os
import shutil
from pathlib import Path


class BuildCache:
    """Content-addressed build cache."""

    def __init__(self, cache_dir: str, max_size_gb: float = 10.0):
        self.cache_dir = os.path.abspath(cache_dir)
        self.max_size = max_size_gb * 1024 * 1024 * 1024
        os.makedirs(self.cache_dir, exist_ok=True)

    def _hash_dir(self, fp: str) -> str:
        """Map fingerprint to cache subdirectory (like Ccache)."""
        return os.path.join(self.cache_dir, fp[:2], fp[2:4])

    def _obj_path(self, fp: str) -> str:
        """Full cache path for an object with this fingerprint."""
        return os.path.join(self._hash_dir(fp), "object.o")

    def _fp_path(self, fp: str) -> str:
        """Fingerprint metadata path."""
        return os.path.join(self._hash_dir(fp), "fingerprint.fp")

    def has(self, fp: str) -> bool:
        """Check if a compiled object with this fingerprint exists in cache."""
        return os.path.isfile(self._obj_path(fp))

    def get(self, fp: str, dest: str) -> bool:
        """
        Copy cached object to destination.
        Returns True if the object was found and copied.
        """
        obj = self._obj_path(fp)
        if not os.path.isfile(obj):
            return False
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copy2(obj, dest)
        return True

    def put(self, fp: str, obj_path: str) -> bool:
        """
        Store an object file in the cache.
        Returns True if stored, False if already present.
        """
        cached = self._obj_path(fp)
        if os.path.isfile(cached):
            return False
        cache_dir = self._hash_dir(fp)
        os.makedirs(cache_dir, exist_ok=True)
        shutil.copy2(obj_path, cached)
        # Store fingerprint for verification
        with open(self._fp_path(fp), "w") as f:
            f.write(fp + "\n")
        return True

    def stats(self) -> dict:
        """Return cache statistics."""
        total_files = 0
        total_size = 0
        for root, dirs, files in os.walk(self.cache_dir):
            for f in files:
                if f.endswith(".o"):
                    total_files += 1
                    try:
                        total_size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
        return {
            "objects": total_files,
            "size_bytes": total_size,
            "size_mb": total_size / 1024 / 1024,
            "max_size_mb": self.max_size / 1024 / 1024,
        }
