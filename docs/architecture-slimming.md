# Architecture slimming guardrails

This project is being slimmed toward a layered architecture where business rules live below adapters.
The guardrails below are enforced by `.importlinter` and `tests/architecture/test_structure.py`.

## Layers

- `autodl_helper.core`, `api`, `auth`, `config`, `notify`, `runtime`, `storage`: reusable core/runtime support.
- `autodl_helper.tasks`: task orchestration for keeper and scheduled-start behavior.
- `autodl_helper.services`: platform service backends and service management.
- `autodl_helper.ui`: terminal UI adapter.
- `autodl_helper.cli`: command-line adapter and command dispatch.

## Dependency direction

Allowed direction is adapters -> business/runtime layers, never the reverse.

- Core/runtime, tasks, and services must not import `autodl_helper.cli` or `autodl_helper.ui`.
- UI code may call reusable lower layers, but must not import the CLI app entrypoint (`autodl_helper.cli.app`).
- CLI internals must not import CLI entrypoints such as `autodl_helper.cli.app`.

## Import style

`from ... import *` is forbidden under `autodl_helper`.
Explicit imports keep module ownership visible and make it possible to enforce architectural boundaries.

## CLI slimming rule

The CLI should be an adapter: parse arguments, delegate to lower-level application/task/service functions, and render output.
Business logic should move into core/task/service modules where it can be tested independently and reused by CLI and UI.
Structure tests express this by keeping compatibility façades thin and by preventing CLI boundary modules from growing new helper/class definitions.

## Removed legacy rule

Old guardrails that referenced the legacy interactive shim module were removed. New rules target the current package boundaries (`ui`, `cli.app`) instead of legacy shim names.
