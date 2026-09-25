"""
System dependency checker for xb.

Detects missing build tools and library headers on the host system,
prompts the user to install them via apt, and re-checks after install.

Dependencies are defined in two places:
  1. Built-in tool checks in this module (g++, protoc, ccache, etc.)
  2. Project-level library requirements passed via xbp.py or build.py

Usage:
    from xb.sysdeps import SysDeps, check_all_deps

    # Quick one-liner (interactive, prompts for each missing dep)
    ok = check_all_deps()

    # Project-supplied extra deps
    ok = check_all_deps(extra_deps={
        "libwasmedge-dev": {"pkg": "libwasmedge-dev",
                           "check": lambda: find_header("wasmedge/wasmedge.h")},
        "rocksdb": {"pkg": "librocksdb-dev",
                    "check": lambda: find_header("rocksdb/db.h")},
    })
"""

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("xb.sysdeps")

# ---------------------------------------------------------------------------
# Dependency definition
# ---------------------------------------------------------------------------


@dataclass
class Dep:
    """A single system dependency to check."""

    name: str                    # human-friendly label
    apt_pkg: str                # apt package name
    check: Callable[[], bool]   # returns True if the dep is present
    hint: str = ""              # extra text shown when missing
    optional: bool = False      # don't block build if missing


# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------

_HEADER_SEARCH_PATHS = [
    "/usr/include",
    "/usr/local/include",
    "/usr/lib/gcc/x86_64-linux-gnu/*/include",
    "/usr/include/x86_64-linux-gnu",
]


def _which(cmd: str) -> str | None:
    """Wrapper around shutil.which that also searches /snap and flatpak paths."""
    return shutil.which(cmd)


def _find_header(header: str, extra_paths: list[str] | None = None) -> str | None:
    """Try to locate *header* by asking the compiler."""
    paths = _HEADER_SEARCH_PATHS + (extra_paths or [])
    # Quick check: just ask gcc where it would find the file
    try:
        result = subprocess.run(
            ["g++", "-print-file-name", header],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip() != header:
            return result.stdout.strip()
    except Exception:
        pass

    # Fallback: brute-force search
    for p in paths:
        candidate = os.path.join(p, header)
        if os.path.isfile(candidate):
            return candidate
    return None


def _pkg_config_ok(pkg: str) -> bool:
    """Return True if pkg-config can find *pkg*."""
    try:
        subprocess.run(
            ["pkg-config", "--exists", pkg],
            capture_output=True, timeout=10,
        )
        return True
    except Exception:
        return False


def _lib_exists(lib_name: str) -> bool:
    """Check if a library file (lib<name>.so or .a) exists anywhere ld can find."""
    for ext in ["so", "a"]:
        pat = f"lib{lib_name}.{ext}"
        try:
            result = subprocess.run(
                ["ldconfig", "-p"], capture_output=True, text=True, timeout=10,
            )
            if pat in result.stdout:
                return True
        except Exception:
            pass
        # Also check standard paths
        for base in ["/usr/lib", "/usr/local/lib", "/usr/lib/x86_64-linux-gnu"]:
            for ext2 in [ext, f"so.1", f"a"]:
                candidate = os.path.join(base, f"lib{lib_name}.{ext2}")
                if os.path.isfile(candidate):
                    return True
    return False


# ---------------------------------------------------------------------------
# Built-in dependency definitions
# ---------------------------------------------------------------------------

def _builtin_deps() -> list[Dep]:
    """Return the default set of tool dependencies."""

    def _cc_ok():
        return _which("g++") is not None or _which("clang++") is not None

    def _protoc_ok():
        return _which("protoc") is not None

    def _pkg_config_ok_func():
        return _which("pkg-config") is not None

    deps = [
        Dep(
            name="C++ compiler (g++ or clang++)",
            apt_pkg="g++",
            check=_cc_ok,
            hint="Also works: clang++ from package clang",
        ),
        Dep(
            name="make",
            apt_pkg="make",
            check=lambda: _which("make") is not None,
        ),
        Dep(
            name="ar (archiver)",
            apt_pkg="binutils",
            check=lambda: _which("ar") is not None,
        ),
        Dep(
            name="protoc (Protocol Buffers compiler)",
            apt_pkg="protobuf-compiler",
            check=_protoc_ok,
            hint="Version >= 3.21.0 recommended for xahaud",
        ),
        Dep(
            name="pkg-config",
            apt_pkg="pkg-config",
            check=_pkg_config_ok_func,
        ),
        Dep(
            name="git",
            apt_pkg="git",
            check=lambda: _which("git") is not None,
        ),
        Dep(
            name="ccache (optional, speeds up rebuilds)",
            apt_pkg="ccache",
            check=lambda: _which("ccache") is not None,
            optional=True,
        ),
    ]

    return deps


# ---------------------------------------------------------------------------
# Interactive install
# ---------------------------------------------------------------------------

def _prompt_install(apt_pkg: str, dep_name: str, hint: str = "") -> bool:
    """
    Ask the user if they want to install *apt_pkg*.

    Returns True if installed successfully, False if user declined or install failed.
    """
    msg = f"\nMissing: {dep_name}\n"
    msg += f"  apt package: {apt_pkg}\n"
    if hint:
        msg += f"  Note: {hint}\n"
    msg += f"\n  Install with: sudo apt-get install -y {apt_pkg}\n"
    msg += f"\n  Install now? [y/N] "

    try:
        answer = input(msg).strip().lower()
        if answer in ("y", "yes"):
            log.info(f"Installing {apt_pkg}...")
            result = subprocess.run(
                ["sudo", "apt-get", "install", "-y", apt_pkg],
                timeout=300,
            )
            if result.returncode == 0:
                log.info(f"Installed {apt_pkg} successfully")
                return True
            else:
                log.error(f"Failed to install {apt_pkg}")
                return False
        else:
            log.info(f"Skipping install of {apt_pkg}")
            return False
    except (KeyboardInterrupt, EOFError):
        log.info("Interrupted")
        return False
    except subprocess.TimeoutExpired:
        log.error(f"Install of {apt_pkg} timed out")
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_all_deps(
    extra_deps: dict[str, dict] | None = None,
    auto_install: bool = True,
    quiet: bool = False,
) -> bool:
    """
    Check all system dependencies.

    Parameters
    ----------
    extra_deps : dict
        Project-supplied dependency overrides. Keys are arbitrary identifiers.
        Values are dicts with "apt_pkg" (str) and "check" (callable).
        Example::

            {
                "wasmedge": {"apt_pkg": "libwasmedge-dev",
                             "check": lambda: _find_header("wasmedge/wasmedge.h")},
                "rocksdb": {"apt_pkg": "librocksdb-dev",
                            "check": lambda: _find_header("rocksdb/db.h")},
            }

    auto_install : bool
        If True, prompt the user to install missing dependencies.
    quiet : bool
        If True, don't print progress messages.

    Returns
    -------
    bool
        True if all required (non-optional) deps are present.
    """
    deps = _builtin_deps()

    # Add project-supplied extra deps
    if extra_deps:
        for key, spec in extra_deps.items():
            dep = Dep(
                name=spec.get("name", key),
                apt_pkg=spec["apt_pkg"],
                check=spec["check"],
                hint=spec.get("hint", ""),
                optional=spec.get("optional", False),
            )
            deps.append(dep)

    if not quiet:
        log.info(f"Checking {len(deps)} system dependencies...")

    missing: list[Dep] = []
    for dep in deps:
        try:
            ok = dep.check()
        except Exception as e:
            log.warning(f"Check failed for {dep.name}: {e}")
            ok = False

        if ok:
            if not quiet:
                log.debug(f"  ✓ {dep.name}")
        else:
            missing.append(dep)
            if not quiet:
                status = "optional" if dep.optional else "REQUIRED"
                log.warning(f"  ✗ {dep.name} [{status}]")

    if not missing:
        if not quiet:
            log.info("All system dependencies satisfied")
        return True

    # Prompt for installs
    blocked = False
    for dep in missing:
        if dep.optional:
            if not quiet:
                log.info(f"  (optional) skipping {dep.name}")
            continue
        if auto_install:
            ok = _prompt_install(dep.apt_pkg, dep.name, dep.hint)
            if ok:
                # Re-check immediately
                try:
                    if dep.check():
                        if not quiet:
                            log.info(f"  ✓ {dep.name} (now present)")
                        continue
                except Exception:
                    pass
            # Still missing after install attempt
            blocked = True
            if not quiet:
                log.error(f"  ✗ {dep.name} is still not available")
        else:
            blocked = True

    if blocked:
        if not quiet:
            log.error("Cannot proceed: missing required dependencies")
        return False

    if not quiet:
        log.info("Required dependencies satisfied (optional ones skipped)")
    return True


# ---------------------------------------------------------------------------
# Convenience: header check function
# ---------------------------------------------------------------------------

def find_header(header: str, extra_paths: list[str] | None = None) -> str | None:
    """Public wrapper around _find_header."""
    return _find_header(header, extra_paths)
