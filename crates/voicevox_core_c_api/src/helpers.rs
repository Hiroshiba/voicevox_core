use easy_ext::ext;
use std::{
    error::Error as _,
    ffi::{CStr, CString},
    fmt::Debug,
    iter,
};
use typed_floats::{NonNaNFinite, PositiveFinite};
use uuid::Uuid;
use voicevox_core::{
    __internal::interop::Validate, AccelerationMode, AccentPhrase, AudioQuery, FrameAudioQuery,
    FramePhoneme, Mora, Note, Score, UserDictWord, VoiceModelId,
};

use duplicate::duplicate_item;
use either::Either;
use thiserror::Error;
use tracing::error;

use crate::{
    VoicevoxAccelerationMode, VoicevoxInitializeOptions, VoicevoxLoadVoiceModelOptions,
    VoicevoxOnExistingVoiceModelId, VoicevoxSynthesisOptions, VoicevoxTtsOptions,
    VoicevoxUserDictWord, VoicevoxUserDictWordType, result_code::VoicevoxResultCode,
};

pub(crate) fn into_result_code_with_error(result: CApiResult<()>) -> VoicevoxResultCode {
    if let Err(err) = &result {
        display_error(err);
    }
    return into_result_code(result);

    fn into_result_code(result: CApiResult<()>) -> VoicevoxResultCode {
        use CApiError::*;
        use VoicevoxResultCode::*;
        use voicevox_core::ErrorKind::*;

        match result {
            Ok(()) => VOICEVOX_RESULT_OK,
            Err(RustApi(err)) => match err.kind() {
                NotLoadedOpenjtalkDict => VOICEVOX_RESULT_NOT_LOADED_OPENJTALK_DICT_ERROR,
                GpuSupport => VOICEVOX_RESULT_GPU_SUPPORT_ERROR,
                InitInferenceRuntime => VOICEVOX_RESULT_INIT_INFERENCE_RUNTIME_ERROR,
                OpenZipFile => VOICEVOX_RESULT_OPEN_ZIP_FILE_ERROR,
                ReadZipEntry => VOICEVOX_RESULT_READ_ZIP_ENTRY_ERROR,
                InvalidModelFormat => VOICEVOX_RESULT_INVALID_MODEL_HEADER_ERROR,
                ModelAlreadyLoaded => VOICEVOX_RESULT_MODEL_ALREADY_LOADED_ERROR,
                StyleAlreadyLoaded => VOICEVOX_RESULT_STYLE_ALREADY_LOADED_ERROR,
                InvalidModelData => VOICEVOX_RESULT_INVALID_MODEL_DATA_ERROR,
                GetSupportedDevices => VOICEVOX_RESULT_GET_SUPPORTED_DEVICES_ERROR,
                StyleNotFound => VOICEVOX_RESULT_STYLE_NOT_FOUND_ERROR,
                ModelNotFound => VOICEVOX_RESULT_MODEL_NOT_FOUND_ERROR,
                RunModel => VOICEVOX_RESULT_RUN_MODEL_ERROR,
                AnalyzeText => VOICEVOX_RESULT_ANALYZE_TEXT_ERROR,
                ParseKana => VOICEVOX_RESULT_PARSE_KANA_ERROR,
                LoadUserDict => VOICEVOX_RESULT_LOAD_USER_DICT_ERROR,
                SaveUserDict => VOICEVOX_RESULT_SAVE_USER_DICT_ERROR,
                WordNotFound => VOICEVOX_RESULT_USER_DICT_WORD_NOT_FOUND_ERROR,
                UseUserDict => VOICEVOX_RESULT_USE_USER_DICT_ERROR,
                InvalidWord => VOICEVOX_RESULT_INVALID_USER_DICT_WORD_ERROR,
                InvalidQuery => {
                    panic!(
                        "the audio query (or accent phrases) should have been validated \
                         beforehand with one of the `validate_` functions",
                    );
                }
                IncompatibleQueries => VOICEVOX_RESULT_INCOMPATIBLE_QUERIES_ERROR,
                __NonExhaustive => unreachable!(),
            },
            Err(InvalidUtf8Input) => VOICEVOX_RESULT_INVALID_UTF8_INPUT_ERROR,
            Err(InvalidAudioQuery(_)) => VOICEVOX_RESULT_INVALID_AUDIO_QUERY_ERROR,
            Err(InvalidAccentPhrase(_)) => VOICEVOX_RESULT_INVALID_ACCENT_PHRASE_ERROR,
            Err(InvalidMora(_)) => VOICEVOX_RESULT_INVALID_MORA_ERROR,
            Err(InvalidScore(_)) => VOICEVOX_RESULT_INVALID_SCORE_ERROR,
            Err(InvalidNote(_)) => VOICEVOX_RESULT_INVALID_NOTE_ERROR,
            Err(InvalidFrameAudioQuery(_)) => VOICEVOX_RESULT_INVALID_FRAME_AUDIO_QUERY_ERROR,
            Err(InvalidFramePhoneme(_)) => VOICEVOX_RESULT_INVALID_FRAME_PHONEME_ERROR,
            Err(InvalidUuid(_)) => VOICEVOX_RESULT_INVALID_UUID_ERROR,
        }
    }
}

pub(crate) fn display_error(err: &impl std::error::Error) {
    iter::chain(
        [err.to_string()],
        iter::successors(err.source(), |&e| e.source()).map(|e| format!("Caused by: {e}")),
    )
    .for_each(|msg| error!("{msg}"));
}

pub(crate) type CApiResult<T> = std::result::Result<T, CApiError>;

#[derive(Error, Debug)]
pub(crate) enum CApiError {
    #[error("{0}")]
    RustApi(#[from] voicevox_core::Error),
    #[error("UTF-8として不正な入力です")]
    InvalidUtf8Input,
    #[error("無効なAudioQueryです: {0}")]
    InvalidAudioQuery(Either<serde_json::Error, String>),
    #[error("無効なAccentPhraseです: {0}")]
    InvalidAccentPhrase(Either<serde_json::Error, String>),
    #[error("無効なモーラです: {0}")]
    InvalidMora(Either<serde_json::Error, String>),
    #[error("無効な楽譜です: {0}")]
    InvalidScore(Either<serde_json::Error, String>),
    #[error("無効なノートです: {0}")]
    InvalidNote(Either<serde_json::Error, String>),
    #[error("無効なFrameAudioQueryです: {0}")]
    InvalidFrameAudioQuery(Either<serde_json::Error, String>),
    #[error("無効なFramePhonemeです: {0}")]
    InvalidFramePhoneme(Either<serde_json::Error, String>),
    #[error("無効なUUIDです: {0}")]
    InvalidUuid(uuid::Error),
}

pub(crate) trait ValidateJson: Validate {
    fn error(source: Either<serde_json::Error, String>) -> CApiError;

    fn validate_json(json: &CStr) -> CApiResult<Self> {
        let res = Self::from_json_without_validation(json)?;
        res.validate()
            .map_err(|e| Either::Right(unwrap_invalid_query_error_kind(&e)))
            .map_err(Self::error)?;
        return Ok(res);

        fn unwrap_invalid_query_error_kind(err: &voicevox_core::Error) -> String {
            err.source()
                .expect("the error is expected to be `InvalidQuery`, which has `source`")
                .to_string()
        }
    }

    fn from_json_without_validation(json: &CStr) -> CApiResult<Self> {
        serde_json::from_str(ensure_utf8(json)?)
            .map_err(Either::Left)
            .map_err(Self::error)
    }
}

#[duplicate_item(
    T ErrorVariant;
    [ AudioQuery ] [ InvalidAudioQuery ];
    [ AccentPhrase ] [ InvalidAccentPhrase ];
    [ Mora ] [ InvalidMora ];
    [ Vec<AccentPhrase> ] [ InvalidAccentPhrase ];
    [ Score ] [ InvalidScore ];
    [ Note ] [ InvalidNote ];
    [ FrameAudioQuery ] [ InvalidFrameAudioQuery ];
    [ FramePhoneme ] [ InvalidFramePhoneme ];
)]
impl ValidateJson for T {
    fn error(source: Either<serde_json::Error, String>) -> CApiError {
        CApiError::ErrorVariant(source)
    }
}

pub(crate) fn audio_query_model_to_json(audio_query_model: &AudioQuery) -> String {
    serde_json::to_string(audio_query_model).expect("should be always valid")
}

pub(crate) fn accent_phrases_to_json(audio_query_model: &[AccentPhrase]) -> String {
    serde_json::to_string(audio_query_model).expect("should be always valid")
}

pub(crate) trait ToCJson {
    fn to_json(&self) -> String;

    fn to_c_json(&self) -> CString {
        CString::new(self.to_json()).expect("a JSON should not contain '\\0'")
    }
}

impl<T> ToCJson for T
where
    for<'a> &'a T: Into<serde_json::Value>,
{
    fn to_json(&self) -> String {
        self.into().to_string()
    }
}

#[duplicate_item(
    T;
    [ NonNaNFinite<f32> ];
    [ PositiveFinite<f32> ];
)]
impl ToCJson for [T] {
    fn to_json(&self) -> String {
        serde_json::to_string(self).expect("should be always JSON serializable")
    }
}

pub(crate) fn ensure_utf8(s: &CStr) -> CApiResult<&str> {
    s.to_str().map_err(|_| CApiError::InvalidUtf8Input)
}

impl From<voicevox_core::AccelerationMode> for VoicevoxAccelerationMode {
    fn from(mode: voicevox_core::AccelerationMode) -> Self {
        use voicevox_core::AccelerationMode::*;
        match mode {
            Auto => Self::VOICEVOX_ACCELERATION_MODE_AUTO,
            Cpu => Self::VOICEVOX_ACCELERATION_MODE_CPU,
            Gpu => Self::VOICEVOX_ACCELERATION_MODE_GPU,
            __NonExhaustive => unreachable!(),
        }
    }
}

impl From<VoicevoxAccelerationMode> for voicevox_core::AccelerationMode {
    fn from(mode: VoicevoxAccelerationMode) -> Self {
        use VoicevoxAccelerationMode::*;
        match mode {
            VOICEVOX_ACCELERATION_MODE_AUTO => Self::Auto,
            VOICEVOX_ACCELERATION_MODE_CPU => Self::Cpu,
            VOICEVOX_ACCELERATION_MODE_GPU => Self::Gpu,
        }
    }
}

impl From<VoicevoxOnExistingVoiceModelId> for voicevox_core::OnExistingVoiceModelId {
    fn from(value: VoicevoxOnExistingVoiceModelId) -> Self {
        use VoicevoxOnExistingVoiceModelId::*;
        match value {
            VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_ERROR => Self::Error,
            VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_RELOAD => Self::Reload,
            VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_SKIP => Self::Skip,
        }
    }
}

impl From<voicevox_core::OnExistingVoiceModelId> for VoicevoxOnExistingVoiceModelId {
    fn from(value: voicevox_core::OnExistingVoiceModelId) -> Self {
        use voicevox_core::OnExistingVoiceModelId::*;
        match value {
            Error => Self::VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_ERROR,
            Reload => Self::VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_RELOAD,
            Skip => Self::VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_SKIP,
            _ => unreachable!(),
        }
    }
}

impl Default for VoicevoxLoadVoiceModelOptions {
    fn default() -> Self {
        Self {
            on_existing: voicevox_core::OnExistingVoiceModelId::default().into(),
        }
    }
}

impl Default for VoicevoxInitializeOptions {
    fn default() -> Self {
        Self {
            acceleration_mode: AccelerationMode::default().into(),
            cpu_num_threads: voicevox_core::__internal::interop::DEFAULT_CPU_NUM_THREADS,
        }
    }
}

impl Default for VoicevoxSynthesisOptions {
    fn default() -> Self {
        Self {
            enable_interrogative_upspeak:
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
        }
    }
}

impl Default for VoicevoxTtsOptions {
    fn default() -> Self {
        Self {
            enable_interrogative_upspeak:
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
        }
    }
}

#[ext(UuidBytesExt)]
pub(crate) impl uuid::Bytes {
    fn to_model_id(self) -> VoiceModelId {
        Uuid::from_bytes(self).into()
    }
}

impl VoicevoxUserDictWord {
    pub(crate) unsafe fn try_into_word(&self) -> CApiResult<voicevox_core::UserDictWord> {
        let (surface, pronunciation) = unsafe {
            // SAFETY: The safety contract must be upheld by the caller.
            (
                CStr::from_ptr(self.surface),
                CStr::from_ptr(self.pronunciation),
            )
        };

        UserDictWord::builder()
            .word_type(self.word_type.into())
            .priority(self.priority)
            .build(
                ensure_utf8(surface)?,
                ensure_utf8(pronunciation)?.to_string(),
                self.accent_type,
            )
            .map_err(Into::into)
    }
}

impl From<VoicevoxUserDictWordType> for voicevox_core::UserDictWordType {
    fn from(value: VoicevoxUserDictWordType) -> Self {
        match value {
            VoicevoxUserDictWordType::VOICEVOX_USER_DICT_WORD_TYPE_PROPER_NOUN => Self::ProperNoun,
            VoicevoxUserDictWordType::VOICEVOX_USER_DICT_WORD_TYPE_COMMON_NOUN => Self::CommonNoun,
            VoicevoxUserDictWordType::VOICEVOX_USER_DICT_WORD_TYPE_VERB => Self::Verb,
            VoicevoxUserDictWordType::VOICEVOX_USER_DICT_WORD_TYPE_ADJECTIVE => Self::Adjective,
            VoicevoxUserDictWordType::VOICEVOX_USER_DICT_WORD_TYPE_SUFFIX => Self::Suffix,
        }
    }
}

impl From<voicevox_core::UserDictWordType> for VoicevoxUserDictWordType {
    fn from(value: voicevox_core::UserDictWordType) -> Self {
        match value {
            voicevox_core::UserDictWordType::ProperNoun => {
                Self::VOICEVOX_USER_DICT_WORD_TYPE_PROPER_NOUN
            }
            voicevox_core::UserDictWordType::CommonNoun => {
                Self::VOICEVOX_USER_DICT_WORD_TYPE_COMMON_NOUN
            }
            voicevox_core::UserDictWordType::Verb => Self::VOICEVOX_USER_DICT_WORD_TYPE_VERB,
            voicevox_core::UserDictWordType::Adjective => {
                Self::VOICEVOX_USER_DICT_WORD_TYPE_ADJECTIVE
            }
            voicevox_core::UserDictWordType::Suffix => Self::VOICEVOX_USER_DICT_WORD_TYPE_SUFFIX,
            voicevox_core::UserDictWordType::__NonExhaustive => unreachable!(),
        }
    }
}
