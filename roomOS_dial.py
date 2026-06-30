#!/usr/bin/env python3
"""
roomos_dial.py

Place a call or check call status on a Cisco RoomOS codec using either:
  - local xAPI over SSH
  - Webex Cloud xAPI REST

Dial (local):
  xCommand Dial Number: "<destination>" [CallType: Video|Audio] [Protocol: SIP|Spark|...]

Dial (cloud):
  POST https://webexapis.com/v1/xapi/command/Dial

Status (--status):
  Local:  xStatus Call
  Cloud:  GET https://webexapis.com/v1/xapi/status?name=Call

Deps:
  pip install paramiko requests pyyaml
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from typing import Any, Dict, List, Tuple

from roomos_common import (parse_kv, resolve_device_id, resolve_token, ssh_run_xcommand,
                           xapi_command, xapi_status, xquote as _xquote)


def build_dial_xcommand(number: str, args: List[Tuple[str, str]]) -> str:
    """Build: xCommand Dial Number: "<number>" <Arg1>: "<Val1>" ..."""
    parts = ["xCommand", "Dial", "Number:", _xquote(number)]
    for k, v in args:
        # treat numeric values as-is if they look numeric, else quote
        vv = v
        if not _looks_number(v) and v.lower() not in ("true", "false"):
            vv = _xquote(v)
        parts += [f"{k}:", vv]
    return " ".join(parts)


def _looks_number(v: str) -> bool:
    try:
        float(v)
        return True
    except Exception:
        return False


# -------------------------
# Cloud mode: Webex xAPI REST
# -------------------------

def cloud_dial(device_id: str, token: str, number: str, args: List[Tuple[str, str]],
               base_url: str, timeout: int) -> Dict[str, Any]:
    """POST /v1/xapi/command/Dial with Number plus coerced extra arguments."""
    arguments: Dict[str, Any] = {"Number": number}
    for k, v in args:
        # Simple coercion: booleans, ints, floats, else string
        lv = v.lower()
        if lv == "true":
            arguments[k] = True
        elif lv == "false":
            arguments[k] = False
        else:
            try:
                if re.fullmatch(r"-?\d+", v):
                    arguments[k] = int(v)
                elif re.fullmatch(r"-?\d+\.\d+", v):
                    arguments[k] = float(v)
                else:
                    arguments[k] = v
            except Exception:
                arguments[k] = v

    return xapi_command("Dial", device_id, token, arguments, base_url, timeout)


def cloud_call_status(device_id: str, token: str, base_url: str,
                      timeout: int) -> Dict[str, Any]:
    """GET /v1/xapi/status?deviceId=...&name=Call[*].*"""
    return xapi_status("Call[*].*", device_id, token, base_url, timeout,
                       empty_default={"status": "no active calls"})


# -------------------------
# CLI
# -------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Place a call from a RoomOS codec (xCommand Dial) via local SSH or Webex Cloud xAPI.")
    sub = ap.add_subparsers(dest="mode", required=True)

    ap_local = sub.add_parser("local", help="Dial via local SSH xAPI")
    ap_local.add_argument("--host", required=True, help="Codec IP/hostname")
    ap_local.add_argument("-P", "--port", type=int, default=22, help="SSH port (default: 22)")
    ap_local.add_argument("-u", "--username", required=True, help="SSH username")
    ap_local.add_argument("-p", "--password", help="SSH password (omit to prompt)")
    ap_local.add_argument("-k", "--key", dest="key_path", help="SSH private key path (optional)")
    ap_local.add_argument("--timeout", type=int, default=10, help="SSH timeout seconds (default: 10)")

    ap_cloud = sub.add_parser("cloud", help="Dial via Webex Cloud xAPI REST")
    ap_cloud.add_argument("--device-id", help="Webex deviceId of the codec (or set ROOMOS_DEVICE_ID)")
    ap_cloud.add_argument("--token", help="Webex access token (omit to prompt)")
    ap_cloud.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap_cloud.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds (default: 15)")

    for p in (ap_local, ap_cloud):
        p.add_argument("--number", help="Dial string / destination (SIP URI, number, etc.)")
        p.add_argument("--arg", action="append", default=[],
                       help="Optional Dial argument key=value (repeatable), e.g. --arg CallType=Video --arg Protocol=SIP")
        p.add_argument("--status", action="store_true",
                       help="Show current call status (xStatus Call) instead of dialing")

    args = ap.parse_args()

    try:
        if not args.status and not args.number:
            print("ERROR: --number is required when not using --status", file=sys.stderr)
            return 2

        dial_args = parse_kv(args.arg, "--arg")

        if args.mode == "local":
            if not args.password and not args.key_path:
                args.password = getpass.getpass("SSH Password: ")

            if args.status:
                out = ssh_run_xcommand(
                    host=args.host,
                    port=args.port,
                    username=args.username,
                    password=args.password,
                    key_path=args.key_path,
                    xcommand="xStatus Call",
                    timeout=args.timeout,
                )
            else:
                cmd = build_dial_xcommand(args.number, dial_args)
                out = ssh_run_xcommand(
                    host=args.host,
                    port=args.port,
                    username=args.username,
                    password=args.password,
                    key_path=args.key_path,
                    xcommand=cmd,
                    timeout=args.timeout,
                )
            if out:
                print(out)
            return 0

        if args.mode == "cloud":
            token = resolve_token(args.token)
            device_id = resolve_device_id(args.device_id)
            if not device_id:
                print("ERROR: device id required: pass --device-id or set ROOMOS_DEVICE_ID",
                      file=sys.stderr)
                return 2

            if args.status:
                result = cloud_call_status(
                    device_id=device_id,
                    token=token,
                    base_url=args.base_url,
                    timeout=args.timeout,
                )
            else:
                result = cloud_dial(
                    device_id=device_id,
                    token=token,
                    number=args.number,
                    args=dial_args,
                    base_url=args.base_url,
                    timeout=args.timeout,
                )
            print(json.dumps(result, indent=2))
            return 0

        return 2

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
