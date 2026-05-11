# Docker

The Docker image is daemon-only. It does not expose or support a Web UI.

## Default command

The container default command is:

```bash
autodl-helper run daemon --config /app/config.yaml
```

The image runs the foreground daemon by default through `autodl-helper run daemon`.

## Build

```bash
docker build -t autodl-helper:local .
```

The image includes a sample `/app/config.yaml` copied from `config.example.yaml`. For real
use, bind-mount your own config instead of baking secrets into the image.

## Run with Docker

```bash
docker run --rm \
  -v "$PWD/config.yaml:/app/config.yaml:ro" \
  -v "$PWD/data:/app/data" \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/.cache:/app/.cache" \
  autodl-helper:local
```

## Run with Compose

```bash
docker compose up -d --build
```

`compose.yaml` mounts local `config.yaml`, `data/`, `logs/`, and `.cache/`. No ports are
published because the container does not provide a UI.
