# Codex Reset Radar · Codex 重置雷达

监测两个非官方 Codex 重置追踪站点，将新公告发送到可配置的飞书 / Lark 群机器人。

Python 3.9+，**零第三方 Python 依赖**。支持普通重置、Banked 重置、预告 / 可能重置分类、跨站去重、SQLite 持久状态、飞书签名和 Linux systemd 部署。

## 数据来源与通知语义

| 来源 | 获取方式 | 处理方式 |
| --- | --- | --- |
| [codexresets.com](https://codexresets.com/) | 首页 HTML 的公告列表 | 使用机器时间戳、公告原文、状态标签和 X 原始链接；不使用预测概率判断重置 |
| [codex-resets.org](https://codex-resets.org/) | [`/api/resets`](https://codex-resets.org/api/resets) JSON，最近一页 | 保留普通 / Banked 类型和 `x_post` / `observed` 来源标注 |

这两个站点不是 OpenAI 官方接口。机器人报告的是**站点收录的信息**，不能验证个人额度是否到账。两个站点的时间或确认方式可能不一致，合并消息保留各自标注及北京时间（固定 UTC+8），不推断哪个正确。`.org` 当前历史接口没有独立预告列表，预告监测来自 `.com`。

同一 X 公告 ID、同一阶段只发一次；跨站时间相差一小时也不会重复。预告之后变为确认，会再通知一次。不同 X 公告仍视为不同事件。无 X 链接的记录按站点 ID 去重。轮询只读取公开页面，不需要 Codex Token、X 账号或浏览器。

## 本地运行

```bash
git clone https://github.com/pjzck/codex-reset-radar.git
cd codex-reset-radar
cp .env.example .env
```

编辑 `.env`，填写自己的地址：

```dotenv
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_WEBHOOK_ID
FEISHU_SECRET=
```

`.env` 是私密配置，已加入 `.gitignore`。若飞书机器人启用了签名校验，填写 `FEISHU_SECRET`。若启用了关键词，允许 `Codex Reset Radar`；若启用了 IP 白名单，需要放行运行机器的出口 IP。参见[飞书自定义机器人指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

```bash
# 只抓取并预览最近 72 小时公告，不发送、不改状态，不需要 Webhook
python -m radar preview

# 发送一条明确标记为测试的连通消息，不改变去重状态
python -m radar test-message --env-file .env

# 检查一次 / 持续运行
python -m radar check --env-file .env
python -m radar run --env-file .env

# 查看各来源最近检查时间和处理记录数
python -m radar status
```

PowerShell 中用 `Copy-Item .env.example .env` 替代 `cp` 也可以。所有命令从项目根目录运行；相对路径按当前目录解析。

## 配置

复制 `config.example.json` 为 `config.local.json`，通过 `--config config.local.json` 使用。省略该参数时使用相同默认值。

| 配置 | 默认 | 含义 |
| --- | --- | --- |
| `sources` | `["com", "org"]` | 选择一个或两个来源 |
| `poll_interval_seconds` | `120` | 每轮结束后等待秒数，最小 30 秒 |
| `request_timeout_seconds` | `20` | 单次网络请求超时 |
| `state_path` | `data/state.sqlite3` | 持久去重数据库；`RADAR_STATE_PATH` 可覆盖 |
| `first_run` | `"latest"` | 首次每个来源只通知最新且在时效内的一条；`"silent"` 只建立基线 |
| `max_event_age_hours` | `72` | 超过时效的历史记录不发送，防止首次运行或断线恢复刷屏 |
| `notify_possible` | `true` | 是否通知可能重置和预告；`false` 只发送来源标为确认的记录 |

初次只推送最新记录，其余建立历史基线；已有状态时推送所有尚未处理且在时效内的记录。超过当前时间 5 分钟的记录保留到后续检查，不提前发送。一个来源失败不会阻止另一个；结构异常不会建立空基线。GET 请求对临时网络错误最多重试三次；飞书失败下轮重试，只有 HTTP 成功且飞书业务码为 0 才记为已发送。

SQLite 状态应保留在更新目录之外。飞书没有本机器人可用的幂等提交键：若飞书已经接收消息但返回响应前连接中断，或进程在发送后落库前崩溃，重试可能重复一次。此处选择优先不漏通知。只允许一个发送进程使用同一状态目录。

## 部署到 Linux 服务器

部署链路：**本地提交 → GitHub → 服务器 `git pull --ff-only` → 测试 → systemd 重启**。不需要把 SSH 私钥或飞书地址放到 GitHub，也不依赖 GitHub Actions 连接国内服务器。

服务器需要 Python 3.9+、Git、systemd 和 `flock`。Rocky Linux 可先运行 `sudo dnf install -y git`，Ubuntu 可运行 `sudo apt-get install -y git`。已有 Python 无需 pip 安装依赖。

首次在服务器运行：

```bash
sudo git clone https://github.com/pjzck/codex-reset-radar.git /opt/codex-reset-radar
sudo install -d -m 755 /etc/codex-reset-radar
sudo install -m 600 /opt/codex-reset-radar/.env.example /etc/codex-reset-radar/radar.env
sudoedit /etc/codex-reset-radar/radar.env
sudo bash /opt/codex-reset-radar/scripts/deploy.sh
```

部署脚本首次复制配置到 `/etc/codex-reset-radar/config.json`，之后保留你的修改。服务以 systemd 动态非特权用户运行，开机自动启动，异常退出自动重启；不监听公网端口。

| 内容 | 服务器路径 |
| --- | --- |
| GitHub 代码副本 | `/opt/codex-reset-radar` |
| Webhook / 签名私密配置 | `/etc/codex-reset-radar/radar.env`，权限 600 |
| 行为配置 | `/etc/codex-reset-radar/config.json` |
| 去重数据库 | `/var/lib/codex-reset-radar/state.sqlite3`，由 systemd 管理 |
| systemd 服务 | `codex-reset-radar.service` |

### 修改代码后发布

在本地项目根目录提交改动，然后执行：

```powershell
git add <你改动的文件>
git commit -m "Describe the change"
.\scripts\publish-deploy.ps1
```

默认使用你本机 SSH 别名 `tx-cloud`。其他服务器可传 `-Server your-ssh-alias`。脚本先跑测试、要求已提交全部本地修改、推送 `main`，再通过 SSH 让服务器从 GitHub 拉取并部署；服务器存在未提交改动或分支不符时会停止，不会强制覆盖。

也可以手工执行：

```bash
git push origin main
ssh tx-cloud 'bash /opt/codex-reset-radar/scripts/deploy.sh'
```

### 运维

```bash
sudo systemctl status codex-reset-radar --no-pager
sudo journalctl -u codex-reset-radar -n 50 --no-pager
sudo journalctl -u codex-reset-radar -f
sudo systemctl restart codex-reset-radar
```

服务保持运行不等于数据源健康；查看日志中 `sources_ok=2 sources_failed=0` 或数据库 `health` 检查各站点是否成功。网站改版、网络错误或飞书拒绝发送时会在日志中记录错误，服务继续下轮重试。`.org` 每次仅获取最新一页，停机过久可能错过该页之外的公告。

## 开发与验证

```bash
python -m unittest discover -s tests -v
```

测试使用合成公告，不会发送真实消息。CI 在 Python 3.9 / 3.12 / 3.14 运行解析、跨站去重、重启持久化、失败重试、分类和签名测试。

## License

MIT © 2026 pjzck。站点与公告内容归各自权利人所有；本项目不隶属于 OpenAI 或两个数据源网站。
