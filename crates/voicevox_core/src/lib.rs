//! 無料で使える中品質なテキスト読み上げソフトウェア、VOICEVOXのコア。
//!
//! # Feature flags
//!
//! - **`buildtime-download-onnxruntime`**: ビルド時に後述する環境変数`VVCORE_BUILD_DOWNLOAD_AND_COPY_ORT`が`1`なら、ONNX
//!   Runtimeのバイナリをダウンロードしてtarget
//!   directory内の複数箇所に配置する。`VVCORE_BUILD_DOWNLOAD_AND_COPY_ORT`が`1`ではないなら警告を出して何もしない。後述の`link-onnxruntime`フィーチャと合わせると、システムにONNX Runtimeが無くてもビルドが可能になる。
//! - **`load-onnxruntime`**: ONNX Runtimeを`dlopen`/`LoadLibraryExW`で開く。[CUDA]と[DirectML]が利用可能。
//!   またmuslをターゲットとしたビルドでは`dlopen`をサポートしないため、このフラグは利用不可であるため、`link-onnxruntime`を利用する必要がある。
//! - **`link-onnxruntime`**: ONNX Runtimeをロード時動的リンクする。そのためビルドするためにはシステムにONNX
//!   Runtimeがインストールされているか、`buildtime-download-onnxruntime`によるダウンロードを行う必要がある。iOSのような`dlopen`の利用が困難な環境でのみこちらを利用するべきである。_Note_:
//!   [動的リンク対象のライブラリ名]は`onnxruntime`で固定。変更は`patchelf(1)`や`install_name_tool(1)`で行うこと。また、[ONNX RuntimeのGPU機能]を使うことは不可。
//!
//! このクレートの利用にあたっては上記の`load-onnxruntime`か`link-onnxruntime`のうちどちらかを有効にしなければならない。両方の有効化はコンパイルエラーとなる。[`Onnxruntime`]の初期化方法はこれらのフィーチャによって決まる。
//!
//! # Build time environment variables
//!
//! - **`VVCORE_BUILD_DOWNLOAD_AND_COPY_ORT`**: `buildtime-download-onnxruntime`フィーチャが有効化されているときのみ機能する。`1`のとき、ONNX
//!   Runtimeのバイナリをダウンロードしてtarget
//!   directory内の複数箇所に配置する。`buildtime-download-onnxruntime`フィーチャが無効化されているときは値が`1`であっても、警告のみを出しダウンロードは行わない。
//!
//! [Cargoフィーチャ]: https://doc.rust-lang.org/stable/cargo/reference/features.html
//! [CUDA]: https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html
//! [DirectML]: https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html
//! [動的リンク対象のライブラリ名]:
//! https://doc.rust-lang.org/cargo/reference/build-scripts.html#rustc-link-lib
//! [`Onnxruntime`]: blocking::Onnxruntime
//! [ONNX RuntimeのGPU機能]: https://onnxruntime.ai/docs/execution-providers/
//!
//! # Examples
//!
//! ```
//! //! トーク
//!
//! use std::{io::Write as _, panic};
//!
//! use anyhow::Context as _;
//! use const_format::concatcp;
//!
//! use voicevox_core::{
//!     blocking::{Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile},
//!     CharacterMeta, StyleMeta,
//! };
//!
//! // ダウンローダーにて`onnxruntime`としてダウンロードできるもの
//! # #[cfg(false)]
//! const VVORT: &str = concatcp!(
//!     "./voicevox_core/onnxruntime/lib/",
//!     Onnxruntime::LIB_VERSIONED_FILENAME,
//! );
//! # use test_util::ONNXRUNTIME_DYLIB_PATH as VVORT;
//!
//! // ダウンローダーにて`dict`としてダウンロードできるもの
//! # #[cfg(false)]
//! const OJT_DIC: &str = "./voicevox_core/dict/open_jtalk_dic_utf_8-1.11";
//! # use test_util::OPEN_JTALK_DIC_DIR as OJT_DIC;
//!
//! // ダウンローダーにて`models`としてダウンロードできるもの
//! # #[cfg(false)]
//! const VVM: &str = "./voicevox_core/models/vvms/0.vvm";
//! # use test_util::SAMPLE_VOICE_MODEL_FILE_PATH as VVM;
//!
//! # #[cfg(false)]
//! const TARGET_CHARACTER_NAME: &str = "ずんだもん";
//! # const TARGET_CHARACTER_NAME: &str = "dummy1";
//! #
//! # #[cfg(false)]
//! const TARGET_STYLE_NAME: &str = "ノーマル";
//! # const TARGET_STYLE_NAME: &str = "style1";
//! #
//! const TEXT: &str = "こんにちは";
//!
//! let synth = {
//!     let ort = Onnxruntime::load_once().filename(VVORT).perform()?;
//!     let ojt = OpenJtalk::new(OJT_DIC)?;
//!     Synthesizer::builder(ort).text_analyzer(ojt).build()?
//! };
//!
//! dbg!(synth.is_gpu_mode());
//!
//! synth
//!     .load_voice_model(&VoiceModelFile::open(VVM)?)
//!     .perform()?;
//!
//! let StyleMeta { id: style_id, .. } = synth
//!     .metas()
//!     .into_iter()
//!     .filter(|CharacterMeta { name, .. }| name == TARGET_CHARACTER_NAME)
//!     .flat_map(|CharacterMeta { styles, .. }| styles)
//!     .find(|StyleMeta { name, .. }| name == TARGET_STYLE_NAME)
//!     .with_context(|| {
//!         format!("could not find \"{TARGET_CHARACTER_NAME} ({TARGET_STYLE_NAME})\"")
//!     })?;
//!
//! eprintln!("Synthesizing");
//! let wav = &synth.tts(TEXT, style_id).perform()?;
//!
//! eprintln!("Playing the WAV");
//! # if false {
//! play(wav)?;
//! # }
//!
//! fn play(wav: &[u8]) -> anyhow::Result<()> {
//!     let tempfile = tempfile::Builder::new().suffix(".wav").tempfile()?;
//!     (&tempfile).write_all(wav)?;
//!     let tempfile = &tempfile.into_temp_path();
//!     open::that_in_background(tempfile)
//!         .join()
//!         .unwrap_or_else(|e| panic::resume_unwind(e))?;
//!     Ok(())
//! }
//! # Ok::<_, anyhow::Error>(())
//! ```
//!
//! ```
//! //! ソング
//!
//! use std::{io::Write as _, panic};
//!
//! use anyhow::Context as _;
//! use const_format::concatcp;
//!
//! use voicevox_core::{
//!     blocking::{Onnxruntime, Synthesizer, VoiceModelFile},
//!     CharacterMeta, Score, StyleMeta, StyleType,
//! };
//!
//! // ダウンローダーにて`onnxruntime`としてダウンロードできるもの
//! # #[cfg(false)]
//! const VVORT: &str = concatcp!(
//!     "./voicevox_core/onnxruntime/lib/",
//!     Onnxruntime::LIB_VERSIONED_FILENAME,
//! );
//! # use test_util::ONNXRUNTIME_DYLIB_PATH as VVORT;
//!
//! // ダウンローダーにて`models`としてダウンロードできるもの
//! # #[cfg(false)]
//! const VVM: &str = "./voicevox_core/models/vvms/s0.vvm";
//! # use test_util::SAMPLE_VOICE_MODEL_FILE_PATH as VVM;
//!
//! # #[cfg(false)]
//! const SINGING_TEACHER_CHARACTER_NAME: &str = "波音リツ";
//! # const SINGING_TEACHER_CHARACTER_NAME: &str = "dummy4";
//! #
//! # #[cfg(false)]
//! const SINGING_TEACHER_STYLE_NAME: &str = "ノーマル";
//! # const SINGING_TEACHER_STYLE_NAME: &str = "style4-2";
//! #
//! # #[cfg(false)]
//! const SINGER_CHARACTER_NAME: &str = "ずんだもん";
//! # const SINGER_CHARACTER_NAME: &str = "dummy4";
//! #
//! # #[cfg(false)]
//! const SINGER_STYLE_NAME: &str = "ノーマル";
//! # const SINGER_STYLE_NAME: &str = "style4-1";
//!
//! let synth = {
//!     let ort = Onnxruntime::load_once().filename(VVORT).perform()?;
//!     Synthesizer::builder(ort).build()?
//! };
//!
//! dbg!(synth.is_gpu_mode());
//!
//! synth
//!     .load_voice_model(&VoiceModelFile::open(VVM)?)
//!     .perform()?;
//!
//! let metas = &synth.metas();
//! let find_style = |character_name, style_name, style_types: &[_]| {
//!     metas
//!         .iter()
//!         .filter(|CharacterMeta { name, .. }| name == character_name)
//!         .flat_map(|CharacterMeta { styles, .. }| styles)
//!         .find(|StyleMeta { name, r#type, .. }| {
//!             name == style_name && style_types.contains(r#type)
//!         })
//!         .map(|&StyleMeta { id, .. }| id)
//!         .with_context(|| format!("could not find \"{character_name} ({style_name})\""))
//! };
//!
//! let singing_teacher = find_style(
//!     SINGING_TEACHER_CHARACTER_NAME,
//!     SINGING_TEACHER_STYLE_NAME,
//!     &[StyleType::SingingTeacher, StyleType::Sing],
//! )?;
//!
//! let singer = find_style(
//!     SINGER_CHARACTER_NAME,
//!     SINGER_STYLE_NAME,
//!     &[StyleType::FrameDecode],
//! )?;
//!
//! let score = &serde_json::from_str::<Score>(
//!     r#"
//! {
//!   "notes": [
//!     { "key": null, "frame_length": 15, "lyric": "" },
//!     { "key": 60, "frame_length": 45, "lyric": "ド" },
//!     { "key": 62, "frame_length": 45, "lyric": "レ" },
//!     { "key": 64, "frame_length": 45, "lyric": "ミ" },
//!     { "key": null, "frame_length": 15, "lyric": "" }
//!   ]
//! }
//!     "#,
//! )
//! .unwrap();
//!
//! let frame_audio_query = &synth.create_sing_frame_audio_query(score, singing_teacher)?;
//!
//! eprintln!("Synthesizing");
//! let wav = &synth
//!     .frame_synthesis(&frame_audio_query, singer)
//!     .perform()?;
//!
//! eprintln!("Playing the WAV");
//! # if false {
//! play(wav)?;
//! # }
//!
//! fn play(wav: &[u8]) -> anyhow::Result<()> {
//!     let tempfile = tempfile::Builder::new().suffix(".wav").tempfile()?;
//!     (&tempfile).write_all(wav)?;
//!     let tempfile = &tempfile.into_temp_path();
//!     open::that_in_background(tempfile)
//!         .join()
//!         .unwrap_or_else(|e| panic::resume_unwind(e))?;
//!     Ok(())
//! }
//! # Ok::<_, anyhow::Error>(())
//! ```
//!
//! # 音声の調整
//!
//! ユーザーガイドの[テキスト音声合成の流れ]を参照。
//!
//! 以下の`wav1`から`wav4`はすべて同一となる。
//!
//! [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
//!
//! ```
//! use std::collections::HashSet;
//!
//! use voicevox_core::{
//!     blocking::{Synthesizer, TextAnalyzer},
//!     AudioQuery, StyleId,
//! };
//! #
//! # use test_util::{ONNXRUNTIME_DYLIB_PATH, OPEN_JTALK_DIC_DIR, SAMPLE_VOICE_MODEL_FILE_PATH};
//! # use voicevox_core::blocking::{Onnxruntime, OpenJtalk, VoiceModelFile};
//!
//! fn f(synth: &Synthesizer<impl TextAnalyzer>) -> anyhow::Result<()> {
//! #    const TEXT: &str = "";
//! #   #[cfg(false)]
//!     const TEXT: &str = _;
//! #
//! #   const STYLE_ID: StyleId = StyleId(0);
//! #   #[cfg(false)]
//!     const STYLE_ID: StyleId = _;
//!
//!     let wav1 = synth.tts(TEXT, STYLE_ID).perform()?;
//!
//!     let wav2 = {
//!         let query = synth.create_audio_query(TEXT, STYLE_ID)?;
//!         synth.synthesis(&query, STYLE_ID).perform()?
//!     };
//!
//!     let wav3 = {
//!         let phrases = synth.create_accent_phrases(TEXT, STYLE_ID)?;
//!         let query = AudioQuery::from(phrases);
//!         synth.synthesis(&query, STYLE_ID).perform()?
//!     };
//!
//!     let wav4 = {
//!         let phrases = synth.text_analyzer().analyze(TEXT)?;
//!         let phrases = synth.replace_mora_data(&phrases, STYLE_ID)?;
//!         let query = AudioQuery::from(phrases);
//!         synth.synthesis(&query, STYLE_ID).perform()?
//!     };
//!
//!     let wav5 = {
//!         let phrases = synth.text_analyzer().analyze(TEXT)?;
//!         let phrases = synth.replace_phoneme_length(&phrases, STYLE_ID)?;
//!         let phrases = synth.replace_mora_pitch(&phrases, STYLE_ID)?;
//!         let query = AudioQuery::from(phrases);
//!         synth.synthesis(&query, STYLE_ID).perform()?
//!     };
//!
//!     assert_eq!(1, HashSet::from([wav1, wav2, wav3, wav4, wav5]).len());
//!     Ok(())
//! }
//! #
//! # let synth = &{
//! #     let ort = Onnxruntime::load_once()
//! #         .filename(ONNXRUNTIME_DYLIB_PATH)
//! #         .perform()?;
//! #     let ojt = OpenJtalk::new(OPEN_JTALK_DIC_DIR)?;
//! #     Synthesizer::builder(ort).text_analyzer(ojt).build()?
//! # };
//! # synth
//! #     .load_voice_model(&VoiceModelFile::open(SAMPLE_VOICE_MODEL_FILE_PATH)?)
//! #     .perform()?;
//! # f(synth)?;
//! # anyhow::Ok(())
//! ```

#![cfg_attr(docsrs, feature(doc_cfg))]

#[cfg(all(not(doc), feature = "load-onnxruntime", feature = "link-onnxruntime"))]
compile_error!("`load-onnxruntime` and `link-onnxruntime` cannot be enabled at the same time");

#[cfg(all(not(feature = "load-onnxruntime"), feature = "link-onnxruntime"))]
const _: () = {
    use dummy::*;

    #[expect(unused_imports)]
    use ort::*;

    #[expect(path_statements)]
    init_from; // PLEASE READ → : when `link-onnxruntime` is enabled,`ort/load-dynamic` must be disabled

    mod dummy {
        #[expect(non_upper_case_globals)]
        pub(super) const init_from: () = ();
    }
};

/// ```compile_fail
/// use voicevox_core::__doc;
/// ```
#[cfg(doc)]
#[cfg_attr(docsrs, doc(cfg(doc)))]
pub mod __doc {
    /// [C API]にある以下のアイテムは、Rust API、つまりこのクレートには存在しない。
    ///
    /// | | 理由 |
    /// | :- | :- |
    /// | `VoicevoxLoadOnnxruntimeOptions` | ビルダースタイルであるため |
    /// | `VoicevoxInitializeOptions` | 〃 |
    /// | `VoicevoxSynthesisOptions` | 〃 |
    /// | `VoicevoxTtsOptions` | 〃 |
    /// | `voicevox_make_default_load_onnxruntime_options` | 〃 |
    /// | `voicevox_make_default_initialize_options` | 〃 |
    /// | `voicevox_make_default_load_voice_model_options` | 〃 |
    /// | `voicevox_make_default_synthesis_options` | 〃 |
    /// | `voicevox_make_default_tts_options` | 〃 |
    /// | `voicevox_json_free` | [Rustのデストラクタ機構]があるため |
    /// | `voicevox_wav_free` | 〃 |
    /// | `voicevox_open_jtalk_rc_delete` | 〃 |
    /// | `voicevox_synthesizer_delete` | 〃 |
    /// | `voicevox_voice_model_file_delete` | 〃 |
    /// | `voicevox_user_dict_delete` | 〃 |
    /// | `voicevox_error_result_to_message` | [`std::error::Error`]としてのエラー表示があるため |
    /// | `voicevox_frame_phoneme_validate` | 型システムで不正な状態を完全に防げるため |
    ///
    /// [C API]: https://voicevox.github.io/voicevox_core/apis/c_api/voicevox__core_8h.html
    /// [Rustのデストラクタ機構]: https://doc.rust-lang.org/reference/destructors.html
    #[doc(alias(
        "VoicevoxLoadOnnxruntimeOptions",
        "VoicevoxInitializeOptions",
        "VoicevoxSynthesisOptions",
        "VoicevoxTtsOptions",
        "voicevox_make_default_load_onnxruntime_options",
        "voicevox_make_default_initialize_options",
        "voicevox_make_default_synthesis_options",
        "voicevox_make_default_tts_options",
        "voicevox_json_free",
        "voicevox_wav_free",
        "voicevox_open_jtalk_rc_delete",
        "voicevox_synthesizer_delete",
        "voicevox_voice_model_file_delete",
        "voicevox_user_dict_delete",
        "voicevox_error_result_to_message",
        "voicevox_frame_phoneme_validate"
    ))]
    pub mod C_APIには存在するがRust_APIには存在しないアイテム {}

    /// 以下の型は[Serde]でのシリアライズ・デシリアライズに対応している。
    ///
    /// - [`SupportedDevices`]（シリアライズのみ）
    /// - [`VoiceModelId`]
    /// - <code>[VoiceModelMeta] = [Vec]\<[CharacterMeta]\></code>
    ///     - [`StyleMeta`]
    ///         - [`StyleId`]
    ///         - [`StyleType`]
    ///     - [`CharacterVersion`]
    /// - [`UserDictWord`]
    ///     - [`UserDictWordType`]
    /// - [`AudioQuery`]
    ///     - [`AccentPhrase`]
    ///     - [`Mora`]
    /// - [`Score`]
    ///     - [`Note`]
    ///         - [`NoteId`]
    ///         - [`OptionalLyric`]
    /// - [`FrameAudioQuery`]
    ///     - [`FramePhoneme`]
    ///         - [`Phoneme`]
    ///             - [`Sil`]
    ///         - `NoteId`
    ///     - [`SamplingRate`]
    ///
    /// [C API]が取り扱うJSONもSerdeの実装に従っている。
    ///
    /// VOICEVOX COREとしての方針については[データのシリアライゼーション]を参照。
    ///
    /// [Serde]: https://serde.rs
    /// [`SupportedDevices`]: crate::SupportedDevices
    /// [`VoiceModelId`]: crate::VoiceModelId
    /// [VoiceModelMeta]: crate::VoiceModelMeta
    /// [Vec]: std::vec::Vec
    /// [CharacterMeta]: crate::CharacterMeta
    /// [`StyleMeta`]: crate::StyleMeta
    /// [`StyleId`]: crate::StyleId
    /// [`StyleType`]: crate::StyleType
    /// [`CharacterVersion`]: crate::CharacterVersion
    /// [`UserDictWord`]: crate::UserDictWord
    /// [`UserDictWordType`]: crate::UserDictWordType
    /// [`AudioQuery`]: crate::AudioQuery
    /// [`AccentPhrase`]: crate::AccentPhrase
    /// [`Mora`]: crate::Mora
    /// [`Score`]: crate::Score
    /// [`Note`]: crate::Note
    /// [`NoteId`]: crate::NoteId
    /// [`OptionalLyric`]: crate::OptionalLyric
    /// [`FrameAudioQuery`]: crate::FrameAudioQuery
    /// [`FramePhoneme`]: crate::FramePhoneme
    /// [`Phoneme`]: crate::Phoneme
    /// [`Sil`]: crate::Sil
    /// [`SamplingRate`]: crate::SamplingRate
    /// [C API]: https://voicevox.github.io/voicevox_core/apis/c_api/voicevox__core_8h.html
    /// [データのシリアライゼーション]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/serialization.md
    pub mod Serde対応 {}
}

mod asyncs;
mod collections;
mod convert;
mod core;
/// cbindgen:ignore
mod engine;
mod error;
mod future;
mod macros;
mod numerics;
mod result;
mod synthesizer;
mod task;
mod version;

#[doc(hidden)]
pub mod __internal;
pub mod blocking;
pub mod nonblocking;

#[cfg(test)]
mod test_util;

#[expect(
    clippy::single_component_path_imports,
    reason = "https://crates.io/crates/rstest_reuse/0.6.0#use-rstest_resuse-at-the-top-of-your-crate"
)]
#[cfg(test)]
use rstest_reuse;

pub use self::{
    core::{
        devices::SupportedDevices,
        metas::{CharacterMeta, CharacterVersion, StyleId, StyleMeta, StyleType, VoiceModelMeta},
        status::OnExistingVoiceModelId,
        voice_model::VoiceModelId,
    },
    engine::{
        Phoneme, SamplingRate, Sil,
        song::{
            queries::{FrameAudioQuery, FramePhoneme, Key, Note, NoteId, OptionalLyric, Score},
            validate::ensure_compatible,
        },
        talk::{
            AccentPhrase, AudioQuery, Mora,
            user_dict::{UserDictWord, UserDictWordBuilder, UserDictWordType},
        },
    },
    error::{Error, ErrorKind},
    result::Result,
    synthesizer::AccelerationMode,
    version::VERSION,
};

// TODO: 後で復活させる
// https://github.com/VOICEVOX/voicevox_core/issues/970
#[doc(hidden)]
pub use self::engine::wav_from_s16le as __wav_from_s16le;
