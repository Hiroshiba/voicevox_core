from os import PathLike
from typing import TYPE_CHECKING, NoReturn, Union
from uuid import UUID

if TYPE_CHECKING:
    from voicevox_core import (
        AccelerationMode,
        AccentPhrase,
        AudioQuery,
        CharacterMeta,
        FrameAudioQuery,
        OnExistingVoiceModelId,
        Score,
        StyleId,
        SupportedDevices,
        UserDictWord,
        VoiceModelId,
    )

class VoiceModelFile:
    """
    音声モデルファイル。"""

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn: ...
    @staticmethod
    def open(path: str | PathLike[str]) -> VoiceModelFile:
        """
        VVMファイルを開く。

        Parameters
        ----------
        path
            VVMファイルへのパス。
        """
        ...
    def __repr__(self) -> str: ...
    def close(self) -> None:
        """
        VVMファイルを閉じる。

        このメソッドが呼ばれた段階で :attr:`Synthesizer.load_voice_model`
        からのアクセスが継続中の場合、アクセスが終わるまで待つ。
        """
        ...
    @property
    def id(self) -> VoiceModelId:
        """
        ID。

        詳細は :class:`voicevox_core.VoiceModelId` を参照。

        :attr:`close` および :attr:`__exit__` の後でも利用可能。
        """
        ...
    @property
    def metas(self) -> list[CharacterMeta]:
        """
        メタ情報。

        この中身を書き換えても、 ``VoiceModelFile`` としての動作には影響しない。

        :attr:`close` および :attr:`__exit__` の後でも利用可能。
        """
        ...
    def __enter__(self) -> "VoiceModelFile": ...
    def __exit__(self, exc_type, exc_value, traceback) -> None: ...

class Onnxruntime:
    """
    ONNX Runtime。

    シングルトンであり、インスタンスは高々一つ。

    .. code-block::

        ort1 = Onnxruntime.load_once()
        ort2 = Onnxruntime.get()
        assert ort2
        assert ort2 is ort1

    .. code-block::

        ort = voicevox_core.blocking.Onnxruntime.load_once()
        assert voicevox_core.asyncio.Onnxruntime.get()
    """

    # ここの定数値が本物と合致するかどうかは、test_type_stub_consts.pyで担保する。

    LIB_NAME: str = "voicevox_onnxruntime"
    """ONNX Runtimeのライブラリ名。"""

    LIB_VERSION: str = "1.17.3"
    """推奨されるONNX Runtimeのバージョン。"""

    LIB_VERSIONED_FILENAME: str
    """
    :attr:`LIB_NAME` と :attr:`LIB_VERSION` からなる動的ライブラリのファイル名。

    WindowsとAndroidでは :attr:`LIB_UNVERSIONED_FILENAME` と同じ。
    """

    LIB_UNVERSIONED_FILENAME: str
    """:attr:`LIB_NAME` からなる動的ライブラリのファイル名。"""

    def __new__(cls, *args: object, **kwargs: object) -> NoReturn: ...
    @staticmethod
    def get() -> Union["Onnxruntime", None]:
        """
        インスタンスが既に作られているならそれを得る。

        作られていなければ ``None`` を返す。
        """
        ...
    @staticmethod
    def load_once(*, filename: str = LIB_VERSIONED_FILENAME) -> "Onnxruntime":
        """
        ONNX Runtimeをロードして初期化する。

        一度成功したら、以後は引数を無視して同じインスタンスを返す。

        Parameters
        ----------
        filename
            ONNX Runtimeのファイル名（モジュール名）もしくはファイルパス。
            ``dlopen``/`LoadLibraryExW
            <https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryexw>`_
            の引数に使われる。
        """
        ...
    def __repr__(self) -> str: ...
    def supported_devices(self) -> SupportedDevices:
        """
        このライブラリで利用可能なデバイスの情報を取得する。
        """
        ...

class OpenJtalk:
    """
    テキスト解析器としてのOpen JTalk。

    Parameters
    ----------
    open_jtalk_dict_dir
        Open JTalkの辞書ディレクトリ。
    """

    def __init__(self, open_jtalk_dict_dir: str | PathLike[str]) -> None: ...
    def __repr__(self) -> str: ...
    def use_user_dict(self, user_dict: UserDict) -> None:
        """
        ユーザー辞書を設定する。

        この関数を呼び出した後にユーザー辞書を変更した場合は、再度この関数を呼ぶ必要がある。

        Parameters
        ----------
        user_dict
            ユーザー辞書。
        """
        ...
    def analyze(self, text: str) -> list[AccentPhrase]:
        """
        日本語のテキストを解析する。

        Parameters
        ----------
        text
            日本語のテキスト。
        """
        ...

class AudioFeature:
    @property
    def frame_length(self) -> int: ...
    @property
    def frame_rate(self) -> float: ...
    def __repr__(self) -> str: ...
    def __eq__(self, other: object) -> bool: ...

class Synthesizer:
    """
    音声シンセサイザ。

    Parameters
    ----------
    onnxruntime
        ONNX Runtime。
    open_jtalk
        Open JTalk。
    acceleration_mode
        ハードウェアアクセラレーションモード。
    cpu_num_threads
        CPU利用数を指定。0を指定すると環境に合わせたCPUが利用される。
    """

    def __init__(
        self,
        onnxruntime: Onnxruntime,
        open_jtalk: OpenJtalk,
        *,
        acceleration_mode: AccelerationMode = "AUTO",
        cpu_num_threads: int = 0,
    ) -> None: ...
    def __repr__(self) -> str: ...
    def __enter__(self) -> "Synthesizer": ...
    def __exit__(self, exc_type, exc_value, traceback) -> None: ...
    @property
    def onnxruntime(self) -> Onnxruntime:
        """ONNX Runtime。"""
        ...
    @property
    def open_jtalk(self) -> OpenJtalk:
        """Open JTalk。"""
        ...
    @property
    def is_gpu_mode(self) -> bool:
        """ハードウェアアクセラレーションがGPUモードかどうか。"""
        ...
    def metas(self) -> list[CharacterMeta]:
        """メタ情報。"""
        ...
    def load_voice_model(
        self,
        model: VoiceModelFile,
        *,
        on_existing: OnExistingVoiceModelId = "ERROR",
    ) -> None:
        """
        モデルを読み込む。

        Parameters
        ----------
        model
            読み込むモデル。
        on_existing
            同じ ``id`` の :class:`VoiceModelFile` が既に読み込まれていたときのふるまい。
        """
        ...
    def unload_voice_model(self, voice_model_id: VoiceModelId | UUID) -> None:
        """
        音声モデルの読み込みを解除する。

        Parameters
        ----------
        voice_model_id
            音声モデルID。
        """
        ...
    def is_loaded_voice_model(self, voice_model_id: VoiceModelId | UUID) -> bool:
        """
        指定したvoice_model_idのモデルが読み込まれているか判定する。

        Parameters
        ----------
        voice_model_id
            音声モデルID。

        Returns
        -------
        モデルが読み込まれているかどうか。
        """
        ...
    def create_audio_query_from_kana(
        self,
        kana: str,
        style_id: StyleId | int,
    ) -> AudioQuery:
        """
        AquesTalk風記法から :class:`AudioQuery` を生成する。

        Parameters
        ----------
        kana
            AquesTalk風記法。
        style_id
            スタイルID。

        Returns
        -------
        スタイルとテキストから生成された :class:`AudioQuery` 。
        """
        ...
    def create_audio_query(
        self,
        text: str,
        style_id: StyleId | int,
    ) -> AudioQuery:
        """
        日本語のテキストから :class:`AudioQuery` を生成する。

        :func:`create_accent_phrases` と |from-accent-phrases|_
        が一体になったショートハンド。詳細は `テキスト音声合成の流れ
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md>`_
        を参照。

        .. |from-accent-phrases| replace:: ``AudioQuery.from_accent_phrases()``
        .. _from-accent-phrases: ../index.html#voicevox_core.AudioQuery.from_accent_phrases

        Parameters
        ----------
        text
            UTF-8の日本語テキスト。
        style_id
            スタイルID。

        Returns
        -------
        スタイルとテキストから生成された :class:`AudioQuery` 。
        """
        ...
    def create_accent_phrases_from_kana(
        self,
        kana: str,
        style_id: StyleId | int,
    ) -> list[AccentPhrase]:
        """
        AquesTalk風記法からAccentPhrase（アクセント句）の配列を生成する。

        Parameters
        ----------
        kana
            AquesTalk風記法。
        style_id
            スタイルID。

        Returns
        -------
        :class:`AccentPhrase` の配列。
        """
        ...
    def create_accent_phrases(
        self,
        text: str,
        style_id: StyleId | int,
    ) -> list[AccentPhrase]:
        """
        日本語のテキストからAccentPhrase（アクセント句）の配列を生成する。

        :func:`OpenJtalk.analyze` と :func:`replace_mora_data`
        が一体になったショートハンド。詳細は `テキスト音声合成の流れ
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md>`_
        を参照。

        Parameters
        ----------
        text
            UTF-8の日本語テキスト。
        style_id
            スタイルID。

        Returns
        -------
        :class:`AccentPhrase` の配列。
        """
        ...
    def replace_mora_data(
        self,
        accent_phrases: list[AccentPhrase],
        style_id: StyleId | int,
    ) -> list[AccentPhrase]:
        """
        アクセント句の音高・音素長を変更した新しいアクセント句の配列を生成する。

        元のアクセント句の音高・音素長は変更されない。

        :func:`replace_phoneme_length` と :func:`replace_mora_pitch`
        が一体になったショートハンド。詳細は `テキスト音声合成の流れ
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md>`_
        を参照。

        Parameters
        ----------
        accent_phrases:
            変更元のアクセント句。
        style_id:
            スタイルID。

        Returns
        -------
        新しいアクセント句の配列。
        """
        ...
    def replace_phoneme_length(
        self,
        accent_phrases: list[AccentPhrase],
        style_id: StyleId | int,
    ) -> list[AccentPhrase]:
        """
        アクセント句の音素長を変更した新しいアクセント句の配列を生成する。

        元のアクセント句の音素長は変更されない。

        Parameters
        ----------
        accent_phrases
            変更元のアクセント句。
        style_id
            スタイルID。
        """
        ...
    def replace_mora_pitch(
        self,
        accent_phrases: list[AccentPhrase],
        style_id: StyleId | int,
    ) -> list[AccentPhrase]:
        """
        アクセント句の音高を変更した新しいアクセント句の配列を生成する。

        元のアクセント句の音高は変更されない。

        Parameters
        ----------
        accent_phrases
            変更元のアクセント句。
        style_id
            スタイルID。
        """
        ...
    def __precompute_render(
        self,
        audio_query: AudioQuery,
        style_id: StyleId | int,
        *,
        enable_interrogative_upspeak: bool = True,
    ) -> AudioFeature: ...
    def __render(
        self,
        audio: AudioFeature,
        start: int,
        stop: int,
    ) -> bytes: ...
    def synthesis(
        self,
        audio_query: AudioQuery,
        style_id: StyleId | int,
        *,
        enable_interrogative_upspeak: bool = True,
    ) -> bytes:
        """
        :class:`AudioQuery` から音声合成する。

        Parameters
        ----------
        audio_query
            :class:`AudioQuery` 。
        style_id
            スタイルID。
        enable_interrogative_upspeak
            疑問文の調整を有効にするかどうか。

        Returns
        -------
        WAVデータ。
        """
        ...
    def tts_from_kana(
        self,
        kana: str,
        style_id: StyleId | int,
        *,
        enable_interrogative_upspeak: bool = True,
    ) -> bytes:
        """
        AquesTalk風記法から音声合成を行う。

        Parameters
        ----------
        kana
            AquesTalk風記法。
        style_id
            スタイルID。
        enable_interrogative_upspeak
            疑問文の調整を有効にするかどうか。
        """
        ...
    def tts(
        self,
        text: str,
        style_id: StyleId | int,
        *,
        enable_interrogative_upspeak: bool = True,
    ) -> bytes:
        """
        日本語のテキストから音声合成を行う。

        :func:`create_audio_query` と :func:`synthesis`
        が一体になったショートハンド。詳細は `テキスト音声合成の流れ
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md>`_
        を参照。

        Parameters
        ----------
        text
            UTF-8の日本語テキスト。
        style_id
            スタイルID。
        enable_interrogative_upspeak
            疑問文の調整を有効にするかどうか。

        Returns
        -------
        WAVデータ。
        """
        ...
    def create_sing_frame_audio_query(
        self,
        score: Score,
        style_id: StyleId | int,
    ) -> FrameAudioQuery:
        """
        |blocking-synthesizer-create-sing-frame-audio-query-score|_ から |blocking-synthesizer-create-sing-frame-audio-query-frame-audio-query| を作成する。

        詳細はユーザーガイド `歌唱音声合成
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md>`_
        を参照。

        .. code-block::

            from voicevox_core import Note, Score

            SINGING_TEACHER = 6000

            SCORE = Score(
                [
                    Note(15, ""),
                    Note(45, "ド", key=60),
                    Note(45, "レ", key=62),
                    Note(45, "ミ", key=64),
                    Note(15, ""),
                ],
            )

            frame_audio_query = synthesizer.create_sing_frame_audio_query(
                SCORE, SINGING_TEACHER
            )

        .. |blocking-synthesizer-create-sing-frame-audio-query-score| replace:: 楽譜
        .. _blocking-synthesizer-create-sing-frame-audio-query-score: ../index.html#voicevox_core.Score
        .. |blocking-synthesizer-create-sing-frame-audio-query-frame-audio-query| replace:: 歌唱音声合成用のクエリ
        .. _blocking-synthesizer-create-sing-frame-audio-query-frame-audio-query: ../index.html#voicevox_core.FrameAudioQuery

        Parameters
        ----------
        score
            楽譜。
        style_id
            スタイルID。

        Returns
        -------
        歌唱音声合成用のクエリ。
        """
        ...
    def create_sing_frame_f0(
        self,
        score: Score,
        frame_audio_query: FrameAudioQuery,
        style_id: StyleId | int,
    ) -> list[float]:
        """
        |blocking-synthesizer-create-sing-frame-f0-score|_ と |blocking-synthesizer-create-sing-frame-f0-frame-audio-query| から、フレームごとの基本周波数を生成する。

        詳細はユーザーガイド `歌唱音声合成
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md>`_
        を参照。

        .. code-block::

            SINGING_TEACHER = 6000

            frame_audio_query = synthesizer.create_sing_frame_audio_query(
                score, SINGING_TEACHER
            )

            # `Note.key`や`FramePhoneme.frame_length`を変更
            for note in score.notes:
                if note.key is not None:
                    note.key += 1

            new_f0 = synthesizer.create_sing_frame_f0(
                score, frame_audio_query, SINGING_TEACHER
            )
            frame_audio_query.f0 = new_f0

        .. |blocking-synthesizer-create-sing-frame-f0-score| replace:: 楽譜
        .. _blocking-synthesizer-create-sing-frame-f0-score: ../index.html#voicevox_core.Score
        .. |blocking-synthesizer-create-sing-frame-f0-frame-audio-query| replace:: 歌唱音声合成用のクエリ
        .. _blocking-synthesizer-create-sing-frame-f0-frame-audio-query: ../index.html#voicevox_core.FrameAudioQuery

        Parameters
        ----------
        score
            楽譜。
        frame_audio_query
            歌唱音声合成用のクエリ。
        style_id
            スタイルID。

        Returns
        -------
        フレームごとの基本周波数。
        """
        ...
    def create_sing_frame_volume(
        self,
        score: Score,
        frame_audio_query: FrameAudioQuery,
        style_id: StyleId | int,
    ) -> list[float]:
        """
        |blocking-synthesizer-create-sing-frame-volume-score|_ と |blocking-synthesizer-create-sing-frame-volume-frame-audio-query| から、フレームごとの音量を生成する。

        詳細はユーザーガイド `歌唱音声合成
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md>`_
        を参照。

        .. code-block::

            SINGING_TEACHER = 6000

            frame_audio_query = synthesizer.create_sing_frame_audio_query(
                score, SINGING_TEACHER
            )

            # `Note.key`や`FramePhoneme.frame_length`を変更
            for note in score.notes:
                if note.key is not None:
                    note.key += 1

            new_f0 = synthesizer.create_sing_frame_f0(
                score, frame_audio_query, SINGING_TEACHER
            )
            frame_audio_query.f0 = new_f0

            new_volume = synthesizer.create_sing_frame_volume(
                score, frame_audio_query, SINGING_TEACHER
            )
            frame_audio_query.volume = new_volume

        .. |blocking-synthesizer-create-sing-frame-volume-score| replace:: 楽譜
        .. _blocking-synthesizer-create-sing-frame-volume-score: ../index.html#voicevox_core.Score
        .. |blocking-synthesizer-create-sing-frame-volume-frame-audio-query| replace:: 歌唱音声合成用のクエリ
        .. _blocking-synthesizer-create-sing-frame-volume-frame-audio-query: ../index.html#voicevox_core.FrameAudioQuery

        Parameters
        ----------
        score
            楽譜。
        frame_audio_query
            歌唱音声合成用のクエリ。
        style_id
            スタイルID。

        Returns
        -------
        フレームごとの音量。
        """
        ...
    def frame_synthesis(
        self,
        frame_audio_query: FrameAudioQuery,
        style_id: StyleId | int,
    ) -> bytes:
        """
        歌唱音声合成を行う。

        詳細はユーザーガイド `歌唱音声合成
        <https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md>`_
        を参照。

        .. code-block::

            SINGER = 3000

            wav = synthesizer.frame_synthesis(frame_audio_query, SINGER)

        Parameters
        ----------
        frame_audio_query
            歌唱音声合成用のクエリ。
        style_id
            スタイルID。

        Returns
        -------
        WAVデータ。
        """
        ...
    def close(self) -> None: ...

class UserDict:
    """ユーザー辞書。"""

    def to_dict(self) -> dict[UUID, UserDictWord]:
        """このオプジェクトを :class:`dict` に変換する。"""
        ...
    def __init__(self) -> None: ...
    def __repr__(self) -> str: ...
    def load(self, path: str | PathLike[str]) -> None:
        """ファイルに保存されたユーザー辞書を読み込む。

        Parameters
        ----------
        path
            ユーザー辞書のパス。
        """
        ...
    def save(self, path: str | PathLike[str]) -> None:
        """
        ユーザー辞書をファイルに保存する。

        Parameters
        ----------
        path
            ユーザー辞書のパス。
        """
        ...
    def add_word(self, word: UserDictWord) -> UUID:
        """
        単語を追加する。

        Parameters
        ----------
        word
            追加する単語。

        Returns
        -------
        単語のUUID。
        """
        ...
    def update_word(self, word_uuid: UUID, word: UserDictWord) -> None:
        """
        単語を更新する。

        Parameters
        ----------
        word_uuid
            更新する単語のUUID。
        word
            新しい単語のデータ。
        """
        ...
    def remove_word(self, word_uuid: UUID) -> None:
        """
        単語を削除する。

        Parameters
        ----------
        word_uuid
            削除する単語のUUID。
        """
        ...
    def import_dict(self, other: UserDict) -> None:
        """
        ユーザー辞書をインポートする。

        Parameters
        ----------
        other
            インポートするユーザー辞書。
        """
        ...
