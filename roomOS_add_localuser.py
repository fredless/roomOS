#!/usr/bin/env python3
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

"""
roomos_add_localuser.py

Create a local admin user on a RoomOS device via Webex Cloud xAPI
(xCommand UserManagement User Add). Cloud-only.

Give a device display-name search term (wildcards allowed); the tool lists the matching devices
and lets you pick one, then creates the user. The new user is created with:
  Role                     = Admin   (admin privileges)
  PassphraseChangeRequired = False   (no forced passphrase reset at first login)
It is always a local account -- that is what UserManagement User Add creates; the command has no
separate "local login" flag. The only login-channel option is ShellLogin (SSH), left at the
device default here. Valid roles per the RoomOS schema: Admin, Audit, User, Integrator,
RoomControl.

Usage: roomos_add_localuser.py --name "<device search>" --username <user> [--password <pw>]

Reads the Webex token from --token or ~/Personal-Local/config.yml (wxteams.auth_token); needs
the spark:xapi_commands scope (not part of spark:all) and admin access to the device's org.
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import sys
from typing import Any, Dict, List, Optional

from roomos_common import list_devices, resolve_token, xapi_command


def find_devices(devices: List[Dict[str, Any]], term: str) -> List[Dict[str, Any]]:
    """Case-insensitive match of a search term against device displayName (wildcards allowed)."""
    t = term.lower()
    pattern = t if any(ch in t for ch in "*?[") else f"*{t}*"
    return [d for d in devices if fnmatch.fnmatch((d.get("displayName") or "").lower(), pattern)]


def choose_device(matches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Print a numbered device list and prompt for a selection; return the choice or None."""
    for i, d in enumerate(matches, 1):
        print(f"  {i}. {d.get('displayName', '')}  "
              f"[{d.get('product', '')}, {d.get('connectionStatus', '')}]", file=sys.stderr)
    try:
        raw = input(f"Select device [1-{len(matches)}] (blank to cancel): ").strip()
    except EOFError:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(matches):
        return matches[int(raw) - 1]
    return None


def confirmed(question: str) -> bool:
    """Ask a yes/no question."""
    return input(f"{question} (y/n): ").strip().lower() in ("y", "yes")


def main() -> int:
    """create a local admin user on a RoomOS device (cloud xAPI)"""
    ap = argparse.ArgumentParser(
        description="Create a local admin user on a RoomOS device via Webex Cloud xAPI.",
    )
    ap.add_argument("--name", required=True,
                    help="Device display-name search term (wildcards allowed)")
    ap.add_argument("--username", required=True, help="Username for the new local user")
    ap.add_argument("--password", help="Passphrase for the new user (omit to be prompted)")
    ap.add_argument("--token", help="Webex access token (omit to read config / prompt)")
    ap.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")

    args = ap.parse_args()

    try:
        # resolve the passphrase (prompt + confirm if not given on the command line)
        if args.password:
            password = args.password
        else:
            password = getpass.getpass("New user passphrase: ")
            if password != getpass.getpass("Confirm passphrase: "):
                print("ERROR: passphrases do not match", file=sys.stderr)
                return 2
        if not password:
            print("ERROR: a passphrase is required", file=sys.stderr)
            return 2

        token = resolve_token(args.token)

        print(f"Searching for devices matching '{args.name}'...", file=sys.stderr)
        matches = find_devices(list_devices(token, args.base_url, args.timeout), args.name)
        if not matches:
            print(f"No devices match '{args.name}'.", file=sys.stderr)
            return 1

        if len(matches) == 1:
            device = matches[0]
            print(f"One match: {device.get('displayName', '')} "
                  f"[{device.get('product', '')}, {device.get('connectionStatus', '')}]",
                  file=sys.stderr)
        else:
            device = choose_device(matches)
        if device is None:
            print("Cancelled.", file=sys.stderr)
            return 0

        if not confirmed(f"Create admin user '{args.username}' on "
                         f"\"{device.get('displayName', '')}\"?"):
            print("Aborted.", file=sys.stderr)
            return 0

        # xCommand UserManagement User Add (fields verified against the RoomOS schema). There is
        # no "local login" flag -- the account is local by definition. Role is a LiteralArray;
        # PassphraseChangeRequired is a True/False literal.
        arguments = {
            "Username": args.username,
            "Passphrase": password,
            "Role": ["Admin"],                    # admin privileges
            "PassphraseChangeRequired": "False",  # no forced reset at first login
        }
        xapi_command("UserManagement.User.Add", device["id"], token, arguments,
                     args.base_url, args.timeout)
        print(f"Created local admin user '{args.username}' on "
              f"{device.get('displayName', '')}.")
        return 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
