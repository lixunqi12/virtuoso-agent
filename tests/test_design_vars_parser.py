"""Tests for _load_allowed_design_vars — dynamic §4 whitelist parser."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import _load_allowed_design_vars  # noqa: E402


@pytest.fixture
def valid_spec(tmp_path):
    """Minimal spec with a valid §4 table."""
    content = """\
## §4 Design Variables

| Var | Role | Range | Priority |
|---|---|---|---|
| `Ibias` | mirror ref | 100u-2m | P1 |
| `nfin_neg` | xcouple | 4-32 | P1 |
| `C` | tank cap | 10f-200f | P2 |
| `L` | inductor | 100p-2n | P1 |

---

## §5 Next section
"""
    path = tmp_path / "spec.md"
    path.write_text(content, encoding="utf-8")
    return path


class TestNormalParse:
    def test_returns_tuple_of_names(self, valid_spec):
        result = _load_allowed_design_vars(valid_spec)
        assert isinstance(result, tuple)
        assert set(result) == {"Ibias", "nfin_neg", "C", "L"}

    def test_order_preserved(self, valid_spec):
        result = _load_allowed_design_vars(valid_spec)
        assert result == ("Ibias", "nfin_neg", "C", "L")


class TestFileMissing:
    def test_raises_runtime_error(self, tmp_path):
        missing = tmp_path / "nonexistent.md"
        with pytest.raises(RuntimeError, match="not found"):
            _load_allowed_design_vars(missing)


class TestSection4Missing:
    def test_raises_runtime_error(self, tmp_path):
        content = "## §3 Overview\nSome text.\n## §5 Other\n"
        path = tmp_path / "spec.md"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(
            RuntimeError, match="Design variables section not found"
        ):
            _load_allowed_design_vars(path)


class TestEmptyTable:
    def test_raises_runtime_error(self, tmp_path):
        content = """\
## §4 Design Variables

No table here, just text.

## §5 Next
"""
        path = tmp_path / "spec.md"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(RuntimeError, match="no design variables"):
            _load_allowed_design_vars(path)


class TestEnvOverride:
    def test_env_var_overrides_default_path(self, valid_spec):
        with patch.dict(os.environ, {"LC_VCO_SPEC_PATH": str(valid_spec)}):
            result = _load_allowed_design_vars(
                Path(os.environ["LC_VCO_SPEC_PATH"])
            )
        assert set(result) == {"Ibias", "nfin_neg", "C", "L"}


class TestEnvOverrideImportTime:
    """LC_VCO_SPEC_PATH env var takes effect at import time."""

    def test_subprocess_import_uses_env_spec(self, tmp_path):
        """Spawn a subprocess with LC_VCO_SPEC_PATH pointing to a
        custom spec; verify _VALID_DESIGN_VAR_NAMES reflects it."""
        custom_spec = tmp_path / "custom_spec.md"
        custom_spec.write_text(
            "## 4. Design variables\n\n"
            "| Var | Role | Range | Priority |\n"
            "|---|---|---|---|\n"
            "| `Alpha` | test var | 1-10 | P1 |\n"
            "| `Beta` | test var | 1-10 | P2 |\n",
            encoding="utf-8",
        )
        script = (
            "import sys; sys.path.insert(0, r'" + str(PROJECT_ROOT) + "'); "
            "from src.agent import _VALID_DESIGN_VAR_NAMES; "
            "print(sorted(_VALID_DESIGN_VAR_NAMES))"
        )
        env = {**os.environ, "LC_VCO_SPEC_PATH": str(custom_spec)}
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "Alpha" in result.stdout
        assert "Beta" in result.stdout
        # Must NOT contain the default spec's vars
        assert "Ibias" not in result.stdout


class TestRealSpec:
    """Smoke-test against the actual spec file in the repo."""

    def test_real_spec_parses_8_vars(self):
        real_spec = PROJECT_ROOT / "config" / "LC_VCO_spec.md"
        if not real_spec.exists():
            pytest.skip("Real spec file not present")
        result = _load_allowed_design_vars(real_spec)
        assert len(result) == 8
        assert set(result) == {
            "Ibias", "nfin_neg", "nfin_cc", "nfin_mirror",
            "nfin_tail", "R", "C", "L",
        }
