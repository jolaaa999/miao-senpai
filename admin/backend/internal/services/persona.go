package services

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type PersonaDraft struct {
	SenpaiName          string    `json:"senpai_name"`
	SystemPrompt        string    `json:"system_prompt"`
	Appearance          string    `json:"appearance"`
	MonsterHunterExtra  string    `json:"monster_hunter_extra"`
	FilePath            string    `json:"file_path"`
	UpdatedAt           time.Time `json:"updated_at"`
}

type PersonaService struct {
	personaPath string
}

func NewPersonaService(projectRoot string) *PersonaService {
	return &PersonaService{
		personaPath: filepath.Join(projectRoot, "src", "plugins", "dl_senpai", "persona.py"),
	}
}

var (
	reSenpaiName = regexp.MustCompile(`(?m)^SENPAI_NAME\s*=\s*["']([^"']*)["']`)
	reTripleStr  = regexp.MustCompile(`(?ms)^([A-Z_]+)\s*=\s*"""([\s\S]*?)"""`)
)

func extractAppearance(systemPrompt string) string {
	start := strings.Index(systemPrompt, "【外貌自设】")
	if start < 0 {
		return ""
	}
	rest := systemPrompt[start+len("【外貌自设】"):]
	end := strings.Index(rest, "\n【")
	if end < 0 {
		return strings.TrimSpace(rest)
	}
	return strings.TrimSpace(rest[:end])
}

func mergeAppearance(systemPrompt, appearance string) string {
	appearance = strings.TrimSpace(appearance)
	if appearance == "" {
		return systemPrompt
	}
	start := strings.Index(systemPrompt, "【外貌自设】")
	if start < 0 {
		return systemPrompt + "\n\n【外貌自设】\n" + appearance
	}
	rest := systemPrompt[start+len("【外貌自设】"):]
	end := strings.Index(rest, "\n【")
	if end < 0 {
		return systemPrompt[:start] + "【外貌自设】\n" + appearance
	}
	return systemPrompt[:start] + "【外貌自设】\n" + appearance + rest[end:]
}

func (s *PersonaService) GetDraft() (*PersonaDraft, error) {
	data, err := os.ReadFile(s.personaPath)
	if err != nil {
		return nil, err
	}
	content := string(data)
	info, _ := os.Stat(s.personaPath)

	draft := &PersonaDraft{
		FilePath:  filepath.ToSlash(filepath.Join("src", "plugins", "dl_senpai", "persona.py")),
		UpdatedAt: info.ModTime(),
	}

	if m := reSenpaiName.FindStringSubmatch(content); len(m) > 1 {
		draft.SenpaiName = m[1]
	}

	for _, m := range reTripleStr.FindAllStringSubmatch(content, -1) {
		if len(m) < 3 {
			continue
		}
		switch m[1] {
		case "SYSTEM_PROMPT":
			draft.SystemPrompt = strings.TrimRight(m[2], "\n")
		case "MONSTER_HUNTER_SYSTEM_EXTRA":
			draft.MonsterHunterExtra = strings.TrimRight(m[2], "\n")
		}
	}

	draft.Appearance = extractAppearance(draft.SystemPrompt)
	return draft, nil
}

func (s *PersonaService) SaveDraft(draft PersonaDraft) error {
	data, err := os.ReadFile(s.personaPath)
	if err != nil {
		return err
	}
	content := string(data)

	systemPrompt := mergeAppearance(strings.TrimSpace(draft.SystemPrompt), draft.Appearance)

	content = replaceSenpaiName(content, strings.TrimSpace(draft.SenpaiName))
	content = replaceTripleString(content, "SYSTEM_PROMPT", systemPrompt)
	content = replaceTripleString(content, "MONSTER_HUNTER_SYSTEM_EXTRA", strings.TrimSpace(draft.MonsterHunterExtra))

	tmp := s.personaPath + ".tmp"
	if err := os.WriteFile(tmp, []byte(content), 0644); err != nil {
		return err
	}
	return os.Rename(tmp, s.personaPath)
}

func replaceSenpaiName(content, name string) string {
	if name == "" {
		name = "学姐"
	}
	if reSenpaiName.MatchString(content) {
		return reSenpaiName.ReplaceAllString(content, fmt.Sprintf(`SENPAI_NAME = "%s"`, escapePyDouble(name)))
	}
	return content
}

func replaceTripleString(content, constName, body string) string {
	pat := regexp.MustCompile(`(?ms)^` + constName + `\s*=\s*"""[\s\S]*?"""`)
	replacement := constName + ` = """` + body + `"""`
	if pat.MatchString(content) {
		return pat.ReplaceAllString(content, replacement)
	}
	return content
}

func escapePyDouble(s string) string {
	return strings.ReplaceAll(s, `\`, `\\`)
}

type PersonaPatch struct {
	SenpaiName         *string `json:"senpai_name"`
	SystemPrompt       *string `json:"system_prompt"`
	Appearance         *string `json:"appearance"`
	MonsterHunterExtra *string `json:"monster_hunter_extra"`
}

func (s *PersonaService) ApplyPatch(patch PersonaPatch) error {
	current, err := s.GetDraft()
	if err != nil {
		return err
	}
	if patch.SenpaiName != nil {
		current.SenpaiName = *patch.SenpaiName
	}
	if patch.SystemPrompt != nil {
		current.SystemPrompt = *patch.SystemPrompt
	}
	if patch.Appearance != nil {
		current.Appearance = *patch.Appearance
	}
	if patch.MonsterHunterExtra != nil {
		current.MonsterHunterExtra = *patch.MonsterHunterExtra
	}
	return s.SaveDraft(*current)
}
