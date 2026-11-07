"""Per-user UUID storage with quotas, bounded downloads and safe maintenance."""
import asyncio
import io
import logging
import time
from collections import defaultdict
from pathlib import Path
from uuid import UUID, uuid4

from app.config import Settings
from app.database.store import AdmissionError, Record, Store
from app.services.archive_text import DocumentError
from app.services.content import validate_file

log = logging.getLogger(__name__)


class LimitedWriter(io.BufferedWriter):
    def __init__(self, raw, limit: int):
        super().__init__(raw)
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise DocumentError('Фактический размер файла превышает лимит загрузки.')
        return super().write(data)


class LectureFiles:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        self.locks = defaultdict(asyncio.Lock)
        self.uploading: set[Path] = set()
        self.upload_tasks: dict[int, asyncio.Task] = {}

    async def save(self, owner: int, name: str, document, bot) -> Record:
        declared = getattr(document, 'file_size', None)
        if declared is not None and (declared <= 0 or declared > self.settings.max_upload_bytes):
            raise DocumentError(f'Размер файла должен быть от 1 байта до {self.settings.max_upload_bytes // 1024 // 1024} МБ.')
        async with self.locks[owner]:
            directory = self.settings.upload_dir / str(owner)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{uuid4()}{Path(name).suffix.lower()}'
            self.uploading.add(path)
            self.upload_tasks[owner] = asyncio.current_task()
            committed = False
            preparation = None
            try:
                remote = await bot.get_file(document.file_id)
                remote_size = getattr(remote, 'file_size', None)
                if remote_size is not None and remote_size > self.settings.max_upload_bytes:
                    raise DocumentError('Размер файла в Telegram превышает лимит загрузки.')
                if not remote.file_path:
                    raise DocumentError('Telegram не предоставил файл. Загрузите его заново.')
                with LimitedWriter(io.FileIO(path, 'wb'), self.settings.max_upload_bytes) as output:
                    async with asyncio.timeout(self.settings.upload_timeout):
                        await bot.download_file(remote.file_path, destination=output)
                size = path.stat().st_size
                if not 0 < size <= self.settings.max_upload_bytes:
                    raise DocumentError('Фактический размер файла не соответствует лимиту.')
                if declared is not None and size != declared:
                    raise DocumentError('Файл скачан не полностью. Попробуйте загрузить его ещё раз.')
                preparation = asyncio.create_task(asyncio.to_thread(validate_file, path))
                digest = await asyncio.shield(preparation)
                row = await self.store.add_lecture(owner, name, path, digest, size, self.settings.retention_days,
                                                    self.settings.max_user_files, self.settings.max_user_bytes)
                committed = True
                log.info('lecture_uploaded owner=%s lecture=%s format=%s bytes=%s', owner, row['id'], row['format'], size)
                return row
            finally:
                if preparation and not preparation.done():
                    try:
                        await preparation
                    except Exception:
                        pass
                self.uploading.discard(path)
                self.upload_tasks.pop(owner, None)
                if not committed:
                    self._unlink(path, owner)

    async def cancel_upload(self, owner: int) -> bool:
        task = self.upload_tasks.get(owner)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return True
        return False

    def _unlink(self, path: Path, owner: int) -> None:
        root = self.settings.upload_dir.resolve()
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(root) or path.parent.name != str(owner):
                log.warning('file_cleanup_rejected owner=%s', owner)
                return
            UUID(path.stem)
            path.unlink(missing_ok=True)
        except (ValueError, OSError):
            log.warning('file_cleanup_failed owner=%s', owner)

    async def delete(self, owner: int, lecture_id: int | None = None) -> int:
        async with self.locks[owner]:
            rows = await self.store.remove_lectures(owner, lecture_id)
            for row in rows:
                self._unlink(Path(row['path']), owner)
            log.info('lectures_deleted owner=%s count=%s', owner, len(rows))
            return len(rows)

    async def cleanup(self) -> None:
        now = time.time()
        for row in await self.store.all_lectures():
            if row['expires'] <= now:
                try:
                    await self.delete(row['owner'], row['id'])
                except AdmissionError:
                    pass  # Queued/running jobs pin their source until completion.
        root = self.settings.upload_dir
        root.mkdir(parents=True, exist_ok=True)
        for directory in root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not directory.name.isdecimal():
                continue
            owner = int(directory.name)
            async with self.locks[owner]:
                known = {r['path'] for r in await self.store.all_lectures()}
                for path in directory.iterdir():
                    if not path.is_file() or path.is_symlink() or path in self.uploading or str(path) in known:
                        continue
                    try:
                        if now - path.stat().st_mtime >= self.settings.orphan_grace:
                            self._unlink(path, owner)
                    except OSError:
                        log.warning('orphan_cleanup_failed owner=%s', owner)
