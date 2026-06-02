# 开源发布检查清单

## 发布前必须检查

- [ ] 替换 `README.md` / `pyproject.toml` 中的私人仓库地址。
- [ ] 确认 `config.example.yaml` 不含真实账号数据。
- [ ] 确认 `.env.template` 只包含占位符。
- [ ] 从工作区移除运行时产物：
  - [ ] `data/`
  - [ ] `logs/`
  - [ ] `.cache/`
  - [ ] `.autodl-helper-auth.json`
  - [ ] `.autodl-helper-state.json`
  - [ ] `.autodl-helper.lock`
- [ ] 检查 git 历史中是否泄露令牌或手机号。

## 项目卫生

- [ ] `README.md` 说明项目用途和运行方式。
- [ ] `LICENSE` 存在。
- [ ] `CONTRIBUTING.md` 存在。
- [ ] `pyproject.toml` 中定义了包元数据。
- [ ] `requirements.txt` 与运行时依赖一致。
- [ ] 本地测试套件通过。

## 可选但推荐

- [ ] 为合并请求增加 CI 测试。
- [ ] 增加反馈模板。
- [ ] 发布第一个带标签的版本。
