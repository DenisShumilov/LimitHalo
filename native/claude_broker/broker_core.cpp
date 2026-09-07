#include "broker_core.h"

#include <werapi.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwchar>
#include <limits>
#include <new>

namespace broker {
namespace {

ZeroizeObserver g_zeroizeObserver = nullptr;

constexpr char kClientId[] = "9d1c250a-e61b-44d9-88ed-5944d1962f5e";

bool ConstantTimeEqual(const unsigned char* left, const unsigned char* right,
                       std::size_t size) noexcept {
  unsigned char difference = 0;
  for (std::size_t index = 0; index < size; ++index) {
    difference |= static_cast<unsigned char>(left[index] ^ right[index]);
  }
  return difference == 0;
}

bool IsValidUtf8(const unsigned char* value, std::size_t size) noexcept {
  if (size == 0) return true;
  if (size > static_cast<std::size_t>(std::numeric_limits<int>::max())) return false;
  return MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                             reinterpret_cast<const char*>(value),
                             static_cast<int>(size), nullptr, 0) > 0;
}

bool AppendUtf8Codepoint(std::uint32_t codepoint, SecureBytes& output) noexcept {
  unsigned char encoded[4] = {};
  std::size_t count = 0;
  if (codepoint <= 0x7f) {
    encoded[0] = static_cast<unsigned char>(codepoint);
    count = 1;
  } else if (codepoint <= 0x7ff) {
    encoded[0] = static_cast<unsigned char>(0xc0 | (codepoint >> 6));
    encoded[1] = static_cast<unsigned char>(0x80 | (codepoint & 0x3f));
    count = 2;
  } else if (codepoint <= 0xffff &&
             !(codepoint >= 0xd800 && codepoint <= 0xdfff)) {
    encoded[0] = static_cast<unsigned char>(0xe0 | (codepoint >> 12));
    encoded[1] = static_cast<unsigned char>(0x80 | ((codepoint >> 6) & 0x3f));
    encoded[2] = static_cast<unsigned char>(0x80 | (codepoint & 0x3f));
    count = 3;
  } else if (codepoint >= 0x10000 && codepoint <= 0x10ffff) {
    encoded[0] = static_cast<unsigned char>(0xf0 | (codepoint >> 18));
    encoded[1] = static_cast<unsigned char>(0x80 | ((codepoint >> 12) & 0x3f));
    encoded[2] = static_cast<unsigned char>(0x80 | ((codepoint >> 6) & 0x3f));
    encoded[3] = static_cast<unsigned char>(0x80 | (codepoint & 0x3f));
    count = 4;
  } else {
    return false;
  }
  const bool result = output.Append(encoded, count);
  SecureZeroMemory(encoded, sizeof(encoded));
  return result;
}

int HexValue(unsigned char value) noexcept {
  if (value >= '0' && value <= '9') return value - '0';
  if (value >= 'a' && value <= 'f') return value - 'a' + 10;
  if (value >= 'A' && value <= 'F') return value - 'A' + 10;
  return -1;
}

class JsonParser final {
 public:
  explicit JsonParser(const SecureBytes& input) noexcept
      : data_(input.data()), size_(input.size()) {}

  Status Parse(std::unique_ptr<JsonValue>& output) noexcept {
    SkipWhitespace();
    if (!ParseValue(output, 0)) return Status::SchemaMismatch;
    SkipWhitespace();
    return position_ == size_ ? Status::Ok : Status::SchemaMismatch;
  }

 private:
  bool ParseValue(std::unique_ptr<JsonValue>& output, unsigned depth) noexcept {
    if (depth > 20 || ++nodes_ > 1024 || position_ >= size_) return false;
    const std::size_t sourceStart = position_;
    std::unique_ptr<JsonValue> value(new (std::nothrow) JsonValue());
    if (!value) return false;
    const unsigned char current = data_[position_];
    if (current == '{') {
      value->type = JsonType::Object;
      if (!ParseObject(*value, depth + 1)) return false;
    } else if (current == '[') {
      value->type = JsonType::Array;
      if (!ParseArray(*value, depth + 1)) return false;
    } else if (current == '"') {
      value->type = JsonType::String;
      if (!ParseString(value->string)) return false;
    } else if (current == 't') {
      if (!ConsumeLiteral("true")) return false;
      value->type = JsonType::Boolean;
      value->boolean = true;
    } else if (current == 'f') {
      if (!ConsumeLiteral("false")) return false;
      value->type = JsonType::Boolean;
      value->boolean = false;
    } else if (current == 'n') {
      if (!ConsumeLiteral("null")) return false;
      value->type = JsonType::Null;
    } else {
      value->type = JsonType::Number;
      if (!ParseNumber(value->number)) return false;
    }
    value->sourceStart = sourceStart;
    value->sourceEnd = position_;
    output = std::move(value);
    return true;
  }

  bool ParseObject(JsonValue& output, unsigned depth) noexcept {
    ++position_;
    SkipWhitespace();
    if (position_ < size_ && data_[position_] == '}') {
      ++position_;
      return true;
    }
    while (position_ < size_) {
      SecureBytes key;
      if (!ParseString(key)) return false;
      for (const auto& existing : output.object) {
        if (key.Equals(existing.first)) return false;
      }
      SkipWhitespace();
      if (position_ >= size_ || data_[position_++] != ':') return false;
      SkipWhitespace();
      std::unique_ptr<JsonValue> child;
      if (!ParseValue(child, depth)) return false;
      output.object.emplace_back(std::move(key), std::move(child));
      SkipWhitespace();
      if (position_ >= size_) return false;
      if (data_[position_] == '}') {
        ++position_;
        return true;
      }
      if (data_[position_++] != ',') return false;
      SkipWhitespace();
    }
    return false;
  }

  bool ParseArray(JsonValue& output, unsigned depth) noexcept {
    ++position_;
    SkipWhitespace();
    if (position_ < size_ && data_[position_] == ']') {
      ++position_;
      return true;
    }
    while (position_ < size_) {
      std::unique_ptr<JsonValue> child;
      if (!ParseValue(child, depth)) return false;
      output.array.emplace_back(std::move(child));
      SkipWhitespace();
      if (position_ >= size_) return false;
      if (data_[position_] == ']') {
        ++position_;
        return true;
      }
      if (data_[position_++] != ',') return false;
      SkipWhitespace();
    }
    return false;
  }

  bool ParseString(SecureBytes& output) noexcept {
    if (position_ >= size_ || data_[position_++] != '"') return false;
    while (position_ < size_) {
      const unsigned char current = data_[position_++];
      if (current == '"') return IsValidUtf8(output.data(), output.size());
      if (current < 0x20) return false;
      if (current != '\\') {
        if (!output.AppendByte(current)) return false;
        continue;
      }
      if (position_ >= size_) return false;
      const unsigned char escaped = data_[position_++];
      switch (escaped) {
        case '"': case '\\': case '/':
          if (!output.AppendByte(escaped)) return false;
          break;
        case 'b': if (!output.AppendByte('\b')) return false; break;
        case 'f': if (!output.AppendByte('\f')) return false; break;
        case 'n': if (!output.AppendByte('\n')) return false; break;
        case 'r': if (!output.AppendByte('\r')) return false; break;
        case 't': if (!output.AppendByte('\t')) return false; break;
        case 'u': {
          std::uint32_t first = 0;
          if (!ParseHexQuad(first)) return false;
          std::uint32_t codepoint = first;
          if (first >= 0xd800 && first <= 0xdbff) {
            if (position_ + 2 > size_ || data_[position_] != '\\' ||
                data_[position_ + 1] != 'u') return false;
            position_ += 2;
            std::uint32_t second = 0;
            if (!ParseHexQuad(second) || second < 0xdc00 || second > 0xdfff)
              return false;
            codepoint = 0x10000 + ((first - 0xd800) << 10) + (second - 0xdc00);
          } else if (first >= 0xdc00 && first <= 0xdfff) {
            return false;
          }
          if (!AppendUtf8Codepoint(codepoint, output)) return false;
          break;
        }
        default:
          return false;
      }
      if (output.size() > kHttpBodyLimit) return false;
    }
    return false;
  }

  bool ParseHexQuad(std::uint32_t& output) noexcept {
    if (position_ + 4 > size_) return false;
    output = 0;
    for (int index = 0; index < 4; ++index) {
      const int value = HexValue(data_[position_++]);
      if (value < 0) return false;
      output = (output << 4) | static_cast<std::uint32_t>(value);
    }
    return true;
  }

  bool ParseNumber(double& output) noexcept {
    const std::size_t start = position_;
    bool negative = false;
    if (position_ < size_ && data_[position_] == '-') {
      negative = true;
      ++position_;
    }
    if (position_ >= size_) return false;
    double value = 0.0;
    if (data_[position_] == '0') {
      ++position_;
      if (position_ < size_ && data_[position_] >= '0' && data_[position_] <= '9')
        return false;
    } else {
      if (data_[position_] < '1' || data_[position_] > '9') return false;
      while (position_ < size_ && data_[position_] >= '0' &&
             data_[position_] <= '9') {
        value = value * 10.0 + static_cast<double>(data_[position_] - '0');
        if (!std::isfinite(value)) return false;
        ++position_;
      }
    }
    if (position_ < size_ && data_[position_] == '.') {
      ++position_;
      if (position_ >= size_ || data_[position_] < '0' || data_[position_] > '9')
        return false;
      double place = 0.1;
      while (position_ < size_ && data_[position_] >= '0' &&
             data_[position_] <= '9') {
        value += static_cast<double>(data_[position_] - '0') * place;
        place *= 0.1;
        ++position_;
      }
    }
    int exponent = 0;
    if (position_ < size_ && (data_[position_] == 'e' || data_[position_] == 'E')) {
      ++position_;
      bool exponentNegative = false;
      if (position_ < size_ && (data_[position_] == '+' || data_[position_] == '-')) {
        exponentNegative = data_[position_] == '-';
        ++position_;
      }
      if (position_ >= size_ || data_[position_] < '0' || data_[position_] > '9')
        return false;
      while (position_ < size_ && data_[position_] >= '0' &&
             data_[position_] <= '9') {
        if (exponent > 10000) return false;
        exponent = exponent * 10 + (data_[position_] - '0');
        ++position_;
      }
      if (exponentNegative) exponent = -exponent;
    }
    if (position_ == start) return false;
    if (exponent != 0) value *= std::pow(10.0, static_cast<double>(exponent));
    if (negative) value = -value;
    if (!std::isfinite(value)) return false;
    output = value;
    return true;
  }

  bool ConsumeLiteral(const char* literal) noexcept {
    const std::size_t length = std::strlen(literal);
    if (position_ + length > size_ ||
        std::memcmp(data_ + position_, literal, length) != 0) return false;
    position_ += length;
    return true;
  }

  void SkipWhitespace() noexcept {
    while (position_ < size_) {
      const unsigned char current = data_[position_];
      if (current != ' ' && current != '\t' && current != '\r' && current != '\n')
        break;
      ++position_;
    }
  }

  const unsigned char* data_;
  std::size_t size_;
  std::size_t position_ = 0;
  unsigned nodes_ = 0;
};

bool SerializeString(const SecureBytes& value, SecureBytes& output,
                     std::size_t limit) noexcept {
  if (!output.AppendByte('"')) return false;
  static constexpr char hex[] = "0123456789abcdef";
  for (std::size_t index = 0; index < value.size(); ++index) {
    const unsigned char current = value.data()[index];
    if (current == '"' || current == '\\') {
      if (!output.AppendByte('\\') || !output.AppendByte(current)) return false;
    } else if (current < 0x20) {
      unsigned char escaped[6] = {'\\', 'u', '0', '0',
                                  static_cast<unsigned char>(hex[current >> 4]),
                                  static_cast<unsigned char>(hex[current & 0x0f])};
      if (!output.Append(escaped, sizeof(escaped))) return false;
      SecureZeroMemory(escaped, sizeof(escaped));
    } else if (!output.AppendByte(current)) {
      return false;
    }
    if (output.size() > limit) return false;
  }
  return output.AppendByte('"') && output.size() <= limit;
}

bool SerializeValue(const JsonValue& value, SecureBytes& output,
                    std::size_t limit, unsigned depth) noexcept {
  if (depth > 20) return false;
  switch (value.type) {
    case JsonType::Null:
      return output.AppendLiteral("null") && output.size() <= limit;
    case JsonType::Boolean:
      return output.AppendLiteral(value.boolean ? "true" : "false") &&
             output.size() <= limit;
    case JsonType::Number: {
      char number[64] = {};
      const double integral = std::floor(value.number);
      const int count = (value.number == integral)
                            ? std::snprintf(number, sizeof(number), "%.0f", value.number)
                            : std::snprintf(number, sizeof(number), "%.17g", value.number);
      if (count <= 0 || static_cast<std::size_t>(count) >= sizeof(number)) return false;
      const bool result = output.Append(number, static_cast<std::size_t>(count)) &&
                          output.size() <= limit;
      SecureZeroMemory(number, sizeof(number));
      return result;
    }
    case JsonType::String:
      return SerializeString(value.string, output, limit);
    case JsonType::Array:
      if (!output.AppendByte('[')) return false;
      for (std::size_t index = 0; index < value.array.size(); ++index) {
        if (index != 0 && !output.AppendByte(',')) return false;
        if (!SerializeValue(*value.array[index], output, limit, depth + 1)) return false;
      }
      return output.AppendByte(']') && output.size() <= limit;
    case JsonType::Object:
      if (!output.AppendByte('{')) return false;
      for (std::size_t index = 0; index < value.object.size(); ++index) {
        if (index != 0 && !output.AppendByte(',')) return false;
        if (!SerializeString(value.object[index].first, output, limit) ||
            !output.AppendByte(':') ||
            !SerializeValue(*value.object[index].second, output, limit, depth + 1))
          return false;
      }
      return output.AppendByte('}') && output.size() <= limit;
  }
  return false;
}

bool IsIntegerNumber(const JsonValue* value, std::uint64_t& result) noexcept {
  if (!value || value->type != JsonType::Number || value->number < 0.0 ||
      value->number > 9007199254740991.0 || std::floor(value->number) != value->number)
    return false;
  result = static_cast<std::uint64_t>(value->number);
  return true;
}

struct CredentialFields final {
  JsonValue* access = nullptr;
  JsonValue* refresh = nullptr;
  JsonValue* expiry = nullptr;
  JsonValue* scopes = nullptr;
  JsonValue* clientId = nullptr;
  JsonValue* subscriptionType = nullptr;
  JsonValue* rateLimitTier = nullptr;
  std::uint64_t expiryMs = 0;
};

bool ParseCredentialDocument(JsonValue& root, CredentialFields& fields) noexcept {
  if (root.type != JsonType::Object) return false;
  JsonValue* oauth = FindMember(root, "claudeAiOauth");
  if (!oauth || oauth->type != JsonType::Object) return false;
  fields.access = FindMember(*oauth, "accessToken");
  fields.refresh = FindMember(*oauth, "refreshToken");
  fields.expiry = FindMember(*oauth, "expiresAt");
  fields.scopes = FindMember(*oauth, "scopes");
  fields.clientId = FindMember(*oauth, "clientId");
  fields.subscriptionType = FindMember(*oauth, "subscriptionType");
  fields.rateLimitTier = FindMember(*oauth, "rateLimitTier");
  if (!fields.access || fields.access->type != JsonType::String ||
      fields.access->string.empty() ||
      !fields.refresh || fields.refresh->type != JsonType::String ||
      fields.refresh->string.empty() ||
      (fields.clientId &&
       (fields.clientId->type != JsonType::String ||
        !fields.clientId->string.EqualsLiteral(kClientId))) ||
      !fields.subscriptionType || fields.subscriptionType->type != JsonType::String ||
      fields.subscriptionType->string.empty() ||
      !fields.rateLimitTier || fields.rateLimitTier->type != JsonType::String ||
      fields.rateLimitTier->string.empty() ||
      !fields.scopes || fields.scopes->type != JsonType::Array ||
      fields.scopes->array.empty() ||
      !IsIntegerNumber(fields.expiry, fields.expiryMs)) return false;
  if (fields.access->string.size() > 2048 || fields.refresh->string.size() > 2048 ||
      (fields.clientId && fields.clientId->string.size() > 128) ||
      fields.subscriptionType->string.size() > 128 ||
      fields.rateLimitTier->string.size() > 128 || fields.scopes->array.size() > 32)
    return false;
  for (const auto& scope : fields.scopes->array) {
    if (!scope || scope->type != JsonType::String || scope->string.empty() ||
        scope->string.size() > 128) return false;
  }
  return true;
}

bool CredentialFieldsEqual(const CredentialFields& left,
                           const CredentialFields& right) noexcept {
  return left.expiryMs == right.expiryMs &&
         left.access->string.Equals(right.access->string) &&
         left.refresh->string.Equals(right.refresh->string);
}

bool AppendJsonEscaped(const SecureBytes& value, SecureBytes& output,
                       std::size_t limit) noexcept {
  return SerializeString(value, output, limit);
}

bool BuildRefreshRequest(const CredentialFields& fields,
                         SecureBytes& output) noexcept {
  constexpr std::size_t kRefreshRequestLimit = 8192;
  if (!output.AppendLiteral("{\"grant_type\":\"refresh_token\",\"refresh_token\":"))
    return false;
  if (!AppendJsonEscaped(fields.refresh->string, output, kRefreshRequestLimit)) return false;
  if (!output.AppendLiteral(",\"client_id\":\"")) return false;
  if (!output.AppendLiteral(kClientId)) return false;
  if (!output.AppendLiteral("\",\"scope\":\"")) return false;
  for (std::size_t index = 0; index < fields.scopes->array.size(); ++index) {
    if (index != 0 && !output.AppendByte(' ')) return false;
    const SecureBytes& scope = fields.scopes->array[index]->string;
    for (std::size_t character = 0; character < scope.size(); ++character) {
      const unsigned char current = scope.data()[character];
      if (current == '"' || current == '\\') {
        if (!output.AppendByte('\\') || !output.AppendByte(current)) return false;
      } else if (current < 0x20) {
        return false;
      } else if (!output.AppendByte(current)) {
        return false;
      }
    }
  }
  return output.AppendLiteral("\"}") && output.size() <= kRefreshRequestLimit;
}

struct RefreshFields final {
  JsonValue* access = nullptr;
  JsonValue* refresh = nullptr;
  std::uint64_t expiresIn = 0;
};

bool ParseRefreshResponse(JsonValue& root, RefreshFields& fields) noexcept {
  if (root.type != JsonType::Object) return false;
  fields.access = FindMember(root, "access_token");
  fields.refresh = FindMember(root, "refresh_token");
  JsonValue* expires = FindMember(root, "expires_in");
  if (!fields.access || fields.access->type != JsonType::String ||
      fields.access->string.empty() || fields.access->string.size() > 2048 ||
      !IsIntegerNumber(expires, fields.expiresIn) ||
      fields.expiresIn == 0 || fields.expiresIn > 604800) return false;
  if (fields.refresh && (fields.refresh->type != JsonType::String ||
                         fields.refresh->string.empty() ||
                         fields.refresh->string.size() > 2048)) return false;
  return true;
}

bool IsInvalidGrant(const SecureBytes& body) noexcept {
  std::unique_ptr<JsonValue> root;
  if (ParseJson(body, root) != Status::Ok || !root || root->type != JsonType::Object)
    return false;
  const JsonValue* error = FindMember(*root, "error");
  return error && error->type == JsonType::String &&
         error->string.EqualsLiteral("invalid_grant");
}

int ParseTwo(const unsigned char* value) noexcept {
  if (value[0] < '0' || value[0] > '9' || value[1] < '0' || value[1] > '9')
    return -1;
  return (value[0] - '0') * 10 + (value[1] - '0');
}

int ParseFour(const unsigned char* value) noexcept {
  int result = 0;
  for (int index = 0; index < 4; ++index) {
    if (value[index] < '0' || value[index] > '9') return -1;
    result = result * 10 + (value[index] - '0');
  }
  return result;
}

bool IsLeapYear(int year) noexcept {
  return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
}

std::int64_t DaysFromCivil(int year, unsigned month, unsigned day) noexcept {
  year -= month <= 2;
  const int era = (year >= 0 ? year : year - 399) / 400;
  const unsigned yearOfEra = static_cast<unsigned>(year - era * 400);
  const unsigned dayOfYear = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 +
                             day - 1;
  const unsigned dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 +
                            dayOfYear;
  return static_cast<std::int64_t>(era) * 146097 +
         static_cast<std::int64_t>(dayOfEra) - 719468;
}

bool ParseRfc3339(const SecureBytes& value, std::int64_t& unixSeconds) noexcept {
  const unsigned char* input = value.data();
  const std::size_t length = value.size();
  if (length < 20 || input[4] != '-' || input[7] != '-' ||
      input[10] != 'T' || input[13] != ':' || input[16] != ':') return false;
  const int year = ParseFour(input);
  const int month = ParseTwo(input + 5);
  const int day = ParseTwo(input + 8);
  const int hour = ParseTwo(input + 11);
  const int minute = ParseTwo(input + 14);
  const int second = ParseTwo(input + 17);
  if (year < 1970 || year > 9999 || month < 1 || month > 12 ||
      hour < 0 || hour > 23 || minute < 0 || minute > 59 ||
      second < 0 || second > 59) return false;
  static constexpr int monthDays[] = {31, 28, 31, 30, 31, 30,
                                       31, 31, 30, 31, 30, 31};
  int maximumDay = monthDays[month - 1];
  if (month == 2 && IsLeapYear(year)) ++maximumDay;
  if (day < 1 || day > maximumDay) return false;
  std::size_t position = 19;
  if (position < length && input[position] == '.') {
    ++position;
    const std::size_t fractionStart = position;
    while (position < length && input[position] >= '0' && input[position] <= '9')
      ++position;
    if (position == fractionStart || position - fractionStart > 9) return false;
  }
  int offsetSeconds = 0;
  if (position < length && input[position] == 'Z') {
    ++position;
  } else if (position + 6 == length &&
             (input[position] == '+' || input[position] == '-') &&
             input[position + 3] == ':') {
    const int offsetHours = ParseTwo(input + position + 1);
    const int offsetMinutes = ParseTwo(input + position + 4);
    if (offsetHours < 0 || offsetHours > 23 ||
        offsetMinutes < 0 || offsetMinutes > 59) return false;
    offsetSeconds = (offsetHours * 60 + offsetMinutes) * 60;
    if (input[position] == '-') offsetSeconds = -offsetSeconds;
    position += 6;
  } else {
    return false;
  }
  if (position != length) return false;
  unixSeconds = DaysFromCivil(year, static_cast<unsigned>(month),
                              static_cast<unsigned>(day)) * 86400 +
                hour * 3600 + minute * 60 + second - offsetSeconds;
  return unixSeconds >= 0;
}

bool MetricFromObject(const JsonValue& object, const char* percentField,
                      std::uint64_t nowSeconds, std::uint64_t futureBound,
                      bool resetRequired, Metric& metric) noexcept {
  if (object.type != JsonType::Object) return false;
  const JsonValue* percent = FindMember(object, percentField);
  const JsonValue* reset = FindMember(object, "resets_at");
  if (!percent || percent->type != JsonType::Number ||
      !std::isfinite(percent->number) || percent->number < 0.0 ||
      percent->number > 100.0)
    return false;
  Metric candidate;
  candidate.available = true;
  candidate.remainingMicros = static_cast<std::uint64_t>(
      std::llround((100.0 - percent->number) * 1000000.0));
  if (!reset || reset->type == JsonType::Null) {
    if (resetRequired) return false;
    metric = candidate;
    return true;
  }
  if (reset->type != JsonType::String) return false;
  std::int64_t resetSeconds = 0;
  if (!ParseRfc3339(reset->string, resetSeconds) ||
      resetSeconds < static_cast<std::int64_t>(nowSeconds) ||
      resetSeconds > static_cast<std::int64_t>(nowSeconds + futureBound))
    return false;
  candidate.resetAvailable = true;
  candidate.resetsAt = resetSeconds;
  metric = candidate;
  return true;
}

bool StringMemberEquals(const JsonValue& object, const char* key,
                        const char* expected) noexcept {
  const JsonValue* value = FindMember(object, key);
  return value && value->type == JsonType::String &&
         value->string.EqualsLiteral(expected);
}

class SecureWide final {
 public:
  SecureWide() noexcept = default;
  ~SecureWide() { Release(); }
  SecureWide(const SecureWide&) = delete;
  SecureWide& operator=(const SecureWide&) = delete;
  bool Allocate(std::size_t characters) noexcept {
    Release();
    if (characters == 0 || characters > 4096) return false;
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
  std::size_t size() const noexcept { return characters_; }
 private:
  void Release() noexcept {
    if (!data_) return;
    const std::size_t bytes = characters_ * sizeof(wchar_t);
    SecureZeroMemory(data_, bytes);
    if (g_zeroizeObserver)
      g_zeroizeObserver(reinterpret_cast<const unsigned char*>(data_), bytes);
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

bool IsFableDisplayName(const SecureBytes& value) noexcept {
  if (value.empty() || value.size() > 256) return false;
  const int wideLength = MultiByteToWideChar(
      CP_UTF8, MB_ERR_INVALID_CHARS,
      reinterpret_cast<const char*>(value.data()), static_cast<int>(value.size()),
      nullptr, 0);
  if (wideLength <= 0) return false;
  const std::size_t rawCharacters = static_cast<std::size_t>(wideLength + 1);
  const std::size_t normalizedCapacity = value.size() * 4 + 1;
  SecureWide workspace;
  if (!workspace.Allocate(rawCharacters + normalizedCapacity)) return false;
  wchar_t* const wide = workspace.data();
  wchar_t* const normalized = workspace.data() + rawCharacters;
  if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                          reinterpret_cast<const char*>(value.data()),
                          static_cast<int>(value.size()), wide, wideLength) !=
      wideLength) return false;
  const int normalizedLength = NormalizeString(
      NormalizationKC, wide, wideLength, normalized,
      static_cast<int>(normalizedCapacity));
  if (normalizedLength <= 0) return false;
  static constexpr wchar_t fable[] = L"fable";
  static constexpr wchar_t fable5[] = L"fable 5";
  return (normalizedLength == 5 &&
          CompareStringOrdinal(normalized, normalizedLength,
                               fable, 5, TRUE) == CSTR_EQUAL) ||
         (normalizedLength == 7 &&
          CompareStringOrdinal(normalized, normalizedLength,
                               fable5, 7, TRUE) == CSTR_EQUAL);
}

Status NormalizeUsage(const SecureBytes& body, std::uint64_t nowSeconds,
                      Result& result) noexcept {
  if (body.size() > kHttpBodyLimit) return Status::SchemaMismatch;
  std::unique_ptr<JsonValue> root;
  if (ParseJson(body, root) != Status::Ok || !root || root->type != JsonType::Object)
    return Status::SchemaMismatch;

  bool recognizedShape = false;
  const JsonValue* fiveHour = FindMember(*root, "five_hour");
  Metric legacySession;
  if (fiveHour) {
    recognizedShape = true;
    if (fiveHour->type != JsonType::Null &&
        !MetricFromObject(*fiveHour, "utilization", nowSeconds, 21600,
                          true, legacySession))
      return Status::SchemaMismatch;
  }

  const JsonValue* sevenDay = FindMember(*root, "seven_day");
  if (sevenDay) recognizedShape = true;

  const JsonValue* limits = FindMember(*root, "limits");
  std::size_t sessionMatches = 0;
  Metric sessionCandidate;
  std::size_t weeklyMatches = 0;
  Metric weeklyCandidate;
  std::size_t fableMatches = 0;
  Metric fableCandidate;
  if (limits) {
    recognizedShape = true;
    if (limits->type != JsonType::Array) return Status::SchemaMismatch;
    for (const auto& rowPointer : limits->array) {
      if (!rowPointer || rowPointer->type != JsonType::Object)
        return Status::SchemaMismatch;
      const JsonValue& row = *rowPointer;
      const bool sessionKind = StringMemberEquals(row, "kind", "session");
      const bool sessionGroup = StringMemberEquals(row, "group", "session");
      if (sessionKind || sessionGroup) {
        if (!sessionKind || !sessionGroup) return Status::SchemaMismatch;
        ++sessionMatches;
        Metric candidate;
        if (!MetricFromObject(row, "percent", nowSeconds, 21600,
                              false, candidate))
          return Status::SchemaMismatch;
        const JsonValue* active = FindMember(row, "is_active");
        if (active && active->type != JsonType::Boolean)
          return Status::SchemaMismatch;
        sessionCandidate = candidate;
        continue;
      }

      const bool weeklyKind = StringMemberEquals(row, "kind", "weekly_all");
      const bool scopedKind = StringMemberEquals(row, "kind", "weekly_scoped");
      const bool weeklyGroup = StringMemberEquals(row, "group", "weekly");
      if ((weeklyKind || scopedKind) && !weeklyGroup)
        return Status::SchemaMismatch;
      if (!weeklyGroup) continue;
      if (weeklyKind) {
        ++weeklyMatches;
        Metric candidate;
        if (!MetricFromObject(row, "percent", nowSeconds, 691200,
                              true, candidate))
          return Status::SchemaMismatch;
        weeklyCandidate = candidate;
        continue;
      }
      if (!scopedKind) continue;
      const JsonValue* scope = FindMember(row, "scope");
      if (!scope || scope->type != JsonType::Object) continue;
      const JsonValue* model = FindMember(*scope, "model");
      if (!model || model->type != JsonType::Object) continue;
      const JsonValue* display = FindMember(*model, "display_name");
      if (!display || display->type != JsonType::String ||
          !IsFableDisplayName(display->string)) continue;
      ++fableMatches;
      Metric candidate;
      if (!MetricFromObject(row, "percent", nowSeconds, 691200,
                            true, candidate))
        return Status::SchemaMismatch;
      fableCandidate = candidate;
    }
  }

  if (sessionMatches > 1 || weeklyMatches > 1 || fableMatches > 1)
    return Status::SchemaMismatch;
  if (sessionMatches == 1) {
    if (legacySession.available) {
      if (legacySession.remainingMicros != sessionCandidate.remainingMicros ||
          (legacySession.resetAvailable && sessionCandidate.resetAvailable &&
           legacySession.resetsAt != sessionCandidate.resetsAt))
        return Status::SchemaMismatch;
      if (!sessionCandidate.resetAvailable && legacySession.resetAvailable) {
        sessionCandidate.resetAvailable = true;
        sessionCandidate.resetsAt = legacySession.resetsAt;
      }
    }
    result.claudeSession = sessionCandidate;
  } else if (legacySession.available) {
    result.claudeSession = legacySession;
  }
  if (weeklyMatches == 1 && weeklyCandidate.available) {
    result.claudeWeeklyAll = weeklyCandidate;
  } else if (weeklyMatches == 0 && sevenDay) {
    if (!MetricFromObject(*sevenDay, "utilization", nowSeconds, 691200,
                          true, result.claudeWeeklyAll))
      return Status::SchemaMismatch;
  }
  if (fableMatches == 1 && fableCandidate.available)
    result.claudeFableWeekly = fableCandidate;

  if (!result.claudeSession.available)
    return Status::SchemaMismatch;
  return recognizedShape ? Status::Ok : Status::SchemaMismatch;
}

const char* TransportEndpointName(TransportEndpoint endpoint) noexcept {
  switch (endpoint) {
    case TransportEndpoint::Refresh: return "refresh";
    case TransportEndpoint::Usage: return "usage";
    case TransportEndpoint::None: break;
  }
  return nullptr;
}

const char* TransportPhaseName(TransportPhase phase) noexcept {
  switch (phase) {
    case TransportPhase::SessionOptions: return "session_options";
    case TransportPhase::Connect: return "connect";
    case TransportPhase::Send: return "send";
    case TransportPhase::Receive: return "receive";
    case TransportPhase::Headers: return "headers";
    case TransportPhase::Body: return "body";
    case TransportPhase::None: break;
  }
  return nullptr;
}

const char* TransportClassName(TransportClass failureClass) noexcept {
  switch (failureClass) {
    case TransportClass::Timeout: return "timeout";
    case TransportClass::NameResolution: return "name_resolution";
    case TransportClass::CannotConnect: return "cannot_connect";
    case TransportClass::SecureFailure: return "secure_failure";
    case TransportClass::OptionFailure: return "option_failure";
    case TransportClass::Http429: return "http_429";
    case TransportClass::Http5xx: return "http_5xx";
    case TransportClass::Other: return "other";
    case TransportClass::None: break;
  }
  return nullptr;
}

void SetHttpDiagnostic(Result& result, TransportEndpoint endpoint,
                       TransportPhase phase,
                       TransportClass failureClass) noexcept {
  result.transportDiagnostic.endpoint = endpoint;
  result.transportDiagnostic.phase = phase;
  result.transportDiagnostic.failureClass = failureClass;
}

Status MapTransport(const HttpReply& reply, TransportEndpoint endpoint,
                    Result& result) noexcept {
  switch (reply.kind) {
    case TransportKind::Timeout: {
      const TransportDiagnostic diagnostic = reply.diagnostic.available()
          ? reply.diagnostic
          : TransportDiagnostic{endpoint, TransportPhase::Receive,
                                TransportClass::Timeout};
      result.transportDiagnostic = diagnostic;
      return Status::Transient;
    }
    case TransportKind::Failure: {
      const TransportDiagnostic diagnostic = reply.diagnostic.available()
          ? reply.diagnostic
          : TransportDiagnostic{endpoint, TransportPhase::Receive,
                                TransportClass::Other};
      result.transportDiagnostic = diagnostic;
      return Status::Transient;
    }
    case TransportKind::Oversized:
      return Status::SchemaMismatch;
    case TransportKind::Response:
      return Status::Ok;
  }
  return Status::Internal;
}

bool DeadlineExceeded(Adapter& adapter, std::uint64_t started) noexcept {
  const std::uint64_t now = adapter.MonotonicMilliseconds();
  return now < started || now - started > kWholeDeadlineMs;
}

}  // namespace

const char* StatusName(Status status) noexcept {
  switch (status) {
    case Status::Ok: return "OK";
    case Status::BadRequest: return "BAD_REQUEST";
    case Status::SecurityPolicy: return "SECURITY_POLICY";
    case Status::CredentialMissing: return "CREDENTIAL_MISSING";
    case Status::CredentialUnsupported: return "CREDENTIAL_UNSUPPORTED";
    case Status::CredentialMalformed: return "CREDENTIAL_MALFORMED";
    case Status::CredentialChanged: return "CREDENTIAL_CHANGED";
    case Status::LockContended: return "LOCK_CONTENDED";
    case Status::AuthLocked: return "AUTH_LOCKED";
    case Status::Transient: return "TRANSIENT";
    case Status::SchemaMismatch: return "SCHEMA_MISMATCH";
    case Status::Deadline: return "DEADLINE";
    case Status::Internal: return "INTERNAL";
  }
  return "INTERNAL";
}

SecureBytes::SecureBytes() noexcept
    : data_(nullptr), size_(0), capacity_(0), locked_(false), werExcluded_(false) {}

SecureBytes::SecureBytes(std::size_t capacity) noexcept : SecureBytes() {
  Reserve(capacity);
}

SecureBytes::~SecureBytes() { Release(); }

SecureBytes::SecureBytes(SecureBytes&& other) noexcept
    : data_(other.data_), size_(other.size_), capacity_(other.capacity_),
      locked_(other.locked_) {
  werExcluded_ = other.werExcluded_;
  other.data_ = nullptr;
  other.size_ = 0;
  other.capacity_ = 0;
  other.locked_ = false;
  other.werExcluded_ = false;
}

SecureBytes& SecureBytes::operator=(SecureBytes&& other) noexcept {
  if (this != &other) {
    Release();
    data_ = other.data_;
    size_ = other.size_;
    capacity_ = other.capacity_;
    locked_ = other.locked_;
    werExcluded_ = other.werExcluded_;
    other.data_ = nullptr;
    other.size_ = 0;
    other.capacity_ = 0;
    other.locked_ = false;
    other.werExcluded_ = false;
  }
  return *this;
}

bool SecureBytes::Reserve(std::size_t capacity) noexcept {
  if (capacity <= capacity_) return true;
  if (capacity == 0 || capacity > kHttpBodyLimit + 1) return false;
  std::size_t nextCapacity = capacity_ == 0 ? 256 : capacity_;
  while (nextCapacity < capacity) {
    if (nextCapacity > (kHttpBodyLimit + 1) / 2) {
      nextCapacity = kHttpBodyLimit + 1;
      break;
    }
    nextCapacity *= 2;
  }
  unsigned char* next = static_cast<unsigned char*>(VirtualAlloc(
      nullptr, nextCapacity, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE));
  if (!next) return false;
  if (WerRegisterExcludedMemoryBlock(next, static_cast<DWORD>(nextCapacity)) != S_OK) {
    SecureZeroMemory(next, nextCapacity);
    VirtualFree(next, 0, MEM_RELEASE);
    return false;
  }
  bool nextLocked = VirtualLock(next, nextCapacity) != FALSE;
    if (!nextLocked) {
    SecureZeroMemory(next, nextCapacity);
    WerUnregisterExcludedMemoryBlock(next);
    VirtualFree(next, 0, MEM_RELEASE);
    return false;
  }
  const std::size_t originalSize = size_;
  if (data_ && originalSize != 0) std::memcpy(next, data_, originalSize);
  Release();
  data_ = next;
  size_ = originalSize;
  capacity_ = nextCapacity;
  locked_ = nextLocked;
  werExcluded_ = true;
  return true;
}

bool SecureBytes::Resize(std::size_t size) noexcept {
  if (!Reserve(size == 0 ? 1 : size)) return false;
  if (size < size_) SecureZeroMemory(data_ + size, size_ - size);
  if (size > size_) SecureZeroMemory(data_ + size_, size - size_);
  size_ = size;
  return true;
}

bool SecureBytes::Assign(const void* data, std::size_t size) noexcept {
  Clear();
  if (size == 0) return true;
  if (!data || !Resize(size)) return false;
  std::memcpy(data_, data, size);
  return true;
}

bool SecureBytes::AssignLiteral(const char* value) noexcept {
  return value && Assign(value, std::strlen(value));
}

bool SecureBytes::Append(const void* data, std::size_t size) noexcept {
  if (size == 0) return true;
  if (!data || size_ > kHttpBodyLimit + 1 - size || !Reserve(size_ + size))
    return false;
  std::memcpy(data_ + size_, data, size);
  size_ += size;
  return true;
}

bool SecureBytes::AppendLiteral(const char* value) noexcept {
  return value && Append(value, std::strlen(value));
}

bool SecureBytes::AppendByte(unsigned char value) noexcept {
  return Append(&value, 1);
}

void SecureBytes::Clear() noexcept {
  if (data_ && capacity_ != 0) SecureZeroMemory(data_, capacity_);
  size_ = 0;
}

bool SecureBytes::Equals(const SecureBytes& other) const noexcept {
  return size_ == other.size_ &&
         (size_ == 0 || ConstantTimeEqual(data_, other.data_, size_));
}

bool SecureBytes::EqualsLiteral(const char* value) const noexcept {
  if (!value) return false;
  const std::size_t length = std::strlen(value);
  return length == size_ &&
         (length == 0 || ConstantTimeEqual(data_,
              reinterpret_cast<const unsigned char*>(value), length));
}

void SecureBytes::Release() noexcept {
  if (!data_) return;
  SecureZeroMemory(data_, capacity_);
  if (g_zeroizeObserver) g_zeroizeObserver(data_, capacity_);
  if (werExcluded_) WerUnregisterExcludedMemoryBlock(data_);
  if (locked_) VirtualUnlock(data_, capacity_);
  VirtualFree(data_, 0, MEM_RELEASE);
  data_ = nullptr;
  size_ = 0;
  capacity_ = 0;
  locked_ = false;
  werExcluded_ = false;
}

void SetZeroizeObserverForSyntheticHost(ZeroizeObserver observer) noexcept {
  g_zeroizeObserver = observer;
}

bool PrepareSecureMemoryWorkingSet() noexcept {
  SIZE_T minimum = 0;
  SIZE_T maximum = 0;
  DWORD flags = 0;
  HANDLE process = GetCurrentProcess();
  if (!GetProcessWorkingSetSizeEx(process, &minimum, &maximum, &flags)) return false;
  constexpr SIZE_T desiredMinimum = 8ULL * 1024ULL * 1024ULL;
  constexpr SIZE_T desiredMaximum = 32ULL * 1024ULL * 1024ULL;
  if (minimum >= desiredMinimum && maximum >= desiredMaximum) return true;
  minimum = (std::max)(minimum, desiredMinimum);
  maximum = (std::max)(maximum, (std::max)(desiredMaximum, minimum));
  return SetProcessWorkingSetSizeEx(process, minimum, maximum,
                                    QUOTA_LIMITS_HARDWS_MIN_ENABLE) != FALSE;
}

Status ParseJson(const SecureBytes& input,
                 std::unique_ptr<JsonValue>& output) noexcept {
  JsonParser parser(input);
  return parser.Parse(output);
}

JsonValue* FindMember(JsonValue& object, const char* key) noexcept {
  if (object.type != JsonType::Object) return nullptr;
  for (auto& member : object.object) {
    if (member.first.EqualsLiteral(key)) return member.second.get();
  }
  return nullptr;
}

const JsonValue* FindMember(const JsonValue& object, const char* key) noexcept {
  if (object.type != JsonType::Object) return nullptr;
  for (const auto& member : object.object) {
    if (member.first.EqualsLiteral(key)) return member.second.get();
  }
  return nullptr;
}

bool SerializeJson(const JsonValue& value, SecureBytes& output,
                   std::size_t limit) noexcept {
  output.Clear();
  return SerializeValue(value, output, limit, 0);
}

Status ProjectClaudeCredential(const SecureBytes& fullDocument,
                               SecureBytes& projectedDocument) noexcept {
  projectedDocument.Clear();
  if (fullDocument.empty() || fullDocument.size() > kCredentialFileLimit)
    return Status::CredentialUnsupported;
  std::unique_ptr<JsonValue> root;
  if (ParseJson(fullDocument, root) != Status::Ok || !root ||
      root->type != JsonType::Object)
    return Status::CredentialMalformed;
  const JsonValue* oauth = FindMember(*root, "claudeAiOauth");
  if (!oauth || oauth->type != JsonType::Object ||
      oauth->sourceEnd <= oauth->sourceStart ||
      oauth->sourceEnd > fullDocument.size())
    return Status::CredentialMalformed;
  if (!projectedDocument.AppendLiteral("{\"claudeAiOauth\":") ||
      !projectedDocument.Append(fullDocument.data() + oauth->sourceStart,
                                oauth->sourceEnd - oauth->sourceStart) ||
      !projectedDocument.AppendByte('}'))
    return Status::Internal;
  if (projectedDocument.size() > kCredentialProjectionLimit) {
    projectedDocument.Clear();
    return Status::CredentialUnsupported;
  }
  return Status::Ok;
}

Status PatchClaudeCredential(const SecureBytes& currentFullDocument,
                             const SecureBytes& replacementProjection,
                             SecureBytes& nextFullDocument) noexcept {
  nextFullDocument.Clear();
  if (currentFullDocument.empty() ||
      currentFullDocument.size() > kCredentialFileLimit ||
      replacementProjection.empty() ||
      replacementProjection.size() > kCredentialProjectionLimit)
    return Status::CredentialUnsupported;

  std::unique_ptr<JsonValue> currentRoot;
  std::unique_ptr<JsonValue> replacementRoot;
  if (ParseJson(currentFullDocument, currentRoot) != Status::Ok || !currentRoot ||
      currentRoot->type != JsonType::Object ||
      ParseJson(replacementProjection, replacementRoot) != Status::Ok ||
      !replacementRoot || replacementRoot->type != JsonType::Object ||
      replacementRoot->object.size() != 1)
    return Status::CredentialMalformed;
  const JsonValue* currentOauth = FindMember(*currentRoot, "claudeAiOauth");
  const JsonValue* replacementOauth = FindMember(*replacementRoot, "claudeAiOauth");
  if (!currentOauth || currentOauth->type != JsonType::Object ||
      !replacementOauth || replacementOauth->type != JsonType::Object ||
      currentOauth->sourceEnd <= currentOauth->sourceStart ||
      currentOauth->sourceEnd > currentFullDocument.size() ||
      replacementOauth->sourceEnd <= replacementOauth->sourceStart ||
      replacementOauth->sourceEnd > replacementProjection.size())
    return Status::CredentialMalformed;

  const std::size_t nextSize = currentOauth->sourceStart +
      (replacementOauth->sourceEnd - replacementOauth->sourceStart) +
      (currentFullDocument.size() - currentOauth->sourceEnd);
  if (nextSize > kCredentialFileLimit) return Status::CredentialUnsupported;
  if (!nextFullDocument.Append(currentFullDocument.data(),
                               currentOauth->sourceStart) ||
      !nextFullDocument.Append(
          replacementProjection.data() + replacementOauth->sourceStart,
          replacementOauth->sourceEnd - replacementOauth->sourceStart) ||
      !nextFullDocument.Append(
          currentFullDocument.data() + currentOauth->sourceEnd,
          currentFullDocument.size() - currentOauth->sourceEnd)) {
    nextFullDocument.Clear();
    return Status::Internal;
  }
  std::unique_ptr<JsonValue> verification;
  if (ParseJson(nextFullDocument, verification) != Status::Ok || !verification ||
      verification->type != JsonType::Object) {
    nextFullDocument.Clear();
    return Status::CredentialMalformed;
  }
  return Status::Ok;
}

Status ParseProtocolRequest(const SecureBytes& input,
                            ProtocolRequest& request) noexcept {
  if (input.empty() || input.size() > 512) return Status::BadRequest;
  std::unique_ptr<JsonValue> root;
  if (ParseJson(input, root) != Status::Ok || !root ||
      root->type != JsonType::Object || root->object.size() != 3)
    return Status::BadRequest;
  const JsonValue* protocol = FindMember(*root, "protocol");
  const JsonValue* requestId = FindMember(*root, "requestId");
  const JsonValue* sequence = FindMember(*root, "acquisitionSequence");
  if (!protocol || protocol->type != JsonType::String ||
      !protocol->string.EqualsLiteral("ai-usage-claude-broker/1") ||
      !requestId || requestId->type != JsonType::String ||
      requestId->string.size() != 32 ||
      !sequence || sequence->type != JsonType::Number ||
      !std::isfinite(sequence->number) || sequence->number < 1.0 ||
      sequence->number > 9007199254740991.0 ||
      std::floor(sequence->number) != sequence->number)
    return Status::BadRequest;
  for (std::size_t index = 0; index < 32; ++index) {
    const unsigned char value = requestId->string.data()[index];
    if (!((value >= '0' && value <= '9') || (value >= 'a' && value <= 'f')))
      return Status::BadRequest;
    request.requestId[index] = static_cast<char>(value);
  }
  request.requestId[32] = '\0';
  request.acquisitionSequence = static_cast<std::uint64_t>(sequence->number);
  return Status::Ok;
}

Result Execute(Adapter& adapter, std::uint64_t) noexcept {
  Result result;
  const std::uint64_t started = adapter.MonotonicMilliseconds();
  std::uint64_t usageElapsedMs = 0;
  std::uint64_t refreshElapsedMs = 0;

  auto requestUsage = [&](const SecureBytes& accessToken,
                          HttpReply& reply) noexcept -> Status {
    if (usageElapsedMs >= kUsageTimeoutMs) return Status::Deadline;
    const DWORD remaining = static_cast<DWORD>(kUsageTimeoutMs - usageElapsedMs);
    const std::uint64_t before = adapter.MonotonicMilliseconds();
    const Status requestStatus = adapter.UsageRequest(accessToken, remaining, reply);
    const std::uint64_t after = adapter.MonotonicMilliseconds();
    if (after < before || after - before > remaining) return Status::Deadline;
    usageElapsedMs += after - before;
    return requestStatus;
  };

  auto requestRefresh = [&](const SecureBytes& requestBody,
                            HttpReply& reply) noexcept -> Status {
    if (refreshElapsedMs >= kRefreshTimeoutMs) return Status::Deadline;
    const DWORD remaining = static_cast<DWORD>(kRefreshTimeoutMs - refreshElapsedMs);
    const std::uint64_t before = adapter.MonotonicMilliseconds();
    const Status requestStatus = adapter.RefreshRequest(requestBody, remaining, reply);
    const std::uint64_t after = adapter.MonotonicMilliseconds();
    if (after < before || after - before > remaining) return Status::Deadline;
    refreshElapsedMs += after - before;
    return requestStatus;
  };

  CredentialRecord record;
  Status status = adapter.ReadCredential(record);
  if (status != Status::Ok) {
    result.status = status;
    return result;
  }
  result.hasCredentialVersion = true;
  result.credentialVersion = record.version;
  if (record.blob.empty() ||
      record.blob.size() > kCredentialProjectionLimit ||
      record.storageDocument.empty() ||
      record.storageDocument.size() > kCredentialFileLimit ||
      record.version.blobSize != record.storageDocument.size()) {
    result.status = Status::CredentialUnsupported;
    return result;
  }

  std::unique_ptr<JsonValue> credentialRoot;
  if (ParseJson(record.blob, credentialRoot) != Status::Ok || !credentialRoot) {
    result.status = Status::CredentialMalformed;
    return result;
  }
  CredentialFields credentials;
  if (!ParseCredentialDocument(*credentialRoot, credentials)) {
    result.status = Status::CredentialMalformed;
    return result;
  }

  // Authentication suppression is intentionally confined to this broker
  // process.  A broker invocation handles one acquisition and never writes a
  // durable lock or consults a previous process's state.
  bool authSuppressed = false;
  auto suppressAuth = [&]() noexcept -> Status {
    authSuppressed = true;
    return Status::AuthLocked;
  };
  bool refreshed = false;
  auto performRefresh = [&]() noexcept -> Status {
    if (authSuppressed || refreshed) return suppressAuth();
    refreshed = true;
    Status lockStatus = adapter.AcquireCredentialWriteLock();
    if (lockStatus != Status::Ok) return lockStatus;
    bool lockHeld = true;
    auto finishLocked = [&](Status value) noexcept -> Status {
      if (lockHeld) {
        adapter.ReleaseCredentialWriteLock();
        lockHeld = false;
      }
      return value;
    };
    CredentialRecord current;
    lockStatus = adapter.ReadCredential(current);
    if (lockStatus != Status::Ok) return finishLocked(lockStatus);
    std::unique_ptr<JsonValue> currentRoot;
    CredentialFields currentFields;
    const bool unchanged = current.version == record.version &&
        current.blob.size() == record.blob.size() &&
        current.blob.Equals(record.blob) &&
        current.storageDocument.size() == record.storageDocument.size() &&
        current.storageDocument.Equals(record.storageDocument) &&
        ParseJson(current.blob, currentRoot) == Status::Ok && currentRoot &&
        ParseCredentialDocument(*currentRoot, currentFields) &&
        CredentialFieldsEqual(credentials, currentFields);
    if (!unchanged) return finishLocked(Status::CredentialChanged);

    SecureBytes refreshRequest;
    if (!BuildRefreshRequest(currentFields, refreshRequest))
      return finishLocked(Status::Internal);
    HttpReply refreshReply;
    Status requestStatus = requestRefresh(refreshRequest, refreshReply);
    if (requestStatus != Status::Ok) return finishLocked(requestStatus);
    requestStatus = MapTransport(refreshReply, TransportEndpoint::Refresh, result);
    if (requestStatus != Status::Ok) return finishLocked(requestStatus);
    if (refreshReply.statusCode == 400 && IsInvalidGrant(refreshReply.body)) {
      const Status auth = suppressAuth();
      return finishLocked(auth);
    }
    if (refreshReply.statusCode == 429) {
      SetHttpDiagnostic(result, TransportEndpoint::Refresh,
                        TransportPhase::Headers, TransportClass::Http429);
      return finishLocked(Status::Transient);
    }
    if (refreshReply.statusCode >= 500) {
      SetHttpDiagnostic(result, TransportEndpoint::Refresh,
                        TransportPhase::Headers, TransportClass::Http5xx);
      return finishLocked(Status::Transient);
    }
    if (refreshReply.statusCode >= 400 && refreshReply.statusCode < 500)
      return finishLocked(Status::SchemaMismatch);
    if (refreshReply.statusCode != 200) {
      SetHttpDiagnostic(result, TransportEndpoint::Refresh,
                        TransportPhase::Headers, TransportClass::Other);
      return finishLocked(Status::Transient);
    }

    std::unique_ptr<JsonValue> refreshRoot;
    if (ParseJson(refreshReply.body, refreshRoot) != Status::Ok || !refreshRoot)
      return finishLocked(Status::SchemaMismatch);
    RefreshFields refreshedFields;
    if (!ParseRefreshResponse(*refreshRoot, refreshedFields))
      return finishLocked(Status::SchemaMismatch);

    if (!currentFields.access->string.Assign(refreshedFields.access->string.data(),
                                             refreshedFields.access->string.size()))
      return finishLocked(Status::Internal);
    if (refreshedFields.refresh &&
        !currentFields.refresh->string.Assign(
            refreshedFields.refresh->string.data(),
            refreshedFields.refresh->string.size()))
      return finishLocked(Status::Internal);
    currentFields.expiryMs = adapter.NowUnixMilliseconds() +
                             refreshedFields.expiresIn * 1000;
    currentFields.expiry->number = static_cast<double>(currentFields.expiryMs);
    SecureBytes replacement;
    if (!SerializeJson(*currentRoot, replacement, kCredentialProjectionLimit))
      return finishLocked(Status::CredentialUnsupported);
    lockStatus = adapter.WriteCredential(current, replacement);
    finishLocked(Status::Ok);
    if (lockStatus != Status::Ok) return lockStatus;

    CredentialRecord updated;
    lockStatus = adapter.ReadCredential(updated);
    if (lockStatus != Status::Ok) return lockStatus;
    std::unique_ptr<JsonValue> updatedRoot;
    CredentialFields updatedFields;
    if (ParseJson(updated.blob, updatedRoot) != Status::Ok || !updatedRoot ||
        !ParseCredentialDocument(*updatedRoot, updatedFields))
      return Status::CredentialMalformed;
    credentialRoot = std::move(updatedRoot);
    credentials = updatedFields;
    result.credentialVersion = updated.version;
    return Status::Ok;
  };

  if (DeadlineExceeded(adapter, started)) {
    result.status = Status::Deadline;
    return result;
  }
  if (credentials.expiryMs <= adapter.NowUnixMilliseconds() + kExpirySkewMs) {
    status = performRefresh();
    if (status != Status::Ok) {
      result.status = status;
      return result;
    }
  }

  HttpReply usageReply;
  status = requestUsage(credentials.access->string, usageReply);
  if (status != Status::Ok) {
    result.status = status;
    return result;
  }
  status = MapTransport(usageReply, TransportEndpoint::Usage, result);
  if (status != Status::Ok) {
    result.status = status;
    return result;
  }
  if (DeadlineExceeded(adapter, started)) {
    result.status = Status::Deadline;
    return result;
  }

  if (usageReply.statusCode == 401 && !refreshed) {
    status = performRefresh();
    if (status != Status::Ok) {
      result.status = status;
      return result;
    }
    HttpReply finalReply;
    status = requestUsage(credentials.access->string, finalReply);
    if (status != Status::Ok) {
      result.status = status;
      return result;
    }
    status = MapTransport(finalReply, TransportEndpoint::Usage, result);
    if (status != Status::Ok) {
      result.status = status;
      return result;
    }
    if (DeadlineExceeded(adapter, started)) {
      result.status = Status::Deadline;
      return result;
    }
    if (finalReply.statusCode == 401) {
      result.status = suppressAuth();
      return result;
    }
    if (finalReply.statusCode >= 400 && finalReply.statusCode < 500 &&
        finalReply.statusCode != 429) {
      result.status = Status::SchemaMismatch;
      return result;
    }
    if (finalReply.statusCode == 429) {
      SetHttpDiagnostic(result, TransportEndpoint::Usage,
                        TransportPhase::Headers, TransportClass::Http429);
      result.status = Status::Transient;
      return result;
    }
    if (finalReply.statusCode >= 500) {
      SetHttpDiagnostic(result, TransportEndpoint::Usage,
                        TransportPhase::Headers, TransportClass::Http5xx);
      result.status = Status::Transient;
      return result;
    }
    if (finalReply.statusCode != 200) {
      SetHttpDiagnostic(result, TransportEndpoint::Usage,
                        TransportPhase::Headers, TransportClass::Other);
      result.status = Status::Transient;
      return result;
    }
    status = NormalizeUsage(finalReply.body,
                            adapter.NowUnixMilliseconds() / 1000, result);
    result.status = status;
    return result;
  }

  if (usageReply.statusCode == 401) {
    result.status = suppressAuth();
    return result;
  }
  if (usageReply.statusCode >= 400 && usageReply.statusCode < 500 &&
      usageReply.statusCode != 429) {
    result.status = Status::SchemaMismatch;
    return result;
  }
  if (usageReply.statusCode == 429) {
    SetHttpDiagnostic(result, TransportEndpoint::Usage,
                      TransportPhase::Headers, TransportClass::Http429);
    result.status = Status::Transient;
    return result;
  }
  if (usageReply.statusCode >= 500) {
    SetHttpDiagnostic(result, TransportEndpoint::Usage,
                      TransportPhase::Headers, TransportClass::Http5xx);
    result.status = Status::Transient;
    return result;
  }
  if (usageReply.statusCode != 200) {
    SetHttpDiagnostic(result, TransportEndpoint::Usage,
                      TransportPhase::Headers, TransportClass::Other);
    result.status = Status::Transient;
    return result;
  }
  status = NormalizeUsage(usageReply.body,
                          adapter.NowUnixMilliseconds() / 1000, result);
  result.status = status;
  return result;
}

bool FormatResult(const Result& result, const ProtocolRequest& request,
                  std::int64_t acquiredAtUnixSeconds,
                  std::uint64_t durationMs,
                  std::string& output) noexcept {
  output.clear();
  const bool hasDiagnostic = result.transportDiagnostic.available();
  if ((result.status == Status::Transient) != hasDiagnostic) return false;

  char transportDiagnostic[256] = {};
  int diagnosticLength = 0;
  if (hasDiagnostic) {
    const char* endpoint = TransportEndpointName(
        result.transportDiagnostic.endpoint);
    const char* phase = TransportPhaseName(result.transportDiagnostic.phase);
    const char* failureClass = TransportClassName(
        result.transportDiagnostic.failureClass);
    if (!endpoint || !phase || !failureClass) return false;
    diagnosticLength = std::snprintf(
        transportDiagnostic, sizeof(transportDiagnostic),
        "{\"endpoint\":\"%s\",\"phase\":\"%s\",\"class\":\"%s\"}",
        endpoint, phase, failureClass);
  } else {
    diagnosticLength = std::snprintf(transportDiagnostic,
                                     sizeof(transportDiagnostic), "null");
  }
  if (diagnosticLength <= 0 ||
      static_cast<std::size_t>(diagnosticLength) >= sizeof(transportDiagnostic))
    return false;

  char credentialVersion[192] = {};
  int credentialLength = 0;
  if (result.hasCredentialVersion) {
    const std::uint64_t fileTime =
        (static_cast<std::uint64_t>(result.credentialVersion.lastWrittenHigh) << 32) |
        result.credentialVersion.lastWrittenLow;
    credentialLength = std::snprintf(
        credentialVersion, sizeof(credentialVersion),
        "{\"credentialLastWrittenFileTime\":\"%llu\",\"credentialBlobSize\":%lu}",
        static_cast<unsigned long long>(fileTime),
        static_cast<unsigned long>(result.credentialVersion.blobSize));
  } else {
    credentialLength = std::snprintf(credentialVersion, sizeof(credentialVersion), "null");
  }
  if (credentialLength <= 0 ||
      static_cast<std::size_t>(credentialLength) >= sizeof(credentialVersion))
    return false;

  char prefix[768] = {};
  const int prefixLength = std::snprintf(
      prefix, sizeof(prefix),
      "{\"protocol\":\"ai-usage-claude-broker/1\",\"requestId\":\"%s\","
      "\"acquisitionSequence\":%llu,\"status\":\"%s\","
      "\"acquiredAtUnixSeconds\":%lld,\"durationMs\":%llu,"
      "\"credentialVersion\":%s,\"transportDiagnostic\":%s,\"metrics\":{",
      request.requestId,
      static_cast<unsigned long long>(request.acquisitionSequence),
      StatusName(result.status), static_cast<long long>(acquiredAtUnixSeconds),
      static_cast<unsigned long long>(durationMs), credentialVersion,
      transportDiagnostic);
  if (prefixLength <= 0 || static_cast<std::size_t>(prefixLength) >= sizeof(prefix))
    return false;
  output.append(prefix, static_cast<std::size_t>(prefixLength));
  SecureZeroMemory(prefix, sizeof(prefix));
  SecureZeroMemory(credentialVersion, sizeof(credentialVersion));
  SecureZeroMemory(transportDiagnostic, sizeof(transportDiagnostic));

  auto appendMetric = [&](const char* name, const Metric& metric,
                          bool first) noexcept -> bool {
    if (!first) output.push_back(',');
    output.push_back('"');
    output.append(name);
    output.append("\":");
    if (result.status != Status::Ok || !metric.available) {
      output.append("null");
      return output.size() <= kOutputLimit;
    }
    char number[96] = {};
    int count = 0;
    if (metric.resetAvailable) {
      count = std::snprintf(
          number, sizeof(number),
          "{\"remainingMicros\":%llu,\"resetUnixSeconds\":%lld}",
          static_cast<unsigned long long>(metric.remainingMicros),
          static_cast<long long>(metric.resetsAt));
    } else {
      count = std::snprintf(
          number, sizeof(number),
          "{\"remainingMicros\":%llu,\"resetUnixSeconds\":null}",
          static_cast<unsigned long long>(metric.remainingMicros));
    }
    if (count <= 0 || static_cast<std::size_t>(count) >= sizeof(number)) return false;
    output.append(number, static_cast<std::size_t>(count));
    SecureZeroMemory(number, sizeof(number));
    return output.size() <= kOutputLimit;
  };

  if (!appendMetric("claude_session", result.claudeSession, true) ||
      !appendMetric("claude_weekly_all", result.claudeWeeklyAll, false) ||
      !appendMetric("claude_fable_weekly", result.claudeFableWeekly, false))
    return false;
  output.append("}}\n");
  return output.size() <= kOutputLimit;
}

}  // namespace broker
