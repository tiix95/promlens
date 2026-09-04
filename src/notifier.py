"""Webhook notifications for the alerts ProMLens computes itself.

Polls the node_exporter metrics in the background, evaluates the ProMLens
rules on them (see local_alerts), diffs the firing set against the previous
cycle and posts a notification to every configured webhook target when an
alert appears (firing) or disappears (resolved).

Nothing here depends on Prometheus alerting rules: the notified alerts are
the ones ProMLens derives from its own thresholds, so a deployment without
alerting rules or Alertmanager still gets notifications.
"""

import asyncio
import logging
import os
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml
from aiohttp import ClientSession, ClientTimeout, TCPConnector

from local_alerts import Thresholds, collect_alerts, parse_thresholds
from prometheus_query import PrometheusConfig, _build_session, build_config

logger = logging.getLogger("promlens.notifier")

_CONFIG_FILE = Path(os.environ.get("CONFIG_FILE", "promlens.yaml"))

DEFAULT_INTERVAL = 60   # seconds between two metric polls
_MIN_INTERVAL    = 10
_FETCH_MARGIN    = 5    # seconds added to the configured timeout as a hang guard
_DEFAULT_FOR     = 2    # consecutive cycles a condition must hold before notifying


@dataclass
class WebhookTarget:
    """A single webhook endpoint and the static fields sent with it."""
    url: str
    topic: str = ""
    recipients: list[str] = field(default_factory=list)
    module: str = "promlens"
    link: str = ""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class NotifierConfig:
    enabled: bool = False
    interval: int = DEFAULT_INTERVAL
    notify_resolved: bool = True
    link: str = ""
    disabled: list[str] = field(default_factory=list)
    severities: list[str] = field(default_factory=list)
    targets: list[WebhookTarget] = field(default_factory=list)
    timeout: int = 10
    ssl_verify: bool = True
    ca_file: str | None = None
    proxy: str | None = None
    for_cycles: int = _DEFAULT_FOR


def _as_list(value: Any) -> list[str]:
    """Accept either a scalar or a list in the YAML config."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _expand(value: Any) -> str:
    """Expand $VAR / ${VAR} so secrets can live in environment variables."""
    return os.path.expandvars(str(value or ""))


def _parse_target(raw: dict[str, Any], default_link: str) -> WebhookTarget:
    return WebhookTarget(
        url=_expand(raw["url"]),
        topic=_expand(raw.get("topic")),
        recipients=[_expand(r) for r in _as_list(raw.get("recipients"))],
        module=_expand(raw.get("module")) or "promlens",
        link=_expand(raw.get("link")) or default_link,
        headers={str(k): _expand(v) for k, v in (raw.get("headers") or {}).items()},
    )


def parse_notifier_config(data: dict[str, Any]) -> NotifierConfig:
    """Build the notifier config from the parsed promlens.yaml mapping."""
    raw = data.get("webhooks")
    if not isinstance(raw, dict):
        return NotifierConfig()

    default_link = _expand(raw.get("link"))
    targets = [
        _parse_target(t, default_link)
        for t in raw.get("targets") or []
        if isinstance(t, dict) and t.get("url")
    ]
    enabled = bool(raw.get("enabled", True))
    if enabled and not targets:
        logger.warning("webhooks enabled but no valid target defined - notifications disabled")

    return NotifierConfig(
        enabled=enabled and bool(targets),
        interval=max(_MIN_INTERVAL, int(raw.get("interval", DEFAULT_INTERVAL))),
        notify_resolved=bool(raw.get("notify_resolved", True)),
        link=default_link,
        disabled=_as_list(raw.get("disabled")),
        severities=[s.lower() for s in _as_list(raw.get("severities"))],
        targets=targets,
        timeout=int(raw.get("timeout", 10)),
        ssl_verify=bool(raw.get("ssl_verify", True)),
        ca_file=_expand(raw.get("ca_file")) or None,
        proxy=raw.get("proxy") or None,
        for_cycles=max(1, int(raw.get("for_cycles", _DEFAULT_FOR))),
    )


def build_ssl_context(config: NotifierConfig) -> ssl.SSLContext | bool:
    """TLS settings of the webhook POST: custom CA bundle, or default verification.

    An unusable ca_file falls back to the default CA store instead of
    disabling verification: a misconfigured path must not weaken TLS.
    """
    if not config.ssl_verify:
        return False
    if not config.ca_file:
        return True
    try:
        return ssl.create_default_context(cafile=config.ca_file)
    except OSError as exc:
        logger.warning("webhook ca_file %s unusable (%s) - falling back to system CAs",
                       config.ca_file, exc)
        return True


def alert_key(alert: dict[str, Any]) -> str:
    """Stable identity of an alert instance: all its labels, sorted."""
    labels = alert.get("labels") or {}
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def is_notifiable(alert: dict[str, Any], config: NotifierConfig) -> bool:
    """Filter out alerts disabled by name pattern or by severity."""
    labels = alert.get("labels") or {}
    name = str(labels.get("alertname", "unknown"))
    if any(fnmatch(name, pattern) for pattern in config.disabled):
        return False
    if config.severities:
        return str(labels.get("severity", "")).lower() in config.severities
    return True


def build_payload(
    alert: dict[str, Any],
    event: str,
    target: WebhookTarget,
    instance_label: str,
) -> dict[str, Any]:
    """Build the webhook body: title, message, url, topic, recipients, module."""
    labels      = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    name = str(labels.get("alertname", "unknown"))
    node = str(labels.get(instance_label) or labels.get("instance") or "")

    title = f"[{event.upper()}] {name}" + (f" - {node}" if node else "")

    summary = annotations.get("summary") or annotations.get("description") or ""
    lines = [str(summary)] if summary else []
    if labels.get("severity"):
        lines.append(f"severity: {labels['severity']}")
    if node:
        lines.append(f"{instance_label}: {node}")
    if alert.get("activeAt"):
        lines.append(f"since: {alert['activeAt']}")

    return {
        "title": title,
        "message": "\n".join(lines) or f"{name} is {event}",
        "url": target.link or str(alert.get("generatorURL") or ""),
        "topic": target.topic,
        "recipients": target.recipients,
        "module": target.module,
    }


async def _post(
    session: ClientSession,
    config: NotifierConfig,
    target: WebhookTarget,
    payload: dict[str, Any],
) -> None:
    """POST one notification; failures are logged, never raised."""
    try:
        async with session.post(
            target.url, json=payload, headers=target.headers, proxy=config.proxy
        ) as resp:
            body = await resp.text()
            if resp.status >= 400:
                logger.warning("webhook %s <- HTTP %s: %s", target.url, resp.status, body[:200])
            else:
                logger.info("webhook %s <- HTTP %s | %s", target.url, resp.status, payload["title"])
    except Exception as exc:  # noqa: BLE001 - a broken webhook must not stop the loop
        logger.warning("webhook %s failed: %s: %s", target.url, type(exc).__name__, exc)


async def send_events(
    config: NotifierConfig,
    events: list[tuple[dict[str, Any], str]],
    instance_label: str,
) -> None:
    """Send every (alert, event) pair to every configured target."""
    connector = TCPConnector(ssl=build_ssl_context(config))
    timeout   = ClientTimeout(total=config.timeout)
    async with ClientSession(connector=connector, timeout=timeout) as session:
        for alert, event in events:
            for target in config.targets:
                await _post(session, config, target, build_payload(alert, event, target, instance_label))


@dataclass
class Settings:
    """Everything the notifier re-reads from promlens.yaml on every cycle."""
    prometheus: PrometheusConfig | None
    notifier: NotifierConfig
    instance_label: str
    thresholds: Thresholds


def _load_settings() -> Settings:
    """Re-read promlens.yaml so config changes apply without a restart."""
    if not _CONFIG_FILE.exists():
        return Settings(None, NotifierConfig(), "instance", Thresholds())
    data = yaml.safe_load(_CONFIG_FILE.read_text()) or {}
    try:
        prom_cfg = build_config(data)
    except ValueError:
        prom_cfg = None
    return Settings(
        prometheus=prom_cfg,
        notifier=parse_notifier_config(data),
        instance_label=str(data.get("instance_label") or "instance"),
        thresholds=parse_thresholds(data),
    )


@dataclass
class _Pending:
    """A condition seen but not confirmed yet: how many cycles, and since when."""
    count: int
    since: str


@dataclass
class NotifierState:
    """Alerts already notified, plus the candidates waiting for confirmation."""
    known: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending: dict[str, _Pending] = field(default_factory=dict)
    armed: bool = False

    def reset(self) -> None:
        """Forget everything, so a re-enabled notifier re-arms silently."""
        self.known.clear()
        self.pending.clear()
        self.armed = False


def _confirm(state: NotifierState, candidates: dict[str, dict[str, Any]],
             for_cycles: int, now: str) -> dict[str, dict[str, Any]]:
    """Keep only the candidates seen on `for_cycles` consecutive polls.

    A metric brushing against its threshold for a single cycle would otherwise
    produce a firing/resolved pair on every poll.
    """
    for key in [k for k in state.pending if k not in candidates]:
        del state.pending[key]

    firing = {}
    for key, alert in candidates.items():
        entry = state.pending.setdefault(key, _Pending(count=0, since=now))
        entry.count += 1
        if entry.count >= for_cycles:
            firing[key] = {**alert, "activeAt": entry.since}
    return firing


async def _poll_once(prom_config: PrometheusConfig, settings: Settings,
                     state: NotifierState) -> None:
    """Evaluate the ProMLens rules, diff against the previous cycle and notify."""
    config = settings.notifier
    # Both the aiohttp total timeout and the outer hang guard raise a bare
    # TimeoutError, so add the context needed to act on it in the logs.
    try:
        async with asyncio.timeout(prom_config.timeout + _FETCH_MARGIN):
            async with _build_session(prom_config) as session:
                alerts = await collect_alerts(
                    session, prom_config, settings.thresholds, settings.instance_label
                )
    except TimeoutError as exc:
        raise TimeoutError(
            f"no answer from {prom_config.url} within {prom_config.timeout}s"
        ) from exc

    now = datetime.now(timezone.utc).isoformat()
    candidates = {alert_key(a): a for a in alerts}

    if not state.armed:
        # First cycle: adopt the current state without notifying to avoid a
        # burst of notifications for conditions that were already active. The
        # counters start confirmed so an alert still there is not re-notified.
        state.pending = {k: _Pending(count=config.for_cycles, since=now) for k in candidates}
        state.known = {k: {**a, "activeAt": now} for k, a in candidates.items()}
        state.armed = True
        logger.info("webhook notifier armed on %d active alert(s), %d target(s)",
                    len(candidates), len(config.targets))
        return

    firing = _confirm(state, candidates, config.for_cycles, now)

    events = [(a, "firing") for k, a in firing.items() if k not in state.known]
    if config.notify_resolved:
        events += [(a, "resolved") for k, a in state.known.items() if k not in firing]

    state.known = dict(firing)

    events = [(a, e) for a, e in events if is_notifiable(a, config)]
    if events:
        logger.info("webhook notifier: %d event(s) to send", len(events))
        await send_events(config, events, settings.instance_label)


async def run_notifier() -> None:
    """Background task: evaluate the ProMLens alerts and fire webhooks forever."""
    state = NotifierState()
    was_enabled: bool | None = None

    while True:
        interval = DEFAULT_INTERVAL
        try:
            settings = _load_settings()
            prom_config = settings.prometheus
            interval = settings.notifier.interval
            enabled = settings.notifier.enabled and prom_config is not None
            if enabled != was_enabled:
                logger.info("webhook notifier %s", "enabled" if enabled else "disabled")
                was_enabled = enabled
            if prom_config is None or not settings.notifier.enabled:
                state.reset()
            else:
                await _poll_once(prom_config, settings, state)
        except Exception as exc:  # noqa: BLE001 - keep polling whatever happens
            # CancelledError derives from BaseException and is not caught here,
            # so shutdown still interrupts the loop.
            # str(exc), not exc: an exception object is always truthy, so a
            # message-less error would otherwise log an empty reason.
            logger.warning("webhook notifier cycle failed (metrics poll): %s: %s",
                           type(exc).__name__, str(exc) or "(no message)")
        await asyncio.sleep(interval)
