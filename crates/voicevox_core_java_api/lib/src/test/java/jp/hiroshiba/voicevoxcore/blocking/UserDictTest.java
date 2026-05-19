package jp.hiroshiba.voicevoxcore.blocking;

import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;
import jp.hiroshiba.voicevoxcore.AudioQuery;
import jp.hiroshiba.voicevoxcore.TestUtils;
import jp.hiroshiba.voicevoxcore.UserDictWord;
import jp.hiroshiba.voicevoxcore.exceptions.InvalidModelDataException;
import jp.hiroshiba.voicevoxcore.exceptions.LoadUserDictException;
import jp.hiroshiba.voicevoxcore.exceptions.RunModelException;
import org.junit.jupiter.api.Test;

class UserDictTest extends TestUtils {

  // 辞書ロードのテスト。
  // 辞書ロード前後でkanaが異なることを確認する
  @Test
  void checkLoad() throws RunModelException, InvalidModelDataException, LoadUserDictException {
    Onnxruntime onnxruntime = loadOnnxruntime();
    OpenJtalk openJtalk = loadOpenJtalk();
    Synthesizer synthesizer = Synthesizer.builder(onnxruntime, openJtalk).build();
    UserDict userDict = new UserDict();
    try (VoiceModelFile model = openModel()) {
      synthesizer.loadVoiceModel(model).perform();
    }
    AudioQuery query1 =
        synthesizer.createAudioQuery(
            "this_word_should_not_exist_in_default_dictionary",
            synthesizer.metas()[0].styles[0].id);

    userDict.addWord(
        new UserDictWord("this_word_should_not_exist_in_default_dictionary", "テスト", 1));
    openJtalk.useUserDict(userDict);
    AudioQuery query2 =
        synthesizer.createAudioQuery(
            "this_word_should_not_exist_in_default_dictionary",
            synthesizer.metas()[0].styles[0].id);
    assertTrue(query1.kana != query2.kana);
  }

  // 辞書操作のテスト。
  @Test
  void checkManipulation() throws Exception {
    UserDict userDict = new UserDict();
    // 単語追加
    UUID uuid = userDict.addWord(new UserDictWord("hoge", "ホゲ", 0));
    assertTrue(userDict.toHashMap().get(uuid) != null);

    // 単語更新
    userDict.updateWord(uuid, new UserDictWord("hoge", "ホゲホゲ", 0));
    assertTrue(userDict.toHashMap().get(uuid).pronunciation.equals("ホゲホゲ"));

    // 単語削除
    userDict.removeWord(uuid);
    assertTrue(userDict.toHashMap().get(uuid) == null);

    // 辞書のインポート
    userDict.addWord(new UserDictWord("hoge", "ホゲ", 0));
    UserDict userDict2 = new UserDict();
    userDict2.addWord(new UserDictWord("fuga", "フガ", 0));
    userDict.importDict(userDict2);
    assertTrue(userDict.toHashMap().size() == 2);

    // 辞書の保存/読み込み
    Path path = Files.createTempFile("voicevox_user_dict", ".json");
    try {
      UserDict userDict3 = new UserDict();
      userDict.save(path.toString());
      userDict3.load(path.toString());
      assertTrue(userDict3.toHashMap().size() == 2);
    } finally {
      Files.deleteIfExists(path);
    }
  }
}
