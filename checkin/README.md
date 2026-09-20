# 露露王国 · 每日打卡助手(独立模块)

游戏"每日打卡"玩法的自动打卡工具, 可脱离面板独立运行。

## 玩法规则(游戏官方)

| 阶段 | 时间 | 说明 |
|---|---|---|
| 参与投入 | 每日 9:30 ~ 24:00 | 选择本期投入宝石; **单期投入 ≥100** 参与幸运星抽取, **投入越早幸运星概率越高** |
| 按时打卡 | 次日 8:00 ~ 9:00 | 打卡成功→参与瓜分; 打卡失败→本期投入 100% 进入奖池 |
| 结算 | 每日 9:30 | 奖池 **95%** 由打卡成功者按投入占比瓜分; **5%** 奖励给当期 1 名幸运星 |

## 依赖

```
pip install requests
```

## 使用

```bash
# 1. 登录拿 token(单设备登录, 游戏客户端登录会顶掉此 token)
python login.py send 13200000000     # 发验证码
python login.py do 13200000000 123456 # 登录, 存 token.json

# 2. 手动预约投入(最低 1 宝石, 真实扣费; 建议投入期一开始就投, 幸运星概率更高)
python checkin.py join 10

# 3. 自动打卡(常驻; 8:00~9:00 窗口自动打卡, 失败每 20 秒重试; 不自动投入)
python checkin.py auto

# 随时查看状态
python checkin.py status
```

## 协议说明

- 全部走游戏官方 HTTP 接口(api.lululu.com.cn), 应用层 AES+HMAC 加密(lulu_crypto.py)
- `GET /daily-checkin/data` 返回 stage 状态机(3=投入期/1=打卡期/2=结算中), 本工具只以
  游戏服务器返回的 stage 判断阶段, 不依赖本机时钟, 规则时间调整自动适应
- `POST /daily-checkin/join {invest_amount}` 投入; `POST /daily-checkin/punch {}` 打卡
- token 为登录凭证, 请勿泄露; 游戏为单设备登录, 在游戏客户端登录会使此 token 失效
