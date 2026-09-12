package services

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type ChatTurn struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type SessionSummary struct {
	SessionID   string    `json:"session_id"`
	SessionType string    `json:"session_type"` // group | private
	RefID       string    `json:"ref_id"`
	TurnCount   int       `json:"turn_count"`
	MessageCount int      `json:"message_count"`
	UpdatedAt   time.Time `json:"updated_at"`
	Preview     string    `json:"preview"`
}

type MemoryService struct {
	dir string
}

func NewMemoryService(dir string) *MemoryService {
	return &MemoryService{dir: dir}
}

var sessionFileRe = regexp.MustCompile(`^(.+)\.json$`)

func (s *MemoryService) fileToSessionID(filename string) string {
	base := strings.TrimSuffix(filename, ".json")
	// reverse safe replacement: group_123 -> group:123 is ambiguous if original had _
	// Python uses: re.sub(r"[^\w\-.:]", "_", session_id)
	// We store session_id in response by parsing content or using known patterns
	if strings.HasPrefix(base, "group_") {
		return "group:" + strings.TrimPrefix(base, "group_")
	}
	if strings.HasPrefix(base, "private_") {
		return "private:" + strings.TrimPrefix(base, "private_")
	}
	// fallback: try colon variants
	if idx := strings.Index(base, "_"); idx > 0 {
		prefix := base[:idx]
		if prefix == "group" || prefix == "private" {
			return prefix + ":" + base[idx+1:]
		}
	}
	return strings.ReplaceAll(base, "_", ":")
}

func (s *MemoryService) sessionIDToFile(sessionID string) string {
	safe := regexp.MustCompile(`[^\w\-.]`).ReplaceAllString(sessionID, "_")
	return safe + ".json"
}

func (s *MemoryService) ListSessions() ([]SessionSummary, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return []SessionSummary{}, nil
		}
		return nil, err
	}
	out := make([]SessionSummary, 0)
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		sid := s.fileToSessionID(e.Name())
		turns, modTime, err := s.loadFile(filepath.Join(s.dir, e.Name()))
		if err != nil {
			continue
		}
		summary := SessionSummary{
			SessionID:    sid,
			TurnCount:    (len(turns) + 1) / 2,
			MessageCount: len(turns),
			UpdatedAt:    modTime,
		}
		parts := strings.SplitN(sid, ":", 2)
		if len(parts) == 2 {
			summary.SessionType = parts[0]
			summary.RefID = parts[1]
		}
		if len(turns) > 0 {
			summary.Preview = truncate(turns[len(turns)-1].Content, 80)
		}
		out = append(out, summary)
	}
	return out, nil
}

func (s *MemoryService) loadFile(path string) ([]ChatTurn, time.Time, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, time.Time{}, err
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, time.Time{}, err
	}
	var raw []map[string]string
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, time.Time{}, err
	}
	var turns []ChatTurn
	for _, item := range raw {
		role := item["role"]
		content := item["content"]
		if role != "user" && role != "assistant" {
			continue
		}
		turns = append(turns, ChatTurn{Role: role, Content: content})
	}
	return turns, info.ModTime(), nil
}

func (s *MemoryService) GetTurns(sessionID string) ([]ChatTurn, error) {
	path := filepath.Join(s.dir, s.sessionIDToFile(sessionID))
	turns, _, err := s.loadFile(path)
	return turns, err
}

func (s *MemoryService) ClearSession(sessionID string) error {
	path := filepath.Join(s.dir, s.sessionIDToFile(sessionID))
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return nil
	}
	return os.Remove(path)
}

func truncate(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n]) + "…"
}
