#include "broker_core.h"

#include <windows.h>
#include <werapi.h>

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

namespace {

using broker::CredentialRecord;
using broker::CredentialVersion;
using broker::HttpReply;
using broker::Metric;
using broker::Result;
using broker::SecureBytes;
using broker::Status;
using broker::TransportClass;
using broker::TransportEndpoint;
using broker::TransportKind;
using broker::TransportPhase;

constexpr std::uint64_t kNowSeconds = 1767225600ULL;
constexpr std::uint64_t kNowMilliseconds = kNowSeconds * 1000ULL;
static_assert(WER_FAULT_REPORTING_FLAG_NOHEAP == 1,
              "Pinned werapi.h NOHEAP flag changed.");

bool g_zeroizeFailure = false;
std::uint64_t g_zeroizeObservations = 0;

void ObserveZeroized(const unsigned char* data, std::size_t size) noexcept {
  ++g_zeroizeObservations;
  for (std::size_t index = 0; index < size; ++index) {
    if (data[index] != 0) {
      g_zeroizeFailure = true;
      return;
    }
  }
}

bool AppendSentinelAccess(SecureBytes& output) noexcept {
  return output.AppendLiteral("SYNTH-A") && output.AppendLiteral("CCESS-8f31-") &&
         output.AppendLiteral("72de-44a0-") && output.AppendLiteral("TOKEN-END");
}

bool AppendSentinelRefresh(SecureBytes& output) noexcept {
  return output.AppendLiteral("SYNTH-R") && output.AppendLiteral("EFRESH-6c29-") &&
         output.AppendLiteral("19ab-55d1-") && output.AppendLiteral("TOKEN-END");
}

bool AppendNewAccess(SecureBytes& output) noexcept {
  return output.AppendLiteral("ROTATED-A") && output.AppendLiteral("CCESS-a55d-") &&
         output.AppendLiteral("TOKEN-END");
}

bool AppendNewRefresh(SecureBytes& output) noexcept {
  return output.AppendLiteral("ROTATED-R") && output.AppendLiteral("EFRESH-b66e-") &&
         output.AppendLiteral("TOKEN-END");
}

bool StringContains(const std::string& haystack,
                    const SecureBytes& needle) noexcept {
  if (needle.empty() || needle.size() > haystack.size()) return false;
  const char* found = std::search(
      haystack.data(), haystack.data() + haystack.size(),
      reinterpret_cast<const char*>(needle.data()),
      reinterpret_cast<const char*>(needle.data() + needle.size()));
  return found != haystack.data() + haystack.size();
}

bool AppendJsonStringBytes(const SecureBytes& value,
                           SecureBytes& output) noexcept {
  if (!output.AppendByte('"')) return false;
  for (std::size_t index = 0; index < value.size(); ++index) {
    const unsigned char current = value.data()[index];
    if (current == '"' || current == '\\') {
      const unsigned char escaped[2] = {'\\', current};
      if (!output.Append(escaped, 2)) return false;
    } else if (current < 0x20 || !output.AppendByte(current)) {
      return false;
    }
  }
  return output.AppendByte('"');
}

constexpr const char* kExpectedClientId =
    "9d1c250a-e61b-44d9-88ed-5944d1962f5e";
constexpr const char* kExpectedClientIdJson =
    "\"9d1c250a-e61b-44d9-88ed-5944d1962f5e\"";

bool BuildCredentialVariant(std::uint64_t expiryMs,
                            const char* clientIdJsonValue,
                            const char* omittedRequiredField,
                            SecureBytes& output) noexcept {
  SecureBytes access;
  SecureBytes refresh;
  if (!AppendSentinelAccess(access) || !AppendSentinelRefresh(refresh) ||
      !output.AppendLiteral(
          "{\"mcpOAuth\":{\"preserved\":true},\"claudeAiOauth\":{"))
    return false;

  bool first = true;
  auto beginMember = [&](const char* name) noexcept {
    if ((!first && !output.AppendByte(',')) || !output.AppendByte('"') ||
        !output.AppendLiteral(name) || !output.AppendLiteral("\":"))
      return false;
    first = false;
    return true;
  };
  auto include = [&](const char* name) noexcept {
    return !omittedRequiredField ||
           std::strcmp(omittedRequiredField, name) != 0;
  };

  if (include("accessToken") &&
      (!beginMember("accessToken") || !AppendJsonStringBytes(access, output)))
    return false;
  if (include("refreshToken") &&
      (!beginMember("refreshToken") || !AppendJsonStringBytes(refresh, output)))
    return false;
  if (include("expiresAt")) {
    char expiry[64] = {};
    const int length = std::snprintf(
        expiry, sizeof(expiry), "%llu",
        static_cast<unsigned long long>(expiryMs));
    if (length <= 0 || static_cast<std::size_t>(length) >= sizeof(expiry) ||
        !beginMember("expiresAt") ||
        !output.Append(expiry, static_cast<std::size_t>(length))) {
      SecureZeroMemory(expiry, sizeof(expiry));
      return false;
    }
    SecureZeroMemory(expiry, sizeof(expiry));
  }
  if (!beginMember("refreshTokenExpiresAt") ||
      !output.AppendLiteral("1769904000000"))
    return false;
  if (include("scopes") &&
      (!beginMember("scopes") ||
       !output.AppendLiteral("[\"user:profile\",\"user:inference\"]")))
    return false;
  if (clientIdJsonValue &&
      (!beginMember("clientId") || !output.AppendLiteral(clientIdJsonValue)))
    return false;
  if (include("subscriptionType") &&
      (!beginMember("subscriptionType") || !output.AppendLiteral("\"max\"")))
    return false;
  if (include("rateLimitTier") &&
      (!beginMember("rateLimitTier") || !output.AppendLiteral("\"default\"")))
    return false;
  return output.AppendLiteral("},\"nonSecretVersion\":1}");
}

bool BuildExpectedRefreshRequest(SecureBytes& output) noexcept {
  SecureBytes refresh;
  return AppendSentinelRefresh(refresh) &&
      output.AppendLiteral(
          "{\"grant_type\":\"refresh_token\",\"refresh_token\":") &&
      AppendJsonStringBytes(refresh, output) &&
      output.AppendLiteral(",\"client_id\":\"") &&
      output.AppendLiteral(kExpectedClientId) &&
      output.AppendLiteral(
          "\",\"scope\":\"user:profile user:inference\"}");
}

bool BuildRefreshSuccess(SecureBytes& output) noexcept {
  SecureBytes access;
  SecureBytes refresh;
  if (!AppendNewAccess(access) || !AppendNewRefresh(refresh) ||
      !output.AppendLiteral("{\"access_token\":" ) ||
      !AppendJsonStringBytes(access, output) ||
      !output.AppendLiteral(",\"refresh_token\":" ) ||
      !AppendJsonStringBytes(refresh, output) ||
      !output.AppendLiteral(",\"expires_in\":3600}")) return false;
  return true;
}

bool BuildCurrentUsage(SecureBytes& output) noexcept {
  return output.AppendLiteral(
      "{\"five_hour\":{\"utilization\":12.25,\"resets_at\":\"2026-01-01T05:00:00Z\"},"
      "\"seven_day\":{\"utilization\":99,\"resets_at\":\"2026-01-07T00:00:00Z\"},"
      "\"limits\":["
      "{\"kind\":\"session\",\"group\":\"session\",\"percent\":12.25,"
      "\"is_active\":true},"
      "{\"kind\":\"weekly_all\",\"group\":\"weekly\",\"percent\":40,"
      "\"resets_at\":\"2026-01-07T00:00:00Z\"},"
      "{\"kind\":\"weekly_scoped\",\"group\":\"weekly\",\"percent\":22,"
      "\"resets_at\":\"2026-01-06T00:00:00Z\","
      "\"scope\":{\"model\":{\"display_name\":\"Fable\"}}}]}" );
}

struct ScriptedReply final {
  TransportKind kind = TransportKind::Response;
  DWORD statusCode = 200;
  SecureBytes body;
  std::uint64_t elapsedMs = 1;
  std::uint64_t componentDelayMs = 1;
  TransportPhase phase = TransportPhase::Receive;
  TransportClass failureClass = TransportClass::Other;
};

class SyntheticAdapter final : public broker::Adapter {
 public:
  SyntheticAdapter() noexcept {
    SetCredential(kNowMilliseconds + 3600000ULL);
  }

  bool SetCredential(std::uint64_t expiryMs) noexcept {
    return SetCredentialVariant(expiryMs, kExpectedClientIdJson, nullptr);
  }

  bool SetCredentialVariant(std::uint64_t expiryMs,
                            const char* clientIdJsonValue,
                            const char* omittedRequiredField) noexcept {
    credential_.storageDocument.Clear();
    credential_.blob.Clear();
    if (!BuildCredentialVariant(expiryMs, clientIdJsonValue,
                                omittedRequiredField,
                                credential_.storageDocument) ||
        broker::ProjectClaudeCredential(credential_.storageDocument,
                                        credential_.blob) != Status::Ok)
      return false;
    credential_.version.lastWrittenLow = 0x11223344;
    credential_.version.lastWrittenHigh = 0x01dc0000;
    credential_.version.blobSize =
        static_cast<DWORD>(credential_.storageDocument.size());
    return true;
  }

  void CorruptCredential() noexcept {
    credential_.storageDocument.Clear();
    credential_.storageDocument.AppendLiteral(
        "{\"claudeAiOauth\":{\"accessToken\":1}}");
    credential_.blob.Clear();
    broker::ProjectClaudeCredential(credential_.storageDocument, credential_.blob);
    credential_.version.blobSize =
        static_cast<DWORD>(credential_.storageDocument.size());
  }

  void BumpCredentialVersion() noexcept {
    ++credential_.version.lastWrittenLow;
  }

  void QueueUsage(ScriptedReply&& reply) { usage_.emplace_back(std::move(reply)); }
  void QueueRefresh(ScriptedReply&& reply) { refresh_.emplace_back(std::move(reply)); }

  Status ReadCredential(CredentialRecord& record) noexcept override {
    ++readCount;
    record.version = credential_.version;
    return record.blob.Assign(credential_.blob.data(), credential_.blob.size()) &&
        record.storageDocument.Assign(credential_.storageDocument.data(),
                                      credential_.storageDocument.size())
               ? Status::Ok : Status::Internal;
  }

  Status AcquireCredentialWriteLock() noexcept override {
    ++lockAcquireCount;
    if (lockContended || lockHeld_) return Status::LockContended;
    lockHeld_ = true;
    if (mutateOnLock) {
      ++credential_.version.lastWrittenLow;
      credential_.storageDocument.AppendByte(' ');
      credential_.version.blobSize =
          static_cast<DWORD>(credential_.storageDocument.size());
    }
    return Status::Ok;
  }

  void ReleaseCredentialWriteLock() noexcept override {
    if (lockHeld_) ++lockReleaseCount;
    lockHeld_ = false;
  }

  Status WriteCredential(const CredentialRecord& expected,
                         const SecureBytes& replacement) noexcept override {
    if (!lockHeld_) return Status::SecurityPolicy;
    if (credential_.version != expected.version ||
        !credential_.blob.Equals(expected.blob) ||
        !credential_.storageDocument.Equals(expected.storageDocument))
      return Status::CredentialChanged;
    ++writeCount;
    SecureBytes nextFull;
    if (broker::PatchClaudeCredential(credential_.storageDocument, replacement,
                                      nextFull) != Status::Ok)
      return Status::Internal;
    if (failAfterCommit) {
      credential_.storageDocument = std::move(nextFull);
      broker::ProjectClaudeCredential(credential_.storageDocument,
                                      credential_.blob);
      ++credential_.version.lastWrittenLow;
      credential_.version.blobSize =
          static_cast<DWORD>(credential_.storageDocument.size());
      return Status::Internal;
    }
    credential_.storageDocument = std::move(nextFull);
    if (broker::ProjectClaudeCredential(credential_.storageDocument,
                                        credential_.blob) != Status::Ok)
      return Status::Internal;
    ++credential_.version.lastWrittenLow;
    credential_.version.blobSize =
        static_cast<DWORD>(credential_.storageDocument.size());
    return Status::Ok;
  }

  Status UsageRequest(const SecureBytes& accessToken, DWORD timeoutMs,
                      HttpReply& reply) noexcept override {
    ++usageCount;
    usageTimeouts.emplace_back(timeoutMs);
    if (timeoutMs == 0 || timeoutMs > broker::kUsageTimeoutMs ||
        usageIndex_ >= usage_.size())
      return Status::SecurityPolicy;
    SecureBytes oldAccess;
    SecureBytes newAccess;
    if (!AppendSentinelAccess(oldAccess) || !AppendNewAccess(newAccess) ||
        (!accessToken.Equals(oldAccess) && !accessToken.Equals(newAccess)))
      return Status::SecurityPolicy;
    return TransferReply(usage_[usageIndex_++], timeoutMs,
                         TransportEndpoint::Usage, reply);
  }

  Status RefreshRequest(const SecureBytes& requestBody, DWORD timeoutMs,
                        HttpReply& reply) noexcept override {
    ++refreshCount;
    if (!lockHeld_) refreshBeforeLock = true;
    refreshTimeouts.emplace_back(timeoutMs);
    if (!lockHeld_ || timeoutMs == 0 || timeoutMs > broker::kRefreshTimeoutMs ||
        refreshIndex_ >= refresh_.size())
      return Status::SecurityPolicy;
    SecureBytes expectedRequest;
    if (!BuildExpectedRefreshRequest(expectedRequest) ||
        !requestBody.Equals(expectedRequest))
      return Status::SecurityPolicy;
    return TransferReply(refresh_[refreshIndex_++], timeoutMs,
                         TransportEndpoint::Refresh, reply);
  }

  std::uint64_t NowUnixMilliseconds() const noexcept override {
    return nowMilliseconds_;
  }

  std::uint64_t MonotonicMilliseconds() const noexcept override {
    return monotonicMilliseconds_;
  }

  bool lockContended = false;
  bool mutateOnLock = false;
  bool failAfterCommit = false;
  unsigned readCount = 0;
  unsigned lockAcquireCount = 0;
  unsigned lockReleaseCount = 0;
  unsigned writeCount = 0;
  unsigned usageCount = 0;
  unsigned refreshCount = 0;
  bool refreshBeforeLock = false;
  std::vector<DWORD> usageTimeouts;
  std::vector<DWORD> refreshTimeouts;

 private:
  Status TransferReply(ScriptedReply& source, DWORD phaseRemainderMs,
                       TransportEndpoint endpoint,
                       HttpReply& destination) noexcept {
    const DWORD componentTimeout = broker::ComponentTimeoutMs(phaseRemainderMs);
    destination.statusCode = 0;
    destination.diagnostic = broker::TransportDiagnostic{};
    destination.body.Clear();
    if (componentTimeout == 0 || source.componentDelayMs > componentTimeout ||
        source.elapsedMs > phaseRemainderMs) {
      monotonicMilliseconds_ += componentTimeout == 0 ? 0 : componentTimeout;
      destination.kind = TransportKind::Timeout;
      destination.diagnostic.endpoint = endpoint;
      destination.diagnostic.phase = source.phase;
      destination.diagnostic.failureClass = TransportClass::Timeout;
      return Status::Ok;
    }
    monotonicMilliseconds_ += source.elapsedMs;
    destination.kind = source.kind;
    destination.statusCode = source.statusCode;
    if (source.kind == TransportKind::Timeout ||
        source.kind == TransportKind::Failure) {
      destination.diagnostic.endpoint = endpoint;
      destination.diagnostic.phase = source.phase;
      destination.diagnostic.failureClass =
          source.kind == TransportKind::Timeout
              ? TransportClass::Timeout
              : source.failureClass;
    }
    return destination.body.Assign(source.body.data(), source.body.size())
               ? Status::Ok : Status::Internal;
  }

  CredentialRecord credential_;
  bool lockHeld_ = false;
  std::uint64_t nowMilliseconds_ = kNowMilliseconds;
  mutable std::uint64_t monotonicMilliseconds_ = 100;
  std::vector<ScriptedReply> usage_;
  std::vector<ScriptedReply> refresh_;
  std::size_t usageIndex_ = 0;
  std::size_t refreshIndex_ = 0;
};

ScriptedReply Reply(DWORD status, const char* body,
                    std::uint64_t elapsed = 1) {
  ScriptedReply reply;
  reply.statusCode = status;
  reply.elapsedMs = elapsed;
  if (body) reply.body.AssignLiteral(body);
  return reply;
}

ScriptedReply UsageSuccess() {
  ScriptedReply reply;
  BuildCurrentUsage(reply.body);
  return reply;
}

ScriptedReply RefreshSuccess() {
  ScriptedReply reply;
  BuildRefreshSuccess(reply.body);
  return reply;
}

ScriptedReply Transport(TransportKind kind) {
  ScriptedReply reply;
  reply.kind = kind;
  reply.statusCode = 0;
  reply.failureClass = kind == TransportKind::Timeout
                           ? TransportClass::Timeout
                           : TransportClass::Other;
  return reply;
}

bool MetricsExact(const Result& result) noexcept {
  return result.status == Status::Ok &&
      result.claudeSession.available &&
      result.claudeSession.remainingMicros == 87750000ULL &&
      result.claudeSession.resetAvailable &&
      result.claudeSession.resetsAt == 1767243600LL &&
      result.claudeWeeklyAll.available &&
      result.claudeWeeklyAll.remainingMicros == 60000000ULL &&
      result.claudeWeeklyAll.resetAvailable &&
      result.claudeWeeklyAll.resetsAt == 1767744000LL &&
      result.claudeFableWeekly.available &&
      result.claudeFableWeekly.remainingMicros == 78000000ULL &&
      result.claudeFableWeekly.resetAvailable &&
      result.claudeFableWeekly.resetsAt == 1767657600LL;
}

bool TestCredentialProjectionAndPatch() {
  const char* current =
      " {\"mcpOAuth\": {\"escaped\":\"\\u0041\",\"number\":9007199254740993},"
      "\"claudeAiOauth\": {\"accessToken\":\"old\",\"refreshToken\":\"refresh\","
      "\"expiresAt\":1,\"scopes\":[\"user:inference\"],"
      "\"clientId\":\"9d1c250a-e61b-44d9-88ed-5944d1962f5e\","
      "\"subscriptionType\":\"max\",\"rateLimitTier\":\"default\"},"
      "\"tail\": [1,  2]}\r\n";
  const char* replacement =
      "{\"claudeAiOauth\":{\"accessToken\":\"new\","
      "\"refreshToken\":\"rotated\",\"expiresAt\":2,"
      "\"scopes\":[\"user:inference\"],"
      "\"clientId\":\"9d1c250a-e61b-44d9-88ed-5944d1962f5e\","
      "\"subscriptionType\":\"max\",\"rateLimitTier\":\"default\"}}";
  const char* expected =
      " {\"mcpOAuth\": {\"escaped\":\"\\u0041\",\"number\":9007199254740993},"
      "\"claudeAiOauth\": {\"accessToken\":\"new\","
      "\"refreshToken\":\"rotated\",\"expiresAt\":2,"
      "\"scopes\":[\"user:inference\"],"
      "\"clientId\":\"9d1c250a-e61b-44d9-88ed-5944d1962f5e\","
      "\"subscriptionType\":\"max\",\"rateLimitTier\":\"default\"},"
      "\"tail\": [1,  2]}\r\n";
  SecureBytes full;
  SecureBytes projection;
  SecureBytes next;
  SecureBytes replacementBytes;
  SecureBytes expectedBytes;
  if (!full.AssignLiteral(current) || !replacementBytes.AssignLiteral(replacement) ||
      !expectedBytes.AssignLiteral(expected) ||
      broker::ProjectClaudeCredential(full, projection) != Status::Ok ||
      broker::PatchClaudeCredential(full, replacementBytes, next) != Status::Ok ||
      !next.Equals(expectedBytes))
    return false;

  for (const char* invalid : {
      "{\"mcpOAuth\":{}}",
      "{\"claudeAiOauth\":1}",
      "{\"claudeAiOauth\":{},\"claudeAiOauth\":{}}",
      "{broken"}) {
    SecureBytes value;
    SecureBytes ignored;
    if (!value.AssignLiteral(invalid) ||
        broker::ProjectClaudeCredential(value, ignored) == Status::Ok)
      return false;
  }

  SecureBytes oversized;
  if (!oversized.Resize(broker::kCredentialFileLimit + 1)) return false;
  std::memset(oversized.data(), ' ', oversized.size());
  if (broker::ProjectClaudeCredential(oversized, projection) !=
      Status::CredentialUnsupported)
    return false;

  SecureBytes largeProjection;
  if (!largeProjection.AppendLiteral("{\"claudeAiOauth\":{\"pad\":\""))
    return false;
  const std::size_t padStart = largeProjection.size();
  if (!largeProjection.Resize(padStart + broker::kCredentialProjectionLimit) )
    return false;
  std::memset(largeProjection.data() + padStart, 'a',
              broker::kCredentialProjectionLimit);
  if (!largeProjection.AppendLiteral("\"}}")) return false;
  return broker::ProjectClaudeCredential(largeProjection, projection) ==
      Status::CredentialUnsupported;
}

bool TestProtocol() {
  SecureBytes valid;
  valid.AssignLiteral(
      "{\"protocol\":\"ai-usage-claude-broker/1\","
      "\"requestId\":\"0123456789abcdef0123456789abcdef\","
      "\"acquisitionSequence\":7}");
  broker::ProtocolRequest request;
  if (broker::ParseProtocolRequest(valid, request) != Status::Ok ||
      request.acquisitionSequence != 7 ||
      std::strcmp(request.requestId, "0123456789abcdef0123456789abcdef") != 0)
    return false;
  SecureBytes extra;
  extra.AssignLiteral(
      "{\"protocol\":\"ai-usage-claude-broker/1\","
      "\"requestId\":\"0123456789abcdef0123456789abcdef\","
      "\"acquisitionSequence\":7,\"extra\":1}");
  if (broker::ParseProtocolRequest(extra, request) != Status::BadRequest) return false;
  SecureBytes uppercase;
  uppercase.AssignLiteral(
      "{\"protocol\":\"ai-usage-claude-broker/1\","
      "\"requestId\":\"0123456789ABCDEF0123456789abcdef\","
      "\"acquisitionSequence\":7}");
  return broker::ParseProtocolRequest(uppercase, request) == Status::BadRequest;
}

bool TestValidAndOutput() {
  SyntheticAdapter adapter;
  adapter.QueueUsage(UsageSuccess());
  Result result = broker::Execute(adapter, 11);
  if (!MetricsExact(result) || adapter.usageCount != 1 ||
      adapter.refreshCount != 0 || adapter.writeCount != 0) {
    std::printf("{\"diagnostic\":\"valid_usage\",\"status\":\"%s\","
                "\"available\":[%u,%u,%u],\"remaining\":[%llu,%llu,%llu],"
                "\"counts\":[%u,%u,%u]}\n",
                broker::StatusName(result.status),
                result.claudeSession.available ? 1U : 0U,
                result.claudeWeeklyAll.available ? 1U : 0U,
                result.claudeFableWeekly.available ? 1U : 0U,
                static_cast<unsigned long long>(result.claudeSession.remainingMicros),
                static_cast<unsigned long long>(result.claudeWeeklyAll.remainingMicros),
                static_cast<unsigned long long>(result.claudeFableWeekly.remainingMicros),
                adapter.usageCount, adapter.refreshCount, adapter.writeCount);
    return false;
  }
  broker::ProtocolRequest request;
  std::memcpy(request.requestId, "0123456789abcdef0123456789abcdef", 33);
  request.acquisitionSequence = 11;
  std::string output;
  if (!broker::FormatResult(result, request, kNowSeconds, 2, output) ||
      output.size() > broker::kOutputLimit ||
      output.find("\"protocol\":\"ai-usage-claude-broker/1\"") == std::string::npos ||
      output.find("\"transportDiagnostic\":null") == std::string::npos ||
      output.find("\"remainingMicros\":87750000") == std::string::npos ||
      output.find("accessToken") != std::string::npos ||
      output.find("utilization") != std::string::npos) return false;
  SecureBytes access;
  SecureBytes refresh;
  AppendSentinelAccess(access);
  AppendSentinelRefresh(refresh);
  const bool clean = !StringContains(output, access) && !StringContains(output, refresh);
  SecureZeroMemory(output.data(), output.size());
  output.clear();
  return clean;
}

bool TestExpiredRefresh() {
  SyntheticAdapter adapter;
  adapter.SetCredential(kNowMilliseconds + 1000);
  adapter.QueueRefresh(RefreshSuccess());
  adapter.QueueUsage(UsageSuccess());
  Result result = broker::Execute(adapter, 12);
  return MetricsExact(result) && adapter.refreshCount == 1 &&
      adapter.usageCount == 1 && adapter.writeCount == 1 &&
      adapter.lockAcquireCount == 1 && adapter.lockReleaseCount == 1 &&
      !adapter.refreshBeforeLock;
}

bool TestUsage401Refresh() {
  SyntheticAdapter adapter;
  adapter.QueueUsage(Reply(401, "{}"));
  adapter.QueueRefresh(RefreshSuccess());
  adapter.QueueUsage(UsageSuccess());
  Result result = broker::Execute(adapter, 13);
  return MetricsExact(result) && adapter.usageCount == 2 &&
         adapter.refreshCount == 1 && adapter.writeCount == 1;
}

bool TestChangedAndLockContention() {
  SyntheticAdapter changed;
  changed.SetCredential(kNowMilliseconds + 1000);
  changed.mutateOnLock = true;
  changed.QueueRefresh(RefreshSuccess());
  Result changedResult = broker::Execute(changed, 14);
  if (changedResult.status != Status::CredentialChanged || changed.writeCount != 0 ||
      changed.refreshCount != 0 || changed.refreshBeforeLock ||
      changed.lockAcquireCount != 1 || changed.lockReleaseCount != 1) return false;

  SyntheticAdapter contended;
  contended.SetCredential(kNowMilliseconds + 1000);
  contended.lockContended = true;
  contended.QueueRefresh(RefreshSuccess());
  Result contendedResult = broker::Execute(contended, 15);
  return contendedResult.status == Status::LockContended &&
         contended.writeCount == 0 && contended.lockAcquireCount == 1;
}

bool TestAuthSuppressionIsProcessMemoryOnly() {
  SyntheticAdapter adapter;
  adapter.SetCredential(kNowMilliseconds + 1000);
  adapter.QueueRefresh(Reply(400, "{\"error\":\"invalid_grant\"}"));
  adapter.QueueRefresh(Reply(400, "{\"error\":\"invalid_grant\"}"));
  Result first = broker::Execute(adapter, 16);
  Result second = broker::Execute(adapter, 17);
  if (first.status != Status::AuthLocked || second.status != Status::AuthLocked ||
      adapter.refreshCount != 2)
    return false;

  SyntheticAdapter final401;
  final401.QueueUsage(Reply(401, "{}"));
  final401.QueueRefresh(RefreshSuccess());
  final401.QueueUsage(Reply(401, "{}"));
  Result result = broker::Execute(final401, 32);
  return result.status == Status::AuthLocked && final401.refreshCount == 1 &&
         final401.usageCount == 2;
}

bool TestCommitFailureAndMalformedRefresh() {
  SyntheticAdapter commitFailure;
  commitFailure.SetCredential(kNowMilliseconds + 1000);
  commitFailure.failAfterCommit = true;
  commitFailure.QueueRefresh(RefreshSuccess());
  Result failed = broker::Execute(commitFailure, 33);
  if (failed.status != Status::Internal || commitFailure.writeCount != 1)
    return false;
  commitFailure.QueueUsage(UsageSuccess());
  Result recovered = broker::Execute(commitFailure, 34);
  if (!MetricsExact(recovered) || commitFailure.refreshCount != 1)
    return false;

  SyntheticAdapter malformed;
  malformed.SetCredential(kNowMilliseconds + 1000);
  malformed.QueueRefresh(Reply(200, "{\"access_token\":1,\"expires_in\":3600}"));
  Result malformedResult = broker::Execute(malformed, 35);
  return malformedResult.status == Status::SchemaMismatch &&
         malformed.writeCount == 0 && malformed.usageCount == 0;
}

bool TestTimeoutLedgersAndDiagnostic() {
  for (TransportKind kind : {TransportKind::Timeout, TransportKind::Failure}) {
    SyntheticAdapter adapter;
    adapter.QueueUsage(Transport(kind));
    Result result = broker::Execute(adapter, 19);
    if (result.status != Status::Transient ||
        result.transportDiagnostic.endpoint != TransportEndpoint::Usage ||
        result.transportDiagnostic.phase != TransportPhase::Receive ||
        result.transportDiagnostic.failureClass !=
            (kind == TransportKind::Timeout ? TransportClass::Timeout
                                            : TransportClass::Other))
      return false;
    broker::ProtocolRequest request;
    std::memcpy(request.requestId, "0123456789abcdef0123456789abcdef", 33);
    request.acquisitionSequence = 19;
    std::string output;
    if (!broker::FormatResult(result, request, kNowSeconds, 1, output) ||
        output.find("\"transportDiagnostic\":{\"endpoint\":\"usage\","
                    "\"phase\":\"receive\",\"class\":") == std::string::npos ||
        output.find("winhttp") != std::string::npos ||
        output.find("api.anthropic.com") != std::string::npos)
      return false;
    SecureZeroMemory(output.data(), output.size());
  }
  for (DWORD code : {429UL, 500UL, 503UL}) {
    SyntheticAdapter adapter;
    adapter.QueueUsage(Reply(code, "{}"));
    Result result = broker::Execute(adapter, 20);
    if (result.status != Status::Transient ||
        result.transportDiagnostic.endpoint != TransportEndpoint::Usage ||
        result.transportDiagnostic.phase != TransportPhase::Headers ||
        result.transportDiagnostic.failureClass !=
            (code == 429 ? TransportClass::Http429 : TransportClass::Http5xx))
      return false;
  }

  SyntheticAdapter refreshFailure;
  refreshFailure.SetCredential(kNowMilliseconds + 1000);
  ScriptedReply secureRefresh = Transport(TransportKind::Failure);
  secureRefresh.phase = TransportPhase::Send;
  secureRefresh.failureClass = TransportClass::SecureFailure;
  refreshFailure.QueueRefresh(std::move(secureRefresh));
  Result refreshFailureResult = broker::Execute(refreshFailure, 20);
  if (refreshFailureResult.status != Status::Transient ||
      refreshFailureResult.transportDiagnostic.endpoint !=
          TransportEndpoint::Refresh ||
      refreshFailureResult.transportDiagnostic.phase != TransportPhase::Send ||
      refreshFailureResult.transportDiagnostic.failureClass !=
          TransportClass::SecureFailure)
    return false;

  SyntheticAdapter componentPass;
  ScriptedReply fourSeconds = UsageSuccess();
  fourSeconds.elapsedMs = 4000;
  fourSeconds.componentDelayMs = 4000;
  componentPass.QueueUsage(std::move(fourSeconds));
  if (!MetricsExact(broker::Execute(componentPass, 21)) ||
      componentPass.usageTimeouts != std::vector<DWORD>{20000})
    return false;

  SyntheticAdapter componentFail;
  ScriptedReply fivePointTwoFive = UsageSuccess();
  fivePointTwoFive.elapsedMs = 5250;
  fivePointTwoFive.componentDelayMs = 5250;
  componentFail.QueueUsage(std::move(fivePointTwoFive));
  Result componentFailure = broker::Execute(componentFail, 22);
  if (componentFailure.status != Status::Transient ||
      componentFailure.transportDiagnostic.failureClass != TransportClass::Timeout)
    return false;

  SyntheticAdapter intendedPath;
  ScriptedReply initial401 = Reply(401, "{}");
  initial401.elapsedMs = 6000;
  initial401.componentDelayMs = 4000;
  ScriptedReply refresh = RefreshSuccess();
  refresh.elapsedMs = 17000;
  refresh.componentDelayMs = 4000;
  ScriptedReply finalUsage = UsageSuccess();
  finalUsage.elapsedMs = 14000;
  finalUsage.componentDelayMs = 4000;
  intendedPath.QueueUsage(std::move(initial401));
  intendedPath.QueueRefresh(std::move(refresh));
  intendedPath.QueueUsage(std::move(finalUsage));
  if (!MetricsExact(broker::Execute(intendedPath, 23)) ||
      intendedPath.usageTimeouts != std::vector<DWORD>({20000, 14000}) ||
      intendedPath.refreshTimeouts != std::vector<DWORD>({20000}) ||
      !(37000 > 20000 && 37000 <= broker::kWholeDeadlineMs) ||
      !(4000 > 1250 && 4000 > 2000 &&
        4000 <= broker::kComponentTimeoutCapMs))
    return false;

  SyntheticAdapter exhaustedUsage;
  ScriptedReply longInitial = Reply(401, "{}");
  longInitial.elapsedMs = 12000;
  longInitial.componentDelayMs = 4000;
  ScriptedReply shortRefresh = RefreshSuccess();
  shortRefresh.elapsedMs = 1;
  shortRefresh.componentDelayMs = 1;
  ScriptedReply overRemainder = UsageSuccess();
  overRemainder.elapsedMs = 9000;
  overRemainder.componentDelayMs = 4000;
  exhaustedUsage.QueueUsage(std::move(longInitial));
  exhaustedUsage.QueueRefresh(std::move(shortRefresh));
  exhaustedUsage.QueueUsage(std::move(overRemainder));
  Result exhausted = broker::Execute(exhaustedUsage, 24);
  return exhausted.status == Status::Transient &&
         exhausted.transportDiagnostic.failureClass == TransportClass::Timeout &&
         exhaustedUsage.usageTimeouts == std::vector<DWORD>({20000, 8000});
}

bool TestUnexpected4xxDoesNotAuthLock() {
  for (DWORD code : {400UL, 403UL, 404UL}) {
    SyntheticAdapter usage;
    usage.QueueUsage(Reply(code, "{\"error\":\"route_changed\"}"));
    Result usageResult = broker::Execute(usage, 30);
    if (usageResult.status != Status::SchemaMismatch)
      return false;

    SyntheticAdapter refresh;
    refresh.SetCredential(kNowMilliseconds + 1000);
    refresh.QueueRefresh(Reply(code, "{\"error\":\"route_changed\"}"));
    Result refreshResult = broker::Execute(refresh, 31);
    if (refreshResult.status != Status::SchemaMismatch)
      return false;
  }
  return true;
}

bool TestSchemaFailures() {
  for (const char* body : {
      "{broken",
      "{\"five_hour\":{\"utilization\":1,\"utilization\":2,"
        "\"resets_at\":\"2026-01-01T05:00:00Z\"}}",
      "{\"unknown\":1}"}) {
    SyntheticAdapter adapter;
    adapter.QueueUsage(Reply(200, body));
    if (broker::Execute(adapter, 22).status != Status::SchemaMismatch) return false;
  }
  SyntheticAdapter oversized;
  oversized.QueueUsage(Transport(TransportKind::Oversized));
  return broker::Execute(oversized, 23).status == Status::SchemaMismatch;
}

bool TestMetricIsolationAndFableIdentity() {
  const char* duplicateFable =
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},"
      "\"seven_day\":{\"utilization\":20,\"resets_at\":\"2026-01-07T00:00:00Z\"},"
      "\"limits\":["
      "{\"kind\":\"weekly_scoped\",\"group\":\"weekly\",\"percent\":1,"
      "\"resets_at\":\"2026-01-06T00:00:00Z\",\"scope\":{\"model\":{\"display_name\":\"Fable\"}}},"
      "{\"kind\":\"weekly_scoped\",\"group\":\"weekly\",\"percent\":2,"
      "\"resets_at\":\"2026-01-06T00:00:00Z\",\"scope\":{\"model\":{\"display_name\":\"Fable 5\"}}}]}";
  SyntheticAdapter duplicate;
  duplicate.QueueUsage(Reply(200, duplicateFable));
  Result duplicateResult = broker::Execute(duplicate, 24);
  if (duplicateResult.status != Status::SchemaMismatch) return false;

  const char* surfaceOnly =
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},"
      "\"limits\":[{\"kind\":\"weekly_scoped\",\"group\":\"weekly\",\"percent\":1,"
      "\"resets_at\":\"2026-01-06T00:00:00Z\",\"scope\":{\"surface\":{\"display_name\":\"Fable\"}}}]}";
  SyntheticAdapter surface;
  surface.QueueUsage(Reply(200, surfaceOnly));
  Result surfaceResult = broker::Execute(surface, 25);
  if (surfaceResult.status != Status::Ok || surfaceResult.claudeFableWeekly.available)
    return false;

  const char* normalized =
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},"
      "\"limits\":[{\"kind\":\"weekly_scoped\",\"group\":\"weekly\",\"percent\":33,"
      "\"resets_at\":\"2026-01-06T00:00:00Z\","
      "\"scope\":{\"model\":{\"display_name\":\"\\uFF26\\uFF41\\uFF42\\uFF4C\\uFF45\"}}}]}";
  SyntheticAdapter nfkc;
  nfkc.QueueUsage(Reply(200, normalized));
  Result nfkcResult = broker::Execute(nfkc, 26);
  if (nfkcResult.status != Status::Ok || !nfkcResult.claudeFableWeekly.available ||
      nfkcResult.claudeFableWeekly.remainingMicros != 67000000ULL) return false;

  const char* prefix =
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},"
      "\"limits\":[{\"kind\":\"weekly_scoped\",\"group\":\"weekly\",\"percent\":33,"
      "\"resets_at\":\"2026-01-06T00:00:00Z\","
      "\"scope\":{\"model\":{\"display_name\":\"Fable Beta\"}}}]}";
  SyntheticAdapter rejected;
  rejected.QueueUsage(Reply(200, prefix));
  Result rejectedResult = broker::Execute(rejected, 27);
  return rejectedResult.status == Status::Ok &&
         !rejectedResult.claudeFableWeekly.available;
}

bool TestQuotaSchemaDriftFailClosed() {
  const char* rejected[] = {
      // Present canonical session data must be valid, never silently null.
      "{\"five_hour\":null,\"seven_day\":{\"utilization\":20,\"resets_at\":\"2026-01-07T00:00:00Z\"}}",
      "{\"five_hour\":{\"utilization\":\"10\",\"resets_at\":\"2026-01-01T05:00:00Z\"}}",
      // A recognized shell with no valid baseline metric is not a success.
      "{\"limits\":[]}",
      "{\"seven_day\":null}",
      // Duplicate and malformed exact weekly-all rows fail closed.
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},\"limits\":[{\"kind\":\"weekly_all\",\"group\":\"weekly\",\"percent\":10,\"resets_at\":\"2026-01-06T00:00:00Z\"},{\"kind\":\"weekly_all\",\"group\":\"weekly\",\"percent\":20,\"resets_at\":\"2026-01-06T00:00:00Z\"}]}",
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},\"limits\":[{\"kind\":\"weekly_all\",\"group\":\"weekly\",\"percent\":\"10\",\"resets_at\":\"2026-01-06T00:00:00Z\"}]}",
      // Invalid seven-day fallback must not become an unavailable lane.
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},\"seven_day\":{\"utilization\":20,\"resets_at\":null}}",
      // A uniquely identified Fable row is canonical and must parse.
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},\"limits\":[{\"kind\":\"weekly_scoped\",\"group\":\"weekly\",\"percent\":\"33\",\"resets_at\":\"2026-01-06T00:00:00Z\",\"scope\":{\"model\":{\"display_name\":\"Fable\"}}}]}",
      // New and legacy session projections describe the same quota and must
      // not silently disagree.
      "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},\"limits\":[{\"kind\":\"session\",\"group\":\"session\",\"percent\":11,\"is_active\":true}]}",
      "{\"limits\":[{\"kind\":\"session\",\"group\":\"session\",\"percent\":5},{\"kind\":\"session\",\"group\":\"session\",\"percent\":5}]}",
      "{\"limits\":[{\"kind\":\"session\",\"group\":\"session\",\"percent\":\"5\"}]}",
      "{\"limits\":[{\"kind\":\"session\",\"group\":\"weekly\",\"percent\":5}]}",
      "{\"limits\":[{\"kind\":\"session\",\"group\":\"session\",\"percent\":5,\"is_active\":\"yes\"}]}"
  };
  unsigned sequence = 40;
  for (const char* body : rejected) {
    SyntheticAdapter adapter;
    adapter.QueueUsage(Reply(200, body));
    if (broker::Execute(adapter, sequence++).status != Status::SchemaMismatch)
      return false;
  }

  const char* changedHigherRemaining =
      "{\"five_hour\":{\"utilization\":2,\"resets_at\":\"2026-01-01T05:00:00Z\"},"
      "\"limits\":[{\"kind\":\"weekly_all\",\"group\":\"weekly\",\"percent\":5,"
      "\"resets_at\":\"2026-01-06T00:00:00Z\"}]}";
  SyntheticAdapter changed;
  changed.QueueUsage(Reply(200, changedHigherRemaining));
  Result changedResult = broker::Execute(changed, sequence++);
  if (changedResult.status != Status::Ok ||
      !changedResult.claudeSession.available ||
      changedResult.claudeSession.remainingMicros != 98000000ULL ||
      !changedResult.claudeWeeklyAll.available ||
      changedResult.claudeWeeklyAll.remainingMicros != 95000000ULL ||
      changedResult.claudeFableWeekly.available) return false;

  // Current Claude payloads can place the session window in limits[] and omit
  // a reset timestamp.  The numeric value remains canonical; no reset is
  // fabricated on the sanitized wire.
  const char* structuredSession =
      "{\"five_hour\":null,\"limits\":["
      "{\"kind\":\"session\",\"group\":\"session\",\"percent\":5,\"is_active\":true},"
      "{\"kind\":\"weekly_all\",\"group\":\"weekly\",\"percent\":20,"
      "\"resets_at\":\"2026-01-07T00:00:00Z\"}]}";
  SyntheticAdapter structured;
  structured.QueueUsage(Reply(200, structuredSession));
  Result structuredResult = broker::Execute(structured, sequence++);
  if (structuredResult.status != Status::Ok ||
      !structuredResult.claudeSession.available ||
      structuredResult.claudeSession.remainingMicros != 95000000ULL ||
      structuredResult.claudeSession.resetAvailable ||
      !structuredResult.claudeWeeklyAll.available)
    return false;
  broker::ProtocolRequest structuredRequest;
  std::memcpy(structuredRequest.requestId,
              "1234567890abcdef1234567890abcdef", 33);
  structuredRequest.acquisitionSequence = sequence - 1;
  std::string structuredOutput;
  if (!broker::FormatResult(structuredResult, structuredRequest, kNowSeconds,
                            1, structuredOutput) ||
      structuredOutput.find(
          "\"claude_session\":{\"remainingMicros\":95000000,\"resetUnixSeconds\":null}") ==
          std::string::npos)
    return false;
  SecureZeroMemory(structuredOutput.data(), structuredOutput.size());

  const char* fullyUsed =
      "{\"limits\":[{\"kind\":\"session\",\"group\":\"session\","
      "\"percent\":100,\"is_active\":false}]}";
  SyntheticAdapter used;
  used.QueueUsage(Reply(200, fullyUsed));
  Result usedResult = broker::Execute(used, sequence);
  return usedResult.status == Status::Ok &&
         usedResult.claudeSession.available &&
         usedResult.claudeSession.remainingMicros == 0 &&
         !usedResult.claudeFableWeekly.available;
}

bool TestUnknownRawFieldDoesNotCross() {
  SecureBytes payload;
  if (!payload.AppendLiteral(
          "{\"five_hour\":{\"utilization\":10,\"resets_at\":\"2026-01-01T05:00:00Z\"},"
          "\"unknown_provider_field\":")) return false;
  SecureBytes sentinel;
  if (!AppendSentinelAccess(sentinel) || !AppendJsonStringBytes(sentinel, payload) ||
      !payload.AppendLiteral("}")) return false;
  ScriptedReply reply;
  reply.body = std::move(payload);
  SyntheticAdapter adapter;
  adapter.QueueUsage(std::move(reply));
  Result result = broker::Execute(adapter, 28);
  broker::ProtocolRequest request;
  std::memcpy(request.requestId, "fedcba9876543210fedcba9876543210", 33);
  request.acquisitionSequence = 28;
  std::string output;
  const bool formatted = broker::FormatResult(result, request, kNowSeconds, 1, output);
  const bool clean = formatted && !StringContains(output, sentinel) &&
                     output.find("unknown_provider_field") == std::string::npos;
  if (!output.empty()) SecureZeroMemory(output.data(), output.size());
  return clean;
}

bool TestMalformedCredential() {
  SyntheticAdapter adapter;
  adapter.CorruptCredential();
  return broker::Execute(adapter, 29).status == Status::CredentialMalformed &&
         adapter.usageCount == 0 && adapter.refreshCount == 0;
}

bool TestOptionalClientIdPolicy() {
  const char* acceptedClientIds[] = {nullptr, kExpectedClientIdJson};
  std::uint64_t sequence = 40;
  for (const char* clientId : acceptedClientIds) {
    SyntheticAdapter adapter;
    if (!adapter.SetCredentialVariant(kNowMilliseconds + 3600000ULL,
                                      clientId, nullptr))
      return false;
    adapter.QueueUsage(UsageSuccess());
    const Result result = broker::Execute(adapter, sequence++);
    if (!MetricsExact(result) || adapter.usageCount != 1 ||
        adapter.refreshCount != 0)
      return false;
  }

  const char* rejectedClientIds[] = {
      "\"not-the-fixed-client-id\"", "1", "\"\""};
  for (const char* clientId : rejectedClientIds) {
    SyntheticAdapter adapter;
    if (!adapter.SetCredentialVariant(kNowMilliseconds + 3600000ULL,
                                      clientId, nullptr))
      return false;
    const Result result = broker::Execute(adapter, sequence++);
    if (result.status != Status::CredentialMalformed ||
        adapter.usageCount != 0 || adapter.refreshCount != 0)
      return false;
  }

  const char* requiredFields[] = {
      "accessToken", "refreshToken", "expiresAt", "scopes",
      "subscriptionType", "rateLimitTier"};
  for (const char* omitted : requiredFields) {
    SyntheticAdapter adapter;
    if (!adapter.SetCredentialVariant(kNowMilliseconds + 3600000ULL,
                                      nullptr, omitted))
      return false;
    const Result result = broker::Execute(adapter, sequence++);
    if (result.status != Status::CredentialMalformed ||
        adapter.usageCount != 0 || adapter.refreshCount != 0)
      return false;
  }

  SyntheticAdapter refresh;
  if (!refresh.SetCredentialVariant(kNowMilliseconds + 1000ULL,
                                    nullptr, nullptr))
    return false;
  refresh.QueueRefresh(RefreshSuccess());
  refresh.QueueUsage(UsageSuccess());
  const Result refreshed = broker::Execute(refresh, sequence);
  return MetricsExact(refreshed) && refresh.refreshCount == 1 &&
         refresh.usageCount == 1 && refresh.writeCount == 1 &&
         refresh.lockAcquireCount == 1 && refresh.lockReleaseCount == 1 &&
         !refresh.refreshBeforeLock;
}

struct TestCase final {
  const char* name;
  bool (*function)();
};

int RunSelfTests() {
  broker::SetZeroizeObserverForSyntheticHost(ObserveZeroized);
  const TestCase tests[] = {
      {"protocol_exact_allowlist", TestProtocol},
      {"credential_projection_patch_bounds", TestCredentialProjectionAndPatch},
      {"valid_usage_and_sanitized_output", TestValidAndOutput},
      {"expired_refresh_compare_write_usage", TestExpiredRefresh},
      {"usage_401_single_refresh_final_usage", TestUsage401Refresh},
      {"changed_record_and_lock_contention", TestChangedAndLockContention},
      {"auth_suppression_process_memory_only", TestAuthSuppressionIsProcessMemoryOnly},
      {"commit_failure_recovery_and_malformed_refresh", TestCommitFailureAndMalformedRefresh},
      {"timeout_ledgers_component_bounds_and_diagnostic",
       TestTimeoutLedgersAndDiagnostic},
      {"unexpected_4xx_not_auth_locked", TestUnexpected4xxDoesNotAuthLock},
      {"schema_and_oversize_fail_closed", TestSchemaFailures},
      {"metric_isolation_and_nfkc_fable", TestMetricIsolationAndFableIdentity},
      {"quota_schema_drift_fail_closed", TestQuotaSchemaDriftFailClosed},
      {"unknown_raw_field_not_forwarded", TestUnknownRawFieldDoesNotCross},
      {"malformed_credential_no_network", TestMalformedCredential},
      {"optional_client_id_fixed_refresh_required_fields",
       TestOptionalClientIdPolicy},
  };
  unsigned passed = 0;
  for (const TestCase& test : tests) {
    if (!test.function()) {
      std::printf("{\"suite\":\"broker-synthetic-v1\",\"status\":\"FAIL\","
                  "\"case\":\"%s\",\"passed\":%u,\"total\":%zu}\n",
                  test.name, passed, sizeof(tests) / sizeof(tests[0]));
      return 1;
    }
    ++passed;
  }
  if (g_zeroizeFailure || g_zeroizeObservations == 0) {
    std::printf("{\"suite\":\"broker-synthetic-v1\",\"status\":\"FAIL\","
                "\"case\":\"zeroization_observer\",\"passed\":%u,"
                "\"total\":%zu}\n",
                passed, sizeof(tests) / sizeof(tests[0]) + 1);
    return 1;
  }
  std::printf("{\"suite\":\"broker-synthetic-v1\",\"status\":\"CANDIDATE_READY\","
              "\"passed\":%u,\"total\":%u,\"zeroizedBuffers\":%llu,"
              "\"sentinelLeakDetected\":false}\n",
              passed + 1, passed + 1,
              static_cast<unsigned long long>(g_zeroizeObservations));
  return 0;
}

}  // namespace

int wmain(int argumentCount, wchar_t** arguments) {
  SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX |
               SEM_NOOPENFILEERRORBOX);
  WerSetFlags(WER_FAULT_REPORTING_FLAG_NOHEAP);
  if (!broker::PrepareSecureMemoryWorkingSet()) return 65;
  if (argumentCount == 2 && std::wcscmp(arguments[1], L"--force-crash") == 0) {
    SecureBytes crashAccess;
    SecureBytes crashRefresh;
    if (!AppendSentinelAccess(crashAccess) ||
        !AppendSentinelRefresh(crashRefresh)) return 66;
    volatile unsigned char crashGuard = static_cast<unsigned char>(
        crashAccess.data()[crashAccess.size() - 1] ^
        crashRefresh.data()[crashRefresh.size() - 1]);
    MemoryBarrier();
    if (crashGuard == 0xff) return 67;
    RaiseFailFastException(nullptr, nullptr, 0);
    return 99;
  }
  if (argumentCount != 2 || std::wcscmp(arguments[1], L"--selftest") != 0)
    return 64;
  return RunSelfTests();
}
