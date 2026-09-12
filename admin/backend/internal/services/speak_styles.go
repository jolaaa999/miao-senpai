package services

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const builtinSpeakStyleID = "senpai_default"

type SpeakStyle struct {
	ID              string  `json:"id"`
	Name            string  `json:"name"`
	Kind            string  `json:"kind"`
	Enabled         bool    `json:"enabled"`
	Model           string  `json:"model"`
	BaseURL         string  `json:"base_url"`
	APIKey          string  `json:"api_key"`
	SystemOverlay   string  `json:"system_overlay"`
	SourcePerson    string  `json:"source_person"`
	QQDataNote      string  `json:"qq_data_note"`
	TrainedModelRef string  `json:"trained_model_ref"`
	Notes           string  `json:"notes"`
	UpdatedAt       float64 `json:"updated_at"`
}

type speakStylesFile struct {
	ActiveID string       `json:"active_id"`
	Styles   []SpeakStyle `json:"styles"`
}

type SpeakStylesSnapshot struct {
	ActiveID string       `json:"active_id"`
	Active   SpeakStyle   `json:"active"`
	Styles   []SpeakStyle `json:"styles"`
	Path     string       `json:"path"`
}

type SpeakStylesService struct {
	mu   sync.Mutex
	path string
}

func NewSpeakStylesService(projectRoot string) *SpeakStylesService {
	dir := filepath.Join(projectRoot, "data", "dl_senpai", "speak_styles")
	_ = os.MkdirAll(dir, 0o755)
	return &SpeakStylesService{path: filepath.Join(dir, "styles.json")}
}

func defaultSpeakStyles() speakStylesFile {
	now := float64(time.Now().Unix())
	return speakStylesFile{
		ActiveID: builtinSpeakStyleID,
		Styles: []SpeakStyle{{
			ID:           builtinSpeakStyleID,
			Name:         "默认学姐",
			Kind:         "builtin",
			Enabled:      true,
			SourcePerson: "学姐人设",
			Notes:        "使用 .env 全局模型 + persona.py",
			UpdatedAt:    now,
		}},
	}
}

func (s *SpeakStylesService) read() (speakStylesFile, error) {
	if _, err := os.Stat(s.path); os.IsNotExist(err) {
		d := defaultSpeakStyles()
		if err := s.write(d); err != nil {
			return d, err
		}
		return d, nil
	}
	raw, err := os.ReadFile(s.path)
	if err != nil {
		return speakStylesFile{}, err
	}
	var data speakStylesFile
	if err := json.Unmarshal(raw, &data); err != nil {
		return speakStylesFile{}, err
	}
	if len(data.Styles) == 0 {
		data = defaultSpeakStyles()
	}
	hasBuiltin := false
	for _, st := range data.Styles {
		if st.ID == builtinSpeakStyleID {
			hasBuiltin = true
			break
		}
	}
	if !hasBuiltin {
		d := defaultSpeakStyles()
		data.Styles = append([]SpeakStyle{d.Styles[0]}, data.Styles...)
	}
	if data.ActiveID == "" {
		data.ActiveID = builtinSpeakStyleID
	}
	return data, nil
}

func (s *SpeakStylesService) write(data speakStylesFile) error {
	_ = os.MkdirAll(filepath.Dir(s.path), 0o755)
	b, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, append(b, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func (s *SpeakStylesService) find(data speakStylesFile, id string) (SpeakStyle, bool) {
	for _, st := range data.Styles {
		if st.ID == id {
			return st, true
		}
	}
	return SpeakStyle{}, false
}

func (s *SpeakStylesService) Snapshot() (*SpeakStylesSnapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := s.read()
	if err != nil {
		return nil, err
	}
	active, ok := s.find(data, data.ActiveID)
	if !ok {
		active, _ = s.find(data, builtinSpeakStyleID)
		data.ActiveID = builtinSpeakStyleID
	}
	return &SpeakStylesSnapshot{
		ActiveID: data.ActiveID,
		Active:   active,
		Styles:   data.Styles,
		Path:     filepath.ToSlash(filepath.Join("data", "dl_senpai", "speak_styles", "styles.json")),
	}, nil
}

func (s *SpeakStylesService) SetActive(id string) (*SpeakStylesSnapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := s.read()
	if err != nil {
		return nil, err
	}
	st, ok := s.find(data, id)
	if !ok {
		return nil, fmt.Errorf("未知风格: %s", id)
	}
	if !st.Enabled {
		return nil, fmt.Errorf("风格已禁用: %s", id)
	}
	data.ActiveID = id
	if err := s.write(data); err != nil {
		return nil, err
	}
	return &SpeakStylesSnapshot{
		ActiveID: id,
		Active:   st,
		Styles:   data.Styles,
		Path:     filepath.ToSlash(filepath.Join("data", "dl_senpai", "speak_styles", "styles.json")),
	}, nil
}

func (s *SpeakStylesService) Upsert(st SpeakStyle) (*SpeakStylesSnapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if st.ID == "" {
		return nil, fmt.Errorf("id 不能为空")
	}
	if st.Name == "" {
		st.Name = st.ID
	}
	if st.ID == builtinSpeakStyleID {
		st.Kind = "builtin"
	} else if st.Kind == "" {
		st.Kind = "finetune"
	}
	st.UpdatedAt = float64(time.Now().Unix())
	data, err := s.read()
	if err != nil {
		return nil, err
	}
	replaced := false
	for i := range data.Styles {
		if data.Styles[i].ID == st.ID {
			data.Styles[i] = st
			replaced = true
			break
		}
	}
	if !replaced {
		data.Styles = append(data.Styles, st)
	}
	if err := s.write(data); err != nil {
		return nil, err
	}
	active, _ := s.find(data, data.ActiveID)
	return &SpeakStylesSnapshot{
		ActiveID: data.ActiveID,
		Active:   active,
		Styles:   data.Styles,
		Path:     filepath.ToSlash(filepath.Join("data", "dl_senpai", "speak_styles", "styles.json")),
	}, nil
}

func (s *SpeakStylesService) Delete(id string) (*SpeakStylesSnapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if id == builtinSpeakStyleID {
		return nil, fmt.Errorf("不能删除默认学姐风格")
	}
	data, err := s.read()
	if err != nil {
		return nil, err
	}
	out := make([]SpeakStyle, 0, len(data.Styles))
	for _, st := range data.Styles {
		if st.ID != id {
			out = append(out, st)
		}
	}
	data.Styles = out
	if data.ActiveID == id {
		data.ActiveID = builtinSpeakStyleID
	}
	if err := s.write(data); err != nil {
		return nil, err
	}
	active, _ := s.find(data, data.ActiveID)
	return &SpeakStylesSnapshot{
		ActiveID: data.ActiveID,
		Active:   active,
		Styles:   data.Styles,
		Path:     filepath.ToSlash(filepath.Join("data", "dl_senpai", "speak_styles", "styles.json")),
	}, nil
}
