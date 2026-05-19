import dataclasses
import typing
from dataclasses import InitVar
from typing import Literal, NewType, NoReturn, TypeAlias
from uuid import UUID

from .._rust import (
    _audio_query_from_accent_phrases,
    _audio_query_from_json,
    _audio_query_to_json,
    _ReservedFields,
    _to_zenkaku,
    _validate_accent_phrase,
    _validate_audio_query,
    _validate_frame_audio_query,
    _validate_frame_phoneme,
    _validate_mora,
    _validate_note,
    _validate_score,
    _validate_user_dict_word,
)
from ._please_do_not_use import _Reserved

Never: TypeAlias = NoReturn

StyleId = NewType("StyleId", int)
"""
スタイルID。

VOICEVOXにおける、ある |styleid-character|_ のある |styleid-style|_ を指す。

.. |styleid-character| replace:: *キャラクター*
.. _styleid-character: #voicevox_core.CharacterMeta
.. |styleid-style| replace:: *スタイル*
.. _styleid-style: #voicevox_core.StyleMeta

Parameters
----------
x : int
"""

CharacterVersion = NewType("CharacterVersion", str)
"""
|characterversion-character|_ のバージョン。

.. |characterversion-character| replace:: *キャラクター*
.. _characterversion-character: #voicevox_core.CharacterMeta

Parameters
----------
x : str
"""

VoiceModelId = NewType("VoiceModelId", UUID)
"""
音声モデルID。

``Synthesizer`` はこのIDをキーとして、音声モデルのロード・アンロードを行う。

同じIDを持つ複数のVVMファイルがあるときは、ファイルとして新しい方を常に使うことが推奨される。 `VOICEVOX/voicevox_vvm <https://github.com/VOICEVOX/voicevox_vvm>`_ で管理されているVVMでは、次の方針が取られている。

- VVMに含まれる声が変化せず、軽微な修正のみのときはIDを使い回してリリースする。
- VVMに含まれる声が明確に変化するかもしくは削除されるような実質的な変更のときは、新しいIDを割り振ってリリースする。

これ以外は未定であり、更なるルールについては `VOICEVOX/voicevox_vvm#19 <https://github.com/VOICEVOX/voicevox_vvm/issues/19>`_ で議論される予定。

Parameters
----------
x : UUID
"""

StyleType: TypeAlias = (
    Literal["talk", "singing_teacher", "frame_decode", "sing"] | _Reserved
)
"""

|styletype-style|_ に対応するモデルの種類。

.. |styletype-style| replace:: *スタイル*
.. _styletype-style: #voicevox_core.StyleMeta

===================== ==================================================
値                    説明
``"talk"``            音声合成クエリの作成と音声合成が可能。
``"singing_teacher"`` 歌唱音声合成用のクエリの作成が可能。
``"frame_decode"``    歌唱音声合成が可能。
``"sing"``            歌唱音声合成用のクエリの作成と歌唱音声合成が可能。
``_Reserved``         将来のために予約されている値。この値が存在することは決してない。
                      ``str`` のサブタイプであるため、 ``StyleType`` を ``str`` として
                      扱うことは可能。
===================== ==================================================

``_Reserved`` の存在により、例えば次のコードはPyright/Pylanceの型検査に通らない。これは意図的なデザインである。

.. code-block::

    def _(style_type: StyleType) -> int:
        match style_type:
            case "talk":
                return 0
            case "singing_teacher":
                return 1
            case "frame_decode":
                return 2
            case "sing":
                return 3

.. code-block:: text

    error: Function with declared return type "int" must return value on all code paths
    "None" is not assignable to "int" (reportReturnType)

``str`` として扱うことは可能。

.. code-block::

    def _(style_type: StyleType):
        _: str = style_type  # OK
"""


def _(style_type: StyleType):
    _: str = style_type


@dataclasses.dataclass
class StyleMeta:
    """
    *スタイル* のメタ情報。
    """

    name: str
    """スタイル名。"""

    id: StyleId
    """スタイルID。"""

    type: StyleType = dataclasses.field(default="talk")
    """スタイルに対応するモデルの種類。"""

    order: int | None = None
    """
    スタイルの順番。

    :attr:`CharacterMeta.styles` は、この値に対して昇順に並んでいるべきである。
    """


@dataclasses.dataclass
class CharacterMeta:
    """
    *キャラクター* のメタ情報。
    """

    name: str
    """キャラクター名。"""

    styles: list[StyleMeta]
    """キャラクターに属するスタイル。"""

    speaker_uuid: str
    """キャラクターのUUID。"""

    version: CharacterVersion
    """キャラクターのバージョン。"""

    order: int | None = None
    """
    キャラクターの順番。

    ``CharacterMeta`` の列は、この値に対して昇順に並んでいるべきである。
    """


@dataclasses.dataclass
class SupportedDevices:
    """
    ONNX Runtimeとして利用可能なデバイスの情報。

    あくまでONNX Runtimeが対応しているデバイスの情報であることに注意。GPUが使える環境ではなかったとしても
    ``cuda`` や ``dml`` は ``True`` を示しうる。

    VOICEVOX CORE以外が作ることはできない。作ろうとした場合 ``TypeError`` となる。
    """

    cpu: bool
    """
    CPUが利用可能。

    常に ``True`` 。
    """

    cuda: bool
    """
    CUDAが利用可能。

    ONNX Runtimeの `CUDA Execution Provider <https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html>`_
    (``CUDAExecutionProvider``)に対応する。必要な環境についてはそちらを参照。
    """

    dml: bool
    """
    DirectMLが利用可能。

    ONNX Runtimeの `DirectML Execution Provider <https://onnxruntime.ai/docs/execution-providers/DirectML-ExecutionProvider.html>`_
    (``DmlExecutionProvider``)に対応する。必要な環境についてはそちらを参照。
    """

    _reserved: InitVar[Never]

    def __post_init__(self, reserved: Never) -> None:
        if not isinstance(typing.cast(object, reserved), _ReservedFields):
            raise TypeError("You cannot instantiate `SupportedDevices` by yourself")


AccelerationMode: TypeAlias = Literal["AUTO", "CPU", "GPU"] | _Reserved
"""
ハードウェアアクセラレーションモードを設定する設定値。

============= =======================================================================
値            説明
``"AUTO"``    実行環境に合った適切なハードウェアアクセラレーションモードを選択する。
``"CPU"``     ハードウェアアクセラレーションモードを"CPU"に設定する。
``"GPU"``     ハードウェアアクセラレーションモードを"GPU"に設定する。
``_Reserved`` 将来のために予約されている値。この値が存在することは決してない。
              ``str`` のサブタイプであるため、 ``AccelerationMode`` を ``str`` として
              扱うことは可能。
============= =======================================================================

``_Reserved`` の存在により、例えば次のコードはPyright/Pylanceの型検査に通らない。これは意図的なデザインである。

.. code-block::

    def _(mode: AccelerationMode) -> int:
        match mode:
            case "AUTO":
                return 0
            case "CPU":
                return 1
            case "GPU":
                return 2

.. code-block:: text

    error: Function with declared return type "int" must return value on all code paths
    "None" is not assignable to "int" (reportReturnType)

``str`` として扱うことは可能。

.. code-block::

    def _(mode: AccelerationMode):
        _: str = mode  # OK
"""


def _(mode: AccelerationMode):
    _: str = mode


OnExistingVoiceModelId: TypeAlias = Literal["ERROR", "RELOAD", "SKIP"] | _Reserved
"""
``Synthesizer::load_voice_model`` の実行時に、同じ ``id`` の ``VoiceModelFile`` が既に読み込まれていたときのふるまい。

============= =============================================
値            説明
``"ERROR"``   エラー。デフォルトのふるまい。
``"RELOAD"``  再読み込みする。VOICEVOX
              COREでは、長文のテキストを一度に音声合成するとCPU/GPUメモリが大量に占有されたままになる。再読み込みを行うとメモリの使用量が元に戻る。
``"SKIP"``    スキップする。
``_Reserved`` 将来のために予約されている値。この値が存在することは決してない。
              ``str`` のサブタイプであるため、 ``OnExistingVoiceModelId`` を ``str`` として
              扱うことは可能。
============= =============================================
"""


def _(on_existing: OnExistingVoiceModelId):
    _: str = on_existing


@dataclasses.dataclass
class Mora:
    """
    モーラ（子音＋母音）ごとの情報。

    このクラスは不正な状態を表現しうる。どのような状態が不正なのかについては
    :func:`validate` を参照。このクラスを使う関数は、不正な状態に対して
    |mora-invalid-query-error|_ を送出する。

    コンストラクト時には、不正な状態であるかの検証は行われない。

    .. |mora-invalid-query-error| replace:: ``InvalidQueryError``
    .. _mora-invalid-query-error: #voicevox_core.InvalidQueryError
    """

    text: str
    """文字。"""

    vowel: str
    """母音の音素。"""

    vowel_length: float
    """母音の音長。"""

    pitch: float
    """音高。"""

    consonant: str | None = None
    """子音の音素。"""

    consonant_length: float | None = None
    """子音の音長。"""

    def validate(self) -> None:
        """
        このインスタンスが不正であるときエラーを返す。

        不正であるとは、以下のいずれかの条件を満たすことである。

        - :attr:`consonant` と :attr:`consonant_length` の有無が不一致。
        - :attr:`consonant` が子音以外の音素であるか、もしくは音素として不正。
        - :attr:`vowel` が子音であるか、もしくは音素として不正。

        送出するエラーは |mora-validate-invalid-query-error|_  。

        また次の状態に対しては |mora-validate-logging-warning|_
        レベルのログを出す。将来的にはエラーになる予定。

        - :attr:`consonant_length` がNaN、infinity、もしくは負。
        - :attr:`vowel_length` がNaN、infinity、もしくは負。
        - :attr:`pitch` がNaNもしくは±infinity。

        .. |mora-validate-invalid-query-error| replace:: ``InvalidQueryError``
        .. _mora-validate-invalid-query-error: #voicevox_core.InvalidQueryError
        .. |mora-validate-logging-warning| replace:: ``WARNING``
        .. _mora-validate-logging-warning: https://docs.python.org/3/library/logging.html#logging.WARNING
        """
        _validate_mora(self)


@dataclasses.dataclass
class AccentPhrase:
    """
    AccentPhrase (アクセント句ごとの情報)。

    このクラスは不正な状態を表現しうる。どのような状態が不正なのかについては
    :func:`validate` を参照。このクラスを使う関数は、不正な状態に対して
    |accent-phrase-invalid-query-error|_ を送出する。

    コンストラクト時には、不正な状態であるかの検証は行われない。

    .. |accent-phrase-invalid-query-error| replace:: ``InvalidQueryError``
    .. _accent-phrase-invalid-query-error: #voicevox_core.InvalidQueryError
    """

    moras: list[Mora]
    """モーラの配列。"""

    accent: int
    """アクセント箇所。"""

    pause_mora: Mora | None = None
    """後ろに無音を付けるかどうか。"""

    is_interrogative: bool = False
    """疑問系かどうか。"""

    def validate(self) -> None:
        """
        このインスタンスが不正であるときエラーを返す。

        不正であるとは、以下のいずれかの条件を満たすことである。

        - |accent-phrase-rust-ty|_ としてデシリアライズ不可。
            - :attr:`accent` が負であるか、もしくは :math:`2^{64}-1` (32ビットプラットフォームの場合 :math:`2^{32}-1`)を超過する。
        - :attr:`moras` もしくは :attr:`pause_mora` の要素のうちいずれかが |accent-phrase-validate-mora-validate|_ 。
        - :attr:`accent` が ``0`` 。

        送出するエラーは |accent-phrase-validate-invalid-query-error|_ 。

        また次の状態に対しては |accent-phrase-validate-logging-warning|_
        レベルのログを出す。将来的にはエラーになる予定。

        - :attr:`moras` もしくは :attr:`pause_mora` の要素のうちいずれかが、警告が出る状態。
        - :attr:`accent` が :attr:`moras` の数を超過している。

        .. |accent-phrase-validate-invalid-query-error| replace:: ``InvalidQueryError``
        .. _accent-phrase-validate-invalid-query-error: #voicevox_core.InvalidQueryError
        .. |accent-phrase-rust-ty| replace:: Rust APIの ``AccentPhrase`` 型
        .. _accent-phrase-rust-ty: ../../../rust_api/voicevox_core/struct.AccentPhrase.html
        .. |accent-phrase-validate-mora-validate| replace:: 不正
        .. _accent-phrase-validate-mora-validate: #voicevox_core.Mora.validate
        .. |accent-phrase-validate-logging-warning| replace:: ``WARNING``
        .. _accent-phrase-validate-logging-warning: https://docs.python.org/3/library/logging.html#logging.WARNING
        """
        _validate_accent_phrase(self)


@dataclasses.dataclass
class AudioQuery:
    """
    AudioQuery (音声合成用のクエリ)。

    このクラスは不正な状態を表現しうる。どのような状態が不正なのかについては
    :func:`validate` を参照。このクラスを使う関数は、不正な状態に対して
    |audio-query-invalid-query-error|_ を送出する。

    コンストラクト時には、不正な状態であるかの検証は行われない。

    .. |audio-query-invalid-query-error| replace:: ``InvalidQueryError``
    .. _audio-query-invalid-query-error: #voicevox_core.InvalidQueryError

    シリアライゼーションのサポートはされていない。詳細は
    `データのシリアライゼーション
    <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/serialization.md>`_
    を参照。
    """

    accent_phrases: list[AccentPhrase]
    """アクセント句の配列。"""

    speed_scale: float
    """全体の話速。"""

    pitch_scale: float
    """全体の音高。"""

    intonation_scale: float
    """全体の抑揚。"""

    volume_scale: float
    """全体の音量。"""

    pre_phoneme_length: float
    """音声の前の無音時間。"""

    post_phoneme_length: float
    """音声の後の無音時間。"""

    output_sampling_rate: int
    """音声データの出力サンプリングレート。"""

    output_stereo: bool
    """音声データをステレオ出力するか否か。"""

    kana: str | None = None
    """
    [読み取り専用] AquesTalk風記法。

    :func:`Synthesizer.create_audio_query` が返すもののみ ``str`` となる。入力として
    のAudioQueryでは無視される。
    """

    @staticmethod
    def from_accent_phrases(accent_phrases: list["AccentPhrase"]) -> "AudioQuery":
        return _audio_query_from_accent_phrases(accent_phrases)

    def validate(self) -> None:
        """
        このインスタンスが不正であるときエラーを返す。

        不正であるとは、以下のいずれかの条件を満たすことである。

        - |audio-query-rust-ty|_ としてデシリアライズ不可。
            - :attr:`output_sampling_rate` が負であるか、もしくは :math:`2^{32}-1` を超過する。
        - :attr:`accent_phrases` の要素のうちいずれかが |audio-query-validate-accent-phrase-validate|_ 。
        - :attr:`output_sampling_rate` が ``24000`` の倍数ではない、もしくは ``0`` (将来的に解消予定。cf. |audio-query-validate-issue762|_)。

        送出するエラーは |audio-query-validate-invalid-query-error|_ 。

        また次の状態に対しては |audio-query-validate-logging-warning|_
        レベルのログを出す。将来的にはエラーになる予定。

        - :attr:`accent_phrases` の要素のうちいずれかが警告が出る状態。
        - :attr:`speed_scale` がNaN、infinity、もしくは負。
        - :attr:`pitch_scale` がNaNもしくは±infinity。
        - :attr:`intonation_scale` がNaNもしくは±infinity。
        - :attr:`volume_scale` がNaN、infinity、もしくは負。
        - :attr:`pre_phoneme_length` がNaN、infinity、もしくは負。
        - :attr:`post_phoneme_length` がNaN、infinity、もしくは負。
        - :attr:`output_sampling_rate` が ``24000`` 以外の値（エラーと同様将来的に解消予定）。

        .. |audio-query-validate-invalid-query-error| replace:: ``InvalidQueryError``
        .. _audio-query-validate-invalid-query-error: #voicevox_core.InvalidQueryError
        .. |audio-query-rust-ty| replace:: Rust APIの ``AudioQuery`` 型
        .. _audio-query-rust-ty: ../../../rust_api/voicevox_core/struct.AudioQuery.html
        .. |audio-query-validate-accent-phrase-validate| replace:: 不正
        .. _audio-query-validate-accent-phrase-validate: #voicevox_core.AccentPhrase.validate
        .. |audio-query-validate-issue762| replace:: #762
        .. _audio-query-validate-issue762: https://github.com/VOICEVOX/voicevox_core/issues/762
        .. |audio-query-validate-logging-warning| replace:: ``WARNING``
        .. _audio-query-validate-logging-warning: https://docs.python.org/3/library/logging.html#logging.WARNING
        """
        _validate_audio_query(self)

    # テストに使用する目的でのみ存在

    @staticmethod
    def __from_json(  # pyright: ignore [reportUnusedFunction]
        json: str,
    ) -> "AudioQuery":
        return _audio_query_from_json(json)

    def __to_json(self) -> str:  # pyright: ignore [reportUnusedFunction]
        return _audio_query_to_json(self)


UserDictWordType: TypeAlias = (
    Literal["PROPER_NOUN", "COMMON_NOUN", "VERB", "ADJECTIVE", "SUFFIX"] | _Reserved
)
"""
ユーザー辞書の単語の品詞。

================= ==========
値                説明
``"PROPER_NOUN"`` 固有名詞。
``"COMMON_NOUN"`` 一般名詞。
``"VERB"``        動詞。
``"ADJECTIVE"``   形容詞。
``"SUFFIX"``      語尾。
``_Reserved``     将来のために予約されている値。この値が存在することは決してない。
                  ``str`` のサブタイプであるため、 ``UserDictWordType`` を ``str`` として
                  扱うことは可能。
================= ==========

``_Reserved`` の存在により、例えば次のコードはPyright/Pylanceの型検査に通らない。これは意図的なデザインである。

.. code-block::

    def _(word_type: UserDictWordType) -> int:
        match word_type:
            case "PROPER_NOUN":
                return 0
            case "COMMON_NOUN":
                return 1
            case "VERB":
                return 2
            case "ADJECTIVE":
                return 3
            case "SUFFIX":
                return 4

.. code-block:: text

    error: Function with declared return type "int" must return value on all code paths
    "None" is not assignable to "int" (reportReturnType)

``str`` として扱うことは可能。

.. code-block::

    def _(word_type: UserDictWordType):
        _: str = word_type  # OK
"""


def _(word_type: UserDictWordType):
    _: str = word_type


@dataclasses.dataclass(frozen=True)
class UserDictWord:
    """
    ユーザー辞書の単語。

    シリアライゼーションのサポートはされていない。詳細は
    `データのシリアライゼーション
    <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/serialization.md>`_
    を参照。
    """

    surface: str
    """言葉の表層形。"""

    pronunciation: str
    """
    言葉の発音。

    カタカナで表記する。
    """

    accent_type: int
    """
    アクセント型。

    音が下がる場所を指す。
    """

    word_type: UserDictWordType = dataclasses.field(default="COMMON_NOUN")
    """品詞。"""

    priority: int = dataclasses.field(default=5)
    """
    単語の優先度。

    0から10までの整数。
    数字が大きいほど優先度が高くなる。
    1から9までの値を指定することを推奨する。
    """

    def __post_init__(self) -> None:
        # Pydanticが（おそらく）やっているであろう方法。
        # とりあえずPython 3.13では大丈夫らしい
        object.__setattr__(self, "surface", _to_zenkaku(self.surface))

        _validate_user_dict_word(self)


NoteId = NewType("NoteId", str)
"""
音符のID。

Parameters
----------
x : str
"""


@dataclasses.dataclass
class Note:
    """
    音符または休符。

    このクラスは不正な状態を表現しうる。どのような状態が不正なのかについては
    :func:`validate` を参照。このクラスを使う関数は、不正な状態に対して
    |note-invalid-query-error|_ を送出する。

    コンストラクト時には、不正な状態であるかの検証は行われない。

    .. code-block::

        note = Note(45, "ファ", key=65)
        rest = Note(45, "")

    .. |note-invalid-query-error| replace:: ``InvalidQueryError``
    .. _note-invalid-query-error: #voicevox_core.InvalidQueryError
    """

    frame_length: int
    """
    音符のフレーム長。

    秒数に93.75をかけ、端数を調整して整数にしたもの。例として125BPM (Beats Per
    Minute)における一拍は:

    93.75[フレーム/秒] / (125[拍/分] / 60[秒/分]) = 45[フレーム/拍]

    ここで設定した値は分割された上で :attr:`FramePhoneme.frame_length`
    に割り当てられる。分割処理の詳細は `子音の侵食
    <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md#子音の侵食>`_
    を参照。
    """

    lyric: str
    """
    歌詞。

    - 音符の場合、一つのモーラを表すひらがな/カタカナ（例: ``"ド"``, ``"ファ"``）。
    - 休符の場合、空文字列。
    """

    key: int | None = None
    """
    音階。

    - 音符の場合、MIDIのnote number（例: C4なら ``60``）。
    - 休符の場合、``None``。
    """

    id: NoteId | None = None
    """
    ID。

    :class:`FrameAudioQuery` を生成するときに :attr:`FramePhoneme.note_id`
    にコピーされる。歌唱音声には影響しない。
    """

    def validate(self) -> None:
        """
        このインスタンスが不正であるときエラーを返す。

        不正であるとは、以下のいずれかの条件を満たすことである。

        - |note-rust-ty|_ としてデシリアライズ不可。
            - :attr:`key` が負であるか、もしくは ``127`` を超過する。
            - :attr:`lyric` が空文字列以外の、モーラとして不正な文字列。
            - :attr:`frame_length` が負であるか、もしくは :math:`2^{53}-1` を超過する。
        - :attr:`key` が ``None`` かつ :attr:`lyric` が ``""`` 以外。
        - :attr:`key` が ``int`` かつ :attr:`lyric` が ``""`` 。

        送出するエラーは |note-validate-invalid-query-error|_ 。

        .. |note-rust-ty| replace:: Rust APIの ``Note`` 型
        .. _note-rust-ty: ../../../rust_api/voicevox_core/struct.Note.html
        .. |note-validate-invalid-query-error| replace:: ``InvalidQueryError``
        .. _note-validate-invalid-query-error: #voicevox_core.InvalidQueryError
        """
        _validate_note(self)


@dataclasses.dataclass
class Score:
    """
    楽譜情報。

    このクラスは不正な状態を表現しうる。どのような状態が不正なのかについては
    :func:`validate` を参照。このクラスを使う関数は、不正な状態に対して
    |score-invalid-query-error|_ を送出する。

    コンストラクト時には、不正な状態であるかの検証は行われない。

    .. code-block::

        Score(
            [
                Note(15, ""),
                Note(45, "ド", key=60),
                Note(45, "レ", key=62),
                Note(45, "ミ", key=64),
                Note(15, ""),
            ],
        )

    .. |score-invalid-query-error| replace:: ``InvalidQueryError``
    .. _score-invalid-query-error: #voicevox_core.InvalidQueryError
    """

    notes: list[Note]
    """音符のリスト。"""

    def validate(self) -> None:
        """
        このインスタンスが不正であるときエラーを返す。

        不正であるとは、以下のいずれかの条件を満たすことである。

        - :attr:`notes` の要素のうちいずれかが |score-validate-note-validate|_ 。
        - :attr:`notes` が空であるか、もしくは先頭が音符。

        送出するエラーは |score-validate-invalid-query-error|_ 。

        .. |score-validate-note-validate| replace:: 不正
        .. _score-validate-note-validate: #voicevox_core.Note.validate
        .. |score-validate-invalid-query-error| replace:: ``InvalidQueryError``
        .. _score-validate-invalid-query-error: #voicevox_core.InvalidQueryError
        """
        _validate_score(self)


@dataclasses.dataclass
class FramePhoneme:
    """
    音素の情報。

    このクラスは不正な状態を表現しうる。どのような状態が不正なのかについては
    :func:`validate` を参照。このクラスを使う関数は、不正な状態に対して
    |frame-phoneme-invalid-query-error|_ を送出する。

    コンストラクト時には、不正な状態であるかの検証は行われない。

    .. |frame-phoneme-invalid-query-error| replace:: ``InvalidQueryError``
    .. _frame-phoneme-invalid-query-error: #voicevox_core.InvalidQueryError
    """

    phoneme: str
    """音素。"""

    frame_length: int
    """音素のフレーム長。"""

    note_id: NoteId | None = None
    """音符のID。"""

    def validate(self) -> None:
        """
        このインスタンスが不正であるときエラーを返す。

        不正であるとは、以下の条件を満たすことである。

        - |frame-phoneme-rust-ty|_ としてデシリアライズ不可。
            - :attr:`phoneme` が音素として不正。
            - :attr:`frame_length` が負であるか、もしくは :math:`2^{53}-1` を超過する。

        送出するエラーは |frame-phoneme-validate-invalid-query-error|_ 。

        .. |frame-phoneme-rust-ty| replace:: Rust APIの ``FramePhoneme`` 型
        .. _frame-phoneme-rust-ty: ../../../rust_api/voicevox_core/struct.FramePhoneme.html
        .. |frame-phoneme-validate-invalid-query-error| replace:: ``InvalidQueryError``
        .. _frame-phoneme-validate-invalid-query-error: #voicevox_core.InvalidQueryError
        """
        _validate_frame_phoneme(self)


@dataclasses.dataclass
class FrameAudioQuery:
    """
    フレームごとの音声合成用のクエリ。

    このクラスは不正な状態を表現しうる。どのような状態が不正なのかについては
    :func:`validate` を参照。このクラスを使う関数は、不正な状態に対して
    |frame-audio-query-invalid-query-error|_ を送出する。

    コンストラクト時には、不正な状態であるかの検証は行われない。

    .. |frame-audio-query-invalid-query-error| replace:: ``InvalidQueryError``
    .. _frame-audio-query-invalid-query-error: #voicevox_core.InvalidQueryError

    シリアライゼーションのサポートはされていない。詳細は
    `データのシリアライゼーション
    <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/serialization.md>`_
    を参照。
    """

    f0: list[float]
    """フレームごとの基本周波数。"""

    volume: list[float]
    """フレームごとの音量。"""

    phonemes: list[FramePhoneme]
    """音素のリスト。"""

    volume_scale: float
    """全体の音量。"""

    output_sampling_rate: int
    """音声データの出力サンプリングレート。"""

    output_stereo: bool
    """音声データをステレオ出力するか否か。"""

    def validate(self) -> None:
        """
        このインスタンスが不正であるときエラーを返す。

        不正であるとは、以下のいずれかの条件を満たすことである。

        - |frame-audio-query-rust-ty|_ としてデシリアライズ不可。
            - :attr:`f0` の要素がNaN、infinity、もしくは負。
            - :attr:`volume` の要素がNaNもしくは±infinity。
            - :attr:`volume_scale` がNaN、infinity、もしくは負。
            - :attr:`output_sampling_rate` 以下の値をとる。
                - ``0`` 以下の値。
                - :math:`2^{32}-1` を超過する値。
                - ``24000`` の倍数以外 (将来的に解消予定。cf. |frame-audio-query-validate-issue762|_)。
        - :attr:`phonemes` の要素のうちいずれかが |frame-audio-query-validate-frame-phoneme-validate|_ 。

        送出するエラーは |frame-audio-query-validate-invalid-query-error|_ 。

        .. |frame-audio-query-rust-ty| replace:: Rust APIの ``FrameAudioQuery`` 型
        .. _frame-audio-query-rust-ty: ../../../rust_api/voicevox_core/struct.FrameAudioQuery.html
        .. |frame-audio-query-validate-issue762| replace:: #762
        .. _frame-audio-query-validate-issue762: https://github.com/VOICEVOX/voicevox_core/issues/762
        .. |frame-audio-query-validate-frame-phoneme-validate| replace:: 不正
        .. _frame-audio-query-validate-frame-phoneme-validate: #voicevox_core.FramePhoneme.validate
        .. |frame-audio-query-validate-invalid-query-error| replace:: ``InvalidQueryError``
        .. _frame-audio-query-validate-invalid-query-error: #voicevox_core.InvalidQueryError
        """
        _validate_frame_audio_query(self)
