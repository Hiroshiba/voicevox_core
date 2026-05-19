/**
 * テキスト音声合成を行う。
 */

// TODO: 実装スタイルをunix/song.cppに揃える
// TODO: ファイル名をunix/song.cppに寄せる

#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

#include "voicevox_core/c_api/include/voicevox_core.h"

#define STYLE_ID 0
#define OUTPUT_WAV_NAME "audio.wav"

int main(int argc, char *argv[]) {
  if (argc != 2) {
    std::cout << "使い方: ./talk <文章>" << std::endl;
    return 0;
  }

  std::string open_jtalk_dict_path(
      "voicevox_core/dict/open_jtalk_dic_utf_8-1.11");
  std::string text(argv[1]);

  std::cout << "coreの初期化中..." << std::endl;

  auto initialize_options = voicevox_make_default_initialize_options();
  const VoicevoxOnnxruntime* onnxruntime;
  auto load_ort_options = voicevox_make_default_load_onnxruntime_options();
  std::string ort_filename = "./voicevox_core/onnxruntime/lib/";
  ort_filename += voicevox_get_onnxruntime_lib_versioned_filename();
  load_ort_options.filename = ort_filename.c_str();
  auto result = voicevox_onnxruntime_load_once(load_ort_options, &onnxruntime);
  if (result != VOICEVOX_RESULT_OK){
    std::cerr << voicevox_error_result_to_message(result) << std::endl;
    return 1;
  }
  OpenJtalkRc* open_jtalk;
  result = voicevox_open_jtalk_rc_new(open_jtalk_dict_path.c_str(),&open_jtalk);
  if (result != VOICEVOX_RESULT_OK){
    std::cerr << voicevox_error_result_to_message(result) << std::endl;
    return 1;
  }
  VoicevoxSynthesizer* synthesizer;
  result = voicevox_synthesizer_new(onnxruntime,open_jtalk,initialize_options,&synthesizer);
  if (result != VOICEVOX_RESULT_OK) {
    std::cerr << voicevox_error_result_to_message(result) << std::endl;
    return 1;
  }
  voicevox_open_jtalk_rc_delete(open_jtalk);

  for (auto const& entry :
       std::filesystem::directory_iterator{"./voicevox_core/models/vvms"}) {
    const auto path = entry.path();
    if (path.extension() != ".vvm") {
      continue;
    }
    VoicevoxVoiceModelFile* model;
    result = voicevox_voice_model_file_open(path.c_str(), &model);
    if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
      std::cerr << voicevox_error_result_to_message(result) << std::endl;
      return 0;
    }
    result = voicevox_synthesizer_load_voice_model(
        synthesizer, model, voicevox_make_default_load_voice_model_options());
    if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
      std::cerr << voicevox_error_result_to_message(result) << std::endl;
      return 0;
    }
    voicevox_voice_model_file_delete(model);
  }

  std::cout << "音声生成中..." << std::endl;

  size_t output_wav_size = 0;
  uint8_t *output_wav = nullptr;

  result = voicevox_synthesizer_tts(synthesizer,text.c_str(), STYLE_ID,
                             voicevox_make_default_tts_options(),
                             &output_wav_size, &output_wav);
  if (result != VOICEVOX_RESULT_OK) {
    std::cerr << voicevox_error_result_to_message(result) << std::endl;
    return 1;
  }

  std::cout << "音声ファイル保存中..." << std::endl;

  std::ofstream wav_file(OUTPUT_WAV_NAME, std::ios::binary);
  wav_file.write(reinterpret_cast<const char *>(output_wav), output_wav_size);
  voicevox_wav_free(output_wav);

  std::cout << "音声ファイル保存完了 (" << OUTPUT_WAV_NAME << ")" << std::endl;

  voicevox_synthesizer_delete(synthesizer);

  return 0;
}
