package services

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type StickerItem struct {
	ID           string            `json:"id"`
	SegmentType  string            `json:"segment_type"`
	SegmentData  map[string]any    `json:"segment_data"`
	ContextText  string            `json:"context_text"`
	Keywords     []string          `json:"keywords"`
	UseCount     int               `json:"use_count"`
	CollectedAt  string            `json:"collected_at"`
	SourceUser   string            `json:"source_user"`
	LocalPath    string            `json:"local_path"`
	HasLocalFile bool              `json:"has_local_file"`
}

type StickerSession struct {
	SessionID   string    `json:"session_id"`
	ItemCount   int       `json:"item_count"`
	UpdatedAt   time.Time `json:"updated_at"`
}

type StickerService struct {
	dir string
}

func NewStickerService(dir string) *StickerService {
	return &StickerService{dir: dir}
}

func (s *StickerService) fileToSessionID(name string) string {
	base := strings.TrimSuffix(name, ".json")
	if strings.HasPrefix(base, "group_") {
		return "group:" + strings.TrimPrefix(base, "group_")
	}
	if strings.HasPrefix(base, "private_") {
		return "private:" + strings.TrimPrefix(base, "private_")
	}
	return strings.ReplaceAll(base, "_", ":")
}

func (s *StickerService) sessionIDToFile(sessionID string) string {
	safe := regexp.MustCompile(`[^\w\-.]`).ReplaceAllString(sessionID, "_")
	return safe + ".json"
}

func (s *StickerService) ListAssetFiles() ([]StickerItem, error) {
	filesDir := filepath.Join(s.dir, "files")
	entries, err := os.ReadDir(filesDir)
	if err != nil {
		if os.IsNotExist(err) {
			return []StickerItem{}, nil
		}
		return nil, err
	}
	var out []StickerItem
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
		out = append(out, StickerItem{
			ID:           id,
			ContextText:  "从 files/ 目录扫描（历史索引可能因 Windows 路径问题损坏）",
			HasLocalFile: true,
			LocalPath:    full,
			CollectedAt:  info.ModTime().Format(time.RFC3339),
		})
	}
	return out, nil
}

func (s *StickerService) AssetPath(filename string) (string, error) {
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

func (s *StickerService) ListSessions() ([]StickerSession, error) {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		if os.IsNotExist(err) {
			return []StickerSession{}, nil
		}
		return nil, err
	}
	var out []StickerSession
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
		out = append(out, StickerSession{
			SessionID: s.fileToSessionID(e.Name()),
			ItemCount: len(items),
			UpdatedAt: info.ModTime(),
		})
	}
	assets, _ := s.ListAssetFiles()
	if len(assets) > 0 {
		latest := time.Time{}
		for _, a := range assets {
			if t, err := time.Parse(time.RFC3339, a.CollectedAt); err == nil && t.After(latest) {
				latest = t
			}
		}
		if latest.IsZero() {
			latest = time.Now()
		}
		out = append([]StickerSession{{
			SessionID: "__all_files__",
			ItemCount: len(assets),
			UpdatedAt: latest,
		}}, out...)
	}
	return out, nil
}

func (s *StickerService) loadItems(path string) ([]StickerItem, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 {
		return []StickerItem{}, nil
	}
	var raw []StickerItem
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

func (s *StickerService) GetItems(sessionID string) ([]StickerItem, error) {
	if sessionID == "__all_files__" {
		return s.ListAssetFiles()
	}
	path := filepath.Join(s.dir, s.sessionIDToFile(sessionID))
	return s.loadItems(path)
}

func (s *StickerService) DeleteItem(sessionID, itemID string) error {
	path := filepath.Join(s.dir, s.sessionIDToFile(sessionID))
	items, err := s.loadItems(path)
	if err != nil {
		return err
	}
	var kept []StickerItem
	var removed *StickerItem
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

func (s *StickerService) LocalFilePath(localPath string) string {
	if localPath == "" {
		return ""
	}
	if filepath.IsAbs(localPath) {
		return localPath
	}
	return filepath.Join(s.dir, "files", localPath)
}
