# roomOS

Small Python CLI utilities for controlling and observing Cisco RoomOS codecs via either **local SSH xAPI** or **Webex Cloud xAPI**.

Each script exposes two subcommands: `local` (SSH) and `cloud` (REST). Cloud mode reads `token` and `device_id` from `config.yaml` (see [config.sample.yaml](config.sample.yaml)); CLI flags override file values.

## Requirements

```
pip install paramiko requests pyyaml
```

## Utilities

| Script | Purpose |
| --- | --- |
| [roomOS_clock_sync.py](roomOS_clock_sync.py) | Read the codec clock and optionally set the local PC clock to match (needs admin/root). |
| [roomOS_dial.py](roomOS_dial.py) | Place a call (SIP/Spark, Video/Audio) or show current call status. |
| [roomOS_ethernet_mics.py](roomOS_ethernet_mics.py) | Enumerate connected microphones and per-stream detail for ethernet audio inputs. |
| [roomOS_macro_SSHlogger.py](roomOS_macro_SSHlogger.py) | Tail the macro log in real time (SSH `xFeedback`) or by polling (cloud `Macros.Log.Get`). |
| [roomOS_notice.py](roomOS_notice.py) | Display or clear on-screen alerts and textline overlays. |
| [roomOS_selfview.py](roomOS_selfview.py) | Toggle self-view: off, PiP thumbnail, or full-screen. |
| [roomOS_send_message.py](roomOS_send_message.py) | Push a `Message Send` onto the macro bus (macros subscribe via `Event/Message/Send`). |

Run any script with `-h` / `--help` for full flag documentation.
