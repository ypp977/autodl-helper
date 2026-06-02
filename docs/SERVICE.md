# 后台服务托管

`autodl-helper` 对外提供一组统一的后台服务命令：

- `service install`
- `service start`
- `service stop`
- `service restart`
- `service status`
- `service uninstall`

具体托管实现会按当前操作系统自动选择。

## 统一约定

所有平台后端遵守同一套约定：

- 使用当前运行 CLI 的 Python 解释器，也就是 `sys.executable`。
- 把 `config.yaml` 所在目录作为工作目录。
- 日志写入 `logs/`：
  - `logs/service.stdout.log`
  - `logs/service.stderr.log`
- 每个已安装服务管理一个前台守护进程。

## 平台支持

| 平台 | 后端 | 说明 |
| --- | --- | --- |
| macOS | LaunchAgent | 使用 `launchctl` 和 `~/Library/LaunchAgents` |
| Linux | systemd 用户服务 | 使用 `systemctl --user` 和 `~/.config/systemd/user` |
| Windows | Task Scheduler | 使用 `schtasks` 和登录触发器 |

## macOS：LaunchAgent

macOS 使用 LaunchAgent。

典型行为：

- 将 plist 文件安装到 `~/Library/LaunchAgents`。
- 使用 `launchctl` 加载服务。
- 在后台保持守护进程运行。
- 通过 `service status` 查看服务状态。

## Linux：systemd 用户服务

Linux 使用用户级 systemd unit 文件。

典型行为：

- 将 unit 文件写入 `~/.config/systemd/user/`。
- 使用 `systemctl --user` 启用和启动。
- 将守护进程绑定到当前用户会话。
- 通过 `service status` 查看服务状态。

当前实现不要求管理员权限。

## Windows：Task Scheduler

Windows 使用 Task Scheduler。

典型行为：

- 创建名为 `autodl-helper` 的计划任务。
- 用户登录时触发。
- 使用当前 Python 解释器运行守护进程。
- 通过 `service status` 查看服务状态。

当前实现刻意使用计划任务，而不是 Windows 服务。

## 命令语义

各平台命令名称保持一致：

- `service install`：安装当前平台的后台托管。
- `service start`：启动已安装服务。
- `service stop`：停止已安装服务。
- `service restart`：重启已安装服务。
- `service status`：显示服务和守护进程状态。
- `service uninstall`：卸载后台托管。

如果当前平台不支持对应后端，命令应返回清晰的平台相关错误。
