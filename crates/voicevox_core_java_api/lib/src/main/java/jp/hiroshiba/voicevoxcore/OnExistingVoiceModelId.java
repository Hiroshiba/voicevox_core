package jp.hiroshiba.voicevoxcore;

/**
 * {@link jp.hiroshiba.voicevoxcore.blocking.Synthesizer#loadVoiceModel
 * Synthesizer.loadVoiceModel}の実行時に、同じIDの{@link jp.hiroshiba.voicevoxcore.blocking.VoiceModelFile
 * VoiceModelFile}が既に読み込まれていたときのふるまい。
 */
public class OnExistingVoiceModelId {
  /**
   * エラー。
   *
   * <p>デフォルトのふるまい。
   */
  public static final OnExistingVoiceModelId ERROR = new OnExistingVoiceModelId("ERROR");

  /**
   * 再読み込みする。
   *
   * <p>VOICEVOX COREでは、長文のテキストを一度に音声合成するとCPU/GPUメモリが大量に占有されたままになる。再読み込みを行うとメモリの使用量が元に戻る。
   */
  public static final OnExistingVoiceModelId RELOAD = new OnExistingVoiceModelId("RELOAD");

  /** 何もしない。 */
  public static final OnExistingVoiceModelId SKIP = new OnExistingVoiceModelId("SKIP");

  private final String identifier;

  private OnExistingVoiceModelId(String identifier) {
    this.identifier = identifier;
  }

  @Override
  public String toString() {
    return identifier;
  }
}
