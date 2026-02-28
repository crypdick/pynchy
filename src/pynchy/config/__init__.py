"""Configuration -- settings, models, access resolution, directives."""

# Re-export the main settings interface so `from pynchy.config import get_settings`
# continues to work. Callers should migrate to `from pynchy.config.settings import ...`
# over time.
from pynchy.config.settings import *  # noqa: F401,F403
