"""
Target representation and build dependency graph.

A *Target* represents a single build unit: a .cpp file that produces a .o file.
A *Module* groups targets into a static library (.a).
The *BuildGraph* orchestrates everything.
"""

import hashlib
import os
from pathlib import Path


class Target:
    """A single compilation unit (one .o file from one source)."""
    __slots__ = ("src", "obj", "mod_name", "fp", "needs_build", "status", "cmd")

    def __init__(self, src: str, obj: str, mod_name: str):
        self.src = src
        self.obj = obj
        self.mod_name = mod_name
        self.fp: str = ""
        self.needs_build: bool = True
        self.status: str = "pending"   # pending | building | done | failed
        self.cmd: list[str] = []

    @property
    def key(self) -> str:
        """Cache-friendly key for this target."""
        return hashlib.sha256(
            f"{self.src}:{self.mod_name}".encode()
        ).hexdigest()[:16]

    def __repr__(self):
        return f"Target({self.src} → {self.obj} [{self.status}])"


class Module:
    """A group of targets that compile into a static library."""
    def __init__(self, name: str, directory: str, archive: str):
        self.name = name
        self.directory = directory
        self.archive = archive
        self.targets: list[Target] = []
        self.needs_link: bool = True
        self.status: str = "pending"
        self.depends: list[str] = []  # module names
        self.libs: list[str] = []     # system libs
        self.ldflags: list[str] = []

    def __repr__(self):
        return f"Module({self.name}: {len(self.targets)} targets)"


class Executable:
    """A final linked binary."""
    def __init__(self, name: str, output: str, module_names: list[str]):
        self.name = name
        self.output = output
        self.module_names = module_names
        self.libs: list[str] = []
        self.ldflags: list[str] = []
        self.status: str = "pending"

    def __repr__(self):
        return f"Executable({self.name}: {self.output})"


class BuildGraph:
    """
    The complete build dependency graph.

    Topology:
        Source files → Targets (.o) → Modules (.a) → Executable (binary)
    """

    def __init__(self, build_dir: str, cache_dir: str):
        self.build_dir = build_dir
        self.cache_dir = cache_dir
        self.modules: dict[str, Module] = {}
        self.executable: Executable | None = None
        self.all_targets: list[Target] = []
        self._dep_order: list[str] = []  # topological sort of modules

    def add_module(self, name: str, directory: str, archive: str,
                   depends: list[str], libs: list[str], ldflags: list[str]):
        mod = Module(name, directory, archive)
        mod.depends = depends
        mod.libs = libs
        mod.ldflags = ldflags
        self.modules[name] = mod
        return mod

    def add_target(self, mod_name: str, src: str, obj: str) -> Target:
        t = Target(src, obj, mod_name)
        self.all_targets.append(t)
        self.modules[mod_name].targets.append(t)
        return t

    def add_executable(self, name: str, output: str, module_names: list[str],
                       libs: list[str], ldflags: list[str]):
        self.executable = Executable(name, output, module_names)
        self.executable.libs = libs
        self.executable.ldflags = ldflags

    def topo_sort(self) -> list[str]:
        """Return module names in build order (dependencies first)."""
        if self._dep_order:
            return self._dep_order

        visited: set[str] = set()
        order: list[str] = []
        visiting: set[str] = set()

        def visit(name: str):
            if name in visiting:
                raise RuntimeError(f"Circular dependency: {name}")
            if name in visited:
                return
            visiting.add(name)
            mod = self.modules.get(name)
            if mod:
                for dep in mod.depends:
                    visit(dep)
            visiting.discard(name)
            visited.add(name)
            order.append(name)

        for mod_name in self.modules:
            visit(mod_name)

        self._dep_order = order
        return order

    def ordered_targets(self) -> list[Target]:
        """Return all targets in module dependency order."""
        result = []
        for mod_name in self.topo_sort():
            mod = self.modules.get(mod_name)
            if mod:
                result.extend(mod.targets)
        return result

    @property
    def total_targets(self) -> int:
        return len(self.all_targets)

    @property
    def ready_count(self) -> int:
        return sum(1 for t in self.all_targets if t.status == "pending")
