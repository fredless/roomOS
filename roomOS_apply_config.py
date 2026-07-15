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
roomOS_apply_config.py

Apply xConfiguration changes to one or many RoomOS devices via the Webex device
configurations API (PATCH /v1/deviceConfigurations, JSON Patch). Cloud-only. There is no
named-template API -- Control Hub UI config templates are not exposed -- so this tool IS the
"apply a config template" path: a set of --set/--remove ops applied to a selected device set.

Choose the changes with repeatable flags (keys use RoomOS dot notation, CASE-SENSITIVE):
  --set Audio.DefaultVolume=60      set a configured value ("replace")
  --remove Audio.DefaultVolume      clear the configured value, reverting to default ("remove")
Values that look like integers are sent as integers, everything else as strings.

Select the target devices the standard fleet-tool way (see also roomOS_find_device.py):
  --device-id ID (repeatable), --stdin (ids one per line, pipeable), the
  --model/--kind/--type/--platform/--connection filters, or --name search with an
  interactive pick (--all to take every match).

All ops go to each device in a single PATCH call (the API takes one deviceId per call).
Before touching anything the tool shows the ops and the device list and asks for
confirmation; skip that with -y/--yes (required for fully non-interactive runs).
--dry-run shows what would be sent and exits. Add --json to print each device's raw
API response to stdout.

Reads the Webex token from --token or ~/Personal-Local/config.yml (wxteams.auth_token); needs
spark-admin:devices_write (and spark-admin:devices_read to list/select devices).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from roomos_common import (add_selection_args, confirmed, device_summary, parse_kv,
                           resolve_target_devices, resolve_token, xconfig_patch)


def coerce_value(value: str) -> Any:
    """Send integer-looking values as integers (numeric configs reject strings)."""
    try:
        return int(value)
    except ValueError:
        return value


def build_ops(sets: List[str], removes: List[str]) -> List[Dict[str, Any]]:
    """Turn --set key=value / --remove key flags into a JSON Patch op list."""
    ops: List[Dict[str, Any]] = []
    for key, value in parse_kv(sets, "--set"):
        ops.append({"op": "replace",
                    "path": f"{key}/sources/configured/value",
                    "value": coerce_value(value)})
    for key in removes:
        ops.append({"op": "remove",
                    "path": f"{key.strip()}/sources/configured/value"})
    return ops


def main() -> int:
    """apply xConfiguration changes to selected devices via JSON Patch (cloud only)"""
    ap = argparse.ArgumentParser(
        description="Apply xConfiguration changes (set/remove) to selected RoomOS devices "
                    "via the Webex device configurations API.",
    )
    ap.add_argument("--token", help="Webex access token (omit to read config / prompt)")
    ap.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    add_selection_args(ap)

    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="Set a configured value, e.g. Audio.DefaultVolume=60 "
                         "(case-sensitive key; repeatable)")
    ap.add_argument("--remove", action="append", default=[], metavar="KEY",
                    help="Clear a configured value, reverting it to default (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show the ops and target devices without applying anything")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Skip the confirmation prompt (needed for non-interactive runs)")
    ap.add_argument("--json", action="store_true",
                    help="Print each device's raw API response to stdout")

    args = ap.parse_args()

    try:
        ops = build_ops(args.set, args.remove)
        if not ops:
            print("ERROR: specify at least one --set or --remove change", file=sys.stderr)
            return 2

        token = resolve_token(args.token)
        devices = resolve_target_devices(args, token, args.base_url, args.timeout)
        if not devices:
            print("No devices selected.", file=sys.stderr)
            return 1

        print(f"Config change(s) ({len(ops)}):", file=sys.stderr)
        for op in ops:
            key = op["path"].removesuffix("/sources/configured/value")
            if op["op"] == "replace":
                print(f"  set    {key} = {op['value']}", file=sys.stderr)
            else:
                print(f"  remove {key} (revert to default)", file=sys.stderr)
        print(f"Target device(s) ({len(devices)}):", file=sys.stderr)
        for device in devices:
            print(f"  {device_summary(device)}", file=sys.stderr)

        if args.dry_run:
            print("Dry run -- nothing applied. JSON Patch body:", file=sys.stderr)
            print(json.dumps(ops, indent=2), file=sys.stderr)
            return 0

        if not args.yes:
            try:
                if not confirmed(f"Apply {len(ops)} change(s) to {len(devices)} device(s)?"):
                    print("Aborted.", file=sys.stderr)
                    return 0
            except EOFError:
                print("ERROR: no console available to confirm -- re-run with -y/--yes",
                      file=sys.stderr)
                return 2

        failures = 0
        for index, device in enumerate(devices, 1):
            name = device.get("displayName", device.get("id", ""))
            print(f"  [{index}/{len(devices)}] patching {name}...", file=sys.stderr)
            try:
                result = xconfig_patch(ops, device["id"], token, args.base_url, args.timeout)
                if args.json:
                    print(json.dumps(result, indent=2))
            except Exception as exc:
                failures += 1
                print(f"  ! {name}: {exc}", file=sys.stderr)

        applied = len(devices) - failures
        print(f"Applied to {applied} of {len(devices)} device(s)"
              + (f"; {failures} failed" if failures else "") + ".", file=sys.stderr)
        return 1 if failures else 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
