use std::{
    collections::{HashMap, HashSet},
    ffi::CString,
    num::NonZero,
    path::Path,
    ptr::NonNull,
    sync::{Arc, LazyLock},
};

use camino::Utf8Path;
use duplicate::duplicate_item;
use easy_ext::ext;
use ref_cast::ref_cast_custom;
use voicevox_core::{CharacterMeta, Result, VoiceModelId};

use crate::{
    OpenJtalkRc, VoicevoxInitializeOptions, VoicevoxOnnxruntime, VoicevoxSynthesizer,
    VoicevoxUserDict, VoicevoxVoiceModelFile,
    helpers::CApiResult,
    object::{CApiObject, CApiObjectPtrExt as _},
};

// FIXME: 中身(Rust API)を直接操作するかラッパーメソッド越しにするのかが混在していて、一貫性を
// 欠いている

impl VoicevoxOnnxruntime {
    #[cfg(feature = "load-onnxruntime")]
    pub(crate) const LIB_VERSIONED_FILENAME: &'static std::ffi::CStr =
        to_cstr!(voicevox_core::blocking::Onnxruntime::LIB_VERSIONED_FILENAME);

    #[cfg(feature = "load-onnxruntime")]
    pub(crate) const LIB_UNVERSIONED_FILENAME: &'static std::ffi::CStr =
        to_cstr!(voicevox_core::blocking::Onnxruntime::LIB_UNVERSIONED_FILENAME);

    #[ref_cast_custom]
    fn new(rust: &voicevox_core::blocking::Onnxruntime) -> &Self;

    pub(crate) fn get() -> Option<&'static Self> {
        voicevox_core::blocking::Onnxruntime::get().map(Self::new)
    }

    #[cfg(feature = "load-onnxruntime")]
    pub(crate) fn load_once(filename: &std::ffi::CStr) -> CApiResult<&'static Self> {
        use crate::helpers::ensure_utf8;

        let inner = voicevox_core::blocking::Onnxruntime::load_once()
            .filename(ensure_utf8(filename)?)
            .perform()?;
        Ok(Self::new(inner))
    }

    #[cfg(feature = "link-onnxruntime")]
    pub(crate) fn init_once() -> CApiResult<&'static Self> {
        let inner = voicevox_core::blocking::Onnxruntime::init_once()?;
        Ok(Self::new(inner))
    }
}

#[cfg(feature = "load-onnxruntime")]
macro_rules! to_cstr {
    ($s:expr) => {{
        static CSTR: &[u8] = const_format::concatcp!($s, '\0').as_bytes();
        unsafe {
            // SAFETY: added a nul with `concatcp!`
            std::ffi::CStr::from_bytes_with_nul_unchecked(CSTR)
        }
    }};
}
#[cfg(feature = "load-onnxruntime")]
use to_cstr;

impl OpenJtalkRc {
    pub(crate) fn new(open_jtalk_dic_dir: impl AsRef<Utf8Path>) -> Result<NonNull<Self>> {
        let body = voicevox_core::blocking::OpenJtalk::new(open_jtalk_dic_dir)?;
        Ok(<Self as CApiObject>::new(body))
    }
}

impl VoicevoxSynthesizer {
    pub(crate) fn new(
        onnxruntime: &'static VoicevoxOnnxruntime,
        open_jtalk: *const OpenJtalkRc,
        VoicevoxInitializeOptions {
            acceleration_mode,
            cpu_num_threads,
        }: VoicevoxInitializeOptions,
    ) -> Result<NonNull<Self>> {
        let body = voicevox_core::blocking::Synthesizer::builder(&onnxruntime.0)
            .text_analyzer(open_jtalk.body().clone())
            .acceleration_mode(acceleration_mode.into())
            .cpu_num_threads(cpu_num_threads)
            .build()?;
        Ok(<Self as CApiObject>::new(body))
    }
}

#[ext(VoicevoxSynthesizerPtrExt)]
impl *const VoicevoxSynthesizer {
    pub(crate) fn onnxruntime(self) -> &'static VoicevoxOnnxruntime {
        VoicevoxOnnxruntime::new(self.body().onnxruntime())
    }

    pub(crate) fn load_voice_model(
        self,
        model: &voicevox_core::blocking::VoiceModelFile,
        on_existing: voicevox_core::OnExistingVoiceModelId,
    ) -> CApiResult<()> {
        self.body()
            .load_voice_model(model)
            .on_existing(on_existing)
            .perform()?;
        Ok(())
    }

    pub(crate) fn unload_voice_model(self, model_id: VoiceModelId) -> Result<()> {
        self.body().unload_voice_model(model_id)?;
        Ok(())
    }

    pub(crate) fn metas(self) -> CString {
        metas_to_json(&self.body().metas())
    }
}

impl VoicevoxVoiceModelFile {
    pub(crate) fn open(path: impl AsRef<Path>) -> Result<NonNull<Self>> {
        let model = voicevox_core::blocking::VoiceModelFile::open(path)?;
        Ok(Self::new(model))
    }
}

#[ext(VoicevoxVoiceModelFilePtrExt)]
impl *const VoicevoxVoiceModelFile {
    pub(crate) fn metas(self) -> CString {
        metas_to_json(self.body().metas())
    }
}

fn metas_to_json(metas: &[CharacterMeta]) -> CString {
    let metas = serde_json::to_string(metas).expect("should not fail");
    CString::new(metas).expect("should not contain NUL")
}

#[duplicate_item(
    H                          B;
    [ OpenJtalkRc ]            [ voicevox_core::blocking::OpenJtalk ];
    [ VoicevoxUserDict ]       [ voicevox_core::blocking::UserDict ];
    [ VoicevoxSynthesizer ]    [ voicevox_core::blocking::Synthesizer<voicevox_core::blocking::OpenJtalk> ];
    [ VoicevoxVoiceModelFile ] [ voicevox_core::blocking::VoiceModelFile ];
)]
impl CApiObject for H {
    type RustApiObject = B;

    fn known_addrs() -> &'static std::sync::Mutex<HashSet<NonZero<usize>>> {
        static KNOWN_ADDRS: LazyLock<std::sync::Mutex<HashSet<NonZero<usize>>>> =
            LazyLock::new(Default::default);
        &KNOWN_ADDRS
    }

    fn heads() -> &'static boxcar::Vec<Self> {
        static HEADS: boxcar::Vec<H> = boxcar::Vec::new();
        &HEADS
    }

    fn bodies() -> &'static std::sync::Mutex<
        HashMap<NonZero<usize>, Arc<parking_lot::RwLock<Option<Self::RustApiObject>>>>,
    > {
        #[expect(clippy::type_complexity, reason = "`CApiObject::bodies`と同様")]
        static BODIES: LazyLock<
            std::sync::Mutex<HashMap<NonZero<usize>, Arc<parking_lot::RwLock<Option<B>>>>>,
        > = LazyLock::new(Default::default);

        &BODIES
    }
}
