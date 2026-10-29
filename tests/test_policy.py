import json

import pytest

from hackrf_mcp import policy, settings
from hackrf_mcp.settings import Settings


def test_off_mode_refuses_everything():
    decision = policy.check_tx(Settings(tx_mode="off"), 915e6, 2e6, (-1e3, 1e3))

    assert not decision.allowed
    assert "tx_mode=off" in decision.reason


def test_unrestricted_mode_allows_any_frequency():
    decision = policy.check_tx(Settings(tx_mode="unrestricted"), 1090e6, 2e6)

    assert decision.allowed
    assert decision.span_hz == (1089e6, 1091e6)


def test_allowed_bands_accepts_signal_inside_ism_band():
    decision = policy.check_tx(Settings(tx_mode="allowed-bands"), 915e6, 2e6, (-10e3, 10e3))

    assert decision.allowed
    assert decision.band.label == "915 MHz ISM"


def test_allowed_bands_refuses_frequency_outside_every_band():
    decision = policy.check_tx(Settings(tx_mode="allowed-bands"), 1090e6, 2e6, (-10e3, 10e3))

    assert not decision.allowed
    assert "not inside any allowed band" in decision.reason


def test_allowed_bands_refuses_unmeasured_signal_bleeding_past_band_edge():
    # 927 MHz with a 10 MHz sample rate could reach 932 MHz, past 928 MHz.
    decision = policy.check_tx(Settings(tx_mode="allowed-bands"), 927e6, 10e6, None)

    assert not decision.allowed
    assert "assumed full sample-rate" in decision.reason


def test_allowed_bands_accepts_measured_narrow_signal_near_band_edge():
    decision = policy.check_tx(Settings(tx_mode="allowed-bands"), 927e6, 10e6, (-20e3, 20e3))

    assert decision.allowed


def test_span_always_includes_the_carrier():
    # Signal entirely above the carrier: the carrier itself still radiates.
    low, high = policy.occupied_span(915e6, 2e6, (100e3, 200e3))

    assert low == 915e6
    assert high == 915.2e6


def test_amateur_bands_only_when_selected():
    ism_only = Settings(tx_mode="allowed-bands")
    with_ham = Settings(tx_mode="allowed-bands", tx_band_sets=("ism", "amateur"))

    assert not policy.check_tx(ism_only, 146.52e6, 2e6, (-8e3, 8e3)).allowed
    assert policy.check_tx(with_ham, 146.52e6, 2e6, (-8e3, 8e3)).allowed


def test_extra_ranges_are_allowed():
    s = Settings(tx_mode="allowed-bands", tx_band_sets=(), tx_extra_ranges=((433.0e6, 435.0e6),))

    assert policy.check_tx(s, 433.92e6, 2e6, (-10e3, 10e3)).allowed
    assert not policy.check_tx(s, 915e6, 2e6, (-10e3, 10e3)).allowed


def test_unknown_band_set_raises():
    with pytest.raises(ValueError, match="unknown band set"):
        policy.allowed_bands(Settings(tx_band_sets=("nope",)))


def test_bands_file_adds_and_overrides_sets(tmp_path):
    bands_file = tmp_path / "bands.json"
    bands_file.write_text(json.dumps({"sets": {"lab": [[433.05, 434.79, "lab bench"]]}}))
    s = Settings(tx_mode="allowed-bands", tx_band_sets=("lab",), bands_file=bands_file)

    assert policy.check_tx(s, 433.92e6, 2e6, (-10e3, 10e3)).allowed
    assert not policy.check_tx(s, 915e6, 2e6, (-10e3, 10e3)).allowed


def test_invalid_tx_mode_is_rejected():
    with pytest.raises(ValueError, match="tx_mode"):
        Settings(tx_mode="on")


def test_settings_from_env():
    s = Settings.from_env(
        {
            "HACKRF_MCP_TX_MODE": "Allowed-Bands",
            "HACKRF_MCP_TX_BANDS": "ism, amateur",
            "HACKRF_MCP_TX_EXTRA_RANGES": "433-435",
            "HACKRF_MCP_MAX_TX_SECONDS": "10",
            "HACKRF_MCP_DATA_DIR": "/srv/captures",
        }
    )

    assert s.tx_mode == "allowed-bands"
    assert s.tx_band_sets == ("ism", "amateur")
    assert s.tx_extra_ranges == ((433e6, 435e6),)
    assert s.max_tx_seconds == 10
    assert str(s.data_dir) == "/srv/captures"


def test_band_set_none_means_no_sets():
    assert settings.parse_band_set_names("none") == ()


@pytest.mark.parametrize("value", ["433", "435-433"])
def test_bad_extra_ranges_are_rejected(value):
    with pytest.raises(ValueError):
        settings.parse_mhz_ranges(value)
