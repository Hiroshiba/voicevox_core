# Spectrogram fixtures

These three PNG images cover the seven measured modes. Modes with identical PCM are grouped in `manifest.json`. The images were generated locally from retained, untimed diagnostic WAVs after their PCM SHA-256 and audio format were matched to the output records in measurement run [37633692185](https://github.com/Hiroshiba/voicevox_core/actions/runs/37633692185), commit `6c282db49dcd4f22bcc067acca731740c655ce18`.

Only rendered PNG pixels and provenance metadata are published. The source WAVs, PCM samples, FFT arrays, and phase data are not included. Render-only CI validates each PNG hash and dimensions and matches the recorded PCM hash/format to the measurement JSON; it does not inspect the private source WAVs.

The source is the unencrypted test `sample.vvm` from [VOICEVOX CORE PR #1447](https://github.com/VOICEVOX/voicevox_core/pull/1447), commit `9b539761f3e152b966e08c2de0784129fe8cf68d`, using style 302 and the benchmark's prepared AudioQuery. Query SHA-256: `3d3b07b94d5a1159994888d72b4e9f3362acec551f2bf5275eb71d8fa6802d63`. These are benchmark test outputs, not recordings of a person. Native output came from the native diagnostic; browser groups came from functional WASM diagnostics and are not Node performance measurements.

Method: first channel of PCM16 / 32768; periodic Hann window 1024; hop 256; centered zero padding; one-sided FFT amplitude normalized by `2 / sum(window)` except DC/Nyquist; `20 log10`, clipped to -100 to 0 dBFS. All images share time, frequency, and color scales. Each image is one diagnostic output, not an average of the 15 measured trials. PNG files contain only RGB pixels with IHDR/IDAT/IEND chunks.
