# Contributing

## Development environment

```bash
git clone https://github.com/yangpengpeng/autodl-helper.git
cd autodl-helper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Local commands

Run tests:

```bash
./.venv/bin/python -m pytest -q
```

Run the interactive console:

```bash
./.venv/bin/python main.py interactive --config config.yaml
```

Run the daemon locally:

```bash
./.venv/bin/python main.py run-daemon --config config.yaml
```

## Config and local state

Do not commit local runtime files. These stay local:

- `.env`
- `config.yaml`
- `.autodl-helper-auth.json`
- `.autodl-helper-state.json`
- `.autodl-helper.lock`
- `data/`
- `logs/`

Use:

- `config.example.yaml` as the public template
- `config.test.yaml` for tests

## Pull request checklist

Before opening a PR:

1. Run `./.venv/bin/python -m pytest -q`
2. Verify no secrets or account identifiers were added
3. Update `README.md` if CLI behavior changed
4. Update `CHANGELOG.md` for user-visible changes

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
