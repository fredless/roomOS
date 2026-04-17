#!/usr/bin/env python3
"""
roomos_notice.py

Display or clear on-screen notices on a Cisco RoomOS codec.

Supports two notice types:
  alert    - popup dialog (xCommand UserInterface Message Alert Display/Clear)
  textline - half-screen overlay (xCommand UserInterface Message TextLine Display/Clear)

Duration behaviour:
  0  = persistent (alert stays until dismissed/cleared; textline stays until cleared)
  >0 = auto-dismiss after that many seconds

Modes:
  local  - SSH into device and run xCommand
  cloud  - Webex Cloud xAPI REST

Deps:
  pip install paramiko requests pyyaml
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from typing import Any, Dict, Optional

import paramiko
import requests
import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "config.yaml")


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


def _xquote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


# ------------------------------------------------------------------
# xCommand builders (local SSH)
# ------------------------------------------------------------------

def build_alert_display_xcommand(title: str, text: str, duration: int) -> str:
    parts = [
        "xCommand", "UserInterface", "Message", "Alert", "Display",
        "Title:", _xquote(title),
        "Text:", _xquote(text),
        "Duration:", str(duration),
    ]
    return " ".join(parts)


def build_alert_clear_xcommand() -> str:
    return "xCommand UserInterface Message Alert Clear"


def build_textline_display_xcommand(
    text: str, duration: int,
    x: Optional[int] = None, y: Optional[int] = None,
) -> str:
    parts = [
        "xCommand", "UserInterface", "Message", "TextLine", "Display",
        "Text:", _xquote(text),
    ]
    if x is not None:
        parts += ["X:", str(x)]
    if y is not None:
        parts += ["Y:", str(y)]
    parts += ["Duration:", str(duration)]
    return " ".join(parts)


def build_textline_clear_xcommand() -> str:
    return "xCommand UserInterface Message TextLine Clear"


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


# ------------------------------------------------------------------
# Cloud mode: Webex xAPI REST
# ------------------------------------------------------------------

def cloud_alert_display(device_id: str, token: str, title: str, text: str,
                        duration: int, base_url: str, timeout: int) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/xapi/command/UserInterface.Message.Alert.Display"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "deviceId": device_id,
        "arguments": {"Title": title, "Text": text, "Duration": duration},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")
    return resp.json() if resp.text.strip() else {"status": "ok"}


def cloud_alert_clear(device_id: str, token: str, base_url: str,
                      timeout: int) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/xapi/command/UserInterface.Message.Alert.Clear"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"deviceId": device_id}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")
    return resp.json() if resp.text.strip() else {"status": "ok"}


def cloud_textline_display(device_id: str, token: str, text: str, duration: int,
                           x: Optional[int], y: Optional[int],
                           base_url: str, timeout: int) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/xapi/command/UserInterface.Message.TextLine.Display"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    arguments: Dict[str, Any] = {"Text": text, "Duration": duration}
    if x is not None:
        arguments["X"] = x
    if y is not None:
        arguments["Y"] = y
    payload = {"deviceId": device_id, "arguments": arguments}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")
    return resp.json() if resp.text.strip() else {"status": "ok"}


def cloud_textline_clear(device_id: str, token: str, base_url: str,
                         timeout: int) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v1/xapi/command/UserInterface.Message.TextLine.Clear"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"deviceId": device_id}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")
    return resp.json() if resp.text.strip() else {"status": "ok"}


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Display or clear on-screen notices on a RoomOS codec "
                    "(alerts and text lines) via local SSH or Webex Cloud xAPI.",
    )
    sub = ap.add_subparsers(dest="mode", required=True)

    # ---- local subparser ----
    ap_local = sub.add_parser("local", help="Send notice via local SSH xAPI")
    ap_local.add_argument("--host", required=True, help="Codec IP/hostname")
    ap_local.add_argument("-P", "--port", type=int, default=22, help="SSH port (default: 22)")
    ap_local.add_argument("-u", "--username", required=True, help="SSH username")
    ap_local.add_argument("-p", "--password", help="SSH password (omit to prompt)")
    ap_local.add_argument("-k", "--key", dest="key_path", help="SSH private key path (optional)")
    ap_local.add_argument("--timeout", type=int, default=10, help="SSH timeout seconds (default: 10)")

    # ---- cloud subparser ----
    ap_cloud = sub.add_parser("cloud", help="Send notice via Webex Cloud xAPI REST")
    ap_cloud.add_argument("--config", default=_DEFAULT_CONFIG,
                          help="Path to YAML config file with token/device_id (default: config.yaml beside script)")
    ap_cloud.add_argument("--device-id", help="Webex deviceId of the codec")
    ap_cloud.add_argument("--token", help="Webex access token (omit to prompt)")
    ap_cloud.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap_cloud.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds (default: 15)")

    # ---- action subparsers shared across both modes ----
    for p in (ap_local, ap_cloud):
        action_sub = p.add_subparsers(dest="action", required=True)

        p_display = action_sub.add_parser("display", help="Display a notice on screen")
        p_display.add_argument("--type", required=True, choices=["alert", "textline"],
                               help="Notice type: alert (popup) or textline (overlay)")
        p_display.add_argument("--title", default="", help="Alert title (alert type only)")
        p_display.add_argument("--text", required=True, help="Notice text content")
        p_display.add_argument("--duration", type=int, default=0,
                               help="Display duration in seconds (0=persistent, default: 0)")
        p_display.add_argument("--x", type=int, default=None,
                               help="X position on screen (textline only, optional)")
        p_display.add_argument("--y", type=int, default=None,
                               help="Y position on screen (textline only, optional)")

        p_clear = action_sub.add_parser("clear", help="Clear a notice from screen")
        p_clear.add_argument("--type", required=True, choices=["alert", "textline"],
                             help="Notice type to clear: alert or textline")

    args = ap.parse_args()

    try:
        # ---- Local mode ----
        if args.mode == "local":
            if not args.password and not args.key_path:
                args.password = getpass.getpass("SSH Password: ")

            if args.action == "display":
                if args.type == "alert":
                    cmd = build_alert_display_xcommand(args.title, args.text, args.duration)
                else:
                    cmd = build_textline_display_xcommand(args.text, args.duration, args.x, args.y)
            else:  # clear
                if args.type == "alert":
                    cmd = build_alert_clear_xcommand()
                else:
                    cmd = build_textline_clear_xcommand()

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

        # ---- Cloud mode ----
        if args.mode == "cloud":
            cfg = load_config(args.config)
            token = args.token or cfg.get("token") or getpass.getpass("Webex Access Token: ")
            device_id = args.device_id or cfg.get("device_id")
            if not device_id:
                print("ERROR: --device-id is required (via CLI or config.yaml)", file=sys.stderr)
                return 2

            if args.action == "display":
                if args.type == "alert":
                    result = cloud_alert_display(
                        device_id=device_id, token=token,
                        title=args.title, text=args.text,
                        duration=args.duration,
                        base_url=args.base_url, timeout=args.timeout,
                    )
                else:
                    result = cloud_textline_display(
                        device_id=device_id, token=token,
                        text=args.text, duration=args.duration,
                        x=args.x, y=args.y,
                        base_url=args.base_url, timeout=args.timeout,
                    )
            else:  # clear
                if args.type == "alert":
                    result = cloud_alert_clear(
                        device_id=device_id, token=token,
                        base_url=args.base_url, timeout=args.timeout,
                    )
                else:
                    result = cloud_textline_clear(
                        device_id=device_id, token=token,
                        base_url=args.base_url, timeout=args.timeout,
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
