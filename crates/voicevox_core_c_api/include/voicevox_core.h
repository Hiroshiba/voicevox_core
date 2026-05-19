/**
 * @file voicevox_core.h
 *
 * 無料で使える中品質なテキスト読み上げソフトウェア、VOICEVOXのコア。
 *
 * <dl>
 *   <dt id="voicevox-core-availability">
 *     <a href="#voicevox-core-availability">Availability</a>
 *   </dt>
 *
 *   <dd>
 *     ヘッダによって次の二つのマクロのうちどちらかが存在する。[リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSでのみ`VOICEVOX_LINK_ONNXRUNTIME`が、他のプラットフォームでは`VOICEVOX_LOAD_ONNXRUNTIME`が存在する。
 *
 *     - `VOICEVOX_LOAD_ONNXRUNTIME`: ::voicevox_onnxruntime_load_once と、それに付属するアイテムが利用可能になる。
 *     - `VOICEVOX_LINK_ONNXRUNTIME`: ::voicevox_onnxruntime_init_once が利用可能になる。またこのマクロが存在するなら、このライブラリはONNX Runtimeをロード時動的リンクする。
 *   </dd>
 * </dl>

 * <dl>
 *   <dt id="voicevox-core-serialization">
 *     <a href="#voicevox-core-serialization">Serialization</a>
 *   </dt>
 *
 *   <dd>
 *     JSONの形式は[Rust APIのSerde実装]に準じており、おおむねVOICEVOX ENGINEと同じになることを目指している。ただし今後の破壊的変更にて変わる可能性がある。[データのシリアライゼーション]を参照。
 *
 *     [Rust APIのSerde実装]: ../rust_api/voicevox_core/__doc/Serde対応/index.html
 *     [データのシリアライゼーション]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/serialization.md
 *   </dd>
 * </dl>
 *
 * <dl>
 *   <dt id="voicevox-core-safety">
 *     <a href="#voicevox-core-safety">⚠️ Safety</a>
 *   </dt>
 *
 *   <dd>
 * このライブラリの利用にあたっては、いくつかの不変条件が守られている必要がある。本ドキュメントではこの不変条件を<b>安全性要件</b>(_safety
 * requirements_)と呼び、"Safety"というセクションの下に安全性要件を示す。
 *
 * 安全性要件の違反は[Rust言語における<b>未定義動作</b>(_undefined behavior_; 通称<em>UB</em>)](
 * https://doc.rust-lang.org/reference/behavior-considered-undefined.html)を引き起こす。Rustの未定義動作は、Cのそれや[C++のそれ](
 * https://cpprefjp.github.io/implementation-compliance.html#nasal-demon)や[Zigのそれ](
 * https://ziglang.org/documentation/0.10.1/#Undefined-Behavior)などとおおよそ同じであり、引き起こしてはならないものとされる。プログラム全体のどこかに未定義動作が含まれるなら、一般的に、処理系はそれについて何をしてもよい。[変数は同時にtrueでもfalseでもあってもよいし](
 * https://markshroyer.com/2012/06/c-both-true-and-false/)、あなたの鼻から悪魔が飛び出してもよい。このことは通称<b>鼻から悪魔</b>(_nasal
 * demons_)と呼ばれる。
 *
 * 未定義動作はプログラム全体に影響する。<b>運が良ければ</b>セグメンテーション違反などで異常終了するだけだが、ライブラリを呼び出している部分から離れた所で「鼻から悪魔」が起こることもある。そうなったら挙動の予測もデバッグも困難である。これが未定義動作が禁忌とされる所以である。
 *
 * `voicevox_core`全体における安全性要件は以下の通りである。
 *
 * - 「読み込みについて有効」と説明されているポインタは次の条件を満たしていなければならない。
 *     - <b>間接参照可能</b>(_dereferenceable_)である。
 *     - 参照先のメモリは他スレッドから書き込み中ではない。
 * - 「書き込みについて有効」と説明されているポインタは次の条件を満たしていなければならない。
 *     - 参照先のメモリは有効である (ただしメモリアラインメントに沿っている必要は無い)。
 *     - 参照先のメモリは他スレッドからアクセス中ではない。
 * - このライブラリで生成したオブジェクトの解放は、このライブラリが提供するAPIで行わなくてはならない(<code>free</code>や<code>HeapFree</code>で行ってはならない)。
 *
 * 次のことに注意すること。
 *
 * - 次のポインタは読み込みにおいても書き込みにおいても有効ではない。
 *     - ヌルポインタ。
 *     - 解放されたポインタ。
 * - voicevox_coreをアンロードする場合、voicevox_coreが生成したポインタが有効であり続けることは保証されない。
 *   </dd>
 * </dl>
 */

#ifndef VOICEVOX_CORE_INCLUDE_GUARD
#define VOICEVOX_CORE_INCLUDE_GUARD

/* Generated with cbindgen:0.28.0 */

#ifdef __cplusplus
#include <cstdint>
#else // __cplusplus
#include <stdbool.h>
#include <stdint.h>
#endif // __cplusplus

//#define VOICEVOX_LINK_ONNXRUNTIME
//#define VOICEVOX_LOAD_ONNXRUNTIME

#if !(defined(VOICEVOX_LINK_ONNXRUNTIME) || defined(VOICEVOX_LOAD_ONNXRUNTIME))
#error "either `VOICEVOX_LINK_ONNXRUNTIME` or `VOICEVOX_LOAD_ONNXRUNTIME` must be enabled"
#endif

#if defined(VOICEVOX_LINK_ONNXRUNTIME) && defined(VOICEVOX_LOAD_ONNXRUNTIME)
#error "`VOICEVOX_LINK_ONNXRUNTIME` or `VOICEVOX_LOAD_ONNXRUNTIME` cannot be enabled at the same time"
#endif

/**
 * ハードウェアアクセラレーションモードを設定する設定値。
 *
 * \orig-impl{VoicevoxAccelerationMode}
 */
enum VoicevoxAccelerationMode
#ifdef __cplusplus
  : int32_t
#endif // __cplusplus
 {
  /**
   * 実行環境に合った適切なハードウェアアクセラレーションモードを選択する
   */
  VOICEVOX_ACCELERATION_MODE_AUTO = 0,
  /**
   * ハードウェアアクセラレーションモードを"CPU"に設定する
   */
  VOICEVOX_ACCELERATION_MODE_CPU = 1,
  /**
   * ハードウェアアクセラレーションモードを"GPU"に設定する
   */
  VOICEVOX_ACCELERATION_MODE_GPU = 2,
};
#ifndef __cplusplus
typedef int32_t VoicevoxAccelerationMode;
#endif // __cplusplus

/**
 * ::voicevox_synthesizer_load_voice_model の実行時に、同じIDの ::VoicevoxVoiceModelFile が既に読み込まれていたときのふるまい。
 *
 * \orig-impl{VoicevoxOnExistingVoiceModelId}
 */
enum VoicevoxOnExistingVoiceModelId
#ifdef __cplusplus
  : int32_t
#endif // __cplusplus
 {
  /**
   * エラーにする。デフォルトのふるまい
   */
  VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_ERROR = 0,
  /**
   * 再読み込みする。VOICEVOX COREでは、長文のテキストを一度に音声合成するとCPU/GPUメモリが大量に占有されたままになる。再読み込みを行うとメモリの使用量が元に戻る
   */
  VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_RELOAD = 1,
  /**
   * 何もしない
   */
  VOICEVOX_ON_EXISTING_VOICE_MODEL_ID_SKIP = 2,
};
#ifndef __cplusplus
typedef int32_t VoicevoxOnExistingVoiceModelId;
#endif // __cplusplus

/**
 * 処理結果を示す結果コード。
 *
 * \orig-impl{VoicevoxResultCode,C APIにしか無いものがあることに注意。}
 */
enum VoicevoxResultCode
#ifdef __cplusplus
  : int32_t
#endif // __cplusplus
 {
  /**
   * 成功
   */
  VOICEVOX_RESULT_OK = 0,
  /**
   * open_jtalk辞書ファイルが読み込まれていない
   */
  VOICEVOX_RESULT_NOT_LOADED_OPENJTALK_DICT_ERROR = 1,
  /**
   * サポートされているデバイス情報取得に失敗した
   */
  VOICEVOX_RESULT_GET_SUPPORTED_DEVICES_ERROR = 3,
  /**
   * GPUモードがサポートされていない
   */
  VOICEVOX_RESULT_GPU_SUPPORT_ERROR = 4,
  /**
   * 推論ライブラリのロードまたは初期化ができなかった
   */
  VOICEVOX_RESULT_INIT_INFERENCE_RUNTIME_ERROR = 29,
  /**
   * スタイルIDに対するスタイルが見つからなかった
   */
  VOICEVOX_RESULT_STYLE_NOT_FOUND_ERROR = 6,
  /**
   * 音声モデルIDに対する音声モデルが見つからなかった
   */
  VOICEVOX_RESULT_MODEL_NOT_FOUND_ERROR = 7,
  /**
   * 推論に失敗した、もしくは推論結果が異常
   */
  VOICEVOX_RESULT_RUN_MODEL_ERROR = 8,
  /**
   * 入力テキストの解析に失敗した
   */
  VOICEVOX_RESULT_ANALYZE_TEXT_ERROR = 11,
  /**
   * 無効なutf8文字列が入力された
   */
  VOICEVOX_RESULT_INVALID_UTF8_INPUT_ERROR = 12,
  /**
   * AquesTalk風記法のテキストの解析に失敗した
   */
  VOICEVOX_RESULT_PARSE_KANA_ERROR = 13,
  /**
   * 無効なAudioQuery
   */
  VOICEVOX_RESULT_INVALID_AUDIO_QUERY_ERROR = 14,
  /**
   * 無効なAccentPhrase
   */
  VOICEVOX_RESULT_INVALID_ACCENT_PHRASE_ERROR = 15,
  /**
   * ZIPファイルを開くことに失敗した
   */
  VOICEVOX_RESULT_OPEN_ZIP_FILE_ERROR = 16,
  /**
   * ZIP内のファイルが読めなかった
   */
  VOICEVOX_RESULT_READ_ZIP_ENTRY_ERROR = 17,
  /**
   * モデルの形式が不正
   */
  VOICEVOX_RESULT_INVALID_MODEL_HEADER_ERROR = 28,
  /**
   * すでに読み込まれている音声モデルを読み込もうとした
   */
  VOICEVOX_RESULT_MODEL_ALREADY_LOADED_ERROR = 18,
  /**
   * すでに読み込まれているスタイルを読み込もうとした
   */
  VOICEVOX_RESULT_STYLE_ALREADY_LOADED_ERROR = 26,
  /**
   * 無効なモデルデータ
   */
  VOICEVOX_RESULT_INVALID_MODEL_DATA_ERROR = 27,
  /**
   * ユーザー辞書を読み込めなかった
   */
  VOICEVOX_RESULT_LOAD_USER_DICT_ERROR = 20,
  /**
   * ユーザー辞書を書き込めなかった
   */
  VOICEVOX_RESULT_SAVE_USER_DICT_ERROR = 21,
  /**
   * ユーザー辞書に単語が見つからなかった
   */
  VOICEVOX_RESULT_USER_DICT_WORD_NOT_FOUND_ERROR = 22,
  /**
   * OpenJTalkのユーザー辞書の設定に失敗した
   */
  VOICEVOX_RESULT_USE_USER_DICT_ERROR = 23,
  /**
   * ユーザー辞書の単語のバリデーションに失敗した
   */
  VOICEVOX_RESULT_INVALID_USER_DICT_WORD_ERROR = 24,
  /**
   * UUIDの変換に失敗した
   */
  VOICEVOX_RESULT_INVALID_UUID_ERROR = 25,
  /**
   * 無効なMora
   */
  VOICEVOX_RESULT_INVALID_MORA_ERROR = 30,
  /**
   * 無効な楽譜
   */
  VOICEVOX_RESULT_INVALID_SCORE_ERROR = 31,
  /**
   * 無効なノート
   */
  VOICEVOX_RESULT_INVALID_NOTE_ERROR = 32,
  /**
   * 無効なFrameAudioQuery
   */
  VOICEVOX_RESULT_INVALID_FRAME_AUDIO_QUERY_ERROR = 33,
  /**
   * 無効なFramePhoneme
   */
  VOICEVOX_RESULT_INVALID_FRAME_PHONEME_ERROR = 34,
  /**
   * 楽譜とFrameAudioQueryの組み合わせが不正
   */
  VOICEVOX_RESULT_INCOMPATIBLE_QUERIES_ERROR = 35,
};
#ifndef __cplusplus
typedef int32_t VoicevoxResultCode;
#endif // __cplusplus

/**
 * ユーザー辞書の単語の種類。
 *
 * \orig-impl{VoicevoxUserDictWordType}
 */
enum VoicevoxUserDictWordType
#ifdef __cplusplus
  : int32_t
#endif // __cplusplus
 {
  /**
   * 固有名詞。
   */
  VOICEVOX_USER_DICT_WORD_TYPE_PROPER_NOUN = 0,
  /**
   * 一般名詞。
   */
  VOICEVOX_USER_DICT_WORD_TYPE_COMMON_NOUN = 1,
  /**
   * 動詞。
   */
  VOICEVOX_USER_DICT_WORD_TYPE_VERB = 2,
  /**
   * 形容詞。
   */
  VOICEVOX_USER_DICT_WORD_TYPE_ADJECTIVE = 3,
  /**
   * 接尾辞。
   */
  VOICEVOX_USER_DICT_WORD_TYPE_SUFFIX = 4,
};
#ifndef __cplusplus
typedef int32_t VoicevoxUserDictWordType;
#endif // __cplusplus

/**
 * テキスト解析器としてのOpen JTalk。
 *
 * <b>構築</b>(_construction_)は ::voicevox_open_jtalk_rc_new で行い、<b>破棄</b>(_destruction_)は ::voicevox_open_jtalk_rc_delete で行う。
 *
 * 参照カウント方式のスマートポインタ(reference-counted smart pointer)であり、
 * ::voicevox_synthesizer_new に渡されるときには参照カウンタがインクリメントされる形でオブジェクトの共有が行われる。
 *
 * \example{
 * ```c
 * OpenJtalkRc *open_jtalk;
 * voicevox_open_jtalk_rc_new("./open_jtalk_dic_utf_8-1.11", &open_jtalk);
 * // ⋮
 * voicevox_open_jtalk_rc_delete(open_jtalk);
 * ```
 * }
 *
 * \orig-impl{OpenJtalkRc}
 */
typedef struct OpenJtalkRc OpenJtalkRc;

/**
 * ONNX Runtime。
 *
 * シングルトンであり、インスタンスは高々一つ。
 *
 * ```c
 * const VoicevoxOnnxruntime *ort1;
 * voicevox_onnxruntime_load_once(
 *     voicevox_make_default_load_onnxruntime_options(), &ort1);
 * const VoicevoxOnnxruntime *ort2 = voicevox_onnxruntime_get();
 * assert(ort1 == ort2);
 * ```
 *
 * \orig-impl{VoicevoxOnnxruntime}
 */
typedef struct VoicevoxOnnxruntime VoicevoxOnnxruntime;

/**
 * 音声シンセサイザ。
 *
 * <b>構築</b>(_construction_)は ::voicevox_synthesizer_new で行い、<b>破棄</b>(_destruction_)は ::voicevox_synthesizer_delete で行う。
 *
 * \orig-impl{VoicevoxSynthesizer}
 */
typedef struct VoicevoxSynthesizer VoicevoxSynthesizer;

/**
 * ユーザー辞書。
 *
 * \orig-impl{VoicevoxUserDict}
 */
typedef struct VoicevoxUserDict VoicevoxUserDict;

/**
 * 音声モデルファイル。
 *
 * VVMファイルと対応する。
 * <b>構築</b>(_construction_)は ::voicevox_voice_model_file_open で行い、<b>破棄</b>(_destruction_)は ::voicevox_voice_model_file_delete で行う。
 *
 * \orig-impl{VoicevoxVoiceModelFile}
 */
typedef struct VoicevoxVoiceModelFile VoicevoxVoiceModelFile;

#if defined(VOICEVOX_LOAD_ONNXRUNTIME)
/**
 * ::voicevox_onnxruntime_load_once のオプション。
 *
 * \availability{
 *   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
 * }
 *
 * \no-orig-impl{VoicevoxLoadOnnxruntimeOptions}
 */
typedef struct VoicevoxLoadOnnxruntimeOptions {
  /**
   * ONNX Runtimeのファイル名（モジュール名）もしくはファイルパスを指定する。
   *
   * `dlopen`/[`LoadLibraryExW`](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryexw)の引数に使われる。デフォルトは ::voicevox_get_onnxruntime_lib_versioned_filename と同じ。
   */
  const char *filename;
} VoicevoxLoadOnnxruntimeOptions;
#endif

/**
 * ::voicevox_synthesizer_new のオプション。
 *
 * \no-orig-impl{VoicevoxInitializeOptions}
 */
typedef struct VoicevoxInitializeOptions {
  /**
   * ハードウェアアクセラレーションモード
   */
  VoicevoxAccelerationMode acceleration_mode;
  /**
   * CPU利用数を指定
   * 0を指定すると環境に合わせたCPUが利用される
   */
  uint16_t cpu_num_threads;
} VoicevoxInitializeOptions;

/**
 * ::voicevox_synthesizer_load_voice_model のオプション。
 *
 * \no-orig-impl{VoicevoxLoadVoiceModelOptions}
 */
typedef struct VoicevoxLoadVoiceModelOptions {
  /**
   * 同じIDの ::VoicevoxVoiceModelFile が既に読み込まれていたときのふるまい
   */
  VoicevoxOnExistingVoiceModelId on_existing;
} VoicevoxLoadVoiceModelOptions;

/**
 * 音声モデルID。
 *
 * ::VoicevoxSynthesizer はこのIDをキーとして、音声モデルのロード・アンロードを行う。
 *
 * 同じIDを持つ複数のVVMファイルがあるときは、ファイルとして新しい方を常に使うことが推奨される。[VOICEVOX/voicevox_vvm]で管理されているVVMでは、次の方針が取られている。
 *
 * - VVMに含まれる声が変化せず、軽微な修正のみのときはIDを使い回してリリースする。
 * - VVMに含まれる声が明確に変化するかもしくは削除されるような実質的な変更のときは、新しいIDを割り振ってリリースする。
 *
 * これ以外は未定であり、更なるルールについては[VOICEVOX/voicevox_vvm#19]で議論される予定。
 *
 * \orig-impl{VoicevoxVoiceModelId}
 *
 * [VOICEVOX/voicevox_vvm]: https://github.com/VOICEVOX/voicevox_vvm
 * [VOICEVOX/voicevox_vvm#19]: https://github.com/VOICEVOX/voicevox_vvm/issues/19
 */
typedef const uint8_t (*VoicevoxVoiceModelId)[16];

/**
 * スタイルID。
 *
 * VOICEVOXにおける、ある<i>キャラクター</i>のある<i>スタイル</i>を指す。
 *
 * \orig-impl{VoicevoxStyleId}
 */
typedef uint32_t VoicevoxStyleId;

/**
 * ::voicevox_synthesizer_synthesis のオプション。
 *
 * \no-orig-impl{VoicevoxSynthesisOptions}
 */
typedef struct VoicevoxSynthesisOptions {
  /**
   * 疑問文の調整を有効にする
   */
  bool enable_interrogative_upspeak;
} VoicevoxSynthesisOptions;

/**
 * ::voicevox_synthesizer_tts のオプション。
 *
 * \no-orig-impl{VoicevoxTtsOptions}
 */
typedef struct VoicevoxTtsOptions {
  /**
   * 疑問文の調整を有効にする
   */
  bool enable_interrogative_upspeak;
} VoicevoxTtsOptions;

/**
 * ユーザー辞書の単語。
 *
 * \orig-impl{VoicevoxUserDictWord}
 */
typedef struct VoicevoxUserDictWord {
  /**
   * 表記
   */
  const char *surface;
  /**
   * 読み
   */
  const char *pronunciation;
  /**
   * アクセント型
   */
  uintptr_t accent_type;
  /**
   * 単語の種類
   */
  VoicevoxUserDictWordType word_type;
  /**
   * 優先度
   */
  uint32_t priority;
} VoicevoxUserDictWord;

#ifdef __cplusplus
extern "C" {
#endif // __cplusplus

#if defined(VOICEVOX_LOAD_ONNXRUNTIME)
/**
 * ONNX Runtimeの動的ライブラリの、バージョン付きのファイル名。
 *
 * WindowsとAndroidでは ::voicevox_get_onnxruntime_lib_unversioned_filename と同じ。
 *
 * \availability{
 *   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
 * }
 *
 * \orig-impl{voicevox_get_onnxruntime_lib_versioned_filename}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
const char *voicevox_get_onnxruntime_lib_versioned_filename(void);
#endif

#if defined(VOICEVOX_LOAD_ONNXRUNTIME)
/**
 * ONNX Runtimeの動的ライブラリの、バージョン無しのファイル名。
 *
 * \availability{
 *   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
 * }
 *
 * \orig-impl{voicevox_get_onnxruntime_lib_unversioned_filename}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
const char *voicevox_get_onnxruntime_lib_unversioned_filename(void);
#endif

#if defined(VOICEVOX_LOAD_ONNXRUNTIME)
/**
 * デフォルトの ::voicevox_onnxruntime_load_once のオプションを生成する。
 *
 * @return デフォルトの ::voicevox_onnxruntime_load_once のオプション
 *
 * \availability{
 *   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
 * }
 *
 * \no-orig-impl{voicevox_make_default_load_onnxruntime_options}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
struct VoicevoxLoadOnnxruntimeOptions voicevox_make_default_load_onnxruntime_options(void);
#endif

/**
 * ::VoicevoxOnnxruntime のインスタンスが既に作られているならそれを得る。
 *
 * 作られていなければ`NULL`を返す。
 *
 * @returns ::VoicevoxOnnxruntime のインスタンス
 *
 * \orig-impl{voicevox_onnxruntime_get}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
const struct VoicevoxOnnxruntime *voicevox_onnxruntime_get(void);

#if defined(VOICEVOX_LOAD_ONNXRUNTIME)
/**
 * ONNX Runtimeをロードして初期化する。
 *
 * 一度成功したら、以後は引数を無視して同じ参照を返す。
 *
 * @param [in] options オプション
 * @param [out] out_onnxruntime ::VoicevoxOnnxruntime のインスタンス
 *
 * @returns 結果コード
 *
 * \availability{
 *   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSを除くプラットフォームで利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
 * }
 *
 * \safety{
 * - `options.filename`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `out_onnxruntime`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_onnxruntime_load_once}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_onnxruntime_load_once(struct VoicevoxLoadOnnxruntimeOptions options,
                                                  const struct VoicevoxOnnxruntime **out_onnxruntime);
#endif

#if defined(VOICEVOX_LINK_ONNXRUNTIME)
/**
 * ONNX Runtimeを初期化する。
 *
 * 一度成功したら以後は同じ参照を返す。
 *
 * @param [out] out_onnxruntime ::VoicevoxOnnxruntime のインスタンス
 *
 * @returns 結果コード
 *
 * \availability{
 *   [リリース](https://github.com/voicevox/voicevox_core/releases)されているライブラリではiOSでのみ利用可能。詳細は<a href="#voicevox-core-availability">ファイルレベルの"Availability"の節</a>を参照。
 * }
 *
 * \safety{
 * - `out_onnxruntime`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_onnxruntime_init_once}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_onnxruntime_init_once(const struct VoicevoxOnnxruntime **out_onnxruntime);
#endif

/**
 * ::OpenJtalkRc を<b>構築</b>(_construct_)する。
 *
 * 解放は ::voicevox_open_jtalk_rc_delete で行う。
 *
 * @param [in] open_jtalk_dic_dir 辞書ディレクトリを指すUTF-8のパス
 * @param [out] out_open_jtalk 構築先
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * OpenJtalkRc *open_jtalk;
 * voicevox_open_jtalk_rc_new("./open_jtalk_dic_utf_8-1.11", &open_jtalk);
 * ```
 * }
 *
 * \safety{
 * - `open_jtalk_dic_dir`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `out_open_jtalk`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_open_jtalk_rc_new}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_open_jtalk_rc_new(const char *open_jtalk_dic_dir,
                                              struct OpenJtalkRc **out_open_jtalk);

/**
 * OpenJtalkの使うユーザー辞書を設定する。
 *
 * この関数を呼び出した後にユーザー辞書を変更した場合、再度この関数を呼び出す必要がある。
 *
 * @param [in] open_jtalk Open JTalkのオブジェクト
 * @param [in] user_dict ユーザー辞書
 *
 * \orig-impl{voicevox_open_jtalk_rc_use_user_dict}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_open_jtalk_rc_use_user_dict(const struct OpenJtalkRc *open_jtalk,
                                                        const struct VoicevoxUserDict *user_dict);

/**
 * 日本語のテキストを解析する。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * @param [in] open_jtalk Open JTalkのオブジェクト
 * @param [in] text UTF-8の日本語テキスト
 * @param [out] output_accent_phrases_json 生成先
 *
 * \orig-impl{voicevox_open_jtalk_rc_use_user_dict}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_open_jtalk_rc_analyze(const struct OpenJtalkRc *open_jtalk,
                                                  const char *text,
                                                  char **output_accent_phrases_json);

/**
 * ::OpenJtalkRc を<b>破棄</b>(_destruct_)する。
 *
 * 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
 *
 * この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
 *
 * @param [in] open_jtalk 破棄対象。nullable
 *
 * \example{
 * ```c
 * voicevox_open_jtalk_rc_delete(open_jtalk);
 * ```
 * }
 *
 * \no-orig-impl{voicevox_open_jtalk_rc_delete}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
void voicevox_open_jtalk_rc_delete(struct OpenJtalkRc *open_jtalk);

/**
 * デフォルトの初期化オプションを生成する
 * @return デフォルト値が設定された初期化オプション
 *
 * \no-orig-impl{voicevox_make_default_initialize_options}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
struct VoicevoxInitializeOptions voicevox_make_default_initialize_options(void);

/**
 * voicevoxのバージョンを取得する。
 * @return SemVerでフォーマットされたバージョン。
 *
 * \orig-impl{voicevox_get_version}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
const char *voicevox_get_version(void);

/**
 * AccentPhraseの配列からAudioQueryを作る。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
 * @param [out] output_accent_phrases_json 生成先
 *
 * \safety{
 * - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_audio_query_create_from_accent_phrases}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_audio_query_create_from_accent_phrases(const char *accent_phrases_json,
                                                                   char **output_audio_query_json);

/**
 * 与えられたJSONが`AudioQuery`型として不正であるときエラーを返す。
 *
 * 不正であるとは、以下のいずれかの条件を満たすことである。
 *
 * - [Rust APIの`AudioQuery`型]としてデシリアライズ不可、もしくはJSONとして不正。
 * - `accent_phrases`の要素のうちいずれかが、 ::voicevox_accent_phrase_validate でエラーになる。
 * - `outputSamplingRate`が`24000`の倍数ではない、もしくは`0` (将来的に解消予定。cf. [#762])。
 *
 * [Rust APIの`AudioQuery`型]: ../rust_api/voicevox_core/struct.AudioQuery.html
 * [#762]: https://github.com/VOICEVOX/voicevox_core/issues/762
 *
 * 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
 *
 * - `accent_phrases`の要素のうちいずれかが警告が出る状態。
 * - `speedScale`が負。
 * - `volumeScale`が負。
 * - `prePhonemeLength`が負。
 * - `postPhonemeLength`が負。
 * - `outputSamplingRate`が`24000`以外の値（エラーと同様将来的に解消予定）。
 *
 * @param [in] audio_query_json `AudioQuery`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_AUDIO_QUERY_ERROR
 *
 * \safety{
 * - `audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_audio_query_validate}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_audio_query_validate(const char *audio_query_json);

/**
 * 与えられたJSONが`AccentPhrase`型として不正であるときエラーを返す。
 *
 * 不正であるとは、以下のいずれかの条件を満たすことである。
 *
 * - [Rust APIの`AccentPhrase`型]としてデシリアライズ不可、もしくはJSONとして不正。
 * - `moras`もしくは`pause_mora`の要素のうちいずれかが、 ::voicevox_mora_validate でエラーになる。
 * - `accent`が`0`。
 *
 * [Rust APIの`AccentPhrase`型]: ../rust_api/voicevox_core/struct.AccentPhrase.html
 *
 * 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
 *
 * - `moras`もしくは`pause_mora`の要素のうちいずれかが、警告が出る状態。
 * - `accent`が`moras`の数を超過している。
 *
 * @param [in] accent_phrase_json `AccentPhrase`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_ACCENT_PHRASE_ERROR
 *
 * \safety{
 * - `accent_phrase_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_accent_phrase_validate}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_accent_phrase_validate(const char *accent_phrase_json);

/**
 * 与えられたJSONが`Mora`型として不正であるときエラーを返す。
 *
 * 不正であるとは、以下のいずれかの条件を満たすことである。
 *
 * - [Rust APIの`Mora`型]としてデシリアライズ不可、もしくはJSONとして不正。
 * - `consonant`と`consonant_length`の有無が不一致。
 * - `consonant`が子音以外の音素であるか、もしくは音素として不正。
 * - `vowel`が子音であるか、もしくは音素として不正。
 *
 * [Rust APIの`Mora`型]: ../rust_api/voicevox_core/struct.Mora.html
 *
 * 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
 *
 * - `consonant_length`が負。
 * - `vowel_length`が負。
 *
 * @param [in] mora_json `Mora`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_MORA_ERROR
 *
 * \safety{
 * - `mora_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_mora_validate}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_mora_validate(const char *mora_json);

/**
 * 与えられたJSONが`Score`型として不正であるときエラーを返す。
 *
 * 不正であるとは、以下のいずれかの条件を満たすことである。
 *
 * - [Rust APIの`Score`型]としてデシリアライズ不可、もしくはJSONとして不正。
 * - `notes`の要素のうちいずれかが、 ::voicevox_note_validate でエラーになる。
 * - `notes`が空であるか、もしくは先頭が音符。
 *
 * [Rust APIの`Score`型]: ../rust_api/voicevox_core/struct.Score.html
 *
 * @param [in] score_json `Score`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_SCORE_ERROR
 *
 * \safety{
 * - `score_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_score_validate}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_score_validate(const char *score_json);

/**
 * 与えられたJSONが`Note`型として不正であるときエラーを返す。
 *
 * 不正であるとは、以下のいずれかの条件を満たすことである。
 *
 * - [Rust APIの`Note`型]としてデシリアライズ不可、もしくはJSONとして不正。
 * - `key`が`null`かつ`lyric`が`""`以外。
 * - `key`が非`null`かつ`lyric`が`""`。
 *
 * [Rust APIの`Note`型]: ../rust_api/voicevox_core/struct.Note.html
 *
 * @param [in] note_json `Note`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_NOTE_ERROR
 *
 * \safety{
 * - `note_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_note_validate}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_note_validate(const char *note_json);

/**
 * 与えられたJSONが`FrameAudioQuery`型として不正であるときエラーを返す。
 *
 * 不正であるとは、以下の条件を満たすことである。
 *
 * - [Rust APIの`FrameAudioQuery`型]としてデシリアライズ不可、もしくはJSONとして不正。
 *
 * [Rust APIの`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
 *
 * 次の状態に対しては警告のログを出す。将来的にはエラーになる予定。
 *
 * - `outputSamplingRate`が`24000`以外の値（将来的に解消予定）。
 *
 * @param [in] frame_audio_query_json `FrameAudioQuery`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_FRAME_AUDIO_QUERY_ERROR
 *
 * \safety{
 * - `frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_frame_audio_query_validate}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_frame_audio_query_validate(const char *frame_audio_query_json);

/**
 * 与えられたJSONが`FramePhoneme`型として不正であるときエラーを返す。
 *
 * 不正であるとは、以下の条件を満たすことである。
 *
 * - [Rust APIの`FramePhoneme`型]としてデシリアライズ不可、もしくはJSONとして不正。
 *
 * [Rust APIの`FramePhoneme`型]: ../rust_api/voicevox_core/struct.FramePhoneme.html
 *
 * @param [in] frame_phoneme_json `FramePhoneme`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_FRAME_PHONEME_ERROR
 *
 * \safety{
 * - `frame_phoneme_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \no-orig-impl{voicevox_frame_phoneme_validate}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_frame_phoneme_validate(const char *frame_phoneme_json);

/**
 * 与えられた楽譜と歌唱合成用のクエリの組み合わせが、基本周波数と音量の生成に利用できるかどうかを確認する。
 *
 * 次のうちどれかを満たすならエラーを返す。
 *
 * - `score_json`が ::voicevox_score_validate でエラーになる。
 * - `frame_audio_query_json`が ::voicevox_frame_audio_query_validate でエラーになる。
 * - `notes`が表す音素ID列と、`phonemes`が表す音素ID列が等しくない。ただし異なる音素の表現が同一のIDを表すことがある。
 *
 * @param [in] score_json `Score`型のJSON
 * @param [in] frame_audio_query_json `FrameAudioQuery`型のJSON
 *
 * @returns 成功時には ::VOICEVOX_RESULT_OK 、失敗時には ::VOICEVOX_RESULT_INVALID_SCORE_ERROR, ::VOICEVOX_RESULT_INVALID_FRAME_AUDIO_QUERY_ERROR, ::VOICEVOX_RESULT_INCOMPATIBLE_QUERIES_ERROR
 *
 * \safety{
 * - `score_json`と`frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 * \orig-impl{voicevox_ensure_compatible}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_ensure_compatible(const char *score_json,
                                              const char *frame_audio_query_json);

/**
 * VVMファイルを開く。
 *
 * @param [in] path vvmファイルへのUTF-8のファイルパス
 * @param [out] out_model 構築先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `path`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `out_model`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_voice_model_file_open}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_voice_model_file_open(const char *path,
                                                  struct VoicevoxVoiceModelFile **out_model);

/**
 * ::VoicevoxVoiceModelFile からIDを取得する。
 *
 * @param [in] model 音声モデル
 * @param [out] output_voice_model_id 音声モデルID。詳細は ::VoicevoxVoiceModelId
 *
 * \safety{
 * - `output_voice_model_id`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_voice_model_file_id}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
void voicevox_voice_model_file_id(const struct VoicevoxVoiceModelFile *model,
                                  uint8_t (*output_voice_model_id)[16]);

/**
 * ::VoicevoxVoiceModelFile からメタ情報を取得する。
 *
 * JSONの解放は ::voicevox_json_free で行う。
 *
 * @param [in] model 音声モデル
 *
 * @returns メタ情報のJSON文字列
 *
 * \orig-impl{voicevox_voice_model_file_create_metas_json}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
char *voicevox_voice_model_file_create_metas_json(const struct VoicevoxVoiceModelFile *model);

/**
 * ::VoicevoxVoiceModelFile を、所有しているファイルディスクリプタを閉じた上で<b>破棄</b>(_destruct_)する。ファイルの削除(_delete_)<b>ではない</b>。
 *
 * 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
 *
 * この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
 *
 * @param [in] model 破棄対象。nullable
 *
 * \no-orig-impl{voicevox_voice_model_file_delete}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
void voicevox_voice_model_file_delete(struct VoicevoxVoiceModelFile *model);

/**
 * ::VoicevoxSynthesizer を<b>構築</b>(_construct_)する。
 *
 * @param [in] onnxruntime
 * @param [in] open_jtalk Open JTalkのオブジェクト
 * @param [in] options オプション
 * @param [out] out_synthesizer 構築先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `onnxruntime`は ::voicevox_onnxruntime_load_once または ::voicevox_onnxruntime_init_once で得たものでなければならない。
 * - `out_synthesizer`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_new}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_new(const struct VoicevoxOnnxruntime *onnxruntime,
                                            const struct OpenJtalkRc *open_jtalk,
                                            struct VoicevoxInitializeOptions options,
                                            struct VoicevoxSynthesizer **out_synthesizer);

/**
 * ::VoicevoxSynthesizer を<b>破棄</b>(_destruct_)する。
 *
 * 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
 *
 * この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
 *
 * @param [in] synthesizer 破棄対象。nullable
 *
 * \no-orig-impl{voicevox_synthesizer_delete}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
void voicevox_synthesizer_delete(struct VoicevoxSynthesizer *synthesizer);

/**
 * デフォルトの `voicevox_synthesizer_load_voice_model` のオプションを生成する
 * @return デフォルト値が設定された `voicevox_synthesizer_load_voice_model` のオプション
 *
 * \no-orig-impl{voicevox_make_default_load_voice_model_options}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
struct VoicevoxLoadVoiceModelOptions voicevox_make_default_load_voice_model_options(void);

/**
 * 音声モデルを読み込む。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] model 音声モデル
 * @param [in] options オプション
 *
 * @returns 結果コード
 *
 * \orig-impl{voicevox_synthesizer_load_voice_model}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_load_voice_model(const struct VoicevoxSynthesizer *synthesizer,
                                                         const struct VoicevoxVoiceModelFile *model,
                                                         struct VoicevoxLoadVoiceModelOptions options);

/**
 * 音声モデルの読み込みを解除する。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] model_id 音声モデルID
 *
 * @returns 結果コード
 *
 * \safety{
 * - `model_id`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_unload_voice_model}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_unload_voice_model(const struct VoicevoxSynthesizer *synthesizer,
                                                           VoicevoxVoiceModelId model_id);

/**
 * ::VoicevoxOnnxruntime のインスタンスを得る。
 *
 * @param [in] synthesizer 音声シンセサイザ
 *
 * @returns ::VoicevoxOnnxruntime のインスタンス
 *
 * \orig-impl{voicevox_synthesizer_get_onnxruntime}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
const struct VoicevoxOnnxruntime *voicevox_synthesizer_get_onnxruntime(const struct VoicevoxSynthesizer *synthesizer);

/**
 * ハードウェアアクセラレーションがGPUモードか判定する。
 *
 * @param [in] synthesizer 音声シンセサイザ
 *
 * @returns GPUモードかどうか
 *
 * \orig-impl{voicevox_synthesizer_is_gpu_mode}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
bool voicevox_synthesizer_is_gpu_mode(const struct VoicevoxSynthesizer *synthesizer);

/**
 * 指定したIDの音声モデルが読み込まれているか判定する。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] model_id 音声モデルID
 *
 * @returns モデルが読み込まれているかどうか
 *
 * \safety{
 * - `model_id`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_is_loaded_voice_model}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
bool voicevox_synthesizer_is_loaded_voice_model(const struct VoicevoxSynthesizer *synthesizer,
                                                VoicevoxVoiceModelId model_id);

/**
 * 今読み込んでいる音声モデルのメタ情報を、JSONで取得する。
 *
 * JSONの解放は ::voicevox_json_free で行う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 *
 * @return メタ情報のJSON文字列
 *
 * \orig-impl{voicevox_synthesizer_create_metas_json}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
char *voicevox_synthesizer_create_metas_json(const struct VoicevoxSynthesizer *synthesizer);

/**
 * ONNX Runtimeとして利用可能なデバイスの情報を、JSONで取得する。
 *
 * JSONの解放は ::voicevox_json_free で行う。
 *
 * あくまでONNX Runtimeが対応しているデバイスの情報であることに注意。GPUが使える環境ではなかったとしても`cuda`や`dml`は`true`を示しうる。
 *
 * @param [in] onnxruntime
 * @param [out] output_supported_devices_json サポートデバイス情報のJSON文字列
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * char *supported_devices;
 * VoicevoxResultCode result = voicevox_onnxruntime_create_supported_devices_json(onnxruntime, &supported_devices);
 * ```
 * }
 *
 * \safety{
 * - `onnxruntime`は ::voicevox_onnxruntime_load_once または ::voicevox_onnxruntime_init_once で得たものでなければならない。
 * - `output_supported_devices_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_onnxruntime_create_supported_devices_json}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_onnxruntime_create_supported_devices_json(const struct VoicevoxOnnxruntime *onnxruntime,
                                                                      char **output_supported_devices_json);

/**
 * AquesTalk風記法から、AudioQueryをJSONとして生成する。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] kana AquesTalk風記法
 * @param [in] style_id スタイルID
 * @param [out] output_audio_query_json 生成先
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * char *audio_query;
 * voicevox_synthesizer_create_audio_query_from_kana(synthesizer, "コンニチワ'",
 *                                                   2, // "四国めたん (ノーマル)"
 *                                                   &audio_query);
 * ```
 * }
 *
 * \safety{
 * - `kana`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_create_audio_query_from_kana}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_create_audio_query_from_kana(const struct VoicevoxSynthesizer *synthesizer,
                                                                     const char *kana,
                                                                     VoicevoxStyleId style_id,
                                                                     char **output_audio_query_json);

/**
 * 日本語テキストから、AudioQueryをJSONとして生成する。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * ::voicevox_synthesizer_create_accent_phrases と ::voicevox_audio_query_create_from_accent_phrases
 * が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] text UTF-8の日本語テキスト
 * @param [in] style_id スタイルID
 * @param [out] output_audio_query_json 生成先
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * char *audio_query;
 * voicevox_synthesizer_create_audio_query(synthesizer, "こんにちは",
 *                                         2, // "四国めたん (ノーマル)"
 *                                         &audio_query);
 * ```
 * }
 *
 * \safety{
 * - `text`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_create_audio_query}
 *
 * [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_create_audio_query(const struct VoicevoxSynthesizer *synthesizer,
                                                           const char *text,
                                                           VoicevoxStyleId style_id,
                                                           char **output_audio_query_json);

/**
 * AquesTalk風記法から、AccentPhrase (アクセント句)の配列をJSON形式で生成する。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] kana AquesTalk風記法
 * @param [in] style_id スタイルID
 * @param [out] output_accent_phrases_json 生成先
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * char *accent_phrases;
 * voicevox_synthesizer_create_accent_phrases_from_kana(
 *     synthesizer, "コンニチワ'",
 *     2, // "四国めたん (ノーマル)"
 *     &accent_phrases);
 * ```
 * }
 *
 * \safety{
 * - `kana`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_create_accent_phrases_from_kana}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_create_accent_phrases_from_kana(const struct VoicevoxSynthesizer *synthesizer,
                                                                        const char *kana,
                                                                        VoicevoxStyleId style_id,
                                                                        char **output_accent_phrases_json);

/**
 * 日本語テキストから、AccentPhrase (アクセント句)の配列をJSON形式で生成する。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * ::voicevox_open_jtalk_rc_analyze と ::voicevox_synthesizer_replace_mora_data
 * が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] text UTF-8の日本語テキスト
 * @param [in] style_id スタイルID
 * @param [out] output_accent_phrases_json 生成先
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * char *accent_phrases;
 * voicevox_synthesizer_create_accent_phrases(synthesizer, "こんにちは",
 *                                            2, // "四国めたん (ノーマル)"
 *                                            &accent_phrases);
 * ```
 * }
 *
 * \safety{
 * - `text`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_create_accent_phrases}
 *
 * [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_create_accent_phrases(const struct VoicevoxSynthesizer *synthesizer,
                                                              const char *text,
                                                              VoicevoxStyleId style_id,
                                                              char **output_accent_phrases_json);

/**
 * AccentPhraseの配列の音高・音素長を、特定の声で生成しなおす。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * ::voicevox_synthesizer_replace_phoneme_length と ::voicevox_synthesizer_replace_mora_pitch
 * が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
 * @param [in] style_id スタイルID
 * @param [out] output_accent_phrases_json 生成先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_replace_mora_data}
 *
 * [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_replace_mora_data(const struct VoicevoxSynthesizer *synthesizer,
                                                          const char *accent_phrases_json,
                                                          VoicevoxStyleId style_id,
                                                          char **output_accent_phrases_json);

/**
 * AccentPhraseの配列の音素長を、特定の声で生成しなおす。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
 * @param [in] style_id スタイルID
 * @param [out] output_accent_phrases_json 生成先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_replace_phoneme_length}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_replace_phoneme_length(const struct VoicevoxSynthesizer *synthesizer,
                                                               const char *accent_phrases_json,
                                                               VoicevoxStyleId style_id,
                                                               char **output_accent_phrases_json);

/**
 * AccentPhraseの配列の音高を、特定の声で生成しなおす。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] accent_phrases_json AccentPhraseの配列のJSON文字列
 * @param [in] style_id スタイルID
 * @param [out] output_accent_phrases_json 生成先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `accent_phrases_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_replace_mora_pitch}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_replace_mora_pitch(const struct VoicevoxSynthesizer *synthesizer,
                                                           const char *accent_phrases_json,
                                                           VoicevoxStyleId style_id,
                                                           char **output_accent_phrases_json);

/**
 * デフォルトの `voicevox_synthesizer_synthesis` のオプションを生成する
 * @return デフォルト値が設定された `voicevox_synthesizer_synthesis` のオプション
 *
 * \no-orig-impl{voicevox_make_default_synthesis_options}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
struct VoicevoxSynthesisOptions voicevox_make_default_synthesis_options(void);

/**
 * AudioQueryから音声合成を行う。
 *
 * 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] audio_query_json AudioQueryのJSON文字列
 * @param [in] style_id スタイルID
 * @param [in] options オプション
 * @param [out] output_wav_length 出力のバイト長
 * @param [out] output_wav 出力先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_synthesis}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_synthesis(const struct VoicevoxSynthesizer *synthesizer,
                                                  const char *audio_query_json,
                                                  VoicevoxStyleId style_id,
                                                  struct VoicevoxSynthesisOptions options,
                                                  uintptr_t *output_wav_length,
                                                  uint8_t **output_wav);

/**
 * デフォルトのテキスト音声合成オプションを生成する
 * @return テキスト音声合成オプション
 *
 * \no-orig-impl{voicevox_make_default_tts_options}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
struct VoicevoxTtsOptions voicevox_make_default_tts_options(void);

/**
 * AquesTalk風記法から音声合成を行う。
 *
 * 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
 *
 * @param [in] synthesizer
 * @param [in] kana AquesTalk風記法
 * @param [in] style_id スタイルID
 * @param [in] options オプション
 * @param [out] output_wav_length 出力のバイト長
 * @param [out] output_wav 出力先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `kana`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_tts_from_kana}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_tts_from_kana(const struct VoicevoxSynthesizer *synthesizer,
                                                      const char *kana,
                                                      VoicevoxStyleId style_id,
                                                      struct VoicevoxTtsOptions options,
                                                      uintptr_t *output_wav_length,
                                                      uint8_t **output_wav);

/**
 * 日本語テキストから音声合成を行う。
 *
 * 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
 *
 * ::voicevox_synthesizer_create_audio_query と ::voicevox_synthesizer_synthesis
 * が一体になったショートハンド。詳細は[テキスト音声合成の流れ]を参照。
 *
 * @param [in] synthesizer
 * @param [in] text UTF-8の日本語テキスト
 * @param [in] style_id スタイルID
 * @param [in] options オプション
 * @param [out] output_wav_length 出力のバイト長
 * @param [out] output_wav 出力先
 *
 * @returns 結果コード
 *
 * \safety{
 * - `text`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_tts}
 *
 * [テキスト音声合成の流れ]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_tts(const struct VoicevoxSynthesizer *synthesizer,
                                            const char *text,
                                            VoicevoxStyleId style_id,
                                            struct VoicevoxTtsOptions options,
                                            uintptr_t *output_wav_length,
                                            uint8_t **output_wav);

/**
 * 楽譜から歌唱音声合成用のクエリを作成する。
 *
 * 詳細はユーザーガイド[歌唱音声合成]を参照。
 *
 * [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
 *
 * 生成したJSONを解放するには ::voicevox_json_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] score_json [`Score`型]を表すJSON
 * @param [in] style_id スタイルID
 * @param [out] output_frame_audio_query_json 生成先
 *
 * [`Score`型]: ../rust_api/voicevox_core/struct.Score.html
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * const char *kScore =
 *     "{"
 *     "  \"notes\": [ "
 *     "    { \"key\": null, \"frame_length\": 15, \"lyric\": \"\" },"
 *     "    { \"key\": 60, \"frame_length\": 45, \"lyric\": \"ド\" },"
 *     "    { \"key\": 62, \"frame_length\": 45, \"lyric\": \"レ\" },"
 *     "    { \"key\": 64, \"frame_length\": 45, \"lyric\": \"ミ\" },"
 *     "    { \"key\": null, \"frame_length\": 15, \"lyric\": \"\" }"
 *     "  ]"
 *     "}";
 * const VoicevoxStyleId kSingingTeacher = 6000;
 *
 * char *frame_audio_query;
 * const VoicevoxResultCode result =
 *     voicevox_synthesizer_create_sing_frame_audio_query(
 *         synthesizer, kScore, kSingingTeacher, &frame_audio_query);
 * ```
 * }
 *
 * \safety{
 * - `score_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_frame_audio_query_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_create_sing_frame_audio_query}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_create_sing_frame_audio_query(const struct VoicevoxSynthesizer *synthesizer,
                                                                      const char *score_json,
                                                                      VoicevoxStyleId style_id,
                                                                      char **output_frame_audio_query_json);

/**
 * 楽譜と歌唱音声合成用のクエリから、フレームごとの基本周波数を生成する。
 *
 * 詳細はユーザーガイド[歌唱音声合成]を参照。
 *
 * [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
 *
 * 生成したJSONを解放するには ::voicevox_json_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] score_json [`Score`型]を表すJSON
 * @param [in] frame_audio_query_json [`FrameAudioQuery`型]を表すJSON
 * @param [in] style_id スタイルID
 * @param [out] output_f0_json 生成先
 *
 * [`Score`型]: ../rust_api/voicevox_core/struct.Score.html
 * [`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
 *
 * @returns 結果コード
 *
 * \safety{
 * - `score_json`と`frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_f0_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_create_sing_frame_f0}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_create_sing_frame_f0(const struct VoicevoxSynthesizer *synthesizer,
                                                             const char *score_json,
                                                             const char *frame_audio_query_json,
                                                             VoicevoxStyleId style_id,
                                                             char **output_f0_json);

/**
 * 楽譜と歌唱音声合成用のクエリから、フレームごとの音量を生成する。
 *
 * 詳細はユーザーガイド[歌唱音声合成]を参照。
 *
 * [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
 *
 * 生成したJSONを解放するには ::voicevox_json_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] score_json [`Score`型]を表すJSON
 * @param [in] frame_audio_query_json [`FrameAudioQuery`型]を表すJSON
 * @param [in] style_id スタイルID
 * @param [out] output_volume_json 生成先
 *
 * [`Score`型]: ../rust_api/voicevox_core/struct.Score.html
 * [`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
 *
 * @returns 結果コード
 *
 * \safety{
 * - `score_json`と`frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_volume_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_create_sing_frame_volume}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_create_sing_frame_volume(const struct VoicevoxSynthesizer *synthesizer,
                                                                 const char *score_json,
                                                                 const char *frame_audio_query_json,
                                                                 VoicevoxStyleId style_id,
                                                                 char **output_volume_json);

/**
 * 歌唱音声合成を行う。
 *
 * 詳細はユーザーガイド[歌唱音声合成]を参照。
 *
 * [歌唱音声合成]: https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md
 *
 * 生成したWAVデータを解放するには ::voicevox_wav_free を使う。
 *
 * @param [in] synthesizer 音声シンセサイザ
 * @param [in] frame_audio_query_json [`FrameAudioQuery`型]を表すJSON
 * @param [in] style_id スタイルID
 * @param [out] output_wav_length 出力のバイト長
 * @param [out] output_wav 出力先
 *
 * [`FrameAudioQuery`型]: ../rust_api/voicevox_core/struct.FrameAudioQuery.html
 *
 * @returns 結果コード
 *
 * \example{
 * ```c
 * const VoicevoxStyleId kSinger = 3000;
 *
 * uint8_t *wav;
 * size_t wav_length;
 * const VoicevoxResultCode result = voicevox_synthesizer_frame_synthesis(
 *     synthesizer, frame_audio_query, kSinger, &wav_length, &wav);
 * ```
 * }
 *
 * \safety{
 * - `frame_audio_query_json`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_wav_length`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * - `output_wav`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_synthesizer_frame_synthesis}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_synthesizer_frame_synthesis(const struct VoicevoxSynthesizer *synthesizer,
                                                        const char *frame_audio_query_json,
                                                        VoicevoxStyleId style_id,
                                                        uintptr_t *output_wav_length,
                                                        uint8_t **output_wav);

/**
 * JSON文字列を解放する。
 *
 * @param [in] json 解放するJSON文字列。nullable
 *
 * \safety{
 * - `json`がヌルポインタでないならば、以下のAPIで得られたポインタでなくてはいけない。
 *     - ::voicevox_audio_query_create_from_accent_phrases
 *     - ::voicevox_onnxruntime_create_supported_devices_json
 *     - ::voicevox_voice_model_file_create_metas_json
 *     - ::voicevox_open_jtalk_rc_analyze
 *     - ::voicevox_synthesizer_create_metas_json
 *     - ::voicevox_synthesizer_create_audio_query
 *     - ::voicevox_synthesizer_create_audio_query_from_kana
 *     - ::voicevox_synthesizer_create_accent_phrases
 *     - ::voicevox_synthesizer_create_accent_phrases_from_kana
 *     - ::voicevox_synthesizer_replace_mora_data
 *     - ::voicevox_synthesizer_replace_phoneme_length
 *     - ::voicevox_synthesizer_replace_mora_pitch
 *     - ::voicevox_synthesizer_create_sing_frame_audio_query
 *     - ::voicevox_synthesizer_create_sing_frame_f0
 *     - ::voicevox_synthesizer_create_sing_frame_volume
 *     - ::voicevox_user_dict_to_json
 * - 文字列の長さは生成時より変更されていてはならない。
 * - `json`がヌルポインタでないならば、<a href="#voicevox-core-safety">読み込みと書き込みについて有効</a>でなければならない。
 * - `json`がヌルポインタでないならば、以後<b>ダングリングポインタ</b>(_dangling pointer_)として扱われなくてはならない。
 * }
 *
 * \no-orig-impl{voicevox_json_free}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
void voicevox_json_free(char *json);

/**
 * WAVデータを解放する。
 *
 * @param [in] wav 解放するWAVデータ。nullable
 *
 * \safety{
 * - `wav`がヌルポインタでないならば、以下のAPIで得られたポインタでなくてはいけない。
 *     - ::voicevox_synthesizer_synthesis
 *     - ::voicevox_synthesizer_tts
 *     - ::voicevox_synthesizer_tts_from_kana
 *     - ::voicevox_synthesizer_frame_synthesis
 * - `wav`がヌルポインタでないならば、<a href="#voicevox-core-safety">読み込みと書き込みについて有効</a>でなければならない。
 * - `wav`がヌルポインタでないならば、以後<b>ダングリングポインタ</b>(_dangling pointer_)として扱われなくてはならない。
 * }
 *
 * \no-orig-impl{voicevox_wav_free}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
void voicevox_wav_free(uint8_t *wav);

/**
 * 結果コードに対応したメッセージ文字列を取得する。
 *
 * @param [in] result_code 結果コード
 *
 * @returns 結果コードに対応したメッセージ文字列
 *
 * \examples{
 * ```c
 * const char *actual = voicevox_error_result_to_message(VOICEVOX_RESULT_OK);
 * const char *EXPECTED = "エラーが発生しませんでした";
 * assert(strcmp(actual, EXPECTED) == 0);
 * ```
 *
 * ```c
 * const char *actual =
 *     voicevox_error_result_to_message(VOICEVOX_RESULT_LOAD_MODEL_ERROR);
 * const char *EXPECTED = "modelデータ読み込みに失敗しました";
 * assert(strcmp(actual, EXPECTED) == 0);
 * ```
 * }
 *
 * \no-orig-impl{voicevox_error_result_to_message}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
const char *voicevox_error_result_to_message(VoicevoxResultCode result_code);

/**
 * ::VoicevoxUserDictWord を最低限のパラメータで作成する。
 *
 * @param [in] surface 表記
 * @param [in] pronunciation 読み
 * @param [in] accent_type アクセント型
 * @returns ::VoicevoxUserDictWord
 *
 * \orig-impl{voicevox_user_dict_word_make}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
struct VoicevoxUserDictWord voicevox_user_dict_word_make(const char *surface,
                                                         const char *pronunciation,
                                                         uintptr_t accent_type);

/**
 * ユーザー辞書をb>構築</b>(_construct_)する。
 *
 * @returns ::VoicevoxUserDict
 *
 * \orig-impl{voicevox_user_dict_new}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
struct VoicevoxUserDict *voicevox_user_dict_new(void);

/**
 * ユーザー辞書にファイルを読み込ませる。
 *
 * @param [in] user_dict ユーザー辞書
 * @param [in] dict_path 読み込む辞書ファイルのパス
 * @returns 結果コード
 *
 * \safety{
 * - `dict_path`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_user_dict_load}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_user_dict_load(const struct VoicevoxUserDict *user_dict,
                                           const char *dict_path);

/**
 * ユーザー辞書に単語を追加する。
 *
 * @param [in] ユーザー辞書
 * @param [in] word 追加する単語
 * @param [out] output_word_uuid 追加した単語のUUID
 * @returns 結果コード
 *
 * # Safety
 * @param user_dict は有効な :VoicevoxUserDict のポインタであること
 *
 * \safety{
 * - `word->surface`と`word->pronunciation`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `output_word_uuid`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_user_dict_add_word}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_user_dict_add_word(const struct VoicevoxUserDict *user_dict,
                                               const struct VoicevoxUserDictWord *word,
                                               uint8_t (*output_word_uuid)[16]);

/**
 * ユーザー辞書の単語を更新する。
 *
 * @param [in] user_dict ユーザー辞書
 * @param [in] word_uuid 更新する単語のUUID
 * @param [in] word 新しい単語のデータ
 * @returns 結果コード
 *
 * \safety{
 * - `word_uuid`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * - `word->surface`と`word->pronunciation`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_user_dict_update_word}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_user_dict_update_word(const struct VoicevoxUserDict *user_dict,
                                                  const uint8_t (*word_uuid)[16],
                                                  const struct VoicevoxUserDictWord *word);

/**
 * ユーザー辞書から単語を削除する。
 *
 * @param [in] user_dict ユーザー辞書
 * @param [in] word_uuid 削除する単語のUUID
 * @returns 結果コード
 *
 * \safety{
 * - `word_uuid`は<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_user_dict_remove_word}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_user_dict_remove_word(const struct VoicevoxUserDict *user_dict,
                                                  const uint8_t (*word_uuid)[16]);

/**
 * ユーザー辞書の単語をJSON形式で出力する。
 *
 * 生成したJSON文字列を解放するには ::voicevox_json_free を使う。
 *
 * @param [in] user_dict ユーザー辞書
 * @param [out] output_json 出力先
 * @returns 結果コード
 *
 * \safety{
 * - `output_json`は<a href="#voicevox-core-safety">書き込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_user_dict_to_json}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_user_dict_to_json(const struct VoicevoxUserDict *user_dict,
                                              char **output_json);

/**
 * 他のユーザー辞書をインポートする。
 *
 * @param [in] user_dict ユーザー辞書
 * @param [in] other_dict インポートするユーザー辞書
 * @returns 結果コード
 *
 * \orig-impl{voicevox_user_dict_import}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_user_dict_import(const struct VoicevoxUserDict *user_dict,
                                             const struct VoicevoxUserDict *other_dict);

/**
 * ユーザー辞書をファイルに保存する。
 *
 * @param [in] user_dict ユーザー辞書
 * @param [in] path 保存先のファイルパス
 *
 * \safety{
 * - `path`はヌル終端文字列を指し、かつ<a href="#voicevox-core-safety">読み込みについて有効</a>でなければならない。
 * }
 *
 * \orig-impl{voicevox_user_dict_save}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
VoicevoxResultCode voicevox_user_dict_save(const struct VoicevoxUserDict *user_dict,
                                           const char *path);

/**
 * ユーザー辞書を<b>破棄</b>(_destruct_)する。
 *
 * 破棄対象への他スレッドでのアクセスが存在する場合、それらがすべて終わるのを待ってから破棄する。
 *
 * この関数の呼び出し後に破棄し終えた対象にアクセスすると、プロセスを異常終了する。
 *
 * @param [in] user_dict 破棄対象。nullable
 *
 * \no-orig-impl{voicevox_user_dict_delete}
 */
#ifdef _WIN32
__declspec(dllimport)
#endif
void voicevox_user_dict_delete(struct VoicevoxUserDict *user_dict);

#ifdef __cplusplus
}  // extern "C"
#endif  // __cplusplus

#endif  /* VOICEVOX_CORE_INCLUDE_GUARD */
