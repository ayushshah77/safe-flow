import pytest
from app.actions.core import safe_path, _check_url

def test_path_traversal():
    with pytest.raises(PermissionError):
        safe_path("../../etc/passwd")

def test_bad_extension():
    with pytest.raises(PermissionError):
        safe_path("run.sh")

def test_ssrf_localhost():
    with pytest.raises(PermissionError):
        _check_url("https://localhost/admin")

def test_http_blocked():
    with pytest.raises(PermissionError):
        _check_url("http://example.com")
