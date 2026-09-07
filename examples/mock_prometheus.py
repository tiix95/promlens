#!/usr/bin/env python3
"""Mock Prometheus API serving demo data for the examples/data/ topology.

Answers the handful of PromQL queries ProMLens issues, plus /api/v1/alerts.
Only used to run a self-contained demo (documentation screenshots, UI work)
without a real Prometheus.

Usage:
    python3 examples/mock_prometheus.py [--port 9090]
    # then point promlens.yaml at http://127.0.0.1:9090
"""

import json
import time
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

GB = 1024 ** 3
NIC_1G = 125_000_000  # 1 Gbps expressed in bytes/s (node_network_speed_bytes)
NIC_100M = 12_500_000

# name -> demo state. cpu/mem/disk are used ratios, uptime is in days.
# parent is set for guests attached to their hypervisor (job="vm").
HOSTS = [
    # id            zone          parent  up  cpu   mem   disk  load  uptime  ram_gb  disk_gb
    ("rtr-edge",    None,         None,   1, 0.11, 0.34, 0.22, 0.42,  412,      2,      8),
    ("fw-dmz",      None,         None,   1, 0.18, 0.41, 0.29, 0.55,  240,      4,     16),
    ("hv01",        "zone-lan",   None,   1, 0.52, 0.68, 0.44, 6.10,  118,    256,   4000),
    ("hv02",        "zone-lan",   None,   1, 0.47, 0.61, 0.51, 5.30,  118,    256,   4000),
    ("srv01",       "zone-lan",   None,   1, 0.23, 0.44, 0.37, 0.88,   96,     32,    500),
    ("srv02",       "zone-lan",   None,   1, 0.31, 0.52, 0.41, 1.24,   96,     32,    500),
    ("srv03",       "zone-lan",   None,   1, 0.14, 0.29, 0.19, 0.36,   61,     16,    250),
    ("srv04",       "zone-lan",   None,   0, 0.00, 0.00, 0.00, 0.00,    0,     16,    250),
    ("srv05",       "zone-lan",   None,   1, 0.62, 0.58, 0.63, 2.40,   33,     64,   1000),
    ("srv06",       "zone-lan",   None,   1, 0.09, 0.22, 0.15, 0.21,  205,      8,    120),
    ("app01",       "zone-vm",    "hv01", 1, 0.38, 0.55, 0.33, 1.10,   72,     16,    100),
    ("app02",       "zone-vm",    "hv01", 1, 0.41, 0.49, 0.35, 1.35,   72,     16,    100),
    ("app03",       "zone-vm",    "hv01", 1, 0.93, 0.74, 0.38, 7.80,   72,     16,    100),
    ("app04",       "zone-vm",    "hv01", 1, 0.27, 0.46, 0.31, 0.74,   72,     16,    100),
    ("db01",        "zone-vm",    "hv01", 1, 0.44, 0.71, 0.88, 2.10,  151,     64,   2000),
    ("git",         "zone-vm",    "hv02", 1, 0.16, 0.38, 0.42, 0.48,  151,      8,    500),
    ("prometheus",  "zone-vm",    "hv02", 1, 0.35, 0.63, 0.57, 1.02,  151,     16,   1000),
    ("grafana",     "zone-vm",    "hv02", 1, 0.21, 0.47, 0.28, 0.66,  151,      8,    200),
    ("web01",       "zone-vm",    "hv02", 1, 0.29, 0.51, 0.34, 0.91,   88,     16,    200),
    ("vps01",       "zone-cloud", None,   1, 0.19, 0.43, 0.39, 0.52,  320,      4,     80),
    ("vps02",       "zone-cloud", None,   1, 0.33, 0.76, 0.48, 1.18,  320,      4,     80),
    ("vps03",       "zone-cloud", None,   1, 0.12, 0.31, 0.24, 0.30,  177,      2,     40),
]

# host -> device -> (rx bytes/s, tx bytes/s, speed bytes/s)
INTERFACES = {
    "rtr-edge": {
        "wan0":        (97_000_000, 24_000_000, NIC_1G),
        "eth-dmz":     (4_200_000, 6_800_000, NIC_1G),
        "eth-servers": (18_500_000, 12_300_000, NIC_1G),
        "eth-cameras": (31_400_000, 1_100_000, NIC_1G),
        "eth-zigbee":  (12_000, 8_400, NIC_100M),
        "eth-wifi":    (2_900_000, 5_600_000, NIC_100M),
        "eth-backup":  (11_000, 9_500, NIC_1G),
        "wg0":         (3_250_000, 1_140_000, 0),
    },
    "fw-dmz":  {"eth-dmz": (6_800_000, 4_200_000, NIC_1G)},
    "hv01":    {"eth0": (22_000_000, 17_400_000, NIC_1G),
                "vm-prod0": (41_000_000, 33_500_000, NIC_1G)},
    "hv02":    {"eth0": (19_600_000, 14_800_000, NIC_1G),
                "vm-prod0": (28_700_000, 25_100_000, NIC_1G)},
    "srv01":   {"eth0": (8_400_000, 6_100_000, NIC_1G), "wg0": (1_180_000, 3_240_000, 0)},
    "srv02":   {"eth0": (5_200_000, 3_800_000, NIC_1G), "wg0": (420_000, 910_000, 0)},
    "srv03":   {"eth0": (1_100_000, 900_000, NIC_1G)},
    "srv05":   {"eth0": (63_000_000, 48_000_000, NIC_1G)},
    "srv06":   {"eth0": (340_000, 260_000, NIC_100M)},
    "app01":   {"eth0": (3_100_000, 2_400_000, NIC_1G)},
    "app02":   {"eth0": (2_800_000, 2_200_000, NIC_1G)},
    "app03":   {"eth0": (9_600_000, 7_300_000, NIC_1G)},
    "app04":   {"eth0": (1_900_000, 1_500_000, NIC_1G)},
    "db01":    {"eth0": (14_200_000, 11_800_000, NIC_1G)},
    "git":     {"eth0": (700_000, 1_300_000, NIC_1G)},
    "prometheus": {"eth0": (12_400_000, 3_900_000, NIC_1G)},
    "grafana": {"eth0": (900_000, 2_100_000, NIC_1G)},
    "web01":   {"eth0": (16_800_000, 21_300_000, NIC_1G)},
    "vps01":   {"eth0": (2_200_000, 1_700_000, NIC_1G), "wg0": (880_000, 640_000, 0)},
    "vps02":   {"eth0": (4_900_000, 3_600_000, NIC_1G), "wg0": (1_420_000, 1_050_000, 0)},
    "vps03":   {"eth0": (600_000, 450_000, NIC_1G), "wg0": (210_000, 180_000, 0)},
}

# instance -> failed systemd units
FAILED_UNITS = {"grafana": ["backup-sync.service"], "srv05": ["logrotate.service"]}

# hypervisor -> [(domain, libvirt state code, description)]
# state codes: 1 running, 3 paused, 5 shut off
LIBVIRT = {
    "hv01": [("app01", 1, "running"), ("app02", 1, "running"), ("app03", 1, "running"),
             ("app04", 1, "running"), ("db01", 1, "running"), ("build01", 5, "shut off")],
    "hv02": [("git", 1, "running"), ("prometheus", 1, "running"), ("grafana", 1, "running"),
             ("web01", 1, "running"), ("staging01", 3, "paused")],
}

# camera -> fps (0 = offline)
CAMERAS = [("cam-lobby", 12.0), ("cam-entrance", 12.0), ("cam-parking", 8.0),
           ("cam-yard", 0.0), ("cam-corridor", 12.0)]

# ICMP/SSH probes: (source job, destination, module, success)
BB_PROBES = [
    ("srv01", "vps01", "icmp", 1), ("srv01", "vps02", "icmp", 1),
    ("srv01", "vps03", "icmp", 1), ("srv01", "srv04", "icmp", 0),
    ("srv01", "probe-dns", "icmp", 1),
    ("srv01", "vps01", "ssh_banner", 1), ("srv01", "srv04", "ssh_banner", 0),
]

# TCP connect probes: (instance, service, port, tool, success)
TCP_PROBES = [
    ("db01", "postgresql", "5432", "psql", 1),
    ("git", "ssh", "22", "ssh", 1),
    ("web01", "nginx", "443", "openssl", 0),
    ("probe-web", "www.example.com", "443", "openssl", 1),
    ("probe-api", "api.example.com", "8443", "openssl", 1),
]

# HTTP(S) probes: (upstream node, target url, success)
HTTP_PROBES = [
    ("web01", "https://www.example.com/", 1),
    ("grafana", "https://grafana.example.com/", 1),
    ("probe-vpn", "https://vpn.example.com/", 0),
]

ALERTS = [
    {
        "labels": {"alertname": "CpuHigh", "instance": "app03", "severity": "critical",
                   "job": "node"},
        "annotations": {"summary": "CPU usage above 90% for 10 minutes",
                        "description": "app03 CPU at 93%"},
        "state": "firing", "value": "93",
    },
    {
        "labels": {"alertname": "NodeDown", "instance": "srv04", "severity": "critical",
                   "job": "node"},
        "annotations": {"summary": "Host unreachable since 12 minutes"},
        "state": "firing", "value": "0",
    },
    {
        "labels": {"alertname": "DiskHigh", "instance": "db01", "severity": "warning",
                   "job": "node"},
        "annotations": {"summary": "Root filesystem 88% full"},
        "state": "firing", "value": "88",
    },
    {
        "labels": {"alertname": "SystemdUnitFailed", "instance": "grafana",
                   "severity": "warning", "job": "node", "name": "backup-sync.service"},
        "annotations": {"summary": "backup-sync.service is in failed state"},
        "state": "firing", "value": "1",
    },
    {
        "labels": {"alertname": "CameraOffline", "instance": "cam-yard", "severity": "info",
                   "job": "frigate"},
        "annotations": {"summary": "No frame received for 5 minutes"},
        "state": "firing", "value": "0",
    },
]


def _sample(labels: dict[str, str], value: float | int) -> dict:
    return {"metric": labels, "value": [time.time(), str(value)]}


def _base_labels(host: str, zone: str | None, parent: str | None) -> dict[str, str]:
    labels = {"instance": host, "job": "vm" if parent else "node", "exporter": "node"}
    if zone:
        labels["zone"] = zone
    if parent:
        labels["parent"] = parent
    return labels


def _host_samples(field: str) -> list[dict]:
    """Build the vector of a per-host node_exporter metric."""
    out = []
    now = time.time()
    for host, zone, parent, up, cpu, mem, disk, load, uptime, ram_gb, disk_gb in HOSTS:
        labels = _base_labels(host, zone, parent)
        if not up and field != "up":
            continue
        match field:
            case "up":
                out.append(_sample(labels, up))
            case "uname":
                out.append(_sample({**labels, "nodename": host, "sysname": "Linux",
                                    "release": "6.12.0-trixie-amd64"}, 1))
            case "cpu_idle":
                out.append(_sample({"instance": host}, round(1 - cpu, 4)))
            case "mem_total":
                out.append(_sample(labels, ram_gb * GB))
            case "mem_avail":
                out.append(_sample(labels, int(ram_gb * GB * (1 - mem))))
            case "disk_total":
                out.append(_sample({**labels, "mountpoint": "/", "fstype": "ext4",
                                    "device": "/dev/sda1"}, disk_gb * GB))
            case "disk_avail":
                out.append(_sample({**labels, "mountpoint": "/", "fstype": "ext4",
                                    "device": "/dev/sda1"}, int(disk_gb * GB * (1 - disk))))
            case "load1":
                out.append(_sample(labels, load))
            case "boot_time":
                out.append(_sample(labels, int(now - uptime * 86400)))
    return out


def _net_samples(field: str) -> list[dict]:
    """Build the vector of a per-interface network metric (rx, tx or speed)."""
    idx = {"rx": 0, "tx": 1, "speed": 2}[field]
    out = []
    for host, devices in INTERFACES.items():
        for device, values in devices.items():
            if field == "speed" and not values[2]:
                continue  # WireGuard interfaces have no link speed
            out.append(_sample({"instance": host, "job": "node", "device": device},
                               values[idx]))
    return out


def _systemd_samples() -> list[dict]:
    return [_sample({"instance": host, "job": "node", "name": unit, "state": "failed"}, 1)
            for host, units in FAILED_UNITS.items() for unit in units]


def _libvirt_samples() -> list[dict]:
    return [_sample({"instance": f"{hv}:9177", "job": "libvirt", "domain": domain,
                     "domainname": domain, "state_desc": desc}, state)
            for hv, domains in LIBVIRT.items() for domain, state, desc in domains]


def _frigate_samples() -> list[dict]:
    return [_sample({"instance": "frigate:9101", "job": "frigate", "camera_name": name,
                     "parent": "sw-cam"}, fps)
            for name, fps in CAMERAS]


def _bb_samples(module: str) -> list[dict]:
    return [_sample({"dest": dest, "job": src, "module": mod, "instance": "blackbox:9115"},
                    ok)
            for src, dest, mod, ok in BB_PROBES if mod == module]


def _tcp_samples() -> list[dict]:
    return [_sample({"instance": inst, "module": "tcp_connect", "service": service,
                     "port": port, "tool": tool}, ok)
            for inst, service, port, tool, ok in TCP_PROBES]


def _http_samples() -> list[dict]:
    return [_sample({"instance": target, "module": "https_2xx", "upstream": upstream}, ok)
            for upstream, target, ok in HTTP_PROBES]


QUERIES = {
    'up{exporter="node"}': lambda: _host_samples("up"),
    'rate(node_network_receive_bytes_total{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}[5m])':
        lambda: _net_samples("rx"),
    'rate(node_network_transmit_bytes_total'
    '{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}[5m])':
        lambda: _net_samples("tx"),
    'node_network_speed_bytes{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}':
        lambda: _net_samples("speed"),
    'node_uname_info': lambda: _host_samples("uname"),
    'avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))':
        lambda: _host_samples("cpu_idle"),
    'node_memory_MemAvailable_bytes': lambda: _host_samples("mem_avail"),
    'node_memory_MemTotal_bytes': lambda: _host_samples("mem_total"),
    'node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|rootfs|overlay"}':
        lambda: _host_samples("disk_avail"),
    'node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|rootfs|overlay"}':
        lambda: _host_samples("disk_total"),
    'node_load1': lambda: _host_samples("load1"),
    'node_boot_time_seconds': lambda: _host_samples("boot_time"),
    'node_systemd_unit_state{state="failed"} == 1': _systemd_samples,
    # Built from blackbox.modules, so the selectors follow the default module names
    'probe_success{module=~"icmp"}': lambda: _bb_samples("icmp"),
    'probe_success{module=~"ssh_banner"}': lambda: _bb_samples("ssh_banner"),
    'probe_success{module=~"tcp_connect"}': _tcp_samples,
    'probe_success{module=~"http_2xx|https_2xx"}': _http_samples,
    'libvirt_domain_info_state': _libvirt_samples,
    'frigate_camera_fps': _frigate_samples,
}


class Handler(BaseHTTPRequestHandler):
    """Serve the two Prometheus API endpoints ProMLens calls."""

    protocol_version = "HTTP/1.1"

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """Route /api/v1/query and /api/v1/alerts, 404 on anything else."""
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/alerts":
            self._send({"status": "success", "data": {"alerts": ALERTS}})
            return
        if parsed.path == "/api/v1/query":
            query = parse_qs(parsed.query).get("query", [""])[0]
            builder = QUERIES.get(query)
            result = builder() if builder else []
            self._send({"status": "success",
                        "data": {"resultType": "vector", "result": result}})
            return
        self.send_error(404)

    def log_message(self, format: str, *args) -> None:  # pylint: disable=redefined-builtin
        """Silence the per-request logging, the demo output stays readable."""


def main() -> None:
    """Parse the CLI arguments and serve until interrupted."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9090)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Mock Prometheus listening on http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
