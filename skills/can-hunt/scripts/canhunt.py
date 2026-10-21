# -*- coding: utf-8 -*-
"""canhunt.py — анализ записанных сессий CAN (SPEC.md §10).

Подкоманды: info, overview, compare, marks, track, corr, plot, decode, export.
Справка: python canhunt.py -h, python canhunt.py <команда> -h.
Зависимости: numpy, PIL. Код — в canhunt_lib\\.
"""
import os
import sys

sys.dont_write_bytecode = True           # не оставлять __pycache__ в папке плагина
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canhunt_lib.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
