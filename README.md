# uc-can-hunt

Монитор контроллера UniversalController (G4 / H723 / H503) и плагин Claude Code для совместного поиска сигналов в
CAN-шине автомобиля. / UniversalController monitor and a Claude Code plugin for collaborative CAN signal hunting.

```
машина ── CAN ── контроллер ── USB ── UC_<плата>_Monitor.exe ── браузер (обзор, настройки, сниффер, совместная работа)
                                             │
                                  Claude Code: навык can-hunt
```

**UniversalController** is an automotive CAN FD / LIN controller for car add-ons: a no-code browser-based
configurator (signal tables, output rules with AND/OR conditions, frame transmission like CAN Hacker), a built-in
CAN / CAN FD / LIN sniffer, sleep mode without battery drain, and **AI-assisted signal hunting** with Claude Code. Boards:
H503 (1×CAN FD, 1×LIN, 8 outputs), G4 and H723 (3×CAN FD, 2×LIN, 12 outputs). Use cases: DRL from turn signals over UDS,
fan by coolant temperature, roller shutter, fuel tank switch, LIN node emulation. Keywords: CAN bus controller, CAN FD
sniffer, LIN, UDS, car hacking, reverse engineering, STM32, Claude Code plugin, no-code automotive controller.

- **По-русски:** папка `RU` — [ИНСТРУКЦИЯ.md](RU/ИНСТРУКЦИЯ.md) и установщик `УСТАНОВКА.bat` — скачать, запустить, настройки, сниффер, прошивка, работа с Claude.
- **In English:** folder `EN` — [GUIDE.md](EN/GUIDE.md) and the `SETUP.bat` installer — download, run, settings, sniffer, flashing, working with Claude.

## Установка плагина / Plugin install

Проще всего запустить `RU\УСТАНОВКА.bat`: он проверит Python, поставит пакеты и подключит плагин. /
Easiest: run `EN\SETUP.bat`: it checks Python, installs packages and connects the plugin. Вручную / manually:

```
/plugin marketplace add ayvengo61/uc-can-hunt
/plugin install uc-can-hunt@uc-tools
```

Нужны Windows, Python 3 с `numpy` и `pillow`, Claude Code (платный план Claude Pro/Max или оплата через API). Платы поставляются прошитыми.
Requires Windows, Python 3 with `numpy` and `pillow`, Claude Code (paid Claude Pro/Max plan or API billing). Boards ship flashed.

## Где купить и как связаться / Where to buy, contact

Страница продукта со скриншотами: https://ayvengo61.github.io/uc-can-hunt/ — там же появятся ссылки на магазины.
Пока магазины открываются, вопросы и заказ — через [Issues](https://github.com/ayvengo61/uc-can-hunt/issues) этого
репозитория (укажите плату, марку и год машины, задачу). /
Product page with screenshots: https://ayvengo61.github.io/uc-can-hunt/ — store links will appear there. Until the
stores open, questions and orders go through this repository's [Issues](https://github.com/ayvengo61/uc-can-hunt/issues)
(board, car make/year, task).

## Состав / Contents

- `monitor/<G4|H723|H503>/` — монитор платы: exe и страницы (обзор, настройки, сниффер, совместная работа); записи
  шины появляются рядом в `can_sessions/`. / Board monitor: exe and pages; recordings go to `can_sessions/` next to it.
- `plugin/` — плагин Claude Code (манифест, навык `can-hunt`: методика, контракт монитора, сценарии по сигналам,
  скрипты). Именно эта папка ставится как плагин и подаётся в каталоги. / The Claude Code plugin (manifest, the
  `can-hunt` skill: method, monitor contract, per-signal playbook, scripts). This folder is what gets installed and listed.

Исходники прошивок и мониторов в пакет не входят. / Firmware and monitor sources are not part of this package.
