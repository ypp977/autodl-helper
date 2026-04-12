# Open-source release checklist

## Must review before publishing

- [ ] Replace any private repository URL in `README.md` / `pyproject.toml`
- [ ] Confirm `config.example.yaml` contains no real account data
- [ ] Confirm `.env.template` contains placeholders only
- [ ] Remove runtime artifacts from the working tree:
  - [ ] `data/`
  - [ ] `logs/`
  - [ ] `.cache/`
  - [ ] `.autodl-helper-auth.json`
  - [ ] `.autodl-helper-state.json`
  - [ ] `.autodl-helper.lock`
- [ ] Check screenshots in `images/` for private account data
- [ ] Check git history for leaked tokens or phone numbers

## Project hygiene

- [ ] `README.md` explains what the project is and how to run it
- [ ] `LICENSE` is present
- [ ] `CONTRIBUTING.md` is present
- [ ] package metadata is defined in `pyproject.toml`
- [ ] `requirements.txt` matches runtime dependencies
- [ ] test suite passes locally

## Optional but recommended

- [ ] Add CI for tests on pull requests
- [ ] Add issue templates
- [ ] Add screenshot redaction for docs
- [ ] Publish a first tagged release
