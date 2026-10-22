# -*- coding: utf-8 -*-
"""Точка входа Vercel: весь API Mini App живёт во Flask-приложении из webapp.py.

Vercel ожидает в этом файле переменную `app` — это и есть Flask-приложение.
Маршруты настраиваются в vercel.json (/app — статика, остальное — сюда).
"""
import os
import sys

# гарантируем, что корень проекта (webapp/) виден для импорта webapp и storage
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp import app  # noqa: F401, E402
