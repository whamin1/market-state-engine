import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def send_prediction_alert(message, env_path=None, token=None, chat_id=None):
    """Send research-only alerts through the Telegram5 bot."""
    _load_environment(Path(env_path) if env_path else DEFAULT_ENV_PATH)
    token = token or os.getenv("TELEGRAM5_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("prediction telegram send failed: missing TELEGRAM5_TOKEN or TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        status_code, response_text = _post_message(url, payload)
    except OSError as exc:
        print(f"prediction telegram send error: {exc}")
        return False

    if status_code != 200:
        print(f"prediction telegram send failed: {response_text}")
        return False
    return True


def _load_environment(env_path):
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_simple_env_file(env_path)
        return

    load_dotenv(env_path)


def _load_simple_env_file(env_path):
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def _post_message(url, payload):
    try:
        import requests
    except ImportError:
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except URLError as exc:
            raise OSError(str(exc)) from exc

    try:
        response = requests.post(url, json=payload, timeout=10)
    except requests.RequestException as exc:
        raise OSError(str(exc)) from exc
    return response.status_code, response.text
