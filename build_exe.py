"""将 win-dhcp-server 打包成 Windows 单文件 exe。

运行前：
    pip install pyinstaller
用法：
    python build_exe.py
产物：
    dist/win-dhcp-server.exe  （双击即运行，已内嵌默认 config.json）

说明：
    --uac-admin  启动即请求管理员权限（Windows 绑定 67 端口必需）
    --onefile    生成单个可执行文件，方便分发
    --add-data   把默认 config.json 打包进 exe，首次运行会复制到 exe 同目录
"""
import os

from PyInstaller.__main__ import run

HERE = os.path.dirname(os.path.abspath(__file__))

opts = [
    os.path.join(HERE, "main.py"),
    "--name", "win-dhcp-server",
    "--onefile",
    "--clean",
    "--noconfirm",
    "--uac-admin",
    "--paths", HERE,
    "--hidden-import", "netutil",
    "--hidden-import", "winsock_raw",
    "--hidden-import", "leasepool",
    "--hidden-import", "dhcpserver",
    "--hidden-import", "webui",
]

if __name__ == "__main__":
    run(opts)
