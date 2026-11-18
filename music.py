"""Opt-in, bounded YouTube audio resolution and atomic Opus cache."""
import asyncio
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile

from audio_utils import ogg_packets

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def validate_video_id(value):
    if not isinstance(value, str) or not VIDEO_ID.fullmatch(value):
        raise ValueError("YouTube video ID không hợp lệ.")
    return value


def extract_video_id(text):
    if not isinstance(text, str):
        return None
    match = re.search(r"(?:v=|youtu\.be/|youtube\.com/(?:shorts/|embed/))([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", text)
    if match:
        return match.group(1)
    match = re.search(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", text)
    return match.group(1) if match else None


def music_search_query(text):
    if not isinstance(text, str):
        return None
    quoted = re.search(r'[\"“”‘’](.+?)[\"“”‘’]', text)
    if quoted:
        query = quoted.group(1).strip()
    else:
        query = re.sub(r'^.*?\b(?:phát|mở|bật|nghe|play)\b', '', text, count=1, flags=re.IGNORECASE).strip()
        query = re.sub(r'^(?:(?:youtube|nhạc|bài hát|bài)\s*)+', '', query, flags=re.IGNORECASE).strip()
        query = re.sub(r'\b(?:trên youtube|youtube|giúp tôi|cho tôi nghe)\b', ' ', query, flags=re.IGNORECASE)
        query = re.sub(r'\b(?:nhé|nha)\b$', '', query, flags=re.IGNORECASE)
    query = re.sub(r'\s+', ' ', query).strip(' .,!?:;')
    return query[:160] or None


def is_music_request(text, *, calendar_intent=False, pending_confirmation=False):
    if not isinstance(text, str) or calendar_intent or pending_confirmation:
        return False
    return bool(re.search(r"\b(?:nhạc|bài hát|phát nhạc|youtube|music)\b|\b(?:phát|mở|bật|nghe)\s+bài\b", text.casefold()))



def route_music(text, *, calendar_intent=False, pending_confirmation=False):
    return 'calendar' if calendar_intent or pending_confirmation else ('music' if is_music_request(text) else 'general')


def _validate_audio(data):
    if (len(data) < 47 or data[:5] != b'OggS\0' or not data[5] & 2
            or data[28:36] != b'OpusHead' or data[36] != 1 or data[37] != 1
            or struct.unpack_from('<I', data, 40)[0] != 24000):
        raise ValueError('Ogg không phải Opus mono 24kHz.')
    packets = ogg_packets(data)
    offset = 0
    last_flags = 0
    serial = None
    sequence = 0
    while offset < len(data):
        if data[offset:offset + 5] != b'OggS\0':
            raise ValueError('Trang Ogg không hợp lệ.')
        last_flags = data[offset + 5]
        page_serial, page_sequence = struct.unpack_from('<II', data, offset + 14)
        if serial is None:
            serial = page_serial
        if page_serial != serial or page_sequence != sequence:
            raise ValueError('Luồng Ogg không liên tục.')
        sequence += 1
        count = data[offset + 26]
        lacing = data[offset + 27:offset + 27 + count]
        offset += 27 + count + sum(lacing)
    if not last_flags & 4:
        raise ValueError('Luồng Ogg chưa hoàn tất.')
    return packets


class MusicCache:
    def __init__(self, root, max_bytes=50 * 1024 * 1024):
        self.root = Path(root)
        self.max_bytes = max_bytes

    def path(self, video_id):
        return self.root / (validate_video_id(video_id) + '.v2.ogg')

    def get(self, video_id):
        path = self.path(video_id)
        if not path.is_file() or path.stat().st_size > self.max_bytes:
            return None
        data = path.read_bytes()
        try:
            _validate_audio(data)
        except (OSError, ValueError, IndexError, struct.error):
            path.unlink(missing_ok=True)
            return None
        return data

    def put(self, video_id, data):
        validate_video_id(video_id)
        if not isinstance(data, bytes) or not data or len(data) > self.max_bytes:
            raise ValueError('Âm thanh nhạc vượt giới hạn.')
        _validate_audio(data)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('wb', dir=self.root, prefix='.music-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(video_id))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class YouTubeResolver:
    def __init__(self, cache, timeout=45, enabled=False):
        self.cache = cache
        self.timeout = timeout
        self.enabled = enabled
        self._semaphore = asyncio.Semaphore(2)

    async def search(self, query):
        if not self.enabled:
            raise RuntimeError('Nhạc YouTube chưa được bật.')
        if not isinstance(query, str) or not query.strip() or len(query.strip()) > 160:
            raise ValueError('Tên bài hát không hợp lệ.')
        async with asyncio.timeout(self.timeout):
            async with self._semaphore:
                result = await self._run([sys.executable, '-m', 'yt_dlp', '--flat-playlist',
                                          '--no-playlist', '--playlist-end', '1', '--no-warnings', '--quiet',
                                          '--print', 'id', '--', 'ytsearch1:' + query.strip()], capture=True)
        ids = [line.strip() for line in result.decode('utf-8', 'strict').splitlines() if line.strip()]
        if len(ids) != 1:
            raise ValueError('Không tìm thấy bài hát trên YouTube.')
        return validate_video_id(ids[0])

    async def resolve(self, video_id):
        video_id = validate_video_id(video_id)
        cached = self.cache.get(video_id)
        if cached is not None:
            return cached
        if not self.enabled:
            raise RuntimeError('Nhạc YouTube chưa được bật.')
        async with asyncio.timeout(self.timeout):
            async with self._semaphore:
                cached = self.cache.get(video_id)
                if cached is not None:
                    return cached
                with tempfile.TemporaryDirectory(dir=self._temporary_root()) as folder:
                    source = Path(folder) / 'source'
                    await self._download(video_id, source)
                    data = await self._transcode(source)
                    self.cache.put(video_id, data)
                    return data

    def _temporary_root(self):
        self.cache.root.mkdir(parents=True, exist_ok=True)
        return self.cache.root

    async def _download(self, video_id, target):
        await self._run([sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-part',
                         '--no-warnings', '--quiet', '--socket-timeout', str(self.timeout),
                         '--max-filesize', str(self.cache.max_bytes), '-f', 'worstaudio',
                         '-o', str(target), '--', 'https://www.youtube.com/watch?v=' + video_id], output_file=target)
        if not target.is_file() or not 0 < target.stat().st_size <= self.cache.max_bytes:
            raise ValueError('Tệp nhạc vượt giới hạn hoặc rỗng.')

    async def _transcode(self, source):
        executable = shutil.which('ffmpeg')
        if not executable:
            raise RuntimeError('Thiếu FFmpeg. Cài FFmpeg và thêm vào PATH, rồi khởi động lại server.')
        return await self._run([executable, '-hide_banner', '-loglevel', 'error',
                                '-protocol_whitelist', 'file,pipe', '-i', str(source), '-vn',
                                '-ac', '1', '-ar', '24000', '-c:a', 'libopus',
                                '-b:a', '32000', '-frame_duration', '60', '-f', 'ogg', 'pipe:1'], capture=True)

    async def _run(self, command, capture=False, output_file=None):
        proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        try:
            data = bytearray()
            if capture:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > self.cache.max_bytes:
                        raise ValueError('Âm thanh nhạc vượt giới hạn.')
            while output_file is not None and proc.returncode is None:
                if output_file.exists() and output_file.stat().st_size > self.cache.max_bytes:
                    raise ValueError('Tệp nhạc vượt giới hạn.')
                try:
                    await asyncio.wait_for(proc.wait(), 0.1)
                except TimeoutError:
                    pass
            if await proc.wait():
                raise RuntimeError('Không tải hoặc chuyển đổi được nhạc.')
            return bytes(data)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
