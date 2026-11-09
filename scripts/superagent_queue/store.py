"""Persist inputs and fence effects without deriving worker liveness."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .model import Claim, INITIAL_DELAY, KINDS, MAX_DELAY, STATUSES
from .model import encoded, failure_text, identifier, timestamp
from .storage import connect, transaction
from .disposition import validate as validate_dispositions
from . import continuation


class Journal:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.connection = connect(path)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def ingest(self, source, event_id, content, key, kind, lane, payload, now=None):
        with transaction(self.connection):
            return self._ingest(source, event_id, content, key, kind, lane, payload, timestamp(now))

    def _ingest(self, source, event_id, content, key, kind, lane, payload, stamp):
        for value in (source, event_id, key, lane):
            identifier(value)
        if kind not in KINDS:
            raise ValueError('unsupported operational effect kind')
        body, work = encoded(content), encoded(payload)
        identity = hashlib.sha256(encoded({'content': content, 'key': key, 'kind': kind,
                                          'lane': lane, 'payload': payload}).encode()).hexdigest()
        before = self.connection.execute('SELECT identity FROM event WHERE source=? AND event_id=?',
                                         (source, event_id)).fetchone()
        if before:
            if before['identity'] != identity:
                raise ValueError('event identity conflict')
            self._enroll_mail(source, event_id, key, lane, stamp)
            return False
        if self.connection.execute('SELECT 1 FROM effect WHERE key=?', (key,)).fetchone():
            raise ValueError('effect identity conflict')
        seq = self.connection.execute('SELECT COALESCE(MAX(seq),0)+1 FROM effect').fetchone()[0]
        self.connection.execute('INSERT INTO event VALUES (?,?,?,?,?,?)',
                                (source, event_id, identity, body, key, stamp))
        self.connection.execute('''INSERT INTO effect
            (key,kind,lane,seq,payload,due_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)''',
                                (key, kind, lane, seq, work, stamp, stamp, stamp))
        self._enroll_mail(source, event_id, key, lane, stamp)
        return True

    def _enroll_mail(self, source, message_id, key, lane, stamp):
        if source == 'orca-inbox':
            self.connection.execute('INSERT OR IGNORE INTO mail_obligation VALUES (?,?,?,?)',
                                    (lane, message_id, key, stamp))

    def source_cursor(self, source_key):
        identifier(source_key)
        row = self.connection.execute('SELECT revision,position FROM source_cursor WHERE source_key=?',
                                      (source_key,)).fetchone()
        return {**json.loads(row['position']), 'revision': row['revision']} if row else None

    def context_replay(self, delivery):
        row = self.connection.execute('''SELECT event.content,event.effect_key,effect.kind,effect.lane,effect.payload
            FROM event JOIN effect ON effect.key=event.effect_key WHERE event.source=? AND event.event_id=?''',
            (delivery['source'], delivery['event_id'])).fetchone()
        if not row:
            return delivery
        target = {key: value for key, value in delivery['payload'].items() if key != 'text'}
        prior = json.loads(row['payload'])
        if (row['content'] != encoded(delivery['content']) or row['effect_key'] != delivery['key']
                or row['kind'] != delivery['kind'] or row['lane'] != delivery['lane']
                or {key: value for key, value in prior.items() if key != 'text'} != target):
            raise ValueError('context identity conflict')
        return {**delivery, 'payload': prior}

    def ingest_page(self, source_key, expected, position, deliveries, now=None):
        identifier(source_key)
        body, stamp = encoded(position), timestamp(now)
        with transaction(self.connection):
            current = self.source_cursor(source_key)
            if current != expected:
                raise ValueError('source cursor changed; re-read the original source page')
            added = sum(int(self._ingest(**delivery, stamp=stamp)) for delivery in deliveries)
            revision = current['revision'] + 1 if current else 1
            self.connection.execute('''INSERT INTO source_cursor VALUES (?,?,?,?)
                ON CONFLICT(source_key) DO UPDATE SET revision=excluded.revision,
                position=excluded.position,updated_at=excluded.updated_at''', (source_key, revision, body, stamp))
        return added

    def current_dispositions(self):
        rows = self.connection.execute('''SELECT * FROM disposition AS current WHERE revision=(
            SELECT MAX(revision) FROM disposition WHERE run_id=current.run_id)''')
        return {row['run_id']: {**dict(row), 'body': json.loads(row['body']), 'owner': json.loads(row['owner'])}
                for row in rows}

    def open_mail(self, run_id, limit=100):
        identifier(run_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('mail obligation page limit must be between 1 and 100')
        rows = self.connection.execute('''SELECT event.event_id,event.content,effect.status
            FROM mail_obligation JOIN effect ON effect.key=mail_obligation.effect_key
            JOIN event ON event.effect_key=effect.key
            LEFT JOIN mail_review ON mail_review.run_id=effect.lane AND mail_review.message_id=event.event_id
            WHERE event.source='orca-inbox' AND effect.lane=? AND mail_review.message_id IS NULL
            ORDER BY effect.seq LIMIT ?''', (run_id, limit))
        return [{'message_id': row['event_id'], 'content': json.loads(row['content']),
                 'delivery_status': row['status']} for row in rows]

    def record_mail_review(self, run_id, reviews, proof, now=None):
        identifier(run_id)
        stamp = timestamp(now)
        with transaction(self.connection):
            for review in reviews:
                message_id = identifier(review['message_id'])
                body = encoded({'review': review, 'proof': proof})
                existing = self.connection.execute('SELECT body FROM mail_review WHERE run_id=? AND message_id=?',
                                                   (run_id, message_id)).fetchone()
                if existing and existing['body'] != body:
                    raise ValueError('native mail review conflicts with its recorded receipt')
                if existing:
                    self.connection.execute('''UPDATE effect SET status='delivered',error='',updated_at=?
                        WHERE key=(SELECT effect_key FROM mail_obligation WHERE run_id=? AND message_id=?)
                        AND status='pending' AND claim_token IS NULL''', (stamp, run_id, message_id))
                    continue
                source = self.connection.execute('''SELECT event.event_id FROM event
                    JOIN effect ON effect.key=event.effect_key WHERE event.source='orca-inbox'
                    AND event.event_id=? AND effect.lane=?''', (message_id, run_id)).fetchone()
                if not source:
                    raise ValueError('native mail review requires its original durable event')
                self.connection.execute('INSERT INTO mail_review VALUES (?,?,?,?)',
                                        (run_id, message_id, body, stamp))
                self.connection.execute('''UPDATE effect SET status='delivered',error='',updated_at=?
                    WHERE key=(SELECT effect_key FROM mail_obligation WHERE run_id=? AND message_id=?)
                    AND status='pending' AND claim_token IS NULL''', (stamp, run_id, message_id))
        return {'run_id': run_id, 'reviewed': [review['message_id'] for review in reviews],
                'native_mutations': False}

    def supersede_reviewed(self, claim, now=None):
        stamp = timestamp(now)
        with transaction(self.connection):
            row = self._owned(claim)
            attempt = self.claim_record(claim)
            if (not row or row['generation'] != claim.generation or row['status'] != 'running'
                    or not attempt or attempt['receipt'] is not None):
                return False
            event = self.connection.execute('SELECT source,event_id,content FROM event WHERE effect_key=?',
                                            (claim.key,)).fetchone()
            if not event or event['source'] not in ('orca-inbox', 'mail-obligation'):
                return False
            messages = ([event['event_id']] if event['source'] == 'orca-inbox'
                        else json.loads(event['content'])['messages'])
            reviews = [self.connection.execute('SELECT body FROM mail_review WHERE run_id=? AND message_id=?',
                                               (claim.lane, message)).fetchone() for message in messages]
            if not messages or any(review is None for review in reviews):
                return False
            receipt = encoded({'source': 'native_mail_review', 'delivery_sent': False,
                               'message_ids': messages, 'review_sha256': {
                                   message: hashlib.sha256(review['body'].encode()).hexdigest()
                                   for message, review in zip(messages, reviews)}})
            self._record_finish(claim, row, 'delivered', '', receipt, stamp, stamp, False)
        return True

    def claim_event(self, claim):
        row = self.connection.execute('SELECT * FROM event WHERE effect_key=?', (claim.key,)).fetchone()
        return {**dict(row), 'content': json.loads(row['content'])} if row else None

    def prepare_send(self, claim, now=None):
        stamp = timestamp(now)
        with transaction(self.connection):
            row = self._owned(claim)
            if not row:
                return False
            attempt = self.claim_record(claim)
            if row['status'] != 'running' or attempt['receipt'] is not None:
                return False
            if row['generation'] != claim.generation or row['payload'] != claim.payload_json:
                self._record_finish(claim, row, 'pending', '', None, stamp, stamp, False)
                return False
        return True

    def supersede_continuation(self, claim, inventory, dispositions, now=None):
        stamp = timestamp(now)
        validate_dispositions(dispositions)
        with transaction(self.connection):
            row = self._owned(claim)
            attempt = self.claim_record(claim)
            if (not row or row['generation'] != claim.generation or row['status'] != 'running'
                    or not attempt or attempt['receipt'] is not None):
                return False
            uncertain = self.connection.execute('''SELECT 1 FROM attempt WHERE effect_key=? AND token!=?
                AND (receipt IS NOT NULL OR outcome!='pending') LIMIT 1''', (claim.key, claim.token)).fetchone()
            if uncertain:
                return False
            published = self.current_dispositions().get(claim.lane)
            decision = published['body'] if published else dispositions.get(claim.lane, {})
            proof = continuation.proof(claim, self.claim_event(claim), inventory, decision, stamp)
            if proof is None:
                return False
            if published:
                proof = {**proof, 'disposition_revision': published['revision'],
                         'disposition_owner': published['owner']}
            self._record_finish(claim, row, 'delivered', '', encoded(proof), stamp, stamp, False)
        return True

    def publish_disposition(self, run_id, body, expected_revision, owner, now=None):
        identifier(run_id)
        validate_dispositions({run_id: body})
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision < 0):
            raise ValueError('disposition revision must be a nonnegative integer')
        text, who, stamp = encoded(body), encoded(owner), timestamp(now)
        with transaction(self.connection):
            revision = self.connection.execute('SELECT COALESCE(MAX(revision),0) FROM disposition WHERE run_id=?',
                                               (run_id,)).fetchone()[0]
            if revision != expected_revision:
                raise ValueError('disposition revision changed; read the current decision before publishing')
            self.connection.execute('INSERT INTO disposition VALUES (?,?,?,?,?)',
                                    (run_id, revision + 1, text, who, stamp))
        return {'run_id': run_id, 'revision': revision + 1, 'body': body, 'owner': owner, 'published_at': stamp}

    def replace(self, key, payload, now=None):
        identifier(key)
        stamp, work = timestamp(now), encoded(payload)
        with transaction(self.connection):
            row = self.connection.execute('SELECT payload FROM effect WHERE key=?', (key,)).fetchone()
            if not row:
                raise ValueError('effect does not exist')
            if row['payload'] == work:
                return False
            self.connection.execute('''UPDATE effect SET payload=?,generation=generation+1,
                status=CASE WHEN claim_token IS NOT NULL OR status='attention' THEN status ELSE 'pending' END,
                failures=0,due_at=?,updated_at=? WHERE key=?''', (work, stamp, stamp, key))
        return True

    def claim(self, owner, now=None, excluded_lanes=()):
        identifier(owner)
        for lane in excluded_lanes:
            identifier(lane)
        exclude = ' AND current.lane NOT IN (' + ','.join('?' for _ in excluded_lanes) + ')' if excluded_lanes else ''
        stamp = timestamp(now)
        with transaction(self.connection):
            row = self.connection.execute('''SELECT * FROM effect AS current
                WHERE current.status='pending' AND current.due_at<=?
                AND NOT EXISTS (SELECT 1 FROM effect AS earlier WHERE earlier.lane=current.lane
                    AND earlier.seq<current.seq AND earlier.status!='delivered')'''
                    + exclude + ' ORDER BY current.seq LIMIT 1', (stamp, *excluded_lanes)).fetchone()
            if not row:
                return None
            token = str(uuid4())
            self.connection.execute("UPDATE effect SET status='running',claim_token=?,updated_at=? WHERE key=?",
                                    (token, stamp, row['key']))
            self.connection.execute('''INSERT INTO attempt
                (token,effect_key,generation,owner,payload,outcome,started_at) VALUES (?,?,?,?,?,'running',?)''',
                                    (token, row['key'], row['generation'], owner, row['payload'], stamp))
            return Claim(row['key'], row['kind'], row['lane'], row['generation'], token, owner, row['payload'])

    def complete(self, claim, receipt, now=None):
        stamp = timestamp(now)
        return self._finish(claim, 'delivered', '', encoded(receipt), stamp, stamp, False)

    def retry(self, claim, error, now=None):
        stamp, message = timestamp(now), failure_text(error)
        with transaction(self.connection):
            row = self._owned(claim)
            if not row:
                raise ValueError('effect claim is no longer current')
            delay = min(MAX_DELAY, INITIAL_DELAY * 2 ** min(row['failures'], 16))
            due = stamp + delay
            self._record_finish(claim, row, 'pending', message, None, due, stamp, True)
        return due

    def defer(self, claim, now=None):
        stamp = timestamp(now)
        return self._finish(claim, 'pending', '', None, stamp + INITIAL_DELAY, stamp, False)

    def attention(self, claim, error, now=None):
        stamp = timestamp(now)
        return self._finish(claim, 'attention', failure_text(error), None, stamp, stamp, False)

    def uncertain(self, claim, error, now=None):
        stamp, message = timestamp(now), failure_text(error)
        with transaction(self.connection):
            if not self._owned(claim):
                return False
            self.connection.execute("UPDATE effect SET status='reconciling',error=?,updated_at=? WHERE key=?",
                                    (message, stamp, claim.key))
            self.connection.execute("UPDATE attempt SET outcome='uncertain',error=? WHERE token=?",
                                    (message, claim.token))
        return True

    def reconcile_running(self):
        with transaction(self.connection):
            result = self.connection.execute("UPDATE effect SET status='reconciling' WHERE status='running'")
        return result.rowcount

    def record_receipt(self, claim, receipt):
        body = encoded(receipt)
        with transaction(self.connection):
            if not self._owned(claim):
                return False
            self.connection.execute('UPDATE attempt SET receipt=? WHERE token=?', (body, claim.token))
        return True

    def claim_record(self, claim):
        row = self.connection.execute('''SELECT effect.status,attempt.receipt,attempt.started_at
            FROM effect JOIN attempt ON attempt.token=effect.claim_token
            WHERE effect.key=? AND effect.claim_token=? AND attempt.generation=? AND attempt.owner=?
            AND attempt.finished_at IS NULL''',
            (claim.key, claim.token, claim.generation, claim.owner)).fetchone()
        return {**dict(row), 'receipt': json.loads(row['receipt']) if row['receipt'] else None} if row else None

    def reconciliation_claims(self, limit=None):
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError('reconciliation limit must be a positive integer')
        rows = self.connection.execute('''SELECT effect.key,effect.kind,effect.lane,attempt.*
            FROM effect JOIN attempt ON attempt.token=effect.claim_token
            WHERE effect.status='reconciling' AND attempt.finished_at IS NULL ORDER BY effect.seq'''
            + (' LIMIT ?' if limit is not None else ''), (limit,) if limit is not None else ())
        return [Claim(row['key'], row['kind'], row['lane'], row['generation'],
                      row['token'], row['owner'], row['payload']) for row in rows]

    def _owned(self, claim):
        return self.connection.execute('''SELECT effect.* FROM effect JOIN attempt ON attempt.token=effect.claim_token
            WHERE effect.key=? AND effect.claim_token=? AND attempt.generation=? AND attempt.owner=?
            AND attempt.finished_at IS NULL''', (claim.key, claim.token, claim.generation, claim.owner)).fetchone()

    def _finish(self, claim, status, error, receipt, due, stamp, failed):
        with transaction(self.connection):
            row = self._owned(claim)
            if not row:
                return False
            self._record_finish(claim, row, status, error, receipt, due, stamp, failed)
        return row['generation'] == claim.generation

    def _record_finish(self, claim, row, status, error, receipt, due, stamp, failed):
        self.connection.execute('UPDATE attempt SET outcome=?,error=?,receipt=?,finished_at=? WHERE token=?',
                                (status, error, receipt, stamp, claim.token))
        current = row['generation'] == claim.generation
        next_status = status if current or status == 'attention' else 'pending'
        self.connection.execute('''UPDATE effect SET status=?,error=?,failures=?,due_at=?,
            claim_token=NULL,updated_at=? WHERE key=?''',
                                (next_status, error if current or status == 'attention' else '',
                                 row['failures'] + int(failed) if current else 0,
                                 due if current else stamp, stamp, claim.key))

    def effects(self):
        return [{**dict(row), 'payload': json.loads(row['payload'])}
                for row in self.connection.execute('SELECT * FROM effect ORDER BY seq')]

    def attempts(self, key):
        identifier(key)
        return [{**dict(row), 'receipt': json.loads(row['receipt']) if row['receipt'] else None}
                for row in self.connection.execute('SELECT * FROM attempt WHERE effect_key=? ORDER BY started_at,rowid', (key,))]

    def summary(self):
        counts = dict(self.connection.execute('SELECT status,COUNT(*) FROM effect GROUP BY status').fetchall())
        return {'events': self.connection.execute('SELECT COUNT(*) FROM event').fetchone()[0],
                'effects': {status: counts.get(status, 0) for status in STATUSES}}
