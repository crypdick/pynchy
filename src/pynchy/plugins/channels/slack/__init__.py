"""Built-in Slack channel plugin."""

from pynchy.config import get_settings as get_settings
from pynchy.logger import logger as logger

from ._plugin import SlackChannel as SlackChannel
from ._plugin import SlackChannelPlugin as SlackChannelPlugin
from ._plugin import TtlCache as TtlCache
from ._plugin import __all__ as __all__
from ._plugin import _channel_id_from_jid as _channel_id_from_jid
from ._plugin import _jid as _jid
from ._plugin import _normalize_chat_name as _normalize_chat_name
from ._plugin import _split_text as _split_text
