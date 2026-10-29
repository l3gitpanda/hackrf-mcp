import pytest

from hackrf_mcp import settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path):
    """Point every test at its own capture directory and audit log, with the
    free-space floor off so results don't depend on the test machine, and
    at a live-service directory where no service is listening."""
    previous = settings.get()
    previous_live = settings.get_live()
    configured = settings.Settings(
        data_dir=tmp_path,
        tx_log_path=tmp_path / "tx-audit.jsonl",
        min_free_mb=0,
    )
    settings.configure(configured)
    settings.configure_live(settings.LiveSettings(live_dir=tmp_path / "live", min_free_mb=0))
    yield configured
    settings.configure(previous)
    settings.configure_live(previous_live)
