"""Guarded Alibaba Cloud recovery. Credentials stay in Windows DPAPI storage."""
import base64
import ctypes
from ctypes import wintypes
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LOG = logging.getLogger('qqbot-tunnel')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


class CredentialStore:
    def __init__(self, path, instance_id):
        self.path = Path(path)
        self.entropy = ('qqbot-cloud-recovery:' + instance_id).encode()

    def _crypt(self, raw, decrypt=False):
        if os.name != 'nt':
            raise RuntimeError('阿里云凭证保管需要 Windows 账号加密。')
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        raw_buffer = ctypes.create_string_buffer(raw)
        entropy_buffer = ctypes.create_string_buffer(self.entropy)
        incoming = Blob(len(raw), ctypes.cast(raw_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy = Blob(len(self.entropy), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = Blob()
        if decrypt:
            operation = crypt.CryptUnprotectData
            operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            success = operation(ctypes.byref(incoming), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing))
        else:
            operation = crypt.CryptProtectData
            operation.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            success = operation(ctypes.byref(incoming), 'QQ Bot cloud recovery', ctypes.byref(entropy), None, None, 1, ctypes.byref(outgoing))
        try:
            if not success:
                raise RuntimeError('Windows 无法解密此凭证，请用原 Windows 账号重新配置。')
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            ctypes.memset(raw_buffer, 0, len(raw_buffer))
            if outgoing.data:
                kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))

    def save(self, credentials):
        encrypted = self._crypt(json.dumps(credentials).encode())
        atomic_json(self.path, {'version': 1, 'dpapi': base64.b64encode(encrypted).decode()})

    def load(self):
        value = json.loads(self.path.read_text(encoding='utf-8'))
        return json.loads(self._crypt(base64.b64decode(value['dpapi'], validate=True), decrypt=True))

    def exists(self):
        return self.path.is_file()


class CloudError(RuntimeError):
    def __init__(self, code):
        self.code = re.sub(r'[^A-Za-z0-9_.-]', '', str(code))[:100] or 'CloudApiError'
        super().__init__('阿里云接口校验失败：' + self.code)


class EcsApi:
    ENDPOINT = 'https://ecs.aliyuncs.com/'

    def __init__(self, credentials, region):
        self.credentials = credentials
        self.region = region
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def encode(value):
        return urllib.parse.quote(str(value), safe='-_.~')

    @classmethod
    def signature(cls, parameters, secret):
        canonical = '&'.join(cls.encode(key) + '=' + cls.encode(value) for key, value in sorted(parameters.items()))
        text = 'POST&%2F&' + cls.encode(canonical)
        return base64.b64encode(hmac.new((secret + '&').encode(), text.encode(), hashlib.sha1).digest()).decode()

    def call(self, action, **parameters):
        if action not in {'DescribeInstances', 'RebootInstance'}:
            raise ValueError('Unsupported cloud operation')
        values = {'Action': action, 'Version': '2014-05-26', 'RegionId': self.region,
                  'Format': 'JSON', 'AccessKeyId': self.credentials['access_key_id'],
                  'SignatureMethod': 'HMAC-SHA1', 'SignatureVersion': '1.0',
                  'SignatureNonce': str(uuid.uuid4()),
                  'Timestamp': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), **parameters}
        values['Signature'] = self.signature(values, self.credentials['access_key_secret'])
        request = urllib.request.Request(self.ENDPOINT, data=urllib.parse.urlencode(values).encode(),
                                         headers={'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
        try:
            with self.opener.open(request, timeout=12) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.loads(exc.read(65536))
            except (ValueError, OSError):
                raise CloudError('Http' + str(exc.code)) from None
        except (OSError, urllib.error.URLError):
            raise CloudError('NetworkUnavailable') from None
        if result.get('Code'):
            raise CloudError(result['Code'])
        return result

    def describe(self, instance_id, expected_ip):
        result = self.call('DescribeInstances', InstanceIds=json.dumps([instance_id]), PageSize='10')
        instances = result.get('Instances', {}).get('Instance', [])
        if len(instances) != 1 or instances[0].get('InstanceId') != instance_id:
            raise CloudError('TargetInstanceMismatch')
        item = instances[0]
        ips = item.get('PublicIpAddress', {}).get('IpAddress', [])
        eip = item.get('EipAddress', {}).get('IpAddress')
        if expected_ip not in ips and expected_ip != eip:
            raise CloudError('TargetPublicIpMismatch')
        if item.get('RegionId') != self.region:
            raise CloudError('TargetRegionMismatch')
        return item

    def dry_run(self, instance_id):
        try:
            self.call('RebootInstance', InstanceId=instance_id, ForceStop='false', DryRun='true')
        except CloudError as exc:
            if exc.code == 'DryRunOperation':
                return True
            raise
        return True

    def reboot(self, instance_id):
        return self.call('RebootInstance', InstanceId=instance_id, ForceStop='false', DryRun='false')


class CloudRecovery:
    def __init__(self, config, directory, healthy, probe_ssh, reconnect,
                 config_path=None, api_factory=EcsApi, store=None, clock=time):
        self.settings = config.setdefault('cloud_recovery', {})
        self.root = Path(directory)
        self.config_path = Path(config_path) if config_path else None
        self.clock = clock
        self.healthy = healthy
        self.probe_ssh = probe_ssh
        self.reconnect = reconnect
        self.api_factory = api_factory
        self.store = store or CredentialStore(self.root/'cloud-credentials.dpapi.json', self.settings.get('instance_id',''))
        self.ledger_path = self.root/'cloud-recovery-state.json'
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.dispatch_lock = threading.Lock()
        self.phase = 'unconfigured'
        self.message = '尚未配置阿里云 API 凭证，自动重启不会执行。'
        self.fault_since = None
        self.failures = 0
        self.next_check = 0
        self.last_tick = self.clock.monotonic()
        self.ledger_error = False
        self.ledger = {'version':1, 'ever_healthy':False, 'episode_rebooted':False, 'grace_until':0, 'reboots':[]}
        if self.ledger_path.exists():
            try:
                data = json.loads(self.ledger_path.read_text(encoding='utf-8'))
                if (data.get('version') != 1 or not isinstance(data.get('reboots'), list)
                        or not isinstance(data.get('ever_healthy'), bool)
                        or not isinstance(data.get('episode_rebooted'), bool)
                        or not isinstance(data.get('grace_until'), (int,float))
                        or any(not isinstance(row,dict) or not isinstance(row.get('time'),(int,float)) for row in data['reboots'])):
                    raise ValueError('Invalid recovery ledger')
                self.ledger = data
            except (OSError, ValueError, TypeError):
                self.ledger_error = True

    def _save_ledger(self):
        try:
            atomic_json(self.ledger_path, self.ledger)
        except (OSError, ValueError):
            self.ledger_error = True
            raise RuntimeError('无法保存重启次数记录，已暂停自动重启。') from None

    def _persist_settings(self):
        if self.config_path:
            value = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
            value['cloud_recovery'] = dict(self.settings)
            atomic_json(self.config_path, value)

    def _set(self, phase, message):
        with self.lock:
            self.phase, self.message = phase, message

    def note_healthy(self):
        with self.lock:
            self.fault_since = None
            self.failures = 0
            self.next_check = 0
            if not self.ledger_error and (not self.ledger['ever_healthy'] or self.ledger['episode_rebooted'] or self.ledger['grace_until']):
                self.ledger['ever_healthy'] = True
                self.ledger['episode_rebooted'] = False
                self.ledger['grace_until'] = 0
                try:
                    self._save_ledger()
                except RuntimeError:
                    pass
            self.phase, self.message = 'monitoring', 'SSH 正常，持续监测中。'

    def note_failure(self, detail):
        if any(word in detail.lower() for word in ['permission denied','host key','密钥','身份校验','端口被占用','按请求']):
            with self.lock:
                self.fault_since = None
                self.failures = 0
                self.phase, self.message = 'blocked', '认证、端口或手动操作异常，不会触发实例重启。'
            return
        with self.lock:
            if self.fault_since is None:
                self.fault_since = self.clock.monotonic()
            self.failures += 1

    def configure(self, key_id, key_secret):
        key_id, key_secret = key_id.strip(), key_secret.strip()
        if not re.fullmatch(r'[A-Za-z0-9]{8,128}',key_id) or not re.fullmatch(r'[A-Za-z0-9+/=_-]{8,256}',key_secret):
            raise ValueError('请填写有效的 AccessKey ID 和 AccessKey Secret。')
        credentials = {'access_key_id':key_id, 'access_key_secret':key_secret}
        api = self.api_factory(credentials,self.settings['region_id'])
        item = api.describe(self.settings['instance_id'],self.settings['expected_ip'])
        if item.get('Status') != 'Running':
            raise ValueError('请在目标实例处于运行中时完成权限配置。')
        api.dry_run(self.settings['instance_id'])
        with self.lock:
            if self.stop_event.is_set():
                raise ValueError('管理面板正在退出，请重新启动后配置。')
            self.store.save(credentials)
            self.settings['enabled'] = True
            self._persist_settings()
            self.phase, self.message = 'monitoring','凭证已加密保存，重启权限预检通过；未执行真实重启。'
        LOG.info('Cloud recovery credentials validated and saved using Windows DPAPI; no reboot sent')
        return self.public_status()

    def set_enabled(self, enabled):
        if not isinstance(enabled,bool):
            raise ValueError('开关值必须为 true 或 false。')
        with self.lock:
            if enabled and not self.store.exists():
                raise ValueError('请先配置并校验阿里云凭证。')
            self.settings['enabled'] = enabled
            self._persist_settings()
        return self.public_status()

    def validate(self):
        if not self.store.exists():
            raise ValueError('请先填写阿里云 API 凭证。')
        api = self.api_factory(self.store.load(),self.settings['region_id'])
        api.describe(self.settings['instance_id'],self.settings['expected_ip'])
        api.dry_run(self.settings['instance_id'])
        return {'ok':True,'message':'实例身份和重启权限预检通过，没有执行真实重启。'}

    def start(self):
        self.thread = threading.Thread(target=self._run,name='cloud-recovery',daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()

    def _run(self):
        while not self.stop_event.wait(5):
            try:
                self.check_once()
            except Exception as exc:
                # Do not expose exception arguments that might contain credentials.
                self._set('error','云端恢复检查失败，已保留 SSH 自动重连；请检查配置。')
                LOG.warning('Cloud recovery check failed (%s)',type(exc).__name__)

    def check_once(self):
        if self.stop_event.is_set():
            return
        now, wall = self.clock.monotonic(),self.clock.time()
        with self.lock:
            if now-self.last_tick>30:
                self.fault_since = None
                self.failures = 0
            self.last_tick = now
            if not self.settings.get('enabled',False):
                return self._set('paused','自动重启已暂停，SSH 自动重连仍然运行。')
            if self.ledger_error:
                return self._set('ledger_error','重启记录无法读取或保存，已暂停自动重启。')
            if not self.store.exists():
                return self._set('unconfigured','等待本机填写并校验阿里云 API 凭证。')
            if not self.ledger['ever_healthy']:
                return self._set('waiting_connection','等待至少一次成功连接，防止配置错误触发重启。')
            if self.ledger['grace_until']>wall:
                return self._set('waiting_boot','实例正在启动或重启，等待恢复，不会重复重启。')
            if self.ledger['episode_rebooted']:
                return self._set('waiting_boot','本次故障已经发送过重启请求，等待恢复。')
            events = [r for r in self.ledger['reboots'] if r['time']>wall-86400]
            if len(events)>=int(self.settings.get('max_reboots_24h',2)):
                return self._set('daily_limit','已达到 24 小时内的自动重启次数上限。')
            if events and wall-max(r['time'] for r in events)<float(self.settings.get('cooldown_seconds',3600)):
                return self._set('cooldown','处于自动重启冷却期，SSH 会继续重连。')
            if self.fault_since is None:
                return self._set('monitoring','SSH 正常，持续监测中。')
            elapsed = now-self.fault_since
            limit = float(self.settings.get('outage_seconds',300))
            if elapsed<limit or self.failures<int(self.settings.get('failure_attempts',5)):
                return self._set('fault_countdown',f'持续故障确认中：已重连失败 {self.failures} 次，最长还需确认 {max(0,int(limit-elapsed))} 秒。')
            if now<self.next_check:
                return
            fault = self.fault_since
            self.next_check = now+60
            self.phase,self.message='checking','正在确认云端状态和独立 SSH 响应。'
        if self.healthy():
            return self.note_healthy()
        try:
            api = self.api_factory(self.store.load(),self.settings['region_id'])
            item = api.describe(self.settings['instance_id'],self.settings['expected_ip'])
            if item.get('Status') != 'Running':
                with self.lock:
                    self.ledger['grace_until']=wall+float(self.settings.get('startup_grace_seconds',600))
                    self._save_ledger()
                return self._set('waiting_boot','云端实例不处于运行中，等待状态恢复，不会发送重启。')
            if self.stop_event.is_set():
                return
            probe = self.probe_ssh()
            if self.stop_event.is_set():
                return
            if probe=='healthy':
                with self.lock:
                    self.fault_since=None
                    self.failures=0
                self.reconnect()
                return self._set('monitoring','独立 SSH 检查正常，正在重建本地隧道，不重启实例。')
            if probe!='unresponsive':
                return self._set('blocked','独立 SSH 检查发现认证或本机配置问题，不执行实例重启。')
            if self.healthy():
                return self.note_healthy()
            with self.dispatch_lock:
                with self.lock:
                    if (self.stop_event.is_set() or self.fault_since!=fault or not self.settings.get('enabled')):
                        return
                    # Persist the reservation BEFORE sending. An ambiguous network
                    # failure must never result in repeated reboot requests.
                    event={'time':self.clock.time(),'outcome':'reserved'}
                    self.ledger['reboots']=[r for r in self.ledger['reboots'] if r['time']>self.clock.time()-86400]+[event]
                    self.ledger['episode_rebooted']=True
                    self.ledger['grace_until']=self.clock.time()+float(self.settings.get('startup_grace_seconds',600))
                    self._save_ledger()
                    self.phase,self.message='rebooting','已确认持续故障，正在请求阿里云普通重启。'
                if self.stop_event.is_set():
                    return
                try:
                    result=api.reboot(self.settings['instance_id'])
                except Exception:
                    with self.lock:
                        event['outcome']='unknown'
                        self._save_ledger()
                    raise
                with self.lock:
                    event['outcome']='accepted'
                    event['request_id']=str(result.get('RequestId',''))[:100]
                    self._save_ledger()
                    self.phase,self.message='waiting_boot','阿里云已接受普通重启请求，正在等待 SSH 恢复。'
                LOG.warning('Cloud normal reboot accepted for configured instance; request=%s',event['request_id'])
        except CloudError as exc:
            with self.lock:
                if not self.ledger['episode_rebooted']:
                    self.fault_since=self.clock.monotonic()
                    self.failures=0
            self._set('error',str(exc)+'；不会盲目重复发送重启请求。')
            LOG.warning('Cloud recovery API unavailable (%s)',exc.code)

    def public_status(self):
        with self.lock:
            configured=self.store.exists()
            phase,message=self.phase,self.message
            if self.ledger_error:
                phase,message='ledger_error','重启记录异常，自动重启已暂停。'
            elif not configured:
                phase,message='unconfigured','待配置阿里云 API 凭证；当前不会自动重启实例。'
            elif not self.settings.get('enabled',False):
                phase,message='paused','自动重启已暂停。'
            return {'phase':phase,'message':message,'configured':configured,
                    'enabled':bool(self.settings.get('enabled',False)),
                    'instance_id':self.settings.get('instance_id',''),
                    'region_id':self.settings.get('region_id',''),
                    'outage_seconds':int(self.settings.get('outage_seconds',300)),
                    'cooldown_seconds':int(self.settings.get('cooldown_seconds',3600)),
                    'max_reboots_24h':int(self.settings.get('max_reboots_24h',2)),
                    'reboots_24h':len([r for r in self.ledger['reboots'] if r['time']>self.clock.time()-86400]),
                    'last_reboot':self.ledger['reboots'][-1] if self.ledger['reboots'] else None}
