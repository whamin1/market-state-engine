# GCP Telegram Report Schedule

## Option 1: Run A Long-Lived Report Loop

```bash
python -m examples.run_report_loop --send-times 09:10,21:10
```

This uses KST times and sends a Telegram report once at each listed time.

## Option 2: Use Cron

KST is UTC+9, so:

- 09:10 KST = 00:10 UTC
- 21:10 KST = 12:10 UTC

Example cron:

```cron
10 0,12 * * * cd /path/to/project && python -m examples.send_status_report --telegram
```

Use Option 1 if you want a simple always-running process. Use Option 2 if the server already uses cron.
