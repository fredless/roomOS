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
roomos_clock_sync.py

Sync the local PC clock from a Cisco RoomOS codec's clock.

Queries xStatus Time DateTime on the codec, compares with local time,
and optionally sets the local system clock to match.

NOTE: Setting the system clock requires elevated privileges:
  Windows - Run as Administrator
  Linux   - Run as root or with sudo

Modes:
  local  - SSH into device and run xStatus
  cloud  - Webex Cloud xAPI REST

Deps:
  pip install paramiko requests pyyaml
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

import requests

from roomos_common import resolve_device_id, resolve_token, ssh_run_xcommands, xapi_command

# Matches xStatus response lines like:
#   *s Time DateTime: "2026-03-05T14:30:45Z"
STATUS_LINE_RE = re.compile(r"^\*s\s+(?P<path>.+?):\s+(?P<val>.*)\s*$")


# ------------------------------------------------------------------
# Local mode: SSH xAPI
# ------------------------------------------------------------------

def ssh_get_codec_time(host: str, port: int, username: str, password,
                       key_path, timeout: int) -> str:
    """Query xStatus Time DateTime over SSH and return the raw datetime string."""
    out = ssh_run_xcommands(host, port, username, password, key_path,
                            ["xStatus Time DateTime"], timeout)
    for line in out.splitlines():
        m = STATUS_LINE_RE.match(line)
        if m and "Time" in m.group("path") and "DateTime" in m.group("path"):
            return m.group("val").strip().strip('"')
    raise RuntimeError(f"Could not parse codec time from response:\n{out}")


# ------------------------------------------------------------------
# Cloud mode: Webex xAPI REST
# ------------------------------------------------------------------

def cloud_get_timezone(device_id: str, token: str, base_url: str,
                       timeout: int) -> str:
    """GET device configuration Time.Zone via the Webex Device Configurations API.

    Not an xAPI command/status call, so this uses the deviceConfigurations endpoint directly.
    """
    url = f"{base_url.rstrip('/')}/v1/deviceConfigurations"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {"deviceId": device_id, "key": "Time.Zone"}
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud device config query failed: HTTP {resp.status_code} - {resp.text}")

    data = resp.json() if resp.text.strip() else {}

    # Response structure:
    #   {"items": {"Time.Zone": {"value": "America/New_York", "source": "configured", ...}}}
    items = data.get("items", {})
    if isinstance(items, dict):
        tz_obj = items.get("Time.Zone", {})
        if isinstance(tz_obj, dict):
            val = tz_obj.get("value", "")
            if val:
                return str(val).strip().strip('"')

    raise RuntimeError(f"Could not determine codec timezone from response:\n{json.dumps(data, indent=2)}")


def cloud_get_codec_time(device_id: str, token: str, base_url: str,
                         timeout: int) -> str:
    """Query codec local time and timezone, return UTC datetime string."""
    try:
        import zoneinfo
    except ImportError:
        from backports import zoneinfo  # type: ignore[no-redef]

    tz_name = cloud_get_timezone(device_id, token, base_url, timeout)

    data = xapi_command("Time.DateTime.Get", device_id, token, {}, base_url, timeout)
    result = data.get("result", data)

    if "Year" not in result:
        raise RuntimeError(f"Could not parse codec time from response:\n{json.dumps(data, indent=2)}")

    # Build a timezone-aware local datetime, then convert to UTC
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except (KeyError, Exception) as exc:
        raise RuntimeError(f"Unknown codec timezone '{tz_name}': {exc}") from exc

    codec_local = datetime(
        year=int(result["Year"]),
        month=int(result["Month"]),
        day=int(result["Day"]),
        hour=int(result["Hour"]),
        minute=int(result["Minute"]),
        second=int(result["Second"]),
        tzinfo=tz,
    )

    codec_utc = codec_local.astimezone(timezone.utc)
    return codec_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------
# Time parsing and sync
# ------------------------------------------------------------------

def parse_codec_time(dt_str: str) -> datetime:
    """Parse a RoomOS datetime string into a timezone-aware UTC datetime."""
    dt_str = dt_str.strip()

    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
    ):
        try:
            dt = datetime.strptime(dt_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    raise ValueError(f"Unable to parse codec datetime: {dt_str!r}")


def set_system_time(dt_utc: datetime) -> None:
    """Set the local system clock. Requires elevated privileges."""
    if sys.platform == "win32":
        # Convert UTC to local time for Windows Set-Date
        dt_local = dt_utc.astimezone()
        ps_date = dt_local.strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["powershell", "-Command", f"Set-Date -Date '{ps_date}'"],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        # Linux/macOS: date -s expects UTC string
        utc_str = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(
            ["date", "-u", "-s", utc_str],
            check=True,
            capture_output=True,
            text=True,
        )


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main() -> int:
    """sync the local clock from a RoomOS codec's clock (local SSH or cloud xAPI)"""
    ap = argparse.ArgumentParser(
        description="Sync local PC clock from a RoomOS codec's clock "
                    "via local SSH or Webex Cloud xAPI.",
    )
    sub = ap.add_subparsers(dest="mode", required=True)

    # ---- local subparser ----
    ap_local = sub.add_parser("local", help="Query codec time via local SSH xAPI")
    ap_local.add_argument("--host", required=True, help="Codec IP/hostname")
    ap_local.add_argument("-P", "--port", type=int, default=22, help="SSH port (default: 22)")
    ap_local.add_argument("-u", "--username", required=True, help="SSH username")
    ap_local.add_argument("-p", "--password", help="SSH password (omit to prompt)")
    ap_local.add_argument("-k", "--key", dest="key_path", help="SSH private key path (optional)")
    ap_local.add_argument("--timeout", type=int, default=10, help="SSH timeout seconds (default: 10)")

    # ---- cloud subparser ----
    ap_cloud = sub.add_parser("cloud", help="Query codec time via Webex Cloud xAPI REST")
    ap_cloud.add_argument("--device-id", help="Webex deviceId of the codec (or set ROOMOS_DEVICE_ID)")
    ap_cloud.add_argument("--token", help="Webex access token (omit to prompt)")
    ap_cloud.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap_cloud.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds (default: 15)")

    # ---- shared args ----
    for p in (ap_local, ap_cloud):
        p.add_argument("--dry-run", action="store_true",
                       help="Show offset only, do not set system clock")
        p.add_argument("--force", action="store_true",
                       help="Set clock without confirmation prompt")

    args = ap.parse_args()

    try:
        # ---- Get codec time ----
        if args.mode == "local":
            if not args.password and not args.key_path:
                args.password = getpass.getpass("SSH Password: ")
            codec_time_str = ssh_get_codec_time(
                host=args.host, port=args.port,
                username=args.username, password=args.password,
                key_path=args.key_path, timeout=args.timeout,
            )
        else:  # cloud
            token = resolve_token(args.token)
            device_id = resolve_device_id(args.device_id)
            if not device_id:
                print("ERROR: device id required: pass --device-id or set ROOMOS_DEVICE_ID",
                      file=sys.stderr)
                return 2
            codec_time_str = cloud_get_codec_time(
                device_id=device_id, token=token,
                base_url=args.base_url, timeout=args.timeout,
            )

        # ---- Compare times ----
        codec_dt = parse_codec_time(codec_time_str)
        local_dt = datetime.now(timezone.utc)
        offset = (codec_dt - local_dt).total_seconds()

        print(f"Codec time (UTC):  {codec_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
        print(f"Local time (UTC):  {local_dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
        print(f"Offset:            {offset:+.3f}s")

        if args.dry_run:
            return 0

        # ---- Set system clock ----
        if abs(offset) < 0.5:
            print("\nClocks are within 0.5s — no adjustment needed.")
            return 0

        if not args.force:
            try:
                answer = input(f"\nSet local clock to codec time ({offset:+.3f}s adjustment)? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 0

        set_system_time(codec_dt)
        new_local = datetime.now(timezone.utc)
        print(f"\nClock updated. New local time (UTC): {new_local.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

        return 0

    except subprocess.CalledProcessError as e:
        stderr_msg = e.stderr.strip() if e.stderr else ""
        if "privilege" in stderr_msg.lower() or "denied" in stderr_msg.lower() or "administrator" in stderr_msg.lower():
            print("ERROR: Setting system clock requires elevated privileges.", file=sys.stderr)
            print("  Windows: Run as Administrator", file=sys.stderr)
            print("  Linux:   Run with sudo", file=sys.stderr)
        else:
            print(f"ERROR: Failed to set clock: {stderr_msg or e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
