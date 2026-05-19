/**
 * 歌唱音声合成を行う。
 *
 * C言語としても参考できるよう、書き方をCに寄せてある。
 */

#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "voicevox_core/c_api/include/voicevox_core.h"

const char* kOjtDicDir = "./voicevox_core/dict/open_jtalk_dic_utf_8-1.11";
const char* kVvm = "./voicevox_core/models/vvms/s0.vvm";
const char* kScore =
    "{"
    "  \"notes\": [ "
    "    { \"key\": null, \"frame_length\": 15, \"lyric\": \"\" },"
    "    { \"key\": 60, \"frame_length\": 45, \"lyric\": \"ド\" },"
    "    { \"key\": 62, \"frame_length\": 45, \"lyric\": \"レ\" },"
    "    { \"key\": 64, \"frame_length\": 45, \"lyric\": \"ミ\" },"
    "    { \"key\": null, \"frame_length\": 15, \"lyric\": \"\" }"
    "  ]"
    "}";
const VoicevoxStyleId kSingingTeacher = 6000;
const VoicevoxStyleId kSinger = 3000;
const char* kOutput = "./audio.wav";

int write_file(const uint8_t* data, size_t data_len) {
  FILE* output = fopen(kOutput, "wb");
  if (output == NULL) {
    return 1;
  }
  const size_t num_wrote = fwrite(data, 1, data_len, output);
  int fclose_result = fclose(output);
  return (num_wrote < data_len) || fclose_result;
}

int main(int argc, char* argv[]) {
  if (argc > 1) {
    fprintf(stderr, "%sは引数を取りません\n", argv[0]);
    return 1;
  }

  VoicevoxLoadOnnxruntimeOptions load_onnxruntime_options =
      voicevox_make_default_load_onnxruntime_options();
  char libonnxruntime_filename[128];
  assert(strlen(voicevox_get_onnxruntime_lib_versioned_filename()) <
         128 - strlen("./voicevox_core/onnxruntime/lib/"));
  snprintf(libonnxruntime_filename, 128, "./voicevox_core/onnxruntime/lib/%s",
           voicevox_get_onnxruntime_lib_versioned_filename());
  load_onnxruntime_options.filename = libonnxruntime_filename;

  VoicevoxInitializeOptions initialize_options =
      voicevox_make_default_initialize_options();

  int stdio_result = 0;
  VoicevoxResultCode voicevox_result = VOICEVOX_RESULT_OK;

  OpenJtalkRc* openjtalk = NULL;
  VoicevoxSynthesizer* synthesizer = NULL;
  VoicevoxVoiceModelFile* model = NULL;
  char* frame_audio_query = NULL;
  uint8_t* wav = NULL;

  const VoicevoxOnnxruntime* onnxruntime;
  size_t wav_length;

#define TRY(result)                              \
  do {                                           \
    voicevox_result = result;                    \
    if (voicevox_result != VOICEVOX_RESULT_OK) { \
      goto cleanup;                              \
    }                                            \
  } while (0)

  TRY(voicevox_onnxruntime_load_once(load_onnxruntime_options, &onnxruntime));

  TRY(voicevox_open_jtalk_rc_new(kOjtDicDir, &openjtalk));

  TRY(voicevox_synthesizer_new(onnxruntime, openjtalk, initialize_options,
                               &synthesizer));

  TRY(voicevox_voice_model_file_open(kVvm, &model));

  TRY(voicevox_synthesizer_load_voice_model(
      synthesizer, model, voicevox_make_default_load_voice_model_options()));

  TRY(voicevox_synthesizer_create_sing_frame_audio_query(
      synthesizer, kScore, kSingingTeacher, &frame_audio_query));

  TRY(voicevox_synthesizer_frame_synthesis(synthesizer, frame_audio_query,
                                           kSinger, &wav_length, &wav));

#undef TRY

  errno = 0;
  stdio_result = write_file(wav, wav_length);

cleanup:
  voicevox_wav_free(wav);
  voicevox_json_free(frame_audio_query);
  voicevox_voice_model_file_delete(model);
  voicevox_synthesizer_delete(synthesizer);
  voicevox_open_jtalk_rc_delete(openjtalk);

  if (stdio_result) {
    if (errno) {
      perror("Result");
    } else {
      fprintf(stderr, "Result: `fwrite` failed\n");
    }
    return 1;
  }
  fprintf(stderr, "Result: %s\n",
          voicevox_error_result_to_message(voicevox_result));
  if (voicevox_result != VOICEVOX_RESULT_OK) {
    return 1;
  }
  return 0;
}
