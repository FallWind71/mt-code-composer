#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mt.cli —— 命令行入口

    mt render a.mid a.wav [--sf 音色库.sf3] [--gain 0.85]
    mt qa a.wav [--sections "0:54:哽咽,54:116:哭泣,116:179:无声"]
    mt verify a.mid
    mt info a.wav
    mt mix spec.json out.wav

spec.json 形如：
{
  "sr": 44100,
  "soundfont": "path/to.sf3",
  "stems": {
    "piano": {"midi": "stems/piano.mid", "target_db": -29, "pan": 0.05, "send": 0.26, "hp": 70},
    "bass":  {"midi": "stems/bass.mid",  "target_db": -33, "pan": 0.0,  "send": 0.05, "hp": 62}
  },
  "reverb_seconds": 1.6,
  "target_lufs": -16,
  "sections": [[1, 0.95], [13, 1.0]],
  "total_bars": 56
}
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def cmd_render(a):
    from .render import render_midi
    render_midi(a.midi, a.wav, soundfont=a.soundfont, gain=a.gain)
    print(f"✅ {a.wav}")
    return 0


def _parse_sections(s):
    """"0:54:哽咽,54:116:哭泣" → [(0,54,'哽咽'), ...]"""
    if not s:
        return None
    out = []
    for part in s.split(","):
        bits = part.split(":")
        if len(bits) != 3:
            raise SystemExit(f"段落格式应为 起点:终点:名字，收到 {part!r}")
        out.append((float(bits[0]), float(bits[1]), bits[2]))
    return out


def cmd_qa(a):
    from .analyze import report
    res = report(a.wav, sections=_parse_sections(a.sections),
                 max_duration=a.max_duration,
                 expect_arc=a.arc.split(",") if a.arc else None)
    return 0 if res["passed"] else 1


def cmd_info(a):
    from .analyze import spectral_bands, spectral_centroid, silent_tail
    from .render import load_wav
    from .mix import lufs, true_peak, peak_db, rms_db
    x, sr = load_wav(a.wav)
    print(f"{a.wav}")
    print(f"  时长 {len(x)/sr:.2f}s｜采样率 {sr}")
    print(f"  {lufs(a.wav):.1f} LUFS｜真峰 {true_peak(a.wav):.1f} dBTP"
          f"｜峰值 {peak_db(x):.1f} dBFS｜RMS {rms_db(x):.1f} dB")
    print(f"  频谱重心 {spectral_centroid(x, sr):.0f} Hz｜静音尾巴 {silent_tail(x, sr):.2f}s")
    print("  频段占比：" + "｜".join(f"{k} {v:.1f}%" for k, v in spectral_bands(x, sr).items()))
    return 0


def cmd_verify(a):
    from .verify import Checker
    c = Checker(a.midi, beats_per_bar=a.beats_per_bar)
    c.has_tempo().markers_in_order().no_overlap().velocity()
    return 0 if c.report() else 1


def cmd_mix(a):
    from .mix import Mixer
    from .render import render_stems, to_mp3
    spec = json.load(open(a.spec, encoding="utf-8"))
    base = os.path.dirname(os.path.abspath(a.spec))
    sr = spec.get("sr", 44100)
    sf = spec.get("soundfont")

    # 1) 先把 spec 里声明的 midi 渲染成 stem
    mids = {n: os.path.join(base, c["midi"]) for n, c in spec["stems"].items() if "midi" in c}
    if mids:
        print("渲染分轨…")
        render_stems(mids, os.path.join(base, "stems"), soundfont=sf, sr=sr)

    # 2) 混音
    m = Mixer(sr=sr)
    for name, c in spec["stems"].items():
        wav = c.get("wav") or os.path.join(base, "stems", name + ".wav")
        m.add(name, wav, target_db=c.get("target_db", -30), pan=c.get("pan", 0.0),
              send=c.get("send", 0.15), hp=c.get("hp", 60), lp=c.get("lp", 16000),
              mono=c.get("mono", True), gain_db=c.get("gain_db", 0.0))
    print("读取分轨…")
    for name, (rms, g) in m.load().items():
        print(f"  {name:14s} 实测 {rms:7.1f} dB → 增益 {g:+6.1f} dB")

    print("混音…")
    info = m.mix(a.out, reverb_seconds=spec.get("reverb_seconds", 2.0),
                 reverb_wet=spec.get("reverb_wet", 0.55),
                 sections=[tuple(s) for s in spec["sections"]] if spec.get("sections") else None,
                 total_bars=spec.get("total_bars", 0),
                 compress_ratio=spec.get("compress_ratio"),
                 compress_thresh=spec.get("compress_thresh", -18.0),
                 target_lufs=spec.get("target_lufs", -16.0))
    if a.mp3 and os.path.exists(a.out):
        to_mp3(a.out)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="mt", description="代码作曲工具链（MIDI → 渲染 → 混音 → 质检）")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="MIDI → wav（fluidsynth）")
    r.add_argument("midi"); r.add_argument("wav")
    r.add_argument("--sf", dest="soundfont"); r.add_argument("--gain", type=float, default=0.85)
    r.set_defaults(func=cmd_render)

    q = sub.add_parser("qa", help="音频客观质检")
    q.add_argument("wav")
    q.add_argument("--sections", help="\"0:54:哽咽,54:116:哭泣\"")
    q.add_argument("--arc", help="要求按此顺序递减的段名，逗号分隔")
    q.add_argument("--max-duration", dest="max_duration", type=float)
    q.set_defaults(func=cmd_qa)

    i = sub.add_parser("info", help="音频概览（响度/频谱/尾巴）")
    i.add_argument("wav"); i.set_defaults(func=cmd_info)

    v = sub.add_parser("verify", help="MIDI 乐理断言")
    v.add_argument("midi"); v.add_argument("--beats-per-bar", dest="beats_per_bar", type=int, default=4)
    v.set_defaults(func=cmd_verify)

    m = sub.add_parser("mix", help="按 spec.json 渲染分轨并混音")
    m.add_argument("spec"); m.add_argument("out"); m.add_argument("--mp3", action="store_true")
    m.set_defaults(func=cmd_mix)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
