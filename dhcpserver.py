"""DHCP 服务端：UDP 广播收发 + DHCP 协议状态机。

协议流程（最小可用）：
  DISCOVER(1) -> OFFER(2)
  REQUEST (3) -> ACK(5) / NAK(6)
  RELEASE (7) -> 释放租约

注意（Windows 关键点）：
  1. 必须以管理员身份运行，否则无法绑定特权端口 67。
  2. 需放行 Windows 防火墙 UDP 67/68 入站。
  3. 同一网段不要和路由器自带的 DHCP 服务冲突。
"""
import socket
import struct
import threading

import ipaddress

import winsock_raw

MAGIC = bytes([99, 130, 83, 99])  # DHCP magic cookie

# DHCP 报文类型（option 53）
DHCP_DISCOVER = 1
DHCP_OFFER = 2
DHCP_REQUEST = 3
DHCP_ACK = 5
DHCP_NAK = 6
DHCP_RELEASE = 7

MSG_NAME = {
    DHCP_DISCOVER: "DISCOVER",
    DHCP_OFFER: "OFFER",
    DHCP_REQUEST: "REQUEST",
    DHCP_ACK: "ACK",
    DHCP_NAK: "NAK",
    DHCP_RELEASE: "RELEASE",
}


def mac_to_bytes(mac_str):
    return bytes(int(x, 16) for x in mac_str.split(":"))


def bytes_to_mac(raw):
    return ":".join(f"{b:02x}" for b in raw[:6])


def parse_options(data):
    """从偏移 240 开始解析 DHCP options。"""
    options = {}
    i = 240
    n = len(data)
    while i < n:
        tag = data[i]
        i += 1
        if tag == 0:       # pad
            continue
        if tag == 255:     # end
            break
        if i >= n:
            break
        length = data[i]
        i += 1
        options[tag] = data[i:i + length]
        i += length
    return options


def build_options(opt_list):
    out = bytearray(MAGIC)
    for tag, val in opt_list:
        out.append(tag)
        out.append(len(val))
        out.extend(val)
    out.append(255)  # end
    while len(out) % 4 != 0:   # 4 字节对齐，部分客户端要求
        out.append(0)
    return bytes(out)


class DHCPServer:
    def __init__(self, config, pool):
        self.config = config
        self.pool = pool
        self.sock = None
        self.running = False
        # 是否"只服务指定网口"（strict 模式）。
        # 条件：interface 不是 0.0.0.0、且明确配置了有效的网口索引、且 winsock 扩展可用。
        self.strict = False
        self.ifindex = 0
        self.src_ip = config.get("server_ip", "0.0.0.0")

    def _decide_mode(self):
        """根据配置决定运行模式：严格单网口 / 全网口。"""
        self.strict = False
        self.ifindex = 0
        self.src_ip = self.config.get("server_ip", "0.0.0.0")

        sel = self.config.get("interface", "0.0.0.0")
        ifindex = self.config.get("interface_index", 0)
        if sel and sel != "0.0.0.0" and ifindex:
            if not winsock_raw.AVAILABLE:
                print("[DHCP] 警告：当前 Windows 不支持 WSARecvMsg，"
                      "无法严格隔离网口，将退化为全网口监听模式。")
                return
            self.strict = True
            self.ifindex = int(ifindex)
            self.src_ip = sel   # 回包源 IP 取该网口自身 IP
            print(f"[DHCP] 严格单网口模式：仅响应网口索引 {self.ifindex} "
                  f"({sel}) 的 DHCP 请求")

    def start(self):
        # Windows 下 DHCP 客户端用受限广播 255.255.255.255 发包，
        # 只有绑定到 0.0.0.0 的 socket 才能收到广播；
        # 绑定到具体网卡 IP 会收不到 DISCOVER（这是之前"选了网口反而不能用"的根因）。
        # 因此始终监听 0.0.0.0，再用 IP_PKTINFO 识别"包从哪块网卡进来"，实现单网口隔离。
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("0.0.0.0", 67))
        except PermissionError:
            raise PermissionError(
                "绑定 67 端口失败：请【以管理员身份运行】本程序。"
            )
        except OSError as e:
            raise OSError(f"绑定 67 端口失败：{e}（检查是否被其他 DHCP 服务占用）")

        self._decide_mode()
        sel = self.config.get("interface", "0.0.0.0")
        mode = "严格单网口" if self.strict else "全网口"
        print(f"[DHCP] 已监听 0.0.0.0:67（{mode}，服务网口：{sel}），"
              f"地址池 {self.config['pool_start']} ~ {self.config['pool_end']}")

        self.running = True
        while self.running:
            try:
                if self.strict:
                    data, pkt_ifindex, _dest = winsock_raw.recv_msg(
                        self.sock.fileno(), 4096
                    )
                    if data is None:
                        if not self.running:
                            break
                        continue
                    # 关键：丢弃来自非目标网口的广播包
                    if pkt_ifindex != self.ifindex:
                        continue
                else:
                    data, _addr = self.sock.recvfrom(4096)
            except OSError:
                if not self.running:   # 主动停止
                    break
                continue
            if not self.running:
                break
            try:
                self.handle(data)
            except Exception as e:  # 单包出错不影响主循环
                print(f"[DHCP] 处理出错: {e}")
        try:
            self.sock.close()
        except OSError:
            pass

    def stop(self):
        """停止服务并关闭 socket（供配置热更新时重启使用）。"""
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    # ---------- 报文处理 ----------
    def handle(self, data):
        if len(data) < 240:
            return
        if data[0] != 1:          # 只处理 bootrequest
            return
        xid = data[4:8]
        flags = struct.unpack(">H", data[10:12])[0]
        chaddr_bytes = data[28:44]
        mac = bytes_to_mac(chaddr_bytes)
        options = parse_options(data)
        msg_type = options.get(53, b"\x00")[0]

        req_ip = None
        if 50 in options:   # 客户端请求的 IP
            req_ip = str(ipaddress.ip_address(struct.unpack(">I", options[50])[0]))
        hostname = ""
        if 12 in options:
            hostname = options[12].decode("utf-8", "ignore")

        if msg_type == DHCP_DISCOVER:
            ip = self.pool.allocate(mac, req_ip, hostname)
            if ip:
                self.send_reply(xid, chaddr_bytes, ip, DHCP_OFFER, flags)
                print(f"[DHCP] DISCOVER {mac} -> 提供 {ip}")

        elif msg_type == DHCP_REQUEST:
            # 若指明了 server id，只响应发给本服务器的请求
            if 54 in options:
                sid = str(ipaddress.ip_address(struct.unpack(">I", options[54])[0]))
                if sid != self.config["server_ip"]:
                    return
            # INIT-REBOOT：客户端用 ciaddr 当请求 IP
            if not req_ip:
                ci = struct.unpack(">I", data[12:16])[0]
                if ci:
                    req_ip = str(ipaddress.ip_address(ci))
            if req_ip and self.pool.ip_available(req_ip, mac):
                self.pool.allocate(mac, req_ip, hostname)
                self.send_reply(xid, chaddr_bytes, req_ip, DHCP_ACK, flags)
                print(f"[DHCP] REQUEST  {mac} -> ACK {req_ip}")
            else:
                self.send_reply(xid, chaddr_bytes, "0.0.0.0", DHCP_NAK, flags)
                print(f"[DHCP] REQUEST  {mac} -> NAK（地址不可用）")

        elif msg_type == DHCP_RELEASE:
            if req_ip:
                self.pool.release(req_ip)
                print(f"[DHCP] RELEASE  {mac} 释放 {req_ip}")

    # ---------- 构造并发送 ----------
    def build_reply(self, xid, chaddr_bytes, yiaddr, msg_type, flags):
        # 固定 BOOTP 头 236 字节，magic cookie 落在偏移 236，options 从 240 起
        packet = bytearray(236)
        packet[0] = 2          # bootreply
        packet[1] = 1          # 以太网
        packet[2] = 6          # hlen
        struct.pack_into(">H", packet, 10, flags)
        packet[4:8] = xid
        struct.pack_into(">I", packet, 16, int(ipaddress.ip_address(yiaddr)))   # yiaddr
        struct.pack_into(">I", packet, 20, int(ipaddress.ip_address(self.config["server_ip"])))  # siaddr
        packet[28:44] = chaddr_bytes

        opts = [
            (53, bytes([msg_type])),                                       # 消息类型
            (54, ipaddress.ip_address(self.config["server_ip"]).packed),   # server id
            (51, struct.pack(">I", self.config["lease_time"])),            # 租期
            (1, ipaddress.ip_address(self.config["netmask"]).packed),      # 子网掩码
            (3, ipaddress.ip_address(self.config["gateway"]).packed),      # 默认网关
        ]
        dns = b"".join(ipaddress.ip_address(d).packed for d in self.config["dns"])
        opts.append((6, dns))                                              # DNS
        packet.extend(build_options(opts))
        while len(packet) < 300:   # 补齐到 300 字节，兼容性更好
            packet.append(0)
        return bytes(packet)

    def send_reply(self, xid, chaddr_bytes, yiaddr, msg_type, flags):
        packet = self.build_reply(xid, chaddr_bytes, yiaddr, msg_type, flags)
        # 客户端尚未有 IP，必须广播回 255.255.255.255:68
        if self.strict and winsock_raw.AVAILABLE:
            # 强制从指定网口、以本网口 IP 为源发出，确保广播回到正确的网卡
            ok = winsock_raw.send_msg(
                self.sock.fileno(), packet,
                "255.255.255.255", 68, self.ifindex, self.src_ip,
            )
            if not ok:
                # 回包失败（如接口已失效），退回普通广播兜底
                try:
                    self.sock.sendto(packet, ("255.255.255.255", 68))
                except OSError:
                    pass
        else:
            self.sock.sendto(packet, ("255.255.255.255", 68))
