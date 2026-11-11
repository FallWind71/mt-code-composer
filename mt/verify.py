#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mt.verify —— 对 MIDI 本体做乐理层面的断言（还没渲染就能查）

存在的理由：渲染很慢、听起来很主观，而"这个音超出乐器音域了""这条旋律
和和弦打架了""同音粘连了"这些事**在 MIDI 数据里就能判死**。

用法：

    from mt.verify import Checker
    c = Checker("a.mid")
    c.range_check({"cello": (36, 76)})          # 按乐器名限音域
    c.no_overlap()
    c.harmony(PROGRESSION, tonic="F")           # 每小节和弦是否落在一起
    c.velocity(lo=20, hi=100)
    assert c.report()
"""
from __future__ import annotations

from collections import defaultdict

from .midi import read_midi, pitch_name

# 常见乐器音域（MIDI 音号，含常用扩展）
RANGES = {
    "piano": (21, 108), "violin": (55, 103), "viola": (48, 91), "cello": (36, 76),
    "contrabass": (28, 67), "flute": (60, 96), "piccolo": (74, 108),
    "oboe": (58, 91), "clarinet": (50, 94), "bassoon": (34, 75),
    "trumpet": (55, 82), "trombone": (40, 72), "tuba": (28, 58),
    "french_horn": (34, 77), "soprano_sax": (56, 87), "tenor_sax": (44, 76),
    "guitar": (40, 88), "bass_guitar": (28, 55), "harp": (24, 103),
    "timpani": (41, 57), "marimba": (45, 96), "glockenspiel": (79, 108),
    "vibraphone": (53, 89), "koto": (48, 84), "shakuhachi": (55, 88),
}

# 和弦 → 音级（C=0）
CHORD_TONES = {
    "C": {0, 4, 7}, "Cm": {0, 3, 7}, "C7": {0, 4, 7, 10},
    "D": {2, 6, 9}, "Dm": {2, 5, 9}, "D7": {2, 6, 9, 0},
    "E": {4, 8, 11}, "Em": {4, 7, 11}, "E7": {4, 8, 11, 2},
    "F": {5, 9, 0}, "Fm": {5, 8, 0}, "F7": {5, 9, 0, 3},
    "G": {7, 11, 2}, "Gm": {7, 10, 2}, "G7": {7, 11, 2, 5},
    "A": {9, 1, 4}, "Am": {9, 0, 4}, "A7": {9, 1, 4, 7},
    "B": {11, 3, 6}, "Bm": {11, 2, 6}, "Bb": {10, 2, 5}, "Bbm": {10, 1, 5},
    "Eb": {3, 7, 10}, "Ebm": {3, 6, 10}, "Ab": {8, 0, 3}, "Abm": {8, 11, 3},
    "Db": {1, 5, 8}, "Dbm": {1, 4, 8}, "Gb": {6, 10, 1}, "Gbm": {6, 9, 1},
}

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class Checker:
    def __init__(self, midi_path: str, ppq: int | None = None, beats_per_bar: int = 4):
        self.m = read_midi(midi_path)
        self.ppq = ppq or self.m.ppq
        self.bpb = beats_per_bar
        self.results: list[tuple[str, bool, str]] = []

    # ── 工具 ──
    def bar_of(self, tick: int) -> int:
        return tick // (self.ppq * self.bpb) + 1

    def _chk(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        return ok

    # ── 检查项 ──
    def range_check(self, limits: dict[str, tuple[int, int]]):
        """limits = {乐器名: (最低音号, 最高音号)}，按"有音符的轨道"顺序对应。

        指挥轨（tempo/marker）是空的，会被自动跳过，不会错位。
        """
        # 只取真的有音符的轨道，按出现顺序
        voiced = [ti for ti in range(len(self.m.tracks)) if self.m.notes(track=ti)]
        for name, ti in zip(limits, voiced):
            lo, hi = limits[name]
            ns = self.m.notes(track=ti)
            actual = (min(n["pitch"] for n in ns), max(n["pitch"] for n in ns))
            bad = [n for n in ns if not (lo <= n["pitch"] <= hi)]
            detail = (f"{name} {actual[0]}({pitch_name(actual[0])}) .. "
                      f"{actual[1]}({pitch_name(actual[1])})，限 {lo}..{hi}")
            if bad:
                detail += f"｜超界 {len(bad)} 个音，最差 {pitch_name(min(b['pitch'] for b in bad))}"
            self._chk(f"音域[{name}]", not bad, detail)
        return self

    def no_overlap(self):
        """同一个音高不许后音在前音结束前开始（会变成两音叠、听感像卡住）。"""
        bad = 0
        worst = ""
        for ti in range(len(self.m.tracks)):
            ns = self.m.notes(track=ti)
            by_pitch = defaultdict(list)
            for n in ns:
                by_pitch[n["pitch"]].append(n)
            for p, lst in by_pitch.items():
                lst.sort(key=lambda x: x["on"])
                for a, b in zip(lst, lst[1:]):
                    if b["on"] < a["off"]:
                        bad += 1
                        worst = f"轨道{ti} {pitch_name(p)} @bar{self.bar_of(b['on'])}"
        self._chk("无同音粘连", bad == 0, f"重叠 {bad} 处{('，例：' + worst) if worst else ''}")
        return self

    def harmony(self, progression, tonic: str = "C", tolerance: float = 0.5):
        """
        progression = {小节号: "和弦名"}。检查每小节里被使用的音级是否落在和弦内。
        tolerance 允许的比例（0.5 = 半数以上音要和弦内）。
        """
        idx = NOTE_NAMES.index(tonic)
        per_bar = defaultdict(set)
        for ti in range(len(self.m.tracks)):
            for n in self.m.notes(track=ti):
                bar = self.bar_of(n["on"])
                per_bar[bar].add((n["pitch"] - idx) % 12)
        bad = []
        for bar, ch in progression.items():
            tones = CHORD_TONES.get(ch)
            if not tones or bar not in per_bar:
                continue
            used = per_bar[bar]
            hit = len(used & tones)
            if hit / max(len(used), 1) < tolerance:
                bad.append(f"bar{bar} {ch} 命中 {hit}/{len(used)}"
                           f"（用了 {' '.join(NOTE_NAMES[p] for p in sorted(used))}）")
        self._chk("和声一致", not bad,
                  f"{len(progression)} 小节检查，{len(bad)} 处不符"
                  + ("｜" + "；".join(bad[:3]) if bad else ""))
        return self

    def velocity(self, lo: int = 1, hi: int = 127):
        bad = 0
        vmin, vmax = 127, 0
        for ti in range(len(self.m.tracks)):
            for n in self.m.notes(track=ti):
                vmin, vmax = min(vmin, n["vel"]), max(vmax, n["vel"])
                if not (lo <= n["vel"] <= hi):
                    bad += 1
        self._chk("力度范围", bad == 0, f"实测 {vmin}..{vmax}，限 {lo}..{hi}"
                  + (f"，越界 {bad}" if bad else ""))
        return self

    def has_tempo(self):
        ok = len(self.m.cond["tempo"]) > 0
        self._chk("有速度设置", ok, f"{len(self.m.cond['tempo'])} 处速度变更")
        return self

    def markers_in_order(self):
        mk = self.m.cond["marker"]
        ok = all(mk[i][0] <= mk[i + 1][0] for i in range(len(mk) - 1))
        self._chk("段落标记有序", ok, f"{len(mk)} 个标记")
        return self

    # ── 汇总 ──
    def report(self, verbose: bool = True) -> bool:
        if verbose:
            print(f"=== MIDI 断言：{len(self.results)} 项 ===")
            for name, ok, detail in self.results:
                print(f"  {'✅' if ok else '❌'} {name}：{detail}")
        ok = all(r[1] for r in self.results)
        if verbose:
            print(f"  {'— 通过' if ok else '— 未通过'}")
        return ok
