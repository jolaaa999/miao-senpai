package services

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type DrawItem struct {
	ID           string    `json:"id"`
	Prompt       string    `json:"prompt"`
	Scene        string    `json:"scene"`
	UserRequest  string    `json:"user_request"`
	SourceUser   string    `json:"source_user"`
	SourceUserID string    `json:"source_user_id"`
	LocalPath    string    `json:"local_path"`
	Model        string    `json:"model"`
	Size         string    `json:"size"`
	CreatedAt    string    `json:"created_at"`
	HasLocalFile bool      `json:"has_local_file"`
}

type DrawSession struct {
	SessionID string    `json:"session_id"`
	ItemCount int       `json:"item_count"`
	UpdatedAt time.Time `json:"updated_at"`
}

type DrawService struct {
	dir string
}

func NewDrawService(dir string) *DrawService {
	return &DrawService{dir: dir}
}

func (s *DrawService) fileToSessionID(name string) string {
	base := strings.TrimSuffix(name, ".json")
	if strings.HasPrefix(base, "group_") {
		return "group:" + strings.TrimPrefix(base, "group_")
	}
	if strings.HasPrefix(base, "private_") {
		return "private:" + strings.TrimPrefix(base, "private_")
	}
	return strings.ReplaceAll(base, "_", ":")
}

func (s *DrawService) sessionIDToFile(sessionID string) string {
	safe := regexp.MustCompile(`[^\w\-.]`).ReplaceAllString(sessionID, "_")
	return safe + ".json"
}

func (s *DrawService) indexByAssetID() map[string]DrawItem {
	out := make(map[string]DrawItem)
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return out
	}
	for _, e := range entries {
		if e.IsDir() || e.Name() == "files" || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		path := filepath.Join(s.dir, e.Name())
		items, err := s.loadItems(path)
		if err != nil {
			continue
		}
		for _, it := range items {
			if it.ID != "" {
				out[it.ID] = it
			}
		}
	}
	return out
}

func (s *DrawService) ListAssetFiles() ([]DrawItem, error) {
	filesDir := filepath.Join(s.dir, "files")
	entries, err := os.ReadDir(filesDir)
	if err != nil {
		if os.IsNotExist(err) {
			return []DrawItem{}, nil
		}
		return nil, err
	}
	index := s.indexByAssetID()
	var out []DrawItem
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		name := e.Name()
		ext := filepath.Ext(name)
		id := strings.TrimSuffix(name, ext)
		full := filepath.Join(filesDir, name)
		item := DrawItem{
			ID:           id,
			Prompt:       "（无索引元数据，仅扫描到图片文件）",
			HasLocalFile: true,
			LocalPath:    full,
			CreatedAt:    info.ModTime().Format(time.RFC3339),
		}
		if meta, ok := index[id]; ok {
			item = meta
			item.HasLocalFile = true
			item.LocalPath = full
			if item.CreatedAt == "" {
				item.CreatedAt = info.ModTime().Format(time.RFC3339)
			}
		}
		out = append(out, item)
	}
	return out, nil
}

func (s *DrawService) AssetPath(filename string) (string, error) {
	if filename == "" || strings.Contains(filename, "..") || strings.ContainsAny(filename, `/\`) {
		return "", os.ErrPermission
	}
	abs := filepath.Join(s.dir, "files", filename)
	abs, err := filepath.Abs(abs)
	if err != nil {
		return "", err
	}
	filesRoot, _ := filepath.Abs(filepath.Join(s.dir, "files"))
	if !strings.HasPrefix(abs, filesRoot+string(os.PathSeparator)) {
		return "", os.ErrPermission
	}
	if _, err := os.Stat(abs); err != nil {
		return "", err
	}
	return abs, nil
}

func (s *DrawService) ListSessions() ([]DrawSession, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return []DrawSession{}, nil
		}
		return nil, err
	}
	var out []DrawSession
	for _, e := range entries {
		if e.IsDir() || e.Name() == "files" || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		path := filepath.Join(s.dir, e.Name())
		info, err := os.Stat(path)
		if err != nil {
			continue
		}
		items, _ := s.loadItems(path)
		out = append(out, DrawSession{
			SessionID: s.fileToSessionID(e.Name()),
			ItemCount: len(items),
			UpdatedAt: info.ModTime(),
		})
	}
	assets, _ := s.ListAssetFiles()
	if len(assets) > 0 {
		latest := time.Time{}
		for _, a := range assets {
			if t, err := time.Parse(time.RFC3339, a.CreatedAt); err == nil && t.After(latest) {
				latest = t
			}
		}
		if latest.IsZero() {
			latest = time.Now()
		}
		out = append(out, DrawSession{
			SessionID: "__all_files__",
			ItemCount: len(assets),
			UpdatedAt: latest,
		})
	}
	return out, nil
}

func (s *DrawService) loadItems(path string) ([]DrawItem, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return []DrawItem{}, nil
	}
	var raw []DrawItem
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, err
	}
	for i := range raw {
		if raw[i].LocalPath != "" {
			if _, err := os.Stat(raw[i].LocalPath); err == nil {
				raw[i].HasLocalFile = true
			}
		}
	}
	return raw, nil
}

func (s *DrawService) GetItems(sessionID string) ([]DrawItem, error) {
	if sessionID == "__all_files__" {
		return s.ListAssetFiles()
	}
	path := filepath.Join(s.dir, s.sessionIDToFile(sessionID))
	return s.loadItems(path)
}

func (s *DrawService) DeleteItem(sessionID, itemID string) error {
	path := filepath.Join(s.dir, s.sessionIDToFile(sessionID))
	items, err := s.loadItems(path)
	if err != nil {
		return err
	}
	var kept []DrawItem
	var removed *DrawItem
	for _, it := range items {
		if it.ID == itemID {
			copy := it
			removed = &copy
			continue
		}
		kept = append(kept, it)
	}
	if removed == nil {
		return nil
	}
	out, err := json.MarshalIndent(kept, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(path, out, 0644); err != nil {
		return err
	}
	if removed.LocalPath != "" {
		_ = os.Remove(removed.LocalPath)
	}
	return nil
}
