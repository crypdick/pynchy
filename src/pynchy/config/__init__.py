"""Configuration -- settings, models, access resolution, directives."""

# Re-export the main settings interface so `from pynchy.config import get_settings`
# resolves to the canonical `pynchy.config.settings` module.
from pynchy.config.settings import *  # noqa: F403
