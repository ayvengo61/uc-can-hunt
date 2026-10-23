# -*- coding: utf-8 -*-
"""canhunt_lib — анализ записанных сессий CAN (SPEC.md §2, §5, §6, §10).

Модули:
  fieldspec — спецификация поля CH:ID:BYTE.BIT:LEN:ORDER[:s][*F][+O] и векторный декодер;
  loader    — разбор candump / PCAN .trc / CAN-Hacker .trc, events.jsonl → фазы, отметки, ввод;
  classify  — классификация байтов (константа, счётчик, контрольная сумма, шум, сигнал, флаги);
  analysis  — compare / marks / track / corr / decode;
  export    — строка для прошивки UniversalController и строка DBC SG_;
  plotting  — PNG-графики через PIL;
  cli       — разбор аргументов и русский вывод.
"""
__version__ = "1.0"
