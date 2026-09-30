# gakka_reserve

东京（警視庁）驾照**学科試験**网上预约的空位监控 + 自动预约 / 改约脚本。
对应网站：<https://license-test.tokyo-madoguchi-yoyaku.com>

只用 Python 3 标准库，无第三方依赖。

## 功能

- 启动时如果没有预约：自动约一个最好的空位（日期最早）；之前的预约已过期的，先在网站上取消再约。
- 之后每 3 秒实时查询一天，把比当前预约更早的日子轮流查一遍；一有空位就**先取消、再预约**，失败则按顺序重新预约原场次或下一个最早的场次。
- 排序：日期越早越好；同一天按 `PLACES` 的顺序优先，再按 `PREFER_PM` 优先下午或上午。
- 只约**明天及以后**（按日本时间实时计算）；可用 `DATE_FROM` / `DATE_TO` 限定范围，范围已过则报错退出。
- 预约成功、改约、失败时发邮件（可选）。

## 使用

```bash
git clone <repo> && cd gakka_reserve
cp .env.example .env        # 填写申请人信息，邮件可选
python3 check.py --test-mail
python3 check.py            # 前台运行，Ctrl+C 结束
```

### macOS 后台常驻（launchd）

```bash
sed "s#__DIR__#$PWD#g" launchd.plist > ~/Library/LaunchAgents/com.gakka.reserve.plist
launchctl load ~/Library/LaunchAgents/com.gakka.reserve.plist     # 启动（登录时也会自动启动）
launchctl unload ~/Library/LaunchAgents/com.gakka.reserve.plist   # 停止
tail -f check.log
```

改了 `.env` 或代码后重启：`launchctl kickstart -k gui/$(id -u)/com.gakka.reserve`

> 考完或不再需要时请 `unload` 并删除 plist：脚本每次启动时如果发现没有预约，会自动约一个新的。

## 配置（`.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `RES_NAME` `RES_PHONE` `RES_BIRTHDAY` `RES_GRACER_NO` | | 申请人信息，和网站上填的一致 |
| `AUTO_BOOK` | `1` | `0` = 只发通知不预约 |
| `COURSE` | `11` | 课程代码（`11` 为作者使用的学科試験，其他课程请自行抓包） |
| `PLACES` | `250,270,280` | 要查的考场，同一天越靠前越优先 |
| `DATE_FROM` / `DATE_TO` | 明天 / 不限 | 预约范围，`YYYY-MM-DD` |
| `PREFER_PM` | `1` | 同一天优先下午（`0` = 上午） |
| `REQ_INTERVAL` | `3` | 实时查询间隔（秒） |
| `CAL_REFRESH` | `300` | 月历刷新间隔（秒） |
| `SMTP_*` `MAIL_TO` | | 邮件通知，`SMTP_USER` 留空则不发 |

考场代码：`250` = 江東、`270` = 府中、`280` = 鮫洲。

## 原理

网站前端直接调用以下 API：

| 接口 | 用途 |
|---|---|
| `GET /calgetres?date=YYYYMM&coursecode=&placecode=&user=pub` | 月历。服务端缓存约 5 分钟，只用来决定要查哪几天 |
| `POST /getres` `{date, coursecode, placecode}` | 某一天的实时余位 |
| `POST /customerops` `action=1` | `A1101` 没有预约 / `B1101` 已有预约 |
| `POST /cancel` `action=3` | `A1301` 取消成功 |
| `POST /putres` | `A4001` 预约成功 / `B4002` 满员 |

每人只能持有一个预约，所以改约 = 先取消再预约。月历有缓存，上面显示的"空位"经常早已被抢走，所以每次都以 `getres` 的实时结果为准再动手。

当前预约保存在 `booked.json`；脚本只管理自己约的场次，在网站上手动约过的请先取消。

## 注意

- 请求太频繁会被 WAF 返回 403（实测 1–2 秒一次会触发），脚本遇到 403 会暂停 60 秒。请不要调低 `REQ_INTERVAL`。
- 仅供个人预约自己的考试使用。非官方工具，请遵守网站的使用规定，使用风险自负。

## License

MIT
