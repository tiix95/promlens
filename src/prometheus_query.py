#!/usr/bin/env python3
"""Prometheus API query tool - returns instances for a given metric."""

import asyncio
import json
import logging
import ssl
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aiohttp import BasicAuth, ClientSession, ClientTimeout, TCPConnector

logger = logging.getLogger(__name__)


@dataclass
class PrometheusConfig:
    url: str
    username: str | None = None
    password: str | None = None
    token: str | None = None
    timeout: int = 30
    ssl_verify: bool = True
    proxy: str | None = None


def build_config(data: dict[str, Any]) -> PrometheusConfig:
    """Build a PrometheusConfig from the parsed promlens.yaml mapping.

    Raises ValueError when the mandatory 'url' field is missing.
    """
    auth = data.get("auth") or {}
    auth_type = (auth.get("type") or "none").lower()
    url = data.get("url", "")
    if not url:
        raise ValueError("promlens.yaml: champ 'url' manquant ou vide")
    return PrometheusConfig(
        url=url,
        username=auth.get("username") if auth_type == "basic" else None,
        password=auth.get("password") if auth_type == "basic" else None,
        token=auth.get("token") if auth_type == "bearer" else None,
        timeout=int(data.get("timeout", 30)),
        ssl_verify=bool(data.get("ssl_verify", True)),
        proxy=data.get("proxy") or None,
    )


@dataclass
class MetricInstance:
    labels: dict[str, str]
    value: float | str | list[Any]
    timestamp: float | None = None


@dataclass
class QueryResult:
    metric: str
    result_type: str
    instances: list[MetricInstance] = field(default_factory=list)


@dataclass
class RangeParams:
    start: str
    end: str
    step: str = "60s"


def _parse_instance(result: dict[str, Any], result_type: str) -> MetricInstance:
    """Parse a single Prometheus result entry into a MetricInstance."""
    labels = result.get("metric", {})
    match result_type:
        case "vector":
            ts, val = result["value"]
            return MetricInstance(labels=labels, value=val, timestamp=float(ts))
        case "matrix":
            return MetricInstance(labels=labels, value=result["values"])
        case "scalar":
            ts, val = result["value"]
            return MetricInstance(labels={}, value=val, timestamp=float(ts))
        case _:
            return MetricInstance(labels=labels, value=str(result.get("value", "")))


def _parse_response(metric: str, data: dict[str, Any]) -> QueryResult:
    """Parse a Prometheus API response payload into a QueryResult."""
    if data.get("status") != "success":
        raise ValueError(f"Prometheus error: {data.get('error', 'unknown')}")

    result_type: str = data["data"]["resultType"]
    raw = data["data"]["result"]

    if result_type in ("scalar", "string"):
        # scalar/string: result is [timestamp, value], not a list of objects
        ts, val = raw
        instances = [MetricInstance(labels={}, value=val, timestamp=float(ts))]
    else:
        instances = [_parse_instance(r, result_type) for r in raw]

    return QueryResult(metric=metric, result_type=result_type, instances=instances)


async def query_instant(
    session: ClientSession,
    config: PrometheusConfig,
    metric: str,
    time: str | None = None,
) -> QueryResult:
    """Execute an instant PromQL query."""
    params: dict[str, str] = {"query": metric}
    if time:
        params["time"] = time

    url = f"{config.url.rstrip('/')}/api/v1/query"
    logger.debug("Instant query -> %s | params=%s", url, params)

    async with session.get(url, params=params, proxy=config.proxy) as resp:
        resp.raise_for_status()
        return _parse_response(metric, await resp.json())


async def query_range(
    session: ClientSession,
    config: PrometheusConfig,
    metric: str,
    range_params: RangeParams,
) -> QueryResult:
    """Execute a range PromQL query."""
    params = {
        "query": metric,
        "start": range_params.start,
        "end": range_params.end,
        "step": range_params.step,
    }
    url = f"{config.url.rstrip('/')}/api/v1/query_range"
    logger.debug("Range query -> %s | params=%s", url, params)

    async with session.get(url, params=params, proxy=config.proxy) as resp:
        resp.raise_for_status()
        return _parse_response(metric, await resp.json())


async def query_alerts(session: ClientSession, config: PrometheusConfig) -> list[dict[str, Any]]:
    """Fetch the active alerts (pending and firing) from the Prometheus API."""
    url = f"{config.url.rstrip('/')}/api/v1/alerts"
    logger.debug("Alerts query -> %s", url)

    async with session.get(url, proxy=config.proxy) as resp:
        resp.raise_for_status()
        data = json.loads(await resp.text())

    return data.get("data", {}).get("alerts", [])


def _build_session(config: PrometheusConfig) -> ClientSession:
    """Create an aiohttp session with auth and TLS settings."""
    headers: dict[str, str] = {}
    auth: BasicAuth | None = None

    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"
    elif config.username and config.password:
        auth = BasicAuth(config.username, config.password)

    ssl_context: ssl.SSLContext | bool = bool(config.ssl_verify)
    connector = TCPConnector(ssl=ssl_context)
    timeout = ClientTimeout(total=config.timeout)
    return ClientSession(headers=headers, auth=auth, timeout=timeout, connector=connector)


def _format_timestamp(ts: float | None) -> str:
    if ts is None:
        return "N/A"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def display_results(result: QueryResult, output_format: str) -> None:
    """Print query results to stdout."""
    match output_format:
        case "json":
            data = {
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
            print(json.dumps(data, indent=2))

        case "table":
            print(f"\nMetric     : {result.metric}")
            print(f"Type       : {result.result_type}")
            print(f"Instances  : {len(result.instances)}")
            print("-" * 60)
            for i, inst in enumerate(result.instances, 1):
                print(f"  [{i}] {_format_timestamp(inst.timestamp)}")
                for k, v in inst.labels.items():
                    print(f"       {k} = {v}")
                if result.result_type == "matrix":
                    print(f"       values ({len(inst.value)} points)")  # type: ignore[arg-type]
                else:
                    print(f"       value = {inst.value}")
                print()


async def run(args: Namespace) -> None:
    """Async entry point: build session and dispatch query."""
    config = PrometheusConfig(
        url=args.url,
        username=getattr(args, "username", None),
        password=getattr(args, "password", None),
        token=getattr(args, "token", None),
        timeout=args.timeout,
        ssl_verify=not args.no_ssl_verify,
    )

    async with _build_session(config) as session:
        match args.mode:
            case "instant" | None:
                result = await query_instant(
                    session, config, args.metric, getattr(args, "time", None)
                )
            case "range":
                result = await query_range(
                    session, config, args.metric,
                    RangeParams(start=args.start, end=args.end, step=args.step),
                )
            case _:
                raise ValueError(f"Unknown mode: {args.mode}")

    display_results(result, args.format)


def _build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description="Query Prometheus API and display instances for a given metric"
    )
    parser.add_argument("metric", help="PromQL metric or expression")
    parser.add_argument(
        "--url", required=True, help="Prometheus base URL (e.g. http://localhost:9090)"
    )
    parser.add_argument("--username", help="Basic auth username")
    parser.add_argument("--password", help="Basic auth password")
    parser.add_argument("--token", help="Bearer token for authentication")
    parser.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30)"
    )
    parser.add_argument(
        "--no-ssl-verify", action="store_true", help="Disable TLS certificate verification"
    )
    parser.add_argument(
        "--format", choices=["table", "json"], default="table",
        help="Output format (default: table)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    sub = parser.add_subparsers(dest="mode")

    instant = sub.add_parser("instant", help="Instant query (default when no subcommand given)")
    instant.add_argument("--time", help="Evaluation timestamp (RFC3339 or Unix epoch)")

    range_p = sub.add_parser("range", help="Range query over a time interval")
    range_p.add_argument("--start", required=True, help="Start time (RFC3339 or Unix epoch)")
    range_p.add_argument("--end", required=True, help="End time (RFC3339 or Unix epoch)")
    range_p.add_argument("--step", default="60s", help="Resolution step width (default: 60s)")

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    if args.mode is None:
        args.mode = "instant"

    try:
        asyncio.run(run(args))
    except ValueError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Unexpected error: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
