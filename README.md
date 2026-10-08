# RustDesk OSS Relay Helper

管理 `hbbs` 下发的单个 `hbbr` 地址。按静态层级、连续健康计数和同层 RTT 中位数选择 relay，支持手动固定与自动恢复。第一版实现依据见 [方案与技术选型](docs/方案与技术选型.md)。

运行环境：与 `hbbs` 共享网络命名空间的 Linux 宿主机、Python 3.9+、Bash、systemd。仅使用 Python 标准库，无需 pip 安装依赖。控制台固定连接 `127.0.0.1:21115`，面向 RustDesk Server OSS 1.1.15 的 `rs` 接口。

## 本地开发

```powershell
python -m unittest discover -s tests -v
python -m relay_helper --help
python -m relay_helper --config config/relay-helper.example.ini --state var/state.json nodes
```

Windows 可运行核心与测试；Bash、journal 和 systemd 在 Linux 使用。不要直接对示例域名运行生产探测。真实开发配置复制为 `config/local.ini`，状态使用 `var/state.json`，两者已被 `.gitignore` 排除。示例配置、测试与文档正常跟踪。

## Linux 安装

先将仓库放到 `/opt/rustdesk-relay-helper`。以下命令在该目录执行，创建配置和状态目录。systemd 默认以 root 运行，无需创建专用账户；已经在 root shell 中时可以省略 `sudo`。

```bash
sudo install -d -m 0750 /etc/rustdesk-relay-helper /var/lib/rustdesk-relay-helper
# 仅首次安装复制；不要覆盖已有真实配置。
sudo test -e /etc/rustdesk-relay-helper/config.ini || sudo install -m 0640 config/relay-helper.example.ini /etc/rustdesk-relay-helper/config.ini
sudo install -m 0755 bin/rustdesk-relay-helper /usr/local/bin/rustdesk-relay-helper
sudo install -m 0644 deploy/systemd/rustdesk-relay-helper.service deploy/systemd/rustdesk-relay-helper.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudoedit /etc/rustdesk-relay-helper/config.ini
```

程序本身不检查用户名，也不要求 root；使用自定义配置和可写状态路径时，可以直接由当前用户运行。以上系统目录部署方式使用 `sudo` 是为了访问对应文件。进程须与 `hbbs` 共享网络命名空间，以访问其 loopback 控制台。

修改示例中的真实地址；备用节点默认禁用，无备用时可保留禁用或删除该节。地址必须是 `host:port` 或 `[IPv6]:port`。`tier` 越小越优先，`rtt_ranked = false` 的节点在同层排名池无可用候选时按配置顺序接管。修改配置后下次命令自动读取，不需要重启常驻进程。

## 命令

```bash
rustdesk-relay-helper help
rustdesk-relay-helper help switch
sudo rustdesk-relay-helper status
sudo rustdesk-relay-helper nodes
sudo rustdesk-relay-helper probe
sudo rustdesk-relay-helper switch usla
sudo rustdesk-relay-helper auto
sudo rustdesk-relay-helper history --lines 50
```

| 命令 | 行为 |
| --- | --- |
| `help [命令]` | 列出所有可用命令及说明，或查看指定命令的参数；无需配置、状态文件或管理员权限。也支持 `--help` 和 `<命令> --help`。 |
| `status` | 实时读取 `rs`，展示模式、实际值、目标、上次决策原因、时间和节点详情；不探测、不写 relay。读取失败显示未确认并返回非零退出码。 |
| `nodes` | 展示配置与已有探测结果，不联系 `hbbs`。 |
| `probe` | 探测所有启用节点并保存健康计数和有限 RTT 样本，不读取或写入 `hbbs`。 |
| `switch <id>` | 只探测指定目标，要求本轮成功且目标已健康，再持久化手动模式并应用目标。写后未确认时返回非零，保留手动意图供下次重试。 |
| `auto` | 先探测，恢复自动模式并立即决策；从手动恢复时重新按层级和 RTT 选优，不等待手动切换的保持期。 |
| `history` | 用 journal 标识 `rustdesk-relay-helper` 读取最近事件；读取 journal 可能需要管理员权限。 |
| `reconcile` | 定时内部命令：探测、读取实际值、按当前模式决策、必要时写入并回读确认。 |
| `web` | 启动仅监听 `127.0.0.1` 的 token WebUI；可用 `--port`、`--token-file`，不会启用自动 timer。 |

所有状态操作使用同一把非阻塞文件锁，冲突返回“管理器忙”。默认配置和状态路径分别为 `/etc/rustdesk-relay-helper/config.ini`、`/var/lib/rustdesk-relay-helper/state.json`。可在子命令前传入 `--config`、`--state`，或设置 `RELAY_HELPER_CONFIG`、`RELAY_HELPER_STATE`。同一实例的手动命令和 timer 必须共用状态路径。Bash 入口的安装目录可由 `RELAY_HELPER_HOME` 修改。

每轮对每个节点连续建立两次 TCP 连接；两次都成功才记一次成功，使用第二次 RTT，任意一次失败则整轮失败。新节点一轮成功即可确认健康并参与排名；已判故障节点默认仍需连续 6 轮成功恢复，3 轮连续失败才判定不可用。健康节点间的主动切换受 300 秒保持期限制，同层 RTT 改善需至少 20 ms；故障逃离不等待保持期。所有节点不可用时保留实际列表，不写空值。手动节点失败只提示，不自动切走。

成功/失败显示连续探测轮数，每轮只加一，超过健康判定阈值后继续累计，另一种结果出现时清零。显式从手动恢复自动是一次重新选优：仍遵守层级和健康条件，选择最高优先层中的 RTT 最优节点，该次不受保持期及 RTT 改善阈值限制。确认后，后续自动切换恢复上述限制；无候选或控制台未确认时，下次自动决策继续重试。`probe` 仍只测活，定时 `reconcile` 负责决策和切换。

`rtt_sample_count` 是成功点窗口上限（1–5，默认 5）。未满窗口时使用全部已有有效点的中位数，满后淘汰最早点；每点只在完成后的两分钟内有效，等于两分钟即失效，读取不会续期。样本全部过期后，健康变为未知并退出排名；故障节点过期仍保持故障，连续计数重新积累。WebUI 的 RTT 悬浮详情可查看两次连接结果。设计和失败边界见 [探测与样本有效期设计](docs/探测与样本有效期设计.md)。

升级时，旧状态中没有逐点时间的数字样本会被废弃，手动模式和已确认 relay 保留；重新探测后积累带时间戳的点。如果旧配置的 `rtt_sample_count` 大于 5，需要先调为 1–5。升级源码后重启常驻 Web 服务，新 CLI 和定时任务下次启动即加载新代码。

TCP 探测只代表 **CNHZ → relay TCP 端口** 可达。耗时包含主机名解析及 TCP connect，不代表 RustDesk 握手或客户端端到端质量。`status` 中“上次决策原因”来自最近一次 `auto` / `switch` / `reconcile`，`probe` 不重新决策。

## 可选本机 WebUI（试验版）

共享 Python 后端增加 WebUI，固定监听 `127.0.0.1`，默认端口 `8765`。支持状态、探测、手动固定、恢复自动与 INI 编辑；没有独立账户体系。

```bash
# 开发时先复制本地配置，避免改动示例；已有 local.ini 时跳过复制。
test -e config/local.ini || cp config/relay-helper.example.ini config/local.ini
python3 -m relay_helper --config config/local.ini --state var/state.json web
# 已安装的生产路径：sudo rustdesk-relay-helper web
```

首次启动生成权限 `0600` 的 `<状态目录>/webui.token`，交互终端输出 `http://127.0.0.1:8765/?token=...`。裸地址和错误 token 均拒绝返回页面。进入后地址栏清除 token，API 改用 bearer；刷新页面须重新打开原始 token 链接。启动 WebUI 不会启用自动 timer。

完成上面的 Linux 安装并配置真实节点后，可由 systemd 常驻运行 WebUI，无需每次手动启动。先结束占用同一端口的前台 WebUI，然后在 `/opt/rustdesk-relay-helper` 执行：

```bash
sudo install -m 0644 deploy/systemd/rustdesk-relay-helper-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rustdesk-relay-helper-web.service
sudo systemctl status rustdesk-relay-helper-web.service --no-pager
```

该服务开机自动启动、失败后自动重启，仍只监听 `127.0.0.1:8765`，独立于 relay 自动选择 timer。重启沿用已有 token；systemd journal 只记录监听地址和 token 文件位置，不输出完整 token。查看服务日志和读取 token：

```bash
sudo journalctl -u rustdesk-relay-helper-web.service -n 50 --no-pager
sudo cat /var/lib/rustdesk-relay-helper/webui.token
```

token 文件属于服务运行用户（默认 root），权限为 `0600`；读取到的内容就是首次登录凭据。远程访问时，在个人电脑建立 SSH 转发并保持连接：

```bash
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 user@server
```

随后在个人电脑浏览器打开 `http://127.0.0.1:8765/`，输入 token 并点击登录。原有 `/?token=<文件内容>` 链接仍可打开登录确认页，清理地址后需显式登录，不自动创建会话。

登录后刷新无需再次输入 token；默认最长 8 小时，可勾选“记住登录”（最多 7 天）。两种会话都不会随访问续期，Web 服务重启后全部失效，必须重新输入 token；原始 token 文件仍跨重启保留。页面“退出登录”会撤销当前会话并清除 Cookie。关闭浏览器或 SSH 隧道不保证退出。更换本地端口或主机名需重新登录。

停止常驻服务并取消开机启动使用 `sudo systemctl disable --now rustdesk-relay-helper-web.service`，不会停止 relay 自动选择 timer。会话与 SSH 安全边界见 [登录持久化设计](docs/Web登录持久化设计.md)。

配置保存共用 CLI 的锁，先校验与检查版本，上一版保存在 `<配置>.webui.bak`。token 轮换与完整安全边界见 [WebUI 方案](docs/WebUI方案.md)。

“节点与策略配置”默认使用 GUI 表单，提供策略数字输入、节点 ID / 地址 / 层级、启用与 RTT 排名开关，以及节点增删和顺序调整。可切换到 INI 文本编辑，两种方式双向映射同一份草稿；切换和预览均不保存。现有配置节的注释与未修改字段保留，最终统一使用“校验并保存”提交。非法 INI 不能映射到表单，但原文仍保留供修正。

## 首次启用自动执行

先核实服务器版本及 `rs` 只读输出，替换真实节点，确认客户端 Relay Server 留空。按规划先用真实客户端验证目标 relay，再执行 `probe`（新节点一轮完整成功即可，故障节点需满足恢复条件）和首次 `switch`，确认新建中继会话的实际路径。只有现场写入及客户端验证通过后，再启用 timer：

```bash
sudo rustdesk-relay-helper auto
sudo systemctl enable --now rustdesk-relay-helper.timer
sudo systemctl list-timers rustdesk-relay-helper.timer
```

timer 默认在每次任务完成约 20 秒后再次执行；INI 中 `probe_interval_seconds` 是运维期望间隔，Python 单次命令不会循环或修改 systemd。若改间隔，需要同时用 `sudo systemctl edit rustdesk-relay-helper.timer` 配置：

```ini
[Timer]
OnUnitInactiveSec=30s
```

然后执行 `sudo systemctl daemon-reload` 与 `sudo systemctl restart rustdesk-relay-helper.timer`。进程向本机 syslog/journal 写入探测、模式、切换与错误记录，不保存历史数据库；无 `/dev/log` 的开发环境输出到 stderr。

停止自动执行使用 `sudo systemctl stop rustdesk-relay-helper.timer`；正在执行的单次 service 仍会完成。如需完全停止管理器，再停止 `rustdesk-relay-helper.service`。停止不会清空 `hbbs` 的 relay 值。`hbbs` 重启丢失运行时列表后，后续 reconcile 在健康条件满足时重新应用目标。

## 验证边界

本地测试覆盖策略序列、状态持久化、锁互斥及 TCP 假控制台的空响应、超时、延迟生效和错误列表。GitHub Actions 配置为 Linux / Python 3.9、3.12。真实服务器的 `rs` 行为、systemd 服务运行及两端中继会话仍需按 [实施规划](docs/实施规划.md) 现场验收。
