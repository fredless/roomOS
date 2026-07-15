# roomOS

Small Python CLI utilities for controlling and observing Cisco RoomOS codecs via either **local SSH xAPI** or **Webex Cloud xAPI**.

The single-device scripts expose two subcommands: `local` (SSH) and `cloud` (REST); the org-wide fleet tools (see [Fleet tools and piping](#fleet-tools-and-piping)) are cloud-only at present (might add on-prem at some future juncture). Shared plumbing (config, SSH, cloud xAPI, and device selection) thrown into [roomos_common.py](roomos_common.py), so must sit alongside the scripts.

## Requirements

```
pip install paramiko requests pyyaml
```

(`paramiko` is needed for `local` SSH mode; `requests` for `cloud` mode.)

## Configuration

**Cloud mode** needs a Webex token and a target device:

- **Token** — read from `~/Personal-Local/config.yml` (the same file the sibling Cisco Collab repo uses), or passed with `--token`:

  ```yaml
  wxteams:
    auth_token: <a Webex access token>
  ```

  (On Windows that's `C:\Users\<you>\Personal-Local\config.yml`.) The token needs the Cloud xAPI scopes **`spark:xapi_statuses`** and **`spark:xapi_commands`** — these are **not** included in `spark:all` and must be added to the integration explicitly (a token without them gets a `403` on xStatus/xCommand calls). Reading device configurations (`roomOS_bulk_query.py --config`) and listing devices instead use admin device scopes such as `spark-admin:devices_read`; writing them (`roomOS_apply_config.py`) needs `spark-admin:devices_write`.

- **Device ID** — supplied per run, since it changes often during a session: pass **`--device-id <id>`**, or set the **`ROOMOS_DEVICE_ID`** environment variable as a session default (`--device-id` overrides it). It is intentionally *not* stored in `config.yml`.

**Local mode** needs no config file — pass `--host`, `--username`, and `--password` (omit to be prompted) or `--key`, plus optional `--port`/`--timeout`.

## Conventions

- Repeatable command arguments use `--kv key=value` (e.g. `--kv CallType=Video`).
- Cloud commands print a short human-readable summary by default; add `--json` for the raw API response.
- xStatus/xConfiguration paths and keys are **case-sensitive** — they must match the RoomOS xAPI casing (PascalCase, e.g. `SystemUnit.Uptime`, `Audio.DefaultVolume`). A wrong-cased key may not error, mind you — the API could just return nothing and leave you to your thoughts. And an functional exception: `roomOS_apply_config.py --file` input *is* case-insensitive, similar to the codec CLI — as keys and enum values are rewritten to canonical casing queried from each target device the way this is setup right now.

## Fleet tools and piping

The org-wide (fleet) tools share the same device-selection priority, in precedence order:

1. `--device-id <id>` — explicit id(s), repeatable
2. `--stdin` — ids read one per line from stdin (pipe from `roomOS_find_device.py`)
3. filter flags — `--model` / `--kind` / `--type` / `--platform` / `--connection` (all matches)
4. `--name <term>` — display-name search (wildcards allowed); several matches prompt a numbered pick, or take them all with `--all`
5. `ROOMOS_DEVICE_ID` env var — session-default single device

Selected device ids go to **stdout**; all prompts, match lists, and progress go to **stderr** — allowing for stdout to pipe to the next tool:

```powershell
# pick a device by name interactively, then apply config to it
python roomOS_find_device.py --name lobby | python roomOS_apply_config.py --stdin --set Audio.DefaultVolume=60

# query every online Desk Pro, reusing one selection for several tools
python roomOS_find_device.py --model "*Desk Pro*" --connection online > ids.txt
Get-Content ids.txt | python roomOS_bulk_query.py --stdin --status SystemUnit.Uptime
Get-Content ids.txt | python roomOS_add_localuser.py --stdin --username svc-av -g -y

# take a timestamped config backup of every Room Bar before touching anything
python roomOS_find_device.py --model "Room Bar*" | python roomOS_backup_config.py --stdin --save backups
```

Tools that change devices (`apply_config`, `add_localuser`) confirm before acting; pass `-y`/`--yes` for non-interactive runs (required when no console is available). Reconfiguring a thousand codecs at once is a thrill best experienced on purpose.

## Utilities

| Script | Purpose |
| --- | --- |
| [roomOS_add_localuser.py](roomOS_add_localuser.py) | Create a local admin user on the selected device(s) via `xCommand UserManagement User Add`; can auto-generate the passphrase (`-g`) and print it (cloud only). |
| [roomOS_apply_config.py](roomOS_apply_config.py) | Apply xConfiguration changes (`--set key=value`, `--remove key`, or `--file <config export>` — web UI backup, CLI session dump, hand-written `xConfiguration` lines in any case, or a Control Hub configuration-template CSV with "Follow default" honored; auto-detected and validated per device) to the selected device(s) via the device configurations API (JSON Patch); `--dry-run` previews (cloud only). |
| [roomOS_backup_config.py](roomOS_backup_config.py) | Export the full xConfiguration of the selected device(s) in the codec backup format — one device to stdout, several to per-device `<name>_<serial>_<timestamp>.txt` files; `--configured-only` for just the explicit overrides. Restorable with `roomOS_apply_config.py --file` (cloud only). |
| [roomOS_bulk_query.py](roomOS_bulk_query.py) | Query xStatus/xConfiguration values across the selected device(s) and export to CSV (cloud only). |
| [roomOS_clock_sync.py](roomOS_clock_sync.py) | Read the codec clock and optionally set the local PC clock to match (needs admin/root). |
| [roomOS_dial.py](roomOS_dial.py) | Place a call (SIP/Spark, Video/Audio) or show current call status. |
| [roomOS_ethernet_mics.py](roomOS_ethernet_mics.py) | Enumerate connected microphones and per-stream detail for ethernet audio inputs. |
| [roomOS_find_device.py](roomOS_find_device.py) | Select org devices by name search (interactive pick) and/or filters and print their ids to stdout for piping into the other fleet tools (cloud only). |
| [roomOS_macro_SSHlogger.py](roomOS_macro_SSHlogger.py) | Tail the macro log in real time (SSH `xFeedback`) or by polling (cloud `Macros.Log.Get`). Built as a workaround to the current implementation (as of June 2026) of Cisco's *other* current cloud logging facility in Collaboration Hub, which **will** let you down (stops outputting) when it is faced with a busy log file. But this way is also handy if you need to watch these logs on a long running basis in a side window, and don't have local\SSH access to a codec.|
| [roomOS_notice.py](roomOS_notice.py) | Display or clear on-screen alerts and textline overlays. |
| [roomOS_selfview.py](roomOS_selfview.py) | Toggle self-view: off, PiP thumbnail, or full-screen. |
| [roomOS_send_message.py](roomOS_send_message.py) | Push a `Message Send` onto the macro bus (macros subscribe via `Event/Message/Send`). |

`--help` *probably* implemented in most places 😜 
