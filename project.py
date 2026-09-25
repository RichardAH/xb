"""
xbp (Xahau Build Project) file format.

Each source directory can contain an xbp.py file that exports a dict with the
following keys (all optional – defaults fill in the gaps):

    sources   – list of glob patterns for source files  (default: **/*.cpp)
    headers   – list of header globs tracked for deps   (default: **/*.h **/*.hpp)
    cflags    – extra compiler flags for this dir
    cxxflags  – extra C++-specific flags
    ldflags   – linker flags
    defines   – dict of preprocessor defines {name: value}  (value=None → -Dname)
    includes  – list of additional -I include paths
    depends   – list of target names this dir's objects link against
    libs      – list of system library names to link (-lname)
    static    – whether to produce .a archive (default True for lib dirs)
    executable – if True, produce a final linked binary from this dir
    entry     – the main() source (required for executable targets)
    exclude   – list of glob patterns to exclude from sources
"""

import os
import glob
import importlib.util
from pathlib import Path

# Default configuration
DEFAULTS = {
    "sources": ["**/*.cpp"],  # Recursive glob
    "headers": ["**/*.h", "**/*.hpp", "**/*.hh"],
    "cflags": [],
    "cxxflags": [],
    "ldflags": [],
    "defines": {},
    "includes": [],
    "depends": [],
    "libs": [],
    "static": True,
    "executable": False,
    "entry": None,
    "exclude": [],
}


def load_xbp(directory: str) -> dict:
    """
    Load and merge an xbp.py configuration file from *directory*.

    Returns a fully resolved config dict (all defaults present).
    """
    xbp_path = os.path.join(directory, "xbp.py")
    config = dict(DEFAULTS)
    # Make mutable copies of lists
    for key in config:
        if isinstance(config[key], list):
            config[key] = list(config[key])
        elif isinstance(config[key], dict):
            config[key] = dict(config[key])

    if not os.path.isfile(xbp_path):
        config["_source_files"] = _resolve_globs(directory, config["sources"])
        config["_header_files"] = _resolve_globs(directory, config["headers"])
        return config

    # Load the xbp.py as a module
    spec = importlib.util.spec_from_file_location(
        "xbp_" + os.path.basename(directory), xbp_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # The module should export a dict
    if hasattr(mod, "config"):
        user_cfg = mod.config
    elif hasattr(mod, "xbp"):
        user_cfg = mod.xbp
    else:
        # Auto-detect first non-uppercase dict in module
        user_cfg = next(
            (v for k, v in mod.__dict__.items()
             if isinstance(v, dict) and not k.startswith("_")),
            {},
        )

    # Merge – user values override defaults
    for key, val in user_cfg.items():
        if key in config:
            if isinstance(config[key], dict) and isinstance(val, dict):
                config[key].update(val)
            elif isinstance(config[key], list) and isinstance(val, list):
                config[key] = val  # replace, not merge – gives user full control
            else:
                config[key] = val

    # Resolve globs
    config["_source_files"] = _resolve_globs(
        directory, config["sources"], exclude=config.get("exclude", [])
    )
    config["_header_files"] = _resolve_globs(directory, config["headers"])

    return config


def _resolve_globs(directory: str, patterns: list[str], exclude: list[str] = None) -> list[str]:
    """Expand glob patterns relative to *directory*."""
    result = []
    for pattern in patterns:
        full = os.path.join(directory, pattern)
        matches = glob.glob(full, recursive=True)
        result.extend(matches)

    # Remove excluded files
    if exclude:
        excluded = set()
        for pat in exclude:
            excluded.update(glob.glob(os.path.join(directory, pat), recursive=True))
        result = [f for f in result if f not in excluded]

    return sorted(set(result))


def discover_modules(src_root: str, lib_parent: str = "libxrpl") -> list[dict]:
    """
    Discover all library modules under src/{lib_parent}/.

    Each subdirectory becomes a module.  Returns sorted list of (name, config).
    """
    lib_dir = os.path.join(src_root, "src", lib_parent)
    modules = []

    if not os.path.isdir(lib_dir):
        return modules

    for entry in sorted(os.listdir(lib_dir)):
        mod_dir = os.path.join(lib_dir, entry)
        if not os.path.isdir(mod_dir):
            continue
        config = load_xbp(mod_dir)
        config["_name"] = f"{lib_parent}.{entry}"
        config["_dir"] = mod_dir
        config["_src_root"] = src_root
        modules.append(config)

    return modules
