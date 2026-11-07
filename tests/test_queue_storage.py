import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from app.database.store import AdmissionError, Store
from app.runtime import Runtime
from app.services.job_queue import JobQueue
from app.services.prompts import PROMPT_VERSION
from helpers import FakeAI, FakeTelegram, job_done, test_settings, wait_until
from aiogram import Bot


class QueueStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.settings = test_settings(self.root, queue_size=1, daily_limit=2)
        self.db = Store(self.settings.database_path); await self.db.open()
        self.ai = FakeAI(); self.queue = JobQueue(self.db, self.ai, self.settings)

    async def asyncTearDown(self):
        await self.queue.close(); await self.db.close(); self.tmp.cleanup()

    async def lecture(self, owner=1, digest='hash', name='lecture.txt'):
        directory = self.settings.upload_dir / str(owner); directory.mkdir(parents=True, exist_ok=True)
        p = directory / f'{uuid4()}.txt'; p.write_text('Лекция')
        return await self.db.add_lecture(owner, name, p, digest, p.stat().st_size, 30, 20, 100000)

    async def test_simultaneous_duplicate_clicks_single_job(self):
        row = await self.lecture()
        results = await asyncio.gather(*(self.queue.submit(1, row['id'], 'outline', 1) for _ in range(10)))
        self.assertEqual([r.kind for r in results].count('queued'), 1)
        self.assertEqual(len(self.queue.pending), 1)
        await self.queue.start(); await wait_until(lambda: job_done(self.db, 1, results[0].record['id']))
        self.assertEqual(len(self.ai.calls), 1)

    async def test_full_queue_rejection_has_no_job_or_slot_and_cancel_frees_capacity(self):
        first = await self.lecture(1); second = await self.lecture(2)
        one = await self.queue.submit(1, first['id'], 'outline', 1)
        with self.assertRaises(AdmissionError) as caught:
            await self.queue.submit(2, second['id'], 'outline', 2)
        self.assertEqual(caught.exception.category, 'queue_full')
        self.assertIsNone(await self.db.active_job(2))
        self.assertEqual((await self.db._one('SELECT COUNT(*) n FROM jobs'))['n'], 1)
        self.assertFalse(await self.queue.cancel(2, one.record['id']))
        self.assertTrue(await self.queue.cancel(1, one.record['id']))
        self.assertEqual(len(self.queue.pending), 0)
        two = await self.queue.submit(2, second['id'], 'short', 2)
        self.assertEqual(two.kind, 'queued')

    async def test_workers_limit_and_one_active_per_user(self):
        self.ai.gate = asyncio.Event(); await self.queue.start()
        one = await self.lecture(1); two = await self.lecture(2)
        a = await self.queue.submit(1, one['id'], 'outline', 1); await self.ai.started.wait()
        await self.queue.submit(2, two['id'], 'short', 2)
        again = await self.queue.submit(1, one['id'], 'short', 1)
        self.assertEqual(again.kind, 'active'); self.assertEqual(len(self.ai.calls), 1)
        self.ai.gate.set(); await wait_until(lambda: job_done(self.db, 1, a.record['id']))

    async def test_queue_wait_timeout_and_execution_timeout_release_admission(self):
        self.queue.settings = replace(self.settings, queue_timeout=.04, execution_timeout=.15)
        self.ai.gate = asyncio.Event(); await self.queue.start()
        one = await self.lecture(1); two = await self.lecture(2)
        a = await self.queue.submit(1, one['id'], 'outline', 1); await self.ai.started.wait()
        b = await self.queue.submit(2, two['id'], 'short', 2)
        await wait_until(lambda: job_done(self.db, 2, b.record['id']))
        self.assertEqual((await self.db.job(2, b.record['id']))['error_category'], 'queue_timeout')
        self.assertEqual(len(self.queue.pending), 0)
        await wait_until(lambda: job_done(self.db, 1, a.record['id']))
        self.assertEqual((await self.db.job(1, a.record['id']))['error_category'], 'execution_timeout')
        self.assertIsNone(await self.db.active_job(1))

    async def test_cache_scope_model_prompt_and_daily_limit(self):
        self.queue.settings = replace(self.settings, daily_limit=1)
        one = await self.lecture(1); same = await self.lecture(1); other = await self.lecture(2)
        await self.queue.start()
        a = await self.queue.submit(1, one['id'], 'outline', 1); await wait_until(lambda: job_done(self.db, 1, a.record['id']))
        cached = await self.queue.submit(1, same['id'], 'outline', 1)
        self.assertEqual(cached.kind, 'cached'); self.assertEqual(len(self.ai.calls), 1)
        self.assertIsNone(await self.db.result(2, cached.record['id']))
        with self.assertRaises(AdmissionError) as caught: await self.queue.submit(1, one['id'], 'short', 1)
        self.assertEqual(caught.exception.category, 'daily_limit')
        self.assertIsNone(await self.db.cached(1, 'hash', 'outline', 'new-model', PROMPT_VERSION))
        self.assertIsNone(await self.db.cached(1, 'hash', 'outline', self.settings.model, 'new-version'))
        b = await self.queue.submit(2, other['id'], 'outline', 2)
        self.assertEqual(b.kind, 'queued')
        await wait_until(lambda: job_done(self.db, 2, b.record['id']))
        self.assertEqual(len(self.ai.calls), 2)

    async def test_restart_preserves_results_bindings_and_interrupts_jobs(self):
        row = await self.lecture(); await self.db.bind(1, 1, 10, row['id'])
        ready = await self.db.admit(1, row['id'], 'short', self.settings.model, PROMPT_VERSION, 1, 10)
        await self.db.finish(ready, 'Готовый результат', {'total_tokens': 10})
        pending = await self.db.admit(1, row['id'], 'outline', self.settings.model, PROMPT_VERSION, 1, 10)
        await self.db.close(); self.db = Store(self.settings.database_path); await self.db.open(); self.queue.store = self.db
        self.assertEqual((await self.db.job(1, pending['id']))['status'], 'interrupted')
        self.assertEqual((await self.db.bound_lecture(1, 1, 10))['id'], row['id'])
        self.assertEqual((await self.db.cached(1, 'hash', 'short', self.settings.model, PROMPT_VERSION))['content'], 'Готовый результат')
        self.assertEqual(self.ai.calls, [])

    async def test_configured_two_workers_run_only_two_requests(self):
        self.queue.settings = replace(self.settings, workers=2, queue_size=3)
        self.ai.gate = asyncio.Event()
        await self.queue.start()
        rows = [await self.lecture(i) for i in (1, 2, 3)]
        for row in rows:
            await self.queue.submit(row['owner'], row['id'], 'outline', row['owner'])
        async def two_started(): return len(self.ai.calls) == 2
        await wait_until(two_started)
        await asyncio.sleep(.02)
        self.assertEqual(len(self.ai.calls), 2)
        self.assertEqual(len(self.queue.pending), 1)
        self.ai.gate.set()

    async def test_shutdown_interrupts_running_and_pending_without_retry(self):
        self.ai.gate = asyncio.Event(); await self.queue.start()
        arow = await self.lecture(1); brow = await self.lecture(2)
        a = await self.queue.submit(1, arow['id'], 'outline', 1); await self.ai.started.wait()
        b = await self.queue.submit(2, brow['id'], 'short', 2)
        await self.queue.close()
        self.assertEqual((await self.db.job(1, a.record['id']))['status'], 'interrupted')
        self.assertEqual((await self.db.job(2, b.record['id']))['status'], 'interrupted')
        self.assertEqual(self.queue.tasks, [])

    async def test_delivery_failure_keeps_result_and_does_not_repeat_generation(self):
        async def broken_delivery(*args, **kwargs):
            raise RuntimeError('Fake Telegram outage')
        self.queue.on_event = broken_delivery
        row = await self.lecture()
        await self.queue.start()
        submitted = await self.queue.submit(1, row['id'], 'outline', 1)
        await wait_until(lambda: job_done(self.db, 1, submitted.record['id']))
        cached = await self.queue.submit(1, row['id'], 'outline', 1)
        self.assertEqual(cached.kind, 'cached')
        self.assertEqual(len(self.ai.calls), 1)

    async def test_owner_delete_and_active_file_protection(self):
        row = await self.lecture(1)
        self.assertEqual(await self.db.remove_lectures(2, row['id']), [])
        submitted = await self.queue.submit(1, row['id'], 'outline', 1)
        with self.assertRaises(AdmissionError): await self.db.remove_lectures(1)
        self.assertTrue(await self.queue.cancel(1, submitted.record['id']))
        removed = await self.db.remove_lectures(1, row['id'])
        self.assertEqual(len(removed), 1); self.assertIsNone(await self.db.lecture(1, row['id']))
