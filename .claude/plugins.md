# Plugin Security Model

All plugin Python code runs on the host during discovery (`__init__`, `validate()`, category methods). Installing a plugin means trusting its code.

**Rule: only install plugins from authors you trust.**

For the full risk-by-category breakdown, see [Plugin Security](docs/plugins/index.md#security-model).
