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
# Tail Cisco RoomOS macro logs over SSH and print to stdout in columns.

# - Connects via SSH
# - Runs: xFeedback register Event/Macros/Log
# - Parses each "** end" block into one row: Timestamp | Level | Macro | Message
# - Auto-sizes columns to the current terminal width, trimming with ellipses
# - Optional colorization by level (Windows Terminal supports ANSI)

# Requires:
#   pip install paramiko
#!/usr/bin/env python3

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple

import paramiko


END_MARKER = "** end"

# Example line:
# *e Macros Log Level: TRACE
LINE_RE = re.compile(r"^\*e\s+Macros\s+Log\s+(?P<key>[^:]+):\s+(?P<val>.*)\s*$")

# Example line:
# *s Peripherals ConnectedDevice 1 SerialNumber: "T4AHKJB123456"
STATUS_LINE_RE = re.compile(r"^\*s\s+(?P<path>.+?):\s+(?P<val>.*)\s*$")


@dataclass
class MacroLogEvent:
    timestamp: str = ""
    level: str = ""
    macro: str = ""
    message: str = ""

    @staticmethod
    def from_kv(kv: Dict[str, str]) -> "MacroLogEvent":
        def clean(v: str) -> str:
            v = v.strip()
            # Strip surrounding quotes if present
            if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
                v = v[1:-1]
            return v

        return MacroLogEvent(
            timestamp=clean(kv.get("Timestamp", "")),
            level=clean(kv.get("Level", "")),
            macro=clean(kv.get("Macro", "")),
            message=clean(kv.get("Message", "")),
        )


def enable_ansi_on_windows() -> None:
    """
    Windows Terminal supports ANSI by default. Some consoles need VT processing enabled.
    This is a best-effort no-op if it fails.
    """
    if os.name != "nt":
        return
    try:
        import ctypes  # pylint: disable=import-outside-toplevel
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE = -11
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def color_for_level(level: str) -> str:
    lvl = (level or "").upper()
    # ANSI colors (keep subtle)
    if lvl in ("ERROR", "FATAL"):
        return "\x1b[31m"  # red
    if lvl in ("WARN", "WARNING"):
        return "\x1b[33m"  # yellow
    if lvl in ("INFO",):
        return "\x1b[32m"  # green
    if lvl in ("DEBUG",):
        return "\x1b[36m"  # cyan
    if lvl in ("TRACE",):
        return "\x1b[90m"  # gray
    return "\x1b[0m"


RESET = "\x1b[0m"


def ellipsize(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(s) <= width:
        return s
    if width == 1:
        return "…"
    return s[: width - 1] + "…"


def compute_widths(term_cols: int, show_ts: bool, show_level: bool, show_macro: bool, show_msg: bool) -> Tuple[int, int, int, int]:
    """
    Compute column widths to fit terminal.
    Layout includes two spaces between columns.
    """
    # Minimums so the table stays readable
    min_ts = 19  # "2026-03-03T17:42:57"
    min_level = 5
    min_macro = 8
    min_msg = 10

    # Preferred widths (we'll shrink as needed)
    pref_ts = 24
    pref_level = 7
    pref_macro = 18

    # How many columns are enabled?
    cols = []
    if show_ts: cols.append("ts")
    if show_level: cols.append("level")
    if show_macro: cols.append("macro")
    if show_msg: cols.append("msg")

    # If user disables message, still keep something printable
    if not cols:
        cols = ["msg"]
        show_msg = True

    # Separator spaces: "  " between each enabled column
    sep_total = 2 * (len(cols) - 1)
    available = max(20, term_cols) - sep_total  # don't go below 20 cols

    # Start from preferred widths, message gets remainder
    w_ts = pref_ts if show_ts else 0
    w_level = pref_level if show_level else 0
    w_macro = pref_macro if show_macro else 0

    fixed = (w_ts + w_level + w_macro)
    # w_msg is remainder if enabled
    w_msg = max(min_msg, available - fixed) if show_msg else 0

    # If we overflow, shrink ts/macro/level down toward minimums, then message.
    def total() -> int:
        return (w_ts + w_level + w_macro + w_msg)

    # Apply minimums
    if show_ts: w_ts = max(min_ts, min(w_ts, available))
    if show_level: w_level = max(min_level, min(w_level, available))
    if show_macro: w_macro = max(min_macro, min(w_macro, available))
    if show_msg: w_msg = max(min_msg, min(w_msg, available))

    # Now shrink loop until fit
    while total() > available:
        # shrink macro first, then ts, then level, then message
        changed = False
        if show_macro and w_macro > min_macro and total() > available:
            w_macro -= 1
            changed = True
        if show_ts and w_ts > min_ts and total() > available:
            w_ts -= 1
            changed = True
        if show_level and w_level > min_level and total() > available:
            w_level -= 1
            changed = True
        if show_msg and w_msg > min_msg and total() > available:
            w_msg -= 1
            changed = True
        if not changed:
            break

    # If still doesn't fit (super narrow terminal), force message tiny
    if total() > available and show_msg:
        w_msg = max(1, w_msg - (total() - available))

    return w_ts, w_level, w_macro, w_msg


def connect_ssh(host: str, port: int, username: str,
                password: Optional[str],
                key_path: Optional[str],
                timeout: int) -> paramiko.SSHClient:
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail Cisco RoomOS macro logs over SSH and print in columns.")
    ap.add_argument("host", help="IP or hostname of the RoomOS device")
    ap.add_argument("-P", "--port", type=int, default=22, help="SSH port (default: 22)")
    ap.add_argument("-u", "--username", required=True, help="SSH username (e.g., admin)")
    ap.add_argument("-p", "--password", help="SSH password (omit to prompt)")
    ap.add_argument("-k", "--key", dest="key_path", help="Path to SSH private key (optional)")
    ap.add_argument("--timeout", type=int, default=10, help="SSH connect timeout seconds (default: 10)")

    ap.add_argument("--no-header", action="store_true", help="Do not print header row")
    ap.add_argument("--no-color", action="store_true", help="Disable ANSI colors")
    ap.add_argument("--cols", default="ts,level,macro,msg",
                    help="Comma-separated columns: ts,level,macro,msg (default: ts,level,macro,msg)")

    ap.add_argument("--peripherals", action="store_true",
                    help="Also subscribe to Status/Peripherals/ConnectedDevice/SerialNumber and log serial number changes")

    # how often to re-check terminal width (seconds)
    ap.add_argument("--resize-interval", type=float, default=1.0,
                    help="How often to re-check terminal width in seconds (default: 1.0)")

    args = ap.parse_args()

    if not args.no_color:
        enable_ansi_on_windows()

    if not args.password and not args.key_path:
        args.password = getpass.getpass("SSH Password: ")

    # Column selection
    allowed = {"ts", "level", "macro", "msg"}
    requested = [c.strip().lower() for c in args.cols.split(",") if c.strip()]
    for c in requested:
        if c not in allowed:
            print(f"Unknown column '{c}'. Allowed: {', '.join(sorted(allowed))}", file=sys.stderr)
            return 2

    show_ts = "ts" in requested
    show_level = "level" in requested
    show_macro = "macro" in requested
    show_msg = "msg" in requested
    if not (show_ts or show_level or show_macro or show_msg):
        show_msg = True

    # Connect SSH
    try:
        client = connect_ssh(args.host, args.port, args.username, args.password, args.key_path, args.timeout)
    except Exception as e:
        print(f"SSH connect failed: {e}", file=sys.stderr)
        return 1

    # Prepare session
    chan = None
    try:
        transport = client.get_transport()
        if transport is None:
            print("SSH transport not available.", file=sys.stderr)
            return 1

        chan = transport.open_session()
        chan.get_pty()
        chan.invoke_shell()

        def send(cmd: str) -> None:
            assert chan is not None
            chan.send(cmd + "\n")

        # Drain initial prompt/banners
        time.sleep(0.25)
        while chan.recv_ready():
            _ = chan.recv(65535)

        send("xFeedback register Event/Macros/Log")
        if args.peripherals:
            send("xFeedback register Status/Peripherals/ConnectedDevice/SerialNumber")

        # Terminal sizing
        last_size_check = 0.0
        term_cols = shutil.get_terminal_size(fallback=(120, 40)).columns
        w_ts, w_level, w_macro, w_msg = compute_widths(term_cols, show_ts, show_level, show_macro, show_msg)

        def make_row(evt: MacroLogEvent) -> str:
            parts = []
            if show_ts:
                parts.append(ellipsize(evt.timestamp, w_ts).ljust(w_ts))
            if show_level:
                lvl = ellipsize(evt.level, w_level).ljust(w_level)
                if args.no_color:
                    parts.append(lvl)
                else:
                    parts.append(f"{color_for_level(evt.level)}{lvl}{RESET}")
            if show_macro:
                parts.append(ellipsize(evt.macro, w_macro).ljust(w_macro))
            if show_msg:
                # message: trim to width but don't pad; it's last column
                parts.append(ellipsize(evt.message, w_msg))
            return "  ".join(parts)

        if not args.no_header:
            header_evt = MacroLogEvent(
                timestamp="Timestamp",
                level="Level",
                macro="Macro",
                message="Message",
            )
            # build header without color
            saved_no_color = args.no_color
            args.no_color = True
            header = make_row(header_evt)
            args.no_color = saved_no_color

            print(header)
            print("-" * min(len(header), term_cols))

        buffer = ""
        kv: Dict[str, str] = {}
        status_kv: Dict[str, str] = {}

        while True:
            now = time.time()
            if now - last_size_check >= args.resize_interval:
                last_size_check = now
                new_cols = shutil.get_terminal_size(fallback=(term_cols, 40)).columns
                if new_cols != term_cols:
                    term_cols = new_cols
                    w_ts, w_level, w_macro, w_msg = compute_widths(term_cols, show_ts, show_level, show_macro, show_msg)
                    # Optional: print a subtle resize note (commented out)
                    # print(f"\x1b[90m(resized to {term_cols} cols)\x1b[0m", file=sys.stderr)

            if chan.recv_ready():
                data = chan.recv(65535)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")

                    if line.strip() == END_MARKER:
                        if kv:
                            evt = MacroLogEvent.from_kv(kv)
                            kv = {}
                            print(make_row(evt), flush=True)
                        elif status_kv:
                            ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                            for path, val in status_kv.items():
                                clean_val = val.strip('"').strip("'")
                                evt = MacroLogEvent(
                                    timestamp=ts,
                                    level="INFO",
                                    macro="Peripheral",
                                    message=f"{path}: {clean_val}",
                                )
                                print(make_row(evt), flush=True)
                            status_kv = {}
                        continue

                    m = LINE_RE.match(line)
                    if m:
                        key = m.group("key").strip()
                        val = m.group("val").strip()
                        kv[key] = val
                        continue

                    if args.peripherals:
                        m2 = STATUS_LINE_RE.match(line)
                        if m2:
                            status_kv[m2.group("path").strip()] = m2.group("val").strip()
            else:
                time.sleep(0.03)

    except KeyboardInterrupt:
        print("\nStopping…", file=sys.stderr)
    finally:
        try:
            if chan is not None and chan.send_ready():
                chan.send("xFeedback deregisterall\n")
                time.sleep(0.05)
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())