#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mt 的最小自检：不依赖 fluidsynth / ffmpeg，纯 Python 逻辑。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mt import Score, read_midi, note_number, pitch_name


def test_note_number():
    assert note_number("C4") == 60
    assert note_number("A4") == 69
    assert note_number("C-1") == 0
    assert note_number("Bb3") == 58
    assert note_number("G#3") == 56
    assert note_number("C#4") == 61
    assert pitch_name(60) == "C4"
    assert pitch_name(69) == "A4"
    assert pitch_name(note_number("Bb3")) == "A#3"


def test_roundtrip():
    """写入 → 独立读回 → 音符必须一模一样。"""
    s = Score(title="roundtrip", beats_per_bar=4)
    s.tempo(1, 120).marker(1, "A")
    v = s.voice("piano", channel=0, program=0)
    v.chord(1, 1, 2, ["A4", "C5", "E5"], vel=64)
    v.note(2, 1, 4, "E4", vel=50)
    path = os.path.join(tempfile.mkdtemp(), "t.mid")
    s.write(path)

    m = read_midi(path)
    ns = m.notes()
    assert len(ns) == 4, f"应有 4 个音，读到 {len(ns)}"
    assert [n["pitch"] for n in ns] == [69, 72, 76, 64]
    # 时值：2 拍 = 2*480 = 960 tick，减去 10 tick 的缝
    chord_len = ns[0]["off"] - ns[0]["on"]
    assert chord_len == 960 - 10, f"和弦时值 {chord_len}"
    # 速度
    assert m.bpm_map() == [(0, 120.0)], m.bpm_map()
    assert m.cond["marker"][0][1] == "A"


def test_pc_guard():
    """program change 只有 1 个数据字节。写错会让整条事件流错位。"""
    s = Score()
    v = s.voice("x", channel=0, program=5)
    tr = v.track
    try:
        tr.ch(0, 0xC0, 5, 0)  # 错误调用
    except ValueError:
        pass
    else:
        raise AssertionError("ch() 应该拒绝单数据字节事件")
    # pc() 是对的路径
    path = os.path.join(tempfile.mkdtemp(), "pc.mid")
    v.note(1, 1, 1, "C4", vel=80)
    s.write(path)
    m = read_midi(path)
    pcs = [(t, ch, a) for (t, hi, ch, a, b) in m.tracks[1] if hi == 0xC0]
    assert pcs and pcs[0][2] == 5, pcs


def test_tempo_map_and_duration():
    s = Score()
    s.tempo(1, 120)
    s.tempo(5, 60)  # 第 5 小节起降速
    v = s.voice("x", program=0)
    v.note(1, 1, 4, "C4", 80)   # 第 1 小节，4 拍 @120 = 2s
    v.note(5, 1, 4, "C4", 80)   # 第 5 小节，4 拍 @60  = 4s
    path = os.path.join(tempfile.mkdtemp(), "tempo.mid")
    s.write(path)
    m = read_midi(path)
    # 前 4 小节(16 拍)@120 = 8s；第 5 小节的 4 拍@60 = 4s；合计约 12s。
    # 断言按这个算出来（不是拍脑袋），顺便验证速度地图被真的应用了。
    dur = m.duration_sec()
    assert 11.5 < dur < 12.5, f"总时长 {dur}（期望约 12s）"


def test_verify_range():
    from mt.verify import Checker
    s = Score()
    s.tempo(1, 100)
    bad = s.voice("bass", program=32)
    bad.note(1, 1, 2, "C1", 70)   # 低于贝斯音域
    path = os.path.join(tempfile.mkdtemp(), "r.mid")
    s.write(path)
    c = Checker(path)
    c.range_check({"bass": (28, 55)})
    assert not c.report(verbose=False), "应该报出音域越界"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
            n += 1
    print(f"\n{n} 项自检全部通过")
