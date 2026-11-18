"""Small, dependency-light subtitle ASR adapter used by the portable build.

The desktop package only needs the public Bcut/B interface that V16 already
uses through VideoCaptioner.  Keeping this adapter local avoids importing the
whole VideoCaptioner GUI/model dependency tree on Windows while preserving the
same service and SRT output behaviour.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import requests


API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"
API_REQ_UPLOAD = f"{API_BASE_URL}/resource/create"
API_COMMIT_UPLOAD = f"{API_BASE_URL}/resource/create/complete"
API_CREATE_TASK = f"{API_BASE_URL}/task"
API_QUERY_RESULT = f"{API_BASE_URL}/task/result"

_HEADERS = {
    "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
    "Content-Type": "application/json",
}


class SubtitleServiceUnavailable(RuntimeError):
    """The remote subtitle service rejected work that should not be retried."""


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: float,
    retries: int = 3,
    **kwargs: Any,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            if getattr(response, "status_code", None) == 412:
                raise SubtitleServiceUnavailable(
                    "字幕云服务暂时拒绝请求（HTTP 412）"
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("字幕接口返回了非对象数据")
            code = payload.get("code", 0)
            if code not in (0, "0", None):
                message = payload.get("message") or payload.get("msg") or code
                raise RuntimeError(f"字幕接口拒绝请求：{message}")
            return payload
        except SubtitleServiceUnavailable:
            # HTTP 412 is a provider-side risk-control response. Retrying it in
            # a tight loop only makes the block last longer, so let the caller
            # switch to the local no-subtitle fallback immediately.
            raise
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < max(1, retries):
                time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"字幕接口请求失败：{last_error}")


def _payload_data(payload: dict[str, Any], stage: str) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"字幕接口在{stage}阶段没有返回有效数据")
    return data


def _srt_time(milliseconds: int | float) -> str:
    total_ms = max(0, int(round(float(milliseconds))))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _write_srt(result: dict[str, Any], output_path: str | Path) -> Path:
    utterances = result.get("utterances")
    if not isinstance(utterances, list):
        raise RuntimeError("字幕接口没有返回可用的 utterances")
    rows: list[str] = []
    sequence = 0
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        text = " ".join(str(utterance.get("transcript") or "").split()).strip()
        if not text:
            continue
        start = utterance.get("start_time", 0)
        end = utterance.get("end_time", start)
        try:
            start_ms = max(0, int(float(start)))
            end_ms = max(start_ms + 1, int(float(end)))
        except (TypeError, ValueError):
            continue
        sequence += 1
        rows.extend(
            [
                str(sequence),
                f"{_srt_time(start_ms)} --> {_srt_time(end_ms)}",
                text,
                "",
            ]
        )
    if not rows:
        raise RuntimeError("字幕接口返回了空字幕")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(rows), encoding="utf-8")
    return destination


def local_whisper_to_srt(wav_path: str | Path, output_path: str | Path, model_dir: str | Path) -> Path:
    """Run the installed Chinese-capable Whisper model entirely on the local CPU."""
    import wave
    import numpy as np
    from faster_whisper import WhisperModel

    root = Path(model_dir).resolve()
    if not all((root / name).is_file() for name in ("model.bin", "tokenizer.json", "config.json")):
        raise FileNotFoundError(f"本机 Whisper 模型不完整：{root}")
    model = WhisperModel(str(root), device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
    with wave.open(str(wav_path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("本机 Whisper 需要单声道 16 kHz 的 PCM 音频。")
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    segments, _ = model.transcribe(samples, language="zh", beam_size=3, vad_filter=True)
    utterances = [{"start_time": round(s.start * 1000), "end_time": round(s.end * 1000),
                   "transcript": s.text} for s in segments]
    return _write_srt({"utterances": utterances}, output_path)


def transcribe_to_srt(
    audio_path: str | Path,
    output_path: str | Path,
    *,
    timeout: float = 480.0,
    request_timeout: float = 35.0,
    poll_interval: float = 1.0,
    session: requests.Session | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Path:
    """Transcribe an MP3 with the Bcut interface and save an SRT file."""
    source = Path(audio_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise FileNotFoundError(f"待识别音频不存在或为空：{source}")
    audio = source.read_bytes()
    client = session or requests.Session()
    deadline = clock() + max(30.0, float(timeout))

    upload_payload = _request_json(
        client,
        "POST",
        API_REQ_UPLOAD,
        timeout=request_timeout,
        data=json.dumps(
            {
                "type": 2,
                "name": "audio.mp3",
                "size": len(audio),
                "ResourceFileType": "mp3",
                "model_id": "8",
            }
        ),
        headers=_HEADERS,
    )
    upload = _payload_data(upload_payload, "创建上传")
    upload_urls = upload.get("upload_urls")
    part_size = int(upload.get("per_size") or 0)
    if not isinstance(upload_urls, list) or not upload_urls or part_size <= 0:
        raise RuntimeError("字幕接口没有返回有效的分片上传地址")

    etags: list[str] = []
    for index, upload_url in enumerate(upload_urls):
        if clock() >= deadline:
            raise TimeoutError("字幕上传超时")
        start = index * part_size
        response = client.put(
            str(upload_url),
            data=audio[start : start + part_size],
            headers=_HEADERS,
            timeout=request_timeout,
        )
        response.raise_for_status()
        etag = response.headers.get("Etag") or response.headers.get("ETag")
        if etag:
            etags.append(etag)

    commit_payload = _request_json(
        client,
        "POST",
        API_COMMIT_UPLOAD,
        timeout=request_timeout,
        data=json.dumps(
            {
                "InBossKey": upload.get("in_boss_key"),
                "ResourceId": upload.get("resource_id"),
                "Etags": ",".join(etags),
                "UploadId": upload.get("upload_id"),
                "model_id": "8",
            }
        ),
        headers=_HEADERS,
    )
    download_url = _payload_data(commit_payload, "完成上传").get("download_url")
    if not download_url:
        raise RuntimeError("字幕接口没有返回音频下载地址")

    task_payload = _request_json(
        client,
        "POST",
        API_CREATE_TASK,
        timeout=request_timeout,
        json={"resource": download_url, "model_id": "8"},
        headers=_HEADERS,
    )
    task_id = _payload_data(task_payload, "创建任务").get("task_id")
    if not task_id:
        raise RuntimeError("字幕接口没有返回任务 ID")

    while clock() < deadline:
        query_payload = _request_json(
            client,
            "GET",
            API_QUERY_RESULT,
            timeout=request_timeout,
            params={"model_id": 7, "task_id": task_id},
            headers=_HEADERS,
        )
        task = _payload_data(query_payload, "查询结果")
        state = int(task.get("state") or 0)
        if state == 4:
            result = task.get("result")
            if isinstance(result, str):
                result = json.loads(result)
            if not isinstance(result, dict):
                raise RuntimeError("字幕接口完成任务但没有返回字幕结果")
            return _write_srt(result, output_path)
        # The Bcut service uses state 3 while recognition is still in progress;
        # only explicit negative/terminal error states may stop polling.
        if state < 0 or state in {5, 6}:
            raise RuntimeError(f"字幕识别任务失败（状态 {state}）")
        sleeper(max(0.05, float(poll_interval)))
    raise TimeoutError(f"字幕识别超过 {int(timeout)} 秒")


__all__ = ["SubtitleServiceUnavailable", "transcribe_to_srt"]
