"""Authoritative cumulative-login status and milestone popup gating."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def milestones():
    return json.loads(Path(__file__).with_name('accumulate_login_cfg.json').read_text())


def milestone_reached(version, days):
    return any(int(cfg['version']) == int(version) and int(cfg['num']) == int(days)
               for cfg in milestones().values())


def payload(db, uid):
    rows = db.query('SELECT version,open_sign,login_days FROM accumulate_sign WHERE uid=?', (uid,))
    if not rows:
        return None
    row = rows[0]
    version, days = int(row['version'] or 1), int(row['login_days'] or 0)
    awards = [int(r['award_id']) for r in db.query(
        'SELECT award_id FROM accumulate_sign_award WHERE uid=? ORDER BY award_id', (uid,))]
    unseen_milestone = any(int(cfg['version']) == version and int(cfg['num']) == days
                           and int(aid) not in awards for aid, cfg in milestones().items())
    return {'version': version, 'login_days': days, 'award_ids': awards,
            'open_sign': int(bool(row['open_sign']) and unseen_milestone)}
