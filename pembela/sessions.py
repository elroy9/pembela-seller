"""Penghubung ke Seller Center tiruan. FlaskSession (in-process, untuk uji) dan HttpSession (server nyata)."""
from __future__ import annotations

import io
import json


class FlaskSession:
    def __init__(self, flask_app, user, password, api_key):
        self.c = flask_app.test_client()
        self.key = api_key
        self.user, self.password = user, password
        self.login()

    def login(self):
        self.c.post("/login", data={"username": self.user, "password": self.password})

    def get_json(self, path):
        r = self.c.get(path, headers={"X-Mock-Key": self.key})
        try:
            return r.status_code, json.loads(r.get_data(as_text=True))
        except ValueError:
            return r.status_code, None

    def get_page(self, path):
        r = self.c.get(path)
        return r.status_code, r.get_data(as_text=True)

    def post_appeal(self, did, reason, text, files):
        """files: list[(nama, bytes)] -> (status_code, html)"""
        data = {"reason": reason, "appeal_text": text,
                "evidence": [(io.BytesIO(b), n) for n, b in files]}
        r = self.c.post("/disputes/%s/appeal" % did, data=data, content_type="multipart/form-data")
        return r.status_code, r.get_data(as_text=True)


class HttpSession:
    """Untuk server asli: python app.py -> http://127.0.0.1:5000 (butuh `requests`)."""

    def __init__(self, base_url, user, password, api_key):
        import requests
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.s = requests.Session()
        self.s.post(self.base + "/login", data={"username": user, "password": password}, timeout=10)

    def get_json(self, path):
        r = self.s.get(self.base + path, headers={"X-Mock-Key": self.key}, timeout=10)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    def get_page(self, path):
        r = self.s.get(self.base + path, timeout=10)
        return r.status_code, r.text

    def post_appeal(self, did, reason, text, files):
        r = self.s.post(self.base + "/disputes/%s/appeal" % did,
                        data={"reason": reason, "appeal_text": text},
                        files=[("evidence", (n, b)) for n, b in files], timeout=30)
        return r.status_code, r.text
