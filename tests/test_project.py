"""Tests for src/project.py — the project layout resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.project import (
    LEGACY_PROJECT,
    PROJECTS_ROOT_DIRNAME,
    SCRATCH_PROJECT,
    Project,
    ProjectNameError,
    infer_project_from_spec,
    list_projects,
    projects_root,
    resolve_project,
)


class TestProjectName:
    def test_accepts_lowercase_alnum_underscore(self):
        Project(name="lc_vco_base", repo_root=Path("."))
        Project(name="cobi_delay", repo_root=Path("."))
        Project(name="a", repo_root=Path("."))
        Project(name="x1", repo_root=Path("."))

    def test_rejects_uppercase(self):
        with pytest.raises(ProjectNameError):
            Project(name="LC_VCO", repo_root=Path("."))

    def test_rejects_dash(self):
        with pytest.raises(ProjectNameError):
            Project(name="cobi-delay", repo_root=Path("."))

    def test_rejects_leading_underscore_user_name(self):
        with pytest.raises(ProjectNameError):
            Project(name="_foo", repo_root=Path("."))

    def test_rejects_empty(self):
        with pytest.raises(ProjectNameError):
            Project(name="", repo_root=Path("."))

    def test_reserved_scratch_allowed(self):
        Project(name=SCRATCH_PROJECT, repo_root=Path("."))

    def test_reserved_legacy_allowed(self):
        Project(name=LEGACY_PROJECT, repo_root=Path("."))


class TestProjectPaths:
    def test_subdir_layout(self, tmp_path: Path):
        p = Project(name="lc_vco_base", repo_root=tmp_path)
        assert p.root == tmp_path / "projects" / "lc_vco_base"
        assert p.constraints_dir == p.root / "constraints"
        assert p.spec_file == p.root / "constraints" / "spec.md"
        assert p.circuit_dir == p.root / "circuit"
        assert p.sim_out_dir == p.root / "sim_out"
        assert p.logs_agent_dir == p.root / "logs" / "agent"
        assert p.logs_hspice_dir == p.root / "logs" / "hspice"

    def test_ensure_creates_all_subdirs(self, tmp_path: Path):
        p = Project(name="cobi_delay", repo_root=tmp_path)
        p.ensure()
        assert p.constraints_dir.is_dir()
        assert p.circuit_dir.is_dir()
        assert p.sim_out_dir.is_dir()
        assert p.logs_agent_dir.is_dir()
        assert p.logs_hspice_dir.is_dir()

    def test_override_path_under_root(self, tmp_path: Path):
        p = Project(name="cobi_matching", repo_root=tmp_path)
        assert p.override_path("scrub_patterns.yaml") == p.root / "scrub_patterns.yaml"


class TestFromRepo:
    def test_resolves_repo_root_to_absolute(self, tmp_path: Path):
        # tmp_path is already absolute, but exercise the resolve() path
        rel = tmp_path
        p = Project.from_repo("lc_vco_40g", repo_root=rel)
        assert p.repo_root.is_absolute()


class TestInferFromSpec:
    def test_recognises_project_constraints_path(self, tmp_path: Path):
        spec = tmp_path / PROJECTS_ROOT_DIRNAME / "lc_vco_base" / "constraints" / "spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# spec", encoding="utf-8")
        assert infer_project_from_spec(spec) == "lc_vco_base"

    def test_returns_none_for_path_outside_projects(self, tmp_path: Path):
        spec = tmp_path / "config" / "LC_VCO_spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# spec", encoding="utf-8")
        assert infer_project_from_spec(spec) is None

    def test_returns_none_for_invalid_project_name(self, tmp_path: Path):
        spec = tmp_path / PROJECTS_ROOT_DIRNAME / "BadName" / "constraints" / "spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# spec", encoding="utf-8")
        assert infer_project_from_spec(spec) is None


class TestResolveProject:
    def test_explicit_name_wins(self, tmp_path: Path):
        spec = tmp_path / PROJECTS_ROOT_DIRNAME / "cobi_delay" / "constraints" / "spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# spec", encoding="utf-8")
        p = resolve_project("cobi_matching", spec_path=spec, repo_root=tmp_path)
        assert p.name == "cobi_matching"

    def test_falls_back_to_inferred(self, tmp_path: Path):
        spec = tmp_path / PROJECTS_ROOT_DIRNAME / "cobi_delay" / "constraints" / "spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# spec", encoding="utf-8")
        p = resolve_project(None, spec_path=spec, repo_root=tmp_path)
        assert p.name == "cobi_delay"

    def test_falls_back_to_default(self, tmp_path: Path):
        p = resolve_project(None, spec_path=None, repo_root=tmp_path)
        assert p.name == SCRATCH_PROJECT


class TestListProjects:
    def test_returns_empty_when_no_projects_dir(self, tmp_path: Path):
        assert list_projects(tmp_path) == []

    def test_lists_subdirs_sorted(self, tmp_path: Path):
        root = projects_root(tmp_path)
        for name in ("cobi_matching", "cobi_delay", "lc_vco_base"):
            (root / name).mkdir(parents=True)
        # also create a hidden + a file, both should be ignored
        (root / ".hidden").mkdir()
        (root / "stray_file.txt").write_text("x", encoding="utf-8")
        assert list_projects(tmp_path) == ["cobi_delay", "cobi_matching", "lc_vco_base"]
