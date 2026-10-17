"""Windows 专用：用 WSARecvMsg / WSASendMsg + IP_PKTINFO 识别并指定收发网口。

为什么需要它（这是"选了指定网口"能真正生效的关键）：
  DHCP 客户端上线时用受限广播 255.255.255.255 发 DISCOVER，
  在 Windows 上只有绑定 0.0.0.0 的 socket 才收得到广播。
  但绑定 0.0.0.0 后，普通 recvfrom 无法区分"这个广播包是从哪块网卡进来的"。

  解决办法：用 WSARecvMsg 收包，它的控制消息里带 IN_PKTINFO，
  其中 ipi_ifindex 就是"接收网口的索引"。据此即可丢弃不属于目标网卡的包，
  实现"只服务指定网卡"。回包时再用 WSASendMsg + IN_PKTINFO 强制从指定网口、
  以指定源 IP 发出，确保广播回到正确的那块网卡。

依赖：仅 Windows + ctypes（Python 标准库），无第三方包。
"""
import sys
import socket
import struct

import ctypes
import ctypes.wintypes as wt

# ---------------- 常量 ----------------
AF_INET = 2
IPPROTO_IP = 0
IP_PKTINFO = 19  # Windows 下 IP_PKTINFO 的值
# SIO_GET_EXTENSION_FUNCTION_POINTER = _WSAIOW(IOC_WS2, 1)
SIO_GET_EXTENSION_FUNCTION_POINTER = 0x88000001

# 扩展函数 GUID
WSAID_WSARECVMSG = struct.pack(
    "<LHH8B",
    0xF689D7C8, 0x6F1F, 0x436B,
    0x8A, 0x53, 0xE5, 0x4A, 0x35, 0x1C, 0x67, 0x39,
)
WSAID_WSASENDMSG = struct.pack(
    "<LHH8B",
    0xA8C9A0E8, 0xFF00, 0x11D9,
    0x8C, 0x02, 0x00, 0x0B, 0x2E, 0xE4, 0x05, 0x7A,
)


# ---------------- 结构体 ----------------
class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_short),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_ubyte * 8),
    ]


class WSABUF(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ulong),
        ("buf", ctypes.c_void_p),
    ]


class WSAMSG(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_void_p),
        ("namelen", ctypes.c_int),
        ("lpBuffers", ctypes.c_void_p),
        ("dwBufferCount", ctypes.c_ulong),
        ("Control", WSABUF),
        ("dwFlags", ctypes.c_ulong),
    ]


class IN_PKTINFO(ctypes.Structure):
    _fields_ = [
        ("ipi_addr", ctypes.c_ubyte * 4),   # 接收网口的本机 IP（收）/ 源 IP（发）
        ("ipi_ifindex", ctypes.c_ulong),    # 接收网口索引（收）/ 指定发送网口（发）
        ("ipi_sottl", ctypes.c_ubyte),
    ]


class WSACMSGHDR(ctypes.Structure):
    _fields_ = [
        ("cmsg_len", ctypes.c_size_t),
        ("cmsg_level", ctypes.c_int),
        ("cmsg_type", ctypes.c_int),
    ]


# ---------------- 函数指针类型 ----------------
WSARecvMsg_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,                 # 返回 0 成功 / -1 失败
    ctypes.c_void_p,              # SOCKET s
    ctypes.c_void_p,              # LPWSAMSG lpMsg
    ctypes.POINTER(ctypes.c_ulong),  # LPDWORD lpdwNumberOfBytesRecvd
    ctypes.c_void_p,              # LPWSAOVERLAPPED
    ctypes.c_void_p,              # LPWSAOVERLAPPED_COMPLETION_ROUTINE
)
WSASendMsg_t = ctypes.WINFUNCTYPE(
    ctypes.c_int,                 # 返回 0 成功 / -1 失败
    ctypes.c_void_p,              # SOCKET s
    ctypes.c_void_p,              # LPWSAMSG lpMsg
    ctypes.c_ulong,               # DWORD dwFlags
    ctypes.POINTER(ctypes.c_ulong),  # LPDWORD lpdwNumberOfBytesSent
    ctypes.c_void_p,              # LPWSAOVERLAPPED
    ctypes.c_void_p,              # LPWSAOVERLAPPED_COMPLETION_ROUTINE
)

# ---------------- 模块状态 ----------------
AVAILABLE = False
_recvmsg = None
_sendmsg = None
_ws2 = None


def _resolve():
    """解析 WSARecvMsg / WSASendMsg 函数指针（仅解析一次）。"""
    global AVAILABLE, _recvmsg, _sendmsg, _ws2
    if _recvmsg is not None or _sendmsg is not None:
        return AVAILABLE
    try:
        _ws2 = ctypes.windll.ws2_32
        _ws2.WSAIoctl.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        _ws2.WSAIoctl.restype = ctypes.c_int

        # 1) 标准方式：WSAIoctl(SIO_GET_EXTENSION_FUNCTION_POINTER)
        recv_ptr = _get_extension(WSAID_WSARECVMSG)
        send_ptr = _get_extension(WSAID_WSASENDMSG)
        # 2) 兜底：Vista+ 的 ws2_32 直接按名导出，用 GetProcAddress 取
        if not recv_ptr:
            recv_ptr = _get_proc("WSARecvMsg")
        if not send_ptr:
            send_ptr = _get_proc("WSASendMsg")

        if recv_ptr and send_ptr:
            _recvmsg = WSARecvMsg_t(int(recv_ptr))
            _sendmsg = WSASendMsg_t(int(send_ptr))
            AVAILABLE = True
        else:
            AVAILABLE = False
    except Exception as e:
        print(f"[RAW ] 解析扩展函数失败，将退化为全网口模式: {e}")
        AVAILABLE = False
    return AVAILABLE


def _get_proc(name):
    """从 ws2_32.dll 按导出名取函数地址（部分 Windows 版本导出 WSARecvMsg/WSASendMsg）。"""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        k32.GetProcAddress.restype = ctypes.c_void_p
        h = k32.GetModuleHandleW("ws2_32.dll")
        if not h:
            return None
        return k32.GetProcAddress(h, name.encode("ascii"))
    except Exception:
        return None


def _get_extension(guid_bytes):
    """用 WSAIoctl(SIO_GET_EXTENSION_FUNCTION_POINTER) 取得扩展函数指针。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        out = ctypes.c_void_p()
        in_buf = ctypes.create_string_buffer(guid_bytes, len(guid_bytes))
        cb = ctypes.c_ulong()
        rc = _ws2.WSAIoctl(
            s.fileno(),
            SIO_GET_EXTENSION_FUNCTION_POINTER,
            in_buf, len(guid_bytes),
            ctypes.byref(out), ctypes.sizeof(ctypes.c_void_p),
            ctypes.byref(cb),
            None, None,
        )
        if rc != 0:
            return None
        return out
    finally:
        s.close()


def _parse_pktinfo(ctrl_buf, ctrl_len):
    """从控制缓冲区解析 IN_PKTINFO，返回 (ifindex, dest_ip_str)。"""
    i = 0
    n = ctrl_len
    hdr_size = ctypes.sizeof(WSACMSGHDR)
    while i + hdr_size <= n:
        hdr = WSACMSGHDR.from_buffer_copy(
            ctrl_buf[i:i + hdr_size]
        )
        if hdr.cmsg_level == IPPROTO_IP and hdr.cmsg_type == IP_PKTINFO:
            off = i + hdr_size
            pkt = IN_PKTINFO.from_buffer_copy(
                ctrl_buf[off:off + ctypes.sizeof(IN_PKTINFO)]
            )
            ip = ".".join(str(b) for b in pkt.ipi_addr)
            return pkt.ipi_ifindex, ip
        # 指向下一个 cmsg（按 sizeof(SIZE_T)=8 对齐）
        i += hdr.cmsg_len
        i = (i + 7) & ~7
    return None, None


def recv_msg(sock_fd, bufsize=4096):
    """接收一帧，返回 (data_bytes, ifindex, dest_ip)。失败返回 (None, None, None)。"""
    if not _resolve() or _recvmsg is None:
        return None, None, None

    data = ctypes.create_string_buffer(bufsize)
    wsa_buf = WSABUF(bufsize, ctypes.cast(data, ctypes.c_void_p))
    addr = SOCKADDR_IN()
    ctrl_buf = ctypes.create_string_buffer(1024)
    ctrl = WSABUF(1024, ctypes.cast(ctrl_buf, ctypes.c_void_p))

    msg = WSAMSG()
    msg.name = ctypes.cast(ctypes.byref(addr), ctypes.c_void_p)
    msg.namelen = ctypes.sizeof(SOCKADDR_IN)
    msg.lpBuffers = ctypes.cast(ctypes.byref(wsa_buf), ctypes.c_void_p)
    msg.dwBufferCount = 1
    msg.Control = ctrl
    msg.dwFlags = 0

    bytes_recv = ctypes.c_ulong()
    rc = _recvmsg(
        sock_fd, ctypes.byref(msg), ctypes.byref(bytes_recv), None, None
    )
    if rc != 0:
        return None, None, None
    ifindex, dest_ip = _parse_pktinfo(ctrl_buf, msg.Control.len)
    return data[:bytes_recv.value], ifindex, dest_ip


def send_msg(sock_fd, data, dest_ip, dest_port, ifindex, src_ip):
    """发送一帧到 dest_ip:dest_port，强制从 ifindex 网口、以 src_ip 为源发出。
       返回是否成功（bool）。
    """
    if not _resolve() or _sendmsg is None:
        return False

    # 构造 IN_PKTINFO 控制数据
    pkt = IN_PKTINFO()
    for i, part in enumerate(int(x) for x in src_ip.split(".")):
        pkt.ipi_addr[i] = part
    pkt.ipi_ifindex = ifindex
    pkt.ipi_sottl = 0

    hdr = WSACMSGHDR()
    hdr.cmsg_len = ctypes.sizeof(WSACMSGHDR) + ctypes.sizeof(IN_PKTINFO)
    hdr.cmsg_level = IPPROTO_IP
    hdr.cmsg_type = IP_PKTINFO

    pkt_off = (ctypes.sizeof(WSACMSGHDR) + 7) & ~7
    ctrl = ctypes.create_string_buffer(pkt_off + ctypes.sizeof(IN_PKTINFO) + 8)
    ctypes.memmove(ctrl, ctypes.byref(hdr), ctypes.sizeof(WSACMSGHDR))
    ctypes.memmove(
        ctypes.byref(ctrl, pkt_off), ctypes.byref(pkt), ctypes.sizeof(IN_PKTINFO)
    )
    ctrl_wsa = WSABUF(hdr.cmsg_len, ctypes.cast(ctrl, ctypes.c_void_p))

    # 发送数据缓冲
    send_data = ctypes.create_string_buffer(data, len(data))
    wsa_buf = WSABUF(len(data), ctypes.cast(send_data, ctypes.c_void_p))

    # 目标地址
    dst = SOCKADDR_IN()
    dst.sin_family = AF_INET
    dst.sin_port = struct.pack(">H", dest_port)  # 端口必须是网络字节序（大端）
    for i, part in enumerate(int(x) for x in dest_ip.split(".")):
        dst.sin_addr[i] = part

    msg = WSAMSG()
    msg.name = ctypes.cast(ctypes.byref(dst), ctypes.c_void_p)
    msg.namelen = ctypes.sizeof(SOCKADDR_IN)
    msg.lpBuffers = ctypes.cast(ctypes.byref(wsa_buf), ctypes.c_void_p)
    msg.dwBufferCount = 1
    msg.Control = ctrl_wsa
    msg.dwFlags = 0

    sent = ctypes.c_ulong()
    rc = _sendmsg(
        sock_fd, ctypes.byref(msg), 0, ctypes.byref(sent), None, None
    )
    return rc == 0


# 非 Windows 平台直接标记为不可用（本工具仅面向 Windows）
if sys.platform != "win32":
    AVAILABLE = False
    _recvmsg = None
    _sendmsg = None
