---
name: no-junk-drawers
enabled: true
event: file
conditions:
  - field: file_path
    operator: regex_match
    pattern: (utils|helpers|misc|common|shared|general)\.py$
action: warn
---

You're creating or editing a junk-drawer module. The blog principle "treat directory structure and filenames as an interface" means every file should have a clear, domain-specific purpose.

Instead of `utils.py`, name the module after what it actually does:
- `billing/compute.py` not `billing/utils.py`
- `auth/tokens.py` not `auth/helpers.py`
- `parsing/csv_reader.py` not `common/misc.py`

The problem isn't shared code — it's *anonymous* shared code. If you genuinely need a shared utility, name it after what it does and put it where it belongs. Prefer a well-named shared package with centralized invariants over hand-rolled helpers scattered across domains. But if the functions are only used by one module, they belong in that module.
