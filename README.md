# Codex Reset Radar · Codex 重置雷达

监测两个非官方 Codex 重置追踪站点，将新公告发送到可配置的飞书 / Lark 群机器人。

Python 3.9+，**零第三方 Python 依赖**。区分重置预告、正在执行、执行确认与待核实信息；支持普通 / Banked 重置、跨站去重、SQLite 持久状态、飞书签名和 Linux systemd 部署。

## 数据来源与通知语义

| 来源 | 获取方式 | 处理方式 |
| --- | --- | --- |
| [codexresets.com](https://codexresets.com/) | 首页 HTML 的公告列表 | 使用机器时间戳、公告原文、状态标签和 X 原始链接；不使用预测概率判断重置 |
| [codex-resets.org](https://codex-resets.org/) | [`/api/resets`](https://codex-resets.org/api/resets) JSON，最近一页 | 保留普通 / Banked 类型和 `x_post` / `observed` 来源标注 |

这两个站点不是 OpenAI 官方接口。机器人报告的是**站点收录的信息**，不能验证个人额度是否到账。通知分别列出每个**采集网站的名称和网站链接**，再单独附上**原始 X 公告链接**；保留各站原文、原始标注、判断依据和北京时间（固定 UTC+8）。网站标注时间不等同于原始发布时间或实际执行时间。

通知首先展示**状态标题、中文内容摘要、预计重置时间、实际重置时间、状态变化、采集时间和核对结果**，再展示参考网站、链接及原文。所有生成的时间字段统一使用北京时间。站点记录时间放到参考信息中并注明“非重置时间”，不用于填补预计 / 实际时间。

中文摘要基于公告中可识别的重置动作、普通 / Banked 类型、适用用户或套餐、备用机会次数、小版本更新及故障背景提取要点；这是针对重置公告的规则摘要，不是通用全文翻译，不需要额外模型 API 或 Token。遇到不熟悉的表述不补充原文没有的信息。

重置时间仅从公告原文中提取：明确日期、时刻及 UTC / 数字偏移 / PST / PDT / PT 等时区会换算到北京时间；“明天”“3 小时后”等相对表述使用原始 X 公告 ID 中的创建时间作为基准，不以抓取时间或站点记录时间代替。X ID 依据 [X 官方说明](https://docs.x.com/fundamentals/x-ids) 和 [Snowflake 原始实现](https://github.com/twitter-archive/snowflake/blob/snowflake-2010/src/main/scala/com/twitter/service/snowflake/IdWorker.scala) 解码。

没有明确重置时间就显示 **待确认**；只有“Done”不能当成公告发布时刻执行。日期 / 时区缺失、CST 等时区歧义、两站给出不同时间、PT 夏令时重叠或不存在的时刻均不猜测。PST 按 UTC−8、PDT 按 UTC−7；PT 使用系统 IANA 时区库按日期处理夏令时，缺少时区数据时显示待确认。仅给出“几小时内”“当天结束前”等含糊范围也保留待确认。时区机制参见 [Python zoneinfo 文档](https://docs.python.org/3/library/zoneinfo.html)。

| 通知状态 | 判定与含义 |
| --- | --- |
| 🕒 重置预告 | 来源明确预告，或原文描述未来计划；到点也不会自动变成已执行 |
| 🔄 正在执行 | 原文描述正在重置、发放或逐步生效；不等于所有账户完成 |
| ✅ 已执行（公告确认） | 原始公告有明确完成表述，例如已经重置、完成、全部生效 |
| 🔎 执行待确认 | 只有站点观测、模糊措辞或缺少执行依据 |
| 💭 可能重置信号 | 来源明确标为可能 / 未确认信号 |
| ⚠️ 来源状态存在分歧 | 同一公告在两站的预告 / 执行结论或重置类型不一致 |

`.org` 的 `observed` 记录不会默认当作执行确认；无 `status` 字段也不会默认已执行。`.com` 即使标为 `OPERATOR CONFIRMED`，原文仍是未来计划时仍归预告；`DELIVERED AS SCHEDULED` 也不能仅凭到点视为执行。原文分类使用保守的已知措辞规则；不认识的新表述保留为待确认，可能需要后续维护规则。

同一 X 公告 ID、同一阶段只发一次；预告随后获得执行证据，会发送“状态更新”。一个来源发现即可通知，不强制等待双站；另一站稍后收录只记录佐证，不重复发消息。双站一致只表示采集结果一致；有分歧时合并为一条待核实通知，保留双方信息。来源故障会在可用来源的通知中注明。不同 X 公告仍视为不同事件，无 X 链接的记录按站点 ID 去重。

升级时保留旧去重记录，先静默建立新分类基线，不重播历史；旧版误把观测当执行的记录，不会阻止之后真正出现的执行证据。每站最新观察和成功通知的阶段保存在 SQLite，重启后继续使用。轮询只读取公开页面，不需要 Codex Token、X 账号或浏览器。

### RSS 与订阅

2026-10-24 检查结果：两个站点均未发现公开 RSS / Atom / JSON Feed；首页没有 Feed 自动发现链接，`/rss.xml`、`/feed.xml`、`/atom.xml`、`/feed.json` 均返回 404，站点地图也没有订阅源。

`.com` 有[邮件订阅入口](https://codexresets.com/#subscribe)，需要邮箱确认；不是机器推送 API。`.org` 的 `/api/resets` 返回 JSON，响应为 `Cache-Control: no-store, must-revalidate`，适合定时检查。目前采用每轮结束后等待 60 秒再检查；`.com` 首页响应还声明 `max-age=60, s-maxage=120`，实际通知延迟包括网站自身同步、共享缓存和本机器检查间隔，不能承诺秒级到账。上述为公开入口的检查结果，不代表不存在未公开接口。

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

# 发送一条包含“预告 / 已执行”两种合成演示的消息，不改变去重状态
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
| `poll_interval_seconds` | `60` | 每轮结束后等待秒数，最小 30 秒 |
| `request_timeout_seconds` | `20` | 单次网络请求超时 |
| `state_path` | `data/state.sqlite3` | 持久去重数据库；`RADAR_STATE_PATH` 可覆盖 |
| `first_run` | `"latest"` | 首次每个来源只通知最新且在时效内的一条；`"silent"` 只建立基线 |
| `max_event_age_hours` | `72` | 超过时效的历史记录不发送，防止首次运行或断线恢复刷屏 |
| `notify_possible` | `true` | 是否通知预告、进行中和待确认信息；`false` 只发送有执行完成依据且无分歧的记录 |

初次只推送最新记录，其余建立历史基线；已有状态时推送所有尚未处理且在时效内的记录。标注时间在未来的预告可以先通知，执行确认时间超过当前时间 5 分钟的记录保留到后续检查。一个来源失败不会阻止另一个；结构异常不会建立空基线。GET 请求对临时网络错误最多重试三次；飞书失败下轮重试，只有 HTTP 成功且飞书业务码为 0 才记为已发送。

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

自动化测试使用合成公告并模拟飞书发送，不会发送真实消息。`test-message` 是主动发送的格式验收命令，消息内醒目标明“格式演示 · 非真实重置通知”，不写任何通知或去重状态。CI 在 Python 3.9 / 3.12 / 3.14 验证预告到点不升级、执行证据升级、站点来源链接、跨站分歧、迟到佐证去重、旧状态迁移、重启持久化、失败重试和签名。

## License

MIT © 2026 pjzck。站点与公告内容归各自权利人所有；本项目不隶属于 OpenAI 或两个数据源网站。
