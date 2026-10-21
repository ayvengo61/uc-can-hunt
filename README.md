# uc-can-hunt

Монитор контроллера UniversalController (G4 / H723 / H503) и плагин Claude Code для совместного поиска сигналов в
CAN-шине автомобиля. / UniversalController monitor and a Claude Code plugin for collaborative CAN signal hunting.

```
машина ── CAN ── контроллер ── USB ── UC_<плата>_Monitor.exe ── браузер (обзор, настройки, сниффер, совместная работа)
                                             │
                                  Claude Code: навык can-hunt
```

- **Инструкция по-русски:** [ИНСТРУКЦИЯ.md](ИНСТРУКЦИЯ.md) — скачать, запустить, настройки, сниффер, прошивка, работа с Claude.
- **English guide:** [GUIDE.md](GUIDE.md) — download, run, settings, sniffer, flashing, working with Claude.

## Установка плагина / Plugin install

```
/plugin marketplace add ayvengo61/uc-can-hunt
/plugin install uc-can-hunt@uc-tools
```

Нужны Windows, Python 3 с `numpy` и `pillow`, Claude Code (любой аккаунт). Платы поставляются прошитыми.
Requires Windows, Python 3 with `numpy` and `pillow`, Claude Code (any account). Boards ship flashed.

## Состав / Contents

- `monitor/<G4|H723|H503>/` — монитор платы: exe и страницы (обзор, настройки, сниффер, совместная работа); записи
  шины появляются рядом в `can_sessions/`. / Board monitor: exe and pages; recordings go to `can_sessions/` next to it.
- `skills/can-hunt/` — навык для Claude Code: методика, контракт монитора, сценарии по сигналам, скрипты. /
  Claude Code skill: method, monitor contract, per-signal playbook, scripts.

Исходники прошивок и мониторов в пакет не входят. / Firmware and monitor sources are not part of this package.
