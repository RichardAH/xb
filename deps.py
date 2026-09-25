"""
External dependency resolver for xb.

Resolves conan-managed dependencies into include paths and library paths
that the compiler and linker can use.  Also handles the protobuf/gRPC
code generation step.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("xb.deps")


def _pkg_config_libs(pkg_name: str) -> list[str]:
    """Use pkg-config to get library names for a package."""
    try:
        import re
        result = subprocess.run(
            ["pkg-config", "--libs", pkg_name],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            libs = re.findall(r"-l(\S+)", result.stdout)
            return sorted(set(libs))
    except Exception:
        pass
    return []


# Known conan package layouts
# Each entry: (name_prefix_to_match, {include_dir, lib_dir, [lib_pattern, ...]})
_PKG_LAYOUTS = [
    ("boost", {
        "include": "include",
        "lib": "lib",
        "libs": [
            "boost_date_time", "boost_program_options", "boost_thread",
            "boost_container", "boost_json", "boost_regex",
            "boost_system", "boost_filesystem", "boost_iostreams",
            "boost_coroutine", "boost_timer", "boost_atomic",
            "boost_chrono", "boost_context", "boost_log",
            "boost_random", "boost_type_erasure", "boost_wave",
        ],
    }),
    ("open", {  # matches openssl (truncated to 'opens...')
        "include": "include",
        "lib": "lib",
        "libs": ["ssl", "crypto"],
    }),
    ("rock", {  # matches rocksdb (truncated to 'rocks...')
        "include": "include",
        "lib": "lib",
        "libs": ["rocksdb"],
    }),
    ("nudb", {
        "include": "include",
        "lib": "lib",
        "libs": [],  # header-only
    }),
    ("soci", {
        "include": "include",
        "lib": "lib",
        "libs": ["soci_core", "soci_sqlite3"],
    }),
    # grpc is linked from system libraries (not built by conan in this setup)
    # The conan grpc package only has source code, no built libs
    ("proto", {  # matches protobuf
        "include": "include",
        "lib": "lib",
        "libs": ["protobuf"],
    }),
    ("libar", {  # matches libarchive
        "include": "include",
        "lib": "lib",
        "libs": ["archive"],
    }),
    ("lz4", {
        "include": "include",
        "lib": "lib",
        "libs": ["lz4"],
    }),
    ("zlib", {
        "include": "include",
        "lib": "lib",
        "libs": ["z"],
    }),
    ("xxhas", {
        "include": "include",
        "lib": "lib",
        "libs": ["xxhash"],
    }),
    ("snappy", {
        "include": "include",
        "lib": "lib",
        "libs": ["snappy"],
    }),
    ("date", {
        "include": "include",
        "lib": "lib",
        "libs": ["date-tz"],
    }),
    ("magic", {
        "include": "include",
        "lib": "lib",
        "libs": [],  # header-only
    }),
    ("sqlite", {
        "include": "include",
        "lib": "lib",
        "libs": ["sqlite3"],
    }),
    ("wasme", {  # matches wasmedge (conan truncates to wasme...)
        "include": "include",
        "lib": "lib",
        "libs": ["wasmedge"],
    }),
]


class ExternalDeps:
    """
    Resolves external dependencies from conan packages.

    Populates include paths and library paths from the conan cache.
    Also handles protobuf/gRPC code generation.
    """

    def __init__(self, conan_cache: str | None = None, conan_build: str | None = None):
        self.include_dirs: list[str] = []
        self.lib_dirs: list[str] = []
        self.libs: list[str] = []
        self._resolved: bool = False
        self._conan_cache = conan_cache or os.path.expanduser("~/.conan2/p")
        self._conan_build = conan_build

    def resolve(self) -> bool:
        """
        Scan the conan cache and build directory for packages.
        Returns True if at least boost and openssl were found.
        """
        if self._resolved:
            return True

        log.info("Resolving external dependencies...")

        # Check conan cache
        if os.path.isdir(self._conan_cache):
            self._scan_conan_cache()

        # Check conan build directory for external deps
        if self._conan_build and os.path.isdir(self._conan_build):
            self._scan_external(self._conan_build)

        # Add system libraries that aren't in conan cache
        self._scan_system_libs()

        self._resolved = True
        log.info(f"Found {len(self.include_dirs)} include dirs, "
                 f"{len(self.lib_dirs)} lib dirs, "
                 f"{len(self.libs)} libraries")
        return True

    def _scan_conan_cache(self):
        """Scan ~/.conan2/p for packages by matching directory name prefixes."""
        cache = Path(self._conan_cache)
        if not cache.exists():
            return

        for entry in sorted(cache.iterdir()):
            if not entry.is_dir():
                continue

            name = entry.name
            # Try to match against known package prefixes
            for prefix, layout in _PKG_LAYOUTS:
                if name.lower().startswith(prefix):
                    self._add_package(prefix, str(entry / "p"), layout)
                    break  # Only match first hit per directory

    def _scan_external(self, build_dir: str):
        """Scan release-build/external for bundled deps (secp256k1, ed25519, etc)."""
        external = Path(build_dir) / "external"
        if not external.exists():
            return

        # secp256k1
        secp = external / "secp256k1" / "lib"
        if secp.exists():
            self.lib_dirs.append(str(secp))
            inc = external / "secp256k1"
            if (inc / "secp256k1.h").exists():
                self.include_dirs.append(str(inc))
            self.libs.append("secp256k1")

        # ed25519
        ed = external / "ed25519-donna"
        ed_lib = ed / "libed25519.a"
        if ed_lib.exists():
            self.lib_dirs.append(str(ed))
            self.libs.append("ed25519")
            # Also check for a lib/ subdirectory
            if (ed / "lib").exists():
                self.lib_dirs.append(str(ed / "lib"))

        # wasmedge
        we = external / "WasmEdge" / "lib"
        if we.exists():
            self.lib_dirs.append(str(we))
            we_inc = external / "WasmEdge" / "include"
            if we_inc.exists():
                self.include_dirs.append(str(we_inc))
            self.libs.append("wasmedge")

    def _scan_system_libs(self):
        """Add system libraries that aren't managed by conan."""
        system_lib_dir = "/usr/lib/x86_64-linux-gnu"
        if not os.path.isdir(system_lib_dir):
            return

        # SOCI (header-only in conan, needs system lib)
        if os.path.isfile(os.path.join(system_lib_dir, "libsoci_core.a")):
            if system_lib_dir not in self.lib_dirs:
                self.lib_dirs.append(system_lib_dir)
            for l in ["soci_core", "soci_sqlite3"]:
                if l not in self.libs:
                    self.libs.append(l)
        
        # gRPC system library (use pkg-config to get full dependency list)
        if os.path.isfile(os.path.join(system_lib_dir, "libgrpc++.so")):
            grpc_libs = _pkg_config_libs("grpc++")
            for l in grpc_libs:
                if l not in self.libs:
                    self.libs.append(l)
        
        # Other system libs
        for l in ["sqlite3", "jsoncpp", "pthread", "dl", "rt", "wasmedge"]:
            if l not in self.libs:
                self.libs.append(l)

    def _add_package(self, prefix: str, base_dir: str, layout: dict):
        """Add a known package to the dependency list."""
        inc = os.path.join(base_dir, layout["include"])
        lib = os.path.join(base_dir, layout["lib"])

        if os.path.isdir(inc) and inc not in self.include_dirs:
            self.include_dirs.append(inc)
        if os.path.isdir(lib) and lib not in self.lib_dirs:
            self.lib_dirs.append(lib)
            # Verify libs actually exist
            for libname in layout["libs"]:
                # Check for both .a and .so
                for pattern in [f"lib{libname}.a", f"lib{libname}.so"]:
                    if os.path.isfile(os.path.join(lib, pattern)):
                        if libname not in self.libs:
                            self.libs.append(libname)
                        break

    @property
    def include_flags(self) -> list[str]:
        """-I flags for all include directories."""
        flags = []
        for d in self.include_dirs:
            flags.extend(["-I", d])
        return flags

    @property
    def lib_flags(self) -> list[str]:
        """-L and -l flags for all libraries."""
        flags = []
        for d in self.lib_dirs:
            flags.extend(["-L", d])
        for lib in self.libs:
            flags.extend(["-l", lib])
        return flags

    @property
    def rpath_flags(self) -> list[str]:
        """-Wl,-rpath flags for runtime library search."""
        flags = []
        for d in self.lib_dirs:
            flags.extend(["-Wl,-rpath,", d])
        return flags


def find_protoc() -> str | None:
    """Find the protoc binary for protobuf code generation."""
    candidates = ["protoc"]
    for c in candidates:
        if shutil.which(c):
            return c
    # Try conan cache
    conan_p = os.path.expanduser("~/.conan2/p")
    if os.path.isdir(conan_p):
        for entry in os.listdir(conan_p):
            if "proto" in entry.lower():
                protoc = os.path.join(conan_p, entry, "p", "bin", "protoc")
                if os.path.isfile(protoc):
                    return protoc
    return None


def generate_protobuf(src_root: str, build_dir: str) -> bool:
    """
    Generate protobuf and gRPC source files.

    Generated files are placed under the include path so they can be
    included with #include <xrpl/proto/ripple.pb.h>.
    """
    protoc = find_protoc()
    if not protoc:
        log.warning("protoc not found – skipping protobuf code gen")
        return True

    proto_dir = os.path.join(src_root, "include", "xrpl", "proto")
    if not os.path.isdir(proto_dir):
        log.info(f"No proto directory found: {proto_dir}")
        return True

    # Find all .proto files
    proto_files = []
    for root, dirs, files in os.walk(proto_dir):
        for f in files:
            if f.endswith(".proto"):
                proto_files.append(os.path.join(root, f))

    if not proto_files:
        log.info("No proto files found")
        return True

    log.info(f"Generating code for {len(proto_files)} proto files")

    for proto in proto_files:
        # Generate C++ sources directly into the include tree
        cmd = [
            protoc,
            f"--proto_path={proto_dir}",
            f"--cpp_out={proto_dir}",
            proto,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            if result.returncode != 0:
                log.error(f"protoc failed for {proto}: {result.stderr.decode()[:200]}")
                return False
        except subprocess.TimeoutExpired:
            log.error(f"protoc timed out for {proto}")
            return False

    # Also generate gRPC stubs
    grpc_plugin = shutil.which("grpc_cpp_plugin")
    if grpc_plugin:
        for proto in proto_files:
            cmd = [
                protoc,
                f"--proto_path={proto_dir}",
                f"--grpc_out={proto_dir}",
                f"--plugin=protoc-gen-grpc={grpc_plugin}",
                proto,
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=30)
                if result.returncode != 0:
                    log.error(f"grpc_cpp_plugin failed for {proto}: {result.stderr.decode()[:200]}")
            except subprocess.TimeoutExpired:
                log.error(f"grpc_cpp_plugin timed out for {proto}")

    log.info(f"Generated proto files in {proto_dir}")
    return True


def find_proto_sources(src_root: str) -> list[str]:
    """Find all generated protobuf .cc and .grpc.pb.cc files."""
    proto_dir = os.path.join(src_root, "include", "xrpl", "proto")
    if not os.path.isdir(proto_dir):
        return []

    result = []
    for root, dirs, files in os.walk(proto_dir):
        for f in files:
            if f.endswith(".pb.cc") or f.endswith(".grpc.pb.cc"):
                result.append(os.path.join(root, f))
    return result
