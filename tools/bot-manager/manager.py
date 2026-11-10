"""Local QQ bot console. Standard library only; one window owns its SSH children."""
import argparse
import atexit
import collections
import ctypes
from ctypes import wintypes
import datetime as dt
import http.server
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from cloud_recovery import CloudRecovery, CloudError

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / 'config.json'
APP_ID = 'aliyun-qq-bot-console-v1'
APP_VERSION = '1.2.0'
TUNNEL_LOG = logging.getLogger('qqbot-tunnel')
TUNNEL_LOG.addHandler(logging.NullHandler())


class WindowsChildJob:
    """The kernel kills owned SSH processes even when the console is forcibly closed."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return

        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                        ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

        class IOCounters(ctypes.Structure):
            _fields_ = [(field, ctypes.c_ulonglong) for field in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IOCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = self.kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.kernel.CloseHandle(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle

    def assign(self, process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def hidden_flags():
    return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def friendly_ssh_error(detail):
    lowered = detail.lower()
    if 'timed out' in lowered or 'timeout' in lowered:
        return '服务器 SSH 响应超时，正在自动重试。'
    if 'permission denied' in lowered:
        return 'SSH 密钥认证未通过，请检查密钥和服务器登录权限。'
    if 'host key verification failed' in lowered:
        return '服务器身份校验未通过，请核对服务器密钥。'
    if 'address already in use' in lowered or 'cannot listen' in lowered:
        return '本地访问端口被占用，请关闭旧隧道后重新启动。'
    if 'broken pipe' in lowered:
        return 'SSH 连接在握手期间中断，正在自动重试。'
    return detail[:400]


def stop_legacy_tunnels(config):
    """Migrate only the exact tunnels created for this deployment in the earlier chat."""
    if os.name != 'nt':
        return
    command = "Get-CimInstance Win32_Process -Filter \"name='ssh.exe'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            creationflags=hidden_flags(), timeout=20)
    if result.returncode:
        raise RuntimeError('无法检查现有 SSH 隧道，请关闭旧隧道后重试。')
    entries = json.loads(result.stdout.strip() or '[]')
    if isinstance(entries, dict):
        entries = [entries]
    expected_key = str(Path(config['key_path'])).lower()
    for entry in entries:
        commandline = (entry.get('CommandLine') or '').lower()
        if (expected_key in commandline and f"{config['user']}@{config['host']}" in commandline
                and ' -n ' in commandline and ' -l ' in commandline
                and any(f':{port}:127.0.0.1:' in commandline for port in [6185, 16185, 5099, 6081])):
            # PID and command line have just been checked; never terminate other SSH sessions.
            subprocess.run(['taskkill.exe', '/PID', str(int(entry['ProcessId'])), '/F'],
                           capture_output=True, creationflags=hidden_flags(), timeout=5)
            print('已接管旧版隧道，改由本窗口统一管理。', flush=True)


def available_port(preferred):
    for candidate in [preferred, *range(preferred + 1, preferred + 50)]:
        with socket.socket() as sock:
            if os.name == 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind(('127.0.0.1', candidate))
                return candidate
            except OSError:
                pass
    raise RuntimeError(f'本地端口 {preferred} 附近没有可用端口。')


class Monitor:
    def __init__(self, config, job, process_factory=None, recovery_factory=CloudRecovery):
        self.config, self.job = config, job
        self.process_factory = process_factory or subprocess.Popen
        self.stop_event = threading.Event()
        self.retry_wakeup = threading.Event()
        self.lock = threading.RLock()
        settings = config.get('tunnel', {})
        self.reconnect_delay = float(settings.get('reconnect_seconds', 10))
        self.startup_timeout = float(settings.get('startup_timeout_seconds', 40))
        self.telemetry_timeout = float(settings.get('telemetry_timeout_seconds', 35))
        self.connect_timeout = int(settings.get('connect_timeout_seconds', 12))
        self.alive_interval = int(settings.get('server_alive_interval_seconds', 15))
        self.alive_count = int(settings.get('server_alive_count_max', 2))
        if min(self.reconnect_delay, self.startup_timeout, self.telemetry_timeout,
               self.connect_timeout, self.alive_interval, self.alive_count) <= 0:
            raise ValueError('SSH 保活与重连时间必须大于零。')
        self.process = None
        self.process_started = None
        self.last_sample_monotonic = None
        self.disconnect_reason = ''
        self.retry_deadline = None
        self.reconnect_count = 0
        self.snapshot = None
        self.received_at = None
        self.connected_at = None
        self.state = 'connecting'
        self.error = ''
        self.logs = collections.deque(maxlen=1200)
        self.log_id = 0
        self.seen_logs = set()
        self.seen_order = collections.deque()
        self.history = collections.deque(maxlen=60)
        self.stderr = collections.deque(maxlen=5)
        self.retry_in = 0
        self.generation = 0
        self.ports = {name: available_port(int(item['local_port'])) for name, item in config['services'].items()}
        self.recovery = recovery_factory(config, BASE, self._healthy_for_cloud, self._probe_for_cloud,
                                         self.reconnect, config_path=CONFIG_PATH) if recovery_factory else None

    def _healthy_for_cloud(self):
        with self.lock:
            return (self.state == 'connected' and self.received_at is not None
                    and time.time()-self.received_at < 18 and not self.stop_event.is_set())

    def _probe_for_cloud(self):
        if self.stop_event.is_set():
            return 'cancelled'
        args = [self.config['ssh_path'], '-T', '-i', self.config['key_path'],
                '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=7',
                '-o','ConnectionAttempts=1','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=1',
                '-p',str(self.config.get('ssh_port',22)),f"{self.config['user']}@{self.config['host']}",'true']
        process = None
        try:
            process = subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                       text=True,encoding='utf-8',errors='replace',creationflags=hidden_flags())
            with self.lock:
                if self.stop_event.is_set():
                    process.terminate()
                    return 'cancelled'
                self.job.assign(process)
            _, error = process.communicate(timeout=12)
            if process.returncode == 0:
                return 'healthy'
            if any(word in error.lower() for word in ['permission denied','host key','bad permissions','no such file','identity file']):
                return 'configuration_error'
            return 'unresponsive'
        except subprocess.TimeoutExpired:
            return 'unresponsive'
        except OSError:
            return 'configuration_error'
        finally:
            if process:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)
                for stream in [process.stdout,process.stderr]:
                    if stream:
                        stream.close()

    def start(self):
        self.supervisor = threading.Thread(target=self._supervise, name='ssh-monitor', daemon=True)
        self.watchdog = threading.Thread(target=self._watchdog, name='ssh-watchdog', daemon=True)
        self.supervisor.start()
        self.watchdog.start()
        if self.recovery:
            self.recovery.start()

    def _watchdog(self):
        while not self.stop_event.wait(.5):
            with self.lock:
                process = self.process
                if not process or process.poll() is not None or self.disconnect_reason:
                    continue
                now = time.monotonic()
                if self.last_sample_monotonic is None:
                    expired = now - self.process_started > self.startup_timeout
                    reason = 'SSH 启动后一直未收到服务器数据，正在重新建立连接。'
                else:
                    expired = now - self.last_sample_monotonic > self.telemetry_timeout
                    reason = '服务器数据长时间未更新，已检测到连接卡住，正在重新建立连接。'
                if expired:
                    self.disconnect_reason = reason
                    self.error = reason
                    self.state = 'reconnecting'
                    try:
                        process.terminate()
                    except OSError:
                        pass

    def _read_errors(self, process):
        for line in process.stderr:
            with self.lock:
                if self.process is process:
                    self.stderr.append(line.strip()[:400])

    def _supervise(self):
        while not self.stop_event.is_set():
            process = None
            with self.lock:
                if self.stop_event.is_set():
                    break
                self.state = 'connecting' if self.generation == 0 else 'reconnecting'
                if self.generation:
                    self.reconnect_count += 1
                self.generation += 1
                self.stderr.clear()
                self.disconnect_reason = ''
                self.last_sample_monotonic = None
                self.process_started = None
                self.retry_deadline = None
                self.retry_in = 0
                self.retry_wakeup.clear()
                TUNNEL_LOG.info('SSH connection attempt %s', self.generation)
            try:
                args = [self.config['ssh_path'], '-T', '-i', self.config['key_path'],
                        '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                        '-o', f'ConnectTimeout={self.connect_timeout}',
                        '-o', 'ConnectionAttempts=1', '-o', 'ExitOnForwardFailure=yes',
                        '-o', f'ServerAliveInterval={self.alive_interval}',
                        '-o', f'ServerAliveCountMax={self.alive_count}', '-o', 'TCPKeepAlive=yes',
                        '-p', str(self.config.get('ssh_port', 22))]
                for name, item in self.config['services'].items():
                    args += ['-L', f"127.0.0.1:{self.ports[name]}:127.0.0.1:{item['remote_port']}"]
                args += [f"{self.config['user']}@{self.config['host']}", 'python3 -u - bot-panel-agent']
                process = self.process_factory(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               text=True, encoding='utf-8', errors='replace', bufsize=1,
                                               creationflags=hidden_flags())
                with self.lock:
                    if self.stop_event.is_set():
                        process.terminate()
                        break
                    # Register under the same lock used by close(), so closing the
                    # window during startup cannot leave an unowned SSH process.
                    self.job.assign(process)
                    self.process = process
                    self.process_started = time.monotonic()
                threading.Thread(target=self._read_errors, args=(process,), daemon=True).start()
                process.stdin.write((BASE / 'remote_agent.py').read_text(encoding='utf-8'))
                process.stdin.close()
                for line in process.stdout:
                    if self.stop_event.is_set():
                        break
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    if data.get('type') != 'snapshot':
                        continue
                    now = time.time()
                    with self.lock:
                        if self.state != 'connected':
                            self.connected_at = now
                            print('SSH 已连接，监控数据已更新。', flush=True)
                            TUNNEL_LOG.info('SSH connected; monitoring data received')
                        self.state = 'connected'
                        self.error = ''
                        self.retry_in = 0
                        self.received_at = now
                        self.last_sample_monotonic = time.monotonic()
                        self.snapshot = {key: value for key, value in data.items() if key != 'logs'}
                        for row in data.get('logs', []):
                            fingerprint = (row.get('service'), row.get('time'), row.get('message'))
                            if fingerprint in self.seen_logs:
                                continue
                            if len(self.seen_order) >= 2400:
                                self.seen_logs.discard(self.seen_order.popleft())
                            self.seen_order.append(fingerprint)
                            self.seen_logs.add(fingerprint)
                            self.log_id += 1
                            self.logs.append({**row, 'id': self.log_id})
                        server = data.get('server', {})
                        self.history.append({'time': now, 'cpu': server.get('cpu_percent'),
                                             'memory': round(server.get('memory_used', 0) / max(1, server.get('memory_total', 1)) * 100, 1)})
                    if self.recovery:
                        self.recovery.note_healthy()
                if not self.stop_event.is_set():
                    raise RuntimeError(self.disconnect_reason or (self.stderr[-1] if self.stderr else 'SSH 连接已断开。'))
            except Exception as exc:
                if process and isinstance(exc, (BrokenPipeError, OSError)):
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                detail = self.disconnect_reason or (self.stderr[-1] if self.stderr else str(exc))
                message = friendly_ssh_error(detail)
                with self.lock:
                    self.state = 'reconnecting'
                    self.error = message
                if not self.stop_event.is_set():
                    print(message, flush=True)
                    TUNNEL_LOG.warning('%s', message)
                    if self.recovery:
                        self.recovery.note_failure(message)
            finally:
                if process:
                    try:
                        process.terminate()
                        process.wait(timeout=4)
                    except (OSError, subprocess.TimeoutExpired):
                        try:
                            process.kill()
                            process.wait(timeout=3)
                        except OSError:
                            pass
                    for stream in [process.stdin, process.stdout, process.stderr]:
                        if stream and not stream.closed:
                            try:
                                stream.close()
                            except (OSError, ValueError):
                                pass
                with self.lock:
                    if self.process is process:
                        self.process = None
            if not self.stop_event.is_set():
                with self.lock:
                    self.retry_in = self.reconnect_delay
                    self.retry_deadline = time.monotonic() + self.reconnect_delay
                self.retry_wakeup.wait(self.reconnect_delay)

    def public_status(self):
        with self.lock:
            age = time.time() - self.received_at if self.received_at else None
            state = 'stale' if self.state == 'connected' and age is not None and age > 18 else self.state
            links = {name: f"http://127.0.0.1:{self.ports[name]}" + item.get('path', '/')
                     for name, item in self.config['services'].items()}
            retry_remaining = max(0, int(self.retry_deadline - time.monotonic() + .999)) if self.retry_deadline else 0
            return {'app_version': APP_VERSION, 'connection': {'state': state, 'error': self.error,
                    'last_update': self.received_at, 'connected_at': self.connected_at, 'retry_in': retry_remaining,
                    'generation': self.generation, 'reconnect_count': self.reconnect_count,
                    'ssh_pid': self.process.pid if self.process and self.process.poll() is None else None},
                    'host': self.config['host'], 'user': self.config['user'],
                    'links': links, 'known_bot': self.config.get('known_bot', {}), 'snapshot': self.snapshot,
                    'history': list(self.history), 'log_count': len(self.logs),
                    'cloud_recovery':self.recovery.public_status() if self.recovery else None}

    def reconnect(self):
        with self.lock:
            self.disconnect_reason = '正在按请求重新建立 SSH 连接。'
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
            self.retry_wakeup.set()

    def close(self):
        first_close = not self.stop_event.is_set()
        self.stop_event.set()
        self.retry_wakeup.set()
        if self.recovery:
            self.recovery.close()
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.terminate()
                except OSError:
                    pass
            self.job.close()
        if first_close:
            TUNNEL_LOG.info('Local manager closed; SSH and automatic retries stopped')


class PanelServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_handler(monitor, token, shutdown):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'BotConsole'

        def log_message(self, *_):
            pass

        def check_host(self):
            expected = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if self.headers.get('Host') not in expected:
                self.send_error(403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in {'http://' + host for host in expected}:
                self.send_error(403)
                return False
            return True

        def authorized(self):
            if not self.check_host():
                return False
            if not secrets.compare_digest(self.headers.get('X-Panel-Token', ''), token):
                self.send_error(403)
                return False
            return True

        def respond(self, data, mime='application/json; charset=utf-8', status=200):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            if not self.check_host():
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == '/health':
                return self.respond({'app': APP_ID, 'version': APP_VERSION})
            if parsed.path.startswith('/api/'):
                if not self.authorized():
                    return
                if parsed.path == '/api/status':
                    return self.respond(monitor.public_status())
                if parsed.path == '/api/logs':
                    query = urllib.parse.parse_qs(parsed.query)
                    service = query.get('service', ['all'])[0]
                    with monitor.lock:
                        rows = [row for row in monitor.logs if service == 'all' or row['service'] == service]
                    return self.respond({'logs': rows})
                return self.respond({'error': 'Not found'}, status=404)
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
                      '/cloud-setup': ('cloud-setup.html', 'text/html; charset=utf-8'),
                      '/setup.js': ('setup.js', 'text/javascript; charset=utf-8'),
                      '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
            if parsed.path not in assets:
                return self.respond({'error': 'Not found'}, status=404)
            name, mime = assets[parsed.path]
            data = (BASE / 'web' / name).read_bytes()
            if name == 'index.html':
                data = data.replace(b'__PANEL_TOKEN__', token.encode('ascii'))
            self.respond(data, mime)

        def do_POST(self):
            if not self.authorized():
                return
            if self.path.startswith('/api/cloud/'):
                if not monitor.recovery:
                    return self.respond({'error':'云端恢复模块不可用。'},status=503)
                try:
                    length = int(self.headers.get('Content-Length','0'))
                    if length < 0 or length > 8192:
                        return self.respond({'error':'请求内容过大。'},status=413)
                    body = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(body,dict):
                        raise ValueError('请求格式错误。')
                    if self.path == '/api/cloud/credentials':
                        key_id,key_secret=body.get('access_key_id'),body.get('access_key_secret')
                        if not isinstance(key_id,str) or not isinstance(key_secret,str):
                            raise ValueError('请填写 AccessKey ID 和 AccessKey Secret。')
                        return self.respond(monitor.recovery.configure(key_id,key_secret))
                    if self.path == '/api/cloud/enabled':
                        return self.respond(monitor.recovery.set_enabled(body.get('enabled')))
                    if self.path == '/api/cloud/validate':
                        return self.respond(monitor.recovery.validate())
                    return self.respond({'error':'Not found'},status=404)
                except (CloudError,ValueError,RuntimeError) as exc:
                    return self.respond({'error':str(exc)},status=400)
                except OSError:
                    return self.respond({'error':'本机无法安全保存或读取配置，请检查目录权限。'},status=400)
            if self.path == '/api/reconnect':
                monitor.reconnect()
                return self.respond({'ok': True})
            if self.path == '/api/shutdown':
                self.respond({'ok': True})
                threading.Thread(target=shutdown, daemon=True).start()
                return
            self.respond({'error': 'Not found'}, status=404)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    config = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
    port = int(config.get('panel_port', 18766))
    url = f'http://127.0.0.1:{port}/'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + 'health', timeout=1) as response:
            if json.load(response).get('app') == APP_ID:
                print('管理面板已启动：' + url, flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
    except Exception:
        pass
    if not Path(config['key_path']).is_file():
        raise RuntimeError('找不到 SSH 密钥：' + config['key_path'])
    if not Path(config['ssh_path']).is_file():
        raise RuntimeError('找不到 Windows SSH 客户端。')
    handler = RotatingFileHandler(BASE/'tunnel-events.log', maxBytes=1024*1024,
                                 backupCount=2, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    TUNNEL_LOG.addHandler(handler)
    TUNNEL_LOG.setLevel(logging.INFO)
    # Reserve the HTTP port before replacing the old tunnels.
    job = WindowsChildJob()
    monitor = Monitor(config, job)
    token = secrets.token_urlsafe(32)
    server = None
    stop_once = threading.Event()

    def shutdown():
        if stop_once.is_set():
            return
        stop_once.set()
        monitor.close()
        if server:
            server.shutdown()

    try:
        server = PanelServer(('127.0.0.1', port), make_handler(monitor, token, shutdown))
        stop_legacy_tunnels(config)
        # Re-evaluate after migration so the established entry-point ports stay the same.
        monitor.ports = {name: available_port(int(item['local_port'])) for name, item in config['services'].items()}
        atexit.register(monitor.close)
        if os.name == 'nt':
            ctypes.windll.kernel32.SetConsoleTitleW('QQ Bot 管理面板 · 关闭此窗口即断开 SSH')
        for sig in [signal.SIGINT, signal.SIGTERM]:
            signal.signal(sig, lambda *_: threading.Thread(target=shutdown, daemon=True).start())
        print('\n  QQ BOT  /  本地管理面板\n', flush=True)
        print('  管理面板  ' + url, flush=True)
        print('  阿里云    ' + config['host'], flush=True)
        print('\n  请保持此窗口打开。关闭窗口将断开 SSH，云端机器人继续运行。', flush=True)
        print('  也可在管理面板中点击“断开并退出”。\n', flush=True)
        monitor.start()
        if not args.no_browser:
            threading.Timer(.6, lambda: webbrowser.open(url)).start()
        server.serve_forever(poll_interval=.25)
    finally:
        monitor.close()
        if server:
            server.server_close()
        print('本地面板已关闭，SSH 连接已断开。', flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print('\n启动失败：' + str(exc), file=sys.stderr, flush=True)
        sys.exit(1)
