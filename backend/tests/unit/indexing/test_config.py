import pytest
from aegis_apps.indexing.config import ScanPolicy


def test_scan_defaults_and_bounds() -> None:
    assert ScanPolicy.from_environment({}) == ScanPolicy(3600, 120, 500, 2)
    for values in (
        {"AEGIS_SCAN_INTERVAL_SECONDS": "0"},
        {"AEGIS_SCAN_IDLE_TIMEOUT_SECONDS": "3601"},
        {"AEGIS_SCAN_BATCH_RECORDS": "2001"},
        {"AEGIS_SCAN_READERS": "0"},
    ):
        with pytest.raises(ValueError, match="invalid scan policy"):
            ScanPolicy.from_environment(values)


@pytest.mark.parametrize(
    "value",
    ("+100", " 100", "100 ", "1_00", "\u0661\u0660\u0660", "1000000"),
)
def test_scan_policy_rejects_noncanonical_integers(value: str) -> None:
    with pytest.raises(ValueError, match="invalid scan policy"):
        ScanPolicy.from_environment({"AEGIS_SCAN_BATCH_RECORDS": value})
