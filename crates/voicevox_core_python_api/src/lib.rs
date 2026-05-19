use std::{
    marker::PhantomData,
    mem,
    ops::{Deref, DerefMut},
};

mod convert;
use crate::convert::VoicevoxCoreResultExt as _;

use self::convert::{AudioQueryExt as _, ToDataclass, from_utf8_path};
use easy_ext::ext;
use log::{debug, warn};
use macros::pyproject_project_version;
use pyo3::{
    Bound, Py, PyAny, PyResult, PyTypeInfo, Python, create_exception,
    exceptions::{PyException, PyKeyError, PyValueError},
    pyclass, pyfunction, pymodule,
    types::{PyAnyMethods as _, PyList, PyModule, PyModuleMethods as _, PyString},
    wrap_pyfunction,
};
use voicevox_core::{
    __internal::interop::raii::MaybeClosed, AccentPhrase, AudioQuery, FrameAudioQuery,
    FramePhoneme, Mora, Note, Score, UserDictWord,
};

#[pymodule]
#[pyo3(name = "_rust")]
fn rust(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    pyo3_log::init();

    module.add("__version__", pyproject_project_version!())?;
    module.add_class::<_ReservedFields>()?;
    module.add_wrapped(wrap_pyfunction!(_audio_query_from_accent_phrases))?;
    module.add_wrapped(wrap_pyfunction!(_audio_query_from_json))?;
    module.add_wrapped(wrap_pyfunction!(_audio_query_to_json))?;
    module.add_wrapped(wrap_pyfunction!(_validate_audio_query))?;
    module.add_wrapped(wrap_pyfunction!(_validate_accent_phrase))?;
    module.add_wrapped(wrap_pyfunction!(_validate_mora))?;
    module.add_wrapped(wrap_pyfunction!(_validate_user_dict_word))?;
    module.add_wrapped(wrap_pyfunction!(_validate_score))?;
    module.add_wrapped(wrap_pyfunction!(_validate_note))?;
    module.add_wrapped(wrap_pyfunction!(_validate_frame_audio_query))?;
    module.add_wrapped(wrap_pyfunction!(_validate_frame_phoneme))?;
    module.add_wrapped(wrap_pyfunction!(_to_zenkaku))?;
    module.add_wrapped(wrap_pyfunction!(wav_from_s16le))?;
    module.add_wrapped(wrap_pyfunction!(ensure_compatible))?;

    add_exceptions(module)?;

    let blocking_module = PyModule::new(py, "voicevox_core._rust.blocking")?;
    blocking_module.add_class::<self::blocking::Synthesizer>()?;
    blocking_module.add_class::<self::blocking::Onnxruntime>()?;
    blocking_module.add_class::<self::blocking::OpenJtalk>()?;
    blocking_module.add_class::<self::blocking::VoiceModelFile>()?;
    blocking_module.add_class::<self::blocking::UserDict>()?;
    blocking_module.add_class::<self::blocking::AudioFeature>()?;
    module.add_and_register_submodule(blocking_module)?;

    let asyncio_module = PyModule::new(py, "voicevox_core._rust.asyncio")?;
    asyncio_module.add_class::<self::asyncio::Synthesizer>()?;
    asyncio_module.add_class::<self::asyncio::Onnxruntime>()?;
    asyncio_module.add_class::<self::asyncio::OpenJtalk>()?;
    asyncio_module.add_class::<self::asyncio::VoiceModelFile>()?;
    asyncio_module.add_class::<self::asyncio::UserDict>()?;
    module.add_and_register_submodule(asyncio_module)
}

#[ext]
impl Bound<'_, PyModule> {
    // https://github.com/PyO3/pyo3/issues/1517#issuecomment-808664021
    fn add_and_register_submodule(&self, module: Bound<'_, PyModule>) -> PyResult<()> {
        let sys = self.py().import("sys")?;
        sys.as_any()
            .getattr("modules")?
            .set_item(module.name()?, &module)?;
        self.add_submodule(&module)
    }
}

macro_rules! exceptions {
    ($($name:ident: $base:ty;)*) => {
        $(
            create_exception!(voicevox_core, $name, $base);
        )*

        fn add_exceptions(module: &Bound<'_, PyModule>) -> PyResult<()> {
            $(
                module.add(stringify!($name), module.py().get_type::<$name>())?;
            )*
            Ok(())
        }
    };
}

exceptions! {
    NotLoadedOpenjtalkDictError: PyException;
    GpuSupportError: PyException;
    InitInferenceRuntimeError: PyException;
    OpenZipFileError: PyException;
    ReadZipEntryError: PyException;
    ModelAlreadyLoadedError: PyException;
    StyleAlreadyLoadedError: PyException;
    InvalidModelFormatError: PyException;
    InvalidModelDataError: PyException;
    GetSupportedDevicesError: PyException;
    StyleNotFoundError: PyKeyError;
    ModelNotFoundError: PyKeyError;
    RunModelError: PyException;
    AnalyzeTextError: PyException;
    ParseKanaError: PyValueError;
    LoadUserDictError: PyException;
    SaveUserDictError: PyException;
    WordNotFoundError: PyKeyError;
    UseUserDictError: PyException;
    InvalidWordError: PyValueError;
    InvalidQueryError: PyValueError;
    IncompatibleQueriesError: PyValueError;
}

#[derive(derive_more::Debug)]
#[debug("{content:?}")]
struct Closable<T, C: PyTypeInfo, A: Async> {
    content: A::RwLock<MaybeClosed<T>>,
    marker: PhantomData<(C, A)>,
}

impl<T, C: PyTypeInfo, A: Async> Closable<T, C, A> {
    fn new(content: T) -> Self {
        Self {
            content: MaybeClosed::Open(content).into(),
            marker: PhantomData,
        }
    }

    fn read(&self) -> PyResult<impl Deref<Target = T>> {
        let lock = self
            .content
            .try_read_()
            .map_err(|_| PyValueError::new_err(format!("The `{}` is being closed", C::NAME)))?;

        voicevox_core::__internal::interop::raii::try_map_guard(lock, |lock| match &**lock {
            MaybeClosed::Open(content) => Ok(content),
            MaybeClosed::Closed => Err(PyValueError::new_err(format!(
                "The `{}` is closed",
                C::NAME,
            ))),
        })
    }

    async fn close_(&self) -> Option<T> {
        let lock = &mut *match self.content.try_write_() {
            Ok(lock) => lock,
            Err(()) => {
                warn!("The `{}` is still in use. Waiting before closing", C::NAME);
                self.content.write_().await
            }
        };

        if matches!(*lock, MaybeClosed::Open(_)) {
            debug!("Closing a {}", C::NAME);
        }
        match mem::replace(lock, MaybeClosed::Closed) {
            MaybeClosed::Open(content) => Some(content),
            MaybeClosed::Closed => None,
        }
    }
}

impl<T, C: PyTypeInfo> Closable<T, C, SingleTasked> {
    #[must_use = "中身は明示的に`drop`でdropすること"]
    fn close(&self) -> Option<T> {
        futures_lite::future::block_on(self.close_())
    }
}

impl<T, C: PyTypeInfo> Closable<T, C, Tokio> {
    #[must_use = "中身は明示的に`drop`でdropすること"]
    async fn close(&self) -> Option<T> {
        self.close_().await
    }
}

trait Async {
    type RwLock<T>: RwLock<Item = T>;
}

enum SingleTasked {}
enum Tokio {}

impl Async for SingleTasked {
    type RwLock<T> = std::sync::RwLock<T>;
}

impl Async for Tokio {
    type RwLock<T> = async_lock::RwLock<T>;
}

trait RwLock: From<Self::Item> {
    type Item;
    type RwLockWriteGuard<'a>: DerefMut<Target = Self::Item>
    where
        Self: 'a;
    fn try_read_(&self) -> Result<impl Deref<Target = Self::Item>, ()>;
    async fn write_(&self) -> Self::RwLockWriteGuard<'_>;
    fn try_write_(&self) -> Result<Self::RwLockWriteGuard<'_>, ()>;
}

impl<T> RwLock for std::sync::RwLock<T> {
    type Item = T;
    type RwLockWriteGuard<'a>
        = std::sync::RwLockWriteGuard<'a, Self::Item>
    where
        Self: 'a;

    fn try_read_(&self) -> Result<impl Deref<Target = Self::Item>, ()> {
        self.try_read().map_err(|e| match e {
            std::sync::TryLockError::Poisoned(e) => panic!("{e}"),
            std::sync::TryLockError::WouldBlock => (),
        })
    }

    async fn write_(&self) -> Self::RwLockWriteGuard<'_> {
        self.write().unwrap_or_else(|e| panic!("{e}"))
    }

    fn try_write_(&self) -> Result<Self::RwLockWriteGuard<'_>, ()> {
        self.try_write().map_err(|e| match e {
            std::sync::TryLockError::Poisoned(e) => panic!("{e}"),
            std::sync::TryLockError::WouldBlock => (),
        })
    }
}

impl<T> RwLock for async_lock::RwLock<T> {
    type Item = T;
    type RwLockWriteGuard<'a>
        = async_lock::RwLockWriteGuard<'a, Self::Item>
    where
        Self: 'a;

    fn try_read_(&self) -> Result<impl Deref<Target = Self::Item>, ()> {
        self.try_read().ok_or(())
    }

    async fn write_(&self) -> Self::RwLockWriteGuard<'_> {
        self.write().await
    }

    fn try_write_(&self) -> Result<Self::RwLockWriteGuard<'_>, ()> {
        self.try_write().ok_or(())
    }
}

struct VoiceModelFilePyFields {
    id: Py<PyAny>,     // `NewType("VoiceModelId", UUID)`
    metas: Py<PyList>, // `list[CharacterMeta]`
}

impl VoiceModelFilePyFields {
    fn format<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyString>> {
        let Self { id, metas } = self;
        let ret = PyString::new(py, "id=");
        let ret = ret.add(id.bind(py).repr()?)?;
        let ret = ret.add(" metas=")?;
        let ret = ret.add(metas.bind(py).repr()?)?;
        ret.cast_into::<PyString>().map_err(Into::into)
    }
}

#[pyclass(frozen)]
struct _ReservedFields;

#[pyfunction]
fn _audio_query_from_accent_phrases(
    #[pyo3(from_py_with = convert::from_accent_phrases)] accent_phrases: Vec<AccentPhrase>,
) -> ToDataclass<AudioQuery> {
    AudioQuery::from_accent_phrases(accent_phrases).into()
}

#[pyfunction]
fn _audio_query_from_json(json: &str) -> PyResult<ToDataclass<AudioQuery>> {
    AudioQuery::from_json(json).map(Into::into)
}

#[pyfunction]
fn _audio_query_to_json(
    #[pyo3(from_py_with = convert::from_audio_query)] audio_query: AudioQuery,
) -> String {
    audio_query.to_json()
}

#[pyfunction]
fn _validate_mora(
    #[pyo3(from_py_with = convert::from_query_like_via_serde)] mora: Mora,
    py: Python<'_>,
) -> PyResult<()> {
    mora.validate().into_py_result(py)
}

#[pyfunction]
fn _validate_accent_phrase(
    #[pyo3(from_py_with = convert::from_query_like_via_serde)] accent_phrase: AccentPhrase,
    py: Python<'_>,
) -> PyResult<()> {
    accent_phrase.validate().into_py_result(py)
}

#[pyfunction]
fn _validate_audio_query(
    #[pyo3(from_py_with = convert::from_audio_query)] audio_query: AudioQuery,
    py: Python<'_>,
) -> PyResult<()> {
    audio_query.validate().into_py_result(py)
}

#[pyfunction]
fn _validate_user_dict_word(
    #[expect(unused_variables)]
    #[pyo3(from_py_with = convert::to_rust_user_dict_word)]
    word: UserDictWord,
) {
}

#[pyfunction]
fn _validate_note(
    #[pyo3(from_py_with = convert::from_query_like_via_serde)] note: Note,
    py: Python<'_>,
) -> PyResult<()> {
    note.validate().into_py_result(py)
}

#[pyfunction]
fn _validate_score(
    #[pyo3(from_py_with = convert::from_query_like_via_serde)] score: Score,
    py: Python<'_>,
) -> PyResult<()> {
    score.validate().into_py_result(py)
}

#[pyfunction]
fn _validate_frame_phoneme(
    #[expect(unused_variables)]
    #[pyo3(from_py_with = convert::from_query_like_via_serde)]
    frame_phoneme: FramePhoneme,
) {
}

#[pyfunction]
fn _validate_frame_audio_query(
    #[pyo3(from_py_with = convert::from_audio_query)] frame_audio_query: FrameAudioQuery,
) {
    frame_audio_query.validate();
}

#[pyfunction]
fn _to_zenkaku(text: &str) -> PyResult<String> {
    Ok(voicevox_core::__internal::to_zenkaku(text))
}

#[pyfunction]
fn wav_from_s16le(pcm: &[u8], sampling_rate: u32, is_stereo: bool) -> Vec<u8> {
    voicevox_core::__wav_from_s16le(pcm, sampling_rate, is_stereo)
}

#[pyfunction]
fn ensure_compatible(
    #[pyo3(from_py_with = convert::from_query_like_via_serde)] score: Score,
    #[pyo3(from_py_with = convert::from_audio_query)] frame_audio_query: FrameAudioQuery,
    py: Python<'_>,
) -> PyResult<()> {
    voicevox_core::ensure_compatible(&score, &frame_audio_query).into_py_result(py)
}

mod blocking {
    use std::{ffi::OsString, path::PathBuf, sync::Arc};

    use camino::Utf8PathBuf;
    use pyo3::{
        Bound, IntoPyObject as _, Py, PyAny, PyRef, PyResult, PyTypeInfo as _, Python,
        exceptions::{PyIndexError, PyTypeError, PyValueError},
        pyclass, pymethods,
        sync::PyOnceLock,
        types::{IntoPyDict as _, PyAnyMethods as _, PyDict, PyList, PyString, PyTuple, PyType},
    };
    use ref_cast::RefCast as _;
    use uuid::Uuid;
    use voicevox_core::{
        __internal::interop::BlockingTextAnalyzerExt as _, AccelerationMode, AccentPhrase,
        AudioQuery, FrameAudioQuery, OnExistingVoiceModelId, Score, StyleId, SupportedDevices,
        UserDictWord, VoiceModelMeta,
    };

    use crate::{
        Closable, SingleTasked, VoiceModelFilePyFields,
        convert::{ToDataclass, VoicevoxCoreResultExt as _},
    };

    #[pyclass(frozen)]
    pub(crate) struct VoiceModelFile {
        model: Closable<voicevox_core::blocking::VoiceModelFile, Self, SingleTasked>,
        fields: VoiceModelFilePyFields,
    }

    #[pymethods]
    impl VoiceModelFile {
        #[new]
        #[classmethod]
        #[pyo3(signature = (*_args, **_kwargs))]
        fn new(
            _cls: Bound<'_, PyType>,
            _args: Bound<'_, PyTuple>,
            _kwargs: Option<Bound<'_, PyDict>>,
        ) -> PyResult<Self> {
            Err(PyTypeError::new_err((
                "`VoiceModelFile` does not have a normal constructor. Use \
                 `VoiceModelFile.load_once` to construct",
            )))
        }

        #[staticmethod]
        fn open(py: Python<'_>, path: PathBuf) -> PyResult<Self> {
            let model = voicevox_core::blocking::VoiceModelFile::open(path).into_py_result(py)?;

            let id = model.id().0.into_pyobject(py)?.into();
            let metas = ToDataclass::ref_cast(model.metas())
                .into_pyobject(py)?
                .into();

            let model = Closable::new(model);

            Ok(Self {
                model,
                fields: VoiceModelFilePyFields { id, metas },
            })
        }

        fn __repr__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyString>> {
            let Self {
                model: rust_api,
                fields,
            } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            let ret = &format!(
                "<voicevox_core.blocking.{NAME} rust_api=<{rust_api:?}> ",
                NAME = Self::NAME,
            );
            let ret = PyString::new(py, ret);
            let ret = ret.add(fields.format(py)?)?;
            let ret = ret.add(">")?;
            ret.cast_into::<PyString>().map_err(Into::into)
        }

        fn close(&self) {
            let this = self.model.close();
            drop(this);
        }

        #[getter]
        fn id(&self, py: Python<'_>) -> Py<PyAny> {
            self.fields.id.clone_ref(py)
        }

        #[getter]
        fn metas(&self, py: Python<'_>) -> Py<PyList> {
            self.fields.metas.clone_ref(py)
        }

        fn __enter__(slf: PyRef<'_, Self>) -> PyResult<PyRef<'_, Self>> {
            slf.model.read()?;
            Ok(slf)
        }

        fn __exit__(
            &self,
            #[expect(unused_variables, reason = "`__exit__`としては必要")] exc_type: &Bound<
                '_,
                PyAny,
            >,
            #[expect(unused_variables, reason = "`__exit__`としては必要")] exc_value: &Bound<
                '_,
                PyAny,
            >,
            #[expect(unused_variables, reason = "`__exit__`としては必要")] traceback: &Bound<
                '_,
                PyAny,
            >,
        ) {
            self.close();
        }
    }

    static ONNXRUNTIME: PyOnceLock<Py<Onnxruntime>> = PyOnceLock::new();

    #[pyclass(frozen)]
    #[derive(Clone)]
    pub(crate) struct Onnxruntime(&'static voicevox_core::blocking::Onnxruntime);

    #[pymethods]
    impl Onnxruntime {
        #[classattr]
        const LIB_NAME: &'static str = voicevox_core::blocking::Onnxruntime::LIB_NAME;

        #[classattr]
        const LIB_VERSION: &'static str = voicevox_core::blocking::Onnxruntime::LIB_VERSION;

        #[classattr]
        const LIB_VERSIONED_FILENAME: &'static str =
            voicevox_core::blocking::Onnxruntime::LIB_VERSIONED_FILENAME;

        #[classattr]
        const LIB_UNVERSIONED_FILENAME: &'static str =
            voicevox_core::blocking::Onnxruntime::LIB_UNVERSIONED_FILENAME;

        #[new]
        #[classmethod]
        #[pyo3(signature = (*_args, **_kwargs))]
        fn new(
            _cls: Bound<'_, PyType>,
            _args: Bound<'_, PyTuple>,
            _kwargs: Option<Bound<'_, PyDict>>,
        ) -> PyResult<Self> {
            Err(PyTypeError::new_err((
                "`Onnxruntime` does not have a normal constructor. Use `Onnxruntime.load_once` or \
                 `Onnxruntime.get` to construct",
            )))
        }

        #[staticmethod]
        fn get(py: Python<'_>) -> PyResult<Option<Py<Self>>> {
            let result =
                ONNXRUNTIME.get_or_try_init(
                    py,
                    || match voicevox_core::blocking::Onnxruntime::get()
                        .map(|o| Py::new(py, Self(o)))
                    {
                        Some(Ok(this)) => Ok(this),
                        Some(Err(err)) => Err(Some(err)),
                        None => Err(None),
                    },
                );

            match result {
                Ok(this) => Ok(Some(this.clone_ref(py))),
                Err(Some(err)) => Err(err),
                Err(None) => Ok(None),
            }
        }

        #[staticmethod]
        #[pyo3(signature = (*, filename = Self::LIB_VERSIONED_FILENAME.into()))]
        fn load_once(filename: OsString, py: Python<'_>) -> PyResult<Py<Self>> {
            ONNXRUNTIME
                .get_or_try_init(py, || {
                    let inner = voicevox_core::blocking::Onnxruntime::load_once()
                        .filename(filename)
                        .perform()
                        .into_py_result(py)?;
                    Py::new(py, Self(inner))
                })
                .map(|onnxruntime| onnxruntime.clone_ref(py))
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self(rust_api) = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.blocking.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        fn supported_devices(&self, py: Python<'_>) -> PyResult<ToDataclass<SupportedDevices>> {
            self.0
                .supported_devices()
                .map(Into::into)
                .into_py_result(py)
        }
    }

    #[pyclass(frozen)]
    #[derive(derive_more::Debug)]
    #[debug("{open_jtalk:?}")]
    pub(crate) struct OpenJtalk {
        open_jtalk: voicevox_core::blocking::OpenJtalk,
    }

    #[pymethods]
    impl OpenJtalk {
        #[new]
        fn new(
            #[pyo3(from_py_with = super::from_utf8_path)] open_jtalk_dict_dir: Utf8PathBuf,
            py: Python<'_>,
        ) -> PyResult<Self> {
            let open_jtalk =
                voicevox_core::blocking::OpenJtalk::new(open_jtalk_dict_dir).into_py_result(py)?;
            Ok(Self { open_jtalk })
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self {
                open_jtalk: rust_api,
            } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.blocking.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        fn use_user_dict(&self, user_dict: UserDict, py: Python<'_>) -> PyResult<()> {
            self.open_jtalk
                .use_user_dict(&user_dict.dict)
                .into_py_result(py)
        }

        fn analyze(&self, text: &str, py: Python<'_>) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            self.open_jtalk
                .analyze_(text)
                .map(Into::into)
                .into_py_result(py)
        }
    }

    #[derive(derive_more::Debug)]
    #[debug("{:?}", _0.get())]
    struct OwnedOpenJtalk(Py<OpenJtalk>);

    impl voicevox_core::blocking::TextAnalyzer for OwnedOpenJtalk {
        fn analyze(&self, text: &str) -> anyhow::Result<Vec<AccentPhrase>> {
            self.0.get().open_jtalk.analyze(text)
        }
    }

    #[pyclass(frozen, eq)]
    #[derive(PartialEq)]
    pub(crate) struct AudioFeature {
        audio: voicevox_core::blocking::__AudioFeature,
    }

    #[pymethods]
    impl AudioFeature {
        #[getter]
        fn frame_length(&self) -> usize {
            self.audio.frame_length
        }

        #[getter]
        fn frame_rate(&self) -> f64 {
            self.audio.frame_rate
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self { audio: rust_api } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.blocking.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }
    }

    #[pyclass(frozen)]
    pub(crate) struct Synthesizer {
        synthesizer:
            Closable<voicevox_core::blocking::Synthesizer<OwnedOpenJtalk>, Self, SingleTasked>,
    }

    #[pymethods]
    impl Synthesizer {
        #[new]
        #[pyo3(signature =(
            onnxruntime,
            open_jtalk,
            *,
            acceleration_mode = Default::default(),
            cpu_num_threads = voicevox_core::__internal::interop::DEFAULT_CPU_NUM_THREADS,
        ))]
        fn new(
            onnxruntime: Onnxruntime,
            open_jtalk: Py<OpenJtalk>,
            #[pyo3(from_py_with = crate::convert::from_acceleration_mode)]
            acceleration_mode: AccelerationMode,
            cpu_num_threads: u16,
            py: Python<'_>,
        ) -> PyResult<Self> {
            let inner = voicevox_core::blocking::Synthesizer::builder(onnxruntime.0)
                .text_analyzer(OwnedOpenJtalk(open_jtalk))
                .acceleration_mode(acceleration_mode)
                .cpu_num_threads(cpu_num_threads)
                .build()
                .into_py_result(py)?;
            Ok(Self {
                synthesizer: Closable::new(inner),
            })
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self {
                synthesizer: rust_api,
            } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.blocking.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        fn __enter__(slf: PyRef<'_, Self>) -> PyResult<PyRef<'_, Self>> {
            slf.synthesizer.read()?;
            Ok(slf)
        }

        fn __exit__(
            &self,
            #[expect(unused_variables, reason = "`__exit__`としては必要")] exc_type: &Bound<
                '_,
                PyAny,
            >,
            #[expect(unused_variables, reason = "`__exit__`としては必要")] exc_value: &Bound<
                '_,
                PyAny,
            >,
            #[expect(unused_variables, reason = "`__exit__`としては必要")] traceback: &Bound<
                '_,
                PyAny,
            >,
        ) {
            self.close();
        }

        #[getter]
        fn onnxruntime(&self, py: Python<'_>) -> Py<Onnxruntime> {
            ONNXRUNTIME
                .get(py)
                .expect("should be initialized")
                .clone_ref(py)
        }

        #[getter]
        fn open_jtalk(&self, py: Python<'_>) -> PyResult<Py<OpenJtalk>> {
            let this = self.synthesizer.read()?;
            Ok(this.text_analyzer().0.clone_ref(py))
        }

        #[getter]
        fn is_gpu_mode(&self) -> PyResult<bool> {
            let synthesizer = self.synthesizer.read()?;
            Ok(synthesizer.is_gpu_mode())
        }

        fn metas(&self) -> PyResult<ToDataclass<VoiceModelMeta>> {
            let synthesizer = self.synthesizer.read()?;
            Ok(synthesizer.metas().into())
        }

        #[pyo3(signature = (model, *, on_existing = Default::default()))]
        fn load_voice_model(
            &self,
            model: &Bound<'_, VoiceModelFile>,
            #[pyo3(from_py_with = crate::convert::from_on_existing_voice_model_id)]
            on_existing: OnExistingVoiceModelId,
            py: Python<'_>,
        ) -> PyResult<()> {
            let this = self.synthesizer.read()?;
            let model = &model.get().model.read()?;
            this.load_voice_model(model)
                .on_existing(on_existing)
                .perform()
                .into_py_result(py)
        }

        fn unload_voice_model(&self, voice_model_id: Uuid, py: Python<'_>) -> PyResult<()> {
            self.synthesizer
                .read()?
                .unload_voice_model(voice_model_id.into())
                .into_py_result(py)
        }

        fn is_loaded_voice_model(&self, voice_model_id: Uuid) -> PyResult<bool> {
            Ok(self
                .synthesizer
                .read()?
                .is_loaded_voice_model(voice_model_id.into()))
        }

        fn create_audio_query_from_kana(
            &self,
            kana: &str,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<AudioQuery>> {
            let synthesizer = self.synthesizer.read()?;

            synthesizer
                .create_audio_query_from_kana(kana, StyleId::new(style_id))
                .map(Into::into)
                .into_py_result(py)
        }

        fn create_audio_query(
            &self,
            text: &str,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<AudioQuery>> {
            let synthesizesr = self.synthesizer.read()?;

            synthesizesr
                .create_audio_query(text, StyleId::new(style_id))
                .map(Into::into)
                .into_py_result(py)
        }

        fn create_accent_phrases_from_kana(
            &self,
            kana: &str,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;

            synthesizer
                .create_accent_phrases_from_kana(kana, StyleId::new(style_id))
                .map(Into::into)
                .into_py_result(py)
        }

        fn create_accent_phrases(
            &self,
            text: &str,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;

            synthesizer
                .create_accent_phrases(text, StyleId::new(style_id))
                .map(Into::into)
                .into_py_result(py)
        }

        fn replace_mora_data(
            &self,
            #[pyo3(from_py_with = crate::convert::from_accent_phrases)] accent_phrases: Vec<
                AccentPhrase,
            >,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;
            synthesizer
                .replace_mora_data(&accent_phrases, style_id.into())
                .map(Into::into)
                .into_py_result(py)
        }

        fn replace_phoneme_length(
            &self,
            #[pyo3(from_py_with = crate::convert::from_accent_phrases)] accent_phrases: Vec<
                AccentPhrase,
            >,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;
            synthesizer
                .replace_phoneme_length(&accent_phrases, style_id.into())
                .map(Into::into)
                .into_py_result(py)
        }

        fn replace_mora_pitch(
            &self,
            #[pyo3(from_py_with = crate::convert::from_accent_phrases)] accent_phrases: Vec<
                AccentPhrase,
            >,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;
            synthesizer
                .replace_mora_pitch(&accent_phrases, style_id.into())
                .map(Into::into)
                .into_py_result(py)
        }

        // TODO: 後で復活させる
        // https://github.com/VOICEVOX/voicevox_core/issues/970
        #[allow(non_snake_case)]
        #[pyo3(signature=(
            audio_query,
            style_id,
            *,
            enable_interrogative_upspeak =
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
        ))]
        fn _Synthesizer__precompute_render(
            &self,
            #[pyo3(from_py_with = crate::convert::from_audio_query)] audio_query: AudioQuery,
            style_id: u32,
            enable_interrogative_upspeak: bool,
            py: Python<'_>,
        ) -> PyResult<AudioFeature> {
            let audio = self
                .synthesizer
                .read()?
                .__precompute_render(&audio_query, StyleId::new(style_id))
                .enable_interrogative_upspeak(enable_interrogative_upspeak)
                .perform()
                .into_py_result(py)?;
            Ok(AudioFeature { audio })
        }

        // TODO: 後で復活させる
        // https://github.com/VOICEVOX/voicevox_core/issues/970
        #[allow(non_snake_case)]
        fn _Synthesizer__render(
            &self,
            audio: &AudioFeature,
            start: usize,
            stop: usize,
            py: Python<'_>,
        ) -> PyResult<Vec<u8>> {
            if start > audio.frame_length() || stop > audio.frame_length() {
                return Err(PyIndexError::new_err(format!(
                    "({start}, {stop}) is out of range for audio feature of length {len}",
                    len = audio.frame_length(),
                )));
            }
            if start > stop {
                return Err(PyValueError::new_err(format!(
                    "({start}, {stop}) is invalid range because start > end",
                )));
            }
            self.synthesizer
                .read()?
                .__render(&audio.audio, start..stop)
                .into_py_result(py)
        }

        #[pyo3(signature=(
            audio_query,
            style_id,
            *,
            enable_interrogative_upspeak =
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
        ))]
        fn synthesis(
            &self,
            #[pyo3(from_py_with = crate::convert::from_audio_query)] audio_query: AudioQuery,
            style_id: u32,
            enable_interrogative_upspeak: bool,
            py: Python<'_>,
        ) -> PyResult<Vec<u8>> {
            self.synthesizer
                .read()?
                .synthesis(&audio_query, StyleId::new(style_id))
                .enable_interrogative_upspeak(enable_interrogative_upspeak)
                .perform()
                .into_py_result(py)
        }

        #[pyo3(signature=(
            kana,
            style_id,
            *,
            enable_interrogative_upspeak =
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
        ))]
        fn tts_from_kana(
            &self,
            kana: &str,
            style_id: u32,
            enable_interrogative_upspeak: bool,
            py: Python<'_>,
        ) -> PyResult<Vec<u8>> {
            let style_id = StyleId::new(style_id);
            self.synthesizer
                .read()?
                .tts_from_kana(kana, style_id)
                .enable_interrogative_upspeak(enable_interrogative_upspeak)
                .perform()
                .into_py_result(py)
        }

        #[pyo3(signature=(
            text,
            style_id,
            *,
            enable_interrogative_upspeak =
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
        ))]
        fn tts(
            &self,
            text: &str,
            style_id: u32,
            enable_interrogative_upspeak: bool,
            py: Python<'_>,
        ) -> PyResult<Vec<u8>> {
            let style_id = StyleId::new(style_id);
            self.synthesizer
                .read()?
                .tts(text, style_id)
                .enable_interrogative_upspeak(enable_interrogative_upspeak)
                .perform()
                .into_py_result(py)
        }

        fn create_sing_frame_audio_query(
            &self,
            #[pyo3(from_py_with = crate::convert::from_query_like_via_serde)] score: Score,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<ToDataclass<FrameAudioQuery>> {
            let style_id = StyleId::new(style_id);
            self.synthesizer
                .read()?
                .create_sing_frame_audio_query(&score, style_id)
                .map(Into::into)
                .into_py_result(py)
        }

        fn create_sing_frame_f0(
            &self,
            #[pyo3(from_py_with = crate::convert::from_query_like_via_serde)] score: Score,
            #[pyo3(from_py_with = crate::convert::from_audio_query)]
            frame_audio_query: FrameAudioQuery,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<Vec<f32>> {
            let style_id = StyleId::new(style_id);

            // TODO: typed_floatsにissueかPRを出しに行き、スライス変換かbytemuck対応を入れてもらう
            self.synthesizer
                .read()?
                .create_sing_frame_f0(&score, &frame_audio_query, style_id)
                .map(|f0s| f0s.into_iter().map(Into::into).collect())
                .into_py_result(py)
        }

        fn create_sing_frame_volume(
            &self,
            #[pyo3(from_py_with = crate::convert::from_query_like_via_serde)] score: Score,
            #[pyo3(from_py_with = crate::convert::from_audio_query)]
            frame_audio_query: FrameAudioQuery,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<Vec<f32>> {
            let style_id = StyleId::new(style_id);

            // TODO: typed_floatsにissueかPRを出しに行き、スライス変換かbytemuck対応を入れてもらう
            self.synthesizer
                .read()?
                .create_sing_frame_volume(&score, &frame_audio_query, style_id)
                .map(|volumes| volumes.into_iter().map(Into::into).collect())
                .into_py_result(py)
        }

        fn frame_synthesis(
            &self,
            #[pyo3(from_py_with = crate::convert::from_audio_query)]
            frame_audio_query: FrameAudioQuery,
            style_id: u32,
            py: Python<'_>,
        ) -> PyResult<Vec<u8>> {
            let style_id = StyleId::new(style_id);
            self.synthesizer
                .read()?
                .frame_synthesis(&frame_audio_query, style_id)
                .perform()
                .into_py_result(py)
        }

        fn close(&self) {
            drop(self.synthesizer.close());
        }
    }

    #[pyclass(frozen)]
    #[derive(Default, Debug, Clone)]
    pub(crate) struct UserDict {
        dict: Arc<voicevox_core::blocking::UserDict>,
    }

    #[pymethods]
    impl UserDict {
        #[new]
        fn new() -> Self {
            Self::default()
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self { dict: rust_api } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.blocking.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        fn load(&self, path: PathBuf, py: Python<'_>) -> PyResult<()> {
            self.dict.load(path).into_py_result(py)
        }

        fn save(&self, path: PathBuf, py: Python<'_>) -> PyResult<()> {
            self.dict.save(path).into_py_result(py)
        }

        fn add_word(
            &self,
            #[pyo3(from_py_with = crate::convert::to_rust_user_dict_word)] word: UserDictWord,
            py: Python<'_>,
        ) -> PyResult<Uuid> {
            self.dict.add_word(word).into_py_result(py)
        }

        fn update_word(
            &self,
            word_uuid: Uuid,
            #[pyo3(from_py_with = crate::convert::to_rust_user_dict_word)] word: UserDictWord,
            py: Python<'_>,
        ) -> PyResult<()> {
            self.dict.update_word(word_uuid, word).into_py_result(py)
        }

        fn remove_word(&self, word_uuid: Uuid, py: Python<'_>) -> PyResult<()> {
            self.dict.remove_word(word_uuid).into_py_result(py)?;
            Ok(())
        }

        fn import_dict(&self, other: &UserDict, py: Python<'_>) -> PyResult<()> {
            self.dict.import(&other.dict).into_py_result(py)?;
            Ok(())
        }

        fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
            self.dict.with_words(|words| {
                words
                    .iter()
                    .map(|(&uuid, word)| (uuid, ToDataclass::ref_cast(word)))
                    .into_py_dict(py)
            })
        }
    }
}

mod asyncio {
    use std::{ffi::OsString, path::PathBuf, sync::Arc};

    use camino::Utf8PathBuf;
    use pyo3::{
        Bound, IntoPyObject as _, Py, PyAny, PyErr, PyRef, PyResult, PyTypeInfo as _, Python,
        exceptions::PyTypeError,
        pyclass, pymethods,
        sync::PyOnceLock,
        types::{IntoPyDict as _, PyAnyMethods as _, PyDict, PyList, PyString, PyTuple, PyType},
    };
    use ref_cast::RefCast as _;
    use uuid::Uuid;
    use voicevox_core::{
        __internal::interop::NonblockingTextAnalyzerExt as _, AccelerationMode, AccentPhrase,
        AudioQuery, FrameAudioQuery, OnExistingVoiceModelId, Score, StyleId, SupportedDevices,
        UserDictWord, VoiceModelMeta,
    };

    use crate::{
        Closable, Tokio, VoiceModelFilePyFields,
        convert::{ToDataclass, VoicevoxCoreResultExt as _},
    };

    #[pyclass(frozen)]
    pub(crate) struct VoiceModelFile {
        model: Closable<voicevox_core::nonblocking::VoiceModelFile, Self, Tokio>,
        fields: VoiceModelFilePyFields,
    }

    #[pymethods]
    impl VoiceModelFile {
        #[new]
        #[classmethod]
        #[pyo3(signature = (*_args, **_kwargs))]
        fn new(
            _cls: Bound<'_, PyType>,
            _args: Bound<'_, PyTuple>,
            _kwargs: Option<Bound<'_, PyDict>>,
        ) -> PyResult<Self> {
            Err(PyTypeError::new_err((
                "`VoiceModelFile` does not have a normal constructor. Use \
                 `VoiceModelFile.load_once` to construct",
            )))
        }

        #[staticmethod]
        async fn open(path: PathBuf) -> PyResult<Self> {
            let model = voicevox_core::nonblocking::VoiceModelFile::open(path).await;
            let (model, id, metas) = Python::attach(|py| {
                let model = Python::attach(|py| model.into_py_result(py))?;
                let id = model.id().0.into_pyobject(py)?.into();
                let metas = ToDataclass::ref_cast(model.metas())
                    .into_pyobject(py)?
                    .into();
                Ok::<_, PyErr>((model, id, metas))
            })?;

            let model = Closable::new(model);

            Ok(Self {
                model,
                fields: VoiceModelFilePyFields { id, metas },
            })
        }

        fn __repr__<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyString>> {
            let Self {
                model: rust_api,
                fields,
            } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            let ret = &format!(
                "<voicevox_core.asyncio.{NAME} rust_api=<{rust_api:?}> ",
                NAME = Self::NAME,
            );
            let ret = PyString::new(py, ret);
            let ret = ret.add(fields.format(py)?)?;
            let ret = ret.add(">")?;
            ret.cast_into::<PyString>().map_err(Into::into)
        }

        async fn close(&self) -> PyResult<()> {
            if let Some(this) = self.model.close().await {
                this.close().await;
            }
            Ok(())
        }

        #[getter]
        fn id(&self, py: Python<'_>) -> Py<PyAny> {
            self.fields.id.clone_ref(py)
        }

        #[getter]
        fn metas(&self, py: Python<'_>) -> Py<PyList> {
            self.fields.metas.clone_ref(py)
        }

        fn __aenter__(slf: PyRef<'_, Self>) -> PyResult<Bound<'_, PyAny>> {
            slf.model.read()?;

            let py = slf.py();
            crate::convert::ready(slf, py)
        }

        async fn __aexit__(
            &self,
            #[expect(unused_variables, reason = "`__aexit__`としては必要")] exc_type: Py<PyAny>,
            #[expect(unused_variables, reason = "`__aexit__`としては必要")] exc_value: Py<PyAny>,
            #[expect(unused_variables, reason = "`__aexit__`としては必要")] traceback: Py<PyAny>,
        ) -> PyResult<()> {
            self.close().await
        }
    }

    static ONNXRUNTIME: PyOnceLock<Py<Onnxruntime>> = PyOnceLock::new();

    #[pyclass(frozen)]
    #[derive(Clone)]
    pub(crate) struct Onnxruntime(&'static voicevox_core::nonblocking::Onnxruntime);

    #[pymethods]
    impl Onnxruntime {
        #[classattr]
        const LIB_NAME: &'static str = voicevox_core::nonblocking::Onnxruntime::LIB_NAME;

        #[classattr]
        const LIB_VERSION: &'static str = voicevox_core::nonblocking::Onnxruntime::LIB_VERSION;

        #[classattr]
        const LIB_VERSIONED_FILENAME: &'static str =
            voicevox_core::nonblocking::Onnxruntime::LIB_VERSIONED_FILENAME;

        #[classattr]
        const LIB_UNVERSIONED_FILENAME: &'static str =
            voicevox_core::nonblocking::Onnxruntime::LIB_UNVERSIONED_FILENAME;

        #[new]
        #[classmethod]
        #[pyo3(signature = (*_args, **_kwargs))]
        fn new(
            _cls: Bound<'_, PyType>,
            _args: Bound<'_, PyTuple>,
            _kwargs: Option<Bound<'_, PyDict>>,
        ) -> PyResult<Self> {
            Err(PyTypeError::new_err((
                "`Onnxruntime` does not have a normal constructor. Use `Onnxruntime.load_once` or \
                 `Onnxruntime.get` to construct",
            )))
        }

        #[staticmethod]
        fn get(py: Python<'_>) -> PyResult<Option<Py<Self>>> {
            let result = ONNXRUNTIME.get_or_try_init(py, || {
                match voicevox_core::nonblocking::Onnxruntime::get().map(|o| Py::new(py, Self(o))) {
                    Some(Ok(this)) => Ok(this),
                    Some(Err(err)) => Err(Some(err)),
                    None => Err(None),
                }
            });

            match result {
                Ok(this) => Ok(Some(this.clone_ref(py))),
                Err(Some(err)) => Err(err),
                Err(None) => Ok(None),
            }
        }

        #[staticmethod]
        #[pyo3(signature = (*, filename = Self::LIB_VERSIONED_FILENAME.into()))]
        async fn load_once(filename: OsString) -> PyResult<&'static Py<Onnxruntime>> {
            let inner = voicevox_core::nonblocking::Onnxruntime::load_once()
                .filename(filename)
                .perform()
                .await;

            Python::attach(|py| {
                ONNXRUNTIME.get_or_try_init(py, || Py::new(py, Self(inner.into_py_result(py)?)))
            })
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self(rust_api) = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.asyncio.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        fn supported_devices(&self, py: Python<'_>) -> PyResult<ToDataclass<SupportedDevices>> {
            self.0
                .supported_devices()
                .into_py_result(py)
                .map(Into::into)
        }
    }

    #[pyclass(frozen)]
    #[derive(derive_more::Debug)]
    #[debug("{open_jtalk:?}")]
    pub(crate) struct OpenJtalk {
        open_jtalk: voicevox_core::nonblocking::OpenJtalk,
    }

    #[pymethods]
    impl OpenJtalk {
        #[new]
        #[classmethod]
        #[pyo3(signature = (*_args, **_kwargs))]
        fn __new__(
            _cls: Bound<'_, PyType>,
            _args: Bound<'_, PyTuple>,
            _kwargs: Option<Bound<'_, PyDict>>,
        ) -> PyResult<Self> {
            Err(PyTypeError::new_err((
                "`OpenJtalk` does not have a normal constructor. Use `OpenJtalk.new` to construct",
            )))
        }

        #[staticmethod]
        async fn new(
            #[pyo3(from_py_with = crate::convert::from_utf8_path)] open_jtalk_dict_dir: Utf8PathBuf,
        ) -> PyResult<Self> {
            let open_jtalk = voicevox_core::nonblocking::OpenJtalk::new(open_jtalk_dict_dir).await;
            let open_jtalk = Python::attach(|py| open_jtalk.into_py_result(py))?;
            Ok(Self { open_jtalk })
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self {
                open_jtalk: rust_api,
            } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.asyncio.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        async fn use_user_dict(&self, user_dict: UserDict) -> PyResult<()> {
            let this = self.open_jtalk.clone();
            let result = this.use_user_dict(&user_dict.dict).await;
            Python::attach(|py| result.into_py_result(py))
        }

        async fn analyze(&self, text: String) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let accent_phrases = self.open_jtalk.analyze_(&text).await.map(Into::into);
            Python::attach(|py| accent_phrases.into_py_result(py))
        }
    }

    #[derive(derive_more::Debug)]
    #[debug("{:?}", _0.get())]
    struct OwnedOpenJtalk(Py<OpenJtalk>);

    impl voicevox_core::nonblocking::TextAnalyzer for OwnedOpenJtalk {
        async fn analyze(&self, text: &str) -> anyhow::Result<Vec<AccentPhrase>> {
            self.0.get().open_jtalk.analyze(text).await
        }
    }

    #[pyclass(frozen)]
    pub(crate) struct Synthesizer {
        synthesizer:
            Arc<Closable<voicevox_core::nonblocking::Synthesizer<OwnedOpenJtalk>, Self, Tokio>>,
    }

    #[pymethods]
    impl Synthesizer {
        #[new]
        #[pyo3(signature =(
            onnxruntime,
            open_jtalk,
            *,
            acceleration_mode = Default::default(),
            cpu_num_threads = voicevox_core::__internal::interop::DEFAULT_CPU_NUM_THREADS,
        ))]
        fn new(
            onnxruntime: Onnxruntime,
            open_jtalk: Py<OpenJtalk>,
            #[pyo3(from_py_with = crate::convert::from_acceleration_mode)]
            acceleration_mode: AccelerationMode,
            cpu_num_threads: u16,
        ) -> PyResult<Self> {
            let synthesizer = voicevox_core::nonblocking::Synthesizer::builder(onnxruntime.0)
                .text_analyzer(OwnedOpenJtalk(open_jtalk))
                .acceleration_mode(acceleration_mode)
                .cpu_num_threads(cpu_num_threads)
                .build();
            let synthesizer = Python::attach(|py| synthesizer.into_py_result(py))?;
            let synthesizer = Closable::new(synthesizer).into();
            Ok(Self { synthesizer })
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self {
                synthesizer: rust_api,
            } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.asyncio.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        fn __aenter__(slf: PyRef<'_, Self>) -> PyResult<Bound<'_, PyAny>> {
            slf.synthesizer.read()?;

            let py = slf.py();
            crate::convert::ready(&slf, py)
        }

        async fn __aexit__(
            &self,
            #[expect(unused_variables, reason = "`__aexit__`としては必要")] exc_type: Py<PyAny>,
            #[expect(unused_variables, reason = "`__aexit__`としては必要")] exc_value: Py<PyAny>,
            #[expect(unused_variables, reason = "`__aexit__`としては必要")] traceback: Py<PyAny>,
        ) -> PyResult<()> {
            self.close().await
        }

        #[getter]
        fn onnxruntime(&self, py: Python<'_>) -> Py<Onnxruntime> {
            ONNXRUNTIME
                .get(py)
                .expect("should be initialized")
                .clone_ref(py)
        }

        #[getter]
        fn open_jtalk(&self, py: Python<'_>) -> PyResult<Py<OpenJtalk>> {
            let this = self.synthesizer.read()?;
            Ok(this.text_analyzer().0.clone_ref(py))
        }

        #[getter]
        fn is_gpu_mode(&self) -> PyResult<bool> {
            let synthesizer = self.synthesizer.read()?;
            Ok(synthesizer.is_gpu_mode())
        }

        fn metas(&self) -> PyResult<ToDataclass<VoiceModelMeta>> {
            let synthesizer = self.synthesizer.read()?;
            Ok(synthesizer.metas().into())
        }

        #[pyo3(signature = (model, *, on_existing = Default::default()))]
        async fn load_voice_model(
            &self,
            model: Py<VoiceModelFile>,
            #[pyo3(from_py_with = crate::convert::from_on_existing_voice_model_id)]
            on_existing: OnExistingVoiceModelId,
        ) -> PyResult<()> {
            let model = &*model.get().model.read()?;
            let result = self
                .synthesizer
                .clone()
                .read()?
                .load_voice_model(model)
                .on_existing(on_existing)
                .perform()
                .await;
            Python::attach(|py| result.into_py_result(py))
        }

        fn unload_voice_model(&self, voice_model_id: Uuid, py: Python<'_>) -> PyResult<()> {
            self.synthesizer
                .read()?
                .unload_voice_model(voice_model_id.into())
                .into_py_result(py)
        }

        fn is_loaded_voice_model(&self, voice_model_id: Uuid) -> PyResult<bool> {
            Ok(self
                .synthesizer
                .read()?
                .is_loaded_voice_model(voice_model_id.into()))
        }

        async fn create_audio_query_from_kana(
            &self,
            kana: String,
            style_id: u32,
        ) -> PyResult<ToDataclass<AudioQuery>> {
            let synthesizer = self.synthesizer.clone();
            let audio_query = synthesizer
                .read()?
                .create_audio_query_from_kana(&kana, StyleId::new(style_id))
                .await
                .map(Into::into);
            Python::attach(|py| audio_query.into_py_result(py))
        }

        async fn create_audio_query(
            &self,
            text: String,
            style_id: u32,
        ) -> PyResult<ToDataclass<AudioQuery>> {
            let synthesizer = self.synthesizer.clone();
            let audio_query = synthesizer
                .read()?
                .create_audio_query(&text, StyleId::new(style_id))
                .await
                .map(Into::into);
            Python::attach(|py| audio_query.into_py_result(py))
        }

        async fn create_accent_phrases_from_kana(
            &self,
            kana: String,
            style_id: u32,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.clone();
            let accent_phrases = synthesizer
                .read()?
                .create_accent_phrases_from_kana(&kana, StyleId::new(style_id))
                .await
                .map(Into::into);
            Python::attach(|py| accent_phrases.into_py_result(py))
        }

        async fn create_accent_phrases(
            &self,
            text: String,
            style_id: u32,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.clone();
            let accent_phrases = synthesizer
                .read()?
                .create_accent_phrases(&text, StyleId::new(style_id))
                .await
                .map(Into::into);
            Python::attach(|py| accent_phrases.into_py_result(py))
        }

        async fn replace_mora_data(
            &self,
            #[pyo3(from_py_with = crate::convert::from_accent_phrases)] accent_phrases: Vec<
                AccentPhrase,
            >,
            style_id: u32,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;
            let phrases = synthesizer
                .replace_mora_data(&accent_phrases, style_id.into())
                .await
                .map(Into::into);
            Python::attach(|py| phrases.into_py_result(py))
        }

        async fn replace_phoneme_length(
            &self,
            #[pyo3(from_py_with = crate::convert::from_accent_phrases)] accent_phrases: Vec<
                AccentPhrase,
            >,
            style_id: u32,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;
            let phrases = synthesizer
                .replace_phoneme_length(&accent_phrases, style_id.into())
                .await
                .map(Into::into);
            Python::attach(|py| phrases.into_py_result(py))
        }

        async fn replace_mora_pitch(
            &self,
            #[pyo3(from_py_with = crate::convert::from_accent_phrases)] accent_phrases: Vec<
                AccentPhrase,
            >,
            style_id: u32,
        ) -> PyResult<ToDataclass<Vec<AccentPhrase>>> {
            let synthesizer = self.synthesizer.read()?;
            let phrases = synthesizer
                .replace_mora_pitch(&accent_phrases, style_id.into())
                .await
                .map(Into::into);
            Python::attach(|py| phrases.into_py_result(py))
        }

        #[pyo3(signature=(
            audio_query,
            style_id,
            *,
            enable_interrogative_upspeak =
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
            cancellable = voicevox_core::__internal::interop::DEFAULT_HEAVY_INFERENCE_CANCELLABLE,
        ))]
        async fn synthesis(
            &self,
            #[pyo3(from_py_with = crate::convert::from_audio_query)] audio_query: AudioQuery,
            style_id: u32,
            enable_interrogative_upspeak: bool,
            cancellable: bool,
        ) -> PyResult<Vec<u8>> {
            let synthesizer = self.synthesizer.clone();
            let wav = synthesizer
                .read()?
                .synthesis(&audio_query, StyleId::new(style_id))
                .enable_interrogative_upspeak(enable_interrogative_upspeak)
                .cancellable(cancellable)
                .perform()
                .await;
            Python::attach(|py| wav.into_py_result(py))
        }

        #[pyo3(signature=(
            kana,
            style_id,
            *,
            enable_interrogative_upspeak =
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
            cancellable = voicevox_core::__internal::interop::DEFAULT_HEAVY_INFERENCE_CANCELLABLE,
        ))]
        async fn tts_from_kana(
            &self,
            kana: String,
            style_id: u32,
            enable_interrogative_upspeak: bool,
            cancellable: bool,
        ) -> PyResult<Vec<u8>> {
            let style_id = StyleId::new(style_id);
            let synthesizer = self.synthesizer.clone();
            let wav = synthesizer
                .read()?
                .tts_from_kana(&kana, style_id)
                .enable_interrogative_upspeak(enable_interrogative_upspeak)
                .cancellable(cancellable)
                .perform()
                .await;
            Python::attach(|py| wav.into_py_result(py))
        }

        #[pyo3(signature=(
            text,
            style_id,
            *,
            enable_interrogative_upspeak =
                voicevox_core::__internal::interop::DEFAULT_ENABLE_INTERROGATIVE_UPSPEAK,
            cancellable = voicevox_core::__internal::interop::DEFAULT_HEAVY_INFERENCE_CANCELLABLE,
        ))]
        async fn tts(
            &self,
            text: String,
            style_id: u32,
            enable_interrogative_upspeak: bool,
            cancellable: bool,
        ) -> PyResult<Vec<u8>> {
            let style_id = StyleId::new(style_id);
            let synthesizer = self.synthesizer.clone();
            let wav = synthesizer
                .read()?
                .tts(&text, style_id)
                .enable_interrogative_upspeak(enable_interrogative_upspeak)
                .cancellable(cancellable)
                .perform()
                .await;
            Python::attach(|py| wav.into_py_result(py))
        }

        async fn create_sing_frame_audio_query(
            &self,
            #[pyo3(from_py_with = crate::convert::from_query_like_via_serde)] score: Score,
            style_id: u32,
        ) -> PyResult<ToDataclass<FrameAudioQuery>> {
            let style_id = StyleId::new(style_id);
            let frame_audio_query = self
                .synthesizer
                .read()?
                .create_sing_frame_audio_query(&score, style_id)
                .await
                .map(Into::into);
            Python::attach(|py| frame_audio_query.into_py_result(py))
        }

        async fn create_sing_frame_f0(
            &self,
            #[pyo3(from_py_with = crate::convert::from_query_like_via_serde)] score: Score,
            #[pyo3(from_py_with = crate::convert::from_audio_query)]
            frame_audio_query: FrameAudioQuery,
            style_id: u32,
        ) -> PyResult<Vec<f32>> {
            let style_id = StyleId::new(style_id);

            // TODO: typed_floatsにissueかPRを出しに行き、スライス変換かbytemuck対応を入れてもらう
            let f0s = self
                .synthesizer
                .read()?
                .create_sing_frame_f0(&score, &frame_audio_query, style_id)
                .await
                .map(|f0s| f0s.into_iter().map(Into::into).collect());
            Python::attach(|py| f0s.into_py_result(py))
        }

        async fn create_sing_frame_volume(
            &self,
            #[pyo3(from_py_with = crate::convert::from_query_like_via_serde)] score: Score,
            #[pyo3(from_py_with = crate::convert::from_audio_query)]
            frame_audio_query: FrameAudioQuery,
            style_id: u32,
        ) -> PyResult<Vec<f32>> {
            let style_id = StyleId::new(style_id);

            // TODO: typed_floatsにissueかPRを出しに行き、スライス変換かbytemuck対応を入れてもらう
            let volumes = self
                .synthesizer
                .read()?
                .create_sing_frame_volume(&score, &frame_audio_query, style_id)
                .await
                .map(|volumes| volumes.into_iter().map(Into::into).collect());
            Python::attach(|py| volumes.into_py_result(py))
        }

        #[pyo3(signature=(
            frame_audio_query,
            style_id,
            *,
            cancellable = voicevox_core::__internal::interop::DEFAULT_HEAVY_INFERENCE_CANCELLABLE,
        ))]
        async fn frame_synthesis(
            &self,
            #[pyo3(from_py_with = crate::convert::from_audio_query)]
            frame_audio_query: FrameAudioQuery,
            style_id: u32,
            cancellable: bool,
        ) -> PyResult<Vec<u8>> {
            let style_id = StyleId::new(style_id);
            let wav = self
                .synthesizer
                .read()?
                .frame_synthesis(&frame_audio_query, style_id)
                .cancellable(cancellable)
                .perform()
                .await;
            Python::attach(|py| wav.into_py_result(py))
        }

        async fn close(&self) -> PyResult<()> {
            let this = self.synthesizer.clone();
            if let Some(this) = this.close().await {
                blocking::unblock(|| drop(this)).await;
            }
            Ok(())
        }
    }

    #[pyclass(frozen)]
    #[derive(Default, Debug, Clone)]
    pub(crate) struct UserDict {
        dict: Arc<voicevox_core::nonblocking::UserDict>,
    }

    #[pymethods]
    impl UserDict {
        #[new]
        fn new() -> Self {
            Self::default()
        }

        fn __repr__(&self, py: Python<'_>) -> String {
            let Self { dict: rust_api } = self;
            let rust_api = PyString::new(py, &format!("{rust_api:?}"));
            format!(
                "<voicevox_core.asyncio.{NAME} rust_api=<{rust_api:?}>>",
                NAME = Self::NAME,
            )
        }

        async fn load(&self, path: PathBuf) -> PyResult<()> {
            let this = self.dict.clone();
            let result = this.load(&path).await;
            Python::attach(|py| result.into_py_result(py))
        }

        async fn save(&self, path: PathBuf) -> PyResult<()> {
            let this = self.dict.clone();
            let result = this.save(&path).await;
            Python::attach(|py| result.into_py_result(py))
        }

        fn add_word(
            &self,
            #[pyo3(from_py_with = crate::convert::to_rust_user_dict_word)] word: UserDictWord,
            py: Python<'_>,
        ) -> PyResult<Uuid> {
            self.dict.add_word(word).into_py_result(py)
        }

        fn update_word(
            &self,
            word_uuid: Uuid,
            #[pyo3(from_py_with = crate::convert::to_rust_user_dict_word)] word: UserDictWord,
            py: Python<'_>,
        ) -> PyResult<()> {
            self.dict.update_word(word_uuid, word).into_py_result(py)?;
            Ok(())
        }

        fn remove_word(&self, word_uuid: Uuid, py: Python<'_>) -> PyResult<()> {
            self.dict.remove_word(word_uuid).into_py_result(py)?;
            Ok(())
        }

        fn import_dict(&self, other: &UserDict, py: Python<'_>) -> PyResult<()> {
            self.dict.import(&other.dict).into_py_result(py)?;
            Ok(())
        }

        fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
            self.dict.with_words(|words| {
                words
                    .iter()
                    .map(|(&uuid, word)| (uuid, ToDataclass::ref_cast(word)))
                    .into_py_dict(py)
            })
        }
    }
}
