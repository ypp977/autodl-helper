# autodl-helper

`autodl-helper` 是一个小而美的 AutoDL 自动化 CLI 工具，核心只做四件事：

- scheduled-start 抢机轮询
- Keeper 保活与止损
- daemon 后台运行
- 本地配置、日志、SQLite 历史与基础诊断

项目不提供 GUI 客户端。Docker 镜像只跑 daemon。macOS / Windows / Linux 推荐通过 `pipx`、`venv` 或 Nuitka console executable 使用。

## Public entrypoints

```bash
autodl-helper --help
python main.py --help
python -m autodl_helper --help
```

## Documentation

- 命令说明：`docs/COMMANDS.md`
- 配置说明：`docs/CONFIGURATION.md`
- Docker：`docs/DOCKER.md`
- 服务托管：`docs/SERVICE.md`
- 架构瘦身规范：`docs/architecture-slimming.md`
- 开发指南：`docs/DEVELOPMENT.md`
- 排障手册：`docs/TROUBLESHOOTING.md`

## Platform support

| Capability | macOS | Linux | Windows | Docker |
| --- | --- | --- | --- | --- |
| CLI | ✅ | ✅ | ✅ | ✅ |
| daemon | ✅ | ✅ | ✅ | ✅ |
| service backend | LaunchAgent | systemd user | Task Scheduler | 不适用 |
| terminal UI | ✅ | ✅ | ✅ | ❌ |
| Nuitka executable | ✅ | 可自行构建 | ✅ | 不适用 |

## Install

推荐二选一：

```bash
pipx install .
pipx inject autodl-helper playwright==1.58.0 --include-apps
playwright install chromium
```

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/playwright install chromium
```

首次运行：

```bash
autodl-helper init
autodl-helper ui --config config.yaml
```

从源码运行时把 `autodl-helper` 换成 `./.venv/bin/python main.py`。Windows 把 `./.venv/bin/python` 换成 `.\.venv\Scripts\python`。

## Common commands

```bash
autodl-helper ui --config config.yaml
autodl-helper run keeper --config config.yaml
autodl-helper run scheduled --config config.yaml
autodl-helper run daemon --config config.yaml
autodl-helper service status --config config.yaml
autodl-helper debug health --config config.yaml
autodl-helper debug history --config config.yaml --task keeper
autodl-helper config validate --config config.yaml
```

完整命令只维护在 `docs/COMMANDS.md`。

## Docker daemon

Docker 只负责 daemon，不支持 UI、不发布端口。

```bash
docker build -t autodl-helper:local .
docker run --rm \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -v "$PWD/data:/app/data" \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/.cache:/app/.cache" \
  autodl-helper:local
```

默认容器命令：

```bash
autodl-helper run daemon --config /app/config.yaml
```

Compose:

```bash
docker compose up -d --build
```

## Local files

默认本地文件：

- `config.yaml`
- `data/autodl-helper.db`
- `logs/`
- `.cache/*.json`
- `.autodl-helper-*.json`

这些文件包含本地状态或凭据，不应提交。

## Architecture

目标结构见 `docs/architecture-slimming.md`。当前公开 API 只保证命令入口和 `config.yaml` 字段兼容；内部 import 路径不再作为兼容面维护。

## Development

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m ruff check .
./.venv/bin/python -m importlinter
./.venv/bin/python -m py_compile $(find autodl_helper -name '*.py') main.py
```

## Limitations

- Docker 仅 daemon，不支持 UI。
- SQLite 历史允许重建，不提供跨版本迁移承诺。
- Playwright 依赖目标环境的浏览器安装。
- Nuitka 可执行文件不包含浏览器资源。

## License

MIT. See `LICENSE`.

## Acknowledgement

本项目当前由 `ypp977` 独立维护。项目早期参考并演进自 [turbo-duck/autodl-keeper](https://github.com/turbo-duck/autodl-keeper)，并保留 MIT License 要求的许可与版权声明。
