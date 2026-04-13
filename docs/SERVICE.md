# Service Management

`autodl-helper` exposes one public service-management command set:

- `service-install`
- `service-start`
- `service-stop`
- `service-restart`
- `service-status`
- `service-uninstall`

The implementation behind those commands is selected by the current operating system.

## Shared conventions

All backends follow the same conventions:

- use the Python interpreter that is currently running the CLI (`sys.executable`)
- treat the directory containing `config.yaml` as the working directory
- write logs under `logs/`
  - `logs/service.stdout.log`
  - `logs/service.stderr.log`
- manage one foreground daemon process per installed service

## Platform support

| Platform | Backend | Notes |
| --- | --- | --- |
| macOS | LaunchAgent | Uses `launchctl` and `~/Library/LaunchAgents` |
| Linux | systemd user service | Uses `systemctl --user` and `~/.config/systemd/user` |
| Windows | Task Scheduler | Uses `schtasks` and a login trigger |

## macOS: LaunchAgent

On macOS, the service backend is LaunchAgent.

Typical behavior:

- install a plist into `~/Library/LaunchAgents`
- bootstrap the service with `launchctl`
- keep the daemon running in the background
- expose status through `service-status`

Use this backend when the project runs on macOS and you want a managed background daemon.

## Linux: systemd user service

On Linux, the service backend is a user-level systemd unit.

Typical behavior:

- write a unit file under `~/.config/systemd/user/`
- enable and start it with `systemctl --user`
- keep the daemon tied to the current user session
- expose status through `service-status`

This backend does not require root in the first version.

## Windows: Task Scheduler

On Windows, the service backend is Task Scheduler.

Typical behavior:

- create one scheduled task named `autodl-helper`
- trigger it when the user logs in
- run the daemon with the current Python interpreter
- expose status through `service-status`

This first version is intentionally task-based, not a Windows Service.

## Command expectations

The service commands keep the same names on every platform.

- `service-install` installs the platform backend
- `service-start` starts the installed service
- `service-stop` stops the installed service
- `service-restart` restarts the installed service
- `service-status` shows the service and daemon state
- `service-uninstall` removes the installed service

If a backend is not available on the current platform, the command should fail with a clear platform-specific message.
