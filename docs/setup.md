# Octopus 安装与入口配置指南

按顺序做即可。所有命令都在项目目录里用 `uv run` 运行。数据默认放在 `~/Octopus`，密钥放在 macOS 钥匙串（Linux / Docker 上是环境变量或 `~/Octopus/secrets.toml`，见 [README](../README.zh-CN.md)「在 Linux / Docker 上运行」），不会写进配置文件。

## 先看两条原则

- **`allowed_users` 里只能放你自己的账号。** 白名单之外的人发来的消息会被直接忽略；而系统通知（比如微信 ClawBot 需要重新扫码时的二维码）会发给白名单里的每一个账号。放了别人，他就会收到你的登录二维码。
- **至少启用一个入口**，`octopus run` 才能启动。各个入口互相独立，哪个出问题都不影响其他的。

## 1. 初始化

```bash
uv run octopus init --language zh
```

会生成中文的 `~/Octopus/config.toml`（不写 `--language` 就按系统语言：中文系统用中文，其他用英文）。用编辑器打开它，后面几步都要改这个文件。

## 2. Claude 凭证

整理和问答用你的 Claude 订阅：

```bash
claude setup-token            # 按提示登录，会打印一个长期凭证
uv run octopus secret set claude_oauth_token   # 粘贴刚才的凭证（输入时不显示）
```

之后可以用 `uv run octopus secret list` 查看哪些密钥已设置。

## 3. Telegram

1. 在 Telegram 里找 @BotFather，发送 `/newbot`，按提示起名，拿到 token。
2. 保存 token：`uv run octopus secret set telegram_token`
3. 给你的机器人随便发一条消息，再找 @userinfobot 查你自己的数字 ID。
4. 编辑 `config.toml`：

```toml
[channels.telegram]
enabled = true
allowed_users = ["123456789"]   # 换成你的数字 ID，只放你自己的账号
```

## 4. 飞书

飞书控制台的菜单名称会变，下面的名称**以当前控制台为准**，找意思相近的入口即可。

1. 打开飞书开放平台，创建**企业自建应用**。
2. **添加应用能力**，选“机器人”。
3. **权限管理**，开通这四项：`im:message`、`im:message.p2p_msg:readonly`、`im:resource`、`im:message:send_as_bot`。
4. 在“凭证与基础信息”里复制 App ID 和 App Secret，保存到钥匙串：

```bash
uv run octopus secret set feishu_app_id
uv run octopus secret set feishu_app_secret
```

5. 在 `config.toml` 里设置 `[channels.feishu] enabled = true`，然后**先启动一次** `uv run octopus run`（长连接必须在运行时，控制台才允许你保存下面的订阅方式）。
6. 回到控制台：
   - **事件与回调 › 事件配置 › 订阅方式**：选“使用长连接接收事件”，**添加事件**：接收消息 v2.0。
   - **事件与回调 › 回调配置**：选“使用长连接接收回调”，**添加**：卡片回传交互。
7. **版本管理与发布 › 创建版本 › 发布**（企业自建应用通常自动通过）。
8. 在飞书里搜到你的机器人，给它发一条消息。
9. 这条消息会被忽略（你还没在白名单里），但日志会记下发送者 ID：

```bash
grep "ignoring message" ~/Octopus/logs/octopus.log
```

   复制你的 open_id（形如 `ou_xxxx`），填进配置：

```toml
[channels.feishu]
enabled = true
allowed_users = ["ou_xxxxxxxx"]   # 只放你自己的账号
```

10. 重启 `octopus run`，再发一条消息试试。

## 5. 微信 ClawBot

1. 确认你的微信里有 ClawBot 插件（我 → 设置 → 插件 → 微信 ClawBot）。
2. 登录：

```bash
uv run octopus clawbot-login
```

   终端会打开二维码图片，用微信扫码；如果手机上弹出验证码，按提示输入。
3. 编辑 `config.toml`：

```toml
[channels.clawbot]
enabled = true
```

   扫码的那个微信账号会自动被当作主人，不需要写 `allowed_users`。
4. 注意：**ClawBot 只能在你先给它发消息之后才能回复你。** 保存结果和通知要等你发了消息才发得出去；每隔一段时间不发消息，窗口会过期，下次你发消息时它会把没发出去的结果补上。
5. 如果服务正在运行时需要重新登录，二维码会通过你的其他入口（Telegram / 飞书）发给你；也可以先停掉服务，再运行 `octopus clawbot-login`。服务运行期间 `clawbot-login` 会拒绝执行，因为两个进程不能同时用同一个账号。错过了二维码、不想等下一张时，在 Telegram / 飞书里发 `/clawbot`，马上会生成一张新的发给你。
6. 只用 ClawBot、没有其他入口时，二维码没地方推送：它保存在数据目录下的 `clawbot-qr.png`（默认 `~/Octopus/clawbot-qr.png`，用 `open ~/Octopus/clawbot-qr.png` 打开扫码，有效约 8 分钟），同时日志里会记一行“微信 ClawBot 需要重新扫码：二维码在 …”（`grep 重新扫码 ~/Octopus/logs/octopus.log`）。没扫上的话，15 分钟、30 分钟、1 小时、2 小时后各换一张新二维码，之后每 3 小时一张。

## 6. Discord

详细说明见 [README](../README.zh-CN.md)「Discord」。

1. [Discord 开发者后台](https://discord.com/developers/applications) →「New Application」→ 左边「Bot」→「Reset Token」拿到 token。「Privileged Gateway Intents」一项都不用打开。
2. 保存 token：`uv run octopus secret set discord_bot_token`（Linux / Docker 也可以用环境变量 `OCTOPUS_DISCORD_BOT_TOKEN`）。
3. 「OAuth2」→「URL Generator」，Scopes 勾 `bot`，用生成的链接把机器人加进一个你自己的服务器。
4. Discord「用户设置」→「高级」打开「开发者模式」，右键自己的头像 →「复制用户 ID」。
5. 编辑 `config.toml`：

```toml
[channels.discord]
enabled = true
owner_user_ids = ["123456789012345678"]   # 只放你自己的用户 ID
```

6. 重启后在服务器成员列表里点机器人 →「发送消息」，私信它一句"你好"。只有私信会被读，服务器频道里的消息不理。

## 7. 邮件（只存不聊）

详细说明（会存什么、怎么防冒充）见 [README](../README.zh-CN.md)「邮件（只存不聊）」。收件邮箱请用 Gmail；QQ 邮箱、Outlook / Hotmail 不支持。

1. 新建一个专门的 Gmail 邮箱，开两步验证，在 Google 账号「安全性」→「应用专用密码」生成一个。
2. 保存密码：`uv run octopus secret set email_password`（Linux / Docker 也可以用环境变量 `OCTOPUS_EMAIL_PASSWORD`）。
3. 编辑 `config.toml`：

```toml
[channels.email]
enabled = true
imap_host = "imap.gmail.com"
smtp_host = "smtp.gmail.com"
username = "my-octopus-box@example.com"
owner_addresses = ["me@example.com"]   # 你自己平时发邮件的地址
reply = "email"                         # 或 "telegram"、"none"
```

4. 重启后从你自己的地址往这个邮箱发一个链接，一分钟内会收到回信。

## 8. 启动

```bash
uv run octopus run
```

看到“Octopus 正在运行：telegram, feishu, clawbot”就好了。Ctrl-C 退出，它会等正在处理的任务收尾。入口断线会自动重连（间隔从 5 秒逐步加长，最长 5 分钟）。同一时间只能有一个 Octopus 在运行，已经有一个时 `octopus run` 会报错退出。

也可以先在终端里试用：`uv run octopus chat`。

### 装成后台服务

要让入口一直在线，运行 `uv run octopus service install`。其他命令：

- `service status`：查看状态。
- `service restart`：重启（改了配置或密钥后用）。
- `service stop`：现在先停下来。服务还装着，**下次登录时会自动再启动**。
- `service start`：把停掉的服务再启动。
- `service uninstall`：停止并卸载，之后不会再自动启动。

要注意：

- **改了配置或密钥后要重启**：编辑 `config.toml` 或运行 `secret set` 之后，后台服务不会自动读取新内容，运行 `uv run octopus service restart`（等于重新安装）。
- **服务运行时，`chat` / `reindex` / `clawbot-login` 会拒绝执行**（同一时间只能有一个 Octopus 在用数据）。先 `uv run octopus service stop` 停掉服务（它还要几秒收尾，稍等再运行），做完再 `service start`（`uninstall` 也行，但做完要重新 `service install`）；或者直接用 Telegram / 飞书 / 微信聊天。
- **登录后才启动，不是开机就启动**：它是用户级的 LaunchAgent，重启电脑（FileVault 开启时，或系统更新后的重启）后要先登录，服务才会起来。`service install` 必须在已登录的桌面会话里运行，不要通过 SSH 运行。
- **搬动仓库、`uv python upgrade` 或重建 `.venv` 之后要重新运行 `service install`**，因为服务记录的是当时的 Python 路径；之后第一次读取密钥时钥匙串可能再次弹窗，选“始终允许”。
- `service status` 会显示运行状态、上次退出码和两个日志文件的路径。

## 9. 常见问题

**钥匙串弹窗**：第一次读取密钥时 macOS 会问是否允许访问钥匙串。请选“**始终允许**”，否则每次启动（尤其是后台运行时）都会卡在弹窗上。如果误点了“拒绝”，打开“钥匙串访问”，找到名为 `octopus` 的条目，在“访问控制”里把 Python 加进去。

**YouTube 打不开 / 被拦截**：YouTube 经常拦截不登录的访问。在 `config.toml` 的 `[media]` 里设置 `youtube_cookies_from_browser = "chrome"`（或 `"safari"`），Octopus 会读取该浏览器里的 YouTube 登录状态。不想让它读浏览器就保持留空。

**“⚠️ 没能获取内容”**：说明这个链接抓不到正文（需要登录、反爬、或者平台不让访问）。条目仍然会占位保存。请把截图、录屏或粘贴的原文直接发过来，Octopus 会合并到同一条里。

**入口没反应**：先看 `uv run octopus run` 启动时打印的提示（缺密钥、没启用）。再看日志 `~/Octopus/logs/octopus.log`。

## 10. 快捷指令导入不了时：手动搭建

双击 `.shortcut` 文件打不开时，可以在「快捷指令」App 里自己搭（先按 [README](../README.zh-CN.md)「Chrome 插件」第 1–3 步打开 `[channels.extension]`、生成密码、重启服务）：

1. 「快捷指令」App → 文件 → 新建快捷指令，命名为「存到 Octopus（网页）」。
2. 右边 ⓘ「快捷指令详细信息」：勾上「在共享表单中显示」；最上面的输入一行选：从「共享表单」接收「Safari 网页」和「URL」。
3. 添加「文本」，粘贴密码（`uv run octopus extension token --copy`）。
4. 添加「文本」，填 `http://127.0.0.1:8765/save`。
5. 添加「从输入中获取 URL」（输入：快捷指令输入）。
6. 添加「获取 URL 内容」：URL 选第二个「文本」（地址）；方法 POST；头部：键 `Authorization`，值 `Bearer ` 加第一个「文本」；请求体 JSON：`kind`（文本）= `page`，`url`（文本）= 第 5 步的「URL」。
7. 添加「获取词典值」：从「URL 的内容」里取键 `id`。
8. 添加「如果」「词典值」「有任何值」→「显示通知」Octopus 已收到，正在存；「否则」→「显示通知」没存上。
9. 选中文字的那个：命名为「存到 Octopus（选中文字）」，详细信息里勾上「用作快速操作」和「服务菜单」，接收「文本」；跳过第 5 步；请求体 JSON 的 `kind` = `selection`，`text` = 「快捷指令输入」。
