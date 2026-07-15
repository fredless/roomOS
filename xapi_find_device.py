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
xapi_find_device.py

Select RoomOS devices in your Webex org and print their device ids to stdout, one per line.
Cloud-only. This is the producer half of the fleet-tool pipeline: everything human-readable
(match lists, the interactive pick, progress) goes to stderr, so stdout can be piped straight
into any consumer tool's --stdin:

  xapi_find_device.py --name lobby | xapi_apply_config.py --stdin --set Audio.DefaultVolume=60

Select devices with any combination of:
  --name         display-name search (wildcards allowed); several matches prompt an
                 interactive numbered pick unless --all is given
  --model / --kind / --type / --platform / --connection
                 the same filters as xapi_bulk_query.py; all matches are emitted
  --all          with --name, emit every match instead of picking one; alone, emit
                 every device in the org

The interactive pick works even while stdout is piped (the prompt reads from the console
directly), so pick-then-pipe works in PowerShell and POSIX shells alike.

Reads the Webex token from --token or ~/Personal-Local/config.yml (wxteams.auth_token); needs
admin device scopes (spark-admin:devices_read) to list devices.
"""

from __future__ import annotations

import argparse
import sys

from xapi_common import (add_selection_args, device_summary, resolve_target_devices,
                           resolve_token)


def main() -> int:
    """select org devices by name/filters and print their ids to stdout (cloud only)"""
    ap = argparse.ArgumentParser(
        description="Select RoomOS devices and print their ids to stdout for piping "
                    "into other fleet tools.",
    )
    ap.add_argument("--token", help="Webex access token (omit to read config / prompt)")
    ap.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    add_selection_args(ap)

    args = ap.parse_args()

    try:
        token = resolve_token(args.token)
        devices = resolve_target_devices(args, token, args.base_url, args.timeout)
        if not devices:
            print("No devices selected.", file=sys.stderr)
            return 1

        for device in devices:
            print(f"  -> {device_summary(device)}", file=sys.stderr)
            print(device["id"])
        return 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
