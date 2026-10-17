"""Windows 网口枚举（零依赖）。

提供本机 IPv4 网口列表，供 Web 界面做"选择网口"下拉框使用。

枚举策略（优先级从高到低，任一成功即用）：
  1. ctypes 调用系统 API GetAdaptersInfo（iphlpapi.dll）——最稳，不依赖 PowerShell/netsh，
     企业/受限环境也能用。
  2. PowerShell Get-NetIPAddress——信息最全（含友好名）。
  3. netsh interface ipv4 show address——兜底。
"""
import subprocess
import json
import re
import ctypes
import ctypes.wintypes as wt


def prefix_to_netmask(prefix):
    """把掩码前缀长度（如 24）转成点分十进制（如 255.255.255.0）。"""
    try:
        p = int(prefix)
    except (TypeError, ValueError):
        return "255.255.255.0"
    if p < 0 or p > 32:
        return "255.255.255.0"
    mask = (0xFFFFFFFF << (32 - p)) & 0xFFFFFFFF
    return ".".join(str((mask >> s) & 0xFF) for s in (24, 16, 8, 0))


def mask_to_prefix(mask):
    """点分掩码 -> 前缀长度（如 255.255.255.0 -> 24）。"""
    try:
        parts = mask.split(".")
        bits = sum(bin(int(x)).count("1") for x in parts)
        return bits
    except Exception:
        return 24


def _is_usable_ip(ip):
    """排除环回/APIPA/零地址，这些地址不适合做 DHCP 服务网口。"""
    if not ip:
        return False
    if ip.startswith("127.") or ip == "0.0.0.0":
        return False
    # APIPA 自动配置地址（DHCP 失败时分配）不适合做服务端
    if ip.startswith("169.254."):
        return False
    return True


# ---------------- 主路径：系统 API GetAdaptersInfo ----------------
def _win_get_adapters_info():
    """通过 iphlpapi!GetAdaptersInfo 枚举网口（最可靠，无需 PowerShell/netsh）。"""
    try:
        iphlpapi = ctypes.windll.iphlpapi

        class IP_ADDR_STRING(ctypes.Structure):
            pass
        IP_ADDR_STRING._fields_ = [
            ("Next", ctypes.POINTER(IP_ADDR_STRING)),
            ("IpAddress", ctypes.c_char * 16),
            ("IpMask", ctypes.c_char * 16),
            ("Context", ctypes.c_ulong),
        ]

        class IP_ADAPTER_INFO(ctypes.Structure):
            pass
        IP_ADAPTER_INFO._fields_ = [
            ("Next", ctypes.POINTER(IP_ADAPTER_INFO)),
            ("ComboIndex", ctypes.c_ulong),
            ("AdapterName", ctypes.c_char * 260),
            ("Description", ctypes.c_char * 132),
            ("AddressLength", ctypes.c_ulong),
            ("Address", ctypes.c_ubyte * 8),
            ("Index", ctypes.c_ulong),
            ("Type", ctypes.c_ulong),
            ("DhcpEnabled", ctypes.c_ulong),
            ("CurrentIpAddress", ctypes.POINTER(IP_ADDR_STRING)),
            ("IpAddressList", IP_ADDR_STRING),
            ("GatewayList", IP_ADDR_STRING),
            ("DhcpServer", IP_ADDR_STRING),
            ("HaveWins", ctypes.c_byte),
            ("PrimaryWinsServer", IP_ADDR_STRING),
            ("SecondaryWinsServer", IP_ADDR_STRING),
            ("LeaseObtained", ctypes.c_long),
            ("LeaseExpires", ctypes.c_long),
        ]

        buf_len = ctypes.c_ulong(0)
        # 第一次调用拿所需缓冲区大小（返回 111 = ERROR_BUFFER_OVERFLOW）
        rc = iphlpapi.GetAdaptersInfo(None, ctypes.byref(buf_len))
        if rc != 0 and rc != 111:
            return None
        raw = ctypes.create_string_buffer(buf_len.value)
        pinfo = ctypes.cast(raw, ctypes.POINTER(IP_ADAPTER_INFO))
        rc = iphlpapi.GetAdaptersInfo(pinfo, ctypes.byref(buf_len))
        if rc != 0:
            return None

        result = []
        info = pinfo.contents
        while True:
            addr = info.IpAddressList
            base_name = info.Description.decode("ascii", "ignore").strip() \
                or info.AdapterName.decode("ascii", "ignore").strip()
            while True:
                ip = addr.IpAddress.decode("ascii", "ignore").strip()
                mask = addr.IpMask.decode("ascii", "ignore").strip()
                if ip and _is_usable_ip(ip):
                    result.append({
                        "name": base_name or ip,
                        "ip": ip,
                        "prefix": mask_to_prefix(mask) if mask else None,
                        "netmask": mask or prefix_to_netmask(None),
                        "ifindex": int(info.Index),
                    })
                if not addr.Next:
                    break
                addr = addr.Next.contents
            if not info.Next:
                break
            info = info.Next.contents
        return result
    except Exception as e:
        print(f"[NET ] GetAdaptersInfo 失败: {e}")
        return None


# ---------------- 兜底：PowerShell ----------------
def _ps_list_interfaces():
    """PowerShell 方式枚举网口。"""
    ps = (
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "Get-NetIPAddress -AddressFamily IPv4 "
        "| Where-Object {$_.InterfaceAlias -notmatch 'Loopback' "
        "-and $_.IPAddress -ne '127.0.0.1'} "
        "| Select-Object InterfaceAlias, IPAddress, PrefixLength, InterfaceIndex "
        "| ConvertTo-Json"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=15,
    )
    return out


def _ps_interfaces():
    out = _ps_list_interfaces()
    if out.returncode != 0:
        print(f"[NET ] PowerShell 枚举网口返回非零：{out.stderr[:200]}")
        return None
    if not out.stdout or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        data = [data]
    result = []
    for it in data:
        ip = it.get("IPAddress")
        if not _is_usable_ip(ip):
            continue
        prefix = it.get("PrefixLength")
        ifindex = it.get("InterfaceIndex")
        try:
            ifindex = int(ifindex) if ifindex not in (None, "") else None
        except (TypeError, ValueError):
            ifindex = None
        name = it.get("InterfaceAlias") or ip
        result.append({
            "name": name,
            "ip": ip,
            "prefix": prefix,
            "netmask": prefix_to_netmask(prefix),
            "ifindex": ifindex,
        })
    return result or None


# ---------------- 兜底：netsh ----------------
def _netsh_list_interfaces():
    """netsh 方式枚举网口。"""
    idx_map = {}
    try:
        r = subprocess.run(
            ["netsh", "interface", "ipv4", "show", "interfaces"],
            capture_output=True, text=True, timeout=15,
        )
        for line in r.stdout.splitlines():
            m = re.match(r"^\s*(\d+)\s+\d+\s+\d+\s+\S+\s+(.+?)\s*$", line)
            if m:
                idx_map[m.group(2).strip()] = int(m.group(1))
    except Exception as e:
        print(f"[NET ] netsh 索引表获取失败: {e}")

    result = []
    try:
        r = subprocess.run(
            ["netsh", "interface", "ipv4", "show", "address"],
            capture_output=True, text=True, timeout=15,
        )
        current_name = None
        current_ip = None
        current_prefix = None
        last_ip = None
        for line in r.stdout.splitlines():
            name_m = re.search(r'["\']([^"\']+)["\']', line)
            if name_m and ("configuration" in line.lower() or "配置" in line
                           or "Interface" in line or line.strip().startswith('"')):
                if current_name and current_ip:
                    result.append({
                        "name": current_name, "ip": current_ip,
                        "prefix": current_prefix,
                        "netmask": prefix_to_netmask(current_prefix),
                        "ifindex": idx_map.get(current_name),
                    })
                current_name = name_m.group(1)
                current_ip = None
                current_prefix = None
                last_ip = None
                continue
            mask_m = re.search(r"(?:[Mm]ask|掩码)\s+(\d+\.\d+\.\d+\.\d+)", line)
            if mask_m:
                parts = [int(x) for x in mask_m.group(1).split(".")]
                current_prefix = sum(bin(x).count("1") for x in parts)
                if last_ip:
                    current_ip = last_ip
                continue
            ips = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", line)
            if len(ips) == 1 and ips[0] != "127.0.0.1":
                last_ip = ips[0]
        if current_name and current_ip:
            result.append({
                "name": current_name, "ip": current_ip,
                "prefix": current_prefix,
                "netmask": prefix_to_netmask(current_prefix),
                "ifindex": idx_map.get(current_name),
            })
    except Exception as e:
        print(f"[NET ] netsh 地址表获取失败: {e}")
    return result


def _netsh_interfaces():
    result = [r for r in _netsh_list_interfaces() if _is_usable_ip(r.get("ip"))]
    return result or None


def list_interfaces():
    """返回 IPv4 网口列表：[{name, ip, prefix, netmask, ifindex}]，排除环回/APIPA。"""
    # 1) 系统 API（最稳）
    try:
        result = _win_get_adapters_info()
        if result:
            print(f"[NET ] GetAdaptersInfo 枚举到 {len(result)} 个可用网口")
            return result
        print("[NET ] GetAdaptersInfo 无结果，尝试 PowerShell...")
    except Exception as e:
        print(f"[NET ] GetAdaptersInfo 异常: {e}，尝试 PowerShell...")

    # 2) PowerShell
    result = _ps_interfaces()
    if result:
        print(f"[NET ] PowerShell 枚举到 {len(result)} 个可用网口")
        return result

    # 3) netsh
    result = _netsh_interfaces()
    if result:
        print(f"[NET ] netsh 枚举到 {len(result)} 个可用网口")
        return result

    print("[NET ] 所有枚举方式均无结果，未能检测到可用网口")
    return []


def ifindex_by_ip(ip):
    """根据 IPv4 地址反查网口索引，找不到返回 None。"""
    for it in list_interfaces():
        if it.get("ip") == ip:
            return it.get("ifindex")
    return None
