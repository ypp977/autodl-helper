# 开发指南

本项目当前以本地 CLI 和守护进程开发为主，推荐使用 Python 3.11 或 3.12。

## 本地环境

```bash
python3 -m venv .venv
./.venv/bin/python --version
./.venv/bin/python -m pip install -r requirements-dev.txt
./.venv/bin/playwright install chromium
./.venv/bin/python main.py --help
```

Windows 下把 `python3` 换成 `py`，把 `./.venv/bin/python` 换成 `.\.venv\Scripts\python`。需要验证接近日常用户的安装方式时，再使用 `pipx install .`。

## Nuitka 可执行文件路径

Nuitka 只用于生成平台本地控制台可执行文件；当前不做 DMG、MSI、图形界面应用或代码签名。

macOS：

```bash
./.venv/bin/python -m pip install -r requirements-dev.txt
./scripts/build_nuitka_macos.sh
./dist/nuitka-macos/autodl-helper --help
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\scripts\build_nuitka_windows.ps1
.\dist\nuitka-windows\autodl-helper.exe --help
```

Playwright 浏览器缓存不会打包进 Nuitka 产物。需要浏览器登录流程时，在运行环境单独执行：

```bash
python -m playwright install chromium
```

## 常用开发命令

```bash
./.venv/bin/python -m pytest -q
./.venv/bin/python -m py_compile $(find autodl_helper -name '*.py')
./.venv/bin/python -m ruff check .
./.venv/bin/python -m importlinter
```

当前静态检查只接入保守门禁，主要用于发现：

- 明确的语法/导入问题。
- 架构边界问题。
- 低风险的静态错误。

## 本地运行

```bash
./.venv/bin/python main.py ui --config config.yaml
./.venv/bin/python main.py run daemon --config config.yaml
./.venv/bin/python main.py service install --config config.yaml
./.venv/bin/python main.py service start --config config.yaml
./.venv/bin/python main.py service status --config config.yaml
```

服务托管说明以 `docs/SERVICE.md` 为准。

## 建议流程

1. 先执行 `./.venv/bin/python main.py init` 生成本地 `.env` 和 `config.yaml`。
2. 再按需修改 `.env` 或 `config.yaml`。
3. 改代码后至少运行相关聚焦测试；提交前按风险运行：
   - `pytest -q`
   - `ruff check .`
   - `py_compile`
4. 涉及用户可见行为时，手动检查：
   - UI 页面
   - Keeper / 抢机
   - 守护进程或服务管理命令

## 必须留在本地的文件

不要提交：

- `config.yaml`
- `.env`
- `.autodl-helper-auth.json`
- `.autodl-helper-state.json`
- `.autodl-helper.lock`
- `data/`
- `logs/`
- `.cache/`

## 当前结构说明

1. UI 动作代码按职责拆分。

   守护进程、Keeper、账号菜单分别在 `autodl_helper/ui/*_menu.py`，`action_menus.py` 只保留兼容导出。
   后台任务完成后的输入等待与页面重绘由 `autodl_helper/ui/background_input.py` 支撑，不要在单个菜单里重复实现输入线程。

2. 任务结果标签应放在任务模块。

   Keeper 和抢机的结果/原因标签在 `autodl_helper/tasks/*_results.py`，CLI/UI 只做展示组合。

3. 静态检查策略是保守接入。

   等代码结构更稳定后，再逐步扩大规则范围，例如未使用导入、导入排序、风格一致性等。

4. 服务托管说明以 `docs/SERVICE.md` 为准。

   开发时不要再把后台服务理解成单一 macOS 专属实现。
