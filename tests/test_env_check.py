"""Minimal environment self-check: verifies core libraries import. No network, no DB."""
import fastapi
import httpx
import jinja2
import pydantic_settings
import pytest
import requests
import sqlalchemy


def test_core_framework_imports():
    assert sqlalchemy.__version__ and fastapi.__version__


def test_http_clients_importable():
    assert requests.__version__ and httpx.__version__


def test_templates_and_settings_importable():
    assert jinja2.__version__ and pydantic_settings.__version__


def test_pytest_itself():
    assert pytest.__version__
