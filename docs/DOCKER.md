# Docker 使用说明

Docker 镜像只运行守护进程，不提供网页界面，也不开放端口。

## 默认命令

容器默认命令是：

```bash
autodl-helper run daemon --config /app/config.yaml
```

也就是说，镜像默认以前台守护进程方式运行。

## 构建镜像

```bash
docker build -t autodl-helper:local .
```

镜像内会包含一份从 `config.example.yaml` 复制的 `/app/config.yaml` 示例。真实使用时应挂载本地配置，不要把密钥或账号信息写进镜像。

## 使用 Docker 运行

```bash
docker run --rm \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -v "$PWD/data:/app/data" \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/.cache:/app/.cache" \
  autodl-helper:local
```

## 使用 Docker Compose 运行

```bash
docker compose up -d --build
```

`compose.yaml` 会挂载本地 `config.yaml`、`data/`、`logs/` 和 `.cache/`。容器不提供 UI，因此不会发布端口。
