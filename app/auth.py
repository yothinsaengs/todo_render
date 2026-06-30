import base64
import hashlib
import hmac
import os
import time


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def create_password_hash(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        _encode(salt),
        _encode(derived),
    )


def verify_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded_hash.split("$", 5)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            password.encode(),
            salt=_decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_decode(expected)),
        )
        return hmac.compare_digest(derived, _decode(expected))
    except (ValueError, TypeError):
        return False


def create_session(username: str, secret: str, lifetime_seconds: int) -> str:
    expires_at = int(time.time()) + lifetime_seconds
    payload = f"{username}\n{expires_at}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_session(token: str | None, username: str, secret: str) -> bool:
    if not token or not username or not secret:
        return False
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _decode(encoded_payload)
        expected_signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected_signature, _decode(encoded_signature)):
            return False
        token_username, expires_at = payload.decode().split("\n", 1)
        return hmac.compare_digest(token_username, username) and int(expires_at) >= int(
            time.time()
        )
    except (ValueError, UnicodeDecodeError):
        return False


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
