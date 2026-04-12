# Troubleshooting

## 1. `最近检查时间` does not move

Check whether the daemon is actually writing new scheduled history.

Commands:

```bash
python main.py service-status --config config.yaml
tail -n 20 logs/service.stdout.log
```

What to verify:

- daemon is running
- new `[抢机检查]` logs are still appearing
- new `[后台轮询]` logs are still appearing

If needed:

```bash
python main.py service-restart --config config.yaml
```

## 2. Interactive page only updates after pressing Enter

This usually means one of these:

- you are not running the latest code
- the daemon is not producing new data
- the page is showing a static state rather than a live state

First:

```bash
python main.py interactive --config config.yaml
```

Then confirm:

- the footer shows auto-refresh text where expected
- background logs continue moving

## 3. Background service shows abnormal status

Check:

```bash
python main.py service-status --config config.yaml
tail -n 50 logs/service.stderr.log
tail -n 50 logs/service.stdout.log
```

Common causes:

- old stderr still contains a previous crash
- LaunchAgent is installed but not currently loaded
- daemon heartbeat stopped moving
- config reload failed

Try:

```bash
python main.py service-restart --config config.yaml
```

## 4. Keeper plan looks wrong

Run:

```bash
python main.py keeper-probe --config config.yaml
python main.py history --config config.yaml --task keeper --limit 20
```

Check:

- release deadline
- next keeper time
- whether the instance is already in cooldown
- whether that release cycle has already executed keeper once

## 5. Keeper summary count looks too large

The project now groups keeper history by execution batch.

If old history was created before batch grouping existed, summary behavior may differ slightly for legacy rows. Check the raw history:

```bash
python main.py history --config config.yaml --task keeper --limit 50
```

## 6. Config changes do not take effect

Run:

```bash
python main.py validate-config --config config.yaml
python main.py config-resolve --config config.yaml
python main.py service-restart --config config.yaml
```

If config reload fails, the daemon keeps using the last valid config.

## 7. Login validation fails

Check:

```bash
python main.py login --config config.yaml --account main
python main.py auth-report --config config.yaml
```

Verify:

- token is still valid
- phone/password flow is still valid
- local cache is not stale

## 8. Database or runtime file errors

Symptoms:

- unable to open database file
- lock file stuck
- local runtime state inconsistent

Check:

```bash
python main.py db-check --config config.yaml
ls -la data logs .cache
```

If needed, stop the daemon before manual cleanup:

```bash
python main.py service-stop --config config.yaml
```

Then back up and inspect local files before deleting anything.

## 9. Before filing an issue

Please collect:

- exact command
- sanitized config snippet
- sanitized logs
- whether you used interactive or daemon mode
- OS and Python version
