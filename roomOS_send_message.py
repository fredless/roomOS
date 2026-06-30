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
  pip install paramiko requests pyyaml
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Dict, List, Tuple

from roomos_common import (load_config, parse_kv, ssh_run_xcommand, xapi_command,
                           xquote as _xquote)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "config.yaml")


def build_message_send_xcommand(text: str, kv_pairs: List[Tuple[str, str]]) -> str:
    parts = ["xCommand", "Message", "Send", "Text:", _xquote(text)]
    for k, v in kv_pairs:
        parts += ["Key:", _xquote(k), "Value:", _xquote(v)]
    return " ".join(parts)


# -------------------------
# Cloud mode: Webex xAPI REST
# -------------------------

def cloud_message_send(device_id: str, token: str, text: str, kv_pairs: List[Tuple[str, str]],
                       base_url: str, timeout: int) -> Dict[str, Any]:
    """POST /v1/xapi/command/Message.Send with Text and parallel Key/Value arrays."""
    arguments: Dict[str, Any] = {"Text": text}
    if kv_pairs:
        # Mapping repeated Key/Value pairs into arrays is the cleanest REST representation.
        arguments["Key"] = [k for k, _ in kv_pairs]
        arguments["Value"] = [v for _, v in kv_pairs]
    return xapi_command("Message.Send", device_id, token, arguments, base_url, timeout)


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
    ap_cloud.add_argument("--config", default=_DEFAULT_CONFIG,
                          help="Path to YAML config file with token/device_id (default: config.yaml beside script)")
    ap_cloud.add_argument("--device-id", help="Webex deviceId of the codec")
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
            cfg = load_config(args.config)
            token = args.token or cfg.get("token") or getpass.getpass("Webex Access Token: ")
            device_id = args.device_id or cfg.get("device_id")
            if not device_id:
                print("ERROR: --device-id is required (via CLI or config.yaml)", file=sys.stderr)
                return 2
            result = cloud_message_send(
                device_id=device_id,
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
