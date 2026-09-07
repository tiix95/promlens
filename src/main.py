import asyncio
import json
import logging
import math
import os
import re
import time
import traceback
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, RootModel, ValidationError, field_validator
from typing import Optional, Union

from auth import LOGIN_HTML, load_app_auth_config, make_session_token, verify_password, verify_session_token
from local_alerts import parse_thresholds
from notifier import run_notifier
from prometheus_query import (
    PrometheusConfig,
    RangeParams,
    _build_session,
    build_config,
    query_alerts,
    query_instant,
    query_range,
)

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s -- %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("promlens")

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Run the webhook alert notifier alongside the HTTP server."""
    task = asyncio.create_task(run_notifier())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="ProMLens", lifespan=lifespan)

CONFIG_FILE   = Path(os.environ.get("CONFIG_FILE",   "promlens.yaml"))
TOPOLOGY_FILE = Path(os.environ.get("TOPOLOGY_FILE", "topology.yaml"))
SESSION_SECURE_COOKIE = os.environ.get("SESSION_SECURE_COOKIE", "1").lower() not in ("0", "false", "no")

_AUTH_OPEN_PATHS = {"/login", "/api/auth/login", "/api/auth/logout"}

_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}

_csp_cache: tuple[float, str] = (0.0, "")


def _build_csp() -> str:
    global _csp_cache
    try:
        mtime = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.exists() else 0.0
    except OSError:
        mtime = 0.0
    if mtime and mtime == _csp_cache[0]:
        return _csp_cache[1]

    extra_origin = ""
    if CONFIG_FILE.exists():
        try:
            data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
            url = data.get("url", "")
            if url:
                p = urlparse(url)
                extra_origin = f"{p.scheme}://{p.netloc}"
        except Exception:
            pass

    connect_src = f"'self' {extra_origin}" if extra_origin else "'self'"
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self' data:; "
        "img-src 'self' data:; "
        f"connect-src {connect_src};"
    )
    _csp_cache = (mtime, csp)
    return csp


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers[header] = value
    response.headers["Content-Security-Policy"] = _build_csp()
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path in _AUTH_OPEN_PATHS:
        return await call_next(request)

    auth_cfg = load_app_auth_config()

    if auth_cfg.mode == "none":
        return await call_next(request)

    if auth_cfg.mode == "cert":
        user = request.headers.get("x-remote-user", "").strip()
        if not user:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "Unauthorized", "detail": "x-remote-user header missing"}, status_code=401)
            return HTMLResponse(
                '<meta charset="UTF-8"><title>403</title>'
                '<body style="background:#060810;color:#dde4f0;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">'
                '<div style="text-align:center"><div style="font-size:48px;opacity:.2">⛔</div>'
                '<h2 style="color:#f04f4f;margin:16px 0 8px">Access denied</h2>'
                '<p style="color:#7e8aaa;font-size:13px">Certificate authentication required (x-remote-user header missing)</p></div></body>',
                status_code=403,
            )
        request.state.user = user
        return await call_next(request)

    if auth_cfg.mode == "basic":
        token = request.cookies.get("promlens_session")
        user = verify_session_token(token, auth_cfg.secret, auth_cfg.session_days * 86400) if token else None
        if not user:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "Unauthorized", "detail": "Session expired or missing"}, status_code=401)
            return RedirectResponse("/login", status_code=302)
        request.state.user = user
        return await call_next(request)

    return await call_next(request)


_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    if request.method not in _CSRF_SAFE_METHODS and request.url.path.startswith("/api/"):
        if not request.url.path.startswith("/api/auth/"):
            origin = request.headers.get("origin")
            if origin:
                expected = request.headers.get("host", "")
                actual   = urlparse(origin).netloc
                if actual != expected:
                    logger.warning("CSRF check failed: Origin=%r Host=%r path=%s", origin, expected, request.url.path)
                    return JSONResponse({"error": "CSRF check failed"}, status_code=403)
    return await call_next(request)


def load_prometheus_config() -> PrometheusConfig:
    if not CONFIG_FILE.exists():
        raise HTTPException(status_code=503, detail="promlens.yaml not found -- create it to configure the Prometheus endpoint")
    try:
        data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except Exception as exc:
        logger.error("failed to parse promlens.yaml: %s", exc)
        raise HTTPException(status_code=500, detail="promlens.yaml parse error") from exc
    try:
        return build_config(data)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


_RATE_WINDOW = 60   # seconds
_RATE_MAX    = 60   # requests per window per IP
_rate_store: dict[str, deque] = {}


def _strip_url_credentials(url: str) -> str:
    p = urlparse(url)
    netloc = p.hostname or ''
    if p.port:
        netloc += f':{p.port}'
    return p._replace(netloc=netloc).geturl()


def _get_client_ip(request: Request) -> str:
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    fwd = request.headers.get("x-forwarded-for", "").strip()
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> bool:
    now = time.monotonic()
    window = _rate_store.get(ip)
    if window is not None:
        while window and window[0] < now - _RATE_WINDOW:
            window.popleft()
        if not window:
            del _rate_store[ip]
            window = None
    if window is not None and len(window) >= _RATE_MAX:
        return False
    if window is None:
        _rate_store[ip] = deque([now])
    else:
        window.append(now)
    return True


class QueryRequest(BaseModel):
    metric: str = Field(max_length=2000)
    mode: str = "instant"
    time: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    step: str = "60s"


@app.post("/api/query")
async def query_prometheus(req: QueryRequest, request: Request):
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded -- max 60 requests per minute")

    config = load_prometheus_config()

    logger.info("-> %s query | url=%s metric=%r ssl_verify=%s timeout=%s auth=%s proxy=%s",
                req.mode, _strip_url_credentials(config.url), req.metric, config.ssl_verify, config.timeout,
                "bearer" if config.token else ("basic" if config.username else "none"),
                config.proxy or "none")

    try:
        async with _build_session(config) as session:
            if req.mode == "range":
                if not req.start or not req.end:
                    raise HTTPException(status_code=422, detail="start and end are required for range queries")
                logger.debug("range params: start=%s end=%s step=%s", req.start, req.end, req.step)
                result = await query_range(
                    session,
                    config,
                    req.metric,
                    RangeParams(start=req.start, end=req.end, step=req.step),
                )
            else:
                result = await query_instant(session, config, req.metric, req.time)

        logger.info("<- %s result(s), type=%s", result.instances.__len__(), result.result_type)

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("query failed: %s\n%s", exc, traceback.format_exc())
        raise HTTPException(status_code=400, detail="Query failed") from exc

    return {
        "metric": result.metric,
        "result_type": result.result_type,
        "count": len(result.instances),
        "instances": [
            {
                "labels": inst.labels,
                "value": inst.value,
                "timestamp": inst.timestamp,
            }
            for inst in result.instances
        ],
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(LOGIN_HTML)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest):
    auth_cfg = load_app_auth_config()
    if auth_cfg.mode != "basic":
        raise HTTPException(status_code=400, detail="Login not available in this auth mode")
    if not auth_cfg.htpasswd_path or not auth_cfg.htpasswd_path.exists():
        raise HTTPException(status_code=503, detail="htpasswd file not found")
    if not verify_password(auth_cfg.htpasswd_path, req.username, req.password):
        logger.warning("Failed login attempt for user %r", req.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    logger.info("Successful login for user %r", req.username)
    token = make_session_token(req.username, auth_cfg.secret)
    response = JSONResponse({"ok": True, "user": req.username})
    response.set_cookie(
        "promlens_session",
        token,
        httponly=True,
        secure=SESSION_SECURE_COOKIE,
        samesite="lax",
        max_age=auth_cfg.session_days * 86400,
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("promlens_session", secure=SESSION_SECURE_COOKIE, samesite="lax")
    return response


@app.post("/api/reload")
async def reload_config():
    errors = []

    if CONFIG_FILE.exists():
        try:
            yaml.safe_load(CONFIG_FILE.read_text())
        except Exception as exc:
            errors.append(f"promlens.yaml: {exc}")
    else:
        errors.append("promlens.yaml: file not found")

    if TOPOLOGY_FILE.exists():
        try:
            yaml.safe_load(TOPOLOGY_FILE.read_text())
        except Exception as exc:
            errors.append(f"topology.yaml: {exc}")

    if errors:
        raise HTTPException(status_code=400, detail=" | ".join(errors))

    logger.info("config reloaded: %s, %s", CONFIG_FILE, TOPOLOGY_FILE)
    return {"ok": True}


@app.get("/api/alerts")
async def get_alerts(request: Request):
    client_ip = _get_client_ip(request)
    if not _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    try:
        config = load_prometheus_config()
        logger.info("alerts -> GET %s/api/v1/alerts",
                    _strip_url_credentials(config.url).rstrip("/"))
        async with asyncio.timeout(10):
            async with _build_session(config) as session:
                alerts = await query_alerts(session, config)
        logger.info("alerts <- %d alert(s)", len(alerts))
        return alerts
    except HTTPException:
        raise
    except Exception as exc:
        # str(exc), not exc: an exception object is always truthy, so a
        # message-less error (a bare TimeoutError) would log an empty reason.
        logger.warning("alerts fetch failed (returning empty): %s: %s",
                       type(exc).__name__, str(exc) or "(no message)")
        return []


# Blackbox module names queried for each probe role. Each role keeps its own
# rendering (icmp/ssh draw edges, tcp/http fill node tooltips), only the module
# names are configurable.
_BLACKBOX_DEFAULT_MODULES = {
    "icmp": ["icmp"],
    "ssh":  ["ssh_banner"],
    "tcp":  ["tcp_connect"],
    "http": ["http_2xx", "https_2xx"],
}

# Module names end up inside probe_success{module=~"..."}: restrict them to safe
# regex characters so a config entry cannot break out of the PromQL matcher.
_MODULE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:?*+|()\[\]-]+$")


def _blackbox_modules(section: dict) -> dict[str, list[str]]:
    """Merge blackbox.modules over the defaults. An empty list disables the role."""
    configured = section.get("modules") or {}
    if not isinstance(configured, dict):
        logger.warning("blackbox.modules must be a mapping - using defaults")
        configured = {}

    modules: dict[str, list[str]] = {}
    for role, default in _BLACKBOX_DEFAULT_MODULES.items():
        if role not in configured:
            modules[role] = list(default)
            continue
        raw = configured[role]
        names = raw if isinstance(raw, list) else [raw]
        valid = []
        for name in names:
            name = str(name).strip()
            if not name:
                continue
            if not _MODULE_NAME_RE.match(name):
                logger.warning("blackbox.modules.%s: ignoring invalid module name %r", role, name)
                continue
            valid.append(name)
        modules[role] = valid
    return modules


@app.get("/api/config")
async def get_config(request: Request):
    if not CONFIG_FILE.exists():
        return {"url": "", "configured": False, "auth_type": "none"}
    try:
        data = yaml.safe_load(CONFIG_FILE.read_text()) or {}
        url = data.get("url", "")
        auth = data.get("auth") or {}
        auth_type = (auth.get("type") or "none").lower()
        instance_label = data.get("instance_label") or "instance"
        # Guest -> parent attachment. Defaults reproduce the historical
        # behaviour (job="vm" + parent label) so existing setups do not move.
        parent_label = data.get("parent_label") or "parent"
        guest_label = data.get("guest_label") or "job"
        guest_values = data.get("guest_values") or ["vm"]
        if not isinstance(guest_values, list):
            guest_values = [guest_values]
        guest_values = [str(v) for v in guest_values if v]
        def _section(key):
            """Key absent -> None (disabled). Key present even if empty -> dict (enabled)."""
            if key not in data:
                return None
            v = data[key]
            return v if isinstance(v, dict) else {}
        blackbox = _section("blackbox")
        if blackbox is not None:
            blackbox = {**blackbox, "modules": _blackbox_modules(blackbox)}
        libvirt  = _section("libvirt")
        frigate  = _section("frigate")
        app_auth_mode = load_app_auth_config().mode
        app_auth_user = getattr(request.state, "user", None)
        refresh = int(data.get("refresh", 30))
        direct_credentials = bool(data.get("direct_credentials", False))
        # Same parsing as the notifier: the graph colors and the ProMLens
        # alerts must never disagree on a threshold.
        thresholds = parse_thresholds(data)
        return {
            "url": url, "configured": bool(url), "auth_type": auth_type,
            "instance_label": instance_label, "parent_label": parent_label,
            "guest_label": guest_label, "guest_values": guest_values,
            "blackbox": blackbox,
            "libvirt": libvirt, "frigate": frigate,
            "app_auth_mode": app_auth_mode, "app_auth_user": app_auth_user,
            "refresh": refresh, "direct_credentials": direct_credentials,
            "thresholds": thresholds.generic,
            "thresholds_by_node": thresholds.by_node,
            "threshold_colors": bool(data.get("threshold_colors", True)),
        }
    except Exception:
        return {"url": "", "configured": False, "auth_type": "none", "app_auth_mode": "none", "app_auth_user": None}


def _flatten_nodes(nodes: list, parent_id: str | None = None) -> list:
    result = []
    for node in nodes:
        n = {k: v for k, v in node.items() if k != "children"}
        if parent_id and "parent" not in n:
            n["parent"] = parent_id
        result.append(n)
        if "children" in node:
            result.extend(_flatten_nodes(node["children"], n["id"]))
    return result


@app.get("/api/topology")
async def get_topology():
    path = TOPOLOGY_FILE
    if not path.exists():
        logger.warning("topology.yaml not found -- returning empty topology")
        return {"nodes": [], "networks": []}
    try:
        data = yaml.safe_load(path.read_text()) or {}
        nodes = _flatten_nodes(data.get("nodes", []))
        logger.debug("topology loaded: %d nodes, %d networks",
                     len(nodes), len(data.get("networks", [])))
        return {"nodes": nodes, "networks": data.get("networks", []), "zones": data.get("zones", []), "tunnels": data.get("tunnels", []), "links": data.get("links", []), "cameras": data.get("cameras", [])}
    except Exception as exc:
        logger.error("failed to parse topology.yaml: %s", exc)
        raise HTTPException(status_code=500, detail="topology.yaml parse error") from exc


LAYOUT_FILE   = Path(os.environ.get("LAYOUT_FILE",   "layout.json"))
if LAYOUT_FILE.suffix != ".json":
    raise SystemExit(f"LAYOUT_FILE must have a .json extension, got: {LAYOUT_FILE}")
_MAX_LAYOUT_BYTES = 512 * 1024  # 512 KB


class _NodePos(BaseModel):
    x: float
    y: float

    @field_validator('x', 'y')
    @classmethod
    def must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError('must be a finite number')
        return v

class _LayoutData(RootModel[dict[str, Union[_NodePos, bool, float]]]):
    pass


@app.get("/api/layout")
async def get_layout():
    if not LAYOUT_FILE.exists():
        return {}
    try:
        return json.loads(LAYOUT_FILE.read_text())
    except Exception:
        return {}

@app.post("/api/layout")
async def save_layout(request: Request):
    body = await request.body()
    if len(body) > _MAX_LAYOUT_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    try:
        validated = _LayoutData.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    LAYOUT_FILE.write_text(json.dumps(validated.model_dump(), indent=2))
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
