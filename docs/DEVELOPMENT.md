# Development

本项目当前以 **本地 CLI / daemon 开发** 为主，推荐使用 Python 3.11 或 3.12。

## Local setup

```bash
python3 -m venv .venv
./.venv/bin/python --version
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/playwright install chromium
./.venv/bin/python main.py --help
```

Windows 把 `python3` 换成 `py`，把 `./.venv/bin/python` 换成 `.\.venv\Scripts\python`。需要验证接近日常用户的安装方式时再使用 `pipx install .`。

### Nuitka executable path

Nuitka 只用于生成平台本地 console executable；当前不做 DMG / MSI / GUI app / 代码签名。
请在目标平台本机执行对应脚本，不做跨平台编译。

macOS:

```bash
./.venv/bin/python -m pip install -r requirements-dev.txt
./scripts/build_nuitka_macos.sh
./dist/nuitka-macos/autodl-helper --help
```

Windows PowerShell:

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\scripts\build_nuitka_windows.ps1
.\dist\nuitka-windows\autodl-helper.exe --help
```

Playwright 浏览器缓存不会打包进 Nuitka 产物；需要浏览器登录流程时，在运行环境单独执行：

```bash
python -m playwright install chromium
```

## Common development commands

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m py_compile $(find autodl_helper -name '*.py')
./.venv/bin/python -m ruff check .
./.venv/bin/python -m importlinter
```

当前 lint 只接入一层**保守门禁**，主要用于发现：

- 明确的语法问题
- 未定义名称
- 一些会直接导致运行失败的静态错误

这一步是为了让开源仓库先具备稳定的基础门禁，而不是一次性推动大规模风格重构。

## Run locally

```bash
./.venv/bin/python main.py ui --config config.yaml
./.venv/bin/python main.py run daemon --config config.yaml
./.venv/bin/python main.py service install --config config.yaml
./.venv/bin/python main.py service start --config config.yaml
./.venv/bin/python main.py service status --config config.yaml
```

服务托管说明以 `docs/SERVICE.md` 为准。

## Suggested workflow

1. 先执行 `./.venv/bin/python main.py init` 生成本地 `.env` 和 `config.yaml`
2. 再按需修改 `.env` / `config.yaml`
3. 先跑：
   - `pytest -q`
   - `ruff check .`
   - `py_compile`
4. 再手动验证：
   - ui 页面
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

1. `autodl_helper/ui/app.py` 体积偏大  
   后续适合按页面或功能拆分渲染逻辑。

2. 目前 lint 策略是保守接入  
   等代码结构更稳定后，再逐步扩大规则范围，例如未使用导入、导入排序、风格一致性等。

3. 服务托管相关说明应以 `docs/SERVICE.md` 为准，开发时不要再把后台服务理解成单一 macOS 专属实现。
