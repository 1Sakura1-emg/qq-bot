"""Read-only monitoring worker, streamed over the same SSH connection as the tunnels."""
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path('/opt/qq-bot')
SERVICES = ('astrbot', 'snowluma')
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
TIMESTAMP = re.compile(r'^(\d{4}-\d\d-\d\dT\S+)\s+(.*)$')
previous_cpu = None
previous_network = None
previous_container_cpu = {}
since = None
log_sequence = 0
last_connected = False


def run(args, timeout=7):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stdout.strip()[:300])
    return result.stdout


def scrub(line):
    line = ANSI.sub('', line)
    if re.search(r'initial credentials:|initial password:|remote desktop password:|远程桌面密码:', line, re.I):
        return '[初始登录凭据已隐藏]'
    line = re.sub(r'(?i)(authorization[=: ]+bearer\s+)\S+', r'\1[已隐藏]', line)
    line = re.sub(r'(?i)((?:access[_-]?token|api[_-]?key|password|密码)["\s]*[:=]["\s]*)[^\s,}"\']+', r'\1[已隐藏]', line)
    return line[:2500]


def server_info():
    global previous_cpu, previous_network
    now = time.monotonic()
    cpu_fields = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    total, idle = sum(cpu_fields), cpu_fields[3] + cpu_fields[4]
    cpu = None
    if previous_cpu:
        total_delta, idle_delta = total - previous_cpu[0], idle - previous_cpu[1]
        cpu = max(0, min(100, round(100 * (1 - idle_delta / max(1, total_delta)), 1)))
    previous_cpu = total, idle
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0]) * 1024
    net_rx = net_tx = 0
    for line in Path('/proc/net/dev').read_text().splitlines()[2:]:
        name, values = line.split(':', 1)
        if name.strip().startswith(('lo', 'docker', 'veth', 'br-')):
            continue
        values = values.split()
        net_rx += int(values[0])
        net_tx += int(values[8])
    rates = {'rx': 0, 'tx': 0}
    if previous_network:
        elapsed = max(.1, now - previous_network[0])
        rates = {'rx': max(0, int((net_rx - previous_network[1]) / elapsed)),
                 'tx': max(0, int((net_tx - previous_network[2]) / elapsed))}
    previous_network = now, net_rx, net_tx
    release = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            release[key] = value.strip('"')
    disk = shutil.disk_usage('/')
    return {'hostname': socket.gethostname(), 'os': release.get('PRETTY_NAME', 'Linux'),
            'arch': os.uname().machine, 'kernel': os.uname().release, 'cpu_cores': os.cpu_count(),
            'cpu_percent': cpu, 'load': list(os.getloadavg()),
            'uptime_seconds': int(float(Path('/proc/uptime').read_text().split()[0])),
            'memory_total': mem['MemTotal'], 'memory_used': mem['MemTotal'] - mem.get('MemAvailable', mem['MemFree']),
            'swap_total': mem['SwapTotal'], 'swap_used': mem['SwapTotal'] - mem['SwapFree'],
            'disk_total': disk.total, 'disk_used': disk.used, 'network': rates,
            'time': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()}


def cgroup_metrics(pid, name):
    try:
        lines = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
        relative = next(line.split(':', 2)[2] for line in lines if line.startswith('0::'))
        group = Path('/sys/fs/cgroup') / relative.lstrip('/')
        usage = int((group / 'memory.current').read_text())
        stats = dict(line.split() for line in (group / 'memory.stat').read_text().splitlines())
        usage = max(0, usage - int(stats.get('inactive_file', 0)))
        cpu_stats = dict(line.split() for line in (group / 'cpu.stat').read_text().splitlines())
        used = int(cpu_stats['usage_usec'])
        now = time.monotonic()
        previous = previous_container_cpu.get(name)
        cpu = round((used - previous[1]) / ((now - previous[0]) * 10000), 1) if previous else None
        previous_container_cpu[name] = now, used
        return {'memory_bytes': usage, 'cpu_percent': cpu}
    except (OSError, ValueError, StopIteration, KeyError):
        return {'memory_bytes': None, 'cpu_percent': None}


def service_info(errors):
    result = []
    try:
        containers = json.loads(run(['docker', 'inspect', *SERVICES]))
    except Exception as exc:
        errors.append('读取服务状态失败：' + str(exc))
        containers = []
    by_name = {item['Name'].lstrip('/'): item for item in containers}
    for name in SERVICES:
        item = by_name.get(name)
        if not item:
            result.append({'name': name, 'status': 'unknown', 'running': False})
            continue
        state = item['State']
        metrics = cgroup_metrics(state.get('Pid', 0), name) if state['Running'] else {}
        result.append({'name': name, 'status': state['Status'], 'running': state['Running'],
                       'started_at': state.get('StartedAt'), 'finished_at': state.get('FinishedAt'),
                       'restart_count': item['RestartCount'], 'image': item['Config']['Image'],
                       'version': item['Config']['Image'].rsplit(':', 1)[-1],
                       'health': state.get('Health', {}).get('Status'), **metrics})
    return result


def read_logs(errors):
    global since, log_sequence
    until = dt.datetime.now(dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    result = []
    for name in SERVICES:
        args = ['docker', 'logs', '--timestamps', '--tail', '150', '--until', until]
        if since:
            args += ['--since', since]
        args.append(name)
        try:
            output = run(args)
        except Exception as exc:
            errors.append(name + ' 日志暂不可用：' + str(exc))
            continue
        for line in output.splitlines():
            if not line.strip():
                continue
            match = TIMESTAMP.match(line)
            timestamp, text = (match.group(1), match.group(2)) if match else (until, line)
            text = scrub(text)
            log_sequence += 1
            level = 'info'
            if re.search(r'\b(error|critical|fatal|traceback)\b', text, re.I):
                level = 'error'
            elif re.search(r'\bwarn(?:ing)?\b', text, re.I):
                level = 'warn'
            result.append({'seq': log_sequence, 'service': name, 'time': timestamp, 'level': level, 'message': text})
    since = until
    return sorted(result, key=lambda row: row['time'])


def bot_info(services, errors):
    global last_connected
    connected = False
    astrbot = next((s for s in services if s['name'] == 'astrbot'), {})
    snow = next((s for s in services if s['name'] == 'snowluma'), {})
    if astrbot.get('running') and snow.get('running'):
        try:
            table = run(['docker', 'exec', 'astrbot', 'cat', '/proc/net/tcp', '/proc/net/tcp6'])
            for row in table.splitlines():
                fields = row.split()
                if len(fields) > 3 and fields[1].endswith(':1837') and fields[3] == '01':
                    connected = True
        except Exception as exc:
            errors.append('机器人连接检测失败：' + str(exc))
    accounts = []
    for path in sorted((ROOT / 'snowluma-data/config').glob('onebot_*.json')):
        match = re.fullmatch(r'onebot_(\d+)\.json', path.name)
        if match:
            accounts.append(match.group(1))
    configured_providers = 0
    admins = []
    try:
        cfg = json.loads((ROOT / 'astrbot-data/cmd_config.json').read_text(encoding='utf-8-sig'))
        configured_providers = len([p for p in cfg.get('provider', []) if p.get('enable', True)])
        admins = [str(value) for value in cfg.get('admins_id', []) if str(value).strip()]
    except (OSError, ValueError):
        pass
    # This installation has one QQ process; do not imply every saved account is online.
    active = accounts[0] if len(accounts) == 1 and connected else None
    return {'accounts': accounts, 'active_uin': active, 'connected': connected,
            'protocol': 'OneBot v11 · 反向 WebSocket', 'adapter': 'qq-snowluma',
            'model_count': configured_providers, 'admin_count': len(admins)}


def main():
    while True:
        start = time.monotonic()
        errors = []
        try:
            server = server_info()
        except Exception as exc:
            server = {}
            errors.append('服务器指标暂不可用：' + str(exc))
        services = service_info(errors)
        logs = read_logs(errors)
        bot = bot_info(services, errors)
        endpoints = {}
        for name, port in [('astrbot', 6185), ('snowluma', 5099), ('novnc', 6081)]:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.6):
                    endpoints[name] = True
            except OSError:
                endpoints[name] = False
        message = {'type': 'snapshot', 'time': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'server': server, 'services': services, 'bot': bot, 'logs': logs, 'endpoints': endpoints, 'errors': errors}
        try:
            print(json.dumps(message, ensure_ascii=False), flush=True)
        except (BrokenPipeError, OSError):
            return
        time.sleep(max(0.2, 5 - (time.monotonic() - start)))


if __name__ == '__main__':
    main()
