"""Background pool rotation and durable level-mail retries."""
import logging
import threading
import time

import game_cycles
import mail_automation

logger = logging.getLogger('runtime_scheduler')
INTERVAL = 30
_runtime = None


def notify_rotation(db):
    import generator
    import gm_actions
    from middleware import DownFrame
    clients = 0
    for player in db.query('SELECT uid FROM users'):
        uid = int(player['uid'])
        with db.transaction():
            frames = [DownFrame(cmd, generator.gen_payload(cmd, uid=uid, db=db)) for cmd in (11001, 16015)]
        clients += gm_actions.sync(uid, [f for f in frames if f.payload])['clients']
    return {'clients': clients}


def run_once(db, now_ts=None, notify=True):
    now = int(time.time()) if now_ts is None else int(now_ts)
    rotation = game_cycles.apply_rotation(db, now)
    delivery = mail_automation.run_pending(db, now)
    if notify:
        if rotation['enabled'] and not rotation['waiting'] and rotation['notify_required']:
            notify_rotation(db)
            game_cycles.rotation_notified(db, rotation)
        if delivery['sent']:
            from server_net import push_mail_notice
            for uid in sorted({item['uid'] for item in delivery['sent']}):
                try:
                    push_mail_notice(uid)
                except Exception:
                    logger.exception('等级邮件通知失败 uid=%s；已投递的邮件保持有效', uid)
    try:
        import social_runtime
        social = social_runtime.tick(db, now, notify=notify)
    except Exception:
        logger.exception('人机活跃检查失败，下次重试')
        social = {'error': 'social_tick_failed'}
    return {'rotation': rotation, 'mail': delivery, 'social': social}


class RuntimeScheduler:
    def __init__(self, db):
        self.db = db
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._status = {'last_tick_ts': 0, 'last_error': '', 'sent_count': 0, 'failed_count': 0}
        self.thread = threading.Thread(target=self._run, name='runtime-schedule', daemon=True)

    def start(self):
        game_cycles.get_settings(self.db)
        mail_automation.initialize(self.db)
        import social_service
        social_service.initialize(self.db)
        self.thread.start()
        return self

    def _run(self):
        try:
            while not self._stop.is_set():
                now = int(time.time())
                try:
                    result = run_once(self.db, now)
                    update = {'last_tick_ts': now, 'last_error': '',
                              'sent_count': len(result['mail']['sent']), 'failed_count': len(result['mail']['failures'])}
                    if result['mail']['failures']:
                        update['last_error'] = result['mail']['failures'][0]['error']
                except Exception as error:
                    logger.exception('周期与等级邮件定时检查失败，下一次检查将重试')
                    update = {'last_tick_ts': now, 'last_error': (type(error).__name__ + ': ' + str(error))[:400]}
                with self._lock:
                    self._status.update(update)
                self._wake.wait(INTERVAL)
                self._wake.clear()
        finally:
            self.db.close()

    def wake(self):
        self._wake.set()

    def status(self):
        with self._lock:
            return {**self._status, 'running': self.thread.is_alive(), 'interval_seconds': INTERVAL}

    def stop(self):
        self._stop.set()
        self._wake.set()
        self.thread.join(timeout=5)


def start(db):
    global _runtime
    if _runtime is None or not _runtime.thread.is_alive():
        _runtime = RuntimeScheduler(db).start()
    return _runtime


def wake():
    if _runtime is not None:
        _runtime.wake()


def status():
    return _runtime.status() if _runtime is not None else {'running': False, 'interval_seconds': INTERVAL,
        'last_tick_ts': 0, 'last_error': '', 'sent_count': 0, 'failed_count': 0}
