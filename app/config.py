import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

BASE_DIR = Path(__file__).resolve().parent.parent


def instance_dir():
    d = Path(os.environ.get("APP_INSTANCE_DIR", BASE_DIR / "instance"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def database_path():
    return os.environ.get("DATABASE_PATH") or str(instance_dir() / "app.db")


def load_keys():
    """Return (flask_secret, fernet_key_bytes).  Env vars win; otherwise a 0600 key file
    is created in instance/.  BACK THIS FILE UP: without the encryption key the stored
    Alipay private key and card keys cannot be decrypted."""
    secret = os.environ.get("APP_SECRET_KEY")
    enc = os.environ.get("APP_ENCRYPTION_KEY")
    if secret and enc:
        return secret, enc.encode()
    f = instance_dir() / "keys.json"
    if f.exists():
        data = json.loads(f.read_text())
    else:
        data = {}
    changed = False
    if "secret_key" not in data:
        data["secret_key"] = secrets.token_hex(32)
        changed = True
    if "enc_key" not in data:
        data["enc_key"] = Fernet.generate_key().decode()
        changed = True
    if changed:
        fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
    return secret or data["secret_key"], (enc or data["enc_key"]).encode()
