# copyright by berlonak
# telegram: @Kilax123
"""Adapter for the ORIGINAL login_telethon.py. Do not change that script.

Original script expects: from config import API_ID, API_HASH, TELETHON_SESSION_NAME.
Settings and session path come from the same config.yaml as the monitor.
"""
import os
from pathlib import Path

from sla.config import load_login_settings

CONFIG_PATH = os.environ.get("SLA_CONFIG", str(Path(__file__).with_name("config.yaml")))
API_ID, API_HASH, TELETHON_SESSION_NAME = load_login_settings(CONFIG_PATH)
