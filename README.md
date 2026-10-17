# win-dhcp-server

一个**小巧简洁**的 Windows DHCP 分发服务小工具，纯 Python 标准库实现，零第三方依赖。
内置 Web 管理页，可在线选择网口、编辑分配参数，并支持**严格单网口隔离**模式。

## 功能特性

- 最小可用的 DHCP 服务：Discover → Offer → Request → Ack / Nak，支持 Release
- IPv4 地址池 + 租约管理，租约落盘 `leases.json`，重启不丢
- 内置 Web 管理页（实时查看租约、编辑 IP 池 / 网关 / DNS / 租期）
- **选择网口**：枚举本机网口并自动填充子网与推荐地址池
- **严格单网口隔离**：通过 Windows `WSARecvMsg` / `WSASendMsg` + `IP_PKTINFO`
  识别并指定收发网口，真正实现"只服务指定网卡"（winsock 不可用时自动退化为全网口）
- 网口枚举优先使用系统 API `GetAdaptersInfo`（不依赖 PowerShell / netsh，企业受限环境也能用）

## 目录结构

```
dhcp_tool/
├── main.py          # 入口：启动 DHCP 后台线程 + Web 管理页
├── dhcpserver.py    # UDP 广播收发 + DHCP 状态机 + 严格单网口模式
├── leasepool.py     # IP 地址池与租约持久化
├── netutil.py       # 网口枚举（GetAdaptersInfo / PowerShell / netsh 三级兜底）
├── winsock_raw.py   # ctypes 封装 WSARecvMsg / WSASendMsg + IP_PKTINFO
├── webui.py         # 标准库 http.server 管理页
├── config.json      # 配置（地址池 / 掩码 / 网关 / DNS / 租期 / 网口）
├── build_exe.py     # 一键打包成 Windows 单文件 exe（PyInstaller）
├── test_dhcp.py     # 逻辑自测（地址池 + 报文编解码 + winsock 结构校验）
└── leases.json      # 运行时生成，勿提交（已在 .gitignore）
```

## 使用方法

1. **以管理员身份**打开命令行（绑定特权端口 67 必须）。
2. 放行 Windows 防火墙 **UDP 67 / 68 入站**。
3. 启动：
   ```bash
   python main.py
   ```
4. 浏览器打开 `http://127.0.0.1:8080`：
   - 选「选择网口」下拉框中的真实网卡，自动填充 IP / 掩码与推荐地址池；
   - 点「保存并应用」立即生效（无需重启程序）；
   - 在「当前租约」中实时查看已分配的 IP。

> 注意：必须与同网段现有路由器自带的 DHCP 服务**错开地址段**，否则会抢 IP。

## 方式一：直接下载 exe（免装 Python）

在仓库 **Releases** 页面下载 `win-dhcp-server.exe`（已内嵌默认配置，单文件）。
**右键 → 以管理员身份运行**，随后浏览器打开 `http://127.0.0.1:8080` 即可使用。
首次运行会在 exe 同目录生成 `config.json`（可手动编辑）与 `leases.json`（租约数据）。

- 默认已请求管理员权限（UAC 提权），以便绑定特权端口 67。
- 若被 Windows 防火墙拦截，放行 **UDP 67 / 68 入站** 即可。

## 方式二：从源码运行 / 自行打包

### 运行源码
```bash
pip install pyinstaller     # 仅打包时需要
python main.py
```

### 打包成单文件 exe
```bash
python build_exe.py
```
产物位于 `dist/win-dhcp-server.exe`（已带 `--uac-admin` 提权清单）。
`--onefile` 生成单个可执行文件，`config.json` 会随 exe 一起分发，首次运行自动复制到 exe 同目录。

## 严格单网口模式说明

Windows 上 DHCP 客户端用受限广播 `255.255.255.255` 发包，只有绑定 `0.0.0.0` 的
socket 能收到广播。本工具始终监听 `0.0.0.0`，再通过 `IP_PKTINFO` 拿到每个广播包的
**接收网口索引**，丢弃非目标网口的包，并以指定网口 + 本网口 IP 为源回包，从而
真正做到只服务所选网卡。选择「全部网口」则为普通全网口模式。

## 自测

```bash
python test_dhcp.py
```

## 免责声明

本工具仅用于你对**拥有合法权限**的网络环境进行运维学习/测试，请勿在未经授权的网络中使用。
