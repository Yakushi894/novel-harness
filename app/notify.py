"""Synology Chat（Incoming Webhook）への通知。未設定なら何もしない。"""
import os

import httpx

ENV_WEBHOOK = os.environ.get("CHAT_WEBHOOK_URL", "")


def webhook_url() -> str:
    try:
        import db
        return db.get_setting("chat_webhook_url", "") or ENV_WEBHOOK
    except Exception:
        return ENV_WEBHOOK


def notify(text: str) -> None:
    webhook = webhook_url()
    if not webhook:
        print(f"[notify:disabled] {text}", flush=True)
        return
    try:
        httpx.post(webhook, data={"payload": '{"text": ' + _json_str(text) + "}"},
                   timeout=15)
        print(f"[notify] {text}", flush=True)
    except Exception as e:  # 通知失敗で工程を止めない
        print(f"[notify:error] {e} / {text}", flush=True)


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)
