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
  pip install paramiko requests
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
import time
from typing import Dict, Optional, Any, List, Tuple

import paramiko
import requests

KV_RE = re.compile(r"^(?P<k>[^=]+)=(?P<v>.*)$")


def parse_kv(items: List[str]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for item in items:
        m = KV_RE.match(item)
        if not m:
            raise ValueError(f"Invalid --arg '{item}'. Expected key=value.")
        k = m.group("k").strip()
        v = m.group("v").strip()
        if not k:
            raise ValueError(f"Invalid --arg '{item}': empty key.")
        out.append((k, v))
    return out


def _xquote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


# -------------------------
# Local mode: SSH xAPI
# -------------------------

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


def ssh_run_xcommand(host: str, port: int, username: str, password: Optional[str],
                     key_path: Optional[str], xcommand: str, timeout: int) -> str:
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

        chan.send(xcommand.strip() + "\n")
        time.sleep(0.2)
        out = drain(chan)

        # Some devices flush slightly later
        time.sleep(0.2)
        out += drain(chan)

        try:
            chan.send("exit\n")
        except Exception:
            pass

        return out.strip()
    finally:
        client.close()


def build_dial_xcommand(number: str, args: List[Tuple[str, str]]) -> str:
    """
    Build: xCommand Dial Number: "<number>" <Arg1>: "<Val1>" ...
    """
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
    """
    POST https://webexapis.com/v1/xapi/command/Dial
    {
      "deviceId": "...",
      "arguments": { "Number": "...", ... }
    }
    """
    url = f"{base_url.rstrip('/')}/v1/xapi/command/Dial"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    arguments: Dict[str, Any] = {"Number": number}
    for k, v in args:
        # Simple coercion: booleans, ints, floats, else string
        lv = v.lower()
        if lv == "true":
            arguments[k] = True
        elif lv == "false":
            arguments[k] = False
        else:
            # int?
            try:
                if re.fullmatch(r"-?\d+", v):
                    arguments[k] = int(v)
                elif re.fullmatch(r"-?\d+\.\d+", v):
                    arguments[k] = float(v)
                else:
                    arguments[k] = v
            except Exception:
                arguments[k] = v

    payload = {"deviceId": device_id, "arguments": arguments}

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")

    return resp.json() if resp.text.strip() else {"status": "ok"}


def cloud_call_status(device_id: str, token: str, base_url: str,
                      timeout: int) -> Dict[str, Any]:
    """GET https://webexapis.com/v1/xapi/status?deviceId=...&name=Call[*].*"""
    url = f"{base_url.rstrip('/')}/v1/xapi/status"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {"deviceId": device_id, "name": "Call[*].*"}

    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")

    return resp.json() if resp.text.strip() else {"status": "no active calls"}


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
    ap_cloud.add_argument("--device-id", required=True, help="Webex deviceId of the codec")
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

        dial_args = parse_kv(args.arg)

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
            token = args.token or getpass.getpass("Webex Access Token: ")

            if args.status:
                result = cloud_call_status(
                    device_id=args.device_id,
                    token=token,
                    base_url=args.base_url,
                    timeout=args.timeout,
                )
            else:
                result = cloud_dial(
                    device_id=args.device_id,
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