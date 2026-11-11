#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mt.midi —— 零依赖的 SMF（Standard MIDI File）写入与读取

写和读是**两条独立实现**，故意不复用代码：
如果你用同一个解析器验证自己的写入器，它只会证明"我前后一致"，不能证明
"符合规范"。这里的 reader 只服务于质检，写法上刻意和 writer 不同路径。

公开 API：
    Score(ppq=480, beats_per_bar=4, title="")
        .tempo(bar, bpm) / .timesig(bar, num, den) / .marker(bar, text)
        .voice(name, channel=0, program=0) -> Voice
        .write(path) -> path
    Voice
        .note(bar, beat, dur, pitch, vel)      dur 单位是"拍"
        .chord(bar, beat, dur, pitches, vel)
        .grace(bar, beat, pitch, vel)          倚音（主音前 34 tick 的短音）
        .cc(bar, beat, num, val)
        .pitchbend(bar, beat, value)           -8192..8191
    read_midi(path) -> Midi  （.fmt .ppq .tracks .cond .notes(ch?)）

坐标约定：bar / beat 都从 1 开始，可以带小数（1.5 = 第 1 小节的半拍处）。
"""
from __future__ import annotations

import struct

PPQ = 480

# ── 基础编码 ──────────────────────────────────────────────────────────
def vlq(n: int) -> bytes:
    """可变长度数值（MIDI 的 delta-time 编码）。"""
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def meta(kind: int, data: bytes) -> bytes:
    return bytes([0xFF, kind]) + vlq(len(data)) + data


def meta_text(kind: int, text: str) -> bytes:
    return meta(kind, text.encode("utf-8"))


NOTE_BASE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note_number(name: str) -> int:
    """'A4' / 'Bb2' / 'G#3' / 'C5' → MIDI 音号（C4 = 60）。"""
    name = name.strip()
    p = name[0].upper()
    if p not in NOTE_BASE:
        raise ValueError(f"无法解析音名 {name!r}")
    i, acc = 1, 0
    while i < len(name) and name[i] in "#b":
        acc += 1 if name[i] == "#" else -1
        i += 1
    octv = int(name[i:])
    return 12 * (octv + 1) + NOTE_BASE[p] + acc


def pitch_name(p: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[p % 12]}{p // 12 - 1}"


# ── 轨道 ──────────────────────────────────────────────────────────────
class Track:
    """一条 MTrk。事件按 (tick, 插入序) 稳定排序，保证结果可复现。"""

    def __init__(self):
        self.ev: list[tuple[int, int, bytes]] = []

    def add(self, tick: int, data: bytes) -> Track:
        self.ev.append((max(0, int(round(tick))), len(self.ev), data))
        return self

    def ch(self, tick: int, status: int, a: int, b: int) -> Track:
        hi = status & 0xF0
        if hi in (0xC0, 0xD0):
            # 踩过的坑：program change / channel pressure 只有 1 个数据字节。
            # 写成 2 个会让之后整条事件流错位，音符全部跑到别的拍子上。
            raise ValueError(f"0x{hi:02X} 是单数据字节事件，请用 pc()")
        return self.add(tick, bytes([status, a & 0x7F, b & 0x7F]))

    def pc(self, tick: int, channel: int, program: int) -> Track:
        return self.add(tick, bytes([0xC0 | (channel & 0x0F), program & 0x7F]))

    def to_bytes(self) -> bytes:
        body = bytearray()
        last = 0
        for tick, _, data in sorted(self.ev, key=lambda x: (x[0], x[1])):
            body += vlq(tick - last) + data
            last = tick
        body += vlq(0) + meta(0x2F, b"")  # End of Track
        return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


# ── 声部（一个 channel 上的演奏者）────────────────────────────────────
class Voice:
    def __init__(self, score: Score, track: Track, channel: int, program: int, name: str):
        self.score = score
        self.track = track
        self.channel = channel & 0x0F
        self.name = name
        self.gap = 10  # 同音之间留的 tick 缝，避免粘连成一个长音
        self.track.pc(0, self.channel, program)

    def t(self, bar, beat) -> int:
        return self.score.t(bar, beat)

    def note(self, bar, beat, dur, pitch, vel=72, transpose=0) -> int:
        """dur 单位是拍。pitch 可以是音名或 MIDI 音号。"""
        p = pitch if isinstance(pitch, int) else note_number(pitch)
        p = max(0, min(127, p + transpose))
        on = self.t(bar, beat)
        off = on + max(1, int(round(dur * self.score.ppq)) - self.gap)
        self.track.ch(on, 0x90 | self.channel, p, vel)
        self.track.ch(off, 0x80 | self.channel, p, 0)
        return p

    def chord(self, bar, beat, dur, pitches, vel=72) -> list[int]:
        return [self.note(bar, beat, dur, p, vel) for p in pitches]

    def run(self, bar, beat, seq, vel=72):
        """seq = [(beat_offset, dur, pitch), ...] 或 (offset, dur, pitch, vel)"""
        out = []
        for item in seq:
            off, dur, pitch = item[0], item[1], item[2]
            v = item[3] if len(item) > 3 else vel
            out.append(self.note(bar, beat + off, dur, pitch, v))
        return out

    def arpeggio(self, bar, beat, pitches, step=0.5, dur=0.45, vel=72):
        return [self.note(bar, beat + i * step, dur, p, vel) for i, p in enumerate(pitches)]

    def grace(self, bar, beat, pitch, vel=64, lead=34, length=24) -> int:
        """倚音：主音前 lead 个 tick 的一颗短音。"""
        p = pitch if isinstance(pitch, int) else note_number(pitch)
        on = self.t(bar, beat) - lead
        self.track.ch(on, 0x90 | self.channel, p, vel)
        self.track.ch(on + length, 0x80 | self.channel, p, 0)
        return p

    def cc(self, bar, beat, num, val):
        self.track.ch(self.t(bar, beat), 0xB0 | self.channel, num, max(0, min(127, int(val))))

    def pitchbend(self, bar, beat, value):
        v = max(-8192, min(8191, int(value))) + 8192
        self.track.ch(self.t(bar, beat), 0xE0 | self.channel, v & 0x7F, (v >> 7) & 0x7F)


# ── 整首曲子 ──────────────────────────────────────────────────────────
class Score:
    def __init__(self, ppq: int = PPQ, beats_per_bar: int = 4, title: str = "", fmt: int = 1):
        self.ppq = ppq
        self.bpb = beats_per_bar
        self.fmt = fmt
        self.conductor = Track()
        self.tracks: list[Track] = []
        if title:
            self.conductor.add(0, meta_text(0x03, title))
        self.timesig(1, 4, 4)

    # 坐标换算
    def t(self, bar, beat) -> int:
        return int(round(((bar - 1) * self.bpb + (beat - 1)) * self.ppq))

    def bar_tick(self, bar) -> int:
        return self.t(bar, 1)

    # 指挥轨
    def tempo(self, bar, bpm):
        us = int(round(60_000_000 / bpm))
        self.conductor.add(self.t(bar, 1), meta(0x51, us.to_bytes(3, "big")))
        return self

    def timesig(self, bar, num, den):
        self.conductor.add(self.t(bar, 1), meta(0x58, bytes([num, den.bit_length() - 1, 24, 8])))
        return self

    def marker(self, bar, text):
        self.conductor.add(self.t(bar, 1), meta_text(0x06, text))
        return self

    def voice(self, name: str, channel: int = 0, program: int = 0) -> Voice:
        tr = Track()
        self.tracks.append(tr)
        return Voice(self, tr, channel, program, name)

    def write(self, path) -> str:
        head = b"MThd" + struct.pack(">IHHH", 6, self.fmt, len(self.tracks) + 1, self.ppq)
        chunks = [self.conductor.to_bytes()] + [t.to_bytes() for t in self.tracks]
        with open(path, "wb") as f:
            f.write(head + b"".join(chunks))
        return str(path)


# ── 读取（独立实现，供质检用）─────────────────────────────────────────
class Midi:
    def __init__(self, fmt, ntrk, ppq, tracks, cond):
        self.fmt, self.ntrk, self.ppq = fmt, ntrk, ppq
        self.tracks = tracks          # list[list[(tick, hi, ch, a, b)]]
        self.cond = cond              # {"tempo":[(tick,us)], "ts":[...], "marker":[(tick,str)]}

    def notes(self, track=None):
        evs = self.tracks if track is None else [self.tracks[track]]
        out = []
        for ev in evs:
            open_, = ({},)
            for t, hi, ch, p, v in ev:
                if hi == 0x90 and v > 0:
                    open_.setdefault((ch, p), []).append((t, v))
                elif hi == 0x80 or (hi == 0x90 and v == 0):
                    st = open_.get((ch, p))
                    if st:
                        on, vel = st.pop(0)
                        out.append({"ch": ch, "pitch": p, "on": on, "off": t, "vel": vel})
        return sorted(out, key=lambda x: (x["on"], x["pitch"]))

    def cc(self, num=None, track=None):
        evs = self.tracks if track is None else [self.tracks[track]]
        return [(t, ch, a, b) for ev in evs for (t, hi, ch, a, b) in ev
                if hi == 0xB0 and (num is None or a == num)]

    def bpm_map(self):
        return [(t, round(60_000_000 / us, 2)) for t, us in self.cond["tempo"]]

    def duration_sec(self):
        """按速度地图把 tick 换算成秒。"""
        end = max((ev[-1][0] for ev in self.tracks if ev), default=0)
        tempo = self.cond["tempo"] or [(0, 500_000)]
        total, last_t, us = 0.0, 0, tempo[0][1]
        for t, u in sorted(tempo):
            if t > end:
                break
            total += (t - last_t) / self.ppq * (us / 1e6)
            last_t, us = t, u
        total += (end - last_t) / self.ppq * (us / 1e6)
        return total


def _vlq_at(b, i):
    v = 0
    while True:
        c = b[i]
        i += 1
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, i


def read_midi(path) -> Midi:
    raw = open(path, "rb").read()
    if raw[:4] != b"MThd":
        raise ValueError("不是 MThd：文件不是标准 MIDI")
    fmt, ntrk, ppq = struct.unpack(">HHH", raw[8:14])
    pos, tracks = 14, []
    cond = {"tempo": [], "ts": [], "marker": []}
    while pos < len(raw) and raw[pos:pos + 4] == b"MTrk":
        ln = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + ln]
        pos += 8 + ln
        i, t, run, ev = 0, 0, None, []
        while i < len(body):
            dt, i = _vlq_at(body, i)
            t += dt
            st = body[i]
            if st == 0xFF:
                kind = body[i + 1]
                ln2, j = _vlq_at(body, i + 2)
                data = body[j:j + ln2]
                if kind == 0x51:
                    cond["tempo"].append((t, int.from_bytes(data, "big")))
                elif kind == 0x58:
                    cond["ts"].append((t, tuple(data)))
                elif kind == 0x06:
                    cond["marker"].append((t, data.decode("utf-8", "replace")))
                elif kind == 0x2F:
                    break
                i = j + ln2
                continue
            if st & 0x80:
                run = st
            else:
                st = run
                i -= 1
            ch, hi = st & 0x0F, st & 0xF0
            if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                ev.append((t, hi, ch, body[i + 1], body[i + 2]))
                i += 3
            elif hi in (0xC0, 0xD0):
                ev.append((t, hi, ch, body[i + 1], 0))
                i += 2
            else:
                raise ValueError(f"未知状态字节 {st:#04x}（偏移 {i}）")
        tracks.append(ev)
    return Midi(fmt, ntrk, ppq, tracks, cond)


def gm_program(name: str, table: dict) -> int:
    """从名字表里取 GM 音色号，取不到就报错（不要静默用 0 号钢琴）。"""
    if name not in table:
        raise KeyError(f"未知 GM 音色 {name!r}，可选：{', '.join(sorted(table)[:12])}...")
    return table[name]


# 常用 GM 音色（0 起）
GM = {
    "acoustic_grand": 0, "bright_piano": 1, "electric_piano": 4,
    "celesta": 8, "glockenspiel": 9, "music_box": 10, "vibraphone": 11,
    "marimba": 12, "tubular_bells": 14,
    "drawbar_organ": 16, "church_organ": 19,
    "nylon_guitar": 24, "steel_guitar": 25, "jazz_guitar": 26, "clean_guitar": 27,
    "overdrive_guitar": 29, "distortion_guitar": 30,
    "acoustic_bass": 32, "finger_bass": 33, "pick_bass": 34, "fretless_bass": 35,
    "violin": 40, "viola": 41, "cello": 42, "contrabass": 43, "tremolo_strings": 44,
    "pizzicato": 45, "harp": 46, "timpani": 47,
    "string_ensemble": 48, "slow_strings": 51, "choir_aahs": 52, "voice_oohs": 53,
    "orchestra_hit": 55, "trumpet": 56, "trombone": 57, "tuba": 58,
    "french_horn": 60, "brass_section": 61, "soprano_sax": 64, "tenor_sax": 66,
    "oboe": 68, "english_horn": 69, "bassoon": 70, "clarinet": 71,
    "piccolo": 72, "flute": 73, "recorder": 74, "pan_flute": 75,
    "shakuhachi": 77, "whistle": 78, "ocarina": 79,
    "sawtooth_lead": 81, "square_lead": 80, "calliope_lead": 82,
    "pad_warm": 89, "pad_poly": 90, "pad_choir": 91, "pad_bowed": 92,
    "koto": 107, "kalimba": 108, "shanai": 111,
    "taiko": 116, "melodic_tom": 117, "synth_drum": 118,
}
