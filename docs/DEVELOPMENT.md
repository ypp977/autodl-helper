# Development

本项目当前以 **本地 CLI / daemon 开发** 为主，推荐使用 Python 3.11 或 3.12。

## Local setup

```bash
python --version
python -m pip install -r requirements-dev.txt
playwright install chromium
```

如果你更偏向包管理方式，也可以：

```bash
python -m pip install -e .[dev]
```

## Common development commands

### Run tests

```bash
python -m pytest -q
```

### Compile check

```bash
python -m py_compile $(find autodl_helper -name '*.py')
```

### Lint

```bash
python -m ruff check .
```

当前 lint 只接入一层**保守门禁**，主要用于发现：

- 明确的语法问题
- 未定义名称
- 一些会直接导致运行失败的静态错误

这一步是为了让开源仓库先具备稳定的基础门禁，而不是一次性推动大规模风格重构。

## Run the app locally

### Interactive

```bash
python main.py interactive --config config.yaml
```

### Daemon

```bash
python main.py run-daemon --config config.yaml
```

### macOS LaunchAgent

```bash
python main.py service-install --config config.yaml
python main.py service-start --config config.yaml
python main.py service-status --config config.yaml
```

## Suggested workflow

1. 从 `config.example.yaml` 复制一份本地 `config.yaml`
2. 在 `.env` 中补齐必要环境变量
3. 先跑：
   - `pytest -q`
   - `ruff check .`
   - `py_compile`
4. 再手动验证：
   - interactive 页面
   - keeper / scheduled-start
   - daemon 或 service 管理命令

## Files that must stay local

这些文件可以存在本地，但不应该提交：

- `config.yaml`
- `.env`
- `.autodl-helper-auth.json`
- `.autodl-helper-state.json`
- `.autodl-helper.lock`
- `data/`
- `logs/`
- `.cache/`

## Current structure notes

当前仓库已适合继续开源演进，但仍有两个现实点：

1. `autodl_helper/interactive_app.py` 体积偏大  
   后续适合按页面或功能拆分渲染逻辑。

2. 目前 lint 策略是保守接入  
   等代码结构更稳定后，再逐步扩大规则范围，例如未使用导入、导入排序、风格一致性等。
