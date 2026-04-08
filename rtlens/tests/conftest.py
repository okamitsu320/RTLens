"""Shared fixtures and helpers for rtlens tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_path(name: str) -> str:
    return str(FIXTURES_DIR / name)
