"""
Compiler toolchain configuration.

Produces the compiler command lines for each compilation unit.
Handles CFLAGS, CXXFLAGS, defines, includes, and per-module overrides.
"""

import os
import subprocess
import shutil
import platform
from pathlib import Path


class Toolchain:
    """Compiler toolchain with common flags."""

    def __init__(
        self,
        cc: str = "g++",
        cxx_std: str = "c++20",
        build_type: str = "Release",
        src_root: str = ".",
        build_root: str = "xb-build",
        include_roots: list[str] | None = None,
    ):
        self.cc = cc
        self._real_cc = cc  # compiler before ccache wrapper
        self.cxx_std = f"-std={cxx_std}"
        self.build_type = build_type
        self.src_root = os.path.abspath(src_root)
        self.build_root = os.path.abspath(build_root)
        self.include_roots = include_roots or []

        # Derived paths
        self.include_dir = os.path.join(self.src_root, "include")
        self.src_include = os.path.join(self.src_root, "src")

        # Common flags
        self._cflags: list[str] = []
        self._cxxflags: list[str] = []
        self._ldflags: list[str] = []
        self._defines: dict[str, str | None] = {}
        self._libs: list[str] = []

        self._setup_defaults()

    def _setup_defaults(self):
        """Set up default flags based on build type."""
        # Wrap compiler with ccache for instant warm builds
        self._try_enable_ccache()

        # Position independent code (needed for static libs that may be
        # included in shared libraries)
        self._cflags.append("-fPIC")

        # Common includes
        self.include_roots = [self.include_dir, self.src_include] + self.include_roots

        # Compiler detection
        self._is_gcc = self._detect_gcc()

        if self.build_type == "Release":
            self._cflags.extend(["-O2", "-DNDEBUG", "-g"])
        elif self.build_type == "Debug":
            self._cflags.extend(["-O0", "-g3", "-DDEBUG", "-D_DEBUG"])
        elif self.build_type == "RelWithDebInfo":
            self._cflags.extend(["-O2", "-g", "-DNDEBUG"])

        # Warning flags (matching CMake config)
        self._cflags.extend([
            "-Wall",
            "-Wdeprecated",
            "-Wno-sign-compare",
            "-Wno-char-subroots",
            "-Wno-format",
            "-Wno-unused-local-typedefs",
            "-Wno-maybe-uninitialized",
            "-fno-strict-aliasing",
        ])

        # C++ specific
        self._cxxflags.extend([
            "-frtti",
            "-Wnon-virtual-dtor",
            "-Wsuggest-override",
        ])

        # Common defines (matching CMake)
        self._defines.update({
            "BOOST_ASIO_DISABLE_HANDLER_TYPE_REQUIREMENTS": None,
            "BOOST_ASIO_USE_TS_EXECUTOR_AS_DEFAULT": None,
            "BOOST_CONTAINER_FWD_BAD_DEQUE": None,
            "HAS_UNCAUGHT_EXCEPTIONS": "1",
            "BOOST_COROUTINES_NO_DEPRECATION_WARNING": None,
            "BOOST_BEAST_ALLOW_DEPRECATED": None,
            "BOOST_FILESYSTEM_DEPRECATED": None,
            "SOCI_HAVE_CXX11": None,
            "OPENSSL_SUPPRESS_DEPRECATED": None,
        })

        # System libs
        self._libs.extend(["pthread", "dl", "rt", "m"])

        # Linker flags
        self._ldflags.extend([
            "-rdynamic",
            "-Wl,-z,relro,-z,now,--build-id",
            "-static-libstdc++",
            "-static-libgcc",
        ])

    def _try_enable_ccache(self):
        """Wrap compiler with ccache if available."""
        ccache_path = shutil.which("ccache")
        if ccache_path:
            self.cc = ccache_path
            os.environ.setdefault("CCACHE_COMPILERTYPE", "gcc")
            os.environ.setdefault("CCACHE_COMPRESS", "1")
            os.environ.setdefault("CCACHE_COMPRESSLEVEL", "6")
            print(f"ccache enabled: {ccache_path}")

    def _try_fast_linker(self):
        """Use the fastest available linker (lld > gold > default)."""
        for linker in ["lld", "gold"]:
            cmd = f"ld.{linker}"
            try:
                subprocess.check_output(
                    [cmd, "--version"],
                    stderr=subprocess.STDOUT,
                )
                self._ldflags.insert(0, f"-fuse-ld={linker}")
                print(f"Using fast linker: {linker}")
                return
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue

    def _detect_gcc(self) -> bool:
        """Detect if the compiler is GCC."""
        try:
            out = subprocess.check_output(
                [self._real_cc, "--version"],
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            return "gcc" in out.lower()
        except Exception:
            return True  # default to GCC-like

    @property
    def cflags(self) -> list[str]:
        return list(self._cflags)

    @property
    def cxxflags(self) -> list[str]:
        return list(self._cxxflags)

    @property
    def ldflags(self) -> list[str]:
        return list(self._ldflags)

    @property
    def libs(self) -> list[str]:
        return list(self._libs)

    def add_cflag(self, flag: str):
        self._cflags.append(flag)

    def add_define(self, name: str, value: str | None = None):
        self._defines[name] = value

    @property
    def define_flags(self) -> list[str]:
        """Convert defines dict to -D flags."""
        flags = []
        for name, value in self._defines.items():
            if value is None:
                flags.append(f"-D{name}")
            else:
                flags.append(f"-D{name}={value}")
        return flags

    @property
    def include_flags(self) -> list[str]:
        """Convert include_roots to -I flags."""
        flags = []
        for d in self.include_roots:
            if os.path.isdir(d):
                flags.extend(["-I", d])
        return flags

    def compile_cmd(self, src: str, obj: str, extra_cflags: list[str] = None) -> list[str]:
        """
        Build the full compiler command line for a single source file.
        """
        cmd = [self.cc]
        # If using ccache, insert the real compiler after it
        if self.cc != self._real_cc:
            cmd.append(self._real_cc)
        cmd.append(self.cxx_std)
        cmd.extend(self.cflags)
        cmd.extend(self.cxxflags)
        cmd.extend(self.define_flags)
        cmd.extend(self.include_flags)
        cmd.extend(extra_cflags or [])
        cmd.extend(["-c", src, "-o", obj])
        return cmd

    def link_cmd(self, objs_or_archives: list[str], output: str,
                 extra_ldflags: list[str] = None, extra_libs: list[str] = None) -> list[str]:
        """
        Build the full linker command line.
        """
        # Use real compiler for linking (ccache not needed for linking)
        cmd = [self._real_cc]
        cmd.extend(self.cflags)
        cmd.extend(extra_ldflags or [])
        cmd.extend(objs_or_archives)
        cmd.extend(self.ldflags)
        for lib in self.libs + (extra_libs or []):
            cmd.extend(["-l", lib])
        cmd.extend(["-o", output])
        return cmd

    def obj_path(self, src: str, mod_name: str) -> str:
        """
        Compute the output .o path for a source file.
        Layout: build_root/mod_name/dirname/basename.o
        """
        rel = os.path.relpath(src, self.src_root)
        # Replace include/ or src/ prefix with build path
        obj_dir = os.path.join(self.build_root, mod_name, os.path.dirname(rel))
        base = os.path.splitext(os.path.basename(rel))[0]
        return os.path.join(obj_dir, base + ".o")

    def archive_path(self, mod_name: str) -> str:
        """Compute the output .a path for a module."""
        return os.path.join(self.build_root, f"lib{mod_name.replace('.', '_')}.a")
