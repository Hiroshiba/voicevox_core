// ここにある`#[doc]`はすべてrustdocではなくDoxygen向けのものである。
#![allow(
    clippy::doc_lazy_continuation,
    clippy::missing_safety_doc // safety documentation自体は書くが、Doxygenの慣習に従い`<dt>`で書く
)]

mod c_impls;
/// cbindgen:ignore
#[cfg(feature = "load-onnxruntime")]
mod compatible_engine;
mod drop_check;
mod helpers;
mod object;
mod result_code;
mod slice_owner;

use self::drop_check::C_STRING_DROP_CHECKER;
use self::helpers::{
    CApiError, ToCJson as _, UuidBytesExt as _, ValidateJson, accent_phrases_to_json,
    audio_query_model_to_json, ensure_utf8, into_result_code_with_error,
};
use self::object::{CApiObject as _, CApiObjectPtrExt as _};
use self::result_code::VoicevoxResultCode;
use self::slice_owner::U8_SLICE_OWNER;
use anstream::{AutoStream, stream::RawStream};
use c_impls::{VoicevoxSynthesizerPtrExt as _, VoicevoxVoiceModelFilePtrExt as _};
use chrono::SecondsFormat;
use colorchoice::ColorChoice;
use educe::Educe;
use ref_cast::RefCastCustom;
use std::env;
use std::ffi::{CStr, CString};
use std::fmt;
use std::io;
use std::mem::MaybeUninit;
use std::os::raw::c_char;
use std::ptr::NonNull;
use std::sync::Once;
use tracing_subscriber::EnvFilter;
use tracing_subscriber::fmt::format::Writer;
use uuid::Uuid;
use voicevox_core::__internal::interop::{
    BlockingTextAnalyzerExt as _, DEFAULT_PRIORITY, DEFAULT_WORD_TYPE, ToJsonValue as _,
};
use voicevox_core::{
    AccentPhrase, AudioQuery, FrameAudioQuery, FramePhoneme, Mora, Note, Score, StyleId,
};

fn init_logger_once() {
    static ONCE: Once = Once::new();

    ONCE.call_once(|| {
        let ansi = {
            // anstyle系のクレートを利用して次の2つを行う。
            //
            // * ANSI escape codeを出してよいかの判定（環境変数のチェックとisatty）
            // * 必要であれば`ENABLE_VIRTUAL_TERMINAL_PROCESSING`の有効化

            assert_eq!(
                ColorChoice::Auto,
                ColorChoice::global(),
                "`ColorChoice::write_global` should not have been called",
            );

            AutoStream::choice(&out()) != ColorChoice::Never
                && anstyle_query::term_supports_ansi_color()
                && anstyle_query::windows::enable_ansi_colors().unwrap_or(true)
        };

        tracing_subscriber::fmt()
            .with_env_filter(if env::var_os(EnvFilter::DEFAULT_ENV).is_some() {
                EnvFilter::from_default_env()
            } else {
                "error,voicevox_core=info,voicevox_core_c_api=info,ort=warn".into()
            })
            .with_timer(local_time as fn(&mut Writer<'_>) -> _)
            .with_ansi(ansi)
            .with_writer(out)
            .init();
    });

    fn local_time(wtr: &mut Writer<'_>) -> fmt::Result {
        // ローカル時刻で表示はするが、そのフォーマットはtracing-subscriber本来のものに近いようにする。
        // https://github.com/tokio-rs/tracing/blob/tracing-subscriber-0.3.16/tracing-subscriber/src/fmt/time/datetime.rs#L235-L241
        wtr.write_str(&chrono::Local::now().to_rfc3339_opts(SecondsFormat::Micros, false))
    }

    fn out() -> impl RawStream {
        io::stderr()
    }
}

// TODO: https://github.com/mozilla/cbindgen/issues/927
//#[cfg(feature = "load-onnxruntime")]
//pub const VOICEVOX_ONNXRUNTIME_LIB_NAME: &CStr = ..;
//#[cfg(feature = "load-onnxruntime")]
//pub const VOICEVOX_ONNXRUNTIME_LIB_VERSION: &CStr = ..;

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ONNX Runtimeの動的ライブラリの、バージョン付きのファイル名。
///
/// WindowsとAndroidでは ::voicevox_get_onnxruntime_lib_unversioned_filename と同じ。
///
/// \availability{
///   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
/// }
///
/// \orig-impl{voicevox_get_onnxruntime_lib_versioned_filename}
#[cfg(feature = "load-onnxruntime")]
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_get_onnxruntime_lib_versioned_filename() -> *const c_char {
    init_logger_once();
    const FILENAME: &CStr = VoicevoxOnnxruntime::LIB_VERSIONED_FILENAME;
    C_STRING_DROP_CHECKER.blacklist(FILENAME).as_ptr()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ONNX Runtimeの動的ライブラリの、バージョン無しのファイル名。
///
/// \availability{
///   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
/// }
///
/// \orig-impl{voicevox_get_onnxruntime_lib_unversioned_filename}
#[cfg(feature = "load-onnxruntime")]
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_get_onnxruntime_lib_unversioned_filename() -> *const c_char {
    init_logger_once();
    const FILENAME: &CStr = VoicevoxOnnxruntime::LIB_UNVERSIONED_FILENAME;
    C_STRING_DROP_CHECKER.blacklist(FILENAME).as_ptr()
}

/// ::voicevox_onnxruntime_load_once のオプション。
///
/// \availability{
///   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
/// }
///
/// \no-orig-impl{VoicevoxLoadOnnxruntimeOptions}
#[cfg(feature = "load-onnxruntime")]
#[repr(C)]
pub struct VoicevoxLoadOnnxruntimeOptions {
    /// ONNX Runtimeのファイル名（モジュール名）もしくはファイルパスを指定する。
    ///
    /// `dlopen`/[`LoadLibraryExW`](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryexw)の引数に使われる。デフォルトは ::voicevox_get_onnxruntime_lib_versioned_filename と同じ。
    filename: *const c_char,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// デフォルトの ::voicevox_onnxruntime_load_once のオプションを生成する。
///
/// @return デフォルトの ::voicevox_onnxruntime_load_once のオプション
///
/// \availability{
///   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
/// }
///
/// \no-orig-impl{voicevox_make_default_load_onnxruntime_options}
#[cfg(feature = "load-onnxruntime")]
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_make_default_load_onnxruntime_options() -> VoicevoxLoadOnnxruntimeOptions
{
    init_logger_once();
    let filename = VoicevoxOnnxruntime::LIB_VERSIONED_FILENAME;
    let filename = C_STRING_DROP_CHECKER.blacklist(filename).as_ptr();
    VoicevoxLoadOnnxruntimeOptions { filename }
}

// https://github.com/mozilla/cbindgen/issues/967
/// ONNX Runtime。
///
/// シングルトンであり、インスタンスは高々一つ。
///
/// ```c
/// const VoicevoxOnnxruntime *ort1;
/// voicevox_onnxruntime_load_once(
///     voicevox_make_default_load_onnxruntime_options(), &ort1);
/// const VoicevoxOnnxruntime *ort2 = voicevox_onnxruntime_get();
/// assert(ort1 == ort2);
/// ```
///
/// \orig-impl{VoicevoxOnnxruntime}
#[cfg(false)]
pub struct VoicevoxOnnxruntime(!);

/// cbindgen:ignore
#[derive(RefCastCustom)]
#[repr(transparent)]
pub struct VoicevoxOnnxruntime(voicevox_core::blocking::Onnxruntime);

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxOnnxruntime のインスタンスが既に作られているならそれを得る。
///
/// 作られていなければ`NULL`を返す。
///
/// @returns ::VoicevoxOnnxruntime のインスタンス
///
/// \orig-impl{voicevox_onnxruntime_get}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_onnxruntime_get() -> Option<&'static VoicevoxOnnxruntime> {
    VoicevoxOnnxruntime::get()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ONNX Runtimeをロードして初期化する。
///
/// 一度成功したら、以後は引数を無視して同じ参照を返す。
///
/// @param [in] options オプション
/// @param [out] out_onnxruntime ::VoicevoxOnnxruntime のインスタンス
///
/// @returns 結果コード
///
/// \availability{
///   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
/// }
///
/// \safety{
/// - `options.filename`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `out_onnxruntime`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_onnxruntime_load_once}
#[cfg(feature = "load-onnxruntime")]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_onnxruntime_load_once(
    options: VoicevoxLoadOnnxruntimeOptions,
    out_onnxruntime: NonNull<&'static VoicevoxOnnxruntime>,
) -> VoicevoxResultCode {
    init_logger_once();
    let filename = unsafe {
        // SAFETY: ユーザーに要求している条件で十分
        CStr::from_ptr(options.filename)
    };
    into_result_code_with_error((|| {
        let instance = VoicevoxOnnxruntime::load_once(filename)?;
        unsafe {
            // SAFETY: ユーザーに要求している条件で十分
            out_onnxruntime.write_unaligned(instance);
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ONNX Runtimeを初期化する。
///
/// 一度成功したら以後は同じ参照を返す。
///
/// @param [out] out_onnxruntime ::VoicevoxOnnxruntime のインスタンス
///
/// @returns 結果コード
///
/// \availability{
///   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSでのみ利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
/// }
///
/// \safety{
/// - `out_onnxruntime`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_onnxruntime_init_once}
#[cfg(feature = "link-onnxruntime")]
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_onnxruntime_init_once(
    out_onnxruntime: NonNull<&'static VoicevoxOnnxruntime>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        let instance = VoicevoxOnnxruntime::init_once()?;
        unsafe {
            // SAFETY: ユーザーに要求している条件で十分
            out_onnxruntime.write_unaligned(instance);
        }
        Ok(())
    })())
}

/// テキスト解析器としてのOpen JTalk。
///
/// <b>構築</b>(_construction_)は ::voicevox_open_jtalk_rc_new で行い、<b>破棄</b>(_destruction_)は ::voicevox_open_jtalk_rc_delete で行う。
///
/// 参照カウント方式のスマートポインタ(reference-counted smart pointer)であり、
/// ::voicevox_synthesizer_new に渡されるときには参照カウンタがインクリメントされる形でオブジェクトの共有が行われる。
///
/// \example{
/// ```c
/// OpenJtalkRc *open_jtalk;
/// voicevox_open_jtalk_rc_new("./open_jtalk_dic_utf_8-1.11", &open_jtalk);
/// // ⋮
/// voicevox_open_jtalk_rc_delete(open_jtalk);
/// ```
/// }
///
/// \orig-impl{OpenJtalkRc}
#[derive(Debug, Educe)]
#[educe(Default(expression = "Self { _padding: MaybeUninit::uninit() }"))]
pub struct OpenJtalkRc {
    _padding: MaybeUninit<[u8; 1]>,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::OpenJtalkRc を<b>構築</b>(_construct_)する。
///
/// 解放は ::voicevox_open_jtalk_rc_delete で行う。
///
/// @param [in] open_jtalk_dic_dir 辞書ディレクトリを指すUTF-8のパス
/// @param [out] out_open_jtalk 構築先
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// OpenJtalkRc *open_jtalk;
/// voicevox_open_jtalk_rc_new("./open_jtalk_dic_utf_8-1.11", &open_jtalk);
/// ```
/// }
///
/// \safety{
/// - `open_jtalk_dic_dir`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `out_open_jtalk`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_open_jtalk_rc_new}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_open_jtalk_rc_new(
    open_jtalk_dic_dir: *const c_char,
    out_open_jtalk: NonNull<NonNull<OpenJtalkRc>>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let open_jtalk_dic_dir = ensure_utf8(unsafe { CStr::from_ptr(open_jtalk_dic_dir) })?;
        let open_jtalk = OpenJtalkRc::new(open_jtalk_dic_dir)?;
        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { out_open_jtalk.write_unaligned(open_jtalk) };
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// OpenJtalkの使うユーザー辞書を設定する。
///
/// この関数を呼び出した後にユーザー辞書を変更した場合、再度この関数を呼び出す必要がある。
///
/// @param [in] open_jtalk Open JTalkのオブジェクト
/// @param [in] user_dict ユーザー辞書
///
/// \orig-impl{voicevox_open_jtalk_rc_use_user_dict}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_open_jtalk_rc_use_user_dict(
    open_jtalk: *const OpenJtalkRc,
    user_dict: *const VoicevoxUserDict,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        open_jtalk.body().use_user_dict(&user_dict.body())?;
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 日本語のテキストを解析する。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// @param [in] open_jtalk Open JTalkのオブジェクト
/// @param [in] text UTF-8の日本語テキスト
/// @param [out] output_accent_phrases_json 生成先
///
/// \orig-impl{voicevox_open_jtalk_rc_use_user_dict}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_open_jtalk_rc_analyze(
    open_jtalk: *const OpenJtalkRc,
    text: *const c_char,
    output_accent_phrases_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    let text = unsafe {
        // SAFETY: The safety contract must be upheld by the caller.
        CStr::from_ptr(text)
    };
    into_result_code_with_error((|| {
        let accent_phrases = &open_jtalk.body().analyze_(ensure_utf8(text)?)?;
        let accent_phrases = serde_json::to_string(accent_phrases).expect("should not fail");
        let accent_phrases = CString::new(accent_phrases).expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_accent_phrases_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(accent_phrases).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::OpenJtalkRc を<b>破棄</b>(_destruct_)する。
///
/// 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
///
/// この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
///
/// @param [in] open_jtalk 破棄対象。nullable
///
/// \example{
/// ```c
/// voicevox_open_jtalk_rc_delete(open_jtalk);
/// ```
/// }
///
/// \no-orig-impl{voicevox_open_jtalk_rc_delete}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_open_jtalk_rc_delete(open_jtalk: *mut OpenJtalkRc) {
    init_logger_once();
    open_jtalk.drop_body();
}

/// ::voicevox_synthesizer_load_voice_model の実行時に、同じIDの ::VoicevoxVoiceModelFile が既に読み込まれていたときのふるまい。
///
/// \orig-impl{VoicevoxOnExistingVoiceModelId}
#[repr(i32)]
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
#[allow(
    non_camel_case_types,
    reason = "実際に公開するC APIとの差異をできるだけ少なくするため"
)]
pub enum VoicevoxOnExistingVoiceModelId {
    /// エラーにする。デフォルトのふるまい
    VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_ERROR = 0,
    /// 再読み込みする。VOICEVOX COREでは、長文のテキストを一度に音声合成するとCPU/GPUメモリが大量に占有されたままになる。再読み込みを行うとメモリの使用量が元に戻る
    VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_RELOAD = 1,
    /// 何もしない
    VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_SKIP = 2,
}

/// ハードウェアアクセラレーションモードを設定する設定値。
///
/// \orig-impl{VoicevoxAccelerationMode}
#[repr(i32)]
#[derive(Debug, PartialEq, Eq, Clone, Copy)]
#[allow(
    non_camel_case_types,
    reason = "実際に公開するC APIとの差異をできるだけ少なくするため"
)]
pub enum VoicevoxAccelerationMode {
    /// 実行環境に合った適切なハードウェアアクセラレーションモードを選択する
    VOICEVOX_ACCELERATION_MODE_AUTO = 0,
    /// ハードウェアアクセラレーションモードを"CPU"に設定する
    VOICEVOX_ACCELERATION_MODE_CPU = 1,
    /// ハードウェアアクセラレーションモードを"GPU"に設定する
    VOICEVOX_ACCELERATION_MODE_GPU = 2,
}

/// ::voicevox_synthesizer_new のオプション。
///
/// \no-orig-impl{VoicevoxInitializeOptions}
#[repr(C)]
pub struct VoicevoxInitializeOptions {
    /// ハードウェアアクセラレーションモード
    acceleration_mode: VoicevoxAccelerationMode,
    /// CPU利用数を指定
    /// 0を指定すると環境に合わせたCPUが利用される
    cpu_num_threads: u16,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// デフォルトの初期化オプションを生成する
/// @return デフォルト値が設定された初期化オプション
///
/// \no-orig-impl{voicevox_make_default_initialize_options}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_make_default_initialize_options() -> VoicevoxInitializeOptions {
    init_logger_once();
    VoicevoxInitializeOptions::default()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// voicevoxのバージョンを取得する。
/// @return SemVerでフォーマットされたバージョン。
///
/// \orig-impl{voicevox_get_version}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_get_version() -> *const c_char {
    init_logger_once();
    return C_STRING_DROP_CHECKER.blacklist(VERSION).as_ptr();

    const VERSION: &CStr = if let Ok(version) =
        CStr::from_bytes_with_nul(concat!(env!("CARGO_PKG_VERSION"), '\0').as_bytes())
    {
        version
    } else {
        panic!("`$CARGO_PKG_VERSION` should be a SemVer, so it should not contain `\\0`");
    };
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AccentPhraseの配列からAudioQueryを作る。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
/// @param [out] output_accent_phrases_json 生成先
///
/// \safety{
/// - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_audio_query_create_from_accent_phrases}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_audio_query_create_from_accent_phrases(
    accent_phrases_json: *const c_char,
    output_audio_query_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    let accent_phrases_json = unsafe {
        // SAFETY: The safety contract must be upheld by the caller.
        CStr::from_ptr(accent_phrases_json)
    };
    into_result_code_with_error((|| {
        let accent_phrases = ValidateJson::from_json_without_validation(accent_phrases_json)?;
        let audio_query = &AudioQuery::from_accent_phrases(accent_phrases);
        let audio_query = serde_json::to_string(audio_query).expect("should not fail");
        let audio_query = CString::new(audio_query).expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_audio_query_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(audio_query).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられたJSONが`AudioQuery`型として不正であるときエラーを返す。
///
/// 不正であるとは、以下のいずれかの条件を満たすことである。
///
/// - [Rust APIの`AudioQuery`型]としてデシリアライズ不可、もしくはJSONとして不正。
/// - `accent_phrases`の要素のうちいずれかが、 ::voicevox_accent_phrase_validate でエラーになる。
/// - `outputSamplingRate`が`24000`の倍数ではない、もしくは`0` (将来的に解消予定。cf. [#762])。
///
/// [Rust APIの`AudioQuery`型]: ../rust_api/voicevox_core/struct.AudioQuery.html
/// [#762]: https://github.com/VOICEVOX/voicevox_core/issues/762
///
/// 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
///
/// - `accent_phrases`の要素のうちいずれかが警告が出る状態。
/// - `speedScale`が負。
/// - `volumeScale`が負。
/// - `prePhonemeLength`が負。
/// - `postPhonemeLength`が負。
/// - `outputSamplingRate`が`24000`以外の値（エラーと同様将来的に解消予定）。
///
/// @param [in] audio_query_json `AudioQuery`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_AUDIO_QUERY_ERROR
///
/// \safety{
/// - `audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_audio_query_validate}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_audio_query_validate(
    audio_query_json: *const c_char,
) -> VoicevoxResultCode {
    init_logger_once();
    // SAFETY: The safety contract must be upheld by the caller.
    let audio_query_json = unsafe { CStr::from_ptr(audio_query_json) };
    into_result_code_with_error(AudioQuery::validate_json(audio_query_json).map(|_| ()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられたJSONが`AccentPhrase`型として不正であるときエラーを返す。
///
/// 不正であるとは、以下のいずれかの条件を満たすことである。
///
/// - [Rust APIの`AccentPhrase`型]としてデシリアライズ不可、もしくはJSONとして不正。
/// - `moras`もしくは`pause_mora`の要素のうちいずれかが、 ::voicevox_mora_validate でエラーになる。
/// - `accent`が`0`。
///
/// [Rust APIの`AccentPhrase`型]: ../rust_api/voicevox_core/struct.AccentPhrase.html
///
/// 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
///
/// - `moras`もしくは`pause_mora`の要素のうちいずれかが、警告が出る状態。
/// - `accent`が`moras`の数を超過している。
///
/// @param [in] accent_phrase_json `AccentPhrase`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_ACCENT_PHRASE_ERROR
///
/// \safety{
/// - `accent_phrase_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_accent_phrase_validate}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_accent_phrase_validate(
    accent_phrase_json: *const c_char,
) -> VoicevoxResultCode {
    init_logger_once();
    // SAFETY: The safety contract must be upheld by the caller.
    let accent_phrase_json = unsafe { CStr::from_ptr(accent_phrase_json) };
    into_result_code_with_error(AccentPhrase::validate_json(accent_phrase_json).map(|_| ()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられたJSONが`Mora`型として不正であるときエラーを返す。
///
/// 不正であるとは、以下のいずれかの条件を満たすことである。
///
/// - [Rust APIの`Mora`型]としてデシリアライズ不可、もしくはJSONとして不正。
/// - `consonant`と`consonant_length`の有無が不一致。
/// - `consonant`が子音以外の音素であるか、もしくは音素として不正。
/// - `vowel`が子音であるか、もしくは音素として不正。
///
/// [Rust APIの`Mora`型]: ../rust_api/voicevox_core/struct.Mora.html
///
/// 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
///
/// - `consonant_length`が負。
/// - `vowel_length`が負。
///
/// @param [in] mora_json `Mora`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_MORA_ERROR
///
/// \safety{
/// - `mora_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_mora_validate}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_mora_validate(mora_json: *const c_char) -> VoicevoxResultCode {
    init_logger_once();
    // SAFETY: The safety contract must be upheld by the caller.
    let mora_json = unsafe { CStr::from_ptr(mora_json) };
    into_result_code_with_error(Mora::validate_json(mora_json).map(|_| ()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられたJSONが`Score`型として不正であるときエラーを返す。
///
/// 不正であるとは、以下のいずれかの条件を満たすことである。
///
/// - [Rust APIの`Score`型]としてデシリアライズ不可、もしくはJSONとして不正。
/// - `notes`の要素のうちいずれかが、 ::voicevox_note_validate でエラーになる。
/// - `notes`が空であるか、もしくは先頭が音符。
///
/// [Rust APIの`Score`型]: ../rust_api/voicevox_core/struct.Score.html
///
/// @param [in] score_json `Score`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_SCORE_ERROR
///
/// \safety{
/// - `score_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_score_validate}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_score_validate(score_json: *const c_char) -> VoicevoxResultCode {
    init_logger_once();
    // SAFETY: The safety contract must be upheld by the caller.
    let score_json = unsafe { CStr::from_ptr(score_json) };
    into_result_code_with_error(Score::validate_json(score_json).map(|_| ()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられたJSONが`Note`型として不正であるときエラーを返す。
///
/// 不正であるとは、以下のいずれかの条件を満たすことである。
///
/// - [Rust APIの`Note`型]としてデシリアライズ不可、もしくはJSONとして不正。
/// - `key`が`null`かつ`lyric`が`""`以外。
/// - `key`が非`null`かつ`lyric`が`""`。
///
/// [Rust APIの`Note`型]: ../rust_api/voicevox_core/struct.Note.html
///
/// @param [in] note_json `Note`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_NOTE_ERROR
///
/// \safety{
/// - `note_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_note_validate}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_note_validate(note_json: *const c_char) -> VoicevoxResultCode {
    init_logger_once();
    // SAFETY: The safety contract must be upheld by the caller.
    let note_json = unsafe { CStr::from_ptr(note_json) };
    into_result_code_with_error(Note::validate_json(note_json).map(|_| ()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられたJSONが`FrameAudioQuery`型として不正であるときエラーを返す。
///
/// 不正であるとは、以下の条件を満たすことである。
///
/// - [Rust APIの`FrameAudioQuery`型]としてデシリアライズ不可、もしくはJSONとして不正。
///
/// [Rust APIの`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
///
/// 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
///
/// - `outputSamplingRate`が`24000`以外の値（将来的に解消予定）。
///
/// @param [in] frame_audio_query_json `FrameAudioQuery`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_FRAME_AUDIO_QUERY_ERROR
///
/// \safety{
/// - `frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_frame_audio_query_validate}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_frame_audio_query_validate(
    frame_audio_query_json: *const c_char,
) -> VoicevoxResultCode {
    init_logger_once();
    // SAFETY: The safety contract must be upheld by the caller.
    let frame_audio_query_json = unsafe { CStr::from_ptr(frame_audio_query_json) };
    into_result_code_with_error(FrameAudioQuery::validate_json(frame_audio_query_json).map(|_| ()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられたJSONが`FramePhoneme`型として不正であるときエラーを返す。
///
/// 不正であるとは、以下の条件を満たすことである。
///
/// - [Rust APIの`FramePhoneme`型]としてデシリアライズ不可、もしくはJSONとして不正。
///
/// [Rust APIの`FramePhoneme`型]: ../rust_api/voicevox_core/struct.FramePhoneme.html
///
/// @param [in] frame_phoneme_json `FramePhoneme`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_FRAME_PHONEME_ERROR
///
/// \safety{
/// - `frame_phoneme_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \no-orig-impl{voicevox_frame_phoneme_validate}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_frame_phoneme_validate(
    frame_phoneme_json: *const c_char,
) -> VoicevoxResultCode {
    init_logger_once();
    // SAFETY: The safety contract must be upheld by the caller.
    let frame_phoneme_json = unsafe { CStr::from_ptr(frame_phoneme_json) };
    into_result_code_with_error(FramePhoneme::validate_json(frame_phoneme_json).map(|_| ()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 与えられた楽譜と歌唱合成用のクエリの組み合わせが、基本周波数と音量の生成に利用できるかどうかを確認する。
///
/// 次のうちどれかを満たすならエラーを返す。
///
/// - `score_json`が ::voicevox_score_validate でエラーになる。
/// - `frame_audio_query_json`が ::voicevox_frame_audio_query_validate でエラーになる。
/// - `notes`が表す音素ID列と、`phonemes`が表す音素ID列が等しくない。ただし異なる音素の表現が同一のIDを表すことがある。
///
/// @param [in] score_json `Score`型のJSON
/// @param [in] frame_audio_query_json `FrameAudioQuery`型のJSON
///
/// @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_SCORE_ERROR, ::VOICEVOX_RESULT_INVALID_FRAME_AUDIO_QUERY_ERROR, ::VOICEVOX_RESULT_INCOMPATIBLE_QUERIES_ERROR
///
/// \safety{
/// - `score_json`と`frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
/// \orig-impl{voicevox_ensure_compatible}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_ensure_compatible(
    score_json: *const c_char,
    frame_audio_query_json: *const c_char,
) -> VoicevoxResultCode {
    init_logger_once();

    // SAFETY: The safety contract must be upheld by the caller.
    let score_json = unsafe { CStr::from_ptr(score_json) };
    let frame_audio_query_json = unsafe { CStr::from_ptr(frame_audio_query_json) };

    into_result_code_with_error((|| {
        let score = &Score::validate_json(score_json)?;
        let frame_audio_query = &FrameAudioQuery::validate_json(frame_audio_query_json)?;
        voicevox_core::ensure_compatible(score, frame_audio_query).map_err(Into::into)
    })())
}

/// 音声モデルファイル。
///
/// VVMファイルと対応する。
/// <b>構築</b>(_construction_)は ::voicevox_voice_model_file_open で行い、<b>破棄</b>(_destruction_)は ::voicevox_voice_model_file_delete で行う。
///
/// \orig-impl{VoicevoxVoiceModelFile}
#[derive(Debug, Educe)]
#[educe(Default(expression = "Self { _padding: MaybeUninit::uninit() }"))]
pub struct VoicevoxVoiceModelFile {
    _padding: MaybeUninit<[u8; 1]>,
}

/// 音声モデルID。
///
/// ::VoicevoxSynthesizer はこのIDをキーとして、音声モデルのロード・アンロードを行う。
///
/// 同じIDを持つ複数のVVMファイルがあるときは、ファイルとして新しい方を常に使うことが推奨される。[VOICEVOX/voicevox_vvm]で管理されているVVMでは、次の方針が取られている。
///
/// - VVMに含まれる声が変化せず、軽微な修正のみのときはIDを使い回してリリースする。
/// - VVMに含まれる声が明確に変化するかもしくは削除されるような実質的な変更のときは、新しいIDを割り振ってリリースする。
///
/// これ以外は未定であり、更なるルールについては[VOICEVOX/voicevox_vvm#19]で議論される予定。
///
/// \orig-impl{VoicevoxVoiceModelId}
///
/// [VOICEVOX/voicevox_vvm]: https://github.com/VOICEVOX/voicevox_vvm
/// [VOICEVOX/voicevox_vvm#19]: https://github.com/VOICEVOX/voicevox_vvm/issues/19
pub type VoicevoxVoiceModelId<'a> = &'a [u8; 16];

/// スタイルID。
///
/// VOICEVOXにおける、ある<i>キャラクター</i>のある<i>スタイル</i>を指す。
///
/// \orig-impl{VoicevoxStyleId}
pub type VoicevoxStyleId = u32;

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// VVMファイルを開く。
///
/// @param [in] path vvmファイルへのUTF-8のファイルパス
/// @param [out] out_model 構築先
///
/// @returns 結果コード
///
/// \safety{
/// - `path`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `out_model`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_voice_model_file_open}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_voice_model_file_open(
    path: *const c_char,
    out_model: NonNull<NonNull<VoicevoxVoiceModelFile>>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let path = ensure_utf8(unsafe { CStr::from_ptr(path) })?;
        let model = VoicevoxVoiceModelFile::open(path)?;
        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { out_model.write_unaligned(model) };
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxVoiceModelFile からIDを取得する。
///
/// @param [in] model 音声モデル
/// @param [out] output_voice_model_id 音声モデルID。詳細は ::VoicevoxVoiceModelId
///
/// \safety{
/// - `output_voice_model_id`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_voice_model_file_id}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_voice_model_file_id(
    model: *const VoicevoxVoiceModelFile,
    output_voice_model_id: NonNull<[u8; 16]>,
) {
    init_logger_once();
    let id = model.body().id().0.into_bytes();
    unsafe { output_voice_model_id.write_unaligned(id) };
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxVoiceModelFile からメタ情報を取得する。
///
/// JSONの解放は ::voicevox_json_free で行う。
///
/// @param [in] model 音声モデル
///
/// @returns メタ情報のJSON文字列
///
/// \orig-impl{voicevox_voice_model_file_create_metas_json}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_voice_model_file_create_metas_json(
    model: *const VoicevoxVoiceModelFile,
) -> *mut c_char {
    init_logger_once();
    C_STRING_DROP_CHECKER.whitelist(model.metas()).into_raw()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxVoiceModelFile を、所有しているファイルディスクリプタを閉じた上で<b>破棄</b>(_destruct_)する。ファイルの削除(_delete_)<b>ではない</b>。
///
/// 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
///
/// この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
///
/// @param [in] model 破棄対象。nullable
///
/// \no-orig-impl{voicevox_voice_model_file_delete}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_voice_model_file_delete(model: *mut VoicevoxVoiceModelFile) {
    init_logger_once();
    model.drop_body();
}

/// 音声シンセサイザ。
///
/// <b>構築</b>(_construction_)は ::voicevox_synthesizer_new で行い、<b>破棄</b>(_destruction_)は ::voicevox_synthesizer_delete で行う。
///
/// \orig-impl{VoicevoxSynthesizer}
#[derive(Debug, Educe)]
#[educe(Default(expression = "Self { _padding: MaybeUninit::uninit() }"))]
pub struct VoicevoxSynthesizer {
    _padding: MaybeUninit<[u8; 1]>,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxSynthesizer を<b>構築</b>(_construct_)する。
///
/// @param [in] onnxruntime
/// @param [in] open_jtalk Open JTalkのオブジェクト
/// @param [in] options オプション
/// @param [out] out_synthesizer 構築先
///
/// @returns 結果コード
///
/// \safety{
/// - `onnxruntime`は ::voicevox_onnxruntime_load_once または ::voicevox_onnxruntime_init_once で得たものでなければならない。
/// - `out_synthesizer`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_new}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_new(
    onnxruntime: &'static VoicevoxOnnxruntime,
    open_jtalk: *const OpenJtalkRc,
    options: VoicevoxInitializeOptions,
    out_synthesizer: NonNull<NonNull<VoicevoxSynthesizer>>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        let synthesizer = VoicevoxSynthesizer::new(onnxruntime, open_jtalk, options)?;
        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { out_synthesizer.write_unaligned(synthesizer) };
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxSynthesizer を<b>破棄</b>(_destruct_)する。
///
/// 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
///
/// この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
///
/// @param [in] synthesizer 破棄対象。nullable
///
/// \no-orig-impl{voicevox_synthesizer_delete}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_synthesizer_delete(synthesizer: *mut VoicevoxSynthesizer) {
    init_logger_once();
    synthesizer.drop_body();
}

/// ::voicevox_synthesizer_load_voice_model のオプション。
///
/// \no-orig-impl{VoicevoxLoadVoiceModelOptions}
#[repr(C)]
pub struct VoicevoxLoadVoiceModelOptions {
    /// 同じIDの ::VoicevoxVoiceModelFile が既に読み込まれていたときのふるまい
    on_existing: VoicevoxOnExistingVoiceModelId,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// デフォルトの `voicevox_synthesizer_load_voice_model` のオプションを生成する
/// @return デフォルト値が設定された `voicevox_synthesizer_load_voice_model` のオプション
///
/// \no-orig-impl{voicevox_make_default_load_voice_model_options}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_make_default_load_voice_model_options() -> VoicevoxLoadVoiceModelOptions
{
    init_logger_once();
    VoicevoxLoadVoiceModelOptions::default()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 音声モデルを読み込む。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] model 音声モデル
/// @param [in] options オプション
///
/// @returns 結果コード
///
/// \orig-impl{voicevox_synthesizer_load_voice_model}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_synthesizer_load_voice_model(
    synthesizer: *const VoicevoxSynthesizer,
    model: *const VoicevoxVoiceModelFile,
    options: VoicevoxLoadVoiceModelOptions,
) -> VoicevoxResultCode {
    init_logger_once();
    let VoicevoxLoadVoiceModelOptions { on_existing } = options;
    into_result_code_with_error(synthesizer.load_voice_model(&model.body(), on_existing.into()))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 音声モデルの読み込みを解除する。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] model_id 音声モデルID
///
/// @returns 結果コード
///
/// \safety{
/// - `model_id`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_unload_voice_model}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_synthesizer_unload_voice_model(
    synthesizer: *const VoicevoxSynthesizer,
    model_id: VoicevoxVoiceModelId<'_>,
) -> VoicevoxResultCode {
    init_logger_once();
    let model_id = model_id.to_model_id();
    into_result_code_with_error(synthesizer.unload_voice_model(model_id).map_err(Into::into))
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxOnnxruntime のインスタンスを得る。
///
/// @param [in] synthesizer 音声シンセサイザ
///
/// @returns ::VoicevoxOnnxruntime のインスタンス
///
/// \orig-impl{voicevox_synthesizer_get_onnxruntime}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_synthesizer_get_onnxruntime(
    synthesizer: *const VoicevoxSynthesizer,
) -> &'static VoicevoxOnnxruntime {
    synthesizer.onnxruntime()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ハードウェアアクセラレーションがGPUモードか判定する。
///
/// @param [in] synthesizer 音声シンセサイザ
///
/// @returns GPUモードかどうか
///
/// \orig-impl{voicevox_synthesizer_is_gpu_mode}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_synthesizer_is_gpu_mode(
    synthesizer: *const VoicevoxSynthesizer,
) -> bool {
    init_logger_once();
    synthesizer.body().is_gpu_mode()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 指定したIDの音声モデルが読み込まれているか判定する。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] model_id 音声モデルID
///
/// @returns モデルが読み込まれているかどうか
///
/// \safety{
/// - `model_id`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_is_loaded_voice_model}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_synthesizer_is_loaded_voice_model(
    synthesizer: *const VoicevoxSynthesizer,
    model_id: VoicevoxVoiceModelId<'_>,
) -> bool {
    init_logger_once();
    let model_id = model_id.to_model_id();
    synthesizer.body().is_loaded_voice_model(model_id)
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 今読み込んでいる音声モデルのメタ情報を、JSONで取得する。
///
/// JSONの解放は ::voicevox_json_free で行う。
///
/// @param [in] synthesizer 音声シンセサイザ
///
/// @return メタ情報のJSON文字列
///
/// \orig-impl{voicevox_synthesizer_create_metas_json}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_synthesizer_create_metas_json(
    synthesizer: *const VoicevoxSynthesizer,
) -> *mut c_char {
    init_logger_once();
    let metas = synthesizer.metas();
    C_STRING_DROP_CHECKER.whitelist(metas).into_raw()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ONNX Runtimeとして利用可能なデバイスの情報を、JSONで取得する。
///
/// JSONの解放は ::voicevox_json_free で行う。
///
/// あくまでONNX Runtimeが対応しているデバイスの情報であることに注意。GPUが使える環境ではなかったとしても`cuda`や`dml`は`true`を示しうる。
///
/// @param [in] onnxruntime
/// @param [out] output_supported_devices_json サポートデバイス情報のJSON文字列
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// char *supported_devices;
/// VoicevoxResultCode result = voicevox_onnxruntime_create_supported_devices_json(onnxruntime, &supported_devices);
/// ```
/// }
///
/// \safety{
/// - `onnxruntime`は ::voicevox_onnxruntime_load_once または ::voicevox_onnxruntime_init_once で得たものでなければならない。
/// - `output_supported_devices_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_onnxruntime_create_supported_devices_json}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_onnxruntime_create_supported_devices_json(
    onnxruntime: &'static VoicevoxOnnxruntime,
    output_supported_devices_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        let supported_devices = CString::new(onnxruntime.0.supported_devices()?.to_json()).unwrap();
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_supported_devices_json.write_unaligned(
                C_STRING_DROP_CHECKER
                    .whitelist(supported_devices)
                    .into_raw(),
            );
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AquesTalk風記法から、AudioQueryをJSONとして生成する。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] kana AquesTalk風記法
/// @param [in] style_id スタイルID
/// @param [out] output_audio_query_json 生成先
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// char *audio_query;
/// voicevox_synthesizer_create_audio_query_from_kana(synthesizer, "コンニチワ'",
///                                                   2, // "四国めたん (ノーマル)"
///                                                   &audio_query);
/// ```
/// }
///
/// \safety{
/// - `kana`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_create_audio_query_from_kana}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_create_audio_query_from_kana(
    synthesizer: *const VoicevoxSynthesizer,
    kana: *const c_char,
    style_id: VoicevoxStyleId,
    output_audio_query_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let kana = unsafe { CStr::from_ptr(kana) };
        let kana = ensure_utf8(kana)?;

        let audio_query = synthesizer
            .body()
            .create_audio_query_from_kana(kana, StyleId::new(style_id))?;
        let audio_query = CString::new(audio_query_model_to_json(&audio_query))
            .expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_audio_query_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(audio_query).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 日本語テキストから、AudioQueryをJSONとして生成する。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// ::voicevox_synthesizer_create_accent_phrases と ::voicevox_audio_query_create_from_accent_phrases
/// が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] text UTF-8の日本語テキスト
/// @param [in] style_id スタイルID
/// @param [out] output_audio_query_json 生成先
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// char *audio_query;
/// voicevox_synthesizer_create_audio_query(synthesizer, "こんにちは",
///                                         2, // "四国めたん (ノーマル)"
///                                         &audio_query);
/// ```
/// }
///
/// \safety{
/// - `text`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_create_audio_query}
///
/// [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_create_audio_query(
    synthesizer: *const VoicevoxSynthesizer,
    text: *const c_char,
    style_id: VoicevoxStyleId,
    output_audio_query_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let text = unsafe { CStr::from_ptr(text) };
        let text = ensure_utf8(text)?;

        let audio_query = synthesizer
            .body()
            .create_audio_query(text, StyleId::new(style_id))?;
        let audio_query = CString::new(audio_query_model_to_json(&audio_query))
            .expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_audio_query_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(audio_query).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AquesTalk風記法から、AccentPhrase (アクセント句)の配列をJSON形式で生成する。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] kana AquesTalk風記法
/// @param [in] style_id スタイルID
/// @param [out] output_accent_phrases_json 生成先
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// char *accent_phrases;
/// voicevox_synthesizer_create_accent_phrases_from_kana(
///     synthesizer, "コンニチワ'",
///     2, // "四国めたん (ノーマル)"
///     &accent_phrases);
/// ```
/// }
///
/// \safety{
/// - `kana`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_create_accent_phrases_from_kana}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_create_accent_phrases_from_kana(
    synthesizer: *const VoicevoxSynthesizer,
    kana: *const c_char,
    style_id: VoicevoxStyleId,
    output_accent_phrases_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let kana = ensure_utf8(unsafe { CStr::from_ptr(kana) })?;
        let accent_phrases = synthesizer
            .body()
            .create_accent_phrases_from_kana(kana, StyleId::new(style_id))?;
        let accent_phrases = CString::new(accent_phrases_to_json(&accent_phrases))
            .expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_accent_phrases_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(accent_phrases).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 日本語テキストから、AccentPhrase (アクセント句)の配列をJSON形式で生成する。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// ::voicevox_open_jtalk_rc_analyze と ::voicevox_synthesizer_replace_mora_data
/// が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] text UTF-8の日本語テキスト
/// @param [in] style_id スタイルID
/// @param [out] output_accent_phrases_json 生成先
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// char *accent_phrases;
/// voicevox_synthesizer_create_accent_phrases(synthesizer, "こんにちは",
///                                            2, // "四国めたん (ノーマル)"
///                                            &accent_phrases);
/// ```
/// }
///
/// \safety{
/// - `text`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_create_accent_phrases}
///
/// [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_create_accent_phrases(
    synthesizer: *const VoicevoxSynthesizer,
    text: *const c_char,
    style_id: VoicevoxStyleId,
    output_accent_phrases_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let text = ensure_utf8(unsafe { CStr::from_ptr(text) })?;
        let accent_phrases = synthesizer
            .body()
            .create_accent_phrases(text, StyleId::new(style_id))?;
        let accent_phrases = CString::new(accent_phrases_to_json(&accent_phrases))
            .expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_accent_phrases_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(accent_phrases).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AccentPhraseの配列の音高・音素長を、特定の声で生成しなおす。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// ::voicevox_synthesizer_replace_phoneme_length と ::voicevox_synthesizer_replace_mora_pitch
/// が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
/// @param [in] style_id スタイルID
/// @param [out] output_accent_phrases_json 生成先
///
/// @returns 結果コード
///
/// \safety{
/// - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_replace_mora_data}
///
/// [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_replace_mora_data(
    synthesizer: *const VoicevoxSynthesizer,
    accent_phrases_json: *const c_char,
    style_id: VoicevoxStyleId,
    output_accent_phrases_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let accent_phrases_json = unsafe { CStr::from_ptr(accent_phrases_json) };
        let accent_phrases = Vec::<AccentPhrase>::validate_json(accent_phrases_json)?;
        let accent_phrases = synthesizer
            .body()
            .replace_mora_data(&accent_phrases, StyleId::new(style_id))?;
        let accent_phrases = CString::new(accent_phrases_to_json(&accent_phrases))
            .expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_accent_phrases_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(accent_phrases).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AccentPhraseの配列の音素長を、特定の声で生成しなおす。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
/// @param [in] style_id スタイルID
/// @param [out] output_accent_phrases_json 生成先
///
/// @returns 結果コード
///
/// \safety{
/// - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_replace_phoneme_length}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_replace_phoneme_length(
    synthesizer: *const VoicevoxSynthesizer,
    accent_phrases_json: *const c_char,
    style_id: VoicevoxStyleId,
    output_accent_phrases_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let accent_phrases_json = unsafe { CStr::from_ptr(accent_phrases_json) };
        let accent_phrases = Vec::<AccentPhrase>::validate_json(accent_phrases_json)?;
        let accent_phrases = synthesizer
            .body()
            .replace_phoneme_length(&accent_phrases, StyleId::new(style_id))?;
        let accent_phrases = CString::new(accent_phrases_to_json(&accent_phrases))
            .expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_accent_phrases_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(accent_phrases).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AccentPhraseの配列の音高を、特定の声で生成しなおす。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
/// @param [in] style_id スタイルID
/// @param [out] output_accent_phrases_json 生成先
///
/// @returns 結果コード
///
/// \safety{
/// - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_replace_mora_pitch}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_replace_mora_pitch(
    synthesizer: *const VoicevoxSynthesizer,
    accent_phrases_json: *const c_char,
    style_id: VoicevoxStyleId,
    output_accent_phrases_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let accent_phrases_json = unsafe { CStr::from_ptr(accent_phrases_json) };
        let accent_phrases = Vec::<AccentPhrase>::validate_json(accent_phrases_json)?;
        let accent_phrases = synthesizer
            .body()
            .replace_mora_pitch(&accent_phrases, StyleId::new(style_id))?;
        let accent_phrases = CString::new(accent_phrases_to_json(&accent_phrases))
            .expect("should not contain '\\0'");
        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_accent_phrases_json
                .write_unaligned(C_STRING_DROP_CHECKER.whitelist(accent_phrases).into_raw());
        }
        Ok(())
    })())
}

/// ::voicevox_synthesizer_synthesis のオプション。
///
/// \no-orig-impl{VoicevoxSynthesisOptions}
#[repr(C)]
pub struct VoicevoxSynthesisOptions {
    /// 疑問文の調整を有効にする
    enable_interrogative_upspeak: bool,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// デフォルトの `voicevox_synthesizer_synthesis` のオプションを生成する
/// @return デフォルト値が設定された `voicevox_synthesizer_synthesis` のオプション
///
/// \no-orig-impl{voicevox_make_default_synthesis_options}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_make_default_synthesis_options() -> VoicevoxSynthesisOptions {
    init_logger_once();
    VoicevoxSynthesisOptions::default()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AudioQueryから音声合成を行う。
///
/// 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] audio_query_json AudioQueryのJSON文字列
/// @param [in] style_id スタイルID
/// @param [in] options オプション
/// @param [out] output_wav_length 出力のバイト長
/// @param [out] output_wav 出力先
///
/// @returns 結果コード
///
/// \safety{
/// - `audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_synthesis}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_synthesis(
    synthesizer: *const VoicevoxSynthesizer,
    audio_query_json: *const c_char,
    style_id: VoicevoxStyleId,
    options: VoicevoxSynthesisOptions,
    output_wav_length: NonNull<usize>,
    output_wav: NonNull<NonNull<u8>>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let audio_query_json = unsafe { CStr::from_ptr(audio_query_json) };
        let audio_query = ValidateJson::validate_json(audio_query_json)?;
        let VoicevoxSynthesisOptions {
            enable_interrogative_upspeak,
        } = options;
        let wav = synthesizer
            .body()
            .synthesis(&audio_query, StyleId::new(style_id))
            .enable_interrogative_upspeak(enable_interrogative_upspeak)
            .perform()?;
        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { U8_SLICE_OWNER.own_and_lend(wav, output_wav, output_wav_length) };
        Ok(())
    })())
}

/// ::voicevox_synthesizer_tts のオプション。
///
/// \no-orig-impl{VoicevoxTtsOptions}
#[repr(C)]
pub struct VoicevoxTtsOptions {
    /// 疑問文の調整を有効にする
    enable_interrogative_upspeak: bool,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// デフォルトのテキスト音声合成オプションを生成する
/// @return テキスト音声合成オプション
///
/// \no-orig-impl{voicevox_make_default_tts_options}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_make_default_tts_options() -> VoicevoxTtsOptions {
    init_logger_once();
    VoicevoxTtsOptions::default()
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// AquesTalk風記法から音声合成を行う。
///
/// 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
///
/// @param [in] synthesizer
/// @param [in] kana AquesTalk風記法
/// @param [in] style_id スタイルID
/// @param [in] options オプション
/// @param [out] output_wav_length 出力のバイト長
/// @param [out] output_wav 出力先
///
/// @returns 結果コード
///
/// \safety{
/// - `kana`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_tts_from_kana}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_tts_from_kana(
    synthesizer: *const VoicevoxSynthesizer,
    kana: *const c_char,
    style_id: VoicevoxStyleId,
    options: VoicevoxTtsOptions,
    output_wav_length: NonNull<usize>,
    output_wav: NonNull<NonNull<u8>>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let kana = ensure_utf8(unsafe { CStr::from_ptr(kana) })?;
        let VoicevoxTtsOptions {
            enable_interrogative_upspeak,
        } = options;
        let output = synthesizer
            .body()
            .tts_from_kana(kana, StyleId::new(style_id))
            .enable_interrogative_upspeak(enable_interrogative_upspeak)
            .perform()?;
        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { U8_SLICE_OWNER.own_and_lend(output, output_wav, output_wav_length) };
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 日本語テキストから音声合成を行う。
///
/// 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
///
/// ::voicevox_synthesizer_create_audio_query と ::voicevox_synthesizer_synthesis
/// が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
///
/// @param [in] synthesizer
/// @param [in] text UTF-8の日本語テキスト
/// @param [in] style_id スタイルID
/// @param [in] options オプション
/// @param [out] output_wav_length 出力のバイト長
/// @param [out] output_wav 出力先
///
/// @returns 結果コード
///
/// \safety{
/// - `text`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_tts}
///
/// [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_tts(
    synthesizer: *const VoicevoxSynthesizer,
    text: *const c_char,
    style_id: VoicevoxStyleId,
    options: VoicevoxTtsOptions,
    output_wav_length: NonNull<usize>,
    output_wav: NonNull<NonNull<u8>>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let text = ensure_utf8(unsafe { CStr::from_ptr(text) })?;
        let VoicevoxTtsOptions {
            enable_interrogative_upspeak,
        } = options;
        let output = synthesizer
            .body()
            .tts(text, StyleId::new(style_id))
            .enable_interrogative_upspeak(enable_interrogative_upspeak)
            .perform()?;
        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { U8_SLICE_OWNER.own_and_lend(output, output_wav, output_wav_length) };
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 楽譜から歌唱音声合成用のクエリを作成する。
///
/// 詳細はユーザーガイド[歌唱音声合成]を参照。
///
/// [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
///
/// 生成したJSONを解放するには ::voicevox_json_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] score_json [`Score`型]を表すJSON
/// @param [in] style_id スタイルID
/// @param [out] output_frame_audio_query_json 生成先
///
/// [`Score`型]: ../rust_api/voicevox_core/struct.Score.html
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// const char *kScore =
///     "{"
///     "  \"notes\": [ "
///     "    { \"key\": null, \"frame_length\": 15, \"lyric\": \"\" },"
///     "    { \"key\": 60, \"frame_length\": 45, \"lyric\": \"ド\" },"
///     "    { \"key\": 62, \"frame_length\": 45, \"lyric\": \"レ\" },"
///     "    { \"key\": 64, \"frame_length\": 45, \"lyric\": \"ミ\" },"
///     "    { \"key\": null, \"frame_length\": 15, \"lyric\": \"\" }"
///     "  ]"
///     "}";
/// const VoicevoxStyleId kSingingTeacher = 6000;
///
/// char *frame_audio_query;
/// const VoicevoxResultCode result =
///     voicevox_synthesizer_create_sing_frame_audio_query(
///         synthesizer, kScore, kSingingTeacher, &frame_audio_query);
/// ```
/// }
///
/// \safety{
/// - `score_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_frame_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_create_sing_frame_audio_query}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_create_sing_frame_audio_query(
    synthesizer: *const VoicevoxSynthesizer,
    score_json: *const c_char,
    style_id: VoicevoxStyleId,
    output_frame_audio_query_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let score_json = unsafe { CStr::from_ptr(score_json) };

        let score = &Score::validate_json(score_json)?;

        let frame_audio_query = synthesizer
            .body()
            .create_sing_frame_audio_query(score, StyleId::new(style_id))?
            .to_c_json();

        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_frame_audio_query_json.write_unaligned(
                C_STRING_DROP_CHECKER
                    .whitelist(frame_audio_query)
                    .into_raw(),
            );
        }
        Ok(())
    })())
}

// TODO: json-cを活用したexaampleを書く
// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 楽譜と歌唱音声合成用のクエリから、フレームごとの基本周波数を生成する。
///
/// 詳細はユーザーガイド[歌唱音声合成]を参照。
///
/// [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
///
/// 生成したJSONを解放するには ::voicevox_json_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] score_json [`Score`型]を表すJSON
/// @param [in] frame_audio_query_json [`FrameAudioQuery`型]を表すJSON
/// @param [in] style_id スタイルID
/// @param [out] output_f0_json 生成先
///
/// [`Score`型]: ../rust_api/voicevox_core/struct.Score.html
/// [`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
///
/// @returns 結果コード
///
/// \safety{
/// - `score_json`と`frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_f0_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_create_sing_frame_f0}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_create_sing_frame_f0(
    synthesizer: *const VoicevoxSynthesizer,
    score_json: *const c_char,
    frame_audio_query_json: *const c_char,
    style_id: VoicevoxStyleId,
    output_f0_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let score_json = unsafe { CStr::from_ptr(score_json) };
        let frame_audio_query_json = unsafe { CStr::from_ptr(frame_audio_query_json) };

        let score = &Score::validate_json(score_json)?;
        let frame_audio_query = &FrameAudioQuery::validate_json(frame_audio_query_json)?;

        let f0 = synthesizer
            .body()
            .create_sing_frame_f0(score, frame_audio_query, StyleId::new(style_id))?
            .to_c_json();

        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_f0_json.write_unaligned(C_STRING_DROP_CHECKER.whitelist(f0).into_raw());
        }
        Ok(())
    })())
}

// TODO: json-cを活用したexaampleを書く
// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 楽譜と歌唱音声合成用のクエリから、フレームごとの音量を生成する。
///
/// 詳細はユーザーガイド[歌唱音声合成]を参照。
///
/// [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
///
/// 生成したJSONを解放するには ::voicevox_json_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] score_json [`Score`型]を表すJSON
/// @param [in] frame_audio_query_json [`FrameAudioQuery`型]を表すJSON
/// @param [in] style_id スタイルID
/// @param [out] output_volume_json 生成先
///
/// [`Score`型]: ../rust_api/voicevox_core/struct.Score.html
/// [`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
///
/// @returns 結果コード
///
/// \safety{
/// - `score_json`と`frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_volume_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_create_sing_frame_volume}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_create_sing_frame_volume(
    synthesizer: *const VoicevoxSynthesizer,
    score_json: *const c_char,
    frame_audio_query_json: *const c_char,
    style_id: VoicevoxStyleId,
    output_volume_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let score_json = unsafe { CStr::from_ptr(score_json) };
        let score = &Score::validate_json(score_json)?;

        // SAFETY: The safety contract must be upheld by the caller.
        let frame_audio_query_json = unsafe { CStr::from_ptr(frame_audio_query_json) };
        let frame_audio_query = &FrameAudioQuery::validate_json(frame_audio_query_json)?;

        let volume = synthesizer
            .body()
            .create_sing_frame_volume(score, frame_audio_query, StyleId::new(style_id))?
            .to_c_json();

        unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            output_volume_json.write_unaligned(C_STRING_DROP_CHECKER.whitelist(volume).into_raw());
        }
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 歌唱音声合成を行う。
///
/// 詳細はユーザーガイド[歌唱音声合成]を参照。
///
/// [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
///
/// 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
///
/// @param [in] synthesizer 音声シンセサイザ
/// @param [in] frame_audio_query_json [`FrameAudioQuery`型]を表すJSON
/// @param [in] style_id スタイルID
/// @param [out] output_wav_length 出力のバイト長
/// @param [out] output_wav 出力先
///
/// [`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
///
/// @returns 結果コード
///
/// \example{
/// ```c
/// const VoicevoxStyleId kSinger = 3000;
///
/// uint8_t *wav;
/// size_t wav_length;
/// const VoicevoxResultCode result = voicevox_synthesizer_frame_synthesis(
///     synthesizer, frame_audio_query, kSinger, &wav_length, &wav);
/// ```
/// }
///
/// \safety{
/// - `frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_synthesizer_frame_synthesis}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_synthesizer_frame_synthesis(
    synthesizer: *const VoicevoxSynthesizer,
    frame_audio_query_json: *const c_char,
    style_id: VoicevoxStyleId,
    output_wav_length: NonNull<usize>,
    output_wav: NonNull<NonNull<u8>>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let frame_audio_query_json = unsafe { CStr::from_ptr(frame_audio_query_json) };
        let frame_audio_query = &FrameAudioQuery::validate_json(frame_audio_query_json)?;

        let output = synthesizer
            .body()
            .frame_synthesis(frame_audio_query, StyleId::new(style_id))
            .perform()?;

        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { U8_SLICE_OWNER.own_and_lend(output, output_wav, output_wav_length) };
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// JSON文字列を解放する。
///
/// @param [in] json 解放するJSON文字列。nullable
///
/// \safety{
/// - `json`がヌルポインタでないならば、以下のAPIで得られたポインタでなくてはいけない。
///     - ::voicevox_audio_query_create_from_accent_phrases
///     - ::voicevox_onnxruntime_create_supported_devices_json
///     - ::voicevox_voice_model_file_create_metas_json
///     - ::voicevox_open_jtalk_rc_analyze
///     - ::voicevox_synthesizer_create_metas_json
///     - ::voicevox_synthesizer_create_audio_query
///     - ::voicevox_synthesizer_create_audio_query_from_kana
///     - ::voicevox_synthesizer_create_accent_phrases
///     - ::voicevox_synthesizer_create_accent_phrases_from_kana
///     - ::voicevox_synthesizer_replace_mora_data
///     - ::voicevox_synthesizer_replace_phoneme_length
///     - ::voicevox_synthesizer_replace_mora_pitch
///     - ::voicevox_synthesizer_create_sing_frame_audio_query
///     - ::voicevox_synthesizer_create_sing_frame_f0
///     - ::voicevox_synthesizer_create_sing_frame_volume
///     - ::voicevox_user_dict_to_json
/// - 文字列の長さは生成時より変更されていてはならない。
/// - `json`がヌルポインタでないならば、<a href="#voicevox-core-safety">読み込みと書き込みについて有効</a>でなければならない。
/// - `json`がヌルポインタでないならば、以後<b>ダングリングポインタ</b>(_dangling pointer_)として扱われなくてはならない。
/// }
///
/// \no-orig-impl{voicevox_json_free}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_json_free(json: *mut c_char) {
    init_logger_once();
    if let Some(json) = C_STRING_DROP_CHECKER.check(json) {
        // SAFETY: The safety contract must be upheld by the caller.
        drop(unsafe { CString::from_raw(json.as_ptr()) });
    }
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// WAVデータを解放する。
///
/// @param [in] wav 解放するWAVデータ。nullable
///
/// \safety{
/// - `wav`がヌルポインタでないならば、以下のAPIで得られたポインタでなくてはいけない。
///     - ::voicevox_synthesizer_synthesis
///     - ::voicevox_synthesizer_tts
///     - ::voicevox_synthesizer_tts_from_kana
///     - ::voicevox_synthesizer_frame_synthesis
/// - `wav`がヌルポインタでないならば、<a href="#voicevox-core-safety">読み込みと書き込みについて有効</a>でなければならない。
/// - `wav`がヌルポインタでないならば、以後<b>ダングリングポインタ</b>(_dangling pointer_)として扱われなくてはならない。
/// }
///
/// \no-orig-impl{voicevox_wav_free}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_wav_free(wav: *mut u8) {
    init_logger_once();
    U8_SLICE_OWNER.drop_for(wav);
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 結果コードに対応したメッセージ文字列を取得する。
///
/// @param [in] result_code 結果コード
///
/// @returns 結果コードに対応したメッセージ文字列
///
/// \examples{
/// ```c
/// const char *actual = voicevox_error_result_to_message(VOICEVOX_RESULT_OK);
/// const char *EXPECTED = "エラーが発生しませんでした";
/// assert(strcmp(actual, EXPECTED) == 0);
/// ```
///
/// ```c
/// const char *actual =
///     voicevox_error_result_to_message(VOICEVOX_RESULT_LOAD_MODEL_ERROR);
/// const char *EXPECTED = "modelデータ読み込みに失敗しました";
/// assert(strcmp(actual, EXPECTED) == 0);
/// ```
/// }
///
/// \no-orig-impl{voicevox_error_result_to_message}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_error_result_to_message(
    result_code: VoicevoxResultCode,
) -> *const c_char {
    init_logger_once();
    let message = result_code::error_result_to_message(result_code);
    C_STRING_DROP_CHECKER.blacklist(message).as_ptr()
}

/// ユーザー辞書。
///
/// \orig-impl{VoicevoxUserDict}
#[derive(Debug, Educe)]
#[educe(Default(expression = "Self { _padding: MaybeUninit::uninit() }"))]
pub struct VoicevoxUserDict {
    _padding: MaybeUninit<[u8; 1]>,
}

/// ユーザー辞書の単語。
///
/// \orig-impl{VoicevoxUserDictWord}
#[derive(Clone, Copy)]
#[repr(C)]
pub struct VoicevoxUserDictWord {
    /// 表記
    surface: *const c_char,
    /// 読み
    pronunciation: *const c_char,
    /// アクセント型
    accent_type: usize,
    /// 単語の種類
    word_type: VoicevoxUserDictWordType,
    /// 優先度
    priority: u32,
}

/// ユーザー辞書の単語の種類。
///
/// \orig-impl{VoicevoxUserDictWordType}
#[repr(i32)]
#[allow(
    non_camel_case_types,
    reason = "実際に公開するC APIとの差異をできるだけ少なくするため"
)]
#[derive(Copy, Clone)]
pub enum VoicevoxUserDictWordType {
    /// 固有名詞。
    VOICEVOX_USER_DICT_WORD_TYPE_PROPER_NOUN = 0,
    /// 一般名詞。
    VOICEVOX_USER_DICT_WORD_TYPE_COMMON_NOUN = 1,
    /// 動詞。
    VOICEVOX_USER_DICT_WORD_TYPE_VERB = 2,
    /// 形容詞。
    VOICEVOX_USER_DICT_WORD_TYPE_ADJECTIVE = 3,
    /// 接尾辞。
    VOICEVOX_USER_DICT_WORD_TYPE_SUFFIX = 4,
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ::VoicevoxUserDictWord を最低限のパラメータで作成する。
///
/// @param [in] surface 表記
/// @param [in] pronunciation 読み
/// @param [in] accent_type アクセント型
/// @returns ::VoicevoxUserDictWord
///
/// \orig-impl{voicevox_user_dict_word_make}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_user_dict_word_make(
    surface: *const c_char,
    pronunciation: *const c_char,
    accent_type: usize,
) -> VoicevoxUserDictWord {
    init_logger_once();
    VoicevoxUserDictWord {
        surface,
        pronunciation,
        accent_type,
        word_type: DEFAULT_WORD_TYPE.into(),
        priority: DEFAULT_PRIORITY,
    }
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ユーザー辞書をb>構築</b>(_construct_)する。
///
/// @returns ::VoicevoxUserDict
///
/// \orig-impl{voicevox_user_dict_new}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_user_dict_new() -> NonNull<VoicevoxUserDict> {
    init_logger_once();
    VoicevoxUserDict::new(Default::default())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ユーザー辞書にファイルを読み込ませる。
///
/// @param [in] user_dict ユーザー辞書
/// @param [in] dict_path 読み込む辞書ファイルのパス
/// @returns 結果コード
///
/// \safety{
/// - `dict_path`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_user_dict_load}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_user_dict_load(
    user_dict: *const VoicevoxUserDict,
    dict_path: *const c_char,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        let dict_path = ensure_utf8(unsafe { CStr::from_ptr(dict_path) })?;
        user_dict.body().load(dict_path)?;

        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ユーザー辞書に単語を追加する。
///
/// @param [in] ユーザー辞書
/// @param [in] word 追加する単語
/// @param [out] output_word_uuid 追加した単語のUUID
/// @returns 結果コード
///
/// # Safety
/// @param user_dict は有効な :VoicevoxUserDict のポインタであること
///
/// \safety{
/// - `word->surface`と`word->pronunciation`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `output_word_uuid`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_user_dict_add_word}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_user_dict_add_word(
    user_dict: *const VoicevoxUserDict,
    word: *const VoicevoxUserDictWord,
    output_word_uuid: NonNull<[u8; 16]>,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let word = unsafe { word.read_unaligned().try_into_word() }?;
        let uuid = user_dict.body().add_word(word)?;
        // SAFETY: The safety contract must be upheld by the caller.
        unsafe { output_word_uuid.write_unaligned(uuid.into_bytes()) };

        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ユーザー辞書の単語を更新する。
///
/// @param [in] user_dict ユーザー辞書
/// @param [in] word_uuid 更新する単語のUUID
/// @param [in] word 新しい単語のデータ
/// @returns 結果コード
///
/// \safety{
/// - `word_uuid`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// - `word->surface`と`word->pronunciation`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_user_dict_update_word}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_user_dict_update_word(
    user_dict: *const VoicevoxUserDict,
    word_uuid: &[u8; 16],
    word: *const VoicevoxUserDictWord,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        let word_uuid = Uuid::from_slice(word_uuid).map_err(CApiError::InvalidUuid)?;
        // SAFETY: The safety contract must be upheld by the caller.
        let word = unsafe { word.read_unaligned().try_into_word() }?;
        user_dict.body().update_word(word_uuid, word)?;

        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ユーザー辞書から単語を削除する。
///
/// @param [in] user_dict ユーザー辞書
/// @param [in] word_uuid 削除する単語のUUID
/// @returns 結果コード
///
/// \safety{
/// - `word_uuid`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_user_dict_remove_word}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_user_dict_remove_word(
    user_dict: *const VoicevoxUserDict,
    word_uuid: &[u8; 16],
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        let word_uuid = Uuid::from_slice(word_uuid).map_err(CApiError::InvalidUuid)?;
        user_dict.body().remove_word(word_uuid)?;
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
// FIXME: infallibleなので、`char*`を戻り値にしてもよいはず
/// ユーザー辞書の単語をJSON形式で出力する。
///
/// 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
///
/// @param [in] user_dict ユーザー辞書
/// @param [out] output_json 出力先
/// @returns 結果コード
///
/// \safety{
/// - `output_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_user_dict_to_json}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_user_dict_to_json(
    user_dict: *const VoicevoxUserDict,
    output_json: NonNull<*mut c_char>,
) -> VoicevoxResultCode {
    init_logger_once();
    let json = user_dict.body().to_json();
    let json = CString::new(json).expect("\\0を含まない文字列であることが保証されている");
    // SAFETY: The safety contract must be upheld by the caller.
    unsafe { output_json.write_unaligned(C_STRING_DROP_CHECKER.whitelist(json).into_raw()) };
    VoicevoxResultCode::VOICEVOX_RESULT_OK
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// 他のユーザー辞書をインポートする。
///
/// @param [in] user_dict ユーザー辞書
/// @param [in] other_dict インポートするユーザー辞書
/// @returns 結果コード
///
/// \orig-impl{voicevox_user_dict_import}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_user_dict_import(
    user_dict: *const VoicevoxUserDict,
    other_dict: *const VoicevoxUserDict,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        user_dict.body().import(&other_dict.body())?;
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ユーザー辞書をファイルに保存する。
///
/// @param [in] user_dict ユーザー辞書
/// @param [in] path 保存先のファイルパス
///
/// \safety{
/// - `path`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
/// }
///
/// \orig-impl{voicevox_user_dict_save}
#[unsafe(no_mangle)]
pub unsafe extern "C" fn voicevox_user_dict_save(
    user_dict: *const VoicevoxUserDict,
    path: *const c_char,
) -> VoicevoxResultCode {
    init_logger_once();
    into_result_code_with_error((|| {
        // SAFETY: The safety contract must be upheld by the caller.
        let path = ensure_utf8(unsafe { CStr::from_ptr(path) })?;
        user_dict.body().save(path)?;
        Ok(())
    })())
}

// SAFETY: voicevox_core_c_apiを構成するライブラリの中に、これと同名のシンボルは存在しない
/// ユーザー辞書を<b>破棄</b>(_destruct_)する。
///
/// 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
///
/// この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
///
/// @param [in] user_dict 破棄対象。nullable
///
/// \no-orig-impl{voicevox_user_dict_delete}
#[unsafe(no_mangle)]
pub extern "C" fn voicevox_user_dict_delete(user_dict: *mut VoicevoxUserDict) {
    init_logger_once();
    user_dict.drop_body();
}
