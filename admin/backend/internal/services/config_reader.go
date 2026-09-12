package services

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type TrendItem struct {
	Source string `json:"source"`
	Title  string `json:"title"`
	Rank   int    `json:"rank"`
	URL    string `json:"url"`
	Hot    string `json:"hot"`
}

type TrendsSnapshot struct {
	FetchedAt time.Time   `json:"fetched_at"`
	BaseURL   string      `json:"base_url"`
	Items     []TrendItem `json:"items"`
	ItemCount int         `json:"item_count"`
}

type trendsCacheFile struct {
	FetchedAt float64     `json:"fetched_at"`
	BaseURL   string      `json:"base_url"`
	Items     []TrendItem `json:"items"`
}

type TrendsService struct {
	dir string
}

func NewTrendsService(dir string) *TrendsService {
	return &TrendsService{dir: dir}
}

func (s *TrendsService) GetSnapshot() (*TrendsSnapshot, error) {
	path := filepath.Join(s.dir, "cache.json")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return &TrendsSnapshot{Items: []TrendItem{}}, nil
		}
		return nil, err
	}
	var raw trendsCacheFile
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, err
	}
	snap := &TrendsSnapshot{
		BaseURL:   raw.BaseURL,
		Items:     raw.Items,
		ItemCount: len(raw.Items),
	}
	if raw.FetchedAt > 0 {
		snap.FetchedAt = time.Unix(int64(raw.FetchedAt), int64((raw.FetchedAt-float64(int64(raw.FetchedAt)))*1e9))
	}
	return snap, nil
}

type FeatureSwitch struct {
	Key         string `json:"key"`
	Value       string `json:"value"`
	Group       string `json:"group"`
	Masked      bool   `json:"masked"`
	Description string `json:"description"`
}

type ConfigReader struct {
	projectRoot string
}

func NewConfigReader(projectRoot string) *ConfigReader {
	return &ConfigReader{projectRoot: projectRoot}
}

var sensitiveKeys = []string{"KEY", "TOKEN", "SECRET", "PASSWORD", "PASS"}

func (r *ConfigReader) isSensitive(key string) bool {
	upper := strings.ToUpper(key)
	for _, s := range sensitiveKeys {
		if strings.Contains(upper, s) {
			return true
		}
	}
	return false
}

func maskValue(v string) string {
	if len(v) <= 8 {
		return "****"
	}
	return v[:4] + "····" + v[len(v)-4:]
}

func (r *ConfigReader) parseEnvFile(path string) map[string]string {
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
		val = strings.Trim(val, `"'`)
		out[key] = val
	}
	return out
}

func (r *ConfigReader) GetFeatureSwitches() []FeatureSwitch {
	merged := map[string]string{}
	for _, name := range []string{".env", ".env.dev"} {
		for k, v := range r.parseEnvFile(filepath.Join(r.projectRoot, name)) {
			merged[k] = v
		}
	}
	keys := map[string]struct{}{}
	for k := range merged {
		keys[k] = struct{}{}
	}
	for k := range configDescriptions {
		keys[k] = struct{}{}
	}
	var out []FeatureSwitch
	prefixes := map[string]string{
		"DL_SENPAI_": "学姐插件",
		"OPENAI_":    "LLM 中转",
		"OLLAMA_":    "Ollama",
		"LLM_":       "LLM 通道",
	}
	for k := range keys {
		if !strings.HasPrefix(k, "DL_SENPAI_") && !strings.HasPrefix(k, "OPENAI_") &&
			!strings.HasPrefix(k, "OLLAMA_") && !strings.HasPrefix(k, "LLM_") {
			continue
		}
		group := "其它"
		for p, g := range prefixes {
			if strings.HasPrefix(k, p) {
				group = g
				break
			}
		}
		v, fromEnv := merged[k]
		masked := r.isSensitive(k)
		display := v
		if !fromEnv {
			display = "（默认，未写入 .env）"
		} else if masked && v != "" {
			display = maskValue(v)
		}
		out = append(out, FeatureSwitch{
			Key:         k,
			Value:       display,
			Group:       group,
			Masked:      masked,
			Description: describeConfigKey(k),
		})
	}
	return out
}
