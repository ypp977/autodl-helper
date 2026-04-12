from __future__ import annotations

import argparse

from autodl_helper.cli import create_store, format_keeper_probe_line, get_enabled_accounts, load_settings, select_accounts, validate_settings, _build_client
from autodl_helper.tasks.keeper import evaluate_keeper_instance


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Probe keeper timing for AutoDL instances')
    parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    parser.add_argument('--headed', action='store_true', help='Use headed Playwright browser mode')
    parser.add_argument('--account', help='Only inspect one configured account')
    parser.add_argument('--only-eligible', action='store_true', help='Only show keeper-eligible instances')
    return parser




def format_probe_line(result, account_name: str = "", executed_in_cycle: bool = False) -> str:
    return format_keeper_probe_line(result, account_name=account_name, executed_in_cycle=executed_in_cycle)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    errors = validate_settings(settings, purpose='run-keeper')
    if errors:
        for error in errors:
            print(error)
        return 1
    store = create_store(settings)
    multiple_accounts = len(get_enabled_accounts(settings)) > 1
    for account in select_accounts(settings, args.account, require_explicit_for_multi=False):
        client = _build_client(settings, args.headed, account=account, store=store)
        for item in client.list_instances():
            result = evaluate_keeper_instance(
                client=client,
                item=item,
                shutdown_release_after_hours=settings.tasks.keeper.shutdown_release_after_hours,
                keeper_trigger_before_hours=settings.tasks.keeper.keeper_trigger_before_hours,
                start_cooldown_minutes=settings.tasks.keeper.start_cooldown_minutes,
                stop_cooldown_minutes=settings.tasks.keeper.stop_cooldown_minutes,
                fallback_to_status_at=settings.tasks.keeper.fallback_to_status_at,
            )
            if args.only_eligible and not result.eligible:
                continue
            executed = bool(result.release_deadline and store.was_keeper_executed_in_cycle(account.name, result.instance_id, result.release_deadline))
            print(format_keeper_probe_line(result, account_name=account.name if multiple_accounts or args.account else '', executed_in_cycle=executed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
