# UC CAN Hunt — collaborative CAN signal hunting with Claude

A Claude Code plugin for reverse engineering a car's CAN bus together with Claude. You sit in the car with a laptop;
the UniversalController board (G4, H723 or H503) listens to the bus; the Windows monitor app shows you a page with
large step-by-step instructions. Claude tells you what to do ("hold 2000 rpm"), records the bus, analyses the
recording and finds the frame and bit that carry the signal: RPM, speed, pedals, gear selector, steering-wheel
buttons, doors, locks, lights, temperature, fuel. It then shows the live value on the same page so you can confirm it
against the dashboard, and writes a description plus a firmware config line.

## What you need

- A UniversalController board with the sniffer firmware (boards ship flashed) and the Windows monitor app
  `UC_<board>_Monitor.exe` from the project repository: https://github.com/ayvengo61/uc-can-hunt (folder `monitor/`).
  The monitor is not part of this plugin; you download and start it yourself.
- Windows, Python 3 with `numpy` and `pillow` (`pip install numpy pillow`).
- Claude Code. The skill uses the Bash tool to run its scripts, so it works in Claude Code only.

## What the plugin runs and where data goes

- `scripts/car.py` sends commands to the local monitor over HTTP on 127.0.0.1 (ports 8473 / 8723 / 8503): start the
  sniffer, start and stop a recording, show an instruction on the user's page, wait for the user's button press.
- `scripts/canhunt.py` analyses recordings that the monitor saved locally (`can_sessions/` next to the monitor).
- Nothing is sent to the internet. Findings are written to `can_hunt_found/` in your project folder.
- The scripts read only these environment variables: `UC_PORT`, `CANHUNT_PORTS`, `CANHUNT_SESSIONS`, `CANHUNT_STATE`,
  `CANHUNT_CACHE`, `CANHUNT_NO_CACHE`, `CANHUNT_LINK_GRACE` (port and folder overrides, all optional) and
  `LOCALAPPDATA` / `WINDIR` (where to keep a small state file and find a font for plots). No credentials, tokens or
  keys are read, stored or transmitted.
- Transmitting frames into the car is a separate mode that the skill enables only on explicit request and only while
  parked.

## Install

```
/plugin install uc-can-hunt --marketplace ayvengo61/uc-can-hunt
```

Or in two steps: `/plugin marketplace add ayvengo61/uc-can-hunt`, then `/plugin install uc-can-hunt@uc-tools`.

## How to use

1. Start the monitor of your board and click «Совместная работа» (collaboration). Enable sound on the page.
2. In Claude Code say what you are looking for: "let's find RPM", "find the brake pedal signal".
3. Follow the instructions on the page and press the buttons under them. Claude does the rest.

Guides: [English](https://github.com/ayvengo61/uc-can-hunt/blob/main/EN/GUIDE.md), [Русский](https://github.com/ayvengo61/uc-can-hunt/blob/main/RU/ИНСТРУКЦИЯ.md).
Product page: https://ayvengo61.github.io/uc-can-hunt/
