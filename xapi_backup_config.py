#!/usr/bin/env python3
# Copyright (C) 2026 Frederick W. Nielsen
#
# This file is part of xAPI tools.
#
# xAPI tools is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# xAPI tools is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with xAPI tools.  If not, see <https://www.gnu.org/licenses/>.

"""
xapi_backup_config.py

Export the full xConfiguration of one or more RoomOS devices via the Webex device
configurations API. Cloud-only. The output uses the same format as the codec web UI's
backup file ("Audio DefaultVolume: 60" lines, freeform strings quoted, enums bare), so it
feeds straight back into xapi_apply_config.py --file -- a backup/restore pair.

Select the target devices the standard fleet-tool way (see xapi_find_device.py).
A single device prints to stdout by default. Multiple devices always write one file per
device (concatenated backups would be useless), named:

  <displayName>_<serial>_<YYYYMMDD-HHMMSS>.txt

--save [DIR] forces file output even for a single device and/or picks the directory
(default: current directory). Other options:
  --configured-only   export only settings with an explicit configured override -- a
                      minimal "what was changed on this box" template
  --json              raw API items JSON instead of the backup format (.json files)

Reads the Webex token from --token or ~/Personal-Local/config.yml (wxteams.auth_token);
needs spark-admin:devices_read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, TextIO

from xapi_common import (add_selection_args, resolve_target_devices, resolve_token,
                           xconfig_get_items)

_INDEXED_PART_RE = re.compile(r"^(?P<name>.+)\[(?P<idx>\d+)\]$")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def spaced_key(key: str) -> str:
    """Turn an API key back into the space-separated backup form.

    "Audio.Input.HDMI[1].Gain" -> "Audio Input HDMI 1 Gain"
    """
    parts = []
    for part in key.split("."):
        m = _INDEXED_PART_RE.match(part)
        if m:
            parts.extend([m.group("name"), m.group("idx")])
        else:
            parts.append(part)
    return " ".join(parts)


def format_value(value: Any, value_space: Dict[str, Any]) -> str:
    """Render a value the way the codec's own backup file does.

    Booleans and numbers are bare, enum members are bare, freeform strings are quoted
    (that is how the web UI backup distinguishes "-1"-the-string from -1-the-number).
    """
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return str(value)
    if value_space.get("type") == "string" and not value_space.get("enum"):
        return f'"{value}"'
    return str(value)


def export_device(device: Dict[str, Any], out: TextIO, args: argparse.Namespace,
                  token: str) -> int:
    """Write one device's configuration to an open stream; return the settings count."""
    items = xconfig_get_items(device["id"], token, args.base_url, args.timeout)
    count = 0
    if args.json:
        json.dump(items, out, indent=2)
        out.write("\n")
        return len(items)
    for key in sorted(items):
        item = items[key]
        value = item.get("value")
        if value is None:
            continue
        if args.configured_only:
            configured = item.get("sources", {}).get("configured", {}).get("value")
            if configured is None:
                continue
        out.write(f"{spaced_key(key)}: "
                  f"{format_value(value, item.get('valueSpace', {}))}\n")
        count += 1
    return count


def backup_filename(device: Dict[str, Any], stamp: str, json_mode: bool) -> str:
    """Build <displayName>_<serial>_<stamp>.txt with filesystem-hostile characters removed."""
    name = _UNSAFE_FILENAME_RE.sub("_", device.get("displayName") or "device").strip("_")
    serial = device.get("serial") or (device.get("id") or "")[-8:] or "unknown"
    ext = "json" if json_mode else "txt"
    return f"{name}_{serial}_{stamp}.{ext}"


def main() -> int:
    """export the full xConfiguration of selected devices (cloud only)"""
    ap = argparse.ArgumentParser(
        description="Export the full xConfiguration of selected RoomOS devices in the "
                    "codec backup format (restorable with xapi_apply_config.py --file).",
    )
    ap.add_argument("--token", help="Webex access token (omit to read config / prompt)")
    ap.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    add_selection_args(ap)

    ap.add_argument("--save", nargs="?", const=".", metavar="DIR",
                    help="Write per-device backup files into DIR (default '.') instead of "
                         "stdout; implied when several devices are selected")
    ap.add_argument("--configured-only", action="store_true",
                    help="Export only settings with an explicit configured override")
    ap.add_argument("--json", action="store_true",
                    help="Emit the raw API configuration items as JSON")

    args = ap.parse_args()

    try:
        token = resolve_token(args.token)
        devices = resolve_target_devices(args, token, args.base_url, args.timeout)
        if not devices:
            print("No devices selected.", file=sys.stderr)
            return 1

        save_dir = args.save
        if save_dir is None and len(devices) > 1:
            save_dir = "."
            if not args.quiet:
                print("Multiple devices selected -- writing one backup file per device.",
                      file=sys.stderr)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        failures = 0
        for index, device in enumerate(devices, 1):
            name = device.get("displayName", device.get("id", ""))
            if not args.quiet:
                print(f"  [{index}/{len(devices)}] exporting {name}...", file=sys.stderr)
            try:
                if save_dir:
                    path = os.path.join(save_dir, backup_filename(device, stamp, args.json))
                    with open(path, "w", encoding="utf-8", newline="\n") as out:
                        count = export_device(device, out, args, token)
                    # the generated (timestamped) filename is essential output; keep it quiet-proof
                    print(f"    wrote {count} setting(s) to {path}", file=sys.stderr)
                else:
                    count = export_device(device, sys.stdout, args, token)
                    if not args.quiet:
                        print(f"    {count} setting(s)", file=sys.stderr)
            except Exception as exc:
                failures += 1
                print(f"  ! {name}: {exc}", file=sys.stderr)

        if failures:
            print(f"{failures} of {len(devices)} export(s) failed.", file=sys.stderr)
        return 1 if failures else 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
