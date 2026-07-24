# xAPI tools

Small Python CLI utilities for controlling and observing Cisco RoomOS codecs via either **local SSH xAPI** or **Webex Cloud xAPI**.

The single-device scripts expose two subcommands: `local` (SSH) and `cloud` (REST); the org-wide fleet tools (see [Fleet tools and piping](#fleet-tools-and-piping)) are cloud-only at present (might add on-prem at some future juncture). Shared plumbing (config, SSH, cloud xAPI, and device selection) thrown into [xapi_common.py](xapi_common.py), so must sit alongside the scripts.

## Requirements

```
pip install paramiko requests pyyaml
```

(`paramiko` is needed for `local` SSH mode; `requests` for `cloud` mode.)

## Configuration

**Cloud mode** needs a Webex token and a target device:

- **Token** — read from `~/Personal-Local/config.yml` (the same file\value the sibling [ciscoCloudCollabTools](https://github.com/fredless/ciscoCollabCloudTools)repo also uses), or passed with `--token`:

  ```yaml
  wxteams:
    auth_token: <a Webex access token>
  ```

  The token needs the Cloud xAPI scopes **`spark:xapi_statuses`** and **`spark:xapi_commands`**. Reading device configurations (`xapi_bulk_query.py --config`) and listing devices instead use admin\WCH device scopes such as `spark-admin:devices_read`; writing them (`xapi_apply_config.py`) needs `spark-admin:devices_write`.

- **Device ID** — base64 encoded, supplied per run, since it changes often during a session: pass **`--device-id <id>`**, or set the **`XAPI_DEVICE_ID`** environment variable as a session default (`--device-id` overrides it). It is intentionally *not* stored in `config.yml`.

**Local mode** needs no config file — pass `--host`, `--username`, and `--password` (omit to be prompted) or `--key`, plus optional `--port`/`--timeout`.

## Conventions

- Repeatable command arguments use `--kv key=value` (e.g. `--kv CallType=Video`).
- The fleet tools take `-q`/`--quiet` to suppress the progress chatter — errors, prompts, dry-run findings, generated passphrases, and written filenames still come through.
- Cloud commands print a short human-readable summary by default; add `--json` for the raw API response.
- xStatus/xConfiguration paths and keys are **case-sensitive** — they must match the RoomOS xAPI casing (PascalCase, e.g. `SystemUnit.Uptime`, `Audio.DefaultVolume`). A wrong-cased key may not error, mind you — the API could just return nothing and leave you to your thoughts. And an functional exception: `xapi_apply_config.py --file` input *is* case-insensitive, similar to the codec CLI — as keys and enum values are rewritten to canonical casing queried from each target device the way this is setup right now.

## Fleet tools and piping

The org-wide (fleet) tools share the same device-selection priority as follows:

1. `--device-id <id>` — explicit id(s), repeatable
2. `--stdin` — ids read one per line from stdin (pipe from `xapi_find_device.py`)
3. filter flags — `--model` / `--kind` / `--type` / `--platform` / `--connection` (all matches)
4. `--name <term>` — display-name search (wildcards allowed); several matches prompt a numbered pick, or take them all with `--all`
5. `XAPI_DEVICE_ID` env var — session-default single device

Selected device ids go to **stdout**; all prompts, match lists, and progress go to **stderr** — allowing for stdout to pipe to the next tool:

```powershell
# pick a device by name interactively, then apply config to it
python xapi_find_device.py --name lobby | python xapi_apply_config.py --stdin --set Audio.DefaultVolume=60

# query every online Desk Pro, reusing one selection for several tools
python xapi_find_device.py --model "*Desk Pro*" --connection online > ids.txt
Get-Content ids.txt | python xapi_bulk_query.py --stdin --status SystemUnit.Uptime
Get-Content ids.txt | python xapi_add_localuser.py --stdin --username svc-av -g -y

# take a timestamped config backup of every Room Bar before touching anything
python xapi_find_device.py --model "Room Bar*" | python xapi_backup_config.py --stdin --save backups
```

Tools that change devices (`apply_config`, `add_localuser`) confirm before acting; pass `-y`/`--yes` for non-interactive runs (required when no console is available). Reconfiguring a thousand codecs at once is a thrill best experienced on purpose.

## Utilities

| Script | Purpose |
| --- | --- |
| [xapi_add_localuser.py](xapi_add_localuser.py) | Create a local admin user on the selected device(s) via `xCommand UserManagement User Add`; can auto-generate the passphrase (`-g`) and print it (cloud only). |
| [xapi_apply_config.py](xapi_apply_config.py) | Apply xConfiguration changes (`--set key=value`, `--remove key`, or `--file <config export>` — web UI backup, CLI session dump, hand-written `xConfiguration` lines in any case, or a Control Hub configuration-template CSV with "Follow default" honored; auto-detected and validated per device) to the selected device(s) via the device configurations API (JSON Patch); `--dry-run` previews (cloud only). |
| [xapi_backup_config.py](xapi_backup_config.py) | Export the full xConfiguration of the selected device(s) in the codec backup format — one device to stdout, several to per-device `<name>_<serial>_<timestamp>.txt` files; `--configured-only` for just the explicit overrides. Restorable with `xapi_apply_config.py --file` (cloud only). |
| [xapi_bulk_query.py](xapi_bulk_query.py) | Query xStatus/xConfiguration values across the selected device(s) and export to CSV (cloud only). |
| [xapi_clock_sync.py](xapi_clock_sync.py) | Read the codec clock and optionally set the local PC clock to match (needs admin/root). |
| [xapi_dial.py](xapi_dial.py) | Place a call (SIP/Spark, Video/Audio) or show current call status. |
| [xapi_ethernet_mics.py](xapi_ethernet_mics.py) | Enumerate connected microphones and per-stream detail for ethernet audio inputs. |
| [xapi_find_device.py](xapi_find_device.py) | Select org devices by name search (interactive pick) and/or filters and print their ids to stdout for piping into the other fleet tools (cloud only). |
| [xapi_macro_logger.py](xapi_macro_logger.py) | Tail the macro log in real time (SSH `xFeedback`) or by polling (cloud `Macros.Log.Get`). Built as a workaround to the current implementation (as of June 2026) of Cisco's *other* current cloud logging facility in Collaboration Hub, which **will** let you down (stops outputting) when it is faced with a busy log file. But this way is also handy if you need to watch these logs on a long running basis in a side window, and don't have local\SSH access to a codec.|
| [xapi_notice.py](xapi_notice.py) | Display or clear on-screen alerts and textline overlays. |
| [xapi_selfview.py](xapi_selfview.py) | Toggle self-view: off, PiP thumbnail, or full-screen. |
| [xapi_send_message.py](xapi_send_message.py) | Push a `Message Send` onto the macro bus (macros subscribe via `Event/Message/Send`). |

`--help` *probably* implemented in most places 😜 
