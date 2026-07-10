# Split Config Models Schema

`src/pynchy/config/models.py` exceeds the file-length budget and currently needs
a `# allow: file-length` exemption for small core config additions.

## Context

The module contains many unrelated core config submodels. Future config work
should split it into smaller focused modules while preserving the root
`Settings` composition surface.

## Plan

- Group related config models into focused modules.
- Keep strict `_StrictModel` behavior for each extracted model.
- Update imports and focused config tests.

## Done

Not started.
