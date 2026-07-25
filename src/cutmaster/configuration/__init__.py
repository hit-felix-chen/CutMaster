"""Configuration schema and loading."""

from cutmaster.configuration.loader import load_config
from cutmaster.configuration.schema import AppConfig

__all__ = ["AppConfig", "load_config"]
