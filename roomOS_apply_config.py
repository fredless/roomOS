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
  --file <path>                     apply every setting from a codec config export
Values that look like integers are sent as integers, everything else as strings.

--file accepts either export format and auto-detects which one it got:
  * the codec web UI's backup file ("Audio DefaultVolume: 60" lines), or
  * a CLI/SSH session dump ("*c xConfiguration Audio DefaultVolume: 50" lines; all other
    session output -- banners, OK/ERROR, xcommand echoes -- is ignored).
Space-separated paths become API keys with instance indexes ("Audio Input HDMI 1 Gain" ->
Audio.Input.HDMI[1].Gain). Quoted values stay strings, unquoted numbers become integers, and
masked secrets ("***") are skipped. Because a JSON Patch is all-or-nothing per device, file
settings are validated against each target device first: keys the device does not have, keys
that are not editable, and keys already at the desired value are dropped (counts reported;
--verbose lists them), and only the remaining changes are patched. --set/--remove are always
applied verbatim on top and override the file for the same key.

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
import os
import re
import sys
from typing import Any, Dict, List, Tuple

from roomos_common import (add_selection_args, confirmed, device_summary, parse_kv,
                           resolve_target_devices, resolve_token, xconfig_get_items,
                           xconfig_patch)

# a config line from an interactive CLI/SSH session dump starts with this marker
_CLI_PREFIX = "*c xConfiguration "

# path tokens in the export formats: PascalCase words, digits (instance indexes), underscores
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")

# strictly digits only -- int() would also accept "3840_2160_30" (underscore separators)
# and silently mangle enum values like resolutions into integers
_INT_RE = re.compile(r"^-?[0-9]+$")


def coerce_value(value: str) -> Any:
    """Send integer values as integers (numeric configs reject strings)."""
    value = value.strip()
    return int(value) if _INT_RE.match(value) else value


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


def dot_key(tokens: List[str]) -> str:
    """Join space-separated path tokens into an API key with instance indexes.

    ["Audio", "Input", "HDMI", "1", "Gain"] -> "Audio.Input.HDMI[1].Gain"
    """
    parts: List[str] = []
    for token in tokens:
        if token.isdigit() and parts:
            parts[-1] += f"[{token}]"
        else:
            parts.append(token)
    return ".".join(parts)


def parse_config_file(path: str) -> Tuple[List[Tuple[str, Any]], int, int]:
    """Parse a codec config export into (key, value) settings.

    Auto-detects the format: if any line carries the '*c xConfiguration' marker the file is
    a CLI session dump and only those lines are read; otherwise it is a web-UI backup and
    every 'Path Words: value' line is read. Quoted values stay strings, unquoted integers
    become ints. Returns (settings, masked_skipped, ignored_lines).
    """
    with open(path, encoding="utf-8-sig") as fh:
        lines = [line.strip() for line in fh]
    cli_mode = any(line.startswith(_CLI_PREFIX) for line in lines)

    settings: List[Tuple[str, Any]] = []
    masked = 0
    ignored = 0
    for line in lines:
        if not line:
            continue
        if cli_mode:
            if not line.startswith(_CLI_PREFIX):
                ignored += 1
                continue
            line = line[len(_CLI_PREFIX):]
        head, sep, raw = line.partition(":")
        tokens = head.split()
        if not sep or not tokens or not all(_TOKEN_RE.match(t) for t in tokens):
            ignored += 1
            continue
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            value: Any = raw[1:-1]
            if value == "***":  # secrets are masked in CLI dumps; never apply the mask
                masked += 1
                continue
        else:
            value = coerce_value(raw)
        settings.append((dot_key(tokens), value))
    return settings, masked, ignored


def validate_file_ops(file_desired: Dict[str, Any], device: Dict[str, Any], token: str,
                      base_url: str, timeout: int,
                      verbose: bool) -> Tuple[List[Dict[str, Any]], str]:
    """Filter file settings against one device's actual configuration set.

    Drops keys the device does not have, keys that are not editable, and keys already at
    the desired value, so the atomic JSON Patch only carries changes that can succeed.
    Returns (ops, summary_text).
    """
    items = xconfig_get_items(device["id"], token, base_url, timeout)
    ops: List[Dict[str, Any]] = []
    missing = readonly = unchanged = 0
    for key, value in file_desired.items():
        item = items.get(key)
        if item is None:
            missing += 1
            if verbose:
                print(f"    - {key}: not on this device", file=sys.stderr)
            continue
        editability = (item.get("sources", {}).get("configured", {})
                       .get("editability", {}))
        if not editability.get("isEditable"):
            readonly += 1
            if verbose:
                print(f"    - {key}: not editable"
                      f" ({editability.get('reason', 'no reason given')})", file=sys.stderr)
            continue
        if str(item.get("value")) == str(value):
            unchanged += 1
            continue
        ops.append({"op": "replace",
                    "path": f"{key}/sources/configured/value",
                    "value": value})
    summary = (f"{len(ops)} to apply, {unchanged} already set, "
               f"{missing} not on device, {readonly} not editable")
    return ops, summary


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
    ap.add_argument("--file", action="append", default=[], metavar="PATH",
                    help="Apply every setting from a codec config export: web UI backup "
                         "or CLI session dump, auto-detected (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be applied per device without changing anything")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Skip the confirmation prompt (needed for non-interactive runs)")
    ap.add_argument("--json", action="store_true",
                    help="Print each device's raw API response to stdout")
    ap.add_argument("--verbose", action="store_true",
                    help="List every file setting skipped per device and why")

    args = ap.parse_args()

    try:
        # settings from --file exports (later files and explicit flags win per key)
        file_desired: Dict[str, Any] = {}
        for path in args.file:
            settings, masked, ignored = parse_config_file(path)
            print(f"{os.path.basename(path)}: {len(settings)} setting(s)"
                  + (f", {masked} masked secret(s) skipped" if masked else "")
                  + (f", {ignored} non-config line(s) ignored" if ignored else ""),
                  file=sys.stderr)
            file_desired.update(settings)

        explicit_ops = build_ops(args.set, args.remove)
        for op in explicit_ops:
            file_desired.pop(op["path"].removesuffix("/sources/configured/value"), None)

        if not file_desired and not explicit_ops:
            print("ERROR: specify at least one --set, --remove, or --file change",
                  file=sys.stderr)
            return 2

        token = resolve_token(args.token)
        devices = resolve_target_devices(args, token, args.base_url, args.timeout)
        if not devices:
            print("No devices selected.", file=sys.stderr)
            return 1

        if file_desired:
            print(f"File setting(s): {len(file_desired)} "
                  "(validated against each device before applying)", file=sys.stderr)
        if explicit_ops:
            print(f"Explicit change(s) ({len(explicit_ops)}):", file=sys.stderr)
            for op in explicit_ops:
                key = op["path"].removesuffix("/sources/configured/value")
                if op["op"] == "replace":
                    print(f"  set    {key} = {op['value']}", file=sys.stderr)
                else:
                    print(f"  remove {key} (revert to default)", file=sys.stderr)
        print(f"Target device(s) ({len(devices)}):", file=sys.stderr)
        for device in devices:
            print(f"  {device_summary(device)}", file=sys.stderr)

        if not args.dry_run and not args.yes:
            try:
                total = len(file_desired) + len(explicit_ops)
                if not confirmed(f"Apply up to {total} change(s) to "
                                 f"{len(devices)} device(s)?"):
                    print("Aborted.", file=sys.stderr)
                    return 0
            except EOFError:
                print("ERROR: no console available to confirm -- re-run with -y/--yes",
                      file=sys.stderr)
                return 2

        failures = 0
        for index, device in enumerate(devices, 1):
            name = device.get("displayName", device.get("id", ""))
            verb = "checking" if args.dry_run else "patching"
            print(f"  [{index}/{len(devices)}] {verb} {name}...", file=sys.stderr)
            try:
                ops = list(explicit_ops)
                if file_desired:
                    file_ops, summary = validate_file_ops(
                        file_desired, device, token, args.base_url, args.timeout,
                        args.verbose)
                    print(f"    file settings: {summary}", file=sys.stderr)
                    ops = file_ops + ops
                if not ops:
                    print("    nothing to change.", file=sys.stderr)
                    continue
                if args.dry_run:
                    print(f"    would apply {len(ops)} change(s):", file=sys.stderr)
                    for op in ops:
                        key = op["path"].removesuffix("/sources/configured/value")
                        if op["op"] == "replace":
                            print(f"      set    {key} = {op['value']}", file=sys.stderr)
                        else:
                            print(f"      remove {key}", file=sys.stderr)
                    continue
                result = xconfig_patch(ops, device["id"], token, args.base_url, args.timeout)
                print(f"    applied {len(ops)} change(s).", file=sys.stderr)
                if args.json:
                    print(json.dumps(result, indent=2))
            except Exception as exc:
                failures += 1
                print(f"  ! {name}: {exc}", file=sys.stderr)

        if args.dry_run:
            print("Dry run -- nothing applied.", file=sys.stderr)
        else:
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
