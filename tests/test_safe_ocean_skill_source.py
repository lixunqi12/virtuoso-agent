from pathlib import Path


def test_safe_ocean_run_tries_ocean_results_dir_before_ade_fallback() -> None:
    """Lock the generic OCEAN resultsDir() fallback for safeOceanRun.

    Some runs return a history token while the openable PSF root is the
    OCEAN session's resultsDir(), not runResult/psf and not ADE's
    asiGetResultsDir(). Dropping this fallback blinds transient readback.
    """
    source = Path("skill/safe_ocean.il").read_text(encoding="utf-8")

    runpath_probe = 'psfPath = strcat(runPath "/psf")'
    ocean_fallback = "oceanDir = resultsDir()"
    ade_fallback = "asiGetResultsDir(asiGetCurrentSession())"

    assert runpath_probe in source
    assert "isCallable('resultsDir)" in source
    assert ocean_fallback in source
    assert ade_fallback in source
    assert source.index(runpath_probe) < source.index(ocean_fallback)
    assert source.index(ocean_fallback) < source.index(ade_fallback)
