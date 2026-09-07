#include "broker_core.h"
#include "production_adapter.h"

#include <windows.h>

#include <cstdint>
#include <string>

namespace {

enum class PipeReadResult { Byte, End, Failure };

PipeReadResult ReadPipeByte(HANDLE pipe, unsigned char& byte,
                            std::uint64_t deadline) noexcept {
  for (;;) {
    DWORD available = 0;
    if (!PeekNamedPipe(pipe, nullptr, 0, nullptr, &available, nullptr)) {
      return GetLastError() == ERROR_BROKEN_PIPE ? PipeReadResult::End
                                                 : PipeReadResult::Failure;
    }
    if (available != 0) {
      DWORD read = 0;
      if (!ReadFile(pipe, &byte, 1, &read, nullptr) || read != 1)
        return PipeReadResult::Failure;
      return PipeReadResult::Byte;
    }
    if (GetTickCount64() >= deadline) return PipeReadResult::Failure;
    Sleep(1);
  }
}

bool ReadSingleRequestLine(broker::SecureBytes& input) noexcept {
  HANDLE standardInput = GetStdHandle(STD_INPUT_HANDLE);
  if (!standardInput || standardInput == INVALID_HANDLE_VALUE ||
      GetFileType(standardInput) != FILE_TYPE_PIPE) return false;
  const std::uint64_t deadline = GetTickCount64() + 1500;
  unsigned char byte = 0;
  bool sawCarriageReturn = false;
  for (;;) {
    if (ReadPipeByte(standardInput, byte, deadline) != PipeReadResult::Byte)
      return false;
    if (byte == '\n') {
      break;
    }
    if (sawCarriageReturn || byte == 0) return false;
    if (byte == '\r') {
      sawCarriageReturn = true;
      continue;
    }
    if (input.size() >= 512) return false;
    if (!input.AppendByte(byte)) return false;
  }
  const PipeReadResult trailing = ReadPipeByte(standardInput, byte, deadline);
  SecureZeroMemory(&byte, sizeof(byte));
  return trailing == PipeReadResult::End && !input.empty() && input.size() <= 512;
}

void CloseStandardError() noexcept {
  HANDLE standardError = GetStdHandle(STD_ERROR_HANDLE);
  SetStdHandle(STD_ERROR_HANDLE, nullptr);
  if (standardError && standardError != INVALID_HANDLE_VALUE) CloseHandle(standardError);
}

void WriteOneLine(const std::string& output) noexcept {
  HANDLE standardOutput = GetStdHandle(STD_OUTPUT_HANDLE);
  if (!standardOutput || standardOutput == INVALID_HANDLE_VALUE || output.empty()) return;
  std::size_t offset = 0;
  while (offset < output.size()) {
    DWORD written = 0;
    if (!WriteFile(standardOutput, output.data() + offset,
                   static_cast<DWORD>(output.size() - offset), &written, nullptr) ||
        written == 0) return;
    offset += written;
  }
}

}  // namespace

int wmain(int argumentCount, wchar_t**) {
  CloseStandardError();
  const bool secureMemoryReady = broker::PrepareSecureMemoryWorkingSet();
  broker::ProtocolRequest request;
  for (char& character : request.requestId) character = '0';
  request.requestId[32] = '\0';
  request.acquisitionSequence = 1;

  const std::uint64_t started = GetTickCount64();
  broker::Result result;
  broker::SecureBytes input;
  if (!secureMemoryReady) {
    result.status = broker::Status::SecurityPolicy;
  } else if (argumentCount != 1 || !ReadSingleRequestLine(input) ||
      broker::ParseProtocolRequest(input, request) != broker::Status::Ok) {
    result.status = broker::Status::BadRequest;
  } else {
    broker::ProductionAdapter adapter;
    const broker::Status policy = adapter.InitializeSecurityPolicy();
    if (policy != broker::Status::Ok) {
      result.status = policy;
    } else {
      result = broker::Execute(adapter, request.acquisitionSequence);
    }
  }
  const std::uint64_t ended = GetTickCount64();
  FILETIME fileTime = {};
  GetSystemTimeAsFileTime(&fileTime);
  const std::uint64_t windows =
      (static_cast<std::uint64_t>(fileTime.dwHighDateTime) << 32) |
      fileTime.dwLowDateTime;
  constexpr std::uint64_t epoch = 116444736000000000ULL;
  const std::int64_t acquiredAt = windows >= epoch
      ? static_cast<std::int64_t>((windows - epoch) / 10000000ULL) : 0;
  std::string output;
  if (!broker::FormatResult(result, request, acquiredAt,
                            ended >= started ? ended - started : 0, output)) {
    TerminateProcess(GetCurrentProcess(), 70);
  }
  WriteOneLine(output);
  SecureZeroMemory(output.data(), output.size());
  output.clear();
  return result.status == broker::Status::Ok ? 0 : 2;
}
