"""Video Composer: analysed review video -> branded short (template + captions + lower-third).

    template.py    template model (canvas, video zone, layers, fonts, colours) + generated placeholder
    textrender.py  Pillow rendering of caption / lower-third PNGs (Devanagari-capable, alpha)
    captions.py    transcript blocks -> caption cues -> SRT / ASS
    renderer.py    deterministic ffmpeg command builder + runner, export presets
    cuts.py        rule-based cut proposals (+ optional Gemini refinement)
    store.py       `renders` records (PostgreSQL or JSON fallback) + background worker
"""
from services.video_composer.settings import ComposerSettings, get_composer_settings

__all__ = ["ComposerSettings", "get_composer_settings"]
