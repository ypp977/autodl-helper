# autodl-helper

`autodl-helper` 是一个面向 AutoDL 的 CLI-first 工具，提供：

- 抢机轮询（scheduled-start）
- Keeper 保活
- 多账号运行控制
- 本地 SQLite 历史与事件记录
- 交互式终端控制台
- 平台化后台托管（按操作系统自动选择后端）

当前定位：

- 适合开发者自用
- 适合二次开发
- 适合作为开源 CLI / daemon 项目继续整理

## Documentation

- 配置说明：`docs/CONFIGURATION.md`
- 命令说明：`docs/COMMANDS.md`
- 服务托管说明：`docs/SERVICE.md`
- 架构说明：`docs/architecture.md`
- 开发指南：`docs/DEVELOPMENT.md`
- 隐私与数据说明：`docs/PRIVACY.md`
- 排障手册：`docs/TROUBLESHOOTING.md`
- 开源发布检查：`docs/OPEN_SOURCE_CHECKLIST.md`

## Features

- **scheduled-start 抢机**
  - 固定实例开机
  - 按 selector 条件轮询候选
  - 支持单次 / 每天计划
  - 支持优先级排序

- **Keeper 保活**
  - 根据释放时间推导接管窗口
  - 支持冷却期和 fallback 策略
  - 记录执行历史，避免同一释放周期重复保活

- **多账号**
  - 多账号配置
  - 账号级缓存与运行态控制
  - 支持后台统一调度

- **可观测性**
  - 本地 SQLite 历史
  - daemon 心跳
  - 配置热重载状态
  - 交互式诊断页

- **交互式 CLI**
  - 查看抢机状态
  - 查看 Keeper 计划
  - 查看诊断信息
  - 启停后台服务

## Platform support

| Capability | macOS | Linux | Windows |
| --- | --- | --- | --- |
| CLI commands | ✅ | ✅ | ✅ |
| `interactive` | ✅ | ✅ | ✅ |
| `run-daemon` | ✅ | ✅ | ✅ |
| `service-install/start/stop/restart/status/uninstall` | ✅ LaunchAgent | ✅ systemd user | ✅ Task Scheduler |

## Quick Start

### 1. Clone

```bash
git clone https://github.com/ypp977/autodl-helper.git
cd autodl-helper
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
playwright install chromium
```

### 3. Bootstrap local files

```bash
python main.py init
```

This opens the first-run bootstrap wizard: it checks the local environment, creates local `.env` and `config.yaml` from templates, validates the config, and can jump into `interactive` directly.

### 4. Run

```bash
python main.py interactive --config config.yaml
```

也可以作为本地包安装：

```bash
python -m pip install -e .[dev]
autodl-helper init
autodl-helper --help
```

## Configuration

公开示例配置见：

- `config.example.yaml`
- `docs/CONFIGURATION.md`

测试配置见：

- `config.test.yaml`

默认本地文件：

- 配置：`config.yaml`
- 数据库：`data/autodl-helper.db`
- 日志：`logs/`
- auth cache：`.cache/*.json`
- 本地状态：`.autodl-helper-*.json`

这些文件都不应提交到开源仓库。

## Common Commands

完整命令说明见：

- `docs/COMMANDS.md`

### Run daemon

```bash
python main.py run-daemon --config config.yaml
```

兼容别名：

```bash
python main.py run-all --config config.yaml
```

### Run individual tasks

```bash
python main.py run-keeper --config config.yaml
python main.py run-scheduled-start --config config.yaml
```

### First-run bootstrap

```bash
python main.py init
python main.py interactive --config config.yaml
```

If you need non-interactive defaults or want to overwrite existing local files, run:

```bash
python main.py init --yes
python main.py init --force
```

### Accounts and login

```bash
python main.py accounts --config config.yaml
python main.py login --config config.yaml --account main
python main.py login --config config.yaml --all
```

### Instances and diagnostics

```bash
python main.py list-instances --config config.yaml
python main.py inspect-instance --config config.yaml --instance-id <id>
python main.py watch-instance --config config.yaml --instance-id <id>
python main.py keeper-probe --config config.yaml
python main.py history --config config.yaml --limit 50
python main.py auth-report --config config.yaml
python main.py healthcheck --config config.yaml --smoke
```

### Interactive console

```bash
python main.py interactive --config config.yaml
```

### Service management

服务托管由当前操作系统自动选择后端。完整说明见：

- `docs/SERVICE.md`

通用命令如下：

```bash
python main.py service-install --config config.yaml
python main.py service-start --config config.yaml
python main.py service-status --config config.yaml
python main.py service-stop --config config.yaml
python main.py service-restart --config config.yaml
python main.py service-uninstall --config config.yaml
```

## Architecture

完整架构说明见：

- `docs/architecture.md`

```text
autodl_helper/
├── api.py                  # AutoDL API access
├── auth*.py                # auth, login, cache, policy
├── cli*.py                 # CLI entry, parser, handlers, renderers
├── config.py               # settings model and loading
├── interactive_*.py        # terminal UI
├── runtime_control.py      # daemon heartbeat, reload, runtime flags
├── services/               # platform service backends
├── storage.py              # SQLite store
├── events.py               # history/event summaries
└── tasks/
    ├── keeper.py
    └── scheduled_start.py
```

仓库其余目录：

```text
tests/              # pytest suite
docs/               # release and maintenance docs
scripts/            # helper scripts
config.example.yaml # public example config
config.test.yaml    # test config
```

## Development

开发文档见：

- `docs/DEVELOPMENT.md`
- `docs/SERVICE.md`

Run tests:

```bash
python -m pytest -q
```

Compile check:

```bash
python -m py_compile $(find autodl_helper -name '*.py')
```

贡献说明见：
 
- `CONTRIBUTING.md`
- `docs/DEVELOPMENT.md`
- `docs/TROUBLESHOOTING.md`

开源发布检查清单见：

- `docs/OPEN_SOURCE_CHECKLIST.md`
- `docs/PRIVACY.md`
- `docs/TROUBLESHOOTING.md`
- `docs/CONFIGURATION.md`
- `docs/COMMANDS.md`
- `docs/architecture.md`

## Limitations

- 当前是 **CLI-first** 项目，不包含 Web UI
- 服务托管由平台后端提供，具体支持边界见 `docs/SERVICE.md`
- Playwright 依赖浏览器环境
- 项目仍偏向真实 AutoDL 使用场景，公开发布前仍需人工检查截图、示例配置和历史记录

## Roadmap

适合继续整理的方向：

- 继续完善跨平台服务托管说明
- 持续收敛交互式页面与诊断页结构
- 更清晰的 package / command 文档
- CI 扩展（lint / smoke / release）
- 发布 PyPI 包
- 更标准的日志与事件导出

## License

MIT. See `LICENSE`.

## Acknowledgement

本项目当前由 `ypp977` 独立维护，并作为新的开源仓库持续演进。

项目早期参考并演进自 [turbo-duck/autodl-keeper](https://github.com/turbo-duck/autodl-keeper)。
当前仓库未保留原始 git 提交历史，但会继续保留 MIT License 要求的许可与版权声明。

当前版本的工程结构、交互界面、后台运行链路、配置体系、测试与文档，均按本仓库自己的路线继续维护。
