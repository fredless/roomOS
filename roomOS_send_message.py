#!/usr/bin/env python3
"""
roomos_message_send.py

Send a macro bus message:
  xCommand Message Send Text: "..." [Key: "..." Value: "..."]...

Triggers Event/Message/Send on the codec (macros can subscribe via xFeedback register Event/Message/Send).

Modes:
  local  - SSH into device and run xCommand
  cloud  - Webex Cloud xAPI REST: POST /v1/xapi/command/Message.Send

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
            raise ValueError(f"Invalid --kv '{item}'. Expected key=value.")
        k = m.group("k").strip()
        v = m.group("v").strip()
        if not k:
            raise ValueError(f"Invalid --kv '{item}': empty key.")
        out.append((k, v))
    return out


def _xquote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def build_message_send_xcommand(text: str, kv_pairs: List[Tuple[str, str]]) -> str:
    parts = ["xCommand", "Message", "Send", "Text:", _xquote(text)]
    for k, v in kv_pairs:
        parts += ["Key:", _xquote(k), "Value:", _xquote(v)]
    return " ".join(parts)


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
        time.sleep(0.15)
        out = drain(chan)

        time.sleep(0.15)
        out += drain(chan)

        try:
            chan.send("exit\n")
        except Exception:
            pass

        return out.strip()
    finally:
        client.close()


# -------------------------
# Cloud mode: Webex xAPI REST
# -------------------------

def cloud_message_send(device_id: str, token: str, text: str, kv_pairs: List[Tuple[str, str]],
                       base_url: str, timeout: int) -> Dict[str, Any]:
    """
    POST https://webexapis.com/v1/xapi/command/Message.Send
    {
      "deviceId": "...",
      "arguments": {
        "Text": "...",
        "Key": ["k1","k2"],
        "Value": ["v1","v2"]
      }
    }
    """
    url = f"{base_url.rstrip('/')}/v1/xapi/command/Message.Send"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    arguments: Dict[str, Any] = {"Text": text}
    if kv_pairs:
        # Mapping repeated Key/Value pairs into arrays is the cleanest REST representation.
        arguments["Key"] = [k for k, _ in kv_pairs]
        arguments["Value"] = [v for _, v in kv_pairs]

    payload = {"deviceId": device_id, "arguments": arguments}

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")

    return resp.json() if resp.text.strip() else {"status": "ok"}


# -------------------------
# CLI
# -------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Send RoomOS macro bus messages (xCommand Message Send).")
    sub = ap.add_subparsers(dest="mode", required=True)

    ap_local = sub.add_parser("local", help="Send via local SSH xAPI")
    ap_local.add_argument("--host", required=True, help="Codec IP/hostname")
    ap_local.add_argument("-P", "--port", type=int, default=22, help="SSH port (default: 22)")
    ap_local.add_argument("-u", "--username", required=True, help="SSH username")
    ap_local.add_argument("-p", "--password", help="SSH password (omit to prompt)")
    ap_local.add_argument("-k", "--key", dest="key_path", help="SSH private key path (optional)")
    ap_local.add_argument("--timeout", type=int, default=10, help="SSH timeout seconds (default: 10)")

    ap_cloud = sub.add_parser("cloud", help="Send via Webex Cloud xAPI REST")
    ap_cloud.add_argument("--device-id", required=True, help="Webex deviceId of the codec")
    ap_cloud.add_argument("--token", help="Webex access token (omit to prompt)")
    ap_cloud.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap_cloud.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds (default: 15)")

    for p in (ap_local, ap_cloud):
        p.add_argument("--text", required=True, help="Message text (Text: ...)")
        p.add_argument("--kv", action="append", default=[], help="Optional key=value pair (repeatable)")

    args = ap.parse_args()

    try:
        kv_pairs = parse_kv(args.kv)

        if args.mode == "local":
            if not args.password and not args.key_path:
                args.password = getpass.getpass("SSH Password: ")
            cmd = build_message_send_xcommand(args.text, kv_pairs)
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
            result = cloud_message_send(
                device_id=args.device_id,
                token=token,
                text=args.text,
                kv_pairs=kv_pairs,
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