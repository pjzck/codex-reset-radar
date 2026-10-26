# Codex Reset Radar · Codex 重置雷达

监测 [codex-reset.com](https://codex-reset.com/) 的重置通知，以简洁中文消息发送到飞书 / Lark 群机器人。默认只读取这一个网站，不做双站交叉核对。

Python 3.9+，零第三方 Python 依赖。支持预告、网站观测到账、后续 Tibo 确认、备用重置、北京时间、持久去重和 GitHub 拉取部署。

## 通知格式

网站观测确认后发送：

```text
Codex 已重置
类型：常规重置
观测时间：2026-10-24 01:40:51（北京时间）
公告译文：
Codex 重置已确认。
我们的检查显示，Codex 用量额度约于北京时间 01:40 重置。
Tibo 尚未发布确认消息，后续确认后可能更新。
消息来源：网站观测
网站：https://codex-reset.com/
相关公告：原始公告链接
```

预告发送：

```text
Codex 已安排重置
类型：常规重置
公告时间：2026-10-23 12:47:34（北京时间）
预计最晚：2026-10-24 01:00:00（北京时间）
重置概率：83%（公告时点，站点模型，非官方承诺）
状态：等待网站确认执行
公告原文：
Landing tomorrow before 10am PST together with a small ship.
消息来源：Tibo（@thsottiaux）
网站：https://codex-reset.com/
原文链接：原始公告链接
```

上面是格式示例。运行时采用网站当前记录，所有生成的时间统一换算为北京时间。网站和原文链接保留在消息末尾，不重复展示摘要、核对结果或多个未知时间字段。

## 数据与判定

运行时每轮读取该站的[中文通知 API](https://codex-reset.com/api/forecast?locale=zh&tz=Asia%2FShanghai)，使用 `latest_alert` 的结构化状态：

- 网站报告 `operator_observed` / `probe_observed` 且状态为 confirmed：显示“已重置”，来源明确标为“网站观测”；不要求 Tibo 已发布消息。
- Tibo 后续确认或网站修正观测时间时，可发送一次新的确认更新。
- Watch / 预告：显示“已安排重置”。有结构化窗口时用 `window.target_at` / `end_at` 作为截止时间；没有窗口或时间表述时截止时间标“待确认”，仍然通知。
- 预告附带站点给出的概率：优先取该条预警的 `score`，没有时回退到 `probabilities.rounded_24h`，并在文案里注明是站点模型、非官方承诺；概率变化不触发重发。
- 到了预计时间仍不能自动升级为“已重置”。仅凭模型概率不会产生预告，概率只出现在站点已标记的预警里。Tibo 自称已完成或进行中（`pending`）的预警本轮不通知，只认站点观测到的确认。
- 网站没有提供明确截止时间时显示“待确认”；观测时间、公告时间与执行时间不混用。

中文观测译文直接采用网站的中文内容，其中明确的 UTC 时间换算为北京时间。此站是非官方追踪器，网站观测与 Tibo 确认在来源字段中区分，均不等于读取你的个人账户。

旧的 `com` / `org` 来源仅为兼容已有配置保留。默认配置及当前部署只使用 `reset`，即 codex-reset.com。

## 配置与运行

```bash
git clone https://github.com/pjzck/codex-reset-radar.git
cd codex-reset-radar
cp .env.example .env
```

编辑私密 `.env`，填写自己的群机器人地址：

```dotenv
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_WEBHOOK_ID
FEISHU_SECRET=
```

`.env` 已加入 `.gitignore`。飞书启用签名校验时填写 `FEISHU_SECRET`；若设置关键词，允许 `Codex`。参见[飞书机器人指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

```bash
# 预览，不发送、不改去重记录
python -m radar preview

# 持续运行 / 检查一次
python -m radar run --env-file .env
python -m radar check --env-file .env

# 分别发送两条简版格式样例
python -m radar test-message --env-file .env

# 使用同站真实数据：历史预告回放 + 当前通知，各自单独发送并注明用途
python -m radar test-message --real-samples --env-file .env
```

PowerShell 可用 `Copy-Item .env.example .env`。命令从项目根目录运行。

可复制 `config.example.json` 为 `config.local.json` 并通过 `--config config.local.json` 使用：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `sources` | `["reset"]` | 只读 codex-reset.com |
| `poll_interval_seconds` | `60` | 每轮结束后等待秒数，最小 30 |
| `request_timeout_seconds` | `20` | 网络请求超时 |
| `state_path` | `data/state.sqlite3` | SQLite 去重状态，`RADAR_STATE_PATH` 可覆盖 |
| `first_run` | `latest` | 首次通知最新记录；`silent` 只建立基线 |
| `max_event_age_hours` | `72` | 历史时效，过旧记录不推送 |
| `notify_possible` | `true` | 是否发送预告；false 只发完成记录 |

预告、观测确认、后续 Tibo 确认分别去重；重复轮询、重启和更新代码不会重复通知。同一公告的截止时间被网站修改时可通知修订。空闲时无可行动通知属于正常结果。接口异常会记录错误并下轮重试，不建立错误基线。

飞书只有业务码为 0 才记为成功；失败下轮重试。若群已收到消息但响应中断，或发送后落库前进程崩溃，可能重复一次。只允许一个发送进程使用同一状态目录。

## Linux 部署

流程：**本地提交 → 推送 GitHub → 服务器拉取 → 导入检查 → systemd 重启**。

服务器需要 Python 3.9+、Git、systemd 和 flock。Rocky Linux 可运行 `sudo dnf install -y git`。

```bash
sudo git clone https://github.com/pjzck/codex-reset-radar.git /opt/codex-reset-radar
sudo install -d -m 755 /etc/codex-reset-radar
sudo install -m 600 /opt/codex-reset-radar/.env.example /etc/codex-reset-radar/radar.env
sudoedit /etc/codex-reset-radar/radar.env
sudo bash /opt/codex-reset-radar/scripts/deploy.sh
```

代码位于 `/opt/codex-reset-radar`；私密 Webhook 位于 `/etc/codex-reset-radar/radar.env`（权限 600）；行为配置位于 `/etc/codex-reset-radar/config.json`；去重数据库位于 `/var/lib/codex-reset-radar/state.sqlite3`。服务以非特权动态用户运行，开机自动启动，不监听公网端口。

本地提交代码后执行：

```powershell
.\scripts\publish-deploy.ps1
```

默认 SSH 别名为 `tx-cloud`，可用 `-Server other-alias` 修改。服务器若有未提交更改或不在 main 分支，部署脚本会停止，避免覆盖。

```bash
sudo systemctl status codex-reset-radar --no-pager
sudo journalctl -u codex-reset-radar -n 30 --no-pager
```

## 开发

```bash
python -m unittest discover -s tests -v
```

只保留两条核心冒烟测试，覆盖预告与网站观测、发送失败重试、重启后去重；不为格式细节或旧来源扩展测试。CI 只运行 Python 3.9，发布和部署不重复跑测试，服务器只检查模块能否导入。

自动化检查模拟发送，不会发真实消息。`test-message` 是可选的手动验收命令，不打开生产去重数据库。真实历史预告回放不代表当前仍待执行，消息会标明“非当前状态”。

## License

MIT © 2026 pjzck。公告与网站内容归各自权利人所有，本项目不隶属于 OpenAI 或数据来源网站。
