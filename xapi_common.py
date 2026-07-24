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
xapi_common.py

Shared helpers for the xAPI tools: YAML config loading, local-mode SSH (xAPI over a
paramiko shell), and cloud-mode Webex xAPI command/status calls. Each script imports the
pieces it needs so this logic lives in exactly one place.
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import paramiko
import requests
import yaml

_KV_RE = re.compile(r"^(?P<k>[^=]+)=(?P<v>.*)$")

# Shared config file in the user's home dir, the same one the other Cisco Collab repos use.
# i.e. ~/Personal-Local/config.yml  (Windows: C:\Users\<you>\Personal-Local\config.yml)
CONFIG_FILE = os.path.join(os.path.expanduser("~"), "Personal-Local", "config.yml")

# device id changes often during a session, so it is resolved from CLI/env, never the config
DEVICE_ID_ENV = "XAPI_DEVICE_ID"


# ------------------------------------------------------------------
# Config / credential resolution
# ------------------------------------------------------------------

def load_config(path: str = CONFIG_FILE) -> Dict[str, Any]:
    """Load the shared YAML config (~/Personal-Local/config.yml). Returns {} if absent."""
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.full_load(fh)
    return data if isinstance(data, dict) else {}


def resolve_token(arg_token: Optional[str] = None) -> str:
    """Webex token: --token arg, else wxteams.auth_token from config, else prompt."""
    if arg_token:
        return arg_token
    wxteams = load_config().get("wxteams") or {}
    token = wxteams.get("auth_token")
    if token:
        return token
    return getpass.getpass("Webex Access Token: ")


def resolve_device_id(arg_device_id: Optional[str] = None) -> Optional[str]:
    """Codec device id: --device-id arg, else the XAPI_DEVICE_ID environment variable."""
    return arg_device_id or os.environ.get(DEVICE_ID_ENV)


# ------------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------------

def xquote(s: str) -> str:
    """Quote and escape a string for use as an xCommand argument value."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def parse_kv(items: List[str], flag: str = "--kv") -> List[Tuple[str, str]]:
    """Parse repeatable key=value CLI items into a list of (key, value) tuples."""
    out: List[Tuple[str, str]] = []
    for item in items:
        m = _KV_RE.match(item)
        if not m:
            raise ValueError(f"Invalid {flag} '{item}'. Expected key=value.")
        k = m.group("k").strip()
        v = m.group("v").strip()
        if not k:
            raise ValueError(f"Invalid {flag} '{item}': empty key.")
        out.append((k, v))
    return out


# ------------------------------------------------------------------
# Device selection (shared producer/consumer contract for the fleet tools)
#
# Every fleet tool selects its target devices the same way, in precedence order:
#   1. --device-id <id>       explicit id(s), repeatable
#   2. --stdin                ids read one per line from stdin (pipe from xapi_find_device.py)
#   3. filter flags           --model/--kind/--type/--platform/--connection (all matches)
#   4. --name <term>          display-name search; interactive pick if several match
#   5. XAPI_DEVICE_ID env   session-default single device
# Selection UI and progress always go to stderr so stdout stays clean for piping.
# ------------------------------------------------------------------

# friendly --connection aliases mapped to the raw Webex connectionStatus values they cover
CONNECTION_ALIASES = {
    "online": {"connected", "connected_with_issues"},
    "offline": {"disconnected", "offline_expired", "offline_deep_sleep", "offline_temporarily"},
    "expired": {"offline_expired"},
}


def device_kind(device: Dict[str, Any]) -> str:
    """Return 'personal' (assigned to a person), 'workspace', or '' (neither)."""
    if device.get("personId"):
        return "personal"
    if device.get("workspaceId"):
        return "workspace"
    return ""


def expand_connections(values: List[str]) -> set:
    """Expand --connection terms (aliases or raw) into a set of raw connectionStatus values."""
    accepted: set = set()
    for value in values:
        low = value.lower()
        accepted |= {v.lower() for v in CONNECTION_ALIASES.get(low, {low})}
    return accepted


def matches_filters(device: Dict[str, Any], models: List[str], kinds: List[str],
                    types: List[str], platforms: List[str], connections: set) -> bool:
    """Apply the model / kind / type / platform / connection filters (all client-side)."""
    if models:
        product = (device.get("product") or "").lower()
        if not any(fnmatch.fnmatch(product, m.lower()) for m in models):
            return False
    if kinds and device_kind(device) not in kinds:
        return False
    if types and (device.get("type") or "").lower() not in [t.lower() for t in types]:
        return False
    if platforms and (device.get("devicePlatform") or "").lower() not in [p.lower() for p in platforms]:
        return False
    if connections and (device.get("connectionStatus") or "").lower() not in connections:
        return False
    return True


def find_devices_by_name(devices: List[Dict[str, Any]], term: str) -> List[Dict[str, Any]]:
    """Case-insensitive match of a search term against device displayName (wildcards allowed)."""
    t = term.lower()
    pattern = t if any(ch in t for ch in "*?[") else f"*{t}*"
    return [d for d in devices if fnmatch.fnmatch((d.get("displayName") or "").lower(), pattern)]


def device_summary(device: Dict[str, Any]) -> str:
    """One-line human summary of a device for selection lists and progress messages."""
    return (f"{device.get('displayName', '')}  "
            f"[{device.get('product', '')}, {device.get('connectionStatus', '')}]")


def console_input(prompt: str) -> str:
    """Prompt on stderr and read a reply from the console, even when stdio is piped.

    stdout may be a pipe (this tool is a producer) and stdin may be a pipe (--stdin consumer),
    so the prompt goes to stderr and, when stdin is not a terminal, the reply is read straight
    from the console device (CONIN$ on Windows, /dev/tty elsewhere). Raises EOFError when no
    console is available (fully non-interactive run).
    """
    sys.stderr.write(prompt)
    sys.stderr.flush()
    if sys.stdin.isatty():
        return input()
    console = "CONIN$" if os.name == "nt" else "/dev/tty"
    try:
        with open(console, encoding="utf-8") as fh:
            line = fh.readline()
    except OSError as e:
        raise EOFError(f"no interactive console available: {e}") from e
    if not line:
        raise EOFError("no interactive console available")
    return line.rstrip("\r\n")


def choose_device(matches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Print a numbered device list and prompt for a selection; return the choice or None."""
    for i, d in enumerate(matches, 1):
        print(f"  {i}. {device_summary(d)}", file=sys.stderr)
    try:
        raw = console_input(f"Select device [1-{len(matches)}] (blank to cancel): ").strip()
    except EOFError:
        print("No console available for the interactive pick -- "
              "use --all, filters, or --device-id instead.", file=sys.stderr)
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(matches):
        return matches[int(raw) - 1]
    return None


def confirmed(question: str) -> bool:
    """Ask a yes/no question on the console. Raises EOFError if no console is available."""
    return console_input(f"{question} (y/n): ").strip().lower() in ("y", "yes")


def add_selection_args(ap: argparse.ArgumentParser) -> None:
    """Add the shared device-selection flags every fleet tool accepts."""
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="Suppress progress chatter; keep errors, prompts, and "
                         "essential results only")
    sel = ap.add_argument_group("device selection")
    sel.add_argument("--device-id", action="append", default=[], metavar="ID",
                     help="Target device id; repeatable (skips search/filters)")
    sel.add_argument("--stdin", action="store_true",
                     help="Read device ids from stdin, one per line "
                          "(pipe from xapi_find_device.py)")
    sel.add_argument("--name", metavar="TERM",
                     help="Device display-name search term (wildcards allowed); "
                          "prompts to pick when several match")
    sel.add_argument("--all", action="store_true",
                     help="With --name: act on every match instead of prompting to pick one")
    sel.add_argument("--model", action="append", default=[],
                     help="Filter by product name; wildcards allowed, e.g. '*Desk*' (repeatable)")
    sel.add_argument("--kind", action="append", default=[], choices=["personal", "workspace"],
                     help="Filter by assignment: personal or workspace (repeatable)")
    sel.add_argument("--type", action="append", default=[],
                     help="Filter by device type, e.g. roomdesk, accessory, phone (repeatable)")
    sel.add_argument("--platform", action="append", default=[],
                     help="Filter by device platform, e.g. cisco (repeatable)")
    sel.add_argument("--connection", action="append", default=[],
                     help="Filter by status: online/offline/expired or a raw connectionStatus "
                          "value (repeatable)")


def resolve_target_devices(args: argparse.Namespace, token: str, base_url: str, timeout: int,
                           default_all: bool = False) -> List[Dict[str, Any]]:
    """Resolve the flags added by add_selection_args() to a list of device dicts.

    Precedence: --device-id / --stdin (combined) -> filters (+ optional --name narrowing,
    interactive pick unless --all) -> XAPI_DEVICE_ID env. With default_all=True, no
    selection at all means every device in the org. Returns [] when a pick is cancelled
    or nothing matches; raises ValueError when no selection was given at all.
    """
    quiet = getattr(args, "quiet", False)
    ids = list(args.device_id)
    if args.stdin:
        ids += [line.strip() for line in sys.stdin if line.strip()]
    if args.device_id or args.stdin:
        return [get_device(i, token, base_url, timeout) for i in ids]

    has_filters = any([args.model, args.kind, args.type, args.platform, args.connection])
    if not has_filters and not args.name:
        env_id = os.environ.get(DEVICE_ID_ENV)
        if env_id:
            return [get_device(env_id, token, base_url, timeout)]
        if not (default_all or args.all):
            raise ValueError("no device selection given: use --device-id, --stdin, "
                             "filter flags, or --name (see --help)")

    if not quiet:
        print("Listing devices...", file=sys.stderr)
    devices = list_devices(token, base_url, timeout)
    connections = expand_connections(args.connection)
    matched = [d for d in devices
               if matches_filters(d, args.model, args.kind, args.type, args.platform, connections)]
    if args.name:
        matched = find_devices_by_name(matched, args.name)
    if not quiet:
        print(f"{len(matched)} of {len(devices)} device(s) match.", file=sys.stderr)
    if not matched:
        return []

    if args.name and not args.all:
        if len(matched) == 1:
            if not quiet:
                print(f"One match: {device_summary(matched[0])}", file=sys.stderr)
            return matched
        device = choose_device(matched)
        return [device] if device else []
    return matched


# ------------------------------------------------------------------
# Local mode: SSH xAPI
# ------------------------------------------------------------------

def connect_ssh(host: str, port: int, username: str, password: Optional[str],
                key_path: Optional[str], timeout: int) -> paramiko.SSHClient:
    """Open a paramiko SSH connection to a codec (password or private key)."""
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
    """Read whatever is currently available on an SSH channel without blocking."""
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
    """Run a single xCommand/xStatus over an interactive SSH shell and return the output."""
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


def ssh_run_xcommands(host: str, port: int, username: str, password: Optional[str],
                      key_path: Optional[str], commands: List[str], timeout: int) -> str:
    """Run one or more commands over a single SSH session and return combined output."""
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
# Cloud mode: Webex xAPI REST
# ------------------------------------------------------------------

def xapi_command(command: str, device_id: str, token: str, arguments: Dict[str, Any],
                 base_url: str, timeout: int,
                 empty_default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """POST /v1/xapi/command/<command> with {deviceId, arguments}; return the JSON result."""
    url = f"{base_url.rstrip('/')}/v1/xapi/command/{command}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload: Dict[str, Any] = {"deviceId": device_id}
    if arguments:
        payload["arguments"] = arguments
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")
    if resp.text.strip():
        return resp.json()
    return empty_default if empty_default is not None else {"status": "ok"}


def xapi_status(name: str, device_id: str, token: str, base_url: str, timeout: int,
                empty_default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET /v1/xapi/status?deviceId=...&name=...; return the JSON result."""
    url = f"{base_url.rstrip('/')}/v1/xapi/status"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {"deviceId": device_id, "name": name}
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Cloud xAPI failed: HTTP {resp.status_code} - {resp.text}")
    if resp.text.strip():
        return resp.json()
    return empty_default if empty_default is not None else {}


def list_devices(token: str, base_url: str, timeout: int,
                 **filters: Any) -> List[Dict[str, Any]]:
    """List devices in the org, following pagination. Server-side filters via kwargs."""
    url = f"{base_url.rstrip('/')}/v1/devices"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params: Dict[str, Any] = {k: v for k, v in filters.items() if v is not None}
    params["max"] = params.get("max", 100)
    start = 0
    devices: List[Dict[str, Any]] = []
    while True:
        params["start"] = start
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        if not resp.ok:
            raise RuntimeError(f"Device list failed: HTTP {resp.status_code} - {resp.text}")
        items = (resp.json() or {}).get("items", [])
        if not items:
            break
        devices.extend(items)
        if len(items) < params["max"]:
            break
        start += len(items)
    return devices


def get_device(device_id: str, token: str, base_url: str, timeout: int) -> Dict[str, Any]:
    """GET /v1/devices/<id>; return the device detail dict."""
    url = f"{base_url.rstrip('/')}/v1/devices/{device_id}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Device lookup failed for {device_id}: "
                           f"HTTP {resp.status_code} - {resp.text}")
    return resp.json()


def xconfig_patch(ops: List[Dict[str, Any]], device_id: str, token: str,
                  base_url: str, timeout: int) -> Dict[str, Any]:
    """PATCH /v1/deviceConfigurations (JSON Patch) for one device; return the JSON result.

    Each op is {"op": "replace"|"remove", "path": "<Config.Path>/sources/configured/value",
    "value": ...} ("remove" reverts the config to its default). Many ops per call are fine,
    but the API takes exactly one deviceId per call. Needs spark-admin:devices_write.
    """
    url = f"{base_url.rstrip('/')}/v1/deviceConfigurations"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json-patch+json",
        "Accept": "application/json",
    }
    resp = requests.patch(url, headers=headers, params={"deviceId": device_id},
                          json=ops, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Config patch failed: HTTP {resp.status_code} - {resp.text}")
    return resp.json() if resp.text.strip() else {}


def xconfig_get_items(device_id: str, token: str, base_url: str, timeout: int,
                      key: Optional[str] = None) -> Dict[str, Any]:
    """GET /v1/deviceConfigurations for a device; return the full items dict.

    Each item carries value/sources/valueSpace detail. Without a key, every
    configuration on the device is returned. Needs spark-admin:devices_read.
    """
    url = f"{base_url.rstrip('/')}/v1/deviceConfigurations"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params: Dict[str, Any] = {"deviceId": device_id}
    if key:
        params["key"] = key
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Config query failed: HTTP {resp.status_code} - {resp.text}")
    items = (resp.json() or {}).get("items", {})
    return items if isinstance(items, dict) else {}


def xconfig_get(key: str, device_id: str, token: str, base_url: str, timeout: int) -> Any:
    """GET a single xConfiguration value via /v1/deviceConfigurations; None if absent."""
    url = f"{base_url.rstrip('/')}/v1/deviceConfigurations"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    params = {"deviceId": device_id, "key": key}
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f"Config query failed: HTTP {resp.status_code} - {resp.text}")
    items = (resp.json() or {}).get("items", {})
    entry = items.get(key) if isinstance(items, dict) else None
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return None
