"""Project layout resolver.

A *project* groups all artifacts for one optimization target (one DUT
under one spec): the spec / constraint file, the scrubbed netlist read
back from the remote host, the simulation outputs (.mt0 / .lis), and
the run logs (agent + hspice transcript).

The on-disk layout is::

    projects/<name>/
      constraints/      # spec.md plus any extra design-constraint yaml
      circuit/          # scrubbed .sp pulled back from the remote host
      sim_out/          # .mt0 / .lis pulled back after each iteration
      logs/
        agent/          # agent main-loop log (run_*.log)
        hspice/         # hspice transcript (jsonl)

The whole ``projects/`` tree is gitignored — every project carries
real (scrubbed) circuit data, so nothing under it ever ships to
GitHub. The tree only lives on the developer's machine.

A small set of "well-known" project names is reserved:

- ``_scratch``   — fallback when no ``--project`` is given and no spec
                   path can be inferred (debug / one-off runs)
- ``_legacy``    — pre-projects-layout artifacts migrated wholesale
                   (e.g. agent run logs from before this refactor)

Per-project override hook: ``Project.override_path(name)`` returns the
path that, if present, overrides the corresponding global config
(e.g. ``projects/<name>/scrub_patterns.yaml`` taking precedence over
the global ``config/hspice_scrub_patterns.yaml``). The loader side
(in :mod:`src.hspice_scrub`) currently does not consult these — the
hook is here so the convention is fixed for when we wire it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


_PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")

PROJECTS_ROOT_DIRNAME = "projects"
SCRATCH_PROJECT = "_scratch"
LEGACY_PROJECT = "_legacy"


class ProjectNameError(ValueError):
    """Raised when a project name violates the naming rules."""


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ProjectNameError("project name must be a non-empty string")
    if name in (SCRATCH_PROJECT, LEGACY_PROJECT):
        return
    if not _PROJECT_NAME_RE.match(name):
        raise ProjectNameError(
            f"project name {name!r} must match {_PROJECT_NAME_RE.pattern} "
            "(lowercase letters, digits, underscores; first char alnum)"
        )


@dataclass(frozen=True)
class Project:
    """Path resolver for a single optimization project.

    Construct from a name and (optionally) a non-default repo root.
    All ``*_dir`` properties return absolute :class:`Path` objects;
    callers should ``mkdir(parents=True, exist_ok=True)`` on them
    before writing.
    """

    name: str
    repo_root: Path

    def __post_init__(self) -> None:
        _validate_name(self.name)
        if not isinstance(self.repo_root, Path):
            object.__setattr__(self, "repo_root", Path(self.repo_root))

    @classmethod
    def from_repo(cls, name: str, repo_root: Optional[Path] = None) -> "Project":
        if repo_root is None:
            repo_root = _default_repo_root()
        return cls(name=name, repo_root=Path(repo_root).resolve())

    @property
    def root(self) -> Path:
        return self.repo_root / PROJECTS_ROOT_DIRNAME / self.name

    @property
    def constraints_dir(self) -> Path:
        return self.root / "constraints"

    @property
    def spec_file(self) -> Path:
        return self.constraints_dir / "spec.md"

    @property
    def circuit_dir(self) -> Path:
        return self.root / "circuit"

    @property
    def sim_out_dir(self) -> Path:
        return self.root / "sim_out"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def logs_agent_dir(self) -> Path:
        return self.logs_dir / "agent"

    @property
    def logs_hspice_dir(self) -> Path:
        return self.logs_dir / "hspice"

    def override_path(self, filename: str) -> Path:
        """Per-project config override location.

        Currently only a convention — no loader consults these yet.
        Reserved so the layout is fixed when we wire it.
        """
        return self.root / filename

    def ensure(self) -> None:
        for d in (
            self.constraints_dir,
            self.circuit_dir,
            self.sim_out_dir,
            self.logs_agent_dir,
            self.logs_hspice_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def projects_root(repo_root: Optional[Path] = None) -> Path:
    if repo_root is None:
        repo_root = _default_repo_root()
    return Path(repo_root) / PROJECTS_ROOT_DIRNAME


def list_projects(repo_root: Optional[Path] = None) -> list[str]:
    root = projects_root(repo_root)
    if not root.is_dir():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            out.append(child.name)
    return out


def infer_project_from_spec(spec_path: str | Path) -> Optional[str]:
    """If ``spec_path`` lives inside a project's constraints/ dir,
    return that project's name. Otherwise return None.

    Tolerates both forward and backslash separators (Windows callers).
    """
    p = Path(spec_path).resolve()
    parts = p.parts
    try:
        idx = parts.index(PROJECTS_ROOT_DIRNAME)
    except ValueError:
        return None
    if idx + 1 >= len(parts):
        return None
    candidate = parts[idx + 1]
    try:
        _validate_name(candidate)
    except ProjectNameError:
        return None
    return candidate


def resolve_project(
    name: Optional[str],
    spec_path: Optional[str | Path] = None,
    *,
    default: str = SCRATCH_PROJECT,
    repo_root: Optional[Path] = None,
) -> Project:
    """CLI helper.

    Priority:
      1. explicit ``name`` argument (``--project`` from CLI)
      2. inferred from ``spec_path`` if it lives under projects/<name>/
      3. ``default`` (defaults to _scratch)
    """
    if name:
        return Project.from_repo(name, repo_root=repo_root)
    if spec_path:
        inferred = infer_project_from_spec(spec_path)
        if inferred:
            return Project.from_repo(inferred, repo_root=repo_root)
    return Project.from_repo(default, repo_root=repo_root)


__all__ = [
    "Project",
    "ProjectNameError",
    "PROJECTS_ROOT_DIRNAME",
    "SCRATCH_PROJECT",
    "LEGACY_PROJECT",
    "projects_root",
    "list_projects",
    "infer_project_from_spec",
    "resolve_project",
]
