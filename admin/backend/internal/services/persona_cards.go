package services

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

// 人设卡：与 Python 侧 src/plugins/dl_senpai/personas.py 读同一批文件。
//
// 为什么不复用 PersonaService：那个服务读写的是 persona.py（源码里的定稿），
// 这里读写的是 data 目录下的 md 卡与绑定表。两者唯一的交汇点是
// source_hash —— 用来发现「卡是导出的副本，但源头后来改过了」。
//
// 后端只做「列 / 绑 / 解绑 / 导出 / 看正文」：
// 编辑卡正文仍推荐直接改 md（体检在 Python 侧，两套 lint 必然漂移）。

type PersonaCardInfo struct {
	ID     string   `json:"id"`
	Name   string   `json:"name"`
	Desc   string   `json:"description"`
	Groups []string `json:"groups"`
	// Chars 是字符数（不是字节），用于「这张卡有多大」的直观判断
	Chars     int       `json:"chars"`
	File      string    `json:"file"`
	SourceRef string    `json:"source_ref"`
	// SourceSynced 三态：null = 没有可对账的来源，true/false = 与 persona.py 同步/不同步
	SourceSynced *bool     `json:"source_synced"`
	UpdatedAt    time.Time `json:"updated_at"`
}

type PersonaCardsSnapshot struct {
	Dir       string            `json:"dir"`
	Cards     []PersonaCardInfo `json:"cards"`
	Bindings  map[string]string `json:"bindings"`
	DefaultID string            `json:"default_id"`
	Note      string            `json:"note"`
}

type PersonaCardBody struct {
	ID   string `json:"id"`
	Name string `json:"name"`
	Body string `json:"body"`
	File string `json:"file"`
}

type PersonaCardsService struct {
	dir         string
	personaPath string
	envFiles    []string
}

func NewPersonaCardsService(projectRoot string) *PersonaCardsService {
	return &PersonaCardsService{
		dir:         filepath.Join(projectRoot, "data", "dl_senpai", "personas"),
		personaPath: filepath.Join(projectRoot, "src", "plugins", "dl_senpai", "persona.py"),
		envFiles: []string{
			filepath.Join(projectRoot, ".env"),
			filepath.Join(projectRoot, ".env.dev"),
		},
	}
}

// 与 Python 侧 personas._DOC_STEMS 保持一致：目录说明/模板不是卡。
var personaDocStems = map[string]bool{
	"readme": true, "index": true, "template": true, "模板": true, "说明": true,
}

var (
	reCardGroups    = regexp.MustCompile(`[,，\s]+`)
	reSystemPrompt  = regexp.MustCompile(`(?ms)^SYSTEM_PROMPT\s*=\s*"""([\s\S]*?)"""`)
)

type personaCardFile struct {
	ID         string
	Name       string
	Desc       string
	Body       string
	SourceRef  string
	SourceHash string
	Groups     []string
	Path       string
	ModTime    time.Time
}

func splitCardGroups(v string) []string {
	out := []string{}
	for _, part := range reCardGroups.Split(v, -1) {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func parsePersonaCardFile(path string) (*personaCardFile, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	text := strings.TrimPrefix(string(data), "\ufeff")
	lines := strings.Split(text, "\n")
	for i, l := range lines {
		lines[i] = strings.TrimRight(l, "\r")
	}

	meta := map[string]string{}
	body := strings.TrimSpace(text)
	if len(lines) > 0 && strings.TrimSpace(lines[0]) == "---" {
		end := -1
		for i := 1; i < len(lines); i++ {
			trimmed := strings.TrimSpace(lines[i])
			if trimmed == "---" || trimmed == "..." {
				end = i
				break
			}
			if trimmed == "" || strings.HasPrefix(trimmed, "#") {
				continue
			}
			if k, v, ok := strings.Cut(trimmed, ":"); ok {
				meta[strings.ToLower(strings.TrimSpace(k))] = strings.Trim(strings.TrimSpace(v), `"'`)
			}
		}
		if end > 0 {
			body = strings.TrimSpace(strings.Join(lines[end+1:], "\n"))
		} else {
			// 没闭合的 front-matter：整篇当正文，与 Python 侧一致
			meta = map[string]string{}
		}
	}

	id := meta["id"]
	if id == "" {
		base := filepath.Base(path)
		id = strings.TrimSuffix(base, filepath.Ext(base))
	}
	name := meta["name"]
	if name == "" {
		name = id
	}

	var mod time.Time
	if info, err := os.Stat(path); err == nil {
		mod = info.ModTime()
	}
	return &personaCardFile{
		ID:         id,
		Name:       name,
		Desc:       strings.TrimSpace(meta["description"]),
		Body:       body,
		SourceRef:  strings.TrimSpace(meta["source_ref"]),
		SourceHash: strings.TrimSpace(meta["source_hash"]),
		Groups:     splitCardGroups(meta["groups"]),
		Path:       path,
		ModTime:    mod,
	}, nil
}

// personaBodyHash 与 Python 侧 personas.body_hash 同算法：
// 归一化换行与行尾空格后再取 sha256 前 16 位 —— 否则 Windows 上每存一次
// 都会报「与源头不一致」，警告很快就被无视了。
func personaBodyHash(text string) string {
	normalized := strings.ReplaceAll(strings.TrimSpace(text), "\r\n", "\n")
	lines := strings.Split(normalized, "\n")
	for i, l := range lines {
		lines[i] = strings.TrimRight(l, " \t")
	}
	sum := sha256.Sum256([]byte(strings.Join(lines, "\n")))
	return hex.EncodeToString(sum[:])[:16]
}

// currentPersonaBody 读 persona.py 里那份定稿正文（导出的源头）。
func (s *PersonaCardsService) currentPersonaBody() (string, error) {
	data, err := os.ReadFile(s.personaPath)
	if err != nil {
		return "", err
	}
	m := reSystemPrompt.FindStringSubmatch(string(data))
	if len(m) < 2 {
		return "", fmt.Errorf("persona.py 里找不到 SYSTEM_PROMPT")
	}
	return strings.TrimSpace(strings.TrimRight(m[1], "\n")), nil
}

func (s *PersonaCardsService) cardPaths() []string {
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return nil
	}
	out := []string{}
	for _, e := range entries {
		if e.IsDir() || !strings.EqualFold(filepath.Ext(e.Name()), ".md") {
			continue
		}
		stem := strings.TrimSuffix(e.Name(), filepath.Ext(e.Name()))
		if stem == "" || strings.HasPrefix(stem, "_") || strings.HasPrefix(stem, ".") {
			continue
		}
		if personaDocStems[strings.ToLower(stem)] {
			continue
		}
		out = append(out, filepath.Join(s.dir, e.Name()))
	}
	sort.Strings(out)
	return out
}

func (s *PersonaCardsService) loadCards() ([]PersonaCardInfo, error) {
	sourceBody, _ := s.currentPersonaBody()
	sourceHash := ""
	if sourceBody != "" {
		sourceHash = personaBodyHash(sourceBody)
	}

	cards := []PersonaCardInfo{}
	seen := map[string]bool{}
	for _, path := range s.cardPaths() {
		card, err := parsePersonaCardFile(path)
		if err != nil || strings.TrimSpace(card.Body) == "" {
			continue
		}
		if seen[card.ID] {
			continue
		}
		seen[card.ID] = true
		info := PersonaCardInfo{
			ID:        card.ID,
			Name:      card.Name,
			Desc:      card.Desc,
			Groups:    card.Groups,
			Chars:     utf8.RuneCountInString(card.Body),
			// 只给文件名：控制台展示用，不暴露绝对路径
			File:      filepath.Base(path),
			SourceRef: card.SourceRef,
			UpdatedAt: card.ModTime,
		}
		if card.SourceRef == "persona.py" && card.SourceHash != "" && sourceHash != "" {
			synced := card.SourceHash == sourceHash
			info.SourceSynced = &synced
		}
		cards = append(cards, info)
	}
	sort.Slice(cards, func(i, j int) bool { return cards[i].ID < cards[j].ID })
	return cards, nil
}

func (s *PersonaCardsService) readBindings() map[string]string {
	out := map[string]string{}
	data, err := os.ReadFile(filepath.Join(s.dir, "bindings.json"))
	if err != nil {
		return out
	}
	var raw struct {
		Bindings map[string]string `json:"bindings"`
	}
	if json.Unmarshal(data, &raw) != nil {
		return out
	}
	for k, v := range raw.Bindings {
		if strings.TrimSpace(k) != "" && strings.TrimSpace(v) != "" {
			out[k] = v
		}
	}
	return out
}

// writeBindings 原子写：先 .tmp 再 rename，避免半截 JSON 让 bot 侧读不进来。
func (s *PersonaCardsService) writeBindings(bindings map[string]string) error {
	if err := os.MkdirAll(s.dir, 0o755); err != nil {
		return err
	}
	path := filepath.Join(s.dir, "bindings.json")

	payload := map[string]any{"bindings": bindings}
	if data, err := os.ReadFile(path); err == nil {
		keep := map[string]json.RawMessage{}
		if json.Unmarshal(data, &keep) == nil {
			delete(keep, "bindings")
			for k, v := range keep {
				var decoded any
				if json.Unmarshal(v, &decoded) == nil {
					payload[k] = decoded
				}
			}
		}
	}

	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// defaultID 读 DL_SENPAI_PERSONA_CARD_DEFAULT。
//
// 先看进程环境，再按 .env -> .env.dev 的顺序读文件（后者覆盖前者），
// 与 bot 侧的 dotenv 语义一致：控制台看到的「默认卡」必须和线上真正用的一致。
func (s *PersonaCardsService) defaultID() string {
	if v := strings.TrimSpace(os.Getenv("DL_SENPAI_PERSONA_CARD_DEFAULT")); v != "" {
		return v
	}
	value := ""
	for _, path := range s.envFiles {
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(strings.TrimRight(line, "\r"))
			if line == "" || strings.HasPrefix(line, "#") {
				continue
			}
			k, v, ok := strings.Cut(line, "=")
			if !ok || strings.TrimSpace(k) != "DL_SENPAI_PERSONA_CARD_DEFAULT" {
				continue
			}
			value = strings.Trim(strings.TrimSpace(v), `"'`)
		}
	}
	return value
}

func (s *PersonaCardsService) Snapshot() (*PersonaCardsSnapshot, error) {
	cards, err := s.loadCards()
	if err != nil {
		return nil, err
	}
	return &PersonaCardsSnapshot{
		Dir:       filepath.ToSlash(s.dir),
		Cards:     cards,
		Bindings:  s.readBindings(),
		DefaultID: s.defaultID(),
		Note: "没绑定的会话继续用 persona.py 那份。群里也能用「/人设 <卡名>」切换，" +
			"下一句就生效；改 .env 的默认卡需要重启 bot。",
	}, nil
}

func (s *PersonaCardsService) findCard(id string) (*personaCardFile, error) {
	wanted := strings.TrimSpace(id)
	if wanted == "" {
		return nil, fmt.Errorf("缺少卡名")
	}
	for _, path := range s.cardPaths() {
		card, err := parsePersonaCardFile(path)
		if err != nil {
			continue
		}
		if card.ID == wanted || strings.EqualFold(card.ID, wanted) || strings.EqualFold(card.Name, wanted) {
			return card, nil
		}
	}
	return nil, fmt.Errorf("没有「%s」这张卡", wanted)
}

// Bind 不校验会话是否真实存在：session 键就是 group:xxx / private:xxx 字符串，
// 由 bot 侧按同样的键去查。写错键不会报错，只会「绑了但没人用」——
// 所以前端的下拉框只列真实会话，手输是逃生口。
func (s *PersonaCardsService) Bind(session, cardID string) (*PersonaCardsSnapshot, error) {
	session = strings.TrimSpace(session)
	if session == "" {
		return nil, fmt.Errorf("缺少会话")
	}
	card, err := s.findCard(cardID)
	if err != nil {
		return nil, err
	}
	bindings := s.readBindings()
	bindings[session] = card.ID
	if err := s.writeBindings(bindings); err != nil {
		return nil, err
	}
	return s.Snapshot()
}

func (s *PersonaCardsService) Unbind(session string) (*PersonaCardsSnapshot, error) {
	session = strings.TrimSpace(session)
	if session == "" {
		return nil, fmt.Errorf("缺少会话")
	}
	bindings := s.readBindings()
	delete(bindings, session)
	if err := s.writeBindings(bindings); err != nil {
		return nil, err
	}
	return s.Snapshot()
}

func (s *PersonaCardsService) Body(id string) (*PersonaCardBody, error) {
	card, err := s.findCard(id)
	if err != nil {
		return nil, err
	}
	return &PersonaCardBody{ID: card.ID, Name: card.Name, Body: card.Body, File: filepath.Base(card.Path)}, nil
}

// Export 把 persona.py 的现役正文存成一张卡。
//
// 为什么要这个按钮：想「在现在的她基础上改一版给某个群」，得先有一份可编辑的
// 副本；直接改 persona.py 会污染所有人的她。
func (s *PersonaCardsService) Export(id string) (*PersonaCardsSnapshot, error) {
	id = strings.TrimSpace(id)
	if id == "" {
		return nil, fmt.Errorf("缺少卡名")
	}
	if strings.ContainsAny(id, `\/:*?"<>|`) {
		return nil, fmt.Errorf("卡名不能带路径符号")
	}
	body, err := s.currentPersonaBody()
	if err != nil {
		return nil, err
	}
	if _, err := s.findCard(id); err == nil {
		return nil, fmt.Errorf("「%s」已经存在，换个名字", id)
	}
	if err := os.MkdirAll(s.dir, 0o755); err != nil {
		return nil, err
	}
	content := "---\n" +
		"id: " + id + "\n" +
		"name: " + id + "\n" +
		"description: 从 persona.py 导出的副本\n" +
		"source_ref: persona.py\n" +
		"source_hash: " + personaBodyHash(body) + "\n" +
		"---\n" + body + "\n"
	path := filepath.Join(s.dir, id+".md")
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(content), 0o644); err != nil {
		return nil, err
	}
	if err := os.Rename(tmp, path); err != nil {
		return nil, err
	}
	return s.Snapshot()
}
