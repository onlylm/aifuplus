"""Shared application context passed to the service modules."""
import threading
import time

from . import db
from .crypto import Crypto


class Core:
    def __init__(self, db_path, enc_key, clock=None, alipay_transport=None, upstream_transport=None,
                 public_base_url_env="", fx_provider=None):
        self.db_path = str(db_path)
        self.crypto = Crypto(enc_key)
        self.clock = clock or time.time
        self.alipay_transport = alipay_transport
        self.upstream_transport = upstream_transport
        self.public_base_url_env = public_base_url_env
        self.fx_provider = fx_provider          # callable() -> (rate_micro, source); None = real providers
        self._local = threading.local()

    def now(self):
        return int(self.clock())

    @property
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = db.connect(self.db_path)
            self._local.conn = c
        return c

    def event(self, conn, order_id, kind, message, detail=""):
        conn.execute("INSERT INTO events(order_id, kind, message, detail, created_at) VALUES (?,?,?,?,?)",
                     (order_id, kind, message[:2000], detail[:2000], self.now()))
