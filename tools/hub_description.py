"""Docker Hub のリポジトリ説明を README（日英）で更新する。

vDSM 上の docker ログイン情報（/root/.docker/config.json）をその場で使い、
認証情報そのものは出力しない。
"""
import base64
import json
import sys

import httpx

CONFIG = "/cfg/config.json"
REPO = "yuichi894/novel-harness"
SHORT = "Local novel-generation controller for Synology NAS + XeStrata. Fully on-prem."


def main() -> int:
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    auths = cfg.get("auths", {})
    entry = next(iter(auths.values()), {})
    raw = base64.b64decode(entry["auth"]).decode()
    username, _, password = raw.partition(":")

    r = httpx.post("https://hub.docker.com/v2/users/login/",
                   json={"username": username, "password": password}, timeout=30)
    print("login:", r.status_code)
    if r.status_code != 200:
        return 1
    jwt = r.json()["token"]

    with open("/docs/README.md", encoding="utf-8") as f:
        jp = f.read()
    with open("/docs/README.en.md", encoding="utf-8") as f:
        en = f.read()
    full = jp.rstrip() + "\n\n---\n\n" + en
    print("full_description chars:", len(full))

    r = httpx.patch(f"https://hub.docker.com/v2/repositories/{REPO}/",
                    headers={"Authorization": f"JWT {jwt}"},
                    json={"description": SHORT, "full_description": full},
                    timeout=30)
    print("patch:", r.status_code)
    if r.status_code != 200:
        print(r.text[:300])
        return 1
    j = r.json()
    print("repo:", j.get("namespace"), "/", j.get("name"), "| is_private:", j.get("is_private"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
