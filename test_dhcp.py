"""逻辑自测：不绑定 67 端口，只验证地址池与 DHCP 报文编解码。

运行：python test_dhcp.py
"""
import struct
import ipaddress
import json
import os
import sys
import ctypes

sys.path.insert(0, os.path.dirname(__file__))

from leasepool import LeasePool
from dhcpserver import parse_options, build_options, DHCPServer


def test_pool():
    cfg = {
        "subnet": "192.168.1.0",
        "netmask": "255.255.255.0",
        "pool_start": "192.168.1.100",
        "pool_end": "192.168.1.102",
        "lease_time": 3600,
    }
    pool = LeasePool(cfg, lease_file="leases_test.json")
    # 连续分配 3 个
    a = pool.allocate("aa:bb:cc:dd:ee:01")
    b = pool.allocate("aa:bb:cc:dd:ee:02")
    c = pool.allocate("aa:bb:cc:dd:ee:03")
    assert a == "192.168.1.100", a
    assert b == "192.168.1.101", b
    assert c == "192.168.1.102", c
    # 第 4 个应耗尽
    d = pool.allocate("aa:bb:cc:dd:ee:04")
    assert d is None, d
    # 同 MAC 续租应拿回原 IP
    again = pool.allocate("aa:bb:cc:dd:ee:01", requested_ip="192.168.1.100")
    assert again == "192.168.1.100", again
    # 跨 MAC 请求他人 IP 应被拒
    assert pool.ip_available("192.168.1.101", "aa:bb:cc:dd:ee:09") is False
    # 释放后可再分配
    pool.release("192.168.1.101")
    e = pool.allocate("aa:bb:cc:dd:ee:09")
    assert e == "192.168.1.101", e
    print("✅ 地址池测试通过")
    try:
        os.remove("leases_test.json")
    except OSError:
        pass


def test_packet():
    # 构造一个 DISCOVER 报文，验证解析 + 回包能被重新解析
    cfg = {
        "server_ip": "192.168.1.1",
        "netmask": "255.255.255.0",
        "gateway": "192.168.1.1",
        "dns": ["192.168.1.1", "8.8.8.8"],
        "lease_time": 3600,
    }
    # 伪造 DISCOVER
    pkt = bytearray(300)
    pkt[0] = 1
    pkt[1] = 1
    pkt[2] = 6
    import dhcpserver
    pkt[4:8] = struct.pack(">I", 0x12345678)
    mac = bytes.fromhex("aabbccddeeff")
    pkt[28:34] = mac
    # options: magic cookie 在偏移 236，选项从 240 起
    pkt[236:240] = dhcpserver.MAGIC
    pkt[240] = 53; pkt[241] = 1; pkt[242] = 1
    pkt[243] = 255

    opts = parse_options(bytes(pkt))
    assert opts[53][0] == 1, opts

    srv = DHCPServer(cfg, None)
    offer = srv.build_reply(pkt[4:8], pkt[28:44], "192.168.1.100", 2, 0x8000)
    assert offer[0] == 2
    assert struct.unpack(">I", offer[16:20])[0] == int(ipaddress.ip_address("192.168.1.100"))
    # 回包 options 可解析
    o2 = parse_options(offer)
    assert o2[53][0] == 2            # OFFER
    assert o2[54] == ipaddress.ip_address("192.168.1.1").packed  # server id
    assert o2[1] == ipaddress.ip_address("255.255.255.0").packed  # mask
    print("✅ 报文编解码测试通过")


def test_winsock():
    import winsock_raw as raw
    # 校验关键结构体布局（64 位 Windows）
    assert ctypes.sizeof(raw.SOCKADDR_IN) == 16, ctypes.sizeof(raw.SOCKADDR_IN)
    # Windows IN_PKTINFO: IN_ADDR(4) + UINT ifindex(4) + UCHAR(1) + 对齐填充 = 12
    assert ctypes.sizeof(raw.IN_PKTINFO) == 12, ctypes.sizeof(raw.IN_PKTINFO)
    # 解析扩展函数（Windows 上应可用）
    ok = raw._resolve()
    print(f"✅ winsock 结构体校验通过；WSARecvMsg/WSASendMsg 可用: {ok}")
    if not ok:
        print("   提示：当前环境不支持 WSARecvMsg，将退化为全网口模式（功能仍可用）。")


if __name__ == "__main__":
    test_pool()
    test_packet()
    test_winsock()
    print("🎉 全部测试通过")
