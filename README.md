# autodl-helper

`autodl-helper` 是一个 AutoDL 自动化命令行工具，核心只做四件事：

- 抢机轮询
- Keeper 保活与止损
- 守护进程后台运行
- 本地配置、日志、SQLite 历史与基础诊断

项目不提供图形界面客户端。Docker 镜像只运行守护进程。macOS、Windows、Linux 推荐通过 `pipx`、`venv` 或 Nuitka 控制台可执行文件使用。

## 公开入口

```bash
autodl-helper --help
python main.py --help
python -m autodl_helper --help
```

## 文档

- 命令说明：`docs/COMMANDS.md`
- 配置说明：`docs/CONFIGURATION.md`
- Docker：`docs/DOCKER.md`
- 服务托管：`docs/SERVICE.md`
- 架构瘦身规范：`docs/architecture-slimming.md`
- 开发指南：`docs/DEVELOPMENT.md`
- 排障手册：`docs/TROUBLESHOOTING.md`

## 平台支持

| 能力 | macOS | Linux | Windows | Docker |
| --- | --- | --- | --- | --- |
| 命令行 | ✅ | ✅ | ✅ | ✅ |
| 守护进程 | ✅ | ✅ | ✅ | ✅ |
| 后台服务托管 | LaunchAgent | systemd 用户服务 | Task Scheduler | 不适用 |
| 终端 UI | ✅ | ✅ | ✅ | ❌ |
| Nuitka 可执行文件 | ✅ | 可自行构建 | ✅ | 不适用 |

## 安装

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

从源码运行时，把 `autodl-helper` 换成 `./.venv/bin/python main.py`。Windows 把 `./.venv/bin/python` 换成 `.\.venv\Scripts\python`。

## 常用命令

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

## 配置填写提示

- Keeper 主要看两个时间：关机后最长保留多久、释放前多久开始保活。
- 抢机目标时间在 UI 中可输入 `9`、`930`、`09:30`、`1430`。
- 抢机提前时间在 UI 中可输入 `90m`、`1.5h`、`2h`。
- 单次抢机会要求填写执行日期，格式是 `YYYY-MM-DD`。

## Docker 守护进程

Docker 只负责运行守护进程，不支持 UI、不发布端口。

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

Docker Compose：

```bash
docker compose up -d --build
```

## 本地文件

默认本地文件：

- `config.yaml`
- `data/autodl-helper.db`
- `logs/`
- `.cache/*.json`
- `.autodl-helper-*.json`

这些文件包含本地状态或凭据，不应提交。

## 架构

目标结构见 `docs/architecture-slimming.md`。当前公开接口只保证命令入口和 `config.yaml` 字段兼容；内部导入路径不作为兼容面维护。

## 开发

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m ruff check .
./.venv/bin/python -m importlinter
./.venv/bin/python -m py_compile $(find autodl_helper -name '*.py') main.py
```

## 限制

- Docker 仅运行守护进程，不支持 UI。
- SQLite 历史允许重建，不提供跨版本迁移承诺。
- Playwright 依赖目标环境的浏览器安装。
- Nuitka 可执行文件不包含浏览器资源。

## 许可

MIT，见 `LICENSE`。

## 致谢

本项目当前由 `ypp977` 独立维护。项目早期参考并演进自 [turbo-duck/autodl-keeper](https://github.com/turbo-duck/autodl-keeper)，并保留 MIT 许可要求的许可与版权声明。
