"""Native mail remains an obligation after transport delivery until receipt-backed review."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import driver, mail_obligation
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue only approved work.'}
CALLER = {'ORCA_TERMINAL_HANDLE': 'term_1', 'ORCA_PANE_KEY': 'pane_1'}
MESSAGE = {'id': 'message_1', 'type': 'status', 'body': 'Read this existing review context.'}
EVENT = {'source': 'orca-inbox', 'event_id': 'message_1', 'content': MESSAGE}


def inventory(since=99000):
    return {'runtime': {'runtime': {'reachable': True, 'state': 'ready'}},
            'runs': [{'id': 'run_1', 'legacy': 0, 'coordinator_handle': 'term_1'}],
            'terminals': [{'handle': 'term_1', 'incarnationId': 'process_1', 'connected': True,
                           'writable': True, 'worktreeId': 'repo::/work', 'paneKey': 'pane_1'}],
            'worktrees': [{'worktreeId': 'repo::/work', 'agents': [{'paneKey': 'pane_1',
                          'state': 'done', 'stateStartedAt': since}]}], 'workers': []}


class MailObligationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private/journal.sqlite3'
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)
        driver.enqueue_context(self.journal, TARGET, EVENT, now=100)

    def receipts(self, question=False):
        message = {**MESSAGE, 'type': 'question' if question else 'status'}
        return [
            {'requestId': 'check_1', 'state': 'completed', 'method': 'orchestration.check',
             'receipt': {'runId': 'run_1', 'deliveryId': 'delivery_1', 'messages': [message]}},
            {'requestId': 'ack_1', 'state': 'completed', 'method': 'orchestration.check',
             'receipt': {'runId': 'run_1', 'acknowledged': 'delivery_1'}},
        ]

    def review(self, request=None, caller=None, responses=None):
        data = request or {'check_request_id': 'check_1', 'ack_request_id': 'ack_1',
                           'reviews': [{'message_id': 'message_1', 'action': 'reviewed'}]}
        with patch.object(mail_obligation.observe, 'query', side_effect=responses or self.receipts()):
            return mail_obligation.publish(self.journal, TARGET, data, caller or CALLER, inventory(), '/orca', now=101)

    def observe_message(self, message):
        self.journal = Journal(self.path.with_name('native-' + message['type'] + '.sqlite3'))
        self.addCleanup(self.journal.close)
        driver.enqueue_context(self.journal, TARGET, {**EVENT, 'content': message}, now=100)

    def test_transport_delivery_does_not_settle_native_mail_obligation(self):
        self.journal.complete(self.journal.claim('driver', now=100), {}, now=101)
        with Journal(self.path) as reopened:
            self.assertEqual(reopened.open_mail('run_1')[0]['message_id'], 'message_1')
            self.assertEqual(reopened.summary()['effects']['delivered'], 1)

    def test_upgrade_enrolls_only_currently_observed_unread_mail_not_historical_delivery(self):
        self.journal.complete(self.journal.claim('driver', now=100), {}, now=101)
        self.journal.connection.execute('DELETE FROM mail_obligation')
        self.assertEqual(self.journal.open_mail('run_1'), [])
        driver.enqueue_context(self.journal, TARGET, EVENT, now=102)
        self.assertEqual(len(self.journal.open_mail('run_1')), 1)
        self.assertEqual(self.journal.summary()['effects']['delivered'], 1)

    def test_receipt_backed_review_settles_without_repeating_native_mutations(self):
        self.review()
        self.assertEqual(self.journal.open_mail('run_1'), [])
        self.assertEqual(self.journal.summary()['effects']['delivered'], 1)
        self.assertIsNone(self.journal.claim('driver', now=102))
        self.review()
        self.assertEqual(self.journal.connection.execute('SELECT COUNT(*) FROM mail_review').fetchone()[0], 1)

    def test_wrong_owner_missing_ack_or_incomplete_batch_never_settles(self):
        with self.assertRaisesRegex(ValueError, 'owner'):
            self.review(caller={'ORCA_TERMINAL_HANDLE': 'foreign', 'ORCA_PANE_KEY': 'pane_1'})
        responses = self.receipts()
        responses[1] = {**responses[1], 'receipt': {'runId': 'run_1', 'acknowledged': 'other'}}
        with self.assertRaisesRegex(ValueError, 'acknowledgment'):
            self.review(responses=responses)
        data = {'check_request_id': 'check_1', 'ack_request_id': 'ack_1', 'reviews': []}
        with self.assertRaisesRegex(ValueError, 'whole native Delivery'):
            self.review(request=data)
        self.assertEqual(len(self.journal.open_mail('run_1')), 1)

    def test_same_id_with_changed_native_body_cannot_close_original_obligation(self):
        responses = self.receipts()
        responses[0] = {**responses[0], 'receipt': {**responses[0]['receipt'],
            'messages': [{**MESSAGE, 'body': 'Different reviewed work'}]}}
        with self.assertRaisesRegex(ValueError, 'original durable native message'):
            self.review(responses=responses)
        self.assertEqual(len(self.journal.open_mail('run_1')), 1)

    def test_native_read_metadata_does_not_change_original_message_identity(self):
        responses = self.receipts()
        responses[0] = {**responses[0], 'receipt': {**responses[0]['receipt'],
            'messages': [{**MESSAGE, 'read_at': '2026-11-03', 'delivery_id': 'delivery_1'}]}}
        self.review(responses=responses)
        self.assertEqual(self.journal.open_mail('run_1'), [])

    def test_malformed_native_delivery_rows_are_validation_errors_with_no_review_write(self):
        for message in (None, 'message', {}, {'id': None}):
            responses = self.receipts()
            responses[0] = {**responses[0], 'receipt': {**responses[0]['receipt'], 'messages': [message]}}
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.review(responses=responses)
            self.assertEqual(self.journal.connection.execute('SELECT COUNT(*) FROM mail_review').fetchone()[0], 0)

    def test_question_requires_actual_native_reply_receipt_not_ack_alone(self):
        responses = self.receipts(question=True)
        self.observe_message(responses[0]['receipt']['messages'][0])
        with self.assertRaisesRegex(ValueError, 'reply'):
            self.review(responses=responses)
        responses = [*responses, {'requestId': 'reply_1', 'state': 'completed', 'method': 'orchestration.reply',
                     'receipt': {'question': {'message_id': 'message_1', 'run_id': 'run_1', 'status': 'answered'},
                                 'message': {'thread_id': 'message_1', 'body': 'Read-only fixture only.'}}}]
        data = {'check_request_id': 'check_1', 'ack_request_id': 'ack_1',
                'reviews': [{'message_id': 'message_1', 'action': 'answered', 'request_id': 'reply_1'}]}
        self.review(request=data, responses=responses)
        self.assertEqual(self.journal.open_mail('run_1'), [])

    def test_completion_review_requires_exact_native_settlement_and_cleanup_receipts(self):
        message = {**MESSAGE, 'type': 'worker_done', 'payload': json.dumps({
            'taskId': 'task_1', 'dispatchId': 'dispatch_1', 'outcome': 'succeeded'})}
        self.observe_message(message)
        responses = self.receipts()
        responses[0] = {**responses[0], 'receipt': {**responses[0]['receipt'], 'messages': [message]}}
        settled = {'dispatch': {'id': 'dispatch_1', 'runId': 'run_1', 'taskId': 'task_1', 'status': 'completed'}}
        released = {'requestId': 'release_1', 'state': 'completed', 'method': 'orchestration.workerRelease',
                    'receipt': {'dispatchId': 'dispatch_1', 'state': 'released'}}
        data = {'check_request_id': 'check_1', 'ack_request_id': 'ack_1',
                'reviews': [{'message_id': 'message_1', 'action': 'released', 'request_id': 'release_1'}]}
        self.review(request=data, responses=[*responses, settled, released])
        self.assertEqual(self.journal.open_mail('run_1'), [])

    def test_completion_with_live_task_or_unconfirmed_release_cannot_settle(self):
        message = {**MESSAGE, 'type': 'worker_done', 'payload': json.dumps({
            'taskId': 'task_1', 'dispatchId': 'dispatch_1', 'outcome': 'succeeded'})}
        self.observe_message(message)
        responses = self.receipts()
        responses[0] = {**responses[0], 'receipt': {**responses[0]['receipt'], 'messages': [message]}}
        data = {'check_request_id': 'check_1', 'ack_request_id': 'ack_1',
                'reviews': [{'message_id': 'message_1', 'action': 'released', 'request_id': 'release_1'}]}
        for status in ('dispatched', 'failed'):
            row = {'dispatch': {'id': 'dispatch_1', 'runId': 'run_1', 'taskId': 'task_1', 'status': status}}
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'outcome'):
                self.review(request=data, responses=[*responses, row])
        row = {'dispatch': {'id': 'dispatch_1', 'runId': 'run_1', 'taskId': 'task_1', 'status': 'completed'}}
        receipt = {'requestId': 'release_1', 'state': 'completed', 'method': 'orchestration.workerRelease',
                   'receipt': {'dispatchId': 'dispatch_1', 'state': 'release_unknown'}}
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            self.review(request=data, responses=[*responses, row, receipt])

    def test_native_rejected_completion_never_requires_release_of_an_unsettled_task(self):
        data = {'check_request_id': 'check_1', 'ack_request_id': 'ack_1',
                'reviews': [{'message_id': 'message_1', 'action': 'rejected'}]}
        message = {**MESSAGE, 'type': 'worker_done', 'payload': json.dumps({
            '_orcaLifecycleRejection': {'code': 'stale_dispatch', 'reason': 'old attempt'}})}
        self.observe_message(message)
        responses = self.receipts()
        responses[0] = {**responses[0], 'receipt': {**responses[0]['receipt'], 'messages': [message]}}
        self.review(request=data, responses=responses)
        self.assertEqual(self.journal.open_mail('run_1'), [])

    def test_invalid_request_and_duplicate_reviews_fail_without_native_reads(self):
        for value in ({}, {'check_request_id': 'check_1', 'ack_request_id': 'ack_1', 'reviews': [
                {'message_id': 'message_1', 'action': 'guessed'}]},
                {'check_request_id': 'check_1', 'ack_request_id': 'ack_1', 'reviews': [
                    {'message_id': 'message_1', 'action': 'reviewed'},
                    {'message_id': 'message_1', 'action': 'reviewed'}]}):
            with self.subTest(value=value), patch.object(mail_obligation.observe, 'query') as read, \
                    self.assertRaises(ValueError):
                mail_obligation.publish(self.journal, TARGET, value, CALLER, inventory(), '/orca', now=101)
            read.assert_not_called()

    def test_unproven_request_or_another_delivery_cannot_be_claimed_reviewed(self):
        for state in ('pending', 'absent'):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, 'completed mutation'):
                self.review(responses=[{'requestId': 'check_1', 'state': state}])
        responses = self.receipts()
        responses[0] = {**responses[0], 'receipt': {**responses[0]['receipt'], 'runId': 'run_other'}}
        with self.assertRaisesRegex(ValueError, 'issued whole'):
            self.review(responses=responses)


    def test_idle_turn_with_unprocessed_mail_gets_one_followup_even_during_approval_wait(self):
        self.journal.complete(self.journal.claim('driver', now=100), {}, now=100)
        settings = {'coordinators': [TARGET]}
        hold = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        for _ in range(3):
            driver.discover(self.journal, settings, inventory(since=101000), hold, now=102)
        self.assertEqual(self.journal.summary()['events'], 2)
        prompt = self.journal.claim('driver', now=102).payload['text']
        self.assertIn('mail-reviewed', prompt)
        self.assertIn('message_1', prompt)
        self.assertIn('Review does not grant new execution, publication or funding permission.', prompt)

    def test_review_failure_rolls_back_all_obligations(self):
        self.journal.connection.execute('''CREATE TRIGGER fail_review BEFORE INSERT ON mail_review
            BEGIN SELECT RAISE(ABORT, 'review storage rejected'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'review storage rejected'):
            self.review()
        self.assertEqual(len(self.journal.open_mail('run_1')), 1)

    def test_completed_review_supersedes_an_already_queued_followup_without_model_wake(self):
        self.journal.complete(self.journal.claim('driver', now=100), {}, now=100)
        hold = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        driver.discover(self.journal, {'coordinators': [TARGET]}, inventory(since=101000), hold, now=102)
        self.review()
        with patch.object(driver.observe, 'collect', return_value=inventory(since=101000)), \
                patch.object(driver.observe, 'collect_workers', return_value=[]), \
                patch.object(driver, 'collect_context', return_value={'queued': 0, 'issues': []}), \
                patch.object(driver.native, 'deliver') as deliver:
            result = driver.tick(self.journal, {'coordinators': [TARGET]}, hold, '/orca', now=103)
        deliver.assert_not_called()
        self.assertEqual(result['queue']['effects']['pending'], 0)
        self.assertEqual(result['deliveries'][0]['status'], 'superseded_by_review')

    def followup(self):
        self.journal.complete(self.journal.claim('driver', now=100), {}, now=100)
        driver.discover(self.journal, {'coordinators': [TARGET]}, inventory(since=101000), {}, now=102)
        return self.journal.claim('driver', now=102)

    def test_review_during_owner_refresh_prevents_the_pending_followup_send(self):
        from superagent_queue import native
        claim = self.followup()

        def refresh(*args, **kwargs):
            self.review()
            return inventory(since=101000)

        with patch.object(native.observe, 'collect', side_effect=refresh), patch.object(native, 'send') as send:
            result = native.deliver(self.journal, claim, '/orca', now=103, snapshot=inventory(since=101000))
        send.assert_not_called()
        self.assertEqual(result['status'], 'superseded_by_review')
        self.assertEqual(self.journal.attempts(claim.key)[0]['receipt']['source'], 'native_mail_review')

    def test_reviewed_followup_does_not_hide_an_already_ambiguous_send(self):
        claim = self.followup()
        self.journal.uncertain(claim, 'response lost after send', now=102)
        self.review()
        self.assertFalse(self.journal.supersede_reviewed(claim, now=103))
        self.assertEqual(self.journal.summary()['effects']['reconciling'], 1)
        self.assertEqual(self.journal.attempts(claim.key)[0]['outcome'], 'uncertain')

    def test_followup_requires_review_of_its_original_messages_not_empty_inbox(self):
        claim = self.followup()
        self.assertFalse(self.journal.supersede_reviewed(claim, now=103))
        self.assertEqual(self.journal.summary()['effects']['running'], 1)

    def test_review_of_an_original_batch_can_cancel_it_despite_new_unprocessed_mail(self):
        claim = self.followup()
        second = {**EVENT, 'event_id': 'message_2', 'content': {**MESSAGE, 'id': 'message_2'}}
        driver.enqueue_context(self.journal, TARGET, second, now=103)
        self.review()
        self.assertTrue(self.journal.supersede_reviewed(claim, now=104))
        self.assertEqual(self.journal.open_mail('run_1')[0]['message_id'], 'message_2')
        self.assertIsNotNone(self.journal.claim('driver', now=104))

    def test_new_followup_generation_is_not_discarded_by_an_old_claim(self):
        claim = self.followup()
        self.journal.replace(claim.key, {**claim.payload, 'text': 'New current work.'}, now=103)
        self.review()
        self.assertFalse(self.journal.supersede_reviewed(claim, now=104))
        self.assertEqual(self.journal.summary()['effects']['running'], 1)

    def test_supersession_failure_rolls_back_the_effect_and_attempt(self):
        claim = self.followup()
        self.review()
        self.journal.connection.execute('''CREATE TRIGGER fail_attempt BEFORE UPDATE ON attempt
            BEGIN SELECT RAISE(ABORT, 'attempt update rejected'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'attempt update rejected'):
            self.journal.supersede_reviewed(claim, now=104)
        self.assertEqual(self.journal.summary()['effects']['running'], 1)
        self.assertEqual(self.journal.attempts(claim.key)[0]['outcome'], 'running')


if __name__ == '__main__':
    unittest.main()
