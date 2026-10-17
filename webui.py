"""内置 Web 管理页（纯标准库，零依赖）。

功能：
  - 实时查看租约
  - 选择网口（枚举本机 IPv4 网口）
  - 在线编辑 IP 地址池 / 网关 / DNS / 租期，保存后热重启 DHCP 服务
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

import netutil


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DHCP 小工具 · 管理页</title>
<style>
  :root { color-scheme: light; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         margin: 0; background: #f5f7fa; color: #1f2d3d; }
  header { background: #1f6feb; color: #fff; padding: 16px 24px; }
  header h1 { margin: 0; font-size: 18px; }
  header p { margin: 4px 0 0; font-size: 13px; opacity: .85; }
  main { padding: 24px; max-width: 960px; margin: 0 auto; }
  .card { background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
          padding: 18px 20px; margin-bottom: 18px; }
  .card h2 { margin: 0 0 14px; font-size: 15px; color: #344563; }
  .meta { display: flex; flex-wrap: wrap; gap: 18px; font-size: 13px; color: #5c6b7a; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px 18px; }
  label { display: block; font-size: 12px; color: #8696a7; margin-bottom: 4px; }
  input, select { width: 100%; box-sizing: border-box; padding: 8px 10px;
                  border: 1px solid #d6dde6; border-radius: 6px; font-size: 14px; }
  .full { grid-column: 1 / -1; }
  .iface-row { display: flex; gap: 10px; align-items: flex-start; }
  .iface-row select { flex: 1; }
  .iface-row button { margin-top: 0; padding: 8px 14px; }
  button { margin-top: 14px; background: #1f6feb; color: #fff; border: 0;
           padding: 9px 18px; border-radius: 6px; font-size: 14px; cursor: pointer; }
  button:hover { background: #185fcc; }
  #msg { margin-top: 10px; font-size: 13px; min-height: 18px; }
  .ok { color: #28a745; } .err { color: #d73a49; }
  .warn { color: #d73a49; font-size: 12px; margin-top: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #eef1f5; }
  th { color: #8696a7; font-weight: 600; }
  .empty { color: #97a3b0; padding: 18px 0; text-align: center; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: #28a745; margin-right: 6px; }
</style>
</head>
<body>
<header>
  <h1>🌐 DHCP 分发服务 · 管理页</h1>
  <p>选择网口、编辑分配参数，保存后立即生效</p>
</header>
<main>
  <div class="card">
    <div class="meta" id="meta">加载中...</div>
  </div>

  <div class="card">
    <h2>⚙️ 服务配置</h2>
    <div class="grid">
      <div class="full">
        <label>选择网口</label>
        <div class="iface-row">
          <select id="iface"><option>加载中...</option></select>
          <button onclick="loadInterfaces()">刷新网口</button>
        </div>
        <div id="iface-warn" class="warn"></div>
      </div>
      <div>
        <label>本机服务 IP (server_ip)</label>
        <input id="server_ip" placeholder="如 192.168.1.1">
      </div>
      <div>
        <label>子网掩码</label>
        <input id="netmask" placeholder="255.255.255.0">
      </div>
      <div>
        <label>地址池起始</label>
        <input id="pool_start" placeholder="192.168.1.100">
      </div>
      <div>
        <label>地址池结束</label>
        <input id="pool_end" placeholder="192.168.1.200">
      </div>
      <div>
        <label>默认网关</label>
        <input id="gateway" placeholder="192.168.1.1">
      </div>
      <div>
        <label>DNS（逗号分隔）</label>
        <input id="dns" placeholder="192.168.1.1,8.8.8.8">
      </div>
      <div>
        <label>租期（秒）</label>
        <input id="lease_time" placeholder="3600">
      </div>
      <div>
        <label>服务网口（用于确定子网）</label>
        <input id="interface" placeholder="0.0.0.0 表示全部网口" readonly>
      </div>
      <div>
        <label>网口索引 (ifindex)</label>
        <input id="interface_index" placeholder="0 表示全部网口" readonly>
      </div>
      <div class="full">
        <div style="font-size:12px;color:#8696a7;line-height:1.6;">
          说明：Windows 下 DHCP 广播需监听所有网口，因此"选择网口"用于<b>自动确定服务子网与地址池</b>；
          选中具体网口后会进入<b>严格模式</b>——仅响应来自该网卡的 DHCP 请求，并以该网卡 IP 为源回包，
          从而真正"只服务指定网口"。指定"全部网口"则为普通全网口模式。
        </div>
      </div>
    </div>
    <button onclick="save()">保存并应用</button>
    <div id="msg"></div>
  </div>

  <div class="card">
    <h2>📋 当前租约</h2>
    <table>
      <thead><tr><th>IP 地址</th><th>MAC</th><th>主机名</th><th>租约到期</th></tr></thead>
      <tbody id="rows"><tr><td colspan="4" class="empty">加载中...</td></tr></tbody>
    </table>
  </div>
</main>
<script>
window._ifaceList = [];

async function loadAll() {
  const [cfg, ifaces] = await Promise.all([fetch('/api/config').then(r=>r.json()),
                                           fetch('/api/interfaces').then(r=>r.json())]);
  console.log('[UI] 当前配置:', cfg);
  document.getElementById('server_ip').value = cfg.server_ip || '';
  document.getElementById('netmask').value = cfg.netmask || '';
  document.getElementById('pool_start').value = cfg.pool_start || '';
  document.getElementById('pool_end').value = cfg.pool_end || '';
  document.getElementById('gateway').value = cfg.gateway || '';
  document.getElementById('dns').value = (cfg.dns||[]).join(', ');
  document.getElementById('lease_time').value = cfg.lease_time || '';
  document.getElementById('interface').value = cfg.interface || '0.0.0.0';
  document.getElementById('interface_index').value = cfg.interface_index || 0;

  renderInterfaces(ifaces.interfaces || [], cfg.interface);
}

async function loadInterfaces() {
  try {
    const ifaces = await fetch('/api/interfaces').then(r=>r.json());
    console.log('[UI] 刷新网口:', ifaces);
    const currentIface = document.getElementById('interface').value || '0.0.0.0';
    renderInterfaces(ifaces.interfaces || [], currentIface);
  } catch (e) {
    document.getElementById('iface-warn').textContent = '刷新网口失败：' + e;
  }
}

function renderInterfaces(list, selectedIp) {
  window._ifaceList = list;
  const sel = document.getElementById('iface');
  const warn = document.getElementById('iface-warn');
  sel.innerHTML = '';
  warn.textContent = '';

  const optAll = document.createElement('option');
  optAll.value = 'ALL';
  optAll.text = '全部网口 (0.0.0.0)';
  sel.appendChild(optAll);

  list.forEach((i, idx) => {
    const o = document.createElement('option');
    // value 只用索引，避免 JSON 字段不完全一致导致匹配失败
    o.value = String(idx);
    o.dataset.ip = i.ip || '';
    o.dataset.netmask = i.netmask || '';
    o.dataset.ifindex = i.ifindex || '';
    const name = i.name || i.ip || '未知网口';
    const nm = i.netmask || '未知掩码';
    const tag = i.ifindex ? '  [ifindex ' + i.ifindex + ']' : '';
    o.text = name + '  (' + (i.ip || '-') + ' / ' + nm + ')' + tag;
    sel.appendChild(o);
  });

  if (list.length === 0) {
    warn.textContent = '未检测到可用网口。请确认本机有已连接且分配到 IP 的网卡，或点击"刷新网口"重试。';
  }

  // 按 IP 匹配当前选中的网口；未匹配则默认"全部网口"
  const idx = list.findIndex(i => i.ip === selectedIp && selectedIp && selectedIp !== '0.0.0.0');
  sel.value = idx >= 0 ? String(idx) : 'ALL';
}

document.getElementById('iface').addEventListener('change', e => {
  const sel = e.target;
  const v = sel.value;
  if (v === 'ALL') {
    document.getElementById('interface').value = '0.0.0.0';
    document.getElementById('interface_index').value = 0;
    return;
  }
  const i = window._ifaceList[parseInt(v, 10)];
  if (!i) return;
  document.getElementById('interface').value = i.ip || '';
  document.getElementById('interface_index').value = i.ifindex || 0;
  document.getElementById('server_ip').value = i.ip || '';
  document.getElementById('netmask').value = i.netmask || '';
  const [ps, pe] = suggestPool(i.ip, i.netmask);
  document.getElementById('pool_start').value = ps;
  document.getElementById('pool_end').value = pe;
});

// 根据网卡 IP + 掩码，推荐一段地址池（网络地址 +100 ~ +200）
function suggestPool(ip, netmask) {
  if (!ip || !netmask) return ['', ''];
  const ipP = ip.split('.').map(Number);
  const nmP = netmask.split('.').map(Number);
  const net = ipP.map((v, i) => v & nmP[i]);
  const start = net.slice(); start[3] += 100;
  const end = net.slice(); end[3] += 200;
  if (start[3] > 254) start[3] = 254;
  if (end[3] > 254) end[3] = 254;
  return [start.join('.'), end.join('.')];
}

async function save() {
  const msg = document.getElementById('msg');
  msg.className = ''; msg.textContent = '保存中...';
  const payload = {
    server_ip: document.getElementById('server_ip').value.trim(),
    netmask: document.getElementById('netmask').value.trim(),
    pool_start: document.getElementById('pool_start').value.trim(),
    pool_end: document.getElementById('pool_end').value.trim(),
    gateway: document.getElementById('gateway').value.trim(),
    dns: document.getElementById('dns').value.trim(),
    lease_time: document.getElementById('lease_time').value.trim(),
    interface: document.getElementById('interface').value.trim() || '0.0.0.0',
    interface_index: document.getElementById('interface_index').value.trim() || 0,
  };
  try {
    const r = await fetch('/api/config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const d = await r.json();
    msg.textContent = d.msg;
    msg.className = d.ok ? 'ok' : 'err';
  } catch (e) {
    msg.textContent = '保存失败：' + e; msg.className = 'err';
  }
}

async function refresh() {
  try {
    const r = await fetch('/api/leases');
    const d = await r.json();
    document.getElementById('meta').innerHTML =
      '<span><span class="dot"></span>服务运行中</span>' +
      '<span>地址池：' + d.pool + '</span>' +
      '<span>已分配：' + d.leases.length + ' 个</span>';
    const tb = document.getElementById('rows');
    if (d.leases.length === 0) {
      tb.innerHTML = '<tr><td colspan="4" class="empty">暂无租约</td></tr>';
    } else {
      tb.innerHTML = d.leases.map(l =>
        '<tr><td>' + l.ip + '</td><td>' + l.mac + '</td><td>' +
        (l.hostname || '-') + '</td><td>' + l.expires + '</td></tr>'
      ).join('');
    }
  } catch (e) {
    document.getElementById('meta').textContent = '无法连接服务';
  }
}
loadAll();
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>"""


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/leases":
                self._json({
                    "leases": app.pool.active_leases(),
                    "pool": f"{app.config['pool_start']} ~ {app.config['pool_end']}",
                })
            elif self.path == "/api/interfaces":
                self._json({"interfaces": netutil.list_interfaces()})
            elif self.path == "/api/config":
                self._json({
                    "server_ip": app.config.get("server_ip", ""),
                    "interface": app.config.get("interface", "0.0.0.0"),
                    "interface_index": app.config.get("interface_index", 0),
                    "subnet": app.config.get("subnet", ""),
                    "netmask": app.config.get("netmask", ""),
                    "pool_start": app.config.get("pool_start", ""),
                    "pool_end": app.config.get("pool_end", ""),
                    "gateway": app.config.get("gateway", ""),
                    "dns": app.config.get("dns", []),
                    "lease_time": app.config.get("lease_time", 3600),
                })
            else:
                self._html(PAGE)

        def do_POST(self):
            if self.path != "/api/config":
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                new = json.loads(body)
            except Exception as e:
                self._json({"ok": False, "msg": f"参数解析失败: {e}"})
                return
            ok, msg = app.apply_config(new)
            self._json({"ok": ok, "msg": msg})

        def _json(self, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, html):
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # 静默访问日志

    return Handler


def start_web(app):
    handler = make_handler(app)
    httpd = ThreadingHTTPServer((app.config["web_host"], app.config["web_port"]), handler)
    print(f"[WEB ] 管理页面已启动: http://{app.config['web_host']}:{app.config['web_port']}")
    httpd.serve_forever()
