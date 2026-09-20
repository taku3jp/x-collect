#!/usr/bin/env python3
"""Export X login session from your real Chrome to state.json.

Xにログイン済みのChromeからクッキーを取り出してPlaywright用の
state.jsonを作る。Xのログイン画面は通らないのでログイン制限を受けない。

事前条件: Chromeでx.comにログイン済みであること。
初回実行時にmacOSのキーチェーン許可ダイアログが出たら「許可」。
"""

import json
import time

import browser_cookie3

STATE_PATH = "state.json"

cj = browser_cookie3.chrome(domain_name="x.com")
cookies = []
for c in cj:
    cookies.append({
        "name": c.name,
        "value": c.value,
        "domain": c.domain,
        "path": c.path or "/",
        "expires": c.expires if c.expires else -1,
        "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
        "secure": bool(c.secure),
        "sameSite": "Lax",
    })

names = {c["name"] for c in cookies}
print(f"{len(cookies)} cookies: {sorted(names)}")

if "auth_token" not in names or "ct0" not in names:
    raise SystemExit("auth_token/ct0 が見つかりません。Chromeでx.comにログインしてから再実行してください。")

state = {
    "cookies": cookies,
    "origins": [{"origin": "https://x.com", "localStorage": []}],
}
with open(STATE_PATH, "w") as f:
    json.dump(state, f, indent=2)

print(f"saved: {STATE_PATH} ({time.strftime('%Y-%m-%d %H:%M:%S')})")
