import asyncio
import base64
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import PROJECT_ROOT, Settings
from app.database.store import Store
from app.main import single_instance
from app.runtime import Runtime
from helpers import FakeAI, test_settings
from unittest.mock import AsyncMock


class Configuration(unittest.TestCase):
    def test_valid_settings_and_safe_validation_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            base = replace(test_settings(Path(directory)),credentials=base64.b64encode(b'client:secret').decode())
            base.validate()
            for overrides in ({'bot_token':'bad'}, {'credentials':''}, {'credentials':'secret-not-base64'},
                              {'queue_size':0}, {'max_upload_bytes':21*1024*1024}, {'scope':'bad'},
                              {'ca_file':Path(directory)/'missing.pem'}, {'proxy_url':'socks5://broken'}):
                with self.assertRaises(ValueError) as error:
                    replace(base,**overrides).validate()
                self.assertNotIn('client:secret',str(error.exception))
                self.assertNotIn(base.credentials,str(error.exception))
            replace(base,ai_enabled=False,credentials='',ca_file=Path('/not-present')).validate()

    def test_paths_and_invalid_environment(self):
        with patch.dict(os.environ,{'UPLOAD_DIR':'relative-files','DATABASE_PATH':'relative-db/bot.sqlite3','AI_ENABLED':'false'}):
            settings=Settings.from_env()
            self.assertEqual(settings.upload_dir,PROJECT_ROOT/'relative-files')
            self.assertEqual(settings.database_path,PROJECT_ROOT/'relative-db/bot.sqlite3')
        with patch.dict(os.environ,{'QUEUE_SIZE':'not-a-number'}):
            with self.assertRaisesRegex(ValueError,'QUEUE_SIZE'): Settings.from_env()
        with patch.dict(os.environ,{'AI_ENABLED':'typo'}):
            with self.assertRaisesRegex(ValueError,'AI_ENABLED'): Settings.from_env()

    def test_single_instance_rejects_second_process_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bot.sqlite3'
            with single_instance(path):
                with self.assertRaisesRegex(ValueError,'уже запущен'):
                    with single_instance(path): pass


class ConfigurationAsync(unittest.IsolatedAsyncioTestCase):
    async def test_newer_schema_is_rejected_without_leaking_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db';db=Store(path);await db.open()
            await db.connection.execute('PRAGMA user_version=999');await db.close()
            with self.assertRaises(ValueError):await db.open()
            self.assertIsNone(db.connection)

    async def test_ai_disabled_needs_no_ca_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=test_settings(Path(directory),ai_enabled=False,credentials='',ca_file=Path('/not-present'))
            runtime=Runtime(settings,AsyncMock())
            await runtime.open()
            try:
                self.assertIsNone(runtime.client)
                self.assertEqual(runtime.queue.tasks[0].get_name(),'study-worker-0')
            finally:await runtime.close()
