# 隐私与本地数据

`autodl-helper` 设计为本地运行工具。项目不包含托管后端，也没有内置远程遥测通道。

## 本地会保存什么

默认情况下，项目会在以下位置保存本地运行数据：

- `config.yaml`
  - 本地项目配置。
- `data/autodl-helper.db`
  - SQLite 历史、运行时控制状态、Keeper 历史、抢机历史和事件日志。
- `.cache/*.json`
  - 授权缓存文件。
- `logs/`
  - 守护进程标准输出、标准错误和相关运行日志。
- `.autodl-helper-auth.json`
  - 本地授权相关状态。
- `.autodl-helper-state.json`
  - 本地运行状态文件。
- `.autodl-helper.lock`
  - 本地锁文件。

## 可能包含的敏感信息

取决于你的配置，本地文件可能包含：

- AutoDL 授权令牌。
- AutoDL 手机号。
- 实例 ID。
- 机器偏好。
- 运行历史和时间戳。

以下内容应视为敏感：

- `.env`
- `config.yaml`
- `data/`
- `logs/`
- `.cache/`
- `.autodl-helper-*.json`

## 不应提交的内容

不要提交：

- `.env`
- `config.yaml`
- `data/`
- `logs/`
- `.cache/`
- `.autodl-helper-auth.json`
- `.autodl-helper-state.json`
- `.autodl-helper.lock`

公开示例只使用：

- `config.example.yaml`
- `.env.template`

## 截图和反馈

发布截图、反馈或日志前，请先：

- 移除令牌。
- 移除手机号。
- 按需移除真实实例 ID。
- 检查终端输出中的账户标识。

## 网络行为

项目会按功能需要访问 AutoDL 相关接口。当前项目没有单独的分析统计服务。

如果你的运行环境有更严格要求，请在使用前直接审查源码和网络访问路径。

## 公开派生仓库前建议

公开推送历史前：

1. 检查 git 历史中是否泄露令牌或手机号。
2. 检查 README 和文档示例中是否包含私人账户数据。
3. 如果怀疑凭据曾被提交，立即轮换相关凭据。
