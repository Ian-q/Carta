import carta


def test_version_string():
    assert isinstance(carta.__version__, str)
    assert len(carta.__version__) > 0
