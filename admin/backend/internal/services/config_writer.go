package services

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
)

// writableEnvFiles 是后台允许写入的文件，**顺序即优先级**（后者覆盖前者）。
//
// 与 pydantic-settings 的 env_file=(".env", ".env.dev") 一致。
// 本机 .env.dev 重复声明了大量 DL_SENPAI_* 键，因此只改 .env 会被
// .env.dev 的旧值盖回——写入时必须两个文件一起处理（见 SetFlag）。
var writableEnvFiles = []string{".env", ".env.dev"}

// envKeyPattern 限定键名形状，避免把任意字符串当键写进文件。
var envKeyPattern = regexp.MustCompile("^[A-Z][A-Z0-9_]*$")

// sensitiveSuffixes 是凭据类后缀，不允许通过后台改写。
//
// 为什么禁止：后台是网页入口，凭据一旦可在页面改写，等于把 .env 里的
// API Key / Token 暴露成可写入面；误改还会让 bot 直接失去凭证。
// 需要换 key 时应当手工编辑 .env，这是有意的取舍。
var sensitiveSuffixes = []string{"_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASS", "_SALT"}

// boolLiteralPattern 限定可写值：只允许开关类字面量，
// 防止后台被当成通用文本编辑器把 .env 写坏。
var boolLiteralPattern = regexp.MustCompile("^(true|false|1|0)$")

// EditableField 描述一个可写配置项。
type EditableField struct {
	Key        string   `json:"key"`
	Value      string   `json:"value"`
	Writable   bool     `json:"writable"`
	InEnvFiles []string `json:"in_env_files"`
}

// ConfigWriter 负责把开关写回 .env / .env.dev。
type ConfigWriter struct {
	projectRoot string
	mu          sync.Mutex
}

func NewConfigWriter(projectRoot string) *ConfigWriter {
	return &ConfigWriter{projectRoot: projectRoot}
}

// IsSensitiveKey 判断键是否为凭据类（不可通过后台修改）。
func IsSensitiveKey(key string) bool {
	upper := strings.ToUpper(key)
	for _, suffix := range sensitiveSuffixes {
		if strings.HasSuffix(upper, suffix) {
			return true
		}
	}
	return false
}

// ValidateKey 校验键名是否可写，返回人类可读的拒绝原因。
func ValidateKey(key string) error {
	if !envKeyPattern.MatchString(key) {
		return fmt.Errorf("非法配置键名: %s", key)
	}
	if !strings.HasPrefix(key, "DL_SENPAI_") && !strings.HasPrefix(key, "LLM_") {
		return fmt.Errorf("不支持修改该前缀的配置: %s", key)
	}
	if IsSensitiveKey(key) {
		return fmt.Errorf("凭据类配置不允许通过后台修改: %s", key)
	}
	return nil
}

// ListEditable 返回两个 env 文件里所有可写的布尔开关（值取生效值）。
func (w *ConfigWriter) ListEditable() []EditableField {
	present := map[string][]string{}
	values := map[string]string{}
	for _, name := range writableEnvFiles {
		for k, v := range parseEnvFileSimple(filepath.Join(w.projectRoot, name)) {
			present[k] = append(present[k], name)
			// 靠后的文件优先级更高，直接覆盖即得生效值
			values[k] = v
		}
	}
	keys := make([]string, 0, len(present))
	for k := range present {
		if ValidateKey(k) == nil && boolLiteralPattern.MatchString(values[k]) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	out := make([]EditableField, 0, len(keys))
	for _, k := range keys {
		out = append(out, EditableField{
			Key:        k,
			Value:      values[k],
			Writable:   true,
			InEnvFiles: present[k],
		})
	}
	return out
}

// SetFlag 把布尔开关写入**所有声明过该键的 env 文件**。
//
// 为什么要写全部而不是只写 .env：
//
//	.env.dev 优先级更高。若某键在 .env.dev 也声明过，只改 .env 会被
//	.env.dev 的旧值覆盖，页面点了开关却毫无效果，且**毫无报错**，极难排查。
//	因此凡是出现过的文件都改成同一个值，保证「页面显示什么就生效什么」。
//
// 若两个文件都没有该键，则追加到优先级最低的 .env（新配置项的默认落点）。
func (w *ConfigWriter) SetFlag(key, value string) ([]string, error) {
	if err := ValidateKey(key); err != nil {
		return nil, err
	}
	if !boolLiteralPattern.MatchString(value) {
		return nil, fmt.Errorf("值必须是 true/false/1/0，收到: %s", value)
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	touched := []string{}
	for _, name := range writableEnvFiles {
		path := filepath.Join(w.projectRoot, name)
		body, err := os.ReadFile(path)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		updated, changed := replaceEnvKey(string(body), key, value)
		if !changed {
			continue
		}
		if err := atomicWrite(path, []byte(updated)); err != nil {
			return touched, err
		}
		touched = append(touched, name)
	}

	if len(touched) == 0 {
		// 两个文件都没有：追加到优先级最低的 .env
		path := filepath.Join(w.projectRoot, writableEnvFiles[0])
		body, err := os.ReadFile(path)
		if err != nil && !os.IsNotExist(err) {
			return nil, err
		}
		updated, _ := replaceEnvKey(string(body), key, value)
		if err := atomicWrite(path, []byte(updated)); err != nil {
			return nil, err
		}
		touched = append(touched, writableEnvFiles[0])
	}
	return touched, nil
}

// replaceEnvKey 就地替换键值，保留注释、空行与行序。
//
// 返回 (新内容, 是否命中)。只替换**未注释**的那一行——
// 被 # 注释掉的同名键不动，避免把注释里的示例意外激活。
func replaceEnvKey(body, key, value string) (string, bool) {
	lines := strings.Split(body, "\n")
	found := false
	re := regexp.MustCompile("^(\\s*)" + regexp.QuoteMeta(key) + "\\s*=.*$")
	for i, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		if m := re.FindStringSubmatch(line); m != nil {
			lines[i] = m[1] + key + "=" + value
			found = true
		}
	}
	if !found {
		out := strings.TrimRight(body, "\n")
		if out != "" {
			out += "\n"
		}
		out += key + "=" + value + "\n"
		return out, true
	}
	return strings.Join(lines, "\n"), true
}

// atomicWrite 先写临时文件再改名，避免写一半崩溃留下残缺 .env。
func atomicWrite(path string, data []byte) error {
	tmp := path + ".admin.tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// parseEnvFileSimple 判断某个键出现在哪些文件。
func parseEnvFileSimple(path string) map[string]string {
	out := map[string]string{}
	data, err := os.ReadFile(path)
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		idx := strings.Index(line, "=")
		if idx <= 0 {
			continue
		}
		key := strings.TrimSpace(line[:idx])
		val := strings.TrimSpace(line[idx+1:])
		val = strings.Trim(val, "\"")
		val = strings.Trim(val, "'")
		out[key] = val
	}
	return out
}
