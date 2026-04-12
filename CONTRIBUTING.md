# Contributing

欢迎提交 issue 和 pull request。

## Before you open a PR

1. Run `python -m pytest -q`
2. Run `python -m ruff check .`
3. Verify no secrets or account identifiers were added
4. Update `README.md` if CLI behavior changed
5. Update `CHANGELOG.md` for user-visible changes

## Development setup

详细开发说明见：

- `docs/DEVELOPMENT.md`
- `docs/TROUBLESHOOTING.md`

公共示例配置见：

- `config.example.yaml`
- `config.test.yaml`

## Scope guidance

This repository is intentionally CLI-first.

Keep changes aligned with:

- scheduled-start grabbing
- keeper keepalive
- multi-account runtime control
- interactive terminal UX
- local SQLite persistence

Avoid mixing in:

- web server code
- desktop app code
- unrelated frontend assets
