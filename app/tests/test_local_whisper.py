import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from v16_subtitle_asr import local_whisper_to_srt


class LocalWhisperTest(unittest.TestCase):
    def test_transcribes_pcm_without_pyav_or_model_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("model.bin", "tokenizer.json", "config.json"):
                (root / name).touch()
            audio = root / "配音.wav"
            with wave.open(str(audio), "wb") as source:
                source.setparams((1, 2, 16000, 0, "NONE", ""))
                source.writeframes(np.array([-16384, 16384], dtype="<i2").tobytes())
            model = Mock()
            model.return_value.transcribe.return_value = ([SimpleNamespace(start=1.25, end=2.4, text="测试")], None)
            with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=model)}):
                output = local_whisper_to_srt(audio, root / "字幕.srt", root)
            self.assertTrue(model.call_args.kwargs["local_files_only"])
            samples = model.return_value.transcribe.call_args.args[0]
            self.assertEqual(samples.dtype, np.float32)
            np.testing.assert_equal(samples, [-.5, .5])
            self.assertIn("00:00:01,250 --> 00:00:02,400\n测试", output.read_text(encoding="utf-8"))
