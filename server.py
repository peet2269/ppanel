#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ppanel —— 极简中文服务器监控面板

设计目标（对齐大王需求）：
  · 概览：本机跑的各个服务是否正常
  · 状态：CPU / 内存 / 负载 / 磁盘，负载要给「流畅还是卡顿」的判断，磁盘要给剩余
  · 监控：今日访问量 + 进/出流量 + 折线图
  · 系统信息：主机名 / 发行版 / 内核 / 架构 / 地址 / 启动时间 / 运行时间
  · 全中文、零第三方依赖（只读 /proc 与 /sys，不装 psutil）

实现：标准库 http.server 起一个服务，暴露
  GET /               -> 前端页面
  GET /api/stats      -> 实时指标 JSON
  GET /api/history    -> 折线图历史（内存环形缓冲）

内存占用目标 < 25 MB（对比 1Panel 的 156 MB）。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ───────────────────────── 配置 ─────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, 'index.html')

# 被监控的本机服务：(显示名, 探测地址, 备注)
# [FIX] 每个服务补上：对外地址、systemd 单元、进程匹配串、专属 API
#   key  = 内部标识（前端用它取详情）
#   url  = 探活地址
#   note = 卡片上那句小字
#   home = 浏览器里打开的地址（None 表示本机服务，不给外链）
#   unit = systemd 单元名（None 表示不是 systemd 管的）
#   proc = 进程匹配串（用来取 CPU/内存/运行时长）
#   api  = 专属指标接口（只走本机，不经 nginx）
# ── 服务清单：从 config.json 读（见 services.example.json）──
#   不想装 config.json 也能跑，下面是通用默认（示例服务）
DEFAULT_SERVICES = [
    {'key': 'web', 'name': '网站', 'url': 'http://127.0.0.1:80/',
     'note': 'nginx', 'home': None,
     'unit': 'nginx', 'proc': 'nginx: worker process',
     'api': None, 'log': None},
]

_CFG = {}
try:
    _cfg_path = os.environ.get('PANEL_CONFIG') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'config.json')
    if os.path.exists(_cfg_path):
        with open(_cfg_path, 'r', encoding='utf-8') as _f:
            _CFG = json.load(_f)
except Exception as _e:                       # 配置坏了不该让面板起不来
    print(f'[panel] 读取 config.json 失败，用默认值：{_e}', flush=True)

SERVICES = _CFG.get('services') or DEFAULT_SERVICES

# nginx 访问日志（统计今日访问量 / 流量）
NGINX_LOGS = [
    '/var/log/nginx/access.log',
    '/var/log/nginx/access.log',
    '/var/log/nginx/error.log',
]
NGINX_LOG_GLOB_DIR = '/var/log/nginx'

HISTORY_LEN = 180          # 折线图保留点数（每 5 秒一个 → 15 分钟）
SAMPLE_INTERVAL = 5        # 采样间隔（秒）

# ── 持久化历史（新增）──
DATA_DIR = os.environ.get('PANEL_DATA') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data')
ARCHIVE_EVERY = 60         # 多少秒落盘一个聚合点（1 分钟一个）
KEEP_DAYS = 7              # 落盘文件保留天数
RANGES = {                 # 支持的时间范围 → 落盘点数
    '15m': 15 * 60,
    '1h': 3600,
    '6h': 6 * 3600,
    '24h': 24 * 3600,
}

# ── 告警阈值（新增）──
_DEFAULT_TH = {
    'disk_pct': 90,        # 磁盘使用率告警线
    'mem_pct': 92,         # 内存使用率告警线
    'load_ratio': 1.6,     # load1 / 核数
    'cpu_sustained': 95,   # CPU 连续超这个值（秒）
    'cpu_sustain_secs': 120,
}
TH = {**_DEFAULT_TH, **(_CFG.get('thresholds') or {})}
ALERT_COOLDOWN = 600      # 同一类告警的重复冷却（秒），避免刷屏

# ── 认证日志（新增）──
NGINX_LOG = _CFG.get('nginx_log') or os.environ.get(
    'PANEL_NGINX_LOG', '/var/log/nginx/access.log')
NGINX_ERR_LOG = _CFG.get('nginx_error_log') or os.environ.get(
    'PANEL_NGINX_ERR_LOG', '/var/log/nginx/error.log')
AUTHLOG_MAX = 500          # 一次最多返回多少条

# ───────────────────────── 采集 ─────────────────────────

_prev_cpu = {'idle': 0, 'total': 0}
_prev_net = {'rx': 0, 'tx': 0, 'ts': 0}
_lock = threading.Lock()
_history = deque(maxlen=HISTORY_LEN)


def _read(path: str) -> str:
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return ''


def cpu_percent() -> float:
    """读 /proc/stat 算总体 CPU 使用率（两次采样的差值）。"""
    line = _read('/proc/stat').split('\n', 1)[0]
    parts = [int(x) for x in line.split()[1:] if x.isdigit()]
    if len(parts) < 4:
        return 0.0
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    with _lock:
        di = idle - _prev_cpu['idle']
        dt = total - _prev_cpu['total']
        _prev_cpu['idle'], _prev_cpu['total'] = idle, total
    if dt <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1 - di / dt) * 100)), 1)


def mem_info() -> dict:
    d = {}
    for ln in _read('/proc/meminfo').splitlines():
        m = re.match(r'(\w+):\s+(\d+)', ln)
        if m:
            d[m.group(1)] = int(m.group(2))     # kB
    total = d.get('MemTotal', 1)
    avail = d.get('MemAvailable', d.get('MemFree', 0))
    used = total - avail
    return {
        'total_mb': round(total / 1024, 1),
        'used_mb': round(used / 1024, 1),
        'avail_mb': round(avail / 1024, 1),
        'percent': round(used / total * 100, 1) if total else 0.0,
    }


def load_info() -> dict:
    parts = _read('/proc/loadavg').split()
    try:
        l1, l5, l15 = float(parts[0]), float(parts[1]), float(parts[2])
    except Exception:
        l1 = l5 = l15 = 0.0
    cores = os.cpu_count() or 1
    # 流畅度判据：用 1 分钟负载 / 核数
    ratio = l1 / cores
    if ratio < 0.7:
        verdict, level = '运行流畅', 'ok'
    elif ratio < 1.0:
        verdict, level = '负载偏高', 'warn'
    else:
        verdict, level = '负载过重', 'bad'
    return {'load1': round(l1, 2), 'load5': round(l5, 2), 'load15': round(l15, 2),
            'cores': cores, 'verdict': verdict, 'level': level}


def disk_info() -> dict:
    try:
        st = os.statvfs('/')
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {
            'total_gb': round(total / 1024 ** 3, 2),
            'used_gb': round(used / 1024 ** 3, 2),
            'free_gb': round(free / 1024 ** 3, 2),
            'percent': round(used / total * 100, 1) if total else 0.0,
        }
    except Exception:
        return {'total_gb': 0, 'used_gb': 0, 'free_gb': 0, 'percent': 0}


def net_info() -> dict:
    """累计收发字节 + 瞬时速率。"""
    rx = tx = 0
    for ln in _read('/proc/net/dev').splitlines():
        if ':' not in ln:
            continue
        name, rest = ln.split(':', 1)
        name = name.strip()
        if name == 'lo':
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    now = time.time()
    up = down = 0.0
    with _lock:
        if _prev_net['ts'] and now > _prev_net['ts']:
            dt = now - _prev_net['ts']
            down = max(0.0, (rx - _prev_net['rx']) / dt)
            up = max(0.0, (tx - _prev_net['tx']) / dt)
        _prev_net.update({'rx': rx, 'tx': tx, 'ts': now})
    return {
        'rx_total': rx, 'tx_total': tx,
        'down_bps': round(down, 1), 'up_bps': round(up, 1),
    }


def temp_info() -> list:
    out = []
    for i in range(4):
        p = f'/sys/class/thermal/thermal_zone{i}/temp'
        t = _read(p).strip()
        tp = f'/sys/class/thermal/thermal_zone{i}/type'
        if t.isdigit():
            name = _read(tp).strip() or f'zone{i}'
            out.append({'name': name, 'c': round(int(t) / 1000, 1)})
    return out


def _probe(url: str, timeout: float = 2.5):
    """轻量 HTTP 探活：用 curl（服务器上必有），避免引 urllib 的开销。"""
    t0 = time.time()
    try:
        r = subprocess.run(
            ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
             '--max-time', str(timeout), '--noproxy', '*', url],
            capture_output=True, text=True, timeout=timeout + 1,
        )
        code = r.stdout.strip()
        ms = round((time.time() - t0) * 1000)
        # 401/403 也说明服务活着（只是要认证），不能判成「不通」
        ok = code.startswith('2') or code.startswith('3') or code in ('401', '403')
        return ok, code or '---', ms
    except Exception:
        return False, 'ERR', round((time.time() - t0) * 1000)


_SVC_CACHE = {'ts': 0.0, 'data': None}
_SVC_TTL = 8          # 秒：服务探活结果缓存（公网那条要 1.1s，没缓存会很拖）


def services_info() -> list:
    """并行探测本机服务；结果缓存 8 秒，避免每次刷新都重探。"""
    now = time.time()
    if _SVC_CACHE['data'] is not None and now - _SVC_CACHE['ts'] < _SVC_TTL:
        return _SVC_CACHE['data']

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(SERVICES)) as ex:
        results = list(ex.map(lambda s: _probe(s['url']), SERVICES))
    out = []
    for svc, (ok, code, ms) in zip(SERVICES, results):
        out.append({
            'key': svc['key'], 'name': svc['name'], 'note': svc['note'],
            'home': svc['home'], 'ok': ok, 'code': code, 'ms': ms,
        })
    _SVC_CACHE['ts'] = now
    _SVC_CACHE['data'] = out
    return out


# ── nginx 日志统计：今日访问量 + 流量 ──
#
# ★ 正则要点：状态码前面是完整的 "METHOD PATH PROTO" 引号段，
#   若写成 .*?" 会先把开引号吃掉，导致状态码永远匹配不上（踩过）。
_LOG_RE = re.compile(
    r'\[(\d{2}/\w{3}/\d{4}):\d{2}:\d{2}:\d{2}[^\]]*\]\s+"[^"]*"\s+(\d{3})\s+(\d+)'
)
_TAIL_BYTES = 1024 * 1024        # 只读每个日志尾部 1MB（今日数据都在这儿）
_TODAY_CACHE = {'ts': 0.0, 'data': None}
_TODAY_TTL = 30                  # 秒：日志统计缓存，别每次请求都全量扫


def _scan_log(path: str, today: str):
    """从文件尾部读，统计今日请求数 / 状态码分布 / 响应字节。"""
    reqs = 0
    nbytes = 0
    codes = {}
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as fb:
            if size > _TAIL_BYTES:
                fb.seek(-_TAIL_BYTES, os.SEEK_END)
                fb.readline()          # 丢掉可能被截断的半行
            data = fb.read()
    except Exception:
        return reqs, nbytes, codes
    for ln in data.decode('utf-8', 'ignore').splitlines():
        m = _LOG_RE.search(ln)
        if not m or m.group(1) != today:
            continue
        reqs += 1
        c = m.group(2)
        codes[c[0] + 'xx'] = codes.get(c[0] + 'xx', 0) + 1
        try:
            nbytes += int(m.group(3))
        except Exception:
            pass
    return reqs, nbytes, codes


def today_stats() -> dict:
    """扫 nginx 日志统计今日访问量；结果缓存 30 秒（避免拖慢接口）。"""
    from datetime import datetime
    now = time.time()
    if _TODAY_CACHE['data'] is not None and now - _TODAY_CACHE['ts'] < _TODAY_TTL:
        return _TODAY_CACHE['data']

    today = datetime.now().strftime('%d/%b/%Y')
    files = []
    if os.path.isdir(NGINX_LOG_GLOB_DIR):
        for fn in sorted(os.listdir(NGINX_LOG_GLOB_DIR)):
            if fn.endswith('.access.log') or fn == 'access.log':
                files.append(os.path.join(NGINX_LOG_GLOB_DIR, fn))

    reqs = nbytes = 0
    codes = {}
    for p in files:
        r, b, c = _scan_log(p, today)
        reqs += r
        nbytes += b
        for k, v in c.items():
            codes[k] = codes.get(k, 0) + v

    data = {'requests': reqs, 'bytes_out': nbytes, 'codes': codes,
            'files': len(files), 'date': today}
    _TODAY_CACHE['ts'] = now
    _TODAY_CACHE['data'] = data
    return data


def sys_info() -> dict:
    distro = ''
    for ln in _read('/etc/os-release').splitlines():
        if ln.startswith('PRETTY_NAME='):
            distro = ln.split('=', 1)[1].strip().strip('"')
            break
    kern = _read('/proc/sys/kernel/osrelease').strip()
    arch = _read('/proc/sys/kernel/arch').strip() or os.uname().machine
    boot = 0.0
    try:
        up = float(_read('/proc/uptime').split()[0])
        boot = time.time() - up
    except Exception:
        up = 0.0
    ip = ''
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = '—'
    return {
        'host': socket.gethostname(),
        'distro': distro or 'Linux',
        'kernel': kern,
        'arch': arch,
        'ip': ip,
        'boot_ts': int(boot),
        'uptime_sec': int(up),
    }


# ───────────────────────── 汇总 ─────────────────────────

def collect() -> dict:
    snap = {
        'ts': int(time.time()),
        'cpu': {'percent': cpu_percent(), 'cores': os.cpu_count() or 1},
        'mem': mem_info(),
        'load': load_info(),
        'disk': disk_info(),
        'net': net_info(),
        'temps': temp_info(),
        'services': services_info(),
        'today': today_stats(),
        'sys': sys_info(),
    }
    _record(snap)
    return snap


def _record(snap: dict):
    _history.append({
        'ts': snap['ts'],
        'cpu': snap['cpu']['percent'],
        'mem': snap['mem']['percent'],
        'down': snap['net']['down_bps'],
        'up': snap['net']['up_bps'],
    })


def _sampler_loop():
    """
    后台定时采样：只采轻量指标（CPU/内存/网络），不碰服务探测与日志。
    否则「没人打开页面就没有历史曲线」，一进去只能看到孤零零一两个点。
    """
    while True:
        try:
            snap = {
                'ts': int(time.time()),
                'cpu': {'percent': cpu_percent()},
                'mem': mem_info(),
                'net': net_info(),
            }
            _record(snap)
            # 落盘聚合（每分钟一个点，供长周期回溯）
            _archive_rollup({
                'cpu': snap['cpu']['percent'],
                'mem': snap['mem']['percent'],
                'down': snap['net']['down_bps'],
                'up': snap['net']['up_bps'],
            })
        except Exception:
            pass
        time.sleep(SAMPLE_INTERVAL)


# ───────────────────── 持久化历史（新增）─────────────────────

_ARCH = {'last': 0.0, 'acc': None, 'n': 0}
_alerts = []            # 活跃告警
_alert_log = deque(maxlen=200)   # 告警流水（含已恢复）


def _ensure_data_dir():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


def _archive_file(ts: int) -> str:
    return os.path.join(DATA_DIR, time.strftime('%Y%m%d', time.localtime(ts)) + '.jsonl')


def _archive_point(p: dict):
    """把聚合点追加到当天的 jsonl。"""
    _ensure_data_dir()
    line = json.dumps(p, ensure_ascii=False)
    try:
        with open(_archive_file(p['ts']), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def _archive_rollup(snap_like: dict):
    """每 SAMPLE_INTERVAL 调一次：攒够 ARCHIVE_EVERY 秒就落一个均值点。"""
    acc = _ARCH['acc']
    if acc is None:
        acc = _ARCH['acc'] = {'cpu': 0.0, 'mem': 0.0, 'down': 0, 'up': 0, 'peak_down': 0, 'peak_up': 0, 'n': 0}
    acc['cpu'] += float(snap_like.get('cpu', 0) or 0)
    acc['mem'] += float(snap_like.get('mem', 0) or 0)
    acc['down'] += int(snap_like.get('down', 0) or 0)
    acc['up'] += int(snap_like.get('up', 0) or 0)
    acc['peak_down'] = max(acc['peak_down'], int(snap_like.get('down', 0) or 0))
    acc['peak_up'] = max(acc['peak_up'], int(snap_like.get('up', 0) or 0))
    acc['n'] += 1
    now = time.time()
    if now - _ARCH['last'] >= ARCHIVE_EVERY and acc['n'] > 0:
        n = acc['n']
        _archive_point({
            'ts': int(now),
            'cpu': round(acc['cpu'] / n, 2),
            'mem': round(acc['mem'] / n, 2),
            # 流量用「峰值」更能反映真实带宽，均值会被采样间隔抹平
            'down': acc['peak_down'],
            'up': acc['peak_up'],
            'avg_down': int(acc['down'] / n),
            'avg_up': int(acc['up'] / n),
        })
        _ARCH['last'] = now
        _ARCH['acc'] = None


def _load_archive(range_key: str = '1h'):
    """读回落盘历史，按 range 过滤。返回按时间升序的点。"""
    span = RANGES.get(range_key, 3600)
    cutoff = time.time() - span
    out = []
    _ensure_data_dir()
    try:
        files = sorted(os.listdir(DATA_DIR))
    except Exception:
        return out
    for name in files:
        if not name.endswith('.jsonl'):
            continue
        # 只看可能覆盖时间范围的文件（最多回看 KEEP_DAYS 天）
        try:
            day = time.strptime(name[:8], '%Y%m%d')
        except Exception:
            continue
        if time.mktime(day) < cutoff - 86400:
            continue
        try:
            with open(os.path.join(DATA_DIR, name), 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        p = json.loads(line)
                    except Exception:
                        continue
                    if p.get('ts', 0) >= cutoff:
                        out.append(p)
        except Exception:
            continue
    out.sort(key=lambda x: x['ts'])
    return out


def _prune_archives():
    """删除过期的落盘文件。"""
    try:
        cutoff = time.time() - KEEP_DAYS * 86400
        for name in os.listdir(DATA_DIR):
            if not name.endswith('.jsonl'):
                continue
            try:
                day = time.strptime(name[:8], '%Y%m%d')
            except Exception:
                continue
            if time.mktime(day) < cutoff:
                os.remove(os.path.join(DATA_DIR, name))
    except Exception:
        pass


# ───────────────────── 服务详情（新增）─────────────────────
# 概览页点某个服务时，返回它自己的指标：进程占用、运行时长、
# systemd 状态、今日请求数，以及它自己 API 里的业务数据。

_SVC_BY_KEY = {s['key']: s for s in SERVICES}
_PROC_CACHE = {'ts': 0.0, 'data': {}}
_PROC_TTL = 5


def _proc_table() -> dict:
    """
    一次 ps 拿到全部进程，缓存 5 秒。
    返回 {匹配串: [{'pid','cpu','mem','rss','etimes'}, ...]}
    """
    now = time.time()
    if _PROC_CACHE['data'] and now - _PROC_CACHE['ts'] < _PROC_TTL:
        return _PROC_CACHE['data']
    pats = [s['proc'] for s in SERVICES if s.get('proc')]
    out = {p: [] for p in pats}
    if not pats:
        _PROC_CACHE['ts'] = now
        _PROC_CACHE['data'] = out
        return out
    try:
        r = subprocess.run(['ps', '-eo', 'pid,pcpu,pmem,rss,etimes,args'],
                           capture_output=True, text=True, timeout=4)
        for line in r.stdout.splitlines()[1:]:
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            pid, pcpu, pmem, rss, et, args = parts
            for p in pats:
                if p in args:
                    try:
                        out[p].append({
                            'pid': int(pid), 'cpu': float(pcpu), 'mem': float(pmem),
                            'rss': int(rss) * 1024, 'uptime': int(et),
                        })
                    except ValueError:
                        pass
    except Exception:
        pass
    _PROC_CACHE['ts'] = now
    _PROC_CACHE['data'] = out
    return out


def _unit_status(name: str) -> dict:
    """systemd 单元的运行状态与启动时间。"""
    if not name:
        return {}
    try:
        r = subprocess.run(
            ['systemctl', 'show', name, '--property=ActiveState,SubState,ActiveEnterTimestamp,NRestarts,MemoryCurrent'],
            capture_output=True, text=True, timeout=4)
        d = {}
        for line in r.stdout.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                d[k] = v
        if not d.get('ActiveState'):
            return {}
        started = d.get('ActiveEnterTimestamp', '')
        # 算已运行时长（天/小时）
        up = ''
        try:
            t0 = time.mktime(time.strptime(started.split()[0], '%a-%Y-%m-%d'))
            secs = int(time.time() - t0)
            if secs > 86400:
                up = f'{secs // 86400} 天'
            elif secs > 3600:
                up = f'{secs // 3600} 小时'
            else:
                up = f'{secs // 60} 分钟'
        except Exception:
            pass
        return {
            'active': d.get('ActiveState', '?'),
            'sub': d.get('SubState', ''),
            'uptime': up,
            'restarts': d.get('NRestarts', '0'),
            'mem': int(d.get('MemoryCurrent') or 0),
        }
    except Exception:
        return {}


def _today_requests(logbase: str) -> int:
    """今日某个 nginx 站点的请求数。"""
    if not logbase:
        return -1
    path = f'/var/log/nginx/{logbase}.access.log'
    try:
        # 只读尾部 1MB，今日请求都在这一段里
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(max(0, size - _TAIL_BYTES))
            data = f.read().decode('utf-8', 'ignore')
        today = time.strftime('%d/%b/%Y')
        return data.count(f'[{today}')
    except Exception:
        return -1


def _api_json(url: str):
    """只走本机取 JSON，不经 nginx。"""
    if not url:
        return None
    try:
        r = subprocess.run(
            ['curl', '-s', '--max-time', '3', '--noproxy', '*', url],
            capture_output=True, text=True, timeout=5)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout)
    except Exception:
        return None


# 业务字段的中文名（各服务自己的 API 都用英文 key）
_LABELS = {
    'rooms': '房间', 'total': '总数', 'waiting': '等待中', 'playing': '游戏中',
    'cleanup': '清理中', 'sessions': '会话', 'players': '玩家',
    'totalConnections': '总连接数', 'inRooms': '在房间内', 'inSessions': '在会话内',
    'timers': '定时器', 'roomDestroyTimers': '房间销毁', 'disconnectTimers': '断线清理',
    'users': '用户数', 'characters': '角色数',
}


def _flatten_api(data, prefix: str = '', depth: int = 0) -> list:
    """把 API 返回的嵌套 JSON 压成 [(标签, 值, 是否高亮), ...]，方便前端直接渲染。"""
    out = []
    if not isinstance(data, dict) or depth > 2:
        return out
    for k, v in data.items():
        if k in ('success', 'timestamp', 'ok', 'status', 'version', 'message'):
            continue
        label = _LABELS.get(k, k.replace('_', ' '))
        if isinstance(v, dict):
            out.extend(_flatten_api(v, prefix=label + ' · ', depth=depth + 1))
        elif isinstance(v, list):
            if v and isinstance(v[0], dict):
                out.append((label, f'{len(v)} 项', False))
            else:
                out.append((label, f'{len(v)} 项', False))
        elif isinstance(v, bool):
            out.append((label, '是' if v else '否', v))
        elif isinstance(v, (int, float)):
            if v == 0 and depth > 0:   # 嵌套里的 0 值不列（全是 0 看着像坏了）
                continue
            out.append((label, v, False))
        elif isinstance(v, str) and v:
            out.append((label, v, False))
    return out


def service_detail(key: str) -> dict:
    """单个服务的完整详情。"""
    svc = _SVC_BY_KEY.get(key)
    if not svc:
        return {'error': 'unknown service'}
    ok, code, ms = _probe(svc['url'])
    procs = _proc_table().get(svc['proc'] or '', [])
    unit = _unit_status(svc['unit'])
    reqs = _today_requests(svc['log'])
    api = _api_json(svc['api'])

    # 汇总该服务全部进程的占用
    cpu = round(sum(p['cpu'] for p in procs), 1)
    mem = round(sum(p['mem'] for p in procs), 1)
    rss = sum(p['rss'] for p in procs)
    uptime = max([p['uptime'] for p in procs], default=0)

    def _dur(sec):
        if not sec:
            return '-'
        if sec > 86400:
            return f'{sec // 86400} 天 {sec % 86400 // 3600} 小时'
        if sec > 3600:
            return f'{sec // 3600} 小时 {sec % 3600 // 60} 分'
        return f'{sec // 60} 分'

    metrics = [
        {'label': '运行状态', 'value': ('正常' if ok else '异常'), 'good': ok,
         'hint': f'HTTP {code}'},
        {'label': '响应时间', 'value': f'{ms} ms',
         'good': ms < 500, 'hint': '小于 500ms 算健康'},
        {'label': 'CPU 占用', 'value': f'{cpu}%',
         'good': cpu < 50, 'hint': f'{len(procs)} 个进程'},
        {'label': '内存占用', 'value': f'{mem}%',
         'good': mem < 30, 'hint': f'共 {rss // 1024 // 1024} MB'},
        {'label': '已运行', 'value': _dur(uptime), 'good': True, 'hint': '进程创建至今'},
    ]
    if unit:
        active = unit.get('active', '')
        metrics.append({
            'label': 'systemd', 'value': active or '-',
            'good': active == 'active', 'hint': f"重启 {unit.get('restarts', '0')} 次"})
    if reqs >= 0:
        metrics.append({
            'label': '今日请求', 'value': f'{reqs}',
            'good': True, 'hint': '来自 nginx 访问日志'})

    extra = _flatten_api(api) if api else []
    return {
        'key': key, 'name': svc['name'], 'note': svc['note'],
        'home': svc['home'],
        'ok': ok, 'code': code, 'ms': ms,
        'procs': procs,
        'unit': unit,
        'requests_today': reqs,
        'metrics': metrics,
        'api_fields': [{'label': a, 'value': b, 'good': g} for a, b, g in extra],
        'ts': int(time.time()),
    }


# ───────────────────── 认证日志（新增）─────────────────────
# panel_auth 格式：时间|状态码|真实IP|方法+URI|UA|直连IP|耗时
_AUTH_RE = re.compile(
    r'^(?P<ts>[^|]+)\|(?P<status>\d{3})\|(?P<ip>[^|]*)\|'
    r'"(?P<method>[^\s]*)\s(?P<uri>[^\s]*)"\|'
    r'"(?P<ua>[^"]*)"\|(?P<direct>[^|]*)\|(?P<dur>[^|]*)$'
)

# 封禁中的 IP（从 iptables 读，让页面能显示「已封禁」标记）
def _banned_ips() -> list:
    out = []
    try:
        r = subprocess.run(['iptables', '-S', 'f2b-panel-auth'],
                           capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            m = re.search(r'-s\s+(\d+\.\d+\.\d+\.\d+)/32', line)
            if m:
                out.append(m.group(1))
    except Exception:
        pass
    return out


def _ua_kind(ua: str) -> str:
    """把 UA 归成可读的类型，便于扫一眼。"""
    if not ua:
        return '未知'
    if 'curl' in ua.lower() or 'wget' in ua.lower():
        return '命令行工具'
    if any(k in ua for k in ('Edg/', 'Chrome/', 'Firefox/', 'Safari/')):
        return '浏览器'
    if any(k in ua.lower() for k in ('python', 'go-http', 'java', 'node', 'axios')):
        return '脚本/爬虫'
    return '其它'


def read_authlog(limit: int = AUTHLOG_MAX, ip_filter: str = '', since_ts: int = 0) -> list:
    """读 nginx 面板日志，返回结构化的登录尝试记录（最新的在前）。"""
    try:
        with open(NGINX_LOG, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception:
        return []
    banned = set(_banned_ips())
    out = []
    for line in reversed(lines):
        line = line.rstrip('\n')
        if not line:
            continue
        m = _AUTH_RE.match(line)
        if not m:
            continue
        ip = (m.group('ip') or '').strip()
        if ip_filter and ip_filter not in ip:
            continue
        # 只留「访问首页」这类动作（认证发生在跳转之前），避免刷屏
        uri = m.group('uri') or '/'
        if not (uri == '/' or uri.startswith('/monitor')):
            continue
        status = int(m.group('status'))
        out.append({
            'ts': m.group('ts'),
            'ip': ip,
            'ok': status == 200,
            'status': status,
            'uri': uri,
            'ua': (m.group('ua') or '')[:200],
            'kind': _ua_kind(m.group('ua') or ''),
            'dur': m.group('dur'),
            'banned': ip in banned,
        })
        if len(out) >= limit:
            break
    return out


def _authlog_summary(rows: list) -> dict:
    """登录概览：成功/失败次数、涉及 IP 数、被封数。"""
    ok = sum(1 for r in rows if r['ok'])
    return {
        'total': len(rows),
        'ok': ok,
        'fail': len(rows) - ok,
        'ips': len({r['ip'] for r in rows if r['ip']}),
        'banned': len({r['ip'] for r in rows if r['ip'] and r['banned']}),
    }


# ───────────────────── 告警引擎（新增）─────────────────────

_alert_state = {}   # key -> {'since': ts, 'last': ts}


def _fire(key: str, level: str, title: str, detail: str):
    """触发一条告警（带冷却，避免刷屏）。"""
    now = time.time()
    st = _alert_state.get(key)
    if st and now - st['last'] < ALERT_COOLDOWN:
        return
    item = {
        'key': key,
        'level': level,          # warn / critical
        'title': title,
        'detail': detail,
        'ts': int(now),
        'state': 'active',
    }
    # 同 key 的旧活跃告警标记为已恢复
    for a in _alerts:
        if a['key'] == key and a['state'] == 'active':
            a['state'] = 'resolved'
            a['resolved_ts'] = int(now)
    _alerts.append(item)
    del _alerts[:-20]            # 活跃列表最多留 20 条
    _alert_log.append(item)
    _append_alert_file(item)


def _resolve(key: str, note: str = ''):
    now = time.time()
    for a in _alerts:
        if a['key'] == key and a['state'] == 'active':
            a['state'] = 'resolved'
            a['resolved_ts'] = int(now)
            r = {
                'key': key, 'level': 'ok', 'title': a['title'] + ' 已恢复',
                'detail': note or a['detail'], 'ts': int(now), 'state': 'resolved',
            }
            _alert_log.append(r)
            _append_alert_file(r)
    _alert_state.pop(key, None)


def _append_alert_file(item: dict):
    _ensure_data_dir()
    try:
        with open(os.path.join(DATA_DIR, 'alerts.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _check_alerts(snap: dict):
    """拿一次完整快照来判告警。由 /api/stats 触发，不额外增加探测开销。"""
    try:
        # 1) 服务不通
        for s in snap.get('services', []):
            key = 'svc:' + s['name']
            if s.get('ok'):
                _resolve(key, '服务已恢复可达')
            else:
                _fire(key, 'critical', f"服务不可用：{s['name']}",
                      f"{s.get('note', '')}（{s.get('ms', 0)} ms）")

        # 2) 磁盘
        d = snap.get('disk') or {}
        dp = float(d.get('percent') or 0)
        if dp >= TH['disk_pct']:
            _fire('disk', 'critical', '磁盘空间告急',
                  f"根分区已用 {dp:.1f}%，仅剩 {d.get('free_gb', 0):.2f} GB")
        else:
            _resolve('disk')

        # 3) 内存
        m = snap.get('mem') or {}
        mp = float(m.get('percent') or 0)
        if mp >= TH['mem_pct']:
            _fire('mem', 'warn', '内存占用过高', f"已用 {mp:.1f}%")
        else:
            _resolve('mem')

        # 4) 负载（相对核数）
        ld = snap.get('load') or {}
        cores = float(ld.get('cores') or 1)
        ratio = float(ld.get('load1') or 0) / max(cores, 1)
        if ratio >= TH['load_ratio']:
            _fire('load', 'warn', '系统负载偏高',
                  f"load1={ld.get('load1', 0):.2f}，为核数的 {ratio:.2f} 倍")
        else:
            _resolve('load')

        # 5) CPU 持续高 —— 需要「持续」判定，不是一次尖峰
        cp = float((snap.get('cpu') or {}).get('percent') or 0)
        st = _alert_state.get('cpu')
        if cp >= TH['cpu_sustained']:
            if st is None:
                _alert_state['cpu'] = {'since': time.time(), 'last': 0}
            elif time.time() - _alert_state['cpu']['since'] >= TH['cpu_sustain_secs']:
                _fire('cpu', 'warn', 'CPU 持续高负载',
                      f"连续 {int((time.time() - _alert_state['cpu']['since']) // 60)} 分钟高于 {TH['cpu_sustained']}%")
        else:
            if st is not None:
                _alert_state['cpu'] = None
                _resolve('cpu', 'CPU 负载已回落')
    except Exception:
        pass


def _alerts_payload() -> dict:
    return {
        'active': [a for a in _alerts if a['state'] == 'active'],
        'recent': list(_alert_log)[-30:],
        'thresholds': TH,
        'ts': int(time.time()),
    }


# ───────────────────────── HTTP ─────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = 'ppanel/1.0'

    def log_message(self, *a):        # 别把面板自己的日志也写进 nginx log
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html'):
            try:
                with open(INDEX, 'rb') as f:
                    self._send(200, f.read(), 'text/html; charset=utf-8')
            except FileNotFoundError:
                self._send(500, b'index.html missing', 'text/plain; charset=utf-8')
            return
        if path == '/api/stats':
            try:
                snap = collect()
                _check_alerts(snap)     # 有完整快照了，顺手判告警，不额外探测
                self._send(200, json.dumps(snap, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            except Exception as e:
                self._send(500, json.dumps({'error': str(e)}).encode(), 'application/json; charset=utf-8')
            return
        if path == '/api/history':
            # 带 range 参数时走落盘历史（长周期），否则用内存缓冲（秒级）
            qs = self.path.split('?', 1)[1] if '?' in self.path else ''
            m = re.search(r'range=([0-9a-z]+)', qs)
            if m and m.group(1) in RANGES:
                self._send(200, json.dumps(_load_archive(m.group(1)), ensure_ascii=False).encode(),
                           'application/json; charset=utf-8')
            else:
                self._send(200, json.dumps(list(_history), ensure_ascii=False).encode(),
                           'application/json; charset=utf-8')
            return
        if path == '/api/svc':
            # 所有服务的详情（概览页展开用）
            out = []
            for svc in SERVICES:
                try:
                    out.append(service_detail(svc['key']))
                except Exception as e:
                    out.append({'key': svc['key'], 'name': svc['name'], 'ok': False, 'error': str(e)})
            self._send(200, json.dumps(out, ensure_ascii=False).encode(),
                       'application/json; charset=utf-8')
            return
        if path.startswith('/api/svc/'):
            key = path.rsplit('/', 1)[-1]
            d = service_detail(key)
            code = 404 if d.get('error') else 200
            self._send(code, json.dumps(d, ensure_ascii=False).encode(),
                       'application/json; charset=utf-8')
            return
        if path == '/api/authlog':
            qs = self.path.split('?', 1)[1] if '?' in self.path else ''
            lim = 200
            m2 = re.search(r'limit=(\d+)', qs)
            if m2:
                try:
                    lim = max(1, min(int(m2.group(1)), AUTHLOG_MAX))
                except ValueError:
                    pass
            m3 = re.search(r'ip=([^&]+)', qs)
            ipf = m3.group(1).strip() if m3 else ''
            rows = read_authlog(limit=lim, ip_filter=ipf)
            self._send(200, json.dumps({
                'rows': rows,
                'summary': _authlog_summary(rows),
                'logpath': NGINX_LOG,
            }, ensure_ascii=False).encode(), 'application/json; charset=utf-8')
            return
        if path == '/api/alerts':
            self._send(200, json.dumps(_alerts_payload(), ensure_ascii=False).encode(),
                       'application/json; charset=utf-8')
            return
        self._send(404, b'not found', 'text/plain; charset=utf-8')


def main():
    port = int(os.environ.get('PANEL_PORT', '8090'))
    _ensure_data_dir()
    _prune_archives()
    print(f'ppanel data dir: {DATA_DIR}', flush=True)
    print(f'archive ranges: {"/".join(RANGES)}', flush=True)
    # 后台采样：保证任何时候打开页面都有历史曲线
    threading.Thread(target=_sampler_loop, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    srv.daemon_threads = True
    print(f'ppanel listening on 127.0.0.1:{port}', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
