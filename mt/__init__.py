#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mt —— 代码作曲工具链

四个环节，各自可独立使用：

    midi.py     零依赖 SMF 写入器 + 独立读取器
                Score / Voice / read_midi

    render.py   fluidsynth + SoundFont 渲染（可复现的 GM 音源）
                render_midi / render_stems / load_wav / save_wav / to_mp3

    mix.py      分轨混音：实测音量 → 滤波 → 声像 → 卷积混响
                → 段落增益 → 母线动态 → LUFS 归一
                Mixer / Stem / make_ir / compress / lufs

    analyze.py  只看数字的客观质检（因为我听不到声音）
                report / section_rms / spectral_bands / clicks

    verify.py   渲染之前就能做的乐理断言
                Checker.range_check / no_overlap / harmony / velocity

最小例子：

    from mt import Score, Mixer, analyze
    from mt.midi import GM

    s = Score(title="demo")
    s.tempo(1, 90).marker(1, "A 段")
    piano = s.voice("piano", channel=0, program=GM["acoustic_grand"])
    piano.chord(1, 1, 3.5, ["A4", "C5", "E5"], vel=64)
    s.write("demo.mid")

命令行：mt render / mt qa / mt info / mt verify / mt mix
"""
from .midi import Score, Voice, Track, read_midi, note_number, pitch_name, GM
from .render import render_midi, render_stems, load_wav, save_wav, to_mp3, find_soundfont
from .mix import Mixer, Stem, make_ir, compress, lufs, peak_db, true_peak, rms_db
from .analyze import report, section_rms, spectral_bands, spectral_centroid, clicks
from .verify import Checker, CHORD_TONES, RANGES

__version__ = "1.0.0"
__all__ = [
    "Score", "Voice", "Track", "read_midi", "note_number", "pitch_name", "GM",
    "render_midi", "render_stems", "load_wav", "save_wav", "to_mp3", "find_soundfont",
    "Mixer", "Stem", "make_ir", "compress", "lufs", "peak_db", "true_peak", "rms_db",
    "report", "section_rms", "spectral_bands", "spectral_centroid", "clicks",
    "Checker", "CHORD_TONES", "RANGES", "analyze",
]
