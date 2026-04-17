#!/usr/bin/env python3
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
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import paramiko
import requests
import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "config.yaml")

# Matches xStatus response lines like:
#   *s Time DateTime: "2026-03-05T14:30:45Z"
STATUS_LINE_RE = re.compile(r"^\*s\s+(?P<path>.+?):\s+(?P<val>.*)\s*$")


# ------------------------------------------------------------------
# Config helpers
# ------------------------------------------------------------------

def load_config(path: str) -> Dict[str, Any]:
    """Load token / device_id from a YAML config file. Returns {} on missing file."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------
# Local mode: SSH xAPI
# ------------------------------------------------------------------

def connect_ssh(host: str, port: int, username: str, password: Optional[str],
                key_path: Optional[str], timeout: int) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    pkey = None
    if key_path:
        last_exc: Optional[Exception] = None
        for key_cls in (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key):
            try:
                pkey = key_cls.from_private_key_file(key_path)
                break
            except Exception as e:
                last_exc = e
        if pkey is None:
            raise RuntimeError(f"Failed to load private key from {key_path}: {last_exc}")

    client.connect(
        hostname=host,
        port=port,
        username=username,
        password=password if not pkey else None,
        pkey=pkey,
        look_for_keys=False,
        allow_agent=False,
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
    )
    return client


def drain(chan, max_reads: int = 50) -> str:
    chunks = []
    reads = 0
    while reads < max_reads and chan.recv_ready():
        data = chan.recv(65535)
        if not data:
            break
        chunks.append(data.decode("utf-8", errors="replace"))
        reads += 1
    return "".join(chunks)


def ssh_get_codec_time(host: str, port: int, username: str, password: Optional[str],
                       key_path: Optional[str], timeout: int) -> str:
    """Query xStatus Time DateTime over SSH and return the raw datetime string."""
    client = connect_ssh(host, port, username, password, key_path, timeout)
    try:
        transport = client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport unavailable")

        chan = transport.open_session()
        chan.get_pty()
        chan.invoke_shell()

        time.sleep(0.2)
        _ = drain(chan)  # banners/prompts

        chan.send("xStatus Time DateTime\n")
        time.sleep(0.3)
        out = drain(chan)
        time.sleep(0.2)
        out += drain(chan)

        try:
            chan.send("exit\n")
        except Exception:
            pass

        # Parse the *s Time DateTime line
        for line in out.splitlines():
            m = STATUS_LINE_RE.match(line)
            if m and "Time" in m.group("path") and "DateTime" in m.group("path"):
                val = m.group("val").strip().strip('"')
                return val

        raise RuntimeError(f"Could not parse codec time from response:\n{out}")
    finally:
        client.close()


# ------------------------------------------------------------------
# Cloud mode: Webex xAPI REST
# ------------------------------------------------------------------

def cloud_get_timezone(device_id: str, token: str, base_url: str,
                       timeout: int) -> str:
    """GET device configuration Time.Zone via Webex Device Configurations API."""
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
    # Get the codec's configured timezone
    try:
        import zoneinfo
    except ImportError:
        from backports import zoneinfo  # type: ignore[no-redef]

    tz_name = cloud_get_timezone(device_id, token, base_url, timeout)

    # Get the codec's local time
    url = f"{base_url.rstrip('/')}/v1/xapi/command/Time.DateTime.Get"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"deviceId": device_id}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")

    data = resp.json() if resp.text.strip() else {}
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
    # RoomOS returns ISO 8601: "2026-03-05T14:30:45Z" or "2026-03-05T14:30:45.123Z"
    # Also handle "+00:00" suffix
    dt_str = dt_str.strip()

    # Try common formats
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
    ap_cloud.add_argument("--config", default=_DEFAULT_CONFIG,
                          help="Path to YAML config file with token/device_id (default: config.yaml beside script)")
    ap_cloud.add_argument("--device-id", help="Webex deviceId of the codec")
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
            cfg = load_config(args.config)
            token = args.token or cfg.get("token") or getpass.getpass("Webex Access Token: ")
            device_id = args.device_id or cfg.get("device_id")
            if not device_id:
                print("ERROR: --device-id is required (via CLI or config.yaml)", file=sys.stderr)
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
