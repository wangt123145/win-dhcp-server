"""DHCP 小工具 - 入口。

启动方式（需管理员权限）：
    python main.py

随后访问 http://127.0.0.1:8080 ，可在页面选择网口、编辑 IP 地址池等配置。
"""
import json
import os
import shutil
import socket
import sys
import threading

import ipaddress

from leasepool import LeasePool
from dhcpserver import DHCPServer
from webui import start_web


def base_dir():
    """程序运行目录：打包成 exe 后用 exe 所在目录，源码模式用脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LEASE_PATH = os.path.join(BASE_DIR, "leases.json")

DEFAULT_CONFIG = {
    "server_ip": "192.168.1.1",
    "interface": "0.0.0.0",
    "interface_index": 0,
    "subnet": "192.168.1.0",
    "netmask": "255.255.255.0",
    "pool_start": "192.168.1.100",
    "pool_end": "192.168.1.200",
    "gateway": "192.168.1.1",
    "dns": ["192.168.1.1", "8.8.8.8"],
    "lease_time": 3600,
    "web_host": "127.0.0.1",
    "web_port": 8080,
}


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(path, cfg):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def ensure_config():
    """保证配置文件存在：优先用打包进 exe 的默认配置，其次退回内置默认。"""
    if os.path.exists(CONFIG_PATH):
        return
    bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "config.json")
    if os.path.isfile(bundled):
        shutil.copyfile(bundled, CONFIG_PATH)
    else:
        save_config(CONFIG_PATH, DEFAULT_CONFIG)


class App:
    """全局应用状态：持有配置、地址池、DHCP 服务，支持配置热更新。"""

    def __init__(self, config_path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.pool = LeasePool(self.config, lease_file=LEASE_PATH)
        self.server = None
        self._start_server()

    def _start_server(self):
        self.server = DHCPServer(self.config, self.pool)
        t = threading.Thread(target=self.server.start, daemon=True)
        t.start()

    def _can_bind(self, interface):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((interface, 67))
            s.close()
            return True
        except Exception:
            return False

    def _validate(self):
        try:
            net = ipaddress.ip_network(
                f"{self.config['subnet']}/{self.config['netmask']}", strict=False
            )
            s = int(ipaddress.ip_address(self.config["pool_start"]))
            e = int(ipaddress.ip_address(self.config["pool_end"]))
            int(ipaddress.ip_address(self.config["gateway"]))
            int(ipaddress.ip_address(self.config["server_ip"]))
        except Exception as ex:
            return f"IP 格式错误: {ex}"
        if s > e:
            return "地址池起始不能大于结束"
        if not (int(net.network_address) <= s <= int(net.broadcast_address)):
            return "地址池起始不在子网范围内"
        if not (int(net.network_address) <= e <= int(net.broadcast_address)):
            return "地址池结束不在子网范围内"
        return None

    def apply_config(self, new):
        """合并新配置、校验、落盘，并热重启 DHCP 服务。"""
        editable = [
            "server_ip", "interface", "interface_index", "subnet", "netmask",
            "pool_start", "pool_end", "gateway", "dns", "lease_time",
        ]
        for k in editable:
            if k in new and new[k] not in (None, ""):
                self.config[k] = new[k]
        # 规范化
        if isinstance(self.config.get("dns"), str):
            self.config["dns"] = [
                d.strip() for d in self.config["dns"].split(",") if d.strip()
            ]
        try:
            self.config["lease_time"] = int(self.config["lease_time"])
        except Exception:
            return False, "租期必须是数字（秒）"

        # 处理网口选择：
        #   - "全部网口" -> interface=0.0.0.0，清空 interface_index，退出严格模式
        #   - 指定网口 -> 校验 interface_index，缺失时尝试按 IP 反查
        iface = self.config.get("interface", "0.0.0.0")
        if iface == "0.0.0.0":
            self.config["interface_index"] = 0
        else:
            idx = self.config.get("interface_index", 0)
            try:
                idx = int(idx) if idx not in (None, "") else 0
            except (TypeError, ValueError):
                idx = 0
            if not idx:
                # 前端偶发未带 index，尝试按 IP 反查
                import netutil
                idx = netutil.ifindex_by_ip(iface) or 0
            self.config["interface_index"] = idx
            if idx:
                # 严格模式要求 server_ip 落在该网口，若未显式设置则同步为本机 IP
                if self.config.get("server_ip") in (None, "", "0.0.0.0"):
                    self.config["server_ip"] = iface

        err = self._validate()
        if err:
            return False, err

        if not self._can_bind("0.0.0.0"):
            return False, ("端口 67 绑定失败：请确认以管理员身份运行，且未被其他程序占用"
                           "（DHCP 需监听所有网口才能收到广播）")

        # 落盘
        save_config(self.config_path, self.config)
        # 重启 DHCP 服务
        self.server.stop()
        self.pool = LeasePool(self.config)
        self._start_server()
        return True, "配置已保存并生效"


def main():
    ensure_config()
    app = App(CONFIG_PATH)
    start_web(app)


if __name__ == "__main__":
    main()
