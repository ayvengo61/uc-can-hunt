# UniversalController Monitor: download, run and use

A step-by-step guide for first-time users. Nothing needs to be installed on the system: the monitor is a single
program that starts with a double click.

## 1. What you need

- A laptop or PC with Windows 10 or 11.
- A UniversalController board: G4, H723 or H503. Boards ship already flashed.
- A USB cable (data, not charge-only) from the computer to the board.
- For work with a car: the board wired to the CAN bus (CAN-H and CAN-L) and 12 V power. For a first look one USB
  cable is enough: the board runs from it, the bus will just be empty.

## 2. Download

1. Open https://github.com/ayvengo61/uc-can-hunt.
2. Click the green **Code** button, then **Download ZIP**.
3. Unpack the archive into any folder, for example `C:\UC`. Inside you will find a `monitor` folder with three
   subfolders: `G4`, `H723`, `H503`. Use the one that matches your board (the board name is printed on it).

The folder can be moved anywhere, but the `can_sessions` folder with bus recordings is created next to the program,
so do not keep it on a USB stick you will unplug later.

## 3. Start the monitor

1. Connect the board to the computer over USB.
2. Open your board's folder and run `UC_G4_Monitor.exe` (or `UC_H723_Monitor.exe`, `UC_H503_Monitor.exe`).
3. On first start Windows may show a blue "Windows protected your PC" window. Click **More info**, then
   **Run anyway**. This is the standard warning for any program without a paid code-signing certificate; there is
   no malware involved.
4. A small monitor window opens, and a browser tab with the board overview.

How to tell it works: the monitor window shows a green line **"● Связь есть — COM…"** (link OK) and the
**"Кадров"** (frames) counter keeps growing.

If instead you see **"● Контроллер не подключён или спит"** (board not connected or asleep):
- make sure the USB cable carries data (many charger cables carry power only);
- the board may have gone to sleep: unplug and plug the USB again;
- pick the COM port manually in the **"Порт"** (port) list if auto-detection chose the wrong one;
- close other programs that may hold the port (CAN Hacker, STM32CubeIDE, serial terminals).

Starting the program a second time does not open a second window; it just opens the page of the running monitor.
Closing the window stops the monitor. The **"Выход"** (exit) button does the same.

## 4. Where things are

The monitor shows everything in the browser. Page address: `http://127.0.0.1:8473` for G4, `8723` for H723,
`8503` for H503. If the page was closed, reopen it with the buttons in the monitor window:

| Button in the monitor window | What it opens |
|---|---|
| (opens by itself) | **Overview**: board state, inputs, outputs, buses, errors |
| **Приложение** (application) | **Settings**: inputs, outputs and rules, CAN and LIN tables, sleep, protection, firmware |
| **CAN сниффер** (CAN sniffer) | **Sniffer**: live CAN frames, recording to file, frame transmission |
| **Совместная работа** (collaboration) | The page for hunting signals together with Claude (section 8) |

The pages are in Russian; the table above gives the English meaning of every control you will meet.

## 5. Board settings (the "Приложение" button)

1. Click **"Прочитать из контроллера"** (read from the controller). A counter "чтение 1/29…" runs at the bottom;
   after a few seconds a green "прочитано из контроллера" (read) appears.
2. The sections are listed on the left: **Входы** (inputs), **Выходы и правила** (outputs and rules), **CAN0**
   (G4 and H723 also have CAN1, CAN2), **LIN**, **Сон** (sleep), **Диагностика** (diagnostics),
   **Защита прошивки** (firmware protection), **Инструменты** (tools).
3. Change what you need. Edited fields are highlighted, and the button at the top shows the number of edits:
   **"Записать в контроллер (N)"** (write to the controller).
4. Click **"Записать в контроллер"**. The board validates the settings and stores them in Flash: they survive a
   power cycle. If the board rejects a setting, a red line at the top explains exactly what is wrong.
5. **"Отменить правки"** (discard edits) returns everything to what the board currently holds.

The sections you will use most:

- **Inputs**: mode of each input (digital, analog, off), thresholds, inversion.
- **Outputs and rules**: every output can be switched by a condition. A condition is up to four comparisons such as
  "input 0 > 0" or "CAN signal RPM > 2000", joined with AND / OR. A momentary push button has a mode where every
  press toggles the output.
- **CAN0**: bus speed, "listen only" mode (safe, the board sends nothing) or transmit mode. Below it the
  **"Сигналы приёма"** table (which bits of which frames the board reads) and **"Кадры передачи"** (what it sends
  and when: bytes, period, trigger condition, start delay). Bytes are typed right in the row, like in CAN Hacker.
- **Sleep**: when the board goes to sleep (no CAN frames, inputs idle) and what wakes it. For bench work tick
  **"не спать, пока открыт монитор"** (stay awake while the monitor is open), otherwise the board falls asleep after
  a few quiet seconds and disappears from USB.
- **Tools**: the **"Прошить"** (flash) button to update the firmware from a .hex or .elf file. Needed only if you
  were sent a new firmware.

## 6. Sniffer (the "CAN сниффер" button)

1. Choose the channel and the bus speed (passenger cars mostly use 500 or 125 kbit/s) and click **"Включить"**
   (enable).
2. **"Лента"** (tape) mode shows frames one after another; **"По ID"** (by ID) mode shows one row per ID with a
   counter, period and the latest data. Changing bytes are highlighted. Double-click a row to split the frame into
   bits.
3. **"Пауза"** freezes the screen, **"Очистить"** (clear) resets the table.
4. **"Запись в файл"** (record to file) writes everything into the `can_sessions` folder next to the program; Claude
   analyses these recordings later.
5. Transmission (**"Отправить один раз"** send once, **"Слать периодически"** send periodically, **"Стоп всё"**
   stop all) works only when the channel is switched to active mode. By default the sniffer listens silently and
   sends nothing into the car.

Important: transmitting frames changes how the car behaves. Send frames only while parked and only if you understand
what you are doing. **"Стоп всё"** stops any transmission immediately.

## 7. Flashing the board

Boards ship flashed; an update is needed only if you were sent a new firmware file.

1. In settings open **Инструменты**, choose the file and click **"Прошить"**.
2. An H503 restarts by itself. For G4 and H723 the monitor asks you to replug USB (or power): after that the board
   starts in bootloader mode and the monitor finishes the flashing on its own. Just watch the log on the page.
3. Your settings are kept: the board migrates them to the new version automatically.

If the board does not answer after the update, replug USB once more and wait for the line "новая прошивка работает"
(new firmware is running).

## 8. Working together with Claude: hunting signals in the car

Use this mode when you do not know which CAN frame carries the data you need (RPM, speed, a steering-wheel button).
You sit in the car, Claude puts short instructions on the screen, records the bus and finds the field itself.

One-time setup:

1. **Python 3**: https://www.python.org/downloads/, during setup tick **Add python.exe to PATH**. Then run
   `pip install numpy pillow` in a command prompt.
2. **Claude Code**: https://claude.ai/code. A paid Claude plan (Pro or Max) or API billing is required;
   Claude Code does not work on a free account.
3. The plugin. In Claude Code enter two commands:

```
/plugin marketplace add ayvengo61/uc-can-hunt
/plugin install uc-can-hunt@uc-tools
```

How to use it:

1. Start the monitor of your board and click **"Совместная работа"**. A page with a large instruction opens. Click
   the yellow **"Звук выключен"** (sound off) bar at the top so you hear when a new instruction arrives.
2. In Claude Code say what you are looking for: "let's find RPM", "find the volume button on the wheel", "looking for
   the brake pedal signal".
3. Do exactly what the page says and press the button under the instruction once the state is steady ("Holding
   2000", "Released"). If something is wrong, type into **"Сообщение для Claude"** (message to Claude).
4. When the field is found, Claude shows its live value on the same page so you can compare it with the dashboard,
   and writes the description into the `can_hunt_found` folder of your project.

Safety: engine and RPM only while parked (P or neutral, handbrake on). Never read the screen or press buttons while
driving. Do not hold the steering wheel at full lock for more than 5 seconds.

## 9. Troubleshooting

| What you see | What to do |
|---|---|
| "Контроллер не подключён или спит" (not connected or asleep) | Data cable, replug USB, pick the COM port manually, close other programs that use the port |
| The board disappears after a few seconds | It went to sleep. In settings, section "Сон", enable "не спать, пока открыт монитор" or disable sleep |
| The page did not open by itself | Open `http://127.0.0.1:8473` (8723, 8503) in the browser. If the port is busy the monitor takes the next one; the address is shown in its window |
| Blue "Windows protected your PC" window | "More info" → "Run anyway" |
| "контроллер не ответил на get …" while reading | The board has an old firmware. Reload the page and read again: the new page skips missing sections |
| The sniffer is empty | Check the bus speed and the CAN-H / CAN-L wiring; on the bench without a car there are no frames anyway |
| "Запись не прошла проверку" (verify failed) while flashing | Replug USB and retry; the old firmware stays intact |

If nothing helps, send the text from the monitor window (log) and a screenshot of the page: they show at which step it
stopped.
