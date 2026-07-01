#!/usr/bin/env python3
# Copyright (C) 2026 Frederick W. Nielsen
#
# This file is part of roomOS.
#
# roomOS is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# roomOS is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with roomOS.  If not, see <https://www.gnu.org/licenses/>.

"""
roomos_bulk_query.py

Query xStatus and/or xConfiguration values across a filtered set of RoomOS devices in your
Webex org (cloud xAPI) and export the results to CSV. Cloud-only -- there is no local mode.

Scope the devices with any combination of:
  --model        product name, supports shell wildcards (e.g. "*Desk*", "Room Bar")
  --kind         personal | workspace  (whether the device is assigned to a person or a space)
  --type         device type, e.g. roomdesk, accessory, phone
  --platform     device platform, e.g. cisco
  --connection   online | offline | expired (aliases) or a raw connectionStatus value

Choose the values to read with repeatable flags using dot-notation tree paths:
  --status SystemUnit.Uptime          (read via /v1/xapi/status)
  --config Audio.DefaultVolume        (read via /v1/deviceConfigurations)
Simple [n] indexes are supported, e.g. --status "Network[1].IPv4.Address".

Each requested path becomes a CSV column. A device that does not return a requested value
(commonly an error/unsupported condition) reports "(null)" for that cell.

Reads the Webex token from --token or ~/Personal-Local/config.yml (wxteams.auth_token); the
token needs the xAPI scopes (spark:xapi_commands / spark:xapi_statuses) and admin access to the
devices' org.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import re
import sys
from typing import Any, Dict, List, Optional

from roomos_common import list_devices, resolve_token, xapi_status, xconfig_get

NULL = "(null)"

# friendly --connection aliases mapped to the raw Webex connectionStatus values they cover
CONNECTION_ALIASES = {
    "online": {"connected", "connected_with_issues"},
    "offline": {"disconnected", "offline_expired", "offline_deep_sleep", "offline_temporarily"},
    "expired": {"offline_expired"},
}

# fixed identity columns emitted before the queried value columns (header -> device field)
IDENTITY_COLUMNS = ["displayName", "status", "type", "devicePlatform", "ipAddress", "macAddress"]

_INDEX_RE = re.compile(r"^(?P<name>.+)\[(?P<idx>\d+)\]$")


def device_kind(device: Dict[str, Any]) -> str:
    """Return 'personal' (assigned to a person), 'workspace', or '' (neither)."""
    if device.get("personId"):
        return "personal"
    if device.get("workspaceId"):
        return "workspace"
    return ""


def expand_connections(values: List[str]) -> set:
    """Expand --connection terms (aliases or raw) into a set of raw connectionStatus values."""
    accepted: set = set()
    for value in values:
        low = value.lower()
        accepted |= {v.lower() for v in CONNECTION_ALIASES.get(low, {low})}
    return accepted


def matches_filters(device: Dict[str, Any], models: List[str], kinds: List[str],
                    types: List[str], platforms: List[str], connections: set) -> bool:
    """Apply the model / kind / type / platform / connection filters (all client-side)."""
    if models:
        product = (device.get("product") or "").lower()
        if not any(fnmatch.fnmatch(product, m.lower()) for m in models):
            return False
    if kinds and device_kind(device) not in kinds:
        return False
    if types and (device.get("type") or "").lower() not in [t.lower() for t in types]:
        return False
    if platforms and (device.get("devicePlatform") or "").lower() not in [p.lower() for p in platforms]:
        return False
    if connections and (device.get("connectionStatus") or "").lower() not in connections:
        return False
    return True


def extract_value(result: Any, path: str) -> Any:
    """Walk an xapi/status result by a dot path (with optional [n]) to a scalar; None if absent."""
    node = result.get("result", result) if isinstance(result, dict) else None
    for part in path.split("."):
        if not isinstance(node, (dict, list)):
            return None
        m = _INDEX_RE.match(part)
        if m:
            node = node.get(m.group("name")) if isinstance(node, dict) else None
            if isinstance(node, list):
                idx = int(m.group("idx"))
                node = node[idx] if 0 <= idx < len(node) else None
            else:
                node = None
        else:
            node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return json.dumps(node) if isinstance(node, (dict, list)) else node


def query_device(device: Dict[str, Any], status_paths: List[str], config_keys: List[str],
                 token: str, base_url: str, timeout: int) -> Dict[str, Any]:
    """Build one CSV row dict for a device: identity columns plus each requested value."""
    device_id = device.get("id", "")
    row: Dict[str, Any] = {
        "displayName": device.get("displayName", ""),
        "status": device.get("connectionStatus", ""),
        "type": device.get("type", ""),
        "devicePlatform": device.get("devicePlatform", ""),
        "ipAddress": device.get("ip", ""),
        "macAddress": device.get("mac", ""),
    }

    for path in status_paths:
        try:
            result = xapi_status(path, device_id, token, base_url, timeout)
            value = extract_value(result, path)
            row[path] = NULL if value is None else value
        except Exception:
            row[path] = NULL

    for key in config_keys:
        try:
            value = xconfig_get(key, device_id, token, base_url, timeout)
            row[key] = NULL if value is None else value
        except Exception:
            row[key] = NULL

    return row


def main() -> int:
    """query xStatus/xConfiguration across a filtered device set and export CSV (cloud only)"""
    ap = argparse.ArgumentParser(
        description="Query xStatus/xConfiguration values across filtered RoomOS devices "
                    "in your Webex org and export to CSV.",
    )
    ap.add_argument("--token", help="Webex access token (omit to read config / prompt)")
    ap.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")

    ap.add_argument("--model", action="append", default=[],
                    help="Filter by product name; wildcards allowed, e.g. '*Desk*' (repeatable)")
    ap.add_argument("--kind", action="append", default=[], choices=["personal", "workspace"],
                    help="Filter by assignment: personal or workspace (repeatable)")
    ap.add_argument("--type", action="append", default=[],
                    help="Filter by device type, e.g. roomdesk, accessory, phone (repeatable)")
    ap.add_argument("--platform", action="append", default=[],
                    help="Filter by device platform, e.g. cisco (repeatable)")
    ap.add_argument("--connection", action="append", default=[],
                    help="Filter by status: online/offline/expired or a raw connectionStatus "
                         "value (repeatable)")

    ap.add_argument("--status", action="append", default=[], metavar="PATH",
                    help="xStatus tree path to read, e.g. SystemUnit.Uptime (repeatable)")
    ap.add_argument("--config", action="append", default=[], metavar="KEY",
                    help="xConfiguration key to read, e.g. Audio.DefaultVolume (repeatable)")

    ap.add_argument("-o", "--output", help="Write CSV to this file (default: stdout)")

    args = ap.parse_args()

    if not args.status and not args.config:
        print("ERROR: specify at least one --status or --config value to query", file=sys.stderr)
        return 2

    try:
        token = resolve_token(args.token)

        print("Listing devices...", file=sys.stderr)
        devices = list_devices(token, args.base_url, args.timeout)

        connections = expand_connections(args.connection)
        matched = [d for d in devices
                   if matches_filters(d, args.model, args.kind, args.type, args.platform, connections)]
        print(f"{len(matched)} of {len(devices)} device(s) match the filter.", file=sys.stderr)
        if not matched:
            return 0

        fieldnames = IDENTITY_COLUMNS + args.status + args.config
        rows = []
        for index, device in enumerate(matched, 1):
            print(f"  [{index}/{len(matched)}] querying {device.get('displayName', device.get('id', ''))}...",
                  file=sys.stderr)
            rows.append(query_device(device, args.status, args.config,
                                     token, args.base_url, args.timeout))

        out = open(args.output, "w", newline="", encoding="utf-8") if args.output else sys.stdout
        try:
            writer = csv.DictWriter(out, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        finally:
            if args.output:
                out.close()

        if args.output:
            print(f"Wrote {len(rows)} row(s) to {args.output}", file=sys.stderr)
        return 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
