# -*- coding: utf-8 -*-
"""露露娱乐多用户服务端
- 用户系统: 注册/登录 (SQLite, PBKDF2 密码哈希)
- 全局采集: 逃杀/斗鸡/赛马 三条 WS (所有用户共享实时数据)
- 每用户独立: token / 下注配置 / 下注引擎 / 盈亏统计
运行: python3 server_app.py  (依赖 waitress)
"""
import os
import json
import time
import math
import socket
import random
import sqlite3
import hashlib
import logging
import datetime
import threading
from collections import defaultdict

import requests
import websocket
from flask import Flask, jsonify, request, session, send_file, redirect

from lulu_crypto import encrypt, decrypt

FEE = 0.97
ITEM_ID = 102201
# 游戏时间 = UTC+8（北京时间）; 所有显示/日界/禁注窗口都按游戏时间
from datetime import timezone, timedelta
GAME_TZ = timezone(timedelta(hours=8))
HELL_START_MIN = 19 * 60 + 55  # 19:55 起停（20-21点地狱模式, 提前5分钟余量）
HELL_END_MIN = 21 * 60 + 5    # 21:05 恢复下注


def game_now():
    return datetime.datetime.now(GAME_TZ)


def game_hm(ts=None, fmt="%H:%M:%S"):
    d = datetime.datetime.fromtimestamp(ts, GAME_TZ) if ts else game_now()
    return d.strftime(fmt)


def in_hell(now=None):
    n = now or game_now()
    m = n.hour * 60 + n.minute
    return HELL_START_MIN <= m < HELL_END_MIN
API = "https://api.lululu.com.cn"
DATA = "data"
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, "lulu.db")
LOG_DIR = DATA

GAMES = {
    "steal": {"name": "逃杀", "host": "xdy.lululu.com.cn", "url": "wss://xdy.lululu.com.cn/ws",
              "slots": 8, "init": ["2001", "2007"], "refresh": ["2001"], "bet_status": 2},
    "cock": {"name": "斗鸡", "host": "lh.lululu.com.cn", "url": "wss://lh.lululu.com.cn/ws",
             "slots": 2, "init": ["2001", "2013"], "refresh": ["2001"], "bet_status": 0},
    "fox": {"name": "赛马", "host": "race.lululu.com.cn", "url": "wss://race.lululu.com.cn/ws",
            "slots": 6, "init": ["2001", "2011", "2012"], "refresh": ["2001", "2011"], "bet_status": 0},
}
STEAL = GAMES["steal"]
COCK_ODDS = 1.95  # 斗鸡固定赔率(客户端口径, 实测)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("server")

# ================= 数据库 =================
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL, salt TEXT NOT NULL,
            lulu_token TEXT DEFAULT '', lulu_userid INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        _migrate_bot_state(c)
        c.execute("""CREATE TABLE IF NOT EXISTS bot_state(
            user_id INTEGER NOT NULL, game TEXT NOT NULL DEFAULT 'steal',
            strategy TEXT DEFAULT 'smartev', amount REAL DEFAULT 0.1,
            max_loss REAL DEFAULT 10.0, enabled INTEGER DEFAULT 0,
            pnl REAL DEFAULT 0, stop_reason TEXT DEFAULT '',
            bet_lead REAL DEFAULT 3.0, take_profit REAL DEFAULT 5.0,
            fox_bet_type INTEGER DEFAULT 1,
            PRIMARY KEY(user_id, game))""")
        c.execute("""CREATE TABLE IF NOT EXISTS official_daily(
            user_id INTEGER, day TEXT, net REAL, consume REAL, gain REAL,
            UNIQUE(user_id, day))""")
        c.execute("""CREATE TABLE IF NOT EXISTS bet_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            game TEXT DEFAULT 'steal', ts TEXT, day TEXT DEFAULT '',
            round_id INTEGER, room INTEGER, amount REAL,
            status TEXT, pnl REAL DEFAULT 0)""")
        # 增量补列(老库兼容)
        for tbl, col, ddl in [("bot_state", "fox_bet_type", "INTEGER DEFAULT 1"),
                              ("bot_state", "bet_lead", "REAL DEFAULT 3.0"),
                              ("bot_state", "take_profit", "REAL DEFAULT 5.0"),
                              ("bet_log", "game", "TEXT DEFAULT 'steal'"),
                              ("bet_log", "day", "TEXT DEFAULT ''")]:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({tbl})").fetchall()]
            if cols and col not in cols:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {ddl}")


def _migrate_bot_state(c):
    """老版 bot_state 是 user_id 单列主键(仅逃杀) -> 迁成 (user_id, game) 复合主键"""
    cols = [r[1] for r in c.execute("PRAGMA table_info(bot_state)").fetchall()]
    if not cols or "game" in cols:
        return
    c.execute("ALTER TABLE bot_state RENAME TO bot_state_old")
    c.execute("""CREATE TABLE bot_state(
        user_id INTEGER NOT NULL, game TEXT NOT NULL DEFAULT 'steal',
        strategy TEXT DEFAULT 'smartev', amount REAL DEFAULT 0.1,
        max_loss REAL DEFAULT 10.0, enabled INTEGER DEFAULT 0,
        pnl REAL DEFAULT 0, stop_reason TEXT DEFAULT '',
        bet_lead REAL DEFAULT 3.0, take_profit REAL DEFAULT 5.0,
        fox_bet_type INTEGER DEFAULT 1,
        harvest_enabled INTEGER DEFAULT 0, last_harvest_ts TEXT DEFAULT '',
        last_harvest_amt REAL DEFAULT 0, last_harvest_epoch REAL DEFAULT 0,
        harvest_period REAL DEFAULT 0, harvest_target REAL DEFAULT 0,
        harvest_next REAL DEFAULT 0, harvest_seen_qty REAL DEFAULT 0,
        PRIMARY KEY(user_id, game))""")
    common = [x for x in cols if x != "id"]
    c.execute(f"INSERT OR IGNORE INTO bot_state({','.join(common)}) "
              f"SELECT {','.join(common)} FROM bot_state_old")
    c.execute("DROP TABLE bot_state_old")
    log.info("bot_state 已迁移为 (user_id, game) 复合主键")


def hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 120000).hex()


def get_user(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def get_bot(uid, game="steal"):
    with db() as c:
        r = c.execute("SELECT * FROM bot_state WHERE user_id=? AND game=?", (uid, game)).fetchone()
        if not r:
            c.execute("INSERT OR IGNORE INTO bot_state(user_id, game) VALUES(?,?)", (uid, game))
            r = c.execute("SELECT * FROM bot_state WHERE user_id=? AND game=?", (uid, game)).fetchone()
        return r


def set_bot(uid, game="steal", **kw):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO bot_state(user_id, game) VALUES(?,?)", (uid, game))
        for k, v in kw.items():
            c.execute(f"UPDATE bot_state SET {k}=? WHERE user_id=? AND game=?", (v, uid, game))


def add_bet(uid, game, round_id, room, amount, status, pnl):
    today = game_now().date().isoformat()
    with db() as c:
        c.execute("INSERT INTO bet_log(user_id,game,ts,day,round_id,room,amount,status,pnl) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (uid, game, game_hm(), today, round_id, room, amount, status, pnl))
        c.execute("DELETE FROM bet_log WHERE id < (SELECT MIN(id) FROM "
                  "(SELECT id FROM bet_log WHERE user_id=? AND game=? ORDER BY id DESC LIMIT 300) t "
                  "WHERE user_id=? AND game=?)", (uid, game, uid, game))


def bet_log_today(uid, game):
    """某游戏今日自算盈亏(斗鸡/赛马的止盈止损口径)"""
    today = game_now().date().isoformat()
    with db() as c:
        return c.execute("SELECT COALESCE(SUM(pnl),0) FROM bet_log "
                         "WHERE user_id=? AND game=? AND day=?", (uid, game, today)).fetchone()[0]


def ensure_bet_log_day():
    """老库兼容: 补 day 列, 历史行按今天算"""
    with db() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(bet_log)").fetchall()]
        if "day" not in cols:
            c.execute("ALTER TABLE bet_log ADD COLUMN day TEXT DEFAULT ''")
            c.execute("UPDATE bet_log SET day=?", (game_now().date().isoformat(),))

def write_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ================= DNS 锚定 =================
_dns_anchored = set()


def anchor_dns(host):
    if host in _dns_anchored:
        return
    try:
        r = requests.get(f"https://dns.alidns.com/resolve?name={host}&type=A",
                         timeout=8, headers={"accept": "application/dns-json"})
        ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
    except Exception:
        ips = []
    if not ips:
        log.warning("DoH 解析 %s 失败", host)
        return
    orig = socket.getaddrinfo

    def patched(h, *a, **k):
        if h == host:
            return orig(ips[0], *a, **k)
        return orig(h, *a, **k)

    socket.getaddrinfo = patched
    _dns_anchored.add(host)
    log.info("DNS 锚定 %s -> %s", host, ips[0])


# ================= 全局采集器 =================

def base_picks(pools, players, round_id, seed_key):
    rooms = sorted(pools.keys())
    rnd = random.Random(seed_key)
    return {
        "roundId": round_id,
        "minpool": min(rooms, key=lambda r: pools[r]),
        "maxpool": max(rooms, key=lambda r: pools[r]),
        "random": rnd.choice(rooms),
        "room1": 1 if 1 in pools else rooms[0],
        "maxplayers": max(rooms, key=lambda r: players.get(r, 0)) if players
                      else max(rooms, key=lambda r: pools[r]),
    }


def fox_win_rates(win_hist, champ, slots=6):
    """赛马各狐夺冠概率估计: 2012冠军计数(长样本) + 近20回合冠军频率加权"""
    cnt = {i: 0.0 for i in range(1, slots + 1)}
    total = 0.0
    for item_id, c in (champ or {}).items():
        try:
            cnt[int(item_id)] += float(c)
            total += float(c)
        except (KeyError, ValueError):
            continue
    for w in (win_hist or [])[-20:]:
        try:
            cnt[int(w)] += 3.0  # 近期权重: 每次冠军抵3张历史票
            total += 3.0
        except (KeyError, ValueError):
            continue
    if total <= 0:
        return {}
    return {i: c / total for i, c in cnt.items()}


def compute_picks(game, pools, players, round_id, kill_counts=None, kill_window=0,
                  win_hist=None, champ=None):
    """统一策略决策: 同一份池子+确定性随机 -> 实盘引擎与模拟盘选房完全一致
    smartev 各游戏口径: 逃杀=结构EV-近期被杀惩罚; 斗鸡=胜率估计×1.95赔率;
    赛马冠军盘=历史夺冠率×池子隐含赔率(价值狐)"""
    seed = f"round-{round_id}" if game == "steal" else f"{game}-round-{round_id}"
    picks = base_picks(pools, players, round_id, seed)
    rooms = sorted(pools.keys())
    if game == "steal":
        r_ev = {r: room_ev(pools, r) for r in rooms}
        if kill_counts and kill_window > 0:
            # 被杀率惩罚: 近期被杀占比越高扣分越多 (λ=0.6, 相当于期望扣 60%×频率)
            score = {r: r_ev[r] - 0.6 * (kill_counts.get(r, 0) / kill_window) for r in rooms}
        else:
            score = r_ev
        picks["smartev"] = max(rooms, key=lambda r: score[r])
    elif game == "cock":
        # 胜率估计 = 池占比与近20回合实际胜率各半
        total = sum(pools.values()) or 1.0
        recent = (win_hist or [])[-20:]
        wr = {r: 0.0 for r in rooms}
        for w in recent:
            try:
                wr[int(w)] += 1.0
            except KeyError:
                pass
        n = len(recent) or 1
        score = {r: (0.5 * pools[r] / total + 0.5 * wr[r] / n) * COCK_ODDS - 1 for r in rooms}
        picks["smartev"] = max(rooms, key=lambda r: score[r])
    else:  # fox 冠军盘
        P = sum(pools.values())
        p = fox_win_rates(win_hist, champ, slots=len(rooms)) or {r: 1.0 / len(rooms) for r in rooms}
        score = {r: p.get(r, 0) * FEE * P / max(pools[r], 0.1) for r in rooms}
        picks["smartev"] = max(rooms, key=lambda r: score[r])
    return picks


def compute_picks_nc(pools_nc, players, round_id, win_hist=None, champ=None):
    """赛马非冠军盘(押某狐拿不到冠军): smartev=最可能落败且非冠军池中相对冷门的狐"""
    rooms = sorted(pools_nc.keys())
    if not rooms:
        return None
    picks = base_picks(pools_nc, players, round_id, f"fox-nc-round-{round_id}")
    p = fox_win_rates(win_hist, champ, slots=6) or {r: 1.0 / 6 for r in range(1, 7)}
    P = sum(pools_nc.values())
    score = {r: (1 - p.get(r, 1.0 / 6)) * FEE * P / max(pools_nc[r], 0.1) for r in rooms}
    picks["smartev"] = max(rooms, key=lambda r: score[r])
    return picks


def sim_settle(game, pools, room, bet, killed=None, winner=None):
    """虚拟下注结算(模拟盘/follow元策略/采集器滚动收益用; 各游戏近似公式,
    实盘盈亏一律以官方 2008 allocation_amount 为准)"""
    if game == "steal":
        killed = killed or set()
        if room in killed:
            return -bet
        total = sum(pools.values())
        dead = sum(v for k, v in pools.items() if k in killed)
        alive = total - dead
        return bet + FEE * dead * bet / (alive + bet) - bet if dead > 0 else 0.0
    if game == "cock":
        return bet * (COCK_ODDS - 1) if room == winner else -bet
    # fox 冠军盘
    if room == winner:
        P = sum(pools.values())
        return bet * (FEE * P / max(pools.get(room, 0.0) + bet, 0.1) - 1) if P > 0 else 0.0
    return -bet


def sim_settle_nc(pools_nc, room, bet, winner):
    """赛马非冠军盘虚拟结算: 目标狐没夺冠即赢"""
    if room != winner:
        P = sum(pools_nc.values())
        return bet * (FEE * P / max(pools_nc.get(room, 0.0) + bet, 0.1) - 1) if P > 0 else 0.0
    return -bet


class Collector:
    """通用采集: live_state_<game>.json + rounds_<game>.jsonl
    各游戏在 T-5s 做统一决策(picks), 实盘引擎与模拟盘共用
    赛马额外维护非冠军盘池子(pools_nc)与 picks_nc, 以及 2012 冠军统计"""

    SNAPSHOT_LEAD = 5.0  # T-5s 快照+统一决策(下注末期池子已基本定型)

    def __init__(self, key):
        self.key = key
        self.cfg = GAMES[key]
        self.log = logging.getLogger("collect-" + key)
        self.ws = None
        self.round_id = None
        self.status = None
        self.pools = {}
        self.pools_nc = {}   # 赛马非冠军盘(bet_type=2)池子
        self.players = {}
        self.end_ms = 0
        self.winner = None
        self.rank = None
        self.last_killed = []
        self.events = []
        self.snapshot = None
        self.snapshot_nc = None
        self.snap_taken = False
        self.picks = None
        self.picks_nc = None
        self.champ = {}      # 赛马 2012 历史冠军计数
        self.saved_rounds = set()
        self._settling = False
        self._handshake_fails = 0
        self._tok_idx = 0
        self.kill_hist = []  # 逃杀: 最近50回合被杀计数(smartev多因子用)
        self.win_hist = []   # 斗鸡/赛马: 最近20回合冠军
        self.strat_hist = {k: [] for k in
                           ("smartev", "minpool", "maxpool", "random", "room1", "maxplayers")}
        self._load_strat_hist()

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="collect-" + self.key).start()

    def _load_strat_hist(self):
        """预热各基础策略近30盘盈亏(follow 元策略用), 按各游戏近似公式结算"""
        try:
            lines = open(os.path.join(DATA, f"rounds_{self.key}.jsonl"), encoding="utf-8").readlines()[-60:]
        except FileNotFoundError:
            return
        for line in lines:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            picks = r.get("picks")
            if not picks or not r.get("pools"):
                continue
            pools = {int(k): v for k, v in r["pools"].items() if v is not None}
            if not pools:
                continue
            killed = set(r.get("killed") or [])
            winner = r.get("winner")
            if self.key != "steal" and winner is None:
                continue
            for sk, room in picks.items():
                if sk == "roundId" or sk not in self.strat_hist:
                    continue
                if room not in pools:
                    continue
                self.strat_hist[sk].append(sim_settle(self.key, pools, room, 5.0,
                                                      killed=killed, winner=winner))
            for sk in self.strat_hist:
                del self.strat_hist[sk][:-30]
        # 逃杀额外预热被杀计数
        if self.key == "steal":
            for line in lines:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for k in r.get("killed", []):
                    self.kill_hist.append(k)
            del self.kill_hist[:-50]
        else:
            for line in lines:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("winner") is not None:
                    self.win_hist.append(r["winner"])
            del self.win_hist[:-20]

    # ---- 连接循环 ----
    def _run(self):
        backoff = 5
        ok = 0
        while True:
            anchor_dns(self.cfg["host"])
            tok, uid = self._pick_token()
            if not tok:
                self.log.warning("暂无可用 token, 30s 后重试")
                time.sleep(30)
                continue
            url = f"{self.cfg['url']}?token={tok}&userid={uid}"
            self.ws = websocket.WebSocketApp(
                url, on_open=self._on_open, on_message=self._on_message,
                on_error=lambda ws, e: self.log.error("WS: %s", str(e)[:100]),
                on_close=lambda ws, c, r: None,
                header={"User-Agent": "BestHTTP"})
            ok = time.time()
            self.ws.run_forever(sslopt={"check_hostname": False}, ping_interval=20)
            if time.time() - ok < 30:
                # 握手即失败(502/500): 连续3次换CDN节点
                self._handshake_fails += 1
                if self._handshake_fails % 3 == 0:
                    _dns_anchored.discard(self.cfg["host"])
                    anchor_dns(self.cfg["host"])
                    self.log.info("连续失败%d次, 已切换CDN节点", self._handshake_fails)
            else:
                self._handshake_fails = 0
            if time.time() - ok > 600:
                backoff = 5
            self.log.warning("断线 %ds 后重连", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    def _pick_token(self):
        """专用采集账号优先; 不存在时回退用户 token 池"""
        try:
            d = json.load(open(os.path.join(DATA, "collector.json"), encoding="utf-8"))
            if d.get("token") and d.get("userid"):
                return d["token"], int(d["userid"])
        except Exception:
            pass
        try:
            with db() as c:
                rows = c.execute("SELECT lulu_token, lulu_userid FROM users "
                                 "WHERE lulu_token != '' AND lulu_userid > 0 ORDER BY id").fetchall()
            if not rows:
                return "", 0
            r = rows[self._tok_idx % len(rows)]
            self._tok_idx += 1
            return r[0], r[1]
        except Exception:
            return "", 0

    def _send(self, evt, d=None):
        try:
            self.ws.send(encrypt(json.dumps({"e": evt, "d": d}, separators=(",", ":"))))
        except Exception:
            pass

    def _on_open(self, ws):
        self.log.info("已连接")
        for e in self.cfg["init"]:
            self._send(e)
        self.push("sys", f"{self.cfg['name']} 采集已连接")

    def push(self, t, text):
        self.events.insert(0, {"ts": int(time.time()), "type": t, "text": text})
        del self.events[40:]

    def _on_message(self, ws, message):
        for part in message.split("&"):
            part = part.strip()
            if not part:
                continue
            try:
                pt = decrypt(part)
            except Exception:
                continue
            if not pt.startswith("{"):
                pt = "{" + pt
            if not pt.endswith("}"):
                pt += "}"
            try:
                obj = json.loads(pt)
            except json.JSONDecodeError:
                continue
            try:
                self.dispatch(obj)
            except Exception:
                self.log.exception("dispatch")
        self.tick()
        self.dump()

    def dispatch(self, obj):
        evt, d = obj.get("e"), obj.get("d") or {}
        if self.key == "steal":
            self._d_steal(evt, d)
        elif self.key == "cock":
            self._d_cock(evt, d)
        else:
            self._d_fox(evt, d)

    # ---- 逃杀 ----
    def _d_steal(self, evt, d):
        if evt == "2001":
            r = d.get("result") or {}
            self.round_id = r.get("roundId")
            self.status = r.get("status")
            self.end_ms = r.get("countdownEndTime") or 0
            self.pools = {rm["roomId"]: rm.get("totalBet", 0) for rm in (r.get("rooms") or [])}
            self.players = {rm["roomId"]: rm.get("playerCount", 0) for rm in (r.get("rooms") or [])}
            self.snap_taken = False
        elif evt == "3001":
            if d.get("status") == 2 and self.status != 2:
                self.snap_taken = False
                self.picks = None
                self.push("round", f"回合 {self.round_id} 开始下注")
            self.status = d.get("status")
            if d.get("countdownEndTime"):
                self.end_ms = d["countdownEndTime"]
        elif evt == "3006":
            self.pools[d["roomId"]] = d.get("totalAmount", 0)
            self.players[d["roomId"]] = d.get("playerCount", 0)
        elif evt == "3004":
            self.last_killed = d.get("killedRooms") or []
            self.kill_hist.extend(self.last_killed)
            del self.kill_hist[:-50]
            self.push("kill", f"回合 {d.get('roundId')} 结算: {self.last_killed} 号房被击杀")
            pools = self.snapshot if self.snapshot else self.pools
            self._save_round(pools, killed=self.last_killed)
            self._send("2001")
        elif evt == "3005":
            u = d.get("user") or {}
            self.push("move", f"{u.get('nickname','?')} {d.get('fromRoomId',0)}号 → {d.get('toRoomId',0)}号")
        elif evt == "3007":
            u = d.get("user") or {}
            act = "进入" if d.get("actionType") == 1 else "离开"
            self.push("join" if d.get("actionType") == 1 else "leave", f"{u.get('nickname','?')} {act}")

    # ---- 斗鸡 ----
    def _d_cock(self, evt, d):
        if evt == "2001":
            r = d.get("round") or {}
            if r.get("round_id") != self.round_id:
                self.snap_taken = False
                self.picks = None
            self.round_id = r.get("round_id")
            self.status = r.get("status")
            self.pools = {it["item_id"]: it.get("total_amount", 0) for it in (d.get("items") or [])}
            self.players = {it["item_id"]: it.get("num", 0) for it in (d.get("item_player_num") or [])}
            if r.get("win_item_id"):
                self.winner = r.get("win_item_id")
            st = r.get("stop_time")
            if st:
                try:
                    self.end_ms = int(time.mktime(time.strptime(st, "%Y-%m-%d %H:%M:%S")) - time.timezone) * 1000
                except Exception:
                    pass
        elif evt == "3003":
            iid = d.get("item_id")
            if iid:
                self.pools[iid] = self.pools.get(iid, 0) + d.get("amount", 0)
                self.players[iid] = self.players.get(iid, 0) + 1
            self.push("bet", f"{d.get('nickname','?')} 押 {iid} 号队 {d.get('amount')}")
        elif evt == "3004":
            if self.pools:
                self._final_pools = dict(self.pools)
                self._final_players = dict(self.players)
            self.push("settle", f"回合 {d.get('round_id')} 结算中")
            self._send("2001")
            self._settling = True
        if evt == "2001" and self._settling and self.winner:
            fp = getattr(self, "_final_pools", None)
            if fp:
                self.pools = fp
                self.players = getattr(self, "_final_players", self.players)
            self._save_round(self.pools, winner=self.winner)
            self.push("kill", f"回合 {self.round_id} 胜者 {self.winner} 号队")
            self._settling = False
            self.winner = None
            self._final_pools = None

    # ---- 赛马 ----
    def _d_fox(self, evt, d):
        if evt == "2001":
            r = d.get("round") or {}
            if r.get("round_id") != self.round_id:
                self.snap_taken = False
                self.picks = None
                self.picks_nc = None
            self.round_id = r.get("round_id")
            self.status = r.get("status")
            if r.get("status") == 0 and r.get("room_countdown"):
                self.end_ms = int(time.time() * 1000 + r["room_countdown"] * 1000)
            if r.get("win_item_id"):
                self.winner = r.get("win_item_id")
        elif evt == "2011":
            rounds = d.get("rounds") or []
            if rounds:
                self.winner = rounds[0].get("win_item_id")
                self.rank = rounds[0].get("race_rank_info")
                if self._settling:
                    pools = getattr(self, "_final_pools", None) or self.pools
                    pools_nc = getattr(self, "_final_nc", None) or self.pools_nc
                    self._save_round(pools, winner=self.winner, rank=self.rank,
                                     pools_nc=pools_nc)
                    self.push("kill", f"回合 {self.round_id} 冠军 {self.winner} 号狐")
                    self._settling = False
                    self._final_pools = None
                    self._final_nc = None
        elif evt == "2012":
            self.champ = {it.get("item_id"): it.get("champion_count", 0)
                          for it in (d.get("items") or []) if it.get("item_id")}
        elif evt == "3002":
            self.push("round", f"回合 {d.get('round_id')} 状态变化")
            for e in self.cfg["refresh"]:
                self._send(e)
        elif evt == "3003":
            iids = d.get("item_id") or []
            if isinstance(iids, int):
                iids = [iids]
            tgt = self.pools_nc if d.get("bet_type", 1) == 2 else self.pools
            for iid in iids:
                tgt[iid] = tgt.get(iid, 0) + d.get("amount", 0)
                if tgt is self.pools:
                    self.players[iid] = self.players.get(iid, 0) + 1
            self.push("bet", f"{d.get('nickname','?')} 押 {iids} 号狐 {d.get('amount')}"
                              + ("(非冠军)" if d.get("bet_type", 1) == 2 else ""))
        elif evt == "3004":
            self.push("settle", f"回合 {d.get('round_id')} 比赛结束")
            if self.pools:
                self._final_pools = dict(self.pools)
            if self.pools_nc:
                self._final_nc = dict(self.pools_nc)
            self.pools = {}
            self.players = {}
            self.pools_nc = {}
            self._settling = True
            for e in self.cfg["refresh"]:
                self._send(e)
        elif evt == "3001":
            u = d.get("user") or {}
            act = "进入" if d.get("actionType") == 1 else "离开"
            self.push("join" if d.get("actionType") == 1 else "leave", f"{u.get('nickname','?')} {act}")

    # ---- 存档/快照/输出 ----
    def _save_round(self, pools, **extra):
        if self.round_id is None or self.round_id in self.saved_rounds:
            return
        rec = {"ts": int(time.time()), "game": self.key, "roundId": self.round_id,
               "pools": {int(k): round(v, 2) for k, v in pools.items()},
               "players": {int(k): v for k, v in self.players.items()},
               "picks": self.picks}
        if self.key == "fox":
            rec["picks_nc"] = self.picks_nc
        rec.update(extra)
        with open(os.path.join(DATA, f"rounds_{self.key}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.saved_rounds.add(self.round_id)
        if len(self.saved_rounds) > 300:
            self.saved_rounds = set(list(self.saved_rounds)[-150:])
        # 冠军历史(斗鸡/赛马 smartev 用) + 各基础策略滚动收益(follow 元策略用)
        winner = extra.get("winner")
        if self.key != "steal" and winner is not None:
            self.win_hist.append(winner)
            del self.win_hist[:-20]
        if self.picks and pools:
            killed = set(extra.get("killed") or [])
            for sk, room in self.picks.items():
                if sk == "roundId" or sk not in self.strat_hist or room not in pools:
                    continue
                self.strat_hist[sk].append(sim_settle(self.key, pools, room, 5.0,
                                                      killed=killed, winner=winner))
                del self.strat_hist[sk][:-30]

    def tick(self):
        if self.status != self.cfg["bet_status"] or not self.end_ms or self.snap_taken:
            return
        remain = (self.end_ms - time.time() * 1000) / 1000
        if 0 < remain <= self.SNAPSHOT_LEAD:
            self.snapshot = dict(self.pools)
            self.snapshot_nc = dict(self.pools_nc)
            self.snap_taken = True
            # T-5s 统一决策: 各游戏定房(所有引擎与模拟盘共用, 保证选房一致)
            if self.key == "steal":
                kc = {}
                for k in self.kill_hist:
                    kc[k] = kc.get(k, 0) + 1
                self.picks = compute_picks("steal", self.snapshot, self.players, self.round_id,
                                           kill_counts=kc, kill_window=len(self.kill_hist))
            elif self.key == "cock":
                self.picks = compute_picks("cock", self.snapshot, self.players, self.round_id,
                                           win_hist=self.win_hist)
            else:
                self.picks = compute_picks("fox", self.snapshot, self.players, self.round_id,
                                           win_hist=self.win_hist, champ=self.champ)
                if self.snapshot_nc:
                    self.picks_nc = compute_picks_nc(self.snapshot_nc, self.players, self.round_id,
                                                     win_hist=self.win_hist, champ=self.champ)
            # follow 元策略: 跟随近30盘收益最高的基础策略(用截至上回合数据, 无前视)
            if any(self.strat_hist.values()):
                best_sk = max(self.strat_hist, key=lambda k: sum(self.strat_hist[k]))
                if self.picks.get(best_sk) in self.snapshot:
                    self.picks["follow"] = self.picks[best_sk]
                    self.picks["follow_of"] = best_sk
            self.log.info("[统一决策] 回合%s %s", self.round_id, self.picks)

    def dump(self):
        st = {"updated": int(time.time() * 1000), "game": self.key, "gameName": self.cfg["name"],
              "roundId": self.round_id, "status": self.status, "countdownEndTime": self.end_ms,
              "pools": {str(k): round(v, 2) for k, v in self.pools.items()},
              "players": {str(k): v for k, v in self.players.items()},
              "winner": self.winner, "rank": self.rank, "lastKilled": self.last_killed,
              "picks": self.picks,
              "events": self.events}
        if self.key == "fox":
            st["poolsNc"] = {str(k): round(v, 2) for k, v in self.pools_nc.items()}
            st["picksNc"] = self.picks_nc
            st["champ"] = {str(k): v for k, v in self.champ.items()}
        try:
            write_atomic(os.path.join(DATA, f"live_state_{self.key}.json"), st)
        except Exception:
            pass

# ================= 下注引擎（每用户一个） =================
def room_ev(pools, R):
    P = sum(pools.values())
    n = len(pools)
    s = sum(p / (P - p) for j, p in pools.items() if j != R)
    q = 1.0 / n
    return -q + (1 - q) * FEE * s / (n - 1)


class BetEngine:
    """每用户每游戏一个引擎。逃杀/斗鸡/赛马共用骨架:
    - 逃杀: 3004 直接带官方派息(finalPayoutAmount)
    - 斗鸡/赛马: 3004 后发 2008 查询本回合自己的结算(allocation_amount)
    - 赛马分冠军盘(bet_type=1)/非冠军盘(bet_type=2), 按用户配置选玩法"""

    def __init__(self, user_id, game="steal"):
        self.uid = user_id
        self.game = game
        self.cfg = GAMES[game]
        self.log = logging.getLogger(f"bot-{game}-{user_id}")
        self.stop_flag = False
        self.connected = False
        self.round_id = None
        self.status = None
        self.end_ms = 0
        self.pools = {}
        self.pools_nc = {}  # 赛马非冠军盘池子
        self.players = {}   # 房间 -> 人数 (maxplayers 策略用)
        self.winner = None
        self.bet_rounds = set()
        self.pending = {}
        self.ws = None
        self.clock_off = 0    # 游戏服务器时间 - 本机时间 (ms)
        self.lulu_uid = 0     # 游戏账号 userid(斗鸡下注要带)
        self.settle_rid = None  # 斗鸡/赛马: 等待 2008 结算明细的回合
        # 下注提前量(秒): 持久化在 bot_state.bet_lead, 重启不丢学到的值
        try:
            self.lead = float(get_bot(user_id, game)["bet_lead"] or 3.0)
        except Exception:
            self.lead = 3.0
        self.hell_block = False  # 地狱模式时段禁注
        # 官方档位: 逃杀实测 {0.1,1,10}; 斗鸡/赛马 UI 含 100 档, 被拒自动降档
        self.tiers = [10.0, 1.0, 0.1] if game == "steal" else [100.0, 10.0, 1.0, 0.1]

    @staticmethod
    def compose(amount, tiers):
        """把任意金额(0.1的倍数)组合成档位列表: 0.3->[0.1]*3, 2->[1]*2, 5.5->[1]*5+[0.1]*5"""
        parts, rem = [], round(amount, 2)
        for t in sorted(tiers, reverse=True):
            n = int(rem / t + 1e-9)
            if n > 0:
                parts.extend([t] * n)
                rem = round(rem - n * t, 2)
            if rem <= 1e-6:
                break
        return parts if rem <= 1e-6 else None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"bet-{self.game}-{self.uid}").start()
        threading.Thread(target=self._ticker, daemon=True, name=f"bet-tick-{self.game}-{self.uid}").start()

    def _ticker(self):
        """独立心跳: 每0.5s检查下注窗口(不依赖消息推送触发)"""
        import threading as _t
        self.bet_lock = _t.Lock()
        while not self.stop_flag:
            try:
                with self.bet_lock:
                    self.maybe_bet()
            except Exception:
                pass
            time.sleep(0.5)

    def stop(self, reason):
        set_bot(self.uid, self.game, enabled=0, stop_reason=reason)
        self.stop_flag = True
        self.log.warning("停止: %s", reason)

    def _run(self):
        anchor_dns(self.cfg["host"])
        backoff = 5
        while not self.stop_flag:
            u = get_user(self.uid)
            tok = u["lulu_token"] if u else ""
            uid = u["lulu_userid"] if u else 0
            if not tok or not uid:
                self.stop("缺少游戏 token，请先在设置里登录")
                return
            self.lulu_uid = int(uid)
            url = f"{self.cfg['url']}?token={tok}&userid={uid}"
            self.ws = websocket.WebSocketApp(
                url, on_open=self._on_open, on_message=self._on_message,
                on_error=lambda ws, e: self.log.error("WS: %s", str(e)[:80]),
                on_close=lambda ws, c, r: setattr(self, "connected", False),
                header={"User-Agent": "BestHTTP"})
            t0 = time.time()
            self.ws.run_forever(sslopt={"check_hostname": False}, ping_interval=20)
            if self.stop_flag:
                return
            if time.time() - t0 > 600:
                backoff = 5
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _send(self, evt, d=None):
        try:
            self.ws.send(encrypt(json.dumps({"e": evt, "d": d}, separators=(",", ":"))))
        except Exception:
            pass

    def _on_open(self, ws):
        self.connected = True
        self.log.info("已连接")
        for e in self.cfg["init"]:
            self._send(e)

    def _on_message(self, ws, message):
        for part in message.split("&"):
            part = part.strip()
            if not part:
                continue
            try:
                pt = decrypt(part)
            except Exception:
                continue
            if not pt.startswith("{"):
                pt = "{" + pt
            if not pt.endswith("}"):
                pt += "}"
            try:
                obj = json.loads(pt)
            except json.JSONDecodeError:
                continue
            try:
                self.dispatch(obj)
            except Exception:
                self.log.exception("dispatch")
        try:
            with getattr(self, "bet_lock", __import__("threading").Lock()):
                self.maybe_bet()
        except Exception:
            pass

    def dispatch(self, obj):
        evt, d = obj.get("e"), obj.get("d") or {}
        if evt == "1003":  # 心跳带游戏服务器时间 -> 校准时钟偏移
            if d.get("time"):
                self.clock_off = int(d["time"]) - int(time.time() * 1000)
            return
        if evt == "2002":  # 下注响应（每笔一个）
            self._resp_bet(obj)
            return
        if self.game == "steal":
            self._ev_steal(evt, d)
        elif self.game == "cock":
            self._ev_cock(evt, d)
        else:
            self._ev_fox(evt, d)

    # ---- 逃杀事件 ----
    def _ev_steal(self, evt, d):
        if evt == "2001":
            r = d.get("result") or {}
            self.round_id = r.get("roundId")
            self.status = r.get("status")
            self.end_ms = r.get("countdownEndTime") or 0
            self.pools = {rm["roomId"]: rm.get("totalBet", 0) for rm in (r.get("rooms") or [])}
            self.players = {rm["roomId"]: rm.get("playerCount", 0) for rm in (r.get("rooms") or [])}
        elif evt == "3001":
            self.status = d.get("status")
            if d.get("countdownEndTime"):
                self.end_ms = d["countdownEndTime"]
        elif evt == "3006":
            self.pools[d["roomId"]] = d.get("totalAmount", 0)
            if d.get("playerCount") is not None:
                self.players[d["roomId"]] = d["playerCount"]
        elif evt == "3004":
            rid = d.get("roundId")
            p = self.pending.pop(rid, None)
            if p:
                payout = float(d.get("finalPayoutAmount") or 0)
                staked = round(sum(p["sent"]), 4)
                pnl = payout - staked if staked else 0.0
                status = ("赢" if d.get("isWinner") == 1 else "输") if staked else "未成交"
                if staked and abs(staked - p["amount"]) > 1e-6:
                    status += f"(部分{staked}/{p['amount']})"
                add_bet(self.uid, self.game, rid, p["room"], staked, status, round(pnl, 4))
                bs = get_bot(self.uid, self.game)
                newpnl = (bs["pnl"] or 0) + pnl
                set_bot(self.uid, self.game, pnl=round(newpnl, 4))
                # 止盈止损按游戏官方口径(含手动下注), 查询失败则跳过本次检查
                gd = gamestat_cached(self.uid)
                tp = gd.get("net") if gd.get("code") == 0 else None
                self.log.info("回合%s %s pnl=%+.3f 累计(自算)%+.3f 今日(官方)%s", rid, status, pnl, newpnl,
                              f"{tp:+.3f}" if tp is not None else "?")
                self._check_stop(bs, tp)
            self._send("2001")

    # ---- 斗鸡事件 ----
    def _ev_cock(self, evt, d):
        if evt == "2001":
            r = d.get("round") or {}
            self.round_id = r.get("round_id")
            self.status = r.get("status")
            self.pools = {it["item_id"]: it.get("total_amount", 0) for it in (d.get("items") or [])}
            self.players = {it["item_id"]: it.get("num", 0) for it in (d.get("item_player_num") or [])}
            st = r.get("stop_time")
            if st:
                try:
                    self.end_ms = int(time.mktime(time.strptime(st, "%Y-%m-%d %H:%M:%S")) - time.timezone) * 1000
                except Exception:
                    pass
        elif evt == "3002":
            self._send("2001")
        elif evt == "3003":
            iid = d.get("item_id")
            if iid:
                self.pools[iid] = self.pools.get(iid, 0) + d.get("amount", 0)
                self.players[iid] = self.players.get(iid, 0) + 1
        elif evt == "3004":
            rid = d.get("round_id")
            if rid in self.pending:
                self.settle_rid = rid
                self._send("2008", {"round_id": rid})
            self._send("2001")
        elif evt == "2008":
            self._settle_cock_2008(d)

    def _settle_cock_2008(self, d):
        rid = self.settle_rid
        p = self.pending.get(rid)
        if rid is None or not p:
            return
        r = d.get("round") or {}
        ui = d.get("user_item") or {}
        staked = round(float(ui.get("amount") or 0), 4)
        alloc = float(ui.get("allocation_amount") or 0)
        won = bool(r.get("win_item_id")) and ui.get("item_id") == r.get("win_item_id")
        self._settle_official(rid, p, staked, alloc, won)

    # ---- 赛马事件 ----
    def _ev_fox(self, evt, d):
        if evt == "2001":
            r = d.get("round") or {}
            self.round_id = r.get("round_id")
            self.status = r.get("status")
            if r.get("status") == 0 and r.get("room_countdown"):
                self.end_ms = int(time.time() * 1000 + r["room_countdown"] * 1000)
        elif evt == "3002":
            for e in self.cfg["refresh"]:
                self._send(e)
        elif evt == "3003":
            iids = d.get("item_id") or []
            if isinstance(iids, int):
                iids = [iids]
            tgt = self.pools_nc if d.get("bet_type", 1) == 2 else self.pools
            for iid in iids:
                tgt[iid] = tgt.get(iid, 0) + d.get("amount", 0)
        elif evt == "3004":
            rid = d.get("round_id")
            if rid in self.pending:
                self.settle_rid = rid
                self._send("2008", {"round_id": rid})
            for e in self.cfg["refresh"]:
                self._send(e)
        elif evt == "2008":
            self._settle_fox_2008(d)

    def _settle_fox_2008(self, d):
        rid = self.settle_rid
        p = self.pending.get(rid)
        if rid is None or not p:
            return
        r = d.get("round") or {}
        winner = r.get("win_item_id")
        bt = int(p.get("bet_type") or 1)
        mine = [ui for ui in (d.get("user_items") or [])
                if int(ui.get("bet_type") or 0) == bt and float(ui.get("amount") or 0) > 0]
        staked = round(sum(float(ui.get("amount") or 0) for ui in mine), 4)
        alloc = sum(float(ui.get("allocation_amount") or 0) for ui in mine)
        if bt == 2:  # 非冠军盘: 押的狐没夺冠才算赢
            won = bool(mine) and all(int(ui.get("item_id")) != winner for ui in mine)
        else:
            won = bool(mine) and any(int(ui.get("item_id")) == winner for ui in mine)
        self._settle_official(rid, p, staked, alloc, won,
                              tag="(非冠军盘)" if bt == 2 else "(冠军盘)")

    # ---- 斗鸡/赛马公共结算(官方 allocation_amount 口径) ----
    def _settle_official(self, rid, p, staked, alloc, won, tag=""):
        self.pending.pop(rid, None)
        self.settle_rid = None
        pnl = alloc - staked if staked else 0.0
        status = ("赢" if won else "输") if staked else "未成交"
        if staked and abs(staked - p["amount"]) > 1e-6:
            status += f"(部分{staked}/{p['amount']})"
        add_bet(self.uid, self.game, rid, p["room"], staked, status + tag, round(pnl, 4))
        bs = get_bot(self.uid, self.game)
        newpnl = (bs["pnl"] or 0) + pnl
        set_bot(self.uid, self.game, pnl=round(newpnl, 4))
        # 止盈止损: 斗鸡/赛马按本面板该游戏 bet_log 自算(官方每日接口口径未覆盖这两游戏)
        tp = bet_log_today(self.uid, self.game)
        self.log.info("回合%s %s%s 官方分配%.3f pnl=%+.3f 累计(自算)%+.3f 今日(本游戏)%+.3f",
                      rid, status, tag, alloc, pnl, newpnl, tp)
        self._check_stop(bs, tp)

    def _check_stop(self, bs, tp):
        if tp is None:
            return
        if tp <= -abs(bs["max_loss"]):
            self.stop(f"触发今日亏损上限 {bs['max_loss']}（今日 {tp:.2f}）已自动暂停，次日手动开启")
        elif tp >= float(bs["take_profit"] or 5.0):
            self.stop(f"触发今日止盈 {bs['take_profit']}（今日 {tp:+.2f}）已自动暂停，次日手动开启")

    # ---- 下注响应 ----
    def _resp_bet(self, obj):
        d = obj.get("d") or {}
        rid = d.get("round_id", self.round_id)
        p = self.pending.get(rid)
        if not p:
            return
        if obj.get("code") == 0:
            p["sent"].append(p["queue"].pop(0) if p["queue"] else 0)
            self._flush_queue(p)
            return
        msg = obj.get("msg", "?")
        amt = p["queue"].pop(0) if p["queue"] else 0
        self.log.warning("下注被拒(%.1f): %s", amt, msg)
        if "无效" in msg and amt in self.tiers:
            self.tiers.remove(amt)  # 档位无效 -> 降档重组剩余
            self.log.info("移除无效档位 %.1f, 剩余档位 %s", amt, self.tiers)
            rest = self.compose(round(sum(p["queue"]) + 1e-9, 2), self.tiers)
            p["queue"] = rest if rest else []
        elif "锁定" in msg or "封盘" in msg or "结束" in msg or "上限" in msg:
            if "锁定" in msg:
                self.lead = min(8.0, self.lead + 1.0)
                set_bot(self.uid, self.game, bet_lead=self.lead)
                self.log.info("提前量自适应 -> %.1fs (已持久化)", self.lead)
            p["queue"] = []  # 本回合放弃剩余
        elif "余额" in msg or "不足" in msg or "token" in msg.lower():
            p["queue"] = []
            if not p["sent"]:
                self.stop(f"下注被拒({msg})")
        self._flush_queue(p)

    def _unified_pick(self, strategy, bet_type=1):
        """读采集器发布的统一决策(live_state_<game>.json 的 picks), 匹配当前回合才用"""
        try:
            with open(os.path.join(DATA, f"live_state_{self.game}.json"), encoding="utf-8") as f:
                st = json.load(f)
            picks = st.get("picksNc") if (self.game == "fox" and bet_type == 2) else st.get("picks")
            if picks and picks.get("roundId") == self.round_id:
                return picks.get(strategy)
        except Exception:
            pass
        return None

    def maybe_bet(self):
        if self.stop_flag:
            return
        self.hell_block = in_hell()  # 游戏时间 19:55~21:05 地狱模式, 禁止下注
        if self.hell_block:
            return
        bs = get_bot(self.uid, self.game)
        if not bs["enabled"]:
            return
        if self.status != self.cfg["bet_status"] or not self.end_ms or not self.pools:
            return
        bt = int(bs["fox_bet_type"] or 1) if self.game == "fox" else 1
        pools = self.pools_nc if (self.game == "fox" and bt == 2) else self.pools
        if not pools:
            return
        # 用游戏服务器校准后的时间算剩余
        remain = (self.end_ms - (time.time() * 1000 + self.clock_off)) / 1000
        if not (0 < remain <= self.lead):
            return
        if self.round_id in self.bet_rounds or self.round_id in self.pending:
            return
        amt = float(bs["amount"])
        st = bs["strategy"]
        if st == "follow" and self.game == "fox" and bt == 2:
            st = "smartev"  # 非冠军盘不提供 follow
        # 优先使用采集器 T-5s 的统一决策(与模拟盘完全一致)
        room = self._unified_pick(st, bt)
        if room:
            pass
        elif st == "minpool":
            room = min(pools, key=lambda r: pools[r])
        elif st == "maxpool":
            room = max(pools, key=lambda r: pools[r])
        elif st == "random":
            room = random.choice(list(pools.keys()))
        elif st == "room1":  # 固定押1号
            if 1 not in pools:
                return
            room = 1
        elif st == "maxplayers":  # 押人最多
            room = max(pools, key=lambda r: (self.players.get(r, 0), -pools[r]))
        elif self.game == "cock":  # smartev 本地兜底: 押池占比高的队
            total = sum(pools.values()) or 1.0
            room = max(pools, key=lambda r: pools[r] / total)
        else:  # steal/fox smartev 本地兜底
            room = max(pools, key=lambda r: room_ev(pools, r) if self.game == "steal" else pools[r])
        self.bet_rounds.add(self.round_id)
        # 档位组合: 官方只认固定档位, 任意 0.1 倍数金额自动组合 (0.3->0.1x3, 2->1x2)
        queue = self.compose(amt, self.tiers)
        if not queue or len(queue) > 60:
            self.log.warning("金额 %s 无法组合或笔数过多, 跳过", amt)
            return
        self.pending[self.round_id] = {"roundId": self.round_id, "room": room,
                                       "amount": amt, "queue": queue, "sent": [],
                                       "flushing": False, "bet_type": bt}
        self.log.info("[下注] %s 回合%s 目标%s 总额%s 组合%s 剩余%.1fs",
                      self.game, self.round_id, room, amt, queue, remain)
        self._flush_queue(self.pending[self.round_id])

    def _flush_queue(self, p):
        """串行发送队列: 一次一笔, 收到响应再发下一笔"""
        if p.get("flushing") or not p.get("queue"):
            return
        p["flushing"] = True
        amt = p["queue"][0]
        if self.game == "steal":
            payload = {"roomId": p["room"], "amount": amt, "item_type": ITEM_ID}
        elif self.game == "cock":
            payload = {"round_id": p["roundId"], "item_id": p["room"],
                       "item_type": ITEM_ID, "user_id": self.lulu_uid, "amount": amt}
        else:  # fox: item_id 是列表, amount 为每只金额(引擎只押一只)
            payload = {"round_id": p["roundId"], "item_id": [p["room"]],
                       "bet_type": int(p.get("bet_type") or 1), "amount": amt}
        self._send("2002", payload)
        p["flushing"] = False


def ensure_cols():
    """增量加列（老库兼容）"""
    with db() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(bot_state)").fetchall()]
        for col, ddl in [("harvest_enabled", "INTEGER DEFAULT 0"),
                         ("last_harvest_ts", "TEXT DEFAULT ''"),
                         ("last_harvest_amt", "REAL DEFAULT 0"),
                         ("last_harvest_epoch", "REAL DEFAULT 0"),
                         ("harvest_period", "REAL DEFAULT 0"),
                         ("harvest_target", "REAL DEFAULT 0"),
                         ("harvest_next", "REAL DEFAULT 0"),
                         ("harvest_seen_qty", "REAL DEFAULT 0"),
                         ("bet_lead", "REAL DEFAULT 3.0"),
                         ("take_profit", "REAL DEFAULT 5.0")]:
            if col not in cols:
                c.execute(f"ALTER TABLE bot_state ADD COLUMN {col} {ddl}")


def http_api(path, token, params=None, payload=None):
    """游戏 HTTP API（应用层加密）"""
    s = requests.Session()
    s.headers.update({"token": token, "User-Agent": "BestHTTP"})
    if payload is not None:
        r = s.post(API + path, data=encrypt(json.dumps(payload, separators=(",", ":"))),
                   timeout=15, headers={"Content-Type": "text/plain"})
    else:
        r = s.get(API + path, params=params, timeout=15)
    try:
        return json.loads(decrypt(r.text))
    except Exception:
        if r.status_code == 401 or "Unauthorized" in r.text:
            return {"code": -1, "msg": "token 已失效，请重新登录游戏"}
        return {"code": -1, "msg": f"HTTP {r.status_code}: {r.text[:60]}"}


# ================= 仓库自动收取引擎 =================
class HarvestEngine:
    """周期学习式收取:
    开启 -> 立即收一次并记 t0 -> 探测期每15分钟查, 量回到目标就收, 学习间隔
    -> 学到周期后按周期定时睡到点再收(醒来最多细查几次), 不再频繁收取"""
    PROBE_INTERVAL = 30     # 快检间隔(秒): 兜底监控
    OUT_TIME = 1800         # 基地产出周期(秒) = 30分钟, 由服务器数据反推验证

    def next_output_time(self, camps, now):
        """下次产出时刻 = min(各基地 activation + ceil((now-act)/T)*T), 与游戏客户端同公式"""
        nxt = 0
        for c in camps or []:
            act = c.get("activation_time") or 0
            if act <= 0 or c.get("status") != 1:
                continue
            end = c.get("end_time") or 0
            k = (int(now) - act) // self.OUT_TIME + 1
            t = act + k * self.OUT_TIME
            if end and t > end:
                continue
            if nxt == 0 or t < nxt:
                nxt = t
        return nxt
    WAKE_CHECK = 300        # 定时到点后的确认检查间隔
    WAKE_RETRY = 6          # 到点后最多细查次数
    MIN_COLLECT = 0.1

    def __init__(self, user_id):
        self.uid = user_id
        self.log = logging.getLogger(f"harvest-{user_id}")
        self.stop_flag = False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"harvest-{self.uid}").start()

    def _run(self):
        while not self.stop_flag:
            try:
                self._tick()
            except Exception as e:
                self.log.warning("异常: %s", str(e)[:80])
            time.sleep(30)

    def _tick(self):
        bs = get_bot(self.uid)
        if not bs["harvest_enabled"]:
            return
        now = time.time()
        nxt = bs["harvest_next"] or 0
        if now < nxt:
            return  # 还没到预定时间, 继续睡(外层30s醒一次无请求)
        self._try_collect(bs, now)

    def _try_collect(self, bs, now):
        u = get_user(self.uid)
        if not u or not u["lulu_token"]:
            set_bot(self.uid, harvest_next=now + 3600)
            return
        ov = http_api("/product/overview", u["lulu_token"])
        if ov.get("code") != 0:
            set_bot(self.uid, harvest_next=now + 1800)
            return
        data = ov.get("data") or {}
        wh = data.get("base_camp_warehouse") or {}
        qty = sum(i.get("quantity", 0) for i in (wh.get("items") or []))
        nxt_out = self.next_output_time(data.get("base_camps"), now)
        # 只要有货(>=0.1)就收
        if qty >= self.MIN_COLLECT:
            pass  # 落到下面收取
        elif nxt_out > now + 15:
            # 空仓且距下次产出>15s: 直接定到产出点+10s缓冲, 不用30s轮询
            set_bot(self.uid, harvest_seen_qty=0, harvest_next=nxt_out + 10)
            return
        else:
            # 接近产出点: 30s粒度盯着
            set_bot(self.uid, harvest_seen_qty=0,
                    harvest_next=now + self.PROBE_INTERVAL)
            return
        # 收取
        r = http_api("/product/warehouse/release", u["lulu_token"], payload={"type": 1})
        if r.get("code") != 0:
            self.log.warning("收取失败: %s", r.get("msg"))
            set_bot(self.uid, harvest_next=now + 900)
            return
        amount = (r.get("data") or {}).get("amount", 0)
        # 下次收取 = 下个产出点+10s; 拿不到相位则30s后兜底查
        nxt = self.next_output_time((ov.get("data") or {}).get("base_camps"), now)
        nxt = (nxt + 10) if nxt > now + 15 else (now + self.PROBE_INTERVAL)
        set_bot(self.uid,
                last_harvest_ts=game_hm(fmt="%m-%d %H:%M"),
                last_harvest_amt=amount,
                last_harvest_epoch=now,
                harvest_seen_qty=0,
                harvest_next=nxt)
        self.log.info("收取 %.4f 宝石, 下次收取 %s", amount, game_hm(nxt, "%H:%M:%S"))


    def cycle(self, force=False):
        """手动立即收取"""
        bs = get_bot(self.uid)
        if not force and not bs["harvest_enabled"]:
            return {"code": -1, "msg": "未开启"}
        u = get_user(self.uid)
        if not u or not u["lulu_token"]:
            return {"code": -1, "msg": "未设置 token"}
        try:
            ov = http_api("/product/overview", u["lulu_token"])
            if ov.get("code") != 0:
                return {"code": -1, "msg": ov.get("msg", "查询失败")}
            wh = (ov.get("data") or {}).get("base_camp_warehouse") or {}
            qty = sum(i.get("quantity", 0) for i in (wh.get("items") or []))
            if qty < self.MIN_COLLECT:
                return {"code": 0, "msg": f"仓库存量 {qty:.2f}，暂无可收", "amount": 0}
            r = http_api("/product/warehouse/release", u["lulu_token"], payload={"type": 1})
            if r.get("code") != 0:
                return {"code": -1, "msg": r.get("msg", "收取失败")}
            amount = (r.get("data") or {}).get("amount", 0)
            now = time.time()
            set_bot(self.uid, last_harvest_ts=game_hm(fmt="%m-%d %H:%M"),
                    last_harvest_amt=amount, last_harvest_epoch=now,
                    harvest_target=round(amount, 3), harvest_seen_qty=0,
                    harvest_next=now + self.PROBE_INTERVAL)
            return {"code": 0, "msg": "收取成功", "amount": amount}
        except Exception as e:
            return {"code": -1, "msg": str(e)[:80]}


harvest_engines = {}


def harvest_for(uid):
    if uid not in harvest_engines:
        harvest_engines[uid] = HarvestEngine(uid)
        harvest_engines[uid].start()
    return harvest_engines[uid]


engines = {}
engines_lock = threading.Lock()


def engine_for(uid, game="steal"):
    with engines_lock:
        e = engines.get((uid, game))
        if e and not e.stop_flag and e.ws:
            return e
        e = BetEngine(uid, game)
        engines[(uid, game)] = e
        return e


# ================= Flask =================
app = Flask(__name__)
app.secret_key = os.environ.get("LULU_SECRET") or os.urandom(24).hex()
if not os.environ.get("LULU_SECRET"):
    # 持久化 secret 免得重启丢 session
    sk_file = os.path.join(DATA, "secret.key")
    if os.path.exists(sk_file):
        app.secret_key = open(sk_file).read().strip()
    else:
        open(sk_file, "w").write(app.secret_key)
app.permanent_session_lifetime = datetime.timedelta(days=7)

PAGE_FILE = os.path.join(os.path.dirname(__file__), "dashboard_page.html")


def me():
    uid = session.get("uid")
    return get_user(uid) if uid else None


@app.route("/")
def index():
    if not me():
        return send_file("login_page.html")
    return send_file(PAGE_FILE)


# ---- 认证 ----
@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    j = request.json or {}
    name, pw = (j.get("username") or "").strip(), (j.get("password") or "").strip()
    if not (3 <= len(name) <= 20) or not name.replace("_", "").isalnum():
        return jsonify({"code": -1, "msg": "用户名 3-20 位字母数字下划线"})
    if len(pw) < 6:
        return jsonify({"code": -1, "msg": "密码至少 6 位"})
    salt = os.urandom(16).hex()
    try:
        with db() as c:
            c.execute("INSERT INTO users(username,pw_hash,salt) VALUES(?,?,?)",
                      (name, hash_pw(pw, salt), salt))
    except sqlite3.IntegrityError:
        return jsonify({"code": -1, "msg": "用户名已存在"})
    uid = db().execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()[0]
    with db() as c:
        c.execute("INSERT OR IGNORE INTO bot_state(user_id, game) VALUES(?, 'steal')", (uid,))
    session["uid"] = uid
    session.permanent = True
    return jsonify({"code": 0, "username": name})


@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    j = request.json or {}
    name, pw = (j.get("username") or "").strip(), (j.get("password") or "").strip()
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    if not u or hash_pw(pw, u["salt"]) != u["pw_hash"]:
        return jsonify({"code": -1, "msg": "用户名或密码错误"})
    session["uid"] = u["id"]
    session.permanent = True
    return jsonify({"code": 0, "username": name})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"code": 0})


@app.route("/api/auth/me")
def auth_me():
    u = me()
    return jsonify({"code": 0, "username": u["username"]} if u else {"code": -1})


# ---- 公共数据 ----
@app.route("/api/live")
def api_live():
    g = request.args.get("game", "steal")
    path = os.path.join(DATA, f"live_state_{g}.json")
    try:
        return open(path, encoding="utf-8").read(), 200, {"Content-Type": "application/json"}
    except FileNotFoundError:
        return jsonify({"empty": True})


@app.route("/api/history")
def api_history():
    g = request.args.get("game", "steal")
    slots = GAMES.get(g, GAMES["steal"])["slots"]
    rows = []
    try:
        lines = open(os.path.join(DATA, f"rounds_{g}.jsonl"), encoding="utf-8").readlines()[-100:]
    except FileNotFoundError:
        lines = []
    seen = set()
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r["roundId"] in seen:
            continue
        if not r.get("pools") and r.get("winner") is None and not r.get("killed"):
            continue
        seen.add(r["roundId"])
        rows.append({"roundId": r["roundId"],
                     "time": game_hm(r.get("ts", 0), "%m-%d %H:%M"),
                     "pools": [float(r["pools"].get(str(i), r["pools"].get(i, 0)) or 0) for i in range(1, slots + 1)],
                     "killed": r.get("killed", []), "winner": r.get("winner")})
    return jsonify({"rows": rows[-60:][::-1]})  # 最新回合在最上


@app.route("/api/strat")
def api_strat():
    key = _strat_cache.get("key")
    rows = _strat_rowcount()
    if _strat_cache.get("payload") and _strat_cache.get("key") == rows:
        return jsonify(_strat_cache["payload"])
    payload = compute_strat()
    return jsonify(payload)


_strat_cache = {"key": None, "payload": None, "busy": False}


def _strat_rowcount():
    try:
        return sum(1 for _ in open(os.path.join(DATA, "rounds_steal.jsonl"), encoding="utf-8"))
    except FileNotFoundError:
        return 0


def compute_strat():
    """策略分析(优化版): 预计算每回合选房 + MC 400次, 按数据行数缓存"""
    if _strat_cache.get("busy"):
        return _strat_cache.get("payload") or {"error": "computing"}
    _strat_cache["busy"] = True
    try:
        try:
            fh = open(os.path.join(DATA, "rounds_steal.jsonl"), encoding="utf-8")
        except FileNotFoundError:
            return {"error": "no data"}
        rounds = []
        seen = set()
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r["roundId"] in seen or not r.get("pools") or not r.get("killed"):
                continue
            seen.add(r["roundId"])
            pools = {int(k): v for k, v in r["pools"].items() if v is not None}
            players = {int(k): v for k, v in (r.get("players") or {}).items()}
            if len(pools) >= 4:
                rounds.append((r["roundId"], pools, set(r["killed"]), players))
        rounds.sort(key=lambda x: x[0])
        n = len(rounds)
        if not n:
            return {"error": "no data"}

        # 预计算: 被杀池/存活池/各策略选房
        pre = []
        obs = defaultdict(int)
        all_pairs = []
        pcts = []
        kh = defaultdict(int)  # 滚动50回合被杀计数(smartev多因子, 与实盘同口径)
        kq = []
        for rid, pools, killed, players in rounds:
            total = sum(pools.values())
            dead = sum(v for k, v in pools.items() if k in killed)
            r_ev = {r: room_ev(pools, r) for r in pools}
            if kq:
                score = {r: r_ev[r] - 0.6 * (kh.get(r, 0) / len(kq)) for r in pools}
                smart = max(pools, key=lambda r: score[r])
            else:
                smart = max(pools, key=lambda r: r_ev[r])
            minp = min(pools, key=lambda r: pools[r])
            maxp = max(pools, key=lambda r: pools[r])
            maxpl = max(pools, key=lambda r: players.get(r, 0)) if players else maxp
            pre.append((rid, pools, killed, dead, total - dead, smart, minp, maxp, maxpl))
            for k in killed:
                obs[k] += 1
                kh[k] = kh.get(k, 0) + 1
                kq.append(k)
            while len(kq) > 50:
                kh[kq.pop(0)] -= 1
            for r, v in pools.items():
                all_pairs.append((v, r in killed))
            sp = sorted(pools.values())
            first_killed = next(iter(killed))
            pcts.append(sp.index(pools[first_killed]) / (len(sp) - 1))
        rooms = sorted({k for _, p, _, _, _, _, _, _, _ in pre for k in p})
        exp_k = n / len(rooms)

        def _settle(e, room, bet=5.0):
            _, pools, killed, dead, alive, _, _, _, _ = e
            if room in killed:
                return -bet
            if dead <= 0 or alive <= 0:
                return 0.0
            return bet + FEE * dead * bet / (alive + bet) - bet

        # ---- 六策略逐回合 room/pnl 序列(follow 元策略需要) ----
        SKEYS = ["smartev", "minpool", "maxpool", "random", "room1", "maxplayers"]
        seq_room = {k: [] for k in SKEYS}
        seq_pnl = {k: [] for k in SKEYS}
        for e in pre:
            rid, pools, killed, dead, alive, smart, minp, maxp, maxpl = e
            rooms_here = {
                "smartev": smart, "minpool": minp, "maxpool": maxp,
                "random": random.Random(f"round-{rid}").choice(sorted(pools.keys())),
                "room1": 1 if 1 in pools else None,
                "maxplayers": maxpl,
            }
            for k in SKEYS:
                room = rooms_here[k]
                if room not in pools:
                    seq_room[k].append(None)
                    seq_pnl[k].append(None)
                    continue
                seq_room[k].append(room)
                if room in killed:
                    seq_pnl[k].append(-5.0)
                else:
                    seq_pnl[k].append(5.0 + FEE * dead * 5.0 / (alive + 5.0) - 5.0 if dead > 0 else 0.0)
        # follow: 每回合押"截至上回合近50盘累计最高"策略的房
        fol_room, fol_pnl = [], []
        for i in range(len(pre)):
            past = {}
            for k in SKEYS:
                vals = [x for x in seq_pnl[k][:i][-30:] if x is not None]
                if vals:
                    past[k] = sum(vals)
            if past:
                bk = max(past, key=lambda k: past[k])
                fol_room.append(seq_room[bk][i])
                fol_pnl.append(seq_pnl[bk][i])
            else:
                fol_room.append(None)
                fol_pnl.append(None)

        random.seed(42)
        strats, curves = [], {"rounds": [e[0] for e in pre]}
        rnd_local = random.Random(42)
        for name, idx in [("smartEV(押结构EV最高)", 5), ("minpool(押最小池)", 6),
                          ("maxpool(押最大池)", 7), ("random(基线)", None),
                          ("room1(固定押1号房)", "room1"), ("maxplayers(押人最多房)", 8),
                          ("follow(跟随近30盘最优)", "FOLLOW")]:
            pnl = 0.0
            wins = 0
            cv = []
            bets_n = 0
            if idx == "FOLLOW":
                seq_p = fol_pnl
                for p in seq_p:
                    if p is None:
                        continue
                    bets_n += 1
                    pnl += p
                    wins += 1 if p > 0 else 0
                    cv.append(round(pnl, 2))
                key = "follow"
                curves[key] = cv
                strats.append({"name": name, "wr": wins / bets_n if bets_n else 0,
                               "pnl": round(pnl, 2), "roi": pnl / (bets_n * 5) if bets_n else 0})
                continue
            for e in pre:
                if idx == "room1":
                    if 1 not in e[1]:
                        continue
                    room = 1
                else:
                    room = e[idx] if idx is not None else random.Random(f"round-{e[0]}").choice(sorted(e[1].keys()))
                bets_n += 1
                p = _settle(e, room)
                pnl += p
                wins += 1 if p > 0 else 0
                cv.append(round(pnl, 2))
            key = ("smart" if "smart" in name else "maxpool" if "maxpool" in name else
                   "random" if "random" in name else "minpool" if "minpool" in name else
                   "room1" if "room1" in name else "maxplayers")
            curves[key] = cv
            strats.append({"name": name, "wr": wins / bets_n if bets_n else 0,
                           "pnl": round(pnl, 2), "roi": pnl / (bets_n * 5) if bets_n else 0})

        per = [_settle(e, e[5]) for e in pre]
        m = sum(per) / n
        se = math.sqrt(sum((x - m) ** 2 for x in per) / (n - 1) / n)
        ci_lo, ci_hi = m - 1.96 * se, m + 1.96 * se
        theory = sum(max(room_ev(p, r) for r in p) for _, p, _, _ in rounds) / n
        actual = sum(per)
        rng_mc = random.Random(2024)
        mc = []
        room_keys = [list(e[1].keys()) for e in pre]
        for _ in range(400):
            t = 0.0
            for e, rk in zip(pre, room_keys):
                t += _settle(e, rng_mc.choice(rk))
            mc.append(t)
        mc.sort()
        mc_pct = round(100 * sum(1 for x in mc if x <= actual) / len(mc))

        all_pairs.sort()
        bn = len(all_pairs) // 5
        buckets = []
        for b in range(5):
            seg = all_pairs[b * bn:(b + 1) * bn] if b < 4 else all_pairs[4 * bn:]
            buckets.append({"label": f"池{seg[0][0]:.0f}~{seg[-1][0]:.0f}",
                            "rate": sum(1 for _, k in seg if k) / len(seg) if seg else 0})
        z_pool = (sum(pcts) / n - 0.5) / math.sqrt(1 / 12 / n)
        if ci_lo > 0:
            verdict = f"✅ 正期望成立（CI 下界 {ci_lo/5*100:+.2f}%）——smartEV 有真实优势。"
        elif ci_hi < 0:
            verdict = f"❌ 负期望坐实（CI 上界 {ci_hi/5*100:+.2f}%）——长期 {m/5*100:.1f}%/注 损耗。"
        else:
            verdict = f"⏳ 尚不显著（CI [{ci_lo/5*100:+.2f}%, {ci_hi/5*100:+.2f}%]），MC 第 {mc_pct} 百分位。"
        payload = {"n": n, "theory_ev": theory, "smart_roi": strats[0]["roi"], "ci_lo": ci_lo,
                   "ci_hi": ci_hi, "mc_pct": mc_pct, "z_pool": z_pool,
                   "kill": {"rooms": [str(r) for r in rooms], "obs": [obs.get(r, 0) for r in rooms],
                            "exp": round(exp_k, 1)},
                   "buckets": buckets, "curve": curves,
                   "strats": sorted(strats, key=lambda s: -s["pnl"]), "verdict": verdict}
        _strat_cache["key"] = _strat_rowcount()
        _strat_cache["payload"] = payload
        return payload
    finally:
        _strat_cache["busy"] = False


def _strat_refresher():
    """后台每5分钟预计算, 用户点开即秒回"""
    while True:
        try:
            if _strat_cache.get("key") != _strat_rowcount():
                compute_strat()
        except Exception:
            pass
        time.sleep(300)


# ---- 用户 token 工具 ----
@app.route("/api/login/send", methods=["POST"])
def api_login_send():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    tel = (request.json or {}).get("tel", "").strip()
    if not (tel.isdigit() and len(tel) == 11):
        return jsonify({"code": -1, "msg": "手机号格式不对"})
    try:
        r = requests.post(API + "/account/send_phone_code",
                          data=encrypt(json.dumps({"tel": tel}, separators=(",", ":"))), timeout=15,
                          headers={"token": "-1", "User-Agent": "BestHTTP", "Content-Type": "text/plain"})
        try:
            return jsonify(json.loads(decrypt(r.text)))
        except Exception:
            if "_guard" in r.text or r.status_code in (456, 503):
                return jsonify({"code": -1, "msg": "游戏防护拦截（服务器繁忙或维护中），请稍后再试"})
            return jsonify({"code": -1, "msg": f"游戏服务异常 HTTP {r.status_code}"})
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)[:80]})


@app.route("/api/login/do", methods=["POST"])
def api_login_do():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    j = request.json or {}
    tel, code = j.get("tel", "").strip(), j.get("code", "").strip()
    try:
        r = requests.post(API + "/account/phone_login",
                          data=encrypt(json.dumps({"tel": tel, "code": code, "invite_code": 0},
                                                  separators=(",", ":"))), timeout=15,
                          headers={"token": "-1", "User-Agent": "BestHTTP", "Content-Type": "text/plain"})
        try:
            d = json.loads(decrypt(r.text))
        except Exception:
            if "_guard" in r.text or r.status_code in (456, 503):
                return jsonify({"code": -1, "msg": "游戏防护拦截（服务器繁忙或维护中），请稍后再试"})
            return jsonify({"code": -1, "msg": f"游戏服务异常 HTTP {r.status_code}"})
        if d.get("code") == 0 and d.get("data", {}).get("token"):
            with db() as c:
                c.execute("UPDATE users SET lulu_token=?, lulu_userid=? WHERE id=?",
                          (d["data"]["token"], d["data"]["user_id"], u["id"]))
        return jsonify(d)
    except Exception as e:
        return jsonify({"code": -1, "msg": str(e)[:80]})


@app.route("/api/token", methods=["GET", "POST"])
def api_token():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    if request.method == "POST":
        tok = (request.json or {}).get("token", "").strip()
        if len(tok) < 16:
            return jsonify({"code": -1, "msg": "token 格式不对"})
        with db() as c:
            c.execute("UPDATE users SET lulu_token=? WHERE id=?", (tok, u["id"]))
        return jsonify({"code": 0})
    tok = u["lulu_token"] or ""
    return jsonify({"token": (tok[:6] + "..." + tok[-4:]) if tok else "", "userid": u["lulu_userid"]})


@app.route("/api/balance")
def api_balance():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    if not u["lulu_token"]:
        return jsonify({"code": -1, "msg": "未设置 token"})
    try:
        s = requests.Session()
        s.headers.update({"token": u["lulu_token"], "User-Agent": "BestHTTP"})
        r = s.get(API + "/player/items", timeout=15)
        d = json.loads(decrypt(r.text))
        items = d.get("data", {}).get("items", [])
        bal = next((i["item_num"] for i in items if i.get("item_id") == ITEM_ID), None)
        return jsonify({"code": 0, "balance": bal})
    except Exception as e:
        s = str(e)
        return jsonify({"code": -1, "msg": "token 已失效" if "401" in s or "b64" in s else s[:80]})


# ---- 自动下注 ----
@app.route("/api/bot/sim")
def api_bot_sim():
    """策略模拟盘: 最近N回合对各策略做虚拟下注(不真实下注), 返回汇总+逐回合明细
    game=steal|cock|fox; 赛马 mode=1(冠军盘)|2(非冠军盘)"""
    game = request.args.get("game", "steal")
    if game not in GAMES:
        return jsonify({"error": "bad game"})
    mode = request.args.get("mode", "1")
    nc = (game == "fox" and mode == "2")
    N = 50
    rounds = []
    try:
        lines = open(os.path.join(DATA, f"rounds_{game}.jsonl"), encoding="utf-8").readlines()
    except FileNotFoundError:
        return jsonify({"error": "no data"})
    seen = set()
    for line in reversed(lines):  # 从尾部取最新N个有决策记录的回合
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        picks = r.get("picks_nc") if nc else r.get("picks")
        if r["roundId"] in seen or not r.get("pools") or not picks:
            continue
        if game == "steal" and not r.get("killed"):
            continue
        if game != "steal" and r.get("winner") is None:
            continue
        seen.add(r["roundId"])
        pools = {int(k): v for k, v in r["pools"].items() if v is not None}
        if nc:
            pools = {int(k): v for k, v in (r.get("pools_nc") or {}).items() if v is not None}
        if pools:
            rounds.append((r["roundId"], pools, set(r.get("killed") or []),
                           r.get("winner"), picks))
        if len(rounds) >= N:
            break
    rounds.sort(key=lambda x: x[0])  # 时间正序
    if not rounds:
        return jsonify({"error": "no data"})

    defs = [
        ("smartev", "smartEV(智能优选)"),
        ("minpool", "minpool(押最小池)"),
        ("maxpool", "maxpool(押最大池)"),
        ("random", "random(随机)"),
        ("room1", "room1(固定1号)"),
        ("maxplayers", "maxplayers(押人最多)"),
    ]
    if not nc:  # 非冠军盘不提供 follow(依赖各基础策略滚动收益)
        defs.append(("follow", "follow(跟随近30盘最优)"))
    out = []
    BET = 5.0
    for key, name in defs:
        pnl_total = 0.0
        wins = 0
        detail = []
        for rid, pools, killed, winner, picks in rounds:
            room = picks.get(key)
            if room not in pools:
                continue
            if nc:
                p = sim_settle_nc(pools, room, BET, winner)
            else:
                p = sim_settle(game, pools, room, BET, killed=killed, winner=winner)
            pnl_total += p
            wins += 1 if p > 0 else 0
            detail.append({"roundId": rid, "room": room, "win": p > 0,
                           "pnl": round(p, 3)})
        n_bets = len(detail)
        out.append({"key": key, "name": name, "pnl": round(pnl_total, 2),
                    "wr": round(wins / n_bets, 4) if n_bets else 0,
                    "roi": round(pnl_total / (n_bets * BET), 4) if n_bets else 0,
                    "bets": n_bets, "detail": detail[-50:][::-1]})  # 最近50回合, 新的在前
    out.sort(key=lambda s: -s["pnl"])
    return jsonify({"game": game, "mode": 2 if nc else 1, "rounds": len(rounds),
                    "latestRound": rounds[-1][0], "strats": out})


_gamestat_cache = {}
_gamestat_lock = threading.Lock()


def _current_steal_round():
    """当前逃杀回合号(读全局采集状态, 廉价)"""
    try:
        return json.load(open(os.path.join(DATA, "live_state_steal.json"), encoding="utf-8")).get("roundId")
    except Exception:
        return None


def gamestat_cached(uid):
    """官方每日统计(按回合缓存): 每用户每回合只真实查询一次, 熔断/累计/前端共用"""
    rid_now = _current_steal_round()
    with _gamestat_lock:
        ent = _gamestat_cache.get(uid)
        if ent and ent[0] == rid_now and rid_now is not None:
            return ent[1]
    u = get_user(uid)
    if not u or not u["lulu_token"]:
        result = {"code": -1, "msg": "未设置 token"}
    else:
        try:
            d = http_api("/boss/gameDailyConsume/summary", u["lulu_token"],
                         params={"game_type": 21, "query_type": 0})
            if d.get("code") == 0:
                data = d.get("data") or {}
                consume = data.get("total_consume", 0) or 0
                gain = data.get("total_gain", 0) or 0
                net = round(gain - consume, 4)
                today = game_now().date().isoformat()
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO official_daily(user_id,day,net,consume,gain) VALUES(?,?,?,?,?)",
                              (uid, today, net, consume, gain))
                    row = c.execute("SELECT MIN(day) FROM official_daily WHERE user_id=?", (uid,)).fetchone()
                    first_official = row[0] if row and row[0] else today
                    legacy = c.execute("SELECT COALESCE(SUM(pnl),0) FROM bet_log WHERE user_id=? AND day<?",
                                       (uid, first_official)).fetchone()[0]
                    off_before = c.execute("SELECT COALESCE(SUM(net),0) FROM official_daily WHERE user_id=? AND day<?",
                                           (uid, today)).fetchone()[0]
                result = {"code": 0, "consume": consume, "gain": gain, "net": net,
                          "cum": round(legacy + off_before + net, 4)}
            else:
                result = {"code": -1, "msg": d.get("msg", "查询失败")}
        except Exception as e:
            result = {"code": -1, "msg": str(e)[:80]}
    with _gamestat_lock:
        _gamestat_cache[uid] = (rid_now, result)
    return result


@app.route("/api/bot/gamestat")
def api_bot_gamestat():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    return jsonify(gamestat_cached(u["id"]))


@app.route("/api/bot/config", methods=["GET", "POST"])
def api_bot_config():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    game = request.values.get("game", "steal")
    if game not in GAMES:
        return jsonify({"code": -1, "msg": "未知游戏"})
    if request.method == "POST":
        j = request.json or {}
        bs = get_bot(u["id"], game)
        if j.get("enabled"):
            if not u["lulu_token"]:
                return jsonify({"code": -1, "msg": "请先登录游戏获取 token"})
            amt = round(float(j.get("amount", 0.1)), 2)
            if amt < 0.1 or abs(amt * 10 - round(amt * 10)) > 1e-6:
                return jsonify({"code": -1, "msg": "金额需为 0.1 的倍数且不低于 0.1（如 0.3 会自动拆成 0.1×3）"})
            if float(j.get("max_loss", 0)) <= 0:
                return jsonify({"code": -1, "msg": "必须设置亏损上限"})
        set_bot(u["id"], game,
                enabled=1 if j.get("enabled") else 0,
                strategy=j.get("strategy", bs["strategy"]),
                amount=max(0.1, float(j.get("amount", bs["amount"]))),
                max_loss=max(0.1, float(j.get("max_loss", bs["max_loss"]))),
                take_profit=max(0.1, float(j.get("take_profit", bs["take_profit"] or 5.0))),
                stop_reason="")
        if game == "fox":
            set_bot(u["id"], "fox", fox_bet_type=2 if int(j.get("fox_bet_type", 1) or 1) == 2 else 1)
        if j.get("enabled"):
            engine_for(u["id"], game).start()
        return jsonify({"code": 0})
    bs = get_bot(u["id"], game)
    out = {"game": game, "strategy": bs["strategy"], "amount": bs["amount"],
           "max_loss": bs["max_loss"], "take_profit": bs["take_profit"] or 5.0,
           "enabled": bool(bs["enabled"])}
    if game == "fox":
        out["foxBetType"] = int(bs["fox_bet_type"] or 1)
    return jsonify(out)


@app.route("/api/bot/status")
def api_bot_status():
    u = me()
    if not u:
        return jsonify({"code": -1})
    game = request.args.get("game", "steal")
    if game not in GAMES:
        return jsonify({"code": -1, "msg": "未知游戏"})
    bs = get_bot(u["id"], game)
    with db() as c:
        bets = [{"time": r["ts"], "roundId": r["round_id"], "room": r["room"],
                 "amount": r["amount"], "status": r["status"], "pnl": r["pnl"]}
                for r in c.execute(
                    "SELECT ts,round_id,room,amount,status,pnl FROM bet_log "
                    "WHERE user_id=? AND game=? ORDER BY id DESC LIMIT 10",
                    (u["id"], game)).fetchall()]
    e = engines.get((u["id"], game))
    today = game_now().date().isoformat()
    with db() as c:
        tp = c.execute("SELECT COALESCE(SUM(pnl),0) FROM bet_log WHERE user_id=? AND game=? AND day=?",
                       (u["id"], game, today)).fetchone()[0]
    out = {"game": game, "enabled": bool(bs["enabled"]), "connected": bool(e and e.connected),
           "stopReason": bs["stop_reason"] or "",
           "hellBlock": bool(e and getattr(e, "hell_block", False)),
           "pnl": bs["pnl"], "todayPnl": round(tp, 4),
           "strategy": bs["strategy"], "amount": bs["amount"],
           "maxLoss": bs["max_loss"], "takeProfit": bs["take_profit"] or 5.0,
           "bets": bets}
    if game == "fox":
        out["foxBetType"] = int(bs["fox_bet_type"] or 1)
    return jsonify(out)


@app.route("/api/harvest/config", methods=["GET", "POST"])
def api_harvest_config():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    if request.method == "POST":
        j = request.json or {}
        enable = 1 if j.get("enabled") else 0
        if enable and not u["lulu_token"]:
            return jsonify({"code": -1, "msg": "请先在上方登录游戏获取 token"})
        set_bot(u["id"], harvest_enabled=enable, harvest_period=0, harvest_next=0)
        if enable:
            harvest_for(u["id"])
        return jsonify({"code": 0})
    bs = get_bot(u["id"])
    import time as _t
    return jsonify({"enabled": bool(bs["harvest_enabled"]),
                    "lastTs": bs["last_harvest_ts"], "lastAmt": bs["last_harvest_amt"],
                    "seenQty": bs["harvest_seen_qty"] or 0,
                    "periodMin": round((bs["harvest_period"] or 0) / 60),
                    "nextTs": (game_hm(bs["harvest_next"], "%H:%M")
                               if bs["harvest_next"] > _t.time() else "")})


@app.route("/api/harvest/run", methods=["POST"])
def api_harvest_run():
    u = me()
    if not u:
        return jsonify({"code": -1, "msg": "请先登录"})
    r = harvest_for(u["id"]).cycle(force=True)
    return jsonify(r)


if __name__ == "__main__":
    init_db()
    ensure_bet_log_day()
    ensure_cols()
    # 全局采集器（3 游戏）
    for key in GAMES:
        Collector(key).start()
        time.sleep(1)
    # 策略分析后台预计算
    threading.Thread(target=_strat_refresher, daemon=True).start()
    # 恢复已开启的引擎（每用户每游戏）
    with db() as c:
        for r in c.execute("SELECT user_id, game FROM bot_state WHERE enabled=1").fetchall():
            engine_for(r["user_id"], r["game"]).start()
        for r in c.execute("SELECT DISTINCT user_id FROM bot_state WHERE harvest_enabled=1").fetchall():
            harvest_for(r["user_id"])
    from waitress import serve
    port = int(os.environ.get("LULU_PORT", "18080"))
    log.info("服务启动 :%d", port)
    serve(app, host="0.0.0.0", port=port, threads=16)
