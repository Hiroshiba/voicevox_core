package jp.hiroshiba.voicevoxcore.blocking;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import jakarta.annotation.Nonnull;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;
import jp.hiroshiba.voicevoxcore.AccelerationMode;
import jp.hiroshiba.voicevoxcore.AccentPhrase;
import jp.hiroshiba.voicevoxcore.AudioQuery;
import jp.hiroshiba.voicevoxcore.CharacterMeta;
import jp.hiroshiba.voicevoxcore.FrameAudioQuery;
import jp.hiroshiba.voicevoxcore.OnExistingVoiceModelId;
import jp.hiroshiba.voicevoxcore.Score;
import jp.hiroshiba.voicevoxcore.StyleType;
import jp.hiroshiba.voicevoxcore.exceptions.IncompatibleQueriesException;
import jp.hiroshiba.voicevoxcore.exceptions.InvalidModelDataException;
import jp.hiroshiba.voicevoxcore.exceptions.InvalidQueryException;
import jp.hiroshiba.voicevoxcore.exceptions.RunModelException;
import jp.hiroshiba.voicevoxcore.exceptions.StyleNotFoundException;
import jp.hiroshiba.voicevoxcore.internal.Convert;
import jp.hiroshiba.voicevoxcore.internal.Dll;

/**
 * 音声シンセサイザ。
 *
 * @see Synthesizer#builder
 */
public class Synthesizer {
  static {
    Dll.loadLibrary();
  }

  private long handle;
  @Nonnull private final Onnxruntime onnxruntime;
  @Nonnull private final OpenJtalk openJtalk;

  private Synthesizer(Onnxruntime onnxruntime, OpenJtalk openJtalk, Builder builder) {
    rsNew(onnxruntime, openJtalk, builder);
    this.onnxruntime = onnxruntime;
    this.openJtalk = openJtalk;
  }

  protected void finalize() throws Throwable {
    rsDrop();
    super.finalize();
  }

  /**
   * ONNX Runtime。
   *
   * @return {@link Onnxruntime}。
   */
  @Nonnull
  public Onnxruntime getOnnxruntime() {
    return onnxruntime;
  }

  /**
   * Open JTalk。
   *
   * @return {@link OpenJtalk}。
   */
  @Nonnull
  public OpenJtalk getOpenJtalk() {
    return openJtalk;
  }

  /**
   * ハードウェアアクセラレーションがGPUモードかどうかを返す。
   *
   * @return ハードウェアアクセラレーションがGPUモードかどうか。
   */
  public boolean isGpuMode() {
    return rsIsGpuMode();
  }

  /**
   * メタ情報を取得する。
   *
   * @return メタ情報。
   */
  @Nonnull
  public CharacterMeta[] metas() {
    GsonBuilder gsonBuilder = new GsonBuilder();
    gsonBuilder.registerTypeAdapter(StyleType.class, new StyleType.Deserializer());
    Gson gson = gsonBuilder.create();
    String metasJson = rsGetMetasJson();
    CharacterMeta[] rawMetas = gson.fromJson(metasJson, CharacterMeta[].class);
    if (rawMetas == null) {
      throw new NullPointerException("metas");
    }
    return rawMetas;
  }

  /**
   * モデルを読み込む。
   *
   * @param voiceModel 読み込むモデル。
   * @return {@link LoadVoiceModelConfigurator}。
   */
  @Nonnull
  public LoadVoiceModelConfigurator loadVoiceModel(VoiceModelFile voiceModel) {
    return new LoadVoiceModelConfigurator(this, voiceModel);
  }

  /**
   * 音声モデルの読み込みを解除する。
   *
   * @param voiceModelId 読み込みを解除する音声モデルのID。
   */
  public void unloadVoiceModel(UUID voiceModelId) {
    rsUnloadVoiceModel(voiceModelId);
  }

  /**
   * 指定した音声モデルのIDが読み込まれているかどうかを返す。
   *
   * @param voiceModelId 音声モデルのID。
   * @return 指定した音声モデルのIDが読み込まれているかどうか。
   */
  public boolean isLoadedVoiceModel(UUID voiceModelId) {
    return rsIsLoadedVoiceModel(voiceModelId);
  }

  /**
   * AquesTalk風記法から {@link AudioQuery} を生成する。
   *
   * @param kana AquesTalk風記法。
   * @param styleId スタイルID。
   * @return {@link AudioQuery}。
   * @throws RunModelException 推論に失敗した場合。
   */
  @Nonnull
  public AudioQuery createAudioQueryFromKana(String kana, int styleId) throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }
    String queryJson = rsCreateAudioQueryFromKana(kana, styleId);
    Gson gson = new Gson();

    AudioQuery audioQuery = gson.fromJson(queryJson, AudioQuery.class);
    if (audioQuery == null) {
      throw new NullPointerException("audio_query");
    }
    return audioQuery;
  }

  /**
   * 日本語のテキストから {@link AudioQuery} を生成する。
   *
   * <p>{@link #createAccentPhrases}と{@link AudioQuery#fromAccentPhrases}を合わせたショートハンド。詳細は<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md">テキスト音声合成の流れ</a>を参照。
   *
   * @param text 日本語のテキスト。
   * @param styleId スタイルID。
   * @return {@link AudioQuery}。
   * @throws RunModelException 推論に失敗した場合。
   */
  @Nonnull
  public AudioQuery createAudioQuery(String text, int styleId) throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }
    String queryJson = rsCreateAudioQuery(text, styleId);
    Gson gson = new Gson();

    AudioQuery audioQuery = gson.fromJson(queryJson, AudioQuery.class);
    if (audioQuery == null) {
      throw new NullPointerException("audio_query");
    }
    return audioQuery;
  }

  /**
   * AquesTalk風記法から {@link AccentPhrase} のリストを生成する。
   *
   * @param kana AquesTalk風記法。
   * @param styleId スタイルID。
   * @return {@link AccentPhrase} のリスト。
   * @throws RunModelException 推論に失敗した場合。
   */
  @Nonnull
  public List<AccentPhrase> createAccentPhrasesFromKana(String kana, int styleId)
      throws RunModelException {
    String accentPhrasesJson = rsAccentPhrasesFromKana(kana, styleId);
    Gson gson = new Gson();
    AccentPhrase[] rawAccentPhrases = gson.fromJson(accentPhrasesJson, AccentPhrase[].class);
    if (rawAccentPhrases == null) {
      throw new NullPointerException("accent_phrases");
    }
    return new ArrayList<AccentPhrase>(Arrays.asList(rawAccentPhrases));
  }

  /**
   * 日本語のテキストから {@link AccentPhrase} のリストを生成する。
   *
   * <p>{@link OpenJtalk#analyze}と{@link #replaceMoraData}を合わせたショートハンド。詳細は<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md">テキスト音声合成の流れ</a>を参照。
   *
   * @param text 日本語のテキスト。
   * @param styleId スタイルID。
   * @return {@link AccentPhrase} のリスト。
   * @throws RunModelException 推論に失敗した場合。
   */
  @Nonnull
  public List<AccentPhrase> createAccentPhrases(String text, int styleId) throws RunModelException {
    String accentPhrasesJson = rsAccentPhrases(text, styleId);
    Gson gson = new Gson();
    AccentPhrase[] rawAccentPhrases = gson.fromJson(accentPhrasesJson, AccentPhrase[].class);
    if (rawAccentPhrases == null) {
      throw new NullPointerException("accent_phrases");
    }
    return new ArrayList<AccentPhrase>(Arrays.asList(rawAccentPhrases));
  }

  /**
   * アクセント句の音高・音素長を変更する。
   *
   * <p>{@link #replacePhonemeLength}と{@link #replaceMoraPitch}を合わせたショートハンド。詳細は<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md">テキスト音声合成の流れ</a>を参照。
   *
   * @param accentPhrases 変更元のアクセント句の配列。
   * @param styleId スタイルID。
   * @return 変更後のアクセント句の配列。
   * @throws RunModelException 推論に失敗した場合。
   */
  @Nonnull
  public List<AccentPhrase> replaceMoraData(List<AccentPhrase> accentPhrases, int styleId)
      throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }
    String accentPhrasesJson = Convert.jsonFromQueryLike(accentPhrases, "不正なアクセント句の列です");
    String replacedAccentPhrasesJson = rsReplaceMoraData(accentPhrasesJson, styleId, false);
    return new ArrayList<>(
        Arrays.asList(new Gson().fromJson(replacedAccentPhrasesJson, AccentPhrase[].class)));
  }

  /**
   * アクセント句の音素長を変更する。
   *
   * @param accentPhrases 変更元のアクセント句の配列。
   * @param styleId スタイルID。
   * @return 変更後のアクセント句の配列。
   * @throws RunModelException 推論に失敗した場合。
   */
  @Nonnull
  public List<AccentPhrase> replacePhonemeLength(List<AccentPhrase> accentPhrases, int styleId)
      throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }
    String accentPhrasesJson = Convert.jsonFromQueryLike(accentPhrases, "不正なアクセント句の列です");
    String replacedAccentPhrasesJson = rsReplacePhonemeLength(accentPhrasesJson, styleId, false);
    return new ArrayList<>(
        Arrays.asList(new Gson().fromJson(replacedAccentPhrasesJson, AccentPhrase[].class)));
  }

  /**
   * アクセント句の音高を変更する。
   *
   * @param accentPhrases 変更元のアクセント句の配列。
   * @param styleId スタイルID。
   * @return 変更後のアクセント句の配列。
   * @throws RunModelException 推論に失敗した場合。
   */
  @Nonnull
  public List<AccentPhrase> replaceMoraPitch(List<AccentPhrase> accentPhrases, int styleId)
      throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }
    String accentPhrasesJson = Convert.jsonFromQueryLike(accentPhrases, "不正なアクセント句の列です");
    String replacedAccentPhrasesJson = rsReplaceMoraPitch(accentPhrasesJson, styleId, false);
    return new ArrayList<>(
        Arrays.asList(new Gson().fromJson(replacedAccentPhrasesJson, AccentPhrase[].class)));
  }

  /**
   * {@link AudioQuery} から音声合成するためのオブジェクトを生成する。
   *
   * @param audioQuery {@link AudioQuery}。
   * @param styleId スタイルID。
   * @return {@link SynthesisConfigurator}。
   * @see SynthesisConfigurator#perform
   */
  @Nonnull
  public SynthesisConfigurator synthesis(AudioQuery audioQuery, int styleId) {
    return new SynthesisConfigurator(this, audioQuery, styleId);
  }

  /**
   * AquesTalk風記法をもとに音声合成を実行するためのオブジェクトを生成する。
   *
   * @param kana AquesTalk風記法。
   * @param styleId スタイルID。
   * @return {@link TtsFromKanaConfigurator}。
   * @see TtsFromKanaConfigurator#perform
   */
  @Nonnull
  public TtsFromKanaConfigurator ttsFromKana(String kana, int styleId) {
    return new TtsFromKanaConfigurator(this, kana, styleId);
  }

  /**
   * 日本語のテキストをもとに音声合成を実行するためのオブジェクトを生成する。
   *
   * <p>{@link #createAudioQuery}と{@link #synthesis}を合わせたショートハンド。詳細は<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/tts-process.md">テキスト音声合成の流れ</a>を参照。
   *
   * @param text 日本語のテキスト。
   * @param styleId スタイルID。
   * @return {@link TtsConfigurator}。
   * @see TtsConfigurator#perform
   */
  @Nonnull
  public TtsConfigurator tts(String text, int styleId) {
    return new TtsConfigurator(this, text, styleId);
  }

  /**
   * 楽譜から歌唱音声合成用のクエリを作成する。
   *
   * <p>詳細はユーザーガイド<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md">歌唱音声合成</a>を参照。
   *
   * @param score 楽譜
   * @param styleId スタイルID
   * @return 歌唱音声合成用のクエリ
   * @throws InvalidQueryException {@code score}に対する{@link Score#validate}が失敗する場合
   * @throws StyleNotFoundException {@code styleId}に対応するスタイルが見つからなかった場合
   * @throws RunModelException 推論に失敗した場合
   */
  @Nonnull
  public FrameAudioQuery createSingFrameAudioQuery(Score score, int styleId)
      throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }

    String scoreJson = Convert.jsonFromQueryLike(score, "不正な楽譜です");

    Gson gson = new Gson();
    String frameAudioQueryJson = rsCreateSingFrameAudioQuery(scoreJson, styleId);
    FrameAudioQuery frameAudioQuery = gson.fromJson(frameAudioQueryJson, FrameAudioQuery.class);
    if (frameAudioQuery == null) {
      throw new NullPointerException("frame_audio_query");
    }
    return frameAudioQuery;
  }

  /**
   * 楽譜と歌唱音声合成用のクエリから、フレームごとの基本周波数を生成する。
   *
   * <p>詳細はユーザーガイド<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md">歌唱音声合成</a>を参照。
   *
   * @param score 楽譜
   * @param frameAudioQuery 歌唱音声合成用のクエリ
   * @param styleId スタイルID
   * @return フレームごとの基本周波数
   * @throws InvalidQueryException {@code score}に対する{@link Score#validate}、もしくは{@code
   *     frameAudioQuery}に対する{@link FrameAudioQuery#validate}が失敗する場合
   * @throws IncompatibleQueriesException {@code score}と{@code FrameAudioQuery}の組み合わせが不正である場合
   * @throws StyleNotFoundException {@code styleId}に対応するスタイルが見つからなかった場合
   * @throws RunModelException 推論に失敗した場合
   */
  @Nonnull
  public float[] createSingFrameF0(Score score, FrameAudioQuery frameAudioQuery, int styleId)
      throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }

    String scoreJson = Convert.jsonFromQueryLike(score, "不正な楽譜です");
    String frameAudioQueryJson = Convert.jsonFromQueryLike(frameAudioQuery, "不正なFrameAudioQueryです");

    return rsCreateSingFrameF0(scoreJson, frameAudioQueryJson, styleId);
  }

  /**
   * 楽譜と歌唱音声合成用のクエリから、フレームごとの音量を生成する。
   *
   * <p>詳細はユーザーガイド<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md">歌唱音声合成</a>を参照。
   *
   * @param score 楽譜
   * @param frameAudioQuery 歌唱音声合成用のクエリ
   * @param styleId スタイルID
   * @return フレームごとの音量
   * @throws InvalidQueryException {@code score}に対する{@link Score#validate}、もしくは{@code
   *     frameAudioQuery}に対する{@link FrameAudioQuery#validate}が失敗する場合
   * @throws IncompatibleQueriesException {@code score}と{@code FrameAudioQuery}の組み合わせが不正である場合
   * @throws StyleNotFoundException {@code styleId}に対応するスタイルが見つからなかった場合
   * @throws RunModelException 推論に失敗した場合
   */
  @Nonnull
  public float[] createSingFrameVolume(Score score, FrameAudioQuery frameAudioQuery, int styleId)
      throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }

    String scoreJson = Convert.jsonFromQueryLike(score, "不正な楽譜です");
    String frameAudioQueryJson = Convert.jsonFromQueryLike(frameAudioQuery, "不正なFrameAudioQueryです");

    return rsCreateSingFrameVolume(scoreJson, frameAudioQueryJson, styleId);
  }

  /**
   * 歌唱音声合成を行う。
   *
   * <p>詳細はユーザーガイド<a
   * href="https://github.com/VOICEVOX/voicevox_core/blob/main/docs/guide/user/song.md">歌唱音声合成</a>を参照。
   *
   * @param frameAudioQuery 歌唱音声合成用のクエリ
   * @param styleId スタイルID
   * @return WAVデータ
   * @throws InvalidQueryException {@code frameAudioQuery}に対する{@link
   *     FrameAudioQuery#validate}が失敗する場合
   * @throws StyleNotFoundException {@code styleId}に対応するスタイルが見つからなかった場合
   * @throws RunModelException 推論に失敗した場合
   */
  @Nonnull
  public byte[] frameSynthesis(FrameAudioQuery frameAudioQuery, int styleId)
      throws RunModelException {
    if (!Utils.isU32(styleId)) {
      throw new IllegalArgumentException("styleId");
    }

    String frameAudioQueryJson = Convert.jsonFromQueryLike(frameAudioQuery, "不正なFrameAudioQueryです");

    return rsFrameSynthesis(frameAudioQueryJson, styleId);
  }

  private native void rsNew(Onnxruntime onnxruntime, OpenJtalk openJtalk, Builder builder);

  private native boolean rsIsGpuMode();

  @Nonnull
  private native String rsGetMetasJson();

  private native void rsLoadVoiceModel(VoiceModelFile voiceModel, OnExistingVoiceModelId onExisting)
      throws InvalidModelDataException;

  private native void rsUnloadVoiceModel(UUID voiceModelId);

  private native boolean rsIsLoadedVoiceModel(UUID voiceModelId);

  @Nonnull
  private native String rsCreateAudioQueryFromKana(String kana, int styleId)
      throws RunModelException;

  @Nonnull
  private native String rsCreateAudioQuery(String text, int styleId) throws RunModelException;

  @Nonnull
  private native String rsAccentPhrasesFromKana(String kana, int styleId) throws RunModelException;

  @Nonnull
  private native String rsAccentPhrases(String text, int styleId) throws RunModelException;

  @Nonnull
  private native String rsReplaceMoraData(String accentPhrasesJson, int styleId, boolean kana)
      throws RunModelException;

  @Nonnull
  private native String rsReplacePhonemeLength(String accentPhrasesJson, int styleId, boolean kana)
      throws RunModelException;

  @Nonnull
  private native String rsReplaceMoraPitch(String accentPhrasesJson, int styleId, boolean kana)
      throws RunModelException;

  @Nonnull
  private native byte[] rsSynthesis(
      String queryJson, int styleId, boolean enableInterrogativeUpspeak) throws RunModelException;

  @Nonnull
  private native byte[] rsTtsFromKana(String kana, int styleId, boolean enableInterrogativeUpspeak)
      throws RunModelException;

  @Nonnull
  private native byte[] rsTts(String text, int styleId, boolean enableInterrogativeUpspeak)
      throws RunModelException;

  @Nonnull
  private native String rsCreateSingFrameAudioQuery(String score, int styleId)
      throws RunModelException;

  @Nonnull
  private native float[] rsCreateSingFrameF0(String score, String frameAudioQuery, int styleId)
      throws RunModelException;

  @Nonnull
  private native float[] rsCreateSingFrameVolume(String score, String frameAudioQuery, int styleId)
      throws RunModelException;

  @Nonnull
  private native byte[] rsFrameSynthesis(String frameAudioQuery, int styleId)
      throws RunModelException;

  private native void rsDrop();

  public static Builder builder(Onnxruntime onnxruntime, OpenJtalk openJtalk) {
    return new Builder(onnxruntime, openJtalk);
  }

  /**
   * 音声シンセサイザのビルダー。
   *
   * @see Synthesizer#builder
   */
  public static class Builder {
    private Onnxruntime onnxruntime;
    private OpenJtalk openJtalk;

    @SuppressWarnings("unused")
    private AccelerationMode accelerationMode;

    @SuppressWarnings("unused")
    private int cpuNumThreads;

    public Builder(Onnxruntime onnxruntime, OpenJtalk openJtalk) {
      this.onnxruntime = onnxruntime;
      this.openJtalk = openJtalk;
    }

    /**
     * ハードウェアアクセラレーションモードを設定する。
     *
     * @param accelerationMode ハードウェアアクセラレーションモード。
     * @return ビルダー。
     */
    public Builder accelerationMode(AccelerationMode accelerationMode) {
      this.accelerationMode = accelerationMode;
      return this;
    }

    /**
     * CPU利用数を指定する。0を指定すると環境に合わせたCPUが利用される。
     *
     * @param cpuNumThreads CPU利用数。
     * @return ビルダー。
     */
    public Builder cpuNumThreads(int cpuNumThreads) {
      if (Utils.isU16(cpuNumThreads)) {
        throw new IllegalArgumentException("cpuNumThreads");
      }
      this.cpuNumThreads = cpuNumThreads;
      return this;
    }

    /**
     * {@link Synthesizer} を構築する。
     *
     * @return {@link Synthesizer}。
     */
    public Synthesizer build() {
      Synthesizer synthesizer = new Synthesizer(onnxruntime, openJtalk, this);
      return synthesizer;
    }
  }

  /** {@link Synthesizer#loadVoiceModel} のオプション。 */
  public class LoadVoiceModelConfigurator {
    private Synthesizer synthesizer;
    private VoiceModelFile voiceModel;

    private OnExistingVoiceModelId onExisting;

    private LoadVoiceModelConfigurator(Synthesizer synthesizer, VoiceModelFile voiceModel) {
      this.synthesizer = synthesizer;
      this.voiceModel = voiceModel;
    }

    /**
     * 同じIDの{@link VoiceModelFile}が既に読み込まれていたときのふるまい。
     *
     * @param onExisting ふるまいの指定。
     * @return {@link LoadVoiceModelConfigurator}。
     */
    @Nonnull
    public LoadVoiceModelConfigurator onExisting(OnExistingVoiceModelId onExisting) {
      this.onExisting = onExisting;
      return this;
    }

    /**
     * 実行する。
     *
     * @throws InvalidModelDataException 無効なモデルデータの場合。
     */
    public void perform() throws InvalidModelDataException {
      synthesizer.rsLoadVoiceModel(voiceModel, onExisting);
    }
  }

  /** {@link Synthesizer#synthesis} のオプション。 */
  public class SynthesisConfigurator {
    private Synthesizer synthesizer;
    private AudioQuery audioQuery;
    private int styleId;
    private boolean interrogativeUpspeak;

    private SynthesisConfigurator(Synthesizer synthesizer, AudioQuery audioQuery, int styleId) {
      if (!Utils.isU32(styleId)) {
        throw new IllegalArgumentException("styleId");
      }
      this.synthesizer = synthesizer;
      this.audioQuery = audioQuery;
      this.styleId = styleId;
      this.interrogativeUpspeak = false;
    }

    /**
     * 疑問文の調整を有効にするかどうか。
     *
     * @param interrogativeUpspeak 疑問文の調整を有効にするかどうか。
     * @return {@link SynthesisConfigurator}。
     */
    @Nonnull
    public SynthesisConfigurator interrogativeUpspeak(boolean interrogativeUpspeak) {
      this.interrogativeUpspeak = interrogativeUpspeak;
      return this;
    }

    /**
     * {@link AudioQuery} から音声合成する。
     *
     * @return 音声データ。
     * @throws RunModelException 推論に失敗した場合。
     */
    @Nonnull
    public byte[] perform() throws RunModelException {
      if (!Utils.isU32(styleId)) {
        throw new IllegalArgumentException("styleId");
      }
      String queryJson = Convert.jsonFromQueryLike(this.audioQuery, "不正なAudioQueryです");
      return synthesizer.rsSynthesis(queryJson, this.styleId, this.interrogativeUpspeak);
    }
  }

  /** {@link Synthesizer#ttsFromKana} のオプション。 */
  public class TtsFromKanaConfigurator {
    private Synthesizer synthesizer;
    private String kana;
    private int styleId;
    private boolean interrogativeUpspeak;

    private TtsFromKanaConfigurator(Synthesizer synthesizer, String kana, int styleId) {
      if (!Utils.isU32(styleId)) {
        throw new IllegalArgumentException("styleId");
      }
      this.synthesizer = synthesizer;
      this.kana = kana;
      this.styleId = styleId;
    }

    /**
     * 疑問文の調整を有効にするかどうか。
     *
     * @param interrogativeUpspeak 疑問文の調整を有効にするかどうか。
     * @return {@link SynthesisConfigurator}。
     */
    @Nonnull
    public TtsFromKanaConfigurator interrogativeUpspeak(boolean interrogativeUpspeak) {
      this.interrogativeUpspeak = interrogativeUpspeak;
      return this;
    }

    /**
     * {@link AudioQuery} から音声合成する。
     *
     * @return 音声データ。
     * @throws RunModelException 推論に失敗した場合。
     */
    @Nonnull
    public byte[] perform() throws RunModelException {
      if (!Utils.isU32(styleId)) {
        throw new IllegalArgumentException("styleId");
      }
      return synthesizer.rsTtsFromKana(this.kana, this.styleId, this.interrogativeUpspeak);
    }
  }

  /** {@link Synthesizer#tts} のオプション。 */
  public class TtsConfigurator {
    private Synthesizer synthesizer;
    private String text;
    private int styleId;
    private boolean interrogativeUpspeak;

    private TtsConfigurator(Synthesizer synthesizer, String text, int styleId) {
      if (!Utils.isU32(styleId)) {
        throw new IllegalArgumentException("styleId");
      }
      this.synthesizer = synthesizer;
      this.text = text;
      this.styleId = styleId;
    }

    /**
     * 疑問文の調整を有効にするかどうか。
     *
     * @param interrogativeUpspeak 疑問文の調整を有効にするかどうか。
     * @return {@link SynthesisConfigurator}。
     */
    @Nonnull
    public TtsConfigurator interrogativeUpspeak(boolean interrogativeUpspeak) {
      this.interrogativeUpspeak = interrogativeUpspeak;
      return this;
    }

    /**
     * {@link AudioQuery} から音声合成する。
     *
     * @return 音声データ。
     * @throws RunModelException 推論に失敗した場合。
     */
    @Nonnull
    public byte[] perform() throws RunModelException {
      if (!Utils.isU32(styleId)) {
        throw new IllegalArgumentException("styleId");
      }
      return synthesizer.rsTts(this.text, this.styleId, this.interrogativeUpspeak);
    }
  }
}
