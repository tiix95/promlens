# ProMLens — Technical Reference

**English** | [Francais](DOC.fr.md)

Version: **0.18.1**

---

## 1. Overview

ProMLens is a FastAPI + Vis.js web application. It combines a static topology declaration (`topology.yaml`) with live Prometheus metrics to render an interactive network graph in the browser.

### Architecture

| Concern | Where | Why |
|---|---|---|
| Prometheus query proxy | Backend (all modes except `cert`) | Hides credentials from the browser, avoids CORS issues |
| Prometheus direct queries | Browser (mode `cert` only) | Client certificate is stored in the browser; mTLS handshake requires browser involvement |
| topology.yaml parsing | Backend | Server-side file, not accessible to the browser directly |
| Layout persistence | Backend (`layout.json`) + localStorage | Survives page reloads, shared across browser sessions |
| Graph construction (buildGraph) | Frontend (JavaScript) | Pure computation on already-fetched data; no extra round-trips |
| Vis.js rendering | Frontend | DOM/Canvas, must run in the browser |
| Threshold colors | Frontend | Computed from the metrics already fetched for the graph |
| ProMLens alert evaluation (`local_alerts.py`) | Backend (background task) | Same thresholds as the graph, but must keep evaluating when no browser is open |
| Alert webhook notifications (`notifier.py`) | Backend (background task) | Must keep firing when no browser is open |

---

## 2. Installation

### Debian package

```bash
# Build from source
make deb

# Install
dpkg -i promlens_*.deb

# Systemd service is enabled automatically
systemctl status promlens
```

File layout after installation:

| Path | Contents |
|---|---|
| `/usr/lib/promlens/` | Application source files |
| `/etc/promlens/promlens.yaml` | Prometheus connection config |
| `/etc/promlens/topology.yaml` | Network topology declaration |
| `/var/lib/promlens/layout.json` | Saved node positions (written at runtime) |
| `/etc/default/promlens` | Environment variable overrides |

### Container

```bash
make build   # build the image
make run     # build + run, mounts examples/data/ at /data
```

The container reads config from `/data/promlens.yaml` and `/data/topology.yaml` by default.
Override with environment variables (see section 8).

---

## 3. Configuration — promlens.yaml

This file is read on every request. Editing it takes effect immediately without a server restart (use the RELOAD button in the UI or `POST /api/reload` to force a graph refresh).

### Prometheus connection

```yaml
url: https://prometheus.example.com:9090

auth:
  type: none          # none | basic | bearer | cert
  username: admin     # basic only
  password: secret    # basic only
  token: mytoken      # bearer only

instance_label: instance   # Prometheus label used as node ID (default: instance)
ssl_verify: true           # set false to skip TLS verification (not recommended)
timeout: 30                # HTTP timeout in seconds
proxy: http://proxy:8080   # optional HTTP proxy
```

### Top-level options

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Prometheus base URL |
| `instance_label` | string | `instance` | Label used to identify each node |
| `ssl_verify` | bool | `true` | TLS certificate verification |
| `timeout` | int | `30` | HTTP request timeout in seconds |
| `proxy` | string | none | HTTP proxy URL |
| `refresh` | int | `30` | Default auto-refresh interval shown in the UI (seconds) |
| `direct_credentials` | bool | `false` | cert mode only: browser sends client cert in direct Prometheus requests |

### app_auth section

Controls access to the ProMLens UI itself, independently of Prometheus authentication.

```yaml
app_auth:
  mode: basic                              # none | basic | cert
  htpasswd: /etc/promlens/.htpasswd        # required for mode basic
  secret: "random-string-change-me"        # signs session cookies
  session_days: 7                          # session lifetime
```

| Field | Type | Default | Description |
|---|---|---|---|
| `mode` | string | `none` | Authentication mode |
| `htpasswd` | path | none | Path to Apache htpasswd file (basic mode) |
| `secret` | string | none | HMAC signing key for session cookies. Must be set in non-none mode |
| `session_days` | int | `7` | Session cookie lifetime in days |

**Create an htpasswd file:**

```bash
# Create file with first user (bcrypt recommended)
htpasswd -cB /etc/promlens/.htpasswd alice

# Add a user
htpasswd -B /etc/promlens/.htpasswd bob

# Remove a user
htpasswd -D /etc/promlens/.htpasswd alice
```

Supported formats: bcrypt (`$2y$`), Apache MD5 (`$apr1$`), SHA1 (`{SHA}`).

### libvirt section

Adds VM state to hypervisor node tooltips **and creates VM nodes on the map** for VMs that do not have their own node_exporter instance.

VMs already present on the map (via topology.yaml or node_exporter) are not duplicated. Auto-created VM nodes are connected to their hypervisor with a dashed edge and colored by libvirt state (green=running, orange=paused/suspended, red=shut off/crashed).

```yaml
libvirt:
  enabled: true                  # set false to disable without removing the section
  instance_label: instance       # label identifying the hypervisor (default: global instance_label)
  metric: libvirt_domain_info_state  # PromQL metric to query (default: libvirt_domain_info_state)
```

Remove the section entirely or set `enabled: false` to disable.

### blackbox section

Enables blackbox exporter probe visualization. Supported modules: `icmp`, `ssh_banner`, `tcp_connect`, `https?_2xx`.

```yaml
blackbox:
  enabled: true
  destination_label: instance    # label identifying the probe target (default: instance)
  source_label: job              # label identifying the probe source; if absent, shows as "prometheus"
  http_node_label: upstream      # label for HTTP/HTTPS probes to identify the node (default: upstream)
  dest_aliases:
    mynode:                      # topology node ID
      - alias-one                # Prometheus label values that map to mynode
      - alias-two
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable without removing the section |
| `destination_label` | string | `instance` | Label that holds the probe target identifier |
| `source_label` | string | none | Label that holds the probe source identifier |
| `http_node_label` | string | `upstream` | Label identifying the node for HTTP/HTTPS probes |
| `dest_aliases` | map | `{}` | Maps topology node IDs to lists of probe target aliases |

### frigate section

Adds one node per Frigate NVR camera using the `frigate_camera_fps` metric. `fps > 0` = online (green); `fps = 0` = offline (red). Clicking a camera node opens `camera_url/#camera_name`.

```yaml
frigate:
  enabled: true
  camera_url: https://frigate.example.com
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Enable/disable without removing the section |
| `camera_url` | string | none | Base URL of the Frigate web UI |

Camera parent resolution order: `cameras` section override in topology.yaml > `parent` label in `frigate_camera_fps` > fallback to first router or switch.

### thresholds section

Defines the metric ratios at which node and link colors switch from green to orange, then orange to red. Both sections are optional. The default is `orange: 0.70`, `red: 0.90` for all metrics.

The `red` level is also what triggers a webhook notification (see [webhooks](#webhooks-section)), so raising it for a node silences both its red color and its notifications.

Supported metrics: `cpu`, `mem`, `disk`, `network`.

Both values must be in `[0.0, 1.0]` and `orange` must be strictly less than `red`.

```yaml
thresholds:
  cpu:     { orange: 0.70, red: 0.90 }
  mem:     { orange: 0.70, red: 0.90 }
  disk:    { orange: 0.80, red: 0.95 }
  network: { orange: 0.60, red: 0.85 }
```

Only the metrics you list are overridden. Omitted metrics keep the default values.

### thresholds_by_node section

Overrides thresholds for individual nodes. The key is the topology ID (the `id` field in `topology.yaml`). Only the listed metrics are overridden for that node; all others fall back to the generic `thresholds` values (or the built-in defaults).

```yaml
thresholds_by_node:
  hv01:
    cpu:  { orange: 0.85, red: 0.95 }
  db01:
    disk: { orange: 0.80, red: 0.95 }
```

In the example above, `hv01` uses a higher CPU threshold (expected sustained load), while `db01` uses a tighter disk threshold. All other metrics on both nodes use the values from `thresholds` or the defaults.

### threshold_colors option

Controls whether CPU/memory/disk/network threshold colors are applied to nodes and edges. Default is `true`.

```yaml
threshold_colors: false
```

When set to `false`:

- Nodes that are UP and have no probe failures use their type-specific neutral color (topology nodes keep their icon color; pure Prometheus server nodes use `#c9d1e8`).
- Edge network bandwidth threshold colors (orange/red) are removed. Edges show green (link up) or red (link down) only.
- WireGuard tunnel threshold coloring is also disabled.

The following colors remain active regardless of this setting:

| Condition | Color |
|---|---|
| Node down | red `#f04f4f` |
| Probe failure (blackbox ICMP/TCP/HTTP) | orange `#e0972a` |
| Prometheus alert overlay | alert severity color |

Set this to `false` when threshold noise is not useful (e.g. a display-only board) and you want a cleaner baseline view.

### webhooks section

Sends a webhook notification when a **ProMLens alert** starts or clears. These alerts are computed by ProMLens itself from the raw node_exporter metrics: no Prometheus alerting rule and no Alertmanager are required. The rules are listed in [ProMLens alerts](#promlens-alerts) below.

Notifications are produced server-side by a background task, so they do not require a browser to be open.

The Prometheus alerts shown in the UI (alerts panel, node overlays, alerts page) come from `/api/v1/alerts` and are a **separate** feed: they are displayed but never notified. Conversely, ProMLens alerts are notified but not displayed in the graph — they only drive the node colors they are derived from.

```yaml
webhooks:
  enabled: true                          # remove the section or set false to disable
  interval: 60                           # seconds between two polls (default: 60, minimum: 10)
  for_cycles: 2                          # consecutive cycles before notifying (default: 2, minimum: 1)
  notify_resolved: true                  # also notify when an alert clears (default: true)
  link: https://promlens.example.com/    # value of the payload "url" field
  severities: [critical]                 # only notify these severities (default: all)
  disabled:                              # alertname glob patterns never notified
    - SystemdUnitFailed
    - "*High"
  timeout: 10                            # HTTP timeout of the webhook POST (default: 10)
  ssl_verify: true                       # TLS verification of the webhook endpoint (default: true)
  ca_file: /etc/promlens/webhook-ca.crt  # CA bundle used to verify the webhook endpoint
  proxy: http://proxy:8080               # optional HTTP proxy for the webhook POST
  targets:
    - url: https://notify.example.com/api/notify
      topic: infra
      recipients: [ops, oncall]
      module: promlens
      link: https://promlens.example.com/     # optional per-target override of the global link
      headers:
        Authorization: "Bearer ${NOTIFY_TOKEN}"
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` when the section exists | Master switch; also forced off when no valid target is defined |
| `interval` | int | `60` | Poll interval in seconds, clamped to a 10 s minimum |
| `for_cycles` | int | `2` | Consecutive polls a condition must hold before it is notified, clamped to a 1 minimum |
| `notify_resolved` | bool | `true` | Send a `resolved` notification when a firing alert clears |
| `link` | string | none | Default value of the payload `url` field |
| `severities` | list | all | Only alerts whose `severity` label is in this list are notified |
| `disabled` | list | empty | Glob patterns matched against `alertname` (fnmatch syntax) |
| `timeout` | int | `10` | Total HTTP timeout of each webhook POST |
| `ssl_verify` | bool | `true` | TLS verification for the webhook endpoint |
| `ca_file` | string | none | Path to a PEM CA bundle used to verify the webhook endpoint instead of the system CA store |
| `proxy` | string | none | HTTP proxy used for the webhook POST |
| `targets[].url` | string | required | Webhook endpoint; a target without `url` is ignored |
| `targets[].topic` | string | `""` | Payload `topic` field |
| `targets[].recipients` | list or string | empty | Payload `recipients` field (a scalar is wrapped into a list) |
| `targets[].module` | string | `promlens` | Payload `module` field |
| `targets[].link` | string | global `link` | Payload `url` field for this target |
| `targets[].headers` | map | empty | Extra HTTP headers sent with the POST |

`ca_file` covers every target: it replaces the system CA store when verifying the TLS certificate of the webhook endpoints, which is what internal PKIs need. It is ignored when `ssl_verify: false`, and an unreadable or malformed file falls back to the system CA store with a warning rather than silently disabling verification. In a container, the file must be mounted and the path must be the one seen inside the container (for example under `/data`).

`$VAR` and `${VAR}` are expanded from the process environment in `ca_file` and in `targets[].url`, `topic`, `recipients`, `module`, `link` and every header value. Keep tokens in `/etc/default/promlens` rather than in `promlens.yaml`.

#### ProMLens alerts

Every cycle, the notifier runs eight instant queries and evaluates the following rules on their result. All of them use the `instance_label` label to identify a node.

| `alertname` | Condition | Extra label | Summary |
|---|---|---|---|
| `NodeDown` | `up{exporter="node"} != 1` | none | `node is down (up == 0)` |
| `CpuHigh` | `1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))` >= `cpu` red threshold | none | `cpu 93% (threshold 90%)` |
| `MemoryHigh` | `1 - MemAvailable / MemTotal` >= `mem` red threshold | none | `memory 93% (threshold 90%)` |
| `DiskHigh` | `1 - avail / size` on `mountpoint="/"` >= `disk` red threshold | none | `disk 93% (threshold 90%)` |
| `NetworkHigh` | busiest interface, `max(rx, tx) / node_network_speed_bytes` >= `network` red threshold | none | `network 93% (threshold 90%)` |
| `SystemdUnitFailed` | `node_systemd_unit_state{state="failed"} == 1` | `unit` | `systemd unit borg.service is failed` |

Notes:

- Every alert carries `severity: critical`. Only the **red** threshold notifies; the orange level stays a UI-only signal, so a node turning orange in the graph does not wake anyone up.
- The thresholds are the ones from the [thresholds](#thresholds-section) and [thresholds_by_node](#thresholds_by_node-section) sections, resolved exactly as the UI resolves them (the instance label is matched against the override keys with and without its port). What turns a node red in the graph is what notifies.
- `threshold_colors: false` only disables the colors in the UI; it does not disable the notifications.
- A node reported down does not produce any other alert for that cycle: the metrics Prometheus still returns for it are stale.
- The interface capacity falls back to 1 Gbit/s when `node_network_speed_bytes` is absent, as in the UI.
- Nodes are enumerated from `up{exporter="node"}`, so a node missing from that query is not evaluated at all.

#### Payload

Each event produces one POST per target with a JSON body of exactly six fields:

```json
{
  "title": "[FIRING] DiskHigh - node2",
  "message": "disk 95% (threshold 90%)\nseverity: critical\ninstance: node2\nsince: 2026-08-26T11:00:00Z",
  "url": "https://promlens.example.com/",
  "topic": "infra",
  "recipients": ["ops", "oncall"],
  "module": "promlens"
}
```

| Field | Content |
|---|---|
| `title` | `[FIRING]` or `[RESOLVED]`, the `alertname`, and the node (`instance_label` label, falling back to `instance`) |
| `message` | Alert `summary` annotation, then `severity`, node and `activeAt` (the first poll where the condition was seen) |
| `url` | `targets[].link`, else the global `link` |
| `topic` | `targets[].topic` |
| `recipients` | `targets[].recipients` |
| `module` | `targets[].module` |

#### Notification lifecycle

1. Every `interval` seconds, `promlens.yaml` is re-read and the metrics are polled. Config changes apply without restarting the service. The poll uses the top-level `timeout` (not the `webhooks` one, which only applies to the POST); when it expires the cycle is logged as `webhook notifier cycle failed (metrics poll): TimeoutError: no answer from <url> within <n>s` and retried on the next interval.
2. Alerts are identified by the full sorted set of their labels, so the same condition on two nodes — or two failed units on one node — is tracked separately.
3. A condition must hold on `for_cycles` consecutive polls before it is notified, which filters out the spikes a single poll would otherwise turn into a firing/resolved pair. With the defaults, a CPU spike must last two minutes to notify. A condition that disappears before being confirmed is simply forgotten.
4. The first cycle after startup (or after re-enabling the section) only records the current state; conditions already active do not generate a notification.
5. A confirmed condition absent from the previous cycle produces a `firing` event; a previously firing alert whose condition is gone produces a `resolved` event when `notify_resolved` is true. Resolution is immediate — `for_cycles` only delays the firing side.
6. `disabled` and `severities` are applied to events, not to the tracked state, so adding a pattern while an alert is firing does not fake a `resolved` notification.
7. A failing webhook (timeout, connection error, HTTP 4xx/5xx) is logged as a warning and never interrupts the loop. There is no retry: the event is lost.

#### Disabling a single alert

Add its `alertname` to `disabled`. Glob patterns are supported:

```yaml
webhooks:
  disabled:
    - SystemdUnitFailed    # exact name
    - "Network*"           # every alert whose name starts with Network
    - "*High"              # every threshold alert
```

Node colors in the UI are unaffected; only the notifications are suppressed.

---

## 4. Authentication modes

### Prometheus auth mode: `cert` (mTLS)

When `auth.type: cert`, the backend does not proxy PromQL queries. The browser executes them directly against Prometheus.

- The client certificate must be installed in the browser (or OS keystore).
- Prometheus must have CORS enabled: `--web.cors.origin="https://promlens.example.com"`
- `direct_credentials: false` (default) — browser sends queries without credentials. Use this when Prometheus is on the same origin or accessible without mTLS from the browser.
- `direct_credentials: true` — browser sends the client certificate with each query. Requires Prometheus to return `Access-Control-Allow-Credentials: true` and a non-wildcard `Access-Control-Allow-Origin`.

Fields `username`, `password`, `token`, `proxy`, and `ssl_verify` are ignored in cert mode.

### App auth mode: `cert` (nginx delegation)

When `app_auth.mode: cert`, there is no login page. Authentication is delegated to a reverse proxy (nginx) that:

1. Terminates TLS and verifies the client certificate.
2. Injects the `x-remote-user: <username>` header.

If the header is missing, the application returns HTTP 403 (HTML) or HTTP 401 (JSON for API endpoints).

> **Changed in 0.15.0** — the header this mode reads was renamed to `x-remote-user`. When upgrading from 0.14.x, update the `proxy_set_header` line in your nginx configuration to match, otherwise every request is rejected. The shipped `/usr/share/promlens/nginx/routes.conf` is already up to date.

**nginx example:**

```nginx
location / {
    proxy_pass http://127.0.0.1:8001;
    proxy_set_header x-remote-user $ssl_client_s_dn_cn;
    if ($ssl_client_verify != SUCCESS) { return 403; }
}
```

---

## 5. Configuration — topology.yaml

### nodes

Topology nodes are always displayed, with or without a matching Prometheus instance. If a Prometheus instance matches the node `id` (or its `nodename` from `node_uname_info`), they are merged: the topology node gets the server icon and real-time metric colors.

```yaml
nodes:
  - id: gw
    label: Gateway
    type: router
    parent: cloud1
    interface: ppp0
    ip: 192.168.1.1
    inherit_zone: true
    children:
      - id: sw1
        type: switch
        interface: eth0
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique identifier. Can match a Prometheus hostname |
| `label` | no | Display name (default: `id`) |
| `type` | yes | `cloud`, `router`, `firewall`, `switch`, `vm`, `lxc`, `kube`, `zigbee`, `wifi` |
| `parent` | no | ID of the parent node (creates a link) |
| `children` | no | List of child nodes — equivalent to setting `parent` on each child |
| `interface` | no | Interface to show in the link tooltip toward the parent |
| `ip` | no | IP address used for node resolution |
| `inherit_zone` | no | Set `false` to exclude this node from automatic zone inheritance (default: `true`) |

**Node type colors:**

| Type | Color |
|---|---|
| `cloud` | grey `#7e8aaa` |
| `router` | cyan `#00cfff` |
| `firewall` | orange `#e0972a` |
| `switch` | blue `#4a8adf` |
| `vm` | purple `#c084fc` |
| `lxc` | green `#34d399` |
| `kube` | indigo `#818cf8` |
| `zigbee` | teal `#00bcd4` |
| `wifi` | light blue `#4fc3f7` |

### networks

Auto-attaches Prometheus hosts to a gateway node by CIDR matching on the instance IP.

```yaml
networks:
  - cidr: 192.168.1.0/24
    gateway: gw
    label: LAN
```

| Field | Description |
|---|---|
| `cidr` | IP range in CIDR notation |
| `gateway` | ID of the node to attach matched hosts to |
| `label` | Optional display name |

### zones

Visual grouping bubbles. Hosts with a Prometheus label `zone=<id>` are automatically placed inside the corresponding zone bubble.

```yaml
zones:
  - id: prod
    label: Production
    parent: sw-server
    default_parent: sw-server
    members:
      - somehost
      - another-topology-node
```

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique zone ID |
| `label` | no | Label shown in the bubble (default: `id`) |
| `parent` | yes | Visual anchor — ID of a topology node or another zone |
| `default_parent` | no | Parent node for Prometheus hosts in this zone (if different from the visual anchor) |
| `members` | no | Additional nodes added to the bubble manually |

Three ways to be in a zone (cumulative):
- **Automatic**: Prometheus instance has `zone=<id>` label.
- **Manual**: node listed in `members`.
- **Inherited**: child topology nodes of a zone member are added automatically unless `inherit_zone: false`.

Nested zones are supported. Only root zones (whose parent is a topology node) display a bubble.

### tunnels

Dashed links between nodes, with RX/TX metrics from the declared WireGuard interface.

```yaml
tunnels:
  - from: gw
    to:
      - peer1
      - peer2
    interface: wg0
    interface_to: wg0          # optional, defaults to interface
    parent_interface: ppp0     # optional: physical interface for capacity reference
```

| Field | Description |
|---|---|
| `from` | Source node ID (topology or Prometheus hostname) |
| `to` | Target node ID or list of IDs |
| `interface` | WireGuard interface on `from` |
| `interface_to` | WireGuard interface on `to` (default: same as `interface`) |
| `parent_interface` | Physical interface whose `node_network_speed_bytes` is used as link capacity |

Tunnel interfaces are automatically excluded from normal link tooltips for the same nodes. If a target is unknown to Prometheus, a greyed-out ghost node is created.

### links

Filters which interfaces appear in link tooltips. Without a `links` declaration, all non-excluded interfaces are shown.

```yaml
links:
  - from: host1
    to:
      - host2
      - host3
    interface: eth0              # interface on `from` side (string or list)
    interface_to: eth1           # interface on `to` side (optional, defaults to interface)
```

### cameras

Overrides the parent node for specific Frigate cameras. Without this section, the parent comes from the `parent` label in `frigate_camera_fps`.

```yaml
cameras:
  - name: front-door
    parent: sw-cameras
  - name: garage
    parent: sw-cameras
```

---

## 6. API reference

### POST /api/query

Proxies a PromQL query to Prometheus. Rate-limited to 60 requests per minute per IP.

**Request body:**

```json
{
  "metric": "up{exporter=\"node\"}",
  "mode": "instant",
  "time": null,
  "start": null,
  "end": null,
  "step": "60s"
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `metric` | string | required | PromQL expression (max 2000 chars) |
| `mode` | string | `instant` | `instant` or `range` |
| `time` | string | null | Evaluation timestamp (RFC3339 or Unix epoch) |
| `start` / `end` | string | null | Required for range queries |
| `step` | string | `60s` | Range resolution |

**Response:**

```json
{
  "metric": "up{exporter=\"node\"}",
  "result_type": "vector",
  "count": 2,
  "instances": [
    {
      "labels": {"instance": "server1:9100", "job": "node"},
      "value": "1",
      "timestamp": 1748000000.0
    }
  ]
}
```

**Errors:**

| HTTP | Cause |
|---|---|
| 400 | Query failed (Prometheus error or network issue) |
| 422 | Missing `start`/`end` for range query |
| 429 | Rate limit exceeded (60 req/min per IP) |
| 503 | `promlens.yaml` not found or missing `url` field |

```bash
curl -s -X POST http://127.0.0.1:8001/api/query \
  -H "Content-Type: application/json" \
  -d '{"metric":"up{exporter=\"node\"}"}' | jq .
```

### GET /api/config

Returns the Prometheus URL (no credentials), auth type, and enabled integrations. Used by the frontend to configure query routing.

```bash
curl -s http://127.0.0.1:8001/api/config | jq .
```

```json
{
  "url": "http://prometheus.example.com:9090",
  "configured": true,
  "auth_type": "none",
  "instance_label": "instance",
  "direct_credentials": false,
  "refresh": 30,
  "blackbox": {"destination_label": "instance"},
  "libvirt": null,
  "frigate": {"camera_url": "https://frigate.example.com"},
  "app_auth_mode": "none",
  "app_auth_user": null
}
```

When a section (`blackbox`, `libvirt`, `frigate`) is absent from `promlens.yaml`, it returns `null`. When present but empty, it returns `{}`. The frontend treats `null` as disabled and anything else as enabled (unless `enabled: false` is set).

### GET /api/topology

Parses and returns `topology.yaml`. Flattens nested `children` into a flat node list with `parent` set.

```bash
curl -s http://127.0.0.1:8001/api/topology | jq .nodes[0]
```

Returns `{"nodes":[], "networks":[], "zones":[], "tunnels":[], "links":[], "cameras":[]}` if the file does not exist.

### GET /api/layout

Returns saved node positions and view toggle states from `layout.json`. Returns `{}` if the file does not exist.

### POST /api/layout

Saves node positions and view toggle states. Body must be a JSON object.

```json
{
  "gw":              {"x": 120.5, "y": -45.2},
  "__srv__host1:9100": {"x": 300.0, "y": 80.0},
  "__zonesVisible":   true,
  "__tunnelsVisible": false,
  "__guestHidden":    {"vm-down": true}
}
```

| HTTP | Cause |
|---|---|
| 413 | Payload exceeds 512 KB |
| 422 | Invalid JSON or schema error |

### POST /api/reload

Validates `promlens.yaml` and `topology.yaml` with `yaml.safe_load`. No server restart required.

```bash
# Success
curl -s -X POST http://127.0.0.1:8001/api/reload | jq .
# {"ok": true}

# Parse error (HTTP 400)
curl -s -X POST http://127.0.0.1:8001/api/reload
# {"detail": "topology.yaml: mapping values are not allowed here\n  line 5, column 3"}
```

### GET /api/alerts

Proxies `GET /api/v1/alerts` from Prometheus and returns the array of alert objects. Used by the frontend to populate the alerts panel and node overlays. Not available in cert mode (the browser fetches alerts directly from Prometheus in that case).

Returns an empty array `[]` if Prometheus is unreachable or returns an error. Does not propagate Prometheus errors to the caller.

**Response:**

```json
[
  {
    "labels":      {"alertname": "NodeHighCPU", "instance": "server1:9100", "severity": "warning"},
    "annotations": {"summary": "CPU above 85% for 10 minutes"},
    "state":       "firing",
    "activeAt":    "2026-05-25T10:00:00Z"
  }
]
```

**Errors:**

| HTTP | Cause |
|---|---|
| 429 | Rate limit exceeded (shared with `POST /api/query`) |
| 503 | `promlens.yaml` not found or missing `url` field |

```bash
curl -s http://127.0.0.1:8001/api/alerts | jq '[.[] | select(.state=="firing")]'
```

### GET /login

Returns the login page HTML. Only meaningful when `app_auth.mode: basic`.

### POST /api/auth/login

Authenticates against the htpasswd file. Sets an HttpOnly session cookie on success.

```bash
curl -s -c cookies.txt -X POST http://127.0.0.1:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"secret"}'
```

### POST /api/auth/logout

Clears the session cookie.

---

## 7. Backend internals — security

### Rate limiting

`POST /api/query` is limited to **60 requests per minute per client IP**. Implemented with an in-memory sliding window (`collections.deque`). Exceeds return HTTP 429.

### CSRF protection

For all non-safe API methods (POST, PUT, DELETE, PATCH) except `/api/auth/*`, the backend checks the `Origin` header against the `Host` header. A mismatch returns HTTP 403.

```python
# Simplified logic
if origin and urlparse(origin).netloc != host:
    return 403
```

### Content-Security-Policy

The CSP is built dynamically from `promlens.yaml` and cached until the file changes. It includes the Prometheus origin in `connect-src` to allow direct queries in cert mode.

```
default-src 'self';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
font-src 'self' data:;
img-src 'self' data:;
connect-src 'self' https://prometheus.example.com;
```

### Security headers (all responses)

| Header | Value |
|---|---|
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | Dynamic (see above) |

### Layout file size cap

`POST /api/layout` rejects payloads larger than **512 KB** with HTTP 413.

---

## 8. Environment variables

Set in `/etc/default/promlens` for the Debian package, or passed to the container.

| Variable | Default | Description |
|---|---|---|
| `CONFIG_FILE` | `/etc/promlens/promlens.yaml` | Path to Prometheus config file |
| `TOPOLOGY_FILE` | `/etc/promlens/topology.yaml` | Path to topology file |
| `LAYOUT_FILE` | `/var/lib/promlens/layout.json` | Path to layout persistence file |
| `BIND_HOST` | `127.0.0.1` | Bind address |
| `BIND_PORT` | `8001` | Bind port |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 9. Frontend internals

### fetchAll — 20 parallel queries

Every refresh cycle runs these in parallel with `Promise.all`:

| # | Source | Query / Endpoint | Used for |
|---|---|---|---|
| 1 | Backend | `GET /api/topology` | Topology nodes, zones, tunnels, links, cameras |
| 2 | Prometheus | `up{exporter="node"}` | Node up/down status |
| 3 | Prometheus | `rate(node_network_receive_bytes_total{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}[5m])` | RX per interface |
| 4 | Prometheus | `rate(node_network_transmit_bytes_total{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}[5m])` | TX per interface |
| 5 | Prometheus | `node_network_speed_bytes{device!~"lo|veth.*|vnet.*|docker.*|br-.*|virbr.*"}` | Interface capacity |
| 6 | Prometheus | `node_uname_info` | Hostname (nodename), OS, kernel version |
| 7 | Prometheus | `avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m]))` | CPU idle (inverted = usage) |
| 8 | Prometheus | `node_memory_MemAvailable_bytes` | Available RAM |
| 9 | Prometheus | `node_memory_MemTotal_bytes` | Total RAM |
| 10 | Prometheus | `node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|rootfs|overlay"}` | Free disk space |
| 11 | Prometheus | `node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|rootfs|overlay"}` | Total disk space |
| 12 | Prometheus | `node_load1` | 1-minute load average |
| 13 | Prometheus | `node_boot_time_seconds` | Boot time (uptime calculation) |
| 14 | Prometheus | `node_systemd_unit_state{state="failed"} == 1` | Failed systemd units |
| 15 | Prometheus | `probe_success{module="icmp"}` | ICMP probe results (blackbox only) |
| 16 | Prometheus | `probe_success{module="ssh_banner"}` | SSH probe results (blackbox only) |
| 17 | Prometheus | `probe_success{module="tcp_connect"}` | TCP connect probe results (blackbox only) |
| 18 | Prometheus | `probe_success{module=~"https?_2xx"}` | HTTP/HTTPS probe results (blackbox only) |
| 19 | Prometheus | `libvirt_domain_info_state` | VM state on hypervisors (libvirt only) |
| 20 | Prometheus | `frigate_camera_fps` | Camera online status (frigate only) |
| 21 | Backend / Prometheus | `GET /api/alerts` (proxied) or `GET /api/v1/alerts` (cert mode, direct) | Firing Prometheus alerts |

Queries 15-20 are skipped (Promise resolves immediately) when their integration is disabled. Query 21 always runs; it returns an empty array on error so a Prometheus alertmanager outage does not block the graph.

### buildGraph phases

**Phase 1 — Prometheus instance index**
Builds `promHostToSrvId`: hostname -> synthetic node ID (`__srv__${instance}`). Detects merged nodes (Prometheus instances whose hostname matches a topology node ID).

**Phase 2 — Topology nodes**
Creates all topology nodes in Vis.js with SVG icons. Builds parent-child edges. Computes link tooltip (RX/TX) using the declared interface if present.

**Phase 3 — Name resolution index**
Builds `hostToNodeId` mapping from all known names (topology ID, IP, label, Prometheus instance, nodename from `node_uname_info`) to Vis.js node IDs. Also computes `linkIfaceMap` and `tunnelIfaceExclude`.

**Phase 4 — Zones**
Resolves nested zones recursively. Collects zone members from Prometheus labels and explicit `members` lists. Sorts zones so parents render before children.

**Phase 5 — Prometheus hosts**
For each `up{exporter="node"}` instance:
- Computes color from worst metric ratio.
- If instance matches a topology node: updates icon and tooltip of the existing node.
- Otherwise: creates a new server node and resolves its parent (see parent resolution order below).
- Adds invisible intra-zone edges for physics grouping.

**Phase 5b — Probe-only nodes**
Creates lightweight nodes for targets that appear in blackbox/TCP/HTTP probes but have no `node_exporter` instance.

**Phase 6 — WireGuard tunnels**
Creates dashed edges. Creates ghost nodes for endpoints unknown to Prometheus.

**Phase 7 — Blackbox probe edges**
Groups probes by unordered node pair. Creates colored edges (green=all UP, red=any DOWN). Bidirectional probes get arrows at both ends.

**Phase 8 — Frigate cameras**
Creates one node per camera from `frigate_camera_fps`. Resolves parent using the cameras section, then the `parent` label, then the topology fallback.

### Parent resolution order (Prometheus hosts)

Priority (highest to lowest):

1. `promParentOverride` (internal — zone explicit member overrides)
2. `network` label matching a declared network by label or CIDR
3. `job=vm` with `parent` label
4. `zone` label matching a declared zone
5. CIDR match in `networks`
6. Fallback: first `router`, then first `switch` in topology

### Node color logic

```
no data (unknown)  ->  grey   #3d4560
down               ->  red    #f04f4f
metricMax >= 0.9   ->  red    #f04f4f
metricMax >= 0.7   ->  orange #e0972a
  OR failed systemd units
  OR failing TCP probe
  OR failing HTTP probe
  OR failing blackbox probe
otherwise          ->  green  #2dba6e

metricMax = max(cpuRatio, memRatio, diskRatio)
cpuRatio   = 1 - cpuIdle
memRatio   = 1 - memAvail / memTotal
diskRatio  = 1 - diskAvail / diskTotal
```

If `metricMax >= 1.0`, the node blinks with a red ring animation via `requestAnimationFrame`.

**Alert severity overlay.** After the base color is computed, if any Prometheus alert is firing for the node (matched via `instance_label`), the node icon and label color are replaced with the severity color. The base color is still computed and displayed in the tooltip; only the visual representation changes.

| Severity | Color |
|---|---|
| `critical` | red `#f04f4f` |
| `warning` | orange `#e0972a` |
| `info` | blue `#4a8adf` |

When a node has alerts with mixed severities, the highest-priority severity wins: critical > warning > info.

### Prometheus alerts panel

A floating panel appears in the bottom-right corner of the graph when at least one Prometheus alert is firing. It is collapsed by default; click the header to expand the alert list.

**Panel border and badge color** reflects the worst severity among all currently firing alerts:

| Worst severity | Border / badge color |
|---|---|
| `critical` | red `#f04f4f` |
| `warning` | orange `#e0972a` |
| `info` | blue `#4a8adf` |

**Alert rows** show the alert name (colored by its own severity) and the node it belongs to. The node is identified using the `instance_label` config option (default: `instance`), so the value shown matches the node identifier used in the graph. The full `summary` annotation is shown below the alert name when present.

### Alert info in node tooltip and detail modal

When a Prometheus alert is firing for a node:

- **Hover tooltip**: shows the alert names colored by severity, with no summary text. Summary is omitted to avoid overflow in the compact tooltip.
- **Double-click modal**: opens the detail modal which shows alert names plus the full `summary` annotation.

### Blinking dot on alerted nodes

Nodes with at least one firing Prometheus alert display a small filled circle rendered on a dedicated overlay canvas (`#alertDotCanvas`) positioned above the Vis.js canvas. The dot color matches the worst severity of the node's firing alerts (same priority: critical > warning > info).

The dot blinks at approximately 1 Hz using `requestAnimationFrame`. Because the overlay canvas is independent of Vis.js, drawing the dots does not trigger Vis.js redraws and has no impact on graph rendering performance.

### Prometheus alerts page

Double-clicking the **Prometheus Alerts** panel header opens a dedicated full-page view listing all currently firing alerts.

To return to the graph, click the **←** button in the header or press `Escape`.

**Layout:**

- Alerts are sorted by severity (critical → warning → info), then alphabetically by alert name.
- Each alert is rendered as a card showing: severity badge, alert name, and the instance value.
- Clicking a card expands it to show:
  - The full `summary` annotation (if present).
  - All remaining labels as `key=value` chips (excluding `alertname` and `severity`, shown in the card header).
  - All remaining annotations as `key=value` chips (excluding `summary`, shown above).

### Link color logic

```
worst = max(ifaceRatio) across all visible interfaces
worst >= 0.9  ->  red    #f04f4f
worst >= 0.7  ->  orange #e0972a
node is up    ->  green  rgba(45,186,110,.3)
node is down  ->  red    rgba(240,79,79,.35)  (dashed)

ifaceRatio = max(rx, tx) / speed
             speed defaults to 1 Gbps if absent or <= 0
```

### UI controls

| Control | Action |
|---|---|
| RELOAD | Calls `POST /api/reload` then `fetchAll()` |
| TEST | Calls `fetchAll()` |
| Refresh (arrow) | Calls `fetchAll()` |
| Fit (square) | `network.fit()` with animation |
| Reset | Clears `savedPositions` and localStorage, then `fetchAll()` |
| Save (grid) | Calls `POST /api/layout` with current positions and toggle states |
| Select mode | Enables rubber-band selection (drag to select multiple nodes) |
| Fullscreen | Enters fullscreen mode; shows a floating toolbar |
| VIEWS dropdown | Toggles zones, tunnels, ICMP-UP links, SSH-UP links, legend, and guest visibility by kind and state |
| Refresh interval | 10s / 30s / 1m / 5m / off |
| Search | Focus and blink a node or a zone by label or ID (press `/` to focus the input) |
| Search modal | `Ctrl+F` / `Cmd+F` opens a command-palette search over visible nodes and zones |
| Help modal | `Ctrl+H` / `Cmd+H` opens the keyboard shortcuts list |

**Node click:** single-click focuses the node (hides unrelated edges). Double-click opens the detail modal. If `camera_url` is configured, clicking a Frigate camera node opens `camera_url/#camera_name` in a new window.

**Edge click:**
- Blackbox edge: opens Prometheus graph for `probe_success{instance="..."}`.
- WireGuard tunnel: opens Prometheus graph for RX/TX of the WireGuard interface.
- Normal server link: opens Prometheus graph for RX/TX of the server instance.

#### Keyboard shortcuts

Six global shortcuts use `Ctrl` on Linux/Windows and `Cmd` on macOS. All of them override the browser default.

| Shortcut | Action | Alerts page |
|---|---|---|
| `Ctrl+F` | Open the search modal | ignored |
| `Ctrl+S` | Save layout | ignored |
| `Ctrl+M` | Toggle selection mode | ignored |
| `Ctrl+A` | Fit the view | ignored |
| `Ctrl+R` | Refresh now | active |
| `Ctrl+H` | Open the keyboard shortcuts help modal | active |

Each shortcut clicks the matching toolbar button (`saveLayoutBtn`, `selectModeBtn`, `fitBtn`, `refreshBtn`), so a shortcut cannot drift from what its button does.

All bindings live in a single `SHORTCUTS` table in `src/static/index.html`. That table drives both the key handler and the help modal, so the on-screen list always matches the real bindings.

**Exceptions:**

- `Ctrl+A` is not intercepted while a text input is focused. There it keeps its native "select all" meaning.
- `Ctrl+F`, `Ctrl+S`, `Ctrl+M` and `Ctrl+A` are flagged `mapOnly` and are ignored while the Prometheus alerts page is open. `Ctrl+R` and `Ctrl+H` work everywhere.

**Help modal:**

`Ctrl+H` opens `<dialog id="helpModal">`, titled "Keyboard shortcuts". It lists every entry of the `SHORTCUTS` table, plus two rows for keys handled by their own listeners:

| Key | Action |
|---|---|
| `/` | Focus the top-bar search field |
| `esc` | Close a modal, or clear the node focus |

Escape closes the help modal, and doing so does not also clear the node focus.

The search modal and the help modal never stack: opening one closes the other.

#### Search modal

`Ctrl+F` (`Cmd+F` on macOS) opens `<dialog id="searchModal">` instead of the browser's native find bar. The shortcut is ignored while the Prometheus alerts page is open.

The modal holds an input, a live-filtered result list (30 entries max) and a footer hint. Each row shows the entry label and its kind badge (`server`, `vm`, `lxc`, `router`, `zone`, ...).

**Searchable entries**, in this order:

1. Every currently displayed node.
2. Every currently displayed root zone.

Nodes hidden by the guest visibility toggles and zones hidden by the zones toggle are not searchable. Only root zones are drawn, so nested zones are folded into their root zone bubble and are not searchable on their own.

**Matching ranks**, lower is better:

| Rank | Condition |
|---|---|
| 0 | Label equals the query |
| 1 | Label starts with the query |
| 2 | Label contains the query |
| 3 | Node ID or zone ID contains the query |

Sorting is stable, so nodes stay ahead of zones at equal rank.

**Keys inside the modal:**

| Key | Action |
|---|---|
| Up / Down | Move the selection in the result list |
| Enter | Zoom on the selected entry |
| Escape | Close the modal |
| `Ctrl+F` | Re-select the input text |

Clicking a row selects it as well.

**Zoom behaviour:**

- Node: `network.focus()` at scale 1.5, plus a 1.5s cyan blink ring around the node. Same result as the top-bar search bar.
- Zone: the view centers on the zone bubble and zooms so the bubble fits the canvas with a small margin, scale capped at 1.5, then the bubble outline blinks cyan for 1.5s.

The top-bar search bar (and its `/` shortcut) now matches zones too, using the same ranking. Pressing Enter there zooms on the best match, node or zone.

#### Guest visibility toggles

Nine toggles in the VIEWS dropdown hide guest nodes by kind and state. All are visible (checked) by default. Turning one off hides the matching nodes and every edge touching them.

| Kind | Topology `type` | Toggles |
|---|---|---|
| vm | `vm` | VM UNMONITORED / VM DOWN / VM UP |
| lxc | `lxc` | LXC UNMONITORED / LXC DOWN / LXC UP |
| pod | `kube` | POD UNMONITORED / POD DOWN / POD UP |

Nodes of any other type (`cloud`, `router`, `firewall`, `switch`, `zigbee`, `wifi`) have no kind and are never affected by these toggles.

State is resolved in `buildGraph`:

- Every topology node starts as `unmonitored`.
- Phase 5: a node merged with a Prometheus `up` instance becomes `up` (`up == 1`) or `down` (`up == 0`). A non-numeric value leaves it `unmonitored`.
- Phase 5c: VM nodes created from the libvirt exporter (VMs with no `node_exporter` of their own) always get kind `vm` and always stay `unmonitored`, whatever libvirt reports. Such a VM is not actually monitored, so it must never count as up or down. Its colour, icon and tooltip still show the real libvirt state (running, paused, shut off, crashed). As a result, VM DOWN and VM UP only apply to VMs declared in `topology.yaml` and merged with a Prometheus `up` series: a VM is `up` only if it is declared in the topology *and* an `up` series matches it with value `1`.

Toggle states are saved by `POST /api/layout` under `__guestHidden`, an object mapping `"<kind>-<state>"` (for example `"vm-down"`) to a boolean meaning *hidden*. The legacy keys `__libvirtShutOffHidden` and `__libvirtUpHidden` are ignored when an older layout is loaded.

### Vis.js physics settings

Solver: `forceAtlas2Based`

| Parameter | Value |
|---|---|
| `gravitationalConstant` | -65 |
| `centralGravity` | 0.004 |
| `springLength` | 130 |
| `springConstant` | 0.08 |
| `damping` | 0.42 |
| `avoidOverlap` | 0.6 |
| `stabilization.iterations` | 160 |

Physics is disabled after stabilization. Subsequent refreshes update node/edge datasets in-place without re-running the physics engine.

---

## 10. Complete topology.yaml examples

### Minimal single-site

```yaml
nodes:
  - id: internet
    label: Internet
    type: cloud

  - id: gw
    label: Gateway
    type: router
    parent: internet
    interface: ppp0

networks:
  - cidr: 192.168.1.0/24
    gateway: gw
    label: LAN
```

All Prometheus hosts with an IP in `192.168.1.0/24` attach to `gw` automatically.

### With zones and nested children

```yaml
nodes:
  - id: isp-cloud
    label: ISP cloud
    type: cloud

  - id: gw
    label: Gateway
    type: router
    parent: isp-cloud
    interface: wan0
    children:
      - id: sw-prod
        type: switch
        interface: eth-prod
      - id: sw-cam
        type: switch
        interface: eth-cam

zones:
  - id: prod
    label: Production
    parent: sw-prod

  - id: vms
    label: VMs
    parent: prod
    default_parent: sw-prod

networks:
  - cidr: 10.0.1.0/24
    gateway: sw-prod
```

Prometheus hosts with `zone=prod` appear in the Production bubble. Hosts with `zone=vms` appear in the VMs bubble (nested inside prod).

### With WireGuard tunnels and interface filtering

```yaml
nodes:
  - id: gw
    label: Gateway
    type: router
    parent: cloud

links:
  - from: gw
    to: [server1, server2]
    interface: eth0

tunnels:
  - from: gw
    to:
      - remote1
      - remote2
    interface: wg0
    parent_interface: ppp0
```

Result:
- Links `gw <-> server1` and `gw <-> server2` show only `eth0` in the tooltip.
- Links to `remote1` and `remote2` are dashed with `wg0` metrics.
- `wg0` is hidden from normal link tooltips on `gw`.
- `ppp0` speed is used as the WireGuard link capacity.

### With Frigate cameras and blackbox probes

```yaml
# promlens.yaml
frigate:
  enabled: true
  camera_url: https://frigate.example.com

blackbox:
  enabled: true
  destination_label: instance
  source_label: probe_src
```

```yaml
# topology.yaml
nodes:
  - id: sw-cam
    label: Camera Switch
    type: switch
    parent: gw

cameras:
  - name: front-door
    parent: sw-cam
  - name: backyard
    parent: sw-cam
```

Cameras `front-door` and `backyard` appear as nodes attached to `sw-cam`. Clicking either opens `https://frigate.example.com/#front-door` (or `#backyard`) in a popup window.

---

## 11. Caveats

- **cert mode and CORS**: In cert mode (`auth.type: cert`), the browser queries Prometheus directly. Prometheus must be configured with `--web.cors.origin` matching the ProMLens origin exactly. A wildcard origin (`*`) will not work when `direct_credentials: true` because browsers reject credentialed requests to wildcard origins.

- **direct_credentials default is false**: Since the `97a0ac6` commit, direct queries in cert mode go without credentials by default (`credentials: 'omit'`). Set `direct_credentials: true` only if your Prometheus requires the client certificate for every request.

- **Rate limit is per IP, in-memory**: The 60 req/min rate limit resets on server restart and is not shared across multiple instances. The frontend makes up to 20 parallel queries per refresh cycle, so a 30-second refresh interval generates at most 40 requests per minute (20 on each cycle, two cycles per minute).

- **Layout file is world-writable by the process**: The process writing `layout.json` needs write access to `LAYOUT_FILE`. The Debian package sets up appropriate permissions. In a container, mount the parent directory with write access.

- **Webhooks in cert mode**: In cert mode (`auth.type: cert`), the backend has no client certificate, so the notifier queries the metrics without credentials. If Prometheus enforces mTLS the poll fails every cycle and only logs a warning. Webhook notifications are therefore only usable when the backend itself can reach Prometheus (`none`, `basic` or `bearer` mode).

- **No notification for alerts already active at startup**: The first poll cycle seeds the state silently. A restart during an incident does not re-send notifications for conditions that were already active, and their eventual `resolved` event is still sent.

- **A flapping metric still produces pairs of events**: `for_cycles` delays the firing side only. A metric that stays above its red threshold for `for_cycles` polls, then drops for one poll, then comes back, produces `firing` / `resolved` / `firing`. Raise `for_cycles` or the threshold for a genuinely noisy node.

- **Notified alerts and displayed alerts are two different feeds**: the graph, the alerts panel and the alerts page show the Prometheus alerts of `/api/v1/alerts`; the webhooks notify the ProMLens alerts computed from the thresholds. A Prometheus alerting rule never triggers a webhook, and a ProMLens alert never appears in the alerts panel.

- **Webhook delivery is best effort**: A failed POST is logged and dropped, with no retry and no queue. Alerts firing and resolving within a single `interval` window are never notified.

- **YAML reload is parse-only**: `POST /api/reload` validates syntax with `yaml.safe_load` but does not validate field values or types. An invalid `url` field passes reload validation but causes HTTP 503 on the next query.
