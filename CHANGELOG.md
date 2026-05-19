<!-- このChangelogの書き方の方針については、./docs/guide/dev/changelog.mdにまとめる。 -->

# Changelog

## [Unreleased]

<!--
### macOSのXCFramework追加

- \[C\] \[macOS\] :tada: GitHub ReleasesのXCFrameworkが、macOS向けのライブラリも同梱するようになります ([#1056] helped by [@nekomimimi])。

    ```diff
    -voicevox_core-ios-xcframework-cpu-{バージョン}.zip
    +voicevox_core-xcframework-cpu-{バージョン}.zip
     └── voicevox_core.xcframework
         ├── Info.plist
    +    ├── macos-arm64_x86_64/
         ├── ios-arm64/
         └── ios-arm64_x86_64-simulator/
    ```

    Changedの章で後述する通り、リリースの名前は変わります。

- \[C\] \[macOS\] GitHub Releasesにおいてvoicevox\_core-**ios**-xcframework-cpu-{バージョン}.zipは、macOS版XCFrameworkの提供に伴ってvoicevox\_core-xcframework-cpu-{バージョン}.zipに改名されます ([#1056] helped by [@nekomimimi])。

[#1056]: https://github.com/VOICEVOX/voicevox_core/pull/1056

### ストリーミングAPI

- split decoder into spectrogram and vocoder without changing API ([#851])
- ストリーミングモードのdecodeを実装（precompute_renderとrender） ([#854])
- fix: Python APIとexample/python/run.pyの型付けを直す ([#864])
- fix compat breaking: revive workaround padding in decode() ([#867])
- feat!: `render`の引数の範囲指定部分を各言語の慣習に合わせる ([#879])
- feat!: decode.onnxを復活させる ([#918])
- [#1319]

[#854]: https://github.com/VOICEVOX/voicevox_core/pull/854
[#864]: https://github.com/VOICEVOX/voicevox_core/pull/864
[#867]: https://github.com/VOICEVOX/voicevox_core/pull/867
[#879]: https://github.com/VOICEVOX/voicevox_core/pull/879
[#1319]: https://github.com/VOICEVOX/voicevox_core/pull/1319

### もし`TextAnalyzer`機能を充実させた場合

- TextAnalyzer traitにstring->AccentPhraseModel[]を移動 ([#740] by [@eyr1n])。
- jlabel導入 ([#742] by [@phenylshima], [#750] by [@phenylshima])。
- feat!: Rust APIだけ`TextAnalyzer`をパブリックにする ([#919])

[#742]: https://github.com/VOICEVOX/voicevox_core/pull/742
[#750]: https://github.com/VOICEVOX/voicevox_core/pull/750

[@phenylshima]: https://github.com/phenylshima
-->

### Added

- `Synthesizer::load_voice_model`にオプション`on_existing`が追加されます ([#1331], [#1337])。
- \[ダウンローダー\] `--os`オプションで`android`と`ios`を指定できるようになります。ただしiOSの`c-api`をダウンロードすることはできません ([#1313])。
- \[ダウンローダー\] 環境変数`VV_DOWNLOADER_C_API_ALLOW_DRAFT`を設定することで、`c-api`のdraft releaseを`--c-api-version`で指定できるようになります。主な用途はこのvoicevox\_coreリポジトリでの内部利用です ([#1315])。

### Changed

- \[Rust\] \[BREAKING\] `Synthesizer::load_voice_model`がビルダースタイルになります ([#1331])。
- \[Rust\] \[BREAKING\] MSRVが1.89.0になります ([#1323])。
- \[Rust\] \[BREAKING\] `load-onnxruntime`フィーチャと`link-onnxruntime`フィーチャの両方において、ビルド時のダウンロードおよびリンカーフラグの設定が[pykeio/ort](https://github.com/pykeio/ort)由来の処理に依存しなくなります。それにより、ビルド時の挙動が以下の点で変わります。なお以下で言及する"ONNX Runtime"はVOICEVOX ONNX Runtimeとは異なることに注意してください [#1278]。
    - ビルド時のONNX Runtimeのダウンロードおよび[ターゲットディレクトリ](https://doc.rust-lang.org/cargo/reference/config.html#buildtarget-dir)への配置が、デフォルトでは行われなくなります。新しく追加される`buildtime-download-onnxruntime`フィーチャを有効化**した上で**環境変数`VVCORE_BUILD_DOWNLOAD_AND_COPY_ORT`を`1`にすることで、これまで通りダウンロードおよびターゲットディレクトリへの配置が行われます。
    - ONNX Runtimeのダウンロード先が`{キャッシュディレクトリ}/voicevox_ort/dfbin/`から`{ターゲットディレクトリ}/voicevox_core/downloads/onnxruntime/`になります。
    - Windows上ではシンボリックリンクの作成を試みなくなります。
    - `load-onnxruntime`での不必要なpkg-configやライブラリリンクが行われなくなります。
    - `link-onnxruntime`におけるpkg-configが、Windowsでは`onnxruntime`を対象にするようになります。これまではWindowsであっても`libonnxruntime`を対象にしていました。
    - `link-onnxruntime`が有効化されていて上述のダウンロードが行われない場合、pkg-configを試みて失敗したら警告を出すようになります。Cargoプロジェクト外でONNX Runtimeを管理するのでなければダウンロードを検討してください。
    - 以前まで使えていた、pykeio/ortに作用する環境変数が使えなくなります。
    - 以前までは`voicevox-ort/{cuda,directml}`フィーチャによりEP付きのONNX Runtimeがダウンロードできていましたが、今後はできなくなります。
- \[Rust\] `Onnxruntime`型のメモリアドレスの所在が`voicevox_core`側になります ([#1278])。
- \[Rust\] 依存ライブラリが変化します ([#1278])。
    - \[削除\]: `ndarray@0.16`
    - \[削除\]: `git+https://github.com/VOICEVOX/ort.git?rev=6d69dbd1ddfae713081d844c456be5b8d097e17e#voicevox-ort@2.0.0-rc.10`
    - \[追加\]: `ndarray@0.17`
    - \[追加\]: `git+https://github.com/pykeio/ort.git?rev=94417081c47f47f5a7d6a92ce94bb38fda10019f#ort@2.0.0-rc.12`
    - \[変更\]: `indexmap@2`: `^2.6.0` → `^2.13.0`
- \[C\] \[BREAKING\] `voicevox_synthesizer_load_voice_model`に引数`VoicevoxLoadVoiceModelOptions options`が追加されます ([#1337])。
- \[Java\] \[BREAKING\] `Synthesizer#load_voice_model`がビルダースタイルになります ([#1337])。

### Fixed

- \[Python\] `Synthesizer.load_voice_model`のdocstringにおける"Parameters"の説明が誤っていたのが修正されます ([#1359])。

### Security

- \[Rust,ダウンローダー\] 以下の脆弱性登録の影響を受けないようになります ([#1349], [#1350])。
    - [RUSTSEC-2026-0044](https://rustsec.org/advisories/RUSTSEC-2026-0044)
    - [RUSTSEC-2026-0045](https://rustsec.org/advisories/RUSTSEC-2026-0045)
    - [RUSTSEC-2026-0046](https://rustsec.org/advisories/RUSTSEC-2026-0046)
    - [RUSTSEC-2026-0047](https://rustsec.org/advisories/RUSTSEC-2026-0047)
    - [RUSTSEC-2026-0048](https://rustsec.org/advisories/RUSTSEC-2026-0048)
    - [RUSTSEC-2026-0049](https://rustsec.org/advisories/RUSTSEC-2026-0049)
    - [RUSTSEC-2026-0067](https://rustsec.org/advisories/RUSTSEC-2026-0067)
    - [RUSTSEC-2026-0068](https://rustsec.org/advisories/RUSTSEC-2026-0068)
    - [RUSTSEC-2026-0098](https://rustsec.org/advisories/RUSTSEC-2026-0098)
    - [RUSTSEC-2026-0099](https://rustsec.org/advisories/RUSTSEC-2026-0099)
    - [RUSTSEC-2026-0104](https://rustsec.org/advisories/RUSTSEC-2026-0104)

## [0.16.4] - 2026-02-19 (+09:00)

主な変更点とその解説については、[GitHub Releaseの本文](https://github.com/VOICEVOX/voicevox_core/releases/tag/0.16.4)をご覧ください。

### Added

- ソング機能が追加されます ([#531], [#732], [#738], [#761], [#895], [#896], [#894], [#1217], [#1236], [#1073], [#1242], [#1250], [#1252], [#1247], [#1253], [#1244], [#1257], [#1255], [#1260], [#1245], [#1246], [#1280], [#1285], [#1279], [#1304])。
- ドキュメントが改善されます。
    - [バージョン0.16.3](#0163---2025-12-08-0900)で導入された、`AudioQuery`/`AccentPhrase`/`Mora`のバリデーション機能に関するドキュメンテーションがよりわかりやすくなります ([#1251])。
    - \[Python,Java\] 一部のドキュメントの文体が改善されます ([#1238])。
- \[Android\] リリースされるバイナリが[16KBデバイスへの互換性を持つようになり、Google Playが要求する要件を満たす](https://developer.android.com/guide/practices/page-sizes)ようになります。ただし(VOICEVOX) ONNX Runtimeのバージョン1.17は16KBへの互換性が無いことに注意してください。(VOICEVOX) ONNX Runtime バージョン1.22以降であれば対応しています ([#1283])。
- \[ダウンローダー\] HTTPクライアントのものを含めた、いくつかの依存ライブラリがアップデートされます ([#1265])。

### Changed

- ONNX Runtimeが出す`FATAL`レベルのログの表示形式が少しだけ変わります。また`VERBOSE`レベルのログは[`tracing::Level::TRACE`](https://docs.rs/tracing/0.1/tracing/struct.Level.html#associatedconstant.TRACE)に格下げされます ([#1276])。
- \[Python,Java\] `AudioQuery`（もしくはその一部）がRustのオブジェクトとして表現できなかったときのエラーが、`InvalidQuery`エラーに包まれるようになります。これまでは`OverflowError`や`RuntimeError`がそのままraiseされていました ([#1237])。
- \[Rust\] 依存ライブラリが変化します ([#1073], [#1250], [#1265], [#1277], [#1276], [#1291])。
    - \[追加\] `arrayvec@0.7`: `^0.7.6`
    - \[追加\] `derive_more@1`: `into_iterator`フィーチャを追加
    - \[追加\] `num-traits@0.2`: `^0.2.15`
    - \[追加\] `smol_str@0.3`: `^0.3.2`
    - \[追加\] `typed_floats@1`: `^1.0.7`
    - \[追加\] `typeshare@1`: `^1.0.4` (`default-features = false`)
    - \[変更\] `bytes@1`: `^1.7.2` → `^1.11.1`
    - \[変更\] `regex@1`: `^1.11.0` → `^1.12.0`
    - \[変更\] `serde@1`: `^1.0.27` → `^1.0.228`
    - \[変更\] `voicevox-ort@2.0.0-rc.10`: `22172d0fcf0715c1316f95ea08db50cf55cf0ad4` → `6d69dbd1ddfae713081d844c456be5b8d097e17e`
- \[Python\] 型ヒントが[`uuid.UUID`](https://docs.python.org/3/library/uuid.html#uuid.UUID)である引数に、`uuid.UUID`ではないオブジェクトを与えたときのエラーが`TypeError`になります ([#1266])。

### Fixed

- \[Python\] `Onnxruntime.load_once`は[デッドロックする可能性](https://pyo3.rs/v0.13.0/faq#im-experiencing-deadlocks-using-pyo3-with-lazy_static-or-once_cell)がありましたが、解消されます ([#1266])。
- \[Java\] 各`validate`メソッドのJavadocにおいて、浮動小数点数がNaNあるいは±infinityだったときの扱いの記述が実態に則したものへと訂正されます ([#1237])。

### Security

- \[C,Java,ダウンローダー\] 現実的な攻撃シナリオは無かったと考えられますが、以下の脆弱性の影響を受けないようになります ([#1265], [#1269], [#1291], [#1295])。
    - [RUSTSEC-2025-0023](https://rustsec.org/advisories/RUSTSEC-2025-0023)
    - [RUSTSEC-2025-0024](https://rustsec.org/advisories/RUSTSEC-2025-0024)
    - [RUSTSEC-2025-0055](https://rustsec.org/advisories/RUSTSEC-2025-0055)
    - [RUSTSEC-2026-0007](https://rustsec.org/advisories/RUSTSEC-2026-0007)
    - [RUSTSEC-2026-0009](https://rustsec.org/advisories/RUSTSEC-2026-0009)

## [0.16.3] - 2025-12-08 (+09:00)

主な変更点とその解説については、[GitHub Releaseの本文](https://github.com/VOICEVOX/voicevox_core/releases/tag/0.16.3)をご覧ください。

### Added

- `sil`に対する扱いが、現行のバージョン0.25.0のVOICEVOX ENGINEと同じになります ([#1197])。
- \[Rust,C,Java\] シリアライズ関係のAPIドキュメントがより詳細になります ([#1223])。

### Changed

- `AudioQuery`/`AccentPhrase`/`Mora`において不正な状態というものが定義され、不正な`AudioQuery`もしくは`accent_phrases`が明示的にエラーを引き起こすようになります ([#1203], [#1208], [#1222], [#1221], [#1224])。
    - \[Rust,Python,Java\] エラーの種類として`InvalidQuery`が追加されます。
    - \[C\] エラーの種類として`VOICEVOX_RESULT_INVALID_MORA_ERROR`が追加されます。
    - メソッドとして`{AudioQuery,AccentPhrase,Mora}::validate`が追加されます。
- \[Rust\] 依存ライブラリが変化します ([#1190], [#1214], [#1221])。
    - \[追加\] `bytemuck@1`: `^1.24.0`
    - \[追加\] `pastey@0.2`: `^0.2.0`
    - \[追加\] `phf@0.13`: `^0.13.1`

### Removed

- 以下の音素は完全に不正なものとして扱われます ([#1203], [#1221])。
    - `""`
    - `consonant`における母音
    - `vowel`における子音
- \[macOS\] macOS 13がサポート範囲から外れます。"arm64"バイナリのリリースはmacOS 14で、"x64"バイナリのリリースはmacOS 15で行われるようになります ([#1174], [#1227])。

### Fixed

- \[ダウンローダー\] `dict`のダウンロード元であるjaist.dl.sourceforge.netが[消失した](https://x.com/zinchang/status/1996112944372044235)ため、代わりに[r9y9/open\_jtalk@`v1.11.1`のリリース](https://github.com/r9y9/open_jtalk/releases/tag/v1.11.1)を利用するようになります ([#1220])。
- \[Java\] Javadocにおいて`UserDictWord`がGSONに対応しているという誤った情報が訂正されます ([#1223])。

## [0.16.2] - 2025-10-28 (+09:00)

主な変更点とその解説については、[GitHub Releaseの本文](https://github.com/VOICEVOX/voicevox_core/releases/tag/0.16.2)をご覧ください。

### Added

- \[C\] APIドキュメントに使っているDoxygenがv1.9.8-r0からv1.12.0-r0になります ([#1155])。

### Changed

- \[Rust\] `voicevox_core_macros`は内部クレートであり、SemVerに従わないということが明記されます。`substitute_type!`と`pyproject_project_version!`に関してはバージョン0.16の間は保持しますが、バージョン0.17以降の保証はしません ([#1149])。
- \[Rust\] 依存ライブラリが変化します ([#1153], [#1147])。
    - \[削除\] `ndarray@0.15`
    - \[削除\] `ndarray-stats@0.5`
    - \[削除\] `git+https://github.com/VOICEVOX/ort.git?rev=12101456be9975b7d263478c7c53554017b7927c#voicevox-ort@2.0.0-rc.4`
    - \[追加\] `ndarray@0.16`: `^0.16.1`
    - \[追加\] `ndarray-stats@0.6`: `^0.6.0`
    - \[追加\] `git+https://github.com/VOICEVOX/ort.git?rev=22172d0fcf0715c1316f95ea08db50cf55cf0ad4#voicevox-ort@2.0.0-rc.10`
    - \[変更\] `anyhow@1`: `^1.0.89` → `^1.0.99`
    - \[変更\] `serde@1`: `^1.0.210` → `^1.0.219`
    - \[変更\] `serde_json@1`: `^1.0.128` → `^1.0.143`
    - \[変更\] `uuid@1`: `^1.10.0` → `^1.18.1`

### Fixed

- \[Windows,Linux\] バージョン[0.16.0-preview.0](#0160-preview0---2025-03-01-0900)にて意図せず著しく低下していた、CUDAでの音声合成のパフォーマンスが元に戻ります ([#1164] by [@Sanzentyo])。

## [0.16.1] - 2025-08-14 (+09:00)

主な変更点とその解説については、[GitHub Releaseの本文](https://github.com/VOICEVOX/voicevox_core/releases/tag/0.16.1)をご覧ください。

### Added

- \[Rust,Python,Java\] 基本的なインターフェイスや特殊メソッドが、以下のように実装されます ([#1100])。
    - Rust API
        - `Debug` for
            - `AudioQuery`
            - `UserDictWordBuilder`
            - `{blocking,nonblocking}.onnxruntime.LoadOnce`
            - `{blocking,nonblocking}.VoiceModelFile`
            - `{blocking,nonblocking}.OpenJtalk`
            - `{blocking,nonblocking}.Synthesizer`
            - `{blocking,nonblocking}.synthesizer.*`
        - `PartialEq` for
            - `StyleMeta`
            - `AudioQuery`
            - `UserDictWord`
        - `{PartialOrd,Ord}` for
            - `AccelerationMode`
            - `UserDictWordType`
        - `Hash` for
            - `CharacterVersion`
            - `AccelerationMode`
        - `{AsRef,AsMut}` for `CharacterVersion`
        - `{UpperHex,LowerHex,Octal,Binary}` for `StyleId`
        - `Into<u32>` for `StyleId` (via `From`)
    - Python API
        - `__repr__` for
            - `{blocking,asyncio}.VoiceModelFile`
            - `{blocking,asyncio}.Onnxruntime`
            - `{blocking,asyncio}.VoiceModelFile`
            - `{blocking,asyncio}.OpenJtalk`
            - `{blocking,asyncio}.UserDict`
    - Java API
        - `Object.equals` for
            - `SupportedDevices`
            - `StyleMeta`
            - `CharacterMeta`
            - `Mora`
            - `AccentPhrase`
            - `AudioQuery`
        - `Cloneable` for
            - `SupportedDevices`
            - `StyleMeta`
            - `CharacterMeta`
            - `Mora`
- `VoiceModelId`が指すIDが何に対して固有なのかが暫定的に定められ、ドキュメンテーションコメントに書かれます ([#1143])。
- バージョン0.14.0からの歴史をまとめた[Keep a Changelog](https://keepachangelog.com)形式のCHANGELOG.mdが追加されます ([#1109], [#1116], [#1117], [#1124], [#1125], [#1126], [#1128], [#1131], [#1132], [#1123], [#1133], [#1134], [#1137], [#1136], [#1138], [#1139], [#1140], [#1118], [#1143], [#1144])。
- \[Rust\] Rust Analyzerが、C APIから参照する目的で[0.16.0-preview.0](#0160-preview0---2025-03-01-0900)の[#976]にて導入した`doc(alias)`に反応しないようになります ([#1099])。
- \[C\] `free`系と`delete`系の関数が、`free(3)`や`HeapFree`のようにヌルポインタを許容するようになります ([#1094])。
- \[Python\] exampleコードにはshebangが付き、filemodeも`100755` (`GIT_FILEMODE_BLOB_EXECUTABLE`)になります ([#1077])。
- \[Java\] \[Windows,macOS,Linux\] :tada: GitHub Releasesのjava\_packages.zipに、PC用のパッケージが追加されます ([#682], [#764])。
- \[ダウンローダー\] :tada: `models`のダウンロード元が[VOICEVOX/voicevox\_vvm]の`>=0.16,<0.17`になります。[VOICEVOX/voicevox\_vvmのバージョン0.16.0](https://github.com/VOICEVOX/voicevox_vvm/releases/tag/0.16.0)には以下の変更が含まれます。今後`>=0.16.1,<0.17`のvoicevox\_vvmをリリースする際は、voicevox\_coreリポジトリでは案内をしません ([VOICEVOX/voicevox\_vvm#21], [VOICEVOX/voicevox\_vvm#22], [VOICEVOX/voicevox\_vvm#23], [VOICEVOX/voicevox\_vvm#30], [VOICEVOX/voicevox\_vvm#31], [VOICEVOX/voicevox\_vvm#33], [VOICEVOX/voicevox\_vvm#34], [#1118])。
    - [10期生](https://voicevox.hiroshiba.jp/dormitory/#10th)および同時期に作られていた追加スタイルに対応するVVM(19.vvm、20.vvm、21.vvm)を追加
    - ソング用VVM(s0.vvm)を追加
    - [`Character::version`を`0.1.0`から`0.16.0`に変更](https://github.com/VOICEVOX/voicevox_vvm/pull/34)
- \[ダウンローダー\] :tada: リトライ機構が導入され、デフォルトで4回のリトライを行うようになります。この回数は`-t, --tries <NUMBER>`で変更可能です。現段階では以下に示す挙動をします。これらの挙動は将来的に変更される予定であり、議論は[#1127]で行われています。 ([#1098] by [@shuntia], [#1111], [#1121], [#1139], [#1140])。
    - 各試行は`<TARGET>`単位で行われる。ダウンロードしたzipやtgzの解凍に失敗してもリトライが行われる。また`models`の場合、どれか一つのVVMのダウンロードに失敗すると、他のVVMも全部まとめてリトライが行われる。
    - プログレスバーを出す前の段階でエラーが発生した場合、リトライは行われない。
- \[ダウンローダー\] `--models-version <SEMVER>`オプションが追加されます。ダウンローダーから見て未来のバージョンを使うことも可能になりますが、警告は出ます ([#1134], [#1137], [#1138], [#1136], [#1118])。
- \[ダウンローダー\] `--models-pattern <GLOB>`オプションが追加され、ダウンロードするVVMファイルを限定できるようになります ([#1093], [#1117])。
- \[ダウンローダー\] ダウンローダーは正式にVOICEVOX COREの一部と定められ、バージョンを共にするようになります。それに伴い、`-V, --version`でVOICEVOX CORE兼ダウンローダーのバージョンを見ることができるようになります ([#1116])。
- \[ダウンローダー\] 環境変数`GITHUB_TOKEN`でGitHubの認証トークンをセットする機能がドキュメント化されます ([#1128])。
- \[ダウンローダー\] 環境変数`GITHUB_TOKEN`に加え、`GH_TOKEN`でもGitHubの認証トークンをセットすることができるようになります ([#1131])。
- \[ダウンローダー\] helpの文章が充実します ([#1117], [#1125], [#1126], [#1128])。
    - リポジトリ指定オプション (`--{target}-repo <REPOSITORY>`)には何も書かれていませんでしたが、書かれます。
    - `-h`ではなく`--help`のみ、オプションの説明の下にいくつかの章が追加されます。内容は[downloader.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.1/docs/guide/user/downloader.md)に書かれているものとほぼ同じです。
- \[ダウンローダー\] 不要である[Oniguruma](https://github.com/kkos/oniguruma)のリンクをやめます ([#1082])。

### Changed

- \[ダウンローダー\] `models`において、GitHub Releaseが無いGitタグは利用できなくなります。また上記の`--models-version <SEMVER>`を指定しない限り、pre-releaseのものは使われなくなります ([#1136], [#1118])。
- \[ダウンローダー\] `-h`と`--help`は別々の表示をするようになります ([#1125])。

### Removed

- \[Windows\] `windows-2019`がサポートから外れ、リリースは`windows-2022`で行われることになります。ただし、`windows-2022`でビルドしたバイナリであっても`windows-2019`相当の環境で動作すると考えられています。またVOICEVOX ONNX Runtimeが既に元々`windows-2022`でビルドされているため、通常の用途においては特に変わらないはずです ([#1096])。
- \[Rust\] 依存ライブラリのバージョン要求が変わります ([#1070], [#1078])。
    - `proc-macro2@1`: `^1.0.86` → `^1.0.95`
    - `syn@2`: `^2.0.79` → `^1.0.86`

### Fixed

- \[Python\] リポジトリにあるMarkdownドキュメントにおいて、wheelファイル名が古いままだった問題が修正されます ([#1063])。
- \[Java\] \[Android\] GHAのUbuntuイメージ備え付けの`$ANDROID_NDK` (現時点ではバージョン27)を使ったリリースがされるようになります。これにより、[#1103]で報告されたAndroidビルドにおけるC++シンボルの問題が解決されます ([#1108])。
- \[Java\] Javaのファイナライザから中身のRustオブジェクトのデストラクトがされない問題が解決されます ([#1085])。
- \[ダウンローダー\] 将来的に[VOICEVOX/voicevox\_vvm]のタグの数が30を超えたときに、もしかしたら起きうるかもしれない問題の対処がされます ([#1123])。
- \[ダウンローダー\] いくつかのエラーの出かたが改善されます ([#1132], [#1133], [#1136])。
- \[ダウンローダー\] `--devices <DEVICES>...`のhelpには「(cudaはlinuxのみ)」と書かれていましたが、この記述は[0.16.0-preview.0](#0160-preview0---2025-03-01-0900)の時点で正しくなくなっていたため消されます ([#1124])。
- \[ダウンローダー\] \[Windows\] GitHub Releasesにおいて、再び署名がされるようになります ([#1060])。

## [0.16.0] - 2025-03-29 (+09:00)

### Added

- 次の二つのドキュメントが追加され、APIドキュメント側からも言及されるようになります ([#1049])。
    - [docs/guide/user/languages.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0/docs/guide/user/languages.md)
    - [docs/guide/user/serialization.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0/docs/guide/user/serialization.md)
- [READMEの「事例紹介」](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0/README.md#事例紹介)に、Swiftの事例として[VoicevoxCoreSwift](https://github.com/yamachu/VoicevoxCoreSwift)が追加されます ([#1055])。
- \[Rust\] APIドキュメントのトップにコード例が入ります ([#1016], [#1045])。
- \[ダウンローダー\] `models`のダウンロード元である[VOICEVOX/voicevox\_vvm]のバージョン範囲が、`>=0.1,<0.2`になります ([#1057])。

### Changed

- \[Rust\] \[BREAKING\] `UserDictWord`のコンストラクト方法がビルダースタイルになります ([#999])。
- \[Java\] \[BREAKING\] `jp.hiroshiba.voicevoxcore.GlobalInfo.SupportedDevices`は`voicevoxcore`直下に移動します ([#991])。
- \[Java\] \[BREAKING\] `UserDict`が扱う"UUID"はすべて、`String`ではなく`java.util.UUID`になります ([#1058])。
- \[Java\] \[Windows,macOS,Linux\] build.gradleに`javadoc.options.encoding = 'UTF-8'`が足されます ([#995])。

### Removed

- \[Java\] docs: APIドキュメントのポータルにおいて、Java APIのJavadocへの案内が一時的に消えます ([#1044])。

### Fixed

- \[Python\] `voicevox_core.{blocking,asyncio}`のクラスのインスタンスに対して、同時にアクセスしたときに`RuntimeError`が出る場合があった問題が解決されます ([#1041])。

- \[Python\] [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)で追加された`__new__`の型定義が修正されます ([#1048])。

- \[Java\] \[Windows\] 同じ環境で二度起動しようとすると失敗する問題が修正されます ([#1043])。

     VOICEVOX CORE Java APIは、voicevox\_core\_java\_api.dllを`%TEMP%`直下に展開してそれをロードすることにより動いています。その動的ライブラリは[`File#deleteOnExit`](https://docs.oracle.com/javase/8/docs/api/java/io/File.html#deleteOnExit--)によってJVMの終了時に削除されるはずでしたが、Windowsの場合上手く消えないことがわかりました。そのためDLL展開時に、以前のものを[`REPLACE_EXISTING`](https://docs.oracle.com/javase/8/docs/api/java/nio/file/StandardCopyOption.html#REPLACE_EXISTING)で上書きすることで問題を解決します。

     voicevox\_core\_java\_api.dllは依然として`%TEMP%`下に残り続ける上に、VOICEVOX CORE Java APIの多重起動ができないことには変わらないことに注意する必要はあります。

- \[Java\] \[Windows,macOS,Linux\] 壊れていたKotlin exampleが直ります ([#994])。

- \[C\] \[Windows\] C++ exampleのREADME.mdの誤記が修正されます ([#1040] by [@nanae772])。

## [0.16.0-preview.1] - 2025-03-08 (+09:00)

### Added

- [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)にて追加された時点では書きかけの状態だった、[docs/guide/user/usage.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0-preview.1/docs/guide/user/usage.md)が書き上がります ([#1032])。
- readmeから「バージョン 0.15.4をご利用ください」の注意書きが削除されます ([#1035])。

### Changed

- \[Python\] \[BREAKING\] Pydanticが依存から外れ、`@pydantic.dataclasses.dataclass`のクラスはすべて素のdataclassになります。dataclassのシリアライズについては代替手段は用意されず、非推奨になります ([#1034])。
- \[ダウンローダー\] `models`のダウンロード元が[VOICEVOX/voicevox\_vvm]の`0.0.1-preview.5` (= 今の[`0.1.0`](https://github.com/VOICEVOX/voicevox_vvm/releases/tag/0.1.0))になり、readmeおよび利用規約の文面が更新されます ([VOICEVOX/voicevox\_vvm#12], [VOICEVOX/voicevox\_vvm#14], [#1015])。

### Removed

- [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)では製品版VVMがこのリポジトリのGitHub Releasesに置かれなくなり、代わりにsample.vvmが置かれていましたが、今回からそれも無くなります ([#1033])。
- \[Linux\] \[BREAKING\] Ubuntu 20.04がサポート対象から外れ、バイナリのリリースはUbuntu 22.04で行われるようになります。それに伴い、glibcの最小バージョンが2.31から2.34に上がります ([#1028])。

### Fixed

- \[ダウンローダー\] エラーメッセージの文面が修正されます ([#1030] by [@nanae772])。

## [0.16.0-preview.0] - 2025-03-01 (+09:00)

### Added

- :tada: Rust APIが利用できるようになります ([#425], [#443], [#479], [#486], [#487], [#508], [#370], [#501], [#502], [#515], [#538], [#532] helped by [@wappon28dev], [#551], [#573], [#580], [#589], [#577], [#622], [#623], [#624], [#646], [#656], [#669], [#675], [#667], [#692], [#694], [#702], [#745], [#740] by [@eyr1n], [#708], [#803], [#759], [#807], [#810], [#805], [#831], [#834], [#835], [#844], [#846], [#847], [#868], [#882], [#886], [#907], [#910], [#912], [#825], [#911], [#919], [#932], [#931], [#940], [#941], [#937], [#949], [#958], [#974], [#982], [#990], [#992], [#996], [#1002], [#1025])。

    ```console
    ❯ cargo add voicevox_core --git https://github.com/VOICEVOX/voicevox_core.git --tag 0.16.0-preview.0 --features load-onnxruntime
    ```

    [mainブランチのAPIドキュメント](https://voicevox.github.io/voicevox_core/apis/rust_api/voicevox_core/)

- 次のAPIが追加されます ([#1025])。

    - `AudioQuery::from_accent_phrases` (C API: `voicevox_audio_query_create_from_accent_phrases`)
    - `OpenJtalk::analyze` (C API: `voicevox_open_jtalk_rc_analyze`)

    これらがどのような位置付けなのかは、後述する[tts-process.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0-preview.0/docs/guide/user/tts-process.md)にて図示されます。

- `SpeakerMeta`改め`CharacterMeta`（後述）、および`StyleMeta`に、オプショナルな整数型フィールド`order`が追加されます ([#728])。

- `StyleMeta`に`type`というフィールドが追加されます ([#531], [#738], [#761], [#895], [#996])。

    取り得る値は`"talk" | "singing_teacher" | "frame_decode" | "sing"`です。ソング機能自体は今後[#1073]で行われる予定です。

    また、エラーの種類として`InvalidModelFormat` (C API: <code>VOICEVOX\_RESULT\_INVALID\_MODEL\_**HEADER**\_ERROR</code>)が追加されます。

- リポジトリ上のMarkdownドキュメントが色々改善されます ([#699], [#707], [#824] by [@cm-ayf], [#838], [#863], [#945], [#1021], [#1023], [#1025])。

    - docs/ディレクトリが再編されます。

        ```
        docs
        ├── ghpages/apis/ : GitHub Pages用
        └── guide
            ├── dev/      : コードに潜る人用のMarkdownドキュメント
            └── user/     : ユーザー用のMarkdownドキュメント
        ```

    - [docs/guide/user/usage.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0-preview.0/docs/guide/user/usage.md)が追加されます。

    - [docs/guide/user/tts-process.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0-preview.0/docs/guide/user/tts-process.md)が追加され、各APIドキュメントからも参照されるようになります。

    - [docs/guide/user/downloader.md](https://github.com/VOICEVOX/voicevox_core/blob/0.16.0-preview.0/docs/guide/user/downloader.md) (旧docs/downloader.md)に、`<TARGET>`についての説明が足されます。

    - ユーザー用ではない、貢献者用のドキュメントがCONTRIBUTING.mdとcrates/voicevox\_core\_c\_api/README.mdに隔離されます。

    - 他色々

- [APIドキュメントのポータル](https://voicevox.github.io/voicevox_core/apis/)に以下の改善が入りました ([#837], [#838])。

    - C APIとPython APIについてのリンク先が変わりました。
    - TODOとして[#496]へのリンクが置かれ、いつかまともなページを用意する意思が表明されました (補足: その後、[#1001]という話が進行中です)。

- \[C\] :tada: `VoicevoxSynthesizer`などのオブジェクトに対する`…_delete`が、どのタイミングで行っても安全になります ([#849], [#862])。

    - "delete"時に対象オブジェクトに対するアクセスがあった場合、アクセスが終わるまで待つようになります。
    - 次の操作が未定義動作ではなくなります。ただし未定義動作ではないだけで明示的にクラッシュするため、起きないように依然として注意する必要があります。
        - "delete"後に他の通常のメソッド関数の利用を試みる
        - "delete"後に"delete"を試みる
        - そもそもオブジェクトとして変なダングリングポインタが渡される
        - ヌルポインタが渡される (補足: [0.16.1](#0161---2025-08-14-0900)にて許容されるようになります)

- \[C\] ドキュメンテーションコメントに以下の改善が入ります ([#976], [#992])。

    - GitHub PagesのRust APIドキュメントへのリンクが張られるようになります。
    - コードブロックがclang-formatで整形されます。

- \[C\] リリース内容物にLICENSEファイルが追加されます ([#965])。

- \[Python\] :tada: 推論を行うAPIにオプション引数`cancellable`が追加されます ([#889], [#1024], [#903], [#992])。

    `True`にすると[タスクとしてキャンセル](https://docs.python.org/3.11/library/asyncio-task.html#task-cancellation)できるようになります。

    デフォルトでキャンセル可能ではない理由は、ドキュメントにも書いてありますがキャンセル可能にすると（キャンセルを行わない場合でも）[ハングする危険性がある](https://github.com/VOICEVOX/voicevox_core/issues/968)からです。ご注意ください。

- \[Python\] :tada: ブロッキングAPIを提供する`voicevox_core.blocking`モジュールが追加されます ([#702], [#706], [#992])。

    ```py
    from voicevox_core.blocking import Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile

    # …
    wav = synthesizer.tts("こんにちは", 0)
    ```

- \[Python\] `VoiceModel.from_path`改め`VoiceModelFile.open`が引数に取るファイルパスが、UTF-8でなくてもよくなります ([#752])。

- \[Python\] 引数の型が`Path | str`となっていた箇所は、一般的な慣習に合わせる形で`str | PathLike[str]`になります ([#753])。

- \[Python\] wheelは`Metadata-Version: 2.4`になり、またライセンス情報とreadmeが含まれるようになります ([#947], [#949], [#959])。

- \[Python\] Pyright/Pylanceをサポートするようになります ([#719])。

- \[Python\] GitHub Pagesに使うSphinxがアップデートされました。また、`NewType`型へのリンクが張られるようになりました ([#952], [#953])。

- \[Python,Java\] `Synthesizer`から`OpenJtalk`を得ることができるゲッターが追加されます ([#1025])。

- \[Python,Java\] `UserDict`の`load`と`store`が引数に取ることができるファイルパスの表現が広くなります ([#835])。

    Python APIでは`StrPath`相当になり、Java APIでは`java.io.File`と`java.nio.file.Path`のオーバーロードが追加されます。

- \[Python,Java\] exampleコードとそのドキュメントに細かい改善が入ります ([#881], [#986], [#992])。

- \[ダウンローダー\] 対象外の`<TARGET>`を見に行かないようになります ([#939])。

    これまではダウンロード対象外であっても、不必要にリポジトリを見にいくようになってました。

### Changed

- \[BREAKING\] :tada: VOICEVOX COREは完全にMIT Licenseになり、代わりにプロプライエタリ部分はONNX Runtime側に移ります ([#913], [VOICEVOX/voicevox\_vvm#1], [#825], [VOICEVOX/voicevox\_vvm#5], [VOICEVOX/voicevox\_vvm#9], [#965], [#973], [#979], [#1019])。

    御自身で手を加えたVOICEVOX COREをそのまま実行できるようになります。

    製品版VVMを読み込む際は、ONNX Runtimeの代わりに**VOICEVOX** ONNX Runtimeというライブラリが必要になります。VOICEVOX ONNX Runtimeは、ダウンローダーにて`onnxruntime`としてダウンロードできます。

    ```console
    ❯ ./download --only onnxruntime --onnxruntime-version voicevox_onnxruntime-1.17.3
    ```

- \[BREAKING\] :tada: (VOICEVOX) ONNX Runtimeを動的リンクすることは基本的になくなり、代わりに`dlopen`/`LoadLibraryExW`でロードするようになります。ロードは`Onnxruntime`型から行う形になります ([#725], [#802], [#806], [#810], [#822], [#860], [#876], [#898], [#921], [#911], [#933], [#992], [#1003], [#1019])。

    これにより、VOICEVOX COREの起動後に(VOICEVOX) ONNX Runtimeを探して読み込むことができるようになりました。ただし、iOS版のリリースにおいてのみ従来の動的リンクの形を継続します。

    またこれに伴い:

    - エラーの種類として`InitInferenceRuntime`が追加されます。
    - C APIでは、LinuxとmacOS用のrpath設定が削除されます。
    - Python APIはmanylinuxに対応するようになり、wheel名の"linux"は"manylinux_{glibcのバージョン}"になります。また、カレントディレクトリ下の動的ライブラリを自動で読み込む機能は無くなります。

    補足: この変更によりCUDAでの音声合成のパフォーマンスが意図せず著しく低下しましたが、バージョン[0.16.2](#0162---2025-10-28-0900)にて修正されます。

- \[BREAKING\] VOICEVOX CORE自体からはCPU版/GPU版という区分は無くなり、GPU違いのリリースについては完全に(VOICEVOX) ONNX Runtimeに委ねる形になります ([#802], [#810])。

- \[BREAKING\] このリポジトリのGitHub ReleasesはONNX Runtimeを含まなくなります。Java APIの依存からはcom.microsoft.onnxruntime/onnxruntime{,\_gpu}が消えます。 ([#810])。

    ダウンローダーは[VOICEVOX/onnxruntime-builder](https://github.com/VOICEVOX/onnxruntime-builder)から直接(VOICEVOX) ONNX Runtimeをダウンロードするようになります。

- \[BREAKING\] VVMの形式が変わり、[0.15.0-preview.16](#0150-preview16---2023-12-01-0900)までのVVMは利用できなくなります ([#795], [#796], [#794], [#851], [#918], [VOICEVOX/voicevox\_vvm#1], [#825], [VOICEVOX/voicevox\_vvm#5], [VOICEVOX/voicevox\_vvm#9])。

    このバージョンのVOICEVOX COREで利用できるVVMの形式が、`vvm_format_version=1`として定められます。

- \[BREAKING\] 製品版VVMは、このリポジトリのGitHub Releasesには置かれなくなります ([#928], [#964], [#1020] by [@nanae772])。

    [VOICEVOX/voicevox\_vvm]に置かれるようになり、ダウンローダーはそこからダウンロードします。なお、VOICEVOX/voicevox\_fat\_resourceは[リポジトリごと削除されました](https://github.com/VOICEVOX/voicevox_core/issues/1061#issuecomment-2766705584)。

    補足: このバージョンでは製品版VVMの代わりにsample.vvmのみがアップロードされていますが、[0.16.0-preview.1](#0160-preview1---2025-03-08-0900)にてそれも無くなります。

- \[BREAKING\] `VoiceModel`は`VoiceModelFile`になり、ファイルディスクリプタを保持する形になります。コンストラクタの名前は"from\_path"から"open"になり、Python APIとJava APIではクローズ可能になります ([#832], [#868], [#937], [#993])。

    クローズ (`__{,a}{enter,exit}__`/`java.io.Closeable`)の挙動については、詳しくはAPIドキュメントをご覧ください。

- \[BREAKING\] `AudioQuery`および`UserDictWord`のJSON表現はVOICEVOX ENGINEと同じになります ([#946], [#1014])。

    これにより、VOICEVOX ENGINEとVOICEVOX COREとで同じ`AudioQuery`と`UserDictWord`が使い回せるようになります。Python APIおよびJava APIにおける、クラスの形には影響しません。

    - <details><summary><code>AudioQuery</code>の例</summary>

      ```json
      {
        "accent_phrases": […],
        "speedScale": 1.0,
        "pitchScale": 0.0,
        "intonationScale": 1.0,
        "volumeScale": 1.0,
        "prePhonemeLength": 0.1,
        "postPhonemeLength": 0.1,
        "outputSamplingRate": 24000,
        "outputStereo": false
      }
      ```
      </details>
    - <details><summary><code>UserDictWord</code>の例</summary>

      ```json
      {
        "surface": "手札",
        "priority": 6,
        "context_id": 1345,
        "part_of_speech": "名詞",
        "part_of_speech_detail_1": "一般",
        "part_of_speech_detail_2": "*",
        "part_of_speech_detail_3": "*",
        "inflectional_type": "*",
        "inflectional_form": "*",
        "stem": "*",
        "yomi": "テフダ",
        "pronunciation": "テフダ",
        "accent_type": 0,
        "mora_count": 3,
        "accent_associative_rule": "*"
      }
      ```
      </details>

- \[BREAKING\] `VoiceModelId`は、VVMに固有のUUIDになります ([#796])。

    補足: この「固有」の意味については[0.16.1](#0161---2025-08-14-0900)で補足されます。

- \[BREAKING\] 一部のエラーの名前が変わります ([#823], [#919])。

    - `InferenceFailed` → `RunModel`
    - `ExtractFullContextLabel` → `AnalyzeText`

- \[BREAKING\] `UserDictWord`の`accent_type`はオプショナルではなくなります ([#1002])。

    VOICEVOX ENGINEに合わせる形です。

- \[BREAKING\] `acceleration_mode`を`GPU`または`AUTO`（デフォルト）にしたときの挙動が変わります ([#810])。

    `Synthesizer`のコンストラクトの時点でGPUの簡易的なチェックを行うことで、適切なGPUの種類が選択されるようになります。チェックがすべて失敗した場合、`GPU`であればエラー、`AUTO`であればCPUにフォールバックとなります。

- `Synthesizer::unload_voice_model`と`UserDict::remove_word`における削除後の要素の順序が変わります ([#846])。

    例えば`[a, b, c, d, e]`のようなキーの並びから`b`を削除したときに、順序を保って`[a, c, d, e]`になります。以前までは`[a, e, c, d]`になってました。

- ドキュメンテーションコメント上の「話者」という表現は「キャラクター」になります ([#943], [#996])。

- \[C\] \[BREAKING\] 次の`VoicevoxVoiceModelFile` (旧`VoicevoxVoiceModel`)のゲッターに位置付けられる関数が、ゲッターではなくなります ([#850])。

    - `voicevox_voice_model_id`改め`voicevox_voice_model_file_id`

        `uint8_t (*output_voice_model_id)[16]`に吐き出すように。

    - `voicevox_voice_model_get_metas_json`

        `voicevox_voice_model_file_create_metas_json`になり、`VoicevoxVoiceModelFile`が保有しない形でアロケートされた文字列を作成するように。

- \[C\] \[BREAKING\] `UserDictWord`の`priority`のデフォルトが`0`から`5`に変わります ([#1002])。

    Python API、Java API、VOICEVOX ENGINEに合わせる形です。

- \[C\] \[BREAKING\] リリース内容物において、動的ライブラリはlib/に、ヘッダはinclude/に入るようになります ([#954], [#967], [#980])。

    ```
    ├── include
    │   └── voicevox_core.h
    ├── lib
    │   ├── voicevox_core.dll
    │   └── voicevox_core.lib
    ├── LICENSE
    ├── README.txt
    └── VERSION
    ```

- \[Python\] \[BREAKING\] ブロックングAPIの実装に伴い、`Synthesizer`, `OpenJtalk`, `VoiceModel`, `UserDict`は`voicevox_core.asyncio`モジュール下に移動します ([#706])。

- \[Python\] \[BREAKING\] `Enum`だったクラスはすべて`Literal`と、実質的なボトム型`_Reserved`の合併型になります ([#950], [#957])。

    ```diff
    -class AccelerationMode(str, Enum):
    -    AUTO = "AUTO"
    -    CPU = "CPU"
    -    GPU = "GPU"
    +AccelerationMode: TypeAlias = Literal["AUTO", "CPU", "GPU"] | _Reserved
    ```

- \[Python\] \[BREAKING\] `Synthesizer.audio_query`は、C APIとJava APIに合わせる形で`create_audio_query`に改名されます ([#882])。

- \[Python\] \[BREAKING\] `UserDict.words`は、Java APIに合わせる形で`UserDict.to_dict`に改名されます ([#977])。

- \[Python\] \[BREAKING\] `Synthesizer.metas`と`UserDict.words`は`@property`ではなく普通のメソッドになります ([#914])。

- \[Python\] \[BREAKING\] `UserDictWord`の`@pydantic.dataclasses.dataclass`としての操作は非サポートになります。またdataclassとして`frozen`になり、コンストラクタ時点で各種バリデートが行われるようになります ([#1014])。

    補足: Pydanticは[0.16.0-preview.1](#0160-preview1---2025-03-08-0900)で消されます。

- \[Python\] \[BREAKING\] デフォルト値付きの引数はすべて[keyword-only argument](https://peps.python.org/pep-3102/)になります ([#998])。

- \[Python,Java\] \[BREAKING\] `SpeakerMeta`は<code>**Character**Meta</code>に、`StyleVersion`は<code>**Character**Version</code>に改名されます ([#931], [#943], [#996])。

    `speaker_uuid`はそのままです。

- \[Java\] \[BREAKING\] `Synthesizer`, `OpenJtalk`, `VoiceModelFile` (旧`VoiceModel`), `UserDict`は`voicevoxcore.blocking`パッケージの下に移ります。それに伴い、いくつかのクラスは`voicevoxcore`パッケージの直下に置かれるようになります ([#861])。

    - `voicevoxcore.{Synthesizer. => }AccelerationMode`
    - `voicevoxcore.{VoiceModel.SpeakerMeta => CharacterMeta}`
    - `voicevoxcore.{VoiceModel. => }StyleMeta`
    - `voicevoxcore.{UserDict.Word => UserDictWord}`

    (`Synthesizer`, `VoiceModelFile`, `UserDict`自体は`voicevoxcore.blocking`下に移動)

- \[Java\] \[BREAKING\] `AccelerationMode`と`UserDictWord.Type`はenumではなくなり、`switch`での網羅ができなくなります ([#955])。

    それぞれの値自体はそのままの名前で`public static final`な定数として定義されているので、引き続きそのまま利用可能です。

    ```java
    var mode = AccelerationMode.AUTO;
    ```

- \[Java\] \[BREAKING\] ビルダーパターンメソッドの締めの`execute`は`perform`に改名されます ([#911])。

- \[ダウンローダー\] \[BREAKING\] `onnxruntime`（新規追加）および`models`のダウンロードの際、利用規約への同意が求められるようになります ([VOICEVOX/voicevox\_vvm#1], [#928], [VOICEVOX/voicevox\_vvm#5], [#964], [#983], [#989], [#1006], [#1011])。

- \[ダウンローダー\] \[BREAKING\] `<TARGET>`のうち`core`は`c-api`に改名され、それに伴い`-v, --version`も`--c-api-version`、`--core-repo`も`--c-api-repo`に改名されます ([#942], [#1019])。

    補足: [0.16.1](#0161---2025-08-14-0900)にて`--version`は、VOICEVOX COREとしてのバージョンを出力するフラグになります。

- \[ダウンローダー\] \[BREAKING\] `<TARGET>`ごとにディレクトリが切られるようになります ([#944], [#969])。

    ```console
          --only <TARGET>...
              ダウンロード対象を限定する [possible values: c-api, onnxruntime, additional-libraries, models, dict]
          --exclude <TARGET>...
              ダウンロード対象を除外する [possible values: c-api, onnxruntime, additional-libraries, models, dict]
    ```

    ```
    voicevox_core
    ├── c_api/
    ├── onnxruntime/
    ├── additional_libraries/
    ├── models/
    └── dict/
    ```

- \[ダウンローダー\] \[BREAKING\] `models`において、上記のようにmodels/下に置かれるようになった他に次のような変更があります ([#928], [#989])。

    - VVM自体はvvm/というディレクトリに入る形になります。
    - README.mdはREADME.txtとして置かれるようになります。
    - [0.15.0-preview.16](#0150-preview16---2023-12-01-0900)まで含まれていたmetas.jsonは無くなります。

- \[ダウンローダー\] \\[BREAKING\] `--device`は`--devices`に改名され、複数の引数を取ることが可能になります ([#810])。

### Deprecated

- \[Python,Java\] PydanticおよびGSONは廃止予定になります ([#985])。

    現段階においては代替手段は無く、シリアライズ自体が推奨されない状態になっています。

    GSONについてはJacksonへの移行が予定されています ([#984])。

    補足: Pydanticについては[0.16.0-preview.1](#0160-preview1---2025-03-08-0900)で消されます。

### Removed

- \[macOS\] macOS 11およびmacOS 12がサポート範囲から外れ、バイナリのリリースはmacOS 13で行われるようになります ([#801], [#884])。

- \[Python\] \[BREAKING\] Pythonのバージョンが≧3.10に引き上げられます ([#915], [#926], [#927])。

    Python 3.10以降では、[asyncioランタイム終了時にクラッシュする問題](https://github.com/VOICEVOX/voicevox_core/issues/873)が発生しなくなります。

- \[Python,Java\] \[BREAKING\] `SupportedDevices`のデシアライズ（JSON → `SupportedDevices`の変換）ができなくなります。Python APIにおいてはコンストラクトもできなくなります ([#958])。

- \[Java\] \[BREAKING\] `UserDict.Word`改め`UserDictWord`には、GSONによるシリアライズは使えなくなります ([#1014])。

### Fixed

- "Added"の章で述べた`CharacterMeta::order`により、製品版VVMにおいて`metas`の出力が適切にソートされるようになります ([#728])。

    これにより、キャラクター/スタイルの順番がバージョン0.14およびVOICEVOX ENGINEのように整います。

- 空の`UserDict`を`use_user_dict`したときにクラッシュする問題が修正されます ([#733])。

- `VoiceModelFile::open` (旧`VoiceModel::from_path`)の実行時点で、ある程度の中身のバリデートがされるようになります ([#830])。

- \[C\] `voicevox_user_dict_add_word`がスタックを破壊してしまう問題が修正されます ([#800])。

- \[C\] \[iOS\] XCFrameworkへのdylibの入れかたが誤っていたために[App Storeへの申請が通らない](https://github.com/VOICEVOX/voicevox_core/issues/715)状態だったため、入れかたを変えます ([VOICEVOX/onnxruntime-builder#25] by [@nekomimimi], [#723] by [@nekomimimi])。

- \[C\] \[iOS\] clang++ 15.0.0でSIM向けビルドが失敗する問題が解決されます ([#720] by [@nekomimimi])。

- \[Python\] asyncioについての挙動が改善されます ([#834], [#868])。

- \[Python\] `StyleMeta`が`voicevox_core`モジュール直下に置かれるようになります ([#930])。

    これまでは、プライベートなモジュールに置かれているだけでした。

- \[Python\] 型定義において呼べないはずのコンストラクタが呼べることになってしまってたため、ダミーとなる`def __new__(cls, *args, **kwargs) -> NoReturn`を定義することで解決します。エラーメッセージも改善されます ([#988], [#997])。

- \[Python\] `SpeakerMeta`改め`CharacterMeta`において、`speaker_uuid`と`version`のdocstringが逆だったのが直ります ([#935])。

- \[Python\] Sphinx上で壊れるようなdocstringは書かれないようになります ([#996])。

### Security

- VOICEVOX COREおよびダウンローダーに影響するものだったのかどうかはわかりませんが、以下の脆弱性登録の影響を受けないように依存ライブラリがアップデートされます ([#856], [#887], [#890])。

    - [RUSTSEC-2024-0332](https://rustsec.org/advisories/RUSTSEC-2024-0332)
    - [RUSTSEC-2024-0336](https://rustsec.org/advisories/RUSTSEC-2024-0336)
    - [RUSTSEC-2024-0402](https://rustsec.org/advisories/RUSTSEC-2024-0402)
    - [RUSTSEC-2024-0404](https://rustsec.org/advisories/RUSTSEC-2024-0404)
    - [RUSTSEC-2024-0421](https://rustsec.org/advisories/RUSTSEC-2024-0421)

## [0.15.0-preview.16] - 2023-12-01 (+09:00)

### Added

- \[Python\] `StyleId`, `StyleVersion`, `VoiceModelId`が`NewType`として導入されます ([#678])。

    関数の引数としては`style_id: StyleId | int`のようになり、元の型は引き続き利用できます。

- \[Python\] 次のdataclassのフィールドが、VOICEVOX ENGINEのようにオプショナルになります ([#696])。

    - `Mora.consonant: Optional[str]`
    - `Mora.consonant_length: Optional[str]`
    - `AccentPhrase.pause_mora: Optional[Mora]`
    - `AccentPhrase.is_interrogative: bool`
    - `AudioQuery.kana: Optional[str]`

### Changed

- \[Python\] \[BREAKING\] 次のメソッドがasync化されます ([#667])。

    - `UserDict.load`
    - `UserDict.save`
    - `OpenJtalk.__new__` (利用できなくなり、代わりに`OpenJtalk.new`が追加されます)
    - `OpenJtalk.use_user_dict`

- \[Python\] \[BREAKING\] Pydanticがv2になります ([#695])。

    補足: Pydanticは[0.16.0-preview.1](#0160-preview1---2025-03-08-0900)で消されます。

### Fixed

- \[Python\] 音声合成の処理がasyncioのランタイムをブロックしないようになります ([#692])。
- \[Java\] ユーザー辞書の利用時に出ていた警告が消えます ([#684])。
- \[Java\] `OpenJtalk.useUserDict`を利用する際は`$TMPDIR`の設定が必要、ということがドキュメンテーションコメントに書かれます ([#682])。

## [0.15.0-preview.15] - 2023-11-13 (+09:00)

### Added

- READMEおよびPython exampleが改善されます ([#661], [#663])。
- `InferenceFailed`エラーが、ONNX Runtimeからの情報をきちんと持つようになります (補足: [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)にて`InferenceFailed`エラーは`RunModel`エラーに改名されます) ([#668])。
- \[Python\] ビルドに用いているMaturin, PyO3, pyo3-asyncio, pyo3-logがアップデートされます ([#664])。
- \[Python,Java\] `Synthesizer`が不要に排他制御されていたのが解消されます ([#666])。
- \[Java\] `Synthesizer#{getMetas,isGpuMode}`および、バージョン情報とデバイス情報が取得できる`GlobalInfo`クラスが追加されます ([#673])。
- \[Java\] ドキュメンテーションコメントが充実します ([#673])。

### Changed

- \[C,Python\] \[BREAKING\] `Synthesizer`および`OpenJtalk`の`new_with_initialize`は`new`にリネームされます ([#669])。
- \[Python\] \[BREAKING\] `Synthesizer.new(_with_initialize)`は無くなり、`__new__`からコンストラクトできるようになります ([#671])。

## [0.15.0-preview.14] - 2023-10-27 (+09:00)

### Added

- \[ダウンローダー\] :tada: ダウンロード対象を限定および除外するオプションが追加されます ([#647])。

    ```console
          --only <TARGET>...
              ダウンロード対象を限定する [possible values: core, models, additional-libraries, dict]
          --exclude <TARGET>...
              ダウンロード対象を除外する [possible values: core, models, additional-libraries, dict]
          --min
              `--only core`のエイリアス
    ```

    補足: [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)において、`core`は`c-api`に改名されます。またONNX Runtimeが`onnxruntime`という名前で分離されます。

- \[Python,Java\] エラーの文脈が例外チェーンとしてくっつくようになりました ([#640])。

### Changed

- \[Python,Java\] `VoicevoxError`/`VoicevoxException`は解体され、個別のエラーになります ([#640])。

### Fixed

- \[Python\] [0.15.0-preview.5](#0150-preview5---2023-08-06-0900)の時点で不要になっていたNumPyの依存が外れます ([#656])。
- \[Java\] MUTF-8である`String`の内容を誤ってUTF-8として認識してしまっていた問題が解決されます ([#654])。

## [0.15.0-preview.13] - 2023-10-14 (+09:00)

### Fixed

- \[ダウンローダー\] \[Windows\] ビルドの問題が解決され、リリースされるようになりました ([#643])。

## [0.15.0-preview.12] - 2023-10-14 (+09:00)

Windows版ダウンローダーのビルドに失敗しています。

### Added

- :tada: Android向けにJava APIが追加されます ([#558], [#611], [#612], [#621])。

    ```java
    var wav = synthesizer.tts("こんにちは", 0).execute();
    // 補足: 0.16.0-preview.0において、`execute`は`perform`になります
    ```

    ~/.m2/repository/の内容をZIPにしたものがjava\_package.zipとしてリリースされます。

- \[ダウンローダー\] リポジトリ指定機能が追加されます ([#641])。

    ```console
          --core-repo <REPOSITORY>
              [default: VOICEVOX/voicevox_core]
          --additional-libraries-repo <REPOSITORY>
              [default: VOICEVOX/voicevox_additional_libraries]
    ```

    ```console
    ❯ download --core-repo ${fork先}/voicevox_core --additional-libraries-repo ${fork先}/voicevox_additional_libraries
    ```

    補足: [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)において、`--core-repo`は`--c-api-repo`に改名されます。

### Changed

- \[BREAKING\] VVMはC APIのリリースに同梱される形でしたが、独立してmodel-{version}.zipとしてリリースされるようになります ([#603])。

### Fixed

- \[C\] [0.15.0-preview.3](#0150-preview3---2023-05-18-0900)で導入された不正な`json_free`および`wav_free`に対するセーフティネットのメッセージが、[0.15.0-preview.4](#0150-preview4---2023-06-21-0900)と[0.15.0-preview.5](#0150-preview5---2023-08-06-0900)に引き続き改善されます ([#625])。

## [0.15.0-preview.11] - 2023-10-08 (+09:00)

### Fixed

- 入っていなかった同梱物、おそらくreadmeがちゃんと入るようになります ([#630])。

## [0.15.0-preview.10] - 2023-10-07 (+09:00)

### Added

- エラーメッセージが改善されます ([#624])。
- \[Python\] GitHub Pagesに使うSphinxがアップデートされました ([#626])。

### Changed

- \[BREAKING\] `kana`オプションは廃止されます。代わりにバージョン0.13にあったような、"\_from\_kana"が付いたAPIが再び追加されます ([#577])。
- \[BREAKING\] 一部のエラーの名前が改名されます ([#622], [#623])。
    - `InvalidStyleId` → `StyleIdNotFound`
    - `InvalidModelId` → `ModelIdNotFound`
    - `UnknownWord` → `WordNotFound`
    - `UnloadedModel` → `ModelNotFound`

## [0.15.0-preview.9] - 2023-09-18 (+09:00)

### Added

- READMEおよびPython exampleが改善されます ([#584] by [@weweweok], [#590], [#598], [#613])。
    - [READMEの「その他の言語」](https://github.com/VOICEVOX/voicevox_core/blob/0.15.0-preview.9/README.md#その他の言語)に[VoicevoxCoreSharp](https://github.com/yamachu/VoicevoxCoreSharp)が追加されます。
    - Python exampleはBlackとisortでフォーマットされ、`--speaker-id`は`--style-id`になります。
- \[C\] 引数の`VoicevoxUserDictWord *`はunalignedであってもよくなります ([#601])。
- \[Python\] `__version__`が追加されます。以前から存在してはいましたが、プライベートなモジュールに置かれているだけでした ([#507], [#597])。
- \[Rust版ダウンローダー\] helpの表示が改善されます ([#604])。

### Changed

- \[C\] エラーの表示は`ERROR`レベルのログとしてなされるようになります ([#600])。
- \[Rust版ダウンローダー\] \[BREAKING\] `--min`と`--additional-libraries-version`の同時使用は無意味であるため、禁止されます ([#605])。

### Removed

- \[BREAKING\] `load_all_models`が廃止されます ([#587])。

    [0.15.0-preview.5](#0150-preview5---2023-08-06-0900)以降においても`${dllの場所}/model/`もしくは`$VV_MODELS_ROOT_DIR`下のVVMを一括で読む機能として残っていましたが、混乱を招くだけと判断して削除されることとなりました。

- \[BREAKING\] Bash版ダウンローダーとPowerShell版ダウンローダーは削除されます ([#602])。

    Rust版をお使いください。

### Fixed

- \[C\] ログ出力においてANSI escape sequenceを出すかどうかの判定を改善しました ([#616])。

    従来は環境変数のみで判定していましたが、これからはstderrがTTYかどうかを見て、必要なら`ENABLE_VIRTUAL_TERMINAL_PROCESSING`を有効化するようになります。

## [0.15.0-preview.8] - 2023-08-26 (+09:00)

### Fixed

- 各ライブラリがきちんとリリースされるようになりました ([#586])。

## [0.15.0-preview.7] - 2023-08-24 (+09:00)

各ライブラリのビルドが不可能な状態に陥り、ダウンローダーだけがリリースされています。コミットとしては[0.15.0-preview.6](#0150-preview6---2023-08-24-0900)と同一です。

## [0.15.0-preview.6] - 2023-08-24 (+09:00)

### Added

- エラーの内容が一部改善されます ([#553])。
- \[Python\] `Synthesizer`に`__enter__`と`__close__`が実装されます。`__close__`後の`Synthesizer`は使用不可になり、ロードされた音声モデルは解放されます ([#555])。
- \[Python\] docstringが書かれていなかった部分について、書かれます ([#570])。

### Changed

- \[C\] \[BREAKING\] `voicevox_synthesizer_audio_query`は`voicevox_synthesizer_create_audio_query`にリネームされます ([#576])。
- \[C\] \[BREAKING\] エラーの名前はすべて`VOICEVOX_RESULT_`が付く形に統一されます ([#576])。
- \[C\] \[BREAKING\] [0.15.0-preview.5](#0150-preview5---2023-08-06-0900)の[#503]はリバートされ、定数化された`voicevox_version`および`_options`はすべて関数に戻ります ([#557] by [@shigobu])。
- \[Python\] docstirngはNumPy記法で統一されます ([#570])。

### Removed

- \[Python\] \[BREAKING\] 意図せず露出してしまっていた内部関数がプライベートになります ([#570])。

### Fixed

- `Synthesizer`は並行に使えるようになります ([#553])。

- `load_all_models`における問題が修正されます ([#574], [#575])。

    補足: `load_all_models`は[0.15.0-preview.9](#0150-preview9---2023-09-18-0900)で削除されます。

- \[C\] \[iOS\] XCFrameworkにmodulemapが入るようになります ([#579] by [@fuziki])。

- \[C,Python\] ドキュメンテーションコメントが色々修正されます ([#571], [#570])。

## [0.15.0-preview.5] - 2023-08-06 (+09:00)

このバージョンではAPIの根本的な刷新が行われました。変更量が多いため、漏れがあるかもしれないことをご了承ください。

### Added

- :tada: 音声モデルが扱いやすい形式になり、音声モデル単位での明示的なロードが可能になります。またアンロードも可能になります ([#370], [#501], [#523], [#512], [#569], [#551])。

    音声モデルのファイルは _VVM_ と呼ばれます。拡張子は.vvmです。

- :tada: ユーザー辞書機能が使えるようになります ([#538], [#546])。

    ```py
    user_dict = UserDict()
    user_dict.add_word(UserDictWord("手札", "テフダ", 1, priority=6))

    open_jtalk.use_user_dict(user_dict)
    ```

    二つのユーザー辞書をマージしたり、JSONとの相互変換をすることができます。

- :tada: ドキュメンテーションコメントが充実します ([#532] helped by [@wappon28dev], [#534])。

    C APIについては、[RustのUB](https://doc.rust-lang.org/reference/behavior-considered-undefined.html)に踏み込むような領域について「安全性要件」が定められ、詳しく記述されるようになります。

- \[C\] Doxygenに`IGNORE_PREFIX`オプションを追加され、目次がよい感じになります ([#565])。

### Changed

- \[BREAKING\] 上記のVVMを表す`VoiceModel`型が追加され、音声モデルのロードはそこから行うことになります。"metas"は定数ではなくなり、`VoiceModel`もしくは下記の`Synthesizer`から取得する形になります ([#370], [#501], [#523], [#512], [#551])。

    補足: [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)において<code>VoiceModel**File**</code>に改名され、性質も変わります。

- \[BREAKING\] `OpenJtalk`型 (C API: <code>OpenJtalk**Rc**</code>)が追加され、システム辞書とユーザー辞書の登録はそこから行うことになります ([#370])。

- \[BREAKING\] `SupportedDevices`のインスタンスは定数ではなくなり、関数から取得する形になります ([#370], [#502])。

    将来のONNX Runtime次第ではありますが、エラーとなりうる形の関数になっています。

    補足: この時点では引数ゼロの静的な関数ですが、[0.16.0-preview.0](#0160-preview0---2025-03-01-0900)において`Onnxruntime`型のメソッドになります。

- \[BREAKING\] `speaker_id`はすべて`style_id`になります (Python APIのみ破壊的変更) ([#370], [#532] helped by [@wappon28dev])。

- READMEに「工事中」の案内が復活します ([#542])。

    補足: [0.16.0-preview.1](#0160-preview1---2025-03-08-0900)で解除されます。

- \[C\] \[BREAKING\] `voicevox_initialize`と`voicevox_finalize`は無くなります。代わりに`VoicevoxSynthesizer`型が追加され、ほとんどの操作はここから行うようになります ([#370], [#501], [#512])。

- \[C\] \[BREAKING\] `voicevox_audio_query_json_free`と`voicevox_accent_phrases_json_free`は、`voicevox_json_free`に統合されます ([#370])。

- \[C\] \[BREAKING\] いくつかの関数が定数になります ([#503])。

    補足: [0.15.0-preview.6](#0150-preview6---2023-08-24-0900)でリバートされます。

- \[Python\] \[BREAKING\] :tada: asyncioを用いたAPIへと変わります ([#370])。

    補足: [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)にて、asyncioによらないブロッキングAPIが復活します。
    補足: この後に登場するRust APIやNode.js API (予定)でも、非同期APIが利用可能になります。

- \[Python\] \[BREAKING\] `VoicevoxCore`は`Synthesizer`に改名されます ([#370])。

- \[Python\] \[BREAKING\] `Style`は`StyleMeta`に、`Meta`は`SpeakerMeta`に改名されます ([#370])。

    補足: [0.16.0-preview.0](#0160-preview0---2025-03-01-0900)にて、`SpeakerMeta`は`CharacterMeta`になります。また、`StyleMeta`はきちんとre-exportされるようになります。

- \[Python\] \[BREAKING\] wheelには音声モデルは埋め込まれなくなります ([#522])。

    補足: [0.15.0-preview.12](#0150-preview12---2023-10-14-0900)ではC APIのリリースからも分離されます。

### Removed

- \[BREAKING\] `predict_duration`といった、推論を直接実行するAPIは削除されます ([#370])。

### Fixed

- \[C\] `output_`系引数がunalignedであってもよくなります。以前はおそらく[RustのUB](https://doc.rust-lang.org/reference/behavior-considered-undefined.html)になっていました ([#534], [#535])。
- \[C\] [0.15.0-preview.3](#0150-preview3---2023-05-18-0900)で導入された不正な`json_free`およびに対するセーフティネットのメッセージが、[0.15.0-preview.4](#0150-preview4---2023-06-21-0900)に引き続き改善されます ([#521])。

## [0.15.0-preview.4] - 2023-06-21 (+09:00)

### Added

- [READMEの「事例紹介」](https://github.com/VOICEVOX/voicevox_core/blob/0.15.0-preview.4/README.md#事例紹介)に、Goラッパーの事例として[voicevoxcore.go](https://github.com/sh1ma/voicevoxcore.go)が追加されます ([#498] by [@sh1ma], [#511])。
- \[C\] :tada: iOS向けXCFrameworkがリリースに含まれるようになります ([#485] by [@HyodaKazuaki])。
- \[C\] [0.15.0-preview.3](#0150-preview3---2023-05-18-0900)で導入された`json_free`のセーフティネットについて、メッセージが改善されます ([#500])。
- \[C\] ヘッダに[cbindgen](https://docs.rs/crate/cbindgen)のバージョンが記載されるようになります ([#519])。
- \[C\] ヘッダにおける変な空行が削除されます ([#518])。
- \[Python\] Rustのパニックが発生したときの挙動が「プロセスのabort」から、「`pyo3_runtime.PanicException`の発生」に変わります ([#505])。

### Fixed

- \[Python\] exampleコードとそのドキュメントが修正されます ([#494], [#495])。

## [0.15.0-preview.3] - 2023-05-18 (+09:00)

### Added

- :tada: 音素の長さ、もしくは音高の再生成ができるようになります ([#479], [#483])。

    VOICEVOX ENGINEの`/mora_{length,pitch,data}`にあたります。これまでは、テキストから`AudioQuery`を丸ごと作る必要がありました。

- `AudioQuery`ではない、`accent_phrases`のみの生成ができるようになります ([#479], [#483])。

    VOICEVOX ENGINEの`/accent_phrases`にあたります。

- `AudioQuery`の`kana`が、VOICEVOX ENGINEと同様に省略可能になります ([#486], [#487])。

- READMEが改善されます ([#404], [#429] by [@windymelt], [#439] by [@misogihagi], [#458] by [@char5742], [#455] by [@yerrowTail], [#463])。

    - [READMEの「その他の言語」](https://github.com/VOICEVOX/voicevox_core/blob/0.15.0-preview.3/README.md#その他の言語)に、Goラッパーの事例として[VOICEVOX CORE Go サンプル](https://github.com/yerrowTail/voicevox_core_go_sample)が追加されます。
    - [READMEの「事例紹介」](https://github.com/VOICEVOX/voicevox_core/blob/0.15.0-preview.3/README.md#事例紹介)に、次の二つが追加されます。
        - Flutterラッパーの事例として[voicevox\_flutter](https://github.com/char5742/voicevox_flutter) ([@char5742])
        - Scalaラッパーの事例として[voicevoxcore4s](https://github.com/windymelt/voicevoxcore4s) ([@windymelt])
    - [VOICEVOX Community by Discord](https://discord.gg/WMwWetrzuh)をはじめとしたバッジが載ります。
    - スクリプト版ダウンローダーの代わりにRust版を使うよう案内されます（補足: [0.15.0-preview.9](#0150-preview9---2023-09-18-0900)にてスクリプト版は削除されます）。
    - 「工事中」の表記が消えます。

- \[C\] :tada: Androidをターゲットとしたビルドが追加されます ([#444] by [@char5742], [#450], [#452] by [@char5742], [#473])。

- \[C\] :tada: iOSをターゲットとしたビルドが追加されます ([#471] by [@HyodaKazuaki])。

- \[C\] `json_free`および`wav_free`に、知らない配列/文字列と解放済みの配列/文字列を拒否するセーフティネット機構が入ります ([#392] by [@higumachan], [#478])。

    アロケーションの回数を抑える、パフォーマンス改善でもあります。

- \[Python\] PyO3版exampleとそのドキュメントが改善されます ([#481], [#419] by [@osakanataro], [#475], [#421] by [@misogihagi])

- \[Rust版ダウンローダー\] download-windows-x64.exeはコード署名されるようになります ([#412])。

### Changed

- \[C\] ログの時刻がローカル時刻になります ([#400], [#434])。
- \[Python\] `ctypes`版exampleは削除され、PyO3版exampleが[example/python](https://github.com/VOICEVOX/voicevox_core/blob/0.15.0-preview.3/example/python)になります ([#432])。
- \[Rust版ダウンローダー\] リリースの`download-{linux,osx}-aarch64`は`…-arm64`に改名されます ([#416])。

### Fixed

- kanaからAudioQueryを作る際、音素の流さと音高が未設定になってしまう問題が修正されます ([#407])。

- `text`がUTF-8である必要があるという案内がドキュメンテーションコメントに書かれます ([#438])。

- READMEの書式について、軽微な修正が入ります（日本語とアルファベットの間にスペースが入っていたりいなかったりしていたので、スペースを入れるよう統一しました）([#455] by [@yerrowTail])。

    補足: その後は特に気にされてはおらず、混在する状態になっています。またこのようなスペースについてissueが提議されたこともありません。

- [#422]が解決されます ([Hiroshiba/vv\_core\_inference#12])。

- \[C\] \[Windows\] C++ exampleが修正されます ([#420] by [@shigobu])。

- \[Python\] モジュールに`__all__`が適切に設定されます ([#415])。

[Unreleased]: https://github.com/VOICEVOX/voicevox_core/compare/0.16.4...HEAD
[0.16.4]: https://github.com/VOICEVOX/voicevox_core/compare/0.16.3...0.16.4
[0.16.3]: https://github.com/VOICEVOX/voicevox_core/compare/0.16.2...0.16.3
[0.16.2]: https://github.com/VOICEVOX/voicevox_core/compare/0.16.1...0.16.2
[0.16.1]: https://github.com/VOICEVOX/voicevox_core/compare/0.16.0...0.16.1
[0.16.0]: https://github.com/VOICEVOX/voicevox_core/compare/0.16.0-preview.1...0.16.0
[0.16.0-preview.1]: https://github.com/VOICEVOX/voicevox_core/compare/0.16.0-preview.0...0.16.0-preview.1
[0.16.0-preview.0]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.16...0.16.0-preview.0
[0.15.0-preview.16]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.15...0.15.0-preview.16
[0.15.0-preview.15]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.14...0.15.0-preview.15
[0.15.0-preview.14]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.13...0.15.0-preview.14
[0.15.0-preview.13]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.12...0.15.0-preview.13
[0.15.0-preview.12]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.11...0.15.0-preview.12
[0.15.0-preview.11]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.10...0.15.0-preview.11
[0.15.0-preview.10]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.9...0.15.0-preview.10
[0.15.0-preview.9]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.8...0.15.0-preview.9
[0.15.0-preview.8]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.7...0.15.0-preview.8
[0.15.0-preview.7]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.6...0.15.0-preview.7
[0.15.0-preview.6]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.5...0.15.0-preview.6
[0.15.0-preview.5]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.4...0.15.0-preview.5
[0.15.0-preview.4]: https://github.com/VOICEVOX/voicevox_core/compare/0.15.0-preview.3...0.15.0-preview.4
[0.15.0-preview.3]: https://github.com/VOICEVOX/voicevox_core/compare/0.14.0...0.15.0-preview.3

[#370]: https://github.com/VOICEVOX/voicevox_core/pull/370
[#392]: https://github.com/VOICEVOX/voicevox_core/pull/392
[#400]: https://github.com/VOICEVOX/voicevox_core/pull/400
[#404]: https://github.com/VOICEVOX/voicevox_core/pull/404
[#407]: https://github.com/VOICEVOX/voicevox_core/pull/407
[#412]: https://github.com/VOICEVOX/voicevox_core/pull/412
[#415]: https://github.com/VOICEVOX/voicevox_core/pull/415
[#416]: https://github.com/VOICEVOX/voicevox_core/pull/416
[#419]: https://github.com/VOICEVOX/voicevox_core/pull/419
[#420]: https://github.com/VOICEVOX/voicevox_core/pull/420
[#421]: https://github.com/VOICEVOX/voicevox_core/pull/421
[#422]: https://github.com/VOICEVOX/voicevox_core/issues/422
[#425]: https://github.com/VOICEVOX/voicevox_core/pull/425
[#429]: https://github.com/VOICEVOX/voicevox_core/pull/429
[#432]: https://github.com/VOICEVOX/voicevox_core/pull/432
[#434]: https://github.com/VOICEVOX/voicevox_core/pull/434
[#438]: https://github.com/VOICEVOX/voicevox_core/pull/438
[#439]: https://github.com/VOICEVOX/voicevox_core/pull/439
[#443]: https://github.com/VOICEVOX/voicevox_core/pull/443
[#444]: https://github.com/VOICEVOX/voicevox_core/pull/444
[#450]: https://github.com/VOICEVOX/voicevox_core/pull/450
[#452]: https://github.com/VOICEVOX/voicevox_core/pull/452
[#455]: https://github.com/VOICEVOX/voicevox_core/pull/455
[#458]: https://github.com/VOICEVOX/voicevox_core/pull/458
[#463]: https://github.com/VOICEVOX/voicevox_core/pull/463
[#471]: https://github.com/VOICEVOX/voicevox_core/pull/471
[#473]: https://github.com/VOICEVOX/voicevox_core/pull/473
[#475]: https://github.com/VOICEVOX/voicevox_core/pull/475
[#478]: https://github.com/VOICEVOX/voicevox_core/pull/478
[#479]: https://github.com/VOICEVOX/voicevox_core/pull/479
[#481]: https://github.com/VOICEVOX/voicevox_core/pull/481
[#483]: https://github.com/VOICEVOX/voicevox_core/pull/483
[#485]: https://github.com/VOICEVOX/voicevox_core/pull/485
[#486]: https://github.com/VOICEVOX/voicevox_core/pull/486
[#487]: https://github.com/VOICEVOX/voicevox_core/pull/487
[#494]: https://github.com/VOICEVOX/voicevox_core/pull/494
[#495]: https://github.com/VOICEVOX/voicevox_core/pull/495
[#496]: https://github.com/VOICEVOX/voicevox_core/issues/496
[#497]: https://github.com/VOICEVOX/voicevox_core/pull/497
[#498]: https://github.com/VOICEVOX/voicevox_core/pull/498
[#500]: https://github.com/VOICEVOX/voicevox_core/pull/500
[#501]: https://github.com/VOICEVOX/voicevox_core/pull/501
[#502]: https://github.com/VOICEVOX/voicevox_core/pull/502
[#503]: https://github.com/VOICEVOX/voicevox_core/pull/503
[#505]: https://github.com/VOICEVOX/voicevox_core/pull/505
[#507]: https://github.com/VOICEVOX/voicevox_core/pull/507
[#508]: https://github.com/VOICEVOX/voicevox_core/pull/508
[#511]: https://github.com/VOICEVOX/voicevox_core/pull/511
[#512]: https://github.com/VOICEVOX/voicevox_core/pull/512
[#514]: https://github.com/VOICEVOX/voicevox_core/pull/514
[#515]: https://github.com/VOICEVOX/voicevox_core/pull/
[#516]: https://github.com/VOICEVOX/voicevox_core/pull/516
[#518]: https://github.com/VOICEVOX/voicevox_core/pull/518
[#519]: https://github.com/VOICEVOX/voicevox_core/pull/519
[#520]: https://github.com/VOICEVOX/voicevox_core/pull/520
[#521]: https://github.com/VOICEVOX/voicevox_core/pull/521
[#522]: https://github.com/VOICEVOX/voicevox_core/pull/522
[#523]: https://github.com/VOICEVOX/voicevox_core/pull/523
[#525]: https://github.com/VOICEVOX/voicevox_core/pull/525
[#531]: https://github.com/VOICEVOX/voicevox_core/pull/531
[#532]: https://github.com/VOICEVOX/voicevox_core/pull/532
[#534]: https://github.com/VOICEVOX/voicevox_core/pull/534
[#535]: https://github.com/VOICEVOX/voicevox_core/pull/535
[#536]: https://github.com/VOICEVOX/voicevox_core/pull/536
[#538]: https://github.com/VOICEVOX/voicevox_core/pull/538
[#542]: https://github.com/VOICEVOX/voicevox_core/pull/542
[#546]: https://github.com/VOICEVOX/voicevox_core/pull/546
[#551]: https://github.com/VOICEVOX/voicevox_core/pull/551
[#553]: https://github.com/VOICEVOX/voicevox_core/pull/553
[#555]: https://github.com/VOICEVOX/voicevox_core/pull/555
[#557]: https://github.com/VOICEVOX/voicevox_core/pull/557
[#558]: https://github.com/VOICEVOX/voicevox_core/pull/558
[#565]: https://github.com/VOICEVOX/voicevox_core/pull/565
[#569]: https://github.com/VOICEVOX/voicevox_core/pull/569
[#570]: https://github.com/VOICEVOX/voicevox_core/pull/570
[#571]: https://github.com/VOICEVOX/voicevox_core/pull/571
[#573]: https://github.com/VOICEVOX/voicevox_core/pull/573
[#574]: https://github.com/VOICEVOX/voicevox_core/pull/574
[#575]: https://github.com/VOICEVOX/voicevox_core/pull/575
[#576]: https://github.com/VOICEVOX/voicevox_core/pull/576
[#577]: https://github.com/VOICEVOX/voicevox_core/pull/577
[#579]: https://github.com/VOICEVOX/voicevox_core/pull/579
[#580]: https://github.com/VOICEVOX/voicevox_core/pull/580
[#584]: https://github.com/VOICEVOX/voicevox_core/pull/584
[#586]: https://github.com/VOICEVOX/voicevox_core/pull/586
[#587]: https://github.com/VOICEVOX/voicevox_core/pull/587
[#589]: https://github.com/VOICEVOX/voicevox_core/pull/589
[#590]: https://github.com/VOICEVOX/voicevox_core/pull/590
[#597]: https://github.com/VOICEVOX/voicevox_core/pull/597
[#598]: https://github.com/VOICEVOX/voicevox_core/pull/598
[#600]: https://github.com/VOICEVOX/voicevox_core/pull/600
[#601]: https://github.com/VOICEVOX/voicevox_core/pull/601
[#602]: https://github.com/VOICEVOX/voicevox_core/pull/602
[#603]: https://github.com/VOICEVOX/voicevox_core/pull/603
[#604]: https://github.com/VOICEVOX/voicevox_core/pull/604
[#605]: https://github.com/VOICEVOX/voicevox_core/pull/605
[#611]: https://github.com/VOICEVOX/voicevox_core/pull/611
[#612]: https://github.com/VOICEVOX/voicevox_core/pull/612
[#613]: https://github.com/VOICEVOX/voicevox_core/pull/613
[#616]: https://github.com/VOICEVOX/voicevox_core/pull/616
[#621]: https://github.com/VOICEVOX/voicevox_core/pull/621
[#622]: https://github.com/VOICEVOX/voicevox_core/pull/622
[#623]: https://github.com/VOICEVOX/voicevox_core/pull/623
[#624]: https://github.com/VOICEVOX/voicevox_core/pull/624
[#625]: https://github.com/VOICEVOX/voicevox_core/pull/625
[#626]: https://github.com/VOICEVOX/voicevox_core/pull/626
[#630]: https://github.com/VOICEVOX/voicevox_core/pull/630
[#640]: https://github.com/VOICEVOX/voicevox_core/pull/640
[#641]: https://github.com/VOICEVOX/voicevox_core/pull/641
[#643]: https://github.com/VOICEVOX/voicevox_core/pull/643
[#646]: https://github.com/VOICEVOX/voicevox_core/pull/646
[#647]: https://github.com/VOICEVOX/voicevox_core/pull/647
[#654]: https://github.com/VOICEVOX/voicevox_core/pull/654
[#656]: https://github.com/VOICEVOX/voicevox_core/pull/656
[#661]: https://github.com/VOICEVOX/voicevox_core/pull/661
[#663]: https://github.com/VOICEVOX/voicevox_core/pull/663
[#664]: https://github.com/VOICEVOX/voicevox_core/pull/664
[#666]: https://github.com/VOICEVOX/voicevox_core/pull/666
[#667]: https://github.com/VOICEVOX/voicevox_core/pull/667
[#668]: https://github.com/VOICEVOX/voicevox_core/pull/668
[#669]: https://github.com/VOICEVOX/voicevox_core/pull/669
[#671]: https://github.com/VOICEVOX/voicevox_core/pull/671
[#673]: https://github.com/VOICEVOX/voicevox_core/pull/673
[#675]: https://github.com/VOICEVOX/voicevox_core/pull/675
[#678]: https://github.com/VOICEVOX/voicevox_core/pull/678
[#682]: https://github.com/VOICEVOX/voicevox_core/pull/682
[#684]: https://github.com/VOICEVOX/voicevox_core/pull/684
[#692]: https://github.com/VOICEVOX/voicevox_core/pull/692
[#694]: https://github.com/VOICEVOX/voicevox_core/pull/694
[#695]: https://github.com/VOICEVOX/voicevox_core/pull/695
[#696]: https://github.com/VOICEVOX/voicevox_core/pull/696
[#699]: https://github.com/VOICEVOX/voicevox_core/pull/699
[#702]: https://github.com/VOICEVOX/voicevox_core/pull/702
[#706]: https://github.com/VOICEVOX/voicevox_core/pull/706
[#707]: https://github.com/VOICEVOX/voicevox_core/pull/707
[#708]: https://github.com/VOICEVOX/voicevox_core/pull/708
[#719]: https://github.com/VOICEVOX/voicevox_core/pull/719
[#720]: https://github.com/VOICEVOX/voicevox_core/pull/720
[#723]: https://github.com/VOICEVOX/voicevox_core/pull/723
[#724]: https://github.com/VOICEVOX/voicevox_core/pull/724
[#725]: https://github.com/VOICEVOX/voicevox_core/pull/725
[#728]: https://github.com/VOICEVOX/voicevox_core/pull/728
[#732]: https://github.com/VOICEVOX/voicevox_core/pull/732
[#733]: https://github.com/VOICEVOX/voicevox_core/pull/733
[#738]: https://github.com/VOICEVOX/voicevox_core/pull/738
[#740]: https://github.com/VOICEVOX/voicevox_core/pull/740
[#745]: https://github.com/VOICEVOX/voicevox_core/pull/745
[#747]: https://github.com/VOICEVOX/voicevox_core/pull/747
[#752]: https://github.com/VOICEVOX/voicevox_core/pull/752
[#753]: https://github.com/VOICEVOX/voicevox_core/pull/753
[#759]: https://github.com/VOICEVOX/voicevox_core/pull/759
[#761]: https://github.com/VOICEVOX/voicevox_core/pull/761
[#764]: https://github.com/VOICEVOX/voicevox_core/pull/764
[#794]: https://github.com/VOICEVOX/voicevox_core/pull/794
[#795]: https://github.com/VOICEVOX/voicevox_core/pull/795
[#796]: https://github.com/VOICEVOX/voicevox_core/pull/796
[#800]: https://github.com/VOICEVOX/voicevox_core/pull/800
[#801]: https://github.com/VOICEVOX/voicevox_core/pull/801
[#802]: https://github.com/VOICEVOX/voicevox_core/pull/802
[#803]: https://github.com/VOICEVOX/voicevox_core/pull/803
[#805]: https://github.com/VOICEVOX/voicevox_core/pull/805
[#806]: https://github.com/VOICEVOX/voicevox_core/pull/806
[#807]: https://github.com/VOICEVOX/voicevox_core/pull/807
[#810]: https://github.com/VOICEVOX/voicevox_core/pull/810
[#822]: https://github.com/VOICEVOX/voicevox_core/pull/822
[#823]: https://github.com/VOICEVOX/voicevox_core/pull/823
[#824]: https://github.com/VOICEVOX/voicevox_core/pull/824
[#825]: https://github.com/VOICEVOX/voicevox_core/pull/825
[#830]: https://github.com/VOICEVOX/voicevox_core/pull/830
[#831]: https://github.com/VOICEVOX/voicevox_core/pull/831
[#832]: https://github.com/VOICEVOX/voicevox_core/pull/832
[#834]: https://github.com/VOICEVOX/voicevox_core/pull/834
[#835]: https://github.com/VOICEVOX/voicevox_core/pull/835
[#837]: https://github.com/VOICEVOX/voicevox_core/pull/837
[#838]: https://github.com/VOICEVOX/voicevox_core/pull/838
[#844]: https://github.com/VOICEVOX/voicevox_core/pull/844
[#846]: https://github.com/VOICEVOX/voicevox_core/pull/846
[#847]: https://github.com/VOICEVOX/voicevox_core/pull/847
[#849]: https://github.com/VOICEVOX/voicevox_core/pull/849
[#850]: https://github.com/VOICEVOX/voicevox_core/pull/850
[#851]: https://github.com/VOICEVOX/voicevox_core/pull/851
[#856]: https://github.com/VOICEVOX/voicevox_core/pull/856
[#860]: https://github.com/VOICEVOX/voicevox_core/pull/860
[#861]: https://github.com/VOICEVOX/voicevox_core/pull/861
[#862]: https://github.com/VOICEVOX/voicevox_core/pull/862
[#863]: https://github.com/VOICEVOX/voicevox_core/pull/863
[#868]: https://github.com/VOICEVOX/voicevox_core/pull/868
[#876]: https://github.com/VOICEVOX/voicevox_core/pull/876
[#881]: https://github.com/VOICEVOX/voicevox_core/pull/881
[#882]: https://github.com/VOICEVOX/voicevox_core/pull/882
[#884]: https://github.com/VOICEVOX/voicevox_core/pull/884
[#886]: https://github.com/VOICEVOX/voicevox_core/pull/886
[#887]: https://github.com/VOICEVOX/voicevox_core/pull/887
[#889]: https://github.com/VOICEVOX/voicevox_core/pull/889
[#890]: https://github.com/VOICEVOX/voicevox_core/pull/890
[#894]: https://github.com/VOICEVOX/voicevox_core/pull/894
[#895]: https://github.com/VOICEVOX/voicevox_core/pull/895
[#896]: https://github.com/VOICEVOX/voicevox_core/pull/896
[#898]: https://github.com/VOICEVOX/voicevox_core/pull/898
[#903]: https://github.com/VOICEVOX/voicevox_core/pull/903
[#907]: https://github.com/VOICEVOX/voicevox_core/pull/907
[#910]: https://github.com/VOICEVOX/voicevox_core/pull/910
[#911]: https://github.com/VOICEVOX/voicevox_core/pull/911
[#912]: https://github.com/VOICEVOX/voicevox_core/pull/912
[#913]: https://github.com/VOICEVOX/voicevox_core/pull/913
[#914]: https://github.com/VOICEVOX/voicevox_core/pull/914
[#915]: https://github.com/VOICEVOX/voicevox_core/pull/915
[#918]: https://github.com/VOICEVOX/voicevox_core/pull/918
[#919]: https://github.com/VOICEVOX/voicevox_core/pull/919
[#921]: https://github.com/VOICEVOX/voicevox_core/pull/921
[#926]: https://github.com/VOICEVOX/voicevox_core/pull/926
[#927]: https://github.com/VOICEVOX/voicevox_core/pull/927
[#928]: https://github.com/VOICEVOX/voicevox_core/pull/928
[#930]: https://github.com/VOICEVOX/voicevox_core/pull/930
[#931]: https://github.com/VOICEVOX/voicevox_core/pull/931
[#932]: https://github.com/VOICEVOX/voicevox_core/pull/932
[#933]: https://github.com/VOICEVOX/voicevox_core/pull/933
[#935]: https://github.com/VOICEVOX/voicevox_core/pull/935
[#937]: https://github.com/VOICEVOX/voicevox_core/pull/937
[#939]: https://github.com/VOICEVOX/voicevox_core/pull/939
[#940]: https://github.com/VOICEVOX/voicevox_core/pull/940
[#941]: https://github.com/VOICEVOX/voicevox_core/pull/941
[#942]: https://github.com/VOICEVOX/voicevox_core/pull/942
[#943]: https://github.com/VOICEVOX/voicevox_core/pull/943
[#944]: https://github.com/VOICEVOX/voicevox_core/pull/944
[#945]: https://github.com/VOICEVOX/voicevox_core/pull/945
[#946]: https://github.com/VOICEVOX/voicevox_core/pull/946
[#947]: https://github.com/VOICEVOX/voicevox_core/pull/947
[#949]: https://github.com/VOICEVOX/voicevox_core/pull/949
[#950]: https://github.com/VOICEVOX/voicevox_core/pull/950
[#952]: https://github.com/VOICEVOX/voicevox_core/pull/952
[#953]: https://github.com/VOICEVOX/voicevox_core/pull/953
[#954]: https://github.com/VOICEVOX/voicevox_core/pull/954
[#955]: https://github.com/VOICEVOX/voicevox_core/pull/955
[#957]: https://github.com/VOICEVOX/voicevox_core/pull/957
[#958]: https://github.com/VOICEVOX/voicevox_core/pull/958
[#959]: https://github.com/VOICEVOX/voicevox_core/pull/959
[#964]: https://github.com/VOICEVOX/voicevox_core/pull/964
[#965]: https://github.com/VOICEVOX/voicevox_core/pull/965
[#967]: https://github.com/VOICEVOX/voicevox_core/pull/967
[#969]: https://github.com/VOICEVOX/voicevox_core/pull/969
[#973]: https://github.com/VOICEVOX/voicevox_core/pull/973
[#974]: https://github.com/VOICEVOX/voicevox_core/pull/974
[#976]: https://github.com/VOICEVOX/voicevox_core/pull/976
[#977]: https://github.com/VOICEVOX/voicevox_core/pull/977
[#979]: https://github.com/VOICEVOX/voicevox_core/pull/979
[#980]: https://github.com/VOICEVOX/voicevox_core/pull/980
[#982]: https://github.com/VOICEVOX/voicevox_core/pull/982
[#983]: https://github.com/VOICEVOX/voicevox_core/pull/983
[#984]: https://github.com/VOICEVOX/voicevox_core/issues/984
[#985]: https://github.com/VOICEVOX/voicevox_core/pull/985
[#986]: https://github.com/VOICEVOX/voicevox_core/pull/986
[#988]: https://github.com/VOICEVOX/voicevox_core/pull/988
[#989]: https://github.com/VOICEVOX/voicevox_core/pull/989
[#990]: https://github.com/VOICEVOX/voicevox_core/pull/990
[#991]: https://github.com/VOICEVOX/voicevox_core/pull/991
[#992]: https://github.com/VOICEVOX/voicevox_core/pull/992
[#993]: https://github.com/VOICEVOX/voicevox_core/pull/993
[#994]: https://github.com/VOICEVOX/voicevox_core/pull/994
[#995]: https://github.com/VOICEVOX/voicevox_core/pull/995
[#996]: https://github.com/VOICEVOX/voicevox_core/pull/996
[#997]: https://github.com/VOICEVOX/voicevox_core/pull/997
[#998]: https://github.com/VOICEVOX/voicevox_core/pull/998
[#999]: https://github.com/VOICEVOX/voicevox_core/pull/999
[#1001]: https://github.com/VOICEVOX/voicevox_core/issues/1001
[#1002]: https://github.com/VOICEVOX/voicevox_core/pull/1002
[#1003]: https://github.com/VOICEVOX/voicevox_core/pull/1003
[#1006]: https://github.com/VOICEVOX/voicevox_core/pull/1006
[#1011]: https://github.com/VOICEVOX/voicevox_core/pull/1011
[#1014]: https://github.com/VOICEVOX/voicevox_core/pull/1014
[#1015]: https://github.com/VOICEVOX/voicevox_core/pull/1015
[#1016]: https://github.com/VOICEVOX/voicevox_core/pull/1016
[#1019]: https://github.com/VOICEVOX/voicevox_core/pull/1019
[#1020]: https://github.com/VOICEVOX/voicevox_core/pull/1020
[#1021]: https://github.com/VOICEVOX/voicevox_core/pull/1021
[#1023]: https://github.com/VOICEVOX/voicevox_core/pull/1023
[#1024]: https://github.com/VOICEVOX/voicevox_core/pull/1024
[#1025]: https://github.com/VOICEVOX/voicevox_core/pull/1025
[#1028]: https://github.com/VOICEVOX/voicevox_core/pull/1028
[#1030]: https://github.com/VOICEVOX/voicevox_core/pull/1030
[#1032]: https://github.com/VOICEVOX/voicevox_core/pull/1032
[#1033]: https://github.com/VOICEVOX/voicevox_core/pull/1033
[#1034]: https://github.com/VOICEVOX/voicevox_core/pull/1034
[#1035]: https://github.com/VOICEVOX/voicevox_core/pull/1035
[#1040]: https://github.com/VOICEVOX/voicevox_core/pull/1040
[#1041]: https://github.com/VOICEVOX/voicevox_core/pull/1041
[#1043]: https://github.com/VOICEVOX/voicevox_core/pull/1043
[#1044]: https://github.com/VOICEVOX/voicevox_core/pull/1044
[#1045]: https://github.com/VOICEVOX/voicevox_core/pull/1045
[#1048]: https://github.com/VOICEVOX/voicevox_core/pull/1048
[#1049]: https://github.com/VOICEVOX/voicevox_core/pull/1049
[#1055]: https://github.com/VOICEVOX/voicevox_core/pull/1055
[#1057]: https://github.com/VOICEVOX/voicevox_core/pull/1057
[#1058]: https://github.com/VOICEVOX/voicevox_core/pull/1058
[#1060]: https://github.com/VOICEVOX/voicevox_core/pull/1060
[#1063]: https://github.com/VOICEVOX/voicevox_core/pull/1063
[#1070]: https://github.com/VOICEVOX/voicevox_core/pull/1070
[#1073]: https://github.com/VOICEVOX/voicevox_core/pull/1073
[#1077]: https://github.com/VOICEVOX/voicevox_core/pull/1077
[#1078]: https://github.com/VOICEVOX/voicevox_core/pull/1078
[#1082]: https://github.com/VOICEVOX/voicevox_core/pull/1082
[#1085]: https://github.com/VOICEVOX/voicevox_core/pull/1085
[#1093]: https://github.com/VOICEVOX/voicevox_core/pull/1093
[#1094]: https://github.com/VOICEVOX/voicevox_core/pull/1094
[#1096]: https://github.com/VOICEVOX/voicevox_core/pull/1096
[#1098]: https://github.com/VOICEVOX/voicevox_core/pull/1098
[#1099]: https://github.com/VOICEVOX/voicevox_core/pull/1099
[#1100]: https://github.com/VOICEVOX/voicevox_core/pull/1100
[#1103]: https://github.com/VOICEVOX/voicevox_core/issues/1103
[#1108]: https://github.com/VOICEVOX/voicevox_core/pull/1108
[#1109]: https://github.com/VOICEVOX/voicevox_core/pull/1109
[#1111]: https://github.com/VOICEVOX/voicevox_core/pull/1111
[#1116]: https://github.com/VOICEVOX/voicevox_core/pull/1116
[#1117]: https://github.com/VOICEVOX/voicevox_core/pull/1117
[#1118]: https://github.com/VOICEVOX/voicevox_core/pull/1118
[#1121]: https://github.com/VOICEVOX/voicevox_core/pull/1121
[#1123]: https://github.com/VOICEVOX/voicevox_core/pull/1123
[#1124]: https://github.com/VOICEVOX/voicevox_core/pull/1124
[#1125]: https://github.com/VOICEVOX/voicevox_core/pull/1125
[#1126]: https://github.com/VOICEVOX/voicevox_core/pull/1126
[#1127]: https://github.com/VOICEVOX/voicevox_core/issues/1127
[#1128]: https://github.com/VOICEVOX/voicevox_core/pull/1128
[#1131]: https://github.com/VOICEVOX/voicevox_core/pull/1131
[#1132]: https://github.com/VOICEVOX/voicevox_core/pull/1132
[#1133]: https://github.com/VOICEVOX/voicevox_core/pull/1133
[#1134]: https://github.com/VOICEVOX/voicevox_core/pull/1134
[#1136]: https://github.com/VOICEVOX/voicevox_core/pull/1136
[#1137]: https://github.com/VOICEVOX/voicevox_core/pull/1137
[#1138]: https://github.com/VOICEVOX/voicevox_core/pull/1138
[#1139]: https://github.com/VOICEVOX/voicevox_core/pull/1139
[#1140]: https://github.com/VOICEVOX/voicevox_core/pull/1140
[#1143]: https://github.com/VOICEVOX/voicevox_core/pull/1143
[#1144]: https://github.com/VOICEVOX/voicevox_core/pull/1144
[#1147]: https://github.com/VOICEVOX/voicevox_core/pull/1147
[#1149]: https://github.com/VOICEVOX/voicevox_core/pull/1149
[#1153]: https://github.com/VOICEVOX/voicevox_core/pull/1153
[#1155]: https://github.com/VOICEVOX/voicevox_core/pull/1155
[#1164]: https://github.com/VOICEVOX/voicevox_core/pull/1164
[#1174]: https://github.com/VOICEVOX/voicevox_core/pull/1174
[#1190]: https://github.com/VOICEVOX/voicevox_core/pull/1190
[#1197]: https://github.com/VOICEVOX/voicevox_core/pull/1197
[#1203]: https://github.com/VOICEVOX/voicevox_core/pull/1203
[#1208]: https://github.com/VOICEVOX/voicevox_core/pull/1208
[#1214]: https://github.com/VOICEVOX/voicevox_core/pull/1214
[#1217]: https://github.com/VOICEVOX/voicevox_core/pull/1217
[#1220]: https://github.com/VOICEVOX/voicevox_core/pull/1220
[#1221]: https://github.com/VOICEVOX/voicevox_core/pull/1221
[#1222]: https://github.com/VOICEVOX/voicevox_core/pull/1222
[#1223]: https://github.com/VOICEVOX/voicevox_core/pull/1223
[#1224]: https://github.com/VOICEVOX/voicevox_core/pull/1224
[#1227]: https://github.com/VOICEVOX/voicevox_core/pull/1227
[#1236]: https://github.com/VOICEVOX/voicevox_core/pull/1236
[#1237]: https://github.com/VOICEVOX/voicevox_core/pull/1237
[#1238]: https://github.com/VOICEVOX/voicevox_core/pull/1238
[#1242]: https://github.com/VOICEVOX/voicevox_core/pull/1242
[#1244]: https://github.com/VOICEVOX/voicevox_core/pull/1244
[#1245]: https://github.com/VOICEVOX/voicevox_core/pull/1245
[#1246]: https://github.com/VOICEVOX/voicevox_core/pull/1246
[#1247]: https://github.com/VOICEVOX/voicevox_core/pull/1247
[#1250]: https://github.com/VOICEVOX/voicevox_core/pull/1250
[#1251]: https://github.com/VOICEVOX/voicevox_core/pull/1251
[#1252]: https://github.com/VOICEVOX/voicevox_core/pull/1252
[#1253]: https://github.com/VOICEVOX/voicevox_core/pull/1253
[#1255]: https://github.com/VOICEVOX/voicevox_core/pull/1255
[#1257]: https://github.com/VOICEVOX/voicevox_core/pull/1257
[#1260]: https://github.com/VOICEVOX/voicevox_core/pull/1260
[#1265]: https://github.com/VOICEVOX/voicevox_core/pull/1265
[#1266]: https://github.com/VOICEVOX/voicevox_core/pull/1266
[#1269]: https://github.com/VOICEVOX/voicevox_core/pull/1269
[#1276]: https://github.com/VOICEVOX/voicevox_core/pull/1276
[#1277]: https://github.com/VOICEVOX/voicevox_core/pull/1277
[#1278]: https://github.com/VOICEVOX/voicevox_core/pull/1278
[#1279]: https://github.com/VOICEVOX/voicevox_core/pull/1279
[#1280]: https://github.com/VOICEVOX/voicevox_core/pull/1280
[#1283]: https://github.com/VOICEVOX/voicevox_core/pull/1283
[#1285]: https://github.com/VOICEVOX/voicevox_core/pull/1285
[#1291]: https://github.com/VOICEVOX/voicevox_core/pull/1291
[#1295]: https://github.com/VOICEVOX/voicevox_core/pull/1295
[#1304]: https://github.com/VOICEVOX/voicevox_core/pull/1304
[#1313]: https://github.com/VOICEVOX/voicevox_core/pull/1313
[#1315]: https://github.com/VOICEVOX/voicevox_core/pull/1315
[#1323]: https://github.com/VOICEVOX/voicevox_core/pull/1323
[#1331]: https://github.com/VOICEVOX/voicevox_core/pull/1331
[#1337]: https://github.com/VOICEVOX/voicevox_core/pull/1337
[#1349]: https://github.com/VOICEVOX/voicevox_core/pull/1349
[#1350]: https://github.com/VOICEVOX/voicevox_core/pull/1350
[#1359]: https://github.com/VOICEVOX/voicevox_core/pull/1359

[VOICEVOX/onnxruntime-builder#25]: https://github.com/VOICEVOX/onnxruntime-builder/pull/25

[VOICEVOX/voicevox\_vvm]: https://github.com/VOICEVOX/voicevox_vvm
[VOICEVOX/voicevox\_vvm#1]: https://github.com/VOICEVOX/voicevox_vvm/pull/1
[VOICEVOX/voicevox\_vvm#5]: https://github.com/VOICEVOX/voicevox_vvm/pull/5
[VOICEVOX/voicevox\_vvm#9]: https://github.com/VOICEVOX/voicevox_vvm/pull/9
[VOICEVOX/voicevox\_vvm#12]: https://github.com/VOICEVOX/voicevox_vvm/pull/12
[VOICEVOX/voicevox\_vvm#14]: https://github.com/VOICEVOX/voicevox_vvm/pull/14
[VOICEVOX/voicevox\_vvm#19]: https://github.com/VOICEVOX/voicevox_vvm/issues/19
[VOICEVOX/voicevox\_vvm#21]: https://github.com/VOICEVOX/voicevox_vvm/pull/21
[VOICEVOX/voicevox\_vvm#22]: https://github.com/VOICEVOX/voicevox_vvm/pull/22
[VOICEVOX/voicevox\_vvm#23]: https://github.com/VOICEVOX/voicevox_vvm/pull/23
[VOICEVOX/voicevox\_vvm#30]: https://github.com/VOICEVOX/voicevox_vvm/pull/30
[VOICEVOX/voicevox\_vvm#31]: https://github.com/VOICEVOX/voicevox_vvm/pull/31
[VOICEVOX/voicevox\_vvm#33]: https://github.com/VOICEVOX/voicevox_vvm/pull/33
[VOICEVOX/voicevox\_vvm#34]: https://github.com/VOICEVOX/voicevox_vvm/pull/34

[Hiroshiba/vv\_core\_inference#12]: https://github.com/Hiroshiba/vv_core_inference/pull/12

[@char5742]: https://github.com/char5742
[@cm-ayf]: https://github.com/cm-ayf
[@eyr1n]: https://github.com/eyr1n
[@fuziki]: https://github.com/fuziki
[@higumachan]: https://github.com/higumachan
[@HyodaKazuaki]: https://github.com/HyodaKazuaki
[@misogihagi]: https://github.com/misogihagi
[@nanae772]: https://github.com/nanae772
[@nekomimimi]: https://github.com/nekomimimi
[@osakanataro]: https://github.com/osakanataro
[@Sanzentyo]: https://github.com/Sanzentyo
[@sh1ma]: https://github.com/sh1ma
[@shigobu]: https://github.com/shigobu
[@shuntia]: https://github.com/shuntia
[@weweweok]: https://github.com/weweweok
[@windymelt]: https://github.com/windymelt
[@yerrowTail]: https://github.com/yerrowTail
[@wappon28dev]: https://github.com/wappon28dev
