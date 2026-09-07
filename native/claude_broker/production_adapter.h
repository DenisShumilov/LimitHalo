#pragma once

#include "broker_core.h"

namespace broker {

class ProductionAdapter final : public Adapter {
 public:
  ProductionAdapter() noexcept;
  ~ProductionAdapter() override;

  Status InitializeSecurityPolicy() noexcept;
  Status ReadCredential(CredentialRecord& record) noexcept override;
  Status AcquireCredentialWriteLock() noexcept override;
  void ReleaseCredentialWriteLock() noexcept override;
  Status WriteCredential(const CredentialRecord& expected,
                         const SecureBytes& replacement) noexcept override;
  Status UsageRequest(const SecureBytes& accessToken, DWORD timeoutMs,
                      HttpReply& reply) noexcept override;
  Status RefreshRequest(const SecureBytes& requestBody, DWORD timeoutMs,
                        HttpReply& reply) noexcept override;
  std::uint64_t NowUnixMilliseconds() const noexcept override;
  std::uint64_t MonotonicMilliseconds() const noexcept override;

 private:
  Status ResolveFixedPaths() noexcept;
  Status SendFixedRequest(bool usage, const SecureBytes& secret,
                          DWORD timeoutMs, HttpReply& reply) noexcept;
  static DWORD WINAPI CredentialLockHeartbeatEntry(void* context) noexcept;
  void CredentialLockHeartbeatLoop() noexcept;
  bool CredentialLockHealthy() const noexcept;

  std::wstring credentialDirectory_;
  std::wstring credentialPath_;
  std::wstring credentialLockPath_;
  HANDLE credentialLockDirectoryHandle_ = INVALID_HANDLE_VALUE;
  HANDLE credentialLockStopEvent_ = nullptr;
  HANDLE credentialLockHeartbeatThread_ = nullptr;
  volatile LONG credentialLockHeartbeatFailed_ = 0;
  bool credentialLockHeld_ = false;
  bool initialized_ = false;
};

}  // namespace broker
