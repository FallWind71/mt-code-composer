#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mt.render —— 用 fluidsynth + SoundFont 把 MIDI 渲染成 wav

为什么不用更高级的音源：GM 音源的目的不是好听，是**可复现、可脚本化**。
同一条命令在任何机器上都能得到同一个波形，这样"这个音对不对"才能被讨论。

    render_midi("a.mid", "a.wav", soundfont="MuseScore_General.sf3")
    render_stems({"piano": "stems/piano.mid"}, "stems", soundfont=...)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import wave

import numpy as np

# 常见 SoundFont 位置（按顺序找第一个存在的）
CANDIDATES = [
    os.environ.get("MT_SOUNDFONT", ""),
    os.path.expanduser("~/Projects/ForLearning/music_train/music/_soundfonts/MuseScore_General.sf3"),
    "/usr/share/soundfonts/default.sf2",
    "/usr/share/sounds/sf2/FluidR3_GM.sf2",
]


def find_soundfont(explicit: str | None = None) -> str:
    for c in ([explicit] if explicit else []) + CANDIDATES:
        if c and os.path.exists(c):
            return c
    raise FileNotFoundError(
        "找不到 SoundFont。用 soundfont=... 指定，或设环境变量 MT_SOUNDFONT。\n"
        "推荐 MuseScore_General.sf3（约 40MB，GM 完整音色）。"
    )


def have_fluidsynth() -> bool:
    return shutil.which("fluidsynth") is not None


def render_midi(midi: str, wav: str, soundfont: str | None = None,
                sr: int = 44100, gain: float = 0.85, force: bool = True) -> str:
    """MIDI → 单文件 wav（44.1k / 16bit / 立体声）。"""
    if not have_fluidsynth():
        raise RuntimeError("没装 fluidsynth（Debian/Arch: sudo apt install fluidsynth / pacman -S fluidsynth）")
    sf = find_soundfont(soundfont)
    if not force and os.path.exists(wav) and os.path.getmtime(wav) > os.path.getmtime(midi):
        return wav
    os.makedirs(os.path.dirname(os.path.abspath(wav)) or ".", exist_ok=True)
    subprocess.run(
        ["fluidsynth", "-ni", "-g", str(gain), "-r", str(sr), "-F", wav, sf, midi],
        check=True, capture_output=True,
    )
    if not os.path.exists(wav) or os.path.getsize(wav) < 1024:
        raise RuntimeError(f"fluidsynth 没产出音频：{wav}")
    return wav


def render_stems(stems: dict[str, str], outdir: str = "stems",
                 soundfont: str | None = None, sr: int = 44100,
                 gain: float = 0.85, force: bool = False) -> dict[str, str]:
    """{名字: midi 路径} → {名字: wav 路径}。默认增量渲染（mid 比 wav 新才重渲）。"""
    os.makedirs(outdir, exist_ok=True)
    out = {}
    for name, mid in stems.items():
        wav = os.path.join(outdir, name + ".wav")
        render_midi(mid, wav, soundfont=soundfont, sr=sr, gain=gain, force=force)
        out[name] = wav
    return out


def load_wav(path: str) -> tuple[np.ndarray, int]:
    """读成 float64 的 (n, 2) 数组，范围 -1..1。单声道自动复制成双声道。"""
    w = wave.open(path)
    sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
    a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64) / 32768.0
    w.close()
    if ch == 1:
        a = np.stack([a, a], axis=1)
    else:
        a = a.reshape(-1, ch)[:, :2]
    return a, sr


def save_wav(path: str, a: np.ndarray, sr: int):
    a = np.clip(a, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((a * 32767).astype(np.int16).tobytes())
    return path


def to_mp3(wav: str, mp3: str | None = None, bitrate: str = "320k") -> str:
    mp3 = mp3 or os.path.splitext(wav)[0] + ".mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", wav, "-c:a", "libmp3lame",
                    "-b:a", bitrate, mp3], check=True)
    return mp3
