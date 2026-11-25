"""Streaming spectral subtraction using a recorded microphone noise profile."""

from collections import deque
import wave

import numpy as np

from .camera import MIC_SAMPLE_RATES

WINDOW_SAMPLES = 1024
HOP_SAMPLES = 128
PCM_BYTES = 640
SEQUENCE_BYTES = 4
WINDOW = np.hanning(WINDOW_SAMPLES + 1)[:-1]
WINDOW_POWER = WINDOW**2
MIN_POWER_GAIN = 10 ** (-18 / 10)


def load_noise_profile(path):
    with wave.open(str(path), "rb") as source:
        length = source.getnframes()
        sample_rate = source.getframerate()
        if (source.getnchannels(), source.getsampwidth()) != (1, 2) or sample_rate not in MIC_SAMPLE_RATES:
            raise ValueError("Noise profile WAV must be 8 or 16 kHz, mono, PCM16")
        if not WINDOW_SAMPLES <= length <= 60 * sample_rate:
            raise ValueError(f"Noise profile WAV must contain {WINDOW_SAMPLES / sample_rate} to 60 seconds")
        pcm = source.readframes(length)
        if len(pcm) != length * 2:
            raise ValueError("Noise profile WAV is truncated")
    samples = np.frombuffer(pcm, "<i2").astype(np.float64)
    samples -= samples.mean()
    windows = np.lib.stride_tricks.sliding_window_view(samples, WINDOW_SAMPLES)[::HOP_SAMPLES]
    return sample_rate, np.mean(abs(np.fft.rfft(windows * WINDOW))**2, axis=0)


class Denoiser:
    def __init__(self, noise_power):
        self.threshold = 3 * noise_power
        self.reset()

    def reset(self):
        self.discard = WINDOW_SAMPLES - HOP_SAMPLES
        self.input = np.zeros(self.discard)
        self.overlap = np.zeros(WINDOW_SAMPLES)
        self.weight = np.zeros(WINDOW_SAMPLES)
        self.gain = np.ones(WINDOW_SAMPLES // 2 + 1)
        self.pending = bytearray()
        self.sequences = deque()
        self.previous = None

    def feed(self, message):
        sequence = message[:SEQUENCE_BYTES]
        number = int.from_bytes(sequence, "big")
        if self.previous is not None and number != (self.previous + 1) & 0xFFFFFFFF:
            self.reset()
        self.previous = number
        self.sequences.append(sequence)
        samples = np.frombuffer(message[SEQUENCE_BYTES:], "<i2")
        data = np.concatenate((self.input, samples))

        consumed = 0
        while consumed + WINDOW_SAMPLES <= len(data):
            self.process_window(data[consumed:consumed + WINDOW_SAMPLES])
            consumed += HOP_SAMPLES
        self.input = data[consumed:]

        frames = []
        while len(self.pending) >= PCM_BYTES:
            frames.append(self.sequences.popleft() + self.pending[:PCM_BYTES])
            del self.pending[:PCM_BYTES]
        return frames

    def process_window(self, samples):
        spectrum = np.fft.rfft(samples * WINDOW)
        power = abs(spectrum)**2
        desired = np.sqrt(np.maximum(MIN_POWER_GAIN, 1 - self.threshold / np.maximum(power, 1e-12)))
        # Smooth over past gains only so the filter needs no extra lookahead.
        self.gain = 0.7 * self.gain + 0.3 * desired
        self.overlap += np.fft.irfft(spectrum * self.gain, n=WINDOW_SAMPLES) * WINDOW
        self.weight += WINDOW_POWER
        if self.discard:
            self.discard -= HOP_SAMPLES
        else:
            pcm = self.overlap[:HOP_SAMPLES] / self.weight[:HOP_SAMPLES]
            self.pending.extend(np.rint(np.clip(pcm, -32768, 32767)).astype("<i2").tobytes())
        for buffer in (self.overlap, self.weight):
            buffer[:-HOP_SAMPLES] = buffer[HOP_SAMPLES:]
            buffer[-HOP_SAMPLES:] = 0
