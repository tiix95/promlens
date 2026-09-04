"""Alerts computed by ProMLens itself, without Prometheus alerting rules.

The webhook notifier used to forward the alerts of /api/v1/alerts, which only
exist when alerting rules are configured on the Prometheus side. Alerts are
now produced here from the raw node_exporter metrics, using the very same
thresholds that color the graph, so what turns a node red in the UI is what
triggers a notification.

Rules evaluated on every cycle:
  - NodeDown          : the node_exporter target is down (up == 0)
  - CpuHigh / MemoryHigh / DiskHigh / NetworkHigh : the metric reached its red
    threshold (per-node overrides included)
  - SystemdUnitFailed : a systemd unit is in the failed state

Alerts are emitted with the shape of a Prometheus /api/v1/alerts entry so the
notifier diff, filters and payload builder treat them exactly the same way.
"""

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientSession

from prometheus_query import PrometheusConfig, QueryResult, query_instant

logger = logging.getLogger("promlens.alerts")

DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    'cpu':     {'orange': 0.7, 'red': 0.9},
    'mem':     {'orange': 0.7, 'red': 0.9},
    'disk':    {'orange': 0.7, 'red': 0.9},
    'network': {'orange': 0.7, 'red': 0.9},
}

_LABEL_RE    = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_DEVICES     = 'device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"'
_ROOT_FS     = 'mountpoint="/",fstype!~"tmpfs|rootfs|overlay"'
_DEFAULT_CAP = 1e9   # assumed link capacity when node_network_speed_bytes is missing (as in the UI)
_SEVERITY    = "critical"   # only the red level notifies, so every alert is critical

# metric -> (alertname, wording used in the summary)
_METRIC_ALERTS = {
    'cpu':     ('CpuHigh',     'cpu'),
    'mem':     ('MemoryHigh',  'memory'),
    'disk':    ('DiskHigh',    'disk'),
    'network': ('NetworkHigh', 'network'),
}


def _parse_metric_thr(raw: dict | None, default: dict) -> dict:
    result = dict(default)
    if not isinstance(raw, dict):
        return result
    for level in ('orange', 'red'):
        try:
            v = float(raw[level])
            if 0.0 <= v <= 1.0:
                result[level] = v
        except (KeyError, TypeError, ValueError):
            pass
    if result['orange'] >= result['red']:
        return dict(default)
    return result


def _parse_group(raw: dict | None, base: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {m: _parse_metric_thr(raw.get(m), base[m]) for m in base}


@dataclass
class Thresholds:
    """Generic thresholds plus per-node overrides, keyed by topology ID."""
    generic: dict[str, dict[str, float]] = field(
        default_factory=lambda: {m: dict(v) for m, v in DEFAULT_THRESHOLDS.items()}
    )
    by_node: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)

    def red(self, instance: str, metric: str) -> float:
        """Red level of a metric for one instance.

        The UI keys the overrides by topology ID and matches the instance label
        both without and with its port, so resolve them the same way here.
        """
        host = instance.rsplit(":", 1)[0] if ":" in instance else instance
        node = self.by_node.get(host) or self.by_node.get(instance)
        return (node or self.generic)[metric]['red']


def parse_thresholds(data: dict[str, Any]) -> Thresholds:
    """Build the thresholds from the parsed promlens.yaml mapping."""
    generic = _parse_group(data.get('thresholds'), DEFAULT_THRESHOLDS)
    raw_by_node = data.get('thresholds_by_node')
    by_node = {
        str(node_id): _parse_group(node_raw, generic)
        for node_id, node_raw in (raw_by_node or {}).items()
    } if isinstance(raw_by_node, dict) else {}
    return Thresholds(generic=generic, by_node=by_node)


def _queries(label: str) -> dict[str, str]:
    """PromQL run on every cycle; the ratios are computed by Prometheus."""
    return {
        'up':      'up{exporter="node"}',
        'cpu':     f'1 - avg by({label})(rate(node_cpu_seconds_total{{mode="idle"}}[5m]))',
        'mem':     '1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes',
        'disk':    f'1 - node_filesystem_avail_bytes{{{_ROOT_FS}}} '
                   f'/ node_filesystem_size_bytes{{{_ROOT_FS}}}',
        'rx':      f'rate(node_network_receive_bytes_total{{{_DEVICES}}}[5m])',
        'tx':      f'rate(node_network_transmit_bytes_total{{{_DEVICES}}}[5m])',
        'speed':   f'node_network_speed_bytes{{{_DEVICES}}}',
        'systemd': 'node_systemd_unit_state{state="failed"} == 1',
    }


def _num(value: Any) -> float | None:
    """Prometheus returns values as strings; NaN and junk mean 'no data'."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(num) else num


def _by_instance(result: QueryResult, label: str) -> dict[str, float]:
    """Index a one-value-per-node query result by instance."""
    values: dict[str, float] = {}
    for inst in result.instances:
        instance = inst.labels.get(label)
        value    = _num(inst.value)
        if instance and value is not None:
            values[instance] = value
    return values


def _iface_load(metrics: dict[str, float]) -> float:
    """Load of one interface: its busiest direction over the link capacity."""
    speed = metrics.get('speed', 0.0)
    cap   = speed if speed > 0 else _DEFAULT_CAP
    return max(metrics.get('rx', 0.0), metrics.get('tx', 0.0)) / cap


def _network_ratios(results: dict[str, QueryResult], label: str) -> dict[str, float]:
    """Worst interface load per node."""
    devices: dict[str, dict[str, dict[str, float]]] = {}
    for kind in ('rx', 'tx', 'speed'):
        for inst in results[kind].instances:
            instance = inst.labels.get(label)
            device   = inst.labels.get('device')
            value    = _num(inst.value)
            if instance and device and value is not None:
                devices.setdefault(instance, {}).setdefault(device, {})[kind] = value

    return {
        instance: max(_iface_load(m) for m in devs.values())
        for instance, devs in devices.items() if devs
    }


def _failed_units(result: QueryResult, label: str) -> dict[str, list[str]]:
    """Failed systemd unit names per node."""
    units: dict[str, list[str]] = {}
    for inst in result.instances:
        instance = inst.labels.get(label)
        name     = inst.labels.get('name')
        if instance and name:
            units.setdefault(instance, []).append(name)
    return units


def _alert(name: str, label: str, instance: str, summary: str,
           extra: dict[str, str] | None = None) -> dict[str, Any]:
    """Build one alert with the shape of a Prometheus /api/v1/alerts entry."""
    labels = {"alertname": name, "severity": _SEVERITY, label: instance}
    labels.update(extra or {})
    return {"labels": labels, "annotations": {"summary": summary}, "state": "firing"}


def _pct(ratio: float) -> str:
    return f"{ratio * 100:.0f}%"


def _node_alerts(instance: str, label: str, ratios: dict[str, dict[str, float]],
                 thresholds: Thresholds, units: list[str]) -> list[dict[str, Any]]:
    """Threshold and systemd alerts of a single node that is up."""
    alerts = []
    for metric, (name, wording) in _METRIC_ALERTS.items():
        ratio = ratios[metric].get(instance)
        red   = thresholds.red(instance, metric)
        if ratio is not None and ratio >= red:
            alerts.append(_alert(name, label, instance,
                                 f"{wording} {_pct(ratio)} (threshold {_pct(red)})"))
    return alerts + [
        _alert('SystemdUnitFailed', label, instance,
               f"systemd unit {unit} is failed", {"unit": unit})
        for unit in sorted(units)
    ]


async def collect_alerts(
    session: ClientSession,
    config: PrometheusConfig,
    thresholds: Thresholds,
    instance_label: str,
) -> list[dict[str, Any]]:
    """Evaluate every rule and return the alerts currently firing."""
    label = instance_label if _LABEL_RE.match(instance_label) else "instance"
    if label != instance_label:
        logger.warning("instance_label %r is not a valid PromQL label - using 'instance'",
                       instance_label)

    queries = _queries(label)
    answers = await asyncio.gather(
        *(query_instant(session, config, expr) for expr in queries.values())
    )
    results = dict(zip(queries, answers))

    ratios = {metric: _by_instance(results[metric], label) for metric in ('cpu', 'mem', 'disk')}
    ratios['network'] = _network_ratios(results, label)
    failed = _failed_units(results['systemd'], label)

    nodes = _by_instance(results['up'], label)
    alerts: list[dict[str, Any]] = []
    for instance, state in sorted(nodes.items()):
        if state != 1:
            # The metrics of a down node are stale: report the node, nothing else.
            alerts.append(_alert('NodeDown', label, instance, "node is down (up == 0)"))
            continue
        alerts += _node_alerts(instance, label, ratios, thresholds, failed.get(instance, []))

    logger.debug("promlens alerts: %d firing on %d node(s)", len(alerts), len(nodes))
    return alerts
