"""Local Silero speech verification; never send non-speech to a cloud model."""
from functools import lru_cache
import hashlib
import io
from pathlib import Path
import wave

MODEL = Path(__file__).parent / 'models/silero_vad_16k_op15.onnx'
MODEL_SHA256 = '7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49'


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as ort
    if hashlib.sha256(MODEL.read_bytes()).hexdigest() != MODEL_SHA256:
        raise RuntimeError('VAD model checksum mismatch; reinstall the bundled model.')
    options = ort.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    return ort.InferenceSession(str(MODEL), sess_options=options, providers=['CPUExecutionProvider'])


def has_speech(wav):
    """Require 96ms of positive speech, allowing a single uncertain 32ms frame.

    Recurrent state belongs to this capture, never the shared inference session.
    This detects speech, not the speaker's identity or intent to address us.
    """
    import numpy as np
    if not isinstance(wav, bytes) or not 44 < len(wav) <= 2_000_000:
        raise ValueError('Invalid or oversized voice WAV.')
    with wave.open(io.BytesIO(wav), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, 'NONE'):
            raise ValueError('Âm thanh phải là WAV mono PCM 16kHz.')
        pcm = audio.readframes(16000 * 60 + 1)
    if len(pcm) % 2 or len(pcm) > 16000 * 60 * 2:
        raise ValueError('Invalid or oversized voice PCM.')
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
    state, context = np.zeros((2, 1, 128), np.float32), np.zeros((1, 64), np.float32)
    recent = []
    session = _session()
    for offset in range(0, len(samples), 512):
        chunk = np.zeros((1, 512), np.float32)
        part = samples[offset:offset + 512]
        chunk[0, :len(part)] = part
        probability, state = session.run(None, {
            'input': np.concatenate((context, chunk), axis=1),
            'state': state, 'sr': np.array(16000, dtype=np.int64)})
        context = chunk[:, -64:]
        recent = (recent + [float(probability[0, 0]) >= 0.5])[-4:]
        if sum(recent) >= 3:
            return True
    return False
