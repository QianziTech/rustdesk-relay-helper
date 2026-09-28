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
sudo rustdesk-relay-helper status
sudo rustdesk-relay-helper nodes
sudo rustdesk-relay-helper probe
sudo rustdesk-relay-helper switch usla
sudo rustdesk-relay-helper auto
sudo rustdesk-relay-helper history --lines 50
```

| 命令 | 行为 |
| --- | --- |
| `status` | 实时读取 `rs`，展示模式、实际值、目标、上次决策原因、时间和节点详情；不探测、不写 relay。读取失败显示未确认并返回非零退出码。 |
| `nodes` | 展示配置与已有探测结果，不联系 `hbbs`。 |
| `probe` | 探测所有启用节点并保存健康计数和有限 RTT 样本，不读取或写入 `hbbs`。 |
| `switch <id>` | 先探测，再要求目标已健康，持久化手动模式并应用目标。写后未确认时返回非零，保留手动意图供下次重试。 |
| `auto` | 先探测，恢复自动模式并立即决策。 |
| `history` | 用 journal 标识 `rustdesk-relay-helper` 读取最近事件；读取 journal 可能需要管理员权限。 |
| `reconcile` | 定时内部命令：探测、读取实际值、按当前模式决策、必要时写入并回读确认。 |

所有状态操作使用同一把非阻塞文件锁，冲突返回“管理器忙”。默认配置和状态路径分别为 `/etc/rustdesk-relay-helper/config.ini`、`/var/lib/rustdesk-relay-helper/state.json`。可在子命令前传入 `--config`、`--state`，或设置 `RELAY_HELPER_CONFIG`、`RELAY_HELPER_STATE`。同一实例的手动命令和 timer 必须共用状态路径。Bash 入口的安装目录可由 `RELAY_HELPER_HOME` 修改。

初次健康未知，默认累计 6 次成功才确认恢复，RTT 排名还需满 5 个样本；3 次连续失败才判定不可用。健康节点间的主动切换受 300 秒保持期限制，同层 RTT 改善需至少 20 ms；故障逃离不等待保持期。恢复后的 RTT 样本重新积累。所有节点不可用时保留实际列表，不写空值。手动节点失败只提示，不自动切走。

TCP 探测只代表 **CNHZ → relay TCP 端口** 可达。耗时包含主机名解析及 TCP connect，不代表 RustDesk 握手或客户端端到端质量。`status` 中“上次决策原因”来自最近一次 `auto` / `switch` / `reconcile`，`probe` 不重新决策。

## 首次启用自动执行

先核实服务器版本及 `rs` 只读输出，替换真实节点，确认客户端 Relay Server 留空。按规划先用真实客户端验证目标 relay，再执行 `probe`（默认需累计 6 个成功周期）和首次 `switch`，确认新建中继会话的实际路径。只有现场写入及客户端验证通过后，再启用 timer：

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
