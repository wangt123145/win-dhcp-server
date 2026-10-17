"""IP 地址池与租约管理。

- 在配置的 IP 区间内分配地址
- 按 MAC 记忆已分配地址（同一设备续租时尽量给回原 IP）
- 租约过期自动回收
- 租约落盘到 leases.json，进程重启不丢
"""
import json
import threading
from datetime import datetime, timedelta

import ipaddress


class LeasePool:
    def __init__(self, config, lease_file="leases.json"):
        self.config = config
        self.lease_file = lease_file
        self.lock = threading.Lock()
        self.network = ipaddress.ip_network(
            f"{config['subnet']}/{config['netmask']}", strict=False
        )
        self.start = int(ipaddress.ip_address(config["pool_start"]))
        self.end = int(ipaddress.ip_address(config["pool_end"]))
        self.leases = {}        # ip_str -> {mac, expiry(float), hostname}
        self.mac_to_ip = {}     # mac -> ip_str
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            with open(self.lease_file, "r", encoding="utf-8") as f:
                self.leases = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.leases = {}
        # 丢弃不在地址池范围内的旧租约（配置变更后保持一致性）
        for ip in [ip for ip in list(self.leases) if not self.ip_in_range(ip)]:
            mac = self.leases[ip]["mac"]
            del self.leases[ip]
            if self.mac_to_ip.get(mac) == ip:
                del self.mac_to_ip[mac]
        for ip, info in self.leases.items():
            self.mac_to_ip[info["mac"]] = ip

    def _save(self):
        with open(self.lease_file, "w", encoding="utf-8") as f:
            json.dump(self.leases, f, indent=2, ensure_ascii=False)

    # ---------- 工具 ----------
    def _expire_ts(self):
        return datetime.now().timestamp() + self.config["lease_time"]

    def _prune_expired(self):
        now = datetime.now().timestamp()
        for ip in [ip for ip, info in self.leases.items() if info["expiry"] < now]:
            mac = self.leases[ip]["mac"]
            del self.leases[ip]
            if self.mac_to_ip.get(mac) == ip:
                del self.mac_to_ip[mac]

    def ip_in_range(self, ip_str):
        try:
            v = int(ipaddress.ip_address(ip_str))
        except ValueError:
            return False
        return self.start <= v <= self.end

    def ip_available(self, ip_str, mac):
        """该 IP 是否可分配给此 MAC（空闲，或已属于该 MAC）。"""
        if not self.ip_in_range(ip_str):
            return False
        if ip_str in self.leases:
            return self.leases[ip_str]["mac"] == mac
        return True

    # ---------- 分配接口 ----------
    def get_lease_for_mac(self, mac):
        with self.lock:
            ip = self.mac_to_ip.get(mac)
            if ip and ip in self.leases:
                self.leases[ip]["expiry"] = self._expire_ts()
                self._save()
                return ip
            return None

    def allocate(self, mac, requested_ip=None, hostname=""):
        with self.lock:
            self._prune_expired()
            # 1) 优先满足客户端请求的 IP
            if requested_ip and self.ip_available(requested_ip, mac):
                self._grant(requested_ip, mac, hostname)
                return requested_ip
            # 2) 否则分配区间内第一个空闲 IP
            for v in range(self.start, self.end + 1):
                ip = str(ipaddress.ip_address(v))
                if ip not in self.leases:
                    self._grant(ip, mac, hostname)
                    return ip
            return None  # 地址池耗尽

    def release(self, ip_str):
        with self.lock:
            if ip_str in self.leases:
                mac = self.leases[ip_str]["mac"]
                del self.leases[ip_str]
                if self.mac_to_ip.get(mac) == ip_str:
                    del self.mac_to_ip[mac]
                self._save()

    def _grant(self, ip_str, mac, hostname):
        self.leases[ip_str] = {
            "mac": mac,
            "expiry": self._expire_ts(),
            "hostname": hostname,
        }
        self.mac_to_ip[mac] = ip_str
        self._save()

    # ---------- 查询 ----------
    def active_leases(self):
        now = datetime.now().timestamp()
        with self.lock:
            return [
                {
                    "ip": ip,
                    "mac": info["mac"],
                    "hostname": info.get("hostname", ""),
                    "expires": datetime.fromtimestamp(info["expiry"]).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
                for ip, info in self.leases.items()
                if info["expiry"] > now
            ]
