#!/usr/bin/env python3
"""
roomos_ethernet_mics.py

Enumerate ethernet microphone input streams on a Cisco RoomOS codec.

Queries two xStatus paths:
  1. Peripherals ConnectedDevice  - serial, MAC, type, name, connection status
     (filtered to microphone types)
  2. Audio Input Connectors Ethernet - stream-level detail for ethernet audio inputs

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
import sys
import time
from typing import Any, Dict, List, Optional

import paramiko
import requests
import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "config.yaml")

# Matches xStatus response lines like:
#   *s Peripherals ConnectedDevice 1 Name: "Cisco Table Microphone"
STATUS_LINE_RE = re.compile(r"^\*s\s+(?P<path>.+?):\s+(?P<val>.*)\s*$")

END_MARKER = "** end"


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


def _clean_value(v: str) -> str:
    """Strip whitespace and surrounding quotes from an xStatus value."""
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    return v


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


def ssh_run_xstatus(host: str, port: int, username: str, password: Optional[str],
                    key_path: Optional[str], commands: List[str],
                    timeout: int) -> str:
    """Run one or more xStatus commands over a single SSH session and return combined output."""
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

        out = ""
        for cmd in commands:
            chan.send(cmd.strip() + "\n")
            time.sleep(0.3)
            out += drain(chan)
            time.sleep(0.2)
            out += drain(chan)

        try:
            chan.send("exit\n")
        except Exception:
            pass

        return out.strip()
    finally:
        client.close()


# ------------------------------------------------------------------
# Parsing xStatus output (local mode)
# ------------------------------------------------------------------

def parse_peripherals(raw: str) -> List[Dict[str, str]]:
    """Parse *s Peripherals ConnectedDevice lines into a list of device dicts."""
    # Group by device index: Peripherals ConnectedDevice <N> <Property>
    device_re = re.compile(
        r"^Peripherals\s+ConnectedDevice\s+(?P<idx>\d+)\s+(?P<prop>.+)$"
    )
    devices: Dict[int, Dict[str, str]] = {}

    for line in raw.splitlines():
        m = STATUS_LINE_RE.match(line)
        if not m:
            continue
        path = m.group("path").strip()
        val = _clean_value(m.group("val"))

        dm = device_re.match(path)
        if dm:
            idx = int(dm.group("idx"))
            prop = dm.group("prop").strip()
            devices.setdefault(idx, {})[prop] = val

    # Filter to microphone types
    mics = []
    for idx in sorted(devices):
        dev = devices[idx]
        dev_type = dev.get("Type", "")
        dev_name = dev.get("Name", "")
        if "microphone" in dev_type.lower() or "microphone" in dev_name.lower():
            dev["_index"] = str(idx)
            mics.append(dev)

    return mics


def parse_ethernet_inputs(raw: str) -> List[Dict[str, str]]:
    """Parse *s Audio Input Connectors Ethernet lines into a list of connector dicts."""
    connector_re = re.compile(
        r"^Audio\s+Input\s+Connectors\s+Ethernet\s+(?P<idx>\d+)\s+(?P<prop>.+)$"
    )
    connectors: Dict[int, Dict[str, str]] = {}

    for line in raw.splitlines():
        m = STATUS_LINE_RE.match(line)
        if not m:
            continue
        path = m.group("path").strip()
        val = _clean_value(m.group("val"))

        cm = connector_re.match(path)
        if cm:
            idx = int(cm.group("idx"))
            prop = cm.group("prop").strip()
            connectors.setdefault(idx, {})[prop] = val

    return [connectors[idx] for idx in sorted(connectors)]


# ------------------------------------------------------------------
# Cloud mode: Webex xAPI REST
# ------------------------------------------------------------------

def cloud_get_status(device_id: str, token: str, name: str,
                     base_url: str, timeout: int) -> Dict[str, Any]:
    """GET /v1/xapi/status?deviceId=...&name=..."""
    url = f"{base_url.rstrip('/')}/v1/xapi/status"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {"deviceId": device_id, "name": name}
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")
    return resp.json() if resp.text.strip() else {}


def extract_cloud_peripherals(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract microphone peripherals from cloud status response."""
    result = data.get("result", data)

    # Navigate into Peripherals -> ConnectedDevice
    peripherals = result.get("Peripherals", result)
    devices_raw = peripherals.get("ConnectedDevice", [])

    if isinstance(devices_raw, dict):
        devices_raw = [devices_raw]

    mics = []
    for dev in devices_raw:
        if not isinstance(dev, dict):
            continue
        dev_type = str(dev.get("Type", ""))
        dev_name = str(dev.get("Name", ""))
        if "microphone" in dev_type.lower() or "microphone" in dev_name.lower():
            mics.append(dev)

    return mics


def extract_cloud_ethernet_inputs(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract ethernet audio input connectors from cloud status response."""
    result = data.get("result", data)

    audio = result.get("Audio", result)
    inputs = audio.get("Input", audio)
    connectors = inputs.get("Connectors", inputs)
    ethernet_raw = connectors.get("Ethernet", [])

    if isinstance(ethernet_raw, dict):
        ethernet_raw = [ethernet_raw]

    return [e for e in ethernet_raw if isinstance(e, dict)]


# ------------------------------------------------------------------
# Display helpers
# ------------------------------------------------------------------

def print_table(title: str, items: List[Dict[str, Any]], key_order: Optional[List[str]] = None) -> None:
    """Print a list of dicts as a readable table."""
    if not items:
        print(f"\n{title}: (none found)")
        return

    print(f"\n{title}:")
    print("-" * len(title))

    for i, item in enumerate(items):
        print(f"\n  [{i + 1}]")
        keys = key_order if key_order else sorted(item.keys())
        for k in keys:
            if k in item:
                print(f"    {k}: {item[k]}")
        # Print remaining keys not in key_order
        if key_order:
            for k in sorted(item.keys()):
                if k not in key_order:
                    print(f"    {k}: {item[k]}")


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Enumerate ethernet microphone input streams on a RoomOS codec "
                    "via local SSH or Webex Cloud xAPI.",
    )
    sub = ap.add_subparsers(dest="mode", required=True)

    # ---- local subparser ----
    ap_local = sub.add_parser("local", help="Query via local SSH xAPI")
    ap_local.add_argument("--host", required=True, help="Codec IP/hostname")
    ap_local.add_argument("-P", "--port", type=int, default=22, help="SSH port (default: 22)")
    ap_local.add_argument("-u", "--username", required=True, help="SSH username")
    ap_local.add_argument("-p", "--password", help="SSH password (omit to prompt)")
    ap_local.add_argument("-k", "--key", dest="key_path", help="SSH private key path (optional)")
    ap_local.add_argument("--timeout", type=int, default=10, help="SSH timeout seconds (default: 10)")

    # ---- cloud subparser ----
    ap_cloud = sub.add_parser("cloud", help="Query via Webex Cloud xAPI REST")
    ap_cloud.add_argument("--config", default=_DEFAULT_CONFIG,
                          help="Path to YAML config file with token/device_id (default: config.yaml beside script)")
    ap_cloud.add_argument("--device-id", help="Webex deviceId of the codec")
    ap_cloud.add_argument("--token", help="Webex access token (omit to prompt)")
    ap_cloud.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap_cloud.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds (default: 15)")

    # ---- shared args ----
    for p in (ap_local, ap_cloud):
        p.add_argument("--json", action="store_true", help="Output as JSON")

    args = ap.parse_args()

    try:
        # ---- Local mode ----
        if args.mode == "local":
            if not args.password and not args.key_path:
                args.password = getpass.getpass("SSH Password: ")

            raw = ssh_run_xstatus(
                host=args.host, port=args.port,
                username=args.username, password=args.password,
                key_path=args.key_path,
                commands=[
                    "xStatus Peripherals ConnectedDevice",
                    "xStatus Audio Input Connectors Ethernet",
                ],
                timeout=args.timeout,
            )

            mics = parse_peripherals(raw)
            eth_inputs = parse_ethernet_inputs(raw)

            if args.json:
                print(json.dumps({"peripherals": mics, "ethernet_inputs": eth_inputs}, indent=2))
            else:
                print_table(
                    "Ethernet Microphone Peripherals", mics,
                    key_order=["Name", "Type", "SerialNumber", "HardwareInfo", "Status"],
                )
                print_table(
                    "Audio Input Connectors (Ethernet)", eth_inputs,
                )

            return 0

        # ---- Cloud mode ----
        if args.mode == "cloud":
            cfg = load_config(args.config)
            token = args.token or cfg.get("token") or getpass.getpass("Webex Access Token: ")
            device_id = args.device_id or cfg.get("device_id")
            if not device_id:
                print("ERROR: --device-id is required (via CLI or config.yaml)", file=sys.stderr)
                return 2

            periph_data = cloud_get_status(
                device_id=device_id, token=token,
                name="Peripherals.ConnectedDevice[*].*",
                base_url=args.base_url, timeout=args.timeout,
            )
            eth_data = cloud_get_status(
                device_id=device_id, token=token,
                name="Audio.Input.Connectors.Ethernet[*].*",
                base_url=args.base_url, timeout=args.timeout,
            )

            mics = extract_cloud_peripherals(periph_data)
            eth_inputs = extract_cloud_ethernet_inputs(eth_data)

            if args.json:
                print(json.dumps({"peripherals": mics, "ethernet_inputs": eth_inputs}, indent=2))
            else:
                print_table(
                    "Ethernet Microphone Peripherals", mics,
                    key_order=["Name", "Type", "SerialNumber", "HardwareInfo", "Status"],
                )
                print_table(
                    "Audio Input Connectors (Ethernet)", eth_inputs,
                )

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
