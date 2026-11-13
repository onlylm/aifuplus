from werkzeug.security import check_password_hash, generate_password_hash  # noqa: F401


def hash_password(pw):
    # pbkdf2 works on every Python build (hashlib.scrypt is missing on LibreSSL builds, e.g. macOS system Python)
    return generate_password_hash(pw, method="pbkdf2:sha256:600000")
