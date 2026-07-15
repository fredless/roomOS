#!/usr/bin/env python3
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
xapi_add_localuser.py

Create a local admin user on one or more RoomOS devices via Webex Cloud xAPI
(xCommand UserManagement User Add). Cloud-only.

Select the target devices the standard fleet-tool way (shared with xapi_find_device.py):
--name search with an interactive pick (the classic single-device flow), explicit
--device-id (repeatable), --stdin (ids one per line, pipeable), or the
--model/--kind/--type/--platform/--connection filters for a whole fleet slice.
The same user (and passphrase) is created on every selected device. It is created with:
  Role                     = Admin   (admin privileges)
  PassphraseChangeRequired = False   (no forced passphrase reset at first login)
It is always a local account -- that is what UserManagement User Add creates; the command has no
separate "local login" flag. The only login-channel option is ShellLogin (SSH), left at the
device default here. Valid roles per the RoomOS schema: Admin, Audit, User, Integrator,
RoomControl.

Usage: xapi_add_localuser.py --name "<device search>" --username <user>
       [--password <pw> | --generate-password] [-y]

With --generate-password (-g) a strong random passphrase is generated and printed to stdout
after the user is created, so you can record it.

Reads the Webex token from --token or ~/Personal-Local/config.yml (wxteams.auth_token); needs
the spark:xapi_commands scope (not part of spark:all) and admin access to the device's org.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import string
import sys

from xapi_common import (add_selection_args, confirmed, device_summary,
                           resolve_target_devices, resolve_token, xapi_command)

_PASSWORD_SYMBOLS = "!@#$%^*-_=+"
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + _PASSWORD_SYMBOLS


def generate_password(length: int = 20) -> str:
    """Generate a strong random passphrase with mixed character classes (crypto-random)."""
    while True:
        pw = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in _PASSWORD_SYMBOLS for c in pw)):
            return pw


def main() -> int:
    """create a local admin user on selected RoomOS devices (cloud xAPI)"""
    ap = argparse.ArgumentParser(
        description="Create a local admin user on RoomOS devices via Webex Cloud xAPI.",
    )
    ap.add_argument("--username", required=True, help="Username for the new local user")
    pw_group = ap.add_mutually_exclusive_group()
    pw_group.add_argument("--password", help="Passphrase for the new user (omit to be prompted)")
    pw_group.add_argument("-g", "--generate-password", action="store_true",
                          help="Generate a strong random passphrase and print it after creation")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="Skip the confirmation prompt (needed for non-interactive runs)")
    ap.add_argument("--token", help="Webex access token (omit to read config / prompt)")
    ap.add_argument("--base-url", default="https://webexapis.com", help="Webex API base URL")
    ap.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds (default: 30)")
    add_selection_args(ap)

    args = ap.parse_args()

    try:
        # resolve the passphrase: explicit --password, generated (-g), or prompt + confirm
        generated = False
        if args.password:
            password = args.password
        elif args.generate_password:
            password = generate_password()
            generated = True
        else:
            password = getpass.getpass("New user passphrase: ")
            if password != getpass.getpass("Confirm passphrase: "):
                print("ERROR: passphrases do not match", file=sys.stderr)
                return 2
        if not password:
            print("ERROR: a passphrase is required", file=sys.stderr)
            return 2

        token = resolve_token(args.token)

        devices = resolve_target_devices(args, token, args.base_url, args.timeout)
        if not devices:
            print("No devices selected.", file=sys.stderr)
            return 1

        print(f"Target device(s) ({len(devices)}):", file=sys.stderr)
        for device in devices:
            print(f"  {device_summary(device)}", file=sys.stderr)

        if not args.yes:
            try:
                if not confirmed(f"Create admin user '{args.username}' on "
                                 f"{len(devices)} device(s)?"):
                    print("Aborted.", file=sys.stderr)
                    return 0
            except EOFError:
                print("ERROR: no console available to confirm -- re-run with -y/--yes",
                      file=sys.stderr)
                return 2

        # xCommand UserManagement User Add (fields verified against the RoomOS schema). There is
        # no "local login" flag -- the account is local by definition. Role is a LiteralArray;
        # PassphraseChangeRequired is a True/False literal.
        arguments = {
            "Username": args.username,
            "Passphrase": password,
            "Role": ["Admin"],                    # admin privileges
            "PassphraseChangeRequired": "False",  # no forced reset at first login
        }
        failures = 0
        for index, device in enumerate(devices, 1):
            name = device.get("displayName", device.get("id", ""))
            print(f"  [{index}/{len(devices)}] creating user on {name}...", file=sys.stderr)
            try:
                xapi_command("UserManagement.User.Add", device["id"], token, arguments,
                             args.base_url, args.timeout)
            except Exception as exc:
                failures += 1
                print(f"  ! {name}: {exc}", file=sys.stderr)

        created = len(devices) - failures
        print(f"Created local admin user '{args.username}' on {created} of "
              f"{len(devices)} device(s)" + (f"; {failures} failed" if failures else "") + ".")
        if generated and created:
            print(f"Generated passphrase: {password}")
        return 1 if failures else 0

    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
