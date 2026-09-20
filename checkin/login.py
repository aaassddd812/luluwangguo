#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""露露王国 · 协议登录获取 token(打卡模块用)

用法:
  python login.py send <手机号>            # 发送短信验证码
  python login.py do <手机号> <验证码>      # 登录, token 存到 token.json
"""
import json
import sys

import requests

from lulu_crypto import encrypt, decrypt

API = "https://api.lululu.com.cn"
HDR = {"token": "-1", "User-Agent": "BestHTTP", "Content-Type": "text/plain"}


def _post(path, payload):
    r = requests.post(API + path, data=encrypt(json.dumps(payload, separators=(",", ":"))),
                      headers=HDR, timeout=15)
    try:
        return json.loads(decrypt(r.text))
    except Exception:
        if "_guard" in r.text or r.status_code in (456, 503):
            return {"code": -1, "msg": "游戏防护拦截(稍后再试)"}
        return {"code": -1, "msg": f"HTTP {r.status_code}: {r.text[:60]}"}


def send_code(tel):
    if not (tel.isdigit() and len(tel) == 11):
        print("手机号格式不对"); return 1
    d = _post("/account/send_phone_code", {"tel": tel})
    print(json.dumps(d, ensure_ascii=False))
    return 0 if d.get("code") == 0 else 1


def do_login(tel, code):
    d = _post("/account/phone_login", {"tel": tel, "code": code, "invite_code": 0})
    if d.get("code") == 0 and d.get("data", {}).get("token"):
        tok, uid = d["data"]["token"], d["data"].get("user_id", 0)
        with open("token.json", "w", encoding="utf-8") as f:
            json.dump({"token": tok, "userid": uid}, f)
        print(f"登录成功 userid={uid}, token 已存 token.json")
        return 0
    print("登录失败:", json.dumps(d, ensure_ascii=False)[:200])
    return 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "send":
        sys.exit(send_code(sys.argv[2]))
    if len(sys.argv) >= 4 and sys.argv[1] == "do":
        sys.exit(do_login(sys.argv[2], sys.argv[3]))
    print(__doc__)
    sys.exit(1)
