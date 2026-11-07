"""SQLite persistence and atomic admission checks for a single bot process."""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

Record = dict[str, Any]
ACTIVE = ('queued', 'running')
SCHEMA_VERSION = 1
SCHEMA = '''
CREATE TABLE IF NOT EXISTS lectures (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
 path TEXT NOT NULL UNIQUE, format TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
 created REAL NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS lecture_owner ON lectures(owner, id DESC);
CREATE TABLE IF NOT EXISTS results (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, file_hash TEXT NOT NULL,
 mode TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 content TEXT NOT NULL, usage_json TEXT, created REAL NOT NULL,
 UNIQUE(owner, file_hash, mode, model, prompt_version));
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL,
 lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
 mode TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','done','error','cancelled','interrupted')),
 created REAL NOT NULL, started REAL, finished REAL, error_category TEXT,
 chat_id INTEGER NOT NULL, result_id INTEGER REFERENCES results(id) ON DELETE SET NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(owner) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS jobs_lecture ON jobs(lecture_id,status);
CREATE TABLE IF NOT EXISTS bindings (
 owner INTEGER NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 lecture_id INTEGER NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
 PRIMARY KEY(owner,chat_id,message_id));
CREATE TABLE IF NOT EXISTS support_attempts (owner INTEGER PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS quiz_sessions (
 id INTEGER PRIMARY KEY, owner INTEGER NOT NULL,
 result_id INTEGER NOT NULL REFERENCES results(id) ON DELETE CASCADE,
 position INTEGER NOT NULL DEFAULT 0, answers TEXT NOT NULL DEFAULT '[]',
 score INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS remote_cleanup (
 file_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL);
'''


class AdmissionError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.connection: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path, isolation_level=None)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute('PRAGMA foreign_keys=ON')
        await self.connection.execute('PRAGMA journal_mode=WAL')
        await self.connection.execute('PRAGMA busy_timeout=5000')
        version = (await self._one('PRAGMA user_version'))['user_version']
        if version > SCHEMA_VERSION:
            await self.close()
            raise ValueError('DATABASE_PATH: схема новее поддерживаемой; обновите приложение')
        await self.connection.executescript(SCHEMA)
        await self.connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        # No automatic retry: a running request may already have consumed tokens.
        await self.connection.execute(
            "UPDATE jobs SET status='interrupted', finished=?, error_category='restart' WHERE status IN ('queued','running')",
            (time.time(),))

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    async def _one(self, sql: str, args: tuple = ()) -> Record | None:
        async with self.connection.execute(sql, args) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _all(self, sql: str, args: tuple = ()) -> list[Record]:
        async with self.connection.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            await self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                await self.connection.rollback()
                raise
            else:
                await self.connection.commit()

    async def add_lecture(self, owner: int, name: str, path: Path, digest: str, size: int,
                          retention_days: int, max_files: int, max_bytes: int) -> Record:
        async with self.transaction():
            totals = await self._one('SELECT COUNT(*) n, COALESCE(SUM(size),0) size FROM lectures WHERE owner=?', (owner,))
            if totals['n'] >= max_files or totals['size'] + size > max_bytes:
                raise AdmissionError('storage_limit', 'Лимит сохранённых файлов исчерпан. Удалите ненужные лекции в «Мои лекции».')
            now = time.time()
            cursor = await self.connection.execute(
                'INSERT INTO lectures(owner,name,path,format,sha256,size,created,expires) VALUES(?,?,?,?,?,?,?,?)',
                (owner, name, str(path), path.suffix.lower(), digest, size, now, now + retention_days * 86400))
            return await self._one('SELECT * FROM lectures WHERE id=?', (cursor.lastrowid,))

    async def lecture(self, owner: int, lecture_id: int) -> Record | None:
        async with self.lock:
            return await self._one('SELECT * FROM lectures WHERE owner=? AND id=?', (owner, lecture_id))

    async def lectures(self, owner: int, page: int = 0, per_page: int = 5) -> tuple[list[Record], int]:
        async with self.lock:
            count = await self._one('SELECT COUNT(*) n FROM lectures WHERE owner=?', (owner,))
            rows = await self._all('SELECT * FROM lectures WHERE owner=? ORDER BY id DESC LIMIT ? OFFSET ?',
                                   (owner, per_page, max(0, page) * per_page))
            return rows, count['n']

    async def bind(self, owner: int, chat: int, message: int, lecture: int) -> None:
        async with self.lock:
            await self.connection.execute('INSERT OR REPLACE INTO bindings VALUES(?,?,?,?)', (owner, chat, message, lecture))

    async def bound_lecture(self, owner: int, chat: int, message: int) -> Record | None:
        async with self.lock:
            return await self._one('SELECT l.* FROM lectures l JOIN bindings b ON b.lecture_id=l.id '
                                   'WHERE b.owner=? AND b.chat_id=? AND b.message_id=? AND l.owner=?',
                                   (owner, chat, message, owner))

    async def cached(self, owner: int, digest: str, mode: str, model: str, version: str) -> Record | None:
        async with self.lock:
            return await self._one('SELECT * FROM results WHERE owner=? AND file_hash=? AND mode=? AND model=? AND prompt_version=?',
                                   (owner, digest, mode, model, version))

    async def active_job(self, owner: int) -> Record | None:
        async with self.lock:
            return await self._one("SELECT * FROM jobs WHERE owner=? AND status IN ('queued','running')", (owner,))

    async def admit(self, owner: int, lecture_id: int, mode: str, model: str, version: str,
                    chat_id: int, daily_limit: int) -> Record:
        async with self.transaction():
            lecture = await self._one('SELECT * FROM lectures WHERE id=? AND owner=?', (lecture_id, owner))
            if not lecture:
                raise AdmissionError('missing', 'Лекция удалена или недоступна. Откройте «Мои лекции».')
            if await self._one("SELECT id FROM jobs WHERE owner=? AND status IN ('queued','running')", (owner,)):
                raise AdmissionError('busy', 'У вас уже есть незавершённое задание. Дождитесь результата или отмените очередь.')
            midnight = int(time.time() // 86400) * 86400
            count = await self._one("SELECT COUNT(*) n FROM jobs WHERE owner=? AND created>=? "
                                    "AND (started IS NOT NULL OR status IN ('queued','running','done'))", (owner, midnight))
            if count['n'] >= daily_limit:
                raise AdmissionError('daily_limit', 'Дневной лимит новых генераций исчерпан. Готовые результаты доступны без ограничений.')
            cursor = await self.connection.execute(
                "INSERT INTO jobs(owner,lecture_id,mode,model,prompt_version,status,created,chat_id) VALUES(?,?,?,?,?,'queued',?,?)",
                (owner, lecture_id, mode, model, version, time.time(), chat_id))
            return await self._one('SELECT * FROM jobs WHERE id=?', (cursor.lastrowid,))

    async def job(self, owner: int, job_id: int) -> Record | None:
        async with self.lock:
            return await self._one('SELECT j.*, l.name, l.path, l.sha256 FROM jobs j JOIN lectures l ON l.id=j.lecture_id '
                                   'WHERE j.owner=? AND j.id=?', (owner, job_id))

    async def set_job_status(self, job_id: int, status: str, category: str | None = None) -> None:
        async with self.lock:
            if status == 'running':
                await self.connection.execute("UPDATE jobs SET status=?,started=? WHERE id=? AND status='queued'", (status, time.time(), job_id))
            else:
                await self.connection.execute('UPDATE jobs SET status=?,finished=?,error_category=? WHERE id=?',
                                               (status, time.time(), category, job_id))

    async def finish(self, job: Record, content: str, usage: dict | None) -> Record:
        async with self.transaction():
            lecture = await self._one('SELECT * FROM lectures WHERE id=?', (job['lecture_id'],))
            await self.connection.execute(
                'INSERT INTO results(owner,file_hash,mode,model,prompt_version,content,usage_json,created) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(owner,file_hash,mode,model,prompt_version) DO UPDATE SET content=excluded.content,usage_json=excluded.usage_json,created=excluded.created',
                (job['owner'], lecture['sha256'], job['mode'], job['model'], job['prompt_version'], content,
                 json.dumps(usage, ensure_ascii=False) if usage else None, time.time()))
            result = await self._one('SELECT * FROM results WHERE owner=? AND file_hash=? AND mode=? AND model=? AND prompt_version=?',
                                      (job['owner'], lecture['sha256'], job['mode'], job['model'], job['prompt_version']))
            await self.connection.execute("UPDATE jobs SET status='done',finished=?,result_id=? WHERE id=?",
                                          (time.time(), result['id'], job['id']))
            return result

    async def result(self, owner: int, result_id: int) -> Record | None:
        async with self.lock:
            return await self._one('SELECT * FROM results WHERE id=? AND owner=?', (result_id, owner))

    async def lecture_results(self, owner: int, lecture_id: int) -> list[Record]:
        async with self.lock:
            return await self._all('SELECT r.* FROM results r JOIN lectures l ON l.owner=r.owner AND l.sha256=r.file_hash '
                                   'WHERE l.id=? AND l.owner=? ORDER BY r.created DESC', (lecture_id, owner))

    async def result_name(self, owner: int, result_id: int) -> str:
        async with self.lock:
            row = await self._one('SELECT l.id, l.name FROM lectures l JOIN results r ON r.owner=l.owner AND r.file_hash=l.sha256 '
                                  'WHERE r.id=? AND r.owner=? ORDER BY l.id DESC LIMIT 1', (result_id, owner))
            return f"{row['name']} (лекция №{row['id']})" if row else 'Лекция'

    async def remove_lectures(self, owner: int, lecture_id: int | None = None) -> list[Record]:
        async with self.transaction():
            clause = 'owner=?' + (' AND id=?' if lecture_id is not None else '')
            args = (owner, lecture_id) if lecture_id is not None else (owner,)
            rows = await self._all('SELECT * FROM lectures WHERE ' + clause, args)
            for row in rows:
                if await self._one("SELECT id FROM jobs WHERE lecture_id=? AND status IN ('queued','running')", (row['id'],)):
                    raise AdmissionError('busy', 'Файл используется заданием. Отмените ожидающее задание или дождитесь завершения.')
            await self.connection.execute('DELETE FROM lectures WHERE ' + clause, args)
            await self.connection.execute('DELETE FROM results WHERE owner=? AND NOT EXISTS '
                                          '(SELECT 1 FROM lectures l WHERE l.owner=results.owner AND l.sha256=results.file_hash)', (owner,))
            return rows

    async def all_lectures(self) -> list[Record]:
        async with self.lock:
            return await self._all('SELECT * FROM lectures')

    async def reserve_support(self, owner: int, cooldown: int) -> bool:
        async with self.transaction():
            row = await self._one('SELECT created FROM support_attempts WHERE owner=?', (owner,))
            now = time.time()
            if row and now - row['created'] < cooldown:
                return False
            await self.connection.execute('INSERT OR REPLACE INTO support_attempts VALUES(?,?)', (owner, now))
            return True

    async def quiz_session(self, owner: int, result_id: int) -> Record:
        async with self.transaction():
            if not await self._one("SELECT id FROM results WHERE id=? AND owner=? AND mode='quiz'", (result_id, owner)):
                raise AdmissionError('missing', 'Тест удалён или недоступен.')
            row = await self._one('SELECT * FROM quiz_sessions WHERE owner=? AND result_id=? AND finished=0 ORDER BY id DESC LIMIT 1', (owner, result_id))
            if row:
                return row
            cursor = await self.connection.execute('INSERT INTO quiz_sessions(owner,result_id,created) VALUES(?,?,?)', (owner, result_id, time.time()))
            return await self._one('SELECT * FROM quiz_sessions WHERE id=?', (cursor.lastrowid,))

    async def session(self, owner: int, session_id: int) -> Record | None:
        async with self.lock:
            return await self._one('SELECT * FROM quiz_sessions WHERE id=? AND owner=?', (session_id, owner))

    async def answer_quiz(self, owner: int, session_id: int, position: int, option: int) -> tuple[Record, Record]:
        async with self.transaction():
            session = await self._one('SELECT * FROM quiz_sessions WHERE id=? AND owner=?', (session_id, owner))
            if not session or session['finished'] or session['position'] != position:
                raise AdmissionError('answered', 'Ответ уже принят или этот вопрос устарел. Откройте тест заново из истории.')
            result = await self._one('SELECT * FROM results WHERE id=? AND owner=?', (session['result_id'], owner))
            questions = json.loads(result['content'])['questions']
            question = questions[position]
            if not 0 <= option < len(question['options']):
                raise AdmissionError('invalid', 'Такого варианта ответа нет.')
            answers = json.loads(session['answers']) + [option]
            score = session['score'] + int(option == question['correct_index'])
            await self.connection.execute('UPDATE quiz_sessions SET position=?,answers=?,score=?,finished=? WHERE id=?',
                                          (position + 1, json.dumps(answers), score, int(position + 1 == len(questions)), session_id))
            return await self._one('SELECT * FROM quiz_sessions WHERE id=?', (session_id,)), question

    async def interrupt_active(self) -> None:
        async with self.lock:
            await self.connection.execute("UPDATE jobs SET status='interrupted',finished=?,error_category='shutdown' WHERE status IN ('queued','running')", (time.time(),))

    async def pending_cleanup(self) -> list[Record]:
        async with self.lock:
            return await self._all('SELECT * FROM remote_cleanup WHERE attempts<3 AND next_attempt<=?', (time.time(),))

    async def schedule_remote_cleanup(self, file_id: str) -> None:
        async with self.lock:
            await self.connection.execute('INSERT OR IGNORE INTO remote_cleanup VALUES(?,0,?)', (file_id, time.time() + 30))

    async def remote_cleanup_done(self, file_id: str, success: bool) -> None:
        async with self.lock:
            if success:
                await self.connection.execute('DELETE FROM remote_cleanup WHERE file_id=?', (file_id,))
            else:
                await self.connection.execute('UPDATE remote_cleanup SET attempts=attempts+1,next_attempt=? WHERE file_id=?',
                                               (time.time() + 300, file_id))
