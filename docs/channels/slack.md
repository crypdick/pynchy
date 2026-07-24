# Slack

Connect Pynchy to Slack channels and DMs through Slack Socket Mode.

## Set up Slack

1. Create a Slack app with Socket Mode enabled.
2. Add the bot and app token environment variable names to
   `data/personalization/pynchy.toml`:

   ```toml
   [connections.synapse]
   type = "slack"
   bot_token_env = "SLACK__BOT_TOKEN"
   app_token_env = "SLACK__APP_TOKEN"
   ```

3. Install the optional dependency:

   ```bash
   uv sync --extra slack
   ```

## Capabilities

- Channel and DM support
- Slack Assistant API panel integration
- Streaming message updates
- Markdown formatting

For read-only browser-session access to a Slack workspace where you cannot
install an app, see [Slack MCP](../integrations/slack-mcp.md).

---

**Want to customize this?** Write your own channel plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
