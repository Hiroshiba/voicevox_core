use std::{
    env,
    ffi::{CStr, CString},
    mem::{self, MaybeUninit},
    slice,
    sync::LazyLock,
};

use assert_cmd::assert::AssertResult;
use const_format::concatcp;
use libloading::Library;
use serde::{Deserialize, Serialize};
use test_util::{
    OPEN_JTALK_DIC_DIR,
    c_api::{
        self, CApi, VoicevoxInitializeOptions, VoicevoxLoadOnnxruntimeOptions, VoicevoxResultCode,
    },
};

use crate::{
    assert_cdylib::{self, Utf8Output, case},
    snapshots,
};

case!(TestCase);

#[derive(Serialize, Deserialize)]
struct TestCase;

#[typetag::serde(name = "song")]
impl assert_cdylib::TestCase for TestCase {
    unsafe fn exec(&self, lib: Library) -> anyhow::Result<()> {
        // SAFETY: The safety contract must be upheld by the caller.
        let lib = unsafe { CApi::from_library(lib) }?;

        let model = {
            let mut model = MaybeUninit::uninit();
            assert_ok(unsafe {
                // SAFETY:
                // - `SAMPLE_VOICE_MODEL_FILE_PATH` is a valid string.
                // - `model` is valid for writes.
                lib.voicevox_voice_model_file_open(
                    c_api::SAMPLE_VOICE_MODEL_FILE_PATH.as_ptr(),
                    model.as_mut_ptr(),
                )
            });
            // SAFETY: `voicevox_voice_model_file_open` initializes `model` if succeeded.
            unsafe { model.assume_init() }
        };

        let onnxruntime = {
            let mut onnxruntime = MaybeUninit::uninit();
            assert_ok(unsafe {
                // SAFETY:
                // - A `CStr` is a valid string.
                // - `onnxruntime` is valid for writes.
                lib.voicevox_onnxruntime_load_once(
                    VoicevoxLoadOnnxruntimeOptions {
                        filename: CStr::from_bytes_with_nul(
                            concatcp!(
                                env::consts::DLL_PREFIX,
                                "onnxruntime",
                                env::consts::DLL_SUFFIX,
                                '\0'
                            )
                            .as_ref(),
                        )
                        .expect("this ends with nul")
                        .as_ptr(),
                    },
                    onnxruntime.as_mut_ptr(),
                )
            });
            // SAFETY: `voicevox_onnxruntime_load_once` initializes `onnxruntime` if succeeded.
            unsafe { onnxruntime.assume_init() }
        };

        let openjtalk = {
            let mut openjtalk = MaybeUninit::uninit();
            let open_jtalk_dic_dir = CString::new(OPEN_JTALK_DIC_DIR).unwrap();
            assert_ok(unsafe {
                // SAFETY:
                // - A `CString` is a valid string.
                // - `openjtalk` is valid for writes.
                lib.voicevox_open_jtalk_rc_new(open_jtalk_dic_dir.as_ptr(), openjtalk.as_mut_ptr())
            });
            // SAFETY: `voicevox_open_jtalk_rc_new` initializes `openjtalk` if succeeded.
            unsafe { openjtalk.assume_init() }
        };

        let synthesizer = {
            let mut synthesizer = MaybeUninit::uninit();
            assert_ok(unsafe {
                // SAFETY:
                // - `onnxruntime` is valid for reads.
                // - `synthesizer` is valid for writes.
                lib.voicevox_synthesizer_new(
                    onnxruntime,
                    openjtalk,
                    VoicevoxInitializeOptions {
                        acceleration_mode:
                            c_api::VoicevoxAccelerationMode_VOICEVOX_ACCELERATION_MODE_CPU,
                        ..lib.voicevox_make_default_initialize_options()
                    },
                    synthesizer.as_mut_ptr(),
                )
            });
            // SAFETY: `voicevox_synthesizer_new` initializes `synthesizer` if succeeded.
            unsafe { synthesizer.assume_init() }
        };

        // SAFETY: `voicevox_synthesizer_load_voice_model` has no safety requirements.
        assert_ok(unsafe {
            lib.voicevox_synthesizer_load_voice_model(
                synthesizer,
                model,
                lib.voicevox_make_default_load_voice_model_options(),
            )
        });

        const SINGING_TEACHER: u32 = 6000;
        const SINGER: u32 = 3000;

        const SCORE: &CStr = cr#"{
  "notes": [
    { "lyric": "", "frame_length": 15, "id": "①" },
    { "key": 60, "lyric": "ド", "frame_length": 45, "id": "②" },
    { "key": 62, "lyric": "レ", "frame_length": 45, "id": "③" },
    { "key": 64, "lyric": "ミ", "frame_length": 45, "id": "④" },
    { "lyric": "", "frame_length": 15, "id": "⑤" }
  ]
}"#;
        const NUM_TOTAL_FRAMES: usize = 15 + 45 + 45 + 45 + 15;

        let frame_audio_query_json = {
            let mut frame_audio_query = MaybeUninit::uninit();
            assert_ok(unsafe {
                // SAFETY:
                // - A `CStr` is a valid string.
                // - `frame_audio_query` is valid for writes.
                lib.voicevox_synthesizer_create_sing_frame_audio_query(
                    synthesizer,
                    SCORE.as_ptr(),
                    SINGING_TEACHER,
                    frame_audio_query.as_mut_ptr(),
                )
            });
            // SAFETY: `voicevox_synthesizer_create_sing_frame_audio_query` initializes
            // `frame_audio_query` if succeeded.
            unsafe { frame_audio_query.assume_init() }
        };

        let frame_audio_query = serde_json::from_str::<FrameAudioQuery>(
            // SAFETY: `voicevox_synthesizer_create_sing_frame_audio_query` output a valid string.
            unsafe { CStr::from_ptr(frame_audio_query_json) }
                .to_str()
                .unwrap(),
        )?;

        std::assert_eq!(
            ["pau", "d", "o", "r", "e", "m", "i", "pau"],
            *frame_audio_query
                .phonemes
                .iter()
                .map(|FramePhoneme { phoneme, .. }| phoneme)
                .collect::<Vec<_>>(),
        );

        std::assert_eq!(
            ["①", "②", "②", "③", "③", "④", "④", "⑤"],
            *frame_audio_query
                .phonemes
                .iter()
                .map(|FramePhoneme { note_id, .. }| note_id)
                .collect::<Vec<_>>(),
        );

        std::assert_eq!(
            SNAPSHOTS.output.phoneme_lengths,
            *frame_audio_query
                .phonemes
                .iter()
                .map(|&FramePhoneme { frame_length, .. }| frame_length)
                .collect::<Vec<_>>(),
        );

        assert!(
            [
                frame_audio_query
                    .phonemes
                    .iter()
                    .map(|&FramePhoneme { frame_length, .. }| frame_length)
                    .sum(),
                frame_audio_query.f0.len(),
                frame_audio_query.volume.len(),
            ]
            .into_iter()
            .all(|len| len == NUM_TOTAL_FRAMES)
        );

        let f0s = {
            let mut f0s = MaybeUninit::uninit();
            assert_ok(unsafe {
                // SAFETY:
                // - `SCORE` and `frame_audio_query` are valid strings.
                // - `f0s` is valid for writes.
                lib.voicevox_synthesizer_create_sing_frame_f0(
                    synthesizer,
                    SCORE.as_ptr(),
                    frame_audio_query_json,
                    SINGING_TEACHER,
                    f0s.as_mut_ptr(),
                )
            });
            // SAFETY: `voicevox_synthesizer_create_sing_frame_f0` initializes `f0s` if succeeded.
            unsafe { f0s.assume_init() }
        };

        std::assert_eq!(
            NUM_TOTAL_FRAMES,
            serde_json::from_str::<Vec<serde_json::Value>>(
                // SAFETY: `voicevox_synthesizer_create_sing_frame_f0` outputs a valid string.
                unsafe { CStr::from_ptr(f0s) }.to_str().unwrap(),
            )?
            .len(),
        );

        let volumes = {
            let mut volumes = MaybeUninit::uninit();
            assert_ok(unsafe {
                // SAFETY:
                // - `SCORE` and `frame_audio_query` are valid strings.
                // - `volumes` is valid for writes.
                lib.voicevox_synthesizer_create_sing_frame_volume(
                    synthesizer,
                    SCORE.as_ptr(),
                    frame_audio_query_json,
                    SINGING_TEACHER,
                    volumes.as_mut_ptr(),
                )
            });
            // SAFETY: `voicevox_synthesizer_create_sing_frame_volume` initializes `volumes` if
            // succeeded.
            unsafe { volumes.assume_init() }
        };

        std::assert_eq!(
            NUM_TOTAL_FRAMES,
            serde_json::from_str::<Vec<serde_json::Value>>(
                // SAFETY: `voicevox_synthesizer_create_sing_frame_volume` outputs a valid string.
                unsafe { CStr::from_ptr(volumes) }.to_str().unwrap(),
            )?
            .len(),
        );

        let (wav_length, wav) = {
            let mut wav_length = MaybeUninit::uninit();
            let mut wav = MaybeUninit::uninit();
            assert_ok(unsafe {
                // SAFETY:
                // - `frame_audio_query` is a valid string.
                // - `wav_length` and `wav` are valid for writes.
                lib.voicevox_synthesizer_frame_synthesis(
                    synthesizer,
                    frame_audio_query_json,
                    SINGER,
                    wav_length.as_mut_ptr(),
                    wav.as_mut_ptr(),
                )
            });
            // SAFETY: `voicevox_synthesizer_frame_synthesis` initializes `wav_length` and `wav` if
            // succeeded.
            let wav_length = unsafe { wav_length.assume_init() };
            let wav = unsafe { wav.assume_init() };
            (wav_length, wav)
        };

        {
            // SAFETY: `voicevox_synthesizer_frame_synthesis` outputs a valid slice.
            let wav = unsafe { slice::from_raw_parts(wav, wav_length) };

            assert!(wav.starts_with(b"RIFF"));
            std::assert_eq!(
                NUM_TOTAL_FRAMES
                    * 256
                    * mem::size_of::<u16>()
                    * (1 + usize::from(frame_audio_query.output_stereo)),
                u32::from_le_bytes(*wav[4..].first_chunk().unwrap()) as usize - 36,
            );
            std::assert_eq!(*b"WAVEfmt ", wav[8..16]);
        }

        // SAFETY: These functions have no safety requirements.
        unsafe { lib.voicevox_voice_model_file_delete(model) };
        unsafe { lib.voicevox_open_jtalk_rc_delete(openjtalk) };
        unsafe { lib.voicevox_synthesizer_delete(synthesizer) };

        // SAFETY: These data are valid, and are no longer used.
        unsafe { lib.voicevox_json_free(frame_audio_query_json) };
        unsafe { lib.voicevox_json_free(f0s) };
        unsafe { lib.voicevox_json_free(volumes) };
        unsafe { lib.voicevox_wav_free(wav) };

        return Ok(());

        fn assert_ok(result_code: VoicevoxResultCode) {
            std::assert_eq!(c_api::VoicevoxResultCode_VOICEVOX_RESULT_OK, result_code);
        }

        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct FrameAudioQuery {
            f0: Vec<serde_json::Value>,
            volume: Vec<serde_json::Value>,
            phonemes: Vec<FramePhoneme>,
            output_stereo: bool,
        }

        #[derive(Deserialize)]
        struct FramePhoneme {
            phoneme: String,
            frame_length: usize,
            note_id: String,
        }
    }

    fn assert_output(&self, output: Utf8Output) -> AssertResult {
        output
            .mask_timestamps()
            .mask_onnxruntime_filename()
            .mask_windows_video_cards()
            .assert()
            .try_success()?
            .try_stdout("")?
            .try_stderr(&*SNAPSHOTS.stderr)
    }
}

static SNAPSHOTS: LazyLock<Snapshots> = snapshots::section!(song);

#[derive(Deserialize)]
struct Snapshots {
    output: ExpectedOutput,
    #[serde(deserialize_with = "snapshots::deserialize_platform_specific_snapshot")]
    stderr: String,
}

#[derive(Deserialize)]
struct ExpectedOutput {
    phoneme_lengths: Vec<usize>,
}
