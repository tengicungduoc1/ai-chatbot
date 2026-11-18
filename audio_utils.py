"""Bounded mono Opus conversion using the user's FFmpeg, no native DLL loading."""
import asyncio
import math
import shutil
import struct


def _crc_entry(byte):
    crc = byte << 24
    for _ in range(8):
        crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
    return crc


_CRC_TABLE = tuple(_crc_entry(byte) for byte in range(256))


def _crc(data):
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[(crc >> 24) ^ byte]
    return crc


def _page(packet, sequence, granule, flags=0):
    lacing = bytes([255] * (len(packet) // 255) + [len(packet) % 255])
    header = b'OggS\0' + bytes([flags]) + struct.pack('<QII', granule, 1, sequence)
    page = bytearray(header + b'\0\0\0\0' + bytes([len(lacing)]) + lacing + packet)
    struct.pack_into('<I', page, 22, _crc(page))
    return bytes(page)


def _opus_pages(packets, input_rate=16000):
    if not packets or len(packets) > 1000:
        raise ValueError('Ghi âm phải dài dưới 60 giây.')
    yield _page(b'OpusHead' + struct.pack('<BBHIhB', 1, 1, 0, input_rate, 0, 0), 0, 0, 2)
    yield _page(b'OpusTags' + struct.pack('<I', 7) + b'Chatbot' + struct.pack('<I', 0), 1, 0)
    for i, packet in enumerate(packets):
        if not isinstance(packet, bytes) or not 1 <= len(packet) <= 4096:
            raise ValueError('Gói âm thanh Opus không hợp lệ.')
        # ponytail: Xiaozhi v1 fixed 60ms mono packets; negotiate before supporting other frame sizes.
        yield _page(packet, i + 2, (i + 1) * 2880, 4 if i == len(packets) - 1 else 0)


def opus_ogg(packets, input_rate=16000):
    return b''.join(_opus_pages(packets, input_rate))


def ogg_packets(data):
    offset, pending, packets = 0, bytearray(), []
    while offset < len(data):
        if data[offset:offset + 4] != b'OggS' or offset + 27 > len(data):
            raise ValueError('Luồng Ogg không hợp lệ.')
        count = data[offset + 26]
        lacing = data[offset + 27:offset + 27 + count]
        if len(lacing) != count:
            raise ValueError('Ogg bị cắt ngắn.')
        cursor = offset + 27 + count
        end = cursor + sum(lacing)
        if end > len(data):
            raise ValueError('Ogg bị cắt ngắn.')
        page = bytearray(data[offset:end])
        checksum = struct.unpack_from('<I', page, 22)[0]
        page[22:26] = b'\0' * 4
        if checksum != _crc(page):
            raise ValueError('Ogg checksum sai.')
        for length in lacing:
            pending.extend(data[cursor:cursor + length])
            cursor += length
            if length < 255:
                packet = bytes(pending)
                pending.clear()
                if not packet.startswith((b'OpusHead', b'OpusTags')):
                    packets.append(packet)
        offset = end
    if pending or not packets:
        raise ValueError('Ogg thiếu gói âm thanh.')
    return packets


async def _ffmpeg(data, arguments):
    executable = shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('Thiếu FFmpeg. Cài FFmpeg và thêm vào PATH, rồi khởi động lại server.')
    proc = await asyncio.create_subprocess_exec(
        executable, '-hide_banner', '-loglevel', 'error', *arguments,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    communication = asyncio.create_task(proc.communicate(input=data))
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), 45)
        if proc.returncode:
            raise ValueError('Không giải mã được âm thanh; kiểm tra định dạng Opus của firmware.')
        return stdout
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await asyncio.gather(communication, return_exceptions=True)


async def opus_to_wav(packets):
    pages = []
    for index, page in enumerate(_opus_pages(packets), 1):
        pages.append(page)
        if index % 8 == 0:
            await asyncio.sleep(0)  # Bound event-loop work; abort leaves no detached thread.
    return await _ffmpeg(b''.join(pages), ['-f', 'ogg', '-i', 'pipe:0', '-ac', '1', '-ar', '16000', '-f', 'wav', 'pipe:1'])


async def _read_ogg_packets(reader):
    pending = bytearray()
    while True:
        try:
            header = await reader.readexactly(27)
        except asyncio.IncompleteReadError as error:
            if error.partial:
                raise ValueError('Luồng Ogg streaming bị cắt ngắn.') from None
            break
        if header[:4] != b'OggS':
            raise ValueError('Luồng Ogg streaming không hợp lệ.')
        lacing = await reader.readexactly(header[26])
        page = bytearray(header + lacing + await reader.readexactly(sum(lacing)))
        checksum = struct.unpack_from('<I', page, 22)[0]
        page[22:26] = bytes(4)
        if checksum != _crc(page):
            raise ValueError('Ogg streaming checksum sai.')
        cursor = 27 + len(lacing)
        for length in lacing:
            pending.extend(page[cursor:cursor + length])
            cursor += length
            if length < 255:
                packet = bytes(pending)
                pending.clear()
                if not packet.startswith((b'OpusHead', b'OpusTags')):
                    yield packet
    if pending:
        raise ValueError('Ogg streaming bị cắt ngắn.')


async def pcm_stream_to_opus_packets(chunks):
    """Encode 24kHz mono PCM incrementally; yield raw Opus packets for Xiaozhi."""
    executable = shutil.which('ffmpeg')
    if not executable:
        raise RuntimeError('Thiếu FFmpeg. Cài FFmpeg và thêm vào PATH, rồi khởi động lại server.')
    proc = await asyncio.create_subprocess_exec(
        executable, '-hide_banner', '-loglevel', 'error', '-f', 's16le', '-ar', '24000', '-ac', '1',
        '-i', 'pipe:0', '-c:a', 'libopus', '-b:a', '32000', '-frame_duration', '60',
        '-flush_packets', '1', '-page_duration', '60000', '-f', 'ogg', 'pipe:1',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    written, odd_byte = 0, b''

    async def feed():
        nonlocal written, odd_byte
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes) or not chunk:
                    continue
                data = odd_byte + chunk
                if len(data) % 2:
                    odd_byte, data = data[-1:], data[:-1]
                else:
                    odd_byte = b''
                written += len(data)
                if written > 24000 * 2 * 180:
                    raise ValueError('PCM streaming vượt quá giới hạn 3 phút.')
                if data:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
            if odd_byte:
                raise ValueError('PCM streaming bị cắt giữa hai byte.')
        finally:
            if not proc.stdin.is_closing():
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

    feeder = asyncio.create_task(feed())
    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        async for packet in _read_ogg_packets(proc.stdout):
            yield packet
        await feeder
        returncode = await proc.wait()
        await stderr_task
        if returncode:
            raise ValueError('Không mã hóa được âm thanh streaming bằng FFmpeg.')
    finally:
        if not feeder.done():
            feeder.cancel()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        await asyncio.gather(feeder, stderr_task, return_exceptions=True)


async def pcm_to_ogg(pcm):
    if not pcm or len(pcm) % 2 or len(pcm) > 24000 * 2 * 180:
        raise ValueError('PCM phải là mono 24kHz 16-bit và dưới 3 phút.')
    return await _ffmpeg(pcm, ['-f', 's16le', '-ar', '24000', '-ac', '1', '-i', 'pipe:0',
                             '-c:a', 'libopus', '-b:a', '32000', '-frame_duration', '60', '-f', 'ogg', 'pipe:1'])


async def notification_beep():
    pcm = b''.join(struct.pack('<h', int(3500 * math.sin(2 * math.pi * 880 * n / 24000)))
                   for n in range(14400))
    return await pcm_to_ogg(pcm)
