#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""露露王国 · 每日打卡助手(独立运行)

玩法规则(游戏官方):
  投入: 每日 9:30~24:00 预约投入宝石(>=100 参与幸运星, 投入越早概率越高)
  打卡: 次日 8:00~9:00; 打卡成功瓜分奖池, 失败则投入 100% 进奖池
  结算: 每日 9:30; 奖池 95% 按投入占比瓜分给打卡成功者, 5% 给 1 名幸运星

用法(先跑 login.py 拿 token):
  python checkin.py status          # 查看当前阶段/总池/我的投入
  python checkin.py join <宝石数>   # 手动预约投入(最低1, 真实扣宝石)
  python checkin.py auto            # 常驻: 打卡期自动打卡(失败每20秒重试)
"""
import json
import os
import sys
import time

import requests

from lulu_crypto import encrypt, decrypt

API = "https://api.lululu.com.cn"
STAGE = {3: "预约期", 1: "打卡期", 2: "结算中"}


def load_token():
    try:
        d = json.load(open("token.json", encoding="utf-8"))
        if d.get("token"):
            return d["token"]
    except FileNotFoundError:
        pass
    print("没有 token: 请先 python login.py send <手机号> / do <手机号> <验证码>")
    sys.exit(1)


def api(path, token, payload=None):
    h = {"token": token, "User-Agent": "BestHTTP"}
    if payload is not None:
        r = requests.post(API + path, data=encrypt(json.dumps(payload, separators=(",", ":"))),
                          headers={**h, "Content-Type": "text/plain"}, timeout=15)
    else:
        r = requests.get(API + path, headers=h, timeout=15)
    try:
        return json.loads(decrypt(r.text))
    except Exception:
        return {"code": -1, "msg": f"HTTP {r.status_code}: {r.text[:60]}"}


def get_data(token):
    d = api("/daily-checkin/data", token)
    if d.get("code") != 0:
        return None
    return d.get("data") or {}


def show(info):
    st = STAGE.get(info.get("stage"), "等待")
    print(f"[{time.strftime('%H:%M:%S')}] 阶段: {st} | 总池 {info.get('invest_amount', 0)} 宝石 | "
          f"{info.get('participant_count', 0)} 人 | 我的投入 {info.get('my_invest_amount', 0)}"
          + (" | ✅已打卡" if info.get("is_punched_current_period") == 1 else ""))


def cmd_status():
    tok = load_token()
    info = get_data(tok)
    if info is None:
        print("查询失败(token 可能失效, 重新登录)"); return 1
    show(info)
    return 0


def cmd_join(amt):
    if amt < 1:
        print("投入宝石数最低 1"); return 1
    tok = load_token()
    if input(f"确认投入 {amt} 宝石预约打卡? (未打卡将被瓜分) [y/N] ").strip().lower() != "y":
        print("已取消"); return 0
    r = api("/daily-checkin/join", tok, {"invest_amount": amt})
    if r.get("code") == 0:
        print(f"投入成功: {amt} 宝石, 期号 {(r.get('data') or {}).get('period_date', '')}")
        return 0
    print("投入失败:", r.get("msg", r)); return 1


def cmd_auto():
    tok = load_token()
    print("自动打卡已启动(只自动打卡, 不自动投入)。Ctrl+C 退出。")
    while True:
        try:
            info = get_data(tok)
            if info is None:
                print("查询失败, 60秒后重试"); time.sleep(60); continue
            stage = info.get("stage")
            if stage == 1:
                if info.get("is_punched_current_period") == 1:
                    show(info); print("今日已打卡, 等待 9:30 结算"); time.sleep(300); continue
                if float(info.get("my_invest_amount") or 0) <= 0:
                    print("⚠️ 打卡期发现未预约(无投入), 自动打卡退出。请在投入期(9:30~24:00)先投入。")
                    return 1
                r = api("/daily-checkin/punch", tok, {})
                if r.get("code") == 0:
                    print("🎉 打卡成功! 等待结算瓜分"); time.sleep(300); continue
                print(f"打卡失败: {r.get('msg', '?')}, 20秒后重试"); time.sleep(20); continue
            show(info)
            time.sleep(60)
        except KeyboardInterrupt:
            print("已退出"); return 0
        except Exception as e:
            print("异常:", str(e)[:80], "60秒后重试"); time.sleep(60)


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        if sys.argv[1] == "status":
            sys.exit(cmd_status())
        if sys.argv[1] == "join" and len(sys.argv) >= 3:
            sys.exit(cmd_join(int(sys.argv[2])))
        if sys.argv[1] == "auto":
            sys.exit(cmd_auto())
    print(__doc__)
    sys.exit(1)
