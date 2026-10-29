# Copilot Codex Companion 1.1.2

Windows 本机兼容方案。包含消息 ID 修复和 Codex 模型目录适配。非 OpenAI/GitHub 官方补丁。采用 [MIT 许可证](LICENSE)。

## 从 GitHub 获取和更新

当前通过 GitHub 源码或 ZIP 分发，安装和更新不需要 Node.js、npm 或 npx。仓库保留的 npm 封装仅作备用；受组织策略限制时不使用。

在有仓库访问权限的电脑上，从 GitHub 页面选择 **Code → Download ZIP** 并解压，或者克隆：

```powershell
git clone https://github.com/flyrobin620/copilot-codex-companion.git
cd copilot-codex-companion

# 首次安装（已有安装不要重复 install）
.\install.cmd --automatic-context

# 已安装 1.1.0 / 1.1.1：原地更新，也可同时切换 hostname
.\update.cmd --hostname git.example.com

# 自定义旧版安装目录
.\update.cmd --dest "D:\Tools\CodexCopilotCompat" --hostname git.example.com
```

以后在源码目录运行 `git pull --ff-only`，再运行 `update.cmd`；使用 ZIP 时下载新版并解压到独立目录后运行 `update.cmd`。省略 `--hostname` 会保留当前主机。请先等活动任务结束并退出 Codex，更新成功后重新打开。

源码目录和安装目录应分开；默认安装到 `%LOCALAPPDATA%\CodexCopilotCompat`。仓库公开，可直接下载或克隆。详细的安装前提、备份及回退行为见下文。

## 安装到另一台 Windows 电脑

前提：Python 3.11+（含 pythonw.exe）、已有可用的 Codex → Copilot Enterprise Responses HTTP 连接和命令式认证，以及与桌面应用匹配的 codex.exe。无需 ChatGPT 账号、管理员权限或第三方 Python 库。

1. 解压整个 ZIP。已有 1.0.0 活动安装时，先用旧版 uninstall.cmd 卸载，再安装；旧版卸载留下的目录现在可以直接复用，脚本会保留旧备份。
2. 要让上游目录自动管理上下文，在解压目录打开终端，运行：

   ```powershell
   .\install.cmd --automatic-context
   ```

   仅双击 install.cmd 也会启用目录适配，但保留已有的手工上下文上限。
3. 等待活动任务结束，完整退出并重开 Codex。
4. 运行 `check.cmd --live` 检查模型目录和一条真实测试回复，再在新聊天检查显示效果。

测试请求只包含固定文本，消耗少量 Copilot 用量。不会发送你的聊天记录。

Copilot CLI、GitHub CLI（gh）和 Codex CLI 是不同工具。安装器复用目标电脑 config.toml 中原有的认证命令；原命令若是 gh，需要目标机已有 gh 登录。仅安装 Copilot CLI 不等于 gh 或 codex.exe 已可用。

## 更新已安装的 1.1.0 / 切换 hostname

将完整的新版本包传到目标 Windows 电脑，解压到独立目录。等活动任务结束并退出 Codex，然后在**新包目录**运行：

```powershell
# 更新默认安装目录，保留当前认证主机
.\update.cmd

# 更新并持久切换认证主机
.\update.cmd --hostname git.example.com

# 自定义安装目录：--dest 指向旧版安装目录，不是新包目录
.\update.cmd --dest "D:\Tools\CodexCopilotCompat" --hostname git.example.com
```

无需先卸载，也不要先将新文件手工覆盖旧安装。脚本先验证认证命令，再将旧程序、settings.json 和模型目录快照备份到安装目录的 `update-backup-*`，停止自己的 bridge，替换程序，重启并验证认证和模型目录。验证失败时尝试自动恢复旧文件、旧 hostname 并重新启动；恢复失败会显示备份路径。备份包含本机安装信息，请仅保留在本机。

更新保留 config.toml、端口、安装身份、原认证命令、上下文设置和卸载恢复信息。省略 `--hostname` 时保留当前主机，不默认指定域名。目标主机需先完成原认证工具的登录（如 `gh auth login --hostname git.example.com`）。更新成功后重新打开 Codex，以刷新缓存的凭据。

以后仅切换 hostname，可在**安装目录**运行同一个命令：

```powershell
.\update.cmd --hostname another.example.com
```

`update.cmd` 需要同目录的完整程序文件，不能只复制这一个脚本。命令行自动化可使用 `python manage.py update --dest "D:\Tools\CodexCopilotCompat" --hostname git.example.com`，避免 `.cmd` 结束时的按键暂停。

### 卸载残留目录与默认路径

如果目录没有 `settings.json`，但存在 `settings.uninstalled.*.json`，说明旧版已经卸载。1.1.2 的 `install.cmd` 可以直接重新安装；`update.cmd` 也会识别此状态并自动转入安装，复用卸载记录中的配置路径、端口和 Codex 命令（可通过参数覆盖）。新安装以当前 config.toml 中已恢复的认证命令为准，`--hostname` 继续生效。

重新安装前会将旧程序、原配置备份及目录快照保存到 `reinstall-backup-*`；原卸载记录和其他文件保持不变。认证预检查失败时不改旧文件，重新安装失败时恢复旧程序和备份。无法识别的非空目录仍不会被覆盖。

```powershell
git pull --ff-only
.\update.cmd --hostname microsoft.ghe.com

# 查看实际默认目录
python manage.py update --help
```

脚本每次运行都会打印 `Installation directory:`。默认目录为 `%LOCALAPPDATA%\CodexCopilotCompat`；若脚本目录已有活动安装或卸载记录，则默认使用脚本目录。`--dest` 可以显式指定目标目录。

## 1.1.0 新增什么

代理把 Copilot `/models` 的 `data` 数据转换为 Codex 所需的 `models` 目录。

- 使用目标电脑当前 codex.exe 的 `debug models --bundled` 提取完整基础目录，保留该版本的模型提示词、工具设置及安全余量。
- 每次目录查询，使用当前请求的认证和请求头读取 Copilot 实际可用模型及容量。不会把一个账户的模型列表缓存给另一个账户。
- 只转换 ID 精确匹配、账户已启用、支持 Responses 和工具调用、且容量信息完整的模型；不会把另一个模型的提示词套到未知模型上。
- 输入上下文预算采用 `min(max_prompt_tokens, max_context_window_tokens - max_output_tokens)`，预留上游声明的最大输出额度。
- 推理档位取上游支持值与本地模型定义的交集；未明确支持的服务档位不会从 OpenAI 默认目录照搬。
- Codex 再应用自身的有效窗口百分比和自动压缩规则。代理不把总容量直接写成可用输入容量。
- 客户端版本与基础目录版本不符时返回明确错误，不静默使用另一版本的提示词；Codex 二进制发生变化时会重新提取默认目录。
- 基础提示词来自已安装 Codex 的发布快照，不冒充来自 OpenAI 云端的实时默认配置；Copilot 容量和可用性由实际接口读取。

在 2026-10-26 本机实测，gpt-6-astra 的上游总容量为 1,050,000、最大输出为 128,000，因此输入预算为 922,000。Codex 实际应用的有效窗口是 875,900，自动压缩阈值是 829,800。这些是当次测试值，不是代码写死的该模型上限，也不代表完成了百万 token 请求压力测试。

原有消息 ID 修复继续按单次响应的 output_index 统一标识。正文、工具参数、call_id、加密推理内容保持不变，流式输出不变。

## 手工限制及回退

`--automatic-context` 会在真实目录验证通过后移除顶层的：

- `model_context_window`
- `model_auto_compact_token_limit`

原值保存在本机回退状态中。卸载时会恢复；如果你随后重新设置了这些字段，卸载会保留你的新值。

如果已有显式 `model_catalog_json` 或 `model_catalog_url`，安装器会停止，要求先处理目录冲突。它不会自行删除组织策略、profile 中的设置或其他配置。

卸载：运行 `uninstall.cmd`，再重启 Codex。它恢复原连接字段，停止自己的进程，保留备份和其他配置修改。检测到连接字段被手工改变时会拒绝覆盖。

## 路径与认证

默认安装路径：`%LOCALAPPDATA%\CodexCopilotCompat`。

默认配置位置：`%USERPROFILE%\.codex\config.toml`；有 CODEX_HOME 时使用其下 config.toml。

运行文件安装后可以删除解压目录，检查及卸载入口也在安装目录。不要删除已安装目录。

仅监听本机 127.0.0.1。认证辅助程序按需启动 pythonw.exe，不增加开机任务。不记录令牌或对话正文；目标机器的原配置备份只在本机保存。**只分发原始 ZIP，不要打包已安装目录或配置备份。**

## 可选参数及常见问题

```powershell
# 端口被占用时选择另一端口
.\install.cmd --port 43130 --automatic-context

# PATH 中没有与桌面版本匹配的 Codex
.\install.cmd --codex "C:\path\to\codex.exe" --automatic-context

# 非默认配置或安装目录
.\install.cmd --config "D:\path\config.toml" --dest "D:\Tools\CodexCopilotCompat" --automatic-context

# 指定原认证命令使用的 hostname（例如 gh auth token 的主机）
.\install.cmd --hostname git.example.com --automatic-context

# 重启已安装的 bridge；也支持 --dest 指定安装目录
.\restart.cmd
.\restart.cmd --dest "D:\Tools\CodexCopilotCompat"

# 自检
.\check.cmd
.\check.cmd --live
```

`--hostname HOST` 是可选参数，不提供时完整保留原认证命令参数，不默认填入任何固定域名。提供时覆盖已有的 `--hostname HOST` 或 `--hostname=HOST`，没有则添加；原认证命令需要支持该参数。安装和更新时保存此选择，卸载仍恢复最初安装前的原配置。已有安装请使用 `update.cmd`。

也可直接调用安装目录中的 `auth_bridge.py`：`python auth_bridge.py --hostname git.example.com` 仅覆盖这次认证调用的主机，标准输出仍然只包含认证凭据。`python auth_bridge.py --restart` 只重启 bridge，不请求或输出凭据；两个参数不能同时使用。主机参数选择认证来源，不改变 Copilot API 地址。

重启会校验安装身份，使用本机管理密钥关闭服务，等待端口释放后重新启动并检查健康状态；服务原本未运行时直接启动。请等活动请求结束后再重启，以免中断正在生成的回复。无法确认服务身份或停止状态时会报错，不终止其他进程。

- 目录包含有效的卸载记录时，可以直接重新安装；无法识别的非空目录需要选择其他 `--dest`。
- 若 Codex 升级后提示目录版本不匹配，确认 PATH 中 codex.exe 与桌面自带运行时一致；必要时用匹配的 --codex 路径重新安装。
- 如果认证失败，先修复该机器原来的认证命令，不需要复制其他机器的令牌。
- 使用环境变量 API key、ChatGPT 认证、WebSocket、复杂 TOML 表/多行数组或由 profile/组织策略覆盖的配置，不在自动迁移覆盖范围内；安装器可能拒绝修改。
- 新模型若不在当前 Codex 自带目录中，不会凭空生成其完整能力及提示词，应先升级匹配的 Codex 版本。

## 验证

本次更新使用 `python -m unittest -v test_distribution` 验证 25 项测试，覆盖 ID 归一化、模型能力转换、安装/卸载、卸载残留目录重装与失败恢复、默认路径提示、上下文恢复、hostname 保留与覆盖、服务重启、身份校验、停止超时、旧版原地更新、更新失败回退、更新前检查及中文和空格路径。认证使用合成测试凭据，模型目录检查使用测试替身。

`npm test` 另验证 Node 命令入口的 Python 探测、参数转发、退出码和异常处理；npm 包可通过本地 `.tgz` 执行测试，无需发布到 registry。

1.1.0 原发布版曾完成真实 Copilot 请求及 Codex 自身验证：目录解析错误 0 次、消息缺失开始记录警告 0 次，并观察到实际有效窗口和压缩阈值。本次更新未重跑真实 Copilot 请求；另一台机器的账户、网络和桌面表现仍需现场验证。

包内仅含程序、说明和文件校验值。不包含任何人的账号、配置、模型目录快照、令牌或聊天记录。

官方模型目录说明：<https://learn.chatgpt.com/docs/model-catalogs>

## npm 入口（备用，不用于当前分发）

项目已提供 npm 命令入口；下面的包名命令需先发布到 npm registry 才能使用。目标电脑需要 Windows、Node.js 20+ / npm、Python 3.11+（含 pythonw.exe），以及已有可用的 Codex → Copilot Enterprise 连接。npm 包不包含 Python，也不会自动安装或替换 Python。

```powershell
# 首次安装
npx --yes copilot-codex-companion@latest install --automatic-context

# 将已有 1.1.0 / 1.1.1 安装升级到当前发布版
npx --yes copilot-codex-companion@latest update

# 更新并持久切换 hostname
npx --yes copilot-codex-companion@latest update --hostname git.example.com

# 自定义安装目录
npx --yes copilot-codex-companion@latest update --dest "D:\Tools\CodexCopilotCompat" --hostname git.example.com

# 重启、自检和卸载
npx --yes copilot-codex-companion@latest restart
npx --yes copilot-codex-companion@latest check
npx --yes copilot-codex-companion@latest uninstall
```

`@latest` 请求 registry 当前的 latest 版本；可换成 `@1.1.2` 固定版本。每次 `update` 都会执行原有的备份、校验和失败回退流程。安装后的服务使用 `%LOCALAPPDATA%\CodexCopilotCompat`（或 `--dest`），不依赖 npm 缓存路径；npx 退出后服务继续运行。等待活动任务结束并退出 Codex 后再更新，成功后重新打开 Codex 刷新凭据。

尚未发布时，可将 `npm pack` 生成的 `.tgz` 传到另一台电脑，直接通过 npx 使用：

```powershell
npx --yes --package .\copilot-codex-companion-1.1.2.tgz copilot-codex-companion update --hostname git.example.com
```

维护者发布步骤：

```powershell
npm test
python -m unittest -v test_distribution
npm pack --pack-destination dist
# 检查包内容后，使用有目标包名发布权限的 npm 账号
npm login
npm publish --access public
```

发布前确认 `package.json` 的 `name` 是自己有权限发布的名称；如改成 `@your-org/copilot-codex-companion`，npx 命令中的包名也使用该完整名称。项目使用 [MIT 许可证](LICENSE)。包仅包含显式列出的运行文件，`prepack` 检查版本一致性、运行文件完整性并生成校验值；没有安装时自动启动 bridge 的 npm lifecycle hook。后续发布需同步提升 `package.json` 与 `manage.py` 的版本，并维护更新器支持的旧版范围。
