"""Configuration -- settings, models, access resolution, prompts."""

# Re-export the main settings interface so `from pynchy.config import get_settings`
# resolves to the canonical `pynchy.config.settings` module.
from pynchy.config.models import RepoConfig as RepoConfig
from pynchy.config.settings import *  # noqa: F403
