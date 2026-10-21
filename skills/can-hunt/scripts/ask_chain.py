# -*- coding: utf-8 -*-
"""
Цепочка инструкций одним фоновым процессом: car.py ask для каждого шага по очереди.
Claude запускает её в фоне (run_in_background) и просыпается один раз — в конце цепочки
или раньше, если что-то пошло не так (пользователь написал сообщение, пропала связь, таймаут).

Файл шагов — по строке на шаг:  ТЕКСТ || КНОПКИ   (кнопки — как у car.py --buttons)
Пустые строки и строки с # пропускаются.

    python ask_chain.py steps.txt [--port N] [--timeout 900]

Код выхода — код car.py на шаге, где цепочка остановилась (0 — все шаги пройдены).
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 3
    steps_file = args[0]
    extra = args[1:]
    port = []
    timeout = ["--timeout", "900"]
    i = 0
    while i < len(extra):
        if extra[i] == "--port" and i + 1 < len(extra):
            port = ["--port", extra[i + 1]]; i += 2
        elif extra[i] == "--timeout" and i + 1 < len(extra):
            timeout = ["--timeout", extra[i + 1]]; i += 2
        else:
            i += 1
    steps = []
    for line in io.open(steps_file, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        text, _, buttons = line.partition("||")
        steps.append((text.strip(), buttons.strip()))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for n, (text, buttons) in enumerate(steps, 1):
        cmd = [sys.executable, os.path.join(HERE, "car.py")] + port + ["ask", text] + timeout
        if buttons:
            cmd += ["--buttons", buttons]
        r = subprocess.run(cmd, capture_output=True, env=env)
        out = r.stdout.decode("utf-8", "replace").strip()
        answer = [l for l in out.splitlines() if not l.startswith("  кнопки") and not l.startswith("Жду ")]
        print("[%d/%d] %s" % (n, len(steps), " | ".join(answer)[-400:]))
        sys.stdout.flush()
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip()
            if err:
                print("  stderr: " + err[-400:])
            print("ЦЕПОЧКА ОСТАНОВЛЕНА на шаге %d, код %d" % (n, r.returncode))
            return r.returncode
    print("ЦЕПОЧКА ПРОЙДЕНА: %d шагов" % len(steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
