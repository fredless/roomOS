"""
roomos_common.py

Shared helpers for the roomOS utilities: YAML config loading, local-mode SSH (xAPI over a
paramiko shell), and cloud-mode Webex xAPI command/status calls. Each script imports the
pieces it needs so this logic lives in exactly one place.
"""

from __future__ import annotations

import getpass
import os
import re
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
DEVICE_ID_ENV = "ROOMOS_DEVICE_ID"


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
    """Codec device id: --device-id arg, else the ROOMOS_DEVICE_ID environment variable."""
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
