// ユーザー辞書の登録によって読みが変化することを確認するテスト。
// 辞書ロード前後でAudioQueryのkanaが変化するかどうかで確認する。

use std::env;
use std::ffi::{CStr, CString};
use std::mem::MaybeUninit;
use std::sync::LazyLock;

use assert_cmd::assert::AssertResult;
use const_format::concatcp;
use libloading::Library;
use serde::{Deserialize, Serialize};
use test_util::OPEN_JTALK_DIC_DIR;
use test_util::c_api::{
    self, CApi, VoicevoxInitializeOptions, VoicevoxLoadOnnxruntimeOptions, VoicevoxResultCode,
};

use crate::{
    assert_cdylib::{self, Utf8Output, case},
    snapshots,
};

case!(TestCase);

#[derive(Serialize, Deserialize)]
struct TestCase;

#[typetag::serde(name = "user_dict_load")]
impl assert_cdylib::TestCase for TestCase {
    unsafe fn exec(&self, lib: Library) -> anyhow::Result<()> {
        // SAFETY: The safety contract must be upheld by the caller.
        let lib = unsafe { CApi::from_library(lib) }?;

        // SAFETY: `voicevox_user_dict_new`には特にはsafety requirementsは無いはず。
        let dict = unsafe { lib.voicevox_user_dict_new() };

        let mut word_uuid = [0u8; _];

        let word = {
            // SAFETY: `voicevox_user_dict_word_make` itself has no safety requirements.
            let mut word = unsafe {
                lib.voicevox_user_dict_word_make(
                    c"this_word_should_not_exist_in_default_dictionary".as_ptr(),
                    c"アイウエオ".as_ptr(),
                    0,
                )
            };
            word.word_type =
                c_api::VoicevoxUserDictWordType_VOICEVOX_USER_DICT_WORD_TYPE_PROPER_NOUN;
            word.priority = 10;

            word
        };

        // SAFETY:
        // - `dict.surface` and `dict.pronunciation` are valid.
        // - `word_uuid` is valid for writes.
        assert_ok(unsafe { lib.voicevox_user_dict_add_word(dict, &word, &mut word_uuid) });

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

        let mut audio_query_without_dict = std::ptr::null_mut();
        assert_ok(unsafe {
            // SAFETY:
            // - A `CStr` is a valid string.
            // - `audio_query_without_dict` is valid for writes.
            lib.voicevox_synthesizer_create_audio_query(
                synthesizer,
                c"this_word_should_not_exist_in_default_dictionary".as_ptr(),
                STYLE_ID,
                &mut audio_query_without_dict,
            )
        });
        let audio_query_without_dict = serde_json::from_str::<serde_json::Value>(
            // SAFETY: `voicevox_synthesizer_create_audio_query` initializes `audio_query` if
            // succeeded.
            unsafe { CStr::from_ptr(audio_query_without_dict) }.to_str()?,
        )?;

        // SAFETY: `voicevox_open_jtalk_rc_use_user_dict` has no safety requirements.
        assert_ok(unsafe { lib.voicevox_open_jtalk_rc_use_user_dict(openjtalk, dict) });

        let mut audio_query_with_dict = std::ptr::null_mut();
        assert_ok(unsafe {
            // SAFETY:
            // - A `CStr` is a valid string.
            // - `audio_query_with_dict` is valid for writes.
            lib.voicevox_synthesizer_create_audio_query(
                synthesizer,
                c"this_word_should_not_exist_in_default_dictionary".as_ptr(),
                STYLE_ID,
                &mut audio_query_with_dict,
            )
        });

        let audio_query_with_dict = serde_json::from_str::<serde_json::Value>(
            // SAFETY: `voicevox_synthesizer_create_audio_query` initializes `audio_query` if
            // succeeded.
            unsafe { CStr::from_ptr(audio_query_with_dict) }.to_str()?,
        )?;

        assert_ne!(
            audio_query_without_dict.get("kana"),
            audio_query_with_dict.get("kana")
        );

        // SAFETY: `voicevox_voice_model_file_delete`, `voicevox_open_jtalk_rc_delete`,
        // `voicevox_synthesizer_delete`, and `voicevox_user_dict_delete` have no safety
        // requirements.
        unsafe { lib.voicevox_voice_model_file_delete(model) };
        unsafe { lib.voicevox_open_jtalk_rc_delete(openjtalk) };
        unsafe { lib.voicevox_synthesizer_delete(synthesizer) };
        unsafe { lib.voicevox_user_dict_delete(dict) };

        return Ok(());

        fn assert_ok(result_code: VoicevoxResultCode) {
            std::assert_eq!(c_api::VoicevoxResultCode_VOICEVOX_RESULT_OK, result_code);
        }
        const STYLE_ID: u32 = 0;
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

static SNAPSHOTS: LazyLock<Snapshots> = snapshots::section!(user_dict_load);

#[derive(Deserialize)]
struct Snapshots {
    #[serde(deserialize_with = "snapshots::deserialize_platform_specific_snapshot")]
    stderr: String,
}
