# Integrations

Connect Pynchy workspaces to the external services that support your work.
Built-in integrations use plugins, so you can replace them or add services that
fit your environment.

## Google

| Integration | What it does |
|-------------|--------------|
| [Google integrations](google/index.md) | Set up one or more Google services for a workspace. |

## Communication

| Integration | What it does |
|-------------|--------------|
| [GitHub PR notifications](github.md) | Send signed pull-request events to their mapped workspace. |
| [Matrix communications](matrix-gateway.md) | Read bridged chats and send approval-gated replies as the account owner. |
| [Proton Mail](proton-mail.md) | Read and send mail through a host-side MCP server. |
| [Slack MCP](slack-mcp.md) | Give agents read access to Slack through browser-session tokens. |
| [X (Twitter)](x-integration.md) | Post and interact with X through browser automation. |

## Work management

| Integration | What it does |
|-------------|--------------|
| [Linear](linear.md) | Keep workspace task boards and receive Linear callbacks. |
| [Notebooks](notebooks.md) | Create and execute Jupyter notebooks and Quarto documents. |

---

**Want to customize this?** Write your own plugin — see the [Plugin Authoring Guide](../plugins/index.md). Have an idea but don't want to build it? [Open a feature request](https://github.com/crypdick/pynchy/issues).
