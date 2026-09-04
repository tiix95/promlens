import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("promlens.auth")

_CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "promlens.yaml"))


@dataclass
class AppAuthConfig:
    mode: str = "none"
    htpasswd_path: Optional[Path] = None
    secret: str = ""
    session_days: int = 7


def load_app_auth_config() -> AppAuthConfig:
    try:
        if not _CONFIG_FILE.exists():
            return AppAuthConfig()
        data = yaml.safe_load(_CONFIG_FILE.read_text()) or {}
        cfg = data.get("app_auth") or {}
        mode = (cfg.get("mode") or "none").lower()
        if mode not in ("none", "cert", "basic"):
            logger.warning("app_auth.mode=%r unknown -- using none", mode)
            mode = "none"
        htpasswd = cfg.get("htpasswd")
        secret = cfg.get("secret") or ""
        session_days = int(cfg.get("session_days") or 7)
        htpasswd_path = Path(htpasswd) if htpasswd else None
        if mode == "basic" and not htpasswd_path:
            logger.warning("app_auth.mode=basic but htpasswd not configured -- auth disabled")
            mode = "none"
        if mode != "none" and not secret:
            logger.critical(
                "app_auth.secret is not set -- authentication disabled. "
                "Set a random secret key in app_auth.secret to enable auth."
            )
            mode = "none"
        return AppAuthConfig(mode=mode, htpasswd_path=htpasswd_path, secret=secret, session_days=session_days)
    except Exception as exc:
        logger.error("Failed to load app_auth config: %s", exc)
        return AppAuthConfig()


def verify_password(htpasswd_path: Path, username: str, password: str) -> bool:
    try:
        from passlib.apache import HtpasswdFile
        ht = HtpasswdFile(str(htpasswd_path))
        ht.load()
        return ht.check_password(username, password) is True
    except Exception as exc:
        logger.error("htpasswd verification error: %s", exc)
        return False


def make_session_token(username: str, secret: str) -> str:
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(secret).dumps({"u": username})


def verify_session_token(token: str, secret: str, max_age: int) -> Optional[str]:
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    try:
        data = URLSafeTimedSerializer(secret).loads(token, max_age=max_age)
        return data.get("u")
    except (BadSignature, SignatureExpired):
        return None


LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ProMLens -- Login</title>
  <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <style>
    :root{--bg0:#060810;--bg1:#0b0e17;--border:#1e2540;--border-hi:#2a3354;
      --text0:#dde4f0;--text1:#7e8aaa;--text2:#3d4560;
      --accent:#00cfff;--accent-dim:rgba(0,207,255,.1);--accent-border:rgba(0,207,255,.28);
      --accent-glow:0 0 18px rgba(0,207,255,.22);
      --err-dim:rgba(240,79,79,.12);--err-border:rgba(240,79,79,.32);
      --radius:6px;--mono:'JetBrains Mono',monospace;--sans:'Syne',sans-serif}
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    html,body{height:100%;background:var(--bg0);color:var(--text0);font-family:var(--sans);font-size:14px}
    body::before{content:'';position:fixed;inset:0;background-image:radial-gradient(circle,rgba(0,207,255,.055) 1px,transparent 1px);background-size:28px 28px;pointer-events:none;z-index:0}
    .center{position:relative;z-index:1;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;gap:32px}
    .logo{font-size:28px;font-weight:800;letter-spacing:.1em;text-align:center}
    .logo .hi{color:var(--accent)}
    .logo-sub{font-family:var(--mono);font-size:10px;color:var(--text2);letter-spacing:.2em;margin-top:4px;text-align:center}
    .card{background:var(--bg1);border:1px solid var(--border-hi);border-radius:10px;padding:32px 40px;width:100%;max-width:360px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
    .field{display:flex;flex-direction:column;gap:6px;margin-bottom:16px}
    .field label{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.12em;color:var(--text2);text-transform:uppercase}
    .field input{background:var(--bg0);border:1px solid var(--border);color:var(--text0);padding:10px 12px;font-family:var(--mono);font-size:12px;border-radius:var(--radius);outline:none;transition:border-color .2s}
    .field input:focus{border-color:var(--accent-border);box-shadow:0 0 0 3px var(--accent-dim)}
    .field input::placeholder{color:var(--text2)}
    .btn{width:100%;background:var(--accent-dim);border:1px solid var(--accent-border);color:var(--accent);padding:11px;font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.15em;border-radius:var(--radius);cursor:pointer;transition:all .2s;margin-top:8px;text-transform:uppercase}
    .btn:hover{background:rgba(0,207,255,.18);box-shadow:var(--accent-glow)}
    .btn:disabled{opacity:.4;cursor:not-allowed}
    .err{display:none;align-items:center;gap:8px;padding:10px 12px;background:var(--err-dim);border:1px solid var(--err-border);border-radius:var(--radius);color:#f07070;font-family:var(--mono);font-size:11px;margin-bottom:16px}
    .err.show{display:flex}
  </style>
</head>
<body>
<div class="center">
  <div>
    <div class="logo">PROM<span class="hi">LENS</span></div>
    <div class="logo-sub">authentication required</div>
  </div>
  <div class="card">
    <div id="errBox" class="err"><span>&#9888;&nbsp;</span><span id="errMsg"></span></div>
    <form id="loginForm">
      <div class="field">
        <label for="username">Username</label>
        <input id="username" name="username" type="text" placeholder="username" autocomplete="username" required autofocus>
      </div>
      <div class="field">
        <label for="password">Password</label>
        <input id="password" name="password" type="password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autocomplete="current-password" required>
      </div>
      <button type="submit" id="submitBtn" class="btn">Sign in</button>
    </form>
  </div>
</div>
<script>
document.getElementById('loginForm').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  const errBox = document.getElementById('errBox');
  const errMsg = document.getElementById('errMsg');
  errBox.classList.remove('show');
  btn.disabled = true;
  btn.textContent = 'Signing in...';
  try {
    const data = {
      username: document.getElementById('username').value,
      password: document.getElementById('password').value
    };
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data)
    });
    if (r.ok) { window.location.replace('/'); return; }
    const d = await r.json().catch(() => ({}));
    errMsg.textContent = d.detail || 'Invalid credentials';
    errBox.classList.add('show');
  } catch {
    errMsg.textContent = 'Connection error';
    errBox.classList.add('show');
  }
  btn.disabled = false;
  btn.textContent = 'Sign in';
});
</script>
</body>
</html>"""
