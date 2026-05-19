// simple_tts.cpp : このファイルには 'main' 関数が含まれています。プログラム実行の開始と終了がそこで行われます。
//

// TODO: 実装スタイルをunix/song.cppに揃える
// TODO: ファイル名をunix/song.cppに寄せる

#include "simple_tts.h"

#include <Windows.h>
#include <pathcch.h>
#include <shlwapi.h>
#include <string.h>

#include <array>
#include <codecvt>
#include <iostream>
#include <vector>
#include <filesystem>
#include <fstream>

#include "voicevox_core.h"

#define OPENJTALK_DICT_NAME L"open_jtalk_dic_utf_8-1.11"
#define MODEL_DIR_NAME L"models\\vvms"

int main() {
  std::wcout.imbue(std::locale(""));
  std::wcin.imbue(std::locale(""));

  std::wcout << L"生成する音声の文字列を入力" << std::endl;
  std::wcout << L">";
  std::wstring speak_words;
  std::wcin >> speak_words;

  std::wcout << L"coreの初期化中" << std::endl;
  VoicevoxInitializeOptions  initializeOptions = voicevox_make_default_initialize_options();
  std::string dict = GetOpenJTalkDict();

  const VoicevoxOnnxruntime* onnxruntime;
  auto load_ort_options = voicevox_make_default_load_onnxruntime_options();
  auto result = voicevox_onnxruntime_load_once(load_ort_options, &onnxruntime);
  if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
    OutErrorMessage(result);
    return 0;
  }
  OpenJtalkRc* open_jtalk;
  result = voicevox_open_jtalk_rc_new(dict.c_str(),&open_jtalk);
  if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
    OutErrorMessage(result);
    return 0;
  }
  VoicevoxSynthesizer* synthesizer;
  result = voicevox_synthesizer_new(onnxruntime,open_jtalk,initializeOptions,&synthesizer);
  if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
    OutErrorMessage(result);
    return 0;
  }
  voicevox_open_jtalk_rc_delete(open_jtalk);

  for (const auto& entry : std::filesystem::directory_iterator{GetModelDir()}) {
    const auto path = entry.path();
    if (path.extension() != ".vvm") {
      continue;
    }
    VoicevoxVoiceModelFile* model;
    result = voicevox_voice_model_file_open(path.generic_u8string().c_str(), &model);
    if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
      OutErrorMessage(result);
      return 0;
    }
    result = voicevox_synthesizer_load_voice_model(
        synthesizer, model,
        voicevox_make_default_load_voice_model_options());
    if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
      OutErrorMessage(result);
      return 0;
    }
    voicevox_voice_model_file_delete(model);
  }

  std::wcout << L"音声生成中" << std::endl;
  int32_t style_id = 0;
  uintptr_t output_binary_size = 0;
  uint8_t* output_wav = nullptr;
  VoicevoxTtsOptions ttsOptions = voicevox_make_default_tts_options();

  result = voicevox_synthesizer_tts(synthesizer,wide_to_utf8_cppapi(speak_words).c_str(), style_id, ttsOptions, &output_binary_size, &output_wav);
  if (result != VoicevoxResultCode::VOICEVOX_RESULT_OK) {
    OutErrorMessage(result);
    return 0;
  }

  {
    //音声ファイルの保存
    std::ofstream out_stream(GetWaveFileName().c_str(), std::ios::binary);
    out_stream.write(reinterpret_cast<const char*>(output_wav), output_binary_size);
    std::wcout << GetWaveFileName() << L" に保存しました。" << std::endl;
  }  //ここでファイルが閉じられる

  std::wcout << L"音声再生中" << std::endl;
  PlaySound((LPCTSTR)output_wav, nullptr, SND_MEMORY);

  std::wcout << L"音声データの開放" << std::endl;
  voicevox_wav_free(output_wav);

  voicevox_synthesizer_delete(synthesizer);
}

/// <summary>
/// OpenJTalk辞書のパスを取得します。
/// </summary>
/// <returns>OpenJTalk辞書のパス</returns>
std::string GetOpenJTalkDict() {
  wchar_t buff[MAX_PATH] = {0};
  PathCchCombine(buff, MAX_PATH, GetExeDirectory().c_str(), OPENJTALK_DICT_NAME);
  std::string retVal = wide_to_utf8_cppapi(buff);
  return retVal;
}

/// <summary>
/// VVMを含むディレクトリのパスを取得します。
/// </summary>
/// <returns>ディレクトのパス</returns>
std::string GetModelDir() {
  wchar_t buff[MAX_PATH] = {0};
  PathCchCombine(buff, MAX_PATH, GetExeDirectory().c_str(), MODEL_DIR_NAME);
  std::string retVal = wide_to_utf8_cppapi(buff);
  return retVal;
}

/// <summary>
/// 音声ファイル名を取得します。
/// </summary>
/// <returns>音声ファイルのフルパス</returns>
std::wstring GetWaveFileName() {
  wchar_t buff[MAX_PATH] = {0};
  PathCchCombine(buff, MAX_PATH, GetExeDirectory().c_str(), L"speech.wav");
  return std::wstring(buff);
}

/// <summary>
/// 自分自身のあるパスを取得する
/// </summary>
/// <returns>自分のexeのフルパス</returns>
std::wstring GetExePath() {
  wchar_t buff[MAX_PATH] = {0};
  GetModuleFileName(nullptr, buff, MAX_PATH);
  return std::wstring(buff);
}

/// <summary>
/// 自分自身のあるディレクトリを取得する
/// </summary>
/// <returns>自分のexeのあるディレクトリ</returns>
std::wstring GetExeDirectory() {
  wchar_t buff[MAX_PATH] = {0};
  wcscpy_s(buff, MAX_PATH, GetExePath().c_str());
  //フルパスからファイル名の削除
  PathRemoveFileSpec(buff);
  return std::wstring(buff);
}

/// <summary>
/// コンソール画面にエラーメッセージを出力します。
/// </summary>
/// <param name="messageCode">メッセージコード</param>
void OutErrorMessage(VoicevoxResultCode messageCode) {
  const char* utf8Str = voicevox_error_result_to_message(messageCode);
  std::wstring wideStr = utf8_to_wide_cppapi(utf8Str);
  std::wcout << wideStr << std::endl;
}

// FIXME: codecvtはC++17から非推奨であるため、std::filesystem::pathに乗り換える。それができたらvcxprojの_SILENCE_CXX17_CODECVT_HEADER_DEPRECATION_WARNINGを削除する。

/// <summary>
/// ワイド文字列をUTF8に変換します。
/// </summary>
/// <param name="src">ワイド文字列</param>
/// <returns>UTF8文字列</returns>
/// <remarks>
/// https://nekko1119.hatenablog.com/entry/2017/01/02/054629 から引用
/// </remarks>
std::string wide_to_utf8_cppapi(std::wstring const& src) {
  std::wstring_convert<std::codecvt_utf8_utf16<wchar_t>> converter;
  return converter.to_bytes(src);
}

/// <summary>
/// UTF8をワイド文字に変換します。
/// </summary>
/// <param name="src">UTF8文字列</param>
/// <returns>ワイド文字列</returns>
/// <remarks>
/// https://nekko1119.hatenablog.com/entry/2017/01/02/054629 から引用
/// </remarks>
std::wstring utf8_to_wide_cppapi(std::string const& src) {
  std::wstring_convert<std::codecvt_utf8_utf16<wchar_t>> converter;
  return converter.from_bytes(src);
}
