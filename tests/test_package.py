import mining_brief


def test_version_is_exposed() -> None:
    assert mining_brief.__version__ == "0.2.4"
