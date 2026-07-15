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

Scope the devices the standard fleet-tool way (shared with roomOS_find_device.py and
roomOS_apply_config.py): explicit --device-id (repeatable), --stdin (ids one per line,
pipeable), --name search with interactive pick, and/or any combination of:
  --model        product name, supports shell wildcards (e.g. "*Desk*", "Room Bar")
  --kind         personal | workspace  (whether the device is assigned to a person or a space)
  --type         device type, e.g. roomdesk, accessory, phone
  --platform     device platform, e.g. cisco
  --connection   online | offline | expired (aliases) or a raw connectionStatus value
With no selection at all, every device in the org is queried.

Choose the values to read with repeatable flags using dot-notation tree paths:
  --status SystemUnit.Uptime          (read via /v1/xapi/status)
  --config Audio.DefaultVolume        (read via /v1/deviceConfigurations)
Simple [n] indexes are supported, e.g. --status "Network[1].IPv4.Address".

These paths are CASE-SENSITIVE and must match the RoomOS xAPI casing exactly (PascalCase, e.g.
SystemUnit.Uptime, Audio.DefaultVolume, Network[1].IPv4.Address). The Webex API accepts a
wrong-cased path but returns no value, so that cell shows "(null)".

Each requested path becomes a CSV column. A device that does not return a requested value
(commonly an error/unsupported condition) reports "(null)" for that cell.

Reads the Webex token from --token or ~/Personal-Local/config.yml (wxteams.auth_token); the
token needs the xAPI scopes (spark:xapi_commands / spark:xapi_statuses) and admin access to the
devices' org.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from typing import Any, Dict, List, Optional

from roomos_common import (add_selection_args, resolve_target_devices, resolve_token,
                           xapi_status, xconfig_get)

NULL = "(null)"

# fixed identity columns emitted before the queried value columns (header -> device field)
IDENTITY_COLUMNS = ["displayName", "status", "type", "devicePlatform", "ipAddress", "macAddress",
                    "lastSeen", "serial", "software", "product"]

_INDEX_RE = re.compile(r"^(?P<name>.+)\[(?P<idx>\d+)\]$")


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
                 token: str, base_url: str, timeout: int, verbose: bool = False):
    """Build one CSV row for a device (identity + queried values); return (row, null_count)."""
    device_id = device.get("id", "")
    name = device.get("displayName", device_id)
    row: Dict[str, Any] = {
        "displayName": device.get("displayName", ""),
        "status": device.get("connectionStatus", ""),
        "type": device.get("type", ""),
        "devicePlatform": device.get("devicePlatform", ""),
        "ipAddress": device.get("ip", ""),
        "macAddress": device.get("mac", ""),
        "lastSeen": device.get("lastSeen", ""),
        "serial": device.get("serial", ""),
        "software": device.get("software", ""),
        "product": device.get("product", ""),
    }
    nulls = 0

    def read(label: str, getter) -> None:
        nonlocal nulls
        reason: Optional[str] = None
        try:
            value = getter()
            if value is None:
                reason = "no value returned"
        except Exception as exc:
            value = None
            reason = str(exc)[:160]
        if value is None:
            nulls += 1
            row[label] = NULL
            if verbose:
                print(f"  ! {name} :: {label} -> (null): {reason}", file=sys.stderr)
        else:
            row[label] = value

    for path in status_paths:
        read(path, lambda p=path: extract_value(xapi_status(p, device_id, token, base_url, timeout), p))
    for key in config_keys:
        read(key, lambda k=key: xconfig_get(k, device_id, token, base_url, timeout))

    return row, nulls


def main() -> int:
    """query xStatus/xConfiguration across a filtered device set and export CSV (cloud only)"""
    ap = argparse.ArgumentParser(
        description="Query xStatus/xConfiguration values across filtered RoomOS devices "
                    "in your Webex org and export to CSV.",
    )
    ap.add_argument("--token", help="Webex access token (omit to read config / prompt)")
    ap.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    add_selection_args(ap)

    ap.add_argument("--status", action="append", default=[], metavar="PATH",
                    help="xStatus tree path to read, e.g. SystemUnit.Uptime (repeatable)")
    ap.add_argument("--config", action="append", default=[], metavar="KEY",
                    help="xConfiguration key to read, e.g. Audio.DefaultVolume (repeatable)")

    ap.add_argument("-o", "--output", help="Write CSV to this file (default: stdout)")
    ap.add_argument("--verbose", action="store_true",
                    help="Log per-value lookup failures (e.g. missing scope) to stderr")

    args = ap.parse_args()

    if not args.status and not args.config:
        print("ERROR: specify at least one --status or --config value to query", file=sys.stderr)
        return 2

    try:
        token = resolve_token(args.token)

        matched = resolve_target_devices(args, token, args.base_url, args.timeout,
                                         default_all=True)
        if not matched:
            print("No devices selected.", file=sys.stderr)
            return 0

        fieldnames = IDENTITY_COLUMNS + args.status + args.config
        rows = []
        total_nulls = 0
        for index, device in enumerate(matched, 1):
            print(f"  [{index}/{len(matched)}] querying {device.get('displayName', device.get('id', ''))}...",
                  file=sys.stderr)
            row, nulls = query_device(device, args.status, args.config,
                                      token, args.base_url, args.timeout, args.verbose)
            rows.append(row)
            total_nulls += nulls

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
        if total_nulls and not args.verbose:
            print(f"Note: {total_nulls} value lookup(s) returned (null) — check path casing "
                  "(paths are case-sensitive) or re-run with --verbose to see why.",
                  file=sys.stderr)
        return 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
