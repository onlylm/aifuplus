import hashlib
import hmac

from cryptography.fernet import Fernet


class Crypto:
    def __init__(self, enc_key):
        self._f = Fernet(enc_key)
        self._hmac_key = hashlib.sha256(b"card-hash:" + enc_key).digest()

    def encrypt(self, text):
        return self._f.encrypt(text.encode("utf-8")).decode("ascii")

    def decrypt(self, token):
        return self._f.decrypt(token.encode("ascii")).decode("utf-8")

    def hash_code(self, text):
        return hmac.new(self._hmac_key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_code(code):
    n = len(code)
    if n <= 4:
        return "*" * n
    if n <= 8:
        return code[:1] + "*" * (n - 2) + code[-1:]
    return code[:2] + "*" * min(n - 4, 12) + code[-2:]
