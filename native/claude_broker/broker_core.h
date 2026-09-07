#pragma once

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <windows.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace broker {

constexpr std::size_t kCredentialFileLimit = 65536;
constexpr std::size_t kCredentialProjectionLimit = 8192;
constexpr std::size_t kHttpBodyLimit = 131072;
constexpr std::size_t kOutputLimit = 8192;
constexpr std::uint64_t kExpirySkewMs = 300000;
constexpr std::uint64_t kWholeDeadlineMs = 50000;
constexpr DWORD kUsageTimeoutMs = 20000;
constexpr DWORD kRefreshTimeoutMs = 20000;
constexpr DWORD kComponentTimeoutCapMs = 5000;

constexpr DWORD ComponentTimeoutMs(DWORD phaseRemainderMs) noexcept {
  return phaseRemainderMs == 0
             ? 0
             : (phaseRemainderMs < kComponentTimeoutCapMs
                    ? phaseRemainderMs
                    : kComponentTimeoutCapMs);
}

enum class Status {
  Ok,
  BadRequest,
  SecurityPolicy,
  CredentialMissing,
  CredentialUnsupported,
  CredentialMalformed,
  CredentialChanged,
  LockContended,
  AuthLocked,
  Transient,
  SchemaMismatch,
  Deadline,
  Internal,
};

const char* StatusName(Status status) noexcept;

class SecureBytes final {
 public:
  SecureBytes() noexcept;
  explicit SecureBytes(std::size_t capacity) noexcept;
  ~SecureBytes();

  SecureBytes(const SecureBytes&) = delete;
  SecureBytes& operator=(const SecureBytes&) = delete;
  SecureBytes(SecureBytes&& other) noexcept;
  SecureBytes& operator=(SecureBytes&& other) noexcept;

  bool Reserve(std::size_t capacity) noexcept;
  bool Resize(std::size_t size) noexcept;
  bool Assign(const void* data, std::size_t size) noexcept;
  bool AssignLiteral(const char* value) noexcept;
  bool Append(const void* data, std::size_t size) noexcept;
  bool AppendLiteral(const char* value) noexcept;
  bool AppendByte(unsigned char value) noexcept;
  void Clear() noexcept;

  unsigned char* data() noexcept { return data_; }
  const unsigned char* data() const noexcept { return data_; }
  std::size_t size() const noexcept { return size_; }
  std::size_t capacity() const noexcept { return capacity_; }
  bool empty() const noexcept { return size_ == 0; }
  bool locked() const noexcept { return locked_; }
  bool Equals(const SecureBytes& other) const noexcept;
  bool EqualsLiteral(const char* value) const noexcept;

 private:
  void Release() noexcept;
  unsigned char* data_;
  std::size_t size_;
  std::size_t capacity_;
  bool locked_;
  bool werExcluded_;
};

using ZeroizeObserver = void (*)(const unsigned char*, std::size_t) noexcept;
void SetZeroizeObserverForSyntheticHost(ZeroizeObserver observer) noexcept;
bool PrepareSecureMemoryWorkingSet() noexcept;

enum class JsonType { Null, Boolean, Number, String, Array, Object };

struct JsonValue final {
  JsonType type = JsonType::Null;
  bool boolean = false;
  double number = 0.0;
  SecureBytes string;
  std::vector<std::unique_ptr<JsonValue>> array;
  std::vector<std::pair<SecureBytes, std::unique_ptr<JsonValue>>> object;
  std::size_t sourceStart = 0;
  std::size_t sourceEnd = 0;

  JsonValue() noexcept = default;
  JsonValue(const JsonValue&) = delete;
  JsonValue& operator=(const JsonValue&) = delete;
  JsonValue(JsonValue&&) noexcept = default;
  JsonValue& operator=(JsonValue&&) noexcept = default;
};

Status ParseJson(const SecureBytes& input, std::unique_ptr<JsonValue>& output) noexcept;
JsonValue* FindMember(JsonValue& object, const char* key) noexcept;
const JsonValue* FindMember(const JsonValue& object, const char* key) noexcept;
bool SerializeJson(const JsonValue& value, SecureBytes& output,
                   std::size_t limit) noexcept;
Status ProjectClaudeCredential(const SecureBytes& fullDocument,
                               SecureBytes& projectedDocument) noexcept;
Status PatchClaudeCredential(const SecureBytes& currentFullDocument,
                             const SecureBytes& replacementProjection,
                             SecureBytes& nextFullDocument) noexcept;

struct CredentialVersion final {
  DWORD lastWrittenLow = 0;
  DWORD lastWrittenHigh = 0;
  DWORD blobSize = 0;

  bool operator==(const CredentialVersion& other) const noexcept {
    return lastWrittenLow == other.lastWrittenLow &&
           lastWrittenHigh == other.lastWrittenHigh &&
           blobSize == other.blobSize;
  }
  bool operator!=(const CredentialVersion& other) const noexcept {
    return !(*this == other);
  }
};

struct CredentialRecord final {
  CredentialVersion version;
  SecureBytes blob;
  SecureBytes storageDocument;
};

enum class TransportKind { Response, Timeout, Failure, Oversized };

enum class TransportEndpoint { None, Refresh, Usage };
enum class TransportPhase {
  None,
  SessionOptions,
  Connect,
  Send,
  Receive,
  Headers,
  Body,
};
enum class TransportClass {
  None,
  Timeout,
  NameResolution,
  CannotConnect,
  SecureFailure,
  OptionFailure,
  Http429,
  Http5xx,
  Other,
};

struct TransportDiagnostic final {
  TransportEndpoint endpoint = TransportEndpoint::None;
  TransportPhase phase = TransportPhase::None;
  TransportClass failureClass = TransportClass::None;

  bool available() const noexcept {
    return endpoint != TransportEndpoint::None &&
           phase != TransportPhase::None &&
           failureClass != TransportClass::None;
  }
};

struct HttpReply final {
  TransportKind kind = TransportKind::Failure;
  DWORD statusCode = 0;
  TransportDiagnostic diagnostic;
  SecureBytes body;
};

class Adapter {
 public:
  virtual ~Adapter() = default;
  virtual Status ReadCredential(CredentialRecord& record) noexcept = 0;
  virtual Status AcquireCredentialWriteLock() noexcept = 0;
  virtual void ReleaseCredentialWriteLock() noexcept = 0;
  virtual Status WriteCredential(const CredentialRecord& expected,
                                 const SecureBytes& replacement) noexcept = 0;
  virtual Status UsageRequest(const SecureBytes& accessToken,
                              DWORD timeoutMs,
                              HttpReply& reply) noexcept = 0;
  virtual Status RefreshRequest(const SecureBytes& requestBody,
                                DWORD timeoutMs,
                                HttpReply& reply) noexcept = 0;
  virtual std::uint64_t NowUnixMilliseconds() const noexcept = 0;
  virtual std::uint64_t MonotonicMilliseconds() const noexcept = 0;
};

struct Metric final {
  bool available = false;
  std::uint64_t remainingMicros = 0;
  bool resetAvailable = false;
  std::int64_t resetsAt = 0;
};

struct Result final {
  Status status = Status::Internal;
  bool hasCredentialVersion = false;
  CredentialVersion credentialVersion;
  TransportDiagnostic transportDiagnostic;
  Metric claudeSession;
  Metric claudeWeeklyAll;
  Metric claudeFableWeekly;
};

struct ProtocolRequest final {
  char requestId[33] = {};
  std::uint64_t acquisitionSequence = 0;
};

Status ParseProtocolRequest(const SecureBytes& input,
                            ProtocolRequest& request) noexcept;
Result Execute(Adapter& adapter, std::uint64_t acquisitionSequence) noexcept;
bool FormatResult(const Result& result, const ProtocolRequest& request,
                  std::int64_t acquiredAtUnixSeconds,
                  std::uint64_t durationMs,
                  std::string& output) noexcept;

}  // namespace broker
