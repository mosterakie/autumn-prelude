import base64

import pytest

from autumn_backend.auth.security import SecretBox, totp, totp_step
from autumn_backend.config import Environment, Settings

pytestmark = pytest.mark.unit


def test_totp_rfc6238_vector_and_replay_boundary() -> None:
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp(secret, 59 // 30, digits=8) == "94287082"
    code = totp(secret, 100)
    assert totp_step(secret, code, 100, None) == 100
    assert totp_step(secret, code, 100, 100) is None
    box = SecretBox("a" * 40)
    encrypted = box.seal(secret)
    assert secret not in encrypted and box.open(encrypted) == secret


def test_production_requires_encryption_key_origin_and_host_cookie_contract() -> None:
    base = dict(
        environment=Environment.PROD,
        session_secret="s" * 40,
        csrf_secret="c" * 40,
        cookie_secure=True,
        cookie_name="__Host-autumn_session",
    )
    with pytest.raises(ValueError, match="AUTH_ENCRYPTION_KEY"):
        Settings(**base)
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(**base, auth_encryption_key="e" * 40)
    with pytest.raises(ValueError, match="Domain"):
        Settings(
            **base,
            auth_encryption_key="e" * 40,
            trusted_origins=("https://autumn.example",),
            cookie_domain="example",
        )
