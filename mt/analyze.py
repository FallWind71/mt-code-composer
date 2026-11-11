#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mt.analyze —— 对渲染出的音频做客观质检

前提：**我听不到声音**。所以每一个"这段对不对"的判断都必须落成数字，
而且这些数字要能证伪（不是"看起来挺好"）。

提供两类东西：
  1. 测量函数：lufs / true_peak / rms_db / spectral_bands / section_rms / clicks
  2. report()：把一组"能判死"的硬指标打印成一张表，返回是否全部通过

硬指标的含义（都是可以明确定义的，不是审美）：
  · 削波    峰值 > -1 dBFS
  · 爆音    相邻采样跳变 > 0.9（≈ -0.9dB 阶跃，听感是"咔"）
  · 死气    末尾挂着大段真空
  · 弧线    段落响度关系是否符合预期（这首歌该是"渐强"还是"渐弱"）
"""
from __future__ import annotations

import numpy as np

from .render import load_wav
from .mix import lufs, peak_db, true_peak, rms_db, active_rms  # noqa: F401  对外复用


def mono(x: np.ndarray) -> np.ndarray:
    return x.mean(axis=1) if x.ndim == 2 else x


def section_rms(a: np.ndarray, sr: int, sections) -> dict[str, float]:
    """sections = [(起点秒, 终点秒, 名字)] → {名字: RMS dB}"""
    m = mono(a)
    out = {}
    for a0, a1, name in sections:
        seg = m[int(a0 * sr):int(a1 * sr)]
        out[name] = rms_db(seg) if len(seg) else -120.0
    return out


def level_curve(a: np.ndarray, sr: int, window: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """逐秒 RMS 曲线，用来肉眼看动态走势。"""
    m = mono(a)
    hop = max(1, int(window * sr))
    n = len(m) // hop
    ts = np.arange(n) * window
    vals = np.array([rms_db(m[i * hop:(i + 1) * hop]) for i in range(n)])
    return ts, vals


def spectral_bands(a: np.ndarray, sr: int, bands=None) -> dict[str, float]:
    """各频段能量占比（%）。用来判断"低频糊了/中频空了"。"""
    bands = bands or [(0, 80, "<80"), (80, 200, "80-200"), (200, 2000, "200-2k"),
                      (2000, 6000, "2k-6k"), (6000, sr // 2, ">6k")]
    m = mono(a)
    N = 4096
    w = np.hanning(N)
    acc = np.zeros(N // 2 + 1)
    for i in range(0, max(1, len(m) - N), N):
        acc += np.abs(np.fft.rfft(m[i:i + N] * w)) ** 2
    f = np.fft.rfftfreq(N, 1 / sr)
    tot = acc.sum() + 1e-20
    return {name: float(acc[(f >= lo) & (f < hi)].sum() / tot * 100)
            for lo, hi, name in bands}


def spectral_centroid(a: np.ndarray, sr: int) -> float:
    m = mono(a)
    N = 4096
    acc = np.zeros(N // 2 + 1)
    w = np.hanning(N)
    for i in range(0, max(1, len(m) - N), N):
        acc += np.abs(np.fft.rfft(m[i:i + N] * w))
    f = np.fft.rfftfreq(N, 1 / sr)
    return float((f * acc).sum() / (acc.sum() + 1e-20))


def clicks(a: np.ndarray, threshold: float = 0.9) -> int:
    """爆音：相邻采样跳变超过阈值。正常波形不会有这种阶跃。"""
    m = mono(a) if a.ndim == 2 else a
    return int((np.abs(np.diff(m)) > threshold).sum())


def silent_tail(a: np.ndarray, sr: int, thresh_db: float = -60.0) -> float:
    """末尾连续静音的秒数。"""
    m = mono(a)
    hop = int(0.1 * sr)
    for i in range(len(m) // hop - 1, -1, -1):
        seg = m[i * hop:(i + 1) * hop]
        if len(seg) and rms_db(seg) > thresh_db:
            return (len(m) - (i + 1) * hop) / sr
    return len(m) / sr


# ── 报表 ──────────────────────────────────────────────────────────────
def report(path: str, sections=None, expect_arc=None, max_duration=None,
           max_silence_tail: float = 3.0, min_peak_db: float = -1.0,
           click_threshold: float = 0.9, verbose: bool = True) -> dict:
    """
    sections   : [(起, 止, 名字)] 用于分段电平
    expect_arc : ["哭泣", "哽咽", "无声的痛楚"]  表示这些段的响度必须严格递减
    """
    a, sr = load_wav(path)
    dur = len(a) / sr
    pk = peak_db(a)
    res = {"path": path, "seconds": dur, "peak_db": pk, "checks": {}}

    def chk(name, ok, detail=""):
        res["checks"][name] = bool(ok)
        if verbose:
            print(f"  {'✅' if ok else '❌'} {name}：{detail}")
        return ok

    if verbose:
        print(f"=== {path} ===")
        print(f"  时长 {dur:.2f}s｜峰值 {pk:.2f} dBFS｜RMS {rms_db(a):.2f} dB"
              f"｜重心 {spectral_centroid(a, sr):.0f} Hz")

    if max_duration:
        chk("时长", dur <= max_duration, f"{dur:.1f}s ≤ {max_duration}s")

    chk("无削波", pk <= min_peak_db, f"峰值 {pk:.2f} ≤ {min_peak_db} dBFS")

    n_clip = clicks(a, click_threshold)
    chk("无爆音", n_clip == 0, f"跳变 >{click_threshold} 的采样数 {n_clip}")

    st = silent_tail(a, sr)
    chk("结尾不挂真空", st <= max_silence_tail, f"静音尾巴 {st:.2f}s ≤ {max_silence_tail}s")

    if sections:
        lv = section_rms(a, sr, sections)
        res["sections"] = lv
        if verbose:
            print("  段落电平：" + "｜".join(f"{k} {v:.1f}dB" for k, v in lv.items()))
        if expect_arc:
            vals = [lv[n] for n in expect_arc if n in lv]
            ok = all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))
            chk("动态弧线", ok, " > ".join(f"{n} {lv[n]:.1f}" for n in expect_arc if n in lv))

    res["passed"] = all(res["checks"].values())
    if verbose:
        print(f"  {'— 全部通过' if res['passed'] else '— 有项目未通过'}")
    return res


def arc_table(a, sr, window: float = 5.0):
    """每 window 秒一个点，打印成一行，方便一眼看走势。"""
    ts, vals = level_curve(a, sr, window)
    print("  " + " ".join(f"{v:4.0f}" for v in vals))
    return ts, vals
