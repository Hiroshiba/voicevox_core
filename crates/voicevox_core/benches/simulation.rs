// FIXME: `walltime`とコードの重複が多々あるのでなんとかする

use voicevox_core::StyleId;

fn main() {
    voicevox_core::blocking::Onnxruntime::load_once()
        .filename(test_util::ONNXRUNTIME_DYLIB_PATH)
        .perform()
        .unwrap();
    let _ = *blocking::FIXTURE;

    divan::main();
}

const STYLE_ID: StyleId = StyleId(0);

#[derive(Clone, Copy, derive_more::Display)]
#[display("{name}")]
struct InputText {
    // `value`をテストケース名にしまうと視認性が著しく損なわれるため、短い識別子として`name`を用意
    name: &'static str,
    value: &'static str,
}

impl InputText {
    const ALL: &[Self] = &[Self::SHORT, Self::LONG];

    const SHORT: Self = Self {
        name: "SHORT",
        value: "この音声は、ボイスボックスを使用して、出力されています。",
    };

    const LONG: Self = Self {
        name: "LONG",
        value: "文章を書くとき一つの文にあれもこれも詰め込みたくなるが、\
            一般的に文章には適切な区切りが必要であり、\
            このような句読点がいくつもあるような文章は、\
            小説などでは例外かもしれないが少なくとも何かを説明するときには、\
            普通悪文とされる。",
    };
}

mod blocking {
    use std::sync::LazyLock;

    use divan::Bencher;
    use voicevox_core::{
        AccelerationMode,
        blocking::{Onnxruntime, OpenJtalk, Synthesizer, TextAnalyzer as _, VoiceModelFile},
    };

    use crate::{InputText, STYLE_ID};

    pub(crate) static FIXTURE: LazyLock<(Synthesizer<OpenJtalk>, VoiceModelFile)> =
        LazyLock::new(|| {
            let ort = Onnxruntime::get().expect("should have been initialized");
            let ojt = OpenJtalk::new(test_util::OPEN_JTALK_DIC_DIR).unwrap();
            let synth = Synthesizer::builder(ort)
                .text_analyzer(ojt)
                .acceleration_mode(AccelerationMode::Cpu)
                .build()
                .unwrap();
            let vvm = VoiceModelFile::open(test_util::SAMPLE_VOICE_MODEL_FILE_PATH).unwrap();
            synth.load_voice_model(&vvm).perform().unwrap();
            (synth, vvm)
        });

    #[divan::bench(args = InputText::ALL, sample_count = 300, sample_size = 10)]
    fn analyze_text(bencher: Bencher<'_, '_>, input: InputText) {
        let ojt = FIXTURE.0.text_analyzer();
        bencher.bench_local(|| ojt.analyze(input.value).unwrap());
    }

    #[divan::bench(args = InputText::ALL, sample_count = 300, sample_size = 10)]
    fn replace_mora_pitch(bencher: Bencher<'_, '_>, input: InputText) {
        let (synth, _) = &*FIXTURE;

        let input = &synth.text_analyzer().analyze(input.value).unwrap();

        bencher.bench_local(|| synth.replace_mora_pitch(input, STYLE_ID).unwrap());
    }

    #[divan::bench(args = InputText::ALL, sample_count = 300, sample_size = 10)]
    fn replace_phoneme_length(bencher: Bencher<'_, '_>, input: InputText) {
        let (synth, _) = &*FIXTURE;

        let input = &synth.text_analyzer().analyze(input.value).unwrap();

        bencher.bench_local(|| synth.replace_phoneme_length(input, STYLE_ID).unwrap());
    }
}
