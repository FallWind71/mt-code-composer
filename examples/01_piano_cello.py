#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
示例：钢琴 + 大提琴，12 小节，走完完整流程

    compose → verify（乐理断言）→ render（音源）→ mix（混音）→ qa（质检）

直接运行：
    cd examples && python3 01_piano_cello.py

三件在实际作品里真的救命的事：
  1. **一轨一个 MIDI 文件**。混音需要独立控制每个声部，一个文件里塞所有乐器
     就没法分轨了。速度/段落标记在每个分轨里重复一份，渲染时才能对齐。
  2. 用 CC11 写"呼吸"，而不是只靠力度 —— 长音才有起伏。
  3. 逐轨实测 RMS 定增益，渲染完用数字验收，而不是"感觉还行"。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mt import Mixer, Score, render_stems, report
from mt.midi import GM
from mt.verify import Checker

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
STEMS = os.path.join(OUT, "stems")
os.makedirs(STEMS, exist_ok=True)

BPM = 66
BARS = 12

# A 小调。需要"不给情绪定性"的地方用空五度（去掉三音）。
PROG = {1: "Am", 2: "Am", 3: "F", 4: "F", 5: "C", 6: "G",
        7: "Am", 8: "Am", 9: "F", 10: "G", 11: "Am", 12: "Am"}
# 和弦声部**不含根音**（根音交给大提琴），也不含旋律音：
# 否则同一个音高会被两个声部同时按住 —— 检查器会抓"同音粘连"。
CHORDS = {"Am": ["E3", "A3", "C4"], "F": ["C3", "F3", "A3"],
          "C": ["G3", "C4", "E4"], "G": ["D3", "G3", "B3"]}
# 最后一小节旋律落在 A3，钢琴就不给小字组 A，避免撞音
CHORDS_LAST = ["E3", "C4"]
BASS = {"Am": "A2", "F": "F2", "C": "C3", "G": "G2"}

MELODY = [
    (1, 1, 2, "E5"), (1, 3, 1, "D5"), (1, 4, 1, "C5"),
    (2, 1, 3, "A4"),
    (3, 1, 2, "C5"), (3, 3, 1, "A4"), (3, 4, 1, "G4"),
    (4, 1, 3, "F4"),
    (5, 1, 2, "E5"), (5, 3, 2, "G5"),
    (6, 1, 2, "D5"), (6, 3, 2, "B4"),
    (7, 1, 4, "A4"),
    (8, 1, 2, "E4"), (8, 3, 2, "A4"),
    (9, 1, 2, "C5"), (9, 3, 2, "A4"),
    (10, 1, 2, "B4"), (10, 3, 2, "D5"),
    (11, 1, 4, "A4"),
    (12, 1, 4, "A3"),
]


def _new_score(title):
    """每个分轨都要带一份速度和段落标记，渲染出来才对得上。"""
    s = Score(title=title)
    s.tempo(1, BPM).marker(1, "A")
    return s


def compose():
    """写出两个分轨 MIDI，返回 {名字: 路径}。"""
    paths = {}

    # ── 大提琴：整小节长音 + CC11 呼吸 ──
    s = _new_score("cello")
    cello = s.voice("cello", channel=1, program=GM["cello"])
    for bar in range(1, BARS + 1):
        ch = PROG[bar]
        cello.note(bar, 1, 3.9, BASS[ch], vel=52 if bar < 7 else 60)
        cello.cc(bar, 1.0, 11, 46)   # 起：收着
        cello.cc(bar, 2.0, 11, 78)   # 中：顶住
        cello.cc(bar, 3.5, 11, 50)   # 收：松开
    paths["cello"] = s.write(os.path.join(STEMS, "cello.mid"))

    # ── 钢琴：和弦铺底 + 旋律 ──
    s = _new_score("piano")
    piano = s.voice("piano", channel=0, program=GM["acoustic_grand"])
    for bar in range(1, BARS + 1):
        tones = CHORDS_LAST if bar == BARS else CHORDS[PROG[bar]]
        vel = 46 if bar < 5 else (54 if bar < 9 else 60)
        if bar < 5:
            piano.chord(bar, 1, 3.6, tones, vel=vel)      # 前半：整小节按住
        else:
            # 后半：切成两半，第二下只弹高两个音 —— 不能和按住的音同高
            piano.chord(bar, 1, 1.4, tones, vel=vel)
            piano.chord(bar, 2.5, 1.4, tones[-2:], vel=vel - 8)
    for bar, beat, dur, pitch in MELODY:
        piano.note(bar, beat, dur * 0.95, pitch, vel=68 if bar < 9 else 74)
    paths["piano"] = s.write(os.path.join(STEMS, "piano.mid"))

    return paths


def main():
    print("① 作曲（一轨一个 MIDI）")
    mids = compose()
    for k, v in mids.items():
        print(f"   {k:6s} → {os.path.relpath(v, HERE)}")

    print("\n② 乐理断言（渲染前就能判死）")
    ok_midi = True
    for name, path in mids.items():
        print(f"  [{name}]")
        c = Checker(path)
        c.has_tempo().markers_in_order().no_overlap().velocity(20, 100)
        ok_midi &= c.report()

    print("\n③ 渲染分轨")
    wavs = render_stems(mids, STEMS)
    print(f"   → {', '.join(os.path.basename(w) for w in wavs.values())}")

    print("\n④ 混音")
    m = Mixer()
    m.add("piano", wavs["piano"], target_db=-26, pan=0.06, send=0.30, hp=70)
    m.add("cello", wavs["cello"], target_db=-28, pan=-0.10, send=0.36, hp=55, lp=6000)
    for name, (rms, g) in m.load().items():
        print(f"   {name:6s} 实测 {rms:7.1f} dB → 增益 {g:+6.1f} dB")
    wav = os.path.join(OUT, "piano_cello.wav")
    m.mix(wav, reverb_seconds=2.4, target_lufs=-16.0)

    print("\n⑤ 质检")
    total = (BARS * 4) * 60 / BPM
    res = report(wav, sections=[(0, total / 2, "前半"), (total / 2, total, "后半")],
                 max_duration=90)

    if os.environ.get("MT_MP3", "1") == "1":
        from mt.render import to_mp3
        print(f"   → {os.path.basename(to_mp3(wav))}")

    print(f"\n{'✅ 全部通过' if (ok_midi and res['passed']) else '❌ 有项目未通过'}")
    return 0 if (ok_midi and res["passed"]) else 1


if __name__ == "__main__":
    sys.exit(main())
