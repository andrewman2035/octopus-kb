# Octopus

[English](README.md) · 简体中文

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
![macOS (Apple Silicon) · Linux · Docker](https://img.shields.io/badge/runs%20on-macOS%20%28Apple%20Silicon%29%20%C2%B7%20Linux%20%C2%B7%20Docker-lightgrey.svg)

**你自己的 AI 知识库。在常用的聊天软件里把链接、截图、文件、语音发给它，它会读完、转写、总结成 Markdown 笔记，存在你自己的电脑上；以后问它问题，它从你存过的内容里找答案，并标出来源。**

```text
你：      https://www.bilibili.com/video/BV…        （在微信 / Telegram / 飞书里发给它）
Octopus： 收到，处理中…
Octopus： ✅ 已保存：指数基金到底怎么赚钱
          ……一小段摘要……
          #投资 #指数基金
          id：20261013-7f3a2c

  …… 一周后 ……

你：      之前那个讲长期投资的视频说了什么？
Octopus： 视频的观点是……[1]

          来源：
          [1] 指数基金到底怎么赚钱（id：20261013-7f3a2c） https://www.bilibili.com/video/BV…
```

## 为什么用 Octopus

- **在哪儿看到就在哪儿存。** 微信（ClawBot）、Telegram、飞书、Discord、邮件转发、Chrome 插件、Mac 快捷指令、终端都行。不用多装一个 App，转发给机器人就存好了。
- **很多工具只存个链接的，它能存下内容。** 公众号文章存全文；B站、YouTube 和 X 的视频、小宇宙等播客、语音消息都会转写成文字；截图会识别文字。小红书、知乎、Reddit 这类要登录才能看的页面，用 Chrome 插件在你自己登录好的浏览器里一键存，什么时候需要这么做，Octopus 会提醒你。
- **笔记在你自己手里。** 每条都是一个普通的 `.md` 文件，带摘要、要点和标签。整个文件夹可以直接放进 Obsidian，不锁在任何数据库或云账号里。
- **问的是你自己的笔记。** 回答先查你存过的内容，并标出 [1] [2] 来源；没存过相关内容会直说。
- **模型自己选。** 用你的 Claude 订阅（通过 Claude Code）、API key、任何兼容 OpenAI 的服务，或者用 Ollama 完全在本地跑。
- **免费开源**（MIT）。macOS（Apple 芯片）、Linux、Docker 都能跑，Windows 可以用 WSL2。

| | Octopus | 常见的云端稍后读 / AI 笔记应用 |
|---|---|---|
| 笔记存在哪 | 你电脑上的 Markdown 文件 | 厂商的服务器 |
| 怎么存 | 平时用的聊天软件、浏览器、邮件 | 它自己的 App 或插件 |
| 视频、播客、语音 | 转写并总结 | 常常只存个链接 |
| 公众号、B站、小宇宙等中文平台 | 内置支持 | 很少支持 |
| 模型 | 自己选，可以全本地 | 它定的 |
| 价格 | 免费，只为你用的模型付费（全本地就不花钱） | 订阅制 |

## 环境要求

- macOS，Apple Silicon（语音转写和 OCR 用到 Apple 芯片上的 MLX 和系统自带的识别能力）。Linux 和 Docker 也可以，见「在 Linux / Docker 上运行」
- [uv](https://docs.astral.sh/uv/)（Python 环境和依赖管理）
- [Ollama](https://ollama.com/)，并拉取两个模型：`ollama pull qwen3.5:35b-a3b`（本地备用模型）和 `ollama pull bge-m3`（向量检索）
- ffmpeg：`brew install ffmpeg`
- 已登录的 Claude Code（`claude` 命令可用）：整理和问答默认用你的 Claude 订阅

## 安装

```bash
git clone https://github.com/andrewman2035/octopus-kb.git && cd octopus-kb
uv sync
```

## 第一次运行

```bash
uv run octopus init --language zh     # 生成中文的 ~/Octopus/config.toml
uv run octopus chat                   # 在终端里直接试用
```

在 `chat` 里先发 `hi`，会看到使用说明。贴一个链接，或者发 `/s 一句话`，Octopus 会回复“收到，处理中…”，处理完发来保存结果；之后直接问一个问题，它会根据已保存的内容回答。`/quit` 退出。

想用手机或电脑上的聊天软件，见下面的“入口”。

### 语言

`octopus init` 新建配置时，不写 `--language` 就按系统语言选：中文系统写 `language = "zh"`，其他（包括没有设置语言的 Docker 容器）写 `"en"`，并说明选了哪个、怎么改。Mac 上看「系统设置」里排第一的首选语言，其他系统看 `LANGUAGE` / `LC_ALL` / `LC_MESSAGES` / `LANG`（粤语也算中文）。想要中文就写 `--language zh`（上面和下面 Linux、Docker 的命令都写了）。以后要换：改 `config.toml` 里的 `language`，再重启后台服务；`init` 不会改已有的配置文件。其他会新建配置的命令（还没有配置时的 `service install`、`library move`）也这样选；还没有配置文件时（`--help`、出错提示、不带配置运行的命令）也说系统的语言。没有 `language` 这一行的配置文件仍然是中文，和以前一样。

`config.toml` 顶层的 `language = "zh"` 或 `"en"` 决定 Octopus 说话用的语言：回复、按钮、Telegram 菜单、提示、命令行和新笔记的标题行（`## 摘要` / `## Summary` …）。发来的命令和文字中英文都认（`存`/`save:`、`问`/`ask`、`好了`/`done`、`确认`/`yes`……；英文的存要写 `save:` 或 `save,`）。已有的笔记保持原来的标题行，往里补充的内容也用它原来的那套，两种都能读。

摘要和要点默认和 `language` 一样（中文配置一直写中文摘要，和以前一样）；`summary_language = "en"` 一直用英文，`"auto"` 按原内容的语言写（英文文章写英文摘要）。回答用你提问的语言：中文配置下只要问题里有一个汉字就按中文回答。

## 日常使用

**一句话规则：普通文字就是聊天，想存文字要加 `/s`。** 链接、图片、文件会自动保存。

### 怎么存

| 你发的内容 | Octopus 做什么 |
|---|---|
| 链接（网页、公众号、YouTube、B 站、X、播客、PDF 等） | 自动抓取正文或转写，保存成笔记 |
| X 帖子 | 先存文字，再把帖子里的视频转成文字（最多 4 个）；长推文要拿全文需设置 `x_full_text_service`（见常见问题） |
| 截图 / 图片 | 自动 OCR 识别文字后保存 |
| 文件（PDF 等）、音频、视频 | 自动提取文字或转写后保存 |
| `/s 内容`（也可以 `/save 内容`、`存 内容`、`存：内容`） | 把这段文字存成笔记，例：`/s 明天给老王回电话` |
| 语音 | 先转成文字，当成你打的字处理。想存就说“存，……” |

### 哪些平台要用插件

有些平台不登录就看不到内容，Octopus 按链接去读只拿到登录页、403 或一小段。这些在电脑上用 Chrome 打开（需要的话先登录），用 Chrome 插件存（见「Chrome 插件」）。在聊天里发这些链接没读全时，回复里会多一行说怎么做；之后用插件存同一个页面，会补全之前存的那一条（id 不变；条件见「Chrome 插件」）。

| 平台 | 怎么存 |
|---|---|
| 公众号文章、YouTube、B 站、播客（小宇宙、Apple 播客、音频链接）、X、普通网页、PDF | 直接发链接 |
| Reddit、小红书、知乎（含专栏）、Instagram、Facebook、LinkedIn 动态（公开文章一般直接发链接就行）、Threads、Medium（会员文章）、微博 | 在电脑上用 Chrome 登录并打开页面，点插件的「存这个页面」 |
| 抖音、TikTok、小红书的视频 | 视频下载不了；插件一般只能存到页面上的文字（标题、描述），要内容就发截图或录屏 |
| 视频号 | 插件也读不了：发截图、录屏或粘贴原文 |

### 怎么聊天

除了上面的情况，其他文字都是和 AI 聊天。Octopus 会先查你的知识库再回答，并列出来源。

- 知识库里没有相关内容时，它也会用自己的知识回答，开头会写“（知识库里没有相关内容，以下是一般性回答）”，不列来源。
- 回答下面有【存下来】按钮（微信 ClawBot 回“存”），会把你的问题和它的回答一起存成一条笔记。
- 存成笔记的回复下面有【改成提问】按钮（微信 ClawBot 回“问”）。按钮 7 天内有效；打字回的“存”“问”只对 10 分钟内的上一条有效，而且“存”要紧跟在那条回答后面。
- 笔记加过备注或改过标题之后，就不能再“改成提问”了（免得把你后来写的东西一起删掉）。
- 发 `hi`、`你好`、`在吗` 或 `/help`，会看到使用说明。

### 在聊天里管理笔记

直接用中文说就行：

| 你说 | Octopus 做什么 |
|---|---|
| 我今天存了什么？ / 我存过哪些关于 AI 的？ | 列出匹配的笔记（id 加标题） |
| 删掉上面这条 / 删除 20261013-73b48c | 删除（放进回收目录，不会直接抹掉） |
| 把上面那条标题改成…… | 改标题 |
| 给这条加个备注：…… | 写进笔记的“我的备注” |
| 那条重新总结一下 | 重新生成摘要 |

删、改、重新总结之前，Octopus 会先问你。点【确认】才会执行（微信 ClawBot 回“确认”或“取消”；“好”“行”“嗯”“ok”这类也算确认）。10 分钟内有效。

打字回“确认”只在这个问题还是最后一条回复时有效：
- 如果中间插进来别的回复（比如刚好有一条保存完成），Octopus 会把问题再问一次，再回一次“确认”就行。
- 如果你中间问了别的事，“确认”会被当成普通聊天。这时点之前的【确认】按钮，或者把要做的事再说一次。

### 命令

在聊天里发这些命令（终端里的 `chat` 和各个入口都一样）。Telegram 输入框旁有命令菜单；使用说明下面有按钮，Telegram 和飞书可以直接点。

| 命令 | 作用 |
|---|---|
| `/s <内容>` | 把文字存成笔记（`/save` 一样） |
| `/recent` | 最近保存的 10 条 |
| `/read <id> [K]` | 在聊天里看这条的全文，从第 K 段起；长的一次发 5 段（`config.toml` 里的 `read_batch_parts`），最后一条下面有【继续（6/38）】和【整篇发文件】按钮（微信回 `/read <id> K` 接着看） |
| `/read <id> 文件`、`/read <id> 全部` | 整篇发成 Markdown 文件 `<标题>-<id 后 6 位>.md`（Telegram、Discord、飞书；微信发不了文件，太大也发不了，就改成分段发）；一次全发完。英文 `file`、`all` 也认；不写 id（`/read 文件`）就是当前这条 |
| `/delete <id> [<id> …]` | 删除一条或多条，id 之间用空格或逗号隔开，例：`/delete id1 id2,id3`（放进回收目录）。`/del` 一样 |
| `/delete`（不带 id） | 列出最近 10 条，每条一个删除按钮。点了还会再问你确认 |
| `/resummarize <id>` | 重新生成摘要 |
| `/model` | 查看当前各环节用的模型 |
| `/model local` | 全部切到本地模型（在国内时用） |
| `/model default` | 恢复默认模型 |
| `/status` | 运行状态 |
| `/clawbot` | 立即生成新的微信 ClawBot 登录二维码 |
| `/help` | 显示使用说明 |

## 模型：订阅、API key 或完全本地

整理（摘要）和问答用的模型在 `config.toml` 的 `[models.summarize]`、`[models.answer]` 里设置：`primary` 是首选，`fallback` 是首选失败时用的（留空表示没有备用）。可以混着用，比如首选 Anthropic API、备用本地模型。向量检索始终用本机 Ollama 的 `bge-m3`（暂不支持在线的向量服务）。

**1. Claude 订阅（默认）**：用已登录的 Claude Code，不需要 key。

```toml
[models.summarize]
primary = "claude:haiku"
fallback = "local"

[models.answer]
primary = "claude:sonnet"
fallback = "local"
```

**2. API key**：没有 Claude 订阅，或者在 Linux / Docker 上运行时用。

Anthropic API 写 `anthropic:haiku`、`anthropic:sonnet`、`anthropic:opus`，或直接写完整的模型 ID（如 `anthropic:claude-sonnet-5-5`）：

```toml
[models.answer]
primary = "anthropic:sonnet"
fallback = "local"
```

任何 OpenAI 兼容的服务（DeepSeek、通义千问的兼容模式、OpenRouter、LM Studio……）：先在 `[providers.<名字>]` 里写地址和模型，再用 `openai:<名字>`。名字只用小写字母、数字、`-` 和 `_`。

```toml
[providers.deepseek]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"

[models.summarize]
primary = "openai:deepseek"
fallback = "local"
```

key 不写进配置文件：

- macOS：存进钥匙串，`uv run octopus secret set anthropic_api_key`；`[providers.deepseek]` 的 key 是 `uv run octopus secret set deepseek_api_key`（`<名字>_api_key`）。后台服务读不到终端里的环境变量，所以在 Mac 上请用钥匙串。
- Linux / Docker：环境变量 `ANTHROPIC_API_KEY`；`[providers.deepseek]` 是 `OCTOPUS_DEEPSEEK_API_KEY`（`OCTOPUS_<名字大写，- 换成 _>_API_KEY`）。
- 两处都有时用环境变量。key 在每次调用时读取，换了钥匙串里的 key 不用重启；改了 `config.toml` 仍要 `service restart`。本机的服务（如 LM Studio）不需要 key。

`uv run octopus models` 列出各环节用的模型、每个 API 的 key 是否已设置、在哪里（不显示 key 本身；`service status` 也会列出）。`uv run octopus models test` 给每个模型发一句很短的话，看能不能用（API 会产生一点点费用）。服务繁忙（HTTP 429、500、502、503、504、529）时会等一会儿再试，最多 3 次；key 不对会直接说明，然后改用 `fallback`。用 API key 时，整理和问答的内容会发给你选的服务。

**3. 完全本地**：只用本机 Ollama，内容不出本机。

```toml
[models.summarize]
primary = "local"
fallback = ""

[models.answer]
primary = "local"
fallback = ""
```

本地模型在 `[local_llm]` 里设置。聊天里发 `/model local` 也可以临时全部切到本地模型，`/model default` 恢复。

## 入口

- 聊天和存东西：Telegram、飞书、微信 ClawBot、Discord（见下面「Discord」）。
- 只存不聊：邮件（见「邮件（只存不聊）」）、Chrome 插件（见「Chrome 插件」）、Mac 上的快捷指令（Safari 和其他 App，见「Safari 和其他 App（快捷指令）」）。

微信 ClawBot 每 8 分钟最多发 3 条，所以 `/read` 在微信里最多发 3 段，最后一条说怎么接着看；它发不了文件，`/read <id> 文件` 在微信里也是分段发。

各个入口互相独立，至少启用一个，后台服务才能启动。每个入口的配置步骤（创建机器人、保存密钥、白名单、扫码）见 **[docs/setup.md](docs/setup.md)**。

## Chrome 插件

在 Mac 上用 Chrome 看到的网页、选中的文字、图片，点一下就存进 Octopus。发的是你眼前看到的内容，所以 X、小红书、Reddit、要登录或付费才能看的页面也能存（不用 Octopus 再去打开链接；X 帖子例外：Octopus 还会按链接去拿视频和全文，拿不到时用你看到的页面）。哪些平台要这样存，见「哪些平台要用插件」。插件只和本机的 Octopus 说话（`127.0.0.1`），后台服务要在运行。

安装：

1. 在 `~/Octopus/config.toml` 里打开：

   ```toml
   [channels.extension]
   enabled = true
   port = 8765
   ```

2. 生成插件的密码（第一次运行时生成，存在钥匙串里；以后再运行显示同一串）：`uv run octopus extension token`
3. 重启后台服务：`uv run octopus service restart`
4. Chrome 打开 `chrome://extensions` → 右上角打开「开发者模式」→「加载已解压的扩展程序」→ 选仓库里的 `extension/chrome` 文件夹。
5. 点插件的「详细信息」→「扩展程序选项」（或插件弹窗右上角的「设置」），把第 2 步的密码粘贴进去（设置页上也写着这条命令），点「保存」，看到"已连上 Octopus"就好了。

几种存法：

- **存整页**：点工具栏上的 Octopus 图标（可以先把它固定到工具栏），再点弹窗最上面的「存这个页面」；只打开弹窗不会存任何东西。点了之后弹窗显示"已收到"，按钮下面是最近 10 次插件存的东西和状态（处理中 / 已存 / 失败），存好后显示标题和一句话摘要。YouTube、B站、播客这类链接照旧按链接存（下载转写）；页面上的正文很短时，Octopus 再按链接试一次，拿到的更多就用链接的，否则保留页面上的那段（Reddit 这类没法自动读的平台直接保留）；只是"登录后查看"这类提示的不算内容。这样存的列表里显示"已存（内容不完整）"。这个链接以前存过、但没读到或只读到一部分时（比如在手机上发的链接），存这个页面会补全那一条（id 不变），列表里写"补全了之前存的那一条"（补了还不完整时写"更新了之前存的那一条"）。条件：页面读到的比原来多；那一条还没补全（发过截图或文字补全了的，照旧算"已经存过"）；YouTube、B站、播客这类按链接存的不会这样补。你用 /title 或在 Obsidian 里改过的标题、在 Obsidian 里改过的正文都保留（这个版本起存的条目；更早存的，改过的标题补全时会换成页面的标题）：正文改过时，页面的内容加在最后「## 页面内容（插件）」下面；补充的截图和文字、/note 也都在。手机上分享的链接和电脑上打开的地址不一样时：小红书（xhslink.com / xhslink.cn 短链接）、Threads、Octopus 读到过的跳转，都能认出是同一条；页面自己写的正式地址（canonical）只用来补全正好存在这个地址下、还没补全的那一条，不会因此算"已经存过"，也不认指向上一级（比如栏目页）的；Reddit App 分享的 `/s/` 短链接认不出（Octopus 不读 Reddit），微博手机版和电脑版地址里的编号不同时也认不出，这时会另存一条新的。这个标签页 10 分钟内已经用插件存过东西（整页、选中的文字、图片都算；整页存失败的不算）时，按钮变成「已存过，再存一次」，点了照样存；页面不能存（`chrome://`、新标签页）时按钮是灰的，下面写着「这个页面不能存」。页面超过 15 MB 时只发链接，按链接存。
- **快捷键**：按 Alt+Shift+S（Mac 上是 Option+Shift+S）直接存当前页面，不打开弹窗，结果显示在图标的小角标上（✓ 或 !）。这个标签页 10 分钟内存过东西时不再发送，角标是灰色的 ✓，鼠标停在图标上会写「10 分钟内存过：没有再发」。插件的设置页最下面显示 Chrome 实际设上的键；快捷键可以在 `chrome://extensions/shortcuts` 里改（别的扩展已经占用 Alt+Shift+S 时 Chrome 不会设上，在那里自己设一个）。
- **存选中的文字**：选中文字 → 右键「存到 Octopus」。存成一条笔记，来源是这个网页，不会被当成提问。
- **存图片**：在图片上右键「存到 Octopus」。图片会识别文字（OCR）后存成一条，来源是这个网页。

在页面空白处右键也有「存到 Octopus」，和「存这个页面」一样存整页。右键存的结果显示在图标的小角标上（✓ 或 !），点开图标能看到列表。

默认不在聊天里发消息。设置里勾上「同时发到 Telegram」后，存好或失败时 Telegram 也会收到和平时一样的那条消息（Octopus 里没有配置 Telegram 时设置页会提示，勾了也不会发）。微信和飞书永远不会收到插件存的消息。

插件的语言默认跟着 Chrome 的语言（中文 Chrome 是中文，其他是英文）；在插件的设置里可以改成「中文」或「English」。更新仓库（`git pull`）之后，到 `chrome://extensions` 里点 Octopus 的刷新按钮（↻），插件才会用上新版本。

出错时插件会说：「请先设置：在设置里粘贴密码」（还没填密码：弹窗里有生成密码的命令和「打开设置」按钮）、「Octopus 没在运行」（后台服务没开）、「密码不对」（重新运行 `uv run octopus extension token --copy` 复制一次，粘贴到设置里）、「内容太大」（选中的文字或图片超过 15 MB）。

安装时 Chrome 会说插件要"读取和更改您在 127.0.0.1 上的数据"：这是插件和本机 Octopus 说话所需要的（Chrome 的本地网络访问限制），它不能读任何网站。

## Safari 和其他 App（快捷指令）

用 Mac 自带的「快捷指令」，在 Safari 里点「共享」就能存整页，在任何 App 里选中文字、右键就能存选中的文字。它们和 Chrome 插件用同一个本机入口，所以先按上一节的第 1–3 步打开 `[channels.extension]`、生成密码、重启后台服务（不用装 Chrome 插件）。

安装：

1. 先用你自己的 Apple 账号生成并签名快捷指令：`uv run python extension/shortcuts/build.py`（只要中文版加 `--lang zh`）。签过名的 `.shortcut` 文件带着签名者的证书，所以公开版不附带。不需要付费的 Apple 开发者账号：`build.py` 用的是 `shortcuts sign --mode anyone`，只要这台 Mac 登录了 Apple ID（iCloud）就能签。没有登录 Apple ID 时，照 [docs/setup.md](docs/setup.md) 第 10 节（英文版：[docs/shortcuts-manual.md](docs/shortcuts-manual.md)）在「快捷指令」App 里自己搭：自己在「快捷指令」App 里建的快捷指令不需要签名。然后在终端运行 `uv run octopus extension token --copy`，密码会复制到剪贴板。
2. 在 Finder 里打开仓库的 `extension/shortcuts` 文件夹，双击 `存到 Octopus（网页）.shortcut`，点「添加快捷指令」。它会问两件事：Octopus 的密码（粘贴第 1 步的密码）和保存地址（没改过端口就不用动）。
3. 同样双击 `存到 Octopus（选中文字）.shortcut` 添加，再填一次密码。（同一个文件夹里的 `Save to Octopus (page).shortcut` / `Save to Octopus (selected text).shortcut` 是英文版，内容一样，导入一组就够了。）
4. 第一次用时 macOS 会问"是否允许「存到 Octopus」连接到 127.0.0.1"，选「始终允许」。

两种存法：

- **存网页**：Safari（或任何能分享链接的 App）里点「共享」按钮 →「存到 Octopus（网页）」。Octopus 按链接去读这一页，和在聊天里发链接一样（YouTube、B站、播客照样下载转写）。要登录才能看的页面读不到时，可以用 Chrome 插件存，或者截图发到聊天里。
- **存选中的文字**：在任何 App 里选中文字 → 右键 →「服务」→「存到 Octopus（选中文字）」。存成一条笔记，不会被当成提问。

Octopus 收到后右上角弹出「Octopus 已收到，正在存」（存好要等一会儿，结果不会发到聊天里，用 `/recent` 能看到）。弹出「没存上」是密码不对：重新导入快捷指令再填一次密码。后台服务没在运行时，快捷指令自己会弹出"无法连接"之类的错误（快捷指令没法接住这个错误，换成自己的提示）。

密码存在快捷指令里面，会跟着 iCloud 同步到你的其他设备（在别的设备上没用：它只连本机 127.0.0.1）。不要把填过密码的快捷指令分享给别人；别人要用，就让他用 `build.py` 自己生成并签名。

右键菜单里没有「服务」→「存到 Octopus（选中文字）」时：打开「快捷指令」App，双击这个快捷指令 → 右边「快捷指令详细信息」里勾上「用作快速操作」和「服务菜单」。双击 `.shortcut` 文件打不开时，可以照 [docs/setup.md](docs/setup.md) 第 10 节的步骤自己在「快捷指令」App 里搭一个。

## Discord

用 Discord 私信和 Octopus 说话，用法和 Telegram 一样：发链接、文件、图片就存，发文字就聊天，`/s`、`/recent` 这些命令照样打，语音消息会转成文字处理。只回复你自己（`owner_user_ids` 里的账号）的私信，别人发的、服务器频道里的都不理。不需要公网地址。

设置：

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications) →「New Application」，起个名字。
2. 左边「Bot」页面：点「Reset Token」拿到 token。不用打开「Privileged Gateway Intents」里的任何一项：Octopus 只读私信，私信的内容不需要它们。
3. 保存 token：`uv run octopus secret set discord_bot_token`
4. 邀请机器人：左边「OAuth2」→「URL Generator」，Scopes 勾 `bot`，复制生成的链接在浏览器打开，选一个你自己的服务器加进去（Discord 要求机器人和你至少在一个共同的服务器里，你才能私信它）。
5. 找到你的用户 ID：Discord「用户设置」→「高级」打开「开发者模式」，然后右键自己的头像 →「复制用户 ID」。
6. 编辑 `config.toml`，再 `uv run octopus service restart`：

```toml
[channels.discord]
enabled = true
owner_user_ids = ["123456789012345678"]   # 换成你的用户 ID，只放你自己的账号
```

7. 在服务器的成员列表里点机器人 →「发送消息」，发一句"你好"试试。

按钮（「改成提问」「确认」等）在 Discord 里是消息下面的按钮；服务重启后旧消息上的按钮也还能点。单个文件最大 100MB。

在 Discord 里打 `/` 会弹出它自己的命令菜单，里面也有 Discord 自带的命令（`/shrug`、`/spoiler`、`/giphy`、`/tts`、`/me` 等）：打 `/s` 回车可能选中 `/shrug` 或 `/spoiler`，而不是把你的文字发出去。Octopus 把自己的命令也放进了这个菜单（服务启动时自动登记，不用设置）。选带机器人头像的那些，后面的内容填在「内容」一栏（只能一行；要写多行，就把命令当普通消息打出来发）。没从菜单里选、当普通消息发出去的命令照样认。请给 Octopus 单独建一个机器人应用：启动时它会用自己的命令替换这个应用的全部全局命令。

## 邮件（只存不聊）

把文章、newsletter、链接、附件转发到一个专门的邮箱，Octopus 每分钟看一次，存好后回一封邮件告诉你（标题、摘要、id）。邮件只用来存，不会聊天。

- 正文只有链接（加一句"Sent from my iPhone"这种也算）：按链接存，最多 10 个；退订、取消订阅这类链接不会去打开。
- 转发的 newsletter / HTML 邮件（"作为附件转发"的也一样）：直接从邮件内容存成一篇文章，不用 Octopus 再去打开网页；找到"在浏览器中查看"链接就记下它。
- 其他文字：存成笔记，标题用邮件主题。
- 附件（图片、PDF、文件）：和在聊天里发文件一样存（图片会识别文字），主题和正文一起存进去。

为了防止别人冒充你：只收 `owner_addresses` 里的地址发来、并且收件邮箱的服务商验证过发件人（DMARC 通过；发件域名没有 DMARC 时看 DKIM / SPF）的邮件，其他邮件放着不动（保持未读）。服务商把验证结果写在邮件头 `Authentication-Results` 里，开头是它自己的服务器名；别人也能在邮件里写一个假的，所以 Octopus 只认服务器名对得上的那一个。

- **收件邮箱请用 Gmail**：它的服务器名（`mx.google.com`）已经内置，什么都不用填。
- 其他服务商：只有它在每封收到的邮件里都写一个固定服务器名的 `Authentication-Results`，才能用，并且要在配置里写 `authserv_id = "<那个服务器名>"`（在网页版里打开一封收到的邮件的"显示原始邮件"，看 `Authentication-Results:` 后面第一个词）。
- **QQ 邮箱、Outlook / Hotmail 不支持**：QQ 邮箱不确定会写这个邮件头；Outlook 写的不带服务器名，没法和冒充的分开。不知道服务器名时，邮件入口不会启动，`octopus run` 会说明原因。

设置：

1. 新建一个专门的 Gmail 邮箱（不要用你的主邮箱：Octopus 会读里面所有新邮件）。开两步验证，然后在 Google 账号「安全性」→「应用专用密码」生成一个。不要在这个邮箱里打开「设置」→「账号和导入」→「查收其他账号的邮件」：那样取来的邮件不经过 Gmail 收件时的验证，冒充的验证结果可能被当真。
2. 保存这个密码：`uv run octopus secret set email_password`
3. 编辑 `config.toml`，再 `uv run octopus service restart`：

```toml
[channels.email]
enabled = true
imap_host = "imap.gmail.com"
smtp_host = "smtp.gmail.com"
username = "my-octopus-box@example.com"
owner_addresses = ["me@example.com"]   # 你自己平时发邮件的地址，可以写几个
reply = "email"                   # 回复方式："email"（回邮件）、"telegram"（发到 Telegram）、"none"（不回）
# poll_seconds = 60  imap_port = 993  smtp_port = 465（587 也可以）
# authserv_id = "mx.example.net"  只有不用 Gmail 时才要写（见上面）
```

转发示例：在手机邮件 App 里打开一封 newsletter →「转发」→ 收件人填这个邮箱 → 发送；看到好文章的链接，直接把链接发到这个邮箱；拍了一张白板照片，作为附件发过去。

第一次启动时只看这个邮箱里还没读的邮件；之后每封新邮件都会看（在手机上先点开了也不会漏），同一封不会存两次。邮箱服务器一直拿不出某一封邮件（试了 3 次、超过 10 分钟）时会跳过它，并在 Telegram 上说一声「有一封邮件读不出来，已跳过：<主题>」（`reply = "none"` 时不说）。

## 命令行

所有命令都用 `uv run octopus <命令>`：

| 命令 | 作用 |
|---|---|
| `init [--language zh\|en]` | 写入默认配置文件（不写 `--language` 就按系统语言：中文系统用中文，其他用英文） |
| `chat` | 在终端里使用 |
| `run` | 前台运行后台服务：启动已启用的入口并处理任务，Ctrl-C 退出 |
| `reindex` | 从 Markdown 重建索引 |
| `backup` | 立即备份数据库（每天最多一份，保留最近 7 份） |
| `secret set <名字>` / `secret list` | 把密钥存进钥匙串（Linux / Docker：`secrets.toml`）/ 查看哪些密钥已设置、来自哪里 |
| `clawbot-login` | 扫码登录微信 ClawBot |
| `extension token [--copy]` | 显示 Chrome 插件（和快捷指令）的密码（第一次运行时生成）；`--copy` 同时复制到剪贴板（Mac） |
| `models` / `models test` | 查看各环节的模型和 API key 是否已设置 / 给每个模型发一句话试试能不能用 |
| `library move <文件夹>` / `library adopt <文件夹>` / `library sync [--apply-deletes]` | 把笔记和附件搬到另一个文件夹（例如 Obsidian 仓库里）/ 笔记文件夹被手动改名或挪走后改用新位置 / 列出文件已经不见的笔记，加 `--apply-deletes` 把它们从索引里去掉（都要先停服务） |
| `service install` / `restart` / `stop` / `start` / `uninstall` / `status` | 安装并启动 / 重启 / 先停一下 / 再启动 / 停止并卸载 / 查看后台服务（macOS launchd，Linux systemd） |

可以用 `--config <路径>` 指定别的配置文件（放在最前面：`uv run octopus --config x.toml chat`）。

## 后台服务

要让各个入口一直能收到消息，把 Octopus 装成登录后自启的后台服务（macOS launchd；Linux 上是 systemd，见「在 Linux / Docker 上运行」）：

```bash
uv run octopus service install
uv run octopus service status
uv run octopus service restart
uv run octopus service stop
uv run octopus service start
uv run octopus service uninstall
```

`stop` 只是现在先停下来，服务还装着，下次登录时会再自动启动。要彻底关掉，用 `uninstall`。

服务异常退出会自动重启。安装前先确认入口已经配置好，并且 `uv run octopus run` 能正常启动。服务的输出在 `~/Octopus/logs/launchd.out.log` 和 `launchd.err.log`。

### 运行须知

- **改了配置或密钥后要重启**：编辑 `config.toml` 或运行 `secret set` 之后，后台服务不会自动读取新内容，运行 `uv run octopus service restart`（等于重新安装）。
- **服务运行时，`chat` / `reindex` / `clawbot-login` / `library move`、`adopt`、`sync` 会拒绝执行**（同一时间只能有一个 Octopus 在用数据）。先 `uv run octopus service stop` 停掉服务（它还要几秒收尾，稍等再运行），做完再 `service start`（`uninstall` 也行，但做完要重新 `service install`）；或者直接用 Telegram / 飞书 / 微信聊天。
- **登录后才启动，不是开机就启动**：它是用户级的 LaunchAgent，重启电脑（FileVault 开启时，或系统更新后的重启）后要先登录，服务才会起来。`service install` 必须在已登录的桌面会话里运行，不要通过 SSH 运行。
- **搬动仓库、`uv python upgrade` 或重建 `.venv` 之后要重新运行 `service install`**，因为服务记录的是当时的 Python 路径；之后第一次读取密钥时钥匙串可能再次弹窗，选“始终允许”。
- `service status` 会显示运行状态、上次退出码和两个日志文件的路径。

## 数据放在哪里

默认全部在 `~/Octopus`：

- `library/`：所有笔记，按年月分目录的 Markdown 文件，文件头是 YAML（标题、来源、标签等）。想用 Obsidian 看，请按下面「用 Obsidian 看笔记」把它搬进你的仓库（直接打开这个目录看不到附件）。`config.toml` 里设置了 `library_dir`（`library move` 会自动写上）时，笔记在那个文件夹里，附件在它的 `attachments/` 里；下面其他的照旧在 `~/Octopus`。
- `attachments/`：保存的图片、音频、文件（设置了 `library_dir` 时在笔记文件夹里，见上）。`attachments/_incoming/` 始终在这里：各入口和 Chrome 插件刚收到、还没存好的文件，24 小时没用到的启动时会清掉。
- `backups/`：数据库的每日备份（见下）。
- `trash/`：被删除的笔记（`/delete` 或在聊天里说“删掉”）。
- `logs/`：运行日志。
- `config.toml`：配置。密钥不在这里，在 macOS 钥匙串里（Linux / Docker 上在这里的 `secrets.toml`，只有你能读）。

笔记 Markdown 才是真正的数据；搜索索引只是它的缓存，丢了可以用 `uv run octopus reindex` 重建。

## 用 Obsidian 看笔记

笔记本来就是 Markdown 文件，可以放进你的 Obsidian 仓库（vault），在 Obsidian 里看、改，Octopus 会跟着更新。

1. 先停掉服务：`uv run octopus service stop`（它还要几秒收尾，稍等再继续）。
2. 把已有笔记搬进仓库里的一个文件夹：`uv run octopus library move ~/Documents/MyVault/Octopus`。它会复制全部笔记和附件、逐个校验，然后改好 `config.toml` 里的 `library_dir`，旧文件移到 `~/Octopus/backups/library-before-move-日期时间/`（确认没问题后可以删掉）。中途断了，再运行一次同样的命令就行。还没存过笔记的话，也可以直接在 `config.toml` 最上面写一行 `library_dir = "~/Documents/MyVault/Octopus"`。
3. 重新启动：`uv run octopus service start`，然后用 Obsidian 打开这个仓库。

搬过去以后，图片、录音、文件也放在这个文件夹的 `attachments/` 里，每条笔记末尾的「附件」一节可以直接看图、点开文件（这一节是自动生成的，改了也会被重写）。数据库、日志、下载、回收目录和密钥不会放进仓库。笔记文件夹里有一个隐藏的 `.octopus-library` 文件，Octopus 靠它认出这是自己的笔记文件夹，不要删。

在 Obsidian 里可以改：

- **正文、摘要、要点、我的备注**：Octopus 每分钟看一次，改过的笔记会重新建索引，之后提问和 `/find` 用的就是新内容（刚保存几秒内的，等下一轮）。
- **标题**：改文件头里的 `title`，或者把「摘要」上面第一行写成 `# 新标题`。
- **标签**：改文件头里的 `tags`。
- **文件名和位置**：可以改名，也可以移到笔记文件夹里别的子文件夹；Octopus 按文件头里的 `id` 认笔记，不会改回去（刚改名的几秒内用 id 打开这条，可能暂时找不到；再发一次它的链接照样会说已经存过）。你自己加的文件头字段（比如 `aliases`）和自己写的段落也会保留。

还要知道：

- 在聊天里补充内容、重新总结时，摘要、要点和标签会重新生成；正文和备注里你改过的内容都会保留，正在改的时候补充也不会冲掉你的修改。
- 在 Obsidian 里**删掉**一条笔记（包括移到 Obsidian 的 `.trash`），大约两分钟后 Octopus 才把它从索引里去掉；附件留在原处，从 `.trash` 放回来就又能用。一次少了好几条（超过 5 条，或者超过全部的一成），Octopus 会当作文件夹出了问题，一条也不去掉，并发消息提醒你。确实是你删的：停掉服务后运行 `uv run octopus library sync --apply-deletes`（不加 `--apply-deletes` 只列出来），再启动服务；文件放回来了就什么都不用做。
- **复制**一条笔记（Obsidian 的“创建副本”）：副本和原来的 `id` 一样，Octopus 只认原来那个文件，副本不会被读、改或删；日志里会提醒一次。在聊天里删掉原来那条以后，副本也不会顶替它（运行 `reindex` 也不会）。想找回在聊天里删掉的笔记：把它从 `~/Octopus/trash/` 放回笔记文件夹（还有副本的话先删掉副本，或去掉副本的 `id`），停掉服务后运行 `uv run octopus reindex`，再启动服务。想让副本成为一条新笔记，请把它文件头里的 `id` 删掉（它就成了你自己的笔记）。
- 文件头里没有 Octopus `id` 的笔记（你自己写的）Octopus 不读也不改。
- 不要改文件头里的 `id`，也不要给 `attachments/` 里的文件改名：Octopus 靠它们找到笔记和附件。文件头写坏了，Octopus 会继续用改坏之前的内容，并在日志里提醒一次。
- **改了笔记文件夹的名字或位置**（或者整个仓库）：Octopus 找不到标记文件，就先停止同步（什么都不删），并发消息提醒你。停掉服务后运行 `uv run octopus library adopt <新位置>`，再启动服务即可。从旧版本升级不用做什么：笔记都在原处时，Octopus 会自己加上标记文件。

## 备份

Octopus 运行时会在启动时和每天日期变化时自动备份数据库，每天最多一份，放在 `~/Octopus/backups/octopus-YYYYMMDD.db`，只保留最近 7 份。也可以随时手动运行 `uv run octopus backup`；当天已经备份过会提示“今天已经备份过了”。笔记本身是普通文件，建议你再用自己习惯的方式（Time Machine、云盘）备份整个 `~/Octopus`。

**从备份恢复数据库**：

1. 停掉服务和所有 `chat`：`uv run octopus service stop`。
2. 删掉 `~/Octopus/octopus.db-wal` 和 `~/Octopus/octopus.db-shm`（如果存在）；留着它们会把旧的写入混进恢复的数据库。
3. 把想恢复的备份复制成数据库：`cp ~/Octopus/backups/octopus-YYYYMMDD.db ~/Octopus/octopus.db`。
4. 重新启动：`uv run octopus service start`（或 `uv run octopus chat`）。如果备份之后又保存过笔记，运行 `uv run octopus reindex` 让索引补上它们。

## 在 Linux / Docker 上运行

Mac 上的用法完全不变（MLX 转写、系统识字、钥匙串、launchd）。在 Linux 服务器、NAS、VPS 上可以直接安装，也可以用 Docker。

**和 Mac 不同的地方**（两种方式都一样）：

- 语音转文字用 faster-whisper（CPU 运行），默认模型是 `small`：第一次转写时从 HuggingFace 下载（约 500 MB），在普通的 4 核 CPU 上一小时的播客大约半小时到一小时转完（估计值）。机器够快可以在 `[media]` 里写 `faster_whisper_model = "medium"` 或 `"large-v3-turbo"`（更准，慢 2～4 倍）。
- 图片识字用 RapidOCR（中文、英文，模型随安装包带，不用下载）。iPhone 的 HEIC 图片暂时识别不了，请发 JPG / PNG 截图。
- 想手动指定：`[media]` 里的 `asr_backend = "auto"`（或 `"mlx"`、`"faster-whisper"`）、`ocr_backend = "auto"`（或 `"ocrmac"`、`"rapidocr"`）。`auto` 在 Mac 上就是原来的 mlx / ocrmac。写了这台机器上没装的（比如 Linux 上写 `"mlx"`），启动时就会说明，不会等到第一条语音才出错。
- 透明背景的 PNG（导出的图、贴纸）会先铺上白底再识别。
- 默认的 `claude:…` 模型要用 Claude Code 命令行登录你的订阅；服务器上一般改用 API key 或本地模型，见上面「模型：订阅、API key 或完全本地」。

### 直接安装（Debian / Ubuntu 等）

```bash
sudo apt install ffmpeg
git clone https://github.com/andrewman2035/octopus-kb.git && cd octopus-kb
uv sync
uv run octopus init --language zh     # 生成中文的 ~/Octopus/config.toml（不写就按系统语言）
```

**密钥**按这个顺序找：环境变量 → `~/Octopus/secrets.toml`（Linux 上默认不用钥匙串）。

- `uv run octopus secret set telegram_token` 存进 `secrets.toml`（Octopus 自己建，权限 600），并说存到了哪里；`uv run octopus secret list` 列出每个密钥是否已设置、来自哪里（环境变量 / 钥匙串 / 密钥文件），不显示密钥本身。
- `secrets.toml` 必须属于你、只有你自己能读：否则 Octopus 不读它，并提示运行 `chmod 600` 或 `chown`。放在不支持权限的网络共享盘上时 Octopus 不会写它，请把 `data_dir` 放到本地磁盘，或改用环境变量。
- 环境变量名是 `OCTOPUS_<名字大写>`，例如 `OCTOPUS_TELEGRAM_TOKEN`、`OCTOPUS_DISCORD_BOT_TOKEN`、`OCTOPUS_EMAIL_PASSWORD`、`OCTOPUS_DEEPSEEK_API_KEY`；Anthropic 的是 `ANTHROPIC_API_KEY`。两处都有时用环境变量。例外：`clawbot_token` 是 Octopus 扫码后自己写的，不读环境变量。
- 想在 Linux 桌面上用钥匙串（GNOME 钥匙串、KeePassXC 等 Secret Service）：在 `config.toml` 里写 `[secrets]` 和 `backend = "keyring"`。Octopus 只用已经解锁的默认钥匙串，从不弹窗解锁或新建；钥匙串锁着、没有响应（5 秒）时改用 `secrets.toml`，并在日志里说一次。`backend = "file"` 在任何系统上都只用文件（Mac 上默认 `auto` 就是钥匙串，和以前一样）。

**后台服务**用 systemd 的用户服务，命令和 Mac 上一样：

```bash
uv run octopus service install   # 写入 ~/.config/systemd/user/octopus.service 并启动
loginctl enable-linger $USER     # 只需一次：不登录也运行，开机就启动
uv run octopus service status
```

`restart` / `stop` / `start` / `uninstall` 也一样。崩溃后 30 秒自动重启；输出在 `~/Octopus/logs/systemd.out.log` 和 `systemd.err.log`。后台服务读不到终端里设置的环境变量，密钥请用 `secret set`（`secret list`、`models` 遇到只在终端环境变量里的密钥会提醒）。用了 `backend = "keyring"` 时，开机后要登录、钥匙串解锁了服务才读得到；想开机不登录就运行，用默认的文件。

### Docker

```bash
git clone https://github.com/andrewman2035/octopus-kb.git && cd octopus-kb
mkdir -p data                                   # 你的笔记、配置、密钥都在这里
docker compose build
docker compose run --rm octopus init --language zh   # 生成中文的 data/config.toml（data_dir = "/data"；容器里不写就是英文）
# 编辑 data/config.toml：模型改成 API key 或本地模型，启用入口
docker compose run --rm octopus secret set telegram_token   # 存进 data/secrets.toml
docker compose up -d
docker compose logs -f                          # 或看 data/logs/octopus.log
```

- **Claude 订阅用不了**：镜像里没有 Claude Code 命令行，`claude:…` 模型会失败。请改用 `anthropic:…` 或 `openai:<名字>`（API key），或者本地模型。API key 用 `docker compose run --rm octopus secret set anthropic_api_key` 存，或写进 `docker-compose.yml` 的 `environment`（建议放在 `.env` 文件里，不要提交到 git）。
- **本地模型和搜索**：向量检索要 Ollama 的 `bge-m3`。用 compose 里自带的 Ollama：`docker compose --profile ollama up -d`，再 `docker compose exec ollama ollama pull bge-m3`，并在 `data/config.toml` 的 `[embed]`（和 `[local_llm]`）里写 `base_url = "http://ollama:11434"`。
- **Chrome 插件**：在 `data/config.toml` 里写（只在容器里这样写；在普通机器上写 `0.0.0.0`，Octopus 启动时会在日志里警告）

  ```toml
  [channels.extension]
  enabled = true
  port = 8765
  bind = "0.0.0.0"
  ```

  容器里必须听 `0.0.0.0`，compose 再把端口发布成 `127.0.0.1:8765:8765`：只有这台机器的浏览器能连上，别的机器连不上。两边的端口号要一样（插件发来的是 `127.0.0.1:8765`）。不在容器里时不要改 `bind`，默认的 `127.0.0.1` 最安全。插件的密码：`docker compose run --rm octopus extension token`。
- **Discord 和邮件**：在容器里照常能用，不用装别的。token 和密码用 `docker compose run --rm octopus secret set discord_bot_token`（`email_password`）存进 `data/secrets.toml`，或者在 `.env` 里写 `OCTOPUS_DISCORD_BOT_TOKEN=…`、`OCTOPUS_EMAIL_PASSWORD=…`，再把 `docker-compose.yml` 里对应的两行取消注释。快捷指令只在 Mac 上用。
- **文件权限**：容器以 uid 1000 运行。`data` 文件夹的属主不是 1000 时，在 `.env` 里写 `OCTOPUS_UID=<你的 uid>` 和 `OCTOPUS_GID=<你的 gid>`。
- 自动重启由 Docker 负责（`restart: unless-stopped`），容器里不用 `service` 命令。镜像里没有你的数据和密钥，都在 `data/` 里；模型缓存在 `data/.cache/`。
- 时区默认 `UTC`，在 `.env` 里写 `TZ=Europe/Berlin` 这样的一行就能改。
- 在容器里跑测试：`docker build --target test -t octopus-test . && docker run --rm octopus-test`。

## 在 Windows 上运行

暂不支持原生 Windows。可以用下面两种方式：

**WSL2（推荐）**：在管理员 PowerShell 里运行 `wsl --install`，会装好 Ubuntu。在 Ubuntu 里照上面「在 Linux / Docker 上运行」的「直接安装」做：uv、`sudo apt install ffmpeg`、faster-whisper 和 RapidOCR、密钥用环境变量或 `secrets.toml`。

- 后台服务：在 `/etc/wsl.conf` 里写 `[boot]` 和 `systemd=true`，在 Windows 里运行 `wsl --shutdown`，再打开 Ubuntu，然后 `uv run octopus service install` 和 `loginctl enable-linger $USER`。
- WSL 在 Windows 这边最后一个进程结束后不久就会停掉这个发行版，服务也跟着停。想一直运行：在「任务计划程序」里加一个「登录时」触发的任务，运行 `wsl.exe -d Ubuntu --exec sleep infinity`。让它在后台隐藏运行（勾选「不管用户是否登录都要运行」，或用隐藏窗口的启动方式）。
- 模型：在 Ubuntu 里装好并登录 Claude Code，`claude:…` 就能用；否则用 API key 或本地模型。Ollama 也装在 Ubuntu 里（`ollama pull bge-m3`），默认的 `127.0.0.1:11434` 直接可用。
- Chrome 插件（装在 Windows 上）：WSL2 默认把 Windows 的 `127.0.0.1:8765` 转发到 Ubuntu，所以 `bind` 保持 `127.0.0.1`。把 `extension/chrome` 复制到 Windows 的文件夹（比如 `cp -r extension/chrome /mnt/c/Users/<你>/octopus-chrome`），加载那个文件夹。
- Windows 上的 Obsidian：打开 `\\wsl$\Ubuntu\home\<用户名>\Octopus\library`，或者在 Ubuntu 里把 `library_dir` 设成 vault 里的文件夹。笔记放在 Linux 文件系统里；Octopus 每分钟扫一次文件夹，在 Windows 里改的也会被读到。
- 上面的 `Ubuntu` 换成 `wsl -l -v` 显示的发行版名字（可能是 `Ubuntu-24.04` 这样的）。

**Docker Desktop（WSL2 后端）**：上面 Docker 一节照样适用，包括给插件发布在 `127.0.0.1:8765` 的端口。和 Linux 上的 Docker 一样，容器里没有 Claude Code：用 API key 或 Ollama（compose 的 `ollama` profile，或 Windows 上的 Ollama：`http://host.docker.internal:11434`）。

只在 Mac 上有的部分（快捷指令、MLX、macOS 的文字识别、钥匙串、launchd）不适用。

## 常见问题

- **先看状态**：在聊天里发 `/status`；再看日志 `~/Octopus/logs/octopus.log`（后台服务另有 `launchd.out.log` / `launchd.err.log`）。
- **在中国大陆用不了 Claude**：Anthropic 不在大陆提供服务。发 `/model local`，整理和问答都改用本地模型；回到海外后发 `/model default`。Claude 调用失败时 Octopus 也会自动改用本地模型，并在回复末尾注明“本次由本地模型回答”。
- **YouTube 打不开 / 被拦截**：YouTube 经常拦截不登录的访问。可以在 `config.toml` 的 `[media]` 里设置 `youtube_cookies_from_browser = "chrome"`（或 `"safari"`），让 Octopus 读取该浏览器里的 YouTube 登录状态。**默认是关闭的**（留空），不设置就不会读取你的浏览器。
- **X 长推文只存了前 280 字**：X 的公开接口只给开头。可以在 `[media]` 里设置 `x_full_text_service = "fxtwitter"`，通过 FxTwitter 服务拿全文（默认关闭，见「隐私」）。不设置时这条笔记是“部分内容”，会请你补截图。
- **“没能获取内容”**：这个链接抓不到正文（需要登录、反爬、被网站拒绝）。条目会占位保存。30 分钟内把截图、录屏或粘贴的原文发过来，会合并到同一条里；过了 30 分钟，请**引用回复**那条“没能获取内容”的消息再发，不引用的文字会被当成聊天。
- **钥匙串弹窗、入口没反应、微信需要重新扫码**：见 [docs/setup.md](docs/setup.md) 的“常见问题”和微信 ClawBot 一节。

## 隐私

所有笔记、索引、备份都只存在你自己的电脑上。向外发出的只有两类请求：一是整理和问答时发给模型的内容（Claude 订阅，或你在 `config.toml` 里配置的 API 服务：Anthropic API、DeepSeek 等 OpenAI 兼容服务；用 `/model local` 可以改成完全不出本机）；二是你发来的链接，Octopus 会去访问对应的网站抓取内容。如果设置了 `x_full_text_service = "fxtwitter"`（默认关闭），长推文的链接还会发给 FxTwitter 服务（api.fxtwitter.com）来拿全文。Telegram / 飞书 / 微信 / Discord 入口当然也会经过各自平台的服务器，邮件入口会连你设置的邮箱服务器；微信 ClawBot 的登录和二维码请求会发到腾讯的服务器。第一次转写语音时，Whisper 模型会从 HuggingFace 一次性下载到本机（约 1.5 GB），之后不再联网。密钥和 API key 保存在 macOS 钥匙串（Linux / Docker 上是只有你能读的 `secrets.toml`，或你自己设置的环境变量），不写进配置文件和日志。

## 免责声明

Octopus 是个人工具，按现状提供，不附带任何保证。请遵守你用到的每个平台的服务条款：Octopus 不绕过登录、付费墙或反爬措施，读取浏览器 cookie 默认关闭。微信 ClawBot 用的是腾讯的 iLink 机器人接口；Claude 订阅模式用的是你自己的账号，受 Anthropic 条款约束，给别人用请改用 API key。邮件入口只信任收件服务商验证过的发件人（建议 Gmail，发件域名要有 DMARC）。完整说明见 [DISCLAIMER.md](DISCLAIMER.md)（英文）。

## 参与开发

开发环境、测试和提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)，整体结构见 [ARCHITECTURE.md](ARCHITECTURE.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告（都是英文）。各依赖的许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 许可证

Octopus 以 [MIT 许可证](LICENSE) 发布。各依赖保留各自的许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（英文）。

## 手动验收

改了入口、模型或媒体处理之后，按 [docs/manual-testing.md](docs/manual-testing.md)（英文）在真实环境里走一遍。
