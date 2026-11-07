"""Installed process flows preserve inputs, native requests and review obligations."""
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import time

from superagent_flow_support import InstalledFlow, TARGET


class FaultFlowTest(InstalledFlow):
    def test_working_coordinator_receives_input_after_turn_end_and_reviews_exact_question(self):
        gates = self.state()['gates']
        self.change(agent_state='working', messages=[self.question()])
        first = self.tick()
        self.assertEqual(first['queue']['effects']['pending'], 1)
        self.assertEqual(self.state()['submissions'], 0)
        self.end_turn()
        time.sleep(2.05)
        second = self.tick()
        self.assertEqual(second['queue']['effects']['delivered'], 1)
        self.assertEqual(self.state()['submissions'], 1)
        checked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1')
        replied = self.native('orchestration', 'reply', '--id', 'question_1', '--body', 'READ_ONLY_FIXTURE')
        review = self.review(checked, [{'message_id': 'question_1', 'action': 'answered',
                                      'request_id': replied['mutation']['requestId']}])
        self.assertEqual(review['reviewed'], ['question_1'])
        self.end_turn()
        final = self.tick()
        self.assertEqual(final['queue']['events'], 1)
        self.assertEqual(self.state()['submissions'], 1)
        self.assertEqual(self.report('status')['mail_obligations']['run_1'], [])
        self.assertEqual(self.state()['gates'], gates)

    def test_receipt_only_input_survives_observation_failure_then_settles_without_resubmission(self):
        self.change(messages=[self.question()], faults={'submit_stage': 'input_accepted', 'observe_after_send': True})
        first = self.tick()
        self.assertEqual(first['queue']['effects']['reconciling'], 1)
        original = self.sql('SELECT receipt FROM attempt')[0]['receipt']
        self.assertIn('native status unavailable verbatim', first['deliveries'][0]['observation_error'])
        self.change(faults={})
        self.end_turn()
        second = self.tick()
        self.assertEqual(second['queue']['effects']['delivered'], 1)
        self.assertEqual(self.state()['submissions'], 1)
        sends = [call for call in self.state()['calls'] if call[:2] == ['terminal', 'send']]
        self.assertEqual(len(sends), 1)
        receipt = json.loads(self.sql('SELECT receipt FROM attempt')[0]['receipt'])
        self.assertEqual(receipt['result'], json.loads(original)['result'])
        self.assertEqual(receipt['delivery_evidence']['request_id'], 'prompt_1')
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 1)

    def test_lost_output_preserves_uncertainty_and_blocks_later_input_after_restart(self):
        self.change(messages=[self.question()], faults={'lost_output': True})
        first = self.tick()
        self.assertEqual(first['queue']['effects']['reconciling'], 1)
        self.assertIn('native accepted input but response output was lost', first['deliveries'][0]['reason'])
        self.change(messages=[self.question(), self.question('question_2')], faults={})
        self.end_turn()
        second = self.tick()
        self.assertEqual(second['queue']['effects']['reconciling'], 1)
        self.assertEqual(second['queue']['effects']['pending'], 1)
        self.assertEqual(self.state()['submissions'], 1)
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 2)

    def test_source_page_write_failure_preserves_cursor_and_original_input_for_next_process(self):
        source = self.root / 'intake.sqlite3'
        with closing(sqlite3.connect(source)) as db, db:
            db.execute('CREATE TABLE todos (id TEXT PRIMARY KEY,message_id TEXT,state TEXT,kind TEXT,title TEXT,summary TEXT,accepted_by TEXT,updated_at TEXT)')
            db.execute("INSERT INTO todos VALUES ('todo_1','message_1','queued','bug','Fixture','Review only',NULL,'2026-11-03')")
        source.chmod(0o600)
        self.write(self.config, {'schema_version': 1, 'coordinators': [TARGET],
                                'intake': [{'run_id': 'run_1', 'path': str(source)}]})
        original = source.read_bytes()
        self.report('status')
        self.sql("CREATE TRIGGER reject_event BEFORE INSERT ON event BEGIN SELECT RAISE(ABORT,'source page storage rejected'); END")
        first = self.tick(expected=1)
        self.assertIn('source page storage rejected', str(first['issues']))
        self.assertEqual(first['queue']['events'], 0)
        self.assertEqual(self.sql("SELECT * FROM source_cursor WHERE source_key LIKE 'intake-%'"), [])
        self.sql('DROP TRIGGER reject_event')
        second = self.tick()
        self.assertEqual(second['queue']['events'], 1)
        self.assertEqual(second['queue']['effects']['delivered'], 1)
        self.assertEqual(source.read_bytes(), original)

    def test_incarnation_change_preserves_original_target_and_refuses_new_input(self):
        event = {'source': 'orca-inbox', 'event_id': 'question_1', 'content': self.question()}
        self.report('enqueue', '--run', 'run_1', value=event)
        self.change(incarnation='replacement_process')
        report = self.tick(expected=1)
        self.assertEqual(self.state()['submissions'], 0)
        self.assertEqual(report['queue']['effects']['attention'], 1)
        self.assertIn('incarnation changed', str(report))
        status = self.report('status', expected=1)
        self.assertEqual(status['effects'][0]['payload']['incarnation_id'], 'process_1')
        self.assertEqual(len(status['mail_obligations']['run_1']), 1)

    def test_live_job_does_not_resume_but_new_question_and_completion_keep_fifo_review(self):
        self.write(self.dispositions, {'run_1': {'state': 'waiting_job', 'job_ids': ['job_1']}})
        worker = {'runId': 'run_1', 'dispatchId': 'dispatch_1', 'dispatchStatus': 'dispatched',
                  'terminalState': 'active', 'projection': {'outcome': 'in_progress',
                                                           'liveness': {'verdict': 'unverifiable'}}}
        self.change(workers=[worker])
        self.tick()
        self.assertEqual(self.state()['submissions'], 0)
        self.change(messages=[self.question()])
        self.tick()
        self.assertEqual(self.state()['submissions'], 1)
        checked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1')
        reply = self.native('orchestration', 'reply', '--id', 'question_1', '--body', 'READ_ONLY_FIXTURE')
        self.review(checked, [{'message_id': 'question_1', 'action': 'answered', 'request_id': reply['mutation']['requestId']}])
        done = {'id': 'completion_1', 'type': 'worker_done', 'body': 'Fixture completion',
                'payload': json.dumps({'dispatchId': 'dispatch_1', 'taskId': 'task_1', 'outcome': 'succeeded'})}
        self.change(workers=[], messages=[done], dispatches=[{'id': 'dispatch_1', 'runId': 'run_1',
                                                           'taskId': 'task_1', 'status': 'completed'}])
        self.end_turn()
        self.tick()
        checked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1')
        released = self.native('orchestration', 'worker-release', '--dispatch', 'dispatch_1')
        self.review(checked, [{'message_id': 'completion_1', 'action': 'released', 'request_id': released['mutation']['requestId']}])
        self.end_turn()
        final = self.tick()
        self.assertEqual(final['queue']['events'], 2)
        self.assertEqual(self.state()['submissions'], 2)
        self.assertEqual(self.report('status')['mail_obligations']['run_1'], [])

    def test_early_turn_end_gets_one_mail_followup_until_receipt_backed_review(self):
        self.change(messages=[self.question()])
        self.tick()
        self.assertEqual(self.state()['submissions'], 1)
        self.end_turn()
        followup = self.tick()
        self.assertEqual(followup['queued'], 1)
        self.assertEqual(self.state()['submissions'], 2)
        checked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1')
        replied = self.native('orchestration', 'reply', '--id', 'question_1', '--body', 'READ_ONLY_FIXTURE')
        self.review(checked, [{'message_id': 'question_1', 'action': 'answered',
                              'request_id': replied['mutation']['requestId']}])
        self.end_turn()
        final = self.tick()
        self.assertEqual(final['queue']['events'], 2)
        self.assertEqual(self.state()['submissions'], 2)
        self.assertEqual(self.report('status')['mail_obligations']['run_1'], [])

    def test_failed_review_publication_reuses_original_native_receipts_after_inbox_ack(self):
        self.change(messages=[self.question()])
        self.tick()
        checked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1')
        replied = self.native('orchestration', 'reply', '--id', 'question_1', '--body', 'READ_ONLY_FIXTURE')
        acked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1',
                            '--ack', checked['deliveryId'])
        value = {'check_request_id': checked['mutation']['requestId'],
                 'ack_request_id': acked['mutation']['requestId'], 'reviews': [{'message_id': 'question_1',
                 'action': 'answered', 'request_id': replied['mutation']['requestId']}]}
        self.sql("CREATE TRIGGER reject_review BEFORE INSERT ON mail_review BEGIN SELECT RAISE(ABORT,'review storage rejected verbatim'); END")
        failed = self.run_cli('mail-reviewed', '--run', 'run_1', value=value, owner=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn('review storage rejected verbatim', failed.stderr)
        self.assertEqual(self.state()['messages'], [])
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 1)
        self.sql('DROP TRIGGER reject_review')
        before = self.state()['mutations']
        self.report('mail-reviewed', '--run', 'run_1', value=value, owner=True)
        self.assertEqual(self.state()['mutations'], before)
        self.assertEqual(self.report('status')['mail_obligations']['run_1'], [])
        self.end_turn()
        self.tick()
        self.assertEqual(self.state()['submissions'], 1)

    def test_one_mail_delivery_requires_review_of_all_questions_before_publication(self):
        self.change(messages=[self.question(), self.question('question_2')])
        self.tick()
        checked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1')
        first = self.native('orchestration', 'reply', '--id', 'question_1', '--body', 'READ_ONLY_FIXTURE')
        second = self.native('orchestration', 'reply', '--id', 'question_2', '--body', 'READ_ONLY_FIXTURE')
        acked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1',
                            '--ack', checked['deliveryId'])
        reviews = [{'message_id': 'question_1', 'action': 'answered', 'request_id': first['mutation']['requestId']},
                   {'message_id': 'question_2', 'action': 'answered', 'request_id': second['mutation']['requestId']}]
        value = {'check_request_id': checked['mutation']['requestId'],
                 'ack_request_id': acked['mutation']['requestId'], 'reviews': reviews[:1]}
        failed = self.run_cli('mail-reviewed', '--run', 'run_1', value=value, owner=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn('whole native Delivery exactly once', failed.stderr)
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 2)
        self.report('mail-reviewed', '--run', 'run_1', value={**value, 'reviews': reviews}, owner=True)
        self.assertEqual(self.report('status')['mail_obligations']['run_1'], [])
        self.end_turn()
        self.tick()
        self.assertEqual(self.state()['submissions'], 1)

    def test_observer_sigkill_after_acceptance_keeps_running_claim_and_never_resends(self):
        gate = self.root / 'release-native-response'
        started = gate.with_suffix('.started')
        self.change(messages=[self.question()], faults={'block_after_send': str(gate)})
        with subprocess.Popen(self.command('tick', '--apply'), env=self.environment(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            deadline = time.monotonic() + 10
            while not started.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            if not started.exists():
                process.kill()
                stdout, stderr = process.communicate(timeout=10)
                self.fail(f'fixture did not reach durable accepted input\n{stdout}{stderr}')
            native_pid = int(started.read_text())
            os.kill(native_pid, 0)
            self.assertEqual(self.state()['submissions'], 1)
            self.assertEqual(self.sql('SELECT status FROM effect')[0]['status'], 'running')
            process.kill()
            gate.write_text('release exact native fixture response')
            process.communicate(timeout=10)
            self.assertEqual(process.returncode, -9)
        self.change(faults={})
        self.end_turn()
        second = self.tick()
        self.assertEqual(second['queue']['effects']['reconciling'], 1)
        self.assertEqual(self.state()['submissions'], 1)
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 1)

    def test_input_enqueue_and_owner_disposition_stay_available_during_native_send(self):
        gate = self.root / 'release-native-response'
        started = gate.with_suffix('.started')
        self.change(messages=[self.question()], faults={'block_after_send': str(gate)})
        with subprocess.Popen(self.command('tick', '--apply'), env=self.environment(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                deadline = time.monotonic() + 10
                while not started.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(started.exists())
                os.kill(int(started.read_text()), 0)
                event = {'source': 'orca-inbox', 'event_id': 'question_2', 'content': self.question('question_2')}
                result = self.report('enqueue', '--run', 'run_1', value=event)
                self.assertEqual(result['queue']['events'], 2)
                published = self.report('disposition', '--run', 'run_1', '--expect-revision', '0',
                    value={'state': 'waiting_job', 'job_ids': ['job_1']}, owner=True)
                self.assertEqual(published['published']['revision'], 1)
                self.assertEqual(self.report('status')['queue']['effects']['pending'], 1)
            finally:
                gate.write_text('release exact native fixture response')
                stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.change(faults={})
        self.end_turn()
        second = self.tick()
        self.assertEqual(second['queue']['effects']['delivered'], 2)
        self.assertEqual(self.state()['submissions'], 2)
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 2)

    def test_native_review_cannot_settle_same_id_with_changed_original_question(self):
        self.change(messages=[self.question()])
        self.tick()
        original = self.sql('SELECT content FROM event')[0]['content']
        self.change(messages=[{**self.question(), 'body': 'Different authorization question'}])
        checked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1')
        replied = self.native('orchestration', 'reply', '--id', 'question_1', '--body', 'READ_ONLY_FIXTURE')
        acked = self.native('orchestration', 'check', '--run', 'run_1', '--terminal', 'term_1',
                            '--ack', checked['deliveryId'])
        value = {'check_request_id': checked['mutation']['requestId'], 'ack_request_id': acked['mutation']['requestId'],
                 'reviews': [{'message_id': 'question_1', 'action': 'answered', 'request_id': replied['mutation']['requestId']}]}
        failed = self.run_cli('mail-reviewed', '--run', 'run_1', value=value, owner=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn('original durable native message', failed.stderr)
        self.assertEqual(self.sql('SELECT content FROM event')[0]['content'], original)
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 1)

    def test_existing_mail_followup_respects_provider_cooldown_until_due(self):
        self.change(messages=[self.question()])
        self.tick()
        retry = time.time() + 3600
        self.report('disposition', '--run', 'run_1', '--expect-revision', '0',
                    value={'state': 'waiting_provider', 'retry_at': retry}, owner=True)
        self.end_turn()
        for _ in range(3):
            report = self.tick()
            self.assertEqual(report['queued'], 0)
        self.assertEqual(self.state()['submissions'], 1)
        self.assertEqual(len(self.report('status')['mail_obligations']['run_1']), 1)
        self.report('disposition', '--run', 'run_1', '--expect-revision', '1',
                    value={'state': 'waiting_provider', 'retry_at': time.time() - 1}, owner=True)
        report = self.tick()
        self.assertEqual(report['queued'], 1)
        self.assertEqual(self.state()['submissions'], 2)
