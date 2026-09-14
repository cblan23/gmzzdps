"""Verified web identities map to the existing QQ/day ledger.

Caller must authenticate OAuth server-side and authorize the reviewer. A typed
QQ number is only a claim until a reviewer independently verifies it in-group.
All mutations require the caller's existing write_database transaction.
"""
import re

ALLOWED_GROUPS = frozenset({'165966739', '1094925831', '732363944'})


def initialize(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS web_card_memberships(
      id INTEGER PRIMARY KEY,
      app_id TEXT NOT NULL,
      openid TEXT NOT NULL,
      verified_qq TEXT UNIQUE,
      verified_group TEXT,
      valid_until REAL,
      reviewed_by TEXT,
      reviewed_at REAL,
      status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','approved','revoked')),
      UNIQUE(app_id,openid))''')
    connection.execute('''CREATE TABLE IF NOT EXISTS web_card_review_audit(
      id INTEGER PRIMARY KEY, membership_id INTEGER NOT NULL,
      reviewer TEXT NOT NULL, action TEXT NOT NULL,
      qq TEXT, group_id TEXT, at REAL NOT NULL,
      FOREIGN KEY(membership_id) REFERENCES web_card_memberships(id))''')


def register_verified_identity(connection, app_id, openid):
    if not re.fullmatch(r'[0-9]{5,20}', app_id):raise ValueError('invalid_app_id')
    if not re.fullmatch(r'[A-Fa-f0-9]{32}', openid):raise ValueError('invalid_openid')
    openid=openid.upper()
    connection.execute('INSERT OR IGNORE INTO web_card_memberships(app_id,openid) VALUES(?,?)',(app_id,openid))
    return connection.execute('SELECT id FROM web_card_memberships WHERE app_id=? AND openid=?',(app_id,openid)).fetchone()[0]


def approve(connection, membership_id, qq, group, reviewer, timestamp, valid_until):
    if group not in ALLOWED_GROUPS:raise ValueError('group_not_allowed')
    if not re.fullmatch(r'[1-9][0-9]{4,15}',qq):raise ValueError('invalid_qq')
    if not reviewer or len(reviewer)>128:raise ValueError('invalid_reviewer')
    if not timestamp < valid_until <= timestamp+31*86400:raise ValueError('invalid_review_expiry')
    previous=connection.execute('SELECT verified_qq FROM web_card_memberships WHERE id=?',(membership_id,)).fetchone()
    if previous is None:raise ValueError('identity_not_found')
    # Revocation never frees a binding for another OAuth identity or QQ.
    if previous[0] is not None and previous[0]!=qq:raise ValueError('identity_rebinding_forbidden')
    connection.execute('''UPDATE web_card_memberships SET verified_qq=?,verified_group=?,
       valid_until=?,reviewed_by=?,reviewed_at=?,status='approved' WHERE id=?''',
       (qq,group,valid_until,reviewer,timestamp,membership_id))
    connection.execute('''INSERT INTO web_card_review_audit(membership_id,reviewer,action,qq,group_id,at)
       VALUES(?,?,'approve',?,?,?)''',(membership_id,reviewer,qq,group,timestamp))


def revoke(connection, membership_id, reviewer, timestamp):
    if not reviewer or len(reviewer)>128:raise ValueError('invalid_reviewer')
    if not connection.execute("UPDATE web_card_memberships SET status='revoked' WHERE id=?",(membership_id,)).rowcount:
        raise ValueError('identity_not_found')
    connection.execute("INSERT INTO web_card_review_audit(membership_id,reviewer,action,at) VALUES(?,?,'revoke',?)",(membership_id,reviewer,timestamp))


def claim_identity(connection, app_id, openid, timestamp):
    row=connection.execute('''SELECT verified_qq,verified_group FROM web_card_memberships
       WHERE app_id=? AND openid=? AND status='approved' AND valid_until>?
       AND verified_qq IS NOT NULL''',(app_id,openid.upper(),timestamp)).fetchone()
    if row is None or row[1] not in ALLOWED_GROUPS:raise PermissionError('membership_review_required')
    return row[0],row[1]
