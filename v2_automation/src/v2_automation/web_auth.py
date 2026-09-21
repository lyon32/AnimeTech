"""Authentication of the web panel: one operator account, a signed session cookie, no secret in code or logs.

Configuration (environment / `.env`, never in the repository):
  ADMIN_WEB_PASSWORD        the operator's password (plain, read once at start)   — or —
  ADMIN_WEB_PASSWORD_HASH   pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>  (see `hash_password`)
  ADMIN_WEB_USERNAME        login name (default "admin")
  ADMIN_WEB_SECRET          signing key of the session cookie (default: random, sessions end at each restart)

Without a password the panel REFUSES to start (`cmd_serve`), unless V2_WEB_ALLOW_NO_AUTH=1 is set explicitly.
Failed logins are rate limited per client address.  The cookie is HttpOnly + SameSite=Strict.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field

COOKIE = "v2_session"
SESSION_TTL_S = 12 * 3600
MAX_FAILURES, WINDOW_S = 5, 60
_ITER = 200_000


def hash_password(password: str, *, salt: bytes | None = None, iterations: int = _ITER) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        _, it, salt, digest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


@dataclass
class WebAuth:
    username: str
    password_hash: str
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    clock: callable = time.time
    _failures: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env=os.environ) -> "WebAuth | None":
        pw, ph = env.get("ADMIN_WEB_PASSWORD", ""), env.get("ADMIN_WEB_PASSWORD_HASH", "")
        if not pw and not ph:
            return None
        secret = env.get("ADMIN_WEB_SECRET", "")
        return cls(env.get("ADMIN_WEB_USERNAME", "admin") or "admin", ph or hash_password(pw),
                   secret.encode() if secret else secrets.token_bytes(32))

    # -- login ------------------------------------------------------------------------
    def limited(self, client: str) -> bool:
        now = self.clock()
        recent = [t for t in self._failures.get(client, []) if now - t < WINDOW_S]
        self._failures[client] = recent
        return len(recent) >= MAX_FAILURES

    def check(self, username: str, password: str, client: str = "?") -> bool:
        ok_user = hmac.compare_digest((username or "").encode(), self.username.encode())
        ok_pass = _verify_hash(password or "", self.password_hash)
        if ok_user and ok_pass:
            self._failures.pop(client, None)
            return True
        self._failures.setdefault(client, []).append(self.clock())
        return False

    # -- session cookie -----------------------------------------------------------------
    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = base64.urlsafe_b64encode(f"{self.username}|{int(self.clock() + SESSION_TTL_S)}".encode()).decode()
        return f"{payload}.{self._sign(payload)}"

    def user_of(self, cookie: str | None) -> str | None:
        if not cookie or "." not in cookie:
            return None
        payload, sig = cookie.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sign(payload)):
            return None
        try:
            user, exp = base64.urlsafe_b64decode(payload.encode()).decode().split("|")
            return user if int(exp) > self.clock() else None
        except (ValueError, TypeError):
            return None


LOGIN_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connexion</title><style>body{font:16px system-ui;background:#14171c;color:#e6e8eb;display:grid;place-items:center;min-height:100vh;margin:0}
form{display:grid;gap:12px;width:min(320px,90vw)}input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #333b46;background:#1c2128;color:inherit}
button{background:#33d6a0;color:#06251c;border:0;font-weight:600;cursor:pointer}#e{color:#ff8a8a;min-height:1.2em}</style></head><body>
<form id="f"><h1 style="margin:0">Panneau admin</h1><input name="username" placeholder="Identifiant" autocomplete="username" required>
<input name="password" type="password" placeholder="Mot de passe" autocomplete="current-password" required><button>Se connecter</button><div id="e" role="alert"></div></form>
<script>document.getElementById('f').onsubmit=async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));
const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
if(r.ok)location.href='/';else document.getElementById('e').textContent=r.status==429?'Trop de tentatives, patientez.':'Identifiants invalides.'}</script></body></html>"""
