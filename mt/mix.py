#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mt.mix —— 分轨混音：音量 → 滤波 → 声像 → 送混响 → 段落增益 → 母线动态 → 响度归一

设计取向（这几条是踩出来的，不是抄的）：

1. **逐轨实测 RMS 再定增益**，不是拍脑袋设 -6dB。GM 音源里不同乐器的固有
   电平差能有 20dB，凭感觉设的平衡换台机器就崩。
2. **混音用纯增益，不做压缩**（除非你明确要"稳"）。压缩会削掉动态弧线，
   而那把弧线往往正是曲子的意思。
3. **卷积混响自己合成 IR**，避免依赖外部 IR 文件；长度直接对应"空间大小"。
4. 母带最后一步按 LUFS 归一，不是按峰值——峰值归一在不同动态内容上
   听起来响度完全不一样。

用法：

    from mt.mix import Mixer
    m = Mixer(sr=44100)
    m.add("piano", "stems/piano.wav", target_db=-29, pan=0.05, send=0.26, hp=70, lp=14000)
    m.add("bass",  "stems/bass.wav",  target_db=-33, pan=0.0,  send=0.05, hp=62,  lp=1200)
    m.mix("out.wav", reverb_seconds=1.6, target_lufs=-16)
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field

import numpy as np
from scipy.signal import butter, fftconvolve, lfilter

from .render import load_wav, save_wav


# ── 基础测量 ──────────────────────────────────────────────────────────
def rms_db(x: np.ndarray) -> float:
    return 20 * np.log10(max(1e-12, float(np.sqrt(np.mean(x ** 2)))))


def active_rms(x: np.ndarray, gate_db: float = -70.0) -> float:
    """只统计"有声"的帧，避免长静音把整体 RMS 拉低。"""
    hop = 2048
    n = len(x) // hop
    if n == 0:
        return rms_db(np.zeros(1))
    frames = x[:n * hop].mean(axis=1).reshape(n, hop)
    r = np.sqrt((frames ** 2).mean(axis=1))
    live = r[r > 10 ** (gate_db / 20)]
    return float(np.median(live)) if len(live) else 1e-9


def lufs(path: str) -> float:
    """用 ffmpeg 的 EBU R128 测整体响度（LUFS）。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        raise RuntimeError(f"loudnorm 没返回数据：{path}")
    return float(json.loads(m.group(0))["input_i"])


def peak_db(x: np.ndarray) -> float:
    return 20 * np.log10(max(1e-12, float(np.abs(x).max())))


def true_peak(path: str) -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_tp\"[^{}]*\}", r.stderr, re.S)
    return float(json.loads(m.group(0))["input_tp"]) if m else float("nan")


# ── 处理单元 ──────────────────────────────────────────────────────────
def filt(x: np.ndarray, hp: float = 0, lp: float = 0, sr: int = 44100) -> np.ndarray:
    if hp:
        x = lfilter(*butter(2, hp / (sr / 2), btype="highpass"), x, axis=0)
    if lp:
        x = lfilter(*butter(2, min(lp, sr / 2 - 100) / (sr / 2), btype="lowpass"), x, axis=0)
    return x


def pan_gains(pan: float) -> tuple[float, float]:
    """pan ∈ [-1, 1]，等功率法则（中间 -3dB，两端不动）。"""
    ang = (max(-1.0, min(1.0, pan)) + 1) * np.pi / 4
    return float(np.cos(ang) * np.sqrt(2)), float(np.sin(ang) * np.sqrt(2))


def make_ir(seconds: float = 1.6, sr: int = 44100, seed: int = 5) -> np.ndarray:
    """合成一条立体声混响 IR：指数衰减噪声 + 几个早期反射。"""
    rng = np.random.default_rng(seed)
    n = int(sr * seconds)
    t = np.arange(n) / sr
    env = np.exp(-t / seconds * 4.2)
    ir = np.zeros((n, 2))
    for c in range(2):
        noise = rng.standard_normal(n) * env
        for d, g in ((0.007, 0.5), (0.013, 0.34), (0.021, 0.22)):
            k = int(d * sr) + c * 9
            if k < n:
                noise[k] += g * rng.choice([-1, 1])
        ir[:, c] = noise / (np.abs(noise).max() + 1e-9)
    return ir


def compress(x: np.ndarray, thresh_db: float = -18.0, ratio: float = 1.8,
             attack: float = 0.012, release: float = 0.22, sr: int = 44100):
    """温和的母线压缩（快起慢落）。返回 (处理后的信号, 最大压缩量 dB)。"""
    env = np.abs(x).max(axis=1)
    ka = 1 - np.exp(-1 / (attack * sr))
    kr = 1 - np.exp(-1 / (release * sr))
    fast = lfilter([ka], [1, -(1 - ka)], env)
    slow = lfilter([kr], [1, -(1 - kr)], env)
    e = np.maximum(fast, slow)
    db = 20 * np.log10(np.maximum(e, 1e-9))
    over = np.maximum(0.0, db - thresh_db)
    gain_db = -over * (1 - 1 / ratio)
    return x * (10 ** (gain_db / 20))[:, None], float(gain_db.min())


def section_env(n: int, sections, total_bars: int, sr: int = 44100,
                smooth: float = 0.4) -> np.ndarray:
    """把 [(起始小节, 增益)] 变成平滑的电平包络（阶跃式的段落切换会"咔"）。"""
    per = n / max(total_bars, 1)
    env = np.ones(n)
    for bar, g in sections:
        env[int((bar - 1) * per):] = g
    k = int(smooth * sr)
    if k > 1:
        env = fftconvolve(env, np.hanning(k) / np.hanning(k).sum(), mode="same")
    return env


def normalize_lufs(path: str, target: float = -16.0) -> float:
    """按纯净增益把整首推到目标 LUFS（线性，不再压动态）。返回施加的增益 dB。"""
    cur = lufs(path)
    g = target - cur
    a, sr = load_wav(path)
    save_wav(path, a * 10 ** (g / 20), sr)
    return g


# ── 混音器 ────────────────────────────────────────────────────────────
@dataclass
class Stem:
    name: str
    path: str
    target_db: float = -30.0
    pan: float = 0.0
    send: float = 0.15          # 送混响的量（0~1）
    hp: float = 60.0            # 高通 Hz
    lp: float = 16000.0         # 低通 Hz
    mono: bool = True           # 单声道源（渲染后复制到两边）
    gain_db: float = 0.0        # 额外手动修正


class Mixer:
    def __init__(self, sr: int = 44100):
        self.sr = sr
        self.stems: list[Stem] = []
        self._audio: dict[str, np.ndarray] = {}

    def add(self, name: str, path: str, **kw) -> Stem:
        s = Stem(name=name, path=path, **kw)
        self.stems.append(s)
        return s

    def load(self):
        """读入所有 stem，返回 {名字: 实测电平 dB}。"""
        levels = {}
        for s in self.stems:
            a, sr = load_wav(s.path)
            if sr != self.sr:
                raise ValueError(f"{s.path} 采样率 {sr} ≠ 混音采样率 {self.sr}")
            if s.mono:
                a = a.mean(axis=1, keepdims=True).repeat(2, axis=1)
            a = filt(a, s.hp, s.lp, self.sr)
            rms = active_rms(a)
            gain = 10 ** ((s.target_db + s.gain_db - 20 * np.log10(max(rms, 1e-9))) / 20)
            s_rms = 20 * np.log10(max(rms, 1e-9))
            levels[s.name] = (s_rms, 20 * np.log10(gain))
            self._audio[s.name] = a * gain
        return levels

    def mix(self, out_path: str, reverb_seconds: float = 2.0, reverb_wet: float = 0.55,
            sections=None, total_bars: int = 0, compress_ratio: float | None = None,
            compress_thresh: float = -18.0, target_lufs: float | None = -16.0,
            trim_tail: float = 1.2, fade: float = 0.8, ceiling: float = -1.5,
            verbose: bool = True) -> dict:
        if not self._audio:
            self.load()

        n = max(len(a) for a in self._audio.values())
        mix, send = np.zeros((n, 2)), np.zeros((n, 2))
        for s in self.stems:
            a = self._audio[s.name]
            pad = np.zeros((n, 2))
            pad[:len(a)] = a
            mix += pad
            send += pad * s.send

        if reverb_seconds > 0:
            ir = make_ir(reverb_seconds, self.sr)
            wet = np.stack([fftconvolve(send[:, 0], ir[:, 0])[:n],
                            fftconvolve(send[:, 1], ir[:, 1])[:n]], axis=1) * reverb_wet
            mix = mix + wet

        if sections and total_bars:
            mix *= section_env(len(mix), sections, total_bars, self.sr)[:, None]

        deepest = 0.0
        if compress_ratio:
            mix, deepest = compress(mix, compress_thresh, compress_ratio, sr=self.sr)

        # 只裁到最后一个有声点，不做长淡出（要能硬接下一首的地方就别淡出）
        live = np.where(np.abs(mix).max(axis=1) > 1e-4)[0]
        end = min(n, live[-1] + int(trim_tail * self.sr)) if len(live) else n
        mix = mix[:end]
        if fade > 0:
            k = min(int(fade * self.sr), len(mix))
            mix[-k:] *= (np.linspace(1, 0, k) ** 1.5)[:, None]

        # 保护：只在真的会削波时才软限幅
        pk = np.abs(mix).max()
        if pk > 10 ** (-1.0 / 20):
            mix = np.tanh(mix / pk * 1.4) / np.tanh(1.4)
        mix *= 10 ** ((ceiling - peak_db(mix)) / 20)

        save_wav(out_path, mix, self.sr)
        gain = normalize_lufs(out_path, target_lufs) if target_lufs else 0.0

        info = {"seconds": len(mix) / self.sr, "lufs": lufs(out_path),
                "true_peak": true_peak(out_path), "lufs_gain": gain,
                "compression_db": deepest}
        if verbose:
            print(f"  → {out_path}｜{info['seconds']:.2f}s｜{info['lufs']:.1f} LUFS"
                  f"｜真峰 {info['true_peak']:.1f} dBTP"
                  + (f"｜压缩最深 {deepest:.1f} dB" if compress_ratio else ""))
        return info
