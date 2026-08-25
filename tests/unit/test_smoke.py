import repotrial


def test_package_exposes_version() -> None:
    assert repotrial.__version__ == "0.1.0"
