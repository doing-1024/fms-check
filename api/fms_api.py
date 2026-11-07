#!/usr/bin/env python3
"""FreeMcServer.net v4 API client (reverse-engineered from Android app).

Protocol summary:
  Base URL : https://api.freemcserver.net/v4/
  Headers  : User-Agent: FMCS-App
             Authorization: <api_key from user/login>   (format: "SCOPED <jwt>")
             X-FMCS-Metrics-wfKpe9EATa: passkey (double xxhash64, seed=0)
  Passkey  : ds = UTC YYYY-MM-DD; c = ceil(utc_hour/6)+1
             h1 = xxh64(ds+c).hex  (16 chars, zero padded)
             pk = xxh64(h1+ds+c+"FreeMcServer.net").hex
"""
import datetime
import json
import math
import os
import sys
import uuid

import requests
import xxhash

BASE = "https://api.freemcserver.net/v4/"
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def generate_passkey(dt: datetime.datetime | None = None) -> str:
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    ds = dt.strftime("%Y-%m-%d")
    c = math.ceil(dt.hour / 6) + 1
    h1 = xxhash.xxh64((ds + str(c)).encode(), seed=0).hexdigest()
    return xxhash.xxh64((h1 + ds + str(c) + "FreeMcServer.net").encode(), seed=0).hexdigest()


def load_config() -> dict:
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            return json.load(f)
    return {}


def save_config(cfg: dict) -> None:
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=1)


class FmsClient:
    def __init__(self, api_key: str | None = None, device_id: str | None = None, user_id: int | None = None):
        cfg = load_config()
        self.api_key = api_key or cfg.get("api_key")
        self.device_id = device_id or cfg.get("device_id") or str(uuid.uuid4())
        self.user_id = user_id or cfg.get("user_id")
        if not cfg.get("device_id"):
            cfg["device_id"] = self.device_id
            save_config(cfg)
        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": "FMCS-App",
            "Content-Type": "application/json",
        })

    def _headers(self) -> dict:
        h = {"X-FMCS-Metrics-wfKpe9EATa": generate_passkey()}
        if self.api_key:
            h["Authorization"] = self.api_key
        return h

    def _post(self, path: str, body: dict, timeout: int = 20):
        r = self.http.post(BASE + path, json=body, headers=self._headers(), timeout=timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text[:500]}

    def _get(self, path: str, timeout: int = 20):
        r = self.http.get(BASE + path, headers=self._headers(), timeout=timeout)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text[:500]}

    # ---- auth ----
    def login(self, username: str, password: str) -> dict:
        code, data = self._post("user/login", {
            "username": username, "password": password,
            "scope": "ANDROID_APP", "mfa": None,
        })
        if code == 200 and data.get("success"):
            self.api_key = data["api_key"]
            self.user_id = data["user"]["id"]
            cfg = load_config()
            cfg.update({"api_key": self.api_key, "user_id": self.user_id,
                        "username": username, "password": password})
            save_config(cfg)
        return {"status": code, **data}

    # ---- device registration ----
    def register_device(self, app_version: str = "52", brand: str = "samsung",
                        model: str = "SM-S918B", fingerprint: str | None = None,
                        fcm_token: str = "no-token") -> dict:
        body = {
            "device_id": self.device_id,
            "device_model": model,
            "fcm_token": fcm_token,
            "is_emulator": False,
            "is_rooted": False,
            "app_version": app_version,
            "brand": brand,
            "fingerprint": fingerprint or f"samsung/dm3qxxx/dm3q:14/UP1A.231005.007/{self.device_id[:8]}:user/release-keys",
            "platform": "android",
        }
        return dict(zip(("status", "data"), self._post("user/device", body)))

    # ---- reward flow (ads for credits) ----
    def reward_authorization(self, operation_type: int = 1, extra: dict | None = None) -> dict:
        body = {"deviceId": self.device_id, "operationType": operation_type}
        if extra:
            body["extra"] = extra
        return dict(zip(("status", "data"), self._post("reward/authorization", body)))

    def reward_claim(self, reward_authorization_id: str, nonce: str,
                     integrity_authorization_id: str | None = None) -> dict:
        body = {
            "rewardAuthorizationId": reward_authorization_id,
            "integrityAuthorizationId": integrity_authorization_id,
            "deviceId": self.device_id,
            "nonce": nonce,
        }
        return dict(zip(("status", "data"), self._post("reward/claim", body)))

    # ---- integrity flow ----
    def integrity_authorization(self, operation_type: int = 1, server_id: int | None = None) -> dict:
        body = {"deviceId": self.device_id, "operationType": operation_type}
        if server_id:
            body["serverId"] = server_id
        return dict(zip(("status", "data"), self._post("integrity/authorization", body)))

    def integrity_dispatch(self, authorization_id: str, token: str, operation_type: int = 1) -> dict:
        body = {
            "authorizationId": authorization_id,
            "deviceId": self.device_id,
            "operationType": operation_type,
            "token": token,
            "b24": True,
        }
        return dict(zip(("status", "data"), self._post("integrity/dispatch", body)))

    # ---- misc ----
    def me(self) -> dict:
        return dict(zip(("status", "data"), self._get("user")))

    def credits_config(self) -> dict:
        return dict(zip(("status", "data"), self._get("app-settings/credits")))

    def servers(self) -> dict:
        return dict(zip(("status", "data"), self._get("server")))

    def toggle_auto_renewal(self, server_id: int, period: int = 7) -> dict:
        return dict(zip(("status", "data"), self._post(f"server/{server_id}/toggle-auto-renewal",
                                                        {"auto_renew_period": period})))


if __name__ == "__main__":
    cfg = load_config()
    c = FmsClient()
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        print(json.dumps(c.login(cfg.get("username", ""), cfg.get("password", "")), indent=1)[:800])
    else:
        print("usage: python3 fms_api.py login")
