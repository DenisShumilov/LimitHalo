#include "production_adapter.h"

#include <bcrypt.h>
#include <winhttp.h>
#include <werapi.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <memory>

namespace broker {
namespace {

constexpr wchar_t kUsageHost[] = L"api.anthropic.com";
constexpr wchar_t kUsagePath[] = L"/api/oauth/usage";
constexpr wchar_t kRefreshHost[] = L"platform.claude.com";
constexpr wchar_t kRefreshPath[] = L"/v1/oauth/token";
constexpr DWORD kCredentialLockHeartbeatMs = 5000;
static_assert(WER_FAULT_REPORTING_FLAG_NOHEAP == 1,
              "Pinned werapi.h NOHEAP flag changed.");

TransportClass ClassifyWinHttpError(DWORD error) noexcept {
  switch (error) {
    case ERROR_WINHTTP_TIMEOUT:
      return TransportClass::Timeout;
    case ERROR_WINHTTP_NAME_NOT_RESOLVED:
      return TransportClass::NameResolution;
    case ERROR_WINHTTP_CANNOT_CONNECT:
    case ERROR_WINHTTP_CONNECTION_ERROR:
      return TransportClass::CannotConnect;
    case ERROR_WINHTTP_SECURE_FAILURE:
    case ERROR_WINHTTP_CLIENT_AUTH_CERT_NEEDED:
    case ERROR_WINHTTP_SECURE_CERT_DATE_INVALID:
    case ERROR_WINHTTP_SECURE_CERT_CN_INVALID:
    case ERROR_WINHTTP_SECURE_INVALID_CA:
    case ERROR_WINHTTP_SECURE_CERT_REV_FAILED:
    case ERROR_WINHTTP_SECURE_CHANNEL_ERROR:
      return TransportClass::SecureFailure;
    default:
      return TransportClass::Other;
  }
}

void SetTransportFailure(HttpReply& reply, TransportEndpoint endpoint,
                         TransportPhase phase,
                         TransportClass failureClass) noexcept {
  reply.kind = failureClass == TransportClass::Timeout
                   ? TransportKind::Timeout
                   : TransportKind::Failure;
  reply.diagnostic.endpoint = endpoint;
  reply.diagnostic.phase = phase;
  reply.diagnostic.failureClass = failureClass;
}

bool SetClippedTimeouts(HINTERNET handle, std::uint64_t phaseStarted,
                        DWORD phaseBudgetMs, TransportEndpoint endpoint,
                        TransportPhase phase, HttpReply& reply) noexcept {
  const std::uint64_t now = GetTickCount64();
  if (now < phaseStarted || now - phaseStarted >= phaseBudgetMs) {
    SetTransportFailure(reply, endpoint, phase, TransportClass::Timeout);
    return false;
  }
  const DWORD remaining = static_cast<DWORD>(phaseBudgetMs - (now - phaseStarted));
  const DWORD clipped = ComponentTimeoutMs(remaining);
  if (clipped == 0) {
    SetTransportFailure(reply, endpoint, phase, TransportClass::Timeout);
    return false;
  }
  const int timeout = static_cast<int>(clipped);
  if (!WinHttpSetTimeouts(handle, timeout, timeout, timeout, timeout)) {
    SetTransportFailure(reply, endpoint, phase, TransportClass::OptionFailure);
    return false;
  }
  return true;
}

class WinHttpHandle final {
 public:
  WinHttpHandle() noexcept = default;
  explicit WinHttpHandle(HINTERNET value) noexcept : value_(value) {}
  ~WinHttpHandle() { if (value_) WinHttpCloseHandle(value_); }
  WinHttpHandle(const WinHttpHandle&) = delete;
  WinHttpHandle& operator=(const WinHttpHandle&) = delete;
  HINTERNET get() const noexcept { return value_; }
  bool valid() const noexcept { return value_ != nullptr; }
 private:
  HINTERNET value_ = nullptr;
};

class LockedWideBuffer final {
 public:
  LockedWideBuffer() noexcept = default;
  ~LockedWideBuffer() { Release(); }
  LockedWideBuffer(const LockedWideBuffer&) = delete;
  LockedWideBuffer& operator=(const LockedWideBuffer&) = delete;
  bool Allocate(std::size_t characters) noexcept {
    Release();
    if (characters == 0 || characters > 8192) return false;
    const std::size_t bytes = characters * sizeof(wchar_t);
    data_ = static_cast<wchar_t*>(VirtualAlloc(nullptr, bytes,
                                               MEM_COMMIT | MEM_RESERVE,
                                               PAGE_READWRITE));
    if (!data_) return false;
    characters_ = characters;
    if (WerRegisterExcludedMemoryBlock(data_, static_cast<DWORD>(bytes)) != S_OK) {
      Release();
      return false;
    }
    werExcluded_ = true;
    locked_ = VirtualLock(data_, bytes) != FALSE;
    if (!locked_) {
      Release();
      return false;
    }
    return true;
  }
  wchar_t* data() noexcept { return data_; }
  const wchar_t* data() const noexcept { return data_; }
  std::size_t capacity() const noexcept { return characters_; }
 private:
  void Release() noexcept {
    if (!data_) return;
    const std::size_t bytes = characters_ * sizeof(wchar_t);
    SecureZeroMemory(data_, bytes);
    if (werExcluded_) WerUnregisterExcludedMemoryBlock(data_);
    if (locked_) VirtualUnlock(data_, bytes);
    VirtualFree(data_, 0, MEM_RELEASE);
    data_ = nullptr;
    characters_ = 0;
    locked_ = false;
    werExcluded_ = false;
  }
  wchar_t* data_ = nullptr;
  std::size_t characters_ = 0;
  bool locked_ = false;
  bool werExcluded_ = false;
};

CredentialVersion MakeVersion(const FILETIME& lastWritten,
                              DWORD blobSize) noexcept {
  CredentialVersion result;
  result.lastWrittenLow = lastWritten.dwLowDateTime;
  result.lastWrittenHigh = lastWritten.dwHighDateTime;
  result.blobSize = blobSize;
  return result;
}

bool EnvironmentValue(const wchar_t* name, std::wstring& output) noexcept {
  const DWORD required = GetEnvironmentVariableW(name, nullptr, 0);
  if (required < 2 || required > 32768) return false;
  std::unique_ptr<wchar_t[]> buffer(new (std::nothrow) wchar_t[required]);
  if (!buffer) return false;
  const DWORD written = GetEnvironmentVariableW(name, buffer.get(), required);
  if (written == 0 || written >= required) return false;
  output.assign(buffer.get(), written);
  SecureZeroMemory(buffer.get(), required * sizeof(wchar_t));
  return true;
}

bool RefuseUnsafeCrashPolicy() noexcept {
  return WerSetFlags(WER_FAULT_REPORTING_FLAG_NOHEAP) == S_OK;
}

bool SafePathHandle(HANDLE handle, bool requireDirectory) noexcept {
  FILE_ATTRIBUTE_TAG_INFO tag = {};
  if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfo,
                                    &tag, sizeof(tag)) ||
      (tag.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0)
    return false;
  const bool directory = (tag.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
  return directory == requireDirectory;
}

bool SafeDirectory(const std::wstring& path) noexcept {
  HANDLE directory = CreateFileW(
      path.c_str(), FILE_READ_ATTRIBUTES,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      nullptr, OPEN_EXISTING,
      FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
  if (directory == INVALID_HANDLE_VALUE) return false;
  const bool safe = SafePathHandle(directory, true);
  CloseHandle(directory);
  return safe;
}

bool WriteAll(HANDLE file, const SecureBytes& value) noexcept {
  std::size_t offset = 0;
  while (offset < value.size()) {
    const DWORD chunk = static_cast<DWORD>((std::min)(
        value.size() - offset,
        static_cast<std::size_t>((std::numeric_limits<DWORD>::max)())));
    DWORD written = 0;
    if (!WriteFile(file, value.data() + offset, chunk, &written, nullptr) ||
        written == 0 || written > chunk)
      return false;
    offset += written;
  }
  return true;
}

bool BuildRandomSiblingPath(const std::wstring& credentialPath,
                            const wchar_t* role,
                            std::wstring& siblingPath) noexcept {
  if (!role || !*role) return false;
  unsigned char random[16] = {};
  if (BCryptGenRandom(nullptr, random, sizeof(random),
                      BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0)
    return false;
  static constexpr wchar_t hex[] = L"0123456789abcdef";
  siblingPath = credentialPath + L".aiusage." + role + L".";
  for (unsigned char value : random) {
    siblingPath.push_back(hex[value >> 4]);
    siblingPath.push_back(hex[value & 0x0f]);
  }
  SecureZeroMemory(random, sizeof(random));
  return siblingPath.size() < 32768;
}

enum class ExactFileState { Missing, Match, Different };

bool HandleEquals(HANDLE file, const SecureBytes& expected) noexcept {
  BY_HANDLE_FILE_INFORMATION information = {};
  if (!SafePathHandle(file, false) ||
      !GetFileInformationByHandle(file, &information) ||
      information.nFileSizeHigh != 0 ||
      information.nFileSizeLow != expected.size())
    return false;
  unsigned char buffer[4096] = {};
  std::size_t offset = 0;
  bool matches = true;
  while (offset < expected.size()) {
    const DWORD wanted = static_cast<DWORD>((std::min)(
        expected.size() - offset, sizeof(buffer)));
    DWORD read = 0;
    if (!ReadFile(file, buffer, wanted, &read, nullptr) || read != wanted ||
        std::memcmp(buffer, expected.data() + offset, wanted) != 0) {
      matches = false;
      break;
    }
    offset += read;
  }
  SecureZeroMemory(buffer, sizeof(buffer));
  return matches && offset == expected.size();
}

ExactFileState InspectExactFile(const std::wstring& path,
                                const SecureBytes& expected) noexcept {
  HANDLE file = CreateFileW(
      path.c_str(), GENERIC_READ,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      nullptr, OPEN_EXISTING,
      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN |
          FILE_FLAG_OPEN_REPARSE_POINT,
      nullptr);
  if (file == INVALID_HANDLE_VALUE) {
    const DWORD error = GetLastError();
    return (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
               ? ExactFileState::Missing
               : ExactFileState::Different;
  }
  const bool matches = HandleEquals(file, expected);
  CloseHandle(file);
  return matches ? ExactFileState::Match : ExactFileState::Different;
}

bool DeleteExactFileIfPresent(const std::wstring& path,
                              const SecureBytes& expected) noexcept {
  HANDLE file = CreateFileW(
      path.c_str(), GENERIC_READ | DELETE,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      nullptr, OPEN_EXISTING,
      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN |
          FILE_FLAG_OPEN_REPARSE_POINT,
      nullptr);
  if (file == INVALID_HANDLE_VALUE) {
    const DWORD error = GetLastError();
    return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND;
  }
  bool removed = false;
  if (HandleEquals(file, expected)) {
    FILE_DISPOSITION_INFO disposition = {};
    disposition.DeleteFile = TRUE;
    removed = SetFileInformationByHandle(
                  file, FileDispositionInfo, &disposition,
                  sizeof(disposition)) != FALSE;
  }
  CloseHandle(file);
  return removed;
}

bool PrepareCredentialReplacement(
    const std::wstring& credentialPath,
    const std::wstring& temporaryPath,
    const SecureBytes& replacement) noexcept {
  BOOL cancel = FALSE;
  if (!CopyFileExW(credentialPath.c_str(), temporaryPath.c_str(), nullptr,
                   nullptr, &cancel, COPY_FILE_FAIL_IF_EXISTS))
    return false;
  HANDLE file = CreateFileW(
      temporaryPath.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
      OPEN_EXISTING,
      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN |
          FILE_FLAG_OPEN_REPARSE_POINT,
      nullptr);
  if (file == INVALID_HANDLE_VALUE || !SafePathHandle(file, false)) {
    if (file != INVALID_HANDLE_VALUE) CloseHandle(file);
    DeleteFileW(temporaryPath.c_str());
    return false;
  }
  LARGE_INTEGER beginning = {};
  const bool prepared =
      SetFilePointerEx(file, beginning, nullptr, FILE_BEGIN) &&
      WriteAll(file, replacement) && SetEndOfFile(file) &&
      FlushFileBuffers(file);
  CloseHandle(file);
  if (!prepared ||
      InspectExactFile(temporaryPath, replacement) != ExactFileState::Match) {
    DeleteFileW(temporaryPath.c_str());
    return false;
  }
  return true;
}

bool EnsureNewCredentialAtCanonical(
    const std::wstring& credentialPath,
    const std::wstring& temporaryPath,
    const SecureBytes& replacement) noexcept {
  if (InspectExactFile(credentialPath, replacement) == ExactFileState::Match)
    return true;
  if (InspectExactFile(temporaryPath, replacement) != ExactFileState::Match)
    return false;
  // The sibling was copied from the original first, so a same-directory move
  // retains its DACL, attributes, extended attributes, and alternate streams.
  if (!MoveFileExW(temporaryPath.c_str(), credentialPath.c_str(),
                   MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
    return false;
  return InspectExactFile(credentialPath, replacement) == ExactFileState::Match;
}

bool ParseCanonicalUint64(const SecureBytes& value,
                          std::uint64_t& output) noexcept {
  if (value.empty() || value.size() > 20 ||
      (value.size() > 1 && value.data()[0] == '0')) return false;
  std::uint64_t result = 0;
  for (std::size_t index = 0; index < value.size(); ++index) {
    const unsigned char digit = value.data()[index];
    if (digit < '0' || digit > '9') return false;
    const std::uint64_t number = digit - '0';
    if (result > (std::numeric_limits<std::uint64_t>::max() - number) / 10)
      return false;
    result = result * 10 + number;
  }
  output = result;
  return true;
}

bool JsonUnsigned(const JsonValue* value, std::uint64_t& output) noexcept {
  if (!value || value->type != JsonType::Number ||
      !std::isfinite(value->number) || value->number < 0.0 ||
      value->number > 9007199254740991.0 ||
      std::floor(value->number) != value->number) return false;
  output = static_cast<std::uint64_t>(value->number);
  return true;
}

}  // namespace

ProductionAdapter::ProductionAdapter() noexcept = default;

ProductionAdapter::~ProductionAdapter() {
  ReleaseCredentialWriteLock();
}

Status ProductionAdapter::ResolveFixedPaths() noexcept {
  std::wstring profile;
  if (!EnvironmentValue(L"USERPROFILE", profile))
    return Status::SecurityPolicy;
  credentialDirectory_ = profile + L"\\.claude";
  credentialPath_ = credentialDirectory_ + L"\\.credentials.json";
  credentialLockPath_ = credentialDirectory_ + L"\\.storage-write.lock";
  return Status::Ok;
}

Status ProductionAdapter::InitializeSecurityPolicy() noexcept {
  if (initialized_) return Status::SecurityPolicy;
  if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32) ||
      !SetDllDirectoryW(L"")) return Status::SecurityPolicy;
  wchar_t systemDirectory[MAX_PATH + 1] = {};
  const UINT length = GetSystemDirectoryW(systemDirectory, MAX_PATH);
  if (length == 0 || length >= MAX_PATH || !SetCurrentDirectoryW(systemDirectory))
    return Status::SecurityPolicy;
  SecureZeroMemory(systemDirectory, sizeof(systemDirectory));
  SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX |
               SEM_NOOPENFILEERRORBOX);
  if (!RefuseUnsafeCrashPolicy()) return Status::SecurityPolicy;
  const Status paths = ResolveFixedPaths();
  if (paths != Status::Ok) return paths;
  if (!SafeDirectory(credentialDirectory_)) return Status::SecurityPolicy;
  initialized_ = true;
  return Status::Ok;
}

Status ProductionAdapter::ReadCredential(CredentialRecord& record) noexcept {
  if (!initialized_) return Status::SecurityPolicy;
  HANDLE file = CreateFileW(
      credentialPath_.c_str(), GENERIC_READ,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      nullptr, OPEN_EXISTING,
      FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN |
          FILE_FLAG_OPEN_REPARSE_POINT,
      nullptr);
  if (file == INVALID_HANDLE_VALUE) {
    const DWORD error = GetLastError();
    return (error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND)
               ? Status::CredentialMissing
               : Status::CredentialUnsupported;
  }
  if (!SafePathHandle(file, false)) {
    CloseHandle(file);
    return Status::SecurityPolicy;
  }
  BY_HANDLE_FILE_INFORMATION information = {};
  if (!GetFileInformationByHandle(file, &information) ||
      information.nFileSizeHigh != 0 || information.nFileSizeLow == 0 ||
      information.nFileSizeLow > kCredentialFileLimit ||
      !record.storageDocument.Resize(information.nFileSizeLow)) {
    CloseHandle(file);
    return Status::CredentialUnsupported;
  }
  std::size_t offset = 0;
  while (offset < record.storageDocument.size()) {
    DWORD read = 0;
    const DWORD chunk = static_cast<DWORD>(record.storageDocument.size() - offset);
    if (!ReadFile(file, record.storageDocument.data() + offset,
                  chunk, &read, nullptr) || read == 0 || read > chunk) {
      CloseHandle(file);
      record.storageDocument.Clear();
      return Status::CredentialUnsupported;
    }
    offset += read;
  }
  CloseHandle(file);
  const Status projected = ProjectClaudeCredential(record.storageDocument,
                                                    record.blob);
  if (projected != Status::Ok) return projected;
  record.version = MakeVersion(information.ftLastWriteTime,
                               information.nFileSizeLow);
  return Status::Ok;
}

Status ProductionAdapter::AcquireCredentialWriteLock() noexcept {
  if (!initialized_ || credentialLockHeld_) return Status::SecurityPolicy;
  if (!CreateDirectoryW(credentialLockPath_.c_str(), nullptr))
    return Status::LockContended;
  credentialLockDirectoryHandle_ = CreateFileW(
      credentialLockPath_.c_str(),
      FILE_READ_ATTRIBUTES | FILE_WRITE_ATTRIBUTES,
      FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
      nullptr, OPEN_EXISTING,
      FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
  if (credentialLockDirectoryHandle_ == INVALID_HANDLE_VALUE ||
      !SafePathHandle(credentialLockDirectoryHandle_, true)) {
    if (credentialLockDirectoryHandle_ != INVALID_HANDLE_VALUE)
      CloseHandle(credentialLockDirectoryHandle_);
    credentialLockDirectoryHandle_ = INVALID_HANDLE_VALUE;
    RemoveDirectoryW(credentialLockPath_.c_str());
    return Status::LockContended;
  }
  credentialLockStopEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  FILETIME now = {};
  GetSystemTimeAsFileTime(&now);
  if (!credentialLockStopEvent_ ||
      !SetFileTime(credentialLockDirectoryHandle_, nullptr, nullptr, &now)) {
    if (credentialLockStopEvent_) CloseHandle(credentialLockStopEvent_);
    credentialLockStopEvent_ = nullptr;
    CloseHandle(credentialLockDirectoryHandle_);
    credentialLockDirectoryHandle_ = INVALID_HANDLE_VALUE;
    RemoveDirectoryW(credentialLockPath_.c_str());
    return Status::LockContended;
  }
  InterlockedExchange(&credentialLockHeartbeatFailed_, 0);
  credentialLockHeartbeatThread_ = CreateThread(
      nullptr, 0, CredentialLockHeartbeatEntry, this, 0, nullptr);
  if (!credentialLockHeartbeatThread_) {
    CloseHandle(credentialLockStopEvent_);
    credentialLockStopEvent_ = nullptr;
    CloseHandle(credentialLockDirectoryHandle_);
    credentialLockDirectoryHandle_ = INVALID_HANDLE_VALUE;
    RemoveDirectoryW(credentialLockPath_.c_str());
    return Status::LockContended;
  }
  credentialLockHeld_ = true;
  return Status::Ok;
}

void ProductionAdapter::ReleaseCredentialWriteLock() noexcept {
  if (!credentialLockHeld_) return;
  if (credentialLockStopEvent_) SetEvent(credentialLockStopEvent_);
  if (credentialLockHeartbeatThread_) {
    WaitForSingleObject(credentialLockHeartbeatThread_, 2000);
    CloseHandle(credentialLockHeartbeatThread_);
    credentialLockHeartbeatThread_ = nullptr;
  }
  if (credentialLockStopEvent_) {
    CloseHandle(credentialLockStopEvent_);
    credentialLockStopEvent_ = nullptr;
  }
  if (credentialLockDirectoryHandle_ != INVALID_HANDLE_VALUE) {
    CloseHandle(credentialLockDirectoryHandle_);
    credentialLockDirectoryHandle_ = INVALID_HANDLE_VALUE;
  }
  RemoveDirectoryW(credentialLockPath_.c_str());
  credentialLockHeld_ = false;
}

DWORD WINAPI ProductionAdapter::CredentialLockHeartbeatEntry(
    void* context) noexcept {
  if (!context) return 1;
  static_cast<ProductionAdapter*>(context)->CredentialLockHeartbeatLoop();
  return 0;
}

void ProductionAdapter::CredentialLockHeartbeatLoop() noexcept {
  for (;;) {
    const DWORD wait = WaitForSingleObject(credentialLockStopEvent_,
                                           kCredentialLockHeartbeatMs);
    if (wait == WAIT_OBJECT_0) return;
    if (wait != WAIT_TIMEOUT ||
        credentialLockDirectoryHandle_ == INVALID_HANDLE_VALUE) {
      InterlockedExchange(&credentialLockHeartbeatFailed_, 1);
      return;
    }
    FILETIME now = {};
    GetSystemTimeAsFileTime(&now);
    if (!SetFileTime(credentialLockDirectoryHandle_, nullptr, nullptr, &now)) {
      InterlockedExchange(&credentialLockHeartbeatFailed_, 1);
      return;
    }
  }
}

bool ProductionAdapter::CredentialLockHealthy() const noexcept {
  return credentialLockHeld_ && credentialLockDirectoryHandle_ != INVALID_HANDLE_VALUE &&
      credentialLockHeartbeatThread_ != nullptr &&
      InterlockedCompareExchange(
          const_cast<volatile LONG*>(&credentialLockHeartbeatFailed_), 0, 0) == 0;
}

Status ProductionAdapter::WriteCredential(
    const CredentialRecord& expected,
    const SecureBytes& replacement) noexcept {
  if (!initialized_ || !CredentialLockHealthy() || replacement.empty() ||
      replacement.size() > kCredentialProjectionLimit ||
      expected.storageDocument.empty() ||
      expected.storageDocument.size() > kCredentialFileLimit)
    return Status::SecurityPolicy;

  CredentialRecord current;
  Status currentStatus = ReadCredential(current);
  if (currentStatus != Status::Ok) return currentStatus;
  if (current.version != expected.version ||
      !current.blob.Equals(expected.blob) ||
      !current.storageDocument.Equals(expected.storageDocument))
    return Status::CredentialChanged;

  SecureBytes nextFullDocument;
  currentStatus = PatchClaudeCredential(current.storageDocument, replacement,
                                        nextFullDocument);
  if (currentStatus != Status::Ok) return currentStatus;

  std::wstring temporaryPath;
  std::wstring backupPath;
  if (!BuildRandomSiblingPath(credentialPath_, L"tmp", temporaryPath) ||
      !BuildRandomSiblingPath(credentialPath_, L"bak", backupPath))
    return Status::Internal;
  if (!PrepareCredentialReplacement(
          credentialPath_, temporaryPath, nextFullDocument))
    return Status::Internal;
  if (!CredentialLockHealthy()) {
    // A completed provider refresh may have rotated its refresh token. Keep the
    // exact new sibling for recovery instead of deleting the only new copy.
    return Status::Internal;
  }

  CredentialRecord finalCheck;
  currentStatus = ReadCredential(finalCheck);
  if (currentStatus != Status::Ok || finalCheck.version != expected.version ||
      !finalCheck.blob.Equals(expected.blob) ||
      !finalCheck.storageDocument.Equals(expected.storageDocument)) {
    // Do not discard the provider-authorized replacement after a CAS failure.
    return currentStatus == Status::Ok ? Status::CredentialChanged : currentStatus;
  }
  if (!CredentialLockHealthy())
    return Status::Internal;
  const bool replaced = ReplaceFileW(
      credentialPath_.c_str(), temporaryPath.c_str(), backupPath.c_str(),
      0, nullptr, nullptr) != FALSE;
  if (!replaced && !EnsureNewCredentialAtCanonical(
                       credentialPath_, temporaryPath, nextFullDocument))
    return Status::Internal;

  CredentialRecord updated;
  currentStatus = ReadCredential(updated);
  if (currentStatus != Status::Ok ||
      !updated.blob.Equals(replacement) ||
      !updated.storageDocument.Equals(nextFullDocument)) {
    // Preserve every remaining recovery artifact on ambiguous post-commit state.
    return Status::Internal;
  }
  if (!DeleteExactFileIfPresent(temporaryPath, nextFullDocument) ||
      !DeleteExactFileIfPresent(backupPath, expected.storageDocument))
    return Status::Internal;
  return Status::Ok;
}

Status ProductionAdapter::UsageRequest(const SecureBytes& accessToken,
                                       DWORD timeoutMs,
                                       HttpReply& reply) noexcept {
  return SendFixedRequest(true, accessToken, timeoutMs, reply);
}

Status ProductionAdapter::RefreshRequest(const SecureBytes& requestBody,
                                         DWORD timeoutMs,
                                         HttpReply& reply) noexcept {
  return SendFixedRequest(false, requestBody, timeoutMs, reply);
}

Status ProductionAdapter::SendFixedRequest(bool usage,
                                           const SecureBytes& secret,
                                           DWORD timeoutMs,
                                           HttpReply& reply) noexcept {
  if (!initialized_ || secret.empty() || secret.size() > 8192 ||
      timeoutMs == 0 ||
      (usage ? timeoutMs > kUsageTimeoutMs : timeoutMs > kRefreshTimeoutMs))
    return Status::SecurityPolicy;

  reply.kind = TransportKind::Failure;
  reply.statusCode = 0;
  reply.diagnostic = TransportDiagnostic{};
  reply.body.Clear();
  const TransportEndpoint endpoint = usage ? TransportEndpoint::Usage
                                            : TransportEndpoint::Refresh;
  const std::uint64_t phaseStarted = GetTickCount64();

  WinHttpHandle session(WinHttpOpen(L"AIUsageWidgetBroker/2.0.0",
                                    WINHTTP_ACCESS_TYPE_NO_PROXY,
                                    WINHTTP_NO_PROXY_NAME,
                                    WINHTTP_NO_PROXY_BYPASS, 0));
  if (!session.valid()) {
    SetTransportFailure(reply, endpoint, TransportPhase::SessionOptions,
                        ClassifyWinHttpError(GetLastError()));
    return Status::Ok;
  }
  DWORD headerLimit = 32768;
  if (!WinHttpSetOption(session.get(), WINHTTP_OPTION_MAX_RESPONSE_HEADER_SIZE,
                        &headerLimit, sizeof(headerLimit))) {
    SetTransportFailure(reply, endpoint, TransportPhase::SessionOptions,
                        TransportClass::OptionFailure);
    return Status::Ok;
  }
  DWORD redirectPolicy = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
  DWORD autologon = WINHTTP_AUTOLOGON_SECURITY_LEVEL_HIGH;
  DWORD disableFeatures = WINHTTP_DISABLE_COOKIES |
                          WINHTTP_DISABLE_REDIRECTS |
                          WINHTTP_DISABLE_AUTHENTICATION;
  if (!WinHttpSetOption(session.get(), WINHTTP_OPTION_REDIRECT_POLICY,
                        &redirectPolicy, sizeof(redirectPolicy))) {
    SetTransportFailure(reply, endpoint, TransportPhase::SessionOptions,
                        TransportClass::OptionFailure);
    return Status::Ok;
  }

  if (!SetClippedTimeouts(session.get(), phaseStarted, timeoutMs, endpoint,
                          TransportPhase::Connect, reply)) return Status::Ok;
  WinHttpHandle connection(WinHttpConnect(session.get(),
                                           usage ? kUsageHost : kRefreshHost,
                                           INTERNET_DEFAULT_HTTPS_PORT, 0));
  if (!connection.valid()) {
    SetTransportFailure(reply, endpoint, TransportPhase::Connect,
                        ClassifyWinHttpError(GetLastError()));
    return Status::Ok;
  }
  WinHttpHandle request(WinHttpOpenRequest(
      connection.get(), usage ? L"GET" : L"POST",
      usage ? kUsagePath : kRefreshPath, nullptr, WINHTTP_NO_REFERER,
      WINHTTP_DEFAULT_ACCEPT_TYPES, WINHTTP_FLAG_SECURE));
  if (!request.valid()) {
    SetTransportFailure(reply, endpoint, TransportPhase::Connect,
                        ClassifyWinHttpError(GetLastError()));
    return Status::Ok;
  }
  if (!WinHttpSetOption(request.get(), WINHTTP_OPTION_AUTOLOGON_POLICY,
                        &autologon, sizeof(autologon)) ||
      !WinHttpSetOption(request.get(), WINHTTP_OPTION_DISABLE_FEATURE,
                        &disableFeatures, sizeof(disableFeatures))) {
    SetTransportFailure(reply, endpoint, TransportPhase::SessionOptions,
                        TransportClass::OptionFailure);
    return Status::Ok;
  }
  DWORD revocation = WINHTTP_ENABLE_SSL_REVOCATION;
  if (!WinHttpSetOption(request.get(), WINHTTP_OPTION_ENABLE_FEATURE,
                        &revocation, sizeof(revocation))) {
    SetTransportFailure(reply, endpoint, TransportPhase::SessionOptions,
                        TransportClass::OptionFailure);
    return Status::Ok;
  }

  if (!SetClippedTimeouts(request.get(), phaseStarted, timeoutMs, endpoint,
                          TransportPhase::Send, reply)) return Status::Ok;

  BOOL sent = FALSE;
  if (usage) {
    if (secret.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
      return Status::Internal;
    const int tokenCharacters = MultiByteToWideChar(
        CP_UTF8, MB_ERR_INVALID_CHARS,
        reinterpret_cast<const char*>(secret.data()),
        static_cast<int>(secret.size()), nullptr, 0);
    if (tokenCharacters <= 0) return Status::CredentialMalformed;
    static constexpr wchar_t prefix[] = L"Authorization: Bearer ";
    static constexpr wchar_t suffix[] =
        L"\r\nAccept: application/json\r\nanthropic-beta: oauth-2025-04-20\r\n";
    const std::size_t total = (sizeof(prefix) / sizeof(prefix[0]) - 1) +
                              static_cast<std::size_t>(tokenCharacters) +
                              (sizeof(suffix) / sizeof(suffix[0]));
    LockedWideBuffer headers;
    if (!headers.Allocate(total)) return Status::Internal;
    wchar_t* cursor = headers.data();
    std::memcpy(cursor, prefix, sizeof(prefix) - sizeof(wchar_t));
    cursor += sizeof(prefix) / sizeof(prefix[0]) - 1;
    if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                            reinterpret_cast<const char*>(secret.data()),
                            static_cast<int>(secret.size()), cursor,
                            tokenCharacters) != tokenCharacters)
      return Status::CredentialMalformed;
    cursor += tokenCharacters;
    std::memcpy(cursor, suffix, sizeof(suffix));
    sent = WinHttpSendRequest(request.get(), headers.data(),
                              static_cast<DWORD>(total - 1),
                              WINHTTP_NO_REQUEST_DATA, 0, 0, 0);
  } else {
    static constexpr wchar_t headers[] =
        L"Content-Type: application/json\r\nAccept: application/json\r\n";
    sent = WinHttpSendRequest(request.get(), headers, static_cast<DWORD>(-1L),
                              const_cast<unsigned char*>(secret.data()),
                              static_cast<DWORD>(secret.size()),
                              static_cast<DWORD>(secret.size()), 0);
  }
  if (!sent) {
    SetTransportFailure(reply, endpoint, TransportPhase::Send,
                        ClassifyWinHttpError(GetLastError()));
    return Status::Ok;
  }
  if (!SetClippedTimeouts(request.get(), phaseStarted, timeoutMs, endpoint,
                          TransportPhase::Receive, reply)) return Status::Ok;
  if (!WinHttpReceiveResponse(request.get(), nullptr)) {
    SetTransportFailure(reply, endpoint, TransportPhase::Receive,
                        ClassifyWinHttpError(GetLastError()));
    return Status::Ok;
  }

  if (!SetClippedTimeouts(request.get(), phaseStarted, timeoutMs, endpoint,
                          TransportPhase::Headers, reply)) return Status::Ok;
  DWORD statusCode = 0;
  DWORD statusBytes = sizeof(statusCode);
  if (!WinHttpQueryHeaders(request.get(),
                           WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                           WINHTTP_HEADER_NAME_BY_INDEX, &statusCode,
                           &statusBytes, WINHTTP_NO_HEADER_INDEX)) {
    SetTransportFailure(reply, endpoint, TransportPhase::Headers,
                        ClassifyWinHttpError(GetLastError()));
    return Status::Ok;
  }
  reply.statusCode = statusCode;

  wchar_t encoding[32] = {};
  DWORD encodingBytes = sizeof(encoding);
  if (WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_CONTENT_ENCODING,
                          WINHTTP_HEADER_NAME_BY_INDEX, encoding,
                          &encodingBytes, WINHTTP_NO_HEADER_INDEX)) {
    if (encoding[0] != L'\0' && CompareStringOrdinal(encoding, -1, L"identity", -1,
                                                      TRUE) != CSTR_EQUAL) {
      SecureZeroMemory(encoding, sizeof(encoding));
      reply.kind = TransportKind::Oversized;
      return Status::Ok;
    }
  }
  SecureZeroMemory(encoding, sizeof(encoding));

  DWORD contentLength = 0;
  DWORD lengthBytes = sizeof(contentLength);
  if (WinHttpQueryHeaders(request.get(),
                          WINHTTP_QUERY_CONTENT_LENGTH | WINHTTP_QUERY_FLAG_NUMBER,
                          WINHTTP_HEADER_NAME_BY_INDEX, &contentLength,
                          &lengthBytes, WINHTTP_NO_HEADER_INDEX) &&
      contentLength > kHttpBodyLimit) {
    reply.kind = TransportKind::Oversized;
    return Status::Ok;
  }

  for (;;) {
    if (!SetClippedTimeouts(request.get(), phaseStarted, timeoutMs, endpoint,
                            TransportPhase::Body, reply)) return Status::Ok;
    DWORD available = 0;
    if (!WinHttpQueryDataAvailable(request.get(), &available)) {
      SetTransportFailure(reply, endpoint, TransportPhase::Body,
                          ClassifyWinHttpError(GetLastError()));
      return Status::Ok;
    }
    if (available == 0) break;
    if (available > kHttpBodyLimit - reply.body.size()) {
      reply.kind = TransportKind::Oversized;
      return Status::Ok;
    }
    const std::size_t original = reply.body.size();
    if (!reply.body.Resize(original + available)) return Status::Internal;
    DWORD read = 0;
    if (!WinHttpReadData(request.get(), reply.body.data() + original,
                         available, &read)) {
      SetTransportFailure(reply, endpoint, TransportPhase::Body,
                          ClassifyWinHttpError(GetLastError()));
      return Status::Ok;
    }
    if (read > available || !reply.body.Resize(original + read))
      return Status::Internal;
  }
  reply.kind = TransportKind::Response;
  return Status::Ok;
}

std::uint64_t ProductionAdapter::NowUnixMilliseconds() const noexcept {
  FILETIME fileTime = {};
  GetSystemTimeAsFileTime(&fileTime);
  const std::uint64_t windows =
      (static_cast<std::uint64_t>(fileTime.dwHighDateTime) << 32) |
      fileTime.dwLowDateTime;
  constexpr std::uint64_t epoch = 116444736000000000ULL;
  return windows >= epoch ? (windows - epoch) / 10000 : 0;
}

std::uint64_t ProductionAdapter::MonotonicMilliseconds() const noexcept {
  return GetTickCount64();
}

}  // namespace broker
