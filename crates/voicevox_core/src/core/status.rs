use std::{
    any,
    fmt::{self, Debug},
};

use duplicate::{duplicate, duplicate_item};
use educe::Educe;
use enum_map::EnumMap;
use indexmap::IndexMap;
use itertools::iproduct;

use crate::{
    Result,
    error::{ErrorRepr, LoadModelError, LoadModelErrorKind, LoadModelResult},
};

use super::{
    infer::{
        self, InferenceDomain, InferenceInputSignature, InferenceRuntime, InferenceSessionOptions,
        InferenceSignature,
        domains::{
            ExperimentalTalkDomain, FrameDecodeDomain, InferenceDomainMap, SingingTeacherDomain,
            TalkDomain, inference_domain_map_values,
        },
        session_set::{InferenceSessionCell, InferenceSessionSet},
    },
    manifest::{InnerVoiceId, StyleIdToInnerVoiceId},
    metas::{self, CharacterMeta, StyleId, StyleMeta, VoiceModelMeta},
    voice_model::{ModelBytesWithInnerVoiceIdsByDomain, VoiceModelHeader, VoiceModelId},
};

/// `Synthesizer::load_voice_model`の実行時に、同じ[`id`]の`VoiceModelFile`が既に読み込まれていたときのふるまい。
///
/// [`id`]: VoiceModelId
#[cfg_attr(doc, doc(alias = "VoicevoxLoadVoiceModelOptions"))]
#[derive(Default, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug)]
#[cfg_attr(test, derive(strum::EnumIter))]
#[non_exhaustive]
pub enum OnExistingVoiceModelId {
    /// エラー。
    ///
    /// デフォルトのふるまい。
    #[default]
    Error,

    /// 再読み込みする。
    ///
    /// VOICEVOX
    /// COREでは、長文のテキストを一度に音声合成するとCPU/GPUメモリが大量に占有されたままになる。再読み込みを行うとメモリの使用量が元に戻る。
    Reload,

    /// 何もしない。
    Skip,
}

#[derive(Debug)]
pub(crate) struct Status<R: InferenceRuntime> {
    pub(crate) rt: &'static R,
    loaded_models: std::sync::Mutex<LoadedModels<R>>,
    session_options: InferenceDomainMap<SessionOptionsByDomain>,
}

impl<R: InferenceRuntime> Status<R> {
    pub(crate) fn new(
        rt: &'static R,
        session_options: InferenceDomainMap<SessionOptionsByDomain>,
    ) -> Self {
        Self {
            rt,
            loaded_models: Default::default(),
            session_options,
        }
    }

    pub(crate) fn insert_model(
        &self,
        model_header: &VoiceModelHeader,
        model_contents: &InferenceDomainMap<ModelBytesWithInnerVoiceIdsByDomain>,
        on_existing: OnExistingVoiceModelId,
    ) -> Result<()> {
        self.loaded_models
            .lock()
            .unwrap()
            .ensure_acceptable(model_header, on_existing)?;

        let session_sets_with_inner_ids = model_contents
            .create_session_sets(self.rt, &self.session_options)
            .map_err(|source| LoadModelError {
                path: model_header.path.clone(),
                context: LoadModelErrorKind::InvalidModelData,
                source: Some(source),
            })?;

        self.loaded_models.lock().unwrap().insert(
            model_header,
            session_sets_with_inner_ids,
            on_existing,
        )?;
        Ok(())
    }

    pub(crate) fn unload_model(&self, voice_model_id: VoiceModelId) -> Result<()> {
        self.loaded_models.lock().unwrap().remove(voice_model_id)
    }

    pub(crate) fn metas(&self) -> VoiceModelMeta {
        self.loaded_models.lock().unwrap().metas()
    }

    /// あるスタイルに対応する`VoiceModelId`と`InnerVoiceId`の組を返す。
    ///
    /// `StyleId` → `InnerVoiceId`のマッピングが存在しない場合は、`InnerVoiceId`としては
    /// `style_id`と同じ値を返す。
    pub(crate) fn ids_for<D: InferenceDomainExt>(
        &self,
        style_id: StyleId,
    ) -> Result<(VoiceModelId, InnerVoiceId)> {
        self.loaded_models.lock().unwrap().ids_for::<D>(style_id)
    }

    pub(crate) fn contains_domain<D: InferenceDomainExt>(&self, style_id: StyleId) -> bool {
        self.loaded_models
            .lock()
            .unwrap_or_else(|e| panic!("{e}"))
            .contains_domain::<D>(style_id)
    }

    pub(crate) fn is_loaded_model(&self, voice_model_id: VoiceModelId) -> bool {
        self.loaded_models
            .lock()
            .unwrap()
            .contains_voice_model(voice_model_id)
    }

    // FIXME: この関数はcompatible_engineとテストでのみ使われるが、テストのために`StyleType`を
    // 引数に含めるようにする
    pub(crate) fn is_loaded_model_by_style_id(&self, style_id: StyleId) -> bool {
        self.loaded_models.lock().unwrap().contains_style(style_id)
    }

    /// 推論を実行する。
    ///
    /// # Performance
    ///
    /// CPU/GPU-boundな操作であるため、非同期ランタイム上では直接実行されるべきではない。
    ///
    /// # Panics
    ///
    /// `self`が`model_id`を含んでいないとき、パニックする。
    pub(crate) async fn run_session<A, I>(
        &self,
        model_id: VoiceModelId,
        input: I,
        cancellable: A::Cancellable,
    ) -> Result<<I::Signature as InferenceSignature>::Output>
    where
        A: infer::AsyncExt,
        I: InferenceInputSignature,
        <I::Signature as InferenceSignature>::Domain: InferenceDomainExt,
    {
        let sess = self.loaded_models.lock().unwrap().get(model_id);
        sess.run::<A>(input, cancellable).await
    }
}

/// 読み込んだモデルの`Session`とそのメタ情報を保有し、追加/削除/取得の操作を提供する。
///
/// この構造体のメソッドは、すべて一瞬で完了すべきである。
#[derive(Educe)]
#[educe(Default(bound = "R: InferenceRuntime"))]
struct LoadedModels<R: InferenceRuntime>(IndexMap<VoiceModelId, LoadedModel<R>>);

impl<R: InferenceRuntime> Debug for LoadedModels<R> {
    fn fmt(&self, fmt: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt.debug_map()
            .entries(self.0.keys().map(|id| (id, format_args!("_"))))
            .finish()
    }
}

struct LoadedModel<R: InferenceRuntime> {
    metas: VoiceModelMeta,
    session_sets_with_inner_ids: InferenceDomainMap<SessionSetsWithInnerVoiceIdsByDomain<R>>,
}

impl<R: InferenceRuntime> LoadedModels<R> {
    fn metas(&self) -> VoiceModelMeta {
        metas::merge(self.0.values().flat_map(|LoadedModel { metas, .. }| metas))
    }

    fn ids_for<D: InferenceDomainExt>(
        &self,
        style_id: StyleId,
    ) -> Result<(VoiceModelId, InnerVoiceId)> {
        let (
            model_id,
            LoadedModel {
                session_sets_with_inner_ids,
                ..
            },
        ) = self
            .0
            .iter()
            .filter(
                |(
                    _,
                    LoadedModel {
                        session_sets_with_inner_ids,
                        ..
                    },
                )| D::visit(session_sets_with_inner_ids).is_some(),
            )
            .find(|(_, LoadedModel { metas, .. })| {
                metas
                    .iter()
                    .flat_map(|CharacterMeta { styles, .. }| styles)
                    .any(|style| style.id == style_id && D::style_types().contains(&style.r#type))
            })
            .ok_or(ErrorRepr::StyleNotFound {
                style_id,
                style_types: D::style_types(),
            })?;

        let inner_voice_id = session_sets_with_inner_ids
            .get::<D>()
            .as_ref()
            .and_then(|(inner_voice_ids, _)| inner_voice_ids.get(&style_id).copied())
            .unwrap_or_else(|| InnerVoiceId::new(style_id.0));

        Ok((*model_id, inner_voice_id))
    }

    /// # Panics
    ///
    /// 次の場合にパニックする。
    ///
    /// - `self`が`model_id`を含んでいないとき
    /// - 対応する`InferenceDomain`が欠けているとき
    fn get<I>(&self, model_id: VoiceModelId) -> InferenceSessionCell<R, I>
    where
        I: InferenceInputSignature,
        <I::Signature as InferenceSignature>::Domain: InferenceDomainExt,
    {
        let (_, session_set) = self.0[&model_id]
            .session_sets_with_inner_ids
            .get::<<I::Signature as InferenceSignature>::Domain>()
            .as_ref()
            .unwrap_or_else(|| {
                let type_name = any::type_name::<<I::Signature as InferenceSignature>::Domain>()
                    .split("::")
                    .last()
                    .unwrap();
                panic!(
                    "missing session set for `{type_name}` (should be checked in \
                     `VoiceModelHeader::new` and `ids_for`)",
                );
            });
        session_set.get()
    }

    fn contains_domain<D: InferenceDomainExt>(&self, style_id: StyleId) -> bool {
        self.0
            .iter()
            .find(|(_, LoadedModel { metas, .. })| {
                metas
                    .iter()
                    .flat_map(|CharacterMeta { styles, .. }| styles)
                    .any(|style| style.id == style_id && D::style_types().contains(&style.r#type))
            })
            .and_then(
                |(
                    _,
                    LoadedModel {
                        session_sets_with_inner_ids,
                        ..
                    },
                )| session_sets_with_inner_ids.get::<D>(),
            )
            .is_some()
    }

    fn contains_voice_model(&self, model_id: VoiceModelId) -> bool {
        self.0.contains_key(&model_id)
    }

    fn contains_style(&self, style_id: StyleId) -> bool {
        self.styles().any(|style| style.id == style_id)
    }

    /// 音声モデルを受け入れ可能かをチェックする。
    ///
    /// # Errors
    ///
    /// 次の場合にエラーを返す。
    ///
    /// 1. `on_existing`が`Error`かつ、現在持っている音声モデルIDが`model_header`と重複するとき
    /// 2. 1.を満たさず、現在持っているスタイルIDが`model_header`と重複するとき
    // FIXME: コメントとテストを書く
    // - https://github.com/VOICEVOX/voicevox_core/pull/761#discussion_r1589978521
    // - https://github.com/VOICEVOX/voicevox_core/pull/761#discussion_r1589976759
    fn ensure_acceptable(
        &self,
        model_header: &VoiceModelHeader,
        on_existing: OnExistingVoiceModelId,
    ) -> LoadModelResult<()> {
        let error = |context| LoadModelError {
            path: model_header.path.clone(),
            context,
            source: None,
        };

        if self.0.contains_key(&model_header.manifest.id) {
            return match on_existing {
                OnExistingVoiceModelId::Error => {
                    Err(error(LoadModelErrorKind::ModelAlreadyLoaded {
                        id: model_header.manifest.id,
                    }))
                }
                OnExistingVoiceModelId::Reload | OnExistingVoiceModelId::Skip => Ok(()),
            };
        }

        // FIXME: https://github.com/VOICEVOX/voicevox_core/pull/761#discussion_r1590200343

        let loaded = self.characters();
        let external = model_header.metas.iter();
        for (loaded, external) in iproduct!(loaded, external) {
            if loaded.speaker_uuid == external.speaker_uuid {
                loaded.warn_diff_except_styles(external);
            }
        }

        let loaded = self.styles().map(|&StyleMeta { id, .. }| id);
        let external = model_header
            .metas
            .iter()
            .flat_map(|CharacterMeta { styles, .. }| styles)
            .map(|&StyleMeta { id, .. }| id);
        if let Some((id, _)) =
            iproduct!(loaded, external).find(|(loaded, external)| loaded == external)
        {
            return Err(error(LoadModelErrorKind::StyleAlreadyLoaded { id }));
        }
        Ok(())
    }

    fn insert(
        &mut self,
        model_header: &VoiceModelHeader,
        session_sets_with_inner_ids: InferenceDomainMap<SessionSetsWithInnerVoiceIdsByDomain<R>>,
        on_existing: OnExistingVoiceModelId,
    ) -> Result<()> {
        self.ensure_acceptable(model_header, on_existing)?;

        let entry = self.0.entry(model_header.manifest.id);
        let model = LoadedModel {
            metas: model_header.metas.clone(),
            session_sets_with_inner_ids,
        };

        match entry {
            indexmap::map::Entry::Occupied(mut entry) => match on_existing {
                OnExistingVoiceModelId::Error => {
                    unreachable!("should have been rejected by `ensure_acceptable`");
                }
                OnExistingVoiceModelId::Reload => {
                    entry.insert(model);
                    Ok(())
                }
                OnExistingVoiceModelId::Skip => Ok(()),
            },
            indexmap::map::Entry::Vacant(entry) => {
                entry.insert(model);
                Ok(())
            }
        }
    }

    fn remove(&mut self, model_id: VoiceModelId) -> Result<()> {
        if self.0.shift_remove(&model_id).is_none() {
            return Err(ErrorRepr::ModelNotFound { model_id }.into());
        }
        Ok(())
    }

    fn characters(&self) -> impl Iterator<Item = &CharacterMeta> + Clone {
        self.0.values().flat_map(|LoadedModel { metas, .. }| metas)
    }

    fn styles(&self) -> impl Iterator<Item = &StyleMeta> {
        self.characters()
            .flat_map(|CharacterMeta { styles, .. }| styles)
    }
}

pub(crate) trait InferenceDomainExt: InferenceDomain {
    fn visit<R: InferenceRuntime>(
        map: &InferenceDomainMap<SessionSetsWithInnerVoiceIdsByDomain<R>>,
    ) -> Option<&(StyleIdToInnerVoiceId, InferenceSessionSet<R, Self>)>;
}

#[duplicate_item(
    T                        field;
    [ TalkDomain ]           [ talk ];
    [ ExperimentalTalkDomain ] [ experimental_talk ];
    [ SingingTeacherDomain ] [ singing_teacher ];
    [ FrameDecodeDomain ]    [ frame_decode ];
)]
impl InferenceDomainExt for T {
    fn visit<R: InferenceRuntime>(
        map: &InferenceDomainMap<SessionSetsWithInnerVoiceIdsByDomain<R>>,
    ) -> Option<&(StyleIdToInnerVoiceId, InferenceSessionSet<R, Self>)> {
        map.field.as_ref()
    }
}

impl<R: InferenceRuntime> InferenceDomainMap<SessionSetsWithInnerVoiceIdsByDomain<R>> {
    fn get<D: InferenceDomainExt>(
        &self,
    ) -> Option<&(StyleIdToInnerVoiceId, InferenceSessionSet<R, D>)> {
        D::visit(self)
    }
}

impl InferenceDomainMap<ModelBytesWithInnerVoiceIdsByDomain> {
    fn create_session_sets<R: InferenceRuntime>(
        &self,
        rt: &R,
        session_options: &InferenceDomainMap<SessionOptionsByDomain>,
    ) -> anyhow::Result<InferenceDomainMap<SessionSetsWithInnerVoiceIdsByDomain<R>>> {
        duplicate! {
            [
                field;
                [ talk ];
                [ experimental_talk ];
                [ singing_teacher ];
                [ frame_decode ];
            ]
            let field = self
                .field
                .as_ref()
                .map(|(inner_voice_ids, model_bytes)| {
                    let session_set = InferenceSessionSet::new(rt, model_bytes, &session_options.field)?;
                    Ok::<_, anyhow::Error>((inner_voice_ids.clone(), session_set))
                })
                .transpose()?;
        }

        Ok(InferenceDomainMap {
            talk,
            experimental_talk,
            singing_teacher,
            frame_decode,
        })
    }
}

type SessionOptionsByDomain =
    inference_domain_map_values!(for<D> EnumMap<D::Operation, InferenceSessionOptions>);

type SessionSetsWithInnerVoiceIdsByDomain<R> =
    inference_domain_map_values!(for<D> Option<(StyleIdToInnerVoiceId, InferenceSessionSet<R, D>)>);

#[cfg(test)]
mod tests {
    use std::sync::{Arc, LazyLock};

    use enum_map::{Enum, EnumMap, enum_map};
    use ndarray::{Array, Dimension};
    use pretty_assertions::assert_eq;
    use rstest::{fixture, rstest};
    use strum::IntoEnumIterator as _;
    use uuid::{Uuid, uuid};

    use crate::{
        CharacterMeta, CharacterVersion, OnExistingVoiceModelId, StyleMeta, StyleType,
        SupportedDevices,
    };

    use super::{
        super::{
            devices::{DeviceSpec, GpuSpec},
            infer::{
                InferenceOperation, InferenceRuntime, InferenceSessionOptions, InputScalarKind,
                OutputScalarKind, OutputTensor, ParamInfo, PushInputTensor,
                domains::{
                    ExperimentalTalkOperation, FrameDecodeOperation, InferenceDomainMap,
                    SingingTeacherOperation, TalkOperation, inference_domain_map,
                },
            },
            voice_model::{ModelBytes, ModelBytesWithInnerVoiceIdsByDomain, VoiceModelHeader},
        },
        Status,
    };

    #[rstest]
    #[case(DeviceSpec::Gpu(GpuSpec::Cuda), 0)]
    #[case(DeviceSpec::Gpu(GpuSpec::Cuda), 1)]
    #[case(DeviceSpec::Gpu(GpuSpec::Cuda), 8)]
    #[case(DeviceSpec::Cpu, 2)]
    #[case(DeviceSpec::Cpu, 4)]
    #[case(DeviceSpec::Cpu, 8)]
    #[case(DeviceSpec::Cpu, 0)]
    fn status_new_works(#[case] device_for_heavy: DeviceSpec, #[case] cpu_num_threads: u16) {
        let light_session_options = InferenceSessionOptions::new(cpu_num_threads, DeviceSpec::Cpu);
        let heavy_session_options = InferenceSessionOptions::new(cpu_num_threads, device_for_heavy);
        let session_options = InferenceDomainMap {
            talk: enum_map! {
                TalkOperation::PredictDuration | TalkOperation::PredictIntonation => {
                    light_session_options
                }
                TalkOperation::Decode => heavy_session_options,
            },
            experimental_talk: enum_map! {
                ExperimentalTalkOperation::PredictDuration
                | ExperimentalTalkOperation::PredictIntonation
                | ExperimentalTalkOperation::GenerateFullIntermediate => light_session_options,
                ExperimentalTalkOperation::RenderAudioSegment => heavy_session_options,
            },
            singing_teacher: enum_map! {
                SingingTeacherOperation::PredictSingConsonantLength
                | SingingTeacherOperation::PredictSingF0
                | SingingTeacherOperation::PredictSingVolume => light_session_options,
            },
            frame_decode: enum_map! {
                FrameDecodeOperation::SfDecode => heavy_session_options,
            },
        };
        let status = Status::new(&InferenceRuntimeMock, session_options);

        assert_eq!(
            light_session_options,
            status.session_options.experimental_talk[ExperimentalTalkOperation::PredictDuration],
        );
        assert_eq!(
            light_session_options,
            status.session_options.experimental_talk[ExperimentalTalkOperation::PredictIntonation],
        );
        assert_eq!(
            light_session_options,
            status.session_options.experimental_talk
                [ExperimentalTalkOperation::GenerateFullIntermediate],
        );
        assert_eq!(
            heavy_session_options,
            status.session_options.experimental_talk[ExperimentalTalkOperation::RenderAudioSegment],
        );

        assert!(status.loaded_models.lock().unwrap().0.is_empty());
    }

    #[rstest]
    fn is_loaded_model_returns_false_for_nonexisting_model_id(
        status: Status<InferenceRuntimeMock>,
    ) {
        assert!(!status.is_loaded_model(uuid!("00000000-0000-4000-a000-000000000001").into()));
    }

    #[rstest]
    fn insert_model_without_id_duplications_succeeds_regardless_of_on_existing(
        status: Status<InferenceRuntimeMock>,
    ) {
        let h1 = &header(uuid!("00000000-0000-4000-a000-000000000001"), [0, 1]);
        let h2 = &header(uuid!("00000000-0000-4000-a000-000000000002"), [2, 3]);

        for o in OnExistingVoiceModelId::iter() {
            status.insert_model(h1, &DUMMY_CONTENTS, o).unwrap();
            status.insert_model(h2, &DUMMY_CONTENTS, o).unwrap();
            assert!(status.is_loaded_model(h1.manifest.id));
            assert!(status.is_loaded_model(h2.manifest.id));

            status.unload_model(h1.manifest.id).unwrap();
            status.unload_model(h2.manifest.id).unwrap();
            assert!(!status.is_loaded_model(h1.manifest.id));
            assert!(!status.is_loaded_model(h2.manifest.id));
        }
    }

    #[rstest]
    fn style_id_duplication_is_denied_regardless_of_on_existing(
        status: Status<InferenceRuntimeMock>,
    ) {
        use OnExistingVoiceModelId::Error;

        let h1 = &header(uuid!("00000000-0000-4000-a000-000000000001"), [0, 1]);
        let h2 = &header(uuid!("00000000-0000-4000-a000-000000000002"), [1, 2]);

        status.insert_model(h1, &DUMMY_CONTENTS, Error).unwrap();

        for o in OnExistingVoiceModelId::iter() {
            let err = status.insert_model(h2, &DUMMY_CONTENTS, o).unwrap_err();
            assert_eq!(crate::ErrorKind::StyleAlreadyLoaded, err.kind());
        }
    }

    #[rstest]
    fn same_model_id_bypasses_style_id_check(status: Status<InferenceRuntimeMock>) {
        use OnExistingVoiceModelId::{Error, Reload, Skip};

        let h1 = &header(uuid!("00000000-0000-4000-a000-000000000001"), [0, 1]);
        let h2 = &header(uuid!("00000000-0000-4000-a000-000000000001"), [1, 2]);

        status.insert_model(h1, &DUMMY_CONTENTS, Error).unwrap();

        let err = status.insert_model(h1, &DUMMY_CONTENTS, Error).unwrap_err();
        assert_eq!(crate::ErrorKind::ModelAlreadyLoaded, err.kind());
        status.insert_model(h2, &DUMMY_CONTENTS, Reload).unwrap();
        status.insert_model(h1, &DUMMY_CONTENTS, Skip).unwrap();
    }

    #[rstest]
    fn on_existing_error_denies_model_id_duplication(status: Status<InferenceRuntimeMock>) {
        use OnExistingVoiceModelId::Error;

        let h = &header(uuid!("00000000-0000-4000-a000-000000000001"), [0]);

        status.insert_model(h, &DUMMY_CONTENTS, Error).unwrap();
        let err = status.insert_model(h, &DUMMY_CONTENTS, Error).unwrap_err();
        assert_eq!(crate::ErrorKind::ModelAlreadyLoaded, err.kind());
    }

    #[rstest]
    fn on_existing_reload_allows_model_id_duplication(status: Status<InferenceRuntimeMock>) {
        use OnExistingVoiceModelId::Reload;

        let h = &header(uuid!("00000000-0000-4000-a000-000000000001"), [0]);

        status.insert_model(h, &DUMMY_CONTENTS, Reload).unwrap();
        status.insert_model(h, &DUMMY_CONTENTS, Reload).unwrap();
    }

    #[rstest]
    fn on_existing_skip_allows_model_id_duplication(status: Status<InferenceRuntimeMock>) {
        use OnExistingVoiceModelId::Skip;

        let h = &header(uuid!("00000000-0000-4000-a000-000000000001"), [0]);

        status.insert_model(h, &DUMMY_CONTENTS, Skip).unwrap();
        status.insert_model(h, &DUMMY_CONTENTS, Skip).unwrap();
    }

    #[fixture]
    fn status() -> Status<InferenceRuntimeMock> {
        Status::new(
            &InferenceRuntimeMock,
            inference_domain_map!(enum_map!(_ => InferenceSessionOptions::new(0, DeviceSpec::Cpu))),
        )
    }

    fn header<const N: usize>(model_id: Uuid, styles: [u32; N]) -> VoiceModelHeader {
        VoiceModelHeader {
            manifest: serde_json::from_str(&format!(
                r#"
                {{
                  "vvm_format_version": 1,
                  "id": "{model_id}",
                  "metas_filename": "metas.json",
                  "talk": {{
                    "predict_duration": {{
                      "type": "onnx",
                      "filename": "predict_duration.onnx"
                    }},
                    "predict_intonation": {{
                      "type": "onnx",
                      "filename": "predict_intonation.onnx"
                    }},
                    "decode": {{
                      "type": "onnx",
                      "filename": "decode.onnx"
                    }}
                  }}
                }}"#,
            ))
            .unwrap(),
            metas: [CharacterMeta {
                name: "".to_owned(),
                styles: styles
                    .into_iter()
                    .map(|id| StyleMeta {
                        id: id.into(),
                        name: "".to_owned(),
                        r#type: StyleType::Talk,
                        order: None,
                    })
                    .collect(),
                version: CharacterVersion("".to_owned()),
                speaker_uuid: "".to_owned(),
                order: None,
            }]
            .into(),
            path: "".into(),
        }
    }

    static DUMMY_CONTENTS: LazyLock<InferenceDomainMap<ModelBytesWithInnerVoiceIdsByDomain>> =
        LazyLock::new(|| InferenceDomainMap {
            talk: Some((
                Default::default(),
                EnumMap::from_fn(|op: TalkOperation| ModelBytes::Onnx(vec![op.into_usize() as u8])),
            )),
            experimental_talk: None,
            singing_teacher: None,
            frame_decode: None,
        });

    struct InferenceRuntimeMock;

    impl InferenceRuntime for InferenceRuntimeMock {
        type Session = ();
        type RunContext = DummyRunContext;

        const DISPLAY_NAME: &'static str = "InferenceRuntimeMock";

        fn supported_devices(&self) -> crate::Result<SupportedDevices> {
            unimplemented!();
        }

        fn test_gpu(&self, _: GpuSpec) -> anyhow::Result<()> {
            unimplemented!();
        }

        fn new_session(
            &self,
            model: &ModelBytes,
            _: InferenceSessionOptions,
        ) -> anyhow::Result<(
            Self::Session,
            Vec<ParamInfo<InputScalarKind>>,
            Vec<ParamInfo<OutputScalarKind>>,
        )> {
            let ModelBytes::Onnx(model) = model else {
                unreachable!()
            };
            let [op] = **model else { unreachable!() };
            let op = TalkOperation::from_usize(op.into());
            let (in_infos, out_infos) = TalkOperation::PARAM_INFOS[op];
            Ok(((), in_infos.to_owned(), out_infos.to_owned()))
        }

        fn run_blocking(_: Self::RunContext) -> anyhow::Result<Vec<OutputTensor>> {
            unimplemented!();
        }

        async fn run_async(_: Self::RunContext, _: bool) -> anyhow::Result<Vec<OutputTensor>> {
            unimplemented!();
        }
    }

    enum DummyRunContext {}

    impl<T> From<Arc<T>> for DummyRunContext {
        fn from(_: Arc<T>) -> Self {
            unimplemented!();
        }
    }

    impl PushInputTensor for DummyRunContext {
        fn push_int64(
            &mut self,
            _: &'static str,
            _: Array<i64, impl Dimension + 'static>,
        ) -> anyhow::Result<()> {
            unimplemented!();
        }

        fn push_float32(
            &mut self,
            _: &'static str,
            _: Array<f32, impl Dimension + 'static>,
        ) -> anyhow::Result<()> {
            unimplemented!();
        }
    }
}
