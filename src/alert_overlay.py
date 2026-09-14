"""Full-screen overlay shown when a new alert appears.

The browser already fetches every alert on each refresh cycle (Prometheus
alerts plus the ones ProMLens derives from its own thresholds). It posts the
keys of the overlay-eligible alerts to /api/alert-state, and this module keeps
the date each key was *first* seen.

Why server side: the "is this alert new?" answer must be the same for every
browser and must survive a page reload, otherwise opening the dashboard would
replay a siren for an alert that has been firing for hours. The state is
persisted so a service restart does not replay them either.

A key absent from the posted set for `clear_after` seconds is dropped, so the
same alert firing again later counts as new again. The delay absorbs a single
failed poll, which would otherwise resolve and re-fire every alert.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Any

# Same YAML "scalar or list" semantics as the webhook config - no second dialect.
from notifier import _as_list

logger = logging.getLogger("promlens.overlay")

DEFAULT_DURATION = 10          # seconds the overlay stays on screen
DEFAULT_CLEAR_AFTER = 60       # seconds an alert must be gone before the state forgets it
DEFAULT_STATE_FILE = os.environ.get("ALERT_STATE_FILE", "/var/lib/promlens/alerts.state")

# Default scope: every blackbox probe failure plus the instance-down alerts.
# Patterns are fnmatch-style and matched case-insensitively on the alert name.
DEFAULT_ALERTS = ["*blackbox*", "*probefailed*", "instancedown"]

_MIN_DURATION = 2
_MAX_DURATION = 300
_MAX_KEYS = 2000               # hard cap on the state size, oldest entries dropped first
_ARM_BACKDATE = 86400          # seconds subtracted from first_seen when adopting a cold state


@dataclass
class OverlayConfig:
    """The `alert_overlay` section of promlens.yaml."""
    enabled: bool = False
    sound: bool = True
    volume: float = 0.7
    blasts: int = 2                # ship horn blasts played per overlay
    duration: int = DEFAULT_DURATION
    alerts: list[str] = field(default_factory=lambda: list(DEFAULT_ALERTS))
    severities: list[str] = field(default_factory=list)
    max_queue: int = 5             # overlays chained for one burst of alerts
    clear_after: int = DEFAULT_CLEAR_AFTER
    state_file: str = DEFAULT_STATE_FILE

    def as_dict(self) -> dict[str, Any]:
        """What the browser needs. The state path stays server side."""
        return {
            "enabled": self.enabled,
            "sound": self.sound,
            "volume": self.volume,
            "blasts": self.blasts,
            "duration": self.duration,
            "alerts": self.alerts,
            "severities": self.severities,
            "max_queue": self.max_queue,
            "clear_after": self.clear_after,
        }


def _clamp_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw)))
    except (TypeError, ValueError):
        return default


def parse_overlay_config(data: dict[str, Any]) -> OverlayConfig:
    """Build the overlay config from the parsed promlens.yaml mapping.

    Section absent -> disabled, like the blackbox/libvirt/frigate sections.
    """
    raw = data.get("alert_overlay")
    if raw is None:
        return OverlayConfig()
    if not isinstance(raw, dict):
        logger.warning("alert_overlay must be a mapping - overlay disabled")
        return OverlayConfig()

    patterns = [p for p in _as_list(raw.get("alerts")) if p.strip()] or list(DEFAULT_ALERTS)
    try:
        volume = min(1.0, max(0.0, float(raw.get("volume", 0.7))))
    except (TypeError, ValueError):
        volume = 0.7

    return OverlayConfig(
        enabled=bool(raw.get("enabled", True)),
        sound=bool(raw.get("sound", True)),
        volume=volume,
        blasts=_clamp_int(raw.get("blasts"), 2, 1, 10),
        duration=_clamp_int(raw.get("duration"), DEFAULT_DURATION, _MIN_DURATION, _MAX_DURATION),
        alerts=patterns,
        severities=[s.lower() for s in _as_list(raw.get("severities"))],
        max_queue=_clamp_int(raw.get("max_queue"), 5, 1, 50),
        clear_after=_clamp_int(raw.get("clear_after"), DEFAULT_CLEAR_AFTER, 0, 86400),
        state_file=str(raw.get("state_file") or DEFAULT_STATE_FILE),
    )


@dataclass
class _Entry:
    """When an alert key was first seen, and when it was last reported."""
    first: float
    last: float


class AlertState:
    """The 'already seen' alert keys, cached in memory and mirrored on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, _Entry] = {}
        self.loaded = False
        self.armed = False       # False until a cold state has been adopted silently
        self.persist = True      # dropped to False once the file proves unwritable

    def _load(self) -> None:
        """Read the state file once per path. A missing file means a cold start."""
        if self.loaded:
            return
        self.loaded = True
        now = time()
        try:
            raw = json.loads(self.path.read_text())
            alerts = raw.get("alerts") if isinstance(raw, dict) else None
            if not isinstance(alerts, dict):
                raise ValueError("missing 'alerts' mapping")
        except FileNotFoundError:
            logger.info("alert state %s not found - adopting the current alerts silently", self.path)
            return
        except (OSError, ValueError) as exc:
            logger.warning("alert state %s unreadable (%s) - starting from an empty state",
                           self.path, exc)
            return

        # `last` is restored to now: the file is only rewritten when the key set
        # changes, so its stored value may be older than clear_after and would
        # drop every alert on the first sync after a restart.
        self.entries = {
            str(key): _Entry(first=float(val.get("first", now)), last=now)
            for key, val in alerts.items()
            if isinstance(val, dict)
        }
        self.armed = True
        logger.info("alert state loaded from %s: %d known alert(s)", self.path, len(self.entries))

    def _save(self) -> None:
        """Atomic best-effort write. A read-only state path must not break the UI."""
        if not self.persist:
            return
        payload = {
            "alerts": {k: {"first": e.first, "last": e.last} for k, e in self.entries.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2))
            tmp.replace(self.path)
            self.path.chmod(0o640)
        except OSError as exc:
            logger.warning("alert state %s not writable (%s) - keeping it in memory only",
                           self.path, exc)
            self.persist = False
            tmp.unlink(missing_ok=True)

    def sync(self, keys: list[str], clear_after: int) -> dict[str, float]:
        """Register the currently firing keys and return their first-seen dates."""
        self._load()
        now = time()

        # A cold state adopts whatever is firing as old news, so enabling the
        # overlay does not siren through every alert already active.
        backdate = 0.0 if self.armed else _ARM_BACKDATE
        before = set(self.entries)

        for key in keys:
            entry = self.entries.get(key)
            if entry is None:
                self.entries[key] = _Entry(first=now - backdate, last=now)
            else:
                entry.last = now

        for key in [k for k, e in self.entries.items() if now - e.last > clear_after]:
            del self.entries[key]

        if len(self.entries) > _MAX_KEYS:
            logger.warning("alert state holds %d keys - trimming to %d", len(self.entries), _MAX_KEYS)
            kept = sorted(self.entries.items(), key=lambda kv: kv[1].last, reverse=True)[:_MAX_KEYS]
            self.entries = dict(kept)

        self.armed = True
        if set(self.entries) != before:
            self._save()
        return {k: e.first for k, e in self.entries.items()}


_state: AlertState | None = None


def get_state(path: str) -> AlertState:
    """Singleton state, rebuilt when the configured path changes."""
    global _state
    target = Path(path)
    if _state is None or _state.path != target:
        _state = AlertState(target)
    return _state
