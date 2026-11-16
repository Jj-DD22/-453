from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


import os

COSYVOICE_ROOT = Path(
    os.environ.get("V17_COSYVOICE_ROOT", os.path.expanduser("~/.openclaw/CosyVoice"))
)
MODEL_DIR = COSYVOICE_ROOT / "pretrained_models/CosyVoice-300M"


def _sentences(text: str) -> list[str]:
    parts = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?;；])", text.strip())
        if part.strip()
    ]
    return parts or ([text.strip()] if text.strip() else [])


def _groups(sentences: list[str], max_chars: int) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for sentence in sentences:
        sentence_chars = len(re.sub(r"\s", "", sentence))
        if current and current_chars + sentence_chars > max_chars:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(sentence)
        current_chars += sentence_chars
    if current:
        groups.append(current)
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description="燃量桌面参考女声快速合成")
    parser.add_argument("--text", required=True)
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeline-output", type=Path)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--chunk-chars", type=int, default=90)
    args = parser.parse_args()

    if not args.prompt.is_file():
        raise FileNotFoundError(f"参考音频不存在：{args.prompt}")
    sentences = _sentences(args.text)
    if not sentences:
        raise ValueError("配音文案为空")

    sys.path.insert(0, str(COSYVOICE_ROOT))
    sys.path.insert(0, str(COSYVOICE_ROOT / "third_party/Matcha-TTS"))
    import soundfile as sf
    import torch
    import torchaudio

    def _soundfile_load(uri, *args, **kwargs):
        data, sr = sf.read(str(uri), dtype="float32", always_2d=True)
        return torch.from_numpy(data.T.copy()), int(sr)

    def _soundfile_save(uri, src, sample_rate, *args, **kwargs):
        arr = src.detach().cpu().to(torch.float32).numpy()
        if arr.ndim == 2:
            arr = arr.T
        sf.write(str(uri), arr, int(sample_rate))

    torchaudio.load = _soundfile_load
    torchaudio.save = _soundfile_save
    from cosyvoice.cli.cosyvoice import AutoModel

    def trim_and_fade(waveform: torch.Tensor, sample_rate: int) -> torch.Tensor:
        if waveform.numel() < sample_rate // 5:
            return waveform
        mono = waveform.abs().mean(dim=0)
        frame_size = max(1, int(sample_rate * 0.01))
        usable = mono[: mono.numel() - (mono.numel() % frame_size)]
        if not usable.numel():
            return waveform
        envelope = usable.reshape(-1, frame_size).mean(dim=1)
        threshold = max(0.0008, float(envelope.max()) * 0.018)
        active = torch.nonzero(envelope >= threshold).flatten()
        if not active.numel():
            return waveform
        padding = int(sample_rate * 0.012)
        start = max(0, int(active[0]) * frame_size - padding)
        end = min(
            waveform.shape[1],
            (int(active[-1]) + 1) * frame_size + padding,
        )
        trimmed = waveform[:, start:end].clone()
        fade_samples = min(int(sample_rate * 0.008), trimmed.shape[1] // 4)
        if fade_samples > 1:
            fade = torch.linspace(0.0, 1.0, fade_samples, device=trimmed.device)
            trimmed[:, :fade_samples] *= fade
            trimmed[:, -fade_samples:] *= fade.flip(0)
        return trimmed

    model = AutoModel(model_dir=str(MODEL_DIR))
    groups = _groups(sentences, max(36, min(180, args.chunk_chars)))
    chunks = []
    timeline = []
    cursor_samples = 0
    pause = torch.zeros(1, int(model.sample_rate * 0.02))

    total_groups = len(groups)
    print(f"COSYVOICE_PROGRESS 0/{total_groups} 正在加载燃量配音模型", flush=True)
    for group_index, group in enumerate(groups):
        print(
            f"COSYVOICE_PROGRESS {group_index + 1}/{total_groups} "
            f"正在合成第 {group_index + 1} 段配音",
            flush=True,
        )
        group_text = "".join(group)
        tagged = group_text if group_text.startswith("<|zh|>") else "<|zh|>" + group_text
        generated = [
            item["tts_speech"]
            for item in model.inference_cross_lingual(
                tagged,
                str(args.prompt),
                stream=False,
                speed=args.speed,
            )
        ]
        if not generated:
            continue
        group_audio = trim_and_fade(torch.cat(generated, dim=1), model.sample_rate)
        if chunks and group_index:
            chunks.append(pause)
            cursor_samples += pause.shape[1]
        group_start = cursor_samples
        chunks.append(group_audio)
        cursor_samples += group_audio.shape[1]

        weights = [
            max(1, len(re.sub(r"[\s，。！？!?;；、]", "", sentence)))
            for sentence in group
        ]
        total_weight = sum(weights)
        assigned = 0
        for sentence_index, (sentence, weight) in enumerate(zip(group, weights)):
            start_sample = group_start + assigned
            if sentence_index == len(group) - 1:
                end_sample = cursor_samples
            else:
                assigned += round(group_audio.shape[1] * weight / total_weight)
                end_sample = min(cursor_samples, group_start + assigned)
            timeline.append(
                {
                    "text": sentence,
                    "start": round(start_sample / model.sample_rate, 6),
                    "end": round(end_sample / model.sample_rate, 6),
                    "duration": round((end_sample - start_sample) / model.sample_rate, 6),
                }
            )
        print(
            f"COSYVOICE_PROGRESS {group_index + 1}/{total_groups} "
            f"第 {group_index + 1} 段配音完成",
            flush=True,
        )

    if not chunks:
        raise RuntimeError("CosyVoice 没有生成音频")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(args.output), torch.cat(chunks, dim=1), model.sample_rate)
    if args.timeline_output:
        args.timeline_output.parent.mkdir(parents=True, exist_ok=True)
        args.timeline_output.write_text(
            json.dumps(
                {
                    "sample_rate": model.sample_rate,
                    "inference_groups": len(groups),
                    "phrases": timeline,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
