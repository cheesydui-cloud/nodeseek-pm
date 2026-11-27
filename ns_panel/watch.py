"""在面板进程外面看 /health。面板容器挂了，由这里发 Telegram。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path


FAILS_NEEDED = 3
INTERVAL_SECONDS = 30


def read_watch_config(db_path: Path) -> tuple[str, str, bool]:
    if not db_path.is_file():
        return "", "", False
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("pragma query_only=on")
        rows = conn.execute("select key, value from settings where key in ('tg_token', 'tg_chat_id', 'enabled')").fetchall()
    except sqlite3.Error:
        return "", "", False
    finally:
        conn.close()
    values = {key: value for key, value in rows}
    token = str(values.get("tg_token") or "").strip()
    chat_id = str(values.get("tg_chat_id") or "").strip()
    enabled = str(values.get("enabled") or "") == "1"
    return token, chat_id, enabled


def probe(url: str, opener=None) -> tuple[bool, bool]:
    """返回 (面板进程在, 监控线程在)。连不上时两项都是假。"""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=5) as response:
            body = response.read(4000)
            status = getattr(response, "status", 200)
    except Exception:
        return False, False
    if status >= 400:
        return False, False
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, AttributeError):
        return False, False
    if not isinstance(data, dict) or data.get("ok") is not True:
        return False, False
    return True, bool(data.get("running"))


def watch_once(db_path: Path, url: str, state: dict, opener=None, sender=None, log=None, fails_needed: int = FAILS_NEEDED) -> dict:
    write = log or (lambda message: print(f"[watch] {message}", flush=True))
    token, chat_id, enabled = read_watch_config(db_path)
    if not token or not chat_id:
        return state
    up, running = probe(url, opener)
    alert = str(state.get("alert") or "")
    fails = int(state.get("fails") or 0)
    if not up:
        fails += 1
        state["fails"] = fails
        if fails >= fails_needed and alert != "down":
            text = "NodeSeek 监控面板没有响应。请到 1Panel 看这一套面板容器是否在跑。"
            if _send(sender, token, chat_id, text, write):
                state["alert"] = "down"
                write("已提醒：面板没有响应。")
        return state

    state["fails"] = 0
    if enabled and not running:
        thread_fails = int(state.get("thread_fails") or 0) + 1
        state["thread_fails"] = thread_fails
        if thread_fails >= fails_needed and alert != "thread":
            text = "NodeSeek 面板还在，但监控线程停了。请打开面板看最近的日志。"
            if _send(sender, token, chat_id, text, write):
                state["alert"] = "thread"
                write("已提醒：监控线程停了。")
        return state

    state["thread_fails"] = 0
    if alert:
        text = "NodeSeek 监控面板已恢复响应。"
        if _send(sender, token, chat_id, text, write):
            state["alert"] = ""
            write("已提醒：面板恢复响应。")
    return state


def _send(sender, token: str, chat_id: str, text: str, log) -> bool:
    send = sender
    if send is None:
        from ns_panel.telegram import send_message

        send = send_message
    try:
        send(token, chat_id, text)
    except Exception:
        log("提醒没有发出去。")
        return False
    return True


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"fails": 0, "thread_fails": 0, "alert": ""}
    if not isinstance(data, dict):
        return {"fails": 0, "thread_fails": 0, "alert": ""}
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    url = (os.environ.get("NS_WATCH_URL") or "http://127.0.0.1:8787/health").strip()
    data_dir = Path(os.environ.get("NS_DATA_DIR") or "data")
    try:
        interval = int(os.environ.get("NS_WATCH_INTERVAL") or INTERVAL_SECONDS)
    except ValueError:
        interval = INTERVAL_SECONDS
    interval = max(15, interval)
    try:
        fails_needed = int(os.environ.get("NS_WATCH_FAILS") or FAILS_NEEDED)
    except ValueError:
        fails_needed = FAILS_NEEDED
    fails_needed = max(1, fails_needed)
    db_path = data_dir / "panel.db"
    state_path = data_dir / "watch-state.json"
    print("面板看门狗已启动。", flush=True)
    while True:
        state = load_state(state_path)
        try:
            state = watch_once(db_path, url, state, fails_needed=fails_needed)
            save_state(state_path, state)
        except Exception:
            print("[watch] 这一轮检查没有完成。", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
