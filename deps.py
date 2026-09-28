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
    ("open", {
        "include": "include",
        "lib": "lib",
        "libs": ["ssl", "crypto"],
    }),
    ("rock", {
        "include": "include",
        "lib": "lib",
        "libs": ["rocksdb"],
    }),
    ("nudb", {
        "include": "include",
        "lib": "lib",
        "libs": [],
    }),
    ("soci", {
        "include": "include",
        "lib": "lib",
        "libs": ["soci_core", "soci_sqlite3"],
    }),
    ("proto", {
        "include": "include",
        "lib": "lib",
        "libs": ["protobuf"],
    }),
    ("libar", {
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
    ("snapp", {
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
        "libs": [],
    }),
    ("sqlite", {
        "include": "include",
        "lib": "lib",
        "libs": ["sqlite3"],
    }),
    ("wasme", {
        "include": "include",
        "lib": "lib",
        "libs": ["wasmedge"],
    }),
]


def _get_package_prefix(path: str) -> str | None:
    """Return the package layout prefix for a path, or None if unknown."""
    path_lower = path.lower()
    for layout_prefix, _ in _PKG_LAYOUTS:
        if layout_prefix in path_lower:
            return layout_prefix
    return None


def _dedup_paths(paths: list[str]) -> list[str]:
    """
    Deduplicate paths by package prefix, keeping one per prefix.
    
    Prefer installed paths (p/PKG/) over built paths (p/b/PKG/).
    Paths not matching any known prefix are always kept.
    """
    # Track one path per prefix, preserving first-seen order
    kept: dict[str, str] = {}
    order: list[str] = []
    non_conan: list[str] = []

    for p in paths:
        prefix = _get_package_prefix(p)
        if prefix is None:
            # Not a known conan package - always keep
            if p not in kept:
                non_conan.append(p)
                kept[p] = p
            continue

        if prefix not in kept:
            kept[prefix] = p
            order.append(prefix)
        else:
            existing = kept[prefix]
            # Prefer installed (no /b/) over built (has /b/)
            if "/b/" in existing and "/b/" not in p:
                kept[prefix] = p
            # else: keep existing (either already installed or both /b/)

    # Rebuild in order: prefixes first, then non-conan
    result = [kept[p] for p in order if p in kept and kept[p] != p]
    # Add non-conan paths
    result.extend(non_conan)
    return result


class ExternalDeps:
    """
    Resolves external dependencies from conan packages.
    """

    def __init__(self, conan_cache: str | None = None, conan_build: str | None = None, src_root: str | None = None):
        self.include_dirs: list[str] = []
        self.lib_dirs: list[str] = []
        self.libs: list[str] = []
        self._resolved: bool = False
        self._conan_cache = conan_cache or os.path.expanduser("~/.conan2/p")
        self._conan_build = conan_build
        self._src_root = src_root

    def resolve(self) -> bool:
        """Scan the conan cache and build directory for packages."""
        if self._resolved:
            return True

        log.info("Resolving external dependencies...")

        if os.path.isdir(self._conan_cache):
            self._scan_conan_cache()

        if self._conan_build and os.path.isdir(self._conan_build):
            self._scan_external(self._conan_build)

        # Also scan source tree's external/ directory
        if self._src_root:
            self._scan_external(self._src_root)

        self._scan_system_libs()

        # Deduplicate paths by package prefix
        self.include_dirs = _dedup_paths(self.include_dirs)
        self.lib_dirs = _dedup_paths(self.lib_dirs)

        self._resolved = True
        log.info(f"Found {len(self.include_dirs)} include dirs, "
                 f"{len(self.lib_dirs)} lib dirs, "
                 f"{len(self.libs)} libraries")
        return True

    def _scan_conan_cache(self):
        cache = Path(self._conan_cache)
        if not cache.exists():
            return

        dirs_to_scan = sorted(cache.iterdir())
        b_dir = cache / "b"
        if b_dir.is_dir():
            dirs_to_scan.extend(sorted(b_dir.iterdir()))

        for entry in dirs_to_scan:
            if not entry.is_dir():
                continue
            name = entry.name
            for prefix, layout in _PKG_LAYOUTS:
                if name.lower().startswith(prefix):
                    self._add_package(prefix, str(entry / "p"), layout)
                    break

    def _scan_external(self, build_dir: str):
        """Scan external/ directory for secp256k1, ed25519, WasmEdge."""
        external = Path(build_dir) / "external"
        if not external.exists():
            return

        # secp256k1
        secp_lib = external / "secp256k1" / "lib"
        secp_inc = external / "secp256k1" / "include"
        if secp_inc.exists():
            self.include_dirs.append(str(secp_inc))
        if secp_lib.exists():
            self.lib_dirs.append(str(secp_lib))
            if "secp256k1" not in self.libs:
                self.libs.append("secp256k1")

        # ed25519-donna
        ed = external / "ed25519-donna"
        if ed.exists():
            ed_inc = ed / "include"
            # ed25519 headers are in the directory itself, not in include/ subdir
            if not ed_inc.exists():
                ed_inc = ed
            if ed_inc.exists():
                self.include_dirs.append(str(ed_inc))
            ed_lib = ed / "libed25519.a"
            if ed_lib.exists():
                self.lib_dirs.append(str(ed))
                if "ed25519" not in self.libs:
                    self.libs.append("ed25519")
            if (ed / "lib").exists():
                self.lib_dirs.append(str(ed / "lib"))

        # WasmEdge
        we = external / "WasmEdge" / "lib"
        if we.exists():
            self.lib_dirs.append(str(we))
        we_inc = external / "WasmEdge" / "include"
        if we_inc.exists():
            self.include_dirs.append(str(we_inc))
        if "wasmedge" not in self.libs:
            self.libs.append("wasmedge")


    def _scan_system_libs(self):
        system_lib_dir = "/usr/lib/x86_64-linux-gnu"
        if not os.path.isdir(system_lib_dir):
            return

        if os.path.isfile(os.path.join(system_lib_dir, "libsoci_core.a")):
            if system_lib_dir not in self.lib_dirs:
                self.lib_dirs.append(system_lib_dir)
            for l in ["soci_core", "soci_sqlite3"]:
                if l not in self.libs:
                    self.libs.append(l)

        if os.path.isfile(os.path.join(system_lib_dir, "libgrpc++.so")):
            grpc_libs = _pkg_config_libs("grpc++")
            for l in grpc_libs:
                if l not in self.libs:
                    self.libs.append(l)

        for l in ["sqlite3", "pthread", "dl", "rt", "wasmedge"]:
            if l not in self.libs:
                self.libs.append(l)

    def _add_package(self, prefix: str, base_dir: str, layout: dict):
        inc = os.path.join(base_dir, layout["include"])
        lib = os.path.join(base_dir, layout["lib"])

        if os.path.isdir(inc) and inc not in self.include_dirs:
            self.include_dirs.append(inc)
        if os.path.isdir(lib) and lib not in self.lib_dirs:
            self.lib_dirs.append(lib)
            for libname in layout["libs"]:
                for pattern in [f"lib{libname}.a", f"lib{libname}.so"]:
                    if os.path.isfile(os.path.join(lib, pattern)):
                        if libname not in self.libs:
                            self.libs.append(libname)
                        break

    @property
    def include_flags(self) -> list[str]:
        flags = []
        for d in self.include_dirs:
            flags.extend(["-I", d])
        return flags

    @property
    def lib_flags(self) -> list[str]:
        flags = []
        for d in self.lib_dirs:
            flags.extend(["-L", d])
        for lib in self.libs:
            flags.extend(["-l", lib])
        return flags

    @property
    def rpath_flags(self) -> list[str]:
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
    conan_p = os.path.expanduser("~/.conan2/p")
    if os.path.isdir(conan_p):
        for entry in os.listdir(conan_p):
            if "proto" in entry.lower():
                protoc = os.path.join(conan_p, entry, "p", "bin", "protoc")
                if os.path.isfile(protoc):
                    return protoc
    return None


def find_grpc_plugin() -> str | None:
    """Find the grpc_cpp_plugin binary."""
    plugin = shutil.which("grpc_cpp_plugin")
    if plugin:
        return plugin
    common_paths = [
        "/usr/lib/grpc/grpc_cpp_plugin",
        "/usr/lib/x86_64-linux-gnu/grpc/grpc_cpp_plugin",
        "/usr/local/bin/grpc_cpp_plugin",
        "/usr/local/lib/grpc/grpc_cpp_plugin",
    ]
    for p in common_paths:
        if os.path.isfile(p):
            return p
    conan_p = os.path.expanduser("~/.conan2/p")
    if os.path.isdir(conan_p):
        for root, dirs, files in os.walk(conan_p):
            for f in files:
                if f == "grpc_cpp_plugin":
                    full = os.path.join(root, f)
                    if os.access(full, os.X_OK):
                        return full
    return None


# Proto files known to have gRPC service definitions that need .grpc.pb.h/.grpc.pb.cc
_KNOWN_GRPC_PROTOS = [
    "xrp_ledger", "ledger", "get_ledger",
    "get_ledger_data", "get_ledger_diff", "get_ledger_entry",
]


def _all_proto_files_exist(proto_dir: str) -> bool:
    """Check if all .proto files have corresponding .pb.h, .pb.cc, AND .grpc.pb files."""
    # Check basic pb files for all protos
    for root, dirs, files in os.walk(proto_dir):
        for f in files:
            if f.endswith(".proto"):
                base = f[:-6]
                pb_h = os.path.join(root, base + ".pb.h")
                pb_cc = os.path.join(root, base + ".pb.cc")
                if not os.path.isfile(pb_h) or not os.path.isfile(pb_cc):
                    return False

    # Check grpc files for known grpc protos
    for proto_name in _KNOWN_GRPC_PROTOS:
        grpc_h = os.path.join(proto_dir, "org", "xrpl", "rpc", "v1",
                            f"{proto_name}.grpc.pb.h")
        grpc_cc = os.path.join(proto_dir, "org", "xrpl", "rpc", "v1",
                             f"{proto_name}.grpc.pb.cc")
        if not os.path.isfile(grpc_h) or not os.path.isfile(grpc_cc):
            return False

    return True


def _copy_proto_files(src_dir: str, dst_dir: str) -> bool:
    """Copy .pb.h, .pb.cc, .grpc.pb.h, .grpc.pb.cc files from src to dst."""
    proto_exts = (".pb.h", ".pb.cc", ".grpc.pb.h", ".grpc.pb.cc")
    copied = 0
    for root, dirs, files in os.walk(src_dir):
        for f in files:
            if any(f.endswith(ext) for ext in proto_exts):
                src_path = os.path.join(root, f)
                rel = os.path.relpath(src_path, src_dir)
                dst_path = os.path.join(dst_dir, rel)
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                if not os.path.isfile(dst_path):
                    try:
                        shutil.copy2(src_path, dst_path)
                        copied += 1
                    except Exception as e:
                        log.error(f"Failed to copy {src_path}: {e}")
    if copied:
        log.debug(f"Copied {copied} proto files")
    return copied > 0


def ensure_proto_files(src_root: str) -> bool:
    """
    Ensure all required .pb.h, .pb.cc, .grpc.pb.h, .grpc.pb.cc files exist.
    
    Strategy:
    1. Check if files already exist (cached from previous build)
    2. If not, try to copy from pre-generated location
    3. If still not complete, try to generate with protoc
    
    Returns True if all proto files are present, False otherwise.
    """
    proto_dir = os.path.join(src_root, "include", "xrpl", "proto")
    if not os.path.isdir(proto_dir):
        log.info(f"No proto directory found: {proto_dir}")
        return True
    
    # If everything is already here, we're done
    if _all_proto_files_exist(proto_dir):
        log.debug("Proto files already present, skipping generation")
        return True
    
    # Try to copy from pre-generated location
    pregen_dirs = [
        "/usr/local/lib/xb/proto-generated",
        "/opt/xb/proto-generated",
    ]
    for pregen_dir in pregen_dirs:
        if os.path.isdir(pregen_dir):
            _copy_proto_files(pregen_dir, proto_dir)
            if _all_proto_files_exist(proto_dir):
                log.info(f"All proto files available (from {pregen_dir})")
                return True
    
    # Try to generate basic proto files with protoc
    protoc = find_protoc()
    if protoc:
        proto_files = []
        for root, dirs, files in os.walk(proto_dir):
            for f in files:
                if f.endswith(".proto"):
                    proto_files.append(os.path.join(root, f))
        
        if proto_files:
            log.info(f"Generating code for {len(proto_files)} proto files")
            for proto in proto_files:
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
                except subprocess.TimeoutExpired:
                    log.error(f"protoc timed out for {proto}")
            
            # Try gRPC generation
            grpc_plugin = find_grpc_plugin()
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
                            log.error(f"grpc_cpp_plugin failed for {proto}")
                    except subprocess.TimeoutExpired:
                        log.error(f"grpc_cpp_plugin timed out for {proto}")
            else:
                log.warning("grpc_cpp_plugin not found - gRPC stubs will not be generated")
            
            if _all_proto_files_exist(proto_dir):
                log.info("Proto generation successful")
                return True
    
    # Final check
    if not _all_proto_files_exist(proto_dir):
        missing = []
        for proto_name in _KNOWN_GRPC_PROTOS:
            for ext in [".grpc.pb.h", ".grpc.pb.cc"]:
                fpath = os.path.join(proto_dir, "org", "xrpl", "rpc", "v1",
                                   f"{proto_name}{ext}")
                if not os.path.isfile(fpath):
                    missing.append(fpath)
        if missing:
            log.warning(f"Proto files still missing: {missing[:5]}...")
            return False
    
    return True


def generate_protobuf(src_root: str, build_dir: str) -> bool:
    """
    Generate protobuf and gRPC source files.
    Now a wrapper around ensure_proto_files for backward compatibility.
    """
    return ensure_proto_files(src_root)


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
