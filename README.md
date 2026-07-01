# roomOS

Small Python CLI utilities for controlling and observing Cisco RoomOS codecs via either **local SSH xAPI** or **Webex Cloud xAPI**.

Each script exposes two subcommands: `local` (SSH) and `cloud` (REST). Shared plumbing (config, SSH, and cloud xAPI calls) lives in [roomos_common.py](roomos_common.py), which must sit alongside the scripts.

## Requirements

```
pip install paramiko requests pyyaml
```

(`paramiko` is only needed for `local` SSH mode; `requests` for `cloud` mode.)

## Configuration

**Cloud mode** needs a Webex token and a target device:

- **Token** — read from `~/Personal-Local/config.yml` (the same file the sibling Cisco Collab repos use), or passed with `--token`:

  ```yaml
  wxteams:
    auth_token: <a Webex access token>
  ```

  (On Windows that's `C:\Users\<you>\Personal-Local\config.yml`.) The token needs the Cloud xAPI scopes **`spark:xapi_statuses`** and **`spark:xapi_commands`** — these are **not** included in `spark:all` and must be added to the integration explicitly (a token without them gets a `403` on xStatus/xCommand calls). Reading device configurations (`roomOS_bulk_query.py --config`) and listing devices instead use admin device scopes such as `spark-admin:devices_read`.

- **Device ID** — supplied per run, since it changes often during a session: pass **`--device-id <id>`**, or set the **`ROOMOS_DEVICE_ID`** environment variable as a session default (`--device-id` overrides it). It is intentionally *not* stored in `config.yml`.

**Local mode** needs no config file — pass `--host`, `--username`, and `--password` (omit to be prompted) or `--key`, plus optional `--port`/`--timeout`.

## Conventions

- Repeatable command arguments use `--kv key=value` (e.g. `--kv CallType=Video`).
- Cloud commands print a short human-readable summary by default; add `--json` for the raw API response.
- `roomOS_bulk_query.py` xStatus/xConfiguration paths are **case-sensitive** — they must match the RoomOS xAPI casing (PascalCase, e.g. `SystemUnit.Uptime`, `Audio.DefaultVolume`).

## Utilities

| Script | Purpose |
| --- | --- |
| [roomOS_bulk_query.py](roomOS_bulk_query.py) | Query xStatus/xConfiguration values across a filtered set of org devices (model/kind/type/platform/connection) and export to CSV (cloud only). |
| [roomOS_clock_sync.py](roomOS_clock_sync.py) | Read the codec clock and optionally set the local PC clock to match (needs admin/root). |
| [roomOS_dial.py](roomOS_dial.py) | Place a call (SIP/Spark, Video/Audio) or show current call status. |
| [roomOS_ethernet_mics.py](roomOS_ethernet_mics.py) | Enumerate connected microphones and per-stream detail for ethernet audio inputs. |
| [roomOS_macro_SSHlogger.py](roomOS_macro_SSHlogger.py) | Tail the macro log in real time (SSH `xFeedback`) or by polling (cloud `Macros.Log.Get`). |
| [roomOS_notice.py](roomOS_notice.py) | Display or clear on-screen alerts and textline overlays. |
| [roomOS_selfview.py](roomOS_selfview.py) | Toggle self-view: off, PiP thumbnail, or full-screen. |
| [roomOS_send_message.py](roomOS_send_message.py) | Push a `Message Send` onto the macro bus (macros subscribe via `Event/Message/Send`). |

Run any script with `-h` / `--help` for full flag documentation.
