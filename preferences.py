"""Validated local assistant preferences, separate from credentials and calendar data."""
import json
import os
from pathlib import Path
import tempfile

# Verified Gemini API models: audio input, function calling, and minimal thinking.
MODELS = (
    'gemini-3-flash-preview',
    'gemini-3.1-flash-lite',
    'gemini-3.6-flash',
)
VOICES = (
    'Zephyr', 'Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede',
    'Callirrhoe', 'Autonoe', 'Enceladus', 'Iapetus', 'Umbriel', 'Algieba',
    'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar',
    'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi',
    'Vindemiatrix', 'Sadachbia', 'Sadaltager', 'Sulafat',
)
DEFAULTS = {'assistant_name': 'Trợ lý', 'style_prompt': '', 'model': 'gemini-flash-latest', 'voice': 'Kore'}
EDITABLE = frozenset(DEFAULTS)


class Preferences:
    def __init__(self, path, defaults=None):
        self.path = Path(path)
        self.defaults = {**DEFAULTS, **(defaults or {})}
        self.values = self._load()

    def validate(self, data):
        if not isinstance(data, dict) or set(data) != EDITABLE:
            raise ValueError('Cần đúng bốn trường assistant_name, style_prompt, model và voice.')
        name, style, model, voice = (data[key] for key in ('assistant_name', 'style_prompt', 'model', 'voice'))
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64 or any(ord(c) < 32 for c in name):
            raise ValueError('assistant_name phải dài 1–64 ký tự.')
        if not isinstance(style, str) or len(style) > 1200 or '\x00' in style:
            raise ValueError('style_prompt tối đa 1200 ký tự.')
        if not isinstance(model, str) or model != DEFAULTS['model'] and model not in MODELS:
            raise ValueError('Model không hỗ trợ audio, công cụ và thinking tối thiểu.')
        if not isinstance(voice, str) or voice not in VOICES:
            raise ValueError('Giọng Gemini TTS không hợp lệ.')
        return {'assistant_name': name.strip(), 'style_prompt': style.strip(), 'model': model, 'voice': voice}

    def _load(self):
        try:
            loaded = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(loaded, dict) or set(loaded) != EDITABLE:
                return self.defaults.copy()
            # Keep legacy .env/default model aliases, but only accept verified IDs from saved preferences.
            if loaded.get('model') not in MODELS and loaded.get('model') != self.defaults['model']:
                return self.defaults.copy()
            if not isinstance(loaded.get('assistant_name'), str) or not 1 <= len(loaded['assistant_name'].strip()) <= 64:
                return self.defaults.copy()
            if not isinstance(loaded.get('style_prompt'), str) or len(loaded['style_prompt']) > 1200 or '\x00' in loaded['style_prompt']:
                return self.defaults.copy()
            if not isinstance(loaded.get('voice'), str) or loaded['voice'] not in VOICES:
                return self.defaults.copy()
            return {'assistant_name': loaded['assistant_name'].strip(), 'style_prompt': loaded['style_prompt'].strip(),
                    'model': loaded['model'], 'voice': loaded['voice']}
        except (OSError, ValueError, TypeError):
            return self.defaults.copy()

    def save(self, data):
        values = self.validate(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                                             prefix=self.path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(values, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.values = values
        return self.as_dict()

    def as_dict(self):
        return self.values.copy()

    def response(self):
        return {**self.as_dict(), 'models': list(MODELS), 'voices': list(VOICES)}
