import pytest

from hackrf_mcp import settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    """Point every test at its own capture directory and audit log, with the
    free-space floor off so results don't depend on the test machine."""
    previous = settings.get()
    configured = settings.Settings(
        data_dir=tmp_path,
        tx_log_path=tmp_path / "tx-audit.jsonl",
        min_free_mb=0,
    )
    settings.configure(configured)
    yield configured
    settings.configure(previous)
